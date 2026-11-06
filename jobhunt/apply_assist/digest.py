"""Render a TriageDigest as a readable terminal block."""

from __future__ import annotations

from jobhunt.apply_assist.schema import TriageDigest

_FOOTER = "Review the form in the browser — YOU click submit. This tool never will."


def render_digest(d: TriageDigest) -> str:
    lines: list[str] = []
    lines.append(f"Posting #{d.posting_id}  ats={d.ats or 'unknown'}")
    lines.append(d.url)
    lines.append("")

    lines.append(f"FILLED ({len(d.filled)})")
    for outcome in d.filled:
        mark = "✓" if outcome.committed else "✗"
        label = outcome.field.label or outcome.field.name or outcome.field.selector
        key = outcome.semantic_key or "?"
        line = f"{mark} {key}  {label}  → {outcome.value}"
        if not outcome.committed:
            line += f"  (readback: {outcome.readback!r})"
        lines.append(line)
    lines.append("")

    if d.drafts:
        lines.append(f"AI DRAFTS — NOT typed in ({len(d.drafts)})")
        for res in d.drafts:
            label = res.field.label or res.field.name or res.field.selector
            value = res.value or ""
            if len(value) > 120:
                value = value[:120] + "…"
            lines.append(f"🔴 {label} → {value}")
        lines.append("")

    lines.append(f"NEEDS YOU ({len(d.residual)})")
    for res in d.residual:
        label = res.field.label or res.field.name or res.field.selector
        lines.append(f"? {label} ({res.reason})")
    lines.append("")

    lines.append(f"SKIPPED ({len(d.skipped)})")
    if d.skipped:
        reasons = ", ".join(
            f"{r.field.label or r.field.name or r.field.selector} ({r.reason})"
            for r in d.skipped
        )
        lines.append(reasons)
    lines.append("")

    if d.errors:
        lines.append("ERRORS")
        lines.extend(d.errors)
        lines.append("")

    lines.append(_FOOTER)
    return "\n".join(lines)
