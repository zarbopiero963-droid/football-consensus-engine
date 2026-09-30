#!/usr/bin/env python3
from __future__ import annotations

import json

from app.result_validation import rebuild_all, sync_fifa_from_match_results


def main():
    fifa_rows = sync_fifa_from_match_results()
    consensus = rebuild_all()
    print(json.dumps({
        "fifa_rows": fifa_rows,
        "consensus": {
            "matches": len(consensus),
            "verified": sum(x["status"] == "VERIFIED" for x in consensus),
            "single_source": sum(x["status"] == "SINGLE_SOURCE" for x in consensus),
            "conflicts": sum(x["status"] == "CONFLICT" for x in consensus),
        },
    }, default=str))


if __name__ == "__main__":
    main()
