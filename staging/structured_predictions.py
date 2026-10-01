"""Source-specific HTML prediction adapters for isolated future pilot slots.

Read only published card/table values. Time, lifecycle and vector diagnostics
remain separate; a valid timestamp never certifies a cross-source match.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import math
import re
import unicodedata


CONTRACT = "CARD_STRUCTURED_TIME_V1"
SOURCES = {"elofoot", "opta_analyst", "clubelo"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}


class Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        return " ".join((c.text() if isinstance(c, Node) else c).strip()
                        for c in self.children).strip()

    def literal_text(self):
        return "".join(c.literal_text() if isinstance(c, Node) else c
                       for c in self.children)

    def has_class(self, value):
        return value in self.attrs.get("class", "").split()


class Tree(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.nodes = 0
        if isinstance(html, bytes):
            html = html.decode("utf-8")
        if len(html.encode("utf-8")) > 5 * 1024 * 1024:
            raise ValueError("source HTML exceeds limit")
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 100000:
            raise ValueError("source DOM exceeds limit")
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        if self.current.tag not in {"script", "style"}:
            self.current.children.append(data)


def explicit_time(raw):
    # A clock, naive datetime, page-day label or browser timezone is not proof.
    if not isinstance(raw, str) or raw.endswith('-00:00') or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})", raw):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def valid_vector(row):
    values = [row.get(k) for k in ("p1", "px", "p2")]
    return (all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 100
                for v in values) and abs(sum(values) - 100) <= 1 + 1e-9)


def base_row(source_id, league=""):
    return {"home_team": "", "away_team": "", "league": league,
            "kickoff_raw": "", "kickoff_utc": None, "source_timezone": "",
            "timezone_status": "TIMEZONE_UNRESOLVED", "p1": None, "px": None, "p2": None,
            "parse_status": "BROKEN", "payload_extra": {
                "source_parser_contract": CONTRACT, "source_match_id": source_id,
                "production_eligible": False, "adds_source_vote": False}}


def attach_time(row, values, display, attribute):
    extra = row["payload_extra"]
    extra["kickoff_display_raw"] = display
    extra["time_evidence"] = {"attribute": attribute, "raw_values": values}
    parsed = [explicit_time(v) for v in values]
    row["kickoff_raw"] = values[0] if values else ""
    if not values or any(v is None for v in parsed) or len(set(parsed)) != 1:
        extra["time_rejection"] = "MISSING_INVALID_OR_CONFLICTING_TIME"
        return
    row["kickoff_utc"] = parsed[0]
    row["source_timezone"] = "UTC" if values[0].endswith("Z") else values[0][-6:]
    row["timezone_status"] = "EXPLICIT_OFFSET"


def finish_row(row, names, probabilities, lifecycle):
    row["payload_extra"]["source_lifecycle"] = lifecycle
    if len(names) == 2 and all(names) and names[0] != names[1]:
        row["home_team"], row["away_team"] = names
    if probabilities is not None:
        row.update(zip(("p1", "px", "p2"), probabilities))
    if row["home_team"] and row["away_team"] and valid_vector(row):
        row["parse_status"] = "OK"
    return row


def parse_elofoot_html(html):
    rows = []
    for card in Tree(html).root.walk():
        href = card.attrs.get("href", "")
        if card.tag != "a" or not href.startswith("/en/prediction/") or "Predicted score" not in card.text():
            continue
        row = base_row(href)
        row["payload_extra"]["source_match_url"] = "https://www.elofoot.com" + href
        names = [n.text() for n in card.walk() if n.has_class("truncate") and
                 n.has_class("text-fg") and n.has_class("font-medium")]
        times = [n for n in card.walk() if n.tag == "time"]
        if times:
            heading = times[0].parent.text()
            row["league"] = heading.rsplit("·", 1)[-1].strip() if "·" in heading else ""
        attach_time(row, [n.attrs.get("datetime", "") for n in times],
                    " | ".join(n.text() for n in times), "time.datetime")
        distributions = re.findall(
            r"\b1\s+(\d+(?:\.\d+)?)\s*%\s+X\s+(\d+(?:\.\d+)?)\s*%\s+2\s+(\d+(?:\.\d+)?)\s*%", card.text())
        probs = list(map(float, distributions[0])) if len(distributions) == 1 else None
        score = [n.text() for n in card.walk() if re.fullmatch(r"\d+\s*[–-]\s*\d+", n.text())]
        if score:
            row["correct_score"] = re.sub(r"\s*[–-]\s*", "-", score[0])
        probability = re.search(r"Predicted score · (\d+(?:\.\d+)?)% probability", card.text())
        if probability:
            row["payload_extra"]["correct_score_probability"] = float(probability.group(1))
        live = any(re.fullmatch(r"LIVE|HT|FT|Half[- ]Time|Full[- ]Time|\d+['′]", n.text(), re.I)
                   for n in card.walk())
        rows.append(finish_row(row, names, probs, "LIVE_OR_FINAL" if live else "FORECAST_CARD"))
    return rows


def parse_opta_html(html):
    rows = []
    for card in Tree(html).root.walk():
        if card.tag != "li" or not card.attrs.get("data-match-id"):
            continue
        league = [n.attrs.get("title", "") or n.text() for n in card.walk()
                  if "match-ticker-tile__comp-name__" in n.attrs.get("class", "")]
        row = base_row(card.attrs["data-match-id"], league[0] if len(league) == 1 else "")
        row["payload_extra"]["source_competition_id"] = card.attrs.get("data-competition-id", "")
        links = [n.attrs["href"] for n in card.walk() if "data-match-tile-link" in n.attrs and "href" in n.attrs]
        if len(links) == 1:
            row["payload_extra"]["source_match_url_raw"] = links[0]
        times = [n for n in card.walk() if n.tag == "time"]
        attach_time(row, [n.attrs.get("datetime", "") for n in times],
                    " | ".join(n.text() for n in times), "time.datetime")
        status = [n.text() for n in card.walk() if "match-ticker-tile__status__" in n.attrs.get("class", "")]
        has_score = any("match-ticker-tile__score__" in n.attrs.get("class", "") for n in card.walk())
        scheduled = len(status) == 1 and status[0].startswith("Kick-off ") and not has_score
        row["payload_extra"]["source_status_raw"] = " | ".join(status)
        distributions = re.findall(r"Win probability:\s*(.+?)\s+(\d+(?:\.\d+)?)%,\s*draw\s+(\d+(?:\.\d+)?)%,\s*(.+?)\s+(\d+(?:\.\d+)?)%\.", card.text(), re.I)
        names, probs = [], None
        if len(distributions) == 1:
            home, p1, px, away, p2 = distributions[0]
            names, probs = [home.strip(), away.strip()], list(map(float, (p1, px, p2)))
            # Do not attach a probability sentence for different teams in this card.
            teams = [n.attrs["title"] for n in card.walk() if "match-ticker-tile__team-link__" in n.attrs.get("class", "") and "title" in n.attrs]
            if teams != names:
                names = []
        rows.append(finish_row(row, names, probs, "SCHEDULED" if scheduled else "LIVE_OR_FINAL"))
    return rows


def club_team(cell):
    names = []
    for n in cell.walk():
        if not n.has_class("min641"):
            continue
        value = n.literal_text()
        siblings = n.parent.children
        for sibling in siblings[siblings.index(n) + 1:]:
            if isinstance(sibling, str) and not sibling.strip():
                continue
            if isinstance(sibling, Node) and sibling.has_class("min1081") and re.fullmatch(r"[A-Za-zÀ-ÿ]*", sibling.text()):
                value += sibling.literal_text()
            else:
                break
        names.append(" ".join(value.split()))
    return names[0] if len(names) == 1 else ""


def club_probability(cell):
    values = [n.text() for n in cell.walk() if n.has_class("min1081")]
    # Read the decimal native presentation, not the responsive rounded duplicate.
    return float(values[0]) if len(values) == 1 and re.fullmatch(r"\d+(?:\.\d+)?", values[0]) else None


def parse_clubelo_html(html):
    rows = []
    for body in Tree(html).root.walk():
        if body.tag not in {"tbody", "table"}:
            continue
        table_rows = [n for n in body.children if isinstance(n, Node) and n.tag == "tr"]
        header = next(([n.text() for n in tr.children if isinstance(n, Node) and n.tag == "th"]
                       for tr in table_rows if any(isinstance(n, Node) and n.tag == "th" for n in tr.children)), [])
        if header[3:6] != ["1", "X", "2"]:
            continue
        league = ""
        for i, tr in enumerate(table_rows):
            headings = [n.text() for n in tr.walk() if n.tag == "i"]
            if len(headings) == 1:
                league = headings[0]
            cells = [n for n in tr.children if isinstance(n, Node) and n.tag == "td"]
            if not cells or "data-live-status-game-id" not in cells[0].attrs:
                continue
            row = base_row(cells[0].attrs["data-live-status-game-id"], league)
            times = [n for n in cells[0].walk() if "data-utc-time" in n.attrs]
            attach_time(row, [n.attrs["data-utc-time"] for n in times],
                        " | ".join(n.text() for n in times), "data-utc-time")
            away = [n for n in table_rows[i + 1].children if isinstance(n, Node) and n.tag == "td"] if i + 1 < len(table_rows) else []
            names, probs = [], None
            # Exact observed paired-row shape, including the shared draw/date cells.
            if (len(cells) == 11 and len(away) == 9 and cells[0].attrs.get("rowspan") == "2"
                    and cells[0].attrs.get("colspan") == "2" and cells[4].attrs.get("rowspan") == "2"
                    and cells[1].has_class("l") and away[0].has_class("l")
                    and not cells[5].text() and not away[2].text()):
                names = [club_team(cells[1]), club_team(away[0])]
                probs = [club_probability(cells[3]), club_probability(cells[4]), club_probability(away[3])]
                row["payload_extra"]["source_team_country_codes"] = [
                    [n.attrs.get("alt", "") for n in cell.walk() if n.tag == "img"]
                    for cell in (cells[1], away[0])]
            live = any(re.fullmatch(r"LIVE|HT|FT|Half[- ]Time|Full[- ]Time|\d+['′]", n.text(), re.I)
                       for n in cells[0].walk())
            rows.append(finish_row(row, names, probs, "LIVE_OR_FINAL" if live else "FORECAST_TABLE"))
    return rows


HTML_PARSERS = {"elofoot": parse_elofoot_html, "opta_analyst": parse_opta_html,
                "clubelo": parse_clubelo_html}


def select_prematch(rows, cutoff):
    """Only future, explicit, complete native rows; retain rejection diagnostics."""
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("explicit ingestion cutoff required")
    ids = defaultdict(list)
    fixture_ids = defaultdict(set)
    for row in rows:
        source_id = row["payload_extra"]["source_match_id"]
        ids[source_id].append(row)
        if row['home_team'] and row['away_team'] and row['kickoff_utc'] and row['league']:
            fixture_ids[(row['home_team'], row['away_team'], row['kickoff_utc'], row['league'])].add(source_id)
    accepted, rejected = [], []
    for source_id, candidates in ids.items():
        row = candidates[0]
        reason = None
        if not source_id or any(c != row for c in candidates[1:]):
            reason = "AMBIGUOUS_SOURCE_ID"
        elif len(fixture_ids[(row['home_team'], row['away_team'], row['kickoff_utc'], row['league'])]) > 1:
            reason = "AMBIGUOUS_FIXTURE_IDS"
        elif row["payload_extra"]["source_lifecycle"] not in {"FORECAST_CARD", "FORECAST_TABLE", "SCHEDULED"}:
            reason = "LIVE_OR_FINAL"
        elif row["parse_status"] != "OK" or not valid_vector(row):
            reason = "INVALID_OR_INCOMPLETE_VECTOR_OR_PAIR"
        elif row["timezone_status"] != "EXPLICIT_OFFSET" or row["kickoff_utc"] is None:
            reason = "TIME_UNRESOLVED"
        elif not row["league"]:
            reason = "COMPETITION_MISSING"
        elif row["kickoff_utc"] <= cutoff:
            reason = "NOT_PREMATCH_AT_INGESTION"
        if reason:
            rejected.append({"source_match_id": source_id, "reason": reason})
        else:
            accepted.append(row)
    return accepted, {"contract": CONTRACT, "ingestion_cutoff_utc": cutoff.isoformat(),
                      "cards_seen": len(rows), "rows_accepted": len(accepted),
                      "identical_duplicates": sum(len(c) - 1 for c in ids.values() if all(r == c[0] for r in c)),
                      "rejections": rejected, "rejection_counts": dict(Counter(r["reason"] for r in rejected))}


def captured_prematch(capture, cutoff):
    if not capture.accepted:
        rows, audit = select_prematch([], cutoff)
        audit["capture_rejected"] = capture.audit["state"]
        return rows, audit
    return select_prematch(capture.parsed, cutoff)


def prediction_key(source, row):
    extra = row.get('payload_extra', {})
    if extra.get('source_parser_contract') == CONTRACT:
        identity = f"{source}|{extra['source_match_id']}|{row['kickoff_utc'].isoformat()}"
    else:
        identity = f"{source}|{row['home_team']}|{row['away_team']}|{row.get('kickoff_raw', '')}"
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def diagnostic_match(pred, anchors):
    """Strict hints only; published UTC never qualifies canonical/context binding."""
    def exact(value):
        return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())
    pair = (exact(pred["home_team"]), exact(pred["away_team"]))
    candidates = [a for a in anchors if (exact(a["home_team"]), exact(a["away_team"])) == pair]
    if not candidates:
        return None, "NOT_FOUND", 0.0, "exact ordered pair absent"
    if len(candidates) != 1:
        return None, "AMBIGUOUS", 0.0, "multiple exact ordered pairs"
    anchor = candidates[0]
    kickoff = pred.get("kickoff_utc")
    if kickoff is None or anchor["kickoff_utc"] is None:
        return None, "TIMEZONE_UNRESOLVED", 0.0, "explicit kickoff missing"
    if abs((kickoff - anchor["kickoff_utc"]).total_seconds()) > 900:
        return None, "KICKOFF_MISMATCH", 0.0, "kickoff differs by more than 15 minutes"
    if not pred.get("league") or not anchor.get("league") or exact(pred["league"]) != exact(anchor["league"]):
        return None, "LEAGUE_MISMATCH", 0.0, "competition context differs or missing"
    return anchor["match_id"], "FOUND_NAME_ONLY", 0.7, "pair/time/league observed; canonical/category binding not qualified"
