"""The standing answers fail closed: an unset value never produces an answer.

solve.py reads its owner-specific answers from the [standing_answers] table of the search
preferences (eligibility.StandingAnswers). Each test swaps in a StandingAnswers with some
values unset and checks the field is asked (handed off), exactly like an unknown value.
Pure: no browser, no database, no model.
"""

from __future__ import annotations

import pytest

from jobhunt import eligibility
from jobhunt.apply_assist import solve
from jobhunt.apply_assist.schema import FieldSpec

RESUME = "/tmp/resume.pdf"
YES_NO = ["Yes", "No"]
ANSWERS = {"first_name": "Sam", "last_name": "Rivera", "full_name": "Sam Rivera"}
ATTENDING = "Which university are you currently attending or did you last attend?"
GRADUATE_FROM = "If you have a college degree, what institution(s) did you graduate from?"


def _field(label, kind="text", options=None, required=True) -> FieldSpec:
    return FieldSpec(
        selector="#f", kind=kind, label=label, required=required, options=list(options or [])
    )


def _one(field, standing, monkeypatch, answers=None):
    monkeypatch.setattr(solve, "_standing", lambda: standing)
    (resolution,) = solve.resolve(
        [field], ANSWERS if answers is None else answers, RESUME, None, None, autoapply=True
    )
    return resolution


EMPTY = eligibility.StandingAnswers()
FULL = eligibility.user_preferences().standing_answers


def _asked(res):
    return res.action == "ask" and res.value is None


@pytest.mark.parametrize(
    "field",
    [
        _field("Expected Salary", "number"),
        _field("What are your salary expectations?"),
        _field("Desired bonus", "number"),
        _field("Pronouns", "select", ["She/Her", "He/Him", "They/Them"]),
        _field("What are your pronouns?"),
        _field("Have you ever served in the military?", "select", YES_NO),
        _field("Have you ever been employed by the US government?", "select", YES_NO),
        _field("Have you worked at a startup?", "radio", YES_NO),
        _field(ATTENDING, "select", ["Example State University", "Other (School Not Listed)"]),
        _field(GRADUATE_FROM, "text"),
        _field("Will you be using a camera for video interviews?", "select", YES_NO),
        _field("Are you able to start in March 2027?", "select", YES_NO),
        _field("Are you willing to travel up to 25% of the time?", "select", YES_NO),
        _field("Who is your current or most recent employer?*", "text"),
        _field("What is your current or more recent job title?*", "text"),
        _field("Did you play any sports in high school or college? Please describe.", "text"),
    ],
    ids=lambda f: f.label[:40],
)
def test_with_no_standing_answers_every_rule_asks(field, monkeypatch):
    res = _one(field, EMPTY, monkeypatch)
    assert res.action in ("ask", "skip"), res
    assert res.value in (None, ""), res


def test_the_same_fields_are_answered_when_the_standing_answers_are_set(monkeypatch):
    # Guards the test above: these fields are answered with the full template persona.
    for field in (
        _field("Expected Salary", "number"),
        _field("Have you worked at a startup?", "radio", YES_NO),
        _field("Who is your current or most recent employer?*", "text"),
    ):
        assert _one(field, FULL, monkeypatch).action == "fill", field.label


@pytest.mark.parametrize("founder", [None, False])
def test_the_startup_yes_needs_founder_experience_true(founder, monkeypatch):
    standing = eligibility.StandingAnswers(founder_experience=founder)
    res = _one(_field("Have you worked at a startup?", "radio", YES_NO), standing, monkeypatch)
    assert res.semantic_key != "startup_experience" and _asked(res), res


def test_an_unset_bonus_is_asked_while_a_set_salary_is_still_answered(monkeypatch):
    standing = eligibility.StandingAnswers(salary=120_000)
    res = _one(_field("What are your salary expectations?"), standing, monkeypatch)
    assert (res.action, res.value) == ("fill", "$120,000")  # no bonus is invented
    assert _asked(_one(_field("Desired bonus", "number"), standing, monkeypatch))


def test_an_unset_salary_leaves_a_set_bonus_alone(monkeypatch):
    standing = eligibility.StandingAnswers(bonus=5_000)
    assert _asked(_one(_field("What are your salary expectations?"), standing, monkeypatch))
    res = _one(_field("Desired bonus", "number"), standing, monkeypatch)
    assert (res.action, res.value) == ("fill", "5000")


def test_the_school_option_matches_the_school_and_its_aliases_only(monkeypatch):
    standing = eligibility.StandingAnswers(
        school="Northern Plains University", school_aliases=("NPU", "Plains U")
    )
    for option in ("Northern Plains University", "NPU", "Plains U (Main Campus)"):
        res = _one(
            _field(ATTENDING, "select", ["Emory University", option, "Other (School Not Listed)"]),
            standing,
            monkeypatch,
        )
        assert (res.action, res.value) == ("fill", option), option
    other = _one(
        _field(ATTENDING, "select", ["Example State University", "Other (School Not Listed)"]),
        standing,
        monkeypatch,
    )
    assert _asked(other)


def test_a_school_with_no_alias_still_matches_and_a_missing_education_text_is_asked(monkeypatch):
    standing = eligibility.StandingAnswers(school="Northern Plains University")
    res = _one(
        _field(ATTENDING, "select", ["Northern Plains University", "Other (School Not Listed)"]),
        standing,
        monkeypatch,
    )
    assert res.value == "Northern Plains University"
    assert _asked(_one(_field(GRADUATE_FROM, "text"), standing, monkeypatch))


def test_pronouns_are_matched_from_the_configured_value(monkeypatch):
    standing = eligibility.StandingAnswers(pronouns="She/Her/Hers")
    res = _one(
        _field("Pronouns", "select", ["He/Him/His", "She/Her/Hers", "They/Them/Theirs"]),
        standing,
        monkeypatch,
    )
    assert res.value == "She/Her/Hers"


def test_a_malformed_standing_table_is_unset():
    parsed = eligibility.load_standing_answers(
        {"salary": "85000", "earliest_start": "soon", "founder_experience": "yes",
         "school_aliases": "ESU", "travel_percent": True, "pronouns": 5}
    )  # fmt: skip
    assert parsed == EMPTY
    assert eligibility.load_standing_answers(None) == EMPTY
