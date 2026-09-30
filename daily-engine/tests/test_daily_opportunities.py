from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.services.yao_scout import daily_opportunities as mod


def test_mainboard_preserves_deep_negative_and_low_price_without_old_quotas():
    frame = pd.DataFrame([
        {'code': '600001', 'name': '主板A', 'price': 1.5, 'change_pct': -9, 'amount': 100},
        {'code': '000001', 'name': '主板B', 'change_pct': 9.9},
        *[{'code': c, 'name': '其他'} for c in ['300001', '688001', '830001']],
        {'code': '600002', 'name': '*ST风险'}, {'code': '600003', 'name': '退市股'},
    ])
    assert mod.mainboard(frame).code.tolist() == ['000001', '600001']


def test_high_is_actual_transport_setting_and_unsupported_model_is_not_downgraded():
    body = mod.high_body({'model': 'deepseek-v4-pro'}, [])
    assert body['thinking'] == {'type': 'enabled'}
    assert body['reasoning_effort'] == 'high'
    with pytest.raises(ValueError, match='尚未验证'):
        mod.high_body({'model': 'instant-model'}, [])


def quote(now):
    return {'code': '600001', 'price': 11, 'pre_close': 10, 'high': 11, 'low': 9,
            'open_price': 10, 'change_pct': 10, 'source': 'fixture', 'provider_timestamp': now.isoformat()}


def test_fetched_time_never_substitutes_for_missing_source_time():
    now = datetime(2026, 9, 8, 10, 30, tzinfo=mod.TZ)
    q = quote(now)
    assert mod.quote_check(q, '600001', now) == []
    q['fetched_at'] = q.pop('provider_timestamp')
    assert '原始行情时间未知' in mod.quote_check(q, '600001', now)
    q = quote(now); q['change_pct'] = 3
    assert '价格或涨跌幅不一致' in mod.quote_check(q, '600001', now)


def test_limit_lock_late_and_unknown_are_never_counted_as_executable():
    now = datetime(2026, 9, 8, 14, 55, tzinfo=mod.TZ)
    q = quote(now)
    assert mod.validate_candidate({'code': '600001'}, q, now, 'live')[0] == 'conditional'
    q['locked_limit_up'] = True
    assert mod.validate_candidate({'code': '600001'}, q, now, 'live')[0] == 'windvane'
    assert mod.validate_candidate({'code': '600001'}, q, now.replace(minute=57), '1455')[0] == 'expired'


class DB:
    def __init__(self): self.runs = []; self.states = []
    def list_yao_runs(self, **kw): return []
    def list_yao_dlm(self, **kw): return []
    def save_yao_run(self, run): self.runs.append(run)
    def save_yao_adaptive_state(self, key, state): self.states.append(state)


def fixture(tmp_path, monkeypatch, responses, now=None):
    now = now or datetime(2026, 9, 8, 10, 30, tzinfo=mod.TZ)
    db = DB()
    frame = pd.DataFrame([{'code': '600001', 'name': '测试股', 'price': 11, 'change_pct': -8, 'amount': 100}])
    calls = []
    def ask(task, evidence):
        calls.append((task, evidence))
        return responses.pop(0)
    ai = SimpleNamespace(ask=ask, audit=[{'requested_effort': 'high'}])
    yao = SimpleNamespace(db=db, _fetch_snapshot=lambda: frame,
        _snapshot_meta=lambda *a, **kw: {'source': 'fixture', 'point_in_time_ok': False},
        history_fetcher=lambda *a, **kw: pd.DataFrame(), data_dir=tmp_path, context_cache_dir=tmp_path)
    monkeypatch.setattr(mod, 'collect_candidate_context', lambda *a, **kw: ([], []))
    monkeypatch.setattr(mod, 'is_market_open', lambda *a: True)
    service = mod.DailyOpportunityService(yao, ai=ai, clock=lambda: now, quote_fetcher=lambda code: quote(now))
    return service, db, calls


def test_complete_chain_retains_observation_provenance_and_never_fabricates_entry(tmp_path, monkeypatch):
    responses = [{'codes': ['600001']}, {'candidates': [{'code': '600001', 'thesis': '剩余空间待验证',
        'triggers': ['等待确认'], 'invalidations': ['破位'], 'evidenceKeys': ['details.600001.snapshot']}]}]
    service, db, calls = fixture(tmp_path, monkeypatch, responses)
    result = service.run()
    assert len(calls) == 2
    assert result['candidateCount'] == 1
    assert result['candidates'][0]['status'] == 'conditional'
    assert result['delivery']['received_at'] is None
    assert not result['training_eligible']
    assert result['candidates'][0]['score'] is None
    assert result['candidates'][0]['probabilities'] == {}
    assert db.runs[0]['candidates'][0]['referencePrice'] == 11


def test_out_of_pool_hallucination_fails_without_fallback(tmp_path, monkeypatch):
    service, db, calls = fixture(tmp_path, monkeypatch, [{'codes': ['688001']}])
    result = service.run()
    assert result['status'] == 'unavailable'
    assert result['candidates'] == []
    assert len(calls) == 1


def test_late_retry_and_reviews_do_not_call_ai_or_generate_legacy_returns(tmp_path, monkeypatch):
    service, db, calls = fixture(tmp_path, monkeypatch, [], datetime(2026, 9, 8, 14, 57, tzinfo=mod.TZ))
    assert service.run('1455')['status'] == 'expired'
    assert service.run('review')['status'] == 'not_due'
    assert db.states == []
    service.clock = lambda: datetime(2026, 9, 8, 15, 30, tzinfo=mod.TZ)
    assert service.run('review')['review']['weightsChanged'] is False
    assert calls == []


def test_failed_ai_request_never_retries_and_does_not_expose_key(monkeypatch):
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs['json'])
        return SimpleNamespace(status_code=429)
    monkeypatch.setattr(mod.requests, 'post', fail)
    client = mod.HighClient({'model': 'deepseek-v4-pro', 'base_url': 'https://example.com/v1', 'api_key': 'test-secret'})
    with pytest.raises(ValueError, match='HTTP 429') as error:
        client.ask('test', {})
    assert 'test-secret' not in str(error.value)
    assert len(calls) == 1


def test_gateway_without_thinking_evidence_is_not_claimed_high(monkeypatch):
    monkeypatch.setattr(mod.requests, 'post', lambda *a, **k: SimpleNamespace(status_code=200,
        json=lambda: {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"codes":[]}'}}]}))
    client = mod.HighClient({'model': 'deepseek-v4-pro', 'base_url': 'https://example.com'})
    with pytest.raises(ValueError, match='无法核验 High'):
        client.ask('test', {})
