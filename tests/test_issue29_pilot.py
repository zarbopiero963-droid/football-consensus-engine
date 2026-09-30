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
