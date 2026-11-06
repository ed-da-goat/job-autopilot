from __future__ import annotations

import json

import pytest
from jobhunt import db as db_module
from jobhunt import scoring as scoring_module
from jobhunt.rubric import load_rubric

RUBRIC = load_rubric()


def make_posting(
    *,
    source: str,
    raw: dict,
    title: str = "Software Engineer",
    status: str = "active",
    company: str | None = "Acme",
) -> dict:
    return {
        "id": 1,
        "source": source,
        "title": title,
        "status": status,
        "company": company,
        "raw_json": json.dumps(raw),
    }


# ------------------------------------------------------- hard filters ------


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineering Intern",
        "Summer Internship - Backend",
        "Engineering Co-op",
        "Data Science Apprenticeship",
    ],
)
def test_hard_filter_excludes_intern_and_apprentice_titles(title):
    excluded, reason = scoring_module.check_hard_filter(title.lower(), "active", RUBRIC)
    assert excluded is True
    assert reason is not None


def test_hard_filter_does_not_exclude_international_markets_engineer():
    excluded, reason = scoring_module.check_hard_filter("international markets engineer", "active", RUBRIC)
    assert excluded is False
    assert reason is None


def test_hard_filter_excludes_non_active_status():
    excluded, reason = scoring_module.check_hard_filter("backend engineer", "closed", RUBRIC)
    assert excluded is True
    assert reason == "status_not_active:closed"


# ----------------------------------------------------- not_target_role -----


def test_not_target_role_excludes_no_title_no_skill_signal():
    posting = make_posting(source="greenhouse", raw={"content": ""}, title="Mobility Tax Analyst")
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["excluded"] is True
    assert result["exclude_reason"] == "not_target_role"
    assert result["total"] is None
    assert result["components"] == {}


def test_not_target_role_excludes_field_marketing_title():
    posting = make_posting(
        source="greenhouse", raw={"content": ""}, title="Field Marketing Specialist, East"
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["excluded"] is True
    assert result["exclude_reason"] == "not_target_role"


def test_target_role_title_forward_deployed_engineer_not_excluded():
    posting = make_posting(source="greenhouse", raw={"content": ""}, title="Forward Deployed Engineer")
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["excluded"] is False


def test_target_role_title_solutions_architect_not_excluded():
    posting = make_posting(source="greenhouse", raw={"content": ""}, title="Solutions Architect")
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["excluded"] is False


def test_no_title_signal_but_strong_skill_match_not_excluded():
    posting = make_posting(
        source="greenhouse",
        raw={"content": "Work with Python and Postgres daily."},
        title="Mobility Tax Analyst",
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["excluded"] is False
    assert result["components"]["skill_match"] >= 6


def test_email_marketing_manager_excluded_ai_does_not_match_inside_email():
    posting = make_posting(source="greenhouse", raw={"content": ""}, title="Email Marketing Manager")
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["excluded"] is True
    assert result["exclude_reason"] == "not_target_role"


def test_score_posting_returns_excluded_dict_for_intern_title():
    posting = make_posting(source="greenhouse", raw={"content": ""}, title="Software Engineering Intern")
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["excluded"] is True
    assert result["exclude_reason"] == "title_intern"
    assert result["total"] is None
    assert result["components"] == {}


# --------------------------------------------------------- degree_fit ------


def test_degree_fit_hard_requirement_without_softener_scores_low():
    posting = make_posting(source="greenhouse", raw={"content": "Requires a BS in Computer Science."})
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["degree_fit"] == 2


def test_degree_fit_with_equivalence_softener_scores_mid():
    posting = make_posting(
        source="greenhouse",
        raw={"content": "Bachelor's degree in Computer Science or equivalent experience required."},
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["degree_fit"] == 12


def test_degree_fit_no_degree_required_scores_max():
    posting = make_posting(
        source="greenhouse",
        raw={"content": "No degree required — we care about what you can build."},
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["degree_fit"] == 15


def test_degree_fit_no_mention_at_all_scores_max():
    posting = make_posting(
        source="greenhouse",
        raw={"content": "We are looking for a strong backend engineer to join our team."},
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["degree_fit"] == 15


# --------------------------------------------------------- seniority -------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("New Grad Software Engineer", 20),
        ("Software Engineer I", 20),
        ("Junior Backend Engineer", 20),
        ("Software Engineer II", 14),
        ("Mid-Level Backend Engineer", 14),
        ("Senior Software Engineer", 4),
        ("Staff Software Engineer", 0),
        ("Engineering Manager", 0),
        ("Director of Engineering", 0),
        ("Backend Engineer", 12),  # no signal -> default
        ("Member of Technical Staff", 12),  # rank-neutral MTS title -> default, not staff tier
        ("Senior Member of Technical Staff", 4),  # "senior" signal still applies
    ],
)
def test_seniority_fit_tiers(title, expected):
    posting = make_posting(source="greenhouse", raw={"content": ""}, title=title)
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["seniority_fit"] == expected


def test_seniority_fit_member_technical_staff_comma_variant():
    posting = make_posting(
        source="greenhouse", raw={"content": ""}, title="Member, Technical Staff"
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["seniority_fit"] == 12


def test_seniority_fit_prefers_senior_signal_over_new_grad_when_both_present():
    posting = make_posting(
        source="greenhouse",
        raw={"content": "Part of our staff engineering track, formerly a new grad rotational program."},
        title="Staff Software Engineer",
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["seniority_fit"] == 0


def test_seniority_fit_ignores_incidental_senior_language_for_new_grad_role():
    posting = make_posting(
        source="greenhouse",
        raw={"content": "What you’ll bring: 0–2 years of experience. Work closely with senior leaders and staff across the company."},
        title="Software Engineer (New Grad)",
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["seniority_fit"] == 20
    assert "0–2 years" in result["score_evidence"]["seniority_fit"]


def test_seniority_fit_explicit_requirement_overrides_early_career_marketing_copy():
    posting = make_posting(
        source="greenhouse",
        raw={"content": "Early career opportunity. What you’ll bring: 3+ years in a relevant technical role."},
        title="Deployed Engineer (Early Career)",
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["seniority_fit"] == 8
    assert "3+ years" in result["score_evidence"]["seniority_fit"]


def test_seniority_fit_six_plus_year_requirement_scores_zero():
    posting = make_posting(
        source="greenhouse",
        raw={"content": "Minimum 10+ years of relevant experience required."},
        title="Software Engineer",
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["seniority_fit"] == 0
    assert "10+ years" in result["score_evidence"]["seniority_fit"]


# ---------------------------------------------------- founder_leverage -----


def test_founder_leverage_title_hit_counts_double():
    posting = make_posting(source="greenhouse", raw={"content": "Come build with us."}, title="Founding Engineer")
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["founder_leverage"] == 6


def test_founder_leverage_description_only_hit_counts_single():
    posting = make_posting(
        source="greenhouse",
        raw={"content": "You'll join as a founding engineer on a small team."},
        title="Software Engineer",
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["founder_leverage"] == 3


def test_founder_leverage_caps_at_25():
    raw = {
        "content": (
            "forward deployed solutions engineer sales engineer founding engineer founding team "
            "early stage zero to one customer-facing wear many hats implementation engineer gtm "
            "customer success startup experience ambiguity full ownership"
        )
    }
    posting = make_posting(source="greenhouse", raw=raw, title="Founding Engineer")
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["founder_leverage"] == 25


# ------------------------------------------------------- unknown midpoints -


def test_no_description_hits_all_the_unknown_midpoints():
    posting = make_posting(source="simplify", raw={}, title="Software Engineer")
    result = scoring_module.score_posting(posting, RUBRIC, None)

    assert result["components"]["degree_fit"] == RUBRIC["unknown_midpoints"]["degree_fit"]
    assert result["components"]["company_signal"] == RUBRIC["unknown_midpoints"]["company_signal"]
    assert "no_description" in result["missing_evidence"]
    assert "company_not_in_seed_list" in result["missing_evidence"]


def test_missing_evidence_has_no_duplicate_notes():
    posting = make_posting(source="simplify", raw={}, title="Software Engineer")
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert len(result["missing_evidence"]) == len(set(result["missing_evidence"]))


# ------------------------------------------------------- company_signal ----


def test_company_signal_uses_category_bonus_and_verified_bump():
    company_row = {"name": "Anthropic", "category": "ai-infra", "verified": "yes"}
    posting = make_posting(source="greenhouse", raw={"content": "We build AI."}, company="Anthropic")
    result = scoring_module.score_posting(posting, RUBRIC, company_row)
    assert result["components"]["company_signal"] == 10  # 8 + 2, cap 10


def test_company_signal_no_match_uses_unknown_midpoint():
    posting = make_posting(source="hn", raw={"comment_text": "Acme | SWE"}, company="Not In Seed List")
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["company_signal"] == RUBRIC["unknown_midpoints"]["company_signal"]
    assert "company_not_in_seed_list" in result["missing_evidence"]


def test_load_companies_by_name(tmp_path):
    conn = db_module.connect(tmp_path / "companies.db")
    try:
        conn.execute(
            "INSERT INTO companies (name, category, verified, why_fit, added_at) "
            "VALUES ('Acme', 'fintech', 'yes', 'strong python shop', '')"
        )
        conn.commit()
        companies = scoring_module.load_companies_by_name(conn)
        assert companies["Acme"]["category"] == "fintech"
        assert companies["Acme"]["verified"] == "yes"
        assert companies["Acme"]["why_fit"] == "strong python shop"
    finally:
        conn.close()


# --------------------------------------- v5: neutral prior for auto rows ---


def test_company_signal_auto_discovered_no_why_fit_scores_neutral():
    company_row = {"name": "Nebula Corp", "category": "auto-discovered", "verified": "auto-yes", "why_fit": None}
    posting = make_posting(
        source="greenhouse", raw={"content": "We build internal tools."}, company="Nebula Corp"
    )
    result = scoring_module.score_posting(posting, RUBRIC, company_row)
    assert result["components"]["company_signal"] == RUBRIC["unknown_midpoints"]["company_signal"]
    assert "company_auto_discovered_no_why_fit" in result["missing_evidence"]


def test_company_signal_auto_promoted_no_why_fit_scores_neutral():
    company_row = {"name": "Orbit Labs", "category": "auto-promoted", "verified": None, "why_fit": None}
    posting = make_posting(source="greenhouse", raw={"content": "Backend role."}, company="Orbit Labs")
    result = scoring_module.score_posting(posting, RUBRIC, company_row)
    assert result["components"]["company_signal"] == RUBRIC["unknown_midpoints"]["company_signal"]


def test_company_signal_auto_discovered_with_why_fit_keeps_full_discrimination():
    """Curated why_fit lifts a row out of the neutral-prior path even if the
    category string is still auto-discovered/auto-promoted; it then scores
    via category_bonus like any other row (0, since neither auto-* string is
    a bonus category — a residual gap, not what this ticket fixes)."""
    company_row = {
        "name": "Verified Auto Co",
        "category": "auto-discovered",
        "verified": "auto-yes",
        "why_fit": "manually reviewed — strong fit",
    }
    posting = make_posting(source="greenhouse", raw={"content": "Backend role."}, company="Verified Auto Co")
    result = scoring_module.score_posting(posting, RUBRIC, company_row)
    assert result["components"]["company_signal"] == 0
    assert "company_auto_discovered_no_why_fit" not in result["missing_evidence"]


def test_company_signal_auto_discovered_still_capped_at_ten_with_verified_bump():
    """auto_categories neutral-prior path returns exactly the neutral
    midpoint — the 'auto-yes' verified value must not sneak in a +2 bump."""
    company_row = {"name": "Nebula Corp", "category": "auto-discovered", "verified": "auto-yes", "why_fit": None}
    posting = make_posting(source="greenhouse", raw={"content": "Backend role."}, company="Nebula Corp")
    result = scoring_module.score_posting(posting, RUBRIC, company_row)
    assert result["components"]["company_signal"] == 5


# ------------------------------------------ v5: missing posted_at neutral --


def test_missing_posted_at_does_not_affect_score():
    """workday/builtin structurally never have posted_at. Nothing in scoring
    reads it, so its presence or absence must be a no-op on the total."""
    posting_without = make_posting(source="workday", raw={"content": "Backend engineer role."})
    posting_without.pop("posted_at", None)
    posting_with = dict(posting_without, posted_at=None)
    result_without = scoring_module.score_posting(posting_without, RUBRIC, None)
    result_with = scoring_module.score_posting(posting_with, RUBRIC, None)
    assert result_without["total"] == result_with["total"]

    posting_dated = dict(posting_without, posted_at="2020-01-01T00:00:00Z")
    result_dated = scoring_module.score_posting(posting_dated, RUBRIC, None)
    assert result_dated["total"] == result_without["total"]


# --------------------------------------- v5: inferred vertical, max-not-add-


def test_inferred_vertical_lifts_unknown_company_out_of_neutral():
    posting = make_posting(
        source="greenhouse",
        raw={"content": "Join our payments infrastructure team building capital markets tooling."},
        title="Backend Engineer",
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["company_signal"] == RUBRIC["category_bonus"]["fintech"]


def test_inferred_vertical_ai_infra_from_title_and_description():
    posting = make_posting(
        source="workday",
        raw={"content": "You'll work as a forward deployed engineer on our applied AI team."},
        title="Forward Deployed Engineer",
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["company_signal"] == RUBRIC["category_bonus"]["ai-infra"]


def test_inferred_vertical_is_max_not_additive_with_curated_category():
    """Curated fintech company row plus fintech-flavored description text
    must not stack — max(10, 10) == 10, not 20."""
    company_row = {"name": "Ramp", "category": "fintech", "verified": "no"}
    posting = make_posting(
        source="greenhouse",
        raw={"content": "Payments and banking infrastructure for capital markets."},
        title="Backend Engineer",
        company="Ramp",
    )
    result = scoring_module.score_posting(posting, RUBRIC, company_row)
    assert result["components"]["company_signal"] == RUBRIC["category_bonus"]["fintech"]


def test_inferred_vertical_is_max_not_additive_across_verticals():
    """A posting hitting both fintech and ai-infra keywords takes the higher
    single vertical's points, not the sum."""
    posting = make_posting(
        source="greenhouse",
        raw={"content": "Forward deployed engineer building payments and trading infrastructure with LLM agents."},
        title="Forward Deployed Engineer",
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["company_signal"] == max(
        RUBRIC["category_bonus"]["fintech"], RUBRIC["category_bonus"]["ai-infra"]
    )


# ------------------------------------------ degree_language / early_grad ---


def test_early_grad_bonus_new_grad_scores_exactly_six_more():
    baseline = make_posting(
        source="greenhouse",
        raw={"content": "Join our backend engineering team."},
        title="Backend Engineer",
    )
    with_bonus = make_posting(
        source="greenhouse",
        raw={"content": "Join our backend engineering team. New grad program available."},
        title="Backend Engineer",
    )
    base_result = scoring_module.score_posting(baseline, RUBRIC, None)
    bonus_result = scoring_module.score_posting(with_bonus, RUBRIC, None)

    assert base_result["components"]["early_grad_bonus"] == 0
    assert bonus_result["components"]["early_grad_bonus"] == 6
    assert bonus_result["total"] - base_result["total"] == 6


def test_degree_language_bonus_business_administration_scores_exactly_six_more():
    baseline = make_posting(
        source="greenhouse",
        raw={"content": "We are looking for a strong backend engineer to join our team."},
    )
    with_bonus = make_posting(
        source="greenhouse",
        raw={
            "content": (
                "We are looking for a strong backend engineer to join our team. "
                "We welcome candidates with a business administration background."
            )
        },
    )
    base_result = scoring_module.score_posting(baseline, RUBRIC, None)
    bonus_result = scoring_module.score_posting(with_bonus, RUBRIC, None)

    assert base_result["components"]["degree_language_bonus"] == 0
    assert bonus_result["components"]["degree_language_bonus"] == 6
    assert bonus_result["total"] - base_result["total"] == 6


def test_early_grad_bonus_applies_once_not_per_keyword():
    posting = make_posting(
        source="greenhouse",
        raw={
            "content": (
                "New grad friendly. Also open to recent graduate and entry level "
                "candidates from the class of 2026."
            )
        },
        title="Software Engineer",
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["early_grad_bonus"] == 6


def test_finance_hybrid_bonus_tax_software_scores_exactly_ten_more():
    # "tax software" is deliberately not in vertical_keywords.fintech, so this
    # isolates finance_hybrid_bonus from the pre-existing inferred_vertical /
    # company_signal path (which "payments"/"banking"/etc. would also move).
    baseline = make_posting(
        source="greenhouse",
        raw={"content": "Join our backend engineering team building internal tools."},
    )
    with_bonus = make_posting(
        source="greenhouse",
        raw={
            "content": (
                "Join our backend engineering team building tax software for accountants."
            )
        },
    )
    base_result = scoring_module.score_posting(baseline, RUBRIC, None)
    bonus_result = scoring_module.score_posting(with_bonus, RUBRIC, None)

    assert base_result["components"]["finance_hybrid_bonus"] == 0
    assert bonus_result["components"]["finance_hybrid_bonus"] == 10
    assert bonus_result["total"] - base_result["total"] == 10


def test_finance_hybrid_bonus_applies_once_not_per_keyword():
    posting = make_posting(
        source="greenhouse",
        raw={
            "content": (
                "Forward deployed engineer for fintech. We build treasury and "
                "underwriting tools with a finance degree welcome."
            )
        },
        title="Forward Deployed Engineer",
    )
    result = scoring_module.score_posting(posting, RUBRIC, None)
    assert result["components"]["finance_hybrid_bonus"] == 10


def test_finance_hybrid_bonus_does_not_degrade_pure_swe_ordering():
    """A pure-SWE posting with no finance-domain language must not score
    higher than an otherwise-identical posting that does carry finance
    signal — v8 only adds credit, it never subtracts from stack-match
    postings."""
    finance_posting = make_posting(
        source="lever",
        raw={
            "descriptionPlain": (
                "Forward deployed engineer building tax software with "
                "Python, FastAPI, and Postgres. No degree required."
            ),
            "lists": [],
        },
        title="Forward Deployed Engineer",
    )
    pure_swe_posting = make_posting(
        source="lever",
        raw={
            "descriptionPlain": (
                "Forward deployed engineer building internal tooling with Python, "
                "FastAPI, and Postgres. No degree required."
            ),
            "lists": [],
        },
        title="Forward Deployed Engineer",
    )
    finance_result = scoring_module.score_posting(finance_posting, RUBRIC, None)
    swe_result = scoring_module.score_posting(pure_swe_posting, RUBRIC, None)

    # every non-bonus component is identical (same stack, same title, same
    # degree language) — v8 must not have touched them
    for key in ("degree_fit", "skill_match", "seniority_fit", "founder_leverage", "company_signal"):
        assert finance_result["components"][key] == swe_result["components"][key]
    assert finance_result["total"] > swe_result["total"]


def test_fintech_category_bonus_is_now_ten():
    company_row = {"name": "Ramp", "category": "fintech", "verified": "no"}
    posting = make_posting(source="greenhouse", raw={"content": "We build fintech tools."}, company="Ramp")
    result = scoring_module.score_posting(posting, RUBRIC, company_row)
    assert result["components"]["company_signal"] == 10


def test_rubric_loads_with_version_eight():
    assert RUBRIC["version"] == 8


# ------------------------------------------------------------ determinism -


def test_score_posting_is_deterministic():
    posting = make_posting(
        source="lever",
        raw={
            "descriptionPlain": "Forward deployed engineer building with Python, FastAPI, and Postgres.",
            "lists": [],
        },
        title="Forward Deployed Engineer",
        company="Palantir",
    )
    company_row = {"name": "Palantir", "category": "fde-solutions", "verified": "yes"}
    first = scoring_module.score_posting(posting, RUBRIC, company_row)
    second = scoring_module.score_posting(posting, RUBRIC, company_row)
    assert first == second


# ---------------------------------------------------------------- totals ---


def test_rubric_v8_weights_sum_to_100():
    assert RUBRIC["version"] == 8
    assert sum(RUBRIC["components"].values()) == 100
    assert "start_fit" not in RUBRIC["components"]


def test_score_posting_total_never_exceeds_100():
    posting = make_posting(
        source="lever",
        raw={
            "descriptionPlain": (
                "Forward deployed founding engineer. No degree required. Python, FastAPI, "
                "Postgres, LLM, agentic, SQLAlchemy, Docker, AWS, RAG, pytest. Junior engineer I. "
                "Zero to one, wear many hats, full ownership, startup experience."
            ),
            "lists": [],
        },
        title="Founding Engineer I",
        company="Anthropic",
    )
    company_row = {"name": "Anthropic", "category": "ai-infra", "verified": "yes"}
    result = scoring_module.score_posting(posting, RUBRIC, company_row)
    assert result["total"] <= 100
