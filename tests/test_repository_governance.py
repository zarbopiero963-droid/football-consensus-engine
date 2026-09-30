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
        "Append-only / immutabilità",
        "Separazione ambienti",
        "Anti-leakage",
        "Dati mancanti",
        "Quote vs modello",
        "Timezone",
        "Matching",
        "Pass 2 — Rigoroso e indipendente",
        "Contratto obbligatorio per una nuova result source",
        "Consensus per campo",
        "Contratto pilot 48h",
        "Contratto promotion → production",
        "Decision ticket contract",
        "Settlement contract",
        "Database e migrations",
        "Issue e commenti",
        "READY_FOR_OWNER_REVIEW / PUÒ ESSERE CHIUSA DALL'OWNER",
    ]
    for phrase in required:
        assert phrase in text, f"missing governance contract: {phrase}"


def test_readme_points_agents_to_governance():
    readme = _read("README.md")
    assert "[AGENTS.md](AGENTS.md)" in readme
    assert "[CLAUDE.md](CLAUDE.md)" in readme
    assert "Pass 2 — Rigoroso" in readme
    assert "FOUND_NAME_ONLY" in readme
    assert "TIMEZONE_UNRESOLVED" in readme
    assert "production_eligible" in readme
