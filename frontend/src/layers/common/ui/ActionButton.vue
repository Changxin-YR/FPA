<script setup lang="ts">
import { computed, useSlots } from 'vue'
import type { VNode } from 'vue'

/**
 * 统一按钮。继承早期版本 `layers/common/ui/ActionButton.vue`（48 行，
 * ARCHITECTURE.md §6.1 列为「质量高于平均」而保留）：
 *   - `aria-label` 从插槽文本推导，图标按钮也有可读名；
 *   - `aria-busy` + 视觉 spinner；
 *   - `prefers-reduced-motion` 下关闭动画。
 *
 * 早期版本的问题是「规范已立、推行未完成」：全仓 `<ActionButton` 只用了 11 处，
 * 同时有 70 处裸 `<button class="ghost-action|primary-action|table-action-btn">`
 * （.local/recon-frontend.md 问题 P-27 对应实体）。新骨架里所有按钮都走本组件。
 */
withDefaults(
  defineProps<{
    variant?: 'primary' | 'secondary' | 'quiet' | 'danger'
    loading?: boolean
    disabled?: boolean
    compact?: boolean
    // 可选：未传时由插槽文本推导（见 accessibleLabel），因此不需要 default
    // eslint-disable-next-line vue/require-default-prop
    label?: string
    type?: 'button' | 'submit' | 'reset'
  }>(),
  {
    variant: 'secondary',
    loading: false,
    disabled: false,
    compact: false,
    type: 'button',
  },
)

const slots = useSlots()

/** 从默认插槽提取纯文本，作为 `aria-label` 的兜底。 */
const slotLabel = computed(() =>
  (slots.default?.({}) ?? [])
    .map((node: VNode) => (typeof node.children === 'string' ? node.children : ''))
    .join('')
    .trim(),
)
</script>

<template>
  <button
    :type="type"
    class="action-button"
    :class="[`action-button--${variant}`, { 'action-button--compact': compact }]"
    :disabled="disabled || loading"
    :aria-busy="loading ? 'true' : undefined"
    :aria-label="label || slotLabel || undefined"
  >
    <span v-if="loading" class="action-button__spinner" aria-hidden="true" />
    <span v-if="loading">处理中…</span>
    <span v-else class="action-button__label"><slot /></span>
  </button>
</template>

<style scoped>
/* 一套按钮，三种语义。高度/圆角/字号统一 —— 先前"查看/提交/归档"三种视觉样式
   混用，是同一功能类型没有一个统一落点的直接后果。 */
.action-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  height: var(--tone-control-height);
  padding: 0 14px;
  border: 1px solid transparent;
  border-radius: var(--tone-radius);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  line-height: 1;
  white-space: nowrap;
  cursor: pointer;
  transition:
    background-color var(--tone-motion),
    border-color var(--tone-motion),
    color var(--tone-motion);
}
.action-button:focus-visible {
  outline: 0;
  box-shadow: var(--tone-focus-ring);
}
.action-button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.action-button--primary {
  color: var(--tone-on-primary);
  background: var(--tone-primary);
}
.action-button--primary:hover:not(:disabled) {
  background: var(--tone-primary-strong);
}
.action-button--secondary {
  border-color: var(--tone-line-strong);
  color: var(--tone-ink);
  background: var(--tone-surface);
}
.action-button--secondary:hover:not(:disabled) {
  border-color: var(--tone-primary);
  color: var(--tone-primary);
}
.action-button--quiet {
  color: var(--tone-ink-soft);
  background: transparent;
}
.action-button--quiet:hover:not(:disabled) {
  color: var(--tone-ink);
  background: var(--tone-surface-soft);
}
.action-button--danger {
  border-color: var(--tone-danger-line);
  color: var(--tone-danger);
  background: var(--tone-surface);
}
.action-button--danger:hover:not(:disabled) {
  border-color: var(--tone-danger);
  background: var(--tone-danger-soft);
}
.action-button--compact {
  height: 28px;
  padding: 0 10px;
  font-size: 12.5px;
}
.action-button__spinner {
  width: 14px;
  height: 14px;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  animation: action-button-spin 0.7s linear infinite;
}
@keyframes action-button-spin {
  to {
    transform: rotate(360deg);
  }
}
@media (prefers-reduced-motion: reduce) {
  .action-button,
  .action-button__spinner {
    transition: none;
    animation: none;
  }
}
</style>
