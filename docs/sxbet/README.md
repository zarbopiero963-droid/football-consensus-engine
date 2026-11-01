# SX Bet nel server MCP: modulo READ_ONLY + SIMULATION

Fonte tecnica: OpenAPI ufficiale **https://docs.sx.bet/openapi.json** (SX Bet API, route V3) e documentazione https://docs.sx.bet. Ogni tool READ_ONLY corrisponde a una route GET di quella specifica; i tool SIMULATION e DERIVED compongono solo quelle GET. Nessun parametro è inventato.

## Stato
| Modalità | Stato |
|---|---|
| `READ_ONLY` | **implementata**: 36 tool `sxbet_*` (tutte le 36 operazioni GET ufficiali) |
| `SIMULATION` + `DERIVED` | **implementata**: `sxbet_simulate_order` e 9 tool DERIVED (position manager, esposizione e P&L, anteprime di cancel e di chiusura, hedge, regole, stato del rischio, riconciliazione). Totale **46** tool `sxbet_*`. Nessuno firma, invia o cancella |
| `LIVE_TRADING` | **non disponibile**. Per la governance §2.6 non può essere attivata finché non esiste ed è testato un signer isolato con conferma fuori banda, verificata dal signer, per ogni singolo ordine |

Garanzia strutturale: il client (`bridge/sxbet/client.py`) implementa solo GET. In `bridge/` non esistono percorsi di codice per creare, eseguire o cancellare ordini, né per approval, transfer o firma. Lo verificano `tests/test_sxbet_read_only.py::test_no_write_capability_exists` e `tests/test_sxbet_simulation.py::test_no_submission_code_in_signer_or_sxbet_module`. La firma esiste solo nel package separato [`signer/`](../../signer/README.md), che `bridge/` non importa mai.

## Configurazione (Render, servizio MCP)
| Variabile | Default | Uso |
|---|---|---|
| `SXBET_ENABLED` | spento | registra i tool `sxbet_*` |
| `SXBET_NETWORK` | `mainnet` | `mainnet` (`https://api.sx.bet`, chain 4162) oppure `testnet` (`https://api.toronto.sx.bet`) |
| `SXBET_API_KEY` | vuoto | inviata solo come header `x-sx-api-key` alle route autenticate. Senza chiave quei tool restituiscono `KEY_NOT_CONFIGURED`, senza chiamate di rete |
| `SXBET_MAX_RPM` | 60 | budget globale per chiave (1..600 richieste per 60 s), condiviso tra processi e istanze tramite PostgreSQL quando `DATABASE_URL` è impostata; si somma ai limiti documentati per route (vedi **Rate limit**) |

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

## SIMULATION: `sxbet_simulate_order`

Fonti: https://docs.sx.bet/developers/posting-orders, https://docs.sx.bet/api-reference/eip712-order-signing, https://docs.sx.bet/developers/odds-rounding, https://docs.sx.bet/developers/time-in-force, https://docs.sx.bet/developers/taking-liquidity, https://docs.sx.bet/developers/order-lifecycle.

Parametri: `market_hash`, `betting_outcome_one`, `stake_base_units` (USDC, 6 decimali), `percentage_odds` (stringa decimale sulla scala 1e20: è il prezzo massimo accettato), `time_in_force` (`GTC`/`IOC`/`FOK`), `expiry` (unix, secondi), `idempotency_key` (8–64 caratteri `[A-Za-z0-9_-]`), `maker_address` (opzionale), `check_balance` (opzionale).

Cosa fa, solo con GET attraverso il client esistente:
1. `GET /metadata/obv3`: controlla la coerenza del dominio (`OBv3 Escrow` v1, `chainId` atteso per la rete, `verifyingContract` = `escrowAddress`, 6 decimali, step della scala, betting delay).
2. Validazione dell'intent (`bridge/sxbet/simulation.py`): hash di mercato, lato, importo intero ≥ `orderSizeMinimumBaseUnits`, quota arrotondata **per difetto** sulla scala, con il valore grezzo conservato (`requested_percentage_odds`) ed eccezioni 0,1% e 99,9%. Scadenza ≥ ora + betting delay massimo + 2 s + margine di 10 s, e ≤ 24 h (politica locale). `timeInForce` dall'enum.
3. `intent_hash` = sha256 del JSON canonico dei parametri esatti. Con `maker_address` l'intent riceve un `salt` nuovo ed è `signing_ready`: quell'hash è il valore a cui deve legarsi l'approvazione fuori banda dell'owner.
4. `GET /markets/find`: abbinamento **esatto** per hash. Un mercato `INACTIVE`, mancante o ambiguo produce `REFUSED` e l'order book non viene letto.
5. `GET /orderbook-v3/snapshot/market?showTakerPerspective=true` (richiede la chiave): simulazione del fill sul lato richiesto. Consuma i livelli a quota ≤ limite, dal più conveniente, e riporta `FULL`/`PARTIAL`/`NONE`, la quota effettiva (puntata / ritorno potenziale), la parte non eseguita e quella che resterebbe nel book (`GTC`, sopra `minRestingOrderSizeBaseUnits`) o verrebbe scartata (`IOC`, dust). `FOK` non esegue nulla se la profondità non basta. Senza book o con un book malformato: `NOT_SIMULATABLE`. La liquidità non viene mai inventata.
6. `check_balance=true`: `GET /user/balance-v3` e confronto con `availableAmount` della riga esatta (token, escrow). Una riga mancante produce `BALANCE_UNRESOLVED`, mai saldo 0.

L'output è `DERIVED_SIMULATION`, non dato del provider: non modella le commissioni, il betting delay, la prevenzione del self-match né il movimento del book. `signed` e `submitted` sono sempre `false`. `live_eligibility` usa i limiti dell'owner, che nel bridge sono zero, quindi è sempre `eligible: false` (`EXPOSURE_LIMIT_ZERO`). Nessun argomento del tool può cambiare i limiti o la modalità.

Sempre in `simulation.py`, e senza tool MCP:
- `simulate_cancel`: cancel per id simulato, con il limite `maxCancelOrders`;
- `SubmissionTracker`: macchina a stati per un futuro percorso di invio, con stati `SUBMITTED`, `UNKNOWN_AFTER_TIMEOUT`, `AMBIGUOUS_RESPONSE`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED` e `NOT_ON_EXCHANGE`. Un timeout o una risposta ambigua bloccano il retry finché la riconciliazione per id non dimostra lo stato. Un 404 di `GET /orders-v3/{orderId}` vale come assenza solo entro 10 minuti (la documentazione indica 10–20) e con `GET /fills-v3?orderId=` vuoto. Il retry è ammesso solo con la stessa chiave di idempotenza.

## DERIVED: position manager, hedging e regole (01-11-2026)

Moduli puri e deterministici, senza rete e senza firma: `bridge/sxbet/positions.py`, `bridge/sxbet/hedging.py`, `bridge/sxbet/rules.py`. Ogni valore calcolato è etichettato `DERIVED` e sta accanto alle righe del provider, riportate invariate. Un dato mancante resta `null`, con un motivo, e niente viene ricostruito con complementi. I valori che usano quote di mercato sono etichettati `HYBRID_MARKET_AWARE`.

### Modello di payout (fonti ufficiali)
- I mercati sono **binari** (outcome one / outcome two). `percentageOdds` è la probabilità implicita × 10^20 di chi la riceve; un maker a `p` offre al taker `10^20 − p` (https://docs.sx.bet/developers/unit-conversions, https://docs.sx.bet/developers/order-book).
- Ogni scommessa, maker o taker, è una puntata `s` su un esito. Se l'esito vince, il ritorno lordo (puntata inclusa) è `s × 10^20 / q`, dove `q` è la probabilità implicita del lato di chi scommette. Profitto = ritorno − puntata; se l'esito perde, si perde la puntata. Esempio ufficiale (`GET /fills-v3`): 333334 a 40% danno `returnAmount` 833335.
- **Perché non si usano le formule back/lay di Betfair.** Su SX Bet "bancare" l'esito uno equivale a puntare sull'esito due: per scommettere su un esito si consumano gli ordini che riposano sull'altro (developers/order-book). Il maker che punta `S` a `p` e il taker con la capacità documentata `S × (10^20 − p) / p` al prezzo `10^20 − p` formano un unico piatto, `S + S(10^20 − p)/p = S × 10^20 / p`. Questo valore è sia il ritorno del maker (`S × 10^20 / p`) sia quello del taker (`[S(10^20 − p)/p] × 10^20 / (10^20 − p)`). Il modello è quindi lo stesso dai due lati e non serve una formula di liability separata: la liability di una riga è la sua puntata.

### Position manager (`positions.build_positions`)
Input: i payload `data` di `GET /positions-v3`, `/trades-v3`, `/fills-v3`, `/orders-v3`, `/user/balance-v3`, `/user/fees-v3`, `/orders-v3/odds/best` (frame maker) e `/markets/find`. Opzionalmente anche le pubblicazioni WebSocket `account:orders_v3_#`, `account:trades_v3_#` e `account:fills_v3_#`, con le forme di api-reference/channel-orders-v3, channel-trades-v3 e channel-fills-v3: un oggetto per messaggio, dentro `order`, `trade` o `fill`.

Per ogni mercato, e poi per selezione, evento e totale, il position manager calcola:
- la grana usata: i fill se coprono ogni bet, altrimenti i trade, altrimenti la riga aggregata di `positions-v3` (developers/which-grain);
- la puntata abbinata per esito, il ritorno lordo potenziale per esito e il P&L se vince l'uno, se vince il due e se il mercato è void (`outcome` 0 = void, developers/bet-lifecycle);
- `P1 = Σ ritorni sull'esito uno − Σ puntate aperte`, `P2` in modo simmetrico, e la liability `max(0, −min(P1, P2))`;
- con la sola riga `positions-v3`: `P1 = maxWin` se `isOutcomeOneMaxWin`, altrimenti `maxLoss` (sono profitti, puntata esclusa, api-reference/get-positions-v3). La ripartizione della puntata e le commissioni restano `null`;
- la parte `MATCHED` non ancora `LOCKED`, riportata a parte. I bet `FAILED` sono esclusi ("no funds moved");
- gli ordini a riposo: residuo per esito, fill parziali (`totalBetSize − remainingSize`) e P&L se tutto il residuo venisse abbinato. Un ordine a riposo **non blocca capitale**, e ogni market:outcome deve stare nel saldo (developers/risk-limits);
- il P&L realizzato: `settleReturnAmount + ceRefundAmount − totalStake` (developers/capital-efficiency). Per `positions-v3` si usa il `pnl` del provider, valorizzato solo con `status=SETTLED`;
- le commissioni: la payout fee si applica al **profitto** del bet vincente, al tasso maker o taker della riga (developers/fees). Un tasso `null` significa "non impostato, nulla addebitato": il `null` grezzo resta in `fees.raw` e lo 0 compare solo in `fees.derived_rates`. Senza `/user/fees-v3` le commissioni proiettate sono `null`, non 0. Accanto c'è la commissione proiettata dal provider (`ritorno − netReturnAmount`);
- il mark-to-market, solo con una quota best odds documentata. È il P&L, uguale sui due esiti, che si avrebbe coprendo subito il gap al prezzo taker del top of book: `max(P1, P2) − |P1 − P2| × q / 10^20`. Non verifica la profondità, esclude le commissioni ed è `HYBRID_MARKET_AWARE`. Senza quota è `null`, con il motivo;
- il capitale: la riga esatta (token, escrow) di `balance-v3`, il disponibile conservativo `min(availableAmount, availableAmount + pendingAvailableAmount)` e l'escrow impegnato `escrowedAmount`.

**Riconciliazione REST/WebSocket.** Le righe si deduplicano per id documentato: `id` dell'ordine, `tradeId`, `id` del fill (`matchId:orderId:marketHash`). I duplicati identici sono scartati e contati, e vale la versione più recente per `updatedAt`. Una chiave diventa **conflitto** quando:
- ha versioni senza `updatedAt`, oppure con lo stesso `updatedAt` ma contenuto diverso;
- regredisce nel ciclo di vita (bet: MATCHED→LOCKED→SETTLED e MATCHED→FAILED; ordini: PENDING→ACTIVE→INACTIVE);
- ha `remainingSize` in aumento o `fillAmount` cambiato.

Sono conflitti anche un `totalStake` di un bet diverso dalla somma dei suoi fill (con `fillCount` completo) e un `totalStake` di `positions-v3` diverso dai bet aperti. Un conflitto viene riportato, escluso da ogni calcolo, e il mercato è marcato `data_conflict`. Una sorgente con altre pagine (`nextKey`) rende il risultato `PARTIAL`, mai completo in silenzio. Parlay e quarter-line non sono modellati: le loro righe sono elencate a parte.

### Hedging (`hedging.simulate_hedge`)
Con `P1 > P2` si punta sull'esito due con puntata `s` e ritorno `r = s × 10^20 / q`. Allora `P1' = P1 − s` e `P2' = P2 + (r − s)`, quindi il gap si riduce esattamente di `r`. La copertura totale richiede un ritorno `r = P1 − P2`, cioè `s = (P1 − P2) × q / 10^20` su un solo livello.

I livelli vengono da `GET /orderbook-v3/snapshot/market` nel frame maker, quello che la documentazione indica come autorevole (`showTakerPerspective` è "for display only"). Il livello maker `(p, size)` sull'altro esito offre il prezzo taker `q = 10^20 − p` per una puntata fino a `size × (10^20 − p) / p`. I livelli si percorrono dal prezzo migliore (price-time priority). La propria quantità a riposo viene tolta, perché "the engine never matches you against your own resting orders" (developers/taking-liquidity).
- Modalità: `FULL` pareggia gli esiti, `PARTIAL` copre una frazione del gap, `REDUCE` usa una puntata data, mai oltre la copertura totale.
- Quota peggiore accettabile: miglior prezzo taker + slittamento massimo, arrotondata **per difetto** sulla scala (developers/odds-rounding). È il `percentageOdds` del taker, "the highest implied probability you're willing to pay".
- Commissioni: l'hedge è un ordine taker, quindi `takerPayoutFee` si applica al suo profitto solo se vince. L'output riporta P&L per esito prima e dopo, commissioni, rischio residuo e liquidità.
- Stati, esattamente quattro:
  - `HEDGE_AVAILABLE`;
  - `PARTIAL_HEDGE_ONLY`: la liquidità entro lo slittamento non basta;
  - `HEDGE_UNAVAILABLE`: mercato non `ACTIVE` o sconosciuto, book mancante o malformato, nessuna liquidità, puntata sotto `orderSizeMinimumBaseUnits`, dati di posizione mancanti;
  - `HEDGE_UNCERTAIN`: dati più vecchi di `max_data_age_seconds`, commissioni non verificabili, dati di posizione in conflitto o incompleti, dimensione minima non verificata.
- **Non è mai un cashout garantito.** L'output contiene sempre l'avvertenza e `guaranteed_cashout: false`, e i parametri d'ordine proposti hanno `executable`, `signed` e `submitted` a `false`.

### Regole (`rules.evaluate`)
Regole disponibili: `stop_loss`, `take_profit`, `trailing_stop` (il picco lo passa il chiamante a ogni chiamata), `daily_loss_limit` (il realizzato del giorno lo passa il chiamante), `exposure_limit`, `close_on_odds_move`, `close_on_time_remaining`, `block_on_suspended_market` e `block_on_stale_data`.

La configurazione arriva con ogni chiamata e non viene salvata. **Tutte le regole sono spente per default.** Gli unici output sono `NO_ACTION`, `ALERT` e `ORDER_PROPOSAL`:
- una proposta descrive una chiusura tramite un hedge `FULL` simulato, con un `proposal_hash`. Non è mai eseguibile, firmata o inviata: servirebbe un'approvazione fuori banda dell'owner, verificata dal signer;
- un mercato sospeso o dati vecchi bloccano le proposte, che diventano `ALERT`;
- un dato mancante produce `ALERT`, mai una supposizione.

Le soglie sono del chiamante. I limiti reali dell'owner stanno solo nella configurazione firmata del signer, e il server MCP non li conosce.

### Nuovi tool MCP (solo GET + moduli puri)
| Tool | Letture | Cosa restituisce |
|---|---|---|
| `sxbet_order_status(order_id)` | `/orders-v3/{orderId}`, `/fills-v3?orderId=` | ordine, fill, eseguito e residuo, fill parziale, coerenza tra ordine e fill. Un 404 non prova l'assenza fuori dalla finestra di 10–20 minuti |
| `sxbet_positions_summary(event_id?)` | `positions-v3` (MATCHED,LOCKED), `trades-v3` (MATCHED e LOCKED), `orders-v3`, `balance-v3`, `fees-v3`, `metadata/obv3`, `orders-v3/odds/best` | il position manager completo |
| `sxbet_exposure_pnl(event_id?)` | `positions-v3` aperte e `SETTLED`, best odds | P&L per esito, worst case, realizzato del provider, mark-to-market |
| `sxbet_resting_orders_scope(order_ids \| market_hash \| event_id)` | `metadata/obv3`, `orders-v3` | **anteprima di cancel**: gli ordini che un cancel toccherebbe e il residuo per esito. Nessun cancel viene inviato, e una richiesta di cancel non è una conferma (`cancelled`/`notCancelled`/`unconfirmed`) |
| `sxbet_hedge_simulation(market_hash, mode, fraction?, hedge_stake_base_units?, max_slippage_percentage_odds, max_data_age_seconds)` | `markets/find`, `metadata/obv3`, `trades-v3` ×2, `orders-v3`, `fees-v3`, order book (frame maker, per ultimo) | la simulazione di hedge con i quattro stati |
| `sxbet_close_position_preview(market_hash, ...)` | come sopra | gli ordini a riposo da cancellare + un hedge `FULL`; nulla viene inviato |
| `sxbet_rules_evaluate(market_hash, rules?, ...)` | come sopra | il verdetto delle regole, il mark-to-market e i prezzi taker correnti |
| `sxbet_risk_status()` | nessuna | `LIVE_TRADING` `UNAVAILABLE`, signer non raggiungibile dal server MCP, nessun tool di scrittura, tipo di coordinatore di rate, limiti di esposizione sconosciuti al server MCP |
| `sxbet_reconciliation_diagnostics(market_hash?, ws_messages?)` | `orders-v3`, `trades-v3` ×2, `fills-v3` ×2 | dedup, conflitti e cross-check REST/WS. I messaggi WS li fornisce il chiamante (`CALLER_SUPPLIED`, massimo 500) |

Il tool di anteprima del cancel si chiama `sxbet_resting_orders_scope` e non `sxbet_cancel_preview`: i test di sicurezza (`test_no_write_capability_exists`, `test_bridge_on_render_never_loads_signer_and_exposes_no_signing_inputs`) vietano la parola "cancel" nei nomi dei tool `sxbet_*`, e quella protezione non è stata indebolita.

Tutte le chiamate passano dal client GET e dal coordinatore di rate condiviso (§2.4): un `SXBET_LOCAL_RATE_LIMITED` interrompe le letture e restituisce `retry_after_seconds`. Un input non valido o una chiave mancante non producono chiamate. Oltre i 256 KiB la risposta è `PAYLOAD_TOO_LARGE`, senza troncature. `/healthz` riporta `sxbet_rate_coordinator` (`POSTGRES_SHARED`, `LOCAL_PROCESS`, oppure `null` se SX Bet è spento), letto dall'oggetto limiter senza chiamate di rete né al database.

### `DOCS_UNVERIFIED` (non calcolati)
- Arrotondamento delle commissioni in unità base: le commissioni proiettate sono decimali esatti. I ritorni dell'hedge sono arrotondati per difetto, una scelta conservativa perché la regola dell'exchange non è documentata.
- Se `settleReturnAmount` sia lordo o netto della commissione: si usa la formula documentata così com'è, e `settleFeeAmount` è solo riportato.
- `settlement.settleFeeAmount` `null` a livello di fill: il significato non è documentato, quindi resta `null`.
- Saldo spendibile: developers/balances-and-ledger dice `available + pendingAvailable`, api-reference/get-user-balance-v3 dice `availableAmount`. Si usa il minore dei due.
- Quarter-line e parlay non sono modellati: la `marketHash` di un fill quarter-line è il leg in REST e il parent nel canale WS.

## Signer isolato

Vedi [`signer/README.md`](../../signer/README.md) e [`wallet_and_signing.md`](wallet_and_signing.md). Il signer è un **servizio separato**: non gira mai nel processo MCP, e `import signer` fallisce se manca `SXBET_SIGNER_PROCESS=1` o se è presente `RENDER` (test `tests/test_sxbet_render_isolation.py`).
- Firma EIP-712 dell'`Order` a 8 campi solo con un'approvazione Ed25519 dell'owner, monouso, TTL ≤ 120 s, legata a un *binding* v2 (account, maker, rete/chainId/contratto, evento, mercato, selezione con etichetta, lato, importo, quota limite, quota vista, commissioni attese, esposizione massima, scadenza, `clientOrderId`, `intent_hash`).
- Autorizzazione transazionale: un rifiuto non consuma l'approvazione; nonce consumato, `AUTHORIZED` ed esposizione riservata sono una sola riga fsync del ledger.
- Macchina a stati degli ordini e ledger append-only con hash chain (`signer/orders.py`): l'esposizione scende solo con evidenza dell'exchange (cancel, scadenza, settlement); `UNKNOWN` non libera mai capitale. Un ledger mancante, estraneo o manomesso blocca la firma.
- Risk engine autoritativo (`signer/risk.py`) con limiti solo da un file JSON firmato dall'owner (`SXBET_SIGNER_RISK_CONFIG`); default tutto a zero, quindi rifiuto.
- Gateway di invio (`signer/gateway.py`) **disabilitato**: si costruisce solo con un flag firmato dall'owner, e nel repository non esiste alcun trasporto di rete (solo un fake in-memory nei test).

Il signer non aggiunge tool al catalogo MCP: i 9 tool DERIVED descritti sopra sono solo lettura e simulazione.

**Unità:** `percentageOdds` è la probabilità implicita × 10^20 (per esempio `52500000000000000000` = 52,5%). Gli importi USDC sono in unità base con 6 decimali (`1000000` = 1 USDC). I dati sono restituiti così come arrivano dal provider.

**Errori** (codici fissi, mai il body del provider né la chiave): `INVALID_INPUT`, `KEY_NOT_CONFIGURED`, `SXBET_RATE_LIMITED` (con `retry_after_seconds`), `SXBET_LOCAL_RATE_LIMITED` (con `retry_after_seconds`; anche durante il cooldown dopo un 429), `SXBET_RATE_COORDINATOR_UNAVAILABLE` (coordinatore non raggiungibile o oltre il budget di tempo: nessuna chiamata di rete), `SXBET_UNAUTHORIZED`, `SXBET_HTTP_ERROR`, `SXBET_REDIRECT_REJECTED`, `SXBET_TIMEOUT` (scadenza complessiva di 30 s), `SXBET_NETWORK_ERROR`, `SXBET_INVALID_JSON`, `SXBET_PROVIDER_STATUS_NOT_SUCCESS`, `SXBET_RESPONSE_TOO_LARGE`, `PAYLOAD_TOO_LARGE`. Non ci sono retry automatici. Il cap di 256 KiB si misura sul JSON compatto della risposta; oltre il cap la risposta è `PAYLOAD_TOO_LARGE`, senza troncature.

**Rate limit** (governance §2.4, fonte: https://docs.sx.bet/developers/rate-limits): ogni richiesta SX Bet passa da un coordinatore **prima** di qualunque chiamata di rete. Una richiesta è ammessa solo se c'è capacità in **tutti** i suoi bucket, quindi il limite effettivo è min(`SXBET_MAX_RPM`, limite documentato) e non supera mai quello documentato:
- bucket globale per chiave: `SXBET_MAX_RPM` per 60 s, su tutte le richieste;
- bucket per route documentata: per chiave sulle route autenticate (per esempio `/user/balance-v3` 300/60 s, `/user/fees-v3` 120/60 s, `/orderbook-v3/snapshot/market` 27.500/300 s, `/user/multisig/*` 60/60 s), per IP sulle route pubbliche (per esempio `/metadata/obv3` 500/60 s, `/trades-v3/public` 200/60 s, e un unico budget di 500/60 s condiviso da `/markets/*`, `/fixture/*`, `/leagues*`, `/sports`, `/teams`, `/live-scores`, `/search`). Il bucket per IP è condiviso da tutte le istanze, per prudenza;
- bucket edge per il traffico non autenticato: 35.000 per 10 min;
- le route non documentate usano solo il bucket globale.

Le finestre sono scorrevoli ed esatte, e ogni slot ammesso resta consumato anche se la chiamata poi fallisce. Un 429 del provider salva un cooldown condiviso sul bucket più specifico della route (`Retry-After`, 60 s se assente, massimo 3600 s): finché è attivo ogni processo risponde `SXBET_LOCAL_RATE_LIMITED` con `retry_after_seconds`, senza chiamare il provider. Non ci sono retry automatici.

Con `DATABASE_URL` impostata (produzione) il coordinatore è PostgreSQL (`bridge/sxbet/ratelimit.py`, tabelle `sxbet_rate_attempts` e `sxbet_rate_cooldowns`, migration `sql/migrations/0007_sxbet_rate_limits.sql`, che è la fonte di verità dello schema; il runtime la ripete in modo idempotente al primo uso). Le prenotazioni sono serializzate da un advisory lock transazionale e usano solo l'orologio del database. Le chiavi dei bucket contengono uno SHA-256 della chiave API, mai la chiave. Budget di tempo: 10 s per la verifica una tantum dello schema e 3 s per ogni prenotazione; un errore o un timeout danno `SXBET_RATE_COORDINATOR_UNAVAILABLE`, senza fallback sul limiter locale. Senza `DATABASE_URL` (test, sviluppo locale) si usa un limiter in-process con la stessa semantica, valido solo per quel processo. `sxbet_health` riporta il coordinatore attivo in `rate_coordinator` (`POSTGRES_SHARED` oppure `LOCAL_PROCESS`).

## Verifica (01-11-2026, backend contro l'API pubblica reale, senza chiave)
Registro storico, precedente a `sxbet_simulate_order`: i conteggi 23/99 si riferiscono a quel deploy. Con SIMULATION il catalogo SX Bet è di 24 tool (`tools/list` offline verificato: 100 = 34 browser + 42 API-FOOTBALL + 24 SX Bet).

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
2. **SIMULATION:** implementata (`sxbet_simulate_order`). Dopo il deploy va verificata dal client MCP con la chiave.
3. **LIVE_TRADING: resta non disponibile.** Signer, risk engine, ledger degli ordini e gateway (disabilitato) esistono e sono testati offline con chiavi usa e getta, ma mancano ancora:
   - il dispositivo di approvazione dell'owner;
   - il deploy del signer come servizio separato;
   - un trasporto reale per il gateway (`POST /orders-v3` e letture di riconciliazione), revisionato e mergiato dall'owner;
   - le decisioni sul proxy wallet (`/user/deploy-proxy`, `/user/transfer-to-proxy`, operazioni finanziarie del §2.6);
   - i limiti reali e lo scope della chiave.

   Dettagli in [`signer/README.md`](../../signer/README.md). Ogni passo verso il trading reale si mergia solo dall'owner.
