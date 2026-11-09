"""Test-process setup.

The `signer` package refuses to import unless SXBET_SIGNER_PROCESS=1 and RENDER
is unset (it is a separate service, never part of the MCP runtime). The test
process plays the role of the signer process for the offline signer tests, so
it opts in here. Subprocess tests that model the MCP/Render runtime build their
own environment explicitly (see tests/test_sxbet_render_isolation.py).
"""
import os

os.environ.setdefault("SXBET_SIGNER_PROCESS", "1")


import pytest


@pytest.fixture(autouse=True)
def _isolate_oauth_throttle():
    """The OAuth limiters are process-global; start every test from a clean window."""
    from bridge import oauth, oauth_throttle
    oauth_throttle.reset_all()
    oauth.reset_bearer_cache()
    yield
    oauth_throttle.reset_all()
    oauth.reset_bearer_cache()
