<script setup>
import { GetFundKLine } from '../../wailsjs/go/main/App'
import {
  CandlestickSeries,
  createChart,
  HistogramSeries,
  LineSeries,
} from 'lightweight-charts'
import { NButton, NFlex, NSpin, NText } from 'naive-ui'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const CLR_RISE = '#e1000f'
const CLR_FALL = '#26a69a'
const DAILY_LIKE_KLT = new Set(['101', '102', '103'])

const ALL_INTERVALS = [
  { klt: '1', label: '1分', limit: 500 },
  { klt: '5', label: '5分', limit: 500 },
  { klt: '15', label: '15分', limit: 500 },
  { klt: '30', label: '30分', limit: 500 },
  { klt: '60', label: '60分', limit: 500 },
  { klt: '101', label: '日K', limit: 600 },
  { klt: '102', label: '周K', limit: 400 },
  { klt: '103', label: '月K', limit: 200 },
]

function isOnExchangeFund(code) {
  const p = code.substring(0, 2)
  return ['15', '16', '50', '51', '52'].includes(p)
}

const props = defineProps({
  fundCode: { type: String, default: '' },
  fundName: { type: String, default: '' },
  darkTheme: { type: Boolean, default: false },
  chartHeight: { type: Number, default: 400 },
})

const chartContainerRef = ref(null)
const activeKlt = ref('101')
const showMA = ref(false)
const loading = ref(false)
const errorText = ref('')
const dataSource = ref('')
const isCandleMode = ref(true)

const intervals = computed(() => {
  if (isOnExchangeFund(props.fundCode)) return ALL_INTERVALS
  return ALL_INTERVALS.filter(i => DAILY_LIKE_KLT.has(i.klt))
})

let chart = null
let candleSeries = null
let lineSeries = null
let volumeSeries = null
let maSeriesMap = {}

function parseKLineData(raw, klt) {
  if (!raw || !raw.length) return { candles: [], lineData: [], volumes: [], maData: {} }
  const candles = []
  const lineData = []
  const volumes = []
  const maData = {}
  const isDailyLike = DAILY_LIKE_KLT.has(klt)

  for (const item of raw) {
    const time = isDailyLike ? item.day.split(' ')[0] : item.day
    const o = parseFloat(item.open)
    const c = parseFloat(item.close)
    const h = parseFloat(item.high)
    const l = parseFloat(item.low)

    candles.push({ time, open: o, high: h, low: l, close: c })
    lineData.push({ time, value: c })

    const v = parseFloat(item.volume) || 0
    volumes.push({
      time,
      value: v,
      color: c >= o ? CLR_RISE + '80' : CLR_FALL + '80',
    })

    if (item.ma) {
      for (const [key, val] of Object.entries(item.ma)) {
        if (!maData[key]) maData[key] = []
        maData[key].push({ time, value: parseFloat(val) })
      }
    }
  }

  return { candles, lineData, volumes, maData }
}

function clearMA() {
  for (const key of Object.keys(maSeriesMap)) {
    if (maSeriesMap[key] && chart) {
      try { chart.removeSeries(maSeriesMap[key]) } catch (_e) { /* ignore */ }
    }
  }
  maSeriesMap = {}
}

function drawMA(maData) {
  clearMA()
  if (!showMA.value || !maData || !chart) return
  const colors = { ma5: '#e6a23c', ma10: '#409eff', ma20: '#c0c0c0', ma30: '#f56c6c', ma60: '#67c23a' }
  for (const [key, data] of Object.entries(maData)) {
    if (!data.length) continue
    const series = chart.addSeries(LineSeries, {
      color: colors[key] || '#999',
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    })
    series.setData(data)
    maSeriesMap[key] = series
  }
}

function removeSeries() {
  if (candleSeries && chart) { try { chart.removeSeries(candleSeries) } catch (_e) {} candleSeries = null }
  if (lineSeries && chart) { try { chart.removeSeries(lineSeries) } catch (_e) {} lineSeries = null }
  if (volumeSeries && chart) { try { chart.removeSeries(volumeSeries) } catch (_e) {} volumeSeries = null }
  clearMA()
}

async function fetchKLine() {
  if (!props.fundCode || !chart) return
  loading.value = true
  errorText.value = ''

  try {
    const interval = intervals.value.find(i => i.klt === activeKlt.value)
    const limit = interval ? interval.limit : 600
    const result = await GetFundKLine(props.fundCode, activeKlt.value, limit)

    if (!result || !result.data || !result.data.length) {
      errorText.value = '暂无K线数据'
      loading.value = false
      return
    }

    dataSource.value = result.source || ''
    isCandleMode.value = result.source.includes('K线')
    removeSeries()

    const { candles, lineData, volumes, maData } = parseKLineData(result.data, activeKlt.value)

    if (isCandleMode.value) {
      candleSeries = chart.addSeries(CandlestickSeries, {
        upColor: CLR_RISE,
        downColor: CLR_FALL,
        borderUpColor: CLR_RISE,
        borderDownColor: CLR_FALL,
        wickUpColor: CLR_RISE,
        wickDownColor: CLR_FALL,
      })
      candleSeries.setData(candles)

      volumeSeries = chart.addSeries(HistogramSeries, {
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume',
      })
      volumeSeries.priceScale().applyOptions({
        scaleMargins: { top: 0.8, bottom: 0 },
      })
      volumeSeries.setData(volumes)
    } else {
      lineSeries = chart.addSeries(LineSeries, {
        color: CLR_RISE,
        lineWidth: 2,
        priceLineVisible: true,
        lastValueVisible: true,
      })
      lineSeries.setData(lineData)
    }

    drawMA(maData)
    chart.timeScale().fitContent()
  } catch (e) {
    errorText.value = '加载失败: ' + (e.message || e)
  } finally {
    loading.value = false
  }
}

function createChartInstance() {
  if (!chartContainerRef.value) return
  if (chart) {
    chart.remove()
    chart = null
  }

  const bg = props.darkTheme ? '#1e1e1e' : '#ffffff'
  const txt = props.darkTheme ? '#d1d4dc' : '#333'
  const grid = props.darkTheme ? '#2b2b43' : '#e1e1e1'

  chart = createChart(chartContainerRef.value, {
    width: chartContainerRef.value.clientWidth,
    height: props.chartHeight,
    layout: { background: { color: bg }, textColor: txt },
    grid: { vertLines: { color: grid }, horzLines: { color: grid } },
    crosshair: { mode: 0 },
    rightPriceScale: { borderColor: grid },
    timeScale: {
      borderColor: grid,
      timeVisible: !DAILY_LIKE_KLT.has(activeKlt.value),
      secondsVisible: false,
    },
  })
}

function switchPeriod(klt) {
  if (activeKlt.value === klt) return
  activeKlt.value = klt

  if (chart) {
    chart.timeScale().applyOptions({
      timeVisible: !DAILY_LIKE_KLT.has(klt),
    })
  }
  fetchKLine()
}

watch(() => showMA.value, () => fetchKLine())

onMounted(() => {
  nextTick(() => {
    createChartInstance()
    fetchKLine()
  })
})

onBeforeUnmount(() => {
  if (chart) { chart.remove(); chart = null }
})

watch(() => props.fundCode, (nv, ov) => {
  if (nv && nv !== ov) {
    activeKlt.value = '101'
    showMA.value = false
    nextTick(() => {
      if (!chart) createChartInstance()
      fetchKLine()
    })
  }
})

let resizeObserver = null
onMounted(() => {
  if (chartContainerRef.value) {
    resizeObserver = new ResizeObserver(() => {
      if (chart && chartContainerRef.value) {
        chart.applyOptions({ width: chartContainerRef.value.clientWidth })
      }
    })
    resizeObserver.observe(chartContainerRef.value)
  }
})

onBeforeUnmount(() => {
  if (resizeObserver) resizeObserver.disconnect()
})
</script>

<template>
  <div>
    <n-flex justify="space-between" align="center" style="margin-bottom: 8px">
      <n-flex align="center" :wrap="false">
        <n-button
          v-for="interval in intervals"
          :key="interval.klt"
          :type="activeKlt === interval.klt ? 'primary' : 'default'"
          size="tiny"
          @click="switchPeriod(interval.klt)"
        >
          {{ interval.label }}
        </n-button>
        <n-button
          size="tiny"
          :type="showMA ? 'warning' : 'default'"
          @click="showMA = !showMA"
        >
          MA
        </n-button>
      </n-flex>
      <n-text v-if="dataSource" depth="3" style="font-size: 12px">
        数据源: {{ dataSource }}
      </n-text>
    </n-flex>

    <n-spin :show="loading">
      <div ref="chartContainerRef" :style="{ height: chartHeight + 'px' }"></div>
    </n-spin>
    <n-text v-if="errorText" type="error" style="margin-top: 8px; display: block">{{ errorText }}</n-text>
  </div>
</template>
