<script setup>
import { computed, ref } from 'vue'
import { NDrawer, NDrawerContent } from 'naive-ui'
import { INDICATORS, combinationsFor, indicatorFor } from '../utils/indicatorGuide.mjs'
const props=defineProps({ show:Boolean, selected:{type:String,default:'MA'}, enabled:{type:Array,default:()=>[]}, signal:String, asOf:String })
const emit=defineEmits(['update:show','select','toggle','apply'])
const search=ref('')
const selectedItem=computed(()=>indicatorFor(props.selected)||INDICATORS[0])
const matches=computed(()=>INDICATORS.filter(item=>`${item.key} ${item.name} ${item.group}`.toLowerCase().includes(search.value.toLowerCase())))
const combos=computed(()=>combinationsFor(selectedItem.value.key))
const status=computed(()=>({bullish:'偏多',bearish:'偏空',oscillating:'震荡 / 极值',neutral:'中性'})[props.signal]||'未参与 / 数据不足')
</script>
<template>
  <NDrawer :show="show" :width="460" placement="right" :auto-focus="true" @update:show="emit('update:show',$event)">
    <NDrawerContent title="指标与读法" closable body-content-style="padding:16px">
      <section class="indicator-guide">
        <label class="search">查找指标<input v-model="search" aria-label="查找指标" placeholder="MA、均线、量能…"></label>
        <div class="catalog" aria-label="全部指标"><button v-for="item in matches" :key="item.key" :aria-pressed="selectedItem.key===item.key" :class="{selected:selectedItem.key===item.key}" @click="emit('select',item.key)"><span>{{ item.name }}</span><small>{{ enabled.includes(item.key) ? '✓ 已画' : item.group }}</small></button><p v-if="!matches.length">没有匹配指标</p></div>
        <article class="guide-detail" :aria-label="selectedItem.name+'说明'">
          <header><h3>{{ selectedItem.name }}</h3><button class="draw-toggle" :aria-pressed="enabled.includes(selectedItem.key)" @click="emit('toggle',selectedItem.key)">{{ enabled.includes(selectedItem.key)?'✓ 已画 · 隐藏':'画到图上' }}</button></header>
          <p class="status">{{ status }} · {{ asOf || '暂无K线时点' }}</p>
          <p>{{ selectedItem.read }}</p>
          <div v-for="combo in combos" :key="combo.id" class="combo"><header><b>{{ combo.name }}</b><button @click="emit('apply',combo.keys)">叠加 {{ combo.keys.join(' + ') }}</button></header><p>{{ combo.steps }}</p><small>{{ combo.caution }}</small></div>
          <details :key="selectedItem.key" class="formula"><summary>公式与口径</summary><p>{{ selectedItem.formula }}</p><p v-if="selectedItem.caution">{{ selectedItem.caution }}</p><small>C/H/L/O/V = 收盘/最高/最低/开盘/成交量；N 指当前周期的 N 根K线。收盘前读数会变化；价格使用当前图表复权口径。</small></details>
          <p class="footnote">标签为简化条件，不是交易指令；占比包含所有可计算指标。</p>
        </article>
      </section>
    </NDrawerContent>
  </NDrawer>
</template>
<style scoped>
.indicator-guide{color:var(--sk-text);font-size:12px;line-height:1.7}.search{display:block;font-size:11px;color:var(--sk-text-muted)}input{box-sizing:border-box;display:block;width:100%;margin:6px 0 12px;padding:9px;border:1px solid var(--sk-border);border-radius:6px;background:var(--sk-surface-2);color:var(--sk-text)}.catalog{display:grid;grid-template-columns:1fr 1fr;gap:5px;max-height:205px;overflow:auto;padding:2px}.catalog button{display:flex;align-items:center;justify-content:space-between;text-align:left;gap:6px}.catalog small{white-space:nowrap;color:var(--sk-text-muted);font-size:9px}button{font:inherit;font-size:11px;color:var(--sk-text);border:1px solid var(--sk-border);border-radius:5px;background:var(--sk-surface-2);padding:6px 9px;cursor:pointer}button.selected,button[aria-pressed=true]{border-color:var(--sk-accent);color:var(--sk-accent)}button:focus-visible,input:focus-visible{outline:2px solid var(--sk-accent);outline-offset:2px}.guide-detail{margin-top:16px;border-top:1px solid var(--sk-border);padding-top:14px}header{display:flex;justify-content:space-between;align-items:center;gap:8px}h3{font-size:17px;margin:0}.status,.footnote{color:var(--sk-text-muted);font-size:10px}.combo{padding:12px;margin:12px 0;background:var(--sk-surface-2);border-radius:7px}.combo header{flex-wrap:wrap}.combo p{margin:8px 0}.combo small{color:var(--sk-text-muted)}.formula{border-top:1px solid var(--sk-border);padding-top:12px}.formula summary{cursor:pointer;color:var(--sk-accent)}.formula small{color:var(--sk-text-muted)}
</style>
