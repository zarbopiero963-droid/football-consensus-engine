"""Isolated SX Bet order signer (governance §2.6).

A separate component, never imported by `bridge/` (enforced by tests). It holds
the order-signing private key in-process only, and signs an EIP-712 `Order`
only for a re-validated intent carrying a matching, single-use, short-lived
Ed25519 approval from the owner's approval key. It contains no HTTP or
submission code: LIVE_TRADING remains UNAVAILABLE. See signer/README.md.
"""
LIVE_TRADING = "UNAVAILABLE"
