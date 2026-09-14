export function toResearchCode(code) {
  const raw = String(code || '').trim()
  if (!raw) return ''
  if (/\.(SH|SZ|BJ|HK|US|CSI)$/i.test(raw) || /^100\./.test(raw)) return raw.toUpperCase()
  const lower = raw.toLowerCase()
  if (lower.startsWith('gb_')) return `${lower.slice(3).toUpperCase()}.US`
  if (lower.startsWith('sh')) return `${lower.slice(2)}.SH`
  if (lower.startsWith('sz')) return `${lower.slice(2)}.SZ`
  if (lower.startsWith('bj')) return `${lower.slice(2)}.BJ`
  if (lower.startsWith('hk')) return `${lower.slice(2).toUpperCase()}.HK`
  if (lower.startsWith('us')) return `${lower.slice(2).toUpperCase()}.US`
  if (/^\d{6}$/.test(raw)) {
    if (/^[56]/.test(raw)) return `${raw}.SH`
    if (/^[489]/.test(raw)) return `${raw}.BJ`
    return `${raw}.SZ`
  }
  if (/^\d{5}$/.test(raw)) return `${raw}.HK`
  if (/^[A-Z][A-Z0-9.-]*$/i.test(raw)) return `${raw.toUpperCase()}.US`
  return ''
}

export function marketFromCode(code) {
  const normalized = toResearchCode(code)
  if (normalized.endsWith('.HK')) return 'hk'
  if (normalized.endsWith('.US')) return 'us'
  return 'cn'
}

export function toInternalStockCode(code) {
  const normalized = toResearchCode(code)
  if (!normalized) return ''
  const [symbol, market] = normalized.split('.')
  if (market === 'US') return `gb_${symbol.toLowerCase()}`
  return `${market.toLowerCase()}${symbol.toLowerCase()}`
}

export function openStockResearch(router, code, name = '') {
  const normalized = toResearchCode(code)
  if (!normalized) return false
  void router.push({
    name: 'klineAnalysis',
    query: { code: normalized, ...(name ? { name: String(name) } : {}) },
  })
  return true
}

export function stockOption(item) {
  return {
    label: `${item?.name || item?.fullname || item?.ts_code || ''} · ${item?.ts_code || ''}`,
    value: item?.ts_code || '',
  }
}
