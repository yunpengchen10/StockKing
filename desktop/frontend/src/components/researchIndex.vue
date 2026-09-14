<script setup>
import {computed, h, onBeforeMount, onBeforeUnmount, onMounted,onUnmounted, ref,reactive} from 'vue'
import {GetAIResponseResultList} from "../../wailsjs/go/main/App";
import {NButton, NEllipsis, NText} from "naive-ui";
import ResearchReport from "./researchReport.vue";
import AiRecommendStocksList from "./aiRecommendStocksList.vue";
import PromptTemplateList from "./promptTemplateList.vue";
import AllStockList from "./allStockList.vue";
import AllStockInfoList from "./allStockInfoList.vue";
import CronTaskManager from "./cron-task-manager.vue";
import TradingRecordManager from "./TradingRecordManager.vue";
import StockChangesMonitor from "./stockChangesMonitor.vue";
import MCPServiceManager from "./mcp-server-manager.vue";
import SkillManager from "./skill-manager.vue";
import UplimitLadder from "./uplimitLadder.vue";
import SelectStock from "./SelectStock.vue";
import DailyOperationPlan from "./DailyOperationPlan.vue";
import {EventsOff, EventsOn} from "../../wailsjs/runtime";
import {useRoute} from 'vue-router'


const defaultTab = "AI分析报告"
const availableTabs = new Set(["AI分析报告", "股票推荐记录", "异动监控", "涨停梯队", "提示词模板", "形态选股", "指标选股", "定时任务", "交易日志", "每日操作计划", "MCP服务", "技能管理"])
const nowTab = ref(defaultTab)
const route = useRoute()
onBeforeMount(() => {
  nowTab.value = availableTabs.has(route.query.name) ? route.query.name : defaultTab
})

onBeforeUnmount(() => {
  EventsOff("changeResearchTab")
})

onUnmounted(() => {

});

EventsOn("changeResearchTab", async (msg) => {
  console.log("changeResearchTab", msg)
  updateTab(msg.name)
})
function updateTab(name) {
  nowTab.value = availableTabs.has(name) ? name : defaultTab
}
</script>

<template>
  <main class="research-tools-page">
  <n-card class="research-tools-card" :bordered="false">
    <n-tabs type="line" animated @update-value="updateTab" :value="nowTab" style="--wails-draggable:no-drag">
      <n-tab-pane name="AI分析报告">
        <ResearchReport/>
      </n-tab-pane>
      <n-tab-pane name="股票推荐记录">
        <AiRecommendStocksList/>
      </n-tab-pane>
      <n-tab-pane name="异动监控">
        <StockChangesMonitor/>
      </n-tab-pane>
      <n-tab-pane name="涨停梯队">
        <UplimitLadder/>
      </n-tab-pane>
      <n-tab-pane name="提示词模板">
        <PromptTemplateList/>
      </n-tab-pane>
      <n-tab-pane name="形态选股">
        <AllStockList/>
      </n-tab-pane>
      <n-tab-pane name="指标选股">
        <SelectStock/>
      </n-tab-pane>
      <n-tab-pane name="定时任务">
        <CronTaskManager />
      </n-tab-pane>
      <n-tab-pane name="交易日志">
        <TradingRecordManager />
      </n-tab-pane>
      <n-tab-pane name="每日操作计划">
        <DailyOperationPlan/>
      </n-tab-pane>
<!--      <n-tab-pane name="全部股票信息">-->
<!--        <AllStockInfoList/>-->
<!--      </n-tab-pane>-->
      <n-tab-pane name="MCP服务">
        <MCPServiceManager/>
      </n-tab-pane>
      <n-tab-pane name="技能管理">
        <SkillManager/>
      </n-tab-pane>
    </n-tabs>
  </n-card>
  </main>
</template>

<style scoped>
.research-tools-page{min-height:100%;box-sizing:border-box;padding:16px 18px 90px;background:var(--sk-page-bg);color:var(--sk-text)}.research-tools-card{min-height:calc(100vh - 126px);background:var(--sk-surface)!important;color:var(--sk-text)}.research-tools-card :deep(.n-tabs-tab__label),.research-tools-card :deep(.n-card__content),.research-tools-card :deep(.n-data-table){color:var(--sk-text)!important}.research-tools-card :deep(.n-tab-pane){background:var(--sk-surface);color:var(--sk-text)}
</style>
