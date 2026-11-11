"""Owner helper (run LOCALLY, never in CI): the key hash of one client address for the emergency
unlock workflow (target=key). The throttle secret is read with getpass (no echo, never argv,
never written); the address is the only argument. Prints only '<key id>:<64 hex>'.

    python3 scripts/oauth_throttle_key_hash.py 203.0.113.5
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.oauth_persistent_throttle import _parse_key, key_hash  # noqa: E402


def main(argv: list[str], read_secret=getpass.getpass) -> int:
    if len(argv) != 2:
        print("usage: oauth_throttle_key_hash.py <client address>", file=sys.stderr)
        return 2
    parsed = _parse_key(read_secret("MCP_OAUTH_THROTTLE_KEY (<key id>:<secret>): "))
    if parsed is None:
        print("invalid key format (expected <key id>:<secret of at least 32 characters>)", file=sys.stderr)
        return 1
    print(key_hash(parsed[0], parsed[1], argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
