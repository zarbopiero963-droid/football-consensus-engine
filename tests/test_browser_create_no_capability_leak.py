"""browser_create must not leak the CDP capability (CLAUDE.md §2.8, §12).

Real finding (2026-10-31, live MCP call): Browser Use `liveUrl` embeds the
session CDP host (`?wss=https://<id>.cdp.browser-use.com`) and was returned
verbatim as `live_url`, contradicting "never the CDP URL".
"""
import asyncio

import httpx
import pytest

from bridge import browser_use, session_leases

CDP = "https://abc123.cdp.browser-use.com"
LIVE = "https://live.browser-use.com?wss=" + CDP.replace(":", "%3A").replace("/", "%2F")


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_API_KEY", "fake-test-key")
    monkeypatch.delenv("BROWSER_USE_EXPOSE_LIVE_URL", raising=False)
    monkeypatch.setattr(session_leases, "register", lambda *a: None)

    def reply(request):
        assert request.url.path.endswith("/browsers")
        return httpx.Response(200, json={"id": "abc123", "status": "active", "cdpUrl": CDP, "liveUrl": LIVE})

    real = httpx.AsyncClient
    monkeypatch.setattr(browser_use.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(reply), **k))
    return browser_use.BrowserUseBridge()


def test_create_withholds_live_url_and_cdp_by_default(bridge):
    out = asyncio.run(bridge.create())
    assert out == {"session_id": "abc123", "status": "active", "live_view_available": True}
    text = repr(out)
    assert "cdp" not in text and "wss" not in text and "browser-use.com" not in text


def test_live_url_only_with_explicit_operator_opt_in(bridge, monkeypatch):
    monkeypatch.setenv("BROWSER_USE_EXPOSE_LIVE_URL", "true")
    out = asyncio.run(bridge.create())
    assert out["live_url"] == LIVE and out["live_view_available"] is True
    assert "cdp_url" not in out and CDP not in {v for v in out.values() if isinstance(v, str) and v == CDP}


@pytest.mark.parametrize("flag", ["", "false", "0", "no", "TRUE-ish"])
def test_non_affirmative_flags_keep_live_url_hidden(bridge, monkeypatch, flag):
    monkeypatch.setenv("BROWSER_USE_EXPOSE_LIVE_URL", flag)
    assert "live_url" not in asyncio.run(bridge.create())
