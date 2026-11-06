"""Read-only SELECTs the web layer needs beyond funnel.py/report.py's exported
functions. No funnel state semantics live here — moves/qualifies/field-sets
all go through jobhunt.funnel; this module only reads.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from jobhunt.funnel import STAGES
from jobhunt.eligibility import load_preferences
from jobhunt.prep import KINDS as DRAFT_KINDS
from jobhunt.text_extract import extract_text

BOARD_STAGES = [s for s in STAGES if s != "harvested"]


def latest_drafts(conn: sqlite3.Connection, posting_id: int) -> dict[str, sqlite3.Row | None]:
    """Most recent draft row per kind for a posting (None if never generated).
    Regenerate always inserts a fresh row, so this is what the detail page
    shows — edited=1 rows included, since an edited draft is still "the"
    draft for that kind until it's regenerated."""
    result: dict[str, sqlite3.Row | None] = {}
    for kind in DRAFT_KINDS:
        result[kind] = conn.execute(
            "SELECT * FROM drafts WHERE posting_id=? AND kind=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (posting_id, kind),
        ).fetchone()
    return result


def latest_rubric_version(conn: sqlite3.Connection) -> int | None:
    return conn.execute("SELECT MAX(rubric_version) v FROM scores").fetchone()["v"]


def _days_since(iso_str: str | None) -> int | None:
    if not iso_str:
        return None
    try:
        then = datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - then).days)


def board_columns(
    conn: sqlite3.Connection, rubric_version: int | None
) -> dict[str, list[dict]]:
    """Funnel rows grouped by stage (main STAGES + collapsed passed/closed),
    each annotated with company/title/score and days-in-current-stage."""
    stages = [*BOARD_STAGES, "passed", "closed"]
    placeholders = ",".join("?" for _ in stages)
    rows = conn.execute(
        f"""
        SELECT f.posting_id, f.stage, f.tier, f.next_action, f.created_at, f.updated_at,
               p.company, p.title, p.url, p.status posting_status, s.total
        FROM funnel f
        JOIN postings p ON p.id = f.posting_id
        LEFT JOIN scores s ON s.posting_id = f.posting_id AND s.rubric_version = ?
        WHERE f.stage IN ({placeholders})
        ORDER BY f.updated_at DESC
        """,
        [rubric_version, *stages],
    ).fetchall()

    entered_at: dict[tuple[int, str], str] = {}
    for e in conn.execute(
        "SELECT posting_id, to_stage, MAX(at) at FROM funnel_events GROUP BY posting_id, to_stage"
    ):
        entered_at[(e["posting_id"], e["to_stage"])] = e["at"]

    columns: dict[str, list[dict]] = {stage: [] for stage in stages}
    for row in rows:
        stamp = entered_at.get((row["posting_id"], row["stage"]), row["created_at"])
        columns[row["stage"]].append(
            {
                "posting_id": row["posting_id"],
                "company": row["company"],
                "title": row["title"],
                "url": row["url"],
                "posting_status": row["posting_status"],
                "total": row["total"],
                "tier": row["tier"],
                "next_action": row["next_action"],
                "days_in_stage": _days_since(stamp),
            }
        )
    return columns


def suggested_rows(conn: sqlite3.Connection, rubric_version: int | None) -> list[dict]:
    """Funnel rows currently sitting in 'suggested', annotated with score and
    the auto-suggest note, newest first. Backs the board's Suggested panel."""
    rows = conn.execute(
        """
        SELECT f.posting_id, f.updated_at, p.company, p.title, p.url, s.total
        FROM funnel f
        JOIN postings p ON p.id = f.posting_id
        LEFT JOIN scores s ON s.posting_id = f.posting_id AND s.rubric_version = ?
        LEFT JOIN eligibility_assessments e
          ON e.posting_id=f.posting_id AND e.rubric_version=s.rubric_version
         AND e.preferences_version=?
        WHERE f.stage = 'suggested'
          AND e.status='eligible' AND p.status='active'
        ORDER BY f.updated_at DESC
        """,
        (rubric_version, load_preferences().version),
    ).fetchall()

    notes: dict[int, str] = {}
    for e in conn.execute(
        "SELECT posting_id, note FROM funnel_events WHERE to_stage='suggested' ORDER BY at"
    ):
        if e["note"]:
            notes[e["posting_id"]] = e["note"]

    return [
        {
            "posting_id": r["posting_id"],
            "company": r["company"],
            "title": r["title"],
            "url": r["url"],
            "total": r["total"],
            "note": notes.get(r["posting_id"], ""),
        }
        for r in rows
    ]


def review_rows(conn: sqlite3.Connection, rubric_version: int | None, min_score: int = 55) -> list[dict]:
    preferences = load_preferences()
    rows = conn.execute(
        """SELECT p.id posting_id, p.company, p.title, p.location, p.url, s.total,
                  e.warnings, e.evidence
           FROM postings p
           JOIN scores s ON s.posting_id=p.id AND s.rubric_version=?
           JOIN eligibility_assessments e
             ON e.posting_id=p.id AND e.rubric_version=s.rubric_version
            AND e.preferences_version=?
           LEFT JOIN funnel f ON f.posting_id=p.id
           WHERE p.status='active' AND s.excluded=0 AND s.total>=?
             AND e.status='review' AND f.posting_id IS NULL
           ORDER BY s.total DESC, p.id LIMIT 30""",
        (rubric_version, preferences.version, min_score),
    ).fetchall()
    return [dict(row) for row in rows]


def postings_browse(
    conn: sqlite3.Connection,
    rubric_version: int | None,
    *,
    min_score: int | None = None,
    max_score: int | None = None,
    source: str | None = None,
    status: str | None = None,
    in_funnel: bool | None = None,
    eligible: bool | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """Filtered, paginated browse over scored postings, newest score first.
    Returns (rows, total_count)."""
    where = ["1=1"]
    params: list = [rubric_version, load_preferences().version]
    if min_score is not None:
        where.append("s.total >= ?")
        params.append(min_score)
    if max_score is not None:
        where.append("s.total <= ?")
        params.append(max_score)
    if source:
        where.append("p.source = ?")
        params.append(source)
    if status:
        where.append("p.status = ?")
        params.append(status)
    if in_funnel is True:
        where.append("f.posting_id IS NOT NULL")
    elif in_funnel is False:
        where.append("f.posting_id IS NULL")
    if eligible is True:
        where.append("e.status = 'eligible'")
    if search:
        where.append("(p.company LIKE ? OR p.title LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])

    where_sql = " AND ".join(where)
    offset = max(0, (page - 1) * page_size)
    join_sql = """
        LEFT JOIN scores s ON s.posting_id = p.id AND s.rubric_version = ?
        LEFT JOIN funnel f ON f.posting_id = p.id
        LEFT JOIN eligibility_assessments e
          ON e.posting_id = p.id AND e.rubric_version = s.rubric_version
         AND e.preferences_version = ?
    """
    total = conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM postings p
        {join_sql}
        WHERE {where_sql}
        """,
        params,
    ).fetchone()["c"]
    rows = conn.execute(
        f"""
        SELECT p.id, p.company, p.title, p.location, p.source, p.status, p.url,
               s.total, f.stage, e.status AS eligibility_status
        FROM postings p
        {join_sql}
        WHERE {where_sql}
        ORDER BY (s.total IS NULL), s.total DESC, p.id
        LIMIT ? OFFSET ?
        """,
        [*params, page_size, offset],
    ).fetchall()

    return [dict(r) for r in rows], total


def posting_detail(conn: sqlite3.Connection, posting_id: int) -> dict | None:
    """Everything the detail page needs: posting, latest score, funnel state,
    event history. None if the posting doesn't exist."""
    posting = conn.execute(
        "SELECT * FROM postings WHERE id=?", (posting_id,)
    ).fetchone()
    if posting is None:
        return None

    rubric_version = latest_rubric_version(conn)
    score = None
    if rubric_version is not None:
        score = conn.execute(
            "SELECT * FROM scores WHERE posting_id=? AND rubric_version=?",
            (posting_id, rubric_version),
        ).fetchone()
    funnel_row = conn.execute(
        "SELECT * FROM funnel WHERE posting_id=?", (posting_id,)
    ).fetchone()
    events = conn.execute(
        "SELECT * FROM funnel_events WHERE posting_id=? ORDER BY at DESC", (posting_id,)
    ).fetchall()
    preferences = load_preferences()
    eligibility = conn.execute(
        """SELECT * FROM eligibility_assessments
           WHERE posting_id=? AND preferences_version=?
           ORDER BY rubric_version DESC LIMIT 1""",
        (posting_id, preferences.version),
    ).fetchone()
    _title, description, _ = extract_text(posting)
    duplicates = conn.execute(
        """SELECT id, location, source, url FROM postings
           WHERE status='active' AND lower(trim(company))=lower(trim(?))
             AND lower(trim(title))=lower(trim(?))
           ORDER BY id""",
        (posting["company"] or "", posting["title"] or ""),
    ).fetchall()
    return {
        "posting": posting,
        "score": score,
        "rubric_version": rubric_version,
        "funnel": funnel_row,
        "events": events,
        "drafts": latest_drafts(conn, posting_id),
        "eligibility": eligibility,
        "description": description,
        "posting_age_days": _days_since(posting["posted_at"] or posting["first_seen_at"]),
        "duplicate_group": duplicates,
    }


def company_extra_roles(
    conn: sqlite3.Connection,
    rubric_version: int,
    company: str,
    exclude_posting_id: int,
    best_total: int,
) -> list[sqlite3.Row]:
    """Other in-range (within 15pts of best_total) postings at `company`,
    excluding the one already shown — backs the shortlist's '+N more roles'."""
    return conn.execute(
        """
        SELECT s.posting_id, s.total, p.title, p.location, p.url
        FROM scores s
        JOIN postings p ON p.id = s.posting_id
        WHERE s.rubric_version = ? AND s.excluded = 0 AND p.company = ?
          AND p.status = 'active' AND s.posting_id != ? AND s.total >= ?
        ORDER BY s.total DESC, s.posting_id
        """,
        (rubric_version, company, exclude_posting_id, best_total - 15),
    ).fetchall()


def funneled_posting_ids(conn: sqlite3.Connection) -> set[int]:
    return {
        r["posting_id"]
        for r in conn.execute("SELECT posting_id FROM funnel").fetchall()
    }


def passed_or_closed_ids(conn: sqlite3.Connection) -> set[int]:
    """Postings whose funnel stage is 'passed' or 'closed' — excluded from
    the shortlist so a decided-against posting can't hold a top-N slot
    forever."""
    return {
        r["posting_id"]
        for r in conn.execute(
            "SELECT posting_id FROM funnel WHERE stage IN ('passed','closed')"
        ).fetchall()
    }
