from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jobhunt import conference, db as db_module, today as today_module
from jobhunt.web.app import app, get_db_path

FIXTURE_MD = """# Fixture

## Action queue

| Due | Recur | Action | Program | Notes |
|---|---|---|---|---|
| 2026-01-01 | 30 | Check + apply | Program A | Some notes |
| 2026-01-02 |  | Verify eligibility | Program B | **Bold note** |
"""


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "t.db"
    app.dependency_overrides[get_db_path] = lambda: db_path
    try:
        yield TestClient(app), db_path
    finally:
        app.dependency_overrides.clear()


def _insert_action(conn, program="Program A", action="Check + apply", **fields):
    row = {
        "program": program,
        "action": action,
        "due_at": fields.get("due_at"),
        "recur_days": fields.get("recur_days"),
        "status": fields.get("status", "planned"),
        "notes": fields.get("notes"),
        "completed_at": fields.get("completed_at"),
        "created_at": datetime.now(UTC).isoformat(),
    }
    conn.execute(
        """INSERT INTO conference_actions
               (program, action, due_at, recur_days, status, notes, completed_at, created_at)
           VALUES (:program, :action, :due_at, :recur_days, :status, :notes, :completed_at, :created_at)""",
        row,
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_parse_conference_markdown(tmp_path):
    path = tmp_path / "conference_funding.md"
    path.write_text(FIXTURE_MD, encoding="utf-8")

    rows = conference.parse_conference_markdown(path)

    assert len(rows) == 2
    a = next(r for r in rows if r["program"] == "Program A")
    assert a["action"] == "Check + apply"
    assert a["due_at"] == "2026-01-01"
    assert a["recur_days"] == 30
    assert a["notes"] == "Some notes"

    b = next(r for r in rows if r["program"] == "Program B")
    assert b["recur_days"] is None
    assert b["notes"] == "Bold note"  # markdown bold stripped


def test_upsert_is_idempotent_and_never_clobbers_due_at_or_status(tmp_path):
    path = tmp_path / "conference_funding.md"
    path.write_text(FIXTURE_MD, encoding="utf-8")
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _insert_action(
            conn,
            due_at="2099-01-01",
            recur_days=1,
            status="completed",
            notes="old notes",
            completed_at="2020-01-01T00:00:00",
        )

        rows = conference.parse_conference_markdown(path)
        count = conference.upsert_conference_actions(conn, rows)
        assert count == 2

        row = conn.execute(
            "SELECT * FROM conference_actions WHERE program='Program A'"
        ).fetchone()
        # due_at/status untouched by re-import, notes/recur_days refreshed
        assert row["due_at"] == "2099-01-01"
        assert row["status"] == "completed"
        assert row["notes"] == "Some notes"
        assert row["recur_days"] == 30

        total = conn.execute("SELECT COUNT(*) c FROM conference_actions").fetchone()["c"]
        assert total == 2  # Program B inserted new, Program A updated not duplicated
    finally:
        conn.close()


def test_build_today_includes_only_planned_and_due_conference_actions(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        today_str = datetime.now(UTC).date().isoformat()
        future = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
        due_id = _insert_action(conn, program="Due Now", action="Act", due_at=today_str)
        _insert_action(conn, program="Not Yet", action="Act", due_at=future)
        _insert_action(conn, program="Done", action="Act", due_at=today_str, status="completed")

        result = today_module.build_today(conn)

        ids = [row["id"] for row in result["conference_actions"]]
        assert ids == [due_id]
    finally:
        conn.close()


def test_complete_conference_action_recur_bumps_due_at_and_stays_planned(client):
    http, db_path = client
    conn = db_module.connect(db_path)
    try:
        past = (datetime.now(UTC) - timedelta(days=3)).date().isoformat()
        action_id = _insert_action(conn, due_at=past, recur_days=7)
    finally:
        conn.close()

    resp = http.post(f"/api/conference_actions/{action_id}/complete", json={"final": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "planned"
    expected_due = (datetime.now(UTC).date() + timedelta(days=7)).isoformat()
    assert body["due_at"] == expected_due

    final_resp = http.post(f"/api/conference_actions/{action_id}/complete", json={"final": True})
    assert final_resp.status_code == 200
    assert final_resp.json()["status"] == "completed"
    assert final_resp.json()["completed_at"]


def test_complete_conference_action_without_recur_completes_directly(client):
    http, db_path = client
    conn = db_module.connect(db_path)
    try:
        action_id = _insert_action(conn, program="One shot", due_at="2026-01-01", recur_days=None)
    finally:
        conn.close()

    resp = http.post(f"/api/conference_actions/{action_id}/complete", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


def test_complete_conference_action_missing_id_404s(client):
    http, _ = client
    resp = http.post("/api/conference_actions/999999/complete", json={})
    assert resp.status_code == 404
