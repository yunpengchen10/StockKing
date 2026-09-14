<script setup>
import {h, onBeforeMount, onMounted, ref, reactive} from 'vue'
import {
  GetPromptTemplateList,
  GetConfig,
  AddPromptTemplate,
  DeletePromptTemplate,
  UpdatePromptTemplate
} from "../../wailsjs/go/main/App";
import { EventsEmit } from "../../wailsjs/runtime";
import {NButton, NInput, NTag, NText, useMessage, useDialog, NModal, NForm, NFormItem, NSpace, NPopover} from "naive-ui";
import { MdEditor, MdPreview } from 'md-editor-v3'
import 'md-editor-v3/lib/style.css'

const message = useMessage()
const dialog = useDialog()
const editorDataRef = reactive({
  darkTheme: false
})
const editorTheme = ref('light')

onBeforeMount(() => {
  GetConfig().then(result => {
    if (result.darkTheme) {
      editorDataRef.darkTheme = true
      editorTheme.value = 'dark'
    }
  })
})

onMounted(() => {
  query({
    page: 1,
    pageSize: paginationReactive.pageSize
  }).then((data) => {
    dataRef.value = data.data
    paginationReactive.page = 1
    paginationReactive.pageCount = data.totalPages
    paginationReactive.itemCount = data.total
    loadingRef.value = false
  })
})

const dataRef = ref([])
const loadingRef = ref(true)

const columnsRef = ref([
  {
    title: '模板名称',
    key: 'name',
    render(row) {
      if (row.type === '模型系统Prompt') {
        return h(NText, { type: "error" }, { default: () => row.name })
      }else{
        return h(NText, { type: "info" }, { default: () => row.name })
      }
    }
  },
  {
    title: '模板类型',
    key: 'type',
    render(row) {
      if (row.type === '模型系统Prompt') {
        return h(NTag, { type: "error" }, { default: () => row.type })
      }else{
        return h(NTag, { type: "info" }, { default: () => row.type })
      }
    }
  },
  {
    title: '创建时间',
    key: 'CreatedAt',
    render(row) {
      return row.CreatedAt.substring(0, 19).replace('T', ' ')
    }
  },
  {
    title: '更新时间',
    key: 'UpdatedAt',
    render(row) {
      return row.UpdatedAt.substring(0, 19).replace('T', ' ')
    }
  },
  {
    title: '模板内容',
    key: 'content',
    width: 200,
    render(row) {
      return h(NPopover, {
        trigger: 'hover',
        placement: 'left',
        showArrow: true,
        style: 'max-width: 800px; max-height: 400px; overflow: hidden',
        scrollable: true
      }, {
        trigger: () => h('span', {
          style: 'display: inline-block; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer;'
        }, row.content),
        default: () => h(MdPreview, {
          style:'text-align: left;',
          modelValue: row.content,
          theme: editorTheme.value
        })
      })
    }
  },
  {
    title: '操作',
    width: 180,
    render(row) {
      return [
        h(
          NButton,
          {
            size: 'small',
            type: 'primary',
            style: 'margin-right: 5px',
            onClick: () => showEditModal(row)
          },
          { default: () => '编辑' }
        ),
        h(
          NButton,
          {
            size: 'small',
            type: 'error',
            onClick: () => deletePromptTemplate(row.ID)
          },
          { default: () => '删除' }
        )
      ]
    }
  }
])

const paginationReactive = reactive({
  page: 1,
  pageCount: 1,
  pageSize: 12,
  itemCount: 0,
  prefix({ itemCount }) {
    return `${itemCount} 条记录`
  }
})

const modalDataRef = reactive({
  visible: false,
  isEdit: false,
  formData: {
    ID: 0,
    name: '',
    type: '',
    content: ''
  }
})

function query({ page, pageSize = 10, name = "", type = "", content = "" }) {
  return new Promise((resolve) => {
    GetPromptTemplateList({
      "page": page,
      "pageSize": pageSize,
      "name": name,
      "type": type,
      "content": content
    }).then((res) => {
      resolve({
        data: res.list,
        total: res.total,
        page: res.page,
        totalPages: res.totalPages
      })
    })
  })
}

function handlePageChange(currentPage) {
  if (!loadingRef.value) {
    loadingRef.value = true
    query({
      page: currentPage,
      pageSize: paginationReactive.pageSize,
      name: searchFormRef.name,
      type: searchFormRef.type,
      content: searchFormRef.content
    }).then((data) => {
      dataRef.value = data.data
      paginationReactive.page = currentPage
      paginationReactive.pageCount = data.totalPages
      paginationReactive.itemCount = data.total
      loadingRef.value = false
    })
  }
}
const promptTypeOptions = [
  {label: "模型系统Prompt", value: '模型系统Prompt'},
  {label: "模型用户Prompt", value: '模型用户Prompt'},]
const searchFormRef = reactive({
  name: "",
  type: null,
  content: ""
})

function handleSearch() {
  if (!loadingRef.value) {
    loadingRef.value = true
    query({
      page: 1,
      pageSize: paginationReactive.pageSize,
      name: searchFormRef.name,
      type: searchFormRef.type,
      content: searchFormRef.content
    }).then((data) => {
      dataRef.value = data.data
      paginationReactive.page = data.page || 1
      paginationReactive.pageCount = data.totalPages
      paginationReactive.itemCount = data.total
      loadingRef.value = false
    })
  }
}

function showAddModal() {
  modalDataRef.isEdit = false
  modalDataRef.formData = {
    ID: 0,
    name: '',
    type: '',
    content: ''
  }
  modalDataRef.visible = true
}

function showEditModal(row) {
  modalDataRef.isEdit = true
  modalDataRef.formData = {
    ID: row.ID,
    name: row.name,
    type: row.type,
    content: row.content
  }
  modalDataRef.visible = true
}

function savePromptTemplate() {
  if (!modalDataRef.formData.name || !modalDataRef.formData.type || !modalDataRef.formData.content) {
    message.warning('请填写完整信息' )
    return
  }

  const apiCall = modalDataRef.isEdit ? UpdatePromptTemplate : AddPromptTemplate
  apiCall(modalDataRef.formData).then((res) => {
    message.info( res )
    modalDataRef.visible = false
    handleSearch()
    EventsEmit('promptTemplatesChanged')
  })
}

function deletePromptTemplate(id) {

  dialog.warning({
    title: '提示',
    content: '确定要删除这个模板吗？',
    positiveText: '确定',
    negativeText: '取消',
    onPositiveClick: () => {
      DeletePromptTemplate(id).then((res) => {
        message.info( res )
        handleSearch()
        EventsEmit('promptTemplatesChanged')
      })
    }
  })
}

</script>

<template>
  <div>
    <!-- 搜索区域 -->
    <n-space vertical style="margin-bottom: 16px">
      <n-space>
        <n-input v-model:value="searchFormRef.name" placeholder="模板名称" clearable />
        <n-select style="width: 200px" v-model:value="searchFormRef.type" :options="promptTypeOptions" placeholder="请选择提示词类型" clearable/>
        <n-input v-model:value="searchFormRef.content" placeholder="内容关键词" clearable />
        <n-button type="primary" @click="handleSearch">搜索</n-button>
        <n-button type="warning" @click="showAddModal">新增模板</n-button>
      </n-space>
    </n-space>

    <!-- 数据表格 -->
    <n-data-table
      remote
      size="small"
      :columns="columnsRef"
      :data="dataRef"
      :loading="loadingRef"
      :pagination="paginationReactive"
      :row-key="(rowData) => rowData.ID"
      @update:page="handlePageChange"
      flex-height
      style="height: calc(100vh - 250px)"
    />

    <!-- 编辑/新增模态框 -->
    <n-modal v-model:show="modalDataRef.visible" preset="card" style="width: 1100px;text-align: left" :title="modalDataRef.formData.ID>0?'修改':'新增'+'Prompt模板'">
      <n-form :model="modalDataRef.formData" label-placement="left" label-width="80">
        <n-form-item label="模板名称" required>
          <n-input v-model:value="modalDataRef.formData.name" placeholder="请输入模板名称" />
        </n-form-item>
        <n-form-item label="模板类型" required>
          <n-select v-model:value="modalDataRef.formData.type" :options="promptTypeOptions" placeholder="请选择提示词类型"/>
        </n-form-item>
        <n-form-item label="模板内容" required>
          <MdEditor
            v-model="modalDataRef.formData.content"
            style="height: 400px"
            :theme="editorTheme"
            :preview="true"
            :toolbarsExclude="['github', 'htmlPreview', 'catalog', 'save']"
            placeholder="请输入模板内容"
          />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="modalDataRef.visible = false">取消</n-button>
          <n-button type="primary" @click="savePromptTemplate">保存</n-button>
        </n-space>
      </template>
    </n-modal>

  </div>
</template>

<style scoped>
:deep(.md-editor) {
  text-align: left;
}
:deep(.n-popover .md-editor-preview) {
  padding: 8px 12px;
}
:deep(.n-popover .md-editor-preview-wrapper) {
  padding: 0;
}
</style>
