"""Tests for the claude_code provider's subprocess nonce.

The judge subprocess inherits Bench's own PreToolUse hook. The hook used to
skip governance whenever BENCH_SUBPROCESS was "1", a value anyone could set.
Now the provider mints a random token per call, records it in an owner-only
file, passes only the token to the child, and removes the file when the call
returns. The hook (tests/test_hook.py) honours a token only while its file
exists and is fresh.

These tests cover the helpers in isolation and the provider's lifecycle: the
file exists while the child runs, is gone afterwards, and is gone even when
the spawn fails. The nonce is deliberately multi-use for its lifetime, since
one child can fire the hook more than once; what bounds it is revocation and
the age window, and both are tested here.

Every symbol is resolved through the live ``api`` module at call time rather
than imported by name. tests/test_api_soft_dependency.py reloads utils.api,
which rebinds the module's classes and functions; a name cached at import
would then be a stale object, and ``assertRaises`` on a stale exception class
never matches the one the reloaded code raises.

Run: python -m unittest tests.test_subprocess_nonce -v
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from utils import api  # noqa: E402

_OK_ENVELOPE: str = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": '{"verdict": "PASS"}',
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
)

_MSGS: list[dict[str, str]] = [{"role": "user", "content": "u"}]


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=stdout, stderr="")


class NonceHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.nonce_dir = Path(self._tmp.name) / "nonces"
        patcher = mock.patch.object(api, "_subprocess_nonce_dir", return_value=self.nonce_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_issue_creates_a_hex_token_and_a_record_named_by_it(self) -> None:
        token, path = api.issue_subprocess_nonce()
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertEqual(path, self.nonce_dir / token)
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["pid"], os.getpid())
        self.assertAlmostEqual(record["created"], time.time(), delta=5)

    def test_issue_is_unique_per_call(self) -> None:
        tokens = {api.issue_subprocess_nonce()[0] for _ in range(20)}
        self.assertEqual(len(tokens), 20)

    @unittest.skipIf(sys.platform == "win32", "POSIX mode bits are not enforced on Windows")
    def test_record_is_owner_only(self) -> None:
        _, path = api.issue_subprocess_nonce()
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_issue_refuses_to_overwrite_an_existing_record(self) -> None:
        # O_EXCL: a token that already has a file is an error, not a reuse.
        self.nonce_dir.mkdir(parents=True)
        (self.nonce_dir / ("a" * 32)).write_text("{}", encoding="utf-8")
        with mock.patch.object(api.secrets, "token_hex", return_value="a" * 32):
            with self.assertRaises(FileExistsError):
                api.issue_subprocess_nonce()

    def test_verify_accepts_a_live_fresh_token(self) -> None:
        token, _ = api.issue_subprocess_nonce()
        self.assertTrue(api.verify_subprocess_nonce(token))

    def test_verify_rejects_malformed_tokens_without_touching_disk(self) -> None:
        for bad in ("1", "", "A" * 32, "0" * 31, "0" * 33, "../" + "0" * 29):
            with self.subTest(token=bad):
                self.assertFalse(api.verify_subprocess_nonce(bad))
        self.assertFalse(self.nonce_dir.exists())

    def test_verify_rejects_a_token_with_no_record(self) -> None:
        self.assertFalse(api.verify_subprocess_nonce("0" * 32))

    def test_verify_rejects_a_revoked_token(self) -> None:
        token, path = api.issue_subprocess_nonce()
        api.revoke_subprocess_nonce(path)
        self.assertFalse(path.exists())
        self.assertFalse(api.verify_subprocess_nonce(token))

    def test_verify_rejects_a_token_past_the_age_window(self) -> None:
        token, path = api.issue_subprocess_nonce()
        stale = {"pid": os.getpid(), "created": time.time() - api._SUBPROCESS_NONCE_MAX_AGE_S - 1}
        path.write_text(json.dumps(stale), encoding="utf-8")
        self.assertFalse(api.verify_subprocess_nonce(token))

    def test_verify_rejects_a_corrupt_record(self) -> None:
        token, path = api.issue_subprocess_nonce()
        for body in ("not json", "{}", '{"created": "soon"}', "[]"):
            with self.subTest(body=body):
                path.write_text(body, encoding="utf-8")
                self.assertFalse(api.verify_subprocess_nonce(token))

    def test_revoke_never_raises(self) -> None:
        missing = self.nonce_dir / ("b" * 32)
        api.revoke_subprocess_nonce(missing)


class ProviderLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.nonce_dir = Path(self._tmp.name) / "nonces"
        patcher = mock.patch.object(api, "_subprocess_nonce_dir", return_value=self.nonce_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        which = mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
        which.start()
        self.addCleanup(which.stop)

    def _live_records(self) -> list[Path]:
        return sorted(self.nonce_dir.glob("*")) if self.nonce_dir.exists() else []

    def test_record_exists_while_the_child_runs_and_is_gone_after(self) -> None:
        seen: dict[str, object] = {}

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            env = kwargs["env"]
            assert isinstance(env, dict)
            token = env["BENCH_SUBPROCESS"]
            seen["token"] = token
            seen["live"] = self._live_records()
            seen["verified"] = api.verify_subprocess_nonce(token)
            return _completed(_OK_ENVELOPE)

        with mock.patch("utils.api.subprocess.run", side_effect=fake_run):
            api._claude_cli_call("m", "sys", _MSGS, 10)

        token = seen["token"]
        assert isinstance(token, str)
        self.assertEqual(seen["live"], [self.nonce_dir / token])
        self.assertTrue(seen["verified"])
        self.assertEqual(self._live_records(), [])
        self.assertFalse(api.verify_subprocess_nonce(token))

    def test_record_is_revoked_when_the_spawn_fails(self) -> None:
        with mock.patch("utils.api.subprocess.run", side_effect=OSError("no exec")):
            with self.assertRaises(api._ProviderError):
                api._claude_cli_call("m", "sys", _MSGS, 10)
        self.assertEqual(self._live_records(), [])

    def test_record_is_revoked_when_the_child_times_out(self) -> None:
        with mock.patch(
            "utils.api.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1),
        ):
            with self.assertRaises(api._ProviderError):
                api._claude_cli_call("m", "sys", _MSGS, 10)
        self.assertEqual(self._live_records(), [])

    def test_nonce_issue_failure_is_a_provider_error_not_a_crash(self) -> None:
        with mock.patch.object(api, "issue_subprocess_nonce", side_effect=OSError("disk full")):
            with mock.patch("utils.api.subprocess.run") as run:
                with self.assertRaises(api._ProviderError):
                    api._claude_cli_call("m", "sys", _MSGS, 10)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
