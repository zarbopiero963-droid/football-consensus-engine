from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import connect
from app.timezones import to_rome_iso
from app.strategy_sync import get_strategy_history, get_strategy_stats, synchronize_strategy_simulations
from app.market_engine import freeze_market_tickets, get_batch_markets, get_market_stats

ROME = ZoneInfo("Europe/Rome")
app = FastAPI(title="Football Consensus Engine", version="0.2.0")


class DecisionRequest(BaseModel):
    label: str = ""
    mode: str = Field(default="all", pattern="^(all|bet|signals)$")
    strategies: list[str] = Field(default_factory=list)


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def strategy_ids(payload: dict) -> list[str]:
    p = _num(payload.get("model_pick_probability"))
    edge = _num(payload.get("picked_edge"))
    odds = _num(payload.get("picked_odds"))
    eligible = str(payload.get("analysis_eligible")) == "1"
    same = str(payload.get("same_as_favorite")) == "1"
    priced = eligible and odds is not None
    if p is None:
        return []
    out: list[str] = []
    if p >= 0.50: out.append("S1")
    if p >= 0.60: out.append("S2")
    if priced and same and p >= 0.50: out.append("S3")
    if priced and same and p >= 0.60: out.append("S4")
    if priced and edge is not None and p >= 0.50 and edge > 0: out.append("S5")
    if priced and edge is not None and p >= 0.60 and edge > 0: out.append("S6")
    if priced and edge is not None and edge >= 0.03: out.append("S7")
    if priced and edge is not None and edge >= 0.05: out.append("S8")
    if priced and edge is not None and edge >= 0.08: out.append("S9")
    return out


def latest_snapshots_for_day(
    day: date,
    *,
    as_of: datetime | None = None,
    future_only: bool = False,
) -> list[dict]:
    clauses = [
        "valid_prematch = TRUE",
        "(kickoff_utc AT TIME ZONE 'Europe/Rome')::date = %s",
        "snapshot_timestamp_utc < kickoff_utc",
    ]
    params: list[object] = [day]
    if as_of is not None:
        clauses.append("snapshot_timestamp_utc <= %s")
        params.append(as_of)
        if future_only:
            clauses.append("kickoff_utc > %s")
            params.append(as_of)

    sql = f"""
    SELECT DISTINCT ON (match_id)
        match_id, kickoff_utc, snapshot_timestamp_utc, home_team, away_team, league, payload
    FROM model_snapshots
    WHERE {' AND '.join(clauses)}
    ORDER BY match_id, snapshot_timestamp_utc DESC
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return list(cur.fetchall())


def latest_results(match_ids: list[str]) -> dict[str, dict]:
    if not match_ids:
        return {}
    sql = """
    SELECT DISTINCT ON (match_id) match_id, version, payload
    FROM match_results
    WHERE match_id = ANY(%s)
    ORDER BY match_id, version DESC
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (match_ids,))
        return {r["match_id"]: r for r in cur.fetchall()}


def view_snapshot(row: dict) -> dict:
    p = dict(row["payload"])
    strategies = strategy_ids(p)
    return {
        "match_id": row["match_id"],
        "kickoff_utc": row["kickoff_utc"].isoformat(),
        "kickoff_italy": to_rome_iso(row["kickoff_utc"]),
        "snapshot_timestamp_utc": row["snapshot_timestamp_utc"].isoformat(),
        "snapshot_timestamp_italy": to_rome_iso(row["snapshot_timestamp_utc"]),
        "league": row["league"],
        "home": row["home_team"],
        "away": row["away_team"],
        "model_pick": p.get("model_pick"),
        "model_probability": _num(p.get("model_pick_probability")),
        "p1": _num(p.get("p_home_normalized")),
        "px": _num(p.get("p_draw_normalized")),
        "p2": _num(p.get("p_away_normalized")),
        "pinnacle_odds": {
            "1": _num(p.get("odds_home")),
            "X": _num(p.get("odds_draw")),
            "2": _num(p.get("odds_away")),
        },
        "pinnacle_fair": {
            "1": _num(p.get("fair_home")),
            "X": _num(p.get("fair_draw")),
            "2": _num(p.get("fair_away")),
        },
        "picked_odds": _num(p.get("picked_odds")),
        "picked_edge": _num(p.get("picked_edge")),
        "strategies": strategies,
        "status": "BET" if strategies else "NO_BET",
    }


def apply_decision_filter(matches: list[dict], mode: str, strategy_filter: list[str]) -> list[dict]:
    wanted = {s.upper() for s in strategy_filter}
    if mode == "all":
        return matches
    if mode == "bet":
        return [m for m in matches if m["status"] == "BET"]
    if mode == "signals":
        if wanted:
            return [m for m in matches if wanted.intersection(m["strategies"])]
        return [m for m in matches if m["strategies"]]
    raise ValueError("invalid decision mode")


def frozen_snapshot_view(match: dict) -> dict:
    # Force a deep JSON copy so the object written into decision_tickets is
    # exactly the object returned to the caller and cannot share mutable state.
    return json.loads(json.dumps(match, sort_keys=True))


def day_response(day: date, include_results: bool) -> dict:
    rows = latest_snapshots_for_day(day)
    matches = [view_snapshot(r) for r in rows]
    if include_results:
        result_map = latest_results([m["match_id"] for m in matches])
        for m in matches:
            r = result_map.get(m["match_id"])
            m["result"] = dict(r["payload"]) if r else None
            m["settlement_status"] = "SETTLED" if r else "PENDING"
    return {
        "date": day.isoformat(),
        "timezone": "Europe/Rome",
        "snapshot_role": "monitoring",
        "matches": matches,
        "count": len(matches),
    }


def create_decision_batch(day: date, req: DecisionRequest, requested_at: datetime | None = None) -> dict:
    requested_at = requested_at or datetime.now(timezone.utc)
    rows = latest_snapshots_for_day(day, as_of=requested_at, future_only=True)
    matches = apply_decision_filter([view_snapshot(r) for r in rows], req.mode, req.strategies)
    frozen = [frozen_snapshot_view(m) for m in matches]
    batch_id = f"decision-{requested_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:10]}"

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO decision_batches
                (batch_id,requested_at_utc,requested_date,timezone,mode,strategy_filter,label)
            VALUES (%s,%s,%s,'Europe/Rome',%s,%s::jsonb,%s)
            """,
            (batch_id, requested_at, day, req.mode, json.dumps(req.strategies), req.label),
        )
        for m in frozen:
            cur.execute(
                """
                INSERT INTO decision_tickets (
                    batch_id,match_id,snapshot_timestamp_utc,kickoff_utc,
                    home_team,away_team,league,model_pick,model_probability,
                    picked_odds,picked_edge,strategies,display_status,snapshot_view,shown_to_user
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,TRUE
                )
                """,
                (
                    batch_id,
                    m["match_id"],
                    m["snapshot_timestamp_utc"],
                    m["kickoff_utc"],
                    m["home"],
                    m["away"],
                    m["league"],
                    m.get("model_pick") or "",
                    m.get("model_probability"),
                    m.get("picked_odds"),
                    m.get("picked_edge"),
                    json.dumps(m.get("strategies", [])),
                    m.get("status", "NO_BET"),
                    json.dumps(m, separators=(",", ":")),
                ),
            )
        conn.commit()

    synchronize_strategy_simulations()
    freeze_market_tickets(batch_id, frozen, requested_at)

    return {
        "batch_id": batch_id,
        "requested_at_utc": requested_at.isoformat(),
        "requested_at_italy": to_rome_iso(requested_at),
        "date": day.isoformat(),
        "timezone": "Europe/Rome",
        "mode": req.mode,
        "strategy_filter": req.strategies,
        "label": req.label,
        "matches": frozen,
        "count": len(frozen),
        "contract": "what_you_saw_is_what_gets_settled",
        "markets": get_batch_markets(batch_id),
    }


def _settle_ticket(ticket: dict, result: dict | None) -> dict:
    out = dict(ticket)
    if not result:
        out["settlement_status"] = "PENDING"
        out["result"] = None
        out["correct"] = None
        out["profit_flat_1"] = None
        return out

    result_payload = dict(result["payload"])
    result_ts = result_payload.get("result_timestamp_utc")
    if result_ts:
        try:
            parsed_result_ts = datetime.fromisoformat(str(result_ts).replace("Z", "+00:00"))
            result_payload["result_timestamp_italy"] = to_rome_iso(parsed_result_ts)
        except ValueError:
            result_payload["result_timestamp_italy"] = None
    real = result_payload.get("result_1x2") or result_payload.get("result")
    pick = out.get("model_pick")
    correct = bool(real and pick and real == pick)
    odds = _num(out.get("picked_odds"))
    out["settlement_status"] = "SETTLED"
    out["result"] = result_payload
    out["correct"] = correct
    out["profit_flat_1"] = (odds - 1.0) if (correct and odds is not None) else (-1.0 if odds is not None else None)
    return out


def decision_batch_view(batch_id: str) -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM decision_batches WHERE batch_id=%s", (batch_id,))
        batch = cur.fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="decision batch not found")
        cur.execute(
            """
            SELECT match_id,snapshot_view
            FROM decision_tickets
            WHERE batch_id=%s AND shown_to_user=TRUE
            ORDER BY kickoff_utc, league, home_team
            """,
            (batch_id,),
        )
        tickets = list(cur.fetchall())

    result_map = latest_results([t["match_id"] for t in tickets])
    settled = [_settle_ticket(dict(t["snapshot_view"]), result_map.get(t["match_id"])) for t in tickets]
    return {
        "batch_id": batch["batch_id"],
        "requested_at_utc": batch["requested_at_utc"].isoformat(),
        "requested_at_italy": to_rome_iso(batch["requested_at_utc"]),
        "date": batch["requested_date"].isoformat(),
        "timezone": batch["timezone"],
        "mode": batch["mode"],
        "strategy_filter": list(batch["strategy_filter"]),
        "label": batch["label"],
        "matches": settled,
        "count": len(settled),
        "contract": "what_you_saw_is_what_gets_settled",
    }


def decision_report_for_day(day: date) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT batch_id FROM decision_batches
            WHERE requested_date=%s
            ORDER BY requested_at_utc
            """,
            (day,),
        )
        ids = [r["batch_id"] for r in cur.fetchall()]
    return [decision_batch_view(batch_id) for batch_id in ids]


@app.get("/healthz")
def healthz():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        row = cur.fetchone()
    return {"ok": bool(row and row["ok"] == 1)}


@app.get("/today")
def today():
    return day_response(datetime.now(ROME).date(), include_results=False)


@app.get("/yesterday")
def yesterday():
    day = datetime.now(ROME).date() - timedelta(days=1)
    batches = decision_report_for_day(day)
    if batches:
        return {
            "date": day.isoformat(),
            "timezone": "Europe/Rome",
            "report_mode": "decision_tickets",
            "batches": batches,
            "count": sum(b["count"] for b in batches),
        }

    fallback = day_response(day, include_results=True)
    fallback["report_mode"] = "monitoring_only"
    fallback["warning"] = "No decision ticket was shown/saved for this date; these are monitoring snapshots only."
    return fallback


@app.get("/date/{day}")
def by_date(day: str, results: bool = Query(default=True), decisions: bool = Query(default=False)):
    try:
        parsed = date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
    if decisions:
        return {
            "date": parsed.isoformat(),
            "timezone": "Europe/Rome",
            "report_mode": "decision_tickets",
            "batches": decision_report_for_day(parsed),
        }
    return day_response(parsed, include_results=results)


@app.post("/decisions/today")
def freeze_today(req: DecisionRequest):
    return create_decision_batch(datetime.now(ROME).date(), req)


@app.post("/decisions/date/{day}")
def freeze_decisions(day: str, req: DecisionRequest):
    try:
        parsed = date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
    return create_decision_batch(parsed, req)


@app.get("/decisions/{batch_id}")
def get_decision_batch(batch_id: str):
    return decision_batch_view(batch_id)


@app.get("/strategies")
def strategies_overview():
    rows = get_strategy_stats()
    return {
        "stake_model": "virtual_flat",
        "stake_eur": 1.0,
        "timezone": "Europe/Rome",
        "strategies": rows,
        "count": len(rows),
    }


@app.get("/strategies/{strategy_id}")
def strategy_detail(strategy_id: str, limit: int = Query(default=100, ge=1, le=500)):
    sid = strategy_id.upper()
    rows = get_strategy_stats()
    meta = next((r for r in rows if r["strategy_id"] == sid), None)
    if meta is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    history = get_strategy_history(sid, limit)
    for item in history:
        item["kickoff_italy"] = to_rome_iso(item["kickoff_utc"])
    return {
        "stake_model": "virtual_flat",
        "stake_eur": 1.0,
        "timezone": "Europe/Rome",
        "strategy": meta,
        "history": history,
        "history_count": len(history),
    }


@app.get("/decisions/{batch_id}/markets")
def decision_market_view(batch_id: str):
    rows = get_batch_markets(batch_id)
    for item in rows:
        item["kickoff_italy"] = to_rome_iso(item["kickoff_utc"])
    return {
        "batch_id": batch_id,
        "timezone": "Europe/Rome",
        "stake_model": "virtual_flat",
        "stake_eur": 1.0,
        "markets": rows,
        "count": len(rows),
    }


@app.get("/markets/stats")
def market_stats():
    return {
        "stake_model": "virtual_flat",
        "stake_eur": 1.0,
        "stats": get_market_stats(),
    }


@app.get("/results/consensus")
def result_consensus(limit: int = Query(default=200, ge=1, le=1000)):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT match_id,final_home_goals,final_away_goals,ht_home_goals,ht_away_goals,
                   corners_home,corners_away,ht_corners_home,ht_corners_away,result_1x2,
                   verification_status,field_verification,providers,updated_at
            FROM result_consensus ORDER BY updated_at DESC LIMIT %s
        """, (limit,))
        rows=list(cur.fetchall())
    return {"results": rows, "count": len(rows)}
