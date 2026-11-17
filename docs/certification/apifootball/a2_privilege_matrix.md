# PATH A2 — privilegi di `fcb_cert_runner` su `defaultdb` (variante R1, decisione owner D2)

**Stato:** variante integrata nella PR #201 tramite il branch `claude/a2-r1-quota-rpc` (ratifica owner FCB-A2-R1-WP009-DECISIONS). Nessuna installazione in produzione. I 9 privilegi diretti del runner sono **eliminati** e sostituiti dalle 4 funzioni controllate; i limiti condivisi 8/min, 80/giorno e 2 concorrenti sono confermati come massimi fissi.

Precheck del restore, guard di sessione generati, `$GUARD` dei workflow restore e ruoli, guard del backup e `verify_bridge_roles.sql` accettano `fcb_cert_quota` **solo** nello stato EXACT della query canonica; schema assente = comportamento di prima; ogni altro stato è rifiutato. Vedi "Compatibilità".

Fonti:
- `sql/aiven/apifootball_cert_path_a_quota_schema.sql`: step 1a, amministratore. Crea lo schema vuoto `fcb_cert_quota`, di proprietà del proprietario delle tabelle di quota.
- `sql/aiven/apifootball_cert_path_a_quota_functions.sql`: step 1b, proprietario delle tabelle (`fcb_migrator` dopo la fase 2). Crea le 4 funzioni e concede EXECUTE e USAGE.
- `sql/aiven/apifootball_cert_path_a_quota_assert.sql`: verifica dello stato esatto, nella stessa transazione dello step 1b e dopo ogni rotazione della password.
- `sql/aiven/apifootball_cert_quota_state.sql`: query canonica di sola lettura (`ABSENT`/`EXACT`/`DRIFT`). È incorporata byte per byte nell'assert e il runner la usa prima di qualsiasi richiesta.

I file della variante a 9 privilegi (`apifootball_cert_path_a_coordinator_grants.sql` e `_assert.sql`) sono **rimossi**, insieme all'eccezione dei 9 privilegi in precheck, impronte e `verify_bridge_roles.sql`.

## Matrice R1

| # | Oggetto | Privilegio | Concesso da |
|---|---|---|---|
| 1 | schema `fcb_cert_quota` | `USAGE` | proprietario delle tabelle |
| 2 | `fcb_cert_quota.reserve(text)` | `EXECUTE` | idem |
| 3 | `fcb_cert_quota.release(text, text)` | `EXECUTE` | idem |
| 4 | `fcb_cert_quota.snapshot(text)` | `EXECUTE` | idem |
| 5 | `fcb_cert_quota.mark_exhausted(text, text)` | `EXECUTE` | idem |
| — | `fcb_cert_quota.pinned_key_hash()` | nessuno (EXECUTE solo al proprietario) | idem |

Il runner non ha **nessun** privilegio, né di tabella né di colonna, su `api_football_quota_counters`, `api_football_quota_leases` e `api_football_quota_attempts`, né su altre relazioni di `public`. Non ha CREATE né su `public` né su `fcb_cert_quota`, e **nessuna voce** sullo schema `public`: con la fase 1 WP-009 (PUBLIC senza USAGE su `public`) non ha nemmeno USAGE su `public`. Le funzioni `SECURITY DEFINER` non ne hanno bisogno; lo step 1 revoca un'eventuale USAGE esplicita della variante precedente. Il pre-flight del runner usa una ricerca nel catalogo al posto di `to_regclass('public.…')`, che senza USAGE fallirebbe. VERIFIED su PostgreSQL 18.6 (suite R1 con PUBLIC senza USAGE, sequenza completa WP-009 → R1).

## Le funzioni

Ogni funzione:
- è `SECURITY DEFINER` e appartiene al proprietario delle tabelle;
- ha `SET search_path = pg_catalog, pg_temp` e relazioni qualificate con lo schema;
- ha EXECUTE revocato a PUBLIC e concesso solo a `fcb_cert_runner`, senza grant option;
- rifiuta se `SESSION_USER` non è `fcb_cert_runner`. Dentro una SECURITY DEFINER `CURRENT_USER` è il proprietario, per questo si usa `SESSION_USER`: chi fa `SET ROLE fcb_cert_runner` viene rifiutato.

| Funzione | Replica di `PostgresQuota` | Restrizioni in più |
|---|---|---|
| `reserve(key_hash)` → `lease_id` | `_reserve`: stesso lock advisory per chiave, potatura dei lease scaduti e dei tentativi fuori finestra, controlli nell'ordine giorno → finestra mobile → provider-zero → bucket, stessi upsert e insert | limiti fissi nel corpo: 8 al minuto, 80 al giorno UTC, 2 lease, TTL 180 s; lease con prefisso `cert-` |
| `release(key_hash, lease_id)` | `_release` | cancella solo un lease `^cert-[0-9A-Za-z_-]{32}$` nominato esattamente, della chiave nominata: al massimo 1 riga |
| `snapshot(key_hash)` | `_snapshot` | limiti fissi |
| `mark_exhausted(key_hash, kind)` | `_mark_exhausted`: `GREATEST` sui contatori e 8 slot provider-zero | può solo alzare i contatori |

`key_hash` deve rispettare `^[0-9a-f]{64}$`, cioè lo SHA-256 della chiave come in `_key_hash`. Il runner non invia mai la chiave. Nessuna firma accetta limiti come argomenti.

I limiti producono messaggi fissi:
- `FCB_CERT_QUOTA_BUSY`;
- `FCB_CERT_QUOTA_DAY <epoch>`;
- `FCB_CERT_QUOTA_MINUTE rolling|provider_zero|bucket <s>`.

`CertQuotaCoordinator` (`scripts/apifootball_cert_batch1.py`) li riconverte in `QuotaConcurrencyBusy` e `QuotaExceeded`, con gli stessi messaggi e la stessa finestra di `PostgresQuota`. Ogni altro errore produce `QUOTA_COORDINATOR_UNAVAILABLE` e non parte nessuna richiesta.

## Esclusioni verificate

Ogni manomissione porta lo stato a `DRIFT` e l'assert rifiuta con `FCB_CERT_PATH_A_QUOTA_STATE_UNEXPECTED`. I 22 casi sono in `tests/integration/test_apifootball_cert_a2_r1_postgres.py::TAMPER`:
- funzioni: funzione in più, corpo cambiato, `SECURITY INVOKER`, `search_path` rimosso o cambiato, `STRICT`;
- EXECUTE: a PUBLIC, a un altro ruolo, con grant option;
- tabelle di quota: privilegio di tabella o di colonna al runner o a PUBLIC;
- schema: proprietario della funzione o dello schema cambiato, CREATE sullo schema, USAGE a PUBLIC, tabella, tipo o default ACL nello schema;
- runner: non "plain" (attributi o membership), CREATE su `public`.

Il runner rifiuta prima di qualsiasi richiesta:
- nel pre-flight di `main()`, con `CAMPAIGN_REFUSED COORDINATOR_FUNCTIONS_NOT_EXACT`;
- oppure in `prepare()` del coordinatore, con `QUOTA_COORDINATOR_UNAVAILABLE`.

## Rischio R1: cosa è eliminato e cosa resta

**Eliminato (VERIFIED su PostgreSQL 18.6 locale).** Con la credenziale del runner e SQL arbitrario non è possibile:
- leggere, scrivere, azzerare o abbassare `used`;
- cancellare tentativi o lease del bridge, o lease non nominati;
- eseguire TRUNCATE, COPY, LOCK o ALTER sulle tabelle;
- passare limiti più alti;
- creare, sostituire o alterare le funzioni, o creare oggetti nello schema;
- ombreggiare tabelle o funzioni con `pg_temp` o con il `search_path`.

**Residuo da ratificare: disponibilità, non superamento.** Chi possiede la credenziale del runner può:
- consumare il budget condiviso fino ai limiti fissi (8 al minuto, 80 al giorno);
- tenere occupati i 2 lease per 180 s;
- segnare la chiave come esaurita per il minuto o per il giorno UTC con `mark_exhausted`;
- tenere aperta una transazione dopo `reserve` o `mark_exhausted`, trattenendo il lock advisory della chiave. Il bridge va in timeout con `QUOTA_COORDINATOR_UNAVAILABLE`, cioè fail-closed. Il server non può imporre un timeout: `idle_in_transaction_session_timeout` e `transaction_timeout` sono impostabili dall'utente. Questo punto non è coperto da un test;
- creare default privileges propri nello schema (`ALTER DEFAULT PRIVILEGES IN SCHEMA fcb_cert_quota`). Non hanno effetto su nessun oggetto, ma lo stato passa a `DRIFT` e il runner si blocca da solo: è un auto-disservizio, come R2.

Nessuno di questi casi supera i limiti né riscrive lo stato del bridge. Il bridge resta fail-closed.

**Limiti fissati (ratificati dall'owner).** 8/80/2 sono i default di `bridge/api_football/config.py` e sono confermati come massimi fissi, mai alzabili o aggirabili dal runner. Il residuo di disponibilità (consumo della quota entro i limiti) è accettato temporaneamente dall'owner; l'alterazione arbitraria delle tabelle non è accettata ed è eliminata (sopra).

**R2 invariato.** Il runner può cambiare la propria password e le impostazioni del proprio ruolo.

## Compatibilità (adattata, FCB-A2-R1-WP009-DECISIONS)

Ogni controllo incorpora la stessa query canonica `sql/aiven/apifootball_cert_quota_state.sql` in forma di una sola riga (`cert_quota_state_one_line()` e `CERT_QUOTA_EXACT` in `scripts/generate_aiven_migrations.py`; i test offline verificano che il testo sia identico ovunque).

| Controllo | Con R1 EXACT | Con R1 in deriva | Senza schema |
|---|---|---|---|
| `defaultdb_precheck.sql` (+ guard generati del restore) | stato invariato, es. `BRIDGE_0001_0008` (VERIFIED) | `REFUSE_UNEXPECTED_SCHEMAS` (VERIFIED, 15 manomissioni) | invariato (VERIFIED: output identico a `ca18d4f` su database fresco, fase 1 e fase 2) |
| `$GUARD` dei workflow restore e ruoli (stesso testo, `$` e `"` escapati per bash) | accetta (VERIFIED con il testo estratto dal workflow) | `FCB_TARGET_GUARD_FAILED` (VERIFIED) | invariato |
| guard del backup (`fcb_db_backup.py`) | ammesso e **incluso** nel dump; il restore isolato deve mostrare EXACT (`restore_cert_quota_state`, VERIFIED round trip su PG 18.6) | `UNEXPECTED_SCHEMA` prima di leggere righe (VERIFIED, 7 manomissioni) | invariato, `restore_cert_quota_state` = ABSENT |
| `verify_bridge_roles.sql` (WP-009) | `HARDENED` dopo la fase 2 con le funzioni di `fcb_migrator` (VERIFIED) | `NOT_HARDENED:cert_quota_state` o il codice del privilegio (VERIFIED, 16 casi) | invariato: produzione di fase 1 = `HARDENED_PHASE1` (VERIFIED, test dedicato) |
| impronte `fingerprint_bridge_*.sql` | invariate: tornano al testo di `main` (nessuna eccezione ACL), stessi valori pinnati | — | invariate |

Ordine: R1 si installa **dopo** la fase 2. Se installato in fase 1 è accettato, ma la fase 2 sposta le tabelle a `fcb_migrator` e non lo schema: la transazione della fase 2 fallisce su `cert_quota_state` e fa rollback (VERIFIED). Lo step 1a dopo la fase 2: l'amministratore ha solo l'appartenenza ADMIN a `fcb_migrator`, quindi si concede SET per la sola `CREATE SCHEMA … AUTHORIZATION` e lo revoca nello stesso blocco, con assert (VERIFIED su PG 18.6; ASSUMED su Aiven).

## Review #201 su 59d98c2 (NOT_SAFE): correzioni

- **P1 (procedure non viste dal controllo esatto).** La query canonica ora richiede **esattamente 5 routine di qualunque tipo** (funzioni, procedure, aggregati, funzioni finestra, overload) e confronta ogni componente con `COALESCE`: prima `pg_get_function_result` di una procedura era NULL e `string_agg` la saltava, quindi una procedura `SECURITY DEFINER` del proprietario (EXECUTE a PUBLIC per default) lasciava lo stato `EXACT`. VERIFIED: la procedura del revisore, un aggregato, un overload, una funzione finestra, vista, sequenza, tipo composto e dominio portano a `DRIFT` e vengono rifiutati da assert step 1b, runner, precheck/guard/`$GUARD`, backup e `verify_bridge_roles.sql`. Riesame degli altri punti ciechi: `pg_class` (tabelle, viste, sequenze, indici), `pg_type` (tutti i tipi), operatori, classi/famiglie, collation, conversioni, text search, estensioni, statistiche, vincoli e default ACL dello schema sono già "nessun oggetto"; i trigger sulle tabelle di quota restano coperti dai controlli su `public` (precheck/verify), non dalla query canonica (documentato).
- **P2 (crescita illimitata delle tabelle condivise).** Il runner usa **una sola chiave**: lo step 1b fissa l'hash SHA-256 della chiave API del runner nella quinta routine `fcb_cert_quota.pinned_key_hash()` (SQL, `SECURITY INVOKER`, `IMMUTABLE`, EXECUTE solo al proprietario, corpo esattamente `SELECT '<64 hex>'::pg_catalog.text`, controllato per forma dalla query canonica). Ogni funzione rifiuta qualunque altra chiave **prima** del lock e di qualsiasi riga (`FCB_CERT_QUOTA_KEY_REFUSED`). Il pin non viene mai sostituito (`FCB_CERT_PATH_A_QUOTA_KEY_CHANGED`: cambiarlo è un atto dell'owner fuori da questo step). `mark_exhausted` pota i tentativi della chiave più vecchi di 60 s ed è un no-op mentre un marcatore provider-zero è vivo. VERIFIED: 1500 chiamate con chiavi casuali creano **zero** righe; 1200 chiamate sulla chiave fissata lasciano ≤ 8 tentativi, 0 lease, ≤ 4 contatori. Righe residue per la chiave fissata: al più 1 contatore per minuto e 1 per giorno UTC con attività (come il bridge), 8 tentativi per finestra, 2 lease.
- **P3 (snapshot vecchio sotto REPEATABLE READ / SERIALIZABLE).** Ogni funzione rifiuta se l'isolamento non è `read committed` (`FCB_CERT_QUOTA_ISOLATION_REFUSED`); l'adapter del runner imposta READ COMMITTED esplicitamente (VERIFIED anche con `default_transaction_isolation=serializable`).
- **P3 residuo (lock advisory trattenuto).** Invariato e accettato come residuo: chi ha la credenziale del runner può tenere aperta una transazione dopo `reserve`/`mark_exhausted` e bloccare il bridge sulla stessa chiave fino al suo timeout fail-closed. Nessuna modifica.

**Installazione bloccata finché l'owner non decide come fornire l'hash fissato.** `apifootball_cert_path_a.py install` legge l'hash da `FCB_CERT_PINNED_KEY_SHA256` (64 hex, SHA-256 della chiave che il runner userà, `pinned_key_hash_of()`); senza, fallisce chiuso con `FCB_CERT_PATH_A_INPUT_MISSING`. Il workflow di installazione A2 **non** è stato modificato per fornirlo: collegare un segreto (per esempio `APIFOOTBALL_API_KEY`) al job di installazione è un trasferimento di segreti che richiede una decisione esplicita dell'owner (CLAUDE.md §2.2).

## Gate prima dell'applicazione in produzione (non autorizzata)

1. CI completa, test PG18 e nuova review indipendente sullo SHA finale della PR #201.
2. Gate WP-009: switch di Render a `fcb_app`, 24 ore di osservazione, G1/G2, fase 2.
3. Solo dopo la fase 2: installazione A2 R1 con i workflow `fcb-aiven-apifootball-cert-a2-*`, su GO scritto dell'owner.
