# SX Bet nel server MCP: modulo READ_ONLY

Fonte tecnica: OpenAPI ufficiale **https://docs.sx.bet/openapi.json** (SX Bet API, route V3) e documentazione https://docs.sx.bet. Ogni tool corrisponde a una route GET di quella specifica; nessun parametro è inventato.

## Stato
| Modalità | Stato |
|---|---|
| `READ_ONLY` | **implementata**: 36 tool `sxbet_*` (tutte le 36 operazioni GET ufficiali) |
| `SIMULATION` | non implementata |
| `LIVE_TRADING` | **non disponibile**. Per la governance §2.6 non può essere attivata finché non esiste ed è testato un signer isolato con conferma fuori banda, verificata dal signer, per ogni singolo ordine |

Garanzia strutturale: il client (`bridge/sxbet/client.py`) implementa solo GET. Non esistono percorsi di codice per creare, eseguire o cancellare ordini, né per approval, transfer o firma. Un test (`tests/test_sxbet_read_only.py::test_no_write_capability_exists`) lo verifica.

## Configurazione (Render, servizio MCP)
| Variabile | Default | Uso |
|---|---|---|
| `SXBET_ENABLED` | spento | registra i tool `sxbet_*` |
| `SXBET_NETWORK` | `mainnet` | `mainnet` (`https://api.sx.bet`, chain 4162) oppure `testnet` (`https://api.toronto.sx.bet`) |
| `SXBET_API_KEY` | vuoto | inviata solo come header `x-sx-api-key` alle route autenticate. Senza chiave quei tool restituiscono `KEY_NOT_CONFIGURED`, senza chiamate di rete |
| `SXBET_MAX_RPM` | 60 | limite locale di richieste al minuto, più restrittivo dei limiti del provider |

⚠️ Un **GitHub Actions secret non arriva al runtime Render**. Per i tool autenticati, `SXBET_API_KEY` va impostata nelle variabili d'ambiente del servizio Render.

La chiave SX Bet permette di **cancellare tutti gli ordini** dell'account (lo dice la documentazione SX Bet). Va trattata come un secret di alto valore.

### Permessi della chiave (fonte: https://docs.sx.bet/developers/api-key-permissions)
La documentazione ufficiale dice: "Each API key can call only the routes for the scopes you select when you create it". Per i tool di questo modulo bastano tre scope di **sola lettura**:

| Scope SX Bet | Route usate | Tool |
|---|---|---|
| `Market data: read` | `GET /orderbook-v3/snapshot/market`, `GET /orderbook-v3/snapshot/event`, `GET /orders-v3/odds/best` | `sxbet_orderbook_market`, `sxbet_orderbook_event`, `sxbet_best_odds` |
| `Orders: read` | `GET /orders-v3` | `sxbet_open_orders` |
| `Account: read` | `GET /user/balance-v3`, `GET /user/fees-v3`, `GET /positions-v3`, `GET /trades-v3`, `GET /fills-v3`, `GET /ledger-v3` | `sxbet_balance`, `sxbet_fees`, `sxbet_positions`, `sxbet_trades`, `sxbet_fills`, `sxbet_ledger` |

Il modulo non richiede `Orders: create`, `Orders: cancel`, `Account: write` né alcuno scope `Wallet: *`. Una chiave senza questi scope riduce il danno in caso di fuga. Se manca uno scope di lettura, il tool risponde `SXBET_UNAUTHORIZED` (HTTP 401/403) senza esporre il body. La documentazione indica che è attiva una sola chiave alla volta: cambiare gli scope significa generare una nuova chiave, e lo decide l'owner.

## Inventario ufficiale (01-11-2026)
Fonte: `https://docs.sx.bet/openapi.json` (SX Bet API 1.0.0), con gli scope presi da `developers/api-key-permissions`. File: `docs/sxbet/official_api_inventory.json`, che riporta per ogni operazione metodo, path, auth, scope, parametri, classificazione e tool MCP.

| Classe | Operazioni | MCP |
|---|---:|---|
| `PUBLIC_READ` (GET senza chiave) | 13 | tutte esposte |
| `AUTHENTICATED_READ` (GET con `x-sx-api-key`) | 23 | tutte esposte |
| `STATE_MUTATION` (cancellazioni, heartbeat, privacy, proxy e multisig admin) | 15 | **non esposte** |
| `REAL_FUNDS_RISK` (creazione ordini, depositi, prelievi e trasferimenti) | 6 | **non esposte** |
| **Totale** | **57** (54 path) | 36 GET → 36 tool, perché `sxbet_leagues` copre 2 route e `sxbet_health` non chiama il provider |

Operazioni documentate fuori dall'OpenAPI:
- `GET /user/realtime-token-v3/api-key`: restituisce una credenziale, quindi non è esposta come tool e la usa solo il probe realtime.
- `GET /positions-v3/settled/pnl`: compare negli scope `Account: read`, ma l'OpenAPI non ne riporta i parametri (`DOCS_UNVERIFIED`), quindi non è implementata.

Route di lettura aggiunte il 01-11-2026:
- **Orders: read:** `sxbet_order` e `sxbet_order_by_client_id`.
- **Account: read:** `sxbet_trade` e `sxbet_privacy`.
- **Market data: read:** `sxbet_public_market_orders`, `sxbet_public_user_orders`, `sxbet_public_positions`.
- **Wallet: read:** `sxbet_proxy_wallet`, `sxbet_proxy_deploy_pending`, `sxbet_proxy_funding_pending`, `sxbet_proxy_funding_status`, `sxbet_proxy_address_check`, `sxbet_safe_info`.

Per i tool `Wallet: read` la chiave deve avere quello scope. Senza scope il tool risponde `SXBET_UNAUTHORIZED` e il workflow registra `BLOCKED_EXTERNAL`.

## Realtime (WebSocket)
- Endpoint: `wss://realtime.sx.bet/connection/websocket` (Centrifugo v6.9.1). Il token si ottiene con `GET /user/realtime-token-v3/api-key`.
- Canali pubblici (8): `orderbook_v3:{marketHash}`, `orderbook_v3_event:{eventId}`, `best_odds_v3:global`, `recent_trades_v3:global`, `markets:global`, `main_line:global`, `fixtures:global` / `fixtures:live_scores`, `parlay_markets:global`.
- Canali dell'account (3): `account:orders_v3_#{address}`, `account:trades_v3_#{address}`, `account:fills_v3_#{address}`.
- Limiti: 512 sottoscrizioni per connessione, ping ogni 30 s, timeout del pong 8 s, coda in uscita di 1 MB. La history per namespace è documentata in `developers/realtime-reference`.
- Il realtime è verificato dal workflow **SX Bet Full API Read-Only Audit**, che resta in ascolto per un tempo limitato e poi si disconnette. Il server MCP non tiene connessioni persistenti.

## Strumenti
- **Pubblici** (nessuna chiave):
  - `sxbet_health` (0 chiamate), `sxbet_sports`, `sxbet_leagues(sport_id, active_only)`;
  - `sxbet_fixtures_active(league_id)`, `sxbet_fixture_status(sportx_event_ids≤30)`, `sxbet_live_scores(sportx_event_ids≤30)`;
  - `sxbet_markets_active(...)`: paginazione con `next_key` → `pagination_key`, `page_size` ≤100; una pagina corta non è necessariamente l'ultima; `type` e `bet_group` si escludono a vicenda;
  - `sxbet_markets_find(market_hashes≤30)`, `sxbet_markets_popular`;
  - `sxbet_metadata`: dominio EIP-712, base token, step della scala quote, limiti di dimensione;
  - `sxbet_search(query)`, `sxbet_teams(...)`, `sxbet_public_trades(...)`.
- **Autenticati, sola lettura** (`x-sx-api-key`):
  - `sxbet_orderbook_market`, `sxbet_orderbook_event`, `sxbet_best_odds(market_hashes≤30)`;
  - `sxbet_balance`, `sxbet_fees`;
  - `sxbet_positions(status=[MATCHED|LOCKED|SETTLED|FAILED])`, `sxbet_open_orders`;
  - `sxbet_trades`, `sxbet_fills`, `sxbet_ledger`.

**Unità:** `percentageOdds` è la probabilità implicita × 10^20 (per esempio `52500000000000000000` = 52,5%). Gli importi USDC sono in unità base con 6 decimali (`1000000` = 1 USDC). I dati sono restituiti così come arrivano dal provider.

**Errori** (codici fissi, mai il body del provider né la chiave): `INVALID_INPUT`, `KEY_NOT_CONFIGURED`, `SXBET_RATE_LIMITED` (con `retry_after_seconds`), `SXBET_LOCAL_RATE_LIMITED`, `SXBET_UNAUTHORIZED`, `SXBET_HTTP_ERROR`, `SXBET_REDIRECT_REJECTED`, `SXBET_TIMEOUT` (scadenza complessiva di 30 s), `SXBET_NETWORK_ERROR`, `SXBET_INVALID_JSON`, `SXBET_PROVIDER_STATUS_NOT_SUCCESS`, `SXBET_RESPONSE_TOO_LARGE`, `PAYLOAD_TOO_LARGE`. Non ci sono retry automatici. Il cap di 256 KiB si misura sul JSON compatto della risposta; oltre il cap la risposta è `PAYLOAD_TOO_LARGE`, senza troncature.

**Rate limit:** il limiter `SXBET_MAX_RPM` vale **per processo**. Oggi il servizio Render gira con 1 istanza e `WEB_CONCURRENCY=1`. Se si scala a più worker o istanze, il ritmo effettivo si moltiplica: in quel caso va spostato sul coordinatore condiviso PostgreSQL (governance §2.4). Decisione dell'owner.

## Verifica (01-11-2026, backend contro l'API pubblica reale, senza chiave)
| Prova | Esito |
|---|---|
| `sxbet_metadata` | PASS: dominio `OBv3 Escrow` v1, chain 4162, minimo d'ordine 1 USDC, `maxCreateOrders` 10, `maxCancelOrders` 100 |
| `sxbet_markets_active` su 2 pagine con `pagination_key` | PASS: 3 + 3 mercati, nessuna sovrapposizione |
| `sxbet_markets_find` (3 hash) | PASS: 3 su 3 |
| `sxbet_public_trades`, `sxbet_fixture_status` | PASS |
| route autenticata senza chiave | PASS: `KEY_NOT_CONFIGURED`, 0 chiamate |
| route autenticate con chiave | **NOT_TESTED** dal backend: la chiave esiste solo su Render; si verifica dal client MCP |
| registrazione in produzione (`/healthz`, deploy delle 09:25:52 UTC) | PASS: `sxbet_registered_count` 23, `sxbet_key_configured` true |
| `tools/list` offline con SX Bet acceso | PASS: 99 tool (34 browser + 42 API-FOOTBALL + 23 SX Bet), nessuna paginazione |

## Diagnosi: un client vede 76 tool e nessun `sxbet_*`
76 = 34 browser + 42 API-FOOTBALL: è il catalogo di un processo avviato **senza** `SXBET_ENABLED`. Le variabili d'ambiente Render valgono solo per i processi avviati dopo il salvataggio e un nuovo deploy. Un client che ha letto `tools/list` prima di quel deploy, o che conserva il catalogo in cache, continua a mostrare 76 tool.

Come verificarlo senza credenziali:
- `GET /healthz` riporta `deploy_commit`, `mcp_tools_total` e `mcp_tools_by_component`, calcolati sullo stesso catalogo servito da `tools/list` nel processo che risponde.
- All'avvio ogni processo scrive nei log la riga `MCP_TOOL_REGISTRY total=… browser=… apifootball=… sxbet=… commit=…`.

Se il server riporta il conteggio atteso di `sxbet` e il client ne mostra 0, il problema è nel catalogo in cache del client: va aggiornato il connettore nel client (refresh, oppure disconnessione e nuova connessione).


## Prossimi passi (decisioni owner)
1. **Lettura attiva** su Render dal 01-11-2026 09:25:52 UTC. Resta da verificare dal client MCP, dopo l'aggiornamento del connettore.
2. **SIMULATION:** validazione degli ordini senza invio (mercato attivo, scala quote, minimi, liquidità dall'order book).
3. **LIVE_TRADING:** progettare il signer isolato EIP-712, primary type `Order` con 8 campi e dominio da `/metadata/obv3`, con conferma fuori banda per ogni ordine. Va valutata anche la gestione del proxy wallet (`/user/deploy-proxy`, `/user/transfer-to-proxy`), che è a sua volta un'operazione finanziaria (§2.6). Ogni passo verso il trading reale si mergia solo dall'owner.
