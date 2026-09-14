<script setup>
import { onMounted, ref } from 'vue'
import { GetStockKingAIBudget, SaveStockKingAIBudget } from '../../wailsjs/go/main/App'
const data=ref(null), error=ref(''), saving=ref(false)
async function refresh(){try{data.value=await GetStockKingAIBudget();error.value=''}catch(e){error.value=String(e)}}
async function save(){saving.value=true;try{await SaveStockKingAIBudget(data.value.settings);await refresh()}catch(e){error.value=String(e)}finally{saving.value=false}}
onMounted(refresh)
defineExpose({refresh})
</script>
<template>
 <details class="economy-budget"><summary>经济模式 · 用量与预算 <span v-if="data">今日 {{data.usedRequests}} / {{data.settings.daily_limit}} 次</span></summary>
  <p>精选复审与单股 AI 研究共享额度；旧聊天助手等其他入口不计入此额度。后台本地计算不消耗 AI token。</p>
  <p v-if="error">{{error}}</p>
  <div v-if="data" class="budget-fields">
   <label>每日新请求上限<n-input-number v-model:value="data.settings.daily_limit" :min="1" :max="1000" /></label>
   <label>输入 token 上限<n-input-number v-model:value="data.settings.input_tokens" :min="1" :max="32000" /></label>
   <label>输出 token 上限<n-input-number v-model:value="data.settings.output_tokens" :min="1" :max="8000" /></label>
   <label>输入单价 / 百万 token<n-input-number v-model:value="data.settings.input_price_per_million" :min="0" placeholder="可留空" /></label>
   <label>输出单价 / 百万 token<n-input-number v-model:value="data.settings.output_price_per_million" :min="0" placeholder="可留空" /></label>
  </div>
  <p>单价采用你当前平台的同一币种，仅用于估算，切换平台后请核对；以平台账单为准。已发送后失败或取消仍占一次，缓存命中不占次数。</p>
  <n-space><n-button size="small" :loading="saving" @click="save" :disabled="!data">保存额度</n-button><n-button size="small" @click="refresh">更新用量</n-button></n-space>
  <p v-for="r in (data?.records || []).slice(0,10)" :key="r.id">{{r.model || '缓存'}} · {{r.status}} · token {{r.usage?.total_tokens ?? '未知'}} · {{r.completed ? Math.round(r.completed-r.started)+'秒' : '尚未结束'}}</p>
 </details>
</template>
<style scoped>.economy-budget{padding:12px;margin:12px 0;border:1px solid var(--sk-border);border-radius:8px;font-size:12px}.budget-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}summary{cursor:pointer}p{color:var(--sk-text-muted)}</style>
