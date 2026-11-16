# PATH A2 — matrice dei 9 privilegi di `fcb_cert_runner` su `defaultdb`

Fonte: `sql/aiven/apifootball_cert_path_a_coordinator_grants.sql` (concessione) e
`sql/aiven/apifootball_cert_path_a_coordinator_assert.sql` (verifica dell'insieme esatto, nella stessa transazione).

**Stato:** documento di preparazione della PR #201. Nessun GRANT è stato applicato in produzione.
L'applicazione richiede una nuova ratifica scritta dell'owner, dopo i gate WP-009.

## Struttura

I 9 privilegi sono concessi con 3 istruzioni `GRANT` su 4 oggetti: 3 tabelle più 1 colonna.
Non è concesso nessun `WITH GRANT OPTION`, nessun privilegio a `PUBLIC` e nessun privilegio su altre tabelle.

| # | Oggetto | Privilegio | Istruzione GRANT |
|---|---|---|---|
| 1 | `api_football_quota_counters` | `SELECT` | `GRANT SELECT, INSERT ON TABLE …counters` |
| 2 | `api_football_quota_counters` | `INSERT` | idem |
| 3 | `api_football_quota_counters.used` (colonna) | `UPDATE (used)` | `GRANT UPDATE (used) ON TABLE …counters` |
| 4 | `api_football_quota_leases` | `SELECT` | `GRANT SELECT, INSERT, DELETE ON TABLE …leases, …attempts` |
| 5 | `api_football_quota_leases` | `INSERT` | idem |
| 6 | `api_football_quota_leases` | `DELETE` | idem |
| 7 | `api_football_quota_attempts` | `SELECT` | idem |
| 8 | `api_football_quota_attempts` | `INSERT` | idem |
| 9 | `api_football_quota_attempts` | `DELETE` | idem |

## Necessità di ciascun privilegio

Ogni privilegio corrisponde a un'istruzione del coordinatore condiviso (`bridge/api_football/quota.py`), che il runner esegue con la propria identità. I riferimenti sono al codice del branch della PR.

| # | Istruzione che lo richiede | Riferimento | Senza il privilegio |
|---|---|---|---|
| 1 | `SELECT used FROM api_football_quota_counters …` (stato del budget e report); lettura delle colonne nel `WHERE` e nel `RETURNING` dell'upsert | `quota.py:224`, `quota.py:266-272`, `quota.py:331` | la prenotazione fallisce: nessuna chiamata al provider |
| 2 | `INSERT INTO api_football_quota_counters … ON CONFLICT … DO UPDATE` (nuovo bucket minuto/giorno) | `quota.py:266`, `quota.py:386` | idem |
| 3 | `DO UPDATE SET used = …used+1` / `GREATEST(…)`: l'upsert aggiorna **solo** la colonna `used` | `quota.py:270`, `quota.py:390` | idem; nessun'altra colonna è aggiornabile (`window_kind`, `bucket` e `api_key_hash` restano immutabili) |
| 4 | `SELECT count(*) … FROM api_football_quota_leases` (concorrenza attiva) | `quota.py:211`, `quota.py:338` | idem |
| 5 | `INSERT INTO api_football_quota_leases (lease_id, …, expires_at)` | `quota.py:288` | idem |
| 6 | `DELETE FROM api_football_quota_leases` (lease scaduti; rilascio del proprio lease) | `quota.py:206`, `quota.py:310` | i lease non vengono rilasciati: la concorrenza resta occupata fino al fail-closed |
| 7 | `SELECT … FROM api_football_quota_attempts` (finestra mobile; idempotenza) | `quota.py:121`, `quota.py:242`, `quota.py:345` | la prenotazione fallisce |
| 8 | `INSERT INTO api_football_quota_attempts` (ogni tentativo; slot "provider zero" con `ON CONFLICT DO NOTHING`) | `quota.py:283`, `quota.py:397` | idem |
| 9 | `DELETE FROM api_football_quota_attempts` (potatura dei tentativi fuori finestra) | `quota.py:236` | la finestra mobile cresce senza limite e il conteggio fallisce chiuso |

`pg_advisory_xact_lock` (`quota.py:201`, `quota.py:380`) non richiede privilegi su tabelle.

## Privilegi esplicitamente esclusi

Ogni voce è verificata da `apifootball_cert_path_a_coordinator_assert.sql` e, per il restore, dal precheck e dai guard di `defaultdb`. Le prove di manomissione si trovano in `tests/integration/test_defaultdb_restore_postgres.py::A2_TAMPER` e in `tests/integration/test_apifootball_cert_path_a_postgres.py`.

| Esclusione | Prova di manomissione che deve essere rifiutata |
|---|---|
| `TRUNCATE`, `UPDATE` di tabella, `REFERENCES`, `TRIGGER` | `extra_table_privilege` |
| `UPDATE` su colonne diverse da `used` | `extra_column_privilege` |
| `WITH GRANT OPTION` | `grant_option` |
| Privilegio mancante: l'insieme deve essere **esattamente** quello previsto | `missing_privilege` |
| Qualunque altra tabella di `defaultdb` (OAuth, lease browser, ledger SX Bet …) | `other_table` |
| Concessioni ad altri ruoli o a `PUBLIC` sulle tre tabelle | `other_role`, `public_grant` |
| Attributi del ruolo (`CREATEDB`, `CREATEROLE`, `BYPASSRLS`, `SUPERUSER`, `REPLICATION`, `INHERIT`) | `runner_not_plain` |
| Membership del runner in altri ruoli | `runner_member` |
| Altri ruoli che possono ereditare dal runner o agire come runner (`INHERIT` / `SET`) | `member_inherit`, `member_set` |
| Ownership delle tabelle al runner | `owner_change` |

`USAGE` sullo schema `public` è concesso esplicitamente in `apifootball_cert_path_a_roles.sql`, è fuori dai 9 privilegi ed è escluso dal fingerprint del restore.

## Gate prima dell'applicazione in produzione (non autorizzata)

1. Merge della #201 con un GO owner, dopo CI verde e review indipendente SAFE sullo SHA finale.
2. Completamento dei gate WP-009: fase 1, switch di Render a `fcb_app`, 24 ore di osservazione, G1/G2.
   Dopo la fase 2 il proprietario delle tabelle è `fcb_migrator`: il workflow di installazione richiede quell'identità.
3. Ratifica scritta dell'owner di questa matrice. Il documento non autorizza nessun GRANT.
