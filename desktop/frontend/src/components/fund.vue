<script setup>
import {ref, onBeforeUnmount} from "vue";
import {EventsOn, EventsOff} from "../../wailsjs/runtime";
import {useRoute} from 'vue-router'
import FundFollow from "./FundFollow.vue";
import FundRanking from "./FundRanking.vue";

const nowTab = ref("基金自选")
const route = useRoute()

nowTab.value = route.query.name || '基金自选'

EventsOn("changeFundTab", async (msg) => {
  nowTab.value = msg.name
})

onBeforeUnmount(() => {
  EventsOff("changeFundTab")
})
</script>

<template>
  <n-card>
    <n-tabs type="line" animated v-model:value="nowTab" style="--wails-draggable:no-drag">
      <n-tab-pane name="基金自选" display-directive="show">
        <FundFollow/>
      </n-tab-pane>
      <n-tab-pane name="基金排行" display-directive="show">
        <FundRanking/>
      </n-tab-pane>
    </n-tabs>
  </n-card>
</template>
