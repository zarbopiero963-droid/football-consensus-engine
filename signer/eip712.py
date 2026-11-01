"""EIP-712 typed data and order id for the SX Bet OBv3 `Order` struct.

Source: https://docs.sx.bet/api-reference/eip712-order-signing and
https://docs.sx.bet/developers/posting-orders. Eight fields, in this order
(salt precedes expiry); `timeInForce` is not signed. Domain: name/version from
meta.domain, chainId from meta.chainId, verifyingContract =
checksum(meta.activeAsset.escrowAddress). The order id IS the EIP-712 digest,
lowercased.
"""
from __future__ import annotations

from typing import Any, Mapping

from eth_account.messages import SignableMessage, encode_typed_data
from eth_utils import keccak, to_checksum_address

ORDER_TYPES = {"Order": [
    {"name": "marketHash", "type": "bytes32"},
    {"name": "baseToken", "type": "address"},
    {"name": "totalBetSize", "type": "uint256"},
    {"name": "percentageOdds", "type": "uint256"},
    {"name": "salt", "type": "uint256"},
    {"name": "expiry", "type": "uint256"},
    {"name": "maker", "type": "address"},
    {"name": "isMakerBettingOutcomeOne", "type": "bool"},
]}


def domain(intent: Mapping[str, Any]) -> dict[str, Any]:
    return {"name": intent["domain_name"], "version": intent["domain_version"],
            "chainId": intent["chain_id"], "verifyingContract": to_checksum_address(intent["verifying_contract"])}


def message(intent: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "marketHash": intent["market_hash"],
        "baseToken": to_checksum_address(intent["base_token"]),
        "totalBetSize": int(intent["total_bet_size"]),
        "percentageOdds": int(intent["percentage_odds"]),
        "salt": int(intent["salt"]),
        "expiry": intent["expiry"],
        "maker": to_checksum_address(intent["maker"]),
        "isMakerBettingOutcomeOne": intent["is_maker_betting_outcome_one"],
    }


def signable(intent: Mapping[str, Any]) -> SignableMessage:
    return encode_typed_data(domain_data=domain(intent), message_types=ORDER_TYPES, message_data=message(intent))


def order_id(msg: SignableMessage) -> str:
    """keccak(0x19 || version || domainSeparator || structHash), lowercased hex."""
    return "0x" + keccak(b"\x19" + msg.version + msg.header + msg.body).hex()


def order_payload(intent: Mapping[str, Any], signature_hex: str) -> dict[str, Any]:
    """The documented order object (amounts as strings, expiry as a number). Not sent anywhere here."""
    return {
        "marketHash": intent["market_hash"],
        "maker": to_checksum_address(intent["maker"]),
        "totalBetSize": intent["total_bet_size"],
        "percentageOdds": intent["percentage_odds"],
        "salt": intent["salt"],
        "expiry": intent["expiry"],
        "baseToken": to_checksum_address(intent["base_token"]),
        "isMakerBettingOutcomeOne": intent["is_maker_betting_outcome_one"],
        "timeInForce": intent["time_in_force"],
        "clientOrderId": intent["idempotency_key"],
        "orderSignature": signature_hex,
    }
