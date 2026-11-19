"""Independent T+1 reference-price statistics for actually published channel hits."""
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from .daily_observation_outcomes import read_verified_close
from .intraday_evidence import SHANGHAI, _datetime
from .signal_learning import _default_calendar


def summarize_strategy_outcomes(documents, data_dir, *, as_of=None, calendar=_default_calendar,
                                close_reader=read_verified_close):
    now = _datetime(as_of) if as_of is not None else datetime.now(SHANGHAI)
    dates, prices, groups = {}, {}, {}
    for doc in documents:
        if doc.get('status') != 'published' or not doc.get('ruleSnapshots'):
            continue
        day = _datetime(doc['asOf']).date()
        for channel in doc.get('channels', []):
            key = (channel['id'], channel['version'])
            groups.setdefault(key, {'id': key[0], 'name': channel['name'], 'version': key[1],
                                    'total': 0, 'observed': 0, 'up': 0, 'bigUp': 0, 'bigDown': 0})
        for row in doc.get('items', []):
            hits = {(match['id'], match['version']) for match in row.get('strategyMatches', [])} & groups.keys()
            if not hits:
                continue
            for key in hits:
                groups[key]['total'] += 1
            if day >= now.date():
                continue
            if day not in dates:
                dates[day] = next((d for d in calendar(day, day + timedelta(days=30)) if d > day), None)
            target = dates[day]
            if target is None or datetime.combine(target, time(15), SHANGHAI) > now:
                continue
            price_key = (row['code'], target.isoformat())
            if price_key not in prices:
                prices[price_key] = close_reader(data_dir, *price_key, now)
            evidence = prices[price_key]
            if evidence.get('status') != 'verified' or evidence.get('price_basis') != 'unadjusted':
                continue
            try:
                close, baseline = Decimal(str(evidence['close'])), Decimal(str(row['price']))
                if not close.is_finite() or not baseline.is_finite() or min(close, baseline) <= 0:
                    continue
            except (InvalidOperation, KeyError):
                continue
            for key in hits:
                group = groups[key]
                group['observed'] += 1
                group['up'] += int(close > baseline)
                group['bigUp'] += int(close >= baseline * Decimal('1.05'))
                group['bigDown'] += int(close <= baseline * Decimal('0.95'))
    return {'asOf': now.isoformat(), 'groups': list(groups.values())}
