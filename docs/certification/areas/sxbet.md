# Phase A: reconciliazione SX Bet e signer (code-first, read-only)

Data: 2026-11-02. `origin/main` = `342fed6`. Il checkout locale (`11729cb`, ramo API-FOOTBALL) non ha diff rispetto a `origin/main` su `bridge signer tests docs evidence`.
Vincoli rispettati: nessuna modifica al repo, nessun push, nessuna chiamata alle API di SX Bet, nessun segreto stampato. Una sola GET pubblica a `/healthz` e una POST senza token a `/mcp` sul nostro Render.
Dettaglio per riga: `/tmp/claude-0/phaseA/sxbet.csv` (61 SX + 35 SG).

## 1. Fatti verificati

- **Tool `sxbet_*`: 46 (VERIFIED)**, contati con `register_sxbet_tools` (fake server). Anche Render `/healthz` riporta `sxbet_registered_count=46` su `deploy_commit=342fed64682d`, `POSTGRES_SHARED`, chiave configurata; `/mcp` senza token risponde 401.
  - Il vecchio "45 read/DERIVED + 1 simulation" è impreciso. Ripartizione reale:
    - 35 READ 1:1 GET: 13 pubblici e 22 autenticati;
    - 2 STATUS senza chiamata: `sxbet_health`, `sxbet_risk_status`;
    - 1 SIMULATION: `sxbet_simulate_order`;
    - 5 DERIVED read: `order_status`, `positions_summary`, `exposure_pnl`, `resting_orders_scope`, `reconciliation_diagnostics`;
    - 3 SIMULATION/DERIVED: `hedge_simulation`, `close_position_preview`, `rules_evaluate`. `sxbet_health` li chiama `simulation_tools` insieme a `simulate_order`.
  - **Nessuna capacità di scrittura:** il client ha solo GET. Le 21 operazioni non-GET dell'inventario sono tutte `NOT_EXPOSED`.
  - L'inventario ufficiale `docs/sxbet/official_api_inventory.json` ha 57 operazioni: 36 GET coperte da 35 tool (`sxbet_leagues` copre `/leagues` e `/leagues/active`) e 21 POST/DELETE.
    - Tutti i parametri dell'inventario sono dichiarati nei tool (confronto automatico: 0 mancanti).
    - `/positions-v3/settled/pnl` è DOCS_UNVERIFIED, quindi BLOCKED.
    - `/user/realtime-token-v3/api-key` non è esposto, per scelta.
- **Test offline su main:** `tests/test_sxbet_*.py` → 728 passed. Integrazione PG16 locale (`test_sxbet_rate_coordinator_postgres`, `test_signer_ledger_anchor_postgres`) → 17 passed.
- **Audit live "SX Bet Full API Read-Only Audit":** run 41320538088, 2026-11-01, mainnet, sul runner GitHub (**non Render**), head `d569e6e`.
  - `bridge/sxbet`, lo script e `signer` sono invariati tra `d569e6e` e `342fed6`.
  - Esiti: PASS 46, EMPTY_VALID 13, NOT_TESTED 3, PARTIAL 1 (copertura tool 43/46); `write_operations_attempted=0`.
  - Coordinatore usato: **LOCAL_PROCESS**, non PG. Realtime: 7 canali sottoscritti.
  - **Il report NON è nel repo:** `evidence/` contiene solo `api_football`. Esiste solo come artefatto GitHub, che **scade il 2026-12-01**. Lo stato dato è quindi `REAL_PROVIDER_VERIFIED`, con evidenza riproducibile (workflow e script nel repo) ma report non archiviato.
- **Nessuna evidenza REAL_MCP_CLIENT** (ChatGPT/Claude.ai via OAuth) per i tool `sxbet_*`. PRODUCTION_VERIFIED vale solo per la registrazione dei tool.

## 2. Conteggi per livello (num/den, il livello più alto raggiunto)

| Livello | Tool (46) | SX totale (60, esclusa 1 N/A) | SG main | SG solo branch |
|---|---|---|---|---|
| PRODUCTION_VERIFIED | 0/46 (registrazione sì) | 1/60 | 0 | 0 |
| REAL_MCP_CLIENT_VERIFIED | 0/46 | 0/60 | 0 | 0 |
| REAL_PROVIDER_VERIFIED | 41/46 | 44/60 | 0 | 0 |
| INTEGRATION_VERIFIED | 0 | 1/60 | 1 (anchor PG) | 0 (PG e2e 12/12 sul branch) |
| OFFLINE_VERIFIED | 5/46 | 11/60 | 15 | 6 |
| MOCK_VERIFIED | 0 | 0 | 0 | 1 (SimExchange e2e) |
| NOT_IMPLEMENTED | 0 | 2/60 | 8 (3 sono **difetti di main**) | – |
| BLOCKED | 0 | 1/60 | 4 | – |

- Valori cumulativi dei tool: ≥ REAL_PROVIDER 41/46; ≥ OFFLINE 46/46.
- Con un criterio stretto ("solo evidenza committata nel repo") i 41 tool scendono a OFFLINE_VERIFIED.
- Tool live NOT_TESTED: `public_user_orders`, `public_positions`, `proxy_funding_status`.
- Senza un test offline dedicato alla forma della richiesta (solo il controllo generico dell'inventario): `fixtures_active`, `live_scores`, `orderbook_market`, `orderbook_event`, `best_odds`, `open_orders`, `fills`.
- I DERIVED sono provati live solo su un account **vuoto** (saldo, posizioni, ordini, trade e fill EMPTY_VALID).

## 3. Branch (non su main; nessuna PR aperta)

| Branch | Head | Merge-base | ahead/behind | Conflitti con main | Test (worktree) |
|---|---|---|---|---|---|
| feat/sxbet-signer-service-approval | 7bcb1f8 | d569e6e | +3/−8 | nessuno (merge-tree pulito) | 111 nuovi passed; suite sxbet+signer+governance 843 passed |
| test/sxbet-e2e-simulated-engine | a510bd8 | d569e6e | +2/−8 | nessuno | 188 passed, 5 skipped (PG); con PG16 locale `test_signer_e2e_postgres` + anchor 12 passed; suite 831 passed |
| feat/sxbet-kms-digest-signer | 86a9d08 | d569e6e | +1/−8 | nessuno | 36 passed; suite 768 passed |
| feat/sxbet-testnet-toronto-profile | 22ac268 | d569e6e | +2/−8 | nessuno | 90 passed; suite 822 passed |

- Conflitti tra branch: nessuno. Toccano gli stessi file `signer/README.md` (3 branch) e `mcp_chat_trading_workflow.md` (2 branch).
- I branch sono indietro rispetto a main solo per file CI/Aiven, non per file SX Bet.

Cosa implementa ogni branch:
- **signer-service-approval** (prototipo dev, 28 file, +4105 righe):
  - servizio Starlette solo in loopback, con HMAC MCP→signer, replay cache persistente e scope minimi;
  - passkey WebAuthn ES256 (Android/PWA), con verificatore in `signer/webauthn.py`;
  - **auth prima del display**: sessione di unlock monouso da 60 s, nessun dettaglio prima dell'autenticazione, errori generici;
  - **seconda asserzione sulla challenge esatta dell'ordine**, dalla stessa credenziale;
  - audit SQLite append-only (trigger) con hash chain;
  - Host allowlist e limite delle sole righe di audit;
  - chiave d'ordine effimera; gateway sempre `LIVE_TRADING_DISABLED`; mainnet rifiutata.
  - **Mancano:** revoca delle passkey, rate limit delle richieste, test su un Android reale, client lato MCP. Contatori di esposizione e perdita persistenti tra riavvii: GAP dichiarato.
- **e2e-simulated-engine:**
  - SimExchange in memoria con fault injection; e2e intent → approvazione → firma → gateway → riconciliazione → settlement;
  - `signer/monitor.py`: letture sotto il lock del gateway, policy di heartbeat senza sender, proposte;
  - e2e su PG: fencing e process kill;
  - **3 correzioni a `signer/orders.py`**. RED riprodotto oggi: i test del branch eseguiti contro `orders.py` di main danno **9 failed**.
- **kms-digest-signer:** adapter DIGEST senza rete (DER → r‖s‖v, low-s, recovery per tentativi, firma RAW rifiutata) e analisi AWS/GCP/Azure/HSM. Non collegato a `core`; nessun backend.
- **testnet-toronto-profile:** profili fissati mainnet (4162) e Toronto (79479957), più una guardia cross-network (chain, escrow, USDC, host, schema) e un doc di readiness. **Non collegato** a `core`/`gateway`: lo importano solo i test.

## 4. Gap con evidenza

1. **P1. Difetti nel ledger di main** (SG-017..019):
   - un `FILLED` con riga purgata non arriva mai a `SETTLED` (stake bloccato, perdita mai contata);
   - un fill tardivo su `FILLED` dà `ILLEGAL_TRANSITION`;
   - il dedupe delle evidenze di assenza blocca `SIGNED` → `EXPIRED_UNCONFIRMED` e il secondo invio perso.
   - Non ha impatto oggi (nessun invio possibile), ma è un bloccante prima di qualunque testnet.
2. **P1.** Report dell'audit live non archiviato nel repo (scade il 2026-12-01).
3. **P1, solo branch.** Revoca delle passkey assente. Esposizione e perdite del servizio signer non persistenti tra riavvii.
4. **P1.** Giurisdizione Italia: `BLOCKED_LEGAL_VERIFICATION`.
5. **P2.**
   - Nessuna evidenza REAL_MCP_CLIENT né di chiamate in produzione ai tool.
   - Il coordinatore PG non è mai stato attraversato da traffico reale verso il provider.
   - La matematica DERIVED è provata solo offline e su un account vuoto.
   - Unità di `fillAmount` e `settleReturnAmount` DOCS_UNVERIFIED (il SimExchange le assume in modo circolare).
   - Formato del `salt` e reinvio dopo timeout DOCS_UNVERIFIED (serve testnet: BLOCKED_EXTERNAL).
6. **P3.**
   - 7 tool senza test offline dedicato;
   - 3 tool NOT_TESTED live;
   - DERIVED solo sulla prima pagina (con PARTIAL segnalato);
   - topic WS `account:trades_v3` e `account:fills_v3` mai sottoscritti live;
   - reconnect WS NOT_IMPLEMENTED (non serve senza un WS persistente).

## 5. Work package proposti

Completati, nessun lavoro: 35/35 GET esposti con parametri completi; nessuna scrittura; coordinatore PG (INTEGRATION); anchor PG; gateway disabilitato; risk engine a zero; isolamento Render.

| WP | Titolo | Priorità | Gap | File | Test RED | GREEN | Test reale (solo lettura o simulazione) | Autorizzazioni dell'owner | DONE |
|---|---|---|---|---|---|---|---|---|---|
| WP1 | Portare su main le 3 correzioni del ledger (solo `orders.py` + 3 file di test, separati da SimExchange e monitor) | P1 | SG-017..019 | `signer/orders.py`, `signer/README.md`, `tests/test_sxbet_orders_{filled_late_fill,filled_purged_settlement,absence_evidence}.py` | 9 failed su main (riprodotto) | 19 passed + suite intera | nessuno (offline + PG16 CI) | merge riservato all'owner (§2.6, codice di firma/ledger) | CI verde, 0 RED, #130 annotata |
| WP2 | Archiviare l'evidenza dell'audit live, legata allo SHA | P1 | SX-060 | `evidence/sxbet/` (JSON sanitizzato del run 41320538088 + rerun su 342fed6), test di leak guard su quel file | test: l'evidenza esiste ed è legata allo SHA di main | file committato, leak guard verde | rerun manuale del workflow read-only su main | dispatch del workflow (environment riservato all'owner); merge di un file evidence | JSON nel repo con SHA, nessun valore sensibile |
| WP3 | E2E simulato + monitor su main | P2 | SG-029/030 | file del branch e2e senza WP1 | già sul branch | 188 + PG 12 | solo simulazione | merge all'owner (signer) | CI con PG verde |
| WP4 | Servizio signer: chiudere i gap prima della PR | P1/P2 | SG-007/008/033, SG-004 | `signer_service/store.py`, `service.py`, `signer/anchor.py` | revoca: un'asserzione di una passkey revocata è rifiutata; flood → 429 con codice fisso; riavvio: esposizione e perdite conservate | nuovi test verdi + 843 | test su Android reale, solo dev/testnet, nessun invio | test sul dispositivo dell'owner; merge all'owner; nessun deploy | test verdi; README "MANCANTE" aggiornato |
| WP5 | Profilo Toronto su main e collegato a `core`/`gateway` | P2 | SG-031 | `signer/network_profile.py`, `core.py`, `gateway.py` | un intent testnet con escrow/host di mainnet viene firmato (RED atteso dopo il collegamento) | `PROFILE_*_OF_OTHER_NETWORK` | GET pubblica `/metadata/obv3` su Toronto | merge all'owner | guardia attiva in `core` e `gateway` |
| WP6 | Adapter KMS su main (senza backend) | P3 | SG-012 | `signer/kms.py` + doc | già sul branch | 36 passed | nessuno | merge all'owner; scelta del provider e del costo: owner | merge; backend fuori scope |
| WP7 | Evidenza REAL_MCP_CLIENT e coordinatore PG con traffico reale | P2 | SX-047, SX-059 | `scripts/` (smoke OAuth read-only), evidence | – | report salvato | 3–5 `sxbet_*` di sola lettura da Claude.ai su Render + `/healthz` | sessione OAuth dell'owner; budget delle chiamate | evidenza nel repo; `POSTGRES_SHARED` osservato in un report |
| WP8 | Test offline per 7 tool + copertura live dei 3 NOT_TESTED, topic account trades/fills | P3 | SX-004/006/014-016/020/022, SX-029/030/035 | `tests/test_sxbet_read_only.py`, script dell'audit | path e parametri attesi | verdi | rerun dell'audit read-only | dispatch del workflow | 46/46 con test dedicato |
| WP9 | Testnet: ordine reale su Toronto | P2 (BLOCKED) | SG-011/020/032 | – | – | – | NON eseguibile ora: richiede signer + conferma fuori banda (§2.6) | account, USDC e chiave Toronto; decisione esplicita; giurisdizione | – |
