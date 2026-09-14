<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import ActionButton from '../../common/ui/ActionButton.vue'
import DataTable from '../../common/ui/DataTable.vue'
import { api } from '../../common/api/client'
import { ApiError, errorText, messageWithContext, submitErrorText } from '../../common/api/errors'
import type { Page } from '../../common/api/models'
import { useMeta } from '../../common/meta/meta.store'
import type { Capability, CapabilityField } from '../../common/types.gen'

/**
 * 工作台：待办与通知的入口。
 *
 * ## 元数据缺口（已上报 负责人，不在前端补）
 *
 * `CAPABILITY_REGISTRY.md` 的 69 条能力里**没有 work_item / notification 的读能力**，
 * 也没有对应的 `ResourceMeta`。但同一份文档的 §3.1（`:1025`）写明
 * 「`pond_status_change.request` → 写 `work_items` 待办（核验待办）」——
 * 也就是说待办是**真实存在的业务实体**，只是没被声明成能力。
 * 内核 `workflow.py:141` 也引用了 `work_items`。
 *
 * 因此本页按「能力存在就渲染、不存在就明确说明」实现：**不硬编码端点、不硬编码列**。
 * 后端补上 `work_item.list` 后，这一页无需改动即可工作。
 */

const meta = useMeta()
const rows = ref<Record<string, unknown>[]>([])
const loading = ref(true)
const pageError = ref('')

/** 待办与通知的能力名（后端补齐后即生效）。 */
const CANDIDATE_NAMES = ['work_item.list', 'work_items.list', 'notification.list', 'workbench.list']

const listCapability = computed<Capability | undefined>(() =>
  meta.capabilities.value.find((item) => CANDIDATE_NAMES.includes(item.name)),
)

const resourceMeta = computed(() =>
  listCapability.value ? meta.resourcesByName.value.get(listCapability.value.resource) : undefined,
)

const columns = computed(() => resourceMeta.value?.columns ?? [])

/** DataTable 需要资源名来查 status_dict——取自能力声明，不硬编码。 */
const resourceName = computed(() => listCapability.value?.resource ?? 'work_item')
const missing = computed(() => !loading.value && !listCapability.value)

async function load(): Promise<void> {
  loading.value = true
  pageError.value = ''
  try {
    await meta.loadMeta()
    const capability = listCapability.value
    if (!capability) return
    const listPath = resourceMeta.value?.list_path
    if (!listPath) return
    const separator = listPath.includes('?') ? '&' : '?'
    const result = await api.get<Page<Record<string, unknown>>>(`${listPath}${separator}page=1&page_size=20`)
    rows.value = result.items ?? []
  } catch (caught) {
    rows.value = []
    pageError.value =
      caught instanceof ApiError
        ? messageWithContext(caught, '待办加载失败')
        : errorText(caught, '待办加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** 待办处理：动作同样来自服务端 `allowed_actions` + 能力声明。 */
const actionError = ref('')
const busyAction = ref<string | null>(null)

async function handleAction(action: string, row: Record<string, unknown>): Promise<void> {
  actionError.value = ''
  // 待办的处理动作在能力清单里以 `work_item.<action>` 出现
  const capability = meta.capabilities.value.find(
    (item) => item.name === `work_item.${action}` || item.name.endsWith(`.${action}`),
  )
  if (!capability) {
    actionError.value = `服务端未登记动作「${action}」，已拒绝执行`
    return
  }
  busyAction.value = action
  try {
    const path = capability.path.replace(/\{([a-z_]+)\}/gi, (_m, name: string) =>
      encodeURIComponent(String(row[name] ?? row.id ?? '')),
    )
    const fields: CapabilityField[] = capability.fields ?? []
    const body: Record<string, unknown> = {}
    for (const field of fields) {
      if (row[field.key] !== undefined && !field.readonly) body[field.key] = row[field.key]
    }
    // 乐观锁版本：待办行的"版本"是**塘口的 row_version**（`pond_version`），
    // 不是待办自己的 —— 核验的是那张申请，但乐观锁比的是塘口行（见 capability 声明）。
    const rowVersion = row.version ?? row.pond_version
    if (rowVersion !== undefined) body.expected_version = rowVersion
    await api.request(path, { method: capability.method, body })
    await load()
  } catch (caught) {
    actionError.value = submitErrorText(caught, errorText(caught, '处理失败'))
  } finally {
    busyAction.value = null
  }
}

defineExpose({ rows, columns, listCapability, missing, load, handleAction })
</script>

<template>
  <main class="workbench-page" data-testid="workbench-page">
    <header class="workbench-page__header">
      <h1 data-testid="page-title">工作台</h1>
      <p class="workbench-page__hint">待办与通知</p>
    </header>

    <p v-if="pageError" class="workbench-page__error" role="alert" data-testid="page-error">
      {{ pageError }}
      <ActionButton compact data-testid="page-reload" @click="load">重新加载</ActionButton>
    </p>

    <p v-if="actionError" class="workbench-page__error" role="alert" data-testid="action-error">
      {{ actionError }}
    </p>

    <!--
      元数据里没有待办/通知能力时**明确说明**，而不是渲染一张空表
      让用户以为"确实没有待办"——那是静默失败。

      ## 文案里**不出现能力名**（t10 修的显示缺陷）

      原先这里写着「当前能力清单中没有 `work_item.list` / `notification.list`」，
      而用户看到的就是 `work_item.list` —— 一个**内部标识符**。用户不需要知道
      "能力"这个概念，更不需要知道某个能力的**名字**：那是给写代码的人看的。

      改法：说清"工作台现在没有数据源，因为服务端还没把待办登记成可查数据"，
      并**顺带说清待办本身是存在的**（否则用户会以为系统坏了）。
      `CANDIDATE_NAMES` 仍留在 `<script>` 里 —— 那是**查找用的**，不该渲染出来。
    -->
    <section v-if="missing" class="workbench-page__notice" role="status" data-testid="missing-capability">
      <strong>待办与通知暂不可用</strong>
      <p>
        服务端还没有把「待办」登记成可以查询的数据，因此工作台暂时没有内容可以显示。
        这不代表您没有待办：塘口状态变更申请提交后会产生待核验的待办， 只是它目前还不能在这里列出。
      </p>
      <p class="workbench-page__notice-hint">如需查看，请到「塘口」列表打开对应塘口。</p>
    </section>

    <p v-else-if="loading" class="workbench-page__state" role="status">正在加载…</p>

    <DataTable
      v-else
      :resource="resourceName"
      :columns="columns"
      row-key="id"
      :rows="rows"
      empty-text="当前没有待处理事项"
      @action="handleAction"
    />
  </main>
</template>

<style scoped>
.workbench-page {
  display: grid;
  gap: 16px;
  width: 100%;
  max-width: var(--tone-content-max);
  margin: 0 auto;
  padding: 24px;
}
.workbench-page__header h1 {
  margin: 0 0 4px;
  color: var(--tone-ink);
  font-size: 20px;
  font-weight: 600;
}
.workbench-page__hint {
  margin: 0;
  color: var(--tone-muted);
  font-size: 13px;
}
.workbench-page__error {
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
.workbench-page__notice {
  padding: 14px 16px;
  border: 1px solid var(--tone-warning-line);
  border-radius: var(--tone-radius);
  background: var(--tone-warning-soft);
  font-size: 13px;
  line-height: 1.7;
}
.workbench-page__notice strong {
  display: block;
  margin-bottom: 6px;
  color: var(--tone-warning);
  font-weight: 600;
}
.workbench-page__notice p {
  margin: 0;
}
.workbench-page__state {
  padding: 40px 0;
  color: var(--tone-muted);
  font-size: 13px;
  text-align: center;
}
.workbench-page__notice-hint {
  margin-top: 6px;
  color: var(--tone-muted);
  font-size: 12px;
}
</style>
