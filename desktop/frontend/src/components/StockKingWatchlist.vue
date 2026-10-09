<script setup>
import { computed, h, inject, onBeforeMount, onBeforeUnmount, ref, watch } from 'vue'
import { usePageActive } from '../utils/pageSession.mjs'
import { useRouter } from 'vue-router'
import { NButton, NInput, NSelect, NTag, useDialog, useMessage } from 'naive-ui'
import {
  AddGroup,
  UpdateGroup,
  RemoveGroup,
  RefreshStockKingWatchlist,
  AddStockGroup,
  Follow,
  GetAllGroupStocks,
  GetFollowList,
  GetGroupList,
  GetStockList,
  RemoveStockGroup,
  SetStockSort,
  UnFollow,
} from '../../wailsjs/go/main/App'
import { EventsOn } from '../../wailsjs/runtime/runtime'
import { watchReturn, priceText } from '../utils/watchlist.mjs'
import StockKingIcon from './StockKingIcon.vue'

const router = useRouter()
const pageActive = usePageActive()
const message = useMessage()
const dialog = useDialog()
const darkTheme = inject('appDarkTheme', ref(true))
const loading = ref(false)
const addLoading = ref(false)
const query = ref('')
const options = ref([])
const searchRows = ref([])
const selected = ref(null)
const rows = ref([])
const groups = ref([])
const memberships = ref([])
const selectedGroupId = ref(0)
const newGroupName = ref('')
const groupLoading = ref(false)
const managingGroups = ref(false)
let unsubscribePrice = null
let quoteRefreshTimer = null
let searchTimer = null
let searchSequence = 0

const emptyText = computed(() => loading.value ? '正在读取本地自选…' : '还没有自选股票')
const groupOptions = computed(() => groups.value.map(group => ({
  label: group.name,
  value: Number(group.ID),
})))

function normalizeInternalCode(code) {
  const raw = String(code || '').trim()
  if (!raw) return ''
  if (/^(sh|sz|bj|hk)\w+/i.test(raw) || /^gb_/i.test(raw)) return raw.toLowerCase()
  const suffix = raw.match(/^(.+)\.(SH|SZ|BJ|HK|US)$/i)
  if (suffix) {
    const symbol = suffix[1]
    const market = suffix[2].toUpperCase()
    if (market === 'US') return `gb_${symbol.toLowerCase()}`
    return `${market.toLowerCase()}${symbol.toLowerCase()}`
  }
  if (/^\d{6}$/.test(raw)) {
    if (/^[56]/.test(raw)) return `sh${raw}`
    if (/^[489]/.test(raw)) return `bj${raw}`
    return `sz${raw}`
  }
  if (/^\d{5}$/.test(raw)) return `hk${raw}`
  if (/^[a-z.]+$/i.test(raw)) return `gb_${raw.toLowerCase()}`
  return raw.toLowerCase()
}

function toResearchCode(code) {
  const raw = String(code || '').toLowerCase()
  if (raw.startsWith('gb_')) return `${raw.slice(3).toUpperCase()}.US`
  if (raw.startsWith('sh')) return `${raw.slice(2)}.SH`
  if (raw.startsWith('sz')) return `${raw.slice(2)}.SZ`
  if (raw.startsWith('bj')) return `${raw.slice(2)}.BJ`
  if (raw.startsWith('hk')) return `${raw.slice(2)}.HK`
  return raw.toUpperCase()
}

function displayCode(code) {
  return toResearchCode(code)
}

function rowValue(row, key, fallback = '') {
  return row?.[key] ?? row?.[key.charAt(0).toLowerCase() + key.slice(1)] ?? fallback
}

async function loadWatchlist(refresh = false) {
  loading.value = true
  try {
    rows.value = (await (refresh ? RefreshStockKingWatchlist : GetFollowList)(Number(selectedGroupId.value || 0))) || []
  } catch (error) {
    rows.value = (await GetFollowList(Number(selectedGroupId.value || 0))) || []
    message.error(`行情更新失败，保留上次报价：${error?.message || error}`)
  } finally {
    loading.value = false
  }
}

async function loadGroups() {
  const [groupRows, membershipRows] = await Promise.all([GetGroupList(), GetAllGroupStocks()])
  groups.value = groupRows || []
  memberships.value = membershipRows || []
}

function rowGroupIds(row) {
  const code = String(rowValue(row, 'StockCode')).toLowerCase()
  return memberships.value
    .filter(item => String(item.stockCode || '').toLowerCase() === code)
    .map(item => Number(item.groupId))
}

async function updateRowGroups(row, nextIds) {
  const code = rowValue(row, 'StockCode')
  const name = rowValue(row, 'Name', code)
  const current = rowGroupIds(row)
  const additions = (nextIds || []).filter(id => !current.includes(Number(id)))
  const removals = current.filter(id => !(nextIds || []).map(Number).includes(id))
  try {
    for (const id of additions) {
      const result = await AddStockGroup(Number(id), code)
      if (result !== '添加成功') throw new Error(result)
    }
    for (const id of removals) {
      const result = await RemoveStockGroup(code, name, Number(id))
      if (result !== '移除成功') throw new Error(result)
    }
    await loadGroups()
    if (selectedGroupId.value && removals.includes(Number(selectedGroupId.value))) await loadWatchlist()
    message.success('分组已保存')
  } catch (error) {
    message.error(`保存分组失败：${error?.message || error}`)
  }
}

async function createGroup() {
  const name = newGroupName.value.trim()
  if (!name || [...name].length > 30 || name === "全部") { message.warning("分组名称须为1至30个字符，不能为全部"); return }
  if (groups.value.some(group => group.name.toLowerCase() === name.toLowerCase())) {
    message.info('该分组已存在')
    return
  }
  groupLoading.value = true
  try {
    const result = await AddGroup({name, sort: groups.value.length + 1})
    if (result !== '添加成功') throw new Error(result)
    newGroupName.value = ''
    await loadGroups()
    message.success('分组已创建并保存')
  } catch (error) {
    message.error(`创建分组失败：${error?.message || error}`)
  } finally {
    groupLoading.value = false
  }
}

function renameGroup(group) {
 let name=group.name
 dialog.create({title:'重命名分组',content:()=>h(NInput,{defaultValue:name,maxlength:30,'onUpdate:value':value=>{name=value}}),positiveText:'保存',negativeText:'取消',async onPositiveClick(){
  const result=await UpdateGroup(Number(group.ID),name.trim());if(result!=='修改成功'){message.error(result);return false}await loadGroups()
 }})
}
function deleteGroup(group) {
 const count=new Set(memberships.value.filter(m=>Number(m.groupId)===Number(group.ID)).map(m=>m.stockCode)).size
 dialog.warning({title:'删除分组',content:`删除“${group.name}”？组内 ${count} 只股票仍保留在全部自选。`,positiveText:'删除分组',negativeText:'取消',async onPositiveClick(){
  const result=await RemoveGroup(Number(group.ID));if(result!=='移除成功'){message.error(result);return false}
  if(selectedGroupId.value===Number(group.ID))selectedGroupId.value=0
  await loadGroups();await loadWatchlist()
 }})
}

async function selectGroup(groupId) {
  selectedGroupId.value = Number(groupId || 0)
  await loadWatchlist()
}

async function moveRow(row, direction) {
  const index = rows.value.indexOf(row)
  const targetIndex = index + direction
  if (index < 0 || targetIndex < 0 || targetIndex >= rows.value.length) return
  const targetSort = Number(rowValue(rows.value[targetIndex], 'Sort', targetIndex + 1))
  await SetStockSort(targetSort, rowValue(row, 'StockCode'))
  await loadWatchlist()
}

function searchStocks(value) {
  const keyword = String(value || '').trim()
  selected.value = null
  if (searchTimer) clearTimeout(searchTimer)
  if (!keyword) {
    options.value = []
    searchRows.value = []
    return
  }
  const sequence = ++searchSequence
  searchTimer = setTimeout(async () => {
    try {
      const result = ((await GetStockList(keyword)) || []).slice(0, 40)
      if (sequence !== searchSequence) return
      searchRows.value = result
      options.value = result.map(item => ({
        label: `${item.name || item.fullname || item.ts_code} · ${item.ts_code}`,
        value: item.ts_code,
      }))
    } catch (error) {
      message.error(`搜索失败：${error?.message || error}`)
    }
  }, 180)
}

function selectStock(code) {
  const stock = searchRows.value.find(item => item.ts_code === code)
  selected.value = stock || { ts_code: code, name: code }
  query.value = stock ? `${stock.name || stock.fullname} · ${stock.ts_code}` : code
}

async function addStock() {
  if (addLoading.value) return
  let stock = selected.value
  if (!stock) {
    const keyword = String(query.value || '').split('·').pop().trim()
    const candidates = (await GetStockList(keyword)) || []
    stock = candidates.find(item => item.ts_code?.toLowerCase() === keyword.toLowerCase()) || candidates[0]
  }
  if (!stock?.ts_code) {
    message.warning('请先从搜索结果中选择一只股票')
    return
  }
  const internalCode = normalizeInternalCode(stock.ts_code)
  addLoading.value = true
  try {
    const result = await Follow(internalCode)
    if (result !== '关注成功' && result !== '已经关注了') throw new Error(result)
    if (selectedGroupId.value) {
      const saved = await AddStockGroup(selectedGroupId.value, internalCode)
      if (saved !== '添加成功') throw new Error(saved)
      await loadGroups()
    }
    await loadWatchlist()
    selected.value = null
    query.value = ''
    options.value = []
    message.success('已添加并保存到本地自选')
  } catch (error) {
    message.error(`添加失败：${error?.message || error}`)
  } finally {
    addLoading.value = false
  }
}

function removeStock(row) {
  const code = rowValue(row, 'StockCode')
  const name = rowValue(row, 'Name', code)
  dialog.warning({
    title: '移出自选',
    content: `确定将 ${name} 移出自选吗？`,
    positiveText: '移出',
    negativeText: '取消',
    async onPositiveClick() {
      try {
        await UnFollow(code)
        await loadWatchlist()
        message.success('已移出自选')
      } catch (error) {
        message.error(`移出失败：${error?.message || error}`)
      }
    },
  })
}

function openResearch(row) {
  router.push({
    name: 'klineAnalysis',
    query: {
      code: toResearchCode(rowValue(row, 'StockCode')),
      name: rowValue(row, 'Name', displayCode(rowValue(row, 'StockCode'))),
    },
  })
}

const columns = [
  { title: '股票', key: 'Name', width: 160, render: row => h('div', { class: 'stock-name' }, [
    h('strong', rowValue(row, 'Name', displayCode(rowValue(row, 'StockCode')))),
    h('span', displayCode(rowValue(row, 'StockCode'))),
  ]) },
  { title: '选入价', key: 'SelectionPrice', width: 140, render: row => h('div', { title: row.SelectionQuoteTime || (row.SelectionStatus ? '本次添加未取得有效行情' : '升级前未记录') }, [priceText(row.SelectionPrice), h('small', {style:'display:block'}, row.SelectionQuoteTime || (row.SelectionStatus ? '未记录' : '升级前未记录'))]) },
  { title: '最新价', key: 'Price', width: 165, render: row => h('div', {}, [priceText(row.Price), h('small',{style:'display:block'},`${row.LatestQuoteTime || '报价时间未知'}${row.LatestQuoteStatus === 'stale' ? ' · 更新失败' : ''}`)]) },
  { title: '至今收益率', key: 'selectionReturn', width: 125, sorter: (a,b) => (watchReturn(a) ?? -Infinity)-(watchReturn(b) ?? -Infinity), render: row => {
    const value=watchReturn(row)
    return h('span',{style:{color:value>0?'#d43838':value<0?'#16854b':'inherit'},title:'原币种未复权价格涨跌幅，不含分红、费用和汇率；不是实际持仓盈利'},value==null?'—':`${value>0?'+':''}${value.toFixed(2)}%`)
  } },
  { title: '分组', key: 'groups', width: 230, render: row => h(NSelect, {
    value: rowGroupIds(row),
    options: groupOptions.value,
    multiple: true,
    clearable: true,
    size: 'small',
    placeholder: '选择分组',
    'onUpdate:value': value => updateRowGroups(row, value),
  }) },
  { title: '选入时间', key: 'Time', width: 190, render: row => {
    const value = rowValue(row, 'Time')
    return value ? new Date(value).toLocaleString('zh-CN') : '—'
  } },
  { title: '操作', key: 'actions', width: 240, render: row => h('div', { class: 'actions' }, [
    h(NButton, { size: 'tiny', tertiary: true, disabled: rows.value.indexOf(row) === 0, onClick: () => moveRow(row, -1) }, { default: () => '上移' }),
    h(NButton, { size: 'tiny', tertiary: true, disabled: rows.value.indexOf(row) === rows.value.length - 1, onClick: () => moveRow(row, 1) }, { default: () => '下移' }),
    h(NButton, { size: 'small', type: 'primary', secondary: true, onClick: () => openResearch(row) }, { default: () => '研究' }),
    h(NButton, { size: 'small', type: 'error', tertiary: true, onClick: () => removeStock(row) }, { default: () => '移出' }),
  ]) },
]

onBeforeMount(() => {
  unsubscribePrice = EventsOn("stock_price", () => { if (pageActive.value && !quoteRefreshTimer) quoteRefreshTimer=setTimeout(() => { quoteRefreshTimer=null; if (pageActive.value) loadWatchlist() },1000) })
  Promise.all([loadGroups(), loadWatchlist(true)]).catch(error => message.error(`初始化自选失败：${error?.message || error}`))
})
onBeforeUnmount(() => { unsubscribePrice?.(); clearTimeout(quoteRefreshTimer); if (searchTimer) clearTimeout(searchTimer); searchSequence++ })
watch(pageActive, active => { if (active) void Promise.all([loadGroups(), loadWatchlist()]) })
</script>

<template>
  <main class="watchlist-page" :class="{ dark: darkTheme }">
    <section class="watchlist-toolbar">
      <div>

        <h1 class="sk-page-heading">自选</h1>

      </div>
      <div class="add-stock">
        <n-auto-complete
          v-model:value="query"
          :options="options"
          placeholder="搜索名称、代码或拼音"
          clearable
          :on-select="selectStock"
          @update:value="searchStocks"
          @keyup.enter="addStock"
        />
        <n-button type="primary" :loading="addLoading" @click="addStock">添加自选</n-button>
      </div>
    </section>

    <section class="group-toolbar">
      <div class="group-tabs">
        <n-button size="small" :type="selectedGroupId === 0 ? 'primary' : 'default'" @click="selectGroup(0)">全部</n-button>
        <n-button
          v-for="group in groups"
          :key="group.ID"
          size="small"
          :type="selectedGroupId === Number(group.ID) ? 'primary' : 'default'"
          @click="selectGroup(group.ID)"
        >{{ group.name }}</n-button>
      </div>
      <n-button size="small" @click="managingGroups=true">管理分组</n-button>
      <div class="group-create">
        <n-input v-model:value="newGroupName" size="small" placeholder="新分组名称" @keyup.enter="createGroup" />
        <n-button size="small" secondary type="primary" :loading="groupLoading" @click="createGroup">创建分组</n-button>
      </div>
    </section>

    <n-modal v-model:show="managingGroups" preset="card" title="管理分组" style="width:520px;max-width:95vw">
      <p>删除分组不会移出自选股票。</p>
      <div v-for="group in groups" :key="group.ID" style="display:flex;gap:12px;align-items:center;margin:14px 0">
        <span style="flex:1">{{ group.name }}</span><n-button size="small" @click="renameGroup(group)">重命名</n-button><n-button size="small" type="error" secondary @click="deleteGroup(group)">删除</n-button>
      </div>
      <n-input v-model:value="newGroupName" placeholder="新分组名称" maxlength="30" /><n-button style="margin-top:12px" @click="createGroup">添加分组</n-button>
    </n-modal>
    <n-card :bordered="false" class="watchlist-card">
      <div class="list-heading">
        <div><StockKingIcon name="watchlist" :size="17" /><strong>{{ selectedGroupId ? groups.find(group => Number(group.ID) === selectedGroupId)?.name || '分组自选' : '全部自选' }}</strong><span class="list-count">{{ rows.length }}</span></div>
        <n-button size="small" quaternary :loading="loading" @click="loadWatchlist(true)"><template #icon><StockKingIcon name="refresh" :size="15" /></template>刷新</n-button>
      </div>
      <n-data-table
        :columns="columns"
        :data="rows"
        :loading="loading"
        :row-key="row => rowValue(row, 'StockCode')"
        :pagination="{ pageSize: 15 }"
        :bordered="false"
        :scroll-x="1300"
      >
        <template #empty><div class="watchlist-empty"><span class="empty-watchlist-icon"><StockKingIcon name="watchlist" :size="34" /></span><h3>{{ emptyText }}</h3><p>搜索股票后添加</p></div></template>
      </n-data-table>
      <details class="sk-help-details watchlist-footnote"><summary>价格说明</summary><p>选入价仅记录升级后新加入时取得的行情，固定不变。至今收益率为原币种未复权价格涨跌幅，不含分红、费用和汇率，不代表实际持仓盈利。最新价以显示的报价时间为准。</p></details>
    </n-card>
  </main>
</template>

<style scoped>
.watchlist-page{min-height:100%;padding:30px;color:var(--sk-text);background:var(--sk-page-bg)}.watchlist-toolbar{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;margin-bottom:27px}.add-stock{display:grid;grid-template-columns:minmax(230px,390px) auto;gap:8px;width:min(48%,530px)}.group-toolbar{display:flex;align-items:center;justify-content:space-between;gap:14px;margin:0 0 18px;padding:14px 0;border-top:1px solid var(--sk-border);border-bottom:1px solid var(--sk-border)}.group-tabs{display:flex;flex-wrap:wrap;gap:8px}.group-create{display:grid;grid-template-columns:155px auto;gap:8px}.watchlist-card{background:var(--sk-surface)!important;border:1px solid var(--sk-border)!important;border-radius:12px}.list-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}.list-heading>div{display:flex;align-items:center;gap:10px}.list-heading>div>svg{color:var(--sk-accent)}.list-heading strong{font-size:15px;font-weight:600}.list-count{font-size:10px;line-height:18px;padding:0 6px;border-radius:5px;color:var(--sk-accent);background:var(--sk-accent-soft)}:deep(.stock-name){display:flex;flex-direction:column;gap:5px}:deep(.stock-name strong){font-weight:550}:deep(.stock-name span){color:var(--sk-text-muted);font-family:Consolas,monospace;font-size:10px;letter-spacing:.025em}.actions{display:flex;gap:6px;align-items:center}.watchlist-empty{display:flex;flex-direction:column;align-items:center;padding:65px 20px 75px;text-align:center}.empty-watchlist-icon{display:grid;place-items:center;width:76px;height:76px;margin-bottom:23px;border:1px solid var(--sk-border);border-radius:23px;color:var(--sk-accent);background:var(--sk-accent-soft)}.watchlist-empty h3{margin:0;font-size:17px;color:var(--sk-text);font-weight:500}.watchlist-empty p{margin:12px 0 0;font-size:12px;color:var(--sk-text-muted)}.watchlist-footnote{margin-top:18px;padding-top:14px;border-top:1px solid var(--sk-border);font-size:10px;color:var(--sk-text-muted)}@media(max-width:1150px){.watchlist-toolbar{align-items:stretch;flex-direction:column}.add-stock{grid-template-columns:minmax(0,1fr) auto;width:100%}.watchlist-page{padding:24px 22px}}@media(max-width:800px){.group-toolbar{align-items:stretch;flex-direction:column}.group-create{grid-template-columns:1fr auto}.watchlist-page{padding:22px 16px}}
</style>
