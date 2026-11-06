"""Deterministic eligibility assessment layered on top of rubric scoring.

Scoring answers "how attractive is this role?"; eligibility answers "should
this role consume daily-triage attention?". Blocked roles remain searchable,
and ambiguous roles are routed to a separate review queue.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import UTC, date, datetime
from pathlib import Path

from jobhunt.text_extract import extract_text

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOCAL_PREFERENCES_PATH = DATA_DIR / "search-preferences.local.toml"
TEMPLATE_PREFERENCES_PATH = DATA_DIR / "search-preferences.template.toml"

US_MARKERS = {
    "united states", "usa", "u.s.", "remote (us", "remote us", "us remote",
    "san francisco", "new york", "nyc", "washington dc", "washington, d.c.",
    "palo alto", "foster city", "mountain view", "atlanta", "marietta",
    "boston", "austin", "seattle", "chicago", "denver", "los angeles",
    "california", "texas", "georgia", "virginia", "maryland", "florida",
}
INTERNATIONAL_MARKERS = {
    "london", "united kingdom", "uk", "europe", "emea", "dublin", "ireland",
    "bangalore", "india", "tokyo", "japan", "singapore", "spain", "chile",
    "mexico", "poland", "brazil", "australia", "uae", "netherlands",
    "belgium", "germany", "dach", "canada", "toronto", "paris", "france",
}
MTS_PHRASES = ("member of technical staff", "member, technical staff")
YEARS_RE = re.compile(
    r"(?P<min>\d{1,2})\s*(?:\+|or more)?\s*"
    r"(?:[-–—]|to)?\s*(?P<max>\d{1,2})?\s*years?(?P<tail>.{0,35})",
    re.IGNORECASE,
)


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
)  # fmt: skip


@dataclass(frozen=True)
class StandingAnswers:
    """The candidate's standing application answers (the [standing_answers] table).

    Fail closed: every field defaults to "unset" ("", None, ()), and an unset value means
    the matching rule in apply_assist/solve.py does not answer: the field is asked and
    handed off exactly like an unknown value. Nothing here ever has a default answer."""

    earliest_start: date | None = None
    travel_percent: int | None = None
    working_location: str = ""
    sports: str = ""
    current_employer: str = ""
    current_title: str = ""
    school: str = ""
    school_aliases: tuple[str, ...] = ()
    education_text: str = ""
    salary: int | None = None
    bonus: int | None = None
    founder_experience: bool | None = None
    onsite_ok: str = ""
    years_experience: str = ""
    years_since_graduation: str = ""
    camera_on: str = ""
    notice_period: str = ""
    years_ma_experience: str = ""
    pronouns: str = ""
    government_employment: str = ""
    military_service: str = ""

    @property
    def earliest_start_text(self) -> str:
        """"January 4, 2027" for 2027-01-04; "" when no earliest start is set."""
        if self.earliest_start is None:
            return ""
        d = self.earliest_start
        return f"{_MONTH_NAMES[d.month - 1]} {d.day}, {d.year}"

    @property
    def salary_text(self) -> str:
        """"$85,000 base plus $15,000 bonus", or "$85,000" with no bonus set; "" with no salary."""
        if self.salary is None:
            return ""
        if self.bonus is None:
            return f"${self.salary:,}"
        return f"${self.salary:,} base plus ${self.bonus:,} bonus"

    @property
    def bonus_text(self) -> str:
        return "" if self.bonus is None else f"${self.bonus:,}"

    def rule_answers(self) -> dict[str, str]:
        """The key -> answer map for the unattended lane, set values only."""
        pairs = {
            "onsite_ok": self.onsite_ok,
            "years_experience": self.years_experience,
            "years_since_graduation": self.years_since_graduation,
            "camera_on": self.camera_on,
            "notice_period": self.notice_period,
            "years_ma_experience": self.years_ma_experience,
            "pronouns": self.pronouns,
            "government_employment": self.government_employment,
            "military_service": self.military_service,
        }
        return {key: value for key, value in pairs.items() if value}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _whole_number(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def load_standing_answers(table: object) -> StandingAnswers:
    """Parse the [standing_answers] table; anything missing or malformed stays unset."""
    if not isinstance(table, dict):
        return StandingAnswers()
    start = table.get("earliest_start")
    if isinstance(start, datetime):
        start = start.date()
    elif isinstance(start, str):
        try:
            start = date.fromisoformat(start.strip())
        except ValueError:
            start = None
    if not isinstance(start, date):
        start = None
    founder = table.get("founder_experience")
    aliases = table.get("school_aliases", [])
    return StandingAnswers(
        earliest_start=start,
        travel_percent=_whole_number(table.get("travel_percent")),
        working_location=_text(table.get("working_location")),
        sports=_text(table.get("sports")),
        current_employer=_text(table.get("current_employer")),
        current_title=_text(table.get("current_title")),
        school=_text(table.get("school")),
        school_aliases=tuple(a for a in (_text(x) for x in aliases) if a)
        if isinstance(aliases, list)
        else (),
        education_text=_text(table.get("education_text")),
        salary=_whole_number(table.get("salary")),
        bonus=_whole_number(table.get("bonus")),
        founder_experience=founder if isinstance(founder, bool) else None,
        onsite_ok=_text(table.get("onsite_ok")),
        years_experience=_text(table.get("years_experience")),
        years_since_graduation=_text(table.get("years_since_graduation")),
        camera_on=_text(table.get("camera_on")),
        notice_period=_text(table.get("notice_period")),
        years_ma_experience=_text(table.get("years_ma_experience")),
        pronouns=_text(table.get("pronouns")),
        government_employment=_text(table.get("government_employment")),
        military_service=_text(table.get("military_service")),
    )


@dataclass(frozen=True)
class SearchPreferences:
    version: int
    allowed_countries: tuple[str, ...]
    allow_us_remote: bool
    max_eligible_years: int
    review_years_max: int
    blocked_title_terms: tuple[str, ...]
    review_title_terms: tuple[str, ...]
    target_roles: tuple[str, ...]
    target_start: str
    required_application_fields: tuple[str, ...]
    sponsorship_status: str = "unknown"
    candidate_name: str = ""
    contact_email: str = ""
    office_city_order: tuple[str, ...] = ()
    home_metro: tuple[str, ...] = ()
    home_state: str = ""
    city_tiers: tuple[tuple[int, tuple[str, ...]], ...] = ()
    standing_answers: StandingAnswers = field(default_factory=StandingAnswers)
    lane: "LaneConfig" = field(default_factory=lambda: LaneConfig())


LANE_PRESETS = ("target_roles", "finance_ai")


@dataclass(frozen=True)
class LaneConfig:
    """The auto-apply candidate lane (the [lane] table). See the template for each key."""

    preset: str = "target_roles"
    include_title_terms: tuple[str, ...] = ()  # empty -> target_roles
    exclude_title_terms: tuple[str, ...] = ()
    require_jd_terms: tuple[str, ...] = ()  # any-of in the JD; empty -> not required
    min_score: int | None = None
    max_tier: int | None = None


def load_lane(raw: object) -> LaneConfig:
    if raw is None:
        return LaneConfig()
    if not isinstance(raw, dict):
        raise ValueError("[lane] must be a table")
    preset = str(raw.get("preset", "target_roles"))
    if preset not in LANE_PRESETS:
        raise ValueError(f"[lane] preset must be one of {', '.join(LANE_PRESETS)}, got {preset!r}")

    def terms(key: str) -> tuple[str, ...]:
        return tuple(str(t).strip() for t in raw.get(key, []) if str(t).strip())

    def opt_int(key: str) -> int | None:
        return None if raw.get(key) is None else int(raw[key])

    return LaneConfig(
        preset=preset,
        include_title_terms=terms("include_title_terms"),
        exclude_title_terms=terms("exclude_title_terms"),
        require_jd_terms=terms("require_jd_terms"),
        min_score=opt_int("min_score"),
        max_tier=opt_int("max_tier"),
    )


@dataclass
class EligibilityAssessment:
    status: str = "eligible"
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    preferences_version: int | None = None
    rubric_version: int | None = None

    def finalize(self) -> "EligibilityAssessment":
        self.blockers = list(dict.fromkeys(self.blockers))
        self.warnings = list(dict.fromkeys(self.warnings))
        self.evidence = list(dict.fromkeys(self.evidence))[:8]
        self.status = "blocked" if self.blockers else "review" if self.warnings else "eligible"
        return self


def load_preferences(path: Path | None = None) -> SearchPreferences:
    selected = path or (LOCAL_PREFERENCES_PATH if LOCAL_PREFERENCES_PATH.exists() else TEMPLATE_PREFERENCES_PATH)
    with selected.open("rb") as handle:
        data = tomllib.load(handle)
    return SearchPreferences(
        version=int(data["version"]),
        allowed_countries=tuple(data.get("allowed_countries", ["US"])),
        allow_us_remote=bool(data.get("allow_us_remote", True)),
        max_eligible_years=int(data.get("max_eligible_years", 3)),
        review_years_max=int(data.get("review_years_max", 5)),
        blocked_title_terms=tuple(data.get("blocked_title_terms", [])),
        review_title_terms=tuple(data.get("review_title_terms", [])),
        target_roles=tuple(data.get("target_roles", [])),
        target_start=str(data.get("target_start", "")),
        required_application_fields=tuple(data.get("required_application_fields", [])),
        sponsorship_status=str(data.get("sponsorship_status", "unknown")),
        candidate_name=str(data.get("candidate_name", "")).strip(),
        contact_email=str(data.get("contact_email", "")).strip(),
        office_city_order=tuple(data.get("office_city_order", [])),
        home_metro=tuple(str(c).lower() for c in data.get("home_metro", [])),
        home_state=str(data.get("home_state", "")).strip().lower(),
        city_tiers=tuple(
            (int(entry["tier"]), tuple(entry.get("cities", [])))
            for entry in data.get("city_tiers", [])
        ),
        standing_answers=load_standing_answers(data.get("standing_answers")),
        lane=load_lane(data.get("lane")),
    )


@lru_cache(maxsize=1)
def user_preferences() -> SearchPreferences:
    """The active preferences (local file, else the template), loaded once per process."""
    return load_preferences()


def candidate_file_stem() -> str:
    """The candidate's name as a filename stem: "Alex Rivera" -> "Alex_Rivera"."""
    words = re.sub(r"[^A-Za-z0-9]+", " ", user_preferences().candidate_name).split()
    return "_".join(words) or "Candidate"


def resume_basename() -> str:
    """Resume file name without extension, e.g. "Alex_Rivera_Resume"."""
    return f"{candidate_file_stem()}_Resume"


def cover_letter_basename() -> str:
    """Cover-letter file name without extension, e.g. "Alex_Rivera_Cover_Letter"."""
    return f"{candidate_file_stem()}_Cover_Letter"


def _contains(text: str, phrase: str) -> bool:
    return re.search(r"\b" + re.escape(phrase) + r"\b", text, re.IGNORECASE) is not None


def _assess_location(location: str, result: EligibilityAssessment) -> None:
    normalized = (location or "").strip().lower()
    if not normalized:
        result.warnings.append("location_unspecified")
        return
    has_us = any(marker in normalized for marker in US_MARKERS)
    has_international = any(_contains(normalized, marker) for marker in INTERNATIONAL_MARKERS)
    if has_us:
        result.evidence.append(f"US location option: {location}")
    elif has_international:
        result.blockers.append("international_only")
        result.evidence.append(f"International-only location: {location}")
    elif "remote" in normalized:
        result.warnings.append("remote_geography_unspecified")
        result.evidence.append(f"Remote scope needs review: {location}")
    else:
        result.warnings.append("location_unrecognized")
        result.evidence.append(f"Unrecognized location: {location}")


def _experience_requirements(description: str) -> list[tuple[int, str]]:
    requirements: list[tuple[int, str]] = []
    for match in YEARS_RE.finditer(description):
        matched = match.group(0)
        tail = match.group("tail") or ""
        core = matched[: -len(tail)] if tail else matched
        snippet = " ".join(core.split())
        context = description[max(0, match.start() - 100) : match.end() + 35].lower()
        if not any(
            token in context
            for token in (
                "experience", "required", "minimum", "at least", "you have",
                "looking for", "what you'll bring", "what you’ll bring", "qualifications",
            )
        ):
            continue
        requirements.append((int(match.group("min")), snippet))
    return requirements


def assess_posting(posting: sqlite3.Row | dict, preferences: SearchPreferences) -> EligibilityAssessment:
    title, description, _ = extract_text(posting)
    result = EligibilityAssessment(preferences_version=preferences.version)
    _assess_location(posting["location"] or "", result)

    is_mts = any(phrase in title for phrase in MTS_PHRASES)
    for term in preferences.blocked_title_terms:
        if _contains(title, term):
            result.blockers.append("seniority_title")
            result.evidence.append(f"Blocked seniority title: {term}")
    if is_mts:
        result.warnings.append("mts_seniority_ambiguous")
        result.evidence.append("Member of Technical Staff rank requires description review")
    else:
        for term in preferences.review_title_terms:
            if _contains(title, term):
                result.warnings.append("seniority_title_ambiguous")
                result.evidence.append(f"Ambiguous seniority title: {term}")

    requirements = _experience_requirements(description)
    if requirements:
        years, snippet = max(requirements, key=lambda item: item[0])
        result.evidence.append(f"Experience requirement: {snippet}")
        if years > preferences.review_years_max:
            result.blockers.append("experience_6_plus")
        elif years > preferences.max_eligible_years:
            result.warnings.append("experience_4_to_5")

    lower = description.lower()
    if any(
        phrase in lower
        for phrase in (
            "direct reports",
            "manage a team",
            "manage the team",
            "people management experience",
        )
    ):
        result.blockers.append("people_management")
        result.evidence.append("Description requires people-management responsibility")
    regulated_text = f"{title} {lower}"
    if any(
        phrase in regulated_text
        for phrase in (
            "federal",
            "us government",
            "u.s. government",
            "security clearance",
            "clearance required",
            "u.s. citizenship",
            "us citizenship",
        )
    ):
        result.warnings.append("government_eligibility_needs_confirmation")
        result.evidence.append("Government/clearance eligibility requires manual confirmation")
    if preferences.sponsorship_status == "unknown" and any(
        phrase in lower for phrase in ("no sponsorship", "cannot sponsor", "unable to sponsor")
    ):
        result.warnings.append("work_authorization_needs_confirmation")
        result.evidence.append("Posting says sponsorship is unavailable")
    return result.finalize()


def upsert_assessment(
    conn: sqlite3.Connection,
    posting_id: int,
    rubric_version: int,
    preferences: SearchPreferences,
    assessment: EligibilityAssessment,
) -> None:
    assessment.preferences_version = preferences.version
    assessment.rubric_version = rubric_version
    conn.execute(
        """INSERT INTO eligibility_assessments
               (posting_id, rubric_version, preferences_version, status,
                blockers, warnings, evidence, assessed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(posting_id, rubric_version, preferences_version) DO UPDATE SET
             status=excluded.status, blockers=excluded.blockers,
             warnings=excluded.warnings, evidence=excluded.evidence,
             assessed_at=excluded.assessed_at""",
        (
            posting_id,
            rubric_version,
            preferences.version,
            assessment.status,
            json.dumps(assessment.blockers),
            json.dumps(assessment.warnings),
            json.dumps(assessment.evidence),
            datetime.now(UTC).isoformat(),
        ),
    )


def latest_assessment(conn: sqlite3.Connection, posting_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM eligibility_assessments WHERE posting_id=?
           ORDER BY rubric_version DESC, preferences_version DESC LIMIT 1""",
        (posting_id,),
    ).fetchone()
