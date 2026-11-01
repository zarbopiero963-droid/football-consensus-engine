"""Isolated SX Bet order signer (governance §2.6).

A separate component, never imported by `bridge/` (enforced by tests). It holds
the order-signing private key in-process only, and signs an EIP-712 `Order`
only for a re-validated intent carrying a matching, single-use, short-lived
Ed25519 approval from the owner's approval key. It contains no HTTP or
network code: LIVE_TRADING remains UNAVAILABLE. See signer/README.md.

Fail-closed import guard: the signer is a separate service and never runs
inside the MCP process. Importing it requires SXBET_SIGNER_PROCESS == "1" in the
environment AND the absence of RENDER (Render sets RENDER=true on every
service, including the MCP bridge). Anything else raises ImportError before any
signing module or eth_account is loaded.
"""
import os as _os

SIGNER_PROCESS_ENV = "SXBET_SIGNER_PROCESS"

if _os.environ.get(SIGNER_PROCESS_ENV) != "1" or "RENDER" in _os.environ:
    raise ImportError("SXBET_SIGNER_IMPORT_REFUSED: the signer runs only in its own process, never in the MCP "
                      "runtime (requires SXBET_SIGNER_PROCESS=1 and no RENDER env)")

LIVE_TRADING = "UNAVAILABLE"
