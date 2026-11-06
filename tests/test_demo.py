"""Demo seeding: deterministic, synthetic-only, never touches the real DB."""

from __future__ import annotations

import re
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from jobhunt import db as db_module
from jobhunt import demo
from jobhunt.web.app import app, get_db_path


def _query(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("demo") / "demo.db"
    started = time.monotonic()
    demo.seed(path)
    assert time.monotonic() - started < 10
    return path


def test_seed_is_deterministic(demo_db, tmp_path):
    other = tmp_path / "again.db"
    demo.seed(other)
    first = "SELECT title FROM postings ORDER BY id LIMIT 5"
    assert _query(demo_db, first) == _query(other, first)
    for table in ("companies", "postings", "scores", "funnel", "funnel_events", "interviews"):
        sql = f"SELECT COUNT(*) FROM {table}"
        assert _query(demo_db, sql) == _query(other, sql)


def test_funnel_counts_in_range(demo_db):
    stages = dict(_query(demo_db, "SELECT stage, COUNT(*) FROM funnel GROUP BY stage"))
    assert _query(demo_db, "SELECT COUNT(*) FROM postings")[0][0] == 5000
    assert 100 <= _query(demo_db, "SELECT COUNT(DISTINCT name) FROM companies")[0][0] <= 200
    assert 350 <= stages["suggested"] <= 450
    assert 100 <= stages["applied"] <= 140
    assert 10 <= stages["replied"] <= 20
    assert stages["interviewing"] == 5
    assert stages["offer"] == 1
    assert _query(demo_db, "SELECT COUNT(*) FROM interviews")[0][0] >= 3
    sources = {r[0] for r in _query(demo_db, "SELECT DISTINCT source FROM postings")}
    assert sources == {"greenhouse", "lever", "ashby", "workday"}


def test_refuses_default_db_path():
    with pytest.raises(ValueError, match="real database"):
        demo.seed(db_module.DEFAULT_DB_PATH)


def test_only_example_dot_com_addresses(demo_db):
    conn = sqlite3.connect(demo_db)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            for row in conn.execute(f"SELECT * FROM {table}"):
                for value in row:
                    if isinstance(value, str) and "@" in value:
                        for addr in re.findall(r"[\w.+-]+@[\w.-]+", value):
                            assert addr.lower().endswith("@example.com"), (table, addr)
    finally:
        conn.close()


def test_api_serves_demo_data(demo_db):
    app.dependency_overrides[get_db_path] = lambda: demo_db
    try:
        http = TestClient(app)
        today = http.get("/api/today")
        assert today.status_code == 200
        body = today.json()
        assert body["eligible"] and body["interviews"] and body["offers"]
        assert body["sprint"]["members"]
        postings = http.get("/api/postings")
        assert postings.status_code == 200
        assert postings.json()
        assert http.get("/api/report").status_code == 200
        assert http.get("/api/health").status_code == 200
    finally:
        app.dependency_overrides.clear()
