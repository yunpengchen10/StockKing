const present = value => value !== null && value !== undefined && value !== ''
export const LEDGER_STATUS = {
  pending: '待复盘', pending_review: '待复盘', not_due: '尚未到期', due: '已到期，待执行',
  review_not_run: '已到期，尚未执行', calendar_unavailable: '交易日历待核验',
  mature_price_pattern: '价格模式已观察', observation_only: '仅观察记录',
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
export function reviewStatusLabel(value, { nextDay = false } = {}) {
  if (value === 'pending') return nextDay ? '尚未到期' : '待处理（到期或执行未确认）'
  if (['pending_data', 'missing_data', 'incomplete'].includes(value)) return '证据待补'
  return statusLabel(value)
}
const REVIEW_REASONS = {
  point_in_time_price_limit_missing: '缺少信号当时的涨停价、前收盘价或适用涨跌幅规则，不能核验模拟成交。',
  signal_quote_missing_or_stale: '信号当时的行情时间缺失或不符合30秒时效要求，不能核验模拟成交。',
  signal_price_missing: '原信号参考价缺失或无效，无法计算价格变化。',
  signal_reference_source_time_missing: '原信号参考价的行情时间缺失，无法核验价格观察基准。',
  signal_reference_source_time_in_future: '参考行情时间晚于信号时间，不能作为当时的价格观察基准。',
  signal_reference_price_unverified: '冻结行情价格缺失或与信号参考价不一致，无法核验价格观察基准。',
  incomplete_minute_path: '信号后至目标收盘的分钟数据不完整，等待补齐价格路径。',
  entry_minute_bars_missing: '缺少信号当日的分钟数据，等待补齐。',
  target_close_minute_missing: '缺少目标交易日的收盘分钟或对应观察路径，等待补齐。',
  daily_close_marks_missing: '缺少观察区间内的每日收盘价格，等待补齐。',
  first_complete_minute_missing: '缺少信号后的首个完整分钟，无法核验模拟入场。',
  exit_minute_bars_missing: '缺少模拟退出所需的分钟数据，暂不能计算完整交易收益。',
  post_target_minute_path_missing: '目标退出时间之后的分钟路径有缺口，模拟持仓尚未结算。',
  suspended_or_unavailable_at_signal: '信号发出时停牌或不可交易，未模拟成交。',
  limit_up_queue_liquidity_or_slippage: '首个入场分钟未通过涨停排队、成交量或滑点等成交条件，未模拟成交。',
  suspended_limit_down_or_exit_slippage: '停牌、跌停、成交量或滑点等条件阻止模拟退出，持仓尚未结算。',
  source_price_limit_conflicts_with_board_rule: '信号当时的涨停价与板块规则冲突，不能核验模拟成交。',
  price_limit_or_corporate_action_unverifiable: '涨跌停制度或除权等公司行为无法核验，相关价格与成交结论暂不可确认。',
  possible_corporate_action_or_discontinuous_bars: '价格路径疑似存在除权等公司行为或不连续数据，暂不据此计算完整结果。',
  suspended_path_or_inactive_target_session: '观察路径存在停牌，或目标交易日全天无成交，价格观察结果不可用。',
  no_fill: '未满足模拟成交条件，没有已实现的模拟交易收益。',
}
export function reviewReasonLabel(value) {
  return present(value) ? Object.hasOwn(REVIEW_REASONS, value) ? REVIEW_REASONS[value] : String(value) : '未记录'
}
export function visibleReviews(rows, { includeControls = false } = {}) {
  if (!Array.isArray(rows)) return []
  return includeControls ? rows : rows.filter(row => row.selected === true)
}
export function reviewProgress(rows = []) {
  const result = { total: rows.length, concluded: 0, waiting: 0, notRun: 0, incomplete: 0, failed: 0, other: 0 }
  for (const row of rows) {
    const key = ['complete', 'completed', 'reviewed', 'mature', 'mature_price_pattern', 'no_fill'].includes(row.status) ? 'concluded'
      : ['pending', 'pending_review', 'not_due'].includes(row.status) ? 'waiting'
        : ['due', 'review_not_run'].includes(row.status) ? 'notRun'
          : ['missing_data', 'pending_data', 'incomplete', 'unverified', 'insufficient', 'calendar_unavailable'].includes(row.status) ? 'incomplete'
            : row.status === 'failed' ? 'failed' : 'other'
    result[key]++
  }
  return result
}
export function learningEffect(state = {}) {
  const source = state.effectiveRankSource
  if (source === 'rules') return { type: 'warning', label: '当前交易候选由人工规则排序',
    detail: state.shadowVersion ? '候选模型仍在影子验证，尚未用于当前推荐排序。' : '尚无已启用的学习模型；复盘记录不会自动成为已生效的模型改进。' }
  if (source === 'champion' || source === 'rollback') return { type: 'info',
    label: source === 'rollback' ? '交易候选使用回退模型排序' : '交易候选使用已启用模型排序',
    detail: `生效版本：${state.effectiveRankVersion || state.championVersion || '未记录'}。新复盘结果仍需通过训练与晋升检验后才会改变该版本。` }
  return { type: 'warning', label: '交易候选排序来源尚未确认', detail: '尚未取得实际生效模型状态，不能据复盘条数判断模型已学习或已改进。' }
}
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
export function nextDaySignalOf(signal = {}) {
  const candidate = signal.snapshot || signal.features?.candidate || signal.candidate || signal.features || {}
  return candidate.nextDaySignal || signal.nextDaySignal || null
}
export function isNextDaySelection(signal = {}) {
  return signal.nextDaySelected === true || signal.nextDayContinuationSelected === true || nextDaySignalOf(signal)?.selected === true
}
export function nextDayQueueLabel(signal = {}) {
  return signal.queue === 'first_board' ? '未触板潜伏' : signal.queue === 'continuation' ? '已触板延续' : '次日观察（旧版未分队列）'
}
export function nextDayTouchLabel(signal = {}) {
  const touched = signal.touchedLimitToday ?? signal.features?.touchedLimitToday
  return touched === true ? '已触板' : touched === false ? '未触板' : '触板状态未知'
}
export function savedRecommendationSummary(run = {}) {
  const execution = Array.isArray(run.candidates) ? run.candidates : []
  const primary = Array.isArray(run.nextDayWatchlist) ? run.nextDayWatchlist : []
  const continuation = Array.isArray(run.nextDayContinuationWatchlist) ? run.nextDayContinuationWatchlist : []
  const codeOf = pick => recordQuoteCode(pick?.symbol?.code || pick?.code) || String(pick?.symbol?.code || pick?.code || '')
  const count = picks => new Set(picks.map(codeOf).filter(Boolean)).size
  return { saved: count([...execution, ...primary]), execution: count(execution), primary: count(primary), continuation: count(continuation),
    splitQueues: Array.isArray(run.nextDayContinuationWatchlist) || primary.some(pick => pick.nextDaySignal?.queue === 'first_board') }
}
export function signalSelectionLabel(signal = {}) {
  const nextDay = nextDaySignalOf(signal)
  return signal.selected === true ? '交易条件候选' : isNextDaySelection(signal)
    ? nextDay?.queue ? `${nextDayQueueLabel(nextDay)}观察` : '次日观察' : '对照'
}
export function nextDayPatternValue(review = {}, key) {
  // Incomplete evidence is unknown, never a failed limit-up prediction.
  if (review.status !== 'mature_price_pattern' || typeof review[key] !== 'boolean') return '—'
  return review[key] ? '是' : '否'
}
export function groupSignals(payload, { includeControls = false } = {}) {
  const groups = new Map(), seen = new Set()
  for (const signal of flattenSignals(payload)) {
    if (!includeControls && signal.selected !== true && !isNextDaySelection(signal)) continue
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
