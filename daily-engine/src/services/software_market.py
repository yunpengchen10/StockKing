"""Authenticated loopback client for the desktop's shared market providers.

No credentials are persisted in signals and no remote endpoint is accepted.
"""
from __future__ import annotations
import os
import re
from datetime import datetime
from time import monotonic
from urllib.parse import urlparse
import requests


class SoftwareMarketClient:
    def __init__(self, url='', token='', *, read_timeout=110):
        parsed = urlparse(url)
        if url and (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or
                    not parsed.port or parsed.username or parsed.password or parsed.path not in ('', '/')):
            raise ValueError('Software market gateway must be an authenticated IPv4 loopback endpoint')
        self.url, self.token = url.rstrip('/'), token
        self.read_timeout = max(.1, float(read_timeout))

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
                headers={'X-Stock-King-Market-Token': self.token}, timeout=(min(3, self.read_timeout), self.read_timeout), allow_redirects=False)
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
        from .public_market_quotes import TZ
        result = self._request('snapshot', {})
        frame = pd.DataFrame(result.get('items', []))
        checked = datetime.now(TZ)
        def fresh(row):
            try:
                stamp = datetime.fromisoformat(str(row.get('source_time') or ''))
                return bool(stamp.tzinfo and 0 <= (checked-stamp).total_seconds() <= 30)
            except (ValueError, TypeError):
                return False
        fresh_count = sum(fresh(row) for row in result.get('items', []))
        complete = bool(result.get('complete'))
        frame.attrs.update(source='software:go/eastmoney', fetched_at=result.get('fetched_at'),
                           stale=not complete or fresh_count != len(frame) or frame.empty,
                           complete=complete, directory_complete=complete,
                           selection_fresh_count=fresh_count,
                           freshness_meaning='provider_row_clock_checked_independently_of_directory_completeness',
                           source_time_meaning=result.get('source_time_meaning'))
        frame.attrs.update({key: result.get(key) for key in ('expected_count','unique_count','received_count','pages')})
        return frame

    def enrich_snapshot_quotes(self, snapshot, *, timeout=12, deadline=None, clock=None):
        """Attach same-observation prices/clocks for the full mainboard universe.

        This is a separate bounded stage: the existing universe snapshot never
        disappears because quote enrichment is slow. A quote's timestamp does
        not date the old turnover/volume-ratio fields. Missing batches retain
        their rows and denominator; receipt time is never used as source time.
        """
        from .public_market_quotes import TZ
        from .yao_scout.minute_history import bounded_fetch_map
        frame = snapshot.copy(deep=True)
        frame.attrs = dict(getattr(snapshot, 'attrs', {}) or {})
        now = clock or (lambda: datetime.now(TZ))
        if not {'code', 'name'}.issubset(frame.columns):
            frame.attrs['market_quote_enrichment'] = {'status':'unavailable', 'reason':'universe_fields_missing'}
            return frame
        records = frame.to_dict('records')
        codes = list(dict.fromkeys(str(row['code']).zfill(6) for row in records
            if re.fullmatch(r'(?:600|601|603|605|000|001|002|003)\d{3}', str(row['code']).zfill(6))
            and not re.search(r'ST|退|整理|停牌', str(row['name']), re.I)))
        code_set = set(codes)
        budget = max(0., min(float(timeout), deadline-monotonic())) if deadline is not None else max(0., float(timeout))
        end = monotonic()+budget
        groups = [codes[index:index+20] for index in range(0, len(codes), 20)]
        def fetch(group):
            remaining = end-monotonic()
            if remaining <= 0:
                return []
            # Per-worker clients avoid mutating another in-flight call's timeout.
            client = SoftwareMarketClient(self.url, self.token, read_timeout=min(3., remaining))
            return client.quotes(group, clock=now).get('quotes', [])
        batches, missing = bounded_fetch_map(fetch, groups, workers=8, timeout=budget)
        observations = {}
        received = now()
        for batch in batches:
            for assessed in batch:
                quote = assessed.get('quote') or {}
                code = str(assessed.get('code', ''))
                if code not in code_set or quote.get('code') != code or quote.get('source') not in {'tencent', 'sina'}:
                    continue
                if 'cross_source_price_conflict' in assessed.get('issues', []):
                    continue
                try:
                    stamp = datetime.fromisoformat(str(quote.get('source_time', '')))
                    if stamp.tzinfo is None or stamp > received or stamp.astimezone(TZ).date() != received.astimezone(TZ).date():
                        continue
                except (TypeError, ValueError):
                    continue
                observations[code] = {key:quote.get(key) for key in (
                    'code', 'price', 'open', 'previous_close', 'high', 'low', 'change_pct',
                    'amount_cny', 'volume_shares',
                    'limit_up', 'limit_down', 'source', 'source_time', 'fetched_at')}
        frame['market_quote'] = [observations.get(str(row['code']).zfill(6)) for row in records]
        frame.attrs['market_quote_source'] = 'software:go/' + '+'.join(sorted({
            observation['source'] for observation in observations.values()}))
        frame.attrs['market_quote_received_at'] = received.isoformat()
        frame.attrs['market_quote_enrichment'] = {
            'status':'complete' if len(observations) == len(codes) and codes else 'partial',
            'requested':len(codes), 'observed':len(observations),
            'missing_batches':len(missing), 'source_time_meaning':'provider_quote_update_time',
            'price_fields_scope':'market_quote only; original selection fields retain original dates'}
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
