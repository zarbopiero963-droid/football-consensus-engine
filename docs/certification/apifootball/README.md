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

**Modifiche tra 848c007 e la base del branch sul percorso di codice API-FOOTBALL** (`bridge/api_football/**`, `bridge/server.py`, `bridge/db_identity.py`, `requirements.txt`): `e155193` (#193), che ha toccato `contracts.py`, `stream_scan.py` e `tools.py`; e #198 (squash `a46ba72` su main), che in `bridge/server.py` aggiunge solo campi di `/healthz` e log di avvio del throttle OAuth, fuori dal percorso dei tool API-FOOTBALL. Gli altri commit del periodo (`6615acb`, `6c69521`, `d09571f`, `f666cc1`) non toccano quel percorso.

**Come è provata l'equivalenza** (`tests/test_apifootball_cert_reuse_equivalence.py`), in due passi:
1. `git diff --name-only 848c007 HEAD` sul percorso di codice deve coincidere con i file registrati. Qualunque nuova modifica invalida i riusi.
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

### AF-07 — Contatore persistente di campagna (migration bridge 0010)

`scripts/apifootball_cert_batch1.py` conta i tentativi in modo write-ahead. Un wrapper del transport httpx, posto sotto il client resiliente reale, fa tre cose **prima** di ogni invio:
1. verifica la variabile dell'owner `APIFOOTBALL_CERT_CAMPAIGN_AUTHORIZED=true`;
2. apre una transazione READ COMMITTED esplicita e inserisce il tentativo; il trigger della 0010 incrementa, nella stessa transazione, il contatore del piano e assegna `seq`;
3. committa.

Ne seguono tre garanzie:
- retry, timeout e risposte ambigue sono sempre contati;
- un riavvio o un nuovo dispatch con lo stesso `campaign_id` continua lo stesso budget e non ritenta le chiamate già tentate;
- ogni richiesta prende anche il lease del coordinatore di quota condiviso (`PostgresQuota`), che resta **intatto**.

**Numero e dipendenza (AF-11, decisione owner).** La PR #198, mergiata dall'owner su main come squash `a46ba72`, introduce la 0009 (`0009_oauth_throttle_buckets.sql`). Questa migration è quindi la **0010** (`sql/migrations/bridge/0010_apifootball_cert_campaign_ledger.sql`).

`app/migrations.py` esige una numerazione consecutiva: il branch è ribasato su main `a46ba72` e la serie è 0001..0010 senza buchi.

In **codice**, #198 è su main. Il **database** di produzione è un'altra cosa: la 0009 va applicata dall'owner con il suo workflow.

Prima di applicare la 0010 in produzione va **verificato** che la 0009 sia già applicata. Il workflow 0010 lo verifica due volte:
- nel precheck in sola lettura, che accetta solo le righe 0001-0009 (apply) o 0001-0010 (no-op);
- dentro la transazione di scrittura, prima dello script bridge, con `sql/aiven/apifootball_cert_ledger_0010_require_0009.sql`. Serve perché lo script 0001-0010 applicherebbe altrimenti anche una 0009 mancante. Il controllo esige la riga 0009 con il checksum del repository e rifiuta con `FCB_CERT_LEDGER_REQUIRES_BRIDGE_0009`.

Se la 0009 non risultasse applicata in produzione, la 0010 non si applica. Una eventuale rinumerazione si fa solo dopo aver ricontrollato la baseline reale della produzione, non in questa PR.

**Contenuto della migration:**
- è additiva e non tocca tabelle esistenti, in particolare `api_football_quota_*`;
- crea cinque tabelle dedicate:
  - `api_football_cert_plan_usage` (review #199 P1): il **contatore persistente per piano** (`used`, 0..budget). Viene creato a 0 con il piano, cresce solo di 1 e non si può cancellare. `fcb_app` non ha alcun privilegio su questa tabella;
  - `api_football_cert_plans` (AF-15): registro append-only dei piani **approvati dall'owner**. Contiene `plan_sha256` (chiave), il tipo (`BATCH1` o `LIVE_FULL_MATCH_SNAPSHOT`), il budget 1..78 (al massimo 8 per lo snapshot), `approved_by`, `github_run_id`, e infine `approved_role` e `approved_at` impostati dal server;
  - `api_football_cert_campaigns`: ogni `campaign_id` è legato a un piano approvato (FK) e non cambia più;
  - `api_football_cert_campaign_attempts`: `seq` 1..78, chiave `(campaign_id, seq)`;
  - `api_football_cert_campaign_outcomes`: al massimo un esito per tentativo, con FK sul tentativo;
- trigger append-only sulle quattro tabelle del registro: UPDATE, DELETE e TRUNCATE sono rifiutati per **ogni** ruolo, owner compreso;
- timestamp di audit (`approved_at`, `created_at`, `reserved_at`, `recorded_at`) e `approved_role` imposti dal server: un trigger `BEFORE INSERT` li sovrascrive con `clock_timestamp()`, anche via COPY;
- **budget per piano (AF-15, P1 della re-review #199)**: la garanzia vale **con qualunque livello di isolamento**. Il trigger di prenotazione è `SECURITY DEFINER` (proprietario `fcb_migrator`, `search_path = pg_catalog, pg_temp`, nomi qualificati con lo schema della tabella). Nella **stessa transazione** del tentativo esegue `UPDATE ... SET used = used + 1 WHERE plan = $1 AND used < budget RETURNING used`, e il `seq` del tentativo è quel valore. Se nessuna riga viene aggiornata, la prenotazione è rifiutata: `BUDGET_EXHAUSTED`, oppure `PLAN_NOT_APPROVED` se il piano non ha contatore.
  - In READ COMMITTED l'aggiornamento concorrente attende il lock della riga e ricontrolla il valore committato. In REPEATABLE READ o SERIALIZABLE fallisce con un errore di serializzazione (40001) e non prenota nulla.
  - Rollback e processi uccisi annullano tentativo e contatore insieme, quindi `used` è sempre uguale ai tentativi committati del piano. I `seq` del piano sono esattamente 1..`used`, senza duplicati né riusi.
  - Il trigger rifiuta di funzionare fuori dal suo uso (`GUARD_MISUSE`). Nessun lock advisory e nessun controllo sull'isolamento: il contatore li rende superflui, e un controllo sull'isolamento impedirebbe di dimostrare la garanzia sotto RR e SERIALIZABLE.
  - Il runner apre comunque ogni transazione del ledger in READ COMMITTED esplicito. Riporta `SERIALIZATION_FAILURE` (40001/40P01), distinto da `CAMPAIGN_BUDGET_EXHAUSTED`. Riprova al massimo 3 volte, ogni volta in una **nuova** transazione, ricontrollando autorizzazione owner e piano approvato, entro lo stesso budget;
- le tre funzioni hanno `SET search_path = pg_catalog, pg_temp` e raggiungono le tabelle solo tramite lo schema della tabella che le ha attivate (`TG_TABLE_SCHEMA`, cioè `public` in produzione). Tabelle omonime in `pg_temp` (piani, campagne, tentativi) con budget più alto non possono quindi aggirare il budget reale;
- trigger di budget: `seq` deve essere il numero successivo ed entro il budget.

**Tentativo PRENOTATO e richiesta INVIATA sono distinti.** La prenotazione è committata prima dell'invio. L'esito si registra dopo lo scambio, in una tabella separata e append-only, senza nessun UPDATE. Ogni tentativo prenotato finisce in uno di tre stati:

| Stato | Significato |
|---|---|
| `RESPONSE_RECEIVED` | il provider ha risposto; lo stato HTTP è registrato |
| `TRANSPORT_ERROR` | l'invio è fallito (timeout, reset); la richiesta **può** essere arrivata al provider |
| `RESERVED_UNKNOWN` | nessun esito: il processo si è fermato dopo la prenotazione, prima, durante o dopo l'invio |

Un tentativo `RESERVED_UNKNOWN` consuma budget e non viene mai ritentato né recuperato in automatico. L'evidenza lo riporta così com'è:
- il run riporta `ledger_attempt_states` e `ledger_attempts`;
- le chiamate non ritentate portano `ledger_state`;
- lo snapshot riporta `budget.attempt_states`.

Se la scrittura dell'esito fallisce, il tentativo resta `RESERVED_UNKNOWN`: è la scelta conservativa.

Una chiamata che non ha prenotato nulla (rifiutata prima dell'invio per autorizzazione, ledger o budget, oppure fermata prima dal client, per esempio `CIRCUIT_OPEN`) non ha inviato nulla. È `NOT_TESTED` con `outcome=REFUSED_BEFORE_SEND` e `refusal_code`, mai `ERROR`. Le chiamate con almeno un tentativo prenotato hanno `outcome=ATTEMPTED`.

**Piani approvati (AF-15).**
- **Identità del piano.** Il piano è identificato dallo SHA-256 del file committato su main: `docs/certification/apifootball/batch1_plan.json` oppure `live_snapshot_plan.json`. Il runner calcola lo SHA dal file committato. Se il file non è esattamente quello che il codice genera, rifiuta con `PLAN_OUT_OF_DATE`.
- **Piano non approvato.** Se il piano non è nel registro, il runner si ferma prima di qualunque richiesta con `PLAN_NOT_APPROVED`. Rigenerare o modificare il piano dà un altro SHA, che non è approvato: non serve a ottenere budget.
- **Approvazione.** `.github/workflows/fcb-aiven-defaultdb-apifootball-cert-plan-approve.yml` (preparato, NON eseguito) ha gli stessi gate del workflow 0010:
  - Environment `aiven-production` e dispatch dell'owner da main;
  - `mode=approve` con la frase `APPROVE API-FOOTBALL CERT PLAN ON defaultdb`;
  - target pinnato, verify-full, identità `fcb_migrator`, 0001-0010 registrate;
  - SQL pinnato `sql/aiven/apifootball_cert_plan_approve.sql`.
- **Controlli dell'approvazione.** L'input `plan_sha256` deve coincidere con lo SHA del file committato, e quel file deve essere aggiornato (`--check-plan`). Budget e tipo vengono dal file, non dagli input. L'inserimento è append-only (`ON CONFLICT DO NOTHING`): un piano già approvato non viene mai rinnovato, e un budget diverso per lo stesso SHA viene rifiutato (`FCB_CERT_PLAN_CONFLICT`).
- **Perché un workflow separato.** È la soluzione più semplice e sicura: l'applicazione della 0010 resta una sola operazione, e ogni nuova autorizzazione di budget è un atto owner distinto e verificabile.
- **Snapshot live.** Lo snapshot ha un suo piano fisso, `live_snapshot_plan.json`, senza fixture né `snapshot_id`. Il suo budget approvato (8) è condiviso da **tutti** gli `snapshot_id`: un nuovo snapshot non rinnova nulla. Per un altro budget serve un nuovo piano committato, con un nuovo SHA, e una nuova approvazione.
- **Ruolo runtime.** `fcb_app` ha solo `SELECT` sui piani.

**Rifiuti prima dell'invio (AF-18).** Una chiamata che non ha prenotato nulla è `NOT_TESTED` con `refusal_code`, mai `ERROR`. Può essere stata rifiutata dal transport (autorizzazione, ledger, budget, piano) oppure fermata prima dal client, per esempio `CIRCUIT_OPEN`.

**Readiness.** La 0010 è sempre "deferred" in `bridge/schema_readiness.py`, perché il runtime non usa queste tabelle: un database senza la 0010 resta READY. Una volta registrata la 0010, sono richieste tutte e cinque le tabelle (AF-19), contatore compreso. Il runtime non applica mai le migration, e un test lo verifica.

**Privilegi (AF-12, decisione owner).** `fcb_app` ha `SELECT` sulle quattro tabelle e `INSERT` **a livello di colonna** solo sulle colonne che scrive, mai sui timestamp di audit e mai sui piani. Non ha UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, ALTER o DROP.
- `fcb_app` non è owner delle tabelle (lo è `fcb_migrator`) e non è membro di `fcb_migrator`. Quindi non può fare ALTER né disabilitare i trigger, e non può impostare `session_replication_role`.
- I trigger rifiutano le modifiche anche a chi ha i privilegi, owner compreso.
- Limite dichiarato: `fcb_app` è **condiviso** con gli altri componenti del runtime. Un ruolo dedicato al ledger resta un obiettivo futuro.

**Compatibilità WP-009 (AF-13, modifica intenzionale).** `grant_bridge_runtime.sql` e `finalize_bridge_privileges.sql` hanno ora un blocco `DO $certledger$` identico.
- Il blocco concede gli stessi privilegi del file di grant 0010 (`SELECT` più `INSERT` per colonna) **solo se le tabelle esistono**; su un database 0001-0009 non cambia nulla, come il blocco della 0009.
- Gli SHA pinnati in `.github/workflows/fcb-aiven-defaultdb-roles.yml` sono aggiornati.
- Una riesecuzione dei file di grant WP-009 dopo la 0010 lascia i privilegi del ledger invariati.
- **Però** oggi il workflow ruoli WP-009 rifiuta qualunque database con `schema_migrations` diverso da 0001-0008. Su un database 0009/0010 quindi il blocco **non viene raggiunto tramite il workflow**, finché il modello di verifica WP-009 non viene aggiornato (PR dedicata, decisione owner). Fino ad allora l'unico percorso di grant è il file 0010.

**Limite dichiarato del modello WP-009 (non cambiato in questa PR).** Il modello di verifica `verify_bridge_roles.sql` resta quello di 0001-0008 (routine, trigger e allowlist pinnati). Lo stesso vale per i conteggi e le righe `schema_migrations` pinnati nel workflow ruoli.
- Su un database con 0009 il modello segnala in più `unexpected_relation_grant`; questo viene da #198.
- La 0010 aggiunge `routine_set`, `trigger_set`, `expression_functions` e `column_grant`: tre funzioni con `search_path` fissato, sette trigger, i default `clock_timestamp()` e i grant `INSERT` per colonna.
- Il workflow ruoli, su un database 0009/0010, **rifiuta prima di scrivere** (fail-closed).
- Aggiornare quel modello è una decisione owner separata.

**Applicazione** (preparata, NON eseguita, AF-14): `.github/workflows/fcb-aiven-defaultdb-apifootball-cert-ledger-0010.yml`, derivato dal workflow 0009 con gli stessi gate:
- Environment `aiven-production` e dispatch dell'owner da main;
- `mode=apply` con la frase `APPLY BRIDGE 0010 API-FOOTBALL CERT LEDGER TO defaultdb`;
- target pinnato, verify-full, identità `fcb_migrator`;
- precheck 0001-0009 (apply) o 0001-0010 (no-op), più il controllo 0009 nella transazione;
- backup schema-only prima della scrittura;
- SQL pinnato SHA-256: `aiven_migrations_bridge_0001_0010.sql`, `apifootball_cert_ledger_0010_require_0009.sql` e `apifootball_cert_ledger_0010_grant.sql`;
- una transazione eseguita due volte;
- grant a `fcb_app`: `SELECT` e `INSERT` per colonna, con l'insieme esatto delle colonne verificato nella stessa transazione.

L'applicazione della 0010, il dispatch della campagna e lo snapshot reale restano azioni dell'owner, **non autorizzate**; nulla li esegue in automatico.

**Test:**
- unit: `tests/test_apifootball_cert_ledger_workflow.py` e `tests/test_apifootball_cert_batch1.py` (crash simulati);
- integrazione su PostgreSQL 18 usa e getta: `tests/integration/test_apifootball_cert_ledger_postgres.py`. Copre:
  - la 0010 su un database già a 0009 con dati;
  - append-only e budget nel database;
  - riavvio e re-run;
  - **6 processi concorrenti** (mai oltre il tetto);
  - runner senza 0010 (`CAMPAIGN_LEDGER_MIGRATION_REQUIRED`, 0 richieste);
  - **processi uccisi** tra prenotazione e invio, e tra invio ed esito: `RESERVED_UNKNOWN`, budget consumato, chiamata non ritentata;
  - esiti append-only;
  - **`fcb_app` reale** nella sequenza dei file WP-009: fase 1, sua riesecuzione, fase 2, grant 0010 come `fcb_migrator`, riesecuzione della fase 2. I privilegi restano identici; UPDATE, DELETE, TRUNCATE, ALTER, DISABLE TRIGGER, DROP e `session_replication_role` sono rifiutati;
  - come `fcb_app`: una tabella ombra in `pg_temp` non aggira il budget, e i timestamp di audit non si impostano né via INSERT né via COPY; per chi ha il privilegio, il valore viene sovrascritto;
  - readiness: con la 0010 registrata, la mancanza della tabella `_outcomes` dà `MISSING_TABLES`;
  - i file WP-009 su un database 0009 senza 0010, senza errori;
  - il controllo 0009 (rifiuta 0008, accetta 0009 e 0010);
  - il differenziale del modello di verifica WP-009.

**Coerenza contatore-ledger** (`_assert_counter_matches_ledger`: `used` è uguale ai tentativi committati del piano, `seq` = 1..`used`, mai oltre il budget). La verifica viene fatta dopo:
- crash e rollback: `test_af15_concurrent_..._kills_and_aborts`, sia dopo la fase dei processi uccisi sia alla fine;
- retry dopo 40001: `test_p1_original_defect_...`, `test_p1_runner_reports_...`;
- concorrenza tra campagne in RC, RR e SERIALIZABLE: `test_p1_nine_processes_...`;
- `test_ledger_is_append_only_...`, `test_invariant3_...`, `test_p1_fcb_app_cannot_touch_...`.

**Test delle tre invarianti owner** (`tests/integration/test_apifootball_cert_ledger_postgres.py`, salvo dove indicato):

1. **Piani immutabili e legati al file committato:**
   - `test_invariant1_fcb_app_cannot_create_change_delete_or_replace_an_approved_plan`, eseguito con un **login reale** come `fcb_app` (credenziali di test, niente `SET ROLE`). Copre INSERT, `ON CONFLICT DO UPDATE`, UPDATE, DELETE, TRUNCATE, ALTER, DROP, COPY, delete+insert da parte dell'owner e un piano ombra in `pg_temp`;
   - `test_invariant1_plan_approval_sql_registers_once_never_renews_and_only_as_fcb_migrator`;
   - `test_af15_a_new_campaign_id_of_an_approved_plan_gets_no_new_budget_and_unapproved_plans_send_nothing`, con il piano modificato e quindi non approvato;
   - unit (`tests/test_apifootball_cert_batch1.py`): `test_execute_refuses_when_the_committed_plan_file_differs_from_the_code`, `test_plan_approval_request_prints_the_committed_plan_sha_budget_and_kind`;
   - unit (`tests/test_apifootball_cert_ledger_workflow.py`): `test_af15_plan_approval_workflow_is_owner_gated_pinned_and_binds_the_committed_plan`, `test_af15_approval_sql_is_append_only_and_never_renews_a_plan`.
2. **Budget atomico (indipendente dall'isolamento, P1):**
   - `test_p1_nine_processes_as_fcb_app_never_exceed_the_plan_budget_at_any_isolation[READ COMMITTED|REPEATABLE READ|SERIALIZABLE|RUNNER]`: 9 processi reali, autenticati come `fcb_app` con SCRAM (`require_auth=scram-sha-256`), su 9 `campaign_id` dello stesso piano. Lo snapshot è preso prima dell'insert, e i 40001 vengono ritentati in nuove transazioni. `RUNNER` usa il default di ruolo `repeatable read` impostato da `fcb_app` stesso. Esito: esattamente il budget, e il contatore coincide con il ledger;
   - `test_p1_original_defect_snapshot_before_another_commits_is_a_serialization_failure_not_an_overrun[REPEATABLE READ|SERIALIZABLE]`: riproduce **esattamente** il difetto originale. T1 prende lo snapshot, T2 prenota e committa, poi T1 prenota e riceve un errore 40001, non un superamento. Il retry in una nuova transazione dà `BUDGET_EXHAUSTED`;
   - `test_p1_runner_reports_serialization_failure_and_budget_exhausted_with_distinct_codes`: i due codici sono distinti; il retry avviene in una nuova transazione e ricontrolla l'autorizzazione;
   - `test_p1_fcb_app_cannot_touch_the_counter_or_call_the_functions`: SELECT, UPDATE, INSERT, DELETE, TRUNCATE e ALTER sul contatore sono rifiutati, così come le chiamate dirette alle funzioni e un trigger che prova a riusare la funzione su una tabella temporanea. Nemmeno l'owner può decrementare il contatore;
   - `test_fcb_app_login_is_scram_authenticated`: login SCRAM reale; una password errata viene rifiutata.
   - `test_af15_concurrent_campaigns_of_one_plan_never_exceed_the_plan_budget_with_kills_and_aborts`, con processi reali. Distingue le prenotazioni committate, che restano e contano, da quelle in transazioni mai committate o annullate, che non lasciano righe né consumano budget;
   - `test_concurrent_processes_never_exceed_the_cap`;
   - `test_a_killed_process_leaves_reserved_unknown_counted_and_never_retried`;
   - `test_af15_snapshot_budget_belongs_to_its_approved_plan_and_is_not_renewed_by_a_new_snapshot_id`.
3. **Evidenze preservate:**
   - `test_invariant3_a_reserved_attempt_cannot_vanish_be_reused_or_be_rewritten_as_never_made`, con login reale come `fcb_app`;
   - `test_outcomes_are_append_only_one_per_reserved_attempt`;
   - `test_ledger_is_append_only_and_the_database_enforces_the_budget`;
   - `test_a_killed_process_leaves_reserved_unknown_counted_and_never_retried`.

**Decisioni owner aperte:**
1. Ruolo dedicato al ledger al posto di `fcb_app` condiviso (obiettivo futuro).
2. Aggiornamento del modello di verifica e dei conteggi del workflow ruoli WP-009 per i database 0009/0010.
3. In produzione: prima applicare la 0009 (workflow #198), poi la 0010.

### AF-08 — Guardia `live_harness` (modifica intenzionale di un test esistente)

`tests/test_api_football_live_harness.py` ammette due percorsi verso il provider:
- `live_harness.coordinated_client`, per i tre script storici;
- il runner ufficiale della campagna.

Il nome del file non basta. `_runner_guard_failures()` verifica sul comportamento che il runner:
- non invia nulla senza la variabile dell'owner;
- non invia nulla se la prenotazione del budget fallisce;
- traccia ogni richiesta nel ledger entro il tetto.
- registra l'esito di ogni richiesta che ha avuto risposta.

**Test di mutazione:** una copia del runner senza la prenotazione, oppure senza il controllo di autorizzazione, fa **fallire** la guardia.

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
- variabile `APIFOOTBALL_CERT_CAMPAIGN_AUTHORIZED=true` richiesta (verificata anche dal runner prima di ogni richiesta), che l'owner non ha impostato;
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
- l'applicazione della migration 0010 tramite il suo workflow (dopo la 0009).

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
