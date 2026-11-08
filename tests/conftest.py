from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from jobhunt import eligibility, rubric

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def load_fixture_json(name: str) -> dict | list:
    return json.loads(load_fixture(name))


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


# Hermetic by construction: the suite never reads the operator's gitignored local files
# (data/search-preferences.local.toml, data/rubric.local.json, and so on). Some modules
# freeze the preferences at import time (apply_assist.solve reads OFFICE_CITY_ORDER at
# module level), so the preferences path is pointed at nothing here, before any test
# module imports them, and again around every test.
_NO_LOCAL_DIR = Path(tempfile.mkdtemp(prefix="jobhunt-tests-nolocal-"))
atexit.register(shutil.rmtree, _NO_LOCAL_DIR, ignore_errors=True)

eligibility.LOCAL_PREFERENCES_PATH = _NO_LOCAL_DIR / "search-preferences.local.toml"
eligibility.user_preferences.cache_clear()
rubric.LOCAL_RUBRIC_PATH = _NO_LOCAL_DIR / "rubric.local.json"


REPO_ROOT = Path(__file__).resolve().parents[1]


def operator_file(relative: str) -> Path:
    """An operator's gitignored local file, for the few tests that check it still agrees with
    the code (facts, answers, master resume). Those tests are about the operator's own data, so
    they run only when JOBHUNT_CHECK_LOCAL_FILES=1; otherwise the path is one that does not
    exist and they skip, whatever is (or is not) in data/."""
    if os.environ.get("JOBHUNT_CHECK_LOCAL_FILES") == "1":
        return REPO_ROOT / relative
    return _NO_LOCAL_DIR / relative


def _clear_caches() -> None:
    # Some tests replace user_preferences with a stub; only a real lru_cache has cache_clear.
    for fn in (eligibility.user_preferences, *vars(rubric).values()):
        clear = getattr(fn, "cache_clear", None)
        if clear is not None:
            clear()


# (module, attribute, filename): every module-level path that points at an operator file.
_LOCAL_PATHS = (
    ("jobhunt.eligibility", "LOCAL_PREFERENCES_PATH", "search-preferences.local.toml"),
    ("jobhunt.rubric", "LOCAL_RUBRIC_PATH", "rubric.local.json"),
    ("jobhunt.applypack", "ANSWERS_FILLED_PATH", "answers.local.md"),
    ("jobhunt.autotailor", "FACTS_PATH", "facts.local.md"),
    ("jobhunt.apply_assist.tailor_answers", "FACTS_PATH", "facts.local.md"),
    ("jobhunt.apply_assist.posting_answers", "FACTS_PATH", "facts.local.md"),
    ("jobhunt.apply_assist.posting_answers", "ANSWERS_PATH", "answers.local.md"),
    ("jobhunt.apply_assist.posting_answers", "MASTER_PATH", "master.rm.json"),
)


@pytest.fixture(autouse=True)
def _isolate_local_files(monkeypatch, tmp_path: Path):
    from jobhunt import prep


    # Tests that exercise a local file monkeypatch their own path (after this fixture), so
    # they keep working; everything else sees an operator with no local files at all.
    _clear_caches()
    for module, attribute, filename in _LOCAL_PATHS:
        monkeypatch.setattr(f"{module}.{attribute}", tmp_path / "nolocal" / filename)
    # PROFILE.md falls back to the shipped PROFILE.example.md; point straight at the example.
    # (prep binds its path as a default argument, so the attribute alone would not reach it.)
    example_profile = REPO_ROOT / "PROFILE.example.md"
    monkeypatch.setattr("jobhunt.applypack.PROFILE_PATH", example_profile)
    monkeypatch.setattr("jobhunt.prep.DEFAULT_PROFILE_PATH", example_profile)
    monkeypatch.setattr(prep._load_profile, "__defaults__", (example_profile,))
    yield
    _clear_caches()


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
