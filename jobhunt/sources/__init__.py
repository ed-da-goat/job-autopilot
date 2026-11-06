"""Per-source job-board fetchers. Each module exposes a `fetch(...)` that
returns a list of normalized posting dicts:
source_posting_id, company, title, location, url, posted_at, raw.
"""
