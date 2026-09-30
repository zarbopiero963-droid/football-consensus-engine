# AGENTS.md

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

Football Consensus Engine è un sistema prospettico e auditabile per:
- acquisire prediction pre-match;
- acquisire quote/benchmark di mercato separatamente;
- congelare decision ticket realmente mostrati;
- acquisire risultati indipendenti;
- misurare strategie preregistrate;
- qualificare nuove prediction source e result source;
- mantenere separati staging, pilot e produzione.

Il progetto privilegia **auditabilità e assenza di leakage** rispetto alla quantità di dati.

## 2. Invarianti non negoziabili

### 2.1 Append-only / immutabilità
- Uno snapshot pre-match già sigillato non viene riscritto.
- Un decision ticket già mostrato non viene riscritto.
- Un raw/hash storico non viene sostituito per far tornare un test.
- Una correzione risultato è una nuova versione/audit, non un overwrite silenzioso.
- Gli slot storici di un pilot non vengono rigenerati dopo un bug fix.

### 2.2 Separazione ambienti
- Discovery, qualification e pilot NON sono produzione.
- Le tabelle `source_pilot_*` e `result_source_pilot_*` non conferiscono production eligibility.
- Nessuna prediction pilot può creare decision/market ticket di produzione senza promozione esplicita.
- Nessun `FOUND_NAME_ONLY` può essere considerato un match production-safe.
- La promozione richiede allowlist/registry/flag esplicito e owner review.

### 2.3 Anti-leakage
- Mai creare una prediction pre-match da una riga live/HT/FT.
- Mai usare risultati futuri o dati post-match per ricostruire uno snapshot mancante.
- Mai modificare una prediction storica per renderla coerente col risultato.
- Uno storico pubblico non sostituisce uno snapshot prospettico.

### 2.4 Dati mancanti
- Missing resta missing.
- Non trasformare missing in zero.
- Non ricostruire probabilità mancanti con complementi, somme o quote salvo che la derivazione sia un prodotto esplicitamente previsto e marcato `DERIVED`, fuori dal layer source originale.
- In particolare non inventare Under = 100 - Over nel layer source se Under non è pubblicato.
- Non derivare HT score da goal events se il source non pubblica HT score ufficiale.

### 2.5 Quote vs modello
- Una quota bookmaker non è una model probability.
- Una implied probability di mercato non è un modello indipendente.
- Un algoritmo che usa odds/market movement va classificato `HYBRID_MARKET_AWARE` o benchmark finché il layer model-only non è separato.
- Conservare sempre model probability e market probability in campi distinti.

### 2.6 Timezone
- UTC è canonical/audit.
- Output utente: `Europe/Rome`.
- Conservare sempre raw/source time.
- Una timezone non si indovina.
- Se non è dimostrata: `TIMEZONE_UNRESOLVED`.
- Il browser configurato in Europe/Rome NON dimostra che il testo della pagina sia Europe/Rome.
- Un match con timezone unresolved non viene promosso a matching production basato sull'orario.

### 2.7 Matching
Per produzione usare, quando disponibili:
- home;
- away;
- kickoff;
- competition/league;
- country;
- categoria (senior/U21/U23/Women/B-team).

Regole:
- exact/canonical pair prima del fuzzy;
- fuzzy/containment solo come indizio, mai come unica prova production;
- `FOUND_NAME_ONLY` = non production-safe;
- ambiguità = non match;
- nessun match permissivo per “far funzionare” settlement o pilot.

### 2.8 Sicurezza/accesso
- Nessun bypass di login, paywall, CAPTCHA, Cloudflare o access control.
- Se il browser/provider risolve automaticamente un challenge standard senza bypass custom, registrare l'esito reale.
- Nessuna credenziale in GitHub, issue, commit, log o screenshot.
- Secret solo in env/config sicura.
- Nessun `cdpUrl` persistito o esposto.

## 3. Sequenza obbligatoria per ogni PR

Ogni PR significativa deve seguire:

### Phase 0
Prima della patch:
1. leggere AGENTS.md e CLAUDE.md;
2. leggere issue sorgente e commenti più recenti;
3. leggere codice corrente di main;
4. verificare dipendenze;
5. identificare invarianti toccate;
6. riprodurre almeno un failure/finding reale quando esiste;
7. annotare rischi e scope.

### Patch
- minima;
- source-specific quando serve;
- niente refactor opportunistici;
- niente workaround che nascondono errori;
- nessun test rimosso/annacquato per far diventare CI verde.

### Test
Ogni PR deve avere:
- test automatici di regressione;
- test negativi/edge case;
- test di invarianti;
- test reale esterno quando la modifica riguarda Render, Neon, Browser Use, collector, settlement o source adapter.

### Hard verify
Prima del merge:
- CI verde;
- review thread risolti;
- nessun errore ignorato;
- verificare stato Render/Neon se pertinente;
- confrontare ciò che il codice dichiara con ciò che è realmente persistito.

### Merge
L'agente può mergiare autonomamente PR operative quando:
- scope approvato;
- CI verde;
- test reali completati;
- nessun finding aperto critico.

Eccezione governance:
- modifiche future a `AGENTS.md` o `CLAUDE.md` richiedono approvazione esplicita dell'owner prima del merge.

## 4. Contratto obbligatorio per una nuova prediction source

Una nuova fonte NON entra direttamente nel consensus.

### Pass 0 — Discovery
Registrare:
- nome canonico;
- URL ufficiale;
- ruolo ipotizzato;
- accesso pubblico/login;
- termini/permessi quando rilevanti;
- mercati dichiarati;
- nessuna conclusione tecnica basata su marketing.

Stato iniziale: `DISCOVERY_PENDING`.

### Pass 1 — Qualification reale
Usare Browser Use Cloud Browser via CDP + Playwright quando necessario, senza Agent che interpreti la pagina.

Verificare realmente:
1. HTTP/final URL;
2. DOM/HTML;
3. eventuali XHR/JSON solo se realmente osservati;
4. fixture future vere;
5. home/away/league/kickoff;
6. prediction pre-match pubblicata;
7. probability/confidence;
8. odds e loro origine;
9. timezone/structured timestamp;
10. storico;
11. access controls;
12. overlap con anchor;
13. provenance/model methodology;
14. dipendenza dal mercato;
15. validità matematica.

Non inventare endpoint.

### Pass 2 — Rigoroso e indipendente
Obbligatorio prima di promozione a pilot quando la fonte è candidata seria.

- nuova sessione browser pulita;
- rieseguire la verifica senza riutilizzare stato del primo pass;
- preferire stesse fixture o campione omogeneo;
- confrontare valori numerici;
- controllare repeatability;
- controllare probability drift;
- controllare retroactive mutation su match già conclusi quando legalmente/tecnicamente osservabile;
- controllare valori <0 o >100;
- controllare somma H/D/A quando completa;
- verificare se output cambia perché usa odds/market movement;
- distinguere model layer da editorial/community layer;
- verificare timezone una seconda volta se ambigua.

Un “secondo refresh della stessa pagina nella stessa sessione” non è un Pass 2 rigoroso.

### Evidenze obbligatorie
Quando tecnicamente consentito salvare:
- raw HTML/DOM;
- screenshot;
- network response rilevanti;
- timestamp;
- SHA-256;
- final URL;
- HTTP status;
- cost telemetry del browser/proxy quando disponibile.

### Classificazioni prediction
Usare una classificazione esplicita, per esempio:
- `INDEPENDENT_MODEL_CANDIDATE`
- `PARTIAL_MODEL`
- `HYBRID_MARKET_AWARE`
- `BENCHMARK_MARKET_DERIVED`
- `COMMUNITY_CONSENSUS`
- `EDITORIAL_BENCHMARK`
- `DATA_SOURCE_ONLY`
- `BLOCKED_BROWSER`
- `PAYWALLED`
- `SOURCE_URL_UNRESOLVED`
- `REJECTED_PREDICTION_LEAKAGE`
- `REJECTED_PREDICTION_RETROCALC`
- `REJECTED_INVALID_PROBABILITY`
- `REJECTED_COVERAGE`

### Gate pilot 48h
Solo candidati con output prospettico utile e provenance sufficiente.

Il pilot deve:
- avere `pilot_id` separato;
- iniziare il clock al primo run reale;
- non partire al merge/deploy;
- mantenere slot storici immutabili;
- salvare run/source/slot;
- misurare HTTP reliability;
- misurare rows parsed;
- misurare complete-vector rate;
- misurare valid probability rate;
- misurare timezone resolution;
- misurare overlap;
- misurare repeatability/drift;
- misurare costi.

## 5. Contratto obbligatorio per una nuova result source

Prediction source e result source sono ruoli separati.

Per ogni result source verificare:
- FT home/away score;
- HT home/away score;
- corners FT;
- corners HT;
- status finale;
- provider match id;
- kickoff;
- competition;
- timezone;
- access/ToS;
- coverage.

Non dichiarare una capability solo perché una parola compare nella pagina. Serve match-level evidence.

### Pass 1 / Pass 2
Anche le result source richiedono:
- primo pass reale;
- secondo pass indipendente per stabilità;
- campione match-level;
- missing non convertiti in zero;
- raw/hash;
- nessun parsing retroattivo ambiguo.

### result_observations
Una source qualificata deve produrre osservazioni normalizzate, non solo `rows_found` o capability flags, prima di poter partecipare al consensus di risultati.

### Consensus per campo
Ogni campo è indipendente:
- `MISSING`
- `SINGLE_SOURCE`
- `VERIFIED`
- `CONFLICT`

Non usare lo status globale per fingere che tutti i campi siano verificati.

Default production:
- `VERIFIED` utilizzabile;
- `MISSING` blocca;
- `CONFLICT` blocca;
- `SINGLE_SOURCE` non è automaticamente certificato salvo policy source-specific esplicita e approvata.

## 6. Contratto pilot 48h

Per ogni pilot registrare anche in Issue #30:
- source issue;
- pilot id;
- objective;
- sources;
- implementation;
- actual start UTC;
- end UTC;
- clock rule;
- Render service/cron;
- Neon tables;
- slots expected;
- state;
- blockers;
- final metrics;
- owner decision.

Regole:
- 16 slot / 48h se cadence = 3h;
- retry non diventa automaticamente nuovo slot;
- bug fix non riscrive slot storici;
- slot invalido va marcato, non corretto retroattivamente;
- pilot diversi restano separati anche se condividono cron/tabelle.

## 7. Contratto promotion → production

Nessuna fonte entra in produzione “perché il pilot è andato bene”.

Serve:
1. qualification completata;
2. Pass 2 rigoroso;
3. pilot completato;
4. timezone/matching sufficienti;
5. validità matematica;
6. provenance;
7. assenza leakage/retrocalc;
8. result independence;
9. cost/reliability;
10. owner review;
11. modifica esplicita di registry/allowlist `production_eligible`.

Fino a quel momento:
- niente decision tickets;
- niente market tickets;
- niente strategy simulations;
- niente production consensus vote.

## 8. Decision ticket contract

“What you saw is what gets settled”.

- Il batch congela esattamente ciò che viene mostrato.
- Snapshot successivi non aggiornano il ticket.
- Quote successive non aggiornano il ticket.
- Risultati successivi vengono joinati al ticket, non ne cambiano il contenuto originale.
- Doppio click/API retry non deve produrre stake duplicato: l'idempotenza è un requisito di produzione.
- Un ticket non è `SETTLED` senza outcome realmente valido.

## 9. Settlement contract

FIFA è una source/fallback, non una verità universale.

Settlement:
- deve usare provider qualificati;
- non deve fare false match;
- deve essere idempotente;
- identico result/provider non crea osservazioni infinite;
- score con zero deve restare zero;
- correzioni ufficiali sono versionate;
- field verification governa i mercati.

Matching permissivo e finestre larghe vanno trattati come finding, non come successo.

## 10. Strategie e probabilità

S1–S9 sono preregistrate e non si ritoccano in base ai risultati.

Canonical strategy probability: 0–1.
Se una source usa 0–100:
- normalizzare esplicitamente prima di applicare strategie;
- conservare raw value nel payload source;
- aggiungere invariant test.

Non cambiare soglie per migliorare performance retrospettive.

## 11. Database e migrations

`CREATE TABLE IF NOT EXISTS` non è una migration sufficiente per un Neon vivo.

Ogni evoluzione schema deve avere:
- migration versionata;
- apply idempotente;
- tracking;
- failure visibile;
- nessuna perdita dati;
- test su DB già inizializzato.

Non aggiungere colonne “per far passare” una query senza verificare ownership semantica del dato.

## 12. Browser Use contract

- Browser Use = infrastruttura browser, non fonte epistemica.
- CDP + Playwright.
- Sessioni sequenziali quando richiesto.
- stop esplicito.
- reaper per sessioni orfane quando implementato.
- niente Agent per interpretazione quando il protocollo richiede raw/DOM diretto.
- browser timezone override non equivale a source timezone.
- non salvare cdpUrl.

## 13. Issue e commenti

Ogni lavoro significativo:
- deve essere tracciato nella issue sorgente;
- deve annotare PR/commit/CI/deploy/test reale;
- deve distinguere VERIFIED da ASSUMED;
- deve correggere checklist stale quando possibile.

Le issue vengono chiuse esclusivamente dall'owner.
L'agente NON chiude issue automaticamente.
Quando completata:
`READY_FOR_OWNER_REVIEW / PUÒ ESSERE CHIUSA DALL'OWNER`.

## 14. Definition of Done universale

Una PR non è “finita” solo perché compila.

DoD:
- scope corretto;
- Phase 0 documentata;
- test regressione;
- test negativo;
- CI verde;
- review findings risolti;
- test reale se applicabile;
- stato esterno verificato;
- nessun invariant violato;
- issue aggiornata;
- nessun dato storico riscritto;
- nessun falso successo.

## 15. Known deviations / remediation

Issue #37 è il backlog autorevole per le deviazioni note, tra cui:
- pilot isolation;
- zero→NULL persistence;
- ProSoccer timezone;
- StatsUltra inferred Under;
- real result-source parsers;
- field-level settlement verification;
- observation idempotency;
- FIFA matching/fetch hardening;
- accurate insert counters;
- migrations.

Finché una remediation non è mergiata e testata, non assumere che il codice corrente rispetti automaticamente il contratto.

## 16. Governance files

`AGENTS.md` e `CLAUDE.md` sono file di governance.
- devono restare allineati;
- non vanno indeboliti per velocizzare una PR;
- un agente può proporre modifiche;
- il merge di una modifica a questi file richiede approvazione esplicita dell'owner.
