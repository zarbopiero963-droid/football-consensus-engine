# Signer isolato SX Bet

Componente separato dal server MCP (governance §2.6). **`LIVE_TRADING` resta `UNAVAILABLE`**: questo package firma, ma non invia nulla. Non contiene codice HTTP né un percorso di invio, e nessun flag lo abilita.

Contratto ufficiale (verificato il 01-11-2026):
- https://docs.sx.bet/api-reference/eip712-order-signing
- https://docs.sx.bet/developers/posting-orders
- https://docs.sx.bet/developers/odds-rounding

## Architettura

```
modello / client MCP ──► bridge (sxbet_simulate_order) ──► intent canonico + intent_hash
                                                             │   (nessuna chiave, nessuna firma)
                                                             ▼
                            dispositivo di approvazione dell'owner (chiave privata Ed25519)
                                                             │   firma (intent_hash, nonce, issued_at, expires_at, signer_id)
                                                             ▼
                                  signer/ (processo separato)
                                  • chiave EIP-712 solo in memoria (env SXBET_SIGNER_PRIVATE_KEY del suo processo)
                                  • chiave PUBBLICA Ed25519 dell'owner
                                  • rivalida l'intent, ricalcola l'hash, verifica l'approvazione,
                                    consuma il nonce, applica i limiti, firma
                                  • audit JSONL append-only con hash chain
                                                             │
                                                             ▼
                                  firma + order id restituiti al chiamante (NESSUN invio)
```

- **Il modello e il bridge non vedono mai la chiave.** `bridge/` non importa `signer` e non ha `eth-account` tra le dipendenze di runtime. Lo verificano `tests/test_sxbet_simulation.py::test_bridge_never_imports_signer` e `::test_signer_deps_not_in_runtime_requirements`. Il signer non importa `bridge`, `httpx`, `requests`, `urllib`, `http` né `socket` (`::test_signer_never_imports_bridge_or_network_libraries`).
- **Custodia della chiave.** Il signer la legge una sola volta, all'avvio, dalla propria variabile d'ambiente. Non la restituisce, non la scrive nei log né nell'audit, e `repr()` la nasconde. L'oggetto non è serializzabile. Una chiave mancante o malformata produce un errore con codice fisso, senza riportarne il valore.
- **Intent canonico.** Contiene tutti gli 8 campi firmati (`marketHash`, `baseToken`, `totalBetSize`, `percentageOdds`, `salt`, `expiry`, `maker`, `isMakerBettingOutcomeOne`) più il dominio EIP-712, `timeInForce` (inviato ma non firmato), la quota richiesta grezza e la chiave di idempotenza. `intent_hash` = `sha256` del JSON canonico (chiavi ordinate, separatori compatti, ASCII). Bridge e signer calcolano lo stesso hash con codice indipendente, e un test lo verifica.
- **"What you saw is what gets signed".** Il signer firma `eip712.message(intent)` ricavato dall'intent rivalidato, cioè dai parametri esatti coperti dall'hash approvato. Se cambia prezzo, importo, mercato o lato, cambia l'hash: `APPROVAL_INTENT_MISMATCH`.
- **Order id.** È il digest EIP-712 in minuscolo, `keccak(0x19 ‖ version ‖ domainSeparator ‖ structHash)`, come da documentazione. Si conosce prima dell'invio, quindi la riconciliazione per id è possibile.

## Perché un testo in chat non è una conferma

Un testo in chat, in un output di tool o in una pagina web può essere scritto da chiunque: dal modello stesso, da una prompt injection, da un terzo. Il signer accetta solo un oggetto `Approval` con una **firma Ed25519** valida della chiave di approvazione dell'owner, che il signer non possiede (conserva solo la chiave pubblica). Il modello non può produrre quella firma, per quanto sia convincente il testo. Inoltre l'approvazione:
- è legata a un singolo `intent_hash` e a un `signer_id`;
- è **monouso**: il nonce viene consumato e, con un percorso su file, resta consumato anche dopo un riavvio;
- ha una **scadenza breve**: `expires_at − issued_at ≤ 120 s`, con una tolleranza di clock di 5 s su `issued_at`.

## Altre garanzie

- **Limiti di esposizione** in `SignerConfig`, fissati dall'owner. Il default `0` rifiuta tutto (`EXPOSURE_LIMIT_ZERO`). L'esposizione impegnata è la somma degli importi firmati, ricostruita dall'audit al riavvio. È una stima conservativa: non scende quando un ordine viene cancellato.
- **Idempotenza.** Una chiave corrisponde a un solo intent. Stessa chiave con lo stesso hash: restituisce il risultato in cache e non produce una seconda firma. Stessa chiave con un hash diverso: rifiuto. Dopo un riavvio, una chiave già firmata è rifiutata (`..._RECONCILE_FIRST`) finché lo stato non è riconciliato. Più richieste concorrenti producono esattamente una firma (lock unico, verificato con 16 thread).
- **Configurazione di dominio.** Il signer accetta solo la rete, il `chainId`, il `verifyingContract` e il `baseToken` configurati dall'owner, e solo `maker` uguale al proprio indirizzo.
- **Audit.** JSONL append-only con `fsync` per ogni riga. Ogni riga contiene lo `sha256` della precedente, quindi una riga riscritta rompe la catena e il signer rifiuta di avviarsi. Campi ammessi (allowlist): decisione, motivo, chiave di idempotenza, `intent_hash`, `order_id`, importo, rete, prefisso di 6 caratteri del nonce. Non contiene mai la chiave né i byte della firma.

## Dipendenze

`signer/requirements.txt` (`eth-account` 0.14.x, `cryptography`). **Non** sono in `requirements.txt`: il runtime Render del bridge non riceve librerie di firma. La CI (`multi-market-tests.yml`, job `pytest`) le installa solo per eseguire i test.

## Cosa manca prima di `LIVE_TRADING` (decisioni owner)

1. **Dispositivo o app di approvazione dell'owner** che mostri i parametri leggibili dell'intent (evento, mercato, lato, importo, quota, esposizione massima) e firmi con Ed25519 il messaggio canonico (`signer/approval.py::approval_message`). La chiave privata di approvazione resta solo su quel dispositivo.
2. **Deploy del signer come servizio separato**, con il proprio segreto, fuori da Render MCP e fuori da GitHub secrets, e con un canale autenticato verso il chiamante. Il nonce store e l'audit vanno su storage durevole.
3. **Percorso di invio** (`POST /orders-v3`) e di riconciliazione (`GET /orders-v3/{orderId}`, `GET /fills-v3?orderId=`) in una PR separata, **revisionata e mergiata dall'owner**, che usi la macchina a stati di `bridge/sxbet/simulation.py::SubmissionTracker`: `UNKNOWN_AFTER_TIMEOUT` e `AMBIGUOUS_RESPONSE` si riconciliano prima di ogni retry, e un retry usa la stessa chiave di idempotenza. Va deciso anche se usare `waitForOutcome`.
4. **Proxy wallet**: `POST /user/deploy-proxy` e il funding (`/user/transfer-to-proxy`, permit EIP-2612) sono operazioni finanziarie del §2.6, con conferma propria. Senza proxy, ogni ordine è rifiutato con `400 PROXY_NOT_DEPLOYED`.
5. **Limiti di esposizione** reali e scope della chiave API (`Orders: create`), scelti dall'owner. Va anche verificata l'idoneità del servizio nella giurisdizione applicabile.
6. Elementi **DOCS_UNVERIFIED**: il comportamento dell'exchange quando si reinvia un ordine con lo stesso id o lo stesso `clientOrderId` dopo un timeout. La documentazione dice solo che `clientOrderId` deve essere unico per indirizzo.
