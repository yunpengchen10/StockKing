import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { groupSignals, ledgerPercent, reviewReturn, shanghaiDate, learningProgress } from './pickLedger.mjs'

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
