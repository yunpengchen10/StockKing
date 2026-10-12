import pytest
import requests

from src.services.eastmoney_context import TABLES, fetch_eastmoney_context
from src.services.yao_scout.sector_fund_evidence import HEADERS, fetch_fund_evidence


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def record(code='000001'):
    row = {key: 1 for key in TABLES['stock_yjbb_em']['fields']}
    return {**row, 'SECURITY_CODE': code, 'NOTICE_DATE': '2026-08-15 00:00:00',
            'REPORTDATE': '2026-06-30 00:00:00', 'PUBLISHNAME': '银行'}


def envelope(rows, *, count=None, pages=1, version='same'):
    return {'success': True, 'code': 0, 'version': version,
            'result': {'count': len(rows) if count is None else count, 'pages': pages, 'data': rows}}


def test_named_fields_and_all_pages_are_validated_without_fetching_first_page_twice():
    seen = []
    def get(url, **kwargs):
        page = int(kwargs['params']['pageNumber'])
        seen.append(page)
        assert kwargs['params']['columns'] != 'ALL' and kwargs['headers']['Referer']
        assert '(SECURITY_TYPE_CODE in ("058001001","058001008"))' in kwargs['params']['filter']
        return Response(envelope([record('00000' + str(page))], count=3, pages=3, version='page-' + str(page)))
    frame = fetch_eastmoney_context('stock_yjbb_em', {'date': '20260630'}, 10, getter=get)
    assert sorted(seen) == [1, 2, 3]
    assert list(frame['股票代码']) == ['000001', '000002', '000003']
    assert list(frame['最新公告日期']) == ['2026-08-15 00:00:00'] * 3
    assert frame.attrs['complete'] and frame.attrs['recordCount'] == 3
    assert frame.attrs['pageVersions'] == {'1': 'page-1', '2': 'page-2', '3': 'page-3'}
    assert 'Eastmoney datacenter/RPT_LICO_FN_CPD' == frame.attrs['source']


@pytest.mark.parametrize('failure', ['page_count', 'duplicate_page', 'missing_fields', 'wrong_period'])
def test_partial_changed_or_wrong_tables_are_never_reported_complete(failure):
    def get(url, **kwargs):
        page = int(kwargs['params']['pageNumber'])
        data = record('00000' + str(page))
        payload = envelope([data], count=2, pages=2)
        if page == 2:
            if failure == 'page_count': payload['result']['count'] = 3
            elif failure == 'duplicate_page': payload['result']['data'] = [record()]
            elif failure == 'missing_fields': data.pop('NOTICE_DATE')
            elif failure == 'wrong_period': data['REPORTDATE'] = '2026-03-31'
        return Response(payload)
    with pytest.raises(ValueError):
        fetch_eastmoney_context('stock_yjbb_em', {'date': '20260630'}, 10, getter=get)


def test_confirmed_zero_count_is_empty_available_but_null_result_is_unknown():
    frame = fetch_eastmoney_context('stock_yjbb_em', {'date': '20260930'}, 10,
        getter=lambda *a, **k: Response(envelope([], count=0, pages=0)))
    assert frame.empty and '股票代码' in frame.columns and frame.attrs['complete']
    with pytest.raises(ValueError, match='provider_result_unavailable:code=9201'):
        fetch_eastmoney_context('stock_yjbb_em', {'date': '20260930'}, 10,
            getter=lambda *a, **k: Response({'success': False, 'code': 9201, 'result': None}))


def test_unlock_units_remain_shares_and_unrequested_future_performance_fields_are_absent():
    row = {'SECURITY_CODE': '000001', 'FREE_DATE': '2026-10-10', 'ABLE_FREE_SHARES': 12.5,
           'FREE_RATIO': .02, 'A20_ADJCHRATE': 50}
    frame = fetch_eastmoney_context('stock_restricted_release_detail_em',
        {'start_date': '20261009', 'end_date': '20261023'}, 10,
        getter=lambda *a, **k: Response(envelope([row])))
    assert frame.iloc[0]['解禁数量'] == 125000
    assert frame.iloc[0]['占解禁前流通市值比例'] == .02
    assert 'A20_ADJCHRATE' not in frame.columns


def test_zero_budget_does_not_start_network_requests():
    with pytest.raises(TimeoutError):
        fetch_eastmoney_context('stock_yjbb_em', {'date': '20260630'}, 0,
            getter=lambda *a, **k: pytest.fail('network should not start'))


def test_fund_headers_are_sent_and_safe_provider_failure_detail_is_retained():
    seen = []
    def failed(url, **kwargs):
        seen.append(kwargs)
        raise requests.ConnectionError('private-proxy-password')
    result = fetch_fund_evidence('600000', '2026-10-09T10:30:00+08:00', getter=failed)
    assert len(seen) == 3 and all(item['headers'] == HEADERS for item in seen)
    assert result['reason'] == 'Eastmoney sources unavailable: ConnectionError,ConnectionError,ConnectionError'
    assert result['status'] == 'unavailable' and not result['metrics']
    assert 'private-proxy-password' not in str(result)
