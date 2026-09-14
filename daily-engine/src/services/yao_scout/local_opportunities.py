"""Local-only T..T+5 selection. No language model is reachable from scans."""
from __future__ import annotations
from src.services.yao_scout.daily_opportunities import DailyOpportunityService, mainboard, clean, validate_candidate
from src.quant.tier_models import get_tier_model_service
from src.services.yao_scout.local_observation_review import review_local_observations, read_observation_reminders

VERSION = 'king-local-5d-v1'

class LocalOpportunityService(DailyOpportunityService):
    def __init__(self, yao, ai_config=None, **kwargs):
        super().__init__(yao, None, **kwargs)

    def _scan(self, run, quality, history, dlm, previous, now, slot, top_n):
        snapshot = self.yao._fetch_snapshot()
        frame = mainboard(snapshot)
        quality.update(self.yao._snapshot_meta(snapshot, point_in_time_ok=False))
        quality.update(snapshot_count=len(snapshot), mainboard_count=len(frame), llm_calls=0)
        if frame.empty or snapshot.attrs.get('stale'):
            raise ValueError('全市场行情为空或陈旧，本轮保留历史结果')
        models = get_tier_model_service()
        from src.services.yao_scout.service import YaoScoutService
        frame = YaoScoutService._preliminary_rank(frame)
        frame['screen_score'] = frame['_pre_score']
        ranked = models.score_local_five_day(clean(frame.to_dict('records')))
        run.update(model_version=VERSION, modelVersion=VERSION, aiAudit=[], aiReviewStatus='not_requested')
        old = {p.get('code') for p in previous.get('candidates', [])}
        quality['model_universe_count'] = len(ranked)
        self._await_target(run)
        # Start the final quote request at the target time, after early ranking.
        # Batch requests keep the first candidates fresh while later ones load.
        quotes = {}
        for index, item in enumerate(ranked):
            if len(run['candidates']) >= min(5, max(0, int(top_n))):
                break
            code = item['code']
            if code not in quotes:
                quotes.update(self._quotes([row['code'] for row in ranked[index:index + 20]]))
            quote = quotes.get(code) or {}
            state, gaps = validate_candidate(item, quote, self.clock(), slot)
            reminders = read_observation_reminders(self.db, code, self.clock())
            candidate = {**item, 'code':code, 'name':item.get('name',code),
                'model_version':VERSION, 'modelVersion':VERSION, 'horizon':'T 至 T+5',
                'rank':len(run['candidates'])+1, 'status':state,
                'stateLabel':{'conditional':'条件观察','premarket':'盘前观察','expired':'已过期','windvane':'不可买风向标','data_insufficient':'数据不足'}[state],
                'modelBranch':'BalancedRank 5日' if item.get('modelStatus')=='qualified' else '本地规则观察',
                'thesis':'本地5日排序；依据：'+'；'.join(str(x) for x in item.get('tierReasons', item.get('degradedReasons', []))),
                'referencePrice':quote.get('price'), 'currentChange':quote.get('change_pct'), 'quote':quote,
                'features':{k:item.get(k) for k in ('price','amount','change_pct','turnover_rate','volume_ratio','rawRankSignal5d','modelStatus')},
                'data_quality':{'gaps':gaps}, 'probabilities':{}, 'triggers':['行情有效且风险条件经人工核实'],
                'invalidations':['来源失效或关键证据变化'], 'noChase':'不可买或证据不足时仅观察',
                'risks':list(dict.fromkeys([*gaps, *reminders])), 'observationReminders':reminders,
                'evidenceKeys':['features','quote'], 'transition':'继续观察' if code in old else '新增观察',
                'decision_at':self.clock().isoformat(), 'sent_at':None, 'received_at':None}
            if state=='windvane':
                run['windvanes'].append(candidate)
            else:
                run['candidates'].append(candidate)
        run['marketSummary']='本地模型与规则筛选；AI仅在点击复审后调用。分数为研究排序，不是成功概率。'
        run['status']='completed_observations' if run['candidates'] else 'no_candidates_with_coverage_limits'
        self._finish_quotes(run, quality)

    def run(self, scan_slot='live', **kwargs):
        result = super().run(scan_slot, **kwargs)
        result.update(modelVersion=VERSION, model_version=VERSION, llmUsed=False)
        if scan_slot=='review' and result.get('status')=='audit_only':
            result['observationReview'] = review_local_observations(self.db, self.quote_fetcher, self.clock(), clock=self.clock)
            result['localMaintenance'] = self.yao.mature_outcomes()
        if scan_slot=='weekly' and result.get('status')=='audit_only':
            models=get_tier_model_service()
            artifact=models._load_artifacts().get('regular')
            symbols=((artifact.metadata if artifact else {}).get('trainingScope') or {}).get('symbols') or []
            result['localTraining'] = models.train_universe(symbols) if symbols else {'status':'initialization_required','message':'请在策略页选择训练股票池；后台不隐式扩展训练范围'}
        self.db.save_yao_run(clean(result))
        return clean(result)
