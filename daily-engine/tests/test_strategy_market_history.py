from datetime import date, datetime

from src.services.yao_scout.strategy_market import provider_path
from src.services.yao_scout.strategy_outcomes import summarize_strategy_outcomes


def test_minute_path_blocks_partial_future_and_other_symbol_and_uses_interval_units():
    symbol = 'sh000300'
    qt = [''] * 31
    qt[1:5] = ['沪深300', '000300', '3990', '3980']
    qt[30] = '20261119103352'
    root = {'qt': {symbol: qt}, 'data': {'date': '20261119', 'data': [
        '1030 3981 8123 900000', '1031 3982 8128 910000',
        '1032 3984 8135 921000', '1033 3990 8145 930000', '1034 3991 8150 940000']}}
    cutoff = datetime.fromisoformat('2026-11-19T10:34:00+08:00')
    payload = {'code': 0, 'data': {symbol: root}}
    path = provider_path(payload, symbol, cutoff)
    assert path['sourceTime'] == '2026-11-19T10:33:00+08:00'
    assert path['points'][-1] == {'sourceTime': path['sourceTime'], 'price': 3984, 'amount': 11000, 'volume': 700}
    assert path['previousClose'] == 3980
    assert not provider_path(payload, 'sz000300', cutoff)
    assert not provider_path(payload, symbol, cutoff.replace(day=20))
    qt[2] = '000001'
    assert not provider_path(payload, symbol, cutoff)


def test_stats_measure_published_hits_per_version_from_frozen_reference_and_verified_target_close():
    channels = [{'id': 'a', 'name': '甲', 'version': 'v1'}, {'id': 'b', 'name': '乙', 'version': 'v2'}]
    day = '2026-11-13T10:30:00+08:00'
    def stock(code, matches):
        return {'code': code, 'price': 10, 'strategyMatches': matches}
    doc = {'asOf': day, 'status': 'published', 'ruleSnapshots': channels, 'channels': channels, 'items': [
        stock('600001', channels), stock('600002', channels[:1]), stock('600003', channels[1:])]}
    def quote(_, code, target, now):
        assert target == '2026-11-16'
        return {'status': 'verified' if code != '600003' else 'missing_close', 'price_basis': 'unadjusted',
                'close': {'600001': 10.5, '600002': 9.5}.get(code)}
    args = {'calendar': lambda *_: [date(2026,11,13), date(2026,11,16)], 'close_reader': quote}
    evidence = summarize_strategy_outcomes([doc, {**doc, 'status': 'unrecorded'}, {**doc, 'ruleSnapshots': []}],
                                          '.', as_of='2026-11-16T15:01:00+08:00', **args)
    assert [(r['total'], r['observed'], r['up'], r['bigUp'], r['bigDown']) for r in evidence['groups']] == [
        (2, 2, 1, 1, 1), (2, 1, 1, 1, 0)]
    pending = summarize_strategy_outcomes([doc], '.', as_of='2026-11-16T14:59:59+08:00', **args)
    assert all(r['total'] == 2 and not r['observed'] for r in pending['groups'])
