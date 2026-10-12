// The engine has a short deadline for record reads. Keep controls usable even
// if an interrupted Wails bridge never sends the rejection to the browser.
export async function withRecordReadTimeout(request, timeoutMs = 20000, timers = globalThis) {
  let timer
  try {
    return await Promise.race([
      request,
      new Promise((_, reject) => {
        timer = timers.setTimeout(() => reject(new Error('记录读取超时，请重试或缩小查询日期范围')), timeoutMs)
      }),
    ])
  } finally {
    timers.clearTimeout(timer)
  }
}
