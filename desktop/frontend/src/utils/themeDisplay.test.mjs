import test from 'node:test'
import assert from 'node:assert/strict'
import { themeBoardDisplay, themeFeedbackDisplay, themeIdentity, themePercent } from './themeDisplay.mjs'

const now = Date.parse('2026-11-13T10:30:00+08:00')
const board = () => ({ status: 'observed', source: 'test:concept', sourceTime: '2026-11-13T10:29:00+08:00',
  themes: [{ id: 'BK001', name: '测试题材', rank: 1, selected: true, heatScore: .8, stage: '持续', changePct: 2,
    relativeStrengthPct: 1.2, breadthPct: 70, amountSharePct: 4.2, feedbackPenalty: .04,
    sourceTime: '2026-11-13T10:29:00+08:00', coverage: { complete: true, validCount: 8, expectedCount: 8, pct: 100 } }] })

test('theme evidence keeps backend membership, scope and frozen ordering intact', () => {
  const raw = board(), before = JSON.stringify(raw)
  const shown = themeBoardDisplay(raw, now)
  assert.equal(shown.fresh, true)
  assert.equal(shown.selected.length, 1)
  assert.equal(shown.themes[0].amountSharePct, 4.2)
  assert.equal(shown.themes[0].heatScore, .8)
  assert.match(shown.scope, /成分可能重叠/)
  assert.match(shown.scope, /概念涨幅中位数/)
  assert.equal(JSON.stringify(raw), before)
})

test('unavailable, stale, future and historical boards never claim current evidence', () => {
  assert.equal(themeBoardDisplay(board(), now, true).fresh, false)
  assert.match(themeBoardDisplay(board(), now, true).title, /历史/)
  assert.equal(themeBoardDisplay(board(), now + 300000).fresh, false)
  assert.equal(themeBoardDisplay(board(), now - 90000).fresh, false)
  assert.equal(themeBoardDisplay({ ...board(), source: '' }, now).fresh, false)
  assert.equal(themeBoardDisplay({ ...board(), status: 'unavailable' }, now).fresh, false)
  assert.equal(themeBoardDisplay(undefined, now).selected.length, 0)
  assert.match(themeBoardDisplay(undefined, now).emptyMessage, /尚未记录/)
})

test('a whole concept universe produces only mainline cards and a bounded rank preview', () => {
  const raw = board()
  raw.themes = Array.from({ length: 400 }, (_, index) => ({ ...raw.themes[0], id: `BK${String(index).padStart(4, '0')}`,
    name: `题材${index}`, rank: index + 1, selected: [0, 2, 5].includes(index) }))
  const shown = themeBoardDisplay(raw, now)
  assert.equal(shown.themes.length, 400)
  assert.equal(shown.cards.length, 3)
  assert.deepEqual(shown.cards.map(item => item.rank), [1, 3, 6])
  assert.equal(shown.rankedPreview.length, 20)
  const feedback = themeFeedbackDisplay({ status: 'partial', asOf: '2026-11-12T15:30:00+08:00',
    themes: raw.themes.map(row => ({ ...row, observedCount: 0, status: 'insufficient' })) }, now)
  assert.equal(feedback.themes.length, 3)
  assert.equal(feedback.unobservedThemeCount, 397)
})

test('missing numeric evidence stays missing, not zero or invented breadth', () => {
  const raw = board()
  Object.assign(raw.themes[0], { heatScore: 80, breadthPct: 101, amountSharePct: false, feedbackPenalty: -.1,
    relativeStrengthPct: null, stage: 'unconfirmed' })
  raw.themes.push(raw.themes[0], { id: '', name: '缺代码' })
  const shown = themeBoardDisplay(raw, now)
  assert.equal(shown.themes.length, 1)
  for (const field of ['heatScore', 'breadthPct', 'amountSharePct', 'feedbackPenalty', 'relativeStrengthPct']) assert.equal(shown.themes[0][field], null)
  assert.equal(shown.themes[0].stage, '阶段待确认')
  assert.equal(themePercent(null), '未取得')
  assert.equal(themePercent(false), '未取得')
  assert.equal(themePercent(0), '0.00%')
  assert.equal(themePercent(-1.23, 2, true), '-1.23%')
  assert.equal(themeIdentity({ name: '行业不可冒充题材' }), null)
})

test('feedback preserves observed comparisons and only bounded penalties', () => {
  const raw = { status: 'partial', asOf: '2026-11-12T15:31:00+08:00', sourceDay: '2026-11-12', summary: '核验部分成分',
    themes: [{ id: 'BK001', name: '测试题材', continuation: 'weakened', selectedMeanChangePct: null,
      attributions: [{ code: 'theme_weakened', label: '题材后续走弱', evidence: '核验变化为负' }] }],
    missedLeaders: [{ code: '600001', observationChangePct: 2.5, researched: true }],
    adjustments: { BK001: .04, BK002: -.04, BK003: 2, BK004: 0 }, gaps: ['缺部分收盘报价'] }
  const before = JSON.stringify(raw)
  const shown = themeFeedbackDisplay(raw, now)
  assert.equal(shown.available, true)
  assert.equal(shown.themes[0].continuationLabel, '后续走弱')
  assert.equal(shown.themes[0].selectedMeanChangePct, null)
  assert.equal(shown.missedLeaders[0].observationChangePct, 2.5)
  assert.deepEqual(shown.adjustments, [{ id: 'BK001', penalty: .04 }])
  assert.match(shown.meaning, /不是成交收益/)
  assert.equal(JSON.stringify(raw), before)
})

test('future or expired feedback cannot provide active adjustments', () => {
  const raw = { status: 'reviewed', asOf: '2026-11-13T15:31:00+08:00', themes: [{ id: 'BK001' }],
    missedLeaders: [{ code: '600001' }], adjustments: { BK001: .05 } }
  const shown = themeFeedbackDisplay(raw, now)
  assert.equal(shown.available, false)
  assert.equal(shown.themes.length, 0)
  assert.equal(shown.missedLeaders.length, 0)
  assert.deepEqual(shown.adjustments, [])
  assert.deepEqual(themeFeedbackDisplay({ ...raw, status: 'expired', asOf: '2026-11-01T15:30:00+08:00' }, now).adjustments, [])
  assert.equal(themeFeedbackDisplay({ status: 'no_history' }, now).available, false)
  assert.equal(themeFeedbackDisplay({ status: 'no_scans' }, now).available, false)
  assert.equal(themeFeedbackDisplay({ status: 'pending_data' }, now).available, false)
})
