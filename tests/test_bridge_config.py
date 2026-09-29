import pytest

from bridge.browser_use import BrowserUseBridge


def test_api_key_required(monkeypatch):
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        BrowserUseBridge._api_key()


def test_proxy_country_code_validation(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_API_KEY", "test-key")

    # Validation happens before any network call.
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(BrowserUseBridge().create("italy"))
