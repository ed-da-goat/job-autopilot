"""`jobhunt init` and `jobhunt doctor`, run against a tmp copy of the repo's templates."""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunt import setup_wizard as sw
from jobhunt.cli import app

REAL = Path(__file__).resolve().parents[1]
SOURCES = [src for src, _dst in (*sw.TEMPLATE_COPIES, *sw.EXAMPLE_COPIES)]
runner = CliRunner()

ARGS = [
    "init", "--non-interactive",
    "--name", "Jordan Casey", "--email", "jordan.casey@example.com", "--phone", "555-0142",
    "--location", "Springfield, ST", "--roles", "data engineer, analytics engineer",
    "--us-authorized", "--no-sponsorship", "--earliest-start", "February 2027", "--no-us-remote",
]


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    tree = tmp_path / "repo"
    for rel in SOURCES:
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REAL / rel, tree / rel)
    monkeypatch.setattr(sw, "REPO_ROOT", tree)
    return tree


def run(*args):
    return runner.invoke(app, list(args))


def test_init_creates_every_local_file(root):
    result = run(*ARGS)
    assert result.exit_code == 0, result.output
    for _src, dst in (*sw.TEMPLATE_COPIES, *sw.EXAMPLE_COPIES):
        assert (root / dst).is_file(), dst
    assert (root / "data/resume.local.docx").read_bytes() == (REAL / "examples/resume.example.docx").read_bytes()
    assert "Replace data/resume.local.docx" in result.output


def test_answers_land_in_both_files(root):
    assert run(*ARGS).exit_code == 0
    prefs_text = (root / sw.PREFS_LOCAL).read_text(encoding="utf-8")
    prefs = tomllib.loads(prefs_text)
    assert prefs["candidate_name"] == "Jordan Casey"
    assert prefs["contact_email"] == "jordan.casey@example.com"
    assert prefs["target_roles"] == ["data engineer", "analytics engineer"]
    assert prefs["target_start"] == "February 2027"
    assert prefs["allow_us_remote"] is False
    assert prefs["sponsorship_status"] == "not_required"
    assert prefs["home_state"] == "st" and prefs["home_metro"] == ["springfield"]
    answers = (root / sw.ANSWERS_LOCAL).read_text(encoding="utf-8")
    for line in (
        "full_name: Jordan Casey",
        "email: jordan.casey@example.com",
        "phone: 555-0142",
        "location: Springfield, ST",
        "earliest_start: February 2027",
        "work_authorization: Authorized to work in the US; no sponsorship required",
    ):
        assert line in answers.splitlines()
    assert "Alex Rivera" not in answers.split("full_name:")[1].splitlines()[0]


def test_toml_edits_keep_comments_and_tables(root):
    template = (root / "data/search-preferences.template.toml").read_text(encoding="utf-8")
    assert run(*ARGS).exit_code == 0
    edited = (root / sw.PREFS_LOCAL).read_text(encoding="utf-8")
    comments = [line for line in template.splitlines() if line.lstrip().startswith("#")]
    assert comments and all(line in edited.splitlines() for line in comments)
    assert tomllib.loads(edited)["standing_answers"] == tomllib.loads(template)["standing_answers"]
    assert tomllib.loads(edited)["city_tiers"] == tomllib.loads(template)["city_tiers"]


def test_a_trailing_comment_survives_a_value_edit():
    text = 'version = 1\nallow_us_remote = true  # keep remote\n[lane]\npreset = "x"\n'
    out = sw.set_toml_key(text, "allow_us_remote", "false")
    assert out == 'version = 1\nallow_us_remote = false  # keep remote\n[lane]\npreset = "x"\n'


def test_a_missing_key_is_added_before_the_first_table():
    text = 'version = 1\n\n[lane]\npreset = "x"\n'
    out = sw.set_toml_key(text, "candidate_name", '"Pat"')
    assert out == 'version = 1\ncandidate_name = "Pat"\n\n[lane]\npreset = "x"\n'
    assert tomllib.loads(out)["candidate_name"] == "Pat"


def test_quotes_in_an_answer_stay_valid_toml(root):
    result = run("init", "--non-interactive", "--name", 'Pat "PJ" O\'Neil \\ Doe')
    assert result.exit_code == 0, result.output
    assert tomllib.loads((root / sw.PREFS_LOCAL).read_text(encoding="utf-8"))["candidate_name"] == 'Pat "PJ" O\'Neil \\ Doe'


def test_nothing_is_overwritten_without_force(root):
    assert run(*ARGS).exit_code == 0
    for _src, dst in (*sw.TEMPLATE_COPIES, *sw.EXAMPLE_COPIES):
        (root / dst).write_bytes(b"MINE " + dst.encode())
    result = run("init", "--non-interactive", "--name", "Someone Else", "--phone", "555-0199")
    assert result.exit_code == 0, result.output
    for _src, dst in (*sw.TEMPLATE_COPIES, *sw.EXAMPLE_COPIES):
        assert (root / dst).read_bytes() == b"MINE " + dst.encode(), dst
    assert "kept" in result.output and "answers not written" in result.output


def test_force_overwrites_and_rewrites_the_answers(root):
    assert run(*ARGS).exit_code == 0
    (root / sw.ANSWERS_LOCAL).write_text("mine\n", encoding="utf-8")
    result = run("init", "--non-interactive", "--force", "--name", "Sam Lee")
    assert result.exit_code == 0, result.output
    answers = (root / sw.ANSWERS_LOCAL).read_text(encoding="utf-8")
    assert "mine" not in answers and "full_name: Sam Lee" in answers
    assert tomllib.loads((root / sw.PREFS_LOCAL).read_text(encoding="utf-8"))["candidate_name"] == "Sam Lee"


def test_unanswered_questions_keep_the_template_values(root):
    assert run("init", "--non-interactive").exit_code == 0
    template = (root / "data/answers.local.template.md").read_text(encoding="utf-8")
    assert (root / sw.ANSWERS_LOCAL).read_text(encoding="utf-8") == template


def test_interactive_prompts_fill_the_answers(root):
    typed = "\n".join([
        "Taylor Kim", "taylor.kim@example.com", "555-0177", "Springfield, ST", "backend engineer, sre",
        "y", "n", "March 2027", "y", "",
    ])
    result = runner.invoke(app, ["init"], input=typed)
    assert result.exit_code == 0, result.output
    prefs = tomllib.loads((root / sw.PREFS_LOCAL).read_text(encoding="utf-8"))
    assert prefs["candidate_name"] == "Taylor Kim"
    assert prefs["target_roles"] == ["backend engineer", "sre"]
    assert prefs["allow_us_remote"] is True and prefs["sponsorship_status"] == "not_required"
    assert "phone: 555-0177" in (root / sw.ANSWERS_LOCAL).read_text(encoding="utf-8").splitlines()


def test_doctor_reports_every_missing_piece_on_an_empty_tree(root):
    result = run("doctor")
    assert result.exit_code == 1
    checks = {c.name: c for c in sw.run_doctor(root)}
    assert checks["local files"].status == "fail" and len(checks["local files"].items) == 6
    assert checks["database"].status == "warn" and "not seeded" in checks["database"].detail
    assert "uv run python -m jobhunt init" in result.output
    assert "uv run python -m jobhunt seed" in result.output


def test_doctor_lists_fill_placeholders_after_init(root):
    run(*ARGS)
    checks = {c.name: c for c in sw.run_doctor(root)}
    assert checks["local files"].status == "ok"
    assert checks["placeholders"].status == "fail"
    joined = "\n".join(checks["placeholders"].items)
    for key in ("github:", "portfolio:", "salary_expectation:", "references:"):
        assert f"data/answers.local.md" in joined and key in joined
    assert "phone:" not in joined  # answered
    assert "work_authorization:" not in joined
    assert checks["example data"].status == "warn"


def test_doctor_passes_the_file_checks_once_filled(root):
    run(*ARGS)
    for rel in sw.PLACEHOLDER_FILES:
        path = root / rel
        path.write_text(path.read_text(encoding="utf-8").replace("[FILL", "[done"), encoding="utf-8")
    checks = {c.name: c for c in sw.run_doctor(root)}
    assert checks["placeholders"].status == "ok"


def test_doctor_sees_a_seeded_database(root):
    import sqlite3

    db = root / sw.DB_RELATIVE
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE companies (name TEXT)")
    assert {c.name: c for c in sw.run_doctor(root)}["database"].status == "warn"
    conn.execute("INSERT INTO companies VALUES ('Acme')")
    conn.commit()
    conn.close()
    check = {c.name: c for c in sw.run_doctor(root)}["database"]
    assert check.status == "ok" and "1 companies" in check.detail


def test_doctor_tool_checks(root, monkeypatch):
    monkeypatch.setattr(sw.shutil, "which", lambda name: None)
    monkeypatch.setattr(sw, "chrome_path", lambda: None)
    checks = {c.name: c for c in sw.run_doctor(root)}
    assert checks["node"].status == "warn" and "Node.js" in checks["node"].detail
    assert checks["chrome"].status == "warn" and "playwright install chromium" in checks["chrome"].detail
    monkeypatch.setattr(sw, "chrome_path", lambda: "/x/chrome")
    monkeypatch.setattr(sw.shutil, "which", lambda name: "/x/node")
    checks = {c.name: c for c in sw.run_doctor(root)}
    assert checks["node"].status == "ok" and checks["chrome"].detail == "/x/chrome"


def test_doctor_flags_missing_python_modules(root, monkeypatch):
    monkeypatch.setattr(sw, "REQUIRED_MODULES", ("definitely_not_a_module_xyz",))
    check = {c.name: c for c in sw.run_doctor(root)}["python deps"]
    assert check.status == "fail" and "uv sync" in check.detail
