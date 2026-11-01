# SX Bet: preparazione all'attivazione del trading reale

Verifica del 01-11-2026 su fonti ufficiali: docs.sx.bet, help.sx.bet, docs.metamask.io, support.rabby.io, connect.trezor.io, adm.gov.it. Questo documento **non è un parere legale**.

Il trading reale resta **disabilitato** e **non attivabile senza decisioni e azioni esplicite dell'owner**.

## 1. Idoneità all'uso dall'Italia: `BLOCKED_LEGAL_VERIFICATION`

| Fatto | Fonte (VERIFIED) |
|---|---|
| "The Company makes no representation as to the legality of the services provided by it in any jurisdiction." | Terms and Conditions, help.sx.bet/en/articles/3613372, 28-09-2026 |
| L'utente dichiara di trovarsi in un paese dove le scommesse sul servizio sono lecite: "It is your responsibility to ensure that your use of the service is legal." | idem |
| Paesi esclusi: elenco nominativo (Italia non citata) più "**any other country which may prohibit the offering of online gambling to its residents**" | idem |
| Vietati "scrape our odds" e "any robot, spider, crawler, scraper, or other automated means or interface not provided by us"; licenza "for your personal non-commercial purposes only" | idem |
| Clausole **senza** l'eccezione "not provided by us": "retrieve or index any information from the Service using any robot, spider or other automated mechanism" e "You are not allowed to use any kind of robots and programmed devices to participate in game play which purpose would be to disrupt the Service or to commit fraud" (integrazione del 01-11-2026, vedi `magic_wallet_existing_account_audit.md` §1) | idem |
| SX Bet / Nextgen Blockchain Technologies **non compare** nell'elenco ADM dei concessionari autorizzati al gioco a distanza | adm.gov.it, elenco concessionari gioco a distanza |
| ADM inibisce i siti "sprovvisto delle autorizzazioni necessarie per operare la raccolta di giochi in Italia" (art. 102 D.L. 104/2020) | adm.gov.it, inibizione siti non autorizzati |
| `sx.bet` **non è** nella lista dei siti inibiti aggiornata al 13-10-2026 (13.078 voci; sha256 verificato sul file di controllo ADM) | adm.gov.it, siti web inibiti |

**Conclusione:** SX Bet non ha la concessione ADM. Il servizio non garantisce la legalità e lascia la responsabilità all'utente. Per questo l'idoneità **non è verificata** e serve un **parere legale qualificato in Italia** prima di qualunque uso con denaro reale.

Divieto assoluto: nessun VPN né altro mezzo per aggirare restrizioni territoriali (governance §2.1 e §2.6).

**Implicazione per il repository:** usiamo solo l'API ufficiale, cioè una "interface provided by" SX Bet. Lo scraping di sx.bet con il browser è vietato dai Terms e non va fatto.

## 2. Metodi di firma

| Wallet | EIP-712 (`eth_signTypedData_v4`) | Rete SX (4162) | Supporto SX Bet | Valutazione |
|---|---|---|---|---|
| **MetaMask** (software) | VERIFIED: "eth_signTypedData_v4 … We recommend this" | rete personalizzata VERIFIED | VERIFIED ("Connect a wallet"; help center) | **Idoneo** per la firma di ogni ordine nel browser dell'owner |
| **Rabby** (software) | DOCS_UNVERIFIED (nessuna pagina ufficiale lo menziona) | "supports all EVM-compatible networks", rete personalizzata VERIFIED | VERIFIED | Probabile; **da verificare su testnet** |
| **Ledger / Trezor dentro MetaMask** | **Limite ufficiale:** "These wallets only support signing data using `personal_sign`" (docs.metamask.io) | — | non citato da SX Bet | **Non affidabile** per gli ordini SX secondo la documentazione MetaMask |
| Trezor Connect diretto | VERIFIED (`ethereumSignTypedData`, conferma sul dispositivo) | — | non citato | Percorso diverso da MetaMask/Rabby; da provare su testnet |
| Ledger dentro Rabby | DOCS_UNVERIFIED | — | non citato | Da provare su testnet |
| **Wallet Magic di sx.bet** | supporta v4, ma appartiene all'app Magic di SX Bet | — | — | **Non utilizzabile** dal nostro MCP senza esportare la chiave (vedi `magic_and_proxy_wallet.md`); esclusa |

**Raccomandazione:** account SX separato con EOA in **MetaMask** o **Rabby**, con firma di ogni ordine nel browser dell'owner. Un hardware wallet entra solo dopo una prova su testnet che firmi il typed data degli ordini.

## 3. Proxy wallet e saldo (sola lettura, run 9 dell'audit)
- `GET /user/proxy` risponde 200: il proxy esiste (campo `deployed` letto dal tool `sxbet_proxy_wallet`).
- `GET /user/multisig/{safe}/info` risponde 200: la Safe esiste.
- Saldo vuoto, nessun ordine, nessuna posizione, nessun deposito in corso.
- **Nessuna operazione eseguita:** niente deploy, deposito o permit.

## 4. Permessi della API key (`developers/api-key-permissions`)

| Fase | Scope necessari |
|---|---|
| Oggi (lettura) | `Market data: read`, `Orders: read`, `Account: read`, `Wallet: read`. Tutti verificati con risposta 200 nell'audit |
| Trading reale (futuro, decisione dell'owner) | in più `Orders: create` e `Orders: cancel` (quest'ultimo copre anche `POST /heartbeat/v3`) |
| Mai necessari per il MCP | `Account: write`, `Wallet: deposit`, `Wallet: withdraw`, `Wallet: transfer`, `Wallet: admin` |

"Only one key can be active at a time": aggiungere degli scope significa rigenerare la chiave. Lo fa l'owner, poi aggiorna Render e l'environment GitHub.

## 5. Limiti finanziari proposti (tutti disabilitati per default)

Sono i valori della config di rischio del signer (`SXBET_SIGNER_RISK_CONFIG`, firmata dall'owner). Oggi tutti i valori sono 0, quindi ogni ordine viene rifiutato. I valori sotto sono **solo una proposta da validare**: non sono configurati.

| Limite | Default attivo | Proposta per una prima fase controllata (da decidere) |
|---|---|---|
| Stake per ordine | 0 | 1–5 USDC (il minimo ordine runtime è 1 USDC) |
| Esposizione per mercato | 0 | 10 USDC |
| Esposizione per evento | 0 | 20 USDC |
| Esposizione totale | 0 | 50 USDC |
| Perdita per sessione | 0 | 10 USDC |
| Perdita giornaliera | 0 | 20 USDC |
| Ordini aperti massimi | 0 | 3 |
| Slippage massimo | 0 bps | 100 bps |
| Quota minima e massima (prob. implicita) | 0 / 0 | 5% / 95% |
| Età massima dei dati di mercato | 0 s | 5 s |

## 6. Checklist
**Testnet (Toronto, nessun denaro reale):**
- [ ] l'owner crea un account su toronto.sx.bet con un EOA MetaMask/Rabby;
- [ ] l'owner chiede testnet USDC al supporto ("Open a support chat on sx.bet to receive testnet USDC");
- [ ] l'owner crea una API key testnet (le chiavi testnet non funzionano su mainnet);
- [ ] probe `GET /metadata/obv3` su Toronto, passando dal coordinatore;
- [ ] verifica del formato del `salt` e del comportamento di un re-invio dopo timeout (DOCS_UNVERIFIED);
- [ ] prova di firma EIP-712 dal wallet scelto, compreso l'eventuale hardware;
- [ ] il trasporto reale verso la testnet resta **una PR separata, con review e merge dell'owner**.

**Sicurezza:**
- [ ] review indipendente del trasporto reale e del percorso di cancellazione;
- [ ] head del ledger ancorato e lease di scrittura attivi (PostgreSQL);
- [ ] chiave privata mai su Render, GitHub o nel MCP;
- [ ] dispositivo di approvazione dell'owner.

**Attivazione (owner):**
- [ ] **verifica legale in Italia** (`BLOCKED_LEGAL_VERIFICATION`);
- [ ] scelta del wallet;
- [ ] config di rischio firmata;
- [ ] proxy finanziato;
- [ ] scope `Orders: create/cancel`;
- [ ] flag `LIVE_TRADING` firmato con scadenza breve;
- [ ] conferma specifica per ogni ordine.

## 7. Note dalle fonti ufficiali
- **Fee:** la guida utente indica "1% … taker … Maker bets pay 0%" (parlay 5%), mentre la documentazione per sviluppatori dice che le fee sono "set **per account**" (`/user/fees-v3`; `null` = nulla addebitato). Fanno fede le aliquote lette dall'API e `settlement.settleFeeAmount`. L'1% fisso non va mai assunto.
- **Minimo d'ordine mainnet:** la documentazione dice 5 USDC, i metadata live dicono 1. Fa fede il valore runtime.
- **Inventario corretto:** `POST /heartbeat` (legacy) usa l'header `X-Api-Key` (`ApiKeyAuth`), non `x-sx-api-key`.
