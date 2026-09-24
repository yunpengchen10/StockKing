<script setup>
defineProps({ indicators: { type: Array, default: () => [] } })
</script>

<template>
  <div class="indicator-evidence">
    <p v-if="!indicators.length" class="missing-evidence">未取得指标记录；不能核验入选条件。</p>
    <article v-for="(indicator, index) in indicators" :key="`${indicator.key}-${index}`" class="indicator-row">
      <div class="indicator-heading"><b>{{ indicator.label }}</b><strong>{{ indicator.value }}</strong></div>
      <div class="indicator-purpose"><span>{{ indicator.role }}</span><span>{{ indicator.status }}</span></div>
      <dl>
        <dt>阈值 / 判断</dt><dd>{{ indicator.threshold }}</dd>
        <dt>来源</dt><dd>{{ indicator.source }}</dd>
        <dt>数据时间</dt><dd>{{ indicator.asOf }}</dd>
      </dl>
    </article>
  </div>
</template>

<style scoped>
.indicator-evidence{display:grid;gap:8px;margin:12px 0;font-size:11px;line-height:1.65}
.indicator-row{padding:10px;border:1px solid var(--sk-border);border-radius:7px;background:var(--sk-surface-2);overflow-wrap:anywhere}
.indicator-heading{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.indicator-heading b{font-weight:500}.indicator-heading strong{font-size:14px;font-variant-numeric:tabular-nums;flex-shrink:0}
.indicator-purpose{display:flex;gap:10px;margin:3px 0;color:var(--sk-text-muted)}
dl{display:grid;grid-template-columns:70px 1fr;gap:2px 8px;margin:7px 0 0}dt{color:var(--sk-text-muted)}dd{margin:0}
.missing-evidence{color:var(--sk-text-muted)}
</style>
