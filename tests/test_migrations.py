from __future__ import annotations

import sqlite3

from jobhunt import db


def test_v0_database_upgrades_without_losing_funnel_history(tmp_path):
    path = tmp_path / "production-copy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE postings (
          id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_posting_id TEXT NOT NULL,
          company TEXT, title TEXT, location TEXT, url TEXT, posted_at TEXT, raw_json TEXT,
          first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active', UNIQUE(source, source_posting_id)
        );
        CREATE TABLE funnel (
          id INTEGER PRIMARY KEY, posting_id INTEGER NOT NULL UNIQUE, stage TEXT NOT NULL,
          tier TEXT, referral_path TEXT, next_action TEXT, next_action_date TEXT, notes TEXT,
          applied_at TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE funnel_events (
          id INTEGER PRIMARY KEY, posting_id INTEGER NOT NULL, from_stage TEXT,
          to_stage TEXT NOT NULL, note TEXT, at TEXT NOT NULL
        );
        INSERT INTO postings
          (id, source, source_posting_id, company, title, first_seen_at, last_seen_at)
          VALUES (7, 'greenhouse', 'legacy-7', 'Acme', 'Engineer', 'old', 'old');
        INSERT INTO funnel
          (posting_id, stage, created_at, updated_at) VALUES (7, 'qualified', 'old', 'old');
        INSERT INTO funnel_events
          (posting_id, from_stage, to_stage, at) VALUES (7, 'harvested', 'qualified', 'old');
        PRAGMA user_version=0;
        """
    )
    legacy.commit()
    legacy.close()

    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM funnel_events").fetchone()[0] == 1
        assert conn.execute("SELECT stage FROM funnel WHERE posting_id=7").fetchone()[0] == "qualified"
        posting_columns = {row[1] for row in conn.execute("PRAGMA table_info(postings)")}
        funnel_columns = {row[1] for row in conn.execute("PRAGMA table_info(funnel)")}
        score_columns = {row[1] for row in conn.execute("PRAGMA table_info(scores)")}
        assert "discovered_after_baseline" in posting_columns
        assert {"pass_reason", "pass_detail"} <= funnel_columns
        assert "score_evidence" in score_columns
        assert conn.execute("SELECT baseline_at FROM source_state WHERE source='greenhouse'").fetchone()[0]
    finally:
        conn.close()


def test_db_stamped_v3_still_gets_the_drafts_check_widened(tmp_path):
    """run_log and the drafts CHECK widening landed as separate commits that both
    claimed SCHEMA_VERSION 3. A database stamped by the first would early-return
    past the second forever, so it would reject kind='resume' permanently."""
    path = tmp_path / "stamped-v3.db"
    conn = db.connect(path)
    conn.executescript(
        """
        DROP TABLE drafts;
        CREATE TABLE drafts (
            id INTEGER PRIMARY KEY,
            posting_id INTEGER NOT NULL REFERENCES postings(id),
            kind TEXT NOT NULL CHECK(kind IN ('tailoring','cover','outreach')),
            content TEXT,
            model TEXT,
            tokens_in INTEGER,
            tokens_out INTEGER,
            cost_usd REAL,
            edited INTEGER DEFAULT 0,
            created_at TEXT
        );
        PRAGMA user_version=3;
        """
    )
    conn.commit()
    conn.close()

    conn = db.connect(path)
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='drafts'"
        ).fetchone()[0]
        assert "'resume'" in sql
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        conn.close()


def test_db_stamped_v4_gains_spend_ledger_job_and_kind_columns(tmp_path):
    """A database created at schema v4 (before spend_ledger.job/kind existed) must
    gain both columns, additive and nullable, on reopen."""
    path = tmp_path / "stamped-v4.db"
    conn = db.connect(path)
    conn.executescript(
        """
        DROP TABLE spend_ledger;
        CREATE TABLE spend_ledger (
            id INTEGER PRIMARY KEY,
            draft_id INTEGER REFERENCES drafts(id),
            day TEXT NOT NULL,
            cost_usd REAL NOT NULL,
            at TEXT NOT NULL
        );
        PRAGMA user_version=4;
        """
    )
    conn.commit()
    conn.close()

    conn = db.connect(path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(spend_ledger)")}
        assert {"job", "kind"} <= columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        conn.close()
