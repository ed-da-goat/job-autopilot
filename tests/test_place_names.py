"""Place names in pick-from-list matching: the location and country keys only.

A stored "Atlanta, Georgia" is listed as "Atlanta, GA, USA" on some forms, and a
stored "United States" as "United States of America". For the two place keys
(solve.PLACE_KEYS) an option may therefore be the answer when it spells the same
place: equal after a small, fixed rewrite (solve.same_place). Never a substring,
never a fuzzy match. The rewrite only ever ADDS a way to match; where it cannot
read a string exactly it matches nothing, and where two options spell the
answer's place it picks neither.

Run 3 evidence: the stored "Atlanta, Georgia" found no option in a "Location
(City)" list, and the stored "United States" found none in a "Country of
Residence" list.

No browser here. tests/test_fill_browser.py drives the react-select half through
real Chrome.
"""

from __future__ import annotations

import pytest

from jobhunt.apply_assist import fill, solve
from jobhunt.apply_assist.schema import FieldSpec, Resolution
from jobhunt.apply_assist.solve import _match_option, resolve, same_place
from tests.test_fill_dial_code import _Box


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(fill, "_pause", lambda seconds: None)


# --- which keys --------------------------------------------------------------------


def test_only_the_location_and_country_keys_are_place_keys():
    assert solve.PLACE_KEYS == frozenset({"location", "country"})


# --- same_place: the canonicalizer ------------------------------------------------------

# (answer, option): the same place.
_SAME = [
    # the United States, whole strings, in every spelling the brief names
    pytest.param("United States", "United States", id="us-same-text"),
    pytest.param("United States", "United States of America", id="us-of-america"),
    pytest.param("United States", "USA", id="us-usa"),
    pytest.param("United States", "U.S.", id="us-dotted"),
    pytest.param("United States", "U.S.A.", id="us-dotted-three"),
    pytest.param("United States", "US", id="us-two-letters"),
    pytest.param("USA", "United States", id="usa-answer"),
    pytest.param("U.S.A.", "United States of America", id="dotted-answer"),
    pytest.param("US", "U.S.", id="two-spellings-of-the-code"),
    pytest.param("United  States  of   America", "UNITED STATES", id="case-and-whitespace"),
    # a US city: the state spelled out or as its postal code, with or without a country
    pytest.param("Atlanta, Georgia", "Atlanta, GA", id="state-as-postal-code"),
    pytest.param("Atlanta, Georgia", "Atlanta, GA, USA", id="postal-code-and-usa"),
    pytest.param("Atlanta, Georgia", "Atlanta, Georgia, United States", id="country-spelled-out"),
    pytest.param(
        "Atlanta, Georgia", "Atlanta, GA, United States of America", id="country-in-full"
    ),
    pytest.param("Atlanta, GA", "Atlanta, Georgia", id="answer-with-the-postal-code"),
    pytest.param("Atlanta, GA", "Atlanta, Georgia, USA", id="postal-code-answer-and-usa"),
    pytest.param("Atlanta,Georgia", "atlanta ,  ga", id="commas-case-and-whitespace"),
    pytest.param("  Atlanta,   Georgia ", "ATLANTA, GA, USA", id="padding"),
    pytest.param("New York, New York", "New York, NY, USA", id="two-word-state-name"),
    pytest.param("Washington, DC", "Washington, District of Columbia, USA", id="dc-in-full"),
    pytest.param("New York, NY", "New York City, New York, United States", id="ashby-nyc"),
    pytest.param("New York City, NY", "New York, New York, United States", id="nyc-answer"),
    pytest.param("Paris, Texas", "Paris, Texas, USA", id="a-state-before-the-country"),
    pytest.param("Chapel Hill, North Carolina", "Chapel Hill, NC", id="two-word-state"),
    # both sides spell the country: it has to be the same one
    pytest.param("Atlanta, Georgia, USA", "Atlanta, GA, United States", id="both-name-a-country"),
    pytest.param("Atlanta, GA, USA", "Atlanta, Georgia, USA", id="both-usa"),
]

# (answer, option): NOT the same place. These are the cases a looser rule would get wrong.
_DIFFERENT = [
    # the country list
    pytest.param(
        "United States", "United States Minor Outlying Islands", id="minor-outlying-islands"
    ),
    pytest.param("United States", "Georgia", id="the-country-georgia"),
    pytest.param("Georgia", "United States", id="the-country-georgia-as-the-answer"),
    pytest.param("United States", "United Kingdom", id="another-united-country"),
    pytest.param("United States", "US Virgin Islands", id="virgin-islands"),
    pytest.param("United States", "United States of Mexico", id="not-a-us-name"),
    pytest.param("United States", "The United States", id="a-prefix"),
    pytest.param("United States", "Georgia, USA", id="the-state-is-not-the-country"),
    pytest.param("Canada", "USA", id="another-country"),
    # a city
    pytest.param("Atlanta, Georgia", "Athens, GA", id="another-city-same-state"),
    pytest.param("Atlanta, Georgia", "Athens, GA, USA", id="another-city-with-usa"),
    pytest.param("Atlanta, Texas", "Atlanta, Georgia", id="same-city-another-state"),
    pytest.param("Atlanta, Georgia", "Atlanta, Texas", id="same-city-another-state-reversed"),
    pytest.param("Atlanta, Georgia", "Atlanta, TX, USA", id="another-state-as-a-postal-code"),
    pytest.param("New York, NY", "New York, United States", id="new-york-the-state"),
    pytest.param("New York, NY", "York, New York, United States", id="york-not-new-york"),
    pytest.param("New York, NY", "New York City, New Jersey, United States", id="nyc-wrong-state"),
    pytest.param("New York, NY", "New York Mills, New York, United States", id="another-ny-town"),
    pytest.param("Atlanta, Georgia", "Atlanta", id="state-missing-on-the-option"),
    pytest.param("Atlanta", "Atlanta, Georgia", id="state-missing-on-the-answer"),
    pytest.param("Atlanta, Georgia", "Atlanta, GA, Canada", id="another-country"),
    pytest.param("Atlanta, Georgia", "Atlanta, GA, UK", id="a-country-not-in-the-list"),
    pytest.param("Atlanta, Georgia", "Georgia, Atlanta", id="parts-in-another-order"),
    pytest.param("Atlanta, Georgia", "Atlanta, GA, USA, Earth", id="more-after-the-country"),
    # a country on the answer must be on the option too
    pytest.param("Atlanta, Georgia, USA", "Atlanta, GA", id="answer-names-a-country-option-not"),
    pytest.param("Atlanta, Georgia, USA", "Atlanta, GA, Canada", id="a-different-country"),
    # a country with no state before it is not read: "Washington" the city, the state, the person
    pytest.param("Atlanta, Fulton", "Atlanta, Fulton, USA", id="usa-after-a-non-state"),
    pytest.param("Paris, France", "Paris, France, USA", id="usa-after-another-country"),
    pytest.param("Atlanta", "Atlanta, USA", id="usa-after-a-bare-city"),
    pytest.param("Washington", "Washington, USA", id="usa-after-a-bare-washington"),
    pytest.param("Georgia", "Georgia, USA", id="usa-after-the-bare-country"),
    # a single word is never rewritten
    pytest.param("Georgia", "GA", id="state-name-alone-is-not-its-code"),
    pytest.param("Texas", "TX", id="state-alone"),
    pytest.param("Georgia", "Georgia, Georgia", id="not-twice"),
    # anything it cannot read exactly matches nothing
    pytest.param("", "", id="both-empty"),
    pytest.param("", "Atlanta, GA", id="empty-answer"),
    pytest.param("Atlanta, Georgia", "", id="empty-option"),
    pytest.param("Atlanta,, Georgia", "Atlanta, GA", id="empty-part-in-the-answer"),
    pytest.param("Atlanta, Georgia", "Atlanta,, GA", id="empty-part-in-the-option"),
    pytest.param("Atlanta, Georgia,", "Atlanta, GA", id="trailing-comma"),
    pytest.param(",", ",", id="only-commas"),
    pytest.param("   ", "   ", id="only-spaces"),
]


@pytest.mark.parametrize("answer,option", _SAME)
def test_same_place_for_spellings_of_one_place(answer, option):
    assert same_place(answer, option) is True


@pytest.mark.parametrize("answer,option", _DIFFERENT)
def test_same_place_is_false_for_everything_that_only_looks_close(answer, option):
    assert same_place(answer, option) is False


def test_same_place_takes_a_missing_answer_or_option_as_nothing():
    assert same_place(None, "USA") is False
    assert same_place("USA", None) is False
    assert same_place(None, None) is False


def test_a_country_on_the_option_is_allowed_when_the_answer_has_none_never_the_reverse():
    assert same_place("Atlanta, Georgia", "Atlanta, GA, USA") is True
    assert same_place("Atlanta, Georgia, USA", "Atlanta, GA") is False


def test_same_place_is_symmetric_where_neither_side_has_a_country():
    for answer, option in [
        ("Atlanta, Georgia", "Atlanta, GA"),
        ("New York, New York", "New York, NY"),
        ("United States", "USA"),
    ]:
        assert same_place(answer, option) is same_place(option, answer) is True


def test_every_us_state_is_the_postal_code_it_is_listed_with():
    # fifty states and the District of Columbia, each once, each code once
    assert len(solve._US_STATES) == 51
    assert len(solve._US_POSTAL_CODES) == 51
    for name, code in solve._US_STATES.items():
        assert code == code.lower() and len(code) == 2
        assert same_place(f"Atlanta, {name}", f"Atlanta, {code.upper()}, USA")
        assert not same_place(f"Atlanta, {name}", "Atlanta, XX, USA")


def test_the_country_trailers_are_the_three_spellings_the_brief_names():
    assert solve._US_TRAILERS == {"usa", "united states", "united states of america"}
    for trailer in ("U.S.", "U.S.A.", "US"):  # fine as a whole string, not after a state
        assert same_place("Atlanta, Georgia", f"Atlanta, GA, {trailer}") is False


# --- solve._match_option: the native-select path -----------------------------------------


def test_a_country_answer_finds_its_other_spelling_in_the_list():
    options = ["Canada", "USA", "Mexico"]
    assert _match_option("United States", options, "country") == "USA"
    assert _match_option("United States", options) is None  # no key: nothing new


def test_the_us_option_wins_over_the_longer_names_that_contain_it():
    options = ["United States of America", "United States Minor Outlying Islands"]
    assert _match_option("United States", options, "country") == "United States of America"
    assert _match_option("United States", options) is None  # two substring hits


def test_minor_outlying_islands_is_never_the_us():
    options = ["Georgia", "United States Minor Outlying Islands", "US"]
    assert _match_option("United States", options, "country") == "US"
    assert _match_option("United States", options, "country") != options[1]
    assert _match_option("United States", ["Georgia", "Canada"], "country") is None


def test_the_country_georgia_is_not_the_united_states():
    assert _match_option("United States", ["Georgia", "Canada", "Mexico"], "country") is None
    assert _match_option("Georgia", ["United States", "USA"], "country") is None


def test_two_options_that_spell_the_answer_match_nothing():
    assert _match_option("United States", ["USA", "U.S.", "Canada"], "country") is None
    assert (
        _match_option("Atlanta, Georgia", ["Atlanta, GA, USA", "Atlanta, Georgia, USA"], "location")
        is None
    )
    # ... not even by the fuzzy tier, which could otherwise tell them apart
    assert (
        _match_option("United States", ["USA", "United States of America"], "country") is None
    )


def test_an_exact_option_beats_a_spelling_of_the_same_place():
    assert _match_option("United States", ["USA", "United States", "U.S."], "country") == (
        "United States"
    )
    assert _match_option("Atlanta, Georgia", ["Atlanta, GA, USA", "Atlanta, Georgia"], "location") == (
        "Atlanta, Georgia"
    )


def test_a_location_answer_finds_its_other_spelling_in_the_list():
    options = ["Athens, GA, USA", "Atlanta, GA, USA", "Atlanta, TX, USA"]
    assert _match_option("Atlanta, Georgia", options, "location") == "Atlanta, GA, USA"


def test_a_location_list_without_the_place_matches_nothing():
    assert _match_option("Atlanta, Georgia", ["Athens, GA, USA", "Atlanta, TX, USA"], "location") is None
    assert _match_option("Atlanta, Georgia", ["Athens, GA"], "location") is None


def test_atlanta_georgia_is_not_the_answer_atlanta_texas():
    assert _match_option("Atlanta, Texas", ["Atlanta, Georgia"], "location") is None
    assert _match_option("Atlanta, Texas", ["Atlanta, Georgia", "Atlanta, GA, USA"], "location") is None
    assert _match_option("Atlanta, Texas", ["Atlanta, Georgia", "Atlanta, TX, USA"], "location") == (
        "Atlanta, TX, USA"
    )


def test_a_key_that_is_not_a_place_key_is_matched_as_before():
    options = ["Canada", "USA", "Mexico"]
    for key in ("school", "work_authorization", "degree", None):
        assert _match_option("United States", options, key) is None
    assert _match_option("Atlanta, Georgia", ["Atlanta, GA, USA"], "school") is None


def test_the_other_tiers_still_run_for_a_place_key():
    # exact, unique substring and a close typo all work as they always did
    assert _match_option("canada", ["Canada", "USA"], "country") == "Canada"
    assert _match_option("Atlanta", ["Atlanta, GA, USA", "Boston, MA, USA"], "location") == (
        "Atlanta, GA, USA"
    )
    assert _match_option("Untied States", ["Canada", "United States"], "country") == (
        "United States"
    )


# --- resolve(): a native select, from the label to the filled option -----------------------


def _select(label, options, name=""):
    return FieldSpec(selector="#sel", kind="select", label=label, name=name, options=options)


def test_resolve_fills_the_country_of_residence_list_with_the_us_option():
    # the WorldQuant shape: the stored "United States" had found no option
    field = _select("Country of Residence", ["Canada", "United States of America", "Mexico"])
    [res] = resolve([field], {"country": "United States"}, resume_path=None)
    assert (res.semantic_key, res.action, res.value) == (
        "country",
        "fill",
        "United States of America",
    )


def test_resolve_fills_the_location_city_list_with_the_other_spelling():
    # the Recidiviz shape: the stored "Atlanta, Georgia" had found no option
    field = _select("Location (City)", ["Atlanta, TX, USA", "Atlanta, GA, USA", "Boston, MA, USA"])
    [res] = resolve([field], {"location": "Atlanta, Georgia"}, resume_path=None)
    assert (res.semantic_key, res.action, res.value) == ("location", "fill", "Atlanta, GA, USA")


@pytest.mark.parametrize(
    "label,key,answer,options",
    [
        pytest.param(
            "Country",
            "country",
            "United States",
            ["USA", "U.S.", "Canada"],
            id="country-two-spellings",
        ),
        pytest.param(
            "Location (City)",
            "location",
            "Atlanta, Georgia",
            ["Atlanta, GA, USA", "Atlanta, Georgia, United States"],
            id="location-two-spellings",
        ),
        pytest.param(
            "Country", "country", "United States", ["Georgia", "Canada"], id="country-georgia-only"
        ),
        pytest.param(
            "Location (City)",
            "location",
            "Atlanta, Texas",
            ["Atlanta, Georgia", "Atlanta, GA, USA"],
            id="location-another-state",
        ),
    ],
)
def test_resolve_asks_where_the_list_does_not_hold_exactly_one_spelling(
    label, key, answer, options
):
    [res] = resolve([_select(label, options)], {key: answer}, resume_path=None)
    assert res.semantic_key == key
    assert res.action == "ask"
    assert res.reason == "stored value matches 0 or >1 options"
    assert res.value is None


def test_resolve_gives_a_list_that_is_not_a_place_the_old_matching():
    field = _select("Degree", ["Bachelor's", "Master's"])
    [res] = resolve([field], {"degree": "Bachelors"}, resume_path=None)
    assert res.action == "fill"  # the fuzzy tier, as ever
    assert res.value == "Bachelor's"


# --- resolve(): a residence question over a list of countries ---------------------------
#
# "Where are you currently located?*" asks for a country on some forms (WorldQuant's
# list: "Georgia" and "United States of America" among two hundred countries). The
# stored location, "Atlanta, Georgia", is listed nowhere there, so the stored country
# answers instead, but only for a question that asks where the applicant is
# (keys.asks_where_you_are), only when the list comes near no spelling of the stored
# location, and only as the one option that is the country (equal, or the same place).
# Anything else keeps asking, with the reason it always had.

_HOME = {"location": "Atlanta, Georgia", "country": "United States"}
_COUNTRY_LIST = [
    "Georgia",
    "United States Minor Outlying Islands",
    "United States of America",
    "Canada",
]
_ASKS = "stored value matches 0 or >1 options"


def _located(label, options, answers=None, kind="select"):
    field = FieldSpec(selector="#sel", kind=kind, label=label, options=list(options))
    [res] = resolve([field], dict(_HOME if answers is None else answers), resume_path=None)
    return res


@pytest.mark.parametrize(
    "label",
    [
        "Where are you currently located?*",
        "Where do you live?",
        "Where are you based?",
        "Location",
        "Current location ✱",
    ],
)
@pytest.mark.parametrize("kind", ["select", "radio"])
def test_a_residence_question_over_a_list_of_countries_takes_the_stored_country(label, kind):
    res = _located(label, _COUNTRY_LIST, kind=kind)
    assert (res.semantic_key, res.action, res.value) == (
        "location",
        "fill",
        "United States of America",
    )
    assert res.fallback_value is None  # the list is known: nothing is left to try later


@pytest.mark.parametrize(
    "label",
    [
        "Location (City)*",
    ],
)
def test_a_question_that_is_not_about_where_you_are_keeps_asking_over_a_list_of_countries(label):
    res = _located(label, _COUNTRY_LIST)
    assert res.semantic_key == "location"
    assert (res.action, res.reason, res.value) == ("ask", _ASKS, None)


# A question about the job's own place (keys._JOB_PLACE_RE) names no key: the stored
# city is where the applicant lives, so it is not a candidate and the list is never
# searched, and the stored country is not tried either. It asks as an unrecognized field.
@pytest.mark.parametrize(
    "label",
    [
        "Which location are you applying for?",
        "Preferred location",
        "Office location",
    ],
)
def test_a_question_about_the_jobs_place_names_no_key_over_a_list_of_countries(label):
    res = _located(label, _COUNTRY_LIST)
    assert (res.semantic_key, res.action, res.reason, res.value) == (
        None,
        "ask",
        "unrecognized field",
        None,
    )


@pytest.mark.parametrize(
    "answers",
    [
        pytest.param({"location": "Atlanta, Georgia"}, id="no-country-stored"),
        pytest.param({"location": "Atlanta, Georgia", "country": ""}, id="empty-country"),
        pytest.param({"location": "Atlanta, Georgia", "country": "   "}, id="blank-country"),
    ],
)
def test_without_a_stored_country_a_residence_question_keeps_asking(answers):
    res = _located("Where are you currently located?*", _COUNTRY_LIST, answers)
    assert (res.semantic_key, res.action, res.reason, res.value) == ("location", "ask", _ASKS, None)


@pytest.mark.parametrize(
    ("options", "filled"),
    [
        # The list names no spelling of the city: the country answers, if it is listed once.
        pytest.param(["Athens, GA", "Boston, MA", "Denver, CO"], None, id="cities-without-atlanta"),
        pytest.param(["Atlanta, GA", "United States"], "Atlanta, GA", id="the-city-is-listed"),
        pytest.param(["Canada", "USA"], "USA", id="the-country-spelled-another-way"),
        pytest.param(["canada", "united states"], "united states", id="the-country-in-lower-case"),
        # The list is made of places like the city: the city is what it wants. Keep asking.
        pytest.param(
            ["Atlanta, GA", "Atlanta, GA, USA", "United States"],
            None,
            id="two-spellings-of-the-city",
        ),
        pytest.param(
            ["Atlanta (Hybrid)", "United States", "Canada"], None, id="the-city-in-another-form"
        ),
        pytest.param(
            ["Atlanta Metro", "Atlanta Suburbs", "United States"], None, id="two-cities-named-atlanta"
        ),
        pytest.param(
            ["Greater Atlanta, GA area", "United States", "Canada"],
            None,
            id="the-city-inside-a-longer-name",
        ),
        # The country must be one option, equal or the same place; never more, never less.
        pytest.param(["United States", "United States of America"], None, id="two-countries"),
        pytest.param(["U.S.", "USA", "Canada"], None, id="two-more-spellings-of-the-country"),
        pytest.param(["United States - Remote", "Canada"], None, id="the-country-and-a-suffix"),
        pytest.param(["Remote - United States", "Canada"], None, id="a-prefix-and-the-country"),
        pytest.param(["Georgia", "Canada"], None, id="the-country-georgia"),
        pytest.param(
            ["Georgia", "United States Minor Outlying Islands"], None, id="georgia-and-the-islands"
        ),
        pytest.param(["Canada", "Mexico"], None, id="neither"),
        pytest.param([], None, id="no-options"),
    ],
)
def test_the_country_answers_only_where_the_list_comes_near_no_spelling_of_the_city(
    options, filled
):
    res = _located("Where are you currently located?*", options)
    if filled is None:
        assert (res.action, res.reason, res.value) == ("ask", _ASKS, None)
    else:
        assert (res.action, res.value) == ("fill", filled)


def test_the_country_answer_never_reaches_a_question_whose_key_is_not_location():
    # A Country field is the country key's, matched as it always was; a field of another
    # key is nobody's to answer with the stored country.
    country = _select("Country of Residence", ["Canada", "United States of America"])
    [res] = resolve([country], dict(_HOME), resume_path=None)
    assert (res.semantic_key, res.value) == ("country", "United States of America")
    school = _select("School", ["Canada", "United States of America"])
    [res] = resolve([school], {"school": "Example State", "country": "United States"}, None)
    assert res.action == "ask"


# --- resolve(): a text box that may be a react-select carries the country ---------------


def _typed(label, answers=None, kind="text"):
    field = FieldSpec(selector="#where", kind=kind, label=label)
    [res] = resolve([field], dict(_HOME if answers is None else answers), resume_path=None)
    return res


def test_a_residence_question_in_a_text_box_carries_the_country_for_fill():
    res = _typed("Where are you currently located?*")
    assert (res.semantic_key, res.action, res.value) == ("location", "fill", "Atlanta, Georgia")
    assert res.fallback_value == "United States"
    assert res.to_dict()["fallback_value"] == "United States"


@pytest.mark.parametrize(
    "label",
    [
        "Location (City)*",
        "Which location are you applying for?",
        "Preferred location",
        "Office location",
        "Full name",
    ],
)
def test_any_other_text_box_carries_no_country(label):
    res = _typed(label, {**_HOME, "full_name": "Pat Example"})
    assert res.fallback_value is None


def test_a_text_box_carries_no_country_when_none_is_stored():
    asked = "Where are you currently located?*"
    assert _typed(asked, {"location": "Atlanta, Georgia"}).fallback_value is None
    assert _typed(asked, {**_HOME, "country": " "}).fallback_value is None


def test_the_country_key_has_no_fallback_of_its_own():
    res = _typed("Country")
    assert (res.semantic_key, res.value, res.fallback_value) == ("country", "United States", None)


def test_a_resolution_has_no_fallback_unless_one_is_set():
    field = FieldSpec(selector="#x", kind="text")
    assert Resolution(field, None, "ask").fallback_value is None
    assert Resolution(field, None, "ask").to_dict()["fallback_value"] is None


# --- fill._option_matches / _matches with place -----------------------------------------


def option_is(answer: str, option: str, *, place: bool = True) -> bool:
    """fill's own call shape: the answer is normalized, the option is raw text."""
    return fill._option_matches(fill._norm(answer), option, place=place)


_PLACE_OPTIONS = [
    pytest.param("United States", "United States of America", id="country-in-full"),
    pytest.param("United States", "USA", id="country-usa"),
    pytest.param("United States", "USA +1", id="country-usa-with-its-dial-code"),
    pytest.param("United States", "United States of America+1", id="country-no-space-code"),
    pytest.param("United States", "U.S.A. +1", id="country-dotted-with-its-code"),
    pytest.param("United States", "  united   states\n of america ", id="case-and-whitespace"),
    pytest.param("USA", "United States +1", id="answer-spelled-short"),
    pytest.param("Atlanta, Georgia", "Atlanta, GA, USA", id="city-and-usa"),
    pytest.param("Atlanta, Georgia", "Atlanta, GA", id="city-postal-code"),
    pytest.param("Atlanta, Georgia", "Atlanta, Georgia, United States", id="city-us-spelled-out"),
]

_NOT_PLACE_OPTIONS = [
    pytest.param("United States Minor Outlying Islands +1", id="minor-outlying-islands-code"),
    pytest.param("United States Minor Outlying Islands", id="minor-outlying-islands"),
    pytest.param("Georgia +995", id="the-country-georgia-with-its-code"),
    pytest.param("Georgia", id="the-country-georgia"),
    pytest.param("United Kingdom +44", id="another-country"),
    pytest.param("US Virgin Islands +1", id="virgin-islands"),
    pytest.param("United States of America +1 +1", id="only-one-code-is-removed"),
    pytest.param("+1", id="bare-code"),
    pytest.param("", id="empty-option"),
]


@pytest.mark.parametrize("answer,option", _PLACE_OPTIONS)
def test_a_place_answer_is_any_option_that_spells_the_same_place(answer, option):
    assert option_is(answer, option) is True


@pytest.mark.parametrize("answer,option", _PLACE_OPTIONS)
def test_the_other_spelling_is_nothing_without_the_place_flag(answer, option):
    # the exact + dial code rule is all a non-place answer ever gets
    assert option_is(answer, option, place=False) is (
        fill._norm(option) == fill._norm(answer)
        or fill._DIAL_CODE.sub("", fill._norm(option), count=1) == fill._norm(answer)
    )


def test_the_place_flag_adds_no_match_to_a_plain_answer_it_did_not_add_before():
    for answer, option in [
        ("United States", "USA"),
        ("United States", "United States of America"),
        ("Atlanta, Georgia", "Atlanta, GA, USA"),
    ]:
        assert option_is(answer, option, place=False) is False
        assert option_is(answer, option, place=True) is True


@pytest.mark.parametrize("option", _NOT_PLACE_OPTIONS)
def test_nothing_that_only_looks_like_the_us_is_the_us(option):
    assert option_is("United States", option) is False


def test_atlanta_georgia_is_neither_athens_nor_atlanta_texas_in_a_react_select():
    for option in ("Athens, GA, USA", "Athens, GA", "Atlanta, TX, USA", "Atlanta, Texas"):
        assert option_is("Atlanta, Georgia", option) is False
    assert option_is("Atlanta, Texas", "Atlanta, Georgia") is False
    assert option_is("Atlanta, Texas", "Atlanta, GA, USA") is False


def test_an_empty_place_answer_matches_no_option():
    for option in ("+1", "USA", "Atlanta, GA, USA", "United States +1"):
        assert option_is("", option) is False


def test_a_chip_that_already_holds_the_place_verifies_as_the_answer():
    assert fill._matches("United States", "United States of America", True, place=True) is True
    assert fill._matches("United States", "USA +1", True, place=True) is True
    assert fill._matches("Atlanta, Georgia", "Atlanta, GA, USA", True, place=True) is True
    # the chip a non-place field leaves is checked as it always was
    assert fill._matches("United States", "United States of America", True) is False
    assert fill._matches("Atlanta, Georgia", "Atlanta, GA, USA", True) is False


def test_a_chip_that_only_looks_like_the_place_does_not_verify():
    assert fill._matches("United States", "United States Minor Outlying Islands", True, place=True) is False
    assert fill._matches("United States", "Georgia +995", True, place=True) is False
    assert fill._matches("Atlanta, Georgia", "Athens, GA, USA", True, place=True) is False
    assert fill._matches("United States", "", True, place=True) is False


def test_a_plain_field_is_read_back_as_before_whatever_the_key():
    # fill() types the stored text itself into a plain field: only that text verifies
    assert fill._matches("United States", "USA", False, place=True) is False
    assert fill._matches("United States", "united  states", False, place=True) is True


@pytest.mark.parametrize(
    "answer,readback,picked,verified",
    [
        pytest.param("United States", "+1", "USA +1", True, id="usa-and-its-code"),
        pytest.param(
            "United States", "+1", "United States of America +1", True, id="in-full-and-its-code"
        ),
        pytest.param("United States", "+44", "USA +1", False, id="another-code-on-the-page"),
        pytest.param("United States", "", "USA +1", False, id="nothing-on-the-page"),
        pytest.param("United States", "+1", "USA", False, id="a-pick-with-no-code"),
        pytest.param("United States", "+1", "", False, id="nothing-picked"),
        pytest.param(
            "United States",
            "+1",
            "United States Minor Outlying Islands +1",
            False,
            id="minor-outlying-islands-shares-the-code",
        ),
        pytest.param("United States", "+995", "Georgia +995", False, id="the-country-georgia"),
    ],
)
def test_a_pick_that_shows_only_its_dial_code_is_verified_through_the_spelling(
    answer, readback, picked, verified
):
    assert fill._matches(answer, readback, True, picked, place=True) is verified


def test_the_dial_code_read_back_of_another_spelling_needs_the_place_flag():
    assert fill._matches("United States", "+1", True, "USA +1", place=False) is False
    assert fill._matches("United States", "+1", True, "USA +1", place=True) is True


# --- fill._commit_react_select with place: a fake combobox -------------------------------------


class _PlaceBox(_Box):
    """The _Box combobox plus what a place answer reads: the texts of the open
    menu's options (fill._MENU_OPTIONS_JS), which the dial-code fake does not
    answer. `menu_reads` says what that script returns: "list" (the menu, or
    null while it is closed), or "raises", "null", "numbers", "elsewhere" (a list
    that does not hold the focused option) for a menu that cannot be read, or a
    list of texts, returned as it is.
    `by_city` makes the menu an async lookup that suggests by the part before the
    first comma, whatever else is typed, as a places search does; a static list
    filters by contains. `menu_reads_made` counts the reads."""

    def __init__(self, options, *, menu_reads="list", by_city=False, **kwargs):
        super().__init__(options, **kwargs)
        self.menu_reads = menu_reads
        self.by_city = by_city
        self.menu_reads_made = 0

    def blur(self, **kwargs):
        """Focus leaves the box: react-select closes the menu and drops what was typed.
        It selects nothing, and no key is pressed."""
        self.log.append(("blur",))
        self.is_open = False
        self.text = ""

    def _refilter(self):
        if not self.by_city:
            return super()._refilter()
        needle = self.text.split(",")[0].strip().casefold()
        self.menu = [option for option in self.options if needle in option.casefold()]
        self.focus_idx = 0 if self.menu else -1

    def evaluate(self, script, arg=None, **kwargs):
        if script != fill._MENU_OPTIONS_JS:
            return super().evaluate(script, arg, **kwargs)
        self.menu_reads_made += 1
        if self.menu_reads == "raises":
            raise RuntimeError("the page went away")
        if self.menu_reads == "null":
            return None
        if self.menu_reads == "numbers":
            return [1, 2]
        if self.menu_reads == "elsewhere":
            return ["Something else"]
        if isinstance(self.menu_reads, list):
            return list(self.menu_reads)
        if self.is_open and 0 <= self.focus_idx < len(self.menu):
            return list(self.menu)
        return None


def _state(box):
    return {"multi": False, "readonly": False, "value": box.value}


def _place_commit(box, answer, *, place=True, picked=None):
    picks = [] if picked is None else picked
    return fill._commit_react_select(box, answer, _state(box), picks, place=place), picks


def _resolution(answer, key="location", label="Location (City)"):
    field = FieldSpec(selector="#place", kind="text", label=label, required=True)
    return Resolution(field=field, semantic_key=key, action="fill", value=answer)


_ATLANTAS = ["Atlanta, TX, USA", "Atlanta, GA, USA", "Atlanta, IN, USA"]


def test_the_one_option_that_spells_the_place_is_tabbed():
    box = _PlaceBox(_ATLANTAS, by_city=True)
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert problem == ""
    assert picks == ["Atlanta, GA, USA"]
    assert box.selected == "Atlanta, GA, USA"
    assert box.keys == ["ArrowDown", "Tab"]  # past Atlanta, TX; never Enter


def test_a_static_list_that_filters_by_the_full_text_shows_nothing_to_pick():
    # The residual: fill() puts "Atlanta, Georgia" in the box, a contains-filter
    # finds no option with that text, so there is nothing focused to accept.
    box = _PlaceBox(["Athens, GA, USA", "Atlanta, GA, USA"])
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert problem == "no option exactly matching 'Atlanta, Georgia'"
    assert picks == []
    assert box.selected == ""
    assert "Tab" not in box.keys


def test_two_options_that_spell_the_place_commit_nothing():
    box = _PlaceBox(["Atlanta, GA, USA", "Atlanta, Georgia, United States"], by_city=True)
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert problem.startswith("no option exactly matching")
    assert picks == []
    assert box.selected == ""
    assert "Tab" not in box.keys


def test_two_options_that_spell_the_place_commit_nothing_in_a_list_that_has_a_third_between():
    box = _PlaceBox(
        ["Atlanta, GA, USA", "Atlanta, TX, USA", "Atlanta, Georgia, United States"], by_city=True
    )
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert problem.startswith("no option exactly matching")
    assert "Tab" not in box.keys
    assert picks == []


def test_an_option_that_equals_the_answer_wins_over_a_spelling_that_comes_first():
    box = _PlaceBox(["Atlanta, GA, USA", "Atlanta, Georgia"], by_city=True)
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert problem == ""
    assert picks == ["Atlanta, Georgia"]
    assert box.selected == "Atlanta, Georgia"


def test_an_exact_option_is_tabbed_whether_or_not_the_menu_can_be_read():
    box = _PlaceBox(["Atlanta, Georgia"], menu_reads="raises")
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert (problem, picks) == ("", ["Atlanta, Georgia"])


@pytest.mark.parametrize("menu_reads", ["raises", "null", "numbers", "elsewhere"])
def test_a_menu_that_cannot_be_read_is_never_shown_to_hold_one_spelling(menu_reads):
    box = _PlaceBox(_ATLANTAS, by_city=True, menu_reads=menu_reads)
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert problem.startswith("no option exactly matching")
    assert picks == []
    assert box.selected == ""
    assert "Tab" not in box.keys
    assert box.menu_reads_made > 0  # it did look


def test_a_menu_read_that_does_not_list_the_focused_option_is_not_trusted():
    # The read shows exactly one spelling, but not the option Tab would commit.
    box = _PlaceBox(
        ["Atlanta, GA, USA"], by_city=True, menu_reads=["Atlanta, Georgia, United States"]
    )
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert problem.startswith("no option exactly matching")
    assert (picks, box.selected) == ([], "")
    assert "Tab" not in box.keys
    assert box.menu_reads_made > 0


def _opened(box):
    """The box with its menu open on the first option, as ArrowDown leaves it."""
    box.press("ArrowDown")
    return box


def test_the_spelling_count_is_the_options_in_the_open_menu_that_spell_the_place():
    want = fill._norm("Atlanta, Georgia")
    one = _opened(_PlaceBox(["Atlanta, TX, USA", "Atlanta, GA, USA"]))
    assert fill._spelling_options(one, want, "Atlanta, TX, USA") == 1
    two = _opened(_PlaceBox(["Atlanta, GA, USA", "Atlanta, GA", "Atlanta, TX, USA"]))
    assert fill._spelling_options(two, want, "Atlanta, GA, USA") == 2
    none = _opened(_PlaceBox(["Atlanta, TX, USA", "Athens, GA, USA"]))
    assert fill._spelling_options(none, want, "Atlanta, TX, USA") == 0


def test_the_spelling_count_is_zero_when_an_option_equals_the_answer_or_the_menu_cannot_be_shown():
    want = fill._norm("Atlanta, Georgia")
    exact = _opened(_PlaceBox(["Atlanta, GA, USA", "Atlanta, Georgia"]))
    assert fill._spelling_options(exact, want, "Atlanta, GA, USA") == 0
    closed = _PlaceBox(["Atlanta, GA, USA"])
    assert fill._spelling_options(closed, want, "Atlanta, GA, USA") == 0  # the read is null
    unlisted = _opened(_PlaceBox(["Atlanta, GA, USA"]))
    assert fill._spelling_options(unlisted, want, "Atlanta, Georgia, United States") == 0


def test_without_the_place_flag_a_spelling_is_never_tabbed_and_the_menu_is_not_read():
    box = _PlaceBox(_ATLANTAS, by_city=True)
    problem, picks = _place_commit(box, "Atlanta, Georgia", place=False)
    assert problem.startswith("no option exactly matching")
    assert picks == []
    assert box.selected == ""
    assert "Tab" not in box.keys
    assert box.menu_reads_made == 0


def test_without_the_place_flag_the_exact_option_is_tabbed_as_before():
    box = _PlaceBox(["Atlanta, GA, USA", "Atlanta, Georgia"], by_city=True)
    problem, picks = _place_commit(box, "Atlanta, Georgia", place=False)
    assert (problem, picks) == ("", ["Atlanta, Georgia"])
    assert box.menu_reads_made == 0


def test_a_chip_that_already_holds_another_spelling_is_left_alone_for_a_place():
    box = _PlaceBox(_ATLANTAS, value="Atlanta, GA, USA", by_city=True)
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert (problem, picks) == ("", [])
    assert box.keys == [] and box.typed == []


def test_a_chip_that_holds_another_spelling_is_not_left_alone_for_a_plain_answer():
    box = _PlaceBox(_ATLANTAS, value="Atlanta, GA, USA", by_city=True)
    problem, _ = _place_commit(box, "Atlanta, Georgia", place=False)
    assert problem.startswith("no option exactly matching")


def test_a_chip_that_holds_another_place_is_not_the_answer():
    box = _PlaceBox(_ATLANTAS, value="Atlanta, TX, USA", by_city=True)
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert (problem, picks) == ("", ["Atlanta, GA, USA"])
    assert box.selected == "Atlanta, GA, USA"


def test_a_list_without_the_place_commits_nothing_and_clears_the_filter():
    box = _PlaceBox(["Atlanta, TX, USA", "Atlanta, IN, USA"], by_city=True)
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert problem.startswith("no option exactly matching")
    assert (picks, box.selected) == ([], "")
    assert box.typed[-1] == ""  # the filter was cleared; blur selects nothing


def test_the_other_atlanta_state_is_never_taken_for_atlanta_georgia():
    box = _PlaceBox(["Atlanta, TX, USA"], by_city=True)
    problem, _ = _place_commit(box, "Atlanta, Georgia")
    assert problem.startswith("no option exactly matching")
    assert box.selected == ""
    box = _PlaceBox(["Atlanta, Georgia"], by_city=True)
    problem, _ = _place_commit(box, "Atlanta, Texas")
    assert problem.startswith("no option exactly matching")
    assert box.selected == ""


# --- the typed fallback (a Country that ignores fill()) ---------------------------------------

_COUNTRY_MENU = [
    "Canada",
    "United States of America +1",
    "United States Minor Outlying Islands +1",
]


def test_the_us_option_is_accepted_at_the_first_word_that_shows_it():
    box = _PlaceBox(_COUNTRY_MENU, keys_only=True)
    problem, picks = _place_commit(box, "United States")
    assert problem == ""
    assert picks == ["United States of America +1"]
    assert box.selected == "United States of America +1"
    assert box.sequences == ["United"]  # "States" was never typed
    assert box.keys == ["ArrowDown", "Tab"]


def test_the_city_option_is_accepted_before_a_later_word_filters_it_away():
    # typing "Georgia" would drop every option that spells the state "GA"
    box = _PlaceBox(
        ["Athens, GA, USA", "Atlanta, GA, USA", "Atlanta, TX, USA"], keys_only=True
    )
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert problem == ""
    assert picks == ["Atlanta, GA, USA"]
    assert box.sequences == ["Atlanta,"]
    assert box.keys == ["ArrowDown", "Tab"]


def test_without_the_place_flag_the_typing_runs_to_the_last_word_and_commits_nothing():
    box = _PlaceBox(["Athens, GA, USA", "Atlanta, GA, USA"], keys_only=True)
    problem, picks = _place_commit(box, "Atlanta, Georgia", place=False)
    assert problem.startswith("no option exactly matching")
    assert box.sequences == ["Atlanta,", " Georgia"]
    assert (picks, box.selected) == ([], "")
    assert "Tab" not in box.keys


def test_the_typing_goes_on_when_the_early_option_is_not_the_place():
    box = _PlaceBox(
        ["Athens, GA, USA", "Athens, TX, USA", "Atlanta, GA, USA"], keys_only=True
    )
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert problem == ""
    assert picks == ["Atlanta, GA, USA"]
    assert box.sequences == ["Atlanta,"]  # the menu narrowed to Atlanta at the first word


def test_a_place_that_is_in_the_list_only_as_two_spellings_commits_nothing_when_typed():
    # Narrowing the menu leaves one of the two behind; the first read of both counts.
    box = _PlaceBox(
        ["Atlanta, GA, USA", "Atlanta, Georgia, United States", "Atlanta, TX, USA"],
        keys_only=True,
    )
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert problem.startswith("no option exactly matching")
    assert picks == []
    assert box.selected == ""
    assert "Tab" not in box.keys


def test_two_spellings_that_the_first_word_filters_apart_still_commit_nothing():
    # "United" drops "USA" and leaves one spelling in view; the menu as it opened
    # (before anything was typed) showed both, and that read still counts.
    box = _PlaceBox(["Canada +1", "USA +1", "United States of America +1"], keys_only=True)
    problem, picks = _place_commit(box, "United States")
    assert problem.startswith("no option exactly matching")
    assert (picks, box.selected) == ([], "")
    assert "Tab" not in box.keys


def test_the_typed_menu_with_an_exact_option_commits_the_exact_option():
    box = _PlaceBox(["Atlanta, GA, USA", "Atlanta, Georgia"], keys_only=True)
    problem, picks = _place_commit(box, "Atlanta, Georgia")
    assert (problem, picks) == ("", ["Atlanta, Georgia"])


@pytest.mark.parametrize("menu_reads", ["raises", "null", "numbers", "elsewhere"])
def test_a_typed_menu_that_cannot_be_read_commits_no_spelling(menu_reads):
    box = _PlaceBox(_COUNTRY_MENU, keys_only=True, menu_reads=menu_reads)
    problem, picks = _place_commit(box, "United States")
    assert problem.startswith("no option exactly matching")
    assert (picks, box.selected) == ([], "")
    assert "Tab" not in box.keys
    assert box.sequences == ["United", " States"]  # typed the whole answer, as always


def test_the_early_accept_adds_no_key_but_the_ones_the_typed_flow_already_sent():
    box = _PlaceBox(_COUNTRY_MENU, keys_only=True)
    _place_commit(box, "United States")
    assert set(box.keys) <= {"ArrowDown", "Tab"}
    assert "Enter" not in box.keys and "Escape" not in box.keys


def test_the_country_georgia_is_not_picked_for_united_states_when_typed():
    box = _PlaceBox(["Georgia +995", "Canada +1"], keys_only=True)
    problem, picks = _place_commit(box, "United States")
    assert problem.startswith("no option exactly matching")
    assert (picks, box.selected) == ([], "")


def test_minor_outlying_islands_alone_is_not_picked_for_united_states_when_typed():
    box = _PlaceBox(["Canada +1", "United States Minor Outlying Islands +1"], keys_only=True)
    problem, picks = _place_commit(box, "United States")
    assert problem.startswith("no option exactly matching")
    assert (picks, box.selected) == ([], "")


# --- the whole fill: the pick, the read-back and the verify pass -------------------------


def _dial_code_only(option):
    return option[option.rindex("+") :]


def test_apply_one_commits_and_verifies_a_country_spelled_another_way():
    box = _PlaceBox(["Canada +1", "United States of America +1"])
    outcome = fill.apply_one(box, _resolution("United States", "country", "Country"), settle_ms=0)
    assert (outcome.committed, outcome.error) == (True, "")
    assert outcome.value == "United States"
    assert outcome.readback == "United States of America +1"
    assert outcome.picked == "United States of America +1"


def test_apply_one_verifies_a_pick_that_shows_only_its_dial_code():
    box = _PlaceBox(_COUNTRY_MENU, keys_only=True, show=_dial_code_only)
    outcome = fill.apply_one(box, _resolution("United States", "country", "Country"), settle_ms=0)
    assert (outcome.committed, outcome.error) == (True, "")
    assert (outcome.readback, outcome.picked) == ("+1", "United States of America +1")


def test_the_verify_pass_does_not_retry_a_pick_that_holds():
    box = _PlaceBox(_ATLANTAS, by_city=True)
    [outcome] = fill.apply_resolutions(box, [_resolution("Atlanta, Georgia")], settle_ms=0)
    assert (outcome.committed, outcome.error) == (True, "")
    assert outcome.readback == "Atlanta, GA, USA"
    assert box.keys == ["ArrowDown", "Tab"]  # a retry would have pressed more keys


def test_the_verify_pass_accepts_another_spelling_without_a_second_try(monkeypatch):
    box = _PlaceBox(_ATLANTAS, by_city=True)
    res = _resolution("Atlanta, Georgia")
    outcome = fill.apply_one(box, res, settle_ms=0)
    assert outcome.committed is True
    monkeypatch.setattr(fill, "_apply_one", lambda *args, **kwargs: pytest.fail("retried"))
    verified = fill._verify_one(box, res, outcome, 0)
    assert (verified.committed, verified.error) == (True, "")
    assert verified.readback == "Atlanta, GA, USA"


def test_a_pick_of_another_place_is_not_verified_by_a_shared_dial_code():
    # The page shows "+1" for any of these; only the option that was Tabbed counts.
    box = _PlaceBox(
        ["Canada +1", "United States Minor Outlying Islands +1"], show=_dial_code_only
    )
    outcome = fill.apply_one(box, _resolution("United States", "country", "Country"), settle_ms=0)
    assert outcome.committed is False
    assert outcome.picked == ""
    assert box.selected == ""


def test_a_non_place_field_gets_no_spelling_through_apply_one():
    box = _PlaceBox(["Canada +1", "United States of America +1"])
    res = _resolution("United States", "school", "School")
    outcome = fill.apply_one(box, res, settle_ms=0)
    assert outcome.committed is False
    assert box.selected == ""
    assert box.menu_reads_made == 0


# --- the whole fill: a residence question whose menu lists countries ------------------------
#
# fill.py tries Resolution.fallback_value (the stored country) in the same box, but only
# when the menu had no option at all for the city: the exact text _commit_react_select
# returns for that, nothing else. These use the fake combobox; the same cases run in a
# real page in tests/test_fill_browser.py.

_WORLD = ["Canada", "Georgia", "United States Minor Outlying Islands", "United States of America"]
_CITY_MISS = "no option exactly matching 'Atlanta, Georgia'"
_CITY_CROWDED = "no option exactly matching 'Atlanta, Georgia' (two or more options spell it)"


def _residence_res(
    *, answer="Atlanta, Georgia", country="United States", label="Where are you currently located?*"
):
    field = FieldSpec(selector="#place", kind="text", label=label, required=True)
    return Resolution(
        field=field, semantic_key="location", action="fill", value=answer, fallback_value=country
    )


def test_the_no_option_text_is_exact_for_a_miss_and_longer_for_a_crowded_menu():
    miss = _PlaceBox(["Athens, GA, USA", "Atlanta, TX, USA"], by_city=True)
    problem, _ = _place_commit(miss, "Atlanta, Georgia")
    assert problem == _CITY_MISS == fill._no_option_text("Atlanta, Georgia")
    crowded = _PlaceBox(["Atlanta, GA, USA", "Atlanta, Georgia, United States"], by_city=True)
    problem, _ = _place_commit(crowded, "Atlanta, Georgia")
    assert problem == _CITY_CROWDED == _CITY_MISS + fill._CROWDED_SUFFIX


def test_the_country_is_committed_when_the_menu_has_no_option_for_the_city():
    box = _PlaceBox(_WORLD)
    outcome = fill.apply_one(box, _residence_res(), settle_ms=0)
    assert (outcome.committed, outcome.error) == (True, "")
    assert outcome.value == "United States"  # what was filled, so what is checked
    assert (outcome.readback, outcome.picked) == ("United States of America",) * 2
    assert box.selected == "United States of America"
    assert box.keys == ["ArrowDown", "Tab"]  # past the islands to the country; never Enter
    assert box.typed == ["Atlanta, Georgia", "", "United States"]  # city, its cleanup, country


def test_the_verify_pass_keeps_the_country_that_held():
    box = _PlaceBox(_WORLD)
    [outcome] = fill.apply_resolutions(box, [_residence_res()], settle_ms=0)
    assert (outcome.committed, outcome.error, outcome.value) == (True, "", "United States")
    assert box.keys == ["ArrowDown", "Tab"]  # a retry would have pressed more keys


def test_the_verify_pass_takes_the_country_again_when_the_pick_is_lost():
    box = _PlaceBox(_WORLD)
    res = _residence_res()
    outcome = fill.apply_one(box, res, settle_ms=0)
    box.value = box.selected = ""  # the page wiped it
    verified = fill._verify_one(box, res, outcome, 0)
    assert (verified.committed, verified.error, verified.value) == (True, "", "United States")
    assert box.selected == "United States of America"
    assert box.keys == ["ArrowDown", "Tab", "ArrowDown", "Tab"]


def test_the_country_is_taken_when_the_list_spells_it_exactly():
    box = _PlaceBox(["Canada", "United States Minor Outlying Islands", "United States"])
    outcome = fill.apply_one(box, _residence_res(), settle_ms=0)
    assert (outcome.committed, outcome.error) == (True, "")
    assert (outcome.picked, outcome.value) == ("United States", "United States")
    assert box.keys == ["ArrowDown", "Tab"]  # past the islands; never Enter


@pytest.mark.parametrize(
    "options",
    [
        pytest.param(["Athens, GA, USA", "Georgia", "Canada"], id="a-city-list"),
        pytest.param(["Georgia", "United States Minor Outlying Islands"], id="georgia-and-islands"),
        pytest.param(["United States - Remote", "Remote - United States"], id="near-misses"),
        pytest.param([], id="an-empty-menu"),
    ],
)
def test_a_menu_with_neither_the_city_nor_the_country_commits_nothing(options):
    box = _PlaceBox(options)
    outcome = fill.apply_one(box, _residence_res(), settle_ms=0)
    assert (outcome.committed, outcome.error) == (False, _CITY_MISS)  # the city's problem
    assert outcome.value == "Atlanta, Georgia"
    assert (outcome.picked, box.selected) == ("", "")
    assert "Tab" not in box.keys
    assert box.typed == ["Atlanta, Georgia", "", "United States", ""]  # tried, cleaned up twice


def test_two_options_that_spell_the_city_never_try_the_country():
    options = ["Atlanta, GA, USA", "Atlanta, Georgia, United States", "United States of America"]
    box = _PlaceBox(options, by_city=True)
    outcome = fill.apply_one(box, _residence_res(), settle_ms=0)
    assert (outcome.committed, outcome.error) == (False, _CITY_CROWDED)
    assert outcome.value == "Atlanta, Georgia"
    assert box.selected == ""  # the country is in the menu: it would be the pick if tried
    assert "Tab" not in box.keys
    assert "United States" not in box.typed


@pytest.mark.parametrize(
    "problem",
    [
        pytest.param("multi-select combobox not supported", id="multi-select"),
        pytest.param(_CITY_CROWDED, id="two-options-spell-it"),
        pytest.param("no option exactly matching 'Boston, Massachusetts'", id="another-answer"),
        pytest.param(_CITY_MISS + ".", id="the-text-and-more"),
        pytest.param("Timeout 30000ms exceeded.", id="a-page-error"),
        pytest.param("", id="no-problem-at-all"),
    ],
)
def test_the_country_is_tried_only_on_the_no_option_text_of_the_city(problem):
    box = _PlaceBox(_WORLD)
    picks: list[str] = []
    got = fill._commit_country_fallback(box, _residence_res(), "Atlanta, Georgia", problem, picks)
    assert got == ""
    assert (picks, box.log) == ([], [])  # the box was not touched


def test_the_country_is_tried_on_the_no_option_text_of_the_city():
    box = _PlaceBox(_WORLD)
    picks: list[str] = []
    got = fill._commit_country_fallback(box, _residence_res(), "Atlanta, Georgia", _CITY_MISS, picks)
    assert (got, picks) == ("United States", ["United States of America"])


def test_only_a_location_answer_falls_back_to_the_country():
    # A country answer that the menu lacks is not swapped for another country.
    res = Resolution(
        field=FieldSpec(selector="#place", kind="text", label="Country", required=True),
        semantic_key="country",
        action="fill",
        value="Mexico",
        fallback_value="United States",
    )
    box = _PlaceBox(_WORLD)
    outcome = fill.apply_one(box, res, settle_ms=0)
    assert (outcome.committed, outcome.error) == (False, "no option exactly matching 'Mexico'")
    assert (outcome.value, box.selected) == ("Mexico", "")
    assert "Tab" not in box.keys
    assert box.typed == ["Mexico", ""]  # tried and cleaned up; "United States" never typed


@pytest.mark.parametrize("country", [None, "", "   ", 7])
def test_there_is_no_country_to_try_without_a_usable_fallback_value(country):
    box = _PlaceBox(_WORLD)
    res = _residence_res(country=country)
    assert fill._commit_country_fallback(box, res, "Atlanta, Georgia", _CITY_MISS, []) == ""
    assert box.log == []
    outcome = fill.apply_one(_PlaceBox(_WORLD), res, settle_ms=0)
    assert (outcome.committed, outcome.error) == (False, _CITY_MISS)


def test_an_empty_city_is_never_followed_by_the_country():
    box = _PlaceBox(_WORLD)
    res = _residence_res(answer="")
    assert fill._commit_country_fallback(box, res, "", fill._no_option_text(""), []) == ""
    assert box.log == []


@pytest.mark.parametrize("country_try", ["raises", "no option"])
def test_a_country_try_that_fails_leaves_the_problem_of_the_city(monkeypatch, country_try):
    calls = []

    def commit(loc, expected, combobox, picked=None, *, place=False):
        calls.append((expected, place))
        if len(calls) == 1:
            return fill._no_option_text(expected)
        if country_try == "raises":
            raise RuntimeError("the page went away")
        return fill._no_option_text(expected)

    monkeypatch.setattr(fill, "_commit_react_select", commit)
    outcome = fill.apply_one(_PlaceBox(_WORLD), _residence_res(), settle_ms=0)
    assert calls == [("Atlanta, Georgia", True), ("United States", True)]
    assert (outcome.committed, outcome.error) == (False, _CITY_MISS)
    assert (outcome.value, outcome.picked) == ("Atlanta, Georgia", "")


@pytest.mark.parametrize("looks", [1, 2])
def test_a_box_that_is_not_a_combobox_any_more_gets_no_country(monkeypatch, looks):
    box = _PlaceBox(_WORLD)
    # The first look finds an open combobox (so the box is blurred), the second none;
    # or the first look already finds none.
    states = iter([{"multi": False, "readonly": False, "value": "", "expanded": True}][: looks - 1])
    seen = []

    def detect(loc):
        seen.append(loc)
        return next(states, None)

    monkeypatch.setattr(fill, "_detect_react_select", detect)
    got = fill._commit_country_fallback(box, _residence_res(), "Atlanta, Georgia", _CITY_MISS, [])
    assert got == ""
    assert len(seen) == looks
    assert box.log == ([("blur",)] if looks == 2 else [])  # nothing typed, nothing pressed


def test_a_menu_left_open_by_the_citys_try_is_closed_by_blur_before_the_country_is_typed():
    countries = [f"Country {number:03d}" for number in range(60)]
    countries += ["United States Minor Outlying Islands", "United States of America"]
    box = _PlaceBox(countries, keys_only=True)
    outcome = fill.apply_one(box, _residence_res(), settle_ms=0)
    assert (outcome.committed, outcome.error) == (True, "")
    assert box.selected == "United States of America"
    assert box.log.count(("blur",)) == 1  # once, between the two tries
    assert set(box.keys) <= {"ArrowDown", "Tab"}  # blur is not a key: no Escape, no Enter
    assert set(box.chars) <= set("Atlanta, GeorgiaUnited States")  # only the answers' own


def test_a_menu_that_is_already_closed_is_not_blurred():
    box = _PlaceBox(_WORLD)
    fill.apply_one(box, _residence_res(), settle_ms=0)
    assert ("blur",) not in box.log
