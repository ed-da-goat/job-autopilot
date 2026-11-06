"""Run heartbeat: every scheduled job writes a row, so a dead loop is loud.

The scheduler is the one part of this tool nobody watches. A launchd job that
never fires, or fires and dies, is indistinguishable from a quiet week unless
something asserts liveness. `track()` writes a `run_log` row around each
scheduled run; `job_status()` reads it back and reports staleness.

Two failure modes look identical in the table and need different fixes, so both
are reported separately: a job with no rows because launchd never ran it, and a
job with no rows because the plist was never installed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_STALE_AFTER_HOURS = 36

LAUNCH_AGENT_DIR = Path.home() / "Library" / "LaunchAgents"

# job -> (launchd plist, hours before a missing success is stale, make target that installs it)
JOBS: dict[str, tuple[str, float, str]] = {
    "daily": ("com.jobhunt.daily.plist", DEFAULT_STALE_AFTER_HOURS, "schedule-install"),
    # Mon-Fri 08:15, so a Friday run is already 72h old by Monday breakfast.
    "apply-queue": ("com.jobhunt.applyqueue.plist", 96, "schedule-install-applyqueue"),
    # Every 20 min; two missed hours means launchd dropped it or a tick is wedged.
    "watch": ("com.jobhunt.watch.plist", 2, "schedule-install-watch"),
}

_SUMMARY_LIMIT = 2000


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def start_run(
    conn: sqlite3.Connection, job: str, *, now: datetime | None = None
) -> int:
    """Open a run row. Committed immediately so a hard kill still leaves a trace."""
    cur = conn.execute(
        "INSERT INTO run_log (job, started_at) VALUES (?, ?)",
        (job, (now or _utcnow()).isoformat()),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    ok: bool,
    summary: str = "",
    now: datetime | None = None,
) -> None:
    conn.execute(
        "UPDATE run_log SET finished_at=?, ok=?, summary=? WHERE id=?",
        (
            (now or _utcnow()).isoformat(),
            1 if ok else 0,
            summary[:_SUMMARY_LIMIT],
            run_id,
        ),
    )
    conn.commit()


@dataclass
class RunHandle:
    """Handle yielded by `track` so a run can annotate its own summary line."""

    run_id: int
    job: str
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.notes.append(message)

    def summary(self) -> str:
        return " · ".join(self.notes)


def _is_clean_exit(exc: BaseException) -> bool:
    """typer.Exit(0) and SystemExit(0) are successful runs, not failures."""
    code = getattr(exc, "exit_code", None)
    if code is None and isinstance(exc, SystemExit):
        code = exc.code
    return code == 0


@contextmanager
def track(
    conn: sqlite3.Connection, job: str, *, now: datetime | None = None
) -> Iterator[RunHandle]:
    """Record a scheduled run. Failures are recorded and re-raised, never swallowed."""
    handle = RunHandle(run_id=start_run(conn, job, now=now), job=job)
    try:
        yield handle
    except BaseException as exc:
        clean = _is_clean_exit(exc)
        if not clean:
            handle.note(f"failed: {type(exc).__name__}: {exc}")
        try:
            finish_run(conn, handle.run_id, ok=clean, summary=handle.summary())
        except sqlite3.Error:  # a broken DB must not mask the original error
            pass
        raise
    finish_run(conn, handle.run_id, ok=True, summary=handle.summary())


def job_status(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Per scheduled job: is it installed, did it ever run, and how stale is it."""
    now = now or _utcnow()
    statuses = []
    for job, (plist, stale_after, make_target) in JOBS.items():
        success = conn.execute(
            "SELECT finished_at FROM run_log WHERE job=? AND ok=1 "
            "ORDER BY finished_at DESC LIMIT 1",
            (job,),
        ).fetchone()
        latest = conn.execute(
            "SELECT started_at, ok, summary FROM run_log WHERE job=? "
            "ORDER BY started_at DESC LIMIT 1",
            (job,),
        ).fetchone()
        # A run still in flight counts as alive — otherwise the daily run's own
        # digest, written before that run finishes, reports yesterday's success.
        # It is bounded by the same window, so a hard-killed run still goes stale.
        in_flight = conn.execute(
            "SELECT started_at FROM run_log WHERE job=? AND ok IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (job,),
        ).fetchone()

        def _age(stamp: str) -> float:
            return max(0.0, (now - _parse(stamp)).total_seconds() / 3600)

        age_hours = (
            _age(success["finished_at"]) if success and success["finished_at"] else None
        )
        in_progress = False
        if in_flight and in_flight["started_at"]:
            running_age = _age(in_flight["started_at"])
            if age_hours is None or running_age < age_hours:
                age_hours = running_age
                in_progress = True
        statuses.append(
            {
                "job": job,
                "last_success_at": success["finished_at"] if success else None,
                "age_hours": round(age_hours, 1) if age_hours is not None else None,
                "in_progress": in_progress,
                "stale": age_hours is None or age_hours > stale_after,
                "stale_after_hours": stale_after,
                "ever_ran": latest is not None,
                "last_ok": None
                if latest is None or latest["ok"] is None
                else bool(latest["ok"]),
                "last_error": (
                    latest["summary"]
                    if latest is not None and latest["ok"] == 0
                    else None
                ),
                "scheduler_installed": (LAUNCH_AGENT_DIR / plist).exists(),
                "install_hint": f"make {make_target}",
            }
        )
    return statuses


def any_stale(statuses: list[dict]) -> bool:
    return any(item["stale"] for item in statuses)


def _age_phrase(status: dict) -> str:
    hours = status["age_hours"]
    if hours is None:
        return "no successful run on record"
    label = "running now, started" if status.get("in_progress") else "last clean run"
    if hours < 48:
        return f"{label} {hours:.0f}h ago"
    return f"{label} {hours / 24:.1f}d ago"


def render_liveness(statuses: list[dict]) -> list[str]:
    """One line per scheduled job, worst news first in the line."""
    lines = []
    for status in statuses:
        mark = "!" if status["stale"] else "ok"
        parts = [f"[{mark}] {status['job']}: {_age_phrase(status)}"]
        if status["stale"] and status["age_hours"] is not None:
            parts.append(f"stale after {status['stale_after_hours']:.0f}h")
        if not status["scheduler_installed"]:
            parts.append(f"launchd job not installed ({status['install_hint']})")
        elif not status["ever_ran"]:
            parts.append("scheduled but has never run")
        if status["last_ok"] is False:
            parts.append(f"last attempt {status['last_error'] or 'failed'}")
        lines.append(" · ".join(parts))
    return lines
