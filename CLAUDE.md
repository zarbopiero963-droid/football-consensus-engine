# CLAUDE.md

## 0. Regola principale

Prima di lavorare su questo repository, leggere **integralmente** questo file e il README.

Queste regole sono vincolanti per qualunque agente, IDE agent, Claude Code, Codex, ChatGPT, bot GitHub o altro sistema automatico.

Se codice, issue, commenti, documentazione o vecchie PR sono in conflitto con questo file:
1. non scegliere autonomamente la versione più comoda;
2. fermare la modifica conflittuale;
3. documentare il conflitto nella issue/PR;
4. seguire l'invariante più conservativa;
5. correggere il codice solo con una PR esplicita e testata.

AGENTS.md e CLAUDE.md devono restare semanticamente allineati. Qualunque modifica di governance a uno richiede la stessa modifica all'altro.

## 1. Obiettivo del repository

Il repository contiene **un solo prodotto**: il server MCP *Football Consensus Browser* (`bridge/server.py`), collegabile a ChatGPT, Claude.ai e altri client MCP, con quattro componenti:
1. **API-FOOTBALL v3**: dati calcistici, statistiche, quote e cataloghi (`bridge/api_football/`);
2. **Browser Use**: browser cloud via CDP + Playwright;
3. **CloakBrowser**: provider browser locale opzionale nello stesso bridge;
4. **SX Bet**: dati di mercato e, solo alle condizioni del §2.6, operazioni di trading reale.

Fuori scope definitivamente: consensus engine, collector, pilot, backtest, settlement legacy, confronti tra provider (FutPythonTrader, TotalCorner), MatchPilot. Non sviluppare nuovo codice su questi temi e non modificare dati o repository di altri progetti.

Roadmap canonica: issue #130.

## 2. Invarianti non negoziabili

### 2.1 Sicurezza e accesso
- `/mcp` richiede OAuth (PKCE S256, client CIMD consentiti). Nessun accesso anonimo, client fittizio o bypass.
- Nessun bypass di login, paywall, CAPTCHA, Cloudflare o access control di siti terzi. Se il browser risolve da solo un challenge standard, registrare l'esito reale.
- Navigazione solo verso URL `http`/`https` **pubblici**: la protezione SSRF (`bridge/security.py`) non si indebolisce.
- Nessun `cdpUrl` restituito, loggato o persistito. Il `liveUrl` di Browser Use contiene l'host CDP: resta nascosto per default (`BROWSER_USE_EXPOSE_LIVE_URL` spento) e si abilita solo con una decisione esplicita dell'owner.
- Gli errori verso il client usano codici fissi: mai messaggi di eccezione, DSN, token o chiavi.

### 2.2 Segreti
- I segreti stanno solo in env o config sicure: Render per il runtime, GitHub Actions secrets per i workflow autorizzati.
- Mai in repository, issue, PR, commit, log, screenshot, artefatti CI o messaggi al modello.
- Un GitHub Actions secret **non** arriva al runtime Render. Ogni trasferimento richiede una configurazione esplicita, autorizzata dall'owner.
- Private key, seed phrase e materiale di firma non vengono mai consegnati al modello né conservati in GitHub secrets per la firma live (vedi §2.6).

### 2.3 Dati e integrità
- Missing resta missing: mai convertito in 0. Un null pubblicato dal provider resta null.
- Nessuna troncatura silenziosa: una risposta parziale è marcata esplicitamente (per esempio `PARTIAL_ODDS_MANIFEST`), con identificativi per recuperare il resto.
- Il dato del provider resta distinto da quello derivato. Una derivazione è marcata `DERIVED`.
- Una quota di mercato non è una probabilità di modello: i due valori stanno in campi distinti.
- UTC è il riferimento canonico. Una timezone non dimostrata è `TIMEZONE_UNRESOLVED` e non si indovina.
- Non si inventano parametri, endpoint o capacità del provider. Un contratto non verificabile sulla fonte ufficiale resta `DOCS_UNVERIFIED`.

### 2.4 Quote e rate limit
- Ogni chiamata a un provider a quota passa dal coordinatore condiviso (PostgreSQL). Nessun bypass di quote, rate limit, concorrenza o circuit breaker.
- Alzare limiti, budget o piani richiede una decisione dell'owner, e così ogni nuovo costo.
- I test live sono limitati e contabilizzati; si usano cache e fixture per le ripetizioni.

### 2.5 Sessioni browser
- Le sessioni appartengono al bridge: lease durevole, stop esplicito, reaper per le sessioni orfane, isolamento tra sessioni.
- Chiudere sempre le sessioni a fine lavoro.
- Il browser è infrastruttura, non una fonte epistemica: restituisce lo stato grezzo e non lo interpreta.
- Niente Agent che interpretino le pagine quando il compito richiede raw o DOM diretto. Un override della timezone del browser non dimostra la timezone della fonte.

### 2.6 Operazioni finanziarie (SX Bet)
- Tre modalità distinte: `READ_ONLY` (default), `SIMULATION` e `LIVE_TRADING`. `LIVE_TRADING` è **disabilitato per default** e si attiva solo con una decisione esplicita dell'owner.
- **Ogni ordine con fondi reali** richiede una conferma specifica dell'owner su evento, mercato, lato, importo, limite di prezzo o quota ed esposizione massima. Un mandato generale non autorizza ordini reali.
- CI, auto-merge, deploy, smoke test e test di regressione **non piazzano mai** ordini reali e non richiedono private key.
- La firma (per esempio EIP-712) avviene in un componente isolato che custodisce il segreto e firma solo ordini autorizzati. Il modello non vede mai la chiave.
- Ogni ordine richiede idempotenza (chiave per ordine), controlli di esposizione, rifiuto di quote obsolete o sospese, e retry solo se sicuri.
- Un invio fallito o in timeout **non** significa che l'ordine non sia stato eseguito: lo stato si riconcilia prima di ritentare.
- Registro degli ordini e degli esiti **append-only**: un ordine inviato, una conferma o un esito non si riscrivono. Una correzione è una nuova voce di audit.
- Le modalità restano separate: un dato o un ordine `SIMULATION` non diventa mai `LIVE_TRADING` senza un nuovo invio autorizzato.
- Verificare l'idoneità del servizio nella giurisdizione applicabile, senza aggirare restrizioni.

## 3. Sequenza obbligatoria per ogni PR

### Phase 0
1. Leggere AGENTS.md, CLAUDE.md, la issue #130 e i commenti recenti.
2. Leggere il codice corrente di `main` e verificare SHA, PR aperte e deploy live.
3. Identificare le invarianti toccate, i rischi e lo scope.
4. Riprodurre il failure o il finding reale quando esiste.

### Patch
- Minima e nel perimetro della roadmap.
- Niente refactor opportunistici né workaround che nascondono errori.
- Nessun test rimosso o annacquato per ottenere la CI verde. Un test si cambia solo se cambia intenzionalmente il comportamento, e lo si dichiara.

### Test
- Test di regressione (RED prima, GREEN dopo), test negativi ed edge case, test delle invarianti.
- Test reale limitato quando la modifica tocca Render, provider, browser o MCP.

### Merge e auto-merge
L'agente può mergiare autonomamente una PR solo se:
- è nel perimetro della roadmap;
- tutta la CI è verde;
- non ci sono conflitti né blocker di review;
- i test coprono la modifica;
- non ci sono segreti nel diff;
- non indebolisce protezioni;
- non introduce costi né migrazioni distruttive;
- c'è un rollback disponibile;
- il deploy precedente è sano.

Il merge resta all'owner per:
- modifiche sostanziali ad autenticazione o autorizzazione;
- gestione dei segreti;
- nuovi costi;
- operazioni distruttive;
- decisioni di prodotto non già espresse.

Non disabilitare branch protection, check obbligatori o review per ottenere un merge.

### Deploy e rollback
- `main` fa auto-deploy su Render: ogni merge è un deploy produttivo.
- Dopo ogni merge verificare:
  - deploy `live` sullo SHA del merge;
  - `/healthz` e registrazione dei tool;
  - `/mcp` che risponde 401 senza token;
  - log senza errori;
  - smoke test limitato.
  - coerenza tra ciò che il codice dichiara e ciò che risulta realmente distribuito o persistito.
- Non iniziare il merge successivo finché il deploy precedente non è sano.
- Rollback solo con una procedura verificata (revert del merge), senza perdita di dati.

## 4. Contratti dei componenti

### API-FOOTBALL
- Ogni tool `apifootball_*` corrisponde a un contratto in `bridge/api_football/contracts.py`, e lo schema MCP espone tutti i suoi parametri (test E2E offline).
- Nessun parametro inventato. La fonte è la documentazione ufficiale v3; se è inaccessibile, il contratto resta `PENDING`/`DOCS_UNVERIFIED`.
- Cap di payload: 256 KiB per la risposta MCP; letture dal provider limitate e chiuse in caso di superamento.
- Oltre il cap si restituisce un manifest esplicito senza prezzi; mai prezzi parziali presentati come completi.
- Il risultato di un test distingue `HTTP_200`, `DATA_PRESENT`, `EMPTY_VALID`, `PARTIAL`, `ERROR` e `NOT_TESTED`.

### Browser Use e CloakBrowser
- Browser Use è il provider di default; Cloak è opzionale e configurato solo lato server.
- Stop e reaper sono consapevoli del provider. Nessun CDP esposto (§2.1).

### SX Bet
- Tool `sxbet_*` esposti solo per funzionalità **ufficialmente supportate** e verificate sulla documentazione o sull'OpenAPI ufficiale.
- Prima lettura e simulazione; trading reale solo secondo il §2.6.

## 5. Database e migrations

- `CREATE TABLE IF NOT EXISTS` non è una migration sufficiente per un database vivo.
- Ogni evoluzione dello schema richiede una migration versionata in `sql/migrations/`, apply idempotente, tracking, failure visibile e nessuna perdita di dati.
- Non si rimuovono migration già applicate e non si fanno migrazioni distruttive senza decisione dell'owner.
- Le tabelle condivise (OAuth, lease delle sessioni, coordinatore di quota) non vengono alterate per "far passare" un test.

## 6. Issue e commenti

- La #130 è la roadmap canonica: niente issue duplicate.
- Ogni lavoro significativo si annota nella #130 con PR, commit, CI, deploy e test reale, distinguendo VERIFIED da ASSUMED.
- Le issue legacy chiuse non si riaprono senza un problema tecnico dimostrato.
- Una chiusura richiede un commento motivato. Nessun bug irrisolto viene chiuso come completato.
- Quando un lavoro è completo si annota `READY_FOR_OWNER_REVIEW / PUÒ ESSERE CHIUSA DALL'OWNER`. Chiusure massive di issue legacy solo su mandato esplicito dell'owner.

## 7. Definition of Done universale

Una PR non è finita solo perché compila. Servono:
- scope corretto e Phase 0 documentata;
- test di regressione e negativi;
- CI verde e findings risolti;
- test reale quando applicabile e stato esterno verificato (deploy, log);
- nessun invariante violato e nessun segreto esposto;
- #130 aggiornata;
- nessun falso successo: niente "100%" senza evidenze.

## 8. Governance files

`AGENTS.md` e `CLAUDE.md` sono i file di governance:
- devono restare allineati (verificato da `tests/test_repository_governance.py`);
- non vanno indeboliti per velocizzare una PR;
- un agente può proporre modifiche, ma il merge richiede l'approvazione esplicita dell'owner.
