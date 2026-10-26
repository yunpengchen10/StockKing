from copy import deepcopy
from datetime import datetime, date, timedelta
from src.services.yao_scout.entry_execution_policy import (
    annotate_entry_plans, execution_entry_allowed, replay_entry_check, TZ)


NOW = datetime(2026, 10, 26, 10, 0, 5, tzinfo=TZ)


def candidate(price=10):
    return {'code': '600000', 'quote': {'code': '600000', 'price': price, 'source': 'fixture',
            'source_time': NOW.isoformat()}, 'primaryAdmission': {'eligible': True},
            'entryRiskFloorAtr': .4, 'indicatorEvidence': [{'key': 'support', 'value': 9.8}],
            'prediction': {'taskId': 'entry_today_exit_next_session', 'status': 'active',
                           'entryEligible': True, 'expectedNetReturn': .01, 'utility': .01, 'winProbability': .7}}


def annotate(row, path, now=NOW):
    return annotate_entry_plans([row], now, path,
        calendar_provider=lambda a, b: [date(2026, 10, 26), date(2026, 10, 27)])[0]


def test_first_trigger_and_chase_limit_never_reset_on_refresh(tmp_path):
    first = annotate(candidate(), tmp_path)
    assert execution_entry_allowed(first)
    assert first['entryExecution']['maximumPrice'] == 10.05
    later = candidate(10.1)
    later['quote']['source_time'] = (NOW+timedelta(minutes=1)).isoformat()
    repeated = annotate(later, tmp_path, NOW+timedelta(minutes=1))
    assert repeated['entryExecution']['state'] == 'above_entry_band'
    assert repeated['entryExecution']['triggerPrice'] == 10
    assert repeated['entryExecution']['triggerAt'] == NOW.isoformat()
    assert not execution_entry_allowed(repeated)


def test_invalidation_and_expiry_are_not_rearmed(tmp_path):
    annotate(candidate(), tmp_path)
    low = annotate(candidate(9.7), tmp_path)
    assert low['entryExecution']['state'] == 'entry_plan_invalidated'
    assert not execution_entry_allowed(annotate(candidate(), tmp_path))
    fresh_path = tmp_path/'expiry'
    annotate(candidate(), fresh_path)
    now = NOW + timedelta(minutes=6)
    row = candidate(); row['quote']['source_time'] = now.isoformat()
    assert annotate(row, fresh_path, now)['entryExecution']['state'] == 'entry_plan_expired'


def test_research_trigger_does_not_require_model_and_replay_checks_actual_fill(tmp_path):
    row = candidate(); row['prediction']['status'] = 'rules_unvalidated'
    plan = annotate(row, tmp_path)['entryExecution']
    assert plan['priceEligible'] and not plan['entryEligible']
    end = NOW.replace(second=0)+timedelta(minutes=2)
    assert replay_entry_check(plan, 10.02, end) is None
    assert replay_entry_check(plan, 10.06, end) == 'entry_price_outside_frozen_band'
    assert replay_entry_check(plan, 10.02, NOW.replace(second=0)+timedelta(minutes=1))
    assert replay_entry_check(plan, 10.02, NOW+timedelta(minutes=6))
    assert replay_entry_check(None, 10.02, end) is None


def test_missing_stale_calendar_and_nontrading_session_fail_closed(tmp_path):
    row = candidate(); row['entryRiskFloorAtr'] = None
    assert not execution_entry_allowed(annotate(row, tmp_path))
    row = candidate(); row['quote']['source_time'] = (NOW-timedelta(seconds=31)).isoformat()
    assert annotate(row, tmp_path)['entryExecution']['state'] == 'entry_quote_unverified'
    row = annotate_entry_plans([candidate()], NOW, tmp_path, calendar_provider=lambda a,b: [])[0]
    assert row['entryExecution']['state'] == 'trading_calendar_unavailable'
    assert not execution_entry_allowed(annotate(candidate(), tmp_path, NOW.replace(hour=12)))


def test_slippage_is_checked_against_frozen_band_not_live_selection(tmp_path):
    plan = annotate(candidate(), tmp_path)['entryExecution']
    end = NOW.replace(second=0)+timedelta(minutes=2)
    assert replay_entry_check(plan, 10.05, end) is None
    assert replay_entry_check(plan, 10.06, end) is not None


def test_big_move_head_cannot_replace_missing_profitable_probability(tmp_path):
    row = candidate()
    row['prediction'].pop('winProbability')
    row['prediction']['bigMoveProbability'] = .9
    assert not execution_entry_allowed(annotate(row, tmp_path))
    for value in (.5, -1, 1.1, float('nan'), None):
        row['prediction']['winProbability'] = value
        assert not execution_entry_allowed(annotate(row, tmp_path))
