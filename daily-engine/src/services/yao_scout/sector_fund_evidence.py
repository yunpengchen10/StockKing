"""Independent sector breadth and provider-defined fund evidence.

Sector breadth is computed from all available constituents at the SAME bar end,
excluding the candidate itself. It is never replaced with daily advancing counts.
Fund flows are vendor estimates, not verified institutional account movements.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil
import json
import os
from pathlib import Path
import re
from statistics import mean
from threading import Lock
from time import monotonic
from uuid import uuid4

import requests
from src.patches.eastmoney_patch import original_request

from .intraday_evidence import _datetime, SHANGHAI
from .minute_history import (MinuteArchive, fetch_recent_bars, fetch_software_bars,
                             normalize_bars, window, number, bounded_fetch_map)

BASE = 'https://push2.eastmoney.com/api/qt/'
HEADERS = {'User-Agent':'Mozilla/5.0','Referer':'https://quote.eastmoney.com/'}
_recent_sector_bars = {}
_recent_sector_lock = Lock()


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


def prepare_sector_membership(codes, cutoff, cache_dir, *, getter=None, timeout=20):
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
    try:
        membership, peers = discover_sectors(codes,cutoff,cache_dir,getter=bounded_get,deadline=deadline)
    except Exception:
        membership, peers = {}, {}
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
    # Start local reads with small industries, deduplicating overlapping peers.
    # Spreading a short collection budget over every industry can leave all of
    # them below the unchanged 80% threshold. Network priority is refined using
    # actual cached coverage below, without looking at any peer's return.
    ordered_boards = sorted(selected_boards, key=lambda board: (len(selected_boards[board]), str(board)))
    all_codes = list(dict.fromkeys(peer for board in ordered_boards
                                  for peer in sorted(set(selected_boards[board]))))
    def sufficient(code, bars):
        normalized = normalize_bars(bars, cutoff)
        ends = requested_ends.get(code) or {_datetime(cutoff)}
        return all(window(normalized, end, 6) for end in ends)
    def cached_minutes(code):
        try:
            with _recent_sector_lock:
                saved = _recent_sector_bars.get(code)
            if saved and 0 <= monotonic()-saved[0] < 10 and sufficient(code, saved[1]):
                return code, saved[1]
            return code, archive.read_recent(code, cutoff, minutes=20)
        except Exception:
            return code, []
    # Separate local reads from network calls, so a few slow providers cannot
    # prevent later candidates from using already archived peer evidence.
    cached, _ = bounded_fetch_map(cached_minutes, all_codes if archive else [], workers=8,
        timeout=min(2., max(0,collection_deadline-monotonic())*.25))
    rows = dict(cached)
    missing = {code for code in all_codes if not sufficient(code, rows.get(code, []))}
    errors = {}
    targets, remainder = [], []
    priorities = []
    for board in ordered_boards:
        peers = set(selected_boards[board])
        absent = sorted(peers.intersection(missing))
        available = len(peers)-len(absent)
        # Include one extra constituent for each candidate's self exclusion.
        # This is only an I/O priority; compute_sector_evidence still evaluates
        # the full denominator and exact window separately for every stock.
        required = min(len(peers), max(4, ceil(.8*max(0, len(peers)-1))+1))
        needed = max(0, required-available)
        priorities.append((needed, len(absent), str(board), absent))
    for needed, _, _, absent in sorted(priorities):
        targets.extend(absent[:needed])
        remainder.extend(absent[needed:])
    fetch_order = list(dict.fromkeys(targets+remainder))

    def publish(unfinished, phase):
        normalized = {peer: normalize_bars(part, cutoff) for peer, part in rows.items()}
        shared_windows, results = {}, {}
        for code in codes:
            sector = membership.get(code, {})
            board = sector.get('code')
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
                        part = window(normalized.get(peer, {}), end, 6)
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
                    'priority':'closest_to_full_denominator_coverage',
                    'unfinishedPeers':unfinished_peers, 'failedPeers':len(failed_peers)})
            result['reasonCodes'] = []
            if not sector:
                result['reasonCodes'].append('sector_membership_unavailable')
            if unfinished_peers:
                result['reasonCodes'].append('sector_peer_collection_pending' if phase == 'cache_snapshot'
                                             else 'sector_peer_deadline_exhausted')
                result['gaps'].append(f'行业分钟采集尚有{unfinished_peers}只成分未返回；完整成分分母与80%覆盖门槛保持不变')
                if phase != 'cache_snapshot':
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

    def get_minutes(code):
        try:
            if monotonic() >= collection_deadline:
                raise TimeoutError('行业分钟采集达到时间预算')
            if bar_fetcher is not None:
                bars = bar_fetcher(code,count=20)
            else:
                bars = []
                try:
                    software = fetch_software_bars(code, count=20,
                        timeout=max(.1, min(4., collection_deadline-monotonic())))
                    if sufficient(code, software):
                        bars = software
                except (requests.RequestException, RuntimeError, ValueError, TypeError, KeyError):
                    pass
                if not bars:
                    bars = fetch_recent_bars(code,count=20,cutoff=cutoff,deadline=collection_deadline)
            if bar_fetcher is None:
                # Route fresh peers through the same quarantine rules as stock
                # evidence. Unverified is observable; tainted sessions are not.
                archive.save(code,bars,cutoff)
                bars = archive.read_recent(code,cutoff,minutes=20)
                with _recent_sector_lock:
                    if len(_recent_sector_bars) >= 6000:
                        _recent_sector_bars.clear()
                    _recent_sector_bars[code] = (monotonic(),bars)
            return code, bars, None if bars else 'empty_source'
        except Exception as exc:
            return code, [], type(exc).__name__
    completed, unfinished = bounded_fetch_map(get_minutes,fetch_order,workers=8,
                                              timeout=max(0,collection_deadline-monotonic()))
    for code, bars, error in completed:
        # A failed refresh cannot erase an already valid earlier cutoff window.
        if bars:
            rows[code] = bars
        if error:
            errors[code] = error
    return publish(set(unfinished), 'deadline_partial' if unfinished else 'complete')


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
    if all(at in rows and bar_session(at)==bar_session(end) for at in stamps):
        result['metrics']['main_net_flow_3m'] = rows[end][0]-rows[stamps[0]][0]
    else:
        result['gaps'].append('资金流缺连续3分钟累计观测，未用当日总量替代')
    return result


def fetch_fund_evidence(code, cutoff, *, getter=None, deadline=None):
    def bounded_get(url, **kwargs):
        remaining = (deadline-monotonic()) if deadline is not None else 4
        if remaining <= 0:
            raise requests.Timeout('资金分钟采集达到时间预算')
        kwargs['timeout'] = min(float(kwargs.get('timeout',4)), remaining)
        return (getter or public_get)(url, **kwargs)
    try:
        data = get_data('stock/fflow/kline/get',{'secid':('1.' if code.startswith('6') else '0.')+code,
            'lmt':'0','klt':'1','fields1':'f1,f2,f3,f7','fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63'},bounded_get)
        return compute_fund_evidence(code,data,cutoff)
    except Exception as exc:
        # Preserve our own bounded provider diagnostics without leaking arbitrary
        # exception messages, request credentials or environment proxy details.
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith('Eastmoney sources unavailable: ') else type(exc).__name__
        return {'metrics':{}, 'source':'Eastmoney分钟资金流（供应商大单分类估算）',
                'asOf':None, 'flowKind':'vendor_estimate', 'status':'unavailable',
                'reasonCodes':['fund_deadline_exhausted' if deadline is not None and monotonic()>=deadline else 'fund_provider_failed'],
                'reason':reason, 'gaps':['分钟资金源不可用：'+reason]}
