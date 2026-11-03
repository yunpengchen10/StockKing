import test from 'node:test'
import assert from 'node:assert/strict'
import { opportunityText, opportunityCard, opportunityDisplay, opportunityFundingStatus } from './opportunityDisplay.mjs'

const now = Date.parse('2026-10-28T10:30:00+08:00')
const sample = () => ({ code: '600001', referencePrice: 10.1, quote: { price: 10.1, source: 'tencent', source_time: '2026-10-28T10:29:45+08:00' },
  opportunity: { version: 'opportunity-flow-20261103-v1', channel: 'F', grade: 'F-A', supports: ['资金与持续趋势已核验'], missing: '独立催化待确认', firstDetectionAt: '2026-10-28T10:29:00+08:00', rankScore: null } })

test('missing confirmations remain visible without rejecting or upgrading the backend F-A decision', () => {
  const pick = sample()
  const card = opportunityCard(pick, now)
  assert.equal(card.grade, 'F-A')
  assert.equal(card.channelLabel, '资金与持续趋势')
  assert.deepEqual(card.missing, ['独立催化待确认'])
  assert.equal(card.current, true)
  assert.equal(card.triggerAt, null)
  assert.equal(card.remainingSpacePct, null)
  assert.equal(card.firstDetectionAt, pick.opportunity.firstDetectionAt)
  assert.equal(pick.executable, undefined)
})

test('legacy E/M decisions retain original scores but are never called current flow recommendations', () => {
  for (const [channel, version] of [['E', 'opportunity-em-20261028-v1'], ['M', 'opportunity-em-20261029-v2']]) {
    const pick = sample()
    Object.assign(pick.opportunity, { version, channel, grade: `${channel}-A`, rankScore: 100 })
    const before = JSON.stringify(pick)
    const card = opportunityCard(pick, now)
    assert.equal(card.current, false)
    assert.equal(card.legacy, true)
    assert.equal(card.isFlowStrategy, false)
    assert.match(card.channelLabel, /历史/)
    assert.match(card.quoteLabel, /旧版规则记录/)
    assert.equal(card.rankScore, 100)
    assert.equal(card.rankingComposite, null)
    const shown = opportunityDisplay({ version, recommendations: [pick] }, now)
    assert.match(shown.title, /历史/)
    assert.match(shown.introduction, /未按当前/)
    assert.equal(JSON.stringify(pick), before)
  }
})

test('unversioned E/M collections remain legacy and unknown F versions cannot claim current qualification', () => {
  const pick = sample()
  Object.assign(pick.opportunity, { channel: 'E', grade: 'E-A', version: undefined })
  const legacy = opportunityDisplay({ recommendations: [pick] }, now)
  assert.equal(legacy.legacy, true)
  assert.match(legacy.title, /历史/)
  Object.assign(pick.opportunity, { channel: 'F', grade: 'F-A', version: 'opportunity-flow-future' })
  const unknown = opportunityCard(pick, now)
  assert.equal(unknown.current, false)
  assert.match(unknown.quoteLabel, /策略版本未核验/)
})

test('flow cards expose source windows, coverage, normalized intensities and transparent rank components', () => {
  const pick = sample()
  const window = { status: 'observed', horizonMinutes: 30, source: 'eastmoney:minute', provider: 'eastmoney',
    asOf: '2026-10-28T10:29:00+08:00', windowStart: '2026-10-28T09:59:00+08:00' }
  Object.assign(pick.opportunity, {
    rankScore: 100, rankingComposite: .7, rankingComponents: { stockNetFlowRatio: { rawValue: .03, percentile: .8, weight: .2, direction: 'higher', peerCount: 12 } },
    metrics: { entryRiskAtr: 1.2, structuralLow30m: 9.8, netReturn30mPct: .5, pathEfficiency30m: .7 },
    flowEvidence: { stock: { ...window, metrics: { netFlowCny: 3000000, turnoverCny: 1e8, netFlowRatio: .03, positiveMinuteFraction: .8 } },
      sector: { ...window, sector: { name: '测试板块' }, coverage: { totalPeers: 10, usablePeers: 8, fraction: .8, excludedSelf: true },
        metrics: { netFlowCny: 1e7, netFlowRatio: .02, positiveMemberFraction: .75 } } },
  })
  const before = JSON.stringify(pick)
  const card = opportunityCard(pick, now)
  assert.equal(card.rankScore, null) // Never show a legacy 0-100 scale for the new strategy.
  assert.equal(card.rankingComposite, .7)
  assert.equal(card.rankingComponents[0].rawValue, .03)
  assert.equal(card.rankingComponents[0].weight, .2)
  assert.equal(card.funding.stock.netFlowRatio, .03) // UI converts ratios to percentages exactly once.
  assert.equal(card.funding.sector.coverageFraction, .8)
  assert.equal(card.funding.sector.totalPeers, 10)
  assert.equal(card.funding.sector.usablePeers, 8)
  assert.equal(card.funding.stock.sourceTime, window.asOf)
  assert.equal(card.funding.stock.windowStart, window.windowStart)
  assert.equal(card.entryRiskAtr, 1.2)
  assert.equal(JSON.stringify(pick), before)
})

test('incomplete or cumulative fund evidence cannot masquerade as a complete 30-minute window', () => {
  const pick = sample()
  pick.opportunity.flowEvidence = { stock: { status: 'unavailable', horizonMinutes: 30,
    metrics: { netFlowCny: 1e9, netFlowRatio: .9 } }, sector: { status: 'observed', horizonMinutes: 1,
    metrics: { netFlowCny: 1e9 } } }
  pick.opportunity.rankingComposite = 75
  const card = opportunityCard(pick, now)
  assert.equal(card.funding.stock.observed, false)
  assert.equal(card.funding.stock.netFlowCny, null)
  assert.equal(card.funding.sector.observed, false)
  assert.equal(card.funding.sector.netFlowCny, null)
  assert.equal(card.rankingComposite, null)
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

test('all missing 30-minute fund windows are an unavailable screen, not a completed zero selection', () => {
  const channels = { version: 'opportunity-flow-20261103-v1', recommendations: [],
    diagnostics: { researchedCount: 306, failedConditionCounts: { stock_flow_unavailable: 306, sector_flow_unavailable: 306 } } }
  const quality = { fundFlowArchive: { status: 'unavailable', reason: 'TimeoutError' },
    fundCollection: { requested: 4792, returned: 533, usable: 521, unfinished: 4259 },
    stageMissingReasons: { fund_evidence: 'provider_failed_or_stage_deadline_exhausted' } }
  const before = JSON.stringify({ channels, quality })
  const shown = opportunityDisplay(channels, now, false, quality)
  assert.equal(shown.cards.length, 0)
  assert.equal(shown.fundingStatus.state, 'unavailable')
  assert.equal(shown.fundingStatus.researched, 306)
  assert.equal(shown.fundingStatus.stockWindows, 0)
  assert.equal(shown.fundingStatus.sectorWindows, 0)
  assert.match(shown.fundingStatus.message, /无法完成资金筛选/)
  assert.ok(shown.fundingStatus.reasons.some(reason => /归档超时/.test(reason)))
  assert.ok(shown.fundingStatus.reasons.some(reason => /4259.*未完成/.test(reason)))
  assert.match(shown.fundingStatus.shortWindow, /3分钟.*521.*不代表30分钟/)
  assert.equal(JSON.stringify({ channels, quality }), before)
})

test('verified fund windows with adverse flow remain a real zero-result assessment', () => {
  const channels = { version: 'opportunity-flow-20261103-v1', recommendations: [],
    diagnostics: { researchedCount: 10, failedConditionCounts: { '个股_net_flow_not_positive': 10, 'entry_risk_above_constraint': 3 } } }
  const shown = opportunityFundingStatus(channels, { fundCollection: { requested: 100, returned: 30, unfinished: 70 } })
  assert.equal(shown.state, 'verified')
  assert.equal(shown.stockWindows, 10)
  assert.equal(shown.sectorWindows, 10)
  assert.match(shown.message, /没有同时满足/)
  assert.doesNotMatch(shown.message, /无法完成/)
})

test('partial archive and fund evidence retain separate packet, candidate and short-window meanings', () => {
  const shown = opportunityFundingStatus({ version: 'opportunity-flow-20261103-v1',
    diagnostics: { researchedCount: 10, failedConditionCounts: { stock_flow_unavailable: 3, sector_flow_unavailable: 7 } } },
  { fundFlowArchive: { status: 'partial', reason: 'TimeoutError', packetCount: 20,
      verifiedPacketCount: 6, failedPacketCount: 1, remainingPacketCount: 13 },
    fundCollection: { legacy3mUsable: 15, candidate30mObserved: 7, aligned30mObserved: 12 } })
  assert.equal(shown.state, 'partial')
  assert.equal(shown.stockWindows, 7)
  assert.equal(shown.sectorWindows, 3)
  assert.ok(shown.reasons.some(reason => /6 \/ 20 个采集包.*13 个尚未处理/.test(reason)))
  assert.match(shown.shortWindow, /3分钟.*15/)
})

test('missing new diagnostics stay unknown and legacy history is not reinterpreted as a fund outage', () => {
  const unknown = opportunityFundingStatus({ version: 'opportunity-flow-20261103-v1', diagnostics: { researchedCount: 5 } })
  assert.equal(unknown.state, 'unknown')
  assert.equal(unknown.stockWindows, null)
  assert.equal(opportunityFundingStatus({ version: 'opportunity-em-20261029-v2', diagnostics: { researchedCount: 5 } }), null)
  const unverifiedMetrics = opportunityFundingStatus({ version: 'opportunity-flow-20261103-v1',
    diagnostics: { researchedCount: 5, failedConditionCounts: { '个股_flow_metrics_unverified': 5 } } })
  assert.equal(unverifiedMetrics.state, 'partial')
})
