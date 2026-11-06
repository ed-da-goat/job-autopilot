"""Daily LLM spend cap enforcement over the `spend_ledger` table.

Every OpenRouter call in this repo must be preceded by `check_budget` and
followed by `record_spend` (see `jobhunt/connectors/openrouter.py` and the
prep engine that calls it). The cap is a hard stop, not a warning: at or
above the cap, callers get `SpendCapError` and no draft/ledger row is
written.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime

DAILY_CAP_USD = 1.00
RUN_CAP_USD = 0.25


class SpendCapError(Exception):
    """Raised when today's spend has reached (or exceeded) the daily cap."""


def _cap_usd() -> float:
    """Daily cap, overridable via JOBHUNT_DAILY_CAP_USD for testing/tuning."""
    raw = os.environ.get("JOBHUNT_DAILY_CAP_USD")
    if raw is None or raw == "":
        return DAILY_CAP_USD
    return float(raw)


def _run_cap_usd() -> float:
    """Per-run cap, overridable via JOBHUNT_RUN_CAP_USD for testing/tuning."""
    raw = os.environ.get("JOBHUNT_RUN_CAP_USD")
    if raw is None or raw == "":
        return RUN_CAP_USD
    return float(raw)


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def today_spend(conn: sqlite3.Connection) -> float:
    """Sum of cost_usd in spend_ledger for today (UTC date)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend_ledger WHERE day=?",
        (_today(),),
    ).fetchone()
    return float(row["total"])


def check_budget(conn: sqlite3.Connection, anticipated_cost: float = 0.0) -> float:
    """Return remaining daily budget in USD.

    Raises SpendCapError if today's spend has already reached the cap.
    """
    cap = _cap_usd()
    spent = today_spend(conn)
    if spent >= cap or spent + anticipated_cost > cap:
        raise SpendCapError(
            f"Daily spend cap would be exceeded: ${spent:.4f} spent + "
            f"${anticipated_cost:.4f} reserved of ${cap:.2f} cap"
        )
    return cap - spent


def record_spend(
    conn: sqlite3.Connection,
    draft_id: int | None,
    cost_usd: float,
    *,
    job: str | None = None,
    kind: str | None = None,
) -> None:
    """Insert a spend_ledger row for today (UTC), attributed to draft_id.

    `job` and `kind` are optional attribution columns (which scheduled job made
    the call, and what kind of draft it paid for); existing positional callers
    are unaffected.
    """
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO spend_ledger (draft_id, day, cost_usd, at, job, kind) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (draft_id, _today(), cost_usd, now, job, kind),
    )
    conn.commit()


def today_breakdown(conn: sqlite3.Connection) -> dict[str, float]:
    """Per-job spend totals for today (UTC), with untagged rows bucketed as 'draft'."""
    rows = conn.execute(
        "SELECT job, COALESCE(SUM(cost_usd), 0.0) AS total FROM spend_ledger "
        "WHERE day=? GROUP BY job",
        (_today(),),
    ).fetchall()
    breakdown: dict[str, float] = {}
    for row in rows:
        key = row["job"] if row["job"] is not None else "draft"
        breakdown[key] = breakdown.get(key, 0.0) + float(row["total"])
    return breakdown


class RunBudget:
    """A per-batch spend ceiling for one autonomous run.

    This is separate from the daily cap: it bounds a single run, not the whole
    day. Without it, one batch can walk the daily cap on its own — 19 postings
    x $0.25 would otherwise consume the entire $1.00 day in a single run. The
    daily cap (`check_budget`) remains the hard stop; `reserve` always checks
    it too, in addition to this run's own cap.
    """

    def __init__(self, conn: sqlite3.Connection, job: str, cap: float | None = None) -> None:
        self.conn = conn
        self.job = job
        self.cap = cap if cap is not None else _run_cap_usd()
        self._reserved = 0.0
        self._spent = 0.0

    def reserve(self, anticipated_cost: float) -> None:
        """Raise SpendCapError if this run or the daily total would exceed its cap."""
        projected = self._reserved + self._spent + anticipated_cost
        if projected > self.cap:
            raise SpendCapError(
                f"Run spend cap would be exceeded for job={self.job!r}: "
                f"${self._spent:.4f} spent + ${self._reserved:.4f} reserved + "
                f"${anticipated_cost:.4f} requested of ${self.cap:.2f} run cap"
            )
        check_budget(self.conn, anticipated_cost)
        self._reserved += anticipated_cost

    def settle(
        self,
        actual_cost: float,
        *,
        kind: str | None = None,
        draft_id: int | None = None,
    ) -> None:
        """Release the outstanding reservation and record the actual spend for
        this run's job. Call sites reserve() once per item then settle() once
        with the real cost, so releasing clears the whole outstanding balance."""
        self._reserved = 0.0
        self._spent += actual_cost
        record_spend(self.conn, draft_id, actual_cost, job=self.job, kind=kind)

    @property
    def spent(self) -> float:
        return self._spent
