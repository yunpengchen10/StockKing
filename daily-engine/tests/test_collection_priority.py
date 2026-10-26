from copy import deepcopy

from src.services.yao_scout.local_opportunities import fund_collection_order


def test_fund_io_prioritizes_viable_minutes_without_dropping_other_stocks():
    packet = {'historyDays':20, 'metrics':{'relative_amount_3m_20d':1.2,
        'speed_3m_pct':.1, 'speed_5m_pct':.2}}
    intraday = {'600002':packet, '600004':deepcopy(packet),
        '600003':{**packet, 'historyDays':19},
        '600005':{**packet, 'metrics':{**packet['metrics'], 'speed_5m_pct':-.1}}}
    before = deepcopy(intraday)
    codes = ['600001','600003','600002','600005','600004']
    assert fund_collection_order(codes, intraday) == ['600002','600004','600001','600003','600005']
    assert intraday == before
    assert codes == ['600001','600003','600002','600005','600004']
