"""Independent sector breadth and provider-defined fund evidence.

Sector breadth is computed from all available constituents at the SAME bar end,
excluding the candidate itself. It is never replaced with daily advancing counts.
Fund flows are vendor estimates, not verified institutional account movements.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError as FutureTimeout, wait
from datetime import datetime, timedelta
from math import ceil
import json
import os
from pathlib import Path
import re
from statistics import mean
from threading import BoundedSemaphore, Lock, local
from time import monotonic
from uuid import uuid4

import requests
from src.patches.eastmoney_patch import original_request

from .intraday_evidence import _datetime, SHANGHAI
from .minute_history import (MinuteArchive, fetch_recent_bars, fetch_software_bars,
                             normalize_bars, window, number, bounded_fetch_map)

BASE = 'https://push2.eastmoney.com/api/qt/'
HEADERS = {'User-Agent':'Mozilla/5.0','Referer':'https://quote.eastmoney.com/'}
EQUITY_CODE = re.compile(r'(?:60|68|00|30)\d{4}')
BULK_INDUSTRY_URL = 'https://data.eastmoney.com/dataapi/xuangu/list'
_recent_sector_bars = {}
_recent_sector_lock = Lock()
_sector_rotation_lock = Lock()


def _collection_count(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def sector_collection_plan(priorities, cache_dir):
    """Deterministic fair peer order, independent of the candidate queue order.

    Industries not reached in earlier scans get the next first turns. Within
    the same service turn, complete the smallest remaining evidence gap first.
    Keep each industry's peers together: spreading one request across every
    industry can consume the entire budget without completing any denominator.
    """
    path = Path(cache_dir) / 'sector-collection-rotation-v1.json'
    with _sector_rotation_lock:
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(state, dict) or state.get('version') != 1 or not isinstance(state.get('industries'), dict):
                state = {}
        except (OSError, ValueError, TypeError):
            state = {}
    industries = state.get('industries') or {}
    def priority(item):
        needed, absent_count, board, _ = item
        previous = industries.get(board)
        previous = previous if isinstance(previous, dict) else {}
        return (_collection_count(previous.get('lastAttemptTurn')), -_collection_count(previous.get('incompleteScans')),
                needed, absent_count, board)
    ordered = sorted(priorities, key=priority)
    queues = [(board, list(absent)) for _, _, board, absent in ordered]
    peers, seen = [], set()
    for _, absent in queues:
        for peer in absent:
            if peer not in seen:
                seen.add(peer)
                peers.append(peer)
    return peers, state, [board for board, _ in queues]


def record_sector_collection(cache_dir, selected_boards, attempted, usable, required, cutoff):
    """Persist scheduling evidence, never alter market observations or weights."""
    path = Path(cache_dir) / 'sector-collection-rotation-v1.json'
    with _sector_rotation_lock:
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(state, dict) or state.get('version') != 1 or not isinstance(state.get('industries'), dict):
                state = {}
        except (OSError, ValueError, TypeError):
            state = {}
        turn = _collection_count(state.get('turn')) + 1
        industries = state.get('industries') or {}
        for board, members in selected_boards.items():
            key, peers = str(board), set(members)
            previous = industries.get(key)
            previous = previous if isinstance(previous, dict) else {}
            complete = len(peers & usable) >= required.get(key, len(peers))
            industries[key] = {'lastAttemptTurn': turn if peers & attempted else _collection_count(previous.get('lastAttemptTurn')),
                'incompleteScans': 0 if complete else _collection_count(previous.get('incompleteScans')) + 1,
                'usablePeers': len(peers & usable), 'totalPeers': len(peers),
                'lastObservedAt': _datetime(cutoff).isoformat()}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name+'.'+uuid4().hex+'.tmp')
            temporary.write_text(json.dumps({'version': 1, 'turn': turn, 'industries': industries},
                ensure_ascii=False, sort_keys=True), encoding='utf-8')
            os.replace(temporary, path)
        except OSError:
            # Scheduling metadata failure cannot invalidate accepted evidence.
            pass
_fund_hosts = (BASE, 'https://70.push2.eastmoney.com/api/qt/', 'https://17.push2.eastmoney.com/api/qt/')
_fund_host_lock = Lock()
_fund_host_health = {}
# Two reused connections avoid the observed cold eight-connection burst: all
# eight valid Tencent replies arrived after the per-request wall-clock budget.
# The bounded two-connection probe completed all 33 windows within that budget.
FUND_TRANSPORT_WORKERS = 2
_fund_transport_pool = ThreadPoolExecutor(max_workers=FUND_TRANSPORT_WORKERS, thread_name_prefix='fund-public-http')
_fund_transport_slots = BoundedSemaphore(FUND_TRANSPORT_WORKERS)
_fund_transport_local = local()
TENCENT_FUND_URL = 'https://proxy.finance.qq.com/cgi/cgi-bin/fundflow/hsfundtab'
TENCENT_FUND_BASIS = ('腾讯供应商大单分类估算；主力=超大单+大单，成交额≥20万元或成交量≥6万股判为主力资金；'
                      '逐分钟累计净额作同窗差分，不等于机构账户真实流入，与东财分类阈值不混用')


class _FundTransportCapacityTimeout(requests.Timeout):
    """No request reached a provider; this cannot establish host failure."""


def _fund_public_get(url, **kwargs):
    # One session per bounded I/O worker reuses TLS connections without sharing
    # mutable requests.Session objects between threads or using environment auth.
    session = getattr(_fund_transport_local, 'session', None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        _fund_transport_local.session = session
    return original_request(session, 'GET', url, **kwargs)


def _fund_request(url, *, params, getter, deadline):
    # requests' socket timeout is not a whole-call limit: DNS/multiple addresses
    # can exceed it. Keep late I/O in one fixed pool, never spawn unbounded
    # abandoned request threads or hold a scan worker past its shared deadline.
    end = min(deadline, monotonic()+4)
    if not _fund_transport_slots.acquire(timeout=max(0, end-monotonic())):
        raise _FundTransportCapacityTimeout('fund transport capacity deadline')
    remaining = end-monotonic()
    if remaining <= 0:
        _fund_transport_slots.release()
        raise _FundTransportCapacityTimeout('fund request deadline')

    def request():
        try:
            request_remaining = end-monotonic()
            if request_remaining <= 0:
                raise _FundTransportCapacityTimeout('fund request deadline')
            return (getter or _fund_public_get)(url, params=params, headers=HEADERS, timeout=request_remaining)
        finally:
            _fund_transport_slots.release()

    try:
        future = _fund_transport_pool.submit(request)
    except Exception:
        _fund_transport_slots.release()
        raise
    try:
        return future.result(timeout=max(0, end-monotonic()))
    except FutureTimeout as exc:
        # The worker retains its slot until the actual socket call ends.
        raise requests.Timeout('fund request wall-clock deadline') from exc


def _fund_data(code, *, getter, deadline):
    shared_deadline = deadline if deadline is not None else monotonic()+4*len(_fund_hosts)
    with _fund_host_lock:
        health = dict(_fund_host_health) if getter is None else {}
    hosts = sorted(_fund_hosts, key=lambda host: -health.get(host, {}).get('lastSuccess', 0))
    errors, skipped = [], []
    params = {'secid':('1.' if code.startswith('6') else '0.')+code,
        'lmt':'0','klt':'1','fields1':'f1,f2,f3,f7',
        'fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63'}
    for host in hosts:
        if monotonic() >= shared_deadline:
            errors.append('Timeout')
            break
        if getter is None:
            with _fund_host_lock:
                blocked = _fund_host_health.get(host, {}).get('blockedUntil', 0) > monotonic()
            if blocked:
                skipped.append(host)
                continue
        attempt_started = monotonic()
        try:
            response = _fund_request(host+'stock/fflow/kline/get', params=params,
                                     getter=getter, deadline=shared_deadline)
            response.raise_for_status()
            response.encoding = 'utf-8'
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('fund response schema unavailable')
            data = payload.get('data')
            if payload.get('rc') != 0 or not isinstance(data, dict) or str(data.get('code')) != code:
                raise ValueError('fund response code or schema unavailable')
            if getter is None:
                with _fund_host_lock:
                    _fund_host_health[host] = {'lastSuccess':monotonic(), 'blockedUntil':0}
            return data, host
        except (requests.RequestException, ValueError, TypeError) as exc:
            errors.append(type(exc).__name__)
            # A stock-specific missing payload must not suppress other stocks.
            # Transport failures are retried in the next stage/scan; the
            # cooldown ends at this existing shared deadline, not a new TTL.
            if (getter is None and isinstance(exc, (requests.ConnectionError, requests.Timeout))
                    and not isinstance(exc, _FundTransportCapacityTimeout)):
                with _fund_host_lock:
                    prior = _fund_host_health.get(host, {})
                    if prior.get('lastSuccess', 0) <= attempt_started:
                        _fund_host_health[host] = {**prior, 'blockedUntil':shared_deadline}
    if skipped:
        errors.append('TransportBackoff')
    raise ValueError('Eastmoney sources unavailable: '+','.join(errors))


def public_get(url, **kwargs):
    # The legacy global Eastmoney patch rewrites headers, sends an unrelated
    # fingerprint POST and sleeps randomly. These read-only feeds use an isolated
    # ordinary HTTP request, with no NID/cookie fabrication or background auth.
    with requests.Session() as session:
        session.trust_env = False
        return original_request(session,'GET',url,**kwargs)


def get_data(endpoint, params, getter=None):
    errors=[]
    for base in ('https://70.push2.eastmoney.com/api/qt/','https://17.push2.eastmoney.com/api/qt/',BASE):
        try:
            response = (getter or public_get)(base+endpoint,params=params,headers=HEADERS,timeout=4)
            response.raise_for_status()
            response.encoding = 'utf-8'
            payload = response.json()
            if payload.get('rc') != 0 or not isinstance(payload.get('data'),dict):
                raise ValueError('Eastmoney data unavailable')
            return payload['data']
        except (requests.RequestException,ValueError,TypeError) as exc:
            errors.append(type(exc).__name__)
    raise ValueError('Eastmoney sources unavailable: '+','.join(errors))


def all_members(fs, getter=None):
    collected = {}
    for page in range(1,61):
        data = get_data('clist/get',{'fs':fs,'pn':str(page),'pz':'100','fields':'f12,f14','fltt':'2','fid':'f3','po':'1'},getter)
        rows = data.get('diff') or []
        rows = rows.values() if isinstance(rows,dict) else rows
        previous = len(collected)
        for row in rows:
            if row.get('f12') and row.get('f14'):
                collected[row['f12']] = {'code':row['f12'],'name':row['f14']}
        if len(collected) >= int(data.get('total') or 0) and collected:
            return list(collected.values())
        if len(collected) == previous:
            raise ValueError('Incomplete constituent pagination')
    raise ValueError('Constituent page limit exceeded')


def cached_metadata(directory, key, cutoff, fetch):
    directory = Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    path = directory/(key+'.json')
    cutoff = _datetime(cutoff)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        observed = _datetime(data['fetchedAt'])
        if observed.date() == cutoff.date() and observed <= cutoff and data['value']:
            return data['value']
    except (OSError,ValueError,TypeError,KeyError):
        pass
    value = fetch()
    tmp = path.with_name('.'+key+'-'+uuid4().hex+'.tmp')
    try:
        tmp.write_text(json.dumps({'fetchedAt':datetime.now(SHANGHAI).isoformat(),'value':value},ensure_ascii=False),encoding='utf-8')
        os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)
    return value


def _bulk_industry_fields(row, cutoff):
    name, stamp = row.get('industry'), str(row.get('sourceDate') or '')
    if not isinstance(name, str) or not name.strip():
        raise ValueError('行业全市场目录存在未分类沪深股票')
    try:
        source_date = datetime.fromisoformat(stamp).date()
    except ValueError as exc:
        raise ValueError('行业元数据缺少源日期') from exc
    if source_date > cutoff.date():
        raise ValueError('行业元数据源日期晚于决策日期')
    return name.strip(), source_date.isoformat()


def discover_bulk_sectors(codes, cutoff, cache_dir, *, getter=None):
    """Read the complete unfiltered stock catalogue once, then group its taxonomy.

    The desktop screening snapshot can be filtered upstream despite reporting
    complete pagination. It is deliberately not a source of constituent counts.
    This endpoint is requested without screening filters, and every page/count
    is validated before any industry is allowed to supply a denominator.
    """
    cutoff = _datetime(cutoff)

    def fetch():
        collected, expected = {}, None
        for page in range(1, 21):
            response = (getter or public_get)(BULK_INDUSTRY_URL,
                params={'st':'SECURITY_CODE', 'sr':'1', 'ps':'6000', 'p':str(page),
                    'sty':'SECURITY_CODE,SECURITY_NAME_ABBR,INDUSTRY,MAX_TRADE_DATE',
                    'source':'SELECT_SECURITIES', 'client':'WEB', 'hyversion':'v2'},
                headers=HEADERS, timeout=4)
            response.raise_for_status()
            response.encoding = 'utf-8'
            payload = response.json()
            result = payload.get('result') or {}
            count, rows = result.get('count'), result.get('data')
            if payload.get('success') is not True or not isinstance(count, int) or count <= 0 or not isinstance(rows, list):
                raise ValueError('行业全市场目录返回结构无效')
            if expected is not None and count != expected:
                raise ValueError('行业全市场目录分页总数变化')
            expected, before = count, len(collected)
            for row in rows:
                code = str(row.get('SECURITY_CODE') or '')
                if not re.fullmatch(r'\d{6}', code) or code in collected:
                    raise ValueError('行业全市场目录代码重复或无效')
                collected[code] = {'code':code, 'industry':row.get('INDUSTRY'),
                                   'sourceDate':row.get('MAX_TRADE_DATE')}
                if EQUITY_CODE.fullmatch(code):
                    _bulk_industry_fields(collected[code], cutoff)
            if len(collected) > expected or len(collected) == before:
                raise ValueError('行业全市场目录分页无进展')
            if result.get('nextpage') is False:
                if len(collected) != expected:
                    raise ValueError('行业全市场目录数量不完整')
                return {'rows':list(collected.values()), 'expectedCount':expected,
                    'observedAt':datetime.now(SHANGHAI).isoformat(),
                    'source':'eastmoney:xuangu-unfiltered', 'complete':True}
        raise ValueError('行业全市场目录超过分页上限')

    catalogue = cached_metadata(Path(cache_dir)/'sector-membership', 'xuangu-industries-v1', cutoff, fetch)
    if (not isinstance(catalogue, dict) or catalogue.get('complete') is not True
            or catalogue.get('source') != 'eastmoney:xuangu-unfiltered'
            or not isinstance(catalogue.get('rows'), list)
            or len(catalogue['rows']) != catalogue.get('expectedCount')):
        raise ValueError('行业全市场缓存缺少完整性证明')
    groups, identities, dates = {}, {}, {}
    seen = set()
    for row in catalogue['rows']:
        code = str(row.get('code') or '')
        if not re.fullmatch(r'\d{6}', code) or code in seen:
            raise ValueError('行业全市场缓存代码重复或无效')
        seen.add(code)
        if not EQUITY_CODE.fullmatch(code):
            continue  # Explicit scope: all Shanghai/Shenzhen A shares, no Beijing.
        name, source_date = _bulk_industry_fields(row, cutoff)
        # Never mix this source's taxonomy with legacy BK or Sina industries.
        board = 'EMXG:' + name
        groups.setdefault(board, []).append(code)
        identities[code] = board
        dates.setdefault(board, set()).add(source_date)
    membership, selected = {}, {}
    for code in codes:
        board = identities.get(code)
        if board is None:
            continue
        selected[board] = sorted(groups[board])
        membership[code] = {'code':board, 'name':board.removeprefix('EMXG:'), 'provider':'Eastmoney',
            'classification':'xuangu-industry-v2', 'membershipComplete':True,
            'membershipScope':'all_shanghai_shenzhen_a_shares',
            'membershipCount':len(groups[board]), 'membershipSource':catalogue['source'],
            'membershipCatalogueCount':catalogue['expectedCount'],
            'membershipUniverseCount':len(identities),
            'membershipExcludedCount':len(seen)-len(identities),
            'membershipSourceUrl':BULK_INDUSTRY_URL,
            'membershipObservedAt':catalogue.get('observedAt'),
            'membershipSourceDates':sorted(dates[board]),
            'membershipSourceTimeMeaning':'MAX_TRADE_DATE is the provider row date; classification update time is unavailable; not a quote time'}
    return membership, selected


def discover_sectors(codes, cutoff, cache_dir, *, getter=None, deadline=None):
    directory = Path(cache_dir)/'sector-membership'
    cutoff = _datetime(cutoff)
    if deadline is None:
        deadline = monotonic() + 20
    def saved(key):
        try:
            packet = json.loads((directory/(key+'.json')).read_text(encoding='utf-8'))
            stamp = _datetime(packet['fetchedAt'])
            if stamp.date() == cutoff.date() and stamp <= cutoff and packet.get('value'):
                return packet['value']
        except (OSError,ValueError,TypeError,KeyError):
            pass
        return None
    # A slow uncached symbol must not hide already archived memberships later
    # in the queue when the request budget runs out.
    membership = {code: value for code in codes if isinstance(value := saved('stock-'+code),dict)}
    missing_codes = [code for code in codes if code not in membership]
    industry_map = {}
    if missing_codes and monotonic() < deadline:
        try:
            industries = cached_metadata(directory,'industry-list',cutoff,lambda:all_members('m:90+t:2',getter))
            industry_map = {row['code']:row['name'] for row in industries}
        except Exception:
            pass
    def lookup(code):
        def fetch():
            data = get_data('slist/get',{'secid':('1.' if code.startswith('6') else '0.')+code,
                 'spt':'3','fields':'f12,f14','pn':'1','pz':'100'},getter)
            rows = data.get('diff') or []
            rows = rows.values() if isinstance(rows,dict) else rows
            # Provider order supplies the primary industry first. No selection of
            # whichever sector happened to perform best after seeing its returns.
            return next(({'code':r['f12'],'name':industry_map[r['f12']]} for r in rows if r.get('f12') in industry_map),{})
        try:
            return code, cached_metadata(directory,'stock-'+code,cutoff,fetch)
        except Exception:
            return code, {}
    completed, _ = bounded_fetch_map(lookup,missing_codes if industry_map else [],workers=4,
                                     timeout=max(0,deadline-monotonic()))
    membership.update(completed)
    selected = {row['code']:row for row in membership.values() if row}
    def equity_codes(rows):
        return [row['code'] for row in rows if re.fullmatch(r'(?:60|68|00|30)\d{4}',row['code'])]
    peers = {board: equity_codes(rows) for board in selected if isinstance(rows := saved('board-'+board),list)}
    def constituents(board):
        try:
            rows = cached_metadata(directory,'board-'+board,cutoff,lambda:all_members('b:'+board,getter))
            return board, equity_codes(rows)
        except Exception:
            return board, []
    completed, _ = bounded_fetch_map(constituents,[board for board in selected if board not in peers],
                                     workers=4,timeout=max(0,deadline-monotonic()))
    peers.update(completed)
    return membership, peers


def discover_sina_sectors(codes, cutoff, cache_dir, *, getter=None, deadline=None):
    """Independent industry fallback; caches the complete classification map."""
    get = getter or public_get
    if deadline is None:
        deadline = monotonic() + 20
    def fetch():
        response = get('https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php',timeout=5)
        response.raise_for_status()
        raw = response.text
        catalogue = json.loads(raw[raw.index('{'):].rstrip().rstrip(';'))
        def members(pair):
            key,value = pair
            try:
                fields=value.split(',')
                base='https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.'
                response=get(base+'getHQNodeStockCount',params={'node':key},timeout=5)
                response.raise_for_status()
                count=int(response.json())
                rows={}
                for page in range(1,(count+79)//80+1):
                    response=get(base+'getHQNodeData',params={'node':key,'num':80,'page':page,'sort':'symbol','asc':1,'symbol':'','_s_r_a':'page'},timeout=5)
                    response.raise_for_status()
                    batch=response.json()
                    if not isinstance(batch,list):raise ValueError('invalid members')
                    for row in batch:
                        code=str(row.get('code') or row.get('symbol','')[-6:])
                        rows[code]=row
                if len(rows)!=count or not count:raise ValueError('incomplete members')
                return {'code':key,'name':fields[1],'provider':'Sina','members':sorted(code for code in rows if re.fullmatch(r'(?:60|68|00|30)\d{4}',code))}
            except Exception:
                return None
        completed, missing = bounded_fetch_map(members,sorted(catalogue.items()),workers=4,
                                               timeout=max(0,deadline-monotonic()))
        if missing:
            # Never cache a partial industry catalogue as complete for the day.
            raise TimeoutError('行业目录采集达到时间预算')
        return [row for row in completed if row]
    all_boards=cached_metadata(Path(cache_dir)/'sector-membership','sina-industries',cutoff,fetch)
    membership={code:next(({k:v for k,v in board.items() if k!='members'} for board in all_boards if code in board['members']),{}) for code in codes}
    selected={row.get('code') for row in membership.values()}
    return membership,{board['code']:board['members'] for board in all_boards if board['code'] in selected}


def compute_sector_evidence(code, stock_minutes, sector, peers, minute_rows, cutoff, *, _peer_returns=None):
    provider=sector.get('provider','Eastmoney') if sector else 'unknown'
    result = {'metrics':{},'source':provider+'行业成分 + 已核验代码的同刻1分钟K线', 'asOf':None,
              'sector':sector, 'gaps':[], 'scope':provider+'主行业；全部沪深成分，候选股自身从板块统计剔除',
              'coverageRequiredPct':80, 'minimumPeers':3}
    if sector:
        result['membershipMetadata'] = {key:sector[key] for key in sector
            if key.startswith('membership') or key == 'classification'}
    try:
        end = _datetime(stock_minutes['asOf'])
        if not 0 <= (_datetime(cutoff)-end).total_seconds() < 300:
            raise ValueError('stale')
        speed = number((stock_minutes.get('metrics') or {}).get('speed_5m_pct'))
        if speed is None or not sector:
            raise ValueError('no stock minute evidence or industry')
    except (ValueError,TypeError,KeyError):
        result['gaps'].append('个股5分钟证据或所属行业未核实')
        return result
    codes = sorted(set(peers)-{code})
    if _peer_returns is None:
        returns = {}
        for peer in codes:
            rows = normalize_bars(minute_rows.get(peer,[]),cutoff)
            part = window(rows,end,6)
            if part:
                returns[peer] = (part[-1]['close']/part[0]['close']-1)*100
    else:
        # Internal batch cache, computed from normalized six-bar windows at this
        # exact end time. Exclude self separately for every candidate.
        returns = {peer: _peer_returns[peer] for peer in codes if peer in _peer_returns}
    coverage = 100*len(returns)/len(codes) if codes else 0
    result.update(asOf=end.isoformat(),peerCount=len(returns),totalPeers=len(codes),peerReturns=returns)
    metrics = result['metrics']
    metrics.update(sector_coverage_pct=coverage,sector_peer_count=len(returns))
    if len(returns) < 3 or coverage < 80:
        result['gaps'].append(f'同刻行业成分覆盖不足（{len(returns)}/{len(codes)}），要求至少3只且覆盖≥80%')
        return result
    average = mean(returns.values())
    metrics.update(sector_return_5m_pct=average, sector_relative_5m_pct=speed-average,
                   sector_breadth=100*sum(value>0 for value in returns.values())/len(returns),
                   sector_rank_5m=1+sum(value>speed for value in returns.values()))
    result['confirmed'] = average>0 and metrics['sector_breadth']>=50 and speed>average
    return result


def prepare_sector_membership(codes, cutoff, cache_dir, *, getter=None, timeout=20, snapshot=None):
    deadline = monotonic() + max(0, timeout)
    def bounded_get(url, **kwargs):
        remaining = deadline-monotonic()
        if remaining <= 0:
            raise requests.Timeout('行业成员采集达到时间预算')
        kwargs['timeout'] = min(float(kwargs.get('timeout',4)),remaining)
        # Some transports ignore or exceed socket timeouts (DNS, patched
        # clients). Bound each uncached request, while preserving the cached
        # memberships already collected by the outer discovery operation.
        responses, _ = bounded_fetch_map(lambda _: (getter or public_get)(url, **kwargs),
                                         [None], workers=1, timeout=remaining)
        if not responses:
            raise requests.Timeout('行业成员请求未在截止时间完成')
        return responses[0]
    # One light unfiltered catalogue replaces hundreds of per-board requests.
    # `snapshot` is accepted from the caller but its upstream screening scope is
    # not proven complete, so it must not define an industry denominator.
    membership, peers = {}, {}
    bulk_deadline = min(deadline, monotonic()+min(4., max(0, timeout)*.5))
    def bulk_get(url, **kwargs):
        remaining = bulk_deadline-monotonic()
        if remaining <= 0:
            raise requests.Timeout('行业全市场目录采集达到时间预算')
        kwargs['timeout'] = min(float(kwargs.get('timeout',4)), remaining)
        responses, _ = bounded_fetch_map(lambda _: (getter or public_get)(url, **kwargs),
                                         [None], workers=1, timeout=remaining)
        if not responses:
            raise requests.Timeout('行业全市场目录请求未在截止时间完成')
        return responses[0]
    try:
        membership, peers = discover_bulk_sectors(codes,cutoff,cache_dir,getter=bulk_get)
    except Exception:
        pass
    missing = [code for code in codes if not membership.get(code)]
    if missing:
        try:
            extra_members, extra_peers = discover_sectors(missing,cutoff,cache_dir,getter=bounded_get,deadline=deadline)
            membership.update({code:value for code,value in extra_members.items() if value})
            peers.update(extra_peers)
        except Exception:
            pass
    missing = [code for code in codes if not membership.get(code)]
    if missing:
        try:
            # Primary discovery may return only its completed subset without
            # raising. Complete those missing identities from the independent
            # catalogue, preserving each provider's board and constituents.
            extra_members, extra_peers = discover_sina_sectors(
                missing,cutoff,cache_dir,getter=bounded_get,deadline=deadline)
            membership.update({code: value for code,value in extra_members.items() if value})
            peers.update(extra_peers)
        except Exception:
            pass
    # Every network request and worker queue already has the same deadline.
    # An outer timeout used to discard the useful cached/partial return value.
    if not any(membership.values()):
        raise TimeoutError('行业成员读取失败或达到时间预算，未生成行业证据')
    return membership, peers


def fetch_sector_batch(codes, intraday, cutoff, cache_dir, *, getter=None, bar_fetcher=None,
                       prepared=None, timeout=20, deadline=None, on_result=None):
    """Collect shared peers and publish independently usable candidate packets.

    The caller may retain ``on_result(code, packet)`` snapshots if its own outer
    deadline expires. Peer I/O stops before that deadline so completed evidence
    can be computed and returned; an unfinished industry never changes another
    industry's denominator or makes an incomplete window pass.
    """
    codes = list(dict.fromkeys(codes))
    started = monotonic()
    deadline = min(deadline, started+max(0,timeout)) if deadline is not None else started+max(0,timeout)
    return_reserve = min(2., max(0, deadline-started)*.2)
    collection_deadline = deadline-return_reserve
    try:
        membership, boards = prepared or prepare_sector_membership(codes,cutoff,cache_dir,getter=getter,
                                                                   timeout=max(0,collection_deadline-monotonic()))
    except Exception as exc:
        results = {code:{'metrics':{}, 'reasonCodes':['sector_membership_unavailable'],
                         'requestedCutoffAt':_datetime(cutoff).isoformat(),
                         'gaps':['板块成员源读取失败：'+type(exc).__name__]} for code in codes}
        if on_result:
            for code, packet in results.items():
                on_result(code, packet)
        return results
    archive = MinuteArchive(Path(cache_dir)/'minute_history') if bar_fetcher is None else None
    requested_ends = {}
    selected_boards = {}
    for code in codes:
        try:
            end = _datetime(intraday[code]['asOf'])
            if not 0 <= (_datetime(cutoff)-end).total_seconds() < 300:
                continue
            if number((intraday[code].get('metrics') or {}).get('speed_5m_pct')) is None:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        board = membership.get(code,{}).get('code')
        selected_boards[board] = boards.get(board, [])
        for peer in selected_boards[board]:
            requested_ends.setdefault(peer, set()).add(end)
    # Stable industry order is only a local-read tie breaker. Network service
    # rotates by prior collection debt, concentrating on completion below.
    ordered_boards = sorted(selected_boards, key=lambda board: (len(selected_boards[board]), str(board)))
    all_codes = list(dict.fromkeys(peer for board in ordered_boards
                                  for peer in sorted(set(selected_boards[board]))))
    def has_required_windows(code, normalized):
        ends = requested_ends.get(code) or {_datetime(cutoff)}
        return all(window(normalized, end, 6) for end in ends)
    rows, normalized_rows = {}, {}
    if archive:
        cache_scope = str(Path(cache_dir).resolve())
        with _recent_sector_lock:
            memory_rows = {code:_recent_sector_bars.get((cache_scope, code)) for code in all_codes}
        # Reuse all exact-window memory hits before the bounded SQLite queue.
        # A slow disk read must not hide a later industry's already held bars.
        for code, saved in memory_rows.items():
            if saved and 0 <= monotonic()-saved[0] < 300:
                normalized = normalize_bars(saved[1], cutoff)
                if has_required_windows(code, normalized):
                    rows[code], normalized_rows[code] = saved[1], normalized
    memory_hits = len(rows)
    def cached_minutes(code):
        try:
            return code, archive.read_recent(code, cutoff, minutes=20)
        except Exception:
            return code, []
    # Separate local reads from network calls, so a few slow providers cannot
    # prevent later candidates from using already archived peer evidence.
    # Candidate minutes have just been collected by this scan. Read these
    # shared industry constituents first, before the bounded local-read budget
    # is consumed by thousands of peers which may have no local session yet.
    cache_order = list(dict.fromkeys([code for code in codes if code in requested_ends]+all_codes))
    cached, _ = bounded_fetch_map(cached_minutes, [code for code in cache_order if code not in rows] if archive else [], workers=8,
        timeout=min(2., max(0,collection_deadline-monotonic())*.25))
    rows.update(cached)
    normalized_rows.update({code:normalize_bars(bars, cutoff) for code,bars in cached})
    missing = {code for code in all_codes if not has_required_windows(code, normalized_rows.get(code, {}))}
    errors = {}
    priorities = []
    required_by_board = {}
    for board in ordered_boards:
        peers = set(selected_boards[board])
        absent = sorted(peers.intersection(missing))
        available = len(peers)-len(absent)
        # Include one extra constituent for each candidate's self exclusion.
        # This is only an I/O priority; compute_sector_evidence still evaluates
        # the full denominator and exact window separately for every stock.
        required = min(len(peers), max(4, ceil(.8*max(0, len(peers)-1))+1))
        required_by_board[str(board)] = required
        needed = max(0, required-available)
        priorities.append((needed, len(absent), str(board), absent))
    fair_order, _, industry_order = sector_collection_plan(priorities, cache_dir)
    primary_by_board = {board:absent for _, _, board, absent in priorities}

    # A 170-stock scan can share thousands of constituents. Normalize each
    # accepted payload once and invalidate only the affected industry windows;
    # rebuilding every peer and all stock packets for each completion used a
    # material part of the network budget even when no request was necessary.
    shared_windows = {}
    peer_boards = {}
    for board, peers in selected_boards.items():
        for peer in peers:
            peer_boards.setdefault(peer, set()).add(board)

    def publish(unfinished, phase, only_boards=None):
        results = {}
        for code in codes:
            sector = membership.get(code, {})
            board = sector.get('code')
            if only_boards is not None and str(board) not in only_boards:
                continue
            peers = set(boards.get(board, [])) - {code}
            try:
                end = _datetime(intraday[code]['asOf'])
            except (KeyError, ValueError, TypeError):
                end = None
            key = (board, end)
            if key not in shared_windows:
                returns, sources = {}, {}
                if end is not None:
                    for peer in boards.get(board, []):
                        part = window(normalized_rows.get(peer, {}), end, 6)
                        if part:
                            returns[peer] = (part[-1]['close']/part[0]['close']-1)*100
                            sources[peer] = {str(row.get('source') or 'unknown') for row in part}
                shared_windows[key] = returns, sources
            returns, sources = shared_windows[key]
            result = compute_sector_evidence(code, intraday.get(code, {}), sector,
                boards.get(board, []), {}, cutoff, _peer_returns=returns)
            unfinished_peers = len(peers.intersection(unfinished))
            failed_peers = {peer: errors[peer] for peer in peers if peer in errors}
            result.update(requestedCutoffAt=_datetime(cutoff).isoformat(),
                sourceIds=sorted({source for peer in peers for source in sources.get(peer, set())}),
                collectionPhase=phase,
                fetchBudget={'seconds':timeout, 'returnReserveSeconds':return_reserve,
                    'priority':'completion_first_rotating_industries_exact_window_cache',
                    'industryOrder':industry_order,
                    'maximumWorkers':8, 'maximumIndustryWorkers':4 if len(industry_order)>1 else 8,
                    'memoryCacheUsablePeers':memory_hits,
                    'unfinishedPeers':unfinished_peers, 'failedPeers':len(failed_peers)})
            result['reasonCodes'] = []
            if not sector:
                result['reasonCodes'].append('sector_membership_unavailable')
            if unfinished_peers:
                result['reasonCodes'].append('sector_peer_collection_pending' if phase in ('cache_snapshot', 'industry_snapshot')
                                             else 'sector_peer_deadline_exhausted')
                result['gaps'].append(f'行业分钟采集尚有{unfinished_peers}只成分未返回；完整成分分母与80%覆盖门槛保持不变')
                if phase not in ('cache_snapshot', 'industry_snapshot'):
                    result['gaps'].append('行业分钟采集达到时间预算，已保留完成部分')
            if failed_peers:
                result['reasonCodes'].append('sector_peer_source_failed')
                result['peerFetchErrors'] = failed_peers
            if result.get('peerCount', 0) < 3 or result.get('metrics', {}).get('sector_coverage_pct', 0) < 80:
                result['reasonCodes'].append('sector_peer_coverage_insufficient')
            results[code] = result
            if on_result:
                on_result(code, result)
        return results

    if not missing:
        return publish(set(), 'complete')
    # Publish cached results before any network wait. Packets are replaced, not
    # mutated, so a caller can safely freeze its latest completed snapshot.
    publish(missing, 'cache_snapshot')

    def get_minutes(code, *, supplement=False):
        try:
            if monotonic() >= collection_deadline:
                raise TimeoutError('行业分钟采集达到时间预算')
            if bar_fetcher is not None:
                bars = bar_fetcher(code,count=20)
            else:
                if supplement:
                    bars = fetch_recent_bars(code,count=20,cutoff=cutoff,deadline=collection_deadline)
                else:
                    # A missing primary window must not make this worker wait
                    # through a slow secondary host before other constituents
                    # receive their first request. Preserve its actual partial
                    # bars, then supplement in the same bounded pool after the
                    # primary pass. The full peer denominator never changes.
                    bars = fetch_software_bars(code, count=20,
                        timeout=max(.1, min(4., collection_deadline-monotonic())))
            if bar_fetcher is None:
                # Route fresh peers through the same quarantine rules as stock
                # evidence. Unverified is observable; tainted sessions are not.
                archive.save(code,bars,cutoff)
                bars = archive.read_recent(code,cutoff,minutes=20)
                with _recent_sector_lock:
                    if len(_recent_sector_bars) >= 6000:
                        _recent_sector_bars.clear()
                    _recent_sector_bars[(str(Path(cache_dir).resolve()), code)] = (monotonic(),bars)
            return code, bars, None if bars else 'empty_source'
        except Exception as exc:
            return code, [], type(exc).__name__
    attempted = set()
    unfinished = set(missing)

    # One shared pool bounds all outstanding requests, including slow requests
    # retained from a previous industry. Consume individual completions instead
    # of waiting for an entire quota batch: a free worker can fetch a substitute
    # while one constituent is still pending, without a new timeout threshold.
    pool = ThreadPoolExecutor(max_workers=8)
    pending = {}
    usable_codes = set(all_codes)-missing
    peers_by_board = {str(board):peers for board,peers in selected_boards.items()}
    maximum_per_industry = 4 if len(industry_order)>1 else 8

    def has_capacity(board):
        return len(set(pending.values()).intersection(peers_by_board[board])) < maximum_per_industry

    def ready_boards():
        return {board for board, peers in peers_by_board.items()
                if len(set(peers).intersection(usable_codes)) >= required_by_board[board]}

    published_ready = ready_boards()

    def receive(*, block=False):
        nonlocal published_ready
        done = {future for future in pending if future.done()}
        remaining = collection_deadline-monotonic()
        if block and not done and pending and remaining > 0:
            done, _ = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
        for future in done:
            code = pending.pop(future)
            try:
                _, bars, error = future.result()
            except Exception as exc:
                bars, error = [], type(exc).__name__
            # A failed refresh cannot erase an already valid earlier window.
            if bars:
                rows[code] = bars
                normalized_rows[code] = normalize_bars(bars, cutoff)
                changed_boards = peer_boards.get(code, set())
                for key in [key for key in shared_windows if key[0] in changed_boards]:
                    shared_windows.pop(key, None)
            unfinished.discard(code)
            if has_required_windows(code, normalized_rows.get(code, {})):
                usable_codes.add(code)
            if error:
                errors[code] = error
            elif code in usable_codes:
                errors.pop(code, None)
        complete = ready_boards()
        if complete-published_ready:
            publish(unfinished, 'industry_snapshot', only_boards=complete-published_ready)
            published_ready = complete

    def submit_primary():
        # Fill an industry's complete quota before spending its remaining
        # 20%, but reserve capacity for another industry if this one is slow.
        # The full denominator and exact-window checks above are unchanged.
        while len(pending) < 8:
            if monotonic() >= collection_deadline:
                break
            complete = ready_boards()
            board_order = [board for board in industry_order if board not in complete]
            board_order += [board for board in industry_order if board in complete]
            code = None
            for board in board_order:
                if not has_capacity(board):
                    continue
                code = next((peer for peer in primary_by_board[board] if peer not in attempted), None)
                if code is not None:
                    break
            if code is None:
                break
            attempted.add(code)
            pending[pool.submit(get_minutes, code)] = code

    try:
        # Fairness operates across focused industry service turns, rather than
        # sprinkling a few peers over dozens of incomplete industries.
        while monotonic() < collection_deadline:
            receive()
            submit_primary()
            if not pending:
                break
            receive(block=True)
        # Source fallback uses only remaining capacity, after every missing
        # peer has had its primary request. It remains inside the same fixed
        # worker/deadline budget; no slow or unavailable peer is excluded from
        # coverage, and a failed supplement cannot erase accepted bars.
        if bar_fetcher is None:
            supplements = [peer for peer in fair_order if peer not in usable_codes]
            supplements = list(dict.fromkeys(supplements))
            while supplements and monotonic() < collection_deadline:
                receive()
                for code in list(supplements):
                    if len(pending) >= 8 or monotonic() >= collection_deadline:
                        break
                    if not any(has_capacity(str(board)) for board in peer_boards.get(code, ())):
                        continue
                    unfinished.add(code)
                    pending[pool.submit(get_minutes, code, supplement=True)] = code
                    supplements.remove(code)
                if not pending:
                    break
                receive(block=True)
            while pending and monotonic() < collection_deadline:
                receive(block=True)
    finally:
        # Late providers may finish their cache writes but cannot mutate rows,
        # emitted snapshots or the final packet after this function returns.
        pool.shutdown(wait=False, cancel_futures=True)
    record_sector_collection(cache_dir, selected_boards, attempted, usable_codes, required_by_board, cutoff)
    return publish(unfinished, 'deadline_partial' if unfinished else 'complete')


def compute_fund_evidence(code, data, cutoff):
    result = {'metrics':{},'source':'Eastmoney分钟资金流（供应商大单分类估算）','asOf':None,'gaps':[],
              'basis':'主力=大单+超大单；供应商买卖方向及订单大小分类，不等于机构账户真实流入',
              'flowKind':'vendor_estimate'}
    if str(data.get('code')) != code:
        result['gaps'].append('分钟资金流代码不匹配')
        return result
    cutoff = _datetime(cutoff)
    rows, conflicting = {}, set()
    for raw in data.get('klines',[]):
        fields = raw.split(',')
        try:
            at = _datetime(fields[0])
            values = [number(v) for v in fields[1:6]]
            if len(values)!=5 or any(v is None for v in values) or at > cutoff or at.date()!=cutoff.date():
                continue
            # Field order: main, small, medium, large, extra-large; reject schema drift.
            if abs(values[0]-values[3]-values[4])>max(2,abs(values[0])*1e-5):
                continue
            if at in rows and rows[at] != values:
                conflicting.add(at)
            rows[at] = values
        except (ValueError,TypeError):
            continue
    for at in conflicting:
        rows.pop(at,None)
    if not rows or (cutoff-max(rows)).total_seconds()>=300:
        result['gaps'].append('未取得当日新鲜且口径一致的分钟资金流')
        return result
    end = max(rows)
    result['asOf'] = end.isoformat()
    result['metrics']['main_net_flow'] = rows[end][0]
    stamps = [end-timedelta(minutes=n) for n in (3,2,1,0)]
    from .minute_history import bar_session
    if bar_session(end) is not None and all(at in rows and bar_session(at)==bar_session(end) for at in stamps):
        result['metrics']['main_net_flow_3m'] = rows[end][0]-rows[stamps[0]][0]
        increments = [rows[right][0]-rows[left][0] for left, right in zip(stamps, stamps[1:])]
        result['metrics']['main_net_flow_1m_deltas'] = increments
        result['metrics']['main_net_positive_fraction_3m'] = sum(value > 0 for value in increments)/3
    else:
        result['gaps'].append('资金流缺连续3分钟累计观测，未用当日总量替代')
    return result


def compute_tencent_fund_evidence(code, payload, cutoff):
    """Keep one provider's cumulative series intact and validate its identities."""
    symbol = ('sh' if code.startswith('6') else 'sz')+code
    if not isinstance(payload, dict) or payload.get('code') != 0:
        raise ValueError('Tencent fund response unavailable')
    data = payload.get('data')
    if not isinstance(data, dict):
        raise ValueError('Tencent fund response schema unavailable')
    trend = data.get('todayFundTrend')
    if not isinstance(trend, dict) or trend.get('stockCode') != symbol:
        raise ValueError('Tencent fund response identity mismatch')
    current = data.get('todayFundFlow')
    if isinstance(current, dict) and current.get('stockCode') != symbol:
        raise ValueError('Tencent fund current response identity mismatch')
    if not isinstance(trend.get('minList'), list):
        raise ValueError('Tencent fund minute series unavailable')
    klines = []
    from .minute_history import bar_session
    for row in trend['minList']:
        if not isinstance(row, dict):
            continue
        try:
            stamp = str(row.get('time') or '')
            if not re.fullmatch(r'\d{12}', stamp):
                continue
            # Tencent labels the START of each minute bucket: the current
            # label's cumulative amounts continue changing before the next
            # boundary. Normalize to completed bar END before cutoff/session
            # checks; the duplicated 11:30/15:00 chart endpoints fall outside
            # the sessions after this conversion and must not become bars.
            at = datetime.strptime(stamp, '%Y%m%d%H%M').replace(tzinfo=SHANGHAI)+timedelta(minutes=1)
            if bar_session(at) is None:
                continue
            values = [number(row.get(key)) for key in ('MainNetInflow', 'SmallNetInflow',
                'NormalNetInflow', 'BigNetInflow', 'SuperNetInflow', 'MainInflow', 'MainOutflow')]
            if any(value is None for value in values):
                continue
            main, small, normal, big, super_, incoming, outgoing = values
            # Vendor rounds separate yuan fields; allow at most two yuan,
            # never a percentage tolerance that grows with the cash balance.
            if incoming < 0 or outgoing < 0 or abs(main-big-super_) > 2 or abs(main-incoming+outgoing) > 2:
                continue
            klines.append(','.join([at.isoformat(), *map(str, (main, small, normal, big, super_))]))
        except (ValueError, TypeError):
            continue
    result = compute_fund_evidence(code, {'code':code, 'klines':klines}, cutoff)
    result.update({'source':'Tencent分钟资金流（供应商大单分类估算）', 'provider':'tencent',
        'providerHost':TENCENT_FUND_URL, 'basis':TENCENT_FUND_BASIS,
        'measurementVersion':'tencent-main-super-big-cumulative-bar-end-v2', 'amountUnit':'CNY',
        'flowKind':'vendor_estimate', 'rawTimeSemantics':'minute_start',
        'normalizedTimeSemantics':'bar_end', 'timeNormalization':'raw_start_plus_one_minute',
        'rawSourceAsOf':(_datetime(result['asOf'])-timedelta(minutes=1)).isoformat() if result['asOf'] else None})
    return result


def _tencent_fund_evidence(code, cutoff, deadline):
    response = _fund_request(TENCENT_FUND_URL,
        params={'code':('sh' if code.startswith('6') else 'sz')+code,
                'type':'todayFundTrend,todayFundFlow'}, getter=None, deadline=deadline)
    response.raise_for_status()
    response.encoding = 'utf-8'
    return compute_tencent_fund_evidence(code, response.json(), cutoff)


def fetch_fund_evidence(code, cutoff, *, getter=None, deadline=None):
    # The currently verified public Tencent series goes first, so three broken
    # Eastmoney hosts cannot consume the fallback's entire existing budget.
    # Injected getters retain the Eastmoney test/client contract. A fallback is
    # a complete independent provider packet; rows are never spliced together.
    shared_deadline = deadline if deadline is not None else monotonic()+12
    tencent_gap = None
    if getter is None and monotonic() < shared_deadline:
        try:
            tencent = _tencent_fund_evidence(code, cutoff, shared_deadline)
            if 'main_net_flow_3m' in tencent['metrics']:
                return tencent
            tencent_gap = tencent
        except (requests.RequestException, ValueError, TypeError):
            pass
    try:
        data, host = _fund_data(code, getter=getter, deadline=shared_deadline)
        result = compute_fund_evidence(code,data,cutoff)
        result['providerHost'] = host
        result['provider'] = 'eastmoney'
        result['measurementVersion'] = 'eastmoney-main-large-extra-large-cumulative-v1'
        result['amountUnit'] = 'CNY'
        if 'main_net_flow_3m' not in result['metrics'] and tencent_gap is not None:
            return tencent_gap
        return result
    except Exception as exc:
        if tencent_gap is not None:
            return tencent_gap
        # Preserve our own bounded provider diagnostics without leaking arbitrary
        # exception messages, request credentials or environment proxy details.
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith('Eastmoney sources unavailable: ') else type(exc).__name__
        return {'metrics':{}, 'source':'Eastmoney分钟资金流（供应商大单分类估算）',
                'asOf':None, 'flowKind':'vendor_estimate', 'status':'unavailable',
                'reasonCodes':['fund_deadline_exhausted' if deadline is not None and monotonic()>=deadline else 'fund_provider_failed'],
                'reason':reason, 'gaps':['分钟资金源不可用：'+reason]}
