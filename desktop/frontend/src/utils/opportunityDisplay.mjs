const rows = value => Array.isArray(value) ? value.filter(item => item && typeof item === 'object') : []
const number = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null
const FLOW_VERSION = 'opportunity-flow-20261103-v1'
const fraction = value => { const n = number(value); return n !== null && n >= 0 && n <= 1 ? n : null }
const componentLabels = {
  sectorNetFlowRatio: '板块净额/成交额', stockNetFlowRatio: '个股净额/成交额',
  positiveMinuteFraction: '净流入分钟占比', pathEfficiency30m: '30分钟路径效率', entryRiskAtr: '入场距离 / ATR',
}

function flowWindow(value) {
  const packet = value && typeof value === 'object' ? value : {}
  const observed = packet.status === 'observed' && number(packet.horizonMinutes) === 30
    && Boolean(packet.source) && Number.isFinite(Date.parse(packet.asOf || ''))
    && Number.isFinite(Date.parse(packet.windowStart || '')) && Date.parse(packet.asOf) > Date.parse(packet.windowStart)
  const metrics = observed ? packet.metrics || {} : {}
  const coverage = packet.coverage || {}
  return {
    observed, source: packet.source || null, provider: packet.provider || null,
    sourceTime: packet.asOf || null, windowStart: packet.windowStart || null,
    netFlowCny: number(metrics.netFlowCny), turnoverCny: number(metrics.turnoverCny),
    netFlowRatio: number(metrics.netFlowRatio), positiveMinuteFraction: fraction(metrics.positiveMinuteFraction),
    positiveMemberFraction: fraction(metrics.positiveMemberFraction),
    topPositiveFraction: fraction(metrics.top1PositiveContribution),
    coverageFraction: fraction(coverage.fraction), totalPeers: number(coverage.totalPeers), usablePeers: number(coverage.usablePeers),
    excludedSelf: coverage.excludedSelf === true, sectorName: packet.sector?.name || null,
    fading: typeof metrics.fading === 'boolean' ? metrics.fading : null,
    recentOutflow: typeof metrics.recentOutflow === 'boolean' ? metrics.recentOutflow : null,
  }
}

// Saved evidence can come from earlier versions as strings or labelled objects.
// Never render an object as a fabricated reason or flatten its numeric metrics.
export function opportunityText(value) {
  const values = Array.isArray(value) ? value : value == null ? [] : [value]
  return [...new Set(values.map(item => typeof item === 'string' ? item
    : item && typeof item === 'object' ? item.text || item.message || item.description || item.reason || item.label : '')
    .filter(item => typeof item === 'string' && item.trim()).map(item => item.trim()))]
}

export function opportunityRecommendations(channels) {
  return rows(channels?.recommendations)
}

export function opportunityCard(pick, now, historical = false) {
  const evidence = pick?.opportunity || {}
  const quote = pick?.quote || {}
  const sourceTime = evidence.sourceEvidence?.quoteSourceTime || quote.source_time || quote.provider_timestamp || pick?.sourceTime || null
  const source = evidence.sourceEvidence?.quoteSource || quote.source || pick?.source || null
  const timestamp = Date.parse(sourceTime || '')
  const age = Number.isFinite(timestamp) ? Number(now) - timestamp : null
  const quotePrice = number(quote.price)
  const price = number(pick?.referencePrice ?? quotePrice ?? pick?.price)
  const sourceMissing = !sourceTime || !source || price === null || price <= 0
  const rejected = quote.fresh === false || quote.is_fresh === false || quote.stale === true
    || ['stale', 'missing', 'invalid', 'conflict'].includes(quote.status)
  const legacy = ['E', 'M'].includes(evidence.channel) || String(evidence.version || '').startsWith('opportunity-em-')
  const isFlowStrategy = evidence.version === FLOW_VERSION && evidence.channel === 'F'
  const current = isFlowStrategy && !historical && !legacy && !sourceMissing && !rejected && age !== null && age >= 0 && age <= 30000
  const quoteLabel = historical ? '历史快照 · 未做当前核验'
    : legacy ? '旧版规则记录 · 未按当前资金策略核验'
    : !isFlowStrategy ? '策略版本未核验 · 仅保留记录'
    : sourceMissing || age === null ? '报价来源或时间待核验'
    : age < 0 ? '源时间异常 · 待核验'
    : current ? '源时间在30秒内 · 已保存参考价'
    : '历史参考价 · 行情已过期，需重新核验'
  return {
    pick, legacy, isFlowStrategy, channel: ['F', 'M', 'E'].includes(evidence.channel) ? evidence.channel : '',
    channelLabel: evidence.channel === 'F' ? evidence.selectionMode === 'flow_pullback' ? '资金趋势 · 回踩' : '资金与持续趋势'
      : evidence.channel === 'M' ? '历史主升确认' : evidence.channel === 'E' ? '历史早期启动' : '研究观察',
    grade: evidence.grade || '未分级', current, quoteLabel, sourceTime, source, price,
    supports: opportunityText(evidence.supports || evidence.support || evidence.reasons),
    missing: opportunityText(evidence.missing), counterEvidence: opportunityText(evidence.counterEvidence),
    invalidation: opportunityText(evidence.invalidation),
    firstDetectionAt: evidence.firstDetectionAt || null,
    firstDetectionPrice: number(evidence.firstDetectionPrice), triggerAt: evidence.triggerAt || null,
    rankScore: legacy ? number(evidence.rankScore) : null,
    rankingComposite: isFlowStrategy ? fraction(evidence.rankingComposite) : null,
    rankingComponents: isFlowStrategy ? Object.entries(evidence.rankingComponents || {}).map(([key, entry]) => ({
      key, label: componentLabels[key] || key, rawValue: number(entry?.rawValue), rankValue: number(entry?.rankValue), percentile: fraction(entry?.percentile),
      weight: fraction(entry?.weight), direction: entry?.direction || null, peerUnit: entry?.peerUnit || null, peerCount: number(entry?.peerCount),
    })) : [],
    funding: { stock: flowWindow(isFlowStrategy ? evidence.flowEvidence?.stock : null), sector: flowWindow(isFlowStrategy ? evidence.flowEvidence?.sector : null) },
    entryRiskAtr: isFlowStrategy ? number(evidence.metrics?.entryRiskAtr) : null,
    structuralLow30m: isFlowStrategy ? number(evidence.metrics?.structuralLow30m) : null,
    netReturn30mPct: isFlowStrategy ? number(evidence.metrics?.netReturn30mPct) : null,
    pathEfficiency30m: isFlowStrategy ? number(evidence.metrics?.pathEfficiency30m) : null,
    remainingSpacePct: number(evidence.remainingSpacePct),
    spaceStatus: evidence.spaceStatus || 'unknown', buyWindow: evidence.buyWindow || {},
  }
}

export function opportunityDisplay(channels, now, historical = false) {
  const savedRows = [...rows(channels?.recommendations), ...rows(channels?.observations)]
  const legacy = String(channels?.version || '').startsWith('opportunity-em-')
    || (savedRows.length > 0 && savedRows.every(row => ['E', 'M'].includes(row.opportunity?.channel)))
  return {
    available: Boolean(channels && typeof channels === 'object'),
    legacy,
    title: legacy ? '机会精选 · 历史 E / M 规则' : '机会精选 · 资金与持续趋势',
    introduction: legacy ? '保留当时 E / M 规则的名单与证据，仅供历史复核；未按当前资金与持续趋势策略重新赋予资格。'
      : '先看资金流向的板块和个股，再核验30分钟持续趋势与入场位置。资金是供应商大单分类估算；等权排序指标不是胜率。',
    cards: opportunityRecommendations(channels).map(pick => opportunityCard(pick, now, historical)),
    observations: rows(channels?.observations).map(pick => opportunityCard(pick, now, historical)),
    message: opportunityText(channels?.diagnostics?.message || channels?.message).join('；'),
  }
}
