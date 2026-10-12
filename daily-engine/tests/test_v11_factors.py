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
    assert score['scoreVersion']=='stockking-v1.2-rules'
    assert score['factorScores']['C'] is None and score['factorScores']['G'] is None
    assert score['factorScores']['L'] is None
    assert score['factorWeightsApplied']['early']['M']==pytest.approx(EARLY_WEIGHTS['M'],abs=1e-6)
    assert score['factorWeightsApplied']['main']['F']==pytest.approx(MAIN_WEIGHTS['F'],abs=1e-6)
    assert score['selectedScoreBranch']=='early'
    assert score['finalScore']==pytest.approx(score['earlyScore']-.2*score['distributionRisk'],abs=1e-4)
    assert score['expectedMFE5'] is None and score['mainRiseProbability'] is None
    assert score['firstTradablePrice'] is None and score['tradability']['status']=='unverified'
    assert score['dataConfidence'] < 1
    assert score['confidenceStatus']=='complete'
    assert score['observableStructure']=='分钟局部突破观察'
    from src.services.yao_scout.local_algorithm import algorithm_contract
    contract = algorithm_contract()
    assert score['scoreVersion'] == contract['scoreVersion']
    assert score['riskPenaltyCoefficient'] == contract['riskPenaltyCoefficient']
    assert score['factorWeightsApplied']['early']['M'] == pytest.approx(contract['weights']['early']['M'], abs=1e-6)


def test_v12_under_five_days_emits_no_score():
    rows = [row for day in DATES[:4] for row in _bars(day)] + _bars(NOW.date(),current=True)
    minute = compute_bar_evidence(rows,NOW,expected_dates=DATES)
    score = score_v11(_candidate(),minute,now=NOW)
    assert score['historicalCoverageDays']==4
    assert score['factorScores']['M'] is None and score['factorScores']['V'] is None
    assert all(value is None for value in score['factorScores'].values())
    assert score['finalScore'] is None
    assert score['dataConfidence']==0
    assert score['dataEligibility']['status']=='insufficient'


def test_missing_historical_amount_does_not_erase_price_momentum_baseline():
    prices_only = [{**row,'amount_cny':None}
                   for day in DATES for row in _bars(day)]
    minute = compute_bar_evidence(prices_only+_bars(NOW.date(),amount=2000,current=True),
                                  NOW,expected_dates=DATES)
    assert minute['historyDays']==0 and minute['priceHistoryDays']==20
    assert minute['v11Inputs']['z_r3_pct'] is not None
    assert minute['v11Inputs']['v3_ratio'] is None
    score = score_v11(_candidate(),minute,now=NOW)
    assert score['v11Inputs']['z_r3_pct'] is not None  # raw observations retained
    assert score['factorScores']['M'] is None
    assert score['factorScores']['V'] is None
    assert score['priceHistoricalCoverageDays']==20
    assert score['confidenceStatus']=='insufficient'
    assert score['finalScore'] is None


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
    monkeypatch.setattr(minute_history,'fetch_sina_bars',lambda *a,**k: [])
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
    monkeypatch.setattr(minute_history,'fetch_sina_bars',lambda *a,**k: [])
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
    assert first['historyBackfill']['reason']=='provider_returned_no_older_minutes'
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
    assert not explanation['evidenceChecks']['history_20d']
    candidate = {**_candidate(),**explanation,'status':'conditional',
                 'referencePrice':quote['price'],'quote':quote}
    candidate.update(score_v11(candidate,minute,now=NOW))
    assert candidate['confidenceStatus']=='low'
    assert candidate['finalScore'] is not None
    assert candidate['factorScores']['F'] is None and candidate['factorScores']['S'] is None
    assert candidate['dataEligibility']['status']=='observation'
    assert not any(profile['entryEligible'] for profile in
        evaluate(candidate,{'coverage':{},'gaps':[]},{})['profiles'].values())


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


def _full_minute():
    return compute_bar_evidence([row for day in DATES for row in _bars(day)]
        + _bars(NOW.date(), amount=2000, current=True), NOW, expected_dates=DATES)


def test_vwap_slope_percentage_scale_preserves_small_move_distinctions():
    minute = _full_minute()
    scores = {}
    for slope in (0., .1, .2, 1., 2.):
        minute['v11Inputs']['vwap_slope_5m_pct'] = slope
        scores[slope] = score_v11(_candidate(), minute, now=NOW)['factorScores']['W']
    assert scores[.1] - scores[0.] == pytest.approx(1.25)
    assert scores[.2] - scores[.1] == pytest.approx(1.25)
    assert scores[1.] == scores[2.]
    assert scores[.2] < scores[1.]


def test_turnover_is_funded_from_volume_budget_and_changes_rule_score():
    minute = _full_minute()
    candidate = _candidate()
    without = score_v11(candidate, minute, now=NOW)
    candidate['float_shares'] = 100_000_000
    with_turnover = score_v11(candidate, minute, now=NOW)
    assert EARLY_WEIGHTS['V'] + EARLY_WEIGHTS['T'] == pytest.approx(.14)
    assert MAIN_WEIGHTS['V'] + MAIN_WEIGHTS['T'] == pytest.approx(.10)
    assert with_turnover['finalScore'] - without['finalScore'] == pytest.approx(
        .05 * with_turnover['turnoverScore'], abs=1e-4)
    assert sum(EARLY_WEIGHTS.values()) == pytest.approx(1.)
    assert sum(MAIN_WEIGHTS.values()) == pytest.approx(1.)


@pytest.mark.parametrize('branch,stage,selected', [
    ('分钟局部突破观察','breakout','early'), ('分歧修复观察','repair','early'),
    ('分钟趋势延续观察','continuation','main')])
def test_stage_selects_one_prespecified_score_not_the_maximum(branch, stage, selected):
    candidate = {**_candidate(), 'strategyBranch': branch}
    result = score_v11(candidate, _full_minute(), now=NOW)
    assert result['scoreStage'] == stage and result['selectedScoreBranch'] == selected
    base = result['earlyScore'] if selected == 'early' else result['mainRiseScore']
    assert result['finalScore'] == pytest.approx(max(0, base-.2*result['distributionRisk']), abs=1e-4)


def test_missing_positive_evidence_never_redistributes_or_increases_score():
    candidate = _candidate()
    complete = score_v11(candidate, _full_minute(), now=NOW)
    candidate['indicatorEvidence'] = [row for row in candidate['indicatorEvidence']
        if row['key'] not in {'active_buy_share_pct','main_net_flow_3m'}]
    missing = score_v11(candidate, _full_minute(), now=NOW)
    assert missing['finalScore'] <= complete['finalScore']
    assert missing['factorWeightsApplied']['early']['M'] == complete['factorWeightsApplied']['early']['M']
    assert missing['factorCoverage']['early'] < complete['factorCoverage']['early']
    assert missing['distributionRisk'] >= complete['distributionRisk']
    assert 'F' in missing['missingFactors'] and 'activeSell' in missing['missingRiskComponents']


def _packet(score, **changes):
    return {'status':'observed', 'score':score, 'asOf':NOW.isoformat(),
            'features':{'sample_count':20}, 'gaps':[], **changes}


@pytest.mark.parametrize('packet', [
    _packet(80, asOf=(NOW+timedelta(seconds=1)).isoformat()),
    _packet(80, asOf=NOW.replace(tzinfo=None).isoformat()),
    _packet(80, asOf=(NOW-timedelta(days=2)).isoformat()),
    _packet(80, status='insufficient'), _packet(float('nan')), _packet(101),
    _packet(80, features={'bad':float('inf')})])
def test_invalid_or_future_research_packet_cannot_score_or_enter_training_features(packet):
    candidate = {**_candidate(), 'researchFactors':{'C':packet}}
    result = score_v11(candidate, _full_minute(), now=NOW)
    assert result['factorScores']['C'] is None
    assert not any(key.startswith('research_C_') for key in result['v11Inputs'])


def test_verified_cg_are_scored_emotion_only_reduces_and_theme_is_shadow():
    minute = _full_minute()
    candidate = _candidate()
    baseline = score_v11(candidate, minute, now=NOW)
    candidate['researchFactors'] = {'C':_packet(80), 'G':_packet(60), 'H':_packet(100)}
    scored = score_v11(candidate, minute, now=NOW)
    assert scored['finalScore'] - baseline['finalScore'] == pytest.approx(.08*80+.07*60, abs=1e-4)
    assert scored['v11Inputs']['research_H_score'] == 100
    assert scored['marketRegime']['state'] == 'unknown'
    candidate['researchFactors']['E'] = _packet(10)
    weak = score_v11(candidate, minute, now=NOW)
    assert weak['marketRegime']['state']=='weak'
    assert weak['finalScore']==pytest.approx(scored['finalScore']*.5, abs=1e-4)
    candidate['researchFactors']['E'] = _packet(90)
    hot = score_v11(candidate, minute, now=NOW)
    assert hot['finalScore']==pytest.approx(scored['finalScore']*.75, abs=1e-4)


def test_twenty_days_does_not_override_missing_critical_factors_or_stale_minutes():
    minute = _full_minute()
    for key in ('z_r3_pct','z_r5_pct','z_a3_pct_per_min','z_a1_pct_per_min'):
        minute['v11Inputs'][key] = None
    result = score_v11(_candidate(), minute, now=NOW)
    assert result['dataEligibility']['status']=='observation'
    assert not result['dataEligibility']['criticalFactorsReady']
    assert score_v11(_candidate(), _full_minute(), now=NOW+timedelta(minutes=5))['finalScore'] is None
