"""SQLite connection + schema management for the job-hunt DB.

WAL mode, one connection per call site (short-lived), schema created
idempotently via CREATE TABLE IF NOT EXISTS.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "jobhunt.db"
BACKUP_DIR = DEFAULT_DB_PATH.parent / "backups"
SCHEMA_VERSION = 8


def _default_resume_path() -> str:
    from jobhunt import eligibility

    return f"resume/{eligibility.resume_basename()}_2026_Founder_Engineer.pdf"


SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    category TEXT,
    platform TEXT,
    slug TEXT,
    verified TEXT,
    why_fit TEXT,
    watchlist INTEGER DEFAULT 0,
    added_at TEXT
);

CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_posting_id TEXT NOT NULL,
    company TEXT,
    title TEXT,
    location TEXT,
    url TEXT,
    posted_at TEXT,
    raw_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    discovered_after_baseline INTEGER NOT NULL DEFAULT 0,
    duplicate_of INTEGER,
    UNIQUE(source, source_posting_id)
);

CREATE TABLE IF NOT EXISTS sweeps (
    id INTEGER PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    source TEXT,
    boards_attempted INTEGER,
    boards_ok INTEGER,
    postings_seen INTEGER,
    postings_new INTEGER,
    errors TEXT
);

CREATE INDEX IF NOT EXISTS idx_postings_status ON postings(status);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings(id),
    rubric_version INTEGER NOT NULL,
    total INTEGER,
    components TEXT,
    score_evidence TEXT NOT NULL DEFAULT '{}',
    missing_evidence TEXT,
    excluded INTEGER DEFAULT 0,
    exclude_reason TEXT,
    scored_at TEXT,
    UNIQUE(posting_id, rubric_version)
);

CREATE INDEX IF NOT EXISTS idx_scores_version_excluded_total
    ON scores(rubric_version, excluded, total DESC);

CREATE TABLE IF NOT EXISTS funnel (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL UNIQUE REFERENCES postings(id),
    stage TEXT NOT NULL DEFAULT 'harvested',
    tier TEXT,
    referral_path TEXT,
    next_action TEXT,
    next_action_date TEXT,
    notes TEXT,
    pass_reason TEXT,
    pass_detail TEXT,
    applied_at TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS funnel_events (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL,
    from_stage TEXT,
    to_stage TEXT NOT NULL,
    note TEXT,
    at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_funnel_stage ON funnel(stage);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings(id),
    kind TEXT NOT NULL CHECK(kind IN ('tailoring','cover','outreach','resume')),
    content TEXT,
    model TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    edited INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS spend_ledger (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER REFERENCES drafts(id),
    day TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    at TEXT NOT NULL,
    job TEXT,
    kind TEXT
);

CREATE INDEX IF NOT EXISTS idx_spend_ledger_day ON spend_ledger(day);

CREATE TABLE IF NOT EXISTS source_state (
    source TEXT PRIMARY KEY,
    baseline_at TEXT NOT NULL,
    last_success_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS eligibility_assessments (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings(id),
    rubric_version INTEGER NOT NULL,
    preferences_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('eligible','review','blocked')),
    blockers TEXT NOT NULL DEFAULT '[]',
    warnings TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '[]',
    assessed_at TEXT NOT NULL,
    UNIQUE(posting_id, rubric_version, preferences_version)
);

CREATE INDEX IF NOT EXISTS idx_eligibility_queue
    ON eligibility_assessments(rubric_version, preferences_version, status, posting_id);

CREATE TABLE IF NOT EXISTS resume_variants (
    id INTEGER PRIMARY KEY,
    label TEXT UNIQUE NOT NULL,
    path TEXT NOT NULL,
    purpose TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER UNIQUE NOT NULL REFERENCES postings(id),
    submitted_at TEXT,
    channel TEXT,
    resume_variant_id INTEGER REFERENCES resume_variants(id),
    confirmation_ref TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    company TEXT,
    title TEXT,
    relationship TEXT,
    source TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS touches (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id),
    posting_id INTEGER REFERENCES postings(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned','completed')),
    due_at TEXT,
    completed_at TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_touches_due ON touches(status, due_at);
CREATE INDEX IF NOT EXISTS idx_touches_completed ON touches(completed_at);

CREATE TABLE IF NOT EXISTS conference_actions (
    id INTEGER PRIMARY KEY,
    program TEXT NOT NULL,
    action TEXT NOT NULL,
    due_at TEXT,
    recur_days INTEGER,
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','completed')),
    notes TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(program, action)
);

CREATE INDEX IF NOT EXISTS idx_conference_actions_due ON conference_actions(status, due_at);

CREATE TABLE IF NOT EXISTS interviews (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings(id),
    round_name TEXT NOT NULL,
    scheduled_at TEXT,
    interviewers TEXT,
    prep_notes TEXT,
    outcome TEXT,
    thank_you_due_at TEXT,
    thank_you_completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER UNIQUE NOT NULL REFERENCES postings(id),
    received_at TEXT,
    deadline TEXT,
    base_salary REAL,
    bonus REAL,
    equity TEXT,
    location TEXT,
    notes TEXT,
    decision TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER REFERENCES postings(id),
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_events (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_entity
    ON activity_events(entity_type, entity_id, at);

CREATE TABLE IF NOT EXISTS application_answers (
    semantic_key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER,
    summary TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_log_job
    ON run_log(job, started_at DESC);

-- Write-through cache for the LLM field mapper (jobhunt/apply_assist/mapper.py):
-- one row per field fingerprint. semantic_key is nullable — a cached "no key
-- fits" is a real, valuable answer that must not trigger a second LLM call.
-- Purely additive like run_log above: no migration needed, this CREATE TABLE
-- IF NOT EXISTS runs on every connect.
CREATE TABLE IF NOT EXISTS field_key_map (
    fingerprint TEXT PRIMARY KEY,
    semantic_key TEXT,
    label TEXT,
    host TEXT,
    source TEXT NOT NULL,
    confidence REAL,
    created_at TEXT NOT NULL
);

-- Write-through cache for tailor_answers.py (jobhunt/apply_assist/tailor_answers.py):
-- one row per (posting, question) fingerprint. answer is nullable — a cached
-- "facts file can't support this" is a real, valuable answer that must not
-- trigger a second LLM call. Purely additive like field_key_map above: no
-- migration needed, this CREATE TABLE IF NOT EXISTS runs on every connect.
CREATE TABLE IF NOT EXISTS tailored_answers (
    posting_id INTEGER NOT NULL REFERENCES postings(id),
    question_fingerprint TEXT NOT NULL,
    question_text TEXT,
    answer TEXT,
    model TEXT,
    cost_usd REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (posting_id, question_fingerprint)
);

-- Write-through cache for jd_summary.py (jobhunt/apply_assist/jd_summary.py):
-- one row per posting, reused across every question tailor_answers.py drafts
-- for that posting — the whole cost argument for pre-summarizing the JD
-- instead of re-sending it per question. Purely additive like tailored_answers
-- above: no migration needed, this CREATE TABLE IF NOT EXISTS runs on every
-- connect.
CREATE TABLE IF NOT EXISTS jd_summaries (
    posting_id INTEGER PRIMARY KEY REFERENCES postings(id),
    summary_json TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL
);

-- v8 adds sprints and sprint_members (jobhunt/sprint.py): a sprint is a small
-- named batch of postings the candidate commits to driving to submission, bounding
-- spend-gated drafting to its members. Purely additive like run_log above:
-- no migration needed, this CREATE TABLE IF NOT EXISTS runs on every connect.
CREATE TABLE IF NOT EXISTS sprints (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','abandoned')),
    target_size INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS sprint_members (
    id INTEGER PRIMARY KEY,
    sprint_id INTEGER NOT NULL REFERENCES sprints(id),
    posting_id INTEGER NOT NULL REFERENCES postings(id),
    added_at TEXT NOT NULL,
    no_warm_path_note TEXT,
    UNIQUE(sprint_id, posting_id)
);

CREATE INDEX IF NOT EXISTS idx_sprint_members_sprint ON sprint_members(sprint_id);

INSERT OR IGNORE INTO resume_variants
    (label, path, purpose, reviewed_at, created_at, updated_at)
VALUES
    ('Founder Engineer 2026',
     '__DEFAULT_RESUME_PATH__',
     'Founder-engineer applications; pending the candidate review',
     NULL, '', '');
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a WAL-mode connection to the job-hunt DB, creating schema if needed."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.exists() and path.stat().st_size > 0
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if existing and path.resolve() == DEFAULT_DB_PATH.resolve() and version < SCHEMA_VERSION:
        _backup_before_migration(conn)
    init_schema(conn)
    _apply_migrations(conn, version)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.replace("__DEFAULT_RESUME_PATH__", _default_resume_path()))
    conn.commit()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _backup_before_migration(conn: sqlite3.Connection) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = BACKUP_DIR / f"jobhunt-auto-pre-v{SCHEMA_VERSION}-{stamp}.db"
    backup = sqlite3.connect(destination)
    try:
        conn.backup(backup)
        if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"migration backup failed integrity check: {destination}")
    finally:
        backup.close()
    return destination


def _drafts_allows_resume_kind(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='drafts'").fetchone()
    return bool(row) and "'resume'" in (row[0] or "")


def _widen_drafts_kind_check(conn: sqlite3.Connection) -> None:
    """v2: allow drafts.kind='resume'.

    SQLite cannot ALTER a CHECK constraint, so the table is rebuilt. Guarded by
    `_drafts_allows_resume_kind` because `_apply_migrations` runs on every
    connect, including fresh databases that already got the widened CHECK from
    SCHEMA.
    """
    if _drafts_allows_resume_kind(conn):
        return
    conn.executescript(
        """
        PRAGMA foreign_keys=off;
        CREATE TABLE drafts_v2 (
            id INTEGER PRIMARY KEY,
            posting_id INTEGER NOT NULL REFERENCES postings(id),
            kind TEXT NOT NULL CHECK(kind IN ('tailoring','cover','outreach','resume')),
            content TEXT,
            model TEXT,
            tokens_in INTEGER,
            tokens_out INTEGER,
            cost_usd REAL,
            edited INTEGER DEFAULT 0,
            created_at TEXT
        );
        INSERT INTO drafts_v2 (id, posting_id, kind, content, model, tokens_in,
                               tokens_out, cost_usd, edited, created_at)
            SELECT id, posting_id, kind, content, model, tokens_in,
                   tokens_out, cost_usd, edited, created_at FROM drafts;
        DROP TABLE drafts;
        ALTER TABLE drafts_v2 RENAME TO drafts;
        PRAGMA foreign_keys=on;
        """
    )


def _apply_migrations(conn: sqlite3.Connection, version: int) -> None:
    if version >= SCHEMA_VERSION:
        return
    if not _has_column(conn, "postings", "discovered_after_baseline"):
        conn.execute(
            "ALTER TABLE postings ADD COLUMN discovered_after_baseline INTEGER NOT NULL DEFAULT 0"
        )
    if not _has_column(conn, "funnel", "pass_reason"):
        conn.execute("ALTER TABLE funnel ADD COLUMN pass_reason TEXT")
    if not _has_column(conn, "funnel", "pass_detail"):
        conn.execute("ALTER TABLE funnel ADD COLUMN pass_detail TEXT")
    _widen_drafts_kind_check(conn)
    # v3 added run_log, which is purely additive: init_schema's CREATE TABLE IF NOT
    # EXISTS runs on every connect, so there is nothing to migrate. The version bump
    # exists so user_version still identifies the schema shape (and forces a backup).
    # v4 re-opens this function for databases already stamped 3. run_log and the
    # drafts CHECK widening landed as separate commits that both claimed 3, so a DB
    # stamped by the first would have early-returned past the second forever.
    # v5 adds spend_ledger.job/kind so per-batch spend (RunBudget) and per-kind
    # breakdowns can be attributed; both columns are additive and nullable.
    if not _has_column(conn, "spend_ledger", "job"):
        conn.execute("ALTER TABLE spend_ledger ADD COLUMN job TEXT")
    if not _has_column(conn, "spend_ledger", "kind"):
        conn.execute("ALTER TABLE spend_ledger ADD COLUMN kind TEXT")
    # v6 adds postings.duplicate_of for cross-source dedupe (jobhunt/dedupe.py):
    # nullable self-reference to the winning posting's id, set when this row is
    # demoted to status='duplicate'. Additive and nullable.
    if not _has_column(conn, "postings", "duplicate_of"):
        conn.execute("ALTER TABLE postings ADD COLUMN duplicate_of INTEGER")
    # v7 stores per-component evidence so score decisions are auditable rather
    # than leaving operators to infer why a component received its value.
    if not _has_column(conn, "scores", "score_evidence"):
        conn.execute("ALTER TABLE scores ADD COLUMN score_evidence TEXT NOT NULL DEFAULT '{}'")
    # v8 adds sprints/sprint_members, which are purely additive: init_schema's
    # CREATE TABLE IF NOT EXISTS runs on every connect, so there is nothing to
    # migrate here.

    now = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO source_state (source, baseline_at)
           SELECT DISTINCT source, ? FROM postings""",
        (now,),
    )
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
