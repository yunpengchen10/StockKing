"""Every published opportunity remains inspectable in the same frozen run."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from src.services.yao_scout.signal_learning import SignalLearningService
from tests.test_opportunity_channels import NOW, candidate


@pytest.mark.parametrize('compact', [False, True])
def test_opportunity_only_publication_is_visible_by_run_and_symbol_without_rewriting(tmp_path, compact):
    svc = SignalLearningService(tmp_path, clock=lambda: NOW)
    row = candidate('600506')
    row.update(name='冻结机会', decision_at=NOW.isoformat(), referencePrice=18.23)
    row['opportunity'] = {'channel': 'E', 'grade': 'E-A', 'detectedAt': NOW.isoformat(),
                          'firstDetectionAt': NOW.isoformat(), 'firstDetectionPrice': 17.1,
                          'metrics': {'r3_pct': -1.65}}
    scan = {'run_id': 'opportunity-first', 'status': 'completed_observations', 'candidates': [],
            'opportunityChannels': {'recommendations': [row], 'observations': [candidate('600009')]}}
    saved = svc.persist_scan(scan, 'live', NOW, False)
    first_id = saved['run_id']
    later = deepcopy(scan)
    later.update(run_id='opportunity-second')
    later['opportunityChannels']['recommendations'] = [candidate('600001')]
    svc.persist_scan(later, 'live', NOW + timedelta(minutes=2), False)
    before = svc.db_path.read_bytes()
    result = svc.history({'run_id': first_id, 'symbol': '600506', 'compact': compact, 'include_controls': False})
    assert len(result['items']) == 1
    assert result['items'][0]['displayRunId'] == first_id
    ledger_id = result['items'][0]['runId']
    assert svc.history({'run_id': ledger_id, 'compact': compact})['items'][0]['displayRunId'] == first_id
    signals = result['items'][0]['signals']
    assert [s['code'] for s in signals] == ['600506']
    assert signals[0]['opportunitySelected'] is True
    assert signals[0]['recommendationKinds'] == ['opportunity_recommendation']
    assert signals[0]['signalPrice'] == 18.23
    assert signals[0]['snapshot']['opportunity'] == row['opportunity']
    assert not signals[0]['selected'] and not signals[0]['trainingEligible'] and not signals[0]['reviewEligible']
    assert svc.history({'run_id': 'missing-run', 'compact': compact})['items'] == []
    assert svc.history({'symbol': '600009', 'compact': compact})['items'] == []
    assert svc.db_path.read_bytes() == before


def test_compact_opportunities_do_not_decode_whole_research_scan(tmp_path, monkeypatch):
    from src.services.yao_scout import signal_learning as ledger
    svc = SignalLearningService(tmp_path, clock=lambda: NOW)
    row = candidate()
    row['opportunity'] = {'channel': 'E', 'grade': 'E-A'}
    scan = {'status': 'completed_observations', 'candidates': [],
            'opportunityChannels': {'recommendations': [row], 'researchCandidates': [{'raw': 'x' * 2_000_000}]}}
    svc.persist_scan(scan, 'live', NOW, False)
    original = json.loads
    def bounded(value, *args, **kwargs):
        assert len(value) < 100_000
        return original(value, *args, **kwargs)
    monkeypatch.setattr(ledger.json, 'loads', bounded)
    result = svc.history({'compact': True, 'include_controls': False})
    assert len(result['items'][0]['signals']) == 1
    assert len(json.dumps(result)) < 15_000


@pytest.mark.parametrize('compact', [False, True])
def test_opportunity_already_in_control_is_neither_lost_nor_duplicated(tmp_path, compact):
    svc = SignalLearningService(tmp_path, clock=lambda: NOW)
    row = candidate()
    row['opportunity'] = {'channel': 'E', 'grade': 'E-A'}
    svc.persist_scan({'status': 'completed_observations', 'candidates': [], 'controls': [row],
        'opportunityChannels': {'recommendations': [row]}}, 'live', NOW, False)
    for include in (True, False):
        result = svc.history({'compact': compact, 'include_controls': include})
        signals = result['items'][0]['signals']
        assert len(signals) == 1
        assert signals[0]['opportunitySelected']
        assert signals[0]['recommendationKinds'] == ['opportunity_recommendation']
