from __future__ import annotations

import json

from typer.testing import CliRunner

from jobhunt import db
from jobhunt.resume import select as vs

_NOW = "2026-07-01T00:00:00+00:00"
FIN = "Finance x AI 2026"
FOUNDER = "Software Engineer 2026"


def _conn(tmp_path):
    return db.connect(tmp_path / "vs.db")


def _variant(conn, vid, label, reviewed=True):
    conn.execute("DELETE FROM resume_variants WHERE id=? OR label=?", (vid, label))
    conn.execute(
        "INSERT INTO resume_variants (id,label,path,reviewed_at,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (vid, label, "x.pdf", _NOW if reviewed else None, _NOW, _NOW),
    )


def _posting(conn, pid, title, stage="ready", components=None, application=None, submitted_at=None):
    conn.execute(
        """INSERT INTO postings (id, source, source_posting_id, company, title, location, url,
               raw_json, first_seen_at, last_seen_at, status)
           VALUES (?, 'greenhouse', ?, 'Acme', ?, '', ?, '{}', ?, ?, 'active')""",
        (pid, str(pid), title, f"https://x.example.com/{pid}", _NOW, _NOW),
    )
    conn.execute("INSERT INTO funnel (posting_id, stage) VALUES (?, ?)", (pid, stage))
    conn.execute(
        "INSERT INTO scores (posting_id, rubric_version, total, components, excluded) VALUES (?,?,?,?,0)",
        (pid, 1, 50, json.dumps(components or {})),
    )
    if application is not False and (application is not None or submitted_at):
        conn.execute(
            "INSERT INTO applications (posting_id, resume_variant_id, submitted_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (pid, application, submitted_at, _NOW, _NOW),
        )
    conn.commit()


def _snapshot(conn):
    return conn.execute(
        "SELECT posting_id, resume_variant_id, submitted_at FROM applications ORDER BY posting_id"
    ).fetchall()


def test_keyword_routing():
    assert vs.select_variant("Fintech Platform Engineer", {}).yaml_key == "finance_ai"
    sel = vs.select_variant("Founding Engineer", {})
    assert (sel.yaml_key, sel.db_label, sel.matched) == ("founder_engineer", FOUNDER, True)
    sel = vs.select_variant("Pastry Chef", {})
    assert (sel.yaml_key, sel.matched) == ("founder_engineer", False)


def test_finance_hybrid_bonus_does_not_select_finance():
    sel = vs.select_variant("Pastry Chef", {"finance_hybrid_bonus": 3})
    assert (sel.yaml_key, sel.matched) == ("founder_engineer", False)
    assert "ignored" in sel.reason
    assert "ignored" not in vs.select_variant("Pastry Chef", {"finance_hybrid_bonus": 0}).reason


def test_plan_actions_all_five(tmp_path):
    conn = _conn(tmp_path)
    _variant(conn, 1, FOUNDER, reviewed=True)
    _variant(conn, 2, FIN, reviewed=False)
    # set: founder title, no application
    _posting(conn, 1, "Founding Engineer")
    # keep: already founder variant
    _posting(conn, 2, "Software Engineer", application=1)
    # blocked_unreviewed: finance variant exists but unreviewed
    _posting(conn, 3, "Quant Trader")
    # skip_submitted
    _posting(conn, 4, "Founding Engineer", application=None, submitted_at=_NOW)
    plan = {r.posting_id: r.action for r in vs.plan_variants(conn)}
    assert plan == {1: "set", 2: "keep", 3: "blocked_unreviewed", 4: "skip_submitted"}
    # blocked_missing_variant: drop the finance variant row
    conn.execute("DELETE FROM resume_variants WHERE id=2")
    conn.commit()
    plan = {r.posting_id: r.action for r in vs.plan_variants(conn)}
    assert plan[3] == "blocked_missing_variant"


def test_plan_filters_stage_and_existing_null_variant_is_set(tmp_path):
    conn = _conn(tmp_path)
    _variant(conn, 1, FOUNDER)
    _posting(conn, 1, "Software Engineer", application=None, submitted_at=None)
    conn.execute(
        "INSERT INTO applications (posting_id, created_at, updated_at) VALUES (1,?,?)", (_NOW, _NOW)
    )
    _posting(conn, 2, "Software Engineer", stage="applied")
    conn.commit()
    plan = vs.plan_variants(conn)
    assert [(r.posting_id, r.action) for r in plan] == [(1, "set")]


def test_dry_run_writes_nothing(tmp_path):
    conn = _conn(tmp_path)
    _variant(conn, 1, FOUNDER)
    _posting(conn, 1, "Founding Engineer")
    before = _snapshot(conn)
    plan = vs.plan_variants(conn)
    text = vs.format_plan(plan)
    assert "Founding Engineer" in text and "set" in text
    assert _snapshot(conn) == before == []


def test_apply_sets_only_set_rows_never_submitted(tmp_path):
    conn = _conn(tmp_path)
    _variant(conn, 1, FOUNDER)
    _variant(conn, 2, FIN, reviewed=False)
    _posting(conn, 1, "Founding Engineer")  # set (new application)
    _posting(conn, 2, "Quant Trader")  # blocked_unreviewed
    _posting(conn, 3, "Founding Engineer", application=2, submitted_at=_NOW)  # skip_submitted
    conn.execute("UPDATE applications SET resume_variant_id=NULL WHERE posting_id=3 AND 0")
    conn.commit()
    plan = vs.plan_variants(conn)
    assert vs.apply_plan(conn, plan) == 1
    rows = {r["posting_id"]: (r["resume_variant_id"], r["submitted_at"]) for r in _snapshot(conn)}
    assert rows[1] == (1, None)
    assert 2 not in rows
    assert rows[3] == (2, _NOW)


def test_cli_dry_run_and_apply(tmp_path, monkeypatch):
    from jobhunt import cli

    path = tmp_path / "cli.db"
    conn = db.connect(path)
    _variant(conn, 1, FOUNDER)
    _posting(conn, 1, "Founding Engineer")
    conn.close()
    real_connect = db.connect
    monkeypatch.setattr(cli.db_module, "connect", lambda *a, **k: real_connect(path))
    runner = CliRunner()
    out = runner.invoke(cli.app, ["variant-plan"])
    assert out.exit_code == 0, out.output
    assert "Dry run" in out.output
    assert real_connect(path).execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    out = runner.invoke(cli.app, ["variant-plan", "--apply"])
    assert out.exit_code == 0, out.output
    assert "set resume variant on 1" in out.output
    assert real_connect(path).execute("SELECT resume_variant_id FROM applications").fetchone()[0] == 1
