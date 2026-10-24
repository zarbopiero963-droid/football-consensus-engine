"""Optional, isolated API-FOOTBALL v3 (football only) MCP module.

Nothing in this package runs at import time against the network. The bridge
imports it defensively: a failure here must never affect browser tools, OAuth,
sessions, leases or health.
"""
