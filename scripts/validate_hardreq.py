"""Validate jobhunt.hardreq against the labeled history (read-only DB) and write a report.

    uv run python scripts/validate_hardreq.py [--out tmp/hardreq-validation-2026-10-26.md]

Labels: the latest `match_report` artifact per posting whose notes start with "dropped" is a DROP,
any other match_report is a KEEP, and every posting with applications.submitted_at is a KEEP
(KEEP wins a conflict). A rule ships enabled only with zero KEEP hits; `years` takes the lowest
min_years in 4..6 with zero KEEP hits. Opens the DB with mode=ro and writes only the report.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobhunt import autoapply, hardreq  # noqa: E402
from jobhunt.apply_assist import submitwatch  # noqa: E402
from jobhunt.text_extract import extract_text  # noqa: E402

DB = ROOT / "data" / "jobhunt.db"
YEARS = (3, 4, 5, 6)
MIN_SAFE_YEARS = 4
NON_YEAR_RULES = tuple(r for r in hardreq.RULES if r != "years")  # incl. domain_years (no min_years)


def connect_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def posting(conn: sqlite3.Connection, pid: int):
    return conn.execute(
        "SELECT id, source, company, title, raw_json FROM postings WHERE id = ?", (pid,)
    ).fetchone()


def row_status(conn: sqlite3.Connection, pid: int):
    r = conn.execute(
        "SELECT p.status, a.submitted_at FROM postings p LEFT JOIN applications a "
        "ON a.posting_id = p.id WHERE p.id = ?", (pid,)).fetchone()
    return (r["status"], r["submitted_at"]) if r else (None, None)


def labels(conn: sqlite3.Connection) -> dict[int, str]:
    out: dict[int, str] = {}
    rows = conn.execute(
        """SELECT a.posting_id, json_extract(a.content, '$.notes') AS notes
           FROM artifacts a
           WHERE a.kind = 'match_report'
             AND a.id = (SELECT MAX(b.id) FROM artifacts b
                         WHERE b.posting_id = a.posting_id AND b.kind = 'match_report')"""
    ).fetchall()
    for r in rows:
        out[r["posting_id"]] = "DROP" if (r["notes"] or "").strip().lower().startswith("dropped") else "KEEP"
    for r in conn.execute("SELECT posting_id FROM applications WHERE submitted_at IS NOT NULL"):
        out[r["posting_id"]] = "KEEP"
    return out


def hits_for(row, rule: str, years: int):
    title, desc, _ = extract_text(row)
    return hardreq.hard_requirement(title, desc, min_years=years, rules={rule})


def configs():
    for rule in NON_YEAR_RULES:
        yield rule, rule, 99
    for y in YEARS:
        yield f"years>={y}", "years", y


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "tmp" / "hardreq-validation-2026-10-26.md"))
    args = ap.parse_args()
    conn = connect_ro()
    lab = labels(conn)
    drops = [p for p, v in lab.items() if v == "DROP"]
    keeps = [p for p, v in lab.items() if v == "KEEP"]
    rows = {p: posting(conn, p) for p in lab}
    rows = {p: r for p, r in rows.items() if r is not None}

    lines = ["# hardreq validation, 2026-10-26", "",
             f"Labels: {len(drops)} DROP, {len(keeps)} KEEP "
             "(latest match_report per posting; KEEP also = any submitted application).", "",
             "## Catch and false drops", "",
             "| config | hits on DROPs | hits on KEEPs (false drops) |", "|---|---|---|"]
    keep_hits: dict[str, list] = {}
    result: dict[str, tuple[int, int]] = {}
    for name, rule, y in configs():
        d = sum(1 for p in drops if p in rows and hits_for(rows[p], rule, y))
        k = []
        for p in keeps:
            if p in rows:
                h = hits_for(rows[p], rule, y)
                if h:
                    k.append((p, rows[p]["company"], rows[p]["title"], h[1]))
        keep_hits[name] = k
        result[name] = (d, len(k))
        lines.append(f"| {name} | {d}/{len(drops)} | {len(k)}/{len(keeps)} |")

    lines += ["", "## Every KEEP hit", ""]
    for name, k in keep_hits.items():
        for p, company, title, ev in k:
            lines.append(f"- `{name}` #{p} {company} | {title}: \"{ev}\"")
    if not any(keep_hits.values()):
        lines.append("(none)")

    enabled = [r for r in NON_YEAR_RULES if result[r][1] == 0]
    years_pick = next((y for y in YEARS if y >= MIN_SAFE_YEARS and result[f"years>={y}"][1] == 0), None)
    if years_pick is not None:
        enabled.append("years")
    lines += ["", "## Chosen defaults", "",
              f"- enabled rules: {', '.join(enabled) or 'none'}",
              f"- hard_req_min_years: {years_pick if years_pick is not None else 'n/a (years rule disabled)'}",
              f"- hard_req_filter: {'True' if enabled else 'False (nothing catches safely)'}"]
    # Distinct DROPs the enabled set would catch
    caught = 0
    for p in drops:
        if p not in rows:
            continue
        for r in enabled:
            if hits_for(rows[p], r, years_pick or 99):
                caught += 1
                break
    lines.append(f"- DROPs caught by the enabled set: {caught}/{len(drops)}; false drops: 0 by construction")

    # Live pool
    pool = autoapply._candidate_pool(conn, autoapply.AutoApplyConfig())
    by_rule: dict[str, list] = {}
    skipped = Counter()
    reach_skipped = Counter()
    reachable = 0
    first_hit = {}
    for cand in pool:
        row = posting(conn, cand.posting_id)
        if row is None:
            continue
        title, desc, _ = extract_text(row)
        for rule in hardreq.RULES:
            y = years_pick or MIN_SAFE_YEARS
            h = hardreq.hard_requirement(title, desc, min_years=y, rules={rule})
            if h:
                by_rule.setdefault(rule, []).append((cand, h[1]))
        # "Reachable": would get past the readiness and submitted skips and need a tailor call
        # (no match_report yet). These are the postings the filter actually saves a call on.
        reach = (
            row_status(conn, cand.posting_id) == ("active", None)
            and not submitwatch.record_blockers(conn, cand.posting_id)
            and not autoapply._has_report(conn, cand.posting_id)
        )
        reachable += reach
        h_en = hardreq.hard_requirement(title, desc, min_years=years_pick or 99, rules=set(enabled))
        if h_en:
            skipped[h_en[0]] += 1
            reach_skipped[h_en[0]] += reach
            first_hit[cand.posting_id] = h_en
    lines += ["", f"## Live pool ({len(pool)} candidates, read-only)", "",
              f"The enabled set would skip {sum(skipped.values())} (first matching rule): "
              + (", ".join(f"{r}={n}" for r, n in skipped.items()) or "none"), "",
              f"Of the pool, {reachable} are reachable (active, not submitted, no readiness blocker, "
              f"no match_report yet); the filter would skip {sum(reach_skipped.values())} of those "
              "(a model call saved each): "
              + (", ".join(f"{r}={n}" for r, n in reach_skipped.items()) or "none"), "",
              f"Per-rule hits over the pool (years at min_years={years_pick or MIN_SAFE_YEARS}; "
              "a posting can hit several rules):", ""]
    rng = random.Random(0)
    for rule in hardreq.RULES:
        hits = by_rule.get(rule, [])
        tag = "ENABLED" if rule in enabled else "disabled"
        lines += [f"### {rule} ({tag}): {len(hits)} hits", ""]
        for cand, ev in rng.sample(hits, min(10, len(hits))):
            lines.append(f"- #{cand.posting_id} {cand.company} | {cand.title}: \"{ev}\"")
        lines.append("")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}; enabled={enabled} min_years={years_pick}")


if __name__ == "__main__":
    main()
