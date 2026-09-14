<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api/client'
import { errorText } from '../api/errors'
import { useMeta } from '../meta/meta.store'
import type { Page } from '../api/models'
import type { FilterMeta } from '../types.gen'
import ActionButton from './ActionButton.vue'

/**
 * 列表页的筛选条。**完全由服务端元数据驱动**。
 *
 * ## 它为什么存在
 *
 * 管理后台的第一需求是"找得到"。后端在 `Resource.filters` / `Resource.search`
 * 上**声明**了这个列表能按什么找（`kernel/workflow.py::FilterSpec`），本组件只负责
 * 把声明翻译成控件，**不做任何 per-resource 分支**——和 `ResourceListPage`、
 * `DynamicForm` 同一条纪律：页面写得很厚 = 元数据缺东西。
 *
 * ## 三条不许违反的规则
 *
 * 1. **查询参数名不许推导**。单参数筛选的参数名就是 `key`；`date_range` 的两个
 *    参数名在 `params.from` / `params.to` 里由**声明**给出。前端自己拼
 *    （管 `x` 叫 `x_from`）不会报错，症状是"填了条件却筛不掉任何行"——这类错误只在
 *    用户抱怨"筛选没用"时暴露。
 * 2. **`status` 的候选值不许在这里写中文**。它取自该资源的 `status_dict`
 *    （唯一来源是状态机）。内核在 `FilterKind.STATUS` 上明确"候选值不下发"，
 *    就是为了不出现第二份状态中文。
 * 3. **`ref` 的候选地址不拼接**。`ref.list_path` 已由内核按引用资源的声明解析好，
 *    直接用；取不到就**显式报错**，而不是渲染一个"看起来没有可选项"的空下拉。
 *
 * ## 为什么值一律当字符串/布尔存
 *
 * 控件值到查询参数的转换只发生在 `buildQuery()` 一处。中间不留"数字/字符串"两种
 * 形态，是因为 HTTP 查询参数本来就是字符串——提前转换只会多一处可漂移的表示。
 */

const props = defineProps<{
  /** 该资源的筛选声明（`ResourceMeta.filters`）。 */
  filters: FilterMeta[]
  /** 资源名，用于取 `status_dict`（`status` 筛选的候选值）。 */
  resource: string
  /** 该资源是否支持 `?keyword=` 搜索（`ResourceMeta.search`）。 */
  search?: boolean
}>()

const emit = defineEmits<{ change: [query: Record<string, string>] }>()

const meta = useMeta()

/**
 * 控件值。按 `key` 存**原始控件值**：
 *   * 标量筛选（status/enum/ref/string）→ 字符串；
 *   * `date_range` → `{ from, to }`（两个日期各自可能为空）；
 *   * `boolean` → 布尔。
 */
/**
 * 每个筛选控件的**原始取值**。索引签名允许 `string | boolean | {from,to}` 三种形态——
 * 这样 `v-model="values[key]"` 在模板里不需要类型断言（模板里的 `as` 不是合法表达式，
 * `vue-tsc` 会直接报 TS1005）。类型收窄交给 `buildQuery()` 一处处理。
 */
const values = ref<Record<string, string | boolean | { from: string; to: string }>>({})
const keyword = ref('')

/** `type="ref"` 的候选项：`筛选 key -> 选项[]`。 */
const refOptions = ref<Record<string, { value: string; label: string }[]>>({})
const refLoading = ref<Record<string, boolean>>({})
const refError = ref<Record<string, string>>({})

/** `status` 筛选的候选值来自该资源的 `status_dict`（不下发 choices）。 */
const statusChoices = computed(() => {
  const resourceMeta = meta.resourcesByName.value.get(props.resource)
  // `StatusEntry` 的字段是 `value`（不是 `code`）；`selectable === false`
  // 的状态（终态 / 预留）不进下拉 —— 判据由服务端给，前端不重算。
  return (resourceMeta?.status_dict ?? [])
    .filter((entry) => entry.selectable !== false)
    .map((entry) => ({ value: entry.value, label: entry.label }))
})

function emptyValue(filter: FilterMeta): string | boolean | { from: string; to: string } {
  if (filter.type === 'boolean') return false
  if (filter.type === 'date_range') return { from: '', to: '' }
  return ''
}

/** 每次 `filters` 变化（如切换资源）重建控件值，旧的筛选值不许跨资源残留。 */
function reset(): void {
  const next: Record<string, string | boolean | { from: string; to: string }> = {}
  for (const filter of props.filters) next[filter.key] = emptyValue(filter)
  values.value = next
  keyword.value = ''
  refOptions.value = {}
  refError.value = {}
}

/**
 * `date_range` 控件的取值。
 *
 * 模板里 `values[filter.key]` 是 `string | boolean | {from,to}` 的联合，直接
 * `values[filter.key].from` 过不了 `vue-tsc`。收窄**只在这里做一次**。
 * 返回的必须是 `values` 里的**同一个对象**（`v-model` 要写回响应式源）；
 * 万一形态不对（声明变了之类），就当场写回一个空区间再返回它，
 * **不返回临时对象**——那会让用户的输入静默丢掉。
 */
function rangeOf(key: string): { from: string; to: string } {
  const value = values.value[key]
  if (value !== null && typeof value === 'object') return value
  const fresh = { from: '', to: '' }
  values.value[key] = fresh
  return fresh
}

function isFilled(value: unknown): boolean {
  if (typeof value === 'boolean') return value
  if (value && typeof value === 'object') {
    const range = value as { from?: string; to?: string }
    return Boolean(range.from) || Boolean(range.to)
  }
  return value !== '' && value !== undefined && value !== null
}

const active = computed(() => Object.values(values.value).some(isFilled) || keyword.value.trim() !== '')

function choicesOf(filter: FilterMeta): { value: string; label: string }[] {
  if (filter.type === 'status') return statusChoices.value
  if (filter.type === 'enum') {
    return (filter.choices ?? []).map((choice) => ({ value: choice.value, label: choice.label }))
  }
  return refOptions.value[filter.key] ?? []
}

/** 下拉空选项文案：四态分开说（加载中 / 加载失败 / 无候选 / 请选择）。 */
function placeholderOf(filter: FilterMeta): string {
  if (refLoading.value[filter.key]) return '加载中…'
  if (filter.type === 'ref' && !refError.value[filter.key]) {
    if ((refOptions.value[filter.key] ?? []).length === 0) return '暂无可选项'
  }
  if (filter.type === 'status' && statusChoices.value.length === 0) return '无可选状态'
  return '全部'
}

/**
 * 拉一个 `ref` 筛选的候选。
 *
 * 地址取自**声明**（`ref.list_path` 已由内核解析），不拼接。取不到就显式报错——
 * 静默的空下拉会让用户以为"系统里确实没有可选项"。
 */
async function loadRefOptions(filter: FilterMeta): Promise<void> {
  const listPath = filter.ref?.list_path
  if (!listPath) {
    refError.value[filter.key] = `筛选「${filter.label}」缺少候选地址声明`
    return
  }
  refLoading.value[filter.key] = true
  refError.value[filter.key] = ''
  try {
    const separator = listPath.includes('?') ? '&' : '?'
    const page = await api.get<Page<Record<string, unknown>>>(`${listPath}${separator}page=1&page_size=100`)
    refOptions.value[filter.key] = (page.items ?? []).map((item) => ({
      value: String(item.id ?? ''),
      label: String(item[filter.ref?.label_key ?? 'name'] ?? item.id ?? ''),
    }))
  } catch (caught) {
    refOptions.value[filter.key] = []
    refError.value[filter.key] = errorText(caught, `无法加载「${filter.label}」的可选项`)
  } finally {
    refLoading.value[filter.key] = false
  }
}

/**
 * 把控件值翻译成真正的查询参数。**参数名转换只在这里发生。**
 *
 * * `date_range` → 按 `params` 展开成两个参数（只展开声明过的那个方向：
 *   单边区间是合法的）；
 * * `boolean` 为 false **不发参数**（后端的判据是"参数在不在"，不是"值是不是 false"，
 *   发 `only_overdue=false` 会多一条无意义的 WHERE 条件）；
 * * 空字符串一律不发。
 */
function buildQuery(): Record<string, string> {
  const query: Record<string, string> = {}
  const trimmed = keyword.value.trim()
  if (props.search && trimmed) query.keyword = trimmed

  for (const filter of props.filters) {
    const value = values.value[filter.key]
    if (filter.type === 'date_range') {
      const range = (value ?? { from: '', to: '' }) as { from?: string; to?: string }
      const params = filter.params ?? {}
      if (params.from && range.from) query[params.from] = range.from
      if (params.to && range.to) query[params.to] = range.to
      continue
    }
    if (filter.type === 'boolean') {
      if (value === true) query[filter.key] = 'true'
      continue
    }
    const text = String(value ?? '').trim()
    if (text) query[filter.key] = text
  }
  return query
}

function emitChange(): void {
  emit('change', buildQuery())
}

function clearAll(): void {
  reset()
  emitChange()
}

onMounted(() => {
  reset()
  for (const filter of props.filters) {
    if (filter.type === 'ref') void loadRefOptions(filter)
  }
})

// 切换资源时重建：筛选值、搜索词、ref 候选全部重置
watch(
  () => props.filters,
  () => {
    reset()
    for (const filter of props.filters) {
      if (filter.type === 'ref') void loadRefOptions(filter)
    }
  },
)

// `search` 能力后到（元数据晚于首屏）时也要能重建控件
watch(
  () => props.search,
  () => reset(),
)

defineExpose({ values, keyword, buildQuery, clearAll, reset, loadRefOptions })
</script>

<template>
  <div v-if="filters.length > 0 || search" class="filter-bar filter-bar__panel" data-testid="filter-bar">
    <div class="filter-bar__row">
      <label v-if="search" class="filter-bar__item filter-bar__item--search">
        <span class="filter-bar__label">搜索</span>
        <input
          v-model="keyword"
          class="filter-bar__control"
          type="search"
          placeholder="按编号 / 名称搜索"
          data-testid="filter-keyword"
          @change="emitChange"
        />
      </label>

      <label
        v-for="filter in filters"
        :key="filter.key"
        class="filter-bar__item"
        :data-filter-key="filter.key"
      >
        <span class="filter-bar__label">{{ filter.label }}</span>

        <!-- boolean：勾上就多一个 WHERE 条件 -->
        <input
          v-if="filter.type === 'boolean'"
          v-model="values[filter.key] as boolean"
          class="filter-bar__checkbox"
          type="checkbox"
          :data-testid="`filter-${filter.key}`"
          @change="emitChange"
        />

        <!-- date_range：**一个**控件、两个日期输入（参数名来自 params） -->
        <span
          v-else-if="filter.type === 'date_range'"
          class="filter-bar__range"
          :data-testid="`filter-${filter.key}`"
        >
          <input
            v-model="rangeOf(filter.key).from"
            class="filter-bar__control"
            type="date"
            :aria-label="`${filter.label}起`"
            @change="emitChange"
          />
          <span class="filter-bar__dash">—</span>
          <input
            v-model="rangeOf(filter.key).to"
            class="filter-bar__control"
            type="date"
            :aria-label="`${filter.label}止`"
            @change="emitChange"
          />
        </span>

        <!-- string：文本输入 -->
        <input
          v-else-if="filter.type === 'string'"
          v-model="values[filter.key] as string"
          class="filter-bar__control"
          type="text"
          :data-testid="`filter-${filter.key}`"
          @change="emitChange"
        />

        <!-- status / enum / ref：下拉，候选值各有各的唯一来源 -->
        <select
          v-else
          v-model="values[filter.key] as string"
          class="filter-bar__control"
          :data-testid="`filter-${filter.key}`"
          @change="emitChange"
        >
          <option :value="''">{{ placeholderOf(filter) }}</option>
          <option
            v-for="choice in choicesOf(filter)"
            :key="String(choice.value)"
            :value="String(choice.value)"
          >
            {{ choice.label }}
          </option>
        </select>
      </label>

      <ActionButton v-if="active" compact data-testid="filter-clear" @click="clearAll">
        清除筛选
      </ActionButton>
    </div>

    <p
      v-for="filter in filters"
      v-show="refError[filter.key]"
      :key="`err-${filter.key}`"
      class="filter-bar__error"
      role="alert"
      :data-testid="`filter-error-${filter.key}`"
    >
      {{ refError[filter.key] }}
    </p>
  </div>
</template>

<style scoped>
.filter-bar {
  display: grid;
  gap: 8px;
  padding: 12px 14px;
  border: 1px solid var(--tone-line);
  border-radius: var(--tone-radius);
  background: var(--tone-surface);
}
.filter-bar__row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 10px 14px;
}
.filter-bar__item {
  display: grid;
  gap: 4px;
  font-size: 13px;
}
.filter-bar__item--search {
  min-width: 220px;
}
.filter-bar__label {
  color: var(--tone-muted);
  font-size: 12px;
}
.filter-bar__control {
  min-width: 120px;
  padding: 6px 8px;
  border: 1px solid var(--tone-line);
  border-radius: var(--tone-radius-sm);
  font-size: 13px;
  color: var(--tone-text);
  background: var(--tone-surface);
}
.filter-bar__checkbox {
  width: 16px;
  height: 16px;
  margin: 6px 0;
}
.filter-bar__range {
  display: flex;
  align-items: center;
  gap: 6px;
}
.filter-bar__dash {
  color: var(--tone-muted);
}
.filter-bar__error {
  margin: 0;
  color: var(--tone-danger);
  font-size: 12px;
}
</style>
