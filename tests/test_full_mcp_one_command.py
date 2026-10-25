import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT=Path("scripts/run_full_mcp_certification.py")

def test_one_command_fails_without_real_evidence(tmp_path):
    root=tmp_path/"proofs"
    root.mkdir()
    target=tmp_path/"result.json"
    cp=subprocess.run([sys.executable,str(SCRIPT),"--evidence-dir",str(root),
                       "--deployed-sha","ee4cabd1cde5a15256651f0a4db6af814cc1bf15",
                       "--out",str(target)],capture_output=True,text=True)
    assert cp.returncode==1
    result=json.loads(target.read_text())
    assert result["status"]=="NO_GO"
    assert result["observed_tool_proof_count"]==0
    assert result["required_tool_proof_count"]==76
    assert result["official_parameter_reconciliation_complete"] is False
