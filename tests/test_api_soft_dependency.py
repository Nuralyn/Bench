"""Tests that the anthropic SDK is a soft dependency of utils.api.

Only BENCH_PROVIDER=anthropic (the default) needs the SDK. The claude_code and
openrouter paths must import and run without it, and the anthropic path must
fail closed with a typed, actionable error rather than an ImportError at module
load — which would crash the hook before it emits JSON and lock out every edit.

Run: python -m unittest tests.test_api_soft_dependency -v
"""

import builtins
import importlib
import sys
import unittest
from pathlib import Path
from typing import Any

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import utils.api  # noqa: E402


class _HideAnthropic:
    """Context manager making `import anthropic` raise ImportError."""

    def __init__(self) -> None:
        self._real_import = builtins.__import__
        self._saved: dict[str, Any] = {}

    def __enter__(self) -> "_HideAnthropic":
        for name in list(sys.modules):
            if name == "anthropic" or name.startswith("anthropic."):
                self._saved[name] = sys.modules.pop(name)

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "anthropic" or name.startswith("anthropic."):
                raise ImportError("No module named 'anthropic'")
            return self._real_import(name, *args, **kwargs)

        builtins.__import__ = fake_import
        return self

    def __exit__(self, *exc: Any) -> None:
        builtins.__import__ = self._real_import
        sys.modules.update(self._saved)


class SoftDependencyTests(unittest.TestCase):
    def test_module_imports_without_anthropic(self) -> None:
        """utils.api must import cleanly when the SDK is absent."""
        with _HideAnthropic():
            module = importlib.reload(utils.api)
            self.assertTrue(hasattr(module, "call_model"))
        importlib.reload(utils.api)

    def test_anthropic_call_raises_typed_error_without_sdk(self) -> None:
        """The anthropic path fails closed with a typed, actionable error."""
        with _HideAnthropic():
            module = importlib.reload(utils.api)
            with self.assertRaises(module._ProviderError) as ctx:
                module._anthropic_call("claude-sonnet-5", "sys", [], 100)
            detail = str(ctx.exception)
            self.assertIn("not installed", detail)
            self.assertIn("BENCH_PROVIDER", detail)
        importlib.reload(utils.api)

    def test_claude_cli_path_reachable_without_sdk(self) -> None:
        """The no-API-key path must not depend on the SDK being importable."""
        with _HideAnthropic():
            module = importlib.reload(utils.api)
            self.assertTrue(hasattr(module, "_claude_cli_call"))
        importlib.reload(utils.api)


if __name__ == "__main__":
    unittest.main()
