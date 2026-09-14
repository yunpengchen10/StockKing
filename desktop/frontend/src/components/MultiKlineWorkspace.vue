<script setup>
import { computed, inject, onBeforeMount, onBeforeUnmount, ref } from 'vue'
import { useMessage } from 'naive-ui'
import {
  DeleteMultiKlineLayout,
  GetStockList,
  ListMultiKlineLayouts,
  SaveMultiKlineLayout,
} from '../../wailsjs/go/main/App'
import StockLightweightKlineChart from './StockLightweightKlineChart.vue'
import StockKingIcon from './StockKingIcon.vue'
import { stockOption, toResearchCode } from '../utils/symbol'

const message = useMessage()
const darkTheme = inject('appDarkTheme', ref(true))
const gridSize = ref(4)
const syncPeriod = ref(true)
const syncCrosshair = ref(true)
const sharedPeriod = ref('101')
const sharedCrosshair = ref(null)
const layoutName = ref('默认研究布局')
const layouts = ref([])
const selectedLayout = ref(null)
const saving = ref(false)
const stockOptions = ref([])
const rows = ref([])
const panels = ref(Array.from({ length: 9 }, () => ({ code: '', name: '', query: '', period: '101' })))

const visiblePanels = computed(() => panels.value.slice(0, gridSize.value))
const columns = computed(() => gridSize.value === 1 ? 1 : gridSize.value === 2 || gridSize.value === 4 ? 2 : 3)
const panelHeight = computed(() => {
  if (gridSize.value <= 2) return Math.max(500, window.innerHeight - 245)
  if (gridSize.value === 4) return 385
  return 300
})

let searchTimer = null
function searchStock(value) {
  if (searchTimer) clearTimeout(searchTimer)
  const keyword = String(value || '').trim()
  if (!keyword) return
  searchTimer = setTimeout(async () => {
    rows.value = ((await GetStockList(keyword)) || []).slice(0, 50)
    stockOptions.value = rows.value.map(stockOption)
  }, 150)
}

function chooseStock(index, value) {
  const code = toResearchCode(value)
  if (!code) return message.warning('证券代码无法识别')
  const row = rows.value.find((item) => item.ts_code === value)
  panels.value[index] = { ...panels.value[index], code, name: row?.name || code, query: '' }
}

function onPeriod(index, period) {
  panels.value[index].period = period
  if (syncPeriod.value) {
    sharedPeriod.value = period
    panels.value.forEach((panel) => { panel.period = period })
  }
}

function onCrosshair(point) {
  if (syncCrosshair.value) sharedCrosshair.value = point
}

async function reloadLayouts() {
  layouts.value = (await ListMultiKlineLayouts()) || []
}

async function saveLayout() {
  saving.value = true
  try {
    const saved = await SaveMultiKlineLayout({
      id: selectedLayout.value?.id || 0,
      name: layoutName.value,
      gridSize: gridSize.value,
      syncPeriod: syncPeriod.value,
      syncCrosshair: syncCrosshair.value,
      period: sharedPeriod.value,
      symbolsJson: JSON.stringify(panels.value.map(({ code, name, period }) => ({ code, name, period }))),
      indicatorsJson: JSON.stringify({ template: 'stock-king-research' }),
      isDefault: true,
    })
    selectedLayout.value = saved
    await reloadLayouts()
    message.success('宫格布局已保存')
  } catch (error) {
    message.error(`保存失败：${error?.message || error}`)
  } finally { saving.value = false }
}

function loadLayout(id) {
  const layout = layouts.value.find((item) => item.id === id)
  if (!layout) return
  selectedLayout.value = layout
  layoutName.value = layout.name
  gridSize.value = layout.gridSize
  syncPeriod.value = layout.syncPeriod
  syncCrosshair.value = layout.syncCrosshair
  sharedPeriod.value = layout.period || '101'
  try {
    const stored = JSON.parse(layout.symbolsJson || '[]')
    stored.slice(0, 9).forEach((panel, index) => { panels.value[index] = { ...panels.value[index], ...panel, query: '' } })
  } catch {}
}

async function removeLayout() {
  if (!selectedLayout.value?.id) return
  await DeleteMultiKlineLayout(selectedLayout.value.id)
  selectedLayout.value = null
  await reloadLayouts()
  message.success('布局已删除')
}

onBeforeMount(async () => {
  try { await reloadLayouts() } catch { message.warning('布局暂未连接，仍可开始多图研究') }
  const initial = layouts.value.find((item) => item.isDefault) || layouts.value[0]
  if (initial) loadLayout(initial.id)
})
onBeforeUnmount(() => { if (searchTimer) clearTimeout(searchTimer) })
</script>

<template>
  <main class="multi-page" :class="{ dark: darkTheme }">
    <header class="multi-toolbar">
      <div class="multi-title"><h1 class="sk-page-heading">多图</h1></div>
    </header>
    <section class="multi-controls">
      <n-radio-group v-model:value="gridSize" size="small">
        <n-radio-button v-for="size in [1,2,4,6,9]" :key="size" :value="size">{{ size }} 宫格</n-radio-button>
      </n-radio-group>
      <n-switch v-model:value="syncPeriod"><template #checked>同步周期</template><template #unchecked>独立周期</template></n-switch>
      <n-switch v-model:value="syncCrosshair"><template #checked>同步十字线</template><template #unchecked>独立十字线</template></n-switch>
      <n-select :value="selectedLayout?.id || null" :options="layouts.map(item => ({ label: item.name, value: item.id }))" placeholder="已保存布局" style="width:150px" @update:value="loadLayout" />
      <n-input v-model:value="layoutName" placeholder="布局名称" style="width:150px" />
      <n-button type="primary" :loading="saving" @click="saveLayout">保存布局</n-button>
      <n-button v-if="selectedLayout" tertiary type="error" @click="removeLayout">删除</n-button>
    </section>

    <section class="chart-grid" :style="{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }">
      <article v-for="(panel, index) in visiblePanels" :key="index" class="panel">
        <div class="panel-head">
          <span class="panel-number">{{ String(index + 1).padStart(2, '0') }}</span><strong>{{ panel.name || '选择股票' }}</strong><span class="panel-code">{{ panel.code }}</span>
          <n-auto-complete
            v-model:value="panel.query"
            :options="stockOptions"
            placeholder="搜索股票代码或名称"
            size="small"
            clearable
            :on-select="(value) => chooseStock(index, value)"
            @update:value="searchStock"
          />
        </div>
        <StockLightweightKlineChart
          v-if="panel.code"
          :key="`${panel.code}-${index}`"
          compact
          :code="panel.code"
          :stock-name="panel.name"
          :dark-theme="darkTheme"
          :chart-height="panelHeight"
          :initial-period="panel.period"
          :external-period="syncPeriod ? sharedPeriod : ''"
          :external-crosshair="syncCrosshair ? sharedCrosshair : null"
          :realtime-interval-ms="60000"
          @period-change="(period) => onPeriod(index, period)"
          @crosshair-change="onCrosshair"
        />
        <div v-else class="empty-panel" :style="{ minHeight: `${panelHeight - 70}px` }"><StockKingIcon name="chart" :size="32" /><strong>选择股票</strong><span>搜索名称或代码</span></div>
      </article>
    </section>
  </main>
</template>

<style scoped>
.multi-page{min-height:100%;padding:27px 24px;background:var(--sk-page-bg);color:var(--sk-text)}.multi-toolbar{display:flex;align-items:center;gap:10px;margin-bottom:24px}.multi-title .sk-eyebrow{margin-bottom:8px}.multi-controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:18px;padding:13px 14px;background:var(--sk-surface);border:1px solid var(--sk-border);border-radius:9px}.multi-controls :deep(.n-switch){font-size:11px}.chart-grid{display:grid;gap:14px}.panel{min-width:0;border:1px solid var(--sk-border);border-radius:11px;background:var(--sk-surface);overflow:hidden}.panel-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:11px 12px;border-bottom:1px solid var(--sk-border);background:color-mix(in srgb,var(--sk-surface-2) 40%,var(--sk-surface))}.panel-number{display:grid;place-items:center;width:23px;height:23px;border:1px solid var(--sk-border);border-radius:5px;font:10px Consolas,monospace;color:var(--sk-text-muted);flex:none}.panel-head strong{font-size:12px;font-weight:550}.panel-code{font:10px Consolas,monospace;color:var(--sk-text-muted)}.panel-head .n-auto-complete{flex:1;min-width:145px}.panel :deep(.lw-kline-root){border:0}.empty-panel{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:36px 15px;color:var(--sk-text-muted)}.empty-panel>svg{color:var(--sk-accent);opacity:.65;margin-bottom:10px}.empty-panel strong{font-size:13px;font-weight:500;color:var(--sk-text)}.empty-panel span{font-size:10px}@media(max-width:1100px){.chart-grid{grid-template-columns:repeat(2,minmax(0,1fr))!important}.panel-head .n-auto-complete{flex-basis:100%}.multi-page{padding:24px 22px}}@media(max-width:800px){.chart-grid{grid-template-columns:1fr!important}.multi-page{padding:22px 16px}.multi-controls{gap:9px}}
</style>
