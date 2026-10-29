import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { compileScript, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import * as ledger from './pickLedger.mjs'
import * as recordRead from './recordRead.mjs'
import * as learningStatus from './learningStatus.mjs'
import * as recommendationQuality from './recommendationQuality.mjs'

const flush = async () => { for (let n = 0; n < 12; n++) await Promise.resolve(); await vue.nextTick() }

// Run the real SFC setup and Vue watchers without a browser or the Wails bridge.
function mountLedger(api, initialMode = 'records', options = {}) {
  const source = readFileSync(new URL('../components/PicksLedger.vue', import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  const modules = { vue, '../../wailsjs/go/main/App': api, '../utils/pickLedger.mjs': ledger, '../utils/recordRead.mjs': recordRead, '../utils/learningStatus.mjs': learningStatus, '../utils/recommendationQuality.mjs': recommendationQuality }
  const compiled = compileScript(descriptor, { id: 'ledger-persistence-test' }).content
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
  component.render = function () { instance = vue.getCurrentInstance(); return null }
  const active = vue.ref(initialMode !== 'records' || options.scope === 'current')
  const refreshToken = vue.ref(0)
  const display = vue.reactive({ displayRunId: '', displayAsOf: '', displayHasOpportunities: false, ...options })
  const renderer = vue.createRenderer({
    createElement: type => ({ type }), createText: text => ({ text }), createComment: text => ({ text }),
    insert() {}, remove() {}, setElementText() {}, setText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null,
  })
  const app = renderer.createApp({ setup: () => () => vue.h(component, { mode: initialMode, active: active.value, refreshToken: refreshToken.value, ...display }) })
  app.mount({})
  if (initialMode === 'records' && options.scope !== 'current') { instance.setupState.recordsScope = 'history'; active.value = true }
  return { active, refreshToken, display, timers, state: () => instance.setupState, dispose: () => app.unmount() }
}

test('returning to historical recommendation records updates only prices and preserves filters, order, and original signals', async () => {
  let historyCalls = 0, quoteCalls = 0
  const saved = { items: [{ runId: 'scan-1', asOf: '2026-10-08T10:30:00+08:00', signals: [
    { signalId: 'first', code: '600001', selected: true, signalPrice: 10, rank: 1 },
    { signalId: 'second', code: '000001', selected: true, signalPrice: 12, rank: 2 },
  ] }] }
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: async () => { historyCalls++; return structuredClone(saved) },
    GetStockKingRecordQuotes: async () => ({ quotes: { '600001': { price: 10 + ++quoteCalls, sourceTime: '2026-10-08T14:00:00+08:00' } } }),
  })
  try {
    await flush()
    const state = mounted.state(), original = state.payload
    state.date = '2026-10-08'; state.symbol = '600001'; state.version = 'v1.2'; state.includeControls = true
    await flush()
    const filtered = state.payload
    mounted.active.value = false; await flush()
    assert.equal(mounted.timers.size, 0)
    mounted.active.value = true; await flush()
    assert.equal(historyCalls, 2)
    assert.ok(quoteCalls > 1)
    assert.notEqual(filtered, original)
    assert.equal(state.payload, filtered)
    assert.deepEqual(state.payload, saved)
    assert.deepEqual([state.date, state.symbol, state.version, state.includeControls], ['2026-10-08', '600001', 'v1.2', true])
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
    GetStockKingRecommendationHistory: async () => ({ items: [{ code: '600001', selected: true, signalPrice: 10 }] }),
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
    GetStockKingRecommendationHistory: async () => { if (++calls > 1) throw new Error('temporary failure'); return { items: [{ signalId: 'saved', selected: true }] } },
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

test('the real ledger presents the next-day score and target independently of the old trade score', async () => {
  const nextDay = { selected: true, score: 61.5, targetSession: '2026-10-09', reasons: ['当日承接'], version: 'next-day-limit-watch-v1' }
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: async () => ({ items: [{ signalId: 'next', selected: false, code: '600001', snapshot: { finalScore: 99, rank: 4, nextDaySignal: nextDay } }] }),
    GetStockKingRecordQuotes: async () => ({ quotes: {} }),
  })
  try {
    await flush()
    const visible = mounted.state().displayGroups[0].signals[0]
    assert.equal(visible.selected, false)
    assert.equal(visible.nextDay.score, 61.5)
    assert.equal(visible.nextDay.targetSession, '2026-10-09')
    assert.deepEqual(visible.nextDay.reasons, ['当日承接'])
    assert.equal(visible.snapshot.finalScore, 99)
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
  const mounted = mountLedger({ GetStockKingRecommendationHistory: async (...args) => { queries.push(args); return { items: [{ signalId: `run-${++calls}`, runId: 'display-run', selected: true }] } }, GetStockKingRecordQuotes: async () => ({ quotes: {} }) }, 'records', { displayRunId: 'display-run' })
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
    assert.equal(mounted.state().payload.items[0].signalId, 'run-2')
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
      return calls === 1 ? new Promise(resolve => { resolveOld = resolve }) : Promise.resolve({ items: [{ signalId: 'retry', selected: true }] })
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
    assert.equal(mounted.state().payload.items[0].signalId, 'retry')
    resolveOld({ items: [{ signalId: 'late', selected: true }] }); await flush()
    assert.equal(mounted.state().payload.items[0].signalId, 'retry')
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


test('current records follow displayed runs, discard stale replies, and keep empty opportunity rounds empty', async () => {
  const pending = new Map(), queries = []
  const mounted = mountLedger({
    GetStockKingRecommendationHistory: (...args) => { queries.push(args); return new Promise(resolve => pending.set(args[4], resolve)) },
    GetStockKingRecordQuotes: async () => ({ quotes: {} }),
  }, 'records', { scope: 'current', displayRunId: 'display-1', displayAsOf: '2026-10-28T18:30:00Z', displayHasOpportunities: true })
  const packet = (id, code, opportunity = true) => ({ items: [{ runId: `ledger-${id}`, displayRunId: id, signals: [
    { signalId: `signal-${id}`, code, selected: false, opportunitySelected: opportunity, nextDaySelected: !opportunity,
      snapshot: { opportunity: { channel: 'M', grade: 'M-A' } } },
  ] }] })
  try {
    await flush()
    assert.deepEqual(queries[0], ['2026-10-29', '', '', false, 'display-1'])
    mounted.display.displayRunId = 'display-2'; await flush()
    pending.get('display-2')(packet('display-2', '603517')); await flush()
    assert.deepEqual(mounted.state().groups.map(group => group.code), ['603517'])
    assert.equal(mounted.state().displayGroups[0].signals[0].opportunity.grade, 'M-A')
    pending.get('display-1')(packet('display-1', '600001')); await flush()
    assert.deepEqual(mounted.state().groups.map(group => group.code), ['603517'])
    mounted.active.value = false; await flush()
    mounted.display.displayRunId = 'display-empty'; await flush()
    assert.equal(mounted.state().groups.length, 0)
    assert.equal(pending.has('display-empty'), false, 'hidden views wait to read but cannot display the previous round')
    mounted.active.value = true; await flush()
    pending.get('display-empty')(packet('display-empty', '600002', false)); await flush()
    assert.equal(mounted.state().groups.length, 0, 'empty opportunity rounds cannot fall back to next-day observations')
    mounted.state().recordsScope = 'history'; await flush()
    assert.deepEqual(queries.at(-1), ['', '', '', false, ''])
    pending.get('')({ items: [...packet('display-2', '603517').items, ...packet('display-empty', '600002', false).items] }); await flush()
    assert.deepEqual(mounted.state().groups.map(group => group.code).sort(), ['600002', '603517'])
  } finally { mounted.dispose() }
})
