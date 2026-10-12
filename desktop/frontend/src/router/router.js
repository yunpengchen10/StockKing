import {createRouter, createWebHashHistory} from 'vue-router'
import { createPageNavigationMemory } from '../utils/pageSession.mjs'

const pageMemory = createPageNavigationMemory()
export const resumePage = pageMemory.destination

const stockView = () => import('../components/StockKingWatchlist.vue')
const settingsView = () => import('../components/settings.vue')
const fundView = () => import('../components/fund.vue')
const marketView = () => import('../components/market.vue')
const agentChat = () => import('../components/agent-chat.vue')
const research = () => import('../components/researchIndex.vue')
const cronTaskManager = () => import('../components/cron-task-manager.vue')
const mcpServerManager = () => import('../components/mcp-server-manager.vue')
const aiConfigManager = () => import('../components/ai-config-manager.vue')
const homeView = () => import('../components/Home.vue')
const klineResearch = () => import('../components/KlineResearch.vue')
const multiKlineWorkspace = () => import('../components/MultiKlineWorkspace.vue')
const kingPicks = () => import('../components/KingPicks.vue')
const aiAdvice = () => import('../components/AIAdvice.vue')
const quantModelCenter = () => import('../components/QuantModelCenter.vue')


const routes = [
    { path: '/', redirect: '/home'},
    { path: '/portfolio', redirect: '/stock'},
    { path: '/kline-analysis', component: klineResearch, name: 'klineAnalysis'},
    { path: '/stock-king', redirect: '/home'},
    { path: '/king-picks', component: kingPicks, name: 'kingPicks'},
    { path: '/multi-kline', component: multiKlineWorkspace, name: 'multiKline'},
    { path: '/ai-advice', component: aiAdvice, name: 'aiAdvice'},
    { path: '/quant-models', component: quantModelCenter, name: 'quantModels'},
    { path: '/home', component: homeView,name: 'home'},
    { path: '/stock', component: stockView,name: 'stock'},
    { path: '/fund', component: fundView,name: 'fund' },
    { path: '/settings', component: settingsView,name: 'settings' },
    { path: '/market', component: marketView,name: 'market' },
    { path: '/agent', component: agentChat,name: 'agent' },
    { path: '/research', component: research,name: 'research' },
    { path: '/cron-tasks', component: cronTaskManager,name: 'cronTasks' },
    { path: '/mcp-servers', component: mcpServerManager,name: 'mcpServers' },
    { path: '/ai-configs', component: aiConfigManager,name: 'aiConfigs' },

]

const router = createRouter({
    //history: createWebHistory(),
    history: createWebHashHistory(),
    routes,
})

router.afterEach((to, _from, failure) => {
    if (!failure) pageMemory.remember(to)
})

export default router
