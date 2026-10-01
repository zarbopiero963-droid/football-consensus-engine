# football-consensus-engine

Sistema prospettico e auditabile per prediction pre-match, decision ticket immutabili, benchmark di mercato, result consensus multi-source, pilot di qualificazione e settlement.

> **OBBLIGATORIO PER QUALUNQUE AGENTE:** leggere prima [AGENTS.md](AGENTS.md) e [CLAUDE.md](CLAUDE.md). Le policy di governance prevalgono su codice storico, checklist stale e vecchie descrizioni quando esiste un conflitto documentato.

## Stato operativo e source of truth

Il repository contiene sia codice vivo sia artefatti storici. Non assumere che una sezione con una data passata rappresenti lo stato corrente.

Source of truth operativo:
1. codice corrente su `main`;
2. invarianti in `AGENTS.md` / `CLAUDE.md`;
3. stato reale Render/Neon;
4. issue operative e relativi commenti più recenti;
5. artefatti storici, solo come evidenza/audit.

Architettura corrente:
- **FootInsights**: source prediction primaria corrente del ledger prospettico;
- **Pinnacle**: benchmark/mercato, separato dal modello;
- **FIFA Calendar**: una result source/fallback, **non** un settlement universale;
- **result sources qualificate**: devono produrre `result_observations` normalizzate prima di partecipare al consensus;
- **pilot prediction/result**: staging isolato, non production eligibility;
- **Neon**: persistenza operativa;
- **Render**: runtime reale di collector/settlement/pilot;
- **Europe/Rome**: timezone di output utente; UTC resta canonical/audit.

### Deviazioni note

La Issue #37 è il backlog autorevole delle remediation architetturali note. Finché una correzione non è mergiata e testata realmente, non assumere che il codice rispetti automaticamente il contratto.

Tra le deviazioni già identificate:
- pilot source non devono alimentare production ticket;
- score `0` deve restare `0`, non `NULL`;
- ProSoccer non può essere marcato timezone-verificato senza prova;
- StatsUltra non deve ricostruire Under da Over nel layer source;
- result-source #20 deve produrre observation match-level reali, non solo capability probe;
- settlement market deve rispettare la verifica field-level;
- observations devono essere idempotenti;
- FIFA matching/fetch va hardenizzato;
- migrations Neon devono essere versionate.


## Contratto repository per nuove fonti

Questa sezione è normativa. I dettagli completi sono in [AGENTS.md](AGENTS.md) e [CLAUDE.md](CLAUDE.md).

### Nuova prediction source: lifecycle obbligatorio

Nessun sito viene aggiunto direttamente al consensus.

#### Pass 0 — Discovery
Registrare almeno:
- URL ufficiale;
- ruolo candidato;
- accesso pubblico/login;
- eventuali limiti/permessi;
- mercati dichiarati;
- stato iniziale `DISCOVERY_PENDING`.

Marketing, claim “AI”, Poisson, Elo, accuracy o ROI non valgono come prova.

#### Pass 1 — Qualification reale
Verificare con browser/CDP o altro metodo appropriato:
- HTTP e final URL;
- DOM/HTML;
- XHR/JSON solo se realmente osservati;
- fixture future;
- home/away/competition/kickoff;
- prediction realmente pre-match;
- probability/confidence;
- odds e loro origine;
- timezone/structured time;
- storico;
- access control;
- overlap con anchor;
- provenance;
- dipendenza dal mercato;
- validità matematica.

Regole:
- niente endpoint inventati;
- niente probability ricostruite;
- niente odds convertite e chiamate “modello”;
- niente dati live/FT usati per creare prediction pre-match.

#### Pass 2 — Rigoroso
Per un candidato serio è obbligatorio un secondo pass **indipendente**:
- nuova sessione browser pulita;
- stesso match o campione omogeneo quando possibile;
- confronto numerico;
- repeatability;
- drift;
- retroactive mutation;
- probability <0 / >100;
- somma vettore H/D/A;
- contamination da odds/market movement;
- timezone verificata di nuovo quando ambigua.

Un semplice refresh nella stessa sessione non è un doppio pass rigoroso.

#### Classificazione
Usare una classificazione esplicita, ad esempio:
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

### Nuova result source: lifecycle obbligatorio

Per ogni result source verificare a livello partita:
- FT home/away;
- HT home/away;
- corners FT;
- corners HT;
- status finale;
- provider match id;
- kickoff;
- competition;
- timezone;
- access/ToS;
- coverage.

Anche le result source richiedono Pass 1 e Pass 2 rigoroso.

Una source non è “integrata” perché `rows_found > 0` o perché una parola compare nell'HTML. Per entrare nel result consensus deve produrre `result_observations` normalizzate e auditabili.

### Timezone contract

- raw/source time va conservato;
- UTC = canonical;
- Europe/Rome = output utente;
- browser timezone override non dimostra source timezone;
- timezone non verificata = `TIMEZONE_UNRESOLVED`;
- non indovinare offset;
- non promuovere matching production basato solo su un'ora ambigua.

### Matching contract

Per produzione usare:
- home;
- away;
- kickoff;
- competition;
- country;
- category quando disponibili.

`FOUND_NAME_ONLY` è un finding, non un match production-safe.

Ambiguità = non match.

### Pilot 48h contract

Un pilot:
- ha `pilot_id` proprio;
- parte dal primo run reale, non dal merge;
- conserva slot storici immutabili;
- non riscrive slot dopo una patch;
- misura reliability, rows, probability validity, timezone, overlap, repeatability e costi;
- resta separato dalla produzione.

Se cadence = 3h, 48h = 16 slot attesi.

Tutti i pilot attivi vanno registrati nella Issue #30.

### Promotion contract

Per diventare production source servono:
1. qualification;
2. Pass 2 rigoroso;
3. pilot completato;
4. timezone/matching sufficienti;
5. probability/data validity;
6. provenance;
7. anti-leakage;
8. reliability/cost;
9. owner review;
10. registry/allowlist `production_eligible` esplicita.

Prima della promozione:
- niente decision ticket;
- niente market ticket;
- niente strategy simulations;
- niente voto production nel consensus.

### Result verification contract

Il consensus è per **campo**, non solo per match:
- `MISSING`
- `SINGLE_SOURCE`
- `VERIFIED`
- `CONFLICT`

Default:
- VERIFIED: utilizzabile;
- MISSING: blocca;
- CONFLICT: blocca;
- SINGLE_SOURCE: non automaticamente certificato senza policy esplicita.

FT verificato non rende automaticamente verificati HT o corners.

### Contratto immutabilità

Non riscrivere:
- snapshot pre-match;
- decision ticket;
- raw/hash storici;
- slot pilot storici.

Un bug fix vale dai run successivi. Gli storici vengono annotati come invalidi/parziali quando necessario, non rigenerati.

### Contratto PR e test reali

Ogni PR significativa deve avere:
- Phase 0 sul codice corrente;
- failure/finding riprodotto quando esiste;
- test regressione;
- test negativo;
- CI verde;
- review findings risolti;
- test reale su Render/Neon/Browser quando applicabile;
- commento nella issue con PR/commit/deploy/evidenza reale.

Nessun test viene rimosso o indebolito per far diventare verde una Action.

### Chiusura issue

Le issue vengono chiuse solo dall'owner.

L'agente, quando tutto è verificato, scrive:

`READY_FOR_OWNER_REVIEW / PUÒ ESSERE CHIUSA DALL'OWNER`

e lascia la issue OPEN.

---

## Layout

```
prospective/     collector FootInsights + Pinnacle, supervisor, settlement FIFA, ledger
staging/forebet/ parser Forebet, pilot 48h, snapshot già chiusi
staging/football_charts/  pilot Football Charts
staging/browser_use_shortlist/  probe CDP degli 8 siti e analisi offline
staging/forebet_cdp/  primo probe CDP Forebet
backtest/        baseline storica e falsificazione (non è il ledger prospettico)
storage/         hash dei raw non versionati e registro delle redazioni
```

## Snapshot storico Forebet — non ricreare e non usare come stato vivo

| Campo | Valore |
|---|---|
| pilot_id | `forebet-48h-20260929T153629Z` |
| deadline | 2026-10-01T15:36:29Z |
| slot pianificati | 16, uno ogni 3 ore |
| slot completati | 1/16 |
| slot 1 | SUCCESS, run canonico `20260929T153629Z-s01a1` |
| retry | 1, run `20260929T153847Z-s01a1` (tunnel). Non è uno slot nuovo |
| classificazione | IN_PROGRESS. Non promuoverlo |

`staging/forebet/pilot48/STARTED.json` esiste già. Un nuovo avvio del loop non deve rigenerarlo e non deve trasformare il retry in uno snapshot pianificato. Il rapporto finale è `slot riusciti / 16`.

I file `page.html`, `page.png` e i body di rete non sono nel git. Hash e path sono in [storage/omitted_blobs.jsonl](storage/omitted_blobs.jsonl). I fixture parsati, i manifest e `manifest.sha256` dello slot 1 ci sono.

Quattro `report.json` di validazione avevano `cookie_sample`. Nel git il campo è `REDACTED_NOT_COMMITTED`. L'hash originale resta in `seal_at_start.json` e in [storage/redactions.jsonl](storage/redactions.jsonl).

## Avvio da un clone pulito

Python 3.10+. Il collector e il settlement usano solo la libreria standard. Playwright serve solo ai pilot che aprono un browser remoto via CDP. Non serve `playwright install`: il browser è quello di Browser Use.

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
# mettere il valore vero solo in .env oppure in staging/forebet/pilot48/var/api_key (gitignored)
```

Collector (scrive ledger, snapshot, raw e manifest; non sovrascrive file esistenti):

```bash
python3 prospective/collect.py
python3 prospective/bin/supervisor.py --once
python3 prospective/bin/supervisor.py --self-test
python3 prospective/bin/supervisor.py
```

`supervisor.py` senza argomenti resta in loop ogni 3 ore, con lock esclusivo, retry a 60/180/300 secondi e alert di stallo dopo 6 ore. `--ensure` avvia il processo solo se non è già in esecuzione.

Settlement legacy/FIFA fallback (il sistema reale può usare anche result observations/consensus; FIFA non è una fonte universale):

```bash
python3 prospective/settle.py --self-test
python3 prospective/settle.py
python3 prospective/bin/settle_loop.py
```

Il loop di settlement è ogni 30 minuti.

Report di qualità dell'ultimo snapshot:

```bash
python3 prospective/report.py
```

Forebet (legge `BROWSER_USE_API_KEY` dall'ambiente, altrimenti dal file gitignored):

```bash
python3 staging/forebet/pilot_loop.py
```

`staging/forebet/extract.py` fa due run di validazione e non incrementa il contatore dei 16 slot. Non usarlo per continuare il pilot.

Football Charts (HTTP proprio, non Browser Use; non scrive il ledger):

```bash
python3 staging/football_charts/pilot.py --self-test
python3 staging/football_charts/pilot_loop.py
```

Shortlist, solo analisi dei file già salvati:

```bash
python3 staging/browser_use_shortlist/analyze.py
```

Backtest storico, separato dal ledger prospettico:

```bash
python3 backtest/run_backtest.py
python3 backtest/run_falsify.py
```

## Architettura

Il collector legge le pagine pubbliche FootInsights e il guest feed Pinnacle (`guest.api.arcadia.pinnacle.com`, sport 29, moneyline full-match). Non usa una API key Pinnacle. Ogni run crea una directory nuova sotto `prospective/raw/` e `prospective/snapshots/`, accoda una riga a `prospective/manifests/manifest_index.jsonl` e scrive `manifest_<run>.sha256`. Il ledger `prospective/prospective_ledger.csv` è append-only. Hash del ledger in questo snapshot: `5d5163bd6b2b09e5c32ece63170b27e301b4bc4901a15c199e295f8e787a5464`.

Il settlement legacy interroga anche `https://api.fifa.com/api/v3/calendar/matches` almeno 120 minuti dopo il kickoff, ma FIFA è una source/fallback e non garantisce copertura universale. I risultati normalizzati e il result consensus devono mantenere provenance e verifica per campo. Uno score identico non deve creare duplicati; una correzione ufficiale è una nuova versione più audit. Le strategie restano preregistrate e non vengono ritoccate in base ai risultati.

Le strategie sono congelate in `prospective/strategies_preregistered.json` (`prereg-2026-09-29`), prima di qualsiasi risultato prospettico.

| Id | Regola |
|---|---|
| S1 | bet sul pick del modello se P >= 0.50 |
| S2 | come S1 con P >= 0.60 |
| S3 | S1 e il pick è il favorito Pinnacle |
| S4 | S2 e il pick è il favorito Pinnacle |
| S5 | S1 e edge > 0 |
| S6 | S2 e edge > 0 |
| S7 | edge >= 0.03 |
| S8 | edge >= 0.05 |
| S9 | edge >= 0.08 |
| B1 | sempre il favorito Pinnacle |
| B2 | favorito Pinnacle sulle partite di S1 |
| B3 | favorito Pinnacle sulle partite di S2 |
| B4 | nessun bet, ROI 0 |

Edge = probabilità del modello sul pick meno la fair probability Pinnacle sullo stesso esito. Stake flat 1. Niente Kelly. Le quote sono Pinnacle, non le quote mostrate sulla pagina FootInsights.

Browser Use è solo infrastruttura: `POST /api/v4/browsers`, `cdpUrl`, Playwright `connect_over_cdp`, poi stop della sessione. Nessun agent interpreta la pagina. Il valore di `cdpUrl` non viene salvato.

I path nel codice sono relativi alla root del repository. I JSON storici (`slots.jsonl`, `seal_at_start.json`) conservano i path assoluti `/workspace/...` scritti nel sandbox. Non vanno riscritti.

## Schema CSV

Ledger, una riga per snapshot di una partita. Il risultato resta vuoto finché non lo scrive un processo esterno; il settlement non aggiorna questo file.

`prospective/prospective_ledger.csv`

`match_id,source_match_id,league,country,season,kickoff_utc,home_team,away_team,home_canon,away_canon,team_flags,snapshot_timestamp_utc,minutes_before_kickoff,model_version,prediction_published_date,scraper_version,strategy_set_id,p_home_raw,p_draw_raw,p_away_raw,p_home_normalized,p_draw_normalized,p_away_normalized,prob_parse_source,model_pick,model_pick_probability,source_url,sha256_model_raw,page_book,page_odds_home,page_odds_draw,page_odds_away,valid_prematch,analysis_eligible,pin_matchup_id,pin_league,pin_home,pin_away,pin_snapshot_timestamp_utc,pin_kickoff_utc,odds_home,odds_draw,odds_away,american_home,american_draw,american_away,fair_home,fair_draw,fair_away,overround,pin_favorite,pin_favorite_probability,pin_favorite_odds,edge_home,edge_draw,edge_away,picked_odds,picked_pin_fair,picked_edge,same_as_favorite,market_version,sha256_odds_raw,odds_raw_file,name_score_note,final_home_goals,final_away_goals,result_1x2,match_status,result_timestamp_utc,result_source,correct,profit_flat_1`

`prospective/normalized/results.csv` è l'unico file di risultati. Colonne S1–S9 e B1–B3 ripetute come `*_active,*_correct,*_profit`. B4 non ha colonne perché è il non-bet.

`prospective/snapshots/<day>/<run>/invalid_prematch.csv`

`match_id,source_url,reason,kickoff_utc,home_team,away_team,snapshot_timestamp_utc,sha256_model_raw`

Forebet `fixtures.csv`:

`run_id,snapshot_timestamp_utc,league,home,away,kickoff_original,kickoff_utc,timezone_assumed,timezone_confidence,p1,px,p2,main_pick,correct_score,avg_goals,odds_1,odds_x,odds_2,ou,btts,row_state,match_id,source_url,row_hash,parse_status`

## Cosa non è in questo repository

- API key, cookie, `cdpUrl`, token, connection string
- HTML FootInsights, JSON Pinnacle, screenshot e body XHR (solo hash)
- servizi Render
- database Neon

Per rimettere un raw al suo posto, vedi [storage/README.md](storage/README.md).


## Neon read layer and query API

The database layer introduced in PR1 is additive: the existing CSV ledger remains the source of truth until a later PR wires collectors directly to Neon.

Set `DATABASE_URL`, then apply the schema and import the current append-only CSV state idempotently:

```bash
python3 -m app.migrate
python3 -m app.import_legacy
```

Run the read-only API locally:

```bash
uvicorn app.api:app --host 0.0.0.0 --port 10000
```

Endpoints:

- `GET /healthz` — database connectivity.
- `GET /health/invariants` — read-only readiness and audit counts. Critical
  pilot leakage, unsafe production time or settlements without required VERIFIED
  fields return `FAIL` / HTTP 503. Coverage, parser, pilot timezone and stale or
  unobserved worker warnings return `DEGRADED` with `ready=false`; connectivity
  alone never certifies readiness. Pending ratio uses shown tickets at least two
  hours after kickoff, warns above 0.5 and stays null when there are none. Worker
  age limits are six hours for collector and one hour for settlement. These
  operational thresholds do not alter strategy rules or promote sources.
- `GET /today` — latest valid pre-match snapshot per match for today's Europe/Rome calendar date.
- `GET /yesterday` — same snapshots plus latest settled result when available.
- `GET /date/YYYY-MM-DD?results=true` — explicit date query.
- `GET /market-reference/{match_id}?known_at=<ISO-offset-time>` — read-only
  Pinnacle FT 1X2 series. `q_open` is the first observed quote, not an asserted
  bookmaker opening price. `q_close` is the latest observed pre-kickoff proxy,
  exposed only after kickoff; before then the reference is `PROVISIONAL`.
  Capture and database sealing must both precede kickoff and the requested
  knowledge time. The response includes decimal odds, proportional inverse-odds
  fair probabilities, overround, timestamps, raw hash and seconds to kickoff.
  A three-hour collector cadence can yield a stale close proxy; no missing price
  is reconstructed. Provider ID/kickoff ambiguity blocks the series. Historical
  quotes without the new matching contract remain `LEGACY_UNCERTIFIED` diagnostics
  with `q_open`/`q_close` missing; reading them never changes frozen tickets.

New collector quotes require an exact ordered name pair (NFKC/case/whitespace
only), kickoff within 15 minutes, explicit timezone and agreeing competition.
Available country/category metadata must agree. Cross-source naming variants
need evidenced registry bindings before adoption; fuzzy/containment never prices
a production snapshot. Competition names that differ remain unmatched. Each
source response carries its own acquisition timestamp, so a run started before
kickoff cannot backdate a response received afterwards. No historical ledger
headers or rows are changed; the existing `name_score_note` records the contract.

The API recomputes only the already preregistered S1-S9 activation flags from the frozen snapshot fields. It does not retune thresholds and does not scrape the web.


## Neon dual-write mode

When `DATABASE_URL` is present, the prospective collector also inserts each newly sealed snapshot into Neon and settlement writes each immutable result version into Neon. Existing CSV/raw/manifest files remain untouched and continue to provide the local audit trail.

For the persistent Render runtime set:

```bash
DATABASE_REQUIRED=1
```

In required mode a Neon write error is fatal rather than silently allowing the persistent database to drift. Snapshot conflicts and result-version conflicts use `ON CONFLICT DO NOTHING`; an existing sealed record is never rewritten. `runtime_state` is the only intentionally mutable operational table.

A clean persistent startup should reconcile the committed CSV state before launching workers:

```bash
python3 -m app.migrate
python3 -m app.import_legacy
```


## Browser Use MCP bridge

The bridge is deliberately separate from the collectors. Browser Use supplies remote browser infrastructure only; no Browser Use Agent interprets page content.

Tools exposed by `bridge.server`:

- `browser_create`
- `browser_open_url`
- `browser_get_html`
- `browser_get_dom`
- `browser_screenshot`
- `browser_get_network_responses`
- `browser_stop`

The Browser Use `cdpUrl` is treated as a bearer-like capability and is kept in process memory only. It is never returned by an MCP tool, committed, or logged by the bridge. Browser sessions stay connected between tool calls and are terminated explicitly with `browser_stop`.

The bridge rejects non-HTTP(S), credential-bearing, loopback, private, link-local, multicast, reserved and unspecified destinations, including navigation redirects. It must not be used to bypass paywalls, authentication, or other access controls.

PR3 adds the MCP server implementation only. Public deployment, authentication and Render configuration are intentionally deferred to the next PR.


## Secure MCP bridge deployment

The public bridge endpoint is mounted at `/mcp` and requires:

```
Authorization: Bearer <MCP_BRIDGE_TOKEN>
```

`MCP_BRIDGE_TOKEN` and `BROWSER_USE_API_KEY` are separate secrets. The first protects access to the bridge; the second is used only server-side to create Browser Use cloud sessions.

Health endpoint:

```
GET /healthz
```

It returns only booleans indicating whether authentication and Browser Use are configured; it never returns secret values.

Render start command for the dedicated bridge service:

```bash
uvicorn bridge.server:app --host 0.0.0.0 --port $PORT
```

The ChatGPT custom MCP URL is the dedicated Render service URL plus `/mcp`.

The bridge records only its own allocated browser IDs in `browser_session_leases`.
It never persists CDP URLs or enumerates unrelated provider browsers. Operations
renew a five-minute lease, serialize per session and have a three-minute timeout.
A background reaper checks every minute for expired idle leases; active leases
and busy local operations are excluded. A failed remote stop remains durable and
retries after a one-minute backoff, including after worker restart. Shutdown stops
owned local sessions. `browser_reap` offers an explicit idle cleanup (minimum 60
seconds, default 300); it uses the same ownership and lease guards.

Each navigation captures at most 500 response metadata records and removes its
own response listener on success, failure or cancellation. A disabled capture
clears the previous one. Reconnecting CDP installs the URL guard on the new
context; local disconnect errors are surfaced.


## ChatGPT OAuth 2.1

The Browser Use bridge now acts as both MCP resource server and a minimal single-user OAuth 2.1 authorization server for this private deployment.

Discovery:

- `GET /.well-known/oauth-protected-resource`
- `GET /.well-known/oauth-authorization-server`

Flow:

- Authorization Code
- PKCE `S256`
- ChatGPT Client ID Metadata Document (CIMD)
- access tokens stored only as SHA-256 hashes in Neon
- rotating refresh tokens stored only as SHA-256 hashes in Neon
- scopes: `browser:use offline_access`

The authorization page requires `MCP_OAUTH_PASSWORD`, stored only as a Render environment variable. The previous static `MCP_BRIDGE_TOKEN` is no longer used by the bridge.

ChatGPT plugin configuration uses:

```
Server URL: https://football-consensus-browser-bridge.onrender.com/mcp
Authentication: OAuth
```

OAuth metadata is discovered automatically from the server. No client secret is manually entered in ChatGPT.


## Decision tickets: what you saw is what gets settled

Automatic snapshots remain monitoring data. A user-facing recommendation must be frozen separately before it is shown.

Create an immutable decision batch:

```http
POST /decisions/date/2026-09-30
Content-Type: application/json
Idempotency-Key: daily-picks-20260930-1700

{"label":"17:00 daily picks","mode":"all","strategies":[]}
```

Supported modes:

- `all`: freeze every still-future match returned to the user.
- `bet`: freeze only rows currently classified BET.
- `signals`: freeze only rows activating at least one requested strategy; for example `["S2","S4","S6","S8","S9"]`.

The batch selects the latest valid snapshot that already existed at `requested_at_utc`, excludes matches that have already kicked off, stores the exact response in `decision_tickets.snapshot_view`, and returns that same object to the caller.

Both decision POST endpoints require `Idempotency-Key` (1–128 ASCII letters,
digits, dots, underscores, colons or hyphens). Generate one key per intended
decision and reuse it for every retry or double click. The same key and request
return the original frozen response, including its batch and market tickets,
even after snapshots or outcomes change. Reusing the key for a different date,
label, mode or strategy list returns HTTP 409; a missing/invalid key returns
HTTP 422. Use a new key for an intentional new decision. Existing frozen batches
are preserved. Batch, decision tickets, market tickets and replay response commit
atomically; concurrent requests for one key serialize in PostgreSQL.

Later automatic snapshots never update decision tickets.

Read a frozen batch:

```http
GET /decisions/{batch_id}
```

After settlement, the endpoint combines the frozen pick/probability/odds with the verified result. Profit uses the frozen displayed odds, never a later quote.

`GET /yesterday` now prefers decision tickets. If none were created for that date it explicitly returns `report_mode=monitoring_only` instead of pretending the latest pre-kickoff snapshot was what the user saw.


## Timezone contract — user output is always Italy time

All storage and matching remain canonical in UTC. Source-specific raw data must preserve the original timestamp and its declared/verified timezone, then normalize to UTC before joining fixtures.

User-facing API/report fields are always rendered in the IANA timezone:

```
Europe/Rome
```

This automatically applies CEST (UTC+2) and CET (UTC+1) according to the calendar date.

For every displayed snapshot the API includes:

- `kickoff_italy`
- `snapshot_timestamp_italy`
- `requested_at_italy` for decision batches

The corresponding `*_utc` values remain present only as canonical machine/audit timestamps.

For a source that exposes a naive local time, adapters must call `normalize_source_datetime(value, source_tz)` with a verified IANA timezone. They must not guess an offset. If a source timezone cannot be verified, the fixture is `TIMEZONE_UNRESOLVED` and must not be silently joined.

Example:

```
source: 2026-09-30 16:00 America/New_York
canonical UTC: 2026-09-30 20:00Z
shown to user in Italy: 2026-09-30 22:00 CEST
```

The daily date boundary for `/today`, `/yesterday`, `/date/...` and decision batches is also Europe/Rome, not UTC.


## Strategy accounting — synchronized virtual €1 flat stake

The strategy ledger is derived automatically from immutable decision tickets and the latest versioned match result.

For every strategy activated in a user-visible decision ticket, the system creates one simulation record with:

- strategy id and descriptive name;
- pick, probability, odds and edge frozen from the decision ticket;
- virtual flat stake: €1.00;
- status: `PENDING`, `WON`, or `LOST`;
- result 1X2;
- correct/incorrect flag;
- profit in EUR when a price exists.

Synchronization occurs automatically:

1. immediately after a decision batch is created, producing `PENDING` rows;
2. after every settlement cycle, updating the derived rows from `match_results` and recomputing aggregate strategy statistics.

Aggregate statistics include signals, pending, settled, priced settled records, wins, losses, total virtual stake, cumulative profit, win rate and ROI.

`sample_reliability` describes only sample maturity, not predictive quality:

- <30 settled: `INSUFFICIENT_SAMPLE`
- 30–99: `EARLY_SAMPLE`
- 100–249: `DEVELOPING_SAMPLE`
- 250+: `MATURE_SAMPLE`

API:

- `GET /strategies`
- `GET /strategies/{strategy_id}`

The simulation ledger never places a real wager and never rewrites decision tickets or match results.

## Versioned database migrations

`python3 -m app.migrate` applies numbered SQL files in `sql/migrations/` in one
transaction, under a transaction-scoped advisory lock. `0001_baseline.sql` freezes
the existing schema. An existing database adopts the baseline without rewriting
its records; later schema changes require a new numbered migration with explicit
DDL such as `ALTER TABLE`.

`schema_migrations` records each version, filename, SHA-256 and application time.
Repeated runs skip an unchanged applied migration. Changed checksums, invalid or
missing versions and SQL errors fail visibly; a failed upgrade rolls back the
whole transaction. Never edit an applied SQL file. Use a direct database
connection for migrations and test each upgrade on a Neon branch before applying
it to production.

## Canonical identity registry

Migration `0004_canonical_entities.sql` adds stable internal UUID identities for
teams and competitions. External IDs are optional metadata, never an identity
authority. Entries require explicit country, gender, age group, squad type and
provenance. Category keys distinguish `MEN:SENIOR:FIRST`, women, Uxx, B, II and
reserve teams. Lexical markers are hints; an unmarked name does not prove a
senior men's first team. A leading B in `B 93 Copenhagen` is not a reserve flag.

`app.identities.create_entity` and `append_alias` operate within the caller's
transaction. Alias bindings are source-specific and exact after Unicode/case/
whitespace normalization; club prefixes, accents, numbers and punctuation are
preserved. Variants require an evidenced alias. Alias revisions are append-only,
ordered and serialized. PostgreSQL rejects update/delete/truncate of the registry.
Retractions append a new revision and retain the original evidence.

`app.identities.resolve` takes both `fixture_at` (validity of the alias) and
`known_at` (knowledge cutoff), with explicit timezones. A later alias cannot
resolve an earlier frozen snapshot retroactively. Missing context returns
`CONTEXT_UNRESOLVED`, no candidate returns `UNKNOWN`, and multiple distinct
entities return `AMBIGUOUS`, all without a usable entity ID.

The registry starts empty and does not auto-import legacy names or assume missing
countries. This additive foundation neither promotes a pilot source nor rewrites
frozen tickets, and is not yet a replacement for all legacy joins. Production
matching adoption requires separately evidenced bindings and strict time and
competition gates. Current collector Pinnacle joins still require the explicitly
tracked follow-up in master #35; identity infrastructure alone does not certify them.
