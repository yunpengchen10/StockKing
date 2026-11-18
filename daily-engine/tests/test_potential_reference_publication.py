"""Real public-quote adapter references work outside the execution window."""
from copy import deepcopy
from datetime import datetime

import pytest

from src.services.public_market_quotes import _assess, adapt_public_quote
from src.services.stock_king_display import LocalPicksDisplayStore, displayable_run
from src.services.yao_scout.potential_picks import (
    prepare_reference_assessment, select_potential_picks,
)
from tests.test_potential_picks import candidate, inputs, BASELINE


def reference_run(decision='15:30:00', source='15:00:00'):
    row = candidate('600001')
    original = row['quote']
    at = datetime.fromisoformat('2026-11-17T' + decision + '+08:00')
    stamp = '2026-11-17T' + source + '+08:00'
    raw = {**original, 'source_time': stamp, 'source': 'tencent', 'transport': 'https',
        'source_url': 'https://qt.gtimg.cn/', 'fetched_at': at.isoformat(),
        'previous_close': original['pre_close'], 'open': original['open_price'],
        'volume_shares': original['volume'], 'amount_cny': original['amount'],
        'order_book_valid': True, 'asks': [], 'bids': []}
    assessed = _assess(row['code'], [{'observations': [raw], 'errors': {}}], at)
    row['quote'] = adapt_public_quote({'schema_version': 1, 'checked_at': at.isoformat(),
        'max_age_seconds': 30}, assessed)
    run, _ = inputs([row])
    run.update(run_id='reference-scan', mode='king_live', scanSlot='live', official=False,
        status='expired', generatedAt=at.isoformat(), candidates=[],
        primarySelectionMode='mixed_intraday_and_daily_observation',
        dataQuality={'snapshot_count': 3000, 'quote_coverage': {'requested': 1, 'fresh': 0}},
        dailyAssessment={'status': 'outside_continuous_trading_window', 'dailyUsableCount': 0})
    return run


@pytest.mark.parametrize(('decision', 'source', 'label'), [
    ('15:03:00', '15:02:50', '收盘价'),
    ('15:30:00', '15:00:00', '收盘价'),
    ('12:00:00', '11:30:00', '午间收盘价'),
    ('14:58:00', '14:57:50', '扫描价'),
    ('09:26:00', '09:25:50', '扫描价'),
])
def test_same_session_reference_has_own_publication_gate_and_exact_history(tmp_path, decision, source, label):
    run = reference_run(decision, source)
    frozen = deepcopy(run)
    assert not displayable_run(run)
    prepare_reference_assessment(run)
    assert displayable_run(run)
    assert run['controls'] == frozen['controls']  # Do not rehabilitate execution flags.
    item, = run['potentialAssessment']['items']
    assert item['basisLabel'] == label and item['sourceTime'].endswith(source + '+08:00')
    store = LocalPicksDisplayStore(None, data_dir=tmp_path)
    shown = store.save(run)
    assert store.records()['items'][0]['items'] == [item]
    assert shown['adaptive']['publishedPicks']['items'] == [item]
    assert displayable_run(shown['adaptive'], historical=True)
    # Reading an old expired scan is not a request to publish it retroactively.
    assert not displayable_run(frozen, historical=True)
    assert 'potentialAssessment' not in frozen


@pytest.mark.parametrize('case', ['conflict', 'wrong_code', 'future', 'prior_day', 'touched',
                                  'missing_clock', 'tainted', 'false_age_reason', 'stale_auction'])
def test_reference_cannot_promote_bad_or_unsupported_price_evidence(case):
    run = reference_run('14:58:00', '14:57:00') if case == 'stale_auction' else reference_run()
    quote = run['controls'][0]['quote']
    metadata = quote['public_quote_metadata']
    if case == 'conflict':
        metadata['issues'].append('cross_source_price_conflict')
    elif case == 'wrong_code':
        metadata['code'] = '600002'
    elif case == 'future':
        quote['source_time'] = quote['provider_timestamp'] = '2026-11-17T15:31:00+08:00'
    elif case == 'prior_day':
        quote['source_time'] = quote['provider_timestamp'] = '2026-11-16T15:00:00+08:00'
    elif case == 'touched':
        quote['high'] = quote['limit_up']
    elif case == 'missing_clock':
        quote.pop('source_time')
        quote.pop('provider_timestamp')
    elif case == 'tainted':
        quote['tainted'] = True
    elif case == 'false_age_reason':
        metadata['checked_at'] = '2026-11-17T15:00:05+08:00'
    assert select_potential_picks(run, None) == []
    prepare_reference_assessment(run)
    assert 'potentialAssessment' not in run and not displayable_run(run)


def test_preopen_previous_close_is_not_made_into_an_auction_quote():
    row = candidate('600001')
    row.update(quote={}, price=10.4, dailyLastClose=10.4, referencePriceKind='previous_close',
        status='premarket', baselineAsOf=BASELINE, baselineSource='daily-feed', sourceTime=BASELINE)
    run, _ = inputs([])
    run.update(premarketCandidates=[row], generatedAt='2026-11-17T09:20:00+08:00')
    item, = select_potential_picks(run, None)
    assert item['basisLabel'] == '前收盘价' and item['sourceTime'] == BASELINE
    run['generatedAt'] = '2026-11-17T09:26:00+08:00'
    assert select_potential_picks(run, None) == []


def test_empty_incomplete_official_or_previously_published_scans_are_not_promoted():
    for changes in ({'controls': []}, {'dataQuality': {'snapshot_count': 0}}, {'official': True},
                    {'publishedPicks': {'status': 'unrecorded'}}):
        run = reference_run()
        run.update(changes)
        before = deepcopy(run)
        prepare_reference_assessment(run)
        assert run == before and 'potentialAssessment' not in run


def test_new_scan_persistence_creates_reference_assessment_and_both_views(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.services.yao_scout import history_maintenance, primary_readiness, signal_learning
    from src.services.yao_scout.local_opportunities import LocalOpportunityService
    run = reference_run()
    archived = []
    service = object.__new__(LocalOpportunityService)
    service.db = SimpleNamespace(save_yao_run=lambda row: archived.append(deepcopy(row)))
    service.yao = SimpleNamespace(data_dir=tmp_path)
    service.clock = lambda: datetime.fromisoformat(run['generatedAt'])
    monkeypatch.setattr(signal_learning, 'SignalLearningService', lambda *_: SimpleNamespace(
        persist_scan=lambda result, *_: result, review_daily_observations=lambda *_, **__: {}))
    monkeypatch.setattr(primary_readiness, 'read_primary_readiness', lambda *_, **__: {})
    monkeypatch.setattr(history_maintenance, 'start_history_preparation', lambda *_: None)
    completed = service._persist_run(run)
    store = LocalPicksDisplayStore(None, data_dir=tmp_path)
    publication = completed['publishedPicks']
    assert completed['status'] == 'completed_observations'
    assert completed['legacySelectionStatus'] == 'expired'
    assert archived[-1]['publishedPicks'] == publication
    assert store.read()['adaptive']['publishedPicks'] == publication
    assert store.records(scope='history')['items'] == [publication]
    assert 'potentialAssessment' not in run and run['status'] == 'expired'
