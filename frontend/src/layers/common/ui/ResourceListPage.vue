<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import ActionButton from './ActionButton.vue'
import DataTable from './DataTable.vue'
import DynamicForm from './DynamicForm.vue'
import ListFilterBar from './ListFilterBar.vue'
import { useDialogFocus } from './useDialogFocus'
import { api } from '../api/client'
import { ApiError, errorText, messageWithContext, submitErrorText } from '../api/errors'
import type { Page } from '../api/models'
import { capabilityByName, detailCapabilityFor, useMeta } from '../meta/meta.store'
import { isResourceDirty, writeVersion } from '../agent/write-signal'
import type { Capability, CapabilityField } from '../types.gen'

/**
 * 通用的「资源列表页」——本项目页面层唯一的实质性组件。
 *
 * ## 它为什么存在
 *
 * INTERFACES.md §2 要求前端**渲染而非声明**：列表列来自 `ResourceMeta.columns`，
 * 表单字段来自 `Capability.fields`，行内动作来自服务端 `allowed_actions`，
 * 状态中文来自 `status_dict`。既然全部来自元数据，那么「一个塘口列表页」和
 * 「一个采购单列表页」在代码上就是**同一个东西**——区别只是一个资源名。
 *
 * 因此各业务页面都退化成一行：
 *
 * ```vue
 * <ResourceListPage resource="pond" />
 * ```
 *
 * ## 页面写得很厚 = 元数据缺东西
 *
 * 这是本项目的一条判据（负责人 明确要求）：如果为了某个资源不得不在这里加
 * 特例分支，说明服务端声明不完整，应该**报出来**而不是在前端补。
 * 本组件刻意没有任何 per-resource 分支——只有 `resource` 一个入参。
 */

const props = withDefaults(
  defineProps<{
    /** 资源名（单数），与 `ResourceMeta.name` / `Capability.resource` 同一命名空间。 */
    resource: string
    /** 每页条数（INTERFACES.md §8：上限 100）。 */
    pageSize?: number
  }>(),
  { pageSize: 20 },
)

const meta = useMeta()
const router = useRouter()

const rows = ref<Record<string, unknown>[]>([])
const total = ref(0)
const page = ref(1)
const hasNext = ref(false)
const loading = ref(true)
const pageError = ref('')
const actionError = ref('')
const busy = ref(false)

/**
 * 筛选/搜索的查询参数。由 `ListFilterBar` 按服务端声明翻译好后交过来——
 * 本组件**不解析**筛选声明，也不推导参数名（那是列表筛选的唯一转换点，
 * 见 `ListFilterBar.buildQuery()`）。
 */
const filterQuery = ref<Record<string, string>>({})

/** 弹窗状态：新建 / 编辑 / 执行某个 action 能力，都用同一套字段表单。 */
const dialog = ref<{ capability: Capability; row?: Record<string, unknown> } | null>(null)
/** 弹窗面板元素，交给 `useDialogFocus` 做「打开入焦 / 关闭还原 / Esc / Tab 圈定」。 */
const dialogPanel = ref<HTMLElement | null>(null)
const dialogOpen = computed(() => dialog.value !== null)

/**
 * 关闭弹窗的**唯一入口**：所有关闭路径（遮罩、×、提交成功、Esc）都走这里。
 *
 * 焦点还原由 `useDialogFocus` 监听 `dialogOpen` 完成，不在各处手写 —— 分散写必然漏。
 */
function closeDialog(): void {
  dialog.value = null
}

useDialogFocus({ open: dialogOpen, panel: dialogPanel, onClose: closeDialog })

const resourceMeta = computed(() => meta.resourcesByName.value.get(props.resource))

/** 该资源下的全部能力（后端按 `Capability.resource` 绑定）。 */
const capabilities = computed(() =>
  meta.capabilities.value.filter((item) => item.resource === props.resource),
)

const findKind = (kind: string): Capability | undefined =>
  capabilities.value.find((item) => item.kind === kind)

const createCapability = computed(() => findKind('create'))
const updateCapability = computed(() => findKind('update'))

const columns = computed(() => resourceMeta.value?.columns ?? [])

/** 由前端**自动带**、因此不出现在表单里的字段键。 */
const AUTO_CARRIED_FIELDS = new Set(['expected_version'])

/**
 * 表单要渲染的字段 = 能力声明的字段 − 「前端自动带」的那两类。
 *
 * * **路径参数**（`{area_id}` 之类）：值来自当前行，用户无从填、也不该填（Q7）；
 * * **`expected_version`**：乐观锁版本，`submitDialog` 从行上的 `version` 自动带
 *   （`kernel/capability.py::_EXPECTED_VERSION` 的 docstring 明说"不出现在表单"）。
 *
 * 不过滤的后果是实测过的：弹窗里出现两个**空的必填输入框**，
 * 前端校验直接拒绝提交 —— 用户看到的是"点了保存什么都没发生"，
 * 而界面上没有任何线索。**所有 update / action 能力都声明了这两个字段**，
 * 所以这一处修的是全站的点不动按钮。
 */
const dialogFields = computed<CapabilityField[]>(() => {
  const capability = dialog.value?.capability
  if (!capability) return []
  return capability.fields.filter(
    (field) => !AUTO_CARRIED_FIELDS.has(field.key) && !(capability.path_parameters ?? []).includes(field.key),
  )
})

/**
 * 路径参数取值的**唯一判据**。
 *
 * 行的主键在接口里叫 `id`，而路径模板里的占位符是 `<资源>_id`（`{area_id}`），
 * 所以这里依次尝试：`row[<资源>_id]` → 去掉 `_id`/`_no`/`_code` 后缀的列 → `row.id`。
 * 两个消费点共用它：拼 URL（`pathOf`）与**填请求体**（`submitDialog`）——
 * 后者是必需的，因为路径参数同时是能力声明的字段（处理器要它）。
 */
function pathParamValue(name: string, row?: Record<string, unknown>): unknown {
  return row?.[name] ?? row?.[name.replace(/_(id|no|code)$/, '')] ?? row?.id
}

/** 路由/能力路径里的 `{name}` 占位符替换。 */
function pathOf(capability: Capability, row?: Record<string, unknown>): string {
  return capability.path.replace(/\{([a-z_]+)\}/gi, (_match, name: string) => {
    const value = pathParamValue(name, row)
    if (value === undefined || value === null) {
      throw new Error(`能力 ${capability.name} 需要路径参数 ${name}，但该行没有这个字段`)
    }
    return encodeURIComponent(String(value))
  })
}

async function load(): Promise<void> {
  loading.value = true
  pageError.value = ''
  try {
    const listPath = resourceMeta.value?.list_path
    if (!listPath) {
      throw new Error('元数据中未登记该资源的列表接口，请检查服务端能力声明')
    }
    // 查询串一律用 URLSearchParams 拼：手写 `&key=value` 在值里含
    // `&`/`=`/中文时会被截断或注入参数（用户能自由输入搜索词）。
    const params = new URLSearchParams({
      page: String(page.value),
      page_size: String(props.pageSize),
    })
    for (const [key, value] of Object.entries(filterQuery.value)) {
      if (value !== '') params.set(key, value)
    }
    const separator = listPath.includes('?') ? '&' : '?'
    const result = await api.get<Page<Record<string, unknown>>>(`${listPath}${separator}${params.toString()}`)
    rows.value = result.items ?? []
    total.value = result.total ?? rows.value.length
    hasNext.value = Boolean(result.has_next)
  } catch (caught) {
    rows.value = []
    total.value = 0
    hasNext.value = false
    pageError.value =
      caught instanceof ApiError
        ? messageWithContext(caught, `${resourceMeta.value?.title ?? props.resource}列表加载失败`)
        : errorText(caught, '列表加载失败')
  } finally {
    loading.value = false
  }
}

async function ensureMeta(): Promise<void> {
  try {
    await meta.loadMeta()
  } catch (caught) {
    pageError.value = messageWithContext(caught, '元数据加载失败，页面无法渲染')
  }
}

onMounted(async () => {
  await ensureMeta()
  await load()
})

// 切换资源（同一组件被不同路由复用）时重置
watch(
  () => props.resource,
  async () => {
    page.value = 1
    // 筛选条件属于**上一个**资源：跨资源留着它会让新列表带着一个
    // 本资源不认的参数去查（后端按白名单取参，于是静默忽略——用户看到的
    // 是"筛选条空了但结果也不对"）。`ListFilterBar` 自己也会按新声明重建。
    filterQuery.value = {}
    await ensureMeta()
    await load()
  },
)

/**
 * 智能体写完之后自动重拉 —— 用户不必再手动刷新。
 *
 * ## 为什么订阅而不是自己判断
 *
 * 「这一轮写了没有、写的是哪个资源」只有服务端知道（`kind === 'executed'` 是网关
 * 判别联合里唯一表示"已提交事务"的取值）。本组件**不做任何猜测**，只读
 * `writeVersion` 这个版本号；发布点在 `AgentPanel`。
 *
 * ## 为什么写别的资源时不刷新
 *
 * 用户可能人在塘口页、让助手去登记成本。刷新塘口列表对那件事毫无用处（数据在成本页），
 * 而且会白费一次请求、还让"我刚看的列表跳了一下"。
 * 更准的做法是**只认当前资源**：`isResourceDirty(props.resource)`。
 *
 * 代价说清楚：被改动的是**别的**资源时，那个页面此刻若没打开就不会刷新 ——
 * 但它下次挂载时本来就会拉最新数据，所以不存在"陈旧数据"。这是刻意的取舍。
 *
 * ## 为什么绑 `writeVersion` 而不是 `dirtyResources`
 *
 * 版本号单调递增，"变没变"永远判得出来；而 `dirtyResources` 是只增的集合 ——
 * 同一资源第二次写入时它**不再变化**，只盯它会漏掉第二次刷新。
 */
watch(writeVersion, async (next, previous) => {
  if (next === previous) return
  if (!isResourceDirty(props.resource)) return
  page.value = 1
  await load()
})

function openCreate(): void {
  const capability = createCapability.value
  if (!capability) return
  actionError.value = ''
  dialog.value = { capability }
}

function openEdit(row: Record<string, unknown>): void {
  const capability = updateCapability.value
  if (!capability) return
  actionError.value = ''
  dialog.value = { capability, row }
}

/**
 * 行内动作：一律走服务端算出的 `allowed_actions`。
 *
 * 前端**不按权限码裁剪按钮**（早期版本 `returnModel.ts:69-71` 已论证过原因：
 * 容易误藏可用动作，越权由后端 403 兜底）。但要确认该动作在服务端能力清单里
 * 真实存在——否则就是"点了没反应"的静默失败。
 */
function runAction(action: string, row: Record<string, unknown>): void {
  actionError.value = ''
  if (action === 'view') {
    closeDialog()
    // 「查看」过去**什么都不做**（只把弹窗关掉）—— 那是"点了没反应"的静默失败。
    // 现在：有详情读能力就进通用详情页（`/detail/:resource/:id`）；
    // 没有就**显式说明**缺什么，而不是继续假装成功。
    // 服务端若声明了 `ui_detail_path`，就用它（例如塘口的独立详情页 `/ponds/{pond_id}`；
    // 缺了这一步，那个含"两阶段状态变更"的页面事实上没有入口，只能手打地址）。
    const uiPath = resourceMeta.value?.ui_detail_path
    if (uiPath) {
      void router.push(uiPath.replace(/\{([a-z_]+)\}/gi, () => encodeURIComponent(String(row.id ?? ''))))
      return
    }
    // 判据用 `detailCapabilityFor`（按 `resource` + `kind=read` + `path` 带占位符找），
    // 不按 `<资源>.get` 找名字 —— 命名不统一（付款单的能力叫 `payment.get`）时，
    // 按名字找会让「查看」对整类资源永久失效。
    if (detailCapabilityFor(props.resource)) {
      void router.push(`/detail/${props.resource}/${String(row.id ?? '')}`)
    } else {
      actionError.value = `服务端还没有登记「${resourceMeta.value?.title ?? props.resource}」的详情接口，暂时无法查看详情`
    }
    return
  }
  // 行内动作名与能力 `kind` 不是同一套词汇：`edit` 对应 update 能力、`create` 对应
  // create 能力。这是一张**词汇映射表**，不是 per-resource 特例——所有资源的
  // `edit` 都指向它自己的 update 能力，所有资源的 `create` 都指向它自己的 create 能力。
  // 词汇定义见 `docs/INTERFACES.md` §2 的 `RowAction`。
  if (action === 'edit') {
    openEdit(row)
    return
  }
  if (action === 'create') {
    openCreate()
    return
  }
  const capability =
    capabilityByName(`${props.resource}.${action}`) ??
    capabilityByName(`${props.resource}.${action.replace(/^archive$/, 'update')}`) ??
    capabilities.value.find((item) => item.kind === action) ??
    capabilities.value.find((item) => item.name.endsWith(`.${action}`))
  if (!capability) {
    // 显式失败优于静默失败
    actionError.value = `服务端未登记动作「${action}」，已拒绝执行`
    return
  }
  if (capability.method === 'DELETE') {
    void submitDelete(capability, row)
    return
  }
  dialog.value = { capability, row }
}

async function submitDelete(capability: Capability, row: Record<string, unknown>): Promise<void> {
  busy.value = true
  try {
    await api.request(pathOf(capability, row), { method: capability.method })
    await load()
  } catch (caught) {
    actionError.value = submitErrorText(caught, errorText(caught, '删除失败'))
  } finally {
    busy.value = false
  }
}

async function submitDialog(payload: Record<string, unknown>): Promise<void> {
  const current = dialog.value
  if (!current || busy.value) return
  const { capability, row } = current
  busy.value = true
  actionError.value = ''
  try {
    // 乐观锁：更新与 action 必须带 expected_version（INTERFACES.md §7）
    const body: Record<string, unknown> = { ...payload }
    const version = row?.version ?? row?.row_version
    if (version !== undefined) body.expected_version = version
    // 路径参数在表单里被滤掉了，但它们是能力声明的字段（处理器要它）⇒ 这里补回去。
    // 取值判据与 `pathOf` 共用 `pathParamValue()`，不复制第二套。
    for (const name of capability.path_parameters ?? []) {
      const value = pathParamValue(name, row)
      if (value !== undefined && value !== null) body[name] = value
    }
    await api.request(pathOf(capability, row), { method: capability.method, body })
    closeDialog()
    await load()
  } catch (caught) {
    actionError.value = submitErrorText(caught, errorText(caught, '提交失败'))
  } finally {
    busy.value = false
  }
}

/**
 * 筛选条件变化：**必须回到第 1 页**再查。
 *
 * 不回第 1 页的话，用户在第 5 页输入条件会得到"共 3 条、第 5 页、空表"——
 * 而后端的行为完全正确（offset 越界），只有界面上多了一个空列表。
 */
function applyFilter(query: Record<string, string>): void {
  filterQuery.value = query
  page.value = 1
  void load()
}

function go(target: number): void {
  page.value = Math.min(Math.max(1, target))
  void load()
}

const title = computed(() => resourceMeta.value?.title ?? props.resource)

defineExpose({
  rows,
  columns,
  load,
  runAction,
  dialog,
  pageError,
  actionError,
  pathOf,
  filterQuery,
  applyFilter,
})
</script>

<template>
  <main class="resource-page" :data-resource="resource">
    <header class="resource-page__header">
      <div>
        <h1 data-testid="page-title">{{ title }}</h1>
        <p class="resource-page__hint">列表列、表单字段、状态与动作全部来自服务端元数据</p>
      </div>
      <ActionButton v-if="createCapability" variant="primary" data-testid="page-create" @click="openCreate">
        {{ createCapability.title }}
      </ActionButton>
    </header>

    <p v-if="pageError" class="resource-page__error" role="alert" data-testid="page-error">
      {{ pageError }}
      <ActionButton compact data-testid="page-reload" @click="load">重新加载</ActionButton>
    </p>

    <p v-if="actionError" class="resource-page__error" role="alert" data-testid="action-error">
      {{ actionError }}
    </p>

    <!--
      筛选条插在表格之前：它是"找得到"的主入口，放在表格下面会让用户在长列表里
      永远看不到它。筛选声明为空且资源不支持搜索时它自己不渲染（`v-if`），
      所以"没有筛选能力的资源"的页面与当前迭代之前完全一致。
    -->
    <ListFilterBar
      :resource="resource"
      :filters="resourceMeta?.filters ?? []"
      :search="resourceMeta?.search === true"
      @change="applyFilter"
    />

    <DataTable
      :resource="resource"
      :columns="columns"
      :rows="rows"
      row-key="id"
      :loading="loading"
      :empty-text="`当前授权范围内暂无${title}`"
      @action="runAction"
    />

    <div class="resource-page__pager" data-testid="page-pager">
      <span data-testid="page-total">共 {{ total }} 条</span>
      <span class="spacer" />
      <ActionButton compact :disabled="page <= 1 || loading" data-testid="page-prev" @click="go(page - 1)">
        上一页
      </ActionButton>
      <span data-testid="page-current">第 {{ page }} 页</span>
      <ActionButton compact :disabled="!hasNext || loading" data-testid="page-next" @click="go(page + 1)">
        下一页
      </ActionButton>
    </div>

    <Teleport to="body">
      <div
        v-if="dialog"
        class="resource-page__overlay"
        role="dialog"
        aria-modal="true"
        :aria-label="dialog.capability.title"
        data-testid="page-dialog"
        @click.self="closeDialog"
      >
        <div ref="dialogPanel" class="resource-page__panel" tabindex="-1">
          <header class="resource-page__panel-head">
            <h2>{{ dialog.capability.title }}</h2>
            <button
              type="button"
              class="resource-page__close"
              aria-label="关闭"
              data-testid="dialog-close"
              @click="closeDialog"
            >
              ×
            </button>
          </header>

          <p v-if="dialog.capability.risk === 'high'" class="resource-page__risk" data-testid="dialog-risk">
            这是高风险操作，确认后会写入系统并留下审计记录
          </p>

          <DynamicForm
            :fields="dialogFields"
            :initial="dialog.row"
            :busy="busy"
            :submit-label="dialog.capability.title"
            @submit="submitDialog"
          />
        </div>
      </div>
    </Teleport>
  </main>
</template>

<style scoped>
.resource-page {
  display: grid;
  /* `minmax(0, 1fr)`：单列轨道允许收缩到内容宽度以下（默认 `1fr` 的 min 是 auto）。
     与 `DataTable` 的 `min-width: 0` 配对 —— 只加一处仍有整页横向溢出的余量。 */
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
  width: 100%;
  max-width: var(--tone-content-max);
  margin: 0 auto;
  padding: 20px 24px 32px;
}
.resource-page__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.resource-page__header h1 {
  margin: 0 0 4px;
  font-size: 22px;
}
.resource-page__hint {
  margin: 0;
  color: var(--tone-muted);
  font-size: 13px;
}
.resource-page__error {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 0;
  padding: 12px 14px;
  border: 1px solid var(--tone-danger-line);
  border-radius: var(--tone-radius);
  color: var(--tone-danger);
  background: var(--tone-danger-soft);
  font-size: 13px;
}
.resource-page__pager {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: var(--tone-muted);
}
.resource-page__pager .spacer {
  flex: 1;
}
.resource-page__overlay {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: grid;
  place-items: center;
  padding: 24px;
  background: var(--tone-overlay);
}
.resource-page__panel {
  width: min(680px, 100%);
  max-height: 90vh;
  overflow-y: auto;
  padding: 20px 24px;
  border-radius: var(--tone-radius);
  background: var(--tone-surface);
  box-shadow: var(--tone-shadow-card);
}
.resource-page__panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.resource-page__panel-head h2 {
  margin: 0;
  font-size: 18px;
}
.resource-page__close {
  padding: 2px 8px;
  border: 1px solid var(--tone-line);
  border-radius: var(--tone-radius-sm);
  font-size: 18px;
  line-height: 1.2;
  background: transparent;
  cursor: pointer;
}
.resource-page__risk {
  margin: 0 0 12px;
  padding: 10px 12px;
  border: 1px solid var(--tone-warning-line);
  border-radius: var(--tone-radius);
  color: var(--tone-warning);
  background: var(--tone-warning-soft);
  font-size: 13px;
}
</style>
