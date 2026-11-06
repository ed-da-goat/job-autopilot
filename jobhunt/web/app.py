"""Local FastAPI/HTMX frontend over the job-hunt pipeline DB.

Read-only pages (board, shortlist, postings, detail, report) render from
jobhunt.report / jobhunt.web.queries. All funnel writes go through
jobhunt.funnel's move()/set_field() — the web layer adds no new DB
semantics. 127.0.0.1-only, zero outbound network calls; see `make ui`.
"""

from __future__ import annotations

import html
import json
import os
import sqlite3
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobhunt import applypack as applypack_module
from jobhunt import db as db_module
from jobhunt import digest as digest_module
from jobhunt.eligibility import load_preferences
from jobhunt import funnel as funnel_module
from jobhunt import lifecycle as lifecycle_module
from jobhunt import prep as prep_module
from jobhunt import report as report_module
from jobhunt import spend as spend_module
from jobhunt import today as today_module
from jobhunt.connectors import openrouter as openrouter_module
from jobhunt.rubric import load_rubric
from jobhunt.scores_db import fetch_score_rows
from jobhunt.scoring import load_companies_by_name
from jobhunt.shortlist import _why_you, build_shortlist_rows
from jobhunt.web import queries
from jobhunt.web.api import create_api_router
from jobhunt.web.render import score_badge_style, score_bar_style

PREP_GATE_ERRORS = (
    openrouter_module.SpendGateError,
    spend_module.SpendCapError,
    prep_module.SprintScopeError,
)


def _gate_error_banner(exc: Exception) -> HTMLResponse:
    """A friendly inline banner for SpendGateError/SpendCapError — never a
    500. Rendered in place of the button that triggered the failed call."""
    return HTMLResponse(f'<p class="banner-error">{html.escape(str(exc))}</p>', status_code=200)

BASE_DIR = Path(__file__).resolve().parent
SPA_INDEX = BASE_DIR / "static" / "spa" / "index.html"
QUALIFY_TIERS = ("top20", "fast", "stretch")
SHORTLIST_TOP = 50
PAGE_SIZE = 50

app = FastAPI(title="job-hunt")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["score_badge_style"] = score_badge_style
templates.env.globals["score_bar_style"] = score_bar_style
templates.env.filters["from_json"] = json.loads

# Set by `make demo` (JOBHUNT_DEMO=1): marks every page as synthetic data.
DEMO_MODE = os.environ.get("JOBHUNT_DEMO") == "1"
DEMO_BADGE = (
    '<div style="position:fixed;top:0;right:0;z-index:99999;background:#f5b800;color:#111;'
    'font:700 12px/1 system-ui,sans-serif;padding:6px 12px;letter-spacing:.08em;'
    'border-bottom-left-radius:6px">DEMO DATA</div>'
)
templates.env.globals["demo_mode"] = DEMO_MODE


def get_db_path() -> Path:
    """Overridden in tests via app.dependency_overrides to point at a tmp_path DB."""
    return Path(os.environ.get("JOBHUNT_DB_PATH", db_module.DEFAULT_DB_PATH))


def get_conn(db_path: Path = Depends(get_db_path)):
    conn = db_module.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_digest_dir() -> Path:
    """Overridden in tests via app.dependency_overrides to point at a tmp_path digest dir."""
    return digest_module.DEFAULT_DIGEST_DIR


def _digest_dates(digest_dir: Path) -> list[str]:
    """Available digest dates (YYYY-MM-DD), newest first."""
    if not digest_dir.exists():
        return []
    return sorted((p.stem for p in digest_dir.glob("*.md")), reverse=True)


def _digest_response(
    request: Request, digest_dir: Path, date: str | None
) -> HTMLResponse:
    dates = _digest_dates(digest_dir)
    if date is None:
        date = dates[0] if dates else None
    elif date not in dates:
        raise HTTPException(status_code=404, detail=f"No digest for date={date}")
    content = (digest_dir / f"{date}.md").read_text(encoding="utf-8") if date else None
    return templates.TemplateResponse(
        request,
        "digest.html",
        {"dates": dates, "date": date, "content": content, "active": "digest"},
    )


@app.get("/pipeline", response_class=HTMLResponse)
@app.get("/legacy/pipeline", response_class=HTMLResponse)
def board(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> HTMLResponse:
    rubric_version = queries.latest_rubric_version(conn)
    columns = queries.board_columns(conn, rubric_version)
    suggested = queries.suggested_rows(conn, rubric_version)
    report_data = report_module.build_report(conn)
    return templates.TemplateResponse(
        request,
        "board.html",
        {
            "columns": columns,
            "stages": queries.BOARD_STAGES,
            "suggested": suggested,
            "report": report_data,
            "active": "board",
        },
    )


@app.get("/legacy", response_class=HTMLResponse)
def today_page(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> HTMLResponse:
    data = today_module.build_today(conn)
    report_data = report_module.build_report(conn)
    return templates.TemplateResponse(
        request,
        "today.html",
        {"data": data, "report": report_data, "pass_reasons": sorted(funnel_module.PASS_REASONS), "active": "today"},
    )


@app.get("/", response_class=FileResponse)
def spa_home() -> FileResponse:
    if not SPA_INDEX.exists():
        raise HTTPException(
            status_code=503,
            detail="React UI is not built. Run `make ui-build`, then reload.",
        )
    if DEMO_MODE:
        # `make demo` only: stamp a fixed badge into the built SPA without rebuilding it.
        return HTMLResponse(SPA_INDEX.read_text(encoding="utf-8").replace("</body>", DEMO_BADGE + "</body>", 1))
    return FileResponse(SPA_INDEX)


@app.get("/api/board")
def api_board(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """JSON twin of the board page: the same columns/suggested data the kanban
    template renders. First slice of the /api/* layer the SPA rebuild sits on
    (specs/brainstorms/2026-07-18-job-hunt-linear-grade-rebuild.md)."""
    rubric_version = queries.latest_rubric_version(conn)
    return {
        "rubric_version": rubric_version,
        "stages": queries.BOARD_STAGES,
        "columns": queries.board_columns(conn, rubric_version),
        "suggested": queries.suggested_rows(conn, rubric_version),
    }


@app.get("/shortlist", response_class=HTMLResponse)
def shortlist_page(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> HTMLResponse:
    rubric = load_rubric()
    rows = fetch_score_rows(
        conn,
        rubric["version"],
        exclude_blocked_preferences_version=load_preferences().version,
    )
    excluded_ids = queries.passed_or_closed_ids(conn)
    rows = [r for r in rows if r["posting_id"] not in excluded_ids]
    companies = load_companies_by_name(conn)
    entries = build_shortlist_rows(rows, SHORTLIST_TOP)
    funneled = queries.funneled_posting_ids(conn)

    enriched = []
    for entry in entries:
        row = entry["row"]
        company_row = companies.get(row["company"]) if row["company"] else None
        extra_roles = []
        if entry["extra_roles"] > 0:
            extra_roles = queries.company_extra_roles(
                conn,
                rubric["version"],
                row["company"],
                row["posting_id"],
                row["total"] or 0,
            )
        enriched.append(
            {
                "rank": entry["rank"],
                "row": row,
                "why": _why_you(row, company_row, rubric),
                "extra_count": entry["extra_roles"],
                "extra_roles": extra_roles,
                "qualified": row["posting_id"] in funneled,
            }
        )

    return templates.TemplateResponse(
        request,
        "shortlist.html",
        {"entries": enriched, "tiers": QUALIFY_TIERS, "active": "shortlist"},
    )


@app.get("/postings", response_class=HTMLResponse)
def postings_page(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    min_score: int | None = None,
    max_score: int | None = None,
    source: str | None = None,
    status: str | None = None,
    in_funnel: str | None = None,
    eligible: str | None = None,
    q: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    rubric_version = queries.latest_rubric_version(conn)
    in_funnel_bool = {"y": True, "n": False}.get(in_funnel or "")
    eligible_bool = True if eligible == "y" else None
    page = max(1, page)
    rows, total = queries.postings_browse(
        conn,
        rubric_version,
        min_score=min_score,
        max_score=max_score,
        source=source or None,
        status=status or None,
        in_funnel=in_funnel_bool,
        eligible=eligible_bool,
        search=q or None,
        page=page,
        page_size=PAGE_SIZE,
    )
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return templates.TemplateResponse(
        request,
        "postings.html",
        {
            "rows": rows,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "min_score": min_score,
            "max_score": max_score,
            "source": source or "",
            "status": status or "",
            "in_funnel": in_funnel or "",
            "eligible": eligible or "",
            "q": q or "",
            "active": "postings",
        },
    )


@app.get("/posting/{posting_id}", response_class=HTMLResponse)
def posting_detail(
    request: Request, posting_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> HTMLResponse:
    detail = queries.posting_detail(conn, posting_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"No posting with id={posting_id}")
    rubric = load_rubric()
    readiness = lifecycle_module.application_readiness(conn, posting_id)
    application = conn.execute(
        "SELECT * FROM applications WHERE posting_id=?", (posting_id,)
    ).fetchone()
    resumes = conn.execute("SELECT * FROM resume_variants ORDER BY label").fetchall()
    artifacts = conn.execute(
        "SELECT * FROM artifacts WHERE posting_id=? ORDER BY created_at DESC", (posting_id,)
    ).fetchall()
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            **detail,
            "components_max": rubric["components"],
            "stages": funnel_module.ALL_STAGES,
            "readiness": readiness,
            "application": application,
            "resumes": resumes,
            "artifacts": artifacts,
            "pass_reasons": sorted(funnel_module.PASS_REASONS),
            "active": "detail",
        },
    )


@app.get("/pack/{posting_id}", response_class=HTMLResponse)
def apply_pack(
    request: Request, posting_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> HTMLResponse:
    """Renders the Apply Pack in-memory — no file write. `pack <id>` (CLI)
    is the only thing that writes data/apply_packs/*.md."""
    try:
        posting_row, sections = applypack_module.build_pack_sections(conn, posting_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    fill_lines = [
        line for section in sections for line in applypack_module.fill_markers(section["content"])
    ]
    return templates.TemplateResponse(
        request,
        "pack.html",
        {
            "posting": posting_row,
            "sections": sections,
            "fill_lines": fill_lines,
            "active": "pack",
        },
    )


@app.get("/report", response_class=HTMLResponse)
def report_page(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> HTMLResponse:
    data = report_module.build_report(conn)
    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "data": data,
            "conversion_stages": report_module.CONVERSION_STAGES,
            "active": "report",
        },
    )


@app.get("/digest", response_class=HTMLResponse)
def digest_latest(
    request: Request, digest_dir: Path = Depends(get_digest_dir)
) -> HTMLResponse:
    return _digest_response(request, digest_dir, None)


@app.get("/digest/{date}", response_class=HTMLResponse)
def digest_by_date(
    request: Request, date: str, digest_dir: Path = Depends(get_digest_dir)
) -> HTMLResponse:
    return _digest_response(request, digest_dir, date)


@app.post("/qualify/{posting_id}")
def qualify(
    posting_id: int,
    tier: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    if tier is not None and tier not in QUALIFY_TIERS:
        raise HTTPException(
            status_code=400, detail=f"tier must be one of {QUALIFY_TIERS}"
        )
    funnel_module.move(conn, posting_id, "qualified")
    if tier is not None:
        funnel_module.set_field(conn, posting_id, "tier", tier)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/move/{posting_id}/{stage}")
def move(
    posting_id: int,
    stage: str,
    note: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    try:
        if stage == "applied":
            raise ValueError("manual submission confirmation is required; use Mark applied")
        if stage == "ready":
            lifecycle_module.mark_ready(conn, posting_id)
        else:
            funnel_module.move(conn, posting_id, stage, note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/pass/{posting_id}")
def pass_posting(
    posting_id: int,
    reason: str = Form(...),
    detail: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    try:
        lifecycle_module.pass_posting(conn, posting_id, reason, detail)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/application/{posting_id}")
def save_application(
    posting_id: int,
    resume_variant_id: int | None = Form(None),
    channel: str = Form("company careers page"),
    confirmation_ref: str = Form(""),
    notes: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    lifecycle_module.ensure_application(
        conn,
        posting_id,
        resume_variant_id=resume_variant_id,
        channel=channel,
        confirmation_ref=confirmation_ref,
        notes=notes,
    )
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/application/{posting_id}/ready")
def mark_application_ready(
    posting_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> Response:
    try:
        lifecycle_module.mark_ready(conn, posting_id)
    except ValueError as exc:
        return _gate_error_banner(exc)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/application/{posting_id}/applied")
def confirm_application_applied(
    posting_id: int,
    confirmed: str | None = Form(None),
    confirmation_ref: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    try:
        lifecycle_module.confirm_applied(
            conn,
            posting_id,
            confirmed=confirmed == "yes",
            confirmation_ref=confirmation_ref,
        )
    except ValueError as exc:
        return _gate_error_banner(exc)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/resume-variant")
def save_resume_variant(
    label: str = Form(...),
    path: str = Form(...),
    purpose: str = Form(""),
    reviewed: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    lifecycle_module.save_resume_variant(
        conn, label=label, path=path, purpose=purpose, reviewed=reviewed == "yes"
    )
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/artifact/{posting_id}")
def create_artifact(
    posting_id: int,
    kind: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    try:
        lifecycle_module.create_artifact(conn, posting_id, kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/set/{posting_id}/{field}")
def set_field(
    posting_id: int,
    field: str,
    value: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    try:
        funnel_module.set_field(conn, posting_id, field, value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/prep/{posting_id}/{kind}")
def prep_draft(
    posting_id: int,
    kind: str,
    model: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    if kind not in prep_module.KINDS:
        raise HTTPException(
            status_code=400, detail=f"kind must be one of {prep_module.KINDS}"
        )
    try:
        prep_module.generate_draft(conn, posting_id, kind, model=model or None)
    except PREP_GATE_ERRORS as exc:
        return _gate_error_banner(exc)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/prep/{posting_id}")
def prep_packet(
    posting_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> Response:
    try:
        prep_module.generate_packet(conn, posting_id)
    except PREP_GATE_ERRORS as exc:
        return _gate_error_banner(exc)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/draft/{draft_id}/save")
def save_draft(
    draft_id: int,
    content: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    row = conn.execute("SELECT id FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No draft with id={draft_id}")
    conn.execute("UPDATE drafts SET content=?, edited=1 WHERE id=?", (content, draft_id))
    conn.commit()
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/draft/{draft_id}/regenerate")
def regenerate_draft(
    draft_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> Response:
    row = conn.execute(
        "SELECT posting_id, kind FROM drafts WHERE id=?", (draft_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No draft with id={draft_id}")
    try:
        prep_module.generate_draft(conn, row["posting_id"], row["kind"])
    except PREP_GATE_ERRORS as exc:
        return _gate_error_banner(exc)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.get("/people", response_class=HTMLResponse)
def people_page(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> HTMLResponse:
    contacts = conn.execute("SELECT * FROM contacts ORDER BY updated_at DESC").fetchall()
    touches = conn.execute(
        """SELECT t.*, c.name contact_name, p.company posting_company, p.title posting_title
           FROM touches t LEFT JOIN contacts c ON c.id=t.contact_id
           LEFT JOIN postings p ON p.id=t.posting_id ORDER BY t.updated_at DESC"""
    ).fetchall()
    return templates.TemplateResponse(
        request,
        "people.html",
        {"contacts": contacts, "touches": touches, "active": "people"},
    )


@app.post("/contact")
def add_contact(
    name: str = Form(...),
    company: str = Form(""),
    title: str = Form(""),
    relationship: str = Form(""),
    source: str = Form(""),
    notes: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    lifecycle_module.create_contact(
        conn,
        name=name,
        company=company,
        title=title,
        relationship=relationship,
        source=source,
        notes=notes,
    )
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/touch")
def add_touch(
    kind: str = Form(...),
    contact_id: int | None = Form(None),
    posting_id: int | None = Form(None),
    due_at: str | None = Form(None),
    completed: str | None = Form(None),
    notes: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    lifecycle_module.create_touch(
        conn,
        kind=kind,
        contact_id=contact_id,
        posting_id=posting_id,
        due_at=due_at or None,
        completed=completed == "yes",
        notes=notes,
    )
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/touch/{touch_id}/complete")
def finish_touch(
    touch_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> Response:
    lifecycle_module.complete_touch(conn, touch_id)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.get("/interviews", response_class=HTMLResponse)
def interviews_page(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> HTMLResponse:
    interviews = conn.execute(
        """SELECT i.*, p.company, p.title FROM interviews i
           JOIN postings p ON p.id=i.posting_id ORDER BY i.scheduled_at"""
    ).fetchall()
    offers = conn.execute(
        """SELECT o.*, p.company, p.title FROM offers o
           JOIN postings p ON p.id=o.posting_id ORDER BY o.deadline"""
    ).fetchall()
    return templates.TemplateResponse(
        request,
        "interviews.html",
        {"interviews": interviews, "offers": offers, "active": "interviews"},
    )


@app.post("/interview")
def add_interview(
    posting_id: int = Form(...),
    round_name: str = Form("Interview"),
    scheduled_at: str | None = Form(None),
    interviewers: str = Form(""),
    prep_notes: str = Form(""),
    thank_you_due_at: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    lifecycle_module.create_interview(
        conn,
        posting_id,
        round_name=round_name,
        scheduled_at=scheduled_at or None,
        interviewers=interviewers,
        prep_notes=prep_notes,
        thank_you_due_at=thank_you_due_at or None,
    )
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/interview/{interview_id}/update")
def update_interview(
    interview_id: int,
    outcome: str | None = Form(None),
    thank_you_completed: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    lifecycle_module.update_interview(
        conn,
        interview_id,
        outcome=outcome,
        thank_you_completed=thank_you_completed == "yes",
    )
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@app.post("/offer")
def add_offer(
    posting_id: int = Form(...),
    deadline: str | None = Form(None),
    base_salary: float | None = Form(None),
    bonus: float | None = Form(None),
    equity: str = Form(""),
    location: str = Form(""),
    notes: str = Form(""),
    decision: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    lifecycle_module.create_offer(
        conn,
        posting_id,
        deadline=deadline or None,
        base_salary=base_salary,
        bonus=bonus,
        equity=equity,
        location=location,
        notes=notes,
        decision=decision,
    )
    return Response(status_code=200, headers={"HX-Refresh": "true"})


app.include_router(create_api_router(get_conn))
