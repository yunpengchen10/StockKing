"""Offline integration boundaries for shared peers, quarantine and fund replay."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import sqlite3

import pandas as pd
import pytest
import requests

from src.services.yao_scout.fund_flow_evidence import compute_stock_flow
from src.services.yao_scout.fund_flow_archive import FundFlowArchive
from tests.test_flow_opportunity_policy import NOW, inputs
from tests.test_opportunity_integration import complete_flow_service


def test_archive_deadline_blocks_further_reads_without_rewriting_saved_evidence(monkeypatch, tmp_path):
    from src.services.yao_scout import fund_flow_archive as module
    monkeypatch.setattr(module, 'monotonic', lambda: 10.)
    archive = FundFlowArchive(tmp_path, timeout=1, deadline=11.)
    cutoff = NOW.replace(second=0)
    packet = inputs()[0]['fundEvidence']
    archive.save(packet, cutoff, observed_at=NOW)
    before = archive.path.read_bytes()
    monkeypatch.setattr(module, 'monotonic', lambda: 11.)
    with pytest.raises(TimeoutError):
        archive.read(packet['code'], NOW.date().isoformat(), packet['provider'],
                     packet['measurementVersion'], as_of=NOW)
    assert archive.path.read_bytes() == before


@pytest.fixture(autouse=True)
def no_network_or_desktop_gateway(monkeypatch):
    from src.services.yao_scout import fund_flow_discovery
    from src.services.software_market import SoftwareMarketClient
    attempts = []
    def reject(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError('network forbidden in flow pipeline tests')
    monkeypatch.setattr(requests.sessions.Session, 'request', reject)
    monkeypatch.setattr(SoftwareMarketClient, 'from_environment',
                        lambda: SimpleNamespace(available=False))
    monkeypatch.setattr(fund_flow_discovery, 'fetch_flow_snapshot',
                        lambda *a, **k: {'status': 'unavailable', 'rows': []})
    yield
    assert not attempts, 'an offline fixture attempted network access'


@pytest.mark.parametrize('state,expected', [('old_only', 'unavailable'), ('current_verified', 'observed'),
                                         ('current_tainted', 'unavailable'), ('current_short', 'unavailable')])
def test_current_lineage_must_resolve_old_taint_through_frozen_window(state, expected):
    row, minute, _, _ = inputs()
    cutoff = NOW.replace(second=0)
    sessions = [{'code': row['code'], 'day': (NOW-timedelta(days=1)).date().isoformat(), 'status': 'tainted'}]
    if state != 'old_only':
        sessions.append({'code': row['code'], 'day': NOW.date().isoformat(),
            'status': 'tainted' if state == 'current_tainted' else 'verified',
            'verifiedThrough': (cutoff-timedelta(minutes=1) if state == 'current_short' else cutoff).isoformat()})
    minute['dataLineage'] = {'status': 'tainted', 'sessions': sessions}
    assert compute_stock_flow(row['fundEvidence'], minute, cutoff)['status'] == expected


def test_real_scan_uses_nonselected_peer_minute_callback_and_full_fund_membership(monkeypatch, tmp_path):
    from src.services.yao_scout import sector_fund_evidence
    service, tick, _ = complete_flow_service(monkeypatch, tmp_path)
    codes = ['600001', '600002', '600003', '600004']
    service.yao._fetch_snapshot = lambda: pd.DataFrame([{'code': codes[0], 'name': '候选', 'price': 10.12}])
    calls = []
    service.fund_fetcher = lambda code, *a: (calls.append(code) or inputs(code, now=tick[0])[0]['fundEvidence'])
    def sector_fetch(requested, intraday, cutoff, directory, **kwargs):
        assert requested == [codes[0]]
        for code in codes[1:]:
            kwargs['on_minutes'](code, inputs(code, now=tick[0])[1])
        return {}
    monkeypatch.setattr(sector_fund_evidence, 'fetch_sector_batch', sector_fetch)
    result = service.run('live', top_n=1)
    assert set(calls) == set(codes)
    assert result['opportunityChannels']['recommendations'], result.get('message')
    selected = result['opportunityChannels']['recommendations'][0]
    sector = selected['opportunity']['flowEvidence']['sector']
    assert sector['coverage']['totalPeers'] == sector['coverage']['usablePeers'] == 3
    assert set(sector['observedPeerCodes']) == set(codes[1:])
    assert sector['asOf'] == selected['fundEvidence']['asOf']


def test_previously_archived_conflict_cannot_be_reaccepted_by_clean_live_packet(monkeypatch, tmp_path):
    service, _, _ = complete_flow_service(monkeypatch, tmp_path)
    cutoff = NOW.replace(second=0)
    archive = FundFlowArchive(tmp_path/'fund_flow_history')
    first = deepcopy(inputs()[0]['fundEvidence'])
    archive.save(first, cutoff, observed_at=cutoff)
    revised = deepcopy(first)
    revised['cumulativeSeries'][5]['main_net_flow'] += 1
    archive.save(revised, cutoff, observed_at=cutoff+timedelta(seconds=1))
    result = service.run('live', top_n=5)
    # One tainted peer reduces every other candidate's 3-member coverage; the
    # tainted name also fails its own required 30-minute fund window.
    assert result['opportunityChannels']['recommendations'] == []
    assert result['dataQuality']['sectorFlowCollection']['observed'] == 1
    rejected = next(row for row in result['opportunityChannels']['researchCandidates'] if row['code'] == '600001')
    assert rejected['fundEvidence']['conflictingTimes']


def test_final_quote_quarantine_removes_member_from_other_candidates_sector_flow(monkeypatch, tmp_path):
    service, _, _ = complete_flow_service(monkeypatch, tmp_path)
    def final_verification(codes, now, **kwargs):
        return {'lineages': {'600001': {'status': 'tainted', 'sessions': [{
            'code': '600001', 'day': NOW.date().isoformat(), 'status': 'tainted',
            'reasonCodes': ['independent_quote_conflict']}]}}}
    service._verify_archived_quotes = final_verification
    result = service.run('live', top_n=5)
    assert result['opportunityChannels']['recommendations'] == []
    for row in result['opportunityChannels']['researchCandidates']:
        if row['code'] != '600001':
            sector = row['opportunity']['flowEvidence']['sector']
            assert sector['status'] == 'unavailable'
            assert sector['coverage']['usablePeers'] == 2


def test_failed_archive_read_does_not_bypass_known_revision_quarantine(monkeypatch, tmp_path):
    service, _, _ = complete_flow_service(monkeypatch, tmp_path)
    def broken_read(*args, **kwargs):
        raise sqlite3.OperationalError('test unavailable archive')
    monkeypatch.setattr(FundFlowArchive, 'read', broken_read)
    result = service.run('live', top_n=5)
    assert result['opportunityChannels']['recommendations'] == []
    assert result['dataQuality']['fundFlowArchive']['status'] == 'unavailable'
