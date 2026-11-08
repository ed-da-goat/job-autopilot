"""Load the scoring rubric config. Pure stdlib — no yaml.

The shipped `jobhunt/rubric.json` is tuned toward finance x AI. A local, gitignored
`data/rubric.local.json` replaces it when present (copy `data/rubric.local.example.json`).
Either file must pass `validate_rubric`; a bad override fails loudly, never falls back.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DEFAULT_RUBRIC_PATH = Path(__file__).resolve().parent / "rubric.json"
LOCAL_RUBRIC_PATH = Path(__file__).resolve().parent.parent / "data" / "rubric.local.json"

_COMPONENTS = ("degree_fit", "skill_match", "seniority_fit", "founder_leverage", "company_signal")
_BONUSES = ("degree_language_bonus", "early_grad_bonus", "finance_hybrid_bonus")
_REQUIRED_KEYS = (
    "version", "changelog", "auto_categories", "components", "unknown_midpoints",
    "category_bonus", "vertical_keywords", "keywords", *_BONUSES,
)
_REQUIRED_KEYWORDS = (
    "hard_filter_intern", "hard_filter_apprentice", "target_role_titles", "degree_no_required",
    "degree_fields", "degree_stems", "degree_softener", "skill_core", "skill_secondary",
    "seniority_tiers", "seniority_year_tiers", "seniority_default",
)


class RubricError(ValueError):
    """A rubric file is missing, unreadable or does not match the schema."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_rubric(rubric: object) -> None:
    """Raise RubricError unless `rubric` has the shape the scorer reads."""
    if not isinstance(rubric, dict):
        raise RubricError("rubric must be a JSON object")
    missing = [key for key in _REQUIRED_KEYS if key not in rubric]
    if missing:
        raise RubricError(f"rubric is missing keys: {', '.join(missing)}")
    if not _is_int(rubric["version"]):
        raise RubricError("rubric version must be an integer")
    changelog = rubric["changelog"]
    if not isinstance(changelog, dict) or not str(changelog.get(str(rubric["version"]), "")).strip():
        raise RubricError(f"rubric changelog needs an entry for version {rubric['version']}")
    components = rubric["components"]
    if not isinstance(components, dict) or set(components) != set(_COMPONENTS):
        raise RubricError(f"rubric components must be exactly: {', '.join(_COMPONENTS)}")
    if not all(_is_int(v) and v >= 0 for v in components.values()) or sum(components.values()) != 100:
        raise RubricError("rubric component weights must be non-negative integers summing to 100")
    for key in ("unknown_midpoints", "category_bonus"):
        table = rubric[key]
        if not isinstance(table, dict) or not all(_is_int(v) and v >= 0 for v in table.values()):
            raise RubricError(f"rubric {key} must map names to non-negative integers")
    for key in ("degree_fit", "company_signal"):
        if key not in rubric["unknown_midpoints"]:
            raise RubricError(f"rubric unknown_midpoints is missing {key}")
    if not isinstance(rubric["auto_categories"], list):
        raise RubricError("rubric auto_categories must be a list")
    vertical = rubric["vertical_keywords"]
    if not isinstance(vertical, dict) or not all(
        isinstance(v, list) and all(isinstance(k, str) for k in v) for v in vertical.values()
    ):
        raise RubricError("rubric vertical_keywords must map names to lists of strings")
    for key in _BONUSES:
        bonus = rubric[key]
        if not isinstance(bonus, dict) or not _is_int(bonus.get("points")) or not isinstance(
            bonus.get("keywords"), list
        ):
            raise RubricError(f"rubric {key} needs integer points and a keywords list")
    keywords = rubric["keywords"]
    if not isinstance(keywords, dict):
        raise RubricError("rubric keywords must be an object")
    absent = [key for key in _REQUIRED_KEYWORDS if key not in keywords]
    if absent:
        raise RubricError(f"rubric keywords is missing: {', '.join(absent)}")


def rubric_path() -> Path:
    """The local override when it exists, else the shipped rubric."""
    return LOCAL_RUBRIC_PATH if LOCAL_RUBRIC_PATH.exists() else DEFAULT_RUBRIC_PATH


@lru_cache(maxsize=8)
def _load(path: Path) -> dict:
    try:
        rubric = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RubricError(f"cannot read rubric {path}: {exc}") from exc
    try:
        validate_rubric(rubric)
    except RubricError as exc:
        raise RubricError(f"invalid rubric {path}: {exc}") from exc
    return rubric


def load_rubric(path: Path | str | None = None) -> dict:
    """Load, validate and cache the rubric config. With no path: data/rubric.local.json if it
    exists, else the shipped rubric. Cached per-path so tests that point at a fixture rubric
    don't collide with the real one."""
    return _load(Path(path) if path is not None else rubric_path())
