<script setup>
import {computed, h, onMounted, ref, reactive} from 'vue'
import {GetAIResponseResultList, DeleteAIResponseResult} from "../../wailsjs/go/main/App";
import {NButton, NEllipsis, NText, useMessage} from "naive-ui";
import {useRouter} from 'vue-router'
import {openStockResearch} from '../utils/symbol'
import {decodeHistoryRecord, renderSafeMarkdown} from '../utils/aiResearch.mjs'



onMounted(() => {
  query({
    page: 1,
    pageSize: paginationReactive.pageSize,
    order: "desc",
    keyword: paginationReactive.keyword,
    startDate: paginationReactive.range[0],
    endDate: paginationReactive.range[1]
  }).then((data) => {
    dataRef.value = data.data
    paginationReactive.page = 1
    paginationReactive.pageCount = data.pageCount
    paginationReactive.itemCount = data.total
    loadingRef.value = false
  })
})
const message = useMessage()
const router = useRouter()
const selectedReport = ref(null)
const showOriginal = ref(false)
const safeReportHTML = computed(() => renderSafeMarkdown(selectedReport.value?.markdown || selectedReport.value?.raw || ''))
const editorDataRef = reactive({
  show: false,
  loading: false,
  darkTheme: false,
  chatId: "",
  modelName: "",
  CreatedAt: "",
  stockName: "",
  stockCode: "",
  question: "",
  content: "",
})
const dataRef = ref([])
const loadingRef = ref(true)
const columnsRef = ref([
  {
    title: '保存时间',
    key: 'CreatedAt',
    render(row, index) {
      //2026-01-14T22:13:27.2693252+08:00 格式化为常用时间格式
      return String(row.CreatedAt || '').substring(0, 19).replace('T', ' ') || '未知'
    }
  },
  {
    title: '模型名称',
    key: 'modelName'
  },
  {
    title: '分析对象',
    key: 'stockName',
    render(row) {
      return h(NButton, {text: true, type: 'primary', onClick: () => openStockResearch(router, row.stockCode, row.stockName)}, {default: () => row.stockName || row.stockCode})
    }
  },
  {
    title: '提示词',
    key: 'question',
    render(row, index) {
      return h(NEllipsis, { tooltip: true ,style: "max-width: 240px;"}, {default: () => h(NText,{type: "info"},{default: () => row.question}),})
    }
  },
  {
    title: '操作',
    render(row, index) {
      return [h(
          NButton,
          {
            strong: true,
            tertiary: true,
            size: 'small',
            type: 'warning', // 橙色按钮
            style: 'font-size: 14px; padding: 0 10px;', // 稍微大一点的按钮
            onClick: () => showReport(row)
          },
          { default: () => '查看' }
      ),
      h(
          NButton,
          {
            strong: true,
            tertiary: true,
            size: 'small',
            type: 'error', // 橙色按钮
            style: 'font-size: 14px; padding: 0 10px;', // 稍微大一点的按钮
            onClick: () => deleteAIResponseResult(row.ID)
          },
          { default: () => '删除' }
      ),
      ]
    }
  },
])
const paginationReactive = reactive({
  page: 1,
  pageCount: 1,
  pageSize: 12,
  itemCount: 0,
  keyword: "",
  startDate:"",
  range: [
    new Date(new Date().getTime() - 3 * 24 * 60 * 60 * 1000), // 前3天
    new Date() // 当天
  ],
  prefix({ itemCount }) {
    return `${itemCount} 条记录`
  }
})
function showReport(row) {
  selectedReport.value = decodeHistoryRecord(row)
  showOriginal.value = false
  editorDataRef.show = true
  editorDataRef.chatId = row.chatId
  editorDataRef.modelName = row.modelName
  editorDataRef.CreatedAt = String(row.CreatedAt || '').substring(0, 19).replace('T', ' ')
  editorDataRef.stockName = row.stockName
  editorDataRef.stockCode = row.stockCode
  editorDataRef.question = row.question
  editorDataRef.content = selectedReport.value.raw
  editorDataRef.loading = false
}

function query({
                 page,
                 pageSize = 10,
                 order = 'desc',
                 keyword = "",
                 startDate = "",
                 endDate = ""
               }) {
  return new Promise((resolve) => {

    GetAIResponseResultList({
      "page": page,
      "pageSize": pageSize,
      "modelName":keyword,
      "question":keyword,
      "stockName":keyword,
      "stockCode":keyword,
      "startDate":startDate,
      "endDate":endDate
    }).then((res) => {
      const pagedData =res.list
      const total = res.total
      const pageCount =res.totalPages
      resolve({
        pageCount,
        data: pagedData,
        total
      })
    })
  })
}

function handlePageChange(currentPage) {
  if (!loadingRef.value) {
    loadingRef.value = true
    query({
      page: currentPage,
      pageSize: paginationReactive.pageSize,
      order: "desc",
      keyword: paginationReactive.keyword,
      startDate: formatDate(paginationReactive.range[0]),
      endDate: formatDate(paginationReactive.range[1])
    }).then((data) => {
      dataRef.value = data.data
      paginationReactive.page = currentPage
      paginationReactive.pageCount = data.pageCount
      paginationReactive.itemCount = data.total
      loadingRef.value = false
    })
  }
}
function handleSearch() {
  if (!loadingRef.value) {
    loadingRef.value = true
    query({
      page: paginationReactive?.page ?? 1,
      pageSize: paginationReactive.pageSize,
      order: "desc",
      keyword: paginationReactive.keyword,
      startDate: formatDate(paginationReactive.range[0]),
      endDate: formatDate(paginationReactive.range[1])
    }).then((data) => {
      dataRef.value = data.data
      paginationReactive.page = data.page
      paginationReactive.pageCount = data.pageCount
      paginationReactive.itemCount = data.total
      loadingRef.value = false
    })
  }
}
function saveAsMarkdown(code,name) {
  const item = selectedReport.value
  if (!item) return
  const url = URL.createObjectURL(new Blob([item.raw], {type:'text/plain;charset=utf-8'}))
  const link = document.createElement('a')
  link.href = url
  link.download = `${code || 'research'}-报告.${item.format === 'json' ? 'json' : 'md'}`
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function copyToClipboard() {
  try {
    await navigator.clipboard.writeText(editorDataRef.content);
    message.success('分析结果已复制到剪切板');
  } catch (err) {
    message.error('复制失败: ' + err);
  }
}
function formatDate(dateString) {
  const date = new Date(dateString)
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  // const hours = String(date.getHours()).padStart(2, '0')
  // const minutes = String(date.getMinutes()).padStart(2, '0')
  // const seconds = String(date.getSeconds()).padStart(2, '0')
  //return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`
  return `${year}-${month}-${day}`
}

function deleteAIResponseResult(id){
  DeleteAIResponseResult(id).then(result => {
    if(result !== ""){
      message.success(result)
    }
    handleSearch()
  })
}
</script>

<template>
  <n-input-group>
    <n-date-picker  v-model:value="paginationReactive.range" type="daterange"   style="width: 50%"/>
    <n-input clearable placeholder="输入关键词搜索" v-model:value="paginationReactive.keyword"/>
    <n-button type="primary" ghost @click="handleSearch"  @input="handleSearch">
      搜索
    </n-button>
  </n-input-group>
        <n-data-table
            remote
            size="small"
            :columns="columnsRef"
            :data="dataRef"
            :loading="loadingRef"
            :pagination="paginationReactive"
            :row-key="(rowData)=>rowData.ID"
            @update:page="handlePageChange"
            flex-height
            style="height: calc(100vh - 210px);margin-top: 10px"
        />



  <n-modal transform-origin="center" v-model:show="editorDataRef.show" preset="card" style="width: 800px;max-width: calc(100vw - 32px);"
           :title="'['+editorDataRef.stockName+']AI分析'">
    <n-spin size="small" :show="editorDataRef.loading">
      <div v-if="selectedReport" class="report-provenance">
        <strong :title="`保存 ${editorDataRef.CreatedAt}`">{{ selectedReport.source === 'import' ? '导入' : selectedReport.source === 'api' ? 'API' : '历史' }} · {{ selectedReport.sourceName }}</strong>
        <p>{{ selectedReport.stockCode || editorDataRef.stockCode }} · {{ selectedReport.analyzedAt || '时点未知' }} · {{ selectedReport.expiresAt ? `有效至 ${selectedReport.expiresAt}` : '有效期未知' }} <span v-if="selectedReport.expiresAt && new Date(selectedReport.expiresAt).getTime() < Date.now()" class="expired-tag">已过期</span></p>
        <details v-if="selectedReport.warnings?.length"><summary>来源详情</summary><p>{{ selectedReport.warnings.join('；') }}</p></details>
      </div>
      <textarea v-if="showOriginal" :value="selectedReport?.raw || ''" readonly class="report-original" aria-label="历史报告原文" />
      <article v-else class="safe-report-preview" v-html="safeReportHTML" />
    </n-spin>
    <template #action>
      <n-flex justify="right">
        <n-button size="tiny" @click="showOriginal = !showOriginal">{{ showOriginal ? '阅读报告' : '查看原文' }}</n-button>
        <n-button size="tiny" type="primary" @click="copyToClipboard">复制</n-button>
        <n-button size="tiny" type="primary" @click="saveAsMarkdown(editorDataRef.stockCode,editorDataRef.stockName)">导出原文</n-button>
      </n-flex>
    </template>
  </n-modal>
</template>

<style scoped>
.report-provenance details{margin-top:5px}.report-provenance summary{cursor:pointer;font-size:10px}.expired-tag{color:var(--sk-warning,#d9ac62);margin-left:5px}
.report-provenance{padding:12px;background:var(--sk-surface-2);border:1px solid var(--sk-border);border-radius:6px;margin-bottom:12px;color:var(--sk-text-muted);font-size:11px;line-height:1.7}.report-provenance strong{color:var(--sk-text)}.report-provenance p{margin:5px 0}.report-original,.safe-report-preview{height:540px;max-height:60vh;overflow:auto;text-align:left;color:var(--sk-text);background:var(--sk-surface);font-size:13px;line-height:1.85;overflow-wrap:anywhere}.report-original{width:100%;box-sizing:border-box;padding:12px;border:1px solid var(--sk-border);border-radius:5px;resize:vertical;font-family:var(--sk-font-mono,Consolas,monospace)}.safe-report-preview :deep(a){color:var(--sk-accent)}.safe-report-preview :deep(pre){white-space:pre-wrap;padding:12px;background:var(--sk-surface-2);border-radius:5px;font-size:11px}.safe-report-preview :deep(table){border-collapse:collapse;display:block;overflow:auto;font-size:11px}.safe-report-preview :deep(th),.safe-report-preview :deep(td){border:1px solid var(--sk-border);padding:6px 10px}.safe-report-preview :deep(blockquote){border-left:2px solid var(--sk-border);padding-left:12px;color:var(--sk-text-muted)}.safe-report-preview :deep(h1){font-size:23px}.safe-report-preview :deep(h2){font-size:18px;margin-top:24px}
</style>
