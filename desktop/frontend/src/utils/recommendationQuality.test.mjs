import test from 'node:test'
import assert from 'node:assert/strict'
import { entryExecutionSummary, dailyObservationRows, dailyObservationStatus, settlementHealthSummary, primaryReadinessSummary } from './recommendationQuality.mjs'
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

test('opportunity reference changes retain their independent category and controls stay opt-in', () => {
  const review = { items: [
    { code: '600001', observation_id: 'opportunity-first', first_category: 'opportunity_recommendation', reference_price: 10,
      outcomes: [{ horizon: 1, status: 'observed', gross_reference_change_pct: -2 }] },
    { code: '600001', observation_id: 'opportunity-control', first_category: 'opportunity_control', reference_price: 9.8, outcomes: [] },
    { code: '600001', observation_id: 'daily-first', first_category: 'observation_watchlist', reference_price: 10.2, outcomes: [] },
  ] }
  const rows = dailyObservationRows(review)
  assert.deepEqual(rows.map(row => row.category), ['机会精选', '日线观察'])
  assert.deepEqual(rows.map(row => row.referencePrice), [10, 10.2])
  assert.equal(rows[0].gross_reference_change_pct, -2)
  assert.equal(rows[0].net_return, undefined)
  assert.equal(dailyObservationRows(review, true)[1].category, '机会对照')
  assert.equal(dailyObservationRows(review, true)[1].status, 'not_reviewed')
})

test('net profit probability is distinct from five percent target probability', () => {
  const result = predictionSummary({ status: 'active', expectedNetReturn: .01, winProbability: .55, bigMoveProbability: .2 })
  assert.match(result.detail, /扣费盈利概率 55.00%/)
  assert.match(result.detail, /净收益达到5%概率 20.00%/)
  assert.match(predictionSummary({ status: 'active', bigMoveProbability: .9 }).detail, /扣费盈利概率 未取得/)
})

test('cold-start distinguishes unknown audit counts from real zero and manual scans from training samples', () => {
  const unknown = primaryReadinessSummary({ progressKnown: false, modelUsableNow: false, minimumSignalDaysForAnyTraining: 156 }, { recordingMode: 'manual_audit_only', trainingEligibleCount: 99 })
  assert.match(unknown.title, /准备中/)
  assert.match(unknown.sampleProgress, /数量未知/)
  assert.doesNotMatch(unknown.sampleProgress, /0 条/)
  assert.match(unknown.recording, /手动扫描.*不增加正式训练样本/)
  assert.match(unknown.requirement, /156.*留出.*前瞻/)
  const zero = primaryReadinessSummary({ progressKnown: true, progressFresh: false, progress: { validSamples: 0, validSampleDays: 0 } }, { recordingMode: 'official_frozen', trainingEligibleCount: 0, trainingControlCount: 0 })
  assert.match(zero.sampleProgress, /有效样本 0 条/)
  assert.match(zero.audit, /历史统计/)
  assert.match(zero.recording, /训练资格 0 条.*对照 0 条/)
  const ready = primaryReadinessSummary({ status: 'active', modelUsableNow: true, configuredChampionVersion: 'v1' })
  assert.equal(ready.modelPending, false)
  assert.match(ready.title, /已就绪/)
  assert.match(primaryReadinessSummary({ configuredChampionVersion: 'v1', modelUsableNow: null }).title, /待核验/)
  const unreadable = primaryReadinessSummary({ modelStatus: 'unknown', modelUsableNow: null, blockerCodes: ['primary_registry_unavailable'] })
  assert.equal(unreadable.modelPending, false)
  assert.match(unreadable.title, /待核验/)
})
