"""Review fixes on signer_service: no order detail before passkey unlock, Host allowlist, audit chain across
processes, rate-limited refusal audit, label / assertion hardening. Offline, throwaway keys only."""
import json
import os

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from starlette.testclient import TestClient  # noqa: E402

from signer import approval as appr  # noqa: E402
from signer import webauthn as wa  # noqa: E402
from signer.webauthn import b64url_decode, b64url_encode  # noqa: E402
from signer_service import config as scfg  # noqa: E402
from signer_service import store as st  # noqa: E402
from signer_service.app import build_app  # noqa: E402
from signer_service.devinit import DEV_MARKET_HASH, simulated_market  # noqa: E402
from signer_service.service import ServiceConfigError  # noqa: E402
from sxbet_webauthn_kit import FakePasskey, to_wire  # noqa: E402
from test_signer_service import KEY, Env, created  # noqa: E402

COOKIE = "fcb_approval"
MARKET = simulated_market()["markets"][DEV_MARKET_HASH]


@pytest.fixture
def e(tmp_path):
    env = Env(tmp_path)
    yield env
    env.svc.close()


def order_secrets(e, rid):
    """Strings that must never appear in a pre-auth response."""
    rec = e.svc.store.get(rid)["record"]
    vals = [MARKET["eventLabel"], MARKET["marketLabel"], MARKET["outcomeOneName"], MARKET["outcomeTwoName"],
            MARKET["sportXeventId"], DEV_MARKET_HASH, rec["intent"]["total_bet_size"],
            rec["intent"]["percentage_odds"], rec["display"]["stake_usdc"], rec["display"]["decimal_odds_limit"],
            rec["intent"]["idempotency_key"], rec["binding"]["intent_hash"], rec["approval"]["nonce"]]
    vals += [b64url_encode(p.credential_id) for p in getattr(e, "passkeys", [])]
    return vals


FIELD_NAMES = ('"binding"', '"display"', '"challenge_approve"', '"challenge_reject"', '"credential_ids"',
               '"intent"', '"state"', '"history"')


def assert_no_leak(e, rid, resp):
    text = resp.text
    for v in order_secrets(e, rid):
        assert v not in text, (resp.request.method, resp.request.url.path, v)
    if resp.headers.get("content-type", "").startswith("application/json"):  # data responses: no field at all
        for f in FIELD_NAMES:
            assert f not in text, (resp.request.method, resp.request.url.path, f)


def cookie_of(resp):
    for h in resp.headers.get_list("set-cookie"):
        if h.startswith(COOKIE + "="):
            return h
    return None


# ---------------------------------------------------------------- pre-auth: nothing leaks
def test_every_route_pre_auth_reveals_no_order_detail(e):
    e.enroll()
    rid = created(e)
    app = build_app(e.svc)
    fresh = TestClient(app, base_url="https://127.0.0.1:8787")  # no cookie, no HMAC
    seen = set()
    for route in app.routes:
        for method in sorted(route.methods - {"HEAD"}):
            path = route.path.replace("{request_id}", rid).replace("{name}", "approve.js")
            seen.add((method, route.path))
            for body in (None, b"{}", b'{"decision":"APPROVE"}', b"not json"):
                r = fresh.request(method, path, content=body, headers={"Content-Type": "application/json"})
                assert_no_leak(e, rid, r)
                if route.path in ("/approve/{request_id}/challenge", "/approve/{request_id}/assertion"):
                    assert (r.status_code, r.json()) == (401, {"error": "APPROVAL_LOCKED"}), (method, path, body)
    assert len(seen) == len([1 for r in app.routes for m in r.methods - {"HEAD"}]) >= 13
    for path in (f"/approve/{rid}/nope", "/static/../service.py", "/v1/requests/" + rid):
        assert_no_leak(e, rid, fresh.get(path))


def test_pre_auth_responses_do_not_reveal_whether_a_request_exists(e):
    rid = created(e)
    ghost = "req_" + "0" * 32
    for path in ("/approve/{}", "/approve/{}/challenge"):
        a, b = e.client.get(path.format(rid)), e.client.get(path.format(ghost))
        assert (a.status_code, a.content) == (b.status_code, b.content), path
    oa = e.client.post(f"/approve/{rid}/unlock/options").json()
    ob = e.client.post(f"/approve/{ghost}/unlock/options").json()
    assert set(oa) == set(ob) == {"request_id", "unlock_id", "challenge", "rp_id", "user_verification", "timeout"}
    assert oa["challenge"] != ob["challenge"] and len(b64url_decode(oa["challenge"])) == 32
    assert "allowCredentials" not in oa  # discoverable credentials: no credential id is disclosed


def test_details_visible_only_after_valid_unlock(e):
    pk = e.enroll()
    rid = created(e)
    assert e.client.get(f"/approve/{rid}/challenge").json() == {"error": "APPROVAL_LOCKED"}
    r = e.unlock(pk, rid)
    assert r.status_code == 200 and r.json() == {"unlocked": True, "expires_at": r.json()["expires_at"]}
    assert_no_leak(e, rid, r)
    c = cookie_of(r)
    for attr in ("HttpOnly", "Secure", "SameSite=Strict", f"Path=/approve/{rid}"):
        assert attr.lower() in c.lower(), (attr, c)
    v = e.client.get(f"/approve/{rid}/challenge").json()
    assert v["display"]["event_label"] == MARKET["eventLabel"] and "credential_ids" not in v


def test_unlock_refusals_are_generic_and_reveal_nothing(e):
    pk = e.enroll()
    rid = created(e)
    o = e.client.post(f"/approve/{rid}/unlock/options").json()
    chal = b64url_decode(o["challenge"])
    cases = [
        FakePasskey().assert_(chal),                         # not enrolled
        pk.assert_(chal, origin="https://evil.example"),     # wrong origin
        pk.assert_(b"\x01" * 32),                            # wrong challenge
        pk.assert_(chal, flags=wa.FLAG_UP),                  # UV missing
    ]
    for parts in cases:
        o = e.client.post(f"/approve/{rid}/unlock/options").json()
        r = e.client.post(f"/approve/{rid}/unlock", json={"unlock_id": o["unlock_id"], **to_wire(parts)})
        assert (r.status_code, r.json()) == (401, {"error": "UNLOCK_REFUSED"})
        assert cookie_of(r) is None
    assert e.client.get(f"/approve/{rid}/challenge").json() == {"error": "APPROVAL_LOCKED"}


def test_unlock_challenge_is_single_use_and_request_bound(e):
    pk = e.enroll()
    a = created(e, cid="client-req-A001")
    b = created(e, cid="client-req-B002")
    o = e.client.post(f"/approve/{a}/unlock/options").json()
    body = {"unlock_id": o["unlock_id"], **to_wire(pk.assert_(b64url_decode(o["challenge"])))}
    assert e.client.post(f"/approve/{b}/unlock", json=body).json() == {"error": "UNLOCK_REFUSED"}  # A's on B
    assert e.client.post(f"/approve/{a}/unlock", json=body).json() == {"error": "UNLOCK_REFUSED"}  # consumed
    o = e.client.post(f"/approve/{a}/unlock/options").json()
    body = {"unlock_id": o["unlock_id"], **to_wire(pk.assert_(b64url_decode(o["challenge"])))}
    e.clock.t += 61
    assert e.client.post(f"/approve/{a}/unlock", json=body).json() == {"error": "UNLOCK_REFUSED"}  # expired


# ---------------------------------------------------------------- session token
def test_token_is_single_use_for_details_and_for_the_decision(e):
    pk = e.enroll()
    rid = created(e)
    e.unlock(pk, rid)
    assert "display" in e.client.get(f"/approve/{rid}/challenge").json()
    assert e.client.get(f"/approve/{rid}/challenge").json() == {"error": "APPROVAL_LOCKED"}  # details once
    body = e.assertion(pk, rid)  # new unlock + details
    assert e.client.post(f"/approve/{rid}/assertion", json=body).json()["state"] == "HANDED_TO_GATEWAY"
    assert e.client.get(f"/approve/{rid}/challenge").json() == {"error": "APPROVAL_LOCKED"}


def test_token_expiry_and_forgery_are_refused(e):
    pk = e.enroll()
    rid = created(e)
    e.unlock(pk, rid)
    e.clock.t += 61
    assert e.client.get(f"/approve/{rid}/challenge").json() == {"error": "APPROVAL_LOCKED"}
    forged = TestClient(build_app(e.svc), base_url="https://127.0.0.1:8787")
    for value in ("x", b64url_encode(os.urandom(32)), ""):
        r = forged.get(f"/approve/{rid}/challenge", headers={"Cookie": f"{COOKIE}={value}"})
        assert r.json() == {"error": "APPROVAL_LOCKED"}


def test_token_from_request_a_is_refused_on_b(e):
    pk = e.enroll()
    a = created(e, cid="client-req-A001")
    b = created(e, cid="client-req-B002")
    token = cookie_of(e.unlock(pk, a)).split(";")[0].split("=", 1)[1]
    other = TestClient(build_app(e.svc), base_url="https://127.0.0.1:8787")
    hdr = {"Cookie": f"{COOKIE}={token}"}
    assert other.get(f"/approve/{b}/challenge", headers=hdr).json() == {"error": "APPROVAL_LOCKED"}
    assert other.post(f"/approve/{b}/assertion", headers=hdr, json={}).json() == {"error": "APPROVAL_LOCKED"}
    assert "display" in other.get(f"/approve/{a}/challenge", headers=hdr).json()  # still valid on A


def test_approval_still_requires_the_order_bound_assertion(e):
    pk = e.enroll()
    rid = created(e)
    o = e.client.post(f"/approve/{rid}/unlock/options").json()
    unlock_parts = pk.assert_(b64url_decode(o["challenge"]))
    e.client.post(f"/approve/{rid}/unlock", json={"unlock_id": o["unlock_id"], **to_wire(unlock_parts)})
    e.client.get(f"/approve/{rid}/challenge")
    r = e.client.post(f"/approve/{rid}/assertion", json={"decision": "APPROVE", **to_wire(unlock_parts)})
    assert (r.status_code, r.json()) == (403, {"error": "WEBAUTHN_CHALLENGE_MISMATCH"})
    assert e.svc.status(KEY, rid)["state"] == "PENDING_APPROVAL"


def test_decision_must_come_from_the_unlocking_credential(e):
    first, second = e.enroll(), e.enroll()
    rid = created(e)
    assert e.unlock(first, rid).status_code == 200
    v = e.client.get(f"/approve/{rid}/challenge").json()
    body = {"decision": "APPROVE", **to_wire(second.assert_(b64url_decode(v["challenge_approve"])))}
    assert e.client.post(f"/approve/{rid}/assertion", json=body).json() == {"error": "APPROVAL_CREDENTIAL_MISMATCH"}


def test_enrolment_requests_discoverable_credentials_and_lists_none(e):
    code = e.svc.issue_enrollment_code()
    opt = e.client.post("/enroll/options", json={"enrollment_code": code}).json()
    assert opt["authenticatorSelection"]["residentKey"] == "required"
    assert opt["authenticatorSelection"]["requireResidentKey"] is True
    assert "excludeCredentials" not in opt


# ---------------------------------------------------------------- DNS rebinding
def test_trusted_host_middleware_refuses_foreign_hosts(e):
    assert e.client.get("/healthz").status_code == 200
    for host in ("evil.example", "127.0.0.1.evil.example", "attacker.test:8787"):
        r = e.client.get("/healthz", headers={"Host": host})
        assert r.status_code == 400, host


def test_allowed_hosts_config(tmp_path):
    env = Env(tmp_path, allowed_hosts=("signer.tailnet.ts.net",))
    c = TestClient(build_app(env.svc), base_url="https://signer.tailnet.ts.net")
    assert c.get("/healthz").status_code == 200
    assert c.get("/healthz", headers={"Host": "127.0.0.1"}).status_code == 400
    env.svc.close()
    (tmp_path / "w").mkdir()
    with pytest.raises(ServiceConfigError):
        Env(tmp_path / "w", allowed_hosts=("*",))
    secret = tmp_path / "s.hex"
    secret.write_text("ab" * 32)
    base = {"SXBET_SIGNER_SERVICE_STATE_DIR": str(tmp_path / "st"), "SXBET_SIGNER_SERVICE_MCP_KEY_ID": "mcp-dev",
            "SXBET_SIGNER_SERVICE_MCP_SECRET_FILE": str(secret), "SXBET_SIGNER_SERVICE_MARKET_FILE": "m.json",
            "SXBET_SIGNER_SERVICE_OWNER_PUBLIC_KEY_HEX": "00" * 32}
    assert scfg.load(base)[0].allowed_hosts == ("127.0.0.1", "localhost")
    cfg = scfg.load({**base, "SXBET_SIGNER_SERVICE_ALLOWED_HOSTS": "a.example, b.example"})[0]
    assert cfg.allowed_hosts == ("a.example", "b.example")


# ---------------------------------------------------------------- MEDIUM: audit chain across processes
def test_two_store_instances_keep_one_valid_audit_chain(tmp_path):
    path = str(tmp_path / "s.sqlite3")
    a = st.ServiceStore(path, lambda: 1)
    b = st.ServiceStore(path, lambda: 2)   # e.g. the enroll-code CLI while `serve` runs
    a.audit("SERVICE_STARTED")
    b.audit("ENROLLMENT_CODE_ISSUED")
    a.audit("ENROLLMENT_REFUSED", reason="X")
    b.audit("ENROLLMENT_CODE_ISSUED")
    a.close()
    b.close()
    c = st.ServiceStore(path, lambda: 3)   # restart: must verify, not STORE_AUDIT_CHAIN_BROKEN
    assert [r["event"] for r in c.audit_rows()] == ["SERVICE_STARTED", "ENROLLMENT_CODE_ISSUED",
                                                   "ENROLLMENT_REFUSED", "ENROLLMENT_CODE_ISSUED"]
    c.close()


# ---------------------------------------------------------------- LOW items
def test_refusal_audit_writes_are_rate_limited_and_counted(e):
    for _ in range(50):
        e.client.post("/enroll/options", json={"enrollment_code": "guess"})
    rows = e.svc.store.audit_rows()
    assert sum(r["event"] == "ENROLLMENT_REFUSED" for r in rows) <= 10
    e.clock.t += 61
    e.client.post("/enroll/options", json={"enrollment_code": "guess"})
    rows = e.svc.store.audit_rows()
    supp = [r for r in rows if r["event"] == "AUDIT_SUPPRESSED"]
    assert supp and supp[-1]["detail"] == {"suppressed_event": "ENROLLMENT_REFUSED", "count": 40}


def test_unlock_refusal_audit_is_rate_limited_too(e):
    e.enroll()
    rid = created(e)
    for _ in range(30):
        o = e.client.post(f"/approve/{rid}/unlock/options").json()
        e.client.post(f"/approve/{rid}/unlock", json={"unlock_id": o["unlock_id"],
                                                       **to_wire(FakePasskey().assert_(b"\x00" * 32))})
    assert sum(r["event"] == "UNLOCK_REFUSED" for r in e.svc.store.audit_rows()) <= 10


def test_unlock_option_table_is_bounded(e):
    for _ in range(300):
        r = e.client.post("/approve/req_" + "1" * 32 + "/unlock/options")
    assert (r.status_code, r.json()) == (429, {"error": "UNLOCK_BUSY"})
    e.clock.t += 61
    assert e.client.post("/approve/req_" + "1" * 32 + "/unlock/options").status_code == 200


@pytest.mark.parametrize("label", ["Home‮FC", "A​B", "x⁦y", "﻿Z", "a b"])
def test_labels_with_format_or_separator_characters_are_refused(label):
    from test_sxbet_webauthn import binding_for  # noqa: E402
    from eth_account import Account
    from sxbet_signer_kit import make_intent
    b = binding_for(make_intent(Account.create().address))
    with pytest.raises(appr.ApprovalError) as exc:
        appr.build_display(b, operation_id="req-1", event_label=label, market_label="m", data_source="SIMULATED")
    assert str(exc.value) == "APPROVAL_DISPLAY_LABEL_INVALID"


def test_assertion_with_attested_credential_data_is_refused():
    pk = FakePasskey()
    policy = wa.WebAuthnPolicy("localhost", frozenset({"http://localhost:8787"}))
    chal = b"\x05" * 32
    cred = wa.verify_registration(expected_challenge=chal, policy=policy, **pk.register(chal))
    parts = pk.assert_(chal, attested=True)
    with pytest.raises(appr.ApprovalError) as exc:
        wa.verify_assertion(cred, policy=policy, challenge=chal, authenticator_data=parts["authenticator_data"],
                            client_data_json=parts["client_data_json"], signature=parts["signature"])
    assert str(exc.value) == "WEBAUTHN_MALFORMED"


def test_unknown_key_id_is_indistinguishable_from_bad_signature(e):
    from signer_service import auth
    raw = json.dumps({}).encode()
    stranger = auth.HmacKey("mcp-stranger", b"S" * 32)
    h = auth.client_headers(stranger, "POST", "/v1/requests/order", raw, timestamp=e.clock.t, nonce="n" * 20)
    r = e.client.post("/v1/requests/order", content=raw, headers=h)
    assert (r.status_code, r.json()) == (401, {"error": "AUTH_SIGNATURE_INVALID"})
