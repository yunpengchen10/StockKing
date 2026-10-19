"""Completed daily-bar conditions, independent of minute models and their ledger.

The lane uses downloaded historical daily bars immediately. It never treats a
daily rule as a calibrated probability, a morning T0 model, or a verified fill.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, time
import re

from .daily_opportunities import finite, is_market_open, quote_check
from .premarket import _at, _daily_valid, _local, previous_session
from .precision_policy import execution_quote_data, POLICIES
from .research_policy import DEFAULT_PARAMETERS

VERSION = 'king-daily-primary-v1'
TASK_ID = 'daily_structure_to_next_session'


def _number(value):
    return None if isinstance(value, bool) else finite(value)


def _known(record, now):
    stamps = [_at(record.get(key)) for key in ('observedAt', 'availableAt', 'firstSeenAt')]
    return bool(record.get('source') and all(stamp is not None and stamp <= now for stamp in stamps)
                and stamps[2] <= stamps[0] and stamps[2] <= stamps[1])


def _events(context, now):
    """Coverage, disclosure availability and negative risks are separate facts."""
    missing, adverse = [], []
    coverage = context.get('coverage') or {}
    if any(coverage.get(key) is not True for key in ('financials', 'forecast', 'unlock')):
        missing.append('financial_forecast_or_unlock_coverage_missing')
    context_at = _at(context.get('decisionAt'))
    if context_at is None or not 0 <= (now-context_at).total_seconds() <= 21600:
        missing.append('financial_context_time_missing_future_or_stale')
    financials = context.get('financials') or {}
    values = {key: _number(financials.get(key)) for key in ('profit', 'eps', 'cashPerShare')}
    if not _known(financials, now) or any(value is None for value in values.values()):
        missing.append('financial_report_or_availability_unverified')
    elif values['profit'] <= 0 or values['eps'] <= 0:
        adverse.append('known_loss_or_nonpositive_earnings')
    for event in context.get('forecasts') or []:
        if not _known(event, now) or not event.get('type') or not event.get('metric'):
            missing.append('forecast_disclosure_unverified')
        elif event['type'] in DEFAULT_PARAMETERS['entry']['negativeForecastTypes'] and '净利润' in event['metric']:
            adverse.append('known_negative_earnings_forecast')
    unlock = 0.
    for event in context.get('unlocks') or []:
        if not _known(event, now):
            missing.append('unlock_disclosure_unverified')
            continue
        try:
            days = (date.fromisoformat(str(event.get('eventDate'))) - now.date()).days
        except ValueError:
            missing.append('unlock_date_unverified')
            continue
        ratio = _number(event.get('floatRatioPct'))
        if ratio is None or ratio < 0:
            missing.append('unlock_ratio_unverified')
        elif 0 <= days <= 14:
            unlock += ratio
    return {'missingReasons': sorted(set(missing)), 'riskReasons': sorted(set(adverse)),
            'unlock14dPct': unlock, 'positiveOperatingCashFlow': bool(values['cashPerShare'] is not None
                                                                   and values['cashPerShare'] > 0),
            'coverage': dict(coverage)}


def _market(evidence, now):
    packet = evidence or {}
    values = packet.get('features') or {}
    stamp = _at(packet.get('asOf'))
    coverage, universe, breadth = (_number(values.get(key)) for key in
                                  ('source_coverage', 'universe_count', 'advancing_pct'))
    available = bool(packet.get('source') and stamp and 0 <= (now-stamp).total_seconds() <= 300
                     and coverage is not None and .8 <= coverage <= 1
                     and universe is not None and universe >= 300
                     and breadth is not None and 0 <= breadth <= 100)
    blocked = bool(available and (breadth < 50 or packet.get('regime') == 'weak'))
    return {'status': 'weak_market_veto' if blocked else 'observed_non_weak' if available else 'unavailable',
            'available': available, 'passed': not blocked if available else None,
            'advancingPct': breadth if available else None,
            'source': packet.get('source') if available else None,
            'asOf': packet.get('asOf') if available else None,
            'meaning': '有可靠负面广度时否决；缺失时不宣称市场环境已通过'}


def _limit_evidence_current(quote, now):
    """An auxiliary provider's limit timestamp cannot borrow the newer quote time."""
    evidence = quote.get('price_limit_evidence')
    if evidence is None:
        return True  # Direct provider fields are checked by execution_quote_data.
    stamp = _at(evidence.get('source_time'))
    if (evidence.get('status') != 'observed' or evidence.get('method') != 'explicit_provider_fields'
            or not evidence.get('source') or stamp is None or stamp.date() != now.date()
            or not 0 <= (now-stamp).total_seconds() <= 30):
        return False
    for field in ('limit_up', 'limit_down'):
        value = _number(evidence.get(field))
        if value is None or quote.get(field) is None or abs(value-quote[field]) > .000001:
            return False
    reference = _number(evidence.get('previous_close'))
    return bool(reference and quote.get('pre_close') and abs(reference-quote['pre_close']) <= .000001)


def build_daily_candidates(queued, daily_by_code, quotes_by_code, decision_at, *, top_n=5,
                           contexts=None, market_evidence=None):
    """Return independent daily candidates/profiles plus an inspectable gate summary.

    ``daily_by_code`` contains packets produced by ``daily_evidence`` from real
    completed, unadjusted daily history. ``contexts`` contains ``for_stock``
    disclosure packets. Minute lineage, minute factors and morning predictions
    are deliberately never read. The caller must keep this task out of the
    minute/morning training ledger and recheck quote freshness at publication.
    """
    now = _local(decision_at)
    market = _market(market_evidence, now)
    assessment = {'version': VERSION, 'taskId': TASK_ID, 'label': '日线条件推荐',
        'asOf': now.isoformat(), 'status': 'completed', 'validationStatus': 'unvalidated_rules',
        'probability': None, 'horizon': 'next_session', 'minuteDataRequired': False,
        'morningModelRequired': False, 'minuteSectorFundRequired': False,
        'marketBreadth': market, 'dailyUsableCount': 0, 'quoteUsableCount': 0,
        'eventCoveragePassedCount': 0, 'researchedCount': 0, 'excludedCounts': {},
        'profileExcludedCounts': {}, 'profileQualifiedCounts': {}, 'count': 0,
        'meaning': '真实已完成日K、当日报价及已披露风险的独立条件筛选；不表示分钟主力确认、上午T0模型通过或上涨概率'}
    result = {'dailyCandidates': [], 'dailyProfileCandidates': {key: [] for key in POLICIES},
              'dailyAssessment': assessment}
    clock = now.time().replace(tzinfo=None)
    if (not is_market_open('cn', now.date()) or not
            (time(9, 30) <= clock <= time(11, 30) or time(13) <= clock < time(14, 57))):
        assessment.update(status='outside_continuous_trading_window')
        return result
    previous = previous_session(now).isoformat()
    assessment['baselineAsOf'] = previous
    excluded, profiles, profile_excluded, seen = Counter(), {key: [] for key in POLICIES}, {key: Counter() for key in POLICIES}, set()
    for item in queued:
        code, name = str(item.get('code') or ''), str(item.get('name') or item.get('code') or '')
        if code in seen:
            continue
        seen.add(code)
        if not re.fullmatch(r'(600|601|603|605|000|001|002|003)\d{3}', code) or re.search(r'ST|退|整理|停牌', name, re.I):
            excluded['unsupported_or_risk_board'] += 1
            continue
        daily = daily_by_code.get(code) or {}
        prior = daily.get('previousSession') or {}
        if (not _daily_valid(daily, now, previous) or prior.get('priceBasis') != 'unadjusted'
                or prior.get('asOf') != previous or prior.get('source') != daily.get('source')
                or 'unknown' in str(daily.get('source')).lower()):
            excluded['daily_history_source_basis_or_latest_session_unverified'] += 1
            continue
        d = {key: _number(value) for key, value in daily['metrics'].items()}
        required = ('last_close', 'ma5', 'ma10', 'ma20', 'high20', 'low20', 'atr14', 'history_sessions')
        prior_close = _number(prior.get('close'))
        if (any(d.get(key) is None or d[key] <= 0 for key in required) or d['history_sessions'] < 20
                or prior_close is None or abs(prior_close-d['last_close']) > .000001):
            excluded['daily_history_prices_invalid'] += 1
            continue
        assessment['dailyUsableCount'] += 1
        quote = dict(quotes_by_code.get(code) or {})
        for key in ('price', 'pre_close', 'high', 'low', 'open_price', 'change_pct',
                    'amount', 'volume', 'limit_up', 'limit_down'):
            quote[key] = _number(quote.get(key))
        execution = execution_quote_data({'code': code, 'quote': quote})
        if (quote_check(quote, code, now) or execution['status'] != 'complete'
                or not _limit_evidence_current(quote, now)):
            excluded['quote_or_provider_price_limits_unverified'] += 1
            continue
        price, prev, amount, volume = (_number(quote.get(key)) for key in ('price', 'pre_close', 'amount', 'volume'))
        if abs(prev-d['last_close']) > DEFAULT_PARAMETERS['entry']['previousCloseToleranceCny']:
            excluded['daily_quote_previous_close_mismatch'] += 1
            continue
        if (any(quote.get(key) is True for key in ('suspended', 'locked_limit_up', 'corporate_action', 'no_price_limit'))
                or quote.get('quote_usable_for_current_price_check') is False
                or amount is None or volume is None or amount <= 0 or volume <= 0
                or not quote['low']-.01 <= amount/volume <= quote['high']+.01
                or not quote['limit_down']+.005 < price < quote['limit_up']-.005):
            excluded['not_tradable_or_limit_locked'] += 1
            continue
        assessment['quoteUsableCount'] += 1
        events = _events((contexts or {}).get(code) or {}, now)
        if events['missingReasons'] or events['riskReasons']:
            excluded.update(events['missingReasons'] + events['riskReasons'])
            continue
        assessment['eventCoveragePassedCount'] += 1
        if market['status'] == 'weak_market_veto':
            excluded['verified_weak_market'] += 1
            continue
        if not d['ma5'] > d['ma10'] > d['ma20'] or price < d['ma20']:
            excluded['daily_uptrend_not_confirmed'] += 1
            continue
        support = max((d[key] for key in ('ma20', 'low20') if 0 < d[key] < price), default=None)
        resistance = min((d[key] for key in ('high20', 'high60') if d.get(key) and d[key] > price), default=None)
        if support is None or resistance is None:
            excluded['daily_support_or_upside_space_unknown'] += 1
            continue
        risk_distance = max(price-support, d['atr14'] * DEFAULT_PARAMETERS['entry']['volatilityRiskFloorAtr'])
        rr, bias, extension = (resistance-price)/risk_distance, (price/d['ma5']-1)*100, (price-d['ma5'])/d['atr14']
        indicator_keys = ('last_close', 'ma5', 'ma10', 'ma20', 'high20', 'high60', 'low20', 'atr14')
        indicators = [{'key': key, 'label': key, 'value': d.get(key), 'unit': '元', 'source': daily['source'],
                       'asOf': previous, 'status': 'observed' if d.get(key) is not None else 'missing'} for key in indicator_keys]
        gaps = ['未依赖分钟板块、分钟资金或上午T0模型；其状态不构成本分支确认']
        if not market['available']:
            gaps.append('全市场广度未取得或未满足时效覆盖要求，市场环境保持未知')
        reasons = [f"{int(d['history_sessions'])}个已完成日K截至{previous}，报价前收盘与日线末收盘一致",
                   'MA5>MA10>MA20，现价维持MA20上方',
                   f'压力空间/支撑及ATR风险距离为{rr:.2f}倍；MA5乖离{bias:.2f}%',
                   '财报、业绩预告及未来14日解禁覆盖已核验']
        risk_notes = ['日线条件规则尚未经过概率校准；排序不代表上涨概率',
                      '按A股T+1约束评估下一交易日；支撑和压力不是保证成交的止损价或收益目标']
        candidate = {'code': code, 'name': name, 'status': 'daily_conditional', 'stateLabel': '日线条件推荐',
            'strategyBranch': '日线趋势与T+1价格纪律', 'strategyId': TASK_ID,
            'version': VERSION, 'decision_at': now.isoformat(), 'quote': quote,
            'price': price, 'signalPrice': price, 'referencePrice': price,
            'source': quote.get('source'), 'sourceTime': quote.get('provider_timestamp') or quote.get('source_time'),
            'dailyLastClose': d['last_close'], 'dailyPreviousSession': dict(prior),
            'baselineAsOf': previous, 'baselineSource': daily['source'], 'dailyEvidence': daily,
            'dailyEvidenceStatus': 'completed_daily_source_and_quote_preclose_verified',
            'support': support, 'resistance': resistance, 'atr14': d['atr14'], 'structureRewardRisk': rr,
            'biasMa5Pct': bias, 'atrExtension': extension, 'riskDistance': risk_distance,
            'indicatorEvidence': indicators, 'selectionReasons': reasons, 'reasons': reasons,
            'risks': risk_notes, 'riskFlags': risk_notes, 'dataGaps': gaps, 'eventAssessment': events,
            'marketBreadth': market, 'entryEligible': True, 'executable': False,
            'validationStatus': 'unvalidated_rules', 'probability': None, 'probabilities': {}, 'confidence': None,
            'horizon': '下一交易日（T+1）', 'horizonId': 'next_session',
            'minuteModelTrainingEligible': False, 'trainingEligible': False, 'training_eligible': False,
            'rankMeaning': '先按当日已成交金额、再按MA5偏离和代码排序；规则排序不是上涨概率',
            'dailySignal': {'version': VERSION, 'taskId': TASK_ID, 'horizon': 'next_session',
                'validationStatus': 'unvalidated_rules', 'probability': None, 'minuteDataRequired': False,
                'morningModelRequired': False, 'trainingEligibleAtSignal': False},
            'executionQuoteData': execution}
        decisions = {}
        for key, rule in POLICIES.items():
            blocked = []
            if bias > rule.max_bias_pct or extension > rule.max_atr_extension:
                blocked.append('daily_ma5_extension_too_large')
            if rr < rule.min_reward_risk:
                blocked.append('daily_upside_reward_risk_insufficient')
            if events['unlock14dPct'] >= rule.max_unlock_pct:
                blocked.append('known_large_unlock')
            if rule.require_financial_quality and not events['positiveOperatingCashFlow']:
                blocked.append('known_nonpositive_operating_cash_flow')
            decisions[key] = {'label': rule.label, 'entryEligible': not blocked,
                              'state': 'avoid' if blocked else 'conditional', 'reasons': blocked}
            profile_excluded[key].update(blocked)
        candidate['dailyProfileDecision'] = decisions
        for key in POLICIES:
            if decisions[key]['entryEligible']:
                profiles[key].append(candidate)
    limit = max(0, min(5, int(top_n)))
    for key, candidates in profiles.items():
        candidates.sort(key=lambda row: (-_number(row['quote']['amount']), abs(row['biasMa5Pct']), row['code']))
        result['dailyProfileCandidates'][key] = [{**row, 'rank': rank, 'riskProfile': key}
                                                for rank, row in enumerate(candidates[:limit], 1)]
    result['dailyCandidates'] = result['dailyProfileCandidates']['regular']
    assessment.update(researchedCount=len(seen), excludedCounts=dict(excluded),
        profileExcludedCounts={key: dict(value) for key, value in profile_excluded.items()},
        profileQualifiedCounts={key: len(value) for key, value in profiles.items()},
        count=len(result['dailyCandidates']))
    if not result['dailyCandidates']:
        assessment['status'] = 'no_daily_candidates'
    assessment['message'] = (f"日线条件推荐{assessment['count']}只；可用日K {assessment['dailyUsableCount']}/{len(seen)}只，"
                             f"日线与新鲜报价核验{assessment['quoteUsableCount']}只，事件风险覆盖通过{assessment['eventCoveragePassedCount']}只。")
    return result
