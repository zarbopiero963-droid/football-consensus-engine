#!/usr/bin/env python3
"""One-time, interactive PKCE enrollment of an isolated certificate client.

No password grant or CLI token arguments. Bootstrap on a trusted local machine,
with Neon vault credentials scoped to the certification database only.
"""
from __future__ import annotations
import argparse
import asyncio
import getpass
import json
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import httpx
from bridge.oauth_test_client import PUBLIC,pkce_pair,authorization_url,exchange_code,write_private_json
from bridge.oauth_refresh_vault import provision

STATE=Path.home()/".config/fcb-cert/pkce-bootstrap.json"
REDIRECT="http://127.0.0.1:8765/oauth/callback"

async def begin():
    import os
    registration_secret=os.getenv("FCB_CERT_OAUTH_REGISTRATION_SECRET","")
    if not registration_secret:
        raise ValueError("MISSING_CERT_REGISTRATION_SECRET")
    async with httpx.AsyncClient(trust_env=False,timeout=20,follow_redirects=False) as client:
        r=await client.post(PUBLIC+"/oauth/cert-register",json={"redirect_uri":REDIRECT},
                            headers={"X-FCB-Cert-Registration":registration_secret})
        if r.status_code!=201:
            raise ValueError("CERT_CLIENT_REGISTRATION_UNAVAILABLE")
        client_id=r.json()["client_id"]
    verifier,challenge=pkce_pair()
    state=secrets.token_urlsafe(32)
    write_private_json(STATE,{"client_id":client_id,"verifier":verifier,"state":state})
    print("Open this authorization URL in your browser and approve once:")
    print(authorization_url(client_id=client_id,redirect_uri=REDIRECT,
                            state=state,challenge=challenge))
    print("The browser may show a local connection error at the callback URL.")
    print("Copy only that callback URL into the local terminal (never into chat).")

async def finish():
    if not STATE.is_file():
        raise ValueError("PKCE_BOOTSTRAP_NOT_FOUND")
    prior=json.loads(STATE.read_text())
    callback=getpass.getpass("Paste callback URL (input hidden): ")
    u=urlparse(callback)
    if (u.scheme,u.hostname,u.port,u.path)!=("http","127.0.0.1",8765,"/oauth/callback"):
        raise ValueError("INVALID_REDIRECT")
    params=parse_qs(u.query)
    if params.get("state")!=[prior["state"]] or len(params.get("code",[]))!=1:
        raise ValueError("PKCE_STATE_MISMATCH")
    result=await exchange_code(client_id=prior["client_id"],code=params["code"][0],
                               redirect_uri=REDIRECT,verifier=prior["verifier"])
    provision(prior["client_id"],result["refresh_token"])
    STATE.unlink()
    print("OAuth certification client provisioned in encrypted database vault.")
    print("Set FCB_OAUTH_CLIENT_ID as a GitHub environment secret with the following public client ID:")
    print(prior["client_id"])

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=("begin","finish"))
    action=parser.parse_args().action
    try:
        asyncio.run(begin() if action=="begin" else finish())
    except (ValueError,KeyError,httpx.HTTPError):
        raise SystemExit("NO_GO: PKCE enrollment failed; no secrets printed.")
