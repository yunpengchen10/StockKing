import test from 'node:test'
import assert from 'node:assert/strict'
import { opportunityText, opportunityCard, opportunityDisplay } from './opportunityDisplay.mjs'

const now = Date.parse('2026-10-28T10:30:00+08:00')
const sample = () => ({ code: '600001', referencePrice: 10.1, quote: { price: 10.1, source: 'tencent', source_time: '2026-10-28T10:29:45+08:00' },
  opportunity: { channel: 'E', grade: 'E-A', supports: ['涨速提升'], missing: '板块同步待确认', firstDetectionAt: '2026-10-28T10:29:00+08:00', rankScore: 82 } })

test('missing confirmations remain visible without rejecting or upgrading the backend E-A decision', () => {
  const pick = sample()
  const card = opportunityCard(pick, now)
  assert.equal(card.grade, 'E-A')
  assert.equal(card.channelLabel, '早期启动')
  assert.deepEqual(card.missing, ['板块同步待确认'])
  assert.equal(card.current, true)
  assert.equal(card.triggerAt, null)
  assert.equal(card.remainingSpacePct, null)
  assert.equal(card.firstDetectionAt, pick.opportunity.firstDetectionAt)
  assert.equal(pick.executable, undefined)
})

test('quote expiry, missing source time and historical viewing preserve rows but cannot claim current prices', () => {
  const pick = sample()
  const before = JSON.stringify(pick)
  assert.equal(opportunityCard(pick, now + 16000).current, false)
  assert.match(opportunityCard(pick, now + 16000).quoteLabel, /历史参考价/)
  assert.equal(opportunityCard(pick, now, true).current, false)
  assert.match(opportunityCard({ ...pick, quote: { price: 10.1, fetched_at: '2026-10-28T10:30:00+08:00' } }, now).quoteLabel, /待核验/)
  assert.equal(opportunityDisplay({ recommendations: [pick] }, now + 16000).cards.length, 1)
  assert.equal(JSON.stringify(pick), before)
})

test('future source timestamps and explicitly rejected quotes are never current', () => {
  const pick = sample()
  assert.equal(opportunityCard(pick, now - 20000).current, false)
  assert.match(opportunityCard(pick, now - 20000).quoteLabel, /源时间异常/)
  assert.equal(opportunityCard({ ...pick, quote: { ...pick.quote, fresh: false } }, now).current, false)
})

test('text compatibility preserves supplied evidence without stringifying unknown objects', () => {
  assert.deepEqual(opportunityText(['局部突破', { message: '量能增加' }, { missing: true }, { label: '板块待确认' }, '局部突破']), ['局部突破', '量能增加', '板块待确认'])
  assert.deepEqual(opportunityDisplay({ recommendations: null, observations: {} }, now).cards, [])
  assert.equal(opportunityDisplay(undefined, now).available, false)
})
