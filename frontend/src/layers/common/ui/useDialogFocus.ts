import { onBeforeUnmount, watch, type Ref } from 'vue'

/**
 * 弹窗焦点管理：**打开入焦、关闭还原、Esc 关闭、（可选）Tab 圈定**。
 *
 * ## 它为什么存在
 *
 * 组件里写了 `@keydown.esc`，并不等于 Esc 能关闭弹窗 —— 键盘事件只会送给
 * **当前有焦点的元素及其祖先**。实测（review P2）：弹窗打开后焦点仍留在背景的
 * 「新建」按钮上，于是
 *
 * 1. 按 Esc 事件落在背景按钮上，弹窗上的监听器根本收不到；
 * 2. 按 Tab 顺着文档顺序走进背景筛选栏，用户以为自己还在弹窗里；
 * 3. 关闭后焦点丢失到 `<body>`，键盘用户要从头 Tab 回来。
 *
 * 三个症状是同一个根因：**没有把焦点移进弹窗**。这里把这套动作收敛成一处，
 * 而不是在两个组件里各写一遍（那必然有一处先漂移）。
 *
 * ## 可访问性约定
 *
 * - 打开时：记住触发元素 → 焦点移到弹窗内第一个可聚焦元素（没有则弹窗本身）；
 * - 关闭时：焦点还给触发元素（它还在 DOM 里的话）；
 * - `trapTab` 为真时 Tab / Shift+Tab 在弹窗内首尾循环；
 * - `trapTab` 为假时保留浏览器默认 Tab 顺序 —— **非模态浮层**（如右侧智能助手）
 *   本就不该把焦点锁死，否则用户无法用键盘离开它。
 *
 * ## 为什么不按 `offsetParent` 过滤可见性
 *
 * 常见写法用 `el.offsetParent !== null` 判可见。jsdom 不做布局，所有元素的
 * `offsetParent` 都是 `null`，单测里会把**全部**候选滤掉、退化成一个都聚焦不了。
 * 所以这里只按 HTML 自身的语义过滤（`disabled` / `hidden` / `tabindex="-1"`），
 * 选择器里已经排除了绝大多数不可聚焦元素。
 */

/** 与 `:not([disabled])` 等一起构成「可聚焦候选」的语义选择器。 */
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => !element.hidden && element.getAttribute('aria-hidden') !== 'true',
  )
}

export interface DialogFocusOptions {
  /** 弹窗是否打开。组件里通常是 `ref(false)`，或由弹窗状态派生的 `computed`。 */
  open: Ref<boolean>
  /** 弹窗根元素。需要能被 `focus()`，因此模板上应带 `tabindex="-1"`。 */
  panel: Ref<HTMLElement | null | undefined>
  /** 用户按 Esc / 需要关闭弹窗时调用。 */
  onClose: () => void
  /**
   * 是否把 Tab 圈定在弹窗内。模态对话框应为 `true`（默认）；
   * 非模态浮层应为 `false`，否则键盘用户被困住。
   */
  trapTab?: boolean
}

export function useDialogFocus(options: DialogFocusOptions): void {
  const { open, panel, onClose, trapTab = true } = options
  /** 打开前的焦点，关闭时还回去 —— 键盘用户由此回到触发按钮。 */
  let previouslyFocused: HTMLElement | null = null

  function firstFocusable(): HTMLElement | null {
    const root = panel.value
    if (!root) return null
    return focusableWithin(root)[0] ?? root
  }

  function onKeydown(event: KeyboardEvent): void {
    if (!open.value) return

    if (event.key === 'Escape') {
      onClose()
      return
    }
    if (event.key !== 'Tab' || !trapTab) return

    const root = panel.value
    if (!root) return
    const items = focusableWithin(root)
    const active = document.activeElement
    const inside = root.contains(active)

    if (items.length === 0) {
      // 弹窗里没有任何可聚焦元素：把焦点按在弹窗本身，别漏到背景去。
      event.preventDefault()
      root.focus()
      return
    }

    const first = items[0]
    const last = items[items.length - 1]
    if (event.shiftKey && (active === first || !inside)) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && (active === last || !inside)) {
      event.preventDefault()
      first.focus()
    }
  }

  watch(
    open,
    (isOpen) => {
      if (isOpen) {
        previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null
        // 监听挂在 document 上：即使某一刻焦点被脚本挪到弹窗外，Esc 也仍然有效。
        document.addEventListener('keydown', onKeydown)
        // `flush: 'post'`：回调在本次 DOM 更新之后运行，弹窗面板此时已经挂载，
        // 可以直接聚焦 —— 不必再嵌套一层 `nextTick`（那会让"打开即聚焦"晚一拍）。
        firstFocusable()?.focus()
      } else {
        document.removeEventListener('keydown', onKeydown)
        // 触发元素可能已随页面卸载消失；`isConnected` 过滤掉这种情况。
        if (previouslyFocused?.isConnected) previouslyFocused.focus()
        previouslyFocused = null
      }
    },
    { immediate: true, flush: 'post' },
  )

  onBeforeUnmount(() => {
    document.removeEventListener('keydown', onKeydown)
  })
}
