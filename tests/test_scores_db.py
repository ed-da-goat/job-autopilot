from __future__ import annotations

import json

from jobhunt import db as db_module
from jobhunt.scores_db import fetch_score_rows, upsert_score


def test_upsert_score_inserts_and_refreshes(tmp_path):
    conn = db_module.connect(tmp_path / "scores.db")
    try:
        conn.execute(
            """INSERT INTO postings
               (source, source_posting_id, company, title, status, first_seen_at, last_seen_at)
               VALUES ('greenhouse', 'p1', 'Acme', 'Backend Engineer', 'active', '', '')"""
        )
        conn.commit()
        posting_id = conn.execute("SELECT id FROM postings WHERE source_posting_id='p1'").fetchone()["id"]

        result_v1 = {
            "total": 42,
            "components": {"degree_fit": 15},
            "missing_evidence": ["no_description"],
            "excluded": False,
            "exclude_reason": None,
        }
        upsert_score(conn, posting_id, 1, result_v1)
        conn.commit()

        row = conn.execute(
            "SELECT * FROM scores WHERE posting_id=? AND rubric_version=1", (posting_id,)
        ).fetchone()
        assert row["total"] == 42
        assert json.loads(row["missing_evidence"]) == ["no_description"]

        # rescoring under the same rubric version refreshes, not duplicates
        result_v1_rescored = {**result_v1, "total": 50}
        upsert_score(conn, posting_id, 1, result_v1_rescored)
        conn.commit()
        rows = conn.execute(
            "SELECT total FROM scores WHERE posting_id=? AND rubric_version=1", (posting_id,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["total"] == 50

        # a bumped rubric version writes a new row instead of clobbering history
        upsert_score(conn, posting_id, 2, {**result_v1, "total": 60})
        conn.commit()
        all_rows = conn.execute(
            "SELECT rubric_version, total FROM scores WHERE posting_id=? ORDER BY rubric_version",
            (posting_id,),
        ).fetchall()
        assert [(r["rubric_version"], r["total"]) for r in all_rows] == [(1, 50), (2, 60)]
    finally:
        conn.close()


def _seed_posting(conn, source_posting_id: str, title: str) -> int:
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, status, first_seen_at, last_seen_at)
           VALUES ('greenhouse', ?, 'Acme', ?, 'active', '', '')""",
        (source_posting_id, title),
    )
    return conn.execute(
        "SELECT id FROM postings WHERE source_posting_id=?", (source_posting_id,)
    ).fetchone()["id"]


def _seed_score(conn, posting_id: int, rubric_version: int, total: int) -> None:
    upsert_score(
        conn,
        posting_id,
        rubric_version,
        {
            "total": total,
            "components": {},
            "missing_evidence": [],
            "excluded": False,
            "exclude_reason": None,
        },
    )


def _seed_eligibility(
    conn, posting_id: int, rubric_version: int, preferences_version: int, status: str
) -> None:
    conn.execute(
        """INSERT INTO eligibility_assessments
           (posting_id, rubric_version, preferences_version, status, blockers, warnings,
            evidence, assessed_at)
           VALUES (?, ?, ?, ?, '[]', '[]', '[]', '')""",
        (posting_id, rubric_version, preferences_version, status),
    )


def test_fetch_score_rows_excludes_blocked_when_version_given(tmp_path):
    conn = db_module.connect(tmp_path / "scores.db")
    try:
        blocked_id = _seed_posting(conn, "p-blocked", "Forward Deployed Engineer")
        eligible_id = _seed_posting(conn, "p-eligible", "Backend Engineer")
        unassessed_id = _seed_posting(conn, "p-unassessed", "Platform Engineer")
        _seed_score(conn, blocked_id, 1, 90)
        _seed_score(conn, eligible_id, 1, 50)
        _seed_score(conn, unassessed_id, 1, 40)
        _seed_eligibility(conn, blocked_id, 1, 3, "blocked")
        _seed_eligibility(conn, eligible_id, 1, 3, "eligible")
        conn.commit()

        rows = fetch_score_rows(conn, 1, exclude_blocked_preferences_version=3)
        posting_ids = {row["posting_id"] for row in rows}
        assert posting_ids == {eligible_id, unassessed_id}
        assert blocked_id not in posting_ids
    finally:
        conn.close()


def test_fetch_score_rows_default_includes_blocked(tmp_path):
    conn = db_module.connect(tmp_path / "scores.db")
    try:
        blocked_id = _seed_posting(conn, "p-blocked", "Forward Deployed Engineer")
        eligible_id = _seed_posting(conn, "p-eligible", "Backend Engineer")
        _seed_score(conn, blocked_id, 1, 90)
        _seed_score(conn, eligible_id, 1, 50)
        _seed_eligibility(conn, blocked_id, 1, 3, "blocked")
        _seed_eligibility(conn, eligible_id, 1, 3, "eligible")
        conn.commit()

        rows = fetch_score_rows(conn, 1)
        posting_ids = {row["posting_id"] for row in rows}
        assert posting_ids == {blocked_id, eligible_id}
    finally:
        conn.close()
