<script setup>
import { computed, inject, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { GetFollowList, GetTelegraphList, ReFleshTelegraphList } from '../../wailsjs/go/main/App'
import { EventsOff, EventsOn } from '../../wailsjs/runtime'
import StockKingIcon from './StockKingIcon.vue'
import AnalyzeMartket from './AnalyzeMartket.vue'
import ConceptEventList from './ConceptEventList.vue'
import RzrqRank from './RzrqRank.vue'
import NewsList from './newsList.vue'

const darkTheme = inject('appDarkTheme', ref(true))
const telegraphList = ref([])
const watchlistCount = ref(null)
const time = ref(new Date())
const newsError = ref('')
const refreshing = ref(false)
const newsTab = ref('telegraph')
const dateText = computed(() => time.value.toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }))
let timer = null

const entryPoints = [
  { name: 'aiAdvice', step: '02', icon: 'ai', title: 'AI 研究', text: '导入 ChatGPT 研究，或用自定义提示词调用 AI。', action: '进入 AI 研究室' },
  { name: 'quantModels', step: '03', icon: 'quant', title: '策略回测', text: '查看策略证据、成交约束与扣费后的表现。', action: '打开策略实验室' },
]
async function refreshTelegraph(force = false) {
  refreshing.value = true
  newsError.value = ''
  try { telegraphList.value = (await (force ? ReFleshTelegraphList('财联社电报') : GetTelegraphList('财联社电报'))) || [] }
  catch { newsError.value = '资讯未连接' }
  finally { refreshing.value = false }
}
onMounted(() => {
  timer = setInterval(() => { time.value = new Date() }, 60_000)
  void refreshTelegraph()
  try { GetFollowList(0).then(rows => { watchlistCount.value = rows?.length || 0 }).catch(() => {}) } catch {}
  try {
    EventsOn('newTelegraph', data => {
      if (!Array.isArray(data)) return
      telegraphList.value = [...data, ...telegraphList.value].slice(0, Math.max(100, telegraphList.value.length))
    })
  } catch {}
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  try { EventsOff('newTelegraph') } catch {}
})
</script>

<template>
  <main class="home-workspace">
    <header class="home-heading">
      <h1 class="sk-page-heading">市场</h1>
      <div class="home-date"><span>{{ dateText }}</span><RouterLink :to="{ name: 'stock' }"><StockKingIcon name="watchlist" :size="15" />{{ watchlistCount === null ? '我的自选' : `${watchlistCount} 只自选` }}<StockKingIcon name="arrow" :size="15" /></RouterLink></div>
    </header>

    <section class="workspace-entry-grid" aria-label="研究流程">
      <RouterLink v-for="entry in entryPoints" :key="entry.name" :to="{ name: entry.name }" class="workspace-entry">
        <span class="entry-icon"><StockKingIcon :name="entry.icon" :size="23" /></span>
        <h2>{{ entry.title }}</h2><StockKingIcon name="arrow" :size="17" />
      </RouterLink>
    </section>

    <section class="home-market-section">
      <div class="home-section-heading"><div><h2>行情</h2></div><RouterLink :to="{ name: 'market' }">全部行情<StockKingIcon name="arrow" :size="15" /></RouterLink></div>
      <div class="home-market-panel"><AnalyzeMartket :dark-theme="darkTheme" :chart-height="245" /></div>
    </section>

    <section class="home-news-section">
      <div class="home-section-heading"><div><h2>资讯</h2></div><n-button size="small" quaternary :loading="refreshing" @click="refreshTelegraph(true)"><template #icon><StockKingIcon name="refresh" :size="15" /></template>刷新</n-button></div>
      <div class="home-research-grid">
        <div class="home-news-panel"><n-tabs v-model:value="newsTab" type="line" size="small" animated><n-tab-pane name="telegraph" tab="快讯"><n-empty v-if="newsError || (!refreshing && !telegraphList.length)" :description="newsError || '暂无资讯'" /><NewsList v-else :news-list="telegraphList" header-title="财联社电报" @update:message="refreshTelegraph(true)" /></n-tab-pane><n-tab-pane name="concept" tab="题材"><ConceptEventList /></n-tab-pane></n-tabs></div>
        <div class="home-capital-panel"><RzrqRank :dark-theme="darkTheme" /></div>
      </div>
    </section>
  </main>
</template>

<style scoped>
.home-workspace{max-width:1720px;margin:0 auto;padding:30px 30px 36px;min-height:100%}.home-heading{display:flex;align-items:center;justify-content:space-between;gap:24px;margin-bottom:26px}.home-date{display:flex;flex-direction:column;align-items:flex-end;gap:14px;white-space:nowrap}.home-date>span{color:var(--sk-text-muted);font-size:12px}.home-date>a{display:flex;align-items:center;gap:9px;text-decoration:none;font-size:11px;color:var(--sk-text);padding:9px 12px;border:1px solid var(--sk-border);border-radius:7px;background:var(--sk-surface)}.workspace-entry-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.workspace-entry{position:relative;display:block;overflow:hidden;padding:20px 22px 17px;border:1px solid var(--sk-border);border-radius:12px;text-decoration:none;color:var(--sk-text);background:linear-gradient(130deg,var(--sk-surface),var(--sk-surface));transition:transform .16s,border-color .16s,background .16s}.workspace-entry:first-child{background:linear-gradient(130deg,var(--sk-accent-soft),var(--sk-surface));border-color:color-mix(in srgb,var(--sk-accent) 33%,var(--sk-border))}.workspace-entry:hover{transform:translateY(-2px);border-color:var(--sk-accent)}.entry-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:19px}.entry-icon{display:grid;place-items:center;width:39px;height:39px;color:var(--sk-accent);border:1px solid color-mix(in srgb,var(--sk-accent) 20%,var(--sk-border));border-radius:10px;background:var(--sk-accent-soft)}.entry-step{font-size:9px;letter-spacing:.13em;color:var(--sk-text-muted)}.workspace-entry h2{margin:0;font-size:17px;font-weight:600;letter-spacing:-.02em}.workspace-entry p{max-width:340px;min-height:38px;margin:9px 0 17px;color:var(--sk-text-muted);font-size:11px;line-height:1.85}.entry-action{display:flex;align-items:center;justify-content:space-between;padding-top:14px;border-top:1px solid var(--sk-border);color:var(--sk-accent);font-size:11px}.home-market-section,.home-news-section{margin-top:29px}.home-section-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:13px}.home-section-heading>div{display:flex;align-items:center;gap:13px}.home-section-heading h2{margin:0;font-size:16px;font-weight:600}.section-kicker{font-size:9px;letter-spacing:.14em;color:var(--sk-text-muted)}.home-section-heading>a{display:flex;align-items:center;gap:8px;font-size:11px;text-decoration:none}.home-market-panel{border:1px solid var(--sk-border);border-radius:12px;overflow:hidden;background:var(--sk-surface);padding:14px}.home-market-panel :deep(.n-card){border-color:var(--sk-border);border-radius:8px}.home-research-grid{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(300px,1fr);gap:16px}.home-news-panel,.home-capital-panel{min-width:0;max-height:560px;overflow:auto;background:var(--sk-surface);border:1px solid var(--sk-border);border-radius:12px;padding:12px 16px}.home-news-panel :deep(.n-card),.home-capital-panel :deep(.n-card){border:0;background:transparent}.home-news-panel :deep(.n-card__content){padding:0}.home-news-panel :deep(.n-card-header){padding:12px 0}.home-news-panel :deep(.n-tabs-nav){padding:0 4px}@media(max-width:1100px){.home-workspace{padding:24px 22px 30px}.workspace-entry{padding:17px 17px 14px}.entry-step{display:none}.home-date{display:none}.home-research-grid{grid-template-columns:1fr}.home-capital-panel{max-height:450px}}@media(max-width:800px){.workspace-entry-grid{grid-template-columns:1fr}.workspace-entry{display:grid;grid-template-columns:42px 1fr;column-gap:14px}.entry-top{grid-row:1/4;margin:0;align-self:start}.workspace-entry p{margin:6px 0 10px;min-height:0;max-width:none}.entry-action{grid-column:2}.section-kicker{display:none}.home-market-panel{padding:7px}.home-heading{margin-bottom:20px}.home-workspace{padding:22px 16px}}
.home-heading{margin-bottom:18px}.home-date{flex-direction:row;align-items:center;gap:18px}.workspace-entry{display:flex;align-items:center;gap:15px;padding:16px 19px}.workspace-entry h2{flex:1;font-size:15px}.workspace-entry>svg{color:var(--sk-text-muted)}.workspace-entry-grid{gap:12px}.home-market-section,.home-news-section{margin-top:23px}@media(max-width:800px){.workspace-entry-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.workspace-entry{padding:12px;gap:8px}.entry-icon{width:30px;height:30px}.workspace-entry>svg{display:none}.workspace-entry h2{font-size:12px}}
</style>
