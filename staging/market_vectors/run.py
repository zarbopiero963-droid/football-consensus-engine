"""Stdout-only native normalization; no migration, cron or ticket calls."""
import argparse
import json
from pathlib import Path

from staging.market_vectors.data import load_dataset
from staging.market_vectors.native import normalize


def main():
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--neon', action='store_true')
    source.add_argument('--input', type=Path)
    parser.add_argument('--known-at', required=True)
    args = parser.parse_args()
    dataset = load_dataset(known_at=args.known_at) if args.neon else json.loads(args.input.read_text())
    print(json.dumps(normalize(dataset, known_at=args.known_at), allow_nan=False, sort_keys=True))


if __name__ == '__main__':
    main()
