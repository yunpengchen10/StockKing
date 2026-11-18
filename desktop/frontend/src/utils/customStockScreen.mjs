const fields = [
  { key: 'gap', label: '距涨停', aliases: /^(?:距离?涨停(?:价)?(?:空间|幅度)?|涨停空间|剩余涨幅)/, unit: '%', units: ['%', '个百分点'] },
  { key: 'size', label: '卖一量', aliases: /^(?:卖一(?:挂单量|挂单|数量|股数|量)|卖盘量)/, unit: '股', units: ['股', '万股'] },
  { key: 'price', label: '股价', aliases: /^(?:最新价|当前价|现价|股价|价格)/, unit: '元', units: ['元', '块'] },
  { key: 'change', label: '涨幅', aliases: /^(?:(?:当日|今日|当前)?涨跌?幅)/, unit: '%', units: ['%', '个百分点'] },
]
const comparisons = [
  ['>=', '大于等于|不小于|不少于|不低于|高于等于|至少|达到|>=|≥'],
  ['<=', '小于等于|不大于|不多于|不高于|不超过|最多|至多|<=|≤'],
  ['>', '大于|高于|多于|超过|>'],
  ['<', '小于|低于|少于|不到|<'],
  ['=', '等于|恰为|为|==|='],
]
const amountPattern = '([+-]?(?:\\d+(?:\\.\\d+)?|\\.\\d+))(个百分点|万股|股|元|块|%)?'
const rangePattern = new RegExp('^' + amountPattern + '(?:至|到|~|-)' + amountPattern + '(?:之间)?$')
const valuePattern = new RegExp('^' + amountPattern + '$')
const fieldStart = '股价|现价|价格|当前价|最新价|涨幅|涨跌幅|距涨停|距离涨停|涨停空间|卖一|名称|代码|题材|概念'
const format = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 })
const numeric = value => typeof value === 'number' && Number.isFinite(value)
const canonical = value => String(value || '').normalize('NFKC').trim()

function verified(row) {
  const entry = row?.limitEntry
  return /^\d{6}$/.test(row?.code) && entry?.code === row.code && entry.eligible === true && entry.status === 'pre_limit'
    && [entry.price, entry.sessionHigh, entry.limitUp, entry.askPrice, entry.askVolumeShares].every(v => numeric(v) && v > 0)
    && entry.price <= entry.sessionHigh && entry.sessionHigh < entry.limitUp && entry.askPrice < entry.limitUp
    && Number.isSafeInteger(entry.askVolumeShares) && Boolean(entry.source && entry.limitSource && entry.bookSource)
    && [entry.sourceTime, entry.limitSourceTime, entry.bookSourceTime].every(stamp => Number.isFinite(Date.parse(stamp)))
}

function fieldValue(row, key) {
  const entry = row.limitEntry
  if (key === 'gap') return (entry.limitUp / entry.price - 1) * 100
  if (key === 'size') return entry.askVolumeShares
  if (key === 'price') return entry.price
  return row.changePct
}

function quantity(value, unit, field) {
  if (unit && !field.units.includes(unit)) throw new Error(field.label + '单位应为' + field.units.join('或'))
  const number = Number(value) * (unit === '万股' ? 10000 : 1)
  if (!numeric(number) || (field.key !== 'change' && number < 0) || (field.key === 'size' && !Number.isSafeInteger(number))) {
    throw new Error(field.label + '数值无效')
  }
  return number
}

function compare(value, operator, bound) {
  const delta = value - bound
  if (Math.abs(delta) <= 1e-8) return ['=', '>=', '<='].includes(operator)
  if (operator === '>' || operator === '>=') return delta > 0
  if (operator === '<' || operator === '<=') return delta < 0
  return false
}

function numericRule(field, original) {
  const body = original.replace(field.aliases, '').replace(/^[为在=:](?=.+)/, '')
  const range = body.match(rangePattern)
  const bounds = []
  if (range) {
    // In "1至3万股" the shared unit applies to both bounds. An explicit
    // left-hand unit avoids ambiguity for mixed scales such as "1000股至1万股".
    const lower = quantity(range[1], range[2] || range[4], field)
    const upper = quantity(range[3], range[4] || range[2], field)
    if (lower > upper) throw new Error(field.label + '区间下限大于上限，请给两端写明单位')
    bounds.push(['>=', lower], ['<=', upper])
  } else {
    let amount = body
    let operator = '='
    for (const [op, expression] of comparisons) {
      const match = body.match(new RegExp('^(?:' + expression + ')'))
      if (match) { operator = op; amount = body.slice(match[0].length); break }
    }
    const suffix = amount.match(/(以上|及以上|以下|及以下|以内)$/)
    if (suffix) {
      if (amount !== body) throw new Error(field.label + '比较条件重复')
      operator = suffix[0].includes('上') ? '>=' : '<='
      amount = amount.slice(0, -suffix[0].length)
    }
    const value = amount.match(valuePattern)
    if (!value) throw new Error('无法识别条件「' + original + '」')
    bounds.push([operator, quantity(value[1], value[2], field)])
  }
  const description = field.label + bounds.map(([op, limit]) => op + format.format(limit) + field.unit).join('且')
  return { description, match(row) {
    const actual = fieldValue(row, field.key)
    if (!numeric(actual) || !bounds.every(([op, bound]) => compare(actual, op, bound))) return null
    return field.label + ' ' + format.format(actual) + field.unit
  } }
}

function textRule(key, value, exact) {
  const label = { name: '名称', theme: '题材', code: '代码' }[key]
  const wanted = value.toLowerCase()
  return { description: label + (exact ? '为' : '包含') + value, match(row) {
    const items = key === 'theme' ? row.themes : [row[key]]
    const actual = Array.isArray(items) && items.find(item => typeof item === 'string' &&
      (exact ? canonical(item).toLowerCase() === wanted : canonical(item).toLowerCase().includes(wanted)))
    return actual ? label + ' ' + actual : null
  } }
}

function parseClause(raw, knownNames) {
  const compact = raw.replace(/\s+/g, '')
  if (compact.includes('或')) throw new Error('仅支持同时满足多个条件，请拆开「' + raw + '」')
  const field = fields.find(f => f.aliases.test(compact))
  if (field) return numericRule(field, compact)
  if (/^(?:当日|今日)?(?:尚未触板|未触板|未涨停|有卖盘)$/.test(compact)) {
    return { description: '当日未触板且有卖盘', match: () => '当日未触板且有卖盘' }
  }
  const code = compact.match(/^(?:(?:股票|证券)?代码(?:为|是|等于|=|:)?)?(?:(?:sh|sz|bj))?(\d{6})(?:\.(?:sh|sz|bj))?$/i)
  if (code) return textRule('code', code[1], true)
  const text = raw.match(/^(股票名称|证券简称|名称|名字|题材|概念)\s*(包含|含有|含|为|是|等于|=|:)?\s*(.+)$/)
  if (text) {
    const value = text[3].trim().replace(/^[「“"](.+)[」”"]$/, '$1').trim()
    if (!value || /^(?:为|是|等于|含|含有|包含)$/.test(value) || !/^[\p{L}\p{N}·*+\- ]{1,40}$/u.test(value) || /[<>]|(?:高于|低于|不少于|不超过|大于|小于)/.test(value)) {
      throw new Error('无法识别条件「' + raw + '」')
    }
    const key = ['题材', '概念'].includes(text[1]) ? 'theme' : 'name'
    return textRule(key, value, ['为', '是', '等于', '='].includes(text[2]))
  }
  if (knownNames.has(raw)) return textRule('name', raw, true)
  throw new Error('无法识别条件「' + raw + '」')
}

export function runCustomStockScreen(text, rows) {
  const universe = Array.isArray(rows) ? rows : []
  const eligible = universe.filter(verified)
  const result = { status: 'empty', errors: [], conditions: [], matches: [], eligibleCount: eligible.length }
  const input = canonical(text).replace(/^(?:请)?(?:帮我)?(?:筛选出?|筛出|选出|挑选)\s*/, '')
  if (!input) return result
  const names = new Set(universe.map(row => canonical(row?.name)).filter(Boolean))
  const separated = input.split(/[,，;；、\n]|并且|而且|同时|以及|且|并(?=(?:股价|涨幅|卖一|距涨停|题材))/)
  const spaceBeforeField = new RegExp('\\s+(?=(?:' + fieldStart + '))')
  const clauses = separated.flatMap(part => part.trim().split(spaceBeforeField))
  if (clauses.some(clause => !clause)) return { ...result, status: 'invalid', errors: ['条件分隔处缺少内容'] }
  const rules = []
  for (const clause of clauses) {
    try { rules.push(parseClause(clause, names)) }
    catch (error) { result.errors.push(error.message) }
  }
  if (result.errors.length) return { ...result, status: 'invalid' }
  result.status = 'ready'
  result.conditions = rules.map(rule => rule.description)
  for (const row of eligible) {
    const reasons = rules.map(rule => rule.match(row))
    if (reasons.every(Boolean)) result.matches.push({ row, reasons })
  }
  return result
}
