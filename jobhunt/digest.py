"""Daily digest: a markdown snapshot of the funnel written to
`data/digest/YYYY-MM-DD.md` (flat files, not DB) for `daily.py` and the
(deferred) `/digest` web page / `digest` CLI command to read.

Reuses `report.build_report()` for weekly-gate and next-action numbers
rather than re-deriving them — those are the canonical queries.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobhunt.autopilot import autopilot_digest_lines
from jobhunt import runlog as runlog_module
from jobhunt import spend as spend_module
from jobhunt.report import build_report
from jobhunt.today import invariant_scan

DEFAULT_DIGEST_DIR = Path(__file__).resolve().parent.parent / "data" / "digest"

TOP_MOVERS_N = 5
APPLICATIONS_GATE = 10
WARM_TOUCHES_GATE = 5


def _today_str() -> str:
    return datetime.now(UTC).date().isoformat()


def new_suggestions(conn: sqlite3.Connection, date_str: str) -> list[sqlite3.Row]:
    """Postings moved to the 'suggested' funnel stage today or yesterday
    (relative to `date_str`), newest/highest score first. Public so
    `notify.py` can reuse the same query instead of re-deriving it."""
    target_date = datetime.fromisoformat(date_str).date()
    window_start = (target_date - timedelta(days=1)).isoformat() + "T00:00:00"
    window_end = (target_date + timedelta(days=1)).isoformat() + "T00:00:00"
    rubric_version = conn.execute(
        "SELECT MAX(rubric_version) v FROM scores"
    ).fetchone()["v"]

    return conn.execute(
        """
        SELECT f.posting_id, f.created_at, s.total, p.company, p.title, p.url
        FROM funnel f
        JOIN postings p ON p.id = f.posting_id
        LEFT JOIN scores s ON s.posting_id = f.posting_id AND s.rubric_version = ?
        WHERE f.stage = 'suggested' AND f.created_at >= ? AND f.created_at < ?
        ORDER BY s.total DESC, f.posting_id
        """,
        (rubric_version, window_start, window_end),
    ).fetchall()


def _sweep_warnings(conn: sqlite3.Connection, date_str: str) -> list[str]:
    """Any error entries recorded on today's sweeps rows, summarized one
    line per sweep row that had errors."""
    day_start = date_str + "T00:00:00"
    day_end = (
        datetime.fromisoformat(date_str).date() + timedelta(days=1)
    ).isoformat() + "T00:00:00"
    rows = conn.execute(
        "SELECT source, errors FROM sweeps WHERE started_at >= ? AND started_at < ?",
        (day_start, day_end),
    ).fetchall()

    warnings: list[str] = []
    for row in rows:
        errors = json.loads(row["errors"] or "[]")
        if not errors:
            continue
        first_msg = errors[0].get("error", "unknown error")
        warnings.append(
            f"WARNING: {row['source']}: {len(errors)} error(s) (e.g. {first_msg})"
        )
    return warnings


def _eligibility_snapshot(conn: sqlite3.Connection) -> dict | None:
    """Latest-version eligibility status counts over active postings, plus
    how many blocked postings are already sitting in the funnel. Returns
    None when eligibility_assessments is empty (nothing to report)."""
    latest = conn.execute(
        """SELECT rubric_version, preferences_version FROM eligibility_assessments
           ORDER BY rubric_version DESC, preferences_version DESC LIMIT 1"""
    ).fetchone()
    if latest is None:
        return None

    rubric_version = latest["rubric_version"]
    preferences_version = latest["preferences_version"]

    counts = {"eligible": 0, "review": 0, "blocked": 0}
    for row in conn.execute(
        """SELECT e.status, COUNT(*) c
           FROM eligibility_assessments e
           JOIN postings p ON p.id = e.posting_id AND p.status = 'active'
           WHERE e.rubric_version = ? AND e.preferences_version = ?
           GROUP BY e.status""",
        (rubric_version, preferences_version),
    ).fetchall():
        counts[row["status"]] = row["c"]

    blocked_in_funnel = conn.execute(
        """SELECT COUNT(*) c
           FROM funnel f
           JOIN eligibility_assessments e
             ON e.posting_id = f.posting_id
            AND e.rubric_version = ? AND e.preferences_version = ?
           WHERE e.status = 'blocked'
             AND f.stage NOT IN ('passed', 'closed', 'offer')""",
        (rubric_version, preferences_version),
    ).fetchone()["c"]

    return {
        "preferences_version": preferences_version,
        "eligible": counts["eligible"],
        "review": counts["review"],
        "blocked": counts["blocked"],
        "blocked_in_funnel": blocked_in_funnel,
    }


def build_digest(
    conn: sqlite3.Connection,
    date_str: str | None = None,
    extra_notes: list[str] | None = None,
) -> str:
    """Build the digest markdown for `date_str` (defaults to today, UTC).

    `extra_notes` is an optional passthrough (e.g. auto-prep stop/cap
    notices from daily.py) rendered in a trailing Notes section; it never
    affects the deterministic core sections above it.
    """
    date_str = date_str or _today_str()
    lines = [f"# Job Hunt Digest — {date_str}", ""]

    # Liveness leads. A digest that opens with counts looks identical whether the
    # scheduler ran this morning or died ten days ago.
    lines.append("## Loop Liveness")
    lines.extend(runlog_module.render_liveness(runlog_module.job_status(conn)))
    lines.append("")

    suggestions = new_suggestions(conn, date_str)
    lines.append("## New Suggestions")
    if not suggestions:
        lines.append("(none)")
    else:
        for row in suggestions:
            score = row["total"] if row["total"] is not None else "-"
            lines.append(
                f"- [{row['posting_id']}] {score}  {row['company']} — {row['title']}  {row['url']}"
            )
    lines.append("")

    report = build_report(conn)
    apps_status = "OK" if report.applications_7d >= APPLICATIONS_GATE else "SHORT"
    warm_status = "OK" if report.warm_touches_7d >= WARM_TOUCHES_GATE else "SHORT"
    lines.append("## Weekly Gates")
    lines.append(
        f"- applications (7d): {report.applications_7d} / {APPLICATIONS_GATE} — {apps_status}"
    )
    lines.append(
        f"- warm touches (7d): {report.warm_touches_7d} / {WARM_TOUCHES_GATE} — {warm_status}"
    )
    lines.append("")

    lines.append("## Next Actions Due/Overdue")
    if not report.next_actions:
        lines.append("(none due)")
    else:
        today = _today_str()
        for row in report.next_actions:
            tag = "OVERDUE" if row["next_action_date"] < today else "DUE"
            lines.append(
                f"- [{tag}] [{row['posting_id']}] {row['next_action_date']}  "
                f"({row['stage']})  {row['next_action']}"
            )
    lines.append("")

    lines.append("## Top Movers")
    movers = report.top15[:TOP_MOVERS_N]
    if not movers:
        lines.append("(none — every scored posting is already in the funnel)")
    else:
        for row in movers:
            lines.append(
                f"- [{row['posting_id']}] {row['total']}  {row['company']} — {row['title']}"
            )
    lines.append("")

    snapshot = _eligibility_snapshot(conn)
    if snapshot is not None:
        lines.append("## Open the OS")
        lines.append("- Today view: http://127.0.0.1:8787 (`make ui` if not running)")
        lines.append(
            f"- Eligible: {snapshot['eligible']} · review: {snapshot['review']} · "
            f"blocked: {snapshot['blocked']} (active postings, "
            f"prefs v{snapshot['preferences_version']})"
        )
        lines.append(
            f"- Blocked-in-funnel needing one-click pass: {snapshot['blocked_in_funnel']}"
        )
        inv = invariant_scan(conn)
        lines.append(
            f"- Dropped balls: {inv['no_next_action_count']} missing next action · "
            f"{inv['applied_missing_submitted_count']} applied w/o submit date"
        )
        lines.append("")

    lines_ap = autopilot_digest_lines(conn, date_str)
    if lines_ap:
        lines.append("## Autopilot")
        lines.extend(lines_ap)
        lines.append("")

    today_spend = spend_module.today_spend(conn)
    if today_spend > 0:
        lines.append("## Spend Today")
        lines.append(f"- ${today_spend:.4f}")
        lines.append("")

    warnings = _sweep_warnings(conn, date_str)
    if warnings:
        lines.append("## Sweep Warnings")
        lines.extend(f"- {w}" for w in warnings)
        lines.append("")

    if extra_notes:
        lines.append("## Notes")
        lines.extend(f"- {n}" for n in extra_notes)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_digest(
    conn: sqlite3.Connection,
    date_str: str | None = None,
    extra_notes: list[str] | None = None,
    digest_dir: Path | str = DEFAULT_DIGEST_DIR,
) -> Path:
    """Build the digest and write it to data/digest/YYYY-MM-DD.md (creating
    the directory if needed). Returns the written path."""
    date_str = date_str or _today_str()
    content = build_digest(conn, date_str, extra_notes=extra_notes)
    out_dir = Path(digest_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{date_str}.md"
    path.write_text(content, encoding="utf-8")
    return path
