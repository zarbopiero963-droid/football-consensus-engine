## Phase 0
- [ ] Ho letto AGENTS.md e CLAUDE.md.
- [ ] Ho letto la issue sorgente e i commenti più recenti.
- [ ] Ho verificato il codice corrente di main.
- [ ] Ho identificato invarianti, rischi e dipendenze.
- [ ] Ho riprodotto il finding/failure reale quando applicabile.

## Scope
Issue:
PR objective:
Files/components touched:

## Invariants
- [ ] Nessun dato storico/prematch/pilot riscritto.
- [ ] Nessun leakage live/FT → pre-match.
- [ ] Missing non trasformato in zero.
- [ ] Probability non ricostruite nel source layer.
- [ ] Quote/market separati dal model layer.
- [ ] Timezone non indovinata.
- [ ] FOUND_NAME_ONLY non usato come production-safe.
- [ ] Pilot/staging non entra in produzione senza promotion esplicita.

## Tests
- [ ] Test regressione.
- [ ] Test negativo/edge case.
- [ ] CI verde.
- [ ] Review findings risolti.

## Test reale
- [ ] Render verificato, se applicabile.
- [ ] Neon verificato, se applicabile.
- [ ] Browser/source reale verificato, se applicabile.
- [ ] Evidenza del test reale annotata nella issue.

## Deployment / persistence
- [ ] Deploy verificato.
- [ ] Nessuna migration implicita su DB vivo.
- [ ] Nessun secret/log sensibile esposto.

## Closure
- [ ] Issue aggiornata con PR/commit/test/deploy.
- [ ] Non chiudere automaticamente la issue.
- [ ] Se completa: annotare READY_FOR_OWNER_REVIEW / PUÒ ESSERE CHIUSA DALL'OWNER.
