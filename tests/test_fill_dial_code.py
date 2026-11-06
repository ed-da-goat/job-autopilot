"""A react-select option that wears a dial code is still the bare answer.

Greenhouse's new application form has a phone-number Country react-select whose
options read "United States +1", "United Arab Emirates +971" and, with no space
before the plus, "Afghanistan+93". The stored `country` answer is the bare name,
"United States". fill.py therefore lets an option carry ONE trailing dial code
and nothing else. These tests pin both halves: the code is tolerated, and
nothing near it is (no prefix, substring or fuzzy match, so "United States
Minor Outlying Islands +1" is never taken for "United States").

They need no browser. tests/test_fill_browser.py drives the same options through
a real Chrome react-select.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect

import pytest

from jobhunt.apply_assist import fill
from jobhunt.apply_assist.schema import FieldSpec, FillOutcome, Resolution


def option_is(answer: str, option: str) -> bool:
    """fill's own call shape: the answer is normalized, the option is raw text."""
    return fill._option_matches(fill._norm(answer), option)


# --- the helper ---------------------------------------------------------------

_TOLERATED = [
    pytest.param("United States", "United States +1", id="space-before-the-code"),
    pytest.param("United States", "United States+1", id="no-space-before-the-code"),
    pytest.param("Afghanistan", "Afghanistan+93", id="two-digit-code"),
    pytest.param("United Arab Emirates", "United Arab Emirates +971", id="three-digit-code"),
    pytest.param("Antigua and Barbuda", "Antigua and Barbuda +1268", id="four-digit-code"),
    pytest.param("United States", "  UNITED   States \n +1 ", id="case-and-whitespace-as-ever"),
    pytest.param("united STATES", "United States +1", id="the-answers-case-does-not-matter"),
]

_EXACT = [
    pytest.param("United States", "United States", id="same-text"),
    pytest.param("united  states", " United States ", id="case-and-whitespace"),
    pytest.param("Yes", "Yes", id="a-yes-no-question"),
    pytest.param("United States +1", "United States +1", id="an-answer-that-includes-the-code"),
]

# Every one of these is NOT the answer "United States".
_REFUSED = [
    pytest.param("United States Minor Outlying Islands +1", id="longer-name-with-the-code"),
    pytest.param("United States Minor Outlying Islands", id="longer-name"),
    pytest.param("United Kingdom +44", id="another-country"),
    pytest.param("United Arab Emirates +971", id="another-united-country"),
    pytest.param("United", id="shorter-prefix"),
    pytest.param("United +1", id="shorter-prefix-with-the-code"),
    pytest.param("+1", id="bare-code"),
    pytest.param("", id="empty-option"),
    pytest.param("The United States +1", id="longer-name-ending-in-the-answer"),
    pytest.param("United Staates +1", id="typo-is-not-fuzzy-matched"),
    pytest.param("United States +1 +1", id="only-one-code-is-removed"),
    pytest.param("United States +12345", id="five-digits-is-not-a-dial-code"),
    pytest.param("+1 United States", id="leading-code"),
    pytest.param("United +1 States", id="code-in-the-middle"),
    pytest.param("United States (+1)", id="parenthesized-code"),
    pytest.param("United States +1a", id="code-with-a-letter"),
    pytest.param("United States +", id="plus-without-digits"),
    pytest.param("United States 1", id="digits-without-a-plus"),
]


@pytest.mark.parametrize("answer,option", _TOLERATED)
def test_an_option_wearing_one_trailing_dial_code_is_the_answer(answer, option):
    assert option_is(answer, option) is True


@pytest.mark.parametrize("answer,option", _EXACT)
def test_a_plain_exact_option_still_matches(answer, option):
    assert option_is(answer, option) is True


@pytest.mark.parametrize("option", _REFUSED)
def test_nothing_but_one_trailing_dial_code_is_tolerated(option):
    assert option_is("United States", option) is False


@pytest.mark.parametrize("answer", ["United", "States", "United Stat", "united states +"])
def test_a_shorter_answer_never_matches_the_longer_option(answer):
    assert option_is(answer, "United States +1") is False


def test_the_dial_code_is_dropped_from_the_option_never_from_the_answer():
    assert option_is("United States +1", "United States +1") is True
    assert option_is("United States +1", "United States") is False


def test_a_bare_dial_code_is_not_the_answer_to_an_empty_answer():
    assert option_is("", "+1") is False
    assert option_is("", "United States +1") is False


def test_a_missing_option_text_matches_nothing():
    assert fill._option_matches("united states", None) is False


# --- the read-back check --------------------------------------------------------


def test_a_combobox_chip_with_a_dial_code_verifies_as_the_answer():
    assert fill._matches("United States", "United States +1", True) is True
    assert fill._matches("American Samoa", "American Samoa+1", True) is True


def test_a_combobox_chip_that_only_looks_similar_does_not_verify():
    assert fill._matches("United States", "United States Minor Outlying Islands +1", True) is False
    assert fill._matches("United", "United States +1", True) is False
    assert fill._matches("United States", "", True) is False


def test_only_a_combobox_read_back_tolerates_a_dial_code():
    # A plain field that holds "United States +1" is not the answer "United States".
    assert fill._matches("United States", "United States +1", False) is False
    assert fill._matches("United States", "united  states", False) is True


# --- the read-back of a pick that shows only its dial code ---------------------------

# Greenhouse's phone-block Country shows a committed option as a flag plus its dial
# code ("+1"), never its name. fill.py checks the name where the option is picked
# (Tab goes only to a focused option that is the answer) and keeps that option's text
# as `picked`; the read-back then only confirms the pick held, through that option's
# own code. Rows: the answer, what the page shows, the option the Tab committed.

_VERIFIED_BY_ITS_CODE = [
    pytest.param("United States", "+1", "United States +1", id="the-pick-and-its-code"),
    pytest.param("United States", "+1", "United States+1", id="no-space-before-the-code"),
    pytest.param("Afghanistan", "+93", "Afghanistan+93", id="two-digit-code"),
    pytest.param("United Arab Emirates", "+971", "United Arab Emirates +971", id="three-digits"),
    pytest.param("Antigua and Barbuda", "+1268", "Antigua and Barbuda +1268", id="four-digits"),
    pytest.param("united STATES", " +1 ", "  UNITED   States \n +1 ", id="case-and-whitespace"),
]

_NOT_VERIFIED_BY_A_CODE = [
    pytest.param("United States", "", "United States +1", id="empty-read-back"),
    pytest.param("United States", "+93", "United States +1", id="a-different-code"),
    pytest.param("United States", "+44", "United States +1", id="another-countrys-code"),
    pytest.param("United States", "+12", "United States +1", id="a-longer-code-than-the-pick"),
    pytest.param("Antigua and Barbuda", "+1", "Antigua and Barbuda +1268", id="a-shorter-code"),
    pytest.param("United States", "US +1", "United States +1", id="text-beside-the-code"),
    pytest.param("United States", "+1", "United States", id="a-pick-without-a-dial-code"),
    pytest.param("United States", "", "United States", id="nothing-shown-for-a-pick-without-one"),
    pytest.param("United States", "+1", "", id="nothing-picked"),
    pytest.param("United States", "+1", "Canada +1", id="another-country-picked"),
    pytest.param(
        "United States", "+1", "United States Minor Outlying Islands +1", id="a-longer-name-picked"
    ),
    pytest.param("United States", "+1", "+1", id="only-a-code-picked"),
    pytest.param("United States", "+1", "United States +1 +1", id="two-codes-on-the-pick"),
    pytest.param("", "+1", "+1", id="no-answer"),
]


@pytest.mark.parametrize("answer,readback,picked", _VERIFIED_BY_ITS_CODE)
def test_a_dial_code_read_back_verifies_the_option_that_was_picked(answer, readback, picked):
    assert fill._matches(answer, readback, True, picked) is True


@pytest.mark.parametrize("answer,readback,picked", _NOT_VERIFIED_BY_A_CODE)
def test_a_dial_code_read_back_verifies_nothing_else(answer, readback, picked):
    assert fill._matches(answer, readback, True, picked) is False


def test_a_dial_code_read_back_is_never_verified_when_nothing_was_picked_by_default():
    assert fill._matches("United States", "+1", True) is False


def test_the_pick_never_takes_away_a_match_the_read_back_already_has():
    assert fill._matches("United States", "United States +1", True, "Canada +1") is True
    assert fill._matches("United States", "united  states", True, "") is True


def test_the_pick_counts_for_a_combobox_only():
    assert fill._matches("United States", "+1", False, "United States +1") is False
    assert fill._matches("United States", "+1", False) is False


def test_picked_is_the_last_field_of_an_outcome_and_empty_by_default():
    assert [f.name for f in dataclasses.fields(FillOutcome)][-1] == "picked"
    field = FieldSpec(selector="#country", kind="text", label="Country")
    outcome = FillOutcome(field, "country", "United States", "United States", True)
    assert outcome.picked == ""
    assert outcome.to_dict()["picked"] == ""


# --- the three call sites, without a browser -----------------------------------

_WITH_CODES = [
    "United Arab Emirates +971",
    "United States +1",
    "United States Minor Outlying Islands +1",
]


class _Box:
    """A react-select combobox without a browser, as test_apply_assist.py's
    _FakeCombobox: typing filters the options (case-insensitive contains) and
    focuses the first match, ArrowDown opens a closed menu or moves the focus
    with wrap-around, Tab commits the focused option of an open menu. Typed text
    is never a value. The scripts fill ships are answered by identity.

    `keys_only` is the Greenhouse Country: fill() leaves its text in the input
    but the widget never hears of it (the menu stays closed, nothing focused);
    only real key presses (ArrowDown, press_sequentially) open and filter it. As
    in react-select, a Space typed into an EMPTY box with an open menu selects
    the focused option. `swallow` is how many typed keys the page loses before
    one lands. `show` is what the single-value renders for a committed option
    (its own text, by default; Greenhouse's phone-block Country shows only the
    option's dial code, see _dial_code_only); `selected` is the option actually
    committed. `log` is every interaction, in order."""

    def __init__(
        self, options, *, value="", keys_only=False, swallow=0, reports_expanded=True, show=None
    ):
        self.options = list(options)
        self.value = value
        self.selected = ""
        self.show = show or (lambda option: option)
        self.keys_only = keys_only
        self.swallow = swallow
        self.reports_expanded = reports_expanded
        self.text = ""
        self.menu: list[str] = []
        self.focus_idx = -1
        self.is_open = False
        self.keys: list[str] = []
        self.typed: list[str] = []
        self.sequences: list[str] = []
        self.chars: list[str] = []
        self.delays: list = []
        self.timeouts: list = []
        self.log: list[tuple] = []

    @property
    def first(self):
        return self

    def locator(self, selector):
        return self

    def _refilter(self):
        needle = self.text.casefold()
        self.menu = [option for option in self.options if needle in option.casefold()]
        self.focus_idx = 0 if self.menu else -1

    def _commit(self, option):
        self.selected = option
        self.value = self.show(option)

    def evaluate(self, script, arg=None, **kwargs):
        if script == fill._REACT_SELECT_JS:
            state = {"multi": False, "readonly": False, "value": self.value}
            if self.reports_expanded:
                state["expanded"] = self.is_open
            return state
        assert script == fill._FOCUSED_OPTION_JS
        if self.is_open and 0 <= self.focus_idx < len(self.menu):
            return self.menu[self.focus_idx]
        return ""

    def fill(self, text, **kwargs):
        self.typed.append(text)
        self.log.append(("fill", text))
        self.text = text
        if self.keys_only:
            if self.is_open:
                self._refilter()
            return
        self._refilter()
        self.is_open = bool(text)

    def focus(self, **kwargs):
        self.log.append(("focus",))

    def press(self, key, **kwargs):
        self.keys.append(key)
        self.log.append(("press", key))
        if key == "ArrowDown":
            if not self.is_open:
                self.is_open = True
                self._refilter()
            elif self.menu:
                self.focus_idx = (self.focus_idx + 1) % len(self.menu)
        elif key == "Tab":
            if self.is_open and 0 <= self.focus_idx < len(self.menu):
                self._commit(self.menu[self.focus_idx])
            self.is_open = False
            self.text = ""

    def press_sequentially(self, text, delay=None, timeout=None):
        self.log.append(("seq", text))
        self.sequences.append(text)
        self.delays.append(delay)
        self.timeouts.append(timeout)
        for char in text:
            self.chars.append(char)
            if self.swallow:
                self.swallow -= 1  # the page lost this key
                continue
            has_focus = self.is_open and 0 <= self.focus_idx < len(self.menu)
            if char == " " and not self.text and has_focus:
                # react-select: a Space in an empty box picks the focused option
                self._commit(self.menu[self.focus_idx])
                self.is_open = False
                continue
            self.text += char
            self.is_open = True
            self._refilter()

    def input_value(self, **kwargs):
        if not self.keys_only:
            raise AssertionError("a combobox is read back from its single-value, not typed text")
        self.log.append(("input_value",))
        return self.text


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(fill, "_pause", lambda seconds: None)


def _commit(box, answer, *, readonly=False, picked=None):
    state = {"multi": False, "readonly": readonly, "value": box.value}
    return fill._commit_react_select(box, answer, state, picked)


def _country(answer):
    field = FieldSpec(selector="#country", kind="text", label="Country", required=True)
    return Resolution(field=field, semantic_key="country", action="fill", value=answer)


def test_the_option_with_a_dial_code_is_tabbed_and_the_bare_name_is_typed():
    box = _Box(_WITH_CODES)
    assert _commit(box, "United States") == ""
    assert box.typed == ["United States"]  # the filter matches the bare name
    assert box.keys == ["Tab"]
    assert box.value == "United States +1"


def test_a_longer_name_that_is_focused_first_is_skipped_for_the_dial_code_option():
    box = _Box(["United States Minor Outlying Islands +1", "United States +1"])
    assert _commit(box, "United States") == ""
    assert box.keys == ["ArrowDown", "Tab"]
    assert box.value == "United States +1"


def test_a_chip_that_already_holds_the_answer_with_a_dial_code_is_left_alone():
    box = _Box(_WITH_CODES, value="United States +1")
    assert _commit(box, "United States") == ""
    assert box.typed == [] and box.keys == []


def test_a_shorter_answer_never_tabs_a_longer_option():
    box = _Box(_WITH_CODES)
    assert _commit(box, "United") == "no option exactly matching 'United'"
    assert box.keys == ["ArrowDown"] * 3  # one lap over the three, then the filter is cleared
    assert box.typed == ["United", ""]
    assert box.value == ""


def test_the_read_back_of_a_dial_code_chip_verifies_the_fill():
    box = _Box(_WITH_CODES)
    outcome = fill.apply_one(box, _country("United States"), settle_ms=0)
    assert (outcome.committed, outcome.error) == (True, "")
    assert outcome.value == "United States"
    assert outcome.readback == "United States +1"
    assert outcome.picked == "United States +1"


# --- a pick that reads back as its dial code only ------------------------------------

# Greenhouse's phone-block Country renders the committed option as a flag plus its
# dial code, "+1": the name is gone and the input's own value is empty. fill.py
# checks the name where the option is picked, and the read-back confirms the pick
# held. `_Box(..., show=_dial_code_only)` is that widget.


def _dial_code_only(option):
    """What that Country shows for a committed option: its dial code, not its name."""
    return option[option.rindex("+") :]


def _full_pass(box, answer="United States"):
    """apply_resolutions: the fill, the verify read, and at most one retry."""
    [outcome] = fill.apply_resolutions(box, [_country(answer)], settle_ms=0)
    return outcome


def test_a_pick_that_shows_only_its_dial_code_is_verified_through_the_picked_option():
    box = _Box(_WITH_CODES, show=_dial_code_only)
    outcome = fill.apply_one(box, _country("United States"), settle_ms=0)
    assert (outcome.committed, outcome.error) == (True, "")
    assert (outcome.value, outcome.readback) == ("United States", "+1")
    assert outcome.picked == "United States +1"
    assert box.selected == "United States +1"
    assert box.keys == ["Tab"]


def test_the_verify_pass_accepts_that_pick_without_a_second_try():
    box = _Box(_WITH_CODES, show=_dial_code_only)
    outcome = _full_pass(box)
    assert (outcome.committed, outcome.error, outcome.readback) == (True, "", "+1")
    assert outcome.picked == "United States +1"
    assert box.keys == ["Tab"]  # a retry would have pressed Tab again


def test_the_typed_fallback_pick_is_verified_the_same_way():
    box = _Box(_WITH_CODES, keys_only=True, show=_dial_code_only)
    outcome = _full_pass(box)
    assert (outcome.committed, outcome.error, outcome.readback) == (True, "", "+1")
    assert outcome.picked == "United States +1"
    assert box.keys == ["ArrowDown", "Tab"]


def test_picked_is_the_option_that_was_focused_when_tab_went_in():
    options = ["United States Minor Outlying Islands +1", "United States +1"]
    box = _Box(options, show=_dial_code_only)
    outcome = _full_pass(box)
    assert (outcome.committed, outcome.readback) == (True, "+1")
    assert outcome.picked == "United States +1"  # not the longer option focused first
    assert box.selected == "United States +1"


@pytest.mark.parametrize("shown", ["+93", "+44", "+12", ""])
def test_a_pick_that_shows_another_dial_code_or_nothing_is_not_verified(shown):
    box = _Box(_WITH_CODES, show=lambda option: shown)
    outcome = _full_pass(box)
    assert outcome.committed is False
    assert outcome.error == ""
    assert outcome.readback == shown
    assert outcome.picked == "United States +1"  # what was committed stays on record
    assert box.keys == ["Tab", "Tab"]  # the first pass and the one retry, never a third


@pytest.mark.parametrize("shown", ["+1", ""])
def test_a_pick_without_a_dial_code_is_not_verified_by_a_code_on_the_page(shown):
    box = _Box(["United States"], show=lambda option: shown)
    outcome = _full_pass(box)
    assert outcome.committed is False
    assert (outcome.readback, outcome.picked) == (shown, "United States")


def test_a_code_on_the_page_is_never_verified_when_no_tab_was_pressed():
    box = _Box(["Canada +1", "Mexico +52"], value="+1", show=_dial_code_only)
    outcome = _full_pass(box)
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'United States'"
    assert outcome.picked == ""
    assert "Tab" not in box.keys


def test_picked_stays_empty_when_the_value_already_matched_and_no_key_was_sent():
    box = _Box(_WITH_CODES, value="United States +1")
    outcome = _full_pass(box)
    assert (outcome.committed, outcome.readback) == (True, "United States +1")
    assert outcome.picked == ""
    assert box.keys == [] and box.typed == []


def test_the_one_retry_carries_its_own_pick():
    shown = iter(["", "+1"])  # the page had not rendered the first pick yet
    box = _Box(_WITH_CODES, show=lambda option: next(shown))
    outcome = _full_pass(box)
    assert (outcome.committed, outcome.error, outcome.readback) == (True, "", "+1")
    assert outcome.picked == "United States +1"
    assert box.keys == ["Tab", "Tab"]


@pytest.mark.parametrize(
    "make_box,readonly,keys",
    [
        pytest.param(lambda: _Box(_WITH_CODES), False, ["Tab"], id="fill-and-tab"),
        pytest.param(
            lambda: _Box(_WITH_CODES, keys_only=True), False, ["ArrowDown", "Tab"], id="typed"
        ),
        pytest.param(
            lambda: _Box(_WITH_CODES), True, ["ArrowDown", "ArrowDown", "Tab"], id="readonly"
        ),
    ],
)
def test_the_commit_reports_the_option_it_tabbed(make_box, readonly, keys):
    box, picks = make_box(), []
    assert _commit(box, "United States", readonly=readonly, picked=picks) == ""
    assert picks == ["United States +1"]
    assert box.keys == keys


class _TabFails(_Box):
    def press(self, key, **kwargs):
        if key == "Tab":
            raise RuntimeError("Tab timed out")
        super().press(key, **kwargs)


def test_a_tab_that_failed_leaves_no_pick_on_record():
    box = _TabFails(_WITH_CODES, show=_dial_code_only)
    outcome = fill.apply_one(box, _country("United States"), settle_ms=0)
    assert outcome.committed is False
    assert outcome.error == "Tab timed out"
    assert (outcome.readback, outcome.picked) == ("", "")
    assert box.selected == ""


@pytest.mark.parametrize(
    "make_box,answer",
    [
        pytest.param(
            lambda: _Box(_WITH_CODES, value="United States +1"),
            "United States",
            id="value-already-matched",
        ),
        pytest.param(lambda: _Box(_WITH_CODES), "United", id="only-longer-options"),
        pytest.param(lambda: _Box(_WITH_CODES), "Narnia", id="no-such-option"),
        pytest.param(lambda: _Box(_WITH_CODES), "", id="no-answer"),
    ],
)
def test_the_commit_reports_no_pick_when_it_pressed_no_tab(make_box, answer):
    box, picks = make_box(), []
    _commit(box, answer, picked=picks)
    assert picks == []
    assert "Tab" not in box.keys


# --- a combobox that ignores fill(): the answer is typed key by key -----------------

# The real Greenhouse Country. fill() leaves its menu closed (aria-expanded false,
# nothing focused), ArrowDown opens it on its first option ('Afghanistan +93'), and
# typing "United States" key by key then leaves 'United States +1' focused. Only
# real key presses reach it, so `_Box(..., keys_only=True)` models exactly that.


def test_a_menu_that_ignores_fill_is_opened_with_arrow_down_and_typed_key_by_key():
    box = _Box(_WITH_CODES, keys_only=True)
    assert _commit(box, "United States") == ""
    assert box.log == [
        ("fill", "United States"),  # tried first: the menu stays closed
        ("fill", ""),  # what that left in the box is dropped
        ("focus",),
        ("press", "ArrowDown"),  # opens the menu
        ("seq", "United"),
        ("input_value",),  # the box holds text before any Space
        ("seq", " States"),
        ("press", "Tab"),  # the focused option is 'United States +1'
    ]
    assert box.value == "United States +1"


def test_a_menu_that_opens_on_fill_is_never_typed_into_key_by_key():
    box = _Box(_WITH_CODES)
    assert _commit(box, "United States") == ""
    assert box.log == [("fill", "United States"), ("press", "Tab")]
    assert box.sequences == [] and box.chars == []


def test_an_open_menu_that_lists_nothing_is_not_retried_by_typing():
    box = _Box(["Mexico", "Canada"])  # fill() worked: the menu is open on "No options"
    assert _commit(box, "Narnia") == "no option exactly matching 'Narnia'"
    assert box.log == [("fill", "Narnia"), ("fill", "")]


def test_a_combobox_that_does_not_report_its_menu_never_takes_the_fallback():
    box = _Box(_WITH_CODES, keys_only=True, reports_expanded=False)
    assert _commit(box, "United States") == "no option exactly matching 'United States'"
    assert box.log == [("fill", "United States"), ("fill", "")]


class _NeverOpens(_Box):
    """A readonly combobox whose ArrowDown does nothing: closed, nothing focused."""

    def press(self, key, **kwargs):
        self.keys.append(key)
        self.log.append(("press", key))


def test_a_readonly_combobox_is_never_typed_into():
    box = _NeverOpens(["Yes", "No"], keys_only=True)
    assert _commit(box, "Maybe", readonly=True) == "no option exactly matching 'Maybe'"
    assert box.log == [("focus",), ("press", "ArrowDown")]  # nothing to type into


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("  United States", id="leading-spaces"),
        pytest.param("United States  ", id="trailing-spaces"),
        pytest.param("United   States", id="inner-run-of-spaces"),
        pytest.param("  United    States  ", id="all-of-them"),
    ],
)
def test_the_answer_is_typed_as_single_spaced_words_and_never_starts_with_a_space(answer):
    box = _Box(_WITH_CODES, keys_only=True)
    assert _commit(box, answer) == ""
    assert box.sequences == ["United", " States"]
    assert box.chars[0] == "U"


# Typed key by key, a newline is Enter (which can submit the form), a tab
# character is Tab and an escape character is Escape. So a stored answer that
# holds any control, format or line-break character, anywhere, has nothing typed
# at all: it is refused as stored, never collapsed into something typeable.
_NOT_PLAIN_TEXT = [
    pytest.param("United\nStates", id="newline"),
    pytest.param("United\rStates", id="carriage-return"),
    pytest.param("United\r\nStates", id="crlf"),
    pytest.param("United\tStates", id="tab"),
    pytest.param("United\x0bStates", id="vertical-tab"),
    pytest.param("United\x0cStates", id="form-feed"),
    pytest.param("United\x85States", id="next-line"),
    pytest.param("United\u2028States", id="line-separator"),
    pytest.param("United\u2029States", id="paragraph-separator"),
    pytest.param("United States\n", id="trailing-newline"),
    pytest.param("\nUnited States", id="leading-newline"),
    pytest.param("\n United\t\tStates \n", id="newlines-and-tabs-around-spaces"),
    pytest.param("United\x1bStates", id="escape-character"),
    pytest.param("United\x00States", id="nul"),
    pytest.param("United\x08States", id="backspace-character"),
    pytest.param("United\x7fStates", id="delete-character"),
    pytest.param("United\u200bStates", id="zero-width-space"),
    pytest.param("United\u200dStates", id="zero-width-joiner"),
    pytest.param("United\u202eStates", id="right-to-left-override"),
    pytest.param("\ufeffUnited States", id="byte-order-mark"),
    pytest.param("United\u00adStates", id="soft-hyphen"),
    pytest.param("United\u00a0States", id="no-break-space"),
    pytest.param("United\ud800States", id="lone-surrogate"),
    pytest.param("x" * (fill._MAX_TYPED_CHARS + 1), id="longer-than-any-option"),
]


@pytest.mark.parametrize("answer", _NOT_PLAIN_TEXT)
def test_an_answer_that_is_not_plain_printable_text_is_never_typed(answer):
    box = _Box(_WITH_CODES, keys_only=True)
    assert _commit(box, answer) == f"no option exactly matching {answer!r}"
    assert box.log == [("fill", answer), ("fill", "")]  # no key of any kind was sent
    assert box.keys == [] and box.sequences == [] and box.chars == []
    assert box.value == ""


@pytest.mark.parametrize(
    "keys_only", [True, False], ids=["menu-ignores-fill", "menu-follows-fill"]
)
def test_a_stored_answer_with_a_newline_types_nothing_and_presses_no_tab(keys_only):
    answer = "United\nStates"  # 'United States +1' is right there, one collapse away
    box = _Box(_WITH_CODES, keys_only=keys_only)
    assert _commit(box, answer) == f"no option exactly matching {answer!r}"
    assert box.sequences == [] and box.chars == []  # nothing typed key by key
    assert "Tab" not in box.keys and "Enter" not in box.keys
    assert box.keys == []  # not even the ArrowDown that would open the menu
    assert box.value == ""  # nothing committed
    assert box.log == [("fill", answer), ("fill", "")]


def test_a_newline_in_the_answer_is_refused_before_the_menu_is_opened_and_the_apply_reports_it():
    box = _Box(_WITH_CODES, keys_only=True)
    outcome = fill.apply_one(box, _country("United\nStates"), settle_ms=0)
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'United\\nStates'"
    assert box.keys == [] and box.sequences == []


@pytest.mark.parametrize(
    "answer,typed",
    [
        ("United States", "United States"),
        ("  United   States  ", "United States"),
        ("Yes", "Yes"),
        ("x" * fill._MAX_TYPED_CHARS, "x" * fill._MAX_TYPED_CHARS),
        ("", ""),
        ("    ", ""),
        ("  United \n States\t", ""),
        ("\n", ""),
        ("Uni\x07ted", ""),
        ("Uni\u200bted", ""),
        ("x" * (fill._MAX_TYPED_CHARS + 1), ""),
        (None, ""),
    ],
)
def test_what_may_be_typed(answer, typed):
    assert fill._typeable(answer) == typed


@pytest.mark.parametrize("answer", _NOT_PLAIN_TEXT)
def test_no_answer_that_is_not_plain_printable_text_may_be_typed(answer):
    assert fill._typeable(answer) == ""


def test_control_the_fake_picks_the_focused_option_on_a_space_in_an_empty_box():
    box = _Box(_WITH_CODES, keys_only=True)
    box.press("ArrowDown")
    box.press_sequentially(" ")
    assert box.value == "United Arab Emirates +971"  # what the guard below keeps from happening


def test_a_space_is_never_sent_into_an_empty_box():
    box = _Box(_WITH_CODES, keys_only=True, swallow=len("United"))  # the first word is lost
    assert _commit(box, "United States") == "no option exactly matching 'United States'"
    assert " " not in box.chars
    assert box.value == ""
    assert box.keys == ["ArrowDown"]
    assert box.log[-3:] == [("seq", "United"), ("input_value",), ("fill", "")]


def test_typing_goes_on_once_something_has_landed_and_the_option_is_still_checked():
    box = _Box(_WITH_CODES, keys_only=True, swallow=2)  # "Un" is lost, "ited" lands
    assert _commit(box, "United States") == ""
    assert box.sequences == ["United", " States"]
    assert box.value == "United States +1"  # Tab went to an option whose text is the answer


def test_tab_is_never_pressed_when_no_option_matches_the_typed_answer():
    box = _Box(_WITH_CODES, keys_only=True)
    assert _commit(box, "Narnia") == "no option exactly matching 'Narnia'"
    assert box.sequences == ["Narnia"]
    assert box.keys == ["ArrowDown"]
    assert box.value == ""
    assert box.log[-1] == ("fill", "")  # the typed filter is cleared; blur selects nothing


def test_a_typed_answer_that_only_prefixes_longer_options_is_never_tabbed():
    box = _Box(_WITH_CODES, keys_only=True)
    assert _commit(box, "United") == "no option exactly matching 'United'"
    assert box.keys == ["ArrowDown"] * 4  # one to open the menu, then one lap over the three
    assert box.value == ""


def test_the_keys_are_typed_at_a_persons_pace_with_a_timeout_that_covers_them():
    box = _Box(_WITH_CODES, keys_only=True)
    assert _commit(box, "United States") == ""
    assert box.delays == [fill._KEY_DELAY_MS] * 2 and fill._KEY_DELAY_MS >= 20
    for text, timeout in zip(box.sequences, box.timeouts, strict=True):
        assert timeout >= fill._ACTION_TIMEOUT_MS + len(text) * fill._KEY_DELAY_MS


def test_the_country_typed_into_a_menu_that_ignored_fill_is_read_back_as_committed():
    box = _Box(_WITH_CODES, keys_only=True)
    outcome = fill.apply_one(box, _country("United States"), settle_ms=0)
    assert (outcome.committed, outcome.error) == (True, "")
    assert outcome.readback == "United States +1"


@pytest.mark.parametrize(
    "state,closed",
    [
        pytest.param({"expanded": False}, True, id="reports-closed"),
        pytest.param({"expanded": True}, False, id="reports-open"),
        pytest.param({"expanded": None}, False, id="says-nothing"),
        pytest.param({}, False, id="no-field"),
        pytest.param(None, False, id="not-a-combobox"),
    ],
)
def test_a_menu_is_closed_only_when_the_combobox_says_so(state, closed):
    class Stub:
        def evaluate(self, script, arg=None, **kwargs):
            return state

    assert fill._menu_is_closed(Stub()) is closed


def test_every_press_sequentially_call_paces_itself_and_has_a_timeout():
    """Key-by-key text is always sent with a delay and a timeout. Where it may be
    called from is the static guard in tests/test_apply_assist.py."""
    calls = [
        node
        for node in ast.walk(ast.parse(inspect.getsource(fill)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "press_sequentially"
    ]
    assert calls, "fill.py no longer types key by key; drop the fallback tests with it"
    for call in calls:
        assert {kw.arg for kw in call.keywords} >= {"delay", "timeout"}
