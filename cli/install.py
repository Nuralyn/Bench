"""``bench install`` and ``bench uninstall``: register Bench in a project.

Registering Bench by hand takes three edits that each fail quietly when
missed: copying the settings template and replacing its placeholder paths,
adding ``/.bench/`` to the project's .gitignore before the first governed
edit, and enabling the ledger commit guard. A hook path that does not resolve
makes Bench fail closed on every Write/Edit/MultiEdit in the project, and a
missing ignore line lets the first ``git add -A`` stage the chain. These two
commands make each step explicit, idempotent, and reversible.

Resources come from the installed package, never from a checkout layout:
the hook script and the commit guard are package data of ``hooks``, and the
core constitution is package data of ``pipeline``, so the same lookup holds
in a checkout, an editable install, and a wheel. ``install`` proves all
three are present (the constitution by loading it) before it writes anything,
so it never registers a hook that could only veto.

The hook command pins the interpreter that ran ``bench install``
(``sys.executable``) rather than a bare ``python``: Claude Code runs hooks with
the governed project as the working directory and whatever ``python`` is on
PATH there, which need not be the environment holding Bench's dependencies.

Ownership is recorded, not inferred. ``install`` writes a receipt at
``<project>/.bench/install.json`` naming exactly what it wrote: the hook
command, the env keys it added and their values, whether it added the ignore
line, and the guard's path and digest. ``uninstall`` removes only what the
receipt names and only if it is unchanged since; everything else is reported
and kept, and with no receipt nothing is removed at all. Removing governance
is also gated on a human at a plain terminal, exactly as chain retirement is.

Nothing here touches the ledger, the constitution, or the pipeline. The only
files written are the governed project's ``.claude/settings.json``,
``.gitignore``, git hooks directory, and the receipt.
"""

import hashlib
import importlib.metadata
import importlib.resources
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ledger.retire import AGENT_ENV_MARKERS
from pipeline.constitution import ConstitutionError, load_constitution_snapshot

HOOK_MATCHER: str = "Write|Edit|MultiEdit"
HOOK_PACKAGE: str = "hooks"
HOOK_SCRIPT_NAME: str = "pre-tool-use.py"
GUARD_NAME: str = "pre-commit"
# The substring every Bench hook command contains, in a checkout, an editable
# install, or a wheel (site-packages/hooks/pre-tool-use.py).
HOOK_SCRIPT_RELPATH: str = f"{HOOK_PACKAGE}/{HOOK_SCRIPT_NAME}"
IGNORE_LINE: str = "/.bench/"
RECEIPT_RELPATH: str = ".bench/install.json"
_RECEIPT_WHAT: str = "the install receipt"
DISTRIBUTION_NAME: str = "bench-governance"
PROVIDERS: tuple[str, ...] = ("anthropic", "openrouter", "claude_code")
_GIT_TIMEOUT_SECONDS: float = 30.0
# Any of these already keeps the ledger directory out of git.
_IGNORE_EQUIVALENTS: frozenset[str] = frozenset(
    {"/.bench/", ".bench/", "/.bench", ".bench"}
)


class InstallError(Exception):
    """A step could not run. Nothing is retried; the message says what to fix."""


@dataclass(frozen=True)
class Resources:
    """The packaged files an install points at."""

    hook_script: Path
    guard: Path


@dataclass
class StepResult:
    """One line of the install or uninstall report.

    ``status`` is one of: written, updated, unchanged, set, removed, kept,
    skipped. ``skipped`` is a legitimate no-op (a project that is not a git
    repository has no hooks directory) and ``kept`` is a deliberate refusal
    to remove something the command cannot prove is its own.
    """

    step: str
    status: str
    detail: str = ""


@dataclass
class Report:
    steps: list[StepResult] = field(default_factory=list)

    def add(self, step: str, status: str, detail: str = "") -> None:
        self.steps.append(StepResult(step, status, detail))

    @property
    def ok(self) -> bool:
        """False when any step failed, which drives the CLI's exit code.

        Every failure in this module raises InstallError before a report is
        returned, so today no step records ``failed`` and every report that
        exists is ok. The property keeps the exit-code contract explicit in
        one place should a non-raising failure ever be added.
        """
        return all(step.status != "failed" for step in self.steps)

    def render(self) -> str:
        lines: list[str] = []
        for step in self.steps:
            line: str = f"  {step.step:<10} {step.status:<9}"
            lines.append(f"{line} {step.detail}".rstrip())
        return "\n".join(lines) + "\n"


def _hooks_package_dir() -> Path:
    """Directory holding the ``hooks`` package's files on disk."""
    return Path(str(importlib.resources.files(HOOK_PACKAGE)))


def locate_resources() -> Resources:
    """Find the packaged hook script and guard, and prove the core
    constitution loads. Raises InstallError naming whatever is missing.

    An install with a missing resource would register a hook that can only
    veto (the hook fails closed when the constitution cannot load), which is
    safe but useless, so the installer refuses up front instead.
    """
    package_dir: Path = _hooks_package_dir()
    hook: Path = package_dir / HOOK_SCRIPT_NAME
    guard: Path = package_dir / GUARD_NAME
    missing: list[str] = [str(p) for p in (hook, guard) if not p.is_file()]
    if missing:
        raise InstallError(
            f"Bench's installed files are incomplete: {', '.join(missing)} not "
            f"found. Reinstall Bench (pip install --force-reinstall) and retry."
        )
    try:
        load_constitution_snapshot()
    except ConstitutionError as exc:
        raise InstallError(
            f"Bench's core constitution does not load, so the hook could only "
            f"veto: {exc}. Reinstall Bench and retry."
        ) from exc
    return Resources(hook_script=hook.resolve(), guard=guard.resolve())


def hook_command(hook_script: Path, interpreter: Path) -> str:
    """The settings.json command line: quoted interpreter, quoted hook path.

    Both paths are absolute and use forward slashes, which every shell Claude
    Code runs hooks through accepts, Windows included. The interpreter is made
    absolute but its symlinks are NOT resolved: a POSIX virtual environment's
    ``bin/python`` is a link to the base interpreter, and following it would
    pin a Python that has none of Bench's dependencies.
    """
    return f'"{interpreter.absolute().as_posix()}" "{hook_script.resolve().as_posix()}"'


def is_bench_hook(hook: Any) -> bool:
    """True for a command hook whose command names Bench's hook script."""
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    command: Any = hook.get("command")
    return isinstance(command, str) and HOOK_SCRIPT_RELPATH in command.replace(
        "\\", "/"
    )


def _load_json_object(path: Path, what: str) -> dict[str, Any] | None:
    """The JSON object in ``path``, or None when the file does not exist."""
    if not path.exists():
        return None
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read {what} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InstallError(
            f"{what} {path} must hold a JSON object, not {type(data).__name__}"
        )
    return data


def _write_json(path: Path, data: dict[str, Any], what: str) -> None:
    """Write via a sibling temp file and rename, so a crash mid-write cannot
    leave a truncated file behind."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp: Path = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise InstallError(f"cannot write {what} {path}: {exc}") from exc


def _mapping(settings: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    value: Any = settings.setdefault(key, {})
    if not isinstance(value, dict):
        raise InstallError(
            f"{path}: '{key}' must be a JSON object, not {type(value).__name__}"
        )
    return value


def _pre_tool_use(settings: dict[str, Any], path: Path) -> list[Any]:
    hooks: dict[str, Any] = _mapping(settings, "hooks", path)
    pre: Any = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        raise InstallError(
            f"{path}: 'hooks.PreToolUse' must be a JSON array, "
            f"not {type(pre).__name__}"
        )
    return pre


def _bench_hooks(pre: list[Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every (entry, hook) pair in PreToolUse whose hook is Bench's."""
    return [
        (entry, hook)
        for entry in pre
        if isinstance(entry, dict) and isinstance(entry.get("hooks"), list)
        for hook in entry["hooks"]
        if is_bench_hook(hook)
    ]


def _register_hook(settings: dict[str, Any], command: str, path: Path) -> str:
    """Add or refresh the Bench PreToolUse hook. Returns the step status.

    One Bench hook in an entry matching every governed tool is refreshed in
    place. Anything else (none, several, or a hook inside an entry whose
    matcher is narrower than ``HOOK_MATCHER``, which would leave a tool
    ungoverned) is consolidated into one fresh entry. Sibling hooks and their
    entry's matcher are never touched: Bench's hook is moved out, not the
    user's hooks moved around.
    """
    pre: list[Any] = _pre_tool_use(settings, path)
    ours: list[tuple[dict[str, Any], dict[str, Any]]] = _bench_hooks(pre)
    if len(ours) == 1 and ours[0][0].get("matcher") == HOOK_MATCHER:
        _, hook = ours[0]
        if hook["command"] == command:
            return "unchanged"
        hook["command"] = command
        return "updated"
    for entry, hook in ours:
        entry["hooks"].remove(hook)
        if not entry["hooks"]:
            pre.remove(entry)
    pre.append(
        {"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": command}]}
    )
    return "updated" if ours else "written"


def _ensure_ignored(gitignore: Path) -> str:
    """Append ``/.bench/`` unless an equivalent line is already present."""
    existing: str = ""
    if gitignore.exists():
        try:
            existing = gitignore.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise InstallError(f"cannot read {gitignore}: {exc}") from exc
    if any(line.strip() in _IGNORE_EQUIVALENTS for line in existing.splitlines()):
        return "unchanged"
    separator: str = "" if not existing or existing.endswith("\n") else "\n"
    try:
        with gitignore.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{separator}{IGNORE_LINE}\n")
    except OSError as exc:
        raise InstallError(f"cannot write {gitignore}: {exc}") from exc
    return "written"


def _hooks_dir(project: Path) -> Path | None:
    """The project's git hooks directory, honouring ``core.hooksPath``, or
    None when the project is not a git repository.

    argv is constant and the project path travels as ``cwd``, so no
    user-supplied value is ever an argument to git.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=str(project),
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError(
            f"git did not answer within {_GIT_TIMEOUT_SECONDS:g}s in {project}"
        ) from exc
    except OSError as exc:
        raise InstallError(f"cannot run git: {exc}") from exc
    if proc.returncode != 0:
        return None
    # Relative to the cwd git ran in; joining an absolute answer is a no-op.
    return (project / proc.stdout.strip()).resolve()


def _locate_hooks_dir(project: Path) -> tuple[Path | None, str]:
    """The hooks directory install may write to, or (None, why not).

    A global ``core.hooksPath`` (or a worktree's shared hooks) resolves to a
    directory every repository on the machine consults. Writing there would
    put the guard in front of unrelated projects, so it is refused.
    """
    hooks_dir: Path | None = _hooks_dir(project)
    if hooks_dir is None:
        return None, f"{project} is not a git repository"
    if project not in hooks_dir.parents:
        return None, f"{hooks_dir} is outside the project; not touching it"
    return hooks_dir, ""


def _read_bytes(path: Path, what: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise InstallError(f"cannot read {what} {path}: {exc}") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _install_guard(project: Path, guard: Path) -> tuple[str, str, Path | None]:
    """Install the ledger commit guard as the project's pre-commit hook.

    Returns (status, detail, target); ``target`` is set only when the file
    at the hooks directory is Bench's guard, whether written now or already
    there. An existing pre-commit hook is never overwritten: the guard is one
    line of defence behind the ignore rule and the CI hygiene test, and
    replacing a project's own hook to add it would be a worse trade.
    """
    hooks_dir, why_not = _locate_hooks_dir(project)
    if hooks_dir is None:
        return "skipped", why_not, None
    target: Path = hooks_dir / GUARD_NAME
    source: bytes = _read_bytes(guard, "the commit guard")
    if target.exists():
        if _read_bytes(target, "the pre-commit hook") == source:
            return "unchanged", str(target), target
        return (
            "skipped",
            f"{target} exists and is not Bench's guard; call {guard.as_posix()} from it",
            None,
        )
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source)
        mode: int = target.stat().st_mode
        target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError as exc:
        raise InstallError(f"cannot write {target}: {exc}") from exc
    return "written", str(target), target


def _resolve_project(project: Path) -> Path:
    resolved: Path = project.expanduser().resolve()
    if not resolved.is_dir():
        raise InstallError(f"project directory does not exist: {resolved}")
    return resolved


def _bench_version() -> str:
    try:
        return importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        # A checkout that was never pip-installed has no distribution
        # metadata; the receipt still records everything uninstall needs.
        return "unknown"


def _str_mapping(value: Any) -> dict[str, str]:
    """A {str: str} copy of ``value`` when it is one, else empty."""
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(v, str)}


def _apply_provider(
    settings: dict[str, Any], provider: str, env_added: dict[str, str], path: Path
) -> str:
    """Set ``env.BENCH_PROVIDER``. Returns the step status.

    The key is recorded in ``env_added`` as install's own only when install
    added it. An explicit ``--provider`` that overrides a value the project
    set itself leaves ownership with the project, so uninstall keeps it.
    """
    env: dict[str, Any] = _mapping(settings, "env", path)
    current: Any = env.get("BENCH_PROVIDER")
    if current == provider:
        status: str = "unchanged"
    elif "BENCH_PROVIDER" not in env:
        status = "set"
        env_added["BENCH_PROVIDER"] = provider
    else:
        status = "updated"
        if "BENCH_PROVIDER" in env_added:
            env_added["BENCH_PROVIDER"] = provider
    env["BENCH_PROVIDER"] = provider
    return status


def _guard_receipt(
    guard_target: Path | None, guard_status: str, previous: dict[str, Any]
) -> dict[str, str] | None:
    """The receipt's guard record: a guard install wrote now, or one an
    earlier receipt already claimed. An identical file nobody claimed stays
    unclaimed, since ownership is recorded, never inferred from content."""
    if guard_target is None:
        return None
    if guard_status != "written" and not isinstance(previous.get("guard"), dict):
        return None
    return {
        "path": str(guard_target),
        "sha256": _sha256(_read_bytes(guard_target, "the commit guard")),
    }


def install(
    project: Path,
    resources: Resources | None = None,
    interpreter: Path | None = None,
    provider: str | None = None,
) -> Report:
    """Register Bench in ``project``. Idempotent: a second run reports every
    step unchanged and rewrites an equal receipt.

    ``provider`` sets ``env.BENCH_PROVIDER`` in the project's settings; left
    None, the environment is untouched and the pipeline's own default applies.
    The receipt records the key only when install added it, so a value the
    project already had stays the project's own.
    """
    found: Resources = resources if resources is not None else locate_resources()
    if provider is not None and provider not in PROVIDERS:
        raise InstallError(
            f"unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}"
        )
    target: Path = _resolve_project(project)
    python: Path = interpreter if interpreter is not None else Path(sys.executable)
    report: Report = Report()
    receipt_path: Path = target / RECEIPT_RELPATH
    previous: dict[str, Any] = _load_json_object(receipt_path, _RECEIPT_WHAT) or {}

    settings_path: Path = target / ".claude" / "settings.json"
    settings: dict[str, Any] = _load_json_object(settings_path, "settings") or {}
    before: str = json.dumps(settings, sort_keys=True)
    command: str = hook_command(found.hook_script, python)
    report.add("hook", _register_hook(settings, command, settings_path), command)

    env_added: dict[str, str] = _str_mapping(previous.get("env_added"))
    if provider is not None:
        status: str = _apply_provider(settings, provider, env_added, settings_path)
        report.add("provider", status, f"BENCH_PROVIDER={provider}")
    if json.dumps(settings, sort_keys=True) != before:
        _write_json(settings_path, settings, "settings")
        report.add("settings", "written", str(settings_path))
    else:
        report.add("settings", "unchanged", str(settings_path))

    ignore_status: str = _ensure_ignored(target / ".gitignore")
    report.add("gitignore", ignore_status, IGNORE_LINE)
    line_added: bool = ignore_status == "written" or bool(
        previous.get("gitignore_line_added")
    )

    guard_status, guard_detail, guard_target = _install_guard(target, found.guard)
    report.add("guard", guard_status, guard_detail)
    guard_record: dict[str, str] | None = _guard_receipt(
        guard_target, guard_status, previous
    )

    receipt: dict[str, Any] = {
        "bench_version": _bench_version(),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "hook": {"command": command, "matcher": HOOK_MATCHER},
        "env_added": env_added,
        "gitignore_line_added": line_added,
        "guard": guard_record,
    }
    _write_json(receipt_path, receipt, _RECEIPT_WHAT)
    report.add("receipt", "written", str(receipt_path))
    return report


def _require_human(
    environ: Mapping[str, str], stdin_isatty: Callable[[], bool]
) -> None:
    """Refuse to remove governance from inside an agent session.

    Installing governance is safe from anywhere. Removing it is the direction
    an agent must never take on its own, so uninstall applies the same gate
    as chain retirement: none of ``AGENT_ENV_MARKERS`` set, and a human at a
    TTY. ``CLAUDECODE`` is set for a command typed with a ``!`` prefix too,
    which is intentional: a shell hosted by an agent does not clear the bar.
    """
    tripped: list[str] = [name for name in AGENT_ENV_MARKERS if environ.get(name)]
    if tripped:
        raise InstallError(
            f"refusing to uninstall: {', '.join(tripped)} set in the "
            f"environment, so this is not a human at a plain terminal. Run "
            f"this from a terminal outside any agent session."
        )
    if not stdin_isatty():
        raise InstallError(
            "refusing to uninstall: stdin is not a TTY, so this is not a "
            "human at a terminal."
        )


def _remove_recorded_hook(settings: dict[str, Any], command: str) -> tuple[str, str]:
    """Drop the hook whose command the receipt recorded; keep any other."""
    hooks: Any = settings.get("hooks")
    pre: Any = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    if not isinstance(pre, list):
        return "unchanged", "no PreToolUse hooks"
    removed: int = 0
    kept: int = 0
    for entry, hook in _bench_hooks(pre):
        if hook.get("command") != command:
            kept += 1
            continue
        entry["hooks"].remove(hook)
        if not entry["hooks"]:
            pre.remove(entry)
        removed += 1
    if not pre:
        del hooks["PreToolUse"]
    if not hooks:
        del settings["hooks"]
    detail: str = command
    if kept:
        detail = f"{command} ({kept} other Bench hook(s) not written by this install, kept)"
    return ("removed" if removed else "unchanged"), detail


def _remove_added_env(settings: dict[str, Any], env_added: dict[str, str]) -> tuple[str, str]:
    """Drop the env keys install added, each only if it still holds the value
    install set. A value the project changed since is the project's now."""
    env: Any = settings.get("env")
    if not isinstance(env, dict) or not env_added:
        return "unchanged", "none added by install"
    removed: list[str] = []
    kept: list[str] = []
    for key, value in env_added.items():
        if key not in env:
            continue
        if env[key] == value:
            del env[key]
            removed.append(key)
        else:
            kept.append(key)
    if not env:
        del settings["env"]
    detail: str = ", ".join(removed) or "nothing left to remove"
    if kept:
        detail += f" (kept, changed since install: {', '.join(kept)})"
    return ("removed" if removed else "unchanged"), detail


def _chain_present(bench_dir: Path, receipt_path: Path) -> bool:
    """True when ``.bench/`` holds anything besides the receipt."""
    if not bench_dir.is_dir():
        return False
    return any(child != receipt_path for child in bench_dir.iterdir())


def _remove_ignore_line(project: Path, added: bool, receipt_path: Path) -> tuple[str, str]:
    gitignore: Path = project / ".gitignore"
    if not added:
        return "kept", f"{IGNORE_LINE} was not added by install"
    if _chain_present(project / ".bench", receipt_path):
        return "kept", f"{project / '.bench'} holds a chain that must stay out of git"
    if not gitignore.exists():
        return "unchanged", IGNORE_LINE
    try:
        lines: list[str] = gitignore.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError(f"cannot read {gitignore}: {exc}") from exc
    remaining: list[str] = [line for line in lines if line.strip() != IGNORE_LINE]
    if len(remaining) == len(lines):
        return "unchanged", IGNORE_LINE
    try:
        if remaining:
            gitignore.write_text("\n".join(remaining) + "\n", encoding="utf-8", newline="\n")
        else:
            gitignore.unlink()
    except OSError as exc:
        raise InstallError(f"cannot write {gitignore}: {exc}") from exc
    return "removed", IGNORE_LINE


def _remove_recorded_guard(project: Path, record: Any) -> tuple[str, str]:
    """Remove the guard only at the recorded path and only with the recorded
    digest, so a hook the project edited since is kept."""
    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
        return "unchanged", "none recorded by install"
    target: Path = Path(record["path"])
    if project not in target.parents:
        return "kept", f"{target} is outside the project"
    if not target.exists():
        return "unchanged", str(target)
    if _sha256(_read_bytes(target, "the pre-commit hook")) != record.get("sha256"):
        return "kept", f"{target} changed since install"
    try:
        target.unlink()
    except OSError as exc:
        raise InstallError(f"cannot remove {target}: {exc}") from exc
    return "removed", str(target)


def uninstall(
    project: Path,
    environ: Mapping[str, str] | None = None,
    stdin_isatty: Callable[[], bool] | None = None,
) -> Report:
    """Reverse ``install`` from its receipt, removing only what the receipt
    names and only if unchanged since. With no receipt nothing is removed.

    ``environ`` and ``stdin_isatty`` default to the live process values; the
    CLI passes them explicitly so the gate it applies is visibly the real one.
    """
    _require_human(
        environ if environ is not None else os.environ,
        stdin_isatty if stdin_isatty is not None else sys.stdin.isatty,
    )
    target: Path = _resolve_project(project)
    receipt_path: Path = target / RECEIPT_RELPATH
    receipt: dict[str, Any] | None = _load_json_object(receipt_path, _RECEIPT_WHAT)
    if receipt is None:
        raise InstallError(
            f"no install receipt at {receipt_path}; nothing removed. Bench was "
            f"registered here by hand or by an older install: remove the hook "
            f"entry, env keys, ignore line, and guard yourself."
        )
    report: Report = Report()

    hook_record: Any = receipt.get("hook")
    command: str = (
        str(hook_record.get("command", "")) if isinstance(hook_record, dict) else ""
    )
    settings_path: Path = target / ".claude" / "settings.json"
    settings: dict[str, Any] = _load_json_object(settings_path, "settings") or {}
    before: str = json.dumps(settings, sort_keys=True)
    hook_status, hook_detail = _remove_recorded_hook(settings, command)
    report.add("hook", hook_status, hook_detail)
    env_status, env_detail = _remove_added_env(settings, _str_mapping(receipt.get("env_added")))
    report.add("env", env_status, env_detail)
    if json.dumps(settings, sort_keys=True) == before:
        report.add("settings", "unchanged", str(settings_path))
    elif settings:
        _write_json(settings_path, settings, "settings")
        report.add("settings", "written", str(settings_path))
    else:
        try:
            settings_path.unlink()
            if not any(settings_path.parent.iterdir()):
                settings_path.parent.rmdir()
        except OSError as exc:
            raise InstallError(f"cannot remove {settings_path}: {exc}") from exc
        report.add("settings", "removed", str(settings_path))

    ignore_status, ignore_detail = _remove_ignore_line(
        target, bool(receipt.get("gitignore_line_added")), receipt_path
    )
    report.add("gitignore", ignore_status, ignore_detail)

    guard_status, guard_detail = _remove_recorded_guard(target, receipt.get("guard"))
    report.add("guard", guard_status, guard_detail)

    try:
        receipt_path.unlink()
        if not any(receipt_path.parent.iterdir()):
            receipt_path.parent.rmdir()
    except OSError as exc:
        raise InstallError(f"cannot remove {receipt_path}: {exc}") from exc
    report.add("receipt", "removed", str(receipt_path))
    return report
