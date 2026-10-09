<script setup>
import { computed, inject, onBeforeMount, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { EventsOff, EventsOn } from '../../wailsjs/runtime'
import {
  GetForecast,
  Follow,
  GetResearchNote,
  GetStockKingPreference,
  GetStockList,
  SaveResearchNote,
  SaveStockKingPreference,
} from '../../wailsjs/go/main/App'
import { useMessage } from 'naive-ui'
import StockLightweightKlineChart from './StockLightweightKlineChart.vue'
import StockKingIcon from './StockKingIcon.vue'
import { stockOption, toInternalStockCode, toResearchCode } from '../utils/symbol'
import { usePageActive } from '../utils/pageSession.mjs'

const route = useRoute()
const pageActive = usePageActive()
const router = useRouter()
const message = useMessage()
const darkTheme = inject('appDarkTheme', ref(true))
const query = ref('')
const options = ref([])
const searchRows = ref([])
const code = ref('')
const name = ref('')
const recent = ref([])
const chartHeight = ref(Math.max(480, window.innerHeight - 265))
const showNotebook = ref(false)
const noteSaving = ref(false)
const forecastsLoading = ref(false)
const forecasts = ref([])
const note = ref({ symbolCode: '', symbolName: '', coreLogic: '', observationPlan: '', invalidationRisk: '' })
let searchTimer = null

const title = computed(() => code.value ? (name.value ? `${name.value} · ${code.value}` : code.value) : '尚未选择研究对象')

function updateHeight() {
  chartHeight.value = Math.max(480, window.innerHeight - 265)
}

async function search(value) {
  const keyword = String(value || '').trim()
  if (searchTimer) clearTimeout(searchTimer)
  if (!keyword) {
    options.value = []
    return
  }
  searchTimer = setTimeout(async () => {
    try {
      searchRows.value = ((await GetStockList(keyword)) || []).slice(0, 50)
      options.value = searchRows.value.map(stockOption)
    } catch (error) {
      message.error(`搜索失败：${error?.message || error}`)
    }
  }, 160)
}

function choose(value) {
  const normalized = toResearchCode(value)
  if (!normalized) {
    message.warning('暂不支持这个证券代码')
    return
  }
  const row = searchRows.value.find((item) => item.ts_code === value)
  openSymbol(normalized, row?.name || row?.fullname || normalized)
  query.value = ''
  options.value = []
}

function submitTyped() {
  const value = String(query.value || '').split('·').pop().trim()
  choose(value)
}

async function openSymbol(nextCode, nextName = '', syncRoute = true) {
  const normalized = toResearchCode(nextCode)
  if (!normalized) return
  code.value = normalized
  name.value = nextName || normalized
  recent.value = [{ code: normalized, name: name.value }, ...recent.value.filter((item) => item.code !== normalized)].slice(0, 12)
  try {
    await Promise.all([
      SaveStockKingPreference('kline.recentSymbols', JSON.stringify(recent.value)),
      SaveStockKingPreference('kline.lastViewedSymbol', JSON.stringify({ code: normalized, name: name.value })),
    ])
  } catch {}
  if (syncRoute && pageActive.value && router.currentRoute.value.name === 'klineAnalysis' && (route.query.code !== code.value || route.query.name !== name.value)) {
    await router.replace({ name: 'klineAnalysis', query: { code: code.value, name: name.value } })
  }
  void loadNote()
  void loadForecasts()
}

async function loadNote() {
  if (!code.value) {
    note.value = { symbolCode: '', symbolName: '', coreLogic: '', observationPlan: '', invalidationRisk: '' }
    return
  }
  const symbol = code.value
  try {
    const stored = await GetResearchNote(symbol)
    if (code.value !== symbol) return
    note.value = {
      symbolCode: code.value,
      symbolName: name.value,
      coreLogic: stored?.coreLogic || '',
      observationPlan: stored?.observationPlan || '',
      invalidationRisk: stored?.invalidationRisk || '',
    }
  } catch (error) {
    message.error(`读取笔记失败：${error?.message || error}`)
  }
}

async function saveNote() {
  noteSaving.value = true
  try {
    note.value.symbolCode = code.value
    note.value.symbolName = name.value
    await SaveResearchNote(note.value)
    message.success('笔记已保存')
  } catch (error) {
    message.error(`保存失败：${error?.message || error}`)
  } finally {
    noteSaving.value = false
  }
}

async function loadForecasts() {
  if (!code.value) {
    forecasts.value = []
    forecastsLoading.value = false
    return
  }
  if (!/\.(SH|SZ|BJ)$/i.test(code.value)) {
    forecasts.value = []
    forecastsLoading.value = false
    return
  }
  forecastsLoading.value = true
  const symbol = code.value
  const result = await Promise.all([1, 5, 20].map(async (horizon) => {
    try { return await GetForecast(symbol, horizon) }
    catch (error) { return { horizon, available: false, unavailableReason: error?.message || String(error) } }
  }))
  if (code.value !== symbol) return
  forecasts.value = result
  forecastsLoading.value = false
}

async function addToWatchlist() {
  if (!code.value) return
  try {
    const result = await Follow(toInternalStockCode(code.value))
    if (result === '关注成功') message.success(`${name.value || code.value} 已加入自选`)
    else if (result === '已经关注了') message.info(`${name.value || code.value} 已在自选中`)
    else message.error(result || '加入自选失败')
  } catch (error) {
    message.error(`加入自选失败：${error?.message || error}`)
  }
}

function openAIAdvice() {
  if (!code.value) return
  void router.push({ name: 'aiAdvice', query: { code: code.value, name: name.value } })
}

function percent(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : '—'
}

onBeforeMount(async () => {
  let recentRaw = ''
  try { recentRaw = await GetStockKingPreference('kline.recentSymbols') } catch {}
  if (recentRaw) {
    try { recent.value = JSON.parse(recentRaw) } catch { recent.value = [] }
  }
  const fromRoute = route.query.code ? { code: route.query.code, name: route.query.name } : null
  if (fromRoute?.code) await openSymbol(fromRoute.code, fromRoute.name || '', false)
  else {
    let lastRaw = ''
    try { lastRaw = await GetStockKingPreference('kline.lastViewedSymbol') } catch {}
    let lastViewed = null
    if (lastRaw) {
      try { lastViewed = JSON.parse(lastRaw) } catch { lastViewed = { code: lastRaw, name: '' } }
    }
    // kline.lastViewedSymbol is a new explicit-observation key. Falling back
    // to recent[0] avoids reviving the legacy hard-coded Moutai preference.
    const last = lastViewed?.code ? lastViewed : recent.value[0]
    if (last?.code) await openSymbol(last.code, last.name || '', false)
  }
})

onMounted(() => {
  window.addEventListener('resize', updateHeight)
  try {
    EventsOn('klineSelectStock', (payload) => {
      if (pageActive.value && payload?.ts_code) void openSymbol(payload.ts_code, payload.name)
    })
  } catch {}
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', updateHeight)
  try { EventsOff('klineSelectStock') } catch {}
  if (searchTimer) clearTimeout(searchTimer)
})

watch(() => route.query.code, (next) => {
  if (next && next !== code.value) void openSymbol(next, route.query.name || '', false)
})
</script>

<template>
  <main class="kline-page" :class="{ dark: darkTheme }">
    <header class="research-bar">
      <div class="security-title">

        <strong>{{ code ? title : '选择股票' }}</strong>
      </div>
      <div class="search-box">
        <n-auto-complete
          v-model:value="query"
          :options="options"
          placeholder="搜索 A 股 / 港股 / 美股"
          clearable
          :on-select="choose"
          @update:value="search"
          @keyup.enter="submitTyped"
        />
        <n-button type="primary" @click="submitTyped">打开</n-button>
        <n-button secondary :disabled="!code" @click="addToWatchlist">加入自选</n-button>
        <n-button secondary :disabled="!code" @click="showNotebook = !showNotebook">笔记</n-button>
        <n-button secondary :disabled="!code" @click="openAIAdvice"><template #icon><StockKingIcon name="ai" :size="16" /></template>AI 研究</n-button>
      </div>
    </header>

    <div v-if="recent.length" class="recent-row">
      <span>最近</span>
      <n-button v-for="item in recent.slice(0, 8)" :key="item.code" size="tiny" text @click="openSymbol(item.code, item.name)">
        {{ item.name || item.code }}
      </n-button>
    </div>

    <section v-if="code" class="research-layout" :class="{ 'with-note': showNotebook }">
      <div class="chart-shell">
        <StockLightweightKlineChart
          :key="code"
          :code="code"
          :stock-name="name"
          :dark-theme="darkTheme"
          :chart-height="chartHeight"
          :realtime-interval-ms="60000"
        />
      </div>

      <aside v-if="showNotebook" class="notebook">
        <div class="notebook-head"><strong>研究笔记</strong><n-tag size="small" :bordered="false">个人记录</n-tag></div>
        <label>核心逻辑</label>
        <n-input v-model:value="note.coreLogic" type="textarea" :autosize="{ minRows: 4, maxRows: 8 }" placeholder="这只股票为什么值得持续观察？" />
        <label>观察计划</label>
        <n-input v-model:value="note.observationPlan" type="textarea" :autosize="{ minRows: 3, maxRows: 7 }" placeholder="价格、业绩或事件触发条件" />
        <label>风险 / 证伪条件</label>
        <n-input v-model:value="note.invalidationRisk" type="textarea" :autosize="{ minRows: 3, maxRows: 7 }" placeholder="出现什么情况说明原判断不成立？" />
        <n-button type="primary" block :loading="noteSaving" @click="saveNote">保存笔记</n-button>

        <n-divider>量化预测</n-divider>
        <n-spin :show="forecastsLoading">
          <div v-if="forecasts.length" class="forecast-list">
            <div v-for="forecast in forecasts" :key="forecast.horizon" class="forecast-item">
              <strong>{{ forecast.horizon }} 日</strong>
              <template v-if="forecast.available">
                <span class="up">涨 {{ percent(forecast.probabilityUp) }}</span>
                <span>震 {{ percent(forecast.probabilityFlat) }}</span>
                <span class="down">跌 {{ percent(forecast.probabilityDown) }}</span>
              </template>
              <small v-else>{{ forecast.unavailableReason || '预测暂不可用' }}</small>
            </div>
          </div>
          <n-empty v-else size="small" description="该市场暂无量化预测" />
        </n-spin>
      </aside>
    </section>
    <section v-else class="empty-research">
      <div class="chart-welcome"><span class="chart-welcome-icon"><StockKingIcon name="chart" :size="40" /></span><h2>选择股票</h2><p>搜索名称或代码</p><div class="chart-market-tags"><span>A 股</span><span>港股</span><span>美股</span></div><n-button secondary type="primary" @click="router.push({ name: 'stock' })">打开自选<template #icon><StockKingIcon name="watchlist" :size="16" /></template></n-button></div>
    </section>
  </main>
</template>

<style scoped>
.kline-page{min-height:100%;padding:20px 22px 24px;background:var(--sk-page-bg);color:var(--sk-text)}.research-bar{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:17px}.security-title{display:flex;flex-direction:column;gap:5px;white-space:nowrap}.security-title strong{font-size:17px;font-weight:600}.chart-eyebrow{font-size:9px;color:var(--sk-text-muted);letter-spacing:.13em}.search-box{display:grid;grid-template-columns:minmax(200px,360px) repeat(4,auto);gap:7px}.recent-row{display:flex;align-items:center;gap:15px;min-height:34px;overflow:hidden;margin-bottom:10px;border-top:1px solid var(--sk-border);border-bottom:1px solid var(--sk-border)}.recent-row>span{font-size:10px;color:var(--sk-text-muted);white-space:nowrap}.recent-row .n-button{font-size:11px}.research-layout{display:grid;grid-template-columns:minmax(0,1fr);gap:14px}.research-layout.with-note{grid-template-columns:minmax(0,1fr) 320px}.chart-shell,.notebook,.empty-research{min-width:0;border:1px solid var(--sk-border);border-radius:12px;background:var(--sk-surface);overflow:hidden}.empty-research{display:grid;place-items:center;min-height:calc(100vh - 222px);color:var(--sk-text);background:radial-gradient(ellipse at 50% 25%,var(--sk-accent-soft),transparent 65%),var(--sk-surface)}.chart-welcome{display:flex;flex-direction:column;align-items:center;text-align:center;padding:48px 22px}.chart-welcome-icon{display:grid;place-items:center;width:86px;height:86px;margin-bottom:30px;color:var(--sk-accent);border:1px solid color-mix(in srgb,var(--sk-accent) 35%,var(--sk-border));border-radius:24px;background:var(--sk-accent-soft);box-shadow:0 12px 45px rgba(0,0,0,.08)}.chart-welcome>.sk-eyebrow{font-size:9px;letter-spacing:.2em;color:var(--sk-text-muted)}.chart-welcome h2{font-size:25px;font-weight:550;letter-spacing:-.04em;margin:9px 0 0}.chart-welcome p{font-size:12px;line-height:2;color:var(--sk-text-muted);margin:15px 0 0}.chart-market-tags{display:flex;gap:10px;margin:23px 0 25px}.chart-market-tags span{font-size:10px;padding:3px 12px;border:1px solid var(--sk-border);border-radius:5px;color:var(--sk-text-muted)}.chart-welcome small{margin-top:18px;color:var(--sk-text-muted);font-size:10px}.notebook{padding:18px;display:flex;flex-direction:column;gap:9px;max-height:calc(100vh - 230px);overflow:auto}.notebook-head{display:flex;align-items:center;justify-content:space-between;padding-bottom:9px;border-bottom:1px solid var(--sk-border);margin-bottom:3px}.notebook-head strong{font-weight:600}.notebook label{font-size:11px;color:var(--sk-text-muted);margin-top:6px}.forecast-list{display:flex;flex-direction:column;gap:8px}.forecast-item{display:grid;grid-template-columns:38px repeat(3,1fr);gap:4px;align-items:center;padding:9px 7px;border-radius:6px;background:var(--sk-surface-2);font-size:10px}.forecast-item small{grid-column:2/5;color:var(--sk-text-muted)}.up{color:var(--sk-up)}.down{color:var(--sk-down)}@media(max-width:1300px){.research-bar{align-items:stretch;flex-direction:column}.search-box{grid-template-columns:minmax(0,1fr) repeat(4,auto)}.security-title{flex-direction:row;align-items:center;gap:14px}.research-layout.with-note{grid-template-columns:minmax(0,1fr) 290px}}@media(max-width:1000px){.research-layout.with-note{grid-template-columns:1fr}.notebook{max-height:none}.kline-page{padding:18px 16px}.search-box{grid-template-columns:1fr repeat(2,auto)}.search-box>.n-auto-complete{grid-column:1/3}.search-box>.n-button{font-size:11px}.chart-eyebrow{display:none}.empty-research{min-height:500px}}
</style>
