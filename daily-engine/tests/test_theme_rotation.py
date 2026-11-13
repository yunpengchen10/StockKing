from copy import deepcopy
from datetime import datetime, timedelta
import json
from time import monotonic, sleep
from zoneinfo import ZoneInfo

import pytest

from src.services.yao_scout import theme_rotation as tr

NOW = datetime(2026, 11, 13, 10, 30, tzinfo=ZoneInfo('Asia/Shanghai'))


def packet():
    rows, members = [], {}
    for index, change in enumerate([6., 5., 4., 3., 2., -1.]):
        tid = f'BK{1000 + index}'
        rows.append({'id': tid, 'name': f'真实概念{index}', 'changePct': change,
                     'amount': (7 - index) * 1e8, 'upCount': 8 - index, 'downCount': index + 2,
                     'flatCount': 0, 'sourceTime': NOW.isoformat()})
        members[tid] = {'source': tr.SOURCE, 'knownAt': NOW.isoformat(), 'complete': True,
                        'expectedCount': 10, 'rows': [{'code': f'{600000 + index * 10 + number}',
                            'sourceTime': NOW.isoformat(), 'changePct': change + 2 - number}
                            for number in range(10)]}
    return {'version': tr.VERSION, 'kind': 'concept', 'source': tr.SOURCE,
            'status': 'observed', 'checkedAt': NOW.isoformat(), 'rows': rows, 'members': members,
            'coverage': {'expectedCount': len(rows), 'receivedCount': len(rows), 'complete': True}}


def test_market_rank_then_select_three_complete_mainlines():
    board = tr.build_theme_board(packet(), NOW)
    assert board['status'] == 'observed'
    assert board['sourceTime'] == NOW.isoformat()
    assert board['selection']['selectedIds'] == ['BK1000', 'BK1001', 'BK1002']
    assert board['themes'][0]['heatScore'] == pytest.approx(11 / 12, abs=1e-6)
    assert sum(row['amountSharePct'] for row in board['themes']) == pytest.approx(100, abs=.001)
    assert '不是全市场' in board['amountShareMeaning']
    assert '不是指数' in board['relativeStrengthMeaning']


@pytest.mark.parametrize('change', ['missing_time', 'old', 'future', 'industry', 'incomplete', 'duplicate', 'late_known'])
def test_bad_time_catalogue_or_industry_never_supplies_theme(change):
    data = packet()
    if change == 'missing_time':
        for row in data['rows']:
            row['sourceTime'] = None
    elif change in {'old', 'future'}:
        for row in data['rows']:
            row['sourceTime'] = (NOW + timedelta(seconds=-301 if change == 'old' else 1)).isoformat()
    elif change == 'industry':
        data['kind'] = 'industry'
    elif change == 'incomplete':
        data['coverage']['complete'] = False
    elif change == 'duplicate':
        data['rows'][1]['id'] = data['rows'][0]['id']
    else:
        data['checkedAt'] = (NOW + timedelta(seconds=1)).isoformat()
    board = tr.build_theme_board(data, NOW)
    assert board['status'] == 'unavailable'
    assert board['selection']['selectedIds'] == []
    assert tr.theme_for_code(board, '600000')['selected'] is False


def test_member_coverage_uses_whole_declared_membership():
    data = packet()
    for row in data['members']['BK1000']['rows'][:3]:
        row['sourceTime'] = None
    board = tr.build_theme_board(data, NOW)
    first = board['themes'][0]
    assert first['coverage']['pct'] == 70
    assert not first['selected']
    data['members']['BK1000']['rows'][2]['sourceTime'] = NOW.isoformat()
    first = tr.build_theme_board(data, NOW)['themes'][0]
    assert first['coverage']['pct'] == 80 and first['selected']
    assert not tr.theme_for_code(tr.build_theme_board(data, NOW), '600000')['selected']


@pytest.mark.parametrize('change', ['missing', 'truncated', 'duplicate', 'future', 'wrong_source'])
def test_member_mapping_never_inferred_or_silently_repaired(change):
    data = packet()
    member = data['members']['BK1000']
    if change == 'missing':
        del data['members']['BK1000']
    elif change == 'truncated':
        member['rows'].pop()
    elif change == 'duplicate':
        member['rows'][1]['code'] = member['rows'][0]['code']
    elif change == 'future':
        member['knownAt'] = (NOW + timedelta(seconds=1)).isoformat()
    else:
        member['source'] = 'industry'
    board = tr.build_theme_board(data, NOW)
    assert not board['themes'][0]['selected']
    assert not tr.theme_for_code(board, '600000')['selected']


def test_identical_concepts_do_not_occupy_multiple_slots_and_overlap_has_one_owner():
    data = packet()
    data['members']['BK1001'] = deepcopy(data['members']['BK1000'])
    board = tr.build_theme_board(data, NOW)
    assert board['selection']['selectedIds'] == ['BK1000', 'BK1002']
    evidence = tr.theme_for_code(board, '600000')
    assert evidence['id'] == 'BK1000' and evidence['leaderRank'] == 1
    assert evidence['memberChangePct'] == 8 and evidence['relativeStrengthPct'] == 2
    assert evidence['memberCount'] == 10
    assert 'memberQuotes' not in evidence


def test_feedback_is_bounded_past_only_and_cannot_override_current_weakness():
    data = packet()
    baseline = tr.build_theme_board(data, NOW)
    adjusted = tr.build_theme_board(data, NOW, feedback={'asOf': NOW.isoformat(), 'adjustments': {'BK1000': 99, 'BK1005': -99}})
    by_id = {row['id']: row for row in adjusted['themes']}
    assert by_id['BK1000']['feedbackPenalty'] == .1
    assert by_id['BK1000']['heatScore'] == pytest.approx(baseline['themes'][0]['heatScore'] - .1, abs=1e-6)
    assert by_id['BK1005']['feedbackPenalty'] == 0 and not by_id['BK1005']['selected']
    for bad_time in [None, (NOW + timedelta(seconds=1)).isoformat()]:
        board = tr.build_theme_board(data, NOW, feedback={'asOf': bad_time, 'adjustments': {'BK1000': .1}})
        assert board['themes'][0]['feedbackPenalty'] == 0


def test_continuity_only_uses_prior_frozen_days_not_future_or_same_day_repeats():
    data = packet()
    past = tr.build_theme_board(data, NOW)
    past['asOf'] = (NOW - timedelta(days=1)).isoformat()
    past['sourceTime'] = past['asOf']
    for row in past['themes']:
        row['sourceTime'] = past['asOf']
    board = tr.build_theme_board(data, NOW, history=[past, past, tr.build_theme_board(data, NOW)])
    assert board['themes'][0]['stage'] == '持续'
    assert board['themes'][0]['priorSelectedDays'] == 1
    future = deepcopy(past)
    future['asOf'] = (NOW + timedelta(days=1)).isoformat()
    assert tr.build_theme_board(data, NOW, history=[future])['themes'][0]['stage'] == '启动'
    inconsistent = deepcopy(past)
    inconsistent['themes'][0]['sourceTime'] = NOW.isoformat()
    assert tr.build_theme_board(data, NOW, history=[inconsistent])['themes'][0]['stage'] == '启动'
    future_known = deepcopy(past)
    future_known['knownAt'] = (NOW + timedelta(seconds=1)).isoformat()
    assert tr.build_theme_board(data, NOW, history=[future_known])['themes'][0]['stage'] == '启动'
    data['rows'][0]['changePct'] = -3
    by_id = {item['id']: item for item in tr.build_theme_board(data, NOW, history=[past])['themes']}
    assert by_id['BK1000']['stage'] == '退潮' and not by_id['BK1000']['selected']


def test_queue_mainline_then_controls_with_stable_order_and_no_industry_fallback():
    board = tr.build_theme_board(packet(), NOW)
    rows = [{'code': '600099', 'industry': '真实概念0'}, {'code': '600010'},
            {'code': '600000'}, {'code': '600000'}, {'code': '600098'}]
    queue = tr.prioritize_theme_queue(rows, board, 4)
    assert [row['code'] for row in queue] == ['600000', '600010', '600099', '600098']
    assert [row['themeRole'] for row in queue] == ['mainline', 'mainline', 'control', 'control']
    assert rows[0] == {'code': '600099', 'industry': '真实概念0'}
    assert tr.prioritize_theme_queue(rows, board, 0) == []


def test_queue_promotes_leaders_and_preserves_control_capacity():
    board = tr.build_theme_board(packet(), NOW)
    rows = [{'code': str(600000 + index)} for index in reversed(range(10))]
    rows += [{'code': '600099'}, {'code': '600098'}]
    queue = tr.prioritize_theme_queue(rows, board, 5)
    assert [row['code'] for row in queue] == ['600000', '600001', '600002', '600003', '600099']
    assert queue[-1]['themeRole'] == 'control'


def test_missing_members_stays_unverified_not_a_market_rejection():
    data = packet()
    del data['members']['BK1000']
    board = tr.build_theme_board(data, NOW)
    assert board['themes'][0]['marketEligible']
    assert 'BK1000' in board['membershipAcquisition']['unverifiedCandidateIds']
    assert any('不代表已排除' in gap for gap in board['gaps'])


class Response:
    def __init__(self, total, rows):
        self.payload = {'rc': 0, 'data': {'total': total, 'diff': rows}}

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def clock(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(NOW.isoformat())
    monkeypatch.setattr(tr, 'datetime', FrozenDatetime)


def provider_rows():
    data = packet()
    boards = [{'f12': row['id'], 'f14': row['name'], 'f3': row['changePct'], 'f6': row['amount'],
               'f104': row['upCount'], 'f105': row['downCount'], 'f106': row['flatCount'],
               'f124': NOW.timestamp()} for row in data['rows']]
    members = {tid: [{'f12': row['code'], 'f3': row['changePct'], 'f124': NOW.timestamp()}
                     for row in value['rows']] for tid, value in data['members'].items()}
    return boards, members


def test_http_requests_are_concept_only_bounded_and_keep_provider_times(monkeypatch, tmp_path):
    clock(monkeypatch)
    boards, members = provider_rows()
    calls = []
    def getter(url, **kwargs):
        params = kwargs['params']
        calls.append(params['fs'])
        assert 0 < kwargs['timeout'] <= 3
        assert '+' not in params['fs']
        rows = boards if params['fs'] == tr.CATALOGUE_FILTER else members[params['fs'][2:]]
        return Response(len(rows), rows)
    result = tr.fetch_theme_market(NOW, monotonic() + 5, tmp_path, getter=getter)
    board = tr.build_theme_board(result, NOW)
    assert board['selection']['selectedIds'] == ['BK1000', 'BK1001', 'BK1002']
    assert len(calls) == 4
    assert result['rows'][0]['sourceTime'] == NOW.isoformat()
    assert result['members']['BK1000']['expectedCount'] == 10
    assert result['catalogueFilter'] == 'm:90 t:3 f:!50'


def test_expired_deadline_never_calls_network(tmp_path):
    def getter(*args, **kwargs):
        pytest.fail('exhausted deadline made a request')
    result = tr.fetch_theme_market(NOW, monotonic() - 1, tmp_path, getter=getter)
    assert result['status'] == 'unavailable'


def test_shared_deadline_does_not_wait_for_slow_socket(tmp_path):
    def getter(*args, **kwargs):
        sleep(.25)
        return Response(1, [])
    started = monotonic()
    result = tr.fetch_theme_market(NOW, started + .03, tmp_path, getter=getter)
    assert monotonic() - started < .2
    assert result['status'] == 'unavailable'


def test_partial_catalogue_cannot_be_presented_as_market_ranking(monkeypatch, tmp_path):
    clock(monkeypatch)
    boards, _ = provider_rows()
    def getter(*args, **kwargs):
        return Response(100, boards)
    result = tr.fetch_theme_market(NOW, monotonic() + 1, tmp_path, getter=getter)
    assert not result['coverage']['complete']
    assert tr.build_theme_board(result, NOW)['selection']['selectedIds'] == []


def test_listing_labels_are_not_narrative_mainlines():
    data = packet()
    data['rows'][0]['name'] = '融资融券'
    board = tr.build_theme_board(data, NOW)
    assert not board['themes'][0]['selected']


class SinaResponse:
    def __init__(self, value, text=None):
        self.value = value
        self.text = text if text is not None else json.dumps(value)

    def raise_for_status(self):
        pass

    def json(self):
        return self.value


def sina_fixture():
    data = packet()
    catalogue, mapping, quotes = {}, {}, []
    for index, item in enumerate(data['rows']):
        tid = f'gn_theme{index}'
        catalogue[tid] = f'{tid},{item["name"]},100,0,0,0'
        mapping[tid] = [row['code'] for row in data['members'][item['id']]['rows']]
        for number, code in enumerate(mapping[tid]):
            quotes.append({'code': code, 'name': '成员', 'source': 'old-snapshot', 'source_time': None,
                'market_quote': {'code': code, 'name': '成员', 'source': 'tencent',
                    'source_time': NOW.isoformat(), 'price': 10. + index, 'amount_cny': item['amount'] / 10,
                    'change_pct': item['changePct'] if number < 8 else -1.}})
    calls = []
    def getter(url, **kwargs):
        params = kwargs['params']
        calls.append((url, params))
        if 'eastmoney' in url:
            raise ConnectionError('primary service unavailable')
        if 'newFLJK' in url:
            return SinaResponse(catalogue, 'var S_Finance_bankuai_class = ' + json.dumps(catalogue))
        codes = mapping[params['node']]
        if 'getHQNodeStockCount' in url:
            return SinaResponse(str(len(codes)))
        pn = int(params['page'])
        return SinaResponse([{'code': code, 'symbol': 'sh' + code} for code in codes[(pn-1)*100:pn*100]])
    return getter, quotes, calls, mapping


def test_sina_fallback_ranks_real_member_quotes_and_preserves_prices(monkeypatch, tmp_path):
    clock(monkeypatch)
    getter, quotes, calls, _ = sina_fixture()
    raw = tr.fetch_theme_market(NOW, monotonic() + 5, tmp_path, quote_rows=quotes, getter=getter)
    board = tr.build_theme_board(raw, NOW)
    assert raw['source'] == tr.FALLBACK_SOURCE
    assert raw['membershipWarmup']['completeCount'] == 6
    assert board['status'] == 'observed'
    assert board['selection']['selectedIds'] == ['gn_theme0', 'gn_theme1', 'gn_theme2']
    assert board['themes'][0]['coverage']['expectedCount'] == 10  # not catalogue's capped count of 100
    assert board['themes'][0]['memberQuotes']['600000']['price'] == 10.
    assert board['themes'][0]['memberQuotes']['600000']['source'] == 'tencent'
    assert '等权' in board['returnMeaning']
    assert any('getHQNodeStockCount' in call[0] for call in calls)
    assert (tmp_path / 'sina-theme-members-v1.json').is_file()


def test_warm_sina_cache_uses_no_network_and_never_retimes_quote(monkeypatch, tmp_path):
    clock(monkeypatch)
    getter, quotes, _, _ = sina_fixture()
    tr._fetch_sina_theme_market(NOW, monotonic() + 5, tmp_path, quotes, getter=getter)
    def unexpected(*args, **kwargs):
        pytest.fail('complete current member cache should not use network')
    for quote in quotes:
        quote['market_quote']['source_time'] = (NOW - timedelta(minutes=6)).isoformat()
        quote['source_time'] = NOW.isoformat()  # unrelated outer row cannot renew stale field
    raw = tr._fetch_sina_theme_market(NOW, monotonic() + 5, tmp_path, quotes, getter=unexpected)
    assert raw['membershipWarmup']['complete']
    assert tr.build_theme_board(raw, NOW)['status'] == 'unavailable'


def test_sina_incomplete_members_stay_in_whole_catalogue_coverage_denominator(monkeypatch, tmp_path):
    clock(monkeypatch)
    getter, quotes, _, _ = sina_fixture()
    def incomplete(url, **kwargs):
        if kwargs['params'].get('node') in {'gn_theme4', 'gn_theme5'}:
            raise TimeoutError('one cold concept')
        return getter(url, **kwargs)
    raw = tr._fetch_sina_theme_market(NOW, monotonic() + 5, tmp_path, quotes, getter=incomplete)
    assert raw['membershipWarmup']['completeCount'] == 4
    assert raw['coverage']['complete']  # complete six-theme catalogue, only four usable mappings
    assert len(raw['rows']) == 6
    assert tr.build_theme_board(raw, NOW)['selection']['selectedIds'] == []
    completed = tr._fetch_sina_theme_market(NOW, monotonic() + 5, tmp_path, quotes, getter=getter)
    assert completed['membershipWarmup']['complete']


def test_sina_full_denominator_and_member_coverage_gate(monkeypatch, tmp_path):
    clock(monkeypatch)
    getter, quotes, _, _ = sina_fixture()
    for row in quotes[:3]:
        row['market_quote']['source_time'] = None
    raw = tr._fetch_sina_theme_market(NOW, monotonic() + 5, tmp_path, quotes, getter=getter)
    board = tr.build_theme_board(raw, NOW)
    assert board['status'] == 'observed'  # 5/6 valid concepts remains above 80%
    assert 'gn_theme0' not in board['selection']['selectedIds']
    for row in quotes[10:13]:
        row['market_quote']['source_time'] = None
    raw = tr._fetch_sina_theme_market(NOW, monotonic() + 5, tmp_path, quotes, getter=getter)
    assert tr.build_theme_board(raw, NOW)['status'] == 'unavailable'  # 4/6 insufficient


def test_sina_future_cache_is_refetched_not_reused(monkeypatch, tmp_path):
    clock(monkeypatch)
    getter, quotes, calls, _ = sina_fixture()
    tr._fetch_sina_theme_market(NOW, monotonic() + 5, tmp_path, quotes, getter=getter)
    path = tmp_path / 'sina-theme-members-v1.json'
    cache = json.loads(path.read_text(encoding='utf-8'))
    cache['members']['gn_theme0']['knownAt'] = (NOW + timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(cache), encoding='utf-8')
    calls.clear()
    tr._fetch_sina_theme_market(NOW, monotonic() + 5, tmp_path, quotes, getter=getter)
    assert any(params.get('node') == 'gn_theme0' for _, params in calls)


def test_sina_authoritative_count_over_100_requires_all_pages(monkeypatch, tmp_path):
    clock(monkeypatch)
    getter, quotes, calls, mapping = sina_fixture()
    mapping['gn_theme0'] = [str(600000 + number) for number in range(103)]
    raw = tr._fetch_sina_theme_market(NOW, monotonic() + 5, tmp_path, quotes, getter=getter)
    assert raw['members']['gn_theme0']['expectedCount'] == 103
    assert len(raw['members']['gn_theme0']['rows']) == 103
    assert any(params.get('node') == 'gn_theme0' and params.get('page') == '2' for _, params in calls)


@pytest.mark.parametrize('placement,flag', [
    ('direct', 'stale'), ('direct', 'is_stale'), ('direct', 'conflict'),
    ('nested', 'stale'), ('nested', 'is_stale'), ('nested', 'conflict'),
    ('outer', 'conflict'), ('direct_metadata', 'issues'), ('nested_metadata', 'issues'),
    ('outer_metadata', 'issues'), ('nested_metadata', 'cross_source_check'),
    ('direct', 'quote_usable_for_current_price_check'), ('nested', 'status'),
])
def test_sina_explicit_invalid_quote_is_rejected_even_with_fresh_source_time(monkeypatch, tmp_path, placement, flag):
    clock(monkeypatch)
    getter, quotes, _, _ = sina_fixture()
    for index in range(3):
        if placement.startswith('direct'):
            quotes[index] = dict(quotes[index]['market_quote'])
            target = quotes[index]
        elif placement.startswith('outer'):
            target = quotes[index]
        else:
            target = quotes[index]['market_quote']
        if placement.endswith('metadata'):
            target['public_quote_metadata'] = {}
            target = target['public_quote_metadata']
        target[flag] = (['cross_source_price_conflict'] if flag == 'issues' else
                        {'prices_agree': False} if flag == 'cross_source_check' else
                        False if flag == 'quote_usable_for_current_price_check' else
                        'invalid' if flag == 'status' else True)
    raw = tr._fetch_sina_theme_market(NOW, monotonic() + 5, tmp_path, quotes, getter=getter)
    board = tr.build_theme_board(raw, NOW)
    assert board['status'] == 'observed'  # remaining five concepts retain the full denominator
    assert 'gn_theme0' not in board['selection']['selectedIds']
    assert all(row['sourceTime'] is None for row in raw['members']['gn_theme0']['rows'][:3])
