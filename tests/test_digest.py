from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from jobhunt import db as db_module
from jobhunt import digest as digest_module
from jobhunt import funnel as funnel_module
from jobhunt.apply_assist.digest import render_digest as render_apply_digest
from jobhunt.apply_assist.schema import FieldSpec, Resolution, TriageDigest

TODAY = datetime.now(UTC).date().isoformat()
YESTERDAY = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()


def _make_posting(
    conn,
    spid: str,
    company: str = "Acme",
    title: str = "Backend Engineer",
    url: str = "https://example.com/job",
) -> int:
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, url, status, first_seen_at, last_seen_at)
           VALUES ('greenhouse', ?, ?, ?, ?, 'active', '', '')""",
        (spid, company, title, url),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM postings WHERE source_posting_id=?", (spid,)
    ).fetchone()["id"]


def _make_score(conn, posting_id: int, rubric_version: int, total: int) -> None:
    conn.execute(
        """INSERT INTO scores
           (posting_id, rubric_version, total, components, missing_evidence, excluded, exclude_reason, scored_at)
           VALUES (?, ?, ?, '{}', '[]', 0, NULL, '')""",
        (posting_id, rubric_version, total),
    )
    conn.execute(
        """INSERT INTO eligibility_assessments
           (posting_id, preferences_version, rubric_version, status, blockers, warnings, evidence, assessed_at)
           VALUES (?, 1, ?, 'eligible', '[]', '[]', '[]', ?)""",
        (posting_id, rubric_version, datetime.now(UTC).isoformat()),
    )
    conn.commit()


def _make_suggested(conn, posting_id: int, created_at: str) -> None:
    conn.execute(
        "INSERT INTO funnel (posting_id, stage, created_at, updated_at) VALUES (?, 'suggested', ?, ?)",
        (posting_id, created_at, created_at),
    )
    conn.commit()


def _make_sweep(conn, source: str, started_at: str, errors: list[dict]) -> None:
    conn.execute(
        """INSERT INTO sweeps
           (started_at, finished_at, source, boards_attempted, boards_ok, postings_seen, postings_new, errors)
           VALUES (?, ?, ?, 1, 0, 0, 0, ?)""",
        (started_at, started_at, source, json.dumps(errors)),
    )
    conn.commit()


def test_build_digest_empty_state_no_crash(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        markdown = digest_module.build_digest(conn, date_str=TODAY)
        assert f"# Job Hunt Digest — {TODAY}" in markdown
        assert "## New Suggestions" in markdown
        assert "## Weekly Gates" in markdown
        assert "## Next Actions Due/Overdue" in markdown
        assert "## Top Movers" in markdown
        # no spend or sweep-warning rows -> those sections are omitted, not crashed on
        assert "## Spend Today" not in markdown
        assert "## Sweep Warnings" not in markdown
        assert "(none)" in markdown
        assert "(none due)" in markdown
    finally:
        conn.close()


def test_build_digest_sections_and_known_lines(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        # New suggestion, suggested "now" (well within the today/yesterday window)
        suggested_id = _make_posting(
            conn,
            "sug1",
            company="Rocket",
            title="Founding Engineer",
            url="https://boards.example.com/rocket",
        )
        _make_score(conn, suggested_id, 1, 88)
        _make_suggested(conn, suggested_id, datetime.now(UTC).isoformat())

        # Top mover: scored, not in funnel at all
        mover_id = _make_posting(
            conn, "mov1", company="Nimbus", title="Backend Engineer"
        )
        _make_score(conn, mover_id, 1, 80)

        # Next action overdue
        action_id = _make_posting(
            conn, "act1", company="Vector", title="Platform Engineer"
        )
        funnel_module.move(conn, action_id, "qualified")
        funnel_module.set_field(conn, action_id, "next_action", "call recruiter")
        funnel_module.set_field(conn, action_id, "next_action_date", YESTERDAY)

        # Applications 7d = 1 (SHORT of 10)
        applied_id = _make_posting(conn, "app1", company="Delta", title="SWE")
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """INSERT INTO applications (posting_id, submitted_at, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (applied_id, now, now, now),
        )
        conn.commit()

        # Sweep warning
        _make_sweep(
            conn,
            "greenhouse",
            datetime.now(UTC).isoformat(),
            [{"company": "Foo", "slug": "foo", "error": "404 board not found"}],
        )

        # Spend today
        conn.execute(
            "INSERT INTO spend_ledger (draft_id, day, cost_usd, at) VALUES (NULL, ?, 0.0042, ?)",
            (TODAY, datetime.now(UTC).isoformat()),
        )
        conn.commit()

        markdown = digest_module.build_digest(conn, date_str=TODAY)

        assert (
            f"- [{suggested_id}] 88  Rocket — Founding Engineer  https://boards.example.com/rocket"
            in markdown
        )
        assert f"- [{mover_id}] 80  Nimbus — Backend Engineer" in markdown
        assert "- applications (7d): 1 / 10 — SHORT" in markdown
        assert "- warm touches (7d): 0 / 5 — SHORT" in markdown
        assert (
            f"[OVERDUE] [{action_id}] {YESTERDAY}  (qualified)  call recruiter"
            in markdown
        )
        assert "## Spend Today" in markdown
        assert "- $0.0042" in markdown
        assert "## Sweep Warnings" in markdown
        assert "WARNING: greenhouse: 1 error(s) (e.g. 404 board not found)" in markdown
    finally:
        conn.close()


def test_build_digest_extra_notes_section(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        markdown = digest_module.build_digest(
            conn, date_str=TODAY, extra_notes=["auto-prep: stopped at daily spend cap"]
        )
        assert "## Notes" in markdown
        assert "- auto-prep: stopped at daily spend cap" in markdown
    finally:
        conn.close()


def test_new_suggestions_excludes_old_suggestions(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        old_id = _make_posting(conn, "old1")
        _make_score(conn, old_id, 1, 90)
        old_created = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        _make_suggested(conn, old_id, old_created)

        markdown = digest_module.build_digest(conn, date_str=TODAY)
        assert f"[{old_id}]" not in markdown.split("## Weekly Gates")[0]
    finally:
        conn.close()


def test_write_digest_creates_file_and_returns_path(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        digest_dir = tmp_path / "digest_out"
        path = digest_module.write_digest(conn, date_str=TODAY, digest_dir=digest_dir)
        assert path == digest_dir / f"{TODAY}.md"
        assert path.exists()
        assert f"# Job Hunt Digest — {TODAY}" in path.read_text(encoding="utf-8")
    finally:
        conn.close()


def test_digest_open_the_os_section(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        # Active posting, eligible at (rubric=1, prefs=1).
        eligible_id = _make_posting(conn, "elig1", company="Rocket", title="SWE")
        _make_score(conn, eligible_id, 1, 90)

        # Posting in the funnel, blocked at the same versions.
        blocked_id = _make_posting(conn, "blk1", company="Vector", title="PM")
        conn.execute(
            """INSERT INTO eligibility_assessments
               (posting_id, preferences_version, rubric_version, status, blockers, warnings, evidence, assessed_at)
               VALUES (?, 1, 1, 'blocked', '[]', '[]', '[]', ?)""",
            (blocked_id, datetime.now(UTC).isoformat()),
        )
        conn.execute(
            "INSERT INTO funnel (posting_id, stage, created_at, updated_at) VALUES (?, 'suggested', ?, ?)",
            (blocked_id, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )
        conn.commit()

        markdown = digest_module.build_digest(conn, date_str=TODAY)

        assert "## Open the OS" in markdown
        assert "http://127.0.0.1:8787" in markdown
        assert (
            "- Eligible: 1 · review: 0 · blocked: 1 (active postings, prefs v1)"
            in markdown
        )
        assert "- Blocked-in-funnel needing one-click pass: 1" in markdown
        assert "- Dropped balls:" in markdown
    finally:
        conn.close()


def test_digest_omits_os_section_without_assessments(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        markdown = digest_module.build_digest(conn, date_str=TODAY)
        assert "## Open the OS" not in markdown
    finally:
        conn.close()


def test_digest_leads_with_loop_liveness(tmp_path):
    """A digest that opens with counts looks the same whether the scheduler ran
    this morning or died ten days ago."""
    conn = db_module.connect(tmp_path / "t.db")
    try:
        text = digest_module.build_digest(conn)
        lines = text.splitlines()
        assert lines[0].startswith("# Job Hunt Digest")
        assert lines[2] == "## Loop Liveness"
        assert "daily:" in text
        assert "apply-queue:" in text
        # Nothing has ever run in a fresh DB, and the digest must say so.
        assert "no successful run on record" in text
        assert text.index("## Loop Liveness") < text.index("## New Suggestions")
    finally:
        conn.close()


# --- apply_assist digest: AI DRAFTS section ------------------------------
# render_digest() here is jobhunt.apply_assist.digest.render_digest, the
# per-posting terminal block — unrelated to build_digest() above (the daily
# job-hunt-wide digest). Covers the drafted-answer review-gate: a draft is
# shown but never claimed as typed.


def _triage_digest(drafts: list[Resolution]) -> TriageDigest:
    return TriageDigest(
        posting_id=1,
        url="https://boards.greenhouse.io/acme/jobs/1",
        ats="greenhouse",
        filled=[],
        residual=[],
        skipped=[],
        errors=[],
        drafts=drafts,
    )


def test_render_apply_digest_shows_ai_drafts_section():
    field = FieldSpec(selector="#why", kind="textarea", label="Why us?")
    res = Resolution(
        field=field,
        semantic_key=None,
        action="fill",
        value="Because I like the mission.",
        draft=True,
    )
    text = render_apply_digest(_triage_digest([res]))
    assert "AI DRAFTS — NOT typed in (1)" in text
    assert "🔴 Why us? → Because I like the mission." in text


def test_render_apply_digest_omits_ai_drafts_section_when_empty():
    text = render_apply_digest(_triage_digest([]))
    assert "AI DRAFTS" not in text


def test_render_apply_digest_truncates_long_draft_values():
    field = FieldSpec(selector="#why", kind="textarea", label="Why us?")
    long_value = "x" * 200
    res = Resolution(
        field=field, semantic_key=None, action="fill", value=long_value, draft=True
    )
    text = render_apply_digest(_triage_digest([res]))
    assert ("x" * 120 + "…") in text
    assert long_value not in text
