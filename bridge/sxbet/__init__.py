"""SX Bet (V3) integration for the MCP bridge — READ_ONLY + SIMULATION.

Source of truth: official OpenAPI https://docs.sx.bet/openapi.json (SX Bet API
1.0.0, V3 routes). This package performs GET requests only: no order, cancel,
approval, transfer or signing path exists here (governance §2.6). SIMULATION
(`simulation.py`) is pure computation over data already read. Signing lives in
the separate top-level `signer/` package, which nothing under `bridge/` imports.
"""
