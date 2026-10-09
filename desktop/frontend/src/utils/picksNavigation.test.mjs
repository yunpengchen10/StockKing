import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { compileScript, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import * as ledger from './pickLedger.mjs'

const flush = async () => { for (let n = 0; n < 12; n++) await Promise.resolve(); await vue.nextTick() }

// Run the real SFC setup and Vue watchers without a browser or the Wails bridge.
function mountLedger(api, initialMode = 'records') {
  const source = readFileSync(new URL('../components/PicksLedger.vue', import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  const modules = { vue, '../../wailsjs/go/main/App': api, '../utils/pickLedger.mjs': ledger }
  const compiled = compileScript(descriptor, { id: 'ledger-persistence-test' }).content
    .replace(/^import\s+(.+?)\s+from\s+['"](.+?)['"];?$/gm, (_, bindings, path) => `const ${bindings.replace(/\bas\b/g, ':')} = modules[${JSON.stringify(path)}]`)
    .replace('export default', 'return')
  const timers = new Map()
  let nextTimer = 0, instance
  const component = new Function('modules', 'setTimeout', 'clearTimeout', 'document', compiled)(modules,
    (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, delay }); return id },
    id => timers.delete(id), { visibilityState: 'visible' })
  component.render = function () { instance = vue.getCurrentInstance(); return null }
  const active = vue.ref(true)
  const refreshToken = vue.ref(0)
  const renderer = vue.createRenderer({
    createElement: type => ({ type }), createText: text => ({ text }), createComment: text => ({ text }),
    insert() {}, remove() {}, setElementText() {}, setText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null,
  })
  const app = renderer.createApp({ setup: () => () => vue.h(component, { mode: initialMode, active: active.value, refreshToken: refreshToken.value }) })
  app.mount({})
  return { active, refreshToken, timers, state: () => instance.setupState, dispose: () => app.unmount() }
}

test('returning to recommendation records updates only prices and preserves filters, order, and original signals', async () => {
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
    mounted.active.value = false; await flush()
    assert.equal(mounted.timers.size, 0)
    mounted.active.value = true; await flush()
    assert.equal(historyCalls, 1)
    assert.ok(quoteCalls > 1)
    assert.equal(state.payload, original)
    assert.deepEqual(state.payload, saved)
    assert.deepEqual([state.date, state.symbol, state.version, state.includeControls], ['2026-10-08', '600001', 'v1.2', true])
    assert.equal(mounted.timers.size, 1)
    await state.refresh(); await flush()
    assert.equal(historyCalls, 2)
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

test('the explicit latest-records action queries once while ordinary returns keep the saved record list', async () => {
  let calls = 0
  const queries = []
  const mounted = mountLedger({ GetStockKingRecommendationHistory: async (...args) => { queries.push(args); return { items: [{ signalId: `run-${++calls}`, selected: true }] } }, GetStockKingRecordQuotes: async () => ({ quotes: {} }) })
  try {
    await flush()
    mounted.state().date = '2026-09-01'; mounted.state().symbol = '600001'; mounted.state().version = 'v1.1'
    mounted.active.value = false; await flush()
    mounted.active.value = true; await flush()
    assert.equal(calls, 1)
    assert.deepEqual([mounted.state().date, mounted.state().symbol, mounted.state().version], ['2026-09-01', '600001', 'v1.1'])
    mounted.refreshToken.value++; await flush()
    assert.equal(calls, 2)
    assert.deepEqual(queries.at(-1), ['', '', ''])
    assert.deepEqual([mounted.state().date, mounted.state().symbol, mounted.state().version], ['', '', ''])
    assert.equal(mounted.state().payload.items[0].signalId, 'run-2')
    mounted.active.value = false; await flush()
    mounted.active.value = true; await flush()
    assert.equal(calls, 2)
  } finally { mounted.dispose() }
})
