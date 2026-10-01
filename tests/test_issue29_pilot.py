from staging.issue29_pilot.pilot48 import (
    parse_kickoff_ai, parse_kickform, parse_opta,
    parse_scoutingstats, parse_totalcorner, valid_triplet,
)


def test_valid_triplet():
    assert valid_triplet(40, 30, 30)
    assert not valid_triplet(40, 30, -1)


def test_kickoff_parser():
    text = """Dortmund
73%
17%
10%
18:30 09 OCTOBER
Werder"""
    rows = parse_kickoff_ai(text)
    assert rows[0]["home_team"] == "Dortmund"
    assert rows[0]["away_team"] == "Werder"
    assert rows[0]["p1"] == 73


def test_kickform_parser():
    text = """Eldense
Real Oviedo
02/10
20:30
22%
20%
58%"""
    rows = parse_kickform(text)
    assert rows[0]["p2"] == 58


def test_opta_parser():
    text = "Win probability: Germany 66.8%, draw 18.2%, Serbia 15.0%."
    rows = parse_opta(text)
    assert rows[0]["home_team"] == "Germany"
    assert rows[0]["px"] == 18.2


def test_scoutingstats_parser():
    text = """Croatia
1.4–1.7
PREDICTED GOALS
England
Home
32%
Draw
22%
Away
46%"""
    rows = parse_scoutingstats(text)
    assert rows[0]["p2"] == 46


def test_totalcorner_parser():
    text = "Friendlies\t10/01 02:00\tArgentina\tvs\tBolivia\t8.5\t6.88\t27\t73\tUnder\t31%"
    rows = parse_totalcorner(text)
    assert rows[0]["parse_status"] == "CORNERS_ONLY"
    assert rows[0]["payload_extra"]["under_probability"] == 73


def test_aipredict_line_parser():
    from staging.issue29_pilot.pilot48 import parse_aipredict
    text = """Argentina
2:0
AI PREDICTION
B
Bolivia
AI
70%
18%
12%
1
Argentina to win
70%"""
    rows = parse_aipredict(text)
    assert len(rows) == 1
    assert rows[0]["home_team"] == "Argentina"
    assert rows[0]["away_team"] == "Bolivia"
    assert rows[0]["p1"] == 70
    assert rows[0]["px"] == 18
    assert rows[0]["p2"] == 12


def test_vitisport_partial_pick_parser():
    from staging.issue29_pilot.pilot48 import parse_vitisport
    text = """18:00
Eritrea
0 : 2
South Africa
2"""
    rows = parse_vitisport(text)
    assert len(rows) == 1
    assert rows[0]["parse_status"] == "PARTIAL_PICK"
    assert rows[0]["payload_extra"]["selection"] == "2"
    assert rows[0]["payload_extra"]["correct_score"] == "0:2"


def test_real_opta_404_content_does_not_certify_transport_success():
    import json
    from pathlib import Path
    from staging.issue29_pilot.pilot48 import run_quality
    fixture = json.loads((Path(__file__).parent / 'fixtures/opta_http404_20261001.json').read_text())
    rows = parse_opta(fixture['visible_text_excerpt'])
    assert len(rows) == fixture['expected_rows'] == 24
    assert run_quality(fixture['http_status'], len(rows)) == {
        'transport_status': 'HTTP_ERROR', 'content_parse_status': 'PARSED'}
    assert all(valid_triplet(r['p1'], r['px'], r['p2']) for r in rows)


def test_transport_unobserved_and_empty_content_are_separate():
    from staging.issue29_pilot.pilot48 import run_quality
    assert run_quality(None, 0) == {
        'transport_status': 'HTTP_UNOBSERVED', 'content_parse_status': 'EMPTY'}
    assert run_quality(200, 0) == {
        'transport_status': 'HTTP_OK', 'content_parse_status': 'EMPTY'}
    assert run_quality(200, 1) == {
        'transport_status': 'HTTP_OK', 'content_parse_status': 'PARSED'}
    assert run_quality(403, 1)['transport_status'] == 'HTTP_ERROR'


def test_observed_opta_homepage_route_keeps_native_probabilities_and_unresolved_time():
    import hashlib
    import json
    from pathlib import Path
    from staging.issue29_pilot.pilot48 import SOURCES, run_quality
    fixture = json.loads((Path(__file__).parent / 'fixtures/opta_home_20261001.json').read_text())
    assert SOURCES['opta_analyst'] == fixture['final_url'] == 'https://theanalyst.com/'
    assert hashlib.sha256(fixture['text'].encode()).hexdigest() == fixture['dom_text_sha256']
    assert not fixture['truncated']
    rows = parse_opta(fixture['text'])
    assert len(rows) == fixture['expected_rows'] == 28
    first = rows[0]
    assert (first['home_team'], first['away_team']) == ('Azerbaijan', 'Liechtenstein')
    assert (first['p1'], first['px'], first['p2']) == (82.9, 11.7, 5.4)
    assert all(r['timezone_status'] == 'TIMEZONE_UNRESOLVED' and r['kickoff_raw'] == '' for r in rows)
    assert run_quality(fixture['http_status'], len(rows)) == {
        'transport_status': 'HTTP_OK', 'content_parse_status': 'PARSED'}
