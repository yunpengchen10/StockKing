<script setup>
import { computed, onBeforeMount, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { GetStockKingAIProviders, GetStockKingResearchEvidence, RunStockKingAIResearch, CancelStockKingAIResearch, GetResearchNote, GetStockKingAIAdviceHistory, GetStockKingPreference, GetStockList, SaveStockKingAIAdvice, SaveStockKingPreference } from '../../wailsjs/go/main/App'
import { stockOption } from '../utils/symbol'
import { BUILTIN_TEMPLATES, TEMPLATE_LIBRARY_KEY, LEGACY_TEMPLATE_KEY, freshTemplateLibrary, loadTemplateLibrary, deleteTemplate, restoreMissingTemplates } from '../utils/researchTemplates.mjs'
import { DEFAULT_PROMPT, MAX_IMPORT_BYTES, MAX_PROMPT_BYTES, WORKSPACE_SCHEMA, utf8Bytes, validateFile, validatePrompt, parsePrompt, parseAIText, buildImportedRecord, decodeHistoryRecord, renderSafeMarkdown, normalizeReportMarkdown, normalizeResearchSymbol, evidenceSources, buildResearchPackage, comparisonRows } from '../utils/aiResearch.mjs'

import EconomyBudget from './EconomyBudget.vue'

const router = useRouter(), route = useRoute(), message = useMessage()
const budgetPanel=ref(null)
const SESSION_KEY = 'aiAdvice.workspaceSession.v1'
const code = ref(''), name = ref(''), query = ref(''), stockOptions = ref([]), stockRows = ref([])
const mode = ref('api'), evidence = ref(null), record = ref(null), note = ref({})
const providers = ref([]), providerName = ref(''), configId = ref('')
const library = ref(freshTemplateLibrary()), libraryError = ref(''), templatesLoading = ref(true), templates = computed(() => library.value.items)
const prompt = ref(BUILTIN_TEMPLATES[0].content), templateName = ref(BUILTIN_TEMPLATES[0].name), templateId = ref(BUILTIN_TEMPLATES[0].id)
const templatePurpose = computed(() => templates.value.find(item => item.id === templateId.value)?.purpose || '自定义研究角度')
const importText = ref(''), importFileName = ref(''), sourceName = ref('ChatGPT 手动导入'), analyzedAt = ref(''), expiresAt = ref(''), importError = ref('')
const history = ref([]), historyLoading = ref(false), loading = ref(false), evidenceLoading = ref(false), saving = ref(false)
const currentRequest = ref(''), requestError = ref(''), showRaw = ref(false), showEvidence = ref(false), packageText = ref(''), savedRecordId = ref(null)
const promptFile = ref(null), reportFile = ref(null)
const compareLatest = ref(false), pickContext = ref(null)
function attachPickEvidence(result) { return pickContext.value ? {...result, selectedKingPick:pickContext.value} : result }
function clearPickContext() {
  pickContext.value = null
  if (evidence.value?.selectedKingPick) { const clean = {...evidence.value}; delete clean.selectedKingPick; evidence.value = clean }
  try { sessionStorage.removeItem('stock-king:ai-pick:'+code.value) } catch { /* Route flag is also removed. */ }
  packageText.value = ''; void persistQuietly(); void router.replace({name:'aiAdvice',query:{...route.query,pick:undefined}})
}
let searchTimer, searchVersion = 0, selectionVersion = 0, requestVersion = 0, alive = true, restoring = true
const busy = computed(() => loading.value || evidenceLoading.value || templatesLoading.value)
const selectedProvider = computed(() => providers.value.find(item => String(item.id) === String(configId.value)))
const providerOptions = computed(() => [...new Set(providers.value.map(item => item.name || '未命名平台'))])
const modelOptions = computed(() => providers.value.filter(item => (item.name || '未命名平台') === providerName.value))
const effectiveEvidence = computed(() => compareLatest.value ? evidence.value : record.value?.evidence || evidence.value)
const comparison = computed(() => comparisonRows(compareLatest.value ? null : record.value, effectiveEvidence.value))
const sources = computed(() => evidenceSources(effectiveEvidence.value))
const reportHTML = computed(() => renderSafeMarkdown(record.value?.markdown || record.value?.raw || ''))
const preview = computed(() => {
  if (!importText.value.trim() || !code.value) return null
  try { return buildImportedRecord(importText.value, importOptions()) } catch (error) { return { error: error.message } }
})
const previewHTML = computed(() => preview.value && !preview.value.error ? renderSafeMarkdown(preview.value.markdown) : '')
const evidenceSummary = computed(() => [
  { label: '行情证据', value: effectiveEvidence.value?.evidence?.goTechnical?.available ? '可用' : '不可用' },
  { label: '证据提取时点', value: formatTime(effectiveEvidence.value?.workspaceProvenance?.retrievedAt) },
  { label: '报告分析时点', value: formatTime(record.value?.analyzedAt) }, { label: '报告有效期', value: formatTime(record.value?.expiresAt) },
])
function formatTime(value) { return value ? String(value).replace('T', ' ') : '未知' }
function statusTags(item) {
  if (!item) return []
  const tags = []
  if (!item.analyzedAt) tags.push('时点未知')
  if (!item.expiresAt) tags.push('有效期未知')
  if (item.expiresAt && new Date(item.expiresAt).getTime() < Date.now()) tags.push('已过期')
  if (item.warnings?.some(value => /未来|格式/.test(value))) tags.push('待核对')
  return tags
}
function comparisonLabel(item) { return ({'AI 声称共识':'AI 共识','AI 指出分歧':'AI 分歧','AI 无法判断':'待判断','未提供结构化比较':'待比较'})[item.conclusion] || item.conclusion }
function errorLabel(value) {
  const text = String(value || '')
  if (/股票.*不一致/.test(text)) return '股票不匹配'
  if (/取消|停止接收/.test(text)) return '已取消'
  if (/JSON/.test(text)) return 'JSON 格式错误'
  if (/日期|时点|有效期/.test(text)) return '日期无效'
  if (/超过|KiB/.test(text)) return '内容超限'
  return text.split(/[；。]/)[0].slice(0, 60)
}
function number(value, digits = 2) { return value == null || value === '' || !Number.isFinite(Number(value)) ? '—' : Number(value).toFixed(digits) }
function tierMetrics(model) {
  const tier = (effectiveEvidence.value?.tierModelAnalysis || effectiveEvidence.value?.evidence?.tierModelAnalysis)?.tiers?.[model]
  if (tier?.modelStatus !== 'qualified') return []
  const labels = { returnLowerBound20d: ['20日收益下界', true], returnLowerBound60d: ['60日收益下界', true], rawRankSignal5d: ['5日排名信号', false], rawRankSignal20d: ['20日排名信号', false], touchProbability1d: ['1日触板概率', true], touchProbability3d: ['3日触板概率', true] }
  return Object.entries(tier.result || {}).filter(([key, value]) => labels[key] && value != null && Number.isFinite(Number(value))).map(([key, value]) => ({ label: labels[key][0], value: labels[key][1] ? `${number(Number(value) * 100)}%` : number(value, 5) }))
}
function importOptions() { return { stockCode: code.value, stockName: name.value, fileName: importFileName.value, sourceName: sourceName.value, analyzedAt: analyzedAt.value, expiresAt: expiresAt.value, evidence: evidence.value } }
function requestId() { return globalThis.crypto?.randomUUID?.() || `research_${Date.now()}_${Math.random().toString(36).slice(2)}` }
function notifyError(error) { message.error(error?.message || String(error)) }
async function persistSession() {
  if (!code.value || restoring) return
  await SaveStockKingPreference(SESSION_KEY, JSON.stringify({ code: code.value, name: name.value, record: record.value, evidence: evidence.value, configId: configId.value, providerName: providerName.value, prompt: prompt.value, templateName: templateName.value, templateId: templateId.value, mode: mode.value, pickContext: pickContext.value, savedRecordId: savedRecordId.value }))
}
async function persistQuietly() { try { await persistSession() } catch { /* Saved history is independent of page state. */ } }
async function loadHistory(symbol = code.value) {
  if (!symbol) return
  historyLoading.value = true
  try { const found = await GetStockKingAIAdviceHistory(symbol, 100); if (symbol === code.value && alive) history.value = (found || []).map(item => ({ ...item, decoded: decodeHistoryRecord(item) })) }
  catch (error) { if (symbol === code.value) notifyError(error) } finally { if (symbol === code.value) historyLoading.value = false }
}
function search(value) {
  clearTimeout(searchTimer)
  const version = ++searchVersion
  if (!String(value || '').trim()) { stockOptions.value = []; return }
  searchTimer = setTimeout(async () => {
    try { const found = ((await GetStockList(value)) || []).filter(item => normalizeResearchSymbol(item.ts_code)).slice(0, 40); if (version !== searchVersion || !alive) return; stockRows.value = found; stockOptions.value = found.map(stockOption) } catch (error) { notifyError(error) }
  }, 180)
}
async function choose(value, explicitName = '', sync = true) {
  if (busy.value) return
  const symbol = normalizeResearchSymbol(value)
  if (!symbol) { message.warning('请选择有效 A 股；其他市场仍可在 K 线中研究'); return }
  const version = ++selectionVersion, stock = stockRows.value.find(item => item.ts_code === value)
  pickContext.value = null; code.value = symbol; name.value = typeof explicitName === 'string' && explicitName ? explicitName : stock?.name || stock?.fullname || symbol
  query.value = ''; evidence.value = null; record.value = null; savedRecordId.value = null; note.value = {}; packageText.value = ''; requestError.value = ''; history.value = []; compareLatest.value = false
  importText.value = ''; importFileName.value = ''; importError.value = ''; analyzedAt.value = ''; expiresAt.value = ''
  const foundNote = await GetResearchNote(symbol).catch(() => ({}))
  if (version !== selectionVersion || !alive) return
  note.value = foundNote || {}; await loadHistory(symbol)
  if (version !== selectionVersion || !alive) return
  await persistQuietly()
  if (sync && (route.query.code !== symbol || route.query.name !== name.value)) await router.replace({ name: 'aiAdvice', query: { code: symbol, name: name.value } })
}
function submitTyped() { const typed = String(query.value || '').split('·').pop().trim(), item = stockRows.value.find(row => row.ts_code === typed || row.name === typed || row.fullname === typed); void choose(item?.ts_code || typed, item?.name || '') }
function changePlatform() { configId.value = ''; void persistQuietly() }
function chooseTemplate() { const item = templates.value.find(item => item.id === templateId.value); prompt.value = item?.content || ''; templateName.value = item?.name || '我的研究模板'; void persistQuietly() }
function newTemplate() { templateId.value = ''; chooseTemplate() }
async function storeLibrary(next) {
  if (libraryError.value) throw new Error(libraryError.value)
  await SaveStockKingPreference(TEMPLATE_LIBRARY_KEY, JSON.stringify(next)); library.value = next
}
async function saveTemplate() {
  try {
    validatePrompt(prompt.value); const title = templateName.value.trim(); if (!title) throw new Error('请填写模板名称')
    const id = templateId.value || requestId(), existing = templates.value.find(item => item.id === id)
    if (!existing && templates.value.length >= 40) throw new Error('最多保留 40 个模板')
    const item = { ...existing, id, name: title.slice(0, 80), content: prompt.value, savedAt: new Date().toISOString() }
    const next = existing ? templates.value.map(old => old.id === id ? item : old) : [...templates.value, item]
    await storeLibrary({ ...library.value, items: next }); templateId.value = id; await persistQuietly(); message.success('模板已保存')
  } catch (error) { notifyError(error) }
}
async function removeTemplate() {
  try { await storeLibrary(deleteTemplate(library.value, templateId.value)); templateId.value = templates.value[0]?.id || ''; chooseTemplate(); await persistQuietly() } catch (error) { notifyError(error) }
}
async function restoreTemplates() { try { await storeLibrary(restoreMissingTemplates(library.value)); if (!templateId.value) { templateId.value = templates.value[0]?.id || ''; chooseTemplate() }; message.success('已补回缺少的内置模板，保留已有修改') } catch (error) { notifyError(error) } }
async function readFile(event, kind) {
  const file = event.target.files?.[0]; if (!file) return
  try {
    validateFile(file, kind === 'prompt' ? MAX_PROMPT_BYTES : MAX_IMPORT_BYTES); const text = await file.text()
    if (kind === 'prompt') { const parsed = parsePrompt(text, file.name); prompt.value = parsed.content; templateName.value = parsed.name; templateId.value = '' }
    else { parseAIText(text, file.name); importText.value = text; importFileName.value = file.name; importError.value = '' }
  } catch (error) { notifyError(error) } finally { event.target.value = '' }
}
async function refreshEvidence() {
  if (!code.value || busy.value) return null
  evidenceLoading.value = true; requestError.value = ''; const symbol = code.value
  try { const result = await GetStockKingResearchEvidence(symbol); if (alive && symbol === code.value) { evidence.value = attachPickEvidence(result); packageText.value = ''; await persistQuietly() }; return result }
  catch (error) { requestError.value = error?.message || String(error); return null } finally { evidenceLoading.value = false }
}
async function exportPackage() {
  try { if (!evidence.value && !await refreshEvidence()) return; packageText.value = buildResearchPackage({ stockCode: code.value, stockName: name.value, prompt: prompt.value, evidence: evidence.value }) } catch (error) { notifyError(error) }
}
async function copyText(text) { try { await navigator.clipboard.writeText(text); message.success('已复制') } catch { message.warning('剪贴板不可用，请在原文框中选择并复制') } }
function download(text, fileName, type = 'text/plain;charset=utf-8') { const url = URL.createObjectURL(new Blob([text], { type })), link = document.createElement('a'); link.href = url; link.download = fileName; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000) }
async function saveRecord(next = record.value) {
  if (!next || saving.value) return
  saving.value = true
  try {
    const existing = history.value.find(item => item.decoded?.source === next.source && item.decoded?.raw === next.raw && item.decoded?.analyzedAt === next.analyzedAt && item.decoded?.expiresAt === next.expiresAt)
    if (existing) { savedRecordId.value = existing.ID || existing.id; await persistQuietly(); message.info('相同原文与分析日期已在历史中'); return }
    const saved = await SaveStockKingAIAdvice(next.stockCode, next.stockName || name.value, JSON.stringify(next), `${next.source === 'import' ? '外部导入' : 'API'} · ${next.sourceName}`)
    if (next === record.value) savedRecordId.value = saved.ID || saved.id || true
    await persistQuietly(); await loadHistory(next.stockCode); message.success('报告、来源与原文已保存')
  } catch (error) { notifyError(new Error(`报告仍在页面，保存失败：${error?.message || error}`)) } finally { saving.value = false }
}
async function importReport() {
  if (saving.value) return
  try { const next = buildImportedRecord(importText.value, importOptions()); record.value = next; savedRecordId.value = null; showRaw.value = false; compareLatest.value = false; importError.value = ''; await saveRecord(next) } catch (error) { importError.value = error.message }
}
function openHistory(item) { if (busy.value) return; record.value = item.decoded; savedRecordId.value = item.ID || item.id || true; showRaw.value = false; compareLatest.value = false; void persistQuietly() }
async function analyze() {
  if (busy.value || saving.value) return
  try { validatePrompt(prompt.value); if (!code.value) throw new Error('请先选择研究股票'); if (!selectedProvider.value?.ready) throw new Error('请明确选择可用的平台和模型') } catch (error) { notifyError(error); return }
  const symbol = code.value, stockName = name.value, selected = { ...selectedProvider.value }, template = { name: templateName.value, content: prompt.value }, version = ++requestVersion, id = requestId()
  currentRequest.value = id; loading.value = true; requestError.value = ''
  try {
    const result = await RunStockKingAIResearch({ requestId: id, symbolCode: symbol, configId: Number(configId.value), prompt: template.content, pickContext: pickContext.value || undefined })
    if (!alive || version !== requestVersion || code.value !== symbol) return
    let report, warnings = []
    try { report = parseAIText(result.llmExplanation) } catch { report = { raw: String(result.llmExplanation || ''), markdown: String(result.llmExplanation || ''), parsed: {}, format: 'text' }; warnings.push('模型返回格式不完整，已保留原文，不能据此自动生成共识') }
    const snapshot = { ...result }; delete snapshot.llmExplanation; evidence.value = snapshot
    record.value = { schema: WORKSPACE_SCHEMA, source: 'api', sourceName: `${selected.name || '已有平台'} / ${selected.model}`, stockCode: symbol, stockName, analyzedAt: result.workspaceAnalyzedAt || null, expiresAt: null, savedAt: new Date().toISOString(), dateOrigin: 'API 返回完成时间', ...report, warnings: [...warnings, '未声明有效期；新行情或证伪条件出现时需要重新研究'], evidence: snapshot, template, requestId: id }
    savedRecordId.value = null; showRaw.value = false; compareLatest.value = false; await saveRecord(record.value)
  } catch (error) { if (alive && version === requestVersion) requestError.value = error?.message || String(error) } finally { if (version === requestVersion) { loading.value = false; currentRequest.value = ''; budgetPanel.value?.refresh() } }
}
async function cancel() {
  if (!currentRequest.value) return
  try { await CancelStockKingAIResearch(currentRequest.value); ++requestVersion; currentRequest.value = ''; loading.value = false; requestError.value = '已停止接收本次结果；平台对已发送请求的计费规则仍适用。' } catch (error) { notifyError(error) }
}
onBeforeMount(async () => {
  const [available, savedTemplates, legacyTemplates, sessionRaw] = await Promise.all([GetStockKingAIProviders().catch(() => []), GetStockKingPreference(TEMPLATE_LIBRARY_KEY).catch(error => { libraryError.value = '模板读取失败，请重新打开本页；原记录未覆盖'; return '' }), GetStockKingPreference(LEGACY_TEMPLATE_KEY).catch(error => { libraryError.value = '旧模板读取失败，请重新打开本页；原记录未覆盖'; return '' }), GetStockKingPreference(SESSION_KEY).catch(() => '')])
  if (!alive) return
  providers.value = available || []
  try { library.value = loadTemplateLibrary(savedTemplates, legacyTemplates) } catch (error) { libraryError.value = error.message }
  let session; try { session = JSON.parse(sessionRaw) } catch { session = null }
  if (session) { providerName.value = session.providerName || ''; configId.value = session.configId || ''; mode.value = ['api', 'import', 'export'].includes(session.mode) ? session.mode : 'api'; const id = session.templateId === 'builtin' ? 'builtin-overview' : session.templateId; templateId.value = templates.value.some(item => item.id === id) ? id : ''; if (typeof session.prompt === 'string' && utf8Bytes(session.prompt) <= MAX_PROMPT_BYTES) { prompt.value = session.prompt; templateName.value = session.templateName || '我的研究模板' } }
  if (templates.value.some(item => item.id === route.query.template)) { templateId.value = route.query.template; chooseTemplate() }
  templatesLoading.value = false
  const initialCode = route.query.code || session?.code
  if (initialCode) { await choose(initialCode, route.query.name || session?.name || '', false); if (session?.code === code.value) { record.value = session.record || null; evidence.value = session.evidence || null; savedRecordId.value = session.savedRecordId || null } }
  if (session?.code === code.value && session.pickContext?.candidate && normalizeResearchSymbol(session.pickContext.candidate.code) === code.value) pickContext.value = session.pickContext
  if (route.query.pick === '1') {
    try { const selected = JSON.parse(sessionStorage.getItem('stock-king:ai-pick:'+code.value) || 'null'); if (!selected?.candidate || normalizeResearchSymbol(selected.candidate.code) !== code.value) throw new Error('精选快照缺失，请从精选重新打开'); pickContext.value = selected } catch (error) { requestError.value = error.message; pickContext.value = null }
  }
  restoring = false; await persistQuietly()
})
onBeforeUnmount(() => { alive = false; clearTimeout(searchTimer); ++requestVersion; if (currentRequest.value) void CancelStockKingAIResearch(currentRequest.value).catch(() => {}) })
watch(() => route.query.code, value => { if (value && value !== code.value) void choose(value, route.query.name || '', false) })
watch([configId, mode], () => { void persistQuietly() })
</script>

<template>
  <main class="ai-workspace">
    <EconomyBudget ref="budgetPanel" />
    <p v-if="evidence?.economyReview">{{evidence.economyReview.cacheHit ? '复用结果' : '新请求'}} · token {{evidence.economyReview.usage?.total_tokens ?? '未知'}} · 费用估算 {{evidence.economyReview.estimatedCost ?? '未配置单价或用量未知'}}</p>
    <header class="workspace-header"><h1>AI 研究</h1><div class="stock-picker"><n-auto-complete v-model:value="query" :options="stockOptions" :disabled="busy" placeholder="选择 A 股：名称或代码" :on-select="choose" @update:value="search" @keyup.enter="submitTyped" /></div></header>
    <nav class="source-tabs" aria-label="研究来源"><button v-for="item in [{key:'api',label:'API 研究'},{key:'import',label:'导入报告'},{key:'export',label:'研究包'}]" :key="item.key" :class="{active:mode===item.key}" @click="mode=item.key"><strong>{{ item.label }}</strong></button></nav>
    <div v-if="code" class="instrument-bar"><strong>{{ name }}</strong><code>{{ code }}</code><button class="text-button" @click="router.push({name:'klineAnalysis',query:{code,name}})">K 线 ↗</button><span v-if="note.coreLogic" :title="note.coreLogic">{{ note.coreLogic }}</span></div><div v-else class="notice">请选择 A 股</div>
    <details v-if="pickContext" class="compact-help"><summary>已带入精选快照 · {{ formatTime(pickContext.generatedAt) }}</summary><p>本次研究将同时提供该快照与最新证据，分别核对时点。分数不是概率。</p><button class="text-button" :disabled="busy" @click="clearPickContext">移除快照</button></details>
    <div v-if="requestError" class="notice warning" role="alert" :title="requestError">{{ errorLabel(requestError) }}</div>
    <section v-if="mode==='api' || mode==='export'" class="input-panel">
      <div class="panel-heading"><h2>{{ mode==='api' ? '模型与提示词' : '研究包' }}</h2><button class="secondary" :disabled="!code || busy" @click="refreshEvidence">{{ evidenceLoading ? '提取中…' : '更新证据' }}</button></div>
      <div v-if="mode==='api'" class="config-grid"><label>平台<select v-model="providerName" :disabled="busy" @change="changePlatform"><option value="">{{ providers.length ? '选择平台' : '暂无配置' }}</option><option v-for="item in providerOptions" :key="item">{{ item }}</option></select></label><label>模型<select v-model="configId" :disabled="busy || !providerName"><option value="">选择模型</option><option v-for="item in modelOptions" :key="item.id" :value="String(item.id)" :disabled="!item.ready">{{ item.model || '未填写模型' }}{{ item.ready ? '' : '（配置不完整）' }}</option></select></label></div>
      <div class="template-toolbar"><label>模板<select v-model="templateId" :disabled="busy" @change="chooseTemplate"><option v-if="!templateId" value="">新模板</option><option v-for="item in templates" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><input v-model="templateName" aria-label="模板名称" maxlength="80" placeholder="模板名称" :disabled="busy"><button class="secondary" :disabled="busy || !!libraryError" @click="saveTemplate">保存模板</button><button class="text-button" :disabled="busy" @click="newTemplate">新建</button><button class="text-button" :disabled="!templateId || busy || !!libraryError" @click="removeTemplate">删除</button><button class="text-button" :disabled="busy || !!libraryError" title="补回已删除的内置模板，不覆盖已有修改" @click="restoreTemplates">补回内置</button><button class="secondary" :disabled="busy" @click="promptFile?.click()">导入提示词</button><input ref="promptFile" type="file" accept=".txt,.md,.json" hidden @change="readFile($event,'prompt')"></div>
      <p class="subtle">{{ templatePurpose }}</p><div v-if="libraryError" class="notice warning" role="alert">{{ libraryError }}</div>
      <textarea v-model="prompt" :disabled="busy" class="prompt-editor" aria-label="研究提示词" spellcheck="false" />
      <div class="action-row"><small :title="'支持 TXT / MD / JSON，模板本地保存'">{{ (utf8Bytes(prompt)/1024).toFixed(1) }} / 32 KiB</small><template v-if="mode==='api'"><button v-if="loading" class="secondary" @click="cancel">取消研究</button><button class="primary" :disabled="!code || busy || saving || !selectedProvider?.ready" @click="analyze">{{ loading ? '研究进行中…' : '运行研究' }}</button></template><button v-else class="primary" :disabled="!code || busy" @click="exportPackage">生成研究包</button></div>
      <details class="compact-help"><summary>使用说明</summary><p v-if="mode==='api'">平台配置来自“AI 模型服务”。点击运行后发送证据与模板；AI 只能解释证据，不能更改量化验证、排名或风控。</p><p v-else>复制研究包到自己的 ChatGPT 会话，再将结果导入。软件不读取 ChatGPT 会话，ChatGPT 订阅也不代替 API 配置。</p><p>提示词支持 TXT / MD / JSON，限 32 KiB；JSON 使用 prompt、content 或 template 字符串字段。</p></details>
      <div v-if="mode==='export' && packageText" class="package-result"><div class="action-row"><strong>已生成</strong><button class="secondary" @click="copyText(packageText)">复制</button><button class="secondary" @click="download(packageText,`${code}-研究包.md`)">下载</button></div><textarea :value="packageText" readonly aria-label="研究包原文" @focus="$event.target.select()" /></div>
    </section>
    <section v-if="mode==='import'" class="input-panel">
      <div class="panel-heading"><h2>导入报告</h2><button class="secondary" :disabled="busy" @click="reportFile?.click()">选择文件</button><input ref="reportFile" type="file" accept=".txt,.md,.json" hidden @change="readFile($event,'report')"></div>
      <div class="config-grid import-meta"><label>来源<input v-model="sourceName" maxlength="120" placeholder="ChatGPT / 其他"></label><label>分析时点<input v-model="analyzedAt" placeholder="YYYY-MM-DD，可留空"></label><label>有效期<input v-model="expiresAt" placeholder="YYYY-MM-DD，可留空"></label></div>
      <textarea v-model="importText" class="import-editor" aria-label="外部报告原文" spellcheck="false" placeholder="粘贴 Markdown、文本或 JSON…" @input="importFileName='';importError=''" />
      <div class="action-row"><small>{{ importFileName || '粘贴内容' }} · {{ (utf8Bytes(importText)/1024).toFixed(1) }} / 256 KiB</small><button class="primary" :disabled="!code || !preview || !!preview.error || saving || busy" @click="importReport">{{ saving ? '保存中…' : '保存报告' }}</button></div>
      <details class="compact-help"><summary>格式说明</summary><p>支持 TXT / MD / JSON，限 256 KiB。JSON 可含 stockCode、analyzedAt、expiresAt、markdown、quantComparison。日期支持 YYYY-MM-DD 或带时区 ISO 时间；留空显示未知。</p><p>股票代码须与当前对象一致。原文完整保留，原始 HTML 不执行。</p></details>
      <div v-if="importError || preview?.error" class="notice warning" role="alert" :title="importError || preview.error">{{ errorLabel(importError || preview.error) }}</div><template v-if="preview && !preview.error"><div class="status-tags" :title="preview.warnings.join('；')"><span v-for="tag in statusTags(preview)" :key="tag">{{ tag }}</span></div><details class="preview-block"><summary>预览 · {{ preview.stockCode }}</summary><article class="safe-markdown" v-html="previewHTML" /></details></template>
    </section>
    <div class="research-layout">
      <section class="report-area"><div class="evidence-strip"><div v-for="item in evidenceSummary" :key="item.label"><span>{{ item.label }}</span><strong>{{ item.value }}</strong></div></div>
        <section class="report-panel"><div class="panel-heading"><div><h2>{{ record?.sourceName || '报告' }}</h2><p v-if="record">{{ record.source==='import' ? '导入' : record.source==='api' ? 'API' : '历史' }} · {{ savedRecordId ? '已保存' : '未保存' }}</p></div><div v-if="record" class="report-actions"><button class="text-button" @click="showRaw=!showRaw">{{ showRaw ? '阅读报告' : '查看原文' }}</button><button class="text-button" @click="copyText(record.raw)">复制</button><button class="text-button" @click="download(normalizeReportMarkdown(record.markdown || record.raw),`${record.stockCode}-报告.md`)">导出</button><button v-if="!savedRecordId" class="secondary" :disabled="saving" @click="saveRecord(record)">保存</button></div></div>
          <div v-if="record" class="status-tags" :title="record.warnings?.join('；')"><span v-for="tag in statusTags(record).filter(tag=>tag==='已过期'||tag==='待核对')" :key="tag">{{ tag }}</span></div>
          <details v-if="record" class="compact-help"><summary>来源详情</summary><p>{{ record.stockCode }} · 日期来源：{{ record.dateOrigin || '未知' }}。导入报告与量化证据可能来自不同时点。</p><p v-if="record.warnings?.length">{{ record.warnings.join('；') }}</p></details>
          <template v-if="record"><textarea v-if="showRaw" :value="record.raw" readonly class="raw-report" aria-label="保存的原始报告" /><article v-else class="safe-markdown" v-html="reportHTML" /></template><div v-else class="empty-report"><h3>暂无报告</h3><p>运行研究或导入报告</p></div>
        </section>
      </section>
      <aside class="evidence-sidebar"><div class="side-heading"><h2>量化对照</h2><span title="AI 共识仅为解释，不改变量化验证、排名或风控。">只读 ⓘ</span></div>
        <div v-if="record?.evidence && evidence" class="evidence-choice"><select v-model="compareLatest" aria-label="对照证据"><option :value="false">报告快照</option><option :value="true">最新证据</option></select><p v-if="compareLatest" class="subtle">证据已更新，需重新比较</p></div>
        <article v-for="item in comparison" :key="item.model" class="quant-card"><div class="quant-heading"><strong>{{ item.label }}</strong><span :class="['status-dot', {qualified:item.qualified}]" :title="item.validation">{{ item.qualified ? '已发布' : '未通过 / 不可用' }}</span></div><div v-if="tierMetrics(item.model).length" class="quant-metrics"><div v-for="metric in tierMetrics(item.model)" :key="metric.label"><span>{{ metric.label }}</span><strong>{{ metric.value }}</strong></div></div><details class="comparison-claim"><summary>{{ comparisonLabel(item) }}</summary><p>{{ item.reason }}</p><p>{{ item.validation }}</p><p v-if="item.degradedReasons.length">{{ item.degradedReasons.join('；') }}</p></details></article>
        <button class="evidence-toggle" @click="showEvidence=!showEvidence">{{ showEvidence ? '收起来源' : '证据与来源' }}</button><div v-if="showEvidence" class="evidence-details"><p v-if="!sources.length" class="subtle">原始来源时间未知</p><div v-for="item in sources" :key="item.path" class="source-row"><code>{{ item.path }}</code><span>{{ item.value }}</span></div><details><summary>完整证据 JSON</summary><pre>{{ JSON.stringify(effectiveEvidence || {}, null, 2) }}</pre></details></div>
      </aside>
    </div>
    <section class="history-panel"><div class="panel-heading"><h2 title="当前股票最近 100 份报告">历史</h2><button class="text-button" :disabled="!code || historyLoading" @click="loadHistory(code)">{{ historyLoading ? '读取中…' : '刷新' }}</button></div><div v-if="history.length" class="history-list"><button v-for="item in history" :key="item.ID || item.id" :disabled="busy" @click="openHistory(item)"><span class="history-source">{{ item.decoded.source==='import'?'导入':item.decoded.source==='api'?'API':'历史' }}</span><div><strong>{{ item.decoded.sourceName }}</strong><p>{{ String(item.decoded.markdown || item.decoded.raw).replace(/[#*`]/g,'').slice(0,100) }}</p></div><time :title="`保存 ${formatTime(item.CreatedAt || item.createdAt)}`">{{ formatTime(item.decoded.analyzedAt) }}</time></button></div><p v-else class="subtle">暂无记录</p></section>
  </main>
</template>

<style scoped>
.ai-workspace{padding:26px 28px 70px;min-height:100%;box-sizing:border-box;background:var(--sk-page-bg,#0c1017);color:var(--sk-text,#dbe3ef);font-family:var(--sk-font-sans,Inter,"Microsoft YaHei",sans-serif)}*{box-sizing:border-box}.workspace-header{display:flex;justify-content:space-between;gap:24px;align-items:center;margin-bottom:22px}.eyebrow{font-size:10px;letter-spacing:2px;color:var(--sk-accent,#3da4ff);font-weight:700}h1{margin:7px 0;font-size:26px;letter-spacing:-.6px}h2{margin:0;font-size:15px;font-weight:650}h3{font-size:18px}.workspace-header p,.panel-heading p{margin:5px 0 0;color:var(--sk-text-muted,#7f8ca3);font-size:12px;line-height:1.6}.stock-picker{width:330px}.stock-picker>label{display:block;font-size:11px;color:var(--sk-text-muted,#7f8ca3);margin-bottom:7px}.source-tabs{display:flex;gap:5px;border-bottom:1px solid var(--sk-border,#263140);padding-bottom:10px}.source-tabs button{padding:10px 18px;background:transparent;border:1px solid transparent;color:var(--sk-text-muted,#7f8ca3);border-radius:6px;cursor:pointer;text-align:left}.source-tabs button.active{background:var(--sk-surface-2,#172130);border-color:var(--sk-border,#263140);color:var(--sk-text,#dbe3ef)}.source-tabs strong{display:block;font-size:13px}.source-tabs span{display:block;font-size:10px;margin-top:4px}.source-tabs .source-boundary{margin:auto 0 auto auto;color:var(--sk-text-muted,#7f8ca3);font-size:11px}.instrument-bar{display:flex;align-items:center;gap:12px;padding:14px 0}.instrument-bar strong{font-size:18px}.instrument-bar code{color:var(--sk-accent,#3da4ff);font-size:12px}.instrument-bar>span{margin-left:auto;font-size:11px;max-width:45%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--sk-text-muted,#7f8ca3)}
.input-panel,.report-panel,.history-panel{border:1px solid var(--sk-border,#263140);border-radius:8px;background:var(--sk-surface,#111824);padding:18px;margin-bottom:18px}.panel-heading{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-bottom:16px}.config-grid{display:grid;grid-template-columns:1fr 1fr 1.3fr;gap:14px;margin-bottom:16px}label{font-size:11px;color:var(--sk-text-muted,#7f8ca3)}select,input:not([type=file]),textarea{font:inherit;display:block;width:100%;background:var(--sk-surface-2,#151e2c);color:var(--sk-text,#dbe3ef);border:1px solid var(--sk-border,#263140);border-radius:5px;padding:9px 10px;outline:none}label select,label input{margin-top:6px}input:focus,textarea:focus,select:focus{border-color:var(--sk-accent,#3da4ff)}textarea{resize:vertical;font-family:var(--sk-font-mono,"Cascadia Code",Consolas,monospace);font-size:12px;line-height:1.7}.config-hint{font-size:11px;line-height:1.8;padding-top:24px;color:var(--sk-text-muted,#7f8ca3)}.template-toolbar{display:flex;align-items:end;gap:9px;margin-bottom:10px}.template-toolbar label{min-width:160px}.template-toolbar>input{max-width:200px}.prompt-editor{min-height:116px}.import-editor{min-height:180px}.action-row{display:flex;align-items:center;gap:9px;margin-top:11px}.action-row small{margin-right:auto;color:var(--sk-text-muted,#7f8ca3);font-size:10px}.primary,.secondary,.text-button{font:inherit;font-size:11px;white-space:nowrap;cursor:pointer;border-radius:5px}.primary{padding:9px 18px;border:1px solid var(--sk-accent,#3da4ff);background:var(--sk-accent,#3da4ff);color:var(--sk-accent-on,#071421);font-weight:700}.secondary{padding:8px 12px;border:1px solid var(--sk-border,#263140);background:var(--sk-surface-2,#151e2c);color:var(--sk-text,#dbe3ef)}.text-button{padding:6px 3px;border:0;background:transparent;color:var(--sk-accent,#3da4ff)}button:disabled,select:disabled{opacity:.45;cursor:not-allowed}button:hover:not(:disabled){filter:brightness(1.12)}.notice{padding:11px 13px;font-size:11px;line-height:1.7;color:var(--sk-text-muted,#7f8ca3);background:var(--sk-surface-2,#151e2c);border-left:2px solid var(--sk-accent,#3da4ff);margin:12px 0}.notice.warning{border-left-color:var(--sk-warning,#d9ac62);color:var(--sk-warning,#d9ac62)}.subtle{font-size:11px;line-height:1.7;color:var(--sk-text-muted,#7f8ca3)}.package-result textarea{height:220px;margin-top:12px}.package-result strong{font-size:12px;margin-right:auto}.preview-block{border-top:1px solid var(--sk-border,#263140);padding-top:12px;max-height:390px;overflow:auto}summary{cursor:pointer;font-size:11px;color:var(--sk-text-muted,#7f8ca3)}
.research-layout{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:18px;align-items:start}.evidence-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;border:1px solid var(--sk-border,#263140);border-radius:7px;overflow:hidden;margin-bottom:14px;background:var(--sk-border,#263140)}.evidence-strip>div{padding:13px;background:var(--sk-surface,#111824)}.evidence-strip span{display:block;font-size:10px;color:var(--sk-text-muted,#7f8ca3);margin-bottom:7px}.evidence-strip strong{font:500 11px var(--sk-font-mono,Consolas,monospace);word-break:break-word}.report-actions{display:flex;gap:10px}.report-panel{min-height:340px}.raw-report{min-height:440px}.empty-report{padding:65px 15px;text-align:center;color:var(--sk-text-muted,#7f8ca3)}.empty-mark{display:inline-flex;border:1px solid var(--sk-border,#263140);width:46px;height:46px;border-radius:10px;align-items:center;justify-content:center;color:var(--sk-accent,#3da4ff);font-weight:700}.empty-report h3{color:var(--sk-text,#dbe3ef);font-weight:500}.empty-report p{font-size:12px}.empty-report small{font-size:10px}.side-heading{display:flex;align-items:center;justify-content:space-between;margin-top:5px}.side-heading>span{font-size:10px;color:var(--sk-text-muted,#7f8ca3)}.quant-card{padding:14px;border:1px solid var(--sk-border,#263140);border-radius:7px;background:var(--sk-surface,#111824);margin-top:10px}.quant-heading{display:flex;gap:5px;align-items:center;justify-content:space-between}.quant-heading strong{font-size:12px}.status-dot{font-size:9px;color:var(--sk-warning,#d9ac62)}.status-dot.qualified{color:var(--sk-positive,#4ac79c)}.validation-state{font-size:10px;line-height:1.6;color:var(--sk-text-muted,#7f8ca3)}.quant-metrics{display:grid;grid-template-columns:repeat(2,1fr);gap:6px}.quant-metrics>div{padding:8px;background:var(--sk-surface-2,#151e2c);border-radius:4px}.quant-metrics span{display:block;font-size:9px;color:var(--sk-text-muted,#7f8ca3)}.quant-metrics strong{font-size:14px;display:block;margin-top:5px;font-family:var(--sk-font-mono,Consolas,monospace)}.comparison-claim{border-top:1px solid var(--sk-border,#263140);padding-top:10px;margin-top:10px}.comparison-claim b{font-size:11px;font-weight:500;color:var(--sk-accent,#3da4ff)}.comparison-claim p,.quant-card>small{font-size:11px;line-height:1.6;color:var(--sk-text-muted,#7f8ca3)}.evidence-toggle{display:block;width:100%;margin:12px 0;border:1px solid var(--sk-border,#263140);padding:9px;background:var(--sk-surface-2,#151e2c);color:var(--sk-text-muted,#7f8ca3);border-radius:5px;font-size:11px;cursor:pointer}.evidence-details{max-height:450px;overflow:auto}.source-row{padding:7px 0;border-bottom:1px solid var(--sk-border,#263140);overflow-wrap:anywhere}.source-row code{font-size:9px;display:block;color:var(--sk-text-muted,#7f8ca3)}.source-row span{font-size:10px}.evidence-details pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:10px}
.history-panel{margin-top:18px}.history-list{display:grid;gap:1px;background:var(--sk-border,#263140)}.history-list>button{display:flex;align-items:center;gap:14px;text-align:left;padding:13px 8px;background:var(--sk-surface,#111824);color:var(--sk-text,#dbe3ef);border:0;cursor:pointer}.history-source{padding:4px 6px;border:1px solid var(--sk-border,#263140);border-radius:4px;font-size:10px;min-width:42px;text-align:center;color:var(--sk-accent,#3da4ff)}.history-list strong{font-size:11px;font-weight:500}.history-list p{font-size:10px;margin:5px 0 0;color:var(--sk-text-muted,#7f8ca3);max-width:650px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.history-list time{font-size:10px;text-align:right;margin-left:auto;white-space:nowrap;color:var(--sk-text-muted,#7f8ca3)}.history-list time small{display:block;margin-top:4px;font-size:9px}
.evidence-choice{grid-column:1/-1;margin-bottom:10px}
.ai-workspace{padding-top:18px}.workspace-header{margin-bottom:12px}.workspace-header h1{font-size:23px;margin:0}.source-tabs{padding-bottom:7px}.source-tabs button{padding:8px 14px}.input-panel,.report-panel,.history-panel{padding:14px;margin-bottom:14px}.panel-heading{margin-bottom:12px}.config-grid{grid-template-columns:1fr 1fr;margin-bottom:12px}.import-meta{grid-template-columns:repeat(3,1fr)}.prompt-editor{min-height:76px}.import-editor{min-height:130px}.report-panel{min-height:210px}.empty-report{padding:25px 12px}.empty-report h3{font-size:15px;margin:12px 0}.evidence-strip>div{padding:10px}.quant-card{padding:12px}.quant-metrics{margin-top:10px}.comparison-claim{margin-top:8px;padding-top:8px}.comparison-claim summary{color:var(--sk-accent)}.compact-help{margin:9px 0;font-size:11px;color:var(--sk-text-muted);line-height:1.7}.compact-help p{margin:7px 0}.compact-help summary{font-size:10px}.status-tags{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.status-tags:empty{display:none}.status-tags span{padding:3px 7px;border:1px solid var(--sk-border);border-radius:4px;color:var(--sk-warning,#d9ac62);font-size:10px}.evidence-choice{margin-top:12px}.history-panel{margin-top:12px}
.safe-markdown{font-size:13px;line-height:1.9;overflow-wrap:anywhere;color:var(--sk-text,#dbe3ef)}.safe-markdown :deep(h1){font-size:22px;margin:22px 0 13px}.safe-markdown :deep(h2){font-size:17px;margin:24px 0 10px;padding-bottom:8px;border-bottom:1px solid var(--sk-border,#263140)}.safe-markdown :deep(h3){font-size:14px;margin:18px 0 8px}.safe-markdown :deep(p){margin:10px 0}.safe-markdown :deep(a){color:var(--sk-accent,#3da4ff)}.safe-markdown :deep(pre){overflow:auto;padding:14px;background:var(--sk-surface-2,#151e2c);border-radius:5px;font-size:11px;white-space:pre-wrap}.safe-markdown :deep(blockquote){margin:14px 0;padding:5px 13px;border-left:2px solid var(--sk-border,#263140);color:var(--sk-text-muted,#7f8ca3)}.safe-markdown :deep(table){border-collapse:collapse;width:100%;font-size:11px;display:block;overflow:auto}.safe-markdown :deep(th),.safe-markdown :deep(td){border:1px solid var(--sk-border,#263140);padding:7px 10px;text-align:left}.safe-markdown :deep(th){background:var(--sk-surface-2,#151e2c)}
@media(min-width:1600px){.research-layout{grid-template-columns:minmax(0,1fr) 380px}}@media(max-width:1150px){.research-layout{grid-template-columns:minmax(0,1fr)}.evidence-sidebar{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.side-heading,.evidence-sidebar>.subtle,.evidence-toggle,.evidence-details{grid-column:1/-1}.quant-card{margin:0}.template-toolbar{flex-wrap:wrap}.config-grid{grid-template-columns:1fr 1fr}.config-hint{grid-column:1/-1;padding:0}.source-boundary{display:none!important}}@media(max-width:740px){.ai-workspace{padding:16px 12px 50px}.workspace-header{align-items:stretch;flex-direction:column}.stock-picker{width:100%}.source-tabs button{padding:9px 10px}.source-tabs span{font-size:9px}.config-grid,.evidence-strip{grid-template-columns:1fr 1fr}.evidence-sidebar{display:block}.quant-card{margin:10px 0}.instrument-bar>span{display:none}.panel-heading{align-items:start;flex-direction:column}.history-list time{display:none}.history-list p{max-width:230px}.import-meta{grid-template-columns:1fr}.action-row{flex-wrap:wrap}.action-row small{width:100%}.template-toolbar label{flex:1}.template-toolbar>input{max-width:none}.report-actions{flex-wrap:wrap}}
</style>
