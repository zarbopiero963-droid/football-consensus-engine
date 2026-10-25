from scripts.run_authenticated_mcp_e2e import SAFE_CALLS, planned_calls

def test_mcp_probe_runs_only_safe_bounded_calls():
    expected=set(SAFE_CALLS)
    calls=planned_calls(expected,cap=8)
    assert len(calls)==len(SAFE_CALLS)
    assert all(isinstance(args,dict) for _,args in calls)
    assert not any(name.startswith("browser_") for name,_ in calls)

def test_mcp_probe_refuses_cap_bypass():
    import pytest
    for cap in (0,9,999):
        with pytest.raises(ValueError):
            planned_calls(set(SAFE_CALLS),cap=cap)

def test_mcp_probe_does_not_fake_unavailable_tools():
    assert planned_calls({"browser_click","unknown_tool"})==[]
