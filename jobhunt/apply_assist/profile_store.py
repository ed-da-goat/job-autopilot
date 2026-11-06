"""SQLite-backed storage for stored application answers (application_answers).

Callers pass an already-open sqlite3.Connection (see jobhunt.db.connect).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from jobhunt.apply_assist.keys import SEMANTIC_KEYS


# Search preferences predate the browser assistant's semantic-key taxonomy.
# Keep compatibility at the boundary while application_answers remains the
# single operational source of truth.
PREFERENCE_KEY_ALIASES: dict[str, str] = {
    "earliest_start": "start_date",
    "portfolio": "website",
    "how_did_you_hear": "how_heard",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_key(key: str) -> None:
    if key not in SEMANTIC_KEYS:
        raise ValueError(f"unknown semantic key: {key!r}")


def get_answers(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT semantic_key, value FROM application_answers"
    ).fetchall()
    return {row["semantic_key"]: row["value"] for row in rows}


def answer_for_preference(answers: dict[str, str], key: str) -> str:
    return answers.get(PREFERENCE_KEY_ALIASES.get(key, key), "")


def set_answer(conn: sqlite3.Connection, key: str, value: str) -> None:
    _validate_key(key)
    now = _now()
    conn.execute(
        """INSERT INTO application_answers (semantic_key, value, created_at, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(semantic_key) DO UPDATE SET
               value=excluded.value,
               updated_at=excluded.updated_at""",
        (key, value, now, now),
    )
    conn.commit()


def delete_answer(conn: sqlite3.Connection, key: str) -> None:
    _validate_key(key)
    conn.execute("DELETE FROM application_answers WHERE semantic_key = ?", (key,))
    conn.commit()


def list_answers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM application_answers ORDER BY semantic_key"
    ).fetchall()
