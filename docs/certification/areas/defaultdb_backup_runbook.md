# FCB-WP-009: backup cifrato e restore-test di `defaultdb` (runbook dell'owner)

Workflow: `.github/workflows/fcb-aiven-defaultdb-backup.yml`.
Logica: `scripts/fcb_db_backup.py`; il workflow è solo un wrapper.

- Avvio solo manuale (`workflow_dispatch`), solo da `main`, solo dall'owner del repository.
- Gira nel GitHub Environment protetto `aiven-production`, che richiede l'approvazione del reviewer.
- Frase di conferma esatta: `BACKUP defaultdb TO ENCRYPTED ARTIFACT`.

**Stato: progettato e testato solo su PostgreSQL 18 locale e usa-e-getta. Non è mai stato eseguito in produzione. Questa non è una certificazione.**

Il backup diventa VERIFIED solo dopo:
1. un run reale PASS;
2. il trasferimento su Drive con lo SHA-256 riverificato;
3. una prova di decrypt + restore fatta dall'owner;
4. la registrazione di questi passi sulla #130.

## Cosa fa (e cosa non fa)

### Sorgente
La sorgente viene **solo letta**:
- una transazione `READ ONLY REPEATABLE READ` con `default_transaction_read_only=on` esporta uno snapshot (`pg_export_snapshot`);
- `pg_dump --snapshot` e tutte le verifiche lato sorgente leggono **lo stesso snapshot**.

Nessuna migration, nessun DDL/DML, nessun `SET ROLE`. Gli unici file SQL eseguiti sono due file solo-catalogo con SHA-256 fissato:
- `count_bridge_catalog.sql`;
- `fingerprint_bridge_structure.sql`.

### Guardia (prima di leggere qualunque riga)
Prima di leggere una sola riga, una guardia sul solo catalogo rifiuta il backup se:
- il database non è `defaultdb`;
- il server non è PostgreSQL 18;
- la connessione non usa TLS;
- uno schema diverso da `public` non è stato previsto. `_avnadmin_monitor` di Aiven è ammesso: viene escluso dal dump e riportato;
- un oggetto di `public` non appartiene all'identità che fa il backup;
- un altro ruolo non superuser può agire come quell'identità;
- ci sono large object;
- c'è un `FORCE ROW LEVEL SECURITY`;
- c'è un'estensione (diversa da `plpgsql`) fuori da `public`, compreso `_avnadmin_monitor` (`EXTENSION_OUTSIDE_PUBLIC`). `pg_dump --exclude-schema` emetterebbe comunque `CREATE EXTENSION … WITH SCHEMA <schema escluso>` e il restore fallirebbe più tardi.

La transazione dello snapshot è aperta in modo esplicito (connessione in autocommit, poi `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY`). `statement_timeout`, `idle_in_transaction_session_timeout` e `transaction_timeout` sono azzerati sia all'avvio della sessione sia con `SET LOCAL`, così la sessione che esporta lo snapshot sopravvive a un dump lungo.

È la stessa regola del runbook WP-009: l'amministratore legge righe solo quando è dimostrato che possiede tutto.

**Dopo la fase 2 di WP-009** il bridge appartiene a `fcb_migrator` e il backup come amministratore viene **rifiutato** (`PUBLIC_OBJECT_NOT_OWNED_BY_SESSION_USER`). Un backup in quello stato richiede una nuova decisione dell'owner (vedi "Decisioni aperte").

### Ruoli
`pg_dumpall --roles-only --no-role-passwords` esporta ruoli, attributi, membership e impostazioni `ALTER ROLE … SET`, **senza password né hash**. Se nel file compare `PASSWORD`, un verifier `SCRAM-SHA-256$…` o un hash `md5…`, il job fallisce.

`roles.sql` è salvato nel pacchetto **completo e identico byte per byte** all'output di `pg_dumpall`: contiene anche i ruoli di servizio di Aiven, le loro membership e le loro impostazioni (vedi "Ruoli gestiti da Aiven" sotto). Nessun filtro tocca il file salvato.

Le proprietà del database vanno in `database.sql`, prodotto con `pg_dump --create --schema-only --exclude-schema=*`:
- `CREATE DATABASE … LOCALE` e owner;
- `ALTER DATABASE … SET …`;
- `ALTER ROLE … IN DATABASE … SET …`;
- `GRANT`/`REVOKE … ON DATABASE …` (ACL del database).

### Restore-test isolato
Il restore-test gira in un cluster PostgreSQL 18 **isolato sul runner**:
- solo socket unix privato;
- nessun listener TCP;
- nessun segreto nell'ambiente del server;
- la connessione alla sorgente è già chiusa.

I passi sono:
1. `roles.sql` viene applicato, **meno** le sole impostazioni dei ruoli gestiti da Aiven in allowlist (sezione seguente). L'unico errore ammesso è `role "<bootstrap>" already exists`.
2. Si crea il database `defaultdb` con l'owner sorgente.
3. Le istruzioni a livello di database di `database.sql` (`ALTER DATABASE … SET`, `ALTER ROLE … IN DATABASE … SET`, `GRANT`/`REVOKE … ON DATABASE`) vengono rieseguite sul database ripristinato, con `ON_ERROR_STOP`. `CREATE DATABASE` e `CREATE`/`COMMENT ON EXTENSION` non vengono rieseguiti; qualunque altra istruzione inattesa fa fallire il job (`DATABASE_SQL_UNEXPECTED_STATEMENT`).
4. `pg_restore --exit-on-error --single-transaction`.

Un exit code 0 di `pg_restore` **non basta**. Il job fallisce, prima di cifrare, se anche una sola di queste verifiche differisce tra snapshot sorgente e restore:
- conteggi oggetti e fingerprint di struttura, uguali anche ai valori fissati per 0001-0008;
- `schema_migrations` 1..8 (nomi e checksum del repository);
- funzioni (firma e md5 della definizione);
- sequenze (`last_value` e `is_called`). Le sequenze non devono muoversi durante il dump;
- owner di ogni tabella, sequenza e funzione;
- opzioni di colonna (`attoptions`, per esempio `n_distinct`). Si ignora solo `pg_attver=<numero>`, l'opzione interna che PostgreSQL 18.7/16.16 scrive da solo su ogni colonna con un valore diverso per ogni server (`pg_dump` 18.7 non la esporta): non è un dato e confrontarla darebbe un falso errore;
- ACL normalizzate (`relacl`, ACL di colonna, `proacl`, ACL dei tipi, owner e ACL dello schema `public`, default privileges `pg_default_acl`);
- database: owner, ACL e tutte le impostazioni `pg_db_role_setting` (`ALTER DATABASE … SET` e `ALTER ROLE … IN DATABASE … SET`);
- ruoli (nomi e attributi `LOGIN`, `CREATEROLE`, …) e membership, confrontati con il catalogo sorgente;
- per ogni tabella: `SELECT` reale, numero di righe e hash del contenuto (SHA-256 delle righe ordinate).

### Ruoli gestiti da Aiven nel restore-test (FCB-WP009-AIVEN-MANAGED-ROLES-RESTORE-FIX)
Il run 43870519736 (main `5e6bc1d`) è fallito con `ROLES_SQL_APPLY_FAILED class=INVALID_SETTING`: `roles.sql` di Aiven contiene `ALTER ROLE _aiven|_aiven_repl|postgres SET avn_wal_sender_stop_timeout TO …`, un parametro che esiste solo nella build di Aiven. Un PostgreSQL 18 standard lo rifiuta (`unrecognized configuration parameter`). I nomi con il punto (`pgaudit.log`, `pg_stat_statements.track`, `timescaledb.disable_load`, `extwlist.extensions`, …) invece sono accettati come placeholder quando il modulo non è caricato (verificato su PostgreSQL 18.6).

**Solo nel restore-test isolato**, e solo per coppie esatte (ruolo, parametro), l'istruzione `ALTER ROLE <ruolo> SET <parametro> TO …;` **non viene rieseguita**. L'allowlist (`AIVEN_MANAGED_ROLE_SETTINGS` in `scripts/fcb_db_backup.py`) usa nomi esatti, mai prefissi:

| ruolo | parametri saltati |
|---|---|
| `_aiven`, `_aiven_repl`, `postgres` | `avn_wal_sender_stop_timeout`, `default_transaction_isolation`, `default_transaction_read_only`, `pg_stat_monitor.pgsm_track`, `pg_stat_plans.track`, `pg_stat_statements.track`, `pgaudit.log`, `timescaledb.disable_load` |
| `_avnadmin_monitor` | `extwlist.extensions`, `pg_stat_monitor.pgsm_track`, `pg_stat_plans.track`, `pg_stat_statements.track`, `pgaudit.log` |

Sono le impostazioni osservate in sola lettura sul servizio di produzione. Si saltano tutte, non solo quella che fallisce: sono configurazione del provider, e `default_transaction_*` su `postgres` cambierebbe le sessioni del bootstrap del cluster di restore stesso.

Cosa viene **ricreato e confrontato** come prima:
- **ogni** ruolo, compresi quelli di Aiven (`_aiven`, `_aiven_repl`, `_aiven_rewind`, `_avnadmin_auditing`, `_avnadmin_managed`, `_avnadmin_monitor`, `postgres`), con i suoi attributi;
- tutte le membership con grantor e opzioni (per esempio `avnadmin` ∈ `avnadmin_group` ∈ `_avnadmin_managed`), owner, ACL e dipendenze che citano un ruolo gestito;
- tutte le altre impostazioni di ruolo, comprese quelle di un ruolo gestito su un parametro fuori allowlist.

Cosa viene **saltato**: solo le istruzioni delle coppie in tabella. Il confronto dei ruoli toglie le stesse coppie, in modo simmetrico, dalla `rolconfig` di sorgente e restore; il `manifest.json` cifrato conserva la `rolconfig` completa della sorgente (`roles`) e l'elenco delle coppie saltate (`restore_test_skipped_role_settings`). La riga di summary `roles_sql_aiven_managed_settings_skipped_in_restore_test` riporta **solo** `skipped=<N> sha256=<digest>` (ratifica dell'owner FCB-WP009-PR204-F5-RUNBOOK): nessun nome di ruolo, nessun nome di parametro, nessun valore. L'elenco delle coppie sta solo nel `manifest.json` cifrato (`restore_test_skipped_role_settings`, più `restore_test_skipped_role_settings_sha256`); il leak check rifiuta un summary che nomini un parametro dell'allowlist o i ruoli `_aiven`/`_aiven_repl` (`SUMMARY_NAMES_MANAGED_ROLE_SETTINGS`).

**Digest.** `sha256` è lo SHA-256 (esadecimale) dei byte UTF-8 di `json.dumps(sorted([[ruolo, parametro], …]), ensure_ascii=True, separators=(",", ":"))`, per esempio `[["_aiven","avn_wal_sender_stop_timeout"],["_aiven","default_transaction_isolation"],…]`; senza coppie è lo SHA-256 di `[]` (`skipped=0 sha256=4f53cda1…`). Non dipende dall'ordine e cambia se una coppia viene aggiunta, tolta o modificata. Sul servizio osservato: `skipped=29`.

Decisioni di progetto:
- `avnadmin` è il ruolo applicativo (owner di `defaultdb` e delle tabelle): le sue impostazioni non si saltano mai.
- `avnadmin_group`, `_aiven_rewind`, `_avnadmin_auditing` e `_avnadmin_managed` non hanno impostazioni osservate, quindi non sono nell'allowlist delle impostazioni: un'impostazione che compaia su di loro viene rieseguita e confrontata come quella di un ruolo applicativo.
- Un nome simile (`_aiven_x`, `_avnadmin_evil`, `"_AIVEN"`), una grafia diversa da quella di `pg_dumpall` (`"_aiven"` tra virgolette), una variante `IN DATABASE` o un parametro fuori allowlist non vengono mai saltati: se il parametro è sconosciuto il job fallisce con `ROLES_SQL_APPLY_FAILED class=INVALID_SETTING`, come prima.
- `roles.sql` viene letto con un lexer esatto delle istruzioni (stringhe `'…'` ed `E'…'`, identificatori `"…"`, commenti `--`, righe `\restrict`/`\unrestrict`): un valore con `;` o a capo non sposta mai il confine di un'istruzione. Quello che il lexer non sa classificare (dollar quoting, commenti `/* */`, backslash fuori da una stringa, istruzione o stringa non terminata, impostazione duplicata) fa fallire il job con `ROLES_SQL_UNPARSEABLE`.

### Pacchetto
Il pacchetto è un tar con:
- `defaultdb.dump` (formato custom, con owner e ACL);
- `roles.sql`;
- `database.sql`;
- `manifest.json`, non sensibile: conteggi, hash, versioni, timestamp, commit sorgente.

Viene cifrato con **age in modalità passphrase**: scrypt (N=2^18) + ChaCha20-Poly1305 a blocchi da 64 KiB, header autenticato con HMAC. Poi:
- il file viene **decifrato di nuovo sul runner** e lo SHA-256 del plaintext deve coincidere;
- una copia manomessa deve essere rifiutata;
- **tutti i file in chiaro vengono eliminati prima dell'upload**: dump, SQL, tar e i dati del cluster di restore.

### Artifact
L'artifact contiene **solo** `fcb-defaultdb-<run>-<attempt>.tar.age` e il suo `.sha256`.
- retention: 1 giorno;
- nome con il run id.

Un leak check controlla summary e cartella dell'artifact (DSN, password, passphrase, plaintext). Il summary viene pubblicato solo dopo il leak check.

### Gestione della passphrase
- La passphrase arriva solo al processo `scripts/fcb_db_backup.py`.
- Viene scritta ad `age` attraverso un terminale (pty) con eco disattivata.
- Non compare mai in argv, nell'ambiente dei processi figli (`pg_dump`, `pg_dumpall`, `pg_restore`, `psql`, `age`) o nei log. I test lo verificano.

Perché `age`:
- è uno strumento standard e verificabile, con formato e parametri robusti fissati dalla specifica (cifratura autenticata);
- il recupero richiede solo il binario `age` (Linux, macOS, Windows);
- non serve codice di questo repository per decifrare.

`age -p` legge la passphrase solo da un terminale, per questo il runner la passa tramite pty e mai tramite argv o variabili d'ambiente.

## 1. Creare `FCB_BACKUP_PASSPHRASE` (una volta)

1. Nel **password manager** dell'owner genera una passphrase casuale di **almeno 32 caratteri**, consigliati 40 o più. Non usare spazi iniziali o finali né a capo.
2. Salvala nel password manager con il nome `FCB_BACKUP_PASSPHRASE (defaultdb backup age)`. **Non salvarla su Google Drive**, né accanto ai file cifrati, né in issue, PR, chat o note.
3. GitHub → Settings → Environments → `aiven-production` → Environment secrets → `FCB_BACKUP_PASSPHRASE` → incolla il valore.

Il workflow fallisce in modo chiuso se il segreto manca, è più corto di 32 caratteri o contiene caratteri di controllo.

**La passphrase deve restare nel password manager anche dopo la cancellazione dell'artifact e per tutta la vita dei backup su Drive: senza di essa i backup sono irrecuperabili.** Se cambi la passphrase, conserva anche le vecchie finché esistono backup cifrati con esse. Annota nel password manager quale passphrase vale per quali run id.

Gli altri segreti sono quelli già usati dai workflow di `defaultdb`: `FCB_DEFAULTDB_URL`, `FCB_AIVEN_CA_PEM` e `FCB_AIVEN_CA_SHA256`.

## 2. Eseguire e approvare il workflow

1. Actions → **FCB Aiven defaultdb encrypted backup (WP-009, owner-gated)** → Run workflow.
   - branch: `main`;
   - `confirm`: `BACKUP defaultdb TO ENCRYPTED ARTIFACT`.
2. Approva il deployment nell'Environment `aiven-production`.
3. Il workflow condivide il gruppo di concorrenza con restore e ruoli: non parte mentre uno di questi è in esecuzione.

**Come appare un PASS** (summary del job):
- `## FCB defaultdb encrypted backup: PASS`;
- una tabella in cui **ogni** riga è `PASS`, tra cui:
  - `source_guard_catalog_only`, `sequences_stable_during_dump`;
  - `roles_sql_without_passwords`, `roles_sql_aiven_managed_settings_skipped_in_restore_test` (in produzione: `skipped=29 …`), `roles_names_attributes_match_source`, `role_memberships_match_source`;
  - `pg_restore_exit_on_error`, `restore_catalog_counts`, `restore_structure`, `restore_schema_migrations_1_8`;
  - `restore_functions`, `restore_sequences_last_value_is_called`, `restore_owners`, `restore_acls_normalized`;
  - `database_sql_applied`, `restore_database_owner_acl_settings`;
  - `restore_rows_per_table` (numero di tabelle, totale righe, hash dei conteggi) e `restore_content_per_table`, `restore_content_sha256`;
  - `decrypt_roundtrip_plaintext_sha256`, `tampered_ciphertext_rejected`;
  - `plaintext_deleted_before_upload`, `artifact_dir_only_ciphertext_and_sha256`;
- in fondo: `encrypted_file`, `encrypted_sha256`, `encrypted_bytes`, `plaintext_sha256`, `plaintext_bytes`, `excluded_schemas` (un numero), `extensions` (un numero), versioni, `source_commit`, `created_at_utc`;
- il summary non contiene conteggi per tabella né nomi di schema o di ruolo (le impostazioni gestite saltate compaiono solo come `skipped=N sha256=…`): i conteggi e gli hash per tabella stanno solo nel `manifest.json` cifrato. In caso di differenza il summary riporta solo i **nomi** delle tabelle (`mismatched=…`);
- lo step "Leak check" stampa `leak check: PASS (summary and artifact directory)`;
- un artifact `fcb-defaultdb-backup-<run_id>-<attempt>` con esattamente due file.

**Copia nel password manager**, accanto alla passphrase:
- il run id;
- `encrypted_sha256`;
- `plaintext_sha256`.

Qualunque `FAIL` significa nessun artifact e plaintext già eliminato. Il summary mostra il codice, per esempio:
- `PUBLIC_OBJECT_NOT_OWNED_BY_SESSION_USER`;
- `UNEXPECTED_SCHEMA`;
- `SEQUENCE_MOVED_DURING_DUMP`: rilancia in un momento senza scritture;
- i codici dei ruoli, in tre famiglie (dettaglio in "Errori dei ruoli" qui sotto): `ROLES_SQL_UNPARSEABLE`, `ROLES_SQL_APPLY_FAILED class=INVALID_SETTING|META_COMMAND_REFUSED|OTHER|…`;
- `EXTENSION_OUTSIDE_PUBLIC`;
- `restore_rows_per_table … mismatched=<tabella>`.

### Errori dei ruoli
**(a) Errore nel file esportato** (`roles.sql` non ha la forma di `pg_dumpall`; nessun comando arriva a psql): `ROLES_SQL_UNPARSEABLE`. Motivi:
- meta-comandi psql diversi dalla coppia di `pg_dumpall` (`\!`, `\i`, `\set`, `\c`, `\connect`, …) o un separatore `\\` su una riga meta;
- coppia `\restrict <key>` / `\unrestrict <key>` mancante, doppia, con chiavi diverse, con una chiave che non è di 63 caratteri alfanumerici, con testo in più sulla riga, o con istruzioni prima di `\restrict` o dopo `\unrestrict`;
- un'istruzione che nomina `standard_conforming_strings` diversa da esattamente `SET standard_conforming_strings = on;`;
- sintassi non supportata dal lexer: dollar quoting, commenti `/* */`, backslash fuori da una stringa, stringa, identificatore o istruzione non terminati, una stessa impostazione gestita ripetuta.

**(b) Compatibilità con Aiven** (il file è corretto, ma il PostgreSQL standard del restore-test non accetta qualcosa del provider): `ROLES_SQL_APPLY_FAILED class=INVALID_SETTING`, per un parametro sconosciuto fuori allowlist (per esempio un nuovo parametro `avn_*`, o un parametro sconosciuto su un ruolo applicativo o su un nome simile come `_aiven_x`). Richiede una PR dell'owner che aggiorni l'allowlist, mai un aggiramento.

**(c) Fallimento del restore isolato** (psql rifiuta o non esegue qualcosa nel cluster di restore):
- `ROLES_SQL_APPLY_FAILED class=META_COMMAND_REFUSED`: psql ha rifiutato un comando backslash (`backslash commands are restricted`, `invalid command`); qualunque riga `error:` del client psql fa fallire il job anche con exit code 0;
- `ROLES_SQL_APPLY_FAILED class=OTHER` (o un'altra classe fissa, per esempio `PERMISSION_DENIED`, `ALREADY_EXISTS`): qualunque altro errore; l'unico ammesso è `role "<bootstrap>" already exists`;
- `ROLES_MISMATCH`, `ROLES_SQL_INCOMPLETE`: ruoli, attributi o membership ripristinati diversi dalla sorgente.

**P3 residui accettati dall'owner:**
- il controllo su `standard_conforming_strings` è testuale: un aggiramento con `U&'…'` o `set_config(…)` richiederebbe un'istruzione di primo livello iniettata nel file, che `pg_dumpall` non produce; il file viene comunque dal `pg_dumpall` del workflow nella stessa esecuzione;
- un valore in allowlist che contenga un backslash (scritto `E'…'`) non ha la forma attesa: non viene saltato, viene rieseguito e, se il parametro è sconosciuto, il job fallisce in modo chiuso (`INVALID_SETTING`).

Gli errori di `pg_dump`, `pg_dumpall`, `pg_restore` e `psql` non vengono **mai** stampati: possono citare righe di produzione (`DETAIL`, `CONTEXT`, `Command was`). Il log mostra solo il codice e una classe fissa, per esempio `PG_RESTORE_FAILED class=ALREADY_EXISTS`, `class=PERMISSION_DENIED`, `class=UNIQUE_VIOLATION`, `class=OTHER`. Per la diagnosi serve un'indagine dell'owner fuori dal workflow.

Non aggirare un FAIL: documentalo sulla #130.

## 3. Spostare il file su Google Drive e cancellare l'artifact

1. Il coordinatore scarica l'artifact `fcb-defaultdb-backup-<run_id>-<attempt>`, cioè lo zip con `.tar.age` e `.tar.age.sha256`. Poi verifica:
   ```
   sha256sum -c fcb-defaultdb-<run_id>-<attempt>.tar.age.sha256
   ```
   L'hash deve essere uguale a `encrypted_sha256` del summary.
2. Il coordinatore carica **entrambi i file** nella cartella privata di Drive **`FCB-WP009-Backups`**.
3. Lo riscarica e **riverifica lo SHA-256**, che deve essere uguale a `encrypted_sha256`.
4. Solo dopo la verifica su Drive, **l'owner elimina esplicitamente l'artifact su GitHub**: run → Artifacts → icona del cestino, oppure `gh api -X DELETE repos/<owner>/<repo>/actions/artifacts/<artifact_id>`. La retention di 1 giorno è solo una rete di sicurezza: l'eliminazione resta un atto manuale dell'owner.
5. Sulla #130 annota:
   - run id;
   - `encrypted_sha256`;
   - `plaintext_sha256`;
   - Drive verificato;
   - artifact eliminato.

   Distingui VERIFIED da ASSUMED.

Su Drive il file è solo testo cifrato. La passphrase **non** va mai su Drive.

## 4. Procedura esatta di decrypt e restore (recovery reale)

Usa una macchina fidata con `age` ≥ 1.1 e PostgreSQL 18 (client, e server per il restore locale).

```
# 1. integrità del file cifrato
sha256sum -c fcb-defaultdb-<run>.tar.age.sha256

# 2. decrypt (age chiede la passphrase dal password manager; mai su riga di comando)
age -d -o fcb-defaultdb-<run>.tar fcb-defaultdb-<run>.tar.age
sha256sum fcb-defaultdb-<run>.tar        # = plaintext_sha256 del run

# 3. estrazione in una cartella privata
umask 077; mkdir restore && tar -xf fcb-defaultdb-<run>.tar -C restore
#    -> defaultdb.dump  roles.sql  database.sql  manifest.json
```

**Confronto del digest pubblico con il manifest** (impostazioni gestite saltate nel restore-test). Ricalcola il digest dall'elenco cifrato con la codifica canonica documentata sopra; i due numeri devono essere uguali a `skipped=N sha256=…` della riga `roles_sql_aiven_managed_settings_skipped_in_restore_test` del summary del run e a `restore_test_skipped_role_settings_sha256` del manifest. Se differiscono, il pacchetto non corrisponde al run: fermati e annota sulla #130.

```
python3 -I -c 'import json,hashlib; m=json.load(open("restore/manifest.json")); l=m["restore_test_skipped_role_settings"]; c=json.dumps(sorted(x.split(" ",1) for x in l),ensure_ascii=True,separators=(",",":")); print(f"skipped={len(l)} sha256={hashlib.sha256(c.encode()).hexdigest()}", m["restore_test_skipped_role_settings_sha256"])'
```

L'elenco stampa nomi di ruoli e parametri solo sulla macchina fidata: non incollarlo in issue, PR o chat.

**A. Restore su un PostgreSQL 18 di cui sei superuser** (verifica o recovery locale). È la procedura dei test.

```
# il bootstrap del cluster ha lo stesso nome del bootstrap sorgente (di solito postgres)
initdb -D ./pgdata -U postgres -E UTF8 --locale=C.UTF-8 --auth=trust
pg_ctl -D ./pgdata -o "-c listen_addresses='' -c unix_socket_directories=$PWD" start
# roles.sql di Aiven: togli SOLO le impostazioni gestite in allowlist (come il restore-test)
grep -v -E '^ALTER ROLE (_aiven|_aiven_repl|postgres|_avnadmin_monitor) SET ' restore/roles.sql > restore/roles_local.sql
diff restore/roles.sql restore/roles_local.sql | grep -c '^<'
#    deve essere uguale al numero di voci di restore_test_skipped_role_settings nel manifest
#    e allo skipped=N del summary del run
#    (29 sul servizio osservato); se è diverso, fermati: c'è un valore su più righe o un parametro nuovo
psql -h "$PWD" -U postgres -X -f restore/roles_local.sql
#    unico errore atteso: role "postgres" already exists
createdb -h "$PWD" -U postgres -T template0 -E UTF8 -O avnadmin defaultdb
# istruzioni a livello di database (vedi sotto), poi gli oggetti
grep -E '^(ALTER DATABASE |ALTER ROLE [^ ]+ IN DATABASE |GRANT |REVOKE |COMMENT ON DATABASE )' restore/database.sql > restore/database_replay.sql
psql -h "$PWD" -U postgres -X -v ON_ERROR_STOP=1 -1 -f restore/database_replay.sql
pg_restore --exit-on-error --single-transaction --no-statistics -h "$PWD" -U postgres -d defaultdb restore/defaultdb.dump
```

`-O avnadmin` indica l'owner del database sorgente. Lo trovi in `database.sql` (`ALTER DATABASE … OWNER TO …`). Se ripristini con un nome di database diverso da `defaultdb`, sostituisci il nome in `database_replay.sql`.

Su un PostgreSQL standard le impostazioni saltate non hanno effetto (moduli Aiven assenti); `default_transaction_*` di `postgres` non va riapplicato al bootstrap locale.

**B. Restore su un nuovo servizio Aiven** (`avnadmin` non è superuser; ASSUMED, non testato su Aiven):
1. Come `avnadmin` crea solo i ruoli applicativi che il dump referenzia e che non esistono, per esempio `fcb_app`, `fcb_migrator` e `fcb_signer`, con gli attributi indicati in `roles.sql`. **Non eseguire `roles.sql` per intero.** Su un servizio Aiven i ruoli `_aiven`, `_aiven_repl`, `_aiven_rewind`, `_avnadmin_auditing`, `_avnadmin_managed`, `_avnadmin_monitor`, `postgres`, `avnadmin` e `avnadmin_group` esistono già e appartengono al provider: le loro `CREATE ROLE`/`ALTER ROLE … WITH`, le loro impostazioni `ALTER ROLE … SET` e le membership tra di loro (per esempio `GRANT _avnadmin_managed TO avnadmin_group`) **non si rieseguono** (fallirebbero o modificherebbero la configurazione del provider). Estrai da `roles.sql` solo le istruzioni il cui ruolo **non** è in quell'elenco (nome esatto, non prefisso), più le `GRANT` che hanno un ruolo applicativo come membro o come ruolo concesso; rileggile e applicale con `ON_ERROR_STOP=1`. Poi confronta le membership con `memberships` del manifest.
2. Applica `database_replay.sql` (come in A) per le istruzioni che `avnadmin` può eseguire: `ALTER DATABASE … SET`, `GRANT`/`REVOKE … ON DATABASE`, `ALTER ROLE … IN DATABASE … SET` sui ruoli che amministra.
3. Lancia `pg_restore --exit-on-error --single-transaction --no-statistics -d defaultdb restore/defaultdb.dump` come `avnadmin`, con `PGSSLMODE=verify-full` e la CA del nuovo servizio.
4. Imposta **nuove password** con il workflow dei ruoli WP-009 (fase 1 o rotazione), mai a mano in chat.
5. Il cambio di `DATABASE_URL` su Render è un atto dell'owner.

**Verifica dopo il restore** (A o B):
- `psql -X -A -t -f sql/aiven/count_bridge_catalog.sql`, uguale a `catalog_counts` del manifest;
- `psql -X -A -t -f sql/aiven/fingerprint_bridge_structure.sql`, uguale a `structure_fingerprint`;
- il numero di righe per tabella uguale a `tables.<nome>.rows` del manifest.

`database.sql`: si rieseguono **tutte** le righe `ALTER DATABASE …`, `ALTER ROLE … IN DATABASE …`, `GRANT`/`REVOKE … ON DATABASE …` e `COMMENT ON DATABASE …` (è ciò che fa il restore-test del workflow). Non si riesegue il `CREATE DATABASE`, né un eventuale `CREATE`/`COMMENT ON EXTENSION`.

Alla fine **elimina** tar, cartella `restore/` e cluster locale. Il `.tar.age` su Drive resta l'unica copia.

## 5. Limiti del rollback (da leggere prima di contarci)

- **Nessuna password dei ruoli.** `--no-role-passwords` esclude password e hash. Dopo un restore servono password nuove: workflow ruoli, segreti, Render.
- **Ruoli gestiti da Aiven** (`_aiven`, `_aiven_repl`, `_aiven_rewind`, `_avnadmin_auditing`, `_avnadmin_managed`, `_avnadmin_monitor`, `postgres`, e `avnadmin`/`avnadmin_group` creati dal provider):
  - compaiono in `roles.sql` completi, con attributi, membership e impostazioni;
  - su Aiven non si ricreano né si modificano;
  - lo schema `_avnadmin_monitor` è escluso dal dump.
- **Limiti della verifica dei ruoli gestiti.** Il restore-test gira su PostgreSQL standard:
  - attributi e membership dei ruoli gestiti sono ricreati e confrontati, ma le **impostazioni in allowlist non sono verificate** (non rieseguite, escluse dal confronto su entrambi i lati): il backup dimostra solo che sono nel `roles.sql` salvato;
  - il restore-test non può dimostrare che quelle impostazioni si riapplichino su un nuovo servizio Aiven, né che i ruoli gestiti abbiano lì gli stessi attributi: li crea il provider;
  - un nuovo parametro o un nuovo ruolo di servizio che Aiven aggiunga in futuro **non** è in allowlist: il job fallisce in modo chiuso (`INVALID_SETTING`) finché l'owner non approva un aggiornamento esplicito dell'allowlist con una PR.
- **Cosa non è catturato:**
  - stato del servizio Aiven (piano, `ip_filter`, termination protection, utenti del pannello);
  - estensioni fuori da `public`: il backup le rifiuta (`EXTENSION_OUTSIDE_PUBLIC`); un'estensione in `public` è nel dump ma deve essere disponibile sul server di destinazione;
  - statistiche (`--no-statistics`).
- **Punto nel tempo.** Il dump è lo stato dello snapshot. Tutte le scritture successive (token OAuth, lease, contatori di quota) **vanno perse** con un restore. Il runtime va fermato o messo in manutenzione prima del ripristino, per evitare stati misti.
- **Piano developer di Aiven senza fork né PITR** (`DOCS_UNVERIFIED`): non va dato per scontato che Aiven offra un ripristino a un istante arbitrario o un fork del servizio. Questo backup logico è l'unica copia sotto controllo dell'owner. Verifica il piano sulla documentazione ufficiale Aiven prima di fare affidamento su altro.
- **Stato WP-009.** Il backup come amministratore è ammesso solo finché l'amministratore possiede tutto: stato 0001-0008, prima della fase 2. Dopo la fase 2 la guardia lo rifiuta.
- **Retention e visibilità.** L'artifact dura al massimo 1 giorno ed è visibile a chi ha accesso in lettura al repository: contiene solo testo cifrato. Va comunque eliminato esplicitamente dall'owner dopo la verifica su Drive.
- **Passphrase persa = backup perso.** Nessuno, nemmeno l'agente, può recuperarla.

## Decisioni aperte per l'owner

- Il backup dopo la fase 2 di WP-009, cioè con il bridge di `fcb_migrator` (stati 0009 e 0010), richiede un'identità diversa (`fcb_migrator`) e nuovi valori fissati: serve una nuova decisione.
- Il test di integrazione PG18 richiede i binari server di PostgreSQL 18 (`initdb`), che la CI attuale non installa. Senza una modifica alla CI, decisa dall'owner, in CI viene saltato.
- Frequenza dei backup, numero di copie su Drive e rotazione della passphrase.
- Ruoli gestiti da Aiven: un helper che produca la versione filtrata di `roles.sql` per la recovery (oggi un `grep` manuale con controllo del conteggio) è una possibile evoluzione.
