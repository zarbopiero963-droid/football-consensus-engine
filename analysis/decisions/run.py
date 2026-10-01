"""Explicit diagnostic reporting or prospective protocol registration."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from analysis.decisions.protocol import Protocol, register
from analysis.decisions.data import load_dataset
from analysis.decisions.report import report


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--protocol-id')
    parser.add_argument('--known-at')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--register', action='store_true')
    action.add_argument('--neon', action='store_true')
    action.add_argument('--input')
    args = parser.parse_args()
    protocol = Protocol.from_dict(json.loads(Path(args.policy).read_text()))
    if args.register:
        if not args.protocol_id or args.known_at: parser.error('registration requires --protocol-id and server time')
        result = register(args.protocol_id,protocol)
    else:
        known = args.known_at or datetime.now(timezone.utc).isoformat()
        if args.neon:
            dataset, registration = load_dataset(known_at=known,protocol=protocol,protocol_id=args.protocol_id)
        else:
            # Files are diagnostics only: user-supplied registrations are not trusted.
            if args.protocol_id: parser.error('--protocol-id requires --neon registry read')
            dataset = json.loads(Path(args.input).read_text()); registration = None
        result = report(dataset,known_at=known,protocol=protocol,registration=registration)
    print(json.dumps(result, sort_keys=True, indent=2, default=str, allow_nan=False))


if __name__ == '__main__': main()
