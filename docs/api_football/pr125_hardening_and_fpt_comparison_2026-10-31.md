# PR #125 — hardening, stato CI e confronto FPT (31 ottobre 2026)

Matrice machine-readable: `pr125_hardening_and_fpt_comparison_2026-10-31.json`.
Distinzione VERIFIED / ASSUMED come da CLAUDE.md §13. **NO MERGE / NO DEPLOY.**

## A. GitHub Actions — `EXTERNAL BILLING BLOCKER` alle 14:51Z, poi sbloccato e verde

| Workflow | Run | Attempt | Job (id) | Esito |
|---|---|---|---|---|
| Multi-market tests | 40228317477 | 2 | pytest (124569018095), decision-postgres (124569017941) | non avviato |
| api-football-contract-check | 40228317491 | 2 | contracts (124569006796) | non avviato |

Annotazione su tutti i job: *"The job was not started because recent account payments have failed or your spending limit needs to be increased."* `runner_id = 0`, zero step eseguiti, durata 2 s (2026-10-31T14:51:40–48Z).
Classificazione: **blocco GitHub Billing / runner non assegnato**. Non è un fallimento dei test, non è un errore del codice né della configurazione CI. Nessun nuovo tentativo, nessuna modifica di billing.

**Aggiornamento dopo lo sblocco da parte dell'owner (31-10, ~20:50Z) — VERIFIED:** head PR `157b435` verde.
`api-football-contract-check` run 41240887064 ✅; `Multi-market tests` run 41240886931 (pytest ✅, decision-postgres ✅); dispatch sullo stesso SHA run 41240715094: pytest 1101 passed / 102 skipped, decision-postgres 101 passed. CodeRabbit: skipped (draft).

## B. Review del diff della PR e correzioni

Difetti riprodotti sul codice della PR (head `3ddcda7`):

1. Un bookmaker senza `bets`, o un bet senza `values`, sollevava `KeyError`: il controllo `book.get("bets", [])` passava, poi `book["bets"]` falliva. L'eccezione non era `ValueError`, quindi arrivava al gestore generico: `PROVIDER_ERROR / PROVIDER_REQUEST_FAILED`, causa nascosta.
2. Un campo `get` non stringa sollevava `AttributeError`, con lo stesso esito generico.
3. Un `bookmakers` mancante veniva contato come 0. Questo viola §2.4: *missing resta missing*.
4. Il percorso manifest perdeva `quota_remaining` e `http_status`, proprio mentre chiede di rispettare la quota. Perdeva anche la provenienza mock/reale.
5. La dimensione del manifest non veniva ricontrollata rispetto al tetto MCP.

Correzioni applicate:
- Le liste mancanti o malformate e un `get` non valido falliscono in modo esplicito. Il risultato resta `PAYLOAD_TOO_LARGE` con `manifest_error.code = INVALID_ODDS_*`.
- Il manifest ora conserva `source_status`, `quota_remaining` e `http_status`, e la sua dimensione viene verificata entro i 256 KiB.
- Nuovi test: 8 a livello tool e transport. Di questi, 6 falliscono sul codice originale della PR e passano con la correzione. Il cap a 2 MiB vale solo per `/odds`; `/odds/live`, `/fixtures` e le altre rotte restano a 512 KiB.
- Il test è stato aggiunto al workflow `api-football-contract-check`.

Verificato, nessuna modifica necessaria:
- Redirect proibiti e host pinning invariati.
- 429, timeout, 4xx e 5xx: invariati, con codici sanitizzati.
- Il manifest non contiene prezzi.
- Nessun componente Browser Use toccato.

Limiti noti, non risolti in questa PR (scope):
- `/odds/live` oltre 256 KiB restituisce ancora `PAYLOAD_TOO_LARGE`, senza manifest.
- La paginazione dei bookmaker resta quella del provider, una pagina per chiamata.
- La matrice dei parametri resta `INCOMPLETE`.

## C. Certificazione reale API-FOOTBALL — `PARTIAL` (reale, a campione)

Workflow manuale `api-football-capability-inventory`, lanciato con l'approvazione dell'owner: run **41240887865**, SHA `157b435`, 20:52–20:56Z. Cap applicati: 60 richieste al giorno, 8 al minuto. Nessun secret compare negli artefatti (verificato con grep).
Evidenze: `evidence/api_football/capability_run_41240887865/`, con i file piccoli e lo SHA-256 dell'artefatto completo, conservato 7 giorni.

| Campo | Stato | Evidenza |
|---|---|---|
| ID fixture, data, timezone | VERIFIED_REAL | `fixture.id/date/timezone` su `/fixtures` e `/odds` |
| Risultato HT | VERIFIED_REAL | `score.halftime.*` int |
| Risultato FT | PARTIAL | `goals.home` int; `score.fulltime` osservato solo null |
| Statistiche squadra | VERIFIED_REAL (forma) | `/teams/statistics` 3 campioni, `/fixtures/statistics` 3 fixture |
| Statistiche giocatori | VERIFIED_REAL (forma) | `/fixtures/players`, `/players` |
| xG | PARTIAL | run 41242779991: `expected_goals` valido su 4/4 righe squadra (es. `0.1`), **solo in-play 1H** |
| Tiri totali e nello specchio | PARTIAL | `Total Shots` e `Shots on Goal` int su 4/4 righe, solo in-play |
| Possesso | PARTIAL | `NN%` su 4/4 righe, coppie a somma 100 (57/43, 42/58), solo in-play |
| Corner | UNAVAILABLE (nel campione) | etichetta `Corner Kicks` MISSING su 4/4 righe in-play; non trasformata in 0; sulle istantanee finali NOT_TESTED |
| Cartellini | PARTIAL | `cards.yellow/red` per giocatore; etichette squadra `Yellow Cards`/`Red Cards` presenti, valori non validati |
| 1X2 | VERIFIED_REAL | Match Winner: 90 valori, 10 bookmaker, 3 fixture |
| Over/Under | VERIFIED_REAL | 486 valori, 8 bookmaker |
| Handicap | VERIFIED_REAL | Asian Handicap: 418 valori, 8 bookmaker |
| Bookmaker diversi | PARTIAL | catalogo con 32 voci, ma `results` dichiara 33 (discrepanza aperta); prezzi osservati per 10 su 32 |
| Mercati live | PARTIAL | 1 fixture, 1 bookmaker, 382 valori |
| Sospensioni e aggiornamenti live | PARTIAL | 17 sospese; due snapshot identici (stesso SHA), nessuna variazione osservata |
| Timestamp osservazioni | VERIFIED_REAL | `updated` su tutti i valori pre-match |
| `main`/`suspended` pre-match | UNAVAILABLE | MISSING su 10066 valori su 10066, mantenuto missing |
| Quote storiche pre-match | NOT_TESTED | nessuna query su fixture passate |

**Estensione del probe (autorizzata dall'owner):** i valori delle statistiche vengono estratti dalla risposta `/fixtures/statistics` già scaricata, quindi **nessuna chiamata in più**. Le etichette sono confrontate in modo esatto, null e missing restano distinti da 0, e ogni osservazione è marcata FINAL/IN_PLAY. Run 41242779991 (SHA `c4bdaaa`): 3 partite, tutte in 1H, una con risposta vuota. Evidenze in `evidence/api_football/capability_run_41242779991/`.

Le 39 rotte note su 39 rispondono HTTP 200: è una prova di raggiungibilità, non una certificazione dei parametri.

## D/E. Confronto FutPythonTrader — `BLOCKED_SOURCE_ACCESS`

La fonte è `matchpilot_staging` (PostgreSQL 18, porta 5434, VPS MatchPilot), con 161.801 righe in `fpt_match_facts` secondo MatchPilot #12. Questa sessione non ha accesso alla VPS né al database, e i backup cifrati non sono stati toccati. Il repository MatchPilot contiene solo seed sintetici, esclusi dal mandato.

Nessuna partita è stata campionata e nessuna metrica è stata calcolata: tutte le metriche valgono `null`, mai 0.
Ricontrollato il 31-10 dopo l'autorizzazione dell'owner: nessuna chiave SSH, nessuna credenziale database, nessuna cartella Remote Control disponibile; i backup su Drive sono cifrati (age) e non sono stati toccati. Per decisione dell'owner resta un'**attività separata**, fuori dalla PR #125.
Accesso minimo necessario: export read-only eseguito dall'owner di 30 righe stratificate, con i campi elencati nel JSON (`fpt_comparison.minimum_access_needed`).

## Verdetto: `INCONCLUSIVE`

La certificazione del provider è `PARTIAL`: reale ma a campione. Il confronto FPT resta `BLOCKED_SOURCE_ACCESS`, quindi non si può derivare un verdetto di sostituzione.
