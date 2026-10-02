"""Read-only qualification parser for the observed public Footiqo overview table.

No network, DB writes or production wiring. matchDate has no source timezone
or four-digit year, so it is retained verbatim and never normalized to UTC.
FTR is checked against both explicit FT fields; unavailable HT/corners stay null.
"""
from collections import Counter
from html.parser import HTMLParser
import re

CONTRACT = 'FOOTIQO_OVERVIEW_QUALIFICATION_V1'
REQUIRED = ('id', 'matchDate', 'Country', 'League', 'Season', 'homeTeam',
            'awayTeam', 'FTHG', 'FTAG', 'FTR')


class Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.depth = 0
        self.rows = None
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.depth += 1
            if self.depth == 1:
                self.rows = []
        elif self.depth == 1 and tag == 'tr':
            self.row = []
        elif self.depth == 1 and tag in ('th', 'td') and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.depth == 1 and self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if self.depth == 1 and tag in ('th', 'td') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif self.depth == 1 and tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == 'table' and self.depth:
            if self.depth == 1:
                self.tables.append(self.rows)
                self.rows = self.row = self.cell = None
            self.depth -= 1


def parse_overview(html):
    parser = Tables()
    parser.feed(html)
    rejected = Counter()
    candidates = {}
    conflicts = set()
    tables_seen = 0
    for table in parser.tables:
        if not table or not set(REQUIRED).issubset(table[0]):
            continue
        tables_seen += 1
        header = table[0]
        if len(header) != len(set(header)):
            rejected['DUPLICATE_HEADER'] += 1
            continue
        for cells in table[1:]:
            if len(cells) != len(header):
                rejected['ROW_WIDTH'] += 1
                continue
            raw = dict(zip(header, cells))
            mid = raw['id']
            if not re.fullmatch(r'[1-9][0-9]*', mid) or any(
                    not raw[key] for key in ('matchDate', 'League', 'Season', 'homeTeam', 'awayTeam')):
                rejected['IDENTITY_MISSING'] += 1
                continue
            if any(not re.fullmatch(r'[0-9]+', raw[k]) for k in ('FTHG', 'FTAG')):
                rejected['FT_MISSING_OR_INVALID'] += 1
                continue
            hg, ag = int(raw['FTHG']), int(raw['FTAG'])
            result = 'H' if hg > ag else 'A' if hg < ag else 'D'
            if raw['FTR'] != result:
                rejected['FT_RESULT_CONFLICT'] += 1
                conflicts.add(mid)
                continue
            row = {
                'provider': 'footiqo', 'source_match_id': mid,
                'home_team': raw['homeTeam'], 'away_team': raw['awayTeam'],
                'league': raw['League'], 'country_raw': raw['Country'], 'season': raw['Season'],
                'kickoff_raw': raw['matchDate'], 'kickoff_utc': None,
                'timezone_status': 'TIMEZONE_UNRESOLVED',
                'status': 'FINISHED_REPORTED', 'final_home_goals': hg, 'final_away_goals': ag,
                'result_1x2': {'H': '1', 'D': 'X', 'A': '2'}[result],
                'ht_home_goals': None, 'ht_away_goals': None,
                'corners_home': None, 'corners_away': None,
                'ht_corners_home': None, 'ht_corners_away': None,
                'verification_status': 'UNVERIFIED', 'production_eligible': False,
            }
            if mid in candidates and candidates[mid] != row:
                conflicts.add(mid)
                rejected['DUPLICATE_ID_CONFLICT'] += 1
            else:
                candidates[mid] = row
    rows = [row for mid, row in candidates.items() if mid not in conflicts]
    return rows, {'contract': CONTRACT, 'tables_seen': tables_seen,
                  'accepted': len(rows), 'conflicting_ids': len(conflicts),
                  'rejected': dict(sorted(rejected.items())), 'production_eligible': False}
