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
2. **Signer isolato con private key** (branch `feat/sxbet-simulation-isolated-signer`, `signer/`).
   - La chiave sta solo nel processo del signer, che accetta un intent strutturato e lo firma solo con un'approvazione Ed25519 dell'owner legata all'hash esatto dei parametri.
   - Altre garanzie: approvazione monouso con scadenza ≤120 s, limiti di esposizione impostati dall'owner (default zero), idempotenza e audit append-only.
   - Il rischio residuo è custodire la chiave in un servizio sempre acceso. Richiede un dispositivo o un'app di approvazione dell'owner, ancora da realizzare.
3. **Chiave nel server MCP, in env Render o GitHub: vietato.** Il modello o la CI potrebbero arrivare a firmare (§2.2, §2.6).

## Cosa manca prima del `LIVE_TRADING` (decisioni dell'owner)

1. Scelta del metodo di firma: opzione 1, browser e wallet, oppure opzione 2, signer isolato con dispositivo di approvazione.
2. Proxy wallet: oggi il proxy è deployato? Si verifica con `sxbet_proxy_wallet` → `deployed`. Serve anche USDC sul proxy. Deploy e depositi sono operazioni finanziarie e richiedono conferme specifiche.
3. API key: oggi ha gli scope di lettura, verificati con 200 nel workflow live. Un percorso di ordini richiederebbe `Orders: create` e `Orders: cancel`, che vanno concessi solo con la decisione di attivare il trading.
4. Limiti di esposizione: importo massimo per ordine, giornaliero e perdita massima, fissati dall'owner. Il default è zero.
5. Percorso di invio e riconciliazione (`POST /orders-v3`, poi `GET /orders-v3/{orderId}` e `GET /fills-v3`): codice di trading, merge riservato all'owner.
6. Verifica di idoneità del servizio nella giurisdizione applicabile.

Il MCP oggi espone **solo letture** (36 tool) e, sul branch del signer, la **simulazione**. Nessun tool può firmare o inviare.
