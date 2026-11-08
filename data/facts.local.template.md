# Facts file — job-autopilot tailoring tier
#
# Copy this file to data/facts.local.md (gitignored — carries no secrets by
# itself, but keep it out of git since it's your personal grounding
# source). jobhunt/apply_assist/tailor_answers.py reads the copy and passes
# it to the LLM as the ONLY source of truth for drafted essay answers.
# The persona below ("Alex Rivera") is a placeholder: replace every fact.
#
# GUARDRAIL — this file is the truthfulness boundary, not the resume.
# The tailoring system prompt is instructed to answer ONLY from facts
# present here and to return null (never a guess) when this file can't
# support an answer. So: if it isn't written down below, it doesn't get
# claimed in a drafted answer, ever.
#
#   * "sells" / "runs outbound sales", NEVER "sold", unless a sale really
#     happened. No completed-sale, acquisition, or exit claims you can't back.
#   * Alex is NOT a CPA and holds no accounting or legal credential.
#   * Alex holds a B.S. in Computer Science (expected Dec 2026), no graduate
#     degree.
#   * GPA (3.4) is stated only if a question directly asks for it.
#
# Format: plain prose under each heading. Write it the way you'd actually
# want to be quoted — the model drafts from this text nearly verbatim.

## Projects

Example Mail Agent: a multi-tenant AI email-drafting agent built and
deployed solo — FastAPI + async SQLAlchemy + Postgres. It monitors a Gmail
inbox, drafts replies grounded in vetted CRM context via an LLM gateway,
and requires human approval in a review UI before anything sends. The
safety core: a Postgres trigger makes it structurally impossible for the AI
to send email (only a human-approver DB role can flip a draft to
approved/sent), row-level security enforces tenant isolation, the audit log
is append-only (DB trigger blocks UPDATE/DELETE), and stored credentials
are encrypted at rest (AES-256-GCM). Every one of those invariants is
pinned by its own Postgres test. Built and grown solo since 2026,
alongside running the outbound sales pipeline (cold calls, discovery
calls, product demos) for Example Ventures LLC, where Alex is the sole
member and primary builder/operator.

[FILL: add other projects here — job-autopilot itself (this repo: a scored,
scheduled job-search pipeline with an apply-assist Playwright layer),
coursework projects, hackathons, anything with real, specific, defensible
numbers.]

## Skills

Engineering: Python (FastAPI, SQLAlchemy, pytest), Postgres (row-level
security, Alembic), SQLite, LLM & agent systems (prompt design, evals),
Google OAuth/Gmail API, JavaScript, HTMX, Tailwind, Docker, Git/CI, AWS,
Google Cloud.

Business: financial modeling (Excel), investment reporting, cold
outbound, discovery calls, product demos, CRM pipeline design.

## Education

Example State University, College of Computing — Bachelor of Science in
Computer Science, expected Dec 2026. Study
abroad in Lyon, France, Summer 2025. GPA 3.4 — state only if a question
directly asks for it.

## Work history

- Example Ventures LLC — Founder & Engineer, 2026–Present, Atlanta, GA.
  Founded an AI consulting company that sells an installed private AI
  assistant to data-sensitive small businesses; sole builder and primary
  outbound operator. See Projects above for the technical detail.
- Example Co — Financial Advisor Assistant, Jan 2024–Present, Atlanta,
  GA. Streamlined the tax-filing process, cutting preparation time by 30%
  through AI-assisted due diligence. Built Excel models projecting client
  retirement scenarios. Prepared quarterly investment-performance
  reports, improving accuracy/presentation quality by 25%.
- Student Entrepreneurs Club — Director of Finance, May 2025–Present,
  Atlanta, GA. Manages a $25,000 budget funding student
  entrepreneurship initiatives at Example State University. Revamped
  financial reporting, cutting member reimbursement payout time by 50%.
- Example Retail — Fulfillment Expert, Oct 2021–Jan 2024, Atlanta, GA.
  Sustained 100+ items/hour fulfillment on a 20-person team, contributing
  to a 10% increase in on-time shipments, while carrying a full course
  load.

## What Alex is looking for (motivation grounding)

Alex wants a first full-time role building and shipping software for a
small, fast-moving team, ideally in finance or fintech. Alex is open to
relocation, New York City included, and any hybrid schedule is fine.

## Constraints — things to NEVER claim

- No CPA, no accounting credential, no legal credential.
- The degree is a B.S. in Computer Science; no graduate degree.
- Never say Example Ventures has "sold" or completed a sale — it is
  pre-revenue with no signed client as of this writing. "Sells," "built and
  runs the outbound pipeline for," etc. are fine; "sold X to Y" is not.
- Never invent metrics, dates, or clients not listed above. If a question
  needs a fact this file doesn't have, the correct answer is null — leave
  it for Alex to answer by hand.
