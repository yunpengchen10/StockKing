import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import * as ledger from './pickLedger.mjs'
import * as recordRead from './recordRead.mjs'
import * as learningStatus from './learningStatus.mjs'
import * as recommendationQuality from './recommendationQuality.mjs'

const flush = async () => { for (let n = 0; n < 12; n++) await Promise.resolve(); await vue.nextTick() }

function publication(runId, codes = ['600001'], asOf = '2026-10-08T10:30:00+08:00') {
  return { runId, asOf, publishedAt: asOf, version: 'potential-picks-v1', status: 'published',
    items: codes.map((code, index) => ({ rank: index + 1, code, name: '已发布' + code, price: 10 + index,
      sourceTime: asOf, source: 'sina', basisLabel: '盘中量价', reasons: [code + '站稳成交均价'], risk: code + '跌破支撑' })) }
}
const records = (...scans) => ({ recordsVersion: 'published-picks-v1', items: scans })

test('channel filtering uses frozen hits and keeps both channel ranks, source and the original recommendation price', async () => {
  const round = publication('channels', ['600001', '600002'])
  const legacy = publication('legacy', ['600003'])
  const channels = [{ id: 'trend_space', name: '趋势空间', version: '1', rank: 2 },
    { id: 'countertrend_leader', name: '逆势领涨', version: '2', rank: 1 }]
  Object.assign(round, { official: true, strategyName: '全部打法', ruleSnapshots: channels, channels })
  for (const [i, row] of round.items.entries()) row.strategyMatches = (i ? channels.slice(0, 1) : channels)
    .map(match => ({ ...match, reasons: ['当时触发'], evidence: [{ label: '分钟成交额', value: 900, unit: '元', source: '归档' }] }))
  const response = { ...records(round, legacy), strategyOutcomes: { groups: [
    { ...channels[1], total: 2, observed: 1, up: 1, bigUp: 0, bigDown: 0 }] } }
  const mount = mountLedger({ GetStockKingRecommendationHistory: async () => structuredClone(response),
    GetStockKingRecordQuotes: async () => ({ quotes: {} }) })
  try {
    await flush()
    assert.match(mount.text(), /历史轮次未记录打法/)
    let state = mount.state()
    state.channelFilter = JSON.stringify(['countertrend_leader', '2'])
    await flush()
    assert.deepEqual(state.quoteCodes, ['600001'])
    assert.equal(state.publications[0].items[0].price, 10)
    assert.equal(state.payload.items[0].items.length, 2)
    for (const label of ['定时扫描', '趋势空间 · 通道第 2 名', '逆势领涨 · 通道第 1 名', '分钟成交额', '100.0%', '900元']) assert.ok(mount.text().includes(label), label)
    assert.doesNotMatch(mount.text(), /已发布600002|已发布600003/)
  } finally { mount.dispose() }
})

// Run the real SFC setup and Vue watchers without a browser or the Wails bridge.
function mountLedger(api, initialMode = 'records', options = {}) {
  const source = readFileSync(new URL('../components/PicksLedger.vue', import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  const modules = { vue, '../../wailsjs/go/main/App': api, '../utils/pickLedger.mjs': ledger, '../utils/recordRead.mjs': recordRead, '../utils/learningStatus.mjs': learningStatus, '../utils/recommendationQuality.mjs': recommendationQuality }
  const script = compileScript(descriptor, { id: 'ledger-persistence-test' })
  const compiled = script.content
    .replace(/^import\s+(.+?)\s+from\s+['"](.+?)['"];?$/gm, (_, bindings, path) => `const ${bindings.replace(/\bas\b/g, ':')} = modules[${JSON.stringify(path)}]`)
    .replace('export default', 'return')
  const timers = new Map()
  let nextTimer = 0, instance
  // Model browser receiver checks: copying a native timer into a plain object
  // and invoking it as that object's method throws, unlike Node's timers.
  function setBrowserTimeout(callback, delay) {
    if (this !== undefined) throw new TypeError('Illegal invocation')
    const id = ++nextTimer; timers.set(id, { callback, delay }); return id
  }
  function clearBrowserTimeout(id) {
    if (this !== undefined) throw new TypeError('Illegal invocation')
    timers.delete(id)
  }
  const component = new Function('modules', 'setTimeout', 'clearTimeout', 'document', compiled)(modules,
    setBrowserTimeout, clearBrowserTimeout, { visibilityState: 'visible' })
  const compiledTemplate = compileTemplate({ source: descriptor.template.content, filename: 'PicksLedger.vue',
    id: 'ledger-persistence-test', compilerOptions: { bindingMetadata: script.bindings } }).code
    .replace(/^import\s+(.+?)\s+from\s+['"]vue['"];?$/gm, (_, bindings) => `const ${bindings.replace(/\bas\b/g, ':')} = vue`)
    .replace('export function render', 'return function render')
  const render = new Function('vue', compiledTemplate)({...vue, vModelSelect: {}, vModelText: {}})
  component.render = function (...args) { instance = vue.getCurrentInstance(); return render.apply(this, args) }
  const active = vue.ref(initialMode !== 'records' || options.scope === 'current')
  const refreshToken = vue.ref(0)
  const display = vue.reactive({ displayRunId: '', displayAsOf: '', displayHasOpportunities: false, ...options })
  const node = (type, text = '') => ({ type, text, props: {}, children: [], parent: null })
  const renderer = vue.createRenderer({
    createElement: type => node(type), createText: text => node('text', text), createComment: text => node('comment', text),
    insert(child, parent, anchor = null) {
      if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1)
      child.parent = parent
      const index = anchor ? parent.children.indexOf(anchor) : -1
      if (index < 0) parent.children.push(child)
      else parent.children.splice(index, 0, child)
    },
    remove(child) {
      if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1)
      child.parent = null
    },
    setElementText(element, text) { element.text = text; element.children = [] },
    setText(element, text) { element.text = text },
    patchProp(element, key, _previous, next) { element.props[key] = next },
    parentNode: element => element.parent,
    nextSibling: element => element.parent?.children[element.parent.children.indexOf(element) + 1] || null,
  })
  const app = renderer.createApp({ setup: () => () => vue.h(component, { mode: initialMode, active: active.value, refreshToken: refreshToken.value, ...display }) })
  for (const tag of new Set([...descriptor.template.content.matchAll(/<(n-[a-z-]+)/g)].map(match => match[1]))) {
    app.component(tag, { setup: (_, {slots}) => () => vue.h('div', slots.default?.()) })
  }
  const root = node('root')
  app.mount(root)
  const visibleText = element => element.type === 'comment' ? '' : element.text + element.children.map(visibleText).join('')
  if (initialMode === 'records' && options.scope !== 'current') { instance.setupState.recordsScope = 'history'; active.value = true }
  return { active, refreshToken, display, timers, text: () => visibleText(root), state: () => instance.setupState, dispose: () => app.unmount() }
}

test('returning to historical recommendation records updates prices and preserves the frozen order, prices, and reasons', async () => {
  let historyCalls = 0, quoteCalls = 0
  const saved = records(publication('scan-1', ['600001', '000001']))
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: async () => { historyCalls++; return structuredClone(saved) },
    GetStockKingRecordQuotes: async () => ({ quotes: { '600001': { price: 10 + ++quoteCalls, sourceTime: '2026-10-08T14:00:00+08:00' } } }),
  })
  try {
    await flush()
    const state = mounted.state(), original = state.payload
    state.date = '2026-10-08'; state.symbol = '600001'; state.version = 'potential-picks-v1'
    await state.refresh(); await flush()
    const filtered = state.payload
    mounted.active.value = false; await flush()
    assert.equal(mounted.timers.size, 0)
    mounted.active.value = true; await flush()
    assert.equal(historyCalls, 2)
    assert.ok(quoteCalls > 1)
    assert.notEqual(filtered, original)
    assert.equal(state.payload, filtered)
    assert.deepEqual(state.payload, saved)
    assert.deepEqual(state.publications, saved.items)
    assert.deepEqual([state.date, state.symbol, state.version], ['2026-10-08', '600001', 'potential-picks-v1'])
    assert.equal(mounted.timers.size, 1)
    await state.refresh(); await flush()
    assert.equal(historyCalls, 3)
  } finally { mounted.dispose() }
})

test('each ledger tab retains its own data and filters until the user queries it again', async () => {
  let historyCalls = 0, reviewCalls = 0
  const api = {
    GetStockKingRecommendationHistory: async () => { historyCalls++; return { items: [] } },
    GetStockKingDelayedReviews: async () => { reviewCalls++; return { items: [{ signalId: 'review-1' }] } },
    GetStockKingRecordQuotes: async () => ({ quotes: {} }),
  }
  const records = mountLedger(api), reviews = mountLedger(api, 'reviews')
  try {
    await flush()
    records.state().symbol = '600001'; reviews.state().symbol = '000001'
    records.active.value = false; await flush()
    reviews.active.value = false; records.active.value = true; await flush()
    assert.equal(records.state().symbol, '600001')
    assert.equal(reviews.state().symbol, '000001')
    assert.equal(reviews.state().payload.items[0].signalId, 'review-1')
    assert.equal(historyCalls, 1); assert.equal(reviewCalls, 1)
  } finally { records.dispose(); reviews.dispose() }
})

test('a quote reply from a deactivated view cannot overwrite prices or create duplicate polling loops', async () => {
  const requests = []
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: async () => records(publication('scan-1')),
    GetStockKingRecordQuotes: () => new Promise(resolve => requests.push(resolve)),
  })
  try {
    await flush()
    mounted.active.value = false; await flush()
    mounted.active.value = true; await flush()
    assert.equal(requests.length, 2)
    requests[1]({ quotes: { '600001': { price: 12 } } }); await flush()
    requests[0]({ quotes: { '600001': { price: 11 } } }); await flush()
    assert.equal(mounted.state().quotes['600001'].price, 12)
    assert.equal(mounted.timers.size, 1)
  } finally { mounted.dispose() }
})

test('an unsuccessful manual query leaves the visible record snapshot intact', async () => {
  let calls = 0
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: async () => { if (++calls > 1) throw new Error('temporary failure'); return records(publication('saved')) },
    GetStockKingRecordQuotes: async () => ({ quotes: {} }),
  })
  try {
    await flush()
    const snapshot = mounted.state().payload
    await mounted.state().refresh(); await flush()
    assert.equal(mounted.state().payload, snapshot)
    assert.match(mounted.state().error, /temporary failure/)
    assert.equal(mounted.timers.size, 1, 'quote updates continue after a failed history query')
  } finally { mounted.dispose() }
})

test('historical records show machine publications, omitting signals, controls, and unversioned legacy rows', async () => {
  const first = publication('10:30', ['600008', '600001'])
  const next = publication('14:55', ['600001'])
  const legacy = { runId: 'old', signals: [{code: '000099', selected: true, snapshot: {finalScore: 99}}] }
  let result = { items: [legacy] }
  const quoteCodes = []
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: async () => structuredClone(result),
    GetStockKingRecordQuotes: async codes => { quoteCodes.push(codes); return {quotes: {}} },
  })
  try {
    await flush()
    const state = mounted.state()
    assert.deepEqual(state.visiblePublications, [])
    result = records(first, legacy, {...next, runId: 'never-published', status: 'unrecorded'}, next)
    await state.refresh(); await flush()
    assert.deepEqual(state.publications.map(scan => [scan.runId, scan.items.map(row => [row.code, row.rank])]),
      [['10:30', [['600008', 1], ['600001', 2]]], ['14:55', [['600001', 1]]]])
    assert.deepEqual(state.publications[0].items[1].reasons, first.items[1].reasons)
    assert.ok(quoteCodes.flat().length > 0)
    assert.ok(!quoteCodes.flat().includes('000099'))
  } finally { mounted.dispose() }
})

test('next-day price-pattern reviews remain visible when the trading review list is empty', async () => {
  const result = { items: [], nextDayReview: { items: [{ signalId: 'next', code: '600001', selected: true, status: 'incomplete', touchLimit: null }] } }
  const mounted = mountLedger({ GetStockKingDelayedReviews: async () => result }, 'reviews')
  try {
    await flush()
    assert.equal(mounted.state().reviews.length, 0)
    assert.equal(mounted.state().nextDayReviews.length, 1)
    assert.equal(mounted.state().nextDayReviews[0].status, 'incomplete')
  } finally { mounted.dispose() }
})

test('the explicit current-records action follows picks while preserving historical filters', async () => {
  let calls = 0
  const queries = []
  const mounted = mountLedger({ GetStockKingRecommendationHistory: async (...args) => { queries.push(args); calls++; return records(publication('display-run')) }, GetStockKingRecordQuotes: async () => ({ quotes: {} }) }, 'records', { displayRunId: 'display-run' })
  try {
    await flush()
    mounted.state().date = '2026-09-01'; mounted.state().symbol = '600001'; mounted.state().version = 'v1.1'
    mounted.active.value = false; await flush()
    mounted.active.value = true; await flush()
    assert.equal(calls, 1)
    assert.deepEqual([mounted.state().date, mounted.state().symbol, mounted.state().version], ['2026-09-01', '600001', 'v1.1'])
    mounted.refreshToken.value++; await flush()
    assert.equal(calls, 2)
    assert.deepEqual(queries.at(-1), ['', '', '', false, 'display-run'])
    assert.deepEqual([mounted.state().date, mounted.state().symbol, mounted.state().version], ['2026-09-01', '600001', 'v1.1'])
    assert.equal(mounted.state().recordsScope, 'current')
    assert.equal(mounted.state().payload.items[0].runId, 'display-run')
    mounted.active.value = false; await flush()
    mounted.active.value = true; await flush()
    assert.equal(calls, 3)
  } finally { mounted.dispose() }
})

test('a pending Wails history call stops spinning and can be retried without accepting its late reply', async () => {
  let resolveOld, calls = 0
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: (...args) => {
      assert.equal(args[3], false)
      calls++
      return calls === 1 ? new Promise(resolve => { resolveOld = resolve }) : Promise.resolve(records(publication('retry')))
    },
    GetStockKingRecordQuotes: async () => ({ quotes: {} }),
  })
  try {
    await flush()
    assert.equal(mounted.state().loading, true)
    const deadline = [...mounted.timers.values()].find(timer => timer.delay === 20000)
    assert.ok(deadline, 'record read must have a browser deadline')
    deadline.callback(); await flush()
    assert.equal(mounted.state().loading, false)
    assert.match(mounted.state().error, /记录读取超时/)
    await mounted.state().refresh(); await flush()
    assert.equal(mounted.state().error, '')
    assert.equal(mounted.state().publications[0].runId, 'retry')
    resolveOld(records(publication('late'))); await flush()
    assert.equal(mounted.state().publications[0].runId, 'retry')
  } finally { mounted.dispose() }
})

test('next-day price summaries show selected losses and keep controls separate', async () => {
  const selected = { version: 'frozen', queue: 'watch', selected: true, signalCount: 1,
    maturePatterns: 1, priceObservationCount: 1, upCount: 0, downCount: 1,
    meanObservedReturnFromSignalPct: -3.25, statusCounts: { mature_price_pattern: 1 } }
  const control = { ...selected, selected: false, downCount: 0, upCount: 1, meanObservedReturnFromSignalPct: 2.5 }
  const mounted = mountLedger({
    GetStockKingDelayedReviews: async () => ({ items: [], nextDayReview: { items: [], summary: { groups: [selected, control] } } }),
    GetStockKingLearningState: async () => ({}),
  }, 'reviews')
  try {
    await flush()
    assert.equal(mounted.state().nextDaySummaryGroups.length, 1)
    assert.equal(mounted.state().nextDaySummaryGroups[0].downCount, 1)
    assert.equal(mounted.state().nextDaySummaryGroups[0].meanObservedReturnFromSignalPct, -3.25)
    mounted.state().includeControls = true; await flush()
    assert.equal(mounted.state().nextDaySummaryGroups.length, 2)
    assert.equal(mounted.state().nextDaySummaryGroups[1].selected, false)
  } finally { mounted.dispose() }
})


test('a delayed history response cannot replace the newer machine publication or a published empty list', async () => {
  const pending = new Map(), queries = []
  const asOf = '2026-10-28T18:30:00Z'
  const initial = publication('display-1', ['600001'], asOf)
  const second = publication('display-2', ['603517'], asOf)
  const empty = publication('display-empty', [], asOf)
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: (...args) => { queries.push(args); return new Promise(resolve => pending.set(args[4], resolve)) },
    GetStockKingRecordQuotes: async () => ({ quotes: {} }),
  }, 'records', { scope: 'current', displayRunId: initial.runId, publication: initial, displayAsOf: asOf, displayedStocks: initial.items })
  try {
    await flush()
    assert.deepEqual(queries[0], ['2026-10-29', '', '', false, initial.runId])
    Object.assign(mounted.display, {displayRunId: second.runId, publication: second, displayedStocks: second.items})
    await flush()
    pending.get(second.runId)(records(second)); await flush()
    const shown = () => mounted.state().publications.flatMap(scan => scan.items.map(row => row.code))
    assert.deepEqual(shown(), ['603517'])
    pending.get(initial.runId)(records(initial)); await flush()
    assert.deepEqual(shown(), ['603517'])
    mounted.active.value = false; await flush()
    Object.assign(mounted.display, {displayRunId: empty.runId, publication: empty, displayedStocks: empty.items})
    await flush()
    assert.deepEqual(shown(), [])
    assert.equal(pending.has(empty.runId), false)
    mounted.active.value = true; await flush()
    pending.get(empty.runId)(records(empty)); await flush()
    assert.deepEqual(shown(), [])
    mounted.state().recordsScope = 'history'; await flush()
    assert.deepEqual(queries.at(-1), ['', '', '', false, ''])
    pending.get('')(records(second, empty)); await flush()
    assert.deepEqual(mounted.state().publications.map(scan => [scan.runId, scan.items.length]),
      [['display-2', 1], ['display-empty', 0]])
  } finally { mounted.dispose() }
})

test('current records visibly compare frozen recommendation prices with rising, falling, flat, and missing quotes', async () => {
  const saved = publication('price-comparison', ['600001', '000001', '600002', '000002'])
  const snapshot = structuredClone(saved)
  const quoteAt = '2026-10-08T14:55:00+08:00'
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: async () => records(saved),
    GetStockKingRecordQuotes: async () => ({ quotes: {
      '600001': { price: 12, source: 'tencent', sourceTime: quoteAt },
      '000001': { price: 10, source: 'sina', sourceTime: quoteAt },
      '600002': { price: 12, source: 'tencent', sourceTime: quoteAt },
    } }),
  }, 'records', { scope: 'current', displayRunId: saved.runId, publication: saved, displayedStocks: saved.items })
  try {
    await flush()
    const text = mounted.text()
    assert.equal((text.match(/推荐参考价/g) || []).length, 4)
    assert.equal((text.match(/最新报价¥/g) || []).length, 4)
    assert.equal((text.match(/较推荐价涨跌/g) || []).length, 4)
    assert.match(text, /推荐参考价¥10\.00/)
    assert.match(text, /最新报价¥12\.00tencent/)
    assert.match(text, /\+2\.00 元\/股 · \+20\.00%/)
    assert.match(text, /-1\.00 元\/股 · -9\.09%/)
    assert.match(text, /0\.00 元\/股 · 0\.00%/)
    assert.match(text, /最新报价¥—暂未取得报价/)
    assert.match(text, /最新报价未取得，暂不比较/)
    assert.match(text, /2026\/10\/8 14:55:00/)
    assert.deepEqual(mounted.state().visiblePublications[0].items.map(row => row.comparison.direction), ['up', 'down', 'flat', 'flat'])
    assert.deepEqual(saved, snapshot, 'live prices must never change saved price, order, or reasons')
  } finally { mounted.dispose() }
})

test('historical and restored close recommendations compare using their saved quote time, not publication or restoration time', async () => {
  const recovered = publication('recovered', ['600001'], '2026-10-08T15:30:00+08:00')
  recovered.publishedAt = null
  recovered.recordedAt = '2026-10-09T09:00:00+08:00'
  recovered.items[0].sourceTime = '2026-10-08T14:55:00+08:00'
  const close = publication('close', ['000001'], '2026-10-08T15:30:00+08:00')
  close.items[0].sourceTime = '2026-10-08'
  close.items[0].basisLabel = '最近完整交易日收盘'
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: async () => records(recovered, close),
    GetStockKingRecordQuotes: async () => ({ quotes: {
      '600001': { price: 11, source: 'tencent', sourceTime: '2026-10-08T15:00:00+08:00' },
      '000001': { price: 10, source: 'tencent', sourceTime: '2026-10-08T15:00:00+08:00' },
    } }),
  })
  try {
    await flush()
    assert.equal(mounted.state().recordsScope, 'history')
    assert.deepEqual(mounted.state().visiblePublications.map(round => round.items[0].comparison.available), [true, true])
    assert.match(mounted.text(), /\+1\.00 元\/股 · \+10\.00%/)
    assert.match(mounted.text(), /0\.00 元\/股 · 0\.00%/)
    assert.match(mounted.text(), /最近完整交易日收盘 · sina · 2026-10-08/)
  } finally { mounted.dispose() }
})

test('missing quote timestamps and quotes older than the saved price stay visible with comparison reasons', async () => {
  const saved = publication('evidence', ['600001', '000001'])
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: async () => records(saved),
    GetStockKingRecordQuotes: async () => ({ quotes: {
      '600001': { price: 12, source: 'tencent' },
      '000001': { price: 12, source: 'sina', sourceTime: '2026-10-07T15:00:00+08:00' },
    } }),
  })
  try {
    await flush()
    assert.equal((mounted.text().match(/最新报价¥12\.00/g) || []).length, 2)
    assert.match(mounted.text(), /行情时间未取得，暂不比较/)
    assert.match(mounted.text(), /行情早于本次精选，暂不比较/)
    assert.doesNotMatch(mounted.text(), /元\/股/)
  } finally { mounted.dispose() }
})

test('a stalled quote read stops spinning, can be retried, and ignores its late result', async () => {
  let resolveOld, calls = 0
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: async () => records(publication('quotes')),
    GetStockKingRecordQuotes: () => ++calls === 1 ? new Promise(resolve => { resolveOld = resolve })
      : Promise.resolve({ quotes: { '600001': { price: 11, source: 'tencent', sourceTime: '2026-10-08T15:00:00+08:00' } } }),
  })
  try {
    await flush()
    assert.equal(mounted.state().quotesLoading, true)
    const deadline = [...mounted.timers.values()].find(timer => timer.delay === 20000)
    assert.ok(deadline)
    deadline.callback(); await flush()
    assert.equal(mounted.state().quotesLoading, false)
    assert.match(mounted.text(), /超时/)
    assert.match(mounted.text(), /推荐参考价¥10\.00/)
    await mounted.state().refreshQuotes(); await flush()
    assert.equal(mounted.state().quotesError, '')
    assert.match(mounted.text(), /\+1\.00 元\/股 · \+10\.00%/)
    resolveOld({ quotes: { '600001': { price: 99, sourceTime: '2026-10-08T15:00:00+08:00' } } }); await flush()
    assert.equal(mounted.state().quotes['600001'].price, 11)
    assert.equal(mounted.timers.size, 1)
  } finally { mounted.dispose() }
})

test('history toolbar requests override current mode while retained history filters survive quote updates and reactivation', async () => {
  const queries = []
  const saved = publication('current')
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: async (...args) => { queries.push(args); return records(saved) },
    GetStockKingRecordQuotes: async () => ({ quotes: {} }),
  }, 'records', { scope: 'current', displayRunId: saved.runId, publication: saved, displayedStocks: saved.items })
  try {
    await flush()
    mounted.state().date = '2026-10-08'
    mounted.state().symbol = '600001'
    mounted.display.recordsView = 'history'; mounted.refreshToken.value++; await flush()
    assert.equal(mounted.state().recordsScope, 'history')
    assert.deepEqual(queries.at(-1), ['2026-10-08', '600001', '', false, ''])
    await mounted.state().refreshQuotes(); await flush()
    mounted.active.value = false; await flush()
    mounted.active.value = true; await flush()
    assert.equal(mounted.state().recordsScope, 'history')
    assert.equal(mounted.state().symbol, '600001')
    mounted.display.recordsView = 'current'; mounted.refreshToken.value++; await flush()
    assert.equal(mounted.state().recordsScope, 'current')
    assert.equal(queries.at(-1)[4], 'current')
  } finally { mounted.dispose() }
})

test('open history refreshes for each new published scan and rejects an older history reply without losing filters', async () => {
  const first = publication('first', ['600001'])
  const second = publication('second', ['000001'])
  const newest = publication('newest', ['600002'])
  const queries = [], pending = []
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: (...args) => {
      queries.push(args)
      if (queries.length === 1) return Promise.resolve(records(first))
      return new Promise(resolve => pending.push(resolve))
    },
    GetStockKingRecordQuotes: async () => ({ quotes: {} }),
  }, 'records', { displayRunId: first.runId })
  try {
    await flush()
    mounted.state().date = '2026-10-08'; mounted.state().version = 'potential-picks-v1'
    mounted.display.displayRunId = second.runId; await flush()
    assert.equal(mounted.state().recordsScope, 'history')
    assert.equal(mounted.state().loading, true)
    assert.match(mounted.text(), /已发布600001/)
    mounted.display.displayRunId = newest.runId; await flush()
    assert.deepEqual(queries.at(-1), ['2026-10-08', '', 'potential-picks-v1', false, ''])
    assert.equal(pending.length, 2)
    pending[1](records(newest, second, first)); await flush()
    assert.match(mounted.text(), /已发布600002/)
    pending[0](records(second, first)); await flush()
    assert.deepEqual(mounted.state().publications.map(round => round.runId), ['newest', 'second', 'first'])
    assert.match(mounted.text(), /已发布600002/)
    assert.equal(mounted.state().loading, false)
    assert.deepEqual([mounted.state().recordsScope, mounted.state().date, mounted.state().version],
      ['history', '2026-10-08', 'potential-picks-v1'])
  } finally { mounted.dispose() }
})

test('a hidden history view reads new publications on activation while preserving its scope and stock filter', async () => {
  const first = publication('first', ['600001'])
  const newest = publication('newest', ['600001'])
  newest.items[0].name = '最新轮推荐'
  const queries = []
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: async (...args) => { queries.push(args); return queries.length === 1 ? records(first) : records(newest, first) },
    GetStockKingRecordQuotes: async () => ({ quotes: {} }),
  }, 'records', { displayRunId: first.runId })
  try {
    await flush()
    mounted.state().symbol = '600001'
    mounted.active.value = false; await flush()
    mounted.display.displayRunId = newest.runId; await flush()
    assert.equal(queries.length, 1)
    assert.equal(mounted.state().publications[0].runId, first.runId)
    assert.equal(mounted.timers.size, 0)
    mounted.active.value = true; await flush()
    assert.equal(queries.length, 2)
    assert.deepEqual(queries.at(-1), ['', '600001', '', false, ''])
    assert.equal(mounted.state().recordsScope, 'history')
    assert.equal(mounted.state().symbol, '600001')
    assert.deepEqual(mounted.state().publications.map(round => round.runId), ['newest', 'first'])
    assert.match(mounted.text(), /最新轮推荐/)
  } finally { mounted.dispose() }
})
