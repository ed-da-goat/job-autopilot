"""Auto-suggest engine: postings scoring at or above a threshold at the
latest rubric version get moved into the 'suggested' funnel side-stage for
review on the board (accept -> qualified, dismiss -> passed).

Two call shapes:
  - daily-run path: `new_posting_ids` given -> every matching posting from
    that set is suggested, no cap (a fresh sweep is small by construction),
    then a straggler sweep suggests up to `limit` qualifying postings from
    OUTSIDE that set. Stragglers exist whenever postings enter the DB
    without passing through a daily suggest step (manual `jobhunt harvest`
    + `score`, an ingester, a daily run that died between harvest and
    suggest); they are never in a later run's new-id set, so without this
    sweep they stay invisible forever (the 2026-08-05 Taktile gap: 45
    postings, top score 81, zero funnel rows).
  - backfill path: `new_posting_ids` omitted -> top `limit` by score across
    the whole DB, so a first run doesn't flood the board.

A posting is never re-suggested once it has ANY funnel row, including
'passed' — a dismissed suggestion stays dismissed.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from jobhunt.eligibility import load_preferences
from jobhunt.funnel import move

DEFAULT_THRESHOLD = 65
DEFAULT_LIMIT = 20
FALLBACK_FILL_QUOTA = 12
DISCRIMINATOR_CATEGORIES = {"fintech", "fde-solutions", "ai-infra"}


def run_suggest(
    conn: sqlite3.Connection,
    new_posting_ids: list[int] | None = None,
    threshold: int = DEFAULT_THRESHOLD,
    limit: int = DEFAULT_LIMIT,
) -> list[int]:
    """Move qualifying postings into 'suggested'. Returns the list of
    posting ids suggested by this call (empty list if nothing qualifies or
    no rubric has been scored yet)."""
    rubric_version = conn.execute("SELECT MAX(rubric_version) v FROM scores").fetchone()["v"]
    if rubric_version is None:
        return []
    preferences = load_preferences()

    suggested_ids: list[int] = []
    if new_posting_ids:
        # Chunked: a surge harvest can hand us tens of thousands of ids, and
        # one IN(...) placeholder per id blows SQLite's variable limit
        # (2026-08-06 daily crash: 33K workday postings in one clause).
        for start in range(0, len(new_posting_ids), 500):
            suggested_ids += _suggest_pass(
                conn,
                preferences.version,
                rubric_version,
                threshold,
                posting_ids=new_posting_ids[start : start + 500],
            )
    # Straggler/backfill sweep, capped so a large pool can't flood the
    # board. The pass above already put this run's new postings in the
    # funnel, so the NOT-IN-funnel clause makes this pass pick up only
    # postings that entered the DB outside a daily run.
    suggested_ids += _suggest_pass(
        conn, preferences.version, rubric_version, threshold, cap=limit
    )
    return suggested_ids


def _suggest_pass(
    conn: sqlite3.Connection,
    preferences_version: int,
    rubric_version: int,
    threshold: int,
    posting_ids: list[int] | None = None,
    cap: int | None = None,
) -> list[int]:
    params: list = [preferences_version, rubric_version, threshold]
    id_filter_sql = ""
    if posting_ids is not None:
        placeholders = ",".join("?" for _ in posting_ids)
        id_filter_sql = f" AND s.posting_id IN ({placeholders})"
        params.extend(posting_ids)

    limit_sql = ""
    if cap is not None:
        limit_sql = " LIMIT ?"
        params.append(cap)

    rows = conn.execute(
        f"""
        SELECT s.posting_id, s.total
        FROM scores s
        JOIN postings p ON p.id = s.posting_id
        JOIN eligibility_assessments e
          ON e.posting_id=s.posting_id AND e.rubric_version=s.rubric_version
         AND e.preferences_version=?
        WHERE s.rubric_version = ? AND s.total >= ? AND s.excluded = 0
          AND e.status='eligible' AND p.status = 'active'
          AND s.posting_id NOT IN (SELECT posting_id FROM funnel)
          {id_filter_sql}
        ORDER BY s.total DESC, s.posting_id
        {limit_sql}
        """,
        params,
    ).fetchall()

    suggested_ids: list[int] = []
    for row in rows:
        posting_id = row["posting_id"]
        move(conn, posting_id, "suggested", note=f"auto-suggested (score {row['total']})")
        suggested_ids.append(posting_id)
    return suggested_ids


def run_weekly_fallback_fill(conn: sqlite3.Connection, quota: int = FALLBACK_FILL_QUOTA) -> list[int]:
    """Top up this week's tier-1 (score >= 70) suggestion supply toward
    `quota` by pulling the best 50-69 postings that DON'T match the
    tier-1 discriminator (non-FDE roles score 0 on founder_leverage and are
    structurally capped around 55-62 — those are worth pulling; 50-69
    postings that DO match the discriminator already had their shot at
    tier-1 and fell short on substance, so they're excluded here)."""
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    rubric_version = conn.execute("SELECT MAX(rubric_version) v FROM scores").fetchone()["v"]
    if rubric_version is None:
        return []

    tier1_count = conn.execute(
        """
        SELECT COUNT(DISTINCT fe.posting_id) c
        FROM funnel_events fe
        JOIN scores s ON s.posting_id = fe.posting_id AND s.rubric_version = ?
        WHERE fe.to_stage IN ('suggested','ready') AND fe.at >= ?
          AND s.total >= 70 AND s.excluded = 0
        """,
        (rubric_version, cutoff),
    ).fetchone()["c"]

    gap = quota - tier1_count
    if gap <= 0:
        return []

    preferences = load_preferences()
    category_placeholders = ",".join("?" for _ in DISCRIMINATOR_CATEGORIES)

    rows = conn.execute(
        f"""
        SELECT s.posting_id, s.total
        FROM scores s
        JOIN postings p ON p.id = s.posting_id
        JOIN eligibility_assessments e
          ON e.posting_id=s.posting_id AND e.rubric_version=s.rubric_version
         AND e.preferences_version=?
        LEFT JOIN companies c ON c.name = p.company
        WHERE s.rubric_version = ? AND s.total >= 50 AND s.total < 70 AND s.excluded = 0
          AND e.status='eligible' AND p.status = 'active'
          AND s.posting_id NOT IN (SELECT posting_id FROM funnel)
          AND COALESCE(json_extract(s.components, '$.founder_leverage'), 0) < 6
          AND (c.category IS NULL OR c.category NOT IN ({category_placeholders}))
        ORDER BY s.total DESC, s.posting_id
        LIMIT ?
        """,
        [preferences.version, rubric_version, *DISCRIMINATOR_CATEGORIES, gap],
    ).fetchall()

    filled_ids: list[int] = []
    for row in rows:
        posting_id = row["posting_id"]
        move(conn, posting_id, "suggested", note=f"fallback-fill (score {row['total']})")
        filled_ids.append(posting_id)
    return filled_ids
