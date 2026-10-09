<script setup>
import {computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref} from 'vue'
import {useMessage} from 'naive-ui'
import * as echarts from 'echarts'
import {GetEngineStatus, GetModelMetrics, ListQuantTasks, PauseQuantTask, ResumeQuantTask,
  GetStockKingLearningState,
  StartQuantTrainingScoped, RunExecutableBacktest, GetExecutableBacktestTemplate,
  ListExecutableBacktests, GetExecutableBacktestReport} from '../../wailsjs/go/main/App'

const message = useMessage()
const engine = ref({ready: false, state: 'starting'})
const metrics = ref({models: {}, modelCount: 0})
const localValidation = ref({})
const tasks = ref([])
const refreshError = ref('')
const starting = ref(false)
const actingTask = ref('')
const symbolsText = ref('')
const objectives = ref(['regular'])
const activeTab = ref('models')
const dataset = ref(null)
const datasetName = ref('我的事前信号研究')
const calendarSource = ref('')
const confirmation = ref(false)
const csvParts = ref({})
const running = ref(false)
const result = ref(null)
const comparison = ref(null)
const executedInput = ref(null)
const backtestError = ref('')
const chartElement = ref(null)
const resultTab = ref('trades')
const savedReports = ref([])
const selectedReport = ref(null)
const loadingReport = ref(false)
const config = ref({initial_cash: 100000, delay_sessions: 0, holding_sessions: 5,
  position_weight: 0.2, max_positions: 5, commission_rate: 0.0003, minimum_commission: 5,
  stamp_tax_rate: null, transfer_fee_rate: 0.00001, slippage_bps: 10,
  stop_loss_pct: null, take_profit_pct: null})
let timer, chart, observer
let refreshing = false
const activeTasks = computed(() => tasks.value.filter(task => ['pending', 'processing', 'pause_requested'].includes(task.status)))
const canTrain = computed(() => engine.value.ready && !starting.value && !activeTasks.value.length)
const tierRows = computed(() => ['conservative', 'regular', 'aggressive'].map(key => ({key,
  label: {conservative: 'SafeBound', regular: 'BalancedRank', aggressive: 'LimitPulse'}[key],
  title: {conservative: '稳健研究', regular: '波段研究', aggressive: '短期研究'}[key],
  horizon: {conservative: '20–60 日', regular: '5–20 日', aggressive: '1–3 日'}[key],
  ...(metrics.value?.tiers?.[key] || {})})))
const balancedModel = computed(() => metrics.value?.tiers?.regular || {})
const balancedReview = computed(() => balancedModel.value.metadata?.lastChallenger || balancedModel.value)
const holdingRows = computed(() => Object.values(balancedReview.value.metrics?.holdingPeriods || {}))
const validationRange = computed(() => balancedReview.value.metadata?.nestedValidation || {})
const localAlgorithm = computed(() => localValidation.value.algorithm || {})
const localStageLabel = computed(() => ({active: '已启用通过发布门槛的排序模型', shadow: '规则排序 · 模型影子验证中', rules_cold_start: '规则排序 · 尚未完成收益验证'})[localValidation.value.stage] || '验证状态未读取')
const localGateRows = computed(() => [['holdout', '隔离样本验收'], ['shadow', '前向影子验证'], ['liveRolling', '启用后滚动复核']].map(([key, label]) => ({key, label, ...(localValidation.value.gates?.[key] || {})})))
const localWeightRows = computed(() => [['early', '早期结构'], ['main', '主升结构'], ['distribution', '分歧风险']].map(([key, label]) => ({key, label, weights: localAlgorithm.value.weights?.[key] || {}})))
const csvComplete = computed(() => ['signals', 'bars', 'calendar', 'benchmark'].every(key => csvParts.value[key]))
const inputReady = computed(() => Boolean(dataset.value || (csvComplete.value && calendarSource.value.trim())))
const isDemo = computed(() => dataset.value?.provenance === 'synthetic_demo')
const failureLabels = {insufficient_independent_blocks: '独立时间块不足', rank_ic_not_significant: '排序下界未大于零', net_return_not_significant: '扣费收益下界未大于零', no_selection_advantage: '选股优势未显著'}
const rejectLabels = {suspended_or_zero_volume: '停牌 / 无量', open_at_limit_up: '开盘涨停，买入作废', open_at_limit_down: '开盘跌停，延后退出', slippage_outside_observed_range: '滑点价超出日内区间', slippage_outside_price_limits: '滑点价超出涨跌停价', max_positions: '达到持仓上限', insufficient_cash_or_minimum_lot: '资金不足最小买入量', existing_position_or_exit_signal: '已有持仓 / 同时退出', outside_execution_window: '延迟后超出数据区间', no_position: '无可卖持仓', t_plus_one: 'T+1 不可当日卖出'}
const exitLabels = {recorded_signal: '事前信号', holding_period: '持有期满', signal_exit: '退出信号', previous_close_stop_loss: '前收盘触发止损', previous_close_take_profit: '前收盘触发止盈'}
function number(value, digits = 2) { return value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('zh-CN', {minimumFractionDigits: digits, maximumFractionDigits: digits}) }
function percent(value) { return value == null || !Number.isFinite(Number(value)) ? '—' : `${(Number(value) * 100).toFixed(2)}%` }
function interval(values) { return Array.isArray(values) ? values.map(percent).join(' ～ ') : '样本不足' }
function direction(value) { return Number(value) > 0 ? 'rise' : Number(value) < 0 ? 'fall' : '' }
function errorText(error) { return typeof error === 'string' ? error : error?.message || JSON.stringify(error) || '未知错误' }
const holdingColumns = [
  {title: '持有期', key: 'horizon', render: row => `${row.horizon} 日`},
  {title: '天数 / 时间块', key: 'evaluationDays', render: row => `${row.evaluationDays} / ${row.approximateBlocks}`},
  {title: 'RankIC', key: 'rankIc', render: row => number(row.rankIc, 4)},
  {title: 'Top 5 标签收益', key: 'top5NetReturn', render: row => percent(row.top5NetReturn)},
  {title: '超额收益 95% 区间', key: 'excessReturnInterval95', render: row => interval(row.excessReturnInterval95)},
  {title: '扣费上涨占比', key: 'top5WinRate', render: row => percent(row.top5WinRate)},
  {title: '验收结论', key: 'qualified', render: row => row.qualified ? '通过' : (row.failures || []).map(reason => failureLabels[reason] || reason).join('；')}]
const tradeColumns = [{title: '成交日', key: 'date'}, {title: '证券', key: 'symbol'},
  {title: '方向', key: 'side', render: row => row.side === 'buy' ? '买入' : '卖出'},
  {title: '股数', key: 'quantity'}, {title: '成交价', key: 'price', render: row => number(row.price)},
  {title: '费税', key: 'fees', render: row => number(row.fees.total)},
  {title: '成交后现金', key: 'cash_after', render: row => number(row.cash_after)},
  {title: '原因', key: 'reason', render: row => exitLabels[row.reason] || row.reason}]
const rejectionColumns = [{title: '执行日', key: 'date', render: row => row.date || '区间外'}, {title: '证券', key: 'symbol'}, {title: '信号', key: 'signal_id'}, {title: '未成交原因', key: 'reason', render: row => rejectLabels[row.reason] || row.reason}]
const positionColumns = [{title: '证券', key: 'symbol'}, {title: '买入日', key: 'entry_date'}, {title: '股数', key: 'quantity'}, {title: '买入价', key: 'entry_price'}, {title: '期末估值价', key: 'mark_price'}, {title: '待退出原因', key: 'pending_exit', render: row => exitLabels[row.pending_exit] || '尚未退出'}]

async function refresh() {
  if (refreshing) return
  refreshing = true
  try {
    const requests = [['engine', GetEngineStatus()]]
    if (activeTab.value === 'models') requests.push(['local', GetStockKingLearningState()])
    if (activeTab.value === 'legacy-models') requests.push(['metrics', GetModelMetrics()], ['tasks', ListQuantTasks(12)])
    const values = await Promise.allSettled(requests.map(([, promise]) => promise))
    const failures = []
    for (const [index, response] of values.entries()) {
      const key = requests[index][0]
      if (response.status === 'rejected') {
        if (key === 'engine') engine.value = {ready: false, state: 'unavailable'}
        failures.push(`${{engine: '引擎状态', local: '本地精选验证', metrics: '日线研究', tasks: '研究任务'}[key]}：${errorText(response.reason)}`)
      } else if (key === 'engine') engine.value = response.value
      else if (key === 'local') localValidation.value = response.value || {}
      else if (key === 'metrics') metrics.value = response.value || {models: {}, modelCount: 0}
      else if (key === 'tasks') tasks.value = response.value?.tasks || []
    }
    refreshError.value = failures.join('；')
  } finally { refreshing = false }
}
async function train() {
  const symbols = [...new Set(symbolsText.value.split(/[\s,，;；]+/).map(v => v.trim()).filter(Boolean))]
  if (symbols.length < 20 || symbols.length > 200) { message.warning('请选择 20–200 只 A 股，面板模型至少需要 20 只有效股票'); return }
  if (!objectives.value.length) { message.warning('请选择至少一个模型'); return }
  starting.value = true
  try {
    const task = await StartQuantTrainingScoped(symbols, objectives.value)
    message.success(task?.deduplicated ? '已连接正在运行的任务' : '所选范围训练已提交，可在下方暂停')
    await refresh()
  } catch (error) { message.error(errorText(error)) }
  finally { starting.value = false }
}
function taskStatus(task) { return ({pending: '排队', processing: '运行中', pause_requested: '等待安全暂停', paused: '已暂停', completed: '完成', failed: '失败'})[task.status] || task.status }
async function controlTask(task, action) {
  actingTask.value = `${action}:${task.task_id}`
  try {
    if (action === 'pause') await PauseQuantTask(task.task_id)
    else await ResumeQuantTask(task.task_id)
    message.success(action === 'pause' ? '已请求停止；将在安全检查点保留缓存并暂停' : '已提交续跑')
    await refresh()
  } catch (error) { message.error(errorText(error)) }
  finally { actingTask.value = '' }
}
function clearResult() { result.value = null; comparison.value = null; executedInput.value = null; backtestError.value = ''; observer?.disconnect(); chart?.dispose(); chart = null }
async function refreshReports() {
  try { savedReports.value = (await ListExecutableBacktests(20))?.reports || [] }
  catch (error) { message.warning(`历史报告读取失败：${errorText(error)}`) }
}
async function openReport() {
  if (!selectedReport.value) return
  loadingReport.value = true
  try {
    const saved = await GetExecutableBacktestReport(selectedReport.value)
    clearResult(); dataset.value = saved.input; csvParts.value = {}; confirmation.value = Boolean(saved.input.point_in_time_confirmed)
    config.value = {...config.value, ...saved.input.config}; executedInput.value = saved.input; result.value = saved.result
    await nextTick(); drawChart()
  } catch (error) { message.error(errorText(error)) }
  finally { loadingReport.value = false }
}
async function loadDemo() {
  try {
    const response = await GetExecutableBacktestTemplate()
    dataset.value = response.template; csvParts.value = {}; confirmation.value = true
    config.value = {...config.value, ...response.template.config}; clearResult()
    message.info('已载入合成演示数据，仅用于熟悉操作')
  } catch (error) { message.error(errorText(error)) }
}
async function importFile(event, kind) {
  const file = event.target.files?.[0]
  if (!file) return
  try {
    if (file.size > 24 * 1024 * 1024) throw new Error('单个文件请控制在 24 MB 内')
    const text = (await file.text()).replace(/^\uFEFF/, '')
    if (kind === 'json') {
      let parsed = JSON.parse(text)
      if (parsed?.input && parsed?.result) parsed = parsed.input
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('JSON 应为包含 signals、bars、calendar、benchmark 的对象')
      dataset.value = parsed; csvParts.value = {}; config.value = {...config.value, ...(parsed.config || {})}
    } else {
      dataset.value = null; csvParts.value = {...csvParts.value, [kind]: {text, name: file.name}}
    }
    confirmation.value = false; clearResult()
  } catch (error) { message.error(`导入失败：${errorText(error)}`) }
  finally { event.target.value = '' }
}
function payload() {
  const base = dataset.value || {dataset_name: datasetName.value, provenance: 'user_supplied_point_in_time', calendar_source: calendarSource.value, price_basis: 'unadjusted', benchmark_name: '导入基准', ...Object.fromEntries(Object.entries(csvParts.value).map(([key, value]) => [`${key}_csv`, value.text]))}
  return {...base, point_in_time_confirmed: confirmation.value, config: {...config.value}}
}
async function run(compare = false) {
  running.value = true; clearResult()
  try {
    executedInput.value = JSON.parse(JSON.stringify(payload()))
    result.value = await RunExecutableBacktest(executedInput.value)
    if (compare) {
      const delayed = {...executedInput.value, config: {...executedInput.value.config, delay_sessions: Math.min(20, executedInput.value.config.delay_sessions + 1)}}
      try { comparison.value = await RunExecutableBacktest(delayed) }
      catch (error) { message.warning(`基准运行完成；额外延迟后无法生成绩效：${errorText(error)}`) }
    }
    await nextTick(); drawChart()
    void refreshReports()
  } catch (error) { backtestError.value = errorText(error) }
  finally { running.value = false }
}
function download(data, name) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}))
  const link = document.createElement('a'); link.href = url; link.download = name; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function downloadTemplate() {
  try { const response = await GetExecutableBacktestTemplate(); download(response.template, 'stock-king-backtest-template.json') }
  catch (error) { message.error(errorText(error)) }
}
function drawChart() {
  observer?.disconnect(); chart?.dispose(); chart = null
  if (!chartElement.value || !result.value) return
  chart = echarts.init(chartElement.value)
  const rows = result.value.equity_curve || []
  chart.setOption({animation: false, backgroundColor: 'transparent', textStyle: {color: '#91a0b8', fontFamily: 'Inter, sans-serif'},
    tooltip: {trigger: 'axis', valueFormatter: value => number(value, 4)},
    legend: {top: 2, right: 12, textStyle: {color: '#91a0b8'}},
    grid: [{left: 56, right: 22, top: 42, bottom: 100}, {left: 56, right: 22, height: 42, bottom: 26}],
    xAxis: [{type: 'category', data: rows.map(r => r.date), boundaryGap: false, axisLabel: {show: false}}, {type: 'category', gridIndex: 1, data: rows.map(r => r.date), boundaryGap: false, axisLine: {lineStyle: {color: '#263449'}}, axisLabel: {fontSize: 10}}],
    yAxis: [{type: 'value', scale: true, splitLine: {lineStyle: {color: '#233044'}}, axisLabel: {formatter: v => v.toFixed(3)}}, {type: 'value', gridIndex: 1, max: 0, splitLine: {show: false}, axisLabel: {formatter: v => `${(v * 100).toFixed(2)}%`, fontSize: 10}}],
    series: [{name: '账户净值', type: 'line', showSymbol: false, lineStyle: {width: 2.5, color: '#5e94ff'}, itemStyle: {color: '#5e94ff'}, areaStyle: {color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{offset: 0, color: 'rgba(74,130,245,.24)'}, {offset: 1, color: 'rgba(74,130,245,0)'}])}, data: rows.map(r => r.nav)},
      {name: result.value.benchmark_name, type: 'line', showSymbol: false, lineStyle: {width: 1.5, type: 'dashed', color: '#9cabc2'}, itemStyle: {color: '#9cabc2'}, data: rows.map(r => r.benchmark_nav)},
      ...(comparison.value ? [{name: '额外延迟 1 日', type: 'line', showSymbol: false, lineStyle: {width: 1.5, color: '#c19aeb'}, itemStyle: {color: '#c19aeb'}, data: comparison.value.equity_curve.map(r => r.nav)}] : []),
      {name: '账户回撤', type: 'line', xAxisIndex: 1, yAxisIndex: 1, showSymbol: false, lineStyle: {width: 1, color: '#23b58e'}, areaStyle: {color: '#23b58e', opacity: .18}, data: rows.map(r => r.drawdown)}]})
  observer = new ResizeObserver(() => chart?.resize()); observer.observe(chartElement.value)
}
function tabChanged(value) {
  activeTab.value = value
  void refresh()
  if (value === 'account') void refreshReports()
  void nextTick(() => chart?.resize())
}
function startRefreshTimer() { clearInterval(timer); timer = setInterval(refresh, 5000) }
onMounted(() => { void refresh(); startRefreshTimer() })
onActivated(() => { startRefreshTimer(); void nextTick(() => chart?.resize()) })
onDeactivated(() => { clearInterval(timer) })
onBeforeUnmount(() => { clearInterval(timer); observer?.disconnect(); chart?.dispose() })
</script>

<template>
  <main class="quant-page">
    <header class="quant-header">
      <h1>策略实验室</h1>
      <div class="engine-pill" :title="engine.lastError || engine.message || engine.state"><i :class="{'is-ready': engine.ready}" />{{ engine.ready ? '引擎在线' : engine.state === 'starting' ? '启动中' : '未就绪' }}<n-button text size="small" @click="refresh">刷新</n-button></div>
    </header>
    <n-alert v-if="refreshError" type="warning" :bordered="false" class="notice">{{ refreshError }}</n-alert>
    <n-tabs v-model:value="activeTab" type="line" animated @update:value="tabChanged">
      <n-tab-pane name="account" tab="账户回测">
        <div class="workspace-grid">
          <aside class="panel controls-panel">
            <div class="section-label"><span>01</span><h3>数据</h3></div>
            <div class="import-grid">
              <label class="file-button primary-file">导入 JSON<input type="file" accept=".json,application/json" @change="importFile($event, 'json')" /></label>
              <n-button secondary @click="loadDemo">演示</n-button>
            </div>
            <div class="template-link"><n-button text size="small" @click="downloadTemplate">下载模板</n-button><span>合成数据</span></div>
            <div v-if="savedReports.length" class="saved-reports">
              <n-select v-model:value="selectedReport" size="small" placeholder="历史报告" :options="savedReports.map(row => ({value: row.report_id, label: `${row.dataset_name} · 延迟 ${row.config?.delay_sessions || 0} 日`}))" />
              <n-button secondary size="small" :disabled="!selectedReport" :loading="loadingReport" @click="openReport">打开</n-button>
            </div>
            <div v-if="dataset" class="dataset-status">
              <b>{{ dataset.dataset_name || '已导入' }}</b><span>{{ dataset.signals?.length ?? 'CSV' }} 条信号 · {{ dataset.bars?.length ?? 'CSV' }} 条行情</span>
              <n-tag v-if="isDemo" size="small" type="warning" :bordered="false">合成演示 · 非业绩</n-tag>
            </div>
            <n-collapse class="csv-collapse">
              <n-collapse-item title="导入 CSV" name="csv">
                <div class="csv-files"><label v-for="(label, key) in {signals: '事前信号', bars: '逐日行情', calendar: '交易日历', benchmark: '基准行情'}" :key="key" class="file-button">{{ csvParts[key]?.name || label }}<input type="file" accept=".csv,text/csv" @change="importFile($event, key)" /></label></div>
                <n-input v-model:value="datasetName" placeholder="数据集名称" class="field-gap" />
                <n-input v-model:value="calendarSource" placeholder="日历来源（必填）" class="field-gap" />
              </n-collapse-item>
              <n-collapse-item title="数据格式" name="format">
                <p>需要事前信号、未复权 OHLCV、交易日历和基准。JSON 字段见下载模板，也可分别导入 4 个 CSV。</p>
                <p>信号须含唯一编号、来源与带时区的 available_at。禁止把当前名单回填到过去。</p>
                <p>日历使用 date 列；volume 单位为股。每只证券与基准须覆盖完整日历；停牌需显式记录。缺失行情不补零。</p>
                <p>逐日提供涨跌停价；无价格限制时显式标注。仅支持未复权价格，不支持公司行动区间。</p>
              </n-collapse-item>
            </n-collapse>
            <n-checkbox v-model:checked="confirmation" class="confirmation">{{ isDemo ? '了解这只是合成演示' : '确认信号为事前记录' }}</n-checkbox>
            <div class="section-label second"><span>02</span><h3>参数</h3></div>
            <div class="field-grid">
              <label>本金 / 元<n-input-number v-model:value="config.initial_cash" :min="100" :max="1000000000" :step="10000" :show-button="false" /></label>
              <label>持仓上限<n-input-number v-model:value="config.max_positions" :min="1" :max="100" /></label>
              <label>单笔仓位 / 0–1<n-input-number v-model:value="config.position_weight" :min="0.01" :max="1" :step="0.05" /></label>
              <label>持有 / 交易日<n-input-number v-model:value="config.holding_sessions" :min="1" :max="500" /></label>
              <label>延迟 / 交易日<n-input-number v-model:value="config.delay_sessions" :min="0" :max="20" /></label>
              <label>滑点 / bp<n-input-number v-model:value="config.slippage_bps" :min="0" :max="500" /></label>
            </div>
            <n-collapse class="parameter-details">
              <n-collapse-item title="费税与止盈止损" name="costs">
                <div class="field-grid">
                  <label>佣金率<n-input-number v-model:value="config.commission_rate" :min="0" :max="0.02" :step="0.0001" :show-button="false" /></label>
                  <label>最低佣金 / 元<n-input-number v-model:value="config.minimum_commission" :min="0" :max="1000" /></label>
                  <label>卖出印花税率<n-input-number v-model:value="config.stamp_tax_rate" :min="0" :max="0.02" placeholder="按日期适配" :show-button="false" /></label>
                  <label>过户费率<n-input-number v-model:value="config.transfer_fee_rate" :min="0" :max="0.01" :show-button="false" /></label>
                  <label>止损比例<n-input-number v-model:value="config.stop_loss_pct" :min="0.01" :max="0.99" placeholder="可留空" :show-button="false" /></label>
                  <label>止盈比例<n-input-number v-model:value="config.take_profit_pct" :min="0.01" :max="10" placeholder="可留空" :show-button="false" /></label>
                </div>
                <p class="tiny">0.0003 = 万分之三；0.08 = 8%。印花税留空时按日期适配，止盈止损留空则不启用。</p>
              </n-collapse-item>
              <n-collapse-item title="执行方法与限制" name="execution">
                <p>仓位 0.20 = 20%；延迟 0 = 可用日期后的下一交易日开盘。持有 1 日最早在买入次日开盘退出。</p>
                <p>执行遵循 T+1、现金与持仓上限。买入受阻即作废，退出受阻逐日重试。止盈止损由前一收盘触发。</p>
                <p>未模拟成交量容量及部分成交。日线无法验证竞价排队、盘口深度或分钟级跟随效果。</p>
                <p>公司行动区间不支持；未计分红、利息、融资和软件费。期末持仓按收盘估值，未扣尚未发生的卖出费税。</p>
              </n-collapse-item>
            </n-collapse>
            <n-button type="primary" block size="large" class="run-button" :loading="running" :disabled="!engine.ready || !inputReady || !confirmation" @click="run(false)">运行回测</n-button>
            <n-button block secondary class="compare-button" :disabled="running || !engine.ready || !inputReady || !confirmation || config.delay_sessions >= 20" @click="run(true)">对照延迟 +1 日</n-button>
          </aside>
          <section class="results-column">
            <n-alert v-if="backtestError" type="error" :bordered="false" class="notice">回测失败：{{ backtestError }}</n-alert>
            <template v-if="result">
              <div class="panel result-heading">
                <div><span class="eyebrow">{{ result.performance_kind === 'synthetic_demo' ? '合成演示' : '导入信号 · 模拟结果' }}</span><h3>{{ result.dataset_name }}</h3><p>{{ result.summary.from }} → {{ result.summary.through }} · {{ result.summary.sessions }} 日 · 延迟 {{ result.config.delay_sessions }} 日</p></div>
                <n-button secondary size="small" @click="download({input: executedInput, result, comparison}, 'stock-king-account-backtest.json')">导出</n-button>
              </div>
              <n-alert v-if="result.performance_kind === 'synthetic_demo'" type="warning" :bordered="false">合成演示，不代表策略收益。</n-alert>
              <div class="stat-grid">
                <div class="stat"><span>扣费收益</span><strong :class="direction(result.summary.net_return)">{{ percent(result.summary.net_return) }}</strong><small>权益 ¥ {{ number(result.summary.final_equity) }}</small></div>
                <div class="stat"><span>最大回撤</span><strong class="fall">{{ percent(result.summary.max_drawdown) }}</strong><small>{{ result.summary.unrecovered_drawdown ? '未恢复前高' : '已恢复前高' }}</small></div>
                <div class="stat"><span>超额收益</span><strong :class="direction(result.summary.excess_return)">{{ percent(result.summary.excess_return) }}</strong><small>基准 {{ percent(result.summary.benchmark_return) }}</small></div>
                <div class="stat"><span>买入成交率</span><strong>{{ percent(result.summary.entry_fill_rate) }}</strong><small>{{ result.summary.closed_trades }} 笔平仓 · 费税 ¥ {{ number(result.summary.total_fees) }}</small></div>
              </div>
              <div v-if="comparison" class="panel comparison"><span>延迟 +1 日</span><strong :class="direction(comparison.summary.net_return)">{{ percent(comparison.summary.net_return) }}</strong><span>收益差 {{ percent(comparison.summary.net_return - result.summary.net_return) }} · 回撤 {{ percent(comparison.summary.max_drawdown) }}</span></div>
              <div class="panel chart-panel"><div class="chart-title"><h3>净值与回撤</h3><span>起点 1.00</span></div><div ref="chartElement" class="equity-chart" /></div>
              <div class="panel ledger-panel"><n-tabs v-model:value="resultTab" type="line">
                <n-tab-pane name="trades" :tab="`成交 ${result.trades.length}`"><n-data-table :columns="tradeColumns" :data="result.trades" :bordered="false" :scroll-x="780" :pagination="{pageSize: 8}" /></n-tab-pane>
                <n-tab-pane name="rejected" :tab="`未成交 ${result.rejected_orders.length}`"><n-data-table :columns="rejectionColumns" :data="result.rejected_orders" :bordered="false" :scroll-x="650" :pagination="{pageSize: 8}" /></n-tab-pane>
                <n-tab-pane name="open" :tab="`期末持仓 ${result.open_positions.length}`"><n-data-table :columns="positionColumns" :data="result.open_positions" :bordered="false" :scroll-x="650" /></n-tab-pane>
              </n-tabs></div>
              <div class="panel methodology"><n-collapse><n-collapse-item title="方法与报告详情">
                <p>未模拟成交量容量及部分成交。</p>
                <p>{{ result.summary.open_positions }} 个期末持仓按收盘估值，未假设期末强制成交。最长回撤期 {{ result.summary.longest_drawdown_sessions }} 个交易日。</p>
                <p v-for="line in result.limitations" :key="line">{{ line }}</p>
                <p>日历：{{ result.calendar_source }} · 引擎：{{ result.engine_version }}</p>
                <p v-if="result.report_path">本机报告：{{ result.report_path }}</p><code>SHA-256 {{ result.input_sha256 }}</code>
              </n-collapse-item></n-collapse></div>
            </template>
            <div v-else class="panel empty-state"><span class="empty-mark" aria-hidden="true">∿</span><h2>{{ inputReady ? '待运行' : '待导入数据' }}</h2><p>{{ inputReady ? '设置参数后运行回测。' : '缺少信号或行情，不生成收益。' }}</p></div>
          </section>
        </div>
      </n-tab-pane>
      <n-tab-pane name="models" tab="模型验证">
        <div class="model-intro panel"><div><h3>本地精选 · 算法与验证</h3><p>读取精选实际使用的规则、排序模型和同一推荐账本。刷新仅核对已保存的验证状态。</p></div><n-button secondary @click="refresh">刷新验证状态</n-button></div>
        <n-alert v-if="!localAlgorithm.contractId" type="warning" :bordered="false" class="notice">尚未取得本地精选算法契约，不能确认当前版本的验证结果。</n-alert>
        <section class="panel holding-review local-contract">
          <div class="section-title"><h3>{{ localStageLabel }}</h3><n-tag size="small" :bordered="false" :type="localValidation.stage === 'active' ? 'info' : 'warning'">{{ localValidation.stage === 'active' ? '排序模型已发布' : '未宣称已验证收益' }}</n-tag></div>
          <dl class="contract-fields"><div><dt>评分规则</dt><dd>{{ localAlgorithm.scoreVersion || '未读取' }}</dd></div><div><dt>实际排序版本</dt><dd>{{ localValidation.effectiveRankVersion || '未读取' }}</dd></div><div><dt>准入规则</dt><dd>{{ localAlgorithm.entryPolicyVersion || '未读取' }}</dd></div><div><dt>证据规则</dt><dd>{{ localAlgorithm.evidenceVersion || '未读取' }}</dd></div><div><dt>算法契约</dt><dd>{{ localAlgorithm.contractId || '未读取' }}</dd></div><div><dt>最近训练</dt><dd>{{ localValidation.lastTrainingAt || '尚未训练' }}</dd></div></dl>
          <p>{{ localValidation.reason || '当前验证状态尚未读取。' }}</p>
          <p v-if="localValidation.rankingFallbackReason">排序回退原因：{{ localValidation.rankingFallbackReason }}</p>
          <p>精选与验证共用评分权重、版本检查和事前准入。实时行情时间与历史验证时点不同，结果不要求相同；旧快照保留其生成时版本。</p>
        </section>
        <section class="stat-grid validation-stats">
          <article class="stat"><span>成熟交易日</span><strong>{{ localValidation.matureDays ?? '—' }} <small>/ {{ localValidation.minimumMatureDays ?? '—' }}</small></strong><small>按同一算法契约统计</small></article>
          <article class="stat"><span>有效成熟样本</span><strong>{{ localValidation.validSamples ?? '—' }} <small>/ {{ localValidation.minimumValidSamples ?? '—' }}</small></strong><small>缺证据、旧契约与手动刷新不进入训练</small></article>
          <article class="stat"><span>前向影子交易日</span><strong>{{ localValidation.shadowDays ?? '—' }} <small>/ {{ localValidation.shadowRequiredDays ?? '—' }}</small></strong><small>{{ localValidation.shadowVersion || '尚无通过隔离验收的影子模型' }}</small></article>
          <article class="stat"><span>实际排序来源</span><strong class="rank-source">{{ {rules: '本地规则', champion: '验证模型', rollback: '回退模型'}[localValidation.effectiveRankSource] || '未读取' }}</strong><small>规则分和模型排名分均不等于上涨概率</small></article>
        </section>
        <section class="tier-model-grid local-gates">
          <article v-for="gate in localGateRows" :key="gate.key" class="panel tier-model-card"><div class="tier-head"><span>{{ gate.label }}</span><n-tag size="small" :bordered="false" :type="gate.passed === true ? 'info' : 'warning'">{{ gate.passed === true ? '通过' : gate.passed === false ? '未通过' : '尚未运行' }}</n-tag></div><p v-if="gate.passed == null">等待真实成熟样本与前置验收条件。</p><p v-else>{{ gate.reason || `已核验 ${gate.days ?? '—'} 个对齐净值交易日` }}</p><dl><div><dt>模拟账户净收益</dt><dd>{{ percent(gate.simulatedAccountNet) }}</dd></div><div><dt>扣费收益区间下界</dt><dd>{{ percent(gate.net95Lower) }}</dd></div><div><dt>相对当前规则优势下界</dt><dd>{{ percent(gate.advantage95Lower) }}</dd></div></dl><p>使用同一事前可入选池及成交约束；未成交保留为现金，数据缺失不补造结果。</p></article>
        </section>
        <section class="panel holding-review"><n-collapse><n-collapse-item title="共用评分、准入与样本口径"><p>{{ localAlgorithm.formula || '等待算法契约' }}</p><p>{{ localAlgorithm.missingFactorPolicy }}</p><p v-for="row in localWeightRows" :key="row.key">{{ row.label }}：{{ Object.entries(row.weights).map(([key, weight]) => `${key} ${percent(weight)}`).join(' · ') || '未读取' }}</p><p>风险扣减系数：{{ localAlgorithm.riskPenaltyCoefficient ?? '未读取' }}</p><p>准入：{{ (localAlgorithm.entryConditions || []).join('；') }}</p><p>{{ localAlgorithm.profilePolicy }}</p><p>{{ localAlgorithm.samplePolicy }}</p><p>{{ localAlgorithm.rankingPolicy }}</p><p>延后复盘记录 T+1 / T+3 / T+5 的分钟证据模拟结果；模型发布仍需隔离验收、前向影子验证和启用后复核。人工规则阶段不会因刷新页面变成已验证模型。</p></n-collapse-item></n-collapse></section>
      </n-tab-pane>
      <n-tab-pane name="legacy-models" tab="独立日线研究">
        <div class="model-intro panel"><div><h3>独立日线研究模型</h3><p>SafeBound / BalancedRank / LimitPulse 使用各自的日线样本与研究标签。这里的训练与验收不驱动“本地精选”，也不代表精选算法已通过验证。</p></div><n-button secondary @click="refresh">刷新研究指标</n-button></div>
        <section class="tier-model-grid">
          <article v-for="tier in tierRows" :key="tier.key" class="panel tier-model-card">
            <div class="tier-head"><span>{{ tier.title }}</span><n-tag size="small" :type="tier.qualified ? 'info' : 'warning'" :bordered="false">{{ tier.qualified ? '研究验证通过' : '未验证 / 未通过' }}</n-tag></div>
            <h2>{{ tier.label }}</h2><p>{{ tier.horizon }}</p>
            <dl><div><dt>训练截止</dt><dd>{{ tier.trained_through || '未训练' }}</dd></div><div><dt>有效股票</dt><dd>{{ tier.metadata?.trainingScope?.loadedSymbols || '未记录' }}</dd></div></dl>
            <n-collapse><n-collapse-item title="验证详情"><p>状态：{{ tier.reason || 'not_trained' }} · 校准截止：{{ tier.calibrated_through || '未记录' }}</p><p>排名分不等于上涨概率；未通过的量化结果不与 AI 分数平均。</p><pre>{{ JSON.stringify({weights: tier.learned_weights || {}, metrics: tier.metrics || {}, metadata: tier.metadata || {} }, null, 2) }}</pre></n-collapse-item></n-collapse>
          </article>
        </section>
        <section class="panel holding-review">
          <div class="section-title"><h3>5 / 20 日验收</h3><span>研究标签 · 非账户净值</span></div>
          <n-data-table v-if="holdingRows.length" :columns="holdingColumns" :data="holdingRows" :scroll-x="1080" :bordered="false" />
          <n-empty v-else description="尚无新版验收报告" />
          <n-collapse class="parameter-details"><n-collapse-item title="验收方法与日期">
            <p>两个周期分别验收，每个周期至少 5 个时间块；重采样保留重叠标签的时间相关性。旧版指标不视为通过新版验收。</p>
            <p v-if="balancedModel.metadata?.lastChallenger">展示最近挑战模型；当前发布模型为 {{ balancedModel.model_version }}（{{ balancedModel.reason }}）。</p>
            <p v-if="holdingRows.length">权重学习截至 {{ validationRange.weightThrough || '—' }} · 隔离 {{ validationRange.purgeTradingDates }} 个交易日并检查标签结束日期 · 验收 {{ validationRange.gateFrom }} 至 {{ validationRange.gateThrough }}</p>
            <p>口径：重叠持有期的收盘到收盘标签，每次扣除 0.15% 成本；超额收益对比同日有效股票池等权毛收益。未模拟实际成交和资金约束，不代表账户净值。</p>
          </n-collapse-item></n-collapse>
        </section>
        <div class="training-grid">
          <section class="panel">
            <div class="section-title"><h3>独立日线模型训练</h3><span>20–200 只 A 股</span></div>
            <n-input v-model:value="symbolsText" type="textarea" :autosize="{minRows: 3, maxRows: 6}" placeholder="股票代码，逗号或换行分隔" />
            <n-checkbox-group v-model:value="objectives" class="objective-options"><n-space><n-checkbox value="conservative">稳健</n-checkbox><n-checkbox value="regular">波段</n-checkbox><n-checkbox value="aggressive">短期</n-checkbox></n-space></n-checkbox-group>
            <n-button type="primary" :loading="starting" :disabled="!canTrain" @click="train">开始训练</n-button>
            <n-collapse class="parameter-details"><n-collapse-item title="训练说明"><p>打开页面与刷新仅读取缓存，不自动训练。任务由主动提交后运行，优先复用已有行情面板。</p><p>至少需要 20 只有效股票。缩小范围可减少耗时，结论仅适用于该范围；多年数据与时间隔离门槛不变，不自动启动 MASTER 训练。</p></n-collapse-item></n-collapse>
          </section>
          <section class="panel">
            <div class="section-title"><h3>任务</h3><span>{{ activeTasks.length }} 项进行中</span></div>
            <n-empty v-if="!tasks.length" description="暂无任务" />
            <article v-for="task in tasks" :key="task.task_id" class="task">
              <div><strong>{{ task.request?.symbol_count || task.request?.symbols?.length || '—' }} 只 · {{ (task.request?.objectives || []).map(key => ({conservative: '稳健', regular: '波段', aggressive: '短期'})[key] || key).join(' / ') }}</strong><span>{{ taskStatus(task) }}</span></div>
              <n-progress type="line" :percentage="Number(task.progress || 0)" :status="task.status === 'failed' ? 'error' : 'default'" />
              <p v-if="task.error" class="task-error">{{ task.error }}</p>
              <n-collapse><n-collapse-item title="任务详情"><p>{{ task.message || '暂无详情' }}</p><p>暂停将在安全检查点停止并保留缓存，可继续运行。</p></n-collapse-item></n-collapse>
              <div class="task-actions"><n-button v-if="['pending','processing'].includes(task.status)" secondary size="tiny" :loading="actingTask === `pause:${task.task_id}`" @click="controlTask(task, 'pause')">暂停</n-button><n-button v-if="task.status === 'paused'" secondary size="tiny" :loading="actingTask === `resume:${task.task_id}`" @click="controlTask(task, 'resume')">继续</n-button></div>
            </article>
          </section>
        </div>
      </n-tab-pane>
    </n-tabs>
  </main>
</template>

<style scoped>
.contract-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 24px;font-size:12px;margin:18px 0}.contract-fields>div{min-width:0}.contract-fields dt{color:var(--q-muted);margin-bottom:5px}.contract-fields dd{margin:0;overflow-wrap:anywhere}.validation-stats{margin-bottom:16px}.stat strong.rank-source{font-size:20px}.local-gates{margin-top:16px}@media(max-width:850px){.contract-fields{grid-template-columns:1fr}}
.quant-page{--q-muted:var(--sk-text-muted,#8e9bb0);min-height:100%;padding:28px 30px 80px;background:var(--sk-page-bg,#0b101a);color:var(--sk-text,#e7edf7);box-sizing:border-box}.quant-header{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:22px}.eyebrow{font-size:10px;letter-spacing:2px;font-weight:700;color:var(--sk-accent,#699cff)}h1{font-size:28px;letter-spacing:-.7px;margin:7px 0}h1 span{font-size:13px;font-weight:400;letter-spacing:0;margin-left:17px;color:var(--q-muted)}h2,h3,p{margin-top:0}.quant-header p,.panel p{color:var(--q-muted);line-height:1.7;font-size:12px;margin-bottom:12px}.quant-header p{margin:0}.engine-pill{display:flex;align-items:center;gap:9px;font-size:11px;color:var(--q-muted);padding:9px 13px;border:1px solid var(--sk-border,#263044);border-radius:20px}.engine-pill i{height:6px;width:6px;border-radius:50%;background:#d49c51}.engine-pill i.is-ready{background:var(--sk-accent,#699cff);box-shadow:0 0 10px #699cff55}.engine-pill .n-button{margin-left:6px}.notice{margin-bottom:14px}.workspace-grid{display:grid;grid-template-columns:325px minmax(0,1fr);gap:18px;align-items:start;margin-top:8px}.panel{background:var(--sk-surface,#111a29);border:1px solid var(--sk-border,#253045);border-radius:12px;padding:20px;min-width:0}.controls-panel{padding:20px 18px}.section-label{display:flex;align-items:center;gap:10px;margin-bottom:12px}.section-label>span{font-size:10px;color:var(--sk-accent,#699cff);background:var(--sk-surface-2,#1c2a40);padding:4px 6px;border-radius:4px}.section-label h3,.chart-title h3,.section-title h3{font-size:14px;margin:0;font-weight:600}.section-label.second{border-top:1px solid var(--sk-border,#263044);padding-top:20px;margin-top:20px}.muted{color:var(--q-muted)}.tiny{font-size:11px!important}.import-grid{display:grid;grid-template-columns:1fr auto;gap:8px}.file-button{display:flex;align-items:center;justify-content:center;min-height:34px;padding:4px 9px;box-sizing:border-box;font-size:12px;border:1px solid var(--sk-border,#263044);border-radius:5px;cursor:pointer;background:var(--sk-surface-2,#182337);text-align:center;overflow-wrap:anywhere}.file-button:hover{border-color:var(--sk-accent,#699cff)}.primary-file{border-color:#4b78c766;color:#8ab2ff}.file-button input{display:none}.template-link{display:flex;justify-content:space-between;align-items:center;margin-top:9px;font-size:10px;color:var(--q-muted)}.dataset-status{display:flex;flex-direction:column;gap:6px;font-size:11px;padding:12px;margin:12px 0;background:var(--sk-surface-2,#182337);border-radius:6px}.dataset-status b{font-size:12px;font-weight:500}.dataset-status span{color:var(--q-muted)}.dataset-status .n-tag{align-self:start}.csv-collapse{margin-top:12px}.csv-files{display:grid;grid-template-columns:1fr 1fr;gap:7px}.field-gap{margin-top:8px}.confirmation{font-size:11px;margin-top:15px;line-height:1.6}.field-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px 10px}.field-grid label{display:flex;flex-direction:column;gap:6px;font-size:11px;color:var(--q-muted)}.field-grid .n-input-number{width:100%}.field-grid+p{margin-top:12px}.run-button{margin-top:19px;font-weight:600}.compare-button{margin-top:8px}.saved-reports{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:7px;margin-top:12px}.results-column{display:flex;flex-direction:column;gap:14px;min-width:0}.result-heading{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:17px 20px}.result-heading h3{font-size:15px;margin:6px 0}.result-heading p{font-size:11px;margin:0}.stat-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.stat{padding:15px;background:var(--sk-surface,#111a29);border:1px solid var(--sk-border,#253045);border-radius:9px;display:flex;flex-direction:column;gap:9px}.stat>span{font-size:11px;color:var(--q-muted)}.stat strong{font-size:24px;font-weight:600;letter-spacing:-.7px;font-variant-numeric:tabular-nums}.stat small{font-size:10px;color:var(--q-muted);line-height:1.5}.rise{color:var(--sk-up,#f56574)!important}.fall{color:var(--sk-down,#23b58e)!important}.chart-panel{padding:17px 20px 8px}.chart-title,.section-title{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}.chart-title>span,.section-title>span{font-size:10px;color:var(--q-muted)}.equity-chart{height:325px;width:100%}.ledger-panel{padding:8px 16px 14px}.methodology{padding:15px 20px}.parameter-details{margin-top:15px}.empty-mark{font-size:64px;line-height:1;color:var(--sk-accent,#699cff);opacity:.7}.task-error{color:var(--sk-up,#f56574)!important}.methodology p{font-size:11px}.methodology code{display:block;overflow-wrap:anywhere;font-size:10px;color:var(--q-muted)}.comparison{display:flex;flex-wrap:wrap;align-items:center;gap:16px;padding:12px 20px;font-size:11px;color:var(--q-muted)}.comparison strong{font-size:18px}.empty-state{min-height:400px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:40px}.empty-state h2{font-size:20px;font-weight:500;margin:16px 0 11px}.empty-state p{max-width:470px}.empty-chart{width:min(420px,100%);color:#4c79c5;margin-bottom:38px}.empty-chart svg{width:100%;opacity:.6}.execution-rules{display:flex;flex-wrap:wrap;justify-content:center;gap:8px;margin:9px 0 24px}.execution-rules span{font-size:10px;background:var(--sk-surface-2,#1a2538);color:var(--q-muted);border:1px solid var(--sk-border,#253045);padding:6px 9px;border-radius:4px}.model-intro{display:flex;justify-content:space-between;align-items:center;gap:20px;margin:8px 0 16px}.model-intro h3{margin:7px 0;font-size:18px}.model-intro p{margin:0;max-width:760px}.tier-model-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-bottom:16px}.tier-head{display:flex;justify-content:space-between;align-items:center;font-size:11px;color:var(--q-muted);margin-bottom:18px}.tier-model-card h2{font-size:24px;font-weight:500;letter-spacing:-.5px;margin-bottom:7px}.tier-model-card dl{font-size:11px;display:flex;flex-direction:column;gap:8px;margin:18px 0}.tier-model-card dl>div{display:flex;justify-content:space-between}.tier-model-card dt{color:var(--q-muted)}.tier-model-card dd{margin:0}.holding-review{margin-bottom:16px}.holding-review .n-empty{padding:25px}.training-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.objective-options{margin:16px 0}.task{padding:12px 0;border-bottom:1px solid var(--sk-border,#253045)}.task:last-child{border-bottom:0}.task>div:first-child{display:flex;justify-content:space-between;gap:12px;margin-bottom:8px;font-size:11px}.task strong{font-size:11px;font-weight:500;overflow-wrap:anywhere}.task span{color:var(--q-muted);white-space:nowrap}.task p{font-size:11px;margin:5px 0}.task-actions{display:flex;justify-content:flex-end}pre{font-size:10px;line-height:1.6;max-height:320px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--q-muted)}:deep(.n-tabs-nav){margin-bottom:12px}:deep(.n-tab-pane){padding-top:8px}:deep(.n-data-table-th){font-size:11px}:deep(.n-data-table-td){font-size:11px}@media(max-width:1200px){.quant-page{padding:20px 18px 70px}.workspace-grid{grid-template-columns:300px minmax(0,1fr)}.stat-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.tier-model-grid{grid-template-columns:1fr}.training-grid{grid-template-columns:1fr}}@media(max-width:850px){.workspace-grid{grid-template-columns:1fr}.quant-header{align-items:flex-start}.quant-header h1 span{display:block;margin:6px 0 0}.engine-pill{white-space:nowrap}.empty-state{min-height:360px}.model-intro{align-items:flex-start}.result-heading{flex-wrap:wrap}}
</style>
