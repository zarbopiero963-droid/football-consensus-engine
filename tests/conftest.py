"""Test-process setup.

The `signer` package refuses to import unless SXBET_SIGNER_PROCESS=1 and RENDER
is unset (it is a separate service, never part of the MCP runtime). The test
process plays the role of the signer process for the offline signer tests, so
it opts in here. Subprocess tests that model the MCP/Render runtime build their
own environment explicitly (see tests/test_sxbet_render_isolation.py).
"""
import os

os.environ.setdefault("SXBET_SIGNER_PROCESS", "1")
