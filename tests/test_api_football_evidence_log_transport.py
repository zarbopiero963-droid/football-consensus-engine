import contextlib
import io
from pathlib import Path
import pytest
from scripts.api_football_evidence_log_transport import encode, decode

def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.delenv("APIFOOTBALL_API_KEY", raising=False)
    p=tmp_path/"route_inventory.json"
    p.write_text('{"route_count":39}')
    out=io.StringIO()
    with contextlib.redirect_stdout(out):
        encode([p])
    z=tmp_path/"audit.zip"
    decode(out.getvalue(), z)
    import zipfile
    with zipfile.ZipFile(z) as f:
        assert f.read("route_inventory.json")==b'{"route_count":39}'

def test_missing_chunks_rejected(tmp_path):
    with pytest.raises(ValueError, match="INCOMPLETE_LOG_CHUNKS"):
        decode("AF_EVIDENCE_V1_BEGIN count=1 sha256=" + "0"*64, tmp_path/"invalid.zip")

def test_key_leak_blocked(tmp_path,monkeypatch):
    monkeypatch.setenv("APIFOOTBALL_API_KEY","PROTECTED_SAMPLE_KEY")
    p=tmp_path/"capabilities.json"
    p.write_text('{"data":"PROTECTED_SAMPLE_KEY"}')
    with pytest.raises(ValueError, match="SECRET_PRESENT"):
        encode([p])
