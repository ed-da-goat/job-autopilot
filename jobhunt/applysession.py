"""In-app launcher for a background apply-queue subprocess.

Spawns `python -m jobhunt apply-queue` detached and tracks its single
running instance so the web UI can start it and poll its status without
blocking a request. Local pre-fill only — the spawned browser NEVER
submits any application; the candidate reviews and submits every form by hand.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from jobhunt import applylock

LOG_PATH = Path("data/apply_session.log")

_PROC: subprocess.Popen | None = None
_STARTED_AT: str | None = None


def start(limit: int = 10, min_score: int | None = None) -> dict:
    """Start the background apply-queue subprocess.

    Local pre-fill only — the spawned browser NEVER submits; the candidate
    reviews and submits every form by hand. Raises RuntimeError if a
    session is already running (either this process's own tracked
    subprocess, or another process — CLI or UI — holding the apply-session
    lock, e.g. `jobhunt apply-queue` run by hand).
    """
    global _PROC, _STARTED_AT
    if _PROC is not None and _PROC.poll() is None:
        raise RuntimeError("apply session already running")

    # Fail fast, synchronously, before spawning: hold the lock only long
    # enough to launch the subprocess. The subprocess itself re-acquires
    # the lock (via `apply_queue()`'s own `applylock.held()`) for the
    # actual duration of the session — this call is a pre-flight check so
    # the UI gets an immediate 409 instead of a subprocess doomed to lose
    # the race a moment later.
    with applylock.held():
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-u", "-m", "jobhunt", "apply-queue", "--limit", str(limit)]
        if min_score is not None:
            cmd.extend(["--min-score", str(min_score)])

        with open(LOG_PATH, "w", encoding="utf-8") as handle:
            _PROC = subprocess.Popen(
                cmd, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True
            )
    _STARTED_AT = datetime.now(UTC).isoformat()
    return status()


def status() -> dict:
    """Current state of the tracked apply-queue subprocess, if any."""
    exit_code = _PROC.poll() if _PROC is not None else None
    log_tail: list[str] = []
    if LOG_PATH.exists():
        log_tail = LOG_PATH.read_text(encoding="utf-8").splitlines()[-20:]
    return {
        "running": _PROC is not None and exit_code is None,
        "pid": _PROC.pid if _PROC is not None else None,
        "started_at": _STARTED_AT,
        "exit_code": exit_code,
        "log_tail": log_tail,
    }


def _reset_for_tests() -> None:
    global _PROC, _STARTED_AT
    _PROC = None
    _STARTED_AT = None
