"""Confirmation-email scan: auto-mark applications submitted.

Reads Gmail (readonly, reusing the alerts OAuth token in
`data/gmail-token.json`) for ATS "thanks for applying" emails, matches
each one to an unsubmitted posting, and runs the same `confirm_applied`
flow the web UI uses. The email is ground truth that a submission
happened; an email that can't be matched to exactly one posting is
reported, never guessed. Safe to re-run: already-submitted postings and
already-seen message ids are skipped.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from jobhunt import lifecycle
from jobhunt.sources.alerts import (
    GMAIL_API_BASE,
    REQUEST_TIMEOUT_SECONDS,
    FetchError,
    _b64url_decode,
    _extract_html_part,
    _header,
    _parse_email_date,
    load_credentials,
)

# ATS notification domains that send application-received emails. The Gmail
# query casts wide (any mail from these domains in the window); the subject
# gate below narrows to confirmations, so interview invites and rejections
# from the same senders are ignored rather than mis-marked.
SENDER_DOMAINS = ("ashbyhq.com", "greenhouse.io", "greenhouse-mail.io", "lever.co")

_SUBJECT_RE = re.compile(r"appl(y|ying|ied|ication)|thank|received|interest", re.IGNORECASE)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

MAX_MESSAGES = 200
PAGE_SIZE = 100

# Marker prefix stored in applications.confirmation_ref — both provenance
# and the idempotency key for re-scans.
REF_PREFIX = "gmail:"


def _norm(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — so 'Founding
    Engineer - Full Stack' matches 'founding engineer full stack'."""
    return _WS_RE.sub(" ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def _body_text(payload: dict) -> str:
    """Plain text of the message body: the text/plain part if present,
    else the html part with tags stripped."""

    def _plain(part: dict) -> str | None:
        if part.get("mimeType") == "text/plain" and (part.get("body") or {}).get("data"):
            return _b64url_decode(part["body"]["data"])
        for sub in part.get("parts") or []:
            found = _plain(sub)
            if found:
                return found
        return None

    text = _plain(payload)
    if text is None:
        html = _extract_html_part(payload)
        text = _TAG_RE.sub(" ", html) if html else ""
    return text


def _from_ats_sender(from_header: str) -> bool:
    return any(domain in from_header.lower() for domain in SENDER_DOMAINS)


def candidate_postings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Postings a confirmation email could belong to: in the funnel at
    suggested/ready/applied with no recorded submission yet."""
    return conn.execute(
        """SELECT f.posting_id AS posting_id, p.company AS company, p.title AS title
           FROM funnel f
           JOIN postings p ON p.id = f.posting_id
           LEFT JOIN applications a ON a.posting_id = f.posting_id
           WHERE f.stage IN ('suggested', 'ready', 'applied')
             AND (a.id IS NULL OR a.submitted_at IS NULL)"""
    ).fetchall()


def match_message(
    subject: str, from_header: str, body_text: str, candidates: list[Any]
) -> tuple[Any | None, str]:
    """Match one confirmation email to exactly one candidate posting.

    Company must appear in the subject or From display name; when several
    of that company's postings are pending, the job title in the
    subject/body disambiguates. Returns (candidate, reason) — candidate is
    None with reason 'no_company_match' or 'ambiguous:<ids>' otherwise.
    """
    hay_company = f" {_norm(f'{subject} {from_header}')} "
    company_matches = [
        c for c in candidates if _norm(c["company"]) and f" {_norm(c['company'])} " in hay_company
    ]
    if not company_matches:
        return None, "no_company_match"
    if len(company_matches) == 1:
        return company_matches[0], "company"

    hay_full = _norm(f"{subject} {body_text}")
    titled = [c for c in company_matches if _norm(c["title"]) and _norm(c["title"]) in hay_full]
    if len(titled) == 1:
        return titled[0], "title"
    pool = titled or company_matches
    ids = ",".join(str(c["posting_id"]) for c in pool)
    return None, f"ambiguous:{ids}"


def _default_resume_variant(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT id FROM resume_variants WHERE reviewed_at IS NOT NULL ORDER BY id LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def _confirm(
    conn: sqlite3.Connection, posting_id: int, message_id: str, submitted_at: str | None
) -> tuple[str, list[str]]:
    """Run the standard confirm_applied flow for an email-verified
    submission. The only blocker resolved automatically is a missing
    resume-variant selection (the pre-fill run attached the default
    reviewed variant in reality); anything else is reported, not forced.
    """
    ref = f"{REF_PREFIX}{message_id}"
    readiness = lifecycle.application_readiness(conn, posting_id)
    if readiness["blockers"] == ["select_resume_variant"]:
        variant = _default_resume_variant(conn)
        if variant is not None:
            lifecycle.ensure_application(
                conn,
                posting_id,
                resume_variant_id=variant,
                confirmation_ref=ref,
                notes="auto: confirmation email scan",
            )
            readiness = lifecycle.application_readiness(conn, posting_id)
    if not readiness["ready"]:
        return "blocked", readiness["blockers"]
    lifecycle.confirm_applied(
        conn, posting_id, confirmed=True, submitted_at=submitted_at, confirmation_ref=ref
    )
    return "confirmed", []


def _fetch_messages(days: int) -> list[dict]:
    """Live Gmail fetch of full messages from the ATS sender domains."""
    from google.auth.transport.requests import AuthorizedSession

    creds = load_credentials()
    if creds is None:
        raise FetchError("confirm-scan: Gmail not configured — run `jobhunt gmail-auth`")
    session = AuthorizedSession(creds)

    query = "(" + " OR ".join(f"from:{d}" for d in SENDER_DOMAINS) + f") newer_than:{days}d"
    ids: list[str] = []
    page_token: str | None = None
    while len(ids) < MAX_MESSAGES:
        params: dict[str, Any] = {
            "q": query,
            "maxResults": min(PAGE_SIZE, MAX_MESSAGES - len(ids)),
        }
        if page_token:
            params["pageToken"] = page_token
        resp = session.get(
            f"{GMAIL_API_BASE}/messages", params=params, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if resp.status_code != 200:
            raise FetchError(f"confirm-scan: messages.list HTTP {resp.status_code}", resp.status_code)
        payload = resp.json()
        ids.extend(m["id"] for m in (payload.get("messages") or []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    messages = []
    for message_id in ids[:MAX_MESSAGES]:
        resp = session.get(
            f"{GMAIL_API_BASE}/messages/{message_id}",
            params={"format": "full"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            raise FetchError(
                f"confirm-scan: messages.get HTTP {resp.status_code} (id={message_id})",
                resp.status_code,
            )
        messages.append(resp.json())
    return messages


def run_scan(
    conn: sqlite3.Connection,
    days: int = 14,
    dry_run: bool = False,
    messages: list[dict] | None = None,
) -> dict:
    """Scan confirmation emails and mark matched postings submitted.

    `messages` injects pre-fetched Gmail message dicts (tests); when None,
    fetches live. Returns a summary dict; prints one line per outcome so
    the daily log shows what moved.
    """
    if messages is None:
        messages = _fetch_messages(days)

    seen_refs = {
        row["confirmation_ref"]
        for row in conn.execute(
            "SELECT confirmation_ref FROM applications WHERE confirmation_ref LIKE ?",
            (REF_PREFIX + "%",),
        ).fetchall()
    }
    candidates = list(candidate_postings(conn))

    summary: dict[str, Any] = {
        "confirmed": [],
        "blocked": [],
        "ambiguous": [],
        "unmatched": [],
        "skipped": 0,
    }

    for message in messages:
        message_id = message.get("id", "")
        if f"{REF_PREFIX}{message_id}" in seen_refs:
            summary["skipped"] += 1
            continue
        payload = message.get("payload") or {}
        headers = payload.get("headers") or []
        subject = _header(headers, "Subject") or ""
        from_header = _header(headers, "From") or ""
        if not _from_ats_sender(from_header) or not _SUBJECT_RE.search(subject):
            summary["skipped"] += 1
            continue

        body = _body_text(payload)
        candidate, reason = match_message(subject, from_header, body, candidates)
        if candidate is None:
            bucket = "ambiguous" if reason.startswith("ambiguous") else "unmatched"
            summary[bucket].append({"subject": subject, "reason": reason})
            print(f"confirm-scan: {bucket} — {subject!r} ({reason})")
            continue

        posting_id = candidate["posting_id"]
        if dry_run:
            summary["confirmed"].append({"posting_id": posting_id, "dry_run": True})
            print(f"confirm-scan: would confirm #{posting_id} — {subject!r}")
            candidates = [c for c in candidates if c["posting_id"] != posting_id]
            continue

        submitted_at = _parse_email_date(_header(headers, "Date"))
        outcome, blockers = _confirm(conn, posting_id, message_id, submitted_at)
        if outcome == "confirmed":
            summary["confirmed"].append({"posting_id": posting_id, "subject": subject})
            print(f"confirm-scan: confirmed #{posting_id} submitted — {subject!r}")
            candidates = [c for c in candidates if c["posting_id"] != posting_id]
        else:
            summary["blocked"].append({"posting_id": posting_id, "blockers": blockers})
            print(f"confirm-scan: #{posting_id} blocked ({', '.join(blockers)}) — confirm in UI")

    return summary
