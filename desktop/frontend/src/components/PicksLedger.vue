<script setup>
import { computed, ref, watch } from 'vue'
import { GetStockKingRecommendationHistory, GetStockKingDelayedReviews, GetStockKingLearningState } from '../../wailsjs/go/main/App'
import { groupSignals, ledgerPercent, ledgerPrice, statusLabel, reviewReturn, learningProgress } from '../utils/pickLedger.mjs'

const props = defineProps({ mode: { type: String, required: true } })
defineEmits(['open-chart'])
const date = ref(''), symbol = ref(''), version = ref(''), includeControls = ref(false)
const loading = ref(false), error = ref(''), payload = ref({ items: [] })
let requestId = 0
const groups = computed(() => groupSignals(payload.value, { includeControls: includeControls.value }))
const reviews = computed(() => payload.value.items || [])
const learning = computed(() => payload.value || {})
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
async function refresh() {
  const current = ++requestId
  loading.value = true; error.value = ''
  try {
    const args = [date.value.trim(), symbol.value.trim(), version.value.trim()]
    const result = props.mode === 'learning' ? await GetStockKingLearningState()
      : props.mode === 'reviews' ? await GetStockKingDelayedReviews(...args)
        : await GetStockKingRecommendationHistory(...args)
    if (current === requestId) payload.value = result || { items: [] }
  } catch (failure) { if (current === requestId) error.value = failure?.message || String(failure) }
  finally { if (current === requestId) loading.value = false }
}
watch(() => props.mode, () => { payload.value = { items: [] }; refresh() }, { immediate: true })
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
      <n-checkbox v-if="mode === 'records'" v-model:checked="includeControls">包括未入选对照样本</n-checkbox>
    </form>
    <n-alert v-if="error" type="error" :show-icon="false">读取失败：{{ error }}。请重试；当前内容未更新。</n-alert>
    <n-spin :show="loading">
      <template v-if="mode === 'records'">
        <p class="ledger-note">同股同日按算法版本汇总，各轮信号独立保留。手动刷新标为观察，不重复计入训练。旧版扫描记录可在“扫描历史”查看。</p>
        <div v-if="groups.length" class="ledger-groups">
          <article v-for="group in groups" :key="group.key" class="ledger-group">
            <header><div><b>{{ group.name }}</b><code>{{ group.code }}</code><span>{{ group.date }} · {{ group.signals.length }} 轮信号</span></div><n-button size="small" secondary @click="$emit('open-chart', group)">图表</n-button></header>
            <small>算法 {{ group.modelVersion || '原记录未保存' }}</small>
            <details v-for="signal in group.signals" :key="signal.signalId || `${signal.runId}-${signal.decisionAt}`" class="signal-row">
              <summary><span>{{ time(signal.decisionAt) }} · {{ signal.slot || '即时' }}</span><n-tag size="small" :type="signal.selected ? 'info' : 'default'">{{ signal.selected ? '入选' : '对照' }}</n-tag><span>{{ signal.official ? '定时扫描' : '手动观察' }}</span><span>{{ statusLabel(signal.reviewStatus) }}</span></summary>
              <dl><dt>信号编号</dt><dd>{{ signal.signalId || signal.signal_id || '未记录' }}</dd><dt>行情时间</dt><dd>{{ time(signal.sourceTime) }}</dd><dt>信号价格</dt><dd>{{ ledgerPrice(signal.signalPrice) }}</dd><dt>首次可成交价</dt><dd>{{ ledgerPrice(signal.firstTradablePrice) }}</dd><dt>当轮排序</dt><dd>{{ signal.rank ?? candidate(signal).rank ?? '未记录' }}</dd><dt>入选原因</dt><dd>{{ text(signal.reason || candidate(signal).selectionReasons) }}</dd><dt>风险</dt><dd>{{ text(candidate(signal).riskReasons || signal.risks) }}</dd><dt>触发条件</dt><dd>{{ text(candidate(signal).triggerConditions || candidate(signal).entryPlan?.trigger) }}</dd><dt>失效条件</dt><dd>{{ text(candidate(signal).invalidationConditions || candidate(signal).entryPlan?.invalidations) }}</dd></dl>
              <div v-if="Object.keys(factors(signal)).length" class="factor-list"><span v-for="(value, key) in factors(signal)" :key="key">{{ key }} <b>{{ value == null ? '未取得' : Number(value).toFixed(1) }}</b></span></div>
            </details>
          </article>
        </div>
        <n-empty v-else :description="error ? '记录暂不可用' : '此范围暂无已保存记录'" />
      </template>

      <template v-else-if="mode === 'reviews'">
        <p class="ledger-note">T+1、T+3、T+5按交易日到期。观察变化以信号价为基准；模拟采用信号后的下一完整分钟入场、对应期限收盘退出，并扣除费用。MAE以正数表示不利幅度。</p>
        <p class="ledger-note">模拟结果与用户真实交易分别记录；未成交不会显示模拟收益，缺少数据会继续等待补齐。</p>
        <div v-if="reviews.length" class="review-table-wrap"><table class="review-table"><thead><tr><th>信号 / 股票</th><th>期限</th><th>复盘状态</th><th>观察变化</th><th>观察MFE / MAE</th><th>模拟成交</th><th>模拟扣费收益</th><th>模拟MFE / MAE</th><th>依据</th></tr></thead><tbody>
          <tr v-for="review in reviews" :key="`${review.signalId}-${review.horizon}`"><td><button @click="$emit('open-chart', { code:review.code, name:review.name })">{{ review.name || review.code }}</button><small>{{ review.signalDate }} · {{ review.code }}</small><small>{{ review.modelVersion || review.algorithmVersion }}</small></td><td>T+{{ review.horizon }}</td><td>{{ statusLabel(review.status) }}</td><td>{{ ledgerPercent(review.observedReturn ?? review.observationReturn ?? review.observed_return) }}</td><td>{{ ledgerPercent(review.observedMfe) }} / {{ ledgerPercent(review.observedMae) }}</td><td>{{ statusLabel(review.fillStatus) }}<small v-if="review.entryPrice">{{ ledgerPrice(review.entryPrice) }} → {{ ledgerPrice(review.exitPrice) }}</small></td><td>{{ reviewReturn(review) }}</td><td>{{ ledgerPercent(review.mfe) }} / {{ ledgerPercent(review.mae) }}</td><td><details><summary>详情</summary><p>{{ text(review.reason) }}</p><p>入场 {{ time(review.entryAt) }}</p><p>退出 {{ time(review.exitAt) }}</p><p>证据 {{ review.evidenceGrade || '未记录' }}</p><code>{{ review.signalId }}</code></details></td></tr>
        </tbody></table></div>
        <n-empty v-else :description="error ? '复盘暂不可用' : '此范围暂无复盘记录'" />
      </template>

      <template v-else-if="mode === 'learning'">
        <div class="learning-header"><div><span>当前阶段</span><h3>{{ statusLabel(learning.stage) }}</h3><p>{{ learning.reason || '等待后台学习检查' }}</p></div><div><span>当前版本</span><b>{{ learning.championVersion || '本地人工规则' }}</b><small>上次训练 {{ time(learning.lastTrainingAt) }}</small></div></div>
        <div class="learning-metrics"><article><span>独立成熟交易日</span><strong>{{ learning.matureDays ?? '—' }} <small>/ {{ minimumDays }}</small></strong><n-progress type="line" :percentage="learningProgress(learning.matureDays, minimumDays)" :show-indicator="false" /></article><article><span>有效成熟样本</span><strong>{{ learning.validSamples ?? '—' }} <small>/ {{ minimumSamples }}</small></strong><n-progress type="line" :percentage="learningProgress(learning.validSamples, minimumSamples)" :show-indicator="false" /></article><article><span>前瞻影子验证</span><strong>{{ learning.shadowDays ?? '—' }} <small>/ {{ learning.shadowRequiredDays ?? 20 }} 日</small></strong><small>{{ learning.shadowVersion || '尚无待晋升版本' }}</small></article></div>
        <p class="ledger-note">训练仅使用本版本的主板信号与对照样本。按时间划分训练、校准和检验，边界隔离至少5个交易日。历史版本记录保留原口径。</p>
        <h4>晋升检查</h4><p class="ledger-note">样本外扣费收益与相对当前规则的收益优势，其95%置信下界均须大于零，且回撤不恶化；随后完成20个交易日前瞻影子验证才可启用。推理失败或版本不兼容时自动回退。</p>
        <dl v-if="gates.length" class="learning-gates"><template v-for="gate in gates" :key="gate.key"><dt>{{ gate.label || gateLabels[gate.key] || gate.key }}</dt><dd>{{ gateValue(gate) }}</dd></template></dl><p v-else class="ledger-note">尚无模型晋升检验结果。</p>
      </template>
    </n-spin>
  </section>
</template>

<style scoped>
.ledger{padding-top:8px}.ledger-filters{display:flex;align-items:flex-end;flex-wrap:wrap;gap:12px;margin:0 0 16px}.ledger-filters label{display:flex;flex-direction:column;gap:6px;font-size:11px;color:var(--sk-text-muted)}.ledger-filters input[type=date]{box-sizing:border-box;height:34px;padding:5px 10px;border:1px solid var(--sk-border);border-radius:4px;background:var(--sk-surface);color:var(--sk-text);font:inherit;color-scheme:dark light}.ledger-note{font-size:12px;line-height:1.7;color:var(--sk-text-muted)}.ledger-groups{display:flex;flex-direction:column;gap:12px}.ledger-group{border:1px solid var(--sk-border);border-radius:9px;background:var(--sk-surface);padding:14px}.ledger-group header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:6px}.ledger-group header>div{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}.ledger-group small,.ledger-group header span,.ledger-group code{font-size:11px;color:var(--sk-text-muted)}.signal-row{margin-top:12px;border-top:1px solid var(--sk-border);padding-top:12px;font-size:12px}.signal-row summary{cursor:pointer;display:flex;align-items:center;gap:12px;flex-wrap:wrap}.signal-row dl,.learning-gates{display:grid;grid-template-columns:110px 1fr;gap:7px 12px;line-height:1.7;overflow-wrap:anywhere}.signal-row dt,.learning-gates dt{color:var(--sk-text-muted)}dd{margin:0}.factor-list{display:flex;flex-wrap:wrap;gap:6px 14px;background:var(--sk-surface-2);padding:10px;border-radius:5px}.factor-list span{color:var(--sk-text-muted)}.factor-list b{color:var(--sk-text);font-variant-numeric:tabular-nums;font-weight:500}.review-table-wrap{overflow:auto}.review-table{border-collapse:collapse;width:100%;font-size:12px;line-height:1.7}.review-table th,.review-table td{padding:12px 10px;text-align:right;border-bottom:1px solid var(--sk-border);white-space:nowrap;font-variant-numeric:tabular-nums}.review-table th{font-size:11px;color:var(--sk-text-muted);font-weight:500}.review-table th:first-child,.review-table td:first-child,.review-table td:last-child{text-align:left}.review-table small{display:block;color:var(--sk-text-muted);font-size:10px}.review-table button{border:none;padding:0;background:transparent;color:var(--sk-accent);font:inherit;cursor:pointer}.review-table details{max-width:240px;white-space:normal;overflow-wrap:anywhere}.review-table details summary{cursor:pointer;color:var(--sk-accent)}.review-table code{font-size:10px}.learning-header{display:flex;justify-content:space-between;gap:22px;margin:20px 0}.learning-header>div{display:flex;flex-direction:column;gap:6px}.learning-header h3,.learning-header p{margin:0}.learning-header span,.learning-header small{font-size:11px;color:var(--sk-text-muted)}.learning-header p{font-size:12px}.learning-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:20px}.learning-metrics article{display:flex;flex-direction:column;gap:12px;padding:18px;background:var(--sk-surface);border:1px solid var(--sk-border);border-radius:9px}.learning-metrics span,.learning-metrics small{font-size:12px;color:var(--sk-text-muted);font-weight:400}.learning-metrics strong{font-size:28px;font-weight:500;font-variant-numeric:tabular-nums}.learning-gates{font-size:12px;grid-template-columns:minmax(150px,1fr) 2fr;border:1px solid var(--sk-border);padding:16px;border-radius:8px}.ledger .n-empty{padding:48px 0}@media(max-width:800px){.learning-metrics{grid-template-columns:1fr}.learning-header{flex-direction:column}.signal-row dl{grid-template-columns:90px 1fr}.ledger-filters{align-items:flex-start}.signal-row summary{gap:7px}}
</style>
