import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick, ref, type Ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import AgentPanel from '../src/layers/common/ui/AgentPanel.vue'
import ResourceListPage from '../src/layers/common/ui/ResourceListPage.vue'
import { useDialogFocus } from '../src/layers/common/ui/useDialogFocus'
import { __resetClientState } from '../src/layers/common/api/client'
import { __resetMetaForTest } from '../src/layers/common/meta/meta.store'
import { __setCsrfTokenForTest } from '../src/layers/common/security/csrf'

/**
 * review P2 回归：弹窗键盘交互。
 *
 * 实测缺陷：弹窗打开后焦点仍停在背景「新建」按钮上 → 面板上的 Esc 监听收不到事件、
 * Tab 顺着文档顺序走进背景筛选栏；`AgentPanel` 声明了 `@keydown.esc` 也因为焦点
 * 没进面板而完全无效。这里把「打开入焦 / 关闭还原 / Esc / Tab 圈定」钉成契约。
 */

enableAutoUnmount(afterEach)

beforeEach(() => {
  __resetClientState()
  __resetMetaForTest()
  __setCsrfTokenForTest('csrf-test')
  vi.unstubAllGlobals()
})

function csrfOk(): Response {
  return new Response(JSON.stringify({ code: 'OK', data: { csrf_token: 'csrf-test' }, request_id: 'r0' }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function ok(data: unknown): Response {
  return new Response(JSON.stringify({ code: 'OK', message: '', data, request_id: 'r1' }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** 在给定目标上派发一次真实的 keydown（bubbles 让它能到达 document 上的监听器）。 */
function pressKey(
  key: string,
  options: KeyboardEventInit = {},
  target: EventTarget = document,
): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...options })
  target.dispatchEvent(event)
  return event
}

interface FocusHarness {
  open: Ref<boolean>
  panel: Ref<HTMLElement | null>
  close: ReturnType<typeof vi.fn>
}

/**
 * 一个最小的弹窗宿主：只负责把 `useDialogFocus` 跑起来，并渲染
 * 「触发按钮 + 两个可聚焦元素」的结构，便于断言首尾循环。
 */
function mountFocusHarness(trapTab = true) {
  const harness = {} as FocusHarness
  const Host = defineComponent({
    setup() {
      harness.open = ref(false)
      harness.panel = ref<HTMLElement | null>(null)
      harness.close = vi.fn(() => {
        harness.open.value = false
      })
      useDialogFocus({ open: harness.open, panel: harness.panel, onClose: harness.close, trapTab })
      return () =>
        h('div', [
          h(
            'button',
            {
              'data-testid': 'trigger',
              onClick: () => {
                harness.open.value = true
              },
            },
            '打开',
          ),
          harness.open.value
            ? h(
                'div',
                {
                  ref: (element: unknown) => {
                    harness.panel.value = (element as HTMLElement | null) ?? null
                  },
                  'data-testid': 'panel',
                  tabindex: -1,
                },
                [
                  h('button', { 'data-testid': 'first' }, '第一个'),
                  h('button', { 'data-testid': 'last' }, '最后一个'),
                ],
              )
            : null,
        ])
    },
  })
  const wrapper = mount(Host, { attachTo: document.body })
  return { wrapper, harness }
}

const first = (): HTMLElement => document.querySelector('[data-testid="first"]') as HTMLElement
const last = (): HTMLElement => document.querySelector('[data-testid="last"]') as HTMLElement

describe('useDialogFocus：弹窗焦点契约', () => {
  it('打开后焦点进入弹窗内的第一个可聚焦元素', async () => {
    const { wrapper, harness } = mountFocusHarness()
    const trigger = wrapper.get('[data-testid="trigger"]').element as HTMLElement
    trigger.focus()

    harness.open.value = true
    await nextTick()

    expect(document.activeElement).toBe(first())
  })

  it('关闭后焦点还给打开它的触发元素', async () => {
    const { wrapper, harness } = mountFocusHarness()
    const trigger = wrapper.get('[data-testid="trigger"]').element as HTMLElement
    trigger.focus()

    harness.open.value = true
    await nextTick()
    harness.open.value = false
    await nextTick()

    expect(document.activeElement).toBe(trigger)
  })

  it('Esc 调用 onClose 并关闭弹窗', async () => {
    const { harness } = mountFocusHarness()
    harness.open.value = true
    await nextTick()

    pressKey('Escape')

    expect(harness.close).toHaveBeenCalledTimes(1)
    expect(harness.open.value).toBe(false)
  })

  it('Tab 在末尾回卷到开头，Shift+Tab 在开头回卷到末尾', async () => {
    const { harness } = mountFocusHarness()
    harness.open.value = true
    await nextTick()

    last().focus()
    const forward = pressKey('Tab', {}, last())
    expect(forward.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(first())

    first().focus()
    const backward = pressKey('Tab', { shiftKey: true }, first())
    expect(backward.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(last())
  })

  it('trapTab=false（非模态浮层）不劫持 Tab，用户能离开面板', async () => {
    const { harness } = mountFocusHarness(false)
    harness.open.value = true
    await nextTick()

    last().focus()
    const event = pressKey('Tab', {}, last())
    expect(event.defaultPrevented).toBe(false)
  })
})

const RESOURCE_META = {
  capabilities: [
    {
      name: 'pond.create',
      title: '新建塘口',
      domain: 'master_data',
      method: 'POST',
      path: '/api/v1/ponds',
      kind: 'create',
      risk: 'normal',
      confirmation: 'never',
      agent_exposure: 'exposed',
      required_permission: 'pond.create',
      scope_required: true,
      idempotent: true,
      description: '',
      resource: 'pond',
      fields: [{ key: 'code', label: '塘口编号', type: 'string', required: true }],
      path_parameters: [],
    },
  ],
  resources: [
    {
      name: 'pond',
      title: '塘口',
      list_path: '/api/v1/ponds',
      columns: [{ key: 'code', label: '塘口编号' }],
      status_dict: [{ value: 'farming', label: '养殖中', tone: 'success' }],
    },
  ],
}

/** ResourceListPage 用了 `useRouter()`（行内「查看」动作要跳详情页），挂载必须给 router。 */
function memoryRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/:pathMatch(.*)*', component: { template: '<div />' } },
    ],
  })
}

async function mountResourcePage() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/auth/csrf')) return Promise.resolve(csrfOk())
      if (url.startsWith('/api/v1/meta/capabilities')) return Promise.resolve(ok(RESOURCE_META))
      return Promise.resolve(ok({ items: [], page: 1, page_size: 20, total: 0, has_next: false }))
    }),
  )
  const wrapper = mount(ResourceListPage, {
    props: { resource: 'pond' },
    global: { plugins: [memoryRouter()], stubs: { Teleport: true } },
    attachTo: document.body,
  })
  await flushPromises()
  await flushPromises()
  return wrapper
}

describe('ResourceListPage：弹窗键盘交互', () => {
  it('打开后焦点进入弹窗，Tab 圈定在弹窗内，Esc 关闭并还原焦点', async () => {
    const wrapper = await mountResourcePage()
    const create = wrapper.get('[data-testid="page-create"]').element as HTMLElement
    create.focus()
    await wrapper.get('[data-testid="page-create"]').trigger('click')
    await nextTick()

    const dialog = wrapper.get('[data-testid="page-dialog"]').element as HTMLElement
    // 根因回归点：焦点不能还留在背景的「新建」按钮上
    expect(document.activeElement).not.toBe(create)
    expect(dialog.contains(document.activeElement)).toBe(true)

    const focusables = Array.from(dialog.querySelectorAll<HTMLElement>('button, input'))
    const dialogLast = focusables[focusables.length - 1]
    dialogLast.focus()
    const tabbed = pressKey('Tab', {}, dialogLast)
    expect(tabbed.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(focusables[0])

    pressKey('Escape')
    await nextTick()
    expect(wrapper.find('[data-testid="page-dialog"]').exists()).toBe(false)
    expect(document.activeElement).toBe(create)
  })
})

describe('AgentPanel：打开入焦与 Esc', () => {
  it('打开面板后焦点进入面板，Esc 关闭并还原到启动按钮', async () => {
    const wrapper = mount(AgentPanel, { props: { pageContext: '/ponds' }, attachTo: document.body })
    const launcher = wrapper.get('.agent-widget__launcher').element as HTMLElement
    launcher.focus()
    await wrapper.get('.agent-widget__launcher').trigger('click')
    await nextTick()

    const panel = wrapper.get('[data-testid="agent-panel"]').element as HTMLElement
    expect(panel.contains(document.activeElement)).toBe(true)

    pressKey('Escape')
    await nextTick()
    expect(wrapper.find('[data-testid="agent-panel"]').exists()).toBe(false)
    expect(document.activeElement).toBe(launcher)
  })

  it('非模态面板不圈定 Tab', async () => {
    const wrapper = mount(AgentPanel, { props: { pageContext: '/ponds' }, attachTo: document.body })
    await wrapper.get('.agent-widget__launcher').trigger('click')
    await nextTick()

    const input = wrapper.get('[data-testid="agent-input"]').element as HTMLElement
    input.focus()
    expect(pressKey('Tab', {}, input).defaultPrevented).toBe(false)
  })
})
