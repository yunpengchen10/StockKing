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
function mountPicks(api = {}, cached = null) {
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
  const component = new Function('modules', 'window', 'document', 'setTimeout', 'clearTimeout', imports(script.content).replace('export default', 'return'))(
    modules, { localStorage: storage }, { visibilityState: 'visible' }, clock.setTimeout, clock.clearTimeout)
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
  for (const name of new Set([...descriptor.template.content.matchAll(/<(n-[\w-]+)/g)].map(match => match[1]))) app.component(name, passthrough)
  app.mount(root)
  const textOf = element => element.type === 'comment' || element.style.display === 'none' ? '' : `${element.text}${element.children.map(textOf).join('')}`
  return {
    active, timers, storage,
    state: () => instance.setupState,
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
