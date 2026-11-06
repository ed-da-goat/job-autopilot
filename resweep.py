"""Weekly resweep: full polite harvest + rescore at the current rubric,
then a summary of what's new. Thin — implementations live in jobhunt/.

    make resweep
    uv run python resweep.py
"""

from __future__ import annotations

from jobhunt import db as db_module
from jobhunt import dedupe as dedupe_module
from jobhunt.eligibility import load_preferences
from jobhunt.harvest import run_harvest
from jobhunt.rubric import load_rubric
from jobhunt.score_sweep import run_score_sweep
from jobhunt.scores_db import fetch_score_rows

NEW_SCORE_THRESHOLD = 55
TOP_N = 10
TRIAGE_BAND_LOW = 50
TRIAGE_BAND_HIGH = 69


def _triage_backlog_count(conn, rubric_version: int) -> int:
    """Active, eligible, un-funneled postings scoring 50-69 at the current
    rubric — the weekly triage backlog (mirrors run_suggest's join shape)."""
    preferences = load_preferences()
    row = conn.execute(
        """
        SELECT COUNT(*) c
        FROM scores s
        JOIN postings p ON p.id = s.posting_id
        JOIN eligibility_assessments e
          ON e.posting_id=s.posting_id AND e.rubric_version=s.rubric_version
         AND e.preferences_version=?
        WHERE s.rubric_version = ? AND s.excluded = 0
          AND s.total BETWEEN ? AND ?
          AND e.status='eligible' AND p.status = 'active'
          AND s.posting_id NOT IN (SELECT posting_id FROM funnel)
        """,
        (preferences.version, rubric_version, TRIAGE_BAND_LOW, TRIAGE_BAND_HIGH),
    ).fetchone()
    return row["c"]


def main() -> None:
    conn = db_module.connect()
    try:
        before_ids = {r["id"] for r in conn.execute("SELECT id FROM postings").fetchall()}

        run_harvest(conn)  # prints its own per-source new counts + any WARNINGs

        rubric = load_rubric()
        run_score_sweep(conn, rubric)
        dedupe_module.run_dedupe(conn)

        rows = fetch_score_rows(conn, rubric["version"])
        new_rows = [r for r in rows if r["posting_id"] not in before_ids]
        new_scoring = [r for r in new_rows if (r["total"] or 0) >= NEW_SCORE_THRESHOLD]
        top_new = sorted(new_rows, key=lambda r: -(r["total"] or 0))[:TOP_N]

        print(f"\nNew postings scoring >= {NEW_SCORE_THRESHOLD}: {len(new_scoring)}")
        print(f"Top {TOP_N} new by score:")
        if not top_new:
            print("  (none)")
        for r in top_new:
            print(f"  [{r['posting_id']:>6}] {r['total']:>3}  {r['company']} — {r['title']}  {r['url']}")

        backlog_count = _triage_backlog_count(conn, rubric["version"])
        print(f"\n50–69 triage backlog: {backlog_count} active, eligible, un-funneled postings")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
