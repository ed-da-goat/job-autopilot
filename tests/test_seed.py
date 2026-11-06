from __future__ import annotations

from jobhunt import db as db_module
from jobhunt import seed as seed_module

from tests.conftest import FIXTURES_DIR


def test_parse_seed_markdown_table_and_watchlist():
    text = (FIXTURES_DIR / "seed_companies_snippet.md").read_text(encoding="utf-8")
    table_rows, watchlist_rows = seed_module.parse_seed_markdown(text)

    assert len(table_rows) == 3
    anthropic = next(r for r in table_rows if r["name"] == "Anthropic")
    assert anthropic["category"] == "ai-infra"
    assert anthropic["platform"] == "greenhouse"
    assert anthropic["slug"] == "anthropic"
    assert anthropic["verified"] == "yes"
    assert anthropic["watchlist"] == 0

    clay = next(r for r in table_rows if r["name"] == "Clay")
    assert clay["platform"] == "unknown"

    assert len(watchlist_rows) == 3
    names = {r["name"] for r in watchlist_rows}
    assert names == {"Meta", "Moov (Moov Financial)", "Retool"}
    for row in watchlist_rows:
        assert row["watchlist"] == 1
        assert row["platform"] is None

    moov = next(r for r in watchlist_rows if r["name"] == "Moov (Moov Financial)")
    # continuation line should be folded into why_fit
    assert "no Greenhouse/Lever/Ashby link found" in moov["why_fit"]


def test_seed_from_file_upserts_into_db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = db_module.connect(db_path)
    try:
        count = seed_module.seed_from_file(conn, FIXTURES_DIR / "seed_companies_snippet.md")
        assert count == 6  # 3 table rows + 3 watchlist rows

        rows = conn.execute("SELECT name, watchlist FROM companies ORDER BY name").fetchall()
        assert len(rows) == 6

        # re-seeding is idempotent (upsert by name), no duplicate rows
        seed_module.seed_from_file(conn, FIXTURES_DIR / "seed_companies_snippet.md")
        rows_again = conn.execute("SELECT COUNT(*) c FROM companies").fetchone()
        assert rows_again["c"] == 6
    finally:
        conn.close()
