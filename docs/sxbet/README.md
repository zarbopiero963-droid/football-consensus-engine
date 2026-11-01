# SX Bet nel server MCP: modulo READ_ONLY

Fonte tecnica: OpenAPI ufficiale **https://docs.sx.bet/openapi.json** (SX Bet API, route V3) e documentazione https://docs.sx.bet. Ogni tool corrisponde a una route GET di quella specifica; nessun parametro è inventato.

## Stato
| Modalità | Stato |
|---|---|
| `READ_ONLY` | **implementata**: 23 tool `sxbet_*` |
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
| route autenticate con chiave | **NOT_TESTED**: serve `SXBET_API_KEY` su Render |
| via client MCP | **NOT_TESTED**: serve `SXBET_ENABLED=true` su Render |

## Prossimi passi (decisioni owner)
1. **Attivare la lettura:** impostare `SXBET_ENABLED=true` e `SXBET_API_KEY` sul servizio Render, poi ricollegare il connettore.
2. **SIMULATION:** validazione degli ordini senza invio (mercato attivo, scala quote, minimi, liquidità dall'order book).
3. **LIVE_TRADING:** progettare il signer isolato EIP-712, primary type `Order` con 8 campi e dominio da `/metadata/obv3`, con conferma fuori banda per ogni ordine. Va valutata anche la gestione del proxy wallet (`/user/deploy-proxy`, `/user/transfer-to-proxy`), che è a sua volta un'operazione finanziaria (§2.6). Ogni passo verso il trading reale si mergia solo dall'owner.
