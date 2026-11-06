"""macOS desktop notifications for the daily digest run.

Best-effort only: any failure (osascript missing, non-macOS runner, hung
process) is logged to stderr and swallowed — a notification failure must
never crash `daily.py`.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

from jobhunt import runlog
from jobhunt.digest import new_suggestions
from jobhunt.report import build_report

NOTIFY_TIMEOUT_SECONDS = 5
WEEKLY_GATE_WEEKDAY_THRESHOLD = 3  # Python weekday(): Mon=0 ... Thu=3 ... Sun=6
APPLICATIONS_GATE = 10
WARM_TOUCHES_GATE = 5


def _applescript_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def notify(title: str, body: str) -> None:
    """Fire a macOS notification via osascript. Never raises — logs to
    stderr and returns on any failure (missing binary, non-zero exit,
    timeout)."""
    script = f"display notification {_applescript_str(body)} with title {_applescript_str(title)}"
    try:
        subprocess.run(
            ["osascript", "-e", script],
            timeout=NOTIFY_TIMEOUT_SECONDS,
            check=True,
            capture_output=True,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"notify: failed to display notification: {exc}", file=sys.stderr)


def _today_str() -> str:
    return datetime.now(UTC).date().isoformat()


def should_notify(digest_data_or_conn: Any, weekday: int | None = None) -> bool:
    """True when there are new suggestions to review, or it's Thursday or
    later in the week and a weekly gate (applications/warm-touches) is
    failing — so the shortfall surfaces before Friday review instead of
    being a silent miss.

    Accepts either a live DB connection (duck-typed via `.execute`, in
    which case the signals are queried directly — reusing
    `digest.new_suggestions` and `report.build_report` rather than
    re-deriving the SQL) or a plain mapping with
    `new_suggestions`/`applications_7d`/`warm_touches_7d` keys, for
    branch-testing without a DB.

    A stale scheduled job always notifies, on any weekday. Note the mapping
    default: unlike the gate keys, `stale_jobs` defaults to stale-unknown
    (loud), because a caller that forgets to report liveness is exactly the
    silence this signal exists to break.
    """
    if hasattr(digest_data_or_conn, "execute"):
        conn = digest_data_or_conn
        new_count = len(new_suggestions(conn, _today_str()))
        report = build_report(conn)
        applications_7d = report.applications_7d
        warm_touches_7d = report.warm_touches_7d
        loop_stale = runlog.any_stale(runlog.job_status(conn))
    else:
        data = digest_data_or_conn
        new_count = data.get("new_suggestions", 0)
        applications_7d = data.get("applications_7d", APPLICATIONS_GATE)
        warm_touches_7d = data.get("warm_touches_7d", WARM_TOUCHES_GATE)
        loop_stale = bool(data.get("stale_jobs", True))

    if loop_stale or new_count > 0:
        return True

    gate_failing = (
        applications_7d < APPLICATIONS_GATE or warm_touches_7d < WARM_TOUCHES_GATE
    )
    if not gate_failing:
        return False

    wd = weekday if weekday is not None else datetime.now(UTC).weekday()
    return wd >= WEEKLY_GATE_WEEKDAY_THRESHOLD
