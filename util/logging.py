"""Centralized logging for the Bench governance pipeline.

Replaces ad-hoc print()-to-stderr calls with Python's logging module,
structured output, and correlation IDs that thread through a full
Challenger -> Defender -> Oracle evaluation chain.

Usage::

    from util.logging import (
        get_logger,
        generate_correlation_id,
        log_evaluation,
        log_veto,
        log_chain,
        log_ledger_entry,
    )

    logger = get_logger("bench.hook")
    cid = generate_correlation_id()
    logger.info("pipeline started", extra={"correlation_id": cid})

All output goes to stdout via StreamHandler.  No external dependencies
beyond the Python standard library (logging, os, uuid).
"""

import logging
import os
import sys
import uuid
from typing import Optional


_LOG_FORMAT: str = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
_DATE_FORMAT: str = "%Y-%m-%dT%H:%M:%S"

_BENCH_LOGGER_NAME: str = "bench"


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger, creating the handler only once.

    Reads ``LOG_LEVEL`` from the environment (default ``INFO``).
    All output is directed to stdout with a structured format.
    Repeated calls with the same *name* return the same logger
    without adding duplicate handlers.
    """
    logger: logging.Logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    level_name: str = os.environ.get("LOG_LEVEL", "INFO").upper()
    level: int = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)

    handler: logging.StreamHandler = logging.StreamHandler(sys.stdout)  # type: ignore[type-arg]
    handler.setLevel(level)

    formatter: logging.Formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    handler.setFormatter(formatter)

    logger.addHandler(handler)
    logger.propagate = False

    return logger


def generate_correlation_id() -> str:
    """Return an 8-character hex string suitable for threading through a pipeline run."""
    return uuid.uuid4().hex[:8]


_gov_logger: logging.Logger = get_logger(f"{_BENCH_LOGGER_NAME}.governance")


def log_evaluation(
    constraint_id: str,
    tool_name: str,
    stage: str,
    verdict: str,
    reasoning_summary: str,
    correlation_id: Optional[str] = None,
) -> None:
    """Log a single constraint evaluation at the given pipeline stage.

    *stage* is one of ``challenger``, ``defender``, or ``oracle``.
    The message is emitted at INFO level.
    """
    prefix: str = f"[{correlation_id}] " if correlation_id else ""
    _gov_logger.info(
        "%s%s | %s | %s | %s — %s",
        prefix,
        constraint_id,
        tool_name,
        stage,
        verdict,
        reasoning_summary,
    )


def log_veto(
    constraint_id: str,
    tool_name: str,
    stage: str,
    reason: str,
    correlation_id: Optional[str] = None,
) -> None:
    """Log a VETO at WARNING level so it stands out in output."""
    prefix: str = f"[{correlation_id}] " if correlation_id else ""
    _gov_logger.warning(
        "%sVETO %s | %s | %s — %s",
        prefix,
        constraint_id,
        tool_name,
        stage,
        reason,
    )


def log_chain(
    correlation_id: str,
    tool_name: str,
    stages_summary: str,
) -> None:
    """Log the full decision chain for one tool call end-to-end.

    Called after all stages complete to give a single-line trace of the
    entire governance pass for *tool_name*.
    """
    _gov_logger.info(
        "[%s] chain complete | %s | %s",
        correlation_id,
        tool_name,
        stages_summary,
    )


def log_ledger_entry(
    entry_hash: str,
    constraint_id: str,
    verdict: str,
    correlation_id: Optional[str] = None,
) -> None:
    """Log a ledger write with the SHA-256 hash for cross-reference."""
    prefix: str = f"[{correlation_id}] " if correlation_id else ""
    _gov_logger.info(
        "%sledger write | %s | %s | hash=%s",
        prefix,
        constraint_id,
        verdict,
        entry_hash,
    )
