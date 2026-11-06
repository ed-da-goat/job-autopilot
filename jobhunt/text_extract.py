"""Pull a scorable (title, description) pair out of a posting row's raw_json.

Each source stores its description under a different key (or none at all);
this module is the single place that knows the per-source shape. Everything
downstream in scoring.py works off plain lowercased text.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3

from jobhunt import posting_guard

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str | None) -> str:
    """Unescape entities, drop tags, collapse whitespace. Handles the
    double-escaped HTML some ATS APIs (Greenhouse) return."""
    if not text:
        return ""
    unescaped = html.unescape(text)
    # Hidden-to-the-reader elements, comments and script/style blocks go
    # before tags are stripped, so neither the rubric nor any LLM prompt
    # downstream ever sees text a human applicant could not.
    visible, _hidden = posting_guard.drop_hidden_html(unescaped)
    no_tags = _TAG_RE.sub(" ", visible)
    cleaned = html.unescape(no_tags)
    return _WS_RE.sub(" ", cleaned).strip()


def _lever_description(raw: dict) -> str:
    parts = [raw.get("descriptionPlain") or ""]
    for item in raw.get("lists") or []:
        if not isinstance(item, dict):
            continue
        parts.append(item.get("text") or "")
        parts.append(strip_html(item.get("content")))
    additional = raw.get("additionalPlain") or ""
    if additional:
        parts.append(additional)
    return "\n".join(p for p in parts if p)


def _extract_description(source: str, raw: dict) -> str:
    if source == "greenhouse":
        return strip_html(raw.get("content"))
    if source == "lever":
        return _lever_description(raw)
    if source == "ashby":
        plain = raw.get("descriptionPlain")
        if plain:
            return plain
        return strip_html(raw.get("descriptionHtml"))
    if source == "hn":
        return strip_html(raw.get("comment_text"))
    if source == "simplify":
        return ""
    return ""


def extract_text(posting_row: sqlite3.Row | dict) -> tuple[str, str, bool]:
    """Return (title, cleaned_description_lowercased, has_description).

    title is returned lowercased too, ready for keyword matching. Falls back
    to empty description (has_description=False) on any parse hiccup rather
    than raising — a bad raw_json blob shouldn't crash a scoring sweep.
    """
    title = posting_row["title"] or ""
    source = posting_row["source"] or ""
    raw_json = posting_row["raw_json"]

    description = ""
    if raw_json:
        try:
            raw = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            raw = None
        if isinstance(raw, dict):
            description = _extract_description(source, raw)

    has_description = bool(description.strip())
    return title.lower(), description.lower(), has_description
