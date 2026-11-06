"""Slices 0-2: structured resume source, variants, and PDF rendering.

These tests never call a model and never spend. The fixture below is a
miniature stand-in for `resume/resume.yaml` (which is gitignored, so tests may
not depend on it existing) and exercises the same code paths.
"""

from __future__ import annotations

import re
import shutil
import textwrap
from pathlib import Path

import pytest

from jobhunt.resume import render as render_module
from jobhunt.resume.validate import (
    ResumeValidationError,
    scan_banned_phrases,
    validate_tailoring,
)

FIXTURE_YAML = textwrap.dedent(
    """
    contact:
      name: Test Candidate
      email: test@example.com
      linkedin: linkedin.com/in/test
      github: github.com/alex-rivera-example
    education:
      - school: Example State University, College of Business
        location: Atlanta, Georgia
        degree: Bachelor of Science in Business Administration, Finance concentration
        dates: Dec 2026
    experience:
      - id: ws
        company: Example Ventures LLC
        title: Founder & Engineer
        location: Atlanta, Georgia
        dates: 2026 - Present
        bullets:
          - id: ws-1
            text: Founded an AI consulting company that sells an installed private AI employee.
          - id: ws-2
            text: Built and deployed a multi-tenant agent solo.
      - id: f755
        company: Example Co
        title: Wealth Manager Assistant
        location: Woodstock, Georgia
        dates: Jan 2024 - Present
        bullets:
          - id: f755-1
            text: Cut tax preparation time by 30% with AI-assisted due diligence.
          - id: f755-2
            text: Built Excel retirement projection models.
    skills:
      - label: Engineering
        text: Python, FastAPI, Postgres
    projects: []
    variants:
      founder_engineer:
        label: Founder Engineer 2026
        summary: Founder-engineer who ships.
        order: [ws-1, ws-2, f755-1, f755-2]
        drop: []
      finance_ai:
        label: Finance x AI 2026
        summary: Finance major who ships AI systems.
        order: [f755-1, f755-2, ws-1, ws-2]
        drop: []
      trimmed:
        label: Trimmed
        summary: Short one.
        order: [f755-1, ws-1, ws-2]
        drop: [ws-2]
    """
)


@pytest.fixture
def resume_path(tmp_path: Path) -> Path:
    path = tmp_path / "resume.yaml"
    path.write_text(FIXTURE_YAML, encoding="utf-8")
    return path


@pytest.fixture
def resume_data(resume_path: Path) -> dict:
    return render_module.load_resume(resume_path)


def test_load_resume_missing_file_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no structured resume"):
        render_module.load_resume(tmp_path / "absent.yaml")


def test_known_bullet_ids(resume_data: dict) -> None:
    assert render_module.known_bullet_ids(resume_data) == {"ws-1", "ws-2", "f755-1", "f755-2"}


def test_founder_variant_leads_with_ws(resume_data: dict) -> None:
    context = render_module.build_context(resume_data, "founder_engineer")
    assert [block["company"] for block in context["experience"]] == [
        "Example Ventures LLC",
        "Example Co",
    ]


def test_finance_variant_leads_with_755(resume_data: dict) -> None:
    """Ask (c): the finance angle is a reordering of existing facts."""
    context = render_module.build_context(resume_data, "finance_ai")
    assert [block["company"] for block in context["experience"]] == [
        "Example Co",
        "Example Ventures LLC",
    ]
    assert context["summary"] == "Finance major who ships AI systems."


def test_both_variants_carry_identical_facts(resume_data: dict) -> None:
    """One source of facts, two orderings: the bullet set cannot drift apart."""

    def bullet_texts(variant: str) -> set[str]:
        context = render_module.build_context(resume_data, variant)
        return {
            bullet["text"] for block in context["experience"] for bullet in block["bullets"]
        }

    assert bullet_texts("founder_engineer") == bullet_texts("finance_ai")


def test_drop_removes_a_bullet(resume_data: dict) -> None:
    context = render_module.build_context(resume_data, "trimmed")
    rendered_ids = {
        bullet["id"] for block in context["experience"] for bullet in block["bullets"]
    }
    assert "ws-2" not in rendered_ids
    assert rendered_ids == {"f755-1", "ws-1"}


def test_unknown_variant_lists_the_valid_ones(resume_data: dict) -> None:
    with pytest.raises(KeyError, match="finance_ai"):
        render_module.build_context(resume_data, "nope")


def test_variant_referencing_unknown_bullet_fails_closed(resume_data: dict) -> None:
    resume_data["variants"]["founder_engineer"]["order"] = ["ws-1", "invented-9"]
    with pytest.raises(ResumeValidationError, match="invented-9"):
        render_module.build_context(resume_data, "founder_engineer")


def test_gpa_is_never_rendered(resume_data: dict) -> None:
    """Decision 2026-08-01: the 3.3 stays in PROFILE.md, never on the resume."""
    for variant in ("founder_engineer", "finance_ai"):
        html = render_module.render_html(render_module.build_context(resume_data, variant))
        assert "GPA" not in html
        assert "3.3" not in html


def test_no_em_dashes_in_rendered_html(resume_data: dict) -> None:
    for variant in ("founder_engineer", "finance_ai"):
        html = render_module.render_html(render_module.build_context(resume_data, variant))
        assert "—" not in html


def test_github_and_contact_render(resume_data: dict) -> None:
    html = render_module.render_html(render_module.build_context(resume_data, "founder_engineer"))
    assert "github.com/alex-rivera-example" in html
    assert "https://github.com" not in html
    assert "linkedin.com/in/test" in html


def test_banned_phrase_in_source_blocks_the_render(resume_data: dict, tmp_path: Path) -> None:
    """A completed-sale claim must stop the render, not reach a PDF."""
    resume_data["experience"][0]["bullets"][0]["text"] = "Sold the company in a 2026 exit."
    context = render_module.build_context(resume_data, "founder_engineer")
    hits = scan_banned_phrases(render_module.context_text(context))
    assert "sold" in hits
    assert "exit" in hits


def test_scan_is_clean_on_the_fixture(resume_data: dict) -> None:
    for variant in ("founder_engineer", "finance_ai"):
        context = render_module.build_context(resume_data, variant)
        assert scan_banned_phrases(render_module.context_text(context)) == []


def test_overlay_reorders_and_rewrites(resume_data: dict) -> None:
    overlay = {
        "summary": "Tailored summary.",
        "bullet_order": ["f755-1", "ws-1"],
        "bullet_rewrites": {"ws-1": "Founded an AI consulting company."},
        "dropped": [],
    }
    context = render_module.build_context(resume_data, "founder_engineer", overlay)
    assert context["summary"] == "Tailored summary."
    assert [block["company"] for block in context["experience"]] == [
        "Example Co",
        "Example Ventures LLC",
    ]
    ws_block = context["experience"][1]
    assert ws_block["bullets"][0]["text"] == "Founded an AI consulting company."
    assert len(ws_block["bullets"]) == 1


def test_overlay_does_not_mutate_the_source(resume_data: dict) -> None:
    original = resume_data["experience"][0]["bullets"][0]["text"]
    render_module.build_context(
        resume_data,
        "founder_engineer",
        {"bullet_order": ["ws-1"], "bullet_rewrites": {"ws-1": "Rewritten."}},
    )
    assert resume_data["experience"][0]["bullets"][0]["text"] == original


# --- validate_tailoring -----------------------------------------------------

KNOWN = {"ws-1", "ws-2", "f755-1", "f755-2"}


def test_validate_tailoring_accepts_a_well_formed_payload() -> None:
    payload = {
        "summary": "Finance major who ships AI.",
        "bullet_order": ["f755-1", "ws-1"],
        "bullet_rewrites": {"ws-1": "Founded an AI consulting company."},
        "dropped": ["ws-2"],
        "rationale": "Leads finance for a fintech posting.",
    }
    assert validate_tailoring(payload, KNOWN)["bullet_order"] == ["f755-1", "ws-1"]


def test_validate_tailoring_rejects_invented_bullet_id() -> None:
    payload = {"bullet_order": ["ws-1", "goldman-1"]}
    with pytest.raises(ResumeValidationError, match="goldman-1"):
        validate_tailoring(payload, KNOWN)


def test_validate_tailoring_rejects_invented_rewrite_target() -> None:
    payload = {"bullet_order": ["ws-1"], "bullet_rewrites": {"nope-1": "text"}}
    with pytest.raises(ResumeValidationError, match="nope-1"):
        validate_tailoring(payload, KNOWN)


def test_validate_tailoring_rejects_duplicate_ids() -> None:
    payload = {"bullet_order": ["ws-1", "ws-1"]}
    with pytest.raises(ResumeValidationError, match="repeats"):
        validate_tailoring(payload, KNOWN)


@pytest.mark.parametrize(
    "text",
    [
        "Sold the company to a strategic buyer.",
        "Acquired by a larger firm in 2026.",
        "Led the company through a successful exit.",
        "Worked as a CPA on tax filings.",
        "Founder-engineer — ships production systems.",
    ],
)
def test_validate_tailoring_rejects_banned_claims_in_summary(text: str) -> None:
    with pytest.raises(ResumeValidationError):
        validate_tailoring({"summary": text, "bullet_order": ["ws-1"]}, KNOWN)


def test_validate_tailoring_rejects_banned_claims_in_a_rewrite() -> None:
    payload = {"bullet_order": ["ws-1"], "bullet_rewrites": {"ws-1": "Sold the product to 12 firms."}}
    with pytest.raises(ResumeValidationError, match="sold"):
        validate_tailoring(payload, KNOWN)


def test_validate_tailoring_rejects_wrong_types() -> None:
    with pytest.raises(ResumeValidationError, match="bullet_order"):
        validate_tailoring({"bullet_order": "ws-1"}, KNOWN)
    with pytest.raises(ResumeValidationError, match="object"):
        validate_tailoring(["ws-1"], KNOWN)


# --- PDF --------------------------------------------------------------------


def _chromium_available() -> bool:
    if shutil.which("uv") is None:
        return False
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            browser.close()
    except Exception:
        return False
    return True


@pytest.mark.skipif(
    not _chromium_available(),
    reason="chromium not installed; run `uv run playwright install chromium`",
)
def test_render_variant_produces_a_one_page_pdf(resume_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    written = render_module.render_variant(
        "finance_ai", out_path=out, resume_path=resume_path
    )
    assert written == out
    raw = out.read_bytes()
    assert raw.startswith(b"%PDF")
    page_counts = [int(match) for match in re.findall(rb"/Count\s+(\d+)", raw)]
    assert page_counts and max(page_counts) == 1


@pytest.mark.skipif(
    not _chromium_available(),
    reason="chromium not installed; run `uv run playwright install chromium`",
)
def test_render_variant_fails_closed_on_a_banned_claim(
    resume_path: Path, tmp_path: Path
) -> None:
    resume_path.write_text(
        FIXTURE_YAML.replace(
            "Built and deployed a multi-tenant agent solo.",
            "Sold the company in a 2026 exit.",
        ),
        encoding="utf-8",
    )
    out = tmp_path / "blocked.pdf"
    with pytest.raises(ResumeValidationError, match="sold"):
        render_module.render_variant("founder_engineer", out_path=out, resume_path=resume_path)
    assert not out.exists()


def test_output_path_naming() -> None:
    assert render_module.output_path("finance_ai").name == "finance_ai.pdf"
    assert render_module.output_path("finance_ai", posting_id=42).name == "42-finance_ai.pdf"
