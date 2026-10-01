# Valutazione dei pronostici mostrati (#35 M24/M39, #42, #43)

Questo è il report dei **nostri ticket**, distinto dall'analisi delle fonti
`analysis.scoring`, che sceglie l'ultimo snapshot pre-match per metodo.
`analysis.decisions` usa esclusivamente `decision_tickets.snapshot_view` per
probabilità, scelta, benchmark e quota. Un JOIN sullo snapshot esattamente
referenziato verifica solo identità, provenienza e tempi. Snapshot/quote
successivi non entrano nel report. Nessun ticket storico viene corretto.

Selezione fissata: primo ticket realmente mostrato `BET`, per partita, nella
strategia e nella finestra dichiarate. Sia richiesta sia kickoff devono essere
nella finestra `[start,end)`. Si seleziona **prima** dei controlli di qualità o
dell'outcome: un primo ticket invalido non viene sostituito. `NO_BET`, pilot,
duplicati di batch/strategie e identità ambigue non diventano campioni indipendenti.
Servono FT home e away entrambi `VERIFIED`, ricostruiti dalle osservazioni note
al cutoff, non dal consensus mutabile corrente. Un risultato 0–0 è valido.

Calibrazione della scelta e delle tre componenti, log loss, Brier e RPS usano
gli stessi valori congelati. La tolleranza di somma `1e-5` ammette l'arrotondamento
a sei decimali della API: **nessuna rinormalizzazione di p_raw, Brier o
calibrazione**. Solo il log score applica il clipping dichiarato seguito da
rinormalizzazione. Lo zero resta zero nel raw. Le esclusioni sono esplicite.

Il confronto con Pinnacle è appaiato sulle stesse partite con fair congelato
e audit della quota originale. Non usa il close successivo. Il rendimento è
virtuale, stake piatto 1, solo alla quota congelata certificata: quote mancanti
non diventano stake/perdita/profitto zero. I costi infrastrutturali non sono
inclusi. Non è una prova di profitto netto dell'intero progetto.

## Protocollo prima del test

`protocol_template.json` è un **template**, con date segnaposto nel 2099:
copiarlo e impostare una finestra futura prima della registrazione. Default
espliciti: 28 giorni, almeno 250 partite e 14 giorni distinti, copertura 90%
rispetto a tutte le prime partite della coorte (incluse invalide/non verificate),
incremento rilevante log loss 0.01 nat rispetto al benchmark congelato, ROI
virtuale 2%, attesa risultati 7 giorni, IC 95%, 2000 repliche, seed 47042.
Le due soglie non modificano le strategie S1–S9.

`s1_20261004_protocol.json` fissa il primo studio futuro S1: **4 ottobre–1
novembre 2026, ore 00:00 Europe/Rome**, 28 giorni di calendario (include il
cambio di ora). Cutoff finale dopo 7 giorni: **8 novembre, ore 00:00 Roma**.
ID operativo previsto `s1-ft1x2-20261004-v1`. Il file da solo non certifica la
registrazione: conta esclusivamente il sigillo pre-window nel database.

Il protocollo completo è salvato in `evaluation_protocols`, con hash e sigillo
**server-owned** prima dell'inizio. UPDATE, DELETE, TRUNCATE, backdating,
registrazione tardiva e riuso dell'id con altri criteri falliscono. Retry con
identico id/policy restituisce la registrazione originale. I vecchi dati non
sono retrocertificati. La migration 0006 è additiva e tracciata.

```bash
python -m analysis.decisions.run --register --protocol-id s1-future-v1 --policy /path/future.json
python -m analysis.decisions.run --neon --protocol-id s1-future-v1 --policy /path/future.json --known-at 2026-11-08T00:00:00Z
python -m analysis.decisions.run --input /path/diagnostic.json --policy /path/history.json --known-at 2026-10-02T00:00:00Z
```

File input e report non preregistrati restano diagnostici: non possono emettere
un verdetto di vantaggio. Il verdetto primario usa il delta di log loss;
Brier, RPS e calibrazione sono secondari, non test alternativi per scegliere
il risultato più favorevole. ROI e qualità hanno denominatori e verdetti
distinti. Nessuna promozione automatica, nessuna nuova fonte attivata.

## Verdetti e stop

Prima della fine della finestra **e** della grace, senza registrazione valida,
campione, giorni o copertura sufficienti: `DATI_INSUFFICIENTI`, con motivi.
Al cutoff terminale i risultati successivi non cambiano l'esperimento; niente
allungamento opportunistico della finestra o abbassamento delle soglie.

Con gate soddisfatti:
- qualità: IC interamente a delta <= -0.01 → `VANTAGGIO_DIMOSTRATO_NEL_CAMPIONE`;
  IC interamente > 0 → `PEGGIORE_DEL_BENCHMARK`;
  altrimenti `VANTAGGIO_NON_DIMOSTRATO`;
- rendimento: limite inferiore IC >= 0.02 → vantaggio nel campione;
  limite superiore < 0 → `RENDIMENTO_NEGATIVO`; altrimenti vantaggio non dimostrato.

In assenza di supporto, l'azione è fermare il claim di vantaggio, senza ritoccare
strategie o prolungare il test. Il blocco è sul claim/decisione di promozione,
non uno spegnimento automatico dei collector. Un nuovo studio richiede nuovo
id, nuovi criteri congelati e nuovo campione futuro.

IC bootstrap a blocchi dei **giorni di kickoff Europe/Rome**, appaiati, con
media pesata per partita. Riduce il problema della dipendenza nello stesso
giorno; non elimina dipendenza fra giorni/leghe o bias di selezione dovuto alla
copertura. 250 non garantisce potenza statistica: l'IC può restare largo e
produrre `VANTAGGIO_NON_DIMOSTRATO`. I dati esclusi possono essere selettivi:
il risultato riguarda soltanto questa coorte, non tutte le partite.

M39 completo (multi-market/source, closing reference, costi, UI consolidata)
resta oltre questo intervento. Questo percorso rende verificabile lo scoring
dei ticket e il contratto prospettico, senza dichiarare chiusa la master.
