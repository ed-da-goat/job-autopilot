"""solve.resolve() factual answers for the unattended lane (2026-10-27).

A form question whose answer is a fact the candidate gave (data/facts.local.md), a line of the
master resume or a row of the tracker is answered; a fact nobody has stated stays a
question. Every rule below has a positive test and, where the rule could be mistaken
for something else, a negative one: an unknown skill, conflicting dates, a consent
checkbox and an office list with no ranked city stay asked.

The labels are the shapes real forms use (the sweep scans); the option lists and the
stored values are made up. Everything is pure: no browser, no model, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jobhunt.apply_assist import solve
from jobhunt.apply_assist.keys import CHECKBOX_TITLE_SEP
from jobhunt.apply_assist.schema import FieldSpec
from jobhunt.eligibility import user_preferences
from tests.conftest import operator_file

RESUME = "/tmp/resume.pdf"

# The active standing answers (the template's example persona when no local file exists).
SA = user_preferences().standing_answers

# Made-up values in the shapes of the stored profile (a degree line ends "Mon YYYY").
ANSWERS = {
    "first_name": "Sam",
    "last_name": "Rivera",
    "full_name": "Sam Rivera",
    "degree": "B.S. Business Administration, Finance, Example Tech, Dec 2026, GPA 3.17",
    "school": "Example Institute of Technology",
    "worked_here": "No",
    "applied_before": "No",
    "resume_skills": "bloomberg,excel,postgres,python",
}


def _field(label, kind="text", options=None, required=True, **kw) -> FieldSpec:
    return FieldSpec(
        selector="#f", kind=kind, label=label, required=required,
        options=list(options or []), **kw,
    )  # fmt: skip


def _resolve(fields, answers=None, *, company=None, autoapply=True):
    return solve.resolve(
        fields, ANSWERS if answers is None else answers, RESUME, None, company, autoapply=autoapply
    )


def _one(field, answers=None, *, company=None, autoapply=True):
    (resolution,) = _resolve([field], answers, company=company, autoapply=autoapply)
    return resolution


def _filled(res, key, value, rule=""):
    assert (res.semantic_key, res.action, res.value, res.option_rule) == (
        key, "fill", value, rule,
    ), res  # fmt: skip


def _asked(res):
    assert res.action == "ask", res


YES_NO = ["Yes", "No"]


# --- Education: the end date of the degree --------------------------------------------


def _education_block(*date_labels, kinds=None):
    fields = [_field("School*", "text"), _field("Degree*", "text"), _field("Discipline*", "text")]
    for index, label in enumerate(date_labels):
        kind = (kinds or {}).get(label, "text")
        fields.append(_field(label, kind, options=[], required=True))
    return fields


def test_education_date_fields_are_those_after_a_school_block():
    fields = _education_block("Start date month", "Start date year", "End date month", "End date year")
    assert solve.education_date_fields(fields) == {
        3: ("start", "month"),
        4: ("start", "year"),
        5: ("end", "month"),
        6: ("end", "year"),
    }


def test_a_jobs_dates_are_not_education_dates():
    fields = [
        _field("School*"), _field("Degree*"),
        _field("Company*"), _field("Job title*"),
        _field("Start date month"), _field("End date year"),
    ]  # fmt: skip
    assert solve.education_date_fields(fields) == {}


def test_a_lone_date_box_with_no_school_before_it_is_not_education():
    assert solve.education_date_fields([_field("Start date month"), _field("End date year")]) == {}


def test_the_end_year_of_the_degree_is_the_year_of_the_stored_graduation():
    res = _resolve(_education_block("Start date year", "End date year"))[4]
    _filled(res, "education_end_year", "2026", "first_unique_equal")
    assert res.option_args == ["2026"]


def test_the_end_month_is_the_graduation_month_on_a_list_that_spells_it_any_way():
    months = ["January", "February", "March", "April", "May", "June", "July", "August",
              "September", "October", "November", "December"]  # fmt: skip
    fields = _education_block("End date month", kinds={"End date month": "select"})
    fields[3] = _field("End date month", "select", options=months)
    res = _resolve(fields)[3]
    assert (res.semantic_key, res.action, res.value) == ("education_end_month", "fill", "December")
    fields[3] = _field("End date month", "select", options=[f"{n:02d}" for n in range(1, 13)])
    assert _resolve(fields)[3].value == "12"


def test_the_start_date_of_the_degree_is_asked_when_no_education_start_is_stored():
    res = _resolve(_education_block("Start date month", "Start date year"))
    _asked(res[3])
    _asked(res[4])
    for unreadable in ("", "fall of 2024", "2024"):
        answers = {**ANSWERS, "education_start": unreadable}
        res = _resolve(_education_block("Start date month", "Start date year"), answers)
        _asked(res[3])
        _asked(res[4])


STARTED = {**ANSWERS, "education_start": "August 2024"}


def test_the_start_year_of_the_degree_is_the_year_of_the_stored_education_start():
    res = _resolve(_education_block("Start date year", "End date year"), STARTED)
    _filled(res[3], "education_start_year", "2024", "first_unique_equal")
    assert res[3].option_args == ["2024"]
    _filled(res[4], "education_end_year", "2026", "first_unique_equal")


def test_the_start_month_is_the_stored_month_on_a_list_that_spells_it_any_way():
    months = ["January", "February", "March", "April", "May", "June", "July", "August",
              "September", "October", "November", "December"]  # fmt: skip
    fields = _education_block("Start date month")
    fields[3] = _field("Start date month", "select", options=months)
    res = _resolve(fields, STARTED)[3]
    assert (res.semantic_key, res.action, res.value) == ("education_start_month", "fill", "August")
    fields[3] = _field("Start date month", "select", options=[f"{n:02d}" for n in range(1, 13)])
    assert _resolve(fields, STARTED)[3].value == "08"
    fields[3] = _field("Start date month", "select", options=[m[:3] for m in months])
    assert _resolve(fields, STARTED)[3].value == "Aug"


def test_a_lone_start_date_box_or_a_jobs_start_date_stays_asked_with_education_start_stored():
    _asked(_one(_field("Start date", "text"), STARTED))
    job = [_field("Company*"), _field("Job title*"), _field("Start date month"), _field("Start date year")]
    res = _resolve(job, STARTED)
    _asked(res[2])
    _asked(res[3])


def test_a_degree_with_no_graduation_month_leaves_the_end_date_asked():
    answers = {**ANSWERS, "degree": "Bachelor's"}
    _asked(_resolve(_education_block("End date year"), answers)[3])


# --- E-signature, sports, referral follow-up, school and graduation re-confirmation -----


@pytest.mark.parametrize(
    "label",
    [
        "By typing your name below you acknowledge that you have read and agree to the Terms of Use",
        # "legal name" would classify as full_name (the same value, a different key), so this
        # label says "name" alone: the shape that reaches the e-signature rule.
        "By entering your name below you attest that you have read the Candidate Agreement",
    ],
)
def test_a_typed_name_certifying_a_policy_is_the_stored_full_name(label):
    _filled(_one(_field(label)), "e_signature", "Sam Rivera")


def test_a_name_box_that_the_acknowledgment_rule_would_have_taken_gets_the_name_instead():
    # Without the e-signature rule this box would be answered "I acknowledge".
    label = "By typing your name below you acknowledge that you have read and agree to the Terms of Use"
    assert _one(_field(label)).value == "Sam Rivera"
    assert _one(_field(label, "textarea")).value == "Sam Rivera"


def test_a_typed_name_certifying_a_fact_is_not_an_e_signature():
    # He states a fact (authorization to work) under his name only when a rule says so.
    label = "By typing your name below you certify that you are authorized to work in the United States"
    res = _one(_field(label))
    assert res.semantic_key != "e_signature" and res.value != "Sam Rivera", res


def test_a_typed_name_with_no_stored_full_name_is_not_an_e_signature():
    label = "By typing your name below you acknowledge that you have read and agree to the Terms of Use"
    res = _one(_field(label), {k: v for k, v in ANSWERS.items() if k != "full_name"})
    assert res.semantic_key != "e_signature", res


def test_the_e_signature_rule_is_the_unattended_lane_only():
    label = "By typing your name below you acknowledge that you have read and agree to the Terms of Use"
    assert _one(_field(label), autoapply=False).semantic_key != "e_signature"


def test_a_sports_question_is_the_sports_fact_even_in_a_textarea():
    label = "Did you play any sports in high school or college? Please describe."
    _filled(_one(_field(label, "textarea")), "sports", SA.sports)
    _filled(_one(_field(label, "text")), "sports", SA.sports)


def test_a_sports_question_that_is_not_about_school_is_not_the_sports_fact():
    assert _one(_field("Which sports team do you support?")).semantic_key != "sports"


def test_the_follow_up_to_an_employee_referral_is_skipped_never_filled():
    label = (
        'If "Employee Referral" was selected for "How did you hear about this job?" '
        "please type the full name of the referring employee"
    )
    res = _one(_field(label, "text"))
    assert (res.semantic_key, res.action, res.value) == ("referral_name", "skip", None), res
    # A textarea is no short-fact box: the rule leaves it, and it is asked, never filled.
    res = _one(_field(label, "textarea"))
    assert res.action == "ask" and res.value is None, res


def test_reconfirming_the_university_picks_the_stored_school():
    label = "Please re-confirm the university you currently attend *"
    _filled(_one(_field(label)), "school", ANSWERS["school"], "first_unique_equal")
    res = _one(_field(label, "select", options=["Other Tech", ANSWERS["school"]]))
    assert (res.semantic_key, res.action, res.value) == ("school", "fill", ANSWERS["school"])
    _asked(_one(_field(label, "select", options=["Other Tech", "Another U"])))


def test_reconfirming_the_university_with_no_stored_school_is_asked():
    label = "Please re-confirm the university you currently attend *"
    _asked(_one(_field(label), {"full_name": "Sam Rivera"}))


def test_reconfirming_the_graduation_date_picks_month_and_year_of_the_degree_line():
    label = "Please re-confirm your expected graduation date*"
    res = _one(_field(label))
    _filled(res, "graduation_date", "December 2026", "first_unique_equal")
    assert res.option_args == ["December 2026", "Dec 2026", "12/2026", "12/2026"]
    res = _one(_field(label, "select", options=["May 2026", "Dec 2026", "May 2027"]))
    assert (res.action, res.value) == ("fill", "Dec 2026")


def test_reconfirming_a_graduation_date_with_no_month_in_the_degree_is_asked():
    label = "Please re-confirm your expected graduation date*"
    _asked(_one(_field(label), {**ANSWERS, "degree": "Bachelor's"}))


# --- In the office, travel ------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "This role requires working in our New York office five days a week. Are you able to commit to this?",
        "Are you comfortable with being in the office the first week?",
        "Our team works in person in the New York office four days a week. Are you able to commit to this schedule?",
    ],
)
def test_an_office_attendance_commitment_is_yes(label):
    res = _one(_field(label, "select", options=YES_NO))
    assert (res.semantic_key, res.action, res.value) in {
        ("office_commitment", "fill", "Yes"),
        ("onsite_ok", "fill", "Yes"),
    }, res


def test_an_office_commitment_in_a_react_select_is_the_affirm_rule():
    label = "This role requires working in our New York office five days a week. Do you confirm you are able to do so?"
    res = _one(_field(label, "text"))
    assert res.action == "fill" and res.value == "Yes"


@pytest.mark.parametrize(
    "label",
    [
        "Which office would you prefer to work from?",
        "Are you open to remote work?",
        "How many days a week would you like to be in the office?",
        "Are you currently located in and able to work in our NYC office five days a week?",
        "Would you like us to help with an office relocation package?",
    ],
)
def test_office_questions_that_are_not_a_commitment_stay_asked(label):
    res = _one(_field(label, "select", options=YES_NO))
    assert res.semantic_key != "office_commitment", res


def test_the_affirm_rule_takes_yes_or_the_one_assenting_option():
    pick = solve.pick_option
    assert pick("affirm", ["Yes"], ["Yes", "No"]) == "Yes"
    assert pick("affirm", ["Yes"], ["I understand and acknowledge", "I do not"]) == (
        "I understand and acknowledge"
    )
    assert pick("affirm", ["Yes"], ["Maybe", "Not sure"]) is None


def test_travel_up_to_the_stated_percent_is_yes():
    label = "Are you willing to travel up to 25% of the time?"
    res = _one(_field(label, "select", options=YES_NO))
    assert (res.semantic_key, res.action, res.value) == ("travel", "fill", "Yes")
    res = _one(_field("Are you willing to travel for this role?", "select", options=YES_NO))
    assert (res.semantic_key, res.value) == ("travel", "Yes")


def test_travel_beyond_the_stated_percent_is_asked():
    label = "Are you willing to travel up to 50% of the time?"
    _asked(_one(_field(label, "select", options=YES_NO)))


def test_a_travel_restriction_text_box_and_a_travel_share_text_box():
    res = _one(_field("Do you have any travel restrictions or limitations?", "text"))
    _filled(res, "travel", "I can travel up to 25% of the time.")
    res = _one(_field("What percentage of the time can you travel?", "text"))
    _filled(res, "travel", "25%")


# --- Agreements, discharge, family ---------------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "Are you currently subject to a non-compete or garden leave agreement?",
        "Are you bound by any agreement that restricts your ability to work for us?",
        "Do you have any non-solicitation obligations to a current or former employer?",
    ],
)
def test_a_non_compete_or_garden_leave_question_is_no(label):
    res = _one(_field(label, "select", options=YES_NO))
    assert (res.semantic_key, res.action, res.value) == ("agreements", "fill", "No"), res


def test_a_non_compete_list_box_is_none():
    label = "List any non-compete or garden leave agreements you are subject to"
    _filled(_one(_field(label, "text")), "agreements", "None")


@pytest.mark.parametrize(
    "label",
    [
        "Are you willing to sign a non-compete agreement?",
        "What is your non-compete compensation expectation?",
        "Describe your preferred garden leave length",
    ],
)
def test_a_question_about_signing_or_paying_for_an_agreement_stays_asked(label):
    assert _one(_field(label, "select", options=YES_NO)).semantic_key != "agreements"


def test_ever_discharged_or_asked_to_resign_is_no():
    label = "Have you ever been discharged, terminated or asked to resign from a position?"
    res = _one(_field(label, "select", options=YES_NO))
    assert (res.semantic_key, res.value) == ("discharged", "No")


def test_family_at_the_company_is_no_only_when_the_label_names_the_company_or_us():
    label = "Do you have any family members employed at Acme Corp?"
    res = _one(_field(label, "select", options=YES_NO), company="Acme Corp")
    assert (res.semantic_key, res.action, res.value) == ("family_at_company", "fill", "No")
    res = _one(_field("Are any relatives of yours employed by the company?", "radio", options=YES_NO))
    assert (res.semantic_key, res.value) == ("family_at_company", "No")
    _asked(_one(_field(label, "select", options=YES_NO), company="Another Co"))


def test_family_list_box_is_none():
    label = "Please list any relatives working at the company"
    _filled(_one(_field(label, "text")), "family_at_company", "None")


# --- Working location, start dates ---------------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "From where do you intend to work?",
        "What city and state will you be working from?",
        "City and state where you intend to work",
    ],
)
def test_a_working_location_text_box_is_the_stated_working_location(label):
    # "From where do you intend to work?" is classified work_location (the posting's city),
    # and solve fills the stated working location in its place; the other two are key-less.
    res = _one(_field(label, "text"))
    assert (res.action, res.value, res.option_rule) == ("fill", SA.working_location, ""), res
    assert res.semantic_key in {"working_location", "work_location"}, res


def test_a_working_location_list_is_not_filled_with_free_text():
    res = _one(_field("From where do you intend to work?", "select", options=["Atlanta, GA", "New York, NY"]))
    assert res.value != SA.working_location


def test_a_plain_location_box_is_not_the_working_location():
    # "Location" is where he lives: the stored address, never this fact.
    res = _one(_field("Working Location*", "text"))
    assert res.value != SA.working_location


@pytest.mark.parametrize(
    "label",
    [
        "What is the earliest you would want to start working for us?",
        "When could you begin?",
        "When can you start?",
    ],
)
def test_an_earliest_start_text_box_is_the_stated_date(label):
    res = _one(_field(label, "text"))
    assert (res.action, res.value) == ("fill", SA.earliest_start_text), res
    assert res.semantic_key in {"earliest_start", "start_date"}


def test_a_stored_start_date_wins_and_a_bare_start_date_box_stays_asked():
    res = _one(_field("When can you start?", "text"), {**ANSWERS, "start_date": "Two weeks"})
    assert (res.action, res.value) == ("fill", "Two weeks"), res
    _asked(_one(_field("Start date", "text")))


@pytest.mark.parametrize(
    ("label", "answer"),
    [
        ("Are you able to start in September 2026?", "No"),
        ("Are you available to start in December 2026?", "No"),
        ("Are you able to start in January 2027?", "Yes"),
        ("Are you able to start in June 2027?", "Yes"),
    ],
)
def test_a_named_start_month_is_yes_from_his_earliest_start_and_no_before(label, answer):
    res = _one(_field(label, "select", options=YES_NO))
    assert (res.semantic_key, res.action, res.value) == ("start_availability", "fill", answer)


@pytest.mark.parametrize(
    "label",
    [
        "Are you able to start before September 2026?",
        "Are you able to start immediately?",
        "Are you available to start?",
    ],
)
def test_a_start_bound_or_a_start_with_no_month_is_asked(label):
    assert _one(_field(label, "select", options=YES_NO)).semantic_key != "start_availability"


def test_the_earliest_start_constant_is_the_one_text():
    assert SA.earliest_start is not None
    assert SA.earliest_start_text == (
        f"{SA.earliest_start:%B} {SA.earliest_start.day}, {SA.earliest_start.year}"
    )


# --- Worked here, applied before ------------------------------------------------------


def test_having_worked_at_the_company_follows_the_resume():
    label = "Have you worked at DoorDash?*"
    res = _one(_field(label, "select", options=YES_NO), company="DoorDash")
    assert (res.action, res.value) == ("fill", "No")
    res = _one(_field(label, "select", options=YES_NO), {**ANSWERS, "worked_here": "Yes"}, company="DoorDash")
    assert (res.action, res.value) == ("fill", "Yes")


def test_a_company_name_with_a_marker_is_still_the_company():
    # The tracker holds "🔥 DoorDash" for a company the sweep flagged.
    label = "Have you worked at DoorDash?*"
    res = _one(_field(label, "select", options=YES_NO), company="🔥 DoorDash")
    assert (res.action, res.value) == ("fill", "No")


def test_having_applied_before_follows_the_tracker():
    label = "Have you previously applied to work at Point72?*"
    res = _one(_field(label, "select", options=YES_NO), {**ANSWERS, "applied_before": "Yes"}, company="Point72")
    assert res.action == "fill" and res.value == "Yes"
    res = _one(_field(label, "select", options=YES_NO), company="Point72")
    assert res.action == "fill" and res.value == "No"


def test_a_question_asking_both_is_yes_when_either_is_yes_and_no_when_both_are_no():
    both = "have you ever worked at or applied to acme"
    reversed_both = "have you previously applied to or worked at acme"
    assert solve._prior_answer("have you ever worked at acme", ANSWERS) == "No"
    assert solve._prior_answer("have you ever worked at acme", {**ANSWERS, "worked_here": "Yes"}) == "Yes"
    assert solve._prior_answer(both, ANSWERS) == "No"
    assert solve._prior_answer(both, {**ANSWERS, "applied_before": "Yes"}) == "Yes"
    assert solve._prior_answer(both, {**ANSWERS, "worked_here": "Yes"}) == "Yes"
    # A both-question is read from both sources whichever clause comes first.
    assert solve._prior_answer(reversed_both, ANSWERS) == "No"
    assert solve._prior_answer(reversed_both, {**ANSWERS, "worked_here": "Yes"}) == "Yes"
    assert solve._prior_answer(reversed_both, {**ANSWERS, "applied_before": "Yes"}) == "Yes"
    assert solve._prior_answer("have you ever applied to acme", {**ANSWERS, "applied_before": "Yes"}) == "Yes"
    assert solve._prior_answer("have you ever applied to acme", ANSWERS) == "No"


def test_a_prior_question_with_a_missing_source_is_asked():
    no_sources = {k: v for k, v in ANSWERS.items() if k not in ("worked_here", "applied_before")}
    label = "Have you worked at DoorDash?*"
    _asked(_one(_field(label, "select", options=YES_NO), no_sources, company="DoorDash"))
    assert solve._prior_answer("have you worked at acme", {"worked_here": ""}) is None


def test_a_question_about_another_company_is_not_answered_from_this_ones_record():
    label = "Have you worked at Globex?*"
    _asked(_one(_field(label, "select", options=YES_NO), company="DoorDash"))


# --- Skills named on the resume -------------------------------------------------------


def test_a_skill_the_resume_names_is_yes():
    label = "Are you comfortable working with Excel and Python?"
    res = _one(_field(label, "select", options=YES_NO))
    assert (res.semantic_key, res.action, res.value) == ("skills", "fill", "Yes")


def test_a_skill_the_resume_does_not_name_stays_asked():
    # None of these tools is in the stored resume_skills, so Yes would be an invented claim.
    for label in (
        "Are you proficient in SQL?",
        "Are you comfortable working with Excel and Tableau?",
        "Do you have experience with Salesforce or HubSpot?",
    ):
        _asked(_one(_field(label, "select", options=YES_NO)))


def test_sql_is_yes_once_the_resume_skills_list_it_and_a_sibling_unnamed_tool_still_asks():
    # posting_answers lists sql when the resume shows Postgres, SQLite or SQLAlchemy.
    answers = {**ANSWERS, "resume_skills": "excel,postgres,python,sql"}
    res = _one(_field("Are you proficient in SQL?", "select", options=YES_NO), answers)
    assert (res.semantic_key, res.action, res.value) == ("skills", "fill", "Yes")
    for label in ("Are you comfortable with SQL and Pandas?", "Are you comfortable with SQL and Tableau?"):
        _asked(_one(_field(label, "select", options=YES_NO), answers))


def test_a_skill_question_with_no_known_skill_or_no_resume_skills_is_asked():
    _asked(_one(_field("Are you comfortable working in a fast-paced environment?", "select", options=YES_NO)))
    _asked(_one(_field("Are you comfortable with Excel?", "select", options=YES_NO), {"full_name": "Sam"}))


# --- Living in New York, proprietary trading -----------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "Are you currently located in New York City?",
        "Do you currently live in NYC?",
        "Are you currently based in the New York area?",
        "This role is hybrid (minimum 2 days/week in our NYC-Flatiron office). Are you currently located in and able to work in the office?",
        # The scan stores the label cut at 140 characters; the live form shows it whole.
        "This role is hybrid (minimum 2 days/week in our NYC-Flatiron office). Are you currently located in and able to work i...",
    ],
)
def test_currently_living_in_new_york_is_no_because_he_lives_in_marietta(label):
    answers = {**ANSWERS, "address_city": "Alpharetta"}
    # "Are you currently located in New York City?" is the older based_in rule's, the same No.
    keys = {"currently_in_nyc", "based_in"}
    for kind, options in (("radio", YES_NO), ("select", YES_NO)):
        res = _one(_field(label, kind, options=options), answers)
        assert (res.action, res.value) == ("fill", "No") and res.semantic_key in keys, res
    res = _one(_field(label, "text"), answers)
    assert (res.action, res.value) == ("fill", "No") and res.semantic_key in keys, res


def test_the_located_in_and_able_to_work_compound_is_the_currently_in_nyc_rule():
    label = (
        "This role is hybrid (minimum 2 days/week in our NYC-Flatiron office). "
        "Are you currently located in and able to work i..."
    )
    res = _one(_field(label, "radio", options=YES_NO), {**ANSWERS, "address_city": "Alpharetta"})
    assert (res.semantic_key, res.action, res.value) == ("currently_in_nyc", "fill", "No"), res


@pytest.mark.parametrize(
    "label",
    [
        "Do you currently live in NYC, or are you willing to relocate?",
        "Are you currently located in New York or willing to relocate there?",
        "Are you currently located within an hour of our New York office?",
        "Are you currently located in Atlanta? Our New York office is hybrid.",
        "Are you currently located in the United States?",
        "Are you willing to relocate to New York?",
        "Are you currently located in New York or the United States?",
        "Are you currently based in New York or San Francisco today?",
    ],
)
def test_a_residence_question_that_offers_relocating_or_names_another_place_is_not_no(label):
    res = _one(_field(label, "select", options=YES_NO), {**ANSWERS, "address_city": "Alpharetta"})
    assert res.semantic_key != "currently_in_nyc", res


def test_living_in_new_york_is_asked_when_no_city_is_stored():
    label = "Are you currently located in New York City?"
    res = _one(_field(label, "select", options=YES_NO), {k: v for k, v in ANSWERS.items()})
    assert res.semantic_key != "currently_in_nyc", res


def test_living_in_new_york_is_asked_when_the_stored_city_is_in_new_york():
    answers = {**ANSWERS, "address_city": "Brooklyn"}
    res = _one(_field("Are you currently located in New York City?", "select", options=YES_NO), answers)
    assert res.semantic_key != "currently_in_nyc", res


def test_proprietary_trading_experience_is_the_stored_no():
    answers = {**ANSWERS, "prop_trading": "No"}
    label = "Do you have relevant internship experience at a proprietary trading firm?*"
    res = _one(_field(label, "select", options=YES_NO), answers)
    assert (res.semantic_key, res.action, res.value) == ("prop_trading", "fill", "No"), res
    res = _one(_field(label, "text"), answers)
    assert (res.semantic_key, res.action, res.value) == ("prop_trading", "fill", "No"), res


@pytest.mark.parametrize(
    "label",
    [
        "How many years of proprietary trading experience do you have?",
        "Are you interested in proprietary trading?",
        "Why do you want to work at a proprietary trading firm?",
        "Describe your proprietary trading experience",
    ],
)
def test_a_proprietary_trading_question_that_is_no_yes_no_is_asked(label):
    res = _one(_field(label, "select", options=YES_NO), {**ANSWERS, "prop_trading": "No"})
    assert res.semantic_key != "prop_trading", res


def test_proprietary_trading_with_no_stored_answer_is_asked():
    label = "Do you have relevant internship experience at a proprietary trading firm?*"
    _asked(_one(_field(label, "select", options=YES_NO)))
    _asked(_one(_field(label, "select", options=YES_NO), {**ANSWERS, "prop_trading": ""}))


# --- Over 18, current or most recent employer and title -------------------------------

OVER_18_LABELS = [
    "Are you over 18 years of age and have the ability to furnish proof if hired? *",
    "Are you 18 years of age or older?",
    "Are you at least 18 years old?",
    "Are you over the age of 18?",
    "Are you 18+?",
]


@pytest.mark.parametrize("label", OVER_18_LABELS)
def test_an_over_18_question_is_the_stored_yes(label):
    res = _one(_field(label, "select", options=YES_NO), {**ANSWERS, "over_18": "Yes"})
    _filled(res, "over_18", "Yes")
    _filled(_one(_field(label, "text"), {**ANSWERS, "over_18": "Yes"}), "over_18", "Yes", "yes_no")


def test_an_over_18_question_with_no_stored_answer_or_a_stored_no_is_asked_or_no():
    label = OVER_18_LABELS[1]
    _asked(_one(_field(label, "select", options=YES_NO)))
    _asked(_one(_field(label, "select", options=YES_NO), {**ANSWERS, "over_18": "maybe"}))
    _filled(
        _one(_field(label, "select", options=YES_NO), {**ANSWERS, "over_18": "No"}),
        "over_18", "No",
    )  # fmt: skip


@pytest.mark.parametrize(
    "label",
    [
        "Are you over 18 and willing to travel?",
        "Are you over 18 years of age and able to relocate?",
        "Are you 18?",
        "Why are you over 18?",
        "Are you over 18 and legally authorized to work in the US?",
    ],
)
def test_an_over_18_question_with_another_condition_or_a_bare_18_is_asked(label):
    res = _one(_field(label, "select", options=YES_NO), {**ANSWERS, "over_18": "Yes"})
    assert res.semantic_key != "over_18", res


def test_an_over_18_checkbox_or_textarea_is_not_answered_by_the_rule():
    answers = {**ANSWERS, "over_18": "Yes"}
    res = _one(_field(OVER_18_LABELS[1], "textarea"), answers)
    assert res.semantic_key != "over_18", res


@pytest.mark.parametrize(
    ("label", "key", "value"),
    [
        ("Who is your current or most recent employer?*", "recent_employer", SA.current_employer),
        ("What is your current or more recent job title?*", "recent_title", SA.current_title),
        ("What is your most recent position?", "recent_title", SA.current_title),
    ],
)
def test_a_long_current_or_most_recent_employer_or_title_box_is_the_current_role(label, key, value):
    _filled(_one(_field(label, "text")), key, value)


def test_a_short_current_company_box_still_reads_the_stored_current_company():
    answers = {**ANSWERS, "current_company": "Example Co"}
    _filled(_one(_field("Current company", "text"), answers), "current_company", "Example Co")
    _filled(_one(_field("Your current employer", "text"), answers), "current_company", "Example Co")


@pytest.mark.parametrize(
    "label",
    [
        "Who is your current employer and what is your title?",
        "What is your current or most recent job title and salary?",
        "Your previous employer",
        "Why do you want to leave your current employer?",
    ],
)
def test_a_two_part_or_other_employer_question_is_not_the_current_role_rule(label):
    res = _one(_field(label, "text"))
    assert res.semantic_key not in ("recent_employer", "recent_title"), res


def test_a_current_employer_list_is_not_filled_by_the_text_rule():
    res = _one(_field("Who is your current or most recent employer?", "select", options=["A", "B"]))
    assert res.semantic_key not in ("recent_employer", "recent_title"), res


# --- Never the answers below ----------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "I consent to receive future opportunities and communications from Acme",
        "I agree to the Acme AI policy and will not use AI tools in my interviews",
        "I am willing to work in the office five days a week",
        "I would like to receive marketing text messages",
    ],
)
def test_a_consent_or_commitment_checkbox_is_never_checked_by_a_factual_rule(label):
    res = _one(_field(label, "checkbox"), company="Acme")
    assert res.action in {"ask", "skip"}, res


def test_an_ai_policy_question_is_never_a_factual_answer():
    label = "Do you agree not to use AI tools during the interview process at Acme?"
    res = _one(_field(label, "select", options=YES_NO), company="Acme")
    _asked(res)


def test_the_assist_lane_never_gets_the_factual_answers():
    for label in (
        "Do you have any family members employed at Acme Corp?",
        "Are you able to start in September 2026?",
        "What is your earliest start date?",
    ):
        _asked(_one(_field(label, "select", options=YES_NO), company="Acme Corp", autoapply=False))


def test_a_demographic_question_is_still_declined_not_answered_from_a_fact():
    res = _one(_field("What is your gender?", "select", options=["Male", "Female", "Decline to self identify"]))
    assert (res.action, res.value) == ("fill", "Decline to self identify")


# --- How he heard of the company -----------------------------------------------------


def test_how_heard_in_a_react_select_is_the_company_site_or_other_rule():
    answers = {**ANSWERS, "how_heard": "Company careers page"}
    res = _one(_field("How did you hear about this job?*", "text"), answers, company="DV Trading")
    assert (res.option_rule, res.option_args) == (
        "company_site_or_other", ["Company careers page", "DV Trading"],
    )  # fmt: skip


def test_the_company_site_or_other_rule_takes_other_only_when_no_site_is_named():
    pick = solve.pick_option
    args = ["Company careers page", "DV Trading"]
    assert pick("company_site_or_other", args, ["Company website", "LinkedIn", "Other"]) == "Company website"
    assert pick("company_site_or_other", args, ["Friend", "Other"]) == "Other"
    assert pick("company_site_or_other", args, ["LinkedIn", "Indeed", "Other"]) == "Other"
    assert pick("company_site_or_other", args, ["DV Website", "Other"]) is None
    assert pick("company_site_or_other", args, ["Careers page", "Other"]) == "Careers page"
    assert pick("company_site_or_other", args, ["Friend", "Referral"]) is None
    assert pick("company_site_or_other", args, ["Other", "Other (please specify)"]) is None


# --- An office list with a ranked city -------------------------------------------------


def _office_group(title, cities, *, required=True):
    return [
        _field(f"{city}{CHECKBOX_TITLE_SEP}{title}", "checkbox", required=required) for city in cities
    ]


def _ticked(fields, answers=None):
    results = _resolve(fields, answers)
    return [f.label.partition(CHECKBOX_TITLE_SEP)[0] for f, r in zip(fields, results) if r.action == "check"]


def test_an_office_list_ticks_only_the_top_ranked_city():
    fields = _office_group(
        "In what cities are you available to work?*",
        ["Chicago", "Denver", "London", "New York City", "San Francisco", "Paris"],
    )
    assert _ticked(fields) == ["New York City"]
    two = _office_group("Which office will you work from?", ["San Francisco", "Atlanta", "Chicago"])
    assert _ticked(two, {**ANSWERS, "address_state": "GA"}) == ["Atlanta"]
    # Atlanta ranks only when he lives in Georgia; with no stored state it is no ranked city.
    assert _ticked(two) == ["Chicago"]
    assert _ticked(_office_group("Which office will you work from?", ["San Francisco", "Chicago"])) == ["Chicago"]


def test_an_office_list_with_no_ranked_city_stays_asked():
    fields = _office_group("Which office will you work from?", ["London", "Paris", "Singapore", "Sydney"])
    assert _ticked(fields) == []
    assert all(r.action == "ask" for r in _resolve(fields))


def test_a_list_of_where_he_lives_now_is_not_an_office_pick():
    fields = _office_group(
        "Which of the following cities are you currently located in?*",
        ["Chicago", "New York City", "San Francisco"],
    )
    assert _ticked(fields) == []


def test_an_office_list_that_is_not_required_is_not_ticked():
    fields = _office_group("Which office will you work from?", ["New York City", "Chicago"], required=False)
    assert _ticked(fields) == []


# --- Military service ------------------------------------------------------------------


def test_military_service_is_no():
    res = _one(_field("Have you ever served in the military?", "select", options=YES_NO), {})
    assert (res.semantic_key, res.value) == ("military_service", "No")


# --- The facts file still says what the rules say ------------------------------------

FACTS = operator_file("data/facts.local.md")
SECTION = "## Standing application answers"
_BULLET = re.compile(r"^\s*[-*]\s+(?P<label>[^:]+?)\s*:\s*(?P<value>.*?)\s*$")


def _bullets() -> dict[str, str]:
    if not FACTS.exists():
        pytest.skip("data/facts.local.md is not on this machine")
    lines = FACTS.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(SECTION)]
    if not starts:
        pytest.skip(f"data/facts.local.md has no '{SECTION}' section")
    found: dict[str, str] = {}
    for line in lines[starts[0] + 1 :]:
        if line.startswith("#"):
            break
        hit = _BULLET.match(line)
        if hit:
            found[hit.group("label").lower()] = hit.group("value").rstrip(".").strip()
    return found


def _bullet(*words: str) -> str:
    bullets = _bullets()
    hits = [v for label, v in bullets.items() if all(w in label for w in words)]
    assert len(hits) == 1, f"facts.local.md should have one standing answer about {words}: {len(hits)}"
    return hits[0]


def test_the_earliest_start_constant_is_the_facts_file_date():
    assert _bullet("earliest", "start") == SA.earliest_start_text


def test_the_travel_percent_is_the_facts_file_percent():
    assert f"{SA.travel_percent}%" in _bullet("travel")


def test_the_working_location_is_the_facts_file_location():
    assert _bullet("working", "location") == SA.working_location


def test_the_sports_answer_is_the_facts_file_sports():
    value = _bullet("sports").lower()
    assert value.rstrip(".") == SA.sports.lower().rstrip(".")


def test_the_no_answers_are_the_facts_file_nones():
    assert _bullet("military").lower().startswith("none")
    assert solve._autoapply_rule_answers()["military_service"] == "No"
    assert _bullet("non-compete").lower().startswith("none")
    assert _bullet("discharged").lower().startswith("never")
    assert _bullet("family", "employed").lower().startswith("none")


def test_the_e_signature_is_allowed_with_the_full_legal_name():
    value = _bullet("e-signature").lower()
    assert value.startswith("allowed") and "full legal name" in value


def _facts_text() -> str:
    if not FACTS.exists():
        pytest.skip("data/facts.local.md is not on this machine")
    return FACTS.read_text(encoding="utf-8")


def test_the_current_role_constants_are_the_facts_file_current_role():
    text = _facts_text()
    work = text.split("## Work history", 1)[-1].split("\n## ", 1)[0]
    line = " ".join(work.split())
    assert f"{SA.current_employer}. {SA.current_title}, Jan 2024 to present" in line


# --- Questions that blocked real postings on 2026-10-27 -------------------------------

STORED = {**ANSWERS, "location": "Alpharetta, GA", "address_city": "Alpharetta",
          "address_state": "GA", "linkedin": "https://www.linkedin.com/in/example"}  # fmt: skip


@pytest.mark.parametrize("kind", ["radio", "select", "text"])
@pytest.mark.parametrize(
    "label",
    [
        "Have you worked at a startup?",
        "Have you worked at a start-up?",
        "Have you ever worked at a Series A-C / early-stage company?",
        "Have you ever worked at an early-stage startup before? *",
        "Did you previously work for a Series B startup?",
    ],
)
def test_having_worked_at_a_startup_is_yes(label, kind):
    res = _one(_field(label, kind, YES_NO if kind != "text" else None), STORED)
    assert (res.semantic_key, res.action) == ("startup_experience", "fill")
    assert res.value == "Yes" or kind == "text"


@pytest.mark.parametrize(
    "label",
    [
        "Are you currently employed at a startup?",
        "Do you currently work at a startup?",
        "How many years have you worked at a startup?",
        "Have you worked at a startup for more than 3 years?",
        "Have you worked at a startup in fintech?",
        "Have you worked at a startup that raised over $10M?",
        "Have you worked at a startup? If yes, which one?",
        "Which startup have you worked at?",
        "Name the startup company you have worked at",
        "Have you worked at a startup in the last 2 years?",
        "Have you worked at a bank?",
        "Have you worked at a Series B company?",
        "Have you worked at a Series A-C company?",
        "Have you worked at a venture-backed company?",
        "Would you like to work at a startup?",
    ],
)
def test_other_startup_questions_are_asked(label):
    res = _one(_field(label, "radio", YES_NO), STORED)
    assert res.action == "ask" and res.semantic_key != "startup_experience", res


def test_the_startup_question_is_asked_in_the_assist_lane_and_in_a_textarea():
    label = "Have you worked at a startup?"
    assert _one(_field(label, "radio", YES_NO), STORED, autoapply=False).action == "ask"
    assert _one(_field(label, "textarea"), STORED).action == "ask"


GRADUATE_FROM = "If you have a college degree, what institution(s) did you graduate from?"
ATTENDING = (
    'Which university are you currently attending or did you last attend? Please select '
    '"Other (School Not Listed)" if you do not see your school.'
)


def test_the_graduate_from_text_box_gets_the_expected_degree_not_a_completed_one():
    res = _one(_field(GRADUATE_FROM, "text"), STORED)
    _filled(res, "school", SA.education_text)
    assert res.value.startswith(f"{SA.school} (")
    assert "expected December 2026" in res.value
    assert "graduated" not in res.value.casefold()


def test_the_currently_attending_list_takes_the_school_option_never_other():
    options = ["Emory University", "Georgia State University",
               SA.school, "Other (School Not Listed)"]  # fmt: skip
    for kind in ("select", "radio"):
        res = _one(_field(ATTENDING, kind, options), STORED)
        assert (res.semantic_key, res.action, res.value) == (
            "school", "fill", SA.school
        )
    res = _one(_field(ATTENDING, "select", [SA.school_aliases[0], "Other (School Not Listed)"]), STORED)
    assert res.value == SA.school_aliases[0]


def test_the_currently_attending_list_with_no_school_option_is_asked_not_other():
    res = _one(_field(ATTENDING, "select", ["Emory University", "Other (School Not Listed)"]), STORED)
    assert res.action == "ask" and res.value is None
    two = [SA.school_aliases[0], SA.school, "Other (School Not Listed)"]
    assert _one(_field(ATTENDING, "select", two), STORED).action == "ask"


def test_the_currently_attending_react_select_is_applied_by_the_school_rule():
    res = _one(_field(ATTENDING, "text"), STORED)
    assert (res.semantic_key, res.action, res.option_rule) == ("school", "fill", "school")


@pytest.mark.parametrize("kind", ["text", "select", "radio"])
@pytest.mark.parametrize(
    "label",
    [
        "What institution(s) did you graduate from?",  # not conditional on holding a degree
        "Which university did you graduate from?",
        "What high school did you graduate from?",
        "If you have a college degree, what was your GPA?",
        "Which university do you want to attend?",
        "Which university did you attend as an undergraduate?",
    ],
)
def test_other_school_questions_do_not_get_the_school_answer(label, kind):
    options = [SA.school, "Other (School Not Listed)"] if kind != "text" else None
    res = _one(_field(label, kind, options), STORED)
    assert res.value != SA.education_text
    assert res.option_rule != "school"
    assert res.value != SA.school or res.action != "fill"


def test_the_school_questions_are_asked_in_the_assist_lane():
    assert _one(_field(GRADUATE_FROM, "text"), STORED, autoapply=False).action == "ask"
    options = [SA.school, "Other (School Not Listed)"]
    assert _one(_field(ATTENDING, "select", options), STORED, autoapply=False).action == "ask"


def test_a_bare_linkedin_long_answer_box_gets_the_stored_profile_url():
    for label in ("LinkedIn", "LinkedIn *", "LinkedIn \u2731", "Your LinkedIn", "LinkedIn Profile URL"):
        res = _one(_field(label, "textarea"), STORED)
        _filled(res, "linkedin", "https://www.linkedin.com/in/example")


def test_a_linkedin_long_answer_box_is_asked_in_the_assist_lane_and_when_not_a_bare_label():
    assert _one(_field("LinkedIn", "textarea"), STORED, autoapply=False).action == "ask"
    for label in ("Tell us about a time you used LinkedIn to find a mentor", "LinkedIn or portfolio and why"):
        assert _one(_field(label, "textarea"), STORED).action == "ask", label
    no_linkedin = {k: v for k, v in STORED.items() if k != "linkedin"}
    assert _one(_field("LinkedIn", "textarea"), no_linkedin).action == "ask"


@pytest.mark.parametrize("kind", ["radio", "select"])
@pytest.mark.parametrize(
    ("label", "stored_state", "expected"),
    [
        ("Are you a resident of California? \u2731", "GA", "No"),
        ("Are you a resident of California?", "GA", "No"),
        ("Are you a resident of the state of California?", "Georgia", "No"),
        ("Are you a resident of Georgia?", "GA", "Yes"),
        ("Are you a resident of Georgia? *", "Georgia", "Yes"),
        ("Are you a resident of California?", "CA", "Yes"),
    ],
)
def test_state_residency_is_answered_from_the_stored_state(label, stored_state, expected, kind):
    res = _one(_field(label, kind, YES_NO), {**STORED, "address_state": stored_state})
    assert (res.semantic_key, res.action, res.value) == ("state_resident", "fill", expected)


@pytest.mark.parametrize(
    "label",
    [
        "Will you relocate to California?",
        "Are you willing to relocate to California?",
        "Are you a US resident?",
        "Are you a resident of the United States?",
        "Are you a resident of Canada?",
        "Are you a resident of California or New York?",
        "California residents: please read this notice",
        "I am a resident of California",
        "Are you a resident of California and over 18?",
    ],
)
def test_other_residency_questions_do_not_get_the_state_resident_answer(label):
    res = _one(_field(label, "radio", YES_NO), STORED)
    assert res.semantic_key != "state_resident", res


def test_state_residency_is_asked_without_a_stored_state_and_in_the_assist_lane():
    label = "Are you a resident of California?"
    assert _one(_field(label, "radio", YES_NO), {**STORED, "address_state": ""}).action == "ask"
    assert _one(_field(label, "radio", YES_NO), STORED, autoapply=False).action == "ask"


MELIO = (
    "This position requires working onsite in New York City three days per week and includes "
    "in-person interviews. Are you able to commit to this schedule?"
)
TALOS = (
    "Talos operates on a hybrid working model, requiring three days in the office per week, "
    "with flexibility on which days. Are you comfortable with this arrangement?"
)


@pytest.mark.parametrize("kind", ["radio", "select"])
@pytest.mark.parametrize("label", [MELIO, TALOS])
def test_a_new_york_onsite_or_hybrid_commitment_is_yes(label, kind):
    res = _one(_field(label, kind, YES_NO), STORED)
    assert (res.semantic_key, res.action, res.value) == ("office_commitment", "fill", "Yes")


@pytest.mark.parametrize(
    "label",
    [
        "Are you available for an in-person interview at our office three days next week?",
        "Which three days per week would you prefer to be in the office?",
        "How many days per week are you willing to be onsite?",
        "Are you currently located in New York City, or willing to be onsite three days a week and conduct remote interviews?",
        "Do you require relocation assistance to work onsite three days per week?",
        "We offer flexibility on remote work. Are you able to work onsite three days per week?",
    ],
)
def test_the_onsite_description_carve_outs_do_not_widen_the_rule(label):
    res = _one(_field(label, "radio", YES_NO), STORED)
    assert res.semantic_key != "office_commitment", res


def test_the_new_york_onsite_commitment_is_asked_in_the_assist_lane():
    assert _one(_field(MELIO, "radio", YES_NO), STORED, autoapply=False).action == "ask"
    assert _one(_field(TALOS, "radio", YES_NO), STORED, autoapply=False).action == "ask"


# --- The resume names the skills the rule answers Yes for ------------------------------

MASTER = operator_file("data/apply_packs/finai/master.rm.json")


def test_every_skill_answered_yes_from_a_real_resume_is_named_on_it(monkeypatch):
    if not MASTER.exists():
        pytest.skip("operator check: set JOBHUNT_CHECK_LOCAL_FILES=1 with the master resume in place")
    from jobhunt.apply_assist import posting_answers

    monkeypatch.setattr(posting_answers, "MASTER_PATH", MASTER)
    monkeypatch.setattr(posting_answers, "FACTS_PATH", FACTS)

    skills = posting_answers._resume_skills()
    assert skills is not None
    text = MASTER.read_text(encoding="utf-8").casefold() + posting_answers._facts_skills_text()
    for skill in filter(None, skills.split(",")):
        # Named on the resume, or shown by a tool it names (SQL by Postgres or SQLAlchemy).
        words = (skill, *posting_answers._SKILL_EVIDENCE.get(skill, ()))
        assert any(re.search(rf"\b{re.escape(word)}\b", text) for word in words), skill
    assert set(skills.split(",")) <= set(solve.SKILL_VOCAB)


# --- Palantir (Lever 688246), the candidate 2026-10-27 -------------------------------------------

HOW_HEARD_TELL = "Please tell us how you heard about this opportunity."


@pytest.mark.parametrize("label", [HOW_HEARD_TELL, HOW_HEARD_TELL + " ✱"])
def test_please_tell_us_how_you_heard_is_how_heard_with_or_without_the_required_mark(label):
    from jobhunt.apply_assist.keys import classify

    assert classify(_field(label)) == "how_heard"
    assert classify(_field(label, "select", ["Company website", "LinkedIn"])) == "how_heard"


def test_please_tell_us_how_you_heard_is_filled_from_the_stored_answer():
    answers = {**ANSWERS, "how_heard": "Company website"}
    label = HOW_HEARD_TELL + " ✱"
    res = _one(_field(label), answers, company="Palantir")
    assert (res.semantic_key, res.action, res.value) == ("how_heard", "fill", "Company website"), res
    options = ["Company website", "LinkedIn", "Referral"]
    res = _one(_field(label, "select", options), answers, company="Palantir")
    assert (res.semantic_key, res.action, res.value) == ("how_heard", "fill", "Company website"), res


@pytest.mark.parametrize(
    "label",
    [
        "Please tell us how you heard about Acme's products",
        "Please tell us why you want this role",
        "Please tell us about your experience",
    ],
)
def test_the_tell_us_wording_is_how_heard_only_for_the_opportunity_question(label):
    from jobhunt.apply_assist.keys import classify

    assert classify(_field(label)) != "how_heard"


LANGUAGES = [
    "English (ENG)", "Spanish (SPA)", "French (FRA)", "German (DEU)",
    "Choose not to disclose", "Polish", "Other",
]  # fmt: skip


def _language_group(title="Language Skill(s) (Check all that apply) ✱", options=LANGUAGES):
    return [_field(o, "checkbox", required=True) for o in options], title


def _checked(fields, answers=None, **kw):
    out = _resolve(fields, answers, **kw)
    return [f.label for f, r in zip(fields, out) if r.action == "check"], out


def test_the_palantir_language_group_ticks_english_and_spanish_only():
    # The title is carried on the field above the run of boxes.
    boxes, title = _language_group()
    fields = [_field(title, "text", required=True), *boxes]
    names, out = _checked(fields)
    assert names == ["English (ENG)", "Spanish (SPA)"], out
    assert all("standing answer" in r.reason for r in out if r.action == "check")


def test_the_language_group_title_can_ride_in_each_label_or_as_an_ashby_title():
    title = "Language Skill(s) (Check all that apply) ✱"
    carried = [_field(f"{title} {o}", "checkbox") for o in LANGUAGES]
    assert _checked(carried)[0] == [carried[0].label, carried[1].label]
    titled = [_field(f"{o}{CHECKBOX_TITLE_SEP}Languages", "checkbox") for o in LANGUAGES]
    assert _checked(titled)[0] == [titled[0].label, titled[1].label]


@pytest.mark.parametrize(
    "title",
    [
        "Which languages are you fluent in?",
        "Languages (professional proficiency)",
        "Which languages do you speak natively?",
        "Languages you can work in at a business level",
        "Are you bilingual? Check all that apply",
    ],
)
def test_a_language_group_that_asks_for_fluency_ticks_english_only(title):
    boxes, _ = _language_group()
    names, _ = _checked([_field(title, "text", required=True), *boxes])
    assert names == ["English (ENG)"]


def test_a_language_group_with_no_english_option_ticks_nothing():
    boxes, title = _language_group(options=["Spanish (SPA)", "French (FRA)", "Other"])
    assert _checked([_field(title, "text"), *boxes])[0] == []


def test_a_language_group_of_english_alone_or_optional_ticks_nothing():
    boxes, title = _language_group(options=["English (ENG)", "Choose not to disclose"])
    assert _checked([_field(title, "text"), *boxes])[0] == []
    optional = [_field(o, "checkbox", required=False) for o in LANGUAGES]
    assert _checked(optional)[0] == []


def test_the_language_group_is_asked_in_the_assist_lane_and_never_ticks_the_decline_box():
    boxes, title = _language_group()
    names, out = _checked([_field(title, "text"), *boxes], autoapply=False)
    assert names == [] and {r.action for r in out[1:]} == {"ask"}
    names, _ = _checked([_field(title, "text"), *boxes])
    assert "Choose not to disclose" not in names and "Other" not in names


PARTNER = (
    "Please share my resume and contact information with external Palantir partners "
    "for job seeking purposes. ✱"
)


def test_the_partner_resume_sharing_checkbox_is_checked():
    res = _one(_field(PARTNER, "checkbox"), company="Palantir")
    assert (res.action, res.value) == ("check", None), res
    assert "standing answer" in res.reason


def test_the_partner_resume_sharing_question_takes_the_yes_option():
    for kind in ("select", "radio"):
        res = _one(_field(PARTNER, kind, ["Yes", "No"]), company="Palantir")
        assert (res.action, res.value) == ("fill", "Yes"), res
    res = _one(_field(PARTNER, "select", ["I agree", "I do not agree"]))
    assert (res.action, res.value) == ("fill", "I agree"), res
    assert _one(_field(PARTNER, "select", ["Yes", "Yes, please"])).action == "ask"
    assert _one(_field(PARTNER, "select", ["Maybe", "No"])).action == "ask"
    assert _one(_field(PARTNER, "text")).action == "ask"


@pytest.mark.parametrize(
    "label",
    [
        "I agree to the privacy policy",
        "Please share my information with partners for marketing",
        "I consent to the processing of my data by Palantir and its partners",
        "I authorize a background check and sharing of the result with partners",
        "Please share my resume with our partners and subscribe me to the newsletter",
        "Do you agree to share your resume with third parties?",
    ],
)
def test_other_consents_are_not_the_partner_resume_sharing_rule(label):
    from jobhunt.apply_assist import solve as s

    assert not s._is_partner_share_consent(s._clean_text(label))


def test_the_partner_resume_sharing_consent_is_asked_in_the_assist_lane():
    assert _one(_field(PARTNER, "checkbox"), autoapply=False).action == "ask"
    assert _one(_field(PARTNER, "select", ["Yes", "No"]), autoapply=False).action == "ask"


# --- The other boxes of a group with a ticked pick (2026-10-27) ----------------------
# Lever and Ashby put `required` on every box of a multi-select. A group with a ticked pick
# leaves its other boxes unticked and not required, so the digest has no residual "ask" to
# block on (the live-page gate still needs one box checked). A group with no pick, and the
# assist lane, keep every box asked.


def test_the_other_boxes_of_the_palantir_language_group_are_skipped_not_required():
    options = ["English (ENG)", "Spanish (SPA)", "French (FRA)", "Choose not to disclose", "Other"]
    fields = [_field(o, "checkbox", required=True) for o in options]
    out = _resolve(fields)
    assert [r.action for r in out] == ["check", "check", "skip", "skip", "skip"]
    assert [r.field.required for r in out] == [True, True, False, False, False]
    assert {r.reason for r in out[2:]} == {"left unticked: another box of this required group is ticked"}
    assert all(r.value is None and r.semantic_key is None for r in out[2:])
    assert [r.field.label for r in out] == options


def test_the_other_boxes_of_the_method_location_group_are_skipped_not_required():
    labels = [
        "Currently located in one of the listed cities on the job description",
        "Open to relocating to a city on the job description",
        "Only considering remote / won't consider relocation",
    ]
    fields = [_field(label, "checkbox", required=True) for label in labels]
    out = _resolve(fields)
    assert [r.action for r in out] == ["skip", "check", "skip"]
    assert [r.field.required for r in out] == [False, True, False]


def test_a_required_group_with_no_pick_keeps_every_box_asked_and_required():
    fields = _office_group("Which office will you work from?", ["London", "Paris", "Singapore"])
    out = _resolve(fields)
    assert [r.action for r in out] == ["ask"] * 3
    assert [r.field.required for r in out] == [True] * 3


def test_the_unticked_boxes_of_a_group_with_a_pick_stay_asked_in_the_assist_lane():
    options = ["English (ENG)", "Spanish (SPA)", "French (FRA)", "Other"]
    fields = [_field(o, "checkbox", required=True) for o in options]
    out = _resolve(fields, autoapply=False)
    assert [r.action for r in out] == ["ask"] * 4
    assert [r.field.required for r in out] == [True] * 4


def test_a_group_with_a_pick_does_not_change_the_boxes_of_another_group():
    fields = [
        *[_field(o, "checkbox", required=True) for o in ["English (ENG)", "Spanish (SPA)", "French (FRA)"]],
        _field("Tell us more", "text", required=False),
        *[_field(o, "checkbox", required=True) for o in ["London", "Paris"]],
    ]
    out = _resolve(fields)
    assert [r.action for r in out[:3]] == ["check", "check", "skip"]
    assert [(r.action, r.field.required) for r in out[4:]] == [("ask", True)] * 2
