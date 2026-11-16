# S10 — Studio della custodia: EOA, proxy, Safe, chiave API (issue #187)

Stato: **STUDIO**. Nessuna modifica a Safe o wallet, nessun fondo mosso, nessuna chiamata autenticata. Un verdetto SAFE della review riguarda l'accuratezza dello studio: **non** certifica che un Safe 2-di-2 funzioni su SX Bet. La custodia della chiave API (decisione owner `owner_commanded_execution.md` D6, distinta dalla D6 della tabella §3) resta aperta, come la scelta della chiave (`owner_commanded_execution.md` D5).

Fonte: documentazione ufficiale https://docs.sx.bet, letta il 16-11-2026: `llms-full.txt`, `developers/accounts`, `developers/funding`, `developers/migrate-to-v3`, `developers/new-in-v3`, `developers/api-key-permissions`, `api-reference/auth-matrix`, `api-reference/api-key`, `user-guides/deposit-withdraw/large-withdrawals`, `get-user-proxy`, `post-user-transfer-to-proxy`, `get-user-multisig-info` e tutte le pagine `post-user-multisig-*`. Ciò che la documentazione non prova è marcato `DOCS_UNVERIFIED` e va provato su Toronto.

## 0. Sintesi

1. **Oggi (mainnet, Safe 1 owner / soglia 1) un EOA compromesso controlla tutto da solo**: ordini, cancellazioni, prelievo verso qualunque indirizzo, trasferimento ad altri proxy, aggiunta di firmatari e cambio della soglia (§2).
2. **Non è dimostrato che un Safe 2-di-2 sia utilizzabile tramite l'API** (`DOCS_UNVERIFIED`, §3 D1). La documentazione dà indizi in entrambe le direzioni: prevede firmatari multipli ("you can add multiple signers to your wallet to approve withdrawals") e una firma "di chi deve ancora firmare", ma ogni `*/execute` richiede che `signerAddress` sia l'indirizzo autenticato e restituisce 403 se "The Safe is not on your account". Ipotesi da provare: un secondo firmatario con un indirizzo proprio potrebbe non riuscire a inviare la sua firma. Decide la prova T0.
3. Anche se fosse realizzabile, il Safe **non** protegge dagli ordini dannosi firmati dall'EOA, né dalle cancellazioni e dal blocco dei prelievi con ordini aperti (§2).
4. Il limite economico che regge anche con la chiave rubata è uno solo: **il saldo del proxy di trading più il valore delle posizioni aperte che vi si regolano**. Il capitale non deve mai sostare nell'EOA di trading (§5).

## 1. Il modello documentato

| Elemento | Ruolo (citazione ufficiale) | Fonte |
|---|---|---|
| **EOA** | "The wallet you control with a private key. Your identity to the API". Firma ogni ordine: "`maker` (your EOA, never the proxy)"; "You still sign with your EOA private key" | developers/accounts; developers/migrate-to-v3 |
| **Proxy** | "A contract your EOA owns. Where your money lives and where bets settle". "**Trading is proxy-only.** Funds must be in your proxy wallet to trade." | developers/accounts |
| **Safe** | "Every deployed proxy has a Safe smart account automatically attached … The Safe authorises withdrawal, transfers to another proxy, and signer changes". `SafeInfo`: "A new Safe starts at 1" | developers/accounts; get-user-multisig-info |
| **Chiave API** | "Every response is scoped to the address that owns the key". "Only one key can be active at a time"; "Any previous keys used will be unauthorized if you generate a new key". Scope separati: Orders create/cancel, Wallet withdraw, Wallet transfer, Wallet admin | api-reference/api-key; developers/api-key-permissions |
| Gestione del Safe | `add-signer`, `remove-signer`, `set-threshold`, `withdraw`, `transfer`: `prepare` → firma `typedData` → `execute`; l'exchange esegue ("isReadyToExecute … The exchange then submits it"). `cancel` è una chiamata unica, firmata dal firmatario che ha proposto | post-user-multisig-* |
| Transazioni pendenti | "A Safe holds at most one pending transaction"; "A later prepare at this nonce replaces a transaction already pending there" | get-user-multisig-info, *-prepare |
| Finanziamento | Permit EIP-2612 con `spender` = `transferToProxyExecutorAddress` ("not your proxy"). `proxyAddress` facoltativo, "checked only for being deployed and having an owner — not for being yours". Nessuna approval ERC-20 in V3 | developers/funding; post-user-transfer-to-proxy; developers/new-in-v3 |
| Blocco prelievi | `withdraw`/`transfer` rifiutati con `HAS_OPEN_ORDERS` "while you have a `PENDING` or `ACTIVE` order" | post-user-multisig-withdraw-*, transfer-* |
| Autorizzazioni | `POST /orders-v3`: API key + ordine EIP-712. `DELETE /orders-v3`, `/all`, `/event`, `/market`: **solo API key**. `POST /heartbeat/v3`: scope "Orders: cancel". Nessuna firma nei corpi delle richieste | api-reference/auth-matrix; developers/api-key-permissions; OpenAPI |
| Wallet Magic | "If you signed up with email or Google, you can retrieve your private key from the assets page" | developers/accounts |

Stato riportato dall'owner per il conto mainnet (audit in sola lettura del 16-11): Safe con **1 owner e soglia 1**, coerente con "A new Safe starts at 1". Il conto mainnet è un wallet Magic (vedi `magic_wallet_existing_account_audit.md`).

## 2. Modello delle minacce

| Credenziale compromessa → azione | Oggi (Safe 1-di-1) | Con Safe 2-di-2 (se realizzabile) |
|---|---|---|
| **Solo chiave API**: cancellare ordini, impostare il heartbeat, leggere | possibile | possibile: non è coperto dal Safe |
| **Solo chiave API**: piazzare ordini | no: serve la firma EIP-712 dell'EOA | no |
| **EOA**: ordini dannosi (quote pessime contro una controparte complice) che drenano il proxy | possibile | **possibile**: il Safe non interviene sugli ordini |
| **EOA**: generare una nuova chiave API da sx.bet (la nostra viene revocata, gli scope li sceglie l'attaccante) | possibile | possibile |
| **EOA**: `withdraw` con `to`/`amount` arbitrari, anche via bridge (`glideSessionId`) | **possibile** (soglia 1) | serve la seconda firma, se la co-firma funziona e l'EOA non ha altre vie (`DOCS_UNVERIFIED`, D1, D2, D4) |
| **EOA**: `transfer` verso un altro proxy | **possibile** | la soglia lo copre? `DOCS_UNVERIFIED` (D3) |
| **EOA**: transazione Safe `approveAsset` (allowance verso terzi) | possibile? (`DOCS_UNVERIFIED`, D6) | la soglia lo copre? `DOCS_UNVERIFIED` |
| **EOA**: `add-signer` / `set-threshold` | **possibile**: può prendere il Safe | serve la soglia corrente? `DOCS_UNVERIFIED` (D3) |
| **EOA**: `HAS_OPEN_ORDERS` come ostacolo | nessun ostacolo: cancella prima tutto con la chiave API | può **bloccare il recupero** tenendo ordini aperti |
| **EOA**: prelievo dalla UI ("Transfer funds from your betting wallet into your EOA on the Account Page") | possibile | passa dal Safe? `DOCS_UNVERIFIED` (D4) |
| **Account email/Google** del wallet Magic: esportazione della chiave privata | possibile: aggira del tutto il signer | idem |

**Conseguenza.** Nella configurazione mainnet attuale **non esiste alcuna protezione contro la compromissione dell'EOA**. Il Safe 2-di-2, anche se realizzabile, è al massimo una protezione dei prelievi, non del capitale esposto al trading. Le protezioni reali sono:
1. la chiave di trading custodita **solo** nel signer isolato, mai nel MCP né nel modello (§2.6); raccomandato, in attesa della decisione owner `owner_commanded_execution.md` D5: un EOA dedicato generato nel signer, non una chiave Magic/email esportata;
2. i limiti firmati dall'owner e il kill switch del signer, che valgono finché la chiave resta nel signer;
3. **nel proxy di trading solo il capitale che si accetta di perdere**: è l'unico limite che regge anche con la chiave rubata.

## 3. Domande aperte (`DOCS_UNVERIFIED`)

| # | Domanda | Indizio della documentazione |
|---|---|---|
| D1 | **Un secondo firmatario può co-firmare?** | A favore: developers/new-in-v3 ("you can add multiple signers to your wallet to approve withdrawals"); get-user-multisig-info (`typedData`: "A signer who still needs to sign signs this"; `confirmedBy`, `confirmationsRequired`, `isExecutable`). Contro: ogni `*/execute` richiede "`signerAddress` … Must be the authenticated address" e restituisce 403 se "The Safe is not on your account"; non è documentato come un secondo indirizzo si autentichi sul Safe di un altro conto. Esecuzione on-chain diretta e co-firma da UI non documentate. **Esito incerto**: decide T0 |
| D2 | L'EOA può chiamare direttamente il proxy e muovere fondi senza il Safe? | "A contract your EOA owns": non escluso |
| D3 | La soglia copre anche `transfer`, `add-signer`, `set-threshold`? | Indizi in entrambe le direzioni. Solo prelievo: `remove-signer/prepare`, "`threshold` is the number of signatures required for a withdrawal". Più ampio: developers/accounts, "The Safe authorises withdrawal, transfers to another proxy, and signer changes"; `set-threshold/prepare`, "changes how many signers must sign for an action". Quali azioni siano davvero soggette alla soglia non è specificato |
| D4 | Il prelievo dalla UI e il percorso bridge (`glideSessionId`) passano dal Safe? | non documentato |
| D5 | Rotazione e recupero della chiave di trading | nessuna pagina (cercati recover, rotat, revok, compromis). Rimuovere l'EOA dai firmatari del Safe **non** gli toglie il trading (ordini = EOA + chiave API). Esito più probabile: **nuovo conto + nuovo proxy + `transfer` dei fondi**, contendibile da un attaccante (ordini aperti → `HAS_OPEN_ORDERS`; transazione pendente sostituita o cancellata) |
| D6 | Approval residue su proxy o escrow | V3 non usa approval ERC-20 per il trading ("There is no longer an ERC-20 approval of the `TokenTransferProxy`"); il permit "can only ever fund once". Però get-user-multisig-info elenca un tipo di transazione Safe `approveAsset`: con soglia 1 è un possibile vettore di allowance. Sull'EOA di trading i permit sono irrilevanti (un ladro ne firma di nuovi); contano sull'EOA di finanziamento dell'owner |
| D7 | Finanziamento diretto del proxy di trading dall'EOA dell'owner (`proxyAddress`) o via `PROXY_TO_PROXY` | ammesso dalla documentazione, non provato |
| D8 | Una chiave API con soli scope Orders/lettura riceve 401/403 su `withdraw/prepare`? | scope documentati, comportamento non provato |
| D9 | **Onboarding con la chiave nel signer**: login su sx.bet per generare la chiave API (firma di una "Signature Request"), azioni amministrative sul Safe e configurazione T0 (`SafeTx` di add-signer/set-threshold) richiedono firme dell'EOA di trading che il signer rifiuta per progetto | aperto: o la chiave esce dal signer (in contrasto con "solo nel signer"), o serve un percorso amministrativo del signer autorizzato dall'owner, fuori dal MCP, da progettare e rivedere a parte |

## 4. Piano di prova su Toronto

Solo con autorizzazione scritta dell'owner, fondi di test ottenuti dal supporto SX Bet, chiave **dedicata** di testnet che non controlla fondi mainnet (§2.6). Ogni prova registra richieste e risposte senza segreti.

| # | Prova | Esito atteso | Stop se |
|---|---|---|---|
| **T0** | **Go/no-go D1**: aggiunta di un secondo firmatario (altro indirizzo), soglia 2, poi un `withdraw` di test firmato da entrambi | il secondo firmatario riesce a firmare e la transazione viene eseguita | **impossibile → stop**: il 2-di-2 non è utilizzabile, si adotta il §5 opzione B |
| T1 | EOA dedicato, proxy **nuovo**, `GET /user/proxy` | `multisigSafeAddress` non null | null → stop |
| T2 | Ordine firmato dalla sola EOA | accettato | rifiutato → stop (il trading richiederebbe il Safe) |
| T3 | `withdraw` con la sola EOA, soglia 2 | `isReadyToExecute=false`, `signedCount 1 < requiredThreshold 2`, `isExecuted` resta false, saldo invariato | eseguito → stop |
| T4 | `set-threshold 1`, `remove-signer`, `add-signer`, `transfer` con la sola EOA | non eseguiti | uno eseguito → stop |
| T5 | Chiamata diretta dell'EOA al proxy (D2) | impossibile | possibile → stop |
| T6 | Prelievo dalla UI e via bridge con la sola EOA (D4) | non eseguiti senza la seconda firma | eseguiti → stop |
| T7 | Chiave API con soli scope Orders: `withdraw/prepare` e `multisig/*` | 401/403 | accettati → stop |
| T8 | Nuova chiave API generata dall'EOA: la vecchia smette di funzionare | 401 sulla vecchia | — (documenta il rischio) |
| T9 | Attaccante con l'EOA: ordini aperti + sostituzione/cancellazione della transazione pendente durante un recupero | il recupero è comunque possibile entro un tempo noto | recupero impossibile → stop: si accetta che il capitale nel proxy sia perso in caso di furto |
| T10 | `transfer-to-proxy` con `proxyAddress` del proxy di trading, firmato dall'EOA di finanziamento dell'owner (D7) | fondi nel proxy di trading senza passare dall'EOA di trading | — |
| T11 | Rotazione: nuovo conto + `transfer` (D5) | documentata e riuscita | — |

## 5. Raccomandazione

**A. Se T0 e T3–T6 riescono (Safe 2-di-2 utilizzabile):** EOA di trading dedicato, generato nel signer (non Magic; raccomandazione, `owner_commanded_execution.md` D5 aperta), proxy proprio, Safe 2-di-2 con la seconda chiave dell'owner offline; chiave API con soli scope Orders e lettura, custodita secondo la decisione owner `owner_commanded_execution.md` D6 (raccomandato: solo nel signer; D9 resta da risolvere); nel proxy solo il capitale di rischio.

**B. Se T0 fallisce o il Safe non segrega (esito conservativo da assumere finché T0 e T3–T6 non sono provati; oggi né il 2-di-2 né la segregazione sono dimostrati):** conto di trading dedicato con capitale minimo; il limite economico è il saldo del proxy più le posizioni aperte, accettato come perdita massima in caso di furto della chiave. Il conto principale (wallet Magic, Safe 1-di-1) **non** viene usato per il trading automatico.

In entrambi i casi:
1. il capitale **non sosta mai** nell'EOA di trading: si finanzia il proxy di trading direttamente dall'EOA dell'owner (`proxyAddress`, T10) o con un `PROXY_TO_PROXY` dal proxy dell'owner, dopo verifica dell'indirizzo di destinazione;
2. **raccomandazione, in attesa della decisione owner D6** (`owner_commanded_execution.md`, ancora aperta: "solo nel signer (consigliato) o anche nel MCP"): la chiave API con scope Orders dovrebbe stare **solo nel signer**, non nel MCP. Motivo: cancellazioni e heartbeat richiedono solo la chiave API, quindi una chiave nel MCP permetterebbe cancellazioni, anche massive via heartbeat, senza la verifica del comando e l'audit del signer (§2.6). La chiave non ha scope Wallet (withdraw, transfer, admin). Con una sola chiave attiva per indirizzo, emettere una chiave amministrativa per lo stesso indirizzo **revoca** quella del signer: le operazioni amministrative avvengono dalla UI, e dopo serve una nuova chiave per il signer;
3. il signer firma solo `Order` EIP-712 e rifiuta `SafeTx`, permit, `personal_sign` e "messaggi arbitrari" (`isolated_signer_architecture.md`); la `DeleteRequest` multisig rientra fra questi ultimi (non è nominata esplicitamente: va aggiunta ai test di rifiuto del signer); le cancellazioni di ordini non sono firmate ma passano comunque dal signer (punto 2). Finché la chiave resta nel signer, l'estrazione è limitata a ordini entro i limiti firmati dall'owner;
4. le operazioni amministrative (finanziamento, prelievo, firmatari, soglia, chiavi API) restano **fuori dal MCP**, come atti firmati dall'owner (§2.6).

## 6. Cosa NON è stato fatto

Nessuna chiamata autenticata, nessuna modifica al Safe mainnet (1-di-1), nessun movimento di fondi, nessuna chiave usata. Lo studio si basa solo sulla documentazione pubblica.
