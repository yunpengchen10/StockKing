<script setup>
import { computed, inject, onActivated, onBeforeMount, onBeforeUnmount, onDeactivated, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Follow, GetEngineStatus, GetKingPicks, GetKingPicksHistory, GetDisplayedKingPicks, StartKingPicksRefresh, GetKingPicksRefreshTask, GetStockKingBackgroundLearning, GetStockKingBackgroundStatus, SetStockKingBackgroundLearning, GetStockKingAutoRecommendations, SetStockKingAutoRecommendations } from '../../wailsjs/go/main/App'
import { useMessage } from 'naive-ui'
import { toResearchCode } from '../utils/symbol'
import PickIndicatorEvidence from './PickIndicatorEvidence.vue'
import PicksLedger from './PicksLedger.vue'
import { explainPick, PICK_BRANCHES, isEvidenceRuleVersion } from '../utils/pickExplain.mjs'
import { createPicksRefreshController, readDisplayedPicks, saveDisplayedPicks, selectDisplayedPicks } from '../utils/picksRefresh.mjs'
import { nextDayQueueLabel, nextDayTouchLabel, savedRecommendationSummary } from '../utils/pickLedger.mjs'

const router = useRouter()
const message = useMessage()
const darkTheme = inject('appDarkTheme', ref(true))
const loading = ref(false), refreshError = ref(''), viewingHistory = ref(false), methodVisible = ref(false), selectedPick = ref(null), explanationVisible = ref(false)
const restoring = ref(true)
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
const continuationVisible = ref(false)
watch(viewMode, mode => {
  if (ledgerModes.includes(mode) && !visitedLedgerModes.value.includes(mode)) visitedLedgerModes.value.push(mode)
})
const backgroundEnabled = ref(true)
const recommendationsEnabled = ref(true)
const backgroundStatus = ref({ status: 'never_run', updatedAt: '' })
const historyVisible = ref(false)
const historyLoading = ref(false)
const historyItems = ref([])
let disposed = false, savedSyncTimer = null, savedSyncInFlight = false, displayReadPromise = null, displayLoaded = false, displayRevision = 0, startupPolls = 0
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
    displayLoaded = true
    displayedResult.value = fresh
    result.value = fresh
    viewingHistory.value = false
    saveDisplayedPicks(snapshotStorage, fresh)
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
const snapshotIsOld = computed(() => {
  const raw = adaptive.value.generatedAt || result.value.generatedAt
  const stamp = raw ? new Date(raw) : null
  if (!stamp || Number.isNaN(stamp.getTime())) return false
  const options = { timeZone: 'Asia/Shanghai' }
  return stamp.toLocaleDateString('sv-SE', options) < new Date().toLocaleDateString('sv-SE', options)
})
const adaptivePicks = computed(() => adaptive.value?.profileCandidates?.[precisionProfile.value] || adaptive.value?.candidates || [])
const nextDayWatchlist = computed(() => Array.isArray(adaptive.value?.nextDayWatchlist) ? adaptive.value.nextDayWatchlist : [])
const nextDayContinuationWatchlist = computed(() => Array.isArray(adaptive.value?.nextDayContinuationWatchlist) ? adaptive.value.nextDayContinuationWatchlist : [])
const savedRecommendations = computed(() => savedRecommendationSummary(adaptive.value))
const nextDayQueues = computed(() => [
  ...(nextDayWatchlist.value.length || savedRecommendations.value.splitQueues ? [{ key: savedRecommendations.value.splitQueues ? 'first_board' : 'legacy', picks: nextDayWatchlist.value, title: savedRecommendations.value.splitQueues ? '未触板潜伏 · 次日研究推荐' : '次日观察 · 历史未分队列' }] : []),
  ...(nextDayContinuationWatchlist.value.length ? [{ key: 'continuation', picks: nextDayContinuationWatchlist.value, title: '已触板延续 · 辅助观察' }] : []),
])
const precisionWatchlist = computed(() => (adaptive.value?.precisionWatchlist || []).filter(p => !p.precisionDecision?.profiles?.[precisionProfile.value]?.entryEligible))
const adaptiveCards = computed(() => adaptivePicks.value.map((pick, index) => ({ pick: { ...pick, rank: index + 1 }, detail: explainPick(pick) })))
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
  if (parsed) { displayedResult.value = parsed; result.value = parsed; classicResult.value = parsed }
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

async function syncSavedResults() {
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
    if (!displayLoaded && !loading.value) await restoreDisplayed()
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
  if (disposed || loading.value) return
  const revision = displayRevision
  try {
    if (!displayReadPromise) displayReadPromise = GetDisplayedKingPicks().finally(() => { displayReadPromise = null })
    const payload = await displayReadPromise
    if (disposed || revision !== displayRevision) return
    displayLoaded = true
    const saved = selectDisplayedPicks(displayedResult.value, payload)
    if (saved) {
      displayedResult.value = saved
      if (!viewingHistory.value) result.value = saved
      saveDisplayedPicks(snapshotStorage, saved)
    }
    refreshController.resume(payload?.refreshTask)
  } catch (_) { /* Keep the local display and retry once the engine becomes ready. */ }
}
function returnToDisplayed() {
  viewingHistory.value = false
  result.value = displayedResult.value || { adaptive: { candidates: [] }, generatedAt: '' }
}

function historyResult(item) { return item?.result || {} }
function historyCandidates(item) { return historyResult(item)?.candidates || [] }
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
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('zh-CN', { hour12: false })
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
  if (disposed) return
  restoring.value = false
  refreshController.resume()
  void syncSavedResults()
  await preferences
})
onBeforeUnmount(() => {
  disposed = true
  refreshController.dispose()
  if (savedSyncTimer !== null) clearTimeout(savedSyncTimer)
})
onActivated(() => {
  pageActive.value = true
  if (!restoring.value && savedSyncTimer === null) void syncSavedResults()
})
onDeactivated(() => {
  pageActive.value = false
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
    <div class="picks-meta"><span>{{ pageResult.asOfDate || pageResult.as_of_date || '待生成' }}</span><details class="sk-help-details"><summary>扫描信息</summary><p>生成 {{ pageResult.generatedAt || pageResult.generated_at || '—' }} · 版本 {{ pageResult.methodologyVersion || '兼容旧榜' }}</p><p>后台学习 {{ backgroundEnabled ? '开启' : '暂停' }} · {{ backgroundStatus.status || '未运行' }} {{ backgroundStatus.updatedAt }}</p><p>每轮扫描保留当时证据，可在历史中回看。</p></details></div>
    <n-tabs v-model:value="viewMode" type="line" animated class="view-tabs"><n-tab-pane name="adaptive" tab="本地精选" /><n-tab-pane name="records" tab="推荐记录" /><n-tab-pane name="reviews" tab="延后复盘" /><n-tab-pane name="learning" tab="学习状态" /><n-tab-pane name="classic" tab="策略分组" /></n-tabs>

    <section v-show="viewMode === 'adaptive'">
      <n-tabs v-if="adaptive.profileCandidates && !isV11" v-model:value="precisionProfile" type="segment">
        <n-tab-pane name="conservative" tab="稳健 · 财务与回撤" />
        <n-tab-pane name="regular" tab="均衡 · 趋势与买点" />
        <n-tab-pane name="aggressive" tab="激进 · 突破与延续" />
      </n-tabs>
      <p v-if="adaptive.profileCandidates && !isV11" class="quote-coverage">三档入场纪律正在验证；盘前只观察，盘中重新确认。</p>
      <details v-if="precisionWatchlist.length" class="sk-help-details"><summary>等待条件的股票（{{ precisionWatchlist.length }}）</summary>
        <p v-for="pick in precisionWatchlist" :key="codeOf(pick)">{{ nameOf(pick) }}：{{ pick.precisionDecision?.profiles?.[precisionProfile]?.reasons?.join('；') }}</p>
      </details>
      <p class="quote-coverage">软件行情 · 本地计算 · 沪深主板非ST。次日冲板观察与交易条件候选分别保留，点击“刷新扫描”才重新选股。</p>
      <n-alert v-if="adaptive.message" type="warning" :show-icon="false">{{ adaptive.message }}</n-alert>
      <div class="adaptive-summary saved-recommendations"><div><b>{{ result.adaptive ? savedRecommendations.saved ? `已保存研究推荐 · ${savedRecommendations.saved} 只` : '已保存扫描 · 暂无研究推荐' : '本地精选' }}</b><n-tag v-if="snapshotIsOld" size="small" type="warning" :bordered="false">历史结果</n-tag><small>生成时间 {{ formatTime(adaptive.generatedAt || result.generatedAt) }}</small><small v-if="adaptive.run_id || adaptive.runId">扫描编号 {{ adaptive.run_id || adaptive.runId }}</small></div><n-button v-if="result.adaptive" size="small" secondary @click="openRecommendationRecords">查看最新推荐记录</n-button><span v-if="loading">扫描中 · 暂时保留原列表</span></div>
      <p v-if="adaptive.nextDayResearch" class="quote-coverage">已保存研究推荐按股票去重：{{ savedRecommendations.splitQueues ? '未触板潜伏' : '次日观察' }} {{ savedRecommendations.primary }} 只 · 交易条件候选 {{ savedRecommendations.execution }} 只<template v-if="savedRecommendations.splitQueues">；已触板延续 {{ savedRecommendations.continuation }} 只单列辅助观察，不计入主推荐。</template>研究观察保留依据和缺口，不代表已通过交易条件。</p>
      <p class="quote-coverage">本轮主板初筛 {{ researchCoverage.universe }} 只 · 进入深研 {{ researchCoverage.queued }} 只 · 完成深研 {{ researchCoverage.completed }} 只 · <template v-if="adaptive.nextDayResearch">研究推荐 {{ savedRecommendations.saved }} 只</template><template v-else>本档入选 {{ adaptivePicks.length }} 只</template></p>
      <p v-if="snapshotIsOld" class="quote-coverage">以上为已保存的历史结果，价格与证据截至标注的生成时间；刷新成功后才会更新。</p>
      <div v-if="quoteCoverageSummary || scheduleSummary" class="quote-coverage"><span v-if="quoteCoverageSummary" title="新鲜表示通过本轮报价时效核验，不代表已成交。">{{ quoteCoverageSummary }}</span><span v-if="scheduleSummary" :title="`北京时间 · 目标 ${adaptive.delivery.target_at} · 实际 ${adaptive.delivery.decision_at || '未记录'}`">{{ scheduleSummary }}</span></div>
      <div v-if="adaptive.dataQuality?.independent_evidence" class="quote-coverage">
        <span v-if="adaptive.dataQuality?.minute_coverage">有效分钟证据：{{ adaptive.dataQuality.minute_coverage.usable }}/{{ adaptive.dataQuality.minute_coverage.requested }} 只</span>
        <span>20日同刻基准齐全：{{ adaptive.dataQuality.independent_evidence.history_20d_ready }}/{{ adaptive.dataQuality.research_count }} 只</span>
        <span>行业联动确认：{{ adaptive.dataQuality.independent_evidence.sector_confirmed }} 只</span>
        <span>分钟资金数据：{{ adaptive.dataQuality.independent_evidence.fund_source_available }} 只</span>
      </div>
      <details v-if="isV11" class="sk-help-details picks-method"><summary>规则与覆盖</summary><p>{{ researchCoverage.description }}</p><p>行情 {{ adaptive.dataQuality?.snapshot_count ?? '未知' }} 只；深研完成表示已逐只核验，缺失和过期证据仍单独记录，不代表全部证据齐全。</p><p>{{ adaptive.marketSummary }}</p><p>算法 {{ adaptive.scoreVersion || adaptive.recommendationLogicVersion || '以单只候选记录为准' }}。</p><p>{{ adaptive.algorithm?.missingFactorPolicy || '缺失处理以该次记录的算法版本为准。' }}</p><p>{{ adaptive.algorithm?.formula || '评分公式以该次记录为准。' }}</p><p>{{ adaptive.algorithm?.profilePolicy }}</p><p>参数为待验证初值。分数用于研究排序，数据覆盖不是胜率；市场情绪调节不表示实际仓位或成交。</p></details>
      <details v-else class="sk-help-details picks-method"><summary>旧版历史口径</summary><p>{{ modelMix }} · {{ adaptive.modelVersion }}。旧分快照保留用于回看；下一次刷新采用当前规则与证据筛选。</p></details>
      <section v-for="queue in nextDayQueues" :key="queue.key" class="next-day-watch" :class="queue.key === 'continuation' ? 'next-day-continuation' : 'next-day-primary'">
        <n-alert v-if="adaptive.nextDayResearch?.retrospectiveProjection" type="warning" :show-icon="false">历史证据重排 · 非当时推荐。{{ adaptive.nextDayResearch.projectionMeaning || '按当时已保存的证据重新排序，不作为当时已经发出的推荐或新规则命中率。' }}</n-alert>
        <div class="adaptive-summary"><div><b>{{ queue.title }} · {{ queue.picks.length }} 只</b><small>目标交易日 {{ queue.picks[0]?.nextDaySignal?.targetSession || '下一交易日' }}</small></div><n-button v-if="queue.key === 'continuation'" size="small" secondary @click="continuationVisible = !continuationVisible">{{ continuationVisible ? '收起延续观察' : '展开延续观察' }}</n-button><n-tag v-else size="small" type="warning" :bordered="false">研究观察</n-tag></div>
        <p class="quote-coverage">{{ queue.key === 'first_board' ? '优先查看扫描时未触板的潜伏线索；具体涨幅、距模式价与昨日形态依据见每只记录。' : queue.key === 'continuation' ? '这些股票扫描时已触及当日模式限价，单独观察延续，不与未触板潜伏混排。' : '旧版名单保留原样，未事后划分潜伏或延续队列。' }}排序分不是涨停概率，交易条件仍待确认。</p>
        <div v-show="queue.key !== 'continuation' || continuationVisible" class="pick-grid">
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
                <p><span>证据日期</span>{{ pick.nextDaySignal?.signalSession || '未记录' }} · 目标 {{ pick.nextDaySignal?.targetSession || '下一交易日' }}</p>
                <p><span>交易条件</span>此通道仅用于观察，未给出买入许可；开盘、量价和风险条件仍需重新确认。</p>
              </details>
            </div>
            <template #footer><n-space justify="end"><n-button size="small" secondary @click="addWatch(pick)">自选</n-button><n-button size="small" type="primary" @click="openKline(pick)">图表</n-button></n-space></template>
          </n-card>
        </div>
        <n-empty v-if="queue.key === 'first_board' && !queue.picks.length" description="本轮暂无满足潜伏条件的研究推荐。已触板延续单列在下方，不补入潜伏名单。" style="padding:24px 0" />
      </section>
      <div v-if="nextDayQueues.length && adaptivePicks.length" class="adaptive-summary"><b>交易条件候选 · {{ adaptivePicks.length }} 只</b><small>按本轮已核验的证据与交易门槛筛选</small></div>
      <n-spin :show="loading && !adaptivePicks.length">
        <section v-if="adaptivePicks.length" class="pick-grid">
          <n-card v-for="{ pick, detail } in adaptiveCards" :key="codeOf(pick)" size="small" :bordered="false" class="pick-card tier-adaptive">
            <template #header><div class="pick-title"><b>{{ pick.rank }}</b><strong>{{ nameOf(pick) }}</strong><code>{{ codeOf(pick) }}</code><n-tag size="small" :bordered="false" type="info" :title="pick.modelBranch || pick.model">{{ pick.stateLabel || `${scoreOf(pick)} 分` }}</n-tag></div></template>
            <div class="pick-body">
              <button class="pick-why" @click="showWhy(pick)"><b>为什么入选 · {{ detail.title }}</b><span>{{ detail.reason }}</span><b>查看全部指标与条件 ↗</b></button>
              <div v-if="detail.indicators.some(row => row.value !== '未取得')" class="indicator-highlights"><span v-for="row in detail.indicators.filter(row => row.value !== '未取得').slice(0, 3)" :key="row.key">{{ row.label }} <b>{{ row.value }}</b></span></div>
              <div class="pick-quote"><strong>{{ pick.referencePrice == null ? '—' : `¥${Number(pick.referencePrice).toFixed(2)}` }}</strong><span :class="Number(pick.currentChange) > 0 ? 'up' : Number(pick.currentChange) < 0 ? 'down' : ''">{{ pick.currentChange == null ? '—' : `${Number(pick.currentChange).toFixed(2)}%` }}</span></div>
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
                <p v-if="pick.precisionDecision"><span>入场检查</span>{{ pick.precisionDecision.profiles[precisionProfile]?.reasons?.join('；') }}</p>
                <p v-if="pick.precisionDecision"><span>财务与事件</span>{{ pick.precisionDecision.sourceContext?.gaps?.join('；') || '已取得财报、预告与解禁覆盖；按记录时点核验' }}</p>
                <p v-if="pick.precisionDecision"><span>价格位置</span>MA5乖离 {{ pick.precisionDecision.metrics.biasMa5Pct?.toFixed(2) ?? '未知' }}% · ATR距离 {{ pick.precisionDecision.metrics.atrExtension?.toFixed(2) ?? '未知' }} 倍</p>
                <p><span>研究排序分</span>{{ scoreOf(pick) }} · 不是胜率或收益预测</p>
                <p v-if="pick.dataEligibility"><span>数据资格</span>{{ { formal: '满足历史与关键因子要求', observation: '历史、关键因子或时效未满足，仅观察', insufficient: '历史或关键证据不足' }[pick.dataEligibility.status] || '待核验' }} · {{ pick.scoreStage || '阶段待确认' }}</p>
                <p v-for="(packet, key) in pick.researchFactors || {}" :key="key"><span>{{ { C: '事件催化', G: '历史股性', E: '市场情绪', H: '题材扩散（影子）' }[key] || key }}</span>{{ { observed: '已记录', insufficient: '样本不足', unavailable: '未取得' }[packet.status] || '未取得' }} · {{ packet.gaps?.join('；') || packet.source || '来源见指标证据' }}</p>
                <p v-if="pick.marketRegime"><span>情绪调节</span>{{ { weak: '偏弱', hot: '过热', normal: '正常', unknown: '证据不足，保留基础分' }[pick.marketRegime.state] || '待核验' }} · 研究分系数 {{ pick.marketRegime.multiplier ?? '未取得' }}，不代表实际仓位</p>
                <template v-if="isV11Pick(pick)"><p><span>主升概率</span>{{ calibrationValue(pick, 'mainRiseProbability') }}</p><p><span>预计MFE</span>1日 {{ calibrationValue(pick, 'expectedMFE1') }} · 3日 {{ calibrationValue(pick, 'expectedMFE3') }} · 5日 {{ calibrationValue(pick, 'expectedMFE5') }}</p><p><span>预计MAE</span>1日 {{ calibrationValue(pick, 'expectedMAE1') }} · 3日 {{ calibrationValue(pick, 'expectedMAE3') }} · 5日 {{ calibrationValue(pick, 'expectedMAE5') }}</p></template>
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
        <p v-else-if="nextDayQueues.length" class="quote-coverage">交易条件候选 0 只；{{ savedRecommendations.saved ? '本轮研究推荐保存在上方，交易证据与风险门槛仍待确认。' : '研究队列与交易条件分别核验，本轮未以延续观察补足主推荐。' }}</p>
        <n-empty v-else :description="adaptive.message || (result.generatedAt ? '本轮无候选 · 请查看规则与覆盖' : '尚未保存推荐，请点击“刷新扫描”。盘前当日分钟证据可能尚未形成。')" style="padding:36px 0" />
        <details v-if="insufficientCards.length" class="sk-help-details insufficient-evidence"><summary>交易条件待补证据 · {{ insufficientCards.length }} 只</summary><p>这些股票未通过交易候选条件，其中有研究线索的可同时保留在上方观察队列；查看具体缺口。</p><article v-for="{ pick, detail } in insufficientCards" :key="codeOf(pick)"><div><b>{{ nameOf(pick) }}</b><code>{{ codeOf(pick) }}</code><n-button size="tiny" secondary @click="showWhy(pick, true)">查看交易缺口</n-button></div><p>{{ detail.risks.join('；') || detail.reason }}</p></article></details>
      </n-spin>
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
      <h3>本地精选 V1.2（当前规则）</h3><p>筛选沪深主板非ST，每档每轮最多5只。突破和修复使用启动分，延续使用主升分，再计入风险与市场状态调节。每轮重新核验队列，候选及未入选对照一同记录；已保存的V1.1记录仍显示其原版本公式。</p>
      <h3>次日冲板研究 · 潜伏与延续分开</h3><p>主推荐默认展示最多5只未触板潜伏：本轮规则要求扫描时涨幅在−3%至5%、当日尚未触及模式限价且仍有至少4%距离，并核验日线、量价与昨日形态；昨日证据缺失会列为缺口，不声称已确认首板。最多5只已触板延续单列为辅助观察，默认折叠，不用于补足潜伏名单或主推荐数量。两队列分别排序，保存当时依据、目标日与缺口。</p><p>已保存研究推荐数量为潜伏与交易候选按股票去重后的并集。研究观察不代表通过交易条件；排序分未校准为涨停概率，下一交易日仍需核验。旧版名单保留原算法记录，不事后分队列。</p>
      <details><summary>入选依据与指标</summary><p>量或价的同刻历史不足5日时不生成评分；5～19日仅供观察。正式候选须量价历史均达到20日、M/V/W/B关键因子有效且分钟证据新鲜，再通过本档风险门槛。数据覆盖不是胜率。缺失因子保持为空、得分贡献为零，固定权重不重新分配；缺失风险按该项上界计入惩罚。</p><p>事件、历史股性和换手参与规则分；市场情绪仅向下调节研究分，未知时保留基础分，不代表实际仓位。题材扩散只作影子记录，新闻社交舆情尚未接入。研究分不是收益预测；未经校准的MFE、MAE及概率保持待校准。风险系数0.2等参数均为待验证初值，旧记录没有保存的证据不事后补写。</p></details>
      <details><summary>旧版历史分支名称</summary><div class="branch-list"><div v-for="(item,key) in PICK_BRANCHES" :key="key"><code>{{ key }}</code><span>{{ item.name }}</span></div></div></details>
      <h3>策略分组</h3><p>V1.2三档共用研究排序，分别执行乖离、ATR、结构盈亏比、业绩与解禁门槛。事件风险覆盖未知时仅观察；已知负面业绩预告或超限解禁独立否决。激进档在分钟突破已核验时允许上方压力与盈亏比未知，但不生成目标价；已知盈亏比仍须达标。旧版不同策略的分值不能直接比较，旧契约模型不会用于新规则。</p>
      <details><summary>定时扫描与学习</summary><p>北京时间09:20盘前观察；09:40、09:55、10:30、14:55独立扫描；15:30归档与到期复盘；周五15:45本地训练。自动推荐和自动学习分别控制。错过时点保留遗漏记录，手动刷新不计入正式训练。</p><p>默认至少120个成熟交易日、1000条有效样本，并满足训练和校准分段要求后才检验模型。独立留出段至少60个信号日、50笔完成交易；训练与验证按实际标签结束时间隔离，至少留5个交易日间隔。扣费收益与相对优势的置信下界须为正，回撤不恶化，再从模型实际创建后开展至少20个交易日前瞻影子验证。失败标签日与空仓日保留，未达要求继续使用规则。</p><p>页面读取已保存的展示快照。“刷新扫描”成功后才更新，刷新期间或失败时仍显示原推荐；新一轮无合格候选时明确显示空结果。模拟成交使用下一完整分钟入场，满足T+1后尝试期限收盘退出；遇跌停、停牌或容量不足则继续持仓，逐日标价并等待首个有完整证据的可卖分钟。缺路径不跳过，未成交独立记录；价格观察与模拟收益均不是用户真实交易。</p></details>
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
        <p>{{ isV11Pick(selectedPick) ? '缺失可选因子保留为空，已取得因子按可用权重计算；报价与交易状态仍须通过检查。' : '此处保留历史快照当时的核验口径。' }}资金指标为供应商分类估算。</p>
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
          <div class="history-head"><div><b>{{ formatTime(historyResult(item).generatedAt || item.as_of) }}</b><small>{{ historySlotLabel(item) }}<template v-if="!isReviewHistory(item)"><template v-if="hasNextDayHistory(item)"> · 研究推荐 {{ historySavedSummary(item).saved }} 只 · {{ historySavedSummary(item).splitQueues ? '潜伏' : '次日观察' }} {{ historyNextDayCandidates(item).length }} 只 · 交易候选 {{ historyCandidates(item).length }} 只<template v-if="historySavedSummary(item).splitQueues"> · 延续 {{ historyContinuationCandidates(item).length }} 只</template></template><template v-else> · {{ historyCandidates(item).length }} 只</template></template> · {{ historyStatusLabel(item) }}</small></div><n-button v-if="isScanHistory(item)" size="small" secondary @click="restoreHistory(item)">查看</n-button></div>
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
          <div v-else-if="historyCandidates(item).length || historyNextDayCandidates(item).length || historyContinuationCandidates(item).length">
            <template v-if="historyNextDayCandidates(item).length"><p class="history-channel">{{ historySavedSummary(item).splitQueues ? '未触板潜伏' : '历史次日观察（未分队列）' }} · 分数用于研究排序，不是涨停概率</p><div class="history-candidates"><n-tag v-for="(pick, index) in historyNextDayCandidates(item)" :key="`${item.run_id}-next-day-${codeOf(pick)}-${index}`" size="small" type="warning" :bordered="false">#{{ pick.rank ?? index + 1 }} {{ nameOf(pick) }} · 观察分 {{ nextDayScore(pick) }} · 目标 {{ pick.nextDaySignal?.targetSession || '下一交易日' }}</n-tag></div></template>
            <template v-if="historyCandidates(item).length"><p v-if="hasNextDayHistory(item)" class="history-channel">交易条件候选</p><div class="history-candidates"><n-tag v-for="(pick, index) in historyCandidates(item)" :key="`${item.run_id}-entry-${codeOf(pick)}-${index}`" size="small" :bordered="false">#{{ pick.rank ?? index + 1 }} {{ nameOf(pick) }} · {{ pick.modelBranch || pick.model }} · {{ scoreOf(pick) }}</n-tag></div></template>
            <details v-if="historyContinuationCandidates(item).length" class="sk-help-details"><summary>已触板延续 · {{ historyContinuationCandidates(item).length }} 只辅助观察</summary><div class="history-candidates"><n-tag v-for="(pick, index) in historyContinuationCandidates(item)" :key="`${item.run_id}-continuation-${codeOf(pick)}-${index}`" size="small" :bordered="false">#{{ pick.rank ?? index + 1 }} {{ nameOf(pick) }} · 延续分 {{ nextDayScore(pick) }} · 目标 {{ pick.nextDaySignal?.targetSession || '下一交易日' }}</n-tag></div></details>
          </div><n-empty v-else size="small" :description="historyEmptyLabel(item)" />
        </article>
      </div><n-empty v-else description="暂无历史" />
    </n-spin></n-drawer-content></n-drawer>
  </main>
</template>

<style scoped>
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
</style>
