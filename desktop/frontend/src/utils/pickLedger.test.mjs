import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { groupSignals, ledgerPercent, ledgerPrice, ledgerSigned, reviewStatusLabel, reviewReasonLabel, visibleReviews, reviewProgress, learningEffect, reviewReturn, shanghaiDate, learningProgress, recordQuoteCode, recordQuoteCodes, signalPriceComparison, createRecordQuoteLoader, nextDaySignalOf, isNextDaySelection, signalSelectionLabel, nextDayPatternValue, savedRecommendationSummary, nextDayQueueLabel, nextDayTouchLabel } from './pickLedger.mjs'

test('saved signals group by Shanghai day, stock and version without losing distinct scans', () => {
  const first = { runId:'a', asOf:'2026-09-28T18:00:00Z', slot:'0940', modelVersion:'v1.1', official:true, signals:[{signalId:'s1',code:'600001',selected:true},{signalId:'s2',code:'600002',selected:false}] }
  const second = { ...first,runId:'b',slot:'0955', signals:[{signalId:'s3',code:'600001',selected:true}] }
  const payload = {items:[first,second,first,{...second,modelVersion:'v2',signals:[{signalId:'s4',code:'600001',selected:true}]}]}
  const groups = groupSignals(payload)
  assert.equal(groups.length,2)
  assert.equal(groups[0].date,'2026-09-29')
  assert.equal(groups.find(group=>group.modelVersion==='v1.1').signals.length,2)
  assert.equal(groupSignals(payload,{includeControls:true}).length,3)
  assert.equal(shanghaiDate('bad timestamp'),'')
})

test('next-day observations are visible by default without changing executable selection or including controls', () => {
  const observation = Object.freeze({ signalId: 'watch', code: '600001', selected: false,
    snapshot: Object.freeze({ finalScore: 98, nextDaySignal: Object.freeze({ selected: true, score: 64, targetSession: '2026-10-09', reasons: ['突破结构'] }) }) })
  const trade = { signalId: 'trade', code: '600002', selected: true }
  const control = { signalId: 'control', code: '600003', selected: false, snapshot: { nextDaySignal: { selected: false, score: 42 } } }
  const both = { signalId: 'both', code: '600004', selected: true, snapshot: { nextDaySignal: { selected: true, score: 70 } } }
  const payload = { items: [{ asOf: '2026-10-08T14:55:00+08:00', signals: [observation, trade, control, both] }] }
  const visible = groupSignals(payload).flatMap(group => group.signals)
  assert.deepEqual(visible.map(signal => signal.signalId), ['watch', 'trade', 'both'])
  assert.equal(groupSignals(payload, { includeControls: true }).length, 4)
  assert.equal(observation.selected, false)
  assert.equal(isNextDaySelection(observation), true)
  assert.equal(signalSelectionLabel(observation), '次日观察')
  assert.equal(signalSelectionLabel(trade), '交易条件候选')
  assert.equal(signalSelectionLabel(control), '对照')
  assert.equal(nextDaySignalOf(observation).score, 64)
  assert.equal(nextDaySignalOf(observation).targetSession, '2026-10-09')
})

test('incomplete next-day price patterns are unknown, never reported as failed predictions', () => {
  assert.equal(nextDayPatternValue({ status: 'mature_price_pattern', touchLimit: true }, 'touchLimit'), '是')
  assert.equal(nextDayPatternValue({ status: 'mature_price_pattern', closeAtLimit: false }, 'closeAtLimit'), '否')
  for (const status of ['incomplete', 'pending', 'unavailable', undefined]) {
    assert.equal(nextDayPatternValue({ status, touchLimit: false }, 'touchLimit'), '—')
  }
  for (const value of [null, undefined, '', 0, 1, 'false']) {
    assert.equal(nextDayPatternValue({ status: 'mature_price_pattern', touchLimit: value }, 'touchLimit'), '—')
  }
})

test('delayed review distinguishes a missed execution from a future target or an unavailable calendar', () => {
  assert.equal(reviewStatusLabel('pending', { nextDay: true }), '尚未到期')
  assert.match(reviewStatusLabel('pending'), /未确认/)
  assert.equal(reviewStatusLabel('review_not_run', { nextDay: true }), '已到期，尚未执行')
  assert.equal(reviewStatusLabel('calendar_unavailable', { nextDay: true }), '交易日历待核验')
  assert.equal(reviewStatusLabel('failed', { nextDay: true }), '执行失败')
  assert.equal(reviewStatusLabel('incomplete', { nextDay: true }), '证据待补')
  assert.equal(reviewStatusLabel('future_status', { nextDay: true }), 'future_status', 'unknown states must not silently become waiting')
})

test('common execution evidence gaps explain what is missing without claiming price observation is impossible', () => {
  assert.match(reviewReasonLabel('point_in_time_price_limit_missing'), /信号当时.*涨停价.*不能核验模拟成交/)
  assert.match(reviewReasonLabel('signal_quote_missing_or_stale'), /30秒.*不能核验模拟成交/)
  assert.match(reviewReasonLabel('limit_up_queue_liquidity_or_slippage'), /成交量.*未模拟成交/)
  assert.match(reviewReasonLabel('no_fill'), /没有已实现的模拟交易收益/)
})

test('price-path evidence gaps can be explained separately from the original simulation failure', () => {
  const review = Object.freeze({ reason: 'point_in_time_price_limit_missing', observationReason: 'incomplete_minute_path' })
  assert.match(reviewReasonLabel(review.observationReason), /分钟数据不完整.*等待补齐/)
  assert.match(reviewReasonLabel(review.reason), /信号当时.*涨停价/)
  assert.match(reviewReasonLabel('entry_minute_bars_missing'), /信号当日.*分钟数据/)
  assert.match(reviewReasonLabel('target_close_minute_missing'), /目标交易日.*收盘分钟/)
  assert.match(reviewReasonLabel('signal_reference_price_unverified'), /参考价不一致/)
})

test('unknown or already-readable review reasons retain their original diagnostic text', () => {
  for (const value of ['new_backend_reason', '目标日价格或涨停制度无法核验', 'toString']) assert.equal(reviewReasonLabel(value), value)
  for (const value of [null, undefined, '']) assert.equal(reviewReasonLabel(value), '未记录')
})

test('review counts keep evidence gaps, missed runs and failures separate from completed observations', () => {
  const rows = ['mature_price_pattern', 'mature', 'no_fill', 'pending', 'review_not_run', 'calendar_unavailable', 'incomplete', 'pending_data', 'unverified', 'failed', 'exit_blocked'].map(status => Object.freeze({ status }))
  assert.deepEqual(reviewProgress(rows), { total: 11, concluded: 3, waiting: 1, notRun: 1, incomplete: 4, failed: 1, other: 1 })
  assert.deepEqual(reviewProgress([]), { total: 0, concluded: 0, waiting: 0, notRun: 0, incomplete: 0, failed: 0, other: 0 })
})

test('review progress defaults to actual selections and includes controls only when requested', () => {
  const rows = Object.freeze([
    Object.freeze({ signalId: 'selected', selected: true, status: 'incomplete' }),
    Object.freeze({ signalId: 'control', selected: false, status: 'mature' }),
    Object.freeze({ signalId: 'legacy', status: 'mature' }),
  ])
  assert.deepEqual(visibleReviews(rows).map(row => row.signalId), ['selected'])
  assert.equal(reviewProgress(visibleReviews(rows)).concluded, 0)
  assert.equal(visibleReviews(rows, { includeControls: true }).length, 3)
  assert.equal(reviewProgress(visibleReviews(rows, { includeControls: true })).concluded, 2)
  assert.deepEqual(visibleReviews(null), [])
})

test('training timestamps and mature sample counts never imply a model has changed live recommendations', () => {
  const rules = { effectiveRankSource: 'rules', stage: 'active', championVersion: 'configured-old', validSamples: 20000, lastTrainingAt: '2026-10-09T15:30:00+08:00' }
  assert.match(learningEffect(rules).label, /人工规则/)
  assert.match(learningEffect(rules).detail, /尚无已启用/)
  assert.match(learningEffect({ ...rules, shadowVersion: 'candidate' }).detail, /影子验证.*尚未用于/)
  const active = learningEffect({ effectiveRankSource: 'champion', effectiveRankVersion: 'rank-verified' })
  assert.match(active.label, /已启用模型/)
  assert.match(active.detail, /rank-verified/)
  assert.match(learningEffect({ effectiveRankSource: 'rollback', effectiveRankVersion: 'previous' }).label, /回退模型/)
  assert.match(learningEffect({ stage: 'active', championVersion: 'configured-only' }).label, /尚未确认/)
})

test('a learning-state failure cannot hide saved reviews or falsely retain an active model', async () => {
  const vue = await import('vue'), { parse, compileScript } = await import('@vue/compiler-sfc')
  const { descriptor } = parse(readFileSync(new URL('../components/PicksLedger.vue', import.meta.url), 'utf8'))
  const compiled = compileScript(descriptor, { id: 'review-state-test' }).content
    .replace(/^import\s+(.+?)\s+from\s+['"](.+?)['"];?$/gm, (_, bindings, path) => `const ${bindings.replace(/\bas\b/g, ':')} = modules[${JSON.stringify(path)}]`)
    .replace('export default', 'return')
  let failLearning = false, failReviews = false, instance
  const api = {
    GetStockKingDelayedReviews: async () => { if (failReviews) throw new Error('复盘连接失败'); return { items: [], nextDayReview: { items: [{ signalId: 'saved', selected: true, status: 'incomplete' }, { signalId: 'control', selected: false, status: 'mature_price_pattern' }] } } },
    GetStockKingLearningState: async () => { if (failLearning) throw new Error('学习连接失败'); return { effectiveRankSource: 'champion', effectiveRankVersion: 'verified' } },
  }
  const component = new Function('modules', compiled)({ vue, '../../wailsjs/go/main/App': api, '../utils/pickLedger.mjs': await import('./pickLedger.mjs') })
  component.render = () => { instance = vue.getCurrentInstance(); return null }
  const renderer = vue.createRenderer({
    createElement: type => ({ type }), createText: text => ({ text }), createComment: text => ({ text }),
    insert() {}, remove() {}, setElementText() {}, setText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null,
  })
  const app = renderer.createApp(component, { mode: 'reviews', active: true })
  app.mount({})
  try {
    for (let n = 0; n < 12; n++) await Promise.resolve()
    await vue.nextTick()
    const state = instance.setupState
    assert.match(state.effectiveLearning.label, /已启用模型/)
    failLearning = true
    await state.refresh()
    assert.equal(state.error, '')
    assert.equal(state.nextDayReviews[0].signalId, 'saved')
    assert.equal(state.nextDayReviews.length, 1)
    assert.equal(state.reviewSummaries[0].concluded, 0)
    state.includeControls = true
    assert.equal(state.nextDayReviews.length, 2)
    assert.equal(state.reviewSummaries[0].concluded, 1)
    assert.match(state.learningError, /学习连接失败/)
    assert.match(state.effectiveLearning.label, /尚未确认/)
    const previous = state.payload, queriedAt = state.queriedAt
    failReviews = true
    await state.refresh()
    assert.equal(state.payload, previous)
    assert.equal(state.queriedAt, queriedAt, 'failed reads must not advance the successful-query timestamp')
    assert.match(state.error, /复盘连接失败/)
  } finally { app.unmount() }
})

test('saved research counts the persisted primary and execution union, never the continuation pool', () => {
  const first = Object.freeze({ code: '600001', nextDaySignal: Object.freeze({ queue: 'first_board' }) })
  const continuation = Object.freeze({ code: '600002', nextDaySignal: Object.freeze({ queue: 'continuation' }) })
  const run = Object.freeze({ candidates: Object.freeze([]), nextDayWatchlist: Object.freeze([first]), nextDayContinuationWatchlist: Object.freeze([continuation]) })
  assert.deepEqual(savedRecommendationSummary(run), { saved: 1, execution: 0, primary: 1, continuation: 1, splitQueues: true })
  assert.equal(savedRecommendationSummary({ ...run, candidates: [{ code: '600001.SH' }] }).saved, 1)
  assert.equal(savedRecommendationSummary({ ...run, nextDayWatchlist: [] }).saved, 0)
  assert.equal(savedRecommendationSummary({ candidates: [], nextDayWatchlist: [{ code: '600003' }] }).splitQueues, false)
  assert.equal(nextDayQueueLabel(first.nextDaySignal), '未触板潜伏')
  assert.equal(nextDayQueueLabel(continuation.nextDaySignal), '已触板延续')
  assert.match(nextDayQueueLabel({}), /旧版未分队列/)
})

test('persisted ledger selection flags surface both named queues without promoting trading eligibility', () => {
  const first = { signalId: 'latent', code: '600001', selected: false, nextDaySelected: true, snapshot: { nextDaySignal: { queue: 'first_board', score: 65 } } }
  const continuation = { signalId: 'continuation', code: '600002', selected: false, nextDayContinuationSelected: true, snapshot: { nextDaySignal: { queue: 'continuation', score: 80 } } }
  const controls = { signalId: 'control', code: '600003', selected: false, snapshot: { nextDaySignal: { queue: 'first_board', selected: false } } }
  assert.equal(groupSignals({ items: [first, continuation, controls] }).length, 2)
  assert.equal(signalSelectionLabel(first), '未触板潜伏观察')
  assert.equal(signalSelectionLabel(continuation), '已触板延续观察')
  assert.equal(first.selected, false)
})

test('touch status uses frozen boolean evidence and never treats missing evidence as an untouched stock', () => {
  assert.equal(nextDayTouchLabel({ touchedLimitToday: true }), '已触板')
  assert.equal(nextDayTouchLabel({ features: { touchedLimitToday: false } }), '未触板')
  assert.equal(nextDayTouchLabel({ queue: 'continuation' }), '触板状态未知')
  for (const value of [undefined, null, 0, '', 'false']) assert.equal(nextDayTouchLabel({ touchedLimitToday: value }), '触板状态未知')
})

test('an unfilled signal never displays a simulated return; missing values stay missing', () => {
  assert.equal(reviewReturn({fillStatus:'unfilled',simulatedNetReturn:.3}),'—')
  assert.equal(reviewReturn({fillStatus:'filled',simulatedNetReturn:0}),'0.00%')
  assert.equal(reviewReturn({fillStatus:'filled',simulatedNetReturn:-.025}),'-2.50%')
  for (const missing of [null,undefined,'',NaN]) assert.equal(ledgerPercent(missing),'—')
  assert.equal(learningProgress(130,120),100)
  assert.equal(learningProgress(null,120),0)
})

test('picks views contain no model provider selector or language-model calls', () => {
  const page = readFileSync(new URL('../components/KingPicks.vue', import.meta.url), 'utf8')
  const ledger = readFileSync(new URL('../components/PicksLedger.vue', import.meta.url), 'utf8')
  for (const source of [page,ledger]) assert.doesNotMatch(source, /ReviewStockKingPicks|SetStockKingPicksAIConfig|GetStockKingAIProviders|openAI\(|reviewPicks\(|EconomyBudget/)
  assert.match(page, /GetStockKingAutoRecommendations/)
  assert.match(page, /GetStockKingBackgroundLearning/)
  assert.match(ledger, /GetStockKingDelayedReviews/)
})

test('each saved signal uses its own immutable reference price, with signed per-share and percentage changes', () => {
  const quote = Object.freeze({ code: '600001', price: 11, sourceTime: '2026-09-30T10:30:00+08:00', source: 'Tencent' })
  const first = Object.freeze({ code: '600001.SH', decisionAt: '2026-09-30T09:40:00.123456+08:00', signalPrice: 10, firstTradablePrice: 99 })
  const second = Object.freeze({ code: 'sh600001', decisionAt: '2026-09-30T09:55:00+08:00', signalPrice: 12 })
  const up = signalPriceComparison(first, quote), down = signalPriceComparison(second, quote)
  assert.equal(up.delta, 1)
  assert.equal(up.percent, .1)
  assert.equal(up.direction, 'up')
  assert.equal(ledgerSigned(up.delta), '+1.00')
  assert.equal(ledgerSigned(up.percent * 100), '+10.00')
  assert.equal(down.delta, -1)
  assert.equal(down.direction, 'down')
  assert.equal(ledgerSigned(down.percent * 100), '-8.33')
  assert.equal(first.signalPrice, 10)
  const flat = signalPriceComparison(first, { ...quote, price: 10 })
  assert.equal(flat.available, true)
  assert.equal(flat.direction, 'flat')
  assert.equal(ledgerSigned(flat.percent), '0.00')
})

test('unavailable prices or timestamps never create a zero or invented profit', () => {
  const signal = { code: '600001', decisionAt: '2026-09-30T10:00:00+08:00', signalPrice: 10 }
  const quote = { code: '600001', price: 11, sourceTime: '2026-09-30T10:30:00+08:00' }
  for (const missing of [0, -1, null, undefined, '', 'bad', NaN, Infinity, true]) {
    assert.equal(signalPriceComparison({ ...signal, signalPrice: missing }, quote).available, false)
    assert.equal(signalPriceComparison(signal, { ...quote, price: missing }).available, false)
  }
  assert.equal(ledgerPrice(Infinity), '—')
  assert.equal(ledgerSigned(null), '—')
  assert.equal(ledgerSigned(-0.00001), '0.00')
  assert.equal(signalPriceComparison(signal, null).latest, null)
  assert.equal(signalPriceComparison(signal, { ...quote, code: '000001' }).available, false)
  assert.match(signalPriceComparison({ ...signal, decisionAt: '' }, quote).reason, /入选时间/)
  assert.match(signalPriceComparison(signal, { ...quote, sourceTime: '' }).reason, /行情时间/)
  assert.equal(signalPriceComparison(signal, { ...quote, sourceTime: '2026-09-30T10:30:00' }).available, false)
})

test('a newly fetched quote that predates a particular selection cannot be used for that selection', () => {
  const quote = { code: '600001', price: 11, sourceTime: '2026-09-30T09:50:00+08:00', fetchedAt: '2026-09-30T12:00:00+08:00' }
  const early = { code: '600001', signalPrice: 10, decisionAt: '2026-09-30T09:40:00+08:00' }
  const later = { ...early, signalPrice: 12, decisionAt: '2026-09-30T09:55:00+08:00' }
  assert.equal(signalPriceComparison(early, quote).available, true)
  const comparison = signalPriceComparison(later, quote)
  assert.equal(comparison.latest, 11)
  assert.equal(comparison.delta, null)
  assert.equal(comparison.percent, null)
  assert.match(comparison.reason, /早于本次精选/)
  assert.equal(signalPriceComparison(early, { ...quote, sourceTime: early.decisionAt }).available, true)
})

test('quote lookup deduplicates valid market codes and reports partial missing data', async () => {
  assert.equal(recordQuoteCode('sh600001'), '600001')
  assert.equal(recordQuoteCode('600001.SH'), '600001')
  assert.equal(recordQuoteCode('SH000001'), '')
  for (const unsupported of ['920001', '430001.BJ', 'bj830001']) assert.equal(recordQuoteCode(unsupported), '')
  assert.deepEqual(recordQuoteCodes(['600001', '600001.SH', 'sz000001', '', 'AAPL', '600001;cmd']), ['600001', '000001'])
  const states = [], calls = []
  const loader = createRecordQuoteLoader({
    fetchQuotes: async codes => { calls.push(codes); return { quotes: { '600001.SH': { price: 10, sourceTime: '2026-09-30T10:00:00+08:00' } } } },
    onState: value => states.push(value),
  })
  await loader.load(['sh600001', '600001.SH', '000001', 'invalid'])
  assert.deepEqual(calls, [['600001', '000001']])
  assert.equal(states.at(-1).loading, false)
  assert.equal(states.at(-1).quotes['600001'].code, '600001')
  assert.match(states.at(-1).error, /部分股票/)
  await loader.load(['invalid'])
  assert.equal(calls.length, 1)
  assert.deepEqual(states.at(-1).quotes, {})
})

test('quote refresh rejects late responses after another query, cancellation, or page disposal', async () => {
  const requests = [], states = []
  const loader = createRecordQuoteLoader({
    fetchQuotes: codes => new Promise(resolve => requests.push({ codes, resolve })),
    onState: value => states.push(value),
  })
  const first = loader.load(['600001']), second = loader.load(['000001'])
  requests[1].resolve({ quotes: { '000001': { code: '000001', price: 12 } } }); await second
  requests[0].resolve({ quotes: { '600001': { code: '600001', price: 99 } } }); await first
  assert.deepEqual(Object.keys(states.at(-1).quotes), ['000001'])
  const cancelled = loader.load(['600001'])
  loader.cancel({ clear: true })
  requests[2].resolve({ quotes: { '600001': { code: '600001', price: 99 } } }); await cancelled
  assert.deepEqual(states.at(-1).quotes, {})
  const disposed = loader.load(['600001'])
  loader.dispose()
  const count = states.length
  requests[3].resolve({ quotes: { '600001': { code: '600001', price: 99 } } }); await disposed
  assert.equal(states.length, count)
})

test('network errors retain previous quote timestamps and permit retry without modifying saved signals', async () => {
  let calls = 0
  const states = []
  const quote = { code: '600001', price: 11, sourceTime: '2026-09-30T10:00:00+08:00' }
  const loader = createRecordQuoteLoader({
    fetchQuotes: async () => { if (++calls === 2) throw new Error('连接超时'); return { quotes: { '600001': quote } } },
    onState: value => states.push(value),
  })
  await loader.load(['600001'])
  await loader.load(['600001'])
  assert.equal(states.at(-1).loading, false)
  assert.match(states.at(-1).error, /连接超时.*原行情时间/)
  assert.deepEqual(states.at(-1).quotes['600001'], quote)
  await loader.load(['600001'])
  assert.equal(states.at(-1).error, '')
})
