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
    assert result['dataQuality']['fundFlowArchive']['verifiedPacketCount'] == 0
    assert result['dataQuality']['fundFlowArchive']['failedPacketCount'] == 4
    assert result['dataQuality']['fundCollection']['candidate30mObserved'] == 0


def test_last_packet_archive_timeout_keeps_earlier_verified_funds(monkeypatch, tmp_path):
    from src.services.yao_scout.fund_flow_archive import archive_fund_packets
    from time import monotonic
    packets = {code: inputs(code)[0]['fundEvidence'] for code in ('600001', '600002')}
    original = FundFlowArchive.read
    def read(archive, code, *args, **kwargs):
        if code == '600002':
            raise TimeoutError('last packet exceeded its archive allowance')
        return original(archive, code, *args, **kwargs)
    monkeypatch.setattr(FundFlowArchive, 'read', read)
    verified, report = archive_fund_packets(packets, NOW.replace(second=0), tmp_path,
        clock=lambda: NOW, deadline=monotonic()+10)
    assert report['status'] == 'partial'
    assert report['verifiedPacketCount'] == report['failedPacketCount'] == report['timeoutPacketCount'] == 1
    assert report['savedPacketCount'] == 2
    assert verified['600001']['archiveStatus'] == 'verified'
    assert compute_stock_flow(verified['600001'], inputs()[1], NOW)['status'] == 'observed'
    assert verified['600002']['cumulativeSeries'] == []


def test_expired_archive_budget_marks_remaining_packets_without_erasing_first(monkeypatch, tmp_path):
    from src.services.yao_scout import fund_flow_archive as module
    seconds = [0.]
    monkeypatch.setattr(module, 'monotonic', lambda: seconds[0])
    original = FundFlowArchive.read
    def read(archive, *args, **kwargs):
        frozen = original(archive, *args, **kwargs)
        seconds[0] = 6.
        return frozen
    monkeypatch.setattr(FundFlowArchive, 'read', read)
    packets = {code: inputs(code)[0]['fundEvidence'] for code in ('600001', '600002', '600003')}
    packets['600004'] = None
    verified, report = module.archive_fund_packets(packets, NOW.replace(second=0), tmp_path,
        clock=lambda: NOW, deadline=6.)
    assert report['verifiedPacketCount'] == report['attemptedPacketCount'] == 1
    assert report['timeoutPacketCount'] == report['remainingPacketCount'] == 3
    assert verified['600001']['cumulativeSeries']
    assert not verified['600002']['cumulativeSeries'] and not verified['600003']['cumulativeSeries']


def test_bad_archive_packet_does_not_block_later_independent_packets(monkeypatch, tmp_path):
    from src.services.yao_scout.fund_flow_archive import archive_fund_packets
    from time import monotonic
    original = FundFlowArchive.save
    def save(archive, packet, *args, **kwargs):
        if packet['code'] == '600002':
            raise ValueError('malformed single packet')
        return original(archive, packet, *args, **kwargs)
    monkeypatch.setattr(FundFlowArchive, 'save', save)
    packets = {code: inputs(code)[0]['fundEvidence'] for code in ('600001', '600002', '600003')}
    verified, report = archive_fund_packets(packets, NOW.replace(second=0), tmp_path,
        clock=lambda: NOW, deadline=monotonic()+10)
    assert report['status'] == 'partial' and report['verifiedPacketCount'] == 2
    assert report['failedPacketCount'] == 1 and report['timeoutPacketCount'] == 0
    assert verified['600001']['cumulativeSeries'] and verified['600003']['cumulativeSeries']


def test_provider_budget_exhaustion_preserves_six_seconds_archive_and_fifteen_seconds_quotes(monkeypatch, tmp_path):
    from src.services.yao_scout import local_opportunities as local, fund_flow_archive, sector_fund_evidence
    service, tick, _ = complete_flow_service(monkeypatch, tmp_path)
    seconds, deadlines = [0.], []
    monkeypatch.setattr(local, 'monotonic', lambda: seconds[0])
    monkeypatch.setattr(fund_flow_archive, 'monotonic', lambda: seconds[0])
    def progress(value, message):
        if value == 72:
            seconds[0] = 80.
            tick[0] = NOW+timedelta(seconds=80)
    service._progress = progress
    service.fund_fetcher = None
    def fund(code, cutoff, **kwargs):
        deadlines.append(kwargs['deadline'])
        return inputs(code)[0]['fundEvidence']
    monkeypatch.setattr(sector_fund_evidence, 'fetch_fund_evidence', fund)
    original = local.bounded_fetch_map
    def exhaust(fetch, items, **kwargs):
        result = original(fetch, items, **kwargs)
        if fetch.__name__ == 'funds_for':
            seconds[0] = deadlines[0]
            tick[0] = NOW+timedelta(seconds=seconds[0])
        return result
    monkeypatch.setattr(local, 'bounded_fetch_map', exhaust)
    result = service.run('live', top_n=1)
    quality = result['dataQuality']
    assert set(deadlines) == {99.}
    assert quality['stageBudgetsSeconds']['fund_flow_archive'] == pytest.approx(6.)
    assert quality['stageBudgetsSeconds']['final_quotes'] == pytest.approx(15.)
    assert quality['fundFlowArchive']['verifiedPacketCount'] == 4
    assert quality['fundCollection']['candidate30mObserved'] == 4
    assert quality['parallelEvidenceCollection']['archiveReserveSeconds'] == 6


def test_three_minute_source_fields_are_not_reported_as_complete_thirty_minute_windows(monkeypatch, tmp_path):
    service, _, _ = complete_flow_service(monkeypatch, tmp_path)
    def partial(code, *args):
        packet = inputs(code)[0]['fundEvidence']
        packet['metrics'] = {'main_net_flow_3m': 3000.}
        packet['cumulativeSeries'] = packet['cumulativeSeries'][-4:]
        return packet
    service.fund_fetcher = partial
    result = service.run('live', top_n=5)
    assert result['dataQuality']['fundCollection']['legacy3mUsable'] == 4
    assert result['dataQuality']['fundCollection']['aligned30mObserved'] == 0
    assert result['dataQuality']['fundCollection']['candidate30mObserved'] == 0
    assert result['opportunityChannels']['recommendations'] == []


def test_funds_and_sector_minutes_share_ready_candidate_order_without_reordering_research(monkeypatch, tmp_path):
    from src.services.yao_scout import sector_fund_evidence
    service, _, _ = complete_flow_service(monkeypatch, tmp_path)
    original = service.minute_fetcher
    def minute(code, *args):
        return {'code': code, 'metrics': {}} if code == '600001' else original(code, *args)
    service.minute_fetcher = minute
    sector_calls = []
    def sector(codes, *args, **kwargs):
        sector_calls.append(list(codes))
        return {}
    monkeypatch.setattr(sector_fund_evidence, 'fetch_sector_batch', sector)
    result = service.run('live', top_n=5)
    priority = ['600002', '600003', '600004', '600001']
    order = result['dataQuality']['fundCollectionOrder']
    assert order['priorityCandidateOrder'] == priority
    assert sector_calls == [priority]
    assert order['requestedCodes'][0] == '600002'
    assert set(order['requestedCodes']) == set(priority)  # Every real constituent remains requested.
    assert result['historyPreparationCodes'] == ['600001', '600002', '600003', '600004']


def test_archive_never_relabels_a_packet_from_another_security(monkeypatch, tmp_path):
    from src.services.yao_scout.fund_flow_archive import archive_fund_packets
    from time import monotonic
    packets = {'600001': inputs('600002')[0]['fundEvidence']}
    verified, report = archive_fund_packets(packets, NOW.replace(second=0), tmp_path,
        clock=lambda: NOW, deadline=monotonic()+10)
    assert report['failedPacketCount'] == 1 and report['verifiedPacketCount'] == 0
    assert verified['600001']['code'] == '600002'
    assert verified['600001']['cumulativeSeries'] == []


def test_candidate_without_own_minutes_can_supply_verified_peer_funds_without_becoming_eligible(monkeypatch, tmp_path):
    from src.services.yao_scout import sector_fund_evidence
    service, _, _ = complete_flow_service(monkeypatch, tmp_path)
    original = service.minute_fetcher
    service.minute_fetcher = lambda code, *args: (
        {'code': code, 'metrics': {}} if code == '600001' else original(code, *args))
    calls = []
    def funds(code, cutoff):
        calls.append((code, cutoff))
        return inputs(code)[0]['fundEvidence']
    service.fund_fetcher = funds
    def sectors(codes, intraday, cutoff, directory, **kwargs):
        # The peer collector independently obtains the completed amount window.
        # Its callback must not fill the candidate's original missing price path.
        peer = inputs('600001')[1]
        peer['flowAmountWindowsByEnd'] = {peer['asOf']: deepcopy(peer)}
        kwargs['on_minutes']('600001', peer)
        return {}
    monkeypatch.setattr(sector_fund_evidence, 'fetch_sector_batch', sectors)
    result = service.run('live', top_n=5)
    assert ('600001', NOW.replace(second=0).isoformat()) in calls
    rows = {row['code']: row for row in result['opportunityChannels']['researchCandidates']}
    assert rows['600001']['ruleRecommendation'] is False
    assert rows['600001']['opportunity']['grade'] in {'B', 'C'}
    assert rows['600001']['opportunity']['flowEvidence']['stock']['status'] == 'unavailable'
    for code in ('600002', '600003', '600004'):
        sector = rows[code]['opportunity']['flowEvidence']['sector']
        assert sector['status'] == 'observed'
        assert sector['coverage']['usablePeers'] == sector['coverage']['totalPeers'] == 3
        assert '600001' in sector['observedPeerCodes']
    selected = {row['code'] for row in result['opportunityChannels']['recommendations']}
    assert selected and '600001' not in selected
