from __future__ import annotations

import pytest
from jobhunt import db as db_module
from jobhunt import funnel as funnel_module


def _make_posting(conn, spid: str = "p1") -> int:
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, status, first_seen_at, last_seen_at)
           VALUES ('greenhouse', ?, 'Acme', 'Backend Engineer', 'active', '', '')""",
        (spid,),
    )
    conn.commit()
    return conn.execute("SELECT id FROM postings WHERE source_posting_id=?", (spid,)).fetchone()["id"]


def test_ensure_funnel_row_creates_at_harvested_and_is_idempotent(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn)
        row1 = funnel_module.ensure_funnel_row(conn, posting_id)
        assert row1["stage"] == "harvested"
        assert row1["applied_at"] is None

        row2 = funnel_module.ensure_funnel_row(conn, posting_id)
        assert row2["id"] == row1["id"]

        count = conn.execute("SELECT COUNT(*) c FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()["c"]
        assert count == 1
    finally:
        conn.close()


def test_move_appends_events(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn)

        from_stage, to_stage = funnel_module.move(conn, posting_id, "qualified", note="looks good")
        assert (from_stage, to_stage) == ("harvested", "qualified")

        funnel_module.move(conn, posting_id, "applied")
        funnel_module.move(conn, posting_id, "replied")

        events = conn.execute(
            "SELECT from_stage, to_stage, note FROM funnel_events WHERE posting_id=? ORDER BY id", (posting_id,)
        ).fetchall()
        assert [(e["from_stage"], e["to_stage"]) for e in events] == [
            ("harvested", "qualified"),
            ("qualified", "applied"),
            ("applied", "replied"),
        ]
        assert events[0]["note"] == "looks good"

        current = conn.execute("SELECT stage FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
        assert current["stage"] == "replied"
    finally:
        conn.close()


def test_move_any_stage_to_any_stage_is_allowed(tmp_path):
    """Real processes are messy: passed -> replied (a late reply) must not be blocked."""
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn)
        funnel_module.move(conn, posting_id, "passed")
        from_stage, to_stage = funnel_module.move(conn, posting_id, "replied")
        assert (from_stage, to_stage) == ("passed", "replied")
    finally:
        conn.close()


def test_applied_at_stamped_once(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn)
        funnel_module.move(conn, posting_id, "applied")
        first = conn.execute("SELECT applied_at FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
        assert first["applied_at"] is not None

        # move away and back to applied — applied_at must not be overwritten
        funnel_module.move(conn, posting_id, "replied")
        funnel_module.move(conn, posting_id, "applied")
        second = conn.execute("SELECT applied_at FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
        assert second["applied_at"] == first["applied_at"]
    finally:
        conn.close()


def test_harvested_to_applied_direct(tmp_path):
    """'tailored' is optional — harvested -> applied direct is fine."""
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn)
        from_stage, to_stage = funnel_module.move(conn, posting_id, "applied")
        assert (from_stage, to_stage) == ("harvested", "applied")
        row = conn.execute("SELECT applied_at FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
        assert row["applied_at"] is not None
    finally:
        conn.close()


def test_move_rejects_unknown_stage(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn)
        with pytest.raises(ValueError, match="unknown stage"):
            funnel_module.move(conn, posting_id, "ghosted")
    finally:
        conn.close()


def test_set_field_rejects_unknown_field(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn)
        with pytest.raises(ValueError, match="unknown field"):
            funnel_module.set_field(conn, posting_id, "salary", "200k")
    finally:
        conn.close()


def test_set_field_allowlisted_field_persists(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn)
        funnel_module.set_field(conn, posting_id, "referral_path", "GT alum intro")
        row = conn.execute("SELECT referral_path FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
        assert row["referral_path"] == "GT alum intro"
    finally:
        conn.close()
