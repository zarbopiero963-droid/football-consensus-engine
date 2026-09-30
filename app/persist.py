from __future__ import annotations

import json
import os
from typing import Iterable

from app.db import connect


def configured() -> bool:
    return bool(os.environ.get("DATABASE_URL", "").strip())


def required() -> bool:
    return os.environ.get("DATABASE_REQUIRED", "").strip().lower() in {"1", "true", "yes"}


def _b(v) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes"}


def _present(v) -> bool:
    return v is not None and str(v).strip() != ""


def _f(v):
    try:
        return float(v) if _present(v) else None
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(v) if _present(v) else None
    except (TypeError, ValueError):
        return None


def upsert_snapshots(rows: Iterable[dict]) -> int:
    items = list(rows)
    if not items or not configured():
        return 0
    inserted = 0
    with connect() as conn, conn.cursor() as cur:
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
                )
                ON CONFLICT (match_id,snapshot_timestamp_utc) DO NOTHING
                """,
                {
                    "match_id": r.get("match_id") or "",
                    "snapshot_timestamp_utc": r.get("snapshot_timestamp_utc"),
                    "kickoff_utc": r.get("kickoff_utc"),
                    "home_team": r.get("home_team") or "",
                    "away_team": r.get("away_team") or "",
                    "league": r.get("league") or "",
                    "source_match_id": r.get("source_match_id") or "",
                    "valid_prematch": _b(r.get("valid_prematch")),
                    "analysis_eligible": _b(r.get("analysis_eligible")),
                    "model_pick": r.get("model_pick") or "",
                    "model_pick_probability": _f(r.get("model_pick_probability")),
                    "picked_odds": _f(r.get("picked_odds")),
                    "picked_edge": _f(r.get("picked_edge")),
                    "pin_favorite": r.get("pin_favorite") or "",
                    "pin_favorite_odds": _f(r.get("pin_favorite_odds")),
                    "same_as_favorite": _b(r.get("same_as_favorite")),
                    "sha256_model_raw": r.get("sha256_model_raw") or "",
                    "sha256_odds_raw": r.get("sha256_odds_raw") or "",
                    "payload": json.dumps(r, separators=(",", ":")),
                },
            )
            inserted += 1
        conn.commit()
    return inserted


def upsert_result(row: dict) -> None:
    if not configured():
        return
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO match_results (
                match_id,version,status,result_1x2,final_home_goals,final_away_goals,
                result_timestamp_utc,result_source,payload
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT (match_id,version) DO NOTHING
            """,
            (
                row.get("match_id") or "",
                _i(row.get("version")) or 1,
                row.get("status") or "",
                row.get("result_1x2") or "",
                _i(row.get("final_home_goals")),
                _i(row.get("final_away_goals")),
                row.get("result_timestamp_utc") or None,
                row.get("result_source") or "",
                json.dumps(row, separators=(",", ":")),
            ),
        )
        conn.commit()


def set_runtime_state(key: str, value: dict) -> None:
    if not configured():
        return
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO runtime_state(key,value,updated_at)
            VALUES (%s,%s::jsonb,NOW())
            ON CONFLICT (key)
            DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
            """,
            (key, json.dumps(value, separators=(",", ":"))),
        )
        conn.commit()


def guarded(operation, *args):
    """Run persistence only when configured.

    When DATABASE_REQUIRED=1, a database error is fatal. Otherwise it is returned
    to the caller so local append-only files remain usable during development.
    """
    if not configured():
        return {"configured": False, "ok": True, "result": None, "error": None}
    try:
        return {"configured": True, "ok": True, "result": operation(*args), "error": None}
    except Exception as exc:
        if required():
            raise
        return {"configured": True, "ok": False, "result": None, "error": f"{type(exc).__name__}: {exc}"}
