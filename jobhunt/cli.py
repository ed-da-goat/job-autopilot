"""Typer CLI: `uv run python -m jobhunt <command>`."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from jobhunt import applylock
from jobhunt import applypack as applypack_module
from jobhunt import autoapply as autoapply_module
from jobhunt import autopilot as autopilot_module
from jobhunt import conference as conference_module
from jobhunt import db as db_module
from jobhunt import dedupe as dedupe_module
from jobhunt import digest as digest_module
from jobhunt import discover as discover_module
from jobhunt.eligibility import load_preferences
from jobhunt import expand as expand_module
from jobhunt import finai as finai_module
from jobhunt import funnel as funnel_module
from jobhunt import notify as notify_module
from jobhunt import prep as prep_module
from jobhunt import report as report_module
from jobhunt import runlog
from jobhunt import sample as sample_module
from jobhunt import seed as seed_module
from jobhunt import sheets as sheets_module
from jobhunt import shortlist as shortlist_module
from jobhunt import simplify_import as simplify_import_module
from jobhunt import spend as spend_module
from jobhunt import sprint as sprint_module
from jobhunt import suggest as suggest_module
from jobhunt.connectors import openrouter as openrouter_module
from jobhunt.harvest import ALL_SOURCES, run_harvest
from jobhunt.resume import render as resume_render_module
from jobhunt.resume import select as resume_select_module
from jobhunt.resume import tailor as resume_tailor_module
from jobhunt.resume import validate as resume_validate_module
from jobhunt.rubric import load_rubric
from jobhunt.score_sweep import format_sweep_report, run_score_sweep
from jobhunt.scores_db import fetch_score_rows
from jobhunt.scoring import load_companies_by_name
from jobhunt.web import queries as web_queries

app = typer.Typer(help="job-hunt harvester CLI")


SEED_PATH_OPTION = typer.Option(str(seed_module.DEFAULT_SEED_PATH), help="Path to seed_companies.md")
SOURCE_OPTION = typer.Option(
    None, "--source", help=f"Limit to specific source(s): {', '.join(ALL_SOURCES)}. Repeatable."
)
LIMIT_BOARDS_OPTION = typer.Option(
    None, "--limit-boards", help="Cap the number of boards fetched per platform (for smoke tests)."
)
SCORE_LIMIT_OPTION = typer.Option(None, "--limit", help="Cap the number of active postings scored.")
SAMPLE_N_OPTION = typer.Option(10, "--n", help="How many postings to sample.")
SAMPLE_SEED_OPTION = typer.Option(42, "--seed", help="Random seed for a deterministic sample.")
SHORTLIST_TOP_OPTION = typer.Option(50, "--top", help="How many top-scored postings to consider.")
SHORTLIST_OUT_OPTION = typer.Option("SHORTLIST.md", "--out", help="Path to write the markdown shortlist.")
QUALIFY_TIER_OPTION = typer.Option(
    None,
    "--tier",
    help="top20 (referral first), fast (same-day apply), or stretch (after exact-fit slate).",
)
NOTE_OPTION = typer.Option(None, "--note", help="Optional note logged on the funnel event.")
PIPELINE_STAGE_OPTION = typer.Option(None, "--stage", help=f"Filter to one stage: {', '.join(funnel_module.ALL_STAGES)}")
SUGGEST_THRESHOLD_OPTION = typer.Option(
    suggest_module.DEFAULT_THRESHOLD, "--threshold", help="Minimum score to auto-suggest."
)
SUGGEST_LIMIT_OPTION = typer.Option(
    suggest_module.DEFAULT_LIMIT, "--limit", help="Cap on backfill suggestions (no new-posting-id filter)."
)
PREP_KIND_OPTION = typer.Option(
    None, "--kind", help=f"One of {', '.join(prep_module.KINDS)}. Default: generate all three."
)
PREP_MODEL_OPTION = typer.Option(None, "--model", help="Override the OpenRouter model slug.")
PREP_OUTSIDE_SPRINT_OPTION = typer.Option(
    False, "--outside-sprint", help="Allow drafting for a posting outside the active sprint."
)
SPRINT_SIZE_OPTION = typer.Option(5, "--size", help="Target number of postings to add to the sprint.")
SPRINT_NAME_OPTION = typer.Option(None, "--name", help="Sprint name (defaults to 'Sprint YYYY-MM-DD').")
SPRINT_YES_OPTION = typer.Option(
    False, "--yes", "-y", help="Accept every candidate without confirmation (for scripting)."
)
DIGEST_PRINT_OPTION = typer.Option(
    False, "--print", help="Print the digest markdown to stdout instead of only the path."
)
PACK_PRINT_OPTION = typer.Option(
    False, "--print", help="Print the pack markdown to stdout instead of only the path."
)
APPLY_SCAN_ONLY_OPTION = typer.Option(
    False, "--scan-only", help="Scan and classify only; fill nothing."
)
QUEUE_LIMIT_OPTION = typer.Option(5, "--limit", help="Max postings to pre-fill in one run.")
QUEUE_MIN_SCORE_OPTION = typer.Option(
    None, "--min-score", help="Only include postings scoring at or above this threshold."
)
QUEUE_POSTING_ID_OPTION = typer.Option(
    None,
    "--posting-id",
    help="Re-run specific posting(s), ignoring the 7-day re-fill cooldown, for "
    "attended verification. Repeat the flag to pre-fill several in one browser.",
)
APPLY_TAILOR_OPTION = typer.Option(
    False,
    "--tailor",
    help=(
        "Draft answers to eligible essay/short-answer questions via "
        "tailor_answers (runs on the Claude subscription, no metered spend; "
        "drafts are never typed here without your approval). Default OFF."
    ),
)

CONFERENCE_PATH_OPTION = typer.Option(
    str(conference_module.DEFAULT_CONFERENCE_PATH), help="Path to conference_funding.md"
)

QUALIFY_TIERS = ("top20", "fast", "stretch")

SHEETS_DRY_RUN_OPTION = typer.Option(
    False, "--dry-run", help="Preview payloads (row counts + first 3 rows/tab); never touches Sheets."
)

EXPAND_LIMIT_OPTION = typer.Option(None, "--limit", help="Cap the number of candidates probed. Default: unlimited.")
EXPAND_DRY_RUN_OPTION = typer.Option(
    False, "--dry-run", help="Discover candidates only; skip probes and all writes."
)
EXPAND_GUESS_WATCHLIST_OPTION = typer.Option(
    False,
    "--guess-watchlist",
    help="Also guess greenhouse slugs for watchlist/platformless companies; a live hit promotes that row.",
)

DISCOVER_SOURCE_OPTION = typer.Option("all", "--source", help="Candidate feeder(s) to run: yc, cse, or all.")
DISCOVER_LIMIT_OPTION = typer.Option(
    300, "--limit", help="Cap the number of candidates/groups probed per feeder."
)
DISCOVER_DRY_RUN_OPTION = typer.Option(
    False, "--dry-run", help="Discover candidates only; skip probes and all writes."
)

DEDUPE_DRY_RUN_OPTION = typer.Option(
    False, "--dry-run", help="Preview groups/winners (summary + first 10 groups); never writes."
)


@app.command()
def seed(path: str = SEED_PATH_OPTION) -> None:
    """Import/refresh companies from data/seed_companies.md."""
    conn = db_module.connect()
    try:
        count = seed_module.seed_from_file(conn, path)
        print(f"Seeded/updated {count} companies from {path}")
    finally:
        conn.close()


@app.command(name="seed-expand")
def seed_expand(
    limit: int | None = EXPAND_LIMIT_OPTION,
    dry_run: bool = EXPAND_DRY_RUN_OPTION,
    guess_watchlist: bool = EXPAND_GUESS_WATCHLIST_OPTION,
) -> None:
    """Mine SimplifyJobs' New-Grad-Positions README + local postings for new
    greenhouse/lever/ashby company boards, probe each candidate once, and
    insert confirmed-live ones into companies (never clobbers hand-curated
    rows). Dead candidates are recorded in data/bad_slugs.md. With
    --guess-watchlist, also guesses greenhouse slugs for watchlist/
    platformless companies; a live hit promotes that row in place."""
    conn = db_module.connect()
    try:
        expand_module.run_expand(conn, limit=limit, dry_run=dry_run, guess_watchlist=guess_watchlist)
    finally:
        conn.close()


@app.command()
def discover(
    source: str = DISCOVER_SOURCE_OPTION,
    limit: int = DISCOVER_LIMIT_OPTION,
    dry_run: bool = DISCOVER_DRY_RUN_OPTION,
) -> None:
    """Find new ATS company boards beyond SimplifyJobs: YC's public hiring
    directory (guessed ashby/greenhouse/lever slugs) and Google Programmable
    Search over the six ATS host domains (certain slugs, no guessing). If
    cse is requested but GOOGLE_CSE_KEY/GOOGLE_CSE_ID aren't configured,
    prints a NOTE and falls back to yc rather than erroring."""
    if source not in ("yc", "cse", "all"):
        raise typer.BadParameter(f"unknown source {source!r}. Valid: yc, cse, all")
    conn = db_module.connect()
    try:
        discover_module.run_discovery(conn, source=source, limit=limit, dry_run=dry_run)
    finally:
        conn.close()


@app.command()
def conferences(path: str = CONFERENCE_PATH_OPTION) -> None:
    """Import/refresh conference funding actions from data/conference_funding.md."""
    conn = db_module.connect()
    try:
        count = conference_module.seed_from_file(conn, path)
        print(f"Seeded/updated {count} conference actions from {path}")
    finally:
        conn.close()


@app.command()
def harvest(
    source: list[str] = SOURCE_OPTION,
    limit_boards: int | None = LIMIT_BOARDS_OPTION,
) -> None:
    """Run a harvest sweep across sources."""
    invalid = [s for s in (source or []) if s not in ALL_SOURCES]
    if invalid:
        raise typer.BadParameter(f"unknown source(s): {invalid}. Valid: {ALL_SOURCES}")

    conn = db_module.connect()
    try:
        run_harvest(conn, sources=source or None, limit_boards=limit_boards)
    finally:
        conn.close()


@app.command()
def demo(
    db_path: str = typer.Option(
        "data/demo.db", "--db", help="Where to write the synthetic demo database."
    ),
    reset: bool = typer.Option(False, "--reset", help="Delete an existing demo database first."),
) -> None:
    """Seed a synthetic demo database (no profile, network, LLM, or keys needed)."""
    from jobhunt import demo as demo_module

    path = Path(db_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if demo_module._is_default_db(path):
        typer.echo(f"Refusing to write demo data to the real database: {db_module.DEFAULT_DB_PATH}", err=True)
        raise typer.Exit(1)
    if path.exists() and path.stat().st_size > 0:
        if not reset:
            typer.echo(f"{path} already exists; pass --reset to rebuild it.", err=True)
            raise typer.Exit(1)
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
    summary = demo_module.seed(path)
    funnel_line = ", ".join(f"{k} {v}" for k, v in sorted(summary["funnel"].items()))
    typer.echo(f"Seeded {summary['postings']} synthetic postings at {path} ({funnel_line}).")
    typer.echo(f"Next: JOBHUNT_DEMO=1 JOBHUNT_DB_PATH={path} uv run uvicorn jobhunt.web.app:app --port 8787")
    typer.echo("Or just run: make demo")


@app.command()
def counts() -> None:
    """Row counts per source: total/active/new-this-week + last sweep per source."""
    conn = db_module.connect()
    try:
        week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        sources = [
            r["source"]
            for r in conn.execute("SELECT DISTINCT source FROM postings ORDER BY source").fetchall()
        ]
        if not sources:
            print("No postings yet — run `harvest` first.")
            return

        print(f"{'source':<12}{'total':>8}{'active':>8}{'new_7d':>8}  last_sweep")
        print("-" * 60)
        for src in sources:
            total = conn.execute("SELECT COUNT(*) c FROM postings WHERE source=?", (src,)).fetchone()["c"]
            active = conn.execute(
                "SELECT COUNT(*) c FROM postings WHERE source=? AND status='active'", (src,)
            ).fetchone()["c"]
            new_7d = conn.execute(
                """SELECT COUNT(*) c FROM postings
                   WHERE source=? AND discovered_after_baseline=1 AND first_seen_at >= ?""",
                (src, week_ago),
            ).fetchone()["c"]
            last_sweep = conn.execute(
                "SELECT finished_at FROM sweeps WHERE source=? ORDER BY finished_at DESC LIMIT 1",
                (src,),
            ).fetchone()
            last_sweep_at = last_sweep["finished_at"] if last_sweep else "never"
            print(f"{src:<12}{total:>8}{active:>8}{new_7d:>8}  {last_sweep_at}")
    finally:
        conn.close()


@app.command()
def score(limit: int | None = SCORE_LIMIT_OPTION) -> None:
    """Score all active postings against the current rubric.json."""
    rubric = load_rubric()
    conn = db_module.connect()
    try:
        result = run_score_sweep(conn, rubric, limit=limit)
        print(format_sweep_report(result))
    finally:
        conn.close()


@app.command()
def sample(n: int = SAMPLE_N_OPTION, seed: int = SAMPLE_SEED_OPTION) -> None:
    """Print a stratified, deterministic sample of scored postings for calibration."""
    rubric = load_rubric()
    conn = db_module.connect()
    try:
        rows = fetch_score_rows(conn, rubric["version"])
        if not rows:
            print("No scores yet — run `score` first.")
            return
        picked = sample_module.stratified_sample(rows, n, seed)
        for row in picked:
            print(sample_module.format_sample_row(row))
            print()
    finally:
        conn.close()


@app.command()
def shortlist(top: int = SHORTLIST_TOP_OPTION, out: str = SHORTLIST_OUT_OPTION) -> None:
    """Write a markdown shortlist of the top-scored postings, one row per company."""
    rubric = load_rubric()
    conn = db_module.connect()
    try:
        rows = fetch_score_rows(
            conn,
            rubric["version"],
            exclude_blocked_preferences_version=load_preferences().version,
        )
        excluded_ids = web_queries.passed_or_closed_ids(conn)
        rows = [r for r in rows if r["posting_id"] not in excluded_ids]
        if not rows:
            print("No scores yet — run `score` first.")
            return
        companies = load_companies_by_name(conn)
        entries = shortlist_module.build_shortlist_rows(rows, top)
        markdown = shortlist_module.render_markdown(entries, companies, rubric)
        Path(out).write_text(markdown, encoding="utf-8")
        print(f"Wrote {len(entries)} rows ({len(rows)} scored postings considered) to {out}")
    finally:
        conn.close()


@app.command()
def qualify(posting_id: int, tier: str | None = QUALIFY_TIER_OPTION, note: str | None = NOTE_OPTION) -> None:
    """Move a posting to 'qualified' and optionally set its execution tier."""
    if tier is not None and tier not in QUALIFY_TIERS:
        raise typer.BadParameter(f"--tier must be one of {QUALIFY_TIERS}")

    conn = db_module.connect()
    try:
        from_stage, to_stage = funnel_module.move(conn, posting_id, "qualified", note)
        if tier is not None:
            funnel_module.set_field(conn, posting_id, "tier", tier)
        tier_suffix = f" (tier={tier})" if tier else ""
        print(f"[{posting_id}] {from_stage} -> {to_stage}{tier_suffix}")
    finally:
        conn.close()


@app.command(name="move")
def move_stage(posting_id: int, stage: str, note: str | None = NOTE_OPTION) -> None:
    """Move a posting to any funnel stage; every move logs a funnel_events row."""
    conn = db_module.connect()
    try:
        try:
            from_stage, to_stage = funnel_module.move(conn, posting_id, stage, note)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        print(f"[{posting_id}] {from_stage} -> {to_stage}")
    finally:
        conn.close()


@app.command(name="set")
def set_field_cmd(posting_id: int, field: str, value: str) -> None:
    """Set one funnel field: tier, referral_path, next_action, next_action_date, notes."""
    conn = db_module.connect()
    try:
        try:
            funnel_module.set_field(conn, posting_id, field, value)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        print(f"[{posting_id}] {field} = {value}")
    finally:
        conn.close()


@app.command()
def show(posting_id: int) -> None:
    """Show a posting: latest score breakdown, funnel state, and event history."""
    conn = db_module.connect()
    try:
        text = report_module.format_show(conn, posting_id)
        if text is None:
            print(f"No posting with id={posting_id}")
            raise typer.Exit(code=1)
        print(text)
    finally:
        conn.close()


@app.command(name="sheets-sync")
def sheets_sync(dry_run: bool = SHEETS_DRY_RUN_OPTION) -> None:
    """One-way mirror of the pipeline into the configured Google Sheet (data/sheets.local.toml)."""
    conn = db_module.connect()
    try:
        sheets_module.run_sync(conn, dry_run=dry_run)
    finally:
        conn.close()


finai_app = typer.Typer(help="Finance x AI lane: candidates, guarded JD, match-report log.")
app.add_typer(finai_app, name="finai")


@finai_app.command("candidates")
def finai_candidates(
    limit: int = typer.Option(25, "--limit"),
    as_json: bool = typer.Option(False, "--json", help="Print a JSON list instead of text lines."),
    max_tier: int = typer.Option(4, "--max-tier", help="Worst city tier to include (1=NYC .. 4=US remote)."),
) -> None:
    """Ranked in-lane postings not yet worked. Deterministic, no network, no LLM."""
    conn = db_module.connect()
    try:
        rows = finai_module.candidates(conn, limit=limit, max_tier=max_tier)
    finally:
        conn.close()
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    for row in rows:
        print(finai_module.format_candidate(row))


@finai_app.command("jd")
def finai_jd(posting_id: int) -> None:
    """Print a posting's JD through the injection guard (the only way the skill reads a JD)."""
    conn = db_module.connect()
    try:
        text = finai_module.jd_text(conn, posting_id)
    finally:
        conn.close()
    if text is None:
        print(f"no posting {posting_id}", file=sys.stderr)
        raise typer.Exit(code=1)
    print(text)


@finai_app.command("record")
def finai_record(
    posting_id: int,
    report: Path = typer.Option(..., "--report", help="Path to the match-report JSON."),
) -> None:
    """Validate a match report and store it as a match_report artifact."""
    try:
        data = json.loads(report.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read report: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from exc
    errors = finai_module.validate_report(data)
    if errors:
        for err in errors:
            print(f"invalid report: {err}", file=sys.stderr)
        raise typer.Exit(code=1)
    conn = db_module.connect()
    try:
        if conn.execute("SELECT 1 FROM postings WHERE id=?", (posting_id,)).fetchone() is None:
            print(f"no posting {posting_id}", file=sys.stderr)
            raise typer.Exit(code=1)
        for note in finai_module.record_report(conn, posting_id, data):
            print(note)
    finally:
        conn.close()


autoapply_app = typer.Typer(
    help=(
        "Greenhouse/Lever/Ashby apply lane. Dry (never clicks) until armed; armed, it fills "
        "the form and hands you a visible browser at Submit. It never clicks Submit itself."
    )
)
app.add_typer(autoapply_app, name="autoapply")


@autoapply_app.command("run")
def autoapply_run(
    limit: int | None = typer.Option(
        None, "--limit", help="Cap on postings taken to the browser this run."
    ),
) -> None:
    """Tailor, fill and gate postings. Dry until `autoapply arm`; armed, it fills a stack of forms in their own tabs, then hands you the whole stack at once (you click Submit and clear any check)."""
    conn = db_module.connect()
    try:
        summary = autoapply_module.run_autoapply(conn, limit=limit)
    except applylock.ApplySessionActiveError as exc:
        print(str(exc))
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    print(autoapply_module.format_summary(summary))
    if summary["aborted"]:
        raise typer.Exit(code=1)


@autoapply_app.command("status")
def autoapply_status(
    require_armed: bool = typer.Option(
        False, "--require-armed", help="Exit 1 unless the lane is armed (guards schedule-install)."
    ),
) -> None:
    """Mode, dry runs used, today's submissions and handoffs, KILL file, and the needs-you count."""
    info = autoapply_module.status_summary()
    print(autoapply_module.format_status(info))
    if require_armed and not info["armed"]:
        print("not armed: finish the dry runs, review the digest, then `autoapply arm`.", file=sys.stderr)
        raise typer.Exit(code=1)


@autoapply_app.command("arm")
def autoapply_arm(
    approved: str = typer.Option(
        ..., "--approved", help="Your approval, in your words; stored with the arm time."
    ),
) -> None:
    """Switch from dry to live. Refused until the dry-run quota has been recorded."""
    try:
        state = autoapply_module.arm(approved=approved)
    except autoapply_module.ArmError as exc:
        print(f"autoapply arm: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from exc
    print(f"armed at {state['armed_at']}: the next run fills each form and hands you the browser at Submit; "
        "you click Submit and clear any check. Stop it with `autoapply disarm`.")


@autoapply_app.command("disarm")
def autoapply_disarm() -> None:
    """Back to dry mode: no more clicks. Dry-run history is kept."""
    autoapply_module.disarm()
    print("disarmed: the next run is dry (fills and gates, never clicks).")


@app.command(name="simplify-import")
def simplify_import(
    path: Path = typer.Argument(simplify_import_module.DEFAULT_EXPORT_PATH, help="Simplify Job Tracker export CSV."),
    apply: bool = typer.Option(False, "--apply", help="Write changes. Default is a dry-run that writes nothing."),
    saved_to_ready: bool = typer.Option(
        False, "--saved-to-ready", help="Treat Saved rows as 'ready'. Default: Saved is a reported no-op."
    ),
) -> None:
    """Import Simplify's Job Tracker CSV export into the funnel (dry-run by default)."""
    if not path.exists():
        print(simplify_import_module.MISSING_FILE_HELP)
        raise typer.Exit(code=1)
    try:
        rows = simplify_import_module.read_rows(path)
    except simplify_import_module.HeaderError as exc:
        print(f"simplify-import: {exc}")
        raise typer.Exit(code=1)
    conn = db_module.connect()
    try:
        plan = simplify_import_module.plan_import(conn, rows, saved_to_ready=saved_to_ready)
        if apply:
            simplify_import_module.apply_plan(conn, plan)
        print(simplify_import_module.format_plan(plan, applied=apply))
    finally:
        conn.close()


@app.command()
def dedupe(dry_run: bool = DEDUPE_DRY_RUN_OPTION) -> None:
    """Mark aggregator reposts (simplify/hn/adzuna) of roles already harvested
    directly (greenhouse/lever/ashby/smartrecruiters/workable) as duplicates,
    so scoring/shortlist/sheets (status='active') see each role once."""
    conn = db_module.connect()
    try:
        if dry_run:
            result, plan = dedupe_module.preview_duplicate_groups(conn)
            print(
                f"dedupe (dry-run): {result.groups_found} group(s), "
                f"{result.marked_duplicate} would be marked duplicate, "
                f"{result.protected_by_funnel} protected by funnel"
            )
            for winner, losers in plan[:10]:
                loser_desc = ", ".join(f"[{r['id']}]:{r['source']}" for r in losers)
                print(
                    f"  winner [{winner['id']}]:{winner['source']} {winner['company']} — "
                    f"{winner['title']} <- {loser_desc}"
                )
        else:
            dedupe_module.run_dedupe(conn)
    finally:
        conn.close()


@app.command()
def report() -> None:
    """Funnel stage counts, conversion, weekly leading indicators, top-15 unworked, next actions due."""
    conn = db_module.connect()
    try:
        # Liveness first: a hand-run report is the surface that catches a
        # scheduler that has been dead all week.
        for line in runlog.render_liveness(runlog.job_status(conn)):
            print(line)
        cap = spend_module._cap_usd()
        spent = spend_module.today_spend(conn)
        print(f"spend today: ${spent:.2f} of ${cap:.2f} cap")
        breakdown = spend_module.today_breakdown(conn)
        if breakdown:
            split = ", ".join(f"{job}: ${total:.2f}" for job, total in sorted(breakdown.items()))
            print(f"  by job: {split}")
        print()
        data = report_module.build_report(conn)
        print(report_module.format_report(data))
    finally:
        conn.close()


@app.command()
def digest(print_: bool = DIGEST_PRINT_OPTION) -> None:
    """Build today's digest and write it to data/digest/YYYY-MM-DD.md."""
    conn = db_module.connect()
    try:
        path = digest_module.write_digest(conn)
        if print_:
            print(path.read_text(encoding="utf-8"))
        else:
            print(path)
    finally:
        conn.close()


@app.command()
def suggest(threshold: int = SUGGEST_THRESHOLD_OPTION, limit: int = SUGGEST_LIMIT_OPTION) -> None:
    """Auto-suggest postings scoring >= threshold that aren't in the funnel yet (backfill: top `limit`)."""
    conn = db_module.connect()
    try:
        ids = suggest_module.run_suggest(conn, threshold=threshold, limit=limit)
        print(f"Suggested {len(ids)} posting(s): {ids}")
    finally:
        conn.close()


@app.command()
def prep(
    posting_id: int,
    kind: str | None = PREP_KIND_OPTION,
    model: str | None = PREP_MODEL_OPTION,
    outside_sprint: bool = PREP_OUTSIDE_SPRINT_OPTION,
) -> None:
    """Generate a draft (tailoring/cover/outreach) for a posting via OpenRouter.

    Costs real money — requires SPEND_OK=1 and OPENROUTER_API_KEY in the
    environment. Defaults to generating all three kinds. When an application
    sprint is active, drafting is refused for postings outside it unless
    --outside-sprint is passed."""
    if kind is not None and kind not in prep_module.KINDS:
        raise typer.BadParameter(f"--kind must be one of {prep_module.KINDS}")
    kinds = [kind] if kind else list(prep_module.KINDS)

    conn = db_module.connect()
    try:
        for k in kinds:
            try:
                draft = prep_module.generate_draft(
                    conn, posting_id, k, model=model, allow_non_sprint=outside_sprint
                )
            except prep_module.SprintScopeError as exc:
                raise typer.BadParameter(str(exc)) from exc
            except openrouter_module.SpendGateError as exc:
                print(f"[{k}] blocked: {exc}")
                print("  export SPEND_OK=1 and OPENROUTER_API_KEY to enable real generation.")
                continue
            except spend_module.SpendCapError as exc:
                print(f"[{k}] blocked: {exc}")
                print("  wait for the daily cap to reset (UTC midnight) or raise JOBHUNT_DAILY_CAP_USD.")
                continue
            print(f"=== {k} (${draft['cost_usd']:.4f}, {draft['model']}) ===")
            print(draft["content"])
            print()
    finally:
        conn.close()


@app.command()
def pack(posting_id: int, print_: bool = PACK_PRINT_OPTION) -> None:
    """Write the Apply Pack for a posting to data/apply_packs/ and print
    its path (or the full markdown with --print)."""
    conn = db_module.connect()
    try:
        try:
            path = applypack_module.write_pack(conn, posting_id)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if print_:
            print(path.read_text(encoding="utf-8"))
        else:
            print(path)
    finally:
        conn.close()


@app.command()
def pipeline(stage: str | None = PIPELINE_STAGE_OPTION) -> None:
    """List funnel rows: id, stage, score, company, title, next_action."""
    conn = db_module.connect()
    try:
        try:
            rows = report_module.fetch_pipeline_rows(conn, stage)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if not rows:
            print("No funnel rows yet — run `qualify` on a posting first.")
            return
        print(report_module.format_pipeline_rows(rows))
    finally:
        conn.close()


@app.command()
def autopilot() -> None:
    """Advance every funnel member to its human gate and fill blank next
    actions (resume auto-attach, ready/parked, follow-up cadence). No LLM,
    no network, no spend."""
    conn = db_module.connect()
    try:
        result = autopilot_module.run_autopilot(conn)
        print(f"advanced: {len(result['advanced'])}")
        print(f"resume_attached: {len(result['resume_attached'])}")
        print(f"next_actions_set: {result['next_actions_set']}")
        if result["parked"]:
            print("parked:")
            for blocker, count in sorted(result["parked"].items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"  {blocker}: {count}")
    finally:
        conn.close()


@app.command()
def apply(posting_id: int, scan_only: bool = APPLY_SCAN_ONLY_OPTION) -> None:
    """Pre-fill a posting's hosted application form in a visible browser.

    Never submits — the candidate reviews and submits by hand. Requires
    `uv run playwright install chromium` on first use if no system Chrome
    is found."""
    from jobhunt import lifecycle as lifecycle_module
    from jobhunt.apply_assist import profile_store as apply_profile_store_module
    from jobhunt.apply_assist.detect import detect_ats
    from jobhunt.apply_assist.digest import render_digest as render_apply_digest

    conn = db_module.connect()
    try:
        posting = conn.execute(
            "SELECT id, url, company, title FROM postings WHERE id = ?", (posting_id,)
        ).fetchone()
        if posting is None:
            print(f"No posting with id={posting_id}")
            raise typer.Exit(code=1)
        url = posting["url"]
        if not url:
            print(f"Posting {posting_id} has no url — nothing to open.")
            raise typer.Exit(code=1)

        if detect_ats(url) is None:
            print("Unknown ATS — generic best-effort scan.")

        answers = apply_profile_store_module.get_answers(conn)

        # Prefers this posting's tailored render, else the standing variant.
        candidate = applypack_module.resume_path_for(conn, posting_id)
        if candidate == applypack_module.RESUME_PATH:
            resume_row = conn.execute(
                "SELECT path FROM resume_variants ORDER BY id LIMIT 1"
            ).fetchone()
            if resume_row is not None:
                candidate = resume_row["path"]
        resume_path = candidate if Path(candidate).exists() else None

        # Imported lazily: pulling in Playwright at module import time would
        # make it a hard dependency for every `uv run python -m jobhunt ...`
        # invocation, including plain pytest collection.
        from jobhunt.apply_assist import driver as driver_module
        from jobhunt.apply_assist import fill as fill_module

        handle, page, is_cdp = driver_module.open_page(url)
        triage = fill_module.run_assist(
            page, posting_id, url, answers, resume_path, scan_only=scan_only
        )

        print(render_apply_digest(triage))

        lifecycle_module.log_activity(
            conn,
            "posting",
            posting_id,
            "apply_assist_scan" if scan_only else "apply_assist_fill",
            data=triage.to_dict(),
        )
        conn.commit()

        if not is_cdp:
            typer.prompt(
                "Browser stays open for your review — press Enter here AFTER "
                "you have reviewed (and submitted by hand, if you chose to)",
                default="",
                show_default=False,
            )

        if not scan_only:
            try:
                from jobhunt.apply_assist import submitwatch as submitwatch_module

                if submitwatch_module.page_submitted(page):
                    status, blockers = submitwatch_module.record_submission(conn, posting_id)
                    if status == "confirmed":
                        print(f"recorded submission for posting #{posting_id} (success page)")
                    else:
                        print(
                            f"posting #{posting_id} success page seen but blocked: "
                            f"{', '.join(blockers)}"
                        )
            except Exception as exc:  # noqa: BLE001 — page may already be closed
                print(f"submission check skipped: {exc}")

        driver_module.close(handle, page, is_cdp)

        print(
            "If you submitted, run the existing applied-confirmation flow "
            "(the `pack`/funnel commands) to record it."
        )
    finally:
        conn.close()


def _queue_browser_gone(context) -> bool:
    """True when the review browser session is over: every tab closed, the
    context/browser raised (already torn down), or the browser process died
    abnormally (crash/SIGTERM keeps Playwright's cached `pages` list
    non-empty, so the disconnect check is the only signal)."""
    try:
        if not context.pages:
            return True
        browser = getattr(context, "browser", None)
        if browser is not None and not browser.is_connected():
            return True
    except Exception:  # noqa: BLE001 — a closed context/browser ends the wait
        return True
    return False


def _review_drafts(page, digest, fill_module) -> None:
    """Walk one posting's AI-drafted answers and let the candidate approve or
    reject each before it is typed into the live form. Never types a draft
    without an explicit confirm — a "no" leaves the field blank."""
    for res in digest.drafts:
        print()
        print(f"posting #{digest.posting_id}")
        label = res.field.label or res.field.name or res.field.selector
        print(label)
        print(res.value or "")
        if typer.confirm("Type this AI draft into the form?", default=False):
            outcome = fill_module.apply_one(page, res)
            if outcome.committed:
                print("typed ✓")
            else:
                print(f"typed ✗ (readback mismatch: {outcome.readback!r})")
        else:
            print("left blank")


def _close_queue_context(handle, context, is_cdp: bool) -> None:
    """Tear down a batch-queue browser session. Same never-close-the-candidate's-own-
    browser rule as driver.close(), just operating on a context instead of a
    single page (the queue opens many tabs, not one)."""
    if is_cdp:
        try:
            browser = context.browser
            if browser is not None:
                browser.close()
        finally:
            handle.stop()
        return
    try:
        context.close()
    finally:
        handle.stop()


def _print_submitwatch_results(
    confirmed: list[int], blocked: list[tuple[int, list[str]]]
) -> None:
    for posting_id in confirmed:
        print(f"recorded submission for posting #{posting_id} (success page)")
    for posting_id, blockers in blocked:
        print(f"posting #{posting_id} success page seen but blocked: {', '.join(blockers)}")


def _run_confirmation_scan_backstop(conn: sqlite3.Connection) -> None:
    """Post-session Gmail confirmation-email scan — the backstop for any
    submission the live success-page watch missed. Never fatal: a Gmail
    auth/network problem should not fail the apply-queue run."""
    from jobhunt import confirmations as confirmations_module

    try:
        report = confirmations_module.run_scan(conn)
        note = (
            f"{len(report['confirmed'])} confirmed, {len(report['blocked'])} blocked, "
            f"{len(report['ambiguous'])} ambiguous, {len(report['unmatched'])} unmatched, "
            f"{report['skipped']} skipped"
        )
        print(f"confirmation email scan: {note}")
    except Exception as exc:  # noqa: BLE001 — a backstop scan must never fail the run
        print(f"confirmation email scan skipped: {exc}")


@app.command(name="apply-queue")
def apply_queue(
    limit: int = QUEUE_LIMIT_OPTION,
    min_score: int | None = QUEUE_MIN_SCORE_OPTION,
    scan_only: bool = APPLY_SCAN_ONLY_OPTION,
    posting_id: list[int] | None = QUEUE_POSTING_ID_OPTION,
    tailor: bool = APPLY_TAILOR_OPTION,
) -> None:
    """Batch pre-fill hosted application forms for the top of the queue
    (suggested + ready-not-submitted postings), one browser, many tabs.

    Never submits — the candidate reviews and submits by hand, per tab. Requires
    `uv run playwright install chromium` on first use if no system Chrome
    is found. Meant to run both interactively and unattended (see
    JOBHUNT_QUEUE_TIMEOUT_MIN in the README) from the Mon-Fri 08:15
    schedule installed by `make schedule-install-applyqueue`."""
    from jobhunt.apply_assist import profile_store as apply_profile_store_module
    from jobhunt.apply_assist import queue as queue_module
    from jobhunt.apply_assist import submitwatch as submitwatch_module

    try:
        with applylock.held():
            _apply_queue_body(
                limit, min_score, scan_only, posting_id, tailor,
                apply_profile_store_module, queue_module, submitwatch_module,
            )
    except applylock.ApplySessionActiveError as exc:
        print(str(exc))
        raise typer.Exit(code=1) from exc


def _apply_queue_body(
    limit: int,
    min_score: int | None,
    scan_only: bool,
    posting_id: list[int] | None,
    tailor: bool,
    apply_profile_store_module,
    queue_module,
    submitwatch_module,
) -> None:
    conn = db_module.connect()
    try:
        with runlog.track(conn, "apply-queue") as run:
            rows = queue_module.select_queue(conn, limit, min_score, posting_id)
            if not rows:
                print("apply-queue: nothing to pre-fill")
                notify_module.notify("apply-queue", "nothing to pre-fill")
                # A plain return, not typer.Exit(0): an empty queue is a healthy
                # run, and the heartbeat must record it as one.
                run.note("nothing to pre-fill")
                return

            answers = apply_profile_store_module.get_answers(conn)

            resume_row = conn.execute(
                "SELECT path FROM resume_variants ORDER BY id LIMIT 1"
            ).fetchone()
            resume_path = None
            if resume_row is not None and Path(resume_row["path"]).exists():
                resume_path = resume_row["path"]

            budget = spend_module.RunBudget(conn, job="apply-queue")
            (
                digests,
                pages,
                handle,
                context,
                is_cdp,
                mapper_results,
                tailor_results,
            ) = queue_module.run_queue(
                conn, rows, answers, resume_path, scan_only=scan_only, budget=budget, tailor=tailor
            )

            print(queue_module.render_queue_summary(digests, mapper_results, tailor_results))
            run.note(f"pre-filled {len(digests)} of {len(rows)} queued")

            notify_module.notify(
                "apply-queue",
                f"{len(digests)} applications pre-filled — review the browser tabs, "
                "submit by hand, close the window when done.",
            )

            tab_map = [(d.posting_id, p) for d, p in zip(digests, pages) if p is not None]

            if sys.stdin.isatty():
                if not scan_only:
                    from jobhunt.apply_assist import fill as fill_module

                    for digest, page in zip(digests, pages):
                        if digest.drafts and page is not None:
                            _review_drafts(page, digest, fill_module)
                typer.prompt(
                    "Browser stays open for your review — press Enter here AFTER "
                    "you have reviewed (and submitted by hand, if you chose to)",
                    default="",
                    show_default=False,
                )
                confirmed, blocked = submitwatch_module.sweep_submitted(conn, tab_map)
                _print_submitwatch_results(confirmed, blocked)
                _close_queue_context(handle, context, is_cdp)
            elif is_cdp:
                _close_queue_context(handle, context, is_cdp)
            else:
                total_drafts = sum(len(d.drafts) for d in digests)
                if total_drafts:
                    print(
                        f"{total_drafts} AI drafts NOT typed (unattended) — "
                        "rerun attended or fill by hand"
                    )
                timeout_min = int(os.environ.get("JOBHUNT_QUEUE_TIMEOUT_MIN", "240"))
                deadline = time.monotonic() + timeout_min * 60
                while time.monotonic() < deadline:
                    queue_module.sweep_blank_tabs(context)
                    confirmed, blocked = submitwatch_module.sweep_submitted(conn, tab_map)
                    _print_submitwatch_results(confirmed, blocked)
                    if _queue_browser_gone(context):
                        break
                    time.sleep(5)
                _close_queue_context(handle, context, is_cdp)

            if not scan_only:
                _run_confirmation_scan_backstop(conn)

            print("Any submission not auto-recorded: run the pack/funnel confirmation flow.")
    finally:
        conn.close()


STANDARD_TAILOR_QUESTIONS = [
    "Why do you want to work at {company}?",
    "Why are you interested in this role?",
    "Tell us about a project you're proud of.",
]


TAILOR_OPTION = typer.Option(
    True,
    "--tailor/--no-tailor",
    help="Pre-draft answers to standard essay questions on the free local model "
    "(or OpenRouter with SPEND_OK=1) before tabs open, printed for you to paste. "
    "Display-only — never typed. --no-tailor for a fast open.",
)


@app.command(name="apply-lite")
def apply_lite(
    limit: int = QUEUE_LIMIT_OPTION,
    min_score: int | None = QUEUE_MIN_SCORE_OPTION,
    posting_id: list[int] | None = QUEUE_POSTING_ID_OPTION,
    tailor: bool = TAILOR_OPTION,
) -> None:
    """Open the apply queue in your OWN default browser — no Playwright, no
    managed profile, no field mapping. Autofill is delegated to a browser
    extension (e.g. Simplify Copilot); you review and submit by hand.
    Submissions are recorded by the Gmail confirmation-email scan after you
    press Enter."""
    import webbrowser

    from jobhunt import lifecycle as lifecycle_module
    from jobhunt.apply_assist import queue as queue_module

    try:
        with applylock.held():
            _apply_lite_body(
                limit, min_score, posting_id, tailor, webbrowser, lifecycle_module, queue_module
            )
    except applylock.ApplySessionActiveError as exc:
        print(str(exc))
        raise typer.Exit(code=1) from exc


def _apply_lite_tailor_drafts(conn: sqlite3.Connection, rows) -> list:
    """Pre-draft standard-question answers for each queued posting, gated the
    same way daily.py gates auto-prep. Never raises — a drafting failure for
    one posting (or the whole gate being closed) must never block tab-opening.
    Returns the list of TailorResult objects with any drafts to print."""
    from jobhunt.apply_assist.schema import FieldSpec
    from jobhunt.apply_assist.tailor_answers import draft_for_posting

    if not (
        openrouter_module.is_local_configured()
        or (os.environ.get("SPEND_OK") == "1" and os.environ.get("OPENROUTER_API_KEY"))
    ):
        print(
            "apply-lite: tailoring skipped (requires a local model configured via "
            "JOBHUNT_LLM_BASE_URL, or SPEND_OK=1 + OPENROUTER_API_KEY)"
        )
        return []

    budget = spend_module.RunBudget(conn, job="apply-lite")
    results = []
    for row in rows:
        company = row["company"]
        fields = [
            FieldSpec(selector="", kind="textarea", label=q.format(company=company))
            for q in STANDARD_TAILOR_QUESTIONS
        ]
        try:
            result = draft_for_posting(conn, row["posting_id"], fields, budget)
        except Exception as exc:  # noqa: BLE001 — tailoring must never block tab-opening
            print(f"apply-lite: tailoring failed for #{row['posting_id']}: {exc}")
            continue
        if result.status in ("error", "gate_closed", "over_cap"):
            print(f"apply-lite: tailoring for #{row['posting_id']}: {result.status} ({result.detail})")
            continue
        if result.drafts:
            results.append((row, result))
    return results


def _print_apply_lite_drafts(tailored) -> None:
    if not tailored:
        return
    print("--- tailored drafts (paste-only, review before use) ---")
    for row, result in tailored:
        print(f"#{row['posting_id']}: {row['company']} — {row['title']}")
        for _fp, (question, answer) in result.drafts.items():
            print(f"  Q: {question}")
            print(f"  A: {answer}")


def _apply_lite_body(
    limit: int,
    min_score: int | None,
    posting_id: list[int] | None,
    tailor: bool,
    webbrowser,
    lifecycle_module,
    queue_module,
) -> None:
    conn = db_module.connect()
    try:
        with runlog.track(conn, "apply-lite") as run:
            rows = queue_module.select_queue(conn, limit, min_score, posting_id)
            if not rows:
                print("apply-lite: nothing to open")
                run.note("nothing to open")
                return

            tailored: list = []
            if tailor:
                tailored = _apply_lite_tailor_drafts(conn, rows)
                _print_apply_lite_drafts(tailored)

            for row in rows:
                url = queue_module.application_url(row["url"])
                webbrowser.open_new_tab(url)
                print(f"opened #{row['posting_id']}: {row['company']} — {row['title']}")
                lifecycle_module.log_activity(
                    conn, "posting", row["posting_id"], "apply_lite_open", data={"url": url}
                )
            conn.commit()
            run.note(f"opened {len(rows)} postings in default browser")

            if sys.stdin.isatty():
                typer.prompt(
                    "Tabs are in your normal browser — press Enter here AFTER "
                    "you have reviewed (and submitted by hand, if you chose to)",
                    default="",
                    show_default=False,
                )
                _run_confirmation_scan_backstop(conn)
            print("Any submission not auto-recorded: run the pack/funnel confirmation flow.")
    finally:
        conn.close()


@app.command(name="tailor-answers")
def tailor_answers_cmd(posting_id: int) -> None:
    """Draft answers to one posting's eligible essay/short-answer questions
    (score >= TAILOR_MIN_SCORE under the current rubric_version), grounded on
    data/facts.local.md. Never submits, never fills anything the drafter
    didn't produce — drafted answers are never typed into the form here.
    They show up under the digest's AI DRAFTS section for review; type them
    yourself, or use `jobhunt apply-queue` for the per-draft approval
    prompt."""
    from jobhunt import lifecycle as lifecycle_module
    from jobhunt.apply_assist import driver as driver_module
    from jobhunt.apply_assist import fill as fill_module
    from jobhunt.apply_assist import profile_store as apply_profile_store_module
    from jobhunt.apply_assist.detect import detect_ats
    from jobhunt.apply_assist.digest import render_digest as render_apply_digest
    from jobhunt.apply_assist.tailor_answers import TailorResult

    conn = db_module.connect()
    try:
        posting = conn.execute(
            "SELECT id, url, company, title FROM postings WHERE id = ?", (posting_id,)
        ).fetchone()
        if posting is None:
            print(f"No posting with id={posting_id}")
            raise typer.Exit(code=1)
        url = posting["url"]
        if not url:
            print(f"Posting {posting_id} has no url — nothing to open.")
            raise typer.Exit(code=1)

        if detect_ats(url) is None:
            print("Unknown ATS — generic best-effort scan.")

        answers = apply_profile_store_module.get_answers(conn)

        candidate = applypack_module.resume_path_for(conn, posting_id)
        if candidate == applypack_module.RESUME_PATH:
            resume_row = conn.execute(
                "SELECT path FROM resume_variants ORDER BY id LIMIT 1"
            ).fetchone()
            if resume_row is not None:
                candidate = resume_row["path"]
        resume_path = candidate if Path(candidate).exists() else None

        budget = spend_module.RunBudget(conn, job="tailor-answers")
        tailor_sink: list[TailorResult] = []

        handle, page, is_cdp = driver_module.open_page(url)
        triage = fill_module.run_assist(
            page,
            posting_id,
            url,
            answers,
            resume_path,
            conn=conn,
            budget=budget,
            tailor=True,
            tailor_sink=tailor_sink,
        )

        print(render_apply_digest(triage))

        if tailor_sink:
            total_drafted = sum(len(tr.drafts) for tr in tailor_sink)
            total_cost = sum(tr.cost_usd for tr in tailor_sink)
            print(f"\nDRAFTED (review before submit): {total_drafted} field(s), ${total_cost:.4f}")
            for tr in tailor_sink:
                for _fp, (question, answer) in tr.drafts.items():
                    print(f"  {question[:60]!r}: {answer[:60]}")
                if tr.status not in ("ok", "all_cached", "no_fields"):
                    print(f"  TAILOR ALERT: status={tr.status} — {tr.detail}")
        else:
            print("\nNo tailoring ran (posting below TAILOR_MIN_SCORE, or nothing eligible).")

        lifecycle_module.log_activity(
            conn,
            "posting",
            posting_id,
            "apply_assist_fill",
            data=triage.to_dict(),
        )
        conn.commit()

        if not is_cdp:
            typer.prompt(
                "Browser stays open for your review — press Enter here AFTER "
                "you have reviewed (and submitted by hand, if you chose to)",
                default="",
                show_default=False,
            )
        driver_module.close(handle, page, is_cdp)

        print(
            "If you submitted, run the existing applied-confirmation flow "
            "(the `pack`/funnel commands) to record it."
        )
    finally:
        conn.close()


# apply-profile import: maps data/answers.local.md keys onto SEMANTIC_KEYS.
# Keys not listed here map to themselves (when they are in SEMANTIC_KEYS).
_ANSWERS_FILE_KEY_MAP: dict[str, str] = {
    "earliest_start": "start_date",
    "how_did_you_hear": "how_heard",
    "portfolio": "website",
}
# Sensitive file keys that never get imported: they always resolve
# interactively at fill-time, so storing a value for them is pointless.
_ANSWERS_FILE_SENSITIVE_SKIP: frozenset[str] = frozenset({"pronouns_eeo_demographics"})
_ANSWERS_FILE_FILL_PLACEHOLDER = "[FILL]"
_ANSWERS_FILE_WARN_CHARS = 60

APPLY_PROFILE_IMPORT_FILE_OPTION = typer.Option(
    Path("data/answers.local.md"),
    "--file",
    help="Answers file to import (key: value lines).",
)
APPLY_PROFILE_OVERWRITE_OPTION = typer.Option(
    False, "--overwrite", help="Overwrite semantic keys that already have a stored value."
)


def _parse_answers_file(path: Path) -> list[tuple[str, str]]:
    """Parse `key: value` lines from an answers file; skip blanks/#comments."""
    pairs: list[tuple[str, str]] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        file_key, _, raw_value = line.partition(":")
        pairs.append((file_key.strip(), raw_value.strip()))
    return pairs


def _import_answers_file(
    conn: sqlite3.Connection, path: Path, overwrite: bool
) -> list[tuple[str, str, str]]:
    """Import `key: value` pairs from `path` into application_answers.

    Returns one (bucket, file_key, reason) row per parsed line, where bucket
    is one of imported/skipped_already_set/skipped_fill/skipped_unmapped.
    """
    from jobhunt.apply_assist import profile_store as apply_profile_store_module
    from jobhunt.apply_assist.keys import SEMANTIC_KEYS

    existing = apply_profile_store_module.get_answers(conn)
    rows: list[tuple[str, str, str]] = []
    for file_key, raw_value in _parse_answers_file(path):
        if raw_value == _ANSWERS_FILE_FILL_PLACEHOLDER:
            rows.append(("skipped_fill", file_key, "value is [FILL] — not filled in yet"))
            continue
        if file_key in _ANSWERS_FILE_SENSITIVE_SKIP:
            rows.append(
                (
                    "skipped_unmapped",
                    file_key,
                    "sensitive key — always resolved interactively, storing is pointless",
                )
            )
            continue
        semantic_key = _ANSWERS_FILE_KEY_MAP.get(file_key, file_key)
        if semantic_key not in SEMANTIC_KEYS:
            rows.append(("skipped_unmapped", file_key, f"no semantic key for {file_key!r}"))
            continue
        if semantic_key in existing and not overwrite:
            rows.append(
                (
                    "skipped_already_set",
                    file_key,
                    f"{semantic_key} already set (use --overwrite to replace)",
                )
            )
            continue
        apply_profile_store_module.set_answer(conn, semantic_key, raw_value)
        existing[semantic_key] = raw_value
        reason = f"-> {semantic_key}"
        if len(raw_value) > _ANSWERS_FILE_WARN_CHARS:
            reason += " (WARN: prose-like value, check it reads well typed into a form field)"
        rows.append(("imported", file_key, reason))
    return rows


def _print_answers_import_summary(rows: list[tuple[str, str, str]]) -> None:
    buckets: dict[str, list[tuple[str, str]]] = {
        "imported": [],
        "skipped_already_set": [],
        "skipped_fill": [],
        "skipped_unmapped": [],
    }
    for bucket, file_key, reason in rows:
        buckets[bucket].append((file_key, reason))
    for bucket_name, items in buckets.items():
        print(f"{bucket_name} ({len(items)}):")
        if not items:
            print("  (none)")
        for file_key, reason in items:
            print(f"  {file_key}: {reason}")


@app.command(name="apply-profile")
def apply_profile(
    action: str = typer.Argument(..., help="list|set|delete|import"),
    key: str = typer.Argument(None),
    value: str = typer.Argument(None),
    file: Path = APPLY_PROFILE_IMPORT_FILE_OPTION,
    overwrite: bool = APPLY_PROFILE_OVERWRITE_OPTION,
) -> None:
    """Manage confirmed application-form answers (application_answers).

    `import` reads `key: value` lines from `--file` (default
    data/answers.local.md) and stores each as a semantic-key answer, mapping
    a few file-specific key names onto SEMANTIC_KEYS along the way.
    """
    from jobhunt.apply_assist import profile_store as apply_profile_store_module
    from jobhunt.apply_assist.keys import SEMANTIC_KEYS

    conn = db_module.connect()
    try:
        if action == "list":
            rows = apply_profile_store_module.list_answers(conn)
            if not rows:
                print("No answers stored yet.")
            else:
                print(f"{'key':<24}{'value':<40}updated_at")
                print("-" * 90)
                for row in rows:
                    print(f"{row['semantic_key']:<24}{row['value']:<40}{row['updated_at']}")
            unset = sorted(SEMANTIC_KEYS - {row["semantic_key"] for row in rows})
            print()
            print(f"Unset keys: {', '.join(unset) if unset else '(none)'}")
        elif action == "set":
            if not key or value is None:
                raise typer.BadParameter(
                    "`apply-profile set <key> <value>` requires both key and value"
                )
            try:
                apply_profile_store_module.set_answer(conn, key, value)
            except ValueError as exc:
                raise typer.BadParameter(
                    f"{exc}. Valid keys: {', '.join(sorted(SEMANTIC_KEYS))}"
                ) from exc
            print(f"{key} = {value}")
        elif action == "delete":
            if not key:
                raise typer.BadParameter("`apply-profile delete <key>` requires a key")
            try:
                apply_profile_store_module.delete_answer(conn, key)
            except ValueError as exc:
                raise typer.BadParameter(
                    f"{exc}. Valid keys: {', '.join(sorted(SEMANTIC_KEYS))}"
                ) from exc
            print(f"deleted {key}")
        elif action == "import":
            if not file.exists():
                raise typer.BadParameter(f"answers file not found: {file}")
            import_rows = _import_answers_file(conn, file, overwrite)
            _print_answers_import_summary(import_rows)
        else:
            raise typer.BadParameter("action must be one of: list, set, delete, import")
    finally:
        conn.close()


apply_map_app = typer.Typer(
    help="Inspect/edit the LLM field-mapper's write-through cache (field_key_map)."
)
app.add_typer(apply_map_app, name="apply-map")


@apply_map_app.command("list")
def apply_map_list() -> None:
    """List every cached field->semantic_key mapping (including cached nulls)."""
    conn = db_module.connect()
    try:
        rows = conn.execute(
            """SELECT fingerprint, semantic_key, label, source, created_at
               FROM field_key_map ORDER BY created_at DESC"""
        ).fetchall()
        if not rows:
            print("No cached field mappings yet.")
            return
        print(f"{'fingerprint':<18}{'key':<22}{'label':<40}{'source':<8}created_at")
        print("-" * 100)
        for row in rows:
            key = row["semantic_key"] or "(none)"
            label = (row["label"] or "")[:38]
            print(
                f"{row['fingerprint']:<18}{key:<22}{label:<40}"
                f"{row['source']:<8}{row['created_at']}"
            )
    finally:
        conn.close()


@apply_map_app.command("forget")
def apply_map_forget(fingerprint: str = typer.Argument(..., help="Fingerprint to forget.")) -> None:
    """Delete one cached mapping so the next run re-queries it."""
    conn = db_module.connect()
    try:
        cur = conn.execute("DELETE FROM field_key_map WHERE fingerprint = ?", (fingerprint,))
        conn.commit()
        if cur.rowcount:
            print(f"forgot {fingerprint}")
        else:
            print(f"no cached mapping for {fingerprint}")
    finally:
        conn.close()


resume_app = typer.Typer(help="Render the structured resume (resume/resume.yaml) to PDF.")
app.add_typer(resume_app, name="resume")

RESUME_VARIANT_OPTION = typer.Option(
    "founder_engineer", "--variant", help="Which variant in resume.yaml to render."
)
RESUME_OUT_OPTION = typer.Option(None, "--out", help="Override the output PDF path.")
RESUME_POSTING_OPTION = typer.Option(
    None, "--posting-id", help="Name the output <posting_id>-<variant>.pdf."
)


@resume_app.command("variants")
def resume_variants_cmd() -> None:
    """List the variants declared in resume/resume.yaml."""
    data = resume_render_module.load_resume()
    variants = data.get("variants") or {}
    if not variants:
        print("no variants declared in resume.yaml")
        raise typer.Exit(code=1)
    for name, spec in variants.items():
        print(f"{name}\t{spec.get('label', '')}\t{spec.get('purpose', '')}")


@resume_app.command("render")
def resume_render_cmd(
    variant: str = RESUME_VARIANT_OPTION,
    out: str | None = RESUME_OUT_OPTION,
    posting_id: int | None = RESUME_POSTING_OPTION,
) -> None:
    """Render a resume variant to a one-page PDF. No LLM call, no spend."""
    try:
        path = resume_render_module.render_variant(
            variant,
            posting_id=posting_id,
            out_path=Path(out) if out else None,
        )
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except FileNotFoundError as exc:
        print(f"blocked: {exc}")
        raise typer.Exit(code=1) from exc
    print(path)


@resume_app.command("tailor")
def resume_tailor_cmd(
    posting_id: int,
    variant: str = RESUME_VARIANT_OPTION,
    model: str | None = PREP_MODEL_OPTION,
) -> None:
    """Tailor the resume to one posting via OpenRouter, then render the PDF.

    Costs real money — requires SPEND_OK=1 and OPENROUTER_API_KEY in the
    environment, and is capped by JOBHUNT_DAILY_CAP_USD. Deliberately manual:
    this is never run on a schedule."""
    conn = db_module.connect()
    try:
        try:
            result = resume_tailor_module.tailor_resume(
                conn, posting_id, variant=variant, model=model
            )
        except openrouter_module.SpendGateError as exc:
            print(f"blocked: {exc}")
            print("  export SPEND_OK=1 and OPENROUTER_API_KEY to enable real generation.")
            raise typer.Exit(code=1) from exc
        except spend_module.SpendCapError as exc:
            print(f"blocked: {exc}")
            print("  wait for the daily cap to reset (UTC midnight) or raise JOBHUNT_DAILY_CAP_USD.")
            raise typer.Exit(code=1) from exc
        except resume_validate_module.ResumeValidationError as exc:
            print(f"rejected: {exc}")
            print("  the model call was still billed and is recorded in spend_ledger.")
            raise typer.Exit(code=1) from exc
        except KeyError as exc:
            raise typer.BadParameter(str(exc)) from exc
    finally:
        conn.close()

    print(f"resume: {result['pdf_path']}")
    print(f"cost:   ${result['cost_usd']:.4f} ({result['model']})")
    if result["payload"].get("rationale"):
        print(f"why:    {result['payload']['rationale']}")


@app.command(name="gmail-auth")
def gmail_auth() -> None:
    """One-time OAuth consent for the Gmail job-alert ingester (LinkedIn/Wellfound
    alert emails). Requires a Desktop-app OAuth client JSON saved at
    data/gmail-oauth-client.json (console.cloud.google.com -> project
    job-hunt-sheets-5002 -> APIs & Services -> Credentials -> Create OAuth
    client ID -> Desktop app). Opens a browser for consent and saves a
    refresh token to data/gmail-token.json; prints no token material."""
    from jobhunt.sources import alerts as alerts_module

    try:
        alerts_module.run_auth_flow()
    except RuntimeError as exc:
        print(str(exc))
        raise typer.Exit(code=1) from exc


CONFIRM_SCAN_DAYS_OPTION = typer.Option(
    14, "--days", help="How many days back to scan for confirmation emails."
)
CONFIRM_SCAN_DRY_RUN_OPTION = typer.Option(
    False, "--dry-run", help="Report matches without marking anything submitted."
)


@app.command(name="confirm-scan")
def confirm_scan(
    days: int = CONFIRM_SCAN_DAYS_OPTION,
    dry_run: bool = CONFIRM_SCAN_DRY_RUN_OPTION,
) -> None:
    """Scan Gmail for ATS "thanks for applying" emails and mark the matched
    postings submitted (same confirm-applied flow as the web UI). Reuses the
    gmail-auth token; ambiguous emails are reported, never guessed."""
    from jobhunt import confirmations as confirmations_module
    from jobhunt.sources import alerts as alerts_module

    if not alerts_module.is_configured():
        print(
            "confirm-scan: Gmail not configured — run `uv run python -m jobhunt "
            "gmail-auth` once (see that command's help for the OAuth client setup)."
        )
        raise typer.Exit(code=1)

    conn = db_module.connect()
    try:
        with runlog.track(conn, "confirm-scan") as run:
            summary = confirmations_module.run_scan(conn, days=days, dry_run=dry_run)
            note = (
                f"{len(summary['confirmed'])} confirmed, {len(summary['blocked'])} blocked, "
                f"{len(summary['ambiguous'])} ambiguous, {len(summary['unmatched'])} unmatched, "
                f"{summary['skipped']} skipped"
            )
            run.note(note)
            print(f"confirm-scan: {note}")
    finally:
        conn.close()


sprint_app = typer.Typer(
    help="Manage the active application sprint (S6): a bounded batch of postings "
    "The candidate is actively driving to submission."
)
app.add_typer(sprint_app, name="sprint")


def _print_sprint_status(status: dict) -> None:
    size_suffix = f", target {status['target_size']}" if status["target_size"] else ""
    print(f"{status['name']} (started {status['started_at']}{size_suffix})")
    if not status["members"]:
        print("  (no members)")
    for member in status["members"]:
        blocker_suffix = f" blockers={member['blockers']}" if member["blockers"] else ""
        print(
            f"  [{member['posting_id']}] {member['company']} — {member['title']} "
            f"({member['stage']}) -> {member['next_action']}{blocker_suffix}"
        )


@sprint_app.command("new")
def sprint_new(
    size: int = SPRINT_SIZE_OPTION,
    name: str | None = SPRINT_NAME_OPTION,
    yes: bool = SPRINT_YES_OPTION,
) -> None:
    """Build a new application sprint from the top-scored eligible, verified,
    early-stage candidates, confirming each one unless --yes is passed."""
    conn = db_module.connect()
    try:
        candidate_rows = sprint_module.candidates(conn, limit=size * 3)
        if not candidate_rows:
            print(
                "No candidates available: no active postings are eligible, verified, "
                "and in an early funnel stage (suggested/qualified/tailored/ready)."
            )
            return

        chosen: list[int] = []
        no_warm_path_notes: dict[int, str] = {}
        for candidate in candidate_rows:
            if len(chosen) >= size:
                break
            prompt = (
                f"Add {candidate['company']} — {candidate['title']} "
                f"(score {candidate['total']}, stage {candidate['stage']})?"
            )
            if yes:
                accepted = True
            else:
                accepted = typer.confirm(prompt)
            if accepted:
                chosen.append(candidate["posting_id"])
                if not yes and not sprint_module.has_warm_path(conn, candidate["posting_id"]):
                    note = typer.prompt(
                        "No warm path found. Note why (Enter to skip and leave as blocker)",
                        default="",
                        show_default=False,
                    )
                    if note.strip():
                        no_warm_path_notes[candidate["posting_id"]] = note

        if not chosen:
            print("No candidates accepted; sprint not created.")
            return

        try:
            sprint_module.create_sprint(conn, chosen, name=name, target_size=size)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc

        for posting_id, note in no_warm_path_notes.items():
            sprint_module.set_no_warm_path_note(conn, posting_id, note)

        _print_sprint_status(sprint_module.sprint_status(conn))
    finally:
        conn.close()


@sprint_app.command("status")
def sprint_status_cmd() -> None:
    """Show the active sprint and each member's stage and next action."""
    conn = db_module.connect()
    try:
        status = sprint_module.sprint_status(conn)
        if status is None:
            print("No active sprint.")
            return
        _print_sprint_status(status)
    finally:
        conn.close()


@sprint_app.command("complete")
def sprint_complete_cmd() -> None:
    """Mark the active sprint completed."""
    conn = db_module.connect()
    try:
        try:
            sprint = sprint_module.complete_sprint(conn)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        print(f"Completed sprint '{sprint['name']}' (id={sprint['id']}).")
    finally:
        conn.close()


@sprint_app.command("no-warm-path")
def sprint_no_warm_path(
    posting_id: int = typer.Argument(..., help="Posting id, must be a member of the active sprint."),
    note: str = typer.Argument(..., help="Why there's no warm path yet."),
) -> None:
    """Record a no-warm-path note for a sprint member, clearing its blocker."""
    conn = db_module.connect()
    try:
        try:
            sprint_module.set_no_warm_path_note(conn, posting_id, note)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        _print_sprint_status(sprint_module.sprint_status(conn))
    finally:
        conn.close()


@app.command(name="variant-plan")
def variant_plan(
    stage: list[str] = typer.Option(
        None, "--stage", help="Funnel stage to include (repeatable). Default: ready and suggested."
    ),
    apply: bool = typer.Option(False, "--apply", help="Write resume_variant_id for 'set' rows. Default is a dry-run."),
) -> None:
    """Show (and optionally apply) the deterministic resume-variant assignment over the queue."""
    stages = tuple(stage) if stage else resume_select_module.DEFAULT_STAGES
    conn = db_module.connect()
    try:
        plan = resume_select_module.plan_variants(conn, stages)
        print(resume_select_module.format_plan(plan))
        if apply:
            count = resume_select_module.apply_plan(conn, plan)
            print(f"\nApplied: set resume variant on {count} application(s).")
        else:
            print("\nDry run: nothing written. Pass --apply to write 'set' rows.")
    finally:
        conn.close()


if __name__ == "__main__":
    app()
