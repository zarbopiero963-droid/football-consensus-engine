#!/usr/bin/env python3
"""Real pre-match probability backtest. No imputed probabilities."""
import csv, json, math, os, re, unicodedata
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from html.parser import HTMLParser
from statistics import mean, pstdev, stdev

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "out")
os.makedirs(OUT, exist_ok=True)

# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------
ALIASES = {
    "man city": "manchester city",
    "manchester city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "manchester utd": "manchester united",
    "nottm forest": "nottingham forest",
    "nottingham": "nottingham forest",
    "wolves": "wolverhampton",
    "wolverhampton wanderers": "wolverhampton",
    "spurs": "tottenham",
    "tottenham hotspur": "tottenham",
    "newcastle": "newcastle",
    "newcastle utd": "newcastle",
    "west ham": "west ham",
    "west ham united": "west ham",
    "brighton": "brighton",
    "brighton and hove albion": "brighton",
    "leicester": "leicester",
    "leicester city": "leicester",
    "ipswich": "ipswich",
    "ipswich town": "ipswich",
    "sporting cp": "sporting",
    "sporting lisbon": "sporting",
    "sporting": "sporting",
    "inter": "inter",
    "internazionale": "inter",
    "inter milan": "inter",
    "ac milan": "milan",
    "milan": "milan",
    "as roma": "roma",
    "roma": "roma",
    "athletic club": "athletic bilbao",
    "athletic bilbao": "athletic bilbao",
    "real betis": "betis",
    "betis": "betis",
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "paris saint germain": "paris saint germain",
    "bayern munich": "bayern munchen",
    "bayern munchen": "bayern munchen",
    "bayern": "bayern munchen",
    "borussia dortmund": "dortmund",
    "dortmund": "dortmund",
    "rb leipzig": "leipzig",
    "rasenballsport leipzig": "leipzig",
    "leipzig": "leipzig",
    "bayer leverkusen": "leverkusen",
    "leverkusen": "leverkusen",
    "borussia monchengladbach": "gladbach",
    "monchengladbach": "gladbach",
    "mgladbach": "gladbach",
    "fc koln": "koln",
    "cologne": "koln",
    "koln": "koln",
    "ath madrid": "atletico madrid",
    "atletico madrid": "atletico madrid",
    "atl madrid": "atletico madrid",
    "sociedad": "real sociedad",
    "real sociedad": "real sociedad",
    "bilbao": "athletic bilbao",
    "espanol": "espanyol",
    "espanyol": "espanyol",
    "celta": "celta vigo",
    "celta vigo": "celta vigo",
    "betis": "betis",
    "valladolid": "valladolid",
    "real valladolid": "valladolid",
    "cordoba": "cordoba",
    "alaves": "alaves",
    "deportivo alaves": "alaves",
    "rayo vallecano": "rayo vallecano",
    "vallecano": "rayo vallecano",
    "fc porto": "porto",
    "porto": "porto",
    "benfica": "benfica",
    "sporting braga": "braga",
    "braga": "braga",
    "ajax": "ajax",
    "psv": "psv",
    "psv eindhoven": "psv",
    "feyenoord": "feyenoord",
    "az": "az alkmaar",
    "az alkmaar": "az alkmaar",
}

STOP = {
    "fc","cf","sc","ac","afc","club","de","the","calcio","ud","cd","sd","fk","sk",
    "if","bk","gk","sv","vfb","vfl","tsg","rc","as","ss","us","osc","ogc","cfc",
    "real","city","united","utd","town","rovers","wanderers","athletic","sporting",
    "hotspur","albion","county","borough",
}
# keep distinctive tokens: do NOT strip city/united blindly inside norm used for equality.
# stripping too much merges Man City and Man United. Aliases handle those.
# STOP is used only for token-jaccard fallback, not for the canonical string.

def strip_accents(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))

def canon(s):
    s = strip_accents(s).lower().replace("&", " and ").replace("'", "")
    s = s.replace(".", " ")
    s = re.sub(r"\b(fc|cf|sc|ac|afc|ud|cd|sd|fk|sk|sv|rc|as|ss|us|osc|ogc)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return ALIASES.get(s, s)

def tokens(s):
    raw = strip_accents(s).lower()
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    toks = [t for t in raw.split() if t not in {"fc","cf","sc","ac","afc","ud","cd","the","de","club"} and len(t) > 1]
    return set(toks)

def similar(a, b):
    na, nb = canon(a), canon(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    inter = ta & tb
    if not inter:
        return False
    j = len(inter) / len(ta | tb)
    if j >= 0.6:
        return True
    # single distinctive shared token (e.g. "Sunderland")
    if len(inter) >= 1 and min(len(ta), len(tb)) <= 2 and j >= 0.34:
        # reject if the only overlap is a generic word
        generic = {"city","united","real","sporting","athletic","rovers","town","county","west","east","north","south","inter","atletico","deportivo","dynamo","dinamo","saint","st"}
        if inter - generic:
            return True
    return False

def parse_date_flexible(s):
    s = str(s).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

# ---------------------------------------------------------------------------
# FootInsights
# ---------------------------------------------------------------------------
INTL = {
    "UEFA Nations League", "International Friendlies",
    "Africa Cup of Nations Qualification", "CONCACAF Nations League",
    "Gulf Cup of Nations",
}

def load_footinsights():
    path = os.path.join(ROOT, "raw/footinsights_predictions.csv")
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["market"] != "1x2":
                continue
            if r["home_goals"] == "" or r["away_goals"] == "":
                continue
            rows.append(r)
    groups = defaultdict(list)
    for r in rows:
        groups[(r["kickoff_utc"], r["home_team"], r["away_team"])].append(r)

    def choose(rs):
        # one row per model_version: earliest settlement
        by = {}
        for r in rs:
            mv = r["model_version"]
            if mv not in by or r["settled_at"] < by[mv]["settled_at"]:
                by[mv] = r
        league = rs[0]["league"]
        d = rs[0]["kickoff_utc"][:10]
        if league in INTL and "nt-elo-0.1.0" in by:
            return by["nt-elo-0.1.0"], "intl-model"
        if league == "UEFA Champions League":
            for mv in ("cl-elo-0.2.0", "cl-elo-0.1.0"):
                if mv in by:
                    return by[mv], "ucl-model"
        if league == "UEFA Europa League" and "el-elo-0.1.0" in by:
            return by["el-elo-0.1.0"], "uel-model"
        if d < "2026-09-25" and "dc-0.1.0" in by:
            return by["dc-0.1.0"], "pre-25-sep-dc"
        for mv in ("ml-1.0.3", "ml-1.0.2", "ensemble-0.1.0", "dc-0.1.0"):
            if mv in by and d >= "2026-09-25":
                return by[mv], "post-25-sep-priority"
        # fallback earliest settled
        return sorted(rs, key=lambda r: r["settled_at"])[0], "earliest-settled"

    chosen = []
    for rs in groups.values():
        r, rule = choose(rs)
        chosen.append((r, rule, sorted({x["model_version"] for x in rs})))
    return chosen

# ---------------------------------------------------------------------------
# Foresportia
# ---------------------------------------------------------------------------
def load_foresportia():
    rows = json.load(open(os.path.join(ROOT, "raw/foresportia_by_league.json"), encoding="utf-8"))
    out = []
    for r in rows:
        d = str(r.get("date match") or "")[:10]
        if not ("2026-08-01" <= d <= "2026-09-28"):
            continue
        try:
            hg = int(float(r["score dom"]))
            ag = int(float(r["score ext"]))
            p1 = float(r["Victoire domicile"])
            px = float(r["Match nul"])
            p2 = float(r["Victoire extérieure"])
        except (TypeError, ValueError, KeyError):
            continue
        if p1 < 0 or px < 0 or p2 < 0:
            continue
        out.append({
            "date": d,
            "league": r.get("Ligue"),
            "home": r.get("Équipe domicile"),
            "away": r.get("Équipe visiteuse"),
            "hg": hg, "ag": ag,
            "p": (p1, px, p2),  # percent
            "time": r.get("heure"),
            "stability": r.get("stability_badge"),
            "version": r.get("stability_version"),
        })
    return out

# ---------------------------------------------------------------------------
# Statarea HTML
# ---------------------------------------------------------------------------
class TDParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tds = []
        self._cur = None
        self._buf = []
    def handle_starttag(self, tag, attrs):
        if tag == "td":
            self._cur = []
            self._buf = []
        elif tag == "tr" and self._cur is None:
            pass
    def handle_endtag(self, tag):
        if tag == "td" and self._cur is not None:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self.tds.append(text)
            self._cur = None
    def handle_data(self, data):
        if self._cur is not None:
            self._buf.append(data)

def load_statarea():
    matches = []
    for fn in sorted(os.listdir(os.path.join(ROOT, "raw/statarea"))):
        if not fn.endswith(".html"):
            continue
        page_date = fn.replace(".html", "")
        html = open(os.path.join(ROOT, "raw/statarea", fn), encoding="utf-8", errors="replace").read()
        p = TDParser()
        p.feed(html)
        tds = p.tds
        league = None
        i = 0
        while i < len(tds):
            t = tds[i]
            if t.startswith("All predictions"):
                i += 1
                continue
            if t.startswith("time:"):
                # previous non-empty league cell should already be set
                i += 1
                continue
            if t.startswith("prediction:"):
                # next two team rows then header then percents — but league is a prior cell
                # look back for league: last cell that isn't time/prediction/percent/team
                i += 1
                continue
            # league headers are like "Spain-Laliga2" and not percentages
            if (not t.endswith("%") and not t[:1].isdigit() and ":" not in t
                    and t not in {"1","X","2","H1","HX","H2","1.5","2.5","3.5","BTS","OTS","V"}
                    and len(t) > 3 and "-" in t and not re.fullmatch(r"\d+", t)):
                league = t
                i += 1
                continue
            i += 1
        # second pass with explicit state
        league = None
        i = 0
        while i < len(tds):
            t = tds[i]
            if re.match(r"^[A-Za-z].+-.+", t) and not t.startswith("All predictions") and not t.startswith("time") and not t.startswith("prediction") and "%" not in t and len(t) > 5:
                # could be a team name with hyphen e.g. "Real Salt" no
                # league names in this HTML contain a hyphen country-comp and appear before time
                if i + 1 < len(tds) and tds[i+1].startswith("time:"):
                    league = t
                    i += 1
                    continue
            if t.startswith("time:") and league:
                # expect prediction, home, away, headers, percents
                # scan forward a little
                window = tds[i:i+20]
                try:
                    pi = next(k for k,x in enumerate(window) if x.startswith("prediction:"))
                except StopIteration:
                    i += 1
                    continue
                # teams are the next two non-header cells after prediction that aren't V and aren't percent
                rest = window[pi+1:]
                teams = []
                perc = []
                for x in rest:
                    if x in {"1","X","2","H1","HX","H2","1.5","2.5","3.5","BTS","OTS","V"}:
                        continue
                    if x.endswith("%"):
                        perc.append(float(x.replace("%","")))
                        if len(perc) == 11:
                            break
                        continue
                    if x.startswith("time:") or x.startswith("prediction:"):
                        break
                    if re.match(r"^[A-Za-z].+-.+", x) and "-" in x and len(teams) >= 2:
                        break
                    if x and not x.endswith("%"):
                        # score cells were separate tds: team then score. Our parser flattens
                        # so team name and score are SEPARATE tds.
                        teams.append(x)
                # The sequence after prediction is: optional V, home, home_score, away, away_score, headers..., percents
                # Re-parse more tightly from raw window
                i += 1
                continue
            i += 1

        # Robust regex on original HTML for each match block
        # Split by time rows
        parts = re.split(r"time:\s*([0-9]{1,2}:[0-9]{2})", html)
        # parts[0] preamble includes first league
        current_league = None
        # find leagues in preamble order by walking
        # We'll re-scan with a simpler regex per block including preceding league
        blocks = re.findall(
            r"(?:<td[^>]*>\s*([^<]{4,80}?)\s*</td>\s*</tr>\s*<tr><td[^>]*>\s*time:\s*([0-9:]+).*?"
            r"prediction:\s*<b>([^<]+)</b>.*?"
            r"<td[^>]*>\s*([^<]+?)\s*</td><td[^>]*>\s*([^<]*?)\s*</td></tr>\s*<tr><td[^>]*>\s*([^<]+?)\s*</td><td[^>]*>\s*([^<]*?)\s*</td>.*?"
            r"<font size=\"1\">([0-9.]+)%</font>.*?<font size=\"1\">([0-9.]+)%</font>.*?<font size=\"1\">([0-9.]+)%</font>.*?"
            r"<font size=\"1\">([0-9.]+)%</font>.*?<font size=\"1\">([0-9.]+)%</font>.*?<font size=\"1\">([0-9.]+)%</font>.*?"
            r"<font size=\"1\">([0-9.]+)%</font>.*?<font size=\"1\">([0-9.]+)%</font>.*?<font size=\"1\">([0-9.]+)%</font>.*?"
            r"<font size=\"1\">([0-9.]+)%</font>.*?<font size=\"1\">([0-9.]+)%</font>",
            html, flags=re.S)
        # The regex above is too greedy/fragile. Use a cleaner line-oriented extraction.
    return parse_statarea_files()

def parse_statarea_files():
    matches = []
    for fn in sorted(os.listdir(os.path.join(ROOT, "raw/statarea"))):
        if not fn.endswith(".html"):
            continue
        page_date = fn.replace(".html", "")
        html = open(os.path.join(ROOT, "raw/statarea", fn), encoding="utf-8", errors="replace").read()
        # isolate the predictions table
        m = re.search(r"All predictions for\s+\d{4}-\d{2}-\d{2}(.*)$", html, flags=re.S)
        chunk = m.group(1) if m else html
        rows = re.findall(r"<tr>(.*?)</tr>", chunk, flags=re.S)
        league = None
        pending_time = None
        pending_pred = None
        teams = []
        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)
            texts = [re.sub(r"<[^>]+>", "", c) for c in cells]
            texts = [re.sub(r"\s+", " ", t).replace("\xa0", " ").strip() for t in texts]
            texts = [t for t in texts if t]
            if not texts:
                continue
            joined = " | ".join(texts)
            if len(texts) == 1 and "-" in texts[0] and not texts[0].startswith("time") and not texts[0].startswith("prediction") and not texts[0].startswith("All "):
                league = texts[0]
                continue
            if texts[0].startswith("time:"):
                pending_time = texts[0].split(":", 1)[1].strip() if False else texts[0].replace("time:", "").strip()
                pending_pred = None
                teams = []
                continue
            if texts[0].startswith("prediction:"):
                pending_pred = texts[0].replace("prediction:", "").strip()
                continue
            if texts[0] in {"1"} and "X" in texts and "2" in texts:
                continue
            if any(t.endswith("%") for t in texts) and len(texts) >= 11 and len(teams) >= 2:
                try:
                    nums = [float(t.replace("%", "")) for t in texts[:11]]
                except ValueError:
                    teams = []
                    continue
                home, hs = teams[0]
                away, as_ = teams[1]
                if hs is None or as_ is None:
                    teams = []
                    continue
                matches.append({
                    "date": page_date,
                    "league": league,
                    "time": pending_time,
                    "pred_label": pending_pred,
                    "home": home,
                    "away": away,
                    "hg": hs,
                    "ag": as_,
                    "p": (nums[0], nums[1], nums[2]),  # 1 X 2 percent
                    "p_over15": nums[6],
                    "p_over25": nums[7],
                    "p_over35": nums[8],
                    "p_btts": nums[9],
                    "p_ots": nums[10],
                })
                teams = []
                continue
            # team row: name + optional score
            if league and pending_time and not texts[0].endswith("%") and texts[0] not in {"1","X","2","V"}:
                name = texts[0]
                score = None
                if len(texts) >= 2 and re.fullmatch(r"\d+", texts[-1]):
                    score = int(texts[-1])
                    if len(texts) > 2:
                        name = " ".join(texts[:-1])
                elif len(texts) == 1:
                    score = None
                teams.append((name, score))
                if len(teams) > 2:
                    teams = teams[-2:]
    return matches

# ---------------------------------------------------------------------------
# football-data odds
# ---------------------------------------------------------------------------
def load_odds():
    rows = []
    for fn in sorted(os.listdir(os.path.join(ROOT, "raw/fd"))):
        if not fn.endswith(".csv"):
            continue
        with open(os.path.join(ROOT, "raw/fd", fn), newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if not r.get("HomeTeam") or not r.get("FTHG"):
                    continue
                d = parse_date_flexible(r["Date"])
                if d is None:
                    continue
                if not (datetime(2026,8,1).date() <= d <= datetime(2026,9,28).date()):
                    continue
                try:
                    hg, ag = int(r["FTHG"]), int(r["FTAG"])
                except ValueError:
                    continue
                def fnum(k):
                    v = (r.get(k) or "").strip()
                    if not v:
                        return None
                    try:
                        return float(v)
                    except ValueError:
                        return None
                rows.append({
                    "div": r.get("Div") or fn.replace(".csv",""),
                    "date": d.isoformat(),
                    "home": r["HomeTeam"],
                    "away": r["AwayTeam"],
                    "hg": hg, "ag": ag,
                    "b365": (fnum("B365H"), fnum("B365D"), fnum("B365A")),
                    "b365c": (fnum("B365CH"), fnum("B365CD"), fnum("B365CA")),
                    "pinnacle": (fnum("PPH"), fnum("PPD"), fnum("PPA")),
                    "pinnacle_close": (fnum("PPCH"), fnum("PPCD"), fnum("PPCA")),
                    "avg": (fnum("AvgH"), fnum("AvgD"), fnum("AvgA")),
                    "avg_close": (fnum("AvgCH"), fnum("AvgCD"), fnum("AvgCA")),
                    "ou_b365": (fnum("B365>2.5"), fnum("B365<2.5")),
                    "ou_b365c": (fnum("B365C>2.5"), fnum("B365C<2.5")),
                })
    return rows

# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
LABELS = ["1", "X", "2"]

def norm_probs(p):
    p = [float(x) for x in p]
    # accept percent or fraction
    if max(p) > 1.5:
        p = [x / 100.0 for x in p]
    s = sum(p)
    if s <= 0:
        return None
    return [x / s for x in p]

def y_of(hg, ag):
    if hg > ag: return 0
    if hg == ag: return 1
    return 2

def brier(p, y):
    return sum((p[i] - (1.0 if i == y else 0.0)) ** 2 for i in range(3))

def logloss(p, y):
    return -math.log(max(p[y], 1e-15))

def argmax(p):
    m = max(p)
    idxs = [i for i, v in enumerate(p) if abs(v - m) < 1e-12]
    if len(idxs) != 1:
        return None  # tie
    return idxs[0]

def fair_probs(odds):
    if any(o is None or o <= 1.0 for o in odds):
        return None
    inv = [1.0 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv], s  # probs, overround raw sum

def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = mean(xs), mean(ys)
    num = sum((a-mx)*(b-my) for a,b in zip(xs, ys))
    dx = math.sqrt(sum((a-mx)**2 for a in xs))
    dy = math.sqrt(sum((b-my)**2 for b in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)

def summarize_model(name, probs, ys):
    n = len(ys)
    correct = 0
    bs = []
    ll = []
    for p, y in zip(probs, ys):
        pick = argmax(p)
        if pick is not None and pick == y:
            correct += 1
        bs.append(brier(p, y))
        ll.append(logloss(p, y))
    return {
        "model": name,
        "n": n,
        "correct": correct,
        "accuracy": correct / n if n else None,
        "brier": mean(bs) if bs else None,
        "logloss": mean(ll) if ll else None,
    }

def max_dd_and_streak(pnls):
    """pnls in chronological order. Return max drawdown (positive number), max losing streak."""
    peak = 0.0
    equity = 0.0
    mdd = 0.0
    streak = 0
    max_streak = 0
    for x in pnls:
        equity += x
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > mdd:
            mdd = dd
        if x < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return mdd, max_streak

def join_on(base, others, date_slack=1):
    """base items have date iso, home, away, hg, ag. others same.
    Returns list of (base_idx, other) with score agreement.
    """
    by_date = defaultdict(list)
    for j, o in enumerate(others):
        by_date[o["date"]].append(j)
    used = set()
    pairs = []
    for i, b in enumerate(base):
        bd = datetime.strptime(b["date"], "%Y-%m-%d").date()
        cands = []
        for delta in range(-date_slack, date_slack+1):
            key = (bd + timedelta(days=delta)).isoformat()
            for j in by_date.get(key, []):
                if j in used:
                    continue
                o = others[j]
                if o["hg"] != b["hg"] or o["ag"] != b["ag"]:
                    continue
                if similar(b["home"], o["home"]) and similar(b["away"], o["away"]):
                    cands.append(j)
        if len(cands) == 1:
            used.add(cands[0])
            pairs.append((i, others[cands[0]]))
    return pairs

def main():
    fi_raw = load_footinsights()
    fi = []
    for r, rule, versions in fi_raw:
        d = r["kickoff_utc"][:10]
        if d > "2026-09-28":
            continue
        fi.append({
            "date": d,
            "kickoff": r["kickoff_utc"],
            "league": r["league"],
            "home": r["home_team"],
            "away": r["away_team"],
            "hg": int(r["home_goals"]),
            "ag": int(r["away_goals"]),
            "p_raw": (float(r["p_home"]), float(r["p_draw"]), float(r["p_away"])),
            "p_over25": float(r["p_over25"]) if r["p_over25"] else None,
            "p_btts": float(r["p_btts"]) if r["p_btts"] else None,
            "model_version": r["model_version"],
            "model_rule": rule,
            "versions_present": "|".join(versions),
            "url": r["match_url"],
            "settled_at": r["settled_at"],
        })
    fs = load_foresportia()
    print(f"FI fixtures {len(fi)} FS window {len(fs)}")

    # index FS
    pairs = join_on(
        [{**x, "date": x["date"]} for x in fi],
        fs,
        date_slack=0,  # same calendar date; FS date is match date
    )
    # join_on compares hg/ag and names. date slack 0.
    print(f"FI-FS matched {len(pairs)}")

    matched = []
    for i, o in pairs:
        b = fi[i]
        p_fi = norm_probs(b["p_raw"])
        p_fs = norm_probs(o["p"])
        if p_fi is None or p_fs is None:
            continue
        y = y_of(b["hg"], b["ag"])
        matched.append({
            "date": b["date"],
            "kickoff": b["kickoff"],
            "league_fi": b["league"],
            "league_fs": o["league"],
            "home": b["home"],
            "away": b["away"],
            "home_fs": o["home"],
            "away_fs": o["away"],
            "hg": b["hg"], "ag": b["ag"], "y": y,
            "result": LABELS[y],
            "fi": p_fi, "fs": p_fs,
            "fi_over25": b["p_over25"], "fi_btts": b["p_btts"],
            "fi_version": b["model_version"],
            "fi_rule": b["model_rule"],
            "fi_versions_present": b["versions_present"],
            "fi_url": b["url"],
            "fs_stability": o["stability"],
            "fs_version": o["version"],
        })
    matched.sort(key=lambda r: (r["kickoff"], r["home"]))
    print(f"usable {len(matched)} dates {matched[0]['date']} -> {matched[-1]['date']}")
    susp = []
    for m in matched:
        if canon(m["home"]) != canon(m["home_fs"]) or canon(m["away"]) != canon(m["away_fs"]):
            if canon(m["home"]) not in canon(m["home_fs"]) and canon(m["home_fs"]) not in canon(m["home"]):
                susp.append((m["date"], m["home"], m["home_fs"], m["away"], m["away_fs"], m["hg"], m["ag"], m["league_fi"]))
    print(f"name-canon mismatches (not substring) {len(susp)}")
    for row in susp[:25]:
        print("  SUSP", row)

    # statarea
    st = parse_statarea_files()
    print(f"statarea finished matches parsed {len(st)}")
    # integrity: pick accuracy
    if st:
        cor = 0
        for s in st:
            p = norm_probs(s["p"])
            if argmax(p) == y_of(s["hg"], s["ag"]):
                cor += 1
        print(f"statarea raw pick acc {cor}/{len(st)} = {cor/len(st):.3f}")

    st_pairs = join_on(
        [{"date": m["date"], "home": m["home"], "away": m["away"], "hg": m["hg"], "ag": m["ag"]} for m in matched],
        st,
        date_slack=1,
    )
    st_by_i = {i: o for i, o in st_pairs}
    print(f"three-source matches (FI+FS+Statarea) {len(st_by_i)}")

    odds = load_odds()
    print(f"odds rows {len(odds)}")
    od_pairs = join_on(
        [{"date": m["date"], "home": m["home"], "away": m["away"], "hg": m["hg"], "ag": m["ag"]} for m in matched],
        odds,
        date_slack=1,
    )
    od_by_i = {i: o for i, o in od_pairs}
    print(f"odds attached {len(od_by_i)}")

    # build consensus fields
    records = []
    for i, m in enumerate(matched):
        sources = [("footinsights", m["fi"]), ("foresportia", m["fs"])]
        st_row = st_by_i.get(i)
        if st_row:
            sources.append(("statarea", norm_probs(st_row["p"])))
        # two-source consensus ALWAYS computed from FI+FS (primary, full sample)
        cons2 = [ (m["fi"][k] + m["fs"][k]) / 2 for k in range(3) ]
        cons2 = norm_probs(cons2)
        pick2 = argmax(cons2)
        p1, p2p = m["fi"], m["fs"]
        # dispersion on consensus pick
        if pick2 is not None:
            vals = [p1[pick2], p2p[pick2]]
            rng = (max(vals) - min(vals)) * 100
            sd = (stdev(vals) * 100) if len(vals) > 1 else 0.0
        else:
            rng, sd = None, None
        picks = [argmax(p1), argmax(p2p)]
        agree2 = picks[0] is not None and picks[0] == picks[1]
        # margin
        ordered = sorted(cons2, reverse=True)
        margin = ordered[0] - ordered[1]
        od = od_by_i.get(i)
        # prefer pinnacle CLOSE, else bet365 CLOSE, else avg CLOSE
        odds_used = None
        odds_src = None
        if od:
            for label, key in (
                ("pinnacle_close", "pinnacle_close"),
                ("bet365_close", "b365c"),
                ("market_avg_close", "avg_close"),
                ("pinnacle_listed", "pinnacle"),
                ("bet365_listed", "b365"),
                ("market_avg_listed", "avg"),
            ):
                trip = od[key]
                if fair_probs(trip):
                    odds_used = trip
                    odds_src = label
                    break
        fair = fair_probs(odds_used)[0] if odds_used else None
        overround = fair_probs(odds_used)[1] if odds_used else None
        edge = None
        if fair and pick2 is not None:
            edge = cons2[pick2] - fair[pick2]
        rec = {
            **{k: m[k] for k in (
                "date","kickoff","league_fi","league_fs","home","away","home_fs","away_fs",
                "hg","ag","result","fi_version","fi_rule","fi_versions_present","fi_url",
                "fs_stability","fs_version","fi_over25","fi_btts")},
            "fi_p1": m["fi"][0], "fi_px": m["fi"][1], "fi_p2": m["fi"][2],
            "fi_pick": LABELS[picks[0]] if picks[0] is not None else "TIE",
            "fs_p1": m["fs"][0], "fs_px": m["fs"][1], "fs_p2": m["fs"][2],
            "fs_pick": LABELS[picks[1]] if picks[1] is not None else "TIE",
            "cons_p1": cons2[0], "cons_px": cons2[1], "cons_p2": cons2[2],
            "cons_pick": LABELS[pick2] if pick2 is not None else "TIE",
            "cons_max": max(cons2),
            "cons_margin": margin,
            "src_range_pp": rng,
            "src_std_pp": sd,
            "agree_2of2": int(agree2),
            "fi_correct": int(picks[0] == m["y"]) if picks[0] is not None else 0,
            "fs_correct": int(picks[1] == m["y"]) if picks[1] is not None else 0,
            "cons_correct": int(pick2 == m["y"]) if pick2 is not None else 0,
            "fi_brier": brier(m["fi"], m["y"]),
            "fs_brier": brier(m["fs"], m["y"]),
            "cons_brier": brier(cons2, m["y"]),
            "fi_logloss": logloss(m["fi"], m["y"]),
            "fs_logloss": logloss(m["fs"], m["y"]),
            "cons_logloss": logloss(cons2, m["y"]),
            "n_sources_primary": 2,
            "st_p1": None, "st_px": None, "st_p2": None, "st_pick": None,
            "st_league": None, "st_over25": None, "st_btts": None,
            "odds_src": odds_src,
            "odds_1": odds_used[0] if odds_used else None,
            "odds_x": odds_used[1] if odds_used else None,
            "odds_2": odds_used[2] if odds_used else None,
            "fair_1": fair[0] if fair else None,
            "fair_x": fair[1] if fair else None,
            "fair_2": fair[2] if fair else None,
            "overround_sum_inv": overround,
            "edge_pick": edge,
            "ou_odds_over": (od or {}).get("ou_b365c", (None, None))[0] if od else None,
            "ou_odds_under": (od or {}).get("ou_b365c", (None, None))[1] if od else None,
        }
        if st_row:
            pst = norm_probs(st_row["p"])
            rec["st_p1"], rec["st_px"], rec["st_p2"] = pst
            sp = argmax(pst)
            rec["st_pick"] = LABELS[sp] if sp is not None else "TIE"
            rec["st_league"] = st_row["league"]
            rec["st_over25"] = st_row["p_over25"] / 100.0
            rec["st_btts"] = st_row["p_btts"] / 100.0
            # 3-way consensus
            c3 = [(m["fi"][k] + m["fs"][k] + pst[k]) / 3 for k in range(3)]
            c3 = norm_probs(c3)
            pk = argmax(c3)
            rec["cons3_p1"], rec["cons3_px"], rec["cons3_p2"] = c3
            rec["cons3_pick"] = LABELS[pk] if pk is not None else "TIE"
            rec["cons3_max"] = max(c3)
            rec["cons3_correct"] = int(pk == m["y"]) if pk is not None else 0
            rec["cons3_brier"] = brier(c3, m["y"])
            rec["cons3_logloss"] = logloss(c3, m["y"])
            rec["st_correct"] = int(sp == m["y"]) if sp is not None else 0
            rec["st_brier"] = brier(pst, m["y"])
            rec["st_logloss"] = logloss(pst, m["y"])
            three_picks = [picks[0], picks[1], sp]
            rec["agree_3of3"] = int(len(set(three_picks)) == 1 and three_picks[0] is not None)
            vals3 = [m["fi"][pk], m["fs"][pk], pst[pk]] if pk is not None else None
            rec["src3_range_pp"] = (max(vals3)-min(vals3))*100 if vals3 else None
            rec["src3_std_pp"] = stdev(vals3)*100 if vals3 else None
        records.append(rec)

    # CSV — stable columns, including 3-source fields that only some rows have
    extra_keys = [
        "st_correct","st_brier","st_logloss",
        "cons3_p1","cons3_px","cons3_p2","cons3_pick","cons3_max","cons3_correct",
        "cons3_brier","cons3_logloss","agree_3of3","src3_range_pp","src3_std_pp",
    ]
    csv_path = os.path.join(OUT, "backtest_matches.csv")
    fields = list(records[0].keys())
    for k in extra_keys:
        if k not in fields:
            fields.append(k)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow(r)
    print("wrote", csv_path, "rows", len(records))

    # ---- metrics primary 2-source ----
    def block(name, subset):
        if not subset:
            return {"name": name, "n": 0}
        ys = [y_of(r["hg"], r["ag"]) for r in subset]
        fi_p = [[r["fi_p1"], r["fi_px"], r["fi_p2"]] for r in subset]
        fs_p = [[r["fs_p1"], r["fs_px"], r["fs_p2"]] for r in subset]
        c_p = [[r["cons_p1"], r["cons_px"], r["cons_p2"]] for r in subset]
        out = {
            "name": name,
            "n": len(subset),
            "date_min": min(r["date"] for r in subset),
            "date_max": max(r["date"] for r in subset),
            "footinsights": summarize_model("footinsights", fi_p, ys),
            "foresportia": summarize_model("foresportia", fs_p, ys),
            "consensus": summarize_model("consensus", c_p, ys),
        }
        # agreement
        both = [r for r in subset if r["agree_2of2"] == 1]
        out["agree_n"] = len(both)
        out["agree_acc"] = mean(r["cons_correct"] for r in both) if both else None
        # prob filters
        filters = {}
        for thr in (0.50, 0.55, 0.60, 0.65, 0.70, 0.75):
            sub = [r for r in subset if r["cons_max"] + 1e-12 >= thr and r["cons_pick"] != "TIE"]
            filters[f">={thr:.2f}"] = {
                "n": len(sub),
                "correct": sum(r["cons_correct"] for r in sub),
                "wrong": len(sub) - sum(r["cons_correct"] for r in sub),
                "hit": (sum(r["cons_correct"] for r in sub) / len(sub)) if sub else None,
            }
        out["filters"] = filters
        # dispersion cumulative on range pp
        disp = {}
        for thr, label in ((3, "<=3"), (5, "<=5"), (10, "<=10")):
            sub = [r for r in subset if r["src_range_pp"] is not None and r["src_range_pp"] <= thr + 1e-9]
            disp[label] = {
                "n": len(sub),
                "hit": (mean(r["cons_correct"] for r in sub) if sub else None),
                "brier": (mean(r["cons_brier"] for r in sub) if sub else None),
            }
        sub = [r for r in subset if r["src_range_pp"] is not None and r["src_range_pp"] > 10]
        disp[">10"] = {
            "n": len(sub),
            "hit": (mean(r["cons_correct"] for r in sub) if sub else None),
            "brier": (mean(r["cons_brier"] for r in sub) if sub else None),
        }
        # disjoint too
        bands = [(0, 3, "0-3"), (3, 5, "3-5"), (5, 10, "5-10"), (10, 1e9, ">10")]
        disp_d = {}
        for a, b, lab in bands:
            sub = [r for r in subset if r["src_range_pp"] is not None and a < r["src_range_pp"] <= b or (a == 0 and r["src_range_pp"] is not None and r["src_range_pp"] <= b and a == 0)]
            # fix logic
        disp_d = {}
        for a, b, lab in ((None, 3, "<=3"), (3, 5, "3-5"), (5, 10, "5-10"), (10, None, ">10")):
            def ok(v):
                if v is None: return False
                if a is None and b is not None: return v <= b
                if b is None: return v > a
                return a < v <= b
            sub = [r for r in subset if ok(r["src_range_pp"])]
            disp_d[lab] = {"n": len(sub), "hit": mean(r["cons_correct"] for r in sub) if sub else None,
                           "brier": mean(r["cons_brier"] for r in sub) if sub else None}
        out["dispersion_cumulative"] = disp
        out["dispersion_disjoint"] = disp_d
        # calibration of consensus pick probability
        bins = [(0, 0.45, "<45"), (0.45, 0.50, "45-49.99"), (0.50, 0.55, "50-54.99"),
                (0.55, 0.60, "55-59.99"), (0.60, 0.65, "60-64.99"), (0.65, 0.70, "65-69.99"),
                (0.70, 0.75, "70-74.99"), (0.75, 0.80, "75-79.99"), (0.80, 1.01, "80+")]
        cal = []
        for a, b, lab in bins:
            sub = [r for r in subset if a <= r["cons_max"] < b and r["cons_pick"] != "TIE"]
            if not sub:
                cal.append({"bin": lab, "n": 0})
                continue
            cal.append({
                "bin": lab,
                "n": len(sub),
                "mean_p": mean(r["cons_max"] for r in sub),
                "hit": mean(r["cons_correct"] for r in sub),
                "gap": mean(r["cons_correct"] for r in sub) - mean(r["cons_max"] for r in sub),
            })
        out["calibration_pick"] = cal
        # outcome-level calibration (each match contributes the prob of the true class? better: all 3 outcomes)
        # bin the predicted prob of each outcome and see frequency that outcome happened
        obins = [(0.0,0.20,"0-20"), (0.20,0.30,"20-30"), (0.30,0.40,"30-40"), (0.40,0.50,"40-50"),
                 (0.50,0.60,"50-60"), (0.60,0.70,"60-70"), (0.70,1.01,"70+")]
        oc = []
        triples = []
        for r in subset:
            ps = [r["cons_p1"], r["cons_px"], r["cons_p2"]]
            y = y_of(r["hg"], r["ag"])
            for k in range(3):
                triples.append((ps[k], 1 if k == y else 0))
        for a, b, lab in obins:
            sub = [(p, h) for p, h in triples if a <= p < b]
            if not sub:
                oc.append({"bin": lab, "n": 0})
                continue
            oc.append({"bin": lab, "n": len(sub), "mean_p": mean(p for p,_ in sub), "freq": mean(h for _,h in sub),
                        "gap": mean(h for _,h in sub) - mean(p for p,_ in sub)})
        out["calibration_outcomes"] = oc
        return out

    primary = block("all_overlap_fi_fs", records)
    # minimal window to reach >=100: walk dates backward
    by_date = Counter(r["date"] for r in records)
    dates_sorted = sorted(by_date)
    running = 0
    cut = None
    for d in reversed(dates_sorted):
        running += by_date[d]
        if running >= 100:
            cut = d
            break
    window100 = [r for r in records if r["date"] >= cut]
    target = [r for r in records if r["date"] >= "2026-09-22"]
    b_target = block("2026-09-22_to_28", target)
    b_100 = block(f"from_{cut}_to_2026-09-28", window100)

    # correlations
    corr = {
        "n": len(records),
        "home": pearson([r["fi_p1"] for r in records], [r["fs_p1"] for r in records]),
        "draw": pearson([r["fi_px"] for r in records], [r["fs_px"] for r in records]),
        "away": pearson([r["fi_p2"] for r in records], [r["fs_p2"] for r in records]),
        "identical_picks": mean(r["agree_2of2"] for r in records),
    }

    # weighted consensus, time split: first 70% train by count, last 30% validation
    n = len(records)
    # split on date boundary so no same-day leakage across split if possible
    split_at = int(n * 0.70)
    # move split to a date boundary
    split_date = records[split_at]["date"]
    train = [r for r in records if r["date"] < split_date]
    val = [r for r in records if r["date"] >= split_date]
    def mean_brier(key, subset):
        return mean(r[key] for r in subset)
    b_fi = mean_brier("fi_brier", train)
    b_fs = mean_brier("fs_brier", train)
    # inverse brier weights
    inv = [1/b_fi, 1/b_fs]
    w_fi, w_fs = inv[0]/sum(inv), inv[1]/sum(inv)
    # also inverse logloss
    l_fi = mean(r["fi_logloss"] for r in train)
    l_fs = mean(r["fs_logloss"] for r in train)
    invl = [1/l_fi, 1/l_fs]
    wl_fi, wl_fs = invl[0]/sum(invl), invl[1]/sum(invl)

    def eval_weighted(subset, wa, wb):
        probs = []
        ys = []
        for r in subset:
            p = norm_probs([wa*r["fi_p1"]+wb*r["fs_p1"], wa*r["fi_px"]+wb*r["fs_px"], wa*r["fi_p2"]+wb*r["fs_p2"]])
            probs.append(p)
            ys.append(y_of(r["hg"], r["ag"]))
        return summarize_model("weighted", probs, ys)

    best_train = "footinsights" if b_fi <= b_fs else "foresportia"
    val_stats = {
        "split_date": split_date,
        "train_n": len(train),
        "train_dates": (train[0]["date"], train[-1]["date"]) if train else None,
        "val_n": len(val),
        "val_dates": (val[0]["date"], val[-1]["date"]) if val else None,
        "train_brier": {"footinsights": b_fi, "foresportia": b_fs},
        "train_logloss": {"footinsights": l_fi, "foresportia": l_fs},
        "weights_inv_brier": {"footinsights": w_fi, "foresportia": w_fs},
        "weights_inv_logloss": {"footinsights": wl_fi, "foresportia": wl_fs},
        "best_single_by_train_brier": best_train,
        "val_footinsights": summarize_model("fi", [[r["fi_p1"],r["fi_px"],r["fi_p2"]] for r in val], [y_of(r["hg"],r["ag"]) for r in val]),
        "val_foresportia": summarize_model("fs", [[r["fs_p1"],r["fs_px"],r["fs_p2"]] for r in val], [y_of(r["hg"],r["ag"]) for r in val]),
        "val_simple": summarize_model("simple", [[r["cons_p1"],r["cons_px"],r["cons_p2"]] for r in val], [y_of(r["hg"],r["ag"]) for r in val]),
        "val_weighted_brier": eval_weighted(val, w_fi, w_fs),
        "val_weighted_logloss": eval_weighted(val, wl_fi, wl_fs),
    }

    # value betting on records with odds, chronological
    bet_rows = [r for r in records if r["edge_pick"] is not None and r["cons_pick"] in ("1","X","2")]
    def simulate(threshold):
        bets = []
        for r in bet_rows:
            if r["edge_pick"] + 1e-15 < threshold:
                continue
            # odds of the pick
            odds_map = {"1": r["odds_1"], "X": r["odds_x"], "2": r["odds_2"]}
            o = odds_map[r["cons_pick"]]
            if o is None or o <= 1:
                continue
            win = r["cons_correct"] == 1
            pnl = (o - 1.0) if win else -1.0
            bets.append({"date": r["date"], "pnl": pnl, "odds": o, "win": win, "edge": r["edge_pick"], "pick": r["cons_pick"]})
        if not bets:
            return {"threshold": threshold, "n": 0}
        pnls = [b["pnl"] for b in bets]
        mdd, streak = max_dd_and_streak(pnls)
        profit = sum(pnls)
        return {
            "threshold": threshold,
            "n": len(bets),
            "wins": sum(b["win"] for b in bets),
            "losses": len(bets) - sum(b["win"] for b in bets),
            "hit": mean(b["win"] for b in bets),
            "avg_odds": mean(b["odds"] for b in bets),
            "profit": profit,
            "roi": profit / len(bets),
            "yield": profit / len(bets),
            "max_drawdown": mdd,
            "max_losing_streak": streak,
            "odds_sources": dict(Counter(r["odds_src"] for r in bet_rows)),
        }
    value = {f"{int(t*100)}pp": simulate(t) for t in (0.0, 0.03, 0.05, 0.08, 0.10, 0.15)}
    # also edge > 0 is same as >= 0 for continuous, but user said >0. Use strict >0
    value[">0"] = simulate(1e-12)

    # three source subset metrics
    three = [r for r in records if r.get("st_p1") is not None]
    three_block = None
    if three:
        ys = [y_of(r["hg"], r["ag"]) for r in three]
        three_block = {
            "n": len(three),
            "dates": (min(r["date"] for r in three), max(r["date"] for r in three)),
            "footinsights": summarize_model("fi", [[r["fi_p1"],r["fi_px"],r["fi_p2"]] for r in three], ys),
            "foresportia": summarize_model("fs", [[r["fs_p1"],r["fs_px"],r["fs_p2"]] for r in three], ys),
            "statarea": summarize_model("st", [[r["st_p1"],r["st_px"],r["st_p2"]] for r in three], ys),
            "cons2": summarize_model("c2", [[r["cons_p1"],r["cons_px"],r["cons_p2"]] for r in three], ys),
            "cons3": summarize_model("c3", [[r["cons3_p1"],r["cons3_px"],r["cons3_p2"]] for r in three], ys),
        }
        agree3 = [r for r in three if r.get("agree_3of3") == 1]
        three_block["agree3_n"] = len(agree3)
        three_block["agree3_hit"] = mean(r["cons3_correct"] for r in agree3) if agree3 else None
        # filters on cons3
        fl = {}
        for thr in (0.55, 0.60, 0.65, 0.70):
            sub = [r for r in agree3 if r["cons3_max"] >= thr]
            fl[f"3/3 & >={thr:.2f}"] = {"n": len(sub), "hit": mean(r["cons3_correct"] for r in sub) if sub else None,
                                        "correct": sum(r["cons3_correct"] for r in sub)}
        three_block["agree_filters"] = fl
        # dispersion 3
        disp = {}
        for a, b, lab in ((None, 3, "<=3"), (None, 5, "<=5"), (None, 10, "<=10"), (10, None, ">10")):
            def ok(v):
                if v is None: return False
                if b is None: return v > a
                return v <= b
            sub = [r for r in three if ok(r.get("src3_range_pp"))]
            disp[lab] = {"n": len(sub), "hit": mean(r["cons3_correct"] for r in sub) if sub else None}
        three_block["dispersion"] = disp

    # coverage bias: PL matches in football-data vs in sample
    e0 = [r for r in odds if r["div"] == "E0"]
    pl_in_sample = [r for r in records if r["league_fi"] == "Premier League"]
    coverage = {
        "football_data_E0_in_window": len(e0),
        "premier_league_in_fi_fs_overlap": len(pl_in_sample),
        "by_league_fi": dict(Counter(r["league_fi"] for r in records).most_common()),
        "n_with_odds": len(od_by_i),
        "odds_src_counts": dict(Counter(r["odds_src"] for r in records if r["odds_src"])),
    }

    # foresportia not-post-hoc check: among matches FS got wrong, mean p of actual winner vs favorite
    wrong = [r for r in records if r["fs_correct"] == 0]
    # probability FS assigned to the true outcome
    def p_true(r, prefix):
        return {"1": r[f"{prefix}_p1"], "X": r[f"{prefix}_px"], "2": r[f"{prefix}_p2"]}[r["result"]]
    leak_check = {
        "fs_misses": len(wrong),
        "fs_mean_p_on_true_when_miss": mean(p_true(r, "fs") for r in wrong) if wrong else None,
        "fs_mean_max_when_miss": mean(max(r["fs_p1"], r["fs_px"], r["fs_p2"]) for r in wrong) if wrong else None,
        "fi_misses": sum(1 for r in records if r["fi_correct"]==0),
        "share_fs_p_true_gt_0_9": mean(1 if p_true(r,"fs")>0.9 else 0 for r in records),
    }

    report = {
        "primary": primary,
        "target_window": b_target,
        "n100_cut_date": cut,
        "n100_window": b_100,
        "correlation": corr,
        "weighted": val_stats,
        "value": value,
        "three_source": three_block,
        "coverage": coverage,
        "leak_check": leak_check,
        "n_records": len(records),
        "statarea_parsed": len(st),
        "sources": {
            "footinsights": "https://footinsights.com/data/predictions.csv",
            "foresportia": "https://www.foresportia.com/api/past_results.php",
            "statarea": "https://www.statarea.com/olddevices/prediction/YYYY-MM-DD/",
            "odds": "https://www.football-data.co.uk/mmz4281/2627/",
        },
    }
    json.dump(report, open(os.path.join(OUT, "metrics.json"), "w"), indent=2, default=float)
    print("metrics written")
    # print key lines
    def brief(b):
        if not b or b.get("n",0)==0:
            print(b.get("name") if b else None, "empty")
            return
        print(f"\n== {b['name']} n={b['n']} {b['date_min']}..{b['date_max']}")
        for k in ("footinsights","foresportia","consensus"):
            s=b[k]
            print(f"  {k:12} acc={s['accuracy']:.4f} ({s['correct']}/{s['n']}) brier={s['brier']:.4f} logloss={s['logloss']:.4f}")
        print("  agree", b["agree_n"], "acc", b["agree_acc"])
        for k,v in b["filters"].items():
            print(f"  filter {k}: n={v['n']} hit={v['hit']}")
        print("  disp disjoint", b["dispersion_disjoint"])
    brief(primary)
    brief(b_target)
    brief(b_100)
    print("\nCORR", corr)
    print("\nWEIGHTED", json.dumps(val_stats, indent=2, default=float)[:2000])
    print("\nVALUE")
    for k,v in value.items():
        print(k, v)
    print("\nTHREE", json.dumps(three_block, indent=2, default=float)[:2500] if three_block else None)
    print("\nCOVERAGE PL", coverage["football_data_E0_in_window"], coverage["premier_league_in_fi_fs_overlap"], "odds", coverage["n_with_odds"], coverage["odds_src_counts"])
    print("LEAK", leak_check)
    print("top leagues", list(coverage["by_league_fi"].items())[:15])

if __name__ == "__main__":
    main()
