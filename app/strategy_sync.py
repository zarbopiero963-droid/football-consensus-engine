from __future__ import annotations

from app.db import connect
from app.strategy_catalog import STRATEGY_CATALOG, VIRTUAL_STAKE_EUR, sample_reliability


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _profit(odds, won):
    if odds is None:
        return None
    return round((odds - 1.0) if won else -1.0, 4)


def synchronize_strategy_simulations():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT dt.batch_id,dt.match_id,dt.kickoff_utc,dt.home_team,dt.away_team,
                   dt.strategies,dt.snapshot_view,dt.snapshot_timestamp_utc,
                   CASE
                     WHEN rc.field_verification->'final_home_goals'->>'status'='VERIFIED'
                      AND rc.field_verification->'final_away_goals'->>'status'='VERIFIED'
                     THEN rc.result_1x2
                     ELSE NULL
                   END AS result_1x2
            FROM decision_tickets dt
            LEFT JOIN result_consensus rc ON rc.match_id=dt.match_id
            WHERE dt.shown_to_user=TRUE
        """)
        tickets = list(cur.fetchall())

        touched = 0
        for row in tickets:
            view = dict(row["snapshot_view"])
            pick = view.get("model_pick") or ""
            probability = _f(view.get("model_probability"))
            odds = _f(view.get("picked_odds"))
            edge = _f(view.get("picked_edge"))
            result = row.get("result_1x2")
            status = "PENDING"
            correct = None
            pnl = None
            if result:
                correct = (pick == result)
                status = "WON" if correct else "LOST"
                pnl = _profit(odds, correct)

            for sid in list(row["strategies"] or []):
                if sid not in STRATEGY_CATALOG:
                    continue
                cur.execute("""
                    INSERT INTO strategy_simulations(
                        batch_id,match_id,strategy_id,strategy_name,kickoff_utc,
                        home_team,away_team,pick,probability,odds,edge,stake_eur,
                        priced,status,result_1x2,correct,profit_eur,
                        source_snapshot_timestamp_utc,updated_at
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1.00,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT(batch_id,match_id,strategy_id)
                    DO UPDATE SET
                        status=EXCLUDED.status,
                        result_1x2=EXCLUDED.result_1x2,
                        correct=EXCLUDED.correct,
                        profit_eur=EXCLUDED.profit_eur,
                        priced=EXCLUDED.priced,
                        updated_at=NOW()
                """, (
                    row["batch_id"],row["match_id"],sid,STRATEGY_CATALOG[sid]["name"],
                    row["kickoff_utc"],row["home_team"],row["away_team"],pick,
                    probability,odds,edge,odds is not None,status,result,correct,pnl,
                    row["snapshot_timestamp_utc"],
                ))
                touched += 1

        for sid, meta in STRATEGY_CATALOG.items():
            cur.execute("""
                SELECT
                    COUNT(*)::int signals_total,
                    COUNT(*) FILTER (WHERE status='PENDING')::int pending,
                    COUNT(*) FILTER (WHERE status IN ('WON','LOST'))::int settled,
                    COUNT(*) FILTER (WHERE status IN ('WON','LOST') AND priced)::int priced_settled,
                    COUNT(*) FILTER (WHERE status='WON')::int wins,
                    COUNT(*) FILTER (WHERE status='LOST')::int losses,
                    COALESCE(SUM(stake_eur) FILTER (WHERE status IN ('WON','LOST') AND priced),0) staked_eur,
                    COALESCE(SUM(profit_eur) FILTER (WHERE status IN ('WON','LOST') AND priced),0) profit_eur
                FROM strategy_simulations WHERE strategy_id=%s
            """, (sid,))
            a = cur.fetchone()
            settled = int(a["settled"] or 0)
            wins = int(a["wins"] or 0)
            staked = float(a["staked_eur"] or 0)
            profit = float(a["profit_eur"] or 0)
            win_rate = wins / settled if settled else None
            roi = profit / staked if staked else None

            cur.execute("""
                INSERT INTO strategy_stats(
                    strategy_id,strategy_name,description,stake_eur,signals_total,
                    pending,settled,priced_settled,wins,losses,staked_eur,
                    profit_eur,cumulative_profit_eur,win_rate,roi,
                    sample_reliability,updated_at
                )
                VALUES (%s,%s,%s,1.00,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT(strategy_id) DO UPDATE SET
                    strategy_name=EXCLUDED.strategy_name,
                    description=EXCLUDED.description,
                    signals_total=EXCLUDED.signals_total,
                    pending=EXCLUDED.pending,
                    settled=EXCLUDED.settled,
                    priced_settled=EXCLUDED.priced_settled,
                    wins=EXCLUDED.wins,
                    losses=EXCLUDED.losses,
                    staked_eur=EXCLUDED.staked_eur,
                    profit_eur=EXCLUDED.profit_eur,
                    cumulative_profit_eur=EXCLUDED.cumulative_profit_eur,
                    win_rate=EXCLUDED.win_rate,
                    roi=EXCLUDED.roi,
                    sample_reliability=EXCLUDED.sample_reliability,
                    updated_at=NOW()
            """, (
                sid,meta["name"],meta["description"],int(a["signals_total"] or 0),
                int(a["pending"] or 0),settled,int(a["priced_settled"] or 0),
                wins,int(a["losses"] or 0),staked,profit,profit,
                win_rate,roi,sample_reliability(settled),
            ))
        conn.commit()

    return {"tickets": len(tickets), "strategy_rows_touched": touched}


def get_strategy_stats():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM strategy_stats ORDER BY strategy_id")
        return list(cur.fetchall())


def get_strategy_history(strategy_id, limit=100):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT batch_id,match_id,strategy_id,strategy_name,kickoff_utc,
                   home_team,away_team,pick,probability,odds,edge,stake_eur,
                   priced,status,result_1x2,correct,profit_eur,updated_at
            FROM strategy_simulations
            WHERE strategy_id=%s
            ORDER BY kickoff_utc DESC
            LIMIT %s
        """, (strategy_id,limit))
        return list(cur.fetchall())
