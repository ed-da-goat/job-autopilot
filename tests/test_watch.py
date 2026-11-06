from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from jobhunt import db as db_module
from jobhunt import watch as watch_module
from jobhunt.rubric import load_rubric


def _conn(tmp_path):
    return db_module.connect(tmp_path / "t.db")


def _posting(conn, spid, *, first_seen, title="Senior Backend Engineer, AI Agents", source="greenhouse", location="San Francisco, CA"):
    raw = json.dumps({"content": "Build agentic backend systems in Python with FastAPI and Postgres. 2+ years experience."})
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, location, url, status, first_seen_at, last_seen_at, raw_json)
           VALUES (?, ?, 'Acme', ?, ?, 'https://x/' || ?, 'active', ?, ?, ?)""",
        (source, spid, title, location, spid, first_seen, first_seen, raw),
    )
    conn.commit()
    return conn.execute("SELECT id FROM postings WHERE source_posting_id=?", (spid,)).fetchone()["id"]


class _Pusher:
    def __init__(self):
        self.calls = []

    def __call__(self, title, body, url, priority):
        self.calls.append((title, body, url, priority))


def test_first_tick_baselines_without_replaying_history(tmp_path):
    conn = _conn(tmp_path)
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    _posting(conn, "old", first_seen=(now - timedelta(days=3)).isoformat())
    pusher = _Pusher()
    result = watch_module.run_watch(
        conn, load_rubric(), harvest=False, pusher=pusher, state_path=tmp_path / "s.json", now=now, min_score=0
    )
    assert result.new_postings == 0
    assert pusher.calls == []
    assert json.loads((tmp_path / "s.json").read_text())["last_tick"] == now.isoformat()


def test_second_tick_scores_and_pushes_only_new_once(tmp_path):
    conn = _conn(tmp_path)
    state = tmp_path / "s.json"
    t0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    watch_module.run_watch(conn, load_rubric(), harvest=False, pusher=_Pusher(), state_path=state, now=t0, min_score=0)

    t1 = t0 + timedelta(minutes=20)
    pid = _posting(conn, "new1", first_seen=(t0 + timedelta(minutes=5)).isoformat())
    _posting(conn, "lever-new", first_seen=(t0 + timedelta(minutes=6)).isoformat(), source="lever")
    _posting(conn, "workday-new", first_seen=(t0 + timedelta(minutes=6)).isoformat(), source="workday")
    pusher = _Pusher()
    result = watch_module.run_watch(conn, load_rubric(), harvest=False, pusher=pusher, state_path=state, now=t1, min_score=0)

    # workday is not a watched source; the two board postings are scored and pushed
    assert result.new_postings == 2
    assert result.scored == 2
    assert len(pusher.calls) == 2
    assert pusher.calls[0][0].startswith("Acme: ")
    assert conn.execute("SELECT COUNT(*) FROM scores WHERE posting_id=?", (pid,)).fetchone()[0] == 1
    assert str(pid) in json.loads(state.read_text())["notified"]

    # a third tick with an overlapping window must not re-push the same ids
    pusher2 = _Pusher()
    result2 = watch_module.run_watch(
        conn, load_rubric(), harvest=False, pusher=pusher2, state_path=state, now=t1 + timedelta(minutes=20), min_score=0
    )
    assert result2.new_postings == 0
    assert pusher2.calls == []


def test_threshold_filters_low_scores(tmp_path):
    conn = _conn(tmp_path)
    state = tmp_path / "s.json"
    t0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    watch_module.run_watch(conn, load_rubric(), harvest=False, pusher=_Pusher(), state_path=state, now=t0)
    _posting(conn, "x", first_seen=(t0 + timedelta(minutes=1)).isoformat())
    pusher = _Pusher()
    result = watch_module.run_watch(
        conn, load_rubric(), harvest=False, pusher=pusher, state_path=state, now=t0 + timedelta(minutes=20), min_score=101
    )
    assert result.scored == 1
    assert result.hits == [] and pusher.calls == []


def test_push_hits_caps_and_summarizes():
    hits = [watch_module.Hit(i, "Co", f"Role {i}", None, f"https://x/{i}", 90 - i) for i in range(14)]
    pusher = _Pusher()
    sent = watch_module.push_hits(hits, pusher)
    assert sent == watch_module.MAX_PUSHES_PER_TICK + 1
    assert pusher.calls[0][3] == "high"  # score 90
    assert pusher.calls[-1][0] == "+4 more new matches"


def test_ntfy_push_without_topic_does_not_post(monkeypatch, capsys):
    monkeypatch.delenv("JOBHUNT_NTFY_TOPIC", raising=False)
    monkeypatch.setattr(watch_module, "DEFAULT_TOPIC_PATH", watch_module.Path("/nonexistent/ntfy"))
    monkeypatch.setattr(watch_module.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("posted")))
    watch_module.ntfy_push("t", "b", None)
    assert "NO TOPIC" in capsys.readouterr().err


def test_ntfy_push_posts_to_topic(monkeypatch):
    monkeypatch.setenv("JOBHUNT_NTFY_TOPIC", "abc123")
    monkeypatch.setenv("JOBHUNT_NTFY_URL", "https://ntfy.example/")
    seen = {}

    class _Resp:
        status_code = 200
        text = ""

    def _post(url, data=None, headers=None, timeout=None):
        seen.update(url=url, data=data, headers=headers)
        return _Resp()

    monkeypatch.setattr(watch_module.requests, "post", _post)
    watch_module.ntfy_push("Acme: Eng", "score 80", "https://job", "high")
    assert seen["url"] == "https://ntfy.example/abc123"
    assert seen["data"] == b"score 80"
    assert seen["headers"]["Title"] == "Acme: Eng"
    assert seen["headers"]["Click"] == "https://job"
    assert seen["headers"]["Priority"] == "high"


def test_tick_lock_second_holder_raises(tmp_path):
    import pytest

    lock = tmp_path / "watch.lock"
    with watch_module.tick_lock(lock):
        with pytest.raises(watch_module.WatchTickActiveError):
            with watch_module.tick_lock(lock):
                pass
    with watch_module.tick_lock(lock):  # released after exit
        pass


def test_daily_in_flight(tmp_path):
    conn = _conn(tmp_path)
    now = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)
    assert watch_module.daily_in_flight(conn, now) is False
    conn.execute("INSERT INTO run_log (job, started_at, finished_at, ok) VALUES ('daily', ?, ?, 1)",
                 ((now - timedelta(hours=1)).isoformat(), now.isoformat()))
    conn.execute("INSERT INTO run_log (job, started_at) VALUES ('watch', ?)", (now.isoformat(),))
    conn.execute("INSERT INTO run_log (job, started_at) VALUES ('daily', ?)", ((now - timedelta(hours=13)).isoformat(),))
    conn.commit()
    assert watch_module.daily_in_flight(conn, now) is False  # finished / other job / stale
    conn.execute("INSERT INTO run_log (job, started_at) VALUES ('daily', ?)", ((now - timedelta(minutes=5)).isoformat(),))
    conn.commit()
    assert watch_module.daily_in_flight(conn, now) is True
