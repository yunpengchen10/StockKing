from copy import deepcopy

from src.services.yao_scout.potential_picks import select_potential_picks


STAMP = '2026-11-17T10:30:00+08:00'
BASELINE = '2026-11-16'


def candidate(code, ceiling=12.5):
    return {
        'code': code, 'name': code, 'dailyLastClose': 10,
        'quote': {'code': code, 'price': 10.4, 'source': 'test', 'source_time': STAMP,
                  'pre_close': 10, 'open_price': 10.1, 'high': 10.5, 'low': 10, 'limit_up': 11,
                  'amount': 102500, 'volume': 10000},
        'dailyEvidence': {'source': 'daily-feed', 'asOf': BASELINE, 'metrics': {
            'ma5': 10.2, 'ma10': 10.1, 'ma20': 10, 'atr14': .3, 'high20': ceiling, 'high60': ceiling}},
    }


def inputs(rows):
    screen = {'rows': [{'code': row['code'], 'limitEntry': {'code': row['code'], 'sourceTime': STAMP,
                        'source': 'test', 'price': row['quote']['price'], 'eligible': True, 'status': 'pre_limit'}}
                       for row in rows]}
    return {'controls': rows, 'dailyAssessment': {'baselineAsOf': BASELINE},
            'generatedAt': '2026-11-17T10:30:12+08:00'}, screen


def test_five_distinct_codes_ranked_by_risk_adjusted_room():
    rows = [candidate(f'60000{i}', 12+i) for i in range(7)]
    run, screen = inputs(rows)
    run['precisionResearch'] = list(reversed(rows))
    assert [(r['rank'], r['code']) for r in select_potential_picks(run, screen)] == [
        (1, '600006'), (2, '600005'), (3, '600004'), (4, '600003'), (5, '600002')]
    rows.reverse()
    assert [r['code'] for r in select_potential_picks(run, screen)] == [r['code'] for r in rows[:5]]


def test_a_touch_even_on_a_different_queue_or_later_risk_vetoes_the_symbol():
    rows = [candidate(f'60000{i}') for i in range(1, 6)]
    run, screen = inputs(rows)
    run['windvanes'] = [deepcopy(rows[0])]
    run['windvanes'][0]['quote']['high'] = 11
    run['nextDayContinuationWatchlist'] = [{'code': rows[1]['code'], 'nextDaySignal': {
        'touchedLimitToday': True, 'sourceTime': STAMP}}]
    run['marketBrief'] = {'focusStocks': [{'code': rows[2]['code'], 'limitEntry': {
        'code': rows[2]['code'], 'status': 'touched_limit', 'sourceTime': STAMP}}]}
    run['precisionResearch'] = [{'code': rows[3]['code'], 'eventAssessment': {'riskReasons': ['known_negative_earnings']}}]
    assert [r['code'] for r in select_potential_picks(run, screen)] == [rows[4]['code']]


def test_breakout_near_limit_can_qualify_without_projecting_an_upside_target():
    row = candidate('600001', 10.8)
    row['quote'].update(price=10.98, high=10.99, amount=107000)
    run, screen = inputs([row])
    chosen, = select_potential_picks(run, screen)
    assert chosen['selectionBasis'] == 'breakout'
    assert chosen['upsideToResistancePct'] is None
    assert chosen['sourceTime'] == STAMP and chosen['price'] == 10.98
    row['quote']['high'] = 11
    assert select_potential_picks(run, screen) == []


def test_insufficient_room_missing_evidence_and_stale_prices_are_not_promoted():
    rows = [candidate(f'60000{i}') for i in range(1, 5)]
    run, screen = inputs(rows)
    rows[0]['quote']['source_time'] = '2026-11-17T10:25:00+08:00'
    rows[1]['dailyEvidence']['asOf'] = '2026-11-13'
    rows[2]['dailyEvidence']['metrics']['atr14'] = None
    rows[3]['dailyEvidence']['metrics'].update(high20=10.41, high60=10.41)
    assert select_potential_picks(run, screen) == []


def test_prior_close_before_first_trade_and_actual_close_after_market():
    row = candidate('600001')
    run, screen = inputs([row])
    row.update(quote={}, price=10.4, dailyLastClose=10.4, referencePriceKind='previous_close',
               status='premarket', baselineAsOf=BASELINE, baselineSource='daily-feed', sourceTime=BASELINE)
    run['premarketCandidates'] = run.pop('controls')
    run['generatedAt'] = '2026-11-17T09:20:00+08:00'
    selected, = select_potential_picks(run, screen)
    assert (selected['basisLabel'], selected['sourceTime']) == ('前收盘价', BASELINE)
    run['generatedAt'] = '2026-11-17T09:26:00+08:00'
    assert select_potential_picks(run, screen) == []

    row = candidate('600001')
    run, _ = inputs([row])
    run['generatedAt'] = '2026-11-17T15:30:00+08:00'
    assert select_potential_picks(run, None) == []
    row['quote']['source_time'] = '2026-11-17T15:00:00+08:00'
    selected, = select_potential_picks(run, None)
    assert selected['basisLabel'] == '收盘价'
    row['quote']['high'] = 11
    assert select_potential_picks(run, None) == []
