"""Local-only T..T+5 selection. No language model is reachable from scans."""
from __future__ import annotations
from datetime import datetime, timedelta
from time import monotonic
import json
import os
import sqlite3
from uuid import uuid4
import pandas as pd
from src.services.yao_scout.daily_opportunities import DailyOpportunityService, mainboard, clean, validate_candidate, quote_check
from src.services.yao_scout.local_observation_review import review_local_observations, read_observation_reminders_batch
from src.services.yao_scout.recommendation_evidence import VERSION, research_queue, research_queue_limit, daily_evidence, explain_candidate, evidence_order
from src.services.yao_scout.v11_factors import SCORE_VERSION, score_v11
from src.services.yao_scout.minute_history import bounded_fetch_map
from src.services.yao_scout.local_algorithm import algorithm_contract, is_entry_eligible

class LocalOpportunityService(DailyOpportunityService):
    persistent_claims = True
    def __init__(self, yao, ai_config=None, *, minute_fetcher=None, sector_fetcher=None, fund_fetcher=None, context_provider=None, progress_callback=None, **kwargs):
        super().__init__(yao, None, **kwargs)
        self.minute_fetcher = minute_fetcher
        self.sector_fetcher, self.fund_fetcher = sector_fetcher, fund_fetcher
        self.context_provider = context_provider
        self.progress_callback = progress_callback

    def _progress(self, progress, message):
        if self.progress_callback:
            self.progress_callback(progress, message)

    def _observe_market_emotion(self, frame, received_at):
        from .research_factors import MarketEmotionArchive
        try:
            # Save what was genuinely received before slow per-stock research.
            # The scoring layer later rechecks its original source timestamp.
            return MarketEmotionArchive(self.yao.data_dir / 'market-emotion').observe(
                frame, received_at, received_at=received_at)
        except Exception as exc:
            return {'status': 'unavailable', 'score': None, 'asOf': None,
                    'features': {}, 'regime': 'unknown', 'source': 'market-emotion-archive',
                    'gaps': ['市场情绪归档不可用：' + type(exc).__name__],
                    'validationStatus': 'unvalidated_starting_points'}

    @staticmethod
    def _research_coverage(quality, frame, queued):
        universe_count = int(frame['code'].nunique())
        quality.update(research_count=len(queued), research_universe_count=universe_count,
                       deep_research_count=0,
                       research_limit=research_queue_limit(universe_count),
                       research_policy='mainboard_10pct_min300_v1',
                       research_coverage=(f'全主板初筛{universe_count}只；按主板池10%向上取整、至少300只'
                                          f'且不超过实际股票数，本轮{len(queued)}只进入深研；'
                                          '未深研股票不作已排除或无机会结论'))

    @staticmethod
    def _factor_availability(candidate, minute):
        """Distinguish observed factor values from missing inputs and directions."""
        availability = candidate.setdefault('factorAvailability', {})
        scores = candidate.get('factorScores') or {}
        for key in ('M','V','T','W','B','L'):
            observed = scores.get(key) is not None
            reason = None
            if not observed:
                reason = ('missing_verified_float_shares_or_interval_volume' if key=='T' else
                          'order_book_missing_stale_or_invalid' if key=='L' else
                          'minute_window_or_historical_baseline_missing')
            availability[key] = {'status':'observed' if observed else 'unavailable',
                'available':observed, 'sourceAsOf':(minute or {}).get('asOf'),
                'requestedCutoffAt':candidate.get('factorCutoffAt'),
                'reasonCodes':[reason] if reason else []}
        for key in ('C','G','E','H'):
            packet = (candidate.get('researchFactors') or {}).get(key) or {}
            observed = packet.get('status')=='observed' and packet.get('score') is not None
            reason = None if observed else {
                'C':'point_in_time_event_coverage_unavailable',
                'G':'completed_daily_history_or_unadjusted_price_basis_unavailable',
                'E':'whole_market_source_timestamp_or_history_unavailable',
                'H':'point_in_time_concept_membership_or_coverage_unavailable'}[key]
            availability[key] = {'status':packet.get('status','unavailable'), 'available':observed,
                'source':packet.get('source'), 'sourceAsOf':packet.get('asOf'),
                'reasonCodes':[reason] if reason else [], 'gaps':list(packet.get('gaps') or []),
                'includedInDecision':key!='H' and (candidate.get('v11Inputs') or {}).get('research_'+key+'_score') is not None}
        for key, packet in availability.items():
            packet['scoreAvailable'] = scores.get(key) is not None
            packet['missing_reason'] = (packet.get('reasonCodes') or [None])[0]

    def _verify_archived_quotes(self, codes, now, *, quotes=None, timeout=30, lineage_cutoff=None):
        """Automatically attach independent public prices to archived identities.

        A quote is evidence only for its own session and provider timestamp.
        Existing quarantines and absent identities are never repaired here.
        """
        from .minute_history import MinuteArchive
        directory = self.yao.data_dir/'minute_history'
        path = directory/'minute-bars-v2.sqlite3'
        if not path.exists():
            return {'status':'unavailable','reason':'minute_archive_missing','requested':0,
                    'verified':0,'unknown':0,'tainted':0,'deferred':0,'lineages':{}}
        deadline = monotonic()+max(0,timeout)
        day = now.date().isoformat()
        try:
            with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1) as db:
                archived = {row[0] for row in db.execute('SELECT code FROM sessions WHERE day=?',(day,))}
        except sqlite3.Error as error:
            return {'status':'unavailable','reason':'minute_archive_'+type(error).__name__,
                    'requested':len(codes),'verified':0,'unknown':0,'tainted':0,
                    'deferred':len(codes),'lineages':{}}
        requested = list(dict.fromkeys(code for code in codes if code in archived))
        if not requested:
            return {'status':'no_current_session','requested':0,'verified':0,'unknown':0,
                    'tainted':0,'deferred':0,'lineages':{}}
        if quotes is None:
            groups = [requested[start:start+20] for start in range(0,len(requested),20)]
            packets, _ = bounded_fetch_map(self._quotes,groups,workers=4,
                timeout=max(0,(deadline-monotonic())*.65))
            quotes = {code:quote for packet in packets for code,quote in packet.items()}
        archives, _ = bounded_fetch_map(lambda _:MinuteArchive(directory),[None],workers=1,
                                       timeout=max(0,deadline-monotonic()))
        if not archives:
            return {'status':'partial','reason':'independent_verification_deadline_exhausted',
                    'requested':len(requested),'verified':0,'unknown':0,'tainted':0,
                    'deferred':len(requested),'lineages':{}}
        archive = archives[0]
        def verify(code):
            if monotonic() >= deadline or not quotes.get(code):
                return code, None
            try:
                lineage = archive.verify_quote(code, quotes[code], now)
                if lineage_cutoff is not None:
                    lineage = archive.quality(code, lineage_cutoff, [day])
                return code, lineage
            except (OSError,ValueError,TypeError,KeyError,sqlite3.Error) as error:
                return code, {'status':'unknown','reasonCodes':['independent_quote_verification_'+type(error).__name__],
                              'sessions':[]}
        completed, missing = bounded_fetch_map(verify,requested,workers=4,timeout=max(0,deadline-monotonic()))
        lineages = {code:lineage for code,lineage in completed if lineage is not None}
        return {'status':'completed' if len(lineages)==len(requested) else 'partial',
                'requested':len(requested),'verified':sum(row.get('status')=='verified' for row in lineages.values()),
                'unknown':sum(row.get('status')=='unknown' for row in lineages.values()),
                'tainted':sum(row.get('status')=='tainted' for row in lineages.values()),
                'deferred':len(requested)-len(lineages), 'asOf':now.isoformat(),
                'reasonCounts':{reason:sum(reason in row.get('reasonCodes',[]) for row in lineages.values())
                    for reason in sorted({reason for row in lineages.values() for reason in row.get('reasonCodes',[])})},
                'meaning':'独立报价核验已存分钟身份与价格范围；不代表逐笔成交或未知旧日已补齐',
                'lineages':lineages}

    @staticmethod
    def _refresh_verified_lineage(original, current):
        from .minute_integrity import _lineage
        if not original or not original.get('sessions'):
            return original or {'status':'unknown','reasonCodes':['minute_lineage_unavailable']}
        updates = {(row['code'],row['day']):row for row in current.get('sessions',[])}
        sessions = []
        for old in original['sessions']:
            new = updates.get((old['code'],old['day']))
            if new and new.get('revision') != old.get('revision'):
                # Recomputing metadata must never pretend old factor values
                # were calculated from minutes received only after scoring.
                old = {**old, 'status':'tainted', 'reasonCodes':['minute_revision_changed_after_factor_calculation']}
            elif new:
                old = new
            sessions.append(old)
        return _lineage(sessions)

    def _scan(self, run, quality, history, dlm, previous, now, slot, top_n):
        stage_started = monotonic()
        stage_name = 'snapshot'
        quality['stageTimingsMs'] = {}
        quality['stageIncompleteCounts'] = {}
        quality['stageBudgetsSeconds'] = {}
        quality['stageMissingReasons'] = {}
        target_at = run['delivery'].get('target_at')
        deadline_at = (datetime.fromisoformat(target_at) + timedelta(seconds=120)
                       if run.get('official') and target_at else self.clock() + timedelta(seconds=120))
        scan_deadline = monotonic() + max(0, (deadline_at-self.clock()).total_seconds())
        quality['collectionPolicy'] = {'mode': 'cached_history_and_incremental_minutes',
            'deadlineAt': deadline_at.isoformat(), 'historicalBackfillInScan': False,
            'quoteReserveSeconds': 15}
        def remaining(reserve=0):
            # Both clocks are bounded: wall time enforces delivery, monotonic
            # protects against clock adjustments during a provider request.
            return max(0, min(scan_deadline-monotonic(),
                              (deadline_at-self.clock()).total_seconds())-reserve)
        def budget_for(name, timeout, *, reserve=15):
            budget = min(timeout, remaining(reserve))
            quality['stageBudgetsSeconds'][name] = budget
            return budget
        def stage(name, progress, message):
            nonlocal stage_started, stage_name
            tick = monotonic()
            quality['stageTimingsMs'][stage_name] = round((tick-stage_started)*1000)
            stage_name, stage_started = name, tick
            self._progress(progress, message)
        def fetch_batch(name, fetch, items, timeout, *, deadline_aware=False, workers=4):
            items = list(items)
            # Universe coverage must not multiply delivery latency. Unfinished
            # symbols remain explicit missing observations, never false signals.
            budget = budget_for(name, timeout, reserve=0 if name=='final_quotes' else 15)
            deadline = monotonic()+budget
            operation = (lambda item: fetch(item, deadline)) if deadline_aware else fetch
            if deadline_aware:
                operation.__name__ = getattr(fetch, '__name__', 'fetch')
            results, unfinished = bounded_fetch_map(operation, items, workers=workers, timeout=budget)
            quality['stageIncompleteCounts'][name] = len(unfinished)
            if unfinished:
                quality['stageMissingReasons'][name] = ('scan_deadline_exhausted' if budget<=0
                    else 'provider_failed_or_stage_deadline_exhausted')
            return results
        self._progress(12, '正在读取全市场行情')
        snapshots = fetch_batch('snapshot', lambda _: self.yao._fetch_snapshot(), [None], 25)
        if not snapshots:
            raise ValueError('全市场行情请求超时或失败，本轮保留历史结果')
        snapshot = snapshots[0]
        snapshot_received_at = self.clock()
        frame = mainboard(snapshot)
        quality.update(self.yao._snapshot_meta(snapshot, point_in_time_ok=False))
        quality.update(snapshot_count=len(snapshot), mainboard_count=len(frame), llm_calls=0)
        if frame.empty or snapshot.attrs.get('stale'):
            raise ValueError('全市场行情为空或陈旧，本轮保留历史结果')
        emotion = self._observe_market_emotion(frame, snapshot_received_at)
        # The former heat score is no longer a fallback recommendation engine.
        # A queue gives several forms of activity a chance to be researched;
        # independent price, structure and remaining-space evidence decides entry.
        queued = clean(research_queue(frame))
        self._research_coverage(quality, frame, queued)
        run.update(model_version=SCORE_VERSION, modelVersion=SCORE_VERSION, recommendationLogicVersion=SCORE_VERSION,
                   evidenceVersion=VERSION,
                   algorithm=algorithm_contract(), aiAudit=[], aiReviewStatus='not_requested', evidenceInsufficient=[])
        old = {p.get('code') for p in previous.get('candidates', [])}
        quality.update(model_universe_count=0,
                       selection_method='成交额/快照量比/换手/开盘修复各自名次并集安排深研；无涨幅权重总分',
                       chatgpt_alignment='V1.2按阶段评分；历史与事件风险单独准入，事件、股性和市场情绪逐项保存真实证据；参数尚未收益验证')
        from src.services.software_market import SoftwareMarketClient
        software_market = SoftwareMarketClient.from_environment()
        software_market.read_timeout = 12
        stage('daily_history', 20, f'已读取 {len(frame)} 只主板股票，正在核验 {len(queued)} 只日线证据')
        from .research_factors import character_factor, event_factor
        from .theme_evidence import theme_factor
        def history_for(item):
            cache = self.yao.data_dir/'daily_history'/'scan-cache-v1'/str(item['code'])
            saved = None
            try:
                payload = json.loads(cache.read_text(encoding='utf-8'))
                if payload.get('code') == item['code'] and datetime.fromisoformat(payload['observedAt']) <= now:
                    saved = pd.DataFrame(payload['bars'])
                    # A partial T bar saved at 10:00 must not become a completed
                    # previous-session close merely because the date advanced.
                    completed_before = datetime.fromisoformat(payload['observedAt']).date()
                    dates = pd.to_datetime(saved['date'], errors='coerce', utc=True).dt.tz_convert('Asia/Shanghai').dt.date
                    saved = saved.loc[dates < completed_before].copy()
                    saved.attrs.update(payload.get('attrs') or {})
            except (OSError, ValueError, KeyError, TypeError):
                pass
            cached_evidence = daily_evidence(saved, now)
            if cached_evidence.get('metrics'):
                cached_evidence['characterFactor'] = character_factor(saved, now,
                    price_basis=saved.attrs.get('daily_adjustment'))
                cached_evidence['collectionMode'] = 'cache'
                return item['code'], cached_evidence
            try:
                if software_market.available:
                    packet = software_market.bars(item['code'],period='101',count=10 if saved is not None else 260)
                    raw = pd.DataFrame(packet.get('bars') or [])
                    if not raw.empty:
                        raw['date'] = raw['end']
                        raw.attrs['daily_source'] = ('software:go/' + str(packet.get('source') or 'unknown')
                            + '; adjustment=' + str(packet.get('adjustment') or 'unknown'))
                        raw.attrs['daily_adjustment'] = packet.get('adjustment')
                        if saved is not None and saved.attrs.get('daily_adjustment') == raw.attrs.get('daily_adjustment'):
                            attrs = dict(raw.attrs)
                            raw = pd.concat([saved, raw], ignore_index=True).drop_duplicates('date', keep='last')
                            raw.attrs.update(attrs)
                else:
                    # Sina's daily endpoint provides unadjusted prices; adjusted
                    # historical highs cannot establish exchange limit touches.
                    raw = self.yao.history_fetcher(item['code'], lookback_days=400, source='sina', retries=1,
                        cache_dir=self.yao.data_dir / 'daily_history', cache_ttl_seconds=3600)
                    if raw is not None and raw.attrs.get('daily_source') == 'sina':
                        raw.attrs['daily_adjustment'] = 'unadjusted'
                if raw is not None and not raw.empty:
                    dates = pd.to_datetime(raw['date'], errors='coerce', utc=True).dt.tz_convert('Asia/Shanghai').dt.date
                    raw = raw.loc[dates < now.date()].copy()
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    temporary = cache.with_name(cache.name+'.'+uuid4().hex+'.tmp')
                    try:
                        temporary.write_text(json.dumps({'code': item['code'], 'observedAt': self.clock().isoformat(),
                            'attrs': dict(raw.attrs), 'bars': clean(raw.to_dict('records'))}, ensure_ascii=False, default=str),encoding='utf-8')
                        os.replace(temporary, cache)
                    finally:
                        temporary.unlink(missing_ok=True)
            except Exception:
                raw = None
            evidence = daily_evidence(raw, now)
            evidence['characterFactor'] = character_factor(raw, now,
                price_basis=raw.attrs.get('daily_adjustment') if raw is not None else None)
            evidence['collectionMode'] = 'incremental' if saved is not None else 'initial_daily_snapshot'
            return item['code'], evidence
        daily = dict(fetch_batch('daily_history', history_for, queued, 25))
        prepared_sectors = None
        stage('sector_membership', 32, '正在读取行业成分与行业证据')
        if not self.sector_fetcher:
            from .sector_fund_evidence import prepare_sector_membership
            try:
                prepared_sectors = prepare_sector_membership([row['code'] for row in queued],self.clock(),self.yao.data_dir,
                    timeout=budget_for('sector_membership', 10))
            except Exception:
                pass
        # Historical minute backfill belongs to review/archive maintenance.
        # Timed scans only read that cache and fetch the current incremental page.
        # Batch fundamentals/events while preparing; never age the final quote
        # with vendor calls. Archives record observedAt independently of reports.
        from src.services.akshare_context import AkshareContextProvider, for_stock
        from .precision_policy import evaluate as evaluate_precision
        stage('fundamental_context', 48, '正在核验财务、业绩预告与解禁信息')
        provider = self.context_provider or AkshareContextProvider(self.yao.data_dir / 'akshare-context', clock=self.clock, timeout=25)
        bundles = fetch_batch('fundamental_context', lambda _: provider.collect(self.clock()), [None], 25)
        context_bundle = bundles[0] if bundles else {'tables': [], 'status': 'unavailable',
            'reason': '外部背景数据超时，保留缺口，不视为已核验'}
        target_at = run['delivery'].get('target_at')
        if run.get('official') and target_at:
            # Leave a bounded preparation window before the desired delivery;
            # only the last quote request waits for the target itself.
            research_at = datetime.fromisoformat(target_at)-timedelta(seconds=90)
            while (wait_seconds := (research_at-self.clock()).total_seconds()) > 0:
                self.sleeper(min(30, wait_seconds))
        if run.get('official') and run['delivery'].get('target_at'):
            # Early work only prepares history. Each target scans the whole pool
            # again so a newly active stock is not confined to yesterday's list
            # or the ten-minutes-old preparation list.
            snapshots = fetch_batch('final_snapshot', lambda _: self.yao._fetch_snapshot(), [None], 25)
            if not snapshots:
                raise ValueError('目标时点全市场行情请求超时或失败，本轮保留历史结果')
            snapshot = snapshots[0]
            snapshot_received_at = self.clock()
            frame = mainboard(snapshot)
            if frame.empty or snapshot.attrs.get('stale'):
                raise ValueError('目标时点全市场复核行情不可用，未用提前准备名单冒充本轮筛选')
            emotion = self._observe_market_emotion(frame, snapshot_received_at)
            queued = clean(research_queue(frame))
            self._research_coverage(quality, frame, queued)
            quality.update(self.yao._snapshot_meta(snapshot, point_in_time_ok=False))
            quality.update(snapshot_count=len(snapshot), mainboard_count=len(frame))
            new_items = [item for item in queued if item['code'] not in daily]
            daily.update(dict(fetch_batch('new_daily_history', history_for, new_items, 25)))
            if new_items and not self.sector_fetcher:
                try:
                    new_sectors = prepare_sector_membership([row['code'] for row in new_items],self.clock(),self.yao.data_dir,
                        timeout=budget_for('new_sector_membership', 5))
                    if prepared_sectors:
                        prepared_sectors[0].update(new_sectors[0])
                        prepared_sectors[1].update(new_sectors[1])
                    else:
                        prepared_sectors = new_sectors
                except Exception:
                    pass
            quality.update(final_universe_refreshed=True, research_count=len(queued),
                           research_universe_count=len(frame), final_pool_checked_at=self.clock().isoformat(),
                           delivery_limit='目标前90秒启动全池复核；实际源时间、决策与延迟另记，不保证分钟数据秒级同步')
        # Archive the whole mainboard, never only the selected or rising stocks.
        # Reception time is an availability boundary, not a quote timestamp.
        run['marketEmotion'] = emotion
        codes = [row['code'] for row in queued]
        # Context tables, theme breadth and saved reminders can be large. Finish
        # this work BEFORE the final quotes, so it cannot consume their 30s TTL.
        stage('context_evaluation', 55, '正在整理事件、行业背景及历史复盘提醒')
        prepared_reminders = read_observation_reminders_batch(self.db, codes, self.clock())
        prepared_context = {}
        prepared_research = {}
        for code in codes:
            at = self.clock()
            prepared_context[code] = for_stock(context_bundle, code, at)
            prepared_research[code] = {
                'C': event_factor(prepared_context[code], at),
                'G': (daily.get(code) or {}).get('characterFactor', {}),
                'E': emotion,
                'H': theme_factor(frame, code, at, received_at=snapshot_received_at),
            }
        # Freeze the last completed-minute boundary once. Acquire the final
        # stock window BEFORE S/F so refreshing stock prices cannot invalidate
        # otherwise usable sector and fund observations from a different window.
        minute_cutoff = self.clock().replace(second=0, microsecond=0)
        quality['factorCutoffAt'] = minute_cutoff.isoformat()
        def minutes_for(item, deadline):
            try:
                from .minute_history import fetch_bar_evidence
                kwargs = {} if self.minute_fetcher else {'software_count':240, 'backfill':False, 'deadline':deadline}
                packet = (self.minute_fetcher or fetch_bar_evidence)(
                    item['code'], minute_cutoff, self.yao.data_dir/'minute_history', **kwargs)
                return item['code'], {**packet, 'requestedCutoffAt':minute_cutoff.isoformat()}
            except Exception as exc:
                return item['code'], {'metrics': {}, 'requestedCutoffAt':minute_cutoff.isoformat(),
                    'reasonCodes':['minute_provider_failed'],
                    'gaps':['分钟行情未取得（'+type(exc).__name__+'）；不以全天涨幅/快照量比代替即时信号']}
        stage('minute_evidence', 60, '正在刷新已完成分钟；历史缺口交由盘后归档补齐')
        intraday = dict(fetch_batch('minute_evidence', minutes_for, queued, 30, deadline_aware=True))
        quality['minute_refresh_count'] = len(intraday)
        for code in codes:
            intraday.setdefault(code, {'metrics': {}, 'requestedCutoffAt':minute_cutoff.isoformat(),
                'reasonCodes':[quality['stageMissingReasons'].get('minute_evidence','minute_provider_failed')],
                'gaps':['分钟证据请求超时或失败；本轮不以缺失数据推断信号']})
        from .sector_fund_evidence import fetch_sector_batch, fetch_fund_evidence
        sector_fetch = self.sector_fetcher or fetch_sector_batch
        stage('sector_evidence', 72, '正在核验同一完成分钟的行业同步表现')
        def sectors_for(_, deadline):
            return sector_fetch(codes,intraday,minute_cutoff,self.yao.data_dir,
                **({'prepared':prepared_sectors, 'deadline':deadline,
                    'timeout':max(0,deadline-monotonic())} if not self.sector_fetcher else {}))
        sector_packets = fetch_batch('sector_evidence', sectors_for, [None], 10, deadline_aware=True)
        sectors = sector_packets[0] if sector_packets else {}
        def funds_for(code, deadline):
            cutoff = (intraday.get(code) or {}).get('asOf')
            if not cutoff:
                return code, {'metrics':{}, 'reasonCodes':['stock_minute_missing'],
                              'gaps':['个股分钟截止时点缺失，未计算资金因子']}
            return code, (self.fund_fetcher or fetch_fund_evidence)(code,cutoff,
                **({'deadline':deadline} if not self.fund_fetcher else {}))
        stage('fund_evidence', 80, '正在核验同窗资金证据；缺失因子将如实标记')
        funds = dict(fetch_batch('fund_evidence', funds_for, codes, 10, deadline_aware=True, workers=8))
        for code in codes:
            for name, packets in (('sector_evidence',sectors),('fund_evidence',funds)):
                packets.setdefault(code, {'metrics':{}, 'reasonCodes':[
                    quality['stageMissingReasons'].get(name,'provider_unavailable')],
                    'gaps':['行业或资金采集未完成；不把缺失当成负面信号']})
        if remaining() <= 0:
            raise ValueError('本轮采集达到全局时间上限，未用陈旧窗口生成新推荐')
        self._await_target(run)
        # Quotes are acquired LAST, so minute/history I/O cannot age the final price.
        stage('final_quotes', 88, '正在刷新最终报价并核验有效时间')
        quote_groups = [codes[start:start+20] for start in range(0, len(codes), 20)]
        quote_batches = fetch_batch('final_quotes', self._quotes, quote_groups, 15)
        quotes = {code: quote for batch in quote_batches for code, quote in batch.items()}
        verification = self._verify_archived_quotes(codes,self.clock(),quotes=quotes,
            timeout=min(10,remaining()),lineage_cutoff=minute_cutoff)
        for code,lineage in verification.pop('lineages').items():
            intraday[code]['dataLineage'] = self._refresh_verified_lineage(
                intraday[code].get('dataLineage'),lineage)
            if intraday[code]['dataLineage'].get('status') == 'tainted':
                intraday[code].update(metrics={}, v11Inputs={})
                intraday[code].setdefault('gaps',[]).append('独立报价核验发现分钟价格或计算版本冲突，本轮隔离')
        quality['independentMinuteVerification'] = verification
        stage('evaluation', 94, '正在按证据规则筛选并整理候选')
        assessed = []
        researched = []
        run['profileCandidates'] = {key: [] for key in ('conservative', 'regular', 'aggressive')}
        run['precisionWatchlist'] = []
        for item in queued:
            code = item['code']
            quote = quotes.get(code) or {}
            state, gaps = validate_candidate(item, quote, self.clock(), slot)
            reminders = prepared_reminders.get(code, [])
            explanation = explain_candidate(item, quote, daily.get(code), intraday.get(code), self.clock(), slot,
                                            dict(snapshot.attrs),sector=sectors.get(code),funds=funds.get(code),v11=True)
            candidate = {**item, 'code':code, 'name':item.get('name',code),
                'model_version':SCORE_VERSION, 'modelVersion':SCORE_VERSION, 'horizon':'T 至 T+5',
                'status':state,
                'minuteSourceTime':(intraday.get(code) or {}).get('asOf'),
                'factorCutoffAt':minute_cutoff.isoformat(),
                'dataLineage':(intraday.get(code) or {}).get('dataLineage') or {'status':'unknown',
                    'reasonCodes':['minute_lineage_unavailable']},
                'industry':((sectors.get(code) or {}).get('sector') or {}).get('name'),
                'stateLabel':{'conditional':'条件观察','premarket':'盘前观察','expired':'已过期','windvane':'不可买风向标','data_insufficient':'数据不足'}[state],
                'modelBranch':'量价结构条件观察', 'modelStatus':'evidence_rules_unvalidated',
                'scoreMeaning':'本地证据规则未校准为收益预测；无综合评分或胜率',
                'referencePrice':quote.get('price'), 'currentChange':quote.get('change_pct'), 'quote':quote,
                'features':{k:item.get(k) for k in ('price','amount','change_pct','turnover_rate','volume_ratio','rawRankSignal5d','modelStatus')},
                **explanation,
                'data_quality':{'gaps':list(dict.fromkeys(gaps + explanation['evidenceGaps']))},
                'risks':list(dict.fromkeys([*explanation['risks'], *gaps, *reminders])), 'observationReminders':reminders,
                'evidenceKeys':['indicatorEvidence','quote'], 'transition':'继续观察' if code in old else '新增观察',
                'decision_at':self.clock().isoformat(), 'sent_at':None, 'received_at':None}
            context = prepared_context[code]
            candidate['researchFactors'] = prepared_research[code]
            candidate['researchFactorStatus'] = {key: value.get('status', 'unavailable')
                                                for key, value in candidate['researchFactors'].items()}
            for key, label in (('C', '已披露事件'), ('G', '历史股性'), ('E', '市场情绪'), ('H', '题材扩散（影子）')):
                packet = candidate['researchFactors'][key]
                candidate['indicatorEvidence'].append({'key': 'research_' + key,
                    'label': label, 'value': packet.get('score'), 'unit': '分',
                    'source': packet.get('source'), 'asOf': packet.get('asOf'),
                    'status': packet.get('status', 'unavailable'),
                    'role': '研究因子', 'threshold': '事前证据规则；分数不是胜率'})
                candidate['data_quality']['gaps'].extend(packet.get('gaps') or [])
            candidate.update(score_v11(candidate, intraday.get(code), now=self.clock()))
            self._factor_availability(candidate, intraday.get(code))
            candidate.update(recommendationLogicVersion=SCORE_VERSION,evidenceVersion=VERSION)
            candidate['algorithmContractId'] = run['algorithm']['contractId']
            candidate['rankMeaning'] = 'V1.2分阶段规则排序；成熟样本通过验证后才使用学习模型，分数与数据覆盖均非胜率'
            candidate['data_quality'].update(confidence=candidate['dataConfidence'],
                                              historicalCoverageDays=candidate['historicalCoverageDays'],
                                              priceHistoricalCoverageDays=candidate['priceHistoricalCoverageDays'],
                                              confidenceStatus=candidate['confidenceStatus'])
            candidate['precisionDecision'] = evaluate_precision(
                candidate, context, daily.get(code))
            candidate['data_quality']['executionQuoteData'] = candidate['precisionDecision']['executionQuoteData']
            if candidate['precisionDecision']['executionQuoteData']['status'] != 'complete':
                message = '前收盘或供应商涨跌停价缺失/冲突；未开放正式交易候选资格'
                candidate['data_quality']['gaps'].append(message)
                candidate['evidenceGaps'].append(message)
            candidate['dailyLastClose'] = ((daily.get(code) or {}).get('metrics') or {}).get('last_close')
            candidate['dailyPreviousSession'] = (daily.get(code) or {}).get('previousSession')
            candidate['data_quality']['gaps'] = list(dict.fromkeys(
                candidate['data_quality']['gaps'] + candidate['precisionDecision']['sourceContext']['gaps']))
            researched.append(candidate)
            if state=='windvane':
                run['windvanes'].append(candidate)
            elif candidate['evidenceEligible']:
                assessed.append(candidate)
            else:
                candidate.update(status='evidence_insufficient', stateLabel='证据不足未入选')
                run['evidenceInsufficient'].append(candidate)
        from .decision_policy import annotate_portfolio, allocate_portfolio
        for candidate, annotated in zip(researched, annotate_portfolio(researched)):
            candidate.update(annotated)
        ordered = sorted(assessed, key=lambda candidate: (
            candidate.get('finalScore') is None,
            -(candidate.get('finalScore') or 0), evidence_order(candidate)))
        from .signal_learning import SignalLearningService
        learning = SignalLearningService(self.yao.data_dir)
        ordered = learning.rank_candidates(ordered, self.clock())
        ranked_by_code = {candidate['code']: candidate for candidate in ordered}
        researched = [ranked_by_code.get(candidate['code'], candidate) for candidate in researched]
        run['precisionResearch'] = ordered  # Keep nonselected controls for later comparisons.
        limit = min(5, max(0, int(top_n)))
        run['profilePortfolioPolicies'] = {}
        for profile in run['profileCandidates']:
            allocation = allocate_portfolio([p for p in ordered if is_entry_eligible(p, profile)][:limit])
            run['profileCandidates'][profile] = allocation['candidates']
            run['profilePortfolioPolicies'][profile] = allocation['policy']
        run.setdefault('adaptive', {})['portfolioPolicy'] = run['profilePortfolioPolicies']['regular']
        run['portfolioPolicy'] = run['profilePortfolioPolicies']['regular']
        run['candidates'] = run['profileCandidates']['regular']
        run['precisionWatchlist'] = [p for p in ordered
            if any(not d['entryEligible'] for d in p['precisionDecision']['profiles'].values())]
        for rank, candidate in enumerate(run['candidates'], 1):
            candidate['rank'] = rank
        quality['evidence_screen'] = {'researched': len(queued), 'supported':len(assessed),
                                      'insufficient':len(run['evidenceInsufficient']),
                                      'windvanes':len(run['windvanes'])}
        quality['deep_research_count'] = len(researched)
        quality['independent_evidence'] = {'logic_version':SCORE_VERSION,'evidence_version':VERSION,
            'history_20d_ready':sum((intraday.get(code) or {}).get('historyDays')==20 for code in codes),
            'sector_confirmed':sum(bool((sectors.get(code) or {}).get('confirmed')) for code in codes),
            'fund_source_available':sum(bool((funds.get(code) or {}).get('metrics')) for code in codes),
            'required_for_intraday':'有效实时报价、正常交易、20日同刻双历史和关键因子完整；分档价格纪律与事件风险均须通过'}
        quality['execution_quote_coverage'] = {
            'requested': len(researched),
            'complete': sum(p['precisionDecision']['executionQuoteData']['status'] == 'complete' for p in researched),
            'missingFields': {field: sum(field in p['precisionDecision']['executionQuoteData']['missingFields']
                                        for p in researched)
                              for field in ('pre_close', 'limit_up', 'limit_down')},
            'conflicting': sum(bool(p['precisionDecision']['executionQuoteData']['conflicts']) for p in researched),
        }
        quality['research_factors'] = {
            'event_observed': sum(p['researchFactorStatus']['C'] == 'observed' for p in researched),
            'character_observed': sum(p['researchFactorStatus']['G'] == 'observed' for p in researched),
            'market_emotion_status': emotion.get('status', 'unavailable'),
            'social_sentiment_status': 'not_connected',
            'theme_observed': sum(p['researchFactorStatus']['H'] == 'observed' for p in researched),
            'validation_status': 'rules_unvalidated',
        }
        quality['precision_policy'] = {'version': run['algorithm']['entryPolicyVersion'], 'status': 'rules_unvalidated',
            'profile_counts': {key: len(value) for key, value in run['profileCandidates'].items()},
            'watch_count': len(run['precisionWatchlist']), 'context_completed_at': context_bundle.get('completedAt')}
        run['marketSummary']='未触板潜伏与已触板延续独立排序。潜伏池要求当日涨跌-3%至5%、尚未触及10%模式价、距该价至少4%，并具备日线平台或修复承接；已知昨日收涨停形态不列潜伏。昨日历史口径未知时不声称已验证首板。已触板股票单列延续，不占潜伏名额。分数不是概率，正式交易条件单独核验。'
        run['status']='completed_observations' if any(run['profileCandidates'].values()) else 'no_candidates_with_coverage_limits'
        def finish_profiles():
            self._finish_quotes(run, quality)
            # Completion can remove stale quotes or whole profiles; recompute
            # weights only from the surviving, model-admitted candidates.
            for profile, rows in run['profileCandidates'].items():
                allocation = allocate_portfolio(rows)
                run['profileCandidates'][profile] = allocation['candidates']
                run['profilePortfolioPolicies'][profile] = allocation['policy']
            run['candidates'] = run['profileCandidates']['regular']
            run['portfolioPolicy'] = run['profilePortfolioPolicies']['regular']
            run.setdefault('adaptive', {})['portfolioPolicy'] = run['portfolioPolicy']
            for rank, candidate in enumerate(run['candidates'], 1):
                candidate['rank'] = rank
        finish_profiles()
        # T+1 has its own objective. Freeze it after the final quote/entry check,
        # with source freshness rechecked at completion, never backfill a slot.
        from .next_day_watch import build_next_day_watchlist
        reviewable = run['status'] in {'completed_observations', 'no_candidates_with_coverage_limits'}
        next_day = build_next_day_watchlist(researched if reviewable else [], self.clock(), top_n=top_n)
        next_research = learning.rank_next_day_candidates(next_day['researchCandidates'], self.clock())
        # Loading a model and checking its full lineage can take time. Recheck
        # after both model tasks, never carry a pre-inference fresh flag forward.
        finish_profiles()
        reviewable = run['status'] in {'completed_observations', 'no_candidates_with_coverage_limits'}
        completion_at = self.clock()
        for row in next_research:
            source_usable = not quote_check(row.get('quote') or {}, row['code'], completion_at)
            row['nextDaySignal']['sourceUsableAtCompletion'] = source_usable
            if not reviewable or not source_usable:
                row['nextDaySignal'].update(selectionEligible=False, selected=False,
                    selectionMissingReasons=['quote_or_delivery_expired_at_completion'])
        for queue, key in (('first_board','nextDayWatchlist'), ('continuation','nextDayContinuationWatchlist')):
            eligible = [row for row in next_research if row['nextDaySignal']['queue']==queue
                        and row['nextDaySignal'].get('selectionEligible') is not False]
            run[key] = [{**row, 'rank':rank, 'status':'next_day_watch',
                'stateLabel':row['nextDaySignal']['queueLabel'],
                'horizon':'下一交易日', 'selectionReasons':row['nextDaySignal']['reasons'],
                'nextDaySignal':{**row['nextDaySignal'], 'selected':True}}
                for rank, row in enumerate(eligible[:limit], 1)]
        run['nextDayResearch'] = next_day['nextDayResearch']
        run['nextDayResearch'].update(selectedCount=len(run['nextDayWatchlist']),
            continuationSelectedCount=len(run['nextDayContinuationWatchlist']),
            learningStatuses=sorted({row['nextDaySignal'].get('learningStatus','rules_unvalidated') for row in next_research}),
            completionQuoteRejectedCount=sum(not row['nextDaySignal']['sourceUsableAtCompletion'] for row in next_research),
            learningRejectedCount=sum(row['nextDaySignal'].get('selectionEligible') is False for row in next_research))
        next_selected = {row['code'] for row in run['nextDayWatchlist'] + run['nextDayContinuationWatchlist']}
        next_signals = {row['code']: {**row['nextDaySignal'], 'selected': row['code'] in next_selected}
                        for row in next_research}
        for candidate in researched:
            if candidate['code'] in next_signals:
                candidate['nextDaySignal'] = next_signals[candidate['code']]
        for collection in run['profileCandidates'].values():
            for candidate in collection:
                if candidate['code'] in next_signals:
                    candidate['nextDaySignal'] = next_signals[candidate['code']]
        quality['next_day_research'] = run['nextDayResearch']
        quality['factorAvailability'] = {key:{
            'observed':sum(bool((p.get('factorAvailability',{}).get(key) or {}).get('available')) for p in researched),
            'missing':sum(not bool((p.get('factorAvailability',{}).get(key) or {}).get('available')) for p in researched),
            'missingReasons':sorted({reason for p in researched
                for reason in ((p.get('factorAvailability',{}).get(key) or {}).get('reasonCodes') or [])})}
            for key in ('M','V','T','F','W','S','B','C','G','E','H','L')}
        if reviewable:
            if run['nextDayWatchlist'] or run['nextDayContinuationWatchlist']:
                run['status'] = 'completed_observations'
            run['message'] = (f'本轮有{len(run["nextDayWatchlist"])}只未触板潜伏观察、'
                              f'{len(run["nextDayContinuationWatchlist"])}只已触板延续观察；'
                              f'通过均衡档交易条件{len(run["candidates"])}只。观察排序未校准为涨停概率。')
        selected_codes = {candidate['code'] for candidate in run['candidates']}
        run['controls'] = [candidate for candidate in researched if candidate['code'] not in selected_codes]
        run['scoreVersion'] = SCORE_VERSION
        quality['v11_scoring'] = {'version':SCORE_VERSION,'status':'uncalibrated_rules',
            'complete_20d':sum((intraday.get(code) or {}).get('historyDays')==20 for code in codes),
            'price_complete_20d':sum((intraday.get(code) or {}).get('priceHistoryDays')==20 for code in codes),
            'low_5_19d':sum(5 <= ((intraday.get(code) or {}).get('historyDays') or 0) < 20 for code in codes),
            'insufficient_under_5d':sum(((intraday.get(code) or {}).get('historyDays') or 0) < 5 for code in codes)}
        def usable_minutes(code):
            minute = intraday.get(code) or {}
            try:
                at = datetime.fromisoformat(minute.get('asOf') or '')
                fresh = at.tzinfo is not None and 0 <= (self.clock()-at).total_seconds() < 300
            except (TypeError, ValueError):
                return False
            metrics = minute.get('metrics') or {}
            return fresh and any(metrics.get(key) is not None for key in
                ('speed_3m_pct', 'speed_5m_pct', 'local_high_5m', 'local_low_5m', 'vwap'))
        usable = sum(usable_minutes(code) for code in codes)
        quality['minute_coverage'] = {'requested': len(codes), 'usable': usable, 'missing': len(codes)-usable}
        def aligned_factor(code, packet):
            try:
                at = datetime.fromisoformat(packet.get('asOf') or '')
                return (at.tzinfo is not None and 0 <= (self.clock()-at).total_seconds() < 300
                        and at == datetime.fromisoformat((intraday.get(code) or {}).get('asOf') or ''))
            except (TypeError, ValueError):
                return False
        # Report the factors that survived the refreshed minute window, rather
        # than raw successes collected before a slow provider delayed the scan.
        quality['independent_evidence'].update(
            sector_confirmed=sum(bool((sectors.get(code) or {}).get('confirmed'))
                                 and aligned_factor(code, sectors[code]) for code in codes),
            fund_source_available=sum(bool((funds.get(code) or {}).get('metrics'))
                                      and aligned_factor(code, funds[code]) for code in codes))
        # Source availability and a bullish signal are different facts. A
        # computed negative sector/fund value is valid data, never a missing one.
        quality['independent_evidence'].update(
            sector_source_available=sum((sectors.get(code) or {}).get('metrics', {}).get('sector_return_5m_pct') is not None
                                        and aligned_factor(code, sectors[code]) for code in codes),
            fund_3m_available=sum((funds.get(code) or {}).get('metrics', {}).get('main_net_flow_3m') is not None
                                  and aligned_factor(code, funds[code]) for code in codes))
        quality['stageTimingsMs'][stage_name] = round((monotonic()-stage_started)*1000)

    def _persist_run(self, run):
        """Finish research and ledger work inside the official slot claim."""
        from .signal_learning import SignalLearningService
        result = clean(run)
        result.update(modelVersion=SCORE_VERSION, model_version=SCORE_VERSION, llmUsed=False)
        slot = result.get('scanSlot')
        if slot == 'review' and result.get('status') == 'audit_only':
            from .minute_history import archive_universe
            learning = SignalLearningService(self.yao.data_dir)
            # Settle existing evidence before potentially slow public I/O. Save
            # this checkpoint so interruption cannot hide work already done.
            result['learningReview'] = learning.review_due(self.clock())
            result['reviewProgress'] = {'stage': 'archived_evidence_reviewed', 'at': self.clock().isoformat()}
            self.db.save_yao_run(clean(result))
            try:
                requirements = learning.review_requirements(self.clock())
                result['priorityMinuteArchive'] = archive_universe(
                    requirements, self.clock(), self.yao.data_dir/'minute_history',
                    include_registered=False, timeout=120, count=1970, required_days=requirements)
                verified = self._verify_archived_quotes(list(requirements),self.clock(),timeout=30)
                verified.pop('lineages', None)
                result['priorityIndependentVerification'] = verified
                result['learningReview'] = learning.review_due(self.clock())
            except Exception as error:
                result['priorityMinuteArchive'] = {'status': 'failed', 'reason': type(error).__name__}
            result['reviewProgress'] = {'stage': 'signal_review_saved', 'at': self.clock().isoformat()}
            self.db.save_yao_run(clean(result))
            try:
                result['observationReview'] = review_local_observations(
                    self.db, self.quote_fetcher, self.clock(), clock=self.clock)
            except Exception as error:
                result['observationReview'] = {'status': 'failed', 'reason': type(error).__name__}
            try:
                snapshot = self.yao._fetch_snapshot()
                received = self.clock()
                result['marketEmotionArchive'] = self._observe_market_emotion(mainboard(snapshot), received)
                universe = mainboard(snapshot)['code'].tolist()
                universe_gap = None
            except Exception:
                universe, universe_gap = [], '本轮股票目录刷新失败，继续采集已登记全量股票池'
            try:
                result['minuteArchiveMaintenance'] = archive_universe(
                    universe, self.clock(), self.yao.data_dir/'minute_history', timeout=180)
                archived_codes = [item['code'] for item in result['minuteArchiveMaintenance'].get('details',[])
                                  if item.get('code')]
                verified = self._verify_archived_quotes(list(dict.fromkeys([*universe,*archived_codes])),
                                                        self.clock(),timeout=45)
                verified.pop('lineages', None)
                result['archiveIndependentVerification'] = verified
                # Today's close can now be independently verified. Settle it
                # within this same review instead of waiting for tomorrow.
                result['learningReview'] = learning.review_due(self.clock())
            except Exception as error:
                result['minuteArchiveMaintenance'] = {'status': 'failed', 'reason': type(error).__name__}
            if universe_gap:
                result['minuteArchiveMaintenance']['universeGap'] = universe_gap
            result['reviewProgress'] = {'stage': 'completed', 'at': self.clock().isoformat(),
                'meaning': '已处理可用证据；缺失样本仍待补齐，不等于模型已学习'}
        elif slot == 'weekly' and result.get('status') == 'audit_only':
            result['localTraining'] = SignalLearningService(self.yao.data_dir).train_and_evaluate(self.clock())
        elif slot in ('live','0920','0940','0955','1030','1455'):
            result = SignalLearningService(self.yao.data_dir).persist_scan(
                result, slot, result.get('cutoff_at') or self.clock(), bool(result.get('official')))
        self.db.save_yao_run(clean(result))
        return clean(result)
