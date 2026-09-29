from app import persist


def test_guarded_is_noop_without_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    called = {"value": False}

    def op():
        called["value"] = True
        raise AssertionError("must not be called")

    result = persist.guarded(op)
    assert result == {"configured": False, "ok": True, "result": None, "error": None}
    assert called["value"] is False


def test_database_required_flag(monkeypatch):
    monkeypatch.setenv("DATABASE_REQUIRED", "1")
    assert persist.required() is True
    monkeypatch.setenv("DATABASE_REQUIRED", "0")
    assert persist.required() is False
