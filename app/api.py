from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query

from app.db import connect

ROME = ZoneInfo("Europe/Rome")
app = FastAPI(title="Football Consensus Engine", version="0.1.0")


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


def latest_snapshots_for_day(day: date) -> list[dict]:
    sql = """
    SELECT DISTINCT ON (match_id)
        match_id, kickoff_utc, snapshot_timestamp_utc, home_team, away_team, league, payload
    FROM model_snapshots
    WHERE valid_prematch = TRUE
      AND (kickoff_utc AT TIME ZONE 'Europe/Rome')::date = %s
      AND snapshot_timestamp_utc < kickoff_utc
    ORDER BY match_id, snapshot_timestamp_utc DESC
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (day,))
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
        "snapshot_timestamp_utc": row["snapshot_timestamp_utc"].isoformat(),
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
        "matches": matches,
        "count": len(matches),
    }


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
    return day_response(day, include_results=True)


@app.get("/date/{day}")
def by_date(day: str, results: bool = Query(default=True)):
    try:
        parsed = date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
    return day_response(parsed, include_results=results)
