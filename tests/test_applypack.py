"""Tests for jobhunt.applypack (Apply Pack assembly — no LLM calls) and the
/pack/{posting_id} web route."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhunt import applypack as applypack_module
from jobhunt import db as db_module
from jobhunt.web.app import app, get_db_path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _insert_posting(
    conn,
    spid: str = "p1",
    company: str = "Acme Corp",
    title: str = "Backend Engineer",
    location: str = "Remote",
    url: str = "https://example.com/jobs/p1",
) -> int:
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, location, url, status, first_seen_at, last_seen_at)
           VALUES ('greenhouse', ?, ?, ?, ?, ?, 'active', '', '')""",
        (spid, company, title, location, url),
    )
    conn.commit()
    return conn.execute("SELECT id FROM postings WHERE source_posting_id=?", (spid,)).fetchone()["id"]


def _insert_score(conn, posting_id: int, rubric_version: int = 1, total: int = 80) -> None:
    conn.execute(
        """INSERT INTO scores
           (posting_id, rubric_version, total, components, missing_evidence, excluded, scored_at)
           VALUES (?, ?, ?, ?, '[]', 0, '')""",
        (posting_id, rubric_version, total, json.dumps({"skill_match": 25, "founder_leverage": 2})),
    )
    conn.commit()


def _insert_draft(conn, posting_id: int, kind: str, content: str) -> None:
    conn.execute(
        """INSERT INTO drafts (posting_id, kind, content, model, tokens_in, tokens_out, cost_usd, edited, created_at)
           VALUES (?, ?, ?, 'm', 10, 10, 0.001, 0, ?)""",
        (posting_id, kind, content, datetime.now(UTC).isoformat()),
    )
    conn.commit()


# ------------------------------------------------------------- load_answers


def test_load_answers_falls_back_to_template_and_flags_fill(tmp_path):
    template = tmp_path / "answers.local.template.md"
    template.write_text(
        "full_name: Alex Rivera\nphone: [FILL]\ngithub: [FILL]\n", encoding="utf-8"
    )
    missing_filled = tmp_path / "answers.local.md"  # never created

    result = applypack_module.load_answers(filled_path=missing_filled, template_path=template)

    assert result["source"] == "template"
    assert result["answers"]["full_name"] == "Alex Rivera"
    assert set(result["unfilled"]) == {"phone", "github"}


def test_load_answers_prefers_filled_copy(tmp_path):
    template = tmp_path / "answers.local.template.md"
    template.write_text("phone: [FILL]\n", encoding="utf-8")
    filled = tmp_path / "answers.local.md"
    filled.write_text("phone: 555-0100\n", encoding="utf-8")

    result = applypack_module.load_answers(filled_path=filled, template_path=template)

    assert result["source"] == "filled"
    assert result["answers"]["phone"] == "555-0100"
    assert result["unfilled"] == []


def test_committed_template_has_no_syntax_surprises():
    """The real committed template parses and has the documented keys."""
    result = applypack_module.load_answers(
        filled_path=Path("/nonexistent/answers.local.md"),
        template_path=applypack_module.ANSWERS_TEMPLATE_PATH,
    )
    assert result["source"] == "template"
    for key in ("full_name", "email", "linkedin", "degree"):
        assert key in result["answers"]
    assert "phone" in result["unfilled"]  # marked [FILL] in the committed template


# ------------------------------------------------------------- build_pack


def test_build_pack_contains_posting_fields_and_standard_answers(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn, company="Acme Corp", title="Backend Engineer")
        _insert_score(conn, posting_id)
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """INSERT INTO application_answers
               (semantic_key, value, created_at, updated_at)
               VALUES ('full_name', 'Alex Rivera', ?, ?)""",
            (now, now),
        )
        conn.execute(
            """INSERT INTO application_answers
               (semantic_key, value, created_at, updated_at)
               VALUES ('gender', 'must never enter an apply pack', ?, ?)""",
            (now, now),
        )
        conn.commit()

        markdown = applypack_module.build_pack(conn, posting_id)

        assert "Acme Corp" in markdown
        assert "Backend Engineer" in markdown
        assert "https://example.com/jobs/p1" in markdown
        assert "## Standard Answers" in markdown
        assert "Alex Rivera" in markdown
        assert "must never enter an apply pack" not in markdown
        assert "## Education" in markdown
        assert "Example State" in markdown
        assert "## Work History" in markdown
        assert "Example Co" in markdown
        assert "Example Ventures" in markdown
        assert "## Checklist" in markdown
        assert f"move {posting_id} applied" in markdown
    finally:
        conn.close()


def test_build_pack_why_you_angle_from_score(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        _insert_score(conn, posting_id)

        markdown = applypack_module.build_pack(conn, posting_id)

        assert "Why this one:" in markdown
        assert "not scored yet" not in markdown
    finally:
        conn.close()


def test_build_pack_unscored_posting_shows_not_scored(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)

        markdown = applypack_module.build_pack(conn, posting_id)

        assert "(not scored yet)" in markdown
    finally:
        conn.close()


def test_build_pack_cover_draft_first_paragraph_extracted(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn, company="Acme Corp")
        _insert_score(conn, posting_id)
        _insert_draft(
            conn,
            posting_id,
            "cover",
            "First paragraph about why Acme.\nStill first paragraph.\n\nSecond paragraph, should not appear.",
        )

        markdown = applypack_module.build_pack(conn, posting_id)

        assert "First paragraph about why Acme." in markdown
        assert "Second paragraph, should not appear." not in markdown
    finally:
        conn.close()


def test_build_pack_cover_draft_skips_salutation_only_paragraph(tmp_path):
    """qwen2.5:7b puts a blank line after the salutation; the salutation-only
    paragraph must not become the "Why company?" answer (posting 74343, draft 33)."""
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn, company="Taktile")
        _insert_score(conn, posting_id)
        _insert_draft(
            conn,
            posting_id,
            "cover",
            "Dear Hiring Manager,\n\n"
            "I am writing to apply for the Forward Deployed Engineer position at Taktile.\n\n"
            "As a founder-engineer, second paragraph should not appear.",
        )

        markdown = applypack_module.build_pack(conn, posting_id)

        assert "**Why Taktile?**\nDear Hiring Manager," not in markdown
        assert "I am writing to apply for the Forward Deployed Engineer position" in markdown
        assert "second paragraph should not appear" not in markdown
    finally:
        conn.close()


def test_first_paragraph_salutation_variants():
    body = "Substantive first paragraph."
    for salutation in ("Dear Hiring Manager,", "Hi Anna,", "To Whom It May Concern:"):
        content = f"{salutation}\n\n{body}"
        assert applypack_module._first_paragraph(content) == body
    # A paragraph that merely starts with a greeting but carries content stays.
    assert (
        applypack_module._first_paragraph("Hi there, quick note about the role.")
        == "Hi there, quick note about the role."
    )
    # All-salutation content still returns something rather than empty.
    assert applypack_module._first_paragraph("Dear Hiring Manager,") == "Dear Hiring Manager,"


def test_build_pack_tailoring_positioning_angle_extracted(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        _insert_score(conn, posting_id)
        _insert_draft(
            conn,
            posting_id,
            "tailoring",
            "1. Lead-with facts: ...\n4. Positioning angle: Forward-deployed engineer who ships.\n",
        )

        markdown = applypack_module.build_pack(conn, posting_id)

        assert "Forward-deployed engineer who ships." in markdown
    finally:
        conn.close()


def test_build_pack_tailoring_header_then_newline_content(tmp_path):
    """qwen2.5:7b renders "## Positioning angle:" as a header with the content
    on the NEXT line; the extractor must not capture ":" (posting 74343, draft 32)."""
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        _insert_score(conn, posting_id)
        _insert_draft(
            conn,
            posting_id,
            "tailoring",
            "## Lead-with facts:\n"
            "1. **Founder-Engineer of Example Ventures LLC**\n\n"
            "## Positioning angle:\n"
            "Founder-engineer who has built and scaled an AI-driven startup.\n",
        )

        markdown = applypack_module.build_pack(conn, posting_id)

        assert "Founder-engineer who has built and scaled an AI-driven startup." in markdown
        assert "**Tell us about yourself**\n:" not in markdown
    finally:
        conn.close()


def test_positioning_angle_junk_captures_rejected():
    # Header with blank line before the content: skip the blank, take the content.
    assert (
        applypack_module._positioning_angle_from_tailoring(
            "## Positioning angle:\n\nShips fast, owns outcomes.\n"
        )
        == "Ships fast, owns outcomes."
    )
    # Header immediately followed by the next section: no junk, no wrong section.
    assert (
        applypack_module._positioning_angle_from_tailoring(
            "## Positioning angle:\n\n## Notes:\ncontent of another section\n"
        )
        is None
    )
    # Markdown-residue capture ("**") is junk, falls through to the next line.
    assert (
        applypack_module._positioning_angle_from_tailoring(
            "**Positioning angle:**\nQuant-minded builder.\n"
        )
        == "Quant-minded builder."
    )


def test_build_pack_missing_drafts_shows_prep_hint(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        _insert_score(conn, posting_id)

        markdown = applypack_module.build_pack(conn, posting_id)

        assert f"run `prep {posting_id}`" in markdown
        # falls back to PROFILE.md's positioning one-liner when no tailoring draft
        assert "Founder-engineer with a business degree." in markdown
    finally:
        conn.close()


def test_build_pack_outreach_draft_referenced(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        _insert_score(conn, posting_id)
        _insert_draft(conn, posting_id, "outreach", "Hi there, quick note about the role.")

        markdown = applypack_module.build_pack(conn, posting_id)

        assert "Hi there, quick note about the role." in markdown
    finally:
        conn.close()


def test_build_pack_unknown_posting_raises(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        with pytest.raises(ValueError):
            applypack_module.build_pack(conn, 999999)
    finally:
        conn.close()


# ------------------------------------------------------------- write_pack


def test_write_pack_writes_file_under_apply_packs(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn, company="Acme Corp!!")
        _insert_score(conn, posting_id)

        packs_dir = tmp_path / "apply_packs"
        monkeypatch.setattr(applypack_module, "APPLY_PACKS_DIR", packs_dir)

        path = applypack_module.write_pack(conn, posting_id)

        assert path.exists()
        assert path.parent == packs_dir
        assert path.name == f"{posting_id}-acme-corp.md"
        assert "Acme Corp!!" in path.read_text(encoding="utf-8")
    finally:
        conn.close()


# ------------------------------------------------------------- web route


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "t.db"
    app.dependency_overrides[get_db_path] = lambda: db_path
    try:
        yield TestClient(app), db_path
    finally:
        app.dependency_overrides.clear()


def test_pack_route_200(client):
    http, db_path = client
    conn = db_module.connect(db_path)
    posting_id = _insert_posting(conn, company="Acme Corp", title="Backend Engineer")
    _insert_score(conn, posting_id)
    conn.close()

    resp = http.get(f"/pack/{posting_id}")

    assert resp.status_code == 200
    assert "Acme Corp" in resp.text
    assert "Apply Pack" in resp.text


def test_pack_route_shows_fill_banner_when_unfilled_answers(client):
    http, db_path = client
    conn = db_module.connect(db_path)
    posting_id = _insert_posting(conn)
    _insert_score(conn, posting_id)
    conn.close()

    resp = http.get(f"/pack/{posting_id}")

    assert resp.status_code == 200
    assert "Still unfilled" in resp.text
    assert "[FILL" in resp.text


def test_pack_route_404_unknown_posting(client):
    http, db_path = client

    resp = http.get("/pack/999999")

    assert resp.status_code == 404


def test_detail_page_links_to_pack(client):
    http, db_path = client
    conn = db_module.connect(db_path)
    posting_id = _insert_posting(conn)
    conn.close()

    resp = http.get(f"/posting/{posting_id}")

    assert resp.status_code == 200
    assert f"/pack/{posting_id}" in resp.text


# ------------------------------------------------------------- gitignore


def test_apply_packs_dir_is_gitignored():
    result = subprocess.run(
        ["git", "check-ignore", "data/apply_packs/10524-acme.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_answers_local_md_is_gitignored():
    result = subprocess.run(
        ["git", "check-ignore", "data/answers.local.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
