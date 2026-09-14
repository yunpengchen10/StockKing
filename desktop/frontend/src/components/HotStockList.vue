<script setup lang="ts">
import {onBeforeMount, onBeforeUnmount, ref} from 'vue'
import {HotStock, IsTradingTime} from "../../wailsjs/go/main/App";
import KLineChart from "./KLineChart.vue";
import {ArrowDown, ArrowUp} from "@vicons/ionicons5";
import {useRouter} from 'vue-router'
import {openStockResearch} from '../utils/symbol'

const {marketType}=defineProps(
    {
      marketType: {
        type: String,
        default: '10'
      }
    }
)
const task =ref()
const checkTask = ref()
const list  = ref([])
const loading = ref(false)
const errorMsg = ref('')
const router = useRouter()

async function fetchHotStock() {
  try {
    loading.value = true
    errorMsg.value = ''
    const res = await HotStock(marketType)
    list.value = res || []
    if (list.value.length === 0) {
      errorMsg.value = '暂无数据，可能为API请求失败或休市'
    }
  } catch (e) {
    console.error('fetchHotStock error:', e)
    errorMsg.value = '请求失败: ' + (e?.message || e || '未知错误')
  } finally {
    loading.value = false
  }
}

function startRefresh() {
  stopRefresh()
  fetchHotStock()
  task.value = setInterval(fetchHotStock, 5000)
  checkTask.value = setInterval(() => {
    IsTradingTime().then(trading => {
      if (!trading) {
        stopRefresh()
        startCheckLoop()
      }
    }).catch(() => {})
  }, 60000)
}

function startCheckLoop() {
  stopCheck()
  checkTask.value = setInterval(() => {
    IsTradingTime().then(trading => {
      if (trading) {
        stopCheck()
        startRefresh()
      }
    }).catch(() => {})
  }, 60000)
}

function stopRefresh() {
  if (task.value) {
    clearInterval(task.value)
    task.value = null
  }
}

function stopCheck() {
  if (checkTask.value) {
    clearInterval(checkTask.value)
    checkTask.value = null
  }
}

onBeforeMount(async () => {
  const trading = await IsTradingTime().catch(() => true)
  if (trading) {
    startRefresh()
  } else {
    fetchHotStock()
    startCheckLoop()
  }
})

onBeforeUnmount(()=>{
  stopRefresh()
  stopCheck()
})

function getMarketCode(item) {
  if (item.exchange	 === 'SZ') {
    return item.code.toLowerCase()
  }
  if (item.exchange	 === 'SH') {
    return item.code.toLowerCase()
  }
  if (item.exchange	 === 'HK') {
    return (item.exchange + item.code).toLowerCase()
  }
  return ("gb_"+item.code).toLowerCase()
}
</script>

<template>
  <n-spin :show="loading" size="small">
    <n-alert v-if="errorMsg && list.length === 0" type="warning" style="margin-bottom: 10px" :bordered="false">
      <template #header>
        {{ errorMsg }}
        <n-button size="tiny" type="primary" @click="fetchHotStock" style="margin-left: 10px">重试</n-button>
      </template>
    </n-alert>
    <n-table striped size="small">
      <n-thead>
        <n-tr>
          <n-th>股票名称</n-th>
          <n-th>涨跌幅</n-th>
          <n-th>当前价格</n-th>
          <n-th>热度</n-th>
          <n-th>热度变化</n-th>
          <n-th>排名变化</n-th>
        </n-tr>
      </n-thead>
      <n-tbody>
        <n-tr v-for="item in list" :key="item.code">
          <n-td><n-text type="info">
            <n-popover trigger="hover" placement="right">
              <template #trigger>
                <n-tag type="info" :bordered="false" style="cursor:pointer" @click="openStockResearch(router, getMarketCode(item), item.name)">  {{item.name}} {{item.code}}</n-tag>
              </template>
              <k-line-chart style="width: 800px" :code="getMarketCode(item)" :chart-height="500" :stockName="item.name" :k-days="20" :dark-theme="true"></k-line-chart>
            </n-popover>
          </n-text></n-td>
          <n-td><n-text :type="item.percent>0?'error':'success'">{{item.percent}}%</n-text></n-td>
          <n-td><n-text type="info">{{item.current}}</n-text></n-td>
          <n-td><n-text type="info">{{item.value}}</n-text></n-td>
          <n-td><n-text  :type="item.increment>0?'error':'success'">
            {{item.increment}}
            <n-icon v-if="item.increment>0" :component="ArrowUp"/>
            <n-icon v-else :component="ArrowDown"/>
          </n-text></n-td>
          <n-td>
            <n-text  :type="item.rank_change>0?'error':'success'">
              {{item.rank_change}}
              <n-icon v-if="item.rank_change>0" :component="ArrowUp"/>
              <n-text v-else-if="item.rank_change==0" ></n-text>
              <n-icon v-else :component="ArrowDown"/>
            </n-text>
          </n-td>
        </n-tr>
      </n-tbody>
    </n-table>
  </n-spin>
</template>

<style scoped>

</style>
