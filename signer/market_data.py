"""Signer-owned market reads (governance §2.6 / §2.3bis, review P1-1 of PR #196).

The signer authorizes an order ONLY on data it reads itself, through the `MarketDataProvider` configured in
its own `SignerConfig.market_data` (never passed per call). No caller (MCP, model, tool output) can supply
the market row, the event's markets, the events with the same teams, the quote, the balance, the fees or
the exchange metadata used for authorization. Without a configured provider every order is refused
(`SIGNER_MARKET_READS_UNAVAILABLE`).

`signer/market_reads.py` implements this protocol (`RestMarketDataProvider`, owner gate P2-B) over an
INJECTED GET function: every page of /markets/active, canonical team names, fail closed. The transport
(HTTPS through the shared rate coordinator) is wired by the signer service PR; this package imports no
network library. Tests also use an in-memory provider that plays the role of the exchange.

`read(...)` returns one plain mapping:
    market:            GET /markets/find row of `market_hash` {marketHash, status, sportXeventId,
                       outcomeOneName, outcomeTwoName, teamOneName, teamTwoName, line, type, sportId,
                       leagueId, gameTime, ...}
    market_observed_at: unix seconds
    event_markets:     every market of that event (GET /markets/active?eventId=)
    same_team_events:  every ACTIVE event whose provider team names are the market's (any league / date),
                       {sportXeventId, teamOneName, teamTwoName, gameTime, leagueId, leagueLabel, country,
                       category} as available: the cross-event ambiguity check of §2.3bis
    quote:             {outcome, best_percentage_odds, available_liquidity_base_units (at the order's
                       limit price), observed_at}
    balance:           {available_base_units, observed_at}
    fees:              GET /user/fees-v3 data
    exchange_limits:   {order_size_minimum_base_units, min_resting_order_size_base_units,
                       base_token_decimals (optional cross-check), observed_at}
Missing or malformed items are refusals (fail closed); missing never becomes zero.
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class MarketDataProvider(Protocol):
    def read(self, *, market_hash: str, outcome_one: bool, percentage_odds: int, now: int) -> Mapping[str, Any]:
        """The signer's own reads for one prospective order. May raise: the signer refuses with a fixed code."""
        ...


__all__ = ["MarketDataProvider"]
