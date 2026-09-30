#!/usr/bin/env python3
from __future__ import annotations

import json, os, re, urllib.request
from datetime import datetime, timedelta, timezone
from app.db import connect
from app.result_validation import normalize_api_football, normalize_footystats, rebuild_all, upsert_observation

UA="Mozilla/5.0 (compatible; football-consensus-results/1.0)"

def canon(s):
    s=(s or "").lower()
    s=re.sub(r"[^a-z0-9]+"," ",s)
    return " ".join(s.split())

def anchors():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT ON(match_id) match_id,home_team,away_team,kickoff_utc
        FROM model_snapshots
        WHERE kickoff_utc > NOW()-INTERVAL '3 days' AND kickoff_utc < NOW()+INTERVAL '1 day'
        ORDER BY match_id,snapshot_timestamp_utc DESC""")
        return list(cur.fetchall())

def find_anchor(home,away,kickoff,rows):
    hits=[]
    for r in rows:
        if canon(r["home_team"])!=canon(home) or canon(r["away_team"])!=canon(away): continue
        if kickoff and abs((kickoff-r["kickoff_utc"]).total_seconds())>7200: continue
        hits.append(r)
    return hits[0] if len(hits)==1 else None

def http_json(url,headers=None):
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"application/json",**(headers or {})})
    with urllib.request.urlopen(req,timeout=45) as resp:
        return json.loads(resp.read())

def ingest_existing_fifa():
    now=datetime.now(timezone.utc)
    n=0
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT ON(match_id) match_id,kickoff_utc,payload
        FROM match_results ORDER BY match_id,version DESC""")
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
        upsert_observation(r["match_id"],"fifa-calendar-v3",now,r["kickoff_utc"],norm); n+=1
    return n

def ingest_footystats(anchor_rows):
    key=os.environ.get("FOOTYSTATS_API_KEY","").strip()
    if not key: return {"configured":False,"inserted":0}
    inserted=0
    for d in {(datetime.now(timezone.utc)+timedelta(days=x)).date().isoformat() for x in (-2,-1,0)}:
        data=http_json(f"https://api.football-data-api.com/todays-matches?key={key}&timezone=Etc/UTC&date={d}")
        matches=(data.get("data") or data.get("matches") or [])
        for m in matches:
            home=(m.get("home_name") or m.get("home_name_en") or m.get("homeTeam") or "")
            away=(m.get("away_name") or m.get("away_name_en") or m.get("awayTeam") or "")
            ts=m.get("date_unix")
            kickoff=datetime.fromtimestamp(int(ts),timezone.utc) if ts else None
            a=find_anchor(home,away,kickoff,anchor_rows)
            if not a: continue
            upsert_observation(a["match_id"],"footystats",datetime.now(timezone.utc),kickoff,normalize_footystats(m)); inserted+=1
    return {"configured":True,"inserted":inserted}

def api_stat_value(rows):
    for idx,t in enumerate(rows or []):
        t["_side"]="home" if idx==0 else "away"
    return rows or []

def ingest_api_football(anchor_rows):
    key=os.environ.get("API_FOOTBALL_KEY","").strip()
    if not key: return {"configured":False,"inserted":0}
    headers={"x-apisports-key":key}
    inserted=0
    for d in {(datetime.now(timezone.utc)+timedelta(days=x)).date().isoformat() for x in (-2,-1,0)}:
        data=http_json(f"https://v3.football.api-sports.io/fixtures?date={d}",headers)
        for item in data.get("response") or []:
            fx=item.get("fixture") or {}; teams=item.get("teams") or {}
            kick=datetime.fromisoformat(str(fx.get("date")).replace("Z","+00:00")) if fx.get("date") else None
            a=find_anchor((teams.get("home") or {}).get("name"),(teams.get("away") or {}).get("name"),kick,anchor_rows)
            if not a: continue
            fid=fx.get("id")
            stats=[]; htstats=[]
            if fid:
                try:
                    stats=api_stat_value((http_json(f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fid}",headers).get("response") or []))
                except Exception: stats=[]
                try:
                    htstats=api_stat_value((http_json(f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fid}&half=true",headers).get("response") or []))
                except Exception: htstats=[]
            norm=normalize_api_football(item,stats,htstats)
            upsert_observation(a["match_id"],"api-football",datetime.now(timezone.utc),kick,norm); inserted+=1
    return {"configured":True,"inserted":inserted}

def main():
    rows=anchors()
    out={"fifa":ingest_existing_fifa()}
    try: out["footystats"]=ingest_footystats(rows)
    except Exception as e: out["footystats"]={"error":f"{type(e).__name__}: {e}"}
    try: out["api_football"]=ingest_api_football(rows)
    except Exception as e: out["api_football"]={"error":f"{type(e).__name__}: {e}"}
    consensus=rebuild_all()
    out["consensus"]={"matches":len(consensus),"verified":sum(x["status"]=="VERIFIED" for x in consensus),"conflicts":sum(x["status"]=="CONFLICT" for x in consensus)}
    print(json.dumps(out,default=str))

if __name__=="__main__":
    main()
