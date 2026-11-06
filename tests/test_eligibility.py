from __future__ import annotations

import json

from jobhunt.eligibility import TEMPLATE_PREFERENCES_PATH, assess_posting, load_preferences

# Explicit template path: module-level load runs before the conftest isolation
# fixture, and the suite must never depend on the operator's local toml.
PREFERENCES = load_preferences(TEMPLATE_PREFERENCES_PATH)


def posting(title: str, location: str, description: str = "") -> dict:
    return {
        "source": "greenhouse",
        "title": title,
        "location": location,
        "raw_json": json.dumps({"content": description}),
    }


def test_scale_director_london_is_blocked():
    result = assess_posting(
        posting("Director, Forward Deployed Engineering", "London, UK"), PREFERENCES
    )
    assert result.status == "blocked"
    assert "international_only" in result.blockers
    assert "seniority_title" in result.blockers


def test_us_new_grad_is_eligible():
    result = assess_posting(
        posting(
            "Software Engineer, New Grad",
            "New York, NY",
            "What you’ll bring: 0–2 years of experience",
        ),
        PREFERENCES,
    )
    assert result.status == "eligible"
    assert any("0–2 years" in item for item in result.evidence)


def test_international_role_is_blocked():
    result = assess_posting(posting("Backend Engineer", "Bangalore, India"), PREFERENCES)
    assert result.status == "blocked"
    assert result.blockers == ["international_only"]


def test_mts_role_requires_review():
    result = assess_posting(
        posting("Member of Technical Staff, Forward Deployed", "San Francisco"), PREFERENCES
    )
    assert result.status == "review"
    assert "mts_seniority_ambiguous" in result.warnings


def test_six_year_requirement_is_blocked():
    result = assess_posting(
        posting(
            "Backend Engineer",
            "Remote (US)",
            "You have at least 6+ years of professional software engineering experience.",
        ),
        PREFERENCES,
    )
    assert result.status == "blocked"
    assert "experience_6_plus" in result.blockers


def test_four_year_requirement_requires_review():
    result = assess_posting(
        posting("Backend Engineer", "Atlanta, GA", "4+ years of experience required."),
        PREFERENCES,
    )
    assert result.status == "review"
    assert "experience_4_to_5" in result.warnings


def test_federal_role_requires_review():
    result = assess_posting(
        posting("Forward Deployed Engineer, Federal", "Washington DC"), PREFERENCES
    )
    assert result.status == "review"
    assert "government_eligibility_needs_confirmation" in result.warnings


def test_people_manager_requirement_is_blocked():
    result = assess_posting(
        posting(
            "Software Engineer",
            "New York, NY",
            "You will manage a team of six engineers and own hiring decisions.",
        ),
        PREFERENCES,
    )
    assert result.status == "blocked"
    assert "people_management" in result.blockers
