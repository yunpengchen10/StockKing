import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { groupSignals, ledgerPercent, ledgerPrice, ledgerSigned, reviewReturn, shanghaiDate, learningProgress, recordQuoteCode, recordQuoteCodes, signalPriceComparison, createRecordQuoteLoader } from './pickLedger.mjs'

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
