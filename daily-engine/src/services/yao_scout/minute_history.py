"""Dated one-minute bars, a persistent archive and exact same-clock baselines.

All timestamps are bar END times; amounts are CNY and volumes are shares.
Never splice cumulative Tencent snapshots into interval bars. Public Sina
backfills about eight sessions; a configured Tushare minute entitlement can
backfill the full preceding twenty. Missing sessions remain explicitly missing.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import zlib
from statistics import median

import requests

from .intraday_evidence import SHANGHAI, _datetime, _prior_sessions, METRIC_DEFINITIONS

SINA_URL = 'https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn/'}


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def symbol(code):
    if not re.fullmatch(r'(?:60|68|00|30)\d{4}', str(code)):
        raise ValueError('invalid SH/SZ equity code')
    return ('sh' if code.startswith(('6',)) else 'sz') + code


def bar_session(at):
    minute = at.hour * 60 + at.minute
    return 'am' if 570 < minute <= 690 else 'pm' if 780 < minute <= 900 else None


def normalize_bars(rows, cutoff):
    """Reject conflicting duplicates, malformed bars and future/partial minutes."""
    cutoff = _datetime(cutoff)
    result, conflicts = {}, set()
    for row in rows:
        try:
            at = _datetime(row['end'])
            values = {key: number(row.get(key)) for key in ('open','high','low','close','amount_cny','volume_shares')}
            if at.second or at.microsecond or at > cutoff or not bar_session(at):
                continue
            if any(values[key] is None for key in ('open', 'high', 'low', 'close')):
                continue
            if not 0 < values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']:
                continue
            if values['amount_cny'] is not None and values['amount_cny'] < 0:
                continue
            if values['volume_shares'] is not None and values['volume_shares'] < 0:
                continue
            # A display adapter may encode an unavailable amount as zero while
            # reporting positive volume. Preserve the missing value explicitly.
            if values['amount_cny'] == 0 and values['volume_shares'] and values['volume_shares'] > 0:
                values['amount_cny'] = None
            # Unit checks catch lots/shares and thousands/yuan errors. Zero-volume
            # suspension bars may exist; an amount without volume is invalid.
            if values['volume_shares'] == 0 and values['amount_cny'] not in (None, 0):
                continue
            if values['volume_shares'] and values['amount_cny'] is not None and not values['low']-.03 <= values['amount_cny']/values['volume_shares'] <= values['high']+.03:
                continue
            cleaned = {'end':at.isoformat(), **values, 'source':row.get('source','unknown'),
                       'fetched_at':row.get('fetched_at') or row.get('fetchedAt')}
            if at in result:
                economic = ('open','high','low','close','amount_cny','volume_shares','source')
                if any(result[at].get(key) != cleaned.get(key) for key in economic):
                    conflicts.add(at)
                elif result[at].get('fetched_at') is None and cleaned['fetched_at'] is not None:
                    result[at] = cleaned
            else:
                result[at] = cleaned
        except (KeyError, TypeError, ValueError):
            continue
    for at in conflicts:
        result.pop(at, None)
    return result


class MinuteArchive:
    def __init__(self, directory):
        self.path = Path(directory) / 'minute-bars-v2.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions(code TEXT, day TEXT, payload BLOB, PRIMARY KEY(code,day))')
            db.execute('CREATE TABLE IF NOT EXISTS attempts(code TEXT, day TEXT, source TEXT, status TEXT, PRIMARY KEY(code,day,source))')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def read(self, code, cutoff):
        start = (_datetime(cutoff)-timedelta(days=65)).date().isoformat()
        with self.connect() as db:
            rows = []
            for record in db.execute('SELECT payload FROM sessions WHERE code=? AND day>=? AND day<=? ORDER BY day',
                    (code, start, _datetime(cutoff).date().isoformat())):
                rows.extend(json.loads(zlib.decompress(record[0])))
            return list(normalize_bars(rows,cutoff).values())

    def save(self, code, rows, cutoff):
        rows = normalize_bars(rows, cutoff)
        days = {}
        for at,row in rows.items():
            days.setdefault(at.date().isoformat(),{})[row['end']] = row
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for day,fresh in days.items():
                old = db.execute('SELECT payload FROM sessions WHERE code=? AND day=?',(code,day)).fetchone()
                merged = {row['end']:row for row in json.loads(zlib.decompress(old[0]))} if old else {}
                for stamp, row in fresh.items():
                    previous = merged.get(stamp)
                    # Do not erase a previously observed real amount with a
                    # chart adapter's missing-amount sentinel for the same bar.
                    if (previous and previous.get('amount_cny') is not None
                            and row.get('amount_cny') is None
                            and all(abs((number(previous.get(key)) or 0)-(number(row.get(key)) or 0)) <= .011
                                    for key in ('open','high','low','close'))):
                        continue
                    merged[stamp] = row
                payload = zlib.compress(json.dumps([merged[k] for k in sorted(merged)],separators=(',',':')).encode('utf-8'))
                db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?)',(code,day,payload))
            db.execute('DELETE FROM sessions WHERE code=? AND day<?', (code, (_datetime(cutoff)-timedelta(days=65)).date().isoformat()))

    def attempted(self, code, day, source):
        with self.connect() as db:
            return db.execute('SELECT status FROM attempts WHERE code=? AND day=? AND source=?', (code,day,source)).fetchone()

    def claim_attempt(self, code, day, source):
        """Claim one historical request per stock/day across local processes."""
        with self.connect() as db:
            cursor = db.execute('INSERT OR IGNORE INTO attempts VALUES(?,?,?,?)',
                                (code,day,source,'started'))
            return cursor.rowcount == 1

    def record(self, code, day, source, status):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?,?)',(code,day,source,status))


def fetch_sina_bars(code, *, count=1970, timeout=5, getter=None):
    response = (getter or requests.get)(SINA_URL, params={'symbol':symbol(code), 'scale':'1','ma':'no','datalen':str(count)},
                                        headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    match = re.search(r'=\s*\((.*)\);?\s*$', response.text, re.S)
    raw = json.loads(match.group(1)) if match else None
    if not isinstance(raw, list) or not raw:
        raise ValueError('Sina returned no minute bars')
    return [{'end':row.get('day'), **{key:row.get(key) for key in ('open','high','low','close')},
             'amount_cny':row.get('amount'), 'volume_shares':row.get('volume'), 'source':'Sina 1min unadjusted'} for row in raw]


def fetch_software_bars(code, *, count=8000, client=None, end=None):
    """Reuse the desktop chart provider and its loopback cache for raw bars."""
    if client is None:
        from src.services.software_market import SoftwareMarketClient
        client = SoftwareMarketClient.from_environment()
    if not client.available:
        raise RuntimeError('software market gateway unavailable')
    options = {'end':end} if end else {}
    packet = client.bars(code, period='1', count=count, **options)
    if packet.get('time_semantics') not in (None, 'bar_end'):
        raise ValueError('software market minute timestamps are not bar ends')
    bars = packet.get('bars') or []
    if not isinstance(bars, list) or not bars:
        raise ValueError('software market returned no minute bars')
    return bars


def fetch_recent_bars(code, *, count=240):
    """Prefer software bars; use the existing public feed if desktop is absent."""
    try:
        return fetch_software_bars(code,count=count)
    except (requests.RequestException,RuntimeError,ValueError,TypeError,KeyError):
        return fetch_sina_bars(code,count=count)


def fetch_tushare_bars(code, cutoff, *, timeout=8, poster=None):
    token = os.getenv('TUSHARE_TOKEN','').strip() or os.getenv('TUSHARE_API_TOKEN','').strip()
    if not token:
        raise ValueError('Tushare minute token not configured')
    dates = _prior_sessions(_datetime(cutoff).date())
    if len(dates) != 20:
        raise ValueError('Exchange calendar unavailable')
    # 20 previous sessions = 4800 rows, below the documented 8000-row limit.
    url = os.getenv('TUSHARE_API_URL','').strip() or os.getenv('TUSHARE_HTTP_URL','').strip() or 'https://api.tushare.pro'
    response = (poster or requests.post)(url, json={'api_name':'stk_mins','token':token,
        'params':{'ts_code':code+('.SH' if code.startswith('6') else '.SZ'),'freq':'1min',
                  'start_date':str(dates[0])+' 09:00:00','end_date':str(dates[-1])+' 15:01:00'},
        'fields':'ts_code,trade_time,open,close,high,low,vol,amount'}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get('code') != 0 or not isinstance(payload.get('data'), dict):
        # Never include response messages/URLs: providers can echo credentials.
        raise ValueError('Tushare minute permission or service unavailable')
    data = payload['data']
    rows = [dict(zip(data.get('fields', []), item)) for item in data.get('items', [])]
    if not rows or any(row.get('ts_code') != code+('.SH' if code.startswith('6') else '.SZ') for row in rows):
        raise ValueError('Tushare minute identity or data missing')
    return [{'end':row['trade_time'], **{key:row.get(key) for key in ('open','high','low','close')},
             'amount_cny':row.get('amount'), 'volume_shares':row.get('vol'), 'source':'Tushare stk_mins 1min unadjusted'} for row in rows]


def window(rows, end, size):
    stamps = [end-timedelta(minutes=i) for i in range(size-1,-1,-1)]
    if any(at not in rows or bar_session(at) != bar_session(end) for at in stamps):
        return None
    return [rows[at] for at in stamps]


def _window_features(rows, end):
    """Completed, contiguous, same-session windows only; returns in percentage points."""
    result = {}
    for minutes in (1, 3, 5, 15):
        prices = window(rows, end, minutes + 1)
        result[f'r{minutes}_pct'] = (prices[-1]['close'] / prices[0]['close'] - 1) * 100 if prices else None
        amounts = window(rows, end, minutes)
        result[f'amount_{minutes}m'] = (sum(row['amount_cny'] for row in amounts)
            if amounts and all(row['amount_cny'] is not None for row in amounts) else None)
        result[f'volume_{minutes}m'] = (sum(row['volume_shares'] for row in amounts)
            if amounts and all(row['volume_shares'] is not None for row in amounts) else None)
    if result['r1_pct'] is not None and result['r5_pct'] is not None:
        result['a1_pct_per_min'] = result['r1_pct'] - result['r5_pct'] / 5
    else:
        result['a1_pct_per_min'] = None
    if result['r3_pct'] is not None and result['r15_pct'] is not None:
        result['a3_pct_per_min'] = result['r3_pct'] / 3 - result['r15_pct'] / 15
    else:
        result['a3_pct_per_min'] = None
    return result


def _robust_z(current, history, *, floor):
    """Median/MAD with a unit-aware floor; zero dispersion never explodes a score."""
    if current is None or len(history) < 5:
        return None
    centre = median(history)
    scale = max(1.4826 * median(abs(value-centre) for value in history),
                abs(centre) * .05, floor)
    return max(-6.0, min(6.0, (current-centre)/scale))


def compute_bar_evidence(rows, cutoff, *, expected_dates=None):
    cutoff = _datetime(cutoff)
    rows = normalize_bars(rows, cutoff)
    current = {at:row for at,row in rows.items() if at.date() == cutoff.date()}
    result = {'metrics':dict.fromkeys(METRIC_DEFINITIONS), 'source':'dated 1min bars', 'asOf':None,
              'gaps':[], 'historyDays':0, 'priceHistoryDays':0, 'featureHistoryDays':{},
              'baselineReady':False, 'baselineDates':[], 'missingHistoryDates':[],
              'sourceTimePrecision':'bar_end', 'metricDefinitions':METRIC_DEFINITIONS.copy(),
              'v11Inputs':{}, 'v11Coverage':{'status':'insufficient','days':0,'requiredDays':20}}
    result['metricDefinitions'].update(amount_3m='截至该分钟结束时间的连续3根1分钟K线成交额之和（元）',
        local_high_5m='此前5根完整1分钟K线最高价，不含当前根（元）',
        local_low_5m='此前5根完整1分钟K线最低价，不含当前根（元）')
    gaps, metrics = result['gaps'], result['metrics']
    if not current:
        gaps.append('当日完整1分钟K线未取得')
        return result
    end = max(current)
    result.update(asOf=end.isoformat(), source=current[end]['source'], currentPrice=current[end]['close'],
                  sourceFetchedAt=current[end].get('fetched_at'),
                  sourceAgeSeconds=(cutoff-end).total_seconds())
    if (cutoff-end).total_seconds() >= 300:
        gaps.append('分钟行情已陈旧，未用于入选')
        return result
    for n in (1,3,5):
        part = window(current,end,n+1)
        if part:
            metrics[f'speed_{n}m_pct'] = (part[-1]['close']/part[0]['close']-1)*100
        else:
            gaps.append(f'缺连续{n}分钟价格窗口')
    prior = window(current,end-timedelta(minutes=1),5)
    if prior:
        metrics.update(local_high_5m=max(r['high'] for r in prior),local_low_5m=min(r['low'] for r in prior))
    current_features = _window_features(current, end)
    metrics['amount_3m'] = current_features['amount_3m']
    # Cumulative VWAP only when every elapsed regular-session bar is present.
    stamps = [end.replace(hour=9,minute=31)+timedelta(minutes=i) for i in range(330)]
    needed = [at for at in stamps if at <= end and bar_session(at)]
    vwap_series = {}
    if needed and all(at in current and current[at]['amount_cny'] is not None
                      and current[at]['volume_shares'] is not None for at in needed):
        vol = sum(current[at]['volume_shares'] for at in needed)
        metrics['vwap'] = sum(current[at]['amount_cny'] for at in needed)/vol if vol else None
        cumulative_amount = cumulative_volume = 0.
        for at in needed:
            cumulative_amount += current[at]['amount_cny']
            cumulative_volume += current[at]['volume_shares']
            if cumulative_volume > 0:
                vwap_series[at] = cumulative_amount/cumulative_volume
    dates = list(expected_dates) if expected_dates is not None else list(_prior_sessions(end.date()))
    dates = sorted({datetime.fromisoformat(str(d)).date() for d in dates if str(d) < end.date().isoformat()})[-20:]
    samples = []
    feature_history = {key: [] for key in current_features}
    if len(dates) == 20:
        for day in dates:
            previous = end.replace(year=day.year,month=day.month,day=day.day)
            part = window(rows,previous,3)
            sample = _window_features(rows, previous)
            for key, value in sample.items():
                if value is not None:
                    feature_history[key].append(value)
            if part and all(r['amount_cny'] is not None for r in part):
                samples.append(sample['amount_3m'])
                result['baselineDates'].append(day.isoformat())
            else:
                result['missingHistoryDates'].append(day.isoformat())
    result['historyDays'] = len(samples)
    result['priceHistoryDays'] = len(feature_history['r3_pct'])
    result['featureHistoryDays'] = {key:len(values) for key,values in feature_history.items()}
    coverage = 'complete' if len(samples) == 20 else 'low' if len(samples) >= 5 else 'insufficient'
    result['v11Coverage'] = {'status':coverage, 'days':len(samples),
                             'priceDays':result['priceHistoryDays'],
                             'factorDays':result['featureHistoryDays'],'requiredDays':20}
    v11 = {'history_days':len(samples),'price_history_days':result['priceHistoryDays'], **current_features}
    if end in vwap_series:
        previous_at = end-timedelta(minutes=1)
        previous_vwap = vwap_series.get(previous_at)
        v11['vwap_reclaim'] = (1. if previous_vwap is not None and current[previous_at]['close'] <= previous_vwap
                                  and current[end]['close'] > vwap_series[end] else 0.
                                  if previous_vwap is not None else None)
        five_back = vwap_series.get(end-timedelta(minutes=5))
        v11['vwap_slope_5m_pct'] = ((vwap_series[end]/five_back-1)*100
            if five_back and five_back > 0 else None)
        recent = window(current,end,5)
        if recent and all(datetime.fromisoformat(row['end']) in vwap_series for row in recent):
            v11['vwap_retest_success'] = 1. if any(
                row['low'] <= vwap_series[datetime.fromisoformat(row['end'])]*1.002
                and row['close'] > vwap_series[datetime.fromisoformat(row['end'])]
                for row in recent) else 0.
    previous_five = window(current,end-timedelta(minutes=1),5)
    if previous_five:
        prior_high = max(row['high'] for row in previous_five)
        v11['failed_breakout_risk'] = (100. if current[end]['high'] > prior_high
            and current[end]['close'] <= prior_high else 0.)
        recent_highs = [row['high'] for row in previous_five[-2:]] + [current[end]['high']]
        v11['lower_high_risk'] = (100. if recent_highs[0] > recent_highs[1] > recent_highs[2] else 0.)
    for key in ('r1_pct','r3_pct','r5_pct','r15_pct','a1_pct_per_min','a3_pct_per_min'):
        v11['z_'+key] = _robust_z(current_features[key],feature_history[key],floor=.05)
    for minutes in (3,5,15):
        key = f'amount_{minutes}m'
        baseline = feature_history[key]
        centre = median(baseline) if len(baseline) >= 5 else None
        v11[f'baseline_{key}'] = centre
        v11[f'v{minutes}_ratio'] = (current_features[key]/centre
            if current_features[key] is not None and centre is not None and centre > 0 else None)
        v11[f'z_{key}'] = _robust_z(current_features[key],baseline,floor=1.0)
        volume_key = f'volume_{minutes}m'
        volume_history = feature_history[volume_key]
        v11[f'baseline_{volume_key}'] = (median(volume_history)
            if len(volume_history) >= 5 else None)
    v3, v15 = v11.get('v3_ratio'),v11.get('v15_ratio')
    v11['va_ratio'] = v3/v15 if v3 is not None and v15 and v15 > 0 else None
    v11['price_efficiency_r5_v5'] = (v11['r5_pct']/v11['v5_ratio']
        if v11.get('r5_pct') is not None and v11.get('v5_ratio') and v11['v5_ratio'] > 0 else None)
    result['v11Inputs'] = v11
    result['baselineMedianAmount'] = median(samples) if len(samples) == 20 else None
    result['baselineSource'] = ', '.join(sorted({r['source'] for at,r in rows.items() if at.date() in dates}))
    if len(samples) == 20 and median(samples) > 0 and metrics['amount_3m'] is not None:
        metrics['relative_amount_3m_20d'] = metrics['amount_3m']/median(samples)
        result['baselineReady'] = True
    else:
        gaps.append(f'同刻20日分钟基准未就绪（{len(samples)}/20）；不以少量样本或全天量比替代')
    result['basis'] = '1分钟K线结束时间；3分钟额为连续3根成交额之和；历史基准严格使用此前20个交易日'
    return result


def fetch_bar_evidence(code, cutoff, cache_dir, *, getter=None, poster=None, backfill=True, software_count=8000):
    cutoff = _datetime(cutoff)
    archive = MinuteArchive(cache_dir)
    errors = []
    software_fetched = False
    client = None
    if getter is None:
        from src.services.software_market import SoftwareMarketClient
        client = SoftwareMarketClient.from_environment()
        if client.available:
            try:
                bars = fetch_software_bars(code,count=software_count,client=client)
                normalized = normalize_bars(bars,cutoff)
                if not normalized:
                    raise ValueError('no usable software minute bars')
                archive.save(code,list(normalized.values()),cutoff)
                software_fetched = True
            except (requests.RequestException, RuntimeError, ValueError, TypeError, KeyError) as exc:
                errors.append('软件分钟更新失败：'+type(exc).__name__)
    if not software_fetched:
        try:
            archive.save(code, fetch_sina_bars(code,getter=getter), cutoff)
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            errors.append('Sina分钟更新失败：'+type(exc).__name__)
    rows = archive.read(code,cutoff)
    result = compute_bar_evidence(rows, cutoff)
    day = cutoff.date().isoformat()
    history_backfill = {'status':'not_needed' if result['historyDays'] >= 20 else 'not_available',
                        'pages':0,'newBars':0,'requestedEnds':[]}
    if (backfill and result['historyDays'] < 20 and client is not None and client.available
            and rows and archive.claim_attempt(code,day,'software_history')):
        history_backfill['status'] = 'empty'
        try:
            for _ in range(2):
                oldest = min(_datetime(row['end']) for row in rows)
                before = (oldest-timedelta(minutes=1)).strftime('%Y%m%d%H%M%S')
                history_backfill['requestedEnds'].append(before)
                page = fetch_software_bars(code,count=8000,client=client,end=before)
                history_backfill['pages'] += 1
                older = [row for at,row in normalize_bars(page,cutoff).items() if at < oldest]
                if not older:
                    break  # Provider ignored end or has no older data.
                archive.save(code,older,cutoff)
                history_backfill['newBars'] += len(older)
                rows = archive.read(code,cutoff)
                result = compute_bar_evidence(rows,cutoff)
                history_backfill['status'] = 'fetched'
                if result['historyDays'] >= 20:
                    break
        except (requests.RequestException,RuntimeError,ValueError,TypeError,KeyError) as exc:
            errors.append('软件历史分钟回填失败：'+type(exc).__name__)
            history_backfill['status'] = 'failed' if not history_backfill['newBars'] else 'partial'
        archive.record(code,day,'software_history',history_backfill['status'])
    elif backfill and result['historyDays'] < 20 and archive.attempted(code,day,'software_history'):
        history_backfill['status'] = 'already_attempted_today'
    if backfill and not result['baselineReady'] and not archive.attempted(code,day,'tushare'):
        if os.getenv('TUSHARE_TOKEN') or os.getenv('TUSHARE_API_TOKEN'):
            try:
                archive.save(code, fetch_tushare_bars(code,cutoff,poster=poster), cutoff)
                archive.record(code,day,'tushare','fetched')
                result = compute_bar_evidence(archive.read(code,cutoff),cutoff)
            except (requests.RequestException,ValueError,TypeError,KeyError) as exc:
                errors.append('Tushare历史分钟回填失败：'+type(exc).__name__)
                archive.record(code,day,'tushare','failed')
    result.update(code=code, fetchedAt=datetime.now(SHANGHAI).isoformat(), archivePath=str(archive.path))
    result['historyBackfill'] = history_backfill
    result['gaps'].extend(errors)
    result['softwareMarketUsed'] = software_fetched
    result['historyAccess'] = 'configured' if os.getenv('TUSHARE_TOKEN') or os.getenv('TUSHARE_API_TOKEN') else 'public_archive_warming'
    return result


def archive_universe(codes, cutoff, cache_dir, *, workers=4, fetcher=None, progress=None):
    """Daily whole-universe maintenance, independent of recommendation membership."""
    cutoff = _datetime(cutoff)
    archive = MinuteArchive(cache_dir)
    registry = Path(cache_dir)/'archive-universe.json'
    try:
        saved = json.loads(registry.read_text(encoding='utf-8')).get('codes',[])
    except (OSError,ValueError,TypeError):
        saved = []
    codes = sorted({str(code) for code in [*codes,*saved] if re.fullmatch(r'(?:60|00)\d{4}',str(code))})
    temporary=registry.with_suffix('.tmp')
    temporary.write_text(json.dumps({'codes':codes,'updatedAt':cutoff.isoformat()}),encoding='utf-8')
    os.replace(temporary,registry)
    day = cutoff.date().isoformat()
    def collect(code):
        if archive.attempted(code,day,'sina_archive') == ('ok',):
            return 'cached'
        try:
            raw = fetcher(code) if fetcher else fetch_recent_bars(code,count=240)
            rows = normalize_bars(raw,cutoff)
            if not rows:
                raise ValueError('no valid bars')
            archive.save(code,list(rows.values()),cutoff)
            archive.record(code,day,'sina_archive','ok')
            return 'updated'
        except Exception:
            archive.record(code,day,'sina_archive','failed')
            return 'failed'
    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = []
        for status in pool.map(collect,codes):
            statuses.append(status)
            if progress and (len(statuses)%100==0 or len(statuses)==len(codes)):
                progress({'done':len(statuses),'total':len(codes),'failed':statuses.count('failed')})
    return {'universe':len(codes), **{key:statuses.count(key) for key in ('cached','updated','failed')},
            'archivePath':str(archive.path),'asOf':cutoff.isoformat(),
            'scope':'全部沪深主板；与是否入选无关；公共源滚动积累，完整20日方可启用基准'}
