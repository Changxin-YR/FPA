<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import ActionButton from '../../common/ui/ActionButton.vue'
import DynamicForm from '../../common/ui/DynamicForm.vue'
import RecordActions from '../../common/ui/RecordActions.vue'
import { api } from '../../common/api/client'
import { ApiError, errorText, messageWithContext, submitErrorText } from '../../common/api/errors'
import { capabilityByName, useMeta } from '../../common/meta/meta.store'
import type { Capability, CapabilityField } from '../../common/types.gen'

/**
 * 塘口详情：含**两阶段状态变更**（申请 → 核验）。
 *
 * ## 为什么这一页比列表页厚（以及这不违反「页面要薄」）
 *
 * 塘口是**双状态资源**（`CAPABILITY_REGISTRY.md` §3.1）：
 * `status` 是记录生命周期，`pond_status` 是业务状态；业务状态的合法转移表在 §3.1，
 * 且必须经 `pond_status_change.request` + `pond_status_change.verify` 两步。
 * 这个「两步流程 + 待核验中间态」是**业务流程本身**，不是元数据缺陷。
 *
 * ## 仍然不写字段名
 *
 * - 记录字段来自 `Capability('pond.update').fields`
 * - 可选的**目标状态**来自服务端状态字典，只取**可选项**（`selectableStatuses`），
 *   不写 build/stocked 字面量
 * - 动作按钮来自服务端 `allowed_actions`
 *
 * ## 字段来源与一处待后端补全的细节
 *
 * `CAPABILITY_REGISTRY.md` §2.5 已为这两个能力声明字段：
 *
 * | key | type | 说明 |
 * |---|---|---|
 * | `to_status` | enum | 合法目标状态（**服务端按 §3.1-B 转移表过滤 `choices`**）|
 * | `reason` | text | 变更原因 |
 * | `expected_pond_version` | integer | 等于塘口当前 `row_version`（前端自动带）|
 *
 * 本页两条路径都支持：
 *  - 后端在 `capability.fields` 里给出声明 → 直接用（含服务端按转移表过滤后的 choices）；
 *  - 未给出 → 用**可选**状态（`selectableStatuses`）派生 `to_status` 的选项
 *    （**不写状态码字面量**），并在界面上明确标注这是派生表单。
 *
 * ## 与"从当前 `pond_status` 出发的合法目标"仍不**完全**等价（但不再是缺口）
 *
 * 服务端声明路径给的是 `available_transitions(current)` ——它按**当前行状态**精确过滤。
 * 回退路径没有"当前行的状态机上下文"可用（它只拿到资源级元数据），所以给的是
 * 「全部可选状态」这个**上界**。
 *
 * 这个上界与之前的差别是**实质的**：先前回退用的是完整 `status_dict`，会把
 * `terminal`（已废弃，进入后不能再转移）与 `reserved`（刻意预留、无能力能进入）
 * 也列进下拉——用户选中后**必然**被服务端拒；现在这两类被排除，
 * 回退路径给的都是真能走的目标。剩下的差异只是"当前状态到不了的合法状态"
 * （例如已核验的塘口不能再回到待建设），那一层由服务端的 `require_transition` 兜住，
 * 并且这正是服务端声明路径存在的意义。
 */

const route = useRoute()
const meta = useMeta()

const pondId = computed(() => String(route.params.id ?? ''))

const record = ref<Record<string, unknown> | null>(null)
const loading = ref(true)
const pageError = ref('')
const actionError = ref('')
const busy = ref(false)
const openRequest = ref(false)

const pondCapabilities = computed(() => meta.capabilities.value.filter((item) => item.resource === 'pond'))
/**
 * 取单条用的能力。
 *
 * **判据是结构性的**：`path` 里带 `{...}` 占位符的那条才是"按 id 取单条"。
 * 只按 `kind === 'read'` 找会命中排在前面的 `pond.list`（`/api/v1/ponds`），
 * 于是 `replace(/\{([a-z_]+)\}/)` 一个占位符都替换不到、请求打到**列表接口**上，
 * 整页字段全空却不报错（列表接口返回 200）。
 */
const listCapability = computed(
  () =>
    pondCapabilities.value.find((item) => item.kind === 'read' && item.path.includes('{')) ??
    pondCapabilities.value.find((item) => item.kind === 'read'),
)
const pendingRequest = computed(
  () => record.value?.pending_status_change as Record<string, unknown> | null | undefined,
)

/** 该记录可执行的动作：服务端算出的 `allowed_actions`。 */
const allowedActions = computed<string[]>(() =>
  Array.isArray(record.value?.allowed_actions) ? (record.value.allowed_actions as string[]) : [],
)

/** 记录字段（用于展示），来自元数据。 */
const recordColumns = computed(() => {
  const resourceMeta = meta.resourcesByName.value.get('pond')
  return resourceMeta?.columns ?? []
})

/**
 * 状态变更申请的字段。
 *
 * 优先用服务端给 `pond_status_change.request` 声明的字段；服务端没声明时，
 * 从 `status_dict` 派生「目标状态」下拉 + 一个理由输入——**不写状态码字面量**。
 */
const requestFields = computed<CapabilityField[]>(() => {
  const declared = capabilityByName('pond_status_change.request')?.fields
  if (declared && declared.length) return declared
  // 只取**可选**状态（`selectableStatuses`），不用全量 `status_dict`：
  // 全量里包含 `terminal`（如已放弃）与 `reserved`（刻意预留、无能力能进入）
  // 两类，选中后服务端的 `require_transition` 必然拒绝 —— 那就是一个
  // "看起来能用、点下去报错"的下拉。
  const choices = meta.selectableStatuses('pond').map((entry) => ({
    value: entry.value,
    label: entry.label,
  }))
  return [
    {
      key: 'to_status',
      label: '目标状态',
      type: 'enum',
      required: true,
      choices,
      help: '变更需经另一人核验后生效',
    },
    { key: 'reason', label: '变更理由', type: 'text', required: true },
  ]
})

/** 服务端声明了字段就用它，否则用派生字段——本页据此在报告里标注缺口。 */
const fieldsAreDerived = computed(() => {
  const declared = capabilityByName('pond_status_change.request')?.fields
  return !declared || declared.length === 0
})

async function load(): Promise<void> {
  loading.value = true
  pageError.value = ''
  try {
    await meta.loadMeta()
    const capability = listCapability.value
    if (!capability) throw new Error('服务端未声明塘口读取能力')
    // 列表能力返回分页；详情用路径参数取单条（path 模板已带 {pond_id}）
    const detailPath = capability.path.replace(/\{([a-z_]+)\}/gi, () => encodeURIComponent(pondId.value))
    const result = await api.get<{ record?: Record<string, unknown> } | Record<string, unknown>>(detailPath)
    record.value =
      (result as { record?: Record<string, unknown> }).record ?? (result as Record<string, unknown>)
  } catch (caught) {
    record.value = null
    pageError.value =
      caught instanceof ApiError
        ? messageWithContext(caught, '塘口详情加载失败')
        : errorText(caught, '塘口详情加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** 提交状态变更申请。 */
async function submitRequest(payload: Record<string, unknown>): Promise<void> {
  if (busy.value) return
  busy.value = true
  actionError.value = ''
  try {
    const capability = capabilityByName('pond_status_change.request')
    if (!capability) throw new Error('服务端未声明状态变更能力')
    const path = capability.path.replace(/\{([a-z_]+)\}/gi, (_m, name: string) =>
      encodeURIComponent(String(record.value?.[name] ?? pondId.value)),
    )
    const body = { ...payload }
    // §2.5：这个能力带的是 `expected_pond_version`（塘口当前 row_version），
    // 不是通用的 `expected_version`——它校验的是申请单与塘口的并发一致性。
    const pondVersion = record.value?.row_version ?? record.value?.version
    if (pondVersion !== undefined) body.expected_pond_version = pondVersion
    await api.request(path, { method: capability.method, body })
    openRequest.value = false
    await load()
  } catch (caught) {
    actionError.value = submitErrorText(caught, errorText(caught, '申请失败'))
  } finally {
    busy.value = false
  }
}

/** 核验待处理的状态变更。 */
async function verifyRequest(): Promise<void> {
  const pending = pendingRequest.value
  if (busy.value || !pending) return
  busy.value = true
  actionError.value = ''
  try {
    const capability = capabilityByName('pond_status_change.verify')
    if (!capability) throw new Error('服务端未声明状态变更核验能力')
    const path = capability.path
      // 逐个占位符替换：`{pond_id}` 取本页塘口，**其余占位符一律取申请单 id**
      // （服务端叫 `request_id`；写死名字时改名会让占位符原样留在 URL 上 → 404）。
      .replace(/\{([a-z_]+)\}/gi, (_m, name: string) =>
        encodeURIComponent(String(name === 'pond_id' ? pondId.value : (pending.id ?? ''))),
      )
    // 三个字段都是能力声明的**必填字段**（路径参数同时也是字段，服务端要它们在 body 里）；
    // `expected_version` 比的是**塘口行的 row_version** —— 申请行没有版本列，
    // 而本能力的回读函数返回的是塘口行（见 `pond_status_change.verify` 的声明）。
    const pondVersion = Number(record.value?.row_version ?? record.value?.version ?? 1)
    const body: Record<string, unknown> = {
      pond_id: Number(pondId.value),
      request_id: Number(pending.id ?? 0),
      expected_version: pondVersion,
    }
    await api.request(path, { method: capability.method, body })
    await load()
  } catch (caught) {
    actionError.value = submitErrorText(caught, errorText(caught, '核验失败'))
  } finally {
    busy.value = false
  }
}

/** 其它行内动作（submit / verify / archive…）统一走能力声明。 */
async function runAction(action: string): Promise<void> {
  actionError.value = ''
  const capability: Capability | undefined = pondCapabilities.value.find((item) =>
    item.name.endsWith(`.${action}`),
  )
  if (!capability || !record.value) {
    actionError.value = `服务端未登记动作「${action}」，已拒绝执行`
    return
  }
  busy.value = true
  try {
    const path = capability.path.replace(/\{([a-z_]+)\}/gi, (_m, name: string) =>
      encodeURIComponent(String(record.value?.[name] ?? pondId.value)),
    )
    const body: Record<string, unknown> = {}
    if (record.value.version !== undefined) body.expected_version = record.value.version
    await api.request(path, { method: capability.method, body })
    await load()
  } catch (caught) {
    actionError.value = submitErrorText(caught, errorText(caught, '操作失败'))
  } finally {
    busy.value = false
  }
}

const canRequestChange = computed(() => Boolean(capabilityByName('pond_status_change.request')))
const canVerifyChange = computed(
  // 判据由**服务端**给：`can_verify_status_change`（是否持 `pond.status.verify`）
  // + 确实存在待核验申请。原先用 `allowedActions.includes('verify')` 是错的 ——
  // 那个列表是**记录生命周期**的动作，业务状态变更的核验不在里面，
  // 于是这个按钮永远不出现（复核在页面上没有入口）。
  () => Boolean(pendingRequest.value) && record.value?.can_verify_status_change === true,
)

defineExpose({
  record,
  requestFields,
  fieldsAreDerived,
  submitRequest,
  verifyRequest,
  runAction,
  allowedActions,
})
</script>

<template>
  <main class="pond-detail" data-testid="pond-detail">
    <p v-if="pageError" class="pond-detail__error" role="alert" data-testid="page-error">
      {{ pageError }}
      <ActionButton compact data-testid="page-reload" @click="load">重新加载</ActionButton>
    </p>

    <p v-else-if="loading" class="pond-detail__state" role="status">正在加载…</p>

    <template v-else-if="record">
      <header class="pond-detail__header">
        <div>
          <h1 data-testid="page-title">{{ record.name ?? `塘口 #${pondId}` }}</h1>
          <p class="pond-detail__hint">{{ record.code ?? '' }}</p>
        </div>
        <RecordActions :actions="allowedActions" :busy-action="busy ? 'verify' : null" @action="runAction" />
      </header>

      <p v-if="actionError" class="pond-detail__error" role="alert" data-testid="action-error">
        {{ actionError }}
      </p>

      <section class="pond-detail__card">
        <h2>档案信息</h2>
        <dl class="pond-detail__pairs" data-testid="record-pairs">
          <div v-for="column in recordColumns" :key="column.key">
            <dt>{{ column.label }}</dt>
            <dd>{{ record[column.key] ?? '—' }}</dd>
          </div>
        </dl>
      </section>

      <section class="pond-detail__card" data-testid="status-change-section">
        <h2>业务状态变更</h2>

        <p v-if="pendingRequest" class="pond-detail__pending" data-testid="pending-change">
          已有待核验的状态变更申请（#{{ pendingRequest.id ?? '—' }}）， 需由另一人核验后生效。
          <ActionButton
            v-if="canVerifyChange"
            compact
            variant="primary"
            :loading="busy"
            data-testid="verify-status-change"
            @click="verifyRequest"
          >
            核验通过
          </ActionButton>
        </p>

        <template v-else>
          <ActionButton
            v-if="canRequestChange"
            variant="primary"
            data-testid="open-status-change"
            @click="openRequest = !openRequest"
          >
            申请变更状态
          </ActionButton>
          <p v-else class="pond-detail__state">服务端未声明状态变更能力</p>
        </template>

        <p v-if="openRequest && canRequestChange" class="pond-detail__note" data-testid="derived-fields-note">
          <template v-if="fieldsAreDerived">
            服务端本能力未下发字段声明，以下表单由状态字典派生（目标状态选项来自
            <code>status_dict</code>）。
          </template>
        </p>

        <DynamicForm
          v-if="openRequest && canRequestChange"
          :fields="requestFields"
          :busy="busy"
          submit-label="提交申请"
          @submit="submitRequest"
        />
      </section>
    </template>
  </main>
</template>

<style scoped>
.pond-detail {
  display: grid;
  gap: 16px;
  width: 100%;
  max-width: var(--tone-content-max);
  margin: 0 auto;
  padding: 24px;
}
.pond-detail__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.pond-detail__header h1 {
  margin: 0 0 4px;
  color: var(--tone-ink);
  font-size: 20px;
  font-weight: 600;
}
.pond-detail__hint {
  margin: 0;
  color: var(--tone-muted);
  font-size: 13px;
}
.pond-detail__card {
  display: grid;
  gap: 12px;
  padding: 18px;
  border: 1px solid var(--tone-line);
  border-radius: var(--tone-radius);
  background: var(--tone-surface);
  box-shadow: var(--tone-shadow-card);
}
.pond-detail__card h2 {
  margin: 0;
  color: var(--tone-ink);
  font-size: 15px;
  font-weight: 600;
}
.pond-detail__pairs {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 6px 16px;
  margin: 0;
  font-size: 13px;
}
.pond-detail__pairs dt {
  color: var(--tone-muted);
}
.pond-detail__pairs dd {
  margin: 0;
  color: var(--tone-ink);
}
.pond-detail__pending {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 0;
  padding: 10px 12px;
  border: 1px solid var(--tone-warning-line);
  border-radius: var(--tone-radius);
  background: var(--tone-warning-soft);
  font-size: 13px;
}
.pond-detail__note {
  margin: 0;
  color: var(--tone-muted);
  font-size: 12px;
  line-height: 1.6;
}
.pond-detail__error {
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
.pond-detail__state {
  padding: 16px 0;
  color: var(--tone-muted);
  font-size: 13px;
}
</style>
