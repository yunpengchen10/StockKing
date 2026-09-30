from datetime import datetime, timedelta
from threading import Event
from time import monotonic
from unittest.mock import MagicMock, patch

import pytest

from src.services.software_market import SoftwareMarketClient
from src.services.yao_scout import minute_history, sector_fund_evidence
from src.services.yao_scout.intraday_evidence import SHANGHAI


NOW = datetime(2026, 9, 29, 10, 30, tzinfo=SHANGHAI)


def bars(day=None):
    end = NOW if day is None else NOW.replace(year=day.year, month=day.month, day=day.day)
    return [{'end': (end-timedelta(minutes=i)).isoformat(), 'open': 10., 'close': 10.,
             'high': 10.1, 'low': 9.9, 'volume_shares': 100., 'amount_cny': 1000.,
             'source': 'fixture'} for i in range(17)]


def test_batch_deadline_returns_without_waiting_or_starting_queued_work():
    release, started = Event(), []
    def fetch(item):
        started.append(item)
        if item:
            release.wait(2)
        return item
    try:
        before = monotonic()
        completed, missing = minute_history.bounded_fetch_map(fetch, range(100), workers=2, timeout=.05)
        elapsed = monotonic()-before
        assert elapsed < .5
        assert completed == [0]
        assert missing == list(range(1,100))
        assert len(started) <= 3
    finally:
        release.set()


def test_batch_records_failed_inputs_as_missing():
    def fetch(item):
        if item == 2:
            raise ValueError('unavailable')
        return item * 10
    completed, missing = minute_history.bounded_fetch_map(fetch, [1,2,3], timeout=1)
    assert completed == [10,30]
    assert missing == [2]


def test_sector_deadline_preserves_full_denominator_and_missing_evidence(tmp_path):
    codes = [f'60000{i}' for i in range(6)]
    release = Event()
    def fetch(code, **_):
        if code not in codes[:3]:
            release.wait(2)
        return bars()
    try:
        before = monotonic()
        result = sector_fund_evidence.fetch_sector_batch([codes[0]],
            {codes[0]: {'asOf': NOW.isoformat(), 'metrics': {'speed_5m_pct': 2}}}, NOW, tmp_path,
            prepared=({codes[0]: {'code': 'industry'}}, {'industry': codes}),
            bar_fetcher=fetch, timeout=.05)[codes[0]]
        assert monotonic()-before < .5
        assert result['totalPeers'] == 5
        assert result['peerCount'] == 2
        assert result['metrics']['sector_coverage_pct'] == 40
        assert result['coverageRequiredPct'] == 80
        assert 'sector_breadth' not in result['metrics']
        assert result['fetchBudget']['unfinishedPeers'] == 3
        assert any('时间预算' in gap for gap in result['gaps'])
    finally:
        release.set()


def test_membership_budget_survives_an_unresponsive_provider(tmp_path):
    release = Event()
    def get(*args, **kwargs):
        release.wait(2)
        raise ValueError('source unavailable')
    try:
        before = monotonic()
        with pytest.raises(TimeoutError):
            sector_fund_evidence.prepare_sector_membership(['600000'], NOW, tmp_path,
                                                          getter=get, timeout=.05)
        assert monotonic()-before < .5
    finally:
        release.set()


@pytest.mark.parametrize('fresh,count', [(True,240),(False,8000)])
def test_only_fresh_warm_archive_reduces_minute_download(monkeypatch,tmp_path,fresh,count):
    days = [NOW.date()-timedelta(days=i) for i in range(1,21)]
    monkeypatch.setattr(minute_history, '_prior_sessions', lambda day: days)
    archive = minute_history.MinuteArchive(tmp_path)
    archive.save('600000', [row for day in days[:5] for row in bars(day)] + (bars() if fresh else []), NOW)
    calls = []
    class Client:
        available = True
        def bars(self, code, **kwargs):
            calls.append(kwargs)
            return {'bars': bars(), 'time_semantics': 'bar_end'}
    monkeypatch.setattr(SoftwareMarketClient, 'from_environment', lambda: Client())
    result = minute_history.fetch_bar_evidence('600000',NOW,tmp_path,backfill=False)
    assert calls == [{'period':'1','count':count}]
    assert result['historyDays'] == 5
    assert len(archive.read('600000', NOW)) == 102


def test_gateway_timeout_is_explicit_and_bounded():
    client = SoftwareMarketClient('http://127.0.0.1:9000', 'test-token', read_timeout=4)
    session = MagicMock()
    session.__enter__.return_value = session
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {}
    with patch('src.services.software_market.requests.Session', return_value=session):
        client.bars('600000')
    assert session.post.call_args.kwargs['timeout'] == (3,4)


def test_sector_short_cache_reuses_bars_but_not_stale_timestamps(monkeypatch,tmp_path):
    codes = [f'60000{i}' for i in range(6)]
    called = []
    monkeypatch.setattr(sector_fund_evidence,'_recent_sector_bars',{})
    monkeypatch.setattr(sector_fund_evidence,'fetch_recent_bars',
                        lambda code, **_: called.append(code) or bars())
    args = ([codes[0]], {codes[0]:{'asOf':NOW.isoformat(),'metrics':{'speed_5m_pct':2}}}, NOW,tmp_path)
    prepared = ({codes[0]:{'code':'industry'}},{'industry':codes})
    first = sector_fund_evidence.fetch_sector_batch(*args,prepared=prepared)
    second = sector_fund_evidence.fetch_sector_batch(*args,prepared=prepared)
    assert len(called) == 6
    assert first[codes[0]]['peerCount'] == second[codes[0]]['peerCount'] == 5
    later = NOW+timedelta(minutes=10)
    stale = sector_fund_evidence.fetch_sector_batch([codes[0]],
        {codes[0]:{'asOf':later.isoformat(),'metrics':{'speed_5m_pct':2}}},later,tmp_path,prepared=prepared)
    assert stale[codes[0]]['peerCount'] == 0
    assert 'sector_breadth' not in stale[codes[0]]['metrics']
