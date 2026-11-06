"""`sample` command: a stratified, deterministic slice of scored postings
for manual rubric calibration. Spreads picks across score bands and, within
each band, across sources.
"""

from __future__ import annotations

import json
import random
import sqlite3

from jobhunt.text_extract import extract_text

BANDS = [
    ("80+", 80, 101),
    ("65-79", 65, 80),
    ("50-64", 50, 65),
    ("35-49", 35, 50),
    ("<35", 0, 35),
]
DESCRIPTION_PREVIEW_CHARS = 200


def _band_for(total: int) -> str:
    for name, lo, hi in BANDS:
        if lo <= total < hi:
            return name
    return BANDS[-1][0]


def _quotas(n: int) -> dict[str, int]:
    base = n // len(BANDS)
    remainder = n % len(BANDS)
    return {name: base + (1 if i < remainder else 0) for i, (name, _, _) in enumerate(BANDS)}


def _redistribute(quotas: dict[str, int], available: dict[str, int]) -> dict[str, int]:
    """Shrink quotas that exceed what's available in their band, then hand
    the shortfall to bands (in band order) that still have room."""
    quotas = dict(quotas)
    shortfall = 0
    for name in quotas:
        if quotas[name] > available[name]:
            shortfall += quotas[name] - available[name]
            quotas[name] = available[name]

    names = [name for name, _, _ in BANDS]
    guard = 0
    idx = 0
    while shortfall > 0 and guard < 10_000:
        name = names[idx % len(names)]
        idx += 1
        guard += 1
        if quotas[name] < available[name]:
            quotas[name] += 1
            shortfall -= 1
        elif all(quotas[n] >= available[n] for n in names):
            break  # every band exhausted, can't fill n
    return quotas


def _pick_source_diverse(candidates: list[sqlite3.Row], k: int, rng: random.Random) -> list[sqlite3.Row]:
    if k <= 0 or not candidates:
        return []
    shuffled = list(candidates)
    rng.shuffle(shuffled)

    by_source: dict[str, list[sqlite3.Row]] = {}
    for row in shuffled:
        by_source.setdefault(row["source"], []).append(row)
    source_order = list(by_source.keys())
    rng.shuffle(source_order)

    picked: list[sqlite3.Row] = []
    while len(picked) < k and any(by_source.values()):
        for src in source_order:
            if len(picked) >= k:
                break
            bucket = by_source.get(src)
            if bucket:
                picked.append(bucket.pop(0))
    return picked


def stratified_sample(rows: list[sqlite3.Row], n: int, seed: int) -> list[sqlite3.Row]:
    """Deterministic given (rows, n, seed) — same inputs always yield the
    same picks in the same order."""
    banded: dict[str, list[sqlite3.Row]] = {name: [] for name, _, _ in BANDS}
    for row in rows:
        banded[_band_for(row["total"])].append(row)

    quotas = _redistribute(_quotas(n), {name: len(banded[name]) for name in banded})

    rng = random.Random(seed)
    picked: list[sqlite3.Row] = []
    for name, _, _ in BANDS:
        picked.extend(_pick_source_diverse(banded[name], quotas[name], rng))
    return picked


def format_sample_row(row: sqlite3.Row) -> str:
    components = json.loads(row["components"])
    missing = json.loads(row["missing_evidence"])
    _title, description, _has_desc = extract_text(row)
    preview = description[:DESCRIPTION_PREVIEW_CHARS] if description else "(no description)"

    comp_line = ", ".join(f"{k}={v}" for k, v in components.items())
    missing_line = ", ".join(missing) if missing else "none"

    return (
        f"[{row['total']:>3}] {row['company']} — {row['title']} ({row['source']})\n"
        f"  {row['url']}\n"
        f"  components: {comp_line}\n"
        f"  missing_evidence: {missing_line}\n"
        f"  description: {preview}"
    )
