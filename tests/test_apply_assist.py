from __future__ import annotations

import ast
import inspect
import time
from pathlib import Path

import pytest

from jobhunt import cli, db
from jobhunt.apply_assist import fill, profile_store
from jobhunt.apply_assist.detect import application_url, detect_ats
from jobhunt.apply_assist.keys import classify
from jobhunt.apply_assist.schema import FieldSpec, Resolution
from jobhunt.apply_assist.solve import resolve, values_match


# --- detect_ats -------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://jobs.lever.co/acme/abc-123", "lever"),
        ("https://jobs.ashbyhq.com/acme/abc-123", "ashby"),
        ("https://acme.com/careers/apply", None),
    ],
)
def test_detect_ats(url, expected):
    assert detect_ats(url) == expected


# --- application_url --------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        # Ashby and Lever serve the description at the harvested URL and the
        # form at a suffixed path; scanning the bare URL found 0 fields.
        (
            "https://jobs.ashbyhq.com/acme/abc-123",
            "https://jobs.ashbyhq.com/acme/abc-123/application",
        ),
        ("https://jobs.lever.co/acme/abc-123", "https://jobs.lever.co/acme/abc-123/apply"),
        # Trailing slash must not produce a doubled separator.
        (
            "https://jobs.ashbyhq.com/acme/abc-123/",
            "https://jobs.ashbyhq.com/acme/abc-123/application",
        ),
        # Idempotent — an already-suffixed URL is left alone.
        (
            "https://jobs.ashbyhq.com/acme/abc-123/application",
            "https://jobs.ashbyhq.com/acme/abc-123/application",
        ),
        ("https://jobs.lever.co/acme/abc-123/apply", "https://jobs.lever.co/acme/abc-123/apply"),
        # Greenhouse serves its form inline and must not be rewritten.
        ("https://boards.greenhouse.io/acme/jobs/123", "https://boards.greenhouse.io/acme/jobs/123"),
        # Unknown ATS is passed through untouched.
        ("https://acme.com/careers/apply", "https://acme.com/careers/apply"),
    ],
)
def test_application_url(url, expected):
    assert application_url(url) == expected


# --- classify -------------------------------------------------------


def test_classify_via_autocomplete():
    field = FieldSpec(selector="#f1", kind="email", autocomplete="email", name="q")
    assert classify(field) == "email"


def test_classify_via_name_regex():
    field = FieldSpec(selector="#f2", kind="text", name="first_name")
    assert classify(field) == "first_name"


def test_classify_via_label():
    field = FieldSpec(selector="#f3", kind="text", label="LinkedIn Profile URL")
    assert classify(field) == "linkedin"


def test_classify_file_resume():
    field = FieldSpec(selector="#f4", kind="file", label="Resume/CV")
    assert classify(field) == "resume"


def test_classify_unmatched_returns_none():
    field = FieldSpec(
        selector="#f5", kind="text", name="favorite_color", label="Favorite color"
    )
    assert classify(field) is None


def test_classify_willing_to_relocate_not_location():
    field = FieldSpec(selector="#f6", kind="text", label="Willing to relocate?")
    assert classify(field) == "willing_to_relocate"


def test_classify_preferred_name_not_full_name():
    field = FieldSpec(selector="#f7", kind="text", label="Preferred Name (if applicable)")
    assert classify(field) == "preferred_name"


# --- resolve -------------------------------------------------------


def test_resolve_sensitive_key_always_asks_even_when_stored():
    field = FieldSpec(selector="#s1", kind="text", name="veteran_status")
    answers = {"veteran_status": "not a veteran"}
    [res] = resolve([field], answers, resume_path=None)
    assert res.action == "ask"
    assert res.semantic_key == "veteran_status"


def test_resolve_checkbox_always_asks():
    field = FieldSpec(selector="#c1", kind="checkbox", name="email")
    [res] = resolve([field], {"email": "ed@example.com"}, resume_path=None)
    assert res.action == "ask"
    assert "checkbox" in res.reason


def test_resolve_select_exact_match():
    field = FieldSpec(
        selector="#sel1",
        kind="select",
        label="Are you authorized to work in the US?",
        name="work_authorization",
        options=["Yes", "No"],
    )
    [res] = resolve([field], {"work_authorization": "yes"}, resume_path=None)
    assert res.action == "fill"
    assert res.value == "Yes"


def test_resolve_select_unique_substring_match():
    field = FieldSpec(
        selector="#sel2",
        kind="select",
        label="Are you authorized to work in the US?",
        name="work_authorization",
        options=["Yes, I am authorized", "No, I am not authorized"],
    )
    [res] = resolve([field], {"work_authorization": "yes, i am"}, resume_path=None)
    assert res.action == "fill"
    assert res.value == "Yes, I am authorized"


def test_resolve_select_ambiguous_match_asks():
    field = FieldSpec(
        selector="#sel3",
        kind="select",
        label="Are you authorized to work in the US?",
        name="work_authorization",
        options=["Authorized - full time", "Authorized - part time"],
    )
    [res] = resolve([field], {"work_authorization": "authorized"}, resume_path=None)
    assert res.action == "ask"


def test_resolve_select_fuzzy_typo_match_fills():
    field = FieldSpec(
        selector="#sel4",
        kind="select",
        label="Are you authorized to work in the US?",
        name="work_authorization",
        options=["Authorized", "Not authorized"],
    )
    [res] = resolve([field], {"work_authorization": "authorised"}, resume_path=None)
    assert res.action == "fill"
    assert res.value == "Authorized"


def test_resolve_select_fuzzy_near_tie_asks():
    field = FieldSpec(
        selector="#sel5",
        kind="select",
        label="Are you authorized to work in the US?",
        name="work_authorization",
        options=["Data Analytics I", "Data Analytics 2"],
    )
    [res] = resolve([field], {"work_authorization": "data analytiks"}, resume_path=None)
    assert res.action == "ask"


def test_resolve_select_fuzzy_dissimilar_asks():
    field = FieldSpec(
        selector="#sel6",
        kind="select",
        label="Are you authorized to work in the US?",
        name="work_authorization",
        options=["Yes", "No"],
    )
    [res] = resolve([field], {"work_authorization": "Yep"}, resume_path=None)
    assert res.action == "ask"


def test_resolve_no_stored_answer_asks():
    field = FieldSpec(selector="#e1", kind="email", name="email")
    [res] = resolve([field], {}, resume_path=None)
    assert res.action == "ask"
    assert res.semantic_key == "email"


def test_resolve_happy_path_fill():
    field = FieldSpec(selector="#e2", kind="email", name="email")
    [res] = resolve([field], {"email": "ed@example.com"}, resume_path=None)
    assert res.action == "fill"
    assert res.value == "ed@example.com"


def test_resolve_resume_upload_with_path():
    field = FieldSpec(selector="#r1", kind="file", label="Resume/CV")
    [res] = resolve([field], {}, resume_path="resume/founder.pdf")
    assert res.action == "upload"
    assert res.value == "resume/founder.pdf"


def test_resolve_resume_upload_without_path_asks():
    field = FieldSpec(selector="#r2", kind="file", label="Resume/CV")
    [res] = resolve([field], {}, resume_path=None)
    assert res.action == "ask"
    assert "resume" in res.reason.lower()


# --- values_match -------------------------------------------------------


def test_values_match_whitespace_and_case():
    assert values_match("Alex  Rivera", "alex rivera") is True


def test_values_match_phone_digit_tolerance():
    assert values_match("(555) 010-0100", "555-010-0100") is True


def test_values_match_empty_readback_false():
    assert values_match("alex.rivera@example.com", "") is False


def test_values_match_mismatch_false():
    assert values_match("alex.rivera@example.com", "someone@else.com") is False


# --- draft review gate (apply_resolutions / apply_one / run_assist) -----


class _FakeLocator:
    """Minimal Playwright-like locator: records .fill() calls and echoes
    the last one back via .input_value() — enough to drive _apply_one for
    a text/textarea field without a real browser. It has no .evaluate(), so
    fill's react-select detection fails and the plain fill path runs. Calls
    take **kwargs because fill passes explicit Playwright timeouts."""

    def __init__(self):
        self.value = ""
        self.fill_calls: list[str] = []

    @property
    def first(self):
        return self

    def fill(self, value, **kwargs):
        self.fill_calls.append(value)
        self.value = value

    def input_value(self, **kwargs):
        return self.value


class _FakeFillPage:
    def __init__(self):
        self.locators: dict[str, _FakeLocator] = {}

    def locator(self, selector):
        return self.locators.setdefault(selector, _FakeLocator())


def _draft_resolution() -> Resolution:
    field = FieldSpec(selector="#why", kind="textarea", label="Why us?")
    return Resolution(
        field=field,
        semantic_key=None,
        action="fill",
        value="Because I like the mission.",
        draft=True,
    )


def test_apply_resolutions_skips_draft_answers():
    page = _FakeFillPage()
    outcomes = fill.apply_resolutions(page, [_draft_resolution()], settle_ms=0)
    assert outcomes == []
    assert page.locators == {}


def test_apply_one_types_a_draft_after_approval():
    page = _FakeFillPage()
    outcome = fill.apply_one(page, _draft_resolution(), settle_ms=0)
    assert outcome.committed is True
    assert outcome.value == "Because I like the mission."
    assert page.locators["#why"].fill_calls == ["Because I like the mission."]


def test_run_assist_drafts_land_in_digest_not_filled_or_residual(tmp_path, monkeypatch):
    field = FieldSpec(
        selector="#why", kind="textarea", label="Why us?", name="why_us"
    )
    draft_res = Resolution(
        field=field,
        semantic_key=None,
        action="fill",
        value="Because I like the mission.",
        draft=True,
    )

    monkeypatch.setattr(fill, "scan_fields", lambda page: [field])
    monkeypatch.setattr(
        fill,
        "_apply_tailoring",
        lambda conn, posting_id, url, resolutions, budget, sink: ([draft_res], []),
    )

    page = _FakeFillPage()
    conn = db.connect(tmp_path / "run_assist.db")
    try:
        digest = fill.run_assist(
            page,
            1,
            "https://boards.greenhouse.io/acme/jobs/1",
            {},
            None,
            conn=conn,
            tailor=True,
        )
    finally:
        conn.close()

    assert digest.drafts == [draft_res]
    assert digest.filled == []
    assert digest.residual == []


def test_apply_resolutions_types_draft_answers_only_for_the_handoff_lane(monkeypatch):
    """`type_drafts` (the handoff lane: the candidate's Submit click approves the text) types a
    draft through the same apply-and-verify path; the default still never does."""
    monkeypatch.setattr(fill, "_pause", lambda seconds: None)
    page = _FakeFillPage()

    assert fill.apply_resolutions(page, [_draft_resolution()], settle_ms=0, autoapply=True) == []
    assert page.locators == {}

    outcomes = fill.apply_resolutions(
        page, [_draft_resolution()], settle_ms=0, autoapply=True, type_drafts=True
    )

    assert len(outcomes) == 1
    assert outcomes[0].committed is True and outcomes[0].error == ""
    assert outcomes[0].value == "Because I like the mission."
    assert page.locators["#why"].fill_calls == ["Because I like the mission."]


def _run_assist_with_a_draft(tmp_path, monkeypatch, **kwargs):
    field = FieldSpec(selector="#why", kind="textarea", label="Why us?", name="why_us")
    draft_res = Resolution(
        field=field, semantic_key=None, action="fill", value="Because I like the mission.", draft=True
    )
    calls: list[dict] = []

    def fake_tailoring(conn, posting_id, url, resolutions, budget, sink, **lane):
        calls.append(lane)
        return [draft_res], []

    monkeypatch.setattr(fill, "scan_fields", lambda page, **kw: [field])
    monkeypatch.setattr(fill, "_apply_tailoring", fake_tailoring)
    monkeypatch.setattr(fill, "_pause", lambda seconds: None)
    page = _FakeFillPage()
    conn = db.connect(tmp_path / "run_assist_lane.db")
    try:
        digest = fill.run_assist(
            page, 1, "https://boards.greenhouse.io/acme/jobs/1", {}, None,
            conn=conn, tailor=True, **kwargs,
        )
    finally:
        conn.close()
    return digest, page, calls, draft_res


def test_run_assist_handoff_lane_types_the_draft_and_lists_it(tmp_path, monkeypatch):
    digest, page, calls, draft_res = _run_assist_with_a_draft(
        tmp_path, monkeypatch, autoapply=True, type_drafts=True
    )

    assert calls == [{"autoapply": True}]  # the unattended lane has no score gate
    assert digest.drafts == [draft_res]
    assert [o.field.selector for o in digest.filled] == ["#why"]
    assert digest.filled[0].committed is True
    assert page.locators["#why"].fill_calls == ["Because I like the mission."]


def test_run_assist_autoapply_without_type_drafts_leaves_the_draft_untyped(tmp_path, monkeypatch):
    digest, page, calls, draft_res = _run_assist_with_a_draft(tmp_path, monkeypatch, autoapply=True)

    assert digest.drafts == [draft_res]
    assert digest.filled == []
    assert page.locators == {}


def test_run_assist_assisted_lane_never_types_drafts_and_keeps_the_score_gate(tmp_path, monkeypatch):
    """type_drafts has no effect outside the unattended lane, and the assisted lane's
    _apply_tailoring call carries no extra argument (so its score gate stays on)."""
    digest, page, calls, draft_res = _run_assist_with_a_draft(
        tmp_path, monkeypatch, type_drafts=True
    )

    assert calls == [{}]
    assert digest.drafts == [draft_res]
    assert digest.filled == []
    assert page.locators == {}


# --- run_assist and the company the posting is for ---------------------------------
# A how-heard question that spells the company out is named only when classify() is
# told the company. The field-mapper must still be handed only what classify() cannot
# name with NO company (its own assertion), so run_assist filters on both.

_HEARD = FieldSpec(
    selector="#heard", kind="text", label="How did you first learn about Veeva? ✱", required=True
)
_HEARD_AND_START = FieldSpec(
    selector="#both",
    kind="text",
    label="How did you first learn about Veeva? When can you start?",
    required=True,
)
_COLOR = FieldSpec(selector="#color", kind="text", label="Favorite color?")
_COMPANY_ANSWERS = {"how_heard": "Company careers page", "start_date": "Immediately"}


def _run_with_a_mapper_spy(tmp_path, monkeypatch, company):
    """run_assist over the three fields above, scan-only, with the real field-mapper
    (no budget: it never reaches the network) behind a spy that notes what it was
    handed. Returns (digest, [selectors handed to the mapper])."""
    monkeypatch.setattr(fill, "scan_fields", lambda page: [_HEARD, _HEARD_AND_START, _COLOR])
    handed: list[list[str]] = []
    real = fill.mapper_map_unknown

    def spy(conn, unknown, budget, **kwargs):
        handed.append([f.selector for f in unknown])
        return real(conn, unknown, budget, **kwargs)  # asserts classify(f) is None

    monkeypatch.setattr(fill, "mapper_map_unknown", spy)
    conn = db.connect(tmp_path / "company.db")
    try:
        digest = fill.run_assist(
            _FakeFillPage(),
            1,
            "https://jobs.lever.co/veeva/1",
            dict(_COMPANY_ANSWERS),
            None,
            scan_only=True,
            conn=conn,
            company=company,
        )
    finally:
        conn.close()
    return digest, handed


def test_the_company_changes_what_classify_names_for_the_fields_under_test():
    # The premises of the tests below, so they cannot pass for want of them.
    assert classify(_HEARD) is None
    assert classify(_HEARD, "Veeva Systems") == "how_heard"
    assert classify(_HEARD_AND_START) == "start_date"
    assert classify(_HEARD_AND_START, "Veeva Systems") is None  # two answers fit: none does
    assert classify(_COLOR) is None
    assert classify(_COLOR, "Veeva Systems") is None


def test_run_assist_hands_the_mapper_only_what_the_company_does_not_name_either(
    tmp_path, monkeypatch
):
    digest, handed = _run_with_a_mapper_spy(tmp_path, monkeypatch, "Veeva Systems")
    # #heard is named by the company; #both was named start_date without it and is
    # nothing with it. Neither is the mapper's to name (its assertion would fail).
    assert handed == [["#color"]]
    by_selector = {r.field.selector: r for r in digest.residual}
    heard = by_selector["#heard"]
    assert (heard.semantic_key, heard.action) == ("how_heard", "fill")
    assert (heard.value, heard.reason) == ("Company careers page", "scan-only run")
    both = by_selector["#both"]
    assert (both.semantic_key, both.action, both.value) == (None, "ask", None)
    assert both.reason == "unrecognized field"  # asked, not filled with the start date
    assert by_selector["#color"].reason == "unrecognized field"
    assert digest.filled == []


def test_run_assist_without_a_company_is_what_it_was(tmp_path, monkeypatch):
    digest, handed = _run_with_a_mapper_spy(tmp_path, monkeypatch, None)
    assert handed == [["#heard", "#color"]]  # nothing classify() cannot name goes unasked
    by_selector = {r.field.selector: r for r in digest.residual}
    assert by_selector["#heard"].reason == "unrecognized field"
    both = by_selector["#both"]
    assert (both.semantic_key, both.action, both.value) == ("start_date", "fill", "Immediately")
    assert both.reason == "scan-only run"


# --- run_assist and a residence question -------------------------------------------

_WHERE = FieldSpec(
    selector="#where", kind="text", label="Where are you currently located?*", required=True
)
_PLACE_ANSWERS = {"location": "Atlanta, Georgia", "country": "United States"}


def test_a_scan_only_run_keeps_the_country_a_residence_question_may_fall_back_to(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(fill, "scan_fields", lambda page: [_WHERE])
    digest = fill.run_assist(
        _FakeFillPage(),
        1,
        "https://boards.greenhouse.io/acme/jobs/1",
        dict(_PLACE_ANSWERS),
        None,
        scan_only=True,
    )
    [residual] = digest.residual
    assert (residual.semantic_key, residual.action) == ("location", "fill")
    assert (residual.value, residual.fallback_value) == ("Atlanta, Georgia", "United States")
    assert residual.reason == "scan-only run"
    assert digest.filled == []


def test_a_plain_text_input_is_given_the_city_and_never_the_country(tmp_path, monkeypatch):
    # The fake page has no combobox to detect: the box is a plain text input.
    monkeypatch.setattr(fill, "scan_fields", lambda page: [_WHERE])
    page = _FakeFillPage()
    digest = fill.run_assist(
        page, 1, "https://boards.greenhouse.io/acme/jobs/1", dict(_PLACE_ANSWERS), None
    )
    [outcome] = digest.filled
    assert (outcome.committed, outcome.value) == (True, "Atlanta, Georgia")
    assert page.locators["#where"].fill_calls == ["Atlanta, Georgia"]

# --- hydration wait (Greenhouse server-renders, React hydrates later) ---
# Greenhouse's form exists at DOMContentLoaded, but React attaches to it 0.2-1 s
# later and the next re-render wipes any value typed in between.


class _HydrationPage:
    """page.evaluate answers the field-count script with `count` and the
    hydration script from a scripted sequence (the last entry repeats; an
    Exception entry is raised). wait_for_timeout records the wait and really
    sleeps at most 20 ms: a monotonic deadline still elapses, and the suite
    skips the production 250/300 ms waits."""

    def __init__(self, hydration, count=3):
        self.hydration = list(hydration)
        self.count = count
        self.hydration_calls = 0
        self.waits: list[int] = []

    def evaluate(self, script, arg=None):
        if script == fill._FIELD_COUNT_JS:
            return self.count
        assert script == fill._HYDRATION_JS, "wait_for_fields ran an unexpected script"
        item = self.hydration[min(self.hydration_calls, len(self.hydration) - 1)]
        self.hydration_calls += 1
        if isinstance(item, Exception):
            raise item
        return item

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        time.sleep(min(ms, 20) / 1000)


def test_wait_for_fields_waits_for_ssr_hydration_to_finish():
    page = _HydrationPage(
        [
            {"ssr": True, "pending": 2},
            {"ssr": True, "pending": 1},
            {"ssr": True, "pending": 0},
        ]
    )
    assert fill.wait_for_fields(page) == 3
    # Two polls while fields were still un-hydrated, then one settle beat.
    assert page.waits == [250, 250, fill._HYDRATION_SETTLE_MS]
    assert page.hydration_calls == 3


def test_wait_for_fields_on_an_already_hydrated_ssr_page_only_settles():
    page = _HydrationPage([{"ssr": True, "pending": 0}])
    assert fill.wait_for_fields(page) == 3
    assert page.waits == [fill._HYDRATION_SETTLE_MS]


def test_wait_for_fields_skips_the_hydration_wait_when_not_server_rendered():
    page = _HydrationPage([{"ssr": False, "pending": 5}])
    assert fill.wait_for_fields(page) == 3
    assert page.waits == []
    assert page.hydration_calls == 1


@pytest.mark.parametrize("result", [None, "ssr", 7, [], [{"ssr": True, "pending": 1}]])
def test_wait_for_fields_treats_a_non_dict_hydration_result_as_not_ssr(result):
    page = _HydrationPage([result])
    assert fill.wait_for_fields(page) == 3
    assert page.waits == []


def test_wait_for_fields_survives_a_hydration_probe_error():
    page = _HydrationPage([RuntimeError("Execution context was destroyed")])
    assert fill.wait_for_fields(page) == 3
    assert page.waits == []


def test_wait_for_fields_gives_up_on_hydration_after_the_timeout():
    page = _HydrationPage([{"ssr": True, "pending": 2}])  # never hydrates
    assert fill.wait_for_fields(page, poll_ms=10, hydrate_timeout_ms=60) == 3
    polls = page.waits[:-1]
    assert polls and all(ms == 10 for ms in polls)
    assert len(polls) < 40  # the deadline ended the wait, not the page
    assert page.waits[-1] == fill._HYDRATION_SETTLE_MS


def test_wait_for_fields_stops_waiting_once_the_page_stops_being_ssr():
    page = _HydrationPage([{"ssr": True, "pending": 1}, {"ssr": False, "pending": 0}])
    assert fill.wait_for_fields(page) == 3
    assert page.waits == [250]  # no settle beat: the page changed under us


def test_wait_for_fields_does_not_probe_hydration_before_a_form_exists():
    page = _HydrationPage([{"ssr": True, "pending": 1}], count=0)
    assert fill.wait_for_fields(page, timeout_ms=40, poll_ms=10) == 0
    assert page.hydration_calls == 0


def test_wait_for_fields_falls_back_to_a_plain_pause_when_the_page_is_closing(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(fill, "_pause", slept.append)

    class _ClosingPage(_HydrationPage):
        def wait_for_timeout(self, ms):
            raise RuntimeError("Target page, context or browser has been closed")

    page = _ClosingPage([{"ssr": True, "pending": 0}])
    assert fill.wait_for_fields(page) == 3
    assert slept == [fill._HYDRATION_SETTLE_MS / 1000]


def test_scan_fields_scans_only_after_hydration_settles():
    order: list[str] = []

    class _ScanPage:
        def evaluate(self, script, arg=None):
            if script == fill._FIELD_COUNT_JS:
                order.append("count")
                return 1
            if script == fill._HYDRATION_JS:
                order.append("hydration")
                return {"ssr": True, "pending": 0}
            assert script == fill._SCAN_JS
            order.append("scan")
            return [
                {
                    "tag": "input",
                    "type": "email",
                    "id": "email",
                    "selector": "#email",
                    "required": True,
                }
            ]

        def wait_for_timeout(self, ms):
            order.append(f"wait{ms}")

    specs = fill.scan_fields(_ScanPage())
    assert order == ["count", "hydration", f"wait{fill._HYDRATION_SETTLE_MS}", "scan"]
    assert [(s.selector, s.kind, s.required) for s in specs] == [("#email", "email", True)]


# --- verify pass (apply_resolutions re-reads what it filled) -------------


@pytest.fixture
def pauses(monkeypatch):
    """Record fill's sleeps instead of taking them."""
    seen: list[float] = []
    monkeypatch.setattr(fill, "_pause", seen.append)
    return seen


def _text_resolution(
    selector="#email", value="alex.rivera@example.com", kind="email", key="email"
) -> Resolution:
    field = FieldSpec(selector=selector, kind=kind, label=key.title(), required=True)
    return Resolution(field=field, semantic_key=key, action="fill", value=value)


class _WipedOnceLocator(_FakeLocator):
    """React hydrating after the first fill: the typed value survives the first
    read-back, then the next re-render wipes it."""

    def __init__(self):
        super().__init__()
        self.reads = 0

    def input_value(self, **kwargs):
        self.reads += 1
        value = self.value
        if self.reads == 1:
            self.value = ""
        return value


class _EmptyReadLocator(_FakeLocator):
    """A field that never keeps what is typed."""

    def input_value(self, **kwargs):
        return ""


class _FlakyReadLocator(_FakeLocator):
    """The element is gone by the time the verify pass re-reads it."""

    def __init__(self):
        super().__init__()
        self.reads = 0

    def input_value(self, **kwargs):
        self.reads += 1
        if self.reads > 1:
            raise RuntimeError("Element is not attached to the DOM")
        return self.value


class _RejectingLocator(_FakeLocator):
    def fill(self, value, **kwargs):
        self.fill_calls.append(value)
        raise RuntimeError("Timeout 10000ms exceeded.")


class _SpyLocator(_FakeLocator):
    def __init__(self):
        super().__init__()
        self.kwargs: list[tuple[str, dict]] = []

    def fill(self, value, **kwargs):
        self.kwargs.append(("fill", kwargs))
        super().fill(value, **kwargs)

    def input_value(self, **kwargs):
        self.kwargs.append(("input_value", kwargs))
        return super().input_value(**kwargs)


def test_verify_pass_refills_a_value_wiped_after_the_first_read_back(pauses):
    page = _FakeFillPage()
    page.locators["#email"] = _WipedOnceLocator()
    outcomes = fill.apply_resolutions(page, [_text_resolution()], settle_ms=0)
    assert [(o.committed, o.error) for o in outcomes] == [(True, "")]
    assert outcomes[0].readback == "alex.rivera@example.com"
    # typed once, wiped, then re-typed by the verify pass: exactly one retry
    assert page.locators["#email"].fill_calls == ["alex.rivera@example.com"] * 2
    assert pauses == [fill._VERIFY_DELAY_S]


def test_verify_pass_leaves_a_stable_value_alone(pauses):
    page = _FakeFillPage()
    outcomes = fill.apply_resolutions(page, [_text_resolution()], settle_ms=0)
    assert [o.committed for o in outcomes] == [True]
    assert page.locators["#email"].fill_calls == ["alex.rivera@example.com"]
    assert pauses == [fill._VERIFY_DELAY_S]


def test_verify_pass_retries_exactly_once(pauses):
    page = _FakeFillPage()
    page.locators["#email"] = _EmptyReadLocator()
    outcomes = fill.apply_resolutions(page, [_text_resolution()], settle_ms=0)
    assert [o.committed for o in outcomes] == [False]
    assert page.locators["#email"].fill_calls == ["alex.rivera@example.com"] * 2


def test_verify_pass_never_executes_drafts(pauses):
    page = _FakeFillPage()
    outcomes = fill.apply_resolutions(
        page, [_draft_resolution(), _text_resolution()], settle_ms=0
    )
    assert [o.field.selector for o in outcomes] == ["#email"]
    assert set(page.locators) == {"#email"}  # the draft's #why was never even located


def test_verify_pass_reports_a_field_it_cannot_re_read(pauses):
    page = _FakeFillPage()
    page.locators["#email"] = _FlakyReadLocator()
    [outcome] = fill.apply_resolutions(page, [_text_resolution()], settle_ms=0)
    assert outcome.committed is False
    assert outcome.error == "verify read failed: Element is not attached to the DOM"
    assert page.locators["#email"].fill_calls == ["alex.rivera@example.com"]  # no re-fill unread


def test_verify_pass_does_not_retry_a_field_that_errored(pauses):
    page = _FakeFillPage()
    page.locators["#email"] = _RejectingLocator()
    [outcome] = fill.apply_resolutions(page, [_text_resolution()], settle_ms=0)
    assert outcome.committed is False
    assert outcome.error == "Timeout 10000ms exceeded."
    assert page.locators["#email"].fill_calls == ["alex.rivera@example.com"]


def test_apply_one_passes_bounded_timeouts_to_playwright():
    page = _FakeFillPage()
    spy = page.locators["#email"] = _SpyLocator()
    fill.apply_one(page, _text_resolution(), settle_ms=0)
    assert spy.kwargs == [
        ("fill", {"timeout": fill._ACTION_TIMEOUT_MS}),
        ("input_value", {"timeout": fill._READBACK_TIMEOUT_MS}),
    ]


def test_a_checkbox_is_never_auto_checked():
    class _NoTouchPage:
        def locator(self, selector):
            raise AssertionError("fill must not even locate a checkbox")

    field = FieldSpec(selector="#agree", kind="checkbox", label="I agree", required=True)
    res = Resolution(field=field, semantic_key="agree", action="fill", value="Yes")
    outcome = fill.apply_one(_NoTouchPage(), res, settle_ms=0)
    assert outcome.committed is False
    assert outcome.error == "unsupported kind for fill: checkbox"


# --- upload read-back (Greenhouse removes the input on change) -----------


class _UploadLocator:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    def evaluate(self, script, arg=None, **kwargs):
        assert script == fill._RECORD_UPLOAD_JS
        self.page.events.append("record-installed")
        self.page.record_args = arg
        if self.page.record_install_error:
            raise RuntimeError("Execution context was destroyed")
        self.page.listening = True

    def set_input_files(self, path, **kwargs):
        self.page.events.append("upload")
        self.page.uploaded = path
        # The capture-phase listener runs before the page's own change handler,
        # so the record exists by the time the page removes the input.
        if self.page.listening:
            self.page.record = {"name": Path(path).name, "size": 1, "count": 1}
        self.page.element_present = not self.page.removes_input


class _UploadPage:
    """A page whose file input (optionally) vanishes when it receives files.
    evaluate() answers fill's file read-back the way the script does: the live
    input's file name, else the upload record, else empty."""

    def __init__(self, *, removes_input=True, record_install_error=False):
        self.removes_input = removes_input
        self.record_install_error = record_install_error
        self.events: list[str] = []
        self.locator_selectors: list[str] = []
        self.readback_args: list = []
        self.listening = False
        self.record = None
        self.uploaded = None
        self.record_args = None
        self.element_present = True

    def locator(self, selector):
        self.locator_selectors.append(selector)
        return _UploadLocator(self)

    def evaluate(self, script, arg=None):
        assert script == fill._FILE_READBACK_JS
        self.readback_args.append(arg)
        if self.element_present and self.uploaded:
            return Path(self.uploaded).name
        if self.record and self.record["count"] >= 1:
            return self.record["name"]
        return ""


def _upload_resolution(path="/tmp/jobhunt/Alex Rivera Resume.pdf") -> Resolution:
    field = FieldSpec(selector="#resume", kind="file", label="Resume/CV", required=True)
    return Resolution(field=field, semantic_key="resume", action="upload", value=path)


def test_upload_reads_back_from_the_record_when_the_input_is_removed(pauses):
    page = _UploadPage(removes_input=True)
    [outcome] = fill.apply_resolutions(page, [_upload_resolution()], settle_ms=0)
    assert outcome.committed is True
    assert outcome.readback == "Alex Rivera Resume.pdf"
    assert outcome.error == ""
    # The record is installed BEFORE the upload so it exists when the page
    # removes the input.
    assert page.events == ["record-installed", "upload"]
    assert page.record_args == [fill.UPLOAD_RECORD_KEY, "#resume"]
    # Read back through the page, never through the removed element: the only
    # locator lookup is the upload itself, and the verify pass skips uploads.
    assert page.locator_selectors == ["#resume"]
    assert page.readback_args == [[fill.UPLOAD_RECORD_KEY, "#resume"]]


def test_upload_reads_the_live_input_when_the_page_keeps_it(pauses):
    page = _UploadPage(removes_input=False)
    [outcome] = fill.apply_resolutions(page, [_upload_resolution()], settle_ms=0)
    assert outcome.committed is True
    assert outcome.readback == "Alex Rivera Resume.pdf"


def test_upload_still_runs_when_the_record_cannot_be_installed(pauses):
    page = _UploadPage(removes_input=False, record_install_error=True)
    [outcome] = fill.apply_resolutions(page, [_upload_resolution()], settle_ms=0)
    assert page.events == ["record-installed", "upload"]
    assert (outcome.committed, outcome.error) == (True, "")


def test_upload_without_any_proof_is_uncommitted_but_not_an_error(pauses):
    page = _UploadPage(removes_input=True, record_install_error=True)
    [outcome] = fill.apply_resolutions(page, [_upload_resolution()], settle_ms=0)
    assert outcome.committed is False  # input gone and no record: nothing shows it landed
    assert outcome.readback == ""
    assert outcome.error == ""  # set_input_files itself succeeded


def test_upload_that_raises_is_an_error_outcome(pauses):
    class _BrokenUploadPage(_UploadPage):
        def locator(self, selector):
            loc = super().locator(selector)
            loc.set_input_files = lambda path, **kw: (_ for _ in ()).throw(
                RuntimeError("Timeout 10000ms exceeded.")
            )
            return loc

    [outcome] = fill.apply_resolutions(
        _BrokenUploadPage(), [_upload_resolution()], settle_ms=0
    )
    assert outcome.committed is False
    assert outcome.error == "Timeout 10000ms exceeded."


# --- react-select comboboxes (Country, most Greenhouse Yes/No questions) -


class _FakeCombobox:
    """A react-select v5 combobox without a browser. fill() types and filters
    the options (case-insensitive contains) and focuses the first match;
    ArrowDown opens a closed menu or moves the focus with wrap-around; Tab
    selects the focused option of an open menu. evaluate() answers the two
    scripts fill ships (combobox state, focused option). Typed text is never a
    value, as in the real widget: only Tab on a focused option sets it."""

    def __init__(self, options, *, value="", multi=False, readonly=False):
        self.options = list(options)
        self.value = value
        self.multi = multi
        self.readonly = readonly
        self.menu: list[str] = []
        self.focus_idx = -1
        self.is_open = False
        self.keys: list[str] = []
        self.tabbed: list[str] = []
        self.fill_calls: list[str] = []
        self.focus_calls = 0

    @property
    def first(self):
        return self

    def evaluate(self, script, arg=None, **kwargs):
        if script == fill._REACT_SELECT_JS:
            return {"multi": self.multi, "readonly": self.readonly, "value": self.value}
        assert script == fill._FOCUSED_OPTION_JS
        if self.is_open and 0 <= self.focus_idx < len(self.menu):
            return self.menu[self.focus_idx]
        return ""

    def fill(self, text, **kwargs):
        self.fill_calls.append(text)
        needle = text.casefold()
        self.menu = [o for o in self.options if needle in o.casefold()]
        self.is_open = bool(text)
        self.focus_idx = 0 if self.menu else -1

    def focus(self, **kwargs):
        self.focus_calls += 1

    def press(self, key, **kwargs):
        self.keys.append(key)
        if key == "ArrowDown":
            if not self.is_open:
                self.is_open, self.menu, self.focus_idx = True, list(self.options), 0
            elif self.menu:
                self.focus_idx = (self.focus_idx + 1) % len(self.menu)
        elif key == "Tab":
            if self.is_open and 0 <= self.focus_idx < len(self.menu):
                self.value = self.menu[self.focus_idx]
                self.tabbed.append(self.value)
            self.is_open = False

    def input_value(self, **kwargs):
        raise AssertionError("a combobox is read back from its single-value, not typed text")


class _ComboboxPage:
    def __init__(self, box):
        self.box = box

    def locator(self, selector):
        return self.box


_COUNTRIES = ["Canada", "United Kingdom", "United States", "United States Minor Outlying Islands"]


def _combobox_outcome(box, answer, *, selector="#country"):
    field = FieldSpec(selector=selector, kind="text", label="Country", required=True)
    res = Resolution(field=field, semantic_key="country", action="fill", value=answer)
    return fill.apply_one(_ComboboxPage(box), res, settle_ms=0)


def test_react_select_exact_option_is_committed_with_tab(pauses):
    box = _FakeCombobox(_COUNTRIES)
    outcome = _combobox_outcome(box, "United States")
    assert (outcome.committed, outcome.error) == (True, "")
    assert outcome.readback == "United States"
    assert box.fill_calls == ["United States"]
    assert box.keys == ["Tab"]
    assert box.tabbed == ["United States"]


def test_react_select_moves_past_a_non_exact_first_option_with_arrow_down(pauses):
    box = _FakeCombobox(["United States Minor Outlying Islands", "United States", "Canada"])
    outcome = _combobox_outcome(box, "United States")
    assert (outcome.committed, outcome.error) == (True, "")
    assert outcome.readback == "United States"
    assert box.keys == ["ArrowDown", "Tab"]
    assert box.tabbed == ["United States"]


def test_react_select_without_an_exact_option_selects_nothing(pauses):
    box = _FakeCombobox(["Mexico", "United States"])
    outcome = _combobox_outcome(box, "Canada")
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'Canada'"
    assert box.fill_calls == ["Canada", ""]  # typed, then the filter cleared
    assert box.keys == []  # nothing was focused, so no key was sent
    assert box.value == ""


def test_react_select_never_tabs_a_near_miss(pauses):
    box = _FakeCombobox(["United States Minor Outlying Islands", "United Kingdom"])
    outcome = _combobox_outcome(box, "United States")
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'United States'"
    assert box.keys == ["ArrowDown"]  # one lap: the lone match wrapped onto itself
    assert box.fill_calls == ["United States", ""]
    assert box.value == ""


def test_react_select_multi_select_is_refused_without_typing(pauses):
    box = _FakeCombobox(_COUNTRIES, multi=True)
    outcome = _combobox_outcome(box, "Canada")
    assert outcome.committed is False
    assert outcome.error == "multi-select combobox not supported"
    assert box.fill_calls == [] and box.keys == []


def test_react_select_already_holding_the_answer_is_left_alone(pauses):
    box = _FakeCombobox(_COUNTRIES, value="United States")
    outcome = _combobox_outcome(box, "united  states")
    assert (outcome.committed, outcome.error) == (True, "")
    assert box.fill_calls == [] and box.keys == []


def test_react_select_exact_match_ignores_case_and_edge_whitespace_only(pauses):
    box = _FakeCombobox(["  UNITED STATES  "])
    outcome = _combobox_outcome(box, "United States")
    assert (outcome.committed, outcome.error) == (True, "")
    assert box.tabbed == ["  UNITED STATES  "]


def test_react_select_readonly_opens_with_arrow_down_and_walks_to_the_option(pauses):
    box = _FakeCombobox(["Yes", "No", "Maybe"], readonly=True)
    outcome = _combobox_outcome(box, "No", selector="#question_1")
    assert (outcome.committed, outcome.error) == (True, "")
    assert outcome.readback == "No"
    assert box.focus_calls == 1 and box.fill_calls == []
    assert box.keys == ["ArrowDown", "ArrowDown", "Tab"]


def test_react_select_readonly_with_no_match_does_one_lap_and_types_nothing(pauses):
    box = _FakeCombobox(["Yes", "No"], readonly=True)
    outcome = _combobox_outcome(box, "Maybe")
    assert outcome.committed is False
    assert outcome.error == "no option exactly matching 'Maybe'"
    assert box.fill_calls == []  # nothing typed, so nothing to clear
    assert box.keys == ["ArrowDown"] * 3  # open, Yes -> No -> Yes (wrapped: stop)
    assert box.value == ""


def test_react_select_gives_up_after_the_arrow_press_bound(pauses):
    box = _FakeCombobox([f"Option {n}" for n in range(100)])
    outcome = _combobox_outcome(box, "Option")  # every option contains it, none equals it
    assert outcome.committed is False
    assert box.keys == ["ArrowDown"] * fill._MAX_ARROW_PRESSES
    assert box.value == ""


@pytest.mark.parametrize(
    "options,answer,readonly",
    [
        (_COUNTRIES, "United States", False),
        (["United States Minor Outlying Islands", "United States"], "United States", False),
        (["Mexico"], "Canada", False),
        (["Yes", "No"], "No", True),
        (["Yes", "No"], "Maybe", True),
    ],
)
def test_react_select_only_ever_presses_tab_and_arrow_down(pauses, options, answer, readonly):
    box = _FakeCombobox(options, readonly=readonly)
    _combobox_outcome(box, answer)
    assert set(box.keys) <= {"Tab", "ArrowDown"}
    # Tab only ever lands on the option whose text is the answer.
    assert all(text.casefold() == answer.casefold() for text in box.tabbed)


def test_read_back_of_a_combobox_is_its_single_value_not_the_typed_text():
    box = _FakeCombobox(_COUNTRIES, value="Canada")
    field = FieldSpec(selector="#country", kind="text", label="Country")
    assert fill._read_back(_ComboboxPage(box), field) == "Canada"


# --- static guard: fill.py stays select-only -----------------------------


def _fill_tree() -> ast.Module:
    return ast.parse(inspect.getsource(fill))


def _attribute_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]


def test_fill_only_presses_tab_and_arrow_down():
    presses = [c for c in _attribute_calls(_fill_tree()) if c.func.attr == "press"]
    assert len(presses) >= 3, "the guard is vacuous if it finds no key presses"
    for call in presses:
        key = call.args[0] if call.args else None
        assert isinstance(key, ast.Constant) and key.value in {"Tab", "ArrowDown"}, ast.dump(
            call
        )


def test_fill_never_clicks_submits_or_checks_a_checkbox():
    tree = _fill_tree()
    called = {call.func.attr for call in _attribute_calls(tree)}
    forbidden_calls = {
        "click",
        "dblclick",
        "tap",
        "dispatch_event",
        "submit",
        "request_submit",
        "type",  # press_sequentially is the one typing call, and has its own rule below
        "set_checked",
        "uncheck",
        "hover",
        "drag_to",
    }
    assert not called & forbidden_calls
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attributes & {"keyboard", "mouse", "touchscreen"}
    # .check() selects a radio; only _check_radio may call it.
    checkers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for call in _attribute_calls(fn)
        if call.func.attr == "check"
    }
    assert checkers == {"_check_radio"}
    # .press_sequentially() sends real key events, so a "\n" in its text is an Enter
    # press that can submit the form. Only _type_like_a_person may touch it (named,
    # called or aliased), and only _commit_react_select may reach that helper.
    typists = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute) and node.attr == "press_sequentially"
    }
    assert typists == {"_type_like_a_person"}
    reachers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Name) and node.id == "_type_like_a_person"
    }
    assert reachers == {"_commit_react_select"}


def test_fill_types_only_text_that_passed_the_fail_closed_check_first():
    """Whatever _type_like_a_person types has been through _typeable, before any
    key or any other action of its own, and _typeable refuses control, format and
    line-break characters outright (never cleaning them into typeable text)."""
    helper = next(
        fn
        for fn in ast.walk(_fill_tree())
        if isinstance(fn, ast.FunctionDef) and fn.name == "_type_like_a_person"
    )
    checks = [
        node.lineno
        for node in ast.walk(helper)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_typeable"
    ]
    actions = [c.lineno for c in _attribute_calls(helper)]
    assert len(checks) == 1 and checks[0] < min(actions), "_typeable must run before any key"
    for refused in (
        "United\nStates",
        "United\rStates",
        "United\tStates",
        "United\x1bStates",
        "United​States",
        "United States",
        "United States\n",
    ):
        assert fill._typeable(refused) == "", repr(refused)
    assert fill._typeable("  United   States ") == "United States"


def test_every_playwright_call_in_fill_has_an_explicit_timeout():
    actions = {
        "fill",
        "select_option",
        "set_input_files",
        "check",
        "press",
        "press_sequentially",
        "focus",
        "input_value",
    }
    checked = 0
    for call in _attribute_calls(_fill_tree()):
        receiver = call.func.value
        on_page = isinstance(receiver, ast.Name) and receiver.id == "page"
        if call.func.attr in actions or (call.func.attr == "evaluate" and not on_page):
            checked += 1
            assert any(kw.arg == "timeout" for kw in call.keywords), (
                f"{call.func.attr}() at fill.py line {call.lineno} has no timeout="
            )
    assert checked >= 10


def test_fill_scripts_never_dispatch_events_or_submit():
    scripts = {
        name: text
        for name, text in vars(fill).items()
        if name.endswith("_JS") and isinstance(text, str)
    }
    assert len(scripts) >= 8
    for name, text in scripts.items():
        for banned in (
            "dispatchEvent",
            ".click(",
            ".submit(",
            "requestSubmit",
            "new Event(",
            "new MouseEvent(",
            "new KeyboardEvent(",
            ".focus(",
        ):
            assert banned not in text, f"{name} contains {banned}"


# --- profile_store -------------------------------------------------------


def test_profile_store_set_get_update_delete(tmp_path):
    conn = db.connect(tmp_path / "answers.db")
    try:
        profile_store.set_answer(conn, "email", "ed@example.com")
        assert profile_store.get_answers(conn) == {"email": "ed@example.com"}

        profile_store.set_answer(conn, "email", "alex.rivera@example.com")
        assert profile_store.get_answers(conn) == {"email": "alex.rivera@example.com"}

        rows = profile_store.list_answers(conn)
        assert [r["semantic_key"] for r in rows] == ["email"]

        profile_store.delete_answer(conn, "email")
        assert profile_store.get_answers(conn) == {}
    finally:
        conn.close()


def test_profile_store_rejects_unknown_key(tmp_path):
    conn = db.connect(tmp_path / "answers2.db")
    try:
        with pytest.raises(ValueError):
            profile_store.set_answer(conn, "favorite_color", "blue")
    finally:
        conn.close()


# --- apply-profile import -------------------------------------------------


def test_import_answers_file_skips_and_maps(tmp_path):
    answers_file = tmp_path / "answers.local.md"
    answers_file.write_text(
        "\n".join(
            [
                "# header comment",
                "",
                "email: file@example.com",
                "earliest_start: December 2026",
                "github: [FILL]",
                "shoe_size: 11",
                "pronouns_eeo_demographics: Decline to self-identify",
            ]
        )
    )
    conn = db.connect(tmp_path / "import.db")
    try:
        profile_store.set_answer(conn, "email", "already@example.com")

        rows = cli._import_answers_file(conn, answers_file, overwrite=False)
        by_key = {file_key: (bucket, reason) for bucket, file_key, reason in rows}

        assert by_key["email"][0] == "skipped_already_set"
        assert by_key["github"][0] == "skipped_fill"
        assert by_key["shoe_size"][0] == "skipped_unmapped"
        assert by_key["pronouns_eeo_demographics"][0] == "skipped_unmapped"
        assert "sensitive" in by_key["pronouns_eeo_demographics"][1]
        assert by_key["earliest_start"] == ("imported", "-> start_date")

        answers = profile_store.get_answers(conn)
        assert answers["email"] == "already@example.com"
        assert answers["start_date"] == "December 2026"
    finally:
        conn.close()


def test_import_answers_file_overwrite(tmp_path):
    answers_file = tmp_path / "answers.local.md"
    answers_file.write_text("email: new@example.com\n")
    conn = db.connect(tmp_path / "import2.db")
    try:
        profile_store.set_answer(conn, "email", "old@example.com")

        not_overwritten = cli._import_answers_file(conn, answers_file, overwrite=False)
        assert not_overwritten[0][0] == "skipped_already_set"
        assert profile_store.get_answers(conn)["email"] == "old@example.com"

        overwritten = cli._import_answers_file(conn, answers_file, overwrite=True)
        assert overwritten[0][0] == "imported"
        assert profile_store.get_answers(conn)["email"] == "new@example.com"
    finally:
        conn.close()


# --- _queue_browser_gone ----------------------------------------------


class _FakeBrowser:
    def __init__(self, connected: bool):
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


class _FakeContext:
    def __init__(self, pages, browser=None):
        self.pages = pages
        self.browser = browser


def test_queue_browser_gone_open_tabs_connected():
    context = _FakeContext(pages=["page"], browser=_FakeBrowser(connected=True))
    assert cli._queue_browser_gone(context) is False


def test_queue_browser_gone_all_tabs_closed():
    context = _FakeContext(pages=[], browser=_FakeBrowser(connected=True))
    assert cli._queue_browser_gone(context) is True


def test_queue_browser_gone_browser_died_with_stale_pages():
    # Chrome crash/SIGTERM: Playwright's cached pages list never empties,
    # but the browser reports disconnected.
    context = _FakeContext(pages=["page"], browser=_FakeBrowser(connected=False))
    assert cli._queue_browser_gone(context) is True


def test_queue_browser_gone_no_browser_attr_still_waits():
    # Persistent contexts may expose browser=None; open tabs keep waiting.
    context = _FakeContext(pages=["page"], browser=None)
    assert cli._queue_browser_gone(context) is False


def test_queue_browser_gone_pages_raises():
    class _ClosedContext:
        @property
        def pages(self):
            raise RuntimeError("context closed")

    assert cli._queue_browser_gone(_ClosedContext()) is True


# --- driver singleton cleanup -----------------------------------------


def _make_singleton_symlinks(profile_dir, target: str) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (profile_dir / name).symlink_to(target)


def test_cleanup_stale_singleton_removes_dead_owner(tmp_path, monkeypatch):
    from jobhunt.apply_assist import driver

    _make_singleton_symlinks(tmp_path, "somehost-4242")
    monkeypatch.setattr(driver, "_pid_alive", lambda pid: False)

    driver._cleanup_stale_singleton(tmp_path)

    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        assert not (tmp_path / name).is_symlink()


def test_cleanup_stale_singleton_keeps_live_owner(tmp_path):
    import os

    from jobhunt.apply_assist import driver

    _make_singleton_symlinks(tmp_path, f"somehost-{os.getpid()}")

    driver._cleanup_stale_singleton(tmp_path)

    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        assert (tmp_path / name).is_symlink()


def test_cleanup_stale_singleton_ignores_regular_file(tmp_path):
    from jobhunt.apply_assist import driver

    lock = tmp_path / "SingletonLock"
    lock.write_text("not a symlink")

    driver._cleanup_stale_singleton(tmp_path)

    assert lock.exists()


def test_cleanup_stale_singleton_ignores_unparseable_target(tmp_path):
    from jobhunt.apply_assist import driver

    _make_singleton_symlinks(tmp_path, "no-pid-here")

    driver._cleanup_stale_singleton(tmp_path)

    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        assert (tmp_path / name).is_symlink()


def test_cleanup_stale_singleton_missing_lock_noop(tmp_path):
    from jobhunt.apply_assist import driver

    driver._cleanup_stale_singleton(tmp_path)  # must not raise


def test_pid_alive():
    import os
    import subprocess

    from jobhunt.apply_assist import driver

    assert driver._pid_alive(os.getpid()) is True

    proc = subprocess.Popen(["true"])
    proc.wait()
    assert driver._pid_alive(proc.pid) is False


def test_persistent_launch_failure_surfaces_underlying_errors(tmp_path, monkeypatch):
    from jobhunt.apply_assist import driver

    class _FakeChromium:
        def launch_persistent_context(self, **kwargs):
            if "channel" in kwargs:
                raise RuntimeError("stale SingletonLock held by orphaned Chrome")
            raise RuntimeError("Executable doesn't exist at .../chromium")

    class _FakePlaywright:
        chromium = _FakeChromium()

        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    fake_pw = _FakePlaywright()

    class _FakeManager:
        def start(self):
            return fake_pw

    monkeypatch.setattr(driver, "_PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(driver, "sync_playwright", lambda: _FakeManager())

    with pytest.raises(RuntimeError) as excinfo:
        driver._open_context_via_persistent()

    message = str(excinfo.value)
    assert "stale SingletonLock held by orphaned Chrome" in message
    assert "Executable doesn't exist" in message
    assert fake_pw.stopped is True


def test_both_persistent_launches_disable_the_chrome_updater(tmp_path, monkeypatch):
    # Branded Chrome's GoogleUpdater holds Playwright's stderr pipe open, so
    # context.close() waited up to five minutes for it. Both launches (system
    # Chrome, then the bundled-Chromium fallback) must send the switch.
    from jobhunt.apply_assist import driver

    launches: list[dict] = []

    class _FakeChromium:
        def launch_persistent_context(self, **kwargs):
            launches.append(kwargs)
            if "channel" in kwargs:
                raise RuntimeError("no system Chrome")
            return object()

    class _FakePlaywright:
        chromium = _FakeChromium()

    class _FakeManager:
        def start(self):
            return _FakePlaywright()

    monkeypatch.setattr(driver, "_PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(driver, "sync_playwright", lambda: _FakeManager())

    driver._open_context_via_persistent()

    assert ["channel" in launch for launch in launches] == [True, False]
    for launch in launches:
        assert "--disable-updater-scheduler" in launch["args"]
        assert "--hide-crash-restore-bubble" in launch["args"]
        assert "--disable-session-crashed-bubble" in launch["args"]


# --- a prose question rendered as a single-line text input (unattended lane) ----------


def _run_assist_over_text_inputs(tmp_path, monkeypatch, fields, answer, **kwargs):
    """run_assist with the real resolve() and the real _apply_tailoring; only the mapper
    and the drafter's model call are stubbed."""
    from jobhunt.apply_assist.mapper import MapperResult
    from jobhunt.apply_assist.tailor_answers import TailorResult, fingerprint, question_text

    drafted: list[list[FieldSpec]] = []

    def fake_draft(conn, posting_id, eligible, budget, **kw):
        drafted.append(list(eligible))
        return TailorResult(
            posting_id=posting_id,
            drafts={fingerprint(question_text(f)): (question_text(f), answer) for f in eligible},
            status="ok", cache_hits=0, misses=len(eligible), drafted=len(eligible), cost_usd=0.0,
        )

    monkeypatch.setattr(fill, "scan_fields", lambda page, **kw: fields)
    monkeypatch.setattr(
        fill, "mapper_map_unknown",
        lambda conn, unknown, budget, host=None: MapperResult({}, "no_unknown_fields", 0, 0, 0, 0.0),
    )
    monkeypatch.setattr(fill, "draft_for_posting", fake_draft)
    monkeypatch.setattr(fill, "_pause", lambda seconds: None)
    page = _FakeFillPage()
    conn = db.connect(tmp_path / "text_prose.db")
    try:
        digest = fill.run_assist(
            page, 1, "https://boards.greenhouse.io/acme/jobs/1", {}, None,
            conn=conn, tailor=True, **kwargs,
        )
    finally:
        conn.close()
    return digest, page, drafted


_MELIO_Q = "In 1-2 sentences, what's the most complex thing you've done with SQL, APIs, or scripting?*"


def test_run_assist_handoff_lane_types_a_text_input_draft_and_lists_it(tmp_path, monkeypatch):
    field = FieldSpec(selector="#q", kind="text", label=_MELIO_Q, required=True)
    digest, page, drafted = _run_assist_over_text_inputs(
        tmp_path, monkeypatch, [field], "I built Example Ventures tools.", autoapply=True, type_drafts=True
    )

    assert drafted == [[field]]
    assert [r.field.selector for r in digest.drafts] == ["#q"]
    assert digest.residual == []
    assert page.locators["#q"].fill_calls == ["I built Example Ventures tools."]


def test_run_assist_click_mode_lists_a_text_input_draft_but_never_types_it(tmp_path, monkeypatch):
    field = FieldSpec(selector="#q", kind="text", label=_MELIO_Q, required=True)
    digest, page, drafted = _run_assist_over_text_inputs(
        tmp_path, monkeypatch, [field], "I built Example Ventures tools.", autoapply=True
    )

    assert [r.field.selector for r in digest.drafts] == ["#q"]  # the draft gate still sees it
    assert digest.filled == []
    assert page.locators == {}


@pytest.mark.parametrize("label", ["LinkedIn", "Website", "How did you hear about us?", "Current company"])
def test_run_assist_never_sends_fact_text_inputs_to_the_drafter(tmp_path, monkeypatch, label):
    field = FieldSpec(selector="#q", kind="text", label=label)
    digest, page, drafted = _run_assist_over_text_inputs(
        tmp_path, monkeypatch, [field], "x", autoapply=True, type_drafts=True
    )

    assert drafted == []
    assert digest.drafts == []


def test_run_assist_assisted_lane_does_not_draft_a_text_input(tmp_path, monkeypatch):
    field = FieldSpec(selector="#q", kind="text", label=_MELIO_Q, required=True)
    monkeypatch.setattr(fill, "load_rubric", lambda: {"version": 1})
    digest, page, drafted = _run_assist_over_text_inputs(tmp_path, monkeypatch, [field], "x")

    assert drafted == []
    assert digest.drafts == []
