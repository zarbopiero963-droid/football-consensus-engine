"""Encrypted, client-isolated refresh-token vault for automated MCP certification.

Secrets reside only as Fernet ciphertext in the configured PostgreSQL database.
An exclusive PostgreSQL row lock serializes refresh exchanges across CI runs.
Provisioning requires a prior interactive PKCE grant.
"""
from __future__ import annotations
import os
from contextlib import contextmanager
from cryptography.fernet import Fernet, InvalidToken
import psycopg

_CREATE = """CREATE TABLE IF NOT EXISTS fcb_cert_oauth_vault (
    client_id TEXT PRIMARY KEY,
    encrypted_refresh BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)"""

def _settings():
    dsn=os.environ.get("FCB_CERT_VAULT_DATABASE_URL","")
    key=os.environ.get("FCB_CERT_VAULT_FERNET_KEY","")
    if not dsn or not key:
        raise ValueError("CERT_VAULT_NOT_CONFIGURED")
    try:
        cipher=Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ValueError("INVALID_VAULT_KEY") from exc
    return dsn,cipher

def provision(client_id:str, refresh_token:str, *, connection=None):
    if not client_id or not refresh_token:
        raise ValueError("INVALID_PROVISIONING_INPUT")
    dsn,cipher=_settings()
    with (connection if connection is not None else psycopg.connect(dsn)) as conn:
        with conn.cursor() as cur:
            cur.execute(_CREATE)
            cur.execute("""INSERT INTO fcb_cert_oauth_vault (client_id,encrypted_refresh)
                VALUES (%s,%s)
                ON CONFLICT (client_id) DO UPDATE SET encrypted_refresh=EXCLUDED.encrypted_refresh,
                  updated_at=NOW()""",
                (client_id,cipher.encrypt(refresh_token.encode("utf-8"))))
        conn.commit()

async def rotate_in_vault(client_id:str, exchange, *, connection=None):
    """Exactly one exchange per client at a time; rollback keeps old credential.

    Exchange must be awaited while SELECT FOR UPDATE holds the row lock.
    If the upstream provider consumed the old token then the transaction fails
    before commit, explicit reauthorization is required (never retry blindly).
    """
    if not client_id:
        raise ValueError("MISSING_CLIENT_ID")
    dsn,cipher=_settings()
    conn=connection if connection is not None else psycopg.connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT encrypted_refresh FROM fcb_cert_oauth_vault WHERE client_id=%s FOR UPDATE",
                            (client_id,))
                row=cur.fetchone()
                if not row:
                    raise ValueError("OAUTH_VAULT_CLIENT_NOT_PROVISIONED")
                try:
                    previous=cipher.decrypt(bytes(row[0])).decode("utf-8")
                except (InvalidToken,UnicodeError) as exc:
                    raise ValueError("OAUTH_VAULT_DECRYPTION_FAILED") from exc
                updated=await exchange(client_id=client_id,refresh_token=previous)
                new_refresh=updated.get("refresh_token")
                if not isinstance(new_refresh,str) or not new_refresh:
                    raise ValueError("INVALID_ROTATED_TOKEN")
                cur.execute("""UPDATE fcb_cert_oauth_vault
                               SET encrypted_refresh=%s, updated_at=NOW()
                               WHERE client_id=%s""",
                            (cipher.encrypt(new_refresh.encode("utf-8")),client_id))
            conn.commit()
            return updated["access_token"]
    finally:
        if connection is None:
            conn.close()
