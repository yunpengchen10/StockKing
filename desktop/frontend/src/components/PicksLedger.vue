<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { GetStockKingRecommendationHistory, GetStockKingDelayedReviews, GetStockKingLearningState, GetStockKingRecordQuotes } from '../../wailsjs/go/main/App'
import { groupSignals, ledgerPercent, ledgerPrice, ledgerSigned, priceObservationPercent, statusLabel, reviewStatusLabel, reviewReasonLabel, visibleReviews, reviewProgress, learningEffect, reviewReturn, learningProgress, recordQuoteCode, signalPriceComparison, createRecordQuoteLoader, nextDaySignalOf, isNextDaySelection, signalSelectionLabel, nextDayPatternValue, nextDayQueueLabel, nextDayTouchLabel } from '../utils/pickLedger.mjs'
import { withRecordReadTimeout } from '../utils/recordRead.mjs'
import { learningTimeline, learningTaskLabel } from '../utils/learningStatus.mjs'
import { entryExecutionSummary, dailyObservationRows, dailyObservationCategory, dailyObservationStatus, settlementHealthSummary } from '../utils/recommendationQuality.mjs'
import { firstRecommendationEvidence, primaryRecommendationState, primaryAdmissionReasons, isDailySelection } from '../utils/pickLedger.mjs'

// A separate, retained instance owns each ledger tab. Activation updates quotes only.
const props = defineProps({ mode: { type: String, required: true }, active: { type: Boolean, default: true }, refreshToken: { type: Number, default: 0 } })
defineEmits(['open-chart'])
const date = ref(''), symbol = ref(''), version = ref(''), includeControls = ref(false)
const loading = ref(false), error = ref(''), payload = ref({ items: [] })
const visibleGroupCount = ref(30), expandedSignals = ref(new Set())
const queriedAt = ref(''), reviewLearning = ref({}), learningError = ref('')
const quotesLoading = ref(false), quotesError = ref(''), quotes = ref({})
let requestId = 0, disposed = false, initialized = false, quoteTimer = null, quoteUpdateGeneration = 0
const groups = computed(() => groupSignals(payload.value, { includeControls: includeControls.value }))
const displayGroups = computed(() => groups.value.slice(0, visibleGroupCount.value).map(group => ({
  ...group,
  signals: group.signals.map(signal => ({ ...signal, daily: isDailySelection(signal), nextDay: isNextDaySelection(signal) ? nextDaySignalOf(signal) : null, priceComparison: signalPriceComparison(signal, quotes.value[recordQuoteCode(signal.code)]) })),
})))
const quoteLoader = createRecordQuoteLoader({
  fetchQuotes: GetStockKingRecordQuotes,
  onState(state) { quotesLoading.value = state.loading; quotesError.value = state.error; quotes.value = state.quotes },
})
const reviews = computed(() => visibleReviews(payload.value.items, { includeControls: includeControls.value }))
const nextDayReviews = computed(() => visibleReviews(payload.value.nextDayReview?.items, { includeControls: includeControls.value }))
const nextDaySummaryGroups = computed(() => (payload.value.nextDayReview?.summary?.groups || []).filter(group => includeControls.value || group.selected === true))
const dailyReview = computed(() => payload.value.dailyObservationReview)
const dailyRows = computed(() => dailyObservationRows(dailyReview.value, includeControls.value))
const dailyGroups = computed(() => (dailyReview.value?.groups || []).filter(group => includeControls.value || group.first_category !== 'unselected_control'))
const reviewSummaries = computed(() => [
  { label: '次日价格模式', ...reviewProgress(nextDayReviews.value) },
  { label: '交易模拟', ...reviewProgress(reviews.value) },
])
const learning = computed(() => payload.value || {})
const learningTimes = computed(() => learningTimeline(learning.value))
const learningTasks = computed(() => Array.isArray(learning.value.taskStates) ? learning.value.taskStates : [])
const effectiveLearning = computed(() => learningEffect(props.mode === 'reviews' ? reviewLearning.value : learning.value))
const minimumDays = computed(() => learning.value.minimumMatureDays ?? 120)
const minimumSamples = computed(() => learning.value.minimumValidSamples ?? 1000)
const gates = computed(() => {
  const source = learning.value.gates || {}
  return Array.isArray(source) ? source.map((gate, index) => ({ key: gate.name || gate.key || index, ...gate })) : Object.entries(source).map(([key, value]) => ({ key, ...(value && typeof value === 'object' ? value : { value }) }))
})
const gateLabels = { holdout:'样本外检验', shadow:'前瞻影子验证', liveRolling:'当前版本滚动检验', sampleSize: '成熟样本', sufficient_samples: '成熟样本', samples: '成熟样本', chronologicalSplit: '时间划分', purgeDays: '边界隔离天数', netReturnLower95: '扣费收益95%下界', excessReturnLower95: '相对规则收益95%下界', drawdownNotWorse: '回撤未恶化', shadowComplete: '影子验证完成' }
function time(value) {
  if (!value) return '未记录'
  const stamp = Date.parse(value)
  return Number.isFinite(stamp) ? new Date(stamp).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : String(value)
}
function text(value) { return Array.isArray(value) ? value.join('；') : value && typeof value === 'object' ? JSON.stringify(value) : value || '未记录' }
function gateValue(gate) {
  if (gate.reason || gate.message) return gate.reason || gate.message
  if (typeof gate.passed === 'boolean') return gate.passed ? '通过' : '未通过'
  if (typeof gate.value === 'boolean') return gate.value ? '通过' : '未通过'
  return gate.value ?? gate.status ?? '待检验'
}
function candidate(signal) { return signal.snapshot || signal.features?.candidate || signal.candidate || signal.features || {} }
function factors(signal) { return candidate(signal).factorScores || signal.factorScores || {} }
function researchScore(value) { return value != null && value !== '' && Number.isFinite(Number(value)) ? Number(value).toFixed(1) : '未记录' }
function signalKey(signal) { return signal.signalId || `${signal.runId}-${signal.decisionAt}` }
function toggleSignal(signal, event) {
  const expanded = new Set(expandedSignals.value)
  if (event.target.open) expanded.add(signalKey(signal))
  else expanded.delete(signalKey(signal))
  expandedSignals.value = expanded
}
function readRecord(request) {
  // Browser timers require Window as their receiver. Wrappers keep these calls
  // global when the timeout helper invokes them through its timer adapter.
  return withRecordReadTimeout(request, 20000, {
    setTimeout: (callback, delay) => setTimeout(callback, delay),
    clearTimeout: timer => clearTimeout(timer),
  })
}
function refreshQuotes() {
  if (!disposed && props.active && props.mode === 'records' && !loading.value) return quoteLoader.load(groups.value.map(group => group.code))
}
function stopQuoteUpdates() {
  quoteUpdateGeneration++
  if (quoteTimer !== null) clearTimeout(quoteTimer)
  quoteTimer = null
  quoteLoader.cancel()
}
async function updateQuotes() {
  if (disposed || !props.active || props.mode !== 'records') return
  const generation = ++quoteUpdateGeneration
  if (quoteTimer !== null) clearTimeout(quoteTimer)
  quoteTimer = null
  if (document.visibilityState !== 'hidden') await refreshQuotes()
  if (!disposed && props.active && generation === quoteUpdateGeneration) quoteTimer = setTimeout(updateQuotes, 30000)
}
async function refresh() {
  initialized = true
  const current = ++requestId
  stopQuoteUpdates()
  loading.value = true; error.value = ''
  try {
    const args = [date.value.trim(), symbol.value.trim(), version.value.trim()]
    let result
    if (props.mode === 'reviews') {
      const [reviewResult, learningResult] = await Promise.allSettled([
        readRecord(GetStockKingDelayedReviews(...args)), readRecord(Promise.resolve().then(() => GetStockKingLearningState())),
      ])
      if (!disposed && current === requestId) {
        reviewLearning.value = learningResult.status === 'fulfilled' ? learningResult.value || {} : {}
        learningError.value = learningResult.status === 'rejected' ? learningResult.reason?.message || String(learningResult.reason) : ''
      }
      if (reviewResult.status === 'rejected') throw reviewResult.reason
      result = reviewResult.value
    } else result = await readRecord(props.mode === 'learning' ? GetStockKingLearningState() : GetStockKingRecommendationHistory(...args, includeControls.value))
    if (!disposed && current === requestId) { payload.value = result || { items: [] }; queriedAt.value = new Date().toISOString(); visibleGroupCount.value = 30 }
  } catch (failure) { if (!disposed && current === requestId) error.value = failure?.message || String(failure) }
  finally { if (!disposed && current === requestId) loading.value = false }
  if (!disposed && current === requestId) void updateQuotes()
}
watch(() => props.active, active => {
  if (!active) { stopQuoteUpdates(); return }
  if (!initialized) void refresh()
  else void updateQuotes()
}, { immediate: true })
watch(includeControls, () => { if (props.mode === 'records' && initialized) void refresh(); else void refreshQuotes() })
watch(() => props.refreshToken, () => {
  if (props.mode !== 'records') return
  date.value = ''; symbol.value = ''; version.value = ''
  void refresh()
})
onBeforeUnmount(() => { disposed = true; requestId++; stopQuoteUpdates(); quoteLoader.dispose() })
</script>

<template>
  <section class="ledger">
    <form class="ledger-filters" @submit.prevent="refresh">
      <template v-if="mode !== 'learning'">
        <label>信号日期<input v-model="date" type="date" aria-label="信号日期" /></label>
        <label>股票代码<n-input v-model:value="symbol" clearable placeholder="全部股票" style="width:145px" /></label>
        <label>算法版本<n-input v-model:value="version" clearable placeholder="全部版本" style="width:205px" /></label>
      </template>
      <n-button attr-type="submit" secondary :loading="loading">{{ mode === 'learning' ? '刷新学习状态' : '查询记录' }}</n-button>
      <n-button v-if="mode === 'records'" attr-type="button" secondary :loading="quotesLoading" :disabled="loading || !groups.length" @click="refreshQuotes">刷新报价</n-button>
      <n-checkbox v-if="mode !== 'learning'" v-model:checked="includeControls">包括未入选对照样本</n-checkbox>
    </form>
    <n-alert v-if="error" type="error" :show-icon="false">读取失败：{{ error }}。请重试；当前内容未更新。</n-alert>
    <n-spin :show="loading">
      <template v-if="mode === 'records'">
        <p class="ledger-note">默认显示已落盘的主推荐、失效记录与两类研究观察；按信号编号保留当时评分、目标日和队列。首次主推荐时间与参考价沿用已保存字段，不随刷新重置；研究观察不代表通过准入。</p>
        <p class="ledger-note">记录、筛选和展开位置保留，点击“查询记录”或切换对照样本时读取名单。可见时每30秒更新报价；参考盈亏按标注行情时间比较，未计费用，不会重新选股。</p>
        <p v-if="quotesLoading" class="ledger-note" role="status">正在取得最新报价，已载入的历史记录可继续查看…</p>
        <n-alert v-if="quotesError" type="warning" :show-icon="false" class="quote-alert">报价提示：{{ quotesError }}</n-alert>
        <div v-if="groups.length" class="ledger-groups">
          <article v-for="group in displayGroups" :key="group.key" class="ledger-group">
            <header><div><b>{{ group.name }}</b><code>{{ group.code }}</code><span>{{ group.date }} · {{ group.signals.length }} 轮信号</span></div><n-button size="small" secondary @click="$emit('open-chart', group)">图表</n-button></header>
            <small>算法 {{ group.modelVersion || '原记录未保存' }}</small>
            <details v-for="signal in group.signals" :key="signalKey(signal)" :open="expandedSignals.has(signalKey(signal))" class="signal-row" @toggle="toggleSignal(signal, $event)">
              <summary><span>{{ time(signal.decisionAt) }} · {{ signal.slot || '即时' }}</span><n-tag size="small" :type="signal.daily || signal.selected ? 'info' : signal.nextDay ? 'warning' : 'default'">{{ signalSelectionLabel(signal) }}</n-tag><n-tag v-if="signal.selected && signal.nextDay" size="small" type="warning">{{ nextDayQueueLabel(signal.nextDay) }}</n-tag><span>{{ signal.official ? '定时扫描' : '手动观察' }}</span><span>{{ statusLabel(signal.reviewStatus) }}</span><span v-if="signal.nextDay">目标 {{ signal.nextDay.targetSession || '未记录' }} · 冲板观察分 {{ researchScore(signal.nextDay.score) }}</span>
                <span class="signal-price-change"><span>{{ signal.daily ? '日线参考' : signal.nextDay ? '观察参考' : signal.selected ? '精选参考' : '对照参考' }} ¥{{ ledgerPrice(signal.priceComparison.baseline) }} → 最新 ¥{{ ledgerPrice(signal.priceComparison.latest) }}</span><b v-if="signal.priceComparison.available" :class="signal.priceComparison.direction">参考盈亏 {{ ledgerSigned(signal.priceComparison.delta) }} 元/股（{{ ledgerSigned(signal.priceComparison.percent * 100) }}%）</b><span v-else class="comparison-unavailable">参考盈亏 — · {{ signal.priceComparison.reason }}</span><small>行情时间 {{ time(signal.priceComparison.sourceTime) }}<template v-if="signal.priceComparison.source"> · {{ signal.priceComparison.source }}</template></small></span>
              </summary>
              <template v-if="expandedSignals.has(signalKey(signal))">
              <dl><dt>信号编号</dt><dd>{{ signal.signalId || signal.signal_id || '未记录' }}</dd><dt>信号行情时间</dt><dd>{{ time(signal.sourceTime) }}</dd><dt>信号参考价</dt><dd>{{ ledgerPrice(signal.signalPrice) }}</dd><dt>最新报价</dt><dd>{{ ledgerPrice(signal.priceComparison.latest) }} · 行情时间 {{ time(signal.priceComparison.sourceTime) }}</dd><dt>每股参考盈亏</dt><dd v-if="signal.priceComparison.available" :class="signal.priceComparison.direction">{{ ledgerSigned(signal.priceComparison.delta) }} 元/股 · {{ ledgerSigned(signal.priceComparison.percent * 100) }}%</dd><dd v-else>— · {{ signal.priceComparison.reason }}</dd><template v-if="!signal.daily"><dt>首次可成交价</dt><dd>{{ ledgerPrice(signal.firstTradablePrice) }}</dd></template>
                <template v-if="signal.daily"><dt>日线推荐状态</dt><dd>{{ primaryRecommendationState(candidate(signal)) }}</dd><dt>首次日线推荐时间</dt><dd>{{ time(firstRecommendationEvidence(candidate(signal)).time) }}</dd><dt>首次日线参考价</dt><dd>{{ ledgerPrice(firstRecommendationEvidence(candidate(signal)).price) }}</dd><dt>日K基准</dt><dd>{{ candidate(signal).baselineAsOf || '未记录' }} · {{ candidate(signal).baselineSource || '来源未记录' }}</dd><dt>观察目标</dt><dd>下一交易日（T+1）</dd><dt>证据缺口</dt><dd>{{ text(candidate(signal).dataGaps) }}</dd></template>
                <template v-else-if="candidate(signal).primaryAdmission || candidate(signal).currentSignalState"><dt>主推荐状态</dt><dd>{{ primaryRecommendationState(candidate(signal)) }}</dd><dt>首次主推荐时间</dt><dd>{{ time(firstRecommendationEvidence(candidate(signal)).time) }}</dd><dt>首次主推荐参考价</dt><dd>{{ ledgerPrice(firstRecommendationEvidence(candidate(signal)).price) }}</dd><dt>最新准入核验</dt><dd>{{ text(primaryAdmissionReasons([...(candidate(signal).primaryAdmission?.missingReasons || []), ...(candidate(signal).primaryAdmission?.reasons || [])])) }}</dd></template>
                <template v-if="signal.nextDay"><dt>观察队列</dt><dd>{{ nextDayQueueLabel(signal.nextDay) }} · 扫描时{{ nextDayTouchLabel(signal.nextDay) }}</dd><dt>目标交易日</dt><dd>{{ signal.nextDay.targetSession || '未记录' }}</dd><dt>冲板观察分</dt><dd>{{ researchScore(signal.nextDay.score) }} · {{ signal.nextDay.scoreMeaning || '研究排序，不是涨停概率' }}</dd><dt>观察规则版本</dt><dd>{{ signal.nextDay.version || '未记录' }}</dd><dt>观察依据</dt><dd>{{ text(signal.nextDay.reasons) }}</dd><dt>待确认缺口</dt><dd>{{ text(signal.nextDay.gaps) }}</dd></template>
                <template v-else><dt>当轮排序</dt><dd>{{ signal.rank ?? candidate(signal).rank ?? '未记录' }}</dd><dt>当轮排序版本</dt><dd>{{ (signal.daily ? candidate(signal).dailySignal?.version : candidate(signal).learningRankVersion) || '原记录未保存' }}</dd><dt>入选原因</dt><dd>{{ text(signal.reason || candidate(signal).selectionReasons) }}</dd></template>
                <dt>风险</dt><dd>{{ text(candidate(signal).riskReasons || candidate(signal).risks || signal.risks) }}</dd><dt>触发条件</dt><dd>{{ text(signal.nextDay?.trigger || candidate(signal).triggerConditions || candidate(signal).entryPlan?.trigger) }}</dd><dt>失效条件</dt><dd>{{ text(signal.nextDay?.invalidation || candidate(signal).invalidationConditions || candidate(signal).entryPlan?.invalidations) }}</dd></dl>
              <p v-if="candidate(signal).entryExecution" class="ledger-note">{{ entryExecutionSummary(candidate(signal).entryExecution).label }}。{{ entryExecutionSummary(candidate(signal).entryExecution).detail }} {{ entryExecutionSummary(candidate(signal).entryExecution).meaning }}</p>
              <div v-if="!signal.daily && !signal.nextDay && Object.keys(factors(signal)).length" class="factor-list"><span v-for="(value, key) in factors(signal)" :key="key">{{ key }} <b>{{ value == null ? '未取得' : Number(value).toFixed(1) }}</b></span></div>
              </template>
            </details>
          </article>
        </div>
        <n-button v-if="groups.length > visibleGroupCount" secondary @click="visibleGroupCount += 30">继续显示 {{ Math.min(30, groups.length - visibleGroupCount) }} 组 · 共 {{ groups.length }} 组</n-button>
        <n-empty v-if="!groups.length" :description="error ? '记录暂不可用' : '此范围暂无已保存记录'" />
      </template>

      <template v-else-if="mode === 'reviews'">
        <p class="ledger-note">本页读取已保存的复盘结果；“查询记录”不会执行复盘或训练。上次查询 {{ time(queriedAt) }}，实际检查时间见每条记录。</p>
        <n-alert :type="effectiveLearning.type" :show-icon="false" class="review-learning"><b>{{ effectiveLearning.label }}</b><p>{{ effectiveLearning.detail }}</p><p v-if="learningError">学习状态读取失败：{{ learningError }}</p><p v-else-if="reviewLearning.reason">{{ reviewLearning.reason }}</p></n-alert>
        <div class="review-progress"><article v-for="summary in reviewSummaries" :key="summary.label"><b>{{ summary.label }} · 本次展示 {{ summary.total }} 条</b><p>有结论 {{ summary.concluded }} · 等待/未结算 {{ summary.waiting }} · 到期待执行 {{ summary.notRun }} · 证据待补 {{ summary.incomplete }} · 执行失败 {{ summary.failed }}<template v-if="summary.other"> · 其他状态 {{ summary.other }}</template></p></article></div>
        <p class="ledger-note">{{ includeControls ? '当前包含未入选对照样本' : '默认仅显示当时入选的交易候选与次日观察' }}。以上仅统计本次展示的信号与期限，同一股票可有多条；有结论包含未成交，不能作为推荐成功率。</p>
        <section v-if="dailyReview" class="next-day-reviews">
          <h3>日线与进攻观察 · 独立 T+1 / T+3 / T+5 复盘</h3>
          <p class="ledger-note">{{ settlementHealthSummary(dailyReview.health) }}。结算状态覆盖全部日线观察；下方名单按查询条件展示。查询只读取结果，不执行补评或训练。</p>
          <n-alert v-if="dailyReview.health?.overdueCount > 0 || dailyReview.health?.lastError" type="warning" :show-icon="false">存在到期未完成的结算，后台会从检查点重试。{{ dailyReview.health?.lastError ? `最近错误：${dailyReview.health.lastError}` : '' }}</n-alert>
          <div v-if="dailyGroups.length" class="review-progress"><article v-for="group in dailyGroups" :key="`${group.trading_day}-${group.task_id}-${group.task_version}-${group.first_category}-${group.horizon}`"><b>{{ group.trading_day }} · {{ dailyObservationCategory(group.first_category) }} · T+{{ group.horizon }}</b><p>{{ group.task_version }} · 有效收盘覆盖 {{ group.covered }}/{{ group.signals }} · 上涨 {{ group.up }} · 下跌 {{ group.down }} · 持平 {{ group.flat }}</p><small>缺失不计成功或失败；首次参考价固定，跨版本可能包含同一股票。</small></article></div>
          <div v-if="dailyRows.length" class="review-table-wrap"><table class="review-table"><thead><tr><th>股票 / 类别</th><th>信号日 / 版本</th><th>期限 / 目标日</th><th>证据状态</th><th>首次参考价</th><th>参考价至收盘变化</th><th>最近检查</th></tr></thead><tbody><tr v-for="row in dailyRows" :key="`${row.observationId}-${row.horizon}`"><td><button @click="$emit('open-chart', { code: row.code, name: row.name })">{{ row.name || row.code }}</button><small>{{ row.category }}</small></td><td>{{ row.tradingDay }}<small>{{ row.taskVersion }}</small></td><td>{{ row.horizon ? `T+${row.horizon}` : '待评' }}<small>{{ row.target_day || '日历待核验' }}</small></td><td>{{ dailyObservationStatus(row.status) }}<small v-if="!row.referenceVerified">首次报价证据不足</small></td><td>{{ ledgerPrice(row.referencePrice) }}</td><td>{{ row.status === 'observed' ? priceObservationPercent(row.gross_reference_change_pct) : '未取得' }}</td><td>{{ time(row.reviewed_at) }}</td></tr></tbody></table></div>
          <p class="ledger-note">本节为未复权参考价变化，未扣费用，不是成交收益或正式推荐胜率。缺失的首次报价不能用后来价格补造。</p>
        </section>
        <section v-if="payload.nextDayReview" class="next-day-reviews">
          <h3>次日冲板观察 · T+1价格模式复盘</h3>
          <p class="ledger-note">次日观察使用独立、尚未验证的人工研究规则；交易候选模型的训练与启用状态不代表次日观察规则已学习。本节只记录价格模式结果，不会自动调整次日评分。</p>
          <p class="ledger-note">以信号日实际收盘价计算主板10%价格模式，分别观察下一交易日是否触及、收盘达到及全天处于该价位。尚未核验目标日除权、ST与实际涨停制度，不作为已核实涨停命中率，也不代表成交或收益；证据不完整显示待补。</p>
          <div v-if="nextDaySummaryGroups.length" class="review-progress"><article v-for="group in nextDaySummaryGroups" :key="`${group.version}-${group.queue}-${group.selected}`"><b>{{ group.version || '版本未记录' }} · {{ nextDayQueueLabel({ queue: group.queue }) }} · {{ group.selected ? '入选' : '对照' }}</b><p>成熟 {{ group.maturePatterns ?? 0 }} / {{ group.signalCount ?? 0 }} 条 · 待补 {{ group.statusCounts?.incomplete ?? 0 }} · 未到期 {{ group.statusCounts?.pending ?? 0 }} · 到期待执行 {{ group.statusCounts?.review_not_run ?? 0 }}</p><p>信号至目标收盘：上涨 {{ group.upCount ?? 0 }} · 下跌 {{ group.downCount ?? 0 }} · 持平 {{ group.flatCount ?? 0 }} · 平均价格变化 {{ priceObservationPercent(group.meanObservedReturnFromSignalPct) }}</p><small>均值仅包含可核验价格的 {{ group.priceObservationCount ?? 0 }} 条；未计交易费用，不是成交收益。</small></article></div>
          <div v-if="nextDayReviews.length" class="review-table-wrap"><table class="review-table"><thead><tr><th>股票 / 信号</th><th>观察类别</th><th>目标交易日</th><th>证据状态</th><th>触及模式价</th><th>收盘模式价</th><th>全天模式价</th><th>模式价 / 依据</th></tr></thead><tbody>
            <tr v-for="review in nextDayReviews" :key="review.signalId"><td><button @click="$emit('open-chart', { code: review.code, name: review.name })">{{ review.name || review.code }}</button><small>{{ review.signalDate }} · {{ review.code }}</small><small>{{ review.signalId }}</small></td><td>{{ review.selected ? '次日观察' : '次日对照' }}</td><td>{{ review.targetSession || '未确定' }}</td><td>{{ reviewStatusLabel(review.status, { nextDay: true }) }}<small>检查 {{ time(review.reviewedAt) }}</small></td><td>{{ nextDayPatternValue(review, 'touchLimit') }}</td><td>{{ nextDayPatternValue(review, 'closeAtLimit') }}</td><td>{{ nextDayPatternValue(review, 'onePriceLimit') }}</td><td><details><summary>¥{{ ledgerPrice(review.limitPrice) }} · 价格复盘</summary><p>目标日收盘 {{ review.targetClose == null ? '未取得' : `¥${ledgerPrice(review.targetClose)}` }} · 目标日涨跌 {{ priceObservationPercent(review.targetDayChangePct) }}</p><p>信号至目标收盘价格变化 {{ priceObservationPercent(review.observedReturnFromSignalPct) }}</p><p>目标日最大有利 / 不利变化（相对信号价）{{ priceObservationPercent(review.observedMfeFromSignalPct) }} / {{ priceObservationPercent(review.observedMaeFromSignalPct) }}</p><p v-if="review.reason">价格证据：{{ reviewReasonLabel(review.reason) }}</p><p v-if="review.limitBasis">{{ review.limitBasis }}</p><p>信号日收盘 ¥{{ ledgerPrice(review.previousSessionClose) }}</p><p>观察截止 {{ time(review.labelEndAt) }}</p><p>以上价格观察未计费用；交易所涨停制度与可成交性未核验。</p></details></td></tr>
          </tbody></table></div>
          <p v-else class="ledger-note">{{ includeControls ? '此查询范围没有已冻结的次日观察记录。' : '此查询范围没有已入选的次日观察记录，可勾选对照样本查看。' }}</p>
        </section>
        <h3>交易模拟与价格变化复盘</h3>
        <p class="ledger-note">T+1、T+3、T+5按交易日到期。观察变化以信号价为基准；模拟采用信号后的下一完整分钟入场、对应期限收盘退出，并扣除费用。MAE以正数表示不利幅度。</p>
        <p class="ledger-note">模拟结果与用户真实交易分别记录；未成交不会显示模拟收益，缺少数据会继续等待补齐。</p>
        <div v-if="reviews.length" class="review-table-wrap"><table class="review-table"><thead><tr><th>信号 / 股票</th><th>期限</th><th>复盘状态</th><th>观察变化</th><th>观察MFE / MAE</th><th>模拟成交</th><th>模拟扣费收益</th><th>模拟MFE / MAE</th><th>依据</th></tr></thead><tbody>
          <tr v-for="review in reviews" :key="`${review.signalId}-${review.horizon}`"><td><button @click="$emit('open-chart', { code:review.code, name:review.name })">{{ review.name || review.code }}</button><small>{{ review.signalDate }} · {{ review.code }}</small><small>{{ review.selected === true ? '交易候选' : review.selected === false ? '未入选对照' : '入选状态未记录' }}</small><small>{{ review.modelVersion || review.algorithmVersion }}</small></td><td>T+{{ review.horizon }}</td><td>{{ reviewStatusLabel(review.status) }}<small>检查 {{ time(review.reviewedAt) }}</small></td><td>{{ ledgerPercent(review.observedReturn ?? review.observationReturn ?? review.observed_return) }}</td><td>{{ ledgerPercent(review.observedMfe) }} / {{ ledgerPercent(review.observedMae) }}</td><td>{{ statusLabel(review.fillStatus) }}<small v-if="review.entryPrice">{{ ledgerPrice(review.entryPrice) }} → {{ ledgerPrice(review.exitPrice) }}</small></td><td>{{ reviewReturn(review) }}</td><td>{{ ledgerPercent(review.mfe) }} / {{ ledgerPercent(review.mae) }}</td><td><details><summary>详情</summary><p v-if="review.observationReason">价格证据：{{ reviewReasonLabel(review.observationReason) }}</p><p>模拟证据：{{ reviewReasonLabel(review.reason) }}</p><p>入场 {{ time(review.entryAt) }}</p><p>退出 {{ time(review.exitAt) }}</p><p>证据 {{ review.evidenceGrade || '未记录' }}</p><code>{{ review.signalId }}</code></details></td></tr>
        </tbody></table></div>
        <n-empty v-else :description="error ? '复盘暂不可用' : includeControls ? '此范围暂无交易模拟复盘记录' : '此范围暂无入选交易候选复盘记录'" />
      </template>

      <template v-else-if="mode === 'learning'">
        <n-alert :type="effectiveLearning.type" :show-icon="false" class="review-learning"><b>{{ effectiveLearning.label }}</b><p>{{ effectiveLearning.detail }}</p><p v-if="learning.rankingFallbackReason">{{ learning.rankingFallbackReason }}</p></n-alert>
        <div class="learning-header"><div><span>当前阶段</span><h3>{{ statusLabel(learning.stage) }}</h3><p>{{ learning.reason || '等待后台学习检查' }}</p></div><div><span>当前版本</span><b>{{ learning.effectiveRankVersion || learning.championVersion || '本地人工规则' }}</b><small v-for="item in learningTimes" :key="item.key">{{ item.label }} {{ item.at ? time(item.at) : '尚未完成' }}</small></div></div>
        <div class="learning-metrics"><article><span>独立成熟交易日</span><strong>{{ learning.matureDays ?? '—' }} <small>/ {{ minimumDays }}</small></strong><n-progress type="line" :percentage="learningProgress(learning.matureDays, minimumDays)" :show-indicator="false" /></article><article><span>有效成熟样本</span><strong>{{ learning.validSamples ?? '—' }} <small>/ {{ minimumSamples }}</small></strong><n-progress type="line" :percentage="learningProgress(learning.validSamples, minimumSamples)" :show-indicator="false" /></article><article><span>前瞻影子验证</span><strong>{{ learning.shadowDays ?? '—' }} <small>/ {{ learning.shadowRequiredDays ?? 20 }} 日</small></strong><small>{{ learning.shadowVersion || '尚无待晋升版本' }}</small></article></div>
        <p class="ledger-note">训练使用学习契约匹配或经过兼容性审计的主板信号与对照样本。按时间划分训练、校准和检验，边界隔离至少5个交易日；历史原始资格保持不变。最低日期数不等于每个训练折都已准备完成。</p>
        <div v-if="learningTasks.length" class="review-progress"><article v-for="task in learningTasks" :key="`${task.taskId}-${task.queue || ''}`"><b>{{ learningTaskLabel(task.taskId, task.queue) }}</b><p>{{ statusLabel(task.stage) }} · {{ task.validSamples ?? 0 }} 条有效样本 / {{ task.matureDays ?? 0 }} 个成熟交易日</p><p>{{ task.reason || '按本任务独立复盘、训练和验证' }}</p></article></div>
        <p class="ledger-note">交易收益与次日行情事件分别训练。行情触板不等于可成交盈利；下一交易日标签成熟即可供本任务使用，无须等待其他期限。</p>
        <h4>晋升检查</h4><p class="ledger-note">样本外扣费收益与相对当前规则的收益优势，其95%置信下界均须大于零，且回撤不恶化；随后完成20个交易日前瞻影子验证才可启用。推理失败或版本不兼容时自动回退。</p>
        <dl v-if="gates.length" class="learning-gates"><template v-for="gate in gates" :key="gate.key"><dt>{{ gate.label || gateLabels[gate.key] || gate.key }}</dt><dd>{{ gateValue(gate) }}</dd></template></dl><p v-else class="ledger-note">尚无模型晋升检验结果。</p>
      </template>
    </n-spin>
  </section>
</template>

<style scoped>
.review-learning{margin-bottom:12px}.review-learning p{margin:6px 0 0}.review-progress{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}.review-progress article{padding:12px 14px;border:1px solid var(--sk-border);border-radius:8px;background:var(--sk-surface);font-size:12px;line-height:1.8}.review-progress p{margin:5px 0 0;color:var(--sk-text-muted)}
.quote-alert{margin-bottom:12px}.signal-price-change{display:flex;flex-basis:100%;align-items:baseline;gap:7px 18px;flex-wrap:wrap;padding:8px 10px;background:var(--sk-surface-2);border-radius:5px;font-variant-numeric:tabular-nums}.signal-price-change>b{font-weight:600}.signal-price-change>small{flex-basis:100%;font-size:10px}.comparison-unavailable{color:var(--sk-text-muted)}.signal-row .up{color:var(--sk-up,#d03050)}.signal-row .down{color:var(--sk-down,#18a058)}.signal-row .flat{color:var(--sk-text)}
.ledger{padding-top:8px}.ledger-filters{display:flex;align-items:flex-end;flex-wrap:wrap;gap:12px;margin:0 0 16px}.ledger-filters label{display:flex;flex-direction:column;gap:6px;font-size:11px;color:var(--sk-text-muted)}.ledger-filters input[type=date]{box-sizing:border-box;height:34px;padding:5px 10px;border:1px solid var(--sk-border);border-radius:4px;background:var(--sk-surface);color:var(--sk-text);font:inherit;color-scheme:dark light}.ledger-note{font-size:12px;line-height:1.7;color:var(--sk-text-muted)}.ledger-groups{display:flex;flex-direction:column;gap:12px}.ledger-group{border:1px solid var(--sk-border);border-radius:9px;background:var(--sk-surface);padding:14px}.ledger-group header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:6px}.ledger-group header>div{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}.ledger-group small,.ledger-group header span,.ledger-group code{font-size:11px;color:var(--sk-text-muted)}.signal-row{margin-top:12px;border-top:1px solid var(--sk-border);padding-top:12px;font-size:12px}.signal-row summary{cursor:pointer;display:flex;align-items:center;gap:12px;flex-wrap:wrap}.signal-row dl,.learning-gates{display:grid;grid-template-columns:110px 1fr;gap:7px 12px;line-height:1.7;overflow-wrap:anywhere}.signal-row dt,.learning-gates dt{color:var(--sk-text-muted)}dd{margin:0}.factor-list{display:flex;flex-wrap:wrap;gap:6px 14px;background:var(--sk-surface-2);padding:10px;border-radius:5px}.factor-list span{color:var(--sk-text-muted)}.factor-list b{color:var(--sk-text);font-variant-numeric:tabular-nums;font-weight:500}.review-table-wrap{overflow:auto}.review-table{border-collapse:collapse;width:100%;font-size:12px;line-height:1.7}.review-table th,.review-table td{padding:12px 10px;text-align:right;border-bottom:1px solid var(--sk-border);white-space:nowrap;font-variant-numeric:tabular-nums}.review-table th{font-size:11px;color:var(--sk-text-muted);font-weight:500}.review-table th:first-child,.review-table td:first-child,.review-table td:last-child{text-align:left}.review-table small{display:block;color:var(--sk-text-muted);font-size:10px}.review-table button{border:none;padding:0;background:transparent;color:var(--sk-accent);font:inherit;cursor:pointer}.review-table details{max-width:240px;white-space:normal;overflow-wrap:anywhere}.review-table details summary{cursor:pointer;color:var(--sk-accent)}.review-table code{font-size:10px}.learning-header{display:flex;justify-content:space-between;gap:22px;margin:20px 0}.learning-header>div{display:flex;flex-direction:column;gap:6px}.learning-header h3,.learning-header p{margin:0}.learning-header span,.learning-header small{font-size:11px;color:var(--sk-text-muted)}.learning-header p{font-size:12px}.learning-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:20px}.learning-metrics article{display:flex;flex-direction:column;gap:12px;padding:18px;background:var(--sk-surface);border:1px solid var(--sk-border);border-radius:9px}.learning-metrics span,.learning-metrics small{font-size:12px;color:var(--sk-text-muted);font-weight:400}.learning-metrics strong{font-size:28px;font-weight:500;font-variant-numeric:tabular-nums}.learning-gates{font-size:12px;grid-template-columns:minmax(150px,1fr) 2fr;border:1px solid var(--sk-border);padding:16px;border-radius:8px}.ledger .n-empty{padding:48px 0}@media(max-width:800px){.learning-metrics{grid-template-columns:1fr}.learning-header{flex-direction:column}.signal-row dl{grid-template-columns:90px 1fr}.ledger-filters{align-items:flex-start}.signal-row summary{gap:7px}}
</style>
