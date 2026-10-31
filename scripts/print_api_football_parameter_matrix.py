"""Deterministic audit of the actual endpoint parameter matrix.

An endpoint existing in source is not proof that the connected MCP schema
is refreshed, or that the official provider behavior has been certified.
"""
from bridge.api_football.contracts import ENDPOINTS

def matrix():
    return [{"tool":name,"path":contract.path,"parameters":list(contract.fields),
             "required":list(contract.required),"code_contract_confidence":contract.confidence,
             "provider_real_coverage":"NOT_EXHAUSTIVELY_VERIFIED",
             "connected_plugin_schema":"REQUIRES_INDEPENDENT_DISCOVERY"}
            for name,contract in sorted(ENDPOINTS.items())]

def main():
    import json
    print(json.dumps({"source":"bridge/api_football/contracts.py",
          "route_count":len({x.path for x in ENDPOINTS.values()}),
          "tool_count":len(ENDPOINTS),"official_parameter_matrix_status":"INCOMPLETE",
          "matrix":matrix()},indent=2,sort_keys=True))

if __name__=="__main__":
    main()
