# SX Bet: wallet Magic, proxy wallet e percorsi di firma (verifica sulla documentazione ufficiale)

Verifica del 01-11-2026 sulla documentazione ufficiale di Magic (https://docs.magic.link) e di SX Bet (https://docs.sx.bet), più una chiamata pubblica a `GET /metadata/obv3` su mainnet e testnet.

Legenda:
- **VERIFIED**: letto su una pagina ufficiale.
- **DOCS_UNVERIFIED**: non trovato nella documentazione ufficiale.
- **INFERENCE**: deduzione nostra, indicata come tale.

Nessuna operazione su wallet o fondi è stata eseguita.

## 1. Magic

| Tema | Esito | Fonte e citazione |
|---|---|---|
| Firma EIP-712 | **VERIFIED** | `docs.magic.link/embedded-wallets/sdk/client-side/javascript`: tra gli "EVM RPC Methods" compaiono `eth_signTypedData_v3` e `eth_signTypedData_v4` |
| Compatibilità con SX Bet | VERIFIED + INFERENCE | SX Bet: "Any injected provider that exposes `eth_signTypedData_v4` (e.g. MetaMask) works" (`api-reference/eip712-order-signing`). Magic espone il metodo, quindi a livello di protocollo è compatibile |
| UI di conferma della firma | **VERIFIED** | "Personal Signature UI is disabled by default"; "Wallets are by default opted out of Magic's Signature Request UI and Sign Confirmation" (`wallets/features/personal-signatures`, `wallets/security/sign-confirmation`). La impostano lo **sviluppatore dell'app**, cioè SX Bet, dalla dashboard: non è una scelta dell'utente |
| Wallet legato a un'app | VERIFIED (indiretto) + INFERENCE | Domain allowlist: "Magic will reject all requests from domains that are not explicitly included"; lato Admin SDK un indirizzo può risultare "address belongs to a different app". Il wallet Magic dell'owner appartiene **all'applicazione Magic di SX Bet**. Un'altra app, con la propria chiave Magic, crea un wallet diverso |
| Usare il wallet Magic di sx.bet dalla nostra app | **Non possibile ufficialmente** | Nessuna integrazione documentata. Usare la chiave pubblicabile di SX Bet dal nostro dominio non è autorizzato: sarebbe un aggiramento dei controlli di accesso, vietato dalla governance §2.1 |
| Export della chiave | VERIFIED | `user.revealEVMPrivateKey`: "Neither Magic nor the developer can see this key; only the end user can." Su SX Bet: pagina assets / "Access Private Key". **Non lo facciamo e non lo chiediamo** |
| Chain SX (4162) | DOCS_UNVERIFIED (non elencata) | Magic documenta reti EVM personalizzate (`rpcUrl`, `chainId`; `@magic-ext/evm`). La firma degli ordini è off-chain: il `chainId` 4162 sta nel dominio EIP-712. Non è verificato come Magic si comporta con un `chainId` di dominio diverso dalla rete attiva |

**Conclusione:** la firma con il wallet Magic dell'owner è possibile solo dentro sx.bet, cioè con piazzamento manuale sul loro sito. Dal nostro MCP non lo è senza esportare la chiave, e l'owner ha escluso l'export.

## 2. Percorsi ufficiali possibili

1. **READ_ONLY + SIMULATION con la sola API key.** Già attivo: 37 tool.
2. **Cancellazioni e heartbeat con la sola API key, senza firma** (auth matrix: `DELETE /orders-v3*`, `POST /heartbeat/v3`).
   - Richiedono lo scope `Orders: cancel`.
   - Sono comunque operazioni coperte dal §2.6: servono la policy e la conferma dell'owner.
3. **Firma per ordine nel browser dell'owner con un wallet iniettabile che l'owner controlla** (MetaMask, Rabby, hardware wallet), tramite `eth_signTypedData_v4` (percorso (b) della documentazione SX Bet).
   - È la via **consigliata**: la chiave non esce dal wallet e ogni firma è un gesto fuori banda dell'owner.
   - **Vincolo:** non funziona con il wallet Magic di sx.bet.
4. **Account SX separato con un EOA che l'owner controlla in un wallet iniettabile** ("Connect a wallet").
   - Lo si finanzia dal proxy dell'account Magic tramite l'interfaccia di sx.bet: prelievo o trasferimento dalla Safe, firmato dentro sx.bet.
   - Poi si usa il percorso 3. La chiave Magic non viene esportata.
5. **Signer isolato** (`signer/`) solo con una chiave dedicata che l'owner fornisce deliberatamente. **Mai la chiave Magic.**

**Non documentato (DOCS_UNVERIFIED):**
- chiavi delegate o di sessione per firmare gli ordini SX Bet. Il campo `maker` deve essere l'EOA che firma, non il proxy;
- delega della firma degli ordini tramite i signer della Safe. La Safe governa solo prelievi, trasferimenti e cambi di firmatario.

## 3. Proxy wallet SX Bet (flusso ufficiale)

| Passo | Route | Firma | Note (VERIFIED) |
|---|---|---|---|
| Individuazione | `GET /user/proxy` | nessuna | Campi `obv3ProxyWalletAddress`, `deployed`, `multisigSafeAddress`. "`deployed` is the only field that reports existence" (indirizzo CREATE2). Tool MCP: `sxbet_proxy_wallet` |
| Deploy | `POST /user/deploy-proxy` | nessuna (API key, scope `Wallet: admin`) | Asincrono; senza proxy `POST /orders-v3` risponde `PROXY_NOT_DEPLOYED`. **Non eseguito** |
| Stato del deploy | `GET /user/pending-deploy-proxy` | nessuna | "`pending: false` does not mean the deployment succeeded". Tool: `sxbet_proxy_deploy_pending` |
| Deposito | `POST /user/transfer-to-proxy` | **permit EIP-2612** su `(owner, spender, value, nonce, deadline)` | `spender` = `transferToProxyExecutorAddress`; `deadline` almeno un'ora nel futuro; lo stesso permit finanzia una sola volta; "A `200` means recorded, not completed". **Non eseguito** |
| Stato del deposito | `GET /user/transfer-to-proxy/status`, `/pending` | nessuna | `PENDING`/`SUCCESS`/`FAILED`. Tool: `sxbet_proxy_funding_status`, `sxbet_proxy_funding_pending` |
| Prelievo e trasferimento | `POST /user/multisig/{withdraw,transfer}/{prepare,execute}` | EIP-712 `SafeTx` dell'EOA firmatario | Errore `HAS_OPEN_ORDERS`: prima vanno cancellati gli ordini aperti. **Non esposto** |
| Info della Safe | `GET /user/multisig/{safeAddress}/info` | nessuna | `owners`, `threshold`, `nonce`, `pendingTransaction`. Tool: `sxbet_safe_info` |

**Stato attuale dell'account owner (audit read-only, run 7):**
- `GET /user/proxy` risponde 200 e `GET /user/multisig/{safe}/info` risponde 200: il proxy e la sua Safe esistono;
- saldo, ordini, posizioni e depositi sono vuoti;
- nessun deploy, deposito o permit è stato tentato.

## 4. Rete e token (`GET /metadata/obv3`, 01-11-2026)

| | Mainnet | Testnet (Toronto) |
|---|---|---|
| `chainId` | 4162 | 79479957 |
| Dominio EIP-712 | `OBv3 Escrow` v1, `verifyingContract` 0x890482680C3a0116aBB003B17e7D694D13a6c1eB | escrow 0x0Db3aBA7de0BCb41FF99D35306f366cFFEEede38 |
| USDC (`baseToken`, 6 decimali) | 0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B | 0x1BC6326EA6aF2aB8E4b6Bc83418044B1923b2956 |
| `transferToProxyExecutorAddress` | 0xfC3dbB46D0703703fC1D1677E7e0BaBF971adcfF | 0xBB2828D990eC30B46715773b654eC4D44703177d |
| `oddsLadderStepSize` | 125 (0,125%) | 125 |
| Minimo d'ordine | `orderSizeMinimum` "1" (1 USDC) | idem |
| `bettingDelay` | live 8000 ms, prematch 0 | idem |
| RPC | https://rpc-rollup.sx.technology | https://rpc-rollup.toronto.sx.technology |

**Conflitto documentale:** la pagina `developers/testnet-and-mainnet` indica 5 USDC come minimo mainnet, mentre i metadata live riportano 1. Per la documentazione stessa ("Read every value at runtime") vale il valore runtime. Il codice legge sempre i metadata e non usa costanti.

Gas: "SX Bet covers all gas fees for betting transactions". Per deploy del proxy ed execute della Safe il gas non è indicato (DOCS_UNVERIFIED).

## 5. Ordini (contratto ufficiale, sintesi)

- **Corpo di `POST /orders-v3`:** `orders[]` con al massimo `maxCreateOrders` (10) ordini, tutti con lo stesso `maker`. Campi di ogni ordine:
  - firmati: `marketHash`, `maker` (l'EOA, **non** il proxy), `totalBetSize`, `percentageOdds`, `salt`, `expiry` (secondi);
  - altri: `baseToken`, `isMakerBettingOutcomeOne`, `timeInForce` (`GTC`/`IOC`/`FOK`, non firmato, nessun default), `orderSignature` (65 byte), `clientOrderId` opzionale (unico per indirizzo).
- **Risposta:** per ogni ordine `orderId` (= digest EIP-712), `status` (`SUBMITTED`/`FAILED`) e `commandId`. Con `waitForOutcome` arriva anche `outcome`:
  - `state`: `RESTED`, `FULLY_FILLED`, `PARTIAL_FILL_DONE`, `PARTIAL_FILL_RESTED`, `CANCELLED`, `TIMEOUT`;
  - `cancelReason`: `NO_LIQUIDITY`, `INSUFFICIENT_BALANCE`, `EXPIRED` e altri.
- **Comportamento:**
  - l'endpoint è asincrono per default;
  - i batch **non sono atomici**;
  - non esiste "amend": per cambiare prezzo si cancella e si ripubblica.
- **Ciclo di vita:** `PENDING`, poi `ACTIVE`, poi `INACTIVE` con `inactiveReason` tra `FILLED`, `USER_REQUESTED`, `EXPIRED`, `NO_LIQUIDITY`, `INSUFFICIENT_BALANCE`, `EVENT_LIFECYCLE`, `MARKET_HALTED`, `HEARTBEAT_TIMEOUT`, `POST_ONLY_WOULD_CROSS`, `SYSTEM`.
- **Cancellazione:** `DELETE /orders-v3` accetta al massimo 100 ordini. La risposta divide gli ordini tra `cancelled`, `notCancelled` e `unconfirmed`; "unconfirmed" **non** è un fallimento e va riletto, non ritentato alla cieca.
- **Fee** (`GET /user/fees-v3`): stringhe decimali oppure `null`, che significa "unset and nothing is charged". Le payout fee si applicano sul profitto al settlement.
- **Limiti dell'exchange** (`developers/risk-limits`): per il maker vale "resting + in-flight + new ≤ balance" per mercato ed esito; per il taker la dimensione si misura in modo globale. **Non** sostituiscono i limiti di esposizione dell'owner (§2.6).
- **Elenco completo degli errori:** non pubblicato (DOCS_UNVERIFIED). Codici visti nella documentazione: `PROXY_NOT_DEPLOYED`, `ODDS_NOT_ON_LADDER`, `TOTAL_BET_SIZE_TOO_LOW`, `POST_ONLY_REQUIRES_GTC`, `INVALID_MARKET`, `BAD_AUTH`.

## 6. Decisione richiesta all'owner prima del trading reale

Con il solo wallet Magic e senza esportare la chiave restano due possibilità:
- (i) READ_ONLY / SIMULATION, più cancellazioni e heartbeat con la sola API key;
- (ii) ordini piazzati a mano su sx.bet.

Per ordini via MCP serve un **EOA in un wallet iniettabile controllato dall'owner**: account SX separato, finanziato dall'owner tramite sx.bet. La firma avviene per ogni ordine nel browser dell'owner, dopo il controllo del rischio e la conferma verificata dal signer.
