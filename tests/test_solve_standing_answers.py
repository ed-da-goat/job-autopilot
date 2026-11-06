"""The standing answers ([standing_answers] in the search preferences), as the unattended lane gives them.

These tests run on the template persona (Alex Rivera): pronouns are he/him, no prior US government employment, a $85,000 salary and a $15,000
bonus, four required consents answered Yes (a background check, a recorded interview,
an arbitration agreement, the applicant privacy notice), and the one office of a city
checkbox group that ranks highest in New York, Atlanta metro, Chicago, San Francisco.
The user's facts file is the source (tests/test_standing_answers_drift.py checks the
config against it). Every rule has its negatives: the nearby question it must not
answer. The assist lane keeps asking all of it. Pure: no browser, no database, no model.
"""

from __future__ import annotations

import pytest

from jobhunt.apply_assist import solve
from jobhunt.apply_assist.keys import CHECKBOX_TITLE_SEP, classify
from jobhunt.apply_assist.mapper import fingerprint
from jobhunt.apply_assist.schema import FieldSpec
from jobhunt.eligibility import user_preferences

RESUME = "/tmp/resume.pdf"
YES_NO = ["Yes", "No"]

ANSWERS = {
    "first_name": "Sam",
    "last_name": "Rivera",
    "full_name": "Sam Rivera",
    "email": "sam@example.com",
    "location": "Alpharetta, GA",
    "address_city": "Alpharetta",
    "address_state": "GA",
    "salary_expectation": "$120,000 - $150,000",  # the posting's range, merged in
    "salary_midpoint": "135000",
}


def _field(label, kind="text", options=None, required=True, **kw) -> FieldSpec:
    return FieldSpec(
        selector="#f", kind=kind, label=label, required=required,
        options=list(options or []), **kw,
    )  # fmt: skip


def _one(field, answers=None, *, autoapply=True, overrides=None):
    (resolution,) = solve.resolve(
        [field],
        ANSWERS if answers is None else answers,
        RESUME,
        overrides,
        None,
        autoapply=autoapply,
    )
    return resolution


def _outcome(res):
    return res.action, res.value


# --- pronouns --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["select", "radio"])
@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (["She/Her", "He/Him", "They/Them", "Prefer not to say"], "He/Him"),
        (["He/Him/His", "She/Her/Hers", "They/Them/Theirs", "Other"], "He/Him/His"),
        (["he/him", "she/her", "they/them"], "he/him"),
        (["He", "She", "They"], "He"),
        (["She/Her", "He / Him", "They/Them"], "He / Him"),
        (["He/Him (he/him/his)", "She/Her (she/her/hers)"], "He/Him (he/him/his)"),
    ],
)
def test_a_required_pronouns_list_takes_the_he_him_option(kind, options, expected):
    res = _one(_field("Pronouns", kind, options))
    assert (res.semantic_key, res.action, res.value) == ("pronouns", "fill", expected)


@pytest.mark.parametrize(
    "label", ["Pronouns", "What are your pronouns?", "Preferred pronouns", "Your Pronouns *"]
)
def test_any_required_pronoun_question_is_answered(label):
    res = _one(_field(label, "select", ["She/Her", "He/Him", "They/Them"]))
    assert (res.semantic_key, _outcome(res)) == ("pronouns", ("fill", "He/Him"))


def test_a_pronouns_text_box_is_typed_he_him_his_or_matched_to_its_menu():
    res = _one(_field("What are your pronouns?"))
    assert (res.semantic_key, _outcome(res)) == ("pronouns", ("fill", "He/Him/His"))
    assert res.option_rule == "pronouns"  # a react-select's open menu is read with it


@pytest.mark.parametrize(
    "options",
    [
        ["She/Her", "They/Them", "Prefer not to say"],  # no he/him at all
        ["He/They", "She/They", "They/Them"],  # He/They is not he/him
        ["He/Him/They", "She/Her/They"],
        ["He/Him", "He/Him/His", "She/Her"],  # two options that both say it
        ["Hers", "Theirs"],
    ],
)
def test_a_pronouns_list_with_no_single_he_him_option_is_asked(options):
    res = _one(_field("Pronouns", "select", options))
    assert (res.action, res.value) == ("ask", None)


def test_he_him_is_the_only_gender_adjacent_answer_and_gender_is_still_declined():
    options = ["Male", "Female", "Non-binary", "Decline to self-identify"]
    res = _one(_field("Gender", "select", options))
    assert (res.semantic_key, _outcome(res)) == ("gender", ("fill", "Decline to self-identify"))


def test_one_he_him_box_of_a_pronouns_checkbox_group_is_checked_and_the_rest_asked():
    title = "Pronouns"
    group = [
        _field(f"{option}{CHECKBOX_TITLE_SEP}{title}", "checkbox")
        for option in ("She/Her", "He/Him", "They/Them")
    ]
    resolutions = solve.resolve(group, ANSWERS, RESUME, autoapply=True)
    assert [r.action for r in resolutions] == ["ask", "check", "ask"]
    assert resolutions[1].semantic_key == "pronouns"


def test_an_optional_pronouns_question_is_not_answered():
    res = _one(_field("Pronouns", "select", ["She/Her", "He/Him"], required=False))
    assert (res.action, res.value) == ("ask", None)


def test_the_assist_lane_still_refuses_pronouns():
    for field in (
        _field("Pronouns", "select", ["She/Her", "He/Him", "They/Them"]),
        _field("Pronouns", "radio", ["He/Him/His", "She/Her/Hers"]),
        _field("What are your pronouns?"),
        _field(f"He/Him{CHECKBOX_TITLE_SEP}Pronouns", "checkbox"),
    ):
        res = _one(field, autoapply=False)
        assert (res.action, res.value) == ("ask", None), field.label
        assert res.reason == "sensitive — answer by hand" or res.reason.startswith("checkbox")


def test_a_mapper_pick_never_answers_pronouns():
    field = _field("Pick one", "select", ["She/Her", "He/Him"])
    res = _one(field, overrides={fingerprint(field): "pronouns"})
    assert res.action == "ask"
    assert res.value is None


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (["He/Him/His", "She/Her/Hers"], "He/Him/His"),
        (["he / him", "she / her"], "he / him"),
        (["He", "She"], "He"),
        (["He/Him/His pronouns", "She/Her/Hers pronouns"], "He/Him/His pronouns"),
        (["He, him", "She, her"], "He, him"),
        (["He/They", "They/Them"], None),
        (["Her", "Them"], None),
        (["Hello", "She"], None),
    ],
)
def test_pick_option_pronouns(options, expected):
    assert solve.pick_option("pronouns", [], options) == expected


# --- salary and bonus ------------------------------------------------------------------


def test_a_number_salary_box_gets_85000_not_the_posting_midpoint():
    res = _one(_field("Expected Salary", "number"))
    assert (res.semantic_key, _outcome(res)) == ("salary_expectation", ("fill", "85000"))


@pytest.mark.parametrize(
    "label",
    [
        "What is your desired salary? (numbers only)",
        "Expected Salary - digits only",
        "Salary desired: (enter a single number, no commas)",
    ],
)
def test_a_text_salary_box_that_asks_for_digits_gets_85000(label):
    assert _outcome(_one(_field(label))) == ("fill", "85000")


def test_a_text_salary_box_gets_the_answers_file_text_not_the_posting_range():
    res = _one(_field("What are your salary expectations?"))
    assert _outcome(res) == ("fill", "$85,000 base plus $15,000 bonus")


def test_the_total_compensation_text_box_is_the_salary_text():
    res = _one(_field("Total compensation expectations (base + bonus)"))
    assert (res.semantic_key, res.value) == (
        "salary_expectation", "$85,000 base plus $15,000 bonus"
    )


@pytest.mark.parametrize("kind", ["select", "radio"])
@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (["Under $70,000", "$70,000 - $90,000", "$90,000+"], "$70,000 - $90,000"),
        (["Under $100K", "$100K-$140K", "$140K+"], "Under $100K"),
        (["$60,000 - $79,999", "$80,000 - $99,999", "$100,000+"], "$80,000 - $99,999"),
    ],
)
def test_a_salary_list_takes_the_one_bracket_holding_85000(kind, options, expected):
    res = _one(_field("Salary expectation", kind, options))
    assert _outcome(res) == ("fill", expected)


@pytest.mark.parametrize(
    "options",
    [
        ["$90,000 - $110,000", "$110,000+"],  # no bracket holds 85,000
        ["$70k-$85k", "$85k-$100k"],  # two do: the edge is not guessed
        ["Yes", "No"],  # no amounts at all
    ],
)
def test_a_salary_list_with_no_single_bracket_for_85000_is_asked(options):
    res = _one(_field("Salary expectation", "select", options))
    assert (res.action, res.value) == ("ask", None)


def test_an_optional_salary_or_bonus_box_is_still_left_blank():
    for label in ("What are your salary expectations?", "Desired bonus"):
        res = _one(_field(label, required=False))
        assert res.action == "skip", label


def test_the_salary_confirmation_question_is_not_the_standing_salary():
    label = "The salary for this role is $97,000. Please confirm that this aligns with your desired salary"
    res = _one(_field(label, "select", ["Yes", "No"]))
    assert (res.semantic_key, _outcome(res)) == ("salary_expectation", ("fill", "Yes"))


@pytest.mark.parametrize(
    "label",
    [
        "Desired bonus",
        "Target annual bonus",
        "Bonus expectations",
        "What are your bonus expectations?",
        "Expected bonus",
        "Desired bonus compensation",
    ],
)
def test_a_desired_bonus_label_is_bonus_expectation(label):
    assert classify(_field(label)) == "bonus_expectation"


def test_a_number_bonus_box_gets_15000_and_a_text_box_15000_dollars():
    res = _one(_field("Desired bonus", "number"))
    assert (res.semantic_key, _outcome(res)) == ("bonus_expectation", ("fill", "15000"))
    res = _one(_field("What are your bonus expectations? (numbers only)"))
    assert _outcome(res) == ("fill", "15000")
    res = _one(_field("Desired bonus"))
    assert _outcome(res) == ("fill", "$15,000")


def test_a_bonus_list_is_asked():
    res = _one(_field("Desired bonus", "select", ["$0 - $10,000", "$10,000 - $20,000"]))
    assert (res.semantic_key, res.action, res.value) == ("bonus_expectation", "ask", None)


@pytest.mark.parametrize(
    "label",
    [
        "Sign-on bonus",
        "Desired signing bonus",
        "Sign on bonus expectations",
        "Relocation bonus",
        "Referral bonus",
        "Retention bonus",
        "What was your bonus last year?",
        "Current bonus",
        "Previous annual bonus",
    ],
)
def test_a_one_time_or_past_bonus_is_not_bonus_expectation(label):
    assert classify(_field(label)) not in ("bonus_expectation", "salary_expectation")
    res = _one(_field(label))
    assert res.action == "ask"
    assert res.value is None


@pytest.mark.parametrize(
    "label",
    ["Do you have deferred compensation?", "Deferred compensation", "Deferred compensation plan balance"],
)
def test_deferred_compensation_is_not_salary(label):
    assert classify(_field(label)) != "salary_expectation"
    for kind in ("text", "number"):
        res = _one(_field(label, kind))
        assert (res.action, res.value) == ("ask", None)


@pytest.mark.parametrize(
    "label",
    [
        "Desired hourly rate / salary expectation",
        "Monthly salary expectation",
        "Salary expectation (per hour)",
        "Salary expectation (USD/hr)",
        "Desired bonus (%)",
        "Target bonus as a percent of base",
    ],
)
def test_pay_asked_in_another_unit_is_asked(label):
    for kind in ("text", "number"):
        res = _one(_field(label, kind))
        assert (res.action, res.value) == ("ask", None), (label, kind)


def test_the_assist_lane_keeps_its_stored_salary_and_asks_the_bonus():
    res = _one(_field("What are your salary expectations?"), autoapply=False)
    assert _outcome(res) == ("fill", "$120,000 - $150,000")
    res = _one(_field("Desired bonus", "number"), autoapply=False)
    assert (res.action, res.value) == ("ask", None)


def test_the_standing_pay_constants():
    # The numbers the tests above assume are the template's example persona.
    standing = user_preferences().standing_answers
    assert (standing.salary, standing.bonus) == (85_000, 15_000)
    assert standing.salary_text == "$85,000 base plus $15,000 bonus"
    assert standing.bonus_text == "$15,000"


# --- prior US government employment ----------------------------------------------------

GOVERNMENT_LABELS = [
    "Prior US Government Employment?",
    "Are you a current or former U.S. government employee?",
    "Have you ever been employed by the US government?",
    "Have you been employed by the federal government in the past?",
    "Are you a current or former US federal employee?",
    "Have you previously worked for the United States government?",
]


@pytest.mark.parametrize("label", GOVERNMENT_LABELS)
@pytest.mark.parametrize("kind", ["select", "radio"])
def test_prior_us_government_employment_is_answered_no(label, kind):
    res = _one(_field(label, kind, YES_NO))
    assert (res.semantic_key, _outcome(res)) == ("government_employment", ("fill", "No"))


def test_government_employment_picks_the_no_option_that_spells_it_out():
    options = ["Yes, I am a current employee", "No, I have never been employed by the US government"]
    res = _one(_field(GOVERNMENT_LABELS[0], "radio", options))
    assert res.value == options[1]


def test_a_government_employment_text_box_is_typed_no_or_matched_to_its_menu():
    res = _one(_field(GOVERNMENT_LABELS[2]))
    assert (res.semantic_key, _outcome(res)) == ("government_employment", ("fill", "No"))


def test_a_government_list_with_no_no_option_is_asked():
    res = _one(_field(GOVERNMENT_LABELS[0], "select", ["Yes, current", "Yes, former"]))
    assert (res.action, res.value) == ("ask", None)


@pytest.mark.parametrize(
    "label",
    [
        "Do you hold an active US security clearance?",
        "Do you have a US government security clearance?",
        "Do you have a relative who works for the US government?",
        "Do you have any family members employed by the U.S. government?",
        "Are you a member of the US federal government's lobbying registry?",
        "Have you ever worked on a US government contract?",
        "Will you require visa sponsorship from the US government?",
        "Is your spouse or partner a US government official?",
    ],
)
def test_a_clearance_a_relative_or_a_contract_is_not_government_employment(label):
    assert classify(_field(label)) != "government_employment"
    res = _one(_field(label, "select", YES_NO))
    assert res.semantic_key != "government_employment"
    assert (res.action, res.value) == ("ask", None)


def test_a_stored_government_answer_beats_the_rule():
    res = _one(_field(GOVERNMENT_LABELS[0], "select", YES_NO), {**ANSWERS, "government_employment": "Yes"})
    assert _outcome(res) == ("fill", "Yes")


def test_the_assist_lane_asks_about_government_employment():
    res = _one(_field(GOVERNMENT_LABELS[0], "select", YES_NO), autoapply=False)
    assert (res.semantic_key, res.action, res.value) == ("government_employment", "ask", None)


# --- the four named consents -----------------------------------------------------------

CONSENTS = [
    ("Do you consent to a background check?", YES_NO, "Yes"),
    ("I consent to a background check being conducted", ["I consent", "I do not consent"], "I consent"),
    ("Please authorize a background screening", ["Yes, I authorize", "No"], "Yes, I authorize"),
    ("I consent to this interview being recorded", YES_NO, "Yes"),
    ("Do you consent to the recording of your interview?", ["I agree", "I do not agree"], "I agree"),
    ("Do you agree to have the call recorded?", YES_NO, "Yes"),
    ("I acknowledge that my video interview session will be recorded", ["I acknowledge", "I do not acknowledge"], "I acknowledge"),
    ("Do you agree to the Arbitration Agreement?", YES_NO, "Yes"),
    ("Arbitration Agreement", ["I agree", "I do not agree"], "I agree"),
    ("Applicant Privacy Notice", ["I acknowledge", "I do not acknowledge"], "I acknowledge"),
    ("Candidate Privacy Policy", ["Yes, I understand", "No"], "Yes, I understand"),
    ("Privacy Notice", ["I consent", "I do not consent"], "I consent"),
    ("Privacy*", ["I agree", "I do not agree"], "I agree"),
]


@pytest.mark.parametrize("kind", ["select", "radio"])
@pytest.mark.parametrize(("label", "options", "expected"), CONSENTS)
def test_a_required_named_consent_list_takes_the_assenting_option(label, options, expected, kind):
    res = _one(_field(label, kind, options))
    assert (res.semantic_key, _outcome(res)) == ("consent", ("fill", expected))


def test_interview_recording_is_covered_in_several_wordings():
    for label in (
        "I consent to the recording of my interview",
        "Do you consent to your interview being recorded?",
        "By selecting Yes, you consent to this call being recorded",
        "I agree that my interviews may be recorded",
        "Do you consent to recording of the conversation?",
    ):
        res = _one(_field(label, "radio", YES_NO))
        assert (res.semantic_key, _outcome(res)) == ("consent", ("fill", "Yes")), label


@pytest.mark.parametrize(
    "label",
    [
        "I consent to my video interview being recorded",
        "Do you agree to have your interview recorded on video?",
    ],
)
@pytest.mark.parametrize("kind", ["text", "radio"])
def test_a_video_interview_recording_consent_is_a_consent_not_the_camera_answer(label, kind):
    res = _one(_field(label, kind, YES_NO))
    assert (res.semantic_key, res.action) == ("consent", "fill")


def test_a_named_consent_in_a_menu_with_hidden_options_is_applied_by_rule():
    res = _one(_field("Do you consent to a background check?"))
    assert (res.semantic_key, res.action, res.option_rule) == ("consent", "fill", "acknowledge")


@pytest.mark.parametrize("kind", ["select", "radio"])
@pytest.mark.parametrize(
    "label",
    [
        "Would you like to be considered for future opportunities?",
        "I consent to being contacted about future opportunities",
        "Do you consent to us sharing your profile with our portfolio companies?",
        "Join our talent community",
        "Do you consent to the use of AI in our hiring process?",
        "Do you use AI notetakers in your interviews?",
        "Do you use AI tools to take notes in meetings?",
        "How often do you use an AI notetaker?",
        "Have you ever failed a background check?",
        "Are you willing to undergo a background check?",
        "I consent to a background check and to being contacted about future roles",
        "I do not consent to a background check",
    ],
)
def test_a_question_that_is_not_one_of_the_four_consents_is_asked(label, kind):
    res = _one(_field(label, kind, YES_NO))
    assert (res.action, res.value) == ("ask", None)
    assert res.semantic_key != "consent"


def test_a_marketing_opt_in_stays_the_existing_no_never_a_consent_yes():
    res = _one(_field("I consent to receive marketing communications", "select", YES_NO))
    assert (res.semantic_key, _outcome(res)) == ("sms_optin", ("fill", "No"))


def test_a_consent_list_with_two_assenting_options_or_none_is_asked():
    label = "Do you consent to a background check?"
    res = _one(_field(label, "select", ["Yes", "Yes, and notify me", "No"]))
    assert (res.action, res.value) == ("ask", None)
    res = _one(_field(label, "select", ["No", "Maybe later"]))
    assert (res.action, res.value) == ("ask", None)


def test_an_optional_consent_a_textarea_and_the_assist_lane_are_asked():
    label = "Do you consent to a background check?"
    assert _one(_field(label, "select", YES_NO, required=False)).action == "ask"
    assert _one(_field(label, "textarea")).action == "ask"
    res = _one(_field(label, "select", YES_NO), autoapply=False)
    assert (res.action, res.value) == ("ask", None)


# --- the office-city order -------------------------------------------------------------

ATLANTA = {**ANSWERS, "location": "Atlanta, Georgia", "address_city": "Atlanta"}


def _group(options, title="Office Location", *, required=True, titled=True):
    return [
        _field(f"{option}{CHECKBOX_TITLE_SEP}{title}" if titled else option, "checkbox",
               required=required)
        for option in options
    ]  # fmt: skip


def _checked(options, answers=None, **kw):
    fields = _group(options, **kw)
    resolutions = solve.resolve(fields, answers or ATLANTA, RESUME, autoapply=True)
    return [
        f.label.split(CHECKBOX_TITLE_SEP)[0]
        for f, r in zip(fields, resolutions)
        if r.action == "check"
    ]


def test_the_office_city_order_constant():
    assert solve.OFFICE_CITY_ORDER == ("New York", "Atlanta metro", "Chicago", "San Francisco")


@pytest.mark.parametrize("new_york", ["New York, NY", "New York City", "NYC", "New York, New York"])
def test_new_york_beats_every_other_city(new_york):
    options = ["San Francisco, CA", "Chicago, IL", "Atlanta, GA", new_york]
    assert _checked(options) == [new_york]


def test_atlanta_beats_chicago_and_san_francisco():
    assert _checked(["San Francisco, CA", "Chicago, IL", "Atlanta, GA"]) == ["Atlanta, GA"]
    assert _checked(["Alpharetta, GA", "San Francisco, CA", "Boston, MA"]) == ["Alpharetta, GA"]


def test_chicago_beats_san_francisco():
    assert _checked(["San Francisco, CA", "Chicago, IL", "Denver, CO"]) == ["Chicago, IL"]


@pytest.mark.parametrize("sf", ["San Francisco, CA", "San Francisco", "SF"])
def test_san_francisco_is_ticked_when_it_is_the_only_city_of_his_order(sf):
    assert _checked([sf, "Denver, CO", "Austin, TX"]) == [sf]


def test_the_untitled_run_of_cities_follows_the_same_order():
    assert _checked(["Boston, MA", "San Francisco, CA", "New York, NY"], titled=False) == ["New York, NY"]


def test_two_boxes_of_the_best_city_are_asked_not_guessed():
    assert _checked(["New York, NY", "New York City", "Chicago, IL"]) == []
    assert _checked(["Atlanta, GA", "Alpharetta, GA", "San Francisco, CA"]) == []


@pytest.mark.parametrize("option", ["Remote - New York", "New York (Hybrid)", "Tri-State Area", "Albany, NY"])
def test_a_place_only_near_new_york_is_not_new_york(option):
    assert _checked([option, "Chicago, IL", "Denver, CO"]) == ["Chicago, IL"]


def test_a_city_in_another_state_is_not_his_city():
    assert _checked(["Chicago, TX", "Denver, CO"]) == []
    assert _checked(["New York, TX", "Denver, CO"]) == []


def test_atlanta_ranks_only_for_a_georgia_home():
    elsewhere = {**ANSWERS, "location": "Denver, Colorado", "address_state": "CO", "address_city": "Denver"}
    assert _checked(["Atlanta, GA", "San Francisco, CA"], elsewhere) == ["San Francisco, CA"]
    assert _checked(["Atlanta, GA", "New York, NY"], elsewhere) == ["New York, NY"]


def test_the_office_group_is_still_ticked_only_when_required_and_in_the_auto_lane():
    assert _checked(["New York, NY", "Denver, CO"], required=False) == []
    fields = _group(["New York, NY", "Denver, CO"])
    resolutions = solve.resolve(fields, ATLANTA, RESUME, autoapply=False)
    assert [r.action for r in resolutions] == ["ask", "ask"]


def test_a_group_title_that_is_not_about_offices_is_asked():
    assert _checked(["New York, NY", "Denver, CO"], title="Which of these interest you?") == []
    assert _checked(["New York, NY", "Denver, CO"], title="Offices to exclude") == []


# --- the AI notetaker notice (the recorded-interview consent, extended) ----------------

NOTETAKER = (
    "As part of our interview process, we may use AI notetakers to transcribe conversations "
    "for accuracy and efficiency. Please confirm that you consent to this."
)


@pytest.mark.parametrize("kind", ["select", "radio"])
@pytest.mark.parametrize(
    ("label", "options", "expected"),
    [
        (NOTETAKER, YES_NO, "Yes"),
        (NOTETAKER, ["I consent", "I do not consent"], "I consent"),
        ("Do you consent to an AI notetaker recording your interview?", YES_NO, "Yes"),
        ("We may use an AI transcription tool during your calls. Do you agree?", YES_NO, "Yes"),
        ("I acknowledge that an AI notetaker may join my interview", ["I acknowledge", "No"], "I acknowledge"),
    ],
)
def test_a_required_ai_notetaker_notice_takes_the_assenting_option(label, options, expected, kind):
    res = _one(_field(label, kind, options))
    assert (res.semantic_key, _outcome(res)) == ("consent", ("fill", expected))


def test_an_ai_notetaker_notice_in_a_menu_with_hidden_options_is_applied_by_rule():
    res = _one(_field(NOTETAKER))
    assert (res.semantic_key, res.action, res.option_rule) == ("consent", "fill", "acknowledge")


@pytest.mark.parametrize("kind", ["select", "radio"])
@pytest.mark.parametrize(
    "label",
    [
        "Do you use AI notetakers when you interview candidates?",
        "Have you used an AI notetaker in a meeting?",
        "Describe your experience with AI notetakers",
        "I do not consent to an AI notetaker recording my interview",
        "We may use AI notetakers. Do you opt out?",
        "We may use AI notetakers and share your profile with our portfolio companies. Do you consent?",
        "Do you consent to the use of AI in our hiring process?",
        "We use AI to screen resumes. Do you consent?",
    ],
)
def test_other_ai_questions_are_still_asked(label, kind):
    res = _one(_field(label, kind, YES_NO))
    assert (res.action, res.value) == ("ask", None)


def test_an_ai_notetaker_notice_is_asked_when_optional_a_textarea_or_in_the_assist_lane():
    assert _one(_field(NOTETAKER, "select", YES_NO, required=False)).action == "ask"
    assert _one(_field(NOTETAKER, "textarea")).action == "ask"
    assert _one(_field(NOTETAKER, "select", YES_NO), autoapply=False).action == "ask"


# --- Method Financial's location checkbox group ----------------------------------------

LISTED = "Currently located in one of the listed cities on the job description"
RELOCATE = "Open to relocating to a city on the job description"
REMOTE_ONLY = "Only considering remote / won't consider relocation"


def _method(labels, *, required=True, autoapply=True):
    fields = [_field(label, "checkbox", required=required) for label in labels]
    resolutions = solve.resolve(fields, ANSWERS, RESUME, autoapply=autoapply)
    return [r.action for r in resolutions], resolutions


@pytest.mark.parametrize(
    "labels",
    [
        [LISTED, RELOCATE, REMOTE_ONLY],
        # The title already inside each option (so the scan adds no separator).
        [f"Location {o}" for o in (LISTED, RELOCATE, REMOTE_ONLY)],
        # Or split off by the separator.
        [f"{o}{CHECKBOX_TITLE_SEP}Location" for o in (LISTED, RELOCATE, REMOTE_ONLY)],
    ],
)
def test_only_the_relocation_box_of_the_location_group_is_ticked(labels):
    actions, resolutions = _method(labels)
    # The two unticked siblings are skipped, not asked (2026-10-27).
    assert actions == ["skip", "check", "skip"]
    assert resolutions[1].semantic_key == "willing_to_relocate"


def test_the_location_group_is_ticked_only_when_required_and_in_the_auto_lane():
    assert _method([LISTED, RELOCATE, REMOTE_ONLY], required=False)[0] == ["ask"] * 3
    assert _method([LISTED, RELOCATE, REMOTE_ONLY], autoapply=False)[0] == ["ask"] * 3


@pytest.mark.parametrize(
    "labels",
    [
        [RELOCATE],  # a lone box is no group
        [RELOCATE, REMOTE_ONLY],  # no "currently located" option: not the Method group
        [LISTED, REMOTE_ONLY],
        [LISTED, "Not open to relocating to a city on the job description"],
        [LISTED, RELOCATE, f"{RELOCATE} (Remote)"],
    ],
)
def test_a_group_without_the_two_signature_options_is_never_ticked(labels):
    actions, _ = _method(labels)
    assert "check" not in actions


def test_the_relocation_box_among_other_checkbox_groups_stays_unchecked_elsewhere():
    fields = [
        _field("Spanish", "checkbox"),
        _field("French", "checkbox"),
        _field("Open to relocating?", "text"),
        _field(RELOCATE, "checkbox"),
        _field(LISTED, "checkbox"),
    ]
    res = solve.resolve(fields, ANSWERS, RESUME, autoapply=True)
    assert [r.action for r in res] == ["ask", "ask", "ask", "check", "skip"]
