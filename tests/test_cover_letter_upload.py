"""The cover-letter PDF upload in fill.run_assist (handoff lane only).

Fakes only: the page, the scan and `cover_letter.draft_cover_letter` are all
stand-ins, so no model, browser or network is touched. solve.resolve, the
real `_apply_tailoring` (no essay, so it drafts nothing) and `apply_resolutions`
run for real.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobhunt import db
from jobhunt.apply_assist import cover_letter, fill, submit
from jobhunt.apply_assist.schema import FieldSpec, FillOutcome, Resolution
from jobhunt.apply_assist.cover_letter import CoverLetterResult

URL = "https://boards.greenhouse.io/acme/jobs/1"
RESUME = FieldSpec(selector="#resume", kind="file", label="Resume/CV", name="resume", required=True)
COVER = FieldSpec(selector="#cover", kind="file", label="Cover Letter", name="cover_letter")
COVER_TWO = FieldSpec(selector="#cover2", kind="file", label="Cover Letter", name="cover_letter_2")


class _Locator:
    def __init__(self):
        self.files: list[str] = []

    @property
    def first(self):
        return self

    def set_input_files(self, path, **kwargs):
        self.files.append(path)


class _Page:
    def __init__(self):
        self.locators: dict[str, _Locator] = {}

    def locator(self, selector):
        return self.locators.setdefault(selector, _Locator())

    def evaluate(self, script, arg=None):
        """fill's file read-back: the name of the file the input received."""
        files = self.locators[arg[1]].files
        return Path(files[-1]).name if files else ""


class _Drafter:
    """draft_cover_letter stand-in: records its calls and returns (or raises) what it is told."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, conn, posting_id, **kwargs):
        self.calls.append({"conn": conn, "posting_id": posting_id, **kwargs})
        if self.error:
            raise self.error
        return self.result


def _ok(tmp_path) -> CoverLetterResult:
    pdf = tmp_path / "Alex_Rivera_Cover_Letter.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake letter")
    return CoverLetterResult(1, "ok", text="Dear team.", pdf_path=pdf)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "cover_upload.db")
    try:
        yield connection
    finally:
        connection.close()


def _run(tmp_path, conn, monkeypatch, drafter, *fields, **kwargs):
    resume = tmp_path / "Alex_Rivera_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake resume")
    monkeypatch.setattr(fill, "scan_fields", lambda page, **kw: list(fields) or [RESUME, COVER])
    monkeypatch.setattr(fill, "_pause", lambda seconds: None)
    monkeypatch.setattr(cover_letter, "draft_cover_letter", drafter)
    page = _Page()
    digest = fill.run_assist(page, 1, URL, {}, str(resume), conn=conn, **kwargs)
    return digest, page


HANDOFF = {"autoapply": True, "type_drafts": True, "tailor": True}


def _cover(digest) -> list[Resolution]:
    return [r for r in digest.residual + digest.drafts if r.semantic_key == "cover_letter"]


def test_handoff_lane_uploads_the_drafted_pdf_as_a_draft(tmp_path, conn, monkeypatch):
    result = _ok(tmp_path)
    drafter = _Drafter(result)
    audit = tmp_path / "audit"

    digest, page = _run(tmp_path, conn, monkeypatch, drafter, **HANDOFF, audit_root=audit)

    assert len(drafter.calls) == 1
    call = drafter.calls[0]
    assert (call["conn"], call["posting_id"], call["audit_root"]) == (conn, 1, audit)
    assert [f.selector for f in call["fields"]] == ["#resume", "#cover"]
    [draft] = digest.drafts
    assert (draft.semantic_key, draft.action, draft.draft) == ("cover_letter", "upload", True)
    assert draft.value == str(result.pdf_path)
    assert draft.reason == "cover letter drafted, review before submit"
    assert digest.residual == []
    assert page.locators["#cover"].files == [str(result.pdf_path)]
    cover_outcome = next(o for o in digest.filled if o.field.selector == "#cover")
    assert cover_outcome.error == "" and cover_outcome.value == str(result.pdf_path)
    assert cover_outcome.committed is True


def test_apply_resolutions_uploads_a_draft_only_when_type_drafts_is_set():
    pdf = "/tmp/letter.pdf"
    res = Resolution(COVER, "cover_letter", "upload", value=pdf, draft=True)

    page = _Page()
    assert fill.apply_resolutions(page, [res], settle_ms=0, autoapply=True) == []
    assert page.locators == {}

    outcomes = fill.apply_resolutions(page, [res], settle_ms=0, autoapply=True, type_drafts=True)
    assert [o.value for o in outcomes] == [pdf]
    assert page.locators["#cover"].files == [pdf]


@pytest.mark.parametrize(
    "lane",
    [
        {"autoapply": True, "tailor": False},  # click mode: no tailor, no type_drafts
        {"autoapply": True, "type_drafts": True, "tailor": False},
        {"autoapply": True, "tailor": True},  # no type_drafts
    ],
)
def test_click_mode_shapes_never_draft_and_keep_attach_by_hand(tmp_path, conn, monkeypatch, lane):
    drafter = _Drafter(_ok(tmp_path))

    digest, page = _run(tmp_path, conn, monkeypatch, drafter, **lane)

    assert drafter.calls == []
    [cover] = _cover(digest)
    assert (cover.action, cover.reason) == ("ask", "cover letter file — attach by hand")
    assert "#cover" not in page.locators


def test_a_run_without_a_connection_never_drafts(tmp_path, monkeypatch):
    drafter = _Drafter(_ok(tmp_path))
    monkeypatch.setattr(fill, "scan_fields", lambda page, **kw: [RESUME, COVER])
    monkeypatch.setattr(cover_letter, "draft_cover_letter", drafter)

    digest = fill.run_assist(_Page(), 1, URL, {}, None, scan_only=True, **HANDOFF)

    assert drafter.calls == []
    assert [r.reason for r in digest.residual if r.field.selector == "#cover"] == [
        "cover letter file — attach by hand"
    ]


def test_assist_lane_never_drafts(tmp_path, conn, monkeypatch):
    drafter = _Drafter(_ok(tmp_path))

    digest, page = _run(tmp_path, conn, monkeypatch, drafter, tailor=True, type_drafts=True)

    assert drafter.calls == []
    [cover] = _cover(digest)
    assert (cover.action, cover.reason) == ("ask", "cover letter file — attach by hand")


@pytest.mark.parametrize(
    "result,reason",
    [
        (CoverLetterResult(1, "ai_policy", detail="AI policy on the form"), fill.AI_POLICY_REASON),
        (CoverLetterResult(1, "rejected"), "cover letter not drafted (rejected), attach by hand"),
        (CoverLetterResult(1, "error"), "cover letter not drafted (error), attach by hand"),
        (CoverLetterResult(1, "gate_closed"), "cover letter not drafted (gate_closed), attach by hand"),
        (CoverLetterResult(1, "ok"), "cover letter not drafted (ok), attach by hand"),  # no pdf_path
    ],
)
def test_no_pdf_leaves_the_input_to_hand_with_the_status(tmp_path, conn, monkeypatch, result, reason):
    drafter = _Drafter(result)

    digest, page = _run(tmp_path, conn, monkeypatch, drafter, **HANDOFF)

    assert len(drafter.calls) == 1
    [cover] = _cover(digest)
    assert (cover.action, cover.reason, cover.draft) == ("ask", reason, False)
    assert cover in digest.residual and digest.drafts == []
    assert "#cover" not in page.locators
    assert [o.field.selector for o in digest.filled] == ["#resume"]  # the run went on


def test_a_raising_drafter_keeps_the_input_ask_and_the_run_going(tmp_path, conn, monkeypatch):
    drafter = _Drafter(error=RuntimeError("boom"))

    digest, page = _run(tmp_path, conn, monkeypatch, drafter, **HANDOFF)

    [cover] = _cover(digest)
    assert (cover.action, cover.reason) == ("ask", "cover letter not drafted (RuntimeError), attach by hand")
    assert [o.field.selector for o in digest.filled] == ["#resume"]
    assert digest.errors == []


def test_two_cover_letter_inputs_draft_one_letter_and_both_get_the_pdf(tmp_path, conn, monkeypatch):
    result = _ok(tmp_path)
    drafter = _Drafter(result)

    digest, page = _run(tmp_path, conn, monkeypatch, drafter, RESUME, COVER, COVER_TWO, **HANDOFF)

    assert len(drafter.calls) == 1
    assert [d.field.selector for d in digest.drafts] == ["#cover", "#cover2"]
    assert page.locators["#cover"].files == page.locators["#cover2"].files == [str(result.pdf_path)]


def test_a_form_without_a_cover_letter_file_input_never_drafts(tmp_path, conn, monkeypatch):
    drafter = _Drafter(_ok(tmp_path))

    digest, page = _run(tmp_path, conn, monkeypatch, drafter, RESUME, **HANDOFF)

    assert drafter.calls == []
    assert digest.drafts == []


def _digest_with_cover_and_essay(pdf: str):
    essay = FieldSpec(selector="#why", kind="textarea", label="Why us?")
    return SimpleNamespace(
        posting_id=1,
        drafts=[
            Resolution(COVER, "cover_letter", "upload", value=pdf, draft=True),
            Resolution(essay, None, "fill", value="Because of the work.", draft=True),
        ],
        filled=[
            FillOutcome(COVER, "cover_letter", pdf, "letter.pdf", True),
            FillOutcome(essay, None, "Because of the work.", "Because of the work.", True),
        ],
    )


def test_write_drafts_skips_the_upload_draft_and_typed_drafts_names_it(tmp_path):
    digest = _digest_with_cover_and_essay("/tmp/letter.pdf")

    path = submit.write_drafts(tmp_path, digest, "Acme")

    assert path == tmp_path / "runs" / date.today().isoformat() / "drafts.md"
    text = path.read_text()
    assert "Question: Why us?\nStatus: typed into the form" in text
    assert "Because of the work." in text
    assert "letter.pdf" not in text and "Cover Letter" not in text
    assert submit.typed_drafts(digest) == [
        ("Cover Letter", "/tmp/letter.pdf"),
        ("Why us?", "Because of the work."),
    ]
    assert submit.drafted_questions(digest) == ["Cover Letter", "Why us?"]
    assert submit._draft_note(submit.drafted_questions(digest)) == "2 AI-drafted: Cover Letter; Why us?"


def test_write_drafts_with_only_the_upload_draft_writes_nothing(tmp_path):
    digest = _digest_with_cover_and_essay("/tmp/letter.pdf")
    digest.drafts = digest.drafts[:1]

    assert submit.write_drafts(tmp_path, digest, "Acme") is None
    assert not (tmp_path / "runs").exists()


def test_the_letter_and_the_essays_share_one_drafts_file(tmp_path):
    """cover_letter logs under `<audit_root>/runs/<date>/drafts.md` and submit.write_drafts
    under the same path, so autoapply's single root puts both in one file."""
    pdf = tmp_path / "Alex_Rivera_Cover_Letter.pdf"
    letter = cover_letter.write_drafts_log(tmp_path, 1, "Acme", "Dear team, the letter.", pdf)
    essays = submit.write_drafts(tmp_path, _digest_with_cover_and_essay(str(pdf)), "Acme")

    assert letter == essays
    text = essays.read_text()
    assert text.count("Dear team, the letter.") == 1
    assert text.count("Because of the work.") == 1
    assert text.count(str(pdf)) == 0
