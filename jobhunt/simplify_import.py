"""Import Simplify's Job Tracker "Export CSV" into the funnel.

Dry-run by default: `plan_import` only reads the DB. `apply_plan` is the single
writer. Matching is URL first, then company+title against active postings;
statuses only ever move a posting forward (never regress a funnel stage).

Pure helpers (header resolution, URL normalization, status mapping, date
parsing) carry no DB access so they are easy to test in isolation.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from jobhunt import funnel, lifecycle

DEFAULT_EXPORT_PATH = Path("data/simplify/export.csv")
MISSING_FILE_HELP = (
    "Export file not found. In Simplify: Job Tracker -> Export CSV, "
    "then save it to data/simplify/export.csv."
)
STUB_SOURCE = "simplify_import"
IMPORT_NOTE = "simplify import"
DATE_FROM_IMPORT_NOTE = "date from import"

# Canonical field -> accepted normalized header names, in priority order.
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "company": ("company name", "company", "employer", "organization"),
    "title": ("position title", "title", "job title", "position", "role"),
    "url": ("job posting link", "url", "link", "job url", "job link", "posting url", "posting link"),
    "status": ("status", "application status"),
    "date": ("applied date", "date applied", "created at", "date", "applied on", "application date"),
    "location": ("location",),
}
REQUIRED_FIELDS = ("company", "title", "status")

STATUS_MAP: dict[str, str] = {
    "applied": "applied",
    "interviewing": "interviewing",
    "interview": "interviewing",
    "offer": "offer",
    "offered": "offer",
    "rejected": "closed",
    "rejection": "closed",
    "saved": "ready",
}

# Stage rank for the no-regress rule. Side stages: suggested/passed sit below
# the pipeline (a real application reopens them); closed is terminal.
_RANK: dict[str, int] = {stage: i for i, stage in enumerate(funnel.STAGES)}
_RANK.update({"suggested": -1, "passed": -1, "closed": 100})
_APPLIED_RANK = _RANK["applied"]


class HeaderError(ValueError):
    """Raised when required CSV columns cannot be resolved."""


@dataclass(frozen=True)
class ImportRow:
    line: int
    company: str
    title: str
    url: str
    status: str
    applied_date: str | None  # ISO string, or None when absent/unparseable
    location: str = ""


@dataclass(frozen=True)
class PlanItem:
    row: ImportRow
    posting_id: int | None
    method: str  # 'url' | 'company+title' | 'unmatched'
    current: str  # funnel stage, '(none)' for no funnel row, '(new)' for a stub
    proposed: str | None
    action: str  # see ACTIONS
    reason: str = ""


ACTIONS = (
    "move",  # advance funnel stage (and record application if applied+)
    "record_application",  # stage already fine; application row is missing
    "create_stub_and_move",  # unmatched -> stub posting, then move
    "skip_further",  # already further along (no regress)
    "skip_status",  # status not mappable -> report only
    "saved_noop",  # Saved without --saved-to-ready: reported, never moved or stubbed
    "skip_invalid",  # row missing company/title
)


def _norm_header(name: str) -> str:
    return re.sub(r"[\s_\-]+", " ", (name or "").strip().lower())


def resolve_headers(fieldnames: list[str] | None) -> dict[str, str]:
    """Map canonical field -> actual CSV header. Fails closed (HeaderError,
    listing the actual headers) if company, title, or status is unresolved."""
    actual = list(fieldnames or [])
    by_norm: dict[str, str] = {}
    for header in actual:
        by_norm.setdefault(_norm_header(header), header)

    resolved: dict[str, str] = {}
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in by_norm:
                resolved[field] = by_norm[alias]
                break

    missing = [f for f in REQUIRED_FIELDS if f not in resolved]
    if missing:
        raise HeaderError(
            f"cannot resolve required column(s): {', '.join(missing)}. "
            f"Actual headers: {actual!r}"
        )
    return resolved


def normalize_url(url: str | None) -> str:
    """Lowercase host, drop scheme/query/utm/fragment/trailing slash.
    Returns '' for empty or unparseable input."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "//" + raw
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
    except ValueError:
        return ""
    if not host:
        return ""
    path = parts.path.rstrip("/")
    return f"{host}{path}"


def map_status(status: str | None) -> str | None:
    return STATUS_MAP.get(re.sub(r"\s+", " ", (status or "").strip().lower()))


def parse_date(value: str | None) -> str | None:
    """Parse a date/datetime cell to an ISO-8601 UTC string; None if blank or unparseable."""
    text = (value or "").strip()
    if not text:
        return None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m/%d/%Y %H:%M:%S", "%b %d, %Y", "%B %d, %Y", "%d %b %Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def read_rows(path: Path) -> list[ImportRow]:
    """Parse the export CSV. Raises FileNotFoundError / HeaderError."""
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = resolve_headers(reader.fieldnames)
        rows: list[ImportRow] = []
        for record in reader:
            def cell(field: str) -> str:
                header = cols.get(field)
                return (record.get(header) or "").strip() if header else ""

            rows.append(
                ImportRow(
                    line=reader.line_num,
                    company=cell("company"),
                    title=cell("title"),
                    url=cell("url"),
                    status=cell("status"),
                    applied_date=parse_date(cell("date")),
                    location=cell("location"),
                )
            )
    return rows


def _rank(stage: str) -> int:
    return _RANK.get(stage, 0)


def _match(
    row: ImportRow,
    url_index: dict[str, int],
    ct_index: dict[tuple[str, str], int],
) -> tuple[int | None, str]:
    norm = normalize_url(row.url)
    if norm and norm in url_index:
        return url_index[norm], "url"
    key = (row.company.casefold(), row.title.casefold())
    if key in ct_index:
        return ct_index[key], "company+title"
    return None, "unmatched"


def plan_import(
    conn: sqlite3.Connection, rows: list[ImportRow], saved_to_ready: bool = False
) -> list[PlanItem]:
    """Read-only: decide one action per row, simulating earlier rows' moves so a
    file containing the same posting twice stays consistent."""
    url_index: dict[str, int] = {}
    ct_index: dict[tuple[str, str], int] = {}
    for p in conn.execute("SELECT id, company, title, url, status FROM postings ORDER BY id DESC"):
        norm = normalize_url(p["url"])
        if norm:
            url_index[norm] = p["id"]  # DESC order -> lowest id wins
        if p["status"] == "active" and p["company"] and p["title"]:
            ct_index[(p["company"].strip().casefold(), p["title"].strip().casefold())] = p["id"]

    stages: dict[int, str] = {
        r["posting_id"]: r["stage"] for r in conn.execute("SELECT posting_id, stage FROM funnel")
    }
    has_app: set[int] = {r["posting_id"] for r in conn.execute("SELECT posting_id FROM applications")}
    stub_stage: dict[str, str] = {}  # simulated stubs keyed by url/company+title
    plan: list[PlanItem] = []

    for row in rows:
        if not row.company or not row.title:
            plan.append(PlanItem(row, None, "unmatched", "-", None, "skip_invalid", "missing company/title"))
            continue
        proposed = map_status(row.status)
        pid, method = _match(row, url_index, ct_index)
        if proposed == "ready" and not saved_to_ready:
            current = stages.get(pid, "(none)") if pid is not None else "(new)"
            plan.append(PlanItem(row, pid, method, current, None, "saved_noop", "saved: no-op"))
            continue
        if proposed is None:
            current = stages.get(pid, "(none)") if pid is not None else "(new)"
            plan.append(
                PlanItem(row, pid, method, current, None, "skip_status", f"unmapped status {row.status!r}")
            )
            continue

        if pid is None:
            stub_key = normalize_url(row.url) or f"{row.company.casefold()}|{row.title.casefold()}"
            current = stub_stage.get(stub_key, "(new)")
            if current == "(new)" or _rank(current) < _rank(proposed):
                stub_stage[stub_key] = proposed
                action = "create_stub_and_move" if current == "(new)" else "move"
                plan.append(PlanItem(row, None, method, current, proposed, action))
            else:
                plan.append(
                    PlanItem(row, None, method, current, proposed, "skip_further", "already further along")
                )
            continue

        current = stages.get(pid, "harvested")
        shown = current if pid in stages else "(none)"
        blocked = (
            proposed == "ready" and current in ("passed", "closed")
        ) or (proposed == "closed" and current == "closed")
        if blocked or _rank(current) >= _rank(proposed):
            if _rank(proposed) >= _APPLIED_RANK and proposed != "closed" and pid not in has_app:
                has_app.add(pid)
                plan.append(
                    PlanItem(row, pid, method, shown, proposed, "record_application",
                             "already at/after proposed stage; application row missing")
                )
            else:
                plan.append(PlanItem(row, pid, method, shown, proposed, "skip_further", "already further along"))
            continue

        stages[pid] = proposed
        if _rank(proposed) >= _APPLIED_RANK and proposed != "closed":
            has_app.add(pid)
        plan.append(PlanItem(row, pid, method, shown, proposed, "move"))
    return plan


def summarize(plan: list[PlanItem]) -> dict[str, int]:
    counts = {a: 0 for a in ACTIONS}
    for item in plan:
        counts[item.action] += 1
    counts["total"] = len(plan)
    counts["actionable"] = counts["move"] + counts["record_application"] + counts["create_stub_and_move"]
    return counts


def format_plan(plan: list[PlanItem], *, applied: bool = False) -> str:
    lines: list[str] = []
    for item in plan:
        proposed = item.proposed or "-"
        extra = f" ({item.reason})" if item.reason else ""
        lines.append(
            f"{item.row.company} · {item.row.title} · {item.method} · "
            f"{item.current}->{proposed} · {item.action}{extra}"
        )
    c = summarize(plan)
    verb = "applied" if applied else "would apply"
    lines.append(
        f"simplify-import ({'apply' if applied else 'dry-run'}): {c['total']} row(s); {verb} "
        f"{c['actionable']} action(s): move={c['move']} record_application={c['record_application']} "
        f"stub+move={c['create_stub_and_move']}; skipped: already further along={c['skip_further']}, "
        f"saved no-op={c['saved_noop']}, unmapped status={c['skip_status']}, invalid={c['skip_invalid']}"
    )
    if not applied and c["actionable"]:
        lines.append("Nothing written. Re-run with --apply to write these changes.")
    return "\n".join(lines)


def _create_stub(conn: sqlite3.Connection, row: ImportRow, now: str) -> int:
    norm = normalize_url(row.url)
    source_id = norm or hashlib.sha1(
        f"{row.company.casefold()}|{row.title.casefold()}".encode()
    ).hexdigest()[:16]
    conn.execute(
        """INSERT OR IGNORE INTO postings
               (source, source_posting_id, company, title, location, url,
                raw_json, first_seen_at, last_seen_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
        (
            STUB_SOURCE,
            source_id,
            row.company,
            row.title,
            row.location,
            row.url or None,
            json.dumps({"simplify_status": row.status, "line": row.line}),
            now,
            now,
        ),
    )
    found = conn.execute(
        "SELECT id FROM postings WHERE source=? AND source_posting_id=?", (STUB_SOURCE, source_id)
    ).fetchone()
    return found["id"]


def _record_application(conn: sqlite3.Connection, posting_id: int, row: ImportRow, now: str) -> None:
    existing = conn.execute("SELECT * FROM applications WHERE posting_id=?", (posting_id,)).fetchone()
    if existing is None:
        notes = "" if row.applied_date else DATE_FROM_IMPORT_NOTE
        lifecycle.ensure_application(conn, posting_id, channel="simplify", notes=notes)
        existing = conn.execute("SELECT * FROM applications WHERE posting_id=?", (posting_id,)).fetchone()
    if existing["submitted_at"] is None:
        conn.execute(
            "UPDATE applications SET submitted_at=?, updated_at=? WHERE posting_id=?",
            (row.applied_date or now, now, posting_id),
        )
        conn.commit()


def apply_plan(conn: sqlite3.Connection, plan: list[PlanItem]) -> dict[str, int]:
    """Execute a plan from `plan_import`. Re-checks the live stage before each
    move so the no-regress rule holds even if the DB changed since planning."""
    now = datetime.now(UTC).isoformat()
    stub_ids: dict[str, int] = {}
    for item in plan:
        if item.action not in ("move", "record_application", "create_stub_and_move"):
            continue
        row, stage = item.row, item.proposed
        assert stage is not None
        pid = item.posting_id
        if pid is None:
            stub_key = normalize_url(row.url) or f"{row.company.casefold()}|{row.title.casefold()}"
            pid = stub_ids.get(stub_key)
            if pid is None:
                pid = _create_stub(conn, row, now)
                stub_ids[stub_key] = pid

        if item.action != "record_application":
            live = funnel.ensure_funnel_row(conn, pid)
            current = live["stage"]
            will_move = _rank(current) < _rank(stage) and not (
                stage == "ready" and current in ("passed", "closed")
            )
            if will_move:
                had_applied_at = live["applied_at"] is not None
                funnel.move(conn, pid, stage, note=IMPORT_NOTE)
                if stage == "applied" and not had_applied_at and row.applied_date:
                    conn.execute("UPDATE funnel SET applied_at=? WHERE posting_id=?", (row.applied_date, pid))
                    conn.commit()
        if _rank(stage) >= _APPLIED_RANK and stage != "closed":
            _record_application(conn, pid, row, now)
    return summarize(plan)
