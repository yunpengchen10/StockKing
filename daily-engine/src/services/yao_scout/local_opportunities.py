"""Local-only T..T+5 selection. No language model is reachable from scans."""
from __future__ import annotations
from datetime import datetime, timedelta
from time import monotonic
import pandas as pd
from src.services.yao_scout.daily_opportunities import DailyOpportunityService, mainboard, clean, validate_candidate
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

    def _scan(self, run, quality, history, dlm, previous, now, slot, top_n):
        stage_started = monotonic()
        stage_name = 'snapshot'
        quality['stageTimingsMs'] = {}
        quality['stageIncompleteCounts'] = {}
        quality['stageBudgetsSeconds'] = {}
        def stage(name, progress, message):
            nonlocal stage_started, stage_name
            tick = monotonic()
            quality['stageTimingsMs'][stage_name] = round((tick-stage_started)*1000)
            stage_name, stage_started = name, tick
            self._progress(progress, message)
        def fetch_batch(name, fetch, items, timeout):
            items = list(items)
            # The former per-stage budget was sized for 30 names. Preserve its
            # per-stock allowance as coverage grows, with bounded concurrency.
            budget = timeout * max(1, (len(items)+29)//30)
            target_at = run['delivery'].get('target_at')
            if run.get('official') and target_at:
                deadline = datetime.fromisoformat(target_at) + timedelta(seconds=120)
                budget = min(budget, max(0, (deadline-self.clock()).total_seconds()))
            quality['stageBudgetsSeconds'][name] = budget
            results, unfinished = bounded_fetch_map(fetch, items, workers=4, timeout=budget)
            quality['stageIncompleteCounts'][name] = len(unfinished)
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
            try:
                if software_market.available:
                    packet = software_market.bars(item['code'],period='101',count=260)
                    raw = pd.DataFrame(packet.get('bars') or [])
                    if not raw.empty:
                        raw['date'] = raw['end']
                        raw.attrs['daily_source'] = ('software:go/' + str(packet.get('source') or 'unknown')
                            + '; adjustment=' + str(packet.get('adjustment') or 'unknown'))
                        raw.attrs['daily_adjustment'] = packet.get('adjustment')
                else:
                    # Sina's daily endpoint provides unadjusted prices; adjusted
                    # historical highs cannot establish exchange limit touches.
                    raw = self.yao.history_fetcher(item['code'], lookback_days=400, source='sina', retries=1,
                        cache_dir=self.yao.data_dir / 'daily_history', cache_ttl_seconds=3600)
                    if raw is not None and raw.attrs.get('daily_source') == 'sina':
                        raw.attrs['daily_adjustment'] = 'unadjusted'
            except Exception:
                raw = None
            evidence = daily_evidence(raw, now)
            evidence['characterFactor'] = character_factor(raw, now,
                price_basis=raw.attrs.get('daily_adjustment') if raw is not None else None)
            return item['code'], evidence
        daily = dict(fetch_batch('daily_history', history_for, queued, 25))
        prepared_sectors = None
        stage('sector_membership', 32, '正在读取行业成分与行业证据')
        if not self.sector_fetcher:
            from .sector_fund_evidence import prepare_sector_membership
            try:
                prepared_sectors = prepare_sector_membership([row['code'] for row in queued],self.clock(),self.yao.data_dir)
            except Exception:
                pass
        # Populate persistent minute history during preparation. A newcomer later
        # gets the same on-demand backfill, never a smaller historical baseline.
        warmed_codes = set()
        if run.get('official') and not self.minute_fetcher:
            stage('minute_warmup', 40, '正在为计划扫描预取分钟历史')
            from .minute_history import fetch_bar_evidence
            def warm(item):
                try:
                    result = fetch_bar_evidence(item['code'],self.clock(),self.yao.data_dir/'minute_history')
                    return item['code'], (result.get('historyDays') or 0) >= 5
                except Exception:
                    return item['code'], False  # Final fetch records the gap.
            warmed_codes = {code for code, ready in fetch_batch('minute_warmup', warm, queued, 35) if ready}
        # Batch fundamentals/events while preparing; never age the final quote
        # with vendor calls. Archives record observedAt independently of reports.
        from src.services.akshare_context import AkshareContextProvider, for_stock
        from .precision_policy import evaluate as evaluate_precision
        stage('fundamental_context', 48, '正在核验财务、业绩预告与解禁信息')
        provider = self.context_provider or AkshareContextProvider(self.yao.data_dir / 'akshare-context', clock=self.clock, timeout=25)
        bundles = fetch_batch('fundamental_context', lambda _: provider.collect(self.clock()), [None], 35)
        context_bundle = bundles[0] if bundles else {'tables': [], 'status': 'unavailable',
            'reason': '外部背景数据超时，保留缺口，不视为已核验'}
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
        # Archive the whole mainboard, never only the selected or rising stocks.
        # Reception time is an availability boundary, not a quote timestamp.
        run['marketEmotion'] = emotion
        def minutes_for(item, refresh=False):
            try:
                from src.services.yao_scout.minute_history import fetch_bar_evidence
                fetch = self.minute_fetcher or fetch_bar_evidence
                kwargs = {'software_count':240} if not self.minute_fetcher and item['code'] in warmed_codes else {}
                if refresh and not self.minute_fetcher:
                    kwargs = {'software_count':240, 'backfill':False}
                return item['code'], fetch(item['code'], self.clock(), self.yao.data_dir / 'minute_history', **kwargs)
            except Exception:
                return item['code'], {'metrics': {}, 'gaps': ['分钟行情未取得；不以全天涨幅/快照量比代替即时信号']}
        stage('minute_evidence', 60, '正在核验分钟价格结构与历史覆盖')
        intraday = dict(fetch_batch('minute_evidence', minutes_for, queued, 20 if warmed_codes else 35))
        for item in queued:
            intraday.setdefault(item['code'], {'metrics': {}, 'gaps': ['分钟证据请求超时或失败；本轮不以缺失数据推断信号']})
        from .sector_fund_evidence import fetch_sector_batch, fetch_fund_evidence
        codes = [row['code'] for row in queued]
        sector_fetch = self.sector_fetcher or fetch_sector_batch
        stage('sector_evidence', 72, '正在核验行业同步表现')
        sectors = sector_fetch(codes,intraday,self.clock(),self.yao.data_dir,
                               **({'prepared':prepared_sectors} if not self.sector_fetcher else {}))
        def funds_for(code):
            cutoff = (intraday.get(code) or {}).get('asOf') or self.clock()
            return code, (self.fund_fetcher or fetch_fund_evidence)(code,cutoff)
        stage('fund_evidence', 80, '正在核验资金证据；缺失因子将如实标记')
        funds = dict(fetch_batch('fund_evidence', funds_for, codes, 12))
        # Context tables, theme breadth and saved reminders can be large. Finish
        # this work BEFORE the final quotes, so it cannot consume their 30s TTL.
        stage('context_evaluation', 82, '正在整理事件、行业背景及历史复盘提醒')
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
        # Broad scans can outlive the earliest minute samples. Refresh price
        # structure after optional slow factors, before acquiring final quotes.
        # Optional factors that no longer align remain missing, never relabeled.
        refresh_at = self.clock()
        def needs_minute_refresh(item):
            try:
                stamp = datetime.fromisoformat((intraday.get(item['code']) or {}).get('asOf') or '')
                return stamp.tzinfo is not None and (refresh_at-stamp).total_seconds() >= 180
            except (ValueError, TypeError):
                return False  # Failed requests stay visible; do not retry a failed entire stage.
        refresh_items = [item for item in queued if needs_minute_refresh(item)]
        if refresh_items:
            stage('minute_refresh', 84, f'正在更新 {len(refresh_items)} 只较早取得的分钟证据')
            intraday.update(dict(fetch_batch('minute_refresh', lambda item: minutes_for(item, refresh=True), refresh_items, 20)))
        quality['minute_refresh_count'] = len(refresh_items)
        self._await_target(run)
        # Quotes are acquired LAST, so minute/history I/O cannot age the final price.
        stage('final_quotes', 88, '正在刷新最终报价并核验有效时间')
        quote_groups = [codes[start:start+20] for start in range(0, len(codes), 20)]
        quote_batches = fetch_batch('final_quotes', self._quotes, quote_groups,
                                    15 * max(1, (len(queued)+79)//80))
        quotes = {code: quote for batch in quote_batches for code, quote in batch.items()}
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
        ordered = sorted(assessed, key=lambda candidate: (
            candidate.get('finalScore') is None,
            -(candidate.get('finalScore') or 0), evidence_order(candidate)))
        from .signal_learning import SignalLearningService
        ordered = SignalLearningService(self.yao.data_dir).rank_candidates(ordered, self.clock())
        ranked_by_code = {candidate['code']: candidate for candidate in ordered}
        researched = [ranked_by_code.get(candidate['code'], candidate) for candidate in researched]
        run['precisionResearch'] = ordered  # Keep nonselected controls for later comparisons.
        limit = min(5, max(0, int(top_n)))
        for profile in run['profileCandidates']:
            run['profileCandidates'][profile] = [p for p in ordered
                if is_entry_eligible(p, profile)][:limit]
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
        self._finish_quotes(run, quality)
        # T+1 has its own objective. Freeze it after the final quote/entry check,
        # with source freshness rechecked at completion, never backfill a slot.
        from .next_day_watch import build_next_day_watchlist
        reviewable = run['status'] in {'completed_observations', 'no_candidates_with_coverage_limits'}
        next_day = build_next_day_watchlist(researched if reviewable else [], self.clock(), top_n=top_n)
        run['nextDayWatchlist'] = next_day['nextDayWatchlist']
        run['nextDayContinuationWatchlist'] = next_day['nextDayContinuationWatchlist']
        run['nextDayResearch'] = next_day['nextDayResearch']
        next_selected = {row['code'] for row in run['nextDayWatchlist'] + run['nextDayContinuationWatchlist']}
        next_signals = {row['code']: {**row['nextDaySignal'], 'selected': row['code'] in next_selected}
                        for row in next_day['researchCandidates']}
        for candidate in researched:
            if candidate['code'] in next_signals:
                candidate['nextDaySignal'] = next_signals[candidate['code']]
        quality['next_day_research'] = run['nextDayResearch']
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
                        and packet.get('asOf') == (intraday.get(code) or {}).get('asOf'))
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
