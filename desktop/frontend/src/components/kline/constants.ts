// Stock King uses the A-share convention across every market: red up, green down.
export const CLR_RISE = '#f05264'
export const CLR_FALL = '#22ab94'

export const DAILY_LIKE_KLT = new Set(['101', '102', '103', '104', '106'])
export const CN_TZ = 'Asia/Shanghai'

export const HISTORY_PAGE_SIZE = 400
export const BARS_BEFORE_LOAD_MORE = 45
export const DEFAULT_VISIBLE_BARS = 180
export const DEFAULT_RIGHT_LOGICAL_GAP = 18
export const SHOW_CHIP_TOOLBAR_BUTTON = false

// 复权类型：qfq=前复权（默认）、hfq=后复权、none=不复权
// 仅日K及更长周期（DAILY_LIKE_KLT）有效；分时周期传空串走各数据源默认行为
export const DEFAULT_ADJUST = 'qfq'
export const ADJUST_OPTIONS = [
  { value: 'qfq', label: '前复权' },
  { value: 'hfq', label: '后复权' },
  { value: 'none', label: '不复权' },
]

export const INTERVALS = [
  { klt: '1', label: '1分', limit: 1000 },
  { klt: '5', label: '5分', limit: 600 },
  { klt: '15', label: '15分', limit: 500 },
  { klt: '30', label: '30分', limit: 500 },
  { klt: '60', label: '60分', limit: 500 },
  { klt: '101', label: '日K', limit: 800 },
  { klt: '102', label: '周K', limit: 520 },
  { klt: '103', label: '月K', limit: 240 },
  { klt: '104', label: '季K', limit: 120 },
  { klt: '106', label: '年K', limit: 40 },
]
