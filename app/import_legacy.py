from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Iterable

from app.db import connect, migrate

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "prospective" / "prospective_ledger.csv"
RESULTS = ROOT / "prospective" / "normalized" / "results.csv"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def b(v: str | None) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes"}


def f(v: str | None):
    try:
        return float(v) if str(v or "").strip() else None
    except ValueError:
        return None


def i(v: str | None):
    try:
        return int(v) if str(v or "").strip() else None
    except ValueError:
        return None


def rows(path: Path) -> Iterable[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def import_ledger(conn) -> tuple[int, int]:
    if not LEDGER.exists():
        return 0, 0
    digest = sha256(LEDGER)
    items = list(rows(LEDGER))
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM ingest_runs WHERE source=%s AND source_sha256=%s", ("ledger", digest))
        if cur.fetchone():
            return len(items), 0
        inserted = 0
        for r in items:
            cur.execute(
                """
                INSERT INTO model_snapshots (
                    match_id,snapshot_timestamp_utc,kickoff_utc,home_team,away_team,league,
                    source_match_id,valid_prematch,analysis_eligible,model_pick,model_pick_probability,
                    picked_odds,picked_edge,pin_favorite,pin_favorite_odds,same_as_favorite,
                    sha256_model_raw,sha256_odds_raw,payload
                ) VALUES (
                    %(match_id)s,%(snapshot_timestamp_utc)s,%(kickoff_utc)s,%(home_team)s,%(away_team)s,%(league)s,
                    %(source_match_id)s,%(valid_prematch)s,%(analysis_eligible)s,%(model_pick)s,%(model_pick_probability)s,
                    %(picked_odds)s,%(picked_edge)s,%(pin_favorite)s,%(pin_favorite_odds)s,%(same_as_favorite)s,
                    %(sha256_model_raw)s,%(sha256_odds_raw)s,%(payload)s::jsonb
                ) ON CONFLICT (match_id,snapshot_timestamp_utc) DO NOTHING
                """,
                {
                    "match_id": r.get("match_id") or "",
                    "snapshot_timestamp_utc": r.get("snapshot_timestamp_utc"),
                    "kickoff_utc": r.get("kickoff_utc"),
                    "home_team": r.get("home_team") or "",
                    "away_team": r.get("away_team") or "",
                    "league": r.get("league") or "",
                    "source_match_id": r.get("source_match_id") or "",
                    "valid_prematch": b(r.get("valid_prematch")),
                    "analysis_eligible": b(r.get("analysis_eligible")),
                    "model_pick": r.get("model_pick") or "",
                    "model_pick_probability": f(r.get("model_pick_probability")),
                    "picked_odds": f(r.get("picked_odds")),
                    "picked_edge": f(r.get("picked_edge")),
                    "pin_favorite": r.get("pin_favorite") or "",
                    "pin_favorite_odds": f(r.get("pin_favorite_odds")),
                    "same_as_favorite": b(r.get("same_as_favorite")),
                    "sha256_model_raw": r.get("sha256_model_raw") or "",
                    "sha256_odds_raw": r.get("sha256_odds_raw") or "",
                    "payload": json.dumps(r, separators=(",", ":")),
                },
            )
            inserted += cur.rowcount
        cur.execute(
            "INSERT INTO ingest_runs(source,source_sha256,rows_seen,rows_inserted) VALUES (%s,%s,%s,%s)",
            ("ledger", digest, len(items), inserted),
        )
    return len(items), inserted


def import_results(conn) -> tuple[int, int]:
    if not RESULTS.exists():
        return 0, 0
    digest = sha256(RESULTS)
    items = list(rows(RESULTS))
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM ingest_runs WHERE source=%s AND source_sha256=%s", ("results", digest))
        if cur.fetchone():
            return len(items), 0
        inserted = 0
        for r in items:
            cur.execute(
                """
                INSERT INTO match_results (
                    match_id,version,status,result_1x2,final_home_goals,final_away_goals,
                    result_timestamp_utc,result_source,payload
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (match_id,version) DO NOTHING
                """,
                (
                    r.get("match_id") or "", i(r.get("version")) or 1, r.get("status") or "",
                    r.get("result_1x2") or "", i(r.get("final_home_goals")), i(r.get("final_away_goals")),
                    r.get("result_timestamp_utc") or None, r.get("result_source") or "",
                    json.dumps(r, separators=(",", ":")),
                ),
            )
            inserted += cur.rowcount
        cur.execute(
            "INSERT INTO ingest_runs(source,source_sha256,rows_seen,rows_inserted) VALUES (%s,%s,%s,%s)",
            ("results", digest, len(items), inserted),
        )
    return len(items), inserted


def main() -> None:
    migrate()
    with connect() as conn:
        l_seen, l_new = import_ledger(conn)
        r_seen, r_new = import_results(conn)
        conn.commit()
    print(json.dumps({
        "ledger": {"seen": l_seen, "inserted": l_new},
        "results": {"seen": r_seen, "inserted": r_new},
    }, indent=2))


if __name__ == "__main__":
    main()
