import test from 'node:test'
import assert from 'node:assert/strict'
import { createRenderer, defineComponent, h, KeepAlive, nextTick, onMounted, ref, watch } from 'vue'
import { createMemoryHistory, createRouter, RouterView, useRoute } from 'vue-router'
import { createPageNavigationMemory, PageSession, usePageActive } from './pageSession.mjs'

// Exercise real Vue KeepAlive / RouterView lifecycle without a desktop bridge.
function makeRenderer() {
  const node = tag => ({ tag, children: [], parent: null, scrollTop: 0, scrollLeft: 0,
    querySelectorAll() { return this.children.flatMap(child => [child, ...child.querySelectorAll()]) },
  })
  const detach = child => {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1)
    child.parent = null
  }
  return { node, renderer: createRenderer({
    createElement: node, createText: text => ({ ...node('#text'), text }), createComment: text => ({ ...node('#comment'), text }),
    setElementText: (element, text) => { element.text = text }, setText: (element, text) => { element.text = text },
    patchProp: (element, key, _old, value) => { element[key] = value },
    insert(child, parent, anchor) { detach(child); const index = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(index < 0 ? parent.children.length : index, 0, child); child.parent = parent },
    remove: detach, parentNode: element => element.parent,
    nextSibling: element => element.parent?.children[element.parent.children.indexOf(element) + 1] || null,
  }) }
}

test('returning to a page preserves its subpage, scroll and route without remounting or observing other stocks', async () => {
  const { renderer, node } = makeRenderer()
  const sessions = {}, mounts = {}, memory = createPageNavigationMemory()
  const page = name => defineComponent({ setup() {
    const route = useRoute(), active = usePageActive(), tab = ref('home'), changes = []
    watch(() => route.query.code, code => changes.push(code))
    onMounted(() => { mounts[name] = (mounts[name] || 0) + 1 })
    sessions[name] = { route, active, tab, changes }
    return () => h('main', [h('section', { class: 'inner-table' }, tab.value)])
  } })
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/picks', name: 'picks', component: page('picks') },
    { path: '/chart', name: 'chart', component: page('chart') },
  ] })
  router.afterEach((to, _from, failure) => { if (!failure) memory.remember(to) })
  await router.push('/picks?code=600000.SH')
  const app = renderer.createApp({ render: () => h(RouterView, {}, {
    default: ({ Component, route }) => h(KeepAlive, {}, () => Component ? h(PageSession, { key: route.name, component: Component, location: route }) : null),
  }) })
  app.use(router)
  const root = node('root')
  app.mount(root)
  await nextTick()
  const picksElement = root.children[0], tableElement = picksElement.children[0].children[0]
  sessions.picks.tab.value = 'records'
  picksElement.scrollTop = 480; tableElement.scrollLeft = 95

  await router.push('/chart?code=000001.SZ')
  await nextTick()
  assert.equal(sessions.picks.active.value, false)
  assert.equal(sessions.picks.route.query.code, '600000.SH')
  assert.deepEqual(sessions.picks.changes, [])
  // WebViews may reset detached scroll containers; activation must restore them.
  picksElement.scrollTop = 0; tableElement.scrollLeft = 0
  await router.push(memory.destination('picks'))
  await nextTick(); await nextTick()
  assert.equal(sessions.picks.active.value, true)
  assert.equal(sessions.chart.active.value, false)
  assert.equal(sessions.picks.tab.value, 'records')
  assert.equal(mounts.picks, 1)
  assert.equal(picksElement.scrollTop, 480)
  assert.equal(tableElement.scrollLeft, 95)

  // An explicit stock link still updates the existing page normally.
  await router.push('/chart?code=300001.SZ')
  await nextTick()
  assert.equal(mounts.chart, 1)
  assert.equal(sessions.chart.route.query.code, '300001.SZ')
  assert.deepEqual(sessions.chart.changes, ['300001.SZ'])
  assert.deepEqual(sessions.picks.changes, [])
  const historyStep = direction => new Promise(resolve => {
    const removeHook = router.afterEach(() => { removeHook(); resolve() })
    router[direction]()
  })
  await historyStep('back')
  await nextTick(); await nextTick()
  assert.equal(sessions.picks.tab.value, 'records')
  assert.equal(picksElement.scrollTop, 480)
  assert.equal(sessions.picks.route.query.code, '600000.SH')
  await historyStep('forward')
  await nextTick()
  assert.equal(sessions.chart.route.query.code, '300001.SZ')
  assert.equal(mounts.chart, 1)
  app.unmount()
})

test('navigation memory copies params, query and hash and preserves distinct destinations', () => {
  const memory = createPageNavigationMemory()
  const route = { name: 'chart', params: {}, query: { code: '600000.SH', name: '浦发银行' }, hash: '#notes' }
  memory.remember(route)
  route.query.code = '000001.SZ'
  memory.remember({ name: 'picks', query: { tab: 'records' }, hash: '' })
  assert.deepEqual(memory.destination('chart'), { name: 'chart', params: {}, query: { code: '600000.SH', name: '浦发银行' }, hash: '#notes' })
  assert.deepEqual(memory.destination('settings'), { name: 'settings' })
})
