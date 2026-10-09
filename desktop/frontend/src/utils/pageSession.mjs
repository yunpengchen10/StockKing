import { defineComponent, h, inject, nextTick, onActivated, onBeforeUnmount, onDeactivated, provide, ref, shallowReactive, watch } from 'vue'
import { onBeforeRouteLeave, routeLocationKey } from 'vue-router'

const pageActiveKey = Symbol('stock-king.page-active')

// A page keeps its own route while cached. Otherwise useRoute() in an inactive
// chart/research page observes another page's stock code and discards its work.
export function usePageActive() {
  return inject(pageActiveKey, ref(true))
}

export function createPageNavigationMemory() {
  const locations = shallowReactive({})
  return {
    remember(route) {
      if (!route?.name) return
      locations[route.name] = { name: route.name, params: { ...route.params }, query: { ...route.query }, hash: route.hash || '' }
    },
    destination(name) {
      return locations[name] || { name }
    },
  }
}

export function captureScrollPositions(root) {
  if (!root) return []
  return [root, ...root.querySelectorAll('*')]
    .filter(element => element.scrollTop || element.scrollLeft)
    .map(element => ({ element, top: element.scrollTop, left: element.scrollLeft }))
}

export function restoreScrollPositions(positions) {
  for (const { element, top, left } of positions) {
    element.scrollTop = top
    element.scrollLeft = left
  }
}

export const PageSession = defineComponent({
  name: 'PageSession',
  props: { component: { required: true }, location: { type: Object, required: true } },
  setup(props) {
    const root = ref(null)
    const active = ref(true)
    const pageRoute = shallowReactive({ ...props.location })
    let scrollPositions = []
    provide(pageActiveKey, active)
    provide(routeLocationKey, pageRoute)
    watch(() => props.location, location => Object.assign(pageRoute, location), { flush: 'sync' })
    onBeforeRouteLeave(() => { scrollPositions = captureScrollPositions(root.value) })
    onActivated(() => {
      active.value = true
      void nextTick(() => { if (active.value) restoreScrollPositions(scrollPositions) })
    })
    onDeactivated(() => { active.value = false })
    onBeforeUnmount(() => { active.value = false; scrollPositions = [] })
    return () => h('div', { ref: root, class: 'sk-page-session' }, [h(props.component)])
  },
})
