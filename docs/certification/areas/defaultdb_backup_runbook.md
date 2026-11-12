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
- c'è un `FORCE ROW LEVEL SECURITY`.

È la stessa regola del runbook WP-009: l'amministratore legge righe solo quando è dimostrato che possiede tutto.

**Dopo la fase 2 di WP-009** il bridge appartiene a `fcb_migrator` e il backup come amministratore viene **rifiutato** (`PUBLIC_OBJECT_NOT_OWNED_BY_SESSION_USER`). Un backup in quello stato richiede una nuova decisione dell'owner (vedi "Decisioni aperte").

### Ruoli
`pg_dumpall --roles-only --no-role-passwords` esporta ruoli, attributi, membership e impostazioni `ALTER ROLE … SET`, **senza password né hash**. Se nel file compare `PASSWORD`, un verifier `SCRAM-SHA-256$…` o un hash `md5…`, il job fallisce.

Le proprietà del database (`CREATE DATABASE … LOCALE`, `ALTER DATABASE … SET`, ACL del database) vanno in `database.sql`, prodotto con `pg_dump --create --schema-only --exclude-schema=*`.

### Restore-test isolato
Il restore-test gira in un cluster PostgreSQL 18 **isolato sul runner**:
- solo socket unix privato;
- nessun listener TCP;
- nessun segreto nell'ambiente del server;
- la connessione alla sorgente è già chiusa.

I passi sono:
1. `roles.sql` viene applicato. L'unico errore ammesso è `role "<bootstrap>" already exists`.
2. Si crea il database `defaultdb`.
3. `pg_restore --exit-on-error --single-transaction`.

Un exit code 0 di `pg_restore` **non basta**. Il job fallisce, prima di cifrare, se anche una sola di queste verifiche differisce tra snapshot sorgente e restore:
- conteggi oggetti e fingerprint di struttura, uguali anche ai valori fissati per 0001-0008;
- `schema_migrations` 1..8 (nomi e checksum del repository);
- funzioni (firma e md5 della definizione);
- sequenze (`last_value` e `is_called`). Le sequenze non devono muoversi durante il dump;
- owner di ogni tabella, sequenza e funzione;
- ACL normalizzate (`relacl`, ACL di colonna, `proacl`, ACL dello schema `public`);
- ruoli (nomi e attributi `LOGIN`, `CREATEROLE`, …) e membership, confrontati con il catalogo sorgente;
- per ogni tabella: `SELECT` reale, numero di righe e hash del contenuto (SHA-256 delle righe ordinate).

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
  - `roles_sql_without_passwords`, `roles_names_attributes_match_source`, `role_memberships_match_source`;
  - `pg_restore_exit_on_error`, `restore_catalog_counts`, `restore_structure`, `restore_schema_migrations_1_8`;
  - `restore_functions`, `restore_sequences_last_value_is_called`, `restore_owners`, `restore_acls_normalized`;
  - `rows <tabella>` e `content <tabella>` per ogni tabella, `restore_content_sha256`;
  - `decrypt_roundtrip_plaintext_sha256`, `tampered_ciphertext_rejected`;
  - `plaintext_deleted_before_upload`, `artifact_dir_only_ciphertext_and_sha256`;
- in fondo: `encrypted_file`, `encrypted_sha256`, `encrypted_bytes`, `plaintext_sha256`, `plaintext_bytes`, `excluded_schemas`, versioni, `source_commit`, `created_at_utc`;
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
- `ROLES_SQL_APPLY_FAILED`;
- `rows <tabella>`.

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

**A. Restore su un PostgreSQL 18 di cui sei superuser** (verifica o recovery locale). È la procedura dei test.

```
# il bootstrap del cluster ha lo stesso nome del bootstrap sorgente (di solito postgres)
initdb -D ./pgdata -U postgres -E UTF8 --locale=C.UTF-8 --auth=trust
pg_ctl -D ./pgdata -o "-c listen_addresses='' -c unix_socket_directories=$PWD" start
psql -h "$PWD" -U postgres -X -f restore/roles.sql
#    unico errore atteso: role "postgres" already exists
createdb -h "$PWD" -U postgres -T template0 -E UTF8 -O avnadmin defaultdb
pg_restore --exit-on-error --single-transaction --no-statistics -h "$PWD" -U postgres -d defaultdb restore/defaultdb.dump
```

`-O avnadmin` indica l'owner del database sorgente. Lo trovi in `database.sql` (`ALTER DATABASE … OWNER TO …`).

**B. Restore su un nuovo servizio Aiven** (`avnadmin` non è superuser; ASSUMED, non testato su Aiven):
1. Come `avnadmin` crea solo i ruoli applicativi che il dump referenzia e che non esistono, per esempio `fcb_app`, `fcb_migrator` e `fcb_signer`, con gli attributi indicati in `roles.sql`. Non eseguire `roles.sql` per intero: i ruoli gestiti da Aiven esistono già o non sono creabili.
2. Lancia `pg_restore --exit-on-error --single-transaction --no-statistics -d defaultdb restore/defaultdb.dump` come `avnadmin`, con `PGSSLMODE=verify-full` e la CA del nuovo servizio.
3. Imposta **nuove password** con il workflow dei ruoli WP-009 (fase 1 o rotazione), mai a mano in chat.
4. Il cambio di `DATABASE_URL` su Render è un atto dell'owner.

**Verifica dopo il restore** (A o B):
- `psql -X -A -t -f sql/aiven/count_bridge_catalog.sql`, uguale a `catalog_counts` del manifest;
- `psql -X -A -t -f sql/aiven/fingerprint_bridge_structure.sql`, uguale a `structure_fingerprint`;
- il numero di righe per tabella uguale a `tables.<nome>.rows` del manifest.

`database.sql` è un riferimento: applica a mano solo le righe `ALTER DATABASE … SET …` e `GRANT … ON DATABASE …` pertinenti, non il `CREATE DATABASE`.

Alla fine **elimina** tar, cartella `restore/` e cluster locale. Il `.tar.age` su Drive resta l'unica copia.

## 5. Limiti del rollback (da leggere prima di contarci)

- **Nessuna password dei ruoli.** `--no-role-passwords` esclude password e hash. Dopo un restore servono password nuove: workflow ruoli, segreti, Render.
- **Ruoli gestiti da Aiven** (`avnadmin`, `_avnadmin_monitor`, `postgres` e gli altri interni):
  - compaiono in `roles.sql` solo come riferimento;
  - su Aiven non si ricreano né si modificano;
  - lo schema `_avnadmin_monitor` è escluso dal dump.
- **Cosa non è catturato:**
  - impostazioni `ALTER ROLE … IN DATABASE …`;
  - stato del servizio Aiven (piano, `ip_filter`, termination protection, utenti del pannello);
  - estensioni installate fuori dal dump;
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
