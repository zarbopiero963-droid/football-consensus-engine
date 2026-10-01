from __future__ import annotations
import hashlib, json
from collections import defaultdict
from app.db import connect

FIELDS=["final_home_goals","final_away_goals","ht_home_goals","ht_away_goals","corners_home","corners_away","ht_corners_home","ht_corners_away"]

def result_1x2(h,a):
    if h is None or a is None: return None
    return "1" if h>a else ("X" if h==a else "2")

def observation_fingerprint(provider_match_id, status, payload):
    semantic = {
        "provider_match_id": str(provider_match_id or ""),
        "status": str(status or ""),
        **{field: payload.get(field) for field in FIELDS},
    }
    return hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def observation_id(match_id, provider, provider_match_id, status, payload):
    fingerprint = observation_fingerprint(provider_match_id, status, payload)
    return hashlib.sha256(
        f"{match_id}|{provider}|{fingerprint}".encode()
    ).hexdigest()[:32]


def upsert_observation(match_id,provider,observed_at,kickoff,payload):
    norm=dict(payload)
    provider_match_id=norm.get("provider_match_id","")
    status=norm.get("status","")
    oid=observation_id(match_id,provider,provider_match_id,status,norm)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO result_observations(
        observation_id,match_id,provider,provider_match_id,observed_at_utc,kickoff_utc,status,
        final_home_goals,final_away_goals,ht_home_goals,ht_away_goals,corners_home,corners_away,
        ht_corners_home,ht_corners_away,raw_sha256,payload)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT(observation_id) DO NOTHING""",
        (oid,match_id,provider,provider_match_id,observed_at,kickoff,status,
         *[norm.get(f) for f in FIELDS],
         hashlib.sha256(json.dumps(norm,sort_keys=True,default=str).encode()).hexdigest(),json.dumps(norm,default=str)))
        conn.commit()

def compute_consensus(rows):
    production_rows=[
        r for r in rows
        if not str(r.get("provider") or "").startswith("pilot:")
    ]
    fv={}; values={}; conflict=False
    for f in FIELDS:
        vals=defaultdict(list)
        for r in production_rows:
            if r.get(f) is not None:
                vals[r.get(f)].append(r.get("provider"))
        if not vals:
            fv[f]={"status":"MISSING","sources":[]}; values[f]=None
        elif len(vals)==1:
            val,sources=next(iter(vals.items()))
            status="VERIFIED" if len(set(sources))>=2 else "SINGLE_SOURCE"
            fv[f]={"status":status,"sources":sources,"value":val}; values[f]=val
        else:
            fv[f]={"status":"CONFLICT","values":{str(k):v for k,v in vals.items()}}
            values[f]=None; conflict=True
    any_verified=any(v["status"]=="VERIFIED" for v in fv.values())
    status="CONFLICT" if conflict else ("VERIFIED" if any_verified else ("SINGLE_SOURCE" if production_rows else "UNVERIFIED"))
    return values,fv,status

def rebuild_consensus(match_id):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT ON(provider) * FROM result_observations
                       WHERE match_id=%s ORDER BY provider,observed_at_utc DESC""",(match_id,))
        rows=list(cur.fetchall())
        values,fv,status=compute_consensus(rows)
        r12=result_1x2(values["final_home_goals"],values["final_away_goals"])
        cur.execute("""INSERT INTO result_consensus(match_id,final_home_goals,final_away_goals,ht_home_goals,ht_away_goals,
        corners_home,corners_away,ht_corners_home,ht_corners_away,result_1x2,verification_status,field_verification,providers,updated_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,NOW())
        ON CONFLICT(match_id) DO UPDATE SET final_home_goals=EXCLUDED.final_home_goals,final_away_goals=EXCLUDED.final_away_goals,
        ht_home_goals=EXCLUDED.ht_home_goals,ht_away_goals=EXCLUDED.ht_away_goals,corners_home=EXCLUDED.corners_home,
        corners_away=EXCLUDED.corners_away,ht_corners_home=EXCLUDED.ht_corners_home,ht_corners_away=EXCLUDED.ht_corners_away,
        result_1x2=EXCLUDED.result_1x2,verification_status=EXCLUDED.verification_status,
        field_verification=EXCLUDED.field_verification,providers=EXCLUDED.providers,updated_at=NOW()""",
        (match_id,*[values[f] for f in FIELDS],r12,status,json.dumps(fv),json.dumps(sorted({
            r["provider"] for r in rows
            if not str(r.get("provider") or "").startswith("pilot:")
        }))))
        conn.commit()
    return {"match_id":match_id,"status":status,"fields":fv}

def rebuild_all():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT match_id FROM result_observations")
        ids=[r["match_id"] for r in cur.fetchall()]
    return [rebuild_consensus(x) for x in ids]


def sync_fifa_from_match_results():
    from datetime import datetime, timezone
    now=datetime.now(timezone.utc)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""WITH latest_results AS (
                         SELECT DISTINCT ON(mr.match_id)
                           mr.match_id,
                           mr.payload
                         FROM match_results mr
                         ORDER BY mr.match_id, mr.version DESC
                       ),
                       latest_snapshots AS (
                         SELECT DISTINCT ON(ms.match_id)
                           ms.match_id,
                           ms.kickoff_utc
                         FROM model_snapshots ms
                         ORDER BY ms.match_id, ms.snapshot_timestamp_utc DESC
                       )
                       SELECT r.match_id, s.kickoff_utc, r.payload
                       FROM latest_results r
                       LEFT JOIN latest_snapshots s USING(match_id)""")
        rows=list(cur.fetchall())
    for r in rows:
        p=dict(r["payload"])
        norm={
          "provider_match_id":str(p.get("result_source_id") or ""),
          "status":str(p.get("status") or ""),
          "final_home_goals":p.get("final_home_goals"),
          "final_away_goals":p.get("final_away_goals"),
          "ht_home_goals":p.get("ht_home_goals"),
          "ht_away_goals":p.get("ht_away_goals"),
          "corners_home":p.get("corners_home"),"corners_away":p.get("corners_away"),
          "ht_corners_home":p.get("ht_corners_home"),"ht_corners_away":p.get("ht_corners_away"),
        }
        upsert_observation(r["match_id"],"fifa-calendar-v3",now,r["kickoff_utc"],norm)
    rebuild_all()
    return len(rows)
