"""Independent sector breadth and provider-defined fund evidence.

Sector breadth is computed from all available constituents at the SAME bar end,
excluding the candidate itself. It is never replaced with daily advancing counts.
Fund flows are vendor estimates, not verified institutional account movements.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
from statistics import mean
from uuid import uuid4

import requests
from src.patches.eastmoney_patch import original_request

from .intraday_evidence import _datetime, SHANGHAI
from .minute_history import fetch_sina_bars, normalize_bars, window, number

BASE = 'https://push2.eastmoney.com/api/qt/'
HEADERS = {'User-Agent':'Mozilla/5.0','Referer':'https://quote.eastmoney.com/'}


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
            response = (getter or public_get)(base+endpoint,params=params,timeout=4)
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
        if data['fetchedAt'][:10] == cutoff.date().isoformat() and data['value']:
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


def discover_sectors(codes, cutoff, cache_dir, *, getter=None):
    directory = Path(cache_dir)/'sector-membership'
    industries = cached_metadata(directory,'industry-list',cutoff,lambda:all_members('m:90+t:2',getter))
    industry_map = {row['code']:row['name'] for row in industries}
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
    with ThreadPoolExecutor(max_workers=4) as pool:
        membership = dict(pool.map(lookup,codes))
    selected = {row['code']:row for row in membership.values() if row}
    def constituents(board):
        try:
            rows = cached_metadata(directory,'board-'+board,cutoff,lambda:all_members('b:'+board,getter))
            codes = [row['code'] for row in rows if re.fullmatch(r'(?:60|68|00|30)\d{4}',row['code'])]
            return board, codes
        except Exception:
            return board, []
    with ThreadPoolExecutor(max_workers=4) as pool:
        peers = dict(pool.map(constituents,selected))
    return membership, peers


def discover_sina_sectors(codes, cutoff, cache_dir, *, getter=None):
    """Independent industry fallback; caches the complete classification map."""
    get = getter or public_get
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
        with ThreadPoolExecutor(max_workers=4) as pool:
            return [row for row in pool.map(members,sorted(catalogue.items())) if row]
    all_boards=cached_metadata(Path(cache_dir)/'sector-membership','sina-industries',cutoff,fetch)
    membership={code:next(({k:v for k,v in board.items() if k!='members'} for board in all_boards if code in board['members']),{}) for code in codes}
    selected={row.get('code') for row in membership.values()}
    return membership,{board['code']:board['members'] for board in all_boards if board['code'] in selected}


def compute_sector_evidence(code, stock_minutes, sector, peers, minute_rows, cutoff):
    provider=sector.get('provider','Eastmoney') if sector else 'unknown'
    result = {'metrics':{},'source':provider+'行业成分 + Sina同刻1分钟K线', 'asOf':None,
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
    returns = {}
    for peer in codes:
        rows = normalize_bars(minute_rows.get(peer,[]),cutoff)
        part = window(rows,end,6)
        if part:
            returns[peer] = (part[-1]['close']/part[0]['close']-1)*100
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


def prepare_sector_membership(codes, cutoff, cache_dir, *, getter=None):
    try:
        return discover_sectors(codes,cutoff,cache_dir,getter=getter)
    except Exception:
        return discover_sina_sectors(codes,cutoff,cache_dir,getter=getter)


def fetch_sector_batch(codes, intraday, cutoff, cache_dir, *, getter=None, bar_fetcher=None, prepared=None):
    try:
        membership, boards = prepared or prepare_sector_membership(codes,cutoff,cache_dir,getter=getter)
    except Exception as exc:
        return {code:{'metrics':{},'gaps':['板块成员源读取失败：'+type(exc).__name__]} for code in codes}
    all_codes = sorted({code for members in boards.values() for code in members})
    def get_minutes(code):
        try:
            return code, (bar_fetcher or fetch_sina_bars)(code,count=20)
        except Exception:
            return code, []
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = dict(pool.map(get_minutes,all_codes))
    return {code:compute_sector_evidence(code,intraday.get(code,{}),membership.get(code,{}),
                boards.get(membership.get(code,{}).get('code'),[]),rows,cutoff) for code in codes}


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


def fetch_fund_evidence(code, cutoff, *, getter=None):
    try:
        data = get_data('stock/fflow/kline/get',{'secid':('1.' if code.startswith('6') else '0.')+code,
            'lmt':'0','klt':'1','fields1':'f1,f2,f3,f7','fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63'},getter)
        return compute_fund_evidence(code,data,cutoff)
    except Exception as exc:
        return {'metrics':{},'gaps':['分钟资金源不可用：'+type(exc).__name__]}
