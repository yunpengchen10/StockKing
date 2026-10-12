"""History stays bounded; interrupted reviews preserve completed evidence."""
from copy import deepcopy
from datetime import date, timedelta
import json
import sqlite3

import pytest

from src.services.yao_scout import next_day_watch as watch
from src.services.yao_scout import signal_learning as ledger
from tests.test_signal_learning import _bars, _candidate, _clock
from tests.test_research_recommendation_persistence import scan as research_scan, NOW


DAY = date(2026, 9, 7)
DAYS = [DAY + timedelta(days=index) for index in range(6)]


def calendar(start, end):
    return [day for day in DAYS if start <= day <= end]


def test_compact_history_keeps_legacy_watch_projection_without_full_scan_decode(tmp_path, monkeypatch):
    service = ledger.SignalLearningService(tmp_path, clock=lambda: NOW)
    result = research_scan()
    control = deepcopy(result['candidates'][0])
    control.update(code='600009', indicatorEvidence={'raw': 'x' * 2_000_000})
    result['controls'] = [control]
    result['candidates'][0].update(entryRiskDistance=.3, entryRiskFloorAtr=.3,
        entryRiskBasis='ATR14', rawStructureRewardRisk=4.)
    service.persist_scan(result, 'live', NOW, False)
    with service._connect() as db:
        db.execute("DELETE FROM signal_events WHERE code IN ('600002','600003')")
        # Model the legacy bug where features_json held an entire candidate.
        db.execute("UPDATE signal_events SET features_json=? WHERE code='600001'",
                   (json.dumps({**result['candidates'][0], 'finalScore': 70.,
                                'factorScores': {'M': 30.}, 'indicatorEvidence': {'raw': 'y' * 2_000_000}}),))
    original_loads = ledger.json.loads
    def bounded_decode(value, *args, **kwargs):
        assert len(value) < 100_000, 'compact history decoded a full scan or raw evidence'
        return original_loads(value, *args, **kwargs)
    monkeypatch.setattr(ledger.json, 'loads', bounded_decode)
    before = service.db_path.read_bytes()
    compact = service.history({'compact': True, 'include_controls': False})
    rows = {item['code']: item for item in compact['items'][0]['signals']}
    assert set(rows) == {'600001', '600002', '600003'}
    assert rows['600002']['ledgerRecordOrigin'] == rows['600003']['ledgerRecordOrigin'] == 'saved_scan_projection'
    assert rows['600003']['nextDayContinuationSelected']
    assert rows['600001']['snapshot']['entryRiskDistance'] == .3
    assert rows['600001']['features'] == {'finalScore': 70., 'factor_M': 30.}
    assert len(json.dumps(compact)) < 15_000
    assert 'result' not in compact['items'][0]['scan']
    assert service.db_path.read_bytes() == before
    with_controls = service.history({'compact': True, 'include_controls': True})
    assert len(with_controls['items'][0]['signals']) == 4
    filtered = service.history({'compact': True, 'include_controls': False, 'symbol': '600003'})
    assert [item['code'] for item in filtered['items'][0]['signals']] == ['600003']


def test_review_fetch_does_not_hold_writer_lock_and_checkpoints_before_interruption(tmp_path, monkeypatch):
    calls = []
    service = ledger.SignalLearningService(tmp_path, clock=lambda: _clock(DAY), calendar_provider=calendar)
    service.persist_scan({'status': 'completed_observations',
                          'candidates': [_candidate('000001', DAY), _candidate('000002', DAY)]},
                         '0940', _clock(DAY), True)
    def fetch(code, cutoff):
        with sqlite3.connect(service.db_path, timeout=.1) as db:
            db.execute("INSERT OR REPLACE INTO learning_registry VALUES('lock-probe','{}',?)", (cutoff.isoformat(),))
        calls.append(code)
        return _bars(DAYS)
    service.bar_fetcher = fetch
    original_simulate = service._simulate_event
    def interrupted(event, *args):
        if event['code'] == '000002':
            raise RuntimeError('simulated process interruption')
        return original_simulate(event, *args)
    monkeypatch.setattr(service, '_simulate_event', interrupted)
    with pytest.raises(RuntimeError, match='process interruption'):
        service.review_due(_clock(DAYS[-1], 15, 30))
    assert calls == ['000001']
    with service._connect() as db:
        assert db.execute("SELECT count(*) FROM signal_outcomes WHERE status='mature'").fetchone()[0] == 3
    monkeypatch.setattr(service, '_simulate_event', original_simulate)
    assert service.review_due(_clock(DAYS[-1], 15, 31))['updated'] == 3


def test_execution_calendar_is_loaded_once_per_review_pass(tmp_path):
    calls = []
    def counted_calendar(start, end):
        calls.append((start, end))
        return calendar(start, end)
    service = ledger.SignalLearningService(tmp_path, clock=lambda: _clock(DAY), calendar_provider=counted_calendar,
                                           bar_fetcher=lambda *_: _bars(DAYS))
    service.persist_scan({'status': 'completed_observations',
                          'candidates': [_candidate('000001', DAY), _candidate('000002', DAY)]},
                         '0940', _clock(DAY), True)
    result = service.review_due(_clock(DAYS[-1], 15, 30))
    assert result['updated'] == 6 and result['statusCounts'] == {'mature': 6}
    assert calls == [(DAY, DAYS[-1])]


def test_next_day_review_does_not_fetch_unmatured_and_keeps_old_labels_when_supplemented(tmp_path):
    current = [_clock(DAY)]
    calls, calendars = [], []
    def counted_calendar(start, end):
        calendars.append((start, end))
        return calendar(start, end)
    service = ledger.SignalLearningService(tmp_path, clock=lambda: current[0], calendar_provider=counted_calendar)
    signal = {'version': 'next-day-limit-watch-v1', 'queue': 'legacy_mixed',
              'signalSession': DAY.isoformat(), 'selected': True}
    candidate = {'code': '000001', 'name': 'sample', 'referencePrice': 10., 'decision_at': _clock(DAY).isoformat(),
                 'quote': {'price': 10., 'source_time': _clock(DAY).isoformat()}, 'nextDaySignal': signal}
    service.persist_scan({'status': 'completed_observations', 'nextDayWatchlist': [candidate]},
                         '0940', _clock(DAY), True)
    def fetch(code, cutoff):
        with sqlite3.connect(service.db_path, timeout=.1) as db:
            db.execute("INSERT OR REPLACE INTO learning_registry VALUES('watch-probe','{}',?)", (cutoff.isoformat(),))
        calls.append(code)
        return _bars(DAYS)
    service.bar_fetcher = fetch
    assert watch.review_next_day_signals(service, current[0])['updated'] == 0
    assert calls == []
    current[0] = _clock(DAYS[1], 15, 30)
    watch.review_next_day_signals(service, current[0])
    with service._connect() as db:
        raw = db.execute('SELECT signal_id,reviewed_at,result_json FROM next_day_outcomes').fetchone()
        old = json.loads(raw['result_json'])
        for key in ('targetDayChangePct', 'targetClose', 'observedReturnFromSignalPct'):
            old.pop(key, None)
        old['touchLimit'] = True  # preserved legacy observation, never reclassified
        db.execute('UPDATE next_day_outcomes SET result_json=?', (json.dumps(old),))
    refreshed = watch.review_next_day_signals(service, current[0] + timedelta(minutes=1))
    item = refreshed['items'][0]
    assert item['version'] == 'next-day-limit-watch-v1' and item['touchLimit'] is True
    assert item['targetDayChangePct'] is not None and item['observedReturnFromSignalPct'] is not None
    summary = refreshed['summary']['groups'][0]
    assert summary['priceObservationCount'] == 1 and summary['meanObservedReturnFromSignalPct'] > 0
    with service._connect() as db:
        assert db.execute('SELECT reviewed_at FROM next_day_outcomes').fetchone()[0] == raw['reviewed_at']
    assert watch.review_next_day_signals(service, current[0] + timedelta(minutes=2))['updated'] == 0


def test_zero_rule_score_ranks_above_missing_and_negative(tmp_path):
    service = ledger.SignalLearningService(tmp_path)
    rows = [{'code': '000001', 'finalScore': None}, {'code': '000002', 'finalScore': 0.},
            {'code': '000003', 'finalScore': -1.}]
    assert [row['code'] for row in service.rank_candidates(rows)] == ['000002', '000003', '000001']


def test_review_summary_deduplicates_frozen_signal_day_and_keeps_losing_latest_observation():
    common = {'code': '000001', 'signalDate': DAY.isoformat(), 'version': 'next-day-limit-watch-v2',
              'queue': 'first_board', 'selected': True, 'status': 'mature_price_pattern',
              'touchLimit': False, 'closeAtLimit': False}
    rows = [{**common, 'decisionAt': _clock(DAY, 9, 40).isoformat(), 'observedReturnFromSignalPct': 2.},
            {**common, 'decisionAt': _clock(DAY, 10, 30).isoformat(), 'observedReturnFromSignalPct': -4.},
            {**common, 'selected': False, 'decisionAt': _clock(DAY, 10, 30).isoformat(),
             'observedReturnFromSignalPct': -4.},
            {**common, 'version': 'next-day-limit-watch-v3', 'status': 'incomplete'}]
    groups = watch._review_summary(rows)['groups']
    selected = next(row for row in groups if row['selected'] and row['version'].endswith('v2'))
    assert selected['recordCount'] == 2 and selected['signalCount'] == selected['maturePatterns'] == 1
    assert selected['downCount'] == 1 and selected['meanObservedReturnFromSignalPct'] == -4.
    unknown = next(row for row in groups if row['version'].endswith('v3'))
    assert unknown['maturePatterns'] == unknown['priceObservationCount'] == 0
    assert unknown['meanObservedReturnFromSignalPct'] is None
