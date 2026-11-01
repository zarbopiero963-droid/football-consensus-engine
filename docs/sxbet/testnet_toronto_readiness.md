# Testnet Toronto: readiness e piano di collaudo

Mandato owner SXBET-SECURE-SIGNER-PREPARATION-01 §8. Verifica del 01-11-2026, solo su fonti ufficiali:
- https://docs.sx.bet (pagine `.md`, header `last-modified` riportati);
- https://status.sx.bet (status page SX.bet);
- https://docs.sx.technology, che oggi reindirizza (301) a `docs.sx.bet`.

Due sole chiamate di rete verso l'exchange: una `GET /metadata/obv3` pubblica e non autenticata per rete (Toronto e mainnet). Non è stato fatto nessun login, nessuna navigazione dell'app sx.bet o toronto.sx.bet, nessuna API key e **nessun ordine, nemmeno su testnet**.

Etichette: `VERIFIED` (fonte ufficiale citata o valore runtime osservato), `DOCS_UNVERIFIED` (la fonte non lo dice), `BLOCKED_EXTERNAL` (serve un'azione dell'owner o di SX Bet), `NOT_TESTED`.

## 1. Fatti verificati

### 1.1 Disponibilità
| Fatto | Stato | Fonte e citazione |
|---|---|---|
| Toronto esiste ed è una testnet completa | VERIFIED | `developers/testnet-and-mainnet` (last-modified 31-10-2026 19:46 GMT): "SX Bet runs a full testnet environment (called **Toronto**) that mirrors mainnet" |
| Superficie ordini di Toronto attiva oggi | VERIFIED (runtime) | `GET https://api.toronto.sx.bet/metadata/obv3` → HTTP 200, `status: success` alle 2026-11-01T16:00:46Z. La documentazione (`api-reference/get-metadata-obv3`) dice: "This is also the canonical order-surface availability probe: 200 means the surface is live, 503 means it is unavailable on that environment" |
| Stato di Toronto sulla status page | DOCS_UNVERIFIED | https://status.sx.bet mostra "All Systems Operational" con i soli componenti "API" e "Web App", senza distinguere la testnet |
| Pagina `api-reference/testnet` | VERIFIED | risponde 307 verso `/developers/testnet-and-mainnet.md`. Anche `developers/environments` reindirizza lì |

### 1.2 Endpoint e catena
Fonte: `developers/testnet-and-mainnet`, sezione "Configuration reference" ("Everything that differs between testnet and mainnet"). È confermata da `api-reference/references` (last-modified 31-10-2026 20:06 GMT).

| | Toronto | Mainnet |
|---|---|---|
| API base URL | `https://api.toronto.sx.bet` | `https://api.sx.bet` |
| App (solo browser dell'owner) | `https://toronto.sx.bet` | `https://sx.bet` |
| Chain ID | `79479957` | `4162` |
| RPC | `https://rpc-rollup.toronto.sx.technology` | `https://rpc-rollup.sx.technology` |
| Realtime WebSocket | `wss://realtime.toronto.sx.bet/connection/websocket` | `wss://realtime.sx.bet/connection/websocket` |
| Explorer API | `https://explorerl2.toronto.sx.technology/api` | `https://explorerl2.sx.technology/api` |
| USDC | `0x1BC6326EA6aF2aB8E4b6Bc83418044B1923b2956` | `0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B` |
| OBv3 Escrow | `0x0Db3aBA7de0BCb41FF99D35306f366cFFEEede38` | `0x890482680C3a0116aBB003B17e7D694D13a6c1eB` |
| Minimo d'ordine (pagina docs) | `1` USDC | `5` USDC |
| Metadata | `https://api.toronto.sx.bet/metadata/obv3` | `https://api.sx.bet/metadata/obv3` |

L'URL dell'explorer web (non API) non è pubblicato: `DOCS_UNVERIFIED`.

### 1.3 Valori runtime osservati (`GET /metadata/obv3`, pubblico, senza chiave)
| Campo | Toronto (2026-11-01T16:00:46Z, HTTP 200) | Mainnet (2026-11-01T16:00:47Z, HTTP 200) |
|---|---|---|
| `chainId` = `domain.chainId` | 79479957 | 4162 |
| `domain.name` / `version` | `OBv3 Escrow` / `"1"` | idem |
| `domain.verifyingContract` = `activeAsset.escrowAddress` | 0x0Db3aBA7de0BCb41FF99D35306f366cFFEEede38 | 0x890482680C3a0116aBB003B17e7D694D13a6c1eB |
| `activeAsset.baseToken` (USDC, `decimals` 6) | 0x1BC6326EA6aF2aB8E4b6Bc83418044B1923b2956 | 0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B |
| `transferToProxyExecutorAddress` | 0xBB2828D990eC30B46715773b654eC4D44703177d | 0xfC3dbB46D0703703fC1D1677E7e0BaBF971adcfF |
| `oddsLadderStepSize` | 125 | 125 |
| `limits.orderSizeMinimum` / `BaseUnits` | "1" / "1000000" | "1" / "1000000" |
| `limits.minRestingOrderSize` / `BaseUnits` | "1" / "1000000" | "1" / "1000000" |
| `limits.maxCreateOrders` / `maxCancelOrders` | 10 / 100 | 10 / 100 |
| `limits.rfqRequestTtlSeconds` | 60 | 60 |
| `bettingDelay.liveMsDefault` / `pregameMsDefault` | 8000 / 0 | 8000 / 0 |
| `bettingDelay.sportIdToMs` | {1:8000, 2:8000, 3:8000, 5:10000, 6:5000, 7:6000, 8:8000, 15:8000} | identico |
| `bettingDelay.leagueIdToMs` | come mainnet **più** `"1431": 10000` | {1:4000, 3:5000, 29:6000, 30:6000, 171:5000, 243:3000, 1112:6000, 1113:6000, 1114:6000, 1192:4000, 1715:4000} |

Coincidono con `docs/sxbet/magic_and_proxy_wallet.md` §4 e con i valori fissati in `signer/network_profile.py`.

**Conflitti documentali:**
- il minimo d'ordine mainnet è 5 USDC secondo la pagina e 1 secondo il runtime;
- `minRestingOrderSize` è "0.1" nell'esempio OpenAPI e "1" nel runtime, su entrambe le reti.

Vale il runtime: "Read every value at runtime — do not copy the examples".

### 1.4 Account, API key e fondi testnet
| Passo | Stato | Citazione (`developers/testnet-and-mainnet`) |
|---|---|---|
| Account | VERIFIED | "Sign up at toronto.sx.bet and export your private key from the assets page, the same way you would on mainnet." |
| Proxy | VERIFIED | "a deployed proxy is mandatory before your first order ... The simplest way to do this is through the toronto.sx.bet UI" |
| USDC testnet | VERIFIED | "Open a support chat on sx.bet to receive testnet USDC." **Nessun faucet** è documentato (ricerca "faucet" in `llms.txt`: zero risultati) |
| API key | VERIFIED | "generate an API key from the **API section**. Testnet keys are network-specific and won't work on mainnet." `api-reference/api-key` (last-modified 31-10-2026 21:54 GMT): "For Toronto testnet, generate the key on toronto.sx.bet — keys are per-environment", "Only one key can be active at a time", header `x-sx-api-key` |
| Proxy per rete | VERIFIED | "Proxy wallets are per-environment. A first mainnet order without a deployed proxy returns `PROXY_NOT_DEPLOYED`." |

### 1.5 Firma EIP-712, salt, nonce, expiry
- **Dominio** (`api-reference/eip712-order-signing`, last-modified 31-10-2026 22:52 GMT): "Read the domain from `GET /metadata/obv3`", con campi `{ name, version, chainId, verifyingContract }`. Per Toronto: `OBv3 Escrow`, `"1"`, 79479957, `0x0Db3…de38` (VERIFIED, runtime). Pitfall ufficiale: "If orders come back with `Invalid OBv3 order EIP-712 signature`, check that your signing domain's `chainId` matches the network". Nel dominio non c'è `salt`.
- **Struct `Order`** (8 campi, in quest'ordine): `marketHash`, `baseToken`, `totalBetSize`, `percentageOdds`, `salt`, `expiry`, `maker`, `isMakerBettingOutcomeOne`. Il dominio è identico su entrambe le reti salvo `chainId` e `verifyingContract`.
- **Salt: conflitto noto, ancora aperto (DOCS_UNVERIFIED).** La firma è sempre su `uint256`, ma il campo JSON inviato ha formati diversi:
  - JS (`eip712-order-signing` e `posting-orders`): `salt: hexlify(randomBytes(32))`;
  - Python (`posting-orders`, last-modified 31-10-2026 19:45 GMT): `"salt": str(int.from_bytes(secrets.token_bytes(32), "big"))`, cioè decimale;
  - OpenAPI: "Random 32-byte hex string".

  Il default dell'adapter (`signer/rest_adapter.py`) è `SALT_HEX32`. Si chiude solo con un POST su testnet.
- **Nonce:** l'ordine OBv3 **non** ha nonce: l'unicità viene dal `salt` (VERIFIED: la struct non contiene un nonce). Un nonce esiste solo nel permit EIP-2612 del deposito (`transfer-to-proxy`).
- **Expiry** (`posting-orders`): "`expiry` is required and must be a unix epoch timestamp in **seconds** (not milliseconds) that is in the future. An `expiry` that falls inside the market's betting delay plus two seconds is rejected."

### 1.6 Invio, cancellazione e settlement
- **Invio:** `POST /orders-v3`, lo stesso contratto su entrambe le reti (l'OpenAPI elenca Toronto tra i `servers`: "Testnet (Toronto, chainId 79479957)"). I dettagli sono in `signer/rest_adapter.py`.
- **Cancellazione** (`developers/cancelling-orders`, last-modified 31-10-2026 19:45 GMT): "All four return `200` even when nothing was cancelled".
  - "Cancel by id is synchronous. The route waits up to 5 seconds ... plus a third bucket, `unconfirmed`".
  - market, event e all sono asincroni: "nothing in the response confirms the orders are gone".
- **Settlement e fee** (`developers/fees`): "Fees on v3 are set **per account**", con payout fee "on **profit** ... at **settlement**, and only on a bet that **won**". Il settlement su Toronto (eventi reali o simulati, tempi) non è descritto: `DOCS_UNVERIFIED`.

### 1.7 Rate limit
`developers/rate-limits` (last-modified 31-10-2026 18:45 GMT) pubblica limiti per API key, per esempio:
- `POST /orders-v3` 5.500/60 s;
- `DELETE /orders-v3` 10.000/60 s;
- `DELETE /orders-v3/all` 60/60 s;
- `GET /orders-v3/{orderId}` 1.200/60 s;
- `GET /fills-v3` 300/60 s.

Per gli endpoint pubblici:
- `GET /metadata/obv3` 500/60 s per IP;
- famiglia markets/fixture/leagues/sports 500/60 s;
- un bucket globale non autenticato di 35.000/10 min.

La pagina **non distingue testnet e mainnet**: i limiti di Toronto sono `DOCS_UNVERIFIED`. Indicazione ufficiale: "read the limit in force from the headers rather than hardcoding a value" (`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Retry-After`). Il bridge resta comunque sotto il coordinatore condiviso con il suo `SXBET_MAX_RPM` (§2.4).

### 1.8 Differenze tra Toronto e mainnet
| Aspetto | Esito |
|---|---|
| Chain, host, contratti, API key, proxy | diversi (tabella §1.2), VERIFIED |
| Minimo d'ordine | runtime 1 USDC su entrambe; la pagina dice 5 per mainnet (conflitto, vale il runtime) |
| `bettingDelay` | uguale, tranne la lega 1431 (10.000 ms) presente solo su Toronto |
| Fee | per account (`GET /user/fees-v3`); su Toronto non lette (servirebbe la chiave): BLOCKED_EXTERNAL |
| Disponibilità dei mercati | "mirrors mainnet" è un'affermazione generica; liquidità e copertura su Toronto non verificate (nessuna chiamata ai mercati): `NOT_TESTED` |

## 2. Ambiente testnet separato (preparato in questa PR, senza rete)

- `docs/sxbet/toronto.env.example`:
  - solo nomi di variabili, segreti vuoti;
  - `SXBET_NETWORK_PROFILE=toronto` e `SXBET_NETWORK=testnet`;
  - nomi distinti `SXBET_TORONTO_API_KEY`, `SXBET_TORONTO_SIGNER_DATA_DIR` e `SXBET_TORONTO_DATABASE_URL`;
  - allowlist esplicita `SXBET_TORONTO_ALLOWED_HOSTS=api.toronto.sx.bet,realtime.toronto.sx.bet`;
  - `SXBET_TORONTO_SIGNER_KEY_FILE` (percorso del file della chiave testnet, mai la chiave) e `SXBET_TORONTO_SIGNER_ADDRESS` (indirizzo pubblico dell'EOA testnet, fissato dall'owner).

  Il repo non ha una directory `config/`: l'unica convenzione è `.env.example` alla radice, quindi il template sta in `docs/` e oggi nessun codice lo legge.
- `signer/network_profile.py`: profili `mainnet` e `toronto` fissati, senza codice di rete. Le funzioni sono:
  - `check_intent`;
  - `check_signer_config`;
  - `check_metadata` (sul `data` di `/metadata/obv3` letto altrove);
  - `check_url`;
  - `parse_allowed_hosts`;
  - `check_toronto_env`;
  - `check_testnet_signer_address`.

  Un profilo Toronto rifiuta, con codici fissi `PROFILE_*_OF_OTHER_NETWORK`, chain id, escrow, USDC, proxy executor e host di mainnet, e viceversa. Valori sconosciuti a entrambe le reti danno `PROFILE_*_UNEXPECTED`. Ogni host accetta un solo schema: `https` per API, RPC ed explorer, `wss` per il realtime (`PROFILE_URL_SCHEME_NOT_ALLOWED`).

Un ambiente Toronto viene rifiutato se contiene:
- `SXBET_API_KEY`, `DATABASE_URL`, `RENDER` o `SXBET_ENABLED`;
- `SXBET_LIVE_TRADING_FLAG`, `SXBET_SIGNER_PRIVATE_KEY` o `SXBET_SIGNER_RISK_CONFIG`, i nomi letti dal signer di produzione;
- una variabile `SXBET_MAINNET_*`.

`SXBET_NETWORK` diverso da `testnet` dà `PROFILE_NETWORK_OF_OTHER_NETWORK` se vale `mainnet` e `PROFILE_NETWORK_UNEXPECTED` per ogni altro valore.

**Requisito per il futuro signer testnet:**
- legge la chiave solo dal file `SXBET_TORONTO_SIGNER_KEY_FILE`, mai da `SXBET_SIGNER_PRIVATE_KEY`; il caricamento è una PR separata con merge dell'owner;
- prima di firmare verifica con `check_testnet_signer_address(derivato, fissato)` che l'indirizzo derivato dalla chiave coincida con `SXBET_TORONTO_SIGNER_ADDRESS`, fissato dall'owner;
- se gli indirizzi differiscono, o se uno dei due è un contratto noto, si ferma.

`check_testnet_signer_address` resta una funzione pura: non vede chiavi e non fa rete.
- Test: `tests/test_sxbet_network_profile.py`, offline.

Nessun sender HTTP è stato aggiunto. `LIVE_TRADING` resta `UNAVAILABLE`.

## 3. Blocchi

| Blocco | Stato | Azione dell'owner |
|---|---|---|
| Account Toronto con EOA (MetaMask/Rabby) | BLOCKED_EXTERNAL | registrarsi su toronto.sx.bet |
| USDC testnet | BLOCKED_EXTERNAL | chiedere al supporto chat di sx.bet (nessun faucet documentato) |
| API key Toronto | BLOCKED_EXTERNAL | generarla su toronto.sx.bet; consegnarla solo all'ambiente testnet (mai su Render di produzione né in GitHub) |
| Proxy Toronto deployato e finanziato | BLOCKED_EXTERNAL | dalla UI di toronto.sx.bet |
| Chiave di firma testnet nel signer testnet | BLOCKED_EXTERNAL | file fornito fuori banda dall'owner al solo host del signer testnet (`SXBET_TORONTO_SIGNER_KEY_FILE`), più l'indirizzo pubblico fissato (`SXBET_TORONTO_SIGNER_ADDRESS`) |
| DB testnet separato | BLOCKED_EXTERNAL | progetto o branch Neon dedicato, se serve il ledger PostgreSQL |
| Sender HTTP verso Toronto | BLOCKED (governance) | PR separata, review e merge dell'owner (§2.6) |
| Autorizzazione al collaudo con ordini testnet | BLOCKED (owner) | autorizzazione esplicita, scritta, per ogni passo del §4 |

## 4. Piano di collaudo futuro (nessun ordine senza autorizzazione esplicita dell'owner)

Prerequisiti:
- blocchi del §3 risolti;
- sender Toronto mergiato dall'owner;
- `check_toronto_env` verde;
- `check_metadata("toronto", …)` verde sulla lettura runtime;
- config di rischio testnet firmata dall'owner con stake = minimo runtime.

Ogni ordine passa dal flusso di approvazione Ed25519 del signer, con un'approvazione per ordine. I passi sono sequenziali, ciascuno con il proprio esito registrato (append-only) e annotato nella #130.

1. **Probe in sola lettura.** `GET /metadata/obv3` passando dal coordinatore, poi `check_metadata`. Con la chiave: `GET /user/proxy` (`deployed: true`), `GET /user/balance-v3` e `GET /user/fees-v3`.
2. **Ordine minimo e cancel.** Un ordine GTC `postOnly` di `orderSizeMinimumBaseUnits`, a quota lontana dal book, su un mercato prematch abbinato in modo esatto (§2.3bis).
   - Atteso `SUBMITTED` e poi `ACTIVE`.
   - Poi `DELETE /orders-v3` per id: atteso `cancelled`; `unconfirmed` va riletto.
   - Verifica finale con `GET /orders-v3/{orderId}`: `INACTIVE` / `USER_REQUESTED`.
3. **Formato del salt.** Due ordini minimi (con cancel) che differiscono solo per la serializzazione del salt: `SALT_HEX32` e poi `SALT_DECIMAL`. Registrare per ciascuno se viene accettato e se l'`orderId` restituito è uguale al digest locale. Chiude il DOCS_UNVERIFIED del §1.5.
4. **Re-invio dopo timeout.** Si simula un `SenderTimeout` lato client dopo l'invio.
   - Riconciliare con `GET /orders-v3/client/{clientOrderId}` **prima** di ogni nuovo invio.
   - Re-inviare lo stesso ordine firmato, con lo stesso `clientOrderId`, solo se la lettura dà 404, e registrare la risposta: duplicato rifiutato o idempotente.
   - Mai un nuovo salt sullo stesso intento senza una nuova approvazione.
5. **Unità di `fillAmount`.** Un ordine IOC minimo che prende liquidità esistente; serve un'approvazione specifica, perché crea esposizione testnet. Confrontare `outcome.fillAmount` (con `waitForOutcome`) con `GET /fills-v3` e con `remainingSize`, per stabilire se le unità sono le stesse di `totalBetSize`. Fino ad allora l'adapter non usa `fillAmount` dalla risposta.
6. **Batch con 400.** Un batch di 2 ordini in cui uno è volutamente invalido, per esempio `ODDS_NOT_ON_LADDER`. Registrare se la risposta è 400 per tutto il batch o una riga `FAILED` per ordine. Fino ad allora l'adapter tratta un 400 su batch come `UNKNOWN` per tutti gli ordini.
7. **Chiusura.** `DELETE /orders-v3` per id su ogni ordine rimasto, verifica di zero ordini aperti e report alla #130 (VERIFIED/ASSUMED).

Fuori dal collaudo, finché non c'è una decisione dell'owner: deposito o prelievo programmatico, heartbeat, cancel-all, mainnet.
