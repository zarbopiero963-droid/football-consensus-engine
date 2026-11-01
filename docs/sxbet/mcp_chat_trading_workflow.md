# SX Bet: trading dalla chat (ChatGPT/Claude) con il wallet Magic esistente

Inventario, gap analysis, roadmap e checklist di attivazione. Mandato `FCB-SXBET-MAGIC-WALLET-FULL-AUDIT-01`, sezioni §8, §10, §11, §12, §13 e §18. Stato del codice: `main` 54b2cb7, che include #151 (positions, hedging, regole), #152 (signer: adapter, cancellazione, ledger; mergiata su autorizzazione scritta dell'owner, eccezione al §2.6 limitata a quella PR), #153 (vettori EIP-712) e #154 (ritmo dell'audit).

**LIVE_TRADING resta non disponibile.** Nessun tool MCP può firmare, inviare o cancellare.

## 1. Flusso previsto

```
ChatGPT / Claude
 → Football Consensus MCP (Render, pubblico, OAuth)    preparazione e anteprima: sxbet_simulate_order, hedge/close/rules preview
 → canale autenticato MCP→signer                        MANCANTE
 → signer isolato (servizio separato)                   signer/core.py, risk.py, approval.py, orders.py
     → risk engine (limiti firmati dall'owner, default 0)
     → approvazione owner fuori banda (app sul telefono)  MANCANTE (app); verifica Ed25519 già presente
     → firma EIP-712 dell'Order con la chiave EOA         presente (chiave da owner; KMS: adattatore MANCANTE)
     → gateway (DISABILITATO, flag LIVE_TRADING firmato dall'owner)
     → OfficialRestAdapter → RestSender                  builder/parser in #152; sender reale MANCANTE
 → SX Bet API (POST /orders-v3)
 → riconciliazione (GET /orders-v3/{id}, /client/{clientOrderId}, /fills-v3)   presente nel ledger
 → risposta in chat (stato reale, mai "eseguito" senza evidenza)
```

Come si conferma senza aprire il wallet: vedi `isolated_signer_architecture.md` §5.

## 2. Capacità operative

Il wallet serve solo per creare ordini. Tutto il resto richiede solo la API key.

| Funzione | Endpoint ufficiale | Scope API key | Firma wallet | Stato nel repository | Mancante | Fattibilità |
|---|---|---|---|---|---|---|
| Creazione ordine (maker) | `POST /orders-v3` (GTC, `postOnly` opzionale) | `Orders: create` | **sì**: EIP-712 `Order` | builder, parser, ledger e risk in `signer/` (#152); anteprima `sxbet_simulate_order` | `RestSender` reale, canale MCP→signer, app di approvazione | `PARTIAL` |
| Ordine taker | `POST /orders-v3` (IOC/FOK contro il book) | `Orders: create` | **sì** | come sopra; il risk engine verifica la liquidità per IOC/FOK | come sopra | `PARTIAL` |
| Cancellazione | `DELETE /orders-v3` (≤100 id), per mercato, per evento, tutti | `Orders: cancel` | no: solo la API key; il §2.6 richiede comunque la conferma dell'owner | `signer/cancellation.py` con `CancelApproval` (#152); anteprima `sxbet_resting_orders_scope` | sender reale | `PARTIAL` |
| Heartbeat | `POST /heartbeat/v3` | `Orders: cancel` | no | solo documentato | sender, policy | `NOT_VERIFIED` |
| Ordini aperti | `GET /orders-v3` | `Orders: read` | no | `sxbet_open_orders`, `sxbet_order`, `sxbet_order_status` | — | `PASS` |
| Fill parziali | `GET /fills-v3` | `Orders: read` | no | `sxbet_fills`; stati `PARTIALLY_FILLED` nel ledger | — | `PASS` (lettura) |
| Posizioni | `GET /positions-v3`, `/trades` | `Orders: read` | no | `sxbet_positions`, `sxbet_positions_summary` | quarter-line e parlay non modellati (segnalati come conflitto) | `PARTIAL` |
| P&L | derivato (`settleReturnAmount`, `pnl`) | `Orders: read` | no | `sxbet_exposure_pnl` (DERIVED) | arrotondamento delle fee `DOCS_UNVERIFIED` | `PARTIAL` |
| Hedging | derivato, più un ordine reale | come creazione ordine | **sì** per eseguire | `sxbet_hedge_simulation` (solo simulazione) | esecuzione = creazione ordine | `PARTIAL` |
| Cashout equivalente | nessun endpoint nativo documentato | — | sì (è un hedge) | `sxbet_close_position_preview`, mai "garantito" | esecuzione | `PARTIAL` |
| Stop-loss / take-profit | non nativi | — | sì per eseguire | `sxbet_rules_evaluate`: solo alert e proposte, tutto disattivato per default | esecuzione con conferma per ordine | `PARTIAL` |
| Trailing stop | non nativo | — | sì | regola presente, valutazione su richiesta | **monitoraggio continuo** (worker) | `PARTIAL` |
| WebSocket | Centrifugo `wss://realtime.sx.bet/connection/websocket`; token da `GET /user/realtime-token-v3/api-key` | lettura (API key) | no | verificato nell'audit; `sxbet_reconciliation_diagnostics` accetta messaggi WebSocket forniti dal chiamante | listener persistente | `PARTIAL` |
| Saldo | `GET /user/balance-v3` | `Wallet: read` | no | `sxbet_balance` | — | `PASS` |
| Settlement | stato del trade `SETTLED`, `outcome` 1/2/0 | `Orders: read` | no | letto in `sxbet_trades` e `sxbet_positions` | — | `PASS` (lettura) |
| Prelievo, trasferimento, cambio firmatario | `POST /user/multisig/...` | `Wallet: withdraw/transfer/admin` | **sì**: `SafeTx` | **escluso per progetto**: il signer rifiuta tutto ciò che non è `Order` | non previsto | fuori scope |

Le funzioni native dell'exchange sono: ordini, cancellazione, heartbeat, letture e WebSocket. Hedging, cashout, stop-loss, take-profit e trailing **non** esistono sull'exchange: sono logica nostra, che produce ordini normali.

## 3. Gateway attuale e codice mancante per il trasporto reale

**Già presente in `signer/`** (#149 e #152 mergiate):
- validazione del payload e binding v2;
- digest e ordine pari all'id;
- verifica del firmatario (`INTENT_MAKER_IS_NOT_SIGNER`);
- scadenze, idempotenza con `clientOrderId` e ledger;
- write-ahead prima dell'invio;
- stato `UNKNOWN` per timeout o crash, con l'esposizione riservata fino alla riconciliazione;
- nessun retry cieco: lo stato si riconcilia prima;
- risposte tardive e fill tardivi sempre registrati;
- `EXPIRED_UNCONFIRMED`;
- riconciliazione tramite letture REST;
- ledger ancorato in PostgreSQL con lease, in #152.

**Mancante** (PR separate, mergiate dall'owner, §2.6):
1. `RestSender` reale: HTTPS verso `api.sx.bet` con header `x-sx-api-key`, timeout, passaggio dal coordinatore di quota condiviso e nessun retry automatico. Un timeout produce `UNKNOWN`;
2. un servizio signer con endpoint autenticato (mTLS o token) raggiungibile solo dalla rete privata, e un tool MCP di "richiesta" che **non** firma;
3. l'app o il dispositivo di approvazione dell'owner, con le schermate dei binding per ordine e per cancel;
4. un adattatore KMS, se si sceglie l'opzione D;
5. la modifica del guard `RENDER` per ospitare il signer su un servizio Render separato (governance);
6. un worker di monitoraggio per trailing e per l'heartbeat;
7. una prova su testnet di: formato del `salt` (hex o decimale), reinvio dopo timeout, unità di `fillAmount`, 400 su un batch.

## 4. Risk engine (`signer/risk.py`)

Tutti i limiti valgono **0 per default** (`RiskLimits`), quindi con la configurazione di default ogni ordine viene rifiutato. La config è valida solo se firmata con la chiave Ed25519 dell'owner (`load_risk_config`). Nessun tool MCP e nessun argomento può modificarla.

| Limite | Campo | Verificato da |
|---|---|---|
| Stake massimo per ordine | `max_stake_per_order_base_units` | `tests/test_sxbet_risk.py` |
| Esposizione per mercato, evento e totale | `max_exposure_per_market/event`, `max_total_exposure_base_units` | idem |
| Perdita per sessione e per giorno UTC | `session_loss_limit_base_units`, `daily_loss_limit_base_units` | idem |
| Ordini aperti massimi | `max_open_orders` | idem |
| Quota minima e massima | `min_/max_percentage_odds` | idem |
| Slippage | `slippage_tolerance_bps` | idem |
| Età dei dati di mercato, quota e saldo | `max_quote_age_seconds` | idem |
| Liquidità | per IOC/FOK, rifiuto se la liquidità è minore dello stake | idem |
| Saldo | il disponibile deve coprire le riserve più lo stake | idem |
| Stato del mercato e dati mancanti | rifiuto se i dati mancano o non sono verificabili | idem |

**Gap:** non esiste una "liquidità minima" configurabile per ordini GTC (la liquidità si controlla solo per IOC/FOK). Va proposta come limite aggiuntivo; è una decisione dell'owner.

**Gap legato al Magic:** gli ordini fatti dal web non sono nel ledger. Prima di ogni ordine il risk engine deve usare l'esposizione letta dal vivo (`/orders-v3`, `/positions-v3`), non solo quella del ledger.

## 5. Test

| Area | Evidenza | Esito |
|---|---|---|
| Firma EIP-712 con EOA fittizi, recovery, formato della chiave, domini mainnet/Toronto, casi negativi | `tests/test_sxbet_eip712_official_vectors.py` (26 test, #153) | `PASS` |
| Vettore ufficiale SX Bet | l'esempio OpenAPI è incoerente: nessun vettore riproducibile | `NOT_VERIFIED` (difetto documentale) |
| Stati dell'ordine, idempotenza, replay, crash e restart, concorrenza tra processi, UNKNOWN, fill tardivi | `tests/test_sxbet_orders.py`, `test_sxbet_gateway.py`, `test_sxbet_signer*.py` | `PASS` |
| Ordini maker e taker, fill parziali, cancellazioni simulate, timeout, risposte 400/ambigue | `tests/test_sxbet_rest_adapter.py`, `test_sxbet_cancellation.py` (#152) | `PASS` (fake transport) |
| Ledger ancorato e lease su PostgreSQL reale | `tests/integration/test_signer_ledger_anchor_postgres.py` (#152, 7/7) | `PASS` |
| Limiti di rischio | `tests/test_sxbet_risk.py` | `PASS` |
| P&L, hedging, stop-loss, take-profit, trailing, riconciliazione | `tests/test_sxbet_positions.py`, `hedging`, `rules`, `derived_tools` (#151) | `PASS` |
| Errori 401/403/429/500, rate limit condiviso | test offline del client e del coordinatore (`tests/test_sxbet_*`, `tests/integration`); audit live read-only su GitHub Actions | `PASS` |
| Isolamento del signer (non importabile nel MCP o su Render) e accesso ai secret | `tests/test_sxbet_render_isolation.py`, `test_sxbet_simulation.py`, test di governance | `PASS` |
| Account e wallet reali dell'owner | letture autenticate nell'audit live (proxy e Safe esistono) | `PARTIAL`: confronto dell'EOA a cura dell'owner |
| Ordine riconosciuto dall'exchange | richiede un invio | `BLOCKED_EXTERNAL`: testnet Toronto |

**Testnet:**
- "Sign up at toronto.sx.bet and export your private key from the assets page";
- "Open a support chat on sx.bet to receive testnet USDC";
- "Testnet keys are network-specific".

Il ciclo completo è collaudabile con sole credenziali di prova, perché Toronto "mirrors mainnet" (deduzione). La checklist ufficiale di go-live finisce con "Place one minimum-size order and cancel it before scaling up".

## 6. Roadmap (PR in ordine)

1. ~~#152: adapter, cancellazione, ancoraggio del ledger (nessun sender).~~ Mergiata (54b2cb7).
2. ~~#153: vettori EIP-712 (solo test).~~ Mergiata (e60c3d4).
3. PR di governance: consentire il signer su un servizio Render **separato** (guard `RENDER` per service ID). Merge dell'owner.
4. PR del servizio signer: HTTP privato, autenticazione MCP→signer, endpoint "richiesta di ordine" che restituisce il binding da approvare. Nessun sender.
5. PR dell'app di approvazione: PWA o passkey, schermate dei binding per ordine e per cancel, verifica nel signer.
6. PR del `RestSender` reale **solo verso Toronto**, con il coordinatore di quota. Merge dell'owner; test su testnet: un ordine minimo più cancel, `salt`, reinvio dopo timeout.
7. (Opzionale) adattatore AWS KMS.
8. PR di attivazione mainnet: sender verso mainnet dietro il flag `LIVE_TRADING` firmato. Merge dell'owner, dopo la verifica legale.
9. Worker di monitoraggio: trailing e heartbeat; propone ordini, mai li esegue senza conferma.

## 7. Azioni che richiedono l'owner

1. **Parere legale in Italia** (`BLOCKED_LEGAL_VERIFICATION`).
2. Decisione sulla custodia: chiave Magic esportata (custodisce **tutti** i fondi del proxy) oppure EOA dedicato; KMS sì o no.
3. Confronto, dal proprio connettore, tra l'EOA mostrato da sx.bet e i `owners` della Safe (`sxbet_safe_info`).
4. Approvazione e merge delle PR 3–8 della roadmap.
5. Creazione del servizio Render privato (circa $7,25 al mese) e, se scelto, dell'account AWS KMS (circa $1 al mese). Sono nuovi costi.
6. Account testnet su toronto.sx.bet, testnet USDC dal supporto, API key testnet.
7. Generazione della chiave di approvazione sul proprio dispositivo; firma della config di rischio e del flag `LIVE_TRADING`.
8. Export della chiave (sul proprio dispositivo, mai in chat) e import nel signer o nel KMS. **Richiede un secondo mandato.**
9. Rigenerazione della API key con `Orders: create` e `Orders: cancel`, poi aggiornamento di Render e dell'environment GitHub.
10. Conferma, ordine per ordine, sul proprio dispositivo.

## 8. Procedura concreta di attivazione futura

1. Merge delle PR 1–6 (roadmap).
2. Collaudo su Toronto con un account di prova:
   - un ordine minimo e la sua cancellazione;
   - la riconciliazione;
   - un timeout simulato;
   - la verifica di formato del `salt` e unità.
3. Parere legale favorevole.
4. L'owner esporta la chiave Magic dal proprio dispositivo e la carica nel signer (secret file del servizio privato, oppure import in KMS). La chiave non passa mai da chat, GitHub o MCP.
5. L'owner firma la config di rischio con limiti piccoli (proposta in `activation_readiness.md` §5) e il flag `LIVE_TRADING` con scadenza breve.
6. Primo ordine mainnet di importo minimo, confermato sul dispositivo, poi la sua cancellazione. Si verificano ledger, exchange e saldo.
7. Solo dopo, uso normale dalla chat: ogni ordine con conferma sul dispositivo.
