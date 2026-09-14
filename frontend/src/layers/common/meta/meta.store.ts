import { computed, ref } from 'vue'
import { api } from '../api/client'
import { errorText } from '../api/errors'
import type { ActionsMeta, Capability, ColumnMeta, ResourceMeta, StatusEntry, Tone } from '../types.gen'

/**
 * 元数据仓库——前端字段契约的**唯一来源**。
 *
 * INTERFACES.md §2 明确：「早期版本把同一份字段清单在前端手写了第二遍并已漂移，
 * 新系统必须让前端**渲染**而不是**声明**。」约束 1 进一步要求
 * 「前端不得出现任何硬编码字段名」。
 *
 * 因此本文件是 DynamicForm / DataTable / 菜单 / 按钮的全部输入。
 * 页面里出现 `'pond_id'` 这种字面量就是违规。
 */

const capabilities = ref<Capability[]>([])
const resources = ref<ResourceMeta[]>([])
/**
 * 动作词→中文标签（服务端 `/meta/capabilities` 的 `actions.row_action_labels`）。
 *
 * **前端不持有任何动作文案表**：与 `status_dict` 同一条纪律——
 * 标签只有一处（服务端），前端只查表。它原先是硬编码在
 * `RecordActions.vue::ACTION_LABELS` 里的，而 `DataTable.vue` 干脆直接渲染 token ——
 * 同一个系统里两套口径。
 */
const actionLabels = ref<Record<string, string>>({})
const loaded = ref(false)
const loading = ref(false)
let error = ref('')
let pending: Promise<void> | null = null

/** 资源名 → 元数据，供 `ref` 类型字段与 DataTable 查表。 */
const resourcesByName = computed<Map<string, ResourceMeta>>(
  () => new Map(resources.value.map((item) => [item.name, item])),
)

/** 能力名 → 声明，供确认卡片与按钮反查。 */
const capabilitiesByName = computed<Map<string, Capability>>(
  () => new Map(capabilities.value.map((item) => [item.name, item])),
)

/**
 * 资源的**前端路径**（从服务端 `ResourceMeta.list_path` 推导）。
 *
 * 服务端给的是 API 地址（`/api/v1/ponds`），而当前页面地址去掉那个前缀就是它
 * 在前端的对应路径（`/ponds`）。** 这是唯一的推导规则**：
 *
 *   * 不在前端手写一张菜单表（那是"两处描述同一件事"）；
 *   * 也不另取一套前端命名（原先手写的路由用 了
 *     `/warehouse/materials`、`/purchase/orders` 这种自定义拼法，与服务端的
 *     `/api/v1/materials`、`/api/v1/purchase-orders` 对不上 —— 两套命名必然漂移）。
 *
 * 返回 `''` 表示该资源**不可导航**（路径不在 `/api/v1` 下，无法推导）。
 */
export function resourcePathOf(listPath: string): string {
  return listPath.startsWith('/api/v1') ? listPath.slice('/api/v1'.length) : ''
}

/**
 * 把 URL 反查成资源名。**"路径 → 资源名"的唯一映射处。**
 *
 * ## 为什么它住在这里，而不是 `router.ts`
 *
 * 它是**纯元数据逻辑**（只读 `resources` + `capabilities`），而要用它的有两处：
 *   * `router.ts` 的 `beforeEnter` —— 判定"这个 URL 对应哪个资源"；
 *   * `ResourcePage.vue` 的"重试"按钮 —— 元数据补上之后**就地重解析**，
 *     不必绕一圈刷新路由。
 * 放两份就是两处会漂移的规则，所以只留这一份。
 *
 * 判据与导航 `navItems()` 完全一致（用 `ResourceMeta.list_path` 推导、
 * 且该资源必须有一条**可见的读能力**），所以"导航里有"与"能进得去"不可能不一致。
 */
export function resolveResourceByPath(path: string): string {
  const normalized = path.replace(/\/+$/, '') || '/'
  for (const resource of resources.value) {
    const read = capabilities.value.find((item) => item.resource === resource.name && item.kind === 'read')
    if (!read) continue
    if (resourcePathOf(resource.list_path) === normalized) return resource.name
  }
  return ''
}

/**
 * 导航条目。**完全由服务端元数据生成，前端不手写菜单。**
 *
 * ## 判据：一个资源为什么能出现在导航里
 *
 * 两个条件，都来自服务端：
 *
 *   1. 它有一条 **读能力**（`kind === 'read'`）——没有读能力就没有列表接口，
 *      点进去只能看到报错；实测 23 个资源里有 4 个是这种
 *      （`access_role` / `accounting_period` / `sales_receipt` / `warehouse_document`），
 *      它们**不得**出现在导航里。
 *   2. 它的 `list_path` 能推出前端路径（见 `resourcePathOf`）。
 *
 * ## 权限过滤在哪里—— **已经过了，而且只过一次**
 *
 * `capabilities` 里只有**当前账号有权调用**的能力：
 * 服务端 `/meta/capabilities` 用 `registry.visible_to(actor.permissions)` 过滤过。
 * 所以"该资源有一条可见的读能力"本身**就是**"这个账号能看这个列表"。
 *
 * **前端不另外定义权限码**（那就是第二处描述）。
 *
 * 顺带一个真实的效果：这条规则使导航与**路由可达性不可能不一致**
 * —— 路由解析用的是同一份（已过滤的）资源列表。
 * 某资源在导航里看不到，它的路径也就解析不出资源名，
 * 手输 URL 也进不去 —— 这是有意的（越去控由服务端判定）。
 */
export function navItems(): {
  name: string
  title: string
  path: string
  module: string
  moduleTitle: string
}[] {
  const items: {
    name: string
    title: string
    path: string
    module: string
    moduleTitle: string
  }[] = []
  for (const resource of resources.value) {
    const read = capabilities.value.find((item) => item.resource === resource.name && item.kind === 'read')
    if (!read) continue
    const path = resourcePathOf(resource.list_path)
    if (!path) continue
    items.push({
      name: resource.name,
      title: resource.title,
      path,
      module: resource.module ?? '',
      // 组标题用服务端给的**中文**；回退到域码仅为不空（宁可看到码，也不让整组消失）。
      moduleTitle: resource.module_title ?? resource.module ?? '',
    })
  }
  return items
}

/**
 * 状态字典查表：`(resource, statusValue) -> StatusEntry`。
 *
 * 早期版本实测缺陷（.local/recon-frontend.md 问题 P-1）：同一 `verified` 在
 * 成本页显示「待确认」、在主数据页显示「已核验」、在智能体词表里显示「已提交」，
 * 因为全仓有 14 处独立的状态中文字面量，而唯一公共函数
 * `common/formatters/status.ts:4` 的 `getStatusLabel` **从未被调用**。
 *
 * 新实现只有这一个入口，且它读的是服务端下发的 `status_dict`。
 */
export function statusEntryOf(resourceName: string, value: string): StatusEntry | undefined {
  return resourcesByName.value.get(resourceName)?.status_dict.find((item) => item.value === value)
}

/** 状态码 → 中文标签。查不到时返回原值（**不静默改写**）。 */
export function getStatusLabel(resourceName: string, value: string): string {
  return statusEntryOf(resourceName, value)?.label ?? value
}

/**
 * 状态字典里**用户可选**的那些项（筛选下拉、目标状态下拉）。
 *
 * ## 为什么必须有这个函数（而不是让每个页面自己过滤）
 *
 * `status_dict` 是**全部**状态——渲染历史行的标签要用它，所以它不能只给可选集
 * （否则库里已存在的 `archived` 行会退化成裸状态码，而那正是早期版本
 * "14 处状态中文字面量"要根除的症状）。
 *
 * 但"能渲染"不等于"能选"：`terminal`（进入后不能再转移）与 `reserved`
 * （刻意保留、无任何能力能进入）两类状态出现在下拉里，用户选中后
 * **要么被服务端拒，要么永远筛出空集**。两者都是"看起来能用"。
 *
 * 判据由服务端 `StatusEntry.selectable` 给出（`State.selectable` 是它的唯一实现），
 * 这里只做筛选——**不重算** `!reserved && !terminal`，否则这条规则就有了第二处实现。
 */
export function selectableStatuses(resourceName: string): StatusEntry[] {
  return (resourcesByName.value.get(resourceName)?.status_dict ?? []).filter(
    // 缺字段时视为可选：老服务端（没下发 selectable）下不能把整张下拉清空，
    // 那会把一次兼容性问题伪装成"这个资源没有可选状态"。
    (entry) => entry.selectable !== false,
  )
}

/**
 * 状态码 → 配色。
 *
 * 与早期版本的关键差别：早期实现是 `column.tones?.[String(row[column.key])]`，
 * 即用**行里的显示值**当键；本实现要求调用方显式给出**状态码**，
 * 并结合 `ColumnMeta.tone_key` 指出状态码所在列。这样中文标签变化不会影响取色。
 */
export function getStatusTone(resourceName: string, value: string): Tone {
  return statusEntryOf(resourceName, value)?.tone ?? 'neutral'
}

/**
 * 要渲染给用户的列：**有中文派生列时隐藏机器码列**。
 *
 * `Resource.columns` 里的 `status` / `unit` / `category` 是**机器码列**
 * （`verified` / `kg` / `feed`），它们必须留在声明里 —— `FilterSpec.key ∈ columns`
 * 强制要求筛选控件的取值列在列清单中 —— 但**不该直接渲染给用户**。
 * 给人看的是 `<key>_label` 派生列（`DISPLAY_CONTRACT.md`）。
 *
 * ## 为什么抽成一处
 *
 * 这条规则原先只写在 `DataTable.vue` 里，于是**通用详情页**漏了它：同一份列声明
 * 在列表里显示中文，在详情里却多渲染一行裸机器码 —— 实测物料详情出现两行
 * 「记录状态」（`已核验` 与 `verified`），用户读作"系统中英文混杂"。
 * 列表与详情是同一个规则的**两个消费点**，规则只留这一份，两处就不可能漂移。
 */
export function displayColumns(columns: ColumnMeta[]): ColumnMeta[] {
  const keys = new Set(columns.map((column) => column.key))
  return columns.filter((column) => !keys.has(`${column.key}_label`))
}

/**
 * 某资源的**单条详情读能力**（`*.get`）。
 *
 * ## 为什么不能只按名字找 `<资源>.get`
 *
 * 能力命名在本仓不统一（与后端 `kernel/workflow.py::capability_for_action` 同一事实）：
 * 付款单的资源名是 `purchase_payment`，而它的详情能力叫 **`payment.get`**。
 * 只按 `<资源>.get` 查会得到 `undefined`，于是列表行的「查看」**永远打不开**
 * （实测：报"服务端还没有登记「付款单」的详情接口"）。
 *
 * 判据改成**结构性的**：`kind === 'read'` 且 `path` 里带 `{...}` 占位符的那条
 * —— 列表读能力（`/api/v1/payments`）没有占位符，所以不会被选中。
 * 这与 `PondDetailPage` 早已采用的判据逐字一致（那里的注释记着同一个坑：
 * 只按 `kind === 'read'` 找会命中排在前面的 `*.list`）。
 */
export function detailCapabilityFor(resource: string): Capability | undefined {
  return capabilities.value.find(
    (item) => item.resource === resource && item.kind === 'read' && item.path.includes('{'),
  )
}

/**
 * 解析某一列在一个行对象上的配色 tone。
 *
 * - 列声明了 `tone_key`：状态码取自 `row[tone_key]`，显示值取自 `row[key]`；
 * - 未声明 `tone_key`：该列自身就是状态码。
 *
 * 未知状态一律返回 `'neutral'`——这是**显式约定**而不是静默降级：
 * `tests/data-table.spec.ts` 专门断言未知状态必须得到 `neutral` 且不得为空串。
 */
export function toneForCell(
  resourceName: string,
  columnKey: string,
  row: Record<string, unknown>,
  toneKey?: string,
): Tone {
  const raw = toneKey ? row[toneKey] : row[columnKey]
  if (raw === null || raw === undefined || raw === '') return 'neutral'
  return getStatusTone(resourceName, String(raw))
}

/**
 * 拉取并缓存元数据。并发调用共享同一个 Promise。
 *
 * 缓存判据只用 `loaded`，**不加 `capabilities.length > 0`**。
 * 实测教训：第一版写成 `loaded.value && capabilities.value.length > 0`，
 * 于是「已加载成功、但该账号确实没有任何能力」的合法情况会被判为未加载，
 * 每次都重新请求（`tests/support-and-coverage.spec.ts` 的
 * 「加载成功后二次调用不再请求」因此失败）。空清单是**有效结果**，不是失败。
 *
 * 需要强制刷新时用 `reload()`。
 */
export function loadMeta(): Promise<void> {
  if (loaded.value) return Promise.resolve()
  return fetchMeta()
}

/** 强制重新拉取（例如切换账号后）。 */
export function reloadMeta(): Promise<void> {
  loaded.value = false
  return fetchMeta()
}

function fetchMeta(): Promise<void> {
  if (pending) return pending

  loading.value = true
  error.value = ''
  pending = (async () => {
    try {
      const data = await api.get<{
        capabilities: Capability[]
        resources: ResourceMeta[]
        actions?: ActionsMeta
      }>('/api/v1/meta/capabilities')
      capabilities.value = data.capabilities ?? []
      resources.value = data.resources ?? []
      actionLabels.value = data.actions?.row_action_labels ?? {}
      loaded.value = true
    } catch (caught) {
      capabilities.value = []
      resources.value = []
      loaded.value = false
      error.value = errorText(caught, '元数据加载失败')
      throw caught
    } finally {
      loading.value = false
      pending = null
    }
  })()

  return pending
}

/** 仅测试使用：重置缓存。 */
export function __resetMetaForTest(): void {
  capabilities.value = []
  resources.value = []
  actionLabels.value = {}
  loaded.value = false
  loading.value = false
  error = ref('')
  pending = null
}

/**
 * 动作词 → 中文标签。
 *
 * 回退到原词是**有意**的：元数据还没到、或服务端加了动作却漏了标签时，
 * 显示原词是**可诊断**的（一眼看出哪个词缺标签），而不是静默显示一个猜的中文。
 */
export function rowActionLabel(action: string): string {
  return actionLabels.value[action] ?? action
}

export function useMeta() {
  return {
    capabilities,
    resources,
    loaded,
    loading,
    error: computed(() => error.value),
    resourcesByName,
    capabilitiesByName,
    statusEntryOf,
    selectableStatuses,
    rowActionLabel,
    navItems,
    resourcePathOf,
    getStatusLabel,
    getStatusTone,
    toneForCell,
    detailCapabilityFor,
    loadMeta,
  }
}

/** 非组件环境（如路由守卫）取资源列表。 */
export function resourceMetas(): ResourceMeta[] {
  return resources.value
}

/** 非组件环境按名取资源元数据。 */
export function resourceByName(name: string): ResourceMeta | undefined {
  return resourcesByName.value.get(name)
}

/** 非组件环境按名取能力声明。 */
export function capabilityByName(name: string): Capability | undefined {
  return capabilitiesByName.value.get(name)
}
