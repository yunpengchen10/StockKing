"""Trend measurements reach the audit ledger without changing model authority."""
from copy import deepcopy
from datetime import timedelta

import pytest

from src.services.yao_scout.minute_history import compute_bar_evidence
from src.services.yao_scout.signal_learning import _feature_values, _snapshot, _COMPACT_KEYS
from src.services.yao_scout.v11_factors import score_v11
from tests.test_v11_factors import NOW, DATES, _bars, _candidate


def evidence():
    history = [row for day in DATES for row in _bars(day)]
    packet = compute_bar_evidence(history + _bars(NOW.date(), current=True), NOW,
                                  expected_dates=DATES)
    packet['dataLineage'] = {'status': 'verified'}
    return packet


def test_shadow_trend_survives_snapshot_without_entering_old_model_vector():
    minute = evidence()
    scored = score_v11(_candidate(), minute, now=NOW)
    assert scored['trendEvidence']['metrics']['signed_log_efficiency_15m'] == 1
    assert scored['trendEvidence']['status'] == 'partial'  # No full session VWAP/30m window.
    assert scored['trendEvidence']['selectionAuthority'] is False
    assert scored['trendEvidence']['mainFundsIdentified'] is False
    assert _snapshot(scored)['trendEvidence'] == scored['trendEvidence']
    assert 'trendEvidence' in _COMPACT_KEYS
    assert 'intradayDirectionEvidence' in _COMPACT_KEYS
    direction = {'intradayDirectionEvidence': {'passed': True, 'probability': None}}
    assert _snapshot(direction) == direction
    without = deepcopy(minute)
    without.pop('trendEvidence')
    old_score = score_v11(_candidate(), without, now=NOW)
    assert _feature_values(scored) == _feature_values(old_score)
    assert scored['finalScore'] == old_score['finalScore']
    assert scored['mainRiseProbability'] is None


@pytest.mark.parametrize('condition', ['tainted', 'unknown', 'future', 'stale', 'misaligned'])
def test_invalid_lineage_or_time_cannot_display_observed_trend_metrics(condition):
    minute = evidence()
    if condition in ('tainted', 'unknown'):
        minute['dataLineage']['status'] = condition
    elif condition == 'future':
        minute['asOf'] = minute['trendEvidence']['asOf'] = (NOW + timedelta(minutes=1)).isoformat()
    elif condition == 'stale':
        minute['asOf'] = minute['trendEvidence']['asOf'] = (NOW - timedelta(minutes=5)).isoformat()
    else:
        minute['trendEvidence']['asOf'] = (NOW - timedelta(minutes=1)).isoformat()
    original = deepcopy(minute)
    result = score_v11(_candidate(), minute, now=NOW)['trendEvidence']
    assert result['status'] == 'unavailable'
    assert all(value is None for value in result['metrics'].values())
    assert result['selectionAuthority'] is False
    assert minute == original  # Scoring must not rewrite archived measurements.


def test_absent_minute_window_is_explicitly_unavailable():
    result = score_v11(_candidate(), {}, now=NOW)['trendEvidence']
    assert result['status'] == 'unavailable'
    assert result['metrics'] == {}
