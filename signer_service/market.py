"""Market data for the service: an injected provider. Only a SIMULATED provider exists in this repository.

The signer must not trust the MCP for labels, fees or quotes, and it has no network access, so the
evidence comes from a provider configured on the signer host. A real provider (exchange reads through
the shared quota coordinator) is MISSING by design in this PR: the simulated one reads a local JSON file
written by the developer (`python -m signer_service dev-init`). Every value it returns is labelled
data_source = "SIMULATED" and the approval page shows it.

File format:
    {"metadata": {"network", "chain_id", "verifying_contract", "base_token", "odds_ladder_step_size"},
     "markets": {"<marketHash>": {"status", "sportXeventId", "eventLabel", "marketLabel",
                                  "outcomeOneName", "outcomeTwoName",
                                  "outcomeOne": {"best_percentage_odds", "available_liquidity_base_units"},
                                  "outcomeTwo": {...}}},
     "balance_base_units": "<decimal>", "fees": {<GET /user/fees-v3 data snapshot>}}
"""
from __future__ import annotations

import copy
import json
from typing import Any, Callable, Mapping, Protocol

DATA_SOURCE = "SIMULATED"


class MarketDataUnavailable(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class MarketDataProvider(Protocol):
    data_source: str

    def metadata(self) -> Mapping[str, Any]:
        ...

    def market(self, market_hash: str) -> Mapping[str, Any]:
        ...

    def fees(self) -> Mapping[str, Any]:
        ...

    def risk_context(self, market_hash: str, outcome: int) -> Mapping[str, Any]:
        ...


class SimulatedMarketData:
    data_source = DATA_SOURCE

    def __init__(self, doc: Mapping[str, Any], clock: Callable[[], float]):
        if not isinstance(doc, Mapping) or not {"metadata", "markets", "balance_base_units", "fees"} <= set(doc):
            raise ValueError("SIMULATED_MARKET_DATA_INVALID")
        self._doc = copy.deepcopy(dict(doc))
        self._clock = clock

    @classmethod
    def from_file(cls, path: str, clock: Callable[[], float]) -> "SimulatedMarketData":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh), clock)

    def metadata(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._doc["metadata"])

    def market(self, market_hash: str) -> Mapping[str, Any]:
        m = self._doc["markets"].get(market_hash)
        if not isinstance(m, Mapping):
            raise MarketDataUnavailable("MARKET_NOT_FOUND")
        return copy.deepcopy(dict(m))

    def fees(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._doc["fees"])

    def quote(self, market_hash: str, outcome: int) -> Mapping[str, Any]:
        side = self.market(market_hash).get("outcomeOne" if outcome == 1 else "outcomeTwo")
        if not isinstance(side, Mapping) or side.get("best_percentage_odds") is None:
            raise MarketDataUnavailable("QUOTE_MISSING")
        return side

    def risk_context(self, market_hash: str, outcome: int) -> Mapping[str, Any]:
        """The signer/risk.py context shape, observed 'now' (simulated)."""
        now = int(self._clock())
        m = self.market(market_hash)
        q = self.quote(market_hash, outcome)
        return {
            "market": {"marketHash": market_hash, "status": m.get("status"), "sportXeventId": m.get("sportXeventId"),
                       "outcomeOneName": m.get("outcomeOneName"), "outcomeTwoName": m.get("outcomeTwoName")},
            "market_observed_at": now,
            "quote": {"outcome": outcome, "best_percentage_odds": q.get("best_percentage_odds"),
                      "available_liquidity_base_units": q.get("available_liquidity_base_units"), "observed_at": now},
            "balance": {"available_base_units": self._doc["balance_base_units"], "observed_at": now},
            "fees": self.fees(),
        }

    # test / dev helper: move the simulated market (e.g. odds drift between request and approval)
    def set_quote(self, market_hash: str, outcome: int, best_percentage_odds: str) -> None:
        self._doc["markets"][market_hash]["outcomeOne" if outcome == 1 else "outcomeTwo"][
            "best_percentage_odds"] = best_percentage_odds
