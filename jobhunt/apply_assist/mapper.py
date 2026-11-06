"""LLM field-mapper with a write-through cache over `field_key_map`.

Fills the gap `classify()` leaves: a field's label/name/id doesn't match any
regex, but a human (or a model) can still tell what it's asking for. This
module NEVER submits, fills, POSTs, or auto-checks anything — it only names
a semantic key for a field. The actual value comes from the existing answers
store; `solve.py` still refuses to fill anything without a stored answer.

Batched: one OpenRouter call per posting covers every classify()-missed
field on that posting, never one call per field. Every answer — including a
model's "no key fits" — is cached by fingerprint, so a rerun over the same
fields costs $0 and makes zero network calls.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from jobhunt.apply_assist.keys import SEMANTIC_KEYS, SENSITIVE_KEYS, classify
from jobhunt.apply_assist.schema import FieldSpec
from jobhunt.connectors import openrouter as openrouter_module
from jobhunt.connectors.openrouter import SpendGateError
from jobhunt.spend import RunBudget, SpendCapError

# Keys a model can never assign, no matter what it returns — these stay
# `ask` forever, enforced on the OUTPUT side (not just the prompt), so a
# misbehaving or adversarial response can't route around them.
_ALWAYS_REFUSE_KEYS: frozenset[str] = SENSITIVE_KEYS | {"resume", "cover_letter"}
_REFUSED_KINDS: frozenset[str] = frozenset({"checkbox", "file"})

_TRAILING_STARS_RE = re.compile(r"\*+$")
_WHITESPACE_RE = re.compile(r"\s+")
_DIGIT_RUN_RE = re.compile(r"\d+")

_SYSTEM_PROMPT = (
    "You name form fields on job-application pages. For each field, choose the "
    "single best-fitting semantic key from the allowed list, or null if none fits. "
    "Never guess for fields about employment history essays, privacy notices, "
    "eligibility yes/no questions, or anything not clearly one of the allowed keys — "
    "return null instead. Return the key only if the field asks for EXACTLY what "
    "that key holds. If the field is broader or narrower than the key (for example "
    "a country field versus a city/region `location`), return null. Return null "
    "rather than guessing a near match. A null costs nothing; a wrong key types the "
    "wrong value into a real application. Respond with ONLY a JSON array, no prose, "
    "no markdown fences: "
    '[{"fingerprint": "...", "semantic_key": "..." or null, "confidence": 0.0-1.0}, ...] '
    "— one object per field, in the order given."
)


def _normalize(text: str) -> str:
    normalized = (text or "").casefold()
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    normalized = _TRAILING_STARS_RE.sub("", normalized).strip()
    return normalized


def fingerprint(field: FieldSpec) -> str:
    """Stable identity for a field across postings/hosts, independent of
    per-board numeric ids. Host is deliberately excluded so one Ashby
    answer generalizes across boards. Digit runs are stripped from
    elem_id (Greenhouse mints ids like question_12198097007 per-board), but
    the label is kept as-is — it's what actually carries the meaning."""
    label = _normalize(field.label)
    name = _normalize(field.name)
    elem_id = _normalize(_DIGIT_RUN_RE.sub("", field.elem_id or ""))
    kind = _normalize(field.kind)
    basis = f"{label}|{name}|{elem_id}|{kind}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _refuse(kind: str, semantic_key: str | None) -> str | None:
    """Enforced on the OUTPUT side: no model response can assign a sensitive
    key, a resume/cover_letter key, or any key at all to a checkbox/file
    field. Those stay `ask` forever regardless of what the model says."""
    if kind in _REFUSED_KINDS:
        return None
    if semantic_key in _ALWAYS_REFUSE_KEYS:
        return None
    return semantic_key


@dataclass
class MapperResult:
    """Outcome of one map_unknown() call. `status` must let the caller tell
    "nothing needed mapping" apart from "the mapper couldn't run" — those are
    never the same thing."""

    overrides: dict[str, str]
    status: str  # ok|no_unknown_fields|all_cached|no_budget|gate_closed|over_cap|error
    cache_hits: int
    misses: int
    mapped: int
    cost_usd: float
    detail: str | None = None


def _build_prompt(to_query: dict[str, FieldSpec], host: str | None) -> str:
    items = [
        {
            "fingerprint": fp,
            "label": f.label,
            "name": f.name,
            "placeholder": f.placeholder,
            "kind": f.kind,
        }
        for fp, f in to_query.items()
    ]
    return (
        f"Allowed semantic keys: {sorted(SEMANTIC_KEYS)}\n"
        f"Host: {host or 'unknown'}\n"
        f"Fields:\n{json.dumps(items, indent=2)}"
    )


def _parse_response(
    text: str, expected_fps: set[str]
) -> dict[str, tuple[str | None, float | None]]:
    if text is None or not text.strip():
        raise ValueError("field-mapper returned an empty response")

    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"field-mapper response was not JSON: {text[:120]!r}") from exc

    if not isinstance(payload, list):
        raise ValueError("field-mapper response was not a JSON array")

    results: dict[str, tuple[str | None, float | None]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        fp = item.get("fingerprint")
        if fp not in expected_fps:
            continue
        key = item.get("semantic_key")
        if key not in SEMANTIC_KEYS:
            key = None
        confidence = item.get("confidence")
        if not isinstance(confidence, int | float) or isinstance(confidence, bool):
            confidence = None
        results[fp] = (key, float(confidence) if confidence is not None else None)
    return results


def map_unknown(
    conn: sqlite3.Connection,
    fields: list[FieldSpec],
    budget: RunBudget | None,
    *,
    host: str | None = None,
    client: Callable[..., dict] | None = None,
) -> MapperResult:
    """Name a semantic key for every field classify() couldn't. Caller's job
    to only pass classify()-None fields in; asserted here defensively."""
    assert all(
        classify(f) is None for f in fields
    ), "map_unknown received a field classify() already resolved"

    if not fields:
        return MapperResult(
            overrides={}, status="no_unknown_fields", cache_hits=0, misses=0, mapped=0, cost_usd=0.0
        )

    by_fingerprint: dict[str, FieldSpec] = {}
    for f in fields:
        by_fingerprint.setdefault(fingerprint(f), f)

    overrides: dict[str, str] = {}
    cache_hits = 0
    to_query: dict[str, FieldSpec] = {}

    for fp, f in by_fingerprint.items():
        row = conn.execute(
            "SELECT semantic_key FROM field_key_map WHERE fingerprint = ?", (fp,)
        ).fetchone()
        if row is None:
            to_query[fp] = f
            continue
        cache_hits += 1
        cached_key = row["semantic_key"]
        if cached_key:
            overrides[fp] = cached_key

    if not to_query:
        return MapperResult(
            overrides=overrides,
            status="all_cached",
            cache_hits=cache_hits,
            misses=0,
            mapped=len(overrides),
            cost_usd=0.0,
        )

    if budget is None:
        return MapperResult(
            overrides=overrides,
            status="no_budget",
            cache_hits=cache_hits,
            misses=len(to_query),
            mapped=len(overrides),
            cost_usd=0.0,
        )

    call = client or openrouter_module.complete
    prompt = _build_prompt(to_query, host)
    max_tokens = min(4000, 200 + 80 * len(to_query))

    try:
        estimated_cost = openrouter_module.estimate_max_cost(prompt, max_tokens=max_tokens)
        budget.reserve(estimated_cost)
    except SpendCapError as exc:
        # reserve() raises before touching `_reserved` on this path, so there
        # is nothing to release — the run-cap/daily-cap ceiling was never
        # crossed by this call.
        return MapperResult(
            overrides=overrides,
            status="over_cap",
            cache_hits=cache_hits,
            misses=len(to_query),
            mapped=len(overrides),
            cost_usd=0.0,
            detail=str(exc),
        )

    try:
        response = call(prompt, max_tokens=max_tokens, system=_SYSTEM_PROMPT)
        parsed = _parse_response(response["text"], set(to_query))
        actual_cost = float(response.get("cost_usd", 0.0))
        budget.settle(actual_cost, kind="field_map")
    except SpendGateError as exc:
        # reserve() above already succeeded, so the reservation must be
        # released here (settle 0) or every later posting in the batch would
        # see an inflated `_reserved` and spuriously trip over_cap.
        budget.settle(0.0, kind="field_map")
        return MapperResult(
            overrides=overrides,
            status="gate_closed",
            cache_hits=cache_hits,
            misses=len(to_query),
            mapped=len(overrides),
            cost_usd=0.0,
            detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — one bad response must not kill the queue run
        budget.settle(0.0, kind="field_map")
        return MapperResult(
            overrides=overrides,
            status="error",
            cache_hits=cache_hits,
            misses=len(to_query),
            mapped=len(overrides),
            cost_usd=0.0,
            detail=f"{type(exc).__name__}: {exc}",
        )

    now = datetime.now(UTC).isoformat()

    # First pass: resolve each field's refused/safe key for this batch.
    batch: dict[str, tuple[str | None, float | None]] = {}
    for fp, f in to_query.items():
        raw_key, confidence = parsed.get(fp, (None, None))
        batch[fp] = (_refuse(f.kind, raw_key), confidence)

    # Ambiguity guard: one stored answer cannot be correct for two distinct
    # fields, so if the model assigned the same semantic key to more than one
    # field in this batch, that key is unusable for all of them here. Drop
    # it (field falls back to `ask`) instead of writing a value that would be
    # wrong for at least one of the fields. Deliberately NOT cached to
    # field_key_map — a key that's ambiguous on this form may be unambiguous
    # on another, so a different board still gets to evaluate it fresh.
    key_counts: dict[str, int] = {}
    for safe_key, _confidence in batch.values():
        if safe_key is not None:
            key_counts[safe_key] = key_counts.get(safe_key, 0) + 1
    ambiguous_keys = {key for key, count in key_counts.items() if count > 1}

    for fp, f in to_query.items():
        safe_key, confidence = batch[fp]
        if safe_key in ambiguous_keys:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO field_key_map
                   (fingerprint, semantic_key, label, host, source, confidence, created_at)
               VALUES (?, ?, ?, ?, 'llm', ?, ?)""",
            (fp, safe_key, f.label, host, confidence, now),
        )
        if safe_key:
            overrides[fp] = safe_key
    conn.commit()

    return MapperResult(
        overrides=overrides,
        status="ok",
        cache_hits=cache_hits,
        misses=len(to_query),
        mapped=len(overrides),
        cost_usd=actual_cost,
    )
