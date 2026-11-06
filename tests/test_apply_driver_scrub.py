from __future__ import annotations

import json
from pathlib import Path

from jobhunt.apply_assist.driver import _scrub_crash_restore


def test_scrub_deletes_sessions_dir(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "Default" / "Sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "Session_1").write_bytes(b"dummy session data")
    cookies = tmp_path / "Default" / "Cookies"
    cookies.write_bytes(b"cookie data")

    _scrub_crash_restore(tmp_path)

    assert not sessions_dir.exists()
    assert cookies.exists()
    assert cookies.read_bytes() == b"cookie data"


def test_scrub_rewrites_crashed_exit_type(tmp_path: Path) -> None:
    default_dir = tmp_path / "Default"
    default_dir.mkdir(parents=True)
    prefs_path = default_dir / "Preferences"
    prefs_data = {
        "profile": {
            "exit_type": "Crashed",
            "name": "Person 1",
        },
        "unrelated_top_level": "keep-me",
    }
    prefs_path.write_text(json.dumps(prefs_data))

    _scrub_crash_restore(tmp_path)

    result = json.loads(prefs_path.read_text())
    assert result["profile"]["exit_type"] == "Normal"
    assert result["profile"]["exited_cleanly"] is True
    assert result["profile"]["name"] == "Person 1"
    assert result["unrelated_top_level"] == "keep-me"


def test_scrub_leaves_clean_profile_alone(tmp_path: Path) -> None:
    default_dir = tmp_path / "Default"
    default_dir.mkdir(parents=True)
    prefs_path = default_dir / "Preferences"
    prefs_data = {
        "profile": {
            "exit_type": "Normal",
            "exited_cleanly": True,
            "name": "Person 1",
        }
    }
    prefs_path.write_text(json.dumps(prefs_data))
    before = prefs_path.read_text()

    _scrub_crash_restore(tmp_path)

    after = prefs_path.read_text()
    assert after == before
    assert not (tmp_path / "Default" / "Sessions").exists()


def test_scrub_survives_corrupt_preferences(tmp_path: Path) -> None:
    default_dir = tmp_path / "Default"
    default_dir.mkdir(parents=True)
    prefs_path = default_dir / "Preferences"
    prefs_path.write_text("{not json")

    _scrub_crash_restore(tmp_path)  # must not raise

    assert prefs_path.read_text() == "{not json"
