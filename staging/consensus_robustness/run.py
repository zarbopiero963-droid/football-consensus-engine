"""python -m staging.consensus_robustness.run --neon | --input path.json"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from staging.consensus_robustness.data import load_rows
from staging.consensus_robustness.harness import report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--neon', action='store_true')
    inputs.add_argument('--input', type=Path)
    parser.add_argument('--known-at', default=datetime.now(timezone.utc).isoformat())
    parser.add_argument('--source-roles', type=Path, help='Explicit research classification JSON; no promotion')
    args = parser.parse_args()
    roles = json.loads(args.source_roles.read_text()) if args.source_roles else None
    rows = load_rows(source_roles=roles) if args.neon else json.loads(args.input.read_text())
    print(json.dumps(report(rows, known_at=args.known_at), indent=2, allow_nan=False, sort_keys=True))


if __name__ == '__main__':
    main()
