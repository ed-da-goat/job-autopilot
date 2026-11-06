from __future__ import annotations

from jobhunt import db as db_module

import daily


def _seed_posting(conn, posting_id: int, score: int = 80) -> None:
    now = "2026-08-04T00:00:00"
    conn.execute(
        """
        INSERT INTO postings
            (id, source, source_posting_id, company, title,
             first_seen_at, last_seen_at)
        VALUES (?, 'greenhouse', ?, 'Acme', 'Engineer', ?, ?)
        """,
        (posting_id, str(posting_id), now, now),
    )
    conn.execute(
        """
        INSERT INTO scores (posting_id, rubric_version, total)
        VALUES (?, 1, ?)
        """,
        (posting_id, score),
    )
    conn.execute(
        """
        INSERT INTO funnel (posting_id, stage, created_at, updated_at)
        VALUES (?, 'suggested', ?, ?)
        """,
        (posting_id, now, now),
    )
    conn.commit()


def test_gate_proceeds_when_local_endpoint_configured(monkeypatch, tmp_path):
    """A configured local endpoint must satisfy the gate even with no
    SPEND_OK/OPENROUTER_API_KEY set — that's the whole point of free
    unattended auto-prep on a local model."""
    monkeypatch.delenv("SPEND_OK", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("JOBHUNT_LLM_BASE_URL", "http://localhost:11434/v1")

    conn = db_module.connect(tmp_path / "gate_local.db")
    try:
        notes = daily._auto_prep(conn)
    finally:
        conn.close()

    assert notes
    assert notes[0] != "auto-prep: skipped (requires a local model configured via " \
        "JOBHUNT_LLM_BASE_URL, or SPEND_OK=1 + OPENROUTER_API_KEY)"
    # Proceeded past the gate to the "no scores yet" branch (empty db).
    assert notes[0] == "auto-prep: skipped (no scores yet)"


def test_gate_skips_without_local_or_spend_ok(monkeypatch, tmp_path):
    monkeypatch.delenv("SPEND_OK", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("JOBHUNT_LLM_BASE_URL", raising=False)

    conn = db_module.connect(tmp_path / "gate_skip.db")
    try:
        notes = daily._auto_prep(conn)
    finally:
        conn.close()

    assert notes == [
        "auto-prep: skipped (requires a local model configured via "
        "JOBHUNT_LLM_BASE_URL, or SPEND_OK=1 + OPENROUTER_API_KEY)"
    ]


def test_crash_in_generate_packet_is_caught_and_noted(monkeypatch, tmp_path):
    """A dead local Ollama endpoint raises requests.ConnectionError (or
    similar) from generate_packet — _auto_prep must never propagate it."""
    monkeypatch.delenv("SPEND_OK", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("JOBHUNT_LLM_BASE_URL", "http://localhost:11434/v1")

    conn = db_module.connect(tmp_path / "crash.db")
    _seed_posting(conn, 1)

    from jobhunt import prep as prep_module

    def _boom(conn, posting_id):
        raise ConnectionError("dead local endpoint")

    monkeypatch.setattr(prep_module, "generate_packet", _boom)

    try:
        notes = daily._auto_prep(conn)  # must not raise
    finally:
        conn.close()

    assert any("skipped" in note and "ConnectionError" in note for note in notes)
