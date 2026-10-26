from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.services.public_market_quotes import TZ
from src.services.software_market import SoftwareMarketClient
from src.services.yao_scout import recommendation_evidence as evidence
from src.services.yao_scout import local_opportunities as local


NOW = datetime(2026, 10, 26, 10, 6, tzinfo=TZ)


def row(code, amount, **updates):
    return {'code': code, 'name': '普通股', 'price': 10., 'open': 9.9,
        'amount': amount, 'volume_ratio': 50., 'turnover_rate': 80.,
        'source': 'old_snapshot', 'source_time': (NOW-timedelta(days=3)).isoformat(), **updates}


def quote(code, amount, **updates):
    return {'code': code, 'source': 'tencent', 'source_time': NOW.isoformat(),
        'price': 10., 'open': 10., 'amount_cny': amount, **updates}


def test_previous_session_winner_cannot_displace_fresh_amount_leader_or_borrow_ratio_clock():
    frame = pd.DataFrame([
        row('600001', 1e12, market_quote=quote('600001', 100.)),
        row('600002', 1., market_quote=quote('600002', 1e9)),
    ])
    queued = evidence.research_queue(frame, 1, now=NOW)
    assert queued[0]['code'] == '600002'
    packet = queued[0]['selectionEvidence']
    assert packet['fields']['amount'] == {'value': 1e9, 'source': 'tencent', 'sourceTime': NOW.isoformat()}
    assert queued[0]['volume_ratio'] is None and queued[0]['turnover_rate'] is None
    assert {'volume_ratio', 'turnover_rate'} <= set(packet['missingFields'])
    assert packet['originalSnapshotSourceTime'].startswith('2026-10-23')


@pytest.mark.parametrize('updates', [
    {'source_time': (NOW-timedelta(seconds=31)).isoformat()},
    {'source_time': (NOW+timedelta(seconds=1)).isoformat()},
    {'source_time': NOW.date().isoformat()}, {'code': '600999'},
])
def test_bad_selection_quote_is_directory_only_and_keeps_original_source_boundary(updates):
    frame = pd.DataFrame([row('600001', 1e12, market_quote={**quote('600001', 1e12), **updates})])
    candidate = evidence.research_queue(frame, now=NOW)[0]
    assert candidate['selectionEvidence']['status'] == 'directory_only'
    assert candidate['selectionEvidence']['fields'] == {}
    assert candidate['amount'] is candidate['volume_ratio'] is candidate['turnover_rate'] is None


def test_fresh_snapshot_ratio_has_its_own_evidence_but_missing_quote_open_does_not_borrow():
    frame = pd.DataFrame([row('600001', 100., source_time=NOW.isoformat(),
        market_quote=quote('600001', 200., open=None))])
    selected = evidence.research_queue(frame, now=NOW)[0]
    assert selected['volume_ratio'] == 50.
    assert selected['selectionEvidence']['fields']['volume_ratio']['source'] == 'old_snapshot'
    assert selected['open'] is None
    assert 'open' in selected['selectionEvidence']['missingFields']


def test_complete_directory_does_not_claim_fresh_selection_inputs(monkeypatch):
    client = SoftwareMarketClient('http://127.0.0.1:9000', 'test-only')
    monkeypatch.setattr(client, '_request', lambda *a: {'complete': True, 'items': [row('600001', 1e9)]})
    frame = client.snapshot()
    assert frame.attrs['directory_complete'] is True
    assert frame.attrs['stale'] is True
    assert frame.attrs['selection_fresh_count'] == 0


def test_post_enrichment_queue_prepares_newly_selected_daily_history(monkeypatch, tmp_path):
    from tests.test_local_quote_integration import make_service
    service, _, _, _ = make_service(monkeypatch, NOW, tmp_path)
    original = service.yao.history_fetcher().copy()
    frame = pd.DataFrame([row('600001', 1e12), row('600002', 1.)])
    frame.attrs.update(stale=True, directory_complete=True)
    service.yao._fetch_snapshot = lambda: frame.copy()
    monkeypatch.setattr(evidence, 'research_queue_limit', lambda n: min(n, 1))
    fetched = []
    class Gateway:
        available = True
        def bars(self, code, **kwargs):
            fetched.append(code)
            return {'source': 'fixture', 'adjustment': 'none', 'bars': [
                {**item, 'end': item['date'].isoformat()} for item in original.to_dict('records')]}
        def enrich_snapshot_quotes(self, data, **kwargs):
            result = data.copy()
            result['market_quote'] = [quote(code, 1e9 if code == '600002' else 100.) for code in result.code]
            return result
    monkeypatch.setattr(SoftwareMarketClient, 'from_environment', lambda: Gateway())
    result = service.run('live')
    assert result['dataQuality']['selectionInputFreshness']['newQueueCodes'] == ['600002']
    assert result['dataQuality']['selectionInputFreshness']['freshRankedCount'] == 1
    assert result['dataQuality']['selectionInputFreshness']['newDailyRequested'] == 1
    assert {'600001', '600002'} <= set(fetched)
    assert result['historyPreparationCodes'] == ['600002']


def test_industry_cap_can_choose_later_independent_name_before_count_limit(monkeypatch):
    monkeypatch.setattr(local, 'is_entry_eligible', lambda row, profile: True)
    candidates = [{'code': f'60000{i}', 'industry': 'A' if i < 5 else 'B',
        'marketRegime': {'state': 'normal'}} for i in range(6)]
    result = local.allocate_profile_candidates(candidates, 'regular', 3)
    assert [row['code'] for row in result['candidates']] == ['600000', '600001', '600005']
    assert result['policy']['industryExposure'] == {'A': .4, 'B': .2}
    assert result['policy']['plannedExposure'] == .6
    assert local.allocate_profile_candidates(candidates, 'regular', 0)['policy']['plannedExposure'] == 0


def test_daily_result_checkpoint_precedes_failed_minute_review(monkeypatch, tmp_path):
    from tests.test_local_quote_integration import make_service
    from src.services.yao_scout.signal_learning import SignalLearningService
    service, _, _, saved = make_service(monkeypatch, NOW.replace(hour=15, minute=30), tmp_path)
    monkeypatch.setattr(SignalLearningService, 'review_daily_observations',
        lambda *a, **kw: {'updated': 7, 'status': 'completed'})
    def fail(*args, **kwargs):
        assert saved[0]['dailyObservationReview']['updated'] == 7
        raise RuntimeError('minute review failed')
    monkeypatch.setattr(SignalLearningService, 'review_due', fail)
    with pytest.raises(RuntimeError, match='minute review failed'):
        service._persist_run({'scanSlot': 'review', 'status': 'audit_only'})
    assert saved[0]['dailyObservationReview']['updated'] == 7


def test_completion_rechecks_frozen_entry_expiry_even_if_quote_and_all_evidence_are_refreshed(monkeypatch, tmp_path):
    from tests.test_local_quote_integration import make_service, enable_primary_evidence, enable_validated_execution_model
    start = NOW.replace(hour=13, minute=17)
    service, tick, _, _ = make_service(monkeypatch, start, tmp_path)
    enable_primary_evidence(service)
    enable_validated_execution_model(monkeypatch)
    original = service._finish_quotes
    before = []
    def refresh_and_finish(run, quality):
        if len(before) == 1:
            tick[0] += timedelta(seconds=301)
        for rows in [run.get('precisionResearch', []), *run.get('profileCandidates', {}).values()]:
            for candidate in rows:
                stamp = tick[0].isoformat()
                candidate['quote'].update(provider_timestamp=stamp, active_flow_as_of=stamp)
                candidate.update(minuteSourceTime=stamp, factorCutoffAt=stamp)
                candidate['sectorEvidence']['asOf'] = stamp
                candidate['fundEvidence']['asOf'] = stamp
                candidate['researchFactors']['E']['asOf'] = stamp
        original(run, quality)
        before.append(len(run['candidates']))
    service._finish_quotes = refresh_and_finish
    result = service.run('live')
    assert before[:2] == [1, 1]  # Quote/minute freshness alone still passed.
    assert result['candidates'] == []
    research = result['precisionResearch'][0]
    assert research['primaryAdmission']['eligible'] is True
    assert research['entryExecution']['state'] == 'entry_plan_expired'
    assert research['entryExecution']['triggerAt'] == start.isoformat()
