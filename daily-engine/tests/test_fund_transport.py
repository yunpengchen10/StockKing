from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event, Lock
from time import monotonic

import pytest
import requests

from src.services.yao_scout import sector_fund_evidence as sf


CUTOFF = '2026-10-22T14:00:00+08:00'


class Response:
    def __init__(self, code, *, flow=-10, stamp='2026-10-22'):
        self.payload = {'rc': 0, 'data': {'code': code, 'klines': [
            f'{stamp} 13:{minute:02d}:00,{flow*n},0,0,{flow*n},0'
            for n, minute in enumerate((56, 57, 58, 59))]}}

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


@pytest.fixture(autouse=True)
def isolated_host_health(monkeypatch):
    # These tests isolate Eastmoney transport and host health. The independent
    # Tencent-first path is exercised with real-shaped packets in its own file.
    def unavailable(*args, **kwargs):
        raise ValueError('isolated Eastmoney test')
    monkeypatch.setattr(sf, '_tencent_fund_evidence', unavailable)
    with sf._fund_host_lock:
        previous = deepcopy(sf._fund_host_health)
        sf._fund_host_health.clear()
    yield
    with sf._fund_host_lock:
        sf._fund_host_health.clear()
        sf._fund_host_health.update(previous)


def test_33_codes_prefer_official_host_and_keep_negative_direction(monkeypatch):
    calls, lock = [], Lock()
    def get(url, **kwargs):
        with lock:
            calls.append((url, kwargs))
        return Response(kwargs['params']['secid'].split('.')[1])
    monkeypatch.setattr(sf, '_fund_public_get', get)
    codes = [f'600{n:03d}' for n in range(33)]
    deadline = monotonic()+2
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda code: sf.fetch_fund_evidence(code, CUTOFF, deadline=deadline), codes))
    assert len(calls) == len(rows) == 33
    assert all(url.startswith(sf.BASE) for url, _ in calls)
    assert all(0 < kwargs['timeout'] <= 2 for _, kwargs in calls)
    assert all(row['metrics']['main_net_flow_3m'] == -30 for row in rows)
    assert all(row['flowKind'] == 'vendor_estimate' and 'selected' not in row for row in rows)


def test_successful_fallback_is_reused_without_repeating_failed_host(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if url.startswith(sf.BASE):
            raise requests.ConnectionError('private details must not escape')
        return Response(kwargs['params']['secid'].split('.')[1])
    monkeypatch.setattr(sf, '_fund_public_get', get)
    deadline = monotonic()+2
    first = sf.fetch_fund_evidence('600001', CUTOFF, deadline=deadline)
    second = sf.fetch_fund_evidence('600002', CUTOFF, deadline=deadline)
    assert len(calls) == 3 and calls[0].startswith(sf.BASE)
    assert first['providerHost'] == second['providerHost'] == sf._fund_hosts[1]
    assert sf._fund_host_health[sf.BASE]['blockedUntil'] == deadline


def test_stock_specific_empty_payload_does_not_back_off_host(monkeypatch):
    calls = []
    def get(url, **kwargs):
        code = kwargs['params']['secid'].split('.')[1]
        calls.append((url, code))
        response = Response(code)
        if code == '600001':
            response.payload['data'] = None
        return response
    monkeypatch.setattr(sf, '_fund_public_get', get)
    missing = sf.fetch_fund_evidence('600001', CUTOFF, deadline=monotonic()+2)
    valid = sf.fetch_fund_evidence('600002', CUTOFF, deadline=monotonic()+2)
    assert missing['status'] == 'unavailable' and missing['metrics'] == {}
    assert len(calls) == 4 and calls[-1][0].startswith(sf.BASE)
    assert valid['metrics']['main_net_flow_3m'] == -30


def test_waiting_for_local_capacity_does_not_mark_uncontacted_host_failed(monkeypatch):
    calls = []
    def request(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise sf._FundTransportCapacityTimeout('no network slot')
        return Response(kwargs['params']['secid'].split('.')[1])
    monkeypatch.setattr(sf, '_fund_request', request)
    row = sf.fetch_fund_evidence('600001', CUTOFF, deadline=monotonic()+2)
    assert len(calls) == 2 and row['metrics']['main_net_flow_3m'] == -30
    assert sf.BASE not in sf._fund_host_health
    assert sf._fund_host_health[sf._fund_hosts[1]]['blockedUntil'] == 0


def test_deadline_bounds_33_slow_calls_and_late_workers_cannot_change_results(monkeypatch):
    entered, release, lock = Event(), Event(), Lock()
    active = maximum = started = 0
    def get(url, **kwargs):
        nonlocal active, maximum, started
        with lock:
            active += 1
            started += 1
            maximum = max(maximum, active)
            if active == sf.FUND_TRANSPORT_WORKERS:
                entered.set()
        try:
            release.wait(3)
            return Response(kwargs['params']['secid'].split('.')[1])
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(sf, '_fund_public_get', get)
    deadline = monotonic()+.3
    pool = ThreadPoolExecutor(max_workers=33)
    try:
        futures = [pool.submit(sf.fetch_fund_evidence, f'600{n:03d}', CUTOFF, deadline=deadline)
                   for n in range(33)]
        assert entered.wait(1)
        rows = [future.result(timeout=1) for future in futures]
        assert monotonic() < deadline+.5
        assert maximum == started == sf.FUND_TRANSPORT_WORKERS == 2
        assert all(row['status'] == 'unavailable' and row['metrics'] == {} for row in rows)
        frozen = deepcopy(rows)
        release.set()
        # Barrier tasks wait behind every late network worker in the fixed pool.
        barriers = [sf._fund_transport_pool.submit(lambda: None) for _ in range(sf.FUND_TRANSPORT_WORKERS)]
        for future in barriers:
            future.result(timeout=2)
        assert rows == frozen
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_new_cutoff_always_refetches_and_does_not_reuse_financial_values(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return Response('600001', flow=10 if len(calls) == 1 else -10)
    monkeypatch.setattr(sf, '_fund_public_get', get)
    first = sf.fetch_fund_evidence('600001', CUTOFF)
    second = sf.fetch_fund_evidence('600001', '2026-10-22T14:01:00+08:00')
    assert len(calls) == 2
    assert first['metrics']['main_net_flow_3m'] == 30
    assert second['metrics']['main_net_flow_3m'] == -30


@pytest.mark.parametrize('stamp', ['2026-10-21', '2026-10-23'])
def test_transport_success_never_promotes_wrong_day_evidence(stamp):
    row = sf.fetch_fund_evidence('600001', CUTOFF,
        getter=lambda *args, **kwargs: Response('600001', stamp=stamp))
    assert row['metrics'] == {} and row['asOf'] is None


def test_no_network_starts_after_expired_deadline():
    row = sf.fetch_fund_evidence('600001', CUTOFF, deadline=monotonic()-1,
        getter=lambda *args, **kwargs: pytest.fail('expired request started'))
    assert row['reasonCodes'] == ['fund_deadline_exhausted'] and row['metrics'] == {}


def test_worker_session_reuses_connection_without_environment_auth(monkeypatch):
    sessions, seen = [], []
    class Session:
        def __init__(self):
            sessions.append(self)
            self.trust_env = True
    monkeypatch.setattr(sf.requests, 'Session', Session)
    monkeypatch.setattr(sf, 'original_request', lambda session, *a, **k: seen.append(session))
    previous = getattr(sf._fund_transport_local, 'session', None)
    if previous is not None:
        del sf._fund_transport_local.session
    try:
        sf._fund_public_get(sf.BASE)
        sf._fund_public_get(sf.BASE)
        assert len(sessions) == 1 and seen == sessions*2 and sessions[0].trust_env is False
    finally:
        if previous is not None:
            sf._fund_transport_local.session = previous
        else:
            del sf._fund_transport_local.session
