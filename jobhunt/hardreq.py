"""No-model hard-requirement filter for the auto-apply lane.

`hard_requirement(title, description, min_years=N)` answers one question without a model call:
does this posting state a requirement the candidate cannot meet (a law degree, a PhD, a master's with no
bachelor's route, a license they do not hold, or N+ years of experience)? A tailoring call costs
about 30 seconds, so a posting that can only be dropped afterwards is skipped before it.

It is biased hard toward NOT dropping: a wrongly skipped posting is lost, a kept one costs one
model call. Anything that reads as preferred, optional, an alternative ("or equivalent"), or that
sits in a preferred / nice-to-have section is ignored. The year rule reuses
`eligibility._experience_requirements` rather than a second parser.

The candidate holds a bachelor's degree (per the master resume) and no professional
license or higher degree, so every license named here counts against them.
"""

from __future__ import annotations

import re

from jobhunt.eligibility import _experience_requirements

RULES = ("jd", "phd", "masters", "license", "domain_years", "years")

# Years claims above this are a company history ("30 years in business"), not a requirement.
_MAX_PLAUSIBLE_YEARS = 15
# domain_years fires from this many years: a few years of a named elite-finance role is the bar itself.
DOMAIN_MIN_YEARS = 2

# Words that soften or replace a requirement. Any clause holding one is blanked before the rules run.
_SOFT_RE = re.compile(
    r"\bprefer(?:red|ably|ence|ences)?\b|nice[- ]to[- ]have|\bplus\b|\bbonus\b|\bideally\b|"
    r"\bdesirable\b|\bhelpful\b|\bbeneficial\b|\badvantage\b|\boptional\b|\bor equivalent\b|"
    r"\bequivalent (?:work |practical |professional |relevant )?(?:experience|education|training)\b|"
    r"\bin lieu\b|\balternatively\b|\bnot (?:be )?required\b|\bnot necessary\b|"
    r"\bno [a-z' ]{0,25}required\b|\bwithout (?:a|an) [a-z' ]{0,20}degree\b|"
    r"\bpursuing\b|\bworking (?:toward|towards|on)\b|\bin progress\b|\beligible\b|"
    # years that are not a requirement on the applicant
    r"\byears ago\b|\bcombined\b|\bcollective\b|\bour team has\b|\bfounded\b|\bin business\b|"
    r"\byears? old\b|\byear-old\b",
    re.IGNORECASE,
)
_TERMINATOR_RE = re.compile(r"[.;\n•·●▪◦]|\s-\s|\s\*\s")
_SOFT_REACH = 100  # chars either side of a soft word, cut at the nearest clause end

_PREFERRED_HEADING_RE = re.compile(
    r"preferred qualifications?|preferred skills?|nice[- ]to[- ]haves?|bonus points?|"
    r"extra credit|bonus qualifications?|\bbonus\b:|\bpreferred\b:|what (?:would|will) make you "
    r"stand out|great to have|good to have|a plus",
    re.IGNORECASE,
)
_REQUIRED_HEADING_RE = re.compile(
    r"minimum qualifications?|basic qualifications?|required qualifications?|requirements?:|"
    r"must[- ]haves?|required skills?|what you(?:'|’)?ll need|what we require",
    re.IGNORECASE,
)
_SECTION_END_RE = re.compile(
    r"responsibilities|what you(?:'|’)?ll do|benefits|what we offer|compensation|salary|"
    r"about (?:us|the (?:company|team))|why join|perks|how to apply",
    re.IGNORECASE,
)
_PREFERRED_SECTION_CAP = 1500

_REQ_WORD_RE = re.compile(
    r"\brequired?\b|\brequires\b|\brequirements?\b|\bmandatory\b|\bmust\b|\bnecessary\b|"
    r"\bessential\b|\bminimum\b|\bbasic qualifications?\b|\bneed(?:s|ed)?\b",
    re.IGNORECASE,
)
_REQ_HEADING_REACH = 600
_CLAUSE_REACH = 160


def _clause(text: str, start: int, end: int, reach: int) -> tuple[int, int]:
    lo = max(0, start - reach)
    hi = min(len(text), end + reach)
    for m in _TERMINATOR_RE.finditer(text, lo, start):
        lo = m.end()
    nxt = _TERMINATOR_RE.search(text, end, hi)
    if nxt:
        hi = nxt.start()
    return lo, hi


def _blank(text: str) -> str:
    """The description with every soft clause and preferred section replaced by spaces."""
    chars = list(text)

    def wipe(lo: int, hi: int) -> None:
        for i in range(lo, hi):
            chars[i] = " "

    for m in _SOFT_RE.finditer(text):
        wipe(*_clause(text, m.start(), m.end(), _SOFT_REACH))
    for m in _PREFERRED_HEADING_RE.finditer(text):
        stop = len(text)
        for end in _SECTION_END_RE.finditer(text, m.end()):
            stop = end.start()
            break
        for req in _REQUIRED_HEADING_RE.finditer(text, m.end()):
            stop = min(stop, req.start())
            break
        wipe(m.start(), min(stop, m.end() + _PREFERRED_SECTION_CAP))
    return "".join(chars)


def _stated_required(text: str, start: int, end: int) -> bool:
    """True when the clause around a match says it is required, or it sits under a required heading."""
    lo, hi = _clause(text, start, end, _CLAUSE_REACH)
    if _REQ_WORD_RE.search(text, lo, hi):
        return True
    window_lo = max(0, start - _REQ_HEADING_REACH)
    last_req = last_other = -1
    for m in _REQUIRED_HEADING_RE.finditer(text, window_lo, start):
        last_req = m.start()
    for m in _SECTION_END_RE.finditer(text, window_lo, start):
        last_other = m.start()
    return last_req > last_other


def _snippet(text: str, start: int, end: int) -> str:
    lo = max(0, start - 40)
    hi = min(len(text), end + 60)
    return " ".join(text[lo:hi].split())


def _first_required(text: str, pattern: re.Pattern[str], veto: re.Pattern[str] | None = None):
    for m in pattern.finditer(text):
        if veto is not None:
            lo, hi = _clause(text, m.start(), m.end(), _CLAUSE_REACH)
            if veto.search(text, lo, hi):
                continue
        if _stated_required(text, m.start(), m.end()):
            return _snippet(text, m.start(), m.end())
    return None


_JD_RE = re.compile(
    r"\bj\.d\.?(?=[\s,;:)]|$)|\bjd\s+(?:degree|from|or)\b|\(jd\)|\bjuris doctor\b|\blaw degree\b|"
    r"\bdegree in law\b|\badmitted to (?:practice|the (?:\w+ )?bar)\b|\bbar (?:admission|membership)\b|"
    r"\bmember of the (?:\w+ )?bar\b|\blicensed (?:attorney|lawyer)\b|\bactive bar\b|"
    r"\bin good standing (?:with|in) (?:the )?(?:\w+ )?bar\b",
    re.IGNORECASE,
)
# A bare "JD" token is also the abbreviation of "job description"; only these forms count for it.
_JD_BARE_RE = re.compile(r"\bjd\b(?=\s+(?:is |are )?(?:required|mandatory))", re.IGNORECASE)
_JD_VETO = re.compile(r"\bor (?:an? )?(?:master|mba|bachelor|undergraduate|equivalent)|"
                      r"\b(?:master|mba|bachelor|b\.?[sa]\.?)['’]?s?\b|\bparalegal\b", re.IGNORECASE)

_PHD_RE = re.compile(r"\bph\.?\s?d\b\.?|\bdoctorate\b|\bdoctoral degree\b|\bdoctor of philosophy\b",
                     re.IGNORECASE)
_PHD_VETO = re.compile(
    r"\b(?:master|masters|mba|bachelor|undergraduate|b\.?[sa]\.?|m\.?[sa]\.?)['’]?s?\b|"
    r"\bor (?:a |an )?(?:advanced|graduate|post-?graduate)\b|\bor (?:an? )?degree\b|"
    r"\bpursuing\b|\bcurrently enrolled\b|\bstudent\b|\bintern(?:ship)?\b|\bcandidates?\b",
    re.IGNORECASE,
)

_MASTERS_RE = re.compile(
    r"\bmaster(?:['’]s|s)(?: degree)?\b|\bmaster degree\b|\bmaster of (?:science|arts|business|finance)\b|"
    r"\bmba\b|\bm\.?s\.? degree\b|\bgraduate degree\b|\badvanced degree\b",
    re.IGNORECASE,
)
_MASTERS_VETO = re.compile(
    r"\bbachelor|\bundergraduate\b|\bb\.?[sa]\.?\b|\bba/bs\b|\bbs/ba\b|\b4[- ]year\b|\bcollege degree\b|"
    r"\bor (?:a |an )?(?:bachelor|undergraduate|college|four[- ]year|degree\b)|\bph\.?\s?d\b|"
    r"\bor\b[^.]{0,50}\b(?:experience|years)\b|\bcurrently enrolled\b|\bpursuing\b|\bstudent\b|\bintern(?:ship)?\b|"
    r"\bcandidates?\b|\bwith (?:at least )?\d+\+? years\b|\byears\b.{0,60}\b(?:in lieu|or)\b",
    re.IGNORECASE,
)

_LICENSE_NAMES = (
    r"cpa|c\.p\.a\.?|certified public accountant|cfa(?: charter(?:holder)?)?|chartered financial analyst|"
    r"cfp|certified financial planner|series\s?(?:7|63|65|66|24|79|82|3|4|6|9|10|22|26|27|28|39|53)"
    r"(?:\s?(?:,|and|&|/)\s?(?:series\s?)?\d{1,2})*|"
    r"professional engineer|p\.?e\.? licen[cs]e|registered nurse|\brn licen[cs]e|"
    r"enrolled agent|certified internal auditor|\bcia\b|certified management accountant|\bcma\b|"
    r"(?:finra|insurance|securities|real estate|nmls|mortgage|state) licen[cs]es?"
)
_LICENSE_RE = re.compile(r"\b(?:" + _LICENSE_NAMES + r")\b", re.IGNORECASE)
_LICENSE_VETO = re.compile(
    r"\b(?:cpa|cfa|cfp) (?:firm|exam|candidate|track|eligible|eligibility|level)|"
    r"\b(?:big (?:4|four)|audit) |\bor\b.{0,40}\b(?:cfa|cpa|cfp|series|licen[cs]e)\b|"
    r"\b(?:cfa|cpa|cfp|series|licen[cs]e)\b.{0,40}\bor\b|\bnot\b|\bwilling\b|\bobtain\b|\bacquire\b|"
    r"\bwithin\b.{0,20}\b(?:days|months|year)\b|\bsponsor|\bwe (?:will |can )?(?:help|support|pay)\b|"
    r"\bdriver|\bdrivers\b",
    re.IGNORECASE,
)


_DOMAIN_TERMS = (
    r"front[- ]office|investment banking|investment bank\b|private equity|buyout|principal[- ]side|"
    r"corporate development|audit(?:ing)?\b|on the (?:investment|underwriting) team|"
    r"on an? acquisitions|(?:investment|underwriting) team|m&a\b|restructuring"
)
_DOMAIN_RE = re.compile(
    r"(?P<n>\d{1,2})\s*\+?\s*(?:[-–]\s*\d{1,2}\s*)?years?[’']?\s*(?:of\s+)?"
    r"(?:(?:relevant|professional|full[- ]time|hands[- ]on|direct|prior)\s+)*(?:experience\s+)?"
    r"(?:(?:in|at|on|as)\s+)?(?:(?:an?|the)\s+)?(?:(?:top[- ]tier|large|leading|major)\s+)?"
    r"(?P<term>" + _DOMAIN_TERMS + r")(?P<after>[^.;]{0,80})",
    re.IGNORECASE,
)


def _domain_years(text: str) -> tuple[str, str] | None:
    """N+ years in a named elite-finance role (front office, banking, PE, audit, ...). A list of
    alternatives after the term ("..., or consulting") means the term is only one way in: skip."""
    for m in _DOMAIN_RE.finditer(text):
        if int(m.group("n")) < DOMAIN_MIN_YEARS or int(m.group("n")) > _MAX_PLAUSIBLE_YEARS:
            continue
        if re.search(r"\bor\b|and/or|/", m.group("after"), re.IGNORECASE):
            continue
        before = text[max(0, m.start() - 60) : m.start()]
        if re.search(r"\bor\b|and/or", before[-25:], re.IGNORECASE):
            continue
        return "domain_years", _snippet(text, m.start(), m.end("term"))
    return None


def _years(text: str, min_years: int) -> tuple[str, str] | None:
    reqs = [r for r in _experience_requirements(text) if min_years <= r[0] <= _MAX_PLAUSIBLE_YEARS]
    if not reqs:
        return None
    years, snippet = max(reqs, key=lambda item: item[0])
    return "years", snippet


def hard_requirement(
    title: str,
    description: str,
    *,
    min_years: int,
    rules: frozenset[str] | set[str] | tuple[str, ...] | None = None,
) -> tuple[str, str] | None:
    """(rule, evidence snippet) for the first hard requirement found, or None.

    `rules` limits which rules run (default: all in RULES). `description` may be raw or already
    lowercased; matching ignores case."""
    enabled = set(RULES if rules is None else rules)
    text = _blank(" ".join((description or "").split()))
    if "jd" in enabled:
        hit = _first_required(text, _JD_RE, _JD_VETO) or _first_required(text, _JD_BARE_RE, _JD_VETO)
        if hit:
            return "jd", hit
    if "phd" in enabled:
        hit = _first_required(text, _PHD_RE, _PHD_VETO)
        if hit:
            return "phd", hit
    if "masters" in enabled:
        hit = _first_required(text, _MASTERS_RE, _MASTERS_VETO)
        if hit:
            return "masters", hit
    if "license" in enabled:
        hit = _first_required(text, _LICENSE_RE, _LICENSE_VETO)
        if hit:
            return "license", hit
    if "domain_years" in enabled:
        hit = _domain_years(text)
        if hit:
            return hit
    if "years" in enabled:
        return _years(text, min_years)
    return None
