"""JD-summary pre-pass for tailor_answers.py: turns a posting's raw JD text
into a small structured object once per posting, cached, so every question
drafted for that posting draws on a compact summary instead of the raw JD
text being re-sent per question.

Write-through cache over `jd_summaries` (jobhunt/db.py), same pattern as
tailor_answers.py's own cache over `tailored_answers`: keyed on posting_id,
read before any LLM call, written on success only. A cache hit makes zero
model calls. A miss runs Haiku through `claude -p` on the candidate's subscription
(jobhunt/connectors/claude_cli.py): no metered spend, no budget needed.

This describes what the EMPLOYER wants (required/nice-to-have skills, their
own vocabulary, seniority signal) — it must never become a source of facts
about the candidate. Parsing is defensive: any response that isn't valid
JSON, isn't an object, or is missing an expected key returns None rather
than raising, so the caller's existing fallback (drafting without a JD
summary) always keeps working.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from jobhunt import posting_guard, text_extract
from jobhunt.connectors import claude_cli as claude_cli_module
from jobhunt.spend import RunBudget

# First ~6000 chars of the JD text sent to the model: a summary pass does
# not need the whole posting.
MAX_JD_CHARS = 6000
_MAX_TOKENS = 700
# Runs on the subscription through `claude -p`; Haiku is plenty for a summary.
SUMMARY_MODEL = "haiku"

# Constraints are stated as HARD RULES with an explicit forbidden-value list
# and a worked example: the local 7b model ignores soft guidance and, given
# only "the employer's own phrasing for technologies/skills", returned culture
# copy ("real-world impact", "ruthless commitment to truth-seeking") instead
# of tech terms. `keywords` is the field that does the ATS matching, so it
# carries the strictest rule.
_SYSTEM_PROMPT = (
    "You read ONE job description and extract what the employer is asking "
    "for, nothing else. Return STRICT JSON — a single JSON object, no "
    "markdown fences, no prose before or after — with exactly these keys:\n"
    '"required_skills": array of AT MOST 10 SHORT noun phrases, 2-6 words '
    "each, naming a must-have skill or qualification. HARD RULE: never copy "
    "a sentence from the posting — compress it. Write "
    '"end-to-end project ownership", NOT "ability to own projects '
    'end-to-end, from first conversation with a customer to shipping".\n'
    '"nice_to_have": array of AT MOST 8 short noun phrases, same rules, for '
    "preferred-but-not-required items.\n"
    '"keywords": array of AT MOST 12 CONCRETE technologies, programming '
    "languages, frameworks, libraries, tools, platforms, cloud services, or "
    "named technical methods that appear LITERALLY in the text, in the "
    "employer's own spelling. HARD RULE: every entry must be a thing you "
    "could install, import, run, or be certified in. Values, culture, "
    "mission, and soft-skill phrasing are FORBIDDEN in this field — entries "
    'like "real-world impact", "truth-seeking", "mission-critical '
    'problems", "ownership", "autonomy", or "collaboration" are WRONG and '
    "must be omitted. Employee benefits, compensation and perks are also "
    "FORBIDDEN here even though the posting mentions them — "
    '"life insurance", "401k plan", "PTO", "equity", "dental" describe what '
    "the employer PAYS you, not what you use to DO the job. Every entry must "
    "be something the person in this role would actually work with. If the "
    "posting names no concrete technologies, return an empty array.\n"
    '"seniority_signal": one short string, e.g. "new grad", "early career", '
    '"3+ years", "senior".\n'
    '"one_line": one sentence summarizing the role.\n\n'
    "Shape of a CORRECT keywords value — these are ILLUSTRATIVE ONLY and "
    "almost certainly do NOT belong in your answer; use only technologies "
    "the posting itself names:\n"
    '["<language named in the posting>", "<framework named in the posting>"]'
    "\n\n"
    "Extract only what the text states; do not infer a candidate's fit or "
    "invent requirements the text doesn't contain."
)


def _filter_keywords_to_source(keywords: list[str], jd_text: str) -> list[str]:
    """Drop any keyword that does not appear literally in the JD text.

    The prompt demands literal presence; this enforces it deterministically
    rather than trusting the model to comply. Verified necessary: given a
    worked example in the prompt, the local 7b emitted React, PostgreSQL,
    Kubernetes and AWS for a posting (2892) containing none of them — it
    copied the example. These keywords feed the answer-drafting prompt, so a
    fabricated technology here becomes a skill claimed on an application.
    """
    low = jd_text.lower()
    kept: list[str] = []
    for raw in keywords:
        keyword = raw.strip()
        if not keyword:
            continue
        if keyword.lower() in low:
            kept.append(keyword)
            continue
        # "TypeScript/JavaScript" — keep whichever halves are really present.
        parts = [part.strip() for part in keyword.split("/") if part.strip()]
        if len(parts) > 1:
            kept.extend(part for part in parts if part.lower() in low)
    deduped: list[str] = []
    seen: set[str] = set()
    for keyword in kept:
        if keyword.lower() not in seen:
            seen.add(keyword.lower())
            deduped.append(keyword)
    return deduped

_REQUIRED_KEYS = (
    "required_skills",
    "nice_to_have",
    "keywords",
    "seniority_signal",
    "one_line",
)
_LIST_KEYS = ("required_skills", "nice_to_have", "keywords")
_STR_KEYS = ("seniority_signal", "one_line")


def _parse_summary(text: str | None) -> dict | None:
    """Strict-JSON parse of the model's response into the 5-key summary
    shape. Returns None — never raises — on anything that doesn't match:
    not JSON, not an object, a missing key, or a key of the wrong type."""
    if not text or not text.strip():
        return None
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
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not set(_REQUIRED_KEYS).issubset(payload.keys()):
        return None
    for key in _LIST_KEYS:
        value = payload[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return None
    for key in _STR_KEYS:
        if not isinstance(payload[key], str):
            return None
    return {key: payload[key] for key in _REQUIRED_KEYS}


def _build_prompt(title: str, jd_text: str) -> str:
    return f"Job title: {title}\n\nJob description:\n{jd_text}"


def summarize_posting(
    conn: sqlite3.Connection, posting_id: int, budget: RunBudget | None
) -> dict | None:
    """Summarize posting_id's JD into required_skills/nice_to_have/keywords/
    seniority_signal/one_line, cached in `jd_summaries`. A cache hit returns
    the stored dict with zero LLM calls. Returns None, never raises, when
    there's no posting/description to summarize, the CLI cannot run on the
    subscription, or the response isn't the expected strict-JSON shape. None
    means "the drafter falls back to its prompt without a JD summary," which
    must keep working. The call is free (subscription), so `budget` is
    optional: when given, it only gets a 0.0 settle so the run's books show
    the call."""
    cached = conn.execute(
        "SELECT summary_json FROM jd_summaries WHERE posting_id = ?", (posting_id,)
    ).fetchone()
    if cached is not None:
        try:
            payload = json.loads(cached["summary_json"])
        except (json.JSONDecodeError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    posting_row = conn.execute("SELECT * FROM postings WHERE id = ?", (posting_id,)).fetchone()
    if posting_row is None:
        return None
    title, description, has_description = text_extract.extract_text(posting_row)
    if not has_description:
        return None

    description = posting_guard.guard_description(description, posting_id=posting_id).text
    prompt = _build_prompt(title, description[:MAX_JD_CHARS])

    try:
        response = claude_cli_module.complete(
            prompt, model=SUMMARY_MODEL, max_tokens=_MAX_TOKENS, system=_SYSTEM_PROMPT
        )
        parsed = _parse_summary(response["text"])
    except Exception:  # noqa: BLE001 — a bad response or an unavailable CLI must not kill the caller
        return None
    if budget is not None:
        budget.settle(0.0, kind="jd_summary")

    if parsed is None:
        return None

    # Enforce the prompt's literal-presence rule in code, not on trust.
    parsed["keywords"] = _filter_keywords_to_source(parsed["keywords"], description)

    now = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO jd_summaries
               (posting_id, summary_json, model, created_at)
           VALUES (?, ?, ?, ?)""",
        (posting_id, json.dumps(parsed), response.get("model"), now),
    )
    conn.commit()
    return parsed
