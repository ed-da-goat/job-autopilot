"""Slice 3: per-job resume tailoring.

Every OpenRouter call is mocked via monkeypatch — no real spend, per repo
convention (see tests/test_openrouter.py). These tests prove the spend gates
hold, the JSON contract is enforced fail-closed, and a rejected payload still
records the money that was actually spent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunt import applypack as applypack_module
from jobhunt import db as db_module
from jobhunt import spend as spend_module
from jobhunt.connectors import openrouter as openrouter_module
from jobhunt.resume import render as render_module
from jobhunt.resume import tailor as tailor_module
from jobhunt.resume.validate import ResumeValidationError
from tests.test_resume_render import FIXTURE_YAML, _chromium_available


@pytest.fixture
def conn(tmp_path: Path):
    connection = db_module.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def resume_path(tmp_path: Path) -> Path:
    path = tmp_path / "resume.yaml"
    path.write_text(FIXTURE_YAML, encoding="utf-8")
    return path


def _insert_posting(
    conn,
    company: str = "Acme Capital",
    title: str = "Fintech Engineer",
    source_posting_id: str = "p1",
) -> int:
    raw = json.dumps({"content": "We want Python, Postgres, and finance domain knowledge."})
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, status, raw_json, first_seen_at, last_seen_at)
           VALUES ('greenhouse', ?, ?, ?, 'active', ?, '', '')""",
        (source_posting_id, company, title, raw),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM postings WHERE source_posting_id=?", (source_posting_id,)
    ).fetchone()["id"]


GOOD_PAYLOAD = {
    "summary": "Finance major who ships AI systems.",
    "bullet_order": ["f755-1", "f755-2", "ws-1"],
    "bullet_rewrites": {"ws-1": "Founded an AI consulting company that sells a private AI employee."},
    "dropped": ["ws-2"],
    "rationale": "Fintech posting, so the wealth-management work leads.",
}


def _fake_result(text: str, cost_usd: float = 0.0092) -> dict:
    return {
        "text": text,
        "tokens_in": 4200,
        "tokens_out": 300,
        "cost_usd": cost_usd,
        "model": "anthropic/claude-sonnet-4.5",
    }


def _mock_complete(monkeypatch, text: str, cost_usd: float = 0.0092) -> None:
    monkeypatch.setattr(
        openrouter_module,
        "complete",
        lambda *args, **kwargs: _fake_result(text, cost_usd),
    )


# --- schema / migration -----------------------------------------------------


def test_drafts_accepts_resume_kind(conn) -> None:
    """v2 migration: the CHECK constraint had to be widened for kind='resume'."""
    posting_id = _insert_posting(conn)
    conn.execute(
        """INSERT INTO drafts (posting_id, kind, content, model, tokens_in, tokens_out,
                               cost_usd, edited, created_at)
           VALUES (?, 'resume', '{}', 'm', 1, 1, 0.0, 0, '')""",
        (posting_id,),
    )
    conn.commit()
    assert conn.execute("SELECT count(*) c FROM drafts WHERE kind='resume'").fetchone()["c"] == 1


def test_drafts_still_rejects_an_unknown_kind(conn) -> None:
    import sqlite3

    posting_id = _insert_posting(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO drafts (posting_id, kind, content, model, tokens_in, tokens_out,
                                   cost_usd, edited, created_at)
               VALUES (?, 'nonsense', '{}', 'm', 1, 1, 0.0, 0, '')""",
            (posting_id,),
        )


def test_fresh_db_is_at_schema_version_2(conn) -> None:
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION


# --- spend gates ------------------------------------------------------------


def test_spend_gate_blocks_and_writes_nothing(conn, resume_path, monkeypatch) -> None:
    """Verify step 4: with SPEND_OK unset, no draft and no spend_ledger row."""
    monkeypatch.delenv("SPEND_OK", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    posting_id = _insert_posting(conn)

    with pytest.raises(openrouter_module.SpendGateError):
        tailor_module.tailor_resume(conn, posting_id, variant="finance_ai", resume_path=resume_path)

    assert conn.execute("SELECT count(*) c FROM drafts").fetchone()["c"] == 0
    assert conn.execute("SELECT count(*) c FROM spend_ledger").fetchone()["c"] == 0
    assert conn.execute("SELECT count(*) c FROM artifacts").fetchone()["c"] == 0


def test_daily_cap_blocks_before_the_call(conn, resume_path, monkeypatch) -> None:
    monkeypatch.setenv("SPEND_OK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("JOBHUNT_DAILY_CAP_USD", "0.0")

    def _explode(*args, **kwargs):
        raise AssertionError("openrouter.complete must not be reached when over cap")

    monkeypatch.setattr(openrouter_module, "complete", _explode)
    posting_id = _insert_posting(conn)

    with pytest.raises(spend_module.SpendCapError):
        tailor_module.tailor_resume(conn, posting_id, variant="finance_ai", resume_path=resume_path)

    assert conn.execute("SELECT count(*) c FROM spend_ledger").fetchone()["c"] == 0


def test_unknown_variant_never_calls_the_model(conn, resume_path, monkeypatch) -> None:
    monkeypatch.setenv("SPEND_OK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def _explode(*args, **kwargs):
        raise AssertionError("openrouter.complete must not be reached for a bad variant")

    monkeypatch.setattr(openrouter_module, "complete", _explode)
    posting_id = _insert_posting(conn)

    with pytest.raises(KeyError):
        tailor_module.tailor_resume(conn, posting_id, variant="nope", resume_path=resume_path)


# --- the happy path ---------------------------------------------------------


@pytest.mark.skipif(
    not _chromium_available(),
    reason="chromium not installed; run `uv run playwright install chromium`",
)
def test_tailor_writes_draft_spend_artifact_and_pdf(conn, resume_path, monkeypatch) -> None:
    monkeypatch.setenv("SPEND_OK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    _mock_complete(monkeypatch, json.dumps(GOOD_PAYLOAD))
    posting_id = _insert_posting(conn)

    result = tailor_module.tailor_resume(
        conn, posting_id, variant="finance_ai", resume_path=resume_path
    )

    assert conn.execute("SELECT count(*) c FROM drafts WHERE kind='resume'").fetchone()["c"] == 1
    ledger = conn.execute("SELECT count(*) c, sum(cost_usd) s FROM spend_ledger").fetchone()
    assert ledger["c"] == 1
    assert ledger["s"] == pytest.approx(0.0092)
    assert result["cost_usd"] < 0.02

    artifact = conn.execute("SELECT * FROM artifacts").fetchone()
    assert artifact["kind"] == "resume_pdf"
    assert artifact["posting_id"] == posting_id
    assert artifact["created_at"] and artifact["updated_at"]

    pdf = Path(result["pdf_path"])
    assert pdf.exists()
    assert pdf.read_bytes().startswith(b"%PDF")
    assert pdf.name == f"{posting_id}-finance_ai.pdf"
    pdf.unlink()


def test_tailor_applies_the_overlay_to_the_render(conn, resume_path, monkeypatch) -> None:
    """The model reorders and re-words by id; the renderer applies it."""
    data = render_module.load_resume(resume_path)
    context = render_module.build_context(data, "founder_engineer", GOOD_PAYLOAD)

    assert context["summary"] == "Finance major who ships AI systems."
    assert [block["company"] for block in context["experience"]] == [
        "Example Co",
        "Example Ventures LLC",
    ]
    ws_bullets = context["experience"][1]["bullets"]
    assert len(ws_bullets) == 1
    assert "sells a private AI employee" in ws_bullets[0]["text"]


# --- fail-closed validation -------------------------------------------------


def test_invented_bullet_id_is_rejected_but_the_spend_is_recorded(
    conn, resume_path, monkeypatch
) -> None:
    """The call already cost money, so the ledger must record it even on reject."""
    monkeypatch.setenv("SPEND_OK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    bad = dict(GOOD_PAYLOAD, bullet_order=["ws-1", "goldman-sachs-1"])
    _mock_complete(monkeypatch, json.dumps(bad))
    posting_id = _insert_posting(conn)

    with pytest.raises(ResumeValidationError, match="goldman-sachs-1"):
        tailor_module.tailor_resume(conn, posting_id, variant="finance_ai", resume_path=resume_path)

    assert conn.execute("SELECT count(*) c FROM drafts WHERE kind='resume'").fetchone()["c"] == 1
    assert conn.execute("SELECT count(*) c FROM spend_ledger").fetchone()["c"] == 1
    # No PDF and no artifact row: the render never ran.
    assert conn.execute("SELECT count(*) c FROM artifacts").fetchone()["c"] == 0


def test_completed_sale_claim_is_rejected(conn, resume_path, monkeypatch) -> None:
    monkeypatch.setenv("SPEND_OK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    bad = dict(GOOD_PAYLOAD, summary="Founder who sold his AI company in a 2026 exit.")
    _mock_complete(monkeypatch, json.dumps(bad))
    posting_id = _insert_posting(conn)

    with pytest.raises(ResumeValidationError):
        tailor_module.tailor_resume(conn, posting_id, variant="finance_ai", resume_path=resume_path)

    assert conn.execute("SELECT count(*) c FROM artifacts").fetchone()["c"] == 0


def test_non_json_response_is_rejected(conn, resume_path, monkeypatch) -> None:
    monkeypatch.setenv("SPEND_OK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    _mock_complete(monkeypatch, "Here is your tailored resume:\n\nALEX RIVERA\n...")
    posting_id = _insert_posting(conn)

    with pytest.raises(ResumeValidationError, match="not valid JSON"):
        tailor_module.tailor_resume(conn, posting_id, variant="finance_ai", resume_path=resume_path)


def test_fenced_json_is_tolerated() -> None:
    fenced = "```json\n" + json.dumps(GOOD_PAYLOAD) + "\n```"
    assert tailor_module.parse_response(fenced)["bullet_order"] == GOOD_PAYLOAD["bullet_order"]


# --- prompt construction ----------------------------------------------------


def test_prompt_withholds_contact_details_from_the_model(resume_path) -> None:
    data = render_module.load_resume(resume_path)
    rendered = tailor_module.resume_yaml_for_prompt(data)
    assert "test@example.com" not in rendered
    assert "ws-1" in rendered
    assert "f755-1" in rendered


def test_messages_confine_the_posting_to_the_user_message(conn, resume_path) -> None:
    posting_id = _insert_posting(conn)
    posting_row = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
    data = render_module.load_resume(resume_path)
    system, user = tailor_module.build_messages(
        posting_row, None, "profile text", tailor_module.resume_yaml_for_prompt(data)
    )
    assert "Security boundary" in system
    assert "Return a single JSON object" in system
    assert "finance domain knowledge" in user
    assert "finance domain knowledge" not in system


def test_prompt_template_renders_json_braces(resume_path) -> None:
    """The template uses {{ }} so .format() leaves literal JSON braces."""
    conn = None
    data = render_module.load_resume(resume_path)
    rendered = tailor_module.build_prompt(
        {"company": "Acme", "title": "Engineer", "raw_json": "{}", "source": "greenhouse"},
        None,
        "profile",
        tailor_module.resume_yaml_for_prompt(data),
    )
    assert '"bullet_order"' in rendered
    assert "{{" not in rendered
    assert conn is None


# --- applypack repoint ------------------------------------------------------


def test_applypack_falls_back_when_no_tailored_resume(conn) -> None:
    posting_id = _insert_posting(conn)
    assert applypack_module.resume_path_for(conn, posting_id) == applypack_module.RESUME_PATH


def test_applypack_prefers_the_tailored_resume(conn, tmp_path, monkeypatch) -> None:
    posting_id = _insert_posting(conn)
    pdf = tmp_path / "tailored.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    tailor_module.record_artifact(conn, posting_id, pdf)
    assert applypack_module.resume_path_for(conn, posting_id) == str(pdf)


def test_applypack_falls_back_when_the_recorded_file_is_gone(conn, tmp_path) -> None:
    posting_id = _insert_posting(conn)
    tailor_module.record_artifact(conn, posting_id, tmp_path / "deleted.pdf")
    assert applypack_module.resume_path_for(conn, posting_id) == applypack_module.RESUME_PATH


def test_latest_artifact_wins(conn, tmp_path) -> None:
    posting_id = _insert_posting(conn)
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    for path in (first, second):
        path.write_bytes(b"%PDF-1.4 fake")
    tailor_module.record_artifact(conn, posting_id, first)
    tailor_module.record_artifact(conn, posting_id, second)
    assert tailor_module.latest_resume_artifact(conn, posting_id) == str(second)


# --- batch queue repoint ----------------------------------------------------
#
# The queue used to hand one batch-wide resume to every tab, so a tailored
# render could exist and still never reach an application. These pin the
# caller's fallback being honoured rather than quietly replaced by RESUME_PATH.


def test_resume_path_for_honours_an_explicit_fallback(conn) -> None:
    posting_id = _insert_posting(conn)
    assert applypack_module.resume_path_for(conn, posting_id, "batch/standing.pdf") == (
        "batch/standing.pdf"
    )


def test_resume_path_for_honours_a_none_fallback(conn) -> None:
    """apply-queue passes None when its own standing resume is missing from
    disk. Tailoring must not resurrect an attachment the caller ruled out."""
    posting_id = _insert_posting(conn)
    assert applypack_module.resume_path_for(conn, posting_id, None) is None


def test_tailored_render_still_beats_an_explicit_fallback(conn, tmp_path) -> None:
    posting_id = _insert_posting(conn)
    pdf = tmp_path / "tailored.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    tailor_module.record_artifact(conn, posting_id, pdf)
    assert applypack_module.resume_path_for(conn, posting_id, "batch/standing.pdf") == str(pdf)


def test_missing_recorded_file_falls_back_to_the_caller_value(conn, tmp_path) -> None:
    posting_id = _insert_posting(conn)
    tailor_module.record_artifact(conn, posting_id, tmp_path / "deleted.pdf")
    assert applypack_module.resume_path_for(conn, posting_id, "batch/standing.pdf") == (
        "batch/standing.pdf"
    )


class _FakePage:
    def goto(self, *args, **kwargs) -> None:
        return None


class _FakeContext:
    def new_page(self) -> _FakePage:
        return _FakePage()


def test_run_queue_attaches_a_different_resume_per_posting(conn, tmp_path, monkeypatch) -> None:
    """The regression this repoint exists for: two postings in one batch, only
    one of them tailored, must not receive the same attachment."""
    from jobhunt.apply_assist import driver as driver_module
    from jobhunt.apply_assist import fill as fill_module
    from jobhunt.apply_assist import queue as queue_module
    from jobhunt.apply_assist.schema import TriageDigest

    tailored_id = _insert_posting(conn, company="Tailored Co", source_posting_id="q1")
    plain_id = _insert_posting(conn, company="Plain Co", source_posting_id="q2")
    pdf = tmp_path / "tailored.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    tailor_module.record_artifact(conn, tailored_id, pdf)

    seen: dict[int, str | None] = {}

    def fake_run_assist(page, posting_id, url, answers, resume, scan_only=False, **kwargs):
        # **kwargs absorbs run_queue's conn/budget/mapper_sink passthrough —
        # this test isn't exercising the field-mapper, just resume routing.
        seen[posting_id] = resume
        return TriageDigest(
            posting_id=posting_id, url=url, ats="greenhouse", filled=[], residual=[], skipped=[]
        )

    monkeypatch.setattr(driver_module, "open_browser", lambda: (None, _FakeContext(), False))
    monkeypatch.setattr(fill_module, "run_assist", fake_run_assist)

    rows = [
        {"posting_id": tailored_id, "url": "https://boards.greenhouse.io/acme/jobs/1"},
        {"posting_id": plain_id, "url": "https://boards.greenhouse.io/acme/jobs/2"},
    ]
    queue_module.run_queue(conn, rows, {}, "batch/standing.pdf")

    assert seen[tailored_id] == str(pdf)
    assert seen[plain_id] == "batch/standing.pdf"
