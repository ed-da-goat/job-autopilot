"""Outbound LLM connectors — deliberately separate from `jobhunt.net` (the
polite-GET harvest path). These make authed, spend-gated POST calls and are
never invoked without an explicit, Ed-triggered gate (see
`jobhunt.connectors.openrouter` and `jobhunt.spend`).
"""
