"""`run_score_sweep` commits per batch (COMMIT_BATCH) instead of once at the end.

The single end-of-sweep commit held SQLite's write lock for the whole run (it
blocked `make daily` for hours on 2026-10-23). These tests pin the contract:
batching changes only *when* rows become visible, never *which* rows or what
the sweep reports, and a sweep that dies mid-way keeps its committed batches
and resumes cleanly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jobhunt import db, score_sweep
from jobhunt.rubric import load_rubric

# (title, location): a mix of scored, hard-excluded (not_target_role), and
# eligible/review/blocked assessments, so the equality checks are not vacuous.
POSTINGS = [
    ("Software Engineer", "Remote - US"),
    ("Backend Engineer", "Atlanta, GA"),
    ("Senior Staff Engineer", "Remote - US"),
    ("Account Executive", "New York, NY"),
    ("Machine Learning Engineer", "San Francisco, CA"),
    ("Data Analyst", "Remote - US"),
    ("Product Engineer", "Atlanta, GA"),
    ("Sales Development Representative", "Remote - US"),
    ("Forward Deployed Engineer", "Remote - US"),
]


def _seed(conn: sqlite3.Connection) -> list[int]:
    ids = []
    for i, (title, location) in enumerate(POSTINGS):
        conn.execute(
            """INSERT INTO postings
               (source, source_posting_id, company, title, location, status,
                first_seen_at, last_seen_at)
               VALUES ('greenhouse', ?, 'Acme', ?, ?, 'active', '', '')""",
            (f"p{i}", title, location),
        )
        ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    # A closed posting must never be swept, whatever the batch size.
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, location, status,
            first_seen_at, last_seen_at)
           VALUES ('greenhouse', 'closed', 'Acme', 'Software Engineer', 'Remote - US',
                   'closed', '', '')"""
    )
    conn.commit()
    return ids


def _stable_rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
    """Every column except timestamps, ordered by posting."""
    columns = [
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})")
        if not row["name"].endswith("_at") and row["name"] != "id"
    ]
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} ORDER BY posting_id, rubric_version"
    ).fetchall()
    return [tuple(row) for row in rows]


def _committed_counts(path: Path) -> tuple[int, int]:
    """Row counts as a *different* connection sees them (committed data only)."""
    reader = sqlite3.connect(str(path))
    try:
        scores = reader.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
        assessments = reader.execute("SELECT COUNT(*) FROM eligibility_assessments").fetchone()[0]
        return scores, assessments
    finally:
        reader.close()


@pytest.fixture
def rubric():
    return load_rubric()


def test_default_commit_batch_is_a_positive_bounded_batch():
    assert isinstance(score_sweep.COMMIT_BATCH, int)
    assert 1 <= score_sweep.COMMIT_BATCH <= 500


@pytest.mark.parametrize("batch", [1, 2, 4, len(POSTINGS), 1000])
def test_results_are_identical_whatever_the_batch_size(tmp_path, monkeypatch, rubric, batch):
    baseline_conn = db.connect(tmp_path / "baseline.db")
    conn = db.connect(tmp_path / "batched.db")
    try:
        _seed(baseline_conn)
        _seed(conn)
        monkeypatch.setattr(score_sweep, "COMMIT_BATCH", 10_000)  # effectively one commit
        expected = score_sweep.run_score_sweep(baseline_conn, rubric)

        monkeypatch.setattr(score_sweep, "COMMIT_BATCH", batch)
        actual = score_sweep.run_score_sweep(conn, rubric)

        assert actual == expected
        assert actual.scored + actual.excluded == len(POSTINGS)
        assert actual.scored > 0 and actual.excluded > 0
        assert _stable_rows(conn, "scores") == _stable_rows(baseline_conn, "scores")
        assert _stable_rows(conn, "eligibility_assessments") == _stable_rows(
            baseline_conn, "eligibility_assessments"
        )
        assert len(_stable_rows(conn, "scores")) == len(POSTINGS)
        assert len(_stable_rows(conn, "eligibility_assessments")) == len(POSTINGS)
    finally:
        conn.close()
        baseline_conn.close()


def test_the_final_partial_batch_is_committed(tmp_path, monkeypatch, rubric):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    try:
        _seed(conn)
        monkeypatch.setattr(score_sweep, "COMMIT_BATCH", 4)  # 9 postings = 4 + 4 + 1

        score_sweep.run_score_sweep(conn, rubric)

        # Seen from another connection while `conn` is still open: it was committed.
        assert _committed_counts(path) == (len(POSTINGS), len(POSTINGS))
    finally:
        conn.close()


def test_each_batch_is_committed_before_the_next_one_starts(tmp_path, monkeypatch, rubric):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    try:
        _seed(conn)
        monkeypatch.setattr(score_sweep, "COMMIT_BATCH", 2)
        seen: list[tuple[int, int]] = []
        original = score_sweep.upsert_assessment

        def spy(*args, **kwargs):
            # Called once per posting, after that posting's score upsert and
            # before its assessment upsert. `seen[i]` is what other
            # connections can read while posting i+1 is being processed.
            seen.append(_committed_counts(path))
            return original(*args, **kwargs)

        monkeypatch.setattr(score_sweep, "upsert_assessment", spy)

        score_sweep.run_score_sweep(conn, rubric)

        assert len(seen) == len(POSTINGS)
        # Posting i (0-based) is processed after floor(i / 2) full batches committed.
        assert seen == [((i // 2) * 2, (i // 2) * 2) for i in range(len(POSTINGS))]
        assert _committed_counts(path) == (len(POSTINGS), len(POSTINGS))
    finally:
        conn.close()


def test_the_write_lock_is_released_between_batches(tmp_path, monkeypatch, rubric):
    """The point of the change: another writer can get in between batches."""
    path = tmp_path / "t.db"
    conn = db.connect(path)
    try:
        _seed(conn)
        monkeypatch.setattr(score_sweep, "COMMIT_BATCH", 3)
        lock_free: list[bool] = []
        original = score_sweep.upsert_score

        def spy(*args, **kwargs):
            # Right before each posting's first write: can a second writer start?
            lock_free.append(_can_write(path))
            return original(*args, **kwargs)

        monkeypatch.setattr(score_sweep, "upsert_score", spy)

        score_sweep.run_score_sweep(conn, rubric)

        # Free at the start and right after each committed batch of 3; held in between.
        assert lock_free == [i % 3 == 0 for i in range(len(POSTINGS))]
    finally:
        conn.close()


def _can_write(path: Path) -> bool:
    other = sqlite3.connect(str(path), timeout=0)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        other.close()


def test_a_sweep_that_dies_midway_keeps_its_committed_batches_and_resumes(
    tmp_path, monkeypatch, rubric
):
    path = tmp_path / "t.db"
    baseline_conn = db.connect(tmp_path / "baseline.db")
    conn = db.connect(path)
    try:
        _seed(baseline_conn)
        _seed(conn)
        expected = score_sweep.run_score_sweep(baseline_conn, rubric)

        monkeypatch.setattr(score_sweep, "COMMIT_BATCH", 2)
        original = score_sweep.upsert_assessment
        calls = {"n": 0}

        def dying(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 5:  # postings 1-4 are done; 1-4 = two full batches
                raise RuntimeError("process killed mid-sweep")
            return original(*args, **kwargs)

        monkeypatch.setattr(score_sweep, "upsert_assessment", dying)

        with pytest.raises(RuntimeError, match="killed"):
            score_sweep.run_score_sweep(conn, rubric)

        # Only the two committed batches survive; posting 5's open transaction is not visible.
        assert _committed_counts(path) == (4, 4)

        # The next run resumes: upserts are idempotent, so it just finishes the job.
        monkeypatch.setattr(score_sweep, "upsert_assessment", original)
        conn.rollback()
        resumed = score_sweep.run_score_sweep(conn, rubric)

        assert resumed == expected
        assert _committed_counts(path) == (len(POSTINGS), len(POSTINGS))
        assert _stable_rows(conn, "scores") == _stable_rows(baseline_conn, "scores")
        assert _stable_rows(conn, "eligibility_assessments") == _stable_rows(
            baseline_conn, "eligibility_assessments"
        )
    finally:
        conn.close()
        baseline_conn.close()


def test_limit_still_bounds_the_sweep_and_commits(tmp_path, monkeypatch, rubric):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    try:
        _seed(conn)
        monkeypatch.setattr(score_sweep, "COMMIT_BATCH", 2)

        result = score_sweep.run_score_sweep(conn, rubric, limit=5)

        assert result.scored + result.excluded == 5
        assert _committed_counts(path) == (5, 5)
    finally:
        conn.close()


def test_an_empty_sweep_commits_nothing_and_reports_nothing(tmp_path, rubric):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    try:
        result = score_sweep.run_score_sweep(conn, rubric)

        assert result == score_sweep.SweepResult()
        assert _committed_counts(path) == (0, 0)
    finally:
        conn.close()
