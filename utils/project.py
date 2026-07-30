"""Which project is currently being governed.

Bench's PreToolUse hook can be registered globally, in which case it governs
every project on the machine. Several subsystems must agree on which project a
given run belongs to:

  * ``ledger.chain`` routes the verdict to that project's ledger
  * ``ledger.chain`` classifies a change as in-project or external
  * ``pipeline.constitution`` resolves that project's constitution layer

If those answers could disagree, a change could be judged against one project's
constitution while being recorded in another project's ledger. This module is
the single definition they all resolve through, so that cannot happen.

Claude Code invokes hooks with the governed project as the working directory.
That is the same assumption ``utils.diff`` relies on when normalizing paths
that fall outside the Bench repo.
"""

import sys
from pathlib import Path

BENCH_ROOT: Path = Path(__file__).resolve().parent.parent


def project_root() -> Path:
    """The root of the project currently being governed.

    A working directory anywhere inside the Bench repo counts as Bench
    governing itself, which is why editing ``utils/api.py`` while sitting in
    ``tests/`` is still in-project.
    """
    try:
        cwd: Path = Path.cwd().resolve()
    except OSError as exc:
        # A deleted or unreadable CWD cannot be recovered here. Fall back to
        # Bench's own root so the run still resolves somewhere rather than
        # failing obscurely (C-001: no silent swallowing).
        print(
            f"[bench project] cannot resolve working directory ({exc}); "
            f"treating Bench's own repo as the project root",
            file=sys.stderr,
        )
        return BENCH_ROOT

    if cwd == BENCH_ROOT or BENCH_ROOT in cwd.parents:
        return BENCH_ROOT
    return cwd


def governs_bench_itself() -> bool:
    """True when the run is Bench governing its own repository."""
    return project_root() == BENCH_ROOT
