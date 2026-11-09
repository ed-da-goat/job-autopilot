"""First-run setup: `jobhunt init` (copy the templates, write your answers) and `jobhunt doctor`.

The local files are edited as text, never through a tomllib round trip, so every comment in
the templates survives. Nothing is overwritten without `force`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent  # tests point this at a tmp tree

# (template, local copy), relative to the repo root. All local copies are gitignored.
TEMPLATE_COPIES = (
    ("PROFILE.example.md", "PROFILE.md"),
    ("data/facts.local.template.md", "data/facts.local.md"),
    ("data/answers.local.template.md", "data/answers.local.md"),
    ("data/search-preferences.template.toml", "data/search-preferences.local.toml"),
)
# Example persona files the pipeline needs to run; replace each with your own.
EXAMPLE_COPIES = (
    ("examples/resume.example.docx", "data/resume.local.docx"),
    ("examples/master.example.rm.json", "data/apply_packs/finai/master.rm.json"),
)
PREFS_LOCAL = "data/search-preferences.local.toml"
ANSWERS_LOCAL = "data/answers.local.md"
PLACEHOLDER_FILES = (ANSWERS_LOCAL, "data/facts.local.md", "PROFILE.md", PREFS_LOCAL)
PERSONA_MARKER = "Alex Rivera"
DB_RELATIVE = "data/jobhunt.db"
# The row db.SCHEMA seeds as the default resume variant. The unattended lane tailors a PDF per
# posting from the resume below, so the variant only has to exist, point at it and be reviewed.
DEFAULT_VARIANT_LABEL = "Software Engineer 2026"
DEFAULT_VARIANT_PATH = "data/resume.local.docx"
REQUIRED_MODULES = ("typer", "fastapi", "uvicorn", "playwright", "yaml", "jinja2", "requests", "gspread")


@dataclass
class Answers:
    """What the wizard asks. None means "not answered": the template value stays."""

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None  # "City, ST"
    roles: list[str] | None = None
    us_authorized: bool | None = None
    needs_sponsorship: bool | None = None
    earliest_start: str | None = None
    allow_us_remote: bool | None = None


@dataclass
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str = ""
    items: list[str] = field(default_factory=list)


# --- format-preserving edits ------------------------------------------------------------


def _one_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _toml_string(value: str) -> str:
    return json.dumps(_one_line(value), ensure_ascii=False)


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _toml_list(values: list[str]) -> str:
    return json.dumps([_one_line(v) for v in values if _one_line(v)], ensure_ascii=False)


def set_toml_key(text: str, key: str, rendered: str) -> str:
    """Set a top-level `key = value` (before the first [table]) and keep the rest verbatim,
    including the line's own trailing comment. A missing key is added at the end of the
    top-level block."""
    header = re.search(r"(?m)^\s*\[", text)
    cut = header.start() if header else len(text)
    head, tail = text[:cut], text[cut:]
    pattern = re.compile(
        r'(?m)^(' + re.escape(key) + r'\s*=\s*)(?:"(?:[^"\\\n]|\\.)*"|\[[^\]\n]*\]|[^\s#\n]+)(.*)$'
    )
    if pattern.search(head):
        head = pattern.sub(lambda m: m.group(1) + rendered + m.group(2), head, count=1)
    else:
        stripped = head.rstrip("\n")
        head = stripped + "\n" + f"{key} = {rendered}\n" + head[len(stripped) + 1 :]
    return head + tail


def set_answer_line(text: str, key: str, value: str) -> str:
    """Set `key: value` in an answers.local.md style file; append when the key is absent."""
    value = _one_line(value)
    pattern = re.compile(r"(?m)^" + re.escape(key) + r"\s*:.*$")
    if pattern.search(text):
        return pattern.sub(lambda m: f"{key}: {value}", text, count=1)
    return text.rstrip("\n") + f"\n{key}: {value}\n"


def _work_authorization(authorized: bool | None) -> str | None:
    """Stored as the bare Yes or No the forms ask for ("Are you legally authorized to work in
    the US?"): the filler types or picks a yes/no word, and a sentence matches no menu option."""
    return None if authorized is None else ("Yes" if authorized else "No")


def _city_state(location: str) -> tuple[str, str] | None:
    city, sep, state = _one_line(location).rpartition(",")
    state = state.strip()
    if sep and city.strip() and re.fullmatch(r"[A-Za-z]{2}", state):
        return city.strip(), state
    return None


def apply_to_prefs(text: str, a: Answers) -> str:
    if a.name:
        text = set_toml_key(text, "candidate_name", _toml_string(a.name))
    if a.email:
        text = set_toml_key(text, "contact_email", _toml_string(a.email))
    if a.roles:
        text = set_toml_key(text, "target_roles", _toml_list(a.roles))
    if a.earliest_start:
        text = set_toml_key(text, "target_start", _toml_string(a.earliest_start))
    if a.allow_us_remote is not None:
        text = set_toml_key(text, "allow_us_remote", _toml_bool(a.allow_us_remote))
    if a.needs_sponsorship is not None:
        status = "required" if a.needs_sponsorship else "not_required"
        text = set_toml_key(text, "sponsorship_status", _toml_string(status))
    if a.location and (parsed := _city_state(a.location)):
        city, state = parsed
        text = set_toml_key(text, "home_state", _toml_string(state.lower()))
        text = set_toml_key(text, "home_metro", _toml_list([city.lower()]))
    tomllib.loads(text)  # refuse to write a file that no longer parses
    return text


def _split_name(name: str) -> tuple[str, str] | tuple[None, None]:
    """"Alex Q. Rivera" -> ("Alex Q.", "Rivera"); a single word is a first name only."""
    parts = _one_line(name).split(" ")
    if len(parts) < 2:
        return parts[0] or None, None
    return " ".join(parts[:-1]), parts[-1]


def apply_to_answers(text: str, a: Answers) -> str:
    first, last = _split_name(a.name) if a.name else (None, None)
    pairs = {
        "full_name": a.name,
        "first_name": first,
        "last_name": last,
        "sponsorship": None if a.needs_sponsorship is None else ("Yes" if a.needs_sponsorship else "No"),
        "email": a.email,
        "phone": a.phone,
        "location": a.location,
        "earliest_start": a.earliest_start,
        "work_authorization": _work_authorization(a.us_authorized),
    }
    for key, value in pairs.items():
        if value:
            text = set_answer_line(text, key, value)
    return text


# --- init ---------------------------------------------------------------------------------


def _copy(root: Path, src_rel: str, dst_rel: str, force: bool) -> tuple[str, str]:
    """-> (status, dst_rel); status is created | overwritten | kept | missing-source."""
    src, dst = root / src_rel, root / dst_rel
    if not src.is_file():
        return "missing-source", src_rel
    existed = dst.exists()
    if existed and not force:
        return "kept", dst_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return ("overwritten" if existed else "created"), dst_rel


def run_init(root: Path, answers: Answers, force: bool = False) -> list[str]:
    """Copy the templates and example files, then write the answers. Returns report lines."""
    lines: list[str] = []
    fresh: set[str] = set()  # local files this run wrote, the only ones answers may edit
    for src, dst in (*TEMPLATE_COPIES, *EXAMPLE_COPIES):
        status, rel = _copy(root, src, dst, force)
        if status in ("created", "overwritten"):
            fresh.add(dst)
        note = {
            "created": "created",
            "overwritten": "overwritten (--force)",
            "kept": "kept (already exists; pass --force to replace it)",
            "missing-source": "SOURCE MISSING, skipped",
        }[status]
        lines.append(f"  {rel}: {note}")

    for rel, editor in ((PREFS_LOCAL, apply_to_prefs), (ANSWERS_LOCAL, apply_to_answers)):
        path = root / rel
        if rel not in fresh:
            if path.exists():
                lines.append(f"  {rel}: answers not written (file kept as it was; edit it by hand or use --force)")
            continue
        path.write_text(editor(path.read_text(encoding="utf-8"), answers), encoding="utf-8")
        lines.append(f"  {rel}: answers written")

    if "data/resume.local.docx" in fresh:
        lines.append("  Replace data/resume.local.docx and data/apply_packs/finai/master.rm.json with your own resume.")
    lines += register_in_db(root, overwrite=force)
    return lines


def register_in_db(root: Path, overwrite: bool = False) -> list[str]:
    """Load data/answers.local.md into application_answers and register the default resume
    variant, so the first `autoapply run` is not skipped as not_ready. Safe to repeat: stored
    answers are kept unless `overwrite`, and a variant someone already reviewed is left alone."""
    from jobhunt import db as db_module
    from jobhunt.cli import _import_answers_file
    from jobhunt.finai import TAILORED_LABEL, tailored_variant_path
    from jobhunt.lifecycle import save_resume_variant

    lines: list[str] = []
    conn = db_module.connect(root / DB_RELATIVE)
    try:
        answers_file = root / ANSWERS_LOCAL
        if answers_file.is_file():
            rows = _import_answers_file(conn, answers_file, overwrite)
            imported = sum(1 for bucket, _k, _r in rows if bucket == "imported")
            unfilled = sum(1 for bucket, _k, _r in rows if bucket == "skipped_fill")
            note = f"{imported} answers imported into {DB_RELATIVE}"
            if unfilled:
                note += f" ({unfilled} still [FILL]; fill them, then run `uv run python -m jobhunt init` again)"
            lines.append(f"  {ANSWERS_LOCAL}: {note}")
        if _reviewed_variant_count(conn) == 0 and (root / DEFAULT_VARIANT_PATH).is_file():
            save_resume_variant(
                conn,
                label=DEFAULT_VARIANT_LABEL,
                path=DEFAULT_VARIANT_PATH,
                purpose="Default variant; the lane tailors a PDF per posting from this resume",
                reviewed=True,
            )
            lines.append(f"  resume variant '{DEFAULT_VARIANT_LABEL}': registered as reviewed ({DEFAULT_VARIANT_PATH})")
        if not _tailored_variant_reviewed(conn):
            # Every tailored PDF is linked to this variant; unreviewed, the readiness check
            # refuses the posting (resume_not_reviewed) and no submission could be recorded.
            save_resume_variant(
                conn,
                label=TAILORED_LABEL,
                path=tailored_variant_path(),
                purpose="Per-posting tailored resume made by the autoapply lane",
                reviewed=True,
            )
            lines.append(f"  resume variant '{TAILORED_LABEL}': registered as reviewed")
    finally:
        conn.close()
    return lines


def _tailored_variant_reviewed(conn: sqlite3.Connection) -> bool:
    from jobhunt.finai import TAILORED_LABEL

    row = conn.execute("SELECT reviewed_at FROM resume_variants WHERE label=?", (TAILORED_LABEL,)).fetchone()
    return bool(row and row[0])


def _reviewed_variant_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM resume_variants WHERE reviewed_at IS NOT NULL").fetchone()[0]


# --- doctor -------------------------------------------------------------------------------


def chrome_path() -> str | None:
    """A Chrome install the apply driver can launch (playwright channel "chrome")."""
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/chrome",
    ]
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            candidates.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    for found in candidates:
        if Path(found).is_file():
            return found
    return shutil.which("google-chrome") or shutil.which("google-chrome-stable")


def _placeholders(root: Path) -> list[str]:
    hits = []
    for rel in PLACEHOLDER_FILES:
        path = root / rel
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "[FILL" in line and not line.lstrip().startswith("#"):
                hits.append(f"{rel}:{number}: {line.strip()[:70]}")
    return hits


def _example_persona_left(root: Path) -> list[str]:
    left = []
    answers = root / ANSWERS_LOCAL
    if answers.is_file() and "Example State" in answers.read_text(encoding="utf-8"):
        left.append(f"{ANSWERS_LOCAL} still names the example school (school, degree): replace them with your own")
    for rel in ("PROFILE.md", "data/facts.local.md"):
        path = root / rel
        if path.is_file() and PERSONA_MARKER in path.read_text(encoding="utf-8"):
            left.append(f"{rel} still describes the example persona ({PERSONA_MARKER})")
    for src, dst in EXAMPLE_COPIES:
        a, b = root / src, root / dst
        if a.is_file() and b.is_file() and a.read_bytes() == b.read_bytes():
            left.append(f"{dst} is still the example file")
    prefs, template = root / PREFS_LOCAL, root / "data/search-preferences.template.toml"
    if prefs.is_file() and template.is_file():
        try:
            same = (
                tomllib.loads(prefs.read_text(encoding="utf-8")).get("standing_answers")
                == tomllib.loads(template.read_text(encoding="utf-8")).get("standing_answers")
            )
        except tomllib.TOMLDecodeError:
            return left + [f"{PREFS_LOCAL} is not valid TOML"]
        if same:
            left.append(f"[standing_answers] in {PREFS_LOCAL} still has the example values (the unattended lane answers from it)")
    return left


def _db_state(root: Path) -> tuple[bool, str]:
    db = root / DB_RELATIVE
    if not db.is_file():
        return False, f"{DB_RELATIVE} does not exist"
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            count = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return False, f"{DB_RELATIVE} has no companies table"
    return (count > 0), f"{count} companies in {DB_RELATIVE}"


def _required_answer_keys(root: Path) -> list[str]:
    for rel in (PREFS_LOCAL, "data/search-preferences.template.toml"):
        path = root / rel
        if path.is_file():
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError:
                continue
            return list(data.get("required_application_fields", []))
    return []


def _pdf_converter_check() -> Check:
    from jobhunt.resume import docx_resume

    found = docx_resume.pdf_converter()
    if found:
        return Check("pdf converter", "ok", found)
    return Check("pdf converter", "warn", f"{docx_resume.NO_CONVERTER_HELP}; autoapply cannot tailor a resume without one")


def _answers_check(root: Path) -> Check:
    """application_answers must hold every required_application_fields key, or the unattended
    lane skips each posting as not_ready:missing_required_answer:<key>."""
    from jobhunt.apply_assist.profile_store import answer_for_preference

    fix = "run `uv run python -m jobhunt init`, which loads data/answers.local.md; fill any [FILL] first"
    db = root / DB_RELATIVE
    stored: dict[str, str] = {}
    if db.is_file():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                stored = {k: v for k, v in conn.execute("SELECT semantic_key, value FROM application_answers")}
            finally:
                conn.close()
        except sqlite3.Error:
            stored = {}
    missing = [k for k in _required_answer_keys(root) if not answer_for_preference(stored, k)]
    if missing:
        return Check("answers", "warn", f"answers not imported: {', '.join(missing)} - {fix}")
    return Check("answers", "ok", "required answers are in the database")


def _resume_variant_check(root: Path) -> Check:
    from jobhunt.finai import TAILORED_LABEL

    fix = "run `uv run python -m jobhunt init`, which registers the default and the tailored resume variants as reviewed"
    db = root / DB_RELATIVE
    count, tailored = 0, False
    if db.is_file():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                count = _reviewed_variant_count(conn)
                tailored = _tailored_variant_reviewed(conn)
            finally:
                conn.close()
        except sqlite3.Error:
            count = 0
    if count == 0:
        return Check("resume variant", "warn", f"no reviewed resume variant; every posting parks at select_resume_variant - {fix}")
    if not tailored:
        return Check("resume variant", "warn", f"'{TAILORED_LABEL}' is not reviewed; autoapply would stop at resume_not_reviewed - {fix}")
    return Check("resume variant", "ok", f"{count} reviewed")


def _claude_cli_check() -> Check:
    from jobhunt.connectors.claude_cli import _binary

    binary = _binary()
    path = Path(binary).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return Check("claude cli", "ok", str(path))
    return Check(
        "claude cli",
        "warn",
        "not found; auto-apply resume tailoring needs the `claude` CLI (Claude Code on your own "
        "subscription). Harvest, scoring and the UI work without it",
    )


def run_doctor(root: Path) -> list[Check]:
    checks: list[Check] = []

    required = [dst for _src, dst in (*TEMPLATE_COPIES, *EXAMPLE_COPIES)]
    missing = [rel for rel in required if not (root / rel).is_file()]
    checks.append(
        Check("local files", "fail", f"{len(missing)} of {len(required)} missing", missing)
        if missing
        else Check("local files", "ok", f"all {len(required)} present")
    )

    holes = _placeholders(root)
    checks.append(
        Check("placeholders", "fail", f"{len(holes)} [FILL] left", holes)
        if holes
        else Check("placeholders", "ok", "no [FILL] left")
    )

    stale = _example_persona_left(root)
    checks.append(
        Check("example data", "warn", "example persona still in use", stale)
        if stale
        else Check("example data", "ok", "none of the example persona is left")
    )

    absent = [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]
    checks.append(
        Check("python deps", "fail", "not importable: " + ", ".join(absent) + " (run `uv sync`)")
        if absent
        else Check("python deps", "ok", "importable")
    )

    node = shutil.which("node")
    spa = root / "jobhunt/web/static/spa/index.html"
    if not node:
        checks.append(Check("node", "warn", "not found; install Node.js to build the web UI"))
    elif not spa.is_file():
        checks.append(Check("node", "ok", f"{node} (UI not built yet: run `make ui-build`)"))
    else:
        checks.append(Check("node", "ok", f"{node} (UI built)"))

    chrome = chrome_path()
    checks.append(
        Check("chrome", "ok", chrome)
        if chrome
        else Check(
            "chrome",
            "warn",
            'not found (the apply driver uses playwright channel "chrome"); install Chrome, '
            "or run `uv run playwright install chromium` for the fallback",
        )
    )

    seeded, detail = _db_state(root)
    checks.append(Check("database", "ok" if seeded else "warn", detail if seeded else f"not seeded: {detail}"))
    checks.append(_pdf_converter_check())
    checks.append(_claude_cli_check())
    checks.append(_answers_check(root))
    checks.append(_resume_variant_check(root))
    return checks


def next_commands(checks: list[Check]) -> list[str]:
    by_name = {c.name: c for c in checks}
    steps = []
    if by_name["python deps"].status == "fail":
        steps.append("uv sync")
    if by_name["local files"].status == "fail":
        steps.append("uv run python -m jobhunt init        # creates the missing local files")
    if by_name["placeholders"].status == "fail" or by_name["example data"].status == "warn":
        steps.append("edit the files listed above, then: uv run python -m jobhunt doctor")
    if by_name["database"].status != "ok":
        steps.append("uv run python -m jobhunt seed")
    if by_name["answers"].status != "ok" or by_name["resume variant"].status != "ok":
        steps.append("uv run python -m jobhunt init        # loads your answers and registers the default resume variant")
    steps += [
        "uv run python -m jobhunt harvest",
        "uv run python -m jobhunt score",
    ]
    if by_name["node"].status == "ok":
        steps.append("make ui        # http://127.0.0.1:8787")
    else:
        steps.append("install Node.js, then: make ui-build && make ui")
    if by_name["chrome"].status != "ok":
        steps.append("install Google Chrome (or: uv run playwright install chromium)")
    if by_name["claude cli"].status != "ok":
        steps.append("install Claude Code (https://claude.com/claude-code) for auto-apply tailoring")
    return steps


def format_checks(checks: list[Check]) -> str:
    mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
    out = ["Doctor"]
    for c in checks:
        out.append(f"  [{mark[c.status]}] {c.name}: {c.detail}")
        out += [f"         - {item}" for item in c.items]
    out.append("")
    out.append("Next commands")
    out += [f"  {step}" for step in next_commands(checks)]
    return "\n".join(out)
