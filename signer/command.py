"""Owner-command authorization: the explicit, complete command the owner wrote in the trading chat (§2.6).

Governance §2.6 replaces the per-order out-of-band confirmation with the owner's explicit and complete
command, written in the same conversation of the trading MCP client and verified HERE, by the signer,
deterministically, against every parameter of the order. The signer never trusts the caller's own parsing.

What the signer receives (`OwnerCommandAuthorization`):
- `command_id`: a NEW single-use identifier for this command (owner decision 2). It is consumed in the ledger
  AUTHORIZE / CANCEL_REQUEST line and refused for ever after (`COMMAND_ID_ALREADY_CONSUMED`), whatever the
  time window, also after a restart;
- `command_text`: the literal text the owner wrote; `command_text_sha256` its sha256 (UTF-8). The signer
  recomputes it (`COMMAND_TEXT_HASH_MISMATCH`) and records only hashes, never the text;
- `received_at`: when the MCP received the command (unix seconds). The command is valid for a short window
  (`command_max_age_seconds` of the owner-signed risk config, hard cap 300 s): `COMMAND_EXPIRED`,
  `COMMAND_NOT_YET_VALID`;
- `client_ref_sha256`: sha256 of the client / conversation reference (audit only; never the reference).

Replay (review PR #196 P2-2): besides the id, the FINGERPRINT of the parsed and resolved operation (event,
market, side, stake in base units, signed price, order type, time in force after owner defaults, post-only)
plus the explicit reference token is consumed: the same command, however spelled, cannot authorise a second
operation (`COMMAND_REPLAYED`). A deliberate repetition carries a new reference token "(rif. 2)" / "(ref 2)".

One command = one operation. A command that names two operations (`COMMAND_MULTIPLE_OPERATIONS`) or a
modification (`COMMAND_MODIFY_NOT_SUPPORTED`: owner decision 1, a modification is a cancel command plus a
new order command, each verified on its own) is refused.

Deterministic grammar (IT + EN; decimal comma or point; no thousands separators). Examples:
    Piazza 10 USDC su Over 2,5 in Cape Verde–Liberia a quota minima 1,90
    Metti un Limit a quota 2,10 per 10 USDC su Over 2,5 in Cape Verde–Liberia
    Place 10 USDC on Liberia +1.5 in Cape Verde vs Liberia at min odds 1.90 FOK
    Cancella l'ordine 0x<64 hex>          Cancel all orders
    Cancella gli ordini sul mercato 0x<64 hex>     Cancella gli ordini dell'evento L12003787

Slots: event ("TeamOne–TeamTwo", "TeamOne vs TeamTwo", or "evento <id>"), selection (the outcome label;
"Over/Under <line>" or "<name> <signed line>" carry a line), stake + currency, odds ("quota minima X" /
"min odds X" = MARKET order, taker, IOC/FOK; "Limit ... a quota X" = LIMIT order, maker, GTC), optional
explicit time in force (FOK / IOC / GTC), optional post-only (LIMIT only). Any other word is refused
(`COMMAND_UNPARSEABLE`): there is no fuzzy reading. Missing slot -> `COMMAND_INCOMPLETE_<FIELD>`; two
readings -> `COMMAND_AMBIGUOUS_<FIELD>`; a slot that disagrees with the order -> `COMMAND_MISMATCH_<FIELD>`.

Comparison with the order (`verify_place`): the event, market, line and selection are compared with the
market row the signer holds (exact canonical match, §2.3bis: accents and case folded, nothing fuzzy) and
must identify exactly ONE market among all markets of the event (`COMMAND_AMBIGUOUS_MARKET` otherwise; the
owner may add the provider market type, "(tipo mercato 52)" / "(market type 52)", to disambiguate); the
stake is converted with Decimal arithmetic to base units using the token decimals of the runtime metadata
(no rounding: a fraction below one base unit is `COMMAND_INVALID_STAKE_PRECISION`); the odds are converted
to the implied probability floor(1e20 / odds) and rounded DOWN on the odds ladder, so the signed price is
never worse than the owner's limit ("what you saw is what gets signed"). Every sport, market type and line
the exchange lists is accepted by the grammar: no sport or market whitelist exists here (allowlists are
owner policy in the risk config).
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Mapping

ODDS_PRECISION = 10 ** 20
ODDS_EXCEPTIONS = frozenset({10 ** 17, 999 * 10 ** 17})
COMMAND_MAX_AGE_HARD_CAP_SECONDS = 300
CLOCK_SKEW_SECONDS = 5
MAX_COMMAND_CHARS = 500
MAX_ODDS = Decimal(1000)
MARKET, LIMIT = "MARKET", "LIMIT"
OVER_UNDER, HANDICAP, NO_LINE = "OVER_UNDER", "HANDICAP", "NO_LINE"
SCOPE_ORDER_IDS, SCOPE_MARKET, SCOPE_EVENT, SCOPE_ALL = "ORDER_IDS", "MARKET", "EVENT", "ALL"

_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX32 = r"0x[0-9a-fA-F]{64}"
_NUM = r"(?<![0-9.,])[0-9]{1,12}(?:[.,][0-9]{1,30})?(?![0-9])"  # ASCII digits only (review P3)
_CURRENCY = r"USDC|USDT|USD|EUR|EURO|EUROS|DAI|ETH|WETH|SX|DOLLARI|DOLLARS|€|\$"


class CommandRefused(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OwnerCommandAuthorization:
    command_id: str
    command_text: str
    command_text_sha256: str
    received_at: int
    client_ref_sha256: str

    def __repr__(self) -> str:
        cid = self.command_id[:8] if isinstance(self.command_id, str) else "?"
        return f"OwnerCommandAuthorization(command_id={cid}..., received_at={self.received_at})"


@dataclass(frozen=True)
class PlaceCommand:
    order_type: str                     # MARKET | LIMIT
    stake: Decimal
    currency: str
    odds: Decimal                       # MARKET: minimum acceptable decimal odds; LIMIT: limit price
    selection: str                      # literal selection phrase
    market_kind: str                    # OVER_UNDER | HANDICAP | NO_LINE
    line: Decimal | None
    event_teams: tuple[str, str] | None
    event_id: str | None
    time_in_force: str | None           # explicit in the command, else None (owner-signed default applies)
    post_only: bool
    market_type: int | None = None      # optional explicit provider market type ("tipo mercato 52")
    reference: str | None = None        # explicit reference token "(rif. 2)": part of the replay fingerprint
    operation: str = "PLACE"


@dataclass(frozen=True)
class CancelCommand:
    scope: str                          # ORDER_IDS | MARKET | EVENT | ALL
    order_ids: tuple[str, ...] = ()
    market_hash: str | None = None
    event_id: str | None = None
    reference: str | None = None
    operation: str = "CANCEL"


# ---------------------------------------------------------------- record: hashes, window, snapshot
def text_sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("’", "'").replace("‘", "'").replace("`", "'").replace("−", "-")
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"[.!]+$", "", t).strip()


def _fingerprint(obj: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                                 ensure_ascii=True).encode("ascii")).hexdigest()


def place_fingerprint(verified: Mapping[str, Any], reference: str | None) -> str:
    """Replay fingerprint of an order command: the RESOLVED operation (event, market, side, stake in base
    units, signed price, order type, time in force after owner defaults, post-only) plus the explicit
    reference token. Equivalent spellings ("Over 2,5" / "over 2.50", explicit FOK / default FOK, a trailing
    comma) give the same fingerprint; a deliberate repetition needs a new reference token "(rif. 2)"."""
    keys = ("event_id", "market_hash", "side_one", "stake_base_units", "percentage_odds", "order_type",
            "time_in_force", "post_only")
    return _fingerprint({"op": "PLACE", **{k: verified[k] for k in keys}, "reference": reference})


def cancel_fingerprint(cmd: "CancelCommand") -> str:
    """Replay fingerprint of a cancel command: scope + target + reference token. A risk-reducing cancel (or
    cancel-all) can be repeated by writing a new reference token, e.g. "Cancella tutti gli ordini (rif. 2)"."""
    return _fingerprint({"op": "CANCEL", "scope": cmd.scope, "order_ids": sorted(cmd.order_ids),
                         "market_hash": cmd.market_hash, "event_id": cmd.event_id, "reference": cmd.reference})


def snapshot(auth: Any) -> OwnerCommandAuthorization:
    """A private, type-checked copy (the caller's object is never read again)."""
    if auth is None or isinstance(auth, (str, bytes, Mapping)) or not isinstance(auth, OwnerCommandAuthorization):
        raise CommandRefused("COMMAND_MISSING")
    vals = (auth.command_id, auth.command_text, auth.command_text_sha256, auth.received_at, auth.client_ref_sha256)
    if not (type(vals[0]) is str and type(vals[1]) is str and type(vals[2]) is str and type(vals[3]) is int
            and type(vals[4]) is str):
        raise CommandRefused("COMMAND_MALFORMED")
    return OwnerCommandAuthorization(*vals)


def verify_record(auth: OwnerCommandAuthorization, *, now: int, max_age_seconds: int) -> None:
    """Shape, text hash and window. Never consumes anything (the ledger does, atomically)."""
    if not _ID.fullmatch(auth.command_id):
        raise CommandRefused("COMMAND_MALFORMED")
    if not 1 <= len(auth.command_text) <= MAX_COMMAND_CHARS or any(
            unicodedata.category(c) == "Cc" and c not in "\t\n" for c in auth.command_text):
        raise CommandRefused("COMMAND_MALFORMED")
    if not _SHA.fullmatch(auth.command_text_sha256) or not _SHA.fullmatch(auth.client_ref_sha256):
        raise CommandRefused("COMMAND_MALFORMED")
    if text_sha256(auth.command_text) != auth.command_text_sha256:
        raise CommandRefused("COMMAND_TEXT_HASH_MISMATCH")
    if type(max_age_seconds) is not int or not 0 < max_age_seconds <= COMMAND_MAX_AGE_HARD_CAP_SECONDS:
        raise CommandRefused("COMMAND_WINDOW_NOT_CONFIGURED")
    if auth.received_at > now + CLOCK_SKEW_SECONDS:
        raise CommandRefused("COMMAND_NOT_YET_VALID")
    if now - auth.received_at > max_age_seconds:
        raise CommandRefused("COMMAND_EXPIRED")


def ledger_fields(auth: OwnerCommandAuthorization, fingerprint: str) -> dict[str, Any]:
    """What the ledger records (hashes only: the text itself is never written)."""
    return {"command_id": auth.command_id, "command_text_sha256": auth.command_text_sha256,
            "command_fingerprint_sha256": fingerprint,
            "client_ref_sha256": auth.client_ref_sha256, "command_received_at": auth.received_at}


# ---------------------------------------------------------------- numbers and labels
def _decimal(raw: str) -> Decimal:
    try:
        return Decimal(raw.replace(",", "."))
    except InvalidOperation:  # pragma: no cover - _NUM only matches digits
        raise CommandRefused("COMMAND_UNPARSEABLE") from None


def _num_text(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return s


def canonical_label(s: str) -> str:
    """Exact canonical form of a name / outcome label: accents and case folded, whitespace collapsed,
    numbers normalised (2,50 == 2.5), Italian Over/Under synonyms mapped. Never fuzzy."""
    t = _strip_accents(_normalise(s)).casefold()
    t = re.sub(r"^(?:sopra|piu di)\s+", "over ", t)
    t = re.sub(r"^(?:sotto|meno di)\s+", "under ", t)

    def num(m: re.Match) -> str:
        return m.group(1) + _num_text(_decimal(m.group(2)))

    t = re.sub(r"([+-]?)([0-9]+(?:[.,][0-9]+)?)", num, t)
    return re.sub(r"\s+", " ", t).strip()


def stake_base_units(stake: Decimal, decimals: int) -> int:
    """Decimal-exact: USDC 10.5 with 6 decimals -> 10500000. A fraction of a base unit is refused."""
    if type(decimals) is not int or not 0 <= decimals <= 36:
        raise CommandRefused("COMMAND_TOKEN_DECIMALS_NOT_CONFIGURED")
    units = stake.scaleb(decimals)
    if units != units.to_integral_value():
        raise CommandRefused("COMMAND_INVALID_STAKE_PRECISION")
    return int(units)


def requested_percentage_odds(odds: Decimal) -> int:
    """Implied probability x 1e20 for decimal odds, rounded DOWN (a lower probability = better odds)."""
    num, den = odds.as_integer_ratio()
    return (ODDS_PRECISION * den) // num


def ladder_floor(p: int, step_size: int) -> int:
    if p in ODDS_EXCEPTIONS:
        return p
    step = step_size * 10 ** 15
    return (p // step) * step


def decimal_odds_of(percentage_odds: int) -> Fraction:
    return Fraction(ODDS_PRECISION, percentage_odds)


# ---------------------------------------------------------------- grammar
_VERB_PLACE = r"piazza|punta|scommetti|metti|place|bet|back|put"
_VERB_CANCEL = r"cancella|annulla|cancel"
_VERB_MODIFY = r"modifica|cambia|sposta|aggiorna|modify|change|amend|replace|move|update"
_ANY_VERB = re.compile(rf"(?<![\w'])(?:{_VERB_PLACE}|{_VERB_CANCEL}|{_VERB_MODIFY})(?![\w])", re.I)
_REF = re.compile(r"\s*[,;]?\s*\(?\s*(?:rif\.?|ref\.?|riferimento|reference)\s*[:#]?\s*([A-Za-z0-9_-]{1,32})\s*\)?\s*$",
                  re.I)

_LIMIT_MARK = re.compile(r"(?<![\w])(?:(?:un|una|a|an)\s+)?(?:ordine\s+)?(?:limit|limite)(?:\s+order)?(?![\w])",
                         re.I)
_MARKET_MARK = re.compile(r"(?<![\w])(?:(?:un|a)\s+)?(?:ordine\s+)?market(?:\s+order)?(?![\w])|(?<![\w])a mercato"
                          r"(?![\w])", re.I)
_MIN_ODDS = re.compile(rf"(?<![\w])(?:(?:a|con)\s+)?quota\s+minima(?:\s+di)?\s+({_NUM})"
                       rf"|(?<![\w])at\s+(?:a\s+)?min(?:imum)?\s+odds(?:\s+of)?\s+({_NUM})"
                       rf"|(?<![\w])at\s+odds\s+of\s+at\s+least\s+({_NUM})"
                       rf"|(?<![\w])min(?:imum)?\s+odds\s+({_NUM})", re.I)
_PLAIN_ODDS = re.compile(rf"(?<![\w])(?:(?:a|con)\s+)?quota(?:\s+di)?\s+({_NUM})"
                         rf"|(?<![\w])at\s+odds(?:\s+of)?\s+({_NUM})"
                         rf"|(?<![\w])(?:al\s+)?prezzo(?:\s+limite)?(?:\s+di)?\s+({_NUM})"
                         rf"|(?<![\w])at\s+(?:a\s+)?(?:limit\s+)?price(?:\s+of)?\s+({_NUM})", re.I)
_STAKE = re.compile(rf"(?<![\w])(?:(?:per|for|di|of)\s+)?({_NUM})\s?({_CURRENCY})(?![\w])"
                    rf"|(?<![\w])(?:(?:per|for|di|of)\s+)?({_CURRENCY})\s?({_NUM})", re.I)
_TIF = re.compile(r"(?<![\w])(FOK|IOC|GTC|fill[ -]or[ -]kill|immediate[ -]or[ -]cancel|good[ -]till?[ -]cancell?ed"
                  r"|tutto\s+o\s+niente)(?![\w])", re.I)
_POST_ONLY = re.compile(r"(?<![\w])(?:post[ -]?only|solo\s+maker)(?![\w])", re.I)
_MARKET_TYPE = re.compile(r"(?<![\w])\(?\s*(?:tipo(?:\s+di)?\s+mercato|market\s+type)\s+([0-9]{1,6})\s*\)?(?![\w])",
                          re.I)
_SEL_EVENT = re.compile(r"^[\s|,()]*(?:su|on)\s+(?P<sel>[^|]+?)\s+(?:in|per|nella partita|nel match|nell'evento"
                        r"|in the match|for)\s+(?P<event>[^|]+?)[\s|,()]*$", re.I)
_SEL_ONLY = re.compile(r"(?<![\w])(?:su|on)\s+[^|]+", re.I)
_BARE_NUM = re.compile(_NUM)
_EVENT_ID = re.compile(r"^(?:l'\s?|the\s+)?(?:evento|event)\s*(?:id\s*)?[:#]?\s*([A-Za-z0-9_-]{1,64})$", re.I)
_EVENT_SEP = re.compile(r"\s+(?:vs\.?|v\.?|contro|-)\s+|\s*[–—]\s*", re.I)
_OU = re.compile(rf"^(over|under|sopra|sotto|piu di|più di|meno di)\s+({_NUM})$", re.I)
_HCP = re.compile(rf"^(.+?)\s+([+-]{_NUM})$", re.I)
_TIF_MAP = {"fok": "FOK", "ioc": "IOC", "gtc": "GTC", "fill or kill": "FOK", "immediate or cancel": "IOC",
            "good til cancelled": "GTC", "good till cancelled": "GTC", "good til canceled": "GTC",
            "good till canceled": "GTC", "tutto o niente": "FOK"}

_ORDER_WORD = r"(?:l'\s?ordine|ordine|ordini|gli\s+ordini|i\s+miei\s+ordini|the\s+order|order|orders|my\s+orders)"
_C_ALL = re.compile(r"^(?:tutti\s+(?:gli|i\s+miei)\s+ordini(?:\s+aperti)?|tutto"
                    r"|all(?:\s+my)?(?:\s+open)?\s+orders)$", re.I)
_C_MARKET = re.compile(rf"^(?:tutti\s+)?(?:all\s+)?{_ORDER_WORD}(?:\s+aperti|\s+open)?\s+(?:sul|nel|del|on|in|for)"
                       rf"(?:\s+the)?\s+(?:mercato|market)\s+({_HEX32})$", re.I)
_C_EVENT = re.compile(rf"^(?:tutti\s+)?(?:all\s+)?{_ORDER_WORD}(?:\s+aperti|\s+open)?\s+(?:sull'\s?|dell'\s?|"
                      rf"per\s+l'\s?|on\s+|in\s+|for\s+)(?:the\s+)?(?:evento|event)\s+([A-Za-z0-9_-]{{1,64}})$", re.I)
_C_IDS = re.compile(rf"^{_ORDER_WORD}\s+((?:{_HEX32})(?:\s*(?:,|\be\b|\band\b)\s*{_HEX32})*)$", re.I)


def _one(pattern: re.Pattern, text: str, field: str) -> tuple[re.Match | None, str]:
    found = list(pattern.finditer(text))
    if len(found) > 1:
        raise CommandRefused("COMMAND_AMBIGUOUS_" + field)
    if not found:
        return None, text
    m = found[0]
    return m, text[:m.start()] + " | " + text[m.end():]


def _group(m: re.Match) -> str:
    return next(g for g in m.groups() if g is not None)


def parse(text: Any) -> PlaceCommand | CancelCommand:
    """Deterministic: the same text always gives the same result or the same refusal code."""
    if not isinstance(text, str) or not text.strip():
        raise CommandRefused("COMMAND_MISSING")
    if any(c.isdigit() and not ("0" <= c <= "9") for c in text):
        raise CommandRefused("COMMAND_NON_ASCII_DIGITS")  # e.g. Arabic-Indic or full-width digits (review P3)
    t = _normalise(text)
    ref_m = _REF.search(t)
    reference = ref_m.group(1).casefold() if ref_m else None
    t = (t[:ref_m.start()] if ref_m else t).strip()
    verbs = [m.group(0).casefold() for m in _ANY_VERB.finditer(_TIF.sub(" ", t))]  # "immediate or cancel" is a TIF
    if len(verbs) > 1 or re.search(r";|\be poi\b|\band then\b", t, re.I):
        raise CommandRefused("COMMAND_MULTIPLE_OPERATIONS")
    head = re.match(rf"^({_VERB_PLACE}|{_VERB_CANCEL}|{_VERB_MODIFY})(?![\w])", t, re.I)
    if head is None:
        raise CommandRefused("COMMAND_UNPARSEABLE")
    verb, rest = head.group(1).casefold(), t[head.end():].strip()
    if re.fullmatch(_VERB_MODIFY, verb, re.I):
        raise CommandRefused("COMMAND_MODIFY_NOT_SUPPORTED")
    if re.fullmatch(_VERB_CANCEL, verb, re.I):
        return replace(_parse_cancel(rest), reference=reference)
    return replace(_parse_place(rest), reference=reference)


def _parse_cancel(rest: str) -> CancelCommand:
    if _C_ALL.fullmatch(rest):
        return CancelCommand(SCOPE_ALL)
    m = _C_MARKET.fullmatch(rest)
    if m:
        return CancelCommand(SCOPE_MARKET, market_hash=m.group(1).lower())
    m = _C_EVENT.fullmatch(rest)
    if m:
        return CancelCommand(SCOPE_EVENT, event_id=m.group(1))
    m = _C_IDS.fullmatch(rest)
    if m:
        ids = tuple(x.lower() for x in re.findall(_HEX32, m.group(1)))
        if len(set(ids)) != len(ids):
            raise CommandRefused("COMMAND_AMBIGUOUS_ORDER_IDS")
        return CancelCommand(SCOPE_ORDER_IDS, order_ids=ids)
    if re.fullmatch(rf"{_ORDER_WORD}(?:\s+.*)?", rest, re.I) and not re.search(_HEX32, rest):
        raise CommandRefused("COMMAND_INCOMPLETE_ORDER_IDS")
    raise CommandRefused("COMMAND_UNPARSEABLE")


def _parse_place(rest: str) -> PlaceCommand:
    work = " " + rest + " "
    type_m, work = _one(_MARKET_TYPE, work, "MARKET")  # before the "market" order-type marker
    limit_m, work = _one(_LIMIT_MARK, work, "ORDER_TYPE")
    market_m, work = _one(_MARKET_MARK, work, "ORDER_TYPE")
    min_m, work = _one(_MIN_ODDS, work, "ODDS")
    plain_m, work = _one(_PLAIN_ODDS, work, "ODDS")
    stake_m, work = _one(_STAKE, work, "STAKE")
    tif_m, work = _one(_TIF, work, "TIME_IN_FORCE")
    post_m, work = _one(_POST_ONLY, work, "POST_ONLY")

    se = _SEL_EVENT.fullmatch(work)
    if se is None:
        if not _SEL_ONLY.search(work):
            raise CommandRefused("COMMAND_INCOMPLETE_SELECTION")
        tail = work[_SEL_ONLY.search(work).start():]
        if not re.search(r"\s(?:in|per|nella partita|nel match|nell'evento|in the match|for)\s+\S", tail, re.I):
            raise CommandRefused("COMMAND_INCOMPLETE_EVENT")
    # Required slots, reported before any leftover-text refusal.
    if stake_m is None:
        before = work[:_SEL_ONLY.search(work).start()] if _SEL_ONLY.search(work) else work
        raise CommandRefused("COMMAND_INCOMPLETE_CURRENCY" if _BARE_NUM.search(before) else
                             "COMMAND_INCOMPLETE_STAKE")
    if min_m is None and plain_m is None:
        raise CommandRefused("COMMAND_INCOMPLETE_ODDS")
    if min_m is not None and plain_m is not None:
        raise CommandRefused("COMMAND_AMBIGUOUS_ODDS")
    if se is None:
        raise CommandRefused("COMMAND_UNPARSEABLE")

    tif = _TIF_MAP[re.sub(r"\s+", " ", tif_m.group(1).casefold())] if tif_m else None
    post_only = post_m is not None
    if min_m is not None:
        if limit_m is not None or tif == "GTC" or post_only:
            raise CommandRefused("COMMAND_AMBIGUOUS_ORDER_TYPE")
        order_type, odds_raw = MARKET, _group(min_m)
    else:
        if limit_m is None or market_m is not None:
            raise CommandRefused("COMMAND_INCOMPLETE_ORDER_TYPE")  # "a quota X": minimum or limit price?
        if tif in ("IOC", "FOK"):
            raise CommandRefused("COMMAND_AMBIGUOUS_ORDER_TYPE")
        order_type, odds_raw = LIMIT, _group(plain_m)

    if _thousands_like(odds_raw):
        raise CommandRefused("COMMAND_AMBIGUOUS_ODDS")  # "2,000": two thousand or two? (review P3)
    odds = _decimal(odds_raw)
    if not Decimal(1) < odds <= MAX_ODDS:
        raise CommandRefused("COMMAND_INVALID_ODDS")
    groups = stake_m.groups()
    amount_raw, currency = (groups[0], groups[1]) if groups[0] is not None else (groups[3], groups[2])
    if _thousands_like(amount_raw):
        raise CommandRefused("COMMAND_AMBIGUOUS_STAKE")  # "1.000" / "1,000": thousands or decimals?
    stake = _decimal(amount_raw)
    if stake <= 0:
        raise CommandRefused("COMMAND_INVALID_STAKE")

    selection = se.group("sel").strip()
    kind, line = _selection_line(selection)
    teams, event_id = _event(se.group("event").strip())
    return PlaceCommand(order_type=order_type, stake=stake, currency=currency.upper(), odds=odds,
                        selection=selection, market_kind=kind, line=line, event_teams=teams, event_id=event_id,
                        time_in_force=tif, post_only=post_only,
                        market_type=int(type_m.group(1)) if type_m else None)


def _thousands_like(raw: str) -> bool:
    """Exactly three digits after the separator ("1.000", "2,000"): thousands or decimals? Refused."""
    parts = re.split(r"[.,]", raw.lstrip("+-"))
    return len(parts) == 2 and len(parts[1]) == 3


def _selection_line(selection: str) -> tuple[str, Decimal | None]:
    if not selection:
        raise CommandRefused("COMMAND_INCOMPLETE_SELECTION")
    m = _OU.fullmatch(selection) or _HCP.fullmatch(selection)
    if m and _thousands_like(m.group(2)):
        raise CommandRefused("COMMAND_AMBIGUOUS_LINE")
    m = _OU.fullmatch(selection)
    if m:
        return OVER_UNDER, _decimal(m.group(2))
    m = _HCP.fullmatch(selection)
    if m:
        return HANDICAP, _decimal(m.group(2))
    return NO_LINE, None


def _event(raw: str) -> tuple[tuple[str, str] | None, str | None]:
    m = _EVENT_ID.fullmatch(raw)
    if m:
        return None, m.group(1)
    seps = list(_EVENT_SEP.finditer(raw))
    if not seps:
        raise CommandRefused("COMMAND_INCOMPLETE_EVENT")
    if len(seps) > 1:
        raise CommandRefused("COMMAND_AMBIGUOUS_EVENT")
    a, b = raw[:seps[0].start()].strip(), raw[seps[0].end():].strip()
    if not a or not b:
        raise CommandRefused("COMMAND_INCOMPLETE_EVENT")
    return (a, b), None


# ---------------------------------------------------------------- comparison with the structured order
def _market_line(market: Mapping[str, Any]) -> Decimal | None:
    v = market.get("line")
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise CommandRefused("COMMAND_MARKET_LINE_UNVERIFIABLE")
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        raise CommandRefused("COMMAND_MARKET_LINE_UNVERIFIABLE") from None
    if not d.is_finite():
        raise CommandRefused("COMMAND_MARKET_LINE_UNVERIFIABLE")
    return d


def _row_mismatch(cmd: PlaceCommand, market: Mapping[str, Any], side_one: bool | None) -> str | None:
    """First field of the command that disagrees with this market row (None = the row matches). With
    `side_one` None the selection may be either outcome (used to look for other matching markets)."""
    # -- event (§2.3bis: exact canonical match; an id must be the provider's event id)
    if cmd.event_id is not None:
        if market.get("sportXeventId") is None or str(market.get("sportXeventId")) != cmd.event_id:
            return "COMMAND_MISMATCH_EVENT"
    else:
        one, two = market.get("teamOneName"), market.get("teamTwoName")
        if not (isinstance(one, str) and one.strip() and isinstance(two, str) and two.strip()):
            return "COMMAND_EVENT_UNVERIFIABLE"
        if (canonical_label(cmd.event_teams[0]), canonical_label(cmd.event_teams[1])) != \
                (canonical_label(one), canonical_label(two)):
            return "COMMAND_MISMATCH_EVENT"
    # -- market: optional explicit provider market type, kind and line
    if cmd.market_type is not None and (type(market.get("type")) is not int or market["type"] != cmd.market_type):
        return "COMMAND_MISMATCH_MARKET"
    labels = market.get("outcomeOneName"), market.get("outcomeTwoName")
    if not all(isinstance(x, str) and x.strip() for x in labels):
        return "COMMAND_SELECTION_UNVERIFIABLE"
    canon = [canonical_label(x) for x in labels]
    mline = _market_line(market)
    if cmd.market_kind == OVER_UNDER:
        heads = sorted(c.split(" ", 1)[0] for c in canon)
        if heads != ["over", "under"] or mline is None:
            return "COMMAND_MISMATCH_MARKET"
    elif cmd.market_kind == HANDICAP:
        if mline is None:
            return "COMMAND_MISMATCH_MARKET"
    elif mline is not None:
        return "COMMAND_INCOMPLETE_LINE"  # the market has a line the command does not state
    if cmd.line is not None and abs(cmd.line) != abs(mline):
        return "COMMAND_MISMATCH_LINE"
    # -- selection: the outcome label of the side being signed
    sel = canonical_label(cmd.selection)
    if side_one is None:
        return None if sel in canon else "COMMAND_MISMATCH_SELECTION"
    if sel != canon[0 if side_one else 1]:
        return "COMMAND_MISMATCH_SELECTION"
    return None


def _resolve_event(cmd: PlaceCommand, market: Mapping[str, Any], same_team_events: Any) -> None:
    """§2.3bis across events (review P1-3): the command's teams must identify exactly ONE active event among
    the signer's own reads (`same_team_events`: every active event whose provider team names equal the
    market's, with kickoff, competition, country and category when the provider has them). Two events with
    the same teams (a second leg, a U21 or Women event listed with identical names, a replay) are
    COMMAND_AMBIGUOUS_EVENT unless the command names the provider event id ("evento <id>"). Exact canonical
    names only: fuzzy or containment never counts."""
    own = str(market.get("sportXeventId"))
    if not isinstance(same_team_events, list) or not all(isinstance(e, Mapping) for e in same_team_events):
        raise CommandRefused("SAME_TEAM_EVENTS_MISSING")
    ids = {str(e.get("sportXeventId")) for e in same_team_events}
    if own not in ids:
        raise CommandRefused("SAME_TEAM_EVENTS_INCOMPLETE")
    if cmd.event_id is not None:
        return  # the provider id is unique by construction and was compared with the market row
    want = (canonical_label(cmd.event_teams[0]), canonical_label(cmd.event_teams[1]))
    matching = set()
    for e in same_team_events:
        one, two = e.get("teamOneName"), e.get("teamTwoName")
        if not (isinstance(one, str) and isinstance(two, str)):
            matching.add("<unverifiable>")  # an event we cannot read could be a second match: fail closed
        elif (canonical_label(one), canonical_label(two)) in (want, (want[1], want[0])):
            # Reversed order counts too (review 98e7525 P2-2): the OpenAPI defines teamOneName only as "the
            # first team/player", not as the home side, so "B - A" is not proven to be a different fixture.
            matching.add(str(e.get("sportXeventId")))
    if matching != {own}:
        raise CommandRefused("COMMAND_AMBIGUOUS_EVENT")


def verify_place(cmd: Any, *, intent: Mapping[str, Any], market: Mapping[str, Any], event_markets: Any,
                 same_team_events: Any, post_only: bool, currency: str, token_decimals: int,
                 odds_ladder_step_size: int, market_time_in_force: str, limit_time_in_force: str) -> dict[str, Any]:
    """Compare EVERY field of the parsed command with the order. Raise COMMAND_MISMATCH_<FIELD> on the first
    difference; return the verified order description on success.

    `event_markets`: every market of the event as read from the exchange (GET /markets/active?eventId=).
    The command must identify exactly ONE of them (§2.3bis: ambiguity = no match): two markets with the
    same event, labels and line (e.g. a regulation-time and an incl.-overtime market) are
    COMMAND_AMBIGUOUS_MARKET unless the command names the provider market type ("tipo mercato 52")."""
    if not isinstance(cmd, PlaceCommand):
        raise CommandRefused("COMMAND_MISMATCH_OPERATION")
    side_one = intent["is_maker_betting_outcome_one"]
    if market.get("sportXeventId") is None:
        raise CommandRefused("COMMAND_EVENT_UNVERIFIABLE")  # no provider event id: nothing to bind the order to
    code = _row_mismatch(cmd, market, side_one)
    if code is not None:
        raise CommandRefused(code)
    _resolve_event(cmd, market, same_team_events)
    if not isinstance(event_markets, list) or not all(isinstance(r, Mapping) for r in event_markets):
        raise CommandRefused("EVENT_MARKETS_MISSING")
    own = str(market.get("marketHash", "")).lower()
    hashes = {str(r.get("marketHash", "")).lower() for r in event_markets}
    if own not in hashes:
        raise CommandRefused("EVENT_MARKETS_INCOMPLETE")
    matching = set()
    for r in event_markets:
        try:
            if _row_mismatch(cmd, r, None) is None:
                matching.add(str(r.get("marketHash", "")).lower())
        except CommandRefused:
            matching.add("<unverifiable>")  # a row we cannot read could be a second match: fail closed
    if matching != {own}:
        raise CommandRefused("COMMAND_AMBIGUOUS_MARKET")
    labels = market.get("outcomeOneName"), market.get("outcomeTwoName")
    mline = _market_line(market)
    # -- stake and currency (Decimal-exact)
    if cmd.currency != currency.upper():
        raise CommandRefused("COMMAND_MISMATCH_CURRENCY")
    units = stake_base_units(cmd.stake, token_decimals)
    if units != int(intent["total_bet_size"]):
        raise CommandRefused("COMMAND_MISMATCH_STAKE")
    # -- order type and time in force (owner-signed defaults when the command names none)
    tif = intent["time_in_force"]
    if (cmd.order_type == LIMIT) != (tif == "GTC"):
        raise CommandRefused("COMMAND_MISMATCH_ORDER_TYPE")
    expected_tif = cmd.time_in_force or (limit_time_in_force if cmd.order_type == LIMIT else market_time_in_force)
    if tif != expected_tif:
        raise CommandRefused("COMMAND_MISMATCH_TIME_IN_FORCE")
    # -- odds: floor(1e20 / odds) then round DOWN on the ladder: never worse than the owner's limit
    requested = requested_percentage_odds(cmd.odds)
    signed = ladder_floor(requested, odds_ladder_step_size)
    if int(intent["requested_percentage_odds"]) != requested or int(intent["percentage_odds"]) != signed:
        raise CommandRefused("COMMAND_MISMATCH_ODDS")
    if signed <= 0 or decimal_odds_of(signed) < Fraction(cmd.odds):  # invariant, cannot fail by construction
        raise CommandRefused("COMMAND_MISMATCH_ODDS")
    if bool(post_only) != cmd.post_only:
        raise CommandRefused("COMMAND_MISMATCH_POST_ONLY")
    return {"order_type": cmd.order_type, "time_in_force": tif, "post_only": cmd.post_only,
            "event_id": str(market.get("sportXeventId")), "market_hash": own, "side_one": bool(side_one),
            "selection_label": labels[0 if side_one else 1],
            "line": None if mline is None else str(mline), "stake_base_units": units,
            "min_decimal_odds": _num_text(cmd.odds), "percentage_odds": signed}


def verify_cancel(cmd: Any, *, scope: str, order_ids: tuple[str, ...] = (), market_hash: str | None = None,
                  event_id: str | None = None) -> None:
    """The cancel command must name exactly the requested operation (ids compared as sets: one operation)."""
    if not isinstance(cmd, CancelCommand):
        raise CommandRefused("COMMAND_MISMATCH_OPERATION")
    if cmd.scope != scope:
        raise CommandRefused("COMMAND_MISMATCH_SCOPE")
    if scope == SCOPE_ORDER_IDS and sorted(cmd.order_ids) != sorted(i.lower() for i in order_ids):
        raise CommandRefused("COMMAND_MISMATCH_ORDER_IDS")
    if scope == SCOPE_MARKET and cmd.market_hash != (market_hash or "").lower():
        raise CommandRefused("COMMAND_MISMATCH_MARKET")
    if scope == SCOPE_EVENT and cmd.event_id != event_id:
        raise CommandRefused("COMMAND_MISMATCH_EVENT")


__all__ = ["CancelCommand", "CommandRefused", "OwnerCommandAuthorization", "PlaceCommand", "canonical_label",
           "cancel_fingerprint", "place_fingerprint", "decimal_odds_of", "ladder_floor", "ledger_fields", "parse",
           "requested_percentage_odds", "snapshot", "stake_base_units", "text_sha256", "verify_cancel",
           "verify_place", "verify_record"]
