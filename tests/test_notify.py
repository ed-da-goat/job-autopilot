from __future__ import annotations

import subprocess

from jobhunt import db as db_module
from jobhunt import funnel as funnel_module
from jobhunt import notify as notify_module
from jobhunt import runlog


def _make_posting(
    conn, spid: str, company: str = "Acme", title: str = "Backend Engineer"
) -> int:
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, status, first_seen_at, last_seen_at)
           VALUES ('greenhouse', ?, ?, ?, 'active', '', '')""",
        (spid, company, title),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM postings WHERE source_posting_id=?", (spid,)
    ).fetchone()["id"]


# --- notify() degradation -------------------------------------------------


def test_notify_swallows_missing_osascript(monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("osascript not found")

    monkeypatch.setattr(subprocess, "run", _raise)
    notify_module.notify("Job Hunt Digest", "3 new suggestions")  # must not raise


def test_notify_swallows_called_process_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["osascript"])

    monkeypatch.setattr(subprocess, "run", _raise)
    notify_module.notify("Job Hunt Digest", "3 new suggestions")  # must not raise


def test_notify_swallows_timeout(monkeypatch):
    def _raise(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["osascript"], timeout=5)

    monkeypatch.setattr(subprocess, "run", _raise)
    notify_module.notify("Job Hunt Digest", "3 new suggestions")  # must not raise


def test_notify_calls_osascript_with_timeout(monkeypatch):
    calls = []

    def _fake_run(cmd, timeout=None, check=None, capture_output=None):
        calls.append((cmd, timeout))

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    notify_module.notify("Title", "Body")
    assert len(calls) == 1
    cmd, timeout = calls[0]
    assert cmd[0] == "osascript"
    assert timeout == notify_module.NOTIFY_TIMEOUT_SECONDS


# --- should_notify: dict-data branches ------------------------------------


def test_should_notify_true_when_new_suggestions(monkeypatch):
    data = {"new_suggestions": 2, "applications_7d": 10, "warm_touches_7d": 5}
    assert notify_module.should_notify(data, weekday=0) is True  # Monday, gates passing


def test_should_notify_false_when_no_suggestions_and_gates_pass():
    data = {
        "new_suggestions": 0,
        "applications_7d": 10,
        "warm_touches_7d": 5,
        "stale_jobs": False,
    }
    assert (
        notify_module.should_notify(data, weekday=6) is False
    )  # Sunday, gates passing


def test_should_notify_false_early_week_gate_failing():
    data = {
        "new_suggestions": 0,
        "applications_7d": 2,
        "warm_touches_7d": 1,
        "stale_jobs": False,
    }
    assert notify_module.should_notify(data, weekday=0) is False  # Monday


def test_should_notify_true_when_a_scheduled_job_is_stale():
    """A dead scheduler notifies on any weekday, even with everything else quiet."""
    data = {
        "new_suggestions": 0,
        "applications_7d": 10,
        "warm_touches_7d": 5,
        "stale_jobs": True,
    }
    assert notify_module.should_notify(data, weekday=0) is True


def test_should_notify_defaults_to_loud_when_liveness_is_unreported():
    """The mapping default is stale-unknown on purpose: a caller that forgets
    to report liveness is the silence this signal exists to break."""
    data = {"new_suggestions": 0, "applications_7d": 10, "warm_touches_7d": 5}
    assert notify_module.should_notify(data, weekday=0) is True


def test_should_notify_true_thursday_gate_failing():
    data = {"new_suggestions": 0, "applications_7d": 2, "warm_touches_7d": 1}
    assert notify_module.should_notify(data, weekday=3) is True  # Thursday


def test_should_notify_true_late_week_gate_failing_one_gate_only():
    data = {"new_suggestions": 0, "applications_7d": 10, "warm_touches_7d": 1}
    assert (
        notify_module.should_notify(data, weekday=4) is True
    )  # Friday, warm touches short


# --- should_notify: conn (duck-typed) branch ------------------------------


def test_should_notify_conn_path_true_with_fresh_suggestion(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn, "p1")
        funnel_module.move(conn, posting_id, "suggested")
        assert notify_module.should_notify(conn, weekday=0) is True
    finally:
        conn.close()


def test_should_notify_conn_path_false_no_data(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        # An empty DB has never run either scheduled job, which is itself a
        # notify-worthy condition — record clean runs to isolate the branch.
        for job in runlog.JOBS:
            runlog.finish_run(conn, runlog.start_run(conn, job), ok=True)
        assert notify_module.should_notify(conn, weekday=0) is False
    finally:
        conn.close()


def test_should_notify_conn_path_true_when_scheduler_never_ran(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        assert notify_module.should_notify(conn, weekday=0) is True
    finally:
        conn.close()
