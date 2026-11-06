from __future__ import annotations

import json
import sqlite3

import pytest

from jobhunt import db as db_module
from jobhunt import harvest as harvest_module
from jobhunt.sources import alerts, greenhouse, simplify


def _posting(spid: str, company: str = "Acme Corp") -> dict:
    return {
        "source_posting_id": spid,
        "company": company,
        "title": "Backend Engineer",
        "location": "Remote",
        "url": f"https://example.com/jobs/{spid}",
        "posted_at": "2026-07-10T00:00:00Z",
        "raw": {"id": spid},
    }


def test_board_fetch_releases_writer_between_boards_and_sources(tmp_path, monkeypatch):
    path = tmp_path / "harvest.db"
    conn = db_module.connect(path)
    reviewer = sqlite3.connect(path, timeout=0)
    try:
        for name in ("A", "B", "C"):
            conn.execute("INSERT INTO companies (name, platform, slug, watchlist, added_at) VALUES (?, 'greenhouse', ?, 0, '')", (name, name.lower()))
        conn.commit()
        fetched = []

        def assert_available():
            assert not conn.in_transaction
            reviewer.execute("BEGIN IMMEDIATE")
            reviewer.execute("UPDATE companies SET added_at='reviewer' WHERE name='A'")
            reviewer.commit()

        def fetch(slug, company=None):
            assert_available()
            fetched.append(slug)
            if slug == "b":
                # The preceding board is visible even while this fetch fails.
                assert reviewer.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1
                raise greenhouse.BoardNotFound(slug)
            return [_posting(slug, company=company)]

        def fetch_single():
            assert_available()
            assert reviewer.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 2
            return [_posting("single")]

        monkeypatch.setattr(greenhouse, "fetch", fetch)
        monkeypatch.setattr(alerts, "fetch", fetch_single)
        results = harvest_module.run_harvest(conn, sources=["greenhouse", "alerts"])
        assert fetched == ["a", "b", "c"]
        assert results["greenhouse"].boards_ok == 2
        assert len(results["greenhouse"].errors) == 1
        assert not conn.in_transaction
    finally:
        reviewer.close()
        conn.close()


def test_board_persistence_failure_rolls_back_whole_board(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "harvest.db")
    try:
        conn.execute("INSERT INTO companies (name, platform, slug, watchlist, added_at) VALUES ('Acme Corp', 'greenhouse', 'acme', 0, '')")
        harvest_module.upsert_posting(conn, "greenhouse", _posting("existing"), "2026-07-01")
        conn.commit()
        monkeypatch.setattr(greenhouse, "fetch", lambda *a, **k: [_posting("new"), _posting("broken")])
        real_close = harvest_module.close_stale

        def fail_after_close(*args, **kwargs):
            real_close(*args, **kwargs)
            raise RuntimeError("persistence failure")

        monkeypatch.setattr(harvest_module, "close_stale", fail_after_close)
        with pytest.raises(RuntimeError, match="persistence failure"):
            harvest_module.run_harvest(conn, sources=["greenhouse"])
        assert not conn.in_transaction
        assert [(r["source_posting_id"], r["status"]) for r in conn.execute("SELECT * FROM postings")] == [("existing", "active")]
    finally:
        conn.close()


def test_upsert_posting_is_idempotent(tmp_path):
    conn = db_module.connect(tmp_path / "test.db")
    try:
        posting = _posting("job-1")

        is_new_first = harvest_module.upsert_posting(conn, "greenhouse", posting, "2026-07-10T00:00:00Z")
        is_new_second = harvest_module.upsert_posting(conn, "greenhouse", posting, "2026-07-11T00:00:00Z")
        conn.commit()

        assert is_new_first is True
        assert is_new_second is False

        rows = conn.execute(
            "SELECT last_seen_at, first_seen_at FROM postings WHERE source_posting_id='job-1'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["first_seen_at"] == "2026-07-10T00:00:00Z"
        assert rows[0]["last_seen_at"] == "2026-07-11T00:00:00Z"
    finally:
        conn.close()


def test_upsert_refreshes_mutable_posting_content(tmp_path):
    conn = db_module.connect(tmp_path / "test.db")
    try:
        original = _posting("job-1")
        harvest_module.upsert_posting(conn, "greenhouse", original, "2026-07-10T00:00:00Z")
        changed = {**original, "company": "Acme AI", "title": "AI Engineer", "posted_at": "2026-07-12", "raw": {"content": "new requirements"}}
        harvest_module.upsert_posting(conn, "greenhouse", changed, "2026-07-12T00:00:00Z")
        conn.commit()
        row = conn.execute("SELECT * FROM postings WHERE source_posting_id='job-1'").fetchone()
        assert row["company"] == "Acme AI"
        assert row["title"] == "AI Engineer"
        assert row["posted_at"] == "2026-07-12"
        assert json.loads(row["raw_json"])["content"] == "new requirements"
    finally:
        conn.close()


def test_close_stale_marks_missing_postings_closed(tmp_path):
    conn = db_module.connect(tmp_path / "test.db")
    try:
        harvest_module.upsert_posting(conn, "greenhouse", _posting("job-1"), "2026-07-10T00:00:00Z")
        harvest_module.upsert_posting(conn, "greenhouse", _posting("job-2"), "2026-07-10T00:00:00Z")
        conn.commit()

        # this sweep only saw job-1
        closed_count = harvest_module.close_stale(conn, "greenhouse", {"job-1"}, company="Acme Corp")
        conn.commit()

        assert closed_count == 1
        statuses = {
            row["source_posting_id"]: row["status"]
            for row in conn.execute("SELECT source_posting_id, status FROM postings").fetchall()
        }
        assert statuses["job-1"] == "active"
        assert statuses["job-2"] == "closed"
    finally:
        conn.close()


def test_run_harvest_closes_postings_missing_on_resweep(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "test.db")
    try:
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, watchlist, added_at) "
            "VALUES ('Acme Corp', 'ai-infra', 'greenhouse', 'acme', 0, '2026-07-01T00:00:00Z')"
        )
        conn.commit()

        first_sweep_jobs = [_posting("job-1"), _posting("job-2")]
        monkeypatch.setattr(greenhouse, "fetch", lambda slug, company=None: first_sweep_jobs)
        harvest_module.run_harvest(conn, sources=["greenhouse"])

        active = {
            r["source_posting_id"]
            for r in conn.execute("SELECT source_posting_id FROM postings WHERE status='active'").fetchall()
        }
        assert active == {"job-1", "job-2"}

        # job-2 disappears from the board on the next sweep
        second_sweep_jobs = [_posting("job-1")]
        monkeypatch.setattr(greenhouse, "fetch", lambda slug, company=None: second_sweep_jobs)
        harvest_module.run_harvest(conn, sources=["greenhouse"])

        statuses = {
            row["source_posting_id"]: row["status"]
            for row in conn.execute("SELECT source_posting_id, status FROM postings").fetchall()
        }
        assert statuses["job-1"] == "active"
        assert statuses["job-2"] == "closed"

        sweep_rows = conn.execute("SELECT * FROM sweeps WHERE source='greenhouse'").fetchall()
        assert len(sweep_rows) == 2
    finally:
        conn.close()


def test_run_harvest_never_closes_postings_from_a_failed_board(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "test.db")
    try:
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, watchlist, added_at) "
            "VALUES ('Acme Corp', 'ai-infra', 'greenhouse', 'acme', 0, '2026-07-01T00:00:00Z')"
        )
        conn.commit()

        monkeypatch.setattr(greenhouse, "fetch", lambda slug, company=None: [_posting("job-1")])
        harvest_module.run_harvest(conn, sources=["greenhouse"])

        def _boom(slug, company=None):
            raise greenhouse.BoardNotFound(slug)

        monkeypatch.setattr(greenhouse, "fetch", _boom)
        harvest_module.run_harvest(conn, sources=["greenhouse"])

        status = conn.execute(
            "SELECT status FROM postings WHERE source_posting_id='job-1'"
        ).fetchone()["status"]
        assert status == "active"  # never closed off a failed board
    finally:
        conn.close()


def test_run_harvest_never_closes_postings_from_anomalous_empty_board(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "test.db")
    try:
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, watchlist, added_at) "
            "VALUES ('Acme Corp', 'ai-infra', 'greenhouse', 'acme', 0, '')"
        )
        conn.commit()
        monkeypatch.setattr(greenhouse, "fetch", lambda slug, company=None: [_posting("job-1")])
        harvest_module.run_harvest(conn, sources=["greenhouse"])
        monkeypatch.setattr(greenhouse, "fetch", lambda slug, company=None: [])
        result = harvest_module.run_harvest(conn, sources=["greenhouse"])["greenhouse"]
        status = conn.execute(
            "SELECT status FROM postings WHERE source_posting_id='job-1'"
        ).fetchone()["status"]
        assert status == "active"
        assert any("anomalous empty board" in item["error"] for item in result.errors)
    finally:
        conn.close()


def test_run_harvest_ingests_alerts_postings(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "test.db")
    try:
        alert_jobs = [_posting("alert-job-1"), _posting("alert-job-2")]
        monkeypatch.setattr(alerts, "fetch", lambda: alert_jobs)
        harvest_module.run_harvest(conn, sources=["alerts"])

        statuses = {
            row["source_posting_id"]: row["status"]
            for row in conn.execute(
                "SELECT source_posting_id, status FROM postings WHERE source='alerts'"
            ).fetchall()
        }
        assert statuses == {"alert-job-1": "active", "alert-job-2": "active"}
    finally:
        conn.close()


def test_run_harvest_alerts_absent_posting_stays_active(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "test.db")
    try:
        monkeypatch.setattr(
            alerts, "fetch", lambda: [_posting("alert-job-1"), _posting("alert-job-2")]
        )
        harvest_module.run_harvest(conn, sources=["alerts"])

        # the next batch of alert emails only mentions alert-job-1 — alert-job-2
        # simply wasn't re-announced, which must NOT be read as it closing.
        monkeypatch.setattr(alerts, "fetch", lambda: [_posting("alert-job-1")])
        harvest_module.run_harvest(conn, sources=["alerts"])

        statuses = {
            row["source_posting_id"]: row["status"]
            for row in conn.execute(
                "SELECT source_posting_id, status FROM postings WHERE source='alerts'"
            ).fetchall()
        }
        assert statuses["alert-job-1"] == "active"
        assert statuses["alert-job-2"] == "active"
    finally:
        conn.close()


def test_run_harvest_alerts_empty_sweep_is_not_anomalous(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "test.db")
    try:
        monkeypatch.setattr(alerts, "fetch", lambda: [_posting("alert-job-1")])
        harvest_module.run_harvest(conn, sources=["alerts"])

        # a run with zero new alert emails is normal, not anomalous
        monkeypatch.setattr(alerts, "fetch", lambda: [])
        result = harvest_module.run_harvest(conn, sources=["alerts"])["alerts"]

        status = conn.execute(
            "SELECT status FROM postings WHERE source_posting_id='alert-job-1'"
        ).fetchone()["status"]
        assert status == "active"
        assert result.errors == []
    finally:
        conn.close()


def test_run_harvest_alerts_fetch_error_recorded_without_raising(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "test.db")
    try:
        def _boom():
            raise RuntimeError("alerts: no Gmail token at ~/.jobhunt — run gmail-auth")

        monkeypatch.setattr(alerts, "fetch", _boom)

        result = harvest_module.run_harvest(conn, sources=["alerts"])["alerts"]

        assert len(result.errors) == 1
        assert "no Gmail token" in result.errors[0]["error"]
    finally:
        conn.close()


def test_run_harvest_single_source_snapshot_still_closes_stale_postings(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "test.db")
    try:
        monkeypatch.setattr(
            simplify, "fetch", lambda: [_posting("simp-job-1"), _posting("simp-job-2")]
        )
        harvest_module.run_harvest(conn, sources=["simplify"])

        # snapshot single sources (unlike alerts) DO close postings missing
        # from a fresh sweep.
        monkeypatch.setattr(simplify, "fetch", lambda: [_posting("simp-job-1")])
        harvest_module.run_harvest(conn, sources=["simplify"])

        statuses = {
            row["source_posting_id"]: row["status"]
            for row in conn.execute(
                "SELECT source_posting_id, status FROM postings WHERE source='simplify'"
            ).fetchall()
        }
        assert statuses["simp-job-1"] == "active"
        assert statuses["simp-job-2"] == "closed"
    finally:
        conn.close()


def test_parallel_boards_fetch_concurrently_but_ingest_in_order(tmp_path, monkeypatch):
    """A module with PARALLEL_BOARDS>1 overlaps fetches; results are still
    ingested in companies order and errors are attributed to the right board."""
    import threading
    from types import SimpleNamespace

    conn = db_module.connect(tmp_path / "harvest.db")
    try:
        names = [f"T{i}" for i in range(6)]
        for name in names:
            conn.execute("INSERT INTO companies (name, platform, slug, watchlist, added_at) VALUES (?, 'workday', ?, 0, '')", (name, name.lower()))
        conn.commit()

        gate = threading.Barrier(3, timeout=5)  # proves >=3 fetches are in flight together
        seen = []
        lock = threading.Lock()

        class BoardNotFound(Exception):
            pass

        def fetch(slug, company=None):
            gate.wait()
            with lock:
                seen.append(slug)
            if slug == "t2":
                raise BoardNotFound(slug)
            if slug == "t4":
                raise RuntimeError("boom")
            return [_posting(slug, company=company)]

        module = SimpleNamespace(fetch=fetch, BoardNotFound=BoardNotFound, PARALLEL_BOARDS=3)
        monkeypatch.setitem(harvest_module.BOARD_SOURCES, "workday", module)
        results = harvest_module.run_harvest(conn, sources=["workday"])

        stats = results["workday"]
        assert sorted(seen) == [n.lower() for n in names]
        assert stats.boards_attempted == 6
        assert stats.boards_ok == 4
        assert [(e["slug"], e["error"]) for e in stats.errors] == [
            ("t2", "404 board not found"),
            ("t4", "unexpected: boom"),
        ]
        rows = [r["company"] for r in conn.execute("SELECT company FROM postings ORDER BY id")]
        assert rows == ["T0", "T1", "T3", "T5"]
        assert not conn.in_transaction
    finally:
        conn.close()


def test_sequential_default_when_module_has_no_parallel_setting(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "harvest.db")
    try:
        for name in ("A", "B"):
            conn.execute("INSERT INTO companies (name, platform, slug, watchlist, added_at) VALUES (?, 'greenhouse', ?, 0, '')", (name, name.lower()))
        conn.commit()
        active = 0
        peak = 0

        def fetch(slug, company=None):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            active -= 1
            return [_posting(slug, company=company)]

        monkeypatch.setattr(greenhouse, "fetch", fetch)
        assert not hasattr(greenhouse, "PARALLEL_BOARDS")
        harvest_module.run_harvest(conn, sources=["greenhouse"])
        assert peak == 1
    finally:
        conn.close()


def test_board_source_with_no_companies_is_not_a_failure(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "harvest.db")
    try:
        monkeypatch.setattr(greenhouse, "fetch", lambda slug, company=None: pytest.fail("must not fetch"))
        harvest_module.run_harvest(conn, sources=["greenhouse"])
        row = conn.execute("SELECT consecutive_failures, last_error FROM source_state WHERE source='greenhouse'").fetchone()
        assert row is None
        sweep = conn.execute("SELECT boards_attempted, boards_ok FROM sweeps WHERE source='greenhouse'").fetchone()
        assert tuple(sweep) == (0, 0)
    finally:
        conn.close()
