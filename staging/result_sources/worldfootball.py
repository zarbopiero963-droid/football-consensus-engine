"""Published worldfootball gameplan rows; clocks never become results."""
from collections import Counter, defaultdict
import re

from staging.structured_predictions import Tree, explicit_time


CONTRACT = "WORLDFOOTBALL_FINISHED_HTML_V1"


def _one(node, cls):
    values = [n.text().strip() for n in node.walk() if n.has_class(cls)]
    return values[0] if len(values) == 1 else None


def parse_document(html):
    root = Tree(html).root
    headers = [n for n in root.walk() if n.tag == "h2" and n.has_class("item-competition")]
    plans = [n for n in root.walk() if n.has_class("module-gameplan")]
    audit = {"contract": CONTRACT, "cards_seen": 0, "rows_accepted": 0,
             "identical_duplicates": 0, "rejection_counts": {}, "rejections": []}
    if len(headers) != 1 or len(plans) != 1:
        audit["rejection_counts"] = {"CONTEXT_UNRESOLVED": 1}
        return {"rows": [], "audit": audit}
    competition = _one(headers[0], "competition-name_extended")
    gender = headers[0].attrs.get("data-gender", "")
    competition_ids = {n.attrs.get("data-competition_id") for n in plans[0].walk()
                       if n.has_class("round-head")}
    context_ok = bool(competition and gender and len(competition_ids) == 1 and None not in competition_ids)
    groups = defaultdict(list)
    rejections = []
    for card in plans[0].walk():
        if not card.has_class("match"):
            continue
        audit["cards_seen"] += 1
        sid = card.attrs.get("data-match_id", "")
        reason = None
        classes = card.attrs.get("class", "").split()
        if ("finished" not in classes or any(c in classes for c in ("live", "running", "postponed", "cancelled", "scheduled"))
                or _one(card, "match-status") != "Ended"):
            reason = "NOT_FINISHED"
        elif not context_ok or card.attrs.get("data-competition_id") not in competition_ids or card.attrs.get("data-gender") != gender:
            reason = "CONTEXT_UNRESOLVED"
        home, away = _one(card, "team-name-home"), _one(card, "team-name-away")
        raw_score = _one(card, "match-result")
        score = re.fullmatch(r"(\d{1,2}):(\d{1,2})", raw_score or "")
        kickoff_raw, end_raw = card.attrs.get("data-datetime", ""), card.attrs.get("data-datetime_end", "")
        kickoff, end = explicit_time(kickoff_raw), explicit_time(end_raw)
        if reason is None and (not sid.isdigit() or not home or not away or home == away or score is None):
            reason = "INVALID_ID_PAIR_OR_SCORE"
        if reason is None and (kickoff is None or end is None or end <= kickoff):
            reason = "TIME_UNRESOLVED_OR_INVALID_END"
        if reason:
            rejections.append({"source_match_id": sid, "reason": reason})
            continue
        groups[sid].append({
            "home_team": home, "away_team": away, "competition": competition,
            "competition_name": competition, "source_date": kickoff.date(),
            "status": "FINISHED", "final_home_goals": int(score.group(1)),
            "final_away_goals": int(score.group(2)), "ht_home_goals": None,
            "ht_away_goals": None, "corners_home": None, "corners_away": None,
            "ht_corners_home": None, "ht_corners_away": None,
            "source_parser_contract": CONTRACT, "source_match_id": sid,
            "source_competition_id": card.attrs["data-competition_id"],
            "source_gender": gender, "source_competition_type": card.attrs.get("data-competition_type", ""),
            "source_kickoff_raw": kickoff_raw, "source_kickoff_utc": kickoff,
            "source_end_raw": end_raw, "source_end_utc": end,
            "timezone_status": "EXPLICIT_OFFSET", "lifecycle_evidence": "finished + Ended + data-datetime_end",
            "production_eligible": False,
        })
    rows = []
    rejected_ids = {r["source_match_id"] for r in rejections if r["source_match_id"]}
    fixtures = defaultdict(set)
    for sid, candidates in groups.items():
        for row in candidates:
            fixtures[(row['home_team'], row['away_team'], row['source_kickoff_utc'], row['competition'])].add(sid)
    for sid, candidates in groups.items():
        row = candidates[0]
        if sid in rejected_ids or any(row != candidate for candidate in candidates[1:]):
            rejections.append({"source_match_id": sid, "reason": "CONFLICTING_ID"})
        elif len(fixtures[(row['home_team'], row['away_team'], row['source_kickoff_utc'], row['competition'])]) > 1:
            rejections.append({"source_match_id": sid, "reason": "AMBIGUOUS_FIXTURE_ID"})
        else:
            audit["identical_duplicates"] += len(candidates) - 1
            rows.append(candidates[0])
    audit["rows_accepted"] = len(rows)
    audit["rejections"] = rejections
    audit["rejection_counts"] = dict(Counter(r["reason"] for r in rejections))
    return {"rows": rows, "audit": audit}


def parse_html(html):
    return parse_document(html)["rows"]
