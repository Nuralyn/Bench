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
import shutil
import stat
import subprocess
import sys
import tempfile
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
_GITIGNORE_NAME: str = ".gitignore"
_CLAUDE_DIRNAME: str = ".claude"
_SETTINGS_NAME: str = "settings.json"
_BENCH_DIRNAME: str = ".bench"
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


def _shell_quote(text: str) -> str:
    """Double-quote ``text`` for a POSIX shell, escaping the characters that
    stay live inside double quotes (backslash, double quote, dollar, backtick)
    so a path containing them is a path, not an expansion. Double quotes are
    kept rather than shlex's single quotes so the command reads the same on
    every shell Claude Code runs hooks through."""
    escaped: str = text
    for char in ("\\", '"', "$", "`"):
        escaped = escaped.replace(char, "\\" + char)
    return f'"{escaped}"'


def hook_command(hook_script: Path, interpreter: Path) -> str:
    """The settings.json command line: quoted interpreter, quoted hook path.

    Both paths are absolute and use forward slashes, which every shell Claude
    Code runs hooks through accepts, Windows included. The interpreter is made
    absolute but its symlinks are NOT resolved: a POSIX virtual environment's
    ``bin/python`` is a link to the base interpreter, and following it would
    pin a Python that has none of Bench's dependencies.
    """
    return (
        f"{_shell_quote(interpreter.absolute().as_posix())} "
        f"{_shell_quote(hook_script.resolve().as_posix())}"
    )


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


def _refuse_symlink(path: Path, what: str) -> None:
    """Refuse to write through a symlink. A governed project is untrusted
    input, and a link at any output path would redirect the write outside
    the project the user named."""
    if path.is_symlink():
        raise InstallError(f"{what} {path} is a symlink; refusing to write through it")


def _write_json(path: Path, data: dict[str, Any], what: str) -> None:
    """Write via an exclusively created sibling temp file and rename.

    A crash mid-write cannot leave a truncated file, and because the temp
    name is random and created with O_EXCL, a link planted at a predictable
    name is never followed. Neither the file nor its directory may be a
    symlink. A file install creates is owner-only; one it rewrites keeps its
    mode, so a 0600 settings file does not come back 0644.
    """
    _refuse_symlink(path.parent, f"the directory holding {what}")
    _refuse_symlink(path, what)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        tmp: Path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, indent=2) + "\n")
            if path.exists():
                shutil.copymode(path, tmp)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
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


def _refuse_symlinked_gitignore(gitignore: Path) -> None:
    """A symlinked .gitignore is refused: git (2.32 and later) does not read
    one, so an ignore line there would protect nothing, and appending would
    write through the link to a file outside the project."""
    if gitignore.is_symlink():
        raise InstallError(
            f"{gitignore} is a symlink; git does not read a symlinked "
            f".gitignore, so {IGNORE_LINE} there would not keep the chain out "
            f"of git. Replace it with a regular file and retry."
        )


def _ignore_plan(gitignore: Path) -> tuple[str, str | None, bool]:
    """Decide the .gitignore step without writing.

    Returns (status, the exact text to append or None, whether the file will
    be created). The appended text follows the file's own line endings and
    supplies a separator only when the file lacks a final newline, so
    uninstall can remove precisely those bytes and leave the rest untouched.
    """
    _refuse_symlinked_gitignore(gitignore)
    if gitignore.is_dir():
        raise InstallError(f"{gitignore} is a directory, not an ignore file")
    created: bool = not gitignore.exists()
    existing: str = ""
    if not created:
        try:
            existing = gitignore.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise InstallError(f"cannot read {gitignore}: {exc}") from exc
    if any(line.strip() in _IGNORE_EQUIVALENTS for line in existing.splitlines()):
        return "unchanged", None, False
    newline: str = "\r\n" if "\r\n" in existing else "\n"
    separator: str = "" if not existing or existing.endswith("\n") else newline
    return "written", f"{separator}{IGNORE_LINE}{newline}", created


def _append_text(path: Path, text: str) -> None:
    try:
        with path.open("ab") as handle:
            handle.write(text.encode("utf-8"))
    except OSError as exc:
        raise InstallError(f"cannot write {path}: {exc}") from exc


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


def _guard_plan(project: Path, guard: Path, source: bytes) -> tuple[str, str, Path | None]:
    """Decide the commit-guard step without writing.

    Returns (status, detail, target); ``target`` is set only when the file
    at the hooks directory is or will be Bench's guard, and status "written"
    means ``_write_guard`` must run. An existing pre-commit hook is never
    overwritten: the guard is one line of defence behind the ignore rule and
    the CI hygiene test, and replacing a project's own hook to add it would
    be a worse trade.
    """
    hooks_dir, why_not = _locate_hooks_dir(project)
    if hooks_dir is None:
        return "skipped", why_not, None
    target: Path = hooks_dir / GUARD_NAME
    if target.is_symlink():
        # Checked before exists(): a dangling link reads as absent, and a
        # write would then land wherever the link points.
        return "skipped", f"{target} is a symlink; not writing through it", None
    if target.exists():
        if _read_bytes(target, "the pre-commit hook") == source:
            return "unchanged", str(target), target
        return (
            "skipped",
            f"{target} exists and is not Bench's guard; call {guard.as_posix()} from it",
            None,
        )
    return "written", str(target), target


def _write_guard(target: Path, source: bytes) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source)
        mode: int = target.stat().st_mode
        target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError as exc:
        raise InstallError(f"cannot write {target}: {exc}") from exc


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
    guard_target: Path | None,
    guard_status: str,
    previous: dict[str, Any],
    project: Path,
    source: bytes,
) -> dict[str, str] | None:
    """The receipt's guard record: a guard install wrote now, or one an
    earlier receipt already claimed. An identical file nobody claimed stays
    unclaimed, since ownership is recorded, never inferred from content.

    The path is recorded relative to the project, so a repository that is
    moved or renamed before ``bench uninstall`` still finds its guard.
    """
    if guard_target is None:
        return None
    if guard_status != "written" and not isinstance(previous.get("guard"), dict):
        return None
    # The digest is of the bytes install writes (or found identical), so the
    # record is correct even though the receipt is written before the guard.
    return {
        "path": guard_target.relative_to(project).as_posix(),
        "sha256": _sha256(source),
    }


def _carry(previous: dict[str, Any], key: str, now: bool) -> bool:
    """A creation flag stays true once any install run recorded it."""
    return now or bool(previous.get(key))


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
    # Checked before anything is written, so a refusal leaves no half-install.
    _refuse_symlinked_gitignore(target / _GITIGNORE_NAME)
    _refuse_symlink(target / _CLAUDE_DIRNAME, "the settings directory")
    _refuse_symlink(target / _BENCH_DIRNAME, "the ledger directory")
    python: Path = interpreter if interpreter is not None else Path(sys.executable)
    report: Report = Report()
    receipt_path: Path = target / RECEIPT_RELPATH
    previous: dict[str, Any] = _load_json_object(receipt_path, _RECEIPT_WHAT) or {}

    # Plan every step in memory first, write the receipt, then apply. A
    # failure after the receipt leaves a record that reverses whatever did
    # land; a failure before it leaves nothing written at all.
    settings_path: Path = target / _CLAUDE_DIRNAME / _SETTINGS_NAME
    settings_existed: bool = settings_path.exists()
    claude_dir_existed: bool = settings_path.parent.exists()
    settings: dict[str, Any] = _load_json_object(settings_path, "settings") or {}
    before: str = json.dumps(settings, sort_keys=True)
    command: str = hook_command(found.hook_script, python)
    hook_status: str = _register_hook(settings, command, settings_path)
    # Ownership is recorded, never inferred: a hook that already matched the
    # generated command (a project wired by hand) is not claimed, so a later
    # uninstall leaves it. A hook install wrote or rewrote is its own.
    hook_record: dict[str, str] | None = None
    if hook_status != "unchanged" or isinstance(previous.get("hook"), dict):
        hook_record = {"command": command, "matcher": HOOK_MATCHER}
    env_added: dict[str, str] = _str_mapping(previous.get("env_added"))
    provider_status: str | None = None
    if provider is not None:
        provider_status = _apply_provider(settings, provider, env_added, settings_path)
    settings_changed: bool = json.dumps(settings, sort_keys=True) != before

    gitignore: Path = target / _GITIGNORE_NAME
    ignore_status, ignore_text, ignore_created = _ignore_plan(gitignore)
    guard_source: bytes = _read_bytes(found.guard, "the commit guard")
    guard_status, guard_detail, guard_target = _guard_plan(target, found.guard, guard_source)

    previous_appended: Any = previous.get("gitignore_appended")
    receipt: dict[str, Any] = {
        "bench_version": _bench_version(),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "hook": hook_record,
        "env_added": env_added,
        "settings_created": _carry(previous, "settings_created", not settings_existed),
        "claude_dir_created": _carry(previous, "claude_dir_created", not claude_dir_existed),
        "gitignore_created": _carry(previous, "gitignore_created", ignore_created),
        "gitignore_appended": (
            ignore_text
            if ignore_text is not None
            else (previous_appended if isinstance(previous_appended, str) else None)
        ),
        "guard": _guard_receipt(guard_target, guard_status, previous, target, guard_source),
    }
    _write_json(receipt_path, receipt, _RECEIPT_WHAT)

    report.add("hook", hook_status, command)
    if provider_status is not None:
        report.add("provider", provider_status, f"BENCH_PROVIDER={provider}")
    if settings_changed:
        _write_json(settings_path, settings, "settings")
    report.add("settings", "written" if settings_changed else "unchanged", str(settings_path))
    if ignore_text is not None:
        _append_text(gitignore, ignore_text)
    report.add("gitignore", ignore_status, IGNORE_LINE)
    if guard_status == "written" and guard_target is not None:
        _write_guard(guard_target, guard_source)
    report.add("guard", guard_status, guard_detail)
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


def _remove_ignore_suffix(
    project: Path, receipt: dict[str, Any], receipt_path: Path
) -> tuple[str, str]:
    """Remove exactly the bytes install appended to .gitignore, and nothing
    else: no re-wrapping of lines, no line-ending changes. The file is
    removed only if install created it and nothing else was added since."""
    gitignore: Path = project / _GITIGNORE_NAME
    appended: Any = receipt.get("gitignore_appended")
    if not isinstance(appended, str) or not appended:
        return "kept", f"{IGNORE_LINE} was not added by install"
    if _chain_present(project / _BENCH_DIRNAME, receipt_path):
        return "kept", f"{project / _BENCH_DIRNAME} holds a chain that must stay out of git"
    if gitignore.is_symlink():
        return "kept", f"{gitignore} is a symlink; not writing through it"
    if not gitignore.exists():
        return "unchanged", IGNORE_LINE
    suffix: bytes = appended.encode("utf-8")
    try:
        data: bytes = gitignore.read_bytes()
        if not data.endswith(suffix):
            return "kept", f"{gitignore} changed since install"
        remaining: bytes = data[: -len(suffix)]
        if not remaining and receipt.get("gitignore_created"):
            gitignore.unlink()
        else:
            gitignore.write_bytes(remaining)
    except OSError as exc:
        raise InstallError(f"cannot write {gitignore}: {exc}") from exc
    return "removed", IGNORE_LINE


def _finish_settings(
    path: Path, settings: dict[str, Any], changed: bool, receipt: dict[str, Any]
) -> tuple[str, str]:
    """Write the settings back, or remove the file only if install created
    it. A file that predates install stays, even as an empty object, and its
    directory is removed only if install created that too."""
    if not changed:
        return "unchanged", str(path)
    if settings or not receipt.get("settings_created"):
        _write_json(path, settings, "settings")
        return "written", str(path)
    try:
        path.unlink()
        if receipt.get("claude_dir_created") and not any(path.parent.iterdir()):
            path.parent.rmdir()
    except OSError as exc:
        raise InstallError(f"cannot remove {path}: {exc}") from exc
    return "removed", str(path)


def _remove_recorded_guard(project: Path, record: Any) -> tuple[str, str]:
    """Remove the guard only at the recorded path and only with the recorded
    digest, so a hook the project edited since is kept."""
    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
        return "unchanged", "none recorded by install"
    # Recorded relative to the project; resolving after the join means a
    # record that climbs out with ".." fails the containment check below.
    target: Path = (project / record["path"]).resolve()
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
    # The same refusals install applies, before anything is read or removed:
    # a directory or file swapped for a symlink after install would otherwise
    # have the recorded removals land in whatever it points at.
    _refuse_symlink(target / _CLAUDE_DIRNAME, "the settings directory")
    _refuse_symlink(target / _CLAUDE_DIRNAME / _SETTINGS_NAME, "settings")
    _refuse_symlink(target / _BENCH_DIRNAME, "the ledger directory")
    receipt_path: Path = target / RECEIPT_RELPATH
    _refuse_symlink(receipt_path, _RECEIPT_WHAT)
    receipt: dict[str, Any] | None = _load_json_object(receipt_path, _RECEIPT_WHAT)
    if receipt is None:
        raise InstallError(
            f"no install receipt at {receipt_path}; nothing removed. Bench was "
            f"registered here by hand or by an older install: remove the hook "
            f"entry, env keys, ignore line, and guard yourself."
        )
    report: Report = Report()

    settings_path: Path = target / _CLAUDE_DIRNAME / _SETTINGS_NAME
    settings: dict[str, Any] = _load_json_object(settings_path, "settings") or {}
    before: str = json.dumps(settings, sort_keys=True)
    hook_record: Any = receipt.get("hook")
    if isinstance(hook_record, dict):
        hook_status, hook_detail = _remove_recorded_hook(
            settings, str(hook_record.get("command", ""))
        )
    else:
        hook_status, hook_detail = (
            "kept",
            "the hook was present before install and was not written by it",
        )
    report.add("hook", hook_status, hook_detail)
    env_status, env_detail = _remove_added_env(settings, _str_mapping(receipt.get("env_added")))
    report.add("env", env_status, env_detail)
    settings_status, settings_detail = _finish_settings(
        settings_path, settings, json.dumps(settings, sort_keys=True) != before, receipt
    )
    report.add("settings", settings_status, settings_detail)

    ignore_status, ignore_detail = _remove_ignore_suffix(target, receipt, receipt_path)
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
