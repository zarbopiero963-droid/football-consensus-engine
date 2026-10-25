"""Deterministic per-tool scenario inventory without invented LIVE input.

Generated from exact connected tool names and typed provider contracts.
Invalid scenarios test validation; valid fixtures must come from provider observations.
"""
import json
from pathlib import Path
from bridge.api_football.contracts import ENDPOINTS
from bridge.api_football.tool_evidence_gate import expected_tools

SAFE_PROVIDER = {
    "apifootball_status": {}, "apifootball_countries": {},
    "apifootball_seasons": {}, "apifootball_timezones": {},
    "apifootball_bookmakers": {}, "apifootball_bets": {},
    "apifootball_live_bets": {}, "apifootball_health": {},
    "apifootball_quota_report": {},
}
BROWSER_READ_ONLY = {
    "browser_health": {}, "browser_cleanup_report": {},
    "browser_list_sessions": {},
}
DANGEROUS_BROWSER = {
    "browser_stop_all", "browser_stop_all_idle", "browser_reap",
}
MUTABLE_BROWSER = {
    "browser_click", "browser_fill", "browser_type", "browser_press",
    "browser_select_option", "browser_set_checked", "browser_stop_all",
}


def build_scenarios():
    scenarios={}
    for name in expected_tools():
        item={"tool":name,"negative":{"arguments":{"__unrecognized_cert_argument__":"denied"},
              "purpose":"reject unrecognized arguments without side effects"},
              "valid":None,"valid_prerequisite":None,"side_effect_risk":"LOW"}
        if name in SAFE_PROVIDER:
            item["valid"]={"arguments":SAFE_PROVIDER[name],"scope":"bounded read-only"}
        elif name in BROWSER_READ_ONLY:
            item["valid"]={"arguments":BROWSER_READ_ONLY[name],"scope":"read-only"}
        elif name.startswith("apifootball_"):
            contract=ENDPOINTS.get(name)
            item["valid_prerequisite"]="REAL_PROVIDER_ID_AND_VALIDATED_PARAMETERS" if contract else "DIAGNOSTIC"
            if contract is not None:
                item["documented_path"]=contract.path
                item["accepted_parameter_names"]=list(contract.fields)
                item["required_parameters"]=list(contract.required)
                item["parameter_official_verification"]="NOT_COMPLETE"
        elif name.startswith("browser_"):
            item["valid_prerequisite"]="ISOLATED_EPHEMERAL_SESSION_AND_SAFE_TEST_PAGE"
            if name in DANGEROUS_BROWSER:
                item["side_effect_risk"]="DESTRUCTIVE_SESSION_MANAGEMENT"
                item["valid_prerequisite"]="DEDICATED_DISPOSABLE_WORKER_ONLY"
            elif name in MUTABLE_BROWSER:
                item["side_effect_risk"]="MUTATES_BROWSER_STATE"
        scenarios[name]=item
    if len(scenarios)!=76:
        raise ValueError("INVENTORY_DRIFT")
    return scenarios


if __name__=="__main__":
    out=Path("evidence/mcp/scenario_inventory.json")
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(build_scenarios(),sort_keys=True,indent=2)+"\n")
    print("SCENARIOS",len(build_scenarios()),"VALID_NEED_PREREQUISITES",
          sum(x["valid"] is None for x in build_scenarios().values()))
