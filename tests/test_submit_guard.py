"""The dry-run submit guard (jobhunt/apply_assist/submit_guard.py).

The candidate's condition for letting fill.py click Ashby's Yes/No answer buttons
(2026-10-24, Q7): before the first click on a real form, a test shows that the
dry-run submit guard stops any submission. The proofs below run in a real
headless Chromium on in-memory pages. The `page` fixture's context aborts every
http(s) request; each test's own route plays the ATS and logs what reaches it,
so "no submission" means three things at once: no page-level submit handler
ran, the form never POSTed, and the page never navigated.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from jobhunt.apply_assist import submit_guard
from tests import test_fill_browser as _browser_fixtures

# The headless-Chromium fixtures (the page's context aborts every http(s)
# request). Bound by assignment rather than imported by name, so ruff does not
# read each test's `page` parameter as a redefinition of an unused import.
browser = _browser_fixtures.browser
page = _browser_fixtures.page

UUID = "0b2ac1d4-6f46-4f6b-ad9e-a258e642751c"  # made up
ELSEWHERE = "https://careers.example.test"  # not an ATS host: only the DOM layer can act

# A form like Ashby's: the buttons have no `type`, so the browser treats them as
# submit buttons. The page records any submit event it is allowed to see.
FORM = """<html><body>
<form id="application-form" action="/post-target" method="post">
  <input id="note" name="note" value="x">
  <button id="plain">Plain</button>
  <button id="answer" aria-pressed="false">Yes</button>
  <button id="inert" type="button">Not a submit</button>
</form>
<script>
  window.__submitted = [];
  document.getElementById("application-form").addEventListener(
    "submit", () => window.__submitted.push("form"));
  document.addEventListener("submit", () => window.__submitted.push("document"), true);
</script>
</body></html>"""

_FETCH_POST = (
    "([url, mode]) => fetch(url, {method: 'POST', body: '{}', mode: mode || 'cors'})"
    ".then(() => 'sent', () => 'aborted')"
)
_CORS = {
    "access-control-allow-origin": "*",
    "access-control-allow-headers": "*",
    "access-control-allow-methods": "*",
}


class Site:
    """Plays the ATS. Register it BEFORE `submit_guard.install`: of several page
    routes the last one registered runs first, and the guard hands everything it
    does not stop on with `route.fallback()`."""

    def __init__(self, tab, pages: dict[str, str] | None = None):
        self.hits: list[tuple[str, str]] = []
        self.pages = pages or {}
        tab.route("**/*", self._handle)

    def _handle(self, route):
        request = route.request
        self.hits.append((request.method, request.url))
        if request.method == "GET":
            body = self.pages.get(request.url, FORM)
            route.fulfill(status=200, content_type="text/html", body=body, headers=_CORS)
        else:
            route.fulfill(status=200, content_type="application/json", body="{}", headers=_CORS)

    def posts(self) -> list[str]:
        return [url for method, url in self.hits if method == "POST"]


@pytest.fixture
def site(page):
    return Site(page)


def _no_submission(page, site) -> None:
    """The three observable signs of a submission are all absent."""
    assert page.evaluate("window.__submitted") == []
    assert site.posts() == []
    assert "post-target" not in page.url


# --- the control: the harness really sees a submission ------------------------------


def test_control_an_unguarded_click_submits(page, site):
    page.goto(f"{ELSEWHERE}/apply")
    page.click("#plain")
    page.wait_for_url("**/post-target")
    assert site.posts() == [f"{ELSEWHERE}/post-target"]


# --- 1 and 2: clicks ---------------------------------------------------------------


def test_unmarked_typeless_button_click_is_stopped_and_recorded(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    page.click("#plain")
    _no_submission(page, site)
    found = submit_guard.blockers(page)
    assert len(found) == 1
    assert found[0].startswith("submit_guard: ")
    assert "form#application-form" in found[0]
    assert 'button "Plain"' in found[0]


def test_marked_button_click_is_stopped_and_not_recorded(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    submit_guard.mark_answer_click(page.locator("#answer"))
    page.click("#answer")
    _no_submission(page, site)
    assert submit_guard.blockers(page) == []
    # the mark is cleared after use
    assert page.evaluate("document.getElementById('answer').hasAttribute('data-jh-answer-click')") is False


def test_a_mark_is_good_for_one_submit_only(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    submit_guard.mark_answer_click(page.locator("#answer"))
    page.click("#answer")
    page.click("#answer")  # unmarked now
    _no_submission(page, site)
    assert len(submit_guard.blockers(page)) == 1


def test_a_mark_on_a_different_button_does_not_excuse_the_click(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    submit_guard.mark_answer_click(page.locator("#answer"))
    page.click("#plain")
    _no_submission(page, site)
    found = submit_guard.blockers(page)
    assert len(found) == 1 and 'button "Plain"' in found[0]


def test_a_stale_mark_is_not_honoured(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    page.evaluate(
        "document.getElementById('answer').setAttribute('data-jh-answer-click', String(Date.now() - 60000))"
    )
    page.click("#answer")
    _no_submission(page, site)
    assert len(submit_guard.blockers(page)) == 1


def test_clear_answer_mark_removes_a_mark_that_was_never_used(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    inert = page.locator("#inert")
    submit_guard.mark_answer_click(inert)
    assert inert.get_attribute("data-jh-answer-click")
    submit_guard.clear_answer_mark(inert)
    assert inert.get_attribute("data-jh-answer-click") is None


def test_clear_answer_mark_never_raises(page):
    page.set_content("<p>nothing here</p>")
    submit_guard.clear_answer_mark(page.locator("#missing"))  # times out inside, swallowed


def test_clear_answer_mark_does_not_wait_for_an_element_that_is_gone(page):
    page.set_content("<p>nothing here</p>")
    started = time.monotonic()
    submit_guard.clear_answer_mark(page.locator("#missing"))
    assert time.monotonic() - started < 1.5  # not the three-second evaluate timeout


# --- click_answer: the one click fill.py makes ---------------------------------------


def _count_clicks(page, selector: str) -> None:
    page.evaluate(
        "sel => document.querySelector(sel).addEventListener("
        "'click', () => { window.__clicks = (window.__clicks || 0) + 1; })",
        selector,
    )


def test_click_answer_refuses_a_page_without_the_guard(page, site):
    page.goto(f"{ELSEWHERE}/apply")
    _count_clicks(page, "#answer")
    with pytest.raises(RuntimeError, match="not installed"):
        submit_guard.click_answer(page, page.locator("#answer"))
    assert page.evaluate("window.__clicks || 0") == 0  # the button was never clicked
    _no_submission(page, site)
    assert not page.locator("#answer").get_attribute("data-jh-answer-click")  # not even marked


def test_click_answer_refuses_before_touching_the_element():
    never_installed = MagicMock(name="locator")
    with pytest.raises(RuntimeError, match="refusing to click"):
        submit_guard.click_answer(object(), never_installed)
    assert never_installed.method_calls == []


def test_click_answer_refuses_a_page_whose_guard_was_lifted(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    _count_clicks(page, "#answer")
    submit_guard.lift(page)
    with pytest.raises(RuntimeError, match="not installed"):
        submit_guard.click_answer(page, page.locator("#answer"))
    assert page.evaluate("window.__clicks || 0") == 0
    _no_submission(page, site)


def test_click_answer_refuses_an_element_in_a_frame_without_the_dom_layer(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    locator = MagicMock(name="locator")
    locator.evaluate.return_value = False  # the element's frame has no (or a switched-off) DOM layer
    with pytest.raises(RuntimeError, match="DOM layer is missing in the element's frame"):
        submit_guard.click_answer(page, locator)
    locator.click.assert_not_called()
    assert [c.args[0] for c in locator.evaluate.call_args_list] == [submit_guard._DOM_LAYER_FN]  # not even marked


def test_click_answer_refuses_when_the_frame_cannot_be_checked(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    locator = MagicMock(name="locator")
    locator.evaluate.side_effect = PlaywrightTimeout("frame detached")
    with pytest.raises(RuntimeError, match="could not confirm the submit guard in the element's frame"):
        submit_guard.click_answer(page, locator)
    locator.click.assert_not_called()


def test_click_answer_refuses_an_element_whose_dom_layer_was_switched_off(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    _count_clicks(page, "#answer")
    page.evaluate("window.__jhSubmitGuardOff = true")  # what lift does to a document
    with pytest.raises(RuntimeError, match="DOM layer is missing"):
        submit_guard.click_answer(page, page.locator("#answer"))
    assert page.evaluate("window.__clicks || 0") == 0


def test_click_answer_clicks_once_and_the_stopped_submit_is_not_a_blocker(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    _count_clicks(page, "#answer")
    submit_guard.click_answer(page, page.locator("#answer"))
    assert page.evaluate("window.__clicks") == 1  # the click really happened
    _no_submission(page, site)  # ... and the type-less button did not submit the form
    assert submit_guard.blockers(page) == []
    assert page.locator("#answer").get_attribute("data-jh-answer-click") is None


def test_click_answer_does_not_excuse_a_second_unmarked_click(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    submit_guard.click_answer(page, page.locator("#answer"))
    page.click("#answer")  # a plain click afterwards is a submit attempt again
    _no_submission(page, site)
    assert len(submit_guard.blockers(page)) == 1


def test_click_answer_clears_the_mark_when_the_click_fails(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    page.evaluate("document.getElementById('answer').disabled = true")
    with pytest.raises(PlaywrightTimeout):
        submit_guard.click_answer(page, page.locator("#answer"), timeout=300)
    assert page.locator("#answer").get_attribute("data-jh-answer-click") is None
    _no_submission(page, site)


def test_click_answer_aims_at_the_position_it_is_given(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    page.evaluate(
        "document.getElementById('answer').addEventListener('click', e => {"
        " const box = e.currentTarget.getBoundingClientRect();"
        " window.__where = [Math.round(e.clientX - box.left), Math.round(e.clientY - box.top)]; })"
    )
    page.evaluate(  # whole-pixel box, no border: offsets count from the padding box
        "Object.assign(document.getElementById('answer').style,"
        " {border: '0', position: 'absolute', left: '50px', top: '60px', width: '80px', height: '30px'})"
    )
    submit_guard.click_answer(page, page.locator("#answer"), position={"x": 3, "y": 4})
    assert page.evaluate("window.__where") == [3, 4]
    _no_submission(page, site)
    assert submit_guard.blockers(page) == []


def test_click_answer_without_a_position_clicks_the_middle(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    page.evaluate(
        "document.getElementById('answer').addEventListener('click', e => {"
        " const box = e.currentTarget.getBoundingClientRect();"
        " window.__where = [e.clientX - box.left, e.clientY - box.top, box.width, box.height]; })"
    )
    submit_guard.click_answer(page, page.locator("#answer"))
    x, y, width, height = page.evaluate("window.__where")
    assert abs(x - width / 2) <= 1 and abs(y - height / 2) <= 1


def test_click_answer_is_quick_for_an_option_the_click_removes(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    page.evaluate(
        "document.getElementById('inert').addEventListener('click', e => e.target.remove())"
    )
    started = time.monotonic()
    submit_guard.click_answer(page, page.locator("#inert"))
    assert time.monotonic() - started < 2.0  # no wait for the vanished element's mark
    assert page.locator("#inert").count() == 0


def _calls_named(tree: ast.AST, names: set[str]) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in names
    ]


def test_the_only_click_in_the_guard_module_follows_the_installed_check():
    source = Path(submit_guard.__file__).read_text()
    tree = ast.parse(source)
    clicks = _calls_named(tree, {"click", "dblclick", "check", "set_checked", "tap"})
    assert len(clicks) == 1 and clicks[0].func.attr == "click"
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "click_answer"
    )
    assert clicks[0] in list(ast.walk(function))
    # the first statement after the docstring refuses an unguarded page
    guard = function.body[1]
    assert isinstance(guard, ast.If)
    assert ast.unparse(guard.test) == "not installed(page)"
    assert isinstance(guard.body[0], ast.Raise)
    assert guard.body[0].lineno < clicks[0].lineno


def test_pressing_enter_in_a_field_is_stopped_and_recorded(page, site):
    """Implicit submission goes through the same submit event."""
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    page.focus("#note")
    page.keyboard.press("Enter")
    _no_submission(page, site)
    assert len(submit_guard.blockers(page)) == 1


# --- 3: form.submit() and form.requestSubmit() -------------------------------------


def test_form_submit_and_request_submit_are_stopped_and_recorded(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    page.evaluate("document.getElementById('application-form').submit()")
    found = submit_guard.blockers(page)
    assert len(found) == 1 and "form.submit()" in found[0]
    page.evaluate("document.getElementById('application-form').requestSubmit()")
    page.evaluate(
        "document.getElementById('application-form').requestSubmit(document.getElementById('plain'))"
    )
    _no_submission(page, site)
    found = submit_guard.blockers(page)
    assert len(found) == 3
    assert all(entry.startswith("submit_guard: ") for entry in found)
    assert 'button "Plain"' in found[2]


def test_every_blocker_is_prefixed_and_free_of_query_strings(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    page.evaluate("fetch('https://boards.greenhouse.io/acme/jobs/1?token=SECRET', {method: 'POST'}).catch(() => 0)")
    page.click("#plain")
    found = submit_guard.blockers(page)
    assert len(found) == 2
    assert all(entry.startswith("submit_guard: ") for entry in found)
    assert not any("SECRET" in entry or "?" in entry for entry in found)


# --- 4: ATS submit requests are aborted --------------------------------------------

GUARDED = [
    pytest.param(
        "https://boards.greenhouse.io/acme/jobs/123",
        "/acme/jobs/123",
        "greenhouse application POST",
        id="greenhouse-classic",
    ),
    pytest.param(
        "https://job-boards.greenhouse.io/acme/jobs/123",
        "/acme/jobs/123",
        "greenhouse application POST",
        id="greenhouse-job-boards",
    ),
    pytest.param(
        "https://boards.eu.greenhouse.io/acme/jobs/123",
        "/acme/jobs/123",
        "greenhouse application POST",
        id="greenhouse-eu",
    ),
    pytest.param(
        "https://job-boards.eu.greenhouse.io/acme/jobs/123",
        "/acme/jobs/123?_data=routes%2Fjobs",
        "greenhouse application POST",
        id="greenhouse-job-boards-eu",
    ),
    pytest.param(
        "https://boards.greenhouse.io/embed/acme/jobs/123",
        "/embed/acme/jobs/123",
        "greenhouse application POST",
        id="greenhouse-embed",
    ),
    pytest.param(
        f"https://jobs.lever.co/acme/{UUID}/apply",
        f"/acme/{UUID}/apply",
        "lever application POST",
        id="lever",
    ),
    pytest.param(
        f"https://jobs.lever.co/acme/{UUID}/apply?lever-source=Board",
        f"/acme/{UUID}/apply?lever-source=Board",
        "lever application POST",
        id="lever-with-query",
    ),
    pytest.param(
        f"https://jobs.eu.lever.co/acme/{UUID}/apply",
        f"/acme/{UUID}/apply",
        "lever application POST",
        id="lever-eu",
    ),
    pytest.param(
        f"https://jobs.ashbyhq.com/acme/{UUID}/application",
        "/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction",
        "ashby submit operation ApiSubmitSingleApplicationFormAction",
        id="ashby-single",
    ),
    pytest.param(
        f"https://jobs.ashbyhq.com/acme/{UUID}/application",
        "/api/non-user-graphql?op=ApiSubmitApplicationFormAction",
        "ashby submit operation ApiSubmitApplicationFormAction",
        id="ashby-application",
    ),
]


@pytest.mark.parametrize(("page_url", "post_path", "described"), GUARDED)
def test_a_post_to_each_guarded_pattern_is_aborted_and_recorded(page, site, page_url, post_path, described):
    submit_guard.install(page)
    page.goto(page_url)
    assert page.evaluate(_FETCH_POST, [post_path, None]) == "aborted"
    assert site.posts() == []
    found = submit_guard.blockers(page)
    assert len(found) == 1
    assert found[0].startswith("submit_guard: aborted ")
    assert described in found[0]


@pytest.mark.parametrize(("page_url", "post_path", "described"), GUARDED[:1] + GUARDED[5:6] + GUARDED[8:9])
def test_control_the_same_posts_go_through_without_the_guard(page, site, page_url, post_path, described):
    page.goto(page_url)
    assert page.evaluate(_FETCH_POST, [post_path, None]) == "sent"
    assert len(site.posts()) == 1


def test_ashby_submit_named_only_in_the_body_is_aborted(page, site):
    submit_guard.install(page)
    page.goto(f"https://jobs.ashbyhq.com/acme/{UUID}/application")
    sent = page.evaluate(
        """() => fetch('/api/non-user-graphql', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({operationName: 'ApiSubmitSurveyFormAction', variables: {}, query: 'mutation {}'}),
        }).then(() => 'sent', () => 'aborted')"""
    )
    assert sent == "aborted"
    assert site.posts() == []
    assert "ApiSubmitSurveyFormAction" in submit_guard.blockers(page)[0]


def test_ashby_batch_with_a_submit_operation_is_aborted(page, site):
    submit_guard.install(page)
    page.goto(f"https://jobs.ashbyhq.com/acme/{UUID}/application")
    sent = page.evaluate(
        """() => fetch('/api/non-user-graphql', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify([{operationName: 'ApiCreateFileUploadHandle'},
                                  {operationName: 'ApiSubmitMultipleFormsAction'}]),
        }).then(() => 'sent', () => 'aborted')"""
    )
    assert sent == "aborted"
    assert site.posts() == []
    assert len(submit_guard.blockers(page)) == 1


def test_a_page_cannot_hide_a_submit_operation_behind_a_harmless_op_parameter(page, site):
    submit_guard.install(page)
    page.goto(f"https://jobs.ashbyhq.com/acme/{UUID}/application")
    sent = page.evaluate(
        """() => fetch('/api/non-user-graphql?op=ApiHarmless', {
            method: 'POST',
            body: JSON.stringify({operationName: 'ApiSubmitApplicationFormAction'}),
        }).then(() => 'sent', () => 'aborted')"""
    )
    assert sent == "aborted"
    assert site.posts() == []


# --- 5: uploads and everything else pass -------------------------------------------

PASSING = [
    pytest.param(
        f"https://jobs.ashbyhq.com/acme/{UUID}/application",
        "/api/non-user-graphql?op=ApiCreateFileUploadHandle",
        id="ashby-upload-handle",
    ),
    pytest.param(
        f"https://jobs.ashbyhq.com/acme/{UUID}/application",
        "/api/non-user-graphql?op=ApiSetFormValueToFile",
        id="ashby-set-file",
    ),
    pytest.param(
        f"https://jobs.ashbyhq.com/acme/{UUID}/application",
        "/api/non-user-graphql?op=ApiFormRender",
        id="ashby-other-operation",
    ),
    pytest.param(
        f"https://jobs.lever.co/acme/{UUID}/apply",
        "/parseResume",
        id="lever-resume-parse",
    ),
    pytest.param(
        "https://boards.greenhouse.io/acme/jobs/123",
        "/acme/jobs/123/uncacheable_attributes/presigned_fields?fields[]=resume",
        id="greenhouse-presigned-fields",
    ),
    pytest.param(
        "https://job-boards.greenhouse.io/acme/jobs/123",
        "https://job-boards-uploads.example.test/presigned-upload",
        id="greenhouse-presigned-storage-post",
    ),
    pytest.param(
        "https://job-boards.greenhouse.io/acme/jobs/123",
        "/seek/send_signal",
        id="greenhouse-confirmation-signal",
    ),
]


@pytest.mark.parametrize(("page_url", "post_url"), PASSING)
def test_upload_like_and_other_posts_are_not_blocked(page, site, page_url, post_url):
    submit_guard.install(page)
    page.goto(page_url)
    assert page.evaluate(_FETCH_POST, [post_url, "no-cors"]) == "sent"
    assert len(site.posts()) == 1
    assert submit_guard.blockers(page) == []


@pytest.mark.parametrize(
    "page_url",
    [
        "https://boards.greenhouse.io/acme/jobs/123",
        f"https://jobs.lever.co/acme/{UUID}/apply",
        f"https://jobs.ashbyhq.com/acme/{UUID}/application",
    ],
)
def test_loading_the_application_page_itself_is_not_blocked(page, site, page_url):
    submit_guard.install(page)
    page.goto(page_url)
    assert ("GET", page_url) in site.hits
    assert page.evaluate("typeof window.__submitted") == "object"  # the form page rendered
    assert submit_guard.blockers(page) == []


def test_a_get_to_a_guarded_path_is_not_blocked(page, site):
    submit_guard.install(page)
    page.goto("https://boards.greenhouse.io/acme/jobs/123")
    sent = page.evaluate(
        "() => fetch('/acme/jobs/456').then(() => 'sent', () => 'aborted')"
    )
    assert sent == "sent"
    assert submit_guard.blockers(page) == []


def test_an_earlier_route_still_sees_what_the_guard_lets_through(page):
    """The guard only falls back; routes registered before it keep working."""
    seen: list[str] = []

    def earlier(route):
        seen.append(route.request.url)
        route.fulfill(status=200, content_type="text/html", body=FORM)

    page.route("**/*", earlier)
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    assert seen == [f"{ELSEWHERE}/apply"]


# --- 5b: the network layer fails closed ---------------------------------------------

_ASHBY_PAGE = f"https://jobs.ashbyhq.com/acme/{UUID}/application"


def _fetch(page, url: str, method: str = "POST", *, headers=None, body="{}") -> str:
    return page.evaluate(
        "([url, method, headers, body]) => fetch(url, {method, headers, body})"
        ".then(() => 'sent', () => 'aborted')",
        [url, method, headers or {}, body],
    )


def test_an_unknown_post_to_a_greenhouse_host_is_aborted_and_recorded(page, site):
    submit_guard.install(page)
    page.goto("https://boards.greenhouse.io/acme/jobs/123")
    assert _fetch(page, "https://boards-api.greenhouse.io/v1/boards/acme/stats") == "aborted"
    assert site.posts() == []
    found = submit_guard.blockers(page)
    assert len(found) == 1
    assert found[0] == (
        "submit_guard: aborted greenhouse POST not on the allowlist (boards-api.greenhouse.io/v1/boards/acme/stats)"
    )
    assert submit_guard.is_unknown_abort(found[0])


def test_a_job_board_api_application_post_is_aborted_as_a_submit(page, site):
    submit_guard.install(page)
    page.goto("https://boards.greenhouse.io/acme/jobs/123")
    assert _fetch(page, "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123") == "aborted"
    assert site.posts() == []
    found = submit_guard.blockers(page)
    assert found == [
        "submit_guard: aborted greenhouse application POST (boards-api.greenhouse.io/v1/boards/acme/jobs/123)"
    ]
    assert not submit_guard.is_unknown_abort(found[0])


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_other_non_get_methods_to_an_ats_host_are_aborted(page, site, method):
    submit_guard.install(page)
    page.goto("https://boards.greenhouse.io/acme/jobs/123")
    assert _fetch(page, "/acme/jobs/123", method) == "aborted"
    assert [m for m, _ in site.hits if m == method] == []
    assert len(submit_guard.blockers(page)) == 1


def test_a_lever_post_that_is_not_the_apply_path_is_aborted(page, site):
    submit_guard.install(page)
    page.goto(f"https://jobs.lever.co/acme/{UUID}/apply")
    assert _fetch(page, f"/acme/{UUID}/something-new") == "aborted"
    assert _fetch(page, "/other") == "aborted"
    assert site.posts() == []
    found = submit_guard.blockers(page)
    assert len(found) == 2
    assert all("aborted lever POST not on the allowlist" in entry for entry in found)


def test_an_ashby_graphql_call_without_an_operation_name_is_aborted(page, site):
    submit_guard.install(page)
    page.goto(_ASHBY_PAGE)
    assert _fetch(page, "/api/non-user-graphql", body='{"query": "mutation {}"}') == "aborted"
    assert _fetch(page, "/api/non-user-graphql", body="not json") == "aborted"
    assert _fetch(page, "/api/non-user-graphql", body="") == "aborted"
    assert site.posts() == []
    found = submit_guard.blockers(page)
    assert len(found) == 3
    assert all("without a readable operation name" in entry for entry in found)


def test_an_ashby_multipart_call_is_aborted_even_with_a_harmless_op(page, site):
    submit_guard.install(page)
    page.goto(_ASHBY_PAGE)
    sent = page.evaluate(
        """() => {
            const form = new FormData();
            form.append('operations', JSON.stringify({operationName: 'ApiCreateFileUploadHandle'}));
            form.append('file', new Blob(['x']), 'a.pdf');
            return fetch('/api/non-user-graphql?op=ApiCreateFileUploadHandle', {method: 'POST', body: form})
                .then(() => 'sent', () => 'aborted');
        }"""
    )
    assert sent == "aborted"
    assert site.posts() == []
    found = submit_guard.blockers(page)
    assert len(found) == 1 and "ashby multipart graphql call" in found[0]


def test_an_unfamiliar_ashby_submit_operation_is_aborted(page, site):
    submit_guard.install(page)
    page.goto(_ASHBY_PAGE)
    assert _fetch(page, "/api/non-user-graphql?op=ApiSubmitSomethingNew") == "aborted"
    body = '{"operationName": "ApiSubmitAnotherNewThing"}'
    assert _fetch(page, "/api/non-user-graphql", body=body) == "aborted"
    assert site.posts() == []
    found = submit_guard.blockers(page)
    assert len(found) == 2
    assert "ashby submit operation ApiSubmitSomethingNew" in found[0]
    assert "ashby submit operation ApiSubmitAnotherNewThing" in found[1]


def test_an_options_preflight_to_an_ats_host_is_not_blocked(page, site):
    submit_guard.install(page)
    page.goto("https://boards.greenhouse.io/acme/jobs/123")
    assert _fetch(page, "https://boards-api.greenhouse.io/v1/x", "OPTIONS") == "sent"
    assert submit_guard.blockers(page) == []


BLOCK = [
    # (method, url, body, content type, expected substring; None = let through)
    ("POST", "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1", None, None, "greenhouse application POST"),
    ("POST", "https://boards-api.greenhouse.io/v1/boards/acme/stats", None, None, "greenhouse POST not on the allowlist"),
    ("PUT", "https://boards.greenhouse.io/acme/jobs/123", None, None, "greenhouse application PUT"),
    ("PATCH", "https://job-boards.greenhouse.io/acme/jobs/123", None, None, "greenhouse application PATCH"),
    ("PUT", f"https://jobs.lever.co/acme/{UUID}/apply", None, None, "lever application PUT"),
    ("OPTIONS", "https://boards-api.greenhouse.io/v1/x", None, None, None),
    ("GET", "https://boards-api.greenhouse.io/v1/x", None, None, None),
    ("HEAD", "https://boards-api.greenhouse.io/v1/x", None, None, None),
    ("POST", "https://boards.greenhouse.io/acme/jobs/123/uncacheable_attributes/presigned_fields?f=1", None, None, None),
    ("POST", "https://job-boards.greenhouse.io/seek/send_signal", None, None, None),
    ("POST", "https://boards.greenhouse.io.evil.test/anything", None, None, None),
    # Greenhouse's Snowplow analytics beacon (dry run 6): a beacon carries no form
    ("POST", "https://c.spl.greenhouse.io/com.snowplowanalytics.snowplow/tp2", '{"schema":"x"}', "application/json", None),
    ("POST", "https://c.spl.greenhouse.io/com.snowplowanalytics.snowplow/tp2?x=1", None, None, None),
    ("POST", "https://c.spl.greenhouse.io/com.snowplowanalytics.snowplow/other-event", None, None, None),
    ("POST", "https://c.spl.greenhouse.io/other", None, None, "greenhouse POST not on the allowlist"),
    ("POST", "https://c.spl.greenhouse.io/com.snowplowanalytics.snowplow/a/b", None, None, "greenhouse POST not on the allowlist"),
    ("POST", "https://boards.greenhouse.io/com.snowplowanalytics.snowplow/tp2", None, None, "greenhouse POST not on the allowlist"),
    ("POST", "https://c.spl.greenhouse.io.evil.test/com.snowplowanalytics.snowplow/tp2", None, None, None),
    ("POST", f"https://jobs.lever.co/acme/{UUID}/other", None, None, "lever POST not on the allowlist"),
    ("POST", f"https://jobs.lever.co/acme/{UUID}", None, None, "lever POST not on the allowlist"),
    ("PUT", "https://jobs.lever.co/parseResume", None, None, "lever PUT not on the allowlist"),
    ("POST", "https://jobs.lever.co/parseResume", None, None, None),
    ("POST", "https://jobs.lever.co/seek/send_signal", None, None, None),
    ("POST", "https://jobs.ashbyhq.com/other", "{}", None, "ashby POST not on the allowlist"),
    ("POST", "https://jobs.ashbyhq.com/seek/send_signal", None, None, None),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", None, None, "without a readable operation name"),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", "{}", None, "without a readable operation name"),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", "[1, 2]", None, "without a readable operation name"),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", '{"operationName": 7}', None, "without a readable operation name"),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiFormRender", None, None, "without a readable operation name"),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiFormRender", "{}", None, None),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiCreateFileUploadHandle", "{}", None, None),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", '{"operationName": "ApiSetFormValueToFile"}', None, None),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiCreateFileUploadHandle", "--b\r\nx", "multipart/form-data; boundary=b", "multipart"),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitSomethingNew", "{}", None, "submit operation ApiSubmitSomethingNew"),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql?op=apisubmitlower", "{}", None, "submit operation apisubmitlower"),
    ("PUT", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitApplicationFormAction", "{}", None, "submit operation"),
    ("GET", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiFormRender", None, None, None),
    ("POST", "https://careers.example.test/api/non-user-graphql", "{}", None, "without a readable operation name"),
    ("POST", "https://careers.example.test/anything", "{}", None, None),
    ("POST", "http://[bad", None, None, None),
]


@pytest.mark.parametrize(("method", "url", "body", "content_type", "expected"), BLOCK)
def test_block_reason(method, url, body, content_type, expected):
    got = submit_guard.block_reason(method, url, body, content_type)
    if expected is None:
        assert got is None
    else:
        assert got is not None and expected in got
        assert "?" not in got  # never the query string


@pytest.mark.parametrize(
    ("method", "url", "body", "content_type"),
    [
        ("POST", "https://boards-api.greenhouse.io/v1/boards/acme/stats", None, None),
        ("PUT", "https://jobs.lever.co/parseResume", None, None),
        ("POST", "https://jobs.ashbyhq.com/other", "{}", None),
    ],
)
def test_an_unknown_abort_is_told_apart_from_a_submit(method, url, body, content_type):
    reason = submit_guard.block_reason(method, url, body, content_type)
    assert reason is not None
    assert submit_guard.is_unknown_abort(reason)
    assert submit_guard.is_unknown_abort("submit_guard: " + reason)


@pytest.mark.parametrize(
    "line",
    [
        "submit_guard: aborted greenhouse application POST (boards.greenhouse.io/acme/jobs/123)",
        f"submit_guard: aborted lever application POST (jobs.lever.co/acme/{UUID}/apply)",
        "submit_guard: aborted ashby submit operation ApiSubmitApplicationFormAction (jobs.ashbyhq.com)",
        "submit_guard: form.submit() stopped (form#application)",
        "submit_guard: submit event stopped (form)",
        "submit_guard: could not list frames (TargetClosedError)",
        "submit_guard: 3 further stopped attempts not listed",
        "",
    ],
)
def test_a_recognised_submit_or_a_dom_catch_is_not_an_unknown_abort(line):
    assert not submit_guard.is_unknown_abort(line)


# Intentional tightening (R2): these were unknown aborts and let the run go on; each is a
# submit attempt, so each stops the run. The request is aborted either way.
@pytest.mark.parametrize(
    ("method", "url", "body", "content_type"),
    [
        ("POST", "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1", None, None),
        ("PUT", "https://boards.greenhouse.io/acme/jobs/123", None, None),
        ("PATCH", "https://job-boards.greenhouse.io/acme/jobs/123", None, None),
        ("PUT", "https://boards.greenhouse.io/embed/acme/jobs/123", None, None),
        ("PATCH", f"https://jobs.lever.co/acme/{UUID}/apply", None, None),
        ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiCreateFileUploadHandle", "--b", "multipart/form-data; boundary=b"),
        ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", "multipart-ish", "multipart/form-data; boundary=b"),
        ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", "{}", None),
        ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", "not json", None),
    ],
)
def test_a_submit_shaped_or_multipart_abort_stops_the_run(method, url, body, content_type):
    reason = submit_guard.block_reason(method, url, body, content_type)
    assert reason is not None
    assert not submit_guard.is_unknown_abort(reason)
    assert not submit_guard.is_unknown_abort("submit_guard: " + reason)


def test_a_lookalike_of_the_catch_all_line_is_not_an_unknown_abort():
    real = "aborted greenhouse POST not on the allowlist (analytics.greenhouse.io/collect)"
    assert submit_guard.is_unknown_abort(real)
    for fake in (
        "submit_guard: submit event stopped (form), aborted greenhouse POST not on the allowlist (x/y)",
        "submit_guard: form.submit() stopped (form#a) aborted greenhouse POST not on the allowlist (x/y)",
        "submit_guard: page reported: aborted greenhouse POST not on the allowlist (x/y)",
        "submit_guard: aborted greenhouse POST not on the allowlist (x/y) and more",
        "submit_guard: aborted greenhouse application POST not on the allowlist (x/y)",
    ):
        assert not submit_guard.is_unknown_abort(fake)


def test_a_page_cannot_forge_an_unknown_abort_through_the_dom_report(page, site):
    submit_guard.install(page)
    page.goto("https://boards.greenhouse.io/acme/jobs/123")
    forged = "aborted greenhouse POST not on the allowlist (analytics.greenhouse.io/collect)"
    state = submit_guard._get(page)
    submit_guard._ingest(state, {"kind": "submit", "detail": forged})
    submit_guard._ingest(state, {"kind": "form.submit", "detail": "  Aborted lever PUT not on the allowlist (a/b)"})
    found = submit_guard.blockers(page)
    assert len(found) == 2
    assert all(entry.startswith("submit_guard: page reported: ") for entry in found)
    assert not any(submit_guard.is_unknown_abort(entry) for entry in found)


def test_the_snowplow_beacon_passes_in_the_browser_and_leaves_no_blocker(page, site):
    submit_guard.install(page)
    page.goto("https://boards.greenhouse.io/acme/jobs/123")
    assert _fetch(page, "https://c.spl.greenhouse.io/com.snowplowanalytics.snowplow/tp2", "POST", body="{}") == "sent"
    assert submit_guard.blockers(page) == []


# --- 6: lift ------------------------------------------------------------------------


def test_after_lift_a_submission_goes_through_and_records_nothing(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    assert submit_guard.installed(page) is True
    submit_guard.lift(page)
    assert submit_guard.installed(page) is False
    page.click("#plain")
    page.wait_for_url("**/post-target")
    assert site.posts() == [f"{ELSEWHERE}/post-target"]
    assert submit_guard.blockers(page) == []


def test_after_lift_ats_posts_go_through_too(page, site):
    submit_guard.install(page)
    page.goto("https://boards.greenhouse.io/acme/jobs/123")
    submit_guard.lift(page)
    assert page.evaluate(_FETCH_POST, ["/acme/jobs/123", None]) == "sent"
    assert page.evaluate(_FETCH_POST, ["/api/non-user-graphql?op=ApiSubmitApplicationFormAction", None]) == "sent"
    assert len(site.posts()) == 2
    assert submit_guard.blockers(page) == []


def test_form_submit_and_request_submit_work_again_after_lift(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    submit_guard.lift(page)
    page.evaluate("document.getElementById('application-form').requestSubmit()")
    page.wait_for_url("**/post-target")
    assert len(site.posts()) == 1
    assert submit_guard.blockers(page) == []


def test_a_document_loaded_after_lift_records_nothing(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    submit_guard.lift(page)
    page.goto(f"{ELSEWHERE}/second")
    page.click("#plain")
    page.wait_for_url("**/post-target")
    assert len(site.posts()) == 1
    assert submit_guard.blockers(page) == []


def test_lift_keeps_what_was_stopped_before_it(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    page.click("#plain")
    submit_guard.lift(page)
    assert len(submit_guard.blockers(page)) == 1


def test_lift_is_idempotent(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    submit_guard.lift(page)
    submit_guard.lift(page)
    assert submit_guard.installed(page) is False


def test_install_after_lift_raises(page, site):
    submit_guard.install(page)
    submit_guard.lift(page)
    with pytest.raises(RuntimeError, match="lifted"):
        submit_guard.install(page)
    assert submit_guard.installed(page) is False


# --- 7: navigation and frames --------------------------------------------------------


def test_the_guard_survives_navigation(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/first")
    page.goto(f"{ELSEWHERE}/second")
    page.click("#plain")
    _no_submission(page, site)
    assert len(submit_guard.blockers(page)) == 1


def test_what_was_recorded_survives_navigation(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/first")
    page.click("#plain")
    page.goto(f"{ELSEWHERE}/second")
    page.click("#plain")
    found = submit_guard.blockers(page)
    assert len(found) == 2
    assert site.posts() == []


def test_the_guard_survives_a_navigation_made_by_the_page(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/first")
    page.evaluate("location.href = '/second'")
    page.wait_for_url("**/second")
    page.click("#plain")
    _no_submission(page, site)
    assert len(submit_guard.blockers(page)) == 1


def test_a_form_in_a_same_origin_iframe_is_guarded(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/outer")
    page.evaluate(
        "(doc) => { const f = document.createElement('iframe'); f.srcdoc = doc;"
        " document.body.appendChild(f); return new Promise(r => f.onload = r); }",
        FORM,
    )
    page.frame_locator("iframe").locator("#plain").click()
    assert site.posts() == []
    assert len(submit_guard.blockers(page)) == 1


def test_a_form_in_a_cross_origin_iframe_is_guarded(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/outer")
    page.evaluate(
        "() => { const f = document.createElement('iframe'); f.src = 'https://embed.example.test/form';"
        " document.body.appendChild(f); return new Promise(r => f.onload = r); }"
    )
    page.frame_locator("iframe").locator("#plain").click()
    assert site.posts() == []
    found = submit_guard.blockers(page)
    assert len(found) == 1 and 'button "Plain"' in found[0]


def test_an_iframe_added_before_install_is_guarded_too(page, site):
    page.goto(f"{ELSEWHERE}/outer")
    page.evaluate(
        "() => { const f = document.createElement('iframe'); f.src = 'https://embed.example.test/form';"
        " document.body.appendChild(f); return new Promise(r => f.onload = r); }"
    )
    submit_guard.install(page)
    page.frame_locator("iframe").locator("#plain").click()
    assert site.posts() == []
    assert len(submit_guard.blockers(page)) == 1


def test_a_marked_click_inside_an_iframe_is_not_recorded(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/outer")
    page.evaluate(
        "() => { const f = document.createElement('iframe'); f.src = 'https://embed.example.test/form';"
        " document.body.appendChild(f); return new Promise(r => f.onload = r); }"
    )
    answer = page.frame_locator("iframe").locator("#answer")
    submit_guard.mark_answer_click(answer)
    answer.click()
    assert site.posts() == []
    assert submit_guard.blockers(page) == []


# --- popups and new tabs ------------------------------------------------------------

_POPUP_URL = "https://boards.greenhouse.io/popup"


def _serve_popups(page) -> None:
    """Play the ATS for pages the test did not route itself. Registered on the
    context BEFORE install, so the guard's own context route runs first."""
    page.context.route(
        _POPUP_URL,
        lambda route: route.fulfill(status=200, content_type="text/html", body=FORM, headers=_CORS),
    )


def test_a_popup_opened_by_the_page_is_guarded(page, site):
    _serve_popups(page)
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    with page.expect_popup() as opened:
        page.evaluate("url => { window.open(url); }", _POPUP_URL)
    popup = opened.value
    popup.wait_for_load_state()
    assert submit_guard.installed(popup) is True
    popup.click("#plain")  # an unmarked submit in the popup
    assert popup.evaluate("window.__submitted") == []
    assert "post-target" not in popup.url
    found = submit_guard.blockers(page)  # recorded against the installed page
    assert len(found) == 1 and 'button "Plain"' in found[0]


def test_a_new_tab_in_the_same_context_is_guarded(page, site):
    _serve_popups(page)
    submit_guard.install(page)
    tab = page.context.new_page()
    tab.goto(_POPUP_URL)
    tab.click("#plain")
    assert "post-target" not in tab.url
    found = submit_guard.blockers(page)
    assert len(found) == 1 and "submit event stopped" in found[0]


def test_an_ats_post_from_a_popup_is_aborted_and_recorded(page, site):
    _serve_popups(page)
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    with page.expect_popup() as opened:
        page.evaluate("url => { window.open(url); }", _POPUP_URL)
    popup = opened.value
    popup.wait_for_load_state()
    assert popup.evaluate(_FETCH_POST, ["/acme/jobs/123", None]) == "aborted"
    # an allowlisted call is not recorded (the fixture context then aborts it: nobody serves this page's calls)
    popup.evaluate(_FETCH_POST, ["/acme/jobs/123/uncacheable_attributes/presigned_fields", "no-cors"])
    found = submit_guard.blockers(page)
    assert len(found) == 1 and "greenhouse application POST" in found[0]


def test_a_marked_click_in_a_popup_is_not_recorded(page, site):
    _serve_popups(page)
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    with page.expect_popup() as opened:
        page.evaluate("url => { window.open(url); }", _POPUP_URL)
    popup = opened.value
    popup.wait_for_load_state()
    submit_guard.click_answer(popup, popup.locator("#answer"))
    assert popup.evaluate("window.__submitted") == []
    assert submit_guard.blockers(page) == []


def test_a_popup_is_not_guarded_without_install_and_after_lift(page, site):
    _serve_popups(page)
    page.goto(f"{ELSEWHERE}/apply")
    with page.expect_popup() as opened:
        page.evaluate("url => { window.open(url); }", _POPUP_URL)
    popup = opened.value
    assert submit_guard.installed(popup) is False  # control: never installed

    submit_guard.install(page)
    with page.expect_popup() as opened_again:
        page.evaluate("url => { window.open(url); }", _POPUP_URL)
    guarded = opened_again.value
    assert submit_guard.installed(guarded) is True
    submit_guard.lift(page)
    assert submit_guard.installed(guarded) is False


def test_lift_takes_the_context_route_off_and_posts_go_through(page, site):
    _serve_popups(page)
    reached: list[str] = []

    def serve_post(route):
        reached.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}", headers=_CORS)

    page.context.route("https://boards.greenhouse.io/acme/**", serve_post)
    submit_guard.install(page)
    tab = page.context.new_page()
    tab.goto(_POPUP_URL)
    assert tab.evaluate(_FETCH_POST, ["/acme/jobs/123", None]) == "aborted"
    assert reached == []
    assert len(submit_guard.blockers(page)) == 1
    submit_guard.lift(page)
    other = page.context.new_page()  # a page created after the lift
    other.goto(_POPUP_URL)
    assert other.evaluate(_FETCH_POST, ["/acme/jobs/123", None]) == "sent"
    assert reached == ["https://boards.greenhouse.io/acme/jobs/123"]


def test_install_registers_the_context_layer_once_for_a_real_page(page):
    context_calls: list[str] = []
    real_route = page.context.route
    page.context.route = lambda *a, **k: (context_calls.append("route"), real_route(*a, **k))[1]
    try:
        submit_guard.install(page)
        submit_guard.install(page)
    finally:
        page.context.route = real_route
    assert context_calls == ["route"]


# --- state: install / installed / blockers ------------------------------------------


def test_install_is_idempotent(page, site):
    submit_guard.install(page)
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    page.click("#plain")
    assert len(submit_guard.blockers(page)) == 1  # one listener, one route, one report


def test_never_installed_page_reports_nothing(page, site):
    page.goto(f"{ELSEWHERE}/apply")
    assert submit_guard.installed(page) is False
    assert submit_guard.blockers(page) == []
    submit_guard.lift(page)  # a no-op: the page still submits
    page.click("#plain")
    page.wait_for_url("**/post-target")


def test_installed_is_per_page(browser):
    context = browser.new_context()
    try:
        first, second = context.new_page(), context.new_page()
        submit_guard.install(first)
        assert submit_guard.installed(first) is True
        assert submit_guard.installed(second) is False
        assert submit_guard.blockers(second) == []
    finally:
        context.close()


def test_the_report_is_current_the_moment_the_click_returns(page, site):
    """blockers() flushes in-flight reports, so the gate never reads stale state."""
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    for count in range(1, 21):
        page.click("#plain")
        assert len(submit_guard.blockers(page)) == count


def test_a_report_the_binding_could_not_deliver_is_drained_from_the_page(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    page.evaluate("window.__jhSubmitGuardReport = undefined")
    page.click("#plain")
    _no_submission(page, site)
    assert len(submit_guard.blockers(page)) == 1
    assert len(submit_guard.blockers(page)) == 1  # drained once, not duplicated


def test_the_event_list_is_capped(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    page.evaluate(
        "() => { const form = document.getElementById('application-form');"
        " for (let i = 0; i < 70; i++) form.submit(); }"
    )
    found = submit_guard.blockers(page)
    assert len(found) == 51
    assert found[-1] == "submit_guard: 20 further stopped attempts not listed"


def test_reports_the_page_makes_up_cannot_inject_garbage(page, site):
    submit_guard.install(page)
    page.goto(f"{ELSEWHERE}/apply")
    page.evaluate(
        """() => {
            window.__jhSubmitGuardReport({kind: 'ping', detail: 'ignored'});
            window.__jhSubmitGuardReport({kind: 'nonsense', detail: 'ignored'});
            window.__jhSubmitGuardReport('not an object');
            window.__jhSubmitGuardReport({kind: 'submit', detail: 'x'.repeat(5000) + '\\n\\u0007bell'});
        }"""
    )
    found = submit_guard.blockers(page)
    assert len(found) == 1
    assert len(found[0]) <= len("submit_guard: ") + 200
    assert "\n" not in found[0] and "\u0007" not in found[0]


# --- the Python half without a browser ------------------------------------------------


class _Frame:
    def __init__(self, result=None, error=None, url="about:blank"):
        self.result = result
        self.error = error
        self.url = url
        self.calls: list[str] = []

    def evaluate(self, expression, *args, **kwargs):
        self.calls.append(expression)
        if self.error is not None:
            raise self.error
        return self.result


class _Page:
    """Just enough page for install / blockers / lift."""

    def __init__(self, frames=None):
        self.frames = frames if frames is not None else [_Frame(result=[])]
        self.bindings: dict = {}
        self.scripts: list[str] = []
        self.routes: list = []
        self.unrouted: list = []

    def expose_binding(self, name, callback):
        self.bindings[name] = callback

    def add_init_script(self, script=None, path=None):
        self.scripts.append(script)

    def route(self, match, handler):
        self.routes.append((match, handler))

    def unroute(self, match, handler=None):
        self.unrouted.append((match, handler))


def test_install_registers_each_layer_exactly_once():
    fake = _Page()
    submit_guard.install(fake)
    submit_guard.install(fake)
    assert list(fake.bindings) == ["__jhSubmitGuardReport"]
    assert len(fake.scripts) == 1
    assert len(fake.routes) == 1
    assert submit_guard.installed(fake) is True


def test_a_failed_install_is_not_reported_as_installed_and_can_be_retried():
    frame = _Frame(error=RuntimeError("frame gone"))
    fake = _Page(frames=[frame])
    with pytest.raises(RuntimeError, match="frame gone"):
        submit_guard.install(fake)
    assert submit_guard.installed(fake) is False
    assert fake.routes == []
    frame.error = None
    submit_guard.install(fake)
    assert submit_guard.installed(fake) is True
    assert len(fake.bindings) == 1 and len(fake.scripts) == 1 and len(fake.routes) == 1


def test_blockers_fail_closed_when_the_main_frame_cannot_be_read():
    fake = _Page(frames=[_Frame(error=RuntimeError("navigating"))])
    fake.frames[0].error = None
    submit_guard.install(fake)
    fake.frames[0].error = RuntimeError("navigating")
    assert submit_guard.blockers(fake) == [
        "submit_guard: could not read guard state in the main frame (RuntimeError)"
    ]


def test_blockers_fail_closed_when_the_frames_cannot_be_listed():
    class Broken(_Page):
        @property
        def frames(self):
            if getattr(self, "_armed", False):
                raise RuntimeError("closed")
            return [_Frame(result=[])]

        @frames.setter
        def frames(self, value):
            pass

    fake = Broken()
    submit_guard.install(fake)
    fake._armed = True
    assert submit_guard.blockers(fake) == ["submit_guard: could not list frames (RuntimeError)"]


def test_blockers_flag_a_main_frame_without_the_dom_layer():
    main = _Frame(result=[])
    fake = _Page(frames=[main])
    submit_guard.install(fake)
    main.result = None  # the document has no window.__jhSubmitGuard
    assert submit_guard.blockers(fake) == ["submit_guard: DOM layer missing in the main frame"]


def test_a_non_ats_child_frame_that_cannot_be_read_is_ignored():
    child = _Frame(result=[], url="https://analytics.example.test/beacon")
    fake = _Page(frames=[_Frame(result=[]), child])
    submit_guard.install(fake)
    child.error = RuntimeError("detached")
    assert submit_guard.blockers(fake) == []
    child.error = None
    child.result = None  # no guard in a child document on a non-ATS host: not a blocker
    assert submit_guard.blockers(fake) == []


# Intentional tightening (not a weakening): a child frame on a Greenhouse, Lever
# or Ashby host that cannot be read, or has no DOM layer, used to count as clean.
# A form could hide there unguarded, so it is a blocker now; non-ATS frames stay ignored.
@pytest.mark.parametrize(
    "url", ["https://boards.greenhouse.io/embed/job_app", f"https://jobs.lever.co/acme/{UUID}/apply", "https://jobs.ashbyhq.com/acme"]
)
def test_an_ats_child_frame_that_cannot_be_read_is_a_blocker(url):
    child = _Frame(result=[], url=url)
    fake = _Page(frames=[_Frame(result=[]), child])
    submit_guard.install(fake)
    child.error = RuntimeError("detached")
    host = url.split("/")[2]
    assert submit_guard.blockers(fake) == [
        f"submit_guard: could not read guard state in a frame on {host} (RuntimeError)"
    ]


def test_an_ats_child_frame_without_the_dom_layer_is_a_blocker():
    child = _Frame(result=[], url="https://job-boards.greenhouse.io/embed/job_app?for=acme&token=SECRET")
    fake = _Page(frames=[_Frame(result=[]), child])
    submit_guard.install(fake)
    child.result = None
    assert submit_guard.blockers(fake) == ["submit_guard: DOM layer missing in a frame on job-boards.greenhouse.io"]


def test_drained_payloads_are_recorded_and_junk_is_not():
    child = _Frame(result=[])
    fake = _Page(frames=[_Frame(result=[]), child])
    submit_guard.install(fake)
    child.result = [
        {"kind": "submit", "detail": "submit event stopped (form#a, no submitter)"},
        {"kind": "ping"},
        "junk",
        {"kind": "form.submit"},
    ]
    assert submit_guard.blockers(fake) == [
        "submit_guard: submit event stopped (form#a, no submitter)",
        "submit_guard: form.submit",
    ]


def test_reports_through_the_binding_are_recorded():
    fake = _Page()
    submit_guard.install(fake)
    fake.bindings["__jhSubmitGuardReport"]({"page": fake}, {"kind": "submit", "detail": "from the page"})
    assert submit_guard.blockers(fake) == ["submit_guard: from the page"]


def test_lift_turns_every_layer_off_and_drops_later_reports():
    fake = _Page(frames=[_Frame(result=[]), _Frame(result=[])])
    submit_guard.install(fake)
    submit_guard.lift(fake)
    assert len(fake.unrouted) == 1
    assert fake.unrouted[0] == fake.routes[0]
    assert len(fake.scripts) == 2  # the guard and, after lift, the off script
    assert "__jhSubmitGuardOff" in fake.scripts[1]
    assert all("__jhSubmitGuardOff" in frame.calls[-1] for frame in fake.frames)
    fake.bindings["__jhSubmitGuardReport"]({}, {"kind": "submit", "detail": "too late"})
    assert submit_guard.blockers(fake) == []
    assert submit_guard.installed(fake) is False


def test_a_lift_that_raises_leaves_the_guard_reported_as_not_in_force():
    frame = _Frame(result=[])
    fake = _Page(frames=[frame])
    submit_guard.install(fake)
    frame.error = RuntimeError("cannot reach the frame")
    with pytest.raises(RuntimeError, match="cannot reach the frame"):
        submit_guard.lift(fake)
    # the off script is already registered for new documents, so no click may rely on the guard
    assert submit_guard.installed(fake) is False
    with pytest.raises(RuntimeError, match="lifted"):
        submit_guard.install(fake)
    frame.error = None
    submit_guard.lift(fake)  # a retry finishes the job
    assert len(fake.unrouted) == 1


def test_lift_is_a_noop_for_pages_the_guard_never_saw():
    mock = MagicMock()
    submit_guard.lift(mock)
    assert mock.method_calls == []  # hashing the mock is the only thing done to it
    assert submit_guard.blockers(mock) == []
    assert submit_guard.installed(mock) is False


def test_a_page_that_cannot_be_tracked_is_treated_as_not_installed():
    class Unhashable:
        __hash__ = None

    assert submit_guard.installed(Unhashable()) is False
    assert submit_guard.blockers(Unhashable()) == []
    submit_guard.lift(Unhashable())


def test_the_registry_does_not_keep_pages_alive():
    import gc
    import weakref

    fake = _Page()
    submit_guard.install(fake)
    ref = weakref.ref(fake)
    del fake
    gc.collect()
    assert ref() is None


# --- classify_request: every URL shape without a browser ---------------------------

CLASSIFY = [
    # Greenhouse
    ("POST", "https://boards.greenhouse.io/acme/jobs/123", None, "greenhouse"),
    ("POST", "https://boards.greenhouse.io/acme/jobs/123/", None, "greenhouse"),
    ("POST", "https://job-boards.greenhouse.io/acme/jobs/123?gh_src=abc", None, "greenhouse"),
    ("POST", "https://boards.eu.greenhouse.io/acme/jobs/123", None, "greenhouse"),
    ("POST", "https://job-boards.eu.greenhouse.io/acme/jobs/123", None, "greenhouse"),
    ("POST", "https://boards.greenhouse.io/embed/acme/jobs/123", None, "greenhouse"),
    ("POST", "https://boards.greenhouse.io/embed/job_app", None, "greenhouse"),
    ("post", "https://boards.greenhouse.io/acme/jobs/123", None, "greenhouse"),
    ("GET", "https://boards.greenhouse.io/acme/jobs/123", None, None),
    ("POST", "https://boards.greenhouse.io/acme/jobs/123/uncacheable_attributes/presigned_fields", None, None),
    ("POST", "https://boards.greenhouse.io/acme", None, None),
    ("POST", "https://boards.greenhouse.io/acme/jobs/abc", None, None),
    ("POST", "https://my.greenhouse.io/users/sign_in", None, None),
    ("POST", "https://example.test/acme/jobs/123", None, None),
    ("POST", "https://boards.greenhouse.io.evil.test/acme/jobs/123", None, None),
    # Lever
    ("POST", f"https://jobs.lever.co/acme/{UUID}/apply", None, "lever"),
    ("POST", f"https://jobs.lever.co/acme/{UUID}/apply?lever-source=x", None, "lever"),
    ("POST", f"https://jobs.eu.lever.co/acme/{UUID}/apply", None, "lever"),
    ("GET", f"https://jobs.lever.co/acme/{UUID}/apply", None, None),
    ("POST", "https://jobs.lever.co/parseResume", None, None),
    ("POST", f"https://jobs.lever.co/acme/{UUID}", None, None),
    ("POST", f"https://example.test/acme/{UUID}/apply", None, None),
    # Ashby
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction", None, "ashby"),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitMultipleFormsAction", None, "ashby"),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitTakeHomeFormAction", None, "ashby"),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", '{"operationName": "ApiSubmitSourcingFormAction"}', "ashby"),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", '[{"operationName": "ApiSubmitRegistrationFormAction"}]', "ashby"),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiCreateFileUploadHandle", None, None),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSetFormValueToFile", None, None),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitAvailability", None, "ashby"),  # any ^ApiSubmit name (was None under the narrow regex)
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", '{"operationName": "ApiCreateFileUploadHandle"}', None),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", "not json", None),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", "[1, 2, null]", None),
    ("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", '{"operationName": 7}', None),
    ("GET", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction", None, None),
    ("POST", "https://jobs.ashbyhq.com/api/other?op=ApiSubmitSingleApplicationFormAction", None, None),
    ("POST", "https://careers.example.test/api/non-user-graphql?op=ApiSubmitApplicationFormAction", None, "ashby"),
    # nonsense
    ("POST", "not a url", None, None),
    ("POST", "http://[bad", None, None),
    ("", "https://boards.greenhouse.io/acme/jobs/123", None, None),
]


@pytest.mark.parametrize(("method", "url", "body", "expected"), CLASSIFY)
def test_classify_request(method, url, body, expected):
    got = submit_guard.classify_request(method, url, body)
    if expected is None:
        assert got is None
    else:
        assert got is not None and expected in got
        assert "?" not in got  # never the query string


def test_classify_request_names_host_and_path_only():
    got = submit_guard.classify_request("POST", "https://boards.greenhouse.io/acme/jobs/123?token=SECRET")
    assert got == "aborted greenhouse application POST (boards.greenhouse.io/acme/jobs/123)"


# Dry run 7, FloQast posting #361883: Cloudflare's challenge script posted to the Lever host.
CHALLENGE_URL = (
    "https://jobs.lever.co/cdn-cgi/challenge-platform/h/b/jsd/oneshot/d2d3316b729e/"
    "0.3321456192769964:1792854619:6Zm9hl_LQFFYwUlHUy2NyUN9vOz55aRUUpxyloApIvU/a4fa49334a63cef"
)


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co/cdn-cgi/challenge-platform/h/b/flow/ov1/1:2:3/abc",
        "https://jobs.lever.co/cdn-cgi/challenge-platform/h/g/orchestrate/chl_api/v1",
        "https://boards.greenhouse.io/cdn-cgi/challenge-platform/h/g/flow/ov1/1:2:3/abc",
        "https://jobs.ashbyhq.com/cdn-cgi/challenge-platform/scripts/jsd/main.js",
    ],
)
def test_a_cloudflare_challenge_request_is_a_bot_challenge_not_a_stop(method, url):
    reason = submit_guard.block_reason(method, url)
    assert reason is not None
    assert "bot challenge (Cloudflare)" in reason
    assert "application" not in reason
    assert len(reason) < 120  # short enough that _clean never cuts it
    assert submit_guard.is_unknown_abort(reason)
    assert submit_guard.is_unknown_abort("submit_guard: " + reason)
    assert submit_guard.classify_request(method, url) is None


def test_the_passive_cloudflare_beacon_is_let_through():
    # Dry run 8: every Lever page posts this passive JavaScript Detections beacon.
    assert submit_guard.block_reason("POST", CHALLENGE_URL) is None
    assert submit_guard.block_reason("POST", CHALLENGE_URL.replace("jobs.lever.co", "boards.greenhouse.io")) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co/x/cdn-cgi/challenge-platform/h/b/jsd/oneshot/abc/1:2:3/def",
        "https://jobs.lever.co/cdn-cgi/challenge-platform/h/bb/jsd/oneshot/abc",
        "https://jobs.lever.co/cdn-cgi/challenge-platform/h/b/jsd/oneshotx/abc",
        "https://jobs.lever.co/cdn-cgi/challenge-platform/scripts/jsd/main.js",
        "https://jobs.lever.co/cdn-cgi/challenge-platform/h/b/jsd/oneshot/%2e%2e/%2e%2e/flow/ov1/1:2:3",
        "https://jobs.lever.co/cdn-cgi/challenge-platform/h/b/jsd/oneshot/%2E%2E/flow/x",
        "https://jobs.lever.co/cdn-cgi/challenge-platform/h/b/jsd/oneshot/../flow/ov1/1:2:3/abc",
        "https://jobs.lever.co/cdn-cgi/challenge-platform/h/b/jsd/oneshot/abc/../../orchestrate/x",
        "https://jobs.lever.co/cdn-cgi/challenge-platform/h/b/jsd/oneshot/abc%2Fdef",
        "https://jobs.lever.co/cdn-cgi/challenge-platform/h/b/jsd/oneshot/abc;x=1",
    ],
)
def test_only_the_exact_passive_beacon_prefix_is_let_through(url):
    reason = submit_guard.block_reason("POST", url)
    assert reason is not None
    assert "not on the allowlist" in reason or "bot challenge (Cloudflare)" in reason
    assert submit_guard.is_unknown_abort(reason)


def test_a_cut_unknown_abort_line_is_still_an_unknown_abort():
    # The dry run 7 line as _clean left it: cut at 200 characters, no closing parenthesis.
    cut = (
        "submit_guard: aborted lever POST not on the allowlist (jobs.lever.co/cdn-cgi/challenge-platform/"
        "h/b/jsd/oneshot/d2d3316b729e/0.3321456192769964:1792854619:6Zm9hl_LQFFYwUlHUy2NyUN9vOz55aRUUpxyloApIvU/a4fa49334a63cef"
    )
    assert submit_guard.is_unknown_abort(cut)
    # A cut submit-shaped line or one with trailing text stays a stop.
    assert not submit_guard.is_unknown_abort("submit_guard: aborted lever application POST (jobs.lever.co/acme/abc/apply")
    assert not submit_guard.is_unknown_abort("submit_guard: aborted lever POST not on the allowlist (a/b and more")
    assert not submit_guard.is_unknown_abort("submit_guard: aborted lever POST bot challenge (Cloudflare) (a/b) and more")


def test_a_submit_shaped_path_under_a_lever_host_still_reads_as_a_submit():
    url = "https://jobs.lever.co/acme/d2d3316b-729e-4c0a-9b1a-0123456789ab/apply"
    reason = submit_guard.block_reason("POST", url)
    assert reason is not None and "bot challenge" not in reason
    assert not submit_guard.is_unknown_abort(reason)
