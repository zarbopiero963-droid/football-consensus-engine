# SX Bet: trading dalla chat (ChatGPT/Claude) con il wallet Magic esistente

> **SUPERATO in parte** dalla governance §2.6 aggiornata: un ordine reale nasce da un comando esplicito e completo dell'owner nella chat del client MCP, verificato dal signer isolato, senza app sul telefono, passkey né approvazione fuori banda per ordine. Le parti che descrivono un'approvazione fuori banda per singolo ordine restano solo come storico. Progetto corrente: [owner_commanded_execution.md](owner_commanded_execution.md); stato della #187: [issue187_completion_matrix.md](issue187_completion_matrix.md).

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
6. un processo che esegua periodicamente il worker di monitoraggio (la logica c'è: `signer/monitor.py`, §9) e un sender di heartbeat;
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
| Motore di trading end-to-end **SIMULATO** (intent → rischio → approvazione → firma → gateway → exchange simulato → riconciliazione → P&L → settlement) | `tests/test_sxbet_e2e_simulated*.py`, `test_sxbet_monitor.py`, `test_sxbet_sim_exchange.py`; vedi §9 | `PASS` (simulato) |
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
9. Worker di monitoraggio: la logica è in `signer/monitor.py` (§9: riconciliazione, scadenze, policy di heartbeat, trailing persistito; propone, non esegue). Restano lo scheduler, il sender di heartbeat e il collegamento alle notifiche dell'owner.

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

## 9. Motore di trading SIMULATO: verifica end-to-end (mandato SXBET-SECURE-SIGNER-PREPARATION-01)

**Cosa è stato verificato:** il nostro motore (signer, risk engine, approvazioni, ledger, gateway, adapter, cancellazione, positions, hedging, regole, monitor) collegato a un **exchange simulato** in memoria (`tests/sxbet_sim_exchange.py`, solo nei test). L'exchange simulato implementa il protocollo `RestSender` e riproduce il contratto documentato (frame maker/taker, capacità `size × (1e20 − p) / p`, ritorno `stake × 1e20 / odds`, ciclo `PENDING → ACTIVE → INACTIVE` con `inactiveReason`, fill parziali, batch non atomici, bucket di cancel, paginazione dei fill, saldo con escrow, settlement 1/2/0 con fee sul profitto, heartbeat). Le scelte non documentate sono elencate in `SIM_ASSUMPTIONS` (`DOCS_UNVERIFIED`).

**Cosa NON è stato verificato:** il comportamento reale di SX Bet. Nessun ordine è stato inviato, nessuna rete è stata usata. Le chiavi sono usa e getta; la config di rischio e il flag `LIVE_TRADING` dei test sono firmati con una chiave owner di test, come nei test esistenti. Nessun `RestSender` eseguibile esiste fuori da `tests/`. `LIVE_TRADING` resta `UNAVAILABLE`.

### Nativo SX Bet o nostro

| Funzione | Nativa SX Bet | Nostra (su ordini normali) | Stato della verifica | Test |
|---|---|---|---|---|
| Ordine maker (GTC, `postOnly`) | sì, `POST /orders-v3` | — | `SIMULATED` | `test_sxbet_e2e_simulated.py`, `test_sxbet_sim_exchange.py` |
| Ordine taker (IOC/FOK) | sì, stesso endpoint | — | `SIMULATED` | idem |
| Fill parziali ed esposizione residua | sì (letture) | contabilità del ledger | `SIMULATED` | `test_simulated_partial_fill_residual_exposure_*` |
| Cancel per id, mercato, evento, tutti | sì, `DELETE /orders-v3*` | approvazione dell'owner per ogni cancel | `SIMULATED` | `test_sxbet_e2e_simulated_cancel.py` |
| Heartbeat | sì, `POST /heartbeat/v3` | solo **policy** in `signer/monitor.py`, nessun sender | `SIMULATED` (lapse) | `test_sxbet_monitor.py` |
| Settlement e P&L realizzato | sì (`outcome` 1/2/0, `settleReturnAmount`) | P&L nel ledger, limiti di perdita | `SIMULATED` | `test_simulated_maker_order_profit_loss_void` |
| Hedging | **no** | `bridge/sxbet/hedging.py` → ordine sull'esito opposto | `SIMULATED`: P1/P2 eseguiti = previsione del simulatore | `test_simulated_hedge_executed_matches_simulator_prediction` |
| "Cashout" / chiusura posizione | **no** (nessun endpoint documentato; il simulatore risponde 404) | cancel del residuo + hedge FULL, etichetta `CLOSE_POSITION_OURS_NOT_NATIVE_SX_BET` | `SIMULATED` | `test_simulated_close_position_is_ours_*` |
| Stop-loss, take-profit | **no** | `bridge/sxbet/rules.py`: solo proposte | `SIMULATED` | `test_simulated_stop_loss_and_take_profit_*` |
| Trailing stop | **no** | regola + picco persistito dal monitor | `SIMULATED`, anche dopo restart | `test_simulated_trailing_stop_monitor_persists_peak_across_restart` |
| Ledger ancorato e lease | — | `signer/anchor.py` + migration signer 0001 (serie `signer`, WP-004) | `VERIFIED` su PostgreSQL 16 reale usa e getta (locale) | `tests/integration/test_signer_e2e_postgres.py` (5) e `test_signer_ledger_anchor_postgres.py` (7) |
| Firma EIP-712 | contratto ufficiale | `signer/eip712.py` | vettori locali (#153); l'exchange simulato verifica la firma con la stessa libreria | `test_sxbet_eip712_official_vectors.py` |

### Scenari coperti (tutti con asserzioni sui numeri)

- **Profitto, perdita, void** (maker 5 USDC a 40%): +7,5 / −5 / 0 USDC; saldo 57,5 / 45 / 50 USDC. Taker a 41%: +5,9 USDC meno la fee taker del 3% sul profitto (177.000 unità).
- **Rifiuti prima dell'exchange:** quota mossa oltre lo slippage, liquidità insufficiente (IOC/FOK), saldo insufficiente, stake sopra il massimo, quota fuori dalla banda dell'owner, nessuna approvazione, approvazione firmata da un'altra chiave, testo "approvato" in chat.
- **Idempotenza e replay:** stesso `clientOrderId` due volte ⇒ un solo ordine; replay dell'approvazione ⇒ rifiutato; invii concorrenti (8 thread e 2 processi) ⇒ un solo ordine e un solo `SEND_ATTEMPT`.
- **Guasti:** timeout e 500 dopo l'accettazione ⇒ `UNKNOWN`, poi la riconciliazione lo trova, nessun doppio ordine; timeout, 500 e 429 prima ⇒ assente dopo 60 s, reinvio dello **stesso** ordine; visibilità ritardata oltre la finestra ⇒ il reinvio è rifiutato dall'exchange (collisione di `clientOrderId`), l'esposizione resta riservata e l'ordine viene ritrovato (H3).
- **Crash** a ogni punto write-ahead (dopo `AUTHORIZE`, dopo `SIGNED`, dopo `UNKNOWN`, dopo l'accettazione, dopo la risposta, durante il cancel), poi restart e replay ⇒ stato coerente, nessuna riserva persa.
- **Cancel:** il bucket `cancelled` con una lettura vecchia, `unconfirmed`, un fill in corsa col cancel e un fill tardivo dopo il cancel ⇒ l'esposizione cambia solo con evidenza di lettura.

### Bug trovati e corretti (con test di regressione RED → GREEN)

1. **`FILLED` non poteva mai andare a `SETTLED` dopo il purge della riga** (404 circa 10–20 minuti dopo `INACTIVE`, mentre il settlement arriva ore dopo). Il matched restava contato per sempre e la perdita realizzata non arrivava ai limiti di perdita. Correzione: `FILLED` è aggiunto al ramo 404 basato sui fill completi e `SETTLED` (`signer/orders.py`; test `tests/test_sxbet_orders_filled_purged_settlement.py`).
2. **Le evidenze di "assenza" identiche venivano scartate dal dedupe.** Dopo `RECONCILED_ABSENT`, gli stessi 404 dopo la scadenza non portavano più a `EXPIRED_UNCONFIRMED` (riserva bloccata per sempre). Un secondo invio perso non poteva più tornare `SIGNED` (`UNKNOWN` per sempre). Correzione: id di evidenza legato al contesto (tentativo d'invio, "dopo la scadenza"), come già per `CONFIRM_AFTER_WINDOW` (`signer/orders.py`; test `tests/test_sxbet_orders_absence_evidence.py`).

3. **Fill tardivo di un ordine `FILLED` perso** (review): un `FILLED` con matched minore dello stake (resto di polvere) che riceve un fill tardivo (404 e fill completi, oppure una riga con fill maggiore) produceva `ILLEGAL_TRANSITION`: il fill non veniva registrato e l'ordine non poteva più andare a `SETTLED`. Correzione: `FILLED → FILLED` è ammesso, come per `CANCELLED`/`EXPIRED`, solo con matched strettamente crescente e non oltre lo stake. Non libera mai esposizione (test `tests/test_sxbet_orders_filled_late_fill.py`).
4. **Race tra monitor e gateway** (review): il monitor leggeva l'exchange fuori dal lock e applicava una fotografia vecchia. Con un trasporto che risponde con byte diversi a ogni lettura, un'"assenza" letta prima di un reinvio del gateway riportava l'ordine a `SIGNED` mentre era sull'exchange: un nuovo submit lo avrebbe inviato di nuovo. Correzione: rilettura dell'ordine, letture dell'exchange e applicazione avvengono tutte sotto il lock condiviso del gateway, ora **obbligatorio** (`lock=gateway._lock`, nessun lock privato di default; `MONITOR_LOCK_REQUIRED`). Test: `test_monitor_stale_absent_snapshot_never_reopens_a_resent_order`.

Tutti fallivano in modo sicuro rispetto all'esposizione oppure erano bloccati dall'exchange (collisione di `clientOrderId`), ma bloccavano capitale o aprivano a un invio in più. Le modifiche di comportamento sono intenzionali e dichiarate. Unici test esistenti modificati: il guard "heartbeat" di `tests/test_sxbet_simulation.py` (esenzione limitata a `signer/monitor.py`) e l'elenco dei moduli in `tests/test_sxbet_render_isolation.py`.

### Gap documentati (decisioni dell'owner, non corretti)

- Il signer non conosce `orderSizeMinimumBaseUnits`: un intent sotto il minimo costruito senza il bridge viene firmato. L'exchange lo rifiuta (400) e lo stake resta riservato fino alla scadenza.
- La quota del contesto di rischio (snapshot del book) include i nostri ordini a riposo, che l'exchange non abbina mai con noi (prevenzione del self-match). Il risk engine può quindi vedere una liquidità che non è utilizzabile.
- Un `PARTIALLY_FILLED` la cui riga `INACTIVE` viene purgata prima di una lettura resta riservato (fallisce in modo sicuro). La mitigazione è il monitor, che rilegge con una frequenza inferiore alla finestra di purge.
- **Limite strutturale delle prove simulate:** le assunzioni `SETTLE_RETURN_NET` (`settleReturnAmount` al netto della fee) e `FILL_AMOUNT_UNITS` (`fillAmount` nelle unità dello stake, con `sum(fillAmount) = totalBetSize − remainingSize`) di `SimExchange` sono **le stesse** assunzioni `DOCS_UNVERIFIED` su cui si basa `signer/orders.py` (calcolo del P&L realizzato, condizione per il settlement). Il simulatore le soddisfa per costruzione, quindi i test simulati **non possono validarle**: lo può fare solo una prova su testnet Toronto (un fill parziale e un settlement reali, confrontando `fillAmount`, `remainingSize`, `settleReturnAmount` e `settleFeeAmount`).

### Worker di monitoraggio (`signer/monitor.py`, solo libreria)

Ogni `tick()`:
- **riconcilia** gli ordini aperti, parziali, `UNKNOWN`, scaduti e in attesa di settlement, usando solo tre letture iniettate (`get_order`, `get_order_by_client_id`, `get_fills`), eseguite sotto il lock obbligatorio del gateway. Non può chiamare nient'altro;
- produce **alert** sulle scadenze, sugli `AUTHORIZED` mai firmati e sugli `UNKNOWN`;
- valuta la **policy di heartbeat**, senza alcun sender;
- valuta le **regole** con un valutatore iniettato e persiste il **picco del trailing** (scrittura atomica, sha256, legata al `ledger_id`) **prima** di emettere proposte.

Una proposta con `executable`, `signed` o `submitted` diversi da `false` è un errore (`MONITOR_PROPOSAL_CLAIMS_EXECUTABLE`). Non esiste uno scheduler né un processo in esecuzione: è logica pura, testata anche dopo un restart.

### PostgreSQL

La migration signer 0001 (serie `signer`, WP-004) e il ledger ancorato sono stati collaudati su un **PostgreSQL 16 usa e getta** avviato in locale (cartella temporanea, solo socket Unix, nessun `DATABASE_URL` di produzione, cluster rimosso a fine prova). Collaudi:
- migrate due volte (idempotente);
- 6 scrittori concorrenti (un solo lease, poi takeover con fencing);
- un processo reale ucciso tra fsync locale e ancora (ripartenza solo con `LedgerHeadAck` firmato dall'owner, nessuna riserva persa);
- una copia vecchia che non può riaccettare;
- la riconciliazione dopo il drop delle sessioni e dopo un vero `pg_ctl restart`.

In CI questi test richiedono `TEST_DATABASE_URL`; senza, sono `SKIPPED`.

