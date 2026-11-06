"""`report` command implementation: funnel stage counts, stage-to-stage
conversion (from event history), weekly leading indicators, an unworked
top-15, and due next actions. Also backs `pipeline` (fetch_pipeline_rows).
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jobhunt.funnel import ALL_STAGES, STAGES
from jobhunt.eligibility import load_preferences

CONVERSION_STAGES = ["qualified", "applied", "replied", "interviewing", "offer"]
TOP_N = 15
TIER1_SCORE_MIN = 70
WEEKLY_GATE_MIN = 10

# A posting counts as 'fallback' tier when its suggested-stage funnel_event
# note starts with 'fallback-fill' (written by run_weekly_fallback_fill in
# suggest.py). Everything else that reached 'suggested' is tier-1.
_FALLBACK_POSTING_IDS_SQL = (
    "SELECT posting_id FROM funnel_events WHERE to_stage='suggested' AND note LIKE 'fallback-fill%'"
)


@dataclass
class ReportData:
    stage_counts: dict[str, int]
    conversion_counts: dict[str, int] = field(default_factory=dict)
    applications_7d: int = 0
    warm_touches_7d: int = 0
    new_postings_7d: int = 0
    submitted_total: int = 0
    replied_total: int = 0
    tier1_supply: int = 0
    applications_7d_by_tier: dict[str, int] = field(default_factory=lambda: {"tier1": 0, "fallback": 0})
    submitted_by_tier: dict[str, int] = field(default_factory=lambda: {"tier1": 0, "fallback": 0})
    replied_by_tier: dict[str, int] = field(default_factory=lambda: {"tier1": 0, "fallback": 0})
    # resume variant label ('none' when unset) -> (replied, submitted); lets a
    # 'Tailored' variant's callback rate be compared against the generic ones.
    response_by_variant: dict[str, tuple[int, int]] = field(default_factory=dict)
    top15: list[sqlite3.Row] = field(default_factory=list)
    next_actions: list[sqlite3.Row] = field(default_factory=list)
    rubric_version: int | None = None
    queue_precision: list[dict] = field(default_factory=list)
    pass_reasons: dict[str, int] = field(default_factory=dict)
    blocked_funnel: list[sqlite3.Row] = field(default_factory=list)
    takt_samples: int = 0
    takt_median_minutes: float | None = None
    takt_samples_7d: int = 0
    apply_lite_opens: int = 0


def _week_ago() -> str:
    return (datetime.now(UTC) - timedelta(days=7)).isoformat()


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _visited_stages(posting_id: int, current_stage: str, events: list[sqlite3.Row]) -> set[str]:
    """Every stage a posting has ever occupied: 'harvested' (the implicit
    starting stage), its current stage, and every from/to in its event
    history — restricted to the ordered STAGES for conversion purposes."""
    visited = {"harvested", current_stage}
    for e in events:
        if e["posting_id"] != posting_id:
            continue
        if e["from_stage"]:
            visited.add(e["from_stage"])
        visited.add(e["to_stage"])
    return {s for s in visited if s in STAGES}


MAX_TAKT_MINUTES = 24 * 60


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _takt_minutes(conn: sqlite3.Connection) -> tuple[list[float], int, int]:
    """Open -> submit minutes per submitted application.

    Pairs each submission with the latest `apply_lite_open` event for the same
    posting at or before `submitted_at`; pairs outside 0..24h (stale tab) are
    ignored. Returns (minutes, samples_in_last_7d, total_open_events)."""
    opens_by_posting: dict[int, list[datetime]] = {}
    total_opens = 0
    for row in conn.execute(
        "SELECT entity_id, at FROM activity_events WHERE entity_type='posting' AND action='apply_lite_open'"
    ).fetchall():
        total_opens += 1
        ts = _parse_ts(row["at"])
        if ts is not None:
            opens_by_posting.setdefault(row["entity_id"], []).append(ts)

    cutoff = datetime.now(UTC) - timedelta(days=7)
    minutes: list[float] = []
    recent = 0
    for row in conn.execute(
        "SELECT posting_id, submitted_at FROM applications WHERE submitted_at IS NOT NULL"
    ).fetchall():
        submitted = _parse_ts(row["submitted_at"])
        if submitted is None:
            continue
        candidates = [t for t in opens_by_posting.get(row["posting_id"], []) if t <= submitted]
        if not candidates:
            continue
        mins = (submitted - max(candidates)).total_seconds() / 60
        if mins < 0 or mins > MAX_TAKT_MINUTES:
            continue
        minutes.append(mins)
        if submitted >= cutoff:
            recent += 1
    return minutes, recent, total_opens


def build_report(conn: sqlite3.Connection) -> ReportData:
    stage_rows = conn.execute("SELECT stage, COUNT(*) c FROM funnel GROUP BY stage").fetchall()
    stage_counts = {row["stage"]: row["c"] for row in stage_rows}
    ordered_counts = {stage: stage_counts.get(stage, 0) for stage in [*STAGES, *("suggested", "passed", "closed")]}

    funnel_rows = conn.execute("SELECT posting_id, stage FROM funnel").fetchall()
    all_events = conn.execute("SELECT posting_id, from_stage, to_stage FROM funnel_events").fetchall()
    events_by_posting: dict[int, list[sqlite3.Row]] = {}
    for e in all_events:
        events_by_posting.setdefault(e["posting_id"], []).append(e)

    conversion_counts = dict.fromkeys(CONVERSION_STAGES, 0)
    for row in funnel_rows:
        visited = _visited_stages(row["posting_id"], row["stage"], events_by_posting.get(row["posting_id"], []))
        for stage in CONVERSION_STAGES:
            if stage in visited:
                conversion_counts[stage] += 1

    week_ago = _week_ago()
    applications_7d = conn.execute(
        "SELECT COUNT(*) c FROM applications WHERE submitted_at IS NOT NULL AND submitted_at >= ?",
        (week_ago,),
    ).fetchone()["c"]
    warm_touches_7d = conn.execute(
        "SELECT COUNT(*) c FROM touches WHERE status='completed' AND completed_at >= ?",
        (week_ago,),
    ).fetchone()["c"]
    new_postings_7d = conn.execute(
        """SELECT COUNT(*) c FROM postings
           WHERE discovered_after_baseline=1 AND first_seen_at >= ?""",
        (week_ago,),
    ).fetchone()["c"]

    submitted_total = conn.execute(
        "SELECT COUNT(*) c FROM applications WHERE submitted_at IS NOT NULL"
    ).fetchone()["c"]
    replied_total = conn.execute(
        """SELECT COUNT(DISTINCT fe.posting_id) c
           FROM funnel_events fe
           JOIN applications a ON a.posting_id=fe.posting_id
           WHERE fe.to_stage IN ('replied', 'interviewing', 'offer')
             AND a.submitted_at IS NOT NULL"""
    ).fetchone()["c"]

    applications_7d_by_tier = {"tier1": 0, "fallback": 0}
    for row in conn.execute(
        f"""SELECT CASE WHEN a.posting_id IN ({_FALLBACK_POSTING_IDS_SQL}) THEN 'fallback' ELSE 'tier1' END tier,
                   COUNT(*) c
            FROM applications a
            WHERE a.submitted_at IS NOT NULL AND a.submitted_at >= ?
            GROUP BY tier""",
        (week_ago,),
    ).fetchall():
        applications_7d_by_tier[row["tier"]] = row["c"]

    submitted_by_tier = {"tier1": 0, "fallback": 0}
    for row in conn.execute(
        f"""SELECT CASE WHEN a.posting_id IN ({_FALLBACK_POSTING_IDS_SQL}) THEN 'fallback' ELSE 'tier1' END tier,
                   COUNT(*) c
            FROM applications a
            WHERE a.submitted_at IS NOT NULL
            GROUP BY tier""",
    ).fetchall():
        submitted_by_tier[row["tier"]] = row["c"]

    replied_by_tier = {"tier1": 0, "fallback": 0}
    for row in conn.execute(
        f"""SELECT CASE WHEN fe.posting_id IN ({_FALLBACK_POSTING_IDS_SQL}) THEN 'fallback' ELSE 'tier1' END tier,
                   COUNT(DISTINCT fe.posting_id) c
            FROM funnel_events fe
            JOIN applications a ON a.posting_id=fe.posting_id
            WHERE fe.to_stage IN ('replied', 'interviewing', 'offer')
              AND a.submitted_at IS NOT NULL
            GROUP BY tier""",
    ).fetchall():
        replied_by_tier[row["tier"]] = row["c"]

    response_by_variant = {
        row["label"]: (row["replied"], row["submitted"])
        for row in conn.execute(
            """SELECT COALESCE(rv.label, 'none') label,
                      COUNT(*) submitted,
                      SUM(EXISTS (SELECT 1 FROM funnel_events fe
                                  WHERE fe.posting_id=a.posting_id
                                    AND fe.to_stage IN ('replied', 'interviewing', 'offer'))) replied
               FROM applications a
               LEFT JOIN resume_variants rv ON rv.id=a.resume_variant_id
               WHERE a.submitted_at IS NOT NULL
               GROUP BY label
               ORDER BY submitted DESC, label"""
        ).fetchall()
    }

    rubric_version =conn.execute("SELECT MAX(rubric_version) v FROM scores").fetchone()["v"]

    tier1_supply = 0
    if rubric_version is not None:
        supply_preferences = load_preferences()
        not_in_funnel = conn.execute(
            """SELECT COUNT(*) c
               FROM scores s
               JOIN postings p ON p.id = s.posting_id
               JOIN eligibility_assessments e
                 ON e.posting_id=s.posting_id AND e.rubric_version=s.rubric_version
                AND e.preferences_version=?
               WHERE s.rubric_version = ? AND s.total >= ? AND s.excluded = 0
                 AND e.status='eligible' AND p.status = 'active'
                 AND s.posting_id NOT IN (SELECT posting_id FROM funnel)""",
            (supply_preferences.version, rubric_version, TIER1_SCORE_MIN),
        ).fetchone()["c"]
        in_suggested = conn.execute(
            """SELECT COUNT(*) c
               FROM scores s
               JOIN postings p ON p.id = s.posting_id
               JOIN eligibility_assessments e
                 ON e.posting_id=s.posting_id AND e.rubric_version=s.rubric_version
                AND e.preferences_version=?
               JOIN funnel f ON f.posting_id = s.posting_id AND f.stage='suggested'
               WHERE s.rubric_version = ? AND s.total >= ? AND s.excluded = 0
                 AND e.status='eligible' AND p.status = 'active'""",
            (supply_preferences.version, rubric_version, TIER1_SCORE_MIN),
        ).fetchone()["c"]
        tier1_supply = not_in_funnel + in_suggested
    top15: list[sqlite3.Row] = []
    if rubric_version is not None:
        preferences = load_preferences()
        top15 = conn.execute(
            """
            SELECT s.posting_id, s.total, p.company, p.title
            FROM scores s
            JOIN postings p ON p.id = s.posting_id
            JOIN eligibility_assessments e
              ON e.posting_id=s.posting_id AND e.rubric_version=s.rubric_version
             AND e.preferences_version=?
            WHERE s.rubric_version = ? AND s.excluded = 0 AND p.status = 'active'
              AND e.status='eligible'
              AND s.posting_id NOT IN (SELECT posting_id FROM funnel)
            ORDER BY s.total DESC, s.posting_id
            LIMIT ?
            """,
            (preferences.version, rubric_version, TOP_N),
        ).fetchall()

    queue_precision: list[dict] = []
    if rubric_version is not None:
        queue_precision = [
            dict(row)
            for row in conn.execute(
                """SELECT (s.total / 10) * 10 score_band,
                          COUNT(*) reviewed,
                          SUM(CASE WHEN f.stage='passed' THEN 1 ELSE 0 END) passed,
                          SUM(CASE WHEN f.stage NOT IN ('suggested','passed','closed','harvested')
                                   THEN 1 ELSE 0 END) accepted
                   FROM funnel f
                   JOIN scores s ON s.posting_id=f.posting_id AND s.rubric_version=?
                   WHERE EXISTS (SELECT 1 FROM funnel_events fe
                                 WHERE fe.posting_id=f.posting_id AND fe.to_stage='suggested')
                     AND f.stage!='suggested'
                   GROUP BY score_band ORDER BY score_band DESC""",
                (rubric_version,),
            ).fetchall()
        ]
    pass_reasons = {
        row["pass_reason"]: row["c"]
        for row in conn.execute(
            """SELECT pass_reason, COUNT(*) c FROM funnel
               WHERE stage='passed' AND pass_reason IS NOT NULL GROUP BY pass_reason"""
        ).fetchall()
    }

    blocked_funnel: list[sqlite3.Row] = []
    if rubric_version is not None:
        preferences = load_preferences()
        blocked_funnel = conn.execute(
            """SELECT f.posting_id, f.stage, p.company, p.title,
                      e.blockers, e.evidence
               FROM funnel f
               JOIN postings p ON p.id=f.posting_id
               JOIN eligibility_assessments e
                 ON e.posting_id=f.posting_id AND e.rubric_version=?
                AND e.preferences_version=?
               WHERE f.stage NOT IN ('passed','closed','offer')
                 AND e.status='blocked'
               ORDER BY f.updated_at DESC, f.posting_id""",
            (rubric_version, preferences.version),
        ).fetchall()

    next_actions = conn.execute(
        """
        SELECT posting_id, stage, next_action, next_action_date
        FROM funnel
        WHERE next_action_date IS NOT NULL AND next_action_date <= ?
          AND stage NOT IN ('passed', 'closed', 'offer')
        ORDER BY next_action_date
        """,
        (_today(),),
    ).fetchall()

    takt_minutes, takt_samples_7d, apply_lite_opens = _takt_minutes(conn)

    return ReportData(
        stage_counts=ordered_counts,
        conversion_counts=conversion_counts,
        applications_7d=applications_7d,
        warm_touches_7d=warm_touches_7d,
        new_postings_7d=new_postings_7d,
        submitted_total=submitted_total,
        replied_total=replied_total,
        tier1_supply=tier1_supply,
        applications_7d_by_tier=applications_7d_by_tier,
        submitted_by_tier=submitted_by_tier,
        replied_by_tier=replied_by_tier,
        response_by_variant=response_by_variant,
        top15=top15,
        next_actions=next_actions,
        rubric_version=rubric_version,
        queue_precision=queue_precision,
        pass_reasons=pass_reasons,
        blocked_funnel=blocked_funnel,
        takt_samples=len(takt_minutes),
        takt_median_minutes=statistics.median(takt_minutes) if takt_minutes else None,
        takt_samples_7d=takt_samples_7d,
        apply_lite_opens=apply_lite_opens,
    )


def _rate_str(replied: int, submitted: int) -> str:
    if submitted:
        pct = round(replied / submitted * 100)
        return f"{replied}/{submitted} ({pct}%)"
    return "0/0 (n/a)"


def format_report(data: ReportData) -> str:
    lines = ["Funnel stage counts:"]
    for stage, count in data.stage_counts.items():
        lines.append(f"  {stage:<14}{count}")

    lines.append("")
    lines.append("Conversion (qualified -> applied -> replied -> interviewing -> offer):")
    prev_count: int | None = None
    for stage in CONVERSION_STAGES:
        count = data.conversion_counts.get(stage, 0)
        pct = f" ({count / prev_count:.0%})" if prev_count else ""
        lines.append(f"  {stage:<14}{count}{pct}")
        prev_count = count

    lines.append("")
    lines.append("Leading indicators (last 7d):")
    lines.append(f"  applications: {data.applications_7d}")
    lines.append(f"  warm touches: {data.warm_touches_7d}")
    lines.append(f"  new postings: {data.new_postings_7d}")
    if data.submitted_total:
        pct = round(data.replied_total / data.submitted_total * 100)
        rate = f"{data.replied_total}/{data.submitted_total} ({pct}%)"
    else:
        rate = "0/0 (n/a)"
    lines.append(f"  response rate: {rate}")
    if data.applications_7d == 0:
        lines.append("  WARNING: 0 applications in the last 7 days")

    lines.append(f"  tier-1 supply: {data.tier1_supply} appliable (gate: 10-12 apps/week)")
    apps_tier1 = data.applications_7d_by_tier.get("tier1", 0)
    apps_fallback = data.applications_7d_by_tier.get("fallback", 0)
    apps7d_line = f"  applications 7d: {data.applications_7d} (tier-1 {apps_tier1} / fallback {apps_fallback})"
    if data.applications_7d < WEEKLY_GATE_MIN:
        apps7d_line += f"   WARNING: below the {WEEKLY_GATE_MIN}-12 apps/week gate"
    lines.append(apps7d_line)
    if data.takt_median_minutes is None:
        lines.append(
            f"  Minutes per application: no data yet (apply-lite open events: {data.apply_lite_opens})"
        )
    else:
        lines.append(
            f"  Minutes per application (open→submit, median of {data.takt_samples}): "
            f"{data.takt_median_minutes:.1f} ({data.takt_samples_7d} in last 7d)"
        )
    lines.append(
        "  response rate by tier: "
        f"tier-1 {_rate_str(data.replied_by_tier.get('tier1', 0), data.submitted_by_tier.get('tier1', 0))} "
        f"· fallback {_rate_str(data.replied_by_tier.get('fallback', 0), data.submitted_by_tier.get('fallback', 0))}"
    )
    if data.response_by_variant:
        by_variant = " · ".join(
            f"{label} {_rate_str(replied, submitted)}"
            for label, (replied, submitted) in data.response_by_variant.items()
        )
        lines.append(f"  response rate by resume: {by_variant}")

    lines.append("")
    lines.append("Blocked roles still in active funnel (remove or reassess):")
    if not data.blocked_funnel:
        lines.append("  (none)")
    for row in data.blocked_funnel:
        blockers = ", ".join(json.loads(row["blockers"] or "[]")) or "blocked"
        lines.append(
            f"  [{row['posting_id']:>6}] ({row['stage']}) {row['company']} — "
            f"{row['title']} [{blockers}]"
        )

    lines.append("")
    if data.rubric_version is None:
        lines.append("Top 15 (unworked): no scores yet — run `score` or `resweep` first.")
    else:
        lines.append(f"Top {TOP_N} unworked (rubric v{data.rubric_version}, not yet in funnel):")
        if not data.top15:
            lines.append("  (none — every scored posting is already in the funnel)")
        for row in data.top15:
            lines.append(f"  [{row['posting_id']:>6}] {row['total']:>3}  {row['company']} — {row['title']}")

    lines.append("")
    lines.append("Next actions due:")
    if not data.next_actions:
        lines.append("  (none due)")
    for row in data.next_actions:
        lines.append(
            f"  [{row['posting_id']:>6}] {row['next_action_date']}  ({row['stage']})  {row['next_action']}"
        )

    return "\n".join(lines)


def fetch_pipeline_rows(conn: sqlite3.Connection, stage: str | None = None) -> list[sqlite3.Row]:
    """Funnel rows joined to their posting + latest-rubric score, optionally
    filtered to one stage. Used by the `pipeline` CLI command."""
    if stage is not None and stage not in ALL_STAGES:
        raise ValueError(f"unknown stage {stage!r}. Valid stages: {', '.join(ALL_STAGES)}")

    rubric_version = conn.execute("SELECT MAX(rubric_version) v FROM scores").fetchone()["v"]
    where = "WHERE f.stage = ?" if stage else ""
    params: list = [rubric_version]
    if stage:
        params.append(stage)

    return conn.execute(
        f"""
        SELECT f.posting_id, f.stage, f.next_action, p.company, p.title, s.total
        FROM funnel f
        JOIN postings p ON p.id = f.posting_id
        LEFT JOIN scores s ON s.posting_id = f.posting_id AND s.rubric_version = ?
        {where}
        ORDER BY f.posting_id
        """,
        params,
    ).fetchall()


def format_pipeline_rows(rows: list[sqlite3.Row]) -> str:
    lines = [f"{'id':<8}{'stage':<14}{'score':>6}  {'company':<24}{'title':<30}next_action", "-" * 100]
    for r in rows:
        score_str = str(r["total"]) if r["total"] is not None else "-"
        company = (r["company"] or "")[:23]
        title = (r["title"] or "")[:29]
        next_action = r["next_action"] or ""
        lines.append(f"{r['posting_id']:<8}{r['stage']:<14}{score_str:>6}  {company:<24}{title:<30}{next_action}")
    return "\n".join(lines)


def format_show(conn: sqlite3.Connection, posting_id: int) -> str | None:
    """Posting summary + latest score breakdown + funnel state + event
    history, as printable text. None if the posting doesn't exist."""
    posting = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
    if posting is None:
        return None

    lines = [
        f"[{posting_id}] {posting['company']} — {posting['title']} ({posting['source']}, {posting['status']})",
        f"  {posting['url']}",
    ]

    latest_version = conn.execute("SELECT MAX(rubric_version) v FROM scores").fetchone()["v"]
    score_row = None
    if latest_version is not None:
        score_row = conn.execute(
            "SELECT * FROM scores WHERE posting_id=? AND rubric_version=?", (posting_id, latest_version)
        ).fetchone()
    if score_row is None:
        lines.append("  score: none")
    else:
        components = json.loads(score_row["components"])
        comp_line = ", ".join(f"{k}={v}" for k, v in components.items())
        lines.append(f"  score (v{latest_version}): {score_row['total']} — {comp_line}")
        evidence = json.loads(score_row["score_evidence"] or "{}")
        for component, note in evidence.items():
            lines.append(f"  {component} evidence: {note}")
        if score_row["excluded"]:
            lines.append(f"  excluded: {score_row['exclude_reason']}")

    funnel_row = conn.execute("SELECT * FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
    if funnel_row is None:
        lines.append("  funnel: not entered (harvested only, no funnel row yet)")
        return "\n".join(lines)

    lines.append(
        f"  funnel: stage={funnel_row['stage']} tier={funnel_row['tier']} "
        f"referral_path={funnel_row['referral_path']} next_action={funnel_row['next_action']} "
        f"next_action_date={funnel_row['next_action_date']} applied_at={funnel_row['applied_at']}"
    )
    if funnel_row["notes"]:
        lines.append(f"  notes: {funnel_row['notes']}")

    events = conn.execute("SELECT * FROM funnel_events WHERE posting_id=? ORDER BY at", (posting_id,)).fetchall()
    if events:
        lines.append("  events:")
        for e in events:
            note_suffix = f" ({e['note']})" if e["note"] else ""
            lines.append(f"    {e['at']}  {e['from_stage']} -> {e['to_stage']}{note_suffix}")

    return "\n".join(lines)
