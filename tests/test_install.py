"""Tests for cli.install: ``bench install`` and ``bench uninstall``.

Every test runs against a scratch project in a temp directory with scratch
resources (a fake hook script and guard), so nothing here touches the real
.claude/settings.json, the real .gitignore, or the real git hooks of this
repository. Git-backed tests pin GIT_CONFIG_GLOBAL to an empty file and set
GIT_CONFIG_NOSYSTEM, so a developer's global core.hooksPath cannot redirect a
write.

Run: python -m unittest tests.test_install -v
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cli import __main__ as cli_main  # noqa: E402
from cli import install as install_module  # noqa: E402
from cli.commands import cmd_install, cmd_uninstall  # noqa: E402
from cli.install import (  # noqa: E402
    HOOK_MATCHER,
    HOOK_SCRIPT_RELPATH,
    IGNORE_LINE,
    RECEIPT_RELPATH,
    InstallError,
    Report,
    Resources,
    hook_command,
    install,
    is_bench_hook,
    locate_resources,
    uninstall,
)

_GUARD_BYTES: bytes = b"#!/bin/sh\n# scratch guard\nexit 0\n"
_HAVE_GIT: bool = shutil.which("git") is not None


def _human() -> bool:
    return True


def _statuses(report: Report) -> dict[str, str]:
    return {step.step: step.status for step in report.steps}


def _step(report: Report, name: str) -> tuple[str, str]:
    found = next(s for s in report.steps if s.step == name)
    return found.status, found.detail


class _ScratchCase(unittest.TestCase):
    """Scratch resources, a project directory, and an isolated git config."""

    def setUp(self) -> None:
        self.tmp: Path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        package: Path = self.tmp / "site" / "hooks"
        package.mkdir(parents=True)
        (package / "pre-tool-use.py").write_text("# hook\n", encoding="utf-8")
        (package / "pre-commit").write_bytes(_GUARD_BYTES)
        self.resources: Resources = Resources(
            hook_script=package / "pre-tool-use.py", guard=package / "pre-commit"
        )
        self.project: Path = self.tmp / "project"
        self.project.mkdir()
        self.interpreter: Path = self.tmp / "venv" / "bin" / "python"
        empty_config: Path = self.tmp / "gitconfig"
        empty_config.write_text("", encoding="utf-8")
        env_patch = patch.dict(
            os.environ,
            {"GIT_CONFIG_GLOBAL": str(empty_config), "GIT_CONFIG_NOSYSTEM": "1"},
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(self.project),
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=True,
        )

    def _init_repo(self) -> None:
        if not _HAVE_GIT:
            self.skipTest("git is not installed")
        self._git("init", "-q")

    def _install(self, provider: str | None = None) -> Report:
        return install(
            self.project,
            resources=self.resources,
            interpreter=self.interpreter,
            provider=provider,
        )

    def _uninstall(self) -> Report:
        return uninstall(self.project, environ={}, stdin_isatty=_human)

    @property
    def command(self) -> str:
        return hook_command(self.resources.hook_script, self.interpreter)

    @property
    def settings_path(self) -> Path:
        return self.project / ".claude" / "settings.json"

    def _settings(self) -> dict:
        return json.loads(self.settings_path.read_text(encoding="utf-8"))

    def _write_settings(self, data: dict) -> None:
        self.settings_path.parent.mkdir(exist_ok=True)
        self.settings_path.write_text(json.dumps(data), encoding="utf-8")

    @property
    def receipt_path(self) -> Path:
        return self.project / RECEIPT_RELPATH

    def _receipt(self) -> dict:
        return json.loads(self.receipt_path.read_text(encoding="utf-8"))

    @property
    def guard_path(self) -> Path:
        return self.project / ".git" / "hooks" / "pre-commit"


class TestLocateResources(_ScratchCase):
    def test_real_install_has_every_resource_and_a_loadable_constitution(self) -> None:
        found: Resources = locate_resources()
        self.assertTrue(found.hook_script.is_file())
        self.assertTrue(found.guard.is_file())
        self.assertEqual(found.hook_script.name, "pre-tool-use.py")
        self.assertEqual(found.guard.read_bytes(), (_REPO_ROOT / "hooks" / "pre-commit").read_bytes())

    def test_missing_packaged_file_is_refused_with_the_fix(self) -> None:
        (self.tmp / "site" / "hooks" / "pre-commit").unlink()
        with patch.object(install_module, "_hooks_package_dir", return_value=self.tmp / "site" / "hooks"):
            with self.assertRaises(InstallError) as ctx:
                locate_resources()
        self.assertIn("pre-commit", str(ctx.exception))
        self.assertIn("Reinstall", str(ctx.exception))

    def test_unloadable_constitution_is_refused_before_any_write(self) -> None:
        with patch.object(install_module, "_hooks_package_dir", return_value=self.tmp / "site" / "hooks"):
            with patch.object(
                install_module,
                "load_constitution_snapshot",
                side_effect=install_module.ConstitutionError("no constitution"),
            ):
                with self.assertRaises(InstallError) as ctx:
                    install(self.project, interpreter=self.interpreter)
        self.assertIn("could only veto", str(ctx.exception))
        self.assertFalse(self.settings_path.exists())
        self.assertFalse((self.project / ".gitignore").exists())


class TestHookCommand(_ScratchCase):
    def test_pins_interpreter_and_hook_script_quoted(self) -> None:
        self.assertEqual(
            self.command,
            f'"{self.interpreter.absolute().as_posix()}" '
            f'"{self.resources.hook_script.resolve().as_posix()}"',
        )
        self.assertNotIn("\\", self.command)

    def test_a_symlinked_venv_interpreter_is_kept_not_followed(self) -> None:
        # A POSIX virtual environment's bin/python links to the base Python.
        # Following the link would pin an interpreter without Bench's
        # dependencies, so the command must keep the venv path.
        real: Path = self.tmp / "base" / "python3"
        real.parent.mkdir()
        real.write_text("", encoding="utf-8")
        link: Path = self.tmp / "venv" / "bin" / "python"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable here: {exc}")
        command: str = hook_command(self.resources.hook_script, link)
        self.assertIn(link.absolute().as_posix(), command)
        self.assertNotIn(real.as_posix(), command)

    def test_shell_metacharacters_in_paths_are_escaped(self) -> None:
        # Claude Code hands the command to a shell. A path holding $, a
        # backtick, a double quote, or a backslash must survive as a path.
        odd_python: Path = self.tmp / 'venv $HOME `id` "q" back\\slash' / "bin" / "python"
        odd_hook: Path = self.tmp / "site $x" / "hooks" / "pre-tool-use.py"
        command: str = hook_command(odd_hook, odd_python)
        self.assertIn("\\$HOME", command)
        self.assertIn("\\`id\\`", command)
        self.assertIn('\\"q\\"', command)
        self.assertIn("\\$x", command)
        sh: str | None = shutil.which("sh")
        if sh is None:
            self.skipTest("no POSIX sh available to round-trip the command")
        proc = subprocess.run(
            [sh, "-c", 'printf "%s\\n" ' + command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.splitlines(),
            [odd_python.absolute().as_posix(), odd_hook.resolve().as_posix()],
        )

    def test_template_has_the_same_shape(self) -> None:
        template: dict = json.loads(
            (_REPO_ROOT / ".claude" / "settings.template.json").read_text(encoding="utf-8")
        )
        entry: dict = template["hooks"]["PreToolUse"][0]
        self.assertEqual(entry["matcher"], HOOK_MATCHER)
        hook: dict = entry["hooks"][0]
        self.assertTrue(is_bench_hook(hook))
        substituted: str = hook["command"].replace(
            "/absolute/path/to/python", self.interpreter.absolute().as_posix()
        ).replace(
            "/absolute/path/to/bench/hooks/pre-tool-use.py",
            self.resources.hook_script.resolve().as_posix(),
        )
        self.assertEqual(substituted, self.command)

    def test_is_bench_hook_matches_checkout_and_wheel_paths(self) -> None:
        self.assertTrue(
            is_bench_hook({"type": "command", "command": 'python "C:\\bench\\hooks\\pre-tool-use.py"'})
        )
        self.assertTrue(
            is_bench_hook(
                {"type": "command", "command": '"/v/bin/python" "/v/lib/python3.12/site-packages/hooks/pre-tool-use.py"'}
            )
        )
        self.assertFalse(is_bench_hook({"type": "command", "command": "ruff check"}))
        self.assertFalse(is_bench_hook({"type": "prompt", "command": HOOK_SCRIPT_RELPATH}))
        self.assertFalse(is_bench_hook("not a hook"))


class TestInstall(_ScratchCase):
    def test_fresh_project_gets_hook_ignore_guard_and_receipt(self) -> None:
        self._init_repo()
        report: Report = self._install()
        self.assertEqual(
            _statuses(report),
            {
                "hook": "written",
                "settings": "written",
                "gitignore": "written",
                "guard": "written",
                "receipt": "written",
            },
        )
        settings: dict = self._settings()
        entry: dict = settings["hooks"]["PreToolUse"][0]
        self.assertEqual(entry["matcher"], HOOK_MATCHER)
        self.assertEqual(entry["hooks"][0]["command"], self.command)
        self.assertNotIn("env", settings)
        self.assertEqual((self.project / ".gitignore").read_text(encoding="utf-8"), f"{IGNORE_LINE}\n")
        self.assertEqual(self.guard_path.read_bytes(), _GUARD_BYTES)
        if os.name != "nt":
            self.assertTrue(os.access(self.guard_path, os.X_OK))
        receipt: dict = self._receipt()
        self.assertEqual(receipt["hook"], {"command": self.command, "matcher": HOOK_MATCHER})
        self.assertEqual(receipt["env_added"], {})
        self.assertTrue(receipt["settings_created"])
        self.assertTrue(receipt["claude_dir_created"])
        self.assertTrue(receipt["gitignore_created"])
        self.assertEqual(receipt["gitignore_appended"], f"{IGNORE_LINE}\n")
        # Relative to the project, so a moved repository still finds it.
        self.assertEqual(
            receipt["guard"],
            {"path": ".git/hooks/pre-commit", "sha256": hashlib.sha256(_GUARD_BYTES).hexdigest()},
        )

    def test_second_run_changes_nothing_but_the_receipt_timestamp(self) -> None:
        self._init_repo()
        first: dict = dict(self._install() and self._receipt())
        settings_before: bytes = self.settings_path.read_bytes()
        report: Report = self._install()
        statuses: dict[str, str] = _statuses(report)
        self.assertEqual(statuses.pop("receipt"), "written")
        self.assertEqual(set(statuses.values()), {"unchanged"})
        self.assertEqual(self.settings_path.read_bytes(), settings_before)
        second: dict = self._receipt()
        first.pop("installed_at")
        second.pop("installed_at")
        self.assertEqual(first, second)

    def test_merges_into_existing_settings_and_refreshes_a_stale_hook(self) -> None:
        self._write_settings(
            {
                "$schema": "https://json.schemastore.org/claude-code-settings.json",
                "permissions": {"allow": ["Bash(ls)"]},
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi", "timeout": 30}]},
                        {
                            "matcher": HOOK_MATCHER,
                            "hooks": [{"type": "command", "command": 'python "/old/bench/hooks/pre-tool-use.py"'}],
                        },
                    ],
                    "PostToolUse": [],
                },
            }
        )
        report: Report = self._install()
        self.assertEqual(_statuses(report)["hook"], "updated")
        settings: dict = self._settings()
        self.assertEqual(settings["$schema"], "https://json.schemastore.org/claude-code-settings.json")
        self.assertEqual(settings["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(settings["hooks"]["PostToolUse"], [])
        pre: list = settings["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 2)
        self.assertEqual(pre[0]["hooks"][0], {"type": "command", "command": "echo hi", "timeout": 30})
        self.assertEqual(pre[1]["hooks"][0]["command"], self.command)

    def test_a_narrowed_matcher_moves_the_hook_out_and_leaves_siblings(self) -> None:
        # Bench's hook inside a user's "Write|Edit" entry would leave MultiEdit
        # ungoverned. It is moved into its own full-matcher entry; the user's
        # entry keeps its matcher and its other hook.
        self._write_settings(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Write|Edit",
                            "hooks": [
                                {"type": "command", "command": "prettier --check"},
                                {"type": "command", "command": 'python "/old/hooks/pre-tool-use.py"'},
                            ],
                        }
                    ]
                }
            }
        )
        report: Report = self._install()
        self.assertEqual(_statuses(report)["hook"], "updated")
        pre: list = self._settings()["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 2)
        self.assertEqual(pre[0]["matcher"], "Write|Edit")
        self.assertEqual(pre[0]["hooks"], [{"type": "command", "command": "prettier --check"}])
        self.assertEqual(pre[1], {"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": self.command}]})

    def test_a_pre_existing_identical_hook_is_not_claimed(self) -> None:
        # A project wired by hand to the exact command install would write:
        # install changes nothing, so the receipt must not claim the hook and
        # uninstall must leave it in place.
        self._write_settings(
            {"hooks": {"PreToolUse": [{"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": self.command}]}]}}
        )
        report: Report = self._install()
        self.assertEqual(_statuses(report)["hook"], "unchanged")
        self.assertIsNone(self._receipt()["hook"])
        removal: Report = self._uninstall()
        self.assertEqual(_statuses(removal)["hook"], "kept")
        self.assertEqual(self._settings()["hooks"]["PreToolUse"][0]["hooks"][0]["command"], self.command)

    def test_a_planted_temp_symlink_is_not_followed(self) -> None:
        victim: Path = self.tmp / "victim.txt"
        victim.write_text("keep me", encoding="utf-8")
        self.settings_path.parent.mkdir()
        try:
            (self.settings_path.parent / "settings.json.tmp").symlink_to(victim)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable here: {exc}")
        self._install()
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep me")
        self.assertTrue(self.settings_path.is_file())
        self.assertFalse(self.settings_path.is_symlink())

    def test_a_symlinked_settings_directory_is_refused_before_any_write(self) -> None:
        outside: Path = self.tmp / "outside"
        outside.mkdir()
        try:
            (self.project / ".claude").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable here: {exc}")
        with self.assertRaises(InstallError) as ctx:
            self._install()
        self.assertIn("symlink", str(ctx.exception))
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.project / ".gitignore").exists())

    def test_several_bench_hooks_are_consolidated_into_one(self) -> None:
        self._write_settings(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": 'python "/a/hooks/pre-tool-use.py"'}]},
                        {"matcher": "Write", "hooks": [{"type": "command", "command": 'python "/b/hooks/pre-tool-use.py"'}]},
                    ]
                }
            }
        )
        self._install()
        pre: list = self._settings()["hooks"]["PreToolUse"]
        self.assertEqual(pre, [{"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": self.command}]}])

    def test_provider_is_recorded_only_when_install_added_it(self) -> None:
        self._write_settings({"env": {"BENCH_PROVIDER": "anthropic", "BENCH_CLAUDE_TIMEOUT": "300"}})
        report: Report = self._install(provider="claude_code")
        self.assertEqual(_statuses(report)["provider"], "updated")
        self.assertEqual(self._settings()["env"]["BENCH_PROVIDER"], "claude_code")
        self.assertEqual(self._receipt()["env_added"], {})
        again: Report = self._install(provider="claude_code")
        self.assertEqual(_statuses(again)["provider"], "unchanged")

    def test_provider_added_by_install_is_recorded(self) -> None:
        report: Report = self._install(provider="claude_code")
        self.assertEqual(_statuses(report)["provider"], "set")
        self.assertEqual(self._receipt()["env_added"], {"BENCH_PROVIDER": "claude_code"})
        with self.assertRaises(InstallError):
            self._install(provider="bedrock")

    def test_non_git_project_skips_the_guard_only(self) -> None:
        report: Report = self._install()
        statuses: dict[str, str] = _statuses(report)
        self.assertEqual(statuses["guard"], "skipped")
        self.assertEqual(statuses["hook"], "written")
        self.assertTrue(self.settings_path.exists())
        self.assertTrue((self.project / ".gitignore").exists())
        self.assertIsNone(self._receipt()["guard"])

    def test_existing_foreign_pre_commit_hook_is_left_alone_and_not_claimed(self) -> None:
        self._init_repo()
        self.guard_path.parent.mkdir(parents=True, exist_ok=True)
        self.guard_path.write_bytes(b"#!/bin/sh\nnpm test\n")
        report: Report = self._install()
        status, detail = _step(report, "guard")
        self.assertEqual(status, "skipped")
        self.assertIn(self.resources.guard.as_posix(), detail)
        self.assertEqual(self.guard_path.read_bytes(), b"#!/bin/sh\nnpm test\n")
        self.assertIsNone(self._receipt()["guard"])

    def test_a_dangling_symlink_at_the_guard_target_is_not_written_through(self) -> None:
        self._init_repo()
        self.guard_path.parent.mkdir(parents=True, exist_ok=True)
        destination: Path = self.tmp / "elsewhere" / "pre-commit"
        destination.parent.mkdir()
        try:
            self.guard_path.symlink_to(destination)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable here: {exc}")
        report: Report = self._install()
        status, detail = _step(report, "guard")
        self.assertEqual(status, "skipped")
        self.assertIn("symlink", detail)
        self.assertFalse(destination.exists())
        self.assertIsNone(self._receipt()["guard"])

    def test_an_identical_unclaimed_guard_is_not_claimed(self) -> None:
        # Same bytes as Bench's guard but no receipt says install wrote it:
        # ownership is recorded, never inferred from content.
        self._init_repo()
        self.guard_path.parent.mkdir(parents=True, exist_ok=True)
        self.guard_path.write_bytes(_GUARD_BYTES)
        report: Report = self._install()
        self.assertEqual(_statuses(report)["guard"], "unchanged")
        self.assertIsNone(self._receipt()["guard"])

    def test_guard_follows_a_project_local_hooks_path(self) -> None:
        self._init_repo()
        self._git("config", "core.hooksPath", ".githooks")
        report: Report = self._install()
        self.assertEqual(_statuses(report)["guard"], "written")
        self.assertEqual((self.project / ".githooks" / "pre-commit").read_bytes(), _GUARD_BYTES)
        self.assertFalse(self.guard_path.exists())

    def test_guard_never_lands_outside_the_project(self) -> None:
        self._init_repo()
        elsewhere: Path = self.tmp / "global-hooks"
        elsewhere.mkdir()
        self._git("config", "core.hooksPath", elsewhere.as_posix())
        report: Report = self._install()
        self.assertEqual(_statuses(report)["guard"], "skipped")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_gitignore_equivalents_count_as_present_and_are_not_claimed(self) -> None:
        (self.project / ".gitignore").write_text("node_modules/\n.bench/\n", encoding="utf-8")
        report: Report = self._install()
        self.assertEqual(_statuses(report)["gitignore"], "unchanged")
        self.assertEqual((self.project / ".gitignore").read_text(encoding="utf-8"), "node_modules/\n.bench/\n")
        self.assertIsNone(self._receipt()["gitignore_appended"])

    def test_gitignore_without_trailing_newline_gets_its_own_line_and_back(self) -> None:
        (self.project / ".gitignore").write_bytes(b"dist")
        self._install()
        self.assertEqual((self.project / ".gitignore").read_bytes(), f"dist\n{IGNORE_LINE}\n".encode())
        self._uninstall()
        self.assertEqual((self.project / ".gitignore").read_bytes(), b"dist")

    def test_gitignore_line_endings_are_preserved_both_ways(self) -> None:
        original: bytes = b"node_modules/\r\ndist/\r\n"
        (self.project / ".gitignore").write_bytes(original)
        self._install()
        self.assertEqual(
            (self.project / ".gitignore").read_bytes(), original + f"{IGNORE_LINE}\r\n".encode()
        )
        self._uninstall()
        self.assertEqual((self.project / ".gitignore").read_bytes(), original)

    def test_gitignore_changed_since_install_is_kept(self) -> None:
        self._install()
        gitignore: Path = self.project / ".gitignore"
        gitignore.write_bytes(gitignore.read_bytes() + b"coverage/\n")
        report: Report = self._uninstall()
        self.assertEqual(_statuses(report)["gitignore"], "kept")
        self.assertEqual(gitignore.read_bytes(), f"{IGNORE_LINE}\ncoverage/\n".encode())

    def test_gitignore_directory_is_refused_before_any_write(self) -> None:
        (self.project / ".gitignore").mkdir()
        with self.assertRaises(InstallError):
            self._install()
        self.assertFalse(self.settings_path.exists())
        self.assertFalse(self.receipt_path.exists())

    def test_a_failure_after_the_receipt_is_reversible(self) -> None:
        # The receipt is written before any other file, so whatever landed
        # before the failure is reversed by uninstall instead of orphaned.
        self._init_repo()
        with patch.object(install_module, "_write_guard", side_effect=InstallError("disk full")):
            with self.assertRaises(InstallError):
                self._install()
        self.assertTrue(self.settings_path.exists())
        self.assertTrue(self.receipt_path.exists())
        self.assertIsNotNone(self._receipt()["hook"])
        report: Report = self._uninstall()
        self.assertEqual(_statuses(report)["hook"], "removed")
        self.assertEqual(_statuses(report)["settings"], "removed")
        self.assertEqual(_statuses(report)["gitignore"], "removed")
        self.assertFalse(self.settings_path.exists())
        self.assertFalse((self.project / ".gitignore").exists())

    def test_invalid_settings_json_is_an_error_not_an_overwrite(self) -> None:
        self.settings_path.parent.mkdir()
        self.settings_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(InstallError):
            self._install()
        self.assertEqual(self.settings_path.read_text(encoding="utf-8"), "{not json")

    def test_null_hooks_list_in_an_entry_is_skipped_not_a_crash(self) -> None:
        self._write_settings({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": None}]}})
        report: Report = self._install()
        self.assertEqual(_statuses(report)["hook"], "written")
        self.assertEqual(len(self._settings()["hooks"]["PreToolUse"]), 2)

    def test_symlinked_gitignore_is_refused_before_any_write(self) -> None:
        real: Path = self.tmp / "real-gitignore"
        real.write_text("dist/\n", encoding="utf-8")
        try:
            (self.project / ".gitignore").symlink_to(real)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable here: {exc}")
        with self.assertRaises(InstallError) as ctx:
            self._install()
        self.assertIn("symlink", str(ctx.exception))
        self.assertFalse(self.settings_path.exists())
        self.assertFalse(self.receipt_path.exists())
        self.assertEqual(real.read_text(encoding="utf-8"), "dist/\n")

    def test_settings_file_mode_is_preserved(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode bits are not enforced on Windows")
        self._write_settings({"permissions": {"allow": []}})
        self.settings_path.chmod(0o600)
        self._install()
        self.assertEqual(self.settings_path.stat().st_mode & 0o777, 0o600)

    def test_missing_project_directory_is_refused(self) -> None:
        with self.assertRaises(InstallError):
            install(self.tmp / "nowhere", resources=self.resources, interpreter=self.interpreter)


class TestUninstall(_ScratchCase):
    def test_reverses_a_fresh_install_completely(self) -> None:
        self._init_repo()
        self._install(provider="claude_code")
        report: Report = self._uninstall()
        self.assertEqual(
            _statuses(report),
            {
                "hook": "removed",
                "env": "removed",
                "settings": "removed",
                "gitignore": "removed",
                "guard": "removed",
                "receipt": "removed",
            },
        )
        self.assertFalse(self.settings_path.exists())
        self.assertFalse(self.settings_path.parent.exists())
        self.assertFalse((self.project / ".gitignore").exists())
        self.assertFalse(self.guard_path.exists())
        self.assertFalse((self.project / ".bench").exists())

    def test_without_a_receipt_nothing_is_removed(self) -> None:
        self._init_repo()
        self._install()
        self.receipt_path.unlink()
        with self.assertRaises(InstallError) as ctx:
            self._uninstall()
        self.assertIn("no install receipt", str(ctx.exception))
        self.assertTrue(self.settings_path.exists())
        self.assertTrue(self.guard_path.exists())
        self.assertIn(IGNORE_LINE, (self.project / ".gitignore").read_text(encoding="utf-8"))

    def test_keeps_everything_it_did_not_write(self) -> None:
        self._init_repo()
        self._write_settings(
            {
                "permissions": {"allow": ["Bash(ls)"]},
                "env": {"BENCH_CLAUDE_TIMEOUT": "300", "OTHER": "1"},
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]},
                        {"matcher": "Write", "hooks": [{"type": "command", "command": 'python "/elsewhere/hooks/pre-tool-use.py"'}]},
                    ]
                },
            }
        )
        (self.project / ".gitignore").write_text("dist/\n/.bench/\n", encoding="utf-8")
        self._install(provider="anthropic")
        # The install consolidated the foreign Bench hook; put a different one
        # back afterwards to stand for a hook this install did not write.
        settings: dict = self._settings()
        settings["hooks"]["PreToolUse"].append(
            {"matcher": "Write", "hooks": [{"type": "command", "command": 'python "/elsewhere/hooks/pre-tool-use.py"'}]}
        )
        self._write_settings(settings)
        self.guard_path.write_bytes(b"#!/bin/sh\n# edited by the project\n")
        report: Report = self._uninstall()
        statuses: dict[str, str] = _statuses(report)
        self.assertEqual(statuses["hook"], "removed")
        self.assertIn("1 other Bench hook", _step(report, "hook")[1])
        self.assertEqual(statuses["env"], "removed")
        self.assertEqual(statuses["gitignore"], "kept")
        self.assertEqual(statuses["guard"], "kept")
        after: dict = self._settings()
        self.assertEqual(after["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(after["env"], {"BENCH_CLAUDE_TIMEOUT": "300", "OTHER": "1"})
        commands: list[str] = [h["command"] for e in after["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertEqual(commands, ["echo hi", 'python "/elsewhere/hooks/pre-tool-use.py"'])
        self.assertEqual((self.project / ".gitignore").read_text(encoding="utf-8"), "dist/\n/.bench/\n")
        self.assertTrue(self.guard_path.exists())
        self.assertFalse(self.receipt_path.exists())

    def test_env_value_changed_since_install_is_kept(self) -> None:
        self._install(provider="claude_code")
        settings: dict = self._settings()
        settings["env"]["BENCH_PROVIDER"] = "anthropic"
        self._write_settings(settings)
        report: Report = self._uninstall()
        status, detail = _step(report, "env")
        self.assertEqual(status, "unchanged")
        self.assertIn("changed since install", detail)
        self.assertEqual(self._settings()["env"], {"BENCH_PROVIDER": "anthropic"})

    def test_a_pre_existing_empty_settings_file_and_directory_survive(self) -> None:
        self._write_settings({})
        self._install()
        self.assertFalse(self._receipt()["settings_created"])
        self.assertFalse(self._receipt()["claude_dir_created"])
        report: Report = self._uninstall()
        self.assertEqual(_statuses(report)["settings"], "written")
        self.assertTrue(self.settings_path.is_file())
        self.assertEqual(self._settings(), {})

    def test_a_pre_existing_directory_survives_when_the_file_did_not(self) -> None:
        self.settings_path.parent.mkdir()
        self._install()
        self.assertTrue(self._receipt()["settings_created"])
        self.assertFalse(self._receipt()["claude_dir_created"])
        report: Report = self._uninstall()
        self.assertEqual(_statuses(report)["settings"], "removed")
        self.assertFalse(self.settings_path.exists())
        self.assertTrue(self.settings_path.parent.is_dir())

    def test_ignore_line_stays_while_a_chain_exists(self) -> None:
        self._install()
        (self.project / ".bench" / "entries").mkdir()
        report: Report = self._uninstall()
        self.assertEqual(_statuses(report)["gitignore"], "kept")
        self.assertIn(IGNORE_LINE, (self.project / ".gitignore").read_text(encoding="utf-8"))
        self.assertTrue((self.project / ".bench" / "entries").exists())
        self.assertFalse(self.receipt_path.exists())

    def test_guard_outside_the_project_is_never_touched(self) -> None:
        self._install()
        elsewhere: Path = self.tmp / "elsewhere" / "pre-commit"
        elsewhere.parent.mkdir()
        elsewhere.write_bytes(_GUARD_BYTES)
        receipt: dict = self._receipt()
        # A recorded path that climbs out of the project with ".." is refused.
        receipt["guard"] = {"path": "../elsewhere/pre-commit", "sha256": hashlib.sha256(_GUARD_BYTES).hexdigest()}
        self.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        report: Report = self._uninstall()
        self.assertEqual(_statuses(report)["guard"], "kept")
        self.assertTrue(elsewhere.exists())

    def test_a_settings_directory_swapped_for_a_symlink_is_refused(self) -> None:
        # Only the recorded Bench hook lives in settings, so a successful
        # uninstall would delete the file: through a link that would be
        # another project's settings.
        self._install()
        other: Path = self.tmp / "other-project" / ".claude"
        other.mkdir(parents=True)
        shutil.copy(self.settings_path, other / "settings.json")
        shutil.rmtree(self.settings_path.parent)
        try:
            self.settings_path.parent.symlink_to(other, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable here: {exc}")
        with self.assertRaises(InstallError) as ctx:
            self._uninstall()
        self.assertIn("symlink", str(ctx.exception))
        self.assertTrue((other / "settings.json").is_file())
        self.assertTrue(self.receipt_path.exists())

    def test_guard_is_still_removed_after_the_project_moves(self) -> None:
        self._init_repo()
        self._install()
        moved: Path = self.tmp / "moved"
        self.project.rename(moved)
        report: Report = uninstall(moved, environ={}, stdin_isatty=_human)
        self.assertEqual(_statuses(report)["guard"], "removed")
        self.assertFalse((moved / ".git" / "hooks" / "pre-commit").exists())
        self.assertFalse((moved / RECEIPT_RELPATH).exists())

    def test_symlinked_gitignore_is_kept_not_written_through(self) -> None:
        self._install()
        real: Path = self.tmp / "real-gitignore"
        real.write_text(f"{IGNORE_LINE}\n", encoding="utf-8")
        gitignore: Path = self.project / ".gitignore"
        gitignore.unlink()
        try:
            gitignore.symlink_to(real)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable here: {exc}")
        report: Report = self._uninstall()
        self.assertEqual(_statuses(report)["gitignore"], "kept")
        self.assertEqual(real.read_text(encoding="utf-8"), f"{IGNORE_LINE}\n")


class TestUninstallHumanGate(_ScratchCase):
    def test_refuses_inside_an_agent_session(self) -> None:
        self._install()
        for marker in ("CLAUDECODE", "BENCH_SUBPROCESS", "CI"):
            with self.subTest(marker=marker):
                with self.assertRaises(InstallError) as ctx:
                    uninstall(self.project, environ={marker: "1"}, stdin_isatty=_human)
                self.assertIn(marker, str(ctx.exception))
        self.assertTrue(self.settings_path.exists())
        self.assertTrue(self.receipt_path.exists())

    def test_refuses_without_a_tty(self) -> None:
        self._install()
        with self.assertRaises(InstallError):
            uninstall(self.project, environ={}, stdin_isatty=lambda: False)
        self.assertTrue(self.settings_path.exists())

    def test_install_has_no_such_gate(self) -> None:
        with patch.dict(os.environ, {"CLAUDECODE": "1"}):
            report: Report = self._install()
        self.assertEqual(_statuses(report)["hook"], "written")


class TestCommandWrappers(unittest.TestCase):
    def test_install_requires_project(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(cmd_install(None), 1)
        self.assertIn("--project", err.getvalue())

    def test_uninstall_requires_project(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(cmd_uninstall(""), 1)
        self.assertIn("--project", err.getvalue())

    def test_install_error_is_reported_on_stderr(self) -> None:
        err = io.StringIO()
        with patch("cli.commands.install", side_effect=InstallError("no resources")):
            with redirect_stderr(err):
                self.assertEqual(cmd_install("/some/project"), 1)
        self.assertIn("no resources", err.getvalue())

    def test_install_renders_the_report(self) -> None:
        report: Report = Report()
        report.add("hook", "written", "cmd")
        out = io.StringIO()
        with patch("cli.commands.install", return_value=report):
            with redirect_stdout(out):
                self.assertEqual(cmd_install("/some/project", provider=None), 0)
        self.assertIn("hook", out.getvalue())
        self.assertIn("written", out.getvalue())

    def test_a_failed_step_would_be_exit_one(self) -> None:
        # No step records "failed" today (failures raise InstallError), so the
        # false branch of Report.ok is exercised here directly.
        report: Report = Report()
        report.add("guard", "failed", "boom")
        self.assertFalse(report.ok)
        with patch("cli.commands.install", return_value=report):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cmd_install("/some/project"), 1)

    def test_uninstall_passes_the_live_gate_inputs(self) -> None:
        with patch("cli.commands.uninstall", return_value=Report()) as fake:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cmd_uninstall("/some/project"), 0)
        _, kwargs = fake.call_args
        self.assertIs(kwargs["environ"], os.environ)
        # A bound method is a fresh object on every attribute access, so
        # identity fails where equality (same function, same stdin) holds.
        self.assertEqual(kwargs["stdin_isatty"], sys.stdin.isatty)


class TestDispatch(unittest.TestCase):
    def test_install_and_uninstall_are_routed(self) -> None:
        with patch("cli.__main__.cmd_install", return_value=0) as fake_install:
            self.assertEqual(
                cli_main.main(["cli", "install", "--project", "p", "--provider", "anthropic"]), 0
            )
        fake_install.assert_called_once_with(project="p", provider="anthropic")
        with patch("cli.__main__.cmd_uninstall", return_value=0) as fake_uninstall:
            self.assertEqual(cli_main.main(["cli", "uninstall", "--project", "p"]), 0)
        fake_uninstall.assert_called_once_with(project="p")

    def test_usage_names_both_spellings_and_both_commands(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            cli_main.main(["cli", "help"])
        text: str = out.getvalue()
        self.assertIn("bench <command>", text)
        self.assertIn("python -m cli <command>", text)
        self.assertIn("install --project PATH", text)
        self.assertIn("uninstall --project PATH", text)

    def test_run_exits_with_main_result(self) -> None:
        with patch("cli.__main__.main", return_value=3):
            with patch.object(sys, "argv", ["bench", "verify"]):
                with self.assertRaises(SystemExit) as ctx:
                    cli_main.run()
        self.assertEqual(ctx.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
