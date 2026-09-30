const present = value => value !== null && value !== undefined && value !== ''
export const LEDGER_STATUS = {
  pending: '待复盘', pending_review: '待复盘', not_due: '待复盘', due: '待补齐',
  missing_data: '待补齐', pending_data: '待补齐', incomplete: '待补齐',
  complete: '已复盘', completed: '已复盘', reviewed: '已复盘', mature: '已复盘',
  filled: '模拟已成交', unfilled: '未成交', not_filled: '未成交',
  no_fill: '未成交', unavailable_at_signal: '信号时不可成交',
  not_filled_within_five_minutes: '入场窗口未成交', simulated_fill: '模拟已成交',
  exit_blocked: '模拟退出受阻', simulated_entry_exit_blocked: '已模拟入场，退出受阻',
  unverified: '证据不足', insufficient: '证据不足', rules_cold_start: '人工规则 · 积累样本',
  collecting: '积累样本', accumulating: '积累样本', insufficient_samples: '积累样本',
  training: '训练中', validating: '验证中', rejected: '未通过晋升', failed: '执行失败',
  shadow: '前瞻影子验证', shadow_validation: '前瞻影子验证', active: '已启用',
  rules: '人工规则运行', fallback: '已回退', paused: '已暂停', unavailable: '暂不可用',
}
export function statusLabel(value) { return LEDGER_STATUS[value] || value || '未记录' }
export function ledgerPercent(value, digits = 2) {
  return present(value) && Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(digits)}%` : '—'
}
export function ledgerPrice(value) { return present(value) && Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value).toFixed(2) : '—' }
export function recordQuoteCode(value) {
  const raw = String(value || '').trim().toUpperCase()
  const match = /^(?:(SH|SZ))?([036]\d{5})(?:\.(SH|SZ))?$/.exec(raw)
  if (!match) return ''
  const [, prefix, code, suffix] = match
  const market = code[0] === '6' ? 'SH' : 'SZ'
  return (prefix && prefix !== market) || (suffix && suffix !== market) ? '' : code
}
export function recordQuoteCodes(codes = []) { return [...new Set(codes.map(recordQuoteCode).filter(Boolean))] }
function positivePrice(value) {
  return ['number', 'string'].includes(typeof value) && present(value) && Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : null
}
function evidenceTime(value) {
  if (typeof value !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return null
  const stamp = Date.parse(value)
  return Number.isFinite(stamp) ? stamp : null
}
export function signalPriceComparison(signal = {}, quote = null) {
  const baseline = positivePrice(signal.signalPrice)
  const code = recordQuoteCode(signal.code)
  const matchedQuote = quote && code && recordQuoteCode(quote.code) === code ? quote : null
  const latest = positivePrice(matchedQuote?.price)
  const result = { available: false, baseline, latest, delta: null, percent: null, direction: 'flat', sourceTime: matchedQuote?.sourceTime || '', source: matchedQuote?.source || '', reason: '' }
  if (baseline === null) return { ...result, reason: '精选参考价缺失，暂不比较' }
  if (latest === null) return { ...result, reason: '最新报价未取得，暂不比较' }
  const decisionAt = evidenceTime(signal.decisionAt), sourceAt = evidenceTime(matchedQuote.sourceTime)
  if (decisionAt === null) return { ...result, reason: '入选时间未记录，暂不比较' }
  if (sourceAt === null) return { ...result, reason: '行情时间未取得，暂不比较' }
  if (sourceAt < decisionAt) return { ...result, reason: '行情早于本次精选，暂不比较' }
  const difference = latest - baseline
  const delta = Math.abs(difference) < 1e-10 ? 0 : difference
  return { ...result, available: true, delta, percent: delta / baseline, direction: delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat' }
}
export function ledgerSigned(value, digits = 2) {
  if (!present(value) || !Number.isFinite(Number(value))) return '—'
  const rounded = Number(Number(value).toFixed(digits))
  return `${rounded > 0 ? '+' : ''}${rounded.toFixed(digits)}`
}
export function createRecordQuoteLoader({ fetchQuotes, onState = () => {} }) {
  let disposed = false, generation = 0
  let state = { loading: false, quotes: {}, error: '' }
  const emit = patch => { state = { ...state, ...patch }; if (!disposed) onState({ ...state }) }
  function cancel({ clear = false } = {}) {
    generation++
    if (!disposed) emit({ loading: false, error: '', ...(clear ? { quotes: {} } : {}) })
  }
  async function load(rawCodes) {
    if (disposed) return
    const codes = recordQuoteCodes(rawCodes), token = ++generation
    if (!codes.length) { emit({ loading: false, quotes: {}, error: '' }); return }
    emit({ loading: true, error: '' })
    try {
      const payload = await fetchQuotes(codes)
      if (disposed || token !== generation) return
      const quotes = {}
      for (const [key, quote] of Object.entries(payload?.quotes || {})) {
        const code = recordQuoteCode(quote?.code || key)
        if (code && codes.includes(code)) quotes[code] = { ...quote, code }
      }
      const errors = Array.isArray(payload?.errors) ? payload.errors.filter(Boolean).map(String) : []
      emit({ quotes, error: errors.length ? errors.join('；') : codes.some(code => !quotes[code]) ? '部分股票暂未取得报价，可稍后刷新。' : '' })
    } catch (failure) {
      if (!disposed && token === generation) emit({ error: `${failure?.message || String(failure)}。已取得报价仍按原行情时间显示。` })
    } finally {
      if (!disposed && token === generation) emit({ loading: false })
    }
  }
  function dispose() { disposed = true; generation++ }
  return { load, cancel, dispose }
}
export function shanghaiDate(value) {
  if (!value) return ''
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value
  const parsed = Date.parse(value)
  if (!Number.isFinite(parsed)) return ''
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(parsed)
  const part = name => parts.find(item => item.type === name)?.value || ''
  return `${part('year')}-${part('month')}-${part('day')}`
}
export function flattenSignals(payload = {}) {
  return (payload.items || []).flatMap(run => {
    const signals = Array.isArray(run.signals) ? run.signals : [run]
    return signals.map(signal => ({
      ...signal,
      runId: signal.runId || run.runId || run.run_id || '',
      slot: signal.slot || run.slot || run.scanSlot || '',
      modelVersion: signal.modelVersion || signal.algorithmVersion || run.modelVersion || run.algorithmVersion || '',
      decisionAt: signal.decisionAt || signal.timestamp || run.asOf || run.as_of || '',
      date: signal.signalDate || shanghaiDate(signal.decisionAt || run.asOf || run.as_of),
      code: signal.code || signal.symbol?.code || (typeof signal.symbol === 'string' ? signal.symbol : ''),
      official: signal.official ?? run.official ?? false,
    }))
  })
}
export function groupSignals(payload, { includeControls = false } = {}) {
  const groups = new Map(), seen = new Set()
  for (const signal of flattenSignals(payload)) {
    if (!includeControls && signal.selected !== true) continue
    const signalId = signal.signalId || signal.signal_id
    if (signalId && seen.has(signalId)) continue
    if (signalId) seen.add(signalId)
    const key = `${signal.date}|${signal.code}|${signal.modelVersion}`
    if (!groups.has(key)) groups.set(key, { key, date: signal.date, code: signal.code, name: signal.name || signal.code, modelVersion: signal.modelVersion, signals: [] })
    groups.get(key).signals.push(signal)
  }
  for (const group of groups.values()) group.signals.sort((a, b) => (Date.parse(a.decisionAt) || 0) - (Date.parse(b.decisionAt) || 0))
  return [...groups.values()].sort((a, b) => b.date.localeCompare(a.date) || a.code.localeCompare(b.code))
}
export function reviewReturn(row = {}) {
  const fillStatus = row.fillStatus || row.fill_status
  if (['unfilled', 'not_filled', 'pending', 'missing_data', 'unavailable_at_signal', 'not_filled_within_five_minutes', 'simulated_entry_exit_blocked', 'unverified'].includes(fillStatus)) return '—'
  return ledgerPercent(row.simulatedNetReturn ?? row.simulated_net_return)
}
export function learningProgress(value, minimum) {
  if (!present(value) || !present(minimum) || !Number.isFinite(Number(value)) || Number(minimum) <= 0) return 0
  return Math.max(0, Math.min(100, Number(value) / Number(minimum) * 100))
}
