# SX Bet — esecuzione reale su comando esplicito dell'owner (progetto)

Stato: **PROGETTO, NON IMPLEMENTATO.** `LIVE_TRADING` resta OFF/UNAVAILABLE. Nessun ordine reale.
Base: `main` = `e155193`. Governance di riferimento: CLAUDE.md/AGENTS.md §2.6 come modificato dalla PR che introduce questo documento.

Flusso richiesto dall'owner: *«Piazza 10 USDC su Over 2,5 in Cape Verde–Liberia a quota minima 1,90»* → MCP verifica → signer firma → SX Bet esegue → MCP restituisce l'esito. Nessuna strategia genera scommesse; nessuna passkey; nessuna seconda conferma per un comando esplicito e completo.

## 1. Cosa esiste già

Su `main`: `signer/risk.py` (motore di rischio), `signer/orders.py` (ledger append-only), `signer/gateway.py`, `signer/rest_adapter.py`, `signer/cancellation.py`, `signer/anchor.py`, `signer/eip712.py`.
Prototipi non mergiati (worktree locali): servizio signer con canale HMAC MCP→signer (senza più la parte passkey), exchange simulato + monitor, adattatore KMS, profilo testnet Toronto.
Mancano: sender REST reale, letture di mercato fatte dal signer stesso, autorizzazione "comando owner" al posto della conferma per ordine, configurazione di rischio v2, client e tool lato MCP, gating delle modalità.

## 2. Minacce e controlli (senza conferma per ordine)

**Limite di fondo, detto onestamente:** senza un fattore fuori banda il signer non può distinguere crittograficamente un comando scritto dall'owner da uno iniettato (prompt injection da pagine web, dati dei provider, output di altri tool) o generato per errore dal modello. I controlli sotto **limitano il danno**, non lo eliminano. È il rischio residuo che l'owner accetta con la modifica di governance.

| Minaccia | Controlli |
|---|---|
| T1 prompt injection / ordine non richiesto | **Limiti nel signer** firmati dall'owner (importo per ordine e al giorno, esposizione per mercato/evento/totale, perdita giornaliera incluse le esposizioni aperte, ordini per ora/giorno, intervallo minimo, sport/leghe/tipi di mercato ammessi, fascia di quote, solo pre-match opzionale); argomento obbligatorio `owner_command` con il testo letterale, analizzato con una grammatica deterministica (IT/EN) e confrontato campo per campo con i parametri (`COMMAND_MISMATCH_<CAMPO>`), ri-verificato dal signer e salvato come hash; connettore di trading separato (`/mcp-trade`) senza tool browser; scope OAuth `sxbet:trade`; tool marcati `destructiveHint` (la richiesta di approvazione del client è UI del client, non un controllo del signer); kill switch; eco post-ordine del comando e dell'ordine esatto |
| T2 replay | HMAC con finestra ±30 s e nonce persistente; request id consumato nel ledger; salt casuale; expiry breve; `clientOrderId` unico |
| T3 manomissione MCP→signer | HMAC su metodo, path, hash del body, timestamp, nonce; il MCP invia solo i campi minimi; il signer ricava dominio, maker, arrotondamento quote, salt, expiry e mapping esito dalle proprie letture; la firma non torna mai al MCP |
| T4 quota obsoleta | limite di prezzo dentro l'ordine firmato (arrotondato per difetto sulla scala, mai peggiore del richiesto); il signer rilegge book e mercato con età massima; expiry oltre il betting delay |
| T5 doppio invio | chiave di idempotenza obbligatoria; guardia "quasi duplicato" (stesso mercato/esito/importo/quota entro N minuti con chiave diversa → `DUPLICATE_SUSPECTED`, superabile solo con comando esplicito "di nuovo"); timeout → `UNKNOWN` e riconciliazione, mai retry cieco |
| T6 MCP/OAuth compromesso | danno limitato ai limiti del signer; EOA dedicato con saldo ridotto; kill switch azionabile fuori dal MCP; rilascio solo con atto firmato |
| T7 host del signer compromesso | KMS (opzionale), EOA dedicato, saldo ridotto sul proxy |

## 3. Architettura

```
ChatGPT / Claude.ai
  → MCP bridge (Render, OAuth; tool di trading con scope sxbet:trade; nessuna chiave, nessuna libreria di firma)
     risolve il mercato in modo esatto (§2.3bis), analizza owner_command, valida
  → HMAC-SHA256 su rete privata
  → signer (servizio separato): kill switch → flag LIVE firmato dall'owner → ri-analisi del comando
     → letture proprie (mercato, book, saldo, commissioni, ordini e posizioni aperti, anche da web)
     → rischio v2 dentro il lock del ledger → AUTHORIZE → firma EIP-712 (env o KMS)
     → SIGNED → UNKNOWN scritto prima dell'invio → POST /orders-v3 (rate coordinator) → riconciliazione
     ledger JSONL su disco persistente + anchor PostgreSQL; monitor di riconciliazione
  → SX Bet (prima testnet Toronto; mainnet solo dopo certificazione e GO)
```

Hosting del signer (decisione owner): servizio privato Render (circa 7,25 $/mese con disco: **nuovo costo**; richiede anche di adattare la guardia in `signer/__init__.py`, che oggi rifiuta l'import quando `RENDER` è impostata), oppure signer "pull" su hardware dell'owner (0 $, disponibilità legata alla macchina). Un web service Free non è adatto (ingresso pubblico, spin-down, filesystem effimero).

## 4. Tool MCP previsti

Tutti con `destructiveHint=true`, scope `sxbet:trade`, esposti solo sul connettore di trading; attivi in `SIMULATION` (senza firma né chiave) e in `LIVE_TRADING`.

- `sxbet_resolve_market` (lettura): evento + mercato + linea + esito → `market_hash` solo con abbinamento `EXACT`; altrimenti `NO_MATCH`/`AMBIGUOUS`.
- `sxbet_place_order`: `owner_command`, `market_hash`, `outcome_name`, `order_type` `MARKET` (FOK o IOC, decisione D3) o `LIMIT` (GTC), `stake_usdc`, `min_decimal_odds`, `idempotency_key` → `FILLED`/`PARTIALLY_FILLED`/`OPEN`/`NOT_FILLED`/`REJECTED`/`UNKNOWN_RECONCILING`, fill, quota media (DERIVED), commissioni, esposizione e limiti residui.
- `sxbet_cancel_order`, `sxbet_cancel_all`, `sxbet_close_position` (cashout, solo se riduce l'esposizione), `sxbet_hedge_order`, `sxbet_execution_status`, `sxbet_reconcile`, `sxbet_trading_status`, `sxbet_kill_switch_engage` (solo attivazione; il rilascio richiede un file firmato dall'owner sul signer).

In `SIMULATION` `sxbet_place_order` fa risoluzione, analisi e valutazione del rischio senza firmare e senza usare la chiave.

## 5. Riconciliazione

Timeout lato MCP → `UNKNOWN_RECONCILING` con lo stesso `client_request_id`; nessun nuovo tentativo con una chiave nuova. Nel signer: `UNKNOWN` scritto prima dell'invio; reinvio solo dello stesso ordine firmato dopo evidenza di non esistenza (404 su entrambe le ricerche, nessun fill, ≥ 60 s). Al riavvio: lease dell'anchor, replay del ledger con hash chain, ricostruzione di contatori ed esposizione dal ledger, riconciliazione di ogni ordine non terminale prima di accettarne di nuovi.

## 6. Test e certificazione

Offline: exchange simulato, iniezione di crash tra ogni riga del ledger, rischio v2 per ogni limite, grammatica dei comandi (corpus valido, discrepanze, corpus di injection), conversione quote, gating di modalità e scope, isolamento (il bridge non importa mai `signer`).
Testnet Toronto (account, chiave e USDC di test forniti dall'owner): ordini minimi GTC/IOC/FOK, cancellazioni, fill parziali, timeout forzato e riconciliazione, riavvio a metà, kill switch, ogni limite, commissioni al settlement, end-to-end dalla chat.
Certificazione prima del GO: suite verdi, checklist testnet con evidenze, almeno 7 giorni di testnet senza discrepanze ledger/exchange, runbook, review di sicurezza indipendente, verifica legale chiusa. Poi l'owner firma i limiti mainnet e il flag LIVE a scadenza breve.

## 7. Sequenza di PR (una aperta alla volta; merge dell'owner per codice di trading/firma, limiti, modalità, flusso di autorizzazione)

1. Governance §2.6 + questo documento.
2. Exchange simulato e monitor.
3. Profilo testnet Toronto.
4. Rischio v2 (limiti giornalieri, ritmo, allowlist, fascia di quote, perdita giornaliera; contatori dal ledger).
5. Autorizzazione "comando owner" nel signer, grammatica, guardia duplicati, kill switch.
6. Servizio signer senza passkey (HMAC, scope, ledger persistente e anchor, dry-run, stato).
7. Letture di mercato dal signer e sender REST reale, solo Toronto.
8. Bridge: client HMAC, `SXBET_MODE`, scope `sxbet:trade`, connettore `/mcp-trade`, tool di esecuzione, `sxbet_resolve_market`.
9. Monitor, riconciliazione all'avvio, heartbeat, cashout/hedge eseguibili.
10. Deploy del signer (nuovo costo, segreti, adattamento della guardia `RENDER` in `signer/__init__.py`: decisione owner).
11. Evidenze di certificazione testnet.
12. (Opzionale) KMS.
13. Abilitazione mainnet dopo certificazione, verifica legale e GO.

## 8. Decisioni dell'owner

- **D1 Limiti** (testnet e mainnet separati). Proposta prudente per il primo mainnet: 1–5 USDC per ordine, 20 USDC al giorno, 50 USDC di esposizione aperta, 20 USDC di perdita giornaliera, max 3 ordini aperti, max 10 ordini al giorno, 30 s tra ordini, quote 1,20–10,0, solo calcio, mercati 1X2/Over-Under/Handicap.
- **D2** L'evento deve comparire nel comando (consigliato, richiesto dal §2.3bis) o può venire dal contesto della chat?
- **D3** «a quota minima X» = FOK (tutto o niente, consigliato) o IOC (parziale ammesso)? Quota minima lorda o netta di commissioni?
- **D4** Hosting del signer (servizio privato Render ~7,25 $/mese, hardware dell'owner, VPS) e KMS sì/no.
- **D5** Chiave: EOA dedicato con saldo ridotto (consigliato) o chiave Magic esportata.
- **D6** Chiave API con permessi di ordine/cancellazione solo nel signer (consigliato) o anche nel MCP.
- **D7** Kill switch: solo blocco o blocco + cancellazione di tutto; heartbeat per gli ordini GTC.
- **D8** Impostazione di approvazione dei tool nel client (chiedi / consenti sempre): non è un controllo del signer.
- **D9** Connettore di trading separato e client OAuth dedicato.
- **D10** Testnet: account, proxy, USDC di test, chiave API e chiave di firma di test.
- **D11** Criteri di certificazione, verifica legale e GO finale.
