<script setup>
import { computed, inject, onActivated, onBeforeMount, onBeforeUnmount, onDeactivated, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Follow, GetEngineStatus, GetKingPicks, GetKingPicksHistory, GetDisplayedKingPicks, StartKingPicksRefresh, GetKingPicksRefreshTask, GetStockKingBackgroundLearning, GetStockKingBackgroundStatus, SetStockKingBackgroundLearning, GetStockKingAutoRecommendations, SetStockKingAutoRecommendations } from '../../wailsjs/go/main/App'
import { useMessage } from 'naive-ui'
import { toResearchCode } from '../utils/symbol'
import PickIndicatorEvidence from './PickIndicatorEvidence.vue'
import PicksLedger from './PicksLedger.vue'
import { predictionSummary, portfolioSummary, nextDayPredictionSummary } from '../utils/learningStatus.mjs'
import { entryExecutionSummary } from '../utils/recommendationQuality.mjs'
import { explainPick, PICK_BRANCHES, isEvidenceRuleVersion } from '../utils/pickExplain.mjs'
import { createPicksRefreshController, readDisplayedPicks, saveDisplayedPicks, selectDisplayedPicks } from '../utils/picksRefresh.mjs'
import { withRecordReadTimeout } from '../utils/recordRead.mjs'
import { nextDayQueueLabel, nextDayTouchLabel, savedRecommendationSummary, primaryRecommendationPicks, primaryRecommendationState, primaryAdmissionReasons, marketBreadthSummary, firstRecommendationEvidence, isDailyPrimaryRun, dailyObservationPicks } from '../utils/pickLedger.mjs'
import { premarketDisplayState, premarketText, premarketQuoteState, premarketPlanLabel } from '../utils/premarket.mjs'

const router = useRouter()
const message = useMessage()
const darkTheme = inject('appDarkTheme', ref(true))
const loading = ref(false), refreshError = ref(''), viewingHistory = ref(false), methodVisible = ref(false), selectedPick = ref(null), explanationVisible = ref(false)
const restoring = ref(true), restoreError = ref('')
const refreshProgress = ref(0), refreshStage = ref(''), refreshElapsed = ref(0)
const isV11 = computed(() => isEvidenceRuleVersion(adaptive.value.scoreVersion || adaptive.value.modelVersion) || adaptivePicks.value.some(pick => isV11Pick(pick)))
const explanation = computed(() => explainPick(selectedPick.value || {}))
function showWhy(pick, insufficient = false) { selectedPick.value = pick; selectedIsInsufficient.value = insufficient; explanationVisible.value = true }
const engine = ref({ state: 'starting', ready: false })
const maxPerBoard = ref(5)
const result = ref({ tiers: {}, kechuang: [], nonKeChuang: [], generatedAt: '', diagnostics: {} })
const displayedResult = ref(null)
const classicResult = ref({ tiers: {}, kechuang: [], nonKeChuang: [], generatedAt: '', diagnostics: {} })
const activeTier = ref('regular')
const precisionProfile = ref('regular')
const activeBoard = ref('nonKeChuang')
const viewMode = ref('adaptive')
const pageActive = ref(true)
const ledgerModes = ['records', 'reviews', 'learning']
const visitedLedgerModes = ref([])
const recordsRefreshToken = ref(0)
watch(viewMode, mode => {
  if (ledgerModes.includes(mode) && !visitedLedgerModes.value.includes(mode)) visitedLedgerModes.value.push(mode)
})
const backgroundEnabled = ref(true)
const recommendationsEnabled = ref(true)
const backgroundStatus = ref({ status: 'never_run', updatedAt: '' })
const historyVisible = ref(false)
const historyLoading = ref(false)
const historyItems = ref([])
let disposed = false, savedSyncTimer = null, savedSyncInFlight = false, displayReadPromise = null, displayRevision = 0, startupPolls = 0
const pageResult = computed(() => viewMode.value === 'classic' ? classicResult.value : result.value)
let snapshotStorage = null
try { snapshotStorage = window.localStorage } catch (_) { /* Server persistence remains available. */ }
const refreshController = createPicksRefreshController({
  startTask: StartKingPicksRefresh,
  getTask: GetKingPicksRefreshTask,
  storage: snapshotStorage,
  onState(state) {
    loading.value = state.loading
    refreshProgress.value = state.progress
    refreshStage.value = state.message
    refreshElapsed.value = state.elapsedSeconds
    refreshError.value = state.error
  },
  onResult(fresh) {
    displayRevision++
    const saved = selectDisplayedPicks(displayedResult.value, fresh)
    displayedResult.value = saved
    result.value = saved
    restoring.value = false
    restoreError.value = ''
    viewingHistory.value = false
    saveDisplayedPicks(snapshotStorage, saved)
    if (fresh.recommendationSyncError) message.warning(`精选已生成，但推荐记录同步失败：${fresh.recommendationSyncError}`)
  },
})

const tierOptions = [
  { key: 'conservative', label: '保守', note: '低波稳健' },
  { key: 'regular', label: '均衡', note: '均衡多因子' },
  { key: 'aggressive', label: '进攻', note: '1–3 日潜力' },
]

const tierData = computed(() => {
  const data = classicResult.value?.tiers?.[activeTier.value]
  if (data) return data
  if (activeTier.value === 'regular') {
    return { kechuang: classicResult.value?.kechuang || [], nonKeChuang: classicResult.value?.nonKeChuang || classicResult.value?.non_kechuang || [] }
  }
  return { kechuang: [], nonKeChuang: [] }
})
const picks = computed(() => tierData.value?.[activeBoard.value] || [])
const shortfall = computed(() => Number(tierData.value?.shortfall?.[activeBoard.value] || 0))
const adaptive = computed(() => result.value?.adaptive || { candidates: [], changes: {}, calibration: {} })
const premarketNow = ref(Date.now())
let premarketTimer = null
const premarketAssessment = computed(() => adaptive.value.premarketAssessment)
const premarketView = computed(() => premarketDisplayState(premarketAssessment.value, premarketNow.value, viewingHistory.value))
const premarketPicks = computed(() => Array.isArray(adaptive.value.premarketCandidates) ? adaptive.value.premarketCandidates : [])
const premarketCards = computed(() => premarketPicks.value.map(pick => ({ pick,
  quoteState: premarketQuoteState(pick, premarketView.value, premarketNow.value),
  planLabel: premarketPlanLabel(pick, premarketView.value),
})))
const currentPremarketQuoteCount = computed(() => premarketCards.value.filter(card => card.quoteState.current).length)
const premarketExclusions = computed(() => {
  const labels = { daily_missing_stale_or_invalid: '日线基准缺失、过期或无效', previous_session_trend_not_confirmed: '昨夜趋势条件未满足',
    known_negative_earnings: '已知负面业绩风险', known_large_unlock: '已知大额解禁风险', unsupported_or_risk_board: '板块或名称风险筛除',
    suspended_locked_or_special_session: '停牌、封板或特殊交易状态', daily_quote_price_basis_mismatch: '昨收与报价口径不一致',
    daily_support_invalidated: '支撑结构失效', limit_up_without_buyable_trade_evidence: '封板且缺少可买成交证据' }
  return Object.entries(premarketAssessment.value?.excludedCounts || {}).filter(([, count]) => Number(count) > 0)
    .map(([key, count]) => `${labels[key] || '其他未通过条件'} ${count} 只`).join('；')
})
function updatePremarketClock() {
  if (premarketTimer !== null) clearTimeout(premarketTimer)
  premarketTimer = null
  premarketNow.value = Date.now()
  if (!disposed && pageActive.value && premarketView.value.current) premarketTimer = setTimeout(updatePremarketClock, 1000)
}
watch([premarketAssessment, viewingHistory], updatePremarketClock)
const snapshotIsOld = computed(() => {
  const raw = adaptive.value.generatedAt || result.value.generatedAt
  const stamp = raw ? new Date(raw) : null
  if (!stamp || Number.isNaN(stamp.getTime())) return false
  const options = { timeZone: 'Asia/Shanghai' }
  return stamp.toLocaleDateString('sv-SE', options) < new Date().toLocaleDateString('sv-SE', options)
})
const adaptivePicks = computed(() => primaryRecommendationPicks(adaptive.value, precisionProfile.value))
const isDailyPrimary = computed(() => isDailyPrimaryRun(adaptive.value))
const hasDailyObservation = computed(() => ['daily_observation', 'mixed_intraday_and_daily_observation'].includes(adaptive.value.primarySelectionMode)
  || dailyObservationPicks(adaptive.value, precisionProfile.value).length > 0)
const dailyPicks = computed(() => isDailyPrimary.value ? adaptivePicks.value : dailyObservationPicks(adaptive.value, precisionProfile.value))
const hasDailyEvidence = computed(() => isDailyPrimary.value || hasDailyObservation.value)
const dailyAssessment = computed(() => adaptive.value.dailyAssessment || {})
const dailyRankMeaning = computed(() => dailyAssessment.value.policyContract?.meaning
  || dailyPicks.value[0]?.rankMeaning || '排序口径以本轮保存的规则版本为准。')
const dailyAttackWatchlist = computed(() => Array.isArray(adaptive.value.dailyAttackWatchlist) ? adaptive.value.dailyAttackWatchlist : [])
const dailyAuditSummary = computed(() => {
  const rows = Array.isArray(adaptive.value.dailyRejectionLedger) ? adaptive.value.dailyRejectionLedger : []
  const labels = { excluded: '基础条件未通过', profile_rejected: '均衡档条件未通过', ranked_out: '均衡档合格但未入前列', qualified_below_top_n: '均衡档合格但未入前列', selected_regular: '已入选均衡档', observed_regular: '均衡档日线观察', observation_below_top_n: '均衡档观察合格但未入前列' }
  const counts = rows.reduce((result, row) => {
    const state = labels[row.state] ? row.state : 'unknown'
    result[state] = (result[state] || 0) + 1
    return result
  }, {})
  return Object.entries(counts).map(([key, count]) => `${labels[key] || '其他已记录状态'} ${count} 只`).join('；')
})
const dailyEmotionPanel = computed(() => adaptive.value.marketEmotionPanel || {})
const dailyEmotionMetrics = computed(() => {
  const features = dailyEmotionPanel.value.features || {}
  return [
    ['advancing_pct', '上涨占比', '%'], ['limit_up_pct', '涨停占比', '%'], ['limit_down_pct', '跌停占比', '%'],
    ['broken_board_pct', '触板后未封住占比', '%'], ['previous_limit_premium_pct', '昨日涨停群体当前平均溢价', '%'],
    ['previous_limit_promotion_pct', '昨日涨停群体晋级率', '%'], ['same_slot_history_sessions', '同刻历史', ' 个交易日'],
  ].map(([key, label, unit]) => {
    const diagnostic = dailyEmotionPanel.value.emotionDiagnostics?.metrics?.[key]
    const value = diagnostic ? diagnostic.value : features[key]
    return { key, label, value: value == null ? '未取得' : `${dailyValue(value)}${unit}`,
      history: diagnostic ? `历史 ${diagnostic.historySampleCount ?? 0}/${diagnostic.requiredHistorySessions ?? 60} 个同刻样本` : '',
      percentile: diagnostic ? diagnostic.empiricalPercentile == null ? '历史分位未取得' : `历史分位 ${dailyValue(diagnostic.empiricalPercentile)}%` : '' }
  })
})
const dailyDisplayMessage = computed(() => precisionProfile.value === 'regular' ? dailyAssessment.value.message
  : `日线条件推荐${adaptivePicks.value.length}只（${{ conservative: '稳健', aggressive: '激进' }[precisionProfile.value] || precisionProfile.value}档）；查看本档价格与风险检查结果。`)
const primaryContractKnown = computed(() => isDailyPrimary.value || hasDailyObservation.value || Boolean(adaptive.value.recommendationPolicy))
const primaryObjective = computed(() => isDailyPrimary.value
  ? '日线评估目标：基于已完成日K、当日报价及披露风险筛选，观察下一交易日（T+1）的价格结构与合法退出条件。'
  : hasDailyObservation.value
  ? '主推荐须通过分钟结构、市场环境、板块联动、资金与风险核验；日线观察独立保留，不计入主推荐。'
  : adaptive.value.primaryObjective === 'morning_signal_to_same_session_close'
  ? '上午评估目标：首次推荐参考价到当日收盘的价格变化，同时评估日内回撤。'
  : adaptive.value.primaryObjective === 'entry_today_exit_next_session'
    ? '评估目标：当日入场后，在下一交易日可合法退出时的表现。'
    : '该快照未记录新的评估目标；保留原版本含义，不能视为当日上涨预测。')
const primaryEmptyMessage = computed(() => isDailyPrimary.value
  ? dailyDisplayMessage.value || (dailyAssessment.value.status === 'outside_continuous_trading_window'
    ? '当前不在日线条件推荐的连续交易核验时段，保留已保存记录。'
    : '本轮没有同时满足日K、报价、已披露风险与本档价格条件的日线候选。')
  : adaptive.value.admissionDiagnostics?.summary || (result.value.generatedAt
  ? primaryContractKnown.value
    ? '主推荐 0 只：本轮没有同时通过数据、板块资金和风险准入的候选。缺数据或条件不满足时保持空榜。'
    : '该旧版快照没有交易候选；研究观察独立保留，不按当前准入重新判定历史。'
  : '尚未保存扫描，请点击“刷新扫描”。盘前当日分钟证据可能尚未形成。'))
const invalidatedPicks = computed(() => {
  const items = [...(adaptive.value.invalidatedCandidates || []), ...(adaptive.value.candidates || [])]
  const seen = new Set()
  return items.filter(pick => {
    const code = codeOf(pick)
    if (!['invalidated', 'withdrawn'].includes(pick.currentSignalState) || seen.has(code)) return false
    seen.add(code)
    return true
  })
})
const nextDayWatchlist = computed(() => Array.isArray(adaptive.value?.nextDayWatchlist) ? adaptive.value.nextDayWatchlist : [])
const nextDayContinuationWatchlist = computed(() => Array.isArray(adaptive.value?.nextDayContinuationWatchlist) ? adaptive.value.nextDayContinuationWatchlist : [])
const savedRecommendations = computed(() => savedRecommendationSummary(adaptive.value))
const nextDayQueues = computed(() => [
  ...(nextDayWatchlist.value.length || savedRecommendations.value.splitQueues ? [{ key: savedRecommendations.value.splitQueues ? 'first_board' : 'legacy', picks: nextDayWatchlist.value, title: savedRecommendations.value.splitQueues ? '未触板潜伏 · 研究观察' : '次日观察 · 历史未分队列' }] : []),
  ...(nextDayContinuationWatchlist.value.length ? [{ key: 'continuation', picks: nextDayContinuationWatchlist.value, title: '已触板延续 · 辅助观察' }] : []),
])
const precisionWatchlist = computed(() => (adaptive.value?.precisionWatchlist || []).filter(p => !p.precisionDecision?.profiles?.[precisionProfile.value]?.entryEligible))
const adaptiveCards = computed(() => isDailyPrimary.value ? [] : adaptivePicks.value.map((pick, index) => ({ pick: { ...pick, rank: index + 1 }, detail: explainPick(pick) })))
const dailyExcluded = computed(() => Object.entries(dailyAssessment.value.excludedCounts || {}).map(([key, count]) => ({ key, count, label: dailyReason(key) })))
const insufficientCards = computed(() => (Array.isArray(adaptive.value?.evidenceInsufficient) ? adaptive.value.evidenceInsufficient : []).map(pick => ({ pick, detail: explainPick(pick) })))
const selectedIsInsufficient = ref(false)
const quoteCoverage = computed(() => adaptive.value?.dataQuality?.quote_coverage)
const researchCoverage = computed(() => {
  const quality = adaptive.value?.dataQuality || {}
  return {
    universe: quality.research_universe_count ?? quality.mainboard_count ?? '未知',
    queued: quality.research_count ?? '未知',
    completed: quality.deep_research_count ?? quality.evidence_screen?.researched ?? quality.research_count ?? '未知',
    description: quality.research_coverage || '每轮先做全主板初筛，再对候选进行深度核验；覆盖范围以本轮记录为准。',
  }
})
const quoteCoverageSummary = computed(() => {
  const coverage = quoteCoverage.value
  if (!coverage) return ''
  const providers = Array.isArray(coverage.providers) ? coverage.providers.map(quoteSourceLabel).join(' / ') : ''
  return ['行情核验', providers, `新鲜 ${coverage.fresh ?? '—'}/${coverage.requested ?? '—'} 只`, Number(coverage.missing) > 0 ? `待补 ${coverage.missing} 只` : ''].filter(Boolean).join(' · ')
})
const scheduleSummary = computed(() => {
  const delivery = adaptive.value?.delivery || {}
  if (!delivery.target_at) return ''
  const late = Number(delivery.late_seconds)
  return [`目标 ${sourceClock(delivery.target_at)}`, `实际生成 ${delivery.decision_at ? sourceClock(delivery.decision_at) : '待记录'}`, Number.isFinite(late) && late > 0 ? `晚 ${durationLabel(late)}` : ''].filter(Boolean).join(' · ')
})
const modelMix = computed(() => {
  const counts = {}
  for (const pick of adaptivePicks.value) {
    const branch = pick?.modelBranch || pick?.model || '未知'
    counts[branch] = (counts[branch] || 0) + 1
  }
  return Object.entries(counts).map(([branch, count]) => `${branch}×${count}`).join(' · ') || '暂无候选'
})

function codeOf(pick) { return pick?.symbol?.code || pick?.code || pick?.stock_code || '' }
function nameOf(pick) { return pick?.symbol?.name || pick?.name || pick?.stock_name || codeOf(pick) }
function dailyValue(value, digits = 2) {
  return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '未取得'
}
function dailyReason(key) {
  return ({
    unsupported_or_risk_board: '板块或名称风险', daily_history_source_basis_or_latest_session_unverified: '日K来源、价格口径或最近交易日未核验',
    daily_history_prices_invalid: '日K价格或历史长度未通过', quote_or_provider_price_limits_unverified: '报价或涨跌停价未核验',
    daily_quote_previous_close_mismatch: '日K末收盘与报价前收盘不一致', not_tradable_or_limit_locked: '成交状态或涨跌停限制',
    financial_forecast_or_unlock_coverage_missing: '财报、预告或解禁覆盖缺失', financial_context_time_missing_future_or_stale: '财务事件时间未通过',
    financial_report_or_availability_unverified: '财报或披露时间未核验', known_loss_or_nonpositive_earnings: '已知亏损或盈利不足',
    forecast_disclosure_unverified: '业绩预告披露未核验', known_negative_earnings_forecast: '已知负面业绩预告',
    unlock_disclosure_unverified: '解禁披露未核验', unlock_date_unverified: '解禁日期未核验', unlock_ratio_unverified: '解禁比例未核验',
    verified_weak_market: '已核实市场偏弱', daily_uptrend_not_confirmed: '日线趋势条件未满足',
    daily_support_or_upside_space_unknown: '支撑或上方空间未取得', daily_ma5_extension_too_large: 'MA5乖离或ATR距离超出本档条件',
    daily_upside_reward_risk_insufficient: '压力空间与风险距离比不足', known_large_unlock: '已知较大解禁风险',
    known_nonpositive_operating_cash_flow: '经营现金流未达到本档条件',
    current_price_not_above_previous_close: '现价未高于昨收', current_price_not_above_open: '现价未高于开盘',
    current_price_not_above_session_vwap: '现价未高于当日成交均价', intraday_direction_inputs_unverified: '盘中方向数据未核验',
  })[key] || key
}
function dailyIndicators(pick) {
  const labels = { last_close: '上一交易日收盘', ma5: 'MA5', ma10: 'MA10', ma20: 'MA20', high20: '20日高点', high60: '60日高点', low20: '20日低点', atr14: 'ATR14' }
  return (pick.indicatorEvidence || []).map(row => ({ ...row, label: labels[row.key] || row.label || row.key,
    value: row.value == null ? '未取得' : `${dailyValue(row.value)}${row.unit || ''}`, role: '已完成日K证据',
    status: row.status === 'observed' ? '已记录' : '未取得', threshold: '结合当日报价与本档价格条件核验',
    source: row.source || pick.baselineSource || '未记录', asOf: row.asOf || pick.baselineAsOf || '未记录' }))
}
function quoteSourceLabel(source) {
  const label = String(source || '')
  return /tencent/i.test(label) ? '腾讯' : /sina/i.test(label) ? '新浪' : /eastmoney/i.test(label) ? '东方财富' : /tdx/i.test(label) ? '通达信' : label || '来源未知'
}
function sourceTimestamp(value) {
  if (typeof value !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}
function sourceClock(value) {
  const stamp = sourceTimestamp(value)
  return stamp === null ? '时间未知' : new Date(stamp).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}
function durationLabel(seconds) {
  const rounded = Math.ceil(seconds)
  if (rounded < 60) return `${rounded} 秒`
  if (rounded < 3600) return `${Math.floor(rounded / 60)} 分 ${rounded % 60} 秒`
  return `${Math.floor(rounded / 3600)} 小时 ${Math.floor(rounded % 3600 / 60)} 分`
}
function quoteEvidence(pick) {
  const quote = pick?.quote || {}
  const source = quote.provider_timestamp || quote.source_time
  const sourceAt = sourceTimestamp(source), fetchedAt = sourceTimestamp(quote.fetched_at)
  const delay = sourceAt === null || fetchedAt === null ? '采集延迟未知' : fetchedAt < sourceAt ? '源时间异常' : `采集延迟 ${durationLabel((fetchedAt - sourceAt) / 1000)}`
  return [quoteSourceLabel(quote.source), `源 ${sourceClock(source)}`, delay, quote.public_quote_metadata?.status === 'reference_only' ? '参考报价' : ''].filter(Boolean).join(' · ')
}
function scoreOf(pick) {
  const raw = pick?.finalScore ?? pick?.final_score ?? pick?.nls ?? pick?.tierScore ?? pick?.potentialScore ?? pick?.score
  if (raw === null || raw === undefined || raw === '') return '—'
  const value = Number(raw)
  return Number.isFinite(value) ? value.toFixed(1) : '—'
}
function nextDayScore(pick) {
  const score = pick?.nextDaySignal?.score
  return score != null && Number.isFinite(Number(score)) ? Number(score).toFixed(1) : '—'
}
function nextDayChange(pick) { return pick?.nextDaySignal?.features?.changePct ?? pick?.currentChange }
function percent(value, digits = 2) {
  if (value === null || value === undefined || value === '') return '—'
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(digits)}%` : '—'
}
function historicalRateVisible(pick) {
  return pick.probabilityStatus === 'research_rate_available' && pick.similarHistoryFeedback?.status === 'ready' && Number.isFinite(pick.similarHistoryFeedback?.touchWithin3dRate)
}
function modelLabel(pick) { return pick?.modelStatus === 'qualified' ? '正式模型' : '规则观察' }
function reasonsOf(pick) { return pick?.tierReasons || pick?.tier_reasons || [] }
function degradedOf(pick) {
  return [...(pick?.degradedReasons || pick?.degraded_reasons || []), ...(pick?.potentialDegradedReasons || pick?.potential_degraded_reasons || [])]
}
function permissionLabel(pick) {
  const buyability = pick?.buyability || {}
  if (!buyability.permissionRequired) return ''
  const board = String(buyability.permissionBoard || pick?.symbol?.board || '')
  if (board.startsWith('star')) return '需科创板权限'
  if (board.startsWith('chinext')) return '需创业板权限'
  if (board.startsWith('bse')) return '需北交所权限'
  return '需相应交易权限'
}

function internalCode(value) {
  const code = toResearchCode(value)
  if (code.endsWith('.US')) return `gb_${code.slice(0, -3).toLowerCase()}`
  const [symbol, market] = code.split('.')
  return `${market.toLowerCase()}${symbol.toLowerCase()}`
}

async function refresh() {
  if (loading.value || restoring.value) return
  if (viewMode.value !== 'classic') {
    displayRevision++
    await refreshController.start(maxPerBoard.value)
    return
  }
  loading.value = true; refreshError.value = ''
  refreshStage.value = '正在计算策略分组'; refreshProgress.value = 0; refreshElapsed.value = 0
  try {
    engine.value = await GetEngineStatus()
    if (!engine.value?.ready) throw new Error(engine.value?.message || 'Daily 研究引擎正在启动')
    const fresh = await GetKingPicks(maxPerBoard.value, true)
    if (!disposed) classicResult.value = fresh
  } catch (error) {
    if (!disposed) refreshError.value = error?.message || String(error)
  } finally { if (!disposed) loading.value = false }
}

function mergeAdaptive(snapshot) {
  if (!snapshot || typeof snapshot !== 'object') return
  result.value = {
    ...result.value,
    schemaVersion: 4,
    methodologyVersion: snapshot.modelVersion || snapshot.model_version || result.value?.methodologyVersion || 'king-adaptive-skill-v2.3.1',
    generatedAt: snapshot.generatedAt || snapshot.generated_at || result.value?.generatedAt || '',
    asOfDate: snapshot.asOfDate || snapshot.as_of_date || result.value?.asOfDate || '',
    adaptive: snapshot,
  }
}

function readSnapshot() {
  const parsed = readDisplayedPicks(snapshotStorage)
  // Cache is a fallback only; first verify the latest completed server scan.
  if (parsed) displayedResult.value = parsed
  return !!parsed
}

function canSyncSavedResults() {
  return !disposed && pageActive.value && document.visibilityState !== 'hidden'
}
function isV11Pick(pick) { return isEvidenceRuleVersion(pick?.scoreVersion) }
function calibrationValue(pick, key) {
  return pick?.probabilityStatus === 'withheld_until_calibrated' || pick?.[key] == null ? '待校准' : percent(pick[key])
}
function factorValue(value) { return value == null || !Number.isFinite(Number(value)) ? '未取得' : Number(value).toFixed(1) }

async function syncSavedResults(refreshDisplay = true) {
  if (disposed || savedSyncInFlight) return
  if (savedSyncTimer !== null) clearTimeout(savedSyncTimer)
  savedSyncTimer = null
  savedSyncInFlight = true
  try {
    if (!canSyncSavedResults()) return
    const status = await GetEngineStatus().catch(() => ({ state: 'unavailable', ready: false }))
    if (disposed) return
    engine.value = status
    if (!status?.ready) { startupPolls++; return }
    startupPolls = 0
    if (refreshDisplay) await restoreDisplayed()
    if (disposed) return
    const background = await GetStockKingBackgroundStatus().catch(() => null)
    if (background && !disposed) backgroundStatus.value = background
  } finally {
    savedSyncInFlight = false
    if (!disposed && pageActive.value) {
      const delay = document.visibilityState !== 'hidden' && !engine.value?.ready && startupPolls < 45 ? 2000 : 30000
      savedSyncTimer = setTimeout(syncSavedResults, delay)
    }
  }
}

async function restoreDisplayed() {
  if (disposed) return
  const revision = displayRevision
  try {
    if (!displayReadPromise) {
      const pending = withRecordReadTimeout(GetDisplayedKingPicks()).finally(() => {
        if (displayReadPromise === pending) displayReadPromise = null
      })
      displayReadPromise = pending
    }
    const payload = await displayReadPromise
    if (disposed || revision !== displayRevision) return
    restoreError.value = payload?.displaySyncError || ''
    const saved = selectDisplayedPicks(displayedResult.value, payload)
    if (saved) {
      displayedResult.value = saved
      if (!viewingHistory.value) result.value = saved
      saveDisplayedPicks(snapshotStorage, saved)
    }
    refreshController.resume(payload?.refreshTask)
  } catch (error) {
    if (disposed || revision !== displayRevision) return
    restoreError.value = error?.message || String(error)
    if (!viewingHistory.value && displayedResult.value) result.value = displayedResult.value
  } finally {
    if (!disposed && revision === displayRevision) restoring.value = false
  }
}
function returnToDisplayed() {
  viewingHistory.value = false
  result.value = displayedResult.value || { adaptive: { candidates: [] }, generatedAt: '' }
}

function historyResult(item) { return item?.result || {} }
function historyCandidates(item) { const run = historyResult(item); return isDailyPrimaryRun(run) ? primaryRecommendationPicks(run) : run?.candidates || [] }
function historyPremarketCandidates(item) { return historyResult(item)?.premarketCandidates || [] }
function historyNextDayCandidates(item) { return historyResult(item)?.nextDayWatchlist || [] }
function historyContinuationCandidates(item) { return historyResult(item)?.nextDayContinuationWatchlist || [] }
function historySavedSummary(item) { return savedRecommendationSummary(historyResult(item)) }
function hasNextDayHistory(item) { return Array.isArray(historyResult(item)?.nextDayWatchlist) }
function isScanHistory(item) { return ['king_live', 'king_0920', 'king_0922', 'king_0940', 'king_0955', 'king_1030', 'king_1455'].includes(item?.mode) }
function isReviewHistory(item) { return ['king_review', 'king_weekly'].includes(item?.mode) }
function isRestorableSnapshot(snapshot) {
  const status = String(snapshot?.status || '')
  return Array.isArray(snapshot?.candidates) && !['unavailable', 'failed', 'expired', 'cancelled', 'retired_slot', 'missed_slot', 'not_due', 'already_claimed'].includes(status) && !status.startsWith('skipped')
}
function historySlotLabel(item) {
  if (item?.mode === 'king_review') return '收盘复盘'
  if (item?.mode === 'king_weekly') return '学习检查'
  return historyResult(item).scanSlot || String(item?.mode || '').replace('king_', '')
}
function historyStatusLabel(item) {
  const status = historyResult(item).status || item?.status || ''
  if (historyResult(item).premarketAssessment && isRestorableSnapshot(historyResult(item))) return '条件预案已保存'
  return { unavailable: '扫描未完成', failed: '扫描失败', expired: '时段已过期', missed_slot: '已记录遗漏', not_due: '未到执行时点', already_claimed: '本轮已领取', cancelled: '已取消', retired_slot: '旧时点已停用', skipped_non_trading_day: '非交易日已跳过', completed_observations: '观察结果', no_candidates_with_coverage_limits: '未形成候选', audit_only: '已记录' }[status] || (String(status).startsWith('skipped') ? '已跳过' : status)
}
function historyEmptyLabel(item) {
  return historyResult(item).message || (!isRestorableSnapshot(historyResult(item)) ? historyStatusLabel(item) : '本轮无候选 · 请查看覆盖')
}
function reviewSummary(item) {
  const snapshot = historyResult(item)
  return snapshot.observationReview?.summary || snapshot.localTraining?.message || snapshot.message || (item?.mode === 'king_weekly' ? '查看本轮学习记录；训练状态不代表收益表现。' : '本轮保存历史与数据核验记录，未验证成交收益。')
}
function observationChange(value) {
  if (value === null || value === undefined || value === '') return '—'
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${parsed > 0 ? '+' : ''}${parsed.toFixed(2)}%` : '—'
}
function observationPrice(value) {
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? parsed.toFixed(2) : '—'
}
function formatTime(value) {
  if (!value) return '时间未知'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}

async function openHistory() {
  historyVisible.value = true
  historyLoading.value = true
  try {
    const payload = await GetKingPicksHistory(30)
    historyItems.value = (payload?.items || []).filter(item => isScanHistory(item) || isReviewHistory(item))
  } catch (error) {
    message.error(error?.message || String(error))
  } finally { historyLoading.value = false }
}

function restoreHistory(item) {
  if (!isScanHistory(item)) return
  const snapshot = historyResult(item)
  if (!snapshot || !Array.isArray(snapshot.candidates)) return
  mergeAdaptive(snapshot)
  viewingHistory.value = true
  viewMode.value = 'adaptive'
  historyVisible.value = false
  message.success(`已恢复 ${formatTime(snapshot.generatedAt || item?.as_of)} 的推荐快照`)
}

async function addWatch(pick) {
  const reply = await Follow(internalCode(codeOf(pick)))
  if (reply === '关注成功' || reply === '已经关注了') message.success('已加入自选')
  else message.error(reply)
}

function openKline(pick) {
  router.push({ name: 'klineAnalysis', query: { code: codeOf(pick), name: nameOf(pick) } })
}
function openRecommendationRecords() {
  // This is an explicit query action; ordinary tab/route returns remain frozen.
  recordsRefreshToken.value++
  viewMode.value = 'records'
}

async function toggleBackground(value) {
  try {
    await SetStockKingBackgroundLearning(value)
    backgroundEnabled.value = value
    backgroundStatus.value = await GetStockKingBackgroundStatus()
    message.success(value ? '自动学习已开启' : '自动学习已暂停')
  } catch (error) {
    backgroundEnabled.value = !value
    message.error(error?.message || String(error))
  }
}

async function toggleRecommendations(value) {
  try {
    await SetStockKingAutoRecommendations(value)
    recommendationsEnabled.value = value
    backgroundStatus.value = await GetStockKingBackgroundStatus()
    message.success(value ? '自动推荐已开启' : '自动推荐已暂停')
  } catch (error) { message.error(error?.message || String(error)) }
}

onBeforeMount(async () => {
  readSnapshot()
  const preferences = Promise.allSettled([
    GetStockKingBackgroundLearning().then(value => { if (!disposed) backgroundEnabled.value = value }),
    GetStockKingAutoRecommendations().then(value => { if (!disposed) recommendationsEnabled.value = value }),
  ])
  await restoreDisplayed()
  if (disposed || !pageActive.value) return
  refreshController.resume()
  void syncSavedResults(false)
  await preferences
})
onBeforeUnmount(() => {
  disposed = true
  refreshController.dispose()
  if (premarketTimer !== null) clearTimeout(premarketTimer)
  if (savedSyncTimer !== null) clearTimeout(savedSyncTimer)
})
onActivated(() => {
  pageActive.value = true
  updatePremarketClock()
  if (!restoring.value && savedSyncTimer === null) {
    restoring.value = true
    void restoreDisplayed().then(() => {
      if (!disposed && pageActive.value) void syncSavedResults(false)
    })
  }
})
onDeactivated(() => {
  pageActive.value = false
  if (premarketTimer !== null) clearTimeout(premarketTimer)
  premarketTimer = null
  // A reply captured before leaving cannot verify the latest scan on return.
  displayRevision++
  displayReadPromise = null
  restoring.value = false
  methodVisible.value = false
  historyVisible.value = false
  explanationVisible.value = false
  if (savedSyncTimer !== null) clearTimeout(savedSyncTimer)
  savedSyncTimer = null
})
</script>

<template>
  <main class="picks-page" :class="{ dark: darkTheme }">
    <header>
      <h2>精选</h2>
      <n-button secondary @click="methodVisible=true">如何使用</n-button>
      <n-input-number v-if="viewMode==='classic'" v-model:value="maxPerBoard" :min="5" :max="50" style="width:112px"><template #suffix>只 / 组</template></n-input-number>
      <div class="background-switch"><n-switch :value="recommendationsEnabled" @update:value="toggleRecommendations" /><span title="北京时间交易日定时扫描；手动刷新不重复计入训练">自动推荐</span></div>
      <div class="background-switch"><n-switch :value="backgroundEnabled" @update:value="toggleBackground" /><span title="控制每周本地训练与模型晋升检查">自动学习</span></div>
      <n-button secondary @click="openHistory">扫描历史</n-button>
      <n-button type="primary" :loading="loading" :disabled="restoring" title="重新获取行情并在本地计算，成功保存后显示" @click="refresh">刷新扫描</n-button>
    </header>
    <n-alert v-if="refreshError" type="warning" :show-icon="false" class="sync-alert">更新未完成：{{ refreshError }}<br/>已保存的推荐仍保留，可点击“刷新扫描”重试。</n-alert>
    <n-alert v-if="viewingHistory || (!engine.ready && !loading)" type="warning" :show-icon="false" class="sync-alert"><span>{{ viewingHistory ? '正在浏览历史快照' : (engine.message || '引擎正在启动，已保存推荐可以继续查看。') }}</span><n-button v-if="viewingHistory" size="tiny" text @click="returnToDisplayed">返回当前</n-button></n-alert>
    <div v-if="loading" class="refresh-progress" role="status" aria-live="polite"><div><b>{{ refreshStage || '正在扫描' }}</b><span>已用 {{ refreshElapsed }} 秒 · {{ refreshProgress }}%</span></div><n-progress type="line" :percentage="refreshProgress" :show-indicator="false" /><small>完成并保存后更新列表；切换页面后可继续查看进度。</small></div>
    <n-alert v-if="restoreError" type="warning" :show-icon="false" class="sync-alert">最新推荐读取失败：{{ restoreError }}<br/>{{ displayedResult ? '当前显示上次保存的推荐，尚未确认是否最新。' : '暂未取得推荐。' }}<n-button size="tiny" text @click="restoring=true; restoreDisplayed()">重试读取</n-button></n-alert>
    <div v-if="!restoring || viewingHistory" class="picks-meta"><span>{{ pageResult.asOfDate || pageResult.as_of_date || pageResult.generatedAt?.slice(0, 10) || '待生成' }}</span><details class="sk-help-details"><summary>扫描信息</summary><p>生成 {{ pageResult.generatedAt || pageResult.generated_at || '—' }} · 版本 {{ viewMode === 'adaptive' && isDailyPrimary ? dailyAssessment.version || '日线条件规则' : pageResult.methodologyVersion || '兼容旧榜' }}</p><p>后台学习 {{ backgroundEnabled ? '开启' : '暂停' }} · {{ backgroundStatus.status || '未运行' }} {{ backgroundStatus.updatedAt }}</p><p>每轮扫描保留当时证据，可在历史中回看。</p></details></div>
    <n-tabs v-model:value="viewMode" type="line" animated class="view-tabs"><n-tab-pane name="adaptive" tab="本地精选" /><n-tab-pane name="records" tab="推荐记录" /><n-tab-pane name="reviews" tab="延后复盘" /><n-tab-pane name="learning" tab="学习状态" /><n-tab-pane name="classic" tab="策略分组" /></n-tabs>

    <section v-if="viewMode === 'adaptive' && restoring && !viewingHistory" class="picks-loading" role="status" aria-live="polite"><n-spin size="small" /><span>正在读取最新已完成的推荐…</span></section>
    <section v-if="!restoring || viewingHistory" v-show="viewMode === 'adaptive'">
      <div v-if="result.adaptive" class="adaptive-summary scan-list-summary" aria-label="本轮名单概览">
        <div><b>本轮名单</b><span v-if="premarketView.current">集合竞价 / 开盘预案 {{ premarketPicks.length }} 只</span><span v-else>{{ isDailyPrimary ? '日线条件推荐' : primaryContractKnown ? '主推荐' : '旧版候选' }} {{ adaptivePicks.length }} 只</span><span v-if="hasDailyObservation">日线观察 {{ dailyPicks.length }} 只</span><span>研究观察 {{ nextDayWatchlist.length + nextDayContinuationWatchlist.length }} 只（潜伏 {{ nextDayWatchlist.length }} / 延续 {{ nextDayContinuationWatchlist.length }}）</span></div>
        <small>本轮生成 {{ formatTime(adaptive.generatedAt || result.generatedAt) }}</small>
      </div>
      <section v-if="premarketView.available" class="premarket-plans" :class="{ 'premarket-historical': premarketView.historical }">
        <div class="adaptive-summary premarket-summary"><div><b>{{ premarketView.current ? '集合竞价 / 开盘预案' : '历史集合竞价 / 开盘预案' }} · {{ premarketPicks.length }} 只</b><n-tag size="small" :type="premarketView.current ? 'info' : 'warning'" :bordered="false">{{ premarketView.phaseLabel }}</n-tag><small>昨夜基准 {{ premarketAssessment.baselineSession || premarketAssessment.baselineAsOf || '未记录' }} · 目标交易日 {{ premarketView.session || '未记录' }}</small></div></div>
        <p v-if="premarketView.historical" class="premarket-notice" role="status">此预案已结束或正在历史回看，不是当前交易指示。09:35 起以新一轮盘中核验结果为准；以下价格和条件只保留当时记录。</p>
        <p v-else class="premarket-notice">按照最近已完成交易日的趋势、压力与支撑准备条件，结合当时报价核验开盘位置。条件预案未通过盘中准入，不代表已成交。</p>
        <div class="quote-coverage"><span>生成 {{ formatTime(premarketAssessment.decisionAt || premarketAssessment.asOf) }}</span><span>生成阶段 {{ premarketAssessment.phaseLabel || premarketAssessment.marketPhase?.label || '未记录' }}</span><span>预案有效至 {{ formatTime(premarketAssessment.expiresAt || premarketAssessment.marketPhase?.validUntil) }}</span><span>日线基准可用 {{ premarketAssessment.dailyUsableCount ?? '未知' }} / 检查 {{ premarketAssessment.researchedCount ?? '未知' }} 只</span><span v-if="premarketView.current">当前报价核验 {{ currentPremarketQuoteCount }}/{{ premarketPicks.length }} 只 · 30 秒内</span></div>
        <p class="premarket-data-gap">{{ premarketText(premarketAssessment.auctionDataGaps, '普通报价未包含可核验的竞价虚拟匹配价、匹配量和未匹配委托，竞价强度未确认。') }}</p>
        <details v-if="premarketExclusions" class="sk-help-details picks-method"><summary>本轮预案筛查</summary><p>{{ premarketExclusions }}</p></details>
        <div v-if="premarketCards.length" class="pick-grid">
          <n-card v-for="({ pick, quoteState, planLabel }, index) in premarketCards" :key="`premarket-${codeOf(pick)}`" size="small" :bordered="false" class="pick-card premarket-card">
            <template #header><div class="pick-title"><b>{{ index + 1 }}</b><strong>{{ nameOf(pick) }}</strong><code>{{ codeOf(pick) }}</code><n-tag size="small" :bordered="false" :type="premarketView.current && pick.planStatus !== 'avoid_chasing' ? 'info' : 'warning'">{{ planLabel }}</n-tag></div></template>
            <div class="pick-body">
              <p class="premarket-reason">{{ pick.selectionReasons?.[0] || '查看上一交易日结构与开盘条件' }}</p>
              <div class="pick-quote"><strong>¥{{ observationPrice(pick.price ?? pick.referencePrice) }}</strong><span>{{ quoteState.priceLabel }}</span></div>
              <div class="quote-evidence"><b>{{ quoteState.label }}</b><br/>{{ pick.referencePriceKind === 'previous_close' ? `日线来源 ${pick.baselineSource || '未记录'} · ${pick.baselineAsOf || '未记录'}` : `报价来源 ${quoteSourceLabel(pick.quote?.source || pick.source)} · 源时间 ${formatTime(quoteState.sourceTime)}` }}</div>
              <div class="premarket-levels"><div><span>昨收</span><b>¥{{ observationPrice(pick.dailyLastClose) }}</b></div><div><span>支撑</span><b>¥{{ observationPrice(pick.support) }}</b></div><div><span>历史压力</span><b>{{ pick.resistance == null ? '未取得' : `¥${observationPrice(pick.resistance)}` }}</b></div><div><span>延续观察位</span><b>¥{{ observationPrice(pick.triggerPrice) }}</b></div></div>
              <p><span>条件触发</span>{{ premarketText(pick.triggerCondition || pick.entryPlan?.trigger) }}</p>
              <p><span>不追条件</span>{{ premarketText(pick.noChaseCondition || pick.noChase || pick.entryPlan?.noChase, '高开远离支撑需重新评估；一字封板且无可买成交时不追。') }}</p>
              <p><span>失效条件</span>{{ premarketText(pick.invalidCondition || pick.invalidations || pick.entryPlan?.invalidations) }}</p>
              <p v-if="pick.openingTradeObserved"><span>开盘核验</span>当时已观察开盘价 ¥{{ observationPrice(pick.openingPrice) }} · 高低开 {{ observationChange(pick.openingGapPct) }}；不等于竞价匹配确认。</p>
              <details class="sk-help-details pick-details"><summary>昨夜依据与数据缺口</summary>
                <p v-for="reason in pick.selectionReasons || []" :key="reason"><span>入选依据</span>{{ reason }}</p>
                <p><span>ATR14</span>¥{{ observationPrice(pick.atr14) }} · 历史波动尺度</p>
                <p><span>基准来源</span>{{ pick.baselineSource || '未记录' }} · {{ pick.baselineAsOf || '未记录' }}</p>
                <p v-for="gap in pick.dataGaps || []" :key="gap"><span>待核验</span>{{ gap }}</p>
                <p v-for="risk in pick.risks || []" :key="risk"><span>风险</span>{{ risk }}</p>
                <p><span>预案含义</span>上一交易日结构与开盘条件尚需验证；排列顺序不是胜率，不提供已验证收益预测。</p>
              </details>
            </div>
            <template #footer><n-space justify="end"><n-button size="small" secondary @click="addWatch(pick)">自选</n-button><n-button size="small" type="primary" @click="openKline(pick)">图表</n-button></n-space></template>
          </n-card>
        </div>
        <div v-else class="primary-empty" role="status"><p>{{ premarketAssessment.message || '本轮没有满足昨夜趋势、日线时效与风险条件的预案候选。' }}</p><p>本时段不要求当日分钟信号；候选为空时请查看日线覆盖与风险检查结果。</p></div>
        <p class="premarket-footnote">预案截止 09:35（北京时间）。A股 T+1 持有风险仍适用，支撑和压力不保证成交或收益。</p>
      </section>
      <section v-if="nextDayQueues.length" class="research-observations" aria-label="研究观察">
      <h3>研究观察 · 不计入主推荐（潜伏 {{ nextDayWatchlist.length }} 只 / 延续 {{ nextDayContinuationWatchlist.length }} 只）</h3>
      <p class="quote-coverage">后台本轮保存的潜伏与延续观察直接显示在这里，并随最新扫描一同更新。以下名单仅供研究，独立保留目标日与数据缺口，不计入主推荐数量。</p>
      <section v-for="queue in nextDayQueues" :key="queue.key" class="next-day-watch" :class="queue.key === 'continuation' ? 'next-day-continuation' : 'next-day-primary'">
        <n-alert v-if="adaptive.nextDayResearch?.retrospectiveProjection" type="warning" :show-icon="false">历史证据重排 · 非当时推荐。{{ adaptive.nextDayResearch.projectionMeaning || '按当时已保存的证据重新排序，不作为当时已经发出的推荐或新规则命中率。' }}</n-alert>
        <div class="adaptive-summary"><div><b>{{ queue.title }} · {{ queue.picks.length }} 只</b><small>目标交易日 {{ queue.picks[0]?.nextDaySignal?.targetSession || '下一交易日' }}</small></div><n-tag size="small" type="warning" :bordered="false">研究观察</n-tag></div>
        <p class="quote-coverage">{{ queue.key === 'first_board' ? '扫描时未触板的潜伏线索；具体涨幅、距模式价与昨日形态依据见每只记录。' : queue.key === 'continuation' ? '这些股票扫描时已触及当日模式限价，单独观察延续，不与未触板潜伏混排。' : '旧版名单保留原样，未事后划分潜伏或延续队列。' }}排序分不是涨停概率，交易条件仍待确认。</p>
        <div class="pick-grid">
          <n-card v-for="(pick, index) in queue.picks" :key="`${queue.key}-${codeOf(pick)}`" size="small" :bordered="false" class="pick-card" :class="queue.key === 'continuation' ? 'next-day-continuation-card' : 'next-day-card'">
            <template #header><div class="pick-title"><b>{{ index + 1 }}</b><strong>{{ nameOf(pick) }}</strong><code>{{ codeOf(pick) }}</code><n-tag size="small" :bordered="false" type="warning">观察分 {{ nextDayScore(pick) }}</n-tag></div></template>
            <div class="pick-body">
              <p class="next-day-reason">{{ pick.nextDaySignal?.reasons?.[0] || '查看本轮保存的量价结构线索' }}</p>
              <div class="pick-quote"><strong>{{ pick.referencePrice == null ? '—' : `¥${Number(pick.referencePrice).toFixed(2)}` }}</strong><span>扫描参考价</span><span :class="Number(nextDayChange(pick)) > 0 ? 'up' : Number(nextDayChange(pick)) < 0 ? 'down' : ''">{{ observationChange(nextDayChange(pick)) }}</span></div>
              <div v-if="pick.quote" class="quote-evidence">{{ quoteEvidence(pick) }}</div>
              <p v-if="pick.nextDaySignal?.branches?.length" class="quote-coverage">结构：{{ pick.nextDaySignal.branches.join(' · ') }}</p>
              <p v-if="pick.nextDaySignal?.queue" class="quote-coverage">{{ nextDayQueueLabel(pick.nextDaySignal) }} · 扫描时{{ nextDayTouchLabel(pick.nextDaySignal) }}<template v-if="pick.nextDaySignal.features?.limitDistancePct != null"> · 距当日模式限价 {{ Number(pick.nextDaySignal.features.limitDistancePct).toFixed(2) }}%</template></p>
              <details class="sk-help-details pick-details"><summary>观察依据与待确认条件</summary>
                <p v-for="reason in pick.nextDaySignal?.reasons || []" :key="reason"><span>线索</span>{{ reason }}</p>
                <p v-for="gap in pick.nextDaySignal?.gaps || []" :key="gap"><span>待确认</span>{{ gap }}</p>
                <p v-for="risk in pick.riskReasons || []" :key="risk"><span>风险</span>{{ risk }}</p>
                <p><span>评分含义</span>{{ pick.nextDaySignal?.scoreMeaning || '用于研究排序，未经胜率校准' }}</p>
                <p v-if="nextDayPredictionSummary(pick.nextDaySignal)"><span>次日事件模型</span>{{ nextDayPredictionSummary(pick.nextDaySignal) }}</p>
                <p><span>证据日期</span>{{ pick.nextDaySignal?.signalSession || '未记录' }} · 目标 {{ pick.nextDaySignal?.targetSession || '下一交易日' }}</p>
                <p><span>交易条件</span>此通道仅用于观察，未给出买入许可；开盘、量价和风险条件仍需重新确认。</p>
              </details>
            </div>
            <template #footer><n-space justify="end"><n-button size="small" secondary @click="addWatch(pick)">自选</n-button><n-button size="small" type="primary" @click="openKline(pick)">图表</n-button></n-space></template>
          </n-card>
        </div>
        <n-empty v-if="queue.key === 'first_board' && !queue.picks.length" description="本轮暂无满足潜伏观察条件的股票。观察队列不补入主推荐。" style="padding:24px 0" />
      </section>
      </section>
      <template v-if="!premarketView.current">
      <n-tabs v-if="(adaptive.profileCandidates || adaptive.dailyProfileCandidates) && (hasDailyEvidence || !isV11)" v-model:value="precisionProfile" type="segment">
        <n-tab-pane name="conservative" tab="稳健 · 财务与回撤" />
        <n-tab-pane name="regular" tab="均衡 · 趋势与买点" />
        <n-tab-pane name="aggressive" tab="激进 · 突破与延续" />
      </n-tabs>
      <p v-if="adaptive.profileCandidates && !isV11 && !isDailyPrimary" class="quote-coverage">三档入场纪律正在验证；盘前只观察，盘中重新确认。</p>
      <details v-if="precisionWatchlist.length && !isDailyPrimary" class="sk-help-details"><summary>等待条件的股票（{{ precisionWatchlist.length }}）</summary>
        <p v-for="pick in precisionWatchlist" :key="codeOf(pick)">{{ nameOf(pick) }}：{{ primaryAdmissionReasons(pick.precisionDecision?.profiles?.[precisionProfile]?.reasons).join('；') }}</p>
      </details>
      <p class="quote-coverage">软件行情 · 本地计算 · 沪深主板非ST。页面可见时每30秒同步后台已完成的扫描，主推荐与研究观察一同更新；点击“刷新扫描”重新选股，成功后以新结果覆盖当前列表。</p>
      <n-alert v-if="isDailyPrimary ? dailyDisplayMessage : adaptive.message" type="warning" :show-icon="false">{{ isDailyPrimary ? dailyDisplayMessage : adaptive.message }}</n-alert>
      <details v-if="adaptive.admissionDiagnostics" class="admission-diagnostics" :open="!isDailyPrimary" :aria-label="isDailyPrimary ? '分钟增强准备情况' : '本轮准入检查'">
        <summary>{{ isDailyPrimary ? '分钟增强准备情况' : '本轮准入检查' }}</summary>
        <p v-if="isDailyPrimary">以下记录分钟增强与上午模型的准备状态。日线条件推荐按已完成日K、报价和披露风险独立筛选。</p>
        <div class="admission-check-grid">
          <div v-for="check in adaptive.admissionDiagnostics.checks || []" :key="check.key" class="admission-check" :class="{ 'check-blocked': check.blocked > 0 }">
            <span>{{ check.label }}</span><b>{{ check.passed }} / {{ check.total }} 只通过</b>
            <small v-if="check.pending > 0" class="check-pending">{{ check.pending }} 只待核验：{{ check.pendingReason || (check.key === 'history' ? '等待当前完整分钟窗口形成，不计为历史缺失' : '等待所需证据形成') }}</small>
            <small v-if="check.blocked">{{ check.action }}</small>
          </div>
        </div>
        <p v-if="!isDailyPrimary">数据、方向及风险同时通过 {{ adaptive.admissionDiagnostics.evidenceAndRiskPassed }} 只 · 模型准入通过 {{ adaptive.admissionDiagnostics.modelAdmissionPassed }} 只 · 最终主推荐 {{ adaptive.admissionDiagnostics.selected }} 只</p>
        <p v-else>分钟增强数据与风险通过 {{ adaptive.admissionDiagnostics.evidenceAndRiskPassed }} 只 · 分钟模型准入通过 {{ adaptive.admissionDiagnostics.modelAdmissionPassed }} 只</p>
        <small>{{ adaptive.admissionDiagnostics.countMeaning }}</small>
        <p v-if="adaptive.historyPreparation?.message">历史准备：{{ adaptive.historyPreparation.message }}</p>
        <p v-if="adaptive.historyPreparation?.baselineClock">历史同刻核验时点：{{ formatTime(adaptive.historyPreparation.baselineClock) }}</p>
        <details v-if="adaptive.morningReadiness" class="sk-help-details"><summary>上午模型准备进度</summary>
          <p>{{ adaptive.morningReadiness.message || '各时段独立积累有效样本并验证。' }}</p>
          <div v-for="queue in adaptive.morningReadiness.queues || []" :key="queue.queue"><b>{{ queue.label || queue.queue }}</b><p>{{ queue.message }}</p><p v-if="queue.action">{{ queue.action }}</p></div>
        </details>
      </details>
      <div class="adaptive-summary saved-recommendations"><div><b>{{ isDailyPrimary ? `日线条件推荐 · ${adaptivePicks.length} 只` : primaryContractKnown ? `主推荐 · ${adaptivePicks.length} 只通过准入` : `旧版候选 · ${adaptivePicks.length} 只` }}</b><n-tag v-if="snapshotIsOld" size="small" type="warning" :bordered="false">历史结果</n-tag><small>本轮生成 {{ formatTime(adaptive.generatedAt || result.generatedAt) }}</small><small v-if="adaptive.run_id || adaptive.runId">扫描编号 {{ adaptive.run_id || adaptive.runId }}</small></div><n-button v-if="result.adaptive" size="small" secondary @click="openRecommendationRecords">查看推荐与失效记录</n-button><span v-if="loading">扫描中 · 暂时保留原列表</span></div>
      <div class="recommendation-objectives"><p>{{ primaryObjective }}</p><p>T+1持有风险单独评估：A股当日买入通常不能当日卖出，仍须承担隔夜与下一交易日退出风险。当日价格观察不等于可实现收益。</p><p v-if="isDailyPrimary">日线条件尚未经过概率校准。{{ dailyRankMeaning }}；卡片保留本轮日K来源、报价时间和风险条件。</p><p v-else>规则分用于排序，不是上涨概率。刷新只更新本轮证据，首次主推荐的时间和参考价以已保存记录为准。</p></div>
      <p v-if="!isDailyPrimary && (adaptive.morningAssessment?.meaning || adaptive.morningAssessment?.message)" class="quote-coverage">{{ adaptive.morningAssessment.meaning || adaptive.morningAssessment.message }}</p>
      <p v-if="!primaryContractKnown && result.adaptive" class="quote-coverage">旧版快照保留原版本候选；未记录统一准入，不标记为通过当前规则。</p>
      <p v-if="!isDailyPrimary && (adaptive.profilePortfolioPolicies?.[precisionProfile] || adaptive.portfolioPolicy)" class="quote-coverage">{{ portfolioSummary(adaptive.profilePortfolioPolicies?.[precisionProfile] || adaptive.portfolioPolicy) }}</p>
      <p class="quote-coverage">本轮主板初筛 {{ researchCoverage.universe }} 只 · 进入深研 {{ researchCoverage.queued }} 只 · 完成深研 {{ researchCoverage.completed }} 只 · 本档候选 {{ adaptivePicks.length }} 只</p>
      <p v-if="snapshotIsOld" class="quote-coverage">以上为已保存的历史结果，价格与证据截至标注的生成时间；当前页面会同步后台新结果，历史回看可点击“返回当前”。</p>
      <div v-if="quoteCoverageSummary || scheduleSummary" class="quote-coverage"><span v-if="quoteCoverageSummary" title="新鲜表示通过本轮报价时效核验，不代表已成交。">{{ quoteCoverageSummary }}</span><span v-if="scheduleSummary" :title="`北京时间 · 目标 ${adaptive.delivery.target_at} · 实际 ${adaptive.delivery.decision_at || '未记录'}`">{{ scheduleSummary }}</span></div>
      <div v-if="!isDailyPrimary && adaptive.dataQuality?.independent_evidence" class="quote-coverage">
        <span v-if="adaptive.dataQuality?.minute_coverage">有效分钟证据：{{ adaptive.dataQuality.minute_coverage.usable }}/{{ adaptive.dataQuality.minute_coverage.requested }} 只</span>
        <span>20日同刻基准齐全：{{ adaptive.dataQuality.independent_evidence.history_20d_ready }}/{{ adaptive.dataQuality.research_count }} 只</span>
        <span>行业联动确认：{{ adaptive.dataQuality.independent_evidence.sector_confirmed }} 只</span>
        <span>分钟资金数据：{{ adaptive.dataQuality.independent_evidence.fund_source_available }} 只</span>
      </div>
      <details v-if="isDailyPrimary" class="sk-help-details picks-method daily-checks"><summary>日线检查结果与未入选原因</summary><p>可用已完成日K {{ dailyAssessment.dailyUsableCount ?? '未记录' }} / {{ dailyAssessment.researchedCount ?? '未记录' }} 只 · 日线与新鲜报价核验 {{ dailyAssessment.quoteUsableCount ?? '未记录' }} 只 · 财务事件覆盖通过 {{ dailyAssessment.eventCoveragePassedCount ?? '未记录' }} 只</p><p v-for="item in dailyExcluded" :key="item.key">{{ item.label }}：{{ item.count }} 只</p><p v-for="(count, key) in dailyAssessment.profileExcludedCounts?.[precisionProfile] || {}" :key="key">本档 {{ dailyReason(key) }}：{{ count }} 只</p><p>{{ dailyAssessment.meaning || '基于已完成日K、当日报价与披露风险的独立条件筛选。' }}</p><p>各原因可能重叠，不能相加为股票总数。</p></details>
      <details v-else-if="isV11" class="sk-help-details picks-method"><summary>规则与覆盖</summary><p>{{ researchCoverage.description }}</p><p>行情 {{ adaptive.dataQuality?.snapshot_count ?? '未知' }} 只；深研完成表示已逐只核验，缺失和过期证据仍单独记录，不代表全部证据齐全。</p><p>{{ adaptive.marketSummary }}</p><p>算法 {{ adaptive.scoreVersion || adaptive.recommendationLogicVersion || '以单只候选记录为准' }}。</p><p>{{ adaptive.algorithm?.missingFactorPolicy || '缺失处理以该次记录的算法版本为准。' }}</p><p>{{ adaptive.algorithm?.formula || '评分公式以该次记录为准。' }}</p><p>{{ adaptive.algorithm?.profilePolicy }}</p><p>规则参数为待验证初值。通过独立检验的模型才提供预测；数据覆盖不是胜率。研究配置执行市场与行业预算，页面不代表实际成交。</p></details>
      <details v-else class="sk-help-details picks-method"><summary>旧版历史口径</summary><p>{{ modelMix }} · {{ adaptive.modelVersion }}。旧分快照保留用于回看；下一次刷新采用当前规则与证据筛选。</p></details>
      <details v-if="hasDailyEvidence && dailyAuditSummary" class="sk-help-details picks-method"><summary>逐股审计摘要 · 均衡档</summary><p>{{ dailyAuditSummary }}</p><p>{{ dailyAssessment.auditMeaning || '仅记录实际进入深研的股票；不能据此计算全市场漏选率。' }}</p></details>
      <details v-if="hasDailyEvidence && Object.keys(dailyEmotionPanel).length" class="sk-help-details picks-method"><summary>A股市场情绪观察</summary>
        <p>来源 {{ dailyEmotionPanel.source || '未取得' }} · 截至 {{ formatTime(dailyEmotionPanel.asOf) }}</p>
        <p v-if="dailyEmotionPanel.decisionAssessment?.available === false">本轮决策的市场证据保持未知，以下仅保留原记录时点的观测，不视为当前有效确认。</p>
        <p v-else-if="dailyEmotionPanel.decisionAssessment?.available === true">{{ marketBreadthSummary(dailyEmotionPanel.decisionAssessment) }}；核验 {{ formatTime(dailyEmotionPanel.evaluatedAt) }}。</p>
        <p v-for="metric in dailyEmotionMetrics" :key="metric.key">{{ metric.label }}：{{ metric.value }}<template v-if="metric.history"> · {{ metric.history }} · {{ metric.percentile }}</template></p>
        <p v-if="dailyEmotionPanel.emotionDiagnostics?.meaning || dailyEmotionPanel.emotionDiagnostics?.message">{{ dailyEmotionPanel.emotionDiagnostics.meaning || dailyEmotionPanel.emotionDiagnostics.message }}</p>
        <p v-for="gap in dailyEmotionPanel.gaps || []" :key="gap">{{ gap }}</p>
        <p v-for="limit in dailyEmotionPanel.emotionDiagnostics?.interpretationLimits || []" :key="limit">{{ limit }}</p>
        <p>原始情绪指标可在历史不足时保留；缺失值不视为中性。</p>
        <p v-if="dailyEmotionPanel.emotionDiagnostics?.measurementVersion === 'explicit-limit-observations-v2'">涨跌停统计采用已核验的提供方明确限价；无可靠限价及特殊交易状态保持未知。</p>
        <p v-else>本记录未注明限价计量口径，历史统计保留原含义。</p>
      </details>
      <details v-if="hasDailyEvidence && dailyAttackWatchlist.length" class="sk-help-details picks-method"><summary>进攻观察 · {{ dailyAttackWatchlist.length }} 只 · 未确认</summary>
        <p>记录尚未封板的方向性机会，供后续核验；观察身份不构成正式推荐，也不代表随后会涨停。</p>
        <p>{{ dailyRankMeaning }}</p>
        <div v-for="pick in dailyAttackWatchlist" :key="codeOf(pick)" class="daily-attack-row">
          <p><b>#{{ pick.rank }} {{ nameOf(pick) }} {{ codeOf(pick) }}</b> · 观察价 ¥{{ dailyValue(pick.quote?.price) }} · {{ observationChange(pick.quote?.change_pct) }}</p>
          <p>开盘后涨幅 / ATR {{ dailyValue(pick.dailyRankEvidence?.openReturnAtr) }} · 方向效率 {{ dailyValue(pick.dailyRankEvidence?.directionalEfficiency) }} · 20日新高 {{ pick.breakout20 === true ? '已突破' : pick.breakout20 === false ? '未突破' : '未知' }}</p>
          <p v-if="pick.dailyPrimaryReasons?.length">均衡档未通过原因：{{ pick.dailyPrimaryReasons.map(dailyReason).join('；') }}</p>
          <p v-if="pick.marketBreadth">{{ marketBreadthSummary(pick.marketBreadth) }}</p>
          <p v-if="pick.riskFlags?.length">{{ primaryAdmissionReasons(pick.riskFlags).join('；') }}</p>
          <p>截至 {{ formatTime(pick.sourceTime) }} · {{ pick.meaning }}</p>
        </div>
      </details>
      <n-spin :show="loading && !adaptivePicks.length">
        <section v-if="!isDailyPrimary && adaptivePicks.length" class="pick-grid">
          <n-card v-for="{ pick, detail } in adaptiveCards" :key="codeOf(pick)" size="small" :bordered="false" class="pick-card tier-adaptive">
            <template #header><div class="pick-title"><b>{{ pick.rank }}</b><strong>{{ nameOf(pick) }}</strong><code>{{ codeOf(pick) }}</code><n-tag size="small" :bordered="false" type="info">{{ primaryRecommendationState(pick) }}</n-tag></div></template>
            <div class="pick-body">
              <button class="pick-why" @click="showWhy(pick)"><b>为什么入选 · {{ detail.title }}</b><span>{{ detail.reason }}</span><b>查看全部指标与条件 ↗</b></button>
              <div v-if="detail.indicators.some(row => row.value !== '未取得')" class="indicator-highlights"><span v-for="row in detail.indicators.filter(row => row.value !== '未取得').slice(0, 3)" :key="row.key">{{ row.label }} <b>{{ row.value }}</b></span></div>
              <div class="first-reference"><span>首次主推荐 {{ formatTime(firstRecommendationEvidence(pick).time) }}</span><b>{{ firstRecommendationEvidence(pick).price == null ? '参考价未记录' : `参考价 ¥${firstRecommendationEvidence(pick).price.toFixed(2)}` }}</b></div>
              <div class="pick-quote"><strong>{{ pick.referencePrice == null ? '—' : `¥${Number(pick.referencePrice).toFixed(2)}` }}</strong><span>本轮参考价</span><span :class="Number(pick.currentChange) > 0 ? 'up' : Number(pick.currentChange) < 0 ? 'down' : ''">{{ pick.currentChange == null ? '—' : `${Number(pick.currentChange).toFixed(2)}%` }}</span></div>
              <p class="quote-coverage">规则排序 {{ scoreOf(pick) }} 分 · 非概率</p>
              <div v-if="pick.quote" class="quote-evidence" title="北京时间；延迟按采集时间减原始源时间计算。此处为本轮保存的行情快照。">{{ quoteEvidence(pick) }}</div>
              <div v-if="isV11Pick(pick)" class="v11-facts">
                <div><span>Early / MainRise</span><b>{{ factorValue(pick.earlyScore) }} / {{ factorValue(pick.mainRiseScore) }}</b></div>
                <div><span>风险 / 数据覆盖</span><b>{{ factorValue(pick.distributionRisk) }} / {{ percent(pick.dataConfidence ?? pick.confidence, 0) }}</b></div>
                <div><span>同刻历史覆盖</span><b>{{ pick.historicalCoverageDays ?? pick.baselineHistoryDays ?? 0 }} / 20 日</b></div>
                <div><span>价格结构</span><b>{{ pick.observableStructure || '待确认' }}</b></div>
              </div>
              <p v-if="detail.risks.length" class="pick-risk" :title="detail.risks.join('；')"><span>主要风险</span>{{ detail.risks[0] }}</p>
              <p v-else class="pick-risk"><span>主要风险</span>未取得具体风险说明，需进一步核验</p>
              <details class="sk-help-details pick-details"><summary>依据与风险</summary>
                <p v-if="detail.legacyNotice"><span>旧版记录</span>{{ detail.legacyNotice }}</p>
                <p v-if="detail.savedExplanation"><span>原记录说明</span>{{ detail.savedExplanation }}</p>
                <p><span>逻辑版本</span>{{ detail.logicVersion }}</p>
                <p v-if="pick.primaryAdmission"><span>统一准入</span>{{ primaryAdmissionReasons([...(pick.primaryAdmission.missingReasons || []), ...(pick.primaryAdmission.reasons || [])]).join('；') || '本轮通过已记录的准入条件' }}</p>
                <p v-if="pick.primaryAdmission?.warnings?.length"><span>市场风险</span>{{ primaryAdmissionReasons(pick.primaryAdmission.warnings).join('；') }}</p>
                <p v-if="pick.precisionDecision"><span>入场检查</span>{{ primaryAdmissionReasons(pick.precisionDecision.profiles?.[precisionProfile]?.reasons).join('；') }}</p>
                <p v-if="pick.precisionDecision"><span>财务与事件</span>{{ pick.precisionDecision.sourceContext?.gaps?.join('；') || '已取得财报、预告与解禁覆盖；按记录时点核验' }}</p>
                <p v-if="pick.precisionDecision"><span>价格位置</span>MA5乖离 {{ pick.precisionDecision.metrics.biasMa5Pct?.toFixed(2) ?? '未知' }}% · ATR距离 {{ pick.precisionDecision.metrics.atrExtension?.toFixed(2) ?? '未知' }} 倍</p>
                <p><span>研究排序分</span>{{ scoreOf(pick) }} · 不是胜率或收益预测</p>
                <p v-if="pick.dataEligibility"><span>数据资格</span>{{ { formal: '满足历史与关键因子要求', observation: '历史、关键因子或时效未满足，仅观察', insufficient: '历史或关键证据不足' }[pick.dataEligibility.status] || '待核验' }} · {{ pick.scoreStage || '阶段待确认' }}</p>
                <p v-for="(packet, key) in pick.researchFactors || {}" :key="key"><span>{{ { C: '事件催化', G: '历史股性', E: '市场情绪', H: '题材扩散（影子）' }[key] || key }}</span>{{ { observed: '已记录', insufficient: '样本不足', unavailable: '未取得' }[packet.status] || '未取得' }} · {{ packet.gaps?.join('；') || packet.source || '来源见指标证据' }}</p>
                <p v-if="pick.prediction"><span>{{ predictionSummary(pick.prediction).label }}</span>{{ predictionSummary(pick.prediction).detail }}</p><p v-if="pick.plannedWeight != null"><span>研究配置</span>{{ (pick.plannedWeight * 100).toFixed(1) }}% · 按总预算及行业上限分配</p>
                <p v-if="pick.marketRegime"><span>情绪调节</span>{{ { weak: '偏弱', hot: '过热', normal: '正常', unknown: '证据不足，保留基础分' }[pick.marketRegime.state] || '待核验' }} · 研究分系数 {{ pick.marketRegime.multiplier ?? '未取得' }}，不代表实际仓位</p>
                <template v-if="isV11Pick(pick)"><p><span>主升概率</span>{{ calibrationValue(pick, 'mainRiseProbability') }}</p><p><span>预计MFE</span>1日 {{ calibrationValue(pick, 'expectedMFE1') }} · 3日 {{ calibrationValue(pick, 'expectedMFE3') }} · 5日 {{ calibrationValue(pick, 'expectedMFE5') }}</p><p><span>预计MAE</span>1日 {{ calibrationValue(pick, 'expectedMAE1') }} · 3日 {{ calibrationValue(pick, 'expectedMAE3') }} · 5日 {{ calibrationValue(pick, 'expectedMAE5') }}</p></template>
                <template v-if="pick.entryExecution"><p><span>冻结入场计划</span>{{ entryExecutionSummary(pick.entryExecution).label }}</p><p>{{ entryExecutionSummary(pick.entryExecution).detail }}</p><p class="pick-hint">{{ entryExecutionSummary(pick.entryExecution).meaning }}</p></template>
                <p><span>观察周期</span>{{ detail.entryPlan.holdingWindow }}</p>
                <p><span>不追条件</span>{{ detail.entryPlan.noChase }}</p>
                <PickIndicatorEvidence :indicators="detail.indicators" />
                <p v-if="pick.quote"><span>源时间</span>{{ pick.quote.provider_timestamp || pick.quote.source_time || '未知' }}</p>
                <p v-if="pick.quote?.fetched_at"><span>采集时间</span>{{ pick.quote.fetched_at }}</p>
                <p v-if="pick.transition"><span>状态</span>{{ pick.transition }}</p>
                <p v-if="pick.data_quality?.gaps"><span>缺口</span>{{ pick.data_quality.gaps.join('；') }}</p>
                <p v-if="pick.pool"><span>候选池</span>{{ pick.pool }}池 · {{ pick.positioning }}</p>
                <p><span>VWAP</span>{{ pick.vwapState || '未知' }}</p>
                <p><span>历史案例</span><template v-if="historicalRateVisible(pick)">{{ pick.similarHistoryFeedback.rateSampleCount }} 个 · 3日内触板 {{ percent(pick.similarHistoryFeedback.touchWithin3dRate) }}<br/>描述性区间 {{ (pick.similarHistoryFeedback.touchWithin3dInterval95 || []).map(v => percent(v)).join(' – ') }}；未消除案例相关性，不代表当前股票上涨概率。</template><template v-else>{{ pick.similarHistoryFeedback?.message || '历史频率不可用' }}</template></p>
                <p v-if="pick.auctionConfirmation && pick.auctionConfirmation !== 'not_applicable'"><span>竞价</span>{{ pick.auctionConfirmation }}</p>
                <p><span>触发</span>{{ detail.entryPlan.trigger }}</p>
                <p><span>失效</span>{{ detail.entryPlan.invalidations }}</p>
                <p><span>风险</span>{{ detail.risks.join('；') || '未取得' }}</p>
              </details>
            </div>
            <template #footer><n-space justify="end"><n-button size="small" secondary @click="addWatch(pick)">自选</n-button><n-button size="small" type="primary" @click="openKline(pick)">图表</n-button></n-space></template>
          </n-card>
        </section>
        <div v-else-if="!adaptivePicks.length" class="primary-empty" role="status"><p>{{ primaryEmptyMessage }}</p><p>研究观察已在本页独立展示，不用于补足主推荐。</p></div>
        <section v-if="hasDailyObservation" class="daily-observation-summary" aria-label="独立日线观察说明">
          <div class="adaptive-summary"><b>日线观察 · {{ dailyPicks.length }} 只 · 不计入主推荐</b></div>
          <p>日线条件仅供研究；市场环境、分钟结构、板块联动及资金证据未全部通过时，不进入正式主榜。</p>
          <p>{{ dailyAssessment.message || '本轮日线观察按已保存的价格与风险证据展示。' }}</p>
          <p>{{ dailyRankMeaning }}；排列顺序不代表上涨概率。</p>
        </section>
        <section v-if="dailyPicks.length" class="pick-grid daily-primary-grid" :aria-label="isDailyPrimary ? '日线条件推荐' : '日线观察'">
          <n-card v-for="(pick, index) in dailyPicks" :key="codeOf(pick)" size="small" :bordered="false" class="pick-card tier-adaptive daily-primary-card">
            <template #header><div class="pick-title"><b>{{ index + 1 }}</b><strong>{{ nameOf(pick) }}</strong><code>{{ codeOf(pick) }}</code><n-tag size="small" :bordered="false" type="info">{{ isDailyPrimary ? '日线条件' : '日线观察' }}</n-tag></div></template>
            <div class="pick-body">
              <p v-if="!isDailyPrimary" class="premarket-data-gap">{{ (pick.dataGaps || []).join('；') || '市场、板块或资金等正式准入证据仍待核验。' }}</p>
              <p class="daily-selection-reason">{{ pick.selectionReasons?.[0] || pick.reasons?.[0] || '查看本轮保存的日K与价格条件' }}</p>
              <div v-if="isDailyPrimary" class="first-reference"><span>首次日线推荐 {{ formatTime(firstRecommendationEvidence(pick).time) }}</span><b>{{ firstRecommendationEvidence(pick).price == null ? '首次参考价未记录' : `首次参考价 ¥${firstRecommendationEvidence(pick).price.toFixed(2)}` }}</b><span>{{ primaryRecommendationState(pick) }}</span></div>
              <div v-else class="first-reference"><span>本轮日线观察 {{ pick.decision_at ? formatTime(pick.decision_at) : '未记录' }}</span><b>{{ dailyValue(pick.referencePrice) === '未取得' ? '本轮参考价未记录' : `本轮参考价 ¥${dailyValue(pick.referencePrice)}` }}</b><span>{{ primaryRecommendationState(pick) }}</span></div>
              <div class="pick-quote"><strong>¥{{ dailyValue(pick.referencePrice) }}</strong><span>本轮参考价</span><span>{{ observationChange(pick.currentChange ?? pick.quote?.change_pct) }}</span></div>
              <div v-if="pick.quote" class="quote-evidence">{{ quoteEvidence(pick) }}</div>
              <p><span>日K基准</span>{{ pick.baselineAsOf || '未记录' }} · {{ pick.baselineSource || '来源未记录' }}</p>
              <div class="v11-facts daily-facts"><div><span>支撑 / 压力</span><b>¥{{ dailyValue(pick.support) }} / ¥{{ dailyValue(pick.resistance) }}</b></div><div><span>压力空间 / 风险距离</span><b>{{ dailyValue(pick.structureRewardRisk) }} 倍</b></div><div><span>MA5乖离 / ATR距离</span><b>{{ dailyValue(pick.biasMa5Pct) }}% / {{ dailyValue(pick.atrExtension) }} 倍</b></div><div><span>观察目标</span><b>下一交易日（T+1）</b></div></div>
              <details class="sk-help-details pick-details"><summary>查看日K证据、条件与风险缺口</summary>
                <p v-for="reason in pick.selectionReasons || pick.reasons || []" :key="reason"><span>{{ isDailyPrimary ? '入选依据' : '观察依据' }}</span>{{ reason }}</p>
                <p><span>当前档位</span>{{ pick.dailyProfileDecision?.[precisionProfile]?.label || precisionProfile }} · {{ isDailyPrimary ? '本轮通过已记录的日线价格和风险条件' : '仅记录日线条件，尚未通过正式准入' }}</p>
                <p v-if="pick.eventAssessment"><span>财务事件</span>财报 {{ pick.eventAssessment.coverage?.financials === true ? '已覆盖' : '待补' }} · 预告 {{ pick.eventAssessment.coverage?.forecast === true ? '已覆盖' : '待补' }} · 解禁 {{ pick.eventAssessment.coverage?.unlock === true ? '已覆盖' : '待补' }}</p>
                <p v-if="pick.eventAssessment"><span>14日解禁</span>{{ dailyValue(pick.eventAssessment.unlock14dPct) }}%</p>
                <p v-for="risk in pick.risks || pick.riskFlags || []" :key="risk"><span>风险</span>{{ risk }}</p>
                <p v-for="gap in pick.dataGaps || []" :key="gap"><span>证据缺口</span>{{ gap }}</p>
                <p v-if="pick.marketBreadth"><span>市场环境</span>{{ marketBreadthSummary(pick.marketBreadth) }}</p>
                <PickIndicatorEvidence :indicators="dailyIndicators(pick)" />
                <p><span>排序依据</span>{{ pick.rankMeaning || '本轮保存的成交金额与价格位置排序' }}</p>
                <p v-if="pick.dailyRankEvidence"><span>排序证据</span>开盘后涨幅 / ATR {{ dailyValue(pick.dailyRankEvidence.openReturnAtr) }} · 方向效率 {{ dailyValue(pick.dailyRankEvidence.directionalEfficiency) }} · 本档比较 {{ pick.dailyRankEvidence.referencePeerCount ?? '未记录' }} 只</p>
                <p><span>行情源时间</span>{{ pick.sourceTime || pick.quote?.provider_timestamp || pick.quote?.source_time || '未记录' }}</p>
                <p><span>条件生成</span>{{ formatTime(pick.decision_at) }}</p>
              </details>
            </div>
            <template #footer><n-space justify="end"><n-button size="small" secondary @click="addWatch(pick)">自选</n-button><n-button size="small" type="primary" @click="openKline(pick)">日K图表</n-button></n-space></template>
          </n-card>
        </section>
        <details v-if="insufficientCards.length && !isDailyPrimary" class="sk-help-details insufficient-evidence"><summary>未通过准入 · {{ insufficientCards.length }} 只</summary><p>查看数据缺口与风险原因。研究线索保留在独立观察区。</p><article v-for="{ pick, detail } in insufficientCards" :key="codeOf(pick)"><div><b>{{ nameOf(pick) }}</b><code>{{ codeOf(pick) }}</code><n-button size="tiny" secondary @click="showWhy(pick, true)">查看准入缺口</n-button></div><p>{{ primaryAdmissionReasons([...(pick.primaryAdmission?.missingReasons || []), ...(pick.primaryAdmission?.reasons || []), ...detail.risks]).join('；') || detail.reason }}</p></article></details>
      </n-spin>
      </template>
      <section v-if="invalidatedPicks.length" class="invalidated-recommendations"><h3>原推荐状态更新</h3><p>失效后退出主推荐；首次信号和原始依据继续保存在推荐记录。</p><article v-for="pick in invalidatedPicks" :key="codeOf(pick)"><b>{{ nameOf(pick) }} {{ codeOf(pick) }} · {{ primaryRecommendationState(pick) }}</b><p>首次主推荐 {{ formatTime(firstRecommendationEvidence(pick).time) }} · 参考价 {{ firstRecommendationEvidence(pick).price ?? '未记录' }}</p><p>{{ primaryAdmissionReasons([...(pick.primaryAdmission?.missingReasons || []), ...(pick.primaryAdmission?.reasons || [])]).join('；') || pick.transition || '查看原推荐与最新核验记录' }}</p></article></section>
    </section>

    <PicksLedger v-for="mode in visitedLedgerModes" v-show="viewMode === mode" :key="mode" :mode="mode" :active="pageActive && viewMode === mode" :refresh-token="mode === 'records' ? recordsRefreshToken : 0" @open-chart="openKline" />

    <section v-show="viewMode === 'classic'">
      <n-tabs v-model:value="activeTier" type="segment" animated class="tier-tabs"><n-tab-pane v-for="tier in tierOptions" :key="tier.key" :name="tier.key"><template #tab><span class="tier-tab" :title="tier.note"><b>{{ tier.label }}</b></span></template></n-tab-pane></n-tabs>
      <details class="sk-help-details picks-method"><summary>方法与口径</summary><p>{{ tierData.description || (activeTier === 'regular' ? '均衡多因子与趋势质量策略。' : '暂无新版数据。') }}</p><p v-if="activeTier === 'aggressive'">“生成时未封板”只表示当时有成交、未停牌且低于涨停价至少0.01元，不保证之后可成交。卡片分数为候选百分位；触板概率仅在模型通过发布门槛时显示。</p></details>
      <n-tabs v-model:value="activeBoard" type="line" animated><n-tab-pane name="nonKeChuang" tab="非科创板" /><n-tab-pane name="kechuang" tab="科创板" /></n-tabs>
      <n-spin :show="loading">
        <section v-if="picks.length" class="pick-grid">
          <n-card v-for="(pick, index) in picks" :key="codeOf(pick)" size="small" :bordered="false" class="pick-card" :class="`tier-${activeTier}`">
            <template #header><div class="pick-title"><b>{{ index + 1 }}</b><strong>{{ nameOf(pick) }}</strong><code>{{ codeOf(pick) }}</code><n-tag size="small" :bordered="false" type="info">{{ scoreOf(pick) }} 分</n-tag></div></template>
            <div class="pick-body">
              <button class="pick-why" @click="showWhy(pick)"><span>{{ explainPick(pick).reason }}</span><b>为何入选 ↗</b></button>
              <p v-if="activeTier === 'aggressive'"><span>状态</span>{{ pick.buyability?.reason || '生成时未封板' }}<em v-if="permissionLabel(pick)"> · {{ permissionLabel(pick) }}</em></p>
              <p v-else><span>风险</span>{{ pick.riskDecision || pick.risk_decision || '已通过硬过滤' }}</p>
              <p v-if="degradedOf(pick).length"><span>降级</span>{{ degradedOf(pick).join('；') }}</p>
              <details class="sk-help-details pick-details"><summary>依据与模型</summary>
                <p><span>来源</span>{{ pick.screeningSource || pick.screening_source || pick.strategy || '策略筛选' }}</p>
                <p><span>评分</span>{{ pick.scoreMeaning || '当日同板块候选百分位' }} · {{ modelLabel(pick) }}</p>
                <p v-if="reasonsOf(pick).length"><span>依据</span>{{ reasonsOf(pick).join('；') }}</p>
                <template v-if="activeTier === 'conservative' && pick.modelStatus === 'qualified'"><p><span>收益下界</span>20日 {{ percent(pick.returnLowerBound20d) }} / 60日 {{ percent(pick.returnLowerBound60d) }}</p><p><span>预测风险</span>波动上界 {{ percent(pick.predictedVolatilityUpper20d) }} / 回撤Q10 {{ percent(pick.drawdownQuantile20d) }}</p></template>
                <template v-if="activeTier === 'regular' && pick.modelStatus === 'qualified'"><p><span>中性排名</span>5日 {{ Number(pick.excessRank5d || 0).toFixed(1) }} / 20日 {{ Number(pick.excessRank20d || 0).toFixed(1) }}</p><p><span>周期</span>{{ pick.expectedHoldingRange || '5–20d' }} · 行业中性</p></template>
                <p v-if="activeTier === 'aggressive'"><span>周期</span>未来1–3个交易日</p>
                <p v-if="activeTier === 'aggressive' && pick.modelStatus === 'qualified'"><span>触板概率</span>1日 {{ percent(pick.touchProbability1d) }} / 3日 {{ percent(pick.touchProbability3d) }} · 基准 {{ percent(pick.baseRate3d) }}</p>
              </details>
            </div>
            <template #footer><n-space justify="end"><n-button size="small" secondary @click="addWatch(pick)">自选</n-button><n-button size="small" type="primary" @click="openKline(pick)">图表</n-button></n-space></template>
          </n-card>
        </section>
        <n-empty v-else :description="shortfall ? `合格候选不足，缺额 ${shortfall} 只` : '暂无合格候选'" style="padding:56px 0" />
      </n-spin>
    </section>
    <n-drawer v-model:show="methodVisible" :width="500" placement="right"><n-drawer-content title="精选 · 方法与使用" closable><div class="pick-explanation">
      <ol class="usage-steps"><li><b>刷新扫描</b> · 检查列表生成时间、行情源时间和覆盖范围。</li><li><b>为何入选</b> · 查看实际因子、价格结构与失效条件。</li><li><b>图表</b> · 使用共用行情核对当前结构。</li><li><b>延后复盘</b> · 查看T+1、T+3、T+5的观察变化与模拟成交。</li></ol>
      <template v-if="isDailyPrimary"><h3>日线条件推荐</h3><p>使用上一完整交易日及更早的日K、当日新鲜报价、涨跌停状态、财报、业绩预告与解禁证据独立筛选。三档分别执行价格距离、压力空间和事件风险条件，每档最多5只；证据缺失或风险不满足仍可为空。日线条件以下一交易日（T+1）为观察目标，未经概率校准。</p><p>日线卡片展示原始日K指标、来源日期、支撑压力和风险缺口；分钟历史与上午模型的准备进度另列，日线条件不作为分钟模型已通过的证据。</p></template>
      <h3>分钟增强 · 任务化学习与规则研究</h3><p>分钟分支筛选沪深主板非ST，每档每轮最多5只。突破和修复使用启动分，延续使用主升分，再计入风险与市场状态调节。每轮重新核验队列，候选及未入选对照一同记录；已保存的V1.1记录仍显示其原版本公式。</p>
      <h3>主推荐与研究观察</h3><p v-if="!isDailyPrimary">主推荐只展示本轮通过统一准入的候选，数据缺失或风险条件未满足时允许空榜。正式主推荐按T+1合法退出的扣费净收益排序与准入。上午方向观察独立考察参考价到当日收盘的价格变化与回撤，不改变正式推荐排序，也不把当天上涨当作当天可卖出的盈利。</p><p>潜伏和延续是两类独立研究观察，在本地精选中直接显示，并随后台已完成的扫描更新；类别与目标日独立标注，不计入主推荐。研究规则分不是上涨概率。旧版候选保留原版本与原始证据，不标记为通过新准入。</p><p v-if="!isDailyPrimary">重复扫描继续使用账本记录的首次主推荐时间和参考价；没有记录的字段显示未知。后续失效的股票退出主推荐，原始记录继续保留，可在推荐记录中查看。</p>
      <details><summary>入选依据与指标</summary><p>量或价的同刻历史不足5日时不生成评分；5～19日仅供观察。正式候选须量价历史均达到20日、M/V/W/B关键因子有效且分钟证据新鲜，再通过本档风险门槛。数据覆盖不是胜率。缺失因子保持为空、得分贡献为零，固定权重不重新分配；缺失风险按该项上界计入惩罚。</p><p>事件、历史股性和换手参与规则分；市场情绪仅向下调节研究分；主推荐仍须独立通过市场、板块和资金准入，缺失时只保留研究记录。题材扩散只作影子记录，新闻社交舆情尚未接入。研究分不是收益预测；未经校准的MFE、MAE及概率保持待校准。风险系数0.2等参数均为待验证初值，旧记录没有保存的证据不事后补写。</p></details>
      <details><summary>旧版历史分支名称</summary><div class="branch-list"><div v-for="(item,key) in PICK_BRANCHES" :key="key"><code>{{ key }}</code><span>{{ item.name }}</span></div></div></details>
      <h3>预测任务与复盘</h3><p>上午方向观察单独评估首次信号到当日收盘的表现与回撤，并保留T+1合法退出的扣费收益评估。下午入场任务继续考察下一交易日合法退出。潜伏、延续独立研究次日价格事件，不能用触板代替可成交盈利。各任务独立成熟、训练和验证。行情身份或价格路径存在冲突时隔离相关数据，旧模型不继续使用失效的数据版本。</p><p>模型使用时间顺序的训练、校准、滚动检验与最终留出。未完成验证时保留规则研究状态，不显示预测概率；启用模型后必须满足预期净收益和风险调整效用均为正，允许本轮零只。</p>
      <h3>策略分组</h3><p>V1.2三档共用研究排序，分别执行乖离、ATR、结构盈亏比、业绩与解禁门槛。事件风险覆盖未知时仅观察；已知负面业绩预告或超限解禁独立否决。激进档在分钟突破已核验时允许上方压力与盈亏比未知，但不生成目标价；已知盈亏比仍须达标。旧版不同策略的分值不能直接比较，旧契约模型不会用于新规则。</p>
      <details><summary>定时扫描与学习</summary><p>北京时间09:20盘前观察；09:40、09:55、10:30、14:55独立扫描；15:30归档与到期复盘；周五15:45本地训练。自动推荐和自动学习分别控制。错过时点保留遗漏记录，手动刷新不计入正式训练。</p><p>默认至少120个成熟交易日、1000条有效样本，并满足训练和校准分段要求后才检验模型。独立留出段至少60个信号日、50笔完成交易；训练与验证按实际标签结束时间隔离，至少留5个交易日间隔。扣费收益与相对优势的置信下界须为正，回撤不恶化，再从模型实际创建后开展至少20个交易日且至少50笔完成观察的前瞻影子验证。失败标签日与空仓日保留，未达要求继续使用规则。</p><p>本地精选读取后台最新完成并保存的扫描，页面可见时每30秒自动同步，返回页面时重新读取。主推荐、日线观察、潜伏与延续观察随同一快照更新。“刷新扫描”成功后以新结果覆盖当前列表，失败时保留已保存的结果；新一轮无合格候选时明确显示空结果。查看扫描历史时保留所选快照，点击“返回当前”恢复最新结果。模拟成交使用下一完整分钟入场，满足T+1后尝试期限收盘退出；遇跌停、停牌或容量不足则继续持仓，逐日标价并等待首个有完整证据的可卖分钟。缺路径不跳过，未成交独立记录；价格观察与模拟收益均不是用户真实交易。</p></details>
    </div></n-drawer-content></n-drawer>
    <n-drawer v-model:show="explanationVisible" :width="520" placement="right"><n-drawer-content :title="`${nameOf(selectedPick)} · ${selectedIsInsufficient ? '交易条件未满足' : '为什么入选'}`" closable><div class="pick-explanation">
      <h3>{{ explanation.title }}</h3><small>快照 {{ formatTime(adaptive.generatedAt || result.generatedAt) }} · 行情来源 {{ explanation.source }}<br/>推荐逻辑 {{ explanation.logicVersion }}</small>
      <h3>{{ selectedIsInsufficient ? '已记录线索（非交易候选）' : '为什么入选' }}</h3><ul v-if="explanation.reasons.length" class="explanation-reasons"><li v-for="reason in explanation.reasons" :key="reason">{{ reason }}</li></ul><p v-else>{{ explanation.reason }}</p>
      <p v-if="explanation.legacyNotice" class="legacy-notice">{{ explanation.legacyNotice }}</p>
      <details v-if="explanation.savedExplanation"><summary>原记录说明（不作为补写的入选依据）</summary><p>{{ explanation.savedExplanation }}</p></details>
      <h3>风险与反证</h3><ul v-if="explanation.risks.length" class="explanation-reasons"><li v-for="risk in explanation.risks" :key="risk">{{ risk }}</li></ul><p v-else>未取得具体风险说明，需进一步核验。</p>
      <section v-if="explanation.evidenceChecks.length" class="evidence-checks">
        <h3>入选条件核验</h3>
        <p v-for="check in explanation.evidenceChecks" :key="check.key">{{ check.passed ? '✓' : '—' }} {{ check.label }}：{{ check.passed ? '满足' : '未满足' }}</p>
        <p>同刻历史样本：{{ explanation.baselineDays ?? 0 }}/20 个交易日；来源：{{ explanation.baselineSource }}</p>
        <p v-if="explanation.missingHistoryDates.length">待补齐日期：{{ explanation.missingHistoryDates.join('、') }}</p>
        <p>{{ isV11Pick(selectedPick) ? '缺失可选因子保留为空，不重新分配权重；报价与交易状态仍须通过检查。' : '此处保留历史快照当时的核验口径。' }}资金指标为供应商分类估算。</p>
      </section>
      <h3>推荐指标 · 当时的数值与判断</h3><PickIndicatorEvidence :indicators="explanation.indicators" />
      <details><summary>{{ explanation.scoreLabel }} · {{ scoreOf(selectedPick) }}（不是胜率）</summary><p>{{ explanation.scoreMeaning }}</p><p>{{ explanation.formula }}</p><div v-for="row in explanation.modelScores" :key="row.key" class="score-row"><span>{{ row.key }} · {{ row.label }}</span><b>{{ row.value }}</b></div></details>
      <h3>证据与来源</h3><p v-if="!explanation.evidence.length">该快照未保留来源；刷新全部后再核对。</p><article v-for="(item,index) in explanation.evidence" :key="index" class="evidence-item"><b>{{ item.title || item.type || '来源' }}</b><p>{{ item.summary || '无摘要' }}</p><small>提取于 {{ formatTime(item.fetched_at) }}（非原始发布时间）</small><a v-if="item.url" :href="item.url" target="_blank" rel="noopener noreferrer">查看来源 ↗</a></article>
      <h3>触发与退出条件</h3><p>观察周期：{{ explanation.entryPlan.holdingWindow }}</p><p>触发：{{ explanation.entryPlan.trigger }}</p><p>失效：{{ explanation.entryPlan.invalidations }}</p><p>不追：{{ explanation.entryPlan.noChase }}</p>
      <details><summary>数据缺口</summary><p>{{ (explanation.quality.gaps || []).join('；') || '旧记录未保存数据缺口检查' }}</p><p>60日历史：{{ explanation.quality.history60d === true ? '有' : '不足 / 未取得' }}；尾盘分钟数据：{{ explanation.quality.minuteTail === true ? '有' : '未取得' }}。文本摘要需回原文核验。</p></details>
      <n-space style="margin-top:18px"><n-button type="primary" @click="openKline(selectedPick)">图表</n-button></n-space>
    </div></n-drawer-content></n-drawer>
    <n-drawer v-model:show="historyVisible" :width="520" placement="right"><n-drawer-content title="精选历史" closable><n-spin :show="historyLoading">
      <div v-if="historyItems.length" class="history-list">
        <article v-for="item in historyItems" :key="item.run_id" class="history-run">
          <div class="history-head"><div><b>{{ formatTime(historyResult(item).generatedAt || item.as_of) }}</b><small>{{ historySlotLabel(item) }}<template v-if="!isReviewHistory(item)"><template v-if="historyResult(item).premarketAssessment"> · 集合竞价 / 开盘预案 {{ historyPremarketCandidates(item).length }} 只</template><template v-else-if="isDailyPrimaryRun(historyResult(item))"> · 日线条件推荐 {{ historyCandidates(item).length }} 只</template><template v-else-if="hasNextDayHistory(item)"> · {{ historySavedSummary(item).splitQueues ? '潜伏' : '次日观察' }} {{ historyNextDayCandidates(item).length }} 只 · 交易候选 {{ historyCandidates(item).length }} 只<template v-if="historySavedSummary(item).splitQueues"> · 延续 {{ historyContinuationCandidates(item).length }} 只</template></template><template v-else> · {{ historyCandidates(item).length }} 只</template></template> · {{ historyStatusLabel(item) }}</small></div><n-button v-if="isScanHistory(item)" size="small" secondary @click="restoreHistory(item)">查看</n-button></div>
          <details v-if="isReviewHistory(item)" class="sk-help-details history-review">
            <summary>查看{{ item.mode === 'king_review' ? '复盘' : '学习' }}摘要</summary>
            <p>{{ reviewSummary(item) }}</p>
            <p v-if="historyResult(item).review">核查 {{ historyResult(item).review.runCount ?? '未知' }} 次运行 · {{ historyResult(item).review.weightsChanged === false ? '未调整权重' : '以本轮学习记录为准' }}</p>
            <p v-if="historyResult(item).localTraining?.status">训练状态：{{ historyResult(item).localTraining.status }}</p>
            <template v-if="historyResult(item).observationReview?.observations?.length">
              <p>观察 {{ historyResult(item).observationReview.observation_count ?? '—' }} 条 · 可核验 {{ historyResult(item).observationReview.verified_count ?? '—' }} 条 · 未知 {{ historyResult(item).observationReview.unknown_count ?? '—' }} 条。价格变化用于复盘，不是成交收益。</p>
              <table class="observation-table"><thead><tr><th>候选</th><th>当时报价</th><th>复盘报价</th><th>变化</th></tr></thead><tbody>
                <tr v-for="(observation, index) in historyResult(item).observationReview.observations" :key="`${observation.run_id}-${observation.code}-${index}`">
                  <td :title="`${observation.code} · ${observation.decision_at || '生成时间未知'}`">{{ observation.name || observation.code }}</td>
                  <td :title="`${quoteSourceLabel(observation.baseline_quote?.source)} · ${observation.baseline_quote?.source_time || '源时间未知'}`">{{ observationPrice(observation.baseline_quote?.price) }}</td>
                  <td :title="`${quoteSourceLabel(observation.review_quote?.source)} · ${observation.review_quote?.source_time || '源时间未知'}`">{{ observationPrice(observation.review_quote?.price) }}</td>
                  <td :title="(observation.gaps || []).join('；')">{{ observationChange(observation.observation_change_pct) }}</td>
                </tr>
              </tbody></table>
            </template>
          </details>
          <div v-else-if="historyPremarketCandidates(item).length || historyCandidates(item).length || historyNextDayCandidates(item).length || historyContinuationCandidates(item).length">
            <template v-if="historyPremarketCandidates(item).length"><p class="history-channel">集合竞价 / 开盘条件预案 · {{ historyPremarketCandidates(item).length }} 只 · 昨夜基准 {{ historyResult(item).premarketAssessment?.baselineSession || historyResult(item).premarketAssessment?.baselineAsOf || '未记录' }}</p><div class="history-candidates"><n-tag v-for="(pick, index) in historyPremarketCandidates(item)" :key="`${item.run_id}-premarket-${codeOf(pick)}-${index}`" size="small" type="info" :bordered="false">#{{ index + 1 }} {{ nameOf(pick) }} · 历史条件预案</n-tag></div></template>
            <template v-if="historyNextDayCandidates(item).length"><p class="history-channel">{{ historySavedSummary(item).splitQueues ? '未触板潜伏' : '历史次日观察（未分队列）' }} · 分数用于研究排序，不是涨停概率</p><div class="history-candidates"><n-tag v-for="(pick, index) in historyNextDayCandidates(item)" :key="`${item.run_id}-next-day-${codeOf(pick)}-${index}`" size="small" type="warning" :bordered="false">#{{ pick.rank ?? index + 1 }} {{ nameOf(pick) }} · 观察分 {{ nextDayScore(pick) }} · 目标 {{ pick.nextDaySignal?.targetSession || '下一交易日' }}</n-tag></div></template>
            <template v-if="historyCandidates(item).length"><p v-if="hasNextDayHistory(item) || isDailyPrimaryRun(historyResult(item))" class="history-channel">{{ isDailyPrimaryRun(historyResult(item)) ? '日线条件推荐 · T+1' : '交易条件候选' }}</p><div class="history-candidates"><n-tag v-for="(pick, index) in historyCandidates(item)" :key="`${item.run_id}-entry-${codeOf(pick)}-${index}`" size="small" :bordered="false">#{{ pick.rank ?? index + 1 }} {{ nameOf(pick) }}<template v-if="!isDailyPrimaryRun(historyResult(item))"> · {{ pick.modelBranch || pick.model }} · {{ scoreOf(pick) }}</template><template v-else> · 日K {{ pick.baselineAsOf || '日期未记录' }}</template></n-tag></div></template>
            <details v-if="historyContinuationCandidates(item).length" class="sk-help-details"><summary>已触板延续 · {{ historyContinuationCandidates(item).length }} 只辅助观察</summary><div class="history-candidates"><n-tag v-for="(pick, index) in historyContinuationCandidates(item)" :key="`${item.run_id}-continuation-${codeOf(pick)}-${index}`" size="small" :bordered="false">#{{ pick.rank ?? index + 1 }} {{ nameOf(pick) }} · 延续分 {{ nextDayScore(pick) }} · 目标 {{ pick.nextDaySignal?.targetSession || '下一交易日' }}</n-tag></div></details>
          </div><n-empty v-else size="small" :description="historyEmptyLabel(item)" />
        </article>
      </div><n-empty v-else description="暂无历史" />
    </n-spin></n-drawer-content></n-drawer>
  </main>
</template>

<style scoped>
.admission-diagnostics{margin:14px 0;padding:14px;border:1px solid var(--sk-border);border-radius:10px;background:var(--sk-surface);font-size:12px;line-height:1.65}.admission-diagnostics>summary{font-size:14px;font-weight:600;cursor:pointer}.admission-diagnostics[open]>summary{margin-bottom:10px}.admission-check-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}.admission-check{display:flex;flex-direction:column;gap:4px;padding:10px;background:var(--sk-surface-2);border-radius:6px;border-left:3px solid #18a058}.admission-check.check-blocked{border-left-color:#d39b36}.admission-check small,.admission-diagnostics>small{color:var(--sk-text-muted)}.admission-check b{font-variant-numeric:tabular-nums}.pick-body .daily-selection-reason{display:block;font-size:13px}.daily-facts{margin:10px 0}
.premarket-plans{margin-bottom:22px;padding:16px;border:1px solid var(--sk-accent);border-radius:12px;background:color-mix(in srgb,var(--sk-surface-2) 45%,transparent)}.premarket-historical{border-color:var(--sk-border);background:var(--sk-surface-2)}.premarket-summary{margin-top:0}.premarket-notice{font-size:13px;line-height:1.7;margin:8px 0 14px}.premarket-historical .premarket-notice{font-weight:600}.premarket-data-gap{padding:10px 12px;border-left:3px solid #d39b36;background:var(--sk-surface);font-size:12px;line-height:1.7;margin:10px 0 16px}.premarket-card{border-top:3px solid var(--sk-accent)!important}.premarket-historical .premarket-card{border-top-color:var(--sk-border)!important}.pick-body>p.premarket-reason{display:block;line-height:1.7;margin:0 0 10px}.premarket-levels{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:10px;background:var(--sk-surface-2);border-radius:6px;margin:10px 0;font-size:12px}.premarket-levels>div{display:flex;justify-content:space-between;gap:8px}.premarket-levels b{font-variant-numeric:tabular-nums}.premarket-footnote{font-size:11px;color:var(--sk-text-muted);line-height:1.6;margin:14px 0 0}.premarket-plans .pick-grid{grid-template-columns:repeat(auto-fit,minmax(min(340px,100%),1fr))}
.picks-loading{display:flex;align-items:center;justify-content:center;gap:12px;min-height:180px;color:var(--sk-text-muted)}
.pick-body>p.next-day-reason,.pick-body>p.quote-coverage{display:block}
.history-channel{margin:10px 0 6px;font-size:11px;color:var(--sk-text-muted)}
.refresh-progress{margin:12px 0;padding:12px;background:var(--sk-surface-2);border-radius:8px;font-size:12px}.refresh-progress>div{display:flex;justify-content:space-between;gap:14px;margin-bottom:8px}.refresh-progress span,.refresh-progress small{color:var(--sk-text-muted)}.refresh-progress small{display:block;margin-top:7px}.adaptive-summary>div{flex-wrap:wrap}
.v11-facts{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:4px 0 12px;padding:10px;background:var(--sk-surface-2);border-radius:6px;font-size:11px}.v11-facts>div{display:flex;flex-direction:column;gap:3px}.v11-facts b{font-weight:500;font-variant-numeric:tabular-nums}.background-switch{white-space:nowrap}
.indicator-highlights{display:flex;flex-wrap:wrap;gap:5px 12px;margin:0 0 8px;font-size:10px;color:var(--sk-text-muted)}.indicator-highlights b{color:var(--sk-text);font-variant-numeric:tabular-nums}.explanation-reasons{padding-left:18px}.explanation-reasons li{margin:7px 0}.legacy-notice{padding:9px 11px;border-left:3px solid var(--sk-accent);background:var(--sk-surface-2)}.insufficient-evidence{margin:18px 0;font-size:12px}.insufficient-evidence>article{padding:10px 0;border-top:1px solid var(--sk-border)}.insufficient-evidence>article>div{display:flex;align-items:center;gap:10px}.insufficient-evidence code{color:var(--sk-text-muted)}.insufficient-evidence .n-button{margin-left:auto}
.quote-coverage{display:flex;flex-wrap:wrap;gap:5px 18px;margin:-4px 0 12px;color:var(--sk-text-muted);font-size:11px;line-height:1.6}.quote-evidence{margin:-4px 0 10px;color:var(--sk-text-muted);font-size:10px;line-height:1.6;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.history-review{margin-top:10px;font-size:11px;line-height:1.6}.observation-table{width:100%;border-collapse:collapse;font-size:10px;font-variant-numeric:tabular-nums}.observation-table th,.observation-table td{padding:5px 3px;text-align:right;border-bottom:1px solid var(--sk-border)}.observation-table th:first-child,.observation-table td:first-child{text-align:left}.observation-table th{color:var(--sk-text-muted);font-weight:500}
.pick-why{display:flex;flex-direction:column;gap:5px;width:100%;padding:0 0 10px;text-align:left;background:none;border:0;cursor:pointer;font:inherit;font-size:11px;line-height:1.6}.pick-why span{color:var(--sk-text)}.pick-why b{font-size:10px;color:var(--sk-accent);font-weight:500}.pick-explanation{font-size:12px;line-height:1.8;color:var(--sk-text)}.pick-explanation h3{font-size:14px;margin:18px 0 8px}.pick-explanation small{color:var(--sk-text-muted);font-size:10px}.pick-explanation details{margin:16px 0;padding-top:10px;border-top:1px solid var(--sk-border)}.pick-explanation summary{cursor:pointer;color:var(--sk-accent)}.usage-steps{padding-left:20px}.usage-steps li{margin:8px 0}.branch-list,.fact-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin:14px 0}.branch-list>div,.fact-grid>div{padding:8px;background:var(--sk-surface-2);border-radius:5px}.branch-list code{color:var(--sk-accent);margin-right:12px}.fact-grid span{display:block;color:var(--sk-text-muted);font-size:10px}.score-row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--sk-border)}.evidence-item{margin:12px 0;padding:10px;background:var(--sk-surface-2);border-radius:6px;overflow-wrap:anywhere}.evidence-item p{margin:6px 0}.evidence-item a{display:block;color:var(--sk-accent)}

.picks-page{min-height:100%;box-sizing:border-box;padding:26px 28px 24px;background:var(--sk-page-bg);color:var(--sk-text)}.picks-page header{display:flex;align-items:center;gap:10px;margin-bottom:14px}.picks-page header>div:first-child{margin-right:auto}.picks-page h2{margin:0 0 4px;font-size:24px}.picks-page header p{margin:0;color:var(--sk-text-muted)}.background-switch{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--sk-text-muted)}.background-state{margin:-4px 0 6px;font-size:11px;color:var(--sk-text-muted)}.sync-alert{margin-bottom:10px}.meta-row{display:flex;flex-wrap:wrap;gap:22px;padding:12px 2px;color:var(--sk-text-muted);font-size:12px}.view-tabs{margin-bottom:10px}.adaptive-summary{display:flex;align-items:center;justify-content:space-between;gap:16px;margin:8px 0 12px;padding:12px;border-left:3px solid var(--sk-accent);background:color-mix(in srgb,var(--sk-surface) 82%,transparent)}.adaptive-summary div{display:flex;flex-direction:column;gap:3px}.adaptive-summary small,.adaptive-summary span{color:var(--sk-text-muted)}.tier-tabs{margin-top:4px}.tier-tab{display:flex;align-items:baseline;gap:8px}.tier-tab small{opacity:.65;font-size:11px}.tier-intro{margin:2px 0 4px;padding:10px 12px;border-left:3px solid var(--sk-accent);border-radius:4px;background:color-mix(in srgb,var(--sk-surface) 82%,transparent);color:var(--sk-text-muted);font-size:12px}.aggressive-note{margin-bottom:12px}.pick-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(350px,1fr));gap:12px}.pick-card{background:var(--sk-surface)!important;border:1px solid var(--sk-border)!important}.pick-card.tier-adaptive{border-top:3px solid var(--sk-accent)!important}.pick-card.tier-conservative{border-top:3px solid #18a058!important}.pick-card.tier-regular{border-top:3px solid #2080f0!important}.pick-card.tier-aggressive{border-top:3px solid #d03050!important}.pick-title{display:grid;grid-template-columns:24px 1fr auto auto;align-items:center;gap:8px}.pick-title>b{color:var(--sk-accent)}.pick-title code{font-size:11px;color:var(--sk-text-muted)}.pick-body p{display:grid;grid-template-columns:76px 1fr;gap:8px;margin:7px 0;font-size:12px;line-height:1.58}.pick-body span{color:var(--sk-text-muted)}.pick-body em{color:#d03050;font-style:normal}.history-list{display:flex;flex-direction:column;gap:12px}.history-run{padding:12px;border:1px solid var(--sk-border);border-radius:8px;background:var(--sk-surface)}.history-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.history-head>div{display:flex;flex-direction:column;gap:3px}.history-head small{color:var(--sk-text-muted)}.history-candidates{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}@media(max-width:850px){.picks-page{padding:16px 14px 24px}.picks-page header{align-items:stretch;flex-direction:column}.picks-page header>div:first-child{margin-right:0}.tier-tab small{display:none}.pick-grid{grid-template-columns:1fr}.pick-title{grid-template-columns:22px 1fr auto}.pick-title code{grid-column:2}.pick-title .n-tag{grid-row:1 / span 2;grid-column:3}.meta-row{gap:8px 16px}.adaptive-summary{align-items:flex-start;flex-direction:column}}
.picks-eyebrow{font-size:10px;letter-spacing:2px;font-weight:700;color:var(--sk-accent);margin-bottom:8px}.picks-page h2{letter-spacing:-.8px}.pick-card{border-radius:12px}.adaptive-summary{border-radius:8px}
.picks-page>header{gap:10px}.picks-page>header>h2{margin:0 auto 0 0;font-size:24px}.picks-meta{display:flex;align-items:flex-start;justify-content:space-between;gap:15px;color:var(--sk-text-muted);font-size:11px;margin:12px 0}.picks-meta details{text-align:right}.picks-method{margin:8px 0 16px}.pick-quote{display:flex;align-items:baseline;gap:13px;padding:5px 0 10px;font-variant-numeric:tabular-nums}.pick-quote strong{font-size:25px;font-weight:550}.pick-quote>span{font-size:13px}.pick-quote .up{color:var(--sk-up)}.pick-quote .down{color:var(--sk-down)}.pick-risk{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.pick-details{padding-top:8px;border-top:1px solid var(--sk-border)}.pick-body .pick-details p{font-size:11px}.adaptive-summary>div{flex-direction:row;align-items:center;gap:12px}
.recommendation-objectives{padding:12px 16px;border:1px solid var(--sk-border);border-radius:8px;margin:10px 0;color:var(--sk-text-muted);font-size:12px;line-height:1.6}.recommendation-objectives p{margin:4px 0}.primary-empty{padding:28px 16px;text-align:center;border:1px dashed var(--sk-border);border-radius:8px;margin:14px 0;color:var(--sk-text-muted)}.first-reference{display:flex;flex-wrap:wrap;gap:8px 14px;padding:8px 0;color:var(--sk-text-muted);font-size:12px}.research-observations{margin-top:22px;padding:14px;border:1px solid var(--sk-border);border-radius:8px}.research-observations>h3{margin:0 0 14px;font-size:14px;font-weight:600;line-height:1.6}.research-observations>.next-day-watch+.next-day-watch{margin-top:20px}.invalidated-recommendations{margin:16px 0;font-size:12px;color:var(--sk-text-muted)}.invalidated-recommendations article{padding:10px;border-top:1px solid var(--sk-border)}
</style>
