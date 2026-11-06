"""Load the committed rubric.json config. Pure stdlib — no yaml."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DEFAULT_RUBRIC_PATH = Path(__file__).resolve().parent / "rubric.json"


@lru_cache(maxsize=8)
def load_rubric(path: Path | str = DEFAULT_RUBRIC_PATH) -> dict:
    """Load and cache the rubric config. Cached per-path so tests that point
    at a fixture rubric don't collide with the real one."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
