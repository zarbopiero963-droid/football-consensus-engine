from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.db import connect
from app.strategy_catalog import sample_reliability

VIRTUAL_STAKE_EUR = 1.0


@dataclass(frozen=True)
class MarketPick:
    market_id: str
    period: str
    selection: str
    probability: float | None
    source: str
    derivation: str = "SOURCE"
    line: float | None = None
    odds: float | None = None
    edge: float | None = None
    payload: dict[str, Any] | None = None


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _argmax(options: dict[str, float | None]) -> tuple[str, float] | None:
    valid = [(k, v) for k, v in options.items() if v is not None]
    if not valid:
        return None
    k, v = max(valid, key=lambda x: x[1])
    return k, float(v)


def project_from_1x2(view: dict[str, Any]) -> list[MarketPick]:
    p1, px, p2 = _f(view.get("p1")), _f(view.get("px")), _f(view.get("p2"))
    out: list[MarketPick] = []
    top = _argmax({"1": p1, "X": px, "2": p2})
    if top:
        sel, prob = top
        odds = _f((view.get("pinnacle_odds") or {}).get(sel))
        fair = _f((view.get("pinnacle_fair") or {}).get(sel))
        edge = None if fair is None else prob - fair
        out.append(MarketPick("FT_1X2", "FT", sel, prob, "footinsights", "SOURCE", odds=odds, edge=edge))
    if None not in (p1, px, p2):
        dc = {"1X": p1 + px, "X2": px + p2, "12": p1 + p2}
        sel, prob = max(dc.items(), key=lambda x: x[1])
        out.append(MarketPick("FT_DOUBLE_CHANCE", "FT", sel, prob, "footinsights", "DERIVED_FROM_1X2"))
    return out


def project_from_source_row(row: dict[str, Any]) -> list[MarketPick]:
    source = row["source"]
    out: list[MarketPick] = []
    p1, px, p2 = _f(row.get("p1")), _f(row.get("px")), _f(row.get("p2"))
    if None not in (p1, px, p2):
        scale = 100.0 if max(p1, px, p2) > 1.0 else 1.0
        probs = {"1": p1 / scale, "X": px / scale, "2": p2 / scale}
        sel, prob = max(probs.items(), key=lambda x: x[1])
        out.append(MarketPick("FT_1X2", "FT", sel, prob, source))
        dc = {"1X": probs["1"] + probs["X"], "X2": probs["X"] + probs["2"], "12": probs["1"] + probs["2"]}
        dsel, dprob = max(dc.items(), key=lambda x: x[1])
        out.append(MarketPick("FT_DOUBLE_CHANCE", "FT", dsel, dprob, source, "DERIVED_FROM_1X2"))

    over25, under25 = _f(row.get("over25")), _f(row.get("under25"))
    if over25 is not None or under25 is not None:
        scale = 100.0 if max(x for x in (over25, under25) if x is not None) > 1.0 else 1.0
        top = _argmax({"OVER": None if over25 is None else over25 / scale, "UNDER": None if under25 is None else under25 / scale})
        if top:
            out.append(MarketPick("FT_OU_2.5", "FT", top[0], top[1], source, line=2.5))

    yes, no = _f(row.get("btts_yes")), _f(row.get("btts_no"))
    if yes is not None or no is not None:
        scale = 100.0 if max(x for x in (yes, no) if x is not None) > 1.0 else 1.0
        top = _argmax({"GG": None if yes is None else yes / scale, "NG": None if no is None else no / scale})
        if top:
            out.append(MarketPick("FT_BTTS", "FT", top[0], top[1], source))

    cs = (row.get("correct_score") or "").strip()
    payload = dict(row.get("payload") or {})
    cs_prob = _f(payload.get("correct_score_probability"))
    if cs:
        if cs_prob is not None and cs_prob > 1:
            cs_prob /= 100.0
        out.append(MarketPick("FT_CORRECT_SCORE", "FT", cs, cs_prob, source))

    # Optional standardized fields for future adapters. They are never fabricated.
    optional = [
        ("HT_1X2", "HT", {"1": "ht_p1", "X": "ht_px", "2": "ht_p2"}, None),
        ("HT_OU_0.5", "HT", {"OVER": "ht_over05", "UNDER": "ht_under05"}, 0.5),
        ("HT_OU_1.5", "HT", {"OVER": "ht_over15", "UNDER": "ht_under15"}, 1.5),
        ("HT_BTTS", "HT", {"GG": "ht_btts_yes", "NG": "ht_btts_no"}, None),
    ]
    for market_id, period, mapping, line in optional:
        vals = {sel: _f(payload.get(key)) for sel, key in mapping.items()}
        if any(v is not None for v in vals.values()):
            scale = 100.0 if max(v for v in vals.values() if v is not None) > 1.0 else 1.0
            vals = {k: None if v is None else v / scale for k, v in vals.items()}
            top = _argmax(vals)
            if top:
                out.append(MarketPick(market_id, period, top[0], top[1], source, line=line))

    ht_cs = (payload.get("ht_correct_score") or "").strip()
    if ht_cs:
        hp = _f(payload.get("ht_correct_score_probability"))
        if hp is not None and hp > 1:
            hp /= 100.0
        out.append(MarketPick("HT_CORRECT_SCORE", "HT", ht_cs, hp, source))

    for prefix, market_id, period in [
        ("corners", "FT_CORNERS_OU", "FT"),
        ("ht_corners", "HT_CORNERS_OU", "HT"),
    ]:
        line = _f(payload.get(f"{prefix}_line"))
        over = _f(payload.get(f"{prefix}_over"))
        under = _f(payload.get(f"{prefix}_under"))
        if line is not None and (over is not None or under is not None):
            scale = 100.0 if max(x for x in (over, under) if x is not None) > 1.0 else 1.0
            top = _argmax({"OVER": None if over is None else over / scale, "UNDER": None if under is None else under / scale})
            if top:
                out.append(MarketPick(market_id, period, top[0], top[1], source, line=line))

    for item in payload.get("goal_lines") or []:
        line = _f(item.get("line"))
        prob = _f(item.get("probability"))
        sel = str(item.get("selection") or "").upper()
        if line is None or prob is None or sel not in {"OVER", "UNDER"}:
            continue
        if prob > 1:
            prob /= 100.0
        out.append(MarketPick(f"FT_OU_{line:g}", "FT", sel, prob, source, line=line))

    for item in payload.get("corner_lines") or []:
        line = _f(item.get("line"))
        prob = _f(item.get("probability"))
        sel = str(item.get("selection") or "").upper()
        if line is None or prob is None or sel not in {"OVER", "UNDER"}:
            continue
        if prob > 1:
            prob /= 100.0
        out.append(MarketPick(f"FT_CORNERS_OU_{line:g}", "FT", sel, prob, source, line=line))

    htft_sel = str(payload.get("htft_selection") or "")
    htft_prob = _f(payload.get("htft_probability"))
    if htft_sel and htft_prob is not None:
        if htft_prob > 1:
            htft_prob /= 100.0
        out.append(MarketPick("HTFT_1X2", "HTFT", htft_sel, htft_prob, source))

    return out



def required_result_fields(market_id: str) -> tuple[str, ...]:
    if market_id == "HTFT_1X2":
        return ("ht_home_goals", "ht_away_goals", "final_home_goals", "final_away_goals")
    if market_id.startswith("HT_CORNERS_OU"):
        return ("ht_corners_home", "ht_corners_away")
    if market_id.startswith("FT_CORNERS_OU"):
        return ("corners_home", "corners_away")
    if market_id.startswith("HT_"):
        return ("ht_home_goals", "ht_away_goals")
    if market_id.startswith("FT_"):
        return ("final_home_goals", "final_away_goals")
    return ()


def result_fields_verified(market_id: str, result: dict[str, Any]) -> bool:
    fields = required_result_fields(market_id)
    if not fields:
        return False
    verification = dict(result.get("field_verification") or {})
    return all(
        isinstance(verification.get(field), dict)
        and verification[field].get("status") == "VERIFIED"
        for field in fields
    )



def settle_market(market_id: str, selection: str, line: float | None, result: dict[str, Any]) -> tuple[str, bool] | None:
    fh = result.get("final_home_goals")
    fa = result.get("final_away_goals")
    hh = result.get("ht_home_goals")
    ha = result.get("ht_away_goals")
    ch = result.get("corners_home")
    ca = result.get("corners_away")
    hch = result.get("ht_corners_home")
    hca = result.get("ht_corners_away")

    def one_x_two(h, a):
        if h is None or a is None:
            return None
        return "1" if h > a else ("X" if h == a else "2")

    def ou(total, ln):
        if total is None or ln is None:
            return None
        if total == ln:
            return "PUSH"
        return "OVER" if total > ln else "UNDER"

    if market_id == "FT_1X2":
        actual = one_x_two(fh, fa)
    elif market_id == "FT_DOUBLE_CHANCE":
        r = one_x_two(fh, fa)
        if r is None:
            return None
        won = r in {"1", "X"} if selection == "1X" else (r in {"X", "2"} if selection == "X2" else r in {"1", "2"})
        return r, won
    elif market_id.startswith("FT_OU_"):
        actual = ou(None if fh is None or fa is None else fh + fa, line)
    elif market_id == "FT_BTTS":
        if fh is None or fa is None:
            return None
        actual = "GG" if fh > 0 and fa > 0 else "NG"
    elif market_id == "FT_CORRECT_SCORE":
        if fh is None or fa is None:
            return None
        actual = f"{fh}-{fa}"
    elif market_id == "HT_1X2":
        actual = one_x_two(hh, ha)
    elif market_id.startswith("HT_OU_"):
        actual = ou(None if hh is None or ha is None else hh + ha, line)
    elif market_id == "HT_BTTS":
        if hh is None or ha is None:
            return None
        actual = "GG" if hh > 0 and ha > 0 else "NG"
    elif market_id == "HT_CORRECT_SCORE":
        if hh is None or ha is None:
            return None
        actual = f"{hh}-{ha}"
    elif market_id.startswith("FT_CORNERS_OU"):
        actual = ou(None if ch is None or ca is None else ch + ca, line)
    elif market_id.startswith("HT_CORNERS_OU"):
        actual = ou(None if hch is None or hca is None else hch + hca, line)
    elif market_id == "HTFT_1X2":
        hr = one_x_two(hh, ha)
        fr = one_x_two(fh, fa)
        if hr is None or fr is None:
            return None
        actual = f"{hr}/{fr}"
    else:
        return None

    if actual is None:
        return None
    if actual == "PUSH":
        return actual, True
    return actual, actual == selection


def _ticket_id(batch_id: str, match_id: str, market_id: str, source: str) -> str:
    return hashlib.sha256(f"{batch_id}|{match_id}|{market_id}|{source}".encode()).hexdigest()[:32]


def freeze_market_tickets(batch_id: str, frozen_matches: list[dict[str, Any]], requested_at) -> int:
    """Freeze production market tickets from production snapshots only.

    Pilot/qualification tables are intentionally not read here. A source must
    be promoted through an explicit production eligibility path before it can
    contribute to user-visible market tickets.
    """
    count = 0
    with connect() as conn, conn.cursor() as cur:
        for view in frozen_matches:
            picks = project_from_1x2(view)

            unique = {}
            for pick in picks:
                unique[(pick.market_id, pick.source)] = pick

            for pick in unique.values():
                tid = _ticket_id(batch_id, view["match_id"], pick.market_id, pick.source)
                cur.execute("""
                    INSERT INTO market_decision_tickets(
                        ticket_id,batch_id,match_id,kickoff_utc,home_team,away_team,league,
                        source,market_id,period,selection,line,probability,odds,edge,
                        derivation,source_snapshot_utc,status,stake_eur,payload
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',1.00,%s::jsonb)
                    ON CONFLICT(batch_id,match_id,market_id,source) DO NOTHING
                """, (
                    tid,batch_id,view["match_id"],view["kickoff_utc"],view["home"],view["away"],view["league"],
                    pick.source,pick.market_id,pick.period,pick.selection,pick.line,pick.probability,pick.odds,
                    pick.edge,pick.derivation,view["snapshot_timestamp_utc"],json.dumps(pick.payload or {})
                ))
                count += 1
        conn.commit()
    return count


def synchronize_market_settlement() -> dict[str, int]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT mdt.*,
                   CASE WHEN rc.match_id IS NULL THEN NULL
                        ELSE jsonb_build_object(
                          'final_home_goals',rc.final_home_goals,
                          'final_away_goals',rc.final_away_goals,
                          'ht_home_goals',rc.ht_home_goals,
                          'ht_away_goals',rc.ht_away_goals,
                          'corners_home',rc.corners_home,
                          'corners_away',rc.corners_away,
                          'ht_corners_home',rc.ht_corners_home,
                          'ht_corners_away',rc.ht_corners_away,
                          'verification_status',rc.verification_status,
                          'field_verification',rc.field_verification
                        ) END AS result_payload
            FROM market_decision_tickets mdt
            LEFT JOIN result_consensus rc ON rc.match_id=mdt.match_id
        """)
        rows = list(cur.fetchall())
        updated = 0
        for row in rows:
            result = dict(row.get("result_payload") or {})
            if not result or not result_fields_verified(row["market_id"], result):
                if row.get("status") in {"WON", "LOST", "PUSH"}:
                    cur.execute("""
                        UPDATE market_decision_tickets
                        SET status='PENDING',result_value=NULL,correct=NULL,profit_eur=NULL,updated_at=NOW()
                        WHERE ticket_id=%s
                    """, (row["ticket_id"],))
                    updated += 1
                continue
            settled = settle_market(row["market_id"], row["selection"], row["line"], result)
            if settled is None:
                continue
            actual, correct = settled
            if actual == "PUSH":
                status, pnl = "PUSH", 0.0
            else:
                status = "WON" if correct else "LOST"
                odds = _f(row.get("odds"))
                pnl = None if odds is None else round((odds - 1.0) if correct else -1.0, 4)
            cur.execute("""
                UPDATE market_decision_tickets
                SET status=%s,result_value=%s,correct=%s,profit_eur=%s,updated_at=NOW()
                WHERE ticket_id=%s
            """, (status,actual,correct,pnl,row["ticket_id"]))
            updated += 1

        cur.execute("SELECT DISTINCT market_id,source FROM market_decision_tickets")
        for key in cur.fetchall():
            cur.execute("""
                SELECT
                  COUNT(*)::int signals_total,
                  COUNT(*) FILTER(WHERE status='PENDING')::int pending,
                  COUNT(*) FILTER(WHERE status IN('WON','LOST','PUSH'))::int settled,
                  COUNT(*) FILTER(WHERE status IN('WON','LOST') AND odds IS NOT NULL)::int priced_settled,
                  COUNT(*) FILTER(WHERE status='WON')::int wins,
                  COUNT(*) FILTER(WHERE status='LOST')::int losses,
                  COALESCE(SUM(stake_eur) FILTER(WHERE status IN('WON','LOST') AND odds IS NOT NULL),0) staked,
                  COALESCE(SUM(profit_eur) FILTER(WHERE status IN('WON','LOST') AND odds IS NOT NULL),0) profit
                FROM market_decision_tickets
                WHERE market_id=%s AND source=%s
            """, (key["market_id"],key["source"]))
            a = cur.fetchone()
            settled_n = int(a["settled"] or 0)
            wins = int(a["wins"] or 0)
            staked = float(a["staked"] or 0)
            profit = float(a["profit"] or 0)
            cur.execute("""
                INSERT INTO market_stats(
                    market_id,source,signals_total,pending,settled,priced_settled,
                    wins,losses,staked_eur,profit_eur,win_rate,roi,sample_reliability,updated_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT(market_id,source) DO UPDATE SET
                    signals_total=EXCLUDED.signals_total,pending=EXCLUDED.pending,
                    settled=EXCLUDED.settled,priced_settled=EXCLUDED.priced_settled,
                    wins=EXCLUDED.wins,losses=EXCLUDED.losses,staked_eur=EXCLUDED.staked_eur,
                    profit_eur=EXCLUDED.profit_eur,win_rate=EXCLUDED.win_rate,roi=EXCLUDED.roi,
                    sample_reliability=EXCLUDED.sample_reliability,updated_at=NOW()
            """, (
                key["market_id"],key["source"],int(a["signals_total"] or 0),int(a["pending"] or 0),
                settled_n,int(a["priced_settled"] or 0),wins,int(a["losses"] or 0),staked,profit,
                wins/settled_n if settled_n else None,profit/staked if staked else None,
                sample_reliability(settled_n)
            ))
        conn.commit()
    return {"tickets_seen": len(rows), "tickets_updated": updated}


def get_batch_markets(batch_id: str) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT ticket_id,match_id,kickoff_utc,home_team,away_team,league,source,
                   market_id,period,selection,line,probability,odds,edge,derivation,
                   status,result_value,correct,stake_eur,profit_eur
            FROM market_decision_tickets
            WHERE batch_id=%s
            ORDER BY kickoff_utc,home_team,market_id,source
        """, (batch_id,))
        return list(cur.fetchall())


def get_market_stats() -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM market_stats ORDER BY market_id,source")
        return list(cur.fetchall())
