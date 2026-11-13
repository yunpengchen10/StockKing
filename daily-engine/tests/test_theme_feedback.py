"""Offline integration checks for frozen theme feedback; no browser or network."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import json
import sqlite3

import pytest

from src.services.yao_scout.theme_feedback import ThemeFeedbackStore, TZ


DAY = datetime(2026, 11, 9, 10, 30, tzinfo=TZ)
CODES = ['600001', '600002', '600003', '600004']


def calendar(start, end):
    return [start + timedelta(days=i) for i in range((end-start).days+1)
            if (start+timedelta(days=i)).weekday() < 5]


def quote(code, at, price=10):
    return {'code': code, 'source': 'fixture', 'source_time': at.isoformat(), 'price': price}


def scan(at=DAY, scan_id=None):
    return {'run_id': scan_id or at.isoformat(), 'asOf': at.isoformat(),
        'themeBoard': {'themes': [{'id': 'AI', 'name': '人工智能', 'rank': 1, 'heatScore': .8,
            'changePct': 2, 'breadthPct': 80, 'selected': True, 'sourceTime': at.isoformat(),
            'memberCodes': CODES, 'coverage': {'expectedCount': 4, 'complete': True, 'pct': 100}}]},
        'opportunityChannels': {'recommendations': [{'code': CODES[0]}]},
        'themeResearchUniverse': [{**quote(code, at), 'name': code, 'themeIds': ['AI'],
            'selected': code == CODES[0], 'researched': code in CODES[:2]} for code in CODES]}


def close_quotes(at=DAY, prices=(9.8, 9.9, 9.9, 10.5)):
    close = at.replace(hour=15, minute=0)
    return {code: quote(code, close, price) for code, price in zip(CODES, prices)}


@pytest.fixture
def store(tmp_path):
    return ThemeFeedbackStore(tmp_path, calendar_provider=calendar)


def settle(store, at=DAY, prices=(9.8, 9.9, 9.9, 10.5)):
    store.save_scan(scan(at), at)
    return store.review(at.replace(hour=15, minute=30), close_quotes(at, prices))


def test_complete_pool_preserves_unresearched_controls_and_describes_missed_leader(store):
    result = settle(store)
    assert result['status'] == 'reviewed'
    assert result['universeCount'] == result['observedCount'] == 4
    assert result['themes'][0]['selectedMeanChangePct'] == -2
    assert result['themes'][0]['meanObservationChangePct'] == .25
    assert {r['code'] for r in result['themes'][0]['attributions']} == {
        'selection_lagged', 'reference_pullback', 'missed_leader'}
    missed = result['missedLeaders'][0]
    assert missed['code'] == CODES[3] and missed['researched'] is False
    assert missed['observationChangePct'] == 5
    assert missed['referenceQuote']['source_time'] == DAY.isoformat()
    assert missed['closeQuote']['source_time'] == DAY.replace(hour=15, minute=0).isoformat()
    assert result['adjustments'] == {}
    assert result['modelWeightsChanged'] is result['trainingEligible'] is False
    assert '非成交收益' in result['meaning'] and '非因果' in result['meaning']


def test_first_valid_reference_selected_status_and_close_are_immutable(store):
    original = scan()
    store.save_scan(original, DAY)
    later = DAY + timedelta(minutes=20)
    changed = scan(later)
    changed['themeResearchUniverse'][0].update(price=20, selected=False)
    changed['opportunityChannels']['recommendations'] = []
    store.save_scan(changed, later)
    first = store.review(DAY.replace(hour=15, minute=30), close_quotes())
    repeat = store.review(DAY.replace(hour=15, minute=31), close_quotes(prices=(20, 20, 20, 20)))
    assert repeat['themes'] == first['themes']
    assert repeat['themes'][0]['selectedMeanChangePct'] == -2
    with sqlite3.connect(store.path) as db:
        row = json.loads(db.execute('SELECT payload FROM baselines WHERE code=?', (CODES[0],)).fetchone()[0])
    assert row['quote']['price'] == 10 and row['selected'] is True


def test_repeated_day_does_not_count_as_separate_learning_dates(store):
    for i in range(4):
        at = DAY + timedelta(minutes=i)
        store.save_scan(scan(at), at)
        feedback = store.review(DAY.replace(hour=15, minute=30)+timedelta(minutes=i),
                                close_quotes(prices=(9, 9, 9, 9)))
    assert feedback['themes'][0]['weakened'] is True
    assert feedback['adjustments'] == {}


def test_three_independent_days_change_next_day_priority_with_bounded_penalty(store):
    for i in range(3):
        feedback = settle(store, DAY+timedelta(days=i), prices=(9, 9, 9, 9))
        assert bool(feedback['adjustments']) == (i == 2)
    context = store.read_context(DAY+timedelta(days=3))
    assert context['feedback']['adjustments'] == {'AI': .06}
    assert len(context['history']) == 3
    # This explicit penalty is consumable without mutating model parameters.
    heat = {'AI': .8, 'robot': .77}
    ranked = sorted(heat, key=lambda key: -(heat[key]-context['feedback']['adjustments'].get(key, 0)))
    assert ranked == ['robot', 'AI']


def test_single_bad_day_and_repeated_missing_data_never_adjust_priority(store):
    settle(store, DAY, prices=(9, 9, 9, 9))
    settle(store, DAY+timedelta(days=1), prices=(11, 11, 11, 11))
    settle(store, DAY+timedelta(days=2), prices=(11, 11, 11, 11))
    assert store.read_context(DAY+timedelta(days=3))['feedback']['adjustments'] == {}


@pytest.mark.parametrize('patch', [
    {'source_time': None, 'fetched_at': DAY.replace(hour=15, minute=0).isoformat()},
    {'source_time': DAY.replace(hour=14, minute=56).isoformat()},
    {'source_time': DAY.replace(hour=15, minute=6).isoformat()},
    {'source_time': (DAY-timedelta(days=1)).replace(hour=15, minute=0).isoformat()},
    {'source_time': DAY.replace(hour=15, minute=31).isoformat()},
    {'source': ''}, {'price': 0}, {'price': float('nan')}, {'code': '600999'}, {'is_stale': True},
])
def test_missing_wrong_stale_future_or_non_close_quotes_do_not_create_results(store, patch):
    store.save_scan(scan(), DAY)
    quotes = close_quotes()
    quotes[CODES[0]].update(patch)
    result = store.review(DAY.replace(hour=15, minute=30), quotes)
    assert result['status'] == 'partial' and result['observedCount'] == 3
    assert result['missingCodes'] == [CODES[0]]
    assert result['themes'][0]['status'] == 'insufficient'
    assert result['missedLeaders'] == [] and result['adjustments'] == {}


def test_partial_review_can_fill_missing_close_without_changing_old_results(store):
    store.save_scan(scan(), DAY)
    at = DAY.replace(hour=15, minute=30)
    first = store.review(at, {CODES[0]: close_quotes()[CODES[0]]})
    assert first['observedCount'] == 1
    assert store.pending_codes(at, limit=2) == CODES[1:3]
    final = store.review(at+timedelta(minutes=1), close_quotes(prices=(15, 9.9, 9.9, 10.5)))
    assert final['status'] == 'reviewed'
    assert final['themes'][0]['selectedMeanChangePct'] == -2
    assert store.pending_codes(at) == CODES[1:]  # Later knowledge cannot leak backward.
    assert store.pending_codes(at+timedelta(minutes=2)) == []


def test_read_context_excludes_same_day_future_knowledge_and_expired_feedback(store):
    settle(store)
    assert store.read_context(DAY.replace(hour=16))['feedback']['status'] == 'no_history'
    assert store.read_context(DAY.replace(hour=16), include_today=True)['feedback']['status'] == 'reviewed'
    assert store.read_context(DAY.replace(hour=15, minute=0), include_today=True)['feedback']['status'] == 'no_history'
    tomorrow = DAY+timedelta(days=1)
    assert store.read_context(tomorrow)['feedback']['sourceDay'] == DAY.date().isoformat()
    settle(store, tomorrow, prices=(9, 9, 9, 9))
    assert store.read_context(tomorrow)['feedback']['sourceDay'] == DAY.date().isoformat()
    assert store.read_context(DAY+timedelta(days=10))['feedback']['status'] == 'expired'
    assert store.read_context(DAY+timedelta(days=10))['feedback']['adjustments'] == {}


def test_holiday_and_weekend_never_relabel_previous_quotes(tmp_path):
    holiday = DAY.date()+timedelta(days=1)
    store = ThemeFeedbackStore(tmp_path, lambda start, end: [d for d in calendar(start, end) if d != holiday])
    settle(store)
    for at in (DAY+timedelta(days=1), DAY+timedelta(days=5)):
        assert store.save_scan(scan(at), at)['status'] == 'rejected'
        assert store.review(at.replace(hour=15, minute=30), close_quotes())['status'] == 'unavailable'
        assert store.pending_codes(at.replace(hour=15, minute=30)) == []


def test_empty_calendar_fails_closed_and_naive_clock_is_rejected(tmp_path):
    store = ThemeFeedbackStore(tmp_path, lambda *_: [])
    assert store.save_scan(scan(), DAY)['status'] == 'rejected'
    assert store.review(DAY.replace(hour=15, minute=30), close_quotes())['status'] == 'unavailable'
    with pytest.raises(ValueError):
        store.read_context(DAY.replace(tzinfo=None))


def test_invalid_baseline_is_saved_as_gap_without_fabricated_reference(store):
    run = scan()
    run['themeResearchUniverse'][0]['source_time'] = None
    run['themeResearchUniverse'][1]['source_time'] = (DAY+timedelta(seconds=1)).isoformat()
    saved = store.save_scan(run, DAY)
    assert saved['validReferenceCount'] == 2
    feedback = store.review(DAY.replace(hour=15, minute=30), close_quotes())
    assert feedback['universeCount'] == 4 and feedback['observedCount'] == 2
    assert feedback['missingCodes'] == CODES[:2]


def test_first_valid_reference_does_not_rewrite_earlier_incomplete_cohort(store):
    run = scan()
    run['themeResearchUniverse'][0]['source_time'] = None
    store.save_scan(run, DAY)
    later = DAY+timedelta(minutes=10)
    store.save_scan(scan(later), later)
    feedback = store.review(DAY.replace(hour=15, minute=30), close_quotes())
    assert feedback['observedCount'] == 4
    assert feedback['themes'][0]['observedCount'] == 3
    assert feedback['themes'][0]['status'] == 'insufficient'
    assert feedback['themes'][0]['selectedMeanChangePct'] is None


def test_incomplete_candidate_universe_never_claims_market_missed_leaders(store):
    run = scan()
    pool = run.pop('themeResearchUniverse')
    run['opportunityChannels']['researchCandidates'] = pool
    store.save_scan(run, DAY)
    result = store.review(DAY.replace(hour=15, minute=30), close_quotes())
    assert result['missedLeaders'] == []
    assert any('完整候选池' in gap for gap in result['gaps'])


def test_close_board_future_timestamp_cannot_supply_continuation_evidence(store):
    store.save_scan(scan(), DAY)
    board = deepcopy(scan()['themeBoard'])
    board['themes'][0].update(rank=3, sourceTime=DAY.replace(hour=15, minute=1).isoformat())
    result = store.review(DAY.replace(hour=15, minute=0), close_quotes(), board)
    assert result['themes'][0]['closeRank'] is None
    result = store.review(DAY.replace(hour=15, minute=30), close_quotes(), board)
    assert result['themes'][0]['closeRank'] == 3


def test_concurrent_duplicate_scans_and_reviews_are_atomic_and_idempotent(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        saved = list(pool.map(lambda _: store.save_scan(scan(), DAY), range(8)))
    assert sum(r['status'] == 'saved' for r in saved) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.review(DAY.replace(hour=15, minute=30), close_quotes()), range(8)))
    assert all(r == results[0] for r in results)
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM scans').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM baselines').fetchone()[0] == 4
        assert db.execute('SELECT COUNT(*) FROM closes').fetchone()[0] == 4
        assert db.execute('SELECT COUNT(*) FROM feedback').fetchone()[0] == 1


def test_future_scan_or_bad_timestamp_is_rejected_and_pending_feedback_not_reused(store):
    assert store.save_scan(scan(DAY+timedelta(seconds=1)), DAY)['status'] == 'rejected'
    run = scan()
    run['asOf'] = 'invalid'
    assert store.save_scan(run, DAY)['status'] == 'rejected'
    store.save_scan(scan(), DAY)
    assert store.review(DAY.replace(hour=15, minute=30), {})['status'] == 'pending_data'
    assert store.read_context(DAY+timedelta(days=1))['feedback']['status'] == 'no_history'
    visible = store.read_context(DAY.replace(hour=16), include_today=True)['feedback']
    assert visible['status'] == 'pending_data' and visible['gaps']
    assert visible['missingCodes'] == CODES and visible['adjustments'] == {}


def test_close_before_frozen_reference_cannot_create_backwards_observation(store):
    at = DAY.replace(hour=15, minute=2)
    store.save_scan(scan(at), at)
    result = store.review(at.replace(minute=30), close_quotes())
    assert result['status'] == 'pending_data'


def test_read_context_and_pending_queries_never_create_or_mutate_files(tmp_path):
    directory = tmp_path/'not-created'
    store = ThemeFeedbackStore(directory, calendar)
    assert store.read_context(DAY)['feedback']['status'] == 'no_history'
    assert store.pending_codes(DAY.replace(hour=15, minute=30)) == []
    assert not directory.exists()
    settle(store)
    before = store.path.read_bytes()
    assert store.read_context(DAY+timedelta(days=1))['feedback']['status'] == 'reviewed'
    assert store.pending_codes(DAY.replace(hour=15, minute=30)) == []
    assert store.path.read_bytes() == before


def test_restart_retains_frozen_observations_and_adjustments_expire_with_their_evidence(store):
    for i in range(3):
        settle(store, DAY+timedelta(days=i), prices=(9, 9, 9, 9))
    restarted = ThemeFeedbackStore(store.path.parent, calendar)
    assert restarted.read_context(DAY+timedelta(days=3))['feedback']['adjustments'] == {'AI': .06}
    # Latest review is only six days old, but its first evidence day is eight
    # days old. The saved historical penalty is no longer usable.
    context = restarted.read_context(DAY+timedelta(days=8))
    assert context['feedback']['status'] == 'reviewed'
    assert context['feedback']['adjustments'] == {}


def test_history_preserves_board_contract_and_actual_decision_after_scan_start(store):
    from src.services.yao_scout.theme_rotation import VERSION, _history_rows
    started = DAY-timedelta(minutes=5)
    run = scan()
    run['cutoff_at'] = started.isoformat()
    run['asOf'] = started.isoformat()
    run['opportunityChannels']['asOf'] = DAY.isoformat()
    run['themeBoard'].update(version=VERSION, status='observed', asOf=DAY.isoformat(),
        sourceTime=DAY.isoformat(), source='fixture')
    coverage = {'knownMembers': 4, 'expectedMembers': 4, 'fraction': 1}
    run['themeBoard']['themes'][0]['coverage'] = coverage
    assert store.save_scan(run, DAY)['validReferenceCount'] == 4
    history = store.read_context(DAY+timedelta(days=1))['history']
    assert len(_history_rows(history, DAY+timedelta(days=1))) == 1
    assert history[0]['version'] == VERSION and history[0]['status'] == 'observed'
    assert history[0]['themes'][0]['coverage'] == coverage
    assert history[0]['themes'][0]['selected'] is True


@pytest.mark.parametrize('conflict,expected', [(False, 'reviewed'), (True, 'pending_data')])
def test_actual_public_quote_adapter_close_reference_exception(store, conflict, expected):
    from src.services.public_market_quotes import adapt_public_quote
    store.save_scan(scan(), DAY)
    quotes = {}
    at = DAY.replace(hour=15, minute=30)
    for code, raw in close_quotes().items():
        source = {**raw, 'previous_close': 10, 'open': 10, 'volume_shares': 100, 'amount_cny': 1000}
        issues = ['stale_quote', 'request_phase_post_close', 'source_phase_post_close', 'post_close_cannot_reconstruct_1455']
        if conflict:
            issues.append('cross_source_price_conflict')
        assessment = {'code': code, 'quote': source, 'status': 'reference_only', 'phase': 'post_close',
            'same_market_date': True, 'issues': issues, 'quote_usable_for_current_price_check': False,
            'execution_verified': False}
        adapted = adapt_public_quote({'checked_at': at.isoformat(), 'quotes': [assessment]}, assessment)
        adapted['stale'] = True  # Both supported spellings must honour the explicit exception.
        quotes[code] = adapted
    assert store.review(at, quotes)['status'] == expected


def test_later_selected_stock_is_not_reported_as_missed_or_repriced_as_early_pick(store):
    store.save_scan(scan(), DAY)
    later = DAY+timedelta(hours=3)
    run = scan(later)
    run['opportunityChannels']['recommendations'].append({'code': CODES[3]})
    run['themeResearchUniverse'][3]['price'] = 10.4
    store.save_scan(run, later)
    result = store.review(DAY.replace(hour=15, minute=30), close_quotes())
    assert result['missedLeaders'] == []
    assert result['themes'][0]['laterSelectedCodes'] == [CODES[3]]
    assert result['themes'][0]['selectedMeanChangePct'] == -2
    assert '首轮' in result['themes'][0]['selectionScope']


def test_outside_eligible_stock_pool_informs_theme_but_cannot_be_missed_pick(store):
    run = scan()
    run['themeResearchUniverse'][3]['eligibleUniverse'] = False
    store.save_scan(run, DAY)
    result = store.review(DAY.replace(hour=15, minute=30), close_quotes())
    assert result['themes'][0]['observedCount'] == 4
    assert result['themes'][0]['meanObservationChangePct'] == .25
    assert result['missedLeaders'] == []


def test_uncollected_non_mainline_catalogue_is_preserved_without_false_coverage_gaps(store):
    run = scan()
    run['themeBoard']['themes'] += [{'id': f'OTHER{i}', 'name': f'未采集概念{i}',
        'selected': False, 'sourceTime': DAY.isoformat(), 'memberCodes': [], 'coverage': None}
        for i in range(100)]
    store.save_scan(run, DAY)
    feedback = store.review(DAY.replace(hour=15, minute=30), close_quotes())
    assert [t['id'] for t in feedback['themes']] == ['AI']
    assert feedback['gaps'] == []
    assert len(store.read_context(DAY+timedelta(days=1))['history'][0]['themes']) == 101


def test_observed_unselected_theme_is_sample_only_and_never_claims_full_theme_breadth(store):
    run = scan()
    other = {**deepcopy(run['themeBoard']['themes'][0]), 'id': 'OTHER', 'selected': False}
    run['themeBoard']['themes'].append(other)
    for row in run['themeResearchUniverse']:
        row['themeIds'].append('OTHER')
    store.save_scan(run, DAY)
    feedback = store.review(DAY.replace(hour=15, minute=30), close_quotes(prices=(9, 9, 9, 9)))
    sample = next(t for t in feedback['themes'] if t['id'] == 'OTHER')
    assert sample['status'] == 'sample_only' and sample['scope'] == 'candidate_observations'
    assert sample['coverage'] is None and sample['observedCount'] == 4
    assert sample['weakened'] is False and sample['continuation'] == 'unknown'
    assert feedback['gaps'] == []


@pytest.mark.parametrize('complete', [True, False])
def test_selected_theme_cannot_replace_whole_membership_with_small_candidate_pool(store, complete):
    run = scan()
    theme = run['themeBoard']['themes'][0]
    theme['memberCodes'] = [*CODES, '600005', '600006', '600007', '600008', '600009', '600010']
    theme['coverage'] = {'complete': complete, 'expectedCount': 10}
    store.save_scan(run, DAY)
    feedback = store.review(DAY.replace(hour=15, minute=30), close_quotes(prices=(9, 9, 9, 9)))
    theme = feedback['themes'][0]
    assert theme['status'] == 'insufficient' and not theme['weakened']
    assert theme['coverage'] == (.4 if complete else None)
    assert theme['memberCount'] == (10 if complete else 4)


def test_saved_history_and_three_day_feedback_really_drive_theme_board_continuation_and_ranking(store):
    from src.services.yao_scout import theme_rotation as tr

    def packet(at):
        rows, members = [], {}
        for i in range(20):
            tid = f'BK{1000+i}'
            codes = CODES if i == 0 else [str(601000+i*10+j) for j in range(4)]
            rows.append({'id': tid, 'name': tid, 'changePct': 20-i, 'amount': 1e8,
                'upCount': 8, 'downCount': 2, 'flatCount': 0, 'sourceTime': at.isoformat()})
            members[tid] = {'source': tr.SOURCE, 'knownAt': at.isoformat(), 'complete': True,
                'expectedCount': 4, 'rows': [{'code': code, 'sourceTime': at.isoformat(), 'changePct': 20-i} for code in codes]}
        return {'version': tr.VERSION, 'kind': 'concept', 'source': tr.SOURCE, 'status': 'observed',
            'checkedAt': at.isoformat(), 'rows': rows, 'members': members,
            'coverage': {'expectedCount': 20, 'receivedCount': 20, 'complete': True}}

    for i in range(3):
        at = DAY+timedelta(days=i)
        context = store.read_context(at)
        board = tr.build_theme_board(packet(at), at, history=context['history'], feedback=context['feedback'])
        assert board['themes'][0]['id'] == 'BK1000'
        assert board['themes'][0]['stage'] == ('启动' if i == 0 else '持续')
        run = scan(at)
        run['themeBoard'] = board
        for row in run['themeResearchUniverse']:
            row['themeIds'] = ['BK1000']
        store.save_scan(run, at)
        store.review(at.replace(hour=15, minute=30), close_quotes(at, prices=(9, 9, 9, 9)))
    at = DAY+timedelta(days=3)
    context = store.read_context(at)
    assert context['feedback']['adjustments'] == {'BK1000': .06}
    plain = tr.build_theme_board(packet(at), at, history=context['history'])
    adjusted = tr.build_theme_board(packet(at), at, history=context['history'], feedback=context['feedback'])
    assert plain['themes'][0]['id'] == 'BK1000' and adjusted['themes'][0]['id'] != 'BK1000'
    ai = next(t for t in adjusted['themes'] if t['id'] == 'BK1000')
    assert ai['feedbackPenalty'] == .06 and ai['stage'] == '持续'
    assert ai['priorSelectedDays'] == 3
