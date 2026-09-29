# Blob non versionati

I file elencati in `omitted_blobs.jsonl` esistevano nel sandbox al momento dello snapshot e non sono nel git perché sono HTML, screenshot, dump di rete o raw FootInsights/Pinnacle.

Ogni riga ha `path` relativo alla root del repository, `bytes`, `sha256`, `reason`.

Per riattaccarli più tardi, copia il file nel path relativo e verifica:

```bash
sha256sum <path>
```

Il valore deve coincidere con `sha256`. Non riscrivere i manifest già sigillati.

`redactions.jsonl` elenca i file committati da cui sono stati tolti cookie o sessioni. `original_sha256` è l'hash del file nel sandbox. `committed_sha256` è l'hash nel git. I sigilli Forebet (`seal_at_start.json`) conservano gli hash originali, quindi un report redatto non combacia più con quel sigillo: è voluto.

`api_key`, pid e lock non sono né nel git né in questo inventario.
