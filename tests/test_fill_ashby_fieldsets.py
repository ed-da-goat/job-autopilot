"""Ashby's bare-fieldset question groups: found by the armed scan, resolved, and clicked.

Dry run 6 (2026-10-24) left required questions unanswered because the second scan
pass looked for each group's title only inside `.ashby-application-form-field-entry`:
  - OpenAI's acknowledgment boxes sit in a fieldset with no field entry around it, so
    the group had no title and was skipped. The box's own text ("I confirm I have read
    the above.") also says nothing about what is being certified.
  - Rogo's Yes/No radios are the same shape.
The fixture below has those shapes, with made-up questions and names. Nothing here
opens a real site: the page is served from a route on a fake host, the dry-run submit
guard is installed, and every test checks that nothing was submitted."""

from __future__ import annotations

import pytest

from jobhunt.apply_assist import fill, solve, submit_guard
from jobhunt.apply_assist.keys import CHECKBOX_TITLE_SEP
from tests import test_fill_browser as _browser_fixtures

browser = _browser_fixtures.browser
page = _browser_fixtures.page
fast_waits = _browser_fixtures.fast_waits

HOST = "https://jobs.example.test"
URL = f"{HOST}/acme/0b2ac1d4-6f46-4f6b-ad9e-a258e642751c/application"  # made up

ARB_TITLE = "Applicant Arbitration Agreement Acknowledgement"
ARB_BOX = "I acknowledge that I have opened, read, and understood the Arbitration Agreement"
CERT_TITLE = (
    "I hereby certify that I have not knowingly withheld any information that might "
    "adversely affect my chances for employment"
)
CERT_BOX = "I confirm I have read the above."
OFFICE_Q = "Are you able, willing, and excited to work full-time in an office?"
DAYS_Q = "Are you able to work from our office three days per week?"
AUTH_Q = "Are you legally authorized to work in the U.S.?"
SPONSOR_Q = "Will you now or in the future require sponsorship for an immigration-related benefit?"
YEARS_Q = "How many years of relevant experience do you have?"

_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  body { font: 14px sans-serif; margin: 24px; }
  ._required_f7cvd_91::after { content: "*"; }
  .hide { position: absolute; opacity: 0; width: 1px; height: 1px; }
</style></head><body>
<form id="application" action="/never-submitted" method="post" novalidate>
  <div class="ashby-application-form-section-container">

    <!-- OpenAI: checkbox groups in a bare fieldset, titled by a heading label whose class
         is not the question-title one, one hidden box named after its own text -->
    <fieldset class="ashby-application-form-input-group">
      <label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42">__ARB_TITLE__</label>
      <div class="ashby-application-form-question-description"><p>Applicant Arbitration
        Agreement</p></div>
      <div class="ashby-application-form-input-checkbox-group-option">
        <span class="ashby-application-form-input-checkbox-group-option-checkbox">
          <input class="hide" type="checkbox" name="__ARB_BOX__" id="arb"></span>
        <label for="arb">__ARB_BOX__</label></div>
    </fieldset>

    <fieldset class="ashby-application-form-input-group">
      <label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42">__CERT_TITLE__</label>
      <div class="ashby-application-form-input-checkbox-group-option">
        <span class="ashby-application-form-input-checkbox-group-option-checkbox">
          <input class="hide" type="checkbox" name="__CERT_BOX__" id="cert"></span>
        <label for="cert">__CERT_BOX__</label></div>
    </fieldset>

    <!-- OpenAI: an optional box in the same shape stays unchecked -->
    <fieldset class="ashby-application-form-input-group">
      <label class="_heading_f7cvd_52 _label_1e3gg_42">Stay in touch</label>
      <div class="ashby-application-form-input-checkbox-group-option">
        <input class="hide" type="checkbox" name="talent" id="talent">
        <label for="talent">Add me to your talent community for future roles</label></div>
    </fieldset>

    <!-- Rogo: Yes/No buttons in a field entry, and Yes/No radios in a bare fieldset -->
    <div class="ashby-application-form-field-entry">
      <label class="ashby-application-form-question-title _required_f7cvd_91">__OFFICE_Q__</label>
      <div class="ashby-application-form-input-yesno"><div class="container">
        <div class="ashby-application-form-input-yesno-option">
          <button type="button" aria-pressed="false">Yes</button></div>
        <div class="ashby-application-form-input-yesno-option">
          <button type="button" aria-pressed="false">No</button></div>
        <input class="hide" type="checkbox" tabindex="-1" name="b5f1-office">
      </div></div>
    </div>

    <div class="ashby-application-form-field-entry">
      <label class="ashby-application-form-question-title _required_f7cvd_91">__DAYS_Q__</label>
      <div class="ashby-application-form-input-yesno"><div class="container">
        <div class="ashby-application-form-input-yesno-option">
          <button type="button" aria-pressed="false">Yes</button></div>
        <div class="ashby-application-form-input-yesno-option">
          <button type="button" aria-pressed="false">No</button></div>
        <input class="hide" type="checkbox" tabindex="-1" name="b5f1-days">
      </div></div>
    </div>

    <div class="ashby-application-form-field-entry">
      <label class="ashby-application-form-question-title _required_f7cvd_91"
             for="years">__YEARS_Q__</label>
      <input id="years" name="years" type="text" required
             class="ashby-application-form-input-text">
    </div>

    <fieldset class="ashby-application-form-input-group">
      <label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42">__AUTH_Q__</label>
      <div class="option"><input type="radio" name="auth-q" id="auth-yes" value="yes">
        <label for="auth-yes">Yes</label></div>
      <div class="option"><input type="radio" name="auth-q" id="auth-no" value="no">
        <label for="auth-no">No</label></div>
    </fieldset>

    <fieldset class="ashby-application-form-input-group">
      <label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42">__SPONSOR_Q__</label>
      <div class="option"><input class="hide" type="radio" name="sponsor-q" id="sp-yes" value="yes">
        <label for="sp-yes">Yes</label></div>
      <div class="option"><input class="hide" type="radio" name="sponsor-q" id="sp-no" value="no">
        <label for="sp-no">No</label></div>
    </fieldset>
  </div>
  <button id="send">Submit Application</button>
</form>
<script>
  window.__submitted = [];
  document.addEventListener('submit', (e) => window.__submitted.push(e.target.id), true);
  for (const group of document.querySelectorAll('.ashby-application-form-input-yesno')) {
    for (const button of group.querySelectorAll('button')) {
      button.addEventListener('click', () => {
        const wasOn = button.getAttribute('aria-pressed') === 'true';
        for (const other of group.querySelectorAll('button')) other.setAttribute('aria-pressed', 'false');
        if (!wasOn) button.setAttribute('aria-pressed', 'true');
      });
    }
  }
</script>
</body></html>"""


class Site:
    def __init__(self, tab):
        self.hits: list[tuple[str, str]] = []
        html = (
            _PAGE.replace("__ARB_TITLE__", ARB_TITLE)
            .replace("__ARB_BOX__", ARB_BOX)
            .replace("__CERT_TITLE__", CERT_TITLE)
            .replace("__CERT_BOX__", CERT_BOX)
            .replace("__OFFICE_Q__", OFFICE_Q)
            .replace("__DAYS_Q__", DAYS_Q)
            .replace("__AUTH_Q__", AUTH_Q)
            .replace("__SPONSOR_Q__", SPONSOR_Q)
            .replace("__YEARS_Q__", YEARS_Q)
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
def site(page, fast_waits):
    fake = Site(page)
    submit_guard.install(page)
    page.goto(URL)
    page.wait_for_selector("#application")
    return fake


def _find(specs, text):
    found = [s for s in specs if text in s.label]
    assert len(found) == 1, [(s.label, s.selector) for s in specs]
    return found[0]


def _nothing_submitted(page, site) -> None:
    assert page.evaluate("window.__submitted") == []
    assert site.posts() == []
    assert submit_guard.blockers(page) == []


def test_the_scan_finds_a_checkbox_group_that_sits_in_a_bare_fieldset(page, site):
    specs = fill.scan_fields(page, autoapply=True)
    arb = _find(specs, ARB_TITLE)
    # The box's own text first, then the group's title: the title says what is certified.
    assert arb.label == f"{ARB_BOX}{CHECKBOX_TITLE_SEP}{ARB_TITLE}"
    assert (arb.kind, arb.required, arb.selector) == ("checkbox", True, "#arb")
    cert = _find(specs, CERT_TITLE)
    assert cert.label == f"{CERT_BOX}{CHECKBOX_TITLE_SEP}{CERT_TITLE}"
    assert (cert.kind, cert.required) == ("checkbox", True)
    talent = _find(specs, "talent community")
    assert (talent.kind, talent.required) == ("checkbox", False)
    # Each box is in the list once: the base scan's own copy is replaced in place.
    assert [s.selector for s in specs].count("#arb") == 1


def test_the_scan_finds_yes_no_radios_that_sit_in_a_bare_fieldset(page, site):
    specs = fill.scan_fields(page, autoapply=True)
    auth = _find(specs, AUTH_Q)
    assert (auth.kind, auth.required, auth.name) == ("radio", True, "auth-q")
    assert auth.options == ["Yes", "No"]
    # The same group with its inputs hidden behind their labels.
    sponsor = _find(specs, SPONSOR_Q)
    assert (sponsor.kind, sponsor.required, sponsor.name) == ("radio", True, "sponsor-q")
    assert sponsor.options == ["Yes", "No"]
    assert [s.name for s in specs].count("auth-q") == 1
    # The Yes/No buttons were already found, and their hidden boxes stay out of the list.
    assert _find(specs, OFFICE_Q).options == ["Yes", "No"]
    assert not [s for s in specs if s.name.startswith("b5f1-")]


def test_without_autoapply_the_scan_adds_nothing_for_the_fieldsets(page, site):
    plain = fill.scan_fields(page)
    assert not any(s.label.endswith(ARB_TITLE) for s in plain)
    assert not any(CHECKBOX_TITLE_SEP in s.label for s in plain)


def test_the_armed_lane_resolves_and_clicks_every_group_dry_run_6_missed(page, site):
    answers = {"work_authorization": "Yes", "sponsorship": "No"}
    fields = fill.scan_fields(page, autoapply=True)
    resolutions = solve.resolve(fields, answers, None, autoapply=True)
    by_text = {r.field.label: r for r in resolutions}

    def res(text):
        return next(r for label, r in by_text.items() if text in label)

    assert res(ARB_TITLE).action == "check"
    assert res(CERT_TITLE).action == "check"
    assert res("talent community").action == "ask"
    # The rules answer these two: no stored answer is needed.
    assert (res(OFFICE_Q).semantic_key, res(OFFICE_Q).value) == ("onsite_ok", "Yes")
    assert (res(DAYS_Q).semantic_key, res(DAYS_Q).value) == ("onsite_ok", "Yes")
    assert (res(YEARS_Q).semantic_key, res(YEARS_Q).value) == ("years_experience", "2")
    assert (res(AUTH_Q).action, res(AUTH_Q).value) == ("fill", "Yes")
    assert (res(SPONSOR_Q).action, res(SPONSOR_Q).value) == ("fill", "No")

    outcomes = fill.apply_resolutions(page, resolutions, autoapply=True)
    failed = [(o.field.label, o.error) for o in outcomes if o.error or not o.committed]
    assert failed == []
    assert page.is_checked("#arb") and page.is_checked("#cert")
    assert not page.is_checked("#talent")
    assert page.is_checked("#auth-yes") and not page.is_checked("#auth-no")
    assert page.is_checked("#sp-no") and not page.is_checked("#sp-yes")
    assert page.input_value("#years") == "2"
    pressed = page.evaluate(
        """() => Array.from(document.querySelectorAll('button[aria-pressed="true"]'))
                 .map((b) => b.closest('.ashby-application-form-field-entry')
                      .querySelector('label').textContent + ' = ' + b.textContent)"""
    )
    assert pressed == [f"{OFFICE_Q} = Yes", f"{DAYS_Q} = Yes"]
    _nothing_submitted(page, site)
    assert submit_guard.installed(page)


def test_the_fieldset_groups_are_not_clicked_on_a_page_without_the_guard(page, fast_waits):
    fake = Site(page)  # no guard installed
    page.goto(URL)
    page.wait_for_selector("#application")
    fields = fill.scan_fields(page, autoapply=True)
    resolutions = solve.resolve(
        fields, {"work_authorization": "Yes", "sponsorship": "No"}, None, autoapply=True
    )
    outcomes = fill.apply_resolutions(page, resolutions, autoapply=True)
    assert not page.is_checked("#arb") and not page.is_checked("#cert")
    assert page.evaluate("window.__submitted") == [] and fake.posts() == []
    refused = [o for o in outcomes if "submit guard is not installed" in o.error]
    assert refused  # the clicks (the two boxes, the hidden radios, the buttons) were refused


_GREENHOUSE_SHAPED = """<!doctype html><html><body>
<form id="application_form">
  <div id="main_fields">
    %s
    <fieldset>
      <legend>Applicant Arbitration Agreement Acknowledgement</legend>
      <label><input type="checkbox" name="arb" id="arb"> I confirm I have read the above.</label>
    </fieldset>
  </div>
</form></body></html>"""


def test_a_greenhouse_shaped_fieldset_gets_no_title_suffix(page):
    page.set_content(_GREENHOUSE_SHAPED % "")
    specs = fill.scan_fields(page, autoapply=True)
    box = [s for s in specs if s.selector == "#arb"]
    assert len(box) == 1
    assert CHECKBOX_TITLE_SEP not in box[0].label
    assert not any(CHECKBOX_TITLE_SEP in s.label for s in specs)
    assert box[0].label == fill.scan_fields(page)[0].label


def test_the_same_fieldset_inside_the_ashby_form_container_does_get_the_suffix(page):
    html = _GREENHOUSE_SHAPED.replace(
        '<div id="main_fields">',
        '<div id="main_fields" class="ashby-application-form-section-container">',
    )
    page.set_content(html % "")
    specs = fill.scan_fields(page, autoapply=True)
    (box,) = [s for s in specs if s.selector == "#arb"]
    assert box.label.endswith(f"{CHECKBOX_TITLE_SEP}Applicant Arbitration Agreement Acknowledgement")


# Spade (2026-10-26, posting 315486): "Where did you hear about Spade?" is an autocomplete
# box in a bare fieldset, so it sits in no field entry. The scan reads its title from the
# fieldset; the readback that finds the widget again read it only from a field entry, saw
# no title, and blocked the posting with "no longer where the scan saw it".
HOW_HEARD_Q = "Where did you hear about Spade?"
_BARE_COMBOBOX = """<!doctype html><html><body>
<form id="application">
  <div class="ashby-application-form-container">
    <div class="ashby-application-form-field-entry">
      <label class="ashby-application-form-question-title _required_f7cvd_91">Location</label>
      <input class="ashby-application-form-input-autocomplete" role="combobox"
             aria-expanded="false" placeholder="Start typing...">
    </div>
    <fieldset class="_container_wz442_28 _fieldEntry_1e3gg_28">
      <label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42
                    ashby-application-form-question-title" for="x">%s</label>
      <div class="_inputContainer_d7ago_28">
        <input class="_input_d7ago_28 ashby-application-form-input-autocomplete"
               placeholder="Start typing..." aria-autocomplete="list" aria-expanded="false"
               role="combobox" value="">
      </div>
    </fieldset>
  </div>
</form></body></html>"""


def test_a_combobox_in_a_bare_fieldset_is_found_again_by_its_title(page):
    page.set_content(_BARE_COMBOBOX % HOW_HEARD_Q)
    how = _find(fill.scan_fields(page, autoapply=True), HOW_HEARD_Q)
    assert how.selector == "input.ashby-application-form-input-autocomplete >> nth=1"
    found = fill._widget_locator(page, how)
    assert found.count() == 1
    assert found.evaluate("(el) => el.closest('fieldset') !== null")


def test_a_combobox_whose_fieldset_title_changed_is_still_not_guessed(page):
    page.set_content(_BARE_COMBOBOX % HOW_HEARD_Q)
    how = _find(fill.scan_fields(page, autoapply=True), HOW_HEARD_Q)
    page.evaluate(
        "document.querySelector('fieldset label').textContent = 'Something else entirely'"
    )
    with pytest.raises(ValueError, match="no longer where the scan saw it"):
        fill._widget_locator(page, how)
