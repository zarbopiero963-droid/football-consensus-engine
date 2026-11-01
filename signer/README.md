# Signer isolato SX Bet

Componente separato dal server MCP (governance §2.6). **`LIVE_TRADING` resta `UNAVAILABLE`**: il package firma e contiene un gateway di invio, un adapter REST e un flusso di cancellazione, ma il gateway è **disabilitato** e nel repository **non esiste alcun trasporto di rete**. L'adapter costruisce richieste e interpreta risposte; l'invio è un `Protocol` astratto (`RestSender`) senza implementazioni. Nessun file di `signer/` importa librerie di rete o driver di database, e nessuno legge una API key.

**Il signer è un servizio separato e non gira mai nel processo MCP.** `import signer` (e quindi qualunque `signer.*`) solleva `ImportError` se:
- `SXBET_SIGNER_PROCESS` non vale esattamente `"1"`, oppure
- la variabile `RENDER` è presente (Render la imposta su ogni servizio, incluso il bridge MCP).

Lo verificano `tests/test_sxbet_render_isolation.py`, che controlla anche che `bridge.server` avviato con `RENDER=true` non carichi mai `signer.*` né `eth_account`, che nessun tool MCP accetti chiavi, approvazioni o firme, che `requirements.txt` non contenga librerie di firma e che nessun modulo di `bridge/` importi `signer`.

Contratto ufficiale (verificato il 01-11-2026):
- https://docs.sx.bet/api-reference/eip712-order-signing
- https://docs.sx.bet/api-reference/post-orders-v3
- https://docs.sx.bet/developers/posting-orders
- https://docs.sx.bet/developers/order-lifecycle
- https://docs.sx.bet/developers/client-order-id
- https://docs.sx.bet/developers/odds-rounding
- https://docs.sx.bet/developers/time-in-force, https://docs.sx.bet/developers/taking-liquidity
- https://docs.sx.bet/developers/cancelling-orders e `api-reference/delete-orders-v3`, `delete-orders-v3-market`, `delete-orders-v3-event`, `delete-orders-v3-all`
- https://docs.sx.bet/api-reference/get-order-v3, `get-order-v3-by-client-id`, `get-fills-v3`
- https://docs.sx.bet/developers/rate-limits, https://docs.sx.bet/developers/testnet-and-mainnet, https://docs.sx.bet/api-reference/post-heartbeat-v3 (solo documentato)
- https://docs.sx.bet/openapi.json (`CreateOrderItem`, `CreateOrderResult`, `CreateOrderOutcome`, `CancelByIdItem`, `OrderV3`, `FillV3`, `TradeV3`, `/user/fees-v3`)

## Architettura

```
client MCP ─► bridge: sxbet_simulate_order ─► intent canonico + intent_hash   (nessuna chiave, nessuna firma)
                                                   │
                                                   ▼
              dispositivo dell'owner: mostra il binding v2 e firma con Ed25519 (chiave privata solo lì)
                                                   │  Approval(binding, nonce, issued_at, expires_at, signer_id)
                                                   ▼
signer/ (processo separato, SXBET_SIGNER_PROCESS=1, mai su Render MCP)
  core.py    1. rivalida l'intent e ricalcola l'hash
             2. verifica l'approvazione SENZA consumarla (firma, TTL ≤ 120 s, signer_id, binding)
             3. risk.py: limiti dell'owner + dati di mercato freschi (contesto di rischio)
             4. orders.py: una riga fsync = nonce consumato + AUTHORIZED + esposizione riservata (lock unico)
             5. firma EIP-712; la riga SIGNED è persistita prima di restituire la firma
  gateway.py DISABILITATO salvo flag firmato dall'owner; trasporto astratto, nessuna implementazione di rete
             verifica firma ↔ intent approvato, idempotenza, write-ahead UNKNOWN, batch non atomici, riconciliazione
  rest_adapter.py  builder di POST /orders-v3 e delle letture + parser delle risposte (RestSender astratto)
  cancellation.py  cancel per id / mercato / evento / tutti: approvazione dell'owner per ogni cancel,
             rilascio dell'esposizione solo da letture dell'exchange
  anchor.py  ancoraggio esterno della testa del ledger + lease di scrittura con fencing token (PostgreSQL)
  audit.py   audit JSONL append-only con hash chain (allowlist di campi, mai chiave né firma)
```

## Binding dell'approvazione (v2)

L'owner firma `approval_message(binding=…, approval_nonce, issued_at, expires_at, signer_id)` con dominio `sxbet.order_approval.v2`. Il `binding` (`signer/approval.py::BINDING_KEYS`) contiene: `account_id` (identità API), `maker`, `network`, `chain_id`, `verifying_contract`, `base_token`, `event_id`, `market_hash`, `selection` (`outcome` 1|2 ed etichetta leggibile), `is_maker_betting_outcome_one` (lato), `time_in_force`, `stake_base_units`, `percentage_odds_limit`, `quoted_percentage_odds` (la quota vista dall'owner), `expected_fees` (snapshot di `GET /user/fees-v3`), `max_exposure_base_units`, `order_expiry`, `client_order_id` e `intent_hash`.

- Cambiare un campo dopo la firma rompe la firma (`APPROVAL_SIGNATURE_INVALID`).
- Il signer ricava di nuovo ogni campo da intent, configurazione e contesto di rischio. Un binding firmato ma incoerente viene rifiutato (`APPROVAL_BINDING_MISMATCH_<CAMPO>`, `FEES_CHANGED_SINCE_APPROVAL`, `ORDER_EXPOSURE_ABOVE_APPROVED_MAX`).
- **Modifica non retrocompatibile, intenzionale:** le approvazioni v1 (solo `intent_hash`) sono rifiutate (`APPROVAL_MALFORMED`). I test sono stati aggiornati di conseguenza.
- Monouso, TTL ≤ 120 s, tolleranza di clock di 5 s. Il nonce si consuma solo nella riga `AUTHORIZE` del ledger, quindi resta consumato anche dopo un riavvio.

## Perché un testo in chat non è una conferma

Un testo in chat, in un output di tool o in una pagina web può essere scritto da chiunque: dal modello stesso, da una prompt injection, da un terzo. Il signer accetta solo un oggetto `Approval` con una **firma Ed25519** valida della chiave dell'owner, che il signer non possiede (conserva solo la chiave pubblica).

## Autorizzazione transazionale

0. **Snapshot:** binding dell'approvazione e contesto di rischio vengono copiati in valori JSON semplici (dict/list/str/int/bool/None) prima di ogni verifica; da lì in poi si usa solo la copia. Un oggetto del chiamante modificato durante la firma non cambia ciò che è verificato o registrato. Un valore non semplice produce `APPROVAL_MALFORMED` o `RISK_CONTEXT_MALFORMED`.
1. Verifica dell'approvazione senza consumo e controllo `nonce_consumed`.
2. Il risk engine gira **dentro il lock del ledger**, sull'esposizione corrente.
3. Solo se tutto passa viene scritta una riga `AUTHORIZE` (fsync) che consuma il nonce e riserva l'importo.
4. Firma; poi la riga `SIGNED`; solo allora la firma esce dalla funzione.

Ne seguono queste proprietà:
- un rifiuto (limiti, saldo, dati scaduti…) **non** consuma l'approvazione, che si può riusare quando i limiti lo permettono;
- due richieste concorrenti con la stessa approvazione producono una sola firma;
- un crash tra consumo e firma lascia l'ordine `AUTHORIZED` con il nonce consumato e nessuna firma emessa. Dopo il riavvio `Signer.abandon_unsigned(client_order_id)` lo porta a `REJECTED` e libera l'esposizione. È sicuro perché nessuna firma esce prima della riga `SIGNED`;
- un fallimento della firma nel processo porta subito a `REJECTED` (`SIGNING_FAILED`);
- se la riga `SIGNED` non si riesce a scrivere, la firma viene scartata e non esce (`LEDGER_WRITE_FAILED`): l'ordine resta `AUTHORIZED` e riservato;
- due `client_order_id` con lo stesso digest EIP-712 sono rifiutati (`ORDER_ID_ALREADY_AUTHORIZED`).

## Stati degli ordini ed esposizione (`signer/orders.py`)

Stati: `PREPARED`, `AUTHORIZED`, `SIGNED`, `SUBMITTED`, `OPEN`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `EXPIRED`, `EXPIRED_UNCONFIRMED`, `SETTLED`, `REJECTED`, `UNKNOWN`. Sono ammesse solo le transizioni di `TRANSITIONS`; ogni altra è `ILLEGAL_TRANSITION_*` e non viene scritta.

| Stato | Riservato | Matched |
|---|---|---|
| `AUTHORIZED`, `SIGNED`, `SUBMITTED`, `OPEN`, `UNKNOWN` | importo − matched (cioè l'intero importo) | matched |
| `EXPIRED_UNCONFIRMED` (404 dopo la scadenza, non è una prova) | importo − matched | matched |
| `PARTIALLY_FILLED` | parte non eseguita, finché l'exchange non conferma cancel o scadenza | parte eseguita |
| `FILLED`, `CANCELLED`, `EXPIRED` | 0 | parte eseguita, fino a `SETTLED` |
| `REJECTED` **dopo** che la firma è uscita (`SIGNED` raggiunto: 400 all'invio, riga `FAILED`, `SIGNED → REJECTED` manuale) | l'intero importo finché `now ≤ expiry + 30 s`, poi 0 (conta anche come ordine aperto) | 0 |
| `REJECTED` mai firmato (`SIGNING_FAILED`, `abandon_unsigned`), `SETTLED` | 0 | 0 (`SETTLED` registra il P&L realizzato) |

- **`REJECTED` poi eseguito (H3, modifica intenzionale):** `REJECTED` non è più un vicolo cieco se la firma era uscita. Se l'exchange mostra poi l'ordine vivo o eseguito, l'evidenza porta a `SUBMITTED`/`OPEN` (riga `PENDING`/`ACTIVE` senza fill: torna riservato), `PARTIALLY_FILLED`/`FILLED` (fill nella riga o, con 404, nei fill completi dell'ordine), oppure `CANCELLED`/`EXPIRED` **solo con un fill**. Senza esecuzione (riga `INACTIVE` senza fill, 404 senza fill, fill `FAILED`) non si impara nulla e la riserva fino alla scadenza continua. Un `REJECTED` mai firmato resta finale (`REJECTED_NEVER_SIGNED_IS_FINAL`). Il gateway scrive l'audit `EVIDENCE_REJECTED_ORDER_EXECUTED`; la transizione è nel ledger con `reason` e `evidence_sha256`, quindi sopravvive al riavvio. Il test `test_states_and_transition_table_are_closed` è stato aggiornato di conseguenza.
- Un `PARTIALLY_FILLED` con 404 (la riga sparisce 10–20 min dopo `INACTIVE`) registra comunque i fill completi che superano il matched (`LATE_FILL_WHILE_NOT_FOUND`); il resto non cambia.
- `UNKNOWN` (timeout, risposta ambigua, write-ahead prima di un invio) **non libera mai capitale** finché non è riconciliato con dati dell'exchange.
- La riconciliazione usa `evidence_from_exchange`, con i campi OpenAPI di `GET /orders-v3/{orderId}` (`id`, `totalBetSize`, `remainingSize`, `status`, `inactiveReason`), di `GET /orders-v3/client/{clientOrderId}` e di `GET /fills-v3?orderId=` (`orderId`, `fillAmount`, `status`, `settlement.settleReturnAmount`).
  - `INACTIVE` con `FILLED` porta a `FILLED`, con `EXPIRED` a `EXPIRED`, con gli altri `inactiveReason` documentati a `CANCELLED`.
  - Un motivo o uno stato sconosciuto non insegna nulla.
  - Il settlement richiede fill completi, tutti `SETTLED` e con somma uguale al matched.
- Un 404 prima della scadenza non dimostra nulla. **Nemmeno dopo la scadenza un 404 libera da solo l'esposizione:** dopo la scadenza (più 30 s) un 404 con lista dei fill completa e vuota porta a `EXPIRED_UNCONFIRMED`, che **resta riservato**. Si passa a `EXPIRED` (rilascio) solo con:
  - evidenza terminale positiva: la riga dell'exchange `INACTIVE` con un `inactiveReason` documentato (`EXPIRED`, `USER_REQUESTED`, …, che porta a `EXPIRED`/`CANCELLED`/`FILLED`);
  - oppure lo stesso 404 con fill completi e somma uguale al matched registrato, osservato di nuovo almeno `EXPIRY_CONFIRM_SECONDS` (24 h) dopo la prima osservazione.
- **Un fill tardivo non si perde mai:** in `EXPIRED_UNCONFIRMED`, `EXPIRED` o `CANCELLED` un fill nuovo (dalla riga dell'exchange o, con 404, da fill completi tutti di quest'ordine; i fill `FAILED` non contano) aumenta il matched con una auto-transizione, e `EXPIRED → FILLED` è ammesso. L'esposizione eseguita torna a essere contata dal risk engine.
- Da `UNKNOWN` si torna a `SIGNED` (unico percorso verso un reinvio) solo con: 404 per id **e** per `clientOrderId`, fill vuoti e almeno 60 s dall'ultimo tentativo. Il reinvio usa **lo stesso ordine firmato e lo stesso `clientOrderId`**.
- Evidenze duplicate o che non cambiano nulla non scrivono righe. Il matched non scende mai e non supera l'importo.
- **Persistenza:** JSONL append-only, fsync per riga, hash chain, `GENESIS` legato a `signer_id` e indirizzo. Stato, nonce, idempotenza, esposizione e P&L si **ricostruiscono sempre** rileggendo il ledger e ripassando ogni riga dalla macchina a stati.
  - Un ledger mancante, vuoto, estraneo, manomesso o con una storia illegale (anche se re-hashata) genera `LedgerError`, e il signer non si avvia.
  - Un nuovo ledger si crea solo con `OrderLedger.create` esplicito, mai "partendo da zero" in silenzio.
- **Un solo scrittore:** un `OrderLedger` tiene un `flock` esclusivo non bloccante sul file per tutta la sua vita (fino a `close()`), sia in modalità locale sia in modalità ancorata. Una seconda istanza, nello stesso processo o in un altro, fallisce con `LEDGER_LOCKED`: due signer non possono accettare lo stesso nonce.
  - Prima di ogni append l'istanza verifica che il file sia ancora quello bloccato (stesso inode), che abbia la dimensione che ha scritto e che termini con la riga il cui hash è la testa in memoria. Altrimenti (uno scrittore esterno che ignora il lock advisory, un file sostituito) `LEDGER_HEAD_MISMATCH`.
  - Un append fallito o parziale, o una testa diversa, **avvelena** l'istanza: ogni scrittura successiva dà `LEDGER_POISONED` fino a riavvio e replay. Una riga finale troncata fa poi fallire il replay (`LEDGER_CORRUPT`) e richiede una decisione dell'owner.
  - Limiti residui: il controllo prima dell'append legge solo dimensione e ultima riga (una riscrittura di righe precedenti a parità di dimensione si rileva al replay successivo); in modalità locale `flock` è advisory e locale, quindi il ledger deve stare su un filesystem locale dell'host del signer. La modalità ancorata qui sotto toglie questa dipendenza.
- Le righe `CANCEL_REQUEST` (vedi Cancellazione) consumano il nonce di un'approvazione di cancel e registrano cosa è stato chiesto; non cambiano stato né esposizione di alcun ordine.

## Ancoraggio esterno della testa e lease di scrittura (`signer/anchor.py`, H1/H2)

Opzionale: `OrderLedger.create/open(..., anchor=<AnchorStore>)`. Senza `anchor` resta la modalità locale (`flock`). Con `anchor`:
- **ledger_id** = `"<signer_id>:<indirizzo>"`; **seq** = numero di righe del file (`GENESIS` = 1); **testa** = hash dell'ultima riga.
- **Lease più `flock`:** all'apertura il ledger prende il lease di scrittura dello store **prima** di aprire il file; un secondo scrittore, su qualsiasi host, riceve `LEDGER_LOCKED`. Ogni acquisizione riceve un **fencing token** strettamente crescente.
  - **Modifica intenzionale (finding C):** il `flock` locale resta attivo anche in modalità ancorata (difesa in profondità). Una seconda istanza sullo stesso file riceve `LEDGER_LOCKED` anche se il suo store le concede un lease (store mal configurato o diviso). Il test che vietava `flock` in modalità ancorata è stato sostituito di conseguenza.
- **Ordine write-ahead:** riga locale scritta e fsync **prima**, poi avanzamento dell'ancora (compare-and-set su `seq`, con il fencing token). Un crash in mezzo lascia il file **una riga avanti** all'ancora, mai indietro.
- Se l'avanzamento fallisce (store irraggiungibile `LEDGER_ANCHOR_WRITE_FAILED`, token superato `LEDGER_FENCED`, seq diverso `LEDGER_ANCHOR_CONFLICT`) l'istanza è **avvelenata**: nessuna firma esce (la riga `AUTHORIZE`/`SIGNED` non ancorata fa rifiutare la richiesta).
- **All'avvio** il signer rifiuta di partire se:
  - l'ancora è più avanti del file (`LEDGER_ANCHOR_AHEAD_OF_LOCAL`): coda troncata o file sostituito con uno più corto, anche se la hash chain è valida;
  - l'hash alla seq ancorata è diverso (`LEDGER_ANCHOR_HASH_MISMATCH`): storia riscritta o fork;
  - il file è più avanti dell'ancora (`LEDGER_LOCAL_AHEAD_OF_ANCHOR`), oppure il ledger non è mai stato ancorato (`LEDGER_ANCHOR_MISSING`). Questi due casi si sbloccano solo con una **conferma firmata dall'owner** (vedi sotto);
  - lo store non risponde (`LEDGER_ANCHOR_UNREACHABLE`).
- **Conferma della testa firmata dall'owner (`anchor.LedgerHeadAck`, finding A, modifica non retrocompatibile):** `OrderLedger.open(..., owner_ack=<LedgerHeadAck>, owner_public_key=<chiave pubblica Ed25519 dell'owner>)`.
  - Il parametro `owner_ack_head` (una stringa con l'hash della testa locale) **non esiste più**. Chiunque abbia scrittura sul file del ledger e sugli argomenti di avvio poteva aggiungere righe forgiate con una hash chain valida e "confermarle" da solo.
  - L'owner firma con la stessa chiave Ed25519 di approvazioni, configurazione di rischio e flag `LIVE_TRADING`, con dominio `sxbet.ledger_head_ack.v1`, il messaggio JSON canonico `{ledger_id ("<signer_id>:<indirizzo>"), anchored {seq, head_hash} oppure null, local {seq, head_hash}, issued_at, expires_at}`. Il TTL è ≤ 300 s, con tolleranza di clock di 5 s. Il signer conserva solo la chiave pubblica, presa dalla propria configurazione e mai dalla conferma.
  - Rifiuti, con codice fisso e senza toccare l'ancora:
    - `LEDGER_HEAD_ACK_KEY_MISSING`: manca la chiave pubblica;
    - `LEDGER_HEAD_ACK_MALFORMED`: una stringa, un dict o un altro oggetto non valido;
    - `LEDGER_HEAD_ACK_SIGNATURE_INVALID`: firma assente, alterata o di un'altra chiave;
    - `LEDGER_HEAD_ACK_TTL_TOO_LONG`, `LEDGER_HEAD_ACK_NOT_YET_VALID`, `LEDGER_HEAD_ACK_EXPIRED`;
    - `LEDGER_HEAD_ACK_MISMATCH`: la conferma riguarda un altro ledger_id, un'altra testa ancorata (anche "mai ancorato" contro "ancorato") o un'altra seq/hash locale.
  - Un fork (hash diverso alla seq ancorata) resta `LEDGER_ANCHOR_HASH_MISMATCH` e nessuna conferma lo sblocca.
  - La conferma viene letta solo quando il file è più avanti dell'ancora, e cita la testa ancorata da cui parte: dopo l'avanzamento non descrive più alcuno stato e non può essere riusata.
- `create` con un'ancora già esistente per lo stesso ledger_id è `LEDGER_ANCHOR_EXISTS` (il file non viene creato): un ledger nuovo non può sostituire una storia ancorata.
- **Scrittore stantio:** se la sessione del primo scrittore muore e un secondo prende il lease (token + 1), il primo viene respinto al suo prossimo avanzamento (`LEDGER_FENCED`). Può aver scritto localmente al massimo **una** riga non ancorata, che il nuovo scrittore rileva (`LEDGER_HEAD_MISMATCH` su file condiviso, oppure `LEDGER_LOCAL_AHEAD_OF_ANCHOR` al riavvio).
- **`PostgresAnchorStore(connect)`** (migration `sql/migrations/0008_signer_ledger_anchor.sql`, additiva):
  - lease: una sessione dedicata tiene `pg_try_advisory_lock` sulla chiave del ledger_id e, sotto il lock, incrementa `signer_ledger_writer_leases.fencing_token`; se la sessione muore PostgreSQL rilascia il lock;
  - avanzamento: transazione breve (`statement_timeout` e `lock_timeout` 2,5 s) che blocca la riga del lease `FOR UPDATE`, controlla token e seq attesa e inserisce la nuova testa in `signer_ledger_anchors`;
  - il database stesso rifiuta `UPDATE`, `DELETE`, `TRUNCATE`, una seq non crescente e un token diverso da quello del lease (trigger). Richiede PostgreSQL ≥ 14 (`CREATE OR REPLACE TRIGGER`);
  - anche `signer_ledger_writer_leases` è protetta da trigger (finding E): un `UPDATE` che non aumenta strettamente `fencing_token` (`SIGNER_LEDGER_LEASE_TOKEN_NOT_INCREASING`) o che cambia `ledger_id` (`SIGNER_LEDGER_LEASE_ID_IMMUTABLE`) è rifiutato. Sono rifiutati anche `DELETE` e `TRUNCATE` (`SIGNER_LEDGER_LEASE_NO_DELETE`), perché una riga cancellata e reinserita a token 1 abbasserebbe il token. Uno scrittore stantio non può quindi tornare valido;
  - la factory di connessione è iniettata: `signer/` non importa driver; il processo del signer passa per esempio `psycopg.connect` con il proprio DSN, fuori da Render MCP.
- La migration 0008 non viene applicata da sola a nessun database (`app.db.migrate()` non è chiamata dal runtime): applicarla al database del signer è un passo esplicito dell'owner.

## Risk engine (`signer/risk.py`)

I limiti si leggono **solo** da un file JSON il cui percorso è in `SXBET_SIGNER_RISK_CONFIG`, firmato Ed25519 dalla chiave dell'owner (dominio `sxbet.risk_config.v1`).
- Un file mancante, non firmato, malformato o con firma non valida produce i limiti tutti a zero, cioè il rifiuto di ogni ordine (`EXPOSURE_LIMIT_ZERO`).
- `RiskLimits` è immutabile e si carica una volta all'avvio. Nessun campo di intent, approvazione o contesto può cambiarlo.

| Limite | Codice di rifiuto |
|---|---|
| `max_stake_per_order_base_units` | `STAKE_ABOVE_LIMIT` |
| `max_exposure_per_market_base_units` | `MARKET_EXPOSURE_ABOVE_LIMIT` |
| `max_exposure_per_event_base_units` | `EVENT_EXPOSURE_ABOVE_LIMIT` |
| `max_total_exposure_base_units` | `EXPOSURE_ABOVE_LIMIT` |
| `session_loss_limit_base_units` (perdita realizzata nella sessione + importo dell'ordine) | `SESSION_LOSS_LIMIT` |
| `daily_loss_limit_base_units` (giorno UTC, sopravvive al riavvio) | `DAILY_LOSS_LIMIT` |
| `max_open_orders` | `OPEN_ORDERS_ABOVE_LIMIT` |
| `slippage_tolerance_bps` (movimento avverso rispetto a `quoted_percentage_odds`; 0 = nessuna tolleranza) | `ODDS_MOVED_BEYOND_SLIPPAGE` |
| `min_percentage_odds` / `max_percentage_odds` | `ODDS_OUTSIDE_OWNER_BAND` |
| `max_quote_age_seconds` (mercato, quota, saldo) | `*_STALE`, `*_FROM_FUTURE` |

Tutti i limiti tranne lo slippage devono essere > 0, altrimenti `EXPOSURE_LIMIT_ZERO`.

Il contesto di rischio è obbligatorio e ogni dato mancante produce un rifiuto (`RISK_CONTEXT_MISSING`, `MARKET_DATA_MISSING`, `QUOTE_MISSING`, `LIQUIDITY_MISSING`, `BALANCE_MISSING`, `FEES_MISSING_OR_MALFORMED`). Contiene:
- la riga del mercato (`status` deve essere `ACTIVE`, altrimenti `MARKET_NOT_ACTIVE`);
- la quota taker per il lato dell'ordine, con la liquidità (per IOC/FOK deve coprire l'importo, altrimenti `INSUFFICIENT_LIQUIDITY`);
- il saldo disponibile, che deve coprire tutte le riserve non eseguite più l'ordine (`INSUFFICIENT_BALANCE`);
- le commissioni.

## Gateway di invio (`signer/gateway.py`): disabilitato

- `SubmissionGateway` si rifiuta di costruirsi senza un `LiveTradingFlag` prodotto da `load_live_trading_flag`, cioè da un file firmato dall'owner, legato a `signer_id` e rete, con TTL ≤ 24 h. Il default è `DISABLED`.
  - Il flag contiene i byte firmati e la firma dell'owner. Il gateway **riverifica** la firma Ed25519 con la chiave pubblica dell'owner ricevuta alla costruzione (`owner_public_key`, dalla propria configurazione, mai dal flag), alla costruzione e **a ogni `submit`**. `enabled`, `signer_id`, rete e scadenza si leggono solo dai byte verificati.
  - La scadenza si ricontrolla a ogni `submit`: da `now ≥ expires_at` ogni invio è rifiutato con `LIVE_TRADING_FLAG_EXPIRED` (la riconciliazione, in sola lettura, continua).
  - `LiveTradingFlag(...)` chiamato direttamente solleva `GatewayDisabled`. Questo impedisce costruzioni accidentali o ingenue, ma **non** è un confine di sicurezza contro codice che gira nel processo del signer (Python non ha membri privati): il confine è la chiave privata Ed25519 dell'owner, che non esiste sull'host del signer.
- Il trasporto è un `Protocol` astratto (`submit_orders`, `get_order`, `get_order_by_client_id`, `get_fills`). L'implementazione del contratto ufficiale è `OfficialRestAdapter(sender)` (vedi sotto), che a sua volta dipende da un `RestSender` astratto. Nel repository **non esiste alcuna implementazione di rete**: i test usano `FakeTransport` e `FakeSender` in memoria.
- Il gateway verifica:
  - che l'intent rivalidato abbia lo stesso `intent_hash` e lo stesso order id;
  - che il payload sia esattamente `eip712.order_payload(intent, firma)`;
  - che la firma recuperi l'indirizzo del maker e del ledger;
  - che l'ordine sia nel ledger con lo stesso hash.
- Payload verificato: l'oggetto canonico del signer `{marketHash, maker, totalBetSize, percentageOdds, salt, expiry, baseToken, isMakerBettingOutcomeOne, timeInForce, orderSignature, clientOrderId}` (salt decimale). Con l'adapter ufficiale la richiesta sul filo si costruisce da questo oggetto (formato di `salt`, `postOnly`, `waitForOutcome`/`maxWaitTime` secondo `SubmitOptions`) **prima** del write-ahead: una richiesta che il contratto rifiuta non viene inviata e l'ordine resta `SIGNED`.
- **Batch (`submit_batch`):** fino a `limits.maxCreateOrders` ordini dello stesso maker in una richiesta; `clientOrderId` duplicati o un batch troppo grande sono rifiutati per intero prima di scrivere qualsiasi cosa. "Batches are not atomic": ogni ordine viene scritto in write-ahead, classificato e portato al proprio stato da solo. Gli ordini già fuori, in `UNKNOWN` o troppo vicini alla scadenza sono riportati e non inviati.
- **Write-ahead:** `SIGNED → UNKNOWN(SEND_ATTEMPT)` è persistito prima della chiamata. Un crash durante l'invio lascia `UNKNOWN`, mai "non inviato".
- Classificazione della risposta (`signer/rest_adapter.py:parse_create_orders`, una risposta non libera mai esposizione):
  - 200 con la riga `SUBMITTED` del nostro id, alla sua posizione, con `commandId` (e `clientOrderId` uguale, se presente) porta a `SUBMITTED`;
  - con `waitForOutcome`: `RESTED` → `OPEN`; ogni altro esito → `SUBMITTED`, con l'intero importo riservato. Sono `FULLY_FILLED`, `PARTIAL_FILL_RESTED`, `PARTIAL_FILL_DONE` e `CANCELLED` con `fillAmount` (motivo `*_RECONCILE_FOR_FILL`), `CANCELLED` senza fill (`OUTCOME_CANCELLED_RECONCILE_TO_RELEASE`) e `TIMEOUT`. Un `outcome` con importi incoerenti è ignorato (`OUTCOME_MALFORMED_IGNORED`);
  - **modifica intenzionale (finding D):** la risposta **non imposta mai** il matched (`filled`), perché le unità di `outcome.fillAmount` sono `DOCS_UNVERIFIED` e `filled` non può più scendere. Un valore sovrastimato faceva fallire ogni lettura successiva con `FILLED_AMOUNT_INVALID`, in modo permanente. Ora solo le letture (`GET /orders-v3`, `/fills-v3`) registrano i fill. Prima `FULLY_FILLED` portava a `FILLED` e `PARTIAL_FILL_*` a `PARTIALLY_FILLED` con il `fillAmount` della risposta. L'esposizione totale non cambia (importo intero riservato invece di matched + resto) e nulla viene rilasciato. I test sono stati aggiornati di conseguenza;
  - una riga `FAILED`, oppure un 400 con `message` per una richiesta di **un** ordine, porta a `REJECTED`; poiché la firma è già uscita, l'importo resta riservato fino alla scadenza dell'ordine (più 30 s) e un'esecuzione scoperta dopo viene registrata (H3);
  - un 400 per un batch di più ordini porta tutti a `UNKNOWN` (non è documentato che un 400 rifiuti ogni ordine del batch);
  - tutto il resto (timeout, errori, 401/403/5xx, body malformato, numero di righe diverso, id o `clientOrderId` diverso, stato sconosciuto, `commandId` mancante) porta a `UNKNOWN`.
- **Idempotenza:**
  - lo stesso `clientOrderId` non esce mai due volte (fuori da `SIGNED` restituisce lo stato senza inviare);
  - in `UNKNOWN` risponde `STATE_UNKNOWN_RECONCILE_FIRST`;
  - un payload diverso con lo stesso id viene rifiutato;
  - gli invii concorrenti passano da un lock unico.

## Adapter REST ufficiale (`signer/rest_adapter.py`)

Builder di richieste e parser di risposte, nient'altro. `RestSender.send(RestRequest) -> RestResponse` (o `SenderTimeout`) è astratto: autenticazione, TLS, URL di base (mainnet `https://api.sx.bet`, testnet `https://api.toronto.sx.bet`), rate limit e timeout sono compito del sender, che **non** esiste nel repository. L'adapter non legge alcuna API key.
- `build_create_orders`: 1..`limits.maxCreateOrders` ordini (da `GET /metadata/obv3`, `ExchangeLimits.from_metadata`), un solo `maker`, `clientOrderId` unici e conformi (`^[A-Za-z0-9_-]{1,64}$`), importi stringa decimale, `expiry` intero in secondi, `timeInForce` `GTC`/`IOC`/`FOK`, `orderSignature` di 65 byte.
- `SubmitOptions` (configurazione del deploy, mai argomento di un tool): `wait_for_outcome`, `max_wait_time_ms` (1..15000, inviato solo con `waitForOutcome`), `post_only` (solo `GTC`; `IOC`/`FOK` → `POST_ONLY_REQUIRES_GTC` in locale), `salt_format`.
- **Maker e taker:** non esiste un endpoint di fill. Un ordine taker è lo stesso ordine firmato con `timeInForce` `IOC` o `FOK`, che esegue contro la liquidità a riposo al prezzo `percentageOdds` o migliore (developers/taking-liquidity); un ordine maker è `GTC` e resta sul book (con `postOnly` non prende mai). Un `GTC` che incrocia all'arrivo esegue subito e lascia a riposo solo il resto.
- Letture: `GET /orders-v3/{orderId}`, `GET /orders-v3/client/{clientOrderId}` (200 → `FOUND`, 404 → `NOT_FOUND`, altro → `ERROR`; id validati prima di costruire il path) e `GET /fills-v3?orderId=&perPage=100&nextKey=` con paginazione; la lista è `complete` solo all'ultima pagina, oltre il limite di pagine è `complete: false`.
- Heartbeat (`POST /heartbeat/v3`, `timeoutSeconds` 0..3600, un lapse cancella **tutti** gli ordini con `HEARTBEAT_TIMEOUT`): solo documentato, nessun sender di heartbeat. `HEARTBEAT_TIMEOUT` è già un `inactiveReason` riconosciuto (→ `CANCELLED`).

## Cancellazione (`signer/cancellation.py`)

Cancellare è un'operazione finanziaria (§2.6): **ogni** cancel richiede un'approvazione dell'owner.
- `CancelApproval`: firma Ed25519 dell'owner (dominio `sxbet.cancel_approval.v1`) su un binding `{account_id, maker, network, scope, order_ids (ordinati) | market_hash | event_id}`, TTL ≤ 120 s, legata a `signer_id`, monouso. Il signer ricalcola il binding dalla richiesta: ogni differenza è `CANCEL_APPROVAL_BINDING_MISMATCH`.
- Rotte: `DELETE /orders-v3` (id, ≤ `limits.maxCancelOrders`), `DELETE /orders-v3/market?marketHash=`, `DELETE /orders-v3/event?eventId=`, `DELETE /orders-v3/all`.
- Flusso: flag live riverificato → richiesta costruita → approvazione verificata (senza consumo) → **pre-check** con letture dell'exchange di ogni ordine coinvolto → **flag e scadenza dell'approvazione ricontrollati** → riga `CANCEL_REQUEST` (fsync, consuma il nonce) → flag e scadenza ricontrollati di nuovo → invio → parsing → **nuova lettura** di ogni ordine coinvolto.
- **Ricontrollo dopo le letture (finding B):** il pre-check può fare fino a 3 letture per ordine. Subito prima del consumo del nonce si riverificano il flag (`LIVE_TRADING_FLAG_EXPIRED`, …) e `now < expires_at` dell'approvazione (`CANCEL_APPROVAL_EXPIRED`, stessa regola del primo controllo, senza tolleranza sulla scadenza). Un rifiuto qui **non consuma** il nonce e non invia nulla. Il controllo si ripete subito prima dell'invio: un rifiuto in quel punto non invia nulla, registra `CANCEL_NOT_SENT` nell'audit e lascia il nonce consumato (fail closed: serve una nuova approvazione).
- **Un cancel non libera mai esposizione da solo.** Il ledger cambia solo con evidenza dell'exchange: `INACTIVE` + `USER_REQUESTED` → `CANCELLED` (rilascia la parte non eseguita, il matched resta), fill → `PARTIALLY_FILLED`/`FILLED`. Il bucket `cancelled`, un comando accettato (`cancelledSubmitted`), `unconfirmed`, un timeout o una risposta ambigua lasciano l'ordine riservato: si rilegge più tardi con `reconcile`.
- Per id: ogni id deve stare in esattamente un bucket documentato (`cancelled`, `notCancelled` con `NOT_FOUND`/`CANCEL_PENDING`/`NOT_ACTIVE`, `unconfirmed`), altrimenti `UNKNOWN`. Un ordine che il pre-check trova già terminale (eseguito, cancellato, scaduto) non viene inviato (`ALREADY_TERMINAL`); se non resta nulla, non parte nessuna richiesta e l'approvazione non viene consumata. Si invia al più l'insieme approvato, mai di più.
- Mercato/evento/tutti: confermano solo che il comando è stato accettato. Gli id in `cancelledSubmitted` che il ledger non conosce (ordini piazzati altrove) sono riportati in `foreign_order_ids`.
- Un nonce consumato resta consumato anche dopo un timeout e dopo il riavvio: un nuovo tentativo richiede una nuova approvazione.

## Custodia della chiave

L'audit (`signer/audit.py`) ammette in più i campi `command_id` (id del comando dell'exchange), `scope` e `bucket` (cancellazioni); mai chiave, firma o firma di approvazione.

Il signer legge la chiave una sola volta, all'avvio, dalla propria variabile d'ambiente `SXBET_SIGNER_PRIVATE_KEY`. Non la restituisce, non la scrive in log, audit o ledger, e `repr()` la nasconde. L'oggetto non è serializzabile. Una chiave mancante o malformata produce un errore con codice fisso, senza riportarne il valore.

## Dipendenze

`signer/requirements.txt` (`eth-account` 0.14.x, `cryptography`). **Non** sono in `requirements.txt`: il runtime Render del bridge non riceve librerie di firma. La CI (`multi-market-tests.yml`, job `pytest`) le installa solo per eseguire i test. `tests/conftest.py` imposta `SXBET_SIGNER_PROCESS=1` per il processo di test.

## DOCS_UNVERIFIED

- Formato di `salt` in `POST /orders-v3`. Fonti: lo schema OpenAPI dice "Random 32-byte hex string" e l'esempio della richiesta invia `"0x8fa0…70ef"`; gli esempi JS inviano `hexlify(randomBytes(32))`; gli esempi Python (posting-orders, taking-liquidity) inviano `str(int.from_bytes(...))`, la stringa decimale dello stesso uint256. **Scelta:** l'adapter usa per default lo schema documentato (`SALT_HEX32`, `0x` + 64 cifre esadecimali minuscole, con zeri a sinistra); `SALT_DECIMAL` è una scelta di configurazione esplicita. Il valore firmato è lo stesso uint256 in entrambi i casi. Va confermato su testnet. (Modifica rispetto alla versione precedente, che inviava il decimale.)
- Che `outcome.fillAmount` (con `waitForOutcome`) sia nelle stesse unità di `totalBetSize`/`remainingSize`. Per questo il valore viene solo validato e mai registrato: il matched viene dalle letture.
- Che un 400 su un batch di più ordini significhi che nessun ordine del batch è stato accettato (documentato solo per la collisione di `clientOrderId`): oggi porta tutti a `UNKNOWN`.
- Quanto dopo un `unconfirmed` (cancel per id) o un comando accettato (mercato/evento/tutti) l'ordine risulti `INACTIVE` nelle letture: il signer rilegge subito e poi su richiesta, senza assumere nulla.
- Comportamento dell'exchange al reinvio dello stesso ordine o dello stesso `clientOrderId` dopo un timeout. La documentazione dice solo che `clientOrderId` è unico per indirizzo "across every order you currently have in the system".
- Se un 404 su id e `clientOrderId`, 60 s dopo l'invio, dimostra che l'ordine non è mai stato accettato. Per questo il verdetto "assente" riapre solo il reinvio dello stesso ordine, senza liberare esposizione.
- Se per un ordine maker `sum(fillAmount)` dei fill è uguale a `totalBetSize − remainingSize`. Il settlement si registra solo quando i due valori coincidono.

## Limiti noti di questa versione

- Nessun `RestSender` reale (rete, autenticazione, rate limit tramite il coordinatore condiviso, timeout): PR separata, owner.
- Il `postOnly` e le opzioni di invio non fanno parte del binding firmato dall'owner: sono configurazione del deploy. `postOnly` può solo ridurre l'esecuzione (non prende mai), ma resta una scelta da coprire nel binding se l'owner lo vuole.
- La riconciliazione dopo un cancel è sincrona e singola: un ordine che l'exchange toglie dal book più tardi resta riservato fino alla prossima `reconcile` (nessun polling né canale realtime nel signer).
- Ancoraggio: lo scrittore stantio può lasciare una riga locale non ancorata prima di essere respinto (rilevata, mai accettata senza una `LedgerHeadAck` firmata dall'owner). Lo store PostgreSQL deve essere un database dell'owner separato da quello del bridge MCP se si vuole che chi controlla il bridge non possa toccare l'ancora.

## Cosa manca prima di `LIVE_TRADING` (decisioni owner)

Vedi la checklist in [`docs/sxbet/wallet_and_signing.md`](../docs/sxbet/wallet_and_signing.md#checklist-di-attivazione-tutte-decisioni-dellowner).
