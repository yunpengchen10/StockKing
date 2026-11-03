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

// Collection counts cover candidates plus their peers. Only this assessment's
// 30-minute failure counts describe the researched selection pool.
export function opportunityFundingStatus(channels, quality = {}) {
  if (channels?.version !== FLOW_VERSION) return null
  const count = value => Number.isInteger(number(value)) && number(value) >= 0 ? number(value) : null
  const diagnostics = channels.diagnostics || {}
  const researched = count(diagnostics.researchedCount)
  const failures = diagnostics.failedConditionCounts
  const known = researched !== null && failures && typeof failures === 'object' && !Array.isArray(failures)
  const missing = key => known ? count(failures[key] ?? 0) : null
  const stockMissing = missing('stock_flow_unavailable'), sectorMissing = missing('sector_flow_unavailable')
  const validCount = value => researched !== null && value !== null && value <= researched ? researched - value : null
  const stockWindows = validCount(stockMissing), sectorWindows = validCount(sectorMissing)
  const otherMissing = known && Object.entries(failures).some(([key, value]) => number(value) > 0
    && (key === 'flow_measurement_mismatch' || /flow_metrics_unverified|资金.*_unavailable/.test(key)))
  const unavailable = researched > 0 && (stockWindows === 0 || sectorWindows === 0)
  const incomplete = stockMissing > 0 || sectorMissing > 0 || otherMissing
  const state = unavailable ? 'unavailable' : researched === 0 ? 'not_researched'
    : stockWindows === null || sectorWindows === null ? 'unknown' : incomplete ? 'partial' : 'verified'
  const descriptions = {
    unavailable: ['资金证据不可用', '本轮无法完成资金筛选；当前0只不能解释为完整筛查后没有机会。'],
    not_researched: ['本轮尚无可评估股票', '本轮未完成候选研究，暂不能判断是否存在资金趋势机会。'],
    unknown: ['资金核验覆盖未记录', '该记录未提供完整的资金核验统计，不能据此判断资金筛选已完成。'],
    partial: ['部分资金证据不可用', '本轮资金筛选覆盖不完整；已核验部分也没有形成入选机会。'],
    verified: ['资金证据检查已完成', '本轮没有同时满足资金方向、持续趋势与入场位置条件的机会。'],
  }
  const reasons = []
  const archive = quality?.fundFlowArchive || {}
  if (['unavailable', 'failed', 'partial'].includes(archive.status)) {
    const archiveReason = { TimeoutError: '归档超时', scan_deadline_exhausted: '归档时间已耗尽',
      deadline_exhausted: '归档时间已耗尽', archive_deadline_exhausted: '归档时间已耗尽' }[archive.reason]
      || (typeof archive.reason === 'string' ? archive.reason : '')
    reasons.push(`${archive.status === 'partial' ? '资金归档仅部分完成' : '资金归档失败'}${archiveReason ? `：${archiveReason}` : ''}`)
  }
  const archived = count(archive.verifiedPacketCount), archiveTotal = count(archive.packetCount)
  if (archived !== null && archiveTotal !== null && archive.status !== 'not_required') {
    reasons.push(`资金归档核验 ${archived} / ${archiveTotal} 个采集包${count(archive.remainingPacketCount) > 0 ? `；${count(archive.remainingPacketCount)} 个尚未处理` : ''}${count(archive.failedPacketCount) > 0 ? `；${count(archive.failedPacketCount)} 个失败` : ''}（归档成功不等于30分钟窗口完整）`)
  }
  const collection = quality?.fundCollection || {}
  const requested = count(collection.requested), returned = count(collection.returned), unfinished = count(collection.unfinished)
  if (unfinished > 0) {
    const reason = quality?.stageMissingReasons?.fund_evidence
    const label = { scan_deadline_exhausted: '扫描时间已耗尽',
      provider_failed_or_stage_deadline_exhausted: '供应商请求失败或采集阶段超时',
      fund_deadline_exhausted: '资金采集超时', fund_provider_failed: '资金供应商请求失败' }[reason]
    reasons.push(`资金采集尚有 ${unfinished} 只未完成${label ? `：${label}` : ''}`)
  }
  return { state, researched, stockWindows, sectorWindows, title: descriptions[state][0],
    message: descriptions[state][1], reasons,
    collection: requested !== null && returned !== null ? `资金采集返回 ${returned} / ${requested} 只（含板块同行，不是本轮入选数量）` : '',
    shortWindow: count(collection.legacy3mUsable ?? collection.usable) !== null
      ? `3分钟基础资金指标可用 ${count(collection.legacy3mUsable ?? collection.usable)} 只，不代表30分钟完整证据` : '',
  }
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

export function opportunityDisplay(channels, now, historical = false, quality = {}) {
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
    fundingStatus: opportunityFundingStatus(channels, quality),
    message: opportunityText(channels?.diagnostics?.message || channels?.message).join('；'),
  }
}
