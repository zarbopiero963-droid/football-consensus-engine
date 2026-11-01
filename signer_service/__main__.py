"""CLI of the DEV signer service (never deploy it; see signer_service/README.md).

  python -m signer_service dev-init <dir>     throwaway dev material (secret files 0600, no value printed)
  python -m signer_service enroll-code        one-time passkey enrolment code -> <STATE_DIR>/enrollment-code.txt
  python -m signer_service serve              inbound HTTP on 127.0.0.1 (non-loopback needs the explicit ack)
Requires SXBET_SIGNER_PROCESS=1 and no RENDER (inherited signer import guard).
"""
from __future__ import annotations

import os
import sys
import time


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "dev-init":
        from .devinit import dev_init
        for name, path in dev_init(argv[1]).items():
            print(f"{name}: {path}")
        return 0
    if argv not in (["serve"], ["enroll-code"]):
        print(__doc__)
        return 2
    from . import config
    from .app import build_app
    from .market import SimulatedMarketData
    from .service import SignerService
    cfg, market_file, host, port = config.load()
    if argv == ["enroll-code"]:
        # Only the store: never a second SignerService (that would run restart recovery on live requests).
        from .service import issue_enrollment_code
        from .store import ServiceStore
        os.makedirs(cfg.state_dir, exist_ok=True)
        store = ServiceStore(os.path.join(cfg.state_dir, "service.sqlite3"), time.time)
        path = os.path.join(cfg.state_dir, "enrollment-code.txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(issue_enrollment_code(store, time.time) + "\n")
        store.close()
        print(f"enrollment code written to {path} (valid 10 minutes, single use)")
        return 0
    service = SignerService(cfg, SimulatedMarketData.from_file(market_file, time.time), clock=time.time)
    import uvicorn  # inbound server only
    uvicorn.run(build_app(service), host=host, port=port, access_log=False, server_header=False,
                log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
