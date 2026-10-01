import json
from datetime import datetime, timezone
import pytest
from prospective.collect import parse_fi_html


def page(start, extra=None):
    event={'@type':'SportsEvent','startDate':start,'homeTeam':{'name':'Synthetic Home'},
           'awayTeam':{'name':'Synthetic Away'},'organizer':{'name':'Synthetic League'}}
    events=[event,extra] if extra else event
    return '<script type="application/ld+json">'+json.dumps(events)+'</script>'


@pytest.mark.parametrize('raw',['2026-10-02T16:00:00Z','2026-10-02T18:00:00+02:00'])
def test_explicit_source_time_retains_raw_without_browser_inference(raw):
    p=parse_fi_html(page(raw),'https://footinsights.com/predictions/synthetic/')
    assert p['kickoff']==datetime(2026,10,2,16,tzinfo=timezone.utc)
    assert p['kickoff_raw']==raw and p['timezone_status']=='EXPLICIT_OFFSET'
    assert p['source_time_evidence']=='SportsEvent.startDate'


@pytest.mark.parametrize('raw',['2026-10-02T16:00:00','not-a-time',''])
def test_unresolved_source_clock_has_no_proof(raw):
    p=parse_fi_html(page(raw),'https://footinsights.com/predictions/synthetic/')
    assert p['kickoff'] is None and p['kickoff_raw']==raw
    assert p['timezone_status']=='TIMEZONE_UNRESOLVED' and p['source_time_evidence']==''


def test_multiple_source_events_are_ambiguous_not_first_event_wins():
    p=parse_fi_html(page('2026-10-02T16:00:00Z',{'@type':'SportsEvent','startDate':'2026-10-03T16:00:00Z'}),'synthetic')
    assert p['kickoff'] is None and p['timezone_status']=='TIMEZONE_UNRESOLVED'
