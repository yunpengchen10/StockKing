<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { GetStockKingRecommendationHistory, GetStockKingDelayedReviews, GetStockKingLearningState, GetStockKingRecordQuotes } from '../../wailsjs/go/main/App'
import { ledgerPercent, ledgerPrice, ledgerSigned, priceObservationPercent, statusLabel, reviewStatusLabel, reviewReasonLabel, visibleReviews, reviewProgress, learningEffect, reviewReturn, learningProgress, publicationPriceComparison, createRecordQuoteLoader, nextDayPatternValue, nextDayQueueLabel } from '../utils/pickLedger.mjs'
import { withRecordReadTimeout } from '../utils/recordRead.mjs'
import { learningTimeline, learningTaskLabel } from '../utils/learningStatus.mjs'
import { dailyObservationRows, dailyObservationCategory, dailyObservationStatus, settlementHealthSummary } from '../utils/recommendationQuality.mjs'
import { shanghaiDate, formatStrategyEvidence } from '../utils/pickLedger.mjs'

// Keep historical filters, but follow the displayed persisted scan in current mode.
const props = defineProps({ mode: { type: String, required: true }, active: { type: Boolean, default: true }, refreshToken: { type: Number, default: 0 }, recordsView: { type: String, default: 'current' }, displayRunId: { type: String, default: '' }, displayAsOf: { type: String, default: '' }, publication: { type: Object, default: null }, displayedStocks: { type: Array, default: null }, reviewFilter: { type: Object, default: null } })
defineEmits(['open-chart', 'open-snapshot', 'open-review'])
const date = ref(''), symbol = ref(''), version = ref(''), includeControls = ref(false)
const recordsScope = ref(props.recordsView === 'history' ? 'history' : 'current')
const channelFilter = ref('')
const currentRecords = computed(() => props.mode === 'records' && recordsScope.value === 'current')
const loading = ref(false), error = ref(''), payload = ref({ items: [] })
const visibleGroupCount = ref(30)
const queriedAt = ref(''), reviewLearning = ref({}), learningError = ref('')
const quotesLoading = ref(false), quotesError = ref(''), quotes = ref({})
let requestId = 0, disposed = false, initialized = false, quoteTimer = null, quoteUpdateGeneration = 0
const savedRounds = computed(() => props.mode === 'records' && payload.value.recordsVersion === 'published-picks-v1' && Array.isArray(payload.value.items)
  ? payload.value.items.filter(round => round?.status === 'published' && round.runId && Array.isArray(round.items)) : [])
function channelKey(channel) { return JSON.stringify([channel.id, channel.version]) }
const channelChoices = computed(() => {
  const choices = new Map()
  for (const round of savedRounds.value) {
    for (const channel of round.channels || []) choices.set(channelKey(channel), channel.name + ' · ' + channel.version)
  }
  return [...choices.entries()]
})
const outcomeGroups = computed(() => (payload.value.strategyOutcomes?.groups || [])
  .filter(group => !channelFilter.value || channelKey(group) === channelFilter.value))
function rate(count, observed) { return observed ? (100 * count / observed).toFixed(1) + '%' : '—' }
const publications = computed(() => {
  if (props.mode !== 'records') return []
  const saved = savedRounds.value
  if (!currentRecords.value) {
    if (!channelFilter.value) return saved
    return saved.filter(round => (round.channels || []).some(c => channelKey(c) === channelFilter.value))
      .map(round => ({ ...round,
        items: round.items.filter(stock => (stock.strategyMatches || []).some(c => channelKey(c) === channelFilter.value)),
      }))
  }
  if (Array.isArray(props.displayedStocks)) {
    return props.publication?.status === 'published' && props.publication.runId === props.displayRunId && props.displayRunId ? [props.publication] : []
  }
  return saved.filter(round => round.runId === props.displayRunId)
})
const visiblePublications = computed(() => publications.value.slice(0, visibleGroupCount.value).map(round => ({
  ...round,
  items: round.items.map(stock => ({
    ...stock,
    comparison: publicationPriceComparison(stock, round, quotes.value[stock.code]),
  })),
})))
const quoteCodes = computed(() => [...new Set(publications.value.slice(0, visibleGroupCount.value).flatMap(round => round.items.map(stock => stock.code)))])
const quoteLoader = createRecordQuoteLoader({
  fetchQuotes: codes => readRecord(GetStockKingRecordQuotes(codes)),
  onState(state) { quotesLoading.value = state.loading; quotesError.value = state.error; quotes.value = state.quotes },
})
const reviews = computed(() => visibleReviews(payload.value.items, { includeControls: includeControls.value }))
const nextDayReviews = computed(() => visibleReviews(payload.value.nextDayReview?.items, { includeControls: includeControls.value }))
const nextDaySummaryGroups = computed(() => (payload.value.nextDayReview?.summary?.groups || []).filter(group => includeControls.value || group.selected === true))
const dailyReview = computed(() => payload.value.dailyObservationReview)
const dailyRows = computed(() => dailyObservationRows(dailyReview.value, includeControls.value))
const dailyGroups = computed(() => (dailyReview.value?.groups || []).filter(group => includeControls.value || !['unselected_control', 'opportunity_control'].includes(group.first_category)))
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
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value
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
function readRecord(request) {
  // Browser timers require Window as their receiver. Wrappers keep these calls
  // global when the timeout helper invokes them through its timer adapter.
  return withRecordReadTimeout(request, 20000, {
    setTimeout: (callback, delay) => setTimeout(callback, delay),
    clearTimeout: timer => clearTimeout(timer),
  })
}
function refreshQuotes() {
  if (!disposed && props.active && props.mode === 'records' && !loading.value) return quoteLoader.load(quoteCodes.value)
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
    const args = currentRecords.value ? [shanghaiDate(props.displayAsOf), '', ''] : [date.value.trim(), symbol.value.trim(), version.value.trim()]
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
    } else if (currentRecords.value && !props.displayRunId) result = { recordsVersion: 'published-picks-v1', items: [] }
    else result = await readRecord(props.mode === 'learning' ? GetStockKingLearningState() : GetStockKingRecommendationHistory(...args, false, currentRecords.value ? props.displayRunId : ''))
    if (props.mode === 'records' && result?.recordsVersion !== 'published-picks-v1' && (currentRecords.value ? props.displayRunId : true)) {
      throw new Error('未取得已发布的推荐记录')
    }
    if (!disposed && current === requestId) { payload.value = result || { items: [] }; queriedAt.value = new Date().toISOString(); visibleGroupCount.value = 30 }
  } catch (failure) { if (!disposed && current === requestId) error.value = failure?.message || String(failure) }
  finally { if (!disposed && current === requestId) loading.value = false }
  if (!disposed && current === requestId) void updateQuotes()
}
watch(() => props.reviewFilter, filter => {
  if (props.mode !== 'reviews' || !filter) return
  date.value = filter.date || ''
  symbol.value = filter.symbol || ''
  version.value = ''
  if (initialized && props.active) void refresh()
  else initialized = false
}, { immediate: true })
watch(() => props.active, active => {
  if (!active) { stopQuoteUpdates(); return }
  if (!initialized || currentRecords.value) void refresh()
  else void updateQuotes()
}, { immediate: true })
watch(visibleGroupCount, () => { if (props.mode === 'records' && props.active) void refreshQuotes() })
watch(channelFilter, () => {
  visibleGroupCount.value = 30
  if (props.active && !currentRecords.value) void refreshQuotes()
})
watch(recordsScope, () => { if (props.mode === 'records') void refresh() })
watch(() => props.displayRunId, () => {
  if (props.mode !== 'records') return
  // New publications refresh either scope using its retained filters. Hidden
  // history is invalidated too, so reopening it reads the newly published rows.
  requestId++; stopQuoteUpdates(); loading.value = false; initialized = false
  if (currentRecords.value) payload.value = { items: [] }
  if (props.active) void refresh()
})
watch([() => props.refreshToken, () => props.recordsView], () => {
  if (props.mode !== 'records') return
  const requested = props.recordsView === 'history' ? 'history' : 'current'
  if (recordsScope.value !== requested) recordsScope.value = requested
  else void refresh()
})
onBeforeUnmount(() => { disposed = true; requestId++; stopQuoteUpdates(); quoteLoader.dispose() })
</script>

<template>
  <section class="ledger">
    <form class="ledger-filters" @submit.prevent="refresh">
      <label v-if="mode === 'records'">显示范围<select v-model="recordsScope" aria-label="记录显示范围"><option value="current">与精选同步 · 当前查看轮次</option><option value="history">历史已发布推荐</option></select></label>
      <template v-if="mode !== 'learning' && !currentRecords">
        <label>信号日期<input v-model="date" type="date" aria-label="信号日期" /></label>
        <label>股票代码<n-input v-model:value="symbol" clearable placeholder="全部股票" style="width:145px" /></label>
        <label>{{ mode === 'records' ? '发布版本' : '算法版本' }}<n-input v-model:value="version" clearable placeholder="全部版本" style="width:205px" /></label>
        <label v-if="mode === 'records'">推荐打法<select v-model="channelFilter" aria-label="筛选历史推荐打法"><option value="">全部打法与旧版记录</option><option v-for="[key, label] in channelChoices" :value="key" :key="key">{{ label }}</option></select></label>
      </template>
      <n-button attr-type="submit" secondary :loading="loading">{{ mode === 'learning' ? '刷新学习状态' : '查询记录' }}</n-button>
      <n-button v-if="mode === 'records'" attr-type="button" secondary :loading="quotesLoading" :disabled="loading || !quoteCodes.length" @click="refreshQuotes">刷新报价</n-button>
      <n-checkbox v-if="mode === 'reviews'" v-model:checked="includeControls">包括未入选对照样本</n-checkbox>
    </form>
    <n-alert v-if="error" type="error" :show-icon="false">读取失败：{{ error }}。请重试；当前内容未更新。</n-alert>
    <n-spin :show="loading">
      <template v-if="mode === 'records'">
        <p v-if="currentRecords" class="ledger-note">与精选同步 · 当前查看轮次 {{ displayRunId || '尚无已发布轮次' }}</p>
        <p v-else class="ledger-note">按发布时间查看各轮推荐，轮内顺序沿用首页排名。</p>
        <details v-if="!currentRecords && outcomeGroups.length" class="strategy-review">
          <summary>比较各打法的次日表现</summary>
          <p class="ledger-note">每次正式发布的一只股票计一次。同股命中多个打法时，各计入对应打法。按下一交易日已核验收盘价相对推荐价计算，未到期及缺价样本单列。以下为已发布样本的历史表现。</p>
          <div class="review-table-wrap"><table class="review-table"><thead><tr><th>打法 / 版本</th><th>已核验 / 已推荐</th><th>待核验</th><th>上涨</th><th>涨幅 ≥5%</th><th>跌幅 ≥5%</th></tr></thead><tbody>
            <tr v-for="group in outcomeGroups" :key="channelKey(group)"><td>{{ group.name }} · {{ group.version }}</td><td>{{ group.observed }} / {{ group.total }}</td><td>{{ group.total - group.observed }}</td><td>{{ rate(group.up, group.observed) }}</td><td>{{ rate(group.bigUp, group.observed) }}</td><td>{{ rate(group.bigDown, group.observed) }}</td></tr>
          </tbody></table></div>
        </details>
        <p v-if="quotesLoading" class="ledger-note" role="status">正在更新报价…</p>
        <n-alert v-if="quotesError" type="warning" :show-icon="false" class="quote-alert">报价提示：{{ quotesError }}</n-alert>
        <div v-if="visiblePublications.length" class="ledger-groups">
          <article v-for="round in visiblePublications" :key="round.runId" class="ledger-group">
            <header><div><b>扫描 {{ time(round.asOf) }}</b><span v-if="round.publishedAt">发布 {{ time(round.publishedAt) }}</span><span v-else-if="round.recordedAt">补录 {{ time(round.recordedAt) }}</span></div><n-button size="small" secondary @click="$emit('open-snapshot', round.runId)">查看该轮精选</n-button></header>
            <p class="ledger-note"><template v-if="typeof round.official === 'boolean'">{{ round.official ? '定时扫描' : '手动扫描' }} · </template><template v-if="round.scanSlot">{{ round.scanSlot }} · </template>{{ round.strategyName || '历史轮次未记录打法' }} · {{ round.version }}</p>
            <details v-if="round.ruleSnapshots?.length" class="frozen-rules"><summary>查看本轮规则与通道结果</summary>
              <p v-for="channel in round.channels" :key="channelKey(channel)">{{ channel.name }} · {{ channel.version }} · {{ channel.reason || ('命中 ' + channel.count + ' 只') }}</p>
              <details v-for="rule in round.ruleSnapshots" :key="channelKey(rule)"><summary>{{ rule.name }} · {{ rule.version }} 规则原文</summary><pre>{{ JSON.stringify(rule, null, 2) }}</pre></details>
            </details>
            <p v-if="!round.items.length" class="ledger-note">{{ channelFilter && !currentRecords ? '本轮该打法没有进入正式推荐名单的股票。' : '本轮发布了空名单。' }}</p>
            <section v-for="stock in round.items" :key="stock.code" class="signal-row">
              <header><div><b>{{ stock.rank === 1 ? '首选' : '第 ' + stock.rank + ' 名' }} · {{ stock.name }}</b><code>{{ stock.code }}</code></div><n-button size="small" secondary @click="$emit('open-chart', stock)">图表</n-button></header>
              <p v-for="reason in stock.reasons" :key="reason">{{ reason }}</p>
              <details v-for="match in stock.strategyMatches || []" :key="channelKey(match)" class="match-evidence">
                <summary>{{ match.name }} · 通道第 {{ match.rank }} 名 · 版本 {{ match.version }}</summary>
                <p v-for="reason in match.reasons" :key="reason">{{ reason }}</p>
                <dl v-if="match.evidence?.length"><template v-for="(point, index) in match.evidence" :key="index"><dt>{{ point.label }}</dt><dd>{{ formatStrategyEvidence(point.value) }}{{ point.unit }} <small>{{ point.source }} <template v-if="point.sourceTime">· {{ time(point.sourceTime) }}</template></small></dd></template></dl>
              </details>
              <p v-if="stock.risk">风险：{{ stock.risk }}</p>
              <div class="record-price-comparison" aria-label="推荐价与最新价比较">
                <div><span>推荐参考价</span><strong>¥{{ ledgerPrice(stock.comparison.baseline) }}</strong><small>{{ stock.basisLabel || '扫描参考价' }} · {{ stock.source }} · {{ time(stock.sourceTime) }}</small></div>
                <div><span>最新报价</span><strong>¥{{ ledgerPrice(stock.comparison.latest) }}</strong><small v-if="stock.comparison.sourceTime">{{ stock.comparison.source }} · {{ time(stock.comparison.sourceTime) }}</small><small v-else>{{ quotesLoading ? '正在获取报价…' : '暂未取得报价' }}</small></div>
                <div :class="stock.comparison.direction"><span>较推荐价涨跌</span><strong v-if="stock.comparison.available">{{ ledgerSigned(stock.comparison.delta) }} 元/股 · {{ ledgerSigned(stock.comparison.percent * 100) }}%</strong><strong v-else>—</strong><small v-if="!stock.comparison.available">{{ stock.comparison.reason }}</small></div>
              </div>
            </section>
          </article>
        </div>
        <n-button v-if="publications.length > visibleGroupCount" secondary @click="visibleGroupCount += 30">显示更多已发布轮次</n-button>
        <n-empty v-if="!publications.length" :description="error ? '记录暂不可用' : '此范围暂无可核验的已发布推荐记录'" />
      </template>

      <template v-else-if="mode === 'reviews'">
        <p class="ledger-note">本页读取已保存的复盘结果；“查询记录”不会执行复盘或训练。上次查询 {{ time(queriedAt) }}，实际检查时间见每条记录。</p>
        <n-alert :type="effectiveLearning.type" :show-icon="false" class="review-learning"><b>{{ effectiveLearning.label }}</b><p>{{ effectiveLearning.detail }}</p><p v-if="learningError">学习状态读取失败：{{ learningError }}</p><p v-else-if="reviewLearning.reason">{{ reviewLearning.reason }}</p></n-alert>
        <div class="review-progress"><article v-for="summary in reviewSummaries" :key="summary.label"><b>{{ summary.label }} · 本次展示 {{ summary.total }} 条</b><p>有结论 {{ summary.concluded }} · 等待/未结算 {{ summary.waiting }} · 到期待执行 {{ summary.notRun }} · 证据待补 {{ summary.incomplete }} · 执行失败 {{ summary.failed }}<template v-if="summary.other"> · 其他状态 {{ summary.other }}</template></p></article></div>
        <p class="ledger-note">{{ includeControls ? '当前包含未入选对照样本' : '默认仅显示当时入选的交易候选与次日观察' }}。以上仅统计本次展示的信号与期限，同一股票可有多条；有结论包含未成交，不能作为推荐成功率。</p>
        <section v-if="dailyReview" class="next-day-reviews">
          <h3>机会精选与日线观察 · T+1 / T+3 / T+5 核验</h3>
          <p class="ledger-note">{{ settlementHealthSummary(dailyReview.health) }}。结算状态覆盖全部日线观察；下方名单按查询条件展示。查询只读取结果，不执行补评或训练。</p>
          <n-alert v-if="dailyReview.health?.overdueCount > 0 || dailyReview.health?.lastError" type="warning" :show-icon="false">存在到期未完成的结算，后台会从检查点重试。{{ dailyReview.health?.lastError ? `最近错误：${dailyReview.health.lastError}` : '' }}</n-alert>
          <div v-if="dailyGroups.length" class="review-progress"><article v-for="group in dailyGroups" :key="`${group.trading_day}-${group.task_id}-${group.task_version}-${group.first_category}-${group.horizon}`"><b>{{ group.trading_day }} · {{ dailyObservationCategory(group.first_category) }} · T+{{ group.horizon }}</b><p>{{ group.task_version }} · 有效收盘覆盖 {{ group.covered }}/{{ group.signals }} · 上涨 {{ group.up }} · 下跌 {{ group.down }} · 持平 {{ group.flat }}</p><small>缺失不计成功或失败；首次参考价固定，跨版本可能包含同一股票。</small></article></div>
          <div v-if="dailyRows.length" class="review-table-wrap"><table class="review-table"><thead><tr><th>股票 / 类别</th><th>信号日 / 版本</th><th>期限 / 目标日</th><th>证据状态</th><th>首次参考价 → 目标日收盘</th><th>参考价至收盘变化</th><th>最近检查</th></tr></thead><tbody><tr v-for="row in dailyRows" :key="`${row.observationId}-${row.horizon}`"><td><button @click="$emit('open-chart', { code: row.code, name: row.name })">{{ row.name || row.code }}</button><small>{{ row.category }}</small></td><td>{{ row.tradingDay }}<small>{{ row.taskVersion }}</small></td><td>{{ row.horizon ? `T+${row.horizon}` : '待评' }}<small>{{ row.target_day || '日历待核验' }}</small></td><td>{{ dailyObservationStatus(row.status) }}<small v-if="!row.referenceVerified">首次报价证据不足</small></td><td>{{ ledgerPrice(row.referencePrice) }} → {{ row.status === 'observed' ? ledgerPrice(row.observed_close) : '—' }}<small v-if="row.details?.closeEvidence?.source">{{ row.details.closeEvidence.source }}</small></td><td>{{ row.status === 'observed' ? priceObservationPercent(row.gross_reference_change_pct) : '未取得' }}</td><td>{{ time(row.reviewed_at) }}</td></tr></tbody></table></div>
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
.strategy-review,.frozen-rules,.match-evidence{margin:10px 0;font-size:12px;line-height:1.7}.strategy-review summary,.frozen-rules summary,.match-evidence summary{cursor:pointer;color:var(--sk-accent)}.frozen-rules pre{white-space:pre-wrap;overflow-wrap:anywhere}.match-evidence dl{margin:7px 0}.strategy-review{border:1px solid var(--sk-border);border-radius:8px;padding:12px 14px}
.record-price-comparison { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin: 12px 0; padding: 14px; border-radius: 8px; background: var(--sk-surface-2); font-variant-numeric: tabular-nums; }
.record-price-comparison > div { display: flex; flex-direction: column; gap: 6px; }
.record-price-comparison span, .record-price-comparison small { color: var(--sk-text-muted); font-size: 12px; }
.record-price-comparison strong { font-size: 16px; }
.record-price-comparison .up strong { color: var(--sk-up); }
.record-price-comparison .down strong { color: var(--sk-down); }
@media (max-width: 800px) { .record-price-comparison { grid-template-columns: 1fr; } }
.review-learning{margin-bottom:12px}.review-learning p{margin:6px 0 0}.review-progress{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}.review-progress article{padding:12px 14px;border:1px solid var(--sk-border);border-radius:8px;background:var(--sk-surface);font-size:12px;line-height:1.8}.review-progress p{margin:5px 0 0;color:var(--sk-text-muted)}
.quote-alert{margin-bottom:12px}.signal-price-change{display:flex;flex-basis:100%;align-items:baseline;gap:7px 18px;flex-wrap:wrap;padding:8px 10px;background:var(--sk-surface-2);border-radius:5px;font-variant-numeric:tabular-nums}.signal-price-change>b{font-weight:600}.signal-price-change>small{flex-basis:100%;font-size:10px}.comparison-unavailable{color:var(--sk-text-muted)}.signal-row .up{color:var(--sk-up,#d03050)}.signal-row .down{color:var(--sk-down,#18a058)}.signal-row .flat{color:var(--sk-text)}
.ledger{padding-top:8px}.ledger-filters{display:flex;align-items:flex-end;flex-wrap:wrap;gap:12px;margin:0 0 16px}.ledger-filters label{display:flex;flex-direction:column;gap:6px;font-size:11px;color:var(--sk-text-muted)}.ledger-filters input[type=date],.ledger-filters select{box-sizing:border-box;height:34px;padding:5px 10px;border:1px solid var(--sk-border);border-radius:4px;background:var(--sk-surface);color:var(--sk-text);font:inherit;color-scheme:dark light}.ledger-note{font-size:12px;line-height:1.7;color:var(--sk-text-muted)}.ledger-groups{display:flex;flex-direction:column;gap:12px}.ledger-group{border:1px solid var(--sk-border);border-radius:9px;background:var(--sk-surface);padding:14px}.ledger-group header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:6px}.ledger-group header>div{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}.ledger-group small,.ledger-group header span,.ledger-group code{font-size:11px;color:var(--sk-text-muted)}.signal-row{margin-top:12px;border-top:1px solid var(--sk-border);padding-top:12px;font-size:12px}.signal-row summary{cursor:pointer;display:flex;align-items:center;gap:12px;flex-wrap:wrap}.signal-row dl,.learning-gates{display:grid;grid-template-columns:110px 1fr;gap:7px 12px;line-height:1.7;overflow-wrap:anywhere}.signal-row dt,.learning-gates dt{color:var(--sk-text-muted)}dd{margin:0}.factor-list{display:flex;flex-wrap:wrap;gap:6px 14px;background:var(--sk-surface-2);padding:10px;border-radius:5px}.factor-list span{color:var(--sk-text-muted)}.factor-list b{color:var(--sk-text);font-variant-numeric:tabular-nums;font-weight:500}.review-table-wrap{overflow:auto}.review-table{border-collapse:collapse;width:100%;font-size:12px;line-height:1.7}.review-table th,.review-table td{padding:12px 10px;text-align:right;border-bottom:1px solid var(--sk-border);white-space:nowrap;font-variant-numeric:tabular-nums}.review-table th{font-size:11px;color:var(--sk-text-muted);font-weight:500}.review-table th:first-child,.review-table td:first-child,.review-table td:last-child{text-align:left}.review-table small{display:block;color:var(--sk-text-muted);font-size:10px}.review-table button{border:none;padding:0;background:transparent;color:var(--sk-accent);font:inherit;cursor:pointer}.review-table details{max-width:240px;white-space:normal;overflow-wrap:anywhere}.review-table details summary{cursor:pointer;color:var(--sk-accent)}.review-table code{font-size:10px}.learning-header{display:flex;justify-content:space-between;gap:22px;margin:20px 0}.learning-header>div{display:flex;flex-direction:column;gap:6px}.learning-header h3,.learning-header p{margin:0}.learning-header span,.learning-header small{font-size:11px;color:var(--sk-text-muted)}.learning-header p{font-size:12px}.learning-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:20px}.learning-metrics article{display:flex;flex-direction:column;gap:12px;padding:18px;background:var(--sk-surface);border:1px solid var(--sk-border);border-radius:9px}.learning-metrics span,.learning-metrics small{font-size:12px;color:var(--sk-text-muted);font-weight:400}.learning-metrics strong{font-size:28px;font-weight:500;font-variant-numeric:tabular-nums}.learning-gates{font-size:12px;grid-template-columns:minmax(150px,1fr) 2fr;border:1px solid var(--sk-border);padding:16px;border-radius:8px}.ledger .n-empty{padding:48px 0}@media(max-width:800px){.learning-metrics{grid-template-columns:1fr}.learning-header{flex-direction:column}.signal-row dl{grid-template-columns:90px 1fr}.ledger-filters{align-items:flex-start}.signal-row summary{gap:7px}}
</style>
