const rows = value => Array.isArray(value) ? value.filter(item => item && typeof item === 'object') : []
const number = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null

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
  const current = !historical && !sourceMissing && !rejected && age !== null && age >= 0 && age <= 30000
  const quoteLabel = historical ? '历史快照 · 未做当前核验'
    : sourceMissing || age === null ? '报价来源或时间待核验'
    : age < 0 ? '源时间异常 · 待核验'
    : current ? '源时间在30秒内 · 已保存参考价'
    : '历史参考价 · 行情已过期，需重新核验'
  return {
    pick, channel: evidence.channel === 'M' ? 'M' : evidence.channel === 'E' ? 'E' : '',
    channelLabel: evidence.channel === 'M' ? '主升确认' : evidence.channel === 'E' ? '早期启动' : '研究观察',
    grade: evidence.grade || '未分级', current, quoteLabel, sourceTime, source, price,
    supports: opportunityText(evidence.supports || evidence.support || evidence.reasons),
    missing: opportunityText(evidence.missing), counterEvidence: opportunityText(evidence.counterEvidence),
    invalidation: opportunityText(evidence.invalidation),
    firstDetectionAt: evidence.firstDetectionAt || null,
    firstDetectionPrice: number(evidence.firstDetectionPrice), triggerAt: evidence.triggerAt || null,
    rankScore: number(evidence.rankScore), remainingSpacePct: number(evidence.remainingSpacePct),
    spaceStatus: evidence.spaceStatus || 'unknown', buyWindow: evidence.buyWindow || {},
  }
}

export function opportunityDisplay(channels, now, historical = false) {
  return {
    available: Boolean(channels && typeof channels === 'object'),
    cards: opportunityRecommendations(channels).map(pick => opportunityCard(pick, now, historical)),
    observations: rows(channels?.observations).map(pick => opportunityCard(pick, now, historical)),
    message: opportunityText(channels?.diagnostics?.message || channels?.message).join('；'),
  }
}
