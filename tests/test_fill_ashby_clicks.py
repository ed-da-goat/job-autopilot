"""fill.py's Ashby answer clicks (autoapply only), behind the dry-run submit guard.

The owner, 2026-10-24 (spec question Q7): fill.py may click Ashby's Yes/No buttons,
hidden radios and list options, with one condition: a test shows that the guard
stops any submission first (tests/test_submit_guard.py). These tests run the
clicks in a real headless Chromium against a page built like Ashby's form: its
class names (read from the live bundle), type-less answer buttons that submit
the form, hidden radios and checkboxes behind labels, a location box whose list
resets when the box loses focus, and a date box with a react-datepicker popup.

Every page records submit events (`window.__submitted`), clicks and the POSTs
that reach its fake host, so "nothing was submitted" is observed three ways. The
`page` fixture's context aborts every http(s) request; the fake host below is
the only thing that answers.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime
from pathlib import Path

import pytest

from jobhunt.apply_assist import fill, solve, submit_guard
from jobhunt.apply_assist.schema import FieldSpec, Resolution
from jobhunt.eligibility import user_preferences
from tests import test_fill_browser as _browser_fixtures

# The headless-Chromium fixtures, bound by assignment (see test_submit_guard.py).
browser = _browser_fixtures.browser
page = _browser_fixtures.page
fast_waits = _browser_fixtures.fast_waits

HOST = "https://jobs.example.test"
URL = f"{HOST}/acme/0b2ac1d4-6f46-4f6b-ad9e-a258e642751c/application"  # made up

AUTH_Q = "Are you authorized to work in the country where the job is located?"
SPONSOR_Q = "Will you now or in the future require sponsorship?"
LOCATION_Q = "Where are you currently located?"
START_Q = "When can you start a new role?"
SMS_Q = "May we text you about your application?"
ACK_Q = "Acknowledgments"

_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  body { font: 14px sans-serif; margin: 24px; }
  .ashby-application-form-field-entry { margin: 14px 0; }
  ._required_f7cvd_91::after { content: "*"; }
  .hide { position: absolute; opacity: 0; width: 1px; height: 1px; }
  [hidden] { display: none !important; }
  .ashby-application-form-input-autocomplete-popup,
  .ashby-application-form-input-date-popup { border: 1px solid #888; width: 300px; }
  [role=option] { padding: 4px 8px; }
  .react-datepicker__day { display: inline-block; width: 36px; text-align: center; }
  .react-datepicker__day--outside-month { color: #aaa; }
  .react-datepicker__day--selected { background: #cde; }
</style></head><body>
<form id="application" action="/never-submitted" method="post" novalidate>
  <div class="ashby-application-form-field-entry">
    <label class="ashby-application-form-question-title _required_f7cvd_91"
           for="legal">Legal Name</label>
    <input id="legal" name="legal" type="text" required
           class="ashby-application-form-input-text">
  </div>

  <div class="ashby-application-form-field-entry">
    <label class="ashby-application-form-question-title _required_f7cvd_91">__AUTH_Q__</label>
    <div class="ashby-application-form-input-yesno"><div class="container">
      <div class="ashby-application-form-input-yesno-option">
        <button data-option="yes" aria-pressed="false">Yes</button></div>
      <div class="ashby-application-form-input-yesno-option">
        <button data-option="no" aria-pressed="false">No</button></div>
      <input class="hide" type="checkbox" tabindex="-1" name="b5f1-auth">
    </div></div>
  </div>

  <div class="ashby-application-form-field-entry">
    <label class="ashby-application-form-question-title">__SPONSOR_Q__</label>
    <div class="ashby-application-form-input-yesno"><div class="container">
      <div class="ashby-application-form-input-yesno-option">
        <button data-option="yes" aria-pressed="false">Yes</button></div>
      <div class="ashby-application-form-input-yesno-option">
        <button data-option="no" aria-pressed="false">No</button></div>
      <input class="hide" type="checkbox" tabindex="-1" name="b5f1-sponsor">
    </div></div>
  </div>

  <div class="ashby-application-form-field-entry">
    <label class="ashby-application-form-question-title _required_f7cvd_91">__LOCATION_Q__</label>
    <input class="ashby-application-form-input-autocomplete" role="combobox"
           aria-expanded="false" placeholder="Start typing..." autocomplete="off">
  </div>
  <div class="ashby-application-form-input-autocomplete-popup" hidden></div>

  <div class="ashby-application-form-field-entry">
    <label class="ashby-application-form-question-title _required_f7cvd_91">__START_Q__</label>
    <input class="ashby-application-form-input-date" type="text"
           placeholder="Pick date..." autocomplete="off">
  </div>
  <div class="ashby-application-form-input-date-popup" hidden>
    <h4><span id="month"></span></h4>
    <button data-direction="previous">Previous</button>
    <button data-direction="next">Next</button>
    <div id="days"></div>
  </div>

  <div class="ashby-application-form-field-entry">
    <label class="ashby-application-form-question-title _required_f7cvd_91">__SMS_Q__</label>
    <div class="ashby-application-form-input-radio-group">
      <div class="ashby-application-form-input-radio-group-option">
        <input class="hide" type="radio" name="smsConsent" id="sms-given" value="given">
        <label for="sms-given">I agree to receive text messages</label></div>
      <div class="ashby-application-form-input-radio-group-option">
        <input class="hide" type="radio" name="smsConsent" id="sms-not" value="notGiven">
        <label for="sms-not">I do not agree</label></div>
    </div>
  </div>

  <div class="ashby-application-form-field-entry">
    <label class="ashby-application-form-question-title _required_f7cvd_91">__ACK_Q__</label>
    <div class="ashby-application-form-input-checkbox-group">
      <div class="ashby-application-form-input-checkbox-group-option">
        <input class="hide" type="checkbox" name="ack" id="ack">
        <label for="ack">I acknowledge <a id="terms" href="#terms">the Arbitration Agreement and
          the Terms of Service for applicants</a></label></div>
    </div>
  </div>

  <button id="send">Submit Application</button>
</form>
<script>
  window.__submitted = []; window.__clicks = []; window.__dayClicks = []; window.__linkClicks = 0;
  document.addEventListener('submit', (e) => window.__submitted.push(e.target.id), true);
  document.addEventListener('click', (e) => window.__clicks.push(e.target.id || e.target.tagName),
                            true);
  document.getElementById('terms').addEventListener('click', (e) => {
    window.__linkClicks += 1; e.preventDefault();
  });
  const ACK_TOGGLES = __ACK_TOGGLES__, TYPING = __TYPING__, DELAY = __DELAY__;
  if (!ACK_TOGGLES) {
    document.getElementById('ack').addEventListener('click', (e) => e.preventDefault());
  }

  // Yes/No: a pressed button pressed again turns back off, as Ashby's does.
  for (const group of document.querySelectorAll('.ashby-application-form-input-yesno')) {
    for (const button of group.querySelectorAll('button')) {
      button.addEventListener('click', () => {
        const wasOn = button.getAttribute('aria-pressed') === 'true';
        for (const other of group.querySelectorAll('button')) other.setAttribute('aria-pressed', 'false');
        if (!wasOn) button.setAttribute('aria-pressed', 'true');
        group.querySelector('input').checked = !wasOn && button.dataset.option === 'yes';
      });
    }
  }

  // Location: typing opens a list; only a pick sets the value, and leaving the box with
  // the list open puts back the last pick.
  const PLACES = ['Atlanta, Georgia, United States', 'Atlanta, Texas, United States',
                  'Atlantic City, New Jersey, United States', 'Boston, Massachusetts, United States',
                  'New York City, New York, United States', 'New York, United States',
                  'York, New York, United States', 'New York Mills, Minnesota, United States'];
  const box = document.querySelector('input.ashby-application-form-input-autocomplete');
  const popup = document.querySelector('.ashby-application-form-input-autocomplete-popup');
  let lastPick = '', timer = null;
  function showList(q) {
    popup.innerHTML = '';
    for (const place of PLACES.filter((p) => p.toLowerCase().includes(q.toLowerCase()))) {
      const option = document.createElement('div');
      option.setAttribute('role', 'option');
      option.textContent = place;
      option.addEventListener('mousedown', (e) => e.preventDefault());
      option.addEventListener('click', () => {
        lastPick = place; box.value = place; popup.hidden = true;
        box.setAttribute('aria-expanded', 'false');
      });
      popup.appendChild(option);
    }
  }
  box.addEventListener('input', () => {
    popup.hidden = false; box.setAttribute('aria-expanded', 'true');
    clearTimeout(timer);
    if (DELAY) {
      popup.innerHTML = '<div class="ashby-application-form-input-autocomplete-popup-loading">Loading</div>';
      timer = setTimeout(() => showList(box.value), DELAY);
    } else {
      showList(box.value);
    }
  });
  box.addEventListener('blur', () => {
    if (!popup.hidden) {
      clearTimeout(timer);
      box.value = lastPick; popup.hidden = true; box.setAttribute('aria-expanded', 'false');
    }
  });

  // Date: the calendar opens on focus. With TYPING the box keeps a typed MM/DD/YYYY when it
  // loses focus; without it only a clicked day sets the date.
  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
                  'September', 'October', 'November', 'December'];
  const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  const dbox = document.querySelector('input.ashby-application-form-input-date');
  const dpop = document.querySelector('.ashby-application-form-input-date-popup');
  let view = new Date(2026, 9, 1), committed = '';
  const pad = (n) => String(n).padStart(2, '0');
  const ordinal = (n) => n + (n % 100 >= 11 && n % 100 <= 13 ? 'th' : ({1: 'st', 2: 'nd', 3: 'rd'}[n % 10] || 'th'));
  const fmt = (d) => pad(d.getMonth() + 1) + '/' + pad(d.getDate()) + '/' + d.getFullYear();
  function render() {
    document.getElementById('month').textContent = MONTHS[view.getMonth()] + ' ' + view.getFullYear();
    const days = document.getElementById('days'); days.innerHTML = '';
    const first = new Date(view.getFullYear(), view.getMonth(), 1 - new Date(view.getFullYear(), view.getMonth(), 1).getDay());
    for (let i = 0; i < 42; i++) {
      const day = new Date(first.getFullYear(), first.getMonth(), first.getDate() + i);
      const outside = day.getMonth() !== view.getMonth();
      const cell = document.createElement('div');
      cell.className = 'react-datepicker__day' + (outside ? ' react-datepicker__day--outside-month' : '') +
        (fmt(day) === committed ? ' react-datepicker__day--selected' : '');
      cell.setAttribute('aria-label', 'Choose ' + DAYS[day.getDay()] + ', ' + MONTHS[day.getMonth()] +
        ' ' + ordinal(day.getDate()) + ', ' + day.getFullYear());
      cell.textContent = day.getDate();
      cell.addEventListener('click', () => {
        window.__dayClicks.push(cell.getAttribute('aria-label'));
        committed = fmt(day) === committed ? '' : fmt(day);
        dbox.value = committed; dpop.hidden = true;
      });
      days.appendChild(cell);
    }
  }
  dpop.addEventListener('mousedown', (e) => e.preventDefault());
  for (const nav of dpop.querySelectorAll('button')) {
    nav.addEventListener('click', () => {
      view = new Date(view.getFullYear(), view.getMonth() + (nav.dataset.direction === 'next' ? 1 : -1), 1);
      render();
    });
  }
  dbox.addEventListener('focus', () => { render(); dpop.hidden = false; });
  dbox.addEventListener('blur', () => {
    dpop.hidden = true;
    if (TYPING && /^\d\d\/\d\d\/\d{4}$/.test(dbox.value)) committed = dbox.value;
    dbox.value = committed;
  });
</script>
</body></html>"""


class Site:
    """The fake ATS host: serves the page, records every request that reaches it."""

    def __init__(self, tab, *, typing=True, delay=0, ack_toggles=True):
        self.hits: list[tuple[str, str]] = []
        html = (
            _PAGE.replace("__AUTH_Q__", AUTH_Q)
            .replace("__SPONSOR_Q__", SPONSOR_Q)
            .replace("__LOCATION_Q__", LOCATION_Q)
            .replace("__START_Q__", START_Q)
            .replace("__SMS_Q__", SMS_Q)
            .replace("__ACK_Q__", ACK_Q)
            .replace("__TYPING__", "true" if typing else "false")
            .replace("__DELAY__", str(delay))
            .replace("__ACK_TOGGLES__", "true" if ack_toggles else "false")
        )

        def handle(route):
            request = route.request
            self.hits.append((request.method, request.url))
            body = html if request.method == "GET" else "ok"
            route.fulfill(status=200, content_type="text/html", body=body)

        tab.route(f"{HOST}/**", handle)

    def posts(self) -> list[str]:
        return [url for method, url in self.hits if method == "POST"]


@pytest.fixture
def make_page(page, fast_waits):
    """Open the Ashby-like page. `guard=False` leaves it unguarded."""

    def open_page(*, guard=True, **site_options):
        site = Site(page, **site_options)
        if guard:
            submit_guard.install(page)
        page.goto(URL)
        page.wait_for_selector("#application")
        return site

    return open_page


def _nothing_submitted(page, site) -> None:
    assert page.evaluate("window.__submitted") == []
    assert site.posts() == []
    assert "never-submitted" not in page.url
    assert submit_guard.blockers(page) == []


def _scan(page) -> list[FieldSpec]:
    return fill.scan_fields(page, autoapply=True)


def _spec(specs: list[FieldSpec], label: str) -> FieldSpec:
    found = [s for s in specs if s.label.startswith(label)]
    assert len(found) == 1, [(s.label, s.selector) for s in specs]
    return found[0]


def _res(spec: FieldSpec, key: str, action: str, value: str | None) -> Resolution:
    return Resolution(field=spec, semantic_key=key, action=action, value=value)


def _apply(page, specs, label, key, value, action="fill"):
    [outcome] = fill.apply_resolutions(
        page, [_res(_spec(specs, label), key, action, value)], autoapply=True
    )
    return outcome


def _pressed(page, nth: int) -> list[str]:
    return page.evaluate(
        """(n) => Array.from(document.querySelectorAll('.ashby-application-form-input-yesno')[n]
             .querySelectorAll('button[aria-pressed="true"]')).map((b) => b.textContent)""",
        nth,
    )


# --- the scan -----------------------------------------------------------------------


def test_without_autoapply_the_scan_is_the_one_pass_exactly(page, make_page):
    make_page()
    plain = fill.scan_fields(page)
    assert plain == [fill._field_from_raw(raw) for raw in page.evaluate(fill._SCAN_JS)]
    assert not any(">> nth=" in spec.selector for spec in plain)
    assert not any(spec.options == ["Yes", "No"] for spec in plain)
    assert plain == fill.scan_fields(page, autoapply=False)


def test_autoapply_scan_reads_a_yes_no_pair_as_one_radio_field(page, make_page):
    make_page()
    specs = _scan(page)
    auth = _spec(specs, AUTH_Q)
    assert (auth.kind, auth.options, auth.required) == ("radio", ["Yes", "No"], True)
    assert auth.selector == ".ashby-application-form-input-yesno >> nth=0"
    sponsor = _spec(specs, SPONSOR_Q)
    assert (sponsor.kind, sponsor.options, sponsor.required) == ("radio", ["Yes", "No"], False)
    assert sponsor.selector == ".ashby-application-form-input-yesno >> nth=1"
    # The checkbox paired with each pair is hidden from the scan.
    assert not [s for s in specs if s.name.startswith("b5f1-")]


def test_autoapply_scan_finds_the_other_ashby_widgets(page, make_page):
    make_page()
    specs = _scan(page)
    location = _spec(specs, LOCATION_Q)
    assert (location.kind, location.required) == ("text", True)
    assert location.selector == "input.ashby-application-form-input-autocomplete >> nth=0"
    start = _spec(specs, START_Q)
    assert (start.kind, start.required) == ("text", True)
    assert start.selector == "input.ashby-application-form-input-date >> nth=0"
    sms = _spec(specs, SMS_Q)
    assert (sms.kind, sms.required, sms.name) == ("radio", True, "smsConsent")
    assert sms.options == ["I agree to receive text messages", "I do not agree"]
    ack = _spec(specs, "I acknowledge")
    assert (ack.kind, ack.required, ack.selector) == ("checkbox", True, "#ack")
    # An ordinary field is still there, once.
    assert [s.selector for s in specs].count("#legal") == 1


# --- Yes/No buttons -----------------------------------------------------------------


def test_yes_no_fill_presses_the_answer_and_reads_it_back(page, make_page):
    site = make_page()
    specs = _scan(page)
    out = _apply(page, specs, AUTH_Q, "work_authorization", "Yes")
    assert (out.error, out.committed, out.readback) == ("", True, "Yes")
    assert _pressed(page, 0) == ["Yes"]
    assert _pressed(page, 1) == []
    out = _apply(page, specs, SPONSOR_Q, "requires_sponsorship", "No")
    assert (out.error, out.committed, out.readback) == ("", True, "No")
    assert _pressed(page, 1) == ["No"]
    _nothing_submitted(page, site)
    assert submit_guard.installed(page)


def test_the_button_click_is_a_real_submit_that_only_the_guard_stops(page, make_page):
    """The control for the fill tests: the same fill on an unguarded page would have
    submitted (the clicked button is a type-less submit button)."""
    site = make_page(guard=False)
    page.click("button[data-option=yes]")
    page.wait_for_url("**/never-submitted")
    assert site.posts() == [f"{HOST}/never-submitted"]


def test_a_yes_no_already_pressed_is_left_alone(page, make_page):
    site = make_page()
    specs = _scan(page)
    _apply(page, specs, AUTH_Q, "work_authorization", "Yes")
    clicks = len(page.evaluate("window.__clicks"))
    out = _apply(page, specs, AUTH_Q, "work_authorization", "Yes")
    assert (out.error, out.committed) == ("", True)
    assert _pressed(page, 0) == ["Yes"]
    assert len(page.evaluate("window.__clicks")) == clicks  # Ashby would have turned it off
    _nothing_submitted(page, site)


def test_a_yes_no_answer_can_be_changed(page, make_page):
    site = make_page()
    specs = _scan(page)
    _apply(page, specs, AUTH_Q, "work_authorization", "Yes")
    out = _apply(page, specs, AUTH_Q, "work_authorization", "No")
    assert (out.error, out.readback) == ("", "No")
    assert _pressed(page, 0) == ["No"]
    _nothing_submitted(page, site)


def test_an_unrecognised_yes_no_answer_is_a_failed_fill_and_clicks_nothing(page, make_page):
    site = make_page()
    specs = _scan(page)
    out = _apply(page, specs, AUTH_Q, "work_authorization", "Maybe")
    assert out.committed is False and "Maybe" in out.error
    assert page.evaluate("window.__clicks") == []
    _nothing_submitted(page, site)


def test_a_yes_no_that_moved_is_found_by_its_question_not_its_position(page, make_page):
    site = make_page()
    specs = _scan(page)
    # The form re-renders with the two questions swapped: nth=0 now holds the other one.
    page.evaluate(
        """() => { const form = document.getElementById('application');
          const [a, b] = form.querySelectorAll('.ashby-application-form-input-yesno');
          const entryA = a.closest('.ashby-application-form-field-entry');
          const entryB = b.closest('.ashby-application-form-field-entry');
          entryA.before(entryB); }"""
    )
    out = _apply(page, specs, AUTH_Q, "work_authorization", "Yes")
    assert (out.error, out.readback) == ("", "Yes")
    assert _pressed(page, 0) == []  # the sponsorship question, now first, was not touched
    assert _pressed(page, 1) == ["Yes"]
    _nothing_submitted(page, site)


def test_a_yes_no_whose_question_is_gone_is_never_guessed(page, make_page):
    make_page()
    specs = _scan(page)
    page.evaluate(
        """() => document.querySelectorAll('.ashby-application-form-question-title')
             .forEach((t) => { if (t.textContent.startsWith('Are you authorized')) t.textContent = 'Other'; })"""
    )
    out = _apply(page, specs, AUTH_Q, "work_authorization", "Yes")
    assert out.committed is False and "no longer where the scan saw it" in out.error
    assert page.evaluate("window.__clicks") == []


# --- hidden radios and checkboxes ---------------------------------------------------


def test_a_hidden_radio_is_chosen_through_its_label(page, make_page):
    site = make_page()
    specs = _scan(page)
    out = _apply(page, specs, SMS_Q, "sms_consent", "I do not agree")
    assert (out.error, out.committed, out.readback) == ("", True, "I do not agree")
    assert page.is_checked("#sms-not") and not page.is_checked("#sms-given")
    # The label was pressed (the browser then relays a click to the input it names).
    assert page.evaluate("window.__clicks") == ["LABEL", "sms-not"]
    _nothing_submitted(page, site)


def test_a_hidden_radio_already_chosen_is_left_alone(page, make_page):
    make_page()
    specs = _scan(page)
    _apply(page, specs, SMS_Q, "sms_consent", "I agree to receive text messages")
    clicks = len(page.evaluate("window.__clicks"))
    out = _apply(page, specs, SMS_Q, "sms_consent", "I agree to receive text messages")
    assert (out.error, out.committed) == ("", True)
    assert len(page.evaluate("window.__clicks")) == clicks


def test_a_visible_radio_is_still_checked_without_a_click(page, make_page):
    site = make_page()
    page.evaluate("""() => document.querySelectorAll('input[name=smsConsent]')
                       .forEach((r) => r.classList.remove('hide'))""")
    specs = _scan(page)
    out = _apply(page, specs, SMS_Q, "sms_consent", "I do not agree")
    assert (out.error, out.readback) == ("", "I do not agree")
    # .check() on a shown radio, as before: the input itself, never a label.
    assert page.evaluate("window.__clicks") == ["sms-not"]
    _nothing_submitted(page, site)


def test_check_ticks_a_required_box_by_its_label_and_not_its_link(page, make_page):
    site = make_page()
    specs = _scan(page)
    out = _apply(page, specs, "I acknowledge", "acknowledgment", None, action="check")
    assert (out.error, out.committed, out.readback) == ("", True, "checked")
    assert page.is_checked("#ack")
    assert page.evaluate("window.__linkClicks") == 0
    _nothing_submitted(page, site)


def test_check_on_a_box_already_ticked_clicks_nothing(page, make_page):
    make_page()
    specs = _scan(page)
    page.check("#ack", force=True)
    page.evaluate("window.__clicks.length = 0")
    out = _apply(page, specs, "I acknowledge", "acknowledgment", None, action="check")
    assert (out.error, out.committed) == ("", True)
    assert page.evaluate("window.__clicks") == []


def test_check_that_does_not_stick_is_a_failed_fill(page, make_page):
    site = make_page(ack_toggles=False)
    specs = _scan(page)
    out = _apply(page, specs, "I acknowledge", "acknowledgment", None, action="check")
    assert out.committed is False
    assert not page.is_checked("#ack")
    _nothing_submitted(page, site)


def test_check_outside_autoapply_is_never_executed(page, make_page):
    site = make_page()
    specs = _scan(page)
    resolution = _res(_spec(specs, "I acknowledge"), "acknowledgment", "check", None)
    assert fill.apply_resolutions(page, [resolution]) == []
    assert not page.is_checked("#ack")
    assert page.evaluate("window.__clicks") == []
    # The single-field entry point, too, refuses it with a reason.
    out = fill.apply_one(page, resolution)
    assert out.committed is False and "only done by the autoapply lane" in out.error
    assert not page.is_checked("#ack")
    _nothing_submitted(page, site)


def test_check_on_something_that_is_not_a_checkbox_is_refused(page, make_page):
    make_page()
    specs = _scan(page)
    out = _apply(page, specs, LOCATION_Q, "location", "Atlanta, Georgia", action="check")
    assert out.committed is False and "on a checkbox" in out.error
    assert page.evaluate("window.__clicks") == []


# --- the location box ---------------------------------------------------------------


def test_location_is_typed_then_the_matching_option_is_pressed(page, make_page):
    site = make_page()
    specs = _scan(page)
    out = _apply(page, specs, LOCATION_Q, "location", "Atlanta, Georgia")
    assert (out.error, out.committed) == ("", True)
    assert out.readback == "Atlanta, Georgia, United States"
    assert page.input_value("input.ashby-application-form-input-autocomplete") == out.readback
    _nothing_submitted(page, site)


def test_the_postings_city_in_usps_form_picks_the_option_spelling_the_state_out(page, make_page):
    # Ashby's live list (Ramp, 2026-10-24) names the city "New York City, New York, United
    # States"; it has no "New York, New York, ..." option, and "New York, United States" is
    # the state.
    site = make_page()
    specs = _scan(page)
    out = _apply(page, specs, LOCATION_Q, "work_location", "New York, NY")
    assert (out.error, out.committed) == ("", True)
    assert out.readback == "New York City, New York, United States"
    assert page.input_value("input.ashby-application-form-input-autocomplete") == out.readback
    _nothing_submitted(page, site)


def test_the_postings_city_in_another_state_is_not_picked(page, make_page):
    make_page()
    specs = _scan(page)
    out = _apply(page, specs, LOCATION_Q, "work_location", "Boston, NY")
    assert out.committed is False and "no option exactly matching" in out.error
    assert page.input_value("input.ashby-application-form-input-autocomplete") == ""


def test_location_waits_for_a_list_that_is_still_loading(page, make_page):
    site = make_page(delay=400)
    specs = _scan(page)
    out = _apply(page, specs, LOCATION_Q, "location", "Atlanta, Georgia")
    assert (out.error, out.readback) == ("", "Atlanta, Georgia, United States")
    _nothing_submitted(page, site)


def test_location_with_two_matching_options_is_a_failed_fill_and_empties_the_box(page, make_page):
    site = make_page()
    specs = _scan(page)
    out = _apply(page, specs, LOCATION_Q, "location", "Atlanta")
    assert out.committed is False and out.error
    assert page.input_value("input.ashby-application-form-input-autocomplete") == ""
    _nothing_submitted(page, site)


def test_location_with_no_option_is_a_failed_fill_and_typed_text_is_not_left_behind(
    page, make_page
):
    site = make_page()
    specs = _scan(page)
    out = _apply(page, specs, LOCATION_Q, "location", "Narnia")
    assert out.committed is False and out.error
    assert page.input_value("input.ashby-application-form-input-autocomplete") == ""
    _nothing_submitted(page, site)


def test_location_already_the_answer_is_left_alone(page, make_page):
    make_page()
    specs = _scan(page)
    _apply(page, specs, LOCATION_Q, "location", "Atlanta, Georgia")
    page.evaluate("window.__clicks.length = 0")
    out = _apply(page, specs, LOCATION_Q, "location", "Atlanta, Georgia")
    assert (out.error, out.committed) == ("", True)
    assert page.evaluate("window.__clicks") == []


def _set_earliest_start(monkeypatch, value):
    standing = dataclasses.replace(user_preferences().standing_answers, earliest_start=value)
    prefs = dataclasses.replace(user_preferences(), standing_answers=standing)
    monkeypatch.setattr(fill, "user_preferences", lambda: prefs)


# --- the start date -----------------------------------------------------------------


def test_the_start_date_is_the_standing_earliest_start():
    # The template persona's earliest_start; a local preferences file may differ.
    assert fill.autoapply_start_date() == user_preferences().standing_answers.earliest_start
    assert fill.autoapply_start_date() == datetime.date(2027, 1, 4)


def test_an_unset_earliest_start_gives_no_start_date(monkeypatch):
    _set_earliest_start(monkeypatch, None)
    assert fill.autoapply_start_date() is None


def test_a_date_that_takes_typing_is_typed_and_no_calendar_cell_is_pressed(page, make_page):
    site = make_page(typing=True)
    specs = _scan(page)
    out = _apply(page, specs, START_Q, "start_date", fill.autoapply_start_date().isoformat())
    assert (out.error, out.committed, out.readback) == ("", True, "2027-01-04")
    assert page.input_value("input.ashby-application-form-input-date") == "01/04/2027"
    assert page.evaluate("window.__dayClicks") == []
    assert page.evaluate("window.__clicks") == []
    _nothing_submitted(page, site)


def test_a_date_that_ignores_typing_is_picked_in_the_calendar(page, make_page):
    site = make_page(typing=False)
    specs = _scan(page)
    out = _apply(page, specs, START_Q, "start_date", "2027-01-04")
    assert (out.error, out.committed, out.readback) == ("", True, "2027-01-04")
    assert page.input_value("input.ashby-application-form-input-date") == "01/04/2027"
    # October 2026 to January 2027 is three month buttons and one day cell: not the
    # outside-month "January 4th" the December page shows.
    assert page.evaluate("window.__dayClicks") == ["Choose Monday, January 4th, 2027"]
    _nothing_submitted(page, site)


def test_a_date_already_the_answer_is_left_alone(page, make_page):
    make_page(typing=True)
    specs = _scan(page)
    _apply(page, specs, START_Q, "start_date", "2027-01-04")
    page.evaluate("window.__clicks.length = 0")
    out = _apply(page, specs, START_Q, "start_date", "2027-01-04")
    assert (out.error, out.committed) == ("", True)
    assert page.evaluate("window.__clicks") == []


def test_text_that_is_not_a_date_is_a_failed_fill(page, make_page):
    make_page()
    specs = _scan(page)
    out = _apply(page, specs, START_Q, "start_date", "ASAP")
    assert out.committed is False and "not a date" in out.error
    assert page.evaluate("window.__clicks") == []


def test_the_start_date_picker_gets_the_standing_date_whatever_was_stored():
    picker = FieldSpec(
        selector="input.ashby-application-form-input-date >> nth=0", kind="text", label=START_Q
    )
    ask = _res(picker, "start_date", "ask", None)
    [adapted] = fill._adapt_to_date_picker([ask])
    assert (adapted.action, adapted.value) == ("fill", "2027-01-04")
    stored = _res(picker, "start_date", "fill", "ASAP")
    assert fill._adapt_to_date_picker([stored])[0].value == "2027-01-04"


def test_the_start_date_picker_is_left_to_resolve_with_no_earliest_start(monkeypatch):
    _set_earliest_start(monkeypatch, None)
    picker = FieldSpec(
        selector="input.ashby-application-form-input-date >> nth=0", kind="text", label=START_Q
    )
    ask = _res(picker, "start_date", "ask", None)
    stored = _res(picker, "start_date", "fill", "ASAP")
    assert fill._adapt_to_date_picker([ask, stored]) == [ask, stored]


def test_other_resolutions_pass_through_the_date_adapter_untouched():
    picker = FieldSpec(selector="input.ashby-application-form-input-date >> nth=0", kind="text")
    other_date = _res(picker, "graduation_date", "ask", None)
    plain_start = _res(FieldSpec(selector="#start", kind="text"), "start_date", "ask", None)
    drafted = Resolution(picker, "start_date", "fill", "ASAP", draft=True)
    skipped = _res(picker, "start_date", "skip", None)
    resolutions = [other_date, plain_start, drafted, skipped]
    assert fill._adapt_to_date_picker(resolutions) == resolutions


# --- no guard, no click -------------------------------------------------------------

_WIDGET_CASES = [
    (AUTH_Q, "work_authorization", "Yes", "fill"),
    (SMS_Q, "sms_consent", "I do not agree", "fill"),
    ("I acknowledge", "acknowledgment", None, "check"),
    (LOCATION_Q, "location", "Atlanta, Georgia", "fill"),
    (START_Q, "start_date", "2027-01-04", "fill"),
]


@pytest.mark.parametrize("label,key,value,action", _WIDGET_CASES)
def test_fill_refuses_every_click_on_a_page_without_the_guard(
    page, make_page, label, key, value, action
):
    site = make_page(guard=False)
    specs = _scan(page)
    assert not submit_guard.installed(page)
    out = _apply(page, specs, label, key, value, action=action)
    assert out.committed is False
    assert "submit guard is not installed" in out.error
    assert page.evaluate("window.__clicks") == []
    assert page.evaluate("window.__submitted") == []
    assert site.posts() == []
    assert _pressed(page, 0) == [] and not page.is_checked("#ack")
    assert not page.is_checked("#sms-given") and not page.is_checked("#sms-not")
    assert page.input_value("input.ashby-application-form-input-date") == ""
    assert page.input_value("input.ashby-application-form-input-autocomplete") == ""


def test_fill_refuses_after_the_guard_was_lifted(page, make_page):
    site = make_page()
    specs = _scan(page)
    submit_guard.lift(page)
    out = _apply(page, specs, AUTH_Q, "work_authorization", "Yes")
    assert out.committed is False and "submit guard is not installed" in out.error
    assert page.evaluate("window.__clicks") == []
    assert site.posts() == []


def test_an_ashby_widget_is_not_filled_outside_autoapply(page, make_page):
    site = make_page()
    specs = _scan(page)
    yes_no = _res(_spec(specs, AUTH_Q), "work_authorization", "fill", "Yes")
    date = _res(_spec(specs, START_Q), "start_date", "fill", "2027-01-04")
    outcomes = fill.apply_resolutions(page, [yes_no, date])
    assert [o.committed for o in outcomes] == [False, False]
    assert all("only filled by the autoapply lane" in o.error for o in outcomes)
    assert page.evaluate("window.__clicks") == []
    assert _pressed(page, 0) == []
    _nothing_submitted(page, site)


def test_a_stray_click_after_the_fill_is_still_caught(page, make_page):
    site = make_page()
    specs = _scan(page)
    _apply(page, specs, AUTH_Q, "work_authorization", "Yes")
    assert submit_guard.blockers(page) == []
    page.click("button[data-option=no]")  # not through fill: unmarked
    assert page.evaluate("window.__submitted") == []
    assert site.posts() == []
    [blocker] = submit_guard.blockers(page)
    assert blocker.startswith("submit_guard: ")


# --- run_assist ---------------------------------------------------------------------


def _fake_resolve(calls):
    """Stands in for solve.resolve, which another builder is changing: records how it
    was called and answers each Ashby widget the way the stored answers would."""

    def fake(fields, answers, resume_path, key_overrides=None, company=None, **kwargs):
        calls.append(kwargs)
        plan = {
            AUTH_Q: ("work_authorization", "fill", "Yes"),
            SMS_Q: ("sms_consent", "fill", "I do not agree"),
            "I acknowledge": ("acknowledgment", "check", None),
            LOCATION_Q: ("location", "fill", "Atlanta, Georgia"),
            START_Q: ("start_date", "ask", None),
        }
        out = []
        for spec in fields:
            hit = next((v for label, v in plan.items() if spec.label.startswith(label)), None)
            if hit is None:
                out.append(Resolution(spec, None, "skip"))
            else:
                key, action, value = hit
                out.append(Resolution(spec, key, action, value))
        return out

    return fake


def test_run_assist_in_autoapply_scans_resolves_and_fills_behind_the_guard(
    page, make_page, monkeypatch
):
    site = make_page(typing=True)
    calls: list[dict] = []
    monkeypatch.setattr(solve, "resolve", _fake_resolve(calls))
    digest = fill.run_assist(page, 7, URL, {}, None, autoapply=True)
    assert calls == [{"autoapply": True}]
    assert digest.errors == []
    done = {o.field.label[:12]: o for o in digest.filled}
    assert all(o.committed and not o.error for o in done.values()), digest.filled
    assert len(digest.filled) == 5
    assert _pressed(page, 0) == ["Yes"]
    assert page.is_checked("#sms-not") and page.is_checked("#ack")
    assert page.input_value("input.ashby-application-form-input-date") == "01/04/2027"
    assert page.input_value("input.ashby-application-form-input-autocomplete").startswith("Atlanta")
    assert page.evaluate("window.__linkClicks") == 0
    _nothing_submitted(page, site)


def test_run_assist_in_autoapply_on_an_unguarded_page_clicks_nothing(
    page, make_page, monkeypatch
):
    site = make_page(guard=False)
    monkeypatch.setattr(solve, "resolve", _fake_resolve([]))
    digest = fill.run_assist(page, 7, URL, {}, None, autoapply=True)
    clicked = [o for o in digest.filled if "submit guard is not installed" in o.error]
    assert len(clicked) == 5  # all five Ashby widgets refused, the date picker included
    assert len(digest.filled) == 5 and not any(o.committed for o in digest.filled)
    assert page.evaluate("window.__clicks") == []
    assert site.posts() == []


def test_run_assist_without_autoapply_resolves_as_before_and_never_checks(
    page, make_page, monkeypatch
):
    site = make_page()
    seen: list[tuple] = []
    plan = _fake_resolve([])

    def fake(*args, **kwargs):
        seen.append((len(args), kwargs))
        return plan(*args, **kwargs)

    monkeypatch.setattr(solve, "resolve", fake)
    digest = fill.run_assist(page, 7, URL, {}, None)
    assert seen == [(5, {})]  # exactly today's call: no autoapply keyword
    # The Ashby widgets are not scanned, so none of them was filled; the one "check" the
    # plan would have made is not even a field of the plain scan.
    assert not page.is_checked("#ack")
    assert digest.filled == []
    assert page.evaluate("window.__clicks") == []
    _nothing_submitted(page, site)


def test_run_assist_turns_a_check_into_a_question_outside_autoapply(page, make_page, monkeypatch):
    make_page()
    ack = FieldSpec(selector="#ack", kind="checkbox", label="I acknowledge")

    def fake(fields, *args, **kwargs):
        return [Resolution(ack, "acknowledgment", "check", None)]

    monkeypatch.setattr(solve, "resolve", fake)
    digest = fill.run_assist(page, 7, URL, {}, None)
    assert [r.field.selector for r in digest.residual] == ["#ack"]
    assert [r.action for r in digest.residual] == ["ask"]
    assert digest.filled == []
    assert not page.is_checked("#ack")


# --- the source -----------------------------------------------------------------------


def _fill_tree() -> ast.Module:
    return ast.parse(Path(fill.__file__).read_text())


def _calls_to(tree: ast.AST, name: str) -> list[tuple[str, ast.Call]]:
    found = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    func = node.func
                    called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if called == name:
                        found.append((fn.name, node))
    return found


def test_the_only_click_in_fill_is_click_answer_inside__click_answer():
    tree = _fill_tree()
    owners = {name for name, _ in _calls_to(tree, "click_answer")}
    assert owners == {"_click_answer"}
    for forbidden in ("click", "dblclick", "tap", "dispatch_event", "submit", "request_submit"):
        assert not _calls_to(tree, forbidden), forbidden


def test__click_answer_checks_the_guard_before_it_clicks():
    fn = next(
        node
        for node in ast.walk(_fill_tree())
        if isinstance(node, ast.FunctionDef) and node.name == "_click_answer"
    )
    calls = [
        (node.lineno, node.func.id if isinstance(node.func, ast.Name) else node.func.attr)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
    ]
    names = [name for _, name in sorted(calls)]
    assert names.index("_require_guard") < names.index("click_answer")


def test_every_widget_flow_checks_the_guard_before_anything_else():
    tree = _fill_tree()
    flows = {
        "_fill_yes_no",
        "_pick_hidden_radio",
        "_check_box",
        "_fill_autocomplete",
        "_fill_date",
    }
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name in flows:
            flows.discard(fn.name)
            first = next(s for s in fn.body if not isinstance(s, ast.Expr) or not isinstance(
                s.value, ast.Constant))
            assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call), fn.name
            assert first.value.func.id == "_require_guard", fn.name
    assert not flows, f"flows not found: {flows}"


def test_fill_never_presses_enter_or_escape():
    keys = {
        call.args[0].value
        for _, call in _calls_to(_fill_tree(), "press")
        if call.args and isinstance(call.args[0], ast.Constant)
    }
    assert keys <= {"Tab", "ArrowDown"}
