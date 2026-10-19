const SHANGHAI = 'Asia/Shanghai'

function timestamp(value) {
  if (typeof value !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}

function sessionOf(value) {
  return new Date(value).toLocaleDateString('sv-SE', { timeZone: SHANGHAI })
}

export function premarketDisplayState(assessment, now = Date.now(), viewingHistory = false) {
  if (!assessment || typeof assessment !== 'object') return { available: false, current: false, historical: false }
  const decisionAt = timestamp(assessment.decisionAt || assessment.asOf)
  const session = assessment.sessionDate || assessment.targetSession || (decisionAt === null ? '' : sessionOf(decisionAt))
  const sessionExpiry = /^\d{4}-\d{2}-\d{2}$/.test(session) ? Date.parse(`${session}T09:35:00+08:00`) : null
  const declaredExpiry = timestamp(assessment.expiresAt)
  const expiresAt = sessionExpiry === null ? null : Math.min(sessionExpiry, declaredExpiry ?? sessionExpiry)
  const current = !viewingHistory && assessment.status !== 'outside_opening_window' && decisionAt !== null && decisionAt <= now
    && session === sessionOf(now) && expiresAt !== null && now < expiresAt
  const at = time => Date.parse(`${session}T${time}:00+08:00`)
  const phaseLabel = !current ? '历史预案' : now < at('09:15') ? '竞价前预案'
    : now < at('09:20') ? '集合竞价 · 可撤单阶段'
      : now < at('09:25') ? '集合竞价 · 不可撤单阶段'
        : now < at('09:30') ? '竞价结束 · 开盘前预案' : '开盘衔接预案'
  return { available: true, current, historical: !current, phaseLabel, session, expiresAt }
}

export function premarketText(value, fallback = '未记录') {
  if (Array.isArray(value)) return value.filter(item => typeof item === 'string' && item.trim()).join('；') || fallback
  return typeof value === 'string' && value.trim() ? value : fallback
}

export function premarketQuoteState(pick, display, now = Date.now()) {
  const sourceTime = pick?.quote?.provider_timestamp || pick?.quote?.source_time || pick?.sourceTime
  const sourceAt = timestamp(sourceTime)
  const current = display.current && pick?.quoteStatus === 'fresh' && pick?.referencePriceKind === 'current_quote'
    && sourceAt !== null && sourceAt <= now && now - sourceAt <= 30_000 && sessionOf(sourceAt) === display.session
  const savedStatus = { missing: '未取得当日报价', stale: '报价已过期', future: '报价源时间异常', invalid: '报价未通过核验' }[pick?.quoteStatus]
  const label = display.historical ? '历史快照 · 未做当前核验' : current ? '当前普通报价已核验'
    : pick?.quoteStatus === 'fresh' ? '保存报价已过期 · 待刷新核验' : savedStatus || '报价待核验'
  return { current, label, sourceTime, priceLabel: pick?.referencePriceKind === 'previous_close' ? '昨收基准价' : current ? '当时报价' : '保存报价' }
}

export function premarketPlanLabel(pick, display) {
  if (display.historical) return '历史条件预案'
  return {
    overnight_plan: '昨夜条件预案', awaiting_auction_confirmation: '竞价条件预案',
    opening_price_observed: '已观察开盘价 · 条件预案', awaiting_open_price: '开盘价格待核验',
    opening_conditional: '开盘条件预案', awaiting_open_trade: '开盘成交待核验',
    avoid_chasing: '已进入不追区间',
  }[pick?.planStatus] || '条件预案'
}
