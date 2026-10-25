# API-FOOTBALL v3 contract registry

This is a fail-closed, offline registry. It derives its tool/path bindings from the existing runtime allowlist, so it cannot silently drift from the 40 provider tools behind the 42 MCP names. It represents 39 unique routes: 38 football paths and /status.

A runtime binding is not official evidence. Until the official source section is acquired and recorded, endpoint metadata, parameter semantics, pagination, response and error schemas remain PENDING_OFFICIAL_VERIFICATION. REAL_VERIFIED is prohibited in this offline mandate.

Use scripts/check_api_football_contracts.py --write to create a snapshot. Snapshot hashes cover only normalized contract content; creation time is metadata. The detector rejects corrupted snapshots and returns nonzero for BREAKING or UNKNOWN changes.
