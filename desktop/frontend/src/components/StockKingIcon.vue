<script setup>
defineProps({ name: { type: String, default: 'overview' }, size: { type: [Number, String], default: 22 } })
const paths = {
  overview: ['M3 3h7v7H3zM14 3h7v4h-7zM14 11h7v10h-7zM3 14h7v7H3z'],
  portfolio: ['M4 7h16a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1Z', 'M8 7V4h8v3M3 12h18M10 12v3h4v-3'],
  chart: ['M5 3v4m0 7v7M3 7h4v7H3zM12 3v9m0 6v3M10 12h4v6h-4zM19 3v3m0 7v8M17 6h4v7h-4z'],
  watchlist: ['M5 3h14v18l-7-4-7 4V3Z', 'M9 8h6M9 12h4'],
  picks: ['m12 3 3.2 5.6 5.8 1.2-4 4.5.6 6.7-5.6-3-5.6 3 .6-6.7-4-4.5 5.8-1.2L12 3Z'],
  ai: ['M7 5h10l4 7-4 7H7l-4-7 4-7Z', 'm9 8 3 4-3 4M13 16h3'],
  quant: ['M4 3v18h17M8 16l4-5 4 2 5-8', 'M8 7h2M14 5h2'],
  grid: ['M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z'],
  tools: ['M4 5h16M4 12h16M4 19h16', 'M8 3v4M16 10v4M10 17v4'],
  settings: ['m9 3-1 3-3 1-2 5 2 5 3 1 1 3h6l1-3 3-1 2-5-2-5-3-1-1-3H9Z', 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z'],
  sun: ['M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5'],
  moon: ['M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11Z'],
  arrow: ['M5 12h14m-5-5 5 5-5 5'],
  search: ['M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15ZM16 16l5 5'],
  refresh: ['M20 7a9 9 0 0 0-15-2L3 7m0-4v4h4M4 17a9 9 0 0 0 15 2l2-2m0 4v-4h-4'],
}
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
    <path v-for="(path, index) in paths[name] || paths.overview" :key="index" :d="path" />
  </svg>
</template>
