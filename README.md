# Football Consensus Browser — MCP server

Server **MCP** (Model Context Protocol) remoto, protetto da OAuth. Espone ai client MCP (ChatGPT, Claude.ai/Claude Code) quattro componenti:

1. **API-FOOTBALL v3**: dati calcistici, statistiche, quote pre-match e live, cataloghi bookmaker e mercati. 42 tool `apifootball_*`.
2. **Browser Use**: browser cloud controllato via CDP + Playwright. Tool `browser_*`.
3. **CloakBrowser**: provider browser locale opzionale, integrato nello stesso bridge (`browser_create_cloak`). Non è un server MCP separato.
4. **SX Bet**: modulo **READ_ONLY** opzionale (`SXBET_ENABLED`), con 23 tool `sxbet_*` basati sull'OpenAPI ufficiale V3. Il trading reale **non è disponibile** (governance §2.6). Vedi [`docs/sxbet/README.md`](docs/sxbet/README.md).

Roadmap e stato canonico: issue [#130](https://github.com/zarbopiero963-droid/football-consensus-engine/issues/130).

> Agenti: leggere prima [AGENTS.md](AGENTS.md) e [CLAUDE.md](CLAUDE.md) (governance vincolante).

### Governance (sintesi, vincolante)
[AGENTS.md](AGENTS.md) e [CLAUDE.md](CLAUDE.md) prevalgono su codice e documentazione in caso di conflitto. Invarianti sempre attivi:
- OAuth obbligatorio su `/mcp` e protezione SSRF sui browser;
- nessun `cdpUrl` né `live_url` esposto per default;
- segreti solo in env;
- missing resta missing; nessuna troncatura silenziosa;
- UTC canonico; una timezone non dimostrata è `TIMEZONE_UNRESOLVED`;
- un contratto non verificabile resta `DOCS_UNVERIFIED`;
- nessun bypass di quote o rate limit;
- SX Bet: `LIVE_TRADING` disabilitato per default; ogni ordine reale richiede una conferma specifica dell'owner, e CI e deploy non piazzano mai ordini;
- le modifiche ai file di governance richiedono l'approvazione dell'owner.

## Architettura

| Componente | Codice | Note |
|---|---|---|
| Entrypoint MCP | `bridge/server.py` (`uvicorn bridge.server:app`) | MCP streamable HTTP su `/mcp/`, JSON response, stateless |
| OAuth 2.1 (PKCE S256, CIMD) | `bridge/oauth.py`, `bridge/oauth_store.py` | client ChatGPT e Claude.ai; `/mcp` senza token risponde **401** |
| Browser Use / Cloak | `bridge/browser_use.py`, `bridge/session_leases.py`, `bridge/security.py` | lease durevoli, reaper, stop esplicito, guard SSRF sugli URL pubblici |
| API-FOOTBALL | `bridge/api_football/` | contratti, transport con host pinning, coordinatore di quota PostgreSQL, cache, circuit breaker |
| Database | `app/db.py`, `app/migrations.py`, `sql/migrations/` | PostgreSQL (Neon): OAuth, lease delle sessioni, contatori di quota |
| Deploy | Render, servizio `football-consensus-browser-bridge` | **auto-deploy a ogni commit su `main`** |

## Strumenti MCP

**API-FOOTBALL (42):** `apifootball_health`, `apifootball_status`, `apifootball_quota_report`, `apifootball_coverage_report`, `apifootball_timezones`, `apifootball_countries`, `apifootball_leagues`, `apifootball_seasons`, `apifootball_teams`, `apifootball_team_statistics`, `apifootball_team_seasons`, `apifootball_team_countries`, `apifootball_venues`, `apifootball_standings`, `apifootball_fixture_rounds`, `apifootball_fixtures`, `apifootball_fixture_headtohead`, `apifootball_fixture_statistics`, `apifootball_fixture_events`, `apifootball_fixture_lineups`, `apifootball_fixture_players`, `apifootball_injuries`, `apifootball_predictions`, `apifootball_coaches`, `apifootball_player_seasons`, `apifootball_player_profiles`, `apifootball_players`, `apifootball_player_squads`, `apifootball_player_teams`, `apifootball_top_scorers`, `apifootball_top_assists`, `apifootball_top_yellow_cards`, `apifootball_top_red_cards`, `apifootball_transfers`, `apifootball_trophies`, `apifootball_sidelined`, `apifootball_odds`, `apifootball_odds_mapping`, `apifootball_bookmakers`, `apifootball_bets`, `apifootball_live_odds`, `apifootball_live_bets`.

**Browser (34):** `browser_create`, `browser_create_cloak`, `browser_open_url`, `browser_get_html`, `browser_get_dom`, `browser_get_elements`, `browser_screenshot`, `browser_click`, `browser_fill`, `browser_type`, `browser_press`, `browser_select_option`, `browser_set_checked`, `browser_hover`, `browser_focus`, `browser_scroll`, `browser_wait_for_selector`, `browser_wait`, `browser_back`, `browser_forward`, `browser_reload`, `browser_list_pages`, `browser_new_page`, `browser_switch_page`, `browser_close_page`, `browser_get_network_responses`, `browser_stop`, `browser_list_sessions`, `browser_session_status`, `browser_reap`, `browser_stop_all_idle`, `browser_stop_all`, `browser_health`, `browser_cleanup_report`.

Lo schema `tools/list` espone tutti i 140 parametri di query della documentazione ufficiale API-FOOTBALL v3.9.3 sui 38 endpoint (matrice: `docs/api_football/official_parameter_matrix_v3.9.3.json`; `from`/`to` compaiono come `from_date`/`to_date`). Il test `tests/test_mcp_offline_protocol_e2e.py` verifica questa corrispondenza.

## Collegare un client

1. Aggiungi un connettore MCP remoto con URL `https://<MCP_PUBLIC_HOST>/mcp/`.
2. Completa il flusso OAuth: la pagina di autorizzazione chiede la password operatore impostata in `MCP_OAUTH_PASSWORD`.
3. **Dopo ogni deploy che cambia gli strumenti, aggiorna o ricollega il connettore.** I client tengono in cache `tools/list`: un parametro nuovo, per esempio `page` su `apifootball_odds_mapping`, resta invisibile finché lo schema non viene riletto.

## Configurazione (solo nomi; i valori stanno in Render, mai nel repository)

| Variabile | Uso |
|---|---|
| `MCP_PUBLIC_HOST`, `MCP_OAUTH_PASSWORD`, `DATABASE_URL` | host pubblico, OAuth, PostgreSQL |
| `BROWSER_USE_API_KEY`, `BROWSER_STOP_LOCK_TIMEOUT_SECONDS`, `BROWSER_USE_EXPOSE_LIVE_URL` (default spento) | Browser Use |
| `CLOAKBROWSER_ENABLED`, `CLOAKBROWSER_HEADLESS`, `CLOAKBROWSER_HUMANIZE`, `CLOAKBROWSER_MAX_LOCAL_SESSIONS`, `CLOAKBROWSER_PROXY_URL` | CloakBrowser |
| `APIFOOTBALL_ENABLED`, `APIFOOTBALL_LIVE_AUTHORIZED`, `APIFOOTBALL_API_KEY` | API-FOOTBALL (entrambi i flag sono necessari per le chiamate reali) |
| `APIFOOTBALL_MAX_RPM` (8), `APIFOOTBALL_MAX_DAILY_REQUESTS` (80), `APIFOOTBALL_MAX_CONCURRENCY` (2; in produzione 1) | limiti locali del coordinatore di quota, più restrittivi del piano del provider |
| `APIFOOTBALL_CACHE_ENABLED`, `APIFOOTBALL_CIRCUIT_BREAKER_ENABLED` | resilienza |
| `SXBET_ENABLED`, `SXBET_NETWORK` (`mainnet`/`testnet`), `SXBET_API_KEY`, `SXBET_MAX_RPM` (60) | SX Bet, solo lettura; la chiave serve solo per le route autenticate |

## Comportamenti e limiti (verificati)

- **Payload:** risposta MCP massima 256 KiB. Una pagina `/odds` o `/odds/live` più grande restituisce `PARTIAL_ODDS_MANIFEST`: ID delle fixture e dei mercati e conteggi, **senza prezzi**. I prezzi si richiedono con `fixture` (pre-match) oppure `fixture` + `bet` (live). Lettura dal provider limitata a 2 MiB per `/odds` e `/odds/live`, 512 KiB per le altre rotte, sempre in modo chiuso.
- **Paginazione:** `pagination` riporta il `paging` del provider; `page` è disponibile dove il contratto lo prevede, compreso `/odds/mapping` (45 pagine osservate).
- **Quota:** ogni richiesta prenota un lease nel coordinatore PostgreSQL condiviso. Errori possibili: `LOCAL_QUOTA_EXHAUSTED`, `LOCAL_CONCURRENCY_BUSY`, `QUOTA_COORDINATOR_UNAVAILABLE`, quest'ultimo con diagnostica fissa nei log (`stage`, `reason`, `elapsed_ms`). L'inizializzazione dello schema non consuma il budget della richiesta, e un lease orfano viene rilasciato.
- **Dati mancanti:** un campo mancante o null non viene mai convertito in 0. Una selezione con `suspended` mancante è contata come sconosciuta.
- **Limiti del provider osservati:**
  - le quote live non indicano il bookmaker;
  - alcune selezioni live sospese hanno `"odd":"0"`, da trattare come prezzo assente;
  - `/injuries` può restituire voci duplicate;
  - `periods` può essere null anche su partite concluse.
- **Browser:** nessun bypass di login, paywall o CAPTCHA. Gli URL non pubblici (per esempio `127.0.0.1`) vengono rifiutati. Il campo `cdpUrl` non viene mai restituito. Anche il `liveUrl` di Browser Use, che contiene l'host CDP, è nascosto per default (`live_view_available: bool`). Lo si abilita solo impostando `BROWSER_USE_EXPOSE_LIVE_URL=true` (PR #133).

## Stato di certificazione (31-10-2026)

| Area | Esito | Evidenza |
|---|---|---|
| Protocollo MCP e OAuth | PASS (offline e client Claude reale) | `tests/test_mcp_offline_protocol_e2e.py`; connettore Claude.ai |
| API-FOOTBALL via client reale | PARTIAL | statistiche FT, quote di 10 bookmaker, manifest pre-match, live con due osservazioni, `page` 2/45: vedi #130 |
| Parametri ufficiali API-FOOTBALL (38 endpoint, 140 parametri, v3.9.3) | **DOCS_VERIFIED** (01-11-2026) | documentazione ufficiale letta nel browser del bridge; matrice `docs/api_football/official_parameter_matrix_v3.9.3.json`, test `tests/test_apifootball_official_parameter_matrix.py` |
| Manifest `/odds/live` | OFFLINE_VERIFIED | nessun payload live abbastanza grande osservato finora |
| Browser Use e Cloak via MCP | PASS (create, open, read, stop, SSRF) | #130 M2 |
| Client ChatGPT | CLIENT_ACTION_REQUIRED | runbook `docs/api_football/mcp_client_certification_runbook.md` |

## Sviluppo e test

```bash
pip install -r requirements.txt
PYTHONPATH=. pytest -q                     # suite offline
TEST_DATABASE_URL=postgresql://... PYTHONPATH=. pytest -q tests/integration   # PostgreSQL dedicato, mai produzione
```

CI (GitHub Actions): `Multi-market tests` (pytest e decision-postgres) e `api-football-contract-check`. I workflow manuali `api-football-capability-inventory` e `api-football-live-smoke` effettuano chiamate reali con limiti fissi.

## Documentazione

- API-FOOTBALL: [`docs/api_football/`](docs/api_football/) (changelog, runbook, evidenze) e [`docs/audits/`](docs/audits/).
- Browser Use e CloakBrowser: [`docs/browser/README.md`](docs/browser/README.md).
- SX Bet: [`docs/sxbet/README.md`](docs/sxbet/README.md).
- Il codice storico dell'engine di consensus, dei collector e dei pilot è stato rimosso con la PR #132; resta consultabile nella storia Git.
