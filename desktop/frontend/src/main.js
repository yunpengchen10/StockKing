import {createApp, nextTick} from 'vue'
import {LogInfo} from '../wailsjs/runtime'
import naive from 'naive-ui'
import App from './App.vue'
import router from './router/router'
import './assets/report-markdown.css'
// 引入组件库的少量全局样式变量
import 'tdesign-vue-next/es/style/index.css';

const app = createApp(App)

app.config.errorHandler = (err) => {
  if (err.message && err.message.includes('ResizeObserver')) {
    return
  }
  console.error(err)
}

window.addEventListener('error', (event) => {
  if (event.message && event.message.includes('ResizeObserver')) {
    event.preventDefault()
    return true
  }
})

window.addEventListener('unhandledrejection', (event) => {
  if (event.reason && event.reason.message && event.reason.message.includes('ResizeObserver')) {
    event.preventDefault()
    return true
  }
})

app.use(router)
app.use(naive)
app.mount('#app')

router.isReady().then(async () => {
  await nextTick()
  if (document.querySelector('.stock-king-shell')) {
    window.stockKingStartupComplete?.()
    // Written by the real Wails host, so packaged startup can be verified
    // independently of the development server and mock bridge tests.
    try { LogInfo('Stock King UI ready v2.4.4') } catch { /* browser preview */ }
  }
}).catch(() => window.stockKingStartupFailed?.())
