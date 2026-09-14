<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { api } from '../api/client'
import { errorText } from '../api/errors'
import { detailCapabilityFor, displayColumns, resourceByName, useMeta } from '../meta/meta.store'
import type { ColumnMeta, StatusEntry } from '../types.gen'

/**
 * **通用详情页**：`/detail/:resource/:id`。
 *
 * ## 它修的是什么
 *
 * 列表里每一行都有「查看」按钮，但它一直没有落点 ——
 * `ResourceListPage.runAction('view')` 只把弹窗关掉，**什么也不发生**
 * （更早的版本是打开**编辑表单**，那是另一种错位：用户想看，系统让他改）。
 * 这个页面就是那个缺失的落点。
 *
 * ## 一条不许违反的纪律：不写字段名、不写中文
 *
 * 列名、状态中文、状态配色**全部来自服务端元数据**：
 *
 *   * 列与标签 → `ResourceMeta.columns`（`DataTable` 用的是同一份）
 *   * 状态中文 → `status_dict`（经 `meta.getStatusLabel`）
 *   * 状态配色 → `tone_key` + `status_dict[].tone`（经 `meta.getStatusTone`）
 *   * 详情数据 → `<resource>.get` 能力的 `path`（不是前端拼地址）
 *
 * ## 为什么"没有 `*.get` 就不进这一页"
 *
 * 路由守卫 `beforeEach` 已经保证了资源可解析；本页再自己检查 `*.get` 是否存在。
 * 缺它时**显式说明**并给回列表的链接 —— 而不是渲染一个空壳或 404
 * （"这个资源还没有详情接口"与"这个地址不存在"是两件事）。
 */

const props = defineProps<{ resource: string; id: string }>()

const route = useRoute()
const meta = useMeta()

const record = ref<Record<string, unknown> | null>(null)
const loading = ref(true)
const error = ref('')

const resourceName = computed(() => String(route.params.resource ?? props.resource ?? ''))
const recordId = computed(() => String(route.params.id ?? props.id ?? ''))

const resourceMeta = computed(() => resourceByName(resourceName.value))
const title = computed(() => resourceMeta.value?.title ?? resourceName.value)
/**
 * 展示列与**列表页同一规则**：有 `<key>_label` 派生列时隐藏机器码列。
 *
 * 详情页原先直接渲染 `ResourceMeta.columns` 全部列，于是同一份列声明在列表里
 * 是中文（`已核验`）、在详情里却多出一行裸机器码（`verified`）——
 * 实测用户把这两行读作"系统中英文混杂"。规则抽在 `displayColumns`，
 * 列表与详情共用，两处不可能再不一致。
 */
const columns = computed<ColumnMeta[]>(() => displayColumns(resourceMeta.value?.columns ?? []))
const listPath = computed(() => resourceMeta.value?.list_path ?? '/')
const getCapability = computed(() => detailCapabilityFor(resourceName.value))

/** `available_transitions`：从**当前**状态出发的合法目标（服务端按转移表过滤）。 */
const transitions = computed<StatusEntry[]>(() => {
  const raw = record.value?.available_transitions
  if (!Array.isArray(raw)) return []
  return raw
    .map((item) => item as { value?: unknown; label?: unknown })
    .filter((item) => typeof item.value === 'string' && typeof item.label === 'string')
    .map((item) => ({ value: item.value as string, label: item.label as string, tone: 'neutral' as const }))
})

/** 用声明的 `path` 模板 + 本页的 id 拼地址；**不猜**参数名之外的任何东西。 */
function detailUrl(): string {
  const capability = getCapability.value
  if (!capability) return ''
  return capability.path.replace(/\{([a-z_]+)\}/gi, () => encodeURIComponent(recordId.value))
}

function cellText(column: ColumnMeta): string {
  const row = record.value
  if (!row) return '—'
  const raw = row[column.key]
  if (raw === null || raw === undefined || raw === '') return '—'
  if (column.tone_key !== undefined) {
    const code = row[column.tone_key]
    if (code !== null && code !== undefined && code !== '') {
      return meta.getStatusLabel(resourceName.value, String(code))
    }
  }
  return String(raw)
}

function cellTone(column: ColumnMeta): string {
  const row = record.value
  if (!row || column.tone_key === undefined) return ''
  const code = row[column.tone_key]
  if (code === null || code === undefined || code === '') return ''
  return `tone-${meta.getStatusTone(resourceName.value, String(code))}`
}

async function load(): Promise<void> {
  if (!getCapability.value) {
    loading.value = false
    error.value = `服务端还没有登记「${title.value}」的详情接口，因此这一页暂时打不开。`
    return
  }
  loading.value = true
  error.value = ''
  try {
    const payload = await api.get<{ record: Record<string, unknown> }>(detailUrl())
    record.value = payload.record ?? null
  } catch (caught) {
    record.value = null
    error.value = errorText(caught, '加载详情失败')
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  // 元数据通常已由路由守卫加载过；没有就自己加载一次（详情页可以被直链打开）。
  if (!meta.loaded.value) {
    try {
      await meta.loadMeta()
    } catch {
      // 加载失败会体现在下面：`getCapability` 为空 ⇒ 显式说明而不是白屏。
    }
  }
  await load()
})
</script>

<template>
  <section class="resource-detail" data-testid="resource-detail">
    <p class="resource-detail__back">
      <RouterLink :to="listPath">← 返回{{ title }}列表</RouterLink>
    </p>

    <h1 class="resource-detail__title">{{ title }}详情</h1>

    <p v-if="loading" class="resource-detail__state" role="status">正在加载…</p>
    <p v-else-if="error" class="resource-detail__state resource-detail__state--error" role="alert">
      {{ error }}
    </p>

    <template v-else-if="record">
      <dl class="resource-detail__fields">
        <div v-for="column in columns" :key="column.key" class="resource-detail__field">
          <dt>{{ column.label }}</dt>
          <dd>
            <span v-if="column.tone_key !== undefined" class="status-pill" :class="cellTone(column)">
              {{ cellText(column) }}
            </span>
            <template v-else>{{ cellText(column) }}</template>
          </dd>
        </div>
      </dl>

      <div v-if="transitions.length" class="resource-detail__transitions">
        <h2>从当前状态可以流转到</h2>
        <ul>
          <li v-for="item in transitions" :key="item.value">{{ item.label }}</li>
        </ul>
      </div>
    </template>
  </section>
</template>

<style scoped>
.resource-detail {
  display: flex;
  flex-direction: column;
  gap: 1rem;
  padding: 1.25rem;
}
.resource-detail__back a {
  color: var(--tone-primary);
  text-decoration: none;
}
.resource-detail__title {
  margin: 0;
  font-size: 1.25rem;
}
.resource-detail__state {
  color: var(--tone-ink-soft);
}
.resource-detail__state--error {
  color: var(--tone-danger);
}
.resource-detail__fields {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(16rem, 1fr));
  gap: 0.75rem 1.5rem;
  margin: 0;
}
.resource-detail__field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  border-bottom: 1px solid var(--tone-line);
  padding-bottom: 0.5rem;
}
.resource-detail__field dt {
  font-size: 0.8125rem;
  color: var(--tone-ink-soft);
}
.resource-detail__field dd {
  margin: 0;
}
.resource-detail__transitions h2 {
  font-size: 0.9375rem;
  margin: 0 0 0.5rem;
}
.resource-detail__transitions ul {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin: 0;
  padding: 0;
  list-style: none;
}
.resource-detail__transitions li {
  border: 1px solid var(--tone-line);
  border-radius: 999px;
  padding: 0.125rem 0.625rem;
  font-size: 0.8125rem;
}
</style>
