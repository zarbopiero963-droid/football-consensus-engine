# API-FOOTBALL v3 — matrice di certificazione offline

Questa cartella contiene `matrix.json` e `matrix.csv`. Sono generati da `scripts/apifootball_cert_matrix.py`
a partire **solo** da fonti del repository:

- `bridge/api_football/contracts.py`, cioè i contratti, i tipi, i vincoli e le regole di combinazione estratte via AST;
- gli schemi MCP reali, prodotti da `register_tools()` (la stessa funzione chiamata da `bridge/server.py`) sulla classe server dell'SDK MCP;
- i test offline, letti con una scansione statica;
- le evidenze live già committate;
- lo snapshot della documentazione `docs/api_football/official_parameter_matrix_v3.9.3.json`.

La generazione non fa chiamate al provider, a MCP, a Render o al database e non consuma quota. Il risultato è deterministico.

```bash
python scripts/apifootball_cert_matrix.py                # rigenera matrix.json e matrix.csv
python scripts/apifootball_cert_matrix.py --check        # exit 1 se i file committati non sono aggiornati
python scripts/apifootball_cert_matrix.py --gate-offline # gate della PR (deve passare)
python scripts/apifootball_cert_matrix.py --gate-live    # certificazione live completa (informativo; oggi FAIL)
python scripts/apifootball_cert_batch1.py --write-plan   # rigenera batch1_plan.json (--check-plan per verificarlo)
```

`--check` dipende anche dai test. Se si modifica un test API-FOOTBALL, la matrice va rigenerata.

## Gate

| Gate | Cosa verifica | Dove gira | Stato |
|---|---|---|---|
| `--gate-offline` | Struttura e vocabolari; ogni tool `apifootball_*` presente; ogni parametro dello schema MCP e del contratto presente ed esposto; nessun parametro inventato; ogni `COVERED` con test citati; ogni riuso con prova; file aggiornati (`--check`). | CI della PR (`multi-market-tests.yml`, job `pytest`) | **PASS** |
| `--gate-live` | Fallisce se una riga `MANDATORY` è `FAIL`, `BLOCKED`, `NOT_TESTED` o `STALE`. Non bloccano `PASS`, `REUSED_EQUIVALENT` e `PROVIDER_LIMITATION` documentata. | Solo a mano, dopo una campagna autorizzata | FAIL atteso |

## Righe e vocabolario

Le righe sono 378:

| kind | righe | contenuto |
|---|---|---|
| `PARAMETER` | 142 | una riga per ogni (tool, parametro) |
| `BEHAVIOUR` | 173 | `ROUTE_CALL`, `EMPTY_VALID`, `NULL_HANDLING`, `PAYLOAD_CAP` e, se paginato, `PAGINATION`, per ogni tool; più 9 righe trasversali (`*`) |
| `RULE` | 49 | rifiuti locali e combinazioni di `/fixtures` |
| `LOCAL_TOOL` | 2 | — |
| `SNAPSHOT` | 12 | requisiti di LIVE FULL MATCH SNAPSHOT |

**verdict** (CLAUDE.md §4): `HTTP_200`, `DATA_PRESENT`, `EMPTY_VALID`, `PARTIAL`, `ERROR`, `NOT_TESTED`. A questi si aggiungono due verdetti di disponibilità del provider (condizione E dell'owner):

- `PROVIDER_NOT_OFFERED`: il provider non offre il dato. L'evidenza è la copertura della lega o la documentazione.
- `PLAN_EXCLUDED`: il piano lo esclude. L'evidenza è la risposta reale con errore dichiarato `plan` (categoria `PLAN_NOT_INCLUDED`, `bridge/api_football/errors.py`).

Non sono difetti del MCP. Se sono documentati, `gate_status` è `PROVIDER_LIMITATION` e non bloccano.

**live_requirement**:
- `MANDATORY`;
- `DATA_DEPENDENT`: serve una partita live, quote in finestra o una risposta oltre il cap;
- `OFFLINE_ONLY`: rifiuti locali a 0 chiamate, oppure errori 401/403/429/5xx/timeout da non provocare live.

**gate_status**: `PASS`, `REUSED_EQUIVALENT`, `PROVIDER_LIMITATION`, `FAIL`, `BLOCKED`, `NOT_TESTED`, `STALE`, `NOT_APPLICABLE`.

**docs_status**: vale `DOCS_UNVERIFIED` su tutte le righe. La pagina ufficiale ha risposto 403 (challenge Cloudflare, non aggirato). `docs_snapshot_status` confronta lo snapshot del 2026-11-01.

## AF-01 — Riuso selettivo delle evidenze dell'11-11 (848c007)

Colonne della matrice: `source_sha`, `evidence_date`, `evidence_ref`, `changes_since`, `reuse_status`, `reuse_reason`, `reuse_proof`.

**Modifiche tra 848c007 e f666cc1 sul percorso di codice API-FOOTBALL** (`bridge/api_football/**`, `bridge/server.py`, `bridge/db_identity.py`, `requirements.txt`): solo `e155193` (#193), che ha toccato `contracts.py`, `stream_scan.py` e `tools.py`. Gli altri commit del periodo (`6615acb`, `6c69521`, `d09571f`, `f666cc1`) non toccano quel percorso.

**Come è provata l'equivalenza** (`tests/test_apifootball_cert_reuse_equivalence.py`), in due passi:
1. `git diff --name-only 848c007 HEAD` sul percorso di codice deve coincidere con i tre file registrati. Qualunque nuova modifica invalida i riusi.
2. La busta del provider registrata, riprodotta attraverso il bridge corrente (client resiliente e transport reali su `httpx.MockTransport`), deve dare la risposta MCP registrata. Il confronto riguarda status, parametri, paginazione, `pagination_state`, warning e `network_calls_made`; anche i dati restano identici.

| Righe | Stato | Motivo |
|---|---|---|
| `player_profiles.page`, `player_profiles` `ROUTE_CALL` (caso A09) | **REUSED_EQUIVALENT** (MANDATORY) | La rotta non è modificata. La modifica a `pagination_state` vale solo per pagine vuote oltre la prima. Il replay è identico. |
| `odds.fixture`, `odds` `ROUTE_CALL` (caso B06f) | **REUSED_EQUIVALENT** (DATA_DEPENDENT) | La rotta non è modificata. Il warning dei manifest vale solo oltre il cap, e B06f era sotto il cap. Il replay è identico. |
| `player_profiles` `PAGINATION`, `EMPTY_VALID` (A10) | STALE | `e155193` ha cambiato proprio questo comportamento: il replay dà `EMPTY_PAGE` e non più `LAST_PAGE`. |
| `fixtures.team`, `fixtures.season`, `fixtures` `ROUTE_CALL` (A03) e regola A04 | STALE | La rotta `/fixtures` è stata modificata da `e155193`. |
| Evidenze del 2026-10 (httpx diretto) | NOT_REUSABLE | Fuori dal MCP e dal coordinatore. |

**Conteggi**:
- `MANDATORY`: 2 `REUSED_EQUIVALENT`, 39 `STALE`, 135 `NOT_TESTED`;
- `DATA_DEPENDENT`: 2 `REUSED_EQUIVALENT`, 3 `STALE`, 146 `NOT_TESTED`.

## AF-05 — Primo lotto reale (`batch1_plan.json`, NON eseguito)

**Dimensione e copertura:**
- 78 chiamate, ciascuna con `max_attempts = 1`, cioè `RetryPolicy(max_attempts=1)`: 78 tentativi in totale, uguale a `HARD_CAP`.
- Coprono 133 delle 174 righe `MANDATORY` ancora senza evidenza valida (135 righe in totale).
- Le 41 righe escluse sono elencate in `mandatory_pending_not_covered`: sono parametri opzionali secondari, per esempio la maggior parte dei parametri opzionali di `headtohead`, `trophies` e `sidelined` (`coach`, `coachs`, `players`).

**Ordine e attribuzione:**
- Il piano parte dai cataloghi, poi leghe, squadre, classifiche, round e head-to-head.
- Segue `/fixtures` con le regole `F-*`, poi le sotto-rotte della partita, giocatori e paginazione (pagina 2 e una pagina oltre il totale), top, transfer e trofei, cataloghi delle quote.
- Gli id che cambiano nel tempo (partita, squadra di casa, giocatore, totale pagine) si leggono da risposte precedenti tramite binding. Un binding non risolto salta la chiamata con 0 tentativi.
- Le chiamate con più filtri opzionali hanno il tag `JOINT_ATTRIBUTION`: l'attribuzione è congiunta e più debole.

**AF-02:**
- `C08 countries(search="En")` è una sola chiamata. La risposta del provider decide se il contratto deve imporre il minimo documentato, con una PR separata.
- Per il tipo di `odds/bets.id` non è prevista nessuna chiamata. Sul filo un intero e una stringa producono lo stesso testo di query, quindi una chiamata reale non li distingue. Resta `DOCS_UNVERIFIED`; l'accettazione di `bets.id` è coperta da `C73`.

### Budget persistente (condizione A) — serve una decisione dell'owner

`scripts/apifootball_cert_batch1.py` conta i tentativi in modo write-ahead:
- un wrapper del transport httpx, posto sotto il client resiliente reale, riserva e committa ogni tentativo nel ledger di campagna **prima** dell'invio;
- retry, timeout e risposte ambigue sono quindi sempre contati;
- un riavvio o un re-run con lo stesso `campaign_id` continua lo stesso budget e non ritenta le chiamate già tentate.

Ogni richiesta prende anche il lease del coordinatore di quota condiviso (`PostgresQuota`).

**Lo schema adatto non esiste.** Le tabelle del coordinatore (`api_football_quota_*`) hanno un altro significato (finestre per chiave): riusarle cambierebbe la loro ownership (CLAUDE.md §5). Per questo il ledger PostgreSQL rifiuta con `CAMPAIGN_LEDGER_MIGRATION_REQUIRED` prima di qualunque richiesta.

Migration proposta, **non scritta in `sql/migrations/`, decisione owner**:

```sql
CREATE TABLE api_football_cert_campaigns (
  campaign_id TEXT PRIMARY KEY, budget INTEGER NOT NULL CHECK (budget BETWEEN 1 AND 78),
  plan_sha256 TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp());
CREATE TABLE api_football_cert_campaign_attempts (          -- append-only (trigger anti UPDATE/DELETE)
  campaign_id TEXT NOT NULL REFERENCES api_football_cert_campaigns,
  seq INTEGER NOT NULL CHECK (seq BETWEEN 1 AND 78), call_id TEXT NOT NULL,
  reserved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY (campaign_id, seq));
```

Il `CHECK (seq <= 78)` rende il tetto vincolante anche nel database. Servono anche i grant `INSERT` e `SELECT` per il ruolo del coordinatore (WP-009).

### Workflow proposto

Il file è `.github/workflows/api-football-cert-campaign.yml`: preparato, **dispatch NON autorizzato**. È meglio di un passaggio via MCP per tre motivi:
- il runner MCP E2E (`fcb-mcp-e2e`) addebita il caso peggiore, 3 tentativi per chiamata, quindi entro 78 farebbe solo 26 chiamate;
- il suo tetto approvato è 45;
- non può fissare `max_attempts = 1` sul server.

Il runner in-process esegue gli stessi tool di `tools.py` (contratti, client resiliente, transport, coordinatore). Lo strato JSON-RPC e OAuth resta coperto dai test offline e da `fcb-mcp-e2e`.

**Protezioni del workflow:**
- solo `workflow_dispatch` e solo da `main`;
- dispatch dell'owner (`require_owner_main_dispatch.sh`);
- Environment protetto `api-football-live` con revisori (`require_protected_environment.sh`);
- variabile `APIFOOTBALL_CERT_CAMPAIGN_AUTHORIZED=true` richiesta, che l'owner non ha impostato;
- limite giornaliero dalla variabile dell'owner `APIFOOTBALL_CERT_MAX_DAILY_REQUESTS`; RPM 4, concorrenza 1;
- `::add-mask::` su chiave, DSN e componenti del DSN;
- scansione anti-segreti degli artefatti, che vengono cancellati se trova qualcosa;
- un JSON per ogni requisito.

**Secret necessari** (già referenziati dai workflow esistenti nell'Environment `api-football-live`; i valori non sono stati letti):
- `APIFOOTBALL_API_KEY`;
- `APIFOOTBALL_COORDINATOR_DATABASE_URL`.

Da creare a cura dell'owner:
- la variabile `APIFOOTBALL_CERT_CAMPAIGN_AUTHORIZED`;
- la variabile `APIFOOTBALL_CERT_MAX_DAILY_REQUESTS`;
- la migration sopra.

## LIVE FULL MATCH SNAPSHOT (campagna `live_full_match_snapshot`, NON eseguita)

**Fonti**, tutte per lo stesso `fixture_id` e verificate in `contracts.py`:
- `apifootball_fixtures(id)`;
- `apifootball_fixture_statistics`;
- `apifootball_fixture_events`;
- `apifootball_fixture_lineups`;
- `apifootball_fixture_players`;
- `apifootball_live_odds(fixture)`;
- `apifootball_coverage_report(id=lega, season)`, usata come evidenza di disponibilità.

`fixtures(live="all")` si usa solo se non viene passato un `fixture_id`, e costa un tentativo in più.

**Budget:** tetto configurato di 8 tentativi per `snapshot_id`, nello stesso ledger persistente. È un tetto, non un'autorizzazione: al raggiungimento lo snapshot si chiude `SNAPSHOT_INCOMPLETE` e non fa altri tentativi.

**Strumenti:** `scripts/apifootball_live_snapshot.py` costruisce il rapporto unico da risposte registrate. `tests/test_apifootball_live_snapshot.py` lo prova offline su fixture sintetiche:
- risposte vuote, null, manifest PARTIAL, partita senza lineups;
- `PLAN_EXCLUDED`, timeout, retry, riavvio, tetto;
- risposta manomessa, assenza del raw, timestamp senza timezone.

**Requisiti** (righe `SNAPSHOT` della matrice, tutte `DATA_DEPENDENT`):

| Riga | Garanzia |
|---|---|
| `SNAP-RAW-PRESERVED` | Si conservano la risposta MCP così come ricevuta e il corpo originale del provider, catturato al transport fino a 16 MiB. |
| `SNAP-FIELD-COMPLETENESS` / `SNAP-NULL-PRESERVED` | Confronto deterministico campo per campo, originale contro rapporto: tipo e valore stretti, null resta null, nessun campo perso. |
| `SNAP-IDENTITY-WITH-ORIGINAL` | Senza cattura del raw la fonte è dichiarata `NOT_VERIFIABLE`, mai affermata; il verdetto complessivo diventa `SNAPSHOT_INCOMPLETE`. |
| `SNAP-TRANSFORMATIONS-LISTED` | Per ogni fonte si elencano, con il percorso del campo, le trasformazioni del bridge verificate nel codice (vedi elenco sotto). |
| `SNAP-TIMESTAMPS` / `SNAP-NOT-SIMULTANEOUS-DECLARED` | Inizio e fine di ogni richiesta in UTC e Europe/Rome, finestra di raccolta (primo inizio → ultima fine), dichiarazione esplicita che le fonti **non** sono simultanee. Il minuto è quello riportato dal provider all'istante della richiesta. |
| `SNAP-DERIVED-CONSISTENCY` | `goals` contro gli eventi `Goal` solo come controllo `DERIVED`; i dati del provider non vengono mai riscritti; l'HT non viene mai derivato. |
| `SNAP-LIVE-ODDS-AS-PUBLISHED` | Bookmaker, mercati, valori e sospensioni come pubblicati, con etichetta `MARKET_PRICES_AS_PUBLISHED_NOT_MODEL_PROBABILITIES`. |
| `SNAP-AVAILABILITY-CLASSIFIED` | `EMPTY_VALID`, `PROVIDER_NOT_OFFERED` (flag di copertura false) o `PLAN_EXCLUDED`. |
| `SNAP-PARTIAL-NOT-CERTIFIED` | Una sola fonte `PARTIAL` porta a `SNAPSHOT_PARTIAL`, con il manifest per recuperare il resto. |
| `SNAP-BUDGET-CAP-CLOSES-INCOMPLETE` | Raggiunto il tetto, lo snapshot si chiude `SNAPSHOT_INCOMPLETE`. |

**Trasformazioni del bridge verificate nel codice:**
- parsing `json.loads`: la formattazione numerica non è conservata e una chiave duplicata tiene l'ultimo valore;
- `errors` del provider ridotto alle sole chiavi;
- manifest oltre 256 KiB;
- metadati del bridge;
- etichette `DERIVED` accanto ai dati: `combination` e `live_odds_state`.

Fuori scope: raccolta continuativa delle quote, storico, calcolo del drop.

## Divergenze tra contratto e snapshot (`CONSTRAINT_DRIFT`, non corrette)

- **Lunghezza minima di `search`.** Lo snapshot indica 3 caratteri per `countries`, `bets`, `bookmakers`, `coaches` e `venues`; il contratto ne ammette 1. La chiamata `C08` del lotto serve a decidere.
- **Tipo di `odds/bets.id`.** La documentazione lo dà come `string`, il contratto come `integer`.
