from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_governance_files_exist_and_are_aligned():
    agents = _read("AGENTS.md")
    claude = _read("CLAUDE.md")
    assert agents.startswith("# AGENTS.md")
    assert claude.startswith("# CLAUDE.md")
    assert agents.replace("# AGENTS.md", "# GOVERNANCE", 1) == claude.replace("# CLAUDE.md", "# GOVERNANCE", 1)


def test_governance_contains_non_negotiable_contracts():
    text = _read("AGENTS.md")
    required = [
        # access, secrets, SSRF, CDP
        "Sicurezza e accesso",
        "OAuth",
        "SSRF",
        "Nessun `cdpUrl` restituito, loggato o persistito",
        "BROWSER_USE_EXPOSE_LIVE_URL",
        "Segreti",
        "GitHub Actions secret **non** arriva al runtime Render",
        # data integrity
        "Missing resta missing",
        "Nessuna troncatura silenziosa",
        "TIMEZONE_UNRESOLVED",
        "DOCS_UNVERIFIED",
        "Una quota di mercato non è una probabilità di modello",
        # quotas / browser isolation
        "Quote e rate limit",
        "Nessun bypass di quote, rate limit, concorrenza o circuit breaker",
        "Sessioni browser",
        # financial safety
        "Operazioni finanziarie",
        "`LIVE_TRADING` è **disabilitato per default**",
        "**ogni ordine con fondi reali** richiede una conferma specifica dell'owner",
        "**non piazzano mai** ordini reali",
        "Il modello non vede mai la chiave",
        "**non** significa che l'ordine non sia stato eseguito",
        "**append-only**",
        "PKCE S256",
        "Un mandato generale non autorizza ordini reali",
        "Lo abilita solo un flag lato server impostato **dall'owner**",
        "Nessun argomento di un tool MCP può attivarlo",
        "Va verificata **dal signer**",
        "approval o allowance di token",
        "`SIMULATION` non usa mai la chiave di firma live",
        "sono fissati **dall'owner** e l'agente non li alza mai",
        "codice di trading o firma, default delle modalità, limiti di esposizione e flusso di conferma",
        "**Nessun ordine reale** su un evento o mercato il cui abbinamento non sia esatto e verificato",
        "Ambiguità = nessun match",
        "fuori dal layer provider/source originale",
        "Un raw o un hash storico non si sostituisce per far tornare un test",
        "Il raw/source time si conserva sempre",
        "Le issue vengono chiuse **esclusivamente dall'owner**",
        "Nessun test rimosso o annacquato",
        "i review thread sono risolti",
        "nessun errore è stato ignorato",
        "Ogni migration ha un test su un DB già inizializzato",
        "nessun dato storico riscritto",
        # process
        "Sequenza obbligatoria per ogni PR",
        "Non disabilitare branch protection",
        "Deploy e rollback",
        "Non iniziare il merge successivo finché il deploy precedente non è sano",
        "Database e migrations",
        "Issue e commenti",
        "READY_FOR_OWNER_REVIEW / PUÒ ESSERE CHIUSA DALL'OWNER",
        "Definition of Done universale",
        "il merge richiede l'approvazione esplicita dell'owner",
    ]
    for phrase in required:
        assert phrase in text, f"missing governance contract: {phrase}"


def test_governance_has_no_legacy_operational_contracts():
    text = _read("AGENTS.md")
    for legacy in ("Contratto pilot 48h", "Decision ticket contract", "Settlement contract",
                   "Contratto promotion → production", "Issue #37 è il backlog"):
        assert legacy not in text, f"legacy contract still present: {legacy}"


def test_readme_points_agents_to_governance():
    readme = _read("README.md")
    assert "[AGENTS.md](AGENTS.md)" in readme
    assert "[CLAUDE.md](CLAUDE.md)" in readme
    for term in ("TIMEZONE_UNRESOLVED", "cdpUrl", "LIVE_TRADING", "DOCS_UNVERIFIED"):
        assert term in readme, term
