<script setup>
import { computed, h, onBeforeMount, onBeforeUnmount, onMounted, provide, ref } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { darkTheme, dateZhCN, zhCN } from 'naive-ui'
import { EventsOff, EventsOn, WindowSetTitle } from '../wailsjs/runtime'
import { GetConfig, GetEngineStatus, IsHKTradingTime, IsTradingTime, IsUSTradingTime } from '../wailsjs/go/main/App'
import StockKingIcon from './components/StockKingIcon.vue'
import stockKingLogo from './assets/images/stock-king-mark.svg'
import { resumePage } from './router/router'
import { PageSession } from './utils/pageSession.mjs'
import './style.css'

const route = useRoute()
const router = useRouter()
const isDark = ref(true)
provide('appDarkTheme', isDark)
const theme = computed(() => isDark.value ? darkTheme : null)
const themeOverrides = computed(() => ({
  common: {
    primaryColor: isDark.value ? '#5b8cff' : '#3569e8', primaryColorHover: '#79a2ff', primaryColorPressed: '#3569e8', primaryColorSuppl: '#5b8cff',
    successColor: '#22ab94', errorColor: '#f05264', warningColor: '#e6b767', infoColor: '#5b8cff',
    bodyColor: isDark.value ? '#0b101b' : '#f3f5f9', cardColor: isDark.value ? '#121a29' : '#ffffff',
    modalColor: isDark.value ? '#172133' : '#ffffff', popoverColor: isDark.value ? '#1a2538' : '#ffffff',
    tableColor: isDark.value ? '#121a29' : '#ffffff', inputColor: isDark.value ? '#0e1624' : '#f5f7fb',
    borderColor: isDark.value ? '#253147' : '#dde3ee', dividerColor: isDark.value ? '#253147' : '#e4e9f2',
    textColorBase: isDark.value ? '#e7edf8' : '#182238', textColor1: isDark.value ? '#e7edf8' : '#182238',
    textColor2: isDark.value ? '#b5c0d4' : '#475773', textColor3: isDark.value ? '#8190a8' : '#6c7a92',
    borderRadius: '8px', borderRadiusSmall: '5px', fontSize: '13px',
    fontFamily: 'Inter, "Segoe UI", "Microsoft YaHei", sans-serif',
  },
  Card: { borderRadius: '12px', paddingMedium: '20px', titleFontSizeMedium: '16px' },
  DataTable: { thColor: isDark.value ? '#172133' : '#f1f4fa', tdColorHover: isDark.value ? '#1a263a' : '#f5f7fc', thFontWeight: '500', borderColor: isDark.value ? '#253147' : '#e4e9f2' },
  Button: { fontWeight: '500' },
}))
const loading = ref(true)
const loadingMessage = ref('加载中…')
const engineStatus = ref({ state: 'starting', ready: false })
const venues = ref([{ label: 'A 股', open: null }, { label: '港股', open: null }, { label: '美股', open: null }])
let statusTimer = null
let startupTimer = null
let statusPending = false

const navigation = [
  { name: 'stock', label: '自选', title: '自选观察', subtitle: '关注值得持续研究的公司', icon: 'watchlist' },
  { name: 'klineAnalysis', label: '图表', title: '图表研究', subtitle: '价格、趋势与交易计划', icon: 'chart' },
  { name: 'kingPicks', label: '精选', title: '机会发现', subtitle: '从候选信号开始，验证你的判断', icon: 'picks' },
  { name: 'aiAdvice', label: 'AI 研究', title: 'AI 研究室', subtitle: '量化证据、外部观点与独立判断', icon: 'ai' },
  { name: 'quantModels', label: '策略', title: '策略实验室', subtitle: '验证策略，了解收益从何而来', icon: 'quant' },
  { name: 'multiKline', label: '多图', title: '多图工作区', subtitle: '在同一视角比较市场机会', icon: 'grid' },
  { name: 'home', label: '市场', title: '市场概览', subtitle: '观察市场脉络，形成今日研究方向', icon: 'overview' },
]
const toolItems = [
  { key: 'market', label: '市场行情' }, { key: 'research', label: '研报与深度研究' },
  { key: 'fund', label: '基金研究' }, { key: 'agent', label: '研究助手' },
  { key: 'cronTasks', label: '计划任务' }, { key: 'mcpServers', label: '数据与工具' },
  { key: 'aiConfigs', label: 'AI 平台配置' },
]
const currentPage = computed(() => navigation.find(item => item.name === route.name) || {
  title: route.name === 'settings' ? '偏好设置' : toolItems.find(item => item.key === route.name)?.label || '研究工作区',
  subtitle: 'STOCK KING / WORKSPACE',
})
const moreActive = computed(() => toolItems.some(item => item.key === route.name))
const toolOptions = toolItems.map(item => ({ ...item, icon: () => h(StockKingIcon, { name: item.key === 'aiConfigs' ? 'ai' : 'tools', size: 17 }) }))
const connectionText = computed(() => engineStatus.value.ready ? '已连接' : engineStatus.value.state === 'starting' ? '连接中' : '待连接')

async function refreshStatus() {
  if (statusPending) return
  statusPending = true
  try {
    const settled = await Promise.allSettled([IsTradingTime(), IsHKTradingTime(), IsUSTradingTime(), GetEngineStatus()])
    venues.value = venues.value.map((venue, index) => ({ ...venue, open: settled[index].status === 'fulfilled' ? !!settled[index].value : null }))
    engineStatus.value = settled[3].status === 'fulfilled' ? settled[3].value || { ready: false, state: 'unavailable' } : { ready: false, state: 'unavailable' }
  } catch { engineStatus.value = { state: 'unavailable', ready: false } }
  finally { statusPending = false }
}
function savedAppearance() {
  try { return localStorage.getItem('stock-king.appearance') } catch { return null }
}
function applyTheme(config = {}, fromSettings = false) {
  const saved = savedAppearance()
  isDark.value = saved === 'dark' || saved === 'light' ? saved === 'dark' : fromSettings ? config?.darkTheme ?? true : true
  document.documentElement.setAttribute('theme-mode', isDark.value ? 'dark' : 'light')
}
function toggleTheme() {
  isDark.value = !isDark.value
  const appearance = isDark.value ? 'dark' : 'light'
  try { localStorage.setItem('stock-king.appearance', appearance) } catch {}
  document.documentElement.setAttribute('theme-mode', appearance)
}
function goTool(key) { void router.push(resumePage(key)) }

onBeforeMount(async () => {
  applyTheme()
  try { applyTheme(await GetConfig()) } catch {}
})
onMounted(() => {
  try { WindowSetTitle('Stock King · 投资研究工作台') } catch {}
  try {
    EventsOn('loadingMsg', value => {
      if (value === 'done') loading.value = false
      else if (value) loadingMessage.value = String(value)
    })
    EventsOn('updateSettings', async () => {
      try {
        localStorage.removeItem('stock-king.appearance')
        applyTheme(await GetConfig(), true)
        localStorage.setItem('stock-king.appearance', isDark.value ? 'dark' : 'light')
      } catch {}
    })
  } catch { loading.value = false }
  void refreshStatus()
  statusTimer = setInterval(refreshStatus, 60_000)
  startupTimer = setTimeout(() => { loading.value = false }, 12_000)
})
onBeforeUnmount(() => {
  try { EventsOff('loadingMsg'); EventsOff('updateSettings') } catch {}
  if (statusTimer) clearInterval(statusTimer)
  if (startupTimer) clearTimeout(startupTimer)
})
</script>

<template>
  <n-config-provider :theme="theme" :theme-overrides="themeOverrides" :locale="zhCN" :date-locale="dateZhCN">
    <n-message-provider><n-notification-provider><n-modal-provider><n-dialog-provider>
      <div class="stock-king-shell" :class="isDark ? 'theme-dark' : 'theme-light'">
        <aside class="sk-sidebar" aria-label="主导航">
          <RouterLink :to="resumePage('home')" class="sk-brand" aria-label="Stock King 市场概览"><img :src="stockKingLogo" alt="Stock King" /></RouterLink>
          <nav class="sk-primary-nav">
            <RouterLink v-for="item in navigation" :key="item.name" :to="resumePage(item.name)" class="sk-nav-item" :class="{ 'is-active': route.name === item.name }" :title="item.title" :aria-current="route.name === item.name ? 'page' : undefined">
              <StockKingIcon :name="item.icon" /><span>{{ item.label }}</span>
            </RouterLink>
          </nav>
          <div class="sk-utility-nav">
            <n-dropdown trigger="click" placement="right-end" :options="toolOptions" @select="goTool">
              <button class="sk-nav-item" :class="{ 'is-active': moreActive }" aria-label="更多研究工具"><StockKingIcon name="tools" /><span>工具</span></button>
            </n-dropdown>
            <RouterLink :to="resumePage('settings')" class="sk-nav-item" :class="{ 'is-active': route.name === 'settings' }" title="偏好设置"><StockKingIcon name="settings" /><span>设置</span></RouterLink>
          </div>
        </aside>
        <div class="sk-workspace">
          <header class="sk-topbar">
            <div class="sk-wordmark">STOCK<span>KING</span><i /></div>
            <div class="sk-page-context"><strong>{{ currentPage.title }}</strong></div>
            <div class="sk-topbar-actions">
              <div class="sk-market-sessions" aria-label="市场交易时段">
                <span v-for="venue in venues" :key="venue.label" :class="{ 'is-open': venue.open === true }" :title="venue.open === null ? '状态暂不可用' : venue.open ? '交易时段' : '非交易时段'"><i />{{ venue.label }}</span>
              </div>
              <span class="sk-connection" :class="{ ready: engineStatus.ready }" :title="connectionText"><i /></span>
              <button class="sk-icon-button" :aria-label="isDark ? '切换浅色主题' : '切换深色主题'" :title="isDark ? '切换浅色主题' : '切换深色主题'" @click="toggleTheme"><StockKingIcon :name="isDark ? 'sun' : 'moon'" :size="19" /></button>
            </div>
          </header>
          <div v-if="loading" class="sk-startup-status" role="status"><span />{{ loadingMessage }}</div>
          <div class="sk-page-content">
            <RouterView v-slot="{ Component, route: pageRoute }">
              <KeepAlive>
                <PageSession v-if="Component" :key="pageRoute.name" :component="Component" :location="pageRoute" />
              </KeepAlive>
            </RouterView>
          </div>
          <footer class="sk-statusbar"><span><i class="sk-status-dot" />{{ connectionText }}</span><span class="sk-color-key"><i class="up" />红涨<i class="down" />绿跌</span></footer>
        </div>
      </div>
    </n-dialog-provider></n-modal-provider></n-notification-provider></n-message-provider>
  </n-config-provider>
</template>
