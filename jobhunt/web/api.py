"""JSON API for the local job-search OS SPA and keyboard clients."""

from __future__ import annotations

import sqlite3
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from jobhunt import applysession, conference, funnel, health, lifecycle, prep, report, spend, sprint, today
from jobhunt.connectors import openrouter
from jobhunt.web import queries

OUTCOMES = {"replied", "screen", "rejected", "ghosted"}
OUTCOME_ELIGIBLE_STAGES = {"applied", "replied", "interviewing"}


class DecisionBody(BaseModel):
    decision: str
    tier: str | None = None
    reason: str | None = None
    detail: str = ""


class StageBody(BaseModel):
    stage: str
    note: str | None = None


class OutcomeBody(BaseModel):
    outcome: str
    note: str | None = None


class ApplicationBody(BaseModel):
    resume_variant_id: int | None = None
    channel: str = "company careers page"
    confirmation_ref: str = ""
    notes: str = ""


class AppliedBody(BaseModel):
    confirmed: bool
    submitted_at: str | None = None
    confirmation_ref: str = ""


class ResumeBody(BaseModel):
    label: str
    path: str
    purpose: str = ""
    reviewed: bool = False


class ContactBody(BaseModel):
    name: str
    company: str = ""
    title: str = ""
    relationship: str = ""
    source: str = ""
    notes: str = ""


class NoWarmPathBody(BaseModel):
    note: str


class TouchBody(BaseModel):
    kind: str
    contact_id: int | None = None
    posting_id: int | None = None
    due_at: str | None = None
    completed: bool = False
    notes: str = ""


class InterviewBody(BaseModel):
    posting_id: int
    round_name: str = "Interview"
    scheduled_at: str | None = None
    interviewers: str = ""
    prep_notes: str = ""
    outcome: str = ""
    thank_you_due_at: str | None = None


class InterviewUpdateBody(BaseModel):
    outcome: str | None = None
    thank_you_completed: bool = False


class OfferBody(BaseModel):
    posting_id: int
    received_at: str | None = None
    deadline: str | None = None
    base_salary: float | None = None
    bonus: float | None = None
    equity: str = ""
    location: str = ""
    notes: str = ""
    decision: str = ""


class ArtifactBody(BaseModel):
    kind: str


class ConferenceCompleteBody(BaseModel):
    final: bool = False


class ApplySessionBody(BaseModel):
    limit: int = 10
    min_score: int | None = None


def _jsonable(value):
    if isinstance(value, sqlite3.Row):
        return dict(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def create_api_router(get_conn: Callable) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/today")
    def api_today(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return today.build_today(conn)

    @router.get("/postings")
    def api_postings(
        min_score: int | None = None,
        source: str | None = None,
        status: str | None = None,
        in_funnel: str | None = None,
        q: str | None = None,
        page: int = 1,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        rubric_version = queries.latest_rubric_version(conn)
        rows, total = queries.postings_browse(
            conn,
            rubric_version,
            min_score=min_score,
            source=source or None,
            status=status or None,
            in_funnel={"y": True, "n": False}.get(in_funnel or ""),
            search=q or None,
            page=max(1, page),
        )
        return {"rows": rows, "total": total, "page": max(1, page)}

    @router.get("/search")
    def api_search(
        q: str = "", conn: sqlite3.Connection = Depends(get_conn)
    ) -> list[dict]:
        like = f"%{q.strip()}%"
        rubric_version = queries.latest_rubric_version(conn)
        jobs = [
            {
                "type": "posting",
                "id": row["id"],
                "primary": row["company"],
                "secondary": row["title"],
                "meta": row["total"],
            }
            for row in conn.execute(
                """SELECT p.id, p.company, p.title, s.total
                   FROM postings p LEFT JOIN scores s
                     ON s.posting_id=p.id AND s.rubric_version=?
                   WHERE p.company LIKE ? OR p.title LIKE ?
                   ORDER BY (s.total IS NULL), s.total DESC, p.id LIMIT 12""",
                (rubric_version, like, like),
            )
        ]
        people = [
            {
                "type": "contact",
                "id": row["id"],
                "primary": row["name"],
                "secondary": row["company"] or row["title"] or "Contact",
                "meta": "person",
            }
            for row in conn.execute(
                """SELECT id, name, company, title FROM contacts
                   WHERE name LIKE ? OR company LIKE ? OR title LIKE ?
                   ORDER BY updated_at DESC LIMIT 5""",
                (like, like, like),
            )
        ]
        actions = [
            {
                "type": "action",
                "id": row["id"],
                "primary": row["kind"].replace("_", " "),
                "secondary": row["notes"] or row["due_at"] or "Planned touch",
                "meta": row["status"],
            }
            for row in conn.execute(
                """SELECT id, kind, notes, due_at, status FROM touches
                   WHERE kind LIKE ? OR notes LIKE ?
                   ORDER BY updated_at DESC LIMIT 5""",
                (like, like),
            )
        ]
        return [*jobs, *people, *actions]

    @router.get("/posting/{posting_id}")
    def api_posting(posting_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        detail = queries.posting_detail(conn, posting_id)
        if detail is None:
            raise HTTPException(404, f"No posting with id={posting_id}")
        detail["readiness"] = lifecycle.application_readiness(conn, posting_id)
        detail["application"] = conn.execute(
            "SELECT * FROM applications WHERE posting_id=?", (posting_id,)
        ).fetchone()
        detail["resume_variants"] = conn.execute(
            "SELECT * FROM resume_variants ORDER BY label"
        ).fetchall()
        detail["artifacts"] = conn.execute(
            "SELECT * FROM artifacts WHERE posting_id=? ORDER BY created_at DESC",
            (posting_id,),
        ).fetchall()
        return _jsonable(detail)

    @router.post("/posting/{posting_id}/decision")
    def api_decision(
        posting_id: int, body: DecisionBody, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        try:
            if body.decision == "qualify":
                funnel.move(conn, posting_id, "qualified", note="triage accepted")
                if body.tier:
                    funnel.set_field(conn, posting_id, "tier", body.tier)
            elif body.decision == "pass":
                lifecycle.pass_posting(conn, posting_id, body.reason or "not_interested", body.detail)
            else:
                raise ValueError("decision must be 'qualify' or 'pass'")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _jsonable(queries.posting_detail(conn, posting_id))

    @router.post("/posting/{posting_id}/stage")
    def api_stage(
        posting_id: int, body: StageBody, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        try:
            if body.stage == "ready":
                lifecycle.mark_ready(conn, posting_id)
            elif body.stage == "applied":
                raise ValueError("use confirm-applied with explicit manual confirmation")
            else:
                funnel.move(conn, posting_id, body.stage, body.note)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return _jsonable(queries.posting_detail(conn, posting_id))

    @router.post("/posting/{posting_id}/outcome")
    def api_outcome(
        posting_id: int, body: OutcomeBody, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        if body.outcome not in OUTCOMES:
            raise HTTPException(400, f"outcome must be one of {', '.join(sorted(OUTCOMES))}")
        funnel_row = conn.execute(
            "SELECT stage FROM funnel WHERE posting_id=?", (posting_id,)
        ).fetchone()
        stage = funnel_row["stage"] if funnel_row else None
        if stage not in OUTCOME_ELIGIBLE_STAGES:
            raise HTTPException(
                400,
                f"posting must be applied/replied/interviewing to record an outcome "
                f"(current stage: {stage or 'none'})",
            )
        if body.outcome == "replied":
            funnel.move(conn, posting_id, "replied", note=body.note or "human reply received")
        elif body.outcome == "screen":
            lifecycle.create_interview(conn, posting_id, round_name="Recruiter screen")
        elif body.outcome == "rejected":
            note = "rejected" + (f" — {body.note}" if body.note else "")
            funnel.move(conn, posting_id, "closed", note=note)
        else:  # ghosted
            funnel.move(conn, posting_id, "closed", note="ghosted after no reply")
        return _jsonable(queries.posting_detail(conn, posting_id))

    @router.get("/posting/{posting_id}/readiness")
    def api_readiness(posting_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            return lifecycle.application_readiness(conn, posting_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/posting/{posting_id}/application")
    def api_application(
        posting_id: int, body: ApplicationBody, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        return dict(lifecycle.ensure_application(conn, posting_id, **body.model_dump()))

    @router.post("/posting/{posting_id}/confirm-applied")
    def api_confirm_applied(
        posting_id: int, body: AppliedBody, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        try:
            return dict(lifecycle.confirm_applied(conn, posting_id, **body.model_dump()))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/resume-variants")
    def api_resume(body: ResumeBody, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return dict(lifecycle.save_resume_variant(conn, **body.model_dump()))

    @router.get("/resume-variants")
    def api_resumes(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM resume_variants ORDER BY label")
        ]

    @router.get("/contacts")
    def api_contacts(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        return [dict(row) for row in conn.execute("SELECT * FROM contacts ORDER BY updated_at DESC")]

    @router.post("/contacts")
    def api_contact(body: ContactBody, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            return dict(lifecycle.create_contact(conn, **body.model_dump()))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/touches")
    def api_touches(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        return [dict(row) for row in conn.execute("SELECT * FROM touches ORDER BY updated_at DESC")]

    @router.post("/touches")
    def api_touch(body: TouchBody, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            return dict(lifecycle.create_touch(conn, **body.model_dump()))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/touches/{touch_id}/complete")
    def api_complete_touch(touch_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            return dict(lifecycle.complete_touch(conn, touch_id))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/sprint/members/{posting_id}/no-warm-path")
    def api_sprint_no_warm_path(
        posting_id: int, body: NoWarmPathBody, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        try:
            return dict(sprint.set_no_warm_path_note(conn, posting_id, body.note))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/conference_actions/{action_id}/complete")
    def api_complete_conference_action(
        action_id: int,
        body: ConferenceCompleteBody = ConferenceCompleteBody(),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        try:
            return conference.complete_conference_action(conn, action_id, final=body.final)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/interviews")
    def api_interviews(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        return [dict(row) for row in conn.execute("SELECT * FROM interviews ORDER BY scheduled_at")]

    @router.post("/interviews")
    def api_interview(body: InterviewBody, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return dict(lifecycle.create_interview(conn, **body.model_dump()))

    @router.post("/interviews/{interview_id}")
    def api_interview_update(
        interview_id: int,
        body: InterviewUpdateBody,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        try:
            return dict(lifecycle.update_interview(conn, interview_id, **body.model_dump()))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/offers")
    def api_offers(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        return [dict(row) for row in conn.execute("SELECT * FROM offers ORDER BY deadline")]

    @router.post("/offers")
    def api_offer(body: OfferBody, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return dict(lifecycle.create_offer(conn, **body.model_dump()))

    @router.post("/posting/{posting_id}/artifacts")
    def api_artifact(
        posting_id: int, body: ArtifactBody, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        try:
            return dict(lifecycle.create_artifact(conn, posting_id, body.kind))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/posting/{posting_id}/prep/{kind}")
    def api_prep_draft(
        posting_id: int,
        kind: str,
        outside_sprint: bool = False,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        if kind not in prep.KINDS:
            raise HTTPException(422, f"kind must be one of {prep.KINDS}")
        try:
            return dict(prep.generate_draft(conn, posting_id, kind, allow_non_sprint=outside_sprint))
        except prep.SprintScopeError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (openrouter.SpendGateError, spend.SpendCapError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/posting/{posting_id}/prep")
    def api_prep_packet(
        posting_id: int,
        outside_sprint: bool = False,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        try:
            return prep.generate_packet(conn, posting_id, allow_non_sprint=outside_sprint)
        except prep.SprintScopeError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (openrouter.SpendGateError, spend.SpendCapError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/report")
    def api_report(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return _jsonable(vars(report.build_report(conn)))

    @router.get("/health")
    def api_health(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return health.build_health(conn)

    @router.get("/apply-session")
    def api_apply_session_status() -> dict:
        return applysession.status()

    @router.post("/apply-session")
    def api_apply_session_start(body: ApplySessionBody) -> dict:
        try:
            return applysession.start(**body.model_dump())
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
