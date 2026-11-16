# PATH A2 — variante R1 (decisione owner D2): report

Branch isolato: `claude/a2-r1-quota-rpc`, da `ca18d4f` (PR #201). Nessuna PR, nessun merge, nessun accesso alla produzione (Aiven, Render, API-FOOTBALL), nessun segreto. Nessuna modifica sotto `bridge/` o `sql/migrations/` e nessuna modifica ai file di governance. Il branch della PR #201 non è stato toccato.

## Vincoli owner della prima fase (superati dalla ratifica, vedi "Integrazione")

Il workflow `.github/workflows/fcb-aiven-defaultdb-roles.yml` e il percorso SQL della fase 1 WP-009 non devono cambiare comportamento. Sono ammessi soltanto aggiornamenti di pin sha256 strettamente necessari. Rientrano nel percorso: prepare, upsert, grant, finalize e `verify_bridge_roles.sql`, oltre a `defaultdb_precheck.sql` e alle impronte, che il workflow dei ruoli esegue.

Con questo vincolo, i punti 5 (parte restore), 7 e la parte WP-009 del design **non sono stati implementati** e vengono riportati come bloccanti (vedi sotto). Nessun file pinnato dal workflow dei ruoli è cambiato, quindi nessun pin di quel workflow è stato aggiornato.

## Design implementato

1. **Schema `fcb_cert_quota`.** Lo crea lo step 1a (`sql/aiven/apifootball_cert_path_a_quota_schema.sql`), eseguito dall'amministratore.
   - Il proprietario è quello delle 3 tabelle di quota. Dopo la fase 2 è `fcb_migrator`, che non ha CREATE sul database: per questo lo schema lo crea l'amministratore, membro di `fcb_migrator` con SET, tramite `AUTHORIZATION`.
   - Se lo schema esiste già con un altro proprietario, lo step rifiuta.
2. **Quattro funzioni `SECURITY DEFINER`.** Le crea lo step 1b (`sql/aiven/apifootball_cert_path_a_quota_functions.sql`), eseguito dal proprietario delle tabelle: `reserve`, `release`, `snapshot`, `mark_exhausted`.
   - Hanno `SET search_path = pg_catalog, pg_temp`, nomi qualificati e operatori `OPERATOR(pg_catalog.x)`.
   - EXECUTE è revocato a PUBLIC e concesso solo al runner; USAGE sullo schema è concesso solo al runner.
   - Replicano `PostgresQuota` istruzione per istruzione: stesse righe, stesso `pg_advisory_xact_lock`. La chiave del lock è il primo blocco di 16 cifre esadecimali convertito in bit(64), poi in int8, con il bit di segno azzerato. È identica a `_advisory_lock_key` ed è verificata dal test differenziale e dal test di concorrenza misto.
   - I limiti sono fissati nel corpo: 8 al minuto, 80 al giorno, 2 lease, TTL di 180 s. Sono i default di `config.py`: `max_rpm=8`, `max_daily_requests=80`, `max_concurrency=2` (VERIFIED nel codice).
   - Validazioni: `key_hash` deve corrispondere a `^[0-9a-f]{64}$`; `lease_id` a `^cert-[0-9A-Za-z_-]{32}$`; `kind` deve essere `minute` o `day`.
   - Identità: si usa `SESSION_USER = 'fcb_cert_runner'`, perché dentro una SECURITY DEFINER `CURRENT_USER` è il proprietario. Il bridge **non** usa le funzioni: mantiene il proprio percorso diretto, invariato.
   - Differenza voluta: i lease del runner hanno il prefisso `cert-`, e `release` cancella solo lease di quel formato, nominati, della chiave nominata (al massimo una riga). Il runner non può quindi rilasciare lease del bridge nemmeno se ne conoscesse l'id.
3. **Stato canonico.** `sql/aiven/apifootball_cert_quota_state.sql` restituisce `ABSENT`, `EXACT` o `DRIFT`. `EXACT` richiede:
   - un runner "plain";
   - un unico proprietario T per le 3 tabelle, che possiede anche lo schema;
   - ACL dello schema esatta;
   - esattamente 4 funzioni, con firma, linguaggio, `prosecdef`, volatilità, `STRICT`, `leakproof`, `proconfig` e md5 del corpo pinnati, proprietario T ed EXECUTE esattamente {T, runner} senza grant option;
   - nessun altro oggetto nello schema (14 cataloghi controllati, compreso `pg_default_acl`);
   - nessun ACL di tabella o di colonna al runner o a PUBLIC sulle 3 tabelle, e nessun CREATE del runner su `public`.
4. **Assert dello step 1b.** `sql/aiven/apifootball_cert_path_a_quota_assert.sql` incorpora la query canonica byte per byte (un test lo verifica). Inoltre richiede:
   - zero privilegi del runner su **tutte** le relazioni e colonne di `public`;
   - una sola voce del runner su `public`: `USAGE:false`;
   - nessun CREATE su `public`.

   La stessa assert viene eseguita dopo la rotazione della password.
5. **Installer** (`scripts/apifootball_cert_path_a.py`).
   - `install` esegue gli step 1 → 1a (amministratore) → 1b (proprietario) → 2 → 3.
   - `rotate-runner` esegue `roles.sql` più la nuova assert.
   - `evidence` aggiunge le righe `quota_function` e `quota_schema_grant`.

   Nel workflow di installazione A2 sono aggiornati i pin dei 3 nuovi file e di `apifootball_cert_path_a.py`, e il conteggio dei file pinnati passa da 9 a 10. Nel workflow di approvazione A2 è aggiornato solo il pin di `apifootball_cert_path_a.py`.
6. **Runner** (`scripts/apifootball_cert_batch1.py`).
   - Nuova classe `CertQuotaCoordinator`, con la stessa interfaccia asincrona di `PostgresQuota`: `prepare`, `acquire`, `release`, `snapshot`, `mark_exhausted`. È l'interfaccia usata da `resilience.py` e da `tools.py` (`snapshot`).
   - Ogni chiamata è una sola istruzione in una propria transazione, con commit prima del ritorno.
   - I codici `FCB_CERT_QUOTA_*` diventano `QuotaExceeded` o `QuotaConcurrencyBusy`, con lo stesso messaggio e la stessa finestra; ogni altro errore si propaga e produce `QUOTA_COORDINATOR_UNAVAILABLE`.
   - `prepare()` esige lo stato `EXACT`.
   - `main()` passa `quota=CertQuotaCoordinator()` a `execute` e a `execute_snapshot`.
   - Il pre-flight tiene `COORDINATOR_TABLES_SQL` e aggiunge `COORDINATOR_FUNCTIONS_NOT_EXACT`.
   - L'ordine autorizzazione → piano → lease → prenotazione in `fcb_cert` → chiamata → esito resta invariato, come provano i test esistenti.
7. **Documentazione.** Aggiornati `docs/certification/apifootball/a2_privilege_matrix.md` (nuova matrice, residuo, compatibilità bloccata) e la sezione PATH A2 del README.

## Test

Le misure sono su PostgreSQL 18.6 locale, con login SCRAM per runner e amministratore.

| Suite | Esito |
|---|---|
| `tests/integration/test_apifootball_cert_a2_r1_postgres.py` (nuova) | 67 passed |
| `tests/integration/test_apifootball_cert_path_a_postgres.py` (adattata a R1) | vedi "Esiti finali" |
| `tests/integration/test_defaultdb_restore_postgres.py -k a2` (invariato, variante legacy) | 18 passed |
| `tests/integration/test_wp009_roles_runtime_postgres.py -k "a2 or full_sequence or g1_fcb_cert"` (invariato, variante legacy) | 16 passed |
| Suite offline `--ignore=tests/integration` | vedi "Esiti finali" |
| `generate_aiven_migrations.py --check`, tutti i 6 target | verde (nessun file generato cambiato) |

Copertura della nuova suite R1:
- **Test differenziale.** Runner e `PostgresQuota(8,80,2)` eseguono la stessa sequenza di 30 operazioni su due chiavi gemelle. Si confrontano esiti (tipo, finestra, messaggio), snapshot, contatori e numero di righe: sono identici. La sequenza tocca busy, rolling, provider-zero e day.
- **Stato condiviso.** Sulla stessa chiave runner e bridge vedono lo stesso stato condiviso.
- **SQL diretto.** 19 istruzioni dirette sulle 3 tabelle (SELECT, INSERT, UPDATE di `used`, DELETE del lease del bridge, TRUNCATE, COPY, ALTER, LOCK) producono tutte `InsufficientPrivilege` e lasciano lo stato invariato.
- **Limiti.** Nessun overload che accetti limiti; i GUC non hanno effetto.
- **Rilascio.** `release` rifiuta lease del bridge e pattern LIKE e cancella solo il lease nominato.
- **Input malformati.** 11 chiavi, 9 lease e 6 finestre malformati vengono rifiutati.
- **Identità.**
  - Il proprietario chiama → `IDENTITY_REFUSED`.
  - Un altro ruolo → nessun EXECUTE.
  - `SET ROLE fcb_cert_runner` da un altro login → `IDENTITY_REFUSED`, e lo stato passa a `DRIFT`.
- **DDL.** 15 istruzioni DDL o di proprietà rifiutate (CREATE, CREATE OR REPLACE, ALTER SECURITY/search_path/OWNER/RENAME, DROP, COMMENT, SECURITY LABEL).
  - Le istruzioni che PostgreSQL accetta con un semplice avviso (GRANT senza grant option) non hanno effetto.
  - `ALTER DEFAULT PRIVILEGES` del runner porta a `DRIFT`: fail-closed, un auto-disservizio.
- **Shadowing.** Tabelle temporanee con gli stessi nomi, `pg_temp.clock_timestamp` e `pg_temp.gen_random_uuid`, `search_path=pg_temp,...`: le righe reali restano corrette e le tabelle temporanee vuote.
- **Concorrenza.**
  - 9 processi runner più 3 processi bridge `PostgresQuota`, sulla stessa chiave, 6 tentativi ciascuno: al massimo 8 concessioni, picco di lease misurato ≤ 2, contatori coerenti.
  - Con il giorno già a 78: al massimo 2 concessioni, giorno ≤ 80.
- **Crash.**
  - Processo ucciso nella transazione: nulla di committato.
  - Due processi uccisi dopo `reserve`: i 2 lease orfani bloccano runner e bridge (busy). Scaduto il TTL, simulato da superuser spostando `expires_at`, il `reserve` successivo li pota.
  - Il TTL è esattamente 180 s.
- **Manomissioni.** 22 casi portano a `DRIFT` e fanno rifiutare l'assert. Una deriva committata ferma il runner prima del provider: zero richieste, zero prenotazioni nel ledger. Il pre-flight di `main()` restituisce `CAMPAIGN_REFUSED COORDINATOR_FUNCTIONS_NOT_EXACT`.

Adattamenti dichiarati nella suite PATH A esistente:
- **Fixture** sugli step 1a e 1b.
- **Test sui privilegi diretti.** I test sui 9 privilegi sono sostituiti da: nessun privilegio di tabella, EXECUTE sulle 4 funzioni, e ogni EXECUTE necessario.
- **Coordinatore del runner.** I test d'ordine, negativi ("nessuna richiesta con il solo lease"), di crash e di kill usano `CertQuotaCoordinator`.
- **Pulizia tra i test.** Un reset da superuser delle righe di quota della chiave del kit tra un test e l'altro: con limiti fissi 8/min e 2 lease, i lease orfani dei test di crash altrimenti si propagherebbero ai test successivi.
- **Test di escalation.** Con zero privilegi, un GRANT su tabella dà errore invece di un avviso.
- **Evidence.** Si attendono 0 grant e 4 righe di funzione.

Adattamento offline: `tests/test_api_football_live_harness.py::test_runtime_client_scripts_keep_the_shared_coordinator` ora richiede `quota=CertQuotaCoordinator()` nell'entry point, al posto dell'assenza di `quota`. È un cambio intenzionale.

## Integrazione (ratifica owner FCB-A2-R1-WP009-DECISIONS)

R1 è integrata nella PR #201 tramite questo branch (nessuna PR separata). I 9 privilegi diretti del runner sono **eliminati**; i limiti 8/min, 80/giorno, 2 concorrenti sono massimi fissi; il residuo di disponibilità è accettato temporaneamente; l'alterazione arbitraria delle tabelle non è accettata.

### Modifiche

- **Query canonica** (`apifootball_cert_quota_state.sql`): i 4 letterali adiacenti sono uniti con `OPERATOR(pg_catalog.||)`, così la query ha una forma su una sola riga identica (`cert_quota_state_one_line()` in `scripts/generate_aiven_migrations.py`). Copia nell'assert dello step 1b aggiornata byte per byte.
- **Ammissione solo EXACT** (`CERT_QUOTA_EXACT`), stesso testo in: `defaultdb_precheck.sql` (CTE `user_ns`), `UNEXPECTED_SCHEMA_EXISTS` e quindi i guard generati `defaultdb_restore_guard.sql`/`_post_guard.sql` (rigenerati), il `$GUARD` dei workflow restore e ruoli (3 righe) e la query `schemas=` del precheck del workflow restore, la colonna `cert_quota_state` della guard del backup (`CERT_QUOTA_STATE_SQL`, copia in `fcb_db_backup.py` per non eseguire file SQL non pinnati dal suo workflow), il nuovo check `cert_quota_state` di `verify_bridge_roles.sql`.
- **Eccezione dei 9 privilegi rimossa**: precheck e impronte `fingerprint_bridge_schema/structure.sql` tornano al testo di `main` (`fd724e4`); trasformazione della struttura in `fcb_db_roles.py` ripristinata; in `verify_bridge_roles.sql` rimossi `cert_runner`, `cert_runner_grants`, `cert_runner_column_grants` e la USAGE del runner su `public` (resta G2). File `apifootball_cert_path_a_coordinator_grants.sql` e `_assert.sql` eliminati.
- **USAGE su `public` del runner rimossa**: lo step 1 non concede nulla e revoca `USAGE, CREATE` su `public` al runner; il suo assert e l'assert R1 richiedono nessuna voce del runner su `public` e nessun privilegio su alcuna relazione di `public` (quota incluse). Il pre-flight del runner usa `SECOND_LEDGER_SQL` (catalogo) invece di `to_regclass('public.…')`, che senza USAGE dà `permission denied for schema public` (VERIFIED).
- **Step 1a dopo la fase 2** (bug trovato in questa fase, prima ASSUMED): nel modello WP-009 l'amministratore ha solo l'appartenenza ADMIN a `fcb_migrator` (`session_reaches_migrator`), quindi `CREATE SCHEMA … AUTHORIZATION fcb_migrator` falliva con `must be able to SET ROLE "fcb_migrator"`. Ora l'amministratore si concede SET per quella sola istruzione e lo revoca nello stesso blocco, con assert `FCB_CERT_PATH_A_QUOTA_SCHEMA_TEMPORARY_GRANT_LEFT`.
- **Backup**: `fcb_cert_quota` EXACT è ammesso e **incluso** nel dump (non escluso: niente troncature silenziose); `REFERENCED_ROLES_SQL` copre ACL e owner dello schema e delle funzioni; nuovo check `restore_cert_quota_state` (ABSENT/ABSENT o EXACT/EXACT). Ogni altro stato: `UNEXPECTED_SCHEMA`.

### Diff dei workflow (solo testo della guard sugli schemi e pin)

- `fcb-aiven-defaultdb-restore.yml`: riga `schemas="$(q "…")"` (precheck) e riga `GUARD="…"` (restore): solo l'aggiunta di ` and not <CERT_QUOTA_EXACT>` / ` AND NOT <CERT_QUOTA_EXACT>` con `$` e `"` escapati (verificato: rimuovendo l'aggiunta il file è identico a `21d7dec`).
- `fcb-aiven-defaultdb-roles.yml`: le due righe `GUARD="…"` (fase 1, fase 2), stessa aggiunta, byte-identiche al restore; pin di `verify_bridge_roles.sql`, `defaultdb_precheck.sql`, `fingerprint_bridge_schema.sql`, `fingerprint_bridge_structure.sql` (le due impronte tornano ai pin di `main`).
- `fcb-aiven-defaultdb-backup.yml`: pin di `fingerprint_bridge_structure.sql` (torna a quello di `main`: il file è identico a `main`).
- `fcb-aiven-apifootball-cert-a2-install.yml`: pin di `apifootball_cert_path_a_roles.sql`, `apifootball_cert_path_a_quota_assert.sql`, `apifootball_cert_path_a_quota_schema.sql` (step 1a), `scripts/apifootball_cert_path_a.py`, `scripts/fcb_db_roles.py`.
- `fcb-aiven-apifootball-cert-a2-plan-approve.yml`: pin di `scripts/apifootball_cert_path_a.py`.

Nessun nuovo step, input, modo o comportamento.

### Classificazione della produzione (fase 1 applicata, senza `fcb_cert_quota`)

VERIFIED su PostgreSQL 18.6: con i file di `ca18d4f` e con quelli adattati, su database fresco, in fase 1 e in fase 2, `defaultdb_precheck.sql`, `verify_bridge_roles.sql`, le due impronte e `diagnose_bridge_roles.sql` producono output **identici** (fase 1: `HARDENED_PHASE1`, `BRIDGE_0001_0008`, precheck `REFUSE_RUNTIME_TABLE_DRIFT`, quota `ABSENT`). Il test `test_a2_r1_production_phase1_state_is_classified_exactly_as_before` lo fissa.

### Ordine

R1 si installa **dopo** la fase 2. Installato in fase 1 è accettato, ma la fase 2 sposta le tabelle e non lo schema: la sua transazione fallisce su `cert_quota_state` e fa rollback (VERIFIED, fail-closed).

### Non misurato / ASSUMED

- Aiven reale: `avnadmin` con ADMIN OPTION su `fcb_migrator` (concessione temporanea dello step 1a), `createrole_self_grant`, PG 18.7+ (`pg_attver`).
- Esecuzione dei workflow su GitHub Actions (nessun dispatch: vietato).
- Limiti effettivi del bridge su Render; durata del blocco del lock advisory.
