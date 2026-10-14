import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { compileScript, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import * as chartDate from './chartDate.mjs'
import * as indicatorGuide from './indicatorGuide.mjs'

const flush = async () => { for (let n = 0; n < 15; n++) await Promise.resolve(); await vue.nextTick() }
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const imports = code => code.replace(/^import\s+([\s\S]*?)\s+from\s+['"](.+?)['"];?/gm,
  (_, bindings, path) => `const ${bindings.replace(/\bas\b/g, ':')} = modules[${JSON.stringify(path)}];`)

// The chart's helpers contain plain JavaScript; evaluate their real exports so
// these tests exercise row merging/normalization without a canvas or Wails.
function helper(path, modules = {}) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8')
  const names = [...source.matchAll(/export (?:function|const) (\w+)/g)].map(match => match[1])
  return new Function('modules', `${imports(source).replace(/export /g, '')}\nreturn { ${names.join(',')} }`)(modules)
}
const constants = helper('../components/kline/constants.ts')
const symbol = helper('./symbol.js')
const format = helper('../components/kline/format.ts')
const time = helper('../components/kline/time.ts', { './constants': constants, 'lightweight-charts': { TickMarkType: {} } })

function mountComponent(filename, api = {}, initialQuery = {}, initialProps = {}) {
  let instance, timerId = 0
  const timers = new Map()
  const setTimer = (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay }); return id }
  const clearTimer = id => timers.delete(id)
  const route = vue.reactive({ name: 'klineAnalysis', query: initialQuery })
  const props = vue.reactive(initialProps)
  const child = { render: () => null }
  const bridge = {
    GetStockKingPreference: async () => '', SaveStockKingPreference: async () => {},
    GetResearchNote: async () => null, GetForecast: async (_, horizon) => ({ horizon }),
    GetFollowList: async () => [], GetGroupList: async () => [], ...api,
  }
  const modules = {
    vue, 'vue-router': { useRoute: () => route, useRouter: () => ({ currentRoute: vue.ref(route), replace: async next => Object.assign(route, next) }) },
    'naive-ui': { useMessage: () => ({ success() {}, error() {}, warning() {} }) },
    '../../wailsjs/go/main/App': bridge, '../../wailsjs/runtime': { EventsOn() {}, EventsOff() {} },
    './StockLightweightKlineChart.vue': child, './StockKingIcon.vue': child,
    '../utils/pageSession.mjs': { usePageActive: () => vue.ref(true) },
    '../utils/symbol': symbol, '../utils/chartDate.mjs': chartDate,
    'lightweight-charts': { LineStyle: {} }, './kline/calc': {},
    './kline/constants': constants, './kline/format': format, './kline/time': time,
    './kline/indicators/toggle': helper('../components/kline/indicators/toggle.ts'),
    './kline/measurePrimitive': {}, './kline/wavePrimitive': {}, './kline/drawingManagerHost': {},
    './IndicatorGuide.vue': child, '../utils/indicatorGuide.mjs': indicatorGuide, './kline/chip': {},
  }
  const { descriptor } = parse(readFileSync(new URL(`../components/${filename}`, import.meta.url), 'utf8'))
  const compiled = imports(compileScript(descriptor, { id: 'chart-loading-test' }).content).replace('export default', 'return')
  const component = new Function('modules', 'window', 'localStorage', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'console', compiled)(
    modules, { innerHeight: 900, addEventListener() {}, removeEventListener() {} },
    { getItem: () => null, setItem() {} }, setTimer, clearTimer, setTimer, clearTimer, { log() {} })
  component.render = function () { instance = vue.getCurrentInstance(); return null }
  const renderer = vue.createRenderer({
    createElement: type => ({ type }), createText: text => ({ text }), createComment: text => ({ text }),
    insert() {}, remove() {}, setElementText() {}, setText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null,
  })
  const app = renderer.createApp({ setup: () => () => vue.h(component, props) })
  app.mount({})
  return { route, props, timers, state: () => instance.setupState, dispose: () => app.unmount() }
}

test('a pick opens its chart before preferences resolve and does not start hidden forecasts', async () => {
  const preferences = deferred()
  let noteCalls = 0, forecastCalls = 0
  const mounted = mountComponent('KlineResearch.vue', {
    GetStockKingPreference: () => preferences.promise,
    GetResearchNote: async () => { noteCalls++; return {} },
    GetForecast: async (_, horizon) => { forecastCalls++; return { horizon } },
  }, { code: '600001', name: '精选股票' })
  try {
    await flush()
    assert.equal(mounted.state().code, '600001.SH')
    assert.equal(mounted.state().name, '精选股票')
    assert.equal(noteCalls, 0)
    assert.equal(forecastCalls, 0)
    mounted.state().showNotebook = true; await flush()
    assert.equal(noteCalls, 1)
    assert.equal(forecastCalls, 3)
    mounted.state().showNotebook = false; await flush()
    mounted.state().showNotebook = true; await flush()
    assert.equal(forecastCalls, 3, 'reopening the same notebook keeps the loaded forecast and edited note')
    preferences.resolve(JSON.stringify([{ code: '000001.SZ', name: '上次股票' }])); await flush()
    assert.deepEqual(mounted.state().recent.map(item => item.code), ['600001.SH', '000001.SZ'])
  } finally { mounted.dispose() }
})

test('restoring a slow last-viewed preference cannot replace an explicit stock selection', async () => {
  const lastViewed = deferred()
  const mounted = mountComponent('KlineResearch.vue', {
    GetStockKingPreference: key => key === 'kline.recentSymbols' ? Promise.resolve('[]') : lastViewed.promise,
  })
  try {
    await flush()
    mounted.state().openSymbol('000001.SZ', '新选择'); await flush()
    lastViewed.resolve(JSON.stringify({ code: '600001.SH', name: '旧选择' })); await flush()
    assert.equal(mounted.state().code, '000001.SZ')
  } finally { mounted.dispose() }
})

test('slow preference writes never delay selecting the next chart or its route', async () => {
  const firstWrite = deferred(), writes = []
  const mounted = mountComponent('KlineResearch.vue', {
    SaveStockKingPreference: (key, value) => { writes.push([key, JSON.parse(value)]); return firstWrite.promise },
  }, { code: '600001.SH' })
  try {
    await flush()
    mounted.state().openSymbol('000001.SZ', '新选择'); await flush()
    assert.equal(mounted.state().code, '000001.SZ')
    assert.equal(mounted.route.query.code, '000001.SZ')
    firstWrite.resolve(); await flush()
    assert.equal(writes.filter(([key]) => key === 'kline.lastViewedSymbol').at(-1)[1].code, '000001.SZ')
  } finally { mounted.dispose() }
})

test('a notebook response from an earlier visit cannot overwrite the current stock', async () => {
  const requests = []
  const mounted = mountComponent('KlineResearch.vue', {
    GetResearchNote: () => { const request = deferred(); requests.push(request); return request.promise },
  }, { code: '600001.SH' })
  try {
    mounted.state().showNotebook = true; await flush()
    mounted.state().openSymbol('000001.SZ'); await flush()
    mounted.state().openSymbol('600001.SH'); await flush()
    requests[2].resolve({ coreLogic: '最新笔记' }); await flush()
    requests[0].resolve({ coreLogic: '过期笔记' }); await flush()
    assert.equal(mounted.state().note.coreLogic, '最新笔记')
    requests[1].resolve({}); await flush()
  } finally { mounted.dispose() }
})

test('reselecting the current stock keeps its pending notebook requests valid', async () => {
  const forecast = deferred(), note = deferred()
  let calls = 0
  const mounted = mountComponent('KlineResearch.vue', {
    GetResearchNote: () => note.promise,
    GetForecast: async (_, horizon) => { calls++; await forecast.promise; return { horizon } },
  }, { code: '600001.SH' })
  try {
    mounted.state().showNotebook = true; await flush()
    mounted.state().openSymbol('600001.SH'); await flush()
    assert.equal(calls, 3)
    note.resolve({ coreLogic: '仍然有效' }); forecast.resolve(); await flush()
    assert.equal(mounted.state().note.coreLogic, '仍然有效')
    assert.equal(mounted.state().forecastsLoading, false)
    assert.equal(mounted.state().forecasts.length, 3)
  } finally { mounted.dispose() }
})

test('reopening retries failed forecasts without reloading a successful edited note', async () => {
  let noteCalls = 0, forecastCalls = 0
  const mounted = mountComponent('KlineResearch.vue', {
    GetResearchNote: async () => { noteCalls++; return { coreLogic: '保存的笔记' } },
    GetForecast: async (_, horizon) => {
      if (++forecastCalls <= 3) throw new Error('temporary bridge failure')
      return { horizon, available: true }
    },
  }, { code: '600001.SH' })
  try {
    mounted.state().showNotebook = true; await flush()
    assert.equal(mounted.state().forecastsLoading, false)
    mounted.state().note.coreLogic = '尚未保存的编辑'
    mounted.state().showNotebook = false; await flush()
    mounted.state().showNotebook = true; await flush()
    assert.equal(forecastCalls, 6)
    assert.equal(noteCalls, 1)
    assert.equal(mounted.state().note.coreLogic, '尚未保存的编辑')
    assert.equal(mounted.state().forecasts.every(item => item.available), true)
  } finally { mounted.dispose() }
})

test('reopening retries a failed note without repeating successful forecasts', async () => {
  let noteCalls = 0, forecastCalls = 0
  const mounted = mountComponent('KlineResearch.vue', {
    GetResearchNote: async () => { if (++noteCalls === 1) throw new Error('temporary failure'); return { coreLogic: '恢复的笔记' } },
    GetForecast: async (_, horizon) => { forecastCalls++; return { horizon } },
  }, { code: '600001.SH' })
  try {
    mounted.state().showNotebook = true; await flush()
    mounted.state().showNotebook = false; await flush()
    mounted.state().showNotebook = true; await flush()
    assert.equal(noteCalls, 2)
    assert.equal(forecastCalls, 3)
    assert.equal(mounted.state().note.coreLogic, '恢复的笔记')
  } finally { mounted.dispose() }
})

const row = (close = '12') => ({ day: '2026-10-13', open: '11', high: '13', low: '10', close, volume: '1000' })
function mountChart() {
  const requests = []
  const mounted = mountComponent('StockLightweightKlineChart.vue', {
    GetStockKLineWithFallback: (...args) => { const request = { ...deferred(), args }; requests.push(request); return request.promise },
  }, {}, { code: '600001.SH' })
  return { ...mounted, requests }
}

test('duplicate chart triggers share the pending request', async () => {
  const mounted = mountChart()
  try {
    await flush()
    mounted.state().loadData(); mounted.state().loadData(); await flush()
    assert.equal(mounted.requests.length, 1)
    mounted.requests[0].resolve({ source: 'shared-market', data: [row()] }); await flush()
    assert.equal(mounted.state().defaultLatestRawRow.close, '12')
    assert.equal(mounted.state().loading, false)
  } finally { mounted.dispose() }
})

test('an older period reply cannot overwrite the selected chart or stop its spinner', async () => {
  const mounted = mountChart()
  try {
    await flush()
    mounted.state().activeKlt = '5'; await flush()
    assert.equal(mounted.requests.length, 2)
    mounted.requests[0].resolve({ source: 'old', data: [row('10')] }); await flush()
    assert.equal(mounted.state().loading, true)
    assert.equal(mounted.state().activeDataSource, '')
    mounted.requests[1].resolve({ source: 'current', data: [row('15')] }); await flush()
    assert.equal(mounted.state().defaultLatestRawRow.close, '15')
    assert.equal(mounted.state().activeDataSource, 'current')
    assert.equal(mounted.state().loading, false)
  } finally { mounted.dispose() }
})

test('returning to a period ignores an earlier request for that same period', async () => {
  const mounted = mountChart()
  try {
    await flush()
    mounted.state().activeKlt = '5'; await flush()
    mounted.state().activeKlt = '101'; await flush()
    mounted.requests[2].resolve({ source: 'current', data: [row('15')] }); await flush()
    mounted.requests[0].resolve({ source: 'stale', data: [row('10')] }); await flush()
    mounted.requests[1].reject(new Error('old failure')); await flush()
    assert.equal(mounted.state().defaultLatestRawRow.close, '15')
    assert.equal(mounted.state().errorText, '')
  } finally { mounted.dispose() }
})

test('switching from a stock to an ETF requests its default adjustment only once', async () => {
  const mounted = mountChart()
  try {
    await flush()
    mounted.props.code = '510300.SH'; await flush()
    assert.equal(mounted.requests.length, 2)
    assert.equal(mounted.requests[1].args[4], 'none')
    mounted.requests[1].resolve({ data: [row()] }); await flush()
    mounted.requests[0].resolve({ data: [] }); await flush()
  } finally { mounted.dispose() }
})
