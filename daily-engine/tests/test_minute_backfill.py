from datetime import datetime, time, timedelta

import pytest

from src.services.yao_scout import minute_history as minute


CUTOFF = datetime(2026, 10, 9, 15, 30, tzinfo=minute.SHANGHAI)
DAYS = ['2026-09-30', '2026-10-08']


def session(day, *, source='software', amount=1000):
    date = datetime.fromisoformat(day).date()
    starts = [datetime.combine(date, time(9, 31), minute.SHANGHAI),
              datetime.combine(date, time(13, 1), minute.SHANGHAI)]
    return [{'end': (start + timedelta(minutes=index)).isoformat(),
             'open': 10, 'high': 10.2, 'low': 9.8, 'close': 10,
             'amount_cny': amount, 'volume_shares': 100, 'source': source}
            for start in starts for index in range(120)]


def test_cross_source_duplicate_enriches_only_compatible_missing_fields():
    original = {**session(DAYS[0])[0], 'amount_cny': None}
    second = {**original, 'source': 'sina', 'amount_cny': 1000}
    result = list(minute.normalize_bars([original, second], CUTOFF).values())
    assert len(result) == 1 and result[0]['amount_cny'] == 1000
    assert result[0]['sources'] == ['sina', 'software']
    assert original['amount_cny'] is None
    assert not minute.normalize_bars([second, {**second, 'source': 'tdx', 'amount_cny': 1001}], CUTOFF)
    assert not minute.normalize_bars([original, {**second, 'volume_shares': 200, 'amount_cny': 2000}], CUTOFF)


def test_archive_supplements_holes_without_overwriting_disagreeing_existing_prices(tmp_path):
    original = session(DAYS[0])
    archive = minute.MinuteArchive(tmp_path)
    archive.save('600000', original[2:], CUTOFF)
    alternative = [{**row, 'source': 'sina', 'close': 10.02, 'amount_cny': 1002} for row in original]
    result = archive.save('600000', alternative, CUTOFF)
    saved = {row['end']: row for row in archive.read('600000', CUTOFF)}
    assert result == {'addedMinutes': 2, 'enrichedMinutes': 0, 'conflictingOverlapMinutes': 238}
    assert len(saved) == 240
    assert saved[original[0]['end']]['source'] == 'sina'
    assert saved[original[0]['end']]['close'] == 10.02
    assert saved[original[2]['end']]['source'] == 'software'
    assert saved[original[2]['end']]['close'] == 10


def test_required_days_find_a_historical_hole_despite_a_complete_current_day(monkeypatch, tmp_path):
    original = session(DAYS[0]) + session(DAYS[1])
    missing = original[119]
    archive = minute.MinuteArchive(tmp_path)
    archive.save('600000', [row for row in original if row != missing], CUTOFF)
    calls = []
    monkeypatch.setattr(minute, 'fetch_software_bars', lambda *a, **k: session(DAYS[1]))
    monkeypatch.setattr(minute, 'fetch_sina_bars', lambda *a, **k: calls.append(k) or
                        [{**row, 'source': 'sina'} for row in original])
    result = minute.archive_universe(['600000'], CUTOFF, tmp_path, include_registered=False,
                                    count=240, required_days={'600000': DAYS})
    detail = result['details'][0]
    assert calls and calls[0]['count'] == 1970
    assert result['updated'] == 1
    assert detail['archiveMerge']['addedMinutes'] == 1
    assert detail['coverage']['expectedMinutes'] == 480
    assert detail['coverage']['missingMinutes'] == 0
    assert detail['coverage']['missingDays'] == []
    repaired = next(row for row in archive.read('600000', CUTOFF) if row['end'] == missing['end'])
    assert repaired['source'] == 'sina'


def test_real_missing_auction_minutes_remain_missing_and_never_get_synthetic_rows(monkeypatch, tmp_path):
    bars = [row for row in session(DAYS[0]) if row['end'][11:16] not in ('14:58', '14:59')]
    monkeypatch.setattr(minute, 'fetch_software_bars', lambda *a, **k: bars)
    monkeypatch.setattr(minute, 'fetch_sina_bars', lambda *a, **k: [{**row, 'source': 'sina'} for row in bars])
    result = minute.archive_universe(['600000'], CUTOFF, tmp_path, include_registered=False,
                                    required_days={'600000': DAYS[:1]})
    assert result['incomplete'] == 1 and result['status'] == 'partial'
    coverage = result['details'][0]['coverage']
    assert coverage['missingDays'] == DAYS[:1]
    assert coverage['missingMinutes'] == 2
    assert [stamp[11:16] for stamp in coverage['missingMinuteTimes'][DAYS[0]]] == ['14:58', '14:59']
    assert len(minute.MinuteArchive(tmp_path).read('600000', CUTOFF)) == 238


def test_short_software_success_still_tries_public_history_and_records_failure(monkeypatch):
    bars = session(DAYS[0])
    calls = []
    monkeypatch.setattr(minute, 'fetch_software_bars', lambda *a, **k: bars)
    def unavailable(*args, **kwargs):
        calls.append(kwargs)
        raise ValueError('not available')
    monkeypatch.setattr(minute, 'fetch_sina_bars', unavailable)
    audit = {}
    result = minute.fetch_recent_bars('600000', count=1970, cutoff=CUTOFF, diagnostics=audit)
    assert len(result) == 240 and len(calls) == 1
    assert audit['sourceFailures'] == [{'source': 'sina', 'error': 'ValueError'}]


def test_scan_evidence_supplements_nonempty_software_response(monkeypatch, tmp_path):
    from src.services.software_market import SoftwareMarketClient
    cutoff = CUTOFF.replace(hour=10, minute=30)
    rows = session('2026-10-09')[:60]
    missing = rows[30]
    class Client:
        available = True
        def bars(self, *args, **kwargs):
            return {'bars': [row for row in rows if row != missing], 'time_semantics': 'bar_end'}
    monkeypatch.setattr(SoftwareMarketClient, 'from_environment', lambda: Client())
    monkeypatch.setattr(minute, '_prior_sessions', lambda *a: [])
    monkeypatch.setattr(minute, 'fetch_sina_bars', lambda *a, **k: [{**row, 'source': 'sina'} for row in rows])
    result = minute.fetch_bar_evidence('600000', cutoff, tmp_path, required_days=['2026-10-09'])
    assert result['softwareMarketUsed']
    assert result['supplementalMinuteFetch']['addedMinutes'] == 1
    assert result['requiredCoverage']['missingMinutes'] == 0


def test_failed_history_claim_can_retry_same_day_but_concurrent_claim_cannot(tmp_path):
    archive = minute.MinuteArchive(tmp_path)
    args = ('600000', CUTOFF.date().isoformat(), 'software_history')
    assert archive.claim_attempt(*args)
    assert not archive.claim_attempt(*args)
    archive.record(*args, 'failed')
    assert archive.claim_attempt(*args)
    archive.record(*args, 'partial')
    assert archive.claim_attempt(*args)
    archive.record(*args, 'empty')
    assert not archive.claim_attempt(*args)


def test_required_coverage_reports_optional_fields_separately_from_price_path():
    rows = [{**row, 'amount_cny': None, 'volume_shares': None} for row in session(DAYS[0])]
    coverage = minute.minute_coverage(rows, CUTOFF, DAYS[:1])
    assert coverage['pricePathComplete'] and coverage['missingMinutes'] == 0
    assert coverage['missingFields'] == {'amount_cny': 240, 'volume_shares': 240}


def test_incomplete_success_can_retry_after_five_minutes_without_all_day_lock(monkeypatch, tmp_path):
    clock = [1000.0]
    monkeypatch.setattr(minute, 'wall_time', lambda: clock[0])
    archive = minute.MinuteArchive(tmp_path)
    args = ('600000', CUTOFF.date().isoformat(), 'software_history')
    archive.record(*args, 'empty')
    assert not archive.claim_attempt(*args, retry_incomplete=True)
    clock[0] += 301
    assert archive.claim_attempt(*args, retry_incomplete=True)
    assert not archive.claim_attempt(*args, retry_incomplete=True)


def test_sina_fetch_records_actual_receipt_time_separately_from_bar_time():
    class Response:
        text = '=([{"day":"2026-09-30 10:30:00","open":10,"close":10,"high":10,"low":10,"amount":1000,"volume":100}]);'
        def raise_for_status(self): pass
    before = datetime.now(minute.SHANGHAI)
    row = minute.fetch_sina_bars('600000', getter=lambda *a, **k: Response())[0]
    after = datetime.now(minute.SHANGHAI)
    assert before <= datetime.fromisoformat(row['fetched_at']) <= after
    assert row['end'] == '2026-09-30 10:30:00'


def test_partial_ten_day_baseline_keeps_full_history_request_until_twenty_ready(monkeypatch, tmp_path):
    from src.services.software_market import SoftwareMarketClient
    cutoff = CUTOFF.replace(hour=10, minute=30)
    days = [datetime(2026, 9, day).date() for day in range(1, 21)]
    historical = [row for day in days for row in session(day.isoformat())[57:60]]
    current = session('2026-10-09')[:60]
    minute.MinuteArchive(tmp_path).save('600000', historical[:30] + current, cutoff)
    requested = []
    class Client:
        available = True
        def bars(self, code, **kwargs):
            requested.append(kwargs['count'])
            rows = historical + current if kwargs['count'] > 240 else current
            return {'bars': rows, 'time_semantics': 'bar_end'}
    monkeypatch.setattr(SoftwareMarketClient, 'from_environment', lambda: Client())
    monkeypatch.setattr(minute, '_prior_sessions', lambda *a: days)
    result = minute.fetch_bar_evidence('600000', cutoff, tmp_path, backfill=False)
    assert requested == [8000]
    assert result['historyDays'] == 20 and result['baselineReady']


@pytest.mark.parametrize('repair_source', ['software', 'sina', 'history_page'])
def test_twenty_amount_days_with_nineteen_price_days_still_repairs_history(monkeypatch, tmp_path, repair_source):
    from src.services.software_market import SoftwareMarketClient
    cutoff = CUTOFF.replace(hour=10, minute=30)
    days = [datetime(2026, 9, day).date() for day in range(1, 21)]
    historical = [row for day in days for row in session(day.isoformat())[56:60]]
    current = session('2026-10-09')[:60]
    # 10:28..10:30 retain the full amount window, but absent 10:27 makes
    # the oldest day's 3-minute price return unavailable.
    missing = historical[0]
    partial = historical[1:] + current
    monkeypatch.setattr(minute, '_prior_sessions', lambda *a: days)
    archive = minute.MinuteArchive(tmp_path)
    archive.save('600000', partial, cutoff)
    before = minute.compute_bar_evidence(partial, cutoff)
    assert before['historyDays'] == 20 and before['priceHistoryDays'] == 19
    requests, public_requests = [], []
    class Client:
        available = True
        def bars(self, code, **kwargs):
            requests.append(kwargs)
            rows = [missing] if kwargs.get('end') else historical + current if repair_source == 'software' else partial
            return {'bars': rows, 'time_semantics': 'bar_end'}
    def public(*args, **kwargs):
        public_requests.append(True)
        return [missing] if repair_source == 'sina' else []
    monkeypatch.setattr(SoftwareMarketClient, 'from_environment', lambda: Client())
    monkeypatch.setattr(minute, 'fetch_sina_bars', public)
    result = minute.fetch_bar_evidence('600000', cutoff, tmp_path)
    assert requests[0]['count'] == 8000
    assert result['historyDays'] == result['priceHistoryDays'] == 20
    assert bool(public_requests) == (repair_source != 'software')
    if repair_source == 'history_page':
        assert len(requests) == 2 and requests[1]['end']
        assert result['historyBackfill']['status'] == 'fetched'
        assert result['historyBackfill']['newBars'] == 1
    else:
        assert result['historyBackfill']['status'] == 'not_needed'
