import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import * as refresh from './picksRefresh.mjs'
import * as recordRead from './recordRead.mjs'
import * as learningStatus from './learningStatus.mjs'
import * as recommendationQuality from './recommendationQuality.mjs'
import * as pickExplain from './pickExplain.mjs'
import * as pickLedger from './pickLedger.mjs'
import * as premarket from './premarket.mjs'
import * as opportunityDisplay from './opportunityDisplay.mjs'

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
const researchSnapshot = (generatedAt, { latent = [], continuation = [], source = 'scheduled' } = {}) => ({
  ...snapshot(generatedAt, source),
  adaptive: { ...snapshot(generatedAt, source).adaptive,
    recommendationPolicy: { version: 'primary-admission-v1' },
    nextDayWatchlist: latent, nextDayContinuationWatchlist: continuation,
  },
})
const zhengyuObservation = () => ({ code: '603089', name: '正裕工业', referencePrice: 15,
  nextDaySignal: { queue: 'continuation', score: 90, signalSession: '2026-10-23', targetSession: '2026-10-26',
    reasons: ['已触板延续线索'], gaps: ['仅在展开详情后显示的待确认条件'] } })

test('current model readiness explains an old empty snapshot without changing its frozen evidence', async () => {
  const saved = researchSnapshot('2026-10-26T10:30:00+08:00')
  saved.currentPrimaryReadiness = { modelUsableNow: false, progressKnown: true, progressFresh: true, countsAsOf: '2026-10-26T15:30:00+08:00', minimumSignalDaysForAnyTraining: 156, progress: { validSamples: 0, validSampleDays: 0 } }
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved }, null, Date.parse('2026-10-26T15:35:00+08:00'))
  try {
    await flush()
    assert.match(mounted.visibleText(), /模型验证精选 · 等待模型验证/)
    assert.match(mounted.visibleText(), /当前 · T\+1模型准备中/)
    assert.match(mounted.visibleText(), /有效样本 0 条.*156/s)
    assert.equal(mounted.state().result.generatedAt, saved.generatedAt)
    assert.equal(mounted.state().adaptive.primaryReadiness, undefined)
    assert.doesNotMatch(mounted.visibleText(), /冷启动研究对照 ·/)
  } finally { mounted.dispose() }
})

test('cold-start controls are visible separately from formal recommendations and show actual publication eligibility', async () => {
  const saved = researchSnapshot('2026-10-26T10:30:00+08:00')
  saved.adaptive.primaryReadiness = { modelUsableNow: false, progressKnown: false, minimumSignalDaysForAnyTraining: 156 }
  saved.adaptive.coldStartResearch = { coldStartControlCount: 1, evidenceEligibleCount: 1, entryPlanReadyCount: 1, recordingMode: 'official_frozen', trainingEligibleCount: 1, trainingControlCount: 1,
    items: [{ code: '600001', name: '研究样本', minimumPrice: 9.95, maximumPrice: 10.15, invalidationPrice: 9.5, expiresAt: '2026-10-26T10:35:00+08:00', trainingEligible: true }] }
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved }, null, Date.parse('2026-10-26T10:31:00+08:00'))
  try {
    await flush()
    assert.match(mounted.visibleText(), /数量未知/)
    assert.match(mounted.visibleText(), /冷启动研究对照 · 1 只 · 未构成正式推荐/)
    assert.match(mounted.visibleText(), /研究样本 600001.*保存时研究区间 ¥9.95–10.15/s)
    assert.match(mounted.visibleText(), /训练资格 1 条.*对照 1 条/s)
    assert.equal(mounted.state().adaptivePicks.length, 0)
  } finally { mounted.dispose() }
})

const earlyOpportunity = () => ({ code: '600001', name: '早期启动样本', referencePrice: 10.1,
  quote: { price: 10.1, source: 'tencent', source_time: '2026-10-28T10:29:55+08:00' },
  opportunity: { channel: 'E', grade: 'E-A', rankScore: 82, supports: ['价格方向突变，短时量能增加'],
    missing: 'VWAP与板块同步尚未确认', counterEvidence: [], invalidation: ['回落并持续无法收复开盘价'],
    firstDetectionAt: '2026-10-28T10:29:00+08:00', firstDetectionPrice: 10, triggerAt: null,
    buyWindow: { status: 'observed', askPrice: 10.1, sourceTime: '2026-10-28T10:29:55+08:00' } } })

test('early opportunity renders before the research lists and unready model with its unconfirmed evidence visible', async () => {
  const saved = researchSnapshot('2026-10-28T10:30:00+08:00', { continuation: [zhengyuObservation()] })
  saved.adaptive.opportunityChannels = { version: 'opportunity-v1', recommendations: [earlyOpportunity()], observations: [] }
  saved.adaptive.primaryReadiness = { modelUsableNow: false, progressKnown: false }
  const before = JSON.stringify(saved)
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved }, null, Date.parse('2026-10-28T10:30:00+08:00'))
  try {
    await flush()
    const text = mounted.visibleText()
    assert.match(text, /机会精选 · 早期启动 \/ 主升确认/)
    assert.match(text, /早期启动样本600001E-A · 早期启动/)
    assert.match(text, /价格方向突变，短时量能增加/)
    assert.match(text, /VWAP与板块同步尚未确认/)
    assert.match(text, /回落并持续无法收复开盘价/)
    assert.match(text, /当日新买通常不能当日卖出/)
    assert.ok(text.indexOf('早期启动样本') < text.indexOf('研究观察 · 不计入主推荐'))
    assert.ok(text.indexOf('早期启动样本') < text.indexOf('模型验证精选 · 等待模型验证'))
    assert.equal(mounted.state().adaptivePicks.length, 0, 'independent opportunities cannot become validated model selections')
    assert.equal(refresh.readDisplayedPicks(mounted.storage).adaptive.opportunityChannels.recommendations[0].code, '600001')
    assert.equal(JSON.stringify(saved), before)
  } finally { mounted.dispose() }
})

test('expired opportunity quotes stay visible as historical evidence and failed refreshes preserve the cards', async () => {
  const saved = researchSnapshot('2026-10-28T10:30:00+08:00')
  saved.adaptive.opportunityChannels = { recommendations: [earlyOpportunity()] }
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved,
    StartKingPicksRefresh: async () => { throw new Error('报价服务暂不可用') },
  }, null, Date.parse('2026-10-28T10:30:00+08:00'))
  try {
    await flush()
    assert.match(mounted.visibleText(), /源时间在30秒内/)
    mounted.setNow(Date.parse('2026-10-28T10:30:26+08:00'))
    await mounted.runTimer(1000)
    assert.match(mounted.visibleText(), /早期启动样本/)
    assert.match(mounted.visibleText(), /历史参考价 · 行情已过期/)
    assert.doesNotMatch(mounted.visibleText(), /源时间在30秒内/)
    await mounted.state().refresh(); await flush()
    assert.match(mounted.visibleText(), /报价服务暂不可用/)
    assert.match(mounted.visibleText(), /早期启动样本/)
    assert.equal(refresh.readDisplayedPicks(mounted.storage).adaptive.opportunityChannels.recommendations.length, 1)
  } finally { mounted.dispose() }
})

test('unselected qualified opportunities keep their real grades alongside B and C observations', async () => {
  const saved = researchSnapshot('2026-10-28T10:30:00+08:00')
  const observations = ['E-A', 'M-A', 'B', 'C'].map((grade, index) => ({ ...earlyOpportunity(),
    code: `60000${index + 2}`, name: `保留等级${index + 1}`,
    opportunity: { ...earlyOpportunity().opportunity, grade, channel: grade === 'M-A' ? 'M' : 'E' } }))
  saved.adaptive.opportunityChannels = { recommendations: [earlyOpportunity()], observations }
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved }, null, Date.parse('2026-10-28T10:30:01+08:00'))
  try {
    await flush()
    assert.match(mounted.visibleText(), /其他机会与待确认线索 · 4 只/)
    assert.doesNotMatch(mounted.text(), /B \/ C 待确认线索/)
    for (const row of observations) assert.ok(mounted.text().includes(`${row.name} ${row.code} · ${row.opportunity.grade}`))
    assert.equal(mounted.state().opportunities.cards.length, 1)
    assert.equal(mounted.state().adaptivePicks.length, 0)
  } finally { mounted.dispose() }
})

test('opportunity scan history retains E/M grades and does not backfill model-selected counts', async () => {
  const saved = researchSnapshot('2026-10-28T10:30:00+08:00')
  saved.adaptive.opportunityChannels = { recommendations: [earlyOpportunity()] }
  const historyItem = { run_id: 'opportunity-run', mode: 'king_1030', result: saved.adaptive }
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved, GetKingPicksHistory: async () => ({ items: [historyItem] }) }, null, Date.parse('2026-10-28T10:30:01+08:00'))
  try {
    await flush()
    await mounted.state().openHistory(); await flush()
    assert.match(mounted.visibleText(), /机会精选 · 1 只 · 保存时研究等级/)
    mounted.state().restoreHistory(historyItem)
    await flush()
    assert.match(mounted.visibleText(), /历史快照 · 未做当前核验/)
    assert.match(mounted.visibleText(), /早期启动样本/)
    assert.equal(mounted.state().adaptivePicks.length, 0)
    assert.equal(mounted.state().viewingHistory, true)
  } finally { mounted.dispose() }
})

test('large opportunity observation lists show ten initially and expand without changing saved decisions', async () => {
  let saved = researchSnapshot('2026-10-28T10:30:00+08:00')
  const observations = Array.from({ length: 300 }, (_, index) => ({ ...earlyOpportunity(),
    code: String(600002 + index), name: `观察队列样本${index + 1}`,
    opportunity: { ...earlyOpportunity().opportunity, grade: ['E-A', 'M-A', 'B', 'C'][index % 4] } }))
  saved.adaptive.opportunityChannels = { recommendations: [earlyOpportunity()], observations }
  const before = JSON.stringify(saved)
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved }, null, Date.parse('2026-10-28T10:30:01+08:00'))
  try {
    await flush()
    assert.match(mounted.visibleText(), /其他机会与待确认线索 · 300 只/)
    assert.match(mounted.text(), /当前展示 10 \/ 300 只/)
    assert.match(mounted.text(), /查看全部 300 条/)
    assert.ok(mounted.text().includes('观察队列样本10 600011 · M-A'))
    assert.ok(!mounted.text().includes('观察队列样本11 600012'))
    assert.equal(mounted.state().visibleOpportunityObservations.length, 10)
    mounted.state().toggleOpportunityObservations(); await flush()
    assert.match(mounted.text(), /当前展示 300 \/ 300 只/)
    assert.ok(mounted.text().includes('观察队列样本300 600301 · C'))
    assert.match(mounted.text(), /收起至前10条/)
    mounted.state().toggleOpportunityObservations(); await flush()
    assert.equal(mounted.state().visibleOpportunityObservations.length, 10)
    assert.equal(JSON.stringify(saved), before)
    assert.equal(mounted.state().opportunities.cards.length, 1)
    assert.equal(mounted.state().adaptivePicks.length, 0)
    mounted.state().toggleOpportunityObservations(); await flush()
    saved = { ...saved, generatedAt: '2026-10-28T10:31:00+08:00',
      adaptive: { ...saved.adaptive, generatedAt: '2026-10-28T10:31:00+08:00' } }
    await mounted.runTimer(30000)
    assert.equal(mounted.state().showAllOpportunityObservations, false, 'a newly saved scan starts with a bounded list')
    assert.equal(mounted.state().visibleOpportunityObservations.length, 10)
  } finally { mounted.dispose() }
})

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
    '../utils/recommendationQuality.mjs': recommendationQuality,
    '../utils/pickExplain.mjs': pickExplain,
    '../utils/pickLedger.mjs': pickLedger,
    '../utils/premarket.mjs': premarket,
    '../utils/opportunityDisplay.mjs': opportunityDisplay,
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
  // Unlike textContent, a closed native <details> exposes only its first
  // <summary>. Use this for assertions about what users see without clicking.
  const visibleTextOf = element => {
    if (element.type === 'comment' || element.style.display === 'none') return ''
    const children = element.type === 'details' && !element.props.open && element.props.open !== ''
      ? [element.children.find(child => child.type === 'summary')].filter(Boolean) : element.children
    return `${element.text}${children.map(visibleTextOf).join('')}`
  }
  return {
    active, timers, storage,
    state: () => instance.setupState,
    setNow(value) { now = value },
    text: () => textOf(root),
    visibleText: () => visibleTextOf(root),
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

test('background polling makes both saved research queues visible without expanding details or launching a scan', async () => {
  let saved = researchSnapshot('2026-10-23T09:40:00+08:00'), reads = 0, scans = 0
  const mounted = mountPicks({
    GetDisplayedKingPicks: async () => { reads++; return saved },
    StartKingPicksRefresh: async () => { scans++; throw new Error('unexpected scan') },
  }, null, Date.parse('2026-10-23T09:56:00+08:00'))
  try {
    await flush()
    assert.doesNotMatch(mounted.visibleText(), /正裕工业|潜伏观察样本/)
    saved = researchSnapshot('2026-10-23T09:55:00+08:00', {
      latent: [{ code: '600001', name: '潜伏观察样本', nextDaySignal: { queue: 'first_board', score: 85 } }],
      continuation: [zhengyuObservation()],
    })
    await mounted.runTimer(30000)
    assert.equal(reads, 2)
    assert.equal(scans, 0)
    assert.match(mounted.visibleText(), /正裕工业603089/)
    assert.match(mounted.visibleText(), /潜伏观察样本600001/)
    assert.match(mounted.visibleText(), /研究观察 · 不计入主推荐（潜伏 1 只 \/ 延续 1 只）/)
    assert.match(mounted.visibleText(), /主推荐 0 只/)
    assert.equal(mounted.state().adaptivePicks.length, 0)
    assert.equal(mounted.state().savedRecommendations.saved, 0)
    assert.match(mounted.text(), /仅在展开详情后显示的待确认条件/)
    assert.doesNotMatch(mounted.visibleText(), /仅在展开详情后显示的待确认条件/, 'visibility checks must ignore collapsed evidence')
    assert.equal(refresh.readDisplayedPicks(mounted.storage).adaptive.nextDayContinuationWatchlist[0].code, '603089')
  } finally { mounted.dispose() }
})

test('a completed manual refresh replaces the displayed background research lists and persists the replacement', async () => {
  const scheduled = researchSnapshot('2026-10-23T09:55:00+08:00', {
    latent: [{ code: '600001', name: '旧潜伏观察', nextDaySignal: { queue: 'first_board', score: 85 } }],
    continuation: [zhengyuObservation()],
  })
  const manual = researchSnapshot('2026-10-23T10:00:00+08:00', { source: 'manual',
    continuation: [{ code: '600002', name: '手动延续观察', nextDaySignal: { queue: 'continuation', score: 88 } }],
  })
  const scan = deferred()
  let scans = 0
  const mounted = mountPicks({
    GetDisplayedKingPicks: async () => scheduled,
    StartKingPicksRefresh: () => { scans++; return scan.promise },
  }, null, Date.parse('2026-10-23T10:00:00+08:00'))
  try {
    await flush()
    assert.match(mounted.visibleText(), /正裕工业/)
    const refreshPromise = mounted.state().refresh(); await flush()
    assert.equal(scans, 1)
    assert.match(mounted.visibleText(), /正裕工业/, 'keep the saved list until the replacement completes')
    scan.resolve({ task_id: 'manual-research', status: 'completed', result: manual })
    await refreshPromise; await flush()
    assert.match(mounted.visibleText(), /手动延续观察600002/)
    assert.doesNotMatch(mounted.visibleText(), /正裕工业|旧潜伏观察/)
    assert.equal(mounted.state().nextDayWatchlist.length, 0)
    assert.deepEqual(mounted.state().nextDayContinuationWatchlist.map(pick => pick.code), ['600002'])
    assert.equal(mounted.state().adaptivePicks.length, 0)
    const cached = refresh.readDisplayedPicks(mounted.storage)
    assert.equal(cached.displaySource, 'manual')
    assert.deepEqual(cached.adaptive.nextDayWatchlist, [])
    assert.deepEqual(cached.adaptive.nextDayContinuationWatchlist.map(pick => pick.code), ['600002'])
  } finally { scan.resolve(null); mounted.dispose() }
})

test('a late background read cannot restore research observations removed by a manual refresh with the same timestamp', async () => {
  const generatedAt = '2026-10-23T10:00:00+08:00'
  const delayedRead = deferred()
  const scheduled = researchSnapshot('2026-10-23T09:55:00+08:00', { continuation: [zhengyuObservation()] })
  const manual = researchSnapshot(generatedAt, { source: 'manual' })
  let reads = 0
  const mounted = mountPicks({
    GetDisplayedKingPicks: () => ++reads === 1 ? Promise.resolve(scheduled) : delayedRead.promise,
    StartKingPicksRefresh: async () => ({ task_id: 'manual-empty-research', status: 'completed', result: manual }),
  }, null, Date.parse(generatedAt))
  try {
    await flush()
    assert.match(mounted.visibleText(), /正裕工业/)
    await mounted.runTimer(30000)
    assert.equal(reads, 2)
    await mounted.state().refresh(); await flush()
    assert.doesNotMatch(mounted.visibleText(), /正裕工业/)
    delayedRead.resolve(researchSnapshot(generatedAt, { continuation: [zhengyuObservation()] })); await flush()
    assert.doesNotMatch(mounted.visibleText(), /正裕工业/)
    assert.match(mounted.visibleText(), /研究观察 0 只（潜伏 0 \/ 延续 0）/)
    assert.equal(mounted.state().nextDayContinuationWatchlist.length, 0)
    assert.equal(mounted.state().result.displaySource, 'manual')
    assert.deepEqual(refresh.readDisplayedPicks(mounted.storage).adaptive.nextDayContinuationWatchlist, [])
    assert.equal(mounted.state().restoreError, '')
  } finally { delayedRead.resolve(null); mounted.dispose() }
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

for (const formalCount of [0, 1]) test(`new daily observations stay separate with ${formalCount} formal picks`, async () => {
  const saved = snapshot('2026-10-22T10:41:49+08:00')
  const observation = { code: '600001', name: '独立日线观察样本', status: 'daily_observation', observationOnly: true,
    entryEligible: false, referencePrice: 12.2, decision_at: saved.generatedAt, dailySignal: { taskId: 'daily_structure_to_next_session', observationOnly: true },
    dailyProfileDecision: { regular: { entryEligible: false, label: '均衡' } },
    dataGaps: ['市场广度缺失', '板块资金尚未确认'], indicatorEvidence: [] }
  const formal = { code: '600002', name: '正式分钟样本', primaryAdmission: { eligible: true }, referencePrice: 15 }
  Object.assign(saved.adaptive, { primarySelectionMode: formalCount ? 'mixed_intraday_and_daily_observation' : 'daily_observation',
    recommendationPolicy: { version: 'v4' }, primaryObjective: 'morning_signal_to_same_session_close',
    candidates: formalCount ? [formal] : [], profileCandidates: { regular: formalCount ? [formal] : [], conservative: [] },
    dailyCandidates: [observation], dailyProfileCandidates: { regular: [observation], conservative: [] },
    dailyAssessment: { message: '日线观察1只，正式准入仍待核验' },
    dailyRejectionLedger: [{ code: '600003', state: 'observation_below_top_n' }],
    admissionDiagnostics: { summary: formalCount ? '正式主榜已独立核验' : '市场与分钟证据不齐，本轮空榜', checks: [], selected: formalCount },
    marketEmotionPanel: { source: 'fixture', decisionAssessment: { available: false }, gaps: ['全市场数据未齐'] } })
  const before = JSON.stringify(saved)
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved }, null, Date.parse(saved.generatedAt))
  try {
    await flush()
    assert.equal(mounted.state().isDailyPrimary, false)
    assert.equal(mounted.state().adaptivePicks.length, formalCount)
    assert.equal(mounted.state().dailyPicks.length, 1)
    assert.match(mounted.text(), new RegExp(`模型验证精选 · ${formalCount} 只通过准入`))
    assert.match(mounted.text(), /日线观察 · 1 只 · 不计入主推荐/)
    assert.match(mounted.text(), /独立日线观察样本/)
    assert.match(mounted.text(), /本轮日线观察 2026\/10\/22 10:41:49/)
    assert.match(mounted.text(), /本轮参考价 ¥12.20/)
    assert.ok(!mounted.text().includes('首次日线观察'))
    assert.match(mounted.text(), /均衡档观察合格但未入前列 1 只/)
    assert.match(mounted.text(), /市场广度缺失；板块资金尚未确认/)
    assert.match(mounted.text(), /全市场数据未齐/)
    assert.ok(!mounted.text().includes('日线条件推荐'))
    if (!formalCount) assert.match(mounted.text(), /市场与分钟证据不齐，本轮空榜/)
    mounted.state().precisionProfile = 'conservative'; await flush()
    assert.equal(mounted.state().adaptivePicks.length, 0)
    assert.equal(mounted.state().dailyPicks.length, 0)
    assert.equal(JSON.stringify(saved), before)
  } finally { mounted.dispose() }
})

test('pending history diagnostics explain session warmup without calling it missing history', async () => {
  const saved = snapshot('2026-10-22T13:01:40+08:00')
  Object.assign(saved.adaptive, { recommendationPolicy: { version: 'primary-evidence-20261015-v2' },
    admissionDiagnostics: { version: 'primary-diagnostics-v2', status: 'session_warmup',
      checks: [{ key: 'history', label: '20日同刻量价基准', passed: 0, blocked: 0, pending: 6, total: 6,
        action: '后台补齐缺失交易日并核验历史分钟数据' }],
      evidenceAndRiskPassed: 0, modelAdmissionPassed: 0, selected: 0,
      summary: '6只正在等待开盘后的完整分钟窗口，不能据此判定历史缺失。' } })
  const before = JSON.stringify(saved)
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved }, null, Date.parse(saved.generatedAt))
  try {
    await flush()
    assert.match(mounted.text(), /0 \/ 6 只通过/)
    assert.match(mounted.text(), /6 只待核验：等待当前完整分钟窗口形成，不计为历史缺失/)
    assert.ok(!mounted.text().includes('后台补齐缺失交易日并核验历史分钟数据'))
    assert.equal(mounted.state().adaptivePicks.length, 0)
    assert.equal(JSON.stringify(saved), before)
  } finally { mounted.dispose() }
})

test('verified weak breadth displays reduced budget warnings while observations stay outside the main list', async () => {
  const saved = snapshot('2026-10-22T13:01:40+08:00')
  const breadth = { available: true, passed: true, status: 'observed_weak', marketRiskState: 'weak', advancingPct: 16.72 }
  const observation = { code: '600001', name: '弱市日线观察', status: 'daily_observation', observationOnly: true,
    version: 'king-daily-primary-v5', entryEligible: false, formalEligible: false, selected: false,
    dailySignal: { taskId: 'daily_structure_to_next_session', observationOnly: true, selected: false },
    referencePrice: 12.2, marketBreadth: breadth, indicatorEvidence: [] }
  const formal = { code: '600002', name: '满足个股条件的样本', primaryAdmission: { eligible: true,
    marketRiskState: 'weak', warnings: ['verified_weak_market_reduced_risk_budget'] }, referencePrice: 15, plannedWeight: .2 }
  Object.assign(saved.adaptive, { primarySelectionMode: 'mixed_intraday_and_daily_observation',
    recommendationPolicy: { version: 'primary-evidence-20261015-v2' }, candidates: [formal],
    profileCandidates: { regular: [formal] }, dailyCandidates: [observation], dailyProfileCandidates: { regular: [observation] },
    dailyAssessment: { version: 'king-daily-primary-v5', count: 1, marketBreadth: breadth },
    portfolioPolicy: { totalExposureCap: .5, plannedExposure: .2, cashWeight: .8 },
    marketEmotionPanel: { source: 'fixture', asOf: saved.generatedAt, evaluatedAt: saved.generatedAt,
      decisionAssessment: breadth, features: { advancing_pct: 16.72 } } })
  const before = JSON.stringify(saved)
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved }, null, Date.parse(saved.generatedAt))
  try {
    await flush()
    assert.equal(mounted.state().adaptivePicks.length, 1)
    assert.equal(mounted.state().dailyPicks.length, 1)
    assert.match(mounted.text(), /已核验市场偏弱/)
    assert.match(mounted.text(), /执行弱市减半总风险预算/)
    assert.match(mounted.text(), /日线观察 · 1 只 · 不计入主推荐/)
    assert.ok(!mounted.text().includes('触发主榜限制'))
    assert.ok(!mounted.text().includes('暂停新增主推荐'))
    assert.equal(JSON.stringify(saved), before)
  } finally { mounted.dispose() }
})

test('new daily observation cards do not borrow historical first references when this round is missing', async () => {
  const saved = snapshot('2026-10-22T10:41:49+08:00')
  const observation = { code: '600001', name: '缺本轮参考', status: 'daily_observation', observationOnly: true,
    dailySignal: { taskId: 'daily_structure_to_next_session', firstReferenceTime: '2026-10-21T09:40:00+08:00', firstReferencePrice: 99 } }
  Object.assign(saved.adaptive, { primarySelectionMode: 'daily_observation', candidates: [], dailyCandidates: [observation] })
  const mounted = mountPicks({ GetDisplayedKingPicks: async () => saved }, null, Date.parse(saved.generatedAt))
  try {
    await flush()
    assert.match(mounted.text(), /本轮日线观察 未记录/)
    assert.match(mounted.text(), /本轮参考价未记录/)
    assert.ok(!mounted.text().includes('首次日线观察'))
    assert.ok(!mounted.text().includes('¥99.00'))
  } finally { mounted.dispose() }
})
