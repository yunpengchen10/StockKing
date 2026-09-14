<script setup>
import { h, onBeforeMount, onMounted, ref, reactive } from 'vue'
import {
  GetAllStockInfoList,
  GetAllMarkets,
  GetAllIndustries,
  GetAllConcepts,
  GetConfig
} from "../../wailsjs/go/main/App";
import {
  NButton,
  NInput,
  NSelect,
  NTag,
  NText,
  useMessage,
  useNotification,
  NDataTable,
  NSpace,
  NPagination,
  NCard,
  NGrid,
  NGridItem,
  NForm,
  NFormItem
} from "naive-ui";
import sparkLine from "./stockSparkLine.vue"

const notify = useNotification()
const message = useMessage()

const editorDataRef = reactive({
  darkTheme: false
})

onBeforeMount(() => {
  GetConfig().then(result => {
    if (result.darkTheme) {
      editorDataRef.darkTheme = true
    }
  })
})

onMounted(() => {
  console.log('all-stock-info-list mounted')
  loadStocks(1, paginationReactive.pageSize)
  loadFilters()
})

const dataRef = ref([])
const loadingRef = ref(false)
const marketsRef = ref([])
const industriesRef = ref([])
const conceptsRef = ref([])

const columnsRef = ref([
  {
    title: '股票代码',
    key: 'SECUCODE',
    width: 120,
    render(row) {
      return h(NText, { type: "info" }, { default: () => row.SECUCODE })
    }
  },
  {
    title: '股票名称',
    key: 'SECURITY_NAME_ABBR',
    width: 120,
    render(row) {
      return h(NText, { type: "error" }, { default: () => row.SECURITY_NAME_ABBR })
    }
  },
  {
    title: '最新价',
    key: 'NEW_PRICE',
    width: 100,
    render(row) {
      const price = parseFloat(row.NEW_PRICE) || 0
      return h(NText, { type: "info" }, { default: () => price.toFixed(2) })
    }
  },
  {
    title: '涨跌幅(%)',
    key: 'CHANGE_RATE',
    width: 120,
    render(row) {
      const rate = parseFloat(row.CHANGE_RATE) || 0
      const type = rate >= 0 ? 'error' : 'success'
      const sign = rate >= 0 ? '+' : ''
      return h(NText, { type: type }, { default: () => `${sign}${rate.toFixed(2)}%` })
    }
  },
  // {
  //   title: '分时图',
  //   key: 'sparkline',
  //   width: 120,
  //   render(row) {
  //     const secucode = row.SECUCODE || row.SECURITY_CODE
  //     return h(sparkLine, {
  //       idSuffix: secucode,
  //       stockName: row.SECURITY_NAME_ABBR,
  //       stockCode: secucode,
  //       lastPrice: parseFloat(row.NEW_PRICE) || 0,
  //       openPrice: parseFloat(row.PRE_CLOSE_PRICE) || 0,
  //       tooltip: true
  //     })
  //   }
  // },
  // {
  //   title: '最高价',
  //   key: 'HIGH_PRICE',
  //   width: 100,
  //   render(row) {
  //     const price = parseFloat(row.HIGH_PRICE) || 0
  //     return h(NText, { type: "info" }, { default: () => price.toFixed(2) })
  //   }
  // },
  // {
  //   title: '最低价',
  //   key: 'LOW_PRICE',
  //   width: 100,
  //   render(row) {
  //     const price = parseFloat(row.LOW_PRICE) || 0
  //     return h(NText, { type: "info" }, { default: () => price.toFixed(2) })
  //   }
  // },
  // {
  //   title: '成交量',
  //   key: 'VOLUME',
  //   width: 120,
  //   render(row) {
  //     const volume = parseInt(row.VOLUME) || 0
  //     let displayVolume = volume
  //     if (volume >= 100000000) {
  //       displayVolume = (volume / 100000000).toFixed(2) + '亿'
  //     } else if (volume >= 10000) {
  //       displayVolume = (volume / 10000).toFixed(2) + '万'
  //     }
  //     return h(NText, { type: "info" }, { default: () => displayVolume })
  //   }
  // },
  // {
  //   title: '成交额',
  //   key: 'DEAL_AMOUNT',
  //   width: 120,
  //   render(row) {
  //     const amount = parseFloat(row.DEAL_AMOUNT) || 0
  //     let displayAmount = amount
  //     if (amount >= 100000000) {
  //       displayAmount = (amount / 100000000).toFixed(2) + '亿'
  //     } else if (amount >= 10000) {
  //       displayAmount = (amount / 10000).toFixed(2) + '万'
  //     }
  //     return h(NText, { type: "info" }, { default: () => displayAmount })
  //   }
  // },
  // {
  //   title: '换手率(%)',
  //   key: 'TURNOVERRATE',
  //   width: 100,
  //   render(row) {
  //     const rate = parseFloat(row.TURNOVERRATE) || 0
  //     return h(NText, { type: "info" }, { default: () => rate.toFixed(2) + '%' })
  //   }
  // },
  // {
  //   title: '量比',
  //   key: 'VOLUME_RATIO',
  //   width: 80,
  //   render(row) {
  //     const ratio = parseFloat(row.VOLUME_RATIO) || 0
  //     return h(NText, { type: "info" }, { default: () => ratio.toFixed(2) })
  //   }
  // },
  {
    title: '所属行业',
    key: 'INDUSTRY',
    width: 120,
    render(row) {
      return h(NTag, { type: "primary", size: "small" }, { default: () => row.INDUSTRY || '无' })
    }
  },
  {
    title: '所属概念',
    key: 'CONCEPT',
    width: 150,
    render(row) {
      return h(NText, { type: "info", size: "small" }, { default: () => row.CONCEPT || '无' })
    }
  },
  {
    title: '交易所',
    key: 'MARKET',
    width: 100,
    render(row) {
      return h(NTag, { type: "warning", size: "small" }, { default: () => row.MARKET || '未知' })
    }
  },
  {
    title: '数据日期',
    key: 'MAX_TRADE_DATE',
    width: 120,
    render(row) {
      return h(NText, { type: "secondary" }, { default: () => row.MAX_TRADE_DATE || '' })
    }
  }
])

const paginationReactive = reactive({
  page: 1,
  pageCount: 1,
  pageSize: 12,
  itemCount: 0,
  prefix({ itemCount }) {
    return `${itemCount} 只股票`
  }
})

const searchFormRef = reactive({
  securityCode: '',
  securityName: '',
  market: null,
  industry: null,
  concept: null,
  minPrice: '',
  maxPrice: '',
  minChange: '',
  maxChange: ''
})

function loadStocks(page, pageSize) {
  if (!loadingRef.value) {
    loadingRef.value = true
    const query = {
      page: page,
      pageSize: pageSize,
      securityCode: searchFormRef.securityCode,
      securityName: searchFormRef.securityName,
      market: searchFormRef.market,
      industry: searchFormRef.industry,
      concept: searchFormRef.concept,
    }
    
    GetAllStockInfoList(query).then((res) => {
      console.log('GetAllStockInfoList result:', res)
      if (res &&  res.list) {
        dataRef.value = res.list || []
        paginationReactive.page = res.page || 1
        paginationReactive.pageCount = res.totalPages || 1
        paginationReactive.itemCount = res.total || 0
      } else {
        dataRef.value = []
        paginationReactive.page = 1
        paginationReactive.pageCount = 1
        paginationReactive.itemCount = 0
        message.error('获取股票数据失败: ' + (res?.message || '未知错误'))
      }
      loadingRef.value = false
    }).catch(err => {
      message.error('获取股票数据失败: ' + err.message)
      loadingRef.value = false
    })
  }
}

function loadFilters() {
  // 加载交易所列表
  GetAllMarkets().then(res => {
    console.log('GetAllMarkets result:', res)
    if (res && res.length > 0) {
      marketsRef.value = (res || []).map(market => ({
        label: market,
        value: market
      }))
    }
  }).catch(err => {
    console.error('加载交易所列表失败:', err)
  })

  // 加载行业列表
  GetAllIndustries().then(res => {
    console.log('GetAllIndustries result:', res)
    if (res && res.length > 0) {
      industriesRef.value = (res || []).map(industry => ({
        label: industry,
        value: industry
      }))
    }
  }).catch(err => {
    console.error('加载行业列表失败:', err)
  })
}

function handlePageChange(currentPage) {
  loadStocks(currentPage, paginationReactive.pageSize)
}

function handlePageSizeChange(pageSize) {
  paginationReactive.pageSize = pageSize
  loadStocks(1, pageSize)
}

function handleSearch() {
  loadStocks(1, paginationReactive.pageSize)
}

function handleReset() {
  searchFormRef.securityCode = ''
  searchFormRef.securityName = ''
  searchFormRef.market = null
  searchFormRef.industry = null
  searchFormRef.concept = null
  loadStocks(1, paginationReactive.pageSize)
}
</script>

<template>
  <div>
    <!-- 搜索条件区域 -->
    <n-card  size="small" style="margin-bottom: 16px;text-align: left">
      <n-grid :cols="5" :x-gap="12" :y-gap="12">
        <n-grid-item>
          <n-form-item label="交易所" label-placement="left">
            <n-select
                v-model:value="searchFormRef.market"
                :options="marketsRef"
                placeholder="请选择交易所"
                clearable
            />
          </n-form-item>
        </n-grid-item>
        <n-grid-item>
          <n-form-item label="所属行业" label-placement="left">
            <n-select
                v-model:value="searchFormRef.industry"
                :options="industriesRef"
                placeholder="请选择行业"
                clearable
                style="width: 200px"
            />
          </n-form-item>
        </n-grid-item>
        <n-grid-item>
          <n-form-item label="股票名称/代码" label-placement="left">
            <n-input 
              v-model:value="searchFormRef.securityName" 
              placeholder="请输入股票名称/代码"
              clearable
            />
          </n-form-item>
        </n-grid-item>
        <n-grid-item>
          <n-form-item label="所属概念" label-placement="left">
            <n-input
              v-model:value="searchFormRef.concept"
              :options="conceptsRef"
              placeholder="请输入概念关键词"
              clearable
            />
          </n-form-item>
        </n-grid-item>
        <n-grid-item>
          <n-space>
            <n-button type="primary" @click="handleSearch">搜索</n-button>
            <n-button @click="handleReset">重置</n-button>
          </n-space>
        </n-grid-item>
      </n-grid>
    </n-card>

    <!-- 数据表格 -->
    <n-data-table
      remote
      size="small"
      :columns="columnsRef"
      :data="dataRef"
      :loading="loadingRef"
      :pagination="paginationReactive"
      :row-key="(rowData) => rowData.ID"
      flex-height
      style="height: calc(100vh - 270px)"
      @update:page="handlePageChange"
    />

    <!-- 分页控件 -->
<!--    <div style="margin-top: 16px; display: flex; justify-content: center;">-->
<!--      <n-pagination-->
<!--        v-model:page="paginationReactive.page"-->
<!--        v-model:page-size="paginationReactive.pageSize"-->
<!--        :page-count="paginationReactive.pageCount"-->
<!--        :item-count="paginationReactive.itemCount"-->
<!--        :page-sizes="[10, 20, 50, 100]"-->
<!--        show-size-picker-->
<!--        show-quick-jumper-->
<!--        @update:page="handlePageChange"-->
<!--        @update:page-size="handlePageSizeChange"-->
<!--      />-->
<!--    </div>-->
  </div>
</template>

<style scoped>
</style>
