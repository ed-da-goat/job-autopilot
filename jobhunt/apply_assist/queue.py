"""Batch pre-fill queue: select the top of the funnel and run apply-assist
across all of them in one browser session, one tab per posting.

Selection (`select_queue`) and rendering (`render_queue_summary`) are pure
Python — no browser. Execution (`run_queue`) is the one function here that
touches Playwright, and it does so via a lazy import so that importing this
module alone never pulls Playwright in (matches the `apply` command's lazy
import of driver.py/fill.py in cli.py).

HARD SAFETY RULE (inherited from fill.py): nothing here ever clicks
submit — run_queue only drives fill_module.run_assist per tab.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from jobhunt.apply_assist.detect import application_url, detect_ats
from jobhunt.apply_assist.mapper import MapperResult
from jobhunt.apply_assist.schema import TriageDigest
from jobhunt.apply_assist.tailor_answers import TailorResult
from jobhunt.eligibility import load_preferences
from jobhunt.spend import RunBudget

RECENT_FILL_DAYS = 7
_GOTO_TIMEOUT_MS = 45_000
_FOOTER = "Review each tab — YOU click submit. This tool never will."


def select_queue(
    conn: sqlite3.Connection,
    limit: int,
    min_score: int | None = None,
    posting_id: int | list[int] | None = None,
) -> list[sqlite3.Row]:
    """Postings ready for a batch pre-fill pass: 'suggested' funnel rows, or
    'ready' rows, active and eligible under current preferences, excluding
    submitted applications. Newest posting day first (first-seen day when
    undated), then latest-rubric score desc (unscored last). Postings that
    already have a tailored resume PDF (an artifacts row of kind 'resume_pdf')
    sort ahead of everything else so paid tailoring is used before it goes stale.

    Excludes postings opened or pre-filled in the last RECENT_FILL_DAYS days (no
    duplicate tab-opens) and any posting whose URL isn't a known ATS —
    apply-assist can't do a useful unattended scan on a generic careers
    page. `limit` applies to the final filtered set, not the SQL rows,
    since the ATS check can't be expressed in SQL.

    If `posting_id` is given (one id or a list), the cooldown exclusion is
    bypassed for those postings (an attended re-run) but every other guard —
    active, eligible, funnel stage, unsubmitted, known ATS — still applies. All other postings
    are excluded, and `limit` does not truncate an explicitly requested set.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=RECENT_FILL_DAYS)).isoformat()

    posting_ids = (
        [posting_id] if isinstance(posting_id, int) else list(posting_id or [])
    )
    # bypass placeholders: the cooldown OR-clause takes `f.posting_id IN (...)`.
    # With no posting_id override the list is empty and "IN (NULL)" matches
    # nothing, leaving the cooldown exclusion fully in effect.
    bypass_sql = ",".join("?" for _ in posting_ids) or "NULL"

    score_filter_sql = ""
    posting_filter_sql = ""
    params: list[Any] = [load_preferences().version, *posting_ids, cutoff]
    if min_score is not None:
        score_filter_sql = "AND s.total >= ?"
        params.append(min_score)
    if posting_ids:
        posting_filter_sql = f"AND f.posting_id IN ({bypass_sql})"
        params.extend(posting_ids)

    rows = conn.execute(
        f"""
        SELECT f.posting_id AS posting_id, p.url AS url, p.company AS company,
               p.title AS title, s.total AS score
        FROM funnel f
        JOIN postings p ON p.id = f.posting_id
        LEFT JOIN scores s
          ON s.posting_id = f.posting_id
         AND s.rubric_version = (
               SELECT MAX(rubric_version) FROM scores WHERE posting_id = f.posting_id
             )
        JOIN eligibility_assessments e
          ON e.id = (
               SELECT id FROM eligibility_assessments
               WHERE posting_id=p.id AND preferences_version=?
               ORDER BY rubric_version DESC LIMIT 1
             )
        WHERE f.stage IN ('suggested', 'ready')
          AND p.status = 'active'
          AND e.status = 'eligible'
          AND (s.rubric_version IS NULL OR e.rubric_version = s.rubric_version)
          AND NOT EXISTS (
                SELECT 1 FROM applications a
                WHERE a.posting_id=p.id AND a.submitted_at IS NOT NULL
              )
          AND (
                f.posting_id IN ({bypass_sql})
                OR f.posting_id NOT IN (
                     SELECT entity_id FROM activity_events
                     WHERE entity_type = 'posting'
                       AND action IN ('apply_assist_fill', 'apply_lite_open') AND at >= ?
                   )
              )
          {score_filter_sql}
          {posting_filter_sql}
        ORDER BY EXISTS (
                     SELECT 1 FROM artifacts ar
                     WHERE ar.posting_id = f.posting_id AND ar.kind = 'resume_pdf'
                 ) DESC,
                 COALESCE(date(p.posted_at), date(p.first_seen_at)) DESC,
                 COALESCE(s.total, -1) DESC, f.posting_id
        """,
        params,
    ).fetchall()

    selected = [row for row in rows if detect_ats(row["url"]) is not None]
    if posting_ids:
        # An explicit id list is the whole ask — don't let the default
        # limit silently drop tabs from an attended multi-posting re-run.
        return selected
    return selected[:limit]


def sweep_blank_tabs(context: Any) -> int:
    """Close every attached about:blank tab. Returns how many closed.
    Popup/restore junk shows as about:blank; posting tabs never do
    (a failed goto leaves about:blank too — closing it is fine, the
    tab shows nothing reviewable)."""
    closed = 0
    for p in list(context.pages):
        if p.url == "about:blank":
            try:
                p.close()
                closed += 1
            except Exception:  # noqa: BLE001 — sweep must never kill the wait
                pass
    return closed


def run_queue(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    answers: dict[str, str],
    resume_path: str | None,
    scan_only: bool = False,
    budget: RunBudget | None = None,
    tailor: bool = False,
) -> tuple[
    list[TriageDigest], list[Any], Any, Any, bool, list[MapperResult], list[TailorResult]
]:
    """Open one browser, one tab per posting, and run apply-assist against
    each. A per-posting failure becomes an error digest and the queue keeps
    going — one bad ATS page must not kill the batch.

    `resume_path` is the batch-wide standing resume, used only where a posting
    has no tailored render of its own; each tab resolves its own attachment.

    `budget`, if given, is threaded into every posting's apply-assist run so
    the LLM field-mapper (jobhunt.apply_assist.mapper) can name a semantic
    key for fields classify() misses, all postings in the batch sharing one
    RunBudget ceiling. With `budget=None` the mapper never makes a network
    call (mapper.map_unknown short-circuits to status="no_budget").

    `tailor`, if True, additionally hands eligible textarea fields to
    tailor_answers.draft_for_posting per posting (see fill.run_assist).
    Default False — tailoring never runs unless explicitly requested.

    Returns (digests, pages, handle, context, is_cdp, mapper_results,
    tailor_results): the caller (the CLI) owns shutdown, since the queue is
    meant to leave many tabs open for review. `pages` is aligned 1:1 with
    `digests` — the live Page for a posting that opened successfully, or
    None where the goto/run_assist raised. `mapper_results` has one entry
    per posting where the mapper actually ran (empty list if every posting
    was fully classified already). `tailor_results` likewise has one entry
    per posting where the drafter actually ran (empty list when
    `tailor=False` or no posting had eligible fields).
    """
    from jobhunt import lifecycle as lifecycle_module
    from jobhunt import applypack as applypack_module
    from jobhunt.apply_assist import driver as driver_module
    from jobhunt.apply_assist import fill as fill_module

    handle, context, is_cdp = driver_module.open_browser()

    digests: list[TriageDigest] = []
    pages: list[Any] = []
    mapper_results: list[MapperResult] = []
    tailor_results: list[TailorResult] = []
    first_page = None
    for row in rows:
        posting_id = row["posting_id"]
        url = row["url"]
        page = None
        try:
            page = context.new_page()
            # application_url, not url — the batch path opens its own tabs and
            # does not go through driver.open_page, so it needs the same
            # description-page → form-page rewrite.
            page.goto(
                application_url(url), wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS
            )
            # Per-posting tailored render when `jobhunt resume tailor` has made
            # one, otherwise the batch-wide standing resume the caller resolved.
            posting_resume = applypack_module.resume_path_for(conn, posting_id, resume_path)
            triage = fill_module.run_assist(
                page,
                posting_id,
                url,
                answers,
                posting_resume,
                scan_only=scan_only,
                conn=conn,
                budget=budget,
                mapper_sink=mapper_results,
                tailor=tailor,
                tailor_sink=tailor_results,
            )
            if first_page is None:
                first_page = page
        except Exception as exc:  # noqa: BLE001 — one bad tab must not kill the batch
            page = None
            triage = TriageDigest(
                posting_id=posting_id,
                url=url,
                ats=detect_ats(url),
                filled=[],
                residual=[],
                skipped=[],
                errors=[str(exc)],
            )

        action = "apply_assist_scan" if scan_only else "apply_assist_fill"
        lifecycle_module.log_activity(
            conn, "posting", posting_id, action, data=triage.to_dict()
        )
        conn.commit()
        digests.append(triage)
        pages.append(page)

    # Leave the reviewer on a posting tab, not the persistent context's
    # about:blank launcher tab. Never on CDP (that is the candidate's own
    # browser), and never when no posting tab opened (closing the last
    # page would end the persistent context).
    if not is_cdp and first_page is not None:
        for p in list(context.pages):
            if p is not first_page and p.url == "about:blank":
                try:
                    p.close()
                except Exception:  # noqa: BLE001 — focus cleanup must never kill the batch
                    pass
        try:
            first_page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass

    return digests, pages, handle, context, is_cdp, mapper_results, tailor_results


_MAPPER_LOUD_STATUSES = {"gate_closed", "over_cap", "error"}
_TAILOR_LOUD_STATUSES = {"gate_closed", "over_cap", "error"}


def render_queue_summary(
    digests: list[TriageDigest],
    mapper_results: list[MapperResult] | None = None,
    tailor_results: list[TailorResult] | None = None,
) -> str:
    """One block per posting (filled/needs-you counts + first 3 residual
    field labels), then a mapper block (mapped/cached counts + this run's
    cost, plus a loud line per posting where the mapper couldn't run —
    gate_closed/over_cap/error must never go silently unreported), then a
    totals line, then the standing footer."""
    lines: list[str] = []
    total_filled = 0
    total_residual = 0

    for d in digests:
        needs_you = len(d.residual)
        total_filled += len(d.filled)
        total_residual += needs_you
        labels = [
            r.field.label or r.field.name or r.field.selector for r in d.residual[:3]
        ]
        summary_line = (
            f"#{d.posting_id}  ats={d.ats or 'unknown'}  "
            f"filled={len(d.filled)}  needs-you={needs_you}"
        )
        if d.drafts:
            summary_line += f"  ai-drafts:{len(d.drafts)} (not typed)"
        lines.append(summary_line)
        lines.append(d.url)
        if labels:
            lines.append("  needs: " + "; ".join(labels))
        if d.errors:
            lines.append("  ERROR: " + "; ".join(d.errors))
        lines.append("")

    lines.append(
        f"TOTAL: {len(digests)} posting(s), {total_filled} field(s) filled, "
        f"{total_residual} needing you"
    )

    if mapper_results:
        total_mapped = sum(mr.mapped for mr in mapper_results)
        total_cache_hits = sum(mr.cache_hits for mr in mapper_results)
        total_cost = sum(mr.cost_usd for mr in mapper_results)
        lines.append(
            f"MAPPER: {total_mapped} field(s) mapped, {total_cache_hits} cache "
            f"hit(s), ${total_cost:.4f} this run"
        )
        for mr in mapper_results:
            if mr.status in _MAPPER_LOUD_STATUSES:
                lines.append(f"MAPPER ALERT: status={mr.status} — {mr.detail}")

    if tailor_results:
        total_drafted = sum(len(tr.drafts) for tr in tailor_results)
        total_tailor_cost = sum(tr.cost_usd for tr in tailor_results)
        lines.append(
            f"DRAFTED (review before submit): {total_drafted} field(s), "
            f"${total_tailor_cost:.4f} this run"
        )
        for tr in tailor_results:
            for _fp, (question, answer) in tr.drafts.items():
                preview = answer[:60]
                lines.append(f"  #{tr.posting_id}  {question[:60]!r}: {preview}")
            if tr.status in _TAILOR_LOUD_STATUSES:
                lines.append(f"TAILOR ALERT: status={tr.status} — {tr.detail}")

    lines.append("")
    lines.append(_FOOTER)
    return "\n".join(lines)
