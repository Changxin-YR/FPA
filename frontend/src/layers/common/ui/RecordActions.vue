<script setup lang="ts">
import { ref, watch } from 'vue'
import ActionButton from './ActionButton.vue'
import { useMeta } from '../meta/meta.store'

/**
 * 按服务端返回的 `allowed_actions` 渲染行内动作。
 *
 * 这是早期版本**做对了的契约**，ARCHITECTURE.md §6.1 与 INTERFACES.md §7 都要求继承：
 * 动作由服务端算（`Capability.effective_confirmation` / 状态机），前端只渲染，
 * 不按权限码自行裁剪——早期版本 `returns/returnModel.ts:69-71` 的注释解释了原因：
 * 「页面不按权限码裁剪按钮以免误藏可用动作；权限仍由后端兜底校验」。
 *
 * ## 中文标签**不在这里**（这里原先就是那个第二处）
 *
 * 本组件曾经持有一张 `ACTION_LABELS = { view:'查看', archive:'归档', ... }`
 * 的表，注释还写着"不在页面里重复"。但事实是它**本身就是那个重复处**：
 *
 *     DataTable.vue        直接渲染裸 token   → 操作列显示 `view` / `archive`
 *     RecordActions.vue    自己翻译            → 显示「查看」/「归档」
 *
 * 于是**同一个系统里两套口径**，而状态的中文却只有一处落点（`State.label`）
 * —— 所以状态列一直是中文。现在动作走同一条纪律：
 * 标签的唯一定义在 `kernel/workflow.py::ACTION_LABELS`，经 `/meta/capabilities`
 * 的 `actions.row_action_labels` 下发，前端只查表（`meta.rowActionLabel`）。
 *
 * **不要再往本文件加任何文案映射**——那会把这一处刚统一的事实再拆回两处。
 */
const props = withDefaults(
  defineProps<{
    actions: string[]
    busyAction?: string | null
  }>(),
  { busyAction: null },
)

const emit = defineEmits<{
  action: [name: string]
}>()

const meta = useMeta()

/**
 * 动作名 → 中文。**查服务端下发的表**，本文件不再持有映射。
 *
 * 未登记动作由 `rowActionLabel` 回退成原词（可诊断）。
 */
function labelOf(action: string): string {
  return meta.rowActionLabel(action)
}

/**
 * 防双击：点击后短暂锁定，避免早期版本 `DataTablePage` 那种
 * 「一次点击触发两次写请求」的问题（早期版本靠 `RecordActions` 的 600ms 定时器兜住）。
 */
const locked = ref<string | null>(null)
watch(
  () => props.actions,
  () => {
    locked.value = null
  },
)

function run(action: string): void {
  if (locked.value) return
  locked.value = action
  emit('action', action)
  window.setTimeout(() => {
    if (locked.value === action) locked.value = null
  }, 600)
}

const isBusy = (action: string): boolean => props.busyAction === action || locked.value === action

const danger = (action: string): boolean => action === 'delete' || action === 'cancel' || action === 'reverse'

defineExpose({ labelOf })
</script>

<template>
  <div class="record-actions" data-testid="record-actions">
    <ActionButton
      v-for="action in actions"
      :key="action"
      compact
      :variant="danger(action) ? 'danger' : 'secondary'"
      :loading="isBusy(action)"
      :label="labelOf(action)"
      @click="run(action)"
    >
      {{ labelOf(action) }}
    </ActionButton>
  </div>
</template>

<style scoped>
.record-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
</style>
