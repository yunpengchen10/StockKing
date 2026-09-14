import test from 'node:test'
import assert from 'node:assert/strict'
import { watchReturn } from './watchlist.mjs'
test('selection returns do not require positions; missing baselines stay missing',()=>{
 const row={SelectionPrice:10,Price:11,LatestQuoteTime:'2026-09-10'}
 assert.ok(Math.abs(watchReturn(row)-10)<1e-10)
 assert.ok(Math.abs(watchReturn({...row,Price:9})+10)<1e-10)
 for(const value of [null,undefined,0,NaN,Infinity])assert.equal(watchReturn({...row,SelectionPrice:value}),null)
 assert.equal(watchReturn({...row,LatestQuoteTime:''}),null)
})
