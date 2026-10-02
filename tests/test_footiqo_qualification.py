from staging.result_sources.footiqo import parse_overview


def table(rows, headers=None):
    headers = headers or ['id', 'matchDate', 'Country', 'League', 'Season',
                          'homeTeam', 'awayTeam', 'FTHG', 'FTAG', 'FTR']
    return '<table><tr>' + ''.join(f'<th>{h}</th>' for h in headers) + '</tr>' + ''.join(
        '<tr>' + ''.join(f'<td>{c}</td>' for c in row) + '</tr>' for row in rows) + '</table>'


BASE = ['151344', '01-10-26 20:45', 'Europe', 'UEFA Nations League',
        '2026/2027', 'Israel', 'Kosovo', '0', '0', 'D']


def test_real_zero_preserved_but_time_and_optional_fields_missing():
    rows, audit = parse_overview(table([BASE]))
    row = rows[0]
    assert row['final_home_goals'] == row['final_away_goals'] == 0
    assert row['result_1x2'] == 'X'
    assert row['kickoff_raw'] == '01-10-26 20:45'
    assert row['kickoff_utc'] is None and row['timezone_status'] == 'TIMEZONE_UNRESOLVED'
    assert row['ht_home_goals'] is None and row['corners_home'] is None
    assert row['verification_status'] == 'UNVERIFIED'
    assert not row['production_eligible'] and not audit['production_eligible']


def test_missing_invalid_or_conflicting_score_never_becomes_zero():
    for score in ('', '-', 'None', '-1', '1.5'):
        rows, audit = parse_overview(table([BASE[:7] + [score, '0', 'D']]))
        assert rows == [] and audit['rejected'] == {'FT_MISSING_OR_INVALID': 1}
    rows, audit = parse_overview(table([BASE[:7] + ['2', '0', 'D']]))
    assert rows == [] and audit['rejected'] == {'FT_RESULT_CONFLICT': 1}


def test_duplicate_conflicting_ids_block_all_versions_even_valid_first():
    revised = BASE[:7] + ['2', '0', 'H']
    for pair in ([BASE, revised], [revised, BASE]):
        rows, audit = parse_overview(table(pair))
        assert rows == [] and audit['conflicting_ids'] == 1
    assert len(parse_overview(table([BASE, BASE]))[0]) == 1
    assert parse_overview(table([BASE, BASE[:7] + ['2', '0', 'D']]))[0] == []


def test_header_and_width_drift_fail_closed_and_markup_decodes():
    assert parse_overview('<table><tr><td>marketing</td></tr></table>')[0] == []
    assert parse_overview(table([BASE[:-1]]))[1]['rejected'] == {'ROW_WIDTH': 1}
    headers = ['id', 'matchDate', 'Country', 'League', 'Season', 'homeTeam',
               'awayTeam', 'FTHG', 'FTAG', 'FTR', 'FTHG']
    assert parse_overview(table([BASE + ['9']], headers))[1]['rejected'] == {'DUPLICATE_HEADER': 1}
    row = BASE.copy()
    row[5] = '<span>A &amp; B</span>'
    assert parse_overview(table([row]))[0][0]['home_team'] == 'A & B'
