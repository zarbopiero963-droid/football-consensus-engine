# API-FOOTBALL — Verifica addizionale 25 ottobre 2026

- Documentazione ufficiale consultata: https://www.api-football.com/documentation_v3, versione 3.9.3.
- Differenza documentale concreta trovata: `/fixtures/statistics` supporta il parametro opzionale booleano `half` (disponibile per dati dalla stagione 2024); mancava nel contratto MCP. Aggiunto in `bridge/api_football/contracts.py` e `bridge/api_football/tools.py`, più test di validazione in `tests/test_api_football_half_contract.py`.
- Il confronto completo di **tutti** i parametri di **tutti** i 39 endpoint con le sezioni ufficiali non è ancora esaustivo: l'audit con stato `INCOMPLETE` resta obbligatorio; non alterare i risultati in PASS senza prove.
- Chiamate REAL del server MCP Render (codice main precedente alla PR): `/odds/bookmakers` HTTP 200, risultati 33, pagine 1/1; `/odds/bets` HTTP 200, risultati 338, pagine 1/1; `/odds/live/bets` HTTP 200, risultati 266, pagine 1/1; `/fixtures?live=all` HTTP 200, risultati 104, pagine 1/1.
- `/odds/live?fixture=1548550` due richieste reali: **48** mercati e **248** selezioni in entrambi gli snapshot, provider timestamp 2026-10-25T17:44:54Z in entrambi, **221** selezioni sospese nel secondo, **0** variazioni osservate tra le due letture con lo stesso update. Nessuna falsa dichiarazione di cambi LIVE durante questa coppia.
- Budget provider rilevato durante il campione: ~7290 richieste residue; restano vincolanti il coordinatore locale, il limite 80/giorno e il cap manuale dello scanner.
- Scanner GitHub manuale di `api-football-capability-inventory.yml` **NON eseguito su branch #123 in questa sessione**: gli strumenti GitHub collegati consentono lettura e PR, ma non espongono `workflow_dispatch`. I test MCP mirati qui sopra non sostituiscono quella scansione e non attestano 100% dei parametri o degli endpoint.
- CI delle modifiche al contratto e ai test in esecuzione separata; attesa prova finale per HEAD esatto e revisione indipendente.

**Verdetto prudente:** connessione reale funzionante e cataloghi completi nei rispettivi snapshot 1/1. Certificazione complessiva `INCOMPLETE`, non 100%. Nessun merge autorizzato.
