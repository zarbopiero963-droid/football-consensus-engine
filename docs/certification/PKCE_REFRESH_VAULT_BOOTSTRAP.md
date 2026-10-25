# FCB-MCP — PKCE unattended certification client (PR #124)

## Architecture (exact implementation)

- The existing public authorization server uses `authorization_code` + PKCE S256 and refresh-token rotation; `MCP_OAUTH_PASSWORD` remains **only on Render**, on the consent page. It is never copied to GitHub Actions.
- The certification client has its own public `fcbe2e1_` client identifier, valid for 30 days, with a **loopback-only callback** `http://127.0.0.1:8765/oauth/callback`. Existing ChatGPT/Claude clients are unchanged.
- The opt-in `/oauth/cert-register` endpoint is **404 by default**. Opening it requires both `FCB_CERT_OAUTH_REGISTRATION_ENABLED=true` and the separate `FCB_CERT_OAUTH_REGISTRATION_SECRET` passed in `X-FCB-Cert-Registration`. Disable registration immediately after bootstrap.
- First authorization is interactive via the owner's normal OAuth consent page and **the owner's existing Render password**. The bootstrap script uses a high-entropy PKCE verifier and state; the callback URI is never logged.
- After exchanging the one-time code, the refresh token is encrypted with Fernet and stored in `fcb_cert_oauth_vault` in an **isolated PostgreSQL role/database**. Each subsequent GitHub run uses `FCB_OAUTH_CLIENT_ID`, `FCB_CERT_VAULT_DATABASE_URL` and `FCB_CERT_VAULT_FERNET_KEY` from the protected GitHub environment; no static access token or OAuth password.
- Refresh rotation holds a PostgreSQL `SELECT ... FOR UPDATE` client-row lock until the remote `/oauth/token` exchange returns, then commits the new encrypted refresh token. Server-side consumption is restricted to the requesting client **within the same atomic SQL update**. Invalid/expired/reused credentials fail closed.
- Administrative revocation occurs **offline** through `oauth_store.revoke_cert_client(client_id)`, which invalidates access and refresh tokens and deletes the client registration. This operation is deliberately not an unauthenticated MCP or HTTP tool.

## One-time setup after the change is safely deployed

1. Create a private certification DB role/schema with narrowly scoped access and a separate Fernet key, never reusing the production `DATABASE_URL` indiscriminately. Provision the required tables using an authorized migration.
2. Temporarily enable `FCB_CERT_OAUTH_REGISTRATION_ENABLED=true` on Render and set an independent high-entropy `FCB_CERT_OAUTH_REGISTRATION_SECRET`. Never reuse `MCP_OAUTH_PASSWORD` or `MCP_BRIDGE_TOKEN` as this secret.
3. On a trusted local environment with vault access, configure `FCB_CERT_OAUTH_REGISTRATION_SECRET`, `FCB_CERT_VAULT_DATABASE_URL`, `FCB_CERT_VAULT_FERNET_KEY`; run `python scripts/provision_cert_oauth.py begin`. Approve the URL once using the existing OAuth password. Copy the callback URL into `python scripts/provision_cert_oauth.py finish` locally (prompt does not echo it).
4. Immediately disable the Render registration flag and remove the temporary registration secret. Put the **public client ID** in protected GitHub Actions environment `FCB_OAUTH_CLIENT_ID`; configure the private database URL and Fernet key as protected environment secrets, without exposing token values to logs.
5. Trigger `fcb-global-e2e-one-command` on the correct branch with explicit LIVE authorization and verified deployed SHA. Refuse the certificate unless the actual 76 method tests, documentary contract matrix, independence review and runtime recovery have proofs.

## Security and recovery

The refresh client never writes tokens to repository files or CI artifacts. A credential-exchange failure is `NO_GO`; don't automatically retry a possibly consumed refresh token. A crash between provider consumption and encrypted DB commit can require another user consent: this is a fail-closed recovery limitation, not a guaranteed zero-touch path. The Fernet key must be backed up using an authorized secret store. To rotate the key, re-encrypt existing rows before removing the old key.

**Current status:** source code and regression tests are added to PR #124; the new OAuth path is not deployed until authorized merge. No new credentials or production vault have been provisioned here. No live authenticated certification or client revocation test against production has been performed. Do not mark the PR CERTIFIED.
