from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from src.services.yao_scout import sector_fund_evidence as mod
from tests.test_tencent_fund_evidence import packet, CUTOFF


def eastmoney_rows(values=(100., 130., 110., 120.)):
    end = datetime.fromisoformat(CUTOFF)
    return [','.join([str(end-timedelta(minutes=3-index)), str(value), '0', '0', str(value), '0'])
        for index, value in enumerate(values)]


def test_fund_persistence_is_same_provider_increment_sequence_not_day_summary():
    result = mod.compute_fund_evidence('600001', {'code': '600001', 'klines': eastmoney_rows()}, CUTOFF)
    assert result['metrics']['main_net_flow_1m_deltas'] == [30., -20., 10.]
    assert result['metrics']['main_net_positive_fraction_3m'] == pytest.approx(2/3)
    assert result['metrics']['main_net_flow_3m'] == 20.


def test_four_lunch_observations_do_not_form_a_trading_window():
    rows = [row.replace('13:57:00', '12:00:00').replace('13:58:00', '12:01:00')
            .replace('13:59:00', '12:02:00').replace('14:00:00', '12:03:00')
            for row in eastmoney_rows()]
    result = mod.compute_fund_evidence('600001', {'code': '600001', 'klines': rows},
        '2026-10-22T12:03:00+08:00')
    assert 'main_net_flow_3m' not in result['metrics']
    assert 'main_net_flow_1m_deltas' not in result['metrics']


@pytest.mark.parametrize('damage', ['missing', 'conflict', 'future', 'lunch'])
@pytest.mark.parametrize('provider', ['eastmoney', 'tencent'])
def test_incomplete_or_conflicting_window_never_invents_persistence(provider, damage):
    cutoff = CUTOFF
    if provider == 'eastmoney':
        rows = eastmoney_rows()
        if damage == 'missing':
            rows.pop(1)
        elif damage == 'conflict':
            rows.append(rows[1].replace(',130.0,0,0,130.0,0', ',140.0,0,0,140.0,0'))
        elif damage == 'future':
            rows[-1] = rows[-1].replace('14:00:00', '14:01:00')
        else:
            rows = [row.replace(row.split(',')[0], stamp) for row, stamp in zip(rows,
                ['2026-10-22 11:29:00+08:00', '2026-10-22 11:30:00+08:00',
                 '2026-10-22 13:01:00+08:00', '2026-10-22 13:02:00+08:00'])]
            cutoff = '2026-10-22T13:02:00+08:00'
        result = mod.compute_fund_evidence('600001', {'code': '600001', 'klines': rows}, cutoff)
    else:
        raw = packet()
        rows = raw['data']['todayFundTrend']['minList']
        if damage == 'missing':
            rows.pop(1)
        elif damage == 'conflict':
            duplicate = deepcopy(rows[1])
            for field in ('MainNetInflow', 'BigNetInflow', 'MainInflow'):
                duplicate[field] = str(float(duplicate[field])+1)
            rows.append(duplicate)
        elif damage == 'future':
            rows[-1]['time'] = '202610221400'
        else:
            for row, stamp in zip(rows, ['202610221128', '202610221129', '202610221300', '202610221301']):
                row['time'] = stamp
            cutoff = '2026-10-22T13:02:00+08:00'
        result = mod.compute_tencent_fund_evidence('600001', raw, cutoff)
    assert 'main_net_flow_3m' not in result['metrics']
    assert 'main_net_flow_1m_deltas' not in result['metrics']
    assert 'main_net_positive_fraction_3m' not in result['metrics']
