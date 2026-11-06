from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from jobhunt import db as db_module
from jobhunt import spend as spend_module


def _insert_ledger_row(conn, day: str, cost_usd: float, draft_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO spend_ledger (draft_id, day, cost_usd, at) VALUES (?, ?, ?, ?)",
        (draft_id, day, cost_usd, datetime.now(UTC).isoformat()),
    )
    conn.commit()


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _yesterday() -> str:
    return (datetime.now(UTC).date() - timedelta(days=1)).isoformat()


def test_today_spend_zero_with_empty_ledger(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        assert spend_module.today_spend(conn) == 0.0
    finally:
        conn.close()


def test_today_spend_sums_only_todays_rows(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _insert_ledger_row(conn, _today(), 0.10)
        _insert_ledger_row(conn, _today(), 0.25)
        _insert_ledger_row(conn, _yesterday(), 5.00)
        assert spend_module.today_spend(conn) == 0.35
    finally:
        conn.close()


def test_check_budget_passes_under_cap(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _insert_ledger_row(conn, _today(), 0.99)
        remaining = spend_module.check_budget(conn)
        assert remaining == pytest.approx(0.01)
    finally:
        conn.close()


def test_check_budget_raises_at_cap_exact(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _insert_ledger_row(conn, _today(), 1.00)
        try:
            spend_module.check_budget(conn)
            assert False, "expected SpendCapError"
        except spend_module.SpendCapError:
            pass
    finally:
        conn.close()


def test_check_budget_raises_when_over_cap(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _insert_ledger_row(conn, _today(), 1.50)
        try:
            spend_module.check_budget(conn)
            assert False, "expected SpendCapError"
        except spend_module.SpendCapError:
            pass
    finally:
        conn.close()


def test_check_budget_ignores_yesterdays_spend(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _insert_ledger_row(conn, _yesterday(), 999.00)
        remaining = spend_module.check_budget(conn)
        assert remaining == spend_module.DAILY_CAP_USD
    finally:
        conn.close()


def test_check_budget_honors_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNT_DAILY_CAP_USD", "0.05")
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _insert_ledger_row(conn, _today(), 0.04)
        remaining = spend_module.check_budget(conn)
        assert remaining == pytest.approx(0.01)
    finally:
        conn.close()


def test_check_budget_reserves_maximum_call_cost(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _insert_ledger_row(conn, _today(), 0.99)
        with pytest.raises(spend_module.SpendCapError):
            spend_module.check_budget(conn, anticipated_cost=0.02)
    finally:
        conn.close()


def test_record_spend_row_shape(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        spend_module.record_spend(conn, draft_id=None, cost_usd=0.0042)
        row = conn.execute("SELECT * FROM spend_ledger").fetchone()
        assert row["draft_id"] is None
        assert row["day"] == _today()
        assert row["cost_usd"] == 0.0042
        assert row["at"] is not None and "T" in row["at"]
    finally:
        conn.close()


def test_record_spend_with_draft_id(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        conn.execute(
            """INSERT INTO postings
               (source, source_posting_id, company, title, status, first_seen_at, last_seen_at)
               VALUES ('greenhouse', 'p1', 'Acme', 'Backend Engineer', 'active', '', '')"""
        )
        conn.commit()
        posting_id = conn.execute("SELECT id FROM postings WHERE source_posting_id='p1'").fetchone()["id"]
        conn.execute(
            "INSERT INTO drafts (posting_id, kind, content, created_at) VALUES (?, 'cover', 'x', '')",
            (posting_id,),
        )
        conn.commit()
        draft_id = conn.execute("SELECT id FROM drafts").fetchone()["id"]

        spend_module.record_spend(conn, draft_id=draft_id, cost_usd=0.01)
        row = conn.execute("SELECT * FROM spend_ledger WHERE draft_id=?", (draft_id,)).fetchone()
        assert row["draft_id"] == draft_id
        assert spend_module.today_spend(conn) == 0.01
    finally:
        conn.close()


def test_record_spend_job_and_kind_round_trip(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        spend_module.record_spend(conn, None, 0.02, job="daily", kind="cover")
        row = conn.execute("SELECT job, kind FROM spend_ledger").fetchone()
        assert row["job"] == "daily"
        assert row["kind"] == "cover"
    finally:
        conn.close()


def test_today_breakdown_splits_by_job_and_buckets_null_as_draft(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        spend_module.record_spend(conn, None, 0.10, job="daily")
        spend_module.record_spend(conn, None, 0.05, job="daily")
        spend_module.record_spend(conn, None, 0.20, job="dial")
        spend_module.record_spend(conn, None, 0.03)  # no job attribution
        breakdown = spend_module.today_breakdown(conn)
        assert breakdown == {"daily": pytest.approx(0.15), "dial": pytest.approx(0.20), "draft": pytest.approx(0.03)}
    finally:
        conn.close()


def test_run_budget_stops_batch_at_run_cap_before_daily_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNT_RUN_CAP_USD", "0.25")
    conn = db_module.connect(tmp_path / "t.db")
    try:
        budget = spend_module.RunBudget(conn, job="daily")
        budget.reserve(0.10)
        budget.settle(0.10)
        budget.reserve(0.10)
        budget.settle(0.10)
        # A third $0.10 item would take this run to $0.30, over the $0.25 run cap,
        # while the daily cap ($1.00) has plenty of room left.
        with pytest.raises(spend_module.SpendCapError):
            budget.reserve(0.10)
        assert budget.spent == pytest.approx(0.20)
        assert spend_module.today_spend(conn) == pytest.approx(0.20)
    finally:
        conn.close()


def test_run_budget_raises_when_daily_cap_is_the_binding_constraint(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNT_RUN_CAP_USD", "50.00")
    monkeypatch.setenv("JOBHUNT_DAILY_CAP_USD", "0.10")
    conn = db_module.connect(tmp_path / "t.db")
    try:
        budget = spend_module.RunBudget(conn, job="daily")
        with pytest.raises(spend_module.SpendCapError):
            budget.reserve(0.20)
    finally:
        conn.close()
