from datetime import datetime, timedelta

import pytest

from src.services.yao_scout.intraday_evidence import SHANGHAI
from src.services.yao_scout.minute_history import compute_bar_evidence, normalize_bars, fetch_bar_evidence, MinuteArchive
from src.services.yao_scout.v11_factors import score_v11, EARLY_WEIGHTS, MAIN_WEIGHTS

NOW = datetime(2026, 9, 18, 10, 30, tzinfo=SHANGHAI)
DATES = [NOW.date()-timedelta(days=i) for i in range(1,21)]


def _bars(day, amount=1000, current=False):
    end = NOW.replace(year=day.year,month=day.month,day=day.day)
    rows = []
    for offset in range(17):
        at = end-timedelta(minutes=16-offset)
        close = 10.1 if current and offset == 16 else 10.
        rows.append({'end':at.isoformat(),'open':10.,'high':10.2,'low':9.8,
                     'close':close,'volume_shares':amount/10,'amount_cny':amount,
                     'source':'fixture'})
    return rows


def _candidate():
    values = {'vwap':9.95,'local_high_5m':10.05,'sector_relative_5m_pct':.4,
              'sector_return_5m_pct':.3,'sector_breadth':80,'active_buy_share_pct':60,
              'main_net_flow_3m':10000}
    return {'referencePrice':10.1,'strategyBranch':'分钟局部突破观察',
        'triggers':['突破后复核'],'invalidations':['跌回平台'],
        'quote':{'source_time':NOW.isoformat(),'active_flow_as_of':NOW.isoformat()},
        'indicatorEvidence':[{'key':key,'value':value,'status':'observed'} for key,value in values.items()]}


def test_v11_history_coverage_mad_floor_and_missing_amount():
    history = [row for day in DATES for row in _bars(day)]
    result = compute_bar_evidence(history+_bars(NOW.date(),amount=2000,current=True),NOW,expected_dates=DATES)
    assert result['v11Coverage']['status']=='complete'
    assert result['v11Inputs']['v3_ratio']==2
    assert result['v11Inputs']['z_r3_pct']==6  # zero historical MAD, bounded by floor and clip
    low = compute_bar_evidence(history[17:]+_bars(NOW.date(),current=True),NOW,expected_dates=DATES)
    assert low['v11Coverage']['status']=='low' and low['v11Coverage']['days']==19
    assert low['v11Coverage']['priceDays']==19
    assert low['v11Inputs']['v3_ratio'] is not None
    short = compute_bar_evidence(history[-68:]+_bars(NOW.date(),current=True),NOW,expected_dates=DATES)
    assert short['v11Coverage']['status']=='insufficient'
    assert short['v11Inputs']['v3_ratio'] is None
    no_amount = [{**row,'amount_cny':None} for row in _bars(NOW.date(),current=True)]
    missing = compute_bar_evidence(history+no_amount,NOW,expected_dates=DATES)
    assert missing['metrics']['amount_3m'] is None
    assert missing['v11Inputs']['v3_ratio'] is None
    assert missing['v11Inputs']['r3_pct'] is not None
    assert len(normalize_bars(no_amount,NOW))==17


def test_v11_weights_final_risk_and_uncalibrated_outputs():
    history = [row for day in DATES for row in _bars(day)]
    minute = compute_bar_evidence(history+_bars(NOW.date(),amount=2000,current=True),NOW,expected_dates=DATES)
    score = score_v11(_candidate(),minute,now=NOW)
    assert score['scoreVersion']=='stockking-v1.1-rules'
    assert score['factorScores']['C'] is None and score['factorScores']['G'] is None
    assert score['factorScores']['L'] is None
    assert score['factorWeightsApplied']['early']['M']==pytest.approx(EARLY_WEIGHTS['M']/.8,abs=1e-6)
    assert score['factorWeightsApplied']['main']['F']==pytest.approx(MAIN_WEIGHTS['F']/.83,abs=1e-6)
    assert score['finalScore']==pytest.approx(max(score['earlyScore'],score['mainRiseScore'])-.2*score['distributionRisk'],abs=1e-4)
    assert score['expectedMFE5'] is None and score['mainRiseProbability'] is None
    assert score['firstTradablePrice'] is None and score['tradability']['status']=='unverified'
    assert score['dataConfidence'] < 1
    assert score['confidenceStatus']=='complete'
    assert score['observableStructure']=='分钟局部突破观察'


def test_v11_under_five_days_only_nulls_historical_factors():
    rows = [row for day in DATES[:4] for row in _bars(day)] + _bars(NOW.date(),current=True)
    minute = compute_bar_evidence(rows,NOW,expected_dates=DATES)
    score = score_v11(_candidate(),minute,now=NOW)
    assert score['historicalCoverageDays']==4
    assert score['factorScores']['M'] is None and score['factorScores']['V'] is None
    assert score['factorScores']['W'] is not None and score['factorScores']['B'] is not None
    assert score['finalScore'] is not None
    assert 0 < score['dataConfidence'] < .35


def test_missing_historical_amount_does_not_erase_price_momentum_baseline():
    prices_only = [{**row,'amount_cny':None}
                   for day in DATES for row in _bars(day)]
    minute = compute_bar_evidence(prices_only+_bars(NOW.date(),amount=2000,current=True),
                                  NOW,expected_dates=DATES)
    assert minute['historyDays']==0 and minute['priceHistoryDays']==20
    assert minute['v11Inputs']['z_r3_pct'] is not None
    assert minute['v11Inputs']['v3_ratio'] is None
    score = score_v11(_candidate(),minute,now=NOW)
    assert score['factorScores']['M'] is not None
    assert score['factorScores']['V'] is None
    assert score['priceHistoricalCoverageDays']==20
    assert score['confidenceStatus']=='low'


def test_software_market_bars_feed_same_archive_and_missing_amount(monkeypatch,tmp_path):
    from src.services import software_market
    calls=[]
    class Client:
        available=True
        def bars(self,*args,**kwargs):
            calls.append(kwargs)
            return {'bars':[{**row,'amount_cny':None} for row in _bars(NOW.date(),current=True)],
                    'source':'software:fixture'}
    monkeypatch.setattr(software_market.SoftwareMarketClient,'from_environment',lambda:Client())
    monkeypatch.setattr('src.services.yao_scout.minute_history.fetch_sina_bars',
                        lambda *a,**k:pytest.fail('software bars should be preferred'))
    result = fetch_bar_evidence('600001',NOW,tmp_path,backfill=False)
    assert result['softwareMarketUsed']
    assert result['metrics']['amount_3m'] is None
    assert result['metrics']['speed_3m_pct'] is not None
    assert calls==[{'period':'1','count':8000}]


def test_missing_software_amount_cannot_erase_archived_real_amount(tmp_path):
    archive = MinuteArchive(tmp_path)
    archive.save('600001',_bars(NOW.date(),amount=1000),NOW)
    archive.save('600001',[{**row,'amount_cny':None} for row in _bars(NOW.date())],NOW)
    assert all(row['amount_cny']==1000 for row in archive.read('600001',NOW))


def test_duplicate_fetch_time_is_audit_metadata_not_bar_conflict():
    original = _bars(NOW.date())[0]
    rows = [{**original,'fetched_at':(NOW+timedelta(seconds=1)).isoformat()},
            {**original,'fetched_at':(NOW+timedelta(seconds=2)).isoformat()}]
    normalized = normalize_bars(rows,NOW)
    assert len(normalized)==1
    assert list(normalized.values())[0]['fetched_at'] is not None


def test_software_history_pages_only_older_bars_once_per_day(monkeypatch,tmp_path):
    from src.services import software_market
    from src.services.yao_scout import minute_history
    monkeypatch.setattr(minute_history,'_prior_sessions',lambda day:DATES)
    calls=[]
    latest = _bars(NOW.date(),current=True)+[row for day in DATES[:3] for row in _bars(day)]
    older = [row for day in DATES[3:] for row in _bars(day)]
    class Client:
        available=True
        def bars(self,code,period,count,**kwargs):
            calls.append(kwargs.get('end'))
            return {'bars':older if kwargs.get('end') else latest,'time_semantics':'bar_end'}
    monkeypatch.setattr(software_market.SoftwareMarketClient,'from_environment',lambda:Client())
    first = fetch_bar_evidence('600001',NOW,tmp_path,backfill=True)
    assert first['historyDays']==20 and first['historyBackfill']['newBars']==len(older)
    assert first['historyBackfill']['pages']==1
    assert calls[0] is None and calls[1]==(NOW.replace(year=DATES[2].year,month=DATES[2].month,
        day=DATES[2].day)-timedelta(minutes=17)).strftime('%Y%m%d%H%M%S')
    second = fetch_bar_evidence('600001',NOW,tmp_path,backfill=True)
    assert second['historyDays']==20
    assert sum(end is not None for end in calls)==1


def test_software_history_stops_when_provider_ignores_end(monkeypatch,tmp_path):
    from src.services import software_market
    from src.services.yao_scout import minute_history
    monkeypatch.setattr(minute_history,'_prior_sessions',lambda day:DATES)
    calls=[]
    latest = _bars(NOW.date(),current=True)+[row for day in DATES[:3] for row in _bars(day)]
    class Client:
        available=True
        def bars(self,code,period,count,**kwargs):
            calls.append(kwargs.get('end'))
            return {'bars':latest,'time_semantics':'bar_end'}
    monkeypatch.setattr(software_market.SoftwareMarketClient,'from_environment',lambda:Client())
    first = fetch_bar_evidence('600001',NOW,tmp_path,backfill=True)
    second = fetch_bar_evidence('600001',NOW,tmp_path,backfill=True)
    assert first['historyDays']==3
    assert first['historyBackfill']['status']=='empty'
    assert second['historyBackfill']['status']=='already_attempted_today'
    assert sum(end is not None for end in calls)==1


def test_v11_uses_five_to_nineteen_day_low_confidence_without_optional_factors():
    from src.services.yao_scout.recommendation_evidence import explain_candidate
    from src.services.yao_scout.precision_policy import evaluate
    minute = compute_bar_evidence(
        [row for day in DATES[1:] for row in _bars(day)] + _bars(NOW.date(),current=True),
        NOW,expected_dates=DATES)
    quote = {'code':'600001','source':'fixture','provider_timestamp':NOW.isoformat(),
             'price':10.1,'pre_close':10.,'open_price':10.,'high':10.2,'low':9.8,
             'amount':20000000,'volume':2000000,'change_pct':1.}
    minute['asOf']=NOW.isoformat()
    minute['metrics']['local_high_5m']=10.05
    explanation = explain_candidate({'code':'600001'},quote,{},minute,NOW,'1030',
                                    sector={},funds={},v11=True)
    assert explanation['evidenceEligible']
    assert not explanation['evidenceChecks']['history_20d']
    candidate = {**_candidate(),**explanation,'status':'conditional',
                 'referencePrice':quote['price'],'quote':quote}
    candidate.update(score_v11(candidate,minute,now=NOW))
    assert candidate['confidenceStatus']=='low'
    assert candidate['finalScore'] is not None
    assert candidate['factorScores']['F'] is None and candidate['factorScores']['S'] is None
    assert evaluate(candidate,{'coverage':{},'gaps':[]},{})['profiles']['regular']['entryEligible']


def test_turnover_and_book_are_observations_not_executed_fill():
    minute = compute_bar_evidence(
        [row for day in DATES for row in _bars(day)] + _bars(NOW.date(),amount=2000,current=True),
        NOW,expected_dates=DATES)
    candidate = _candidate()
    candidate['float_shares'] = 100_000_000
    candidate['quote']['quote_usable_for_current_price_check'] = True
    candidate['quote']['public_quote_metadata'] = {'order_book':{
        'source_time':NOW.isoformat(),
        'asks':[{'price':10.11,'volume_shares':100_000}],
        'bids':[{'price':10.10,'volume_shares':100_000}]}}
    score = score_v11(candidate,minute,now=NOW)
    assert score['turnoverScore'] is not None
    assert score['v11Inputs']['turnover_velocity_3m_pct'] > 0
    assert score['tradabilityScore'] is not None
    assert score['tradability']['status']=='order_book_observed'
    assert score['firstTradablePrice'] is None
    assert score['tradability']['executionVerified'] is False
