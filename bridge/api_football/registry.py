"""Canonical deterministic API-FOOTBALL v3 contract registry; offline only."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib, json
from typing import Any, Mapping
REGISTRY_VERSION="2026.10.25"; SOURCE_URL="https://www.api-football.com/documentation_v3"
VALID_STATUSES=frozenset({"DOCUMENTED","IMPLEMENTED","MOCK_VERIFIED","REAL_VERIFIED","UNKNOWN","BLOCKED","PENDING_OFFICIAL_VERIFICATION"})
@dataclass(frozen=True)
class ParameterContract:
    id:str; name:str; type:str; required:bool
    status:str="PENDING_OFFICIAL_VERIFICATION"; enum:tuple[str,...]=(); minimum:int|None=None; maximum:int|None=None; format:str|None=None; source:str=SOURCE_URL; notes:str=""
@dataclass(frozen=True)
class EndpointContract:
    id:str; method:str; path:str; category:str; status:str
    parameters:tuple[ParameterContract,...]=(); response_schema:Mapping[str,Any]|None=None; source:str=SOURCE_URL; verified_at_utc:str|None=None; notes:str=""
def _p(e,n,t="string",r=False,**kw): return ParameterContract(id=f"{e}.{n}",name=n,type=t,required=r,**kw)
def build_registry():
    paths={"status":("/status","account"),"countries":("/countries","catalog"),"leagues":("/leagues","catalog"),"seasons":("/leagues/seasons","catalog"),"fixtures":("/fixtures","fixtures"),"fixture_statistics":("/fixtures/statistics","fixtures"),"fixture_events":("/fixtures/events","fixtures"),"fixture_lineups":("/fixtures/lineups","fixtures"),"fixture_players":("/fixtures/players","fixtures"),"teams":("/teams","teams"),"team_statistics":("/teams/statistics","teams"),"team_seasons":("/teams/seasons","teams"),"team_countries":("/teams/countries","teams"),"venues":("/venues","catalog"),"standings":("/standings","standings"),"fixture_rounds":("/fixtures/rounds","fixtures"),"fixture_headtohead":("/fixtures/headtohead","fixtures"),"injuries":("/injuries","medical"),"coaches":("/coachs","people"),"player_seasons":("/players/seasons","players"),"player_profiles":("/players/profiles","players"),"players":("/players","players"),"player_squads":("/players/squads","players"),"player_teams":("/players/teams","players"),"top_scorers":("/players/topscorers","players"),"top_assists":("/players/topassists","players"),"top_yellow_cards":("/players/topyellowcards","players"),"top_red_cards":("/players/topredcards","players"),"transfers":("/transfers","players"),"trophies":("/trophies","people"),"sidelined":("/sidelined","people"),"predictions":("/predictions","predictions"),"odds":("/odds","odds"),"odds_mapping":("/odds/mapping","odds"),"bookmakers":("/odds/bookmakers","odds"),"bets":("/odds/bets","odds"),"live_odds":("/odds/live","odds"),"live_bets":("/odds/live/bets","odds")}
    common={"id":"integer","league":"integer","season":"integer","team":"integer","fixture":"integer","player":"integer","coach":"integer","page":"integer","last":"integer","next":"integer","date":"string","from":"string","to":"string","timezone":"string","search":"string","country":"string","code":"string","name":"string","live":"string","status":"string","current":"boolean","dates":"boolean","bookmaker":"integer","bet":"integer","h2h":"string"}
    req={"fixture_statistics":["fixture"],"fixture_events":["fixture"],"fixture_lineups":["fixture"],"fixture_players":["fixture"],"predictions":["fixture"],"team_statistics":["league","season","team"],"standings":["league","season"],"fixture_rounds":["league","season"],"fixture_headtohead":["h2h"],"team_seasons":["team"],"player_teams":["player"],"top_scorers":["league","season"],"top_assists":["league","season"],"top_yellow_cards":["league","season"],"top_red_cards":["league","season"]}
    empty={"status","seasons","team_countries","player_seasons","odds_mapping"}
    pending={"players","player_profiles","injuries","bookmakers","bets","odds_mapping"}
    eps=[]
    for key,(path,cat) in paths.items():
        names=[] if key in empty else list(common)
        params=tuple(_p(key,n,common[n],n in req.get(key,[]),minimum=1 if common[n]=="integer" else None,maximum=2000000000 if common[n]=="integer" else None,format="YYYY-MM-DD" if n in {"date","from","to"} else None) for n in names)
        eps.append(asdict(EndpointContract(id="apifootball."+key,method="GET",path=path,category=cat,status="PENDING_OFFICIAL_VERIFICATION" if key in pending else "DOCUMENTED",parameters=params,response_schema={"type":"object","required":["response"],"properties":{"response":{"type":"array"}}})))
    return {"registry_version":REGISTRY_VERSION,"source":SOURCE_URL,"retrieved_at_utc":datetime.now(timezone.utc).date().isoformat(),"endpoints":eps,"verification_policy":{"real_api_calls_authorized":False,"unknown_is_not_pass":True}}
def canonical_json(reg): return json.dumps(reg,sort_keys=True,separators=(",",":"),ensure_ascii=False)
def sha256(reg): return hashlib.sha256(canonical_json(reg).encode()).hexdigest()
def validate_registry(reg):
    errors=[]; ids=set()
    for e in reg.get("endpoints",[]):
        if e["id"] in ids: errors.append("DUPLICATE_ID:"+e["id"])
        ids.add(e["id"])
        if e["status"] not in VALID_STATUSES: errors.append("INVALID_STATUS:"+e["id"])
        for p in e.get("parameters",[]):
            if p["id"] in ids: errors.append("DUPLICATE_ID:"+p["id"])
            ids.add(p["id"])
            if p["status"] not in VALID_STATUSES: errors.append("INVALID_STATUS:"+p["id"])
            if p["status"]=="REAL_VERIFIED" and not reg["verification_policy"].get("real_api_calls_authorized"): errors.append("REAL_VERIFIED_FORBIDDEN:"+p["id"])
    return errors
