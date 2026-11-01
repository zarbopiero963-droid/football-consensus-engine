# Signer isolato SX Bet

Componente separato dal server MCP (governance §2.6). **`LIVE_TRADING` resta `UNAVAILABLE`**: il package firma e contiene un gateway di invio, ma il gateway è **disabilitato** e nel repository **non esiste alcun trasporto di rete**. Nessun file di `signer/` importa librerie di rete.

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
- https://docs.sx.bet/openapi.json (`OrderV3`, `FillV3`, `TradeV3`, `/user/fees-v3`)

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
             verifica firma ↔ intent approvato, idempotenza, write-ahead UNKNOWN, riconciliazione
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

1. Verifica dell'approvazione senza consumo e controllo `nonce_consumed`.
2. Il risk engine gira **dentro il lock del ledger**, sull'esposizione corrente.
3. Solo se tutto passa viene scritta una riga `AUTHORIZE` (fsync) che consuma il nonce e riserva l'importo.
4. Firma; poi la riga `SIGNED`; solo allora la firma esce dalla funzione.

Ne seguono queste proprietà:
- un rifiuto (limiti, saldo, dati scaduti…) **non** consuma l'approvazione, che si può riusare quando i limiti lo permettono;
- due richieste concorrenti con la stessa approvazione producono una sola firma;
- un crash tra consumo e firma lascia l'ordine `AUTHORIZED` con il nonce consumato e nessuna firma emessa. Dopo il riavvio `Signer.abandon_unsigned(client_order_id)` lo porta a `REJECTED` e libera l'esposizione. È sicuro perché nessuna firma esce prima della riga `SIGNED`;
- un fallimento della firma nel processo porta subito a `REJECTED` (`SIGNING_FAILED`);
- due `client_order_id` con lo stesso digest EIP-712 sono rifiutati (`ORDER_ID_ALREADY_AUTHORIZED`).

## Stati degli ordini ed esposizione (`signer/orders.py`)

Stati: `PREPARED`, `AUTHORIZED`, `SIGNED`, `SUBMITTED`, `OPEN`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `EXPIRED`, `SETTLED`, `REJECTED`, `UNKNOWN`. Sono ammesse solo le transizioni di `TRANSITIONS`; ogni altra è `ILLEGAL_TRANSITION_*` e non viene scritta.

| Stato | Riservato | Matched |
|---|---|---|
| `AUTHORIZED`, `SIGNED`, `SUBMITTED`, `OPEN`, `UNKNOWN` | importo − matched (cioè l'intero importo) | matched |
| `PARTIALLY_FILLED` | parte non eseguita, finché l'exchange non conferma cancel o scadenza | parte eseguita |
| `FILLED`, `CANCELLED`, `EXPIRED` | 0 | parte eseguita, fino a `SETTLED` |
| `SETTLED`, `REJECTED` | 0 | 0 (`SETTLED` registra il P&L realizzato) |

- `UNKNOWN` (timeout, risposta ambigua, write-ahead prima di un invio) **non libera mai capitale** finché non è riconciliato con dati dell'exchange.
- La riconciliazione usa `evidence_from_exchange`, con i campi OpenAPI di `GET /orders-v3/{orderId}` (`id`, `totalBetSize`, `remainingSize`, `status`, `inactiveReason`), di `GET /orders-v3/client/{clientOrderId}` e di `GET /fills-v3?orderId=` (`orderId`, `fillAmount`, `status`, `settlement.settleReturnAmount`).
  - `INACTIVE` con `FILLED` porta a `FILLED`, con `EXPIRED` a `EXPIRED`, con gli altri `inactiveReason` documentati a `CANCELLED`.
  - Un motivo o uno stato sconosciuto non insegna nulla.
  - Il settlement richiede fill completi, tutti `SETTLED` e con somma uguale al matched.
- Un 404 prima della scadenza non dimostra nulla. Dopo la scadenza (più 30 s), un 404 con lista dei fill completa e vuota porta a `EXPIRED`.
- Da `UNKNOWN` si torna a `SIGNED` (unico percorso verso un reinvio) solo con: 404 per id **e** per `clientOrderId`, fill vuoti e almeno 60 s dall'ultimo tentativo. Il reinvio usa **lo stesso ordine firmato e lo stesso `clientOrderId`**.
- Evidenze duplicate o che non cambiano nulla non scrivono righe. Il matched non scende mai e non supera l'importo.
- **Persistenza:** JSONL append-only, fsync per riga, hash chain, `GENESIS` legato a `signer_id` e indirizzo. Stato, nonce, idempotenza, esposizione e P&L si **ricostruiscono sempre** rileggendo il ledger e ripassando ogni riga dalla macchina a stati.
  - Un ledger mancante, vuoto, estraneo, manomesso o con una storia illegale (anche se re-hashata) genera `LedgerError`, e il signer non si avvia.
  - Un nuovo ledger si crea solo con `OrderLedger.create` esplicito, mai "partendo da zero" in silenzio.

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

- `SubmissionGateway` si rifiuta di costruirsi senza un `LiveTradingFlag` con `source == "OWNER_SIGNED"`. Il flag si ottiene solo da `load_live_trading_flag`, cioè da un file firmato dall'owner, legato a `signer_id` e rete, con TTL ≤ 24 h. Il default è `DISABLED`.
- Il trasporto è un `Protocol` astratto (`submit_orders`, `get_order`, `get_order_by_client_id`, `get_fills`). Nel repository **non esiste alcuna implementazione di rete**: i test usano un `FakeTransport` in memoria.
- Il gateway verifica:
  - che l'intent rivalidato abbia lo stesso `intent_hash` e lo stesso order id;
  - che il payload sia esattamente `eip712.order_payload(intent, firma)`;
  - che la firma recuperi l'indirizzo del maker e del ledger;
  - che l'ordine sia nel ledger con lo stesso hash.
- Payload `{"orders": [ {marketHash, maker, totalBetSize, percentageOdds, salt, expiry, baseToken, isMakerBettingOutcomeOne, timeInForce, orderSignature, clientOrderId} ]}`. `waitForOutcome` non viene inviato.
- **Write-ahead:** `SIGNED → UNKNOWN(SEND_ATTEMPT)` è persistito prima della chiamata. Un crash durante l'invio lascia `UNKNOWN`, mai "non inviato".
- Classificazione della risposta:
  - 200 con una riga `SUBMITTED` per il nostro id porta a `SUBMITTED`;
  - 400 con `message`, oppure una riga `FAILED`, porta a `REJECTED`;
  - tutto il resto (timeout, errori, 401/403/5xx, body malformato, id diverso) porta a `UNKNOWN`.
- **Idempotenza:**
  - lo stesso `clientOrderId` non esce mai due volte (fuori da `SIGNED` restituisce lo stato senza inviare);
  - in `UNKNOWN` risponde `STATE_UNKNOWN_RECONCILE_FIRST`;
  - un payload diverso con lo stesso id viene rifiutato;
  - gli invii concorrenti passano da un lock unico.

## Custodia della chiave

Il signer legge la chiave una sola volta, all'avvio, dalla propria variabile d'ambiente `SXBET_SIGNER_PRIVATE_KEY`. Non la restituisce, non la scrive in log, audit o ledger, e `repr()` la nasconde. L'oggetto non è serializzabile. Una chiave mancante o malformata produce un errore con codice fisso, senza riportarne il valore.

## Dipendenze

`signer/requirements.txt` (`eth-account` 0.14.x, `cryptography`). **Non** sono in `requirements.txt`: il runtime Render del bridge non riceve librerie di firma. La CI (`multi-market-tests.yml`, job `pytest`) le installa solo per eseguire i test. `tests/conftest.py` imposta `SXBET_SIGNER_PROCESS=1` per il processo di test.

## DOCS_UNVERIFIED

- Formato di `salt` in `POST /orders-v3`: lo schema dice "32-byte hex string", l'esempio Python ufficiale invia la stringa decimale dello stesso uint256. Qui resta la stringa decimale (esempio Python); va confermato su testnet.
- Comportamento dell'exchange al reinvio dello stesso ordine o dello stesso `clientOrderId` dopo un timeout. La documentazione dice solo che `clientOrderId` è unico per indirizzo "across every order you currently have in the system".
- Se un 404 su id e `clientOrderId`, 60 s dopo l'invio, dimostra che l'ordine non è mai stato accettato. Per questo il verdetto "assente" riapre solo il reinvio dello stesso ordine, senza liberare esposizione.
- Se per un ordine maker `sum(fillAmount)` dei fill è uguale a `totalBetSize − remainingSize`. Il settlement si registra solo quando i due valori coincidono.

## Cosa manca prima di `LIVE_TRADING` (decisioni owner)

Vedi la checklist in [`docs/sxbet/wallet_and_signing.md`](../docs/sxbet/wallet_and_signing.md#checklist-di-attivazione-tutte-decisioni-dellowner).
