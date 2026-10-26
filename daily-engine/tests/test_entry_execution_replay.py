"""Replay the real frozen ledger, including slippage crossing the entry cap."""
from datetime import date, timedelta
import pytest

from tests.test_signal_learning import _clock, _candidate, _bars
from src.services.yao_scout.signal_learning import SignalLearningService
from src.services.yao_scout.entry_execution_policy import policy_contract


@pytest.mark.parametrize('maximum, expected', [(10.05, 'mature'), (10.00, 'no_fill')])
def test_frozen_plan_reaches_actual_execution_simulator(tmp_path, maximum, expected):
    days = [date(2026, 9, 7) + timedelta(days=n) for n in range(6)]
    now = _clock(days[0])
    row = _candidate('000001', days[0])
    row['entryExecution'] = {**policy_contract(), 'state': 'ready',
        'triggerAt': now.isoformat(), 'triggerPrice': 10.,
        'minimumPrice': 9.95, 'maximumPrice': maximum, 'invalidationPrice': 9.8,
        'expiresAt': (now+timedelta(minutes=5)).isoformat(), 'targetSession': days[1].isoformat(),
        'priceEligible': True, 'modelEligible': False, 'entryEligible': False}
    service = SignalLearningService(tmp_path, bar_fetcher=lambda code, cutoff: _bars(days),
        calendar_provider=lambda a,b: [d for d in days if a <= d <= b], clock=lambda: now)
    service.persist_scan({'status': 'completed_observations', 'candidates': [row]}, '0940', now, True)
    service.review_due(_clock(days[1], 15, 30))
    outcome = next(item for item in service.reviews()['items'] if item['horizon'] == 1)
    assert outcome['status'] == expected
    if expected == 'no_fill':
        assert outcome['reason'] == 'entry_price_outside_frozen_band'
    else:
        assert outcome['entryPrice'] > 10.0
