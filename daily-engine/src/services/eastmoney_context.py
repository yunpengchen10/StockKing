"""Bounded, named-field Eastmoney tables used by the archived context provider."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
import json
from time import monotonic

import pandas as pd
import requests

from src.patches.eastmoney_patch import original_request


HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://data.eastmoney.com/'}
TABLES = {
    'stock_yjbb_em': {
        'report': 'RPT_LICO_FN_CPD', 'date_field': 'REPORTDATE',
        'universe_filter': '(SECURITY_TYPE_CODE in ("058001001","058001008"))',
        'url': 'https://datacenter-web.eastmoney.com/api/data/v1/get',
        'sort': 'SECURITY_CODE,UPDATE_DATE', 'sort_types': '1,-1',
        'fields': {'SECURITY_CODE': '股票代码', 'NOTICE_DATE': '最新公告日期',
                   'BASIC_EPS': '每股收益', 'PARENT_NETPROFIT': '净利润-净利润',
                   'SJLTZ': '净利润-同比增长', 'YSTZ': '营业总收入-同比增长',
                   'WEIGHTAVG_ROE': '净资产收益率', 'MGJYXJJE': '每股经营现金流量',
                   'PUBLISHNAME': '所处行业'},
    },
    'stock_yjyg_em': {
        'report': 'RPT_PUBLIC_OP_NEWPREDICT', 'date_field': 'REPORT_DATE',
        'url': 'https://datacenter.eastmoney.com/securities/api/data/v1/get',
        'sort': 'NOTICE_DATE,SECURITY_CODE', 'sort_types': '-1,1',
        'fields': {'SECURITY_CODE': '股票代码', 'NOTICE_DATE': '公告日期',
                   'PREDICT_FINANCE': '预测指标', 'PREDICT_TYPE': '预告类型',
                   'INCREASE_JZ': '业绩变动幅度'},
    },
    'stock_restricted_release_detail_em': {
        'report': 'RPT_LIFT_STAGE', 'url': 'https://datacenter-web.eastmoney.com/api/data/v1/get',
        'sort': 'FREE_DATE,SECURITY_CODE', 'sort_types': '1,1',
        'fields': {'SECURITY_CODE': '股票代码', 'FREE_DATE': '解禁时间',
                   'ABLE_FREE_SHARES': '解禁数量', 'FREE_RATIO': '占解禁前流通市值比例'},
    },
}


def _day(value):
    return datetime.strptime(str(value), '%Y%m%d').date().isoformat()


def _get(url, **kwargs):
    with requests.Session() as session:
        session.trust_env = False
        return original_request(session, 'GET', url, **kwargs)


def fetch_eastmoney_context(endpoint, params, timeout, *, getter=None):
    """Fetch every page once within one deadline; partial tables are not usable.

    The caller archives the completed table with its actual observation time.
    These are latest vendor tables, never evidence of earlier availability.
    """
    table = TABLES[endpoint]
    deadline = monotonic() + max(0, float(timeout))
    date_field = table.get('date_field')
    if date_field:
        requested_date = _day(params['date'])
        condition = f"({date_field}='{requested_date}')"
    else:
        start, end = _day(params['start_date']), _day(params['end_date'])
        condition = f"(FREE_DATE>='{start}')(FREE_DATE<='{end}')"
    condition += table.get('universe_filter', '')
    columns = list(table['fields']) + ([date_field] if date_field else [])
    query = {'reportName': table['report'], 'columns': ','.join(columns), 'filter': condition,
             'sortColumns': table['sort'], 'sortTypes': table['sort_types'],
             'pageSize': '500', 'source': 'WEB', 'client': 'WEB'}

    def page(number):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError('context_table_deadline_exceeded')
        response = (getter or _get)(table['url'], params={**query, 'pageNumber': str(number)},
                                   headers=HEADERS, timeout=min(6, remaining))
        response.raise_for_status()
        response.encoding = 'utf-8'
        payload = response.json()
        result = payload.get('result')
        if payload.get('success') is not True or payload.get('code') != 0 or not isinstance(result, dict):
            raise ValueError('provider_result_unavailable:code=' + str(payload.get('code')))
        count, pages, rows = result.get('count'), result.get('pages'), result.get('data')
        if (isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 40000
                or isinstance(pages, bool) or not isinstance(pages, int) or not 0 <= pages <= 80
                or not isinstance(rows, list) or (count == 0) != (pages == 0)):
            raise ValueError('provider_pagination_schema_changed')
        if count == 0 and rows or count > 0 and (not rows or len(rows) > 500):
            raise ValueError('provider_page_incomplete')
        for row in rows:
            if not isinstance(row, dict) or not set(columns).issubset(row):
                raise ValueError('provider_field_schema_changed')
            if date_field and str(row[date_field])[:10] != requested_date:
                raise ValueError('provider_report_period_mismatch')
            if not date_field and not start <= str(row['FREE_DATE'])[:10] <= end:
                raise ValueError('provider_unlock_period_mismatch')
        return {'count': count, 'pages': pages, 'rows': rows, 'version': payload.get('version')}

    first = page(1)
    parts = {1: first}
    if first['pages'] > 1:
        pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='context-pages')
        futures = {pool.submit(page, number): number for number in range(2, first['pages'] + 1)}
        try:
            done, pending = wait(futures, timeout=max(0, deadline - monotonic()))
            if pending:
                raise TimeoutError('context_table_deadline_exceeded')
            parts.update({futures[future]: future.result() for future in done})
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    rows, fingerprints = [], set()
    for _, part in sorted(parts.items()):
        # Eastmoney's version is a response/page fingerprint, not a global
        # table revision: different pages legitimately have different values.
        if any(part[key] != first[key] for key in ('count', 'pages')):
            raise ValueError('provider_table_changed_during_pagination')
        fingerprint = json.dumps(part['rows'], sort_keys=True, ensure_ascii=False)
        if fingerprint in fingerprints:
            raise ValueError('provider_repeated_page')
        fingerprints.add(fingerprint)
        rows.extend(part['rows'])
    if len(rows) != first['count']:
        raise ValueError('provider_record_count_mismatch')
    frame = pd.DataFrame(rows, columns=columns).rename(columns=table['fields'])
    frame = frame[list(table['fields'].values())]
    if endpoint == 'stock_restricted_release_detail_em':
        frame['解禁数量'] = pd.to_numeric(frame['解禁数量'], errors='coerce') * 10000
    frame.attrs.update(source='Eastmoney datacenter/' + table['report'], pages=first['pages'],
                       recordCount=first['count'], complete=True,
                       sourceUrl=table['url'], universeFilter=table.get('universe_filter'),
                       pageVersions={str(number): part['version'] for number, part in sorted(parts.items())},
                       collectionMethod='bounded_named_fields_parallel_pages')
    return frame
