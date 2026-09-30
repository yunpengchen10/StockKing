// Presentation fixtures only: no API keys, user database, real recommendations or returns.
module.exports = function createTourFixture(sample) {
  const stocks = sample.instruments;
  const asOf = sample.asOfMarketDate;
  const generatedAt = `${asOf}T17:30:00+08:00`;
  const code = s => `${s.code}.${s.code.startsWith('6') ? 'SH' : 'SZ'}`;
  const candidates = stocks.slice(0, 2).map((s, index) => ({
    code: code(s), name: s.name, rank: index + 1, scoreVersion: 'stockking-v1.1-demo',
    stateLabel: '演示候选', referencePrice: s.quote.priceCny,
    currentChange: (s.quote.priceCny / s.quote.previousCloseCny - 1) * 100,
    score: null, earlyScore: null, mainRiseScore: null, distributionRisk: null,
    dataConfidence: null, historicalCoverageDays: 0, observableStructure: '演示布局 · 待核验',
    selectionReasons: ['操作演示：查看原始证据，再决定是否继续观察。', '本卡片为演示 fixture，不是算法历史推荐。'],
    riskReasons: ['历史行情样本，不能用作当前交易依据。'],
    triggerConditions: ['先核验当日量价、公告与数据时间。'],
    invalidationConditions: ['缺少当日证据时保留观察，不推断可成交。'],
    entryPlan: { holdingWindow: '操作演示', trigger: '补齐当前证据后再判断', noChase: '不依据历史报价追价', invalidations: '缺少证据即停止推断' },
    probabilityStatus: 'withheld_until_calibrated',
    quote: { source: '历史样本', provider_timestamp: s.quote.sourceTime, fetched_at: s.quote.checkedAt, public_quote_metadata: { status: 'reference_only' } },
    indicatorEvidence: [
      { key: 'sample_price', label: '历史样本价格', value: s.quote.priceCny, unit: ' 元', role: 'reference', status: 'reference', source: `${s.quote.source} 历史快照`, asOf: s.quote.sourceTime, threshold: '仅展示历史值，不作为入选门槛' },
      { key: 'sample_bars', label: '已载入日 K 线', value: s.daily.bars.length, unit: ' 根', role: 'reference', status: 'observed', source: `${s.daily.source} · 未复权`, asOf: asOf, threshold: '展示 K 线与指标；不据此宣称推荐表现' },
    ],
  }));
  const adaptive = {
    generatedAt, asOfDate: asOf, status: 'completed_observations', scoreVersion: 'stockking-v1.1-demo',
    modelVersion: 'stockking-v1.1-demo', candidates, changes: {},
    dataQuality: { research_universe_count: 3, research_count: 3, deep_research_count: 3, snapshot_count: 3,
      research_coverage: '此处仅载入3只历史样本供界面演示，未执行全市场扫描。真实深研按主板池10%向上取整、至少300只且不超过池大小。' },
    message: '演示数据：候选与排序为界面 fixture；价格取自 2026-09-29 历史样本。',
  };
  const displayed = { schemaVersion: 4, generatedAt, asOfDate: asOf, methodologyVersion: 'stockking-v1.1-demo', adaptive };
  const rows = stocks.map((s, index) => ({ ID: index + 1, StockCode: `${s.code.startsWith('6') ? 'sh' : 'sz'}${s.code}`, Name: s.name,
    Price: s.quote.priceCny, SelectionPrice: null, SelectionStatus: 'demo', LatestQuoteTime: s.quote.sourceTime,
    LatestQuoteStatus: 'historical', Sort: index + 1, Time: generatedAt }));
  const signals = candidates.map((candidate, index) => ({
    signalId: `demo-only-${index + 1}`, code: candidate.code, name: candidate.name, signalDate: asOf,
    decisionAt: generatedAt, sourceTime: stocks[index].quote.sourceTime,
    modelVersion: 'stockking-v1.1-demo', slot: '演示', official: false, selected: true,
    signalPrice: candidate.referencePrice, firstTradablePrice: null, reviewStatus: 'pending_review',
    snapshot: candidate, reason: '演示记录：保留原始快照、时间与证据。', rank: index + 1,
  }));
  const fixed = {
    GetConfig: { darkTheme: true, aiConfigs: [], AiConfigs: [] }, GetEngineStatus: { ready: true, state: 'ready' },
    IsTradingTime: false, IsHKTradingTime: false, IsUSTradingTime: false,
    GetDisplayedKingPicks: displayed, GetLatestKingPicks: adaptive, GetKingPicks: displayed,
    StartKingPicksRefresh: { task_id: 'demo-only', status: 'completed', progress: 100, result: displayed },
    GetKingPicksRefreshTask: { task_id: 'demo-only', status: 'completed', progress: 100, result: displayed },
    GetStockKingBackgroundLearning: false, GetStockKingAutoRecommendations: false,
    GetStockKingBackgroundStatus: { status: '演示模式 · 未运行' },
    GetFollowList: rows, RefreshStockKingWatchlist: rows,
    GetGroupList: [{ ID: 1, name: '银行观察', sort: 1 }, { ID: 2, name: '长期研究', sort: 2 }],
    GetAllGroupStocks: rows.map((r, i) => ({ groupId: i < 2 ? 1 : 2, stockCode: r.StockCode })),
    GetStockList: stocks.map(s => ({ ts_code: code(s), name: s.name })),
    GetStockKingRecommendationHistory: { items: signals },
    GetStockKingRecordQuotes: { quotes: Object.fromEntries(stocks.map(s => [s.code, { code: s.code, price: s.quote.priceCny, sourceTime: s.quote.sourceTime, source: '历史样本（非当前报价）' }])) },
    GetStockKingDelayedReviews: { items: [1, 3, 5].map(horizon => ({
      signalId: 'demo-only-1', code: candidates[0].code, name: candidates[0].name, signalDate: asOf,
      modelVersion: 'stockking-v1.1-demo', horizon, status: 'pending_review', fillStatus: 'unverified',
      observedReturn: null, observedMfe: null, observedMae: null, simulatedNetReturn: null,
      reason: '演示条目：未载入信号后证据，因此不展示模拟收益。', evidenceGrade: '演示 fixture · 待补齐',
    })) },
    GetStockKingLearningState: { stage: 'rules_cold_start', reason: '演示 fixture：未训练模型，未验证收益。', matureDays: 0, validSamples: 0, shadowDays: 0, minimumMatureDays: 120, minimumValidSamples: 1000 },
    GetKingPicksHistory: { items: [] }, GetResearchNote: {}, GetForecast: { available: false, unavailableReason: '演示模式未训练模型' },
    GetStockKLinePageWithFallback: { data: [], source: '历史样本' }, GetAiConfigs: [], GetStockKingAIProviders: [],
  };
  const bars = Object.fromEntries(stocks.map(s => [s.code, s.daily.bars.map(b => ({ day: b.date, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume, amount: b.amount_cny }))]));
  const sources = Object.fromEntries(stocks.map(s => [s.code, `历史样本 · ${s.daily.source === 'sina' ? '新浪' : s.daily.source}日线 · ${s.daily.priceAdjustment === 'none' ? '未复权' : s.daily.priceAdjustment}`]));
  return { fixed, bars, sources };
};

module.exports.install = function installTourFixture({ fixed, bars, sources }) {
  window.__tourCalls = [];
  const prefs = {};
  localStorage.setItem('kline-indicator-settings', JSON.stringify({ showMA: true, showVOL: true, showMACD: false }));
  window.go = { main: { App: new Proxy({}, { get: (_, key) => async (...args) => {
    window.__tourCalls.push({ method: String(key), args });
    if (key === 'GetStockKingPreference') return prefs[args[0]] || '';
    if (key === 'SaveStockKingPreference') { prefs[args[0]] = args[1]; return null; }
    if (key === 'GetStockKLineWithFallback') {
      const symbol = String(args[0]).replace(/\D/g, '').slice(0, 6);
      if (!bars[symbol]) throw Error(`Missing historical sample: ${symbol}`);
      return { data: structuredClone(bars[symbol]), source: sources[symbol] };
    }
    if (key === 'GetFollowList' || key === 'RefreshStockKingWatchlist') {
      const group = Number(args[0]);
      return structuredClone(fixed[key].filter(row => !group || fixed.GetAllGroupStocks.some(m => m.groupId === group && m.stockCode === row.StockCode)));
    }
    if (!Object.hasOwn(fixed, key)) throw Error(`Tour fixture missing method: ${String(key)}`);
    return structuredClone(fixed[key]);
  } }) } };
  window.runtime = new Proxy({}, { get: (_, key) => key === 'EventsOnMultiple' ? (event, callback) => {
    if (event === 'loadingMsg') setTimeout(() => callback('done'), 20);
    return () => {};
  } : () => {} });
};
