"""Posting upsert/close-stale/sweep-logging primitives, shared by harvest.py.

Split out of harvest.py to keep orchestration logic separate from the raw
DB read/write operations it composes.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field


@dataclass
class SourceStats:
    source: str
    boards_attempted: int = 0
    boards_ok: int = 0
    postings_seen: int = 0
    postings_new: int = 0
    errors: list[dict] = field(default_factory=list)


def upsert_posting(conn: sqlite3.Connection, source: str, posting: dict, now: str) -> bool:
    """Insert or refresh a posting. Returns True if it was newly inserted."""
    spid = posting.get("source_posting_id")
    if not spid:
        return False
    existing = conn.execute(
        "SELECT id FROM postings WHERE source=? AND source_posting_id=?",
        (source, spid),
    ).fetchone()
    if existing is None:
        has_baseline = conn.execute(
            "SELECT 1 FROM source_state WHERE source=?", (source,)
        ).fetchone()
        conn.execute(
            """INSERT INTO postings
               (source, source_posting_id, company, title, location, url, posted_at,
                raw_json, first_seen_at, last_seen_at, status, discovered_after_baseline)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (
                source,
                spid,
                posting.get("company"),
                posting.get("title"),
                posting.get("location"),
                posting.get("url"),
                posting.get("posted_at"),
                json.dumps(posting.get("raw"), default=str),
                now,
                now,
                1 if has_baseline else 0,
            ),
        )
        return True
    conn.execute(
        """UPDATE postings SET last_seen_at=?, company=?, title=?, location=?, url=?,
                  posted_at=?, raw_json=?, status='active'
           WHERE source=? AND source_posting_id=?""",
        (
            now,
            posting.get("company"),
            posting.get("title"),
            posting.get("location"),
            posting.get("url"),
            posting.get("posted_at"),
            json.dumps(posting.get("raw"), default=str),
            source,
            spid,
        ),
    )
    return False


def close_stale(
    conn: sqlite3.Connection,
    source: str,
    seen_ids: set[str],
    company: str | None = None,
) -> int:
    """Mark active postings not in seen_ids as closed, scoped to (source, company)
    if company is given, else to the whole source. Returns count closed."""
    if company is not None:
        rows = conn.execute(
            "SELECT source_posting_id FROM postings WHERE source=? AND company=? AND status='active'",
            (source, company),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT source_posting_id FROM postings WHERE source=? AND status='active'",
            (source,),
        ).fetchall()

    stale_ids = [r["source_posting_id"] for r in rows if r["source_posting_id"] not in seen_ids]
    for spid in stale_ids:
        if company is not None:
            conn.execute(
                "UPDATE postings SET status='closed' WHERE source=? AND company=? AND source_posting_id=?",
                (source, company, spid),
            )
        else:
            conn.execute(
                "UPDATE postings SET status='closed' WHERE source=? AND source_posting_id=?",
                (source, spid),
            )
    return len(stale_ids)


def ingest(conn: sqlite3.Connection, source: str, stats: SourceStats, postings: list[dict], now: str) -> set[str]:
    """Upsert a batch of postings, updating stats in place. Returns the set of
    source_posting_ids seen (for close_stale scoping)."""
    seen_ids: set[str] = set()
    for posting in postings:
        spid = posting.get("source_posting_id")
        if not spid:
            continue
        seen_ids.add(spid)
        is_new = upsert_posting(conn, source, posting, now)
        stats.postings_seen += 1
        if is_new:
            stats.postings_new += 1
    return seen_ids


def record_sweep(conn: sqlite3.Connection, source: str, stats: SourceStats, started_at: str, finished_at: str) -> None:
    conn.execute(
        """INSERT INTO sweeps
           (started_at, finished_at, source, boards_attempted, boards_ok,
            postings_seen, postings_new, errors)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            started_at,
            finished_at,
            source,
            stats.boards_attempted,
            stats.boards_ok,
            stats.postings_seen,
            stats.postings_new,
            json.dumps(stats.errors),
        ),
    )
    conn.commit()
