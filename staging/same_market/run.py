"""Replay an explicit saved capture; stdout only, no DB or browser writes."""
import argparse
import json
from pathlib import Path

from staging.same_market.pinnacle import normalize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--known-at', required=True)
    args = parser.parse_args()
    report = normalize(json.loads(args.input.read_text()), known_at=args.known_at)
    print(json.dumps(report, sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
