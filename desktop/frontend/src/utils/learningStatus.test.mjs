import test from 'node:test'
import assert from 'node:assert/strict'
import { learningTimeline, learningTaskLabel, predictionSummary, portfolioSummary, nextDayPredictionSummary } from './learningStatus.mjs'

test('legacy check timestamp cannot be displayed as a completed fit', () => {
  const timeline = learningTimeline({ lastTrainingAt: '2026-10-09T15:45:00+08:00' })
  assert.equal(timeline[0].at, '2026-10-09T15:45:00+08:00')
  assert.equal(timeline[1].at, null)
  assert.equal(timeline[3].at, null)
})

test('cold start and rejected predictions do not display stale numeric forecasts', () => {
  for (const status of ['rules_unvalidated', 'data_rejected', 'unknown']) {
    const result = predictionSummary({ status, expectedNetReturn: .99, bigMoveProbability: .88 })
    assert.ok(!result.detail.includes('99.00%'))
    assert.ok(!result.detail.includes('88.00%'))
  }
})

test('validated return, probability and lower quantile retain their units and meaning', () => {
  const result = predictionSummary({ status: 'active', entryEligible: false,
    expectedNetReturn: .01, bigMoveProbability: .3, lowerReturnQuantile: -.05 })
  assert.match(result.label, /未发现/)
  assert.match(result.detail, /1.00%/)
  assert.match(result.detail, /30.00%/)
  assert.match(result.detail, /-5.00%/)
  assert.match(result.detail, /不是预期收益置信下界/)
})

test('unknown forecast is not coerced into zero and task labels preserve T+1', () => {
  assert.match(predictionSummary({ status: 'active' }).detail, /未取得/)
  assert.match(learningTaskLabel('entry_next_session_exit_following_session'), /再下一交易日/)
  assert.match(learningTaskLabel('next_session_price_event', 'continuation'), /行情事件.*延续/)
  assert.match(portfolioSummary({ plannedExposure: .5, cashWeight: .5, totalExposureCap: .5 }), /50.00%/)
})

test('next-day price observations retain their own prediction meaning', () => {
  assert.doesNotMatch(nextDayPredictionSummary({ learningStatus: 'rules_unvalidated', probability: .9 }), /90.00%/)
  const summary = nextDayPredictionSummary({ learningStatus: 'active', probability: .3, expectedPriceReturn: .01, returnQ10: -.02 })
  assert.match(summary, /价格模式概率 30.00%/)
  assert.match(summary, /不代表可成交收益/)
  assert.doesNotMatch(summary, /预期净收益/)
})
