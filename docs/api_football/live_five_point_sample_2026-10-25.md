# API-FOOTBALL — collaudo reale mirato del 25 ottobre 2026

**Provenienza:** strumenti `apifootball_*` del server MCP Render (deploy main `cae48c4b`); dati provider reali, cache MISS per le chiamate indicate. Questo report riassume esclusivamente risposte osservate, non il workflow manuale di scansione completa della PR #123. Dati delle partite LIVE variabili. Non vengono conservate credenziali, nomi di persone fisiche o payload integrali.

## Coordinatore PostgreSQL / Render
- Il database Neon `football-consensus-engine` (project `sweet-haze-36367333`) è raggiungibile tramite connettore Neon e contiene `api_football_quota_counters`, `api_football_quota_leases`, `api_football_quota_attempts`.
- Lo snapshot SQL in sola lettura sui bucket UTC correnti ha restituito **21** richieste prenotate nel bucket giornaliero locale del coordinatore. Tale valore è distinto dal contatore provider; non indica da solo l'uso complessivo della API key.
- Dal MCP sono riuscite chiamate ripetute a `/odds/bookmakers`, `/odds/bets`, `/odds/live/bets`, `/fixtures`, `/odds/live`, `/odds`, `/fixtures/statistics`, `/teams/statistics` e `/players`, con HTTP 200 e dati reali.
- L'errore precedente `QUOTA_COORDINATOR_UNAVAILABLE` non si è riprodotto in questa sessione. **Causa originaria NON determinata; NON asserire riparazione definitiva.** La PR #123 aggiunge stato `quota_coordinator` a health per diagnosticare eventuali ricadute; non modifica i limiti e non bypassa il coordinatore.

## Le cinque matrici: risultati osservati
| Requisito | Risultato reale del campione | Verdetto |
|---|---|---|
| Bookmaker | `/odds/bookmakers`: HTTP 200, **33** voci, paging **1/1**. `/odds?fixture=1557450`: **9** operatori con mercati e quote; `fixture=1557448`: **10**. Betfair ID 3 e Pinnacle ID 4 con prezzi in entrambi i campioni. | **PASS_SCOPE** (catalogo completo per la risposta, disponibilità non certificata per tutti i 33 operatori) |
| Mercati | `/odds/bets`: **338** ID, paging **1/1**. `/odds/live/bets`: **266** ID, paging **1/1**. Prematch e LIVE separati. `/odds/live?fixture=1548550`: almeno **52** mercati e **291** selezioni osservate; `odds[]` è la chiave effettiva LIVE (bug del parser corretto in PR #123). | **PASS_SCOPE**, non ogni mercato del catalogo disponibile per ogni fixture |
| Statistiche | `/fixtures/statistics` su fixture **1548550** e **1549040**: due squadre per partita, statistiche eterogenee, esempi `expected_goals` e valori percentuali stringa. `/players` su team **616** e **406**, stagione 2026: 20 giocatori a pagina, paging **1/2**. `/teams/statistics` team 616, lega 210 stagione 2026 ha risposto. Una richiesta a fixture futura 1557450 ha restituito 0 risultati: esito vuoto legittimo. | **PASS_SCOPE** su due fixture e due squadre; **PARTIAL** su totale delle pagine e varianti stagione/lega |
| LIVE | Stessa fixture 1548550, update provider **2026-10-25T17:27:29Z** e **2026-10-25T17:28:25Z**; 291 selezioni, sospese osservate rispettivamente **16** e **72**. Un'ulteriore coppia di chiamate con identico `update` 17:28:25Z ha correttamente rilevato **0** modifiche. | **PASS_SCOPE** per osservazione di variazione aggregate delle sospensioni; **PARTIAL** per attribuzione selezione-per-selezione tra snapshot non conservati |
| Copertura | Archivi autentici verificati SHA256 dai run **40205480029** e **40211464376**: `/odds` HTTP 200 con 0 risultati nello storico; `/odds/live` bloccato per mancanza di fixture LIVE osservata. Entrambi gli endpoint hanno risposto HTTP 200 con dati su fixture mirate nell'odierno collaudo. | **PASS_SCOPE** sulla spiegazione dei due esiti, non una garanzia universale sulle 39 rotte |

## Contatori e regole

La quota lato provider rilevata dal MCP durante il campione è nell'ordine di **7.300** richieste disponibili oggi. Il server applica autonomamente un budget locale più restrittivo (**80/giorno, 8/minuto, concorrenza 1**) attraverso il database PostgreSQL; non va aumentato senza decisione esplicita e prova di necessità.

Il test con `/odds?league=39&season=2026&page=1` ha prodotto `PROVIDER_RESPONSE_TOO_LARGE`; ripetuto con filtro `fixture` è riuscito. Questo è un limite di dimensione del payload MCP, non indisponibilità delle quote.

### Condizioni residue di certificazione globale
- Eseguire manualmente il workflow `api-football-capability-inventory` sul branch/head esatto PR #123, con secret GitHub nell'environment autorizzato; il workflow esegue scansione max **60** chiamate e 8 rpm. Non è stato lanciato da questa sessione, perché l'integrazione GitHub disponibile non espone workflow_dispatch.
- Recuperare `five_point_verdict.json`, `bookmaker_availability.json`, `statistics_samples.json`, `live_delta.json` dai log `AF_EVIDENCE_V1` o dall'artifact (se disponibile).
- Nessun LIVE di una singola ora dimostra il comportamento di tutti i bookmaker in tutte le fixture o le sospensioni possibili. Mantenere gli esiti `PARTIAL/BLOCKED` dove appropriato.
- Revisioni indipendenti e CI verde su HEAD esatto prima del merge.
