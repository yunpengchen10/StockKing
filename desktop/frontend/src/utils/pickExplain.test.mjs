import test from 'node:test'
import assert from 'node:assert/strict'
import { explainPick, normalizeIndicatorEvidence, safeEvidenceURL } from './pickExplain.mjs'

test('independent checks retain failures and exact missing baseline dates', () => {
  const result=explainPick({evidenceChecks:{sector_resonance:true,history_20d:false,fund_direction:false},
    baselineEvidence:{historyDays:8,missingHistoryDates:['2026-09-01'],baselineSource:'Sina'}})
  assert.deepEqual(result.evidenceChecks.map(x=>x.passed),[true,false,false])
  assert.equal(result.baselineDays,8)
  assert.deepEqual(result.missingHistoryDates,['2026-09-01'])
  assert.deepEqual(explainPick({}).evidenceChecks,[])
})

test('daily recommendations expose recorded reasons, risk, indicators and execution conditions', () => {
  const result = explainPick({
    recommendationLogicVersion: 'evidence-v2', strategyBranch: '高波动修复', modelBranch: '本地规则观察',
    selectionReasons: ['20日涨幅 18%，回踩支撑后修复', '量比 1.8 倍，成交额达到流动性门槛'],
    riskReasons: ['20日波动率 6.2%，短期回撤风险高'], risks: ['20日波动率 6.2%，短期回撤风险高'],
    indicatorEvidence: [{ key: 'volatility20d', label: '20日波动率', value: 6.2, unit: '%', threshold: '≥ 5% 视为高波动',
      source: '已完成日K线', asOf: '2026-09-14', status: 'warning', role: 'risk' }],
    entryPlan: { trigger: '重新站上前高', invalidations: ['跌破支撑', '行情过期'], noChase: '高开超过 5% 不追', holdingWindow: 'T 至 T+5' },
  })
  assert.equal(result.title, '高波动修复')
  assert.equal(result.reasons.length, 2)
  assert.equal(result.risks.length, 1)
  assert.deepEqual(result.indicators[0], { key: 'volatility20d', label: '20日波动率', value: '6.2%', threshold: '≥ 5% 视为高波动', source: '已完成日K线', asOf: '2026-09-14', status: '需注意', role: '风险指标' })
  assert.equal(result.entryPlan.invalidations, '跌破支撑；行情过期')
  assert.equal(result.entryPlan.noChase, '高开超过 5% 不追')
  assert.match(result.scoreMeaning, /不是胜率/)
})

test('old local thesis is retained as original text and never mislabelled as AI evidence', () => {
  const thesis = '本地5日排序；依据：三档面板模型未通过发布门槛：无模型；当前仅显示规则观察分。'
  const result = explainPick({ modelBranch: '本地规则观察', thesis, features: { volume_ratio: 0, turnover_rate: 3.45 }, quote: { price: 12.3, source: 'tencent', provider_timestamp: '2026-09-14T14:55:00+08:00' } })
  assert.equal(result.savedExplanation, thesis)
  assert.match(result.reason, /旧记录未保存具体入选理由/)
  assert.equal(result.reasons.length, 0)
  assert.equal(result.title, '本地规则观察')
  assert.ok(!result.title.includes('AI'))
  const price = result.indicators.find(row => row.key === 'price')
  assert.equal(price.value, '12.3元')
  assert.equal(price.source, 'tencent')
  assert.equal(price.asOf, '2026-09-14T14:55:00+08:00')
  const volume = result.indicators.find(row => row.key === 'volume_ratio')
  assert.equal(volume.value, '0倍')
  assert.equal(volume.source, '未取得')
  assert.equal(volume.asOf, '未取得')
  assert.equal(volume.threshold, '未取得')
})

test('a branch label alone cannot invent reasons for a historical stock', () => {
  const result = explainPick({ model: 'M6-B', features: { recent_limitups_10d: 3, recovery_ratio: 0.7 } })
  assert.match(result.reason, /旧记录未保存/)
  assert.equal(result.reasons.length, 0)
  assert.equal(result.indicators.find(row => row.key === 'recovery_ratio').value, '70%')
  assert.equal(result.indicators.find(row => row.key === 'recovery_ratio').threshold, '未取得')
})

test('missing, invalid, zero and false indicators retain distinct meaning', () => {
  const rows = normalizeIndicatorEvidence([
    { label: '缺失量比', value: null, unit: '倍', status: 'passed' },
    { label: '涨幅', value: 0, unit: '%', source: 'snapshot', status: 'passed' },
    { label: '趋势多头', value: false, threshold: true, status: 'failed' },
    { label: '无效', value: NaN, unit: '%' },
    null,
  ])
  assert.equal(rows.length, 4)
  assert.equal(rows[0].value, '未取得')
  assert.equal(rows[0].status, '未取得')
  assert.equal(rows[0].source, '未取得')
  assert.equal(rows[1].value, '0%')
  assert.equal(rows[1].status, '满足')
  assert.equal(rows[2].value, '否')
  assert.equal(rows[2].threshold, '是')
  assert.equal(rows[3].value, '未取得')
})

test('new evidence-insufficient records do not gain synthetic reasons or numeric evidence', () => {
  const result = explainPick({ recommendationLogicVersion: 'evidence-v2', selectionReasons: [], indicatorEvidence: [], riskReasons: ['成交额未取得'], status: 'data_insufficient' })
  assert.equal(result.reasons.length, 0)
  assert.match(result.reason, /未保存充分/)
  assert.equal(result.indicators.length, 0)
  assert.deepEqual(result.risks, ['成交额未取得'])
  assert.equal(result.entryPlan.trigger, '未取得')
})

test('model availability warnings cannot become a reason and unsafe evidence links are rejected', () => {
  const result = explainPick({ selectionReasons: ['模型未通过发布门槛；当前仅显示规则观察分。'], evidence: [{ url: 'javascript:alert(1)' }] })
  assert.deepEqual(result.reasons, [])
  assert.equal(result.evidence[0].url, '')
  assert.equal(safeEvidenceURL('https://example.com/report'), 'https://example.com/report')
  assert.doesNotThrow(() => explainPick(null))
})
