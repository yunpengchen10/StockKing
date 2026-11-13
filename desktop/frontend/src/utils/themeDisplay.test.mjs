import test from 'node:test'
import assert from 'node:assert/strict'
import { marketBriefDisplay, themeBoardDisplay, themeFeedbackDisplay, themeIdentity, themePercent } from './themeDisplay.mjs'

const now = Date.parse('2026-11-13T10:30:00+08:00')
const board = () => ({ status: 'observed', source: 'test:concept', sourceTime: '2026-11-13T10:29:00+08:00',
  themes: [{ id: 'BK001', name: '测试题材', rank: 1, selected: true, heatScore: .8, stage: '持续', changePct: 2,
    relativeStrengthPct: 1.2, breadthPct: 70, amountSharePct: 4.2, feedbackPenalty: .04,
    sourceTime: '2026-11-13T10:29:00+08:00', coverage: { complete: true, validCount: 8, expectedCount: 8, pct: 100 } }] })

const brief = () => ({ version: 'market-brief-v1', asOf: '2026-11-13T10:30:00+08:00', status: 'observed',
  mainlines: [{ id: 'event-1', name: '事件题材样本', rank: 1, catalyst: '已发布的事件线索', status: 'continuation',
    eventDate: '2026-11-12', eventSourceTime: '2026-11-12T14:00:00+08:00', source: 'fixture:events',
    quoteCoverage: { validCount: 1, expectedCount: 2 }, leaders: ['600001'] }],
  focusStocks: [{ code: '600001', name: '关联股样本', themeId: 'event-1', themeName: '事件题材样本',
    price: 10.5, changePct: 2, sourceTime: '2026-11-13T10:29:50+08:00', source: 'fixture:quotes',
    status: 'watch', reason: '昨日事件关联，今日真实报价延续', pending: ['30分钟资金窗口待确认'], ruleRecommendation: false }],
  gaps: ['完整成分尚未补齐'] })

test('market event observations survive missing trading gates without becoming recommendations or changing dates', () => {
  const raw = brief(), before = JSON.stringify(raw)
  const shown = marketBriefDisplay(raw, now)
  assert.equal(shown.focusStocks.length, 1)
  assert.equal(shown.focusStocks[0].conditionsMet, false)
  assert.equal(shown.focusStocks[0].statusLabel, '观察 / 等待确认')
  assert.equal(shown.focusStocks[0].price, 10.5)
  assert.equal(shown.mainlines[0].eventDate, '2026-11-12')
  assert.equal(shown.mainlines[0].statusLabel, '昨日题材今日跟踪')
  assert.match(shown.mainlines[0].rankScope, /非全概念成分排名/)
  assert.equal(JSON.stringify(raw), before)
})

test('condition claims require matching original recommendations and current evidence, while old quotes stay reference only', () => {
  const raw = brief()
  Object.assign(raw.focusStocks[0], { status: 'conditions_met', ruleRecommendation: true })
  assert.equal(marketBriefDisplay(raw, now).focusStocks[0].conditionsMet, false)
  const cards = [{ pick: { code: 'sh600001' }, isFlowStrategy: true, current: true }]
  assert.equal(marketBriefDisplay(raw, now, false, cards).focusStocks[0].conditionsMet, true)
  const expired = marketBriefDisplay(raw, now + 30001, false, cards)
  assert.equal(expired.focusStocks[0].conditionsMet, false)
  assert.match(expired.focusStocks[0].statusLabel, /扫描时条件已满足.*待复核/)
  assert.match(expired.focusStocks[0].quoteLabel, /历史参考价/)
  assert.equal(marketBriefDisplay(raw, now, true, cards).focusStocks[0].conditionsMet, false)
  assert.match(marketBriefDisplay(raw, now, true, cards).focusStocks[0].quoteLabel, /历史快照/)
  raw.focusStocks[0].quoteFreshness = 'session_reference'
  assert.match(marketBriefDisplay(raw, now + 3600000).focusStocks[0].quoteLabel, /交易时段参考价 · 非实时/)
  assert.equal(marketBriefDisplay(raw, now + 3600000).focusStocks[0].conditionsMet, false)
})

test('market brief rejects invalid and future quotes rather than filling prices or borrowing legacy recommendations', () => {
  const raw = brief(), valid = raw.focusStocks[0]
  raw.focusStocks = [{ ...valid, code: '600002', price: null }, { ...valid, code: '600003', source: '' },
    { ...valid, code: '600004', sourceTime: '2026-11-13T10:30:01+08:00' },
    { ...valid, code: '600005', sourceTime: '2026-11-13T10:29:50' }, { ...valid, code: '600006', price: true }]
  const shown = marketBriefDisplay(raw, now, false, [{ pick: { code: '600099', name: '旧股' }, price: 5 }])
  assert.equal(shown.focusStocks.length, 0)
  assert.equal(shown.invalidCount, 5)
  assert.match(shown.gaps.at(-1), /5 只关联股票/)
})

test('an empty event brief preserves existing real recommendations with their original qualification', () => {
  const raw = brief()
  raw.focusStocks = []
  const shown = marketBriefDisplay(raw, now, false, [{ pick: { code: '600099', name: '原推荐样本' }, price: 12,
    source: 'fixture', sourceTime: '2026-11-13T10:29:50+08:00', current: true, isFlowStrategy: true,
    supports: ['已保存的原准入证据'], missing: [], counterEvidence: [], quoteLabel: '源时间在30秒内' }])
  assert.equal(shown.focusStocks[0].name, '原推荐样本')
  assert.equal(shown.focusStocks[0].conditionsMet, true)
  assert.equal(shown.focusStocks[0].reason, '已保存的原准入证据')
})

test('watch feedback uses its independent coverage and never reports missing close evidence as a zero return', () => {
  const shown = themeFeedbackDisplay({ status: 'no_scans', asOf: '2026-11-12T15:31:00+08:00',
    marketBriefFeedback: { status: 'partial', sourceDay: '2026-11-12', asOf: '2026-11-12T15:30:00+08:00',
      summary: '重点观察已核验1只', rows: [{ code: '600001', referencePrice: 10, closePrice: 10.5, changePct: 5 },
        { code: '600002', referencePrice: 20, closePrice: null, changePct: 0, gaps: ['缺收盘证据'] }] } }, now)
  assert.equal(shown.available, false)
  assert.equal(shown.marketBriefFeedback.available, true)
  assert.equal(shown.marketBriefFeedback.rows[0].changePct, 5)
  assert.equal(shown.marketBriefFeedback.rows[1].changePct, null)
  assert.equal(shown.marketBriefFeedback.rows[1].closePrice, null)
  assert.match(shown.marketBriefFeedback.meaning, /不计入规则精选收益/)
})

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
