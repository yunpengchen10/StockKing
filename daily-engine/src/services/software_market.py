"""Authenticated loopback client for the desktop's shared market providers.

No credentials are persisted in signals and no remote endpoint is accepted.
"""
from __future__ import annotations
import os
from datetime import datetime
from urllib.parse import urlparse
import requests


class SoftwareMarketClient:
    def __init__(self, url='', token=''):
        parsed = urlparse(url)
        if url and (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or
                    not parsed.port or parsed.username or parsed.password or parsed.path not in ('', '/')):
            raise ValueError('Software market gateway must be an authenticated IPv4 loopback endpoint')
        self.url, self.token = url.rstrip('/'), token

    @classmethod
    def from_environment(cls):
        return cls(os.getenv('STOCK_KING_MARKET_URL', ''), os.getenv('STOCK_KING_MARKET_TOKEN', ''))

    @property
    def available(self):
        return bool(self.url and self.token)

    def _request(self, path, payload):
        if not self.available:
            raise RuntimeError('Software market gateway is not running')
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(self.url + '/v1/' + path, json=payload,
                headers={'X-Stock-King-Market-Token': self.token}, timeout=(3, 110), allow_redirects=False)
            if response.status_code != 200:
                raise RuntimeError(f'Software market gateway returned {response.status_code}')
            return response.json()

    def bars(self, code, period='1', count=5000, end=None):
        if end and ('T' in str(end) or '-' in str(end)):
            from zoneinfo import ZoneInfo
            cutoff = datetime.fromisoformat(str(end))
            if cutoff.tzinfo is None:
                raise ValueError('Historical bar cutoff requires timezone')
            end = cutoff.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d%H%M%S')
        result = self._request('bars', {'codes': [code], 'period': str(period), 'count': count, 'end': end or ''})
        return result.get('results', {}).get(code, {'bars': []})

    def snapshot(self):
        import pandas as pd
        result = self._request('snapshot', {})
        frame = pd.DataFrame(result.get('items', []))
        frame.attrs.update(source='software:go/eastmoney', fetched_at=result.get('fetched_at'),
                           stale=not result.get('complete', False), complete=bool(result.get('complete')),
                           source_time_meaning=result.get('source_time_meaning'))
        frame.attrs.update({key: result.get(key) for key in ('expected_count','unique_count','received_count','pages')})
        return frame

    def sectors(self):
        return self._request('sectors', {})

    def quotes(self, codes, *, clock=None):
        from src.services.public_market_quotes import normalize_quote_codes, parse_quote_payload, _assess, TZ
        codes = normalize_quote_codes(codes)
        fetched = self._request('quotes', {'codes': codes})
        now = clock() if clock else datetime.now(TZ)
        results = []
        for source, packet in fetched.get('sources', {}).items():
            parsed = parse_quote_payload(packet.get('payload', ''), source, codes)
            observations = [{**quote, 'source': source, 'transport': 'https',
                'source_url': 'https://qt.gtimg.cn/' if source == 'tencent' else 'https://hq.sinajs.cn/',
                'fetched_at': packet['fetched_at'], 'gateway': 'software:go'}
                for quote in parsed['quotes'].values() if datetime.fromisoformat(quote['source_time']) <= now]
            results.append({'observations': observations, 'errors': parsed['errors'], 'attempts': []})
        return {'schema_version': 1, 'checked_at': now.isoformat(), 'max_age_seconds': 30,
                'source_time_meaning': 'provider_quote_update_time', 'gateway': 'software:go',
                'exchange_execution_time_verified': False, 'historical_snapshot_supported': False,
                'auction_fields_supported': False, 'quotes': [_assess(code, results, now) for code in codes],
                'attempts': [{'source': s, 'gateway': 'software:go'} for s in fetched.get('sources', {})]}
