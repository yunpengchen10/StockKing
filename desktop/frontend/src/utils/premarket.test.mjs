import test from 'node:test'
import assert from 'node:assert/strict'
import { premarketDisplayState, premarketQuoteState, premarketPlanLabel, premarketText } from './premarket.mjs'

const at = clock => Date.parse(`2026-10-16T${clock}+08:00`)
const assessment = { decisionAt: '2026-10-16T09:19:00+08:00', sessionDate: '2026-10-16', expiresAt: '2026-10-16T09:35:00+08:00' }
const quotePick = { planStatus: 'awaiting_auction_confirmation', referencePriceKind: 'current_quote', quoteStatus: 'fresh', sourceTime: '2026-10-16T09:20:00+08:00' }

test('Shanghai auction phases change at 09:20, 09:25, 09:30 and expire at 09:35', () => {
  assert.equal(premarketDisplayState(assessment, at('09:19:59')).phaseLabel, '集合竞价 · 可撤单阶段')
  assert.equal(premarketDisplayState(assessment, at('09:20:00')).phaseLabel, '集合竞价 · 不可撤单阶段')
  assert.equal(premarketDisplayState(assessment, at('09:25:00')).phaseLabel, '竞价结束 · 开盘前预案')
  assert.equal(premarketDisplayState(assessment, at('09:30:00')).phaseLabel, '开盘衔接预案')
  assert.equal(premarketDisplayState(assessment, at('09:34:59')).current, true)
  assert.equal(premarketDisplayState(assessment, at('09:35:00')).historical, true)
  assert.equal(premarketDisplayState({ ...assessment, expiresAt: '2026-10-16T10:30:00+08:00' }, at('09:35:00')).current, false)
})

test('previous dates, future or ambiguous decision times and explicit historical views cannot become current plans', () => {
  assert.equal(premarketDisplayState(assessment, Date.parse('2026-10-19T09:20:00+08:00')).current, false)
  assert.equal(premarketDisplayState(assessment, at('09:18:59')).current, false)
  assert.equal(premarketDisplayState({ ...assessment, decisionAt: '2026-10-16T09:19:00' }, at('09:20:00')).current, false)
  assert.equal(premarketDisplayState(assessment, at('09:20:00'), true).historical, true)
  assert.equal(premarketDisplayState(null).available, false)
})

test('stored quote verification ages out without another scan and never certifies auction matching', () => {
  const display = premarketDisplayState(assessment, at('09:20:00'))
  assert.equal(premarketQuoteState(quotePick, display, at('09:20:30')).current, true)
  assert.equal(premarketQuoteState(quotePick, display, at('09:20:31')).current, false)
  assert.equal(premarketQuoteState(quotePick, display, at('09:19:59')).current, false)
  assert.match(premarketQuoteState(quotePick, display, at('09:20:00')).label, /普通报价/)
  assert.equal(premarketQuoteState({ ...quotePick, referencePriceKind: 'previous_close', quoteStatus: 'missing' }, display, at('09:20:00')).priceLabel, '昨收基准价')
  const historical = premarketDisplayState(assessment, at('09:35:00'))
  assert.match(premarketQuoteState(quotePick, historical, at('09:35:00')).label, /历史快照/)
  assert.equal(premarketPlanLabel(quotePick, historical), '历史条件预案')
})

test('condition text preserves only recorded readable strings', () => {
  assert.equal(premarketText(['价格确认', null, '成交核验']), '价格确认；成交核验')
  assert.equal(premarketText({ score: 90 }, '未记录'), '未记录')
})
