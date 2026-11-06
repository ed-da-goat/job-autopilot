from __future__ import annotations

import pytest
from jobhunt import applysession as applysession_module
from jobhunt import db as db_module
from jobhunt import funnel as funnel_module


class FakePopen:
    """Stand-in for subprocess.Popen: records the launch command and lets
    the test control when the process "exits" via .returncode."""

    def __init__(self, cmd, stdout=None, stderr=None, start_new_session=None):
        self.cmd = cmd
        self.pid = 4242
        self.returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    applysession_module._reset_for_tests()
    monkeypatch.setattr(applysession_module, "LOG_PATH", tmp_path / "apply_session.log")
    yield
    applysession_module._reset_for_tests()


def _make_posting(conn, spid: str, company: str = "Acme", title: str = "Backend Engineer") -> int:
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, status, first_seen_at, last_seen_at)
           VALUES ('greenhouse', ?, ?, ?, 'active', '', '')""",
        (spid, company, title),
    )
    conn.commit()
    return conn.execute("SELECT id FROM postings WHERE source_posting_id=?", (spid,)).fetchone()["id"]


def test_start_spawns_apply_queue_subprocess(monkeypatch):
    fakes: list[FakePopen] = []

    def fake_popen(cmd, **kwargs):
        proc = FakePopen(cmd, **kwargs)
        fakes.append(proc)
        return proc

    monkeypatch.setattr(applysession_module.subprocess, "Popen", fake_popen)

    result = applysession_module.start(limit=7)
    assert len(fakes) == 1
    cmd = fakes[0].cmd
    assert "apply-queue" in cmd
    assert "--limit" in cmd
    assert cmd[cmd.index("--limit") + 1] == "7"
    assert "--min-score" not in cmd
    assert result["running"] is True
    assert result["pid"] == fakes[0].pid


def test_start_passes_min_score(monkeypatch):
    fakes: list[FakePopen] = []
    monkeypatch.setattr(
        applysession_module.subprocess,
        "Popen",
        lambda cmd, **kwargs: fakes.append(FakePopen(cmd, **kwargs)) or fakes[-1],
    )
    applysession_module.start(limit=3, min_score=60)
    cmd = fakes[0].cmd
    assert cmd[cmd.index("--min-score") + 1] == "60"


def test_start_while_running_raises(monkeypatch):
    monkeypatch.setattr(applysession_module.subprocess, "Popen", lambda cmd, **kwargs: FakePopen(cmd, **kwargs))
    applysession_module.start()
    with pytest.raises(RuntimeError, match="already running"):
        applysession_module.start()


def test_status_shape_before_and_after_exit(monkeypatch):
    procs: list[FakePopen] = []

    def fake_popen(cmd, **kwargs):
        proc = FakePopen(cmd, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(applysession_module.subprocess, "Popen", fake_popen)

    idle = applysession_module.status()
    assert idle == {
        "running": False,
        "pid": None,
        "started_at": None,
        "exit_code": None,
        "log_tail": [],
    }

    applysession_module.start(limit=5)
    running = applysession_module.status()
    assert running["running"] is True
    assert running["pid"] == procs[0].pid
    assert running["started_at"] is not None
    assert running["exit_code"] is None

    procs[0].returncode = 0
    finished = applysession_module.status()
    assert finished["running"] is False
    assert finished["exit_code"] == 0


def test_invariant_scan_counts_dropped_balls(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        no_action_id = _make_posting(conn, "p1")
        funnel_module.move(conn, no_action_id, "qualified")

        applied_id = _make_posting(conn, "p2")
        funnel_module.move(conn, applied_id, "applied")

        healthy_id = _make_posting(conn, "p3")
        funnel_module.move(conn, healthy_id, "ready")
        funnel_module.set_field(conn, healthy_id, "next_action", "follow up")
        funnel_module.set_field(conn, healthy_id, "next_action_date", "2026-08-10")

        from jobhunt.today import invariant_scan

        result = invariant_scan(conn)
        assert result["no_next_action_count"] == 1
        assert result["applied_missing_submitted_count"] == 1
        no_action_ids = {row["posting_id"] for row in result["no_next_action"]}
        applied_ids = {row["posting_id"] for row in result["applied_missing_submitted"]}
        assert no_action_ids == {no_action_id}
        assert applied_ids == {applied_id}
        assert healthy_id not in no_action_ids
    finally:
        conn.close()
