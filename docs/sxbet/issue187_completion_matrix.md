# Issue #187: matrice di completamento SX Bet V3

Mandato dell'owner `FCB-SXBET-ISSUE187-FULL-COMPLETION` (12-11-2026). Base: `main` `315f778`.

Legenda:
- `PASS`: verificato con evidenza (test offline, collaudo live in sola lettura o entrambi, come indicato).
- `PARTIAL`: verificato solo in parte (per esempio solo offline, o con account vuoto).
- `FAIL`: assente o errato.
- `NOT_TESTED`: implementato ma senza la prova richiesta.
- `BLOCKED`: dipende da una decisione o da un'azione dell'owner.

Tre gate distinti, mai confusi: **implementazione completata**, **certificazione testnet (Toronto)** e **autorizzazione mainnet**. La chiusura della #187 riguarda i primi due e **non** attiva il trading reale.

## 1. Requisiti

| # | Requisito | Stato | Evidenza o motivo | PR |
|---|---|---|---|---|
| R1 | Mercati, quote, order book, liquidità (36 GET ufficiali) | PASS | collaudo live 11-11 sui 46 tool `sxbet_*` (36 GET + derivati): 35 PASS, 11 EMPTY_VALID, 0 FAIL; suite offline | — |
| R2 | Saldo, ordini, posizioni, esposizione (lettura) | PARTIAL | live solo con account vuoto (EMPTY_VALID); logica coperta offline | S8 (testnet) |
| R3 | Simulazione ordini e costi | PASS (offline + live) | `sxbet_simulate_order`, `net_pnl` con fee lette da `/user/fees-v3` | — |
| R4 | P&L lordo e netto, fee, USDC a 6 decimali | PARTIAL | formule e Decimal PASS offline; scala delle fee VERIFIED (S1); fee reale al settlement mai osservata | S8 |
| R5 | Ordini Market (FOK/IOC) e Limit (GTC, post-only): validazione e firma | PARTIAL | signer: comando owner, risk v2, EIP-712 con vettori ufficiali, PASS offline; nessun invio | S6–S8 |
| R6 | Invio ordini a SX Bet | FAIL | nessun `RestSender` reale (per progetto, fino ai gate) | S8 |
| R7 | Cancellazione (id, mercato, evento, tutti) | PARTIAL | `signer/cancellation.py` offline; nessun invio; `CANCEL_AFTER_MATCH` (changelog 09-11) non recepito | S3, S8 |
| R8 | Ordini aperti, fill completi e parziali, scadenze | PARTIAL | macchina a stati e `SimExchange` offline; unità di `fillAmount` e `settleReturnAmount` DOCS_UNVERIFIED | S8 |
| R9 | Chiusura totale o parziale, hedge, cashout | PARTIAL | solo simulazione; D1 corretto in S1; esecuzione = nuovo comando owner | S7, S8 |
| R10 | Recovery dopo crash, timeout, riavvio; esiti UNKNOWN | PARTIAL | ledger append-only, ancoraggio PostgreSQL, write-ahead: PASS offline e su PG usa e getta; testnet no | S8 |
| R11 | Riconciliazione con l'exchange; idempotenza | PARTIAL | `evidence_from_exchange`, monitor (libreria) offline; manca `GET /orders-v3/batch` | S2, S8, S9 |
| R12 | Kill switch persistente (P2-A) | FAIL | un ingaggio non registrato nel ledger non sopravvive al riavvio | S4 |
| R13 | Eventi con squadre identiche, paginazione completa e normalizzazione (P2-B) | FAIL | manca il `MarketDataProvider` di rete con lettura completa verificata | S5 |
| R14 | Comando esplicito dell'owner dal client MCP fino al signer | FAIL | grammatica e verifica nel signer PASS offline; mancano servizio signer e tool MCP di richiesta | S6, S7 |
| R15 | Signer separato dal MCP; chiave mai esposta | PASS (offline) | guard di import, `tests/test_sxbet_render_isolation.py`; nessuna firma restituita al MCP da garantire nel servizio | S6 |
| R16 | Segregazione dei poteri sui fondi (EOA dedicato + Safe multisig) | NOT_TESTED | docs ufficiali: `add-signer` con `threshold`, `set-threshold`; non provato che l'EOA non possa muovere fondi aggirando la Safe | S10 (testnet) |
| R17 | Integrazione MCP verificata realmente (ChatGPT e Claude.ai) | PARTIAL | lettura e simulazione verificate; trading no | S11 |
| R18 | Realtime (WebSocket) | PASS (per progetto) | verificato dal workflow di audit; il MCP non tiene connessioni persistenti | — |
| R19 | Heartbeat e scheduler del monitor | FAIL | logica in `signer/monitor.py`, nessun processo né sender | S9 |
| R20 | Idoneità legale Italia | BLOCKED | `BLOCKED_LEGAL_VERIFICATION`: gate **mainnet**, non della #187 | owner |

## 2. Difetti dell'audit live dell'11-11

| Difetto | Stato | PR |
|---|---|---|
| D1 hedge/chiusura/regole senza posizione | corretto | S1 |
| D2 testi sul vecchio modello di approvazione | corretto | S1 |
| D3 404 come `PROVIDER_ERROR` | corretto (`NOT_FOUND`) | S1 |
| D4 minimo di scadenza prima dei metadati | chiuso formalmente (approvazione owner 12-11): limite inferiore dichiarato e testato | S2 |
| D5 intent per mercato sconosciuto | corretto | S1 |
| D6 id scartati in silenzio | corretto (`not_found`) | S1 |
| D7 righe obsolete `ACTIVE`, fixture non paginate, status interi | corretto (viste DERIVED: stati decodificati, finali/scaduti segnalati; la route `/fixture/active` non ha paginazione nel contratto) | S2 |
| D8 parametri di path non riportati | corretto (`path_parameters`) | S1 |
| D9 scala delle fee | corretto (VERIFIED) | S1 |
| D10 minimo 1 vs 5 USDC | PASS: vale il valore runtime | — |
| D11 `GET /orders-v3/batch` | aperto | S2 |
| D12 `rate_limit_remaining` spesso null | osservazione: il dato del provider resta null | — |

## 3. Roadmap PR (una sola PR aperta alla volta, merge solo dell'owner)

| PR | Obiettivo | Perimetro | Dipende da |
|---|---|---|---|
| **S1** | D1, D2, D3, D5, D6, D8, D9 | bridge | — |
| S2 | D7, chiusura D4, D11 (tool `sxbet_orders_batch`), `newestOrderCreatedAtMs` | bridge (+ conteggi tool condivisi) | S1 |
| S3 | `CANCEL_AFTER_MATCH` nel parser delle cancellazioni e nel ledger | signer | — |
| S4 | P2-A: kill switch persistente, fail-closed al riavvio | signer | — |
| S5 | P2-B: `MarketDataProvider` con lettura completa e normalizzazione canonica | signer | S4 |
| S6 | Servizio signer (nessun sender), timbro `received_at` al confine, firma mai restituita al MCP | signer + infrastruttura | S4, S5, decisione owner |
| S7 | Tool MCP di richiesta ordine e cancel: non firmano, disattivati per default | bridge | S6 |
| S8 | `RestSender` **solo Toronto** e certificazione testnet | signer | S7, GO testnet |
| S9 | Scheduler del monitor e sender di heartbeat | signer | S8 |
| S10 | Prova della segregazione (EOA dedicato + Safe 2-di-2) su testnet | docs + evidenze | S8, atti owner |
| S11 | Certificazione da ChatGPT e Claude.ai, matrice delle evidenze finale | docs + evidenze | S8–S10 |

## 4. Criteri misurabili di chiusura della #187

1. R1–R19 in `PASS`, oppure esclusi formalmente con motivazione approvata dall'owner nella #187.
2. D1–D12 corretti o esclusi formalmente.
3. Test di regressione RED→GREEN per ogni correzione; CI verde sullo SHA finale; review indipendente `SAFE`; nessun P0/P1 aperto.
4. Certificazione Toronto con evidenze conservate (id ordine, hash del comando, righe del ledger, letture dell'exchange) per: Market, Limit, non eseguito, fill parziale e completo, cancel, timeout/UNKNOWN, riavvio, idempotenza, concorrenza, limiti di rischio, kill switch, wallet e autorizzazioni.
5. Nessuna chiave in repository, log, artefatti CI o output MCP (test dedicati più scansione dei segreti).
6. Deploy Render sano dopo ogni merge; nessuna regressione dei tool API-FOOTBALL e browser.
7. `LIVE_TRADING` mainnet resta `UNAVAILABLE` alla chiusura.
