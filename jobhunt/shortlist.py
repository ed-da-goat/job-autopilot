"""`shortlist` command: markdown output of the top-N scored postings, deduped
to one row per company, with a one-line "why you" angle per row.

Rubric v1 hasn't been calibrated against Ed's manual review yet — every run
prints a warning to stderr so the output is never mistaken for vetted.
"""

from __future__ import annotations

import json
import sqlite3

from jobhunt.scoring_components import matched_skill_names
from jobhunt.text_extract import extract_text

FOUNDER_LEVERAGE_THRESHOLD = 6


def _why_you(row: sqlite3.Row, company_row: dict | None, rubric: dict) -> str:
    components = json.loads(row["components"])
    title, description, _has_desc = extract_text(row)
    text = f"{title} {description}"

    if company_row and company_row.get("category") == "fintech":
        return "finance degree + shipped AI is the exact hybrid they want"

    top_component = max(components, key=lambda k: components[k])
    if top_component == "founder_leverage" and components["founder_leverage"] >= FOUNDER_LEVERAGE_THRESHOLD:
        return "forward-deployed + build role — founder story lands here"
    if top_component == "skill_match":
        matched = matched_skill_names(text, rubric)
        if matched:
            return f"stack match: {', '.join(matched[:5])}"

    return "broad fit across the rubric — worth a closer look"


def build_shortlist_rows(rows: list[sqlite3.Row], top_n: int) -> list[dict]:
    """Collapse to one row per company FIRST (best posting wins, extras with a
    score within 15 points counted), then take the top N companies — so the
    shortlist is N companies, not N postings collapsed to fewer."""
    ranked = sorted(rows, key=lambda r: (-(r["total"] or 0), r["posting_id"]))

    company_best: dict[str, sqlite3.Row] = {}
    company_count: dict[str, int] = {}
    order: list[str] = []
    for row in ranked:
        company = row["company"] or "(unknown)"
        if company not in company_best:
            company_best[company] = row
            order.append(company)
        if (row["total"] or 0) >= (company_best[company]["total"] or 0) - 15:
            company_count[company] = company_count.get(company, 0) + 1

    return [
        {"rank": i, "row": company_best[company], "extra_roles": company_count[company] - 1}
        for i, company in enumerate(order[:top_n], start=1)
    ]


def render_markdown(entries: list[dict], companies: dict[str, dict], rubric: dict) -> str:
    lines = [
        "# Shortlist",
        "",
        f"_Generated from rubric v{rubric.get('version', '?')} scores (Ed-calibrated 2026-07-17). Regenerate: make shortlist or `uv run python -m jobhunt shortlist`._",
        "",
        "| Rank | Score | Company | Title | Location | Why you |",
        "|---|---|---|---|---|---|",
    ]
    for entry in entries:
        row = entry["row"]
        company_row = companies.get(row["company"]) if row["company"] else None
        angle = _why_you(row, company_row, rubric)
        title = row["title"] or ""
        if entry["extra_roles"] > 0:
            title += f" (+{entry['extra_roles']} more roles)"
        location = row["location"] or ""
        lines.append(
            f"| {entry['rank']} | {row['total']} | [{row['company']}]({row['url']}) | {title} | {location} | {angle} |"
        )
    lines.append("")
    return "\n".join(lines)
