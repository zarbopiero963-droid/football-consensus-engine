"""Local / dev-only signer SERVICE prototype (owner mandate SXBET-SECURE-SIGNER-PREPARATION-01).

An inbound-only HTTP front end for the isolated signer (`signer/`): MCP -> signer requests authenticated
with HMAC-SHA256, a persistent append-only request lifecycle, and owner approval with a WebAuthn passkey
from an Android phone (PWA page). It is NOT operational in production:
- it inherits the signer import guard (SXBET_SIGNER_PROCESS=1 and no RENDER, checked again here);
- it signs only with an EPHEMERAL throwaway key generated in memory at start (never read from env/file);
- it has NO exchange sender, NO outbound HTTP client and builds the submission gateway with no transport:
  the LIVE_TRADING flag is never loaded, so every hand-off to the gateway is refused and recorded;
- market data is SIMULATED (injected provider).
No financial operation is executable through it. See signer_service/README.md.
"""
import os as _os

import signer as _signer  # noqa: F401  (raises ImportError: SXBET_SIGNER_IMPORT_REFUSED outside the signer process)

if _os.environ.get("SXBET_SIGNER_PROCESS") != "1" or "RENDER" in _os.environ:  # defence in depth
    raise ImportError("SXBET_SIGNER_IMPORT_REFUSED: signer_service runs only in the signer process")

SERVICE_MODE = "DEV_SIMULATION_ONLY"
LIVE_TRADING = "UNAVAILABLE"
