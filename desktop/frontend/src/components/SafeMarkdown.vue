<script setup>
import { computed, nextTick, watch } from 'vue'
import { renderSafeMarkdown } from '../utils/aiResearch.mjs'
import '../assets/report-markdown.css'

const props = defineProps({ modelValue: { type: String, default: '' }, theme: String, editorId: String })
const emit = defineEmits(['onHtmlChanged'])
const html = computed(() => renderSafeMarkdown(props.modelValue))
watch(html, async value => { await nextTick(); emit('onHtmlChanged', value) }, { immediate: true })
</script>

<template>
  <div class="md-editor-preview-wrapper" :id="editorId">
    <article class="md-editor-preview sk-report-markdown" v-html="html" />
  </div>
</template>
