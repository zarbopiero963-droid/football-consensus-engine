# FCB — Audit indipendente finale dei gap (2026-11-09)

- **Mandato:** `FCB-OWNER-FULL-MCP-COMPLETION-AUDIT-20261109`
- **Auditor:** Claude (indipendente da Codex)
- **Commit auditato:** `main` = `a8fb2289cb06284f5426d4cd64b6064be57f617c` (#175). Il deploy Render live è sullo stesso SHA.
- **Metodo:**
  - solo lettura;
  - sorgente esportato con `git archive a8fb228`;
  - test offline e integrazione su PostgreSQL 16 locale usa e getta;
  - probe live limitati a discovery, `/healthz`, `POST /mcp` senza token, log Render, query Aiven read-only (solo conteggi) e metadati GitHub Actions.
- **Cosa non è stato fatto:**
  - nessuna scrittura in produzione;
  - nessuna chiamata a provider che consumi quota;
  - nessuna modifica applicativa.
- **Allegati:**
  - `FCB_FULL_MCP_COMPLETION_MATRIX.md` / `.csv`: matrice;
  - `FCB_TOOL_INVENTORY_a8fb228.csv`: 122 righe, una per tool;
  - `areas/A…F`: rapporti di area con riproduzioni;
  - `areas/PHASE0.md`: fatti di produzione.

> Regole applicate:
> - prova storica ≠ certificazione sullo SHA distribuito;
> - xfail e skip ≠ PASS;
> - tool registrato ≠ tool funzionante;
> - PR mergiata ≠ WP chiusa.

---

## 1. Fase Zero — realtà al 2026-11-09 (VERIFIED)

| Elemento | Stato osservato |
|---|---|
| `main` / PR aperte | `a8fb228`; 0 PR aperte. Ultime PR mergiate: #172, #173, #174, #175 |
| Render | live su `a8fb2289cb06`; **piano free** (spin-down, istanze riavviate spesso); `healthCheckPath` vuoto |
| `/healthz` | `ok`, **`schema_state=READY`**, 122 tool (browser 34, apifootball 42, sxbet 46), `browser_tools_enabled=false`, `cloak_enabled=false`, coordinatore SX `POSTGRES_SHARED` |
| WP-005 | **apply eseguito**: run [42399341274](https://github.com/zarbopiero963-droid/football-consensus-engine/actions/runs/42399341274), 2026-11-07 18:03 UTC, attore owner, `mode=apply`. Lo step di restore ha superato le asserzioni interne: due esecuzioni (la seconda no-op), conteggi 33/4/9/60, checksum uguali ai file, stato finale `BRIDGE_0001_0008`. **Su #130 non risulta registrato**: l'ultimo commento, delle 17:55, dice "riconferma backup pending" |
| Database `defaultdb` (conteggi) | 33 tabelle, migration bridge 1–8, serie signer assente, **tutte le tabelle di proprietà di `avnadmin`** |
| Ruoli PostgreSQL | **Unico utente `avnadmin`**; `fcb_migrator`, `fcb_app` e `fcb_signer` non esistono |
| Uso del MCP | **0 righe** in `oauth_access_tokens`, `oauth_refresh_tokens`, `oauth_authorization_codes` e `oauth_claude_dcr_clients`. **0 righe** in `api_football_quota_counters` |
| Log Render dal 2026-11-07 18:00 | **Zero errori o warning**. Ogni `POST /mcp` riceve 401. **Nessuna** richiesta a `/oauth/authorize`, `/oauth/token` o `/oauth/register` |
| Aiven `piero-1` | RUNNING, PG 18.6, 1 GB, `max_connections` 15. Backup giornalieri, ultimo 2026-11-08 21:11. **`termination_protection=false`**, **`ip_filter 0.0.0.0/0`** |

**Conclusione Fase Zero.** Lo schema è inizializzato e sano, ma dal 7 novembre nessun client si è autenticato e nessun tool è stato chiamato in produzione. La verifica post-apply richiesta nell'approvazione owner (OAuth, `initialize`, `tools/list`, chiamate read-only) **non è stata eseguita**.

---

## 2. Verdetti

### VERDETTO A — MCP CORE: **PARTIAL**

- **Dimostrato:**
  - schema `READY` in produzione;
  - discovery OAuth e risposta 401 osservate dal vivo;
  - flusso completo (DCR/CIMD → authorize → token → `initialize` → `tools/list` → `tools/call`, rotazione refresh) eseguito con successo sul codice di `a8fb228` contro un PostgreSQL reale locale;
  - nessun errore nei log dopo l'apply.
- **Cosa impedisce il PASS:**
  1. **Nessuna prova con client reale in produzione**, il P0. Il connettore va ri-autorizzato dall'owner.
  2. Brute force sulla password di consenso, P1 (A-05).
  3. Errori DB che diventano 500 grezzi, con I/O DB sincrono e senza timeout sull'event loop, P1 (A-06, A-07).
  4. Stabilità non provata sul piano free (A-08).

### VERDETTO B — MCP NEL PERIMETRO AUTORIZZATO: **PARTIAL**

- **Perimetro:** OAuth, API-FOOTBALL read (D04), SX Bet read-only e simulazione, browser spento (D05).
- **Dimostrato:**
  - parità 142/142 parametri;
  - suite offline verdi: circa 1.578 test, 111 di integrazione su PG16;
  - nessun percorso verso il trading reale.
- **Cosa impedisce il PASS:**
  - **0 tool su 122 certificati sullo SHA distribuito**: le prove reali sono tutte precedenti al 7 novembre e fuori dal coordinatore;
  - P1 aperti: B-03 (codici di errore API-FOOTBALL), B-13 (secret avnadmin esposto ai branch), B-14 (TLS verify-full del runtime non dimostrato), B-15 (ruoli assenti), B-17 (recovery mai provata).

### VERDETTO C — ROADMAP TOTALE: **FAIL (non completata)**

- **Stato delle WP:** 12 work package su 25 sono NOT_STARTED, 7 PARTIAL e 1 BLOCKED (legale).
- **Browser:** i residui SSRF sono aperti e richiedono il proxy WP-024.
- **Signer:** è in **conflitto** con la decisione owner del 2026-11-06, come spiegato sotto.

**Il conflitto sul signer (C-02):**
- **Codice e CLAUDE.md/AGENTS.md §2.6:** richiedono una conferma per ogni ordine e un flag LIVE da rinnovare entro 24 ore.
- **Decisione owner del 2026-11-06:** il signer deve essere autonomo, senza passkey né approvazione per singolo ordine.

Il completamento tecnico del signer è distinto dall'abilitazione operativa, che resta BLOCKED (D12, legale).

---

## 3. Tool (ricontati dal registro reale su `a8fb228`)

**122 = browser 34 + API-FOOTBALL 42 + SX Bet 46.** Corrisponde a `/healthz`. Dettaglio per tool in `FCB_TOOL_INVENTORY_a8fb228.csv`.

| Misura | Conteggio | Criterio |
|---|---|---|
| Certificati sullo SHA distribuito con client reale (D13) | **0 / 122** | nessuna chiamata autenticata dal 2026-11-07 |
| Con prova reale **storica** | 77 (API-FOOTBALL 40, SX Bet 37) | API-FOOTBALL: run 2026-10-25/31 a livello di rotta, script httpx fuori MCP e fuori coordinatore, 0/142 parametri provati dal vivo. SX Bet: run 41320538088 del 2026-11-01, MCP in Actions con coordinatore locale; 10 tool solo con dati vuoti, 4 solo sul percorso 404 |
| Solo offline | 14 (browser 7, API-FOOTBALL 2, SX Bet 5) | test del repo |
| Simulati | **4** (`sxbet_simulate_order`, `hedge_simulation`, `close_position_preview`, `rules_evaluate`) | nessun invio |
| Disabilitati per policy | **34 browser**: 26 bloccati dal kill switch; 8 di ciclo di vita (stop, reap, list, status, health…) chiamabili ma inerti, perché nessuna sessione si può creare | D05 |
| Implementati non verificati | 1 (`browser_stop_all`) | — |
| Difettosi (difetto riprodotto) | **API-FOOTBALL 40** (errori collassati, B-03); **`browser_stop_all`** (`str(exc)` al client); **tutti i 122** (codici fissi del bridge mascherati dall'SDK, B-11) | nessun tool "rotto" nel senso di risultato sbagliato su input valido |
| Senza alcun test che chiami il tool | 18 (browser 11, SX Bet 7) | traccia con profiler (B_tools.md D5) |
| Possono scrivere o muovere fondi | **0** | client SX Bet solo GET; `signer/` non importato dal bridge |

---

## 4. Difetti consolidati

NEW significa assente dal registro WP e da #130 prima di questo audit.

| ID | Sev. | NEW/KNOWN | Difetto | Prova |
|---|---|---|---|---|
| G-01 | **P0** | NEW (stato) | WP-005 non verificato dopo l'apply e non registrato su #130. Il registro dice ancora "PRODUCTION NOT RESTORED" | §1; F_db_wp.md P0-1 |
| G-02 | **P1** | **NEW** | `FCB_PG18_TEST_URL` è consumato da un job senza environment, quindi è un secret di repository. Contiene le credenziali `avnadmin`, l'unico utente, che raggiungono `defaultdb` di produzione. Qualunque branch o PR dello stesso repo può leggerlo, aggirando tutti i gate del workflow di ripristino | `.github/workflows/fcb-aiven-pg18-test.yml:22-45`, verificato dall'auditor capo; E F-01 |
| G-03 | **P1** | KNOWN (WP-022) | Password di consenso senza throttling: 60 tentativi in 0,19 s. Viene verificata prima del client | `bridge/oauth.py:331-334`; A F-P1-1 |
| G-04 | **P1** | KNOWN (WP-007), con aggravante NEW | Errori DB OAuth → 500 grezzo. Accesso DB sincrono sull'event loop senza `connect_timeout` (default psycopg 130 s): un Bearer casuale blocca anche `/healthz` | A F-P1-2; E F-04; F P1-1 |
| G-05 | **P1** | KNOWN (WP-008) | TLS verify-full del runtime opt-in e non dimostrato in produzione | `app/db.py`; F P1-2 |
| G-06 | **P1** | KNOWN (WP-009) + NEW (Aiven) | Ruoli assenti, runtime presumibilmente come `avnadmin`, `ip_filter` aperto, termination protection spenta, nessun workflow per i ruoli | §1; F P1-3 |
| G-07 | **P1** | NEW (nessuna WP) | Recovery mai provata: niente drill, niente dump logico, RPO/RTO non definiti | F P1-4 |
| G-08 | **P1** | KNOWN (WP-016) | API-FOOTBALL: 401, 429, 5xx e breaker aperto collassati in `PROVIDER_REQUEST_FAILED` | `bridge/api_football/tools.py:170-180`; C F-01 |
| G-09 | **P1** | NEW (governance) | Signer in conflitto con la decisione di autonomia (§2.6 e codice). Con l'autonomia, rischio non autenticato e limiti di perdita insufficienti (R3: 3 ordini da 5 USDC firmati con limite giornaliero 5) | D F-0, F-1, F-2 |
| G-10 | P1 alla riattivazione | KNOWN (WP-010), ora riprodotto | L'URL `wss://` del CDP finisce nei log tramite l'SDK (viola §2.1). Oggi latente: il kill switch impedisce la connessione | E F-06 |
| G-11 | P2 | **NEW** | Il kill switch del browser fallisce aperto: valore vuoto o non riconosciuto → tool abilitati | `bridge/browser_use.py:91-93`; E F-03 |
| G-12 | P2 | **NEW** | `/oauth/register` senza autenticazione né limiti: una riga con TTL di 730 giorni per ogni chiamata | A F-P2-3; E F-05 |
| G-13 | P2 | KNOWN (WP-022) | Nessun rilevamento di riuso del refresh, nessuna revoca, rotazione con nuovi 30 giorni a ogni rinnovo (sessione infinita) | A F-P2-1/2 |
| G-14 | P2 | NEW | SDK `mcp[cli]>=2,<3` non pinnato: il mascheramento degli errori dipende dalla versione. I codici fissi del bridge non arrivano al client | A F-P2-5; E F-07 |
| G-15 | P2 | NEW (da verificare) | `/mcp` → 307 verso `http://…/mcp/` in locale; in produzione è da verificare col primo client reale | A F-P2-6 |
| G-16 | P2 | KNOWN (WP-018) | Un solo scope (`browser:use`) per tutti i 122 tool; nessun subject | A F-P2-7 |
| G-17 | P2 | NEW | Nessun pool di connessioni né cap rispetto a `max_connections=15` | F P2-1 |
| G-18 | P2 | NEW | L'unico test OAuth su PostgreSQL è sempre saltato in CI (`DATABASE_URL` contro `TEST_DATABASE_URL`) | F §0 |
| G-19 | P2 | NEW | API-FOOTBALL: contabilità `network_calls_made` errata; esaurimento quota segnalato in body 200 non riconciliato; manifest odds senza id | C F-02, F-04, F-05 |
| G-20 | P2 | KNOWN (WP-012) + NEW (R2) | Ledger del signer: stati bloccati R1, R7 (fix su branch non mergiato) e R2 (nuovo) | D F-3, F-4, F-5 |
| G-21 | P2 | NEW | Render free e `healthCheckPath` vuoto: `/healthz` risponde sempre `ok:true` e nessun allarme | F P2-4/5 |
| G-22 | P2 | NEW (da verificare) | Environment `api-football-live`: restrizione al solo `main` non documentata | E F-02 |
| G-23 | P3 | vari | PKCE debole, scope arbitrari, schema Bearer case-sensitive, niente `no-store`, niente anti-framing, `Session` con repr che include `cdp_url`, azioni non pinnate per SHA, dipendenze senza hash, pytest nel runtime (PYSEC-2026-1845), 6to4 192.88.99/24, documentazione obsoleta | A/E/F |

**Verificato pulito:**
- nessun segreto nell'albero o nei 794 commit;
- nessun `pull_request_target`;
- SQL parametrizzato;
- download CloakBrowser firmato;
- nessuna fuga di token nei log;
- nessun bypass di LIVE;
- nessun float sul denaro;
- nessun SSRF via CIMD.

---

## 5. Work package

Tabella completa in matrice e in `areas/F_db_wp.md` §3.

- **Chiudibili oggi con prove sufficienti:**
  - **WP-004:** migration in produzione.
  - **WP-006:** readiness in produzione.
  - **WP-011:** archivio evidenza, prova del 2026-11-01.
- **Non chiudibili e motivo:**
  - WP-001 è DONE_TESTED, ma il browser non è mai stato esercitato dal vivo.
  - WP-005 è RESTORED_NOT_VERIFIED.
  - WP-008 e WP-009 aspettano la configurazione owner.
  - WP-003 non ha una run reale sul coordinatore.
  - WP-002, WP-002b e WP-019 sono PARTIAL.
  - WP-007, 010, 013–018, 020–022 e 024 sono NOT_STARTED. WP-013 ha il testo obsoleto sulla passkey.
  - WP-012 è PARTIAL.
  - WP-023 è BLOCKED.
- **Esigenze non coperte da nessuna WP:**
  - G-02;
  - pool, timeout e I/O non bloccante (oltre WP-007);
  - Render health check e allarmi;
  - protezione Aiven;
  - backup e DR;
  - workflow ruoli;
  - checklist post-apply WP-005;
  - tabelle legacy nello schema;
  - CI su push a `main`;
  - nuova WP per il signer autonomo.

---

## 6. Confronto indipendente con le attestazioni precedenti (Codex e #130)

1. "WP-005 DONE_TESTED / PRODUCTION_NOT_RESTORED" (6705557169): oggi l'apply **c'è stato**, ma il criterio di chiusura WP-005 ("OAuth verificato da client reale") **non è soddisfatto**. Il registro è obsoleto.
2. L'apply del 7 novembre e la riconferma del backup che doveva precederlo **non sono documentati** su #130.
3. "pytest: SUCCESS" include un test OAuth su PostgreSQL **sempre saltato** (G-18).
4. "WP-009: test di readiness, DCR e coordinatori via `fcb_app`": vero, ma non copre token OAuth né lease del browser. In produzione i ruoli non esistono.
5. "D02 applicato prima dell'apply" vale **solo per il workflow**, non per il runtime (G-05).
6. "42/42 tool API-FOOTBALL testati con mock" **non si riproduce**: il test a catalogo copre 22 tool.
7. WP-013 chiede ancora la "revoca passkey", contro la direttiva del 2026-11-06.
8. WP-019 risulta "non iniziata", ma metà esiste (job PG18).
9. L'intestazione del registro ("main=7f9ee53", "MCP non sano") è obsoleta.

**Non verificato indipendentemente da me:**
- harness PG18 e TLS (solo prove CI dichiarate: assenza di Docker locale);
- variabili d'ambiente Render;
- scope dei secret e regole degli environment GitHub (API 403 dal proxy);
- documentazione ufficiale API-FOOTBALL (Cloudflare 403);
- Browser Use cloud reale.

---

## 7. Roadmap eseguibile

Vale la regola di **una PR applicativa alla volta**. Le azioni owner e le PR di sola documentazione possono procedere in parallelo.

### P0 — MCP operativo

| # | Attività | Esecutore | Accettazione | Gate |
|---|---|---|---|---|
| P0-1 | Ri-autorizzare i connettori Claude.ai e ChatGPT, con login owner | **Owner** | Su `oauth_access_tokens` e `oauth_refresh_tokens` +1 ciascuno; nei log `/oauth/token` 200 | owner |
| P0-2 | Verifica post-apply WP-005: E2E client reale (A_mcp_oauth.md §4: `initialize`, `tools/list`=122, una chiamata a costo zero, refresh, restart, negativi) e registrazione dell'apply su #130 | verificatore indipendente + Codex | Tutti i punti con evidenza → WP-005 PRODUCTION_VERIFIED | — |
| P0-3 | G-02: spostare `FCB_PG18_TEST_URL` in un environment protetto solo `main` (workflow) e ruotare la password `avnadmin` | Codex (PR workflow) + **Owner** (secret e rotazione) | Il job referenzia l'environment; un test statico rifiuta secret DB fuori dagli environment protetti; password ruotata e Render aggiornato | owner (segreti) |

### P1 — MCP stabile e certificabile

| # | Attività | Accettazione | Gate |
|---|---|---|---|
| P1-1 | **WP-022a**: throttling della password di consenso (PostgreSQL), 429 fisso | Il tentativo n+1 nella finestra riceve 429 anche con la password giusta; test su PG | merge owner (D08) |
| P1-2 | **WP-007** più I/O non bloccante: 503 fissi su tutti gli endpoint OAuth, `connect_timeout`, `to_thread` o async, emissione dei token atomica, `tests/integration/test_oauth_store_postgres.py` su PG16 e PG18 | Criteri 1–7 in A_mcp_oauth.md §3; `/healthz` non bloccato con DB in stallo | merge owner (D08) |
| P1-3 | **WP-008/009 in produzione**: flag verify-full e CA su Render; workflow owner-gated per i ruoli, poi DSN `fcb_app`; termination protection attiva | `/healthz` READY con DSN `fcb_app` verify-full; `avnadmin` non usato dal runtime | owner (config e credenziali) |
| P1-4 | **Recovery**: decisione owner tra dump logico gratuito delle tabelle OAuth e quote (artefatto protetto) e drill a pagamento; RPO/RTO documentati | Restore provato su un DB usa e getta | owner (D07 se a pagamento) |
| P1-5 | **WP-016** più G-19: codici API-FOOTBALL distinti, contabilità corretta, riconciliazione della quota in body 200 | Riproduzioni di C F-01/02/05 da RED a GREEN | ordinaria |
| P1-6 | **WP-010** più G-11 e G-14: errori con codice fisso (`ToolError`), URL CDP fuori dai log, kill switch fail-closed, SDK `mcp` pinnato | Probe `p_cdp.py` pulito; `BROWSER_TOOLS_ENABLED=disabled` → spento | ordinaria |
| P1-7 | **WP-015**: certificazione dei 122 tool su SHA distribuito, inclusi il piano API-FOOTBALL da 16 chiamate (C §4) e la run SX Bet read-only su `main` (refresh WP-011) | Matrice per tool PASS/FAIL/BLOCKED/NOT_TESTED su SHA esatto; contatori di produzione coerenti | owner (dispatch e budget) |
| P1-8 | **WP-019**: workflow post-deploy (SHA, `/healthz`, 401, numero tool) e CI su push a `main`; dipendenze pinnate | Run verde dopo il merge | ordinaria |

### P2 — Copertura completa del perimetro

- **WP-022 (resto):** revoca RFC 7009, famiglie di refresh, durata assoluta, limite su DCR.
- **WP-018:** subject; richiede prima la decisione owner sul subject.
- **WP-014:** fonte unica per lo stato della documentazione API-FOOTBALL.
- **WP-020:** Europe/Rome.
- **Manifest API-FOOTBALL** con identificativi (C F-03/F-04).
- **WP-017:** test per i 18 tool senza chiamata, cap su HTML e screenshot, `stop_all` con codice fisso.
- **WP-021:** rimozione dei moduli fuori scope; per le tabelle legacy decide l'owner.
- **G-23:** voci P3.
- **Decisione owner sul piano Render:** costo.

### P3 — Roadmap avanzata

- **Browser:**
  - prototipo WP-024 a costo zero (D06);
  - chiusura dei residui WP-002b;
  - riattivazione solo con decisione owner.
- **Signer:**
  1. Owner: emendamento di CLAUDE.md/AGENTS.md §2.6 per il signer autonomo (file di governance, merge owner).
  2. Nuova WP "signer autonomo": custodia isolata o KMS, provenienza autenticata dei dati di rischio, limiti che includono l'esposizione abbinata (R3), kill switch, riconciliazione continua.
  3. WP-012: R1, R2, R7, audit log append-only.
  4. WP-013 ri-scopata senza passkey.
  5. Rimozione dei riferimenti passkey (D §6).
  6. WP-023 resta bloccata per motivi legali.
- **Trading reale:** sempre OFF (D12).

---

## 8. Risposte alle 18 domande dell'owner

1. **Il MCP funziona oggi realmente?** Il server è attivo, lo schema è pronto e discovery e 401 sono corretti. Il funzionamento autenticato è **non dimostrato in produzione**: dopo l'inizializzazione non c'è stata nessuna sessione OAuth né chiamata. Offline, su PostgreSQL reale, il flusso completo funziona.
2. **`defaultdb` è READY?** **Sì**, verificato: apply run 42399341274, `/healthz READY`, 33 tabelle e 8 migration.
3. **OAuth funziona end-to-end?** Sul codice sì, su PostgreSQL locale. **In produzione non è verificato**: 0 token, nessuna richiesta `/oauth/*`. In più ci sono due P1: brute force (G-03) ed errori DB (G-04).
4. **Quanti tool sono registrati?** **122**: browser 34, API-FOOTBALL 42, SX Bet 46.
5. **Quanti funzionano realmente?** **0 dimostrati sullo SHA distribuito.** 77 hanno prove reali storiche, anteriori al 7 novembre.
6. **Quanti sono solo simulati?** **4** (SX Bet).
7. **Quanti sono disabilitati?** **34** browser: 26 bloccati e 8 di ciclo di vita, inerti.
8. **Quanti sono difettosi?** **41** con difetto riprodotto: 40 API-FOOTBALL (errori collassati) e `browser_stop_all`. Tutti i 122 sono toccati da G-14 (codici fissi mascherati). Nessuno restituisce dati errati su input validi.
9. **Quali WP si possono chiudere oggi?** **WP-004, WP-006, WP-011.**
10. **Quali WP restano aperte e perché?** Le altre 22: vedi §5 e la matrice.
11. **Esistono P0/P1 non registrati?** **Sì:**
    - G-01, P0 (WP-005 non verificata né registrata);
    - G-02, P1 (secret `avnadmin` leggibile dai branch);
    - G-07, P1 (recovery mai provata);
    - G-09, P1 (conflitto di governance del signer);
    - aggravante di G-04 (event loop bloccato).
12. **Cosa blocca il MCP core?**
    - P0-1, la ri-autorizzazione dell'owner;
    - P0-2, l'E2E reale;
    - G-03 e G-04, perché il core sia certificabile.
13. **Cosa blocca la certificazione completa?** P1-1…P1-8, le voci P2, i residui del browser (WP-024) e il signer (governance, poi nuova WP, poi WP-012/013). WP-023 è legale.
14. **Cosa deve sviluppare Codex per primo?**
    1. PR workflow per G-02.
    2. WP-022a (throttling).
    3. WP-007 con I/O non bloccante.

    Prima e in parallelo: la verifica P0-2 subito dopo la ri-autorizzazione dell'owner.
15. **Cosa spetta esclusivamente all'owner?**
    - Ri-autorizzare i connettori (login).
    - Secret: spostamento di `FCB_PG18_TEST_URL`, rotazione `avnadmin`, configurazione TLS e CA su Render, credenziali dei ruoli.
    - Aiven: termination protection, `ip_filter`.
    - Verificare la regola di branch dell'environment `api-football-live`.
    - Dispatch dei workflow live.
    - Merge delle PR auth e signer.
    - Emendamento §2.6.
    - Decisione sul subject (WP-018).
    - Decisioni di costo.
    - Legale.
16. **Cosa richiede costi?**
    - Piano Render a pagamento: opzionale, contro lo spin-down.
    - Fork o drill Aiven: piccolo costo orario o mensile.
    - Hosting del proxy di egress (WP-024 in produzione) o di un container Cloak.
    - Tutto il resto è a costo zero.
17. **Cosa non è certificabile nel perimetro autorizzato?**
    - Browser Use cloud: OFF, egress non imponibile, vedi la proposta WP-024.
    - Cloak in produzione.
    - Trading SX Bet reale (D12) e signer operativo.
    - Testnet (legale).
    - Documentazione ufficiale API-FOOTBALL (Cloudflare): DOCS_UNVERIFIED finché non c'è una sessione browser autorizzata.
18. **Verdetto complessivo:** **A = PARTIAL, B = PARTIAL, C = FAIL (non completata).** Mancano: l'E2E con client reale sullo SHA distribuito, la chiusura dei P1 (G-02…G-08), la certificazione per tool (WP-015) con il coordinatore condiviso, recovery provata, ruoli e TLS del runtime in produzione e, per il livello C, browser (WP-024) e signer autonomo.

---
Audit indipendente: nessuna prova copiata da Codex è attestata come indipendente. Le affermazioni non verificate sono marcate come tali.
