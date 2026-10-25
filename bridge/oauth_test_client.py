"""OAuth authorization-code/PKCE bootstrap and atomic refresh-token client.

Never log access, refresh, verifier, code or passwords. No resource-owner
password grant: the first authorization is interactive and consented.
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlencode
import httpx

PUBLIC = "https://football-consensus-browser-bridge.onrender.com"
RESOURCE = PUBLIC + "/mcp"

def pkce_pair():
    verifier=secrets.token_urlsafe(48)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier,challenge

def authorization_url(*, client_id, redirect_uri, state, challenge, scope="mcp:browser"):
    if not all((client_id,redirect_uri,state,challenge)):
        raise ValueError("MISSING_PKCE_FIELDS")
    return PUBLIC+"/oauth/authorize?"+urlencode({
       "client_id":client_id,"redirect_uri":redirect_uri,"response_type":"code",
       "scope":scope,"state":state,"code_challenge":challenge,
       "code_challenge_method":"S256","resource":RESOURCE,
    })

def validate_token_response(payload):
    if not isinstance(payload,dict) or payload.get("token_type","").lower()!="bearer":
        raise ValueError("INVALID_TOKEN_RESPONSE")
    if not all(isinstance(payload.get(k),str) and payload[k] for k in ("access_token","refresh_token")):
        raise ValueError("MISSING_TOKEN_PAIR")
    ttl=payload.get("expires_in")
    if not isinstance(ttl,int) or ttl<=0 or ttl>86400:
        raise ValueError("INVALID_EXPIRY")
    return payload

async def refresh(*,client_id,refresh_token,transport=None):
    if not client_id or not refresh_token:
        raise ValueError("MISSING_REFRESH_CREDENTIALS")
    async with httpx.AsyncClient(transport=transport,timeout=20,trust_env=False,follow_redirects=False) as client:
        response=await client.post(PUBLIC+"/oauth/token",data={
           "grant_type":"refresh_token","client_id":client_id,"refresh_token":refresh_token})
        if response.status_code!=200:
            raise ValueError("OAUTH_REFRESH_REJECTED")
        return validate_token_response(response.json())

async def exchange_code(*,client_id,code,redirect_uri,verifier,transport=None):
    if not all((client_id,code,redirect_uri,verifier)):
        raise ValueError("INCOMPLETE_AUTHORIZATION_CODE")
    async with httpx.AsyncClient(transport=transport,timeout=20,trust_env=False,follow_redirects=False) as client:
        response=await client.post(PUBLIC+"/oauth/token",data={
           "grant_type":"authorization_code","client_id":client_id,
           "code":code,"redirect_uri":redirect_uri,"code_verifier":verifier})
        if response.status_code!=200:
            raise ValueError("OAUTH_EXCHANGE_REJECTED")
        return validate_token_response(response.json())

def write_private_json(path: Path, payload:dict):
    """Atomic owner-only write, never print returned secrets."""
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name("."+path.name+"."+secrets.token_hex(8)+".tmp")
    fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as stream:
            json.dump(payload,stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp,path)
        os.chmod(path,0o600)
    finally:
        if tmp.exists():tmp.unlink()
