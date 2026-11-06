"""Fact-drift guardrails for resume rendering and tailoring.

Two independent checks, both fail-closed:

1. `validate_tailoring()` — structural. Every bullet id a model hands back must
   already exist in `resume.yaml`. This is what stops a tailoring call
   inventing an employer, a date, or a signed client: the model may only
   reference, reorder, and re-word ids it was given.

2. `scan_banned_phrases()` — lexical. Runs over the *rendered* text, so it
   catches drift regardless of which layer introduced it. "sold"/"acquired"/
   "exit" would each be a completed-sale claim (the candidate's venture is pre-revenue with no
   signed client); "CPA" would attribute a credential the candidate does not hold;
   em dashes read as machine-written and are banned in prose under his name.
"""

from __future__ import annotations

import re

BANNED_PATTERNS: dict[str, re.Pattern[str]] = {
    "sold": re.compile(r"\bsold\b", re.IGNORECASE),
    "acquired": re.compile(r"\bacquired?\b|\bacquisitions?\b", re.IGNORECASE),
    "exit": re.compile(r"\bexits?\b|\bexited\b", re.IGNORECASE),
    "CPA": re.compile(r"\bCPA\b|\bcertified public accountant\b", re.IGNORECASE),
    "em dash": re.compile(r"—"),
}


class ResumeValidationError(ValueError):
    """Raised when a tailoring payload or a rendered resume fails a guardrail."""


def scan_banned_phrases(text: str) -> list[str]:
    """Return the names of every banned pattern present in `text`."""
    return [name for name, pattern in BANNED_PATTERNS.items() if pattern.search(text)]

def assert_clean(text: str, *, where: str) -> None:
    """Fail closed if `text` carries a banned claim or an em dash."""
    hits = scan_banned_phrases(text)
    if hits:
        raise ResumeValidationError(
            f"{where} contains banned phrase(s): {', '.join(hits)}. "
            "Bullets are facts: the candidate's venture is pre-revenue with no signed client, "
            "the candidate is not a CPA, and em dashes are banned in their prose."
        )


def validate_tailoring(payload: dict, known_ids: set[str]) -> dict:
    """Check a model tailoring response against the facts in `resume.yaml`.

    Expected shape::

        {summary, bullet_order: [id], bullet_rewrites: {id: text},
         dropped: [id], rationale}

    Returns the normalized payload. Raises `ResumeValidationError` on any
    unknown id, wrong type, or banned phrase.
    """
    if not isinstance(payload, dict):
        raise ResumeValidationError(f"tailoring payload must be an object, got {type(payload).__name__}")

    summary = payload.get("summary", "")
    if not isinstance(summary, str):
        raise ResumeValidationError("`summary` must be a string")

    bullet_order = payload.get("bullet_order", [])
    if not isinstance(bullet_order, list) or not all(isinstance(i, str) for i in bullet_order):
        raise ResumeValidationError("`bullet_order` must be a list of bullet id strings")

    dropped = payload.get("dropped", [])
    if not isinstance(dropped, list) or not all(isinstance(i, str) for i in dropped):
        raise ResumeValidationError("`dropped` must be a list of bullet id strings")

    rewrites = payload.get("bullet_rewrites", {})
    if not isinstance(rewrites, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in rewrites.items()
    ):
        raise ResumeValidationError("`bullet_rewrites` must be an object of {bullet id: text}")

    unknown = sorted(
        ({*bullet_order} | {*dropped} | {*rewrites}) - known_ids
    )
    if unknown:
        raise ResumeValidationError(
            f"tailoring referenced bullet id(s) absent from resume.yaml: {', '.join(unknown)}. "
            "Tailoring may reorder and re-word existing bullets, never invent new ones."
        )

    duplicates = sorted({i for i in bullet_order if bullet_order.count(i) > 1})
    if duplicates:
        raise ResumeValidationError(f"`bullet_order` repeats bullet id(s): {', '.join(duplicates)}")

    assert_clean(summary, where="tailored summary")
    for bullet_id, text in rewrites.items():
        assert_clean(text, where=f"rewrite of bullet {bullet_id}")

    return {
        "summary": summary,
        "bullet_order": bullet_order,
        "bullet_rewrites": rewrites,
        "dropped": dropped,
        "rationale": payload.get("rationale", ""),
    }
