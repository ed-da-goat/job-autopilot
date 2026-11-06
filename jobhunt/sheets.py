"""`sheets-sync`: one-way mirror of the SQLite pipeline into a Google Sheet.

SQLite stays the source of truth. Every sync clears each worksheet tab and
rewrites it from a fresh query — no cell-level diffing, no reads back from
the sheet. Nothing here ever reads from Sheets or writes back to SQLite.

gspread is imported lazily inside `push()` so this module — including
`run_sync(..., dry_run=True)` and every payload builder — works without the
`gspread`/`google-auth` packages installed or any credentials on disk.
"""

from __future__ import annotations

import json
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path

from jobhunt.finai import REPORT_KIND, city_tier
from jobhunt.funnel import STAGES

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOCAL_CONFIG_PATH = DATA_DIR / "sheets.local.toml"
TEMPLATE_CONFIG_PATH = DATA_DIR / "sheets.local.template.toml"

DEFAULT_SHORTLIST_MIN_SCORE = 55
DEFAULT_SHORTLIST_LIMIT = 200

# Stages counted as "past qualification" for the pipeline tab: everything on
# the primary track at or beyond 'qualified'. 'harvested' (not yet reviewed)
# and the side stages ('suggested', 'passed', 'closed') are excluded — a
# passed posting's current stage is 'passed', not a qualification stage.
PIPELINE_STAGES = [s for s in STAGES if s != "harvested"]

Payload = tuple[list[str], list[list]]


class SheetsConfigError(RuntimeError):
    """Raised when data/sheets.local.toml is missing or incomplete."""


@dataclass(frozen=True)
class SheetsConfig:
    spreadsheet_id: str
    service_account_json: str
    shortlist_min_score: int = DEFAULT_SHORTLIST_MIN_SCORE
    shortlist_limit: int = DEFAULT_SHORTLIST_LIMIT


def load_config(path: Path | None = None) -> SheetsConfig:
    """Load data/sheets.local.toml. Raises SheetsConfigError (with a
    copy-the-template pointer) if the file is missing or incomplete."""
    selected = path or LOCAL_CONFIG_PATH
    if not selected.exists():
        raise SheetsConfigError(
            f"missing {selected} — copy data/sheets.local.template.toml to "
            f"{selected.name} and fill in spreadsheet_id and service_account_json."
        )
    with selected.open("rb") as handle:
        data = tomllib.load(handle)
    missing = [k for k in ("spreadsheet_id", "service_account_json") if not data.get(k)]
    if missing:
        raise SheetsConfigError(
            f"{selected} is missing {', '.join(missing)} — see "
            "data/sheets.local.template.toml for the required keys."
        )
    return SheetsConfig(
        spreadsheet_id=str(data["spreadsheet_id"]),
        service_account_json=str(data["service_account_json"]),
        shortlist_min_score=int(data.get("shortlist_min_score", DEFAULT_SHORTLIST_MIN_SCORE)),
        shortlist_limit=int(data.get("shortlist_limit", DEFAULT_SHORTLIST_LIMIT)),
    )


def _current_rubric_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(rubric_version) v FROM scores").fetchone()
    return row["v"] if row else None


def _first_event_dates(conn: sqlite3.Connection, posting_ids: list[int], to_stage: str) -> dict[int, str]:
    if not posting_ids:
        return {}
    placeholders = ",".join("?" * len(posting_ids))
    rows = conn.execute(
        f"""SELECT posting_id, MIN(at) at FROM funnel_events
            WHERE to_stage=? AND posting_id IN ({placeholders}) GROUP BY posting_id""",
        (to_stage, *posting_ids),
    ).fetchall()
    return {r["posting_id"]: r["at"] for r in rows}


def _last_events(conn: sqlite3.Connection, posting_ids: list[int]) -> dict[int, dict]:
    if not posting_ids:
        return {}
    placeholders = ",".join("?" * len(posting_ids))
    rows = conn.execute(
        f"""SELECT posting_id, note, at FROM funnel_events
            WHERE id IN (
                SELECT MAX(id) FROM funnel_events
                WHERE posting_id IN ({placeholders}) GROUP BY posting_id
            )""",
        posting_ids,
    ).fetchall()
    return {r["posting_id"]: {"at": r["at"], "note": r["note"]} for r in rows}


def _next_touch_due(conn: sqlite3.Connection, posting_ids: list[int]) -> dict[int, str]:
    if not posting_ids:
        return {}
    placeholders = ",".join("?" * len(posting_ids))
    rows = conn.execute(
        f"""SELECT posting_id, MIN(due_at) due_at FROM touches
            WHERE status='planned' AND posting_id IN ({placeholders}) GROUP BY posting_id""",
        posting_ids,
    ).fetchall()
    return {r["posting_id"]: r["due_at"] for r in rows}


def build_pipeline_tab(conn: sqlite3.Connection) -> Payload:
    """Every posting whose funnel stage is at or past 'qualified'. Ordered by
    stage progression (most advanced first), then score desc."""
    header = [
        "stage", "company", "title", "score", "location", "url",
        "qualified_at", "last_event_at", "last_event_note", "next_touch_due_at",
    ]
    rubric_version = _current_rubric_version(conn)
    placeholders = ",".join("?" * len(PIPELINE_STAGES))
    rows = conn.execute(
        f"""SELECT f.posting_id, f.stage, p.company, p.title, p.location, p.url,
                   s.total AS score
            FROM funnel f
            JOIN postings p ON p.id = f.posting_id
            LEFT JOIN scores s ON s.posting_id = f.posting_id AND s.rubric_version = ?
            WHERE f.stage IN ({placeholders})""",
        (rubric_version, *PIPELINE_STAGES),
    ).fetchall()

    posting_ids = [r["posting_id"] for r in rows]
    qualified_at = _first_event_dates(conn, posting_ids, "qualified")
    last_event = _last_events(conn, posting_ids)
    next_touch = _next_touch_due(conn, posting_ids)

    stage_order = {s: i for i, s in enumerate(STAGES)}
    ordered = sorted(rows, key=lambda r: (-stage_order.get(r["stage"], 0), -(r["score"] or 0)))

    data_rows = []
    for r in ordered:
        pid = r["posting_id"]
        ev = last_event.get(pid, {})
        data_rows.append([
            r["stage"], r["company"] or "", r["title"] or "", r["score"],
            r["location"] or "", r["url"] or "", qualified_at.get(pid, ""),
            ev.get("at", ""), ev.get("note") or "", next_touch.get(pid, ""),
        ])
    return header, data_rows


def build_shortlist_tab(
    conn: sqlite3.Connection,
    shortlist_min_score: int = DEFAULT_SHORTLIST_MIN_SCORE,
    shortlist_limit: int = DEFAULT_SHORTLIST_LIMIT,
) -> Payload:
    """Active, not-yet-qualified postings (no funnel row, or still sitting at
    'harvested') scoring >= shortlist_min_score, top shortlist_limit by score."""
    header = ["score", "company", "title", "location", "url", "posted_at", "first_seen_at"]
    rubric_version = _current_rubric_version(conn)
    if rubric_version is None:
        return header, []
    rows = conn.execute(
        """SELECT s.total AS score, p.company, p.title, p.location, p.url,
                  p.posted_at, p.first_seen_at
           FROM scores s
           JOIN postings p ON p.id = s.posting_id
           LEFT JOIN funnel f ON f.posting_id = p.id
           WHERE s.rubric_version = ? AND s.excluded = 0 AND p.status = 'active'
             AND s.total >= ? AND (f.posting_id IS NULL OR f.stage = 'harvested')
           ORDER BY s.total DESC, p.id
           LIMIT ?""",
        (rubric_version, shortlist_min_score, shortlist_limit),
    ).fetchall()
    data_rows = [
        [r["score"], r["company"] or "", r["title"] or "", r["location"] or "",
         r["url"] or "", r["posted_at"] or "", r["first_seen_at"] or ""]
        for r in rows
    ]
    return header, data_rows


def build_applications_tab(conn: sqlite3.Connection) -> Payload:
    """Applications joined to their posting and current funnel stage, newest
    submission first. Follow-up columns come from funnel.next_action, the
    same field `report`/`today` use for due next actions."""
    header = ["company", "title", "stage", "submitted_at", "url", "follow_up_action", "follow_up_due_at"]
    rows = conn.execute(
        """SELECT p.company, p.title, p.url, a.submitted_at,
                  f.stage, f.next_action, f.next_action_date
           FROM applications a
           JOIN postings p ON p.id = a.posting_id
           LEFT JOIN funnel f ON f.posting_id = a.posting_id
           ORDER BY a.submitted_at DESC"""
    ).fetchall()
    data_rows = [
        [r["company"] or "", r["title"] or "", r["stage"] or "", r["submitted_at"] or "",
         r["url"] or "", r["next_action"] or "", r["next_action_date"] or ""]
        for r in rows
    ]
    return header, data_rows


def build_companies_tab(conn: sqlite3.Connection) -> Payload:
    """One row per company: platform/slug/verified/watchlist plus its active
    posting count and best score among its postings, best score first."""
    header = ["company", "platform", "slug", "verified", "watchlist", "active_posting_count", "best_score"]
    rubric_version = _current_rubric_version(conn)
    rows = conn.execute(
        """SELECT c.name, c.platform, c.slug, c.verified, c.watchlist,
                  COUNT(DISTINCT CASE WHEN p.status='active' THEN p.id END) active_posting_count,
                  MAX(CASE WHEN s.rubric_version=? THEN s.total END) best_score
           FROM companies c
           LEFT JOIN postings p ON p.company = c.name
           LEFT JOIN scores s ON s.posting_id = p.id AND s.excluded = 0
           GROUP BY c.id
           ORDER BY best_score IS NULL, best_score DESC, c.name""",
        (rubric_version,),
    ).fetchall()
    data_rows = [
        [r["name"], r["platform"] or "", r["slug"] or "", r["verified"] or "",
         "yes" if r["watchlist"] else "no", r["active_posting_count"],
         r["best_score"] if r["best_score"] is not None else ""]
        for r in rows
    ]
    return header, data_rows


def build_health_tab(conn: sqlite3.Connection) -> Payload:
    """Per-source last-sweep freshness (source_state + latest sweeps row),
    plus one row per scheduled job from run_log."""
    header = [
        "source", "last_success_at", "consecutive_failures", "last_error_at",
        "last_error", "last_sweep_postings_seen", "last_sweep_postings_new",
    ]
    source_rows = conn.execute(
        "SELECT source, last_success_at, consecutive_failures, last_error_at, last_error "
        "FROM source_state ORDER BY source"
    ).fetchall()
    data_rows = []
    for r in source_rows:
        sweep = conn.execute(
            """SELECT postings_seen, postings_new FROM sweeps
               WHERE source=? AND finished_at IS NOT NULL
               ORDER BY finished_at DESC LIMIT 1""",
            (r["source"],),
        ).fetchone()
        data_rows.append([
            r["source"], r["last_success_at"] or "", r["consecutive_failures"],
            r["last_error_at"] or "", r["last_error"] or "",
            sweep["postings_seen"] if sweep else "", sweep["postings_new"] if sweep else "",
        ])

    job_rows = conn.execute(
        """SELECT job,
                  MAX(CASE WHEN ok=1 THEN finished_at END) last_success_at,
                  MAX(CASE WHEN ok=0 THEN summary END) last_error
           FROM run_log GROUP BY job ORDER BY job"""
    ).fetchall()
    for r in job_rows:
        data_rows.append([f"job:{r['job']}", r["last_success_at"] or "", "", "", r["last_error"] or "", "", ""])

    return header, data_rows


def build_finai_tab(conn: sqlite3.Connection) -> Payload:
    """One row per posting carrying a finai match report (the latest one), best
    city tier first, then match_after (falling back to match_before) desc."""
    header = [
        "posting_id", "company", "title", "location", "city_tier", "match_before",
        "match_after", "keyword_match", "skills_overlap", "experience_proof",
        "education_fit", "role_alignment", "missing_keywords", "red_flags",
        "tailored_resume", "funnel_stage", "scored_at", "url",
    ]
    rows = conn.execute(
        """SELECT a.posting_id, a.content, a.created_at, p.company, p.title, p.location, p.url,
                  f.stage,
                  (SELECT r.content FROM artifacts r WHERE r.posting_id = a.posting_id
                   AND r.kind = 'resume_pdf' ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS pdf
           FROM artifacts a
           JOIN postings p ON p.id = a.posting_id
           LEFT JOIN funnel f ON f.posting_id = a.posting_id
           WHERE a.kind = ?
           ORDER BY a.created_at, a.id""",
        (REPORT_KIND,),
    ).fetchall()
    latest = {r["posting_id"]: r for r in rows}  # later rows overwrite earlier ones

    entries = []
    for r in latest.values():
        try:
            rep = json.loads(r["content"])
        except json.JSONDecodeError:
            continue
        before, after = rep.get("match_before"), rep.get("match_after")
        dims = rep.get("dimensions") or {}
        tier = city_tier(r["location"])
        entries.append((
            tier if tier is not None else 99,
            -(after if after is not None else (before or 0)),
            [
                r["posting_id"], r["company"] or "", r["title"] or "", r["location"] or "",
                tier if tier is not None else "", before, after if after is not None else "",
                dims.get("keyword_match"), dims.get("skills_overlap"),
                dims.get("experience_proof"), dims.get("education_fit"),
                dims.get("role_alignment"), "; ".join(rep.get("missing_keywords") or []),
                "; ".join(rep.get("red_flags") or []), r["pdf"] or rep.get("resume_pdf") or "",
                r["stage"] or "", r["created_at"] or "", r["url"] or "",
            ],
        ))
    entries.sort(key=lambda e: (e[0], e[1]))
    return header, [e[2] for e in entries]


def _build_all_payloads(conn: sqlite3.Connection, config: SheetsConfig) -> dict[str, Payload]:
    return {
        "Pipeline": build_pipeline_tab(conn),
        "Shortlist": build_shortlist_tab(conn, config.shortlist_min_score, config.shortlist_limit),
        "Applications": build_applications_tab(conn),
        "Companies": build_companies_tab(conn),
        "FinAI Matches": build_finai_tab(conn),
        "Health": build_health_tab(conn),
    }


def push(payloads: dict[str, Payload], config: SheetsConfig) -> str:
    """Clear and rewrite each worksheet tab from its payload. One `update()`
    call per tab. Creates a tab if it doesn't exist yet. Returns the
    spreadsheet URL. Imports gspread lazily — never touched by dry-run."""
    import gspread

    client = gspread.service_account(filename=config.service_account_json)
    spreadsheet = client.open_by_key(config.spreadsheet_id)

    for tab_name, (header, data_rows) in payloads.items():
        try:
            worksheet = spreadsheet.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=tab_name, rows=1, cols=max(len(header), 1))

        worksheet.clear()
        values = [header] + data_rows
        worksheet.resize(rows=max(len(values), 1), cols=max(len(header), 1))
        worksheet.update(values=values)

    return spreadsheet.url


def run_sync(conn: sqlite3.Connection, dry_run: bool = False) -> None:
    """Build every tab's payload and either print a dry-run preview or push
    to the configured Google Sheet. dry_run never imports gspread; if
    data/sheets.local.toml is also absent, shortlist thresholds fall back to
    the module defaults since no push will happen."""
    try:
        config = load_config()
    except SheetsConfigError:
        if not dry_run:
            raise
        config = SheetsConfig(spreadsheet_id="", service_account_json="")

    payloads = _build_all_payloads(conn, config)

    for tab_name, (_header, data_rows) in payloads.items():
        print(f"{tab_name}: {len(data_rows)} rows")
        if dry_run:
            for row in data_rows[:3]:
                print(f"  {row}")

    if dry_run:
        return

    url = push(payloads, config)
    print(f"Synced to {url}")
