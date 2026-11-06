"""jobhunt.hardreq: positives and negatives per rule, biased toward not dropping."""

from __future__ import annotations

import pytest

from jobhunt import hardreq


def hit(description: str, *, min_years: int = 5, title: str = "Analyst", rules=None):
    return hardreq.hard_requirement(title, description, min_years=min_years, rules=rules)


def rule_of(description: str, **kw) -> str | None:
    found = hit(description, **kw)
    return found[0] if found else None


# --- jd -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Minimum qualifications: J.D. degree from an accredited law school.",
    "You are admitted to practice in at least one U.S. state. Bar admission is required.",
    "A law degree is required for this role.",
    "Requirements: Juris Doctor from an ABA-accredited school and an active bar membership.",
])
def test_jd_required_is_a_hit(text):
    assert rule_of(text) == "jd"


@pytest.mark.parametrize("text", [
    "A J.D. is preferred but not required.",
    "J.D. a plus.",
    "Nice to have: law degree.",
    "Bachelor's degree or J.D. required.",
    "Bar admission or equivalent legal experience.",
    "Please read the JD and apply.",  # "JD" as the job description
    "Preferred qualifications: J.D. degree. Responsibilities: draft memos.",
    "We work with a law degree holders network of advisors.",  # no requirement word
])
def test_jd_soft_or_alternative_is_not_a_hit(text):
    assert rule_of(text) != "jd"


# --- phd ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Required qualifications: Ph.D. in physics or a closely related field.",
    "You must have a PhD in machine learning.",
    "Minimum qualifications: a doctorate in economics.",
])
def test_phd_required_is_a_hit(text):
    assert rule_of(text) == "phd"


@pytest.mark.parametrize("text", [
    "PhD preferred.",
    "A PhD is a plus.",
    "PhD or equivalent experience required.",
    "Bachelor's, Master's or PhD in a quantitative field required.",
    "Nice to have: doctorate.",
    "Currently pursuing a PhD, required for this internship.",
    "Bonus points for a PhD. Requirements: Python.",
])
def test_phd_soft_or_alternative_is_not_a_hit(text):
    assert rule_of(text) != "phd"


# --- masters --------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Minimum requirements: Master's degree in statistics.",
    "A master's degree is required.",
    "MBA required.",
    "Required: master’s degree or higher in a quantitative field.",
])
def test_masters_required_is_a_hit(text):
    assert rule_of(text) == "masters"


@pytest.mark.parametrize("text", [
    "Master's degree preferred.",
    "Master's degree or equivalent experience required.",
    "Bachelor's degree required; Master's degree a plus.",
    "Bachelor's degree with 3 years, or a Master's degree, required.",
    "Master's in finance or related field is a bonus.",
    "You will be a scrum master and run the team. Must have a great attitude.",
    "Master data management experience is required.",
    "Nice to have: MBA.",
    "Requirements: Bachelor's or Master's degree in finance.",
])
def test_masters_soft_or_alternative_is_not_a_hit(text):
    assert rule_of(text) != "masters"


# --- license --------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Requirements: active CPA license required.",
    "You must be a Certified Public Accountant (CPA).",
    "Minimum qualifications: Series 7 and 63 required.",
    "CFA charterholder required.",
    "A registered nurse (RN) license is required.",
])
def test_license_required_is_a_hit(text):
    assert rule_of(text) == "license"


@pytest.mark.parametrize("text", [
    "CPA preferred.",
    "CPA or CFA a plus.",
    "CPA eligible candidates welcome. Requirements: accounting degree.",
    "Working toward your CPA or CFA is required.",
    "You will work with our CPA firm clients and must be organized.",
    "Series 7 and 63 licenses obtained within 90 days of hire, required.",
    "Nice to have: CFA charter.",
    "CPA or equivalent certification required.",
])
def test_license_soft_or_alternative_is_not_a_hit(text):
    assert rule_of(text) != "license"


# --- domain_years ---------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "What you will need: 3+ years of front-office experience at a large asset manager.",
    "Requirements: 2+ years of investment banking experience at a top-tier bank.",
    "You have 3+ years on the investment team at a buyout fund.",
    "Qualifications: 3 years of audit experience.",
])
def test_domain_years_is_a_hit(text):
    assert rule_of(text) == "domain_years"


@pytest.mark.parametrize("text", [
    "3+ years of experience in accounting, corporate finance, FP&A, investment banking, or private equity.",
    "Preferred: 3+ years of investment banking experience.",
    "1 year of audit experience required.",
    "3+ years of experience in corporate development or strategy consulting required.",
    "3+ years of investment banking or equivalent experience required.",
    "We have 30 years of audit experience as a firm.",
])
def test_domain_years_soft_or_alternative_is_not_a_hit(text):
    assert rule_of(text, rules={"domain_years"}) is None


# --- years ----------------------------------------------------------------------------------------


def test_years_at_or_above_the_floor_is_a_hit():
    found = hit("Requirements: 6+ years of experience in finance.", min_years=5)
    assert found is not None and found[0] == "years" and "6+ years" in found[1]
    assert rule_of("Qualifications: 5 years of relevant experience.", min_years=5) == "years"


def test_years_below_the_floor_or_from_the_config_is_not_a_hit():
    assert rule_of("Requirements: 4+ years of experience in finance.", min_years=5) is None
    assert rule_of("Requirements: 6+ years of experience in finance.", min_years=7) is None


@pytest.mark.parametrize("text", [
    "Requirements: 8+ years of experience, preferred.",
    "Nice to have: 7+ years of experience in fintech.",
    "8+ years of experience or equivalent practical experience required.",
    "Bonus: 10+ years of experience with Kubernetes.",
    "Preferred qualifications: 8+ years of experience leading teams. Responsibilities: build.",
    "Our founders have 30 years of combined experience in finance.",
    "We were founded 10 years ago and have experience in payments.",
    "The company has 20 years of experience helping clients.",  # implausible requirement
])
def test_years_soft_or_alternative_is_not_a_hit(text):
    assert rule_of(text, min_years=5) is None


# --- shape ----------------------------------------------------------------------------------------


def test_nothing_in_an_ordinary_posting_is_a_hit():
    text = ("About us: we build AI for finance teams. You will draft memos, model cash flows and "
            "work with engineers. Requirements: Bachelor's degree, 1-2 years of experience, Excel.")
    assert hit(text) is None
    assert hit("") is None
    assert hardreq.hard_requirement("t", None, min_years=5) is None  # type: ignore[arg-type]


def test_rules_limit_which_checks_run():
    text = "A J.D. degree is required. 8+ years of experience required."
    assert rule_of(text, rules={"years"}) == "years"
    assert rule_of(text, rules={"jd"}) == "jd"
    assert rule_of(text, rules=set()) is None


def test_matching_ignores_case_and_returns_an_evidence_snippet():
    rule, evidence = hit("MINIMUM QUALIFICATIONS: A MASTER'S DEGREE IS REQUIRED.")  # type: ignore[misc]
    assert rule == "masters"
    assert "master's degree" in evidence.lower()
