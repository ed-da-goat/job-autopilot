"""The six rubric component scorers. Each takes plain lowercased text plus
the rubric config and returns a plain score (or (score, evidence-note)
where the component can hit an "unknown" path). No DB access, no I/O —
these are pure functions so scoring is deterministic and unit-testable
without a database.
"""

from __future__ import annotations

import re

_WORD_START = re.compile(r"^[A-Za-z0-9]")
_WORD_END = re.compile(r"[A-Za-z0-9]$")


def _kw_pattern(phrase: str) -> re.Pattern:
    escaped = re.escape(phrase.lower())
    left = r"\b" if _WORD_START.match(phrase) else ""
    right = r"\b" if _WORD_END.search(phrase) else ""
    return re.compile(left + escaped + right)


def any_keyword_match(text: str, phrases: list[str]) -> bool:
    """Public helper — also used by scoring.py for the hard-filter checks."""
    return any(_kw_pattern(p).search(text) for p in phrases)


_any_match = any_keyword_match


def _find_match(text: str, phrases: list[str]) -> re.Match | None:
    for phrase in phrases:
        m = _kw_pattern(phrase).search(text)
        if m:
            return m
    return None


# ---------------------------------------------------------------- degree ---


def score_degree_fit(
    title: str, description: str, has_description: bool, rubric: dict
) -> tuple[int, str | None]:
    max_pts = rubric["components"]["degree_fit"]
    if not has_description:
        return rubric["unknown_midpoints"]["degree_fit"], "no_description"

    kw = rubric["keywords"]
    text = f"{title} {description}"

    if _any_match(text, kw["degree_no_required"]):
        return max_pts, None

    stem_match = _find_match(text, kw["degree_stems"])
    field_match = _find_match(text, kw["degree_fields"])
    if stem_match is None or field_match is None:
        return max_pts, None  # no degree mention at all

    # softener window: look within ~150 chars after the later of the two matches
    window_start = max(stem_match.end(), field_match.end())
    window = text[window_start : window_start + 150]
    if _any_match(window, kw["degree_softener"]) or _any_match(text, kw["degree_softener"]):
        return 12, None
    return 2, None


# --------------------------------------------------------------- skills ----


def score_skill_match(
    title: str, description: str, has_description: bool, rubric: dict
) -> tuple[int, str | None]:
    max_pts = rubric["components"]["skill_match"]
    kw = rubric["keywords"]
    text = title if not has_description else f"{title} {description}"

    total = 0
    for variants in kw["skill_core"]:
        if _any_match(text, variants):
            total += 4
    for variants in kw["skill_secondary"]:
        if _any_match(text, variants):
            total += 2

    score = min(total, max_pts)
    missing = None if has_description else "no_description"
    return score, missing


# ------------------------------------------------------------ seniority ----


_EXPERIENCE_RE = re.compile(
    r"(?P<min>\d{1,2})\s*(?:\+|or more)?\s*"
    r"(?:[-–—]|to)?\s*(?P<max>\d{1,2})?\s*years?(?P<tail>.{0,45})",
    re.IGNORECASE,
)
_EXPERIENCE_CONTEXT = (
    "experience",
    "required",
    "minimum",
    "at least",
    "you have",
    "looking for",
    "years of",
    "what you'll bring",
    "what you’ll bring",
    "qualifications",
)


def _experience_requirements(description: str) -> list[tuple[int, str]]:
    """Return explicit minimum-years requirements, ignoring incidental dates.

    Job descriptions often mention senior colleagues, staff teams, or company
    history. Those words are not candidate seniority evidence. A years value is
    accepted only when the surrounding text reads like a candidate requirement.
    """
    requirements: list[tuple[int, str]] = []
    for match in _EXPERIENCE_RE.finditer(description):
        context = description[max(0, match.start() - 100) : match.end() + 45].lower()
        if not any(token in context for token in _EXPERIENCE_CONTEXT):
            continue
        matched = match.group(0)
        tail = match.group("tail") or ""
        core = matched[: -len(tail)] if tail else matched
        snippet = " ".join(core.split())
        requirements.append((int(match.group("min")), snippet))
    return requirements


def score_seniority_fit(title: str, description: str, rubric: dict) -> tuple[int, str]:
    kw = rubric["keywords"]
    title_text = title
    # "Member of Technical Staff" (and the "Member, Technical Staff" variant)
    # is a rank-neutral title used by Vapi/OpenAI/Perplexity etc. — strip it
    # before tier matching so its "staff" substring doesn't false-match the
    # tier-0 "staff" pattern. An explicit signal elsewhere in the text (e.g.
    # "senior") still applies against the neutralized text.
    for phrase in kw.get("seniority_neutral_phrases", []):
        title_text = re.sub(re.escape(phrase), " ", title_text, flags=re.IGNORECASE)
    # Explicit title rank is authoritative. Description-only words such as
    # "partner with senior leaders" must never turn an early-career role into a
    # senior one.
    title_match: tuple[int, str] | None = None
    for tier in kw["seniority_tiers"]:
        match = _find_match(title_text, tier["patterns"])
        if match:
            title_match = (tier["score"], f"title: {match.group(0)}")
            # Restrictive rank in the title is authoritative. Optimistic
            # early-career language is held until explicit years have been
            # checked, because some postings market an "early career" role
            # while still requiring 3+ years.
            if tier["score"] < rubric["components"]["seniority_fit"]:
                return title_match
            break

    requirements = _experience_requirements(description)
    if requirements:
        years, snippet = max(requirements, key=lambda item: item[0])
        for tier in kw.get("seniority_year_tiers", []):
            if years >= tier["minimum"]:
                return tier["score"], f"experience requirement: {snippet}"

    if title_match:
        return title_match

    return kw["seniority_default"], "no explicit seniority signal"


# -------------------------------------------------------- founder_leverage-


def score_founder_leverage(title: str, description: str, rubric: dict) -> int:
    max_pts = rubric["components"]["founder_leverage"]
    kw = rubric["keywords"]

    total = 0
    for concept in kw["founder_leverage"]:
        in_title = _any_match(title, concept["patterns"])
        in_desc = _any_match(description, concept["patterns"])
        if not (in_title or in_desc):
            continue
        if in_title and concept["title_double"]:
            total += 6
        else:
            total += 3

    return min(total, max_pts)


# ---------------------------------------------------------- company_signal-


def matched_skill_names(text: str, rubric: dict) -> list[str]:
    """Which skill_match keyword groups hit, by their display (first) variant.
    Used by the shortlist's "why you" angle to name the matched stack."""
    kw = rubric["keywords"]
    names = []
    for variants in kw["skill_core"] + kw["skill_secondary"]:
        if any_keyword_match(text, variants):
            names.append(variants[0])
    return names


def score_inferred_vertical(title: str, description: str, rubric: dict) -> int:
    """Title/description-inferred vertical bonus — the same points a curated
    company would earn via category_bonus, but reachable from posting text
    alone. Used as a floor (via max(), never additive) so an auto-discovered
    or unknown-company posting that is *textually* a fintech/AI-agent role
    isn't stuck at the neutral midpoint just because its company row lacks
    curated metadata."""
    cfg = rubric.get("vertical_keywords")
    if not cfg:
        return 0
    text = f"{title} {description}"
    best = 0
    for vertical, keywords in cfg.items():
        points = rubric["category_bonus"].get(vertical, 0)
        if points and _any_match(text, keywords):
            best = max(best, points)
    return best


def score_company_signal(
    company_row: dict | None, title: str, description: str, rubric: dict
) -> tuple[int, str | None]:
    max_pts = rubric["components"]["company_signal"]
    neutral = rubric["unknown_midpoints"]["company_signal"]
    inferred = score_inferred_vertical(title, description, rubric)

    if company_row is None:
        return min(max(neutral, inferred), max_pts), "company_not_in_seed_list"

    category = company_row.get("category")
    auto_categories = rubric.get("auto_categories", ())
    if category in auto_categories and not company_row.get("why_fit"):
        return min(max(neutral, inferred), max_pts), "company_auto_discovered_no_why_fit"

    base = rubric["category_bonus"].get(category, 0)
    if company_row.get("verified") == "yes":
        base += 2
    return min(max(base, inferred), max_pts), None


# ------------------------------------------------- posting-text bonuses ----


def score_degree_language_bonus(title: str, description: str, rubric: dict) -> int:
    """+points once if the posting welcomes a business/finance-degree or
    non-traditional background. Case-insensitive substring match over
    title+description; applies at most once regardless of keyword count."""
    cfg = rubric.get("degree_language_bonus")
    if not cfg:
        return 0
    text = f"{title} {description}"
    if _any_match(text, cfg["keywords"]):
        return cfg["points"]
    return 0


def score_early_grad_bonus(title: str, description: str, rubric: dict) -> int:
    """+points once if the posting text signals a genuinely early-grad /
    new-grad role. Case-insensitive substring match over title+description;
    applies at most once regardless of keyword count."""
    cfg = rubric.get("early_grad_bonus")
    if not cfg:
        return 0
    text = f"{title} {description}"
    if _any_match(text, cfg["keywords"]):
        return cfg["points"]
    return 0


def score_finance_hybrid_bonus(title: str, description: str, rubric: dict) -> int:
    """Rubric v8: +points once if the posting carries finance-domain depth signal
    (payments/tax/wealth/trading/banking-infrastructure context, a finance
    degree call-out, or applied-AI-in-finance phrasing) — the "finance
    degree + shipped AI" hybrid the candidate has stated is his #1 target. Distinct
    from degree_language_bonus (credits openness to non-CS majors generally,
    not finance-domain content) and from company_signal's fintech
    category_bonus/inferred_vertical path, which already caps at 10 points
    and cannot reflect domain depth in the posting text itself. Case-
    insensitive substring match over title+description; applies at most
    once regardless of keyword count."""
    cfg = rubric.get("finance_hybrid_bonus")
    if not cfg:
        return 0
    text = f"{title} {description}"
    if _any_match(text, cfg["keywords"]):
        return cfg["points"]
    return 0
