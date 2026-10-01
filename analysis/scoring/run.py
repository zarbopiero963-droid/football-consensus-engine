import argparse
import json
from pathlib import Path

from analysis.scoring.data import load_dataset
from analysis.scoring.metrics import ScorePolicy
from analysis.scoring.report import report


def main():
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--neon',action='store_true'); source.add_argument('--input',type=Path)
    parser.add_argument('--known-at',required=True)
    parser.add_argument('--methods',nargs='+',required=True)
    parser.add_argument('--policy',type=Path,required=True)
    args = parser.parse_args()
    config = json.loads(args.policy.read_text()); config['bin_edges'] = tuple(config['bin_edges'])
    policy = ScorePolicy(**config)
    dataset = load_dataset(known_at=args.known_at) if args.neon else json.loads(args.input.read_text())
    print(json.dumps(report(dataset,known_at=args.known_at,methods=args.methods,policy=policy),
                     sort_keys=True,allow_nan=False))


if __name__ == '__main__': main()
