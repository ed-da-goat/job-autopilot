from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def load_fixture_json(name: str) -> dict | list:
    return json.loads(load_fixture(name))


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture(autouse=True)
def _isolate_search_preferences(monkeypatch, tmp_path: Path):
    # The suite must be deterministic on the template defaults, never the
    # operator's gitignored data/search-preferences.local.toml.
    monkeypatch.setattr(
        "jobhunt.eligibility.LOCAL_PREFERENCES_PATH",
        tmp_path / "search-preferences.local.toml",
    )


class MockResponse:
    """Minimal stand-in for requests.Response used across source parse tests."""

    def __init__(self, status_code: int = 200, json_data=None, text: str = "", headers=None):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._json_data is None:
            raise ValueError("no json configured on MockResponse")
        return self._json_data
