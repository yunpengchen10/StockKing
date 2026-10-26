import test from 'node:test'
import assert from 'node:assert/strict'
import { entryExecutionSummary, dailyObservationRows, dailyObservationStatus, settlementHealthSummary } from './recommendationQuality.mjs'
import { predictionSummary } from './learningStatus.mjs'

test('frozen entry display never claims a fill or resets trigger', () => {
  const result = entryExecutionSummary({ state: 'above_entry_band', triggerAt: '2026-10-26T10:00:05+08:00', triggerPrice: 10,
    minimumPrice: 9.95, maximumPrice: 10.05, invalidationPrice: 9.8, expiresAt: '2026-10-26T10:05:05+08:00', targetSession: '2026-10-27', modelEligible: false })
  assert.match(result.label, /超过追价上限.*尚未通过/)
  assert.match(result.detail, /9.95–.*10.05/)
  assert.match(result.meaning, /非成交回执/)
})

test('observations and controls stay separate; missing outcomes remain missing', () => {
  const source = { items: [
    { code: '1', observation_id: 'a', first_category: 'observation_watchlist', reference_verified: 0, outcomes: [] },
    { code: '2', observation_id: 'b', first_category: 'unselected_control', outcomes: [{ horizon: 1, status: 'observed', gross_reference_change_pct: -1 }] },
  ] }
  assert.equal(dailyObservationRows(source).length, 1)
  assert.equal(dailyObservationRows(source)[0].status, 'not_reviewed')
  assert.equal(dailyObservationRows(source, true).length, 2)
  assert.match(dailyObservationStatus('not_matured'), /保存时/)
  assert.match(settlementHealthSummary({ dueCount: 3, overdueCount: 2, missingDataCount: 1 }), /应结算 3.*逾期待处理 2.*行情待补 1/)
})

test('net profit probability is distinct from five percent target probability', () => {
  const result = predictionSummary({ status: 'active', expectedNetReturn: .01, winProbability: .55, bigMoveProbability: .2 })
  assert.match(result.detail, /扣费盈利概率 55.00%/)
  assert.match(result.detail, /净收益达到5%概率 20.00%/)
  assert.match(predictionSummary({ status: 'active', bigMoveProbability: .9 }).detail, /扣费盈利概率 未取得/)
})
