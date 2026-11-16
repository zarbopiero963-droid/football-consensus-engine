"""Pinned MCP surface of the 42 apifootball_* tools, for the protected live E2E (WP6, E-G1/E-G3).

The live E2E client (scripts/mcp_e2e_render.py) is standard-library only and runs with
`python -I`, so it cannot import the bridge. This script derives, from the server's OWN registry
(bridge/api_football/tools.py register_tools on a real MCPServer, contracts.ENDPOINTS), the file
docs/certification/apifootball/mcp_schema_v1.json that the client compares the deployed server
against:

- the exact set of the 42 apifootball_* tool names;
- per tool, a projection of its MCP inputSchema (property names, JSON types, defaults, required,
  additionalProperties; titles and descriptions excluded) and its SHA-256, plus one SHA-256 over
  the whole set (projection: scripts/mcp_e2e_render.schema_projection, the single implementation);
- zero-provider-call negative cases: arguments refused locally before any quota reservation,
  each with the fixed result the server returns (INVALID_INPUT + fixed code from contracts.validate,
  or an MCP schema refusal before the tool runs). Every case is computed by CALLING the real tool
  through the real MCPServer with live requests authorized and a tripwire provider client: a case
  is pinned only if the tripwire was never touched.

`--check` (CI, tests/test_apifootball_mcp_schema.py) fails when the committed file differs from
what the registry produces now. No network, no database, no secret: the API key is a fixed fake.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.mcp_e2e_render import (  # noqa: E402  (single implementation of the projection)
    MCP_SCHEMA_FORMAT, MCP_SCHEMA_PATH, NO_LOCAL_REJECTION_REASON, _sha256, schema_projection)

OUT = ROOT / MCP_SCHEMA_PATH
FAKE_KEY = "GENERATOR_FAKE_KEY_NEVER_SENT"
WRONG_TYPE_VALUE = "not-an-integer"


class ProviderTouched(Exception):
    pass


class Tripwire:
    """A provider client that records any use: a pinned negative must never reach it."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        self.touched.append(name)
        raise ProviderTouched(name)


def _server(tripwire: Tripwire):
    from mcp.server import MCPServer
    from bridge.api_football.config import Settings
    from bridge.api_football.tools import register_tools
    server = MCPServer("apifootball-mcp-schema-generator")
    names = register_tools(server, Settings(enabled=True, api_key=FAKE_KEY, live_authorized=True), client=tripwire)
    return server, names


def _bad_value(prop: dict[str, Any]) -> Any:
    """Schema-valid but refused by contracts.validate: 0 for integers, "" for strings."""
    types = schema_projection({"properties": {"p": prop}})["properties"]["p"]["types"]
    if "integer" in types:
        return 0
    if "string" in types:
        return ""
    return None


async def _outcome(server, tripwire: Tripwire, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    import pydantic
    before = len(tripwire.touched)
    try:
        res = await server.call_tool(tool, args)
    except Exception as exc:  # the SDK raises ToolError; on the wire it becomes isError=true
        schema = isinstance(exc.__cause__, pydantic.ValidationError)  # the argument model, not the tool body
        return {"kind": "MCP_SCHEMA_REJECTED" if schema else "TOOL_ERROR",
                "provider_touched": len(tripwire.touched) > before}
    touched = len(tripwire.touched) > before
    dumped = res.model_dump(by_alias=True, mode="json")
    payload = dumped.get("structuredContent")
    if dumped.get("isError"):
        return {"kind": "TOOL_ERROR", "provider_touched": touched}
    if isinstance(payload, dict) and payload.get("status") == "INVALID_INPUT" \
            and payload.get("network_calls_made") == 0 and isinstance(payload.get("error"), dict):
        return {"kind": "INVALID_INPUT", "code": payload["error"].get("code"), "provider_touched": touched}
    return {"kind": "OTHER", "provider_touched": touched}


async def _build() -> dict[str, Any]:
    from bridge.api_football.contracts import ENDPOINTS
    tripwire = Tripwire()
    server, names = _server(tripwire)
    # The wire shape (camelCase inputSchema), exactly as tools/list serialises it.
    listed = {t.name: t.model_dump(by_alias=True, mode="json") for t in await server.list_tools()
              if t.name.startswith("apifootball_")}
    if sorted(listed) != sorted(names) or len(names) != 42:
        raise SystemExit("registry and tools/list disagree")
    tools: dict[str, Any] = {}
    for name in sorted(listed):
        schema = listed[name].get("inputSchema") if isinstance(listed[name].get("inputSchema"), dict) else {}
        projection = schema_projection(schema)
        tools[name] = {"projection_sha256": _sha256(projection), "projection": projection}

    negatives: list[dict[str, Any]] = []
    not_testable: dict[str, str] = {}
    wrong_type_done = False
    for name in sorted(ENDPOINTS):
        schema = listed[name]["inputSchema"]
        props = schema.get("properties") or {}
        required = list(schema.get("required") or [])
        if not props:
            # Unknown arguments are dropped by the SDK (they never fail the call), so a tool
            # without parameters has no argument set that is refused before the provider.
            not_testable[name] = NO_LOCAL_REJECTION_REASON
            continue
        candidates = [{r: _bad_value(props[r]) for r in required}] if required else []
        candidates += [{p: _bad_value(props[p]), **{r: _bad_value(props[r]) for r in required if r != p}}
                       for p in props if p not in required]
        for args in candidates:
            if any(v is None for v in args.values()):
                continue
            out = await _outcome(server, tripwire, name, args)
            if out["kind"] == "INVALID_INPUT" and not out["provider_touched"]:
                negatives.append({"tool": name, "arguments": args, "expect": "INVALID_INPUT", "code": out["code"],
                                  "class": "INVALID_VALUE"})
                break
        else:
            raise SystemExit(f"no locally refused argument set for {name}")
        if required:
            out = await _outcome(server, tripwire, name, {})
            if out != {"kind": "MCP_SCHEMA_REJECTED", "provider_touched": False}:
                raise SystemExit(f"missing required argument not refused by the schema for {name}")
            negatives.append({"tool": name, "arguments": {}, "expect": "MCP_SCHEMA_REJECTED", "code": None,
                              "class": "MISSING_REQUIRED"})
            ints = [r for r in required if "integer" in schema_projection(schema)["properties"][r]["types"]]
            if ints and not wrong_type_done:
                args = {r: (WRONG_TYPE_VALUE if r == ints[0] else 1) for r in required}
                out = await _outcome(server, tripwire, name, args)
                if out != {"kind": "MCP_SCHEMA_REJECTED", "provider_touched": False}:
                    raise SystemExit(f"wrong argument type not refused by the schema for {name}")
                negatives.append({"tool": name, "arguments": args, "expect": "MCP_SCHEMA_REJECTED", "code": None,
                                  "class": "WRONG_TYPE"})
                wrong_type_done = True
    if tripwire.touched:
        raise SystemExit("the provider client was touched")
    for i, case in enumerate(negatives, 1):
        case["id"] = f"Z{i:03d}"
    projections = {n: t["projection"] for n, t in tools.items()}
    return {
        "format": MCP_SCHEMA_FORMAT,
        "generated_by": "scripts/apifootball_mcp_schema.py",
        "source": "bridge/api_football/tools.py register_tools on mcp.server.MCPServer; contracts.ENDPOINTS",
        "projection": "scripts/mcp_e2e_render.schema_projection (types, defaults, required, additionalProperties; "
                      "no titles or descriptions)",
        "tool_count": len(tools),
        "schema_set_sha256": _sha256(projections),
        "tools": tools,
        "zero_call_negatives": [{k: c[k] for k in ("id", "tool", "class", "arguments", "expect", "code")}
                                for c in negatives],
        "zero_call_not_testable": not_testable,
    }


def render() -> str:
    return json.dumps(asyncio.run(_build()), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    args = ap.parse_args(argv)
    text = render()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if current != text:
            print(f"STALE: {MCP_SCHEMA_PATH} differs from the registry; run scripts/apifootball_mcp_schema.py")
            return 1
        print(f"OK: {MCP_SCHEMA_PATH} matches the registry")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {MCP_SCHEMA_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
