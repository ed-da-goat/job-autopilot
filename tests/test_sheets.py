from __future__ import annotations

import builtins
import sys

import pytest

from jobhunt import db as db_module
from jobhunt import funnel as funnel_module
from jobhunt import sheets as sheets_module


def _make_posting(
    conn,
    spid: str,
    company: str = "Acme",
    title: str = "Backend Engineer",
    status: str = "active",
    location: str = "Atlanta, GA",
    url: str = "https://example.com/job",
    posted_at: str = "2026-07-01",
    first_seen_at: str = "2026-07-02",
) -> int:
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, location, url, posted_at,
            status, first_seen_at, last_seen_at)
           VALUES ('greenhouse', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (spid, company, title, location, url, posted_at, status, first_seen_at, first_seen_at),
    )
    conn.commit()
    return conn.execute("SELECT id FROM postings WHERE source_posting_id=?", (spid,)).fetchone()["id"]


def _make_score(conn, posting_id: int, rubric_version: int, total: int, excluded: int = 0) -> None:
    conn.execute(
        """INSERT INTO scores
           (posting_id, rubric_version, total, components, missing_evidence, excluded, exclude_reason, scored_at)
           VALUES (?, ?, ?, '{}', '[]', ?, NULL, '')""",
        (posting_id, rubric_version, total, excluded),
    )
    conn.commit()


def _make_company(conn, name: str, platform: str = "greenhouse", slug: str = "acme", watchlist: int = 0) -> None:
    conn.execute(
        "INSERT INTO companies (name, platform, slug, verified, watchlist, added_at) VALUES (?, ?, ?, 'yes', ?, '')",
        (name, platform, slug, watchlist),
    )
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    connection = db_module.connect(tmp_path / "sheets.db")
    try:
        yield connection
    finally:
        connection.close()


# --- payload builders -------------------------------------------------


def test_build_pipeline_tab_orders_by_stage_then_score(conn):
    _make_company(conn, "Acme")
    p_offer = _make_posting(conn, "p1", company="Acme", title="Role A")
    _make_score(conn, p_offer, 1, 70)
    for stage in ["qualified", "applied", "offer"]:
        funnel_module.move(conn, p_offer, stage)

    p_qualified_high = _make_posting(conn, "p2", company="Acme", title="Role B")
    _make_score(conn, p_qualified_high, 1, 90)
    funnel_module.move(conn, p_qualified_high, "qualified")

    p_qualified_low = _make_posting(conn, "p3", company="Acme", title="Role C")
    _make_score(conn, p_qualified_low, 1, 40)
    funnel_module.move(conn, p_qualified_low, "qualified")

    # harvested-only posting must not appear (not past qualification)
    p_harvested = _make_posting(conn, "p4", company="Acme", title="Role D")
    _make_score(conn, p_harvested, 1, 99)
    funnel_module.ensure_funnel_row(conn, p_harvested)

    header, rows = sheets_module.build_pipeline_tab(conn)
    assert header[:3] == ["stage", "company", "title"]
    titles = [r[2] for r in rows]
    assert titles == ["Role A", "Role B", "Role C"]
    assert rows[0][0] == "offer"
    assert rows[0][6] != ""  # qualified_at populated
    assert rows[0][7] != ""  # last_event_at populated


def test_build_shortlist_tab_excludes_qualified_and_respects_threshold(conn):
    active_unqualified_high = _make_posting(conn, "s1", title="Should show")
    _make_score(conn, active_unqualified_high, 1, 80)

    active_unqualified_low = _make_posting(conn, "s2", title="Below threshold")
    _make_score(conn, active_unqualified_low, 1, 10)

    already_qualified = _make_posting(conn, "s3", title="Already qualified")
    _make_score(conn, already_qualified, 1, 95)
    funnel_module.move(conn, already_qualified, "qualified")

    header, rows = sheets_module.build_shortlist_tab(conn, shortlist_min_score=55, shortlist_limit=200)
    assert header[0] == "score"
    titles = [r[2] for r in rows]
    assert titles == ["Should show"]


def test_build_shortlist_tab_respects_limit(conn):
    for i in range(5):
        pid = _make_posting(conn, f"lim{i}", title=f"Role {i}")
        _make_score(conn, pid, 1, 60 + i)

    _header, rows = sheets_module.build_shortlist_tab(conn, shortlist_min_score=0, shortlist_limit=2)
    assert len(rows) == 2
    assert rows[0][0] >= rows[1][0]


def test_build_applications_tab(conn):
    posting_id = _make_posting(conn, "a1", company="Acme", title="Role A")
    funnel_module.move(conn, posting_id, "qualified")
    funnel_module.move(conn, posting_id, "applied")
    conn.execute(
        """INSERT INTO applications (posting_id, submitted_at, channel, created_at, updated_at)
           VALUES (?, '2026-07-15', 'careers page', '', '')""",
        (posting_id,),
    )
    conn.commit()

    header, rows = sheets_module.build_applications_tab(conn)
    assert header == ["company", "title", "stage", "submitted_at", "url", "follow_up_action", "follow_up_due_at"]
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "Acme"
    assert row[2] == "applied"
    assert row[3] == "2026-07-15"
    assert row[5] == "follow up"  # auto-set by funnel.move on first 'applied'
    assert row[6] != ""


def test_build_companies_tab(conn):
    _make_company(conn, "Acme", watchlist=1)
    p1 = _make_posting(conn, "c1", company="Acme")
    _make_score(conn, p1, 1, 50)
    p2 = _make_posting(conn, "c2", company="Acme")
    _make_score(conn, p2, 1, 90)

    header, rows = sheets_module.build_companies_tab(conn)
    assert header[0] == "company"
    acme_row = next(r for r in rows if r[0] == "Acme")
    assert acme_row[4] == "yes"  # watchlist
    assert acme_row[5] == 2  # active_posting_count
    assert acme_row[6] == 90  # best_score


def test_build_finai_tab_one_row_per_posting_latest_report_sorted(conn):
    import json

    nyc = _make_posting(conn, "f1", title="Financial Analyst", location="New York, NY")
    atl = _make_posting(conn, "f2", title="Risk Analyst", location="Atlanta, GA")
    dims = {k: 50 for k in ("keyword_match", "skills_overlap", "experience_proof", "education_fit", "role_alignment")}

    def report(pid, created, before, after=None):
        rep = {"match_before": before, "dimensions": dims, "missing_keywords": ["dcf", "excel"],
               "red_flags": ["gap"]}
        if after is not None:
            rep["match_after"] = after
        conn.execute(
            "INSERT INTO artifacts (posting_id, kind, content, created_at, updated_at)"
            " VALUES (?, 'match_report', ?, ?, ?)", (pid, json.dumps(rep), created, created))

    report(nyc, "2026-10-01", 40)
    report(nyc, "2026-10-02", 55, 80)  # latest wins
    report(atl, "2026-10-01", 90)
    conn.commit()
    header, rows = sheets_module.build_finai_tab(conn)
    assert header == [
        "posting_id", "company", "title", "location", "city_tier", "match_before",
        "match_after", "keyword_match", "skills_overlap", "experience_proof",
        "education_fit", "role_alignment", "missing_keywords", "red_flags",
        "tailored_resume", "funnel_stage", "scored_at", "url",
    ]
    assert [r[0] for r in rows] == [nyc, atl]  # tier 1 before tier 2 despite lower match
    assert rows[0][4:7] == [1, 55, 80] and rows[0][12] == "dcf; excel"
    assert rows[1][6] == ""


def test_all_payloads_include_finai_tab(conn):
    config = sheets_module.SheetsConfig(spreadsheet_id="x", service_account_json="y")
    assert "FinAI Matches" in sheets_module._build_all_payloads(conn, config)


def test_build_health_tab(conn):
    conn.execute(
        "INSERT INTO source_state (source, baseline_at, last_success_at, consecutive_failures) "
        "VALUES ('greenhouse', '2026-07-01', '2026-07-30', 0)"
    )
    conn.execute(
        """INSERT INTO sweeps (started_at, finished_at, source, boards_attempted, boards_ok,
                                postings_seen, postings_new, errors)
           VALUES ('2026-07-30', '2026-07-30T01:00', 'greenhouse', 5, 5, 100, 3, '')"""
    )
    conn.execute(
        "INSERT INTO run_log (job, started_at, finished_at, ok, summary) VALUES ('daily', '', '2026-07-30', 1, 'ok')"
    )
    conn.commit()

    header, rows = sheets_module.build_health_tab(conn)
    assert header[0] == "source"
    source_row = next(r for r in rows if r[0] == "greenhouse")
    assert source_row[5] == 100
    assert source_row[6] == 3
    job_row = next(r for r in rows if r[0] == "job:daily")
    assert job_row[1] == "2026-07-30"


# --- config -------------------------------------------------------------


def test_load_config_missing_file_error_points_to_template(tmp_path):
    missing = tmp_path / "sheets.local.toml"
    with pytest.raises(sheets_module.SheetsConfigError) as exc_info:
        sheets_module.load_config(missing)
    message = str(exc_info.value)
    assert "sheets.local.template.toml" in message
    assert str(missing) in message


def test_load_config_applies_defaults(tmp_path):
    path = tmp_path / "sheets.local.toml"
    path.write_text('spreadsheet_id = "abc123"\nservice_account_json = "creds.json"\n')
    config = sheets_module.load_config(path)
    assert config.spreadsheet_id == "abc123"
    assert config.service_account_json == "creds.json"
    assert config.shortlist_min_score == sheets_module.DEFAULT_SHORTLIST_MIN_SCORE
    assert config.shortlist_limit == sheets_module.DEFAULT_SHORTLIST_LIMIT


def test_load_config_overrides(tmp_path):
    path = tmp_path / "sheets.local.toml"
    path.write_text(
        'spreadsheet_id = "abc123"\nservice_account_json = "creds.json"\n'
        "shortlist_min_score = 70\nshortlist_limit = 25\n"
    )
    config = sheets_module.load_config(path)
    assert config.shortlist_min_score == 70
    assert config.shortlist_limit == 25


def test_load_config_incomplete_file_errors(tmp_path):
    path = tmp_path / "sheets.local.toml"
    path.write_text('spreadsheet_id = "abc123"\n')  # missing service_account_json
    with pytest.raises(sheets_module.SheetsConfigError):
        sheets_module.load_config(path)


# --- push() with a fake gspread client -----------------------------------


class _FakeWorksheet:
    def __init__(self, title: str):
        self.title = title
        self.cleared = False
        self.resized = None
        self.updated_values = None

    def clear(self):
        self.cleared = True

    def resize(self, rows=None, cols=None):
        self.resized = (rows, cols)

    def update(self, values):
        self.updated_values = values


class _FakeWorksheetNotFound(Exception):
    pass


class _FakeSpreadsheet:
    def __init__(self):
        self.url = "https://docs.google.com/spreadsheets/d/fake/edit"
        self._worksheets: dict[str, _FakeWorksheet] = {}

    def worksheet(self, title):
        if title not in self._worksheets:
            raise _FakeWorksheetNotFound(title)
        return self._worksheets[title]

    def add_worksheet(self, title, rows, cols):
        ws = _FakeWorksheet(title)
        self._worksheets[title] = ws
        return ws


class _FakeClient:
    def __init__(self, spreadsheet):
        self._spreadsheet = spreadsheet

    def open_by_key(self, key):
        self._spreadsheet.opened_key = key
        return self._spreadsheet


class _FakeGspreadModule:
    WorksheetNotFound = _FakeWorksheetNotFound

    def __init__(self, client):
        self._client = client

    def service_account(self, filename):
        self.filename_used = filename
        return self._client


def test_push_creates_clears_and_updates_each_tab(monkeypatch):
    fake_spreadsheet = _FakeSpreadsheet()
    fake_client = _FakeClient(fake_spreadsheet)
    fake_gspread = _FakeGspreadModule(fake_client)
    monkeypatch.setitem(sys.modules, "gspread", fake_gspread)

    config = sheets_module.SheetsConfig(spreadsheet_id="sheet123", service_account_json="creds.json")
    payloads = {
        "Pipeline": (["stage", "company"], [["qualified", "Acme"]]),
        "Shortlist": (["score", "company"], []),
    }

    url = sheets_module.push(payloads, config)

    assert url == fake_spreadsheet.url
    assert fake_spreadsheet.opened_key == "sheet123"
    pipeline_ws = fake_spreadsheet._worksheets["Pipeline"]
    assert pipeline_ws.cleared is True
    assert pipeline_ws.updated_values == [["stage", "company"], ["qualified", "Acme"]]
    assert pipeline_ws.resized == (2, 2)

    shortlist_ws = fake_spreadsheet._worksheets["Shortlist"]
    assert shortlist_ws.updated_values == [["score", "company"]]


def test_push_reuses_existing_worksheet_and_clears_it(monkeypatch):
    fake_spreadsheet = _FakeSpreadsheet()
    existing = _FakeWorksheet("Pipeline")
    fake_spreadsheet._worksheets["Pipeline"] = existing
    fake_client = _FakeClient(fake_spreadsheet)
    fake_gspread = _FakeGspreadModule(fake_client)
    monkeypatch.setitem(sys.modules, "gspread", fake_gspread)

    config = sheets_module.SheetsConfig(spreadsheet_id="sheet123", service_account_json="creds.json")
    sheets_module.push({"Pipeline": (["a"], [["b"]])}, config)

    assert existing.cleared is True
    assert existing.updated_values == [["a"], ["b"]]


# --- run_sync -------------------------------------------------------------


def test_run_sync_dry_run_never_imports_gspread(conn, monkeypatch, capsys):
    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "gspread":
            raise AssertionError("dry-run must not import gspread")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    sheets_module.run_sync(conn, dry_run=True)

    captured = capsys.readouterr()
    assert "Pipeline: 0 rows" in captured.out
    assert "Shortlist: 0 rows" in captured.out


def test_run_sync_dry_run_falls_back_when_config_missing(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(sheets_module, "LOCAL_CONFIG_PATH", tmp_path / "does-not-exist.toml")
    # should not raise even though no config file exists
    sheets_module.run_sync(conn, dry_run=True)


def test_run_sync_live_requires_config(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(sheets_module, "LOCAL_CONFIG_PATH", tmp_path / "does-not-exist.toml")
    with pytest.raises(sheets_module.SheetsConfigError):
        sheets_module.run_sync(conn, dry_run=False)


def test_run_sync_live_pushes_and_prints_url(conn, monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "sheets.local.toml"
    config_path.write_text('spreadsheet_id = "abc123"\nservice_account_json = "creds.json"\n')
    monkeypatch.setattr(sheets_module, "LOCAL_CONFIG_PATH", config_path)

    fake_spreadsheet = _FakeSpreadsheet()
    fake_client = _FakeClient(fake_spreadsheet)
    fake_gspread = _FakeGspreadModule(fake_client)
    monkeypatch.setitem(sys.modules, "gspread", fake_gspread)

    sheets_module.run_sync(conn, dry_run=False)

    captured = capsys.readouterr()
    assert fake_spreadsheet.url in captured.out
    assert "Pipeline: 0 rows" in captured.out
