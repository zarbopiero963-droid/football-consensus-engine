# Runbook — certificazione ChatGPT / Claude.ai → MCP → API-FOOTBALL

Questo è il percorso che i test automatici **non** coprono:

`client reale (ChatGPT o Claude.ai) → OAuth → MCP Render → backend → API-FOOTBALL → risposta → client`

`tests/test_mcp_offline_protocol_e2e.py` verifica offline HTTP, OAuth middleware, `initialize`, `tools/list`, `tools/call` e la corrispondenza tra schema e contratti. Il risultato vale `OFFLINE TESTED`: non è `REAL_DATA_VERIFIED`.

## Prerequisiti (owner)
1. Deploy di `main` attivo su `football-consensus-browser-bridge`, con il commit annotato.
2. Connettore aggiornato nel client: disconnettere e riconnettere, oppure usare "refresh tools". In questo modo il client rilegge `tools/list` e non usa uno schema in cache.
3. Budget: al massimo **15 chiamate provider** per esecuzione. Il coordinatore applica comunque 80 al giorno, 8 al minuto, concorrenza configurata.

## Passi (una chiamata ciascuno, nell'ordine)
| # | Tool | Argomenti | Atteso | Stato da registrare |
|---|---|---|---|---|
| 0 | (lista tool del connettore) | — | 42 tool `apifootball_*`; `apifootball_fixtures` mostra `from_date`/`to_date`, `ids`, `round`, `venue` | EXPOSED / FAILED |
| 1 | `apifootball_health` | — | `LIVE_ENABLED`, `quota_coordinator: READY` | — (0 chiamate provider) |
| 2 | `apifootball_status` | — | piano e contatori, **senza** nome/email | REAL_DATA_VERIFIED |
| 3 | `apifootball_fixtures` | `live="all"` | elenco o `results: 0` | DATA_PRESENT / EMPTY_VALID |
| 4 | `apifootball_fixtures` | `league=39, season=2026, last=1` | 1 partita FT con `score.halftime`/`fulltime` | REAL_DATA_VERIFIED |
| 5 | `apifootball_fixture_statistics` | `fixture=<id del #4>` | `expected_goals`, `Total Shots`, `Ball Possession` | registrare MISSING / NULL senza convertirli in 0 |
| 6 | `apifootball_odds` | `fixture=<prossima partita>` | più bookmaker, `update` | REAL_DATA_VERIFIED |
| 7 | `apifootball_odds` | `league=39, season=2026` | `REAL_API_RESPONSE`, oppure `PARTIAL_ODDS_MANIFEST` senza prezzi (PR #125) | PARTIAL è corretto se è manifest |
| 8 | `apifootball_live_odds` | `fixture=<id live del #3>` | `odds[]`, `suspended` | DATA_PRESENT / EMPTY_VALID |
| 9 | `apifootball_bookmakers` | — | 33 voci, una con `name: null` (ID 37) | REAL_DATA_VERIFIED |
| 10 | `apifootball_standings` | `league=39, season=2026` | classifica | REAL_DATA_VERIFIED |
| 11 | `apifootball_fixtures` | `league="x"` | errore di validazione, 0 chiamate | FAILED_SAFE (corretto) |
| 12 | due chiamate ravvicinate dopo 15 min di inattività | qualunque tool | nessun `LOCAL_CONCURRENCY_BUSY` sulla seconda (PR #126) | PASS / FAIL |

## Registrazione
Per ogni riga conservare:
- timestamp UTC;
- client (ChatGPT o Claude.ai);
- commit deployato;
- `status`, `network_calls_made`, `cache_status`, `quota_remaining`;
- uno stralcio della risposta senza dati personali.

Vietato: incollare la API key o l'access token, e trascrivere dati non restituiti.
Il 100% è dichiarabile solo se ogni tool applicabile risulta EXPOSED e verificato secondo la tabella. Le funzionalità condizionate alla disponibilità del provider (live, quote) sono certificate per comportamento corretto (`EMPTY_VALID`), senza inventare copertura.
