# football-consensus-engine

Ledger prospettico append-only di probabilità 1X2 pre-match. FootInsights è il modello A, Pinnacle il mercato, FIFA il settlement. Forebet e Football Charts sono pilot di staging separati: non scrivono il ledger.

Questo repository è la copia dello stato al 2026-09-29. Non avvia processi. Il sandbox non è il runtime.

## Layout

```
prospective/     collector FootInsights + Pinnacle, supervisor, settlement FIFA, ledger
staging/forebet/ parser Forebet, pilot 48h, snapshot già chiusi
staging/football_charts/  pilot Football Charts
staging/browser_use_shortlist/  probe CDP degli 8 siti e analisi offline
staging/forebet_cdp/  primo probe CDP Forebet
backtest/        baseline storica e falsificazione (non è il ledger prospettico)
storage/         hash dei raw non versionati e registro delle redazioni
```

## Stato Forebet da non ricreare

| Campo | Valore |
|---|---|
| pilot_id | `forebet-48h-20260929T153629Z` |
| deadline | 2026-10-01T15:36:29Z |
| slot pianificati | 16, uno ogni 3 ore |
| slot completati | 1/16 |
| slot 1 | SUCCESS, run canonico `20260929T153629Z-s01a1` |
| retry | 1, run `20260929T153847Z-s01a1` (tunnel). Non è uno slot nuovo |
| classificazione | IN_PROGRESS. Non promuoverlo |

`staging/forebet/pilot48/STARTED.json` esiste già. Un nuovo avvio del loop non deve rigenerarlo e non deve trasformare il retry in uno snapshot pianificato. Il rapporto finale è `slot riusciti / 16`.

I file `page.html`, `page.png` e i body di rete non sono nel git. Hash e path sono in [storage/omitted_blobs.jsonl](storage/omitted_blobs.jsonl). I fixture parsati, i manifest e `manifest.sha256` dello slot 1 ci sono.

Quattro `report.json` di validazione avevano `cookie_sample`. Nel git il campo è `REDACTED_NOT_COMMITTED`. L'hash originale resta in `seal_at_start.json` e in [storage/redactions.jsonl](storage/redactions.jsonl).

## Avvio da un clone pulito

Python 3.10+. Il collector e il settlement usano solo la libreria standard. Playwright serve solo ai pilot che aprono un browser remoto via CDP. Non serve `playwright install`: il browser è quello di Browser Use.

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
# mettere il valore vero solo in .env oppure in staging/forebet/pilot48/var/api_key (gitignored)
```

Collector (scrive ledger, snapshot, raw e manifest; non sovrascrive file esistenti):

```bash
python3 prospective/collect.py
python3 prospective/bin/supervisor.py --once
python3 prospective/bin/supervisor.py --self-test
python3 prospective/bin/supervisor.py
```

`supervisor.py` senza argomenti resta in loop ogni 3 ore, con lock esclusivo, retry a 60/180/300 secondi e alert di stallo dopo 6 ore. `--ensure` avvia il processo solo se non è già in esecuzione.

Settlement FIFA (scrive solo `prospective/normalized/results.csv` e i report di settlement; non tocca ledger, raw, manifest, strategie):

```bash
python3 prospective/settle.py --self-test
python3 prospective/settle.py
python3 prospective/bin/settle_loop.py
```

Il loop di settlement è ogni 30 minuti.

Report di qualità dell'ultimo snapshot:

```bash
python3 prospective/report.py
```

Forebet (legge `BROWSER_USE_API_KEY` dall'ambiente, altrimenti dal file gitignored):

```bash
python3 staging/forebet/pilot_loop.py
```

`staging/forebet/extract.py` fa due run di validazione e non incrementa il contatore dei 16 slot. Non usarlo per continuare il pilot.

Football Charts (HTTP proprio, non Browser Use; non scrive il ledger):

```bash
python3 staging/football_charts/pilot.py --self-test
python3 staging/football_charts/pilot_loop.py
```

Shortlist, solo analisi dei file già salvati:

```bash
python3 staging/browser_use_shortlist/analyze.py
```

Backtest storico, separato dal ledger prospettico:

```bash
python3 backtest/run_backtest.py
python3 backtest/run_falsify.py
```

## Architettura

Il collector legge le pagine pubbliche FootInsights e il guest feed Pinnacle (`guest.api.arcadia.pinnacle.com`, sport 29, moneyline full-match). Non usa una API key Pinnacle. Ogni run crea una directory nuova sotto `prospective/raw/` e `prospective/snapshots/`, accoda una riga a `prospective/manifests/manifest_index.jsonl` e scrive `manifest_<run>.sha256`. Il ledger `prospective/prospective_ledger.csv` è append-only. Hash del ledger in questo snapshot: `5d5163bd6b2b09e5c32ece63170b27e301b4bc4901a15c199e295f8e787a5464`.

Il settlement interroga `https://api.fifa.com/api/v3/calendar/matches` almeno 120 minuti dopo il kickoff. Uno score identico non viene riappeso. Uno score ufficiale cambiato è una versione nuova più una riga di audit. Le colonne S1–S9 e B1–B4 sono calcolate qui, non nel collector.

Le strategie sono congelate in `prospective/strategies_preregistered.json` (`prereg-2026-09-29`), prima di qualsiasi risultato prospettico.

| Id | Regola |
|---|---|
| S1 | bet sul pick del modello se P >= 0.50 |
| S2 | come S1 con P >= 0.60 |
| S3 | S1 e il pick è il favorito Pinnacle |
| S4 | S2 e il pick è il favorito Pinnacle |
| S5 | S1 e edge > 0 |
| S6 | S2 e edge > 0 |
| S7 | edge >= 0.03 |
| S8 | edge >= 0.05 |
| S9 | edge >= 0.08 |
| B1 | sempre il favorito Pinnacle |
| B2 | favorito Pinnacle sulle partite di S1 |
| B3 | favorito Pinnacle sulle partite di S2 |
| B4 | nessun bet, ROI 0 |

Edge = probabilità del modello sul pick meno la fair probability Pinnacle sullo stesso esito. Stake flat 1. Niente Kelly. Le quote sono Pinnacle, non le quote mostrate sulla pagina FootInsights.

Browser Use è solo infrastruttura: `POST /api/v4/browsers`, `cdpUrl`, Playwright `connect_over_cdp`, poi stop della sessione. Nessun agent interpreta la pagina. Il valore di `cdpUrl` non viene salvato.

I path nel codice sono relativi alla root del repository. I JSON storici (`slots.jsonl`, `seal_at_start.json`) conservano i path assoluti `/workspace/...` scritti nel sandbox. Non vanno riscritti.

## Schema CSV

Ledger, una riga per snapshot di una partita. Il risultato resta vuoto finché non lo scrive un processo esterno; il settlement non aggiorna questo file.

`prospective/prospective_ledger.csv`

`match_id,source_match_id,league,country,season,kickoff_utc,home_team,away_team,home_canon,away_canon,team_flags,snapshot_timestamp_utc,minutes_before_kickoff,model_version,prediction_published_date,scraper_version,strategy_set_id,p_home_raw,p_draw_raw,p_away_raw,p_home_normalized,p_draw_normalized,p_away_normalized,prob_parse_source,model_pick,model_pick_probability,source_url,sha256_model_raw,page_book,page_odds_home,page_odds_draw,page_odds_away,valid_prematch,analysis_eligible,pin_matchup_id,pin_league,pin_home,pin_away,pin_snapshot_timestamp_utc,pin_kickoff_utc,odds_home,odds_draw,odds_away,american_home,american_draw,american_away,fair_home,fair_draw,fair_away,overround,pin_favorite,pin_favorite_probability,pin_favorite_odds,edge_home,edge_draw,edge_away,picked_odds,picked_pin_fair,picked_edge,same_as_favorite,market_version,sha256_odds_raw,odds_raw_file,name_score_note,final_home_goals,final_away_goals,result_1x2,match_status,result_timestamp_utc,result_source,correct,profit_flat_1`

`prospective/normalized/results.csv` è l'unico file di risultati. Colonne S1–S9 e B1–B3 ripetute come `*_active,*_correct,*_profit`. B4 non ha colonne perché è il non-bet.

`prospective/snapshots/<day>/<run>/invalid_prematch.csv`

`match_id,source_url,reason,kickoff_utc,home_team,away_team,snapshot_timestamp_utc,sha256_model_raw`

Forebet `fixtures.csv`:

`run_id,snapshot_timestamp_utc,league,home,away,kickoff_original,kickoff_utc,timezone_assumed,timezone_confidence,p1,px,p2,main_pick,correct_score,avg_goals,odds_1,odds_x,odds_2,ou,btts,row_state,match_id,source_url,row_hash,parse_status`

## Cosa non è in questo repository

- API key, cookie, `cdpUrl`, token, connection string
- HTML FootInsights, JSON Pinnacle, screenshot e body XHR (solo hash)
- servizi Render
- database Neon

Per rimettere un raw al suo posto, vedi [storage/README.md](storage/README.md).
