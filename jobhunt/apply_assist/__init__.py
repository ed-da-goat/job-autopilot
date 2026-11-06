"""Apply-assist: pure-Python core for triaging application form fields.

This package classifies scraped form fields into a semantic-key taxonomy,
resolves each field to fill/upload/ask/skip using stored answers, and
renders a human-readable triage digest. It never touches the network or a
browser; a later slice drives Playwright against these interfaces.
"""

from __future__ import annotations
