from datetime import timedelta
from threading import Event
from time import monotonic

import pandas as pd
import pytest

from src.services.software_market import SoftwareMarketClient
from tests.test_research_factors import NOW, archive, snapshot


def quote(code, **changes):
    return {'code':code, 'issues':[], 'quote':{'code':code, 'source':'tencent',
        'source_time':NOW.isoformat(), 'fetched_at':NOW.isoformat(),
        'price':9., 'previous_close':10., 'high':10., 'low':9., **changes}}


def client():
    return SoftwareMarketClient('http://127.0.0.1:9000', 'test-only')


def test_quote_clock_is_only_attached_to_its_own_prices(monkeypatch, tmp_path):
    frame = snapshot(NOW)
    frame.attrs.pop('source_time')
    frame['source_time'] = NOW.date().isoformat()
    original = frame.copy(deep=True)
    monkeypatch.setattr(SoftwareMarketClient, 'quotes',
                        lambda self, codes, **kw:{'quotes':[quote(code) for code in codes]})
    enriched = client().enrich_snapshot_quotes(frame, clock=lambda:NOW)
    pd.testing.assert_frame_equal(frame, original)
    assert enriched['source_time'].tolist() == original['source_time'].tolist()
    assert enriched['price'].tolist() == original['price'].tolist()
    assert enriched.attrs['snapshot_source'] == original.attrs['snapshot_source']
    result = archive(tmp_path).observe(enriched, NOW,
        received_at=enriched.attrs['market_quote_received_at'])
    assert result['features']['source_coverage'] == 1
    assert result['features']['advancing_pct'] == 0  # Use quote price=9, not old positive prices.
    assert result['source'] == 'software:go/tencent'
    assert result['asOf'] == NOW.isoformat()
    assert result['score'] is None  # Raw breadth does not invent 60 historical sessions.


@pytest.mark.parametrize('bad_quote', [
    quote('600002', source_time=None),
    quote('600002', source_time=NOW.date().isoformat()),
    quote('600002', source_time=(NOW+timedelta(seconds=1)).isoformat()),
    quote('600002', source_time=(NOW-timedelta(days=1)).isoformat()),
    quote('600002', source='unknown'),
    {'code':'600002', 'quote':quote('600003')['quote'], 'issues':[]},
    {**quote('600002'), 'issues':['cross_source_price_conflict']},
])
def test_invalid_provider_clock_identity_and_conflict_keep_universe_denominator(monkeypatch, tmp_path, bad_quote):
    frame = snapshot(NOW)
    monkeypatch.setattr(SoftwareMarketClient, 'quotes', lambda self,codes,**kw:{
        'quotes':[bad_quote if code == '600002' else quote(code) for code in codes]})
    enriched = client().enrich_snapshot_quotes(frame, clock=lambda:NOW)
    result = archive(tmp_path).observe(enriched, NOW, received_at=NOW)
    assert len(enriched) == 3
    assert result['features']['source_coverage'] == pytest.approx(2/3)
    assert result['status'] == 'unavailable'


def test_old_quote_keeps_actual_source_clock_and_stays_unavailable(monkeypatch, tmp_path):
    old = NOW-timedelta(minutes=6)
    monkeypatch.setattr(SoftwareMarketClient, 'quotes', lambda self,codes,**kw:{
        'quotes':[quote(code, source_time=old.isoformat()) for code in codes]})
    enriched = client().enrich_snapshot_quotes(snapshot(NOW), clock=lambda:NOW)
    assert enriched.iloc[0]['market_quote']['source_time'] == old.isoformat()
    result = archive(tmp_path).observe(enriched, NOW, received_at=NOW)
    assert result['status'] == 'unavailable'
    assert result['features']['source_coverage'] == 0


def test_missing_quote_clock_cannot_borrow_aggregate_snapshot_clock(tmp_path):
    frame = snapshot(NOW)
    frame['market_quote'] = [{'code':code,'price':9.,'previous_close':10.}
                             for code in frame['code']]
    result = archive(tmp_path).observe(frame, NOW, received_at=NOW)
    assert result['status'] == 'unavailable'
    assert result['features']['source_coverage'] == 0


def test_snapshot_enrichment_deadline_preserves_partial_batches(monkeypatch):
    codes = [f'600{i:03d}' for i in range(40)]
    frame = pd.DataFrame({'code':codes, 'name':['普通股']*40, 'price':[10.]*40})
    release = Event()
    def fetch(self, batch, **kwargs):
        if batch[0] == codes[20]:
            release.wait(2)
        return {'quotes':[quote(code) for code in batch]}
    monkeypatch.setattr(SoftwareMarketClient, 'quotes', fetch)
    try:
        started = monotonic()
        result = client().enrich_snapshot_quotes(frame, clock=lambda:NOW, timeout=.1)
        assert monotonic()-started < .6
        assert len(result) == 40
        assert sum(isinstance(value, dict) for value in result.market_quote) == 20
        assert result.attrs['market_quote_enrichment']['missing_batches'] == 1
    finally:
        release.set()


def test_expired_enrichment_starts_no_request(monkeypatch):
    monkeypatch.setattr(SoftwareMarketClient, 'quotes', lambda *a,**kw:pytest.fail('late request'))
    result = client().enrich_snapshot_quotes(snapshot(NOW), deadline=monotonic()-1, clock=lambda:NOW)
    assert len(result) == 3 and result['market_quote'].isna().all()
