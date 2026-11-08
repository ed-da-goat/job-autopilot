"""Answers the posting decides, not the candidate, so no stored profile can hold them.

A form asks where they will work (the posting's city), what pay they expect (the
posting's range), whether they have worked for or applied to the company before
(the tracker knows) and whether anyone referred them (the contacts table knows).
`posting_answers` reads those from the database and the posting and returns them
under the semantic keys keys.POSTING_DERIVED_KEYS and salary_expectation, for the
autoapply lane to merge over the stored answers (its values win).

Pure reads: it never writes the database and never touches the network. A key it
cannot decide is left out of the result, and the field that asks for it goes to
the user (solve.resolve asks about a key with no answer).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from jobhunt import eligibility, finai, text_extract
from jobhunt.apply_assist.solve import SKILL_VOCAB, US_STATE_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
# The master resume's employers decide whether a company is one the candidate has worked
# for. Defined here, as in jobhunt.autotailor, so this module does not import the
# whole tailoring stack; a test keeps the two paths equal and others point it at a
# temporary file.
MASTER_PATH = REPO_ROOT / "data" / "apply_packs" / "finai" / "master.rm.json"
# Read-only, gitignored files of the candidate's own words (never written here): their facts decide
# which tools beyond the resume they confirmed (their "## Skills" block), and their answers file
# holds two facts no stored key does yet (education_start, over_18). A test keeps FACTS_PATH
# equal to jobhunt.autotailor's; others point both at temporary files.
FACTS_PATH = REPO_ROOT / "data" / "facts.local.md"
ANSWERS_PATH = REPO_ROOT / "data" / "answers.local.md"



def default_salary() -> int | None:
    """The candidate's answer for a salary question when the posting gives no readable
    annual base range: the standing salary of the search preferences ([standing_answers]),
    or None when it is unset (fail closed: no salary is written, the field is asked)."""
    return eligibility.user_preferences().standing_answers.salary


# A one-number salary field gets the midpoint rounded to the nearest of these.
SALARY_STEP = 5_000
# Base pay for a full-time job in dollars a year lies in this window; a figure
# outside it is an hourly rate, a monthly one, a funding round or a stray number.
_MIN_ANNUAL = 20_000
_MAX_ANNUAL = 1_000_000


# --- Locations -------------------------------------------------------------------

_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)
# One posting location may list several places.
_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:;|\||\n|\s/\s|\sor\s)\s*")
_ASIDE_RE = re.compile(r"\([^()]*\)")
# Words that make a segment something other than one city.
_NOT_A_CITY_RE = re.compile(
    r"\b(?:remote|virtual|anywhere|multiple|various|nationwide|worldwide|global"
    r"|locations?)\b",
    re.IGNORECASE,
)
# Words a posting adds around a city that are not part of its name.
_PLACE_NOISE_RE = re.compile(
    r"\b(?:hybrid|on-?site|in[\s-]?office|hq|headquarters|office)\b", re.IGNORECASE
)
# "CA - Modesto", "USA - OH - Ashland", "NY-LaGuardia": a code, then a site name.
_CODE_PREFIX_RE = re.compile(r"^[A-Z]{2,3}\s*[-–]")
_DASH_RE = re.compile(r"\s[-–—]\s")
_CITY_PART_RE = re.compile(
    r"^[^\W\d_][^\W\d_ .'’-]*(?:[ .'’-]+[^\W\d_][^\W\d_ .'’-]*)*\.?$"
)
_COUNTRY_PARTS = frozenset(
    {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}
)
# A state's name that is a city's too: "New York" alone is the city. Any other bare
# state name ("Georgia", "California") is not one.
_CITY_STATE_NAMES = frozenset({"new york"})
# Not a city on their own, whatever the posting calls them.
_WIDE_PLACES = frozenset(
    {
        "united kingdom", "uk", "canada", "north america", "emea", "apac", "europe",
        "latam", "america", "americas",
    }
)  # fmt: skip


def _readable_place(segment: str) -> str | None:
    """`segment` as "City", "City, ST" or "City, State" (a trailing United States
    dropped), or None when it names no single city: a count of locations, a bare
    state or country, a code-prefixed site name, a street address."""
    text = " ".join(_ASIDE_RE.sub(" ", segment).split())
    if _NOT_A_CITY_RE.search(text):
        return None
    text = " ".join(_PLACE_NOISE_RE.sub(" ", text).split()).strip(" ,;:-–—")
    if not text or _CODE_PREFIX_RE.match(text):
        return None
    text = _DASH_RE.split(text)[0].strip(" ,;:-–—")  # "San Francisco - Mission Bay"
    parts = [part.strip() for part in text.split(",")]
    if not all(parts) or len(parts) > 3:
        return None
    if len(parts) > 1 and parts[-1].casefold() in _COUNTRY_PARTS:
        parts.pop()
    if not all(_CITY_PART_RE.match(part) for part in parts):
        return None
    first = parts[0].casefold()
    if len(parts) == 1 and (
        (first in US_STATE_NAMES and first not in _CITY_STATE_NAMES)
        or first in _COUNTRY_PARTS
        or first in _WIDE_PLACES
    ):
        return None
    return ", ".join(parts)


def posting_city(location: str | None) -> str | None:
    """The one city a posting's location names, or None for a remote or unreadable
    location. A location that lists several places gives the best-tier one
    (finai.city_tier: the cities the candidate is looking in come first), then the first
    listed."""
    text = location or ""
    if _REMOTE_RE.search(text):
        return None
    places = [
        place
        for segment in _SEGMENT_SPLIT_RE.split(text)
        if (place := _readable_place(segment))
    ]
    if not places:
        return None
    ranked = [
        (tier, index)
        for index, place in enumerate(places)
        if (tier := finai.city_tier(place)) is not None
    ]
    return places[min(ranked)[1]] if ranked else places[0]


# --- Salary ------------------------------------------------------------------------

_NUMBER = r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?"
_MONEY_RANGE_RE = re.compile(
    rf"\$\s?(?P<lo>{_NUMBER})\s?(?P<lo_k>[kK])?(?![\d,])"
    r"\s*(?:-|–|—|to|and)\s*"
    rf"\$?\s?(?P<hi>{_NUMBER})\s?(?P<hi_k>[kK])?(?![\d,])"
)
# What follows a figure that is not a yearly one.
_NOT_YEARLY_RE = re.compile(
    r"(?:per|an|/)\s*(?:hour|hr)\b|\bhourly\b|\bper\s+month\b|\bmonthly\b",
    re.IGNORECASE,
)
# What introduces a figure that is not base pay.
_NOT_BASE_RE = re.compile(
    r"\b(?:bonus(?:es)?|equity|stock|rsus?|options?|commissions?|ote|on-?target"
    r"|signing|sign-on|variable|incentives?|total\s+(?:compensation|cash|rewards?"
    r"|target))\b",
    re.IGNORECASE,
)
_BASE_RE = re.compile(r"\b(?:base|salary|pay)\b", re.IGNORECASE)
# A dollar range with none of these near it is not a salary (a funding round, a
# revenue figure).
_PAY_WORD_RE = re.compile(
    r"\b(?:salary|salaries|base|pay|paid|compensation|wage|annual(?:ly)?|yearly"
    r"|per\s+year|a\s+year)\b|/\s*(?:yr|year)\b",
    re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"[.!?]\s|\n")
_LABEL_CHARS = 90  # how far before a range its place label may start
_CONTEXT_BEFORE, _CONTEXT_AFTER = 160, 60


def _dollars(number: str, k: str | None) -> float:
    value = float(number.replace(",", ""))
    return value * 1000 if k else value


def _annual_range(low: float, high: float) -> tuple[int, int] | None:
    """(low, high) when both are plausible annual figures in order, else None."""
    if low > high or low < _MIN_ANNUAL or high > _MAX_ANNUAL:
        return None
    return int(round(low)), int(round(high))


def _ashby_salary(components: Any) -> list[tuple[int, int]]:
    """The yearly USD salary ranges among an Ashby tier's components (equity,
    bonus and commission components are ignored)."""
    ranges: list[tuple[int, int]] = []
    for component in components if isinstance(components, list) else []:
        if (
            isinstance(component, dict)
            and component.get("compensationType") == "Salary"
            and str(component.get("interval") or "").strip().upper() == "1 YEAR"
            and str(component.get("currencyCode") or "").upper() == "USD"
            and isinstance(component.get("minValue"), (int, float))
            and isinstance(component.get("maxValue"), (int, float))
        ):
            bounds = _annual_range(component["minValue"], component["maxValue"])
            if bounds:
                ranges.append(bounds)
    return ranges


def _structured_ranges(source: str, raw: dict[str, Any]) -> list[tuple[str, int, int]]:
    """(label, low, high) for each annual USD base-pay range the ATS's own fields
    give: Lever's `salaryRange`, Ashby's `compensation`. The label is the tier's
    title and summary (Ashby), which sometimes names the place the tier is for."""
    found: list[tuple[str, int, int]] = []
    if source == "lever":
        pay = raw.get("salaryRange")
        if (
            isinstance(pay, dict)
            and pay.get("interval") == "per-year-salary"
            and str(pay.get("currency") or "").upper() == "USD"
            and isinstance(pay.get("min"), (int, float))
            and isinstance(pay.get("max"), (int, float))
        ):
            bounds = _annual_range(pay["min"], pay["max"])
            if bounds:
                found.append(("", *bounds))
    elif source == "ashby":
        compensation = raw.get("compensation")
        if isinstance(compensation, dict):
            tiers = compensation.get("compensationTiers")
            for tier in tiers if isinstance(tiers, list) else []:
                if not isinstance(tier, dict):
                    continue
                label = f"{tier.get('title') or ''} {tier.get('tierSummary') or ''}"
                found.extend(
                    (label, low, high)
                    for low, high in _ashby_salary(tier.get("components"))
                )
            if not found:
                found.extend(
                    ("", low, high)
                    for low, high in _ashby_salary(
                        compensation.get("summaryComponents")
                    )
                )
    return found


def _is_base_pay(text: str, start: int, end: int) -> bool:
    """Whether the dollar figure at text[start:end] reads as annual base pay: not
    hourly or monthly, with a pay word near it, and not introduced by a bonus,
    equity, commission or total-compensation word (unless "base", "salary" or "pay"
    comes after that word in the same sentence)."""
    if _NOT_YEARLY_RE.search(text[end : end + 24]):
        return False
    if not (
        _PAY_WORD_RE.search(text[max(0, start - _CONTEXT_BEFORE) : start])
        or _PAY_WORD_RE.search(text[end : end + _CONTEXT_AFTER])
    ):
        return False
    lead = _SENTENCE_END_RE.split(text[max(0, start - _LABEL_CHARS) : start])[-1]
    not_base = list(_NOT_BASE_RE.finditer(lead))
    if not not_base:
        return True
    return bool(_BASE_RE.search(lead[not_base[-1].end() :]))


def _text_ranges(text: str) -> list[tuple[str, int, int]]:
    """(label, low, high) for each annual base-pay dollar range in the posting
    text, in reading order. The label is the words before the range back to the
    previous range, which lets a caller pick the range a place introduces
    ("New York City: $150,000 - $180,000")."""
    found: list[tuple[str, int, int]] = []
    previous_end = 0
    for match in _MONEY_RANGE_RE.finditer(text):
        label = text[max(previous_end, match.start() - _LABEL_CHARS) : match.start()]
        previous_end = match.end()
        low = _dollars(match["lo"], match["lo_k"])
        high = _dollars(match["hi"], match["hi_k"])
        if match["hi_k"] and not match["lo_k"] and low < 1000:
            low *= 1000  # "$120-150K"
        bounds = _annual_range(low, high)
        if bounds and _is_base_pay(text, match.start(), match.end()):
            found.append((label, *bounds))
    return found


def _pick_range(
    ranges: list[tuple[str, int, int]], city: str | None
) -> tuple[int, int] | None:
    """The range labeled with the posting's city, else the first one."""
    if not ranges:
        return None
    name = (city or "").split(",")[0].strip().casefold()
    if name:
        for label, low, high in ranges:
            if name in label.casefold():
                return low, high
    _, low, high = ranges[0]
    return low, high


def _money(amount: int) -> str:
    return f"${amount:,}"


def _posting_text(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """The posting's text without finai.jd_text's header line. Not jd_text for a
    Workday posting: it fetches a short description over the network, and this
    module does not."""
    if row["source"] == "workday":
        _, description, _ = text_extract.extract_text(row)
        return description or ""
    text = finai.jd_text(conn, row["id"]) or ""
    return text.split("\n", 1)[1] if "\n" in text else ""


def _salary_answers(
    conn: sqlite3.Connection, row: sqlite3.Row, city: str | None
) -> dict[str, str]:
    """salary_expectation ("$120,000 - $150,000") and salary_midpoint ("135000":
    digits, to the nearest $5,000) from the posting's annual base-pay range, or
    The candidate's default when it shows none."""
    try:
        raw = json.loads(row["raw_json"] or "{}")
    except (TypeError, ValueError):
        raw = {}
    picked = None
    if isinstance(raw, dict):
        picked = _pick_range(_structured_ranges(row["source"], raw), city)
    if picked is None:
        picked = _pick_range(_text_ranges(_posting_text(conn, row)), city)
    if picked is None:
        fallback = default_salary()
        if fallback is None:
            return {}
        picked = (fallback, fallback)
    low, high = picked
    middle = int((low + high) / 2 / SALARY_STEP + 0.5) * SALARY_STEP
    written = _money(low) if low == high else f"{_money(low)} - {_money(high)}"
    return {"salary_expectation": written, "salary_midpoint": str(middle)}


# --- The tracker ---------------------------------------------------------------------

_LEGAL_SUFFIXES = frozenset(
    {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co",
     "company", "plc", "gmbh", "lp", "llp"}
)  # fmt: skip
_NAME_CHARS_RE = re.compile(r"[^\w&]+")


def _company_words(name: str | None) -> tuple[str, ...]:
    """A company name as comparable words: asides, emoji, asterisks and other
    decoration gone, lowercased, one trailing legal suffix dropped
    ("🔥 DoorDash" and "Chronograph (chronograph.pe)" read "doordash" and
    "chronograph")."""
    text = _ASIDE_RE.sub(" ", str(name or "")).casefold()
    words = _NAME_CHARS_RE.sub(" ", text).split()
    if len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return tuple(words)


def same_company(left: str | None, right: str | None) -> bool:
    """Whether two company names may be one company: the same words, the same
    letters run together ("J.P. Morgan" and "JPMorgan"), or one is the leading
    words of the other ("Example Exchange" and "Example Exchange Group"). A false
    yes sends the question to the user, which is the safe error."""
    one, two = _company_words(left), _company_words(right)
    if not one or not two:
        return False
    if "".join(one) == "".join(two):
        return True
    shorter, longer = sorted((one, two), key=len)
    return longer[: len(shorter)] == shorter


def _resume_employers() -> list[str] | None:
    """The companies in the master resume's workExperience, or None when the file
    cannot be read (nothing is known about their employers then)."""
    try:
        master = json.loads(Path(MASTER_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    work = master.get("workExperience") if isinstance(master, dict) else None
    if not isinstance(work, list):
        return None
    return [
        str(job["company"])
        for job in work
        if isinstance(job, dict) and job.get("company")
    ]


def _worked_or_applied_here(
    conn: sqlite3.Connection, posting_id: int, company: str | None
) -> bool | None:
    """True when `company` is on the master resume, or the tracker holds a
    SUBMITTED application for another posting at the same company. Unsubmitted
    rows (blocked or dry autoapply attempts) do not count: nothing was applied.
    None when the master resume cannot be read."""
    employers = _resume_employers()
    if employers is None:
        return None
    if any(same_company(company, employer) for employer in employers):
        return True
    rows = conn.execute(
        "SELECT p.company FROM applications a JOIN postings p ON p.id = a.posting_id "
        "WHERE a.posting_id != ? AND a.submitted_at IS NOT NULL",
        (posting_id,),
    ).fetchall()
    return any(same_company(company, row["company"]) for row in rows)


# Tools the resume shows them using without writing the tool's name: a SQL database or an ORM
# over one is SQL (2026-10-27: "SQL experience: Yes" from Postgres, SQLite, SQLAlchemy;
# data/facts.local.md skills, master resume line 27).
_SKILL_EVIDENCE = {"sql": ("postgres", "postgresql", "sqlite", "sqlalchemy", "mysql")}


def _facts_skills_text() -> str:
    """The "## Skills" block of data/facts.local.md, casefolded ("" when the file or the
    block is missing): tools the candidate confirmed in their own words (2026-10-27: Pandas, Tableau,
    Salesforce, HubSpot), on top of what the resume names."""
    try:
        lines = Path(FACTS_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    block: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("## "):
            inside = line[3:].strip().casefold() == "skills"
            continue
        if inside:
            block.append(line)
    return "\n".join(block).casefold()


def _resume_skills() -> str | None:
    """The tools of solve.SKILL_VOCAB the master resume or the Skills block of the facts
    file names, comma-joined and sorted ("excel,pandas,postgres,python,sql"); "" when they
    name none, None when the resume cannot be read. A yes/no skill question is answered Yes
    only for a tool in this list."""
    try:
        master = json.loads(Path(MASTER_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    text = json.dumps(master, ensure_ascii=False).casefold() + "\n" + _facts_skills_text()

    def named(word: str) -> bool:
        return re.search(rf"\b{re.escape(word)}\b", text) is not None

    skills = {skill for skill in SKILL_VOCAB if named(skill)}
    for skill, evidence in _SKILL_EVIDENCE.items():
        if skill in SKILL_VOCAB and any(named(word) for word in evidence):
            skills.add(skill)
    return ",".join(sorted(skills))


_FILE_ANSWER_KEYS = ("education_start", "over_18")


def _answers_file() -> dict[str, str]:
    """education_start ("August 2024") and over_18 ("Yes") from data/answers.local.md, each
    only when the file holds a non-empty `key: value` line for it; {} when unreadable."""
    try:
        lines = Path(ANSWERS_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    found: dict[str, str] = {}
    for raw in lines:
        key, sep, value = raw.strip().partition(":")
        key, value = key.strip(), value.strip()
        if sep and key in _FILE_ANSWER_KEYS and value:
            found[key] = value
    return found


_PROP_TRADING_RE = re.compile(r"\bprop(?:rietary)?\s+(?:trading|shop)\b")


def _resume_prop_trading() -> str | None:
    """"No" when the master resume names no proprietary trading experience (the resume is
    the full record of their work, as for worked_here); None when it names one (a human reads
    the answer) or the file cannot be read."""
    try:
        master = json.loads(Path(MASTER_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    text = json.dumps(master, ensure_ascii=False).casefold()
    return None if _PROP_TRADING_RE.search(text) else "No"


def _submitted_here(conn: sqlite3.Connection, company: str | None) -> bool:
    """True when the tracker holds a SUBMITTED application at `company` (any posting)."""
    rows = conn.execute(
        "SELECT p.company FROM applications a JOIN postings p ON p.id = a.posting_id "
        "WHERE a.submitted_at IS NOT NULL"
    ).fetchall()
    return any(same_company(company, row["company"]) for row in rows)


def _has_contact_or_referral(
    conn: sqlite3.Connection, posting_id: int, company: str | None
) -> bool:
    """True when the contacts table has a row at `company`, or a referral is
    recorded for this posting or any other posting at `company` (a touch whose
    kind says referral; companies compared as same_company does, case-insensitively)."""
    for row in conn.execute("SELECT company FROM contacts").fetchall():
        if same_company(company, row["company"]):
            return True
    rows = conn.execute(
        "SELECT t.posting_id, p.company FROM touches t "
        "JOIN postings p ON p.id = t.posting_id WHERE lower(t.kind) LIKE '%referr%'"
    ).fetchall()
    return any(
        row["posting_id"] == posting_id
        or same_company(company, row["company"])
        or (bool(company) and company.casefold() == (row["company"] or "").casefold())
        for row in rows
    )


def posting_answers(conn: sqlite3.Connection, posting_id: int) -> dict[str, str]:
    """The answers posting `posting_id` decides, as {semantic key: value}:

      work_location, desired_location  the posting's city (desired_location only
                                       for a city in finai tier 1 or 2)
      salary_expectation               "$120,000 - $150,000", the annual base range
      salary_midpoint                  its midpoint in digits, to the nearest $5,000
      prior_relationship               "No" unless the company is on the master
                                       resume or a submitted application exists
                                       at another of its postings
      referred                         "No" when the contacts table has no one at
                                       the company and no referral is recorded
      worked_here                      "Yes" when the company is on the master resume,
                                       else "No" (absent when the resume is unreadable)
      applied_before                   "Yes" when a submitted application exists at the
                                       company, else "No"
      resume_skills                    the solve.SKILL_VOCAB tools the master resume or
                                       the facts file's Skills block names, comma-joined
                                       (a skill question is Yes only for these)
      prop_trading                     "No" when the master resume names no proprietary
                                       trading experience (absent when it names one or
                                       is unreadable)
      education_start, over_18         the candidate's "August 2024" and "Yes" from
                                       data/answers.local.md (absent when the file or the
                                       line is missing)

    A key that cannot be decided is absent. Pure reads: nothing is written, and
    `conn` must return rows by name (jobhunt.db.connect does)."""
    row = conn.execute("SELECT * FROM postings WHERE id = ?", (posting_id,)).fetchone()
    if row is None:
        return {}
    answers: dict[str, str] = {}
    city = posting_city(row["location"])
    if city:
        answers["work_location"] = city
        if finai.city_tier(city) in (1, 2):
            answers["desired_location"] = city
    answers.update(_salary_answers(conn, row, city))
    if _worked_or_applied_here(conn, posting_id, row["company"]) is False:
        answers["prior_relationship"] = "No"
    if not _has_contact_or_referral(conn, posting_id, row["company"]):
        answers["referred"] = "No"
    employers = _resume_employers()
    if employers is not None:
        answers["worked_here"] = (
            "Yes" if any(same_company(row["company"], e) for e in employers) else "No"
        )
    answers["applied_before"] = "Yes" if _submitted_here(conn, row["company"]) else "No"
    skills = _resume_skills()
    if skills is not None:
        answers["resume_skills"] = skills
    prop_trading = _resume_prop_trading()
    if prop_trading is not None:
        answers["prop_trading"] = prop_trading
    answers.update(_answers_file())
    return answers
