"""Resolve scraped fields against stored answers: fill, upload, ask, or skip.

Pure decision logic — no browser interaction. The browser layer (a later
slice) executes each Resolution and reports back a FillOutcome.
"""

from __future__ import annotations

import dataclasses
import difflib
import re
from datetime import date

from jobhunt import eligibility
from jobhunt.apply_assist.keys import (
    CHECKBOX_TITLE_SEP,
    POSTING_DERIVED_KEYS,
    SENSITIVE_KEYS,
    YES_NO_KEYS,
    address_piece,
    allowed_keys_for,
    asks_where_you_are,
    asks_yes_no,
    classify,
    is_legal_name_label,
    label_matches_any_key,
    names_company_site,
    years_at_least,
)
from jobhunt.apply_assist.mapper import fingerprint
from jobhunt.apply_assist.schema import FieldSpec, Resolution

_UNSUPPORTED_KINDS = {"unknown", "hidden"}
_CHOICE_KINDS = {"select", "radio"}

_DIGITS_RE = re.compile(r"\d")
_WHITESPACE_RE = re.compile(r"\s+")

# The reason a choice field is asked about when the stored value fits none of its
# options, or fits more than one. Every rule below uses it.
_NO_OPTION = "stored value matches 0 or >1 options"


# --- Legal acknowledgments (the unattended lane only) ------------------------------
#
# 2026-10-24: "apply anyways" on the legal boxes. A REQUIRED checkbox whose
# label is a legal acknowledgment (a consent, certification, acknowledgment,
# agreement, privacy notice, terms or an arbitration agreement) and that names a legal
# object (or is a bare "I acknowledge") is checked; a box that claims a fact about
# them (a license, citizenship, clearance, years, commute, conviction) is asked. An
# OPTIONAL box (marketing, SMS, talent community, sharing) is left unchecked, and
# so is a required box that is really one of those. This is wording only: the
# scans show most required checkboxes are the options of a group ("Yes", "No",
# "Polish", "He/him"), which carry none of these words and so are asked about.

_ACKNOWLEDGMENT_RE = re.compile(
    r"\b(?:acknowledg\w*|certif\w*|attest\w*|agree\w*|consent\w*|confirm\w*"
    r"|affirm\w*|i\s+understand|understood|privacy\s+(?:notice|policy|statement)"
    r"|terms|arbitration)\b"
)

# The legal object a box must name to be checked (the candidate's Q5 approved legal consents,
# certifications and acknowledgments only): terms, a policy, a notice, an agreement,
# arbitration, a privacy statement, "the information ... is true/accurate/complete",
# or consent to processing / a background check.
_LEGAL_OBJECT_RE = re.compile(
    r"\b(?:terms|policy|policies|notice|notices|agreement|arbitration|privacy"
    r"|next\s+steps|statement|disclaimer|disclosures?|processing|process(?:ed)?|collect\w*|stor(?:e|ed|ing)"
    r"|background\s+(?:check|screen\w*|investigation)|data\s+protection|gdpr|eula)\b"
    r"|\binformation\b[^.?]{0,80}\b(?:true|accurate|complete|correct|truthful)\b"
    r"|\b(?:true|accurate|complete|correct|truthful)\b[^.?]{0,40}\binformation\b"
)

# An option that assents: "Yes", "Yes, I understand", "I acknowledge", "Acknowledge".
_ACK_OPTION_RE = re.compile(
    r"^(?:yes\b|i\s+(?:acknowledge|agree|understand|accept|confirm|consent|have\s+read)"
    r"|acknowledg\w*|agree\w*|accept\w*|confirm\w*|consent\w*|understood)"
)

# A bare acknowledgment ("I acknowledge", "I affirm and agree"): the box names no
# fact about them, only assent to the text the form shows beside it.
_BARE_ACKNOWLEDGMENT_RE = re.compile(
    r"^[\s*\u2731]*(?:i\s+|we\s+)?(?:(?:hereby\s+)?(?:acknowledg\w*|agree\w*|accept\w*"
    r"|affirm\w*|certif\w*|consent\w*|confirm\w*|understand|attest\w*)"
    r"(?:\s*(?:,|and|&)\s*)?\s*)+(?:this|the\s+above|the\s+foregoing|the\s+following)?"
    r"[\s*\u2731.!]*$"
)

# A box that claims a fact about the candidate is theirs to answer, whatever legal word it
# carries. "I have read and understood" and "I have provided accurate information"
# claim nothing about them and are exempt from the "I have" pattern.
_FACT_CLAIM_RE = re.compile(
    r"\bi(?:'m|\s+am)\b"
    r"|\bi(?:'ve|\s+have)\s+(?!read\b|reviewed\b|received\b|been\s+(?:informed|provided"
    r"|given|notified|advised)\b|provided\b|submitted\b|answered\b|opened(?=[\s,]+(?:and\s+)?(?:read|reviewed|understood))|understood\s+(?:the|this|these)\b)"
    r"|\bi\s+(?:hold|live|reside|possess|currently|work|worked|was|had|do|did|can|will|would"
    r"|own|speak|require|need)\b"
    r"|\b(?:i|we)(?:'ve|\s+have)\s+never\b|\bnever\b"
    r"|\blicen[sc]e\w*|\bcitizen\w*|\bclearance\b|\bvisa\b|\bsponsor\w*"
    r"|\bauthori[sz]ed\s+to\s+work\b|\bcommut\w*|\bconvict\w*|\bfelon\w*"
    r"|\bmisdemeanor\w*|\bnon[\s-]*compete\b|\byears?\s+of\b|\b\d+\s*\+?\s*years?\b"
    r"|\bexperience\b|\bresid(?:e|ent|ency)\b|\bsafe\s+sender\b|\bmarked\b"
    # A commitment to relocate, to a work arrangement, to travel or to a schedule is
    # theirs to make, even when it rides in a legal acknowledgment.
    r"|\brelocat\w*|\bon[\s-]?site\b|\bin[\s-]office\b|\bin[\s-]person\b"
    r"|\btravel\w*|\d+\s*%|\bavailab\w*|\b\d+\s*days?\b"
)

# "I certify that I have not knowingly withheld any information ..." certifies that what
# they gave is complete: it claims nothing about them, so it is no fact claim and it is a
# legal object. The phrase is cut out before the fact-claim patterns read the label.
_COMPLETE_INFO_RE = re.compile(
    r"\b(?:have|has)\s+not\s+(?:(?:knowingly|intentionally|willfully|wilfully|deliberately)\s+)?"
    r"(?:withheld|omitted|misrepresented|concealed|falsified|misstated)\b"
    r"(?:\s*(?:,|or|and)\s*(?:withheld|omitted|misrepresented|concealed|falsified|misstated)\b)*"
    r"\s+(?:(?:any|all|the|such|other|material|relevant|requested)\s+){0,3}"
    r"information\b"
)

# "... I am agreeing to be bound by the terms of the Arbitration Agreement" assents to
# the text beside the box; it claims nothing about them. Cut out like the phrase above.
_ASSENT_RE = re.compile(
    r"\bi\s+(?:am\s+agreeing|am\s+consenting|agree|consent)\s+"
    r"(?:to\s+be\s+bound\s+by|to\s+the\s+terms|to\s+abide\s+by)\b"
)


# "... or for immediate discharge if I am employed" is a consequence the certification
# attaches to a misstatement, not a statement about them. Only the conditional "if I am
# employed/hired/..." is cut out; a bare "I am employed" stays a fact claim.
_IF_EMPLOYED_RE = re.compile(
    r"\bif\s+i\s+am\s+(?:later\s+|subsequently\s+|ever\s+|ultimately\s+)?"
    r"(?:employed|hired|selected|engaged|retained|offered\s+(?:employment|a\s+position))\b"
)


def _without_carve_outs(text: str) -> str:
    """`text` with the phrases that sound like a fact claim and are not cut out."""
    text = _IF_EMPLOYED_RE.sub(" ", text)
    return _ASSENT_RE.sub(" ", _COMPLETE_INFO_RE.sub(" ", text))


# A box that withholds the acknowledgment ("I do not agree") is never checked.
_WITHHELD_RE = re.compile(
    r"\b(?:do\s+not|don'?t|does\s+not|doesn'?t|disagree\w*|decline\w*|refuse\w*"
    r"|withhold\w*|revoke\w*|opt(?:ed)?[\s-]*out)\b"
)

# Boxes that are optional in every form seen and that the candidate leaves unchecked even
# when a form marks one required: marketing, SMS, a talent community or future
# openings, and sharing their data with other companies.
_OPTIONAL_KIND_RE = re.compile(
    r"\b(?:marketing|promotion\w*|newsletters?|sms|text\s+messag\w*|whats\s*app"
    r"|talent\s+(?:community|network|pool)|job\s+alerts?"
    r"|future\s+(?:job\s+)?(?:opportunit\w*|roles?|positions?|openings?|employment))\b"
)
_SHARING_RE = re.compile(r"\b(?:shar(?:e|es|ed|ing)|transfer\w*|sell|sold|sale)\b")
_OTHER_PARTIES_RE = re.compile(
    r"\b(?:other|third[\s-]*part\w*|external|partners?|affiliat\w*|outside|employers?"
    r"|compan(?:y|ies)|organi[sz]ations?)\b"
)


def _is_required_acknowledgment(field: FieldSpec) -> bool:
    """Whether `field` is a required checkbox that is a legal acknowledgment the
    unattended lane may check (see above). The caller has already established
    that classify() named no key for it."""
    if not field.required:
        return False
    raw = f"{field.label} {field.placeholder}".replace("\u2019", "'")
    # An Ashby box's label is its own text, the separator, then its group's title; each
    # part can be a bare acknowledgment on its own ("I agree" under "Privacy").
    parts = [" ".join(part.split()).casefold() for part in raw.split(CHECKBOX_TITLE_SEP)]
    text = " ".join(raw.replace(CHECKBOX_TITLE_SEP, " ").split()).casefold()
    if not _ACKNOWLEDGMENT_RE.search(text):
        return False
    if len(parts) > 1:
        # A group's title only supplies the legal object: the box's own text must be
        # the acknowledgment, and must claim no fact ("Male | Acknowledgement",
        # "CFA | Certifications" are options, not assent).
        own = parts[0]
        if not _ACKNOWLEDGMENT_RE.search(own) or _FACT_CLAIM_RE.search(
            _without_carve_outs(own)
        ):
            return False
    if _WITHHELD_RE.search(text) or _OPTIONAL_KIND_RE.search(text):
        return False
    if _SHARING_RE.search(text) and _OTHER_PARTIES_RE.search(text):
        return False
    if _FACT_CLAIM_RE.search(_without_carve_outs(text)):
        return False
    return bool(
        _LEGAL_OBJECT_RE.search(text)
        or _COMPLETE_INFO_RE.search(text)
        or any(_BARE_ACKNOWLEDGMENT_RE.match(part) for part in [text, *parts])
    )


# --- Demographic questions (the unattended lane only) ----------------------------------
#
# 2026-10-24: a REQUIRED gender, race, veteran or disability question is
# answered with its decline option; an OPTIONAL one is left blank; a required one
# with no decline option is theirs to answer. Pronouns stay theirs, and so does every
# demographic checkbox, and so does every one of these questions in the assist lane.
# A decline option is recognized by what it says it declines to do ("Decline to
# self-identify", "I don't wish to answer", "Prefer not to say"); a substantive
# option ("I am not a protected veteran") never is.

_DEMOGRAPHIC_KEYS = frozenset(
    {"gender", "race_ethnicity", "veteran_status", "disability_status"}
)

_DECLINE_VERB = (
    r"(?:answer|disclose|say|identify|self[\s-]*identify|respond|state|provide|specify"
    r"|share|report|reply)"
)
_DECLINE_OPTION_RE = re.compile(
    r"\bdeclin(?:e[sd]?|ing)\s+to\s+" + _DECLINE_VERB
    + r"|\b(?:do\s+not|don't)\s+(?:wish|want|choose)\s+to\s+" + _DECLINE_VERB
    + r"|\b(?:prefer|choose|elect)\s+not\s+to\s+" + _DECLINE_VERB
    + r"|\brather\s+not\s+" + _DECLINE_VERB
)  # fmt: skip


# What a hidden-menu demographic answer is called until fill.py reads the real option.
_DECLINE_PLACEHOLDER = "Decline to self-identify"


def _decline_demographic(field: FieldSpec, key: str) -> Resolution:
    """The unattended lane's answer to a demographic question (see above)."""
    if not field.required:
        return Resolution(
            field, key, "skip", reason="optional demographic question — left blank"
        )
    if field.kind not in _CHOICE_KINDS:
        # A react-select hides its options until it is open, so there is no
        # decline option to point at from here. A one-line box (kind "text") may be
        # one: fill.py opens the menu, reads it, and commits the single option
        # pick_option("decline") names; a box that is no menu fails and is asked.
        # Any other kind (textarea, file, ...) is theirs to answer.
        if field.kind == "text":
            return Resolution(
                field, key, "fill", value=_DECLINE_PLACEHOLDER, option_rule="decline"
            )
        return Resolution(
            field, key, "ask", reason="sensitive — answer by hand (options not visible)"
        )
    declines = [o for o in field.options if _DECLINE_OPTION_RE.search(_plain(o))]
    if len(declines) == 1:
        return Resolution(field, key, "fill", value=declines[0])
    why = "no decline option" if not declines else "several decline options"
    return Resolution(field, key, "ask", reason=f"sensitive — answer by hand ({why})")



# --- Standard factual questions (the unattended lane only) -------------------------------
#
# 2026-10-26: "you can always auto answer questions". These are the company
# questions no stored key covers and that block a form when left to them: whether they
# may be sent SMS/WhatsApp (No), whether they are based in the cities a posting lists,
# whether their bachelor's is finished, and a pure acknowledgment of a privacy notice
# on a list rather than a box. Each reads the label only, runs only for a field
# classify() named nothing for, and a question addressed to an AI or language model
# is never answered (_AI_ADDRESSED_RE), nor a field of any other kind than a list,
# a radio group or a one-line box (a textarea is prose; a checkbox is never ticked
# here). A one-line box is a react-select as often as not, so each answer is a rule
# fill.py applies to the open menu (a box that is no menu takes the plain word).

_AI_ADDRESSED_RE = re.compile(
    r"\b(?:ai|a\.i\.|llm|llms|large\s+language\s+models?|language\s+models?"
    r"|chat\s*gpt|gpt|chatbot|artificial\s+intelligence)\b"
)
_ANSWER_KINDS = frozenset({"select", "radio", "text"})
_COMMS_OPT_IN_RE = re.compile(
    r"\b(?:sms|text\s+messag\w*|whats\s*app|marketing|promotional)\b"
)
_COMMS_ASKED_RE = re.compile(
    r"\b(?:receive|get|contact(?:ed)?|communicat\w*|opt|consent|permission|send|texts?"
    r"|messag\w*)\b"
)
_BASED_IN_RE = re.compile(
    r"\bare\s+you\s+(?:currently\s+|now\s+)?(?:based|located|living|residing)\s+in\s+"
    r"(?P<places>[^?.:;(\n]+)"
)
# "What country do you currently reside in?": which country they live in, answered with
# the stored country. Not a country they would work in, move to or holds a passport of.
_RESIDENCE_COUNTRY_RE = re.compile(
    r"\b(?:what|which)\s+country\s+(?:do\s+you|are\s+you)\s+(?:currently\s+|now\s+"
    r"|presently\s+)?(?:reside|residing|live|living|located|based)\s+in\b"
)
_BASED_IN_CUT_RE = re.compile(
    r"\s+(?:today|right\s+now|at\s+(?:the\s+)?(?:moment|present|this\s+time)"
    r"|as\s+of\s+\w+|currently|now)\s*$"
)
_BACHELOR_GRADUATED_RE = re.compile(
    r"\bhave\s+you\s+(?:already\s+|yet\s+)?(?:graduated|completed|earned|finished"
    r"|received|obtained)\b[^?]*\bbachelor"
)
# A place that is no city: a country, a state, or a catch-all. A list naming one asks
# something wider than the cities, which only the candidate can read.
_NOT_A_CITY_RE = re.compile(
    r"\b(?:united\s+states|u\.?s\.?a?|america|canada|uk|united\s+kingdom|remote"
    r"|anywhere|country|state|region|area|within|commut\w*)\b"
)
_MONTHS = (
    "jan feb mar apr may jun jul aug sep oct nov dec".split()
)
_MONTH_YEAR_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(20\d\d)\b", re.IGNORECASE
)
_YEAR_RE = re.compile(r"\b(20\d\d)\b")
_ACK_EXCLUDED_RE = re.compile(
    r"\b(?:background|arbitration|consumer\s+report|credit|fee|payment|charge|cost"
    r"|waive\w*|release|indemnif\w*|non[\s-]*compete|garden\s+leave|non[\s-]*solicit\w*"
    r"|restrictive\s+covenants?)\b|\bagreements?\b[^.?]{0,80}\brestrict\w*"
)


# Four required consents the candidate answers Yes on a select or radio (2026-10-27): a
# background check, being recorded in an interview, an arbitration agreement and the
# applicant privacy acknowledgement. _ACK_EXCLUDED_RE keeps the checkbox rule off the
# first and third; these name them, and nothing else, so the AI policy and a "consider me
# for future opportunities or portfolio companies" question stay theirs.
_CONSENT_WORD = r"\b(?:consent\w*|authori[sz]\w*|agree\w*|acknowledg\w*)\b"
_NAMED_CONSENT_RES = (
    re.compile(r"\bbackground\s+(?:checks?|screen\w*|investigations?)\b"),
    re.compile(
        r"\b(?:interview|call|conversation|meeting|session)s?\b[^.?]{0,50}\brecord\w*"
        r"|\brecord\w*[^.?]{0,50}\b(?:interview|call|conversation|meeting|session)s?\b"
    ),
    re.compile(r"\barbitration\s+agreements?\b"),
    re.compile(
        r"\b(?:applicant|candidate)\s+privacy\b|\bprivacy\s+(?:notice|policy|statement"
        r"|acknowledg\w*|consent)\b|^privacy[\s*\u2731:.]*$"
    ),
)
# Arbitration and privacy name the document, so the label needs no consent word; a
# background check or a recording is a question until it says consent.
_CONSENT_NEEDS_WORD = (True, True, False, False)
_NOT_A_CONSENT_RE = re.compile(
    r"\b(?:future|portfolio|talent\s+(?:community|network|pool)|newsletters?|marketing"
    r"|do\s+not|don'?t|decline\w*|disagree\w*|withhold\w*)\b"
)


# "As part of our interview process, we may use AI notetakers to transcribe conversations ...":
# the recorded-interview consent (2026-10-27) extended to a transcription / notetaker
# notice. It names an AI, so it is read BEFORE the AI-addressed guard of _autoapply_question.
# The company's notice only: a question about whether HE uses AI tools or notetakers ("Do you
# use AI notetakers?"), a refusal or opt-out wording, and the _NOT_A_CONSENT_RE wordings are
# asked, as is any label claiming a fact about them.
_AI_NOTETAKER_RE = re.compile(
    r"\bnote[\s-]?takers?\b|\bai\s+(?:powered\s+)?(?:transcri\w+|meeting\s+(?:assistants?"
    r"|notes?)|scribes?)\b"
)
_NOTETAKER_CONTEXT_RE = re.compile(r"\b(?:interviews?|conversations?|calls?|meetings?)\b")
_NOTETAKER_NOTICE_RE = re.compile(r"\b(?:we|our|us)\b|" + _CONSENT_WORD)
_NOTETAKER_HIS_USE_RE = re.compile(
    r"\b(?:do|did|have|would|will|are)\s+you\b[^.?]{0,25}\b(?:use|used|using|utili[sz]e\w*)\b"
    r"|\bhow\s+(?:often|do\s+you)\b|\byour\s+(?:use|usage|experience)\b"
)


def _is_ai_notetaker_consent(text: str) -> bool:
    """Whether the label (already plain) is the company's AI notetaker / transcription
    notice for a call or interview, with nothing else attached (see above)."""
    return bool(
        _AI_NOTETAKER_RE.search(text)
        and _NOTETAKER_CONTEXT_RE.search(text)
        and _NOTETAKER_NOTICE_RE.search(text)
        and not _NOTETAKER_HIS_USE_RE.search(text)
        and not _NOT_A_CONSENT_RE.search(text)
        and not _WITHHELD_RE.search(text)
        and not _OPTIONAL_KIND_RE.search(text)
        and not _FACT_CLAIM_RE.search(_without_carve_outs(text))
    )


# Palantir's required "Please share my resume and contact information with external
# Palantir partners for job seeking purposes." (Lever, 2026-10-27). 2026-10-27: Yes.
# Only their own resume, application or contact information shared with the company's partners
# or affiliates for job seeking, recruiting or employment. Privacy, data-processing,
# background-check, terms and marketing consents are other rules' (or theirs), and so is a
# label that does not say what the sharing is for.
_PARTNER_SHARE_RE = re.compile(
    r"\bshar(?:e|es|ing)\b[^.?]{0,60}\b(?:resume|r\u00e9sum\u00e9|cv|application|contact"
    r"|personal\s+information|information)\b[^.?]{0,60}"
    r"\b(?:with|to)\b[^.?]{0,40}\b(?:partners?|affiliat\w*|partner\s+compan(?:y|ies))\b"
)
_PARTNER_PURPOSE_RE = re.compile(
    r"\b(?:job[\s-]*seek\w*|recruit\w*|employment|job\s+opportunit\w*|hiring)\b"
)
_PARTNER_EXCLUDED_RE = re.compile(
    r"\b(?:privacy|data[\s-]*processing|background|terms|marketing|advertis\w*|promotion\w*"
    r"|newsletters?|sell|sold|sale|third[\s-]*part\w*)\b"
)


def _is_partner_share_consent(text: str) -> bool:
    """Whether the label (already plain and clean) asks consent to share their resume or
    contact information with the company's partners for job seeking (see above)."""
    return bool(
        _PARTNER_SHARE_RE.search(text)
        and _PARTNER_PURPOSE_RE.search(text)
        and not _PARTNER_EXCLUDED_RE.search(text)
        and not _NOT_A_CONSENT_RE.search(text)
        and not _WITHHELD_RE.search(text)
        and not _is_ai_notetaker_consent(text)
    )


def _is_named_consent(text: str) -> bool:
    """Whether the label (already plain) is one of the four consents above and no more:
    no fact about them (_FACT_CLAIM_RE) and no future-opportunities, portfolio-company,
    marketing or withholding wording."""
    if _NOT_A_CONSENT_RE.search(text) or _OPTIONAL_KIND_RE.search(text):
        return False
    if _FACT_CLAIM_RE.search(_without_carve_outs(text)):
        return False
    for pattern, needs_word in zip(_NAMED_CONSENT_RES, _CONSENT_NEEDS_WORD):
        if pattern.search(text) and (not needs_word or re.search(_CONSENT_WORD, text)):
            return True
    return False


def _stored_city(answers: dict[str, str]) -> str:
    """The stored city: address_city, else the first part of the stored location
    ("Atlanta, GA - open to relocation" is "Atlanta")."""
    city = str(answers.get("address_city") or "").strip()
    if not city:
        location = str(answers.get("location") or "").strip()
        city = re.split(r"\s*[,\u2014\u2013]\s*|\s+-\s+", location)[0].strip()
    return city


def _places_listed(text: str) -> list[str]:
    """The places a "Are you currently based in X or Y today?" label lists."""
    found = _BASED_IN_RE.search(text)
    if found is None:
        return []
    places = _BASED_IN_CUT_RE.sub("", found.group("places").strip())
    places = re.sub(r"\b(?:the|either|one\s+of)\b", " ", places)
    parts = re.split(r"\s*(?:,|/|&|\bor\b|\band\b)\s*", places)
    return [" ".join(part.split()) for part in parts if part.strip()]


def _stored_in_home_state(answers: dict[str, str]) -> bool:
    """Whether the stored location or state says the configured home state
    (`home_state` in the preferences file, a two-letter code), spelled as code or name."""
    if not _HOME_STATE:
        return False
    spellings = {_HOME_STATE} | {name for name, code in _US_STATES.items() if code == _HOME_STATE}
    stored_place = _plain(f"{answers.get('location') or ''} {answers.get('address_state') or ''}")
    return re.search(r"\b(?:" + "|".join(map(re.escape, sorted(spellings))) + r")\b", stored_place) is not None


def _based_in_answer(field: FieldSpec, text: str, answers: dict[str, str]):
    """Yes when the stored city (or the home metro's principal city, when the stored place is in the home state) is
    one of the cities listed, else No; asked when the list is no list of cities."""
    places = _places_listed(text)
    city = _stored_city(answers)
    if not places or not city or any(
        len(place.split()) > 3
        or _NOT_A_CITY_RE.search(place)
        or (_state_code(place) and place != "new york")  # New York is a city too
        for place in places
    ):
        return None
    mine = {_plain(city)}
    if _stored_in_home_state(answers) and _HOME_METRO_PRINCIPAL:
        mine.add(_HOME_METRO_PRINCIPAL)
    answer = "Yes" if any(_plain(place) in mine for place in places) else "No"
    return _picked(field, "based_in", answer, "first_unique_equal", [answer])


def _graduated(degree: str, today: date) -> bool | None:
    """Whether the stored degree line's graduation date ("... Dec 2026") has passed:
    True, False, or None when the line gives no date (or only this year)."""
    found = _MONTH_YEAR_RE.search(degree)
    if found:
        month = _MONTHS.index(found.group(1).lower()[:3]) + 1
        return (int(found.group(2)), month) < (today.year, today.month)
    years = _YEAR_RE.findall(degree)
    if len(years) == 1 and int(years[0]) != today.year:
        return int(years[0]) < today.year
    return None


def _autoapply_question(field: FieldSpec, answers: dict[str, str]) -> Resolution | None:
    """The answer to one of the standard questions above, or None when the field is
    none of them (the caller then asks about it, as it always did)."""
    if field.kind not in _ANSWER_KINDS:
        return None
    text = _plain(f"{field.label} {field.placeholder}")
    if field.required and _is_ai_notetaker_consent(_clean_text(text)):
        return _picked(field, "consent", "I agree", "acknowledge", [])
    # A yes/no select or radio asking to share their resume with the company's partners
    # (_is_partner_share_consent): the single Yes / "I agree" option. A text box stays asked.
    if (
        field.required
        and field.kind != "text"
        and not _AI_ADDRESSED_RE.search(text)
        and _is_partner_share_consent(_clean_text(text))
    ):
        return _picked(field, "consent", "I agree", "acknowledge", [])
    if not text or _AI_ADDRESSED_RE.search(text):
        return None
    if _COMMS_OPT_IN_RE.search(text) and _COMMS_ASKED_RE.search(text):
        return _picked(field, "sms_optin", "No", "first_unique_equal", ["No", "No, thank you"])
    if _RESIDENCE_COUNTRY_RE.search(text):
        country = str(answers.get("country") or "").strip()
        if not country:
            return None
        return _picked(field, "residence_country", country, "country", [country])
    found = _BASED_IN_RE.search(text)
    if found:
        return _based_in_answer(field, text, answers)
    if _BACHELOR_GRADUATED_RE.search(text):
        done = _graduated(str(answers.get("degree") or ""), date.today())
        if done is None:
            return None
        answer = "Yes" if done else "No"
        return _picked(field, "degree_completed", answer, "first_unique_equal", [answer])
    if field.required and _is_named_consent(text):
        return _picked(field, "consent", "I agree", "acknowledge", [])
    if (
        field.required
        and not _ACK_EXCLUDED_RE.search(text)
        and _is_required_acknowledgment(field)
    ):
        return _picked(field, "acknowledgment", "I acknowledge", "acknowledge", [])
    return None

# --- Factual answers (the unattended lane only) -----------------------------------------
#
# 2026-10-27: finish more forms unattended, but only with facts they gave. Each rule
# below answers a question classify() names nothing for, from one of four sources and no
# other: their standing answers (data/facts.local.md, "Standing application answers"), the
# master resume, the stored profile (application_answers) and the tracker (via
# posting_answers). A rule only reads the label's wording; the answer is a constant that
# tests/test_standing_answers_drift.py ties to the facts file, or a stored/derived value
# that is absent when unknown (and the field is then asked). Never here: a consent
# checkbox, a demographic question, the AI policy, an essay, or a fact no source holds.

def _standing() -> eligibility.StandingAnswers:
    """The candidate's standing answers: the [standing_answers] table of the active
    search preferences (data/search-preferences.local.toml, else the template). Every
    rule reads this at call time and answers only from a value that is set; an unset value
    means the rule does not answer and the field is asked (fail closed)."""
    return eligibility.user_preferences().standing_answers


# Tools a yes/no skill question may name. The answer is Yes only when every tool the label
# names is one the master resume names too (posting_answers builds "resume_skills").
SKILL_VOCAB = (
    "sql", "python", "excel", "pandas", "tableau", "power bi", "salesforce", "hubspot",
    "postgres", "javascript", "typescript", "java", "looker", "snowflake", "dbt", "airflow",
    "spark", "vba", "matlab", "bloomberg", "jira", "gong",
)  # fmt: skip
_MONTH_NAMES = (
    "January February March April May June July August September October November December"
).split()

_QUESTION_START_RE = re.compile(
    r"^(?:are|is|do|does|did|have|has|were|was|will|would|can|could)\b"
)
_YES_NO_HINT_RE = re.compile(r"\b(?:if\s+yes|yes\s*/\s*no|yes\s+or\s+no)\b")
_GENERIC_US_RE = re.compile(
    r"\b(?:(?:for|at|with)\s+us|our\s+(?:company|organi[sz]ation)|(?:this|the)\s+company)\b"
)
# They will be in the office: any days a week, the first week in person, relocating to an
# office ("I want to be inside the office", 2026-10-27). A where/which/how-many question,
# a remote option and a "currently located" compound ("currently located in and able to
# work in NYC": they live in another city) are not this.
_OFFICE_CUE_RE = re.compile(r"\b(?:offices?|on[\s-]?site|in[\s-]person)\b")
_ATTENDANCE_CUE_RE = re.compile(
    r"\b(?:\d+|one|two|three|four|five)\s+days?\b|\bdays?\s+(?:per|a|each|every)\s+week\b"
    r"|\bfirst\s+week\b|\b(?:expected|required|asked)\s+to\s+(?:work|be|come|join|attend)\b"
    r"|\brequires?\s+(?:you\s+)?(?:working|to\s+work|being|presence)\b"
    r"|\bwork(?:ing)?\s+(?:from|out\s+of|in)\s+(?:the|our|one)\b|\bcome\s+(?:in|into)\b"
    r"|\brelocat\w*\b|\bin[\s-]person\b"
)
_COMMIT_CUE_RE = re.compile(
    r"\b(?:able|willing|confirm|commit|comfortable|agree|acknowledg\w*|understand"
    r"|can\s+you|are\s+you|do\s+you|will\s+you|expected|required|requires?|requiring|asked"
    r"|expectation)\b"
)
# Two descriptions inside an office-commitment label that trip _ONSITE_NOT_RE and are not a
# different question (2026-10-27: Melio "... three days per week and includes in-person
# interviews. Are you ...", Talos "... three days in the office per week, with flexibility on
# which days"). Cut out before _ONSITE_NOT_RE reads the label; "Are you available for an
# in-person interview?" and "Which days do you prefer?" still fail it.
_ONSITE_DESCRIPTION_RE = re.compile(
    r"\bincludes?\s+in[\s-]person\s+interviews?\b"
    r"|\bflexib\w+\s+(?:on|in|as\s+to|regarding|around|over)\s+which\s+days?\b"
)
_ONSITE_NOT_RE = re.compile(
    r"\bremote\w*|\bwork\s+from\s+home\b|\bcurrently\s+(?:located|live|living|reside"
    r"|residing|based)\b|\bbased\s+in\b|\bwhich\b|\bwhat\b|\bhow\s+(?:many|often)\b|\bwhen\b"
    r"|\bpreferr?ed\b|\bnot\s+(?:open|willing|able)\b|\bapplying\s+for\b"
    r"|\bdo\s+you\s+(?:require|need)\b|\bassistance\b|\bstipend\b|\bpackage\b|\breimburs\w*"
    r"|\binterview\w*|\bconversation\b|\brecruit\w*|\bmeeting\b|\bcall\b"
)
_TRAVEL_RE = re.compile(r"\btravel\w*\b")
_TRAVEL_PERCENT_RE = re.compile(r"(\d+)\s*(?:%|percent\b)")
_TRAVEL_LIMITS_RE = re.compile(r"\b(?:restrictions?|limitations?|constraints?|concerns?)\b")
_TRAVEL_SHARE_RE = re.compile(
    r"\b(?:what|how\s+much)\b[^.?]{0,40}\b(?:percent\w*|%|share|amount|portion|time)\b"
)
_TRAVEL_WILLING_RE = re.compile(
    r"\b(?:willing|able|comfortable|open|ok|okay|fine|available|can\s+you|ready)\b"
)
_AGREEMENT_RE = re.compile(
    r"\b(?:garden\s+leave|non[\s-]*compete|noncompete|non[\s-]*solicit\w*"
    r"|restrictive\s+covenants?)\b|\bbound\s+by\b[^.?]{0,60}\bagreements?\b"
    r"|\bagreements?\b[^.?]{0,80}\brestrict\w*\s+(?:your|you)\b"
)
_AGREEMENT_HELD_RE = re.compile(
    r"\b(?:subject\s+to|bound|party\s+to|currently|restrict\w*|do\s+you\s+have"
    r"|have\s+you\s+(?:signed|entered)|under\s+(?:any|a))\b"
)
_AGREEMENT_NOT_RE = re.compile(
    r"\b(?:willing\s+to\s+sign|agree\s+to\s+sign|will\s+you\s+sign|would\s+you\s+sign"
    r"|compensation|salary|bonus|equity|deferred|pay)\b"
)
_DISCHARGED_RE = re.compile(
    r"\b(?:discharged|terminated|fired|dismissed|(?:forced|asked|required)\s+to\s+resign)\b"
)
_DISCHARGED_ASKED_RE = re.compile(
    r"\b(?:have\s+you|were\s+you|has\s+your|did\s+you)\b[^.?]{0,40}\b(?:ever|been|employ\w*)\b"
)
_FAMILY_RE = re.compile(
    r"\b(?:family\s+members?|relatives?|spouses?|partners?|significant\s+others?)\b"
)
_FAMILY_EMPLOYED_RE = re.compile(
    r"\b(?:employed|work(?:s|ing)?|employees?)\b[^.?]{0,40}\b(?:at|by|for|with|of)\b"
)
_WORKING_LOCATION_RE = re.compile(
    r"\bworking\s+location\b[^.]{0,60}\bcity\b|\bcity\b[^.?]{0,30}\bstate\b[^.?]{0,40}"
    r"\b(?:work|working)\b|\bfrom\s+where\s+do\s+you\s+intend\s+to\s+work\b"
)
_SPORTS_RE = re.compile(
    r"\b(?:team\s+)?sports?\b[^.?]{0,60}\b(?:play(?:ed)?|particip\w*)\b"
    r"|\b(?:play(?:ed)?|particip\w*)\b[^.?]{0,40}\b(?:team\s+)?sports?\b|\bathletics?\b"
)
_SCHOOL_LEVEL_RE = re.compile(r"\b(?:high\s+school|college|university|school)\b")
_ESIGN_RE = re.compile(
    r"\b(?:sign(?:ing)?|typ(?:e|ing)|enter(?:ing)?|print(?:ing)?|writ(?:e|ing))\b[^.?]{0,60}\b(?:legal\s+)?"
    r"(?:(?:first\s+and\s+last|full)\s+)?name\b"
)
_ESIGN_CERTIFIES_RE = re.compile(
    r"\b(?:certify|attest|acknowledg\w*|(?:have|has)\s+read|read\s+and|understand|accept)\b"
)
_ESIGN_OBJECT_RE = re.compile(
    r"\b(?:terms|polic(?:y|ies)|agreements?|notices?|disclosures?|handbook)\b"
)
_ESIGN_FACT_RE = re.compile(
    r"\b(?:citizen\w*|licen[sc]e\w*|clearance|convict\w*|felon\w*|veteran|disab\w*"
    r"|authori[sz]ed\s+to\s+work|sponsor\w*|visa|relocat\w*|salary|criminal|years?\s+of)\b"
)
_START_TEXT_RE = re.compile(
    r"\b(?:earliest|soonest)\b[^.?]{0,60}\bstart\b|\bwhen\s+(?:can|could|would)\s+you\s+"
    r"(?:start|begin|join)\b|\bearliest\s+(?:start\s+)?date\b"
)
_START_BY_RE = re.compile(r"\b(?:able|available|ready|free)\s+to\s+(?:start|begin|join)\b")
_START_NOT_RE = re.compile(
    r"\b(?:before|prior|earlier|after|later|at\s+least|within|sooner|immediately)\b"
)
_SKILL_ASKED_RE = re.compile(
    r"\b(?:comfortable|proficien\w*|familiar\w*|experience[d]?|skilled|fluent|strong)\b"
)
# "Have you worked at or applied to X?" asks both: the second clause leads with "or", so each
# regex reads either lead. Without it a both-question matched one regex only and was
# answered from that source alone (No from the tracker while the resume held the job).
_PRIOR_LEAD = (
    r"(?:\b(?:have|did)\s+you|\bor)\s+(?:ever\s+|previously\s+|already\s+|before\s+)?"
)
_WORKED_RE = re.compile(
    _PRIOR_LEAD + r"(?:worked|work"
    r"|been\s+employed|interned|been\s+an?\s+(?:employee|intern|contractor))\s+(?:at|for|with)\b"
)
_APPLIED_RE = re.compile(
    _PRIOR_LEAD + r"(?:applied|filed\s+an?\s+application|submitted\s+an?\s+application)\b"
)
# "Do you have relevant internship experience at a proprietary trading firm?": No from the
# master resume (posting_answers "prop_trading"), which names no such firm. Only a yes/no
# question about having the experience: a years count, an interest or a why is not this.
_PROP_TRADING_RE = re.compile(r"\bproprietary\s+trading\b|\bprop(?:rietary)?\s+(?:trading|shop)\b")
_PROP_EXPERIENCE_RE = re.compile(
    r"\bexperience\b|\b(?:worked|interned|been\s+employed)\s+(?:at|for|with|in)\b"
)
_PROP_NOT_RE = re.compile(
    r"\b(?:interested|interest|want|wish|plan|hope|why|how\s+many|how\s+long|years?|describe|explain"
    r"|tell|list|which|what)\b"
)
# "Are you currently located in (NYC)?", also "...located in and able to work in our NYC
# office": No, they live elsewhere (2026-10-27; standing working_location). Only a
# question whose place is New York alone (a list of cities is based_in's), or the "and able
# to work" compound over a label that names it, is this. A question that offers relocating, another city (Atlanta is their own
# metro) or a distance ("near", "within an hour") is not.
_LIVES_IN_RE = re.compile(
    r"\b(?:are\s+you\s+(?:currently\s+|now\s+)?(?:located|living|based|residing)"
    r"|do\s+you\s+(?:currently\s+|now\s+)?(?:live|reside))\s+in\b"
)
_NYC_PLACE_RE = re.compile(r"\b(?:nyc|new\s+york|manhattan|brooklyn)\b")
_LIVES_IN_TAIL_RE = re.compile(
    r"^(?:(?:the\s+)?(?:nyc|new\s+york|manhattan|brooklyn)(?:\s+(?:city|area|metro(?:politan)?"
    r"(?:\s+area)?))?\s*(?:today|now|right\s+now)?\s*(?:[?.!]|$)|and\s+(?:able|available)\b)"
)
_LIVES_IN_NOT_RE = re.compile(
    r"\b(?:relocat\w*|willing|open\s+to|mov(?:e|ing)|commut\w*|within|near|nearby|hour"
    r"|minutes?|miles?|plan\w*|will\s+you|would\s+you)\b"
)
# "Who is your current or most recent employer?" / "What is your current or more recent job
# title?": a text box about their own present role (standing current_employer, current_title).
_RECENT_ROLE_LEAD = (
    r"^(?:(?:who|what)\s+is\s+|what['’]s\s+|please\s+(?:enter|provide|state|list)\s+|enter\s+)?"
    r"your\s+(?:current|present|latest|(?:most|more)\s+recent|last)"
    r"(?:\s+(?:or|and|/)\s+(?:current|present|latest|previous|last|(?:most|more)\s+recent))?\s+"
)
_RECENT_EMPLOYER_RE = re.compile(
    _RECENT_ROLE_LEAD + r"(?:employer|company|organi[sz]ation)(?:\s+name)?\s*(?:[?.:!]|$)"
)
_RECENT_TITLE_RE = re.compile(
    _RECENT_ROLE_LEAD + r"(?:job\s+title|title|position|role)\s*(?:[?.:!]|$)"
)
# "Are you over 18 / at least 18 years of age / 18 or older?": the stored over_18 (the candidate,
# 2026-10-27: Yes, they are 21). Only a question about the age alone, or with the proof-of-age
# rider ("... and have the ability to furnish proof if hired"); any other condition is asked.
_OVER_18_RE = re.compile(
    r"^(?:are|is)\s+you\s+(?:currently\s+)?(?:"
    r"(?:over|older\s+than|at\s+least|above)\s+(?:the\s+age\s+of\s+)?(?:18|eighteen)"
    r"(?:\s+years?(?:\s+of\s+age|\s+old)?)?(?:\s+or\s+(?:older|over|above))?"
    r"|(?:18|eighteen)(?:\s+years?(?:\s+of\s+age|\s+old)?)?\s+or\s+(?:older|over|above)"
    r"|(?:18|eighteen)\s*\+)"
    r"(?:\s+and\s+(?:have|has)\s+the\s+ability\s+to\s+(?:furnish|provide|show)\s+proof"
    r"(?:\s+of\s+age)?(?:\s+if\s+(?:hired|offered|selected))?)?"
    r"\s*(?:[?.!]|$)"
)
_REFERRAL_FOLLOWUP_RE = re.compile(
    r"\bif\b[^.?]{0,40}\breferr\w*\b[^.?]{0,40}\b(?:selected|chose|chosen|checked)\b"
)
_RECONFIRM_SCHOOL_RE = re.compile(
    r"\bre[\s-]?confirm\s+the\s+(?:university|school|college)\b"
)
_RECONFIRM_GRADUATION_RE = re.compile(
    r"\bre[\s-]?confirm\s+your\s+(?:expected\s+)?graduation\s+date\b"
)
_EDU_DATE_FIELD_RE = re.compile(r"^(start|end)\s+date(?:\s*[-:(]?\s*(month|year))?\)?$")
_EDU_SECTION_RE = re.compile(
    r"^(?:school|university|college|degree|discipline|field\s+of\s+study|major)\b"
)
_WORK_SECTION_RE = re.compile(
    r"^(?:company|employer|job\s+title|title|position|role|organi[sz]ation)\b"
)


def _clean_text(text: str) -> str:
    """A label for the rules above: plain, without the required marks."""
    return " ".join(_plain(text).replace("*", " ").replace("✱", " ").split())


def _month_year(value: object) -> tuple[int, int] | None:
    """(year, month) of the first "Mon YYYY" in `value`; None when it holds none."""
    found = _MONTH_YEAR_RE.search(str(value or ""))
    if not found:
        return None
    return int(found.group(2)), _MONTHS.index(found.group(1).lower()[:3]) + 1


def _graduation_month_year(answers: dict[str, str]) -> tuple[int, int] | None:
    """(year, month) of the stored degree line's graduation ("... Dec 2026"); None when
    the line holds no month and year."""
    return _month_year(answers.get("degree"))


def education_date_fields(fields: list[FieldSpec]) -> dict[int, tuple[str, str | None]]:
    """The positions of the "Start date month / End date year" boxes that sit in an
    education block, as {index: (edge, part)}. A date box is education's when the nearest
    school, degree or discipline box before it comes before any company, employer or
    title box; anything else (a job's dates, a lone date) is not."""
    cleaned = [_clean_text(f"{f.label}") for f in fields]
    found: dict[int, tuple[str, str | None]] = {}
    for index, label in enumerate(cleaned):
        match = _EDU_DATE_FIELD_RE.match(label)
        if match is None:
            continue
        for back in range(index - 1, max(-1, index - 9), -1):
            if _EDU_DATE_FIELD_RE.match(cleaned[back]):
                continue
            if _EDU_SECTION_RE.match(cleaned[back]):
                found[index] = (match.group(1), match.group(2))
                break
            if _WORK_SECTION_RE.match(cleaned[back]):
                break
    return found


def _education_date_answer(
    field: FieldSpec, edge: str, part: str | None, answers: dict[str, str]
) -> Resolution | None:
    """The start or end month or year of the degree. The end is read off the stored degree
    line ("... Dec 2026"); the start off the stored education_start ("August 2024", the candidate,
    2026-10-27). A lone date box has no known format, and a missing or unreadable source
    leaves the box asked."""
    if part is None:
        return None
    found = (
        _graduation_month_year(answers)
        if edge == "end"
        else _month_year(answers.get("education_start"))
    )
    if found is None:
        return None
    year, month = found
    if part == "year":
        return _picked(
            field, f"education_{edge}_year", str(year), "first_unique_equal", [str(year)]
        )
    name = _MONTH_NAMES[month - 1]
    return _picked(
        field,
        f"education_{edge}_month",
        name,
        "first_unique_equal",
        [name, name[:3], str(month), f"{month:02d}"],
    )


def _affirm(field: FieldSpec, key: str) -> Resolution:
    """A Yes (or an affirmative statement option, or an assenting one) for a commitment."""
    return _picked(field, key, "Yes", "affirm", ["Yes"])


def _no_answer(field: FieldSpec, key: str, text: str) -> Resolution:
    """"No" for a yes/no question (a list, a radio or a box asking for the word), "None"
    for a box that asks to list or describe the items."""
    if _QUESTION_START_RE.match(text) or _YES_NO_HINT_RE.search(text) or _is_choice(field):
        return _picked(field, key, "No", "yes_no", ["No"])
    return Resolution(field, key, "fill", value="None")


def _names_company_or_us(text: str, company: str | None) -> bool:
    if _GENERIC_US_RE.search(text):
        return True
    # A tracker company name can carry a marker ("🔥 DoorDash"): compare the words only.
    name = " ".join(re.findall(r"\w+", _plain(company or "")))
    words = {name, name.split()[0]} if name else set()
    return any(w and re.search(rf"\b{re.escape(w)}\b", text) for w in words)


def _prior_answer(text: str, answers: dict[str, str]) -> str | None:
    """"Yes" or "No" to "have you worked at / applied to <the company> before": from the
    master resume (worked_here) and the tracker (applied_before), which posting_answers
    reads. A question that asks both is Yes when either is Yes and No when both are No.
    None when the label asks neither or a source is missing (the field is asked)."""
    asked = [
        str(answers.get(key) or "").strip().casefold()
        for key, pattern in (("worked_here", _WORKED_RE), ("applied_before", _APPLIED_RE))
        if pattern.search(text)
    ]
    if not asked or any(a not in ("yes", "no") for a in asked):
        return None
    return "Yes" if "yes" in asked else "No"


def _yes_no_stored(field: FieldSpec, key: str, stored: str) -> Resolution | None:
    word = stored.strip().casefold()
    if word not in ("yes", "no"):
        return None
    answer = word.capitalize()
    return _picked(field, key, answer, "yes_no", [answer])


def _start_availability(field: FieldSpec, text: str) -> Resolution | None:
    """"Are you able to start in September 2026?": No when the month named is before their
    earliest start (standing earliest_start), Yes when it is that month or later. A before/after
    bound or a question with no month and year is asked."""
    asked = _START_BY_RE.search(text)
    if not asked or _START_NOT_RE.search(text) or _standing().earliest_start is None:
        return None
    found = _MONTH_YEAR_RE.search(text[asked.end():])
    if not found:
        return None
    month = _MONTHS.index(found.group(1).lower()[:3]) + 1
    wanted = (int(found.group(2)), month)
    earliest = _standing().earliest_start
    if earliest is None:
        return None
    answer = "Yes" if wanted >= (earliest.year, earliest.month) else "No"
    return _picked(field, "start_availability", answer, "yes_no", [answer])


def _skills_answer(field: FieldSpec, text: str, answers: dict[str, str]) -> Resolution | None:
    """Yes to "are you comfortable with SQL and Python" only when every tool the label
    names is on the master resume (answers["resume_skills"]); an unnamed or missing tool is
    asked, as is a question that names none."""
    if not _SKILL_ASKED_RE.search(text):
        return None
    named = [s for s in SKILL_VOCAB if re.search(rf"\b{re.escape(s)}\b", text)]
    known = {s.strip() for s in str(answers.get("resume_skills") or "").split(",") if s.strip()}
    if not named or any(s not in known for s in named):
        return None
    return _picked(field, "skills", "Yes", "yes_no", ["Yes"])


def _prop_trading_answer(field: FieldSpec, text: str, answers: dict[str, str]) -> Resolution | None:
    """"Do you have internship experience at a proprietary trading firm?": the stored
    prop_trading answer (No while the master resume names no such firm), for a yes/no question
    only. Absent (the resume names one, or cannot be read) or any other shape is asked."""
    if (
        _PROP_TRADING_RE.search(text)
        and _PROP_EXPERIENCE_RE.search(text)
        and _QUESTION_START_RE.match(text)
        and not _PROP_NOT_RE.search(text)
    ):
        return _yes_no_stored(field, "prop_trading", str(answers.get("prop_trading") or ""))
    return None


def _lives_in_nyc_answer(field: FieldSpec, text: str, answers: dict[str, str]) -> Resolution | None:
    """No to "are you currently located in NYC", from the stored city. None when
    the label offers relocation, names no New York place, or asks about another place, and
    when the stored city is itself in New York (then the fact does not hold)."""
    found = _LIVES_IN_RE.search(text)
    if not found or _LIVES_IN_NOT_RE.search(text) or not _NYC_PLACE_RE.search(text):
        return None
    if not _LIVES_IN_TAIL_RE.match(text[found.end():].strip()):
        return None
    city = _plain(_stored_city(answers))
    if not city or _NYC_PLACE_RE.search(city):  # no stored city: the question is theirs
        return None
    return _picked(field, "currently_in_nyc", "No", "yes_no", ["No"])


# "Have you worked at a startup?" / "Have you ever worked at a Series A-C / early-stage
# company?": Yes (2026-10-27: the candidate co-founded a venture and the facts file states "founder
# experience", facts.local.md). Only the plain have-you-worked question about a startup, an
# early-stage or a Series A-D company is this: whatever else the label adds (a count, a
# domain, a name or funding amount, a time window, "currently") leaves a word behind in
# _STARTUP_LEFTOVER_RE and the question is asked.
_STARTUP_WORKED_RE = re.compile(
    r"^(?:have|did)\s+you\s+(?:ever\s+|previously\s+)?(?:worked|work|been\s+employed)\s+"
    r"(?:at|for|in|with)\s+(?P<what>[^?.!]{1,70}?)\s*[?.!]?$"
)
# A Series mention alone is not enough: the candidate's venture never raised a round, so only "startup" or
# "early-stage" (alone or beside a Series) makes the question this one.
_STARTUP_TERM_RE = re.compile(r"\bstart[\s-]?ups?\b|\bearly[\s-]stage\b")
_STARTUP_LEFTOVER_RE = re.compile(
    r"\b(?:an?|any|or|and|to|early|seed|growth|stage|series|b|c|d|start|ups?|startups?"
    r"|compan(?:y|ies)|firms?|before|previously|venture|backed|funded)\b|[-\u2013/,&]"
)


def _startup_answer(field: FieldSpec, text: str) -> Resolution | None:
    # Answers Yes only when the candidate's standing founder_experience is true; unset or
    # false is not an answer, the field is asked.
    if _standing().founder_experience is not True:
        return None
    found = _STARTUP_WORKED_RE.match(text)
    if found is None:
        return None
    what = found.group("what")
    if not _STARTUP_TERM_RE.search(what) or _STARTUP_LEFTOVER_RE.sub(" ", what).strip():
        return None
    return _affirm(field, "startup_experience")


# "If you have a college degree, what institution(s) did you graduate from?" (a text box) and
# "Which university are you currently attending or did you last attend?" (a list): the stored school
# (standing school, education_text). They have not graduated, so the graduate-from question is
# answered only when it is conditional on holding a degree, and then with the "expected" text;
# a list is answered only with its one option for that school, never "Other" (pick_option
# "school"). A bare "which school did you graduate from?" is asked.
_SCHOOL_NOUN = r"(?:institutions?|schools?|colleges?|universit(?:y|ies))(?:\(s\))?"
_GRADUATED_FROM_RE = re.compile(
    r"^if\s+you\s+(?:have|hold|earned|received)\s+an?\s+(?:college|university|bachelor'?s?"
    r"|undergraduate|four[\s-]year)\s+degree\s*[,:]?\s*(?:what|which)\s+" + _SCHOOL_NOUN
    + r"\s+did\s+you\s+graduate\s+from\s*[?.!]?$"
)
_ATTENDING_RE = re.compile(
    r"^(?:which|what)\s+" + _SCHOOL_NOUN + r"\s+(?:are\s+you|do\s+you)\s+currently\s+"
    r"attend\w*\b"
)


def _school_option_re() -> re.Pattern[str] | None:
    """Matches a list option naming the standing school or one of its school_aliases
    (whole words, case-folded); None when no school is set."""
    standing = _standing()
    names = [n for n in (standing.school, *standing.school_aliases) if n.strip()]
    if not names:
        return None
    words = [r"\s+".join(re.escape(w) for w in _plain(n).split()) for n in names]
    return re.compile(r"\b(?:" + "|".join(sorted(words, key=len, reverse=True)) + r")\b")


def _education_school_answer(field: FieldSpec, text: str) -> Resolution | None:
    standing = _standing()
    if field.kind == "text" and _GRADUATED_FROM_RE.match(text):
        if not standing.education_text:
            return None
        return Resolution(field, "school", "fill", value=standing.education_text)
    if _ATTENDING_RE.match(text):
        if not standing.school:
            return None
        return _picked(field, "school", standing.school, "school", [])
    return None


# "Are you a resident of California?": the stored state (address_state) against the state asked.
# Only a single state after "resident of" is this: a country, two states or a longer clause is
# asked, as are "Will you relocate to California?" and "California residents: read this".
_STATE_RESIDENT_RE = re.compile(
    r"^(?:are|is)\s+you\s+(?:currently\s+)?(?:an?\s+)?(?:legal\s+)?resident\s+of\s+"
    r"(?:the\s+state\s+of\s+)?(?P<state>[a-z][a-z .]*?)\s*[?.!]?$"
)


def _state_resident_answer(
    field: FieldSpec, text: str, answers: dict[str, str]
) -> Resolution | None:
    found = _STATE_RESIDENT_RE.match(text)
    if found is None:
        return None
    asked = _state_code(found.group("state"))
    stored = _state_code(str(answers.get("address_state") or ""))
    if asked is None or stored is None:
        return None
    answer = "Yes" if asked == stored else "No"
    return _picked(field, "state_resident", answer, "yes_no", [answer])


def _esignature_answer(field: FieldSpec, answers: dict[str, str]) -> Resolution | None:
    """A box that asks them to type their name to certify they have read a policy, terms or an
    agreement is their stored full legal name (2026-10-27: allowed). It is tried before
    the acknowledgment rule, which would put "I acknowledge" in a box that wants a name.
    A name that certifies a fact (authorization to work, a licence, a conviction) is not it."""
    if field.kind not in ("text", "textarea"):
        return None
    text = _clean_text(f"{field.label} {field.placeholder}")
    if not text or _AI_ADDRESSED_RE.search(text):
        return None
    if not (
        _ESIGN_RE.search(text)
        and _ESIGN_CERTIFIES_RE.search(text)
        and _ESIGN_OBJECT_RE.search(text)
        and not _ESIGN_FACT_RE.search(text)
    ):
        return None
    name = str(answers.get("full_name") or "").strip()
    return Resolution(field, "e_signature", "fill", value=name) if name else None


def _factual_question(
    field: FieldSpec,
    answers: dict[str, str],
    company: str | None,
    education: tuple[str, str | None] | None = None,
) -> Resolution | None:
    """The answer to a question of the kinds above, or None when the field is none of them
    (the caller then asks about it, as it always did)."""
    if field.kind not in _ANSWER_KINDS and field.kind != "textarea":
        return None
    text = _clean_text(f"{field.label} {field.placeholder}")
    if not text or _AI_ADDRESSED_RE.search(text):
        return None
    if education is not None:
        return _education_date_answer(field, education[0], education[1], answers)
    # Two prose-looking boxes take a one-line fact; a textarea is nothing else here.
    if _SPORTS_RE.search(text) and _SCHOOL_LEVEL_RE.search(text) and _standing().sports:
        return Resolution(field, "sports", "fill", value=_standing().sports)
    if field.kind == "textarea":
        return None
    if _REFERRAL_FOLLOWUP_RE.search(text):
        return Resolution(
            field,
            "referral_name",
            "skip",
            reason="follow-up to an Employee Referral he never chooses: left blank",
        )
    if _RECONFIRM_SCHOOL_RE.search(text):
        school = str(answers.get("school") or "").strip()
        return _picked(field, "school", school, "first_unique_equal", [school]) if school else None
    if _RECONFIRM_GRADUATION_RE.search(text):
        graduation = _graduation_month_year(answers)
        if graduation is None:
            return None
        year, month = graduation
        name = _MONTH_NAMES[month - 1]
        return _picked(
            field,
            "graduation_date",
            f"{name} {year}",
            "first_unique_equal",
            [f"{name} {year}", f"{name[:3]} {year}", f"{month}/{year}", f"{month:02d}/{year}"],
        )
    if (
        _OFFICE_CUE_RE.search(text)
        and _ATTENDANCE_CUE_RE.search(text)
        and _COMMIT_CUE_RE.search(text)
        and not _ONSITE_NOT_RE.search(_ONSITE_DESCRIPTION_RE.sub(" ", text))
    ):
        return _affirm(field, "office_commitment")
    resolution = _startup_answer(field, text) or _education_school_answer(field, text)
    if resolution is not None:
        return resolution
    resolution = _state_resident_answer(field, text, answers)
    if resolution is not None:
        return resolution
    resolution = _lives_in_nyc_answer(field, text, answers)
    if resolution is not None:
        return resolution
    travel_max = _standing().travel_percent
    if travel_max is not None and _TRAVEL_RE.search(text):
        percent = _TRAVEL_PERCENT_RE.search(text)
        if _TRAVEL_LIMITS_RE.search(text) and field.kind == "text":
            return Resolution(
                field,
                "travel",
                "fill",
                value=f"I can travel up to {travel_max}% of the time.",
            )
        if _TRAVEL_SHARE_RE.search(text) and field.kind == "text":
            return Resolution(field, "travel", "fill", value=f"{travel_max}%")
        if _TRAVEL_WILLING_RE.search(text) and (
            percent is None or int(percent.group(1)) <= travel_max
        ):
            return _affirm(field, "travel")
    if _AGREEMENT_RE.search(text) and _AGREEMENT_HELD_RE.search(text) and not _AGREEMENT_NOT_RE.search(text):
        return _no_answer(field, "agreements", text)
    if _DISCHARGED_RE.search(text) and _DISCHARGED_ASKED_RE.search(text):
        return _no_answer(field, "discharged", text)
    if (
        _FAMILY_RE.search(text)
        and _FAMILY_EMPLOYED_RE.search(text)
        and _names_company_or_us(text, company)
    ):
        return _no_answer(field, "family_at_company", text)
    standing = _standing()
    if field.kind == "text" and standing.working_location and _WORKING_LOCATION_RE.search(text):
        return Resolution(field, "working_location", "fill", value=standing.working_location)
    if field.kind == "text" and standing.earliest_start and _START_TEXT_RE.search(text):
        return Resolution(field, "earliest_start", "fill", value=standing.earliest_start_text)
    resolution = _start_availability(field, text)
    if resolution is not None:
        return resolution
    if _names_company_or_us(text, company):
        prior = _prior_answer(text, answers)
        if prior is not None:
            return _yes_no_stored(field, "prior_application", prior)
    if field.kind == "text" and standing.current_employer and _RECENT_EMPLOYER_RE.match(text):
        return Resolution(field, "recent_employer", "fill", value=standing.current_employer)
    if field.kind == "text" and standing.current_title and _RECENT_TITLE_RE.match(text):
        return Resolution(field, "recent_title", "fill", value=standing.current_title)
    if _OVER_18_RE.match(text):
        return _yes_no_stored(field, "over_18", str(answers.get("over_18") or ""))
    resolution = _prop_trading_answer(field, text, answers)
    if resolution is not None:
        return resolution
    return _skills_answer(field, text, answers)


# The pieces of a postal address that mean a lone "City" belongs to one.
_ADDRESS_COMPANIONS = frozenset({"street", "whole", "state", "zip"})


# --- A required office-location multi-select (the unattended lane only) ---------------
#
# "Which office will you work from?" is a group of checkboxes, one per office, and the
# form wants at least one ticked. Every other checkbox stays a question; this group's
# one box is ticked when all of these hold:
#   - the group is an office-location list: an Ashby group whose title (the part after
#     CHECKBOX_TITLE_SEP) says office, location or city, or, where a box carries no
#     title, a run of two or more adjacent checkboxes that are ALL "City, ST" places;
#   - the candidate's city order (OFFICE_CITY_ORDER, from the preferences file; the
#     owner's was New York, the Atlanta metro, Chicago, San Francisco) ranks a box of the group, and the highest-ranked city has
#     exactly one box in the group and it is required. The home metro ranks only
#     where the stored place is in the home state (so it is the candidate's own metro).
# A group none of whose boxes ranks, or whose best city has two boxes, is left to the
# human: nothing guesses which office they would want, and a place that is only near a
# city ("Atlanta (Hybrid)", "Remote - Southeast", "Remote - New York") is no place
# named. The metro is finai.CITY_TIERS' tier-2 Atlanta set. Every other box of the
# group, and every demographic or consent box, keeps its "ask".
# The order, the home metro's cities and the home state come from the preferences file
# (office_city_order, home_metro, home_state); an order entry ending in " metro" stands
# for the home metro, and the first home_metro city is its principal city.
_PREFS = eligibility.user_preferences()
OFFICE_CITY_ORDER = tuple(_PREFS.office_city_order)
_HOME_METRO = frozenset(_PREFS.home_metro)
_HOME_METRO_PRINCIPAL = _PREFS.home_metro[0] if _PREFS.home_metro else ""
_HOME_STATE = _PREFS.home_state
# Spellings and own state of cities an order entry may name; any other city matches by
# its plain name in any state.
_CITY_SPELLINGS: dict[str, tuple[frozenset[str], str | None]] = {
    "new york": (frozenset({"new york", "new york city", "nyc"}), "ny"),
    "chicago": (frozenset({"chicago"}), "il"),
    "san francisco": (frozenset({"san francisco", "sf"}), "ca"),
}
_OFFICE_TITLE_RE = re.compile(r"\b(?:offices?|locations?|cit(?:y|ies))\b")
# A title that asks where they will NOT work, which offices to leave out, or where they live
# now ("Which of the following cities are you currently located in?"): that is a
# statement about them, not an office they picks.
_OFFICE_TITLE_NEGATED_RE = re.compile(
    r"\b(?:not|unwilling|exclud\w*|avoid\w*|except|other\s+than|rather\s+not|currently"
    r"|current|live|living|reside|residing|based|home|located)\b"
)
_COUNTRY_TAILS = frozenset({"united states", "united states of america", "usa", "us", "u.s."})


def _place_state(option: str) -> tuple[str, str | None] | None:
    """The (city, state code) of an option spelled "City, ST" ("Atlanta, Georgia, United
    States" too), with None for the state of a bare "City"; None when the option is
    anything else."""
    parts = [" ".join(part.split()) for part in option.split(",")]
    if parts and _plain(parts[-1]) in _COUNTRY_TAILS:
        parts = parts[:-1]
    if not parts or not parts[0] or len(parts) > 2:
        return None
    if len(parts) == 1:
        return _plain(parts[0]), None
    code = _state_code(parts[1])
    return (_plain(parts[0]), code) if code else None


def _in_home_metro(option: str) -> bool:
    place = _place_state(option)
    if place is None or place[0] not in _HOME_METRO:
        return False
    city, state = place
    # Duluth, MN is not the (Atlanta) metro: the Georgia suburb needs its state spelled.
    return state == _HOME_STATE or (state is None and city != "duluth")


def _office_rank(option: str, answers: dict[str, str]) -> int | None:
    """The position of `option`'s city in OFFICE_CITY_ORDER (0 is first), or None when
    the option is no city of the order. "New York, NY", "New York City", "NYC" and
    "New York, New York" are New York; a state other than the city's own is no match."""
    place = _place_state(option)
    if place is None:
        return None
    city, state = place
    for rank, entry in enumerate(OFFICE_CITY_ORDER):
        name = _plain(entry)
        if name.endswith(" metro"):
            if _in_home_metro(option) and _stored_in_home_state(answers):
                return rank
            continue
        names, own_state = _CITY_SPELLINGS.get(name, (frozenset({name}), None))
        if city in names and (own_state is None or state in (None, own_state)):
            return rank
    return None


def _checkbox_groups(fields: list[FieldSpec]) -> dict[tuple[str, str | int], list[int]]:
    """The positions of the checkboxes of `fields` by group: an Ashby box with a title
    (CHECKBOX_TITLE_SEP) joins the group of that title, and a run of adjacent boxes with
    none is one group."""
    groups: dict[tuple[str, str | int], list[int]] = {}
    run = 0
    in_run = False
    for index, f in enumerate(fields):
        if f.kind != "checkbox":
            in_run = False
            continue
        _, separator, title = f.label.partition(CHECKBOX_TITLE_SEP)
        if separator:
            groups.setdefault(("title", _plain(title)), []).append(index)
            in_run = False
            continue
        if not in_run:
            run += 1
        in_run = True
        groups.setdefault(("run", run), []).append(index)
    return groups


# Method Financial's required location checkbox group (2026-10-27): "Currently located in one
# of the listed cities on the job description", "Open to relocating to a city on the job
# description", "Only considering remote / won't consider relocation". The one box ticked is
# the relocation one (facts.local.md: a home city, "Open to relocation, including SF and
# NYC"). The group is told by its two signature options, whatever title or prefix the labels
# carry (a title the option already contains is not split off); a group without both, a
# relocation option that is negated, or two relocation options ticks nothing.
_RELOCATE_OPTION_RE = re.compile(
    r"\bopen\s+to\s+relocat\w+\s+to\s+(?:a|one\s+of\s+the|any)\s+(?:listed\s+)?"
    r"cit(?:y|ies)\s+(?:on|in|listed\s+(?:on|in))\s+the\s+(?:job\s+description|posting|listing)\b"
)
_LOCATED_LISTED_OPTION_RE = re.compile(
    r"\bcurrently\s+located\s+in\s+(?:one\s+of\s+)?(?:the\s+)?(?:listed\s+)?cit(?:y|ies)\b"
)


def _relocation_picks(fields: list[FieldSpec]) -> frozenset[int]:
    """The position of the one "open to relocating to a city on the job description" box of
    each required location group that also holds a "currently located in a listed city" box."""
    picks: set[int] = set()
    for indexes in _checkbox_groups(fields).values():
        if len(indexes) < 2:
            continue
        texts = {
            i: _plain(fields[i].label.replace(CHECKBOX_TITLE_SEP, " ")) for i in indexes
        }
        wanted = [
            i
            for i, t in texts.items()
            if _RELOCATE_OPTION_RE.search(t)
            and not re.search(r"\bnot\s+open\b|\bnot\s+willing\b", t)
        ]
        located = [i for i, t in texts.items() if _LOCATED_LISTED_OPTION_RE.search(t)]
        if len(wanted) == 1 and located and fields[wanted[0]].required:
            picks.add(wanted[0])
    return frozenset(picks)


# Palantir's required "Language Skill(s) (Check all that apply)" group (Lever, 2026-10-27),
# one box per language ("English (ENG)", "Spanish (SPA)", ..., "Choose not to disclose",
# "Other"). 2026-10-27: English, and a little Spanish. So the group takes English and
# Spanish; when its question asks for fluency or a proficiency level (a little Spanish is
# not fluent) it takes English alone. A group with no English box, or with no other
# language beside it, ticks nothing, and neither "Choose not to disclose", "Other" nor any
# other language is ever ticked. The group's question is its Ashby title, the text a label
# carries before "check all that apply", or the field just above an untitled run.
_ENGLISH_OPTION_RE = re.compile(r"english\b")
_SPANISH_OPTION_RE = re.compile(r"spanish\b")
_LANGUAGE_NON_OPTION_RE = re.compile(
    r"^other\b|\bnot\s+(?:to\s+)?(?:disclose|say|answer)\b|\bprefer\s+not\b|\bdecline\w*"
)
_LANGUAGE_LEVEL_RE = re.compile(
    r"\b(?:fluent|fluency|native\w*|professional|proficien\w*|business[\s-]?level|bilingual)\b"
)
_CHECK_ALL_RE = re.compile(r"check\s+all\s+that\s+apply\s*\)?")


def _language_picks(fields: list[FieldSpec]) -> frozenset[int]:
    """The positions of the English box (and the Spanish box, unless the question asks for
    fluency or a level) of each required language checkbox group (see above)."""
    picks: set[int] = set()
    for indexes in _checkbox_groups(fields).values():
        if len(indexes) < 2 or not any(fields[i].required for i in indexes):
            continue
        options: dict[int, str] = {}
        question: list[str] = []
        for i in indexes:
            own, _, title = fields[i].label.partition(CHECKBOX_TITLE_SEP)
            text = _clean_text(own)
            *lead, last = _CHECK_ALL_RE.split(text)
            options[i] = " ".join(last.split())
            question += [title, *lead]
        before = indexes[0] - 1
        if before >= 0 and fields[before].kind != "checkbox":
            question.append(f"{fields[before].label} {fields[before].placeholder}")
        english = [i for i, t in options.items() if _ENGLISH_OPTION_RE.match(t)]
        others = [
            i
            for i, t in options.items()
            if i not in english and t and not _LANGUAGE_NON_OPTION_RE.search(t)
        ]
        if len(english) != 1 or not others:
            continue
        picks.add(english[0])
        if not _LANGUAGE_LEVEL_RE.search(_plain(" ".join(question))):
            picks.update(i for i in others if _SPANISH_OPTION_RE.match(options[i]))
    return frozenset(picks)


def _office_location_picks(
    fields: list[FieldSpec], answers: dict[str, str]
) -> frozenset[int]:
    """The positions in `fields` of the one box to tick in each required office-location
    multi-select: the only box of the group's highest-ranked city (see above)."""
    groups = _checkbox_groups(fields)

    picks: set[int] = set()
    for (kind, name), indexes in groups.items():
        options = [fields[i].label.partition(CHECKBOX_TITLE_SEP)[0] for i in indexes]
        if len(indexes) < 2:
            continue
        if kind == "title":
            title = str(name)
            if (
                not _OFFICE_TITLE_RE.search(title)
                or _OFFICE_TITLE_NEGATED_RE.search(title)
                or _AI_ADDRESSED_RE.search(title)
            ):
                continue
        elif not all(
            (place := _place_state(option)) is not None and place[1] is not None
            for option in options
        ):
            continue  # an untitled run is an office list only when every box is "City, ST"
        ranked = [
            (rank, i)
            for i, option in zip(indexes, options)
            if (rank := _office_rank(option, answers)) is not None
        ]
        if not ranked:
            continue
        best = min(rank for rank, _ in ranked)
        top = [i for rank, i in ranked if rank == best]
        if len(top) == 1 and fields[top[0]].required:
            picks.add(top[0])
    return frozenset(picks)


def resolve(
    fields: list[FieldSpec],
    answers: dict[str, str],
    resume_path: str | None,
    key_overrides: dict[str, str] | None = None,
    company: str | None = None,
    *,
    autoapply: bool = False,
) -> list[Resolution]:
    """Decide what to do with each field, given the stored answer map.

    `key_overrides` maps a field's fingerprint (jobhunt.apply_assist.mapper)
    to a semantic key named by the LLM mapper for a field classify() missed.
    It only ever fills in for classify()'s None — a classify() hit is never
    second-guessed — and only when that None means the label named nothing:
    a label that matches some key pattern and was refused (a conflict, wording
    that points the other way) keeps its refusal, and the override is dropped.
    An override is also held to the same label shape classify() is
    (keys.allowed_keys_for): a key the shape rules out is dropped. A yes/no key
    (keys.YES_NO_KEYS) is always dropped, because only classify() settles which
    way such a question points, and so is a key decided from the posting
    (keys.POSTING_DERIVED_KEYS). In each case the field is asked about instead.
    `company` is the company the posting is for; classify() uses it to name a
    how-heard question that spells the company out (keys.classify).
    `autoapply` is True only for the unattended lane (jobhunt.autoapply). It adds
    the two things that lane may do and the assist lane may not: answer a
    required demographic question with its decline option (skipping an optional
    one), and emit the action "check" (value None) for a required checkbox that is
    a legal acknowledgment. With the default False every demographic question and
    every checkbox is asked about, exactly as before.
    A key the posting decides (keys.POSTING_DERIVED_KEYS) is read from `answers`
    like any other, so the caller merges posting_answers.posting_answers into it.
    Pure: no DB, no network."""
    # Which pieces of a postal address the form asks for. A lone "City" is the
    # city of that address when a street, state or zip box sits beside it, and
    # a location question otherwise.
    address_pieces = frozenset(
        piece
        for f in fields
        if f.kind not in _UNSUPPORTED_KINDS and (piece := address_piece(f)) is not None
    )
    office_picks = _office_location_picks(fields, answers) if autoapply else frozenset()
    relocation_picks = _relocation_picks(fields) if autoapply else frozenset()
    language_picks = _language_picks(fields) if autoapply else frozenset()
    education = education_date_fields(fields) if autoapply else {}
    resolutions: list[Resolution] = []
    for index, f in enumerate(fields):
        resolutions.append(
            _resolve_one(
                f,
                answers,
                resume_path,
                key_overrides,
                company,
                autoapply=autoapply,
                address_pieces=address_pieces,
                office_pick=index in office_picks,
                relocate_pick=index in relocation_picks,
                language_pick=index in language_picks,
                education=education.get(index),
            )
        )
    if autoapply:
        # 2026-10-27: Lever and Ashby put `required` on every box of a multi-select,
        # so once one box of a group is ticked (an office, relocation or language pick) its
        # other boxes would still resolve to "ask" and submit._digest_blockers would block
        # the form on each. Their required flag is cleared and they are skipped instead; the
        # live-page gate (submit.py group_checked) still checks that the group has a box ticked.
        picks = office_picks | relocation_picks | language_picks
        for indexes in _checkbox_groups(fields).values():
            if not picks.intersection(indexes):
                continue
            for i in indexes:
                if i not in picks and resolutions[i].action == "ask":
                    resolutions[i] = Resolution(
                        dataclasses.replace(fields[i], required=False),
                        None,
                        "skip",
                        reason="left unticked: another box of this required group is ticked",
                    )
    return resolutions


def _resolve_one(
    field: FieldSpec,
    answers: dict[str, str],
    resume_path: str | None,
    key_overrides: dict[str, str] | None = None,
    company: str | None = None,
    *,
    autoapply: bool = False,
    address_pieces: frozenset[str] = frozenset(),
    office_pick: bool = False,
    relocate_pick: bool = False,
    language_pick: bool = False,
    education: tuple[str, str | None] | None = None,
) -> Resolution:
    if field.kind in _UNSUPPORTED_KINDS:
        return Resolution(field, None, "skip", reason="unsupported field kind")

    classified = classify(field, company)
    key = classified
    # classify() leaves "City" with the location keys: on its own it is a place
    # question. Beside a street, state or zip box it is the city of an address.
    if (
        key in (None, "location")
        and address_pieces & _ADDRESS_COMPANIONS
        and address_piece(field) in ("city", "bare_city")
    ):
        key = "address_city"
    overridden = False
    if key is None and key_overrides:
        key = key_overrides.get(fingerprint(field))
        overridden = key is not None

    # Sensitive/checkbox/file/cover_letter guards run AFTER the override is
    # applied, so a mapper override can never route around them — the
    # mapper only names a field, resolve() still decides what happens to it.
    if key in SENSITIVE_KEYS:
        # The standing pronouns are he/him (2026-10-27): a required pronouns question
        # classify() named is answered, a mapper's pick never is.
        if autoapply and not overridden and key == "pronouns" and field.required:
            resolution = _pronouns_answer(field)
            if resolution is not None:
                return resolution
        # The unattended lane declines a demographic question for the candidate (see
        # above), but only one classify() named: a mapper's pick never does.
        if (
            autoapply
            and not overridden
            and key in _DEMOGRAPHIC_KEYS
            and field.kind != "checkbox"
        ):
            return _decline_demographic(field, key)
        return Resolution(field, key, "ask", reason="sensitive — answer by hand")

    if field.kind == "checkbox":
        # Only the unattended lane ever checks a box, only one classify() named
        # nothing for (a mapper pick never widens this), and only a required
        # legal acknowledgment (_is_required_acknowledgment).
        if (
            autoapply
            and classified is None
            and not _AI_ADDRESSED_RE.search(_plain(f"{field.label} {field.placeholder}"))
            and _is_required_acknowledgment(field)
        ):
            return Resolution(
                field, None, "check", reason="required legal acknowledgment"
            )
        # The one box of a required office-location multi-select that ranks highest in
        # their city order (_office_location_picks); resolve() only passes it for the
        # unattended lane.
        if autoapply and office_pick and classified is None:
            return Resolution(
                field, None, "check", reason="the highest-ranked office in his city order"
            )
        # The relocation box of a required location group (_relocation_picks); classify()
        # may name it willing_to_relocate, which is the same fact.
        if autoapply and relocate_pick and classified in (None, "willing_to_relocate"):
            return Resolution(
                field,
                "willing_to_relocate",
                "check",
                reason="open to relocating to a listed city (standing answer)",
            )
        # The English (and Spanish) box of a required language group (_language_picks).
        if autoapply and language_pick and classified is None:
            return Resolution(
                field,
                None,
                "check",
                reason="English and a little Spanish (standing answer)",
            )
        # Palantir's "share my resume and contact information with external partners" box
        # (_is_partner_share_consent): Yes.
        if (
            autoapply
            and classified is None
            and field.required
            and _is_partner_share_consent(_clean_text(f"{field.label} {field.placeholder}"))
        ):
            return Resolution(
                field,
                None,
                "check",
                reason="consent to share his resume with the company's partners (standing answer)",
            )
        return Resolution(field, key, "ask", reason="checkbox — never auto-checked")

    # What limits classify() limits the mapper too. The mapper only sees
    # fields classify() left unnamed, and a field is often unnamed precisely
    # because classify() refused it: its label is a question the stored answers
    # cannot answer (an essay prompt, a yes/no about something else), or it
    # matches two keys at once, or its wording points the other way ("Can you
    # work in the US without sponsorship?"). An LLM that picks a key for one of
    # those gets it filled with a stored answer to a different question. So an
    # override is accepted only when the label matches no key pattern at all
    # (classify() found nothing, it did not refuse), and then only for a key
    # the label shape allows.
    #
    # A yes/no key (work authorization, sponsorship, relocation) is never taken
    # from the mapper, whatever the shape allows. Only classify() settles which
    # way such a question points (keys._settle_polarity), and a pick skips that
    # step. "Do you have a valid visa?" classifies as None, yet its shape
    # allows every yes/no key: a work_authorization pick would type "Yes" for
    # someone who needs no visa. So that question goes to the user. A key the
    # posting decides (a work location, whether they have been here before) is
    # never taken from the mapper either: it names what the posting says, and
    # whether the question asks for it is classify()'s call alone.
    #
    # Every refusal is answered as a plain miss, with the key dropped, so a
    # textarea stays eligible for answer tailoring (fill.py gates that on this
    # exact reason).
    if overridden:
        allowed = allowed_keys_for(field)
        if (
            key in YES_NO_KEYS
            or key in POSTING_DERIVED_KEYS
            or label_matches_any_key(field)
            or (allowed is not None and key not in allowed)
        ):
            return Resolution(field, None, "ask", reason="unrecognized field")

    if key == "resume" and field.kind == "file":
        if resume_path:
            return Resolution(field, key, "upload", value=resume_path)
        return Resolution(field, key, "ask", reason="no resume path")

    if key == "cover_letter" and field.kind == "file":
        return Resolution(
            field, key, "ask", reason="cover letter file — attach by hand"
        )

    if key is None:
        if autoapply and not overridden:
            resolution = _esignature_answer(field, answers)
            if resolution is None:
                resolution = _autoapply_question(field, answers)
            if resolution is None:
                resolution = _factual_question(field, answers, company, education)
            if resolution is not None:
                return resolution
        return Resolution(field, key, "ask", reason="unrecognized field")

    # Only a file input can take a resume or cover letter. Reaching here, the
    # field is not one (the file branches above returned), so the key names a
    # text box that merely mentions a resume; nothing stored belongs in it.
    if key in ("resume", "cover_letter"):
        return Resolution(
            field, key, "ask", reason=f"{key} on a non-file field — answer by hand"
        )

    # Unattended, an optional salary question is left blank: nothing says what
    # the candidate asks for, and a required one is answered from the posting (see
    # _salary_answer). The assist path keeps its old behavior: the stored answer.
    if autoapply and key in ("salary_expectation", "bonus_expectation") and not field.required:
        return Resolution(
            field, key, "skip", reason="optional salary question — left blank"
        )

    # A textarea is a free-text answer box, never a short stored value like a
    # name or a city. A key was named for it (by a label that mentions one),
    # but the box is asking for prose. A textarea with NO key keeps the plain
    # "unrecognized field" reason above, which is what lets fill.py hand it to
    # the answer tailor. The exceptions are the boxes that only LOOK like prose:
    # see _short_answer_textarea.
    if field.kind == "textarea" and not _short_answer_textarea(
        field, key, autoapply=autoapply
    ):
        return Resolution(
            field, key, "ask", reason="long-answer field — answer by hand"
        )

    # The candidate's own words (spec, "standing answers"): yes at any number of office days,
    # two years of experience, none since graduation. Unattended, they stand in for a
    # stored answer that is missing; a stored one still wins.
    if (
        autoapply
        and key in _autoapply_rule_answers()
        and not str(answers.get(key) or "").strip()
    ):
        answers = {**answers, key: _autoapply_rule_answers()[key]}

    # "Have you applied to <company> before?" (classify names it prior_relationship): when
    # posting_answers left it out (the company is on their resume or they have applied there),
    # the specific facts it keeps answer it (_prior_answer): Yes only for a submitted
    # application or a job on the resume.
    if (
        autoapply
        and key == "prior_relationship"
        and not str(answers.get(key) or "").strip()
    ):
        prior = _prior_answer(_clean_text(f"{field.label} {field.placeholder}"), answers)
        if prior is not None:
            answers = {**answers, key: prior}

    # "From where do you intend to work?" typed as city and state, with no city from the
    # posting: their own working-location text (standing working_location).
    if (
        autoapply
        and key == "work_location"
        and _standing().working_location
        and field.kind == "text"
        and not str(answers.get(key) or "").strip()
        and _WORKING_LOCATION_RE.search(_clean_text(f"{field.label} {field.placeholder}"))
    ):
        answers = {**answers, key: _standing().working_location}

    # "When can you start?" typed as a date, with no start date stored: their earliest start
    # (standing earliest_start), as the key-less "When could you begin?" already gets. A
    # bare "Start date" box (a job's or a school's dates) is not this: _START_TEXT_RE only
    # matches a question about when they could begin.
    if (
        autoapply
        and key == "start_date"
        and _standing().earliest_start
        and field.kind == "text"
        and not str(answers.get(key) or "").strip()
        and _START_TEXT_RE.search(_clean_text(f"{field.label} {field.placeholder}"))
    ):
        answers = {**answers, key: _standing().earliest_start_text}

    # The type of sponsorship is N/A when the stored sponsorship is No (they need none).
    # Derived here, not a standing answer in _autoapply_rule_answers(): any other stored
    # sponsorship (Yes, missing, empty) leaves the follow-up asked, and a stored type
    # still wins.
    if (
        autoapply
        and key == "sponsorship_type"
        and not str(answers.get(key) or "").strip()
        and str(answers.get("sponsorship") or "").strip().casefold() == "no"
    ):
        answers = {**answers, key: _SPONSORSHIP_TYPE_NA}

    # No GPA is stored on its own: the unattended lane reads it off the stored degree
    # line ("... GPA 3.17"), only where a form asks for one.
    if autoapply and key == "gpa" and not str(answers.get(key) or "").strip():
        derived = _gpa_from_degree(str(answers.get("degree") or ""))
        if derived is not None:
            return _derived_gpa_answer(field, key, derived)

    if autoapply and key == "salary_expectation":
        resolution = _salary_confirm_answer(field, key, answers)
        if resolution is not None:
            return resolution
    if autoapply and key in ("salary_expectation", "bonus_expectation"):
        resolution = _standing_pay_answer(field, key)
        if resolution is not None:
            return resolution
    if (
        autoapply
        and key == "field_of_study"
        and not str(answers.get(key) or "").strip()
    ):
        return _derived_field_of_study(field, key, answers)

    if autoapply and key == "how_heard":
        resolution = _how_heard_other_answer(field, key, answers, company)
        if resolution is not None:
            return resolution

    answer = _SPECIAL_ANSWERS.get(key)
    if answer is not None:
        resolution = answer(field, key, answers, company, address_pieces)
        if resolution is not None:
            return resolution

    stored = _stored_answer(key, answers)
    if stored is None:
        return Resolution(field, key, "ask", reason=f"no stored answer for '{key}'")
    return _ordinary_answer(field, key, stored, answers)


_BARE_LINKEDIN_RE = re.compile(
    r"^(?:your\s+|please\s+(?:enter|provide)\s+your\s+)?linkedin"
    r"(?:\s+(?:profile|url|link|account|profile\s+(?:url|link)))?\s*[:.]?$"
)


def _short_answer_textarea(
    field: FieldSpec, key: str, *, autoapply: bool = False
) -> bool:
    """Whether a textarea that `key` was named for takes a short stored answer
    after all: a legal name (DRW makes "Legal First Name" a textarea) or a street
    address. A how-did-you-hear box stays a long answer in the assist lane; the
    unattended lane fills the stored answer (2026-10-24: it is their own
    answer, not a drafted one)."""
    if key in ("first_name", "last_name", "full_name"):
        return is_legal_name_label(field)
    if key == "how_heard":
        return autoapply
    # A required long-answer box labelled just "LinkedIn" (Reducto, 2026-10-27) wants the
    # profile URL: the unattended lane fills the stored one; the assist lane asks.
    if key == "linkedin":
        return autoapply and _BARE_LINKEDIN_RE.match(_clean_text(field.label)) is not None
    # "If so, please specify the type of sponsorship required." is a one-word N/A.
    return key in ("address_street", "sponsorship_type")


def _stored_answer(key: str, answers: dict[str, str]) -> str | None:
    """The stored answer for `key`, or None when there is none. The one fallback:
    a preferred name that was never stored is the first name."""
    if key == "preferred_name" and not str(answers.get(key) or "").strip():
        return answers.get("first_name")
    return answers.get(key)


def _ordinary_answer(
    field: FieldSpec, key: str, stored: str, answers: dict[str, str]
) -> Resolution:
    """The way a stored answer goes into a field when nothing about the key asks
    for more: typed into a text box, or matched to one option of a list."""
    country = _residence_country(field, key, answers)

    if field.kind in _CHOICE_KINDS:
        matched_option = _match_option(stored, field.options, key)
        if matched_option is None and country is not None:
            matched_option = _country_option(stored, country, field.options)
        if matched_option is None:
            matched_option = _affirmative_statement_option(
                field.options, key, stored
            )
        if matched_option is None:
            return Resolution(field, key, "ask", reason=_NO_OPTION)
        return Resolution(field, key, "fill", value=matched_option)

    # A react-select is scanned as plain text, so its options are unknown here.
    # A stored Yes or No to a yes/no question goes in as the menu's one option that is
    # that word or opens with it (DV Trading: "Yes, I am authorized to work ...").
    word = stored.strip()
    if field.kind == "text" and key in YES_NO_KEYS and word.casefold() in ("yes", "no"):
        if key in _STATEMENT_WORDS and word.casefold() == "yes":
            # Lithic: "Yes - I am within a commutable distance" and "Yes - I am willing
            # to relocate" both open with Yes, so the plain word names two. The same
            # statement fallback a visible list gets picks the one that says relocate.
            return _picked(field, key, word, "yes_no_statement", [word, key])
        return _picked(field, key, word, "yes_no", [word])
    # fill.py tries the country only if the menu has no option for the city.
    fallback = country if field.kind == "text" else None
    return Resolution(field, key, "fill", value=stored, fallback_value=fallback)


# --- Stored answers that need more than a text match -------------------------------
#
# Each function below takes over one key's answer when the field has something the
# ordinary path (_ordinary_answer) cannot do with a stored string: a range list, a
# yes/no, a posting-derived number. It returns None to hand the field back to the
# ordinary path, so a field it has no rule for is answered exactly as before. A
# list the rule cannot settle (no option, or two) is always asked, never guessed.

_NUMBER = r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k\b)?"
_SEPARATOR = r"\s*(?:-|–|—|to)\s*"
_BETWEEN_RE = re.compile(_NUMBER + _SEPARATOR + _NUMBER)
_LONE_NUMBER_RE = re.compile(_NUMBER)
_ABOVE_RE = re.compile(
    r"(?P<excl>more\s+than|over|above|greater\s+than|>)\s*" + _NUMBER
    + r"|(?P<incl>at\s+least|minimum(?:\s+of)?|>=)\s*" + _NUMBER
    + r"|" + _NUMBER + r"\s*(?:\+|plus\b|or\s+(?:more|above|higher|greater|over)\b"
    r"|and\s+(?:up|above|over|higher)\b)"
)  # fmt: skip
_BELOW_RE = re.compile(
    r"(?P<excl>less\s+than|under|below|fewer\s+than|<)\s*" + _NUMBER
    + r"|(?P<incl>up\s+to|at\s+most|maximum(?:\s+of)?|<=)\s*" + _NUMBER
    + r"|" + _NUMBER + r"\s*(?:or\s+(?:less|fewer|below|lower|under)\b"
    r"|and\s+(?:below|under|lower)\b)"
)  # fmt: skip


def _num(digits: str, thousands: str | None) -> float:
    value = float(digits.replace(",", ""))
    return value * 1000 if thousands else value


def _first_number(match: re.Match[str]) -> float:
    """The number a qualifier match (_ABOVE_RE, _BELOW_RE) found, whichever of
    its three alternatives matched."""
    found = _LONE_NUMBER_RE.search(match.group(0))
    return _num(*found.groups()) if found else 0.0


def _option_range(
    option: str,
) -> tuple[float | None, bool, float | None, bool] | None:
    """The numeric range an option stands for, as (low, low_inclusive, high,
    high_inclusive) with None for an open end, or None when the option is not a
    range ("Prefer not to say"). "3.0 - 3.29", "1-3 years", "5+ years", "Less
    than 1 year", "$100K - $120K", "Below 3.0", "3.5 and above", "2"."""
    text = " ".join(str(option or "").casefold().replace("’", "'").split())
    above = _ABOVE_RE.search(text)
    if above:
        return _first_number(above), above.group("excl") is None, None, False
    below = _BELOW_RE.search(text)
    if below:
        return None, False, _first_number(below), below.group("excl") is None
    between = _BETWEEN_RE.search(text)
    if between:
        low_d, low_k, high_d, high_k = between.groups()
        # "100-120k": the k is shared.
        low = _num(
            low_d, low_k or (high_k if float(low_d.replace(",", "")) < 1000 else None)
        )
        return low, True, _num(high_d, high_k), True
    lone = list(_LONE_NUMBER_RE.finditer(text))
    if len(lone) == 1:
        value = _num(*lone[0].groups())
        return value, True, value, True
    return None


def _in_range(
    value: float, bounds: tuple[float | None, bool, float | None, bool]
) -> bool:
    low, low_incl, high, high_incl = bounds
    if low is not None and (value < low or (value == low and not low_incl)):
        return False
    if high is not None and (value > high or (value == high and not high_incl)):
        return False
    return True


def _range_option(value: float, options: list[str]) -> str | None:
    """The one option whose numeric range holds `value`, or None when no option
    or two do (a range list that overlaps is not read: it is asked)."""
    hits = []
    for option in options:
        bounds = _option_range(option)
        if bounds is not None and _in_range(value, bounds):
            hits.append(option)
    return hits[0] if len(hits) == 1 else None


def _stored_number(stored: str) -> float | None:
    """The number a stored answer opens with: 3.17, "2 years" -> 2.0."""
    found = re.match(r"\s*\$?(\d[\d,]*(?:\.\d+)?)", stored or "")
    return float(found.group(1).replace(",", "")) if found else None


def _plain_number(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def _yes_no_option(options: list[str], answer: str) -> str | None:
    """The option that is `answer` ("Yes" or "No"): the same word, or an option
    opening with it ("Yes, I am", "No - I have not"). Never a substring ("know"
    holds "no"). None when there is not exactly one."""
    word = answer.strip().casefold()
    exact = [o for o in options if o.strip().casefold() == word]
    if len(exact) == 1:
        return exact[0]
    opening = [o for o in options if re.match(re.escape(word) + r"\b(?!')", _plain(o))]
    return opening[0] if len(opening) == 1 else None


# A relocation or office-attendance list whose options are statements, not Yes/No
# (Ashby, 2026-10-26: "I will relocate and am happy to work in office", "Willing to
# relocate to New York", "I am able to be in-person in San Francisco"). The candidate will
# relocate to be within an hour of any office, and on-site is Yes, so a stored Yes takes
# the one option that says so with no negation in it.
_STATEMENT_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|unable|cannot|only)\b|n['\u2019]t\b"
)
_RELOCATE_WORD_RE = re.compile(r"\b(?:relocat\w*|move|moving)\b")
_ABLE_WORD_RE = re.compile(
    r"\b(?:able|willing|happy|will|comfortable|ok|okay|fine|excited)\b"
)
# An office-attendance option must also name the office and never be a remote one
# ("I am happy to work remotely" is not the on-site Yes).
_OFFICE_WORD_RE = re.compile(
    r"\b(?:offices?|on[\s-]*site|onsite|in[\s-]*person|in[\s-]*office)\b"
)
_REMOTE_WORD_RE = re.compile(r"\bremote(?:ly)?\b")
_STATEMENT_WORDS = {
    "willing_to_relocate": _RELOCATE_WORD_RE,
    "onsite_ok": _ABLE_WORD_RE,
}


def _affirmative_statement_option(
    options: list[str], key: str, answer: str
) -> str | None:
    """The one option of a list of statements that is the candidate's Yes: no negation and,
    for a relocation question, a word about relocating or moving, for an
    office-attendance question, a word of ability or willingness that also names the
    office or in-person and is not a remote option. None when the stored
    answer is not Yes, the key is any other, or there are not exactly one such option
    (none or two: the list is asked)."""
    if answer.strip().casefold() != "yes" or key not in _STATEMENT_WORDS:
        return None
    says_yes = [
        o
        for o in options
        if _STATEMENT_WORDS[key].search(_plain(o))
        and not _STATEMENT_NEGATION_RE.search(_plain(o))
        and (
            key != "onsite_ok"
            or (
                _OFFICE_WORD_RE.search(_plain(o))
                and not _REMOTE_WORD_RE.search(_plain(o))
            )
        )
    ]
    return says_yes[0] if len(says_yes) == 1 else None


def _fill_choice(field: FieldSpec, key: str, option: str | None) -> Resolution:
    if option is None:
        return Resolution(field, key, "ask", reason=_NO_OPTION)
    return Resolution(field, key, "fill", value=option)


def _is_choice(field: FieldSpec) -> bool:
    return field.kind in _CHOICE_KINDS


def _yes_no_answer(field, key, answers, company, address_pieces) -> Resolution | None:
    """A stored Yes or No (military service, defense work, office days, whether
    they have been here before, whether they were referred). A list takes the Yes or No
    option, strictly; anything else stored is the ordinary path's."""
    stored = (answers.get(key) or "").strip()
    if stored.casefold() not in ("yes", "no") or not _is_choice(field):
        return None
    option = _yes_no_option(field.options, stored)
    if option is None:
        option = _affirmative_statement_option(field.options, key, stored)
    return _fill_choice(field, key, option)


def _count_answer(field, key, answers, company, address_pieces) -> Resolution | None:
    """Years of experience, or of full-time work since graduation (stored "2
    years", "0 years"). "N or more years?" is a yes/no: Yes when the stored years
    reach N. A list of ranges takes the range holding the number; a number box
    or a text box takes the bare number."""
    years = _stored_number(str(answers.get(key) or ""))
    if years is None:
        return None
    at_least = years_at_least(field)
    if at_least is not None:
        answer = "Yes" if years >= at_least else "No"
        if _is_choice(field):
            return _fill_choice(field, key, _yes_no_option(field.options, answer))
        return Resolution(field, key, "fill", value=answer)
    if _is_choice(field):
        option = _range_option(years, field.options)
        # No range holds the number: the list is asked, not read by substring.
        return _fill_choice(field, key, option)
    return Resolution(field, key, "fill", value=_plain_number(years))


_DEGREE_GPA_RE = re.compile(r"\bGPA\s*:?\s*([0-4]\.\d{1,2})\b", re.IGNORECASE)


def _gpa_from_degree(degree: str) -> str | None:
    """The GPA a stored degree line states ("B.S. ... GPA 3.17"), or None."""
    found = _DEGREE_GPA_RE.search(degree)
    return found.group(1) if found else None


# A GPA label the degree line's GPA does not answer: another school level, a different
# scale, a weighted or major-only figure, a percent.
_GPA_OTHER_RE = re.compile(
    r"\bhigh\s*school\b|\bsecondary\b|\bgrad(?:uate)?\b|\bmba\b|\bmaster'?s?\b"
    r"|\bphd\b|\bdoctora\w*\b|\bmajor\b|\bweighted\b|\bscale\b|\bout\s+of\b"
    r"|\bpercent\w*|%"
)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# The most a 4.0-scale option list can hold (a 4.3 scale is the highest in use).
_GPA_SCALE_MAX = 4.3


def _derived_gpa_answer(field, key, derived: str) -> Resolution:
    """The GPA read off the stored degree line, for the form's GPA question. Only a
    label that asks for the degree's own cumulative GPA takes it: a school level, a
    scale, a weighted or percent figure, or any number but 4.0 in the label is asked.
    A list is read by its range buckets alone, and only when every number in it fits
    a 4.0 scale; a list no bucket settles is asked."""
    label = " ".join(field.label.casefold().replace("\u2019", "'").split())
    numbers = [float(n) for n in _NUMBER_RE.findall(label)]
    if _GPA_OTHER_RE.search(label) or any(n != 4.0 for n in numbers):
        return Resolution(
            field, key, "ask", reason="GPA question is not the degree's cumulative GPA"
        )
    if not _is_choice(field):
        return Resolution(field, key, "fill", value=derived)
    in_options = [float(n) for o in field.options for n in _NUMBER_RE.findall(o)]
    if not in_options or max(in_options) > _GPA_SCALE_MAX:
        return Resolution(field, key, "ask", reason=_NO_OPTION)
    return _fill_choice(field, key, _range_option(float(derived), field.options))


def _gpa_answer(field, key, answers, company, address_pieces) -> Resolution | None:
    """A text box gets the stored GPA; a list of ranges gets the range holding it
    ("3.0 - 3.29" for 3.17)."""
    gpa = _stored_number(str(answers.get(key) or ""))
    if gpa is None or not _is_choice(field):
        return None
    option = _range_option(gpa, field.options)
    return Resolution(field, key, "fill", value=option) if option else None


_BUSINESS_AND_MANAGEMENT_RE = re.compile(
    r"^business(?:\s+administration)?(?:\s+and\s+management)?$"
    r"|^business\s+and\s+administration$|^business\s+management$"
)


def _field_of_study_answer(
    field, key, answers, company, address_pieces, *, derived: bool = False
) -> Resolution | None:
    """A list takes, in order: the stored field exactly; for a business degree,
    the one option that is business and/or management ("Business & Management",
    "Business Administration and Management"); else the only option containing
    "business". Several candidates, or none, are asked. `derived` (a major read off the
    degree line, not a stored answer) stops after the business/management option."""
    stored = str(answers.get(key) or "").strip()
    if not stored or not _is_choice(field):
        return None

    def norm(text: str) -> str:
        return " ".join(
            _plain(text).replace("&", " and ").replace("/", " and ").split()
        )

    wanted = norm(stored)
    exact = [o for o in field.options if norm(o) == wanted]
    if len(exact) == 1:
        return Resolution(field, key, "fill", value=exact[0])
    if "business" not in wanted.split():
        return None
    named = [o for o in field.options if _BUSINESS_AND_MANAGEMENT_RE.match(norm(o))]
    if len(named) == 1:
        return Resolution(field, key, "fill", value=named[0])
    if named:
        return Resolution(field, key, "ask", reason=_NO_OPTION)
    if derived:
        return None
    containing = [o for o in field.options if "business" in norm(o).split()]
    return _fill_choice(field, key, containing[0] if len(containing) == 1 else None)


_DEGREE_PREFIX_RE = re.compile(
    r"^\s*(?:b\.?[sa]\.?|bba|bachelor(?:['\u2019]s)?(?:\s+of\s+(?:science|arts))?)"
    r"\s*(?:in\s+)?",
    re.IGNORECASE,
)
_DEGREE_TAIL_RE = re.compile(r"\s+[\u2014\u2013]\s+")


def _major_parts(degree: str) -> list[str]:
    """The majors of a stored degree line, in order: "B.S. Business Administration,
    Finance \u2014 Example University, Dec 2026" -> ["Business Administration", "Finance"].
    Empty when the line does not open with a bachelor's degree."""
    if not _BACHELOR_RE.match(_plain(degree)):
        return []
    major = _DEGREE_TAIL_RE.split(degree, maxsplit=1)[0]
    major = _DEGREE_PREFIX_RE.sub("", major, count=1)
    return [part.strip() for part in major.split(",") if part.strip()]


def _derived_field_of_study(field, key, answers) -> Resolution:
    """The unattended lane's discipline when none is stored: the first major of the
    stored degree that equals exactly one option. A form with none of them is asked."""
    parts = _major_parts(str(answers.get("degree") or ""))
    if not parts:
        return Resolution(field, key, "ask", reason=f"no stored answer for '{key}'")
    if _is_choice(field):
        # A business degree takes the list's business/management option
        # ("Business & Management"), the way a stored field does.
        resolution = _field_of_study_answer(
            field, key, {**answers, key: parts[0]}, None, None, derived=True
        )
        if resolution is not None and resolution.action == "fill":
            return resolution
    return _picked(field, key, parts[0], "first_unique_equal", parts)


_DOLLARS_RE = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(k\b)?", re.IGNORECASE)
_CONFIRM_RE = re.compile(
    r"\b(?:confirm\w*|align\w*|agree\w*|comfortable|acceptable|consistent\s+with)\b"
)


def _salary_confirm_answer(field, key, answers) -> Resolution | None:
    """A question that states the role's pay and asks whether they accept it ("... the
    salary for this role is $97,000. Please confirm that this aligns with your desired
    salary"). Its options are answers, not amounts, so the stored salary never goes in.
    The lowest figure the label states at or above their own floor (the candidate's fallback
    expectation, posting_answers.default_salary(); never the merged expectation, which in
    the unattended lane is the posting's own range) is a Yes (the one option opening
    with "Yes"); below it, or unreadable, or no single Yes option, is asked. None when
    the label is no such question."""
    # posting_answers imports this module, so the floor is read at call time.
    from jobhunt.apply_assist.posting_answers import default_salary

    text = f"{field.label} {field.placeholder}"
    figures = [
        _num(digits, thousands)
        for digits, thousands in _DOLLARS_RE.findall(text)
    ]
    if not figures or not _CONFIRM_RE.search(text.casefold()):
        return None
    lowest = min(figures)
    # A figure under $1,000 is an hourly rate or a bonus, not the annual pay.
    if lowest < 1000:
        return Resolution(field, key, "ask", reason="posted pay unreadable — answer by hand")
    floor = default_salary()
    if floor is None:
        return Resolution(field, key, "ask", reason="no standing salary — answer by hand")
    if lowest < floor:
        return Resolution(
            field, key, "ask", reason="posted pay is below his salary floor"
        )
    return _picked(field, key, "Yes", "starts_with", ["Yes"])


# A stored degree opening with an abbreviation or the word: "B.S. Business Administration,
# Finance — Example University, Dec 2026", "Bachelor of Science in Finance", "BA".
_BACHELOR_RE = re.compile(r"^(?:b\.?[sa]\.?|bba|bachelor(?:'?s)?)(?=[\s.,'(]|$)")

# A question about a COMPLETED degree ("highest level of education completed", "Have you
# earned a bachelor's?"). The candidate's degree is in progress, so "Bachelor's" would claim
# more than is true; only an option that says in progress / expected answers it.
_COMPLETED_DEGREE_RE = re.compile(
    r"\b(?:complet\w*|obtain\w*|earn\w*|confer\w*|graduat\w*|award\w*)\b"
    r"|\bhighest\s+(?:level\s+of\s+)?(?:degree|education|educational)\b"
)
_IN_PROGRESS_OPTION_RE = re.compile(
    r"\bin[\s-]+progress\b|\bpursuing\b|\bcurrently\s+(?:enrolled|pursuing|attending)\b"
    r"|\benrolled\b|\bexpected\b|\bexpecting\b|\banticipated\b|\bcurrent\s+student\b"
)


# Rules whose answer is only ever an option of a menu: a box with no menu is not filled.
MENU_ONLY_RULES = frozenset({"decline", "acknowledge"})


def pick_option(rule: str, args: list[str], options: list[str]) -> str | None:
    """The one option `rule` names in `options`, or None when it names none or two.
    "starts_with": the single option opening with args[0] as a word ("Bachelor" for
    "Bachelor's Degree", "Yes" for "Yes, I understand ..."). "first_unique_equal": the
    first of `args`, in order, that equals (case-insensitively) exactly one option.
    "yes_no" (args = [answer]): the one option that is the Yes or No or opens with it.
    "yes_no_statement" (args = [answer, key]): "yes_no", else the one affirmative
    statement option of a relocation or office-attendance list.
    "pronouns": the one option that is the standing pronouns ("He/Him/His", "he/him", "He").
    "school": the one option naming the standing school or one of its aliases.
    An unknown rule picks nothing. fill.py reads a react-select's menu with this."""
    if rule == "in_progress":
        # The one option that says the degree is under way or expected, never "some
        # college" or a finished degree.
        hits = [o for o in options if _IN_PROGRESS_OPTION_RE.search(_plain(o))]
        return hits[0] if len(hits) == 1 else None
    if rule == "decline":
        # The one option that declines to answer ("Decline To Self Identify", "I don't
        # wish to answer"); a substantive option is never one.
        hits = [o for o in options if _DECLINE_OPTION_RE.search(_plain(o))]
        return hits[0] if len(hits) == 1 else None
    if rule == "school":
        # The one option of a school list that names the standing school (or an alias);
        # never "Other (School Not Listed)": with none, or two, nothing is picked.
        matcher = _school_option_re()
        if matcher is None:
            return None
        hits = [o for o in options if matcher.search(_plain(o))]
        return hits[0] if len(hits) == 1 else None
    if rule == "pronouns":
        matcher = _pronouns_option_re(_standing().pronouns)
        if matcher is None:
            return None
        hits = [o for o in options if matcher.fullmatch(_plain(o))]
        return hits[0] if len(hits) == 1 else None
    if rule == "acknowledge":
        # The one option that assents ("Yes", "I acknowledge", "I agree"); never one
        # that withholds it.
        hits = [
            o
            for o in options
            if _ACK_OPTION_RE.match(_plain(o)) and not _WITHHELD_RE.search(_plain(o))
        ]
        return hits[0] if len(hits) == 1 else None
    if rule == "starts_with" and args:
        # "Bachelor" also opens "Bachelors" and "Bachelor's" (one optional ending).
        opening = re.compile(re.escape(_plain(args[0])) + r"(?:'?s)?\b")
        hits = [o for o in options if opening.match(_plain(o))]
        return hits[0] if len(hits) == 1 else None
    if rule == "first_unique_equal":
        for arg in args:
            hits = [o for o in options if _plain(o) == _plain(arg)]
            if len(hits) == 1:
                return hits[0]
    if rule == "yes_no" and args:
        # A stored Yes or No against a menu that spells it out ("Yes, I am authorized to
        # work ...", "No, I will not require sponsorship ..."): the option that is the
        # word or opens with it, never a substring ("Now" holds "no"), and only when
        # exactly one does, so a crowded menu or one holding only the other answer
        # picks nothing.
        return _yes_no_option(options, args[0])
    if rule == "yes_no_statement" and len(args) >= 2:
        # A relocation or office-attendance Yes against a menu that may spell it as
        # "Yes - I am willing to relocate" beside another "Yes - ...": the plain Yes/No
        # option when it names exactly one, else the one affirmative statement option
        # (_affirmative_statement_option). Two, or none, picks nothing.
        return _yes_no_option(options, args[0]) or _affirmative_statement_option(
            options, args[1], args[0]
        )
    if rule == "country" and args:
        # The one option that is the stored country, the same text or the same place
        # ("United States of America" for "United States"); equality only.
        hits = [
            o for o in options
            if _plain(o) == _plain(args[0]) or same_place(args[0], o)
        ]  # fmt: skip
        return hits[0] if len(hits) == 1 else None
    if rule == "affirm":
        # A commitment they make (in the office, relocating, travel): the Yes option, else
        # the one affirmative statement of an office-attendance or relocation list, else
        # the one assenting option ("I understand", "I acknowledge").
        return (
            _yes_no_option(options, "Yes")
            or _affirmative_statement_option(options, "onsite_ok", "Yes")
            or _affirmative_statement_option(options, "willing_to_relocate", "Yes")
            or pick_option("acknowledge", [], options)
        )
    if rule == "company_site_or_other":
        # args = [stored text, company]. The company-site option "company_site" names, else
        # the one "Other" option when no option names any site or the company (so "Other"
        # is true and no site option was passed over); with any such option, none.
        company_name = args[1] if len(args) > 1 and args[1] else None
        site = pick_option("company_site", args[:1], options)
        if site is not None:
            return site
        if any(names_company_site(o, company_name) or _names_any_site(o, company_name) for o in options):
            return None
        others = [o for o in options if _OTHER_OPTION_RE.fullmatch(_plain(o))]
        return others[0] if len(others) == 1 else None
    if rule == "company_site":
        # The one option naming the company's own site in a generic wording ("Company
        # website", "Careers page"); with two, the one equal to the stored text. A
        # company-named option ("DV Website") is never one (names_company_site).
        sites = [o for o in options if names_company_site(o)]
        if len(sites) > 1 and args:
            sites = [o for o in sites if _plain(o) == _plain(args[0])] or sites
        return sites[0] if len(sites) == 1 else None
    return None


def _picked(field, key, value, rule, args) -> Resolution:
    """Fill a list from a rule: now when the options are known, else (a react-select)
    by handing fill.py the rule to apply to the open menu."""
    if _is_choice(field):
        return _fill_choice(field, key, pick_option(rule, args, field.options))
    return Resolution(field, key, "fill", value=value, option_rule=rule, option_args=args)


def _degree_answer(field, key, answers, company, address_pieces) -> Resolution | None:
    """A stored bachelor's degree ("Bachelor's", or a full "B.S. ..." line) on a
    list: the stored text exactly, else the single option opening with "Bachelor"
    (Greenhouse's "Bachelor's Degree"); a react-select gets the same rule applied to
    its menu. A question about a completed degree takes only the one in-progress or
    expected option, else is asked. Any other degree, and any other kind of field, is
    the ordinary path's."""
    stored = str(answers.get(key) or "").strip()
    if not _BACHELOR_RE.match(_plain(stored)):
        return None
    label = _plain(f"{field.label} {field.placeholder}")
    if _COMPLETED_DEGREE_RE.search(label):
        # Their degree is not finished: only a single in-progress option is true.
        return _picked(field, key, stored, "in_progress", [])
    if _is_choice(field):
        wanted = _plain(stored)
        exact = [o for o in field.options if _plain(o) == wanted]
        if len(exact) == 1:
            return Resolution(field, key, "fill", value=exact[0])
    return _picked(field, key, stored, "starts_with", ["Bachelor"])


def _how_heard_answer(
    field, key, answers, company, address_pieces
) -> Resolution | None:
    """A list takes the option that names the company's own website or careers
    page, and is asked when it has none (or two); a text box takes the stored
    answer, except a react-select (its options are unseen), which gets the rule that
    picks the menu's generic company-site option when the stored answer is one."""
    if not _is_choice(field):
        stored = str(answers.get(key) or "").strip()
        if field.kind == "text" and names_company_site(stored, company):
            return _picked(field, key, stored, "company_site", [stored])
        return None
    sites = [o for o in field.options if names_company_site(o, company)]
    stored = str(answers.get(key) or "").strip().casefold()
    if len(sites) > 1:
        sites = [o for o in sites if o.strip().casefold() == stored] or sites
    if len(sites) == 1:
        return Resolution(field, key, "fill", value=sites[0])
    return Resolution(field, key, "ask", reason=_NO_OPTION)


# An "Other" how-heard option: "Other", "Other (please specify)", "Other - please
# specify", "Other:". Not "Other job board", "Other website", "Others", "Another".
_OTHER_OPTION_RE = re.compile(
    r"other(?:\s*(?:[(\-–—:,]\s*)?\(?\s*(?:please\s+)?(?:specify|explain)"
    r"(?:\s+(?:below|here))?\s*\)?)?\s*[:.]?"
)


_ANY_SITE_WORD_RE = re.compile(
    r"\b(?:web\s*site|careers?|career\s+(?:site|page)|jobs?\s+(?:site|page)|\.com)\b",
    re.IGNORECASE,
)


def _names_any_site(option: str, company: str | None) -> bool:
    """An option that names a website or careers page in any form, or the company
    itself: "Other" beside it would be untrue, so that list is asked."""
    if _ANY_SITE_WORD_RE.search(option):
        return True
    name = _plain(company or "")
    first = name.split()[0] if name else ""
    return any(
        w and re.search(rf"\b{re.escape(w)}\b", _plain(option))
        for w in {name, first}
    )


def _how_heard_other_answer(field, key, answers, company) -> Resolution | None:
    """Unattended only (2026-10-25): a how-heard list with no company-site
    option takes its single "Other" option, when the stored answer itself names the
    company's site (so "Other" is true). Anything else is the ordinary path's. The
    "If you selected Other" box is never filled here."""
    stored = str(answers.get(key) or "").strip()
    if not _is_choice(field):
        # A react-select whose menu is unseen: fill.py picks the company-site option, or
        # the one "Other" when the menu names no site (company_site_or_other).
        if field.kind == "text" and stored and names_company_site(stored, company):
            return Resolution(
                field,
                key,
                "fill",
                value=stored,
                option_rule="company_site_or_other",
                option_args=[stored, company or ""],
            )
        return None
    if any(
        names_company_site(o, company) or _names_any_site(o, company)
        for o in field.options
    ):
        return None
    if not stored or not names_company_site(stored, company):
        return None
    others = [o for o in field.options if _OTHER_OPTION_RE.fullmatch(_plain(o))]
    if len(others) != 1:
        return None
    return Resolution(field, key, "fill", value=others[0])


_NUMBER_ONLY_RE = re.compile(
    r"\b(?:numbers?|digits?|numeric(?:al)?)\s+only\b|\bonly\s+(?:a\s+)?(?:numbers?|digits?"
    r"|numeric)\b|\bno\s+(?:commas|symbols|currency|dollar)|\bsingle\s+(?:number|figure)\b"
)


def _salary_answer(field, key, answers, company, address_pieces) -> Resolution | None:
    """With the posting's midpoint in `answers` (salary_midpoint), the field's type
    decides: a number-only box gets the midpoint, a list the range holding it, a
    text box the stored range text (the ordinary path). Without a midpoint the
    stored text is the ordinary path's too."""
    midpoint = _stored_number(str(answers.get("salary_midpoint") or ""))
    if midpoint is None:
        return None
    text = f"{field.label} {field.placeholder}".casefold()
    if field.kind == "number" or _NUMBER_ONLY_RE.search(text):
        return Resolution(field, key, "fill", value=str(int(midpoint)))
    if _is_choice(field):
        option = _range_option(midpoint, field.options)
        return Resolution(field, key, "fill", value=option) if option else None
    return None


_DEFERRED_PAY_RE = re.compile(r"\bdeferred\b|\bnon[\s-]*compete\b|\bnoncompete\b")
# A pay question that wants a rate or a share, not the annual dollars the candidate stated: an
# hourly, monthly or weekly figure, or a percent of base. Their $85,000 / $15,000 would be
# the wrong unit there, so it is asked.
_PAY_OTHER_UNIT_RE = re.compile(
    r"\b(?:hourly|monthly|weekly|bi[\s-]?weekly|daily|percent\w*)\b"
    r"|\bper\s+(?:hour|month|week|day)\b|/\s*(?:hr|hour|mo|month|wk|week)\b|%"
)


def _standing_pay_answer(field: FieldSpec, key: str) -> Resolution | None:
    """The candidate's pay (standing salary, bonus), the unattended lane only: a
    number-only box gets the digits ("85000", "15000"), a text box the text ("$85,000
    base plus $15,000 bonus", "$15,000"), a salary list the one range holding $85,000
    (never the posting's own midpoint). A bonus list is None (asked), and so is a salary
    list no single range holds. Deferred compensation, a non-compete and a pay question
    in another unit (hourly, a percent) are the candidate's to answer, so they stay asked."""
    if field.kind == "textarea":
        return None
    text = f"{field.label} {field.placeholder}".casefold()
    if _DEFERRED_PAY_RE.search(text):
        return Resolution(
            field, key, "ask", reason="deferred pay or a non-compete — answer by hand"
        )
    if _PAY_OTHER_UNIT_RE.search(text):
        return Resolution(
            field, key, "ask", reason="pay asked in another unit — answer by hand"
        )
    salary = key == "salary_expectation"
    standing = _standing()
    amount = standing.salary if salary else standing.bonus
    if amount is None:
        return None
    if _is_choice(field):
        if not salary:
            return None
        return _fill_choice(field, key, _range_option(amount, field.options))
    if field.kind == "number" or _NUMBER_ONLY_RE.search(text):
        return Resolution(field, key, "fill", value=str(amount))
    return Resolution(
        field, key, "fill", value=standing.salary_text if salary else standing.bonus_text
    )


def _pronouns_answer(field: FieldSpec) -> Resolution | None:
    """The standing pronouns for a required pronouns question (the unattended lane only). A list
    takes the one matching option (pick_option "pronouns"), a react-select gets the
    rule applied to its open menu, a text box the stored text. One box of a checkbox
    group is checked when its own text is the standing pronouns; every other box of the group stays
    asked. None when the field is none of these."""
    value = _standing().pronouns
    matcher = _pronouns_option_re(value)
    if matcher is None:
        return None
    if field.kind == "checkbox":
        own = field.label.partition(CHECKBOX_TITLE_SEP)[0]
        if matcher.fullmatch(_plain(own)):
            return Resolution(field, "pronouns", "check", reason=f"pronouns: {value}")
        return None
    if field.kind in (*_CHOICE_KINDS, "text"):
        return _picked(field, "pronouns", value, "pronouns", [])
    return None


def _employment_status_answer(
    field, key, answers, company, address_pieces
) -> Resolution | None:
    """ "Self-employed" when the list offers it (the candidate's own venture); a plain "Are you
    currently employed?" yes/no gets Yes. Anything else is the stored answer's."""
    if _is_choice(field):
        selfs = [
            o for o in field.options if re.search(r"self[\s-]*employed", _plain(o))
        ]
        if len(selfs) == 1:
            return Resolution(field, key, "fill", value=selfs[0])
        if asks_yes_no(field) and _yes_no_option(field.options, "Yes"):
            return _fill_choice(field, key, _yes_no_option(field.options, "Yes"))
    elif asks_yes_no(field):
        return Resolution(field, key, "fill", value="Yes")
    return None


def _desired_job_type_answer(
    field, key, answers, company, address_pieces
) -> Resolution | None:
    """Full-time (unless another type is stored). A list takes the one option that
    says full time."""
    stored = str(answers.get(key) or "").strip() or "Full-time"
    if not _is_choice(field):
        return Resolution(field, key, "fill", value=stored)
    wanted = re.sub(r"[\s-]+", " ", _plain(stored))
    hits = [o for o in field.options if re.sub(r"[\s-]+", " ", _plain(o)) == wanted]
    if not hits and wanted == "full time":
        hits = [
            o
            for o in field.options
            if re.search(r"\bfull[\s-]*time\b", _plain(o))
            and not re.search(r"\b(?:part|not|non)\b", _plain(o))
        ]
    return _fill_choice(field, key, hits[0] if len(hits) == 1 else None)


# Pieces of an address other than the street: a form that has any of these asks for
# the street separately, so a plain "Address" box is just the street.
_ADDRESS_OTHER_PIECES = frozenset({"city", "bare_city", "state", "zip"})


def _address_street_answer(
    field, key, answers, company, address_pieces
) -> Resolution | None:
    """ "Address" with no other address box in the form is one box for all of it:
    "street, city, state zip". Any part not stored and it is asked. A street box
    beside city, state or zip boxes takes the street alone."""
    if (
        address_piece(field) != "whole"
        or address_pieces & _ADDRESS_OTHER_PIECES
        or field.kind not in ("text", "textarea")
    ):
        return None
    parts = {
        k: str(answers.get(k) or "").strip()
        for k in ("address_street", "address_city", "address_state", "address_zip")
    }
    missing = [k for k, v in parts.items() if not v]
    if missing:
        return Resolution(
            field, key, "ask", reason=f"no stored answer for '{missing[0]}'"
        )
    value = (
        f"{parts['address_street']}, {parts['address_city']}, "
        f"{parts['address_state']} {parts['address_zip']}"
    )
    return Resolution(field, key, "fill", value=value)


def _state_code(text: str) -> str | None:
    """The two-letter code a state is spelled as ("Georgia", "GA", "GA - Georgia",
    "Georgia (GA)"), or None when it is not one state spelled either way."""
    codes = set()
    for part in re.split(r"\s*[-–,()]\s*", _plain(text).rstrip(".")):
        part = part.strip().rstrip(".")
        if not part:
            continue
        code = _US_STATES.get(part, part)
        if code not in _US_POSTAL_CODES:
            return None
        codes.add(code)
    return codes.pop() if len(codes) == 1 else None


def _address_state_answer(
    field, key, answers, company, address_pieces
) -> Resolution | None:
    """A state list spells the state by name or by code; the stored one is either.
    The one option that is the stored state is taken."""
    stored = str(answers.get(key) or "").strip()
    code = _state_code(stored)
    if code is None or not _is_choice(field):
        return None
    hits = [o for o in field.options if _state_code(o) == code]
    return (
        _fill_choice(field, key, hits[0] if len(hits) == 1 else None) if hits else None
    )


# A notice-period list's option that says none is required: "None", "No notice
# required", "Immediately", "0 days", "0 weeks". "None of the above" is no answer, and
# an option that negates one ("Not immediately") is the opposite of it.
_NO_NOTICE_OPTION_RE = re.compile(
    r"\bno\s+notice\b|\bnone\b(?!\s+of\b)|\bimmediate(?:ly)?\b"
    r"|\b(?:0|zero)\s*(?:days?|weeks?|months?)\b|^0$"
)
_NOTICE_NEGATION_RE = re.compile(r"\b(?:not|never|cannot)\b|n['’]t\b")


def _notice_period_answer(
    field, key, answers, company, address_pieces
) -> Resolution | None:
    """The stored "None" (2026-10-26: no notice period is required). A list
    takes the one option that says none, else it is asked (zero or two options);
    a number box takes 0, a text box the stored word. Any other stored notice
    ("2 weeks") is the ordinary path's."""
    if str(answers.get(key) or "").strip().casefold() != "none":
        return None
    if _is_choice(field):
        hits = [
            o
            for o in field.options
            if _NO_NOTICE_OPTION_RE.search(_plain(o))
            and not _NOTICE_NEGATION_RE.search(_plain(o))
        ]
        return _fill_choice(field, key, hits[0] if len(hits) == 1 else None)
    if field.kind == "number":
        return Resolution(field, key, "fill", value="0")
    return None


# A "what type of sponsorship" list's option that says none is needed: "N/A", "Not
# applicable", "None", "No sponsorship needed". "None of the above" is no answer.
_NO_SPONSORSHIP_OPTION_RE = re.compile(
    r"(?<![a-z])n\s?/\s?a(?![a-z])|\bnot\s+applicable\b|\bnone\b(?!\s+of\b)"
    r"|\bno\s+sponsorship\b"
)
_SPONSORSHIP_TYPE_NA = "N/A"


def _sponsorship_type_answer(
    field, key, answers, company, address_pieces
) -> Resolution | None:
    """The type of sponsorship a follow-up asks for is N/A when none is needed.
    resolve() puts the N/A in place of a missing answer only when the stored
    sponsorship is No (unattended lane); any other stored type is the ordinary
    path's. A list takes the one option that says none is needed, else it is asked;
    only a text box or textarea is typed the N/A."""
    stored = str(answers.get(key) or "").strip()
    if stored.casefold() != _SPONSORSHIP_TYPE_NA.casefold():
        return None
    if _is_choice(field):
        hits = [o for o in field.options if _NO_SPONSORSHIP_OPTION_RE.search(_plain(o))]
        return _fill_choice(field, key, hits[0] if len(hits) == 1 else None)
    if field.kind in ("text", "textarea"):
        return Resolution(field, key, "fill", value=_SPONSORSHIP_TYPE_NA)
    return Resolution(field, key, "ask", reason=f"a {field.kind} field cannot take N/A")


# The candidate's pay (standing salary and bonus, [standing_answers] in the search
# preferences). posting_answers merges the posting's own range into the unattended
# lane's answers, which would win over any stored text, so _standing_pay_answer reads
# the standing values directly: a number box gets the digits, a text box the text. An
# unset salary or bonus is not answered (the field is asked). The drift test
# (tests/test_standing_answers_drift.py) compares them with the facts file when one exists.


def _pronouns_option_re(pronouns: str) -> re.Pattern[str] | None:
    """Matches an option that is the standing pronouns: for "He/Him/His", "He/Him/His",
    "he/him", "He / Him", "He" (with a trailing aside or the word "pronouns"). "He/They"
    and "He/Him/They" are not it. None when no pronouns are set."""
    parts = [_plain(p) for p in re.split(r"[/,]", pronouns) if _plain(p)]
    if not parts:
        return None
    tail = ""
    for part in reversed(parts[1:]):
        tail = rf"(?:\s*[/,-]\s*{re.escape(part)}{tail})?"
    return re.compile(
        re.escape(parts[0]) + tail + r"(?:\s*\([^)]*\))?(?:\s+pronouns?)?[\s.]*"
    )


def _autoapply_rule_answers() -> dict[str, str]:
    """The candidate's standing answers keyed like classify() keys (onsite_ok, years of
    experience, camera, notice period, pronouns, government employment, military service):
    set values only, so an unset standing answer leaves its field asked (fail closed)."""
    return _standing().rule_answers()


_SPECIAL_ANSWERS = {
    "government_employment": _yes_no_answer,
    "military_service": _yes_no_answer,
    "defense_work": _yes_no_answer,
    "onsite_ok": _yes_no_answer,
    "prior_relationship": _yes_no_answer,
    "referred": _yes_no_answer,
    "years_experience": _count_answer,
    "years_since_graduation": _count_answer,
    "years_ma_experience": _count_answer,
    "camera_on": _yes_no_answer,
    "notice_period": _notice_period_answer,
    "sponsorship_type": _sponsorship_type_answer,
    "gpa": _gpa_answer,
    "field_of_study": _field_of_study_answer,
    "degree": _degree_answer,
    "how_heard": _how_heard_answer,
    "salary_expectation": _salary_answer,
    "employment_status": _employment_status_answer,
    "desired_job_type": _desired_job_type_answer,
    "address_street": _address_street_answer,
    "address_state": _address_state_answer,
}


def _plain(text: str) -> str:
    """An option's text for comparing: lower case, one space, curly quotes
    straight."""
    return " ".join(str(text or "").casefold().replace("’", "'").split())


# --- Place names ---------------------------------------------------------------
#
# A stored place ("Atlanta, Georgia", "United States") is often listed under a
# different spelling of the same place: "Atlanta, GA, USA", "United States of
# America". For the location and country keys only, same_place() treats two
# spellings as one place when they are EQUAL after a small, fixed rewrite. It is
# an equality test, never a substring or fuzzy one, and it only ever ADDS a way
# to match an option; it never removes one. Anything it cannot read exactly
# (a part it does not know, a stray comma) is simply not equal to anything.

PLACE_KEYS = frozenset({"location", "country"})

# The posting's own city (posting_answers): where they would work. A form lists it as
# "New York, New York, United States" for the stored "New York, NY", so it is matched
# as a place too.
JOB_PLACE_KEYS = frozenset({"work_location", "desired_location"})


def is_place_key(key: str | None) -> bool:
    """Whether `key` names a place that same_place() may match by spelling."""
    return key in PLACE_KEYS or key in JOB_PLACE_KEYS


# Whole strings that name the United States, in a country list.
_US_NAMES = frozenset(
    {"united states", "united states of america", "usa", "u.s.", "u.s.a.", "us"}
)

# What an option may append after a US state: a country, in these spellings only.
_US_TRAILERS = frozenset({"usa", "united states", "united states of america"})

_US_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "district of columbia": "dc", "florida": "fl", "georgia": "ga", "hawaii": "hi",
    "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy",
}  # fmt: skip
_US_POSTAL_CODES = frozenset(_US_STATES.values())
# The state names alone, for callers that must tell "Georgia" the state from a city.
US_STATE_NAMES = frozenset(_US_STATES)

_COMMA_RE = re.compile(r"\s*,\s*")

# The one city whose list spelling differs from the usual "<city>, <state>": Ashby's
# location box offers "New York City, New York, United States" and never
# "New York, New York, United States". Only this pair, only inside New York state.
_NYC_LONG = "new york city"
_NYC_SHORT = "new york"


def _place_parts(text: str) -> tuple[str, bool]:
    """(canonical place, whether a trailing United States was spelled out).

    Case, runs of whitespace and the space around commas do not matter. A whole
    string that names the United States becomes "united states". Otherwise, for
    "<place>, <US state>, <USA | United States | United States of America>" the
    country is dropped (and remembered), and a trailing US state name becomes its
    postal code, so "Atlanta, Georgia" and "Atlanta, GA, USA" both read
    "atlanta, ga". A single word is never rewritten ("Georgia" stays "georgia",
    the country), and an empty part makes the whole string unreadable ("")."""
    folded = _COMMA_RE.sub(", ", " ".join(str(text or "").split()).casefold())
    if folded in _US_NAMES:
        return "united states", False
    parts = folded.split(", ")
    if not all(parts):
        return "", False
    has_country = False
    if (
        len(parts) >= 3
        and parts[-1] in _US_TRAILERS
        and (parts[-2] in _US_STATES or parts[-2] in _US_POSTAL_CODES)
    ):
        parts.pop()
        has_country = True
    if len(parts) >= 2 and parts[-1] in _US_STATES:
        parts[-1] = _US_STATES[parts[-1]]
    if len(parts) == 2 and parts[0] == _NYC_LONG and parts[1] == "ny":
        parts[0] = _NYC_SHORT  # Ashby's location list names the city "New York City"
    return ", ".join(parts), has_country


def same_place(answer: str, option: str) -> bool:
    """Whether `option` names the same place as the stored `answer`: equal after
    _place_parts, with a trailing United States allowed on the option when the
    answer has none (never the other way round). Not for use on any key but
    PLACE_KEYS: "Georgia" the country is not "Georgia" the state, and a list of
    anything else has no business being rewritten."""
    want, want_country = _place_parts(answer)
    have, have_country = _place_parts(option)
    if not want or want != have:
        return False
    return have_country or not want_country


def _match_option(
    expected: str, options: list[str], key: str | None = None
) -> str | None:
    """Match `expected` against `options`: exact case-insensitive first, then
    (for a place key only: PLACE_KEYS) the one option that names the same place
    (same_place), then a unique case-insensitive substring match, then a close
    fuzzy match (_fuzzy_match). Fail closed: None whenever 0 or >1 options
    plausibly match — the caller asks instead of guessing."""
    expected_cf = expected.strip().casefold()

    for option in options:
        if option.strip().casefold() == expected_cf:
            return option

    if is_place_key(key):
        same = [option for option in options if same_place(expected, option)]
        if len(same) == 1:
            return same[0]
        if same:
            # Two options name the answer's place: neither is the answer.
            return None

    substring_matches = [
        option for option in options if expected_cf in option.strip().casefold()
    ]
    if len(substring_matches) == 1:
        return substring_matches[0]
    if substring_matches:
        # >1 substring hits is real ambiguity — never let the fuzzy tier
        # break the tie.
        return None

    return _fuzzy_match(expected_cf, options)


# Fuzzy-tier acceptance: the best option must clear this similarity AND beat
# the runner-up by the margin, otherwise we fall back to "ask".
_FUZZY_ACCEPT = 0.85
_FUZZY_MARGIN = 0.05


def _fuzzy_match(expected_cf: str, options: list[str]) -> str | None:
    """Snap a stored answer onto a legal option despite typos/paraphrase
    (e.g. "authorised" -> "Authorized"). Unlike AIHawk's always-pick-best,
    this is fail-closed: a low or contested best score returns None."""
    best_ratio, second_ratio, best_option = 0.0, 0.0, None
    for option in options:
        ratio = difflib.SequenceMatcher(
            None, expected_cf, option.strip().casefold()
        ).ratio()
        if ratio > best_ratio:
            best_ratio, second_ratio, best_option = ratio, best_ratio, option
        elif ratio > second_ratio:
            second_ratio = ratio
    if best_ratio < _FUZZY_ACCEPT or best_ratio - second_ratio < _FUZZY_MARGIN:
        return None
    return best_option


# --- A residence question whose list is made of countries ------------------------
#
# "Where are you currently located?" is sometimes a list of ~200 countries, and
# the stored city ("Atlanta, Georgia") matches none of them: "Georgia" is the
# country, and a fuzzy match onto it would be wrong. Such a question is really
# asking for the country, so it may take the stored country instead, but only
# when all of these hold:
#   - the field is a location field and its label asks where the applicant is
#     (keys.asks_where_you_are), not which office the job is in;
#   - a country is stored;
#   - the list shows no sign of being about cities: no option comes near the
#     stored city (_comes_near), so a list that names Atlanta in any form is left
#     to the city answer, and when that is ambiguous the field is asked about;
#   - exactly one option IS the country (_country_option): equal to it, or the
#     same place spelled another way. Never a substring or a fuzzy match.
# A react-select hides its options until it is open, so resolve() only carries
# the country along (Resolution.fallback_value) and fill.py applies the same
# rule to the open menu.


def _residence_country(
    field: FieldSpec, key: str, answers: dict[str, str]
) -> str | None:
    """The stored country, when `field` is a residence question it may answer
    (see above), else None."""
    if key != "location" or not asks_where_you_are(field):
        return None
    country = answers.get("country")
    if not isinstance(country, str) or not country.strip():
        return None
    return country.strip()


def _comes_near(answer: str, options: list[str]) -> bool:
    """Whether `answer` is near any of `options`: one spells it exactly, names
    the same place, contains it, is a close fuzzy match for it, or contains its
    city (the part before the first comma) as a whole word ("Atlanta (Hybrid)"
    for "Atlanta, Georgia"). Any of these means the list is made of places like
    the answer, and the stored city, not the country, is what it wants."""
    answer_cf = answer.strip().casefold()
    option_cfs = [option.strip().casefold() for option in options]
    if answer_cf in option_cfs:
        return True
    if any(same_place(answer, option) for option in options):
        return True
    if any(answer_cf in option_cf for option_cf in option_cfs):
        return True
    for option_cf in option_cfs:
        ratio = difflib.SequenceMatcher(None, answer_cf, option_cf).ratio()
        if ratio >= _FUZZY_ACCEPT:
            return True
    city = answer_cf.split(",")[0].split()
    if not city:
        return False
    city_re = re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, city)) + r"(?!\w)")
    return any(city_re.search(" ".join(option_cf.split())) for option_cf in option_cfs)


def _country_option(location: str, country: str, options: list[str]) -> str | None:
    """The one option that is `country`, for a residence question the stored
    `location` matched nothing in, or None: when the list comes near the
    location, when no option or two or more are the country. Equality only:
    the same text ignoring case, or the same place (same_place)."""
    if _comes_near(location, options):
        return None
    country_cf = country.strip().casefold()
    hits = [
        option
        for option in options
        if option.strip().casefold() == country_cf or same_place(country, option)
    ]
    return hits[0] if len(hits) == 1 else None


def values_match(expected: str, readback: str) -> bool:
    """True if `readback` (what the browser reads after filling) matches
    `expected` (what we asked it to fill), tolerating whitespace/case
    differences and phone-number formatting differences."""
    if not readback.strip() and expected.strip():
        return False

    norm_expected = _WHITESPACE_RE.sub(" ", expected).strip().casefold()
    norm_readback = _WHITESPACE_RE.sub(" ", readback).strip().casefold()
    if norm_expected == norm_readback:
        return True

    expected_digits = "".join(_DIGITS_RE.findall(expected))
    readback_digits = "".join(_DIGITS_RE.findall(readback))
    if len(expected_digits) >= 7 and len(readback_digits) >= 7:
        return expected_digits == readback_digits

    return False
