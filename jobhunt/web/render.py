"""Tiny template-side rendering helpers (Jinja globals). No DB access, no
funnel semantics — pure presentation math the templates couldn't do inline.
"""

from __future__ import annotations

ACCENT_HUE = 165  # single teal accent; red is reserved for the apply-rate warning


def score_badge_style(total: int | None) -> str:
    """One accent hue, lightness scaled by score — a gradient by band without
    introducing a second color."""
    if total is None:
        return "background:#2a2a32;color:#8a8a95;"
    clamped = max(0, min(100, total))
    lightness = 24 + round(clamped * 0.32)  # 24..56
    text = "#08120f" if clamped >= 35 else "#e7ece9"
    return f"background:hsl({ACCENT_HUE} 55% {lightness}%);color:{text};"


def score_bar_style(value: int, max_value: int) -> str:
    """Width % for a per-component score breakdown bar."""
    pct = 0 if not max_value else max(0, min(100, round(value / max_value * 100)))
    return f"width:{pct}%;background:hsl({ACCENT_HUE} 55% 42%);"
