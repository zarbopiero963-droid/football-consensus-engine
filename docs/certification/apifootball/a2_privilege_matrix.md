# PATH A2 — privilegi di `fcb_cert_runner` su `defaultdb` (variante R1, decisione owner D2)

**Stato:** variante preparata su un branch isolato (`claude/a2-r1-quota-rpc`), derivato dalla PR #201. Nessuna installazione in produzione. L'applicazione richiede una nuova ratifica scritta dell'owner dopo i gate WP-009.

**La variante non è ancora compatibile** con:
- il precheck del restore;
- i guard di sessione dei workflow restore e ruoli;
- il backup;
- la verifica WP-009 (`verify_bridge_roles.sql`).

Vedi "Compatibilità bloccata". Per vincolo owner, questi file non sono stati modificati.

Fonti:
- `sql/aiven/apifootball_cert_path_a_quota_schema.sql`: step 1a, amministratore. Crea lo schema vuoto `fcb_cert_quota`, di proprietà del proprietario delle tabelle di quota.
- `sql/aiven/apifootball_cert_path_a_quota_functions.sql`: step 1b, proprietario delle tabelle (`fcb_migrator` dopo la fase 2). Crea le 4 funzioni e concede EXECUTE e USAGE.
- `sql/aiven/apifootball_cert_path_a_quota_assert.sql`: verifica dello stato esatto, nella stessa transazione dello step 1b e dopo ogni rotazione della password.
- `sql/aiven/apifootball_cert_quota_state.sql`: query canonica di sola lettura (`ABSENT`/`EXACT`/`DRIFT`). È incorporata byte per byte nell'assert e il runner la usa prima di qualsiasi richiesta.

I file della variante a 9 privilegi (`apifootball_cert_path_a_coordinator_grants.sql` e `_assert.sql`) restano nel repository. Nessun workflow né lo script di installazione li esegue più. Li usano ancora soltanto i test di restore e WP-009, che verificano l'eccezione dei 9 privilegi tuttora presente nel precheck e in `verify_bridge_roles.sql`. Vanno rimossi insieme a quell'eccezione.

## Matrice R1

| # | Oggetto | Privilegio | Concesso da |
|---|---|---|---|
| 1 | schema `fcb_cert_quota` | `USAGE` | proprietario delle tabelle |
| 2 | `fcb_cert_quota.reserve(text)` | `EXECUTE` | idem |
| 3 | `fcb_cert_quota.release(text, text)` | `EXECUTE` | idem |
| 4 | `fcb_cert_quota.snapshot(text)` | `EXECUTE` | idem |
| 5 | `fcb_cert_quota.mark_exhausted(text, text)` | `EXECUTE` | idem |
| — | schema `public` | `USAGE` (invariato, step 1) | amministratore |

Il runner non ha **nessun** privilegio, né di tabella né di colonna, su `api_football_quota_counters`, `api_football_quota_leases` e `api_football_quota_attempts`, né su altre relazioni di `public`. Non ha CREATE né su `public` né su `fcb_cert_quota`.

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

**Limiti fissati (ASSUMED, da verificare dall'owner).** 8/80/2 sono i default di `bridge/api_football/config.py`. Se il runtime Render usa limiti più bassi (`APIFOOTBALL_MAX_*`), il runner può consumare fino a 8/80 mentre il bridge si ferma prima. Il valore effettivo su Render non è stato letto: nessun accesso alla produzione.

**R2 invariato.** Il runner può cambiare la propria password e le impostazioni del proprio ruolo.

## Compatibilità bloccata (NON modificata, vincolo owner)

Misure su PostgreSQL 18.6 con la variante installata:

| Controllo | Esito con R1 | Motivo |
|---|---|---|
| `defaultdb_precheck.sql` (restore WP-005) | `REFUSE_UNEXPECTED_SCHEMAS` (VERIFIED) | rifiuta ogni schema che non sia `public` o di sistema |
| guard di sessione: costante `UNEXPECTED_SCHEMA_EXISTS` di `generate_aiven_migrations.py`; `$GUARD` dei workflow restore e ruoli | rifiuta (VERIFIED sulla costante del generatore; i `$GUARD` usano lo stesso testo) | idem |
| guard del backup (`fcb_db_backup.py`) | `UNEXPECTED_SCHEMA` (VERIFIED) | idem |
| `verify_bridge_roles.sql` (WP-009) | atteso `NOT_HARDENED:missing_runtime_grant` (per ispezione del codice, NON eseguito) | il modello attuale **richiede** i 9 privilegi per un `fcb_cert_runner` plain |

Per rendere R1 installabile serve una PR separata, approvata dall'owner. Toccherebbe:
- precheck e guard rigenerati;
- `UNEXPECTED_SCHEMA_EXISTS`;
- i tre `$GUARD` dei workflow restore e ruoli;
- il backup;
- `verify_bridge_roles.sql`.

In ciascun punto si accetterebbe `fcb_cert_quota` solo nello stato `EXACT`, secondo la query canonica, e si rimuoverebbe l'eccezione dei 9 privilegi. Questo modifica il percorso SQL e il workflow della fase 1 WP-009, che l'owner ha escluso da questa variante.

## Gate prima dell'applicazione in produzione (non autorizzata)

1. Decisione dell'owner sulla compatibilità bloccata, poi una PR dedicata con CI verde e review indipendente.
2. Gate WP-009: fase 1, switch di Render a `fcb_app`, 24 ore di osservazione, G1/G2, fase 2.
3. Ratifica scritta dell'owner di questa matrice, del residuo di disponibilità e dei limiti fissati: 8/80/2, oppure valori diversi, uguali ai limiti effettivi del bridge.
