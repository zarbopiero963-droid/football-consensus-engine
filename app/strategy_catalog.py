from __future__ import annotations

STRATEGY_CATALOG = {
    "S1": {"name": "Probabilità ≥50%", "description": "Pick modello con probabilità almeno 50%."},
    "S2": {"name": "Probabilità ≥60%", "description": "Pick modello con probabilità almeno 60%."},
    "S3": {"name": "Favorito Pinnacle + P≥50%", "description": "Pick uguale al favorito Pinnacle e probabilità almeno 50%."},
    "S4": {"name": "Favorito Pinnacle + P≥60%", "description": "Pick uguale al favorito Pinnacle e probabilità almeno 60%."},
    "S5": {"name": "P≥50% + edge positivo", "description": "Probabilità almeno 50% ed edge positivo."},
    "S6": {"name": "P≥60% + edge positivo", "description": "Probabilità almeno 60% ed edge positivo."},
    "S7": {"name": "Edge ≥3 pp", "description": "Edge almeno 3 punti percentuali."},
    "S8": {"name": "Edge ≥5 pp", "description": "Edge almeno 5 punti percentuali."},
    "S9": {"name": "Edge ≥8 pp", "description": "Edge almeno 8 punti percentuali."},
}

VIRTUAL_STAKE_EUR = 1.00


def sample_reliability(settled: int) -> str:
    if settled < 30:
        return "INSUFFICIENT_SAMPLE"
    if settled < 100:
        return "EARLY_SAMPLE"
    if settled < 250:
        return "DEVELOPING_SAMPLE"
    return "MATURE_SAMPLE"
