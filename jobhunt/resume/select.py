"""Deterministic resume-variant selector. No LLM call, no spend.

Maps a posting's title (and score components) to one of the resume variants
defined in `resume/resume.yaml`. `variant-plan` shows the assignment over the
queue read-only; `apply_plan` writes `applications.resume_variant_id` only for
rows whose chosen variant exists in `resume_variants` and has been reviewed,
and never touches an already-submitted application.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

# Ordered: first cluster with a keyword hit wins. yaml_key is the variant key in
# resume/resume.yaml; db_label is resume_variants.label.
RULES: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "finance_ai",
        "Finance x AI 2026",
        (
            "finance",
            "fintech",
            "quant",
            "trading",
            "trader",
            "analyst",
            "banking",
            "bank",
            "capital",
            "investment",
            "treasury",
            "payments",
            "risk",
        ),
    ),
    (
        "founder_engineer",
        "Software Engineer 2026",
        (
            "founder",
            "founding",
            "forward deployed",
            "gtm",
            "full stack",
            "full-stack",
            "ai engineer",
            "product engineer",
            "software engineer",
            "platform",
            "forward-deployed",
            "deployed engineer",
            "fullstack",
            "solutions engineer",
            "agent engineer",
            "machine learning",
            "backend",
            "infrastructure",
            "research engineer",
            "data scientist",
            "integrations engineer",
            "developer relations",
            "support engineer",
        ),
    ),
]
DEFAULT_YAML_KEY = "founder_engineer"
FINANCE_YAML_KEY = "finance_ai"
DEFAULT_STAGES = ("ready", "suggested")

_LABEL_BY_KEY = {key: label for key, label, _ in RULES}


@dataclass(frozen=True)
class Selection:
    yaml_key: str
    db_label: str
    matched: bool
    reason: str


@dataclass(frozen=True)
class PlanRow:
    posting_id: int
    stage: str
    title: str
    selection: Selection
    variant_id: int | None
    variant_reviewed: bool
    current_variant_id: int | None
    has_application: bool
    submitted_at: str | None
    action: str


def select_variant(title: str, components: dict | None) -> Selection:
    lowered = (title or "").lower()
    for yaml_key, db_label, keywords in RULES:
        for keyword in keywords:
            if keyword in lowered:
                return Selection(yaml_key, db_label, True, f"title~'{keyword}'")
    # The finance_hybrid_bonus score component is deliberately NOT a selector:
    # on the live queue it fired for Sierra strategists, a CLEAR SDE, and two
    # security engineers. Title keywords only; the bonus is reported as context.
    bonus = (components or {}).get("finance_hybrid_bonus", 0) or 0
    try:
        bonus_note = "; finance_hybrid_bonus>0 (ignored)" if float(bonus) > 0 else ""
    except (TypeError, ValueError):
        bonus_note = ""
    return Selection(DEFAULT_YAML_KEY, _LABEL_BY_KEY[DEFAULT_YAML_KEY], False, "no keyword match; default" + bonus_note)


def _components(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _action(
    *,
    variant_id: int | None,
    reviewed: bool,
    current_variant_id: int | None,
    submitted_at: str | None,
) -> str:
    if submitted_at:
        return "skip_submitted"
    if variant_id is None:
        return "blocked_missing_variant"
    if not reviewed:
        return "blocked_unreviewed"
    if current_variant_id == variant_id:
        return "keep"
    return "set"


def plan_variants(conn: sqlite3.Connection, stages: tuple[str, ...] | list[str] = DEFAULT_STAGES) -> list[PlanRow]:
    """Read-only: compute the variant assignment for funnel rows in `stages`."""
    stages = tuple(stages)
    if not stages:
        return []
    placeholders = ",".join("?" for _ in stages)
    rows = conn.execute(
        f"""SELECT f.posting_id AS posting_id, f.stage AS stage, p.title AS title,
                   s.components AS components,
                   a.id AS application_id, a.resume_variant_id AS current_variant_id,
                   a.submitted_at AS submitted_at
            FROM funnel f
            JOIN postings p ON p.id = f.posting_id
            LEFT JOIN scores s ON s.posting_id = f.posting_id AND s.excluded = 0
                 AND s.rubric_version = (SELECT MAX(rubric_version) FROM scores)
            LEFT JOIN applications a ON a.posting_id = f.posting_id
            WHERE f.stage IN ({placeholders})
            ORDER BY f.posting_id""",
        stages,
    ).fetchall()
    variants = {
        row["label"]: (row["id"], row["reviewed_at"] is not None and str(row["reviewed_at"]).strip() != "")
        for row in conn.execute("SELECT id, label, reviewed_at FROM resume_variants")
    }
    plan: list[PlanRow] = []
    for row in rows:
        selection = select_variant(row["title"] or "", _components(row["components"]))
        variant_id, reviewed = variants.get(selection.db_label, (None, False))
        plan.append(
            PlanRow(
                posting_id=row["posting_id"],
                stage=row["stage"],
                title=row["title"] or "",
                selection=selection,
                variant_id=variant_id,
                variant_reviewed=reviewed,
                current_variant_id=row["current_variant_id"],
                has_application=row["application_id"] is not None,
                submitted_at=row["submitted_at"],
                action=_action(
                    variant_id=variant_id,
                    reviewed=reviewed,
                    current_variant_id=row["current_variant_id"],
                    submitted_at=row["submitted_at"],
                ),
            )
        )
    return plan


def apply_plan(conn: sqlite3.Connection, plan: list[PlanRow]) -> int:
    """Write the variant for `set` rows only. Returns the number of rows set."""
    from jobhunt import lifecycle

    count = 0
    now = datetime.now(UTC).isoformat()
    for row in plan:
        if row.action != "set" or row.variant_id is None:
            continue
        if not row.has_application:
            lifecycle.ensure_application(conn, row.posting_id, resume_variant_id=row.variant_id)
        else:
            conn.execute(
                "UPDATE applications SET resume_variant_id=?, updated_at=? "
                "WHERE posting_id=? AND submitted_at IS NULL",
                (row.variant_id, now, row.posting_id),
            )
        count += 1
    conn.commit()
    return count


def format_plan(plan: list[PlanRow]) -> str:
    lines: list[str] = []
    for row in plan:
        flag = "matched" if row.selection.matched else "unmapped"
        lines.append(
            f"{row.posting_id} · {row.stage} · {row.title[:60]} · {row.selection.db_label} · {flag} · {row.action}"
        )
    lines.append("")
    lines.append(f"Total: {len(plan)}")
    by_label = Counter(row.selection.db_label for row in plan)
    by_action = Counter(row.action for row in plan)
    lines.append("Per label: " + (", ".join(f"{k}={v}" for k, v in sorted(by_label.items())) or "none"))
    lines.append("Per action: " + (", ".join(f"{k}={v}" for k, v in sorted(by_action.items())) or "none"))
    unmapped: list[str] = []
    seen: set[str] = set()
    for row in plan:
        if row.selection.matched:
            continue
        key = row.title.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unmapped.append(row.title)
    shown = unmapped[:30]
    lines.append(f"Unmapped titles ({len(unmapped)} distinct, showing {len(shown)}):")
    lines.extend(f"  - {t}" for t in shown)
    return "\n".join(lines)
