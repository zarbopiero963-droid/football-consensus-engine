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
client MCP (chat dell'owner) ─► bridge: intent canonico + testo letterale del comando dell'owner
                                 (nessuna chiave, nessuna firma; client e tool MCP: PR successive)
                                                   │  OwnerCommandAuthorization(command_id monouso, command_text,
                                                   │  command_text_sha256, received_at, client_ref_sha256)
                                                   ▼
signer/ (processo separato, SXBET_SIGNER_PROCESS=1, mai su Render MCP)
  core.py    0. kill switch (ledger): se ingaggiato, nessun ordine nuovo
             1. rivalida l'intent e ricalcola l'hash
             2. command.py: verifica il comando SENZA consumarlo (hash del testo, finestra breve firmata
                dall'owner, id e testo mai usati), lo analizza con la propria grammatica (IT/EN) e confronta
                OGNI campo con l'ordine e con la riga di mercato (COMMAND_MISMATCH_<CAMPO>)
             3. risk.py v2: limiti firmati dall'owner + dati di mercato freschi, contatori dal ledger
             4. orders.py: una riga fsync = id e testo del comando consumati + AUTHORIZED + esposizione riservata
             5. firma EIP-712; la riga SIGNED è persistita prima di restituire la firma
  gateway.py DISABILITATO salvo flag firmato dall'owner; rifiuta ogni invio a kill switch ingaggiato;
             verifica firma ↔ intent autorizzato, idempotenza, write-ahead UNKNOWN, batch non atomici, riconciliazione
  rest_adapter.py  builder di POST /orders-v3 e delle letture + parser delle risposte (RestSender astratto)
  cancellation.py  cancel per id / mercato / evento / tutti: comando esplicito dell'owner per ogni cancel,
             rilascio dell'esposizione solo da letture dell'exchange
  killswitch.py    stop di emergenza (manuale o automatico deterministico): blocca, registra gli intenti di
             cancel, verifica dalle letture; rilascio solo con atto firmato dall'owner. Non invia nulla
  anchor.py  ancoraggio esterno della testa del ledger + lease di scrittura con fencing token (PostgreSQL)
  audit.py   audit JSONL append-only con hash chain (allowlist di campi, mai chiave, firma o testo del comando)
```

## Comando esplicito dell'owner (`signer/command.py`) — sostituisce l'`Approval` Ed25519 per ordine

Governance §2.6 (riscritta) e decisioni dell'owner nella issue #187. **Modifica non retrocompatibile, intenzionale:** l'`Approval` Ed25519 per singolo ordine e la `CancelApproval` per singolo cancel non esistono più (`signer/approval.py` è stato rimosso); i test che li usavano sono stati aggiornati. Restano firmati dall'owner solo gli atti non legati a un singolo ordine: configurazione di rischio, flag `LIVE_TRADING`, conferma della testa del ledger e rilascio del kill switch.

Il signer riceve un `OwnerCommandAuthorization`:
- `command_id`: identificativo **nuovo e monouso** del comando (decisione 2). Si consuma nella riga `AUTHORIZE` / `CANCEL_REQUEST` del ledger e resta consumato per sempre, a prescindere dalla finestra temporale e anche dopo un riavvio (`COMMAND_ID_ALREADY_CONSUMED`);
- `command_text`: il testo letterale scritto dall'owner; `command_text_sha256` il suo sha256 UTF-8, ricalcolato dal signer (`COMMAND_TEXT_HASH_MISMATCH`). Nel ledger e nell'audit finiscono **solo gli hash**, mai il testo;
- `received_at`: istante di ricezione da parte del MCP. Finestra breve `command_max_age_seconds` della configurazione di rischio firmata (massimo 300 s): `COMMAND_EXPIRED`, `COMMAND_NOT_YET_VALID`;
- `client_ref_sha256`: hash del riferimento al client / conversazione (solo audit).

Si consuma anche l'**impronta del comando analizzato** (`command_fingerprint_sha256`, revisione PR #196 P2-2): evento, mercato, lato, importo in unità base, prezzo firmato, tipo d'ordine, time in force dopo i default firmati, post-only **e il token di riferimento esplicito**. Scritture equivalenti (maiuscole, virgola finale, «2,5»/«2.50», FOK esplicito uguale al default) hanno la stessa impronta: lo stesso comando non autorizza una seconda operazione (`COMMAND_REPLAYED`). Per ripetere deliberatamente lo stesso ordine (o un cancel / cancel-all di riduzione del rischio) l'owner scrive un nuovo comando con un **nuovo token di riferimento**, per esempio «(rif. 2)».

**Un comando = una operazione.** Due operazioni nello stesso testo → `COMMAND_MULTIPLE_OPERATIONS`; una modifica → `COMMAND_MODIFY_NOT_SUPPORTED` (decisione 1: modifica = comando di cancel + comando di nuovo ordine, verificati separatamente).

Grammatica deterministica (IT + EN, virgola o punto decimale, nessun separatore delle migliaia):
- `Piazza 10 USDC su Over 2,5 in Cape Verde–Liberia a quota minima 1,90` → ordine **Market** (taker, `FOK`/`IOC`; default firmato `market_time_in_force`, oppure esplicito «FOK»/«IOC»/«tutto o niente»);
- `Metti un Limit a quota 2,10 per 10 USDC su Over 2,5 in Cape Verde–Liberia [post-only]` → ordine **Limit** (maker, `GTC`);
- `Place 10 USDC on Liberia +1.5 in Cape Verde vs Liberia at min odds 1.90 FOK`;
- evento come `Squadra1–Squadra2` / `Squadra1 vs Squadra2` oppure `evento <sportXeventId>`; tipo di mercato del provider facoltativo `(tipo mercato 52)`;
- `Cancella l'ordine 0x…`, `Cancella gli ordini 0x… e 0x…`, `Cancella gli ordini sul mercato 0x…`, `Cancella gli ordini dell'evento <id>`, `Cancella tutti gli ordini` / `Cancel all orders`.

Ogni altra parola è un rifiuto (`COMMAND_UNPARSEABLE`): nessuna lettura fuzzy. Slot mancante → `COMMAND_INCOMPLETE_<CAMPO>` (`EVENT`, `SELECTION`, `STAKE`, `CURRENCY`, `ODDS`, `ORDER_TYPE`, `LINE`, `ORDER_IDS`); doppia lettura → `COMMAND_AMBIGUOUS_<CAMPO>` (per esempio «1.000 USDC»: migliaia o decimali?).

Confronto campo per campo (`verify_place`), su dati del signer e mai sul parsing del chiamante:
- **evento** (§2.3bis): nomi canonici esatti di `teamOneName`/`teamTwoName` nell'ordine del provider, oppure l'id `sportXeventId`;
- **mercato e linea**: Over/Under richiede etichette Over/Under e una linea; una linea con segno richiede una linea del mercato; un mercato con linea non citata è `COMMAND_INCOMPLETE_LINE`; |linea| uguale; tipo del provider se citato;
- **selezione**: etichetta canonica esatta dell'esito firmato (`outcomeOneName`/`outcomeTwoName`);
- **unicità** (§2.3bis, ambiguità = nessun match): fra tutti i mercati dell'evento (`event_markets`, da `GET /markets/active?eventId=`) il comando deve identificarne **uno solo**, quello dell'ordine (`COMMAND_AMBIGUOUS_MARKET`, `EVENT_MARKETS_MISSING`, `EVENT_MARKETS_INCOMPLETE`);
- **importo e valuta**: Decimal esatto → unità base con i decimali del token presi dai metadati runtime (`COMMAND_INVALID_STAKE_PRECISION` sotto l'unità base, mai arrotondato); simbolo uguale a quello dei metadati;
- **quota**: probabilità implicita `floor(1e20 / quota)` e arrotondamento **per difetto** sulla scala: il prezzo firmato non è mai peggiore del limite dell'owner, ed è esattamente quello (nessuna tolleranza di slippage);
- **tipo d'ordine, time in force, post-only**: dal comando o dai default firmati dall'owner, mai una scelta del modello.

Copertura universale (decisione 4): nessuna whitelist funzionale di sport, tipi di mercato o linee; le allowlist sono politica dell'owner nella configurazione di rischio (default ammesso `ANY`).

### Rischio residuo accettato dall'owner

Un testo in chat, in un output di tool o in una pagina web può essere scritto da chiunque: dal modello stesso, da una prompt injection, da un terzo. Senza un fattore fuori banda il signer **non può distinguere crittograficamente** un comando dell'owner da uno iniettato o generato per errore. I controlli sopra (testo completo e coerente, monouso, finestra breve, unicità del mercato) e soprattutto i **limiti obbligatori** firmati dall'owner e il kill switch **limitano** il danno, non lo eliminano (§2.6).

## Autorizzazione transazionale

0. **Snapshot:** il comando (ogni campo letto una sola volta, tipi controllati) e le **letture del signer** (dal suo provider, mai dal chiamante) vengono copiati in valori semplici prima di ogni verifica; da lì in poi si usa solo la copia. Un valore non semplice produce `COMMAND_MALFORMED` o `RISK_CONTEXT_MALFORMED`.
1. Verifica del comando senza consumo, controllo di id e testo già consumati.
2. Il risk engine gira **dentro il lock del ledger**, sull'esposizione e sui contatori correnti.
3. Solo se tutto passa viene scritta una riga `AUTHORIZE` (fsync) che consuma id e hash del testo e riserva l'importo.
4. Firma; poi la riga `SIGNED`; solo allora la firma esce dalla funzione.

Ne seguono queste proprietà:
- un rifiuto (limiti, saldo, dati scaduti…) **non** consuma il comando, che si può riusare entro la finestra quando i limiti lo permettono;
- due richieste concorrenti con lo stesso id (o lo stesso testo) producono una sola autorizzazione;
- un crash tra consumo e firma lascia l'ordine `AUTHORIZED` con il comando consumato e nessuna firma emessa. Dopo il riavvio `Signer.abandon_unsigned(client_order_id)` lo porta a `REJECTED` e libera l'esposizione. È sicuro perché nessuna firma esce prima della riga `SIGNED`;
- un fallimento della firma nel processo porta subito a `REJECTED` (`SIGNING_FAILED`);
- se la riga `SIGNED` non si riesce a scrivere, la firma viene scartata e non esce (`LEDGER_WRITE_FAILED`): l'ordine resta `AUTHORIZED` e riservato;
- due `client_order_id` con lo stesso digest EIP-712 sono rifiutati (`ORDER_ID_ALREADY_AUTHORIZED`).

## Revisione avversaria della PR #196 (esito UNSAFE): correzioni

- **P1-1 letture del signer:** `sign_order(intent, authorization, *, post_only)` non accetta più un contesto (modifica intenzionale). Mercato, mercati dell'evento, eventi con le stesse squadre, quota, saldo, commissioni e metadati arrivano solo dal `MarketDataProvider` configurato in `SignerConfig.market_data`; senza provider `SIGNER_MARKET_READS_UNAVAILABLE`, errore del provider `SIGNER_MARKET_READS_FAILED`. Il mercato risolto dalle letture (un evento, un mercato) deve essere quello firmato (`COMMAND_MISMATCH_MARKET`).
- **P1-2 token:** simbolo e decimali del token sono configurazione dell'owner (`SignerConfig.base_token_symbol`, `base_token_decimals`, legati a `base_token`); mancanti → `SIGNER_TOKEN_CONFIG_MISSING`; un valore dei metadati diverso → `TOKEN_DECIMALS_DIFFER_FROM_EXCHANGE`.
- **P1-3 ambiguità fra eventi (§2.3bis):** il provider restituisce anche gli eventi attivi con gli stessi nomi delle squadre (con orario, competizione, paese, categoria se disponibili); se il comando ne identifica più di uno → `COMMAND_AMBIGUOUS_EVENT`, salvo `evento <id>`. Solo nomi canonici esatti; nessun fuzzy o contenimento. `SAME_TEAM_EVENTS_MISSING` / `_INCOMPLETE` se la lettura manca o non contiene l'evento dell'ordine.
- **P2-1 kill switch:** il controllo si ripete sotto il lock del ledger dentro `authorize` (prima e dopo i controlli di rischio) e dopo `AUTHORIZE` (un ingaggio in quel momento porta l'ordine a `REJECTED` senza firma). `ENGAGE` e la fotografia degli intenti (`AUTHORIZED`, `SIGNED` non scaduti, ordini sul book) sono scritti sotto un unico possesso del lock.
- **P2-2 replay:** impronta del comando analizzato + token di riferimento (vedi sopra).
- **P2-3 post-only per ordine** (vedi Adapter REST).
- **P2-4 monitor senza flag:** il lock di invio appartiene al ledger (`ledger.send_lock`, usato da gateway, cancellazioni e monitor); il monitor non richiede più un gateway costruibile, quindi riconcilia e alimenta il kill switch anche a flag scaduto.
- **P2-5 identità del ledger:** la riga `GENESIS` contiene un nonce casuale e il flag `LIVE_TRADING` firmato dall'owner contiene `ledger_genesis_sha256`: un nuovo file di ledger (id consumati, kill switch e contatori azzerati) non è mai live senza un nuovo flag (`LIVE_TRADING_FLAG_NOT_FOR_THIS_LEDGER`). Modifica intenzionale del formato del flag.
- **P3:** solo cifre ASCII (`COMMAND_NON_ASCII_DIGITS`); ogni numero con esattamente tre decimali è ambiguo (`COMMAND_AMBIGUOUS_STAKE` / `_ODDS` / `_LINE`; si scrive «2,0000»); la finestra della `CancellationService` viene dalla configurazione di rischio firmata (`risk_limits=`); l'ingaggio volatile del kill switch è visto da signer e gateway; l'orologio dell'host è una radice di fiducia: con l'orologio reale uno scarto oltre 120 s rispetto all'orologio monotono blocca la firma (`SIGNER_CLOCK_JUMP_DETECTED`).

## Kill switch (`signer/killswitch.py`, decisione D7 / #187 decisione 3)

- **Attivazione** manuale (`engage`, non serve firma: riduce solo il rischio) o **automatica** (`evaluate_automatic`) solo a condizioni deterministiche: `DAILY_LOSS_LIMIT_BREACHED` (perdita realizzata del giorno UTC > 0 e ≥ `max_daily_loss_base_units`), `EXPOSURE_ABOVE_LIMIT` (esposizione del ledger oltre `max_total_exposure_base_units`, per esempio per fill tardivi), `RECONCILIATION_MISMATCH` (ordini nostri sconosciuti al ledger o evidenze in conflitto, per esempio gli alert `EVIDENCE_CONFLICT` del monitor), `LEDGER_ANCHOR_FORK` (fork, fencing, testa diversa, ledger avvelenato). Stessi input, stessa decisione.
- **Effetti**, tutti nel ledger append-only (sopravvivono al riavvio) e nell'audit: riga `KILL_SWITCH ENGAGE`; da quel momento `sign_order` e il gateway rifiutano ogni nuovo ordine (`KILL_SWITCH_ENGAGED`), mentre letture, riconciliazione e cancel comandati dall'owner restano possibili; riga `CANCEL_INTENT` con gli id degli ordini che possono essere sul book.
- **Nessun invio in questa PR:** il kill switch non ha trasporto, non firma e non crea esposizione; registra gli **intenti** di cancel. L'invio effettivo (solo per ridurre il rischio, solo su quella lista) arriva con la PR del sender. `verify_cancellations` registra (`CANCEL_VERIFIED`) quali cancel risultano davvero avvenuti nel ledger riconciliato (`CONFIRMED`) e quali no (`STILL_OPEN`): una risposta di cancel non è mai una prova.
- Se il ledger non è scrivibile (anomalia, avvelenato) il kill switch resta attivo in memoria e lo registra nell'audit; il signer rifiuta comunque ogni firma su un ledger avvelenato.
- **Persistenza fuori dal ledger (gate P2-A dell'owner, issue #187).** Prima di ogni riga `ENGAGE` il kill switch scrive un **marcatore** separato dal ledger (`<ledger>.killswitch.json`, configurabile con `kill_marker_path`, scrittura atomica con fsync del file e della directory, permessi 0600; contiene solo schema, `ledger_id`, seq del ledger al momento della marcatura, trigger, fonte, istante e id casuale: mai chiavi, firme o testo di comandi). Ogni `OrderLedger.open/create` lo verifica:
  - marcatore assente: nessun effetto;
  - ingaggio registrato nel ledger dopo il marcatore e ancora attivo: `CONSISTENT`;
  - ingaggio registrato dopo il marcatore e rilasciato con l'atto firmato dell'owner: marcatore obsoleto, rimosso (`STALE_REMOVED`);
  - nessun ingaggio dopo il marcatore (scrittura persa, coda troncata, ledger ricreato) oppure marcatore illeggibile, malformato o di un altro ledger: `UNRECORDED` / `INVALID`, il ledger resta ingaggiato in memoria dalla prima istruzione, quindi `sign_order` e il gateway rifiutano (`KILL_SWITCH_ENGAGED`) anche senza un oggetto `KillSwitch`.

  Costruire il `KillSwitch` **registra** l'ingaggio mancante nel ledger (riduce soltanto il rischio, nessuna firma richiesta, audit `KILL_SWITCH_MARKER_*`); da lì si riprende solo con il rilascio firmato dall'owner, che poi rimuove il marcatore. Se né il marcatore né la riga `ENGAGE` si possono scrivere, il file del ledger viene **messo in quarantena** (rinominato in `<ledger>.killswitch-quarantine-<ts>`, contenuto invariato; un rename funziona anche quando le scritture falliscono): il riavvio trova `LEDGER_MISSING` e nulla riprende senza l'owner, e un ledger nuovo ha una nuova identità che non è mai live senza un nuovo flag firmato (P2-5). Il signer **non riprende mai da solo**.
  - Correzioni dopo la review indipendente di `30f5a96`:
    - ingaggio e rilascio sono serializzati sul `kill_lock` del ledger, e il rilascio rimuove il marcatore solo se precede l'ingaggio rilasciato: il marcatore di un ingaggio successivo non si cancella mai;
    - il marcatore è legato al **file** del ledger (`genesis_sha256`), non solo a `signer_id:indirizzo`;
    - il file temporaneo ha nome casuale e non segue link simbolici;
    - se il marcatore JSON non si può scrivere si crea un **flag vuoto** (`<marcatore>.flag`, nessun blocco di dati), creato anche prima di una quarantena;
    - un ingaggio superato da un nuovo ingaggio non richiede un secondo rilascio.
  - Il percorso del marcatore deve restare **fisso** fra un avvio e l'altro: un marcatore in un percorso diverso non viene visto.
  - Un flag presente vale sempre `UNRECORDED` (non porta una seq e può appartenere a un ingaggio successivo a quello nel ledger): il `KillSwitch` registra un nuovo ingaggio che sostituisce il precedente, il flag viene sostituito dal marcatore JSON e l'owner firma il rilascio di **quell**'ingaggio.
  - **Procedura dell'owner** per qualunque stato `UNRECORDED` / `INVALID` o per un ledger in quarantena: (1) **non** rimettere a mano il file in quarantena al suo percorso senza i passi successivi; (2) riportarlo al suo percorso, costruire il `KillSwitch`, che registra l'ingaggio nel ledger; (3) firmare il rilascio (`KillSwitchRelease`) fuori dal MCP, che rimuove marcatore e flag. `KILL_SWITCH_MARKER_KEPT_LATER_ENGAGEMENT` indica un marcatore conservato perché appartiene a un ingaggio successivo (serve il suo rilascio). Se il rilascio registra `KILL_SWITCH_MARKER_UNREMOVABLE` (marcatore, flag o bit dell'inode non rimovibili, per esempio una directory o un file illeggibile) il signer resta bloccato a ogni avvio finché l'owner non rimuove a mano quel percorso dopo aver verificato il ledger.
  - **Testimone sull'inode del ledger** (review `f667594` P2-1): prima di una quarantena il signer imposta con `fchmod` il bit set-group-ID sul file del ledger. È un aggiornamento dei metadati dell'inode: nessun blocco di dati o di directory, quindi riesce anche quando le scritture falliscono. `mv` e `cp -p` lo conservano, quindi un ledger rimesso a mano al suo percorso si apre **ingaggiato** (`UNRECORDED`) finché non si segue la procedura dell'owner, che alla fine lo cancella. Vale in modalità locale e ancorata (l'ancora PostgreSQL registra solo la testa del ledger e da sola non protegge da questo caso).
  - Ogni riavvio con il flag presente e il marcatore JSON non scrivibile aggiunge un nuovo `ENGAGE` (limitato ai riavvii; termina con il rilascio dell'owner della seq **corrente**): con un supervisore che riavvia in ciclo il ledger cresce di due righe per riavvio.
  - **Rischi residui dichiarati:**
    - se falliscono marcatore, flag, `fchmod` **e** rename resta solo l'ingaggio in memoria, registrato nell'audit (`KILL_SWITCH_NOT_PERSISTED`);
    - chi ha accesso in scrittura all'host del signer può cancellare marcatore, flag o il bit dell'inode (`chmod g-s`): il signer non può difendersi da un'azione manuale sul proprio filesystem, per questo l'host del signer è accessibile solo all'owner;
    - `cp` senza `-p` (o una copia da backup che non conserva il modo) perde il bit dell'inode.
  - Aggiornamento da `30f5a96`: un marcatore scritto da quella versione non ha `genesis_sha256` e risulta `INVALID` (fallisce chiuso): il recupero registra l'ingaggio e serve un rilascio dell'owner.

  Test: `tests/test_sxbet_killswitch_persistent.py`.
- **Rilascio** solo con un atto dell'owner fuori dal MCP: `KillSwitchRelease` firmato Ed25519 (dominio `sxbet.kill_switch_release.v1`) su `{ledger_id, engage_seq, release_id, issued_at, expires_at}`, TTL ≤ 300 s, verificato con la chiave pubblica della configurazione del signer (anche da file, `load_release_file`). È legato alla riga `ENGAGE` che rilascia e il suo id è monouso (`KILL_SWITCH_RELEASE_*`). Una condizione automatica ancora vera riattiva il kill switch alla valutazione successiva.

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
- **Persistenza:** JSONL append-only, fsync per riga, hash chain, `GENESIS` legato a `signer_id` e indirizzo. Stato, nonce e id dei comandi consumati, idempotenza, esposizione, contatori di rischio, kill switch e P&L si **ricostruiscono sempre** rileggendo il ledger e ripassando ogni riga dalla macchina a stati.
  - Un ledger mancante, vuoto, estraneo, manomesso o con una storia illegale (anche se re-hashata) genera `LedgerError`, e il signer non si avvia.
  - Un nuovo ledger si crea solo con `OrderLedger.create` esplicito, mai "partendo da zero" in silenzio.
- **Un solo scrittore:** un `OrderLedger` tiene un `flock` esclusivo non bloccante sul file per tutta la sua vita (fino a `close()`), sia in modalità locale sia in modalità ancorata. Una seconda istanza, nello stesso processo o in un altro, fallisce con `LEDGER_LOCKED`: due signer non possono accettare lo stesso nonce o lo stesso comando.
  - Prima di ogni append l'istanza verifica che il file sia ancora quello bloccato (stesso inode), che abbia la dimensione che ha scritto e che termini con la riga il cui hash è la testa in memoria. Altrimenti (uno scrittore esterno che ignora il lock advisory, un file sostituito) `LEDGER_HEAD_MISMATCH`.
  - Un append fallito o parziale, o una testa diversa, **avvelena** l'istanza: ogni scrittura successiva dà `LEDGER_POISONED` fino a riavvio e replay. Una riga finale troncata fa poi fallire il replay (`LEDGER_CORRUPT`) e richiede una decisione dell'owner.
  - Limiti residui: il controllo prima dell'append legge solo dimensione e ultima riga (una riscrittura di righe precedenti a parità di dimensione si rileva al replay successivo); in modalità locale `flock` è advisory e locale, quindi il ledger deve stare su un filesystem locale dell'host del signer. La modalità ancorata qui sotto toglie questa dipendenza.
- Le righe `CANCEL_REQUEST` (vedi Cancellazione) consumano id e hash del testo del comando di cancel dell'owner (o, per righe precedenti, il nonce di un'approvazione di cancel) e registrano cosa è stato chiesto; non cambiano stato né esposizione di alcun ordine.

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
  - L'owner firma con la stessa chiave Ed25519 di configurazione di rischio, rilascio del kill switch e flag `LIVE_TRADING`, con dominio `sxbet.ledger_head_ack.v1`, il messaggio JSON canonico `{ledger_id ("<signer_id>:<indirizzo>"), anchored {seq, head_hash} oppure null, local {seq, head_hash}, issued_at, expires_at}`. Il TTL è ≤ 300 s, con tolleranza di clock di 5 s. Il signer conserva solo la chiave pubblica, presa dalla propria configurazione e mai dalla conferma.
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
- **`PostgresAnchorStore(connect)`** (migration `sql/migrations/signer/0001_signer_ledger_anchor.sql`, serie signer separata, tracciata in `signer_schema_migrations`, additiva):
  - lease: una sessione dedicata tiene `pg_try_advisory_lock` sulla chiave del ledger_id e, sotto il lock, incrementa `signer_ledger_writer_leases.fencing_token`; se la sessione muore PostgreSQL rilascia il lock;
  - avanzamento: transazione breve (`statement_timeout` e `lock_timeout` 2,5 s) che blocca la riga del lease `FOR UPDATE`, controlla token e seq attesa e inserisce la nuova testa in `signer_ledger_anchors`;
  - il database stesso rifiuta `UPDATE`, `DELETE`, `TRUNCATE`, una seq non crescente e un token diverso da quello del lease (trigger). Richiede PostgreSQL ≥ 14 (`CREATE OR REPLACE TRIGGER`);
  - anche `signer_ledger_writer_leases` è protetta da trigger (finding E): un `UPDATE` che non aumenta strettamente `fencing_token` (`SIGNER_LEDGER_LEASE_TOKEN_NOT_INCREASING`) o che cambia `ledger_id` (`SIGNER_LEDGER_LEASE_ID_IMMUTABLE`) è rifiutato. Sono rifiutati anche `DELETE` e `TRUNCATE` (`SIGNER_LEDGER_LEASE_NO_DELETE`), perché una riga cancellata e reinserita a token 1 abbasserebbe il token. Uno scrittore stantio non può quindi tornare valido;
  - la factory di connessione è iniettata: `signer/` non importa driver; il processo del signer passa per esempio `psycopg.connect` con il proprio DSN, fuori da Render MCP.
- La migration signer non viene applicata da sola a nessun database e il runner bridge non la applica mai (`app.db.migrate()` applica solo la serie bridge e non è chiamata dal runtime): applicarla al database del signer (`apply_migrations(connect, series=SIGNER)`) è un passo esplicito dell'owner.

## Risk engine v2 (`signer/risk.py`)

I limiti si leggono **solo** da un file JSON il cui percorso è in `SXBET_SIGNER_RISK_CONFIG`, firmato Ed25519 dalla chiave dell'owner (dominio e schema `sxbet.risk_config.v2`, con `config_version`). **Modifica non retrocompatibile, intenzionale:** un file v1 viene rifiutato (`MALFORMED`, limiti a zero). I valori di **testnet e mainnet sono separati** (`networks.mainnet`, `networks.testnet`); il signer usa solo il blocco della propria rete (`RISK_CONFIG_NETWORK_MISMATCH` se gli si passa l'altro) e una rete assente ha i limiti a zero (`NETWORK_NOT_CONFIGURED`).
- Un file mancante, non firmato, malformato o con firma non valida produce i limiti tutti a zero, cioè il rifiuto di ogni ordine (`EXPOSURE_LIMIT_ZERO`): il default sicuro è zero esposizione live.
- `RiskLimits` è immutabile e si carica una volta all'avvio. Nessun campo di intent, comando o contesto può cambiarlo.
- **I contatori non stanno solo in memoria:** importo del giorno UTC, ordini nell'ultima ora e nel giorno, ultimo ordine e perdita realizzata si ricalcolano dal ledger append-only (righe `AUTHORIZE` con il loro `ts`, righe `SETTLED`) a ogni riavvio. Ogni `AUTHORIZE` conta, anche se poi rifiutato o abbandonato (prudente).

| Limite (per rete) | Codice di rifiuto |
|---|---|
| `max_stake_per_order_base_units` | `STAKE_ABOVE_LIMIT` |
| `max_stake_per_utc_day_base_units` | `DAILY_STAKE_ABOVE_LIMIT` |
| `max_exposure_per_market_base_units` / `_per_event_` / `max_total_exposure_base_units` | `MARKET_` / `EVENT_EXPOSURE_ABOVE_LIMIT`, `EXPOSURE_ABOVE_LIMIT` |
| `max_daily_loss_base_units` (perdita realizzata del giorno UTC + caso peggiore di tutte le posizioni aperte + questo ordine) | `DAILY_LOSS_LIMIT` |
| `max_open_orders` | `OPEN_ORDERS_ABOVE_LIMIT` |
| `max_orders_per_hour`, `max_orders_per_utc_day` | `ORDERS_PER_HOUR_ABOVE_LIMIT`, `ORDERS_PER_DAY_ABOVE_LIMIT` |
| `min_seconds_between_orders` (0 = nessun intervallo) | `ORDER_PACE_TOO_FAST` |
| `allowed_sport_ids`, `allowed_league_ids`, `allowed_market_types` (`"ANY"` oppure lista; lista vuota = niente) | `SPORT_` / `LEAGUE_` / `MARKET_TYPE_NOT_ALLOWED`, `*_MISSING` |
| `min_decimal_odds` / `max_decimal_odds` (stringhe decimali, sul prezzo firmato) | `ODDS_OUTSIDE_OWNER_BAND` |
| `pregame_only` | `PREGAME_ONLY_EVENT_STARTED`, `GAME_TIME_MISSING` |
| `max_quote_age_seconds` (mercato, quota, saldo, metadati) | `*_STALE`, `*_FROM_FUTURE` |
| `command_max_age_seconds` (≤ 300) | `COMMAND_EXPIRED` |
| `market_time_in_force` (`FOK`/`IOC`), `limit_time_in_force` (`GTC`): default firmati dall'owner | `COMMAND_MISMATCH_TIME_IN_FORCE` |

Tutti i limiti interi tranne `min_seconds_between_orders` devono essere > 0 e la fascia di quote valida (1 < min ≤ max), altrimenti `EXPOSURE_LIMIT_ZERO`. **Rimossi (modifica intenzionale):** `session_loss_limit_base_units` (sostituito dalla perdita giornaliera che conta anche le posizioni aperte) e `slippage_tolerance_bps` (il comando dell'owner fissa il prezzo limite: nessuna tolleranza può peggiorarlo).

Il contesto di rischio è obbligatorio e ogni dato mancante produce un rifiuto (`RISK_CONTEXT_MISSING`, `MARKET_DATA_MISSING`, `QUOTE_MISSING`, `LIQUIDITY_MISSING`, `BALANCE_MISSING`, `FEES_MISSING_OR_MALFORMED`, `EXCHANGE_LIMITS_MISSING`, `EVENT_MARKETS_MISSING`). Contiene:
- la riga del mercato (`status` deve essere `ACTIVE`, altrimenti `MARKET_NOT_ACTIVE`) e tutti i mercati dell'evento (unicità del comando);
- la quota taker per il lato dell'ordine, con la liquidità al prezzo limite (per IOC/FOK deve coprire l'importo, altrimenti `INSUFFICIENT_LIQUIDITY`);
- il saldo disponibile, che deve coprire tutte le riserve non eseguite più l'ordine (`INSUFFICIENT_BALANCE`);
- le commissioni (null resta null);
- i metadati runtime `GET /metadata/obv3`: importo minimo (`STAKE_BELOW_EXCHANGE_MINIMUM`), importo minimo a riposo per i GTC (`STAKE_BELOW_MIN_RESTING_SIZE`), simbolo e decimali del token. **Modifica intenzionale:** prima il signer non conosceva il minimo e firmava; l'exchange rispondeva 400 con l'importo riservato fino alla scadenza.

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
- `SubmitOptions` (configurazione del deploy, mai argomento di un tool): `wait_for_outcome`, `max_wait_time_ms` (1..15000, inviato solo con `waitForOutcome`), `salt_format`. **Modifica intenzionale (PR #196 P2-3):** `post_only` non è più un'opzione globale: `postOnly` è inviato solo per gli ordini la cui riga `AUTHORIZE` registra un post-only comandato dall'owner (solo `GTC`; altrimenti `POST_ONLY_REQUIRES_GTC` in locale).
- **Maker e taker:** non esiste un endpoint di fill. Un ordine taker è lo stesso ordine firmato con `timeInForce` `IOC` o `FOK`, che esegue contro la liquidità a riposo al prezzo `percentageOdds` o migliore (developers/taking-liquidity); un ordine maker è `GTC` e resta sul book (con `postOnly` non prende mai). Un `GTC` che incrocia all'arrivo esegue subito e lascia a riposo solo il resto.
- Letture: `GET /orders-v3/{orderId}`, `GET /orders-v3/client/{clientOrderId}` (200 → `FOUND`, 404 → `NOT_FOUND`, altro → `ERROR`; id validati prima di costruire il path) e `GET /fills-v3?orderId=&perPage=100&nextKey=` con paginazione; la lista è `complete` solo all'ultima pagina, oltre il limite di pagine è `complete: false`.
- Heartbeat (`POST /heartbeat/v3`, `timeoutSeconds` 0..3600, un lapse cancella **tutti** gli ordini con `HEARTBEAT_TIMEOUT`): solo documentato, nessun sender di heartbeat. `HEARTBEAT_TIMEOUT` è già un `inactiveReason` riconosciuto (→ `CANCELLED`).

## Cancellazione (`signer/cancellation.py`)

Cancellare è un'operazione finanziaria (§2.6): **ogni** cancel richiede un comando esplicito dell'owner.
- **Modifica intenzionale:** la `CancelApproval` Ed25519 è sostituita dal comando dell'owner (`OwnerCommandAuthorization`, stesse regole degli ordini: id monouso, hash del testo, finestra breve). Il signer analizza il testo e il comando deve nominare esattamente questa operazione: scope, stesso insieme di id, stesso mercato o evento (`COMMAND_MISMATCH_SCOPE`, `_ORDER_IDS`, `_MARKET`, `_EVENT`, `_OPERATION`). `CancellationService(gateway, account_id=…, risk_limits=…)` non riceve più la chiave dell'owner; la finestra è `command_max_age_seconds` della configurazione di rischio firmata. Un cancel o cancel-all ripetuto richiede un nuovo token di riferimento.
- I cancel restano possibili a kill switch ingaggiato: riducono solo il rischio.
- Rotte: `DELETE /orders-v3` (id, ≤ `limits.maxCancelOrders`), `DELETE /orders-v3/market?marketHash=`, `DELETE /orders-v3/event?eventId=`, `DELETE /orders-v3/all`.
- Flusso: flag live riverificato → richiesta costruita → comando verificato (senza consumo) → **pre-check** con letture dell'exchange di ogni ordine coinvolto → **flag e finestra del comando ricontrollati** → riga `CANCEL_REQUEST` (fsync, consuma id e hash del testo) → flag e finestra ricontrollati di nuovo → invio → parsing → **nuova lettura** di ogni ordine coinvolto.
- **Ricontrollo dopo le letture (finding B):** il pre-check può fare fino a 3 letture per ordine. Subito prima del consumo si riverificano il flag (`LIVE_TRADING_FLAG_EXPIRED`, …) e la finestra (`COMMAND_EXPIRED`). Un rifiuto qui **non consuma** il comando e non invia nulla. Il controllo si ripete subito prima dell'invio: un rifiuto in quel punto non invia nulla, registra `CANCEL_NOT_SENT` nell'audit e lascia il comando consumato (fail closed: serve un nuovo comando).
- **Un cancel non libera mai esposizione da solo.** Il ledger cambia solo con evidenza dell'exchange: `INACTIVE` + `USER_REQUESTED` → `CANCELLED` (rilascia la parte non eseguita, il matched resta), fill → `PARTIALLY_FILLED`/`FILLED`. Il bucket `cancelled`, un comando accettato (`cancelledSubmitted`), `unconfirmed`, un timeout o una risposta ambigua lasciano l'ordine riservato: si rilegge più tardi con `reconcile`.
- Per id: ogni id deve stare in esattamente un bucket documentato (`cancelled`, `notCancelled` con `NOT_FOUND`/`CANCEL_PENDING`/`NOT_ACTIVE`, `unconfirmed`), altrimenti `UNKNOWN`. Un ordine che il pre-check trova già terminale (eseguito, cancellato, scaduto) non viene inviato (`ALREADY_TERMINAL`); se non resta nulla, non parte nessuna richiesta e il comando non viene consumato. Si invia al più l'insieme comandato, mai di più.
- Mercato/evento/tutti: confermano solo che il comando è stato accettato. Gli id in `cancelledSubmitted` che il ledger non conosce (ordini piazzati altrove) sono riportati in `foreign_order_ids` (input del kill switch: `RECONCILIATION_MISMATCH`).
- Un comando consumato resta consumato anche dopo un timeout e dopo il riavvio: un nuovo tentativo richiede un nuovo comando dell'owner.

## Worker di monitoraggio (`signer/monitor.py`)

Logica pura, senza rete: **non invia, non firma e non cancella mai**. Un `Monitor.tick()`:
- riconcilia, con tre letture iniettate (`get_order`, `get_order_by_client_id`, `get_fills`, esposte da `_ReadOnly`), gli ordini che possono ancora cambiare. Usa la stessa `evidence_from_exchange` e la stessa `apply_evidence` del gateway. **Modifica intenzionale (review PR #195, P3):** il monitor prende da sé il lock di invio del proprio ledger (`ledger.send_lock`, lo stesso di gateway e cancellazioni; PR #196 P2-4: nessun gateway né flag richiesto); i parametri `lock=` e `gateway=` non esistono. Rilettura dell'ordine, letture dell'exchange e applicazione avvengono tutte sotto quel lock;
- emette alert su scadenze, `EXPIRED_UNCONFIRMED`, `AUTHORIZED` mai firmati (`abandon_unsigned` resta un passo dell'operatore), `UNKNOWN` e reinvii possibili (il reinvio resta una decisione del gateway); gli alert `EVIDENCE_CONFLICT` alimentano il kill switch (`ReconciliationFindings.from_monitor_report`);
- valuta la policy di heartbeat (`HeartbeatPolicy`, `evaluate_heartbeat`). Non esiste un sender di heartbeat;
- valuta le regole con un valutatore e uno snapshot iniettati (per esempio `bridge.sxbet.rules.evaluate`, che `signer/` non importa). Restituisce solo proposte: una proposta "eseguibile" produce `MONITOR_PROPOSAL_CLAIMS_EXECUTABLE`. Senza picco salvato e senza mark-to-market intero il picco passa come `None`, mai 0; se il valutatore lo rifiuta l'esito è l'alert `RULES_EVALUATION_REFUSED`.
- Il report di un tick riporta `orders_submitted`, `orders_cancelled` e `network_writes` sempre a 0 **per costruzione**, etichettati `write_counters: CONSTANT_BY_CONSTRUCTION`; `exchange_reads` è misurato.

`MonitorState` è un file JSON scritto in modo atomico (fsync e rename), legato al `ledger_id`; si crea solo con `create`. Un file corrotto o di un altro ledger produce un errore (fail closed). All'apertura **ogni campo è controllato nel tipo** (chiavi esatte, righe del trailing `{peak: int, updated_at: int}`, ack int o null, proposte stringhe, tick ≥ 0). Con `mac_key` (≥ 32 byte, dalla configurazione dell'host del signer) il corpo è protetto da HMAC-SHA256 invece che da un semplice sha256: chi può scrivere il file ma non ha la chiave non può falsificare un picco, e un file con chiave non può essere declassato a uno senza (`MONITOR_STATE_MAC_*`). **Senza chiave** lo sha256 rileva solo corruzioni accidentali, non una manomissione deliberata di chi ha accesso in scrittura: rischio residuo documentato (il picco alimenta solo proposte, mai un ordine). Contiene il picco del trailing (monotono, salvato prima di ogni proposta), l'ultimo ack di heartbeat e le proposte già emesse. Lo stato degli ordini si ricostruisce sempre dal ledger.

## Correzioni trovate con il motore simulato (modifiche di comportamento intenzionali)

- **`FILLED` con 404 (riga purgata):** ora si porta a `SETTLED` con fill completi, tutti `SETTLED` e con somma uguale al matched, come già avveniva per `CANCELLED`/`EXPIRED`. Prima restava `FILLED` per sempre. Test: `tests/test_sxbet_orders_filled_purged_settlement.py`.
- **Evidenze di assenza ripetute:** `RECONCILED_ABSENT` (per tentativo d'invio) e `NOT_FOUND_AFTER_EXPIRY_NO_FILLS` hanno un id di evidenza legato al contesto. Prima il dedupe le scartava: un `SIGNED` mai inviato non arrivava mai a `EXPIRED_UNCONFIRMED`, e un secondo invio perso restava `UNKNOWN` per sempre. Test: `tests/test_sxbet_orders_absence_evidence.py`.

- **`FILLED → FILLED`:** ora è ammesso solo per registrare un fill tardivo, con matched strettamente crescente e non oltre lo stake (come `CANCELLED`/`EXPIRED`). Non libera mai nulla. Prima produceva `ILLEGAL_TRANSITION`: il fill non veniva registrato e l'ordine non andava mai a `SETTLED`. Test: `tests/test_sxbet_orders_filled_late_fill.py`.

`SimExchange` soddisfa per costruzione le stesse assunzioni `DOCS_UNVERIFIED` di questo modulo (`fillAmount` nelle unità dello stake con somma uguale a `totalBetSize − remainingSize`; `settleReturnAmount` al netto della fee). I test simulati non le validano: serve una prova su testnet.

Il motore end-to-end simulato è in `tests/sxbet_sim_exchange.py` (exchange in memoria, solo test) e `tests/test_sxbet_e2e_simulated*.py`; vedi `docs/sxbet/mcp_chat_trading_workflow.md` §9.

## Custodia della chiave

L'audit (`signer/audit.py`) ammette in più i campi `command_id` (id del comando dell'exchange), `scope` e `bucket` (cancellazioni, kill switch), `owner_command_id` e `owner_command_sha256` (id monouso e hash del comando dell'owner); mai chiave, firma o testo del comando.

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
- Formato delle etichette degli esiti per mercati con linea (`outcomeOneName` = "Over 2.5", "Lakers -4.5"?) e segno di `line` per gli handicap (riferito all'esito uno?). Il signer confronta l'etichetta esatta e solo il valore assoluto della linea; se le etichette non contengono la linea il comando viene rifiutato (fail closed). Da verificare su testnet per ogni sport/tipo di mercato.
- Presenza di `teamOneName`/`teamTwoName`, `sportId`, `leagueId`, `type`, `line` e `gameTime` nelle righe di `GET /markets/find` e `GET /markets/active?eventId=` per tutti gli sport (una riga senza nomi delle squadre si può comandare solo con `evento <id>`).
- Se la quota minima del comando vada intesa lorda o netta delle commissioni (decisione D3): oggi è la quota lorda dell'ordine firmato.

## Limiti noti di questa versione

- Nessun `RestSender` reale (rete, autenticazione, rate limit tramite il coordinatore condiviso, timeout): PR separata, owner. Il kill switch registra intenti di cancel ma non li invia.
- Il provider di letture del signer (`signer/market_data.py`) è solo un'interfaccia: l'implementazione di rete arriva con la PR del sender. Fino ad allora un signer senza provider configurato rifiuta ogni ordine (`SIGNER_MARKET_READS_UNAVAILABLE`); i test usano un provider in memoria che fa la parte dell'exchange.
- `received_at` del comando è dichiarato dal MCP (fiducia documentata): un MCP compromesso può dichiarare un comando "fresco". Il danno resta limitato da id monouso, impronta, limiti e kill switch; la marcatura dell'istante al confine del signer arriverà con il servizio signer (canale HMAC, passo 6).
- Guardia "quasi duplicato" (`DUPLICATE_SUSPECTED`, stesso mercato/esito/importo/quota entro N minuti con comandi diversi) del documento di progetto: non implementata; oggi limitano il caso `min_seconds_between_orders`, i limiti per ora/giorno e il rifiuto dello stesso testo.
- La riconciliazione dopo un cancel è sincrona e singola: un ordine che l'exchange toglie dal book più tardi resta riservato fino alla prossima `reconcile` (nessun polling né canale realtime nel signer).
- Ancoraggio: lo scrittore stantio può lasciare una riga locale non ancorata prima di essere respinto (rilevata, mai accettata senza una `LedgerHeadAck` firmata dall'owner). Lo store PostgreSQL deve essere un database dell'owner separato da quello del bridge MCP se si vuole che chi controlla il bridge non possa toccare l'ancora.

## Cosa manca prima di `LIVE_TRADING` (decisioni owner)

Vedi la checklist in [`docs/sxbet/wallet_and_signing.md`](../docs/sxbet/wallet_and_signing.md#checklist-di-attivazione-tutte-decisioni-dellowner).
