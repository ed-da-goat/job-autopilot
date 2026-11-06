"""Live submission detection: watch open apply tabs for an ATS success page.

Detects that the candidate already submitted an application by hand — a
Greenhouse/Lever/Ashby confirmation page is visible in the tab. This module
never clicks, never submits, never fills a form; it only reads `page.url`
and `document.body.innerText` and, on a match, records the submission via
the standard `confirm_applied` flow. The Gmail confirmation-email scan
(`jobhunt.confirmations`) remains the ground-truth backstop for anything
this misses.

The armed autoapply lane (`apply_assist/submit.py`) reuses `page_submitted`
to confirm its own click and `record_submission` (with its own ref/note) to
record it, and `record_blockers` to prove before clicking that the DB write
afterwards cannot be refused.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from jobhunt import lifecycle
from jobhunt.apply_assist.detect import detect_ats

# Greenhouse and Lever redirect to a distinct confirmation URL on submit.
# Ashby is a single-page app — the URL rarely changes, so it relies on text
# matching only.
_SUCCESS_URL_SUBSTRINGS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("/confirmation",),
    "lever": ("/thanks",),
    "ashby": (),
}

_SUCCESS_TEXT_RES: dict[str, tuple[re.Pattern, ...]] = {
    "greenhouse": (
        re.compile(r"thank you for applying", re.IGNORECASE),
        re.compile(r"application (has been )?(submitted|received)", re.IGNORECASE),
    ),
    "lever": (
        re.compile(r"application submitted", re.IGNORECASE),
        re.compile(r"thank you for (applying|your (application|interest))", re.IGNORECASE),
    ),
    "ashby": (
        # ASSUMED copy: Ashby's confirmation reads "...successfully submitted".
        re.compile(r"application (was )?(successfully )?submitted", re.IGNORECASE),
        re.compile(r"thank you for applying", re.IGNORECASE),
        re.compile(r"we('| ha)ve received your application", re.IGNORECASE),
    ),
}

# Default `applications.notes` when the watch has to link a resume variant itself.
_LIVE_WATCH_NOTE = "auto: live success-page watch"


def page_submitted(page: Any) -> bool:
    """True when `page` is currently showing an ATS success/confirmation
    page. Read-only (URL + innerText); never raises — a closed or
    navigating tab just reports False."""
    try:
        url = page.url
        ats = detect_ats(url)
        if ats is None:
            return False
        if any(substr in url for substr in _SUCCESS_URL_SUBSTRINGS.get(ats, ())):
            return True
        text = page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 8000)"
        )
        return any(pattern.search(text) for pattern in _SUCCESS_TEXT_RES.get(ats, ()))
    except Exception:  # noqa: BLE001 — a closed/navigating tab must never raise
        return False


def _default_resume_variant(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT id FROM resume_variants WHERE reviewed_at IS NOT NULL ORDER BY id LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def record_blockers(conn: sqlite3.Connection, posting_id: int) -> list[str]:
    """Read-only preview of `record_submission`: the readiness blockers that
    would still stand after its single auto-resolve (a sole
    `select_resume_variant` cleared by linking the default reviewed variant).
    Empty means the DB write that follows a real submit cannot be refused.
    Writes nothing; raises ValueError for an unknown posting, like
    `lifecycle.application_readiness`."""
    blockers = list(lifecycle.application_readiness(conn, posting_id)["blockers"])
    if blockers == ["select_resume_variant"] and _default_resume_variant(conn) is not None:
        return []
    return blockers


def record_submission(
    conn: sqlite3.Connection,
    posting_id: int,
    *,
    ref: str | None = None,
    note: str | None = None,
) -> tuple[str, list[str]]:
    """Mirror `confirmations._confirm` for a live success-page detection:
    auto-resolve only a missing resume-variant selection, then run the
    standard `confirm_applied` flow. Returns ("confirmed", []) or
    ("blocked", blockers).

    `ref` / `note` default to the live-watch values (`livewatch:<id>` and
    "auto: live success-page watch"); the autoapply lane passes its own so the
    audit trail says which lane recorded the submission. A custom `note` is
    also logged on the `submit_detected` event, because `confirm_applied`
    rewrites `applications.notes`.

    Idempotent: a posting already recorded submitted is reported confirmed
    without re-running the flow, so a `close()` failure that leaves a tab
    open for a later poll can't double-record it.
    """
    existing = conn.execute(
        "SELECT submitted_at FROM applications WHERE posting_id=?", (posting_id,)
    ).fetchone()
    if existing is not None and existing["submitted_at"] is not None:
        return "confirmed", []

    ref = ref if ref is not None else f"livewatch:{posting_id}"
    detected: dict[str, str] = {"ref": ref}
    if note is not None:
        detected["note"] = note
    lifecycle.log_activity(conn, "posting", posting_id, "submit_detected", detected)
    conn.commit()

    readiness = lifecycle.application_readiness(conn, posting_id)
    if readiness["blockers"] == ["select_resume_variant"]:
        variant = _default_resume_variant(conn)
        if variant is not None:
            lifecycle.ensure_application(
                conn,
                posting_id,
                resume_variant_id=variant,
                confirmation_ref=ref,
                notes=note if note is not None else _LIVE_WATCH_NOTE,
            )
            readiness = lifecycle.application_readiness(conn, posting_id)
    if not readiness["ready"]:
        return "blocked", readiness["blockers"]
    lifecycle.confirm_applied(conn, posting_id, confirmed=True, confirmation_ref=ref)
    return "confirmed", []


def sweep_submitted(
    conn: sqlite3.Connection, tab_map: list[tuple[int, Any]]
) -> tuple[list[int], list[tuple[int, list[str]]]]:
    """Check every (posting_id, page) pair for a success page; record and
    close the tab on a match. Callers pass the same `tab_map` every poll —
    a posting already confirmed will have a closed tab (skipped below), so
    this never double-records.
    """
    confirmed: list[int] = []
    blocked: list[tuple[int, list[str]]] = []
    for posting_id, page in tab_map:
        try:
            if page.is_closed():
                continue
        except Exception:  # noqa: BLE001 — a torn-down page must never raise
            continue
        if not page_submitted(page):
            continue
        status, blockers = record_submission(conn, posting_id)
        try:
            page.close()
        except Exception:  # noqa: BLE001 — recording already happened; a close failure is cosmetic
            pass
        if status == "confirmed":
            confirmed.append(posting_id)
        else:
            blocked.append((posting_id, blockers))
    return confirmed, blocked
