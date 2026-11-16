"""SX Bet TORONTO (testnet) certification, S8.2 (issue #187; governance §2.6). Owner-run workflow only.

Today this script only runs the PREFLIGHT. It sends no order, makes no signature, reads no private key and
needs no secret. It checks:
- the PUBLIC GET https://api.toronto.sx.bet/metadata/obv3 against the pinned Toronto domain
  (signer/toronto_sender.verify_toronto_metadata: chain 79479957, "OBv3 Escrow"/"1", escrow, USDC), with no
  redirects followed;
- the GitHub Environment `sxbet-toronto` VARIABLES (public values, not secrets):
  - SXBET_TORONTO_MAKER_ADDRESS: the dedicated testnet EOA;
  - SXBET_LIVE_MAKER_ADDRESS: the owner's live maker (§2.6: it must differ from the testnet one);
  - SXBET_OWNER_ED25519_PUBLIC_KEY_HEX;
  - SXBET_TORONTO_RISK_CONFIG_B64: owner-signed risk config v2 with a "testnet" block.
  A missing variable is NOT_CONFIGURED, never a pass.

`--send` exists so the workflow's send job has a fixed entry point. It ALWAYS refuses with
SEND_PATH_NOT_ENABLED_PENDING_OWNER_GO: no testnet order is authorized, and the send scenarios (Market, Limit,
cancel, partial fill, timeout, restart, reconciliation) are implemented only after the owner's GO and test funds.

The report holds outcomes and fixed codes only: no address and no key.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
from typing import Any, Callable, Mapping

TORONTO_METADATA_URL = "https://api.toronto.sx.bet/metadata/obv3"
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX32 = re.compile(r"^[0-9a-fA-F]{64}$")


def check_metadata(body: Any) -> dict[str, Any]:
    from signer.toronto_sender import TorontoGuardError, verify_toronto_metadata
    if not isinstance(body, Mapping) or body.get("status") != "success":
        return {"outcome": "FAIL", "reason": "METADATA_REPLY_NOT_SUCCESS"}
    try:
        verify_toronto_metadata(body.get("data"))
    except TorontoGuardError as exc:
        return {"outcome": "FAIL", "reason": exc.code}
    return {"outcome": "PASS", "reason": "TORONTO_DOMAIN_MATCHES_PINNED_VALUES"}


def check_makers(env: Mapping[str, str]) -> dict[str, Any]:
    maker, live = env.get("SXBET_TORONTO_MAKER_ADDRESS", "").strip(), env.get("SXBET_LIVE_MAKER_ADDRESS", "").strip()
    if not maker or not live:
        return {"outcome": "NOT_CONFIGURED", "reason": "MAKER_VARIABLES_MISSING"}
    if not _ADDRESS.fullmatch(maker) or not _ADDRESS.fullmatch(live):
        return {"outcome": "FAIL", "reason": "MAKER_ADDRESS_MALFORMED"}
    if maker.lower() == live.lower():
        return {"outcome": "FAIL", "reason": "TESTNET_MAKER_IS_THE_LIVE_MAKER"}       # §2.6
    from signer.toronto_sender import MAINNET_MARKERS
    if maker.lower() in MAINNET_MARKERS:
        return {"outcome": "FAIL", "reason": "MAKER_IS_A_MAINNET_CONTRACT"}
    from signer.toronto_sender import TORONTO
    if int(maker, 16) == 0 or maker.lower() in (TORONTO["usdc"], TORONTO["escrow"]):
        return {"outcome": "FAIL", "reason": "MAKER_IS_NOT_AN_EOA"}                     # zero or a contract
    return {"outcome": "PASS", "reason": "DEDICATED_TESTNET_MAKER"}


def check_risk_config(env: Mapping[str, str]) -> dict[str, Any]:
    pub_hex = env.get("SXBET_OWNER_ED25519_PUBLIC_KEY_HEX", "").strip()
    cfg_b64 = env.get("SXBET_TORONTO_RISK_CONFIG_B64", "").strip()
    if not pub_hex or not cfg_b64:
        return {"outcome": "NOT_CONFIGURED", "reason": "RISK_CONFIG_VARIABLES_MISSING"}
    if not _HEX32.fullmatch(pub_hex):
        return {"outcome": "FAIL", "reason": "OWNER_PUBLIC_KEY_MALFORMED"}
    try:
        raw = base64.b64decode(cfg_b64, validate=True)
    except ValueError:
        return {"outcome": "FAIL", "reason": "RISK_CONFIG_NOT_BASE64"}
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    owner = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
    source, version = load_risk_config_bytes(raw, owner, "testnet")
    if source != "OWNER_SIGNED":
        return {"outcome": "FAIL", "reason": "RISK_CONFIG_" + source}
    if load_risk_config_bytes(raw, owner, "mainnet")[0] == "OWNER_SIGNED":
        # The Toronto config must not also enable mainnet limits (a testnet-only document).
        return {"outcome": "FAIL", "reason": "RISK_CONFIG_ALSO_CONFIGURES_MAINNET"}
    return {"outcome": "PASS", "reason": "OWNER_SIGNED_TESTNET_ONLY", "config_version": version}


def load_risk_config_bytes(raw: bytes, owner: Any, network: str) -> tuple[str, int | None]:
    """The signer's own loader (signature + schema), on a private temporary copy of the variable."""
    from signer.risk import load_risk_config
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "risk.json")
        with open(path, "wb") as fh:
            fh.write(raw)
        loaded = load_risk_config(path, owner, network=network)
        return loaded.source, loaded.config_version


def preflight(env: Mapping[str, str], fetch: Callable[[str], Any]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    try:
        results["toronto_metadata"] = check_metadata(fetch(TORONTO_METADATA_URL))
    except Exception as exc:  # type only
        results["toronto_metadata"] = {"outcome": "FAIL", "reason": "METADATA_UNREACHABLE",
                                       "error_type": type(exc).__name__}
    results["makers"] = check_makers(env)
    results["risk_config"] = check_risk_config(env)
    outcomes = [r["outcome"] for r in results.values()]
    return {"suite": "sxbet_toronto_preflight", "network": "testnet", "orders_sent": 0, "signatures": 0,
            "ready_for_send": all(o == "PASS" for o in outcomes), "results": results}


def _fetch(url: str) -> Any:
    import httpx
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        r = client.get(url, headers={"Accept": "application/json"})
        if r.status_code != 200:
            return {"status": "http_" + str(r.status_code)}
        return r.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args()
    if args.send:
        print(json.dumps({"refused": "SEND_PATH_NOT_ENABLED_PENDING_OWNER_GO"}))
        return 3
    report = preflight(os.environ, _fetch)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    print(json.dumps({k: report[k] for k in ("ready_for_send", "orders_sent")}))
    return exit_code(report)


def exit_code(report: dict) -> int:
    """0 ONLY when every check PASSed (ready_for_send). 1 on any FAIL, 2 when something is NOT_CONFIGURED.
    A non-ready preflight must never let a dependent job run (fail closed)."""
    if report.get("ready_for_send") is True and all(
            r.get("outcome") == "PASS" for r in report["results"].values()):
        return 0
    if any(r.get("outcome") == "FAIL" for r in report["results"].values()):
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
