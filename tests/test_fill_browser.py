"""Real-Chromium checks of apply_assist/fill.py on simulated Greenhouse pages.

tests/test_apply_assist.py drives fill.py through fakes, so none of its
JavaScript (field scan, hydration probe, upload record, react-select detection)
runs there. These tests run that JavaScript in a real browser against pages
that reproduce the three Greenhouse behaviours that made fills not stick:

  * React hydrates the server-rendered form after DOMContentLoaded and its first
    re-render wipes anything typed before then.
  * `input#resume` is removed from the DOM the moment its change event fires.
  * react-select comboboxes clear typed text on blur; only a committed option
    counts.

Pages are built in memory with page.set_content. Nothing here opens a real ATS
or the network (every http(s) request is aborted), and fill.py is never given a
form to submit: each page records clicks, submits and key presses so the tests
can prove fill sent nothing but Tab/ArrowDown (and, for a combobox that ignores
fill(), the answer's own characters) to a combobox.

Chromium: system Chrome is launched first (apply_assist/driver.py drives it
too), then Playwright's bundled Chromium. The module skips only if neither
launches. Run `uv run pytest -q tests/test_fill_browser.py -rs` to confirm the
tests ran rather than skipped.
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
from pathlib import Path

import pytest

from jobhunt.apply_assist import fill, mapper, solve, submit
from jobhunt.apply_assist.keys import classify
from jobhunt.apply_assist.schema import FieldSpec, Resolution, TriageDigest

sync_api = pytest.importorskip("playwright.sync_api")


# --- browser fixtures ---------------------------------------------------------

# Branded Chrome starts `GoogleUpdater --wake-all` some 15-20 s after launch.
# The updater, and the crash handler that outlives the browser with it, inherit
# the stderr pipe Playwright's driver reads, and the driver only reports a
# browser closed once every holder of that pipe has exited. Without this switch
# Browser.close() blocked for 15-300 s after any session that long, which this
# module's own run is. Builds that do not know the switch ignore it.
_LAUNCH_ARGS = ["--disable-updater-scheduler"]


@pytest.fixture(scope="module")
def browser():
    """One headless Chromium for the module: system Chrome, else bundled."""
    try:
        playwright = sync_api.sync_playwright().start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"playwright driver did not start: {exc}")
    launched = None
    try:
        failures: list[str] = []
        for label, extra in (("system Chrome", {"channel": "chrome"}), ("bundled Chromium", {})):
            try:
                launched = playwright.chromium.launch(headless=True, args=_LAUNCH_ARGS, **extra)
                break
            except Exception as exc:  # noqa: BLE001
                reason = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
                failures.append(f"{label}: {reason}")
        if launched is None:
            pytest.skip("no Chromium could be launched (" + "; ".join(failures) + ")")
        yield launched
    finally:
        if launched is not None:
            launched.close()
        playwright.stop()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1100, "height": 900})
    # The pages are built in memory; nothing may reach a real site.
    context.route(re.compile(r"^https?://"), lambda route: route.abort())
    tab = context.new_page()
    tab.set_default_timeout(10_000)
    try:
        yield tab
    finally:
        context.close()


@pytest.fixture
def fast_waits(monkeypatch):
    """Shorten the fixed pause before fill's verify pass; no behavior under test
    here depends on its length (the hydration tests keep the real one)."""
    monkeypatch.setattr(fill, "_VERIFY_DELAY_S", 0.05)


# --- react-select simulation --------------------------------------------------

# The phone-number country list on Greenhouse: ~244 hidden [role=option]
# elements that must never be mistaken for the Country menu.
_PHONE_COUNTRIES = [f"Country {i} +{i}" for i in range(240)] + [
    "United States +1",
    "Canada +1",
    "Afghanistan +93",
    "Albania +355",
]

_SELECT_PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { font: 14px sans-serif; margin: 24px; }
  .select__control { display: flex; width: 320px; min-height: 38px; border: 1px solid #888; }
  .select__value-container { display: grid; flex: 1; padding: 2px 8px; }
  .select__placeholder, .select__single-value, .select__input-container { grid-area: 1 / 1; }
  .select__input { width: 100%; min-width: 2px; border: 0; outline: 0; background: transparent; }
  .select__menu { position: absolute; width: 320px; border: 1px solid #888; background: #fff; }
  .select__option--is-focused { background: #dde; }
  .requiredInput__x1 { opacity: 0; position: absolute; width: 100%; height: 0; pointer-events: none; }
  .phone-list { display: none; }
</style>
</head>
<body>
<form id="app" action="/never-submitted" method="post" novalidate>
  <div>
    <label for="first_name">First Name *</label>
    <input id="first_name" name="first_name" type="text" required>
  </div>
  <div id="country-shell" style="position: relative">
    <label for="country">Country *</label>
    <div class="select__control" id="country-control">
      <div class="__CONTAINER_CLASS__">
        <div class="select__placeholder" id="country-placeholder">Select...</div>
        <div class="select__input-container">
          <input id="country" class="select__input" type="text" role="combobox"__READONLY__
                 aria-required="true" aria-autocomplete="list" aria-expanded="false"
                 aria-haspopup="true" autocomplete="off" spellcheck="false" value="">
        </div>
      </div>
      <div class="select__indicators"><span class="select__dropdown-indicator">v</span></div>
    </div>
    <input class="requiredInput__x1" tabindex="-1" autocomplete="off" required value="">
  </div>
  <button type="submit" id="go">Submit application</button>
</form>
<ul class="phone-list" role="listbox">__DISTRACTORS__</ul>
<script>
(function () {
  var OPTIONS = __OPTIONS__;
  var PORTAL = __PORTAL__;
  var ARIA_ACTIVE = __ARIA_ACTIVE__;
  var SEARCHABLE = __SEARCHABLE__;
  var DELAY = __RENDER_DELAY__;
  // The real Greenhouse Country opens and filters only on key events: Playwright's
  // fill() (insertText: an input event with no key down) leaves its menu closed.
  var KEYS_ONLY = __KEYS_ONLY__;
  // Printable keystrokes this page loses before any lands (a re-render eating them).
  var SWALLOW = __SWALLOW__;
  // How a committed option is drawn. null: its own text. Greenhouse's phone-block
  // Country draws a flag and the option's dial code, never its name ("dial-code");
  // any other string is the code drawn whatever was picked (a page that disagrees).
  var SHOWS = __SHOWS__;
  var input = document.getElementById('country');
  var shell = document.getElementById('country-shell');
  var valueContainer = shell.querySelector('.select__value-container');
  var host = PORTAL ? document.body : shell;
  var open = false, query = '', focused = -1, shown = [];
  var keyIsDown = false, swallowed = 0;
  window.__keys = [];
  window.__keyTimes = [];
  window.__ignored = [];
  window.__clicks = [];
  window.__submits = 0;
  window.__selected = null;

  function refilter() {
    var q = query.trim().toLowerCase();
    shown = OPTIONS.filter(function (label) {
      return !q || label.toLowerCase().indexOf(q) !== -1;
    });
    focused = shown.length ? 0 : -1;
  }

  function render() {
    var old = document.getElementById('country-menu');
    if (old) old.remove();
    input.setAttribute('aria-expanded', open ? 'true' : 'false');
    input.removeAttribute('aria-activedescendant');
    if (!open) return;
    var menu = document.createElement('div');
    menu.id = 'country-menu';
    menu.className = 'select__menu';
    var list = document.createElement('div');
    list.className = 'select__menu-list';
    list.setAttribute('role', 'listbox');
    if (!shown.length) {
      var none = document.createElement('div');
      none.className = 'select__menu-notice select__menu-notice--no-options';
      none.textContent = 'No options';
      list.appendChild(none);
    }
    shown.forEach(function (label, i) {
      var opt = document.createElement('div');
      opt.id = 'react-select-country-option-' + i;
      opt.setAttribute('role', 'option');
      opt.className = 'select__option' + (i === focused ? ' select__option--is-focused' : '');
      opt.textContent = label;
      list.appendChild(opt);
    });
    menu.appendChild(list);
    host.appendChild(menu);
    if (ARIA_ACTIVE && focused >= 0) {
      input.setAttribute('aria-activedescendant', 'react-select-country-option-' + focused);
    }
  }

  // React batches renders: the DOM can trail the state by a few milliseconds.
  function schedule() { if (DELAY) setTimeout(render, DELAY); else render(); }

  function drawSingleValue(single, label) {
    if (SHOWS === null) { single.textContent = label; return; }
    var plus = label.lastIndexOf('+');
    var code = SHOWS === 'dial-code' ? (plus < 0 ? '' : label.slice(plus)) : SHOWS;
    single.textContent = '';
    var flag = document.createElement('div');
    flag.className = 'iti__flag iti__us';
    var span = document.createElement('span');
    span.textContent = code;
    single.appendChild(flag);
    single.appendChild(span);
  }

  function choose(label) {
    window.__selected = label;
    open = false; query = ''; input.value = '';
    var placeholder = valueContainer.querySelector('.select__placeholder');
    if (placeholder) placeholder.remove();
    var single = valueContainer.querySelector('.select__single-value');
    if (!single) {
      single = document.createElement('div');
      single.className = 'select__single-value';
      valueContainer.insertBefore(single, valueContainer.firstChild);
    }
    drawSingleValue(single, label);
    var required = document.querySelector('input[class*="requiredInput"]');
    if (required) required.remove();
    schedule();
  }

  input.addEventListener('input', function () {
    if (!SEARCHABLE) return;
    // A controlled input: an input event with no key down is put back as it was.
    if (KEYS_ONLY && !keyIsDown) { window.__ignored.push(input.value); input.value = query; return; }
    query = input.value; open = true; refilter(); schedule();
  });
  input.addEventListener('keyup', function () { keyIsDown = false; });
  input.addEventListener('keydown', function (ev) {
    window.__keys.push(ev.key);
    window.__keyTimes.push([ev.key, performance.now()]);
    keyIsDown = true;
    if (KEYS_ONLY && ev.key.length === 1 && swallowed < SWALLOW) {
      swallowed += 1; ev.preventDefault(); return;
    }
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      if (!open) { open = true; refilter(); }
      else if (shown.length) { focused = (focused + 1) % shown.length; }
      schedule();
    } else if (ev.key === 'Tab') {
      // react-select's tabSelectsValue: Tab picks the focused option and keeps focus.
      if (open && focused >= 0) { ev.preventDefault(); choose(shown[focused]); }
    } else if (ev.key === 'Enter') {
      if (open && focused >= 0) { ev.preventDefault(); choose(shown[focused]); }
    } else if (ev.key === ' ') {
      // react-select: a Space in an EMPTY box is "pick the focused option" (or
      // "open the menu"); in a box that holds text it is just a character.
      if (!input.value) {
        ev.preventDefault();
        if (!open) { open = true; refilter(); schedule(); }
        else if (focused >= 0) { choose(shown[focused]); }
      }
    } else if (ev.key === 'Escape') {
      open = false; query = ''; input.value = ''; schedule();
    }
  });
  // Blur never selects: typed text is simply dropped.
  input.addEventListener('blur', function () {
    keyIsDown = false; query = ''; input.value = ''; open = false; render();
  });
  document.addEventListener('click', function (ev) {
    window.__clicks.push(ev.target.id || ev.target.tagName);
  }, true);
  document.getElementById('app').addEventListener('submit', function (ev) {
    ev.preventDefault();
    window.__submits += 1;
  });
})();
</script>
</body>
</html>
"""


def _select_page(
    options: list[str],
    *,
    portal: bool = False,
    aria_active: bool = True,
    searchable: bool = True,
    multi: bool = False,
    render_delay_ms: int = 0,
    keys_only: bool = False,
    swallow: int = 0,
    shows: str | None = None,
) -> str:
    distractors = "".join(
        f'<li role="option" id="phone-option-{i}">{name}</li>'
        for i, name in enumerate(_PHONE_COUNTRIES)
    )
    container = "select__value-container"
    if multi:
        container += " select__value-container--is-multi"
    return (
        _SELECT_PAGE.replace("__CONTAINER_CLASS__", container)
        .replace("__READONLY__", "" if searchable else " readonly")
        .replace("__DISTRACTORS__", distractors)
        .replace("__OPTIONS__", json.dumps(options))
        .replace("__PORTAL__", "true" if portal else "false")
        .replace("__ARIA_ACTIVE__", "true" if aria_active else "false")
        .replace("__SEARCHABLE__", "true" if searchable else "false")
        .replace("__RENDER_DELAY__", str(render_delay_ms))
        .replace("__KEYS_ONLY__", "true" if keys_only else "false")
        .replace("__SWALLOW__", str(swallow))
        .replace("__SHOWS__", json.dumps(shows))
    )


_COUNTRIES = [
    "Afghanistan",
    "Canada",
    "United Kingdom",
    "United States",
    "United States Minor Outlying Islands",
]
# Typing "United States" focuses the first match, which here is the longer option.
_COUNTRIES_LONGER_FIRST = [
    "Afghanistan",
    "United States Minor Outlying Islands",
    "United States",
    "Canada",
]

_MENU_VARIANTS = [
    pytest.param({}, id="inline-menu"),
    pytest.param({"portal": True}, id="portaled-menu"),
    pytest.param({"portal": True, "aria_active": False}, id="portaled-menu-class-fallback"),
    pytest.param({"render_delay_ms": 40}, id="async-render"),
]

# Delete is Playwright's own doing: locator.fill("") clears a field by pressing
# Delete after selecting its text. fill.py itself only ever presses Tab/ArrowDown,
# and types the answer's own characters into a combobox that ignores fill().
_ALLOWED_KEYS = {"Tab", "ArrowDown", "Delete"}


def _country(value: str) -> Resolution:
    spec = FieldSpec(
        selector="#country", kind="text", label="Country", elem_id="country", required=True
    )
    return Resolution(field=spec, semantic_key="country", action="fill", value=value)


def _ruled(value: str, rule: str, args: list[str], key: str = "degree") -> Resolution:
    """A Greenhouse list whose options solve.py could not see: fill.py applies the rule
    to the open menu."""
    spec = FieldSpec(
        selector="#country", kind="text", label="Degree*", elem_id="country", required=True
    )
    return Resolution(
        field=spec, semantic_key=key, action="fill", value=value,
        option_rule=rule, option_args=args,
    )  # fmt: skip


def _keys(page) -> list[str]:
    return page.evaluate("window.__keys")


def _nav_keys(page) -> list[str]:
    """The keys fill chose to press. Playwright's own Delete (locator.fill("")
    empties a field that way) is not one of them."""
    return [key for key in _keys(page) if key != "Delete"]


def _single_value(page) -> str | None:
    single = page.locator(".select__single-value")
    return single.first.text_content() if single.count() else None


def _assert_inert(page, typed: str = "") -> None:
    """fill sent no Enter/Escape, clicked nothing and submitted nothing. `typed`
    is the answer a test expects to have been typed key by key, if any: its own
    characters are the only ones allowed besides Tab/ArrowDown/Delete."""
    keys = _keys(page)
    assert set(keys) <= _ALLOWED_KEYS | set(typed), keys
    assert page.evaluate("window.__clicks") == []
    assert page.evaluate("window.__submits") == 0


def test_simulation_control_typing_alone_selects_nothing(page):
    """The Greenhouse failure: typed text is cleared on blur, nothing selected."""
    page.set_content(_select_page(_COUNTRIES))
    page.locator("#country").fill("United States")
    assert page.input_value("#country") == "United States"
    page.locator("#first_name").focus()  # blur the combobox
    assert page.input_value("#country") == ""
    assert _single_value(page) is None
    assert page.locator("input[class*=requiredInput]").count() == 1


def test_simulation_control_harness_sees_enter_clicks_and_submits(page):
    """_assert_inert would fail if fill ever pressed Enter on a closed menu."""
    page.set_content(_select_page(_COUNTRIES))
    page.locator("#country").press("Enter")
    assert "Enter" in _keys(page)
    assert page.evaluate("window.__clicks") == ["go"]
    assert page.evaluate("window.__submits") == 1
    with pytest.raises(AssertionError):
        _assert_inert(page)


@pytest.mark.parametrize("variant", _MENU_VARIANTS)
def test_react_select_commits_the_exact_option_when_it_is_focused_first(page, fast_waits, variant):
    page.set_content(_select_page(_COUNTRIES, **variant))
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.readback == "United States"
    assert _single_value(page) == "United States"
    assert page.evaluate("window.__selected") == "United States"
    assert page.locator("input[class*=requiredInput]").count() == 0
    assert _keys(page) == ["Tab"]
    _assert_inert(page)


@pytest.mark.parametrize("variant", _MENU_VARIANTS)
def test_react_select_arrows_past_a_longer_option_that_is_focused_first(page, fast_waits, variant):
    page.set_content(_select_page(_COUNTRIES_LONGER_FIRST, **variant))
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.readback == "United States"
    assert page.evaluate("window.__selected") == "United States"
    assert _single_value(page) == "United States"
    assert page.locator("input[class*=requiredInput]").count() == 0
    # Tab was only sent once the exact option was the focused one.
    assert _keys(page) == ["ArrowDown", "Tab"]
    _assert_inert(page)


def test_react_select_missing_option_selects_nothing(page, fast_waits, monkeypatch):
    monkeypatch.setattr(fill, "_OPTION_POLL_S", 0.01)  # no menu appears: skip the 3 s poll
    page.set_content(_select_page(["Afghanistan", "Mexico", "United States"]))
    [outcome] = fill.apply_resolutions(page, [_country("Canada")])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'Canada'"
    assert outcome.readback == ""
    assert _single_value(page) is None
    assert page.evaluate("window.__selected") is None
    assert page.locator("input[class*=requiredInput]").count() == 1
    assert page.input_value("#country") == ""  # the filter text was cleared again
    # No Tab, no ArrowDown: the filter text was only typed, then emptied again.
    assert _nav_keys(page) == []
    page.locator("#first_name").focus()  # blurring still selects nothing
    assert _single_value(page) is None
    _assert_inert(page)


def test_react_select_near_misses_are_never_committed(page, fast_waits):
    page.set_content(
        _select_page(["United States Minor Outlying Islands", "United States of Atlantis"])
    )
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'United States'"
    assert page.evaluate("window.__selected") is None
    assert _single_value(page) is None
    # One lap over the two near-miss options, then the filter is cleared. Never Tab.
    assert _nav_keys(page) == ["ArrowDown", "ArrowDown"]
    _assert_inert(page)


def test_react_select_already_chosen_value_is_left_alone(page, fast_waits):
    page.set_content(_select_page(_COUNTRIES))
    fill.apply_resolutions(page, [_country("United States")])
    assert _keys(page) == ["Tab"]
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.committed is True
    assert outcome.error == ""
    assert outcome.readback == "United States"
    assert _keys(page) == ["Tab"]  # nothing typed or pressed the second time
    _assert_inert(page)


def test_react_select_readonly_combobox_opens_with_arrow_down(page, fast_waits):
    page.set_content(_select_page(["Yes", "No"], searchable=False))
    [outcome] = fill.apply_resolutions(page, [_country("No")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.readback == "No"
    assert page.evaluate("window.__selected") == "No"
    # Open the menu (Yes is focused), move to No, commit it.
    assert _keys(page) == ["ArrowDown", "ArrowDown", "Tab"]
    _assert_inert(page)


def test_react_select_readonly_combobox_without_the_answer_selects_nothing(page, fast_waits):
    page.set_content(_select_page(["Yes", "No"], searchable=False))
    [outcome] = fill.apply_resolutions(page, [_country("Maybe")])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'Maybe'"
    assert page.evaluate("window.__selected") is None
    assert _keys(page) == ["ArrowDown", "ArrowDown", "ArrowDown"]  # open + one lap
    _assert_inert(page)


def test_react_select_multi_select_is_refused_untouched(page, fast_waits):
    page.set_content(_select_page(_COUNTRIES, multi=True))
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.committed is False
    assert outcome.error == "multi-select combobox not supported"
    assert _keys(page) == []
    assert page.input_value("#country") == ""
    assert page.evaluate("window.__selected") is None
    _assert_inert(page)


# --- react-select options that carry a dial code ------------------------------

# The new Greenhouse form's phone block has its own Country react-select whose
# options read "<name> +<dial code>", some with no space before the plus. The
# stored answer is the bare name, and typing the bare name is what filters them.
_DIAL_CODE_COUNTRIES = [
    "United Arab Emirates +971",
    "United States +1",
    "United States Minor Outlying Islands +1",
    "American Samoa+1",
]


@pytest.mark.parametrize("variant", _MENU_VARIANTS)
def test_react_select_commits_the_option_that_carries_a_dial_code(page, fast_waits, variant):
    page.set_content(_select_page(_DIAL_CODE_COUNTRIES, **variant))
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.value == "United States"
    assert outcome.readback == "United States +1"
    assert _single_value(page) == "United States +1"
    assert page.evaluate("window.__selected") == "United States +1"
    assert page.locator("input[class*=requiredInput]").count() == 0
    assert _keys(page) == ["Tab"]
    _assert_inert(page)


def test_react_select_commits_a_dial_code_written_without_a_space(page, fast_waits):
    page.set_content(_select_page(_DIAL_CODE_COUNTRIES))
    [outcome] = fill.apply_resolutions(page, [_country("American Samoa")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.readback == "American Samoa+1"
    assert _single_value(page) == "American Samoa+1"
    assert page.evaluate("window.__selected") == "American Samoa+1"
    assert _keys(page) == ["Tab"]
    _assert_inert(page)


def test_react_select_arrows_past_a_longer_name_to_the_option_with_the_dial_code(page, fast_waits):
    page.set_content(_select_page(["United States Minor Outlying Islands +1", "United States +1"]))
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.readback == "United States +1"
    assert page.evaluate("window.__selected") == "United States +1"
    # Tab was only sent once the exact name (plus its code) was the focused option.
    assert _keys(page) == ["ArrowDown", "Tab"]
    _assert_inert(page)


def test_react_select_leaves_a_chip_that_already_holds_the_answer_with_its_dial_code(
    page, fast_waits
):
    page.set_content(_select_page(_DIAL_CODE_COUNTRIES))
    fill.apply_resolutions(page, [_country("United States")])
    assert _keys(page) == ["Tab"]
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.readback == "United States +1"
    assert _keys(page) == ["Tab"]  # nothing typed or pressed the second time
    _assert_inert(page)


def test_react_select_never_picks_a_longer_option_for_a_shorter_answer(page, fast_waits):
    page.set_content(_select_page(_DIAL_CODE_COUNTRIES))
    [outcome] = fill.apply_resolutions(page, [_country("United")])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'United'"
    assert outcome.readback == ""
    assert _single_value(page) is None
    assert page.evaluate("window.__selected") is None
    assert page.locator("input[class*=requiredInput]").count() == 1
    assert page.input_value("#country") == ""  # the filter text was cleared again
    # One lap over the three "United ..." options, then the filter is cleared. Never Tab.
    assert _nav_keys(page) == ["ArrowDown"] * 3
    page.locator("#first_name").focus()  # blurring still selects nothing
    assert _single_value(page) is None
    _assert_inert(page)


# --- react-select that ignores fill(): opened with ArrowDown, typed key by key ----

# The real Greenhouse Country: fill() leaves its menu closed with nothing focused.
# It opens on ArrowDown and filters as keys are typed (keys_only=True: the page
# honors an input event only while a key is down, and puts back any other).
_KEYS_ONLY_COUNTRIES = [
    "Afghanistan +93",
    "Canada +1",
    "United Kingdom +44",
    "United States +1",
    "United States Minor Outlying Islands +1",
]


@pytest.fixture
def quick_polls(monkeypatch):
    """The wait for a first focused option is 3 s on a real page; these pages
    either show their menu at once or never will."""
    monkeypatch.setattr(fill, "_OPTION_POLL_S", 0.01)


def _typed_by_key(answer: str) -> list[str]:
    """The keys of a fill that had to type `answer`: Playwright's Delete (the
    empty fill that drops what the ignored fill left), ArrowDown (opens the
    menu), the answer's characters, then Tab."""
    return ["Delete", "ArrowDown", *answer, "Tab"]


def test_simulation_control_a_keys_only_menu_ignores_fill_and_follows_real_keys(page):
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, keys_only=True))
    box = page.locator("#country")
    box.fill("United States")
    assert page.evaluate("window.__ignored") == ["United States"]
    assert page.input_value("#country") == ""
    assert box.get_attribute("aria-expanded") == "false"
    assert page.locator("#country-menu").count() == 0
    box.press("ArrowDown")
    assert box.get_attribute("aria-expanded") == "true"
    box.press_sequentially("United States", delay=0)
    assert page.input_value("#country") == "United States"
    assert page.locator(".select__option--is-focused").text_content() == "United States +1"
    assert page.evaluate("window.__selected") is None  # typing only filters


def test_simulation_control_a_space_in_an_empty_open_box_picks_the_focused_option(page):
    """The react-select behaviour the typed answer must never trigger."""
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, keys_only=True))
    box = page.locator("#country")
    box.press("ArrowDown")
    box.press(" ")
    assert page.evaluate("window.__selected") == "Afghanistan +93"


def test_simulation_control_swallowed_keystrokes_leave_the_box_empty(page):
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, keys_only=True, swallow=6))
    box = page.locator("#country")
    box.press("ArrowDown")
    box.press_sequentially("United", delay=0)
    assert page.input_value("#country") == ""
    box.press_sequentially("Un", delay=0)  # the page has lost its six: these land
    assert page.input_value("#country") == "Un"


def test_the_combobox_state_reports_whether_its_menu_is_open(page):
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, keys_only=True))
    box = page.locator("#country")
    assert fill._detect_react_select(box)["expanded"] is False
    assert fill._menu_is_closed(box) is True
    box.press("ArrowDown")
    assert fill._detect_react_select(box)["expanded"] is True
    assert fill._menu_is_closed(box) is False
    box.evaluate("el => el.removeAttribute('aria-expanded')")
    assert fill._detect_react_select(box)["expanded"] is None
    assert fill._menu_is_closed(box) is False  # silence is not a closed menu


@pytest.mark.parametrize("variant", _MENU_VARIANTS)
def test_react_select_that_ignores_fill_is_opened_and_typed_into_key_by_key(
    page, fast_waits, quick_polls, variant
):
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, keys_only=True, **variant))
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.value == "United States"
    assert outcome.readback == "United States +1"
    assert _single_value(page) == "United States +1"
    assert page.evaluate("window.__selected") == "United States +1"
    assert page.locator("input[class*=requiredInput]").count() == 0
    assert page.evaluate("window.__ignored") == ["United States"]  # fill() was tried first
    assert _keys(page) == _typed_by_key("United States")
    _assert_inert(page, typed="United States")


def test_the_answer_is_typed_at_a_persons_pace(page, fast_waits, quick_polls):
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, keys_only=True))
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.committed is True
    stamps = page.evaluate("window.__keyTimes")
    typed = [t for key, t in stamps if len(key) == 1]
    assert len(typed) == len("United States")
    # Each key waits fill._KEY_DELAY_MS before the next. Judge the whole span, not
    # the gaps, and allow a lot of slack for jitter. The first floor is a person's
    # pace whatever the constant is tuned to; the second ties it to the constant.
    span, gaps = typed[-1] - typed[0], len(typed) - 1
    assert span >= 0.7 * gaps * 20
    assert span >= 0.7 * gaps * fill._KEY_DELAY_MS


def test_the_typed_answer_goes_in_even_when_the_first_keystrokes_are_lost(
    page, fast_waits, quick_polls
):
    """Two lost keystrokes ("Un"): the box still holds text before the Space, and
    the option left focused is what decides, not what was typed."""
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, keys_only=True, swallow=2))
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.readback == "United States +1"
    assert page.evaluate("window.__selected") == "United States +1"
    _assert_inert(page, typed="United States")


def test_a_space_is_never_typed_when_the_first_word_did_not_land(page, fast_waits, quick_polls):
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, keys_only=True, swallow=len("United")))
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'United States'"
    assert outcome.readback == ""
    assert " " not in _keys(page)  # a Space here would have picked 'Afghanistan +93'
    assert _keys(page) == ["Delete", "ArrowDown", *"United", "Delete"]
    assert page.evaluate("window.__selected") is None
    assert _single_value(page) is None
    assert page.locator("input[class*=requiredInput]").count() == 1
    assert page.input_value("#country") == ""
    _assert_inert(page, typed="United")


def test_a_filter_matching_nothing_commits_nothing_and_leaves_the_input_empty(
    page, fast_waits, quick_polls
):
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, keys_only=True))
    [outcome] = fill.apply_resolutions(page, [_country("Narnia")])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'Narnia'"
    assert outcome.readback == ""
    assert page.evaluate("window.__selected") is None
    assert _single_value(page) is None
    assert page.locator("input[class*=requiredInput]").count() == 1
    assert page.input_value("#country") == ""  # the typed filter was cleared again
    # Typed, found nothing, cleared: no Tab and no Enter.
    assert _keys(page) == ["Delete", "ArrowDown", *"Narnia", "Delete"]
    page.locator("#first_name").focus()  # blurring still selects nothing
    assert _single_value(page) is None
    assert page.evaluate("window.__selected") is None
    _assert_inert(page, typed="Narnia")


def test_a_typed_answer_that_only_prefixes_longer_options_is_never_committed(
    page, fast_waits, quick_polls
):
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, keys_only=True))
    [outcome] = fill.apply_resolutions(page, [_country("United")])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'United'"
    assert page.evaluate("window.__selected") is None
    assert _single_value(page) is None
    assert page.input_value("#country") == ""
    # Open the menu and type, then one lap over the three "United ..." options. Never Tab.
    assert _keys(page) == ["Delete", "ArrowDown", *"United", *["ArrowDown"] * 3, "Delete"]
    _assert_inert(page, typed="United")


def test_a_menu_that_takes_fill_is_never_typed_into(page, fast_waits, quick_polls):
    """Control for the fallback: a page whose fill() works sees only fill and Tab."""
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES))
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.committed is True
    assert outcome.readback == "United States +1"
    assert _keys(page) == ["Tab"]
    _assert_inert(page)


def test_an_open_menu_that_lists_nothing_is_never_typed_into(page, fast_waits, quick_polls):
    """'No options' is an open menu, not a closed one: the fill() worked and found
    nothing, so typing the same answer again would be noise, not a retry."""
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES))
    [outcome] = fill.apply_resolutions(page, [_country("Narnia")])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'Narnia'"
    assert _keys(page) == ["Delete"]  # only the cleanup fill(""): no ArrowDown, no character
    assert page.evaluate("window.__selected") is None
    assert page.input_value("#country") == ""
    _assert_inert(page)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("United\nStates", id="newline"),
        pytest.param("United\r\nStates", id="crlf"),
        pytest.param("United\tStates", id="tab"),
        pytest.param("United States\n", id="trailing-newline"),
        pytest.param("United​States", id="zero-width-space"),
        pytest.param("United States", id="line-separator"),
    ],
)
def test_a_stored_answer_holding_a_control_character_types_nothing(
    page, fast_waits, quick_polls, answer
):
    """Typed key by key a newline is Enter (it can submit the form) and a tab
    character is Tab. 'United States +1' is one whitespace-collapse away, and
    still nothing may be typed: not the ArrowDown that opens the menu, not one
    character, not a Tab."""
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, keys_only=True))
    [outcome] = fill.apply_resolutions(page, [_country(answer)])
    assert outcome.committed is False
    assert outcome.error == f"no option exactly matching {answer!r}"
    assert outcome.readback == ""
    # Only Playwright's own Delete, from the cleanup fill(""): no key of ours at all.
    assert _keys(page) == ["Delete"]
    assert "Enter" not in _keys(page) and "Tab" not in _keys(page)
    assert page.evaluate("window.__selected") is None
    assert _single_value(page) is None
    assert page.input_value("#country") == ""
    assert page.evaluate("window.__submits") == 0
    assert page.evaluate("window.__clicks") == []
    _assert_inert(page)


# --- react-select that shows a pick as a flag and its dial code, never its name ----

# Greenhouse's phone-block Country draws the committed option as
# `<div class="iti__flag iti__us"></div><span>+1</span>` and leaves the input
# empty: the page never shows "United States" after the pick. shows="dial-code"
# draws the picked option's own code; any other string is the code drawn whatever
# was picked (a page that disagrees with the pick), "" is a pick drawn with no code.


def test_simulation_control_a_phone_country_shows_a_flag_and_a_dial_code_after_a_pick(page):
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, keys_only=True, shows="dial-code"))
    box = page.locator("#country")
    box.press("ArrowDown")
    box.press_sequentially("United States", delay=0)
    box.press("Tab")
    assert page.evaluate("window.__selected") == "United States +1"
    assert page.input_value("#country") == ""
    assert page.locator(".select__single-value > div.iti__flag").count() == 1
    assert page.locator(".select__single-value > span").text_content() == "+1"
    assert "United" not in page.locator("#country-shell").inner_text()
    state = fill._detect_react_select(box)
    assert state["value"] == "+1"
    assert state["expanded"] is False
    assert fill._read_back_state(page, _country("United States").field) == ("+1", True)
    # The read-back alone is no evidence of the country: the failure being fixed.
    assert fill._matches("United States", "+1", True) is False


@pytest.mark.parametrize("variant", _MENU_VARIANTS)
def test_a_pick_that_shows_only_a_flag_and_its_dial_code_is_verified(
    page, fast_waits, quick_polls, variant
):
    page.set_content(
        _select_page(_KEYS_ONLY_COUNTRIES, keys_only=True, shows="dial-code", **variant)
    )
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.value == "United States"
    assert outcome.readback == "+1"
    assert outcome.picked == "United States +1"
    # What carries the verdict is the pick: the read-back on its own never would.
    assert fill._matches(outcome.value, outcome.readback, True) is False
    assert page.evaluate("window.__selected") == "United States +1"
    assert _single_value(page) == "+1"
    assert page.locator("input[class*=requiredInput]").count() == 0
    # One pass only: the verify pass took the read-back as it stood and picked nothing
    # again (a second pass would be a second Tab).
    assert _keys(page) == _typed_by_key("United States")
    _assert_inert(page, typed="United States")


@pytest.mark.parametrize("variant", _MENU_VARIANTS)
def test_a_pick_through_a_working_fill_that_shows_only_its_dial_code_is_verified(
    page, fast_waits, quick_polls, variant
):
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, shows="dial-code", **variant))
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.readback == "+1"
    assert outcome.picked == "United States +1"
    assert page.evaluate("window.__selected") == "United States +1"
    assert _keys(page) == ["Tab"]
    _assert_inert(page)


@pytest.mark.parametrize(
    "shown",
    [
        pytest.param("+44", id="another-country-code"),
        pytest.param("+12", id="a-longer-code"),
        pytest.param("", id="no-code-at-all"),
    ],
)
def test_a_pick_that_shows_another_dial_code_or_none_is_not_verified(
    page, fast_waits, quick_polls, shown
):
    """The page disagrees with the option that was picked. The verify pass picks
    once more (the single retry), and what the page then shows still is not
    'United States +1': not committed."""
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, keys_only=True, shows=shown))
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.committed is False
    assert outcome.error == ""
    assert outcome.readback == shown
    assert outcome.picked == "United States +1"  # the retry's own pick
    assert page.evaluate("window.__selected") == "United States +1"
    # Two full passes, each its own open, type and Tab: one try and the one retry.
    assert _keys(page) == _typed_by_key("United States") * 2
    _assert_inert(page, typed="United States")


def test_a_page_already_showing_a_dial_code_is_picked_again_for_another_country(
    page, fast_waits, quick_polls
):
    """'+1' is Canada as much as the United States: alone it is never taken for
    the answer, so a later run does not leave the wrong country in place."""
    page.set_content(_select_page(_KEYS_ONLY_COUNTRIES, keys_only=True, shows="dial-code"))
    [canada] = fill.apply_resolutions(page, [_country("Canada")])
    assert canada.committed is True
    assert canada.picked == "Canada +1"
    assert page.evaluate("window.__selected") == "Canada +1"
    assert _single_value(page) == "+1"
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.picked == "United States +1"
    assert page.evaluate("window.__selected") == "United States +1"
    _assert_inert(page, typed="CanadaUnited States")


def test_a_dial_code_left_by_an_earlier_pick_never_verifies_a_fill_that_picked_nothing(
    page, fast_waits, quick_polls
):
    page.set_content(
        _select_page(["Canada +1", "United Kingdom +44"], keys_only=True, shows="dial-code")
    )
    [canada] = fill.apply_resolutions(page, [_country("Canada")])
    assert canada.committed is True
    [outcome] = fill.apply_resolutions(page, [_country("United States")])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'United States'"
    assert outcome.picked == ""
    assert page.evaluate("window.__selected") == "Canada +1"  # nothing was picked again
    assert _single_value(page) == "+1"
    _assert_inert(page, typed="CanadaUnited States")


# --- a stored place the menu spells another way: location and country --------------

# A location or country answer also takes the ONE option that spells the same place
# another way (solve.same_place): "Atlanta, GA, USA" for "Atlanta, Georgia", "United
# States of America" for "United States". It stays equality: never "United States
# Minor Outlying Islands", never the country "Georgia", never "Atlanta, TX, USA".
# Tab still goes only to the focused option, and only while no other option in the
# menu spells the place too, so a menu that is ambiguous commits nothing.

_ATLANTAS = ["Athens, GA, USA", "Atlanta, GA, USA", "Atlanta, TX, USA"]
_AMERICAS = ["Canada", "United States of America", "United States Minor Outlying Islands"]
# A test that has to ArrowDown through a menu must not race a render slower than the
# poll that quick_polls shortens, so those use the menus that render at once.
_PROMPT_MENUS = [v for v in _MENU_VARIANTS if "render_delay_ms" not in v.values[0]]


def _place(value: str, key: str = "location") -> Resolution:
    label = "Country" if key == "country" else "Location (City)"
    spec = FieldSpec(
        selector="#country", kind="text", label=label, elem_id="country", required=True
    )
    return Resolution(field=spec, semantic_key=key, action="fill", value=value)


def test_simulation_control_typing_the_whole_stored_city_lists_no_option(page):
    """Why the first word that shows the city is taken: the next word filters it away."""
    page.set_content(_select_page(_ATLANTAS, keys_only=True))
    box = page.locator("#country")
    box.press("ArrowDown")
    box.press_sequentially("Atlanta,", delay=0)
    assert page.locator(".select__option--is-focused").text_content() == "Atlanta, GA, USA"
    box.press_sequentially(" Georgia", delay=0)
    assert page.locator(".select__option").count() == 0
    assert page.locator(".select__menu-notice--no-options").count() == 1


@pytest.mark.parametrize("variant", _MENU_VARIANTS)
def test_the_options_of_the_open_menu_are_read_from_the_menu_around_the_focused_one(
    page, quick_polls, variant
):
    """Not from the 244 hidden [role=option] items of the phone list on the page."""
    page.set_content(_select_page(_ATLANTAS, keys_only=True, **variant))
    box = page.locator("#country")
    assert fill._menu_options(box) is None  # nothing focused: no menu to read
    box.press("ArrowDown")
    assert fill._wait_for_focused_option(box) == "Athens, GA, USA"
    assert fill._menu_options(box) == _ATLANTAS


@pytest.mark.parametrize("variant", _MENU_VARIANTS)
def test_a_stored_city_is_picked_from_a_menu_that_spells_it_another_way(
    page, fast_waits, quick_polls, variant
):
    page.set_content(_select_page(_ATLANTAS, keys_only=True, **variant))
    [outcome] = fill.apply_resolutions(page, [_place("Atlanta, Georgia")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.value == "Atlanta, Georgia"
    assert outcome.readback == "Atlanta, GA, USA"
    assert outcome.picked == "Atlanta, GA, USA"
    assert _single_value(page) == "Atlanta, GA, USA"
    assert page.evaluate("window.__selected") == "Atlanta, GA, USA"
    assert page.locator("input[class*=requiredInput]").count() == 0
    # Opened, typed the first word, Tab: the second word would filter the city away.
    assert _keys(page) == ["Delete", "ArrowDown", *"Atlanta,", "Tab"]
    _assert_inert(page, typed="Atlanta,")


@pytest.mark.parametrize("variant", _MENU_VARIANTS)
def test_a_stored_country_is_picked_from_a_menu_that_spells_it_another_way(
    page, fast_waits, quick_polls, variant
):
    page.set_content(_select_page(_AMERICAS, keys_only=True, **variant))
    [outcome] = fill.apply_resolutions(page, [_place("United States", "country")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.readback == "United States of America"
    assert outcome.picked == "United States of America"
    assert page.evaluate("window.__selected") == "United States of America"
    assert page.locator("input[class*=requiredInput]").count() == 0
    # "United" lists the country first; "United States" would list it just the same,
    # but the second word is never needed.
    assert _keys(page) == ["Delete", "ArrowDown", *"United", "Tab"]
    _assert_inert(page, typed="United")


@pytest.mark.parametrize("variant", _MENU_VARIANTS)
def test_a_menu_that_takes_fill_is_arrowed_past_the_outlying_islands_to_the_country(
    page, fast_waits, variant
):
    options = ["Canada", "United States Minor Outlying Islands", "United States of America"]
    page.set_content(_select_page(options, **variant))
    [outcome] = fill.apply_resolutions(page, [_place("United States", "country")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.picked == "United States of America"
    assert _single_value(page) == "United States of America"
    assert page.evaluate("window.__selected") == "United States of America"
    # The longer name is focused first: one ArrowDown past it, then Tab.
    assert _keys(page) == ["ArrowDown", "Tab"]
    _assert_inert(page)


def test_a_menu_that_takes_fill_commits_the_one_option_that_holds_the_stored_city(
    page, fast_waits, quick_polls
):
    page.set_content(_select_page(["Atlanta, Georgia, USA", "Atlanta, TX, USA"]))
    [outcome] = fill.apply_resolutions(page, [_place("Atlanta, Georgia")])
    assert outcome.committed is True
    assert outcome.picked == "Atlanta, Georgia, USA"
    assert page.evaluate("window.__selected") == "Atlanta, Georgia, USA"
    assert _keys(page) == ["Tab"]
    _assert_inert(page)


def test_a_menu_that_filters_by_the_whole_answer_does_not_list_a_city_spelled_otherwise(
    page, fast_waits, quick_polls
):
    """fill() types the answer as stored and the menu lists only the options that
    contain it: 'Atlanta, GA, USA' is not among them, so nothing is committed. The
    typed fallback is for a menu that stayed closed, and this one is open."""
    page.set_content(_select_page(_ATLANTAS))
    [outcome] = fill.apply_resolutions(page, [_place("Atlanta, Georgia")])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'Atlanta, Georgia'"
    assert outcome.picked == ""
    assert page.evaluate("window.__selected") is None
    assert _keys(page) == ["Delete"]  # only the cleanup fill(""): no ArrowDown, no Tab
    _assert_inert(page)


@pytest.mark.parametrize("variant", _PROMPT_MENUS)
def test_an_option_equal_to_the_answer_wins_over_one_that_only_spells_it(
    page, fast_waits, quick_polls, variant
):
    options = ["United States of America", "United States"]
    page.set_content(_select_page(options, keys_only=True, **variant))
    [outcome] = fill.apply_resolutions(page, [_place("United States", "country")])
    assert outcome.committed is True
    assert outcome.picked == "United States"
    assert page.evaluate("window.__selected") == "United States"
    assert _keys(page) == ["Delete", "ArrowDown", *"United States", "ArrowDown", "Tab"]
    _assert_inert(page, typed="United States")


@pytest.mark.parametrize("variant", _MENU_VARIANTS)
def test_a_phone_country_that_spells_the_country_another_way_is_picked_and_verified(
    page, fast_waits, quick_polls, variant
):
    options = ["Canada +1", "United States of America +1", "United States Minor Outlying Islands +1"]
    page.set_content(_select_page(options, keys_only=True, shows="dial-code", **variant))
    [outcome] = fill.apply_resolutions(page, [_place("United States", "country")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.readback == "+1"
    assert outcome.picked == "United States of America +1"
    assert page.evaluate("window.__selected") == "United States of America +1"
    # One pass only: the verify pass took the pick and its dial code as they stood.
    assert _keys(page) == ["Delete", "ArrowDown", *"United", "Tab"]
    _assert_inert(page, typed="United")


def test_a_readonly_combobox_is_arrowed_to_the_option_that_spells_the_country(page, fast_waits):
    page.set_content(_select_page(_AMERICAS, searchable=False))
    [outcome] = fill.apply_resolutions(page, [_place("United States", "country")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.picked == "United States of America"
    assert page.evaluate("window.__selected") == "United States of America"
    assert _keys(page) == ["ArrowDown", "ArrowDown", "Tab"]  # open, past Canada, pick
    _assert_inert(page)


@pytest.mark.parametrize(
    ("options", "answer", "key"),
    [
        pytest.param(
            ["Atlanta, GA", "Atlanta, GA, USA", "Atlanta, TX, USA"],
            "Atlanta, Georgia",
            "location",
            id="city-two-spellings-listed-from-the-start",
        ),
        pytest.param(
            ["Canada", "USA", "United States of America", "United States Minor Outlying Islands"],
            "United States",
            "country",
            id="country-one-of-two-spellings-filtered-away-by-typing",
        ),
    ],
)
@pytest.mark.parametrize("variant", _PROMPT_MENUS)
def test_two_options_that_spell_the_place_commit_nothing(
    page, fast_waits, quick_polls, variant, options, answer, key
):
    """Typing narrows the menu, so the second spelling can be gone by the time the
    first is focused: the menu as it was listed before typing still counts."""
    page.set_content(_select_page(options, keys_only=True, **variant))
    [outcome] = fill.apply_resolutions(page, [_place(answer, key)])
    assert outcome.committed is False
    assert outcome.error == (
        f"no option exactly matching {answer!r} (two or more options spell it)"
    )
    assert outcome.picked == ""
    assert outcome.readback == ""
    assert page.evaluate("window.__selected") is None
    assert _single_value(page) is None
    assert page.input_value("#country") == ""
    assert "Tab" not in _keys(page)
    _assert_inert(page, typed=answer)


def test_two_options_that_hold_the_stored_city_commit_nothing_on_a_menu_that_takes_fill(
    page, fast_waits, quick_polls
):
    options = ["Atlanta, Georgia, USA", "Atlanta, Georgia, United States", "Atlanta, TX, USA"]
    page.set_content(_select_page(options))
    [outcome] = fill.apply_resolutions(page, [_place("Atlanta, Georgia")])
    assert outcome.committed is False
    assert outcome.error == (
        "no option exactly matching 'Atlanta, Georgia' (two or more options spell it)"
    )
    assert outcome.picked == ""
    assert page.evaluate("window.__selected") is None
    assert _single_value(page) is None
    assert "Tab" not in _keys(page)
    _assert_inert(page)


@pytest.mark.parametrize("keys_only", [True, False], ids=["typed-key-by-key", "fill-works"])
@pytest.mark.parametrize(
    ("options", "answer", "key"),
    [
        pytest.param(
            ["Athens, GA, USA", "Atlanta, TX, USA"],
            "Atlanta, Georgia",
            "location",
            id="athens-and-atlanta-texas-for-atlanta-georgia",
        ),
        pytest.param(
            ["Atlanta, GA, USA", "Atlanta, TX, USA"],
            "Atlanta, Texas",
            "location",
            id="atlanta-georgia-is-not-atlanta-texas",
        ),
        pytest.param(
            ["Georgia", "United States Minor Outlying Islands"],
            "United States",
            "country",
            id="the-country-georgia-and-the-outlying-islands-for-the-united-states",
        ),
        pytest.param(
            ["Atlanta, Georgia, Fulton, USA", "Georgia, USA"],
            "Atlanta, Georgia",
            "location",
            id="a-longer-place-is-not-the-city",
        ),
    ],
)
def test_a_different_place_is_never_committed_for_the_stored_one(
    page, fast_waits, quick_polls, keys_only, options, answer, key
):
    page.set_content(_select_page(options, keys_only=keys_only))
    [outcome] = fill.apply_resolutions(page, [_place(answer, key)])
    assert outcome.committed is False
    assert outcome.error == f"no option exactly matching {answer!r}"
    assert outcome.picked == ""
    assert page.evaluate("window.__selected") is None
    assert _single_value(page) is None
    assert page.input_value("#country") == ""
    assert "Tab" not in _keys(page)
    _assert_inert(page, typed=answer if keys_only else "")


def test_a_key_that_is_not_a_place_never_takes_another_spelling(page, fast_waits, quick_polls):
    page.set_content(_select_page(_AMERICAS))
    [outcome] = fill.apply_resolutions(page, [_place("United States", "school")])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'United States'"
    assert outcome.picked == ""
    assert page.evaluate("window.__selected") is None
    assert "Tab" not in _keys(page)
    _assert_inert(page)


def test_a_menu_whose_options_cannot_be_found_is_never_taken_for_a_spelling(
    page, fast_waits, quick_polls
):
    """Nothing says that no second option spells the country when the menu around the
    focused option cannot be located, so the option that spells it is not taken."""
    html = (
        _select_page(_AMERICAS, keys_only=True)
        .replace("menu.className = 'select__menu';", "menu.className = 'shell';")
        .replace("list.className = 'select__menu-list';", "list.className = 'rows';")
        .replace("list.setAttribute('role', 'listbox');", "")
    )
    page.set_content(html)
    assert page.locator("#country-menu").count() == 0
    box = page.locator("#country")
    box.press("ArrowDown")
    assert fill._wait_for_focused_option(box) == "Canada"
    assert fill._menu_options(box) is None  # the control: no container to scope the read
    box.press("Escape")
    before = len(_keys(page))  # the control's own two keys are not fill's
    [outcome] = fill.apply_resolutions(page, [_place("United States", "country")])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'United States'"
    assert outcome.picked == ""
    assert page.evaluate("window.__selected") is None
    sent = _keys(page)[before:]
    assert "Tab" not in sent
    assert set(sent) <= _ALLOWED_KEYS | set("United States"), sent
    assert page.evaluate("window.__clicks") == []
    assert page.evaluate("window.__submits") == 0


# --- a residence question whose menu lists countries -----------------------------------

# Greenhouse's "Where are you currently located?*" is a react-select of countries, so
# the stored city ("Atlanta, Georgia") has no option in it. For that question alone
# (keys.asks_where_you_are) solve.resolve also carries the stored country in
# Resolution.fallback_value, and fill tries it in the same box, but only when the menu
# has no option at all for the city: not when two options spell it, not for a
# multi-select, not for any other problem. The keys stay ArrowDown, the answer's own
# characters and Tab, and nothing but "United States of America" is ever the pick.

_CITY_AND_COUNTRY = {"location": "Atlanta, Georgia", "country": "United States"}
_WORLD = [
    "Afghanistan",
    "Canada",
    "Georgia",
    "United States Minor Outlying Islands",
    "United States of America",
    "Zimbabwe",
]
_TYPED_BOTH = "Atlanta, GeorgiaUnited States"  # the characters of the city, then the country


def _residence(label: str = "Where are you currently located?*", answers=None) -> Resolution:
    """What solve.resolve gives this question: the Resolution the lane hands to fill.
    The page's input is #country, but the field is scanned with no id of its own (a
    Greenhouse question's is a number), so only the label says what it asks."""
    spec = FieldSpec(selector="#country", kind="text", label=label, required=True)
    [res] = solve.resolve([spec], answers or _CITY_AND_COUNTRY, resume_path=None)
    return res


def test_a_residence_question_carries_the_country_and_other_labels_do_not():
    res = _residence()
    assert (res.semantic_key, res.action) == ("location", "fill")
    assert (res.value, res.fallback_value) == ("Atlanta, Georgia", "United States")
    other = _residence("Location (City)*")
    assert (other.value, other.fallback_value) == ("Atlanta, Georgia", None)
    # A question about the job's own place names no key: nothing is filled or tried.
    for label in ("Which location are you applying for?", "Preferred location"):
        other = _residence(label)
        assert (other.action, other.value, other.fallback_value) == ("ask", None, None), label
    assert _residence(answers={"location": "Atlanta, Georgia"}).fallback_value is None


@pytest.mark.parametrize("keys_only", [False, True], ids=["fill-works", "typed-key-by-key"])
@pytest.mark.parametrize("variant", _PROMPT_MENUS)
def test_a_menu_of_countries_takes_the_stored_country_for_a_residence_question(
    page, fast_waits, quick_polls, variant, keys_only
):
    page.set_content(_select_page(_WORLD, keys_only=keys_only, **variant))
    [outcome] = fill.apply_resolutions(page, [_residence()])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.value == "United States"  # the country is what was filled and verified
    assert outcome.readback == "United States of America"
    assert outcome.picked == "United States of America"
    assert page.evaluate("window.__selected") == "United States of America"
    assert _single_value(page) == "United States of America"
    assert page.locator("input[class*=requiredInput]").count() == 0
    keys = _keys(page)
    assert keys.count("Tab") == 1 and keys[-1] == "Tab"  # one commit; the verify pass kept it
    _assert_inert(page, typed=_TYPED_BOTH if keys_only else "")


def test_the_country_is_reached_past_the_outlying_islands_with_the_usual_keys(
    page, fast_waits, quick_polls
):
    page.set_content(_select_page(_WORLD))
    [outcome] = fill.apply_resolutions(page, [_residence()])
    assert outcome.committed is True
    # The city's cleanup (Delete), then past "...Minor Outlying Islands" to the country.
    assert _keys(page) == ["Delete", "ArrowDown", "Tab"]
    _assert_inert(page)


@pytest.mark.parametrize("keys_only", [False, True], ids=["fill-works", "typed-key-by-key"])
def test_a_long_menu_of_countries_takes_the_stored_country(
    page, fast_waits, quick_polls, keys_only
):
    """The WorldQuant shape: some two hundred countries. The city's failed try leaves a
    menu that ignores fill() open on every option; the country is then typed key by key
    from a closed menu, not scanned for, far beyond the arrow presses a scan allows."""
    countries = [f"Country {number:03d}" for number in range(180)]
    countries += ["United States Minor Outlying Islands", "United States of America", "Zambia"]
    page.set_content(_select_page(countries, keys_only=keys_only))
    [outcome] = fill.apply_resolutions(page, [_residence()])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.value == "United States"
    assert page.evaluate("window.__selected") == "United States of America"
    assert _keys(page).count("Tab") == 1
    _assert_inert(page, typed=_TYPED_BOTH if keys_only else "")


def test_a_menu_that_cannot_be_typed_into_takes_the_country_by_arrowing(
    page, fast_waits, quick_polls
):
    page.set_content(_select_page(_WORLD, searchable=False))
    [outcome] = fill.apply_resolutions(page, [_residence()])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.value == "United States"
    assert outcome.picked == "United States of America"
    assert page.evaluate("window.__selected") == "United States of America"
    assert _keys(page).count("Tab") == 1
    assert set(_keys(page)) <= {"ArrowDown", "Tab"}  # nothing typed: the box is read-only
    _assert_inert(page)


def test_the_verify_pass_checks_the_country_that_was_filled_not_the_city(
    page, fast_waits, quick_polls, monkeypatch
):
    page.set_content(_select_page(_WORLD))
    res = _residence()
    outcome = fill.apply_one(page, res, settle_ms=0)
    assert (outcome.committed, outcome.value) == (True, "United States")
    monkeypatch.setattr(fill, "_apply_one", lambda *args, **kwargs: pytest.fail("retried"))
    verified = fill._verify_one(page, res, outcome, 0)
    assert (verified.committed, verified.error, verified.value) == (True, "", "United States")
    assert verified.readback == "United States of America"


@pytest.mark.parametrize(
    ("options", "keys_only"),
    [
        pytest.param(
            ["Atlanta, Georgia, USA", "Atlanta, Georgia, United States", "United States of America"],
            False,
            id="fill-works",
        ),
        pytest.param(
            ["Atlanta, GA", "Atlanta, GA, USA", "Atlanta, TX, USA", "United States of America"],
            True,
            id="typed-key-by-key",
        ),
    ],
)
def test_a_menu_with_two_spellings_of_the_city_commits_nothing_and_never_tries_the_country(
    page, fast_waits, quick_polls, options, keys_only
):
    page.set_content(_select_page(options, keys_only=keys_only))
    [outcome] = fill.apply_resolutions(page, [_residence()])
    assert outcome.committed is False
    assert outcome.error == (
        "no option exactly matching 'Atlanta, Georgia' (two or more options spell it)"
    )
    assert outcome.value == "Atlanta, Georgia"
    assert outcome.picked == ""
    # "United States of America" is in the menu: it would be the pick if it were tried.
    assert page.evaluate("window.__selected") is None
    assert _single_value(page) is None
    assert "Tab" not in _keys(page)
    _assert_inert(page, typed="Atlanta, Georgia" if keys_only else "")


@pytest.mark.parametrize("keys_only", [False, True], ids=["fill-works", "typed-key-by-key"])
@pytest.mark.parametrize(
    "options",
    [
        pytest.param(
            ["Athens, GA, USA", "Boston, MA, USA", "Denver, CO, USA"], id="cities-without-atlanta"
        ),
        pytest.param(
            ["Athens, GA, USA", "United States - Remote", "United States Minor Outlying Islands"],
            id="cities-and-near-misses-of-the-country",
        ),
        pytest.param(["Georgia", "Canada"], id="the-country-georgia-and-canada"),
        pytest.param(
            ["Georgia", "United States Minor Outlying Islands"], id="georgia-and-the-islands"
        ),
    ],
)
def test_a_menu_with_neither_the_city_nor_the_country_commits_nothing(
    page, fast_waits, quick_polls, keys_only, options
):
    page.set_content(_select_page(options, keys_only=keys_only))
    [outcome] = fill.apply_resolutions(page, [_residence()])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'Atlanta, Georgia'"  # the city's problem
    assert outcome.value == "Atlanta, Georgia"
    assert outcome.picked == ""
    assert page.evaluate("window.__selected") is None
    assert _single_value(page) is None
    assert page.input_value("#country") == ""
    assert "Tab" not in _keys(page)
    _assert_inert(page, typed=_TYPED_BOTH if keys_only else "")


@pytest.mark.parametrize("keys_only", [False, True], ids=["fill-works", "typed-key-by-key"])
@pytest.mark.parametrize(
    "label",
    [
        "Location (City)*",
    ],
)
def test_a_label_that_does_not_ask_where_you_are_never_tries_the_country(
    page, fast_waits, quick_polls, keys_only, label
):
    res = _residence(label)
    assert res.fallback_value is None
    page.set_content(_select_page(_WORLD, keys_only=keys_only))
    [outcome] = fill.apply_resolutions(page, [res])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'Atlanta, Georgia'"
    assert outcome.value == "Atlanta, Georgia"
    assert page.evaluate("window.__selected") is None  # "United States of America" is there
    assert _single_value(page) is None
    assert "Tab" not in _keys(page)
    _assert_inert(page, typed="Atlanta, Georgia" if keys_only else "")


@pytest.mark.parametrize(
    "label",
    [
        "Which location are you applying for?",
        "Preferred location",
        "Office location",
    ],
)
def test_a_label_about_the_jobs_place_is_asked_with_no_city_and_no_country(label):
    # It names no key, so nothing reaches fill: no stored city to type, and no stored
    # country to fall back to.
    res = _residence(label)
    assert (res.action, res.value, res.fallback_value) == ("ask", None, None)


def test_a_multi_select_never_falls_back_to_the_country(page, fast_waits):
    page.set_content(_select_page(_WORLD, multi=True))
    [outcome] = fill.apply_resolutions(page, [_residence()])
    assert outcome.committed is False
    assert outcome.error == "multi-select combobox not supported"
    assert outcome.value == "Atlanta, Georgia"
    assert _keys(page) == []
    assert page.evaluate("window.__selected") is None
    _assert_inert(page)


def test_a_plain_text_input_takes_the_city_and_never_the_country(page, fast_waits):
    page.set_content(
        '<form><label for="where">Where are you currently located?*</label>'
        '<input id="where" type="text"></form>'
    )
    spec = FieldSpec(
        selector="#where", kind="text", label="Where are you currently located?*", required=True
    )
    [res] = solve.resolve([spec], _CITY_AND_COUNTRY, resume_path=None)
    assert res.fallback_value == "United States"
    [outcome] = fill.apply_resolutions(page, [res])
    assert (outcome.error, outcome.committed) == ("", True)
    assert outcome.value == "Atlanta, Georgia"
    assert page.input_value("#where") == "Atlanta, Georgia"


def test_a_fallback_is_ignored_for_a_field_that_is_not_a_place(page, fast_waits, quick_polls):
    """Only the location and country keys take another spelling, and only the location
    key carries a country: a Resolution for any other key never tries one."""
    page.set_content(_select_page(_WORLD))
    res = dataclasses.replace(_residence(), semantic_key="school")
    [outcome] = fill.apply_resolutions(page, [res])
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'Atlanta, Georgia'"
    assert page.evaluate("window.__selected") is None
    assert "Tab" not in _keys(page)
    _assert_inert(page)


# --- file upload --------------------------------------------------------------

_UPLOAD_PAGE = """
<!doctype html>
<html>
<head>
<style>
  /* Visually hidden, as on Greenhouse: still 1x1 px, so the field scan sees it. */
  #resume { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
            overflow: hidden; clip: rect(0 0 0 0); border: 0; }
</style>
</head>
<body>
<form id="app" action="/never-submitted" method="post">
  <div id="resume-field">
    <label for="resume">Resume/CV *</label>
    <input id="resume" name="resume" type="file" required>
  </div>
</form>
<script>
(function () {
  var MODE = __MODE__;
  window.__clicks = [];
  document.addEventListener('click', function (ev) {
    window.__clicks.push(ev.target.id || ev.target.tagName);
  }, true);
  function chip(file) {
    var el = document.createElement('div');
    el.className = 'chip';
    el.innerHTML = '<span class="filename"></span><button type="button" aria-label="Remove file">x</button>';
    el.querySelector('.filename').textContent = file.name;
    document.getElementById('resume-field').appendChild(el);
  }
  if (MODE === 'delegated-remove') {
    // React-style: one listener at the root, running after the input's own.
    document.addEventListener('change', function (ev) {
      if (ev.target.id !== 'resume') return;
      var file = ev.target.files[0];
      ev.target.remove();
      chip(file);
    });
  } else if (MODE === 'direct-remove') {
    document.getElementById('resume').addEventListener('change', function (ev) {
      var file = ev.target.files[0];
      ev.target.remove();
      chip(file);
    });
  } else {
    document.getElementById('resume').addEventListener('change', function (ev) {
      chip(ev.target.files[0]);
    });
  }
})();
</script>
</body></html>
"""


def _upload_page(mode: str) -> str:
    return _UPLOAD_PAGE.replace("__MODE__", json.dumps(mode))


def _resume_file(tmp_path: Path) -> Path:
    resume = tmp_path / "Alex_Rivera_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return resume


def _upload_resolution(page, resume: Path) -> Resolution:
    [spec] = [s for s in fill.scan_fields(page) if s.kind == "file"]
    return Resolution(field=spec, semantic_key="resume", action="upload", value=str(resume))


@pytest.mark.parametrize("mode", ["delegated-remove", "direct-remove", "kept"])
def test_upload_is_read_back_whether_or_not_the_page_removes_the_input(
    page, fast_waits, tmp_path, mode
):
    resume = _resume_file(tmp_path)
    page.set_content(_upload_page(mode))
    res = _upload_resolution(page, resume)
    started = time.monotonic()
    [outcome] = fill.apply_resolutions(page, [res])
    elapsed = time.monotonic() - started

    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.readback == resume.name
    removed = mode != "kept"
    assert (page.locator("#resume").count() == 0) is removed
    assert page.locator(".chip .filename").first.text_content() == resume.name
    record = page.evaluate("key => window[key]", fill.UPLOAD_RECORD_KEY)
    assert record == {"#resume": {"name": resume.name, "size": resume.stat().st_size, "count": 1}}
    assert page.evaluate("window.__clicks") == []
    # The old read-back waited out Playwright's 30 s auto-wait for the removed input.
    assert elapsed < 10


def test_upload_is_read_back_without_any_settle_time(page, fast_waits, tmp_path):
    """Read immediately after set_input_files: the page's handler may or may not
    have removed the input yet, and either way the file name comes back."""
    resume = _resume_file(tmp_path)
    page.set_content(_upload_page("delegated-remove"))
    res = _upload_resolution(page, resume)
    [outcome] = fill.apply_resolutions(page, [res], settle_ms=0)
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.readback == resume.name


# --- file inputs that are fully transparent: Lever's resume ---------------------

# Lever's resume field is a styled "ATTACH RESUME/CV" button with the real
# `input[type=file][name=resume]#resume-upload-input` laid over it as its click
# target: computed display block, visibility visible, a real rect, and opacity 0.
# The scan read that opacity as a honeypot and never returned the input, so every
# Lever posting blocked at the gate with "no resume upload was recorded". Only a
# file input is exempt from the opacity test; any other element at opacity 0 is
# still a honeypot, and display, visibility and size still apply to the file input.
_LEVER_RESUME_PAGE = """
<!doctype html>
<html>
<head>
<style>
  .resume-upload { position: relative; width: 240px; height: 44px; }
  .resume-upload .attach { display: block; width: 100%; height: 100%; }
  .resume-upload input[type=file] {
    position: absolute; left: 0; top: 0; width: 100%; height: 100%; opacity: 0;
    __FILE_CSS__
  }
</style>
</head>
<body>
<form id="app" action="/never-submitted" method="post">
  <label for="name">Full name ✱</label>
  <input id="name" name="name" type="text" required>
  <label for="resume-upload-input">Resume/CV ✱</label>
  <div class="resume-upload">
    <button type="button" class="attach">ATTACH RESUME/CV</button>
    <input type="file" id="resume-upload-input" name="resume" required>
  </div>
  <span id="file-name"></span>
  __EXTRA__
</form>
<script>
  window.__clicks = [];
  document.addEventListener('click', function (ev) {
    window.__clicks.push(ev.target.id || ev.target.tagName);
  }, true);
  document.getElementById('resume-upload-input').addEventListener('change', function (ev) {
    document.getElementById('file-name').textContent = ev.target.files[0].name;
  });
</script>
</body></html>
"""


def _lever_resume_page(file_css: str = "", extra: str = "") -> str:
    return _LEVER_RESUME_PAGE.replace("__FILE_CSS__", file_css).replace("__EXTRA__", extra)


def _layout_facts(page, selector: str) -> dict:
    """What the scan's visibility test reads about one element."""
    return page.evaluate(
        """(sel) => {
          const el = document.querySelector(sel);
          const style = getComputedStyle(el);
          const rect = el.getBoundingClientRect();
          return {display: style.display, visibility: style.visibility,
                  opacity: style.opacity, width: rect.width, height: rect.height};
        }""",
        selector,
    )


def test_scan_returns_levers_transparent_resume_input_as_a_file_field(page):
    page.set_content(_lever_resume_page())
    facts = _layout_facts(page, "#resume-upload-input")
    # The live Lever form: laid out, shown, and fully transparent.
    assert (facts["display"], facts["visibility"], facts["opacity"]) == ("block", "visible", "0")
    assert facts["width"] > 0 and facts["height"] > 0

    specs = {spec.selector: spec for spec in fill.scan_fields(page)}

    assert list(specs) == ["#name", "#resume-upload-input"]
    resume = specs["#resume-upload-input"]
    assert resume.kind == "file"
    assert resume.name == "resume"
    assert resume.elem_id == "resume-upload-input"
    assert resume.label == "Resume/CV ✱"
    assert resume.required is True
    assert specs["#name"].kind == "text"


def test_a_transparent_file_input_is_exempt_whatever_the_case_of_its_type(page):
    html = _lever_resume_page().replace('type="file" id=', 'type="FILE" id=')
    assert 'type="FILE"' in html
    page.set_content(html)
    assert _layout_facts(page, "#resume-upload-input")["opacity"] == "0"

    resume = {spec.selector: spec for spec in fill.scan_fields(page)}["#resume-upload-input"]

    assert resume.kind == "file"


_NOT_LAID_OUT = {
    "display-none": "display: none;",
    "visibility-hidden": "visibility: hidden;",
    "zero-size": "width: 0; height: 0;",
    "zero-width": "width: 0;",
    "zero-height": "height: 0;",
}


@pytest.mark.parametrize("opacity", ["0", "1"], ids=["opacity-0", "opacity-1"])
@pytest.mark.parametrize("layout", sorted(_NOT_LAID_OUT))
def test_a_file_input_that_is_not_laid_out_is_still_skipped(page, layout, opacity):
    page.set_content(_lever_resume_page(f"{_NOT_LAID_OUT[layout]} opacity: {opacity};"))
    facts = _layout_facts(page, "#resume-upload-input")
    assert facts["opacity"] == opacity
    assert (
        facts["display"] == "none"
        or facts["visibility"] == "hidden"
        or facts["width"] == 0
        or facts["height"] == 0
    ), facts

    assert [spec.selector for spec in fill.scan_fields(page)] == ["#name"]


_HONEYPOTS = {
    "text": '<input id="trap" name="trap" type="text" style="opacity: 0">',
    "typeless": '<input id="trap" name="trap" style="opacity: 0">',
    "email": '<input id="trap" name="trap" type="email" style="opacity: 0">',
    "textarea": '<textarea id="trap" name="trap" style="opacity: 0"></textarea>',
    "select": '<select id="trap" name="trap" style="opacity: 0"><option>Yes</option></select>',
    "checkbox": '<input id="trap" name="trap" type="checkbox" style="opacity: 0">',
    "radio": '<input id="trap" name="trap" type="radio" value="yes" style="opacity: 0">',
    "combobox": '<input id="trap" name="trap" type="text" role="combobox" style="opacity: 0">',
}


@pytest.mark.parametrize("trap", sorted(_HONEYPOTS))
def test_an_element_that_is_not_a_file_input_at_opacity_zero_is_still_a_honeypot(page, trap):
    page.set_content(_lever_resume_page(extra=_HONEYPOTS[trap]))
    facts = _layout_facts(page, "#trap")
    assert facts["opacity"] == "0"
    assert facts["width"] > 0 and facts["height"] > 0  # skipped for its opacity alone

    specs = fill.scan_fields(page)

    # Only the file input beside it is exempt.
    assert [spec.selector for spec in specs] == ["#name", "#resume-upload-input"]
    assert [spec.kind for spec in specs] == ["text", "file"]


def test_the_scan_and_hydration_scripts_share_one_visibility_test():
    for script in (fill._SCAN_JS, fill._HYDRATION_JS):
        assert script.count("function isVisible") == 1
        assert fill._IS_VISIBLE_JS in script


_SSR_PAGE = """
<!doctype html>
<html><body>
<script>window.__remixContext = {};</script>
<input id="name" name="name" type="text">
<div style="position: relative; width: 240px; height: 44px">
  <input id="resume" name="resume" type="file"
         style="position: absolute; left: 0; top: 0; width: 100%; height: 100%; opacity: 0">
</div>
<input id="trap" name="trap" type="text" style="opacity: 0">
<script>
  // React has reached the text field and nothing else.
  document.getElementById('name')['__reactProps$x'] = {};
</script>
</body></html>
"""


def test_hydration_counts_a_transparent_file_input_but_not_a_honeypot(page):
    page.set_content(_SSR_PAGE)
    # `resume` is a field React has not reached yet; `trap` is a honeypot nobody waits on.
    assert fill._hydration_state(page) == (True, 1)
    page.evaluate("document.getElementById('resume')['__reactProps$x'] = {}")
    assert fill._hydration_state(page) == (True, 0)


def test_levers_resume_is_scanned_uploaded_and_accepted_by_the_gate(page, fast_waits, tmp_path):
    """The original symptom end to end on a Lever-shaped page: the scan finds the
    resume input, the upload lands, and the gate's own resume check has nothing to
    block (it blocked with "no resume upload was recorded" while the scan missed it)."""
    resume = _resume_file(tmp_path)
    page.set_content(_lever_resume_page())
    res = _upload_resolution(page, resume)  # raises unless the scan found the file input
    [outcome] = fill.apply_resolutions(page, [res])

    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.readback == resume.name
    assert page.text_content("#file-name") == resume.name
    assert page.evaluate("window.__clicks") == []
    digest = TriageDigest(
        posting_id=1,
        url="https://example.invalid/apply",
        ats="lever",
        filled=[outcome],
        residual=[],
        skipped=[],
        errors=[],
    )
    assert submit._resume_blockers(page, digest, resume) == []


# --- hydration ----------------------------------------------------------------

_HYDRATION_PAGE = """
<!doctype html>
<html><body>
<form id="app">
  <label for="first_name">First Name *</label>
  <input id="first_name" name="first_name" type="text" required>
  <label for="email">Email *</label>
  <input id="email" name="email" type="email" required>
</form>
<script>
  window.__remixContext = {};
  window.__hydratedAt = null;
  window.__firstInputAt = null;
  document.addEventListener('input', function () {
    if (window.__firstInputAt === null) window.__firstInputAt = performance.now();
  });
  // What the first React re-render does: wipe typed text, then own the fields.
  window.__hydrate = function () {
    var fields = document.querySelectorAll('input');
    fields.forEach(function (el) { el.value = ''; });
    fields.forEach(function (el) { el['__reactProps$x'] = {}; });
    window.__hydratedAt = performance.now();
  };
  var AUTO_MS = __AUTO_MS__;
  if (AUTO_MS !== null) setTimeout(window.__hydrate, AUTO_MS);
</script>
</body></html>
"""


def _hydration_page(auto_ms: int | None) -> str:
    return _HYDRATION_PAGE.replace("__AUTO_MS__", "null" if auto_ms is None else str(auto_ms))


def _fill_resolution(spec: FieldSpec, value: str, key: str) -> Resolution:
    return Resolution(field=spec, semantic_key=key, action="fill", value=value)


def test_fill_waits_for_react_to_hydrate_so_the_value_sticks(page):
    page.set_content(_hydration_page(auto_ms=600))
    specs = {spec.selector: spec for spec in fill.scan_fields(page)}
    # scan_fields only returned once React owned every visible field.
    assert page.evaluate("window.__hydratedAt") is not None
    assert page.evaluate(
        "[...document.querySelectorAll('input')].every(el =>"
        " Object.getOwnPropertyNames(el).some(k => k.startsWith('__reactProps$')))"
    )

    resolutions = [
        _fill_resolution(specs["#first_name"], "Alex", "first_name"),
        _fill_resolution(specs["#email"], "alex.rivera@example.com", "email"),
    ]
    outcomes = fill.apply_resolutions(page, resolutions)

    assert [o.committed for o in outcomes] == [True, True]
    # Typing happened after hydration, so the re-render had nothing left to wipe.
    assert page.evaluate("window.__firstInputAt") > page.evaluate("window.__hydratedAt")
    page.wait_for_timeout(1500)
    assert page.input_value("#first_name") == "Alex"
    assert page.input_value("#email") == "alex.rivera@example.com"


def test_simulation_control_an_early_fill_is_wiped_by_hydration(page, fast_waits):
    """Without the wait the old flow reported committed and the value vanished."""
    page.set_content(_hydration_page(auto_ms=None))
    spec = FieldSpec(selector="#first_name", kind="text", label="First Name", required=True)
    [outcome] = fill.apply_resolutions(page, [_fill_resolution(spec, "Alex", "first_name")])
    assert outcome.committed is True  # the read-back saw the value
    page.evaluate("window.__hydrate()")  # React's first re-render
    assert page.input_value("#first_name") == ""


def test_verify_pass_refills_a_value_wiped_after_the_first_readback(page):
    """A page that is not server-rendered gets no hydration wait, so the verify
    pass is the safety net: it re-reads, sees the value gone, and fills once more."""
    page.set_content(
        """
        <form id="app"><input id="first_name" name="first_name" type="text"></form>
        <script>
          window.__fills = 0;
          var armed = true;
          document.addEventListener('input', function (ev) {
            window.__fills += 1;
            if (!armed) return;
            armed = false;
            setTimeout(function () { ev.target.value = ''; }, 250);
          });
        </script>
        """
    )
    spec = FieldSpec(selector="#first_name", kind="text", label="First Name")
    [outcome] = fill.apply_resolutions(page, [_fill_resolution(spec, "Alex", "first_name")])
    assert outcome.committed is True
    assert outcome.readback == "Alex"
    assert outcome.error == ""
    assert page.input_value("#first_name") == "Alex"
    assert page.evaluate("window.__fills") == 2  # the first fill and exactly one retry


def test_wait_for_fields_gives_up_on_a_field_that_never_hydrates(page):
    page.set_content(
        '<script>window.__remixContext = {};</script><input id="a" name="a" type="text">'
    )
    started = time.monotonic()
    count = fill.wait_for_fields(page, poll_ms=100, hydrate_timeout_ms=800)
    elapsed = time.monotonic() - started
    assert count == 1
    assert 0.8 <= elapsed < 5  # waited out the timeout, then stopped


def test_wait_for_fields_does_not_wait_on_a_client_rendered_page(page):
    page.set_content('<input id="a" name="a" type="text">')
    started = time.monotonic()
    assert fill.wait_for_fields(page, poll_ms=100, hydrate_timeout_ms=5000) == 1
    assert time.monotonic() - started < 1.5


# --- scan and plain fields ----------------------------------------------------

_FORM_PAGE = """
<!doctype html>
<html><body>
<form id="app">
  <label for="first_name">First Name *</label>
  <input id="first_name" name="first_name" type="text" required>
  <label for="email">Email</label>
  <input id="email" name="email" type="email" autocomplete="email">
  <label for="phone">Phone</label>
  <input id="phone" name="phone" type="tel" autocomplete="tel">
  <input id="linkedin" name="linkedin" type="url" aria-label="LinkedIn Profile" aria-required="true">
  <label for="notes">Notes</label>
  <textarea id="notes" name="notes"></textarea>
  <label for="state">State</label>
  <select id="state" name="state">
    <option value="">Select</option><option>Georgia</option><option>Texas</option>
  </select>
  <label for="country">Country *</label>
  <div class="select__control">
    <div class="select__value-container">
      <div class="select__placeholder">Select...</div>
      <input id="country" class="select__input" type="text" role="combobox" aria-required="true">
    </div>
  </div>
  <fieldset>
    <label for="auth_yes">Yes</label><input type="radio" id="auth_yes" name="auth" value="yes">
    <label for="auth_no">No</label><input type="radio" id="auth_no" name="auth" value="no" required>
  </fieldset>
  <fieldset>
    <label><input type="radio" name="sponsor" value="yes"> Yes</label>
    <label><input type="radio" name="sponsor" value="no"> No</label>
  </fieldset>
  <label><input type="checkbox" id="agree" name="agree"> I agree</label>
  <input type="hidden" id="csrf" name="csrf" value="x">
  <input id="ghost" name="ghost" type="text" style="display:none">
  <input id="faint" name="faint" type="text" style="opacity:0">
  <input type="submit" value="Submit">
</form>
</body></html>
"""


def test_scan_reads_labels_kinds_options_and_required_flags(page):
    page.set_content(_FORM_PAGE)
    specs = {spec.selector: spec for spec in fill.scan_fields(page)}

    assert list(specs) == [
        "#first_name",
        "#email",
        "#phone",
        "#linkedin",
        "#notes",
        "#state",
        "#country",
        '[name="auth"]',
        '[name="sponsor"]',
        "#agree",
    ]  # hidden, display:none and opacity:0 inputs are not fields
    assert {sel: spec.kind for sel, spec in specs.items()} == {
        "#first_name": "text",
        "#email": "email",
        "#phone": "tel",
        "#linkedin": "url",
        "#notes": "textarea",
        "#state": "select",
        "#country": "text",
        '[name="auth"]': "radio",
        '[name="sponsor"]': "radio",
        "#agree": "checkbox",
    }
    assert specs["#first_name"].label == "First Name *"
    assert specs["#linkedin"].label == "LinkedIn Profile"
    assert specs["#phone"].autocomplete == "tel"
    assert specs["#state"].options == ["Select", "Georgia", "Texas"]
    assert specs['[name="auth"]'].options == ["Yes", "No"]
    assert specs['[name="sponsor"]'].options == ["Yes", "No"]
    # required: the attribute, aria-required (react-select), or any radio member.
    assert {sel: spec.required for sel, spec in specs.items()} == {
        "#first_name": True,
        "#email": False,
        "#phone": False,
        "#linkedin": True,
        "#notes": False,
        "#state": False,
        "#country": True,
        '[name="auth"]': True,
        '[name="sponsor"]': False,
        "#agree": False,
    }


def test_plain_fields_fill_and_read_back_through_real_playwright(page, fast_waits):
    """text, email, textarea, select and radio all still work with the bounded
    timeouts fill now passes to every Playwright call."""
    page.set_content(_FORM_PAGE)
    specs = {spec.selector: spec for spec in fill.scan_fields(page)}
    resolutions = [
        _fill_resolution(specs["#first_name"], "Alex", "first_name"),
        _fill_resolution(specs["#email"], "alex.rivera@example.com", "email"),
        _fill_resolution(specs["#phone"], "4045550123", "phone"),
        _fill_resolution(specs["#notes"], "Happy to share more.", "notes"),
        _fill_resolution(specs["#state"], "Georgia", "state"),
        _fill_resolution(specs['[name="sponsor"]'], "No", "sponsorship"),
    ]
    outcomes = fill.apply_resolutions(page, resolutions)

    assert [o.error for o in outcomes] == [""] * 6
    assert [o.committed for o in outcomes] == [True] * 6
    assert [o.readback for o in outcomes] == [
        "Alex",
        "alex.rivera@example.com",
        "4045550123",
        "Happy to share more.",
        "Georgia",
        "No",
    ]
    assert page.is_checked('[name="sponsor"][value="no"]')


def test_a_checkbox_is_never_checked_by_fill(page, fast_waits):
    page.set_content(_FORM_PAGE)
    specs = {spec.selector: spec for spec in fill.scan_fields(page)}
    [outcome] = fill.apply_resolutions(page, [_fill_resolution(specs["#agree"], "yes", "agree")])
    assert outcome.committed is False
    assert outcome.error == "unsupported kind for fill: checkbox"
    assert not page.is_checked("#agree")


def test_a_combobox_outside_react_select_is_filled_by_typing(page, fast_waits):
    """Only a combobox inside a react-select control gets the keyboard path; an
    autocomplete input on another ATS is typed into like any text field."""
    page.set_content(
        """
        <label for="loc">Location</label>
        <input id="loc" type="text" role="combobox" aria-autocomplete="list">
        <script>
          window.__keys = [];
          document.addEventListener('keydown', ev => window.__keys.push(ev.key), true);
        </script>
        """
    )
    spec = FieldSpec(selector="#loc", kind="text", label="Location")
    [outcome] = fill.apply_resolutions(page, [_fill_resolution(spec, "Atlanta, GA", "location")])
    assert outcome.error == ""
    assert outcome.committed is True
    assert outcome.readback == "Atlanta, GA"
    assert _keys(page) == []


# --- question labels: Lever custom cards, Ashby radio groups ----------------------

# Lever's custom questions have no <label for>: the question is a heading <div>
# beside the control, and a radio option's label is its answer ("Yes"). Before
# the scan named that heading every such field reached the digest as
# `cards[<uuid>][field0]` and classify() had nothing to match on.
_LEVER_HEARD = "cards[11111111-1111-4111-8111-111111111111][field0]"
_LEVER_AUTH = "cards[22222222-2222-4222-8222-222222222222][field0]"
_LEVER_SPONSOR = "cards[33333333-3333-4333-8333-333333333333][field0]"

_LEVER_PAGE = f"""
<!doctype html>
<html><body>
<form id="application-form" action="/never-submitted" method="post" novalidate>
<ul>
  <li class="application-question">
    <label>
      <div class="application-label">Full name<span class="required">✱</span></div>
      <div class="application-field"><input type="text" name="name" required></div>
    </label>
  </li>
  <li class="application-question custom-question">
    <div class="application-label full-width">
      <div class="text">How did you hear about this role?<span class="required">✱</span></div>
    </div>
    <div class="application-field full-width">
      <textarea name="{_LEVER_HEARD}" required></textarea>
    </div>
  </li>
  <li class="application-question custom-question">
    <div class="application-label full-width">
      <div class="text">Are you legally authorized to work in the United States?<span class="required">✱</span></div>
    </div>
    <div class="application-field full-width">
      <ul data-qa="multiple-choice">
        <li><label><input type="radio" name="{_LEVER_AUTH}" value="Yes" required><span class="application-answer-alternative">Yes</span></label></li>
        <li><label><input type="radio" name="{_LEVER_AUTH}" value="No" required><span class="application-answer-alternative">No</span></label></li>
      </ul>
    </div>
  </li>
  <li class="application-question custom-question">
    <div class="application-label full-width">
      <div class="text">Will you now or in the future require visa sponsorship?<span class="required">✱</span></div>
    </div>
    <div class="application-field full-width">
      <ul data-qa="multiple-choice">
        <li><label><input type="radio" name="{_LEVER_SPONSOR}" value="Yes" required><span class="application-answer-alternative">Yes</span></label></li>
        <li><label><input type="radio" name="{_LEVER_SPONSOR}" value="No" required><span class="application-answer-alternative">No</span></label></li>
      </ul>
    </div>
  </li>
</ul>
</form>
</body></html>
"""

# Ashby titles a question with a <label> that has no `for` when the control is a
# radio group, and its options carry their own label[for].
_ASHBY_PAGE = """
<!doctype html>
<html><body>
<form action="/never-submitted" novalidate>
  <div class="ashby-application-form-field-entry">
    <label class="ashby-application-form-question-title" for="_systemfield_name">Name</label>
    <input id="_systemfield_name" name="_systemfield_name" type="text" required>
  </div>
  <div class="ashby-application-form-field-entry">
    <label class="ashby-application-form-question-title">Are you authorized to work in the United States?</label>
    <div>
      <input type="radio" id="auth-yes" name="auth" value="yes"><label for="auth-yes">Yes</label>
      <input type="radio" id="auth-no" name="auth" value="no"><label for="auth-no">No</label>
    </div>
  </div>
</form>
</body></html>
"""


def test_scan_names_lever_custom_questions_by_their_heading(page):
    page.set_content(_LEVER_PAGE)
    specs = {spec.name: spec for spec in fill.scan_fields(page)}

    assert list(specs) == ["name", _LEVER_HEARD, _LEVER_AUTH, _LEVER_SPONSOR]
    # A wrapping <label> already named the standard field: unchanged.
    assert specs["name"].label == "Full name✱"
    # The textarea has no label of any kind: the heading beside it names it.
    assert specs[_LEVER_HEARD].kind == "textarea"
    assert specs[_LEVER_HEARD].label == "How did you hear about this role?✱"
    # A radio group is named by its question; its options are still the answers.
    assert specs[_LEVER_AUTH].kind == "radio"
    assert specs[_LEVER_AUTH].label == "Are you legally authorized to work in the United States?✱"
    assert specs[_LEVER_AUTH].options == ["Yes", "No"]
    assert specs[_LEVER_SPONSOR].kind == "radio"
    assert specs[_LEVER_SPONSOR].label == "Will you now or in the future require visa sponsorship?✱"
    assert specs[_LEVER_SPONSOR].options == ["Yes", "No"]


def test_scan_names_an_ashby_radio_group_by_its_question_title(page):
    page.set_content(_ASHBY_PAGE)
    specs = {spec.name: spec for spec in fill.scan_fields(page)}

    assert list(specs) == ["_systemfield_name", "auth"]
    assert specs["_systemfield_name"].label == "Name"  # label[for]: unchanged
    assert specs["auth"].kind == "radio"
    assert specs["auth"].label == "Are you authorized to work in the United States?"
    assert specs["auth"].options == ["Yes", "No"]


def test_lever_card_questions_classify_by_their_heading_not_their_uuid_name(page):
    """The card names are uuids, so only the heading can say what a question is.
    These are the keys the digest could not give the work-authorization,
    sponsorship and how-did-you-hear questions on Lever."""
    page.set_content(_LEVER_PAGE)
    specs = {spec.name: spec for spec in fill.scan_fields(page)}

    assert mapper.classify is classify  # the classifier fill and the mapper both use
    assert classify(specs["name"]) == "full_name"
    assert classify(specs[_LEVER_HEARD]) == "how_heard"
    assert classify(specs[_LEVER_AUTH]) == "work_authorization"
    assert classify(specs[_LEVER_SPONSOR]) == "sponsorship"
    # The same group scanned with the empty label it used to get matches nothing:
    # it went to needs-you under its uuid.
    assert classify(dataclasses.replace(specs[_LEVER_AUTH], label="")) is None
    # fill hands map_unknown only what classify() cannot name: none of these now.
    assert [spec.name for spec in specs.values() if classify(spec) is None] == []


_QUESTION_MARKUP = [
    pytest.param(
        '<span id="q">Preferred pronouns</span><input name="g" type="text" aria-labelledby="q">',
        "Preferred pronouns",
        [],
        id="aria-labelledby-on-the-field",
    ),
    pytest.param(
        '<span id="q1">Do you require</span> <span id="q2">visa sponsorship?</span>'
        '<div role="radiogroup" aria-labelledby="q1 q2">'
        '<label><input type="radio" name="g" value="y"> Yes</label>'
        '<label><input type="radio" name="g" value="n"> No</label></div>',
        "Do you require visa sponsorship?",
        ["Yes", "No"],
        id="radiogroup-aria-labelledby-joins-its-ids",
    ),
    pytest.param(
        '<div role="group" aria-label="  Do you   require sponsorship? ">'
        '<label><input type="radio" name="g" value="y"> Yes</label>'
        '<label><input type="radio" name="g" value="n"> No</label></div>',
        "Do you require sponsorship?",
        ["Yes", "No"],
        id="group-aria-label",
    ),
    pytest.param(
        '<div class="field"><div class="application-label">Outer heading</div>'
        "<fieldset><legend>Do you require sponsorship?</legend>"
        '<label><input type="radio" name="g" value="y"> Yes</label>'
        '<label><input type="radio" name="g" value="n"> No</label></fieldset></div>',
        "Do you require sponsorship?",
        ["Yes", "No"],
        id="fieldset-legend-beats-the-box-heading",
    ),
    pytest.param(
        "<fieldset><legend>Wrong</legend>"
        '<span id="q">Right</span><div role="radiogroup" aria-labelledby="q">'
        '<label><input type="radio" name="g" value="y"> Yes</label>'
        '<label><input type="radio" name="g" value="n"> No</label></div></fieldset>',
        "Right",
        ["Yes", "No"],
        id="role-group-beats-the-fieldset-legend",
    ),
    pytest.param(
        '<div class="field">'
        '<label><input type="radio" name="g" value="y"> Option A</label>'
        '<div class="application-label">The real question</div>'
        '<label><input type="radio" name="g" value="n"> Option B</label></div>',
        "The real question",
        ["Option A", "Option B"],
        id="a-label-that-holds-a-control-is-not-a-heading",
    ),
    pytest.param(
        '<ul><li class="application-question"><div class="application-label">Pick one✱</div>'
        '<input type="radio" name="g" value="alpha"><input type="radio" name="g" value="beta">'
        "</li></ul>",
        "Pick one✱",
        ["alpha", "beta"],
        id="options-without-a-label-keep-their-value-not-the-question",
    ),
    pytest.param(
        '<div class="field"><div class="application-label">Question?</div>'
        '<input type="radio" name="g" value="y" aria-labelledby="oy"><span id="oy">Yes</span>'
        '<input type="radio" name="g" value="n" aria-labelledby="on"><span id="on">No</span></div>',
        "Question?",
        ["y", "n"],
        id="a-radios-own-aria-labelledby-names-the-option-not-the-question",
    ),
    pytest.param(
        '<ul><li class="application-question"><div class="application-label">'
        '<div class="text">Question?</div><div class="description">Details</div></div>'
        '<textarea name="g"></textarea></li></ul>',
        "Question? Details",
        [],
        id="block-children-collapse-to-one-line",
    ),
    pytest.param(
        '<div class="field-wrapper"><label class="application-label"></label>'
        '<div class="application-label">The real question</div>'
        '<input name="g" type="text"></div>',
        "The real question",
        [],
        id="an-empty-heading-is-skipped",
    ),
    pytest.param(
        '<div class="ashby-application-form-field-entry">'
        '<div class="ashby-application-form-question-title">Question?</div>'
        '<input name="g" type="text"></div>',
        "Question?",
        [],
        id="ashby-title-that-is-not-a-label",
    ),
]


@pytest.mark.parametrize(("markup", "label", "options"), _QUESTION_MARKUP)
def test_scan_finds_the_question_heading_in_the_markup_a_page_provides(
    page, markup, label, options
):
    page.set_content(f'<form action="/never-submitted" novalidate>{markup}</form>')
    [spec] = fill.scan_fields(page)  # a radio group is one field
    assert spec.name == "g"
    assert (spec.label, spec.options) == (label, options)


def test_a_checkbox_label_stays_the_option_text(page):
    page.set_content(
        """
        <ul><li class="application-question">
          <div class="application-label">Consent</div>
          <label><input type="checkbox" name="agree"> I agree to the privacy notice</label>
          <input type="checkbox" name="bare">
        </li></ul>
        """
    )
    specs = {spec.name: spec for spec in fill.scan_fields(page)}
    assert specs["agree"].label == "I agree to the privacy notice"
    assert specs["bare"].label == ""  # never the question's heading


# --- all three problems on one page ---------------------------------------------

_GREENHOUSE_RESUME_FIELD = """
  <div id="resume-field">
    <label for="resume">Resume/CV *</label>
    <input id="resume" name="resume" type="file" required
           style="position:absolute;width:1px;height:1px;margin:-1px;overflow:hidden;clip:rect(0 0 0 0)">
  </div>
"""

_GREENHOUSE_SCRIPT = """
<script>
  window.__remixContext = {};
  window.__hydratedAt = null;
  window.__firstActionAt = null;
  ['keydown', 'input', 'change'].forEach(function (type) {
    document.addEventListener(type, function () {
      if (window.__firstActionAt === null) window.__firstActionAt = performance.now();
    }, true);
  });
  // The page's change handler removes the file input and shows a chip.
  document.addEventListener('change', function (ev) {
    if (ev.target.id !== 'resume') return;
    var chip = document.createElement('div');
    chip.className = 'chip';
    chip.innerHTML = '<span class="filename"></span>';
    chip.querySelector('.filename').textContent = ev.target.files[0].name;
    document.getElementById('resume-field').appendChild(chip);
    ev.target.remove();
  });
  // Hydration: React wipes what was typed, then owns every field.
  setTimeout(function () {
    document.querySelectorAll('input').forEach(function (el) {
      if (el.type !== 'hidden') el.value = '';
      el['__reactProps$x'] = {};
    });
    window.__hydratedAt = performance.now();
  }, 600);
</script>
"""


def _greenhouse_page() -> str:
    """Server-rendered form: a text field, a react-select Country, a resume
    upload that vanishes on change, and a hydration wipe 600 ms in."""
    submit = '<button type="submit" id="go">'
    return (
        _select_page(_COUNTRIES)
        .replace(submit, _GREENHOUSE_RESUME_FIELD + submit)
        .replace("</body>", _GREENHOUSE_SCRIPT + "</body>")
    )


def test_text_combobox_and_resume_all_stick_on_a_hydrating_page(page, tmp_path):
    resume = _resume_file(tmp_path)
    page.set_content(_greenhouse_page())
    specs = {spec.selector: spec for spec in fill.scan_fields(page)}
    assert page.evaluate("window.__hydratedAt") is not None  # the scan waited for React
    assert set(specs) == {"#first_name", "#country", "#resume"}

    resolutions = [
        _fill_resolution(specs["#first_name"], "Alex", "first_name"),
        Resolution(
            field=specs["#country"], semantic_key="country", action="fill", value="United States"
        ),
        Resolution(
            field=specs["#resume"], semantic_key="resume", action="upload", value=str(resume)
        ),
    ]
    outcomes = fill.apply_resolutions(page, resolutions)

    assert [o.error for o in outcomes] == ["", "", ""]
    assert [o.committed for o in outcomes] == [True, True, True]
    assert [o.readback for o in outcomes] == ["Alex", "United States", resume.name]
    # Nothing was typed, pressed or uploaded until React owned the form.
    assert page.evaluate("window.__firstActionAt") > page.evaluate("window.__hydratedAt")
    page.wait_for_timeout(1000)
    assert page.input_value("#first_name") == "Alex"
    assert _single_value(page) == "United States"
    assert page.locator("input[class*=requiredInput]").count() == 0
    assert page.locator("#resume").count() == 0
    assert page.locator(".chip .filename").first.text_content() == resume.name
    assert _keys(page) == ["Tab"]
    _assert_inert(page)


# --- a pick rule applied to a menu solve.py could not see ---------------------------

_DEGREES = ["Associate's Degree", "Bachelor's Degree", "Master's Degree", "Other"]
_DISCIPLINES = ["Accounting", "Business", "Business Administration", "Finance", "Finance"]


@pytest.mark.parametrize("variant", _MENU_VARIANTS)
def test_a_stored_bachelors_line_picks_the_one_bachelor_option_of_the_menu(
    page, fast_waits, quick_polls, variant
):
    page.set_content(_select_page(_DEGREES, **variant))
    stored = "B.S. Business Administration, Finance \u2014 Example Tech, Dec 2030, GPA 3.50"
    [outcome] = fill.apply_resolutions(page, [_ruled(stored, "starts_with", ["Bachelor"])])
    assert (outcome.error, outcome.committed) == ("", True)
    assert outcome.value == outcome.readback == "Bachelor's Degree"
    assert page.evaluate("window.__selected") == "Bachelor's Degree"
    _assert_inert(page, typed="Bachelor's Degree")


def test_a_menu_with_two_bachelor_options_commits_nothing(page, fast_waits, quick_polls):
    page.set_content(_select_page([*_DEGREES, "Bachelor of Arts"]))
    [outcome] = fill.apply_resolutions(page, [_ruled("B.S. Finance", "starts_with", ["Bachelor"])])
    assert outcome.committed is False
    assert outcome.error == fill._NO_RULE_OPTION
    assert _single_value(page) is None
    _assert_inert(page)


def test_a_partial_menu_is_not_trusted_with_one_match(page, fast_waits, quick_polls):
    # A "Load more" row says the list goes on past what is rendered: a single Bachelor
    # match proves nothing about the options not shown, so the field is left for him.
    page.set_content(_select_page([*_DEGREES, "Load more results"]))
    [outcome] = fill.apply_resolutions(page, [_ruled("B.S. Finance", "starts_with", ["Bachelor"])])
    assert (outcome.committed, outcome.error) == (False, fill._PARTIAL_MENU)
    assert _single_value(page) is None
    _assert_inert(page)


def test_a_menu_with_no_bachelor_option_commits_nothing(page, fast_waits, quick_polls):
    page.set_content(_select_page(["Associate's Degree", "Other"]))
    [outcome] = fill.apply_resolutions(page, [_ruled("B.S. Finance", "starts_with", ["Bachelor"])])
    assert (outcome.committed, outcome.error) == (False, fill._NO_RULE_OPTION)
    assert _single_value(page) is None


def test_the_first_major_that_equals_one_option_is_picked(page, fast_waits, quick_polls):
    page.set_content(_select_page(_DISCIPLINES))
    majors = ["Business Administration", "Finance"]
    [outcome] = fill.apply_resolutions(
        page, [_ruled(majors[0], "first_unique_equal", majors, key="field_of_study")]
    )
    assert (outcome.error, outcome.committed) == ("", True)
    assert outcome.readback == "Business Administration"


def test_a_major_listed_twice_is_skipped_for_the_next_one(page, fast_waits, quick_polls):
    page.set_content(_select_page(_DISCIPLINES))
    majors = ["Finance", "Business"]
    [outcome] = fill.apply_resolutions(
        page, [_ruled(majors[0], "first_unique_equal", majors, key="field_of_study")]
    )
    assert (outcome.error, outcome.committed) == ("", True)
    assert outcome.readback == "Business"


def test_a_rule_on_a_plain_text_input_leaves_the_value_as_it_was(page, fast_waits):
    page.set_content('<input id="country" type="text">')
    [outcome] = fill.apply_resolutions(page, [_ruled("B.S. Finance", "starts_with", ["Bachelor"])])
    assert (outcome.error, outcome.committed, outcome.readback) == ("", True, "B.S. Finance")


# DV Trading's Greenhouse menus (2026-10-26): the options spell the answer out in full.
_DV_AUTH = [
    "Yes, I am authorized to work in this country for any employer",
    "No, I am not authorized to work in this country for any employer",
    "I am authorized to work in this country for my present employer only",
]
_DV_SPONSOR = [
    "No, I will not require sponsorship now or in the future to work in this country",
    "Yes, I will require sponsorship now or in the future to work in this country",
]
_DV_HEARD = [
    "LinkedIn", "DV Recruitment", "DV Employee", "DV Intern", "DV Website",
    "Student Organization", "Campus Event", "Word of Mouth", "SHRM", "Other",
]  # fmt: skip


@pytest.mark.parametrize("variant", _MENU_VARIANTS)
def test_a_yes_rule_commits_the_menus_one_yes_option(page, fast_waits, quick_polls, variant):
    page.set_content(_select_page(_DV_AUTH, **variant))
    [outcome] = fill.apply_resolutions(
        page, [_ruled("Yes", "yes_no", ["Yes"], key="work_authorization")]
    )
    assert (outcome.error, outcome.committed) == ("", True)
    assert outcome.readback == _DV_AUTH[0]
    assert page.evaluate("window.__selected") == _DV_AUTH[0]
    _assert_inert(page, typed=_DV_AUTH[0])


def test_a_no_rule_commits_the_menus_no_option_not_the_yes_that_holds_no(
    page, fast_waits, quick_polls
):
    page.set_content(_select_page(_DV_SPONSOR))
    [outcome] = fill.apply_resolutions(page, [_ruled("No", "yes_no", ["No"], key="sponsorship")])
    assert (outcome.error, outcome.committed) == ("", True)
    assert outcome.readback == _DV_SPONSOR[0]


def test_a_yes_rule_commits_nothing_when_the_menu_only_has_the_no(page, fast_waits, quick_polls):
    page.set_content(_select_page([_DV_AUTH[1], _DV_AUTH[2]]))
    [outcome] = fill.apply_resolutions(
        page, [_ruled("Yes", "yes_no", ["Yes"], key="work_authorization")]
    )
    assert (outcome.committed, outcome.error) == (False, fill._NO_RULE_OPTION)
    assert _single_value(page) is None
    _assert_inert(page)


def test_a_yes_rule_commits_nothing_on_a_crowded_menu(page, fast_waits, quick_polls):
    page.set_content(_select_page(["Yes, for any employer", "Yes, with sponsorship", "No"]))
    [outcome] = fill.apply_resolutions(
        page, [_ruled("Yes", "yes_no", ["Yes"], key="work_authorization")]
    )
    assert (outcome.committed, outcome.error) == (False, fill._NO_RULE_OPTION)
    assert _single_value(page) is None


def test_a_country_rule_commits_the_one_option_spelling_the_stored_country(
    page, fast_waits, quick_polls
):
    menu = ["Canada", "Georgia", "United Kingdom", "United States of America"]
    page.set_content(_select_page(menu))
    [outcome] = fill.apply_resolutions(
        page, [_ruled("United States", "country", ["United States"], key="residence_country")]
    )
    assert (outcome.error, outcome.committed) == ("", True)
    assert outcome.readback == "United States of America"


def test_a_country_rule_commits_nothing_when_the_menu_has_no_such_country(
    page, fast_waits, quick_polls
):
    page.set_content(_select_page(["Canada", "Georgia", "United Kingdom"]))
    [outcome] = fill.apply_resolutions(
        page, [_ruled("United States", "country", ["United States"], key="residence_country")]
    )
    assert (outcome.committed, outcome.error) == (False, fill._NO_RULE_OPTION)
    assert _single_value(page) is None


def test_a_company_site_rule_leaves_a_company_named_how_heard_menu_to_him(
    page, fast_waits, quick_polls
):
    page.set_content(_select_page(_DV_HEARD))
    [outcome] = fill.apply_resolutions(
        page,
        [_ruled("Company careers page", "company_site", ["Company careers page"], key="how_heard")],
    )
    assert (outcome.committed, outcome.error) == (False, fill._NO_RULE_OPTION)
    assert _single_value(page) is None
    _assert_inert(page)


def test_a_company_site_rule_commits_the_menus_generic_company_site_option(
    page, fast_waits, quick_polls
):
    page.set_content(_select_page(["LinkedIn", "Company website", "Other"]))
    [outcome] = fill.apply_resolutions(
        page,
        [_ruled("Company careers page", "company_site", ["Company careers page"], key="how_heard")],
    )
    assert (outcome.error, outcome.committed) == ("", True)
    assert outcome.readback == "Company website"


_GENDERS = ["Male", "Female", "Decline To Self Identify"]


def test_a_decline_rule_commits_the_menus_decline_option(page, fast_waits, quick_polls):
    page.set_content(_select_page(_GENDERS))
    [outcome] = fill.apply_resolutions(
        page, [_ruled("Decline to self-identify", "decline", [], key="gender")]
    )
    assert (outcome.error, outcome.committed) == ("", True)
    assert outcome.readback == "Decline To Self Identify"


def test_a_decline_rule_commits_nothing_when_the_menu_has_no_decline_option(
    page, fast_waits, quick_polls
):
    page.set_content(_select_page(["Male", "Female"]))
    [outcome] = fill.apply_resolutions(
        page, [_ruled("Decline to self-identify", "decline", [], key="gender")]
    )
    assert (outcome.committed, outcome.error) == (False, fill._NO_RULE_OPTION)
    assert _single_value(page) is None


def test_a_decline_rule_never_types_into_a_plain_text_input(page, fast_waits):
    page.set_content('<input id="country" type="text">')
    [outcome] = fill.apply_resolutions(
        page, [_ruled("Decline to self-identify", "decline", [], key="gender")]
    )
    assert outcome.committed is False
    assert outcome.error == "not a menu: answer by hand"
    assert page.input_value("#country") == ""
