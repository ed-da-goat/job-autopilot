from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jobhunt import db, funnel
from jobhunt import simplify_import as si
from jobhunt.cli import app

_NOW = "2026-07-01T00:00:00+00:00"
HEADERS = "Company name,Position Title,Location,Job Posting Link,Status,Notes"


def _conn(tmp_path):
    return db.connect(tmp_path / "si.db")


def _posting(conn, pid, *, company="Acme", title="Engineer", url="https://boards.example.com/acme/jobs/1", status="active"):
    conn.execute(
        """INSERT INTO postings (id, source, source_posting_id, company, title, location, url,
               raw_json, first_seen_at, last_seen_at, status)
           VALUES (?, 'greenhouse', ?, ?, ?, '', ?, '{}', ?, ?, ?)""",
        (pid, str(pid), company, title, url, _NOW, _NOW, status),
    )
    conn.commit()


def _csv(tmp_path, body, headers=HEADERS, name="export.csv"):
    p = tmp_path / name
    p.write_text(headers + "\n" + body + "\n", encoding="utf-8")
    return p


def _plan(conn, path):
    return si.plan_import(conn, si.read_rows(path))


def _counts(conn):
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("postings", "funnel", "funnel_events", "applications")
    )


def test_header_tolerance(tmp_path):
    headers = "company,JOB TITLE,url,Application_Status,Date-Applied"
    p = _csv(tmp_path, "Acme,Engineer,https://x.com/a,Applied,2026-09-01", headers=headers)
    rows = si.read_rows(p)
    assert len(rows) == 1
    r = rows[0]
    assert (r.company, r.title, r.url, r.status) == ("Acme", "Engineer", "https://x.com/a", "Applied")
    assert r.applied_date.startswith("2026-09-01")


def test_fail_closed_on_missing_headers(tmp_path):
    p = _csv(tmp_path, "Acme,Engineer,Applied", headers="Company,Role Name,Status")
    with pytest.raises(si.HeaderError) as exc:
        si.read_rows(p)
    assert "title" in str(exc.value)
    assert "Role Name" in str(exc.value)  # lists the actual headers


def test_missing_file_cli_exits_nonzero(tmp_path):
    result = CliRunner().invoke(app, ["simplify-import", str(tmp_path / "nope.csv")])
    assert result.exit_code != 0
    assert "data/simplify/export.csv" in result.output


def test_normalize_url():
    assert si.normalize_url("HTTPS://Boards.Example.com/acme/jobs/1/?utm_source=x#frag") == "boards.example.com/acme/jobs/1"
    assert si.normalize_url("http://boards.example.com/acme/jobs/1") == "boards.example.com/acme/jobs/1"
    assert si.normalize_url("") == ""


def test_url_match(tmp_path):
    conn = _conn(tmp_path)
    _posting(conn, 1, company="Other", title="Other")
    p = _csv(tmp_path, "Acme Inc,Eng,SF,https://Boards.Example.com/acme/jobs/1/?utm_source=s,Applied,")
    (item,) = _plan(conn, p)
    assert (item.posting_id, item.method, item.action) == (1, "url", "move")


def test_company_title_match(tmp_path):
    conn = _conn(tmp_path)
    _posting(conn, 1)
    p = _csv(tmp_path, "ACME,engineer,SF,,Applied,")
    (item,) = _plan(conn, p)
    assert (item.posting_id, item.method) == (1, "company+title")


def test_company_title_ignores_inactive_postings(tmp_path):
    conn = _conn(tmp_path)
    _posting(conn, 1, status="duplicate", url="https://other.example.com/1")
    (item,) = _plan(conn, _csv(tmp_path, "Acme,Engineer,,,Applied,"))
    assert item.method == "unmatched"


def test_saved_maps_to_ready_not_ignored(tmp_path):
    conn = _conn(tmp_path)
    _posting(conn, 1)
    p = _csv(tmp_path, "Acme,Engineer,,,Saved,")
    (item,) = si.plan_import(conn, si.read_rows(p), saved_to_ready=True)
    assert (item.proposed, item.action) == ("ready", "move")
    si.apply_plan(conn, [item])
    assert conn.execute("SELECT stage FROM funnel WHERE posting_id=1").fetchone()[0] == "ready"


def test_status_mapping_and_unmapped(tmp_path):
    assert si.map_status("Interviewing") == "interviewing"
    assert si.map_status("interview") == "interviewing"
    assert si.map_status("Offer") == "offer"
    assert si.map_status("Rejected") == "closed"
    assert si.map_status("Ghosted") is None
    conn = _conn(tmp_path)
    _posting(conn, 1)
    (item,) = _plan(conn, _csv(tmp_path, "Acme,Engineer,,,Ghosted,"))
    assert item.action == "skip_status"


def test_monotonic_no_regress(tmp_path):
    conn = _conn(tmp_path)
    _posting(conn, 1)
    funnel.move(conn, 1, "interviewing")
    (item,) = _plan(conn, _csv(tmp_path, "Acme,Engineer,,,Applied,"))
    # no regress; only the missing application row is backfilled
    assert item.action == "record_application"
    si.apply_plan(conn, [item])
    assert conn.execute("SELECT stage FROM funnel WHERE posting_id=1").fetchone()[0] == "interviewing"
    # Saved never pulls a posting back either
    (item2,) = si.plan_import(
        conn, si.read_rows(_csv(tmp_path, "Acme,Engineer,,,Saved,", name="b.csv")), saved_to_ready=True
    )
    assert item2.action == "skip_further"


def test_rejected_closes_from_applied(tmp_path):
    conn = _conn(tmp_path)
    _posting(conn, 1)
    funnel.move(conn, 1, "applied")
    plan = _plan(conn, _csv(tmp_path, "Acme,Engineer,,,Rejected,"))
    assert plan[0].action == "move"
    si.apply_plan(conn, plan)
    assert conn.execute("SELECT stage FROM funnel WHERE posting_id=1").fetchone()[0] == "closed"


def test_applied_inserts_application_with_date(tmp_path):
    conn = _conn(tmp_path)
    _posting(conn, 1)
    p = _csv(tmp_path, "Acme,Engineer,SF,,Applied,,2026-09-03", headers=HEADERS + ",Applied Date")
    plan = _plan(conn, p)
    si.apply_plan(conn, plan)
    app_row = conn.execute("SELECT * FROM applications WHERE posting_id=1").fetchone()
    assert app_row["submitted_at"].startswith("2026-09-03")
    assert "date from import" not in (app_row["notes"] or "")
    assert conn.execute("SELECT stage FROM funnel WHERE posting_id=1").fetchone()[0] == "applied"


def test_applied_without_date_notes_import(tmp_path):
    conn = _conn(tmp_path)
    _posting(conn, 1)
    si.apply_plan(conn, _plan(conn, _csv(tmp_path, "Acme,Engineer,,,Applied,")))
    app_row = conn.execute("SELECT * FROM applications WHERE posting_id=1").fetchone()
    assert app_row["submitted_at"]
    assert "date from import" in app_row["notes"]


def test_idempotent_second_run_proposes_nothing(tmp_path):
    conn = _conn(tmp_path)
    _posting(conn, 1)
    _posting(conn, 2, company="Beta", title="Analyst", url="https://b.example.com/2")
    p = _csv(
        tmp_path,
        "Acme,Engineer,,,Applied,\nBeta,Analyst,,,Saved,\nNewco,Quant,,https://n.example.com/q,Interviewing,",
    )
    first = si.plan_import(conn, si.read_rows(p), saved_to_ready=True)
    assert si.summarize(first)["actionable"] == 3
    si.apply_plan(conn, first)
    second = si.plan_import(conn, si.read_rows(p), saved_to_ready=True)
    assert si.summarize(second)["actionable"] == 0
    before = _counts(conn)
    si.apply_plan(conn, second)
    assert _counts(conn) == before


def test_unmatched_stub_only_on_apply(tmp_path):
    conn = _conn(tmp_path)
    p = _csv(tmp_path, "Newco,Quant,NYC,https://n.example.com/q?utm=1,Applied,")
    plan = _plan(conn, p)
    assert plan[0].action == "create_stub_and_move"
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 0
    si.apply_plan(conn, plan)
    stub = conn.execute("SELECT * FROM postings").fetchone()
    assert stub["source"] == "simplify_import"
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1


def test_dry_run_writes_nothing(tmp_path):
    conn = _conn(tmp_path)
    _posting(conn, 1)
    p = _csv(tmp_path, "Acme,Engineer,,,Applied,\nNewco,Quant,,,Saved,")
    before = _counts(conn)
    plan = _plan(conn, p)
    text = si.format_plan(plan)
    assert _counts(conn) == before
    assert "Acme · Engineer · company+title · (none)->applied · move" in text
    assert "dry-run" in text and "Nothing written" in text


def test_saved_is_noop_by_default(tmp_path):
    conn = _conn(tmp_path)
    _posting(conn, 1)
    p = _csv(tmp_path, "Acme,Engineer,,,SAVED,\nNewco,Quant,,https://n.example.com/q,Saved,")
    plan = _plan(conn, p)
    assert [i.action for i in plan] == ["saved_noop", "saved_noop"]
    c = si.summarize(plan)
    assert c["actionable"] == 0 and c["saved_noop"] == 2
    assert "saved: no-op" in si.format_plan(plan)
    before = _counts(conn)
    si.apply_plan(conn, plan)
    assert _counts(conn) == before  # no funnel move, no stub
    # opt-in still stubs unmatched Saved rows
    opted = si.plan_import(conn, si.read_rows(p), saved_to_ready=True)
    assert [i.action for i in opted] == ["move", "create_stub_and_move"]


def test_real_export_headers_resolve(tmp_path):
    headers = "Job Title,Company Name,Location,Job URL,Applied Date,Status,Status Date,Archived,Date Archived,Notes,job_type"
    p = _csv(
        tmp_path,
        "Engineer,Acme,SF,https://boards.example.com/acme/jobs/1,2026-08-05,APPLIED,2026-08-05,false,,,Full-time\n"
        "Analyst,Beta,NYC,https://b.example.com/2,,SAVED,,true,2026-08-06,,Full-time",
        headers=headers,
    )
    conn = _conn(tmp_path)
    _posting(conn, 1)
    rows = si.read_rows(p)
    assert [r.status for r in rows] == ["APPLIED", "SAVED"]
    plan = si.plan_import(conn, rows)
    assert (plan[0].proposed, plan[0].action) == ("applied", "move")
    assert plan[0].row.applied_date.startswith("2026-08-05")
    assert plan[1].action == "saved_noop"
