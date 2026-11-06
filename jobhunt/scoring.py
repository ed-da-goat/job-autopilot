"""Rubric-driven posting scorer. Deterministic, DB-read-only — no network
calls. Orchestrates text_extract + scoring_components into one score dict
per posting.
"""

from __future__ import annotations

import sqlite3

from jobhunt.scoring_components import (
    any_keyword_match,
    score_company_signal,
    score_degree_fit,
    score_degree_language_bonus,
    score_early_grad_bonus,
    score_finance_hybrid_bonus,
    score_founder_leverage,
    score_seniority_fit,
    score_skill_match,
)
from jobhunt.text_extract import extract_text


def _check_intern_or_apprentice(title: str, kw: dict) -> str | None:
    if any_keyword_match(title, kw["hard_filter_intern"]):
        return "title_intern"
    if any_keyword_match(title, kw["hard_filter_apprentice"]):
        return "title_apprentice"
    return None


def check_hard_filter(title: str, status: str | None, rubric: dict) -> tuple[bool, str | None]:
    """title must already be lowercased (as returned by extract_text)."""
    if status != "active":
        return True, f"status_not_active:{status}"

    reason = _check_intern_or_apprentice(title, rubric["keywords"])
    if reason:
        return True, reason
    return False, None


def _check_not_target_role(title: str, skill_score: int, rubric: dict) -> str | None:
    """Second-stage hard filter, applied only once a posting has cleared the
    intern/apprentice/status checks. Excludes postings whose title carries no
    target-role signal AND whose computed skill_match is weak — title-only
    matches (e.g. "Solutions Architect") and skill-only matches (e.g. a
    generically-titled role with a strong Python/Postgres description) both
    survive; only the intersection of "no title signal" and "no skill signal"
    is excluded."""
    if any_keyword_match(title, rubric["keywords"]["target_role_titles"]):
        return None
    if skill_score >= 6:
        return None
    return "not_target_role"


def load_companies_by_name(conn: sqlite3.Connection) -> dict[str, dict]:
    """Batch-load companies keyed by name for company_signal lookups —
    call once per sweep, not once per posting."""
    rows = conn.execute(
        "SELECT name, category, platform, slug, verified, watchlist, why_fit FROM companies"
    ).fetchall()
    return {r["name"]: dict(r) for r in rows}


def score_posting(posting_row: sqlite3.Row | dict, rubric: dict, company_row: dict | None) -> dict:
    """Score one posting. Pure given its inputs — company_row must already
    be looked up by the caller (see load_companies_by_name)."""
    title, description, has_description = extract_text(posting_row)
    status = posting_row["status"]

    excluded, exclude_reason = check_hard_filter(title, status, rubric)
    if excluded:
        return {
            "total": None,
            "components": {},
            "score_evidence": {},
            "missing_evidence": [],
            "excluded": True,
            "exclude_reason": exclude_reason,
        }

    skill_score, skill_note = score_skill_match(title, description, has_description, rubric)

    not_target_reason = _check_not_target_role(title, skill_score, rubric)
    if not_target_reason:
        return {
            "total": None,
            "components": {},
            "score_evidence": {},
            "missing_evidence": [],
            "excluded": True,
            "exclude_reason": not_target_reason,
        }

    degree_score, degree_note = score_degree_fit(title, description, has_description, rubric)
    seniority_score, seniority_evidence = score_seniority_fit(title, description, rubric)
    founder_score = score_founder_leverage(title, description, rubric)
    company_score, company_note = score_company_signal(company_row, title, description, rubric)
    degree_language_score = score_degree_language_bonus(title, description, rubric)
    early_grad_score = score_early_grad_bonus(title, description, rubric)
    finance_hybrid_score = score_finance_hybrid_bonus(title, description, rubric)

    # dict.fromkeys dedupes while preserving first-seen order (degree_fit and
    # skill_match both flag "no_description" when a posting has no body text)
    missing_evidence = list(
        dict.fromkeys(note for note in (degree_note, skill_note, company_note) if note)
    )

    components = {
        "degree_fit": degree_score,
        "skill_match": skill_score,
        "seniority_fit": seniority_score,
        "founder_leverage": founder_score,
        "company_signal": company_score,
        "degree_language_bonus": degree_language_score,
        "early_grad_bonus": early_grad_score,
        "finance_hybrid_bonus": finance_hybrid_score,
    }
    total = max(0, min(100, sum(components.values())))

    return {
        "total": total,
        "components": components,
        "score_evidence": {"seniority_fit": seniority_evidence},
        "missing_evidence": missing_evidence,
        "excluded": False,
        "exclude_reason": None,
    }
