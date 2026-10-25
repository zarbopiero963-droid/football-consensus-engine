from scripts.mcp_scenarios_76 import build_scenarios

def test_all_connected_tools_have_scenario_entries():
    scenarios=build_scenarios()
    assert len(scenarios)==76
    assert all(row["negative"]["arguments"] for row in scenarios.values())
    assert all(row["valid"] is not None or row["valid_prerequisite"] for row in scenarios.values())

def test_official_parameter_status_is_cautious():
    for row in build_scenarios().values():
        if "documented_path" in row:
            assert row["parameter_official_verification"]=="NOT_COMPLETE"
