"""Read-only operational health for harvest freshness and opt-in scheduling."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from jobhunt import runlog

# One definition, shared with the scheduled-run heartbeat in runlog.
STALE_AFTER_HOURS = runlog.DEFAULT_STALE_AFTER_HOURS
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / "com.jobhunt.daily.plist"


def build_health(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    rows = conn.execute(
        """SELECT ss.source, ss.baseline_at,
                  COALESCE(ss.last_success_at,
                    (SELECT MAX(s.finished_at) FROM sweeps s
                     WHERE s.source=ss.source AND s.boards_ok>0)) last_success_at,
                  ss.last_error_at, ss.last_error, ss.consecutive_failures
           FROM source_state ss ORDER BY ss.source"""
    ).fetchall()
    sources = []
    for row in rows:
        age_hours = None
        if row["last_success_at"]:
            stamp = datetime.fromisoformat(row["last_success_at"])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            age_hours = max(0.0, (now - stamp).total_seconds() / 3600)
        sources.append(
            {
                **dict(row),
                "age_hours": round(age_hours, 1) if age_hours is not None else None,
                "stale": age_hours is None or age_hours > STALE_AFTER_HOURS,
            }
        )
    jobs = runlog.job_status(conn, now=now)
    return {
        "sources": sources,
        "jobs": jobs,
        "scheduler_installed": LAUNCH_AGENT_PATH.exists(),
        "healthy": (
            bool(sources)
            and all(not item["stale"] for item in sources)
            and not runlog.any_stale(jobs)
        ),
        "stale_after_hours": STALE_AFTER_HOURS,
    }
