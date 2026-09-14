import { DEFAULT_PROMPT, utf8Bytes, MAX_PROMPT_BYTES } from './aiResearch.mjs'

const boundary = '\n只使用 suppliedEvidence 中可核验的数据，引用证据键和原始时点。缺失项列为待补，不编造价格、概率、财务数据或新闻。AI意见不能覆盖量化未验证/降级状态。使用当前选定股票；不保证收益。先给三行结论，再给简表，避免重复免责声明。'
const template = (id, name, purpose, content) => ({ id: `builtin-${id}`, builtin: true, name, purpose, content: content + boundary })
export const BUILTIN_TEMPLATES = [
  template('overview', '快速看懂', '第一次研究一只股票', DEFAULT_PROMPT + '\n输出：1. 当前值得继续研究、等待补证或暂时回避；2. 最重要的两项依据与一个反例；3. 下一步需要观察什么。'),
  template('short', '短线机会', '1–3 个交易日的条件观察', '围绕1–3个交易日评估该股：催化是否有原始公告支持、量价与板块证据是否一致、是否可能追高或无法成交。用表格列出“观察条件｜确认所需证据｜失效条件｜当前是否满足”。考虑A股T+1、涨跌停和滑点。无盘口或分钟数据时，不给盘中买点；区分已发生、推断和待确认。'),
  template('swing', '波段计划', '5–20 个交易日的趋势复核', '以5–20个交易日为观察周期，检查趋势、量能、波动和关键位。分别列出继续观察、条件满足、逻辑失效三个场景。关键价格只有证据能计算时才引用，并写明来源、周期、复权与计算方式。给出复核事项和时间条件，不把相关指标当成独立多票胜率。'),
  template('long', '长期公司研究', '经营质量与长期持有依据', '以6–24个月的公司研究视角，分开分析商业模式、竞争、现金流、资产负债、估值前提、治理和催化。输入若只有技术行情，应明确“长期证据不足”，列出缺少的财报期间、现金流和估值数据。输出投资假设、最强反证、季度复核清单；不把短线套牢变成长线理由。'),
  template('holding', '持仓复核', '已有计划是否仍然成立', '复核用户提供的持仓或研究笔记：原始入场依据是否仍成立、触及哪些失效条件、是否到了复核日。输出“继续跟踪｜需要复核退出｜信息不足”的条件分析及待办。没有成本、股数、仓位或风险预算时列为缺失，不猜测账户，也不直接给减仓股数；区分计划参考价与实际可成交价。'),
  template('news', '公告与消息核验', '分清事实、传闻与影响', '按“事实｜原始来源与时点｜推断｜待核实”整理公告和新闻。区分上市公司原始披露、媒体转述、题材关联与传闻；没有原始公告时不声称已核实。说明潜在影响的传导条件、市场是否可能已反映、什么新证据会推翻判断。'),
  template('signals', '指标组合解读', '把技术信号读成条件', '只对证据中实际提供的技术指标做解读。先核对周期、复权和最新K线是否收盘，再区分趋势、动量、量能和波动。给出不超过3个指标的互补观察组合及先后阅读顺序，指出同源相关与冲突。未提供的指标不能补造当前读数；分形、ZigZag等需要后续确认的标记不能当成当时已知信号。'),
  template('challenge', '反方审查', '检查推荐是否站得住', '对本次精选或量化结论做反方审查：逐项检查证据时效、缺省值、规则分与概率混淆、样本规模、费用、基准与样本外验证。列出支持证据、最强反对证据、关键分歧、下一项最有价值的验证。未通过量化验收时保留失败状态，不给平均分替它背书。'),
]
export const TEMPLATE_LIBRARY_KEY = 'aiAdvice.promptTemplates.v2'
export const LEGACY_TEMPLATE_KEY = 'aiAdvice.promptTemplates.v1'
export const freshTemplateLibrary = () => ({ version: 2, items: BUILTIN_TEMPLATES.map(t => ({ ...t })), removed: [] })
const validItem = item => item && typeof item.id === 'string' && typeof item.name === 'string' && typeof item.content === 'string' && item.content.trim() && utf8Bytes(item.content) <= MAX_PROMPT_BYTES
function parseLibrary(raw) { try { return JSON.parse(raw) } catch { throw new Error('模板库格式损坏，原记录未覆盖；请从备份恢复') } }
export function loadTemplateLibrary(raw, legacyRaw = '') {
  if (raw) {
    const parsed = parseLibrary(raw)
    if (parsed?.version !== 2 || !Array.isArray(parsed.items) || !parsed.items.every(validItem)) throw new Error('模板库读取失败，原记录未覆盖')
    const removed = Array.isArray(parsed.removed) ? parsed.removed.filter(id => typeof id === 'string') : []
    const items = [...new Map(parsed.items.map(item => [item.id, item])).values()]
    for (const built of BUILTIN_TEMPLATES) if (!removed.includes(built.id) && !items.some(item => item.id === built.id)) items.push({ ...built })
    return { version: 2, items, removed }
  }
  const library = freshTemplateLibrary()
  if (legacyRaw) {
    const legacy = parseLibrary(legacyRaw)
    if (!Array.isArray(legacy) || !legacy.every(validItem)) throw new Error('旧模板读取失败，原记录未覆盖')
    for (const item of legacy) if (!library.items.some(t => t.id === item.id)) library.items.push({ ...item, builtin: false })
  }
  return library
}
export function deleteTemplate(library, id) {
  return { ...library, items: library.items.filter(item => item.id !== id), removed: [...new Set([...library.removed, ...(BUILTIN_TEMPLATES.some(t => t.id === id) ? [id] : [])])] }
}
export function restoreMissingTemplates(library) {
  return { ...library, items: [...library.items, ...BUILTIN_TEMPLATES.filter(t => !library.items.some(item => item.id === t.id)).map(t => ({ ...t }))], removed: [] }
}
