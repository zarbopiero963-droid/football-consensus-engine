# SX Bet: wallet Magic dell'account esistente — audit di compatibilità

Mandato dell'owner: `FCB-SXBET-MAGIC-WALLET-FULL-AUDIT-01` (01-11-2026), baseline verificata `main` 3ddec62.

Fonti: solo pagine ufficiali (docs.sx.bet, help.sx.bet, docs.magic.link, magic.link/legal), più test offline nel repository. Non è un parere legale.

**Cosa NON è stato fatto:**
- nessuna chiave privata reale è stata richiesta, letta, esportata o usata;
- nessun ordine, cancellazione, deposito, permit o operazione on-chain;
- nessuna pagina dell'app sx.bet è stata aperta: i Terms vietano lo scraping.

Legenda:
- `PASS`: verificato su fonte ufficiale o con un test riproducibile.
- `PARTIAL`: verificato in parte; il resto è indicato.
- `NOT_VERIFIED`: dedotto o non documentato.
- `BLOCKED_EXTERNAL`: richiede un'azione esterna (owner, testnet, SX Bet).
- `OWNER_REPORTED`: dichiarato dall'owner, non verificabile da noi.

## 1. Esito MAGIC-01 … MAGIC-08

| ID | Domanda | Esito | Evidenza |
|---|---|---|---|
| MAGIC-01 | La chiave esportata è l'EOA dell'account (`maker`)? | `PASS` (documentazione) + `BLOCKED_EXTERNAL` (verifica sul tuo account) | (1) |
| MAGIC-02 | Può produrre firme EIP-712 valide per gli ordini? | `PASS` | (2) |
| MAGIC-03 | Si può firmare senza aprire sx.bet? | `PASS` | (3) |
| MAGIC-04 | Serve una sessione Magic attiva? | `PASS`: no | (4) |
| MAGIC-05 | Una firma esterna è riconosciuta? | `PARTIAL` | (5) |
| MAGIC-06 | Il backend può firmare senza l'SDK JS di Magic? | `PASS` | (3) |
| MAGIC-07 | L'account web continua a funzionare? | `PARTIAL`: sì, con conflitti operativi | (6) |
| MAGIC-08 | Condizioni del provider | `PARTIAL`: nessun divieto tecnico; idoneità legale `BLOCKED_LEGAL_VERIFICATION` | (7) |

**(1) MAGIC-01**
- `docs.sx.bet/developers/quickstart`: "Sign up at sx.bet with email or Google … Fetch your private key from the assets page — you'll use it to sign orders". SX Bet stessa indica di esportare la chiave Magic per firmare gli ordini.
- `docs.sx.bet/user-guides/faq/export-wallet-metamask`: "Select "Access Private Key" … import account and paste your private key … Logout … Connect MetaMask … You are now connected on SX Bet via MetaMask."
- `developers/accounts`: l'EOA è "The wallet you control with a private key. Your identity to the API".
- `api-reference/get-user-proxy`: "The proxy address is a deterministic derivation from your user address".
- Deduzione (`NOT_VERIFIED` testualmente): stessa chiave, quindi stesso indirizzo, quindi stesso proxy, stessa Safe e stesso betting account. La FAQ non dice letteralmente "same account".
- Discrepanza documentale: la FAQ cita ancora "Fortmatic" e `sx.bet/account`; le pagine developer citano `sx.bet/wallet/assets`.
- **Verifica sul tuo account (azione owner, §2 sotto):** confrontare l'indirizzo mostrato dal wallet dopo l'import con i `owners` della Safe letti in sola lettura. Nessuna chiave viene rivelata a noi.

**(2) MAGIC-02**
- Magic ToS: "a cryptographic private and public key pair is generated". Product security: "Magic generates a key pair based on the Ethereum blockchain". SDK: `revealEVMPrivateKey` "Allows for users to take their private key to another wallet".
- La chiave si importa in MetaMask, quindi è una chiave EOA secp256k1 standard. La parola "secp256k1" non compare nella documentazione Magic: deduzione.
- SX Bet (`api-reference/eip712-order-signing`): esempi con `eth_account`/ethers e chiave privata. "Using a private key is the most straightforward approach for bots and scripts."
- **Prova crittografica:** `tests/test_sxbet_eip712_official_vectors.py` (PR #153), 26 test, solo chiavi usa e getta e chiavi di test pubbliche:
  - il digest di `signer/eip712.py` è identico a quello di un'implementazione indipendente scritta a mano (codifica EIP-712, keccak, ecrecover secp256k1, EIP-55), su mainnet 4162 e Toronto 79479957;
  - una chiave esadecimale a 32 byte (con o senza `0x`) deriva l'indirizzo atteso e la sua firma ricostruisce esattamente `maker`;
  - test negativi: chainId, verifyingContract, nome o versione, ordine dei campi, `timeInForce` non firmato, maker diverso dal firmatario.
- Il formato esatto con cui Magic mostra la chiave (con o senza `0x`) non è documentato: i test coprono entrambi.

**(3) MAGIC-03 / MAGIC-06**
- La firma è una normale firma `eth_account` sul typed data `Order`. Il dominio arriva dal pubblico `GET /metadata/obv3`.
- L'SDK Magic serve solo per rivelare la chiave: "Neither Magic nor the developer can see this key; only the end user can."

**(4) MAGIC-04**
- `developers/authentication`: "Place an order and account actions | API key + signature".
- `POST /orders-v3`: API key più ordine EIP-712. Nessuna route richiede un token di sessione Magic.

**(5) MAGIC-05**
- `maker` "Must match the account that signs the order. This is NOT your proxy wallet address". L'exchange verifica la firma: errore documentato "Invalid OBv3 order EIP-712 signature" (pagina testnet).
- Il metodo esatto di verifica (ecrecover o EIP-1271) non è documentato. Il formato "packed v-r-s (65 bytes)" indica ECDSA: deduzione.
- **Non dimostrabile senza un invio reale:** la prova definitiva è un ordine su testnet Toronto con un account di prova (`BLOCKED_EXTERNAL`, §13 del mandato).
- Nota: l'unico esempio completo dell'OpenAPI (`POST /orders-v3`) è **incoerente**. La firma non ricostruisce il `maker` indicato e l'`orderId` non corrisponde al digest. Per questo nessun vettore ufficiale è riproducibile; è un difetto della documentazione (test `test_OFFICIAL_EXAMPLE_NOT_A_VECTOR_*`).

**(6) MAGIC-07**
- Magic ToS: l'export è "a copy of your Private Key". Nessuna fonte dice che la chiave venga ruotata o il wallet disabilitato dopo l'export (deduzione: l'account web resta funzionante).
- Conflitti operativi tra uso web e backend (fatti VERIFIED, impatto dedotto):
  - "Only one key can be active at a time": rigenerare la API key dal web invalida quella del backend;
  - `clientOrderId` "Must be unique per address": web e backend condividono lo spazio dei nomi;
  - "A Safe holds at most one pending transaction": operazioni Safe concorrenti tra web e backend collidono;
  - l'heartbeat vale per l'intero proxy ("The proxy wallet owned by the key's address"): un timeout dell'heartbeat del backend cancellerebbe anche gli ordini fatti dal web;
  - gli ordini fatti dal web non sono nel ledger del signer. Il risk engine deve leggere ordini aperti e saldo dal vivo, non fidarsi solo del ledger;
  - gli ordini usano un `salt` casuale, quindi non c'è conflitto di nonce on-chain per gli ordini.

**(7) MAGIC-08**
- **Magic ToS (in vigore dal 7-5-2025):**
  - "you may choose to export a copy of your Private Key … YOU ALONE WILL BE RESPONSIBLE FOR THE MANAGEMENT OF YOUR PRIVATE KEY COPY … MAGIC WILL NOT ASSIST YOU WITH STORING YOUR PRIVATE KEY COPY";
  - nessuna clausola trovata vieta l'export o l'uso fuori da Magic.
- **SX Bet Terms (28-09-2026):**
  - "maintain the security and confidentiality of your Ethereum private keys";
  - divieto di "automated means or interface not provided by us": l'API ufficiale è un'interfaccia fornita da loro;
  - **però esistono anche clausole senza quell'eccezione:** "retrieve or index any information from the Service using any robot, spider or other automated mechanism" e "You are not allowed to use any kind of robots and programmed devices to participate in game play which purpose would be to disrupt the Service or to commit fraud";
  - "personal non-commercial purposes only"; vietato "permit any third party to use of the service".
- **Segnali che l'automazione via API è prevista:** `docs.sx.bet/llms.txt` ("Build trading bots, market-making systems … with REST, WebSocket, and on-chain APIs") e la pagina di firma ("for bots and scripts").
- Non esistono termini API separati (`NOT_VERIFIED` che ce ne siano).
- **Esito:** nessun impedimento tecnico. L'uso con fondi reali resta `BLOCKED_LEGAL_VERIFICATION` (vedi `activation_readiness.md` §1).

## 2. Lo stesso account: EOA, proxy, Safe, saldo

Modello ufficiale (`developers/accounts`): "An account is two addresses. You sign with one and you are funded at the other."

| Elemento | Cosa è | Come si legge (sola lettura) | Stato sul tuo account |
|---|---|---|---|
| EOA (Magic) | identità verso l'API, firma ogni ordine (`maker`) e le transazioni della Safe | `GET /user/balance-v3` → `userAddress` ("Your EOA."; lista vuota se il conto non è mai stato finanziato); `owners` della Safe | indirizzo non riportato qui, per riservatezza |
| Proxy (betting account) | contratto CREATE2 derivato dall'EOA, dove stanno i fondi e si regolano le scommesse | `GET /user/proxy` (`sxbet_proxy_wallet`), campo `deployed` | esiste (audit run 9) |
| Safe | autorizza prelievi, trasferimenti e cambi di firmatario; **non** gli ordini | `GET /user/multisig/{safe}/info` (`sxbet_safe_info`): `owners`, `threshold` | esiste (audit run 9) |
| API key | legata a un solo indirizzo; "Only one key can be active at a time" | — | scope di lettura verificati (risposte 200) |
| Saldo | `available` più `pendingAvailable`, contro `availableAmount`: le due pagine ufficiali sono in conflitto, il MCP usa il valore minore | `sxbet_balance` | vuoto (audit run 9) |

**Reti mostrate su `sx.bet/account` (Betting account USDC, SX Rollup, Ethereum, Polygon, Arbitrum):** non sono cinque wallet indipendenti.
- Lo stesso EOA ha lo stesso indirizzo su tutte le reti EVM.
- Il betting account è il proxy su SX Rollup (chainId 4162). Gli ordini si firmano solo nel dominio SX (escrow `0x8904…c1eB`).
- Le altre reti sono saldi dello stesso indirizzo EOA, che si portano sul proxy tramite i flussi di deposito o bridge dell'interfaccia.
- Fondi disponibili, fondi nel wallet e fondi impegnati in posizioni sono grandezze distinte (`sxbet_balance`, `sxbet_positions_summary`).

**Conclusione:**
- Un ordine firmato dalla chiave Magic esportata porta `maker` uguale all'EOA dell'account. Per costruzione del proxy (derivazione deterministica) viene regolato sullo **stesso** betting account e sullo **stesso** proxy, senza creare un secondo account.
- **Questo è documentato, non provato con un ordine reale:** la prova definitiva richiede un ordine (vedi §5 della checklist).
- **Verifica che puoi fare tu, senza rivelare nulla:**
  1. dal tuo connettore MCP (Claude o ChatGPT) chiedi `sxbet_proxy_wallet` e poi `sxbet_safe_info`: i `owners` della Safe contengono il tuo EOA. Sono dati del tuo account, mostrati solo nella tua chat; l'audit CI non li pubblica;
  2. confrontalo con l'indirizzo che `sx.bet` mostra per il tuo wallet Magic. Se un giorno importi la chiave in un wallet locale, confrontalo anche con l'indirizzo mostrato da quel wallet: devono coincidere.

## 3. Rischio decisivo: la chiave Magic custodisce tutti i fondi

La stessa chiave che firma gli ordini firma anche le `SafeTx` di **prelievo, trasferimento e cambio di firmatario** (`developers/accounts`, flussi `POST /user/multisig/{withdraw,transfer}/{prepare,execute}`). SX Bet: "Anyone with your private key has full control of your wallet and funds."

Conseguenze per il progetto:
1. il signer deve accettare **solo** typed data `Order` sul dominio dell'escrow, e rifiutare qualunque `SafeTx`, permit EIP-2612, `personal_sign` o messaggio arbitrario. È già così: `signer/` firma solo `Order` (governance §2.6);
2. una compromissione del signer equivale alla perdita di **tutto** il saldo del proxy, non solo dell'esposizione degli ordini. I limiti di rischio non proteggono da questo scenario;
3. mitigazioni (decisione dell'owner):
   - mantenere sul proxy solo il capitale destinato al trading;
   - custodia in KMS/HSM (`isolated_signer_architecture.md` §3);
   - in alternativa, un EOA dedicato, cioè un account separato (opzione D).

## 4. Verifica dall'Italia

| Aspetto | Esito |
|---|---|
| Accessibilità tecnica e funzionamento dell'account | `OWNER_REPORTED`: l'owner dichiara che l'account è accessibile e funzionante dall'Italia |
| Disponibilità dell'account | `OWNER_REPORTED` |
| Condizioni contrattuali | `PARTIAL`: nessuna dichiarazione di legalità, esclusione generica dei paesi "which may prohibit"; clausole sull'automazione al §1 (7) |
| Requisiti normativi (ADM) | `BLOCKED_LEGAL_VERIFICATION`: SX Bet non è concessionario ADM (vedi `activation_readiness.md` §1) |
| API automatizzate con fondi reali | `BLOCKED_LEGAL_VERIFICATION` |

Nessuna VPN né altro aggiramento geografico.
