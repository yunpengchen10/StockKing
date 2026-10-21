"""Point-in-time A-share emotion axes remain descriptive shadow evidence."""
from datetime import datetime, timedelta
import json
import sqlite3

import pandas as pd
import pytest

from src.services.yao_scout.research_factors import MarketEmotionArchive, TZ

NOW = datetime(2026, 9, 24, 10, 30, tzinfo=TZ)


def calendar(start, end):
    return [stamp.date() for stamp in pd.bdate_range(start, end)]


def frame(at, changes=(10., 0., -1.), previous=(10., 10., 10.), highs=None):
    rows = []
    for index, (change, prior) in enumerate(zip(changes, previous)):
        price = round(prior * (1 + change / 100), 2)
        rows.append({'code': f'60000{index+1}', 'name': f'正常样本{index}',
                     'price': price, 'previous_close': prior,
                     'high': highs[index] if highs is not None else max(price, prior),
                     'limit_up': round(prior * 1.1, 2), 'limit_down': round(prior * .9, 2)})
    result = pd.DataFrame(rows)
    result.attrs.update(snapshot_source='test-observed-provider', source_time=at.isoformat())
    return result


def archive(tmp_path, **policy):
    return MarketEmotionArchive(tmp_path, calendar_provider=calendar,
        policy={'minimumUniverse': 3, 'historySessions': 2, **policy})


def observe(store, at, **kwargs):
    return store.observe(frame(at, **kwargs), at, received_at=at)


def seed(store):
    # Both historical same-slot cohorts are based on actual preceding closes.
    observe(store, NOW - timedelta(days=3) + timedelta(hours=4, minutes=30))
    observe(store, NOW - timedelta(days=2), changes=(0., 0., -1.), previous=(11., 10., 10.),
            highs=(12.1, 10., 10.))
    observe(store, NOW - timedelta(days=2) + timedelta(hours=4, minutes=30))
    observe(store, NOW - timedelta(days=1), changes=(10., 0., -1.), previous=(11., 10., 10.))
    observe(store, NOW - timedelta(days=1) + timedelta(hours=4, minutes=30))


def test_default_is_sixty_sessions_and_missing_does_not_turn_into_zero(tmp_path):
    store = archive(tmp_path, historySessions=60)
    result = observe(store, NOW)
    diagnostics = result['emotionDiagnostics']
    assert diagnostics['shadowOnly'] is True
    assert diagnostics['baseline']['sessions'] == 60
    assert result['score'] is None
    premium = diagnostics['metrics']['previous_limit_premium_pct']
    assert premium['value'] is None and not premium['available']
    assert premium['historySampleCount'] == 0
    assert premium['historyMean'] is premium['empiricalPercentile'] is None
    bad = frame(NOW)
    bad.attrs['stale'] = True
    missing = store.observe(bad, NOW, received_at=NOW)['emotionDiagnostics']
    assert missing['status'] == 'unavailable'
    assert all(item['value'] is None for item in missing['metrics'].values())


def test_six_independent_axes_have_strict_samples_directions_and_midrank(tmp_path):
    store = archive(tmp_path)
    seed(store)
    result = observe(store, NOW, changes=(5., 0., -1.), previous=(11., 10., 10.),
                     highs=(12.1, 10., 10.))
    diagnostics = result['emotionDiagnostics']
    assert diagnostics['status'] == 'observed'
    assert len(diagnostics['metrics']) == 6
    assert 'score' not in diagnostics and diagnostics['shadowOnly']
    premium = diagnostics['metrics']['previous_limit_premium_pct']
    assert premium['value'] == pytest.approx(5)
    assert premium['historyMean'] == pytest.approx(5)
    assert premium['historySampleCount'] == 2
    assert premium['empiricalPercentile'] == 50
    broken = diagnostics['metrics']['broken_board_pct']
    assert broken['value'] == 100 and broken['currentSampleCount'] == 1
    assert broken['empiricalPercentile'] == 75 and broken['favorablePercentile'] == 25
    assert broken['favorableDirection'] == 'lower' and '不是炸板次数' in broken['definition']
    assert diagnostics['metrics']['limit_down_pct']['favorableDirection'] == 'lower'
    assert diagnostics['metrics']['previous_limit_promotion_pct']['favorableDirection'] == 'higher'
    # Existing score still contains precisely the same three breadth votes.
    ranks = [diagnostics['metrics'][key]['favorablePercentile']
             for key in ('advancing_pct', 'limit_up_pct', 'limit_down_pct')]
    assert result['score'] == pytest.approx(sum(ranks) / 3, abs=1e-6)


@pytest.mark.parametrize('mutation', [
    'absent_limits', 'no_price_limit', 'corporate_action', 'suspended',
    'derived_evidence', 'future_evidence', 'conflicting_evidence', 'boolean_limit',
])
def test_missing_exceptional_or_inferred_limits_never_make_limit_statistics(tmp_path, mutation):
    store = archive(tmp_path)
    quote = frame(NOW)
    if mutation == 'absent_limits':
        quote = quote.drop(columns=['limit_up', 'limit_down'])
    elif mutation in {'no_price_limit', 'corporate_action', 'suspended'}:
        quote[mutation] = True
    elif mutation == 'boolean_limit':
        quote['limit_up'] = True
    else:
        evidence = {'method': 'explicit_provider_fields', 'status': 'observed', 'source': 'test',
                    'source_time': NOW.isoformat(), 'previous_close': 10., 'limit_up': 11., 'limit_down': 9.}
        if mutation == 'derived_evidence':
            evidence['method'] = 'estimated_from_previous_close'
        elif mutation == 'future_evidence':
            evidence['source_time'] = (NOW + timedelta(seconds=1)).isoformat()
        else:
            evidence['previous_close'] = 9.
        quote['price_limit_evidence'] = [dict(evidence) for _ in range(3)]
    result = store.observe(quote, NOW, received_at=NOW)
    assert result['features']['advancing_pct'] == pytest.approx(100/3)
    for key in ('limit_up_pct', 'limit_down_pct', 'broken_board_pct'):
        assert key not in result['features']
        assert result['emotionDiagnostics']['metrics'][key]['value'] is None


def test_high_only_describes_current_unsealed_state_and_invalid_high_is_unknown(tmp_path):
    store = archive(tmp_path)
    result = observe(store, NOW, changes=(5., 0., -1.), highs=(11., 10., 10.))
    assert result['features']['broken_board_pct'] == 100
    assert 'break_count' not in result['features']
    quote = frame(NOW, changes=(5., 0., -1.), highs=(12., 12., 12.))
    result = store.observe(quote, NOW, received_at=NOW)
    assert result['emotionDiagnostics']['metrics']['broken_board_pct']['value'] is None


def test_future_receipts_same_day_replays_and_other_slots_do_not_fill_history(tmp_path):
    store = archive(tmp_path)
    seed(store)
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE observations SET received_at=? WHERE trading_day=? AND slot=?',
                   ((NOW + timedelta(seconds=1)).isoformat(), '2026-09-23', '10:30'))
    result = observe(store, NOW, previous=(11., 10., 10.))
    metric = result['emotionDiagnostics']['metrics']['previous_limit_premium_pct']
    assert metric['historySampleCount'] == 1
    assert metric['historyMean'] is metric['empiricalPercentile'] is None
    assert result['score'] is None
    repeat = observe(store, NOW, previous=(11., 10., 10.))
    assert repeat['emotionDiagnostics']['metrics']['previous_limit_premium_pct']['historySampleCount'] == 1
    shifted = observe(store, NOW + timedelta(minutes=5), previous=(11., 10., 10.))
    assert shifted['emotionDiagnostics']['metrics']['advancing_pct']['historySampleCount'] == 0


def test_legacy_estimated_limit_archive_is_not_reused_as_explicit_evidence(tmp_path):
    store = archive(tmp_path)
    seed(store)
    with sqlite3.connect(store.path) as db:
        for digest, payload in db.execute('SELECT digest,payload FROM observations').fetchall():
            packet = json.loads(payload)
            packet.pop('measurementVersion')
            db.execute('UPDATE observations SET payload=? WHERE digest=?', (json.dumps(packet), digest))
    result = observe(store, NOW, previous=(11., 10., 10.))
    assert result['score'] is None
    assert result['emotionDiagnostics']['metrics']['advancing_pct']['historySampleCount'] == 0
    assert 'previous_limit_premium_pct' not in result['features']


def test_late_cohort_cannot_be_backdated_into_a_persisted_snapshot(tmp_path):
    store = archive(tmp_path)
    previous = NOW - timedelta(days=1) + timedelta(hours=4, minutes=30)
    observe(store, previous)
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE observations SET received_at=?', ((NOW + timedelta(minutes=1)).isoformat(),))
    cutoff = NOW + timedelta(minutes=2)
    result = store.observe(frame(NOW, previous=(11., 10., 10.)), cutoff, received_at=NOW)
    assert 'previous_limit_premium_pct' not in result['features']
    replay = store.observe(frame(NOW, previous=(11., 10., 10.)), NOW, received_at=NOW)
    assert replay['emotionDiagnostics']['metrics']['previous_limit_premium_pct']['value'] is None


def test_invalid_current_trading_calendar_never_produces_history_ranks(tmp_path):
    store = archive(tmp_path)
    seed(store)
    store.calendar = lambda start, end: calendar(start, end - timedelta(days=1))
    result = observe(store, NOW, previous=(11., 10., 10.))
    assert result['score'] is None
    assert result['emotionDiagnostics']['metrics']['advancing_pct']['historySampleCount'] == 0
