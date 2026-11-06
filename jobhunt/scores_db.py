"""Upsert primitives for the scores table, mirroring postings.py's shape."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime


def upsert_score(conn: sqlite3.Connection, posting_id: int, rubric_version: int, result: dict) -> None:
    """Insert or refresh the score row for (posting_id, rubric_version).
    A rescore under the same rubric version overwrites; bumping the rubric
    version writes a fresh row instead of clobbering history."""
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO scores
            (posting_id, rubric_version, total, components, score_evidence,
             missing_evidence, excluded, exclude_reason, scored_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(posting_id, rubric_version) DO UPDATE SET
            total=excluded.total,
            components=excluded.components,
            score_evidence=excluded.score_evidence,
            missing_evidence=excluded.missing_evidence,
            excluded=excluded.excluded,
            exclude_reason=excluded.exclude_reason,
            scored_at=excluded.scored_at
        """,
        (
            posting_id,
            rubric_version,
            result["total"],
            json.dumps(result["components"]),
            json.dumps(result.get("score_evidence", {})),
            json.dumps(result["missing_evidence"]),
            1 if result["excluded"] else 0,
            result["exclude_reason"],
            now,
        ),
    )


def fetch_score_rows(
    conn: sqlite3.Connection,
    rubric_version: int,
    *,
    exclude_blocked_preferences_version: int | None = None,
) -> list[sqlite3.Row]:
    """All scored (non-excluded) postings for a rubric version, newest scoring
    join with posting fields needed for display; optionally excludes postings
    blocked under the given eligibility preferences version."""
    query = """
        SELECT s.*, p.company, p.title, p.location, p.url, p.source, p.raw_json, p.status
        FROM scores s
        JOIN postings p ON p.id = s.posting_id
        WHERE s.rubric_version = ? AND s.excluded = 0 AND p.status = 'active'
        """
    params: list[int] = [rubric_version]
    if exclude_blocked_preferences_version is not None:
        query += """
        AND NOT EXISTS (
            SELECT 1 FROM eligibility_assessments e
            WHERE e.posting_id = s.posting_id
              AND e.rubric_version = s.rubric_version
              AND e.preferences_version = ?
              AND e.status = 'blocked'
        )
        """
        params.append(exclude_blocked_preferences_version)
    query += "ORDER BY s.posting_id"
    return conn.execute(query, params).fetchall()
