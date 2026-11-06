"""Dataclasses shared between the detect/classify/resolve/digest steps."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class FieldSpec:
    """One form field as scraped from the page (no live element reference)."""

    selector: str
    kind: str  # text|email|tel|url|number|textarea|select|radio|checkbox|file|unknown
    label: str = ""
    name: str = ""
    elem_id: str = ""
    autocomplete: str = ""
    placeholder: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Resolution:
    """What we decided to do with a field: fill it, upload to it, ask the
    human, or skip it outright."""

    field: FieldSpec
    semantic_key: str | None
    action: str  # fill|upload|ask|skip|check
    value: str | None = None
    reason: str = ""
    draft: bool = False  # True when `value` was drafted by tailor_answers.py,
    # never confirmed by the candidate or classify()/mapper.py — flagged for review.
    # The one other answer fill.py may try, and only on a react-select whose menu
    # has no option for `value` at all: the stored country, for a question that
    # asks where the applicant is (solve._residence_country). None everywhere else.
    fallback_value: str | None = None
    # A pick rule for a react-select whose options solve.py cannot see (a Greenhouse
    # degree, discipline or confirmation list): fill.py opens the menu, reads the
    # options and commits the one solve.pick_option(option_rule, option_args, options)
    # names, and fails the field when it names none. Empty everywhere else.
    option_rule: str = ""
    option_args: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FillOutcome:
    """Result of actually attempting to fill/upload a field in the browser
    layer, including the readback value used to verify the fill stuck."""

    field: FieldSpec
    semantic_key: str | None
    value: str
    readback: str
    committed: bool
    error: str = ""
    # The react-select option text this fill committed with Tab ("" when no Tab
    # was pressed). Only fill.py's read-back check uses it.
    picked: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TriageDigest:
    """Full summary of one application-form pass, ready to render."""

    posting_id: int
    url: str
    ats: str | None
    filled: list[FillOutcome]
    residual: list[Resolution]
    skipped: list[Resolution]
    errors: list[str]
    # Proposed-but-not-typed AI answers awaiting per-draft approval; never
    # typed into the form without explicit human confirmation.
    drafts: list[Resolution] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())
