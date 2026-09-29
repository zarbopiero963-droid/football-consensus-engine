#!/usr/bin/env python3
"""Falsification backtest: high model probabilities vs Pinnacle closing odds.

Discovery window of the previous study (do not treat as confirmation):
2026-08-16 .. 2026-09-28.
"""
import csv, json, math, os, random, re, unicodedata
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from statistics import mean, median

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "out_falsify")
os.makedirs(OUT, exist_ok=True)
random.seed(42)

DISCOVERY_START = "2026-08-16"

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
    "nottingham forest": "nottingham forest",
    "nott forest": "nottingham forest",
    "wolves": "wolverhampton",
    "wolverhampton wanderers": "wolverhampton",
    "wolverhampton": "wolverhampton",
    "spurs": "tottenham",
    "tottenham hotspur": "tottenham",
    "tottenham": "tottenham",
    "newcastle": "newcastle",
    "newcastle utd": "newcastle",
    "newcastle united": "newcastle",
    "west ham": "west ham",
    "west ham united": "west ham",
    "brighton": "brighton",
    "brighton and hove albion": "brighton",
    "leicester": "leicester",
    "leicester city": "leicester",
    "ipswich": "ipswich",
    "ipswich town": "ipswich",
    "sheffield utd": "sheffield united",
    "sheffield united": "sheffield united",
    "sheff utd": "sheffield united",
    "sheffield weds": "sheffield wednesday",
    "sheffield wednesday": "sheffield wednesday",
    "qpr": "qpr",
    "queens park rangers": "qpr",
    "hull": "hull",
    "hull city": "hull",
    "cardiff": "cardiff",
    "cardiff city": "cardiff",
    "swansea": "swansea",
    "swansea city": "swansea",
    "norwich": "norwich",
    "norwich city": "norwich",
    "preston": "preston",
    "preston north end": "preston",
    "stoke": "stoke",
    "stoke city": "stoke",
    "coventry": "coventry",
    "coventry city": "coventry",
    "bristol city": "bristol city",
    "birmingham": "birmingham",
    "birmingham city": "birmingham",
    "west brom": "west bromwich",
    "west bromwich albion": "west bromwich",
    "west bromwich": "west bromwich",
    "leeds": "leeds",
    "leeds united": "leeds",
    "southampton": "southampton",
    "everton": "everton",
    "fulham": "fulham",
    "brentford": "brentford",
    "bournemouth": "bournemouth",
    "afc bournemouth": "bournemouth",
    "crystal palace": "crystal palace",
    "palace": "crystal palace",
    "aston villa": "aston villa",
    "villa": "aston villa",
    "chelsea": "chelsea",
    "arsenal": "arsenal",
    "liverpool": "liverpool",
    "sunderland": "sunderland",
    "afc sunderland": "sunderland",
    "luton": "luton",
    "luton town": "luton",
    "burnley": "burnley",
    "watford": "watford",
    "middlesbrough": "middlesbrough",
    "millwall": "millwall",
    "blackburn": "blackburn",
    "blackburn rovers": "blackburn",
    "plymouth": "plymouth",
    "plymouth argyle": "plymouth",
    "oxford": "oxford united",
    "oxford utd": "oxford united",
    "oxford united": "oxford united",
    "portsmouth": "portsmouth",
    "derby": "derby",
    "derby county": "derby",
    "sporting cp": "sporting",
    "sporting lisbon": "sporting",
    "sporting": "sporting",
    "fc porto": "porto",
    "porto": "porto",
    "benfica": "benfica",
    "sl benfica": "benfica",
    "braga": "braga",
    "sc braga": "braga",
    "sporting braga": "braga",
    "sporting clube de braga": "braga",
    "guimaraes": "vitoria guimaraes",
    "vitoria guimaraes": "vitoria guimaraes",
    "vitória sc": "vitoria guimaraes",
    "inter": "inter",
    "internazionale": "inter",
    "inter milan": "inter",
    "fc internazionale milano": "inter",
    "ac milan": "milan",
    "milan": "milan",
    "as roma": "roma",
    "roma": "roma",
    "lazio": "lazio",
    "ss lazio": "lazio",
    "juventus": "juventus",
    "napoli": "napoli",
    "ssc napoli": "napoli",
    "atalanta": "atalanta",
    "atalanta bc": "atalanta",
    "fiorentina": "fiorentina",
    "acf fiorentina": "fiorentina",
    "torino": "torino",
    "bologna": "bologna",
    "udinese": "udinese",
    "sassuolo": "sassuolo",
    "us sassuolo": "sassuolo",
    "genoa": "genoa",
    "cagliari": "cagliari",
    "lecce": "lecce",
    "empoli": "empoli",
    "verona": "verona",
    "hellas verona": "verona",
    "como": "como",
    "parma": "parma",
    "venezia": "venezia",
    "monza": "monza",
    "cremonese": "cremonese",
    "pisa": "pisa",
    "ath madrid": "atletico madrid",
    "atletico madrid": "atletico madrid",
    "atl madrid": "atletico madrid",
    "ath bilbao": "athletic bilbao",
    "athletic club": "athletic bilbao",
    "athletic bilbao": "athletic bilbao",
    "sociedad": "real sociedad",
    "real sociedad": "real sociedad",
    "betis": "betis",
    "real betis": "betis",
    "real betis balompie": "betis",
    "espanol": "espanyol",
    "espanyol": "espanyol",
    "rcd espanyol de barcelona": "espanyol",
    "espanyol barcelona": "espanyol",
    "celta": "celta vigo",
    "celta vigo": "celta vigo",
    "rc celta de vigo": "celta vigo",
    "valladolid": "valladolid",
    "real valladolid": "valladolid",
    "vallecano": "rayo vallecano",
    "rayo vallecano": "rayo vallecano",
    "rayo vallecano de madrid": "rayo vallecano",
    "alaves": "alaves",
    "deportivo alaves": "alaves",
    "girona": "girona",
    "getafe": "getafe",
    "osasuna": "osasuna",
    "ca osasuna": "osasuna",
    "mallorca": "mallorca",
    "rcd mallorca": "mallorca",
    "sevilla": "sevilla",
    "villarreal": "villarreal",
    "villarreal cf": "villarreal",
    "real madrid": "real madrid",
    "real madrid cf": "real madrid",
    "barcelona": "barcelona",
    "fc barcelona": "barcelona",
    "bayern munich": "bayern",
    "bayern munchen": "bayern",
    "fc bayern munchen": "bayern",
    "bayern": "bayern",
    "dortmund": "dortmund",
    "borussia dortmund": "dortmund",
    "leverkusen": "leverkusen",
    "bayer leverkusen": "leverkusen",
    "bayer 04 leverkusen": "leverkusen",
    "rb leipzig": "leipzig",
    "leipzig": "leipzig",
    "rasenballsport leipzig": "leipzig",
    "mgladbach": "gladbach",
    "monchengladbach": "gladbach",
    "borussia monchengladbach": "gladbach",
    "gladbach": "gladbach",
    "ein frankfurt": "frankfurt",
    "eintracht frankfurt": "frankfurt",
    "frankfurt": "frankfurt",
    "wolfsburg": "wolfsburg",
    "vfl wolfsburg": "wolfsburg",
    "freiburg": "freiburg",
    "sc freiburg": "freiburg",
    "hoffenheim": "hoffenheim",
    "tsg hoffenheim": "hoffenheim",
    "mainz": "mainz",
    "fsv mainz 05": "mainz",
    "augsburg": "augsburg",
    "fc augsburg": "augsburg",
    "union berlin": "union berlin",
    "1 fc union berlin": "union berlin",
    "fc koln": "koln",
    "koln": "koln",
    "1 fc koln": "koln",
    "cologne": "koln",
    "stuttgart": "stuttgart",
    "vfb stuttgart": "stuttgart",
    "werder bremen": "werder",
    "werder": "werder",
    "bochum": "bochum",
    "vfl bochum": "bochum",
    "heidenheim": "heidenheim",
    "1 fc heidenheim": "heidenheim",
    "darmstadt": "darmstadt",
    "st pauli": "st pauli",
    "fc st pauli": "st pauli",
    "holstein kiel": "holstein kiel",
    "hamburg": "hamburg",
    "hamburger sv": "hamburg",
    "psg": "paris sg",
    "paris saint germain": "paris sg",
    "paris sg": "paris sg",
    "paris saint-germain": "paris sg",
    "marseille": "marseille",
    "olympique de marseille": "marseille",
    "olympique marseille": "marseille",
    "lyon": "lyon",
    "olympique lyonnais": "lyon",
    "olympique lyon": "lyon",
    "monaco": "monaco",
    "as monaco": "monaco",
    "lille": "lille",
    "losc lille": "lille",
    "nice": "nice",
    "ogc nice": "nice",
    "lens": "lens",
    "rc lens": "lens",
    "rennes": "rennes",
    "stade rennais": "rennes",
    "stade rennais fc": "rennes",
    "strasbourg": "strasbourg",
    "rc strasbourg alsace": "strasbourg",
    "nantes": "nantes",
    "fc nantes": "nantes",
    "montpellier": "montpellier",
    "toulouse": "toulouse",
    "toulouse fc": "toulouse",
    "reims": "reims",
    "stade de reims": "reims",
    "brest": "brest",
    "stade brestois": "brest",
    "stade brestois 29": "brest",
    "le havre": "le havre",
    "metz": "metz",
    "fc metz": "metz",
    "auxerre": "auxerre",
    "lorient": "lorient",
    "fc lorient": "lorient",
    "angers": "angers",
    "angers sco": "angers",
    "ajax": "ajax",
    "afc ajax": "ajax",
    "psv": "psv",
    "psv eindhoven": "psv",
    "feyenoord": "feyenoord",
    "az": "az alkmaar",
    "az alkmaar": "az alkmaar",
    "twente": "twente",
    "fc twente": "twente",
    "utrecht": "utrecht",
    "fc utrecht": "utrecht",
    "anderlecht": "anderlecht",
    "rsc anderlecht": "anderlecht",
    "club brugge": "club brugge",
    "brugge": "club brugge",
    "genk": "genk",
    "krc genk": "genk",
    "gent": "gent",
    "kaa gent": "gent",
    "standard": "standard liege",
    "standard liege": "standard liege",
    "union sg": "union sg",
    "royale union saint gilloise": "union sg",
    "antwerp": "antwerp",
    "royal antwerp": "antwerp",
    "galatasaray": "galatasaray",
    "fenerbahce": "fenerbahce",
    "besiktas": "besiktas",
    "trabzonspor": "trabzonspor",
    "basaksehir": "basaksehir",
    "istanbul basaksehir": "basaksehir",
    "olympiacos": "olympiacos",
    "olympiakos": "olympiacos",
    "panathinaikos": "panathinaikos",
    "aek": "aek athens",
    "aek athens": "aek athens",
    "paok": "paok",
    "celtic": "celtic",
    "rangers": "rangers",
    "hearts": "hearts",
    "heart of midlothian": "hearts",
    "hibernian": "hibernian",
    "aberdeen": "aberdeen",
}

def strip_accents(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))

def canon(s):
    s = strip_accents(s).lower().replace("&", " and ").replace("'", "")
    s = s.replace(".", " ")
    s = re.sub(r"\b(fc|cf|sc|ac|afc|ud|cd|sd|fk|sk|sv|rc|as|ss|us|osc|ogc|vfl|vfb|tsg|fsv|bv|rsc|kaa|krc)\b", " ", s)
    s = re.sub(r"\b(de|the|club|calcio)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\b1\b", " ", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return ALIASES.get(s, s)

GENERIC = {
    "city","united","real","sporting","athletic","rovers","town","county","west",
    "east","north","south","inter","atletico","deportivo","dynamo","dinamo",
    "saint","st","hotspur","wanderers","albion","calcio","club","fc",
}

def tokens(s):
    raw = strip_accents(s).lower().replace("'", "")
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    return {t for t in raw.split() if t not in {"fc","cf","sc","ac","afc","ud","cd","the","de","club"} and len(t) > 1}

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
    distinctive = inter - GENERIC
    if distinctive and j >= 0.34 and min(len(ta), len(tb)) <= 3:
        return True
    return False

def name_score(a, b):
    na, nb = canon(a), canon(b)
    if na == nb:
        return 3
    if na in nb or nb in na:
        return 2
    if similar(a, b):
        return 1
    return 0

def parse_date(s):
    s = str(s).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def fnum(v):
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return x if x > 1.0 else None

# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------
MAPPABLE = {
    "PREMIER_LEAGUE","CHAMPIONSHIP","LIGA","LIGA2","SERIEA","SERIEB",
    "BDL","BDL2","LIGUE_1","LIGUE_2","PORT","NL","BEL","TUR","GREECE","SCOTLAND",
}
# E2/E3/SC1/F2/D2/I2/SP2 included via league codes above where FS has them.
# PORT2, BRE, MLS etc. have no football-data file in this pull.

def load_fs():
    rows = json.load(open(os.path.join(ROOT, "raw/foresportia_by_league.json"), encoding="utf-8"))
    extra_path = os.path.join(ROOT, "raw/foresportia_extra.json")
    if os.path.exists(extra_path):
        rows = rows + json.load(open(extra_path, encoding="utf-8"))
    out = []
    seen = set()
    for r in rows:
        d = str(r.get("date match") or "")[:10]
        if not d or d < "2023-01-01" or d > "2026-09-28":
            continue
        league = r.get("Ligue")
        try:
            hg = int(float(r["score dom"]))
            ag = int(float(r["score ext"]))
            p1 = float(r["Victoire domicile"])
            px = float(r["Match nul"])
            p2 = float(r["Victoire extérieure"])
        except (TypeError, ValueError, KeyError):
            continue
        if min(p1, px, p2) < 0 or (p1+px+p2) <= 0:
            continue
        home, away = r.get("Équipe domicile"), r.get("Équipe visiteuse")
        key = (d, canon(home), canon(away), hg, ag)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "date": d, "league": league, "home": home, "away": away,
            "hg": hg, "ag": ag, "p": (p1, px, p2),
            "stability": r.get("stability_badge"),
            "version": r.get("stability_version"),
            "mappable": league in MAPPABLE or league in {"LIGA2","SERIEB","BDL2","LIGUE_2"},
        })
    return out

def load_fd():
    rows = []
    folder = os.path.join(ROOT, "raw/fd_hist")
    for fn in sorted(os.listdir(folder)):
        if not fn.endswith(".csv"):
            continue
        season, div = fn.replace(".csv","").split("_", 1)
        with open(os.path.join(folder, fn), newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if not r.get("HomeTeam") or r.get("FTHG") in (None, ""):
                    continue
                d = parse_date(r.get("Date") or "")
                if d is None:
                    continue
                try:
                    hg, ag = int(r["FTHG"]), int(r["FTAG"])
                except ValueError:
                    continue
                # closing pinnacle: PSCH (older) or PPCH (2627)
                close = None
                pre = None
                for ck, pk in (
                    (("PSCH","PSCD","PSCA"), ("PSH","PSD","PSA")),
                    (("PPCH","PPCD","PPCA"), ("PPH","PPD","PPA")),
                ):
                    c = tuple(fnum(r.get(k)) for k in ck)
                    p = tuple(fnum(r.get(k)) for k in pk)
                    if all(c):
                        close = c
                        if all(p):
                            pre = p
                        break
                if close is None:
                    continue
                rows.append({
                    "date": d.isoformat(),
                    "season": season,
                    "div": div,
                    "home": r["HomeTeam"],
                    "away": r["AwayTeam"],
                    "hg": hg, "ag": ag,
                    "close": close,
                    "pre": pre,
                })
    return rows

def load_fi():
    path = os.path.join(ROOT, "raw/footinsights_predictions.csv")
    groups = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["market"] != "1x2" or r["home_goals"] == "":
                continue
            groups[(r["kickoff_utc"][:10], r["home_team"], r["away_team"])].append(r)
    out = []
    for (d, h, a), rs in groups.items():
        # earliest settled row = first graded published forecast
        r = sorted(rs, key=lambda x: x["settled_at"])[0]
        try:
            p = (float(r["p_home"]), float(r["p_draw"]), float(r["p_away"]))
            hg, ag = int(r["home_goals"]), int(r["away_goals"])
        except ValueError:
            continue
        out.append({
            "date": d, "league": r["league"], "home": h, "away": a,
            "hg": hg, "ag": ag, "p": p, "url": r["match_url"],
            "model": r["model_version"],
        })
    return out

# ---------------------------------------------------------------------------
# maths
# ---------------------------------------------------------------------------
def norm_probs(p):
    p = [float(x) for x in p]
    if max(p) > 1.5:
        p = [x/100.0 for x in p]
    s = sum(p)
    if s <= 0:
        return None
    return [x/s for x in p]

def y_of(hg, ag):
    return 0 if hg > ag else (1 if hg == ag else 2)

def argmax(p):
    m = max(p)
    idxs = [i for i,v in enumerate(p) if abs(v-m) < 1e-12]
    return idxs[0] if len(idxs)==1 else None

def fair(odds):
    if any(o is None or o <= 1 for o in odds):
        return None
    inv = [1.0/o for o in odds]
    s = sum(inv)
    return [x/s for x in inv], s

def brier(p, y):
    return sum((p[i] - (1.0 if i==y else 0.0))**2 for i in range(3))

def logloss(p, y):
    return -math.log(max(p[y], 1e-15))

def wilson(k, n, z=1.96):
    if n <= 0:
        return (None, None)
    ph = k/n
    den = 1 + z*z/n
    centre = ph + z*z/(2*n)
    marg = z*math.sqrt(ph*(1-ph)/n + z*z/(4*n*n))
    return ((centre-marg)/den, (centre+marg)/den)

def max_dd_streak(pnls):
    peak = equity = 0.0
    mdd = 0.0
    streak = max_streak = 0
    for x in pnls:
        equity += x
        peak = max(peak, equity)
        mdd = max(mdd, peak-equity)
        if x < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return mdd, max_streak

def boot_roi(pnls, nboot=2000):
    n = len(pnls)
    if n == 0:
        return None
    rng = random.Random(42)
    stats = []
    for _ in range(nboot):
        s = 0.0
        for __ in range(n):
            s += pnls[rng.randrange(n)]
        stats.append(s/n)
    stats.sort()
    lo = stats[int(0.025*(nboot-1))]
    hi = stats[int(0.975*(nboot-1))]
    return {
        "n": n,
        "roi": sum(pnls)/n,
        "ci95": [lo, hi],
        "frac_boot_roi_gt_0": sum(1 for x in stats if x > 0)/nboot,
    }

def boot_mean(xs, nboot=2000):
    n = len(xs)
    if n == 0:
        return None
    rng = random.Random(42)
    stats = []
    for _ in range(nboot):
        s = 0.0
        for __ in range(n):
            s += xs[rng.randrange(n)]
        stats.append(s/n)
    stats.sort()
    return {
        "mean": sum(xs)/n,
        "ci95": [stats[int(0.025*(nboot-1))], stats[int(0.975*(nboot-1))]],
        "frac_gt_0": sum(1 for x in stats if x > 0)/nboot,
    }

PROB_BINS = [
    (0.00, 0.45, "<45"),
    (0.45, 0.50, "45-49.99"),
    (0.50, 0.55, "50-54.99"),
    (0.55, 0.60, "55-59.99"),
    (0.60, 0.65, "60-64.99"),
    (0.65, 0.70, "65-69.99"),
    (0.70, 0.75, "70-74.99"),
    (0.75, 0.80, "75-79.99"),
    (0.80, 0.85, "80-84.99"),
    (0.85, 1.01, "85+"),
]
ODDS_BINS = [
    (1.01, 1.15, "1.01-1.15"),
    (1.16, 1.25, "1.16-1.25"),
    (1.26, 1.35, "1.26-1.35"),
    (1.36, 1.45, "1.36-1.45"),
    (1.46, 1.60, "1.46-1.60"),
    (1.61, 1.80, "1.61-1.80"),
    (1.81, 2.00, "1.81-2.00"),
    (2.01, 2.50, "2.01-2.50"),
    (2.51, 3.00, "2.51-3.00"),
    (3.01, 99, ">3.00"),
]
P_MATRIX = [b for b in PROB_BINS if b[0] >= 0.50]
FAV_ODDS = [
    (1.01, 1.20, "<1.20"),
    (1.20, 1.30, "1.20-1.29"),
    (1.30, 1.40, "1.30-1.39"),
    (1.40, 1.50, "1.40-1.49"),
    (1.50, 1.60, "1.50-1.59"),
    (1.60, 1.80, "1.60-1.79"),
    (1.80, 2.00, "1.80-1.99"),
    (2.00, 99, "2.00+"),
]
EDGE_BINS = [
    (-9, -0.10, "<-10pp"),
    (-0.10, -0.05, "-10/-5"),
    (-0.05, 0.0, "-5/0"),
    (0.0, 0.03, "0/+3"),
    (0.03, 0.05, "+3/+5"),
    (0.05, 0.08, "+5/+8"),
    (0.08, 0.10, "+8/+10"),
    (0.10, 0.15, "+10/+15"),
    (0.15, 9, ">+15"),
]

def in_bin(v, a, b):
    # last bins use inclusive top via b>1 or b>3
    return a <= v < b or (b > 50 and v >= a)

def pnl_of(win, odds):
    return (odds - 1.0) if win else -1.0

def summarize_bets(rows, pick_key="model_pick", p_key="model_p", odds_key="picked_odds"):
    """rows are match dicts already filtered. Chronological order assumed."""
    if not rows:
        return {"n": 0}
    wins = 0
    pnls = []
    odds = []
    ps = []
    briers = []
    lls = []
    for r in rows:
        win = r[pick_key] == r["y"]
        wins += int(win)
        o = r[odds_key]
        pnls.append(pnl_of(win, o))
        odds.append(o)
        ps.append(r[p_key])
        # brier/logloss of the full distribution stored
        briers.append(r["brier"])
        lls.append(r["logloss"])
    mdd, streak = max_dd_streak(pnls)
    k = wins
    n = len(rows)
    lo, hi = wilson(k, n)
    return {
        "n": n,
        "wins": k,
        "losses": n-k,
        "hit": k/n,
        "hit_ci95": [lo, hi],
        "mean_p": mean(ps),
        "gap": k/n - mean(ps),
        "mean_odds": mean(odds),
        "median_odds": median(odds),
        "pnl": sum(pnls),
        "roi": sum(pnls)/n,
        "max_dd": mdd,
        "max_losing_streak": streak,
        "brier": mean(briers),
        "logloss": mean(lls),
    }

def ece(rows, p_of, y_of_row):
    # probability bins of the PICKED probability vs hit — pick-level ECE
    total = 0.0
    n = 0
    for a,b,lab in PROB_BINS:
        sub = [r for r in rows if a <= p_of(r) < b]
        if not sub:
            continue
        mp = mean(p_of(r) for r in sub)
        fr = mean(1 if y_of_row(r) else 0 for r in sub)
        total += len(sub) * abs(fr-mp)
        n += len(sub)
    return (total/n) if n else None

# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------
def join_fs_fd(fs, fd):
    by_date = defaultdict(list)
    for j, o in enumerate(fd):
        by_date[o["date"]].append(j)
    used = set()
    pairs = []
    ambiguous = []
    for i, b in enumerate(fs):
        bd = datetime.strptime(b["date"], "%Y-%m-%d").date()
        cands = []
        for delta in (-1, 0, 1):
            key = (bd + timedelta(days=delta)).isoformat()
            for j in by_date.get(key, []):
                o = fd[j]
                if o["hg"] != b["hg"] or o["ag"] != b["ag"]:
                    continue
                hs = name_score(b["home"], o["home"])
                as_ = name_score(b["away"], o["away"])
                if hs > 0 and as_ > 0:
                    cands.append((hs+as_, j))
        if not cands:
            continue
        cands.sort(reverse=True)
        top = cands[0][0]
        best = [j for s,j in cands if s == top]
        # unique FD rows
        best = list(dict.fromkeys(best))
        if len(best) > 1:
            ambiguous.append({
                "date": b["date"], "fs": f"{b['home']} vs {b['away']}",
                "score": f"{b['hg']}-{b['ag']}",
                "cands": [f"{fd[j]['home']} vs {fd[j]['away']} ({fd[j]['div']})" for j in best[:4]],
            })
            continue
        j = best[0]
        if j in used:
            # two FS rows claim the same FD match — skip both later? keep first, flag second
            ambiguous.append({
                "date": b["date"], "fs": f"{b['home']} vs {b['away']}",
                "score": f"{b['hg']}-{b['ag']}",
                "cands": ["FD row already used"],
            })
            continue
        used.add(j)
        pairs.append((i, j, cands[0][0]))
    return pairs, ambiguous

def build_records(fs, fd, pairs, fi_index):
    recs = []
    for i, j, score in pairs:
        b, o = fs[i], fd[j]
        p = norm_probs(b["p"])
        if p is None:
            continue
        y = y_of(b["hg"], b["ag"])
        pick = argmax(p)
        if pick is None:
            continue
        fp, over = fair(o["close"])
        pre_fp = fair(o["pre"])[0] if o["pre"] else None
        fav = argmax(fp)
        odds = o["close"][pick]
        # FI
        fi = fi_index.get((b["date"], canon(b["home"]), canon(b["away"])))
        if fi is None:
            # try date slack already resolved via FD date
            fi = fi_index.get((o["date"], canon(o["home"]), canon(o["away"])))
        fi_p = norm_probs(fi["p"]) if fi else None
        fi_pick = argmax(fi_p) if fi_p else None
        if fi_p and fi_pick is not None:
            cons = norm_probs([(p[k]+fi_p[k])/2 for k in range(3)])
            cons_pick = argmax(cons)
        else:
            cons = None
            cons_pick = None
        edge = p[pick] - fp[pick]
        rec = {
            "date": b["date"],
            "fd_date": o["date"],
            "league": b["league"],
            "div": o["div"],
            "season": o["season"],
            "home": b["home"],
            "away": b["away"],
            "fd_home": o["home"],
            "fd_away": o["away"],
            "hg": b["hg"], "ag": b["ag"],
            "score": f"{b['hg']}-{b['ag']}",
            "y": y,
            "outcome": "1X2"[y] if False else ["1","X","2"][y],
            "fs_p": p,
            "fs_pick": pick,
            "fs_pmax": p[pick],
            "brier": brier(p, y),
            "logloss": logloss(p, y),
            "close": o["close"],
            "pre": o["pre"],
            "fair": fp,
            "overround": over,
            "pre_fair": pre_fp,
            "fav": fav,
            "fav_odds": o["close"][fav] if fav is not None else None,
            "picked_odds": odds,
            "edge": edge,
            "same_as_fav": int(pick == fav) if fav is not None else None,
            "name_score": score,
            "discovery": b["date"] >= DISCOVERY_START,
            "fi_p": fi_p,
            "fi_pick": fi_pick,
            "fi_league": fi["league"] if fi else None,
            "fi_url": fi["url"] if fi else None,
            "cons": cons,
            "cons_pick": cons_pick,
            "stability": b["stability"],
        }
        recs.append(rec)
    recs.sort(key=lambda r: (r["date"], r["home"]))
    return recs

def attach_market_rows(recs):
    """Flatten for CSV and betting helpers."""
    out = []
    for r in recs:
        pick = r["fs_pick"]
        fav = r["fav"]
        win = pick == r["y"]
        fav_win = fav == r["y"] if fav is not None else None
        row = {
            "date": r["date"],
            "league": r["league"],
            "div": r["div"],
            "season": r["season"],
            "home": r["home"],
            "away": r["away"],
            "fd_home": r["fd_home"],
            "fd_away": r["fd_away"],
            "score": r["score"],
            "outcome": r["outcome"],
            "FP_p1": r["fs_p"][0], "FP_px": r["fs_p"][1], "FP_p2": r["fs_p"][2],
            "FP_pick": ["1","X","2"][pick],
            "FP_pmax": r["fs_pmax"],
            "FI_p1": r["fi_p"][0] if r["fi_p"] else "",
            "FI_px": r["fi_p"][1] if r["fi_p"] else "",
            "FI_p2": r["fi_p"][2] if r["fi_p"] else "",
            "FI_pick": ["1","X","2"][r["fi_pick"]] if r["fi_pick"] is not None else "",
            "CONS_p1": r["cons"][0] if r["cons"] else "",
            "CONS_px": r["cons"][1] if r["cons"] else "",
            "CONS_p2": r["cons"][2] if r["cons"] else "",
            "CONS_pick": ["1","X","2"][r["cons_pick"]] if r["cons_pick"] is not None else "",
            "CONS_pmax": max(r["cons"]) if r["cons"] else "",
            "model_pick": ["1","X","2"][pick],
            "model_probability": r["fs_pmax"],
            "pin_odds_1": r["close"][0], "pin_odds_x": r["close"][1], "pin_odds_2": r["close"][2],
            "pin_fair_1": r["fair"][0], "pin_fair_x": r["fair"][1], "pin_fair_2": r["fair"][2],
            "pin_pre_1": r["pre"][0] if r["pre"] else "",
            "pin_pre_x": r["pre"][1] if r["pre"] else "",
            "pin_pre_2": r["pre"][2] if r["pre"] else "",
            "picked_odds": r["picked_odds"],
            "edge": r["edge"],
            "correct": int(win),
            "profit_flat_1": pnl_of(win, r["picked_odds"]),
            "fav_pick": ["1","X","2"][fav] if fav is not None else "",
            "fav_odds": r["fav_odds"] if r["fav_odds"] else "",
            "fav_correct": int(fav_win) if fav_win is not None else "",
            "profit_fav": pnl_of(fav_win, r["fav_odds"]) if fav is not None else "",
            "same_as_fav": r["same_as_fav"],
            "brier_fp": r["brier"],
            "logloss_fp": r["logloss"],
            "discovery_window": int(r["discovery"]),
            "fi_url": r["fi_url"] or "",
            "name_score": r["name_score"],
        }
        if r["cons"] is not None and r["cons_pick"] is not None:
            cw = r["cons_pick"] == r["y"]
            co = r["close"][r["cons_pick"]]
            row["cons_correct"] = int(cw)
            row["cons_picked_odds"] = co
            row["profit_cons"] = pnl_of(cw, co)
            row["cons_edge"] = r["cons"][r["cons_pick"]] - r["fair"][r["cons_pick"]]
            row["cons_brier"] = brier(r["cons"], r["y"])
            row["cons_logloss"] = logloss(r["cons"], r["y"])
        else:
            row["cons_correct"] = ""
            row["cons_picked_odds"] = ""
            row["profit_cons"] = ""
            row["cons_edge"] = ""
            row["cons_brier"] = ""
            row["cons_logloss"] = ""
        if r["fi_p"] is not None and r["fi_pick"] is not None:
            iw = r["fi_pick"] == r["y"]
            io = r["close"][r["fi_pick"]]
            row["fi_correct"] = int(iw)
            row["fi_picked_odds"] = io
            row["profit_fi"] = pnl_of(iw, io)
            row["fi_pmax"] = r["fi_p"][r["fi_pick"]]
            row["fi_brier"] = brier(r["fi_p"], r["y"])
            row["fi_logloss"] = logloss(r["fi_p"], r["y"])
        else:
            row["fi_correct"] = ""
            row["fi_picked_odds"] = ""
            row["profit_fi"] = ""
            row["fi_pmax"] = ""
            row["fi_brier"] = ""
            row["fi_logloss"] = ""
        # line move: fair prob of the MODEL pick, pre-close minus? movement of market toward pick
        # delta = fair_close(pick) - fair_pre(pick). Positive => market moved toward the pick.
        if r["pre_fair"] is not None:
            row["fair_pre_pick"] = r["pre_fair"][pick]
            row["fair_close_pick"] = r["fair"][pick]
            row["move_toward_pick_pp"] = (r["fair"][pick] - r["pre_fair"][pick]) * 100
        else:
            row["fair_pre_pick"] = ""
            row["fair_close_pick"] = ""
            row["move_toward_pick_pp"] = ""
        out.append(row)
    return out

# ---------------------------------------------------------------------------
# analyses on a list of flat rows, using FS as the model unless prefix given
# ---------------------------------------------------------------------------
def as_bet_rows(rows):
    """Normalize to keys used by summarize_bets for Foresportia pick."""
    out = []
    for r in rows:
        out.append({
            "date": r["date"],
            "y": {"1":0,"X":1,"2":2}[r["outcome"]],
            "model_pick": {"1":0,"X":1,"2":2}[r["model_pick"]],
            "model_p": float(r["model_probability"]),
            "picked_odds": float(r["picked_odds"]),
            "brier": float(r["brier_fp"]),
            "logloss": float(r["logloss_fp"]),
            "fair_pick": float(r["pin_fair_1"] if r["model_pick"]=="1" else r["pin_fair_x"] if r["model_pick"]=="X" else r["pin_fair_2"]),
            "edge": float(r["edge"]),
            "div": r["div"],
            "league": r["league"],
            "season": r["season"],
            "same": (int(r["same_as_fav"]) if r["same_as_fav"] not in ("", None) else None),
            "fav_odds": float(r["fav_odds"]) if r["fav_odds"] != "" else None,
            "profit": float(r["profit_flat_1"]),
            "profit_fav": float(r["profit_fav"]) if r["profit_fav"] != "" else None,
            "fav_correct": int(r["fav_correct"]) if r["fav_correct"] != "" else None,
            "move": float(r["move_toward_pick_pp"]) if r["move_toward_pick_pp"] != "" else None,
            "home_pick": r["model_pick"] == "1",
            "away_pick": r["model_pick"] == "2",
        })
    return out

def bucket_table(bets):
    tabs = []
    for a,b,lab in PROB_BINS:
        sub = [r for r in bets if a <= r["model_p"] < b]
        s = summarize_bets(sub)
        s["bin"] = lab
        tabs.append(s)
    return tabs

def matrix(bets):
    cells = []
    for pa,pb,pl in P_MATRIX:
        for oa,ob,ol in ODDS_BINS:
            sub = [r for r in bets if pa <= r["model_p"] < pb and oa <= r["picked_odds"] < ob]
            if not sub:
                cells.append({"p": pl, "odds": ol, "n": 0})
                continue
            s = summarize_bets(sub)
            s["p"] = pl
            s["odds"] = ol
            s["mean_fair"] = mean(r["fair_pick"] for r in sub)
            s["small"] = s["n"] < 30
            cells.append(s)
    return cells

def edge_table(bets):
    tabs = []
    for a,b,lab in EDGE_BINS:
        sub = [r for r in bets if a <= r["edge"] < b]
        s = summarize_bets(sub)
        s["bin"] = lab
        tabs.append(s)
    return tabs

def fav_split(bets):
    same = [r for r in bets if r["same"] == 1]
    diff = [r for r in bets if r["same"] == 0]
    def pack(sub, label):
        s = summarize_bets(sub)
        s["label"] = label
        if sub and all(r["profit_fav"] is not None for r in sub):
            s["baseline_roi"] = mean(r["profit_fav"] for r in sub)
            s["baseline_hit"] = mean(r["fav_correct"] for r in sub)
            s["incremental_roi"] = s["roi"] - s["baseline_roi"]
            paired = [r["profit"]-r["profit_fav"] for r in sub]
            s["paired_mean_pnl_diff"] = mean(paired)
            s["paired_boot"] = boot_mean(paired, nboot=1500)
        return s
    return {"same_favorite": pack(same, "same"), "different": pack(diff, "diff")}

def fav_odds_table(bets):
    # only when model agrees with pinnacle favorite, bucket by favorite odds
    same = [r for r in bets if r["same"] == 1 and r["fav_odds"]]
    tabs = []
    for a,b,lab in FAV_ODDS:
        sub = [r for r in same if a <= r["fav_odds"] < b]
        s = summarize_bets(sub)
        s["bin"] = lab
        if sub:
            s["baseline_roi"] = mean(r["profit_fav"] for r in sub)
            # when same pick, baseline pnl == strategy pnl, incremental ~ 0
            s["incremental_roi"] = s["roi"] - s["baseline_roi"]
        tabs.append(s)
    return tabs

def home_away(bets, predicate):
    sub = [r for r in bets if predicate(r)]
    home = [r for r in sub if r["home_pick"]]
    away = [r for r in sub if r["away_pick"]]
    draw = [r for r in sub if not r["home_pick"] and not r["away_pick"]]
    def pack(x, lab):
        s = summarize_bets(x)
        s["side"] = lab
        if x and x[0]["profit_fav"] is not None:
            s["baseline_roi"] = mean(r["profit_fav"] for r in x)
            s["incremental_roi"] = s["roi"] - s["baseline_roi"]
        return s
    return [pack(home,"home"), pack(away,"away"), pack(draw,"draw"), pack(sub,"all")]

def by_league(bets, min_n=50):
    tabs = []
    for div, n in Counter(r["div"] for r in bets).most_common():
        sub = [r for r in bets if r["div"]==div]
        if len(sub) < min_n:
            continue
        s = summarize_bets(sub)
        s["div"] = div
        s["baseline_roi"] = mean(r["profit_fav"] for r in sub)
        s["baseline_hit"] = mean(r["fav_correct"] for r in sub)
        s["incremental_roi"] = s["roi"] - s["baseline_roi"]
        # calibration gap already in s
        tabs.append(s)
    return tabs

def temporal(bets, block=500):
    tabs = []
    for i in range(0, len(bets), block):
        sub = bets[i:i+block]
        s = summarize_bets(sub)
        s["i0"] = i
        s["date_min"] = sub[0]["date"]
        s["date_max"] = sub[-1]["date"]
        s["baseline_roi"] = mean(r["profit_fav"] for r in sub)
        s["incremental_roi"] = s["roi"] - s["baseline_roi"]
        tabs.append(s)
    return tabs

def monthly(bets):
    tabs = []
    months = sorted({r["date"][:7] for r in bets})
    for m in months:
        sub = [r for r in bets if r["date"][:7]==m]
        if len(sub) < 30:
            continue
        s = summarize_bets(sub)
        s["month"] = m
        s["baseline_roi"] = mean(r["profit_fav"] for r in sub)
        s["incremental_roi"] = s["roi"] - s["baseline_roi"]
        tabs.append(s)
    return tabs

def line_move(bets):
    sub = [r for r in bets if r["move"] is not None]
    def pack(xs, lab):
        s = summarize_bets(xs)
        s["label"] = lab
        if xs:
            s["mean_move_pp"] = mean(r["move"] for r in xs)
            s["baseline_roi"] = mean(r["profit_fav"] for r in xs)
            s["incremental_roi"] = s["roi"] - s["baseline_roi"]
        return s
    toward = [r for r in sub if r["move"] >= 1.0]      # >=1 pp toward pick
    against = [r for r in sub if r["move"] <= -1.0]
    stable = [r for r in sub if abs(r["move"]) < 1.0]
    return {
        "n_with_preclose": len(sub),
        "toward": pack(toward, "toward"),
        "stable": pack(stable, "stable"),
        "against": pack(against, "against"),
    }

def filter_pack(bets, pred, do_boot=False):
    sub = [r for r in bets if pred(r)]
    s = summarize_bets(sub)
    if sub and sub[0]["profit_fav"] is not None:
        s["baseline_hit"] = mean(r["fav_correct"] for r in sub)
        s["baseline_roi"] = mean(r["profit_fav"] for r in sub)
        s["baseline_mean_odds"] = mean(r["fav_odds"] for r in sub if r["fav_odds"])
        s["incremental_roi"] = s["roi"] - s["baseline_roi"]
        paired = [r["profit"] - r["profit_fav"] for r in sub]
        s["paired_mean"] = mean(paired)
        if do_boot:
            s["roi_boot"] = boot_roi([r["profit"] for r in sub], nboot=2000)
            s["paired_boot"] = boot_mean(paired, nboot=2000)
            s["baseline_boot"] = boot_roi([r["profit_fav"] for r in sub], nboot=2000)
    elif do_boot and sub:
        s["roi_boot"] = boot_roi([r["profit"] for r in sub], nboot=2000)
    return s

def split_70_30(bets):
    n = len(bets)
    # date boundary near 70%
    idx = int(n * 0.70)
    if idx <= 0 or idx >= n:
        return bets, []
    split_date = bets[idx]["date"]
    train = [r for r in bets if r["date"] < split_date]
    val = [r for r in bets if r["date"] >= split_date]
    return train, val, split_date

def model_level(rows_flat, pmax_field, pick_field, profit_field, odds_field, brier_field, correct_field):
    """Generic pack for FI or consensus rows that have those fields filled."""
    bets = []
    for r in rows_flat:
        if r[pmax_field] == "" or r[pick_field] == "" or r[odds_field] == "":
            continue
        y = {"1":0,"X":1,"2":2}[r["outcome"]]
        pick = {"1":0,"X":1,"2":2}[r[pick_field]]
        bets.append({
            "date": r["date"],
            "y": y,
            "model_pick": pick,
            "model_p": float(r[pmax_field]),
            "picked_odds": float(r[odds_field]),
            "brier": float(r[brier_field]),
            "logloss": float(r["logloss_fp"]) if brier_field=="brier_fp" else float(r.get("cons_logloss") or r.get("fi_logloss") or 0),
            "profit": float(r[profit_field]),
            "profit_fav": float(r["profit_fav"]) if r["profit_fav"] != "" else None,
            "fav_correct": int(r["fav_correct"]) if r["fav_correct"] != "" else None,
            "fav_odds": float(r["fav_odds"]) if r["fav_odds"] != "" else None,
            "fair_pick": float({"1": r["pin_fair_1"], "X": r["pin_fair_x"], "2": r["pin_fair_2"]}[r[pick_field]]),
            "edge": float(r[pmax_field]) - float({"1": r["pin_fair_1"], "X": r["pin_fair_x"], "2": r["pin_fair_2"]}[r[pick_field]]),
            "same": int(pick == {"1":0,"X":1,"2":2}[r["fav_pick"]]) if r["fav_pick"] else None,
            "div": r["div"],
            "league": r["league"],
            "home_pick": r[pick_field]=="1",
            "away_pick": r[pick_field]=="2",
            "move": float(r["move_toward_pick_pp"]) if r["move_toward_pick_pp"] != "" else None,
        })
    # fix logloss for fi/cons
    return bets

def main():
    print("loading...")
    fs = load_fs()
    fd = load_fd()
    fi = load_fi()
    print(f"FS {len(fs)} FD pinnacle {len(fd)} FI {len(fi)}")
    mappable = [r for r in fs if r["league"] in MAPPABLE or r["league"] in {"LIGA2","SERIEB","BDL2","LIGUE_2"}]
    print("FS mappable leagues", len(mappable), "date", mappable[0]["date"] if mappable else None, mappable[-1]["date"] if mappable else None)

    pairs, ambiguous = join_fs_fd(fs, fd)
    print(f"joined {len(pairs)} ambiguous {len(ambiguous)}")

    fi_index = {}
    for r in fi:
        fi_index[(r["date"], canon(r["home"]), canon(r["away"]))] = r
    recs = build_records(fs, fd, pairs, fi_index)
    flat = attach_market_rows(recs)
    print(f"records {len(flat)} with FI {sum(1 for r in flat if r['FI_p1']!='')} with pre {sum(1 for r in flat if r['move_toward_pick_pp']!='')}")
    print("name score dist", Counter(r["name_score"] for r in flat))
    print("date range", flat[0]["date"], flat[-1]["date"])
    print("discovery n", sum(1 for r in flat if r["discovery_window"]==1))

    # weak name matches for audit
    weak = [r for r in flat if r["name_score"] < 4]
    # name_score is sum of two sides, exact+exact = 6
    print("name_score < 6", sum(1 for r in flat if int(r["name_score"])<6), " <5", sum(1 for r in flat if int(r["name_score"])<5))

    csv_path = os.path.join(OUT, "falsify_matches.csv")
    fields = list(flat[0].keys())
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(flat)
    print("wrote", csv_path)

    bets_all = as_bet_rows(flat)
    dropped_no_fav = sum(1 for r in bets_all if r["profit_fav"] is None or r["fav_correct"] is None)
    bets = [r for r in bets_all if r["profit_fav"] is not None and r["fav_correct"] is not None]
    print(f"bets {len(bets)} dropped tied-favorite {dropped_no_fav}")
    pre = [r for r in bets if r["date"] < DISCOVERY_START]
    disc = [r for r in bets if r["date"] >= DISCOVERY_START]
    print(f"pre-discovery {len(pre)} discovery {len(disc)}")

    train, val, split_date = split_70_30(pre)
    print(f"pre split {split_date} train {len(train)} {train[0]['date']}..{train[-1]['date']} val {len(val)} {val[0]['date']}..{val[-1]['date']}")

    # overall model quality
    def quality(sub, label):
        if not sub:
            return {"label": label, "n": 0}
        return {
            "label": label,
            "n": len(sub),
            "hit": mean(r["model_pick"]==r["y"] for r in sub),
            "brier": mean(r["brier"] for r in sub),
            "logloss": mean(r["logloss"] for r in sub),
            "dates": (sub[0]["date"], sub[-1]["date"]),
            "ece_pick": ece(sub, lambda r: r["model_p"], lambda r: r["model_pick"]==r["y"]),
            "always_fav_hit": mean(r["fav_correct"] for r in sub),
            "always_fav_roi": mean(r["profit_fav"] for r in sub),
            "always_model_roi": mean(r["profit"] for r in sub),
        }

    report = {
        "n": len(bets),
        "date_min": bets[0]["date"],
        "date_max": bets[-1]["date"],
        "n_fd": len(fd),
        "n_fs": len(fs),
        "n_fi": len(fi),
        "n_ambiguous_skipped": len(ambiguous),
        "ambiguous_sample": ambiguous[:15],
        "n_with_opening_or_preclose": sum(1 for r in bets if r["move"] is not None),
        "n_discovery_window_included": len(disc),
        "n_pre_discovery": len(pre),
        "coverage_by_div": dict(Counter(r["div"] for r in bets)),
        "coverage_by_season": dict(Counter(r["date"][:4] for r in bets)),
        "quality_all": quality(bets, "all"),
        "quality_pre": quality(pre, "pre"),
        "quality_discovery": quality(disc, "discovery"),
        "note_odds": "Pinnacle closing (PSCH/PPCH). Pre-closing snapshot (PSH/PPH) used only for line movement, per football-data.co.uk notes: columns without C are pre-closing, with C are closing.",
        "sources": {
            "foresportia": "https://www.foresportia.com/api/past_results.php",
            "foresportia_page": "https://www.foresportia.com/en/past_results.html",
            "footinsights": "https://footinsights.com/data/predictions.csv",
            "pinnacle": "https://www.football-data.co.uk/notes.txt",
            "files": "https://www.football-data.co.uk/mmz4281/{2324,2425,2526,2627}/{div}.csv",
        },
    }

    # Analysis 1 buckets on PRE (uncontaminated) and on full
    report["A1_prob_buckets_pre"] = bucket_table(pre)
    report["A1_prob_buckets_full"] = bucket_table(bets)
    report["A1_prob_buckets_train"] = bucket_table(train)
    report["A1_prob_buckets_val"] = bucket_table(val)

    # Analysis 2 matrix — descriptive on pre, and val only for cells
    report["A2_matrix_pre"] = matrix(pre)
    report["A2_matrix_val"] = matrix(val)
    report["A2_matrix_train"] = matrix(train)

    # Analysis 3 favorites
    report["A3_pre"] = fav_split(pre)
    report["A3_val"] = fav_split(val)
    report["A3_odds_pre"] = fav_odds_table(pre)
    report["A3_odds_val"] = fav_odds_table(val)

    # Analysis 4 home away on preregistered filters, validation + pre
    report["A4_home_away"] = {
        "pre_p50": home_away(pre, lambda r: r["model_p"] >= 0.50),
        "pre_p60": home_away(pre, lambda r: r["model_p"] >= 0.60),
        "val_p50": home_away(val, lambda r: r["model_p"] >= 0.50),
        "val_p60": home_away(val, lambda r: r["model_p"] >= 0.60),
    }

    # Analysis 5 leagues on pre (stable history) n>=50
    report["A5_leagues_pre"] = by_league(pre, 50)
    report["A5_leagues_val_p50"] = by_league([r for r in val if r["model_p"]>=0.50], 30)

    # Analysis 6 edge
    report["A6_edge_pre"] = edge_table(pre)
    report["A6_edge_val"] = edge_table(val)
    report["A6_edge_full"] = edge_table(bets)

    # Analysis 7 line movement on pre
    report["A7_line_pre"] = line_move(pre)
    report["A7_line_val"] = line_move(val)

    # Analysis 8 calibration already inside buckets + ECE in quality
    # outcome-level calibration (each of 3 outcomes) on pre
    triples = []
    # rebuild from flat pre dates
    pre_dates = {r["date"] for r in pre}  # not unique
    # use recs
    pre_recs = [r for r in recs if r["date"] < DISCOVERY_START]
    oc = []
    obs = []
    for r in pre_recs:
        for k in range(3):
            obs.append((r["fs_p"][k], 1 if k==r["y"] else 0))
    for a,b,lab in [(0,0.15,"0-15"),(0.15,0.25,"15-25"),(0.25,0.35,"25-35"),(0.35,0.45,"35-45"),(0.45,0.55,"45-55"),(0.55,0.65,"55-65"),(0.65,0.75,"65-75"),(0.75,1.01,"75+")]:
        sub = [(p,h) for p,h in obs if a <= p < b]
        if not sub:
            oc.append({"bin": lab, "n": 0})
            continue
        mp = mean(p for p,_ in sub)
        fr = mean(h for _,h in sub)
        oc.append({"bin": lab, "n": len(sub), "mean_p": mp, "freq": fr, "gap": fr-mp})
    # ECE outcomes
    nobs = sum(c["n"] for c in oc)
    ece_out = sum(c["n"]*abs(c["gap"]) for c in oc if c["n"]) / nobs if nobs else None
    report["A8_outcome_calibration_pre"] = oc
    report["A8_ece_outcomes_pre"] = ece_out
    report["A8_ece_pick_train"] = ece(train, lambda r: r["model_p"], lambda r: r["model_pick"]==r["y"])
    report["A8_ece_pick_val"] = ece(val, lambda r: r["model_p"], lambda r: r["model_pick"]==r["y"])

    # Analysis 9 split already defined. Preregistered filters.
    def reg(sub, label):
        return {
            "label": label,
            "n_matches": len(sub),
            "dates": (sub[0]["date"], sub[-1]["date"]) if sub else None,
            "all_picks": filter_pack(sub, lambda r: True, do_boot=False),
            "p50": filter_pack(sub, lambda r: r["model_p"] >= 0.50, do_boot=True),
            "p60": filter_pack(sub, lambda r: r["model_p"] >= 0.60, do_boot=True),
            "baseline_fav": {
                "n": len(sub),
                "hit": mean(r["fav_correct"] for r in sub) if sub else None,
                "roi": mean(r["profit_fav"] for r in sub) if sub else None,
                "mean_odds": mean(r["fav_odds"] for r in sub) if sub else None,
                "boot": boot_roi([r["profit_fav"] for r in sub], 2000) if sub else None,
                "max_dd": max_dd_streak([r["profit_fav"] for r in sub])[0] if sub else None,
            },
        }
    report["split_date_prediscovery"] = split_date
    report["A9_train"] = reg(train, "train")
    report["A9_val"] = reg(val, "val")
    report["A13_full_prediscovery"] = reg(pre, "pre_discovery_all")
    report["A13_full_including_discovery"] = reg(bets, "full_including_discovery_window")
    report["A13_discovery_only"] = reg(disc, "discovery_window_already_seen")

    # Also 70/30 on FULL sample to show the contamination if someone splits naively
    trF, vaF, sdF = split_70_30(bets)
    report["naive_full_split_date"] = sdF
    report["naive_full_val_p50_p60"] = {
        "val_n": len(vaF),
        "val_dates": (vaF[0]["date"], vaF[-1]["date"]) if vaF else None,
        "p50": filter_pack(vaF, lambda r: r["model_p"]>=0.50, do_boot=True),
        "p60": filter_pack(vaF, lambda r: r["model_p"]>=0.60, do_boot=True),
        "note": "This validation slice of the FULL series may overlap the Aug-Sep 2026 discovery window. Not used for the classification.",
    }

    # Analysis 10 stability on pre, blocks of 500, and on p>=50 within blocks
    report["A10_blocks_all_picks"] = temporal(pre, 500)
    report["A10_blocks_p50"] = temporal([r for r in pre if r["model_p"]>=0.50], 250)
    report["A10_blocks_p60"] = temporal([r for r in pre if r["model_p"]>=0.60], 200)
    report["A10_monthly_p50"] = monthly([r for r in pre if r["model_p"]>=0.50])
    report["A10_monthly_p60"] = monthly([r for r in pre if r["model_p"]>=0.60])

    def by_season(sub, label):
        tabs = []
        for season in sorted({r["season"] for r in sub}):
            chunk = [r for r in sub if r["season"]==season]
            s = filter_pack(chunk, lambda r: True, do_boot=True)
            s["season"] = season
            s["slice"] = label
            s["date_min"] = chunk[0]["date"]
            s["date_max"] = chunk[-1]["date"]
            tabs.append(s)
        return tabs

    report["A4b_season_all_pre"] = by_season(pre, "all_picks")
    report["A4b_season_p50_pre"] = by_season([r for r in pre if r["model_p"]>=0.50], "p50")
    report["A4b_season_p60_pre"] = by_season([r for r in pre if r["model_p"]>=0.60], "p60")
    # validation-only seasons (the untouched final block inside pre-discovery)
    report["A4b_season_p50_val"] = by_season([r for r in val if r["model_p"]>=0.50], "val_p50")
    report["A4b_season_p60_val"] = by_season([r for r in val if r["model_p"]>=0.60], "val_p60")

    # Consensus + FI only exist in discovery window mostly
    cons_rows = [r for r in flat if r["CONS_pmax"] != ""]
    fi_rows = [r for r in flat if r["fi_pmax"] != ""]
    print("consensus rows", len(cons_rows), "fi rows", len(fi_rows))
    def side_model(rows, pmax, pick, profit, odds, brier, logf, correct):
        bets2 = []
        for r in rows:
            if r[pmax]=="" or r[pick]=="" or r["profit_fav"]=="" or r[profit]=="" or r["fav_pick"]=="":
                continue
            y = {"1":0,"X":1,"2":2}[r["outcome"]]
            pk = {"1":0,"X":1,"2":2}[r[pick]]
            bets2.append({
                "date": r["date"], "y": y, "model_pick": pk,
                "model_p": float(r[pmax]),
                "picked_odds": float(r[odds]),
                "brier": float(r[brier]),
                "logloss": float(r[logf]) if r[logf] != "" else 0.0,
                "profit": float(r[profit]),
                "profit_fav": float(r["profit_fav"]),
                "fav_correct": int(r["fav_correct"]),
                "fav_odds": float(r["fav_odds"]),
                "fair_pick": float({"1":r["pin_fair_1"],"X":r["pin_fair_x"],"2":r["pin_fair_2"]}[r[pick]]),
                "edge": float(r[pmax]) - float({"1":r["pin_fair_1"],"X":r["pin_fair_x"],"2":r["pin_fair_2"]}[r[pick]]),
                "same": int(pk == {"1":0,"X":1,"2":2}[r["fav_pick"]]),
                "div": r["div"], "league": r["league"],
                "home_pick": r[pick]=="1", "away_pick": r[pick]=="2",
                "move": None,
            })
        bets2.sort(key=lambda x: x["date"])
        return bets2
    # logloss fields
    # fi uses fi_logloss, cons uses cons_logloss — add if missing handled
    cons_b = side_model(cons_rows, "CONS_pmax", "CONS_pick", "profit_cons", "cons_picked_odds", "cons_brier", "cons_logloss", "cons_correct")
    fi_b = side_model(fi_rows, "fi_pmax", "FI_pick", "profit_fi", "fi_picked_odds", "fi_brier", "fi_logloss", "fi_correct")
    report["consensus_n"] = len(cons_b)
    report["fi_joined_n"] = len(fi_b)
    if cons_b:
        report["consensus_dates"] = (cons_b[0]["date"], cons_b[-1]["date"])
        report["consensus_prereg"] = reg(cons_b, "consensus_discovery_window_only")
        report["consensus_quality"] = quality(cons_b, "consensus")
        report["consensus_buckets"] = bucket_table(cons_b)
    if fi_b:
        report["fi_dates"] = (fi_b[0]["date"], fi_b[-1]["date"])
        report["fi_prereg"] = reg(fi_b, "footinsights_with_pinnacle")
        report["fi_quality"] = quality(fi_b, "fi")
        report["fi_buckets"] = bucket_table(fi_b)

    if cons_b:
        overlap_keys = {(r["date"], r["home"], r["away"]) for r in cons_rows}
        ov_bets = []
        for r in flat:
            if (r["date"], r["home"], r["away"]) not in overlap_keys:
                continue
            b = as_bet_rows([r])[0]
            if b["profit_fav"] is None:
                continue
            ov_bets.append(b)
        report["overlap_foresportia"] = reg(ov_bets, "fs_on_consensus_matches_already_seen")

    json.dump(report, open(os.path.join(OUT, "falsify_metrics.json"), "w"), indent=2, default=float)
    # write ambiguous list
    with open(os.path.join(OUT, "ambiguous_joins.json"), "w") as f:
        json.dump(ambiguous[:200], f, indent=2)
    print("metrics written")

    # console summary of the decision numbers
    def brief(title, block):
        print("\n==", title)
        for k in ("all_picks", "p50", "p60", "baseline_fav"):
            s = block[k]
            if not s or s.get("n",0)==0:
                print(k, "empty"); continue
            if k=="baseline_fav":
                print(f"  BASE n={s['n']} hit={s['hit']:.3f} roi={s['roi']:.3f} odds={s['mean_odds']:.3f} dd={s['max_dd']:.2f}")
                if s.get("boot"):
                    print(f"       boot ROI {s['boot']['roi']:.3f} CI {s['boot']['ci95']}")
            else:
                print(f"  {k} n={s['n']} hit={s.get('hit')} roi={s.get('roi')} odds={s.get('mean_odds')} gap={s.get('gap')} inc={s.get('incremental_roi')} dd={s.get('max_dd')}")
                if s.get("roi_boot"):
                    print(f"       ROI CI {s['roi_boot']['ci95']} P(roi>0)~{s['roi_boot']['frac_boot_roi_gt_0']:.2f}")
                if s.get("paired_boot"):
                    print(f"       paired Δ CI {s['paired_boot']['ci95']} P(Δ>0)~{s['paired_boot']['frac_gt_0']:.2f}")

    brief("TRAIN pre-discovery", report["A9_train"])
    brief("VAL pre-discovery", report["A9_val"])
    brief("ALL pre-discovery", report["A13_full_prediscovery"])
    brief("DISCOVERY already seen", report["A13_discovery_only"])
    print("\nECE pick train/val", report["A8_ece_pick_train"], report["A8_ece_pick_val"], "outcome ECE", ece_out)
    print("split", split_date, "n", len(flat))

if __name__ == "__main__":
    main()
