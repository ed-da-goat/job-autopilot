"""`score` command implementation: sweep postings, score, upsert, summarize.
Kept separate from scoring.py (pure component logic) and cli.py (wiring).
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from jobhunt.scores_db import upsert_score
from jobhunt.scoring import load_companies_by_name, score_posting
from jobhunt.eligibility import assess_posting, load_preferences, upsert_assessment

BUCKET_SIZE = 10
# Postings scored per transaction. One commit at the end of the sweep kept the
# SQLite write lock for the whole run (it held `make daily` from 08:00 to 16:45
# on 2026-10-23); committing per batch releases it between batches. The upserts
# are idempotent, so a sweep that dies mid-way just resumes on the next run.
COMMIT_BATCH = 50


@dataclass
class SweepResult:
    scored: int = 0
    excluded: int = 0
    exclude_reasons: Counter = field(default_factory=Counter)
    histogram: Counter = field(default_factory=Counter)  # bucket floor -> count
    missing_evidence_count: int = 0


def _bucket(total: int) -> int:
    floor = (total // BUCKET_SIZE) * BUCKET_SIZE
    return min(floor, 90)  # 90-100 share a bucket


def run_score_sweep(conn: sqlite3.Connection, rubric: dict, limit: int | None = None) -> SweepResult:
    companies = load_companies_by_name(conn)
    result = SweepResult()

    query = "SELECT * FROM postings WHERE status = 'active' ORDER BY id"
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    postings = conn.execute(query).fetchall()

    rubric_version = rubric["version"]
    preferences = load_preferences()
    pending = 0
    for posting in postings:
        company_row = companies.get(posting["company"]) if posting["company"] else None
        scored = score_posting(posting, rubric, company_row)
        upsert_score(conn, posting["id"], rubric_version, scored)
        assessment = assess_posting(posting, preferences)
        upsert_assessment(conn, posting["id"], rubric_version, preferences, assessment)
        pending += 1
        if pending >= COMMIT_BATCH:
            conn.commit()
            pending = 0

        if scored["excluded"]:
            result.excluded += 1
            base_reason = (scored["exclude_reason"] or "unknown").split(":")[0]
            result.exclude_reasons[base_reason] += 1
            continue

        result.scored += 1
        result.histogram[_bucket(scored["total"])] += 1
        if scored["missing_evidence"]:
            result.missing_evidence_count += 1

    conn.commit()
    return result


def format_sweep_report(result: SweepResult) -> str:
    lines = [
        f"Scored: {result.scored}",
        f"Excluded: {result.excluded}",
    ]
    if result.exclude_reasons:
        lines.append("Top exclude reasons:")
        for reason, count in result.exclude_reasons.most_common(3):
            lines.append(f"  {reason}: {count}")

    lines.append("Score distribution:")
    if result.scored == 0:
        lines.append("  (nothing scored)")
    else:
        max_count = max(result.histogram.values())
        for floor in range(0, 100, BUCKET_SIZE):
            count = result.histogram.get(floor, 0)
            bar_width = round((count / max_count) * 40) if max_count else 0
            label = "90-100" if floor == 90 else f"{floor:>2}-{floor + BUCKET_SIZE - 1}"
            lines.append(f"  {label:>7}: {'#' * bar_width} {count}")

    lines.append(f"Missing evidence: {result.missing_evidence_count}")
    return "\n".join(lines)
