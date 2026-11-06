from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobhunt import db, runlog


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def _isolate_launch_agents(tmp_path, monkeypatch):
    """Never read the real ~/Library/LaunchAgents — the result would depend on
    whether the machine running the tests happens to have the jobs installed."""
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    monkeypatch.setattr(runlog, "LAUNCH_AGENT_DIR", agents)
    return agents


def _install(agents, job):
    (agents / runlog.JOBS[job][0]).write_text("<plist/>")


def test_track_records_a_successful_run(conn):
    with runlog.track(conn, "daily") as run:
        run.note("suggested 3")

    row = conn.execute("SELECT * FROM run_log").fetchone()
    assert row["job"] == "daily"
    assert row["ok"] == 1
    assert row["finished_at"] is not None
    assert row["summary"] == "suggested 3"


def test_track_records_failure_and_reraises(conn):
    with pytest.raises(ValueError, match="harvest exploded"):
        with runlog.track(conn, "daily"):
            raise ValueError("harvest exploded")

    row = conn.execute("SELECT * FROM run_log").fetchone()
    assert row["ok"] == 0
    assert "failed: ValueError: harvest exploded" in row["summary"]


def test_track_treats_clean_exit_as_success(conn):
    """typer.Exit(0) is how a CLI ends a run early, not how it fails."""

    class Exit(RuntimeError):
        exit_code = 0

    with pytest.raises(Exit):
        with runlog.track(conn, "apply-queue"):
            raise Exit()

    assert conn.execute("SELECT ok FROM run_log").fetchone()["ok"] == 1


def test_start_run_is_committed_before_the_work(conn):
    """A hard kill mid-run must still leave a trace, so the row lands up front."""
    run_id = runlog.start_run(conn, "daily")
    row = conn.execute("SELECT * FROM run_log WHERE id=?", (run_id,)).fetchone()
    assert row["started_at"] is not None
    assert row["ok"] is None


def test_never_run_job_is_stale(conn):
    statuses = {item["job"]: item for item in runlog.job_status(conn)}
    assert statuses["daily"]["stale"] is True
    assert statuses["daily"]["ever_ran"] is False
    assert statuses["daily"]["age_hours"] is None


def test_job_goes_stale_past_its_window(conn):
    now = datetime.now(UTC)
    old = now - timedelta(hours=50)
    run_id = runlog.start_run(conn, "daily", now=old)
    runlog.finish_run(conn, run_id, ok=True, now=old)

    daily = next(i for i in runlog.job_status(conn, now=now) if i["job"] == "daily")
    assert daily["stale"] is True
    assert daily["age_hours"] == pytest.approx(50.0, abs=0.2)

    # The same 50h-old run is fresh for apply-queue, whose window spans a weekend.
    queue_id = runlog.start_run(conn, "apply-queue", now=old)
    runlog.finish_run(conn, queue_id, ok=True, now=old)
    queue = next(
        i for i in runlog.job_status(conn, now=now) if i["job"] == "apply-queue"
    )
    assert queue["stale"] is False


def test_in_flight_run_counts_as_alive(conn):
    """The daily run writes its own digest before finishing; it must not read
    as a day stale in its own output."""
    now = datetime.now(UTC)
    runlog.start_run(conn, "daily", now=now)

    daily = next(i for i in runlog.job_status(conn, now=now) if i["job"] == "daily")
    assert daily["stale"] is False
    assert daily["in_progress"] is True


def test_abandoned_in_flight_run_still_goes_stale(conn):
    """A SIGKILLed run leaves an unfinished row forever; it must not mask death."""
    now = datetime.now(UTC)
    runlog.start_run(conn, "daily", now=now - timedelta(days=10))

    daily = next(i for i in runlog.job_status(conn, now=now) if i["job"] == "daily")
    assert daily["stale"] is True


def test_failed_last_attempt_is_reported(conn):
    now = datetime.now(UTC)
    run_id = runlog.start_run(conn, "daily", now=now)
    runlog.finish_run(
        conn, run_id, ok=False, summary="failed: RuntimeError: boom", now=now
    )

    daily = next(i for i in runlog.job_status(conn, now=now) if i["job"] == "daily")
    assert daily["last_ok"] is False
    assert "boom" in daily["last_error"]


def test_uninstalled_scheduler_is_distinguished_from_a_dead_one(
    conn, _isolate_launch_agents
):
    _install(_isolate_launch_agents, "daily")
    statuses = {item["job"]: item for item in runlog.job_status(conn)}
    assert statuses["daily"]["scheduler_installed"] is True
    assert statuses["apply-queue"]["scheduler_installed"] is False

    lines = "\n".join(runlog.render_liveness(runlog.job_status(conn)))
    assert "make schedule-install-applyqueue" in lines
    assert "scheduled but has never run" in lines


def test_render_liveness_marks_stale_jobs(conn):
    now = datetime.now(UTC)
    old = now - timedelta(hours=60)
    run_id = runlog.start_run(conn, "daily", now=old)
    runlog.finish_run(conn, run_id, ok=True, now=old)

    statuses = runlog.job_status(conn, now=now)
    lines = runlog.render_liveness(statuses)
    daily_line = next(line for line in lines if line.startswith("[!] daily"))
    assert "2.5d ago" in daily_line
    assert "stale after 36h" in daily_line
    assert runlog.any_stale(statuses) is True


def test_reconnecting_an_older_db_creates_run_log_and_bumps_version(tmp_path):
    """v3 is additive: init_schema runs on every connect, so an existing DB
    picks up run_log without migration logic."""
    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.execute("DROP TABLE run_log")
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()

    conn = db.connect(path)
    try:
        assert (
            conn.execute(
                "SELECT count(*) c FROM sqlite_master WHERE name='run_log'"
            ).fetchone()["c"]
            == 1
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        conn.close()
