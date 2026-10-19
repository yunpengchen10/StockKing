import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import * as refresh from './picksRefresh.mjs'
import * as recordRead from './recordRead.mjs'
import * as learningStatus from './learningStatus.mjs'
import * as pickExplain from './pickExplain.mjs'
import * as pickLedger from './pickLedger.mjs'
import * as premarket from './premarket.mjs'

const flush = async () => { for (let n = 0; n < 30; n++) await Promise.resolve(); await vue.nextTick() }
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const snapshot = (generatedAt, source = 'scheduled') => ({
  generatedAt, displaySnapshotVersion: 1, displaySource: source,
  adaptive: { generatedAt, candidates: [], status: 'completed_observations' },
})
const oldSnapshot = () => snapshot('2026-07-13T10:30:00+08:00', 'manual')
const latestSnapshot = () => snapshot('2026-10-13T10:30:00+08:00')

// Exercise the real setup, template, and KeepAlive lifecycle without a browser
// or Wails. Every timer, including those used by the imported utilities, is
// controlled here so unresolved bridge calls cannot keep the test runner alive.
function mountPicks(api = {}, cached = null, now = null) {
  const timers = new Map(), stored = new Map()
  let nextTimer = 0, instance
  const clock = {
    setTimeout: (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, delay }); return id },
    clearTimeout: id => timers.delete(id),
  }
  const storage = {
    getItem: key => stored.get(key) ?? null,
    setItem: (key, value) => stored.set(key, value),
    removeItem: key => stored.delete(key),
  }
  if (cached) refresh.saveDisplayedPicks(storage, cached)
  const bridge = {
    GetDisplayedKingPicks: async () => latestSnapshot(),
    GetEngineStatus: async () => ({ ready: true, state: 'ready' }),
    GetStockKingBackgroundStatus: async () => ({ status: 'idle' }),
    GetStockKingBackgroundLearning: async () => true,
    GetStockKingAutoRecommendations: async () => true,
    StartKingPicksRefresh: async () => { throw new Error('opening the page must not start a scan') },
    GetKingPicksRefreshTask: async () => { throw new Error('no task should require polling') },
    ...api,
  }
  const child = { render: () => null }
  const modules = {
    vue,
    'vue-router': { useRouter: () => ({ push() {} }) },
    'naive-ui': { useMessage: () => ({ success() {}, warning() {}, error() {} }) },
    '../../wailsjs/go/main/App': bridge,
    '../utils/symbol': { toResearchCode: value => value },
    './PickIndicatorEvidence.vue': child,
    './PicksLedger.vue': child,
    '../utils/learningStatus.mjs': learningStatus,
    '../utils/pickExplain.mjs': pickExplain,
    '../utils/pickLedger.mjs': pickLedger,
    '../utils/premarket.mjs': premarket,
    '../utils/picksRefresh.mjs': {
      ...refresh,
      createPicksRefreshController: options => refresh.createPicksRefreshController({ ...options, setTimer: clock.setTimeout, clearTimer: clock.clearTimeout }),
    },
    '../utils/recordRead.mjs': {
      ...recordRead,
      withRecordReadTimeout: (request, timeoutMs) => recordRead.withRecordReadTimeout(request, timeoutMs, clock),
    },
  }
  const source = readFileSync(new URL('../components/KingPicks.vue', import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  const script = compileScript(descriptor, { id: 'picks-display-test' })
  const imports = code => code.replace(/^import\s+(.+?)\s+from\s+['"](.+?)['"];?$/gm,
    (_, bindings, path) => `const ${bindings.replace(/\bas\b/g, ':')} = modules[${JSON.stringify(path)}]`)
  const componentDate = now === null ? Date : class extends Date {
    constructor(...args) { super(...(args.length ? args : [now])) }
    static now() { return now }
  }
  const component = new Function('modules', 'window', 'document', 'setTimeout', 'clearTimeout', 'Date', imports(script.content).replace('export default', 'return'))(
    modules, { localStorage: storage }, { visibilityState: 'visible' }, clock.setTimeout, clock.clearTimeout, componentDate)
  const template = compileTemplate({ source: descriptor.template.content, filename: 'KingPicks.vue', id: 'picks-display-test', compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  const render = new Function('modules', imports(template.code).replace('export function render', 'return function render'))(modules)
  component.render = function (...args) { instance = vue.getCurrentInstance(); return render.apply(this, args) }

  const node = (type, text = '') => ({ type, text, props: {}, style: {}, children: [], parent: null })
  const detach = child => {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1)
    child.parent = null
  }
  const renderer = vue.createRenderer({
    createElement: type => node(type), createText: text => node('text', text), createComment: text => node('comment', text),
    insert(child, parent, anchor = null) { detach(child); child.parent = parent; const index = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(index < 0 ? parent.children.length : index, 0, child) },
    remove: detach,
    setElementText(element, text) { element.text = text; element.children = [] },
    setText(element, text) { element.text = text },
    patchProp(element, key, _old, value) { element.props[key] = value },
    parentNode: element => element.parent,
    nextSibling: element => element.parent?.children[element.parent.children.indexOf(element) + 1] ?? null,
  })
  const active = vue.ref(true)
  const root = node('root')
  const app = renderer.createApp({ setup: () => () => vue.h(vue.KeepAlive, null, { default: () => active.value ? vue.h(component) : null }) })
  const passthrough = { props: ['show'], setup: (props, { slots }) => () => props.show === false ? null : vue.h('div', slots.default?.()) }
  const spin = { setup: (_, { slots }) => () => vue.h('div', slots.default?.()) }
  const card = { setup: (_, { slots }) => () => vue.h('div', [slots.header?.(), slots.default?.(), slots.footer?.()]) }
  const tabPane = { props: ['tab'], setup: (props, { slots }) => () => vue.h('div', [props.tab, slots.default?.()]) }
  for (const name of new Set([...descriptor.template.content.matchAll(/<(n-[\w-]+)/g)].map(match => match[1]))) app.component(name, name === 'n-spin' ? spin : name === 'n-card' ? card : name === 'n-tab-pane' ? tabPane : passthrough)
  app.mount(root)
  const textOf = element => element.type === 'comment' || element.style.display === 'none' ? '' : `${element.text}${element.children.map(textOf).join('')}`
  return {
    active, timers, storage,
    state: () => instance.setupState,
    setNow(value) { now = value },
    text: () => textOf(root),
    async runTimer(delay) {
      const timer = [...timers].find(([, item]) => item.delay === delay)
      assert.ok(timer, `expected a ${delay} ms timer`)
      timers.delete(timer[0]); timer[1].callback(); await flush()
    },
    dispose: () => { app.unmount(); timers.clear() },
  }
}

test('an old cached recommendation stays out of the visible result while the latest saved recommendation is loading', async () => {
  const pending = deferred(), cached = oldSnapshot()
  const mounted = mountPicks({ GetDisplayedKingPicks: () => pending.promise }, cached)
  try {
    await flush()
    assert.equal(mounted.state().restoring, true)
    assert.equal(mounted.state().result.generatedAt, '')
    assert.equal(mounted.state().displayedResult.generatedAt, cached.generatedAt)
    assert.match(mounted.text(), /正在读取最新已完成的推荐/)
    assert.ok(!mounted.text().includes(cached.generatedAt))
    pending.resolve(latestSnapshot()); await flush()
    assert.equal(mounted.state().restoring, false)
    assert.equal(mounted.state().result.generatedAt, latestSnapshot().generatedAt)
    assert.ok(mounted.text().includes(latestSnapshot().generatedAt))
  } finally { pending.resolve(null); mounted.dispose() }
})

test('a newer saved server recommendation replaces an older manually generated cache', async () => {
  const mounted = mountPicks({}, oldSnapshot())
  try {
    await flush()
    assert.equal(mounted.state().result.generatedAt, latestSnapshot().generatedAt)
    assert.equal(mounted.state().result.displaySource, 'scheduled')
    assert.equal(refresh.readDisplayedPicks(mounted.storage).generatedAt, latestSnapshot().generatedAt)
  } finally { mounted.dispose() }
})

test('auction plans are visible before minute bars exist, age out at 09:35, and retain their separate saved lane', async () => {
  const saved = snapshot('2026-10-16T09:22:00+08:00')
  saved.adaptive.premarketAssessment = { decisionAt: saved.generatedAt, sessionDate: '2026-10-16', baselineAsOf: '2026-10-15', expiresAt: '2026-10-16T09:35:00+08:00', phaseLabel: '集合竞价·不可撤单阶段', researchedCount: 5 }
  saved.adaptive.premarketCandidates = [{ code: '600001', name: '竞价预案样本', price: 10, referencePriceKind: 'previous_close', quoteStatus: 'missing', planStatus: 'overnight_plan', baselineAsOf: '2026-10-15', baselineSource: '昨夜日线', triggerPrice: 10.2, support: 9.5, triggerCondition: '价格在支撑上方承接', invalidCondition: '跌破支撑失效' }]
  saved.adaptive.recommendationPolicy = { version: 'primary-admission-v1' }
  const before = JSON.stringify(saved)
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved }, oldSnapshot(), Date.parse('2026-10-16T09:22:01+08:00'))
  try {
    await flush()
    assert.equal(mounted.state().premarketView.current, true)
    assert.equal(mounted.state().adaptivePicks.length, 0)
    assert.match(mounted.text(), /集合竞价 \/ 开盘预案 · 1 只/)
    assert.match(mounted.text(), /竞价预案样本/)
    assert.match(mounted.text(), /昨夜基准 2026-10-15/)
    assert.match(mounted.text(), /昨收基准价/)
    assert.match(mounted.text(), /未匹配委托/)
    assert.doesNotMatch(mounted.text(), /主推荐 0 只|缺少当日分钟|旧版候选 · 0/)
    assert.equal(refresh.readDisplayedPicks(mounted.storage).adaptive.premarketCandidates.length, 1)
    mounted.setNow(Date.parse('2026-10-16T09:35:00+08:00'))
    await mounted.runTimer(1000)
    assert.equal(mounted.state().premarketView.historical, true)
    assert.match(mounted.text(), /历史集合竞价 \/ 开盘预案/)
    assert.match(mounted.text(), /不是当前交易指示/)
    assert.match(mounted.text(), /历史快照 · 未做当前核验/)
    assert.match(mounted.text(), /主推荐 0 只/)
    assert.equal(JSON.stringify(saved), before, 'display expiry must not mutate saved evidence')
  } finally { mounted.dispose() }
})

test('the actual page leaves the primary list empty when only research or rejected candidates exist', async () => {
  const saved = latestSnapshot()
  saved.adaptive.recommendationPolicy = { version: 'primary-admission-v1' }
  saved.adaptive.primaryObjective = 'morning_signal_to_same_session_close'
  saved.adaptive.candidates = [{ code: '600001', name: 'Rejected', primaryAdmission: { eligible: false } }]
  saved.adaptive.nextDayWatchlist = [{ code: '600002', name: 'Research only', nextDaySignal: { queue: 'first_board', score: 95 } }]
  saved.adaptive.nextDayContinuationWatchlist = [{ code: '600003', name: 'Continuation', nextDaySignal: { queue: 'continuation', score: 99 } }]
  const before = JSON.stringify(saved)
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved })
  try {
    await flush()
    assert.equal(mounted.state().adaptivePicks.length, 0)
    assert.equal(mounted.state().savedRecommendations.saved, 0)
    assert.equal(mounted.state().nextDayWatchlist.length, 1)
    const text = mounted.text()
    assert.match(text, /主推荐 0 只/)
    assert.match(text, /缺数据或条件不满足时保持空榜/)
    assert.match(text, /首次推荐参考价到当日收盘/)
    assert.match(text, /T\+1持有风险单独评估/)
    assert.ok(text.indexOf('主推荐 0 只') < text.indexOf('研究观察 · 不计入主推荐'))
    assert.equal(JSON.stringify(saved), before, 'display cannot rewrite old evidence or eligibility')
  } finally { mounted.dispose() }
})

test('saved polling preserves a persisted first anchor and exposes invalidation without restoring the old candidate', async () => {
  const firstTime = '2026-10-13T09:40:00+08:00'
  const pick = { code: '600001', name: 'Anchored', primaryAdmission: { eligible: true }, firstReferenceTime: firstTime, firstReferencePrice: 10, referencePrice: 10.4, currentSignalState: 'new' }
  let saved = latestSnapshot()
  saved.adaptive = { ...saved.adaptive, recommendationPolicy: { version: 'primary-admission-v1' }, primaryObjective: 'morning_signal_to_same_session_close', candidates: [pick] }
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved })
  try {
    await flush()
    assert.equal(mounted.state().adaptiveCards[0].pick.firstReferencePrice, 10)
    saved = { ...snapshot('2026-10-13T11:00:00+08:00'), adaptive: { ...saved.adaptive, candidates: [{ ...pick, referencePrice: 11, currentSignalState: 'continuing' }] } }
    await mounted.runTimer(30000)
    assert.equal(mounted.state().adaptiveCards[0].pick.firstReferenceTime, firstTime)
    assert.equal(mounted.state().adaptiveCards[0].pick.firstReferencePrice, 10)
    assert.equal(mounted.state().adaptiveCards[0].pick.referencePrice, 11)
    assert.match(mounted.text(), /首次主推荐/)
    saved = { ...snapshot('2026-10-13T11:10:00+08:00'), adaptive: { ...saved.adaptive, candidates: [], invalidatedCandidates: [{ ...pick, currentSignalState: 'invalidated', primaryAdmission: { eligible: false, missingReasons: ['F_source_missing_stale_or_misaligned'], reasons: ['sector_resonance_not_confirmed'] } }] } }
    await mounted.runTimer(30000)
    assert.equal(mounted.state().adaptiveCards.length, 0)
    assert.equal(mounted.state().invalidatedPicks[0].firstReferenceTime, firstTime)
    assert.match(mounted.text(), /已失效/)
    assert.match(mounted.text(), /板块共振尚未确认/)
    assert.match(mounted.text(), /资金数据缺失、已过期或未与分钟窗口对齐/)
    assert.doesNotMatch(mounted.text(), /sector_resonance_not_confirmed|F_source_missing_stale_or_misaligned/)
    assert.equal(refresh.readDisplayedPicks(mounted.storage).adaptive.invalidatedCandidates[0].firstReferencePrice, 10)
  } finally { mounted.dispose() }
})

test('legacy candidates show their saved version without inventing admission or first-reference evidence', async () => {
  const saved = latestSnapshot()
  saved.adaptive.candidates = [{ code: '600001', name: 'Legacy', referencePrice: 15, decision_at: saved.generatedAt }]
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved })
  try {
    await flush()
    assert.equal(mounted.state().adaptivePicks.length, 1)
    assert.match(mounted.text(), /旧版候选/)
    assert.match(mounted.text(), /首次主推荐 时间未知/)
    assert.match(mounted.text(), /参考价未记录/)
    assert.equal(mounted.state().adaptivePicks[0].firstReferencePrice, undefined)
  } finally { mounted.dispose() }
})

test('returning to the keep-alive route reads the latest saved recommendation without launching a new scan', async () => {
  let reads = 0, scans = 0
  const first = oldSnapshot(), latest = latestSnapshot(), returnRead = deferred()
  const mounted = mountPicks({
    GetDisplayedKingPicks: () => ++reads === 1 ? Promise.resolve(first) : returnRead.promise,
    StartKingPicksRefresh: async () => { scans++; throw new Error('unexpected scan') },
  })
  try {
    await flush()
    assert.equal(reads, 1)
    mounted.active.value = false; await flush()
    assert.equal(mounted.timers.size, 0)
    mounted.active.value = true; await flush()
    assert.equal(reads, 2)
    assert.equal(mounted.state().restoring, true)
    assert.ok(!mounted.text().includes(first.generatedAt))
    returnRead.resolve(latest); await flush()
    assert.equal(mounted.state().result.generatedAt, latest.generatedAt)
    assert.equal(scans, 0)
  } finally { returnRead.resolve(null); mounted.dispose() }
})

test('reentering the keep-alive route starts a fresh read instead of sharing an older background poll', async () => {
  const oldPoll = deferred(), reentryRead = deferred()
  let reads = 0
  const mounted = mountPicks({ GetDisplayedKingPicks: () => {
    reads++
    return reads === 1 ? Promise.resolve(oldSnapshot()) : reads === 2 ? oldPoll.promise : reentryRead.promise
  } })
  try {
    await flush()
    await mounted.runTimer(30000)
    assert.equal(reads, 2)
    mounted.active.value = false; await flush()
    mounted.active.value = true; await flush()
    assert.equal(reads, 3, 'returning must query again even when an old read has not replied')
    assert.equal(mounted.state().restoring, true)
    const outdated = snapshot('2026-10-13T09:40:00+08:00')
    oldPoll.resolve(outdated); await flush()
    assert.equal(mounted.state().restoring, true, 'the detached request must not remove the loading state')
    assert.ok(!mounted.text().includes(outdated.generatedAt))
    const duplicateRead = mounted.state().restoreDisplayed(); await flush()
    assert.equal(reads, 3, 'the detached request must not clear the current read promise')
    reentryRead.resolve(latestSnapshot()); await duplicateRead; await flush()
    assert.equal(mounted.state().restoring, false)
    assert.equal(mounted.state().result.generatedAt, latestSnapshot().generatedAt)
  } finally { oldPoll.resolve(null); reentryRead.resolve(null); mounted.dispose() }
})

test('leaving during the initial read and reopening also verifies a fresh saved result', async () => {
  const initialRead = deferred(), reentryRead = deferred()
  let reads = 0
  const mounted = mountPicks({ GetDisplayedKingPicks: () => ++reads === 1 ? initialRead.promise : reentryRead.promise }, oldSnapshot())
  try {
    await flush()
    mounted.active.value = false; await flush()
    mounted.active.value = true; await flush()
    assert.equal(reads, 2)
    initialRead.resolve(oldSnapshot()); await flush()
    assert.equal(mounted.state().restoring, true)
    assert.ok(!mounted.text().includes(oldSnapshot().generatedAt))
    reentryRead.resolve(latestSnapshot()); await flush()
    assert.equal(mounted.state().restoring, false)
    assert.equal(mounted.state().result.generatedAt, latestSnapshot().generatedAt)
  } finally { initialRead.resolve(null); reentryRead.resolve(null); mounted.dispose() }
})

test('background saved-result polling picks up a newer scheduled recommendation', async () => {
  let saved = oldSnapshot(), reads = 0, scans = 0
  const mounted = mountPicks({
    GetDisplayedKingPicks: async () => { reads++; return saved },
    StartKingPicksRefresh: async () => { scans++; throw new Error('unexpected scan') },
  })
  try {
    await flush()
    assert.equal(reads, 1)
    saved = latestSnapshot()
    await mounted.runTimer(30000)
    assert.equal(reads, 2)
    assert.equal(mounted.state().result.generatedAt, saved.generatedAt)
    assert.equal(scans, 0)
  } finally { mounted.dispose() }
})

test('a saved-result read started before a successful manual scan cannot overwrite that scan', async () => {
  const delayedRead = deferred(), manual = snapshot(latestSnapshot().generatedAt, 'manual')
  manual.adaptive.scanId = 'fresh-manual'
  let reads = 0
  const mounted = mountPicks({
    GetDisplayedKingPicks: () => ++reads === 1 ? Promise.resolve(oldSnapshot()) : delayedRead.promise,
    StartKingPicksRefresh: async () => ({ task_id: 'manual-1', status: 'completed', result: manual }),
  })
  try {
    await flush()
    await mounted.runTimer(30000)
    assert.equal(reads, 2)
    await mounted.state().refresh(); await flush()
    assert.equal(mounted.state().result.adaptive.scanId, 'fresh-manual')
    // Seconds-resolution timestamps can tie, so the in-flight read must also
    // be invalidated by the successful manual refresh, not just by date order.
    delayedRead.resolve({ ...latestSnapshot(), adaptive: { ...latestSnapshot().adaptive, scanId: 'earlier-saved' } })
    await flush()
    assert.equal(mounted.state().result.adaptive.scanId, 'fresh-manual')
    assert.equal(refresh.readDisplayedPicks(mounted.storage).adaptive.scanId, 'fresh-manual')
    assert.equal(mounted.state().restoreError, '')
  } finally { delayedRead.resolve(null); mounted.dispose() }
})

test('a failed saved-result read shows the cached recommendation with an explicit fallback notice', async () => {
  const cached = oldSnapshot()
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => { throw new Error('bridge unavailable') } }, cached)
  try {
    await flush()
    assert.equal(mounted.state().restoring, false)
    assert.equal(mounted.state().result.generatedAt, cached.generatedAt)
    assert.match(mounted.state().restoreError, /bridge unavailable/)
    assert.match(mounted.text(), /最新推荐读取失败/)
    assert.match(mounted.text(), /当前显示上次保存的推荐，尚未确认是否最新/)
  } finally { mounted.dispose() }
})

test('a server history-reconciliation error labels its saved result as fallback without persisting the error', async () => {
  const cached = oldSnapshot()
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => ({ ...cached, displaySyncError: 'history database unavailable' }) }, cached)
  try {
    await flush()
    assert.equal(mounted.state().restoring, false)
    assert.equal(mounted.state().result.generatedAt, cached.generatedAt)
    assert.match(mounted.state().restoreError, /history database unavailable/)
    assert.match(mounted.text(), /当前显示上次保存的推荐，尚未确认是否最新/)
    assert.equal(refresh.readDisplayedPicks(mounted.storage).displaySyncError, undefined)
  } finally { mounted.dispose() }
})

test('an unanswered saved-result read times out, permits retry, and ignores its late reply', async () => {
  const pending = deferred(), cached = oldSnapshot()
  let reads = 0
  const mounted = mountPicks({ GetDisplayedKingPicks: () => ++reads === 1 ? pending.promise : Promise.resolve(latestSnapshot()) }, cached)
  try {
    await flush()
    await mounted.runTimer(20000)
    assert.equal(mounted.state().restoring, false)
    assert.equal(mounted.state().result.generatedAt, cached.generatedAt)
    assert.match(mounted.state().restoreError, /记录读取超时/)
    mounted.state().restoring = true
    await mounted.state().restoreDisplayed(); await flush()
    assert.equal(mounted.state().restoring, false)
    assert.equal(mounted.state().restoreError, '')
    assert.equal(mounted.state().result.generatedAt, latestSnapshot().generatedAt)
    pending.resolve(cached); await flush()
    assert.equal(mounted.state().result.generatedAt, latestSnapshot().generatedAt)
  } finally { pending.resolve(null); mounted.dispose() }
})

test('daily cards render their own evidence and T+1 objective while minute readiness remains separate', async () => {
  const saved = snapshot('2026-10-16T14:35:00+08:00')
  const daily = { code: '600001', name: '日线独立样本', referencePrice: 12.2, baselineAsOf: '2026-10-15', baselineSource: 'verified daily fixture',
    decision_at: saved.generatedAt, dailySignal: { taskId: 'daily_structure_to_next_session', firstReferenceTime: '2026-10-16T10:30:00+08:00', firstReferencePrice: 11.8, currentSignalState: 'continued' },
    dailyProfileDecision: { regular: { entryEligible: true, label: '均衡' }, conservative: { entryEligible: false } },
    support: 11, resistance: 15, structureRewardRisk: 2.33, biasMa5Pct: 1.1, atrExtension: .6,
    selectionReasons: ['已完成日K显示MA5大于MA10和MA20'], risks: ['持有至下一交易日的隔夜风险'], dataGaps: ['市场广度缺失，保持未知'],
    indicatorEvidence: [{ key: 'ma5', value: 12.07, unit: '元', source: 'fixture daily source', asOf: '2026-10-15', status: 'observed' }],
    eventAssessment: { coverage: { financials: true, forecast: true, unlock: true }, unlock14dPct: 0 },
    earlyScore: 99, finalScore: 99, prediction: { probability: .99 } }
  Object.assign(saved.adaptive, { primarySelectionMode: 'daily_conditional', primaryObjective: 'daily_structure_to_next_session',
    recommendationPolicy: { version: 'minute-contract' }, candidates: [daily], scoreVersion: 'stockking-v1.2-rules',
    profileCandidates: { regular: [daily], conservative: [] },
    dailyAssessment: { count: 1, dailyUsableCount: 30, quoteUsableCount: 25, eventCoveragePassedCount: 12, researchedCount: 306,
      message: '日线条件推荐1只', excludedCounts: { daily_uptrend_not_confirmed: 11 } },
    admissionDiagnostics: { summary: '分钟主推荐0只', checks: [{ key: 'history', label: '20日同刻分钟基准', passed: 0, blocked: 306, total: 306 }],
      evidenceAndRiskPassed: 0, modelAdmissionPassed: 0, selected: 0 },
    historyPreparation: { message: '历史准备进行中', baselineClock: '2026-10-16T10:30:00+08:00' },
    morningReadiness: { message: '上午模型尚在准备', queues: [] } })
  const before = JSON.stringify(saved)
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved }, null, Date.parse(saved.generatedAt))
  try {
    await flush()
    assert.equal(mounted.state().isDailyPrimary, true)
    assert.equal(mounted.state().adaptivePicks.length, 1)
    assert.match(mounted.text(), /日线条件推荐 · 1 只/)
    assert.match(mounted.text(), /稳健 · 财务与回撤/)
    assert.match(mounted.text(), /均衡 · 趋势与买点/)
    assert.match(mounted.text(), /激进 · 突破与延续/)
    assert.match(mounted.text(), /日线独立样本/)
    assert.match(mounted.text(), /首次日线推荐 2026\/10\/16 10:30:00/)
    assert.match(mounted.text(), /首次参考价 ¥11.80/)
    assert.match(mounted.text(), /持续跟踪/)
    assert.match(mounted.text(), /下一交易日（T\+1）/)
    assert.match(mounted.text(), /verified daily fixture/)
    assert.match(mounted.text(), /市场广度缺失，保持未知/)
    assert.match(mounted.text(), /分钟增强准备情况/)
    assert.match(mounted.text(), /历史同刻核验时点：2026\/10\/16 10:30:00/)
    assert.ok(!mounted.text().includes('分钟主推荐0只'))
    assert.ok(!mounted.text().includes('最终主推荐 0 只'))
    assert.ok(!mounted.text().includes('Early / MainRise'))
    assert.ok(!mounted.text().includes('规则排序 99'))
    assert.ok(!mounted.text().includes('主升概率'))
    assert.equal(mounted.state().dailyIndicators(daily)[0].value, '12.07元')
    assert.equal(mounted.state().dailyIndicators(daily)[0].source, 'fixture daily source')
    mounted.state().precisionProfile = 'conservative'; await flush()
    assert.equal(mounted.state().adaptivePicks.length, 0)
    assert.match(mounted.state().primaryEmptyMessage, /日线条件推荐0只（稳健档）/)
    assert.equal(JSON.stringify(saved), before)
  } finally { mounted.dispose() }
})

test('an empty daily result explains daily gates instead of substituting the minute blockers', async () => {
  const saved = snapshot('2026-10-16T14:35:00+08:00')
  Object.assign(saved.adaptive, { primarySelectionMode: 'daily_conditional', primaryObjective: 'daily_structure_to_next_session', candidates: [],
    dailyAssessment: { count: 0, message: '日线报价与风险条件未满足', excludedCounts: { quote_or_provider_price_limits_unverified: 4 } },
    admissionDiagnostics: { summary: '全部分钟缺证据', checks: [], evidenceAndRiskPassed: 0, modelAdmissionPassed: 0, selected: 0 } })
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved }, null, Date.parse(saved.generatedAt))
  try {
    await flush()
    assert.match(mounted.text(), /日线报价与风险条件未满足/)
    assert.match(mounted.text(), /报价或涨跌停价未核验：4 只/)
    assert.ok(!mounted.text().includes('全部分钟缺证据'))
    assert.ok(!mounted.text().includes('最终主推荐 0 只'))
  } finally { mounted.dispose() }
})
