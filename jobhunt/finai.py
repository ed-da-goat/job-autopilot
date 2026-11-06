"""`finai`: finance x AI lane selection, JD reading, and match-report logging.

Deterministic and offline. The recruiter-style scoring and rewriting happen in
the Claude Code session (subscription); this module only picks candidates,
hands the session a guarded JD, and records the resulting JSON report in
`artifacts` (kind `match_report`). No schema migration, no LLM, no network.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobhunt import eligibility, posting_guard, text_extract
from jobhunt.sources import workday

REPORT_KIND = "match_report"
TAILORED_LABEL = "Tailored (RM) 2026"
WORKED_STAGES = ("applied", "replied", "interviewing", "offer", "passed", "closed")
DIMENSIONS = (
    "keyword_match", "skills_overlap", "experience_proof", "education_fit", "role_alignment",
)


def _rx(terms: list[str]) -> re.Pattern[str]:
    """Word-boundary alternation; a trailing '*' allows any word suffix (financ* -> finance)."""
    parts = [re.escape(t.rstrip("*")) + (r"\w*" if t.endswith("*") else "") for t in terms]
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)


FINANCE_TITLE = _rx([
    "financ*", "investment*", "analyst*", "quant*", "banking", "banker*", "wealth", "fp&a",
    "treasury", "credit", "risk", "accounting", "audit*", "tax", "valuation*", "portfolio*",
    "asset management", "capital markets", "equity research", "private equity", "venture",
    "m&a", "corporate development", "strategy", "business operations", "bizops",
])
AI_SIGNAL = _rx([
    "ai", "artificial intelligence", "machine learning", "llm*", "generative", "gen ai",
    "automation", "agentic", "python", "data science",
])
TECH_CUSTOMER_TITLE = _rx([
    "forward deployed", "solutions engineer*", "solutions consultant*", "solutions architect*",
    "implementation", "deployment strategist*", "ai strategy", "customer engineer*",
    "product analyst*", "product operations",
])
FINANCE_SIGNAL = _rx([
    "financial services", "fintech", "banking", "bank*", "wealth", "trading",
    "capital markets", "payments", "investment*", "asset management", "insurance",
    "accounting", "lending", "credit",
])
PURE_CS_TITLE = _rx([
    "software engineer*", "swe", "developer*", "backend", "frontend", "full stack",
    "full-stack", "sre", "devops", "site reliability", "infrastructure engineer*",
    "platform engineer*", "ml engineer*", "machine learning engineer*", "research scientist*",
    "research engineer*", "data engineer*", "security engineer*", "firmware", "embedded",
])
# The only engineer titles allowed through the pure-CS filter (route B).
ALLOWED_ENGINEER = _rx(["forward deployed engineer*", "solutions engineer*", "customer engineer*"])
# Route C: employers whose name alone says finance (JD not needed).
KNOWN_FINANCE_EMPLOYERS = _rx([
    "wells fargo", "citi", "citigroup", "capital one", "morgan stanley", "fidelity",
    "state street", "mastercard", "visa", "blackrock", "s&p global", "bank of america",
    "jpmorgan", "jp morgan", "goldman sachs", "charles schwab", "schwab", "american express",
    "td", "bny", "northern trust", "vanguard", "invesco", "moody's", "nasdaq", "discover",
])
# Retail/branch banking titles: never route C (a finance firm's name is no proof of finance x AI work).
RETAIL_BANKING_TITLE = _rx([
    "teller*", "branch", "personal banker*", "relationship banker*", "loan officer*",
    "mortgage", "banker associate*", "client service associate*", "financial center",
    "premier banker*", "private client banker*",
])
SHORT_JD_CHARS = 200  # below this a JD is treated as unread (Workday list payloads carry none)
EXTRA_BLOCKED = ("intern", "internship", "co-op")

def _build_city_tiers() -> list[tuple[int, re.Pattern[str]]]:
    """Location tiers from the preferences file (city_tiers), best tier first."""
    return [
        (tier, _rx(list(cities)))
        for tier, cities in sorted(eligibility.user_preferences().city_tiers)
    ]


CITY_TIERS: list[tuple[int, re.Pattern[str]]] = _build_city_tiers()
REMOTE = _rx(["remote"])
US_HINT = _rx(["united states", "usa", "u.s.", "us", "america"])
INTL_HINT = _rx(sorted(eligibility.INTERNATIONAL_MARKERS))


def city_tier(location: str | None) -> int | None:
    """Best (lowest) tier across every location named; None when out of lane."""
    loc = location or ""
    for tier, pattern in CITY_TIERS:
        if pattern.search(loc):
            return tier
    if REMOTE.search(loc) and not INTL_HINT.search(loc):
        if US_HINT.search(loc) or re.fullmatch(r"\W*remote\W*", loc, re.IGNORECASE):
            return 4
    return None


def lane_route(
    title: str, description: str, category: str | None, company: str = ""
) -> str | None:
    """'A' (finance title + AI signal), 'B' (technical-customer title + finance
    signal), 'C' (finance title at a finance firm whose JD is unread, i.e. empty or
    < SHORT_JD_CHARS) or None. Pure-CS titles are dropped unless they are route-B engineers."""
    if PURE_CS_TITLE.search(ALLOWED_ENGINEER.sub(" ", title)):
        return None
    if FINANCE_TITLE.search(title) and AI_SIGNAL.search(f"{title} {description}"):
        return "A"
    if TECH_CUSTOMER_TITLE.search(title) and _finance_company(description, category):
        return "B"
    if (
        len(description.strip()) < SHORT_JD_CHARS
        and FINANCE_TITLE.search(title)
        and not RETAIL_BANKING_TITLE.search(title)
        and _finance_firm(category, company)
    ):
        return "C"
    return None


def _finance_firm(category: str | None, company: str) -> bool:
    """Finance category, or a finance word / well-known finance employer in the name."""
    if category and FINANCE_SIGNAL.search(category):
        return True
    return bool(FINANCE_SIGNAL.search(company) or KNOWN_FINANCE_EMPLOYERS.search(company))


def _finance_company(description: str, category: str | None) -> bool:
    """Finance category, or >= 2 distinct finance terms in the JD (one stray
    'insurance' mention does not make a company a finance employer)."""
    if category and FINANCE_SIGNAL.search(category):
        return True
    # Trailing 's' is dropped so "bank"/"banks" count as one term, not two.
    return len({m.group(0).lower().rstrip("s") for m in FINANCE_SIGNAL.finditer(description)}) >= 2


def _title_blocked(title: str, blocked_terms: tuple[str, ...]) -> bool:
    return any(eligibility._contains(title, t) for t in (*blocked_terms, *EXTRA_BLOCKED))


def candidates(
    conn: sqlite3.Connection, *, limit: int = 25, max_tier: int = 4
) -> list[dict[str, Any]]:
    """Ranked in-lane postings not yet worked: tier asc, route (A, B, then C), score desc (NULL last), posted_at desc."""
    blocked = eligibility.load_preferences().blocked_title_terms
    worked = ",".join("?" * len(WORKED_STAGES))
    rows = conn.execute(
        f"""SELECT p.id, p.company, p.title, p.location, p.url, p.posted_at, c.category,
                   (SELECT total FROM scores s WHERE s.posting_id = p.id
                    ORDER BY s.rubric_version DESC LIMIT 1) AS score
            FROM postings p
            LEFT JOIN companies c ON c.name = p.company
            WHERE p.status = 'active' AND p.duplicate_of IS NULL
              AND NOT EXISTS (SELECT 1 FROM funnel f WHERE f.posting_id = p.id
                              AND f.stage IN ({worked}))""",
        WORKED_STAGES,
    ).fetchall()
    # artifacts has no posting_id index, so a per-row NOT EXISTS is a full scan each time.
    reported = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT posting_id FROM artifacts WHERE kind = ?", (REPORT_KIND,))
    }

    found: list[dict[str, Any]] = []
    for r in rows:
        if r["id"] in reported:
            continue
        title = r["title"] or ""
        tier = city_tier(r["location"])
        if tier is None or tier > max_tier or _title_blocked(title, blocked):
            continue
        # Cheap title/location gates first; the big raw_json blob is read only for survivors.
        if not (FINANCE_TITLE.search(title) or TECH_CUSTOMER_TITLE.search(title)):
            continue
        full = conn.execute("SELECT source, title, raw_json FROM postings WHERE id=?", (r["id"],)).fetchone()
        _, description, _ = text_extract.extract_text(full)
        route = lane_route(title, description, r["category"], r["company"] or "")
        if route is None:
            continue
        found.append({
            "id": r["id"], "company": r["company"] or "", "title": title,
            "location": r["location"] or "", "city_tier": tier, "route": route,
            "url": r["url"] or "", "score": r["score"], "_posted": r["posted_at"] or "",
        })

    # Stable multi-pass sort: least significant key first.
    found.sort(key=lambda d: d["_posted"], reverse=True)
    found.sort(key=lambda d: -(d["score"] if d["score"] is not None else -1))
    found.sort(key=lambda d: (d["city_tier"], d["route"]))
    for d in found:
        del d["_posted"]
    return found[:limit]


def format_candidate(d: dict[str, Any]) -> str:
    score = "-" if d["score"] is None else str(d["score"])
    return (f"{d['id']}\tT{d['city_tier']}\t{d['route']}\t{score}\t"
            f"{d['company']} | {d['title']} | {d['location']}")


def jd_text(conn: sqlite3.Connection, posting_id: int) -> str | None:
    """Header line + guarded description. None when the posting does not exist.
    The skill reads JDs only through here so injection text never reaches the prompt."""
    row = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
    if row is None:
        return None
    _, description, _ = text_extract.extract_text(row)
    unavailable = False
    if row["source"] == "workday" and len(description.strip()) < SHORT_JD_CHARS:
        # On-demand detail fetch; never written back to the DB (no caching yet).
        fetched = workday.fetch_description(row["url"] or "")
        if fetched.strip():
            description = fetched
        else:
            unavailable = True
    guarded = posting_guard.guard_description(description, posting_id=posting_id)
    lines = [f"{row['company']} — {row['title']} — {row['location']} — {row['url']}"]
    if unavailable:
        lines.append("JD: unavailable (workday detail fetch failed)")
    if guarded.flagged:
        lines.append(
            f"GUARD: injection-like content removed "
            f"({len(guarded.instruction_like)} segments, {guarded.invisible_flagged} invisible chars)"
        )
    lines.append(guarded.text)
    return "\n".join(lines)


def _is_score(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def validate_report(report: Any) -> list[str]:
    """All problems with a match report; empty list means valid."""
    if not isinstance(report, dict):
        return ["report must be a JSON object"]
    errs: list[str] = []
    if not _is_score(report.get("match_before")):
        errs.append("match_before must be an int 0-100")
    dims = report.get("dimensions")
    if not isinstance(dims, dict) or set(dims) != set(DIMENSIONS):
        errs.append(f"dimensions must have exactly the keys {', '.join(DIMENSIONS)}")
    elif not all(_is_score(v) for v in dims.values()):
        errs.append("every dimensions value must be an int 0-100")
    kw = report.get("missing_keywords")
    if not _is_str_list(kw) or len(kw) > 5:
        errs.append("missing_keywords must be a list of at most 5 strings")
    if not _is_str_list(report.get("red_flags")):
        errs.append("red_flags must be a list of strings")
    if "match_after" in report and not _is_score(report["match_after"]):
        errs.append("match_after must be an int 0-100")
    if "skim_flags" in report and not _is_str_list(report["skim_flags"]):
        errs.append("skim_flags must be a list of strings")
    for key in ("resume_pdf", "notes"):
        if key in report and not isinstance(report[key], str):
            errs.append(f"{key} must be a string")
    return errs


def record_report(conn: sqlite3.Connection, posting_id: int, report: dict[str, Any]) -> list[str]:
    """Insert the report artifact; when a rendered resume exists, also register it
    and link the application to the Tailored variant. Returns human-readable notes."""
    # Lazy: tailor pulls in yaml/renderer, which candidates/jd never need.
    from jobhunt import lifecycle
    from jobhunt.resume import tailor

    notes: list[str] = []
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO artifacts (posting_id, kind, content, edited, created_at, updated_at)
           VALUES (?, ?, ?, 0, ?, ?)""",
        (posting_id, REPORT_KIND, json.dumps(report, sort_keys=True), now, now),
    )
    conn.commit()
    notes.append(f"recorded {REPORT_KIND} for posting {posting_id}")

    pdf = report.get("resume_pdf")
    if pdf and Path(pdf).exists():
        tailor.record_artifact(conn, posting_id, Path(pdf))
        variant = conn.execute(
            "SELECT id FROM resume_variants WHERE label=?", (TAILORED_LABEL,)
        ).fetchone()
        if variant is None:
            variant = lifecycle.save_resume_variant(
                conn, label=TAILORED_LABEL,
                path=f"data/apply_packs/resume/tailored/<posting_id>/{eligibility.resume_basename()}.pdf",
            )
        # ensure_application overwrites channel/ref/notes, so carry existing values through.
        prior = conn.execute("SELECT * FROM applications WHERE posting_id=?", (posting_id,)).fetchone()
        extra = ({"channel": prior["channel"] or "company careers page",
                  "confirmation_ref": prior["confirmation_ref"] or "", "notes": prior["notes"] or ""}
                 if prior else {})
        lifecycle.ensure_application(conn, posting_id, resume_variant_id=variant["id"], **extra)
        notes.append(f"linked resume variant '{TAILORED_LABEL}'")
    elif pdf:
        notes.append(f"resume_pdf not found, skipped: {pdf}")
    return notes
