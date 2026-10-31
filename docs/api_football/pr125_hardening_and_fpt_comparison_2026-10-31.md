# PR #125 — hardening, stato CI e confronto FPT (31 ottobre 2026)

Matrice machine-readable: `pr125_hardening_and_fpt_comparison_2026-10-31.json`.
Distinzione VERIFIED / ASSUMED come da CLAUDE.md §13. **NO MERGE / NO DEPLOY.**

## A. GitHub Actions — `EXTERNAL BILLING BLOCKER` (VERIFIED)

| Workflow | Run | Attempt | Job (id) | Esito |
|---|---|---|---|---|
| Multi-market tests | 40228317477 | 2 | pytest (124569018095), decision-postgres (124569017941) | non avviato |
| api-football-contract-check | 40228317491 | 2 | contracts (124569006796) | non avviato |

Annotazione su tutti i job: *"The job was not started because recent account payments have failed or your spending limit needs to be increased."* `runner_id = 0`, zero step eseguiti, durata 2 s (2026-10-31T14:51:40–48Z).
Classificazione: **blocco GitHub Billing / runner non assegnato**. Non è un fallimento dei test, non è un errore del codice né della configurazione CI. Nessun nuovo tentativo, nessuna modifica di billing.

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

## C. Certificazione reale API-FOOTBALL — `BLOCKED_SOURCE_ACCESS`

In questo container non c'è una chiave provider: `/status` senza chiave risponde HTTP 403 "Missing application key". Il MCP live su Render è protetto da OAuth e non è collegato a questa sessione. Leggere la chiave da Render o eseguire job lì è fuori mandato.

Per questo i campi del JSON sono marcati `PARTIAL` solo dove esiste un'osservazione reale del 25-10 (owner session), e `NOT_TESTED` altrove. Nessun campo è `VERIFIED_REAL` in questa sessione.
Accesso minimo necessario: eseguire manualmente `api-football-capability-inventory` dopo il ripristino del billing, oppure fare le verifiche dal connettore MCP claude.ai.

## D/E. Confronto FutPythonTrader — `BLOCKED_SOURCE_ACCESS`

La fonte è `matchpilot_staging` (PostgreSQL 18, porta 5434, VPS MatchPilot), con 161.801 righe in `fpt_match_facts` secondo MatchPilot #12. Questa sessione non ha accesso alla VPS né al database, e i backup cifrati non sono stati toccati. Il repository MatchPilot contiene solo seed sintetici, esclusi dal mandato.

Nessuna partita è stata campionata e nessuna metrica è stata calcolata: tutte le metriche valgono `null`, mai 0.
Accesso minimo necessario: export read-only eseguito dall'owner di 30 righe stratificate, con i campi elencati nel JSON (`fpt_comparison.minimum_access_needed`).

## Verdetto: `INCONCLUSIVE`

Mancano sia l'evidenza reale API-FOOTBALL sia il campione FPT. Il verdetto non deriva da documentazione o mock.
