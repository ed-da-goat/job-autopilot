"""The unattended lane's standing answers must still match the user's facts file.

The answers live in the [standing_answers] table of the active search preferences
(data/search-preferences.local.toml, else the template). data/facts.local.md (gitignored,
read here and never written) holds the user's standing application answers under
"## Standing application answers". When that file exists, these tests compare the active
config with it: if a bullet there changes and the config is left behind, the lane keeps
answering the old thing under the user's name, and this test fails first. The public
repo has no facts.local.md, so every test here skips with that reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jobhunt.apply_assist import solve
from jobhunt.eligibility import user_preferences

SA = user_preferences().standing_answers

FACTS = Path(__file__).resolve().parents[1] / "data" / "facts.local.md"
SECTION_HEADING = "## Standing application answers"
NO_FACTS = (
    "data/facts.local.md is not on this machine (the public repo has none): there is "
    "nothing to compare the active [standing_answers] config against"
)
UPDATE_HINT = (
    "update [standing_answers] in data/search-preferences.local.toml to match "
    "facts.local.md"
)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?P<label>[^:]+?)\s*:\s*(?P<value>.*?)\s*$")


def _standing_bullets() -> dict[str, str]:
    """{lowercased bullet label: value without its closing period} for the section,
    or pytest.skip when the file or the section is not there."""
    if not FACTS.exists():
        pytest.skip(NO_FACTS)
    lines = FACTS.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(SECTION_HEADING)]
    if not starts:
        pytest.skip(f"data/facts.local.md has no '{SECTION_HEADING}' section")
    bullets: dict[str, str] = {}
    for line in lines[starts[0] + 1 :]:
        if line.startswith("#"):
            break
        found = _BULLET_RE.match(line)
        if found:
            bullets[found.group("label").lower()] = found.group("value").rstrip(".").strip()
    return bullets


def _bullet(bullets: dict[str, str], *words: str) -> str:
    """The value of the one bullet whose label holds every word in `words`."""
    hits = [v for label, v in bullets.items() if all(w in label for w in words)]
    assert len(hits) == 1, (
        f"facts.local.md should have exactly one standing answer about {words}, "
        f"found {len(hits)}: {UPDATE_HINT}"
    )
    return hits[0]


def test_the_camera_on_rule_answer_matches_the_standing_answer():
    value = _bullet(_standing_bullets(), "camera")
    assert value.casefold() == "yes", f"camera on is now {value!r}: {UPDATE_HINT}"
    assert SA.camera_on.casefold() == "yes", UPDATE_HINT


def test_the_notice_period_rule_answer_matches_the_standing_answer():
    value = _bullet(_standing_bullets(), "notice")
    assert re.fullmatch(r"none(?: required)?", value.casefold()), (
        f"notice period is now {value!r}: {UPDATE_HINT}"
    )
    assert SA.notice_period.casefold() == "none", UPDATE_HINT


def test_the_m_and_a_rule_answer_matches_the_standing_answer():
    value = _bullet(_standing_bullets(), "m&a")
    assert SA.years_ma_experience == f"{value} years", (
        f"years of M&A experience is now {value!r}: {UPDATE_HINT}"
    )


def test_the_pronouns_rule_answer_matches_the_standing_answer():
    value = _bullet(_standing_bullets(), "pronouns")
    assert SA.pronouns == value, f"pronouns are now {value!r}: {UPDATE_HINT}"
    # The pick rule that reads a menu or a checkbox group must still find the answer.
    menu = ["Other/Others", value, "Someone/Else"]
    assert solve.pick_option("pronouns", [], menu) == value, UPDATE_HINT


def test_the_government_employment_rule_answer_matches_the_standing_answer():
    value = _bullet(_standing_bullets(), "government")
    assert SA.government_employment.casefold() == value.casefold(), (
        f"prior US government employment is now {value!r}: {UPDATE_HINT}"
    )


_PAY_RE = re.compile(r"^\$(?P<salary>\d[\d,]*) base plus \$(?P<bonus>\d[\d,]*) bonus$")


def test_the_salary_and_bonus_constants_match_the_standing_answer():
    value = _bullet(_standing_bullets(), "salary")
    found = _PAY_RE.match(value)
    assert found, f"salary expectation is now {value!r}, not '$N base plus $M bonus': {UPDATE_HINT}"
    salary = int(found.group("salary").replace(",", ""))
    bonus = int(found.group("bonus").replace(",", ""))
    assert SA.salary == salary, UPDATE_HINT
    assert SA.bonus == bonus, UPDATE_HINT
    assert SA.salary_text == value, UPDATE_HINT
    assert SA.bonus_text == f"${bonus:,}", UPDATE_HINT
    # The floor under a posting's stated pay (_salary_confirm_answer) is the same salary.
    from jobhunt.apply_assist import posting_answers

    assert posting_answers.default_salary() == salary, UPDATE_HINT


def test_the_salary_text_in_the_answers_file_matches_the_standing_answer():
    answers = FACTS.with_name("answers.local.md")
    if not answers.exists():
        pytest.skip("data/answers.local.md is not on this machine (the public repo has none)")
    lines = [
        line.partition(":")[2].strip()
        for line in answers.read_text(encoding="utf-8").splitlines()
        if line.startswith("salary_expectation:")
    ]
    assert lines == [SA.salary_text], (
        f"answers.local.md salary_expectation is {lines!r}: {UPDATE_HINT}"
    )


def test_the_office_city_order_matches_the_standing_answer():
    value = _bullet(_standing_bullets(), "city order")
    # The bullet opens with the order, then says how it is used.
    order = tuple(city.strip() for city in value.split(".")[0].split(","))
    assert order == solve.OFFICE_CITY_ORDER, (
        f"the office city order is now {order!r}: update OFFICE_CITY_ORDER and "
        "office_city_order in the search preferences to match facts.local.md"
    )


def test_the_four_named_consents_are_not_in_the_facts_file_yet():
    """The background check, interview recording, arbitration and privacy consents are
    answered Yes (solve._NAMED_CONSENT_RES) on the candidate's word of 2026-10-27, but
    facts.local.md has no bullet for them. When it gets one, check it here."""
    bullets = _standing_bullets()
    hits = [v for label, v in bullets.items() if "consent" in label or "background check" in label]
    if not hits:
        pytest.skip("facts.local.md has no consent bullet: the four named consents are unverified")
    assert all(re.search(r"\byes\b", v.casefold()) for v in hits), (
        f"a consent standing answer is now {hits!r}: update solve._NAMED_CONSENT_RES to match"
    )


def _facts_section(heading: str) -> str:
    """The text of the facts file's "## heading" section on one line, or skip."""
    if not FACTS.exists():
        pytest.skip(NO_FACTS)
    text = FACTS.read_text(encoding="utf-8")
    parts = text.split(f"## {heading}", 1)
    if len(parts) < 2:
        pytest.skip(f"data/facts.local.md has no '## {heading}' section")
    return " ".join(parts[1].split("\n## ", 1)[0].split())


def test_the_school_and_degree_text_match_the_education_section():
    """standing school / education_text answer "what institution did you graduate from" for
    a degree that is NOT finished: the text must keep the facts file's school and say
    "expected", and every school alias must be a spelling of the same school."""
    education = _facts_section("Education")
    assert SA.school, "the facts file has an Education section but [standing_answers] has no school"
    assert education.startswith(SA.school), UPDATE_HINT
    assert SA.education_text.startswith(f"{SA.school} ("), UPDATE_HINT
    assert "expected" in SA.education_text.casefold(), UPDATE_HINT
    assert "graduated" not in SA.education_text.casefold()
    degree = SA.education_text[len(SA.school) + 2 :].split(", expected", 1)[0]
    assert degree and degree in education, (
        f"the Education section no longer holds the degree {degree!r}: {UPDATE_HINT}"
    )


def test_the_startup_yes_rests_on_the_founder_facts():
    """"Have you worked at a startup?" is answered Yes (solve._startup_answer) only when
    founder_experience is set, and the facts file's work history must show a founder role."""
    if SA.founder_experience is not True:
        pytest.skip("founder_experience is not true in [standing_answers]: the startup rule is off")
    work = _facts_section("Work history")
    assert re.search(r"\bfounder\b", work, re.IGNORECASE), (
        "no founder role in the work history: set founder_experience = false"
    )


def test_the_relocation_tick_rests_on_the_open_to_relocation_fact():
    """The Method location group's relocation box (solve._relocation_picks) is ticked because
    he is open to relocation, including NYC."""
    looking = _facts_section("What Alex is looking for (motivation grounding)")
    assert re.search(r"open to relocation", looking, re.IGNORECASE), (
        "the open-to-relocation fact changed: retire solve._relocation_picks"
    )
    if SA.working_location:
        assert SA.working_location in _standing_bullets().values(), UPDATE_HINT


def test_the_new_york_onsite_yes_rests_on_the_standing_answers():
    bullets = _standing_bullets()
    assert _bullet(bullets, "fully on-site").casefold() == "yes", UPDATE_HINT
    assert "any schedule is fine" in _bullet(bullets, "hybrid"), UPDATE_HINT
