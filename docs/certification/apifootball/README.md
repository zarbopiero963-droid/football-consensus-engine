# API-FOOTBALL v3 — matrice di certificazione offline

Questa cartella contiene `matrix.json` e `matrix.csv`. Sono generati da `scripts/apifootball_cert_matrix.py`
a partire **solo** da fonti del repository:

- `bridge/api_football/contracts.py`, cioè i contratti, i tipi, i vincoli e le regole di combinazione estratte via AST da `validate()` e `fixtures_combination()`;
- gli schemi MCP reali, prodotti da `register_tools()` (la stessa funzione chiamata da `bridge/server.py`) sulla classe server dell'SDK MCP;
- i test offline, letti con una scansione statica di `tests/test_*.py` e `tests/integration/test_*.py`;
- le evidenze live già committate;
- lo snapshot della documentazione `docs/api_football/official_parameter_matrix_v3.9.3.json`.

La generazione non fa chiamate al provider, a MCP, a Render o al database e non consuma quota. Il risultato è deterministico: non contiene orari né dati dell'host.

```bash
python scripts/apifootball_cert_matrix.py           # rigenera matrix.json e matrix.csv
python scripts/apifootball_cert_matrix.py --check   # exit 1 se i file committati non sono aggiornati
python scripts/apifootball_cert_matrix.py --gate    # exit 1 se una riga MANDATORY non è PASS live
```

`--check` dipende anche dai test. Se si aggiunge o si modifica un test API-FOOTBALL, la matrice va rigenerata.
`tests/test_apifootball_cert_matrix.py` lo verifica.

## Righe

Il file ha 366 righe e copre 42 tool MCP:

| kind | righe | contenuto |
|---|---|---|
| `PARAMETER` | 142 | Una riga per ogni (tool, parametro) del contratto. Riporta il nome MCP (`from` → `from_date`, `to` → `to_date`), il tipo nello schema MCP, `required`, il tipo e i vincoli del contratto e il confronto con lo snapshot della documentazione. |
| `BEHAVIOUR` | 173 | Per ogni tool provider: `ROUTE_CALL`, `EMPTY_VALID`, `NULL_HANDLING`, `PAYLOAD_CAP` e, se il tool è paginato, `PAGINATION`. Ci sono poi 9 righe trasversali (`*`): errori HTTP 401, 403, 429, 5xx e timeout via transport mockato, `PROVIDER_DECLARED_ERRORS`, null preservati, empty valid e cap a 256 KiB. |
| `RULE` | 49 | Rifiuti locali con codice fisso (`*_FILTER_REQUIRED`, `REQUIRED_PARAMETER_MISSING`, `SEARCH_TOO_SHORT`, ...) e combinazioni di `/fixtures`, etichettate `DOCUMENTED_VALID`, `DOCS_UNVERIFIED`, `MCP_REJECTED` o `PROVIDER_REJECTED_OBSERVED`. |
| `LOCAL_TOOL` | 2 | `apifootball_health` e `apifootball_quota_report`, che non fanno chiamate al provider. |

## Colonne principali e vocabolario

- **offline_status**: `COVERED` se almeno una funzione di test nomina il tool e il parametro, il codice o il comportamento. Il testo considerato include le tabelle di modulo che la funzione usa. `offline_tests` elenca gli id `file::test`. È un'euristica statica: indica che esiste un test pertinente, non che quel test asserisca il comportamento.
- **verdict** (CLAUDE.md §4): `HTTP_200`, `DATA_PRESENT`, `EMPTY_VALID`, `PARTIAL`, `ERROR`, `NOT_TESTED`. Riporta la migliore evidenza live disponibile, con la fonte citata in `live_evidence_source`.
- **live_requirement**:
  - `MANDATORY`: va provato live per certificare;
  - `DATA_DEPENDENT`: si può provare solo se il dato esiste in una certa finestra (vedi sotto);
  - `OFFLINE_ONLY`: non si deve provocare live, perché è un rifiuto locale a 0 chiamate oppure un errore 401, 403, 429, 5xx o timeout, che richiederebbe una chiave invalida o consumerebbe quota.
- **gate_status**:
  - `PASS`: c'è evidenza live sul codice corrente;
  - `STALE`: l'evidenza è reale ma non certifica il codice corrente;
  - `NOT_TESTED`;
  - `BLOCKED`;
  - `FAIL`: `ERROR` non atteso;
  - `NOT_APPLICABLE`.
- **Gate:** fallisce se una riga `MANDATORY` è `FAIL`, `BLOCKED`, `NOT_TESTED` o `STALE`.
- **Perché tutta l'evidenza esistente è `STALE`:**
  - la route inventory del 2026-10-25 e le capability run del 2026-10-31 sono script httpx diretti, fuori dal path MCP e dal coordinatore (gap G1 in `../areas/apifootball.md`);
  - la certificazione MCP del 2026-11-11 è stata fatta su `848c007`, e dopo `bridge/api_football` è cambiato (`e155193`).
- **docs_status**: vale `DOCS_UNVERIFIED` su tutte le righe. La pagina ufficiale `https://www.api-football.com/documentation-v3`, richiesta una volta il 2026-11-11, ha risposto HTTP 403 con il challenge Cloudflare, che non è stato aggirato.
- **docs_snapshot_status**: è il confronto con lo snapshot del 2026-11-01, che non ha capture raw né hash. Valori: `MATCH` / `MISSING_IN_CONTRACT` / `MISSING_IN_DOCS` / `DOCS_UNVERIFIED`.

## Stato attuale (generato)

| metrica | valore |
|---|---|
| righe | 366 |
| offline `COVERED` / `NOT_COVERED` | 312 / 54 |
| verdict `NOT_TESTED` | 319 |
| verdict con evidenza | `DATA_PRESENT` 44, `EMPTY_VALID` 1, `HTTP_200` 1, `ERROR` 1 (A04, atteso) |
| `MANDATORY` / `DATA_DEPENDENT` / `OFFLINE_ONLY` | 176 / 139 / 51 |
| gate | **FAIL**: 176 righe `MANDATORY` non PASS (135 `NOT_TESTED`, 41 `STALE`) |
| docs snapshot `MATCH` / `DOCS_UNVERIFIED` | 308 / 58 (nessun `MISSING_IN_CONTRACT` o `MISSING_IN_DOCS`) |

### Gap offline (54 righe `NOT_COVERED`)

| Gruppo | Righe | Dettaglio |
|---|---|---|
| `PAYLOAD_CAP` per route | 19 | Il cap è coperto in modo trasversale (`*|PAYLOAD_CAP_256KIB`), non per singola route. |
| `EMPTY_VALID` per route | 12 | — |
| `REQUIRED_PARAMETER_MISSING` | 12 tool | Il set `required` è verificato contro la documentazione (`test_apifootball_official_parameter_matrix.py`), ma il rifiuto con quel codice no. |
| Parametri | 6 | `bets.search`, `bookmakers.search`, `countries.search`, `venues.city`, `fixture_headtohead.next`, `fixture_lineups.type` |
| Regole | 5 | `INVALID_COUNTRY_CODE`, `LIVE_ODDS_FILTER_REQUIRED`, `LEAGUE_SEASON_PAIR_REQUIRED`, `teams SEARCH_TOO_SHORT`, `TEAM_FILTER_REQUIRED` |

### Divergenze tra contratto e snapshot (`CONSTRAINT_DRIFT`, non corrette qui)

- **Lunghezza minima di `search`.** Lo snapshot indica `= 3` o `>= 3` caratteri per `countries`, `bets`, `bookmakers`, `coaches` e `venues`. Il contratto ammette 1 carattere: la richiesta arriva al provider, che resta l'autorità finale.
- **Tipo di `odds/bets.id`.** La documentazione lo dà come `string`, il contratto come `integer`.

Le due divergenze sono documentate qui. Qualunque correzione richiede una PR dedicata (CLAUDE.md §0).

## Budget live (stima, da approvare dall'owner, CLAUDE.md §2.4)

Ogni chiamata passa dal coordinatore PostgreSQL condiviso attraverso i tool MCP. Nessuna di queste chiamate è stata eseguita.

| Gruppo | Righe | Chiamate (limite superiore) | Stima con accorpamento |
|---|---|---|---|
| `MANDATORY` `ROUTE_CALL` (37 `STALE`, 1 `NOT_TESTED`) e 24 parametri obbligatori | 62 | 62 | 38: una chiamata per tool, che copre anche i parametri obbligatori |
| `MANDATORY` parametri opzionali | 106 | 106 | circa 100. Ogni parametro va isolato, con i soli parametri obbligatori, per attribuirgli l'esito. I parametri più numerosi sono `fixtures` 14, `headtohead` 10, `leagues` 10, `injuries` 8 e `teams` 8. |
| `MANDATORY` regole `F-*` di `/fixtures` | 5 | 5 | 0: coincidono con le chiamate di `id`, `ids`, `date`, `league+season` e `from/to` |
| `MANDATORY` paginazione (`players`, `player_profiles`, `odds_mapping`) | 3 | 6 | 6: pagina 2 e una pagina oltre il totale |
| `DATA_DEPENDENT` quote pre-match e live, `fixtures.live`, `F-LIVE` | 16 | 17 | circa 17, da eseguire in una finestra con partite in corso o imminenti |
| `DATA_DEPENDENT` `PAYLOAD_CAP` | 40 | 40 | circa 5: solo le risposte realmente oltre 256 KiB, cioè `leagues` senza filtro, `odds` per data, `fixtures` per data, `transfers` e `odds/live` con molte partite |
| `DATA_DEPENDENT` `EMPTY_VALID` e `NULL_HANDLING` | 80 | 80 | circa 0–10: si osservano sulle chiamate precedenti, più poche chiamate mirate per pagine o id vuoti |
| **Totale** | | **316** (`summary.live_budget`) | **circa 170–180** |

## Righe `DATA_DEPENDENT`

- **Finestra temporale:**
  - `/odds`: le quote pre-match esistono solo nei giorni prima del calcio d'inizio;
  - `/odds/live`: serve una partita in corso con il mercato aperto;
  - `fixtures.live` e la regola `F-LIVE`: serve una partita in corso.
- **Dato reale:**
  - `PAYLOAD_CAP`: serve una risposta reale oltre 256 KiB;
  - `NULL_HANDLING`: serve un payload con campi null pubblicati dal provider;
  - `EMPTY_VALID`: serve un filtro che il provider restituisce vuoto.

Una riga `DATA_DEPENDENT` non certificata non fa fallire il gate, ma resta `NOT_TESTED` o `STALE` nella matrice. Non si dichiara mai coperta per assenza di dati (CLAUDE.md §7).

## Proposta di step CI (solo offline)

Questa è una proposta: non è stato aggiunto nessun workflow.

```yaml
- name: API-FOOTBALL cert matrix is up to date (offline)
  run: python scripts/apifootball_cert_matrix.py --check
```

`--gate` va usato solo dopo una campagna live autorizzata dall'owner. Oggi fallisce per costruzione.
