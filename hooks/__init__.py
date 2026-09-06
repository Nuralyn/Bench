"""Bench's hook scripts, shipped as package data.

``pre-tool-use.py`` is the Claude Code PreToolUse hook (invoked by path, never
imported: its name is not an importable module name on purpose). ``pre-commit``
is the ledger commit guard that ``bench install`` copies into a governed
project's git hooks directory. Making this directory a package is what puts
both files inside the wheel, so ``bench install`` can locate them with
``importlib.resources`` in a checkout, an editable install, and a wheel alike.
"""
