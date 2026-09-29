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
export function ledgerPrice(value) { return present(value) && Number(value) > 0 ? Number(value).toFixed(2) : '—' }
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
