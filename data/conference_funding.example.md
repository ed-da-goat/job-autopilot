# Conference funding (example)

Copy this file to `data/conference_funding.md` (gitignored) and replace the
fictional rows with your own. Then run `uv run jobhunt conferences`.

Only the "Action queue" table below is read. Columns:

- Due: ISO date the action first surfaces on Today.
- Recur: days between repeats after you mark it done (blank = one-shot).
- Action: what to do.
- Program: the conference or funding program (required; rows without one are skipped).
- Notes: free text.

## Action queue

| Due | Recur | Action | Program | Notes |
|---|---|---|---|---|
| 2026-12-01 | 30 | Check + apply | Example Data Summit travel grant | Fictional program; applications reopen monthly |
| 2026-12-15 |  | Verify eligibility | Example Students in Tech scholarship | Fictional; confirm enrollment status first |
| 2027-01-10 |  | Submit receipts | Example Dev Conf reimbursement | **Fictional**; keep itemized receipts |
