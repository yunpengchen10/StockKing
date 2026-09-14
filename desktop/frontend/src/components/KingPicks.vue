<script setup>
import { computed, inject, onBeforeMount, onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Follow, GetEngineStatus, GetKingPicks, GetKingPicksHistory, GetStockKingBackgroundLearning, GetStockKingBackgroundStatus, SetStockKingBackgroundLearning, GetStockKingAIProviders, GetStockKingPreference, SetStockKingPicksAIConfig, ReviewStockKingPicks } from '../../wailsjs/go/main/App'
import { useMessage } from 'naive-ui'
import { toResearchCode } from '../utils/symbol'
import EconomyBudget from './EconomyBudget.vue'
import SafeMarkdown from './SafeMarkdown.vue'
import { explainPick, PICK_BRANCHES } from '../utils/pickExplain.mjs'

const router = useRouter()
const message = useMessage()
const darkTheme = inject('appDarkTheme', ref(true))
const loading = ref(false), refreshError = ref(''), viewingHistory = ref(false), methodVisible = ref(false), selectedPick = ref(null), explanationVisible = ref(false)
const restoring = ref(true)
const aiProviders = ref([]), aiPlatform = ref(''), aiConfigId = ref(''), aiSaving = ref(false)
const aiPlatforms = computed(() => [...new Set(aiProviders.value.map(p => p.name))])
const aiModels = computed(() => aiProviders.value.filter(p => p.name === aiPlatform.value))
async function saveAISelection() {
  if (!aiConfigId.value) return
  aiSaving.value = true
  try { await SetStockKingPicksAIConfig(Number(aiConfigId.value)); message.success('模型已保存') }
  catch (e) { aiConfigId.value = ''; message.error(String(e?.message || e)) }
  finally { aiSaving.value = false }
}
const isDailyRules = computed(() => adaptive.value.modelVersion === 'king-daily-rules-20260908' || adaptive.value.modelVersion?.startsWith('king-local-'))
const explanation = computed(() => explainPick(selectedPick.value || {}))
function showWhy(pick) { selectedPick.value = pick; explanationVisible.value = true }
function openAI(pick) {
  try { sessionStorage.setItem('stock-king:ai-pick:'+toResearchCode(codeOf(pick)), JSON.stringify({ candidate:{...pick,code:codeOf(pick)}, generatedAt:adaptive.value.generatedAt || result.value.generatedAt || '', scanSlot:adaptive.value.scanSlot || '', provenance:'用户选择的精选快照，须与最新行情分开核对' })) }
  catch { message.error('快照暂存失败，请重试'); return }
  router.push({name:'aiAdvice',query:{code:codeOf(pick),name:nameOf(pick),template:'builtin-challenge',pick:'1'}})
}
const engine = ref({ state: 'starting', ready: false })
const maxPerBoard = ref(5)
const result = ref({ tiers: {}, kechuang: [], nonKeChuang: [], generatedAt: '', diagnostics: {} })
const activeTier = ref('regular')
const activeBoard = ref('nonKeChuang')
const viewMode = ref('adaptive')
const backgroundEnabled = ref(true)
const backgroundStatus = ref({ status: 'never_run', updatedAt: '' })
const historyVisible = ref(false)
const historyLoading = ref(false)
const historyItems = ref([])
const snapshotKey = 'stock-king:picks:snapshot:v2.3'
let disposed = false, savedSyncTimer = null, savedSyncInFlight = false, latestReadPromise = null, startupPolls = 0

const tierOptions = [
  { key: 'conservative', label: '保守', note: '低波稳健' },
  { key: 'regular', label: '均衡', note: '均衡多因子' },
  { key: 'aggressive', label: '进攻', note: '1–3 日潜力' },
]

const tierData = computed(() => {
  const data = result.value?.tiers?.[activeTier.value]
  if (data) return data
  if (activeTier.value === 'regular') {
    return { kechuang: result.value?.kechuang || [], nonKeChuang: result.value?.nonKeChuang || result.value?.non_kechuang || [] }
  }
  return { kechuang: [], nonKeChuang: [] }
})
const picks = computed(() => tierData.value?.[activeBoard.value] || [])
const shortfall = computed(() => Number(tierData.value?.shortfall?.[activeBoard.value] || 0))
const adaptive = computed(() => result.value?.adaptive || { candidates: [], changes: {}, calibration: {} })
const adaptivePicks = computed(() => adaptive.value?.candidates || [])
const quoteCoverage = computed(() => adaptive.value?.dataQuality?.quote_coverage)
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
  return /tencent/i.test(label) ? '腾讯' : /sina/i.test(label) ? '新浪' : label || '来源未知'
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
  const value = Number(pick?.nls ?? pick?.tierScore ?? pick?.potentialScore ?? pick?.score ?? pick?.final_score)
  return Number.isFinite(value) ? value.toFixed(1) : '—'
}
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

const reviewLoading=ref(false), reviewResult=ref(null), budgetPanel=ref(null), reviewSnapshot=ref('')
async function reviewPicks(){
 if(reviewLoading.value || !adaptivePicks.value.length)return
 if(!aiConfigId.value){message.warning('请先选择复审平台和模型');return}
 reviewLoading.value=true
 const snapshot=JSON.parse(JSON.stringify(adaptive.value));reviewSnapshot.value=snapshot.generatedAt || ''
 try{reviewResult.value=await ReviewStockKingPicks(crypto.randomUUID(),Number(aiConfigId.value),snapshot)}
 catch(e){message.error(String(e?.message || e))}
 finally{reviewLoading.value=false;budgetPanel.value?.refresh()}
}
async function refresh() {
  if (loading.value || restoring.value) return
  loading.value = true; refreshError.value = ''
  try {
    engine.value = await GetEngineStatus()
    if (!engine.value?.ready) throw new Error(engine.value?.message || 'Daily 研究引擎正在启动')
    const fresh = await GetKingPicks(maxPerBoard.value, true)
    if (fresh?.adaptive?.status === 'unavailable') throw new Error(fresh.adaptive.message || '当日机会未完成')
    result.value=fresh
    viewingHistory.value = false
    persistSnapshot()
    if (result.value?.recommendationSyncError) message.warning(`精选已生成，但推荐记录同步失败：${result.value.recommendationSyncError}`)
  } catch (error) {
    refreshError.value = error?.message || String(error)
    message.error(refreshError.value)
  } finally { loading.value = false }
}

function persistSnapshot() {
  try { localStorage.setItem(snapshotKey, JSON.stringify(result.value)) } catch (_) { /* backend history remains the fallback */ }
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
  try {
    const parsed = JSON.parse(localStorage.getItem(snapshotKey) || 'null')
    if (parsed?.adaptive) result.value = parsed
    return !!parsed?.adaptive
  } catch (_) { return false }
}

function canSyncSavedResults() {
  return !disposed && document.visibilityState !== 'hidden' && !loading.value && !viewingHistory.value && !historyVisible.value && !explanationVisible.value && !reviewLoading.value
}

async function syncSavedResults() {
  if (disposed || savedSyncInFlight) return
  savedSyncInFlight = true
  try {
    if (!canSyncSavedResults()) return
    const status = await GetEngineStatus().catch(() => ({ state: 'unavailable', ready: false }))
    if (disposed) return
    engine.value = status
    if (!status?.ready) { startupPolls++; return }
    startupPolls = 0
    await restoreLatest({ background: true })
    if (!canSyncSavedResults()) return
    const background = await GetStockKingBackgroundStatus().catch(() => null)
    if (background && !disposed) backgroundStatus.value = background
  } finally {
    savedSyncInFlight = false
    if (!disposed) {
      const delay = document.visibilityState !== 'hidden' && !engine.value?.ready && startupPolls < 45 ? 2000 : 30000
      savedSyncTimer = setTimeout(syncSavedResults, delay)
    }
  }
}

async function restoreLatest({ background = false } = {}) {
  if (disposed || (background && !canSyncSavedResults())) return false
  const restored = background ? Boolean(result.value?.adaptive) : readSnapshot()
  if (!background) viewingHistory.value = false
  // Keep the cached snapshot visible while reading saved background results.
  // This read never starts a market scan or an AI request.
  try {
    if (!latestReadPromise) latestReadPromise = GetKingPicksHistory(100).finally(() => { latestReadPromise = null })
    const payload = await latestReadPromise
    if (disposed || (background && !canSyncSavedResults())) return restored
    const latest = (payload?.items || []).filter(isScanHistory)
      .map(historyResult).filter(isRestorableSnapshot)
      .sort((a, b) => snapshotTime(b) - snapshotTime(a))[0]
    if (!latest) return restored
    if (!restored || !isRestorableSnapshot(adaptive.value) || snapshotTime(latest) >= snapshotTime(adaptive.value)) {
      mergeAdaptive(latest)
      persistSnapshot()
    }
    return true
  } catch (_) {
    return restored
  }
}

function historyResult(item) { return item?.result || {} }
function historyCandidates(item) { return historyResult(item)?.candidates || [] }
function isScanHistory(item) { return ['king_live', 'king_0920', 'king_0922', 'king_1030', 'king_1455'].includes(item?.mode) }
function isReviewHistory(item) { return ['king_review', 'king_weekly'].includes(item?.mode) }
function isRestorableSnapshot(snapshot) {
  const status = String(snapshot?.status || '')
  return Array.isArray(snapshot?.candidates) && !['unavailable', 'failed', 'expired', 'cancelled', 'retired_slot'].includes(status) && !status.startsWith('skipped')
}
function snapshotTime(snapshot) { return Date.parse(snapshot?.generatedAt || snapshot?.generated_at || snapshot?.as_of || '') || 0 }
function historySlotLabel(item) {
  if (item?.mode === 'king_review') return '收盘复盘'
  if (item?.mode === 'king_weekly') return '学习检查'
  return historyResult(item).scanSlot || String(item?.mode || '').replace('king_', '')
}
function historyStatusLabel(item) {
  const status = historyResult(item).status || item?.status || ''
  return { unavailable: '扫描未完成', failed: '扫描失败', expired: '时段已过期', cancelled: '已取消', retired_slot: '旧时点已停用', skipped_non_trading_day: '非交易日已跳过', completed_observations: '观察结果', no_candidates_with_coverage_limits: '未形成候选', audit_only: '已记录' }[status] || (String(status).startsWith('skipped') ? '已跳过' : status)
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

function openRecommendationHistory() {
  router.push({ name: 'research', query: { name: '股票推荐记录' } })
}

async function toggleBackground(value) {
  try {
    await SetStockKingBackgroundLearning(value)
    backgroundEnabled.value = value
    backgroundStatus.value = await GetStockKingBackgroundStatus()
    message.success(value ? '后台自学习已开启' : '后台自学习已暂停；计划任务会静默跳过')
  } catch (error) {
    backgroundEnabled.value = !value
    message.error(error?.message || String(error))
  }
}

onBeforeMount(async () => {
  readSnapshot()
  await syncSavedResults()
  if (disposed) return
  aiProviders.value = await GetStockKingAIProviders().catch(() => [])
  aiConfigId.value = String(await GetStockKingPreference('picks.ai.configId').catch(() => '') || '')
  const configured = aiProviders.value.find(p => String(p.id) === aiConfigId.value)
  aiPlatform.value = configured?.name || ''
  if (!configured) aiConfigId.value = ''
  backgroundEnabled.value = await GetStockKingBackgroundLearning().catch(() => true)
  restoring.value = false
})
onBeforeUnmount(() => {
  disposed = true
  if (savedSyncTimer !== null) clearTimeout(savedSyncTimer)
})
</script>

<template>
  <main class="picks-page" :class="{ dark: darkTheme }">
    <header>
      <h2>精选</h2>
      <n-button secondary @click="methodVisible=true">如何使用</n-button>
      <n-input-number v-if="viewMode==='classic'" v-model:value="maxPerBoard" :min="5" :max="50" style="width:112px"><template #suffix>只 / 组</template></n-input-number>
      <div class="background-switch"><n-switch :value="backgroundEnabled" @update:value="toggleBackground" /><span title="控制后台定时扫描与历史反馈学习，不代表实时行情推送">后台学习</span></div>
      <n-button secondary @click="openHistory">历史</n-button>
      <n-button secondary @click="openRecommendationHistory">推荐记录</n-button>
      <n-button type="primary" :loading="loading" :disabled="restoring" title="重新扫描当日机会与策略分组" @click="refresh">刷新全部</n-button>
    </header>
    <n-alert v-if="!engine.ready || refreshError || viewingHistory" type="warning" :show-icon="false"><span :title="refreshError || engine.message || engine.state">{{ viewingHistory ? '历史快照' : refreshError ? '更新失败 · 当前为旧结果' : '引擎未就绪 · 就绪后自动读取已存结果' }}</span><n-button v-if="viewingHistory" size="tiny" text @click="restoreLatest">返回当前</n-button></n-alert>
    <div class="picks-meta"><span>{{ result.asOfDate || result.as_of_date || '待生成' }}</span><details class="sk-help-details"><summary>扫描信息</summary><p>生成 {{ result.generatedAt || result.generated_at || '—' }} · 版本 {{ result.methodologyVersion || '兼容旧榜' }}</p><p>后台学习 {{ backgroundEnabled ? '开启' : '暂停' }} · {{ backgroundStatus.status || '未运行' }} {{ backgroundStatus.updatedAt }}</p><p>每轮扫描保留当时证据，可在历史中回看。</p></details></div>
    <n-tabs v-model:value="viewMode" type="segment" animated class="view-tabs"><n-tab-pane name="adaptive" tab="当日机会" /><n-tab-pane name="classic" tab="策略分组" /></n-tabs>

    <template v-if="viewMode === 'adaptive'">
      <div class="picks-ai-config">
        <label>平台<select v-model="aiPlatform" :disabled="loading || aiSaving" @change="aiConfigId=''" aria-label="当日机会AI平台"><option value="">请选择</option><option v-for="name in aiPlatforms" :key="name">{{ name }}</option></select></label>
        <label>模型<select v-model="aiConfigId" :disabled="loading || aiSaving || !aiPlatform" @change="saveAISelection" aria-label="当日机会AI模型"><option value="">请选择</option><option v-for="p in aiModels" :value="String(p.id)" :key="p.id" :disabled="!p.ready">{{ p.model }}</option></select></label>
        <n-button :loading="reviewLoading" :disabled="!adaptivePicks.length || loading" @click="reviewPicks">AI 复审</n-button><span>免费行情 + 本地计算 · 点击复审才调用 AI</span>
      </div>
      <EconomyBudget ref="budgetPanel" />
      <details v-if="reviewResult" open class="sk-help-details"><summary>AI 风险审查 · {{reviewResult.cacheHit ? '复用结果' : '新请求'}} · {{reviewResult.analyzedAt}}</summary><p>原候选快照 {{reviewSnapshot}} · token {{reviewResult.usage?.total_tokens ?? '未知'}} · 费用估算 {{reviewResult.estimatedCost ?? '未配置单价或用量未知'}}</p><SafeMarkdown :model-value="reviewResult.markdown" /></details>
      <n-alert v-if="adaptive.message" type="warning" :show-icon="false">{{ adaptive.message }}</n-alert>
      <div class="adaptive-summary"><div><b>Top 5</b><small>{{ formatTime(adaptive.generatedAt || result.generatedAt) }}</small></div><span>{{ loading ? '更新中…' : `新增 ${adaptive.changes?.added?.length || 0} · 移除 ${adaptive.changes?.removed?.length || 0}` }}</span></div>
      <div v-if="quoteCoverageSummary || scheduleSummary" class="quote-coverage"><span v-if="quoteCoverageSummary" title="新鲜表示通过本轮报价时效核验，不代表已成交。">{{ quoteCoverageSummary }}</span><span v-if="scheduleSummary" :title="`北京时间 · 目标 ${adaptive.delivery.target_at} · 实际 ${adaptive.delivery.decision_at || '未记录'}`">{{ scheduleSummary }}</span></div>
      <details v-if="isDailyRules" class="sk-help-details picks-method"><summary>规则与覆盖</summary><p>主板非ST · T至T+5 · 最多5只 · 本地5日排序与独立风险检查</p><p>行情 {{ adaptive.dataQuality?.snapshot_count ?? '未知' }} 只，主板 {{ adaptive.dataQuality?.mainboard_count ?? '未知' }} 只，深研 {{ adaptive.dataQuality?.deep_research_count ?? '未知' }} 只；历史 {{ adaptive.historicalPrior?.availableTradingDays ?? 0 }}/20 日。</p><p>{{ adaptive.marketSummary }}</p><p v-for="(call,index) in adaptive.aiAudit" :key="index">{{ call.provider }} / {{ call.model }} · 请求 High · {{ call.completed_at }}；服务端内部强度未知。</p><p>成交与送达链路未联合核验时只显示观察，不计作可执行命中。</p></details>
      <details v-else class="sk-help-details picks-method"><summary>旧版历史口径</summary><p>{{ modelMix }} · {{ adaptive.modelVersion }}。旧分快照保留用于回看；下一次刷新采用本地5日筛选。</p></details>
      <n-spin :show="loading">
        <section v-if="adaptivePicks.length" class="pick-grid">
          <n-card v-for="pick in adaptivePicks" :key="codeOf(pick)" size="small" :bordered="false" class="pick-card tier-adaptive">
            <template #header><div class="pick-title"><b>{{ pick.rank }}</b><strong>{{ nameOf(pick) }}</strong><code>{{ codeOf(pick) }}</code><n-tag size="small" :bordered="false" type="info" :title="pick.modelBranch || pick.model">{{ pick.stateLabel || `${scoreOf(pick)} 分` }}</n-tag></div></template>
            <div class="pick-body">
              <button class="pick-why" @click="showWhy(pick)"><span>{{ explainPick(pick).reason }}</span><b>为何入选 ↗</b></button>
              <div class="pick-quote"><strong>{{ pick.referencePrice == null ? '—' : `¥${Number(pick.referencePrice).toFixed(2)}` }}</strong><span :class="Number(pick.currentChange) > 0 ? 'up' : Number(pick.currentChange) < 0 ? 'down' : ''">{{ pick.currentChange == null ? '—' : `${Number(pick.currentChange).toFixed(2)}%` }}</span></div>
              <div v-if="pick.quote" class="quote-evidence" title="北京时间；延迟按采集时间减原始源时间计算。此处为本轮保存的行情快照。">{{ quoteEvidence(pick) }}</div>
              <p v-if="pick.risks?.length" class="pick-risk" :title="pick.risks.join('；')"><span>风险</span>{{ pick.risks[0] }}</p>
              <details class="sk-help-details pick-details"><summary>依据与风险</summary>
                <p v-if="pick.thesis"><span>空间</span>{{ pick.thesis }}</p>
                <p v-if="pick.thesis"><span>计划</span>{{ pick.buyRange || '待确认区间' }} · {{ pick.noChase || '未提供不追条件' }}</p>
                <p v-if="pick.quote"><span>源时间</span>{{ pick.quote.provider_timestamp || pick.quote.source_time || '未知' }}</p>
                <p v-if="pick.quote?.fetched_at"><span>采集时间</span>{{ pick.quote.fetched_at }}</p>
                <p v-if="pick.transition"><span>状态</span>{{ pick.transition }}</p>
                <p v-if="pick.data_quality?.gaps"><span>缺口</span>{{ pick.data_quality.gaps.join('；') }}</p>
                <p v-if="pick.pool"><span>候选池</span>{{ pick.pool }}池 · {{ pick.positioning }}</p>
                <p><span>VWAP</span>{{ pick.vwapState || '未知' }}</p>
                <p><span>历史案例</span><template v-if="historicalRateVisible(pick)">{{ pick.similarHistoryFeedback.rateSampleCount }} 个 · 3日内触板 {{ percent(pick.similarHistoryFeedback.touchWithin3dRate) }}<br/>描述性区间 {{ (pick.similarHistoryFeedback.touchWithin3dInterval95 || []).map(v => percent(v)).join(' – ') }}；未消除案例相关性，不代表当前股票上涨概率。</template><template v-else>{{ pick.similarHistoryFeedback?.message || '历史频率不可用' }}</template></p>
                <p v-if="pick.auctionConfirmation && pick.auctionConfirmation !== 'not_applicable'"><span>竞价</span>{{ pick.auctionConfirmation }}</p>
                <p><span>触发</span>{{ (pick.upgradeConditions || pick.triggers || []).join('；') }}</p>
                <p><span>失效</span>{{ (pick.invalidationConditions || pick.invalidations || []).join('；') }}</p>
                <p><span>风险</span>{{ (pick.risks || []).join('；') }}</p>
              </details>
            </div>
            <template #footer><n-space justify="end"><n-button size="small" secondary @click="addWatch(pick)">自选</n-button><n-button size="small" secondary @click="openKline(pick)">图表</n-button><n-button size="small" secondary @click="openAI(pick)">AI 复核</n-button></n-space></template>
          </n-card>
        </section>
        <n-empty v-else :description="adaptive.message || (result.generatedAt ? '本轮无候选 · 请查看规则与覆盖' : '点击刷新全部生成候选')" style="padding:56px 0" />
      </n-spin>
    </template>

    <template v-else>
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
            <template #footer><n-space justify="end"><n-button size="small" secondary @click="addWatch(pick)">自选</n-button><n-button size="small" secondary @click="openKline(pick)">图表</n-button><n-button size="small" type="primary" @click="openAI(pick)">AI 复核</n-button></n-space></template>
          </n-card>
        </section>
        <n-empty v-else :description="shortfall ? `合格候选不足，缺额 ${shortfall} 只` : '暂无合格候选'" style="padding:56px 0" />
      </n-spin>
    </template>
    <n-drawer v-model:show="methodVisible" :width="500" placement="right"><n-drawer-content title="精选 · 方法与使用" closable><div class="pick-explanation">
      <ol class="usage-steps"><li><b>刷新全部</b> · 等引擎就绪，检查列表生成时间。</li><li><b>为何入选</b> · 看实际特征、评分和来源。</li><li><b>图表 / AI 复核</b> · 核对当前条件与反证。</li></ol>
      <h3>当日机会</h3><p>免费获取公开行情，由本地5日模型筛选主板非ST候选，再核对腾讯、新浪的原始报价时间。最多5只，不凑数；点击“AI复审”才调用所选平台。无成交与送达证据时仅为条件观察。</p>
      <details><summary>排序依据</summary><p>资金、当日板块与地位、剩余空间、催化预期差、历史股性。无固定权重和席位配额，不把相关量价证据重复计分。全市场源覆盖可能不足，源时间未知不能当作当前事实。</p></details>
      <div class="branch-list"><div v-for="(item,key) in PICK_BRANCHES" :key="key"><code>{{ key }}</code><span>{{ item.name }}</span></div></div>
      <h3>策略分组</h3><p>保守、均衡、进攻采用不同目标，分数不能跨组比较。模型未通过发布门槛时显示规则观察；AI只能解释，不能让失败模型变成已验证。</p>
      <details><summary>后台学习与历史频率</summary><p>进入精选先显示上次快照，引擎就绪后读取最新已存结果；停留页面时每30秒检查一次，查看历史时暂停更新。只有“刷新全部”重新扫描。收盘复盘和学习记录可在“历史”中查看。</p><p>历史频率需20个已成熟官方交易日、60日行情和30个有效相似案例。未积累足够数据仍可使用候选和AI研究，不能把研究分当成胜率。</p></details>
    </div></n-drawer-content></n-drawer>
    <n-drawer v-model:show="explanationVisible" :width="520" placement="right"><n-drawer-content :title="`${nameOf(selectedPick)} · 为何入选`" closable><div class="pick-explanation">
      <h3>{{ explanation.title }}</h3><p>{{ explanation.logic }}</p><small>快照 {{ formatTime(adaptive.generatedAt || result.generatedAt) }} · 行情来源 {{ explanation.source }}</small>
      <div class="fact-grid"><div v-for="fact in explanation.facts" :key="fact.label"><span>{{ fact.label }}</span><b>{{ fact.value }}</b></div></div>
      <details v-if="!selectedPick?.thesis"><summary>{{ explanation.scoreLabel }} · {{ scoreOf(selectedPick) }}（非概率）</summary><p>{{ explanation.formula }}</p><div v-for="row in explanation.modelScores" :key="row.key" class="score-row"><span>{{ row.key }} · {{ row.label }}</span><b>{{ row.value }}</b></div></details>
      <h3>证据与来源</h3><p v-if="!explanation.evidence.length">该快照未保留来源；刷新全部后再核对。</p><article v-for="(item,index) in explanation.evidence" :key="index" class="evidence-item"><b>{{ item.title || item.type || '来源' }}</b><p>{{ item.summary || '无摘要' }}</p><small>提取于 {{ formatTime(item.fetched_at) }}（非原始发布时间）</small><a v-if="item.url" :href="item.url" target="_blank" rel="noopener noreferrer">查看来源 ↗</a></article>
      <details><summary>条件与数据缺口</summary><p>确认：{{ (selectedPick?.upgradeConditions || selectedPick?.triggers || []).join('；') || '未提供' }}</p><p>失效：{{ (selectedPick?.invalidationConditions || selectedPick?.invalidations || []).join('；') || '未提供' }}</p><p>风险：{{ (selectedPick?.risks || degradedOf(selectedPick) || []).join('；') || '未提供' }}</p><p>60日历史：{{ explanation.quality.history60d === true ? '有' : '不足 / 未提供' }}；尾盘分钟数据：{{ explanation.quality.minuteTail === true ? '有' : '未提供' }}。文本摘要需回原文核验。</p></details>
      <n-space style="margin-top:18px"><n-button secondary @click="openKline(selectedPick)">图表</n-button><n-button type="primary" @click="openAI(selectedPick)">AI 复核</n-button></n-space>
    </div></n-drawer-content></n-drawer>
    <n-drawer v-model:show="historyVisible" :width="520" placement="right"><n-drawer-content title="精选历史" closable><n-spin :show="historyLoading">
      <div v-if="historyItems.length" class="history-list">
        <article v-for="item in historyItems" :key="item.run_id" class="history-run">
          <div class="history-head"><div><b>{{ formatTime(historyResult(item).generatedAt || item.as_of) }}</b><small>{{ historySlotLabel(item) }}<template v-if="!isReviewHistory(item)"> · {{ historyCandidates(item).length }} 只</template> · {{ historyStatusLabel(item) }}</small></div><n-button v-if="isScanHistory(item)" size="small" secondary @click="restoreHistory(item)">查看</n-button></div>
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
          <div v-else-if="historyCandidates(item).length" class="history-candidates"><n-tag v-for="pick in historyCandidates(item)" :key="`${item.run_id}-${codeOf(pick)}`" size="small" :bordered="false">#{{ pick.rank }} {{ nameOf(pick) }} · {{ pick.modelBranch || pick.model }} · {{ scoreOf(pick) }}</n-tag></div><n-empty v-else size="small" :description="historyEmptyLabel(item)" />
        </article>
      </div><n-empty v-else description="暂无历史" />
    </n-spin></n-drawer-content></n-drawer>
  </main>
</template>

<style scoped>
.quote-coverage{display:flex;flex-wrap:wrap;gap:5px 18px;margin:-4px 0 12px;color:var(--sk-text-muted);font-size:11px;line-height:1.6}.quote-evidence{margin:-4px 0 10px;color:var(--sk-text-muted);font-size:10px;line-height:1.6;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.history-review{margin-top:10px;font-size:11px;line-height:1.6}.observation-table{width:100%;border-collapse:collapse;font-size:10px;font-variant-numeric:tabular-nums}.observation-table th,.observation-table td{padding:5px 3px;text-align:right;border-bottom:1px solid var(--sk-border)}.observation-table th:first-child,.observation-table td:first-child{text-align:left}.observation-table th{color:var(--sk-text-muted);font-weight:500}
.pick-why{display:flex;flex-direction:column;gap:5px;width:100%;padding:0 0 10px;text-align:left;background:none;border:0;cursor:pointer;font:inherit;font-size:11px;line-height:1.6}.pick-why span{color:var(--sk-text)}.pick-why b{font-size:10px;color:var(--sk-accent);font-weight:500}.pick-explanation{font-size:12px;line-height:1.8;color:var(--sk-text)}.pick-explanation h3{font-size:14px;margin:18px 0 8px}.pick-explanation small{color:var(--sk-text-muted);font-size:10px}.pick-explanation details{margin:16px 0;padding-top:10px;border-top:1px solid var(--sk-border)}.pick-explanation summary{cursor:pointer;color:var(--sk-accent)}.usage-steps{padding-left:20px}.usage-steps li{margin:8px 0}.branch-list,.fact-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin:14px 0}.branch-list>div,.fact-grid>div{padding:8px;background:var(--sk-surface-2);border-radius:5px}.branch-list code{color:var(--sk-accent);margin-right:12px}.fact-grid span{display:block;color:var(--sk-text-muted);font-size:10px}.score-row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--sk-border)}.evidence-item{margin:12px 0;padding:10px;background:var(--sk-surface-2);border-radius:6px;overflow-wrap:anywhere}.evidence-item p{margin:6px 0}.evidence-item a{display:block;color:var(--sk-accent)}

.picks-page{min-height:100%;box-sizing:border-box;padding:26px 28px 24px;background:var(--sk-page-bg);color:var(--sk-text)}.picks-page header{display:flex;align-items:center;gap:10px;margin-bottom:14px}.picks-page header>div:first-child{margin-right:auto}.picks-page h2{margin:0 0 4px;font-size:24px}.picks-page header p{margin:0;color:var(--sk-text-muted)}.background-switch{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--sk-text-muted)}.background-state{margin:-4px 0 6px;font-size:11px;color:var(--sk-text-muted)}.sync-alert{margin-bottom:10px}.meta-row{display:flex;flex-wrap:wrap;gap:22px;padding:12px 2px;color:var(--sk-text-muted);font-size:12px}.view-tabs{margin-bottom:10px}.adaptive-summary{display:flex;align-items:center;justify-content:space-between;gap:16px;margin:8px 0 12px;padding:12px;border-left:3px solid var(--sk-accent);background:color-mix(in srgb,var(--sk-surface) 82%,transparent)}.adaptive-summary div{display:flex;flex-direction:column;gap:3px}.adaptive-summary small,.adaptive-summary span{color:var(--sk-text-muted)}.tier-tabs{margin-top:4px}.tier-tab{display:flex;align-items:baseline;gap:8px}.tier-tab small{opacity:.65;font-size:11px}.tier-intro{margin:2px 0 4px;padding:10px 12px;border-left:3px solid var(--sk-accent);border-radius:4px;background:color-mix(in srgb,var(--sk-surface) 82%,transparent);color:var(--sk-text-muted);font-size:12px}.aggressive-note{margin-bottom:12px}.pick-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(350px,1fr));gap:12px}.pick-card{background:var(--sk-surface)!important;border:1px solid var(--sk-border)!important}.pick-card.tier-adaptive{border-top:3px solid var(--sk-accent)!important}.pick-card.tier-conservative{border-top:3px solid #18a058!important}.pick-card.tier-regular{border-top:3px solid #2080f0!important}.pick-card.tier-aggressive{border-top:3px solid #d03050!important}.pick-title{display:grid;grid-template-columns:24px 1fr auto auto;align-items:center;gap:8px}.pick-title>b{color:var(--sk-accent)}.pick-title code{font-size:11px;color:var(--sk-text-muted)}.pick-body p{display:grid;grid-template-columns:76px 1fr;gap:8px;margin:7px 0;font-size:12px;line-height:1.58}.pick-body span{color:var(--sk-text-muted)}.pick-body em{color:#d03050;font-style:normal}.history-list{display:flex;flex-direction:column;gap:12px}.history-run{padding:12px;border:1px solid var(--sk-border);border-radius:8px;background:var(--sk-surface)}.history-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.history-head>div{display:flex;flex-direction:column;gap:3px}.history-head small{color:var(--sk-text-muted)}.history-candidates{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}@media(max-width:850px){.picks-page{padding:16px 14px 24px}.picks-page header{align-items:stretch;flex-direction:column}.picks-page header>div:first-child{margin-right:0}.tier-tab small{display:none}.pick-grid{grid-template-columns:1fr}.pick-title{grid-template-columns:22px 1fr auto}.pick-title code{grid-column:2}.pick-title .n-tag{grid-row:1 / span 2;grid-column:3}.meta-row{gap:8px 16px}.adaptive-summary{align-items:flex-start;flex-direction:column}}
.picks-eyebrow{font-size:10px;letter-spacing:2px;font-weight:700;color:var(--sk-accent);margin-bottom:8px}.picks-page h2{letter-spacing:-.8px}.pick-card{border-radius:12px}.adaptive-summary{border-radius:8px}
.picks-page>header{gap:10px}.picks-page>header>h2{margin:0 auto 0 0;font-size:24px}.picks-meta{display:flex;align-items:flex-start;justify-content:space-between;gap:15px;color:var(--sk-text-muted);font-size:11px;margin:12px 0}.picks-meta details{text-align:right}.picks-method{margin:8px 0 16px}.pick-quote{display:flex;align-items:baseline;gap:13px;padding:5px 0 10px;font-variant-numeric:tabular-nums}.pick-quote strong{font-size:25px;font-weight:550}.pick-quote>span{font-size:13px}.pick-quote .up{color:var(--sk-up)}.pick-quote .down{color:var(--sk-down)}.pick-risk{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.pick-details{padding-top:8px;border-top:1px solid var(--sk-border)}.pick-body .pick-details p{font-size:11px}.adaptive-summary>div{flex-direction:row;align-items:center;gap:12px}
</style>
