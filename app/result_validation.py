from __future__ import annotations
import hashlib, json
from collections import defaultdict
from app.db import connect

FIELDS=["final_home_goals","final_away_goals","ht_home_goals","ht_away_goals","corners_home","corners_away","ht_corners_home","ht_corners_away"]

def result_1x2(h,a):
    if h is None or a is None: return None
    return "1" if h>a else ("X" if h==a else "2")

def observation_id(match_id,provider,observed_at):
    return hashlib.sha256(f"{match_id}|{provider}|{observed_at}".encode()).hexdigest()[:32]

def normalize_footystats(item):
    def iv(k):
        v=item.get(k)
        try: v=int(v)
        except: return None
        return None if v < 0 else v
    return {
      "provider_match_id":str(item.get("id") or ""),
      "status":str(item.get("status") or ""),
      "final_home_goals":iv("homeGoalCount"),"final_away_goals":iv("awayGoalCount"),
      "ht_home_goals":iv("ht_goals_team_a"),"ht_away_goals":iv("ht_goals_team_b"),
      "corners_home":iv("team_a_corners"),"corners_away":iv("team_b_corners"),
      "ht_corners_home":None,"ht_corners_away":None,
    }

def normalize_api_football(fixture, stats=None, ht_stats=None):
    score=fixture.get("score") or {}
    full=score.get("fulltime") or {}; half=score.get("halftime") or {}
    def stat(rows,label):
        for team in rows or []:
            side=team.get("_side")
            for s in team.get("statistics") or []:
                if s.get("type")==label:
                    try: return side,int(s.get("value"))
                    except: pass
        return None
    out={"provider_match_id":str((fixture.get("fixture") or {}).get("id") or ""),
         "status":str(((fixture.get("fixture") or {}).get("status") or {}).get("short") or ""),
         "final_home_goals":full.get("home"),"final_away_goals":full.get("away"),
         "ht_home_goals":half.get("home"),"ht_away_goals":half.get("away"),
         "corners_home":None,"corners_away":None,"ht_corners_home":None,"ht_corners_away":None}
    for rows,prefix in ((stats,"corners"),(ht_stats,"ht_corners")):
        for side in ("home","away"):
            for team in rows or []:
                if team.get("_side")!=side: continue
                for s in team.get("statistics") or []:
                    if s.get("type")=="Corner Kicks":
                        try: out[f"{prefix}_{side}"]=int(s.get("value"))
                        except: pass
    return out

def upsert_observation(match_id,provider,observed_at,kickoff,payload):
    norm=dict(payload)
    oid=observation_id(match_id,provider,observed_at)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO result_observations(
        observation_id,match_id,provider,provider_match_id,observed_at_utc,kickoff_utc,status,
        final_home_goals,final_away_goals,ht_home_goals,ht_away_goals,corners_home,corners_away,
        ht_corners_home,ht_corners_away,raw_sha256,payload)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT(observation_id) DO NOTHING""",
        (oid,match_id,provider,norm.get("provider_match_id",""),observed_at,kickoff,norm.get("status",""),
         *[norm.get(f) for f in FIELDS],
         hashlib.sha256(json.dumps(norm,sort_keys=True,default=str).encode()).hexdigest(),json.dumps(norm,default=str)))
        conn.commit()

def compute_consensus(rows):
    fv={}; values={}; conflict=False
    for f in FIELDS:
        vals=defaultdict(list)
        for r in rows:
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
    status="CONFLICT" if conflict else ("VERIFIED" if any_verified else ("SINGLE_SOURCE" if rows else "UNVERIFIED"))
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
        (match_id,*[values[f] for f in FIELDS],r12,status,json.dumps(fv),json.dumps(sorted({r["provider"] for r in rows}))))
        conn.commit()
    return {"match_id":match_id,"status":status,"fields":fv}

def rebuild_all():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT match_id FROM result_observations")
        ids=[r["match_id"] for r in cur.fetchall()]
    return [rebuild_consensus(x) for x in ids]
