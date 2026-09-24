"""Local-only T..T+5 selection. No language model is reachable from scans."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from src.services.yao_scout.daily_opportunities import DailyOpportunityService, mainboard, clean, validate_candidate
from src.quant.tier_models import get_tier_model_service
from src.services.yao_scout.local_observation_review import review_local_observations, read_observation_reminders
from src.services.yao_scout.recommendation_evidence import VERSION, research_queue, daily_evidence, explain_candidate, evidence_order

class LocalOpportunityService(DailyOpportunityService):
    def __init__(self, yao, ai_config=None, *, minute_fetcher=None, sector_fetcher=None, fund_fetcher=None, context_provider=None, **kwargs):
        super().__init__(yao, None, **kwargs)
        self.minute_fetcher = minute_fetcher
        self.sector_fetcher, self.fund_fetcher = sector_fetcher, fund_fetcher
        self.context_provider = context_provider

    def _scan(self, run, quality, history, dlm, previous, now, slot, top_n):
        snapshot = self.yao._fetch_snapshot()
        frame = mainboard(snapshot)
        quality.update(self.yao._snapshot_meta(snapshot, point_in_time_ok=False))
        quality.update(snapshot_count=len(snapshot), mainboard_count=len(frame), llm_calls=0)
        if frame.empty or snapshot.attrs.get('stale'):
            raise ValueError('全市场行情为空或陈旧，本轮保留历史结果')
        # The former heat score is no longer a fallback recommendation engine.
        # A queue gives several forms of activity a chance to be researched;
        # independent price, structure and remaining-space evidence decides entry.
        queued = clean(research_queue(frame))
        run.update(model_version=VERSION, modelVersion=VERSION, recommendationLogicVersion=VERSION,
                   aiAudit=[], aiReviewStatus='not_requested', evidenceInsufficient=[])
        old = {p.get('code') for p in previous.get('candidates', [])}
        quality.update(model_universe_count=0, research_count=len(queued),
                       research_universe_count=len(frame),
                       research_coverage='全主板快照；最多30只并行深研，未深研股票不作已排除或无机会结论',
                       selection_method='成交额/快照量比/换手/开盘修复各自名次并集安排深研；无涨幅权重总分',
                       chatgpt_alignment='分钟结构+同刻行业共振+20日同刻放量+供应商资金方向；催化与预期差不作已核实结论')
        def history_for(item):
            try:
                raw = self.yao.history_fetcher(item['code'], lookback_days=100, source='auto', retries=1,
                    cache_dir=self.yao.data_dir / 'daily_history', cache_ttl_seconds=3600)
            except Exception:
                raw = None
            return item['code'], daily_evidence(raw, now)
        with ThreadPoolExecutor(max_workers=4) as pool:
            daily = dict(pool.map(history_for, queued))
        prepared_sectors = None
        if not self.sector_fetcher:
            from .sector_fund_evidence import prepare_sector_membership
            try:
                prepared_sectors = prepare_sector_membership([row['code'] for row in queued],self.clock(),self.yao.data_dir)
            except Exception:
                pass
        # Populate persistent minute history during preparation. A newcomer later
        # gets the same on-demand backfill, never a smaller historical baseline.
        if not self.minute_fetcher:
            from .minute_history import fetch_bar_evidence
            def warm(item):
                try:
                    fetch_bar_evidence(item['code'],self.clock(),self.yao.data_dir/'minute_history')
                except Exception:
                    pass  # Final fetch records the actionable per-candidate gap.
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(warm,queued))
        # Batch fundamentals/events while preparing; never age the final quote
        # with vendor calls. Archives record observedAt independently of reports.
        from src.services.akshare_context import AkshareContextProvider, for_stock
        from .precision_policy import evaluate as evaluate_precision
        provider = self.context_provider or AkshareContextProvider(self.yao.data_dir / 'akshare-context', clock=self.clock)
        context_bundle = provider.collect(self.clock())
        target_at = run['delivery'].get('target_at')
        if run.get('official') and target_at:
            # Leave a bounded preparation window before the desired delivery;
            # only the last quote request waits for the target itself.
            research_at = datetime.fromisoformat(target_at)-timedelta(seconds=90)
            while (remaining := (research_at-self.clock()).total_seconds()) > 0:
                self.sleeper(min(30, remaining))
        if run.get('official') and run['delivery'].get('target_at'):
            # Early work only prepares history. Each target scans the whole pool
            # again so a newly active stock is not confined to yesterday's list
            # or the ten-minutes-old preparation list.
            snapshot = self.yao._fetch_snapshot()
            frame = mainboard(snapshot)
            if frame.empty or snapshot.attrs.get('stale'):
                raise ValueError('目标时点全市场复核行情不可用，未用提前准备名单冒充本轮筛选')
            queued = clean(research_queue(frame))
            new_items = [item for item in queued if item['code'] not in daily]
            with ThreadPoolExecutor(max_workers=4) as pool:
                daily.update(dict(pool.map(history_for, new_items)))
            if new_items and not self.sector_fetcher:
                try:
                    new_sectors = prepare_sector_membership([row['code'] for row in new_items],self.clock(),self.yao.data_dir)
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
        def minutes_for(item):
            try:
                from src.services.yao_scout.minute_history import fetch_bar_evidence
                fetch = self.minute_fetcher or fetch_bar_evidence
                return item['code'], fetch(item['code'], self.clock(), self.yao.data_dir / 'minute_history')
            except Exception:
                return item['code'], {'metrics': {}, 'gaps': ['分钟行情未取得；不以全天涨幅/快照量比代替即时信号']}
        with ThreadPoolExecutor(max_workers=4) as pool:
            intraday = dict(pool.map(minutes_for, queued))
        from .sector_fund_evidence import fetch_sector_batch, fetch_fund_evidence
        codes = [row['code'] for row in queued]
        sector_fetch = self.sector_fetcher or fetch_sector_batch
        sectors = sector_fetch(codes,intraday,self.clock(),self.yao.data_dir,
                               **({'prepared':prepared_sectors} if not self.sector_fetcher else {}))
        def funds_for(code):
            cutoff = (intraday.get(code) or {}).get('asOf') or self.clock()
            return code, (self.fund_fetcher or fetch_fund_evidence)(code,cutoff)
        with ThreadPoolExecutor(max_workers=4) as pool:
            funds = dict(pool.map(funds_for,codes))
        self._await_target(run)
        # Quotes are acquired LAST, so minute/history I/O cannot age the final price.
        quotes = self._quotes([row['code'] for row in queued])
        assessed = []
        run['profileCandidates'] = {key: [] for key in ('conservative', 'regular', 'aggressive')}
        run['precisionWatchlist'] = []
        for item in queued:
            code = item['code']
            quote = quotes.get(code) or {}
            state, gaps = validate_candidate(item, quote, self.clock(), slot)
            reminders = read_observation_reminders(self.db, code, self.clock())
            explanation = explain_candidate(item, quote, daily.get(code), intraday.get(code), self.clock(), slot,
                                            dict(snapshot.attrs),sector=sectors.get(code),funds=funds.get(code))
            candidate = {**item, 'code':code, 'name':item.get('name',code),
                'model_version':VERSION, 'modelVersion':VERSION, 'horizon':'T 至 T+5',
                'status':state,
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
            candidate['precisionDecision'] = evaluate_precision(
                candidate, for_stock(context_bundle, code, self.clock()), daily.get(code))
            candidate['data_quality']['gaps'] = list(dict.fromkeys(
                candidate['data_quality']['gaps'] + candidate['precisionDecision']['sourceContext']['gaps']))
            if state=='windvane':
                run['windvanes'].append(candidate)
            elif candidate['evidenceEligible']:
                assessed.append(candidate)
            else:
                candidate.update(status='evidence_insufficient', stateLabel='证据不足未入选')
                run['evidenceInsufficient'].append(candidate)
        ordered = sorted(assessed, key=evidence_order)
        run['precisionResearch'] = ordered  # Keep nonselected controls for later comparisons.
        limit = min(5, max(0, int(top_n)))
        for profile in run['profileCandidates']:
            run['profileCandidates'][profile] = [p for p in ordered
                if p['precisionDecision']['profiles'][profile]['entryEligible']][:limit]
        run['candidates'] = run['profileCandidates']['regular']
        run['precisionWatchlist'] = [p for p in ordered
            if any(not d['entryEligible'] for d in p['precisionDecision']['profiles'].values())]
        for rank, candidate in enumerate(run['candidates'], 1):
            candidate['rank'] = rank
        quality['evidence_screen'] = {'researched': len(queued), 'supported':len(assessed),
                                      'insufficient':len(run['evidenceInsufficient']),
                                      'windvanes':len(run['windvanes'])}
        quality['independent_evidence'] = {'logic_version':VERSION,
            'history_20d_ready':sum((intraday.get(code) or {}).get('historyDays')==20 for code in codes),
            'sector_confirmed':sum(bool((sectors.get(code) or {}).get('confirmed')) for code in codes),
            'fund_source_available':sum(bool((funds.get(code) or {}).get('metrics')) for code in codes),
            'required_for_intraday':'分钟结构、行业共振、20/20同刻基准、即时放量、资金方向共同满足；否则不补位'}
        quality['precision_policy'] = {'version': 'king-precision-v1', 'status': 'rules_unvalidated',
            'profile_counts': {key: len(value) for key, value in run['profileCandidates'].items()},
            'watch_count': len(run['precisionWatchlist']), 'context_completed_at': context_bundle.get('completedAt')}
        run['marketSummary']='盘中按分钟结构、行业同刻共振、20日同刻放量和供应商资金方向共同筛选；每项保留来源、时间、覆盖率和计算口径。盘前仅列条件观察。'
        run['status']='completed_observations' if any(run['profileCandidates'].values()) else 'no_candidates_with_coverage_limits'
        self._finish_quotes(run, quality)

    def run(self, scan_slot='live', **kwargs):
        result = super().run(scan_slot, **kwargs)
        result.update(modelVersion=VERSION, model_version=VERSION, llmUsed=False)
        if scan_slot=='review' and result.get('status')=='audit_only':
            result['observationReview'] = review_local_observations(self.db, self.quote_fetcher, self.clock(), clock=self.clock)
            result['localMaintenance'] = self.yao.mature_outcomes()
            from .minute_history import archive_universe
            try:
                snapshot = self.yao._fetch_snapshot()
                universe = mainboard(snapshot)['code'].tolist()
                universe_gap = None
            except Exception:
                universe, universe_gap = [], '本轮股票目录刷新失败，继续采集已登记全量股票池'
            result['minuteArchiveMaintenance'] = archive_universe(universe,self.clock(),self.yao.data_dir/'minute_history')
            if universe_gap:
                result['minuteArchiveMaintenance']['universeGap'] = universe_gap
        if scan_slot=='weekly' and result.get('status')=='audit_only':
            models=get_tier_model_service()
            artifact=models._load_artifacts().get('regular')
            symbols=((artifact.metadata if artifact else {}).get('trainingScope') or {}).get('symbols') or []
            result['localTraining'] = models.train_universe(symbols) if symbols else {'status':'initialization_required','message':'请在策略页选择训练股票池；后台不隐式扩展训练范围'}
        self.db.save_yao_run(clean(result))
        return clean(result)
