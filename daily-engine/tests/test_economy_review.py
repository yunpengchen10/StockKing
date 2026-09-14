import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from src.services.economy_review import EconomyReviewService, summarize, encode

CONFIG={'config_id':1,'model':'deepseek-v4-pro','api_key':'test-only-secret','base_url':'https://fixture.invalid','max_tokens':1000}
FACTS={'600001':{'price':10,'source_time':'2026-09-10T10:00:00+08:00','modelStatus':'not_validated'}}

def response(**kwargs):
    return SimpleNamespace(status_code=200,json=lambda:{'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'reviews':[{'code':'600001','summary':'数据仅作观察','risks':['缺少实时成交'],'references':['600001.price']}]})}}],**kwargs})

def test_durable_idempotency_cache_budget_and_secret_exclusion(tmp_path):
    calls=[]
    def post(*args,**kwargs):
        calls.append(kwargs)
        return response(usage={'prompt_tokens':100,'completion_tokens':20,'total_tokens':120})
    path=tmp_path/'review.db'
    service=EconomyReviewService(path,post)
    service.settings({'daily_limit':1})
    first=service.run('request-0001',CONFIG,['600001'],FACTS,'审查')
    second=EconomyReviewService(path,post).run('request-0002',CONFIG,['600001'],FACTS,'审查')
    assert first['usage']['total_tokens']==120 and second['cacheHit']
    assert len(calls)==1 and service.usage()['usedRequests']==1
    assert calls[0]['json']['thinking']=={'type':'disabled'}
    assert calls[0]['json']['max_tokens']==1000
    assert calls[0]['allow_redirects'] is False
    with pytest.raises(ValueError,match='额度'):
        service.run('request-0003',CONFIG,['600001'],FACTS,'另一次审查')
    assert CONFIG['api_key'].encode() not in path.read_bytes()

@pytest.mark.parametrize('failure',['402','timeout','length','invalid','reference'])
def test_failed_attempts_are_counted_and_never_retried(tmp_path,failure):
    calls=[]
    def post(*a,**kw):
        calls.append(1)
        if failure=='timeout': raise TimeoutError('credential must not leak')
        if failure=='402':return SimpleNamespace(status_code=402)
        payload=response().json()
        if failure=='length':payload['choices'][0]['finish_reason']='length'
        elif failure=='invalid':payload['choices'][0]['message']['content']='not json'
        else:payload['choices'][0]['message']['content']=payload['choices'][0]['message']['content'].replace('600001.price','invented')
        return SimpleNamespace(status_code=200,json=lambda:payload)
    service=EconomyReviewService(tmp_path/'r.db',post)
    for _ in range(2):
        with pytest.raises(ValueError):service.run('request-fail',CONFIG,['600001'],FACTS,'审查')
    assert len(calls)==1 and service.usage()['usedRequests']==1
    assert service.usage()['records'][0]['usage'] is None

def test_concurrent_cancel_does_not_refund_or_publish(tmp_path):
    entered=threading.Event();release=threading.Event()
    def post(*a,**kw):entered.set();release.wait(10);return response()
    service=EconomyReviewService(tmp_path/'r.db',post)
    with ThreadPoolExecutor() as pool:
        future=pool.submit(service.run,'request-long',CONFIG,['600001'],FACTS,'审查')
        assert entered.wait(5)
        with pytest.raises(ValueError):service.run('request-next',CONFIG,['600001'],FACTS,'审查')
        service.cancel('request-long');release.set()
        with pytest.raises(ValueError,match='取消'):future.result()
    assert service.status('request-long')['status']=='cancelled'
    assert service.usage()['usedRequests']==1

def test_oversize_and_unsupported_models_never_send(tmp_path):
    service=EconomyReviewService(tmp_path/'r.db',lambda *a,**kw:pytest.fail('unexpected transport'))
    with pytest.raises(ValueError,match='上限'):service.run('request-size',CONFIG,['600001'],FACTS,'x'*30000)
    with pytest.raises(ValueError,match='兼容'):service.run('request-model',{**CONFIG,'model':'reasoner'},['600001'],FACTS,'审查')
    assert service.usage()['usedRequests']==0

def test_summary_reduces_large_historical_packet_without_changing_facts():
    evidence={**FACTS,'history':[{'report':'历史报告'*2000} for _ in range(100)],'api_key':'secret'}
    summary=summarize(evidence)
    assert len(encode(summary).encode())<len(encode(evidence).encode())*.2
    assert summary['600001.price']==10 and not any('history' in k or 'api_key' in k for k in summary)

def test_missing_usage_is_unknown_and_evidence_changes_invalidate_cache(tmp_path):
    calls=[]
    service=EconomyReviewService(tmp_path/'r.db',lambda *a,**kw:(calls.append(1) or response()))
    assert service.run('request-one1',CONFIG,['600001'],FACTS,'审查')['usage'] is None
    service.run('request-two2',CONFIG,['600001'],{'600001':{**FACTS['600001'],'price':11}},'审查')
    assert len(calls)==2

def test_local_scan_uses_no_llm_and_caps_candidates(tmp_path,monkeypatch):
    import pandas as pd
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.services.yao_scout import local_opportunities as local
    from src.services.yao_scout import daily_opportunities as base
    saved=[]
    db=SimpleNamespace(list_yao_runs=lambda **kw:[],list_yao_dlm=lambda **kw:[],save_yao_run=saved.append)
    frame=pd.DataFrame([{'code':f'60000{i}','name':'股票','price':10} for i in range(8)])
    yao=SimpleNamespace(db=db,_fetch_snapshot=lambda:frame,_snapshot_meta=lambda *a,**kw:{})
    monkeypatch.setattr(base.HighClient,'ask',lambda *a:pytest.fail('LLM in background'))
    monkeypatch.setattr(base,'is_market_open',lambda *a:True)
    monkeypatch.setattr(local,'get_tier_model_service',lambda:SimpleNamespace(score_local_five_day=lambda rows:rows))
    result=local.LocalOpportunityService(yao,{'api_key':'ignored'},quote_fetcher=lambda code:{'code':code},clock=lambda:datetime(2026,9,10,10,30,tzinfo=ZoneInfo('Asia/Shanghai'))).run('1030')
    assert len(result['candidates'])==5 and result['modelVersion']==local.VERSION
    assert result['llmUsed'] is False and result['aiAudit']==[]


def test_cancel_before_submission_is_durable_and_free(tmp_path):
    service=EconomyReviewService(tmp_path/'r.db',lambda *a,**kw:pytest.fail('cancelled request sent'))
    service.cancel('request-before')
    with pytest.raises(ValueError):service.run('request-before',CONFIG,['600001'],FACTS,'审查')
    assert service.usage()['usedRequests']==0


def test_classic_screening_disables_llm_and_remote_post_analysis(monkeypatch):
    from contextlib import nullcontext
    from src.services import screening_service as mod
    calls=[]
    monkeypatch.setattr(mod,'_screening_runtime_env',lambda *a,**kw:nullcontext())
    monkeypatch.setattr(mod,'_screening_litellm_headers',lambda *a,**kw:nullcontext())
    monkeypatch.setattr(mod.ScreeningPipelineConfig,'from_env',lambda:object())
    monkeypatch.setattr(mod,'_build_screening_context',lambda *a,**kw:{})
    monkeypatch.setattr(mod,'_build_screening_dsa_daily_history_fetcher',lambda:None)
    monkeypatch.setattr(mod,'run_screening_pipeline',lambda *a,**kw:calls.append(kw))
    mod._call_screening_screen('balanced_alpha','cn',5,None,local_only=True)
    assert calls[0]['use_llm'] is False
    assert calls[0]['post_analyzers']==['scorecard']
    assert calls[0]['deep_analysis'] is False


def test_missing_tokenizer_data_uses_explicit_estimate(monkeypatch):
    import sys
    from src.services.economy_review import count_tokens
    def unavailable(model):
        raise OSError('tokenizer cache unavailable')
    monkeypatch.setitem(sys.modules,'tiktoken',SimpleNamespace(encoding_for_model=unavailable))
    count,method=count_tokens({'evidence':'本地证据'},'gpt-4o')
    assert count>0 and method=='estimate'
