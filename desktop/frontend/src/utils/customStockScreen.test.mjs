import assert from 'node:assert/strict'
import test from 'node:test'
import { runCustomStockScreen } from './customStockScreen.mjs'

const at = '2026-11-16T14:30:00+08:00'
function stock(code, name, price, limitUp, changePct, size, themes) {
  return { code, name, changePct, themes, limitEntry: {
    code, eligible: true, status: 'pre_limit', price, sessionHigh: price, limitUp,
    askPrice: price + .01, askVolumeShares: size, sourceTime: at, limitSourceTime: at, bookSourceTime: at,
    source: 'q', limitSource: 'q', bookSource: 'q',
  } }
}
const a = stock('600001', '甲能源', 20, 21, 3.27, 18000, ['机器人', '新能源'])
const b = stock('000002', '乙能源', 25, 26, -1.5, 5000, ['新能源'])
const c = stock('600003', '丙科技', 10, 11, undefined, 30000, [])
const rows = [a, b, c]
const codes = query => runCustomStockScreen(query, rows).matches.map(match => match.row.code)

test('numeric ranges, AND, actual matched data and percentage headroom', () => {
  const result = runCustomStockScreen('股价10至30元，涨幅2%到6%，距涨停至少5%，卖一量不少于1万股', rows)
  assert.equal(result.status, 'ready')
  assert.deepEqual(result.matches.map(match => match.row), [a])
  assert.deepEqual(result.matches[0].reasons, ['股价 20元', '涨幅 3.27%', '距涨停 5%', '卖一量 18,000股'])
  assert.equal(result.conditions.length, 4)
  assert.deepEqual(codes('距离涨停空间>5%'), [c.code])
  assert.deepEqual(codes('卖一量在1至3万股之间'), [a.code, c.code])
  assert.deepEqual(codes('涨幅-2%至-1%'), [b.code])
  assert.deepEqual(codes('涨幅≥０％ 且 股价不超过２０元'), [a.code])
  assert.deepEqual(codes('股价20元及以下'), [a.code, c.code])
})

test('text and identity filters can be combined without adding unverified themes', () => {
  assert.deepEqual(codes('题材包含新能源 且 名称含能源，股价高于20元'), [b.code])
  assert.deepEqual(codes('请帮我选出代码为000002'), [b.code])
  assert.deepEqual(codes('SH600001'), [a.code])
  assert.deepEqual(codes('600001.SH'), [a.code])
  assert.deepEqual(codes('丙科技'), [c.code])
  assert.deepEqual(codes('名称为甲'), [])
  assert.deepEqual(codes('概念=能源'), [])
  assert.deepEqual(codes('概念含能源'), [a.code, b.code])
  assert.deepEqual(codes('未触板 且 有卖盘'), [a.code, b.code, c.code])
})

test('missing data, incompatible units, unknown clauses and blank requests never match by default', () => {
  assert.deepEqual(codes('股价低于12元，涨幅高于0%'), [])
  assert.deepEqual(codes('概念包含科技'), [])
  for (const input of ['', '  ']) {
    const result = runCustomStockScreen(input, rows)
    assert.equal(result.status, 'empty')
    assert.deepEqual(result.matches, [])
  }
  for (const input of ['涨幅大约5%', '股价>10元，明天一定涨停', '股价>10元或涨幅>2%',
    '价格30至10元', '涨幅>2元', '卖一量1.5股', '题材为', '股价>20元，', '低估值']) {
    const result = runCustomStockScreen(input, rows)
    assert.equal(result.status, 'invalid', input)
    assert.ok(result.errors.length, input)
    assert.deepEqual(result.matches, [], input)
  }
})

test('locked, reopened, missing and contradictory evidence never enters the custom screener', () => {
  const badRows = [{ status: 'at_limit' }, { status: 'touched_limit' }, { eligible: false },
    { askVolumeShares: 0 }, { code: '600005' }, { askPrice: 21 }, { sessionHigh: 21 }, { limitSource: null },
    { bookSourceTime: '' }, { price: NaN }].map(patch => ({ ...a, limitEntry: { ...a.limitEntry, ...patch } }))
  const result = runCustomStockScreen('股价>5元', [...badRows, a])
  assert.equal(result.eligibleCount, 1)
  assert.deepEqual(result.matches.map(match => match.row), [a])
})
