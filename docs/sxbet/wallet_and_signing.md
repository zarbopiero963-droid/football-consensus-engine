# SX Bet: wallet Magic, proxy, firma EIP-712 e percorso verso il trading reale

Stato: **progettazione verificata sulla documentazione ufficiale** (01-11-2026). Il trading reale resta **non disponibile** (governance §2.6).

## Cosa dice la documentazione ufficiale

| Tema | Fatto documentato | Fonte |
|---|---|---|
| Account | "An account is two addresses. You **sign** with one and you are **funded** at the other." L'EOA è l'identità verso l'API; il proxy è il contratto dove stanno i fondi e dove si regolano le scommesse | `developers/accounts` |
| Magic | Con email o Google viene creato un wallet Magic **non-custodial**, legato all'email. La private key si può recuperare dalla pagina assets o account | `developers/accounts`, `user-guides/faq/export-wallet-metamask` |
| Chi firma gli ordini | La private key dell'EOA "signs every order"; il campo `maker` dell'ordine è l'indirizzo dell'EOA | `developers/accounts`, `api-reference/eip712-order-signing` |
| Formato della firma | EIP-712, primary type `Order` con 8 campi (`marketHash`, `baseToken`, `totalBetSize`, `percentageOdds`, `salt`, `expiry`, `maker`, `isMakerBettingOutcomeOne`). Il dominio si legge da `GET /metadata/obv3` (`OBv3 Escrow` v1, chain 4162). L'order id è il digest EIP-712 | `api-reference/eip712-order-signing`, `developers/posting-orders` |
| Firma senza esportare la chiave | "Any injected provider that exposes `eth_signTypedData_v4` (e.g. MetaMask) works" | `api-reference/eip712-order-signing` |
| Proxy | Il trading avviene solo dal proxy; si crea con `POST /user/deploy-proxy` o al primo login. Senza proxy, `POST /orders-v3` risponde `400 PROXY_NOT_DEPLOYED` | `developers/accounts`, `developers/posting-orders` |
| Safe | Ogni proxy ha una Safe (`multisigSafeAddress`) che autorizza prelievi, trasferimenti e cambi di firmatario. Gli ordini **non** passano dalla Safe | `developers/accounts` |
| Capitale | Pubblicare un ordine non blocca capitale: i fondi vanno in escrow solo al match | `developers/posting-orders` |
| Anti-replay | `salt` casuale a 256 bit ed `expiry` firmati; `clientOrderId` unico per indirizzo | `developers/posting-orders`, `developers/client-order-id` |
| Cancellazione | `DELETE /orders-v3*` richiede solo la API key con scope `Orders: cancel`, nessuna firma | `developers/api-key-permissions` |

**Non documentato (`DOCS_UNVERIFIED`):**
- se il wallet Magic di SX Bet si può usare come provider `eth_signTypedData_v4` fuori dal sito sx.bet, cioè con l'SDK Magic e la chiave pubblicabile di SX Bet;
- cosa succede se lo stesso ordine o `clientOrderId` viene reinviato dopo un timeout.

## Opzioni di firma, con i rischi

1. **Firma nel browser dell'owner con un provider iniettato (consigliata).**
   - Come funziona: il server prepara l'ordine esatto, l'owner lo vede e lo firma nel proprio wallet (MetaMask, Rabby o un hardware wallet tramite questi) con `eth_signTypedData_v4`, e la firma torna al server.
   - Vantaggi:
     - la chiave non esce mai dal wallet;
     - ogni ordine richiede un gesto dell'owner fuori dalla chat, cioè la conferma fuori banda del §2.6;
     - "what you see is what gets signed": il wallet mostra i campi EIP-712.
   - Per un account Magic: la via documentata è importare la chiave Magic in MetaMask (`export-wallet-metamask`). L'operazione la fa l'owner sul proprio dispositivo; l'agente non la chiede e non la vede mai.
2. **Signer isolato con private key** (`signer/`, servizio separato: non si importa nel processo MCP né su Render).
   - La chiave sta solo nel processo del signer, che accetta un intent strutturato e lo firma solo con un'approvazione Ed25519 dell'owner legata al binding v2 dell'ordine (account, maker, evento, mercato, selezione, lato, importo, quota limite, commissioni attese, esposizione massima, scadenza, `clientOrderId`, rete/chainId/contratto).
   - Altre garanzie: approvazione monouso con scadenza ≤120 s che un rifiuto non consuma, risk engine con limiti firmati dall'owner (default zero), macchina a stati degli ordini con ledger append-only, idempotenza e audit append-only.
   - Il rischio residuo è custodire la chiave in un servizio sempre acceso. Richiede un dispositivo o un'app di approvazione dell'owner, ancora da realizzare.
3. **Chiave nel server MCP, in env Render o GitHub: vietato.** Il modello o la CI potrebbero arrivare a firmare (§2.2, §2.6).

## Architettura del percorso (stato attuale)

```
MCP preview (sxbet_simulate_order, bridge, nessuna chiave)
   → risk check (signer/risk.py, limiti firmati dall'owner, dati freschi)
   → approvazione dell'owner (Ed25519, binding v2, monouso, ≤120 s, fuori banda)
   → firma del wallet (signer/core.py, EIP-712; ledger AUTHORIZED → SIGNED)
   → gateway (signer/gateway.py, DISABILITATO; nessun trasporto di rete nel repository)
        └ adapter ufficiale (signer/rest_adapter.py: builder di POST /orders-v3 + parser, RestSender astratto)
   → riconciliazione (GET /orders-v3/{orderId}, /orders-v3/client/{clientOrderId}, /fills-v3)
   → cancellazione (signer/cancellation.py: approvazione dell'owner per ogni cancel, rilascio solo da letture)
   ledger ancorato fuori dall'host (signer/anchor.py + migration 0008, opzionale) con lease e fencing token
```

- **Stati dell'ordine:** `PREPARED`, `AUTHORIZED`, `SIGNED`, `SUBMITTED`, `OPEN`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `EXPIRED`, `EXPIRED_UNCONFIRMED`, `SETTLED`, `REJECTED`, `UNKNOWN`. Le transizioni ammesse e l'esposizione per stato sono in [`signer/README.md`](../../signer/README.md#stati-degli-ordini-ed-esposizione-signerorderspy).
- **L'esposizione scende solo con evidenza dell'exchange:** un cancel o una scadenza confermati, oppure un settlement con fill completi. `UNKNOWN` (timeout, risposta ambigua, crash durante l'invio) resta riservato all'intero importo finché non è riconciliato.
  - Un 404 dopo la scadenza non è una prova: porta a `EXPIRED_UNCONFIRMED`, che resta riservato fino a una riga `INACTIVE` dell'exchange o a una seconda osservazione identica dopo 24 h. Un fill tardivo viene sempre registrato e contato, anche dopo `EXPIRED` o `CANCELLED`.
  - Una volta uscita la firma, un rifiuto (400, riga `FAILED`, `SIGNED → REJECTED` manuale) non libera l'importo prima della scadenza dell'ordine (più 30 s): l'ordine firmato resta pubblicabile da chi lo possiede.
  - Se l'exchange mostra poi che un ordine `REJECTED` (con firma uscita) è stato eseguito o è a riposo, l'evidenza lo porta a `PARTIALLY_FILLED`/`FILLED`/`OPEN` con l'esposizione registrata: nessun evento finanziario si perde (audit `EVIDENCE_REJECTED_ORDER_EXECUTED`).
- **Ledger a scrittore unico:** in modalità locale `flock` esclusivo per tutta la vita dell'istanza (`LEDGER_LOCKED` per una seconda istanza, anche in un altro processo), controllo della testa del file prima di ogni append (`LEDGER_HEAD_MISMATCH`) e istanza avvelenata dopo un append fallito (`LEDGER_POISONED` fino a riavvio e replay).
- **Ancoraggio esterno (opzionale, `PostgresAnchorStore`):** dopo ogni append fsync la testa (seq, hash, fencing token) viene scritta in una tabella append-only; all'avvio il signer rifiuta un file più corto (troncamento, rollback), diverso o più lungo dell'ancora, e uno store irraggiungibile. Il lease PostgreSQL (advisory lock di sessione + fencing token) sostituisce `flock`, quindi il ledger non dipende più da un filesystem locale; uno scrittore stantio viene respinto (`LEDGER_FENCED`). Dettagli in [`signer/README.md`](../../signer/README.md#ancoraggio-esterno-della-testa-e-lease-di-scrittura-signeranchorpy-h1h2).
- **Cancellazione:** quattro rotte (`DELETE /orders-v3` per id fino a `maxCancelOrders`, `/market`, `/event`, `/all`), ciascuna con un'approvazione Ed25519 dell'owner legata a scope e bersaglio, monouso e con TTL ≤ 120 s. Tutte rispondono 200 anche se non hanno cancellato nulla: un cancel non libera mai esposizione da solo, solo una lettura che mostra `INACTIVE`/`USER_REQUESTED` (o i fill) cambia il ledger.
- **Limiti** (tutti a zero per default, quindi rifiuto):
  - importo massimo per ordine;
  - esposizione per mercato, per evento e totale;
  - perdita massima per sessione e per giorno UTC;
  - numero massimo di ordini aperti;
  - slippage in bps;
  - quota minima e massima;
  - età massima dei dati di mercato, quota e saldo.
- **Rifiuto in assenza di dati:** se saldo, liquidità, stato del mercato, commissioni o freschezza dei dati mancano o non sono verificabili, il signer rifiuta.

## Checklist di attivazione (tutte decisioni dell'owner)

Finché un solo punto è aperto, `LIVE_TRADING` resta non attivabile (§2.6).

1. [ ] Scelta del metodo di firma: opzione 1 (browser + wallet) oppure opzione 2 (signer isolato + dispositivo di approvazione).
2. [ ] **Dispositivo o app di approvazione dell'owner:**
   - mostra il binding v2 leggibile e firma `approval_message`;
   - la chiave Ed25519 resta solo lì.
3. [ ] **Deploy del signer come servizio separato:**
   - fuori dal servizio Render MCP: `RENDER` non deve essere impostata e `SXBET_SIGNER_PROCESS=1` sì;
   - con il proprio segreto, fuori da GitHub secrets;
   - con un canale autenticato verso il chiamante;
   - ledger e audit su storage durevole, con ancoraggio esterno dell'hash di testa (la troncatura della coda non è rilevabile dalla sola hash chain). Il codice esiste (`PostgresAnchorStore`, migration 0008): resta da scegliere il database dell'owner che lo ospita, separato da quello del bridge MCP, e applicarvi la migration.
4. [ ] **Config di rischio** (`SXBET_SIGNER_RISK_CONFIG`): valori reali scelti e firmati dall'owner.
5. [ ] **Provenienza dei dati di rischio:** oggi il contesto (mercato, quota, saldo, commissioni) è fornito dal chiamante, e il signer ne verifica solo freschezza e coerenza. Serve una fonte autenticata, per esempio letture del signer stesso o snapshot firmati.
6. [ ] **Sender reale per l'adapter** (`RestSender` per `POST /orders-v3`, `DELETE /orders-v3*` e le letture di riconciliazione). Builder e parser del contratto ufficiale esistono (`signer/rest_adapter.py`, `signer/cancellation.py`); manca solo l'I/O:
   - PR separata, revisionata e mergiata dall'owner, con rate limit tramite il coordinatore condiviso e timeout;
   - test su testnet, incluso il formato di `salt` (`DOCS_UNVERIFIED`; default dell'adapter: stringa esadecimale di 32 byte dello schema OpenAPI, decimale come opzione);
   - verifica del comportamento sul reinvio dopo un timeout (`DOCS_UNVERIFIED`);
   - decisione su `waitForOutcome`/`maxWaitTime` e `postOnly` (configurazione dell'adapter, oggi spenti).
7. [ ] **Flag `LIVE_TRADING` firmato dall'owner** (`load_live_trading_flag`): con scadenza breve e legato a `signer_id` e rete. L'agente non lo crea mai. Il gateway riverifica la firma dell'owner e la scadenza a ogni invio (`LIVE_TRADING_FLAG_EXPIRED`); la chiave pubblica dell'owner gli arriva dalla propria configurazione.
8. [ ] **Cancellazione** (`DELETE /orders-v3*`, scope `Orders: cancel`): il flusso esiste (`signer/cancellation.py`) con approvazione dell'owner per ogni cancel e rilascio solo da letture dell'exchange. Mancano il sender reale (punto 6) e, sul dispositivo di approvazione (punto 2), la schermata che mostra e firma il binding di cancel (`sxbet.cancel_approval.v1`). Un heartbeat (`POST /heartbeat/v3`) è solo documentato: armarlo è una decisione dell'owner, perché un lapse cancella tutti gli ordini.
9. [ ] **Proxy wallet e fondi:** verificare che il proxy sia deployato (`sxbet_proxy_wallet` → `deployed`) e che ci sia USDC sul proxy. Deploy e depositi sono operazioni finanziarie con conferme proprie.
10. [ ] **API key con `Orders: create`/`Orders: cancel`:** si concede solo con la decisione di attivare il trading.
11. [ ] Verifica di idoneità del servizio nella giurisdizione applicabile.

Il MCP oggi espone **solo letture** (36 tool) e la **simulazione** (1 tool, 37 `sxbet_*` in totale). Nessun tool può firmare o inviare, e nessun tool nuovo è stato aggiunto con il signer, il risk engine o il gateway.
