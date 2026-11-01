# Servizio signer (prototipo locale / dev)

Mandato owner `SXBET-SECURE-SIGNER-PREPARATION-01`. **Nessuna capacità finanziaria è eseguibile**: il servizio non ha alcun sender verso l'exchange, nessun client HTTP in uscita, firma solo con una chiave **effimera** generata in memoria all'avvio e consegna l'ordine firmato a un gateway che resta **disabilitato** (il flag `LIVE_TRADING` non viene mai caricato). I dati di mercato sono **SIMULATI**. **Non va mai distribuito** (né su Render né su altri host pubblici).

## Cosa fa

```
MCP (futuro client, NON incluso) ──HMAC──► POST /v1/requests/order|cancel   crea una RICHIESTA (intent)
                                           GET  /v1/requests/{id}           legge lo stato
telefono Android dell'owner (PWA) ───────► GET  /approve/{id}               pagina statica (stessi byte per ogni id, nessun dato)
                                           POST /approve/{id}/unlock/options challenge opaca di sblocco (nessun campo d'ordine)
                                           POST /approve/{id}/unlock        asserzione passkey → cookie di sessione
                                           GET  /approve/{id}/challenge     [sessione] binding + display + challenge, una volta
                                           POST /approve/{id}/assertion     [sessione, consumata] asserzione legata all'ordine
                                           GET  /enroll, POST /enroll/*     registrazione passkey con codice monouso
```

Ciclo di vita (SQLite append-only, `signer_service/store.py`):
`REQUESTED → PENDING_APPROVAL → APPROVED → SIGNED → HANDED_TO_GATEWAY` per gli ordini; `… → APPROVED → HANDED_TO_GATEWAY` per i cancel (nulla da firmare); `REJECTED` (rifiuto dell'owner, rifiuto del signer o del risk engine, riavvio) ed `EXPIRED` (TTL ≤ 120 s, applicato lato server). `HANDED_TO_GATEWAY` registra sempre il rifiuto del gateway (`LIVE_TRADING_DISABLED`); la firma viene scartata e non è mai persistita né restituita.

## Sicurezza

| Area | Implementazione |
|---|---|
| Isolamento | `import signer_service` eredita il guard di `signer` (`SXBET_SIGNER_PROCESS=1`, nessuna `RENDER`) e lo ricontrolla |
| Rete | solo in ingresso; bind `127.0.0.1` per default; un bind non loopback richiede `SXBET_SIGNER_SERVICE_ALLOW_NON_LOOPBACK_BIND=I_UNDERSTAND_PRIVATE_NETWORK_ONLY` |
| Chiave d'ordine | effimera (`secrets.token_hex(32)`), mai letta da env o file; il servizio **rifiuta di avviarsi** se `SXBET_SIGNER_PRIVATE_KEY` o `SXBET_LIVE_TRADING_FLAG` sono presenti |
| Rete blockchain | solo `testnet` (mainnet rifiutata all'avvio e per richiesta) |
| MCP → signer | HMAC-SHA256 su `FCB-SIGNER-HMAC-V1 \n metodo \n path?query \n sha256(body) \n timestamp \n nonce \n key_id`; skew ±30 s; confronto a tempo costante; nonce monouso in SQLite (sopravvive al riavvio); codici fissi (`AUTH_*`) |
| Minimo privilegio | la chiave MCP ha solo `request:create` e `request:read`: crea richieste e ne legge lo stato. Nessuna route accetta HMAC per approvare, firmare, registrare passkey o cambiare limiti (non esistono route per i limiti) |
| Contenuto della richiesta | il MCP fornisce solo mercato, esito, stake, quota limite, time in force e TTL. Maker, dominio, salt, `clientOrderId`, nonce di approvazione, etichette, fee e quota vista li fissa il servizio |
| Idempotenza | stesso `client_request_id` + stesso payload canonico ⇒ stessa richiesta (200); payload diverso ⇒ `409 DUPLICATE_REQUEST_CONFLICT` |
| Approvazione | passkey WebAuthn ES256 (P-256): challenge = SHA-256 dell'envelope canonico `{dominio, decisione, binding v2, display, nonce, issued_at, expires_at, signer_id}`; verifica in `signer/webauthn.py` (solo `cryptography`): tipo, challenge, origin in allowlist, `crossOrigin`, rpIdHash, flag UP e UV, signCount monotono, firma DER |
| Audit | tabella `audit` append-only con hash chain (trigger SQLite rifiutano UPDATE/DELETE); solo campi in allowlist; mai segreti HMAC, firme, asserzioni o nonce di approvazione. Il signer core scrive anche `signer-audit.jsonl` (`signer/audit.py`) |
| Nessun dettaglio prima dell'autenticazione | prima di tutto la pagina riceve solo l'id opaco e una challenge di sblocco casuale (legata alla richiesta, monouso, 60 s). Un'asserzione valida di una passkey registrata produce un token di sessione (32 byte casuali; in memoria solo lo SHA-256) consegnato in un cookie `HttpOnly; Secure; SameSite=Strict; Path=/approve/<id>`, valido 60 s, legato alla richiesta e alla credenziale che ha sbloccato, usabile per **una** lettura dei dettagli e **una** decisione. La decisione richiede comunque una seconda asserzione sulla challenge esatta dell'ordine, dalla stessa credenziale (`APPROVAL_CREDENTIAL_MISMATCH`). Prima dello sblocco ogni errore è generico (`APPROVAL_LOCKED`, `UNLOCK_REFUSED`) e non rivela se la richiesta esiste. Le passkey sono discoverable (`residentKey: required`): nessun elenco di credential id esce dal server (niente `allowCredentials`, niente `excludeCredentials`). Il riavvio invalida tutte le sessioni (fail closed) |
| DNS rebinding | `TrustedHostMiddleware` con allowlist dell'header `Host` (`SXBET_SIGNER_SERVICE_ALLOWED_HOSTS`, default `127.0.0.1,localhost`, nessun wildcard) |
| Audit degli errori non autenticati | al massimo 10 righe per tipo (`ENROLLMENT_REFUSED`, `UNLOCK_REFUSED`, `APPROVAL_ATTEMPT_REFUSED`) ogni 60 s; il resto è contato in una riga `AUDIT_SUPPRESSED {suppressed_event, count}`. Le challenge di sblocco in attesa sono limitate senza mai rifiutare: al massimo 4 per una richiesta esistente e 256 in tutto per id inesistenti, eliminando le più vecchie; un flood di id inventati non può quindi bloccare lo sblocco dell'owner (la risposta resta identica nei due casi). Le righe sospese vengono scritte anche alla chiusura del servizio |
| Pagina PWA | HTML/JS/CSS statici, nessun CDN, nessuno script inline, CSP `default-src 'none'; script-src 'self'…`, `textContent` per i dati, `Cache-Control: no-store` |

### Semantica SX Bet mostrata all'owner

SX Bet non ha BUY/SELL: un ordine **punta** un esito. `isMakerBettingOutcomeOne=true` punta l'esito 1 (`outcomeOneName`), `false` l'esito 2; "bancare" l'esito 1 equivale a puntare l'esito 2. `percentageOdds` è la probabilità implicita **massima** (prezzo peggiore) a cui l'ordine può essere abbinato: il limite di slippage è quindi imposto dall'ordine firmato stesso; lo slippage in bps mostrato è `(limite − quota vista) / quota vista` (DERIVED). La perdita massima è lo stake. Il ritorno lordo è `stake / p` prima delle commissioni (DERIVED); come le fee di `GET /user/fees-v3` si applichino al payout resta `DOCS_UNVERIFIED`.

## Uso locale (solo sviluppo)

```bash
export SXBET_SIGNER_PROCESS=1 PYTHONPATH=$PWD
python -m signer_service dev-init /percorso/dev        # materiale DEV usa e getta (file segreti 0600)
export SXBET_SIGNER_SERVICE_STATE_DIR=/percorso/state \
       SXBET_SIGNER_SERVICE_MCP_KEY_ID=mcp-dev \
       SXBET_SIGNER_SERVICE_MCP_SECRET_FILE=/percorso/dev/mcp-secret.hex \
       SXBET_SIGNER_SERVICE_MARKET_FILE=/percorso/dev/market.json \
       SXBET_SIGNER_SERVICE_OWNER_PUBLIC_KEY_HEX=$(cat /percorso/dev/owner.pub) \
       SXBET_SIGNER_RISK_CONFIG=/percorso/dev/risk-config.json \
       SXBET_SIGNER_SERVICE_ACCOUNT_ID=sx-dev-account
python -m signer_service enroll-code                    # codice monouso in <STATE_DIR>/enrollment-code.txt
python -m signer_service serve                          # http://127.0.0.1:8787
```

`dev-init` genera una chiave owner Ed25519 **usa e getta**, la usa una volta per firmare una config di rischio DEV con limiti minimi e la scarta: su disco resta solo la chiave pubblica. Nessun valore segreto viene stampato.

**Telefono Android:** WebAuthn richiede un contesto sicuro (HTTPS, oppure `http://localhost`). Per provare dal telefono senza esporre endpoint pubblici: un'origine HTTPS **privata** (per esempio `tailscale serve` sul tailnet dell'owner, che termina TLS e inoltra a `127.0.0.1`, oppure una VPN o la rete locale con un certificato dell'owner), con `SXBET_SIGNER_SERVICE_RP_ID` e `SXBET_SIGNER_SERVICE_ORIGINS` impostati su quell'host. Non è stato fatto alcun deploy e non va esposto alcun endpoint pubblico.

## Implementato / simulato / mancante

| | Stato |
|---|---|
| Servizio HTTP in ingresso, bind loopback, codici d'errore fissi | IMPLEMENTATO (solo dev) |
| Autenticazione HMAC MCP→signer con replay cache persistente e scope | IMPLEMENTATO |
| Store persistente append-only del ciclo di vita, idempotenza, TTL lato server, recovery fail-closed al riavvio | IMPLEMENTATO |
| Verifica WebAuthn registrazione (attestation `none`) e asserzione ES256 | IMPLEMENTATO (testato con asserzioni sintetiche, nessun browser reale nei test) |
| Binding v2 + display legato alla challenge | IMPLEMENTATO |
| Firma EIP-712 tramite `signer.core` | IMPLEMENTATO con chiave **effimera** usa e getta |
| Dati di mercato, quota, saldo, fee | **SIMULATI** (file locale) |
| Gateway | invocato e **sempre rifiutato** (`LIVE_TRADING_DISABLED`); nessun transport |
| Client MCP (lato bridge) e tool MCP di richiesta | MANCANTE (vietato in questa PR; il catalogo MCP resta invariato) |
| Provider di mercato reale, `RestSender`, flag `LIVE_TRADING`, chiave reale, KMS | MANCANTE per scelta (governance §2.6, decisioni dell'owner) |
| Prova dell'hardware della passkey (attestation) | MANCANTE: con `none` il modello del dispositivo non è dimostrato; la registrazione è un atto fidato dell'owner (codice monouso letto sull'host) |
| Revoca di una passkey | MANCANTE (le credenziali non si cancellano; serve una tabella di revoca append-only) |
| Prova manuale su Android Chrome reale | NON ESEGUITA (`NOT_TESTED`) |
| Contatori di esposizione e perdita tra riavvii | **GAP DA CHIUDERE prima di qualunque chiave reale**: il ledger è per indirizzo della chiave effimera (un file nuovo a ogni avvio) e `session_started_at` riparte a ogni avvio, quindi esposizione, ordini aperti e perdite di sessione/giornaliere ripartono da zero dopo un riavvio. Accettabile solo perché la chiave è usa e getta e nulla viene inviato; con una chiave reale serve un unico ledger persistente (eventualmente ancorato, `signer/anchor.py`) e l'esposizione letta dall'exchange |
| Rate limit delle chiamate (non solo dell'audit) | MANCANTE: il prototipo limita le righe di audit e la tabella delle challenge, non il numero di richieste |

Test: `tests/test_signer_service.py`, `tests/test_sxbet_webauthn.py`.
