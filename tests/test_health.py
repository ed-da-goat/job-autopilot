from __future__ import annotations

from datetime import UTC, datetime

from jobhunt import db, health


def test_health_uses_last_successful_historical_sweep_after_migration(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    try:
        now = datetime.now(UTC)
        conn.execute(
            "INSERT INTO source_state (source, baseline_at) VALUES ('greenhouse', ?)",
            (now.isoformat(),),
        )
        conn.execute(
            """INSERT INTO sweeps
               (source, finished_at, boards_ok) VALUES ('greenhouse', ?, 1)""",
            (now.isoformat(),),
        )
        conn.commit()
        data = health.build_health(conn, now=now)
        assert data["sources"][0]["last_success_at"] == now.isoformat()
        assert data["sources"][0]["stale"] is False
    finally:
        conn.close()
