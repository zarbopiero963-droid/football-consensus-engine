# S10 — Studio della custodia: EOA, proxy, Safe 2-di-2 (issue #187)

Stato: **STUDIO**. Nessuna modifica a Safe o wallet, nessun fondo mosso, nessuna chiamata autenticata.

Fonte: documentazione ufficiale https://docs.sx.bet, letta il 16-11-2026: `llms-full.txt`, sezioni "Accounts", "Funding", "Safe info" e "Testnet & Mainnet", più le pagine `/user/multisig/*`. Le affermazioni che la documentazione non prova sono marcate `DOCS_UNVERIFIED` e vanno provate su Toronto.

## 1. Il modello documentato

| Elemento | Ruolo (documentazione) | Fonte |
|---|---|---|
| **EOA** | "The wallet you control with a private key. Your identity to the API". Firma **ogni ordine**: `maker` è sempre l'EOA, "never the proxy" | developers/accounts; changelog OBv3 ("maker (your EOA, never the proxy)") |
| **Proxy** | "A contract your EOA owns. Where your money lives and where bets settle". "Trading is proxy-only": i fondi devono stare nel proxy | developers/accounts |
| **Safe** | "Every deployed proxy has a Safe smart account automatically attached" (`multisigSafeAddress`). "The Safe authorises withdrawal, transfers to another proxy, and signer changes" | developers/accounts |
| Gestione del Safe | `add-signer`, `remove-signer` (con nuova `threshold`), `set-threshold`, `withdraw`, `transfer`, `cancel`: ciascuna in due passi, `prepare` → firma `typedData` → `execute` | api-reference `post-user-multisig-*` |
| Finanziamento | Dall'EOA al proxy con un permit EIP-2612 firmato dall'EOA (`POST /user/transfer-to-proxy`) | developers/funding |
| Prelievo e trasferimento | Bloccati con ordini `ACTIVE`/`PENDING` (`400 HAS_OPEN_ORDERS`) | post-user-multisig-withdraw-*, transfer-* |

Stato osservato del tuo conto mainnet (audit in sola lettura del 16-11, riportato dall'owner): Safe con **1 owner e threshold 1**. Il requisito 2-di-2 **non è soddisfatto**.

## 2. Cosa protegge davvero un Safe 2-di-2

| Rischio con la chiave di trading (EOA) compromessa | Protetto da Safe 2-di-2? | Motivo |
|---|---|---|
| Prelievo dei fondi verso l'esterno | **Sì**, se il Safe governa davvero ogni prelievo (`DOCS_UNVERIFIED`, vedi §3) | serve la seconda firma |
| Trasferimento verso un altro proxy | **Sì**, alle stesse condizioni | Safe |
| Rimozione del secondo firmatario o soglia riportata a 1 | **Sì**, se anche i cambi di firmatari e di soglia richiedono la soglia corrente | Safe |
| **Ordini dannosi**: quote pessime contro una controparte complice, che drenano il valore del proxy attraverso il trading | **No** | gli ordini li firma l'EOA da solo; il Safe non interviene |
| Cancellazione di ordini | No (rischio basso) | firma dell'EOA |

**Conseguenza.** Il Safe 2-di-2 è necessario ma **non sufficiente**. Contro una chiave di trading compromessa il limite economico reale lo danno solo:
1. la chiave custodita **solo** nel signer isolato, mai nel MCP né nel modello;
2. i limiti firmati dall'owner e il kill switch del signer, che però valgono solo finché la chiave non esce dal signer;
3. **tenere nel proxy solo il capitale che si accetta di rischiare**, finanziandolo a tranche dall'EOA o da un altro conto. È l'unico limite che regge anche se la chiave è rubata.

## 3. Domande aperte (`DOCS_UNVERIFIED`), da provare su Toronto

1. **Il proxy è "owned" dall'EOA** ("A contract your EOA owns"). L'EOA può chiamare direttamente il proxy, senza passare dal Safe, per muovere fondi? Se sì, il Safe non protegge i prelievi. Prova: lettura on-chain di owner e moduli del proxy e del Safe, poi un tentativo di prelievo con soglia 2 e una sola firma (deve fallire).
2. **`add-signer/execute`: "signerAddress must be the authenticated address".** Chi può aggiungere il secondo firmatario, e il nuovo firmatario deve avere un proprio conto o una propria chiave API SX Bet? Prova: aggiunta di un firmatario dedicato su Toronto.
3. **I cambi di firmatari e di soglia richiedono la soglia corrente** (2 firme su 2)? Prova: con soglia 2, tentativo di `set-threshold 1` con una sola firma (deve fallire).
4. **Allowance:** il finanziamento usa un permit verso `transferToProxyExecutorAddress`. Restano allowance residue sfruttabili? Prova: lettura di `allowance(EOA, spender)` prima e dopo.
5. **Recupero:** se la chiave di trading è compromessa, il secondo firmatario può svuotare o trasferire il proxy da solo (con soglia 2 no), e come si ruota la chiave? Serve una procedura documentata con entrambe le chiavi.

## 4. Piano di prova su Toronto

Solo con autorizzazione dell'owner, una chiave dedicata di testnet e fondi di test.

| # | Prova | Esito atteso |
|---|---|---|
| T1 | EOA di test dedicato, proxy deployato, `GET /user/proxy` → `multisigSafeAddress` | Safe presente |
| T2 | Aggiunta di un secondo firmatario (chiave dell'owner, offline) e soglia 2 | `GET /user/multisig/{safe}/info`: 2 firmatari, soglia 2 |
| T3 | Ordine firmato dalla sola EOA | **accettato**: il trading resta automatico |
| T4 | Prelievo con la sola EOA | **rifiutato** o in attesa della seconda firma |
| T5 | `set-threshold 1` o `remove-signer` con la sola EOA | **rifiutato** |
| T6 | Chiamata diretta dell'EOA al proxy per muovere fondi | **impossibile** |
| T7 | Allowance residue dopo il finanziamento | nessuna allowance sfruttabile |
| T8 | Procedura di rotazione della chiave di trading con 2 firme | documentata e riuscita |

Se T4, T5 o T6 falliscono (cioè la sola EOA riesce a muovere fondi o a cambiare la soglia), **ci si ferma**: il Safe non dà segregazione, e si propongono alternative (sotto).

## 5. Raccomandazione e alternative

1. **Raccomandato:** un EOA di trading dedicato, separato dal wallet principale, con proxy e Safe 2-di-2. La seconda chiave è dell'owner e resta offline. Nel proxy si tiene **solo il capitale di rischio**, ricaricato a tranche dall'owner.
2. **Se il Safe non segrega** (T4–T6 falliti): conto di trading usa e getta con capitale minimo e ricariche manuali dell'owner; il limite economico è il saldo del proxy.
3. Le operazioni amministrative (finanziamento, prelievo, firmatari, soglia) restano **fuori dal MCP**, come atti firmati dall'owner (§2.6).

## 6. Cosa NON è stato fatto

Nessuna chiamata autenticata, nessuna modifica al Safe mainnet (1-di-1), nessun movimento di fondi, nessuna chiave usata. Lo studio si basa solo sulla documentazione pubblica.
