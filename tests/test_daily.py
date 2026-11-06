from __future__ import annotations

import sys

from jobhunt import db as db_module
from jobhunt import sheets as sheets_module

import daily

# Captured before any test monkeypatches db_module.connect (tests patch the
# attribute on the shared jobhunt.db module object, so a name bound to it
# afterward would resolve to the patched — and by-then-closed-conn — lambda).
_real_db_connect = db_module.connect


def test_notify_body_is_plain_when_the_loop_is_healthy():
    assert daily._notify_body(3, "2026-08-02.md", []) == (
        "3 new suggestion(s) — see 2026-08-02.md"
    )


def test_notify_body_leads_with_the_stale_scheduler():
    """A notification fired *because* a job died must not read as
    '0 new suggestion(s)' — an alarm that misnames its cause gets muted."""
    body = daily._notify_body(0, "2026-08-02.md", ["apply-queue"])
    assert body.startswith("SCHEDULER STALE: apply-queue.")
    assert "0 new suggestion(s)" in body


def test_notify_body_names_every_stale_job():
    body = daily._notify_body(1, "d.md", ["daily", "apply-queue"])
    assert "daily, apply-queue" in body


def _wire_common_mocks(monkeypatch, tmp_path, call_order):
    """Patches every daily.main() dependency other than expand/discover with
    a no-op that records its name to call_order, so tests can assert on
    ordering without exercising harvest/score/suggest/digest/sheets/notify
    for real (and never touching data/jobhunt.db or a live Sheet)."""
    db_path = tmp_path / "daily_test.db"
    conn = db_module.connect(db_path)
    # main() always closes conn in its `finally`, so tests that need to
    # inspect state after main() returns reopen a fresh connection on the
    # same file rather than reusing this (by-then-closed) handle.
    monkeypatch.setattr(daily.db_module, "connect", lambda *a, **k: conn)

    monkeypatch.setattr(
        daily,
        "run_harvest",
        lambda *a, **k: call_order.append("harvest"),
    )
    monkeypatch.setattr(daily, "load_rubric", lambda: {})
    monkeypatch.setattr(
        daily,
        "run_score_sweep",
        lambda *a, **k: call_order.append("score_sweep"),
    )
    monkeypatch.setattr(
        daily.dedupe_module,
        "run_dedupe",
        lambda *a, **k: call_order.append("dedupe"),
    )
    monkeypatch.setattr(
        daily,
        "run_suggest",
        lambda *a, **k: call_order.append("suggest") or [],
    )
    digest_path = tmp_path / "2026-08-02.md"
    monkeypatch.setattr(
        daily.digest_module,
        "write_digest",
        lambda *a, **k: call_order.append("digest") or digest_path,
    )
    monkeypatch.setattr(sheets_module, "run_sync", lambda *a, **k: call_order.append("sheets"))
    monkeypatch.setattr(daily.notify_module, "should_notify", lambda *a, **k: False)
    monkeypatch.setattr(daily.notify_module, "notify", lambda *a, **k: None)
    return db_path


def _last_run_summary(conn) -> str:
    row = conn.execute(
        "SELECT summary FROM run_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["summary"] if row else ""


def test_discovery_runs_before_harvest(monkeypatch, tmp_path):
    call_order: list[str] = []
    _wire_common_mocks(monkeypatch, tmp_path, call_order)
    monkeypatch.setattr(
        daily.expand_module,
        "run_expand",
        lambda *a, **k: call_order.append("expand")
        or {
            "live_inserted": 1,
            "embed_guess_confirmed": 0,
            "watchlist_guess_confirmed": 0,
            "dead": 2,
        },
    )
    monkeypatch.setattr(
        daily.discover_module,
        "run_discovery",
        lambda *a, **k: call_order.append("discover")
        or {"yc": {"inserted": 1, "promoted": 0, "dead": 1}, "cse": None},
    )
    monkeypatch.setattr(sys, "argv", ["daily.py"])

    daily.main()

    assert call_order.index("expand") < call_order.index("discover")
    assert call_order.index("discover") < call_order.index("harvest")


def test_no_harvest_skips_discovery_entirely(monkeypatch, tmp_path):
    call_order: list[str] = []
    _wire_common_mocks(monkeypatch, tmp_path, call_order)
    monkeypatch.setattr(
        daily.expand_module,
        "run_expand",
        lambda *a, **k: call_order.append("expand") or {},
    )
    monkeypatch.setattr(
        daily.discover_module,
        "run_discovery",
        lambda *a, **k: call_order.append("discover") or {},
    )
    monkeypatch.setattr(sys, "argv", ["daily.py", "--no-harvest"])

    daily.main()

    assert "expand" not in call_order
    assert "discover" not in call_order
    assert "harvest" not in call_order
    assert call_order == ["suggest", "digest"]  # ran the no-harvest path fine


def test_discovery_exception_does_not_block_the_rest_of_the_run(monkeypatch, tmp_path):
    call_order: list[str] = []
    db_path = _wire_common_mocks(monkeypatch, tmp_path, call_order)

    def _boom(*a, **k):
        raise RuntimeError("simplify README fetch failed")

    monkeypatch.setattr(daily.expand_module, "run_expand", _boom)
    monkeypatch.setattr(
        daily.discover_module,
        "run_discovery",
        lambda *a, **k: call_order.append("discover") or {},
    )
    monkeypatch.setattr(sys, "argv", ["daily.py"])

    daily.main()  # must not raise

    assert "discover" not in call_order  # never reached — expand blew up first
    assert call_order == ["harvest", "score_sweep", "dedupe", "suggest", "digest", "sheets"]

    conn = _real_db_connect(db_path)
    try:
        assert "discovery failed: simplify README fetch failed" in _last_run_summary(conn)
    finally:
        conn.close()


def test_discovery_probe_limits_are_passed_through(monkeypatch, tmp_path):
    call_order: list[str] = []
    _wire_common_mocks(monkeypatch, tmp_path, call_order)
    expand_calls = []
    discover_calls = []
    monkeypatch.setattr(
        daily.expand_module,
        "run_expand",
        lambda *a, **k: expand_calls.append(k)
        or {
            "live_inserted": 0,
            "embed_guess_confirmed": 0,
            "watchlist_guess_confirmed": 0,
            "dead": 0,
        },
    )
    monkeypatch.setattr(
        daily.discover_module,
        "run_discovery",
        lambda *a, **k: discover_calls.append(k) or {},
    )
    monkeypatch.setattr(sys, "argv", ["daily.py"])

    daily.main()

    assert expand_calls == [
        {"limit": daily.DAILY_EXPAND_PROBE_LIMIT, "guess_watchlist": True}
    ]
    assert discover_calls == [
        {
            "source": "all",
            "limit": daily.DAILY_DISCOVER_PROBE_LIMIT,
            "dry_run": False,
        }
    ]
    assert daily.DAILY_EXPAND_PROBE_LIMIT == 100
    assert daily.DAILY_DISCOVER_PROBE_LIMIT == 150
