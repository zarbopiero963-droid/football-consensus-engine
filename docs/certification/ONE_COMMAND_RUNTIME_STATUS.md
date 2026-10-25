# Esecuzione globale con un solo workflow

Il workflow `fcb-global-e2e-one-command.yml` costituisce il **punto di ingresso unico**. Richiede `deployed_sha` e `authorize_live_probe=true` per utilizzare la chiave API dell'ambiente protetto `api-football-live`.

## Controlli eseguiti
1. Esegue campioni HTTP **reali** degli endpoint API-FOOTBALL per cui il progetto possiede esempi ufficialmente compatibili.
2. Registra percorso, parametri validati, stato HTTP, cardinalità e impronta SHA-256 delle risposte; non registra la chiave.
3. Esegue test di validazione della copertura dei 76 strumenti, delle prove e delle regole fail-closed.
4. Esegue il verificatore globale. **L'assenza di qualsiasi prova obbligatoria produce NO_GO e il workflow fallisce**, indipendentemente dall'esito positivo dei campioni.

## Limitazioni attualmente bloccanti
- Non esiste ancora un'esecuzione autenticata `tools/call` su ciascuno dei 76 metodi dal workflow: le letture HTTP non sostituiscono questo collaudo.
- I 34 Browser Use richiedono scenari con sessione, URL consentiti e cleanup, senza operazioni esterne indesiderate.
- Alcuni parametri richiedono fixture, squadre, giocatori e provider/league ID scavati dinamicamente; gli esempi statici non bastano.
- Il registro documentale ufficiale dei parametri è incompleto. Una risposta HTTP 200 non dimostra la correttezza di tutti i parametri.
- L'ultima approvazione indipendente e il collaudo ChatGPT OAuth sul commit rilasciato non sono ancora attestati.

## Criterio per il prossimo incremento
Un esecutore `tools/list` / `tools/call` autenticato dovrà verificare ogni tool con scenari validi e invalidi, testare autorizzazione e concorrenza, allegare un rapporto separato per ciascun metodo, e marcare `BLOCKED` i metodi senza input sicuri o prerequisiti. La certificazione può risultare `CERTIFIED` solo dopo ogni prova e revisione indipendente.

**Non eseguire merge della PR #124 basandosi soltanto su questo workflow.**
