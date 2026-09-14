import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import PondDetailPage from '../src/layers/product/ponds/PondDetailPage.vue'
import WorkbenchPage from '../src/layers/product/workbench/WorkbenchPage.vue'
import { __resetClientState } from '../src/layers/common/api/client'
import { __setCsrfTokenForTest } from '../src/layers/common/security/csrf'
import { __resetMetaForTest } from '../src/layers/common/meta/meta.store'

/**
 * 两个特殊页面的测试：塘口详情（两阶段状态变更）与工作台。
 *
 * 工作台那一组同时是**元数据缺口的可执行证据**：能力清单里没有
 * `work_item.list` 时，页面必须**明确说明**而不是渲染空表——
 * 空表会让用户以为"确实没有待办"，那是静默失败。
 */

const POND_META = {
  capabilities: [
    {
      name: 'pond.list',
      title: '塘口列表',
      domain: 'master_data',
      resource: 'pond',
      method: 'GET',
      path: '/api/v1/ponds/{pond_id}',
      kind: 'read',
      risk: 'normal',
      confirmation: 'never',
      agent_exposure: 'exposed',
      required_permission: 'pond.view',
      scope_required: true,
      idempotent: false,
      description: '',
      fields: [],
      path_parameters: ['pond_id'],
      row_actions: ['view', 'edit', 'submit', 'verify'],
    },
    {
      name: 'pond_status_change.request',
      title: '申请变更塘口状态',
      domain: 'master_data',
      resource: 'pond',
      method: 'POST',
      path: '/api/v1/ponds/{pond_id}/status-changes',
      kind: 'action',
      risk: 'normal',
      confirmation: 'never',
      agent_exposure: 'exposed',
      required_permission: 'pond.update',
      scope_required: true,
      idempotent: true,
      description: '',
      // 刻意留空：registry §1.4 没有为它声明字段表（真实缺口）
      fields: [],
      path_parameters: ['pond_id'],
      row_actions: [],
    },
    {
      name: 'pond_status_change.verify',
      title: '核验塘口状态变更',
      domain: 'master_data',
      resource: 'pond',
      method: 'POST',
      path: '/api/v1/ponds/{pond_id}/status-changes/{change_id}/verify',
      kind: 'action',
      risk: 'high',
      confirmation: 'always',
      agent_exposure: 'exposed',
      required_permission: 'pond.verify',
      scope_required: true,
      idempotent: true,
      description: '',
      fields: [],
      path_parameters: ['pond_id', 'change_id'],
      row_actions: [],
    },
  ],
  resources: [
    {
      name: 'pond',
      title: '塘口',
      list_path: '/api/v1/ponds',
      columns: [
        { key: 'code', label: '塘口编号' },
        { key: 'pond_status_label', label: '业务状态', tone_key: 'pond_status' },
      ],
      status_dict: [
        { value: 'build', label: '待建设', tone: 'neutral' },
        { value: 'stocked', label: '已放苗', tone: 'info' },
        { value: 'farming', label: '养殖中', tone: 'success' },
      ],
    },
  ],
}

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

function stubFetch(payload: unknown, handler?: (url: string, init?: RequestInit) => Response | undefined) {
  const calls: { url: string; init?: RequestInit }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      calls.push({ url, init })
      if (url.endsWith('/auth/csrf')) return Promise.resolve(csrfOk())
      const custom = handler?.(url, init)
      if (custom) return Promise.resolve(custom)
      return Promise.resolve(ok(payload))
    }),
  )
  return calls
}

async function memoryRouter(id = '7') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', redirect: '/workbench' },
      { path: '/ponds', component: { template: '<div />' } },
      { path: '/ponds/:id', component: { template: '<div />' } },
      { path: '/workbench', component: { template: '<div />' } },
      { path: '/:pathMatch(.*)*', component: { template: '<div />' } },
    ],
  })
  await router.push(id ? `/ponds/${id}` : '/workbench')
  await router.isReady()
  return router
}

beforeEach(() => {
  __resetClientState()
  __resetMetaForTest()
  __setCsrfTokenForTest('csrf-test')
  vi.unstubAllGlobals()
})

describe('塘口详情：两阶段状态变更', () => {
  async function mountDetail(record: Record<string, unknown> | null) {
    stubFetch(record ? { record } : {}, (url) => {
      if (url.startsWith('/api/v1/meta/capabilities')) return ok(POND_META)
      return undefined
    })
    const router = await memoryRouter('7')
    const wrapper = mount(PondDetailPage, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    await flushPromises()
    return wrapper
  }

  it('渲染档案字段（列来自 ResourceMeta.columns）', async () => {
    const wrapper = await mountDetail({ id: 7, code: 'P-007', name: '3 号塘', pond_status: 'farming' })
    expect(wrapper.get('[data-testid="page-title"]').text()).toBe('3 号塘')
    const pairs = wrapper.get('[data-testid="record-pairs"]').text()
    expect(pairs).toContain('塘口编号')
    expect(pairs).toContain('P-007')
  })

  it('没有待核验变更时，给出「申请变更状态」入口', async () => {
    const wrapper = await mountDetail({ id: 7, code: 'P-007', name: '3 号塘', allowed_actions: [] })
    expect(wrapper.find('[data-testid="open-status-change"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pending-change"]').exists()).toBe(false)
  })

  it('申请表单的目标状态选项来自 status_dict，不写状态码字面量', async () => {
    const wrapper = await mountDetail({ id: 7, code: 'P-007', name: '3 号塘', allowed_actions: [] })
    await wrapper.get('[data-testid="open-status-change"]').trigger('click')
    await flushPromises()

    const form = wrapper.get('[data-testid="status-change-section"]')
    const options = form.findAll('option').map((n) => n.text())
    // 来自 POND_META.resources[0].status_dict
    expect(options).toContain('待建设')
    expect(options).toContain('已放苗')
    expect(options).toContain('养殖中')
  })

  it('后端未下发字段声明时，明确标注表单是派生的（不静默）', async () => {
    const wrapper = await mountDetail({ id: 7, code: 'P-007', name: '3 号塘', allowed_actions: [] })
    await wrapper.get('[data-testid="open-status-change"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="derived-fields-note"]').text()).toContain('服务端本能力未下发字段声明')
  })

  it('服务端下发「有待核验申请 + 可核验」时才给出核验按钮', async () => {
    // 判据**由服务端给**：`pending_status_change` + `can_verify_status_change`。
    // 原先这里判的是 `allowed_actions.includes('verify')` —— 那是**记录生命周期**的
    // 动作，业务状态变更的核验不在里面，于是按钮永远不出现（复核没有入口，实测）。
    const wrapper = await mountDetail({
      id: 7,
      code: 'P-007',
      name: '3 号塘',
      allowed_actions: ['view', 'archive'],
      pending_status_change: { id: 19, status: 'submitted', version: 1 },
      can_verify_status_change: true,
    })
    expect(wrapper.find('[data-testid="pending-change"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="verify-status-change"]').exists()).toBe(true)
  })

  it('有待核验申请但无权核验时不渲染核验按钮（不渲染点了会 403 的按钮）', async () => {
    const wrapper = await mountDetail({
      id: 7,
      code: 'P-007',
      name: '3 号塘',
      allowed_actions: ['view'],
      pending_status_change: { id: 19, status: 'submitted', version: 1 },
      can_verify_status_change: false,
    })
    expect(wrapper.find('[data-testid="pending-change"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="verify-status-change"]').exists()).toBe(false)
  })

  it('核验按钮按能力声明的路径提交（含路径参数替换）', async () => {
    const record = {
      id: 7,
      code: 'P-007',
      name: '3 号塘',
      row_version: 4,
      allowed_actions: ['view'],
      pending_status_change: { id: 19, status: 'submitted', version: 1 },
      can_verify_status_change: true,
    }
    const calls = stubFetch({ record }, (url) => {
      if (url.startsWith('/api/v1/meta/capabilities')) return ok(POND_META)
      return undefined
    })
    const router = await memoryRouter('7')
    const wrapper = mount(PondDetailPage, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    await flushPromises()

    await wrapper.get('[data-testid="verify-status-change"]').trigger('click')
    await flushPromises()

    const write = calls.find((c) => c.url.includes('/status-changes/19/verify'))
    expect(write, calls.map((c) => c.url).join(', ')).toBeDefined()
    expect(write!.init?.method).toBe('POST')
    // 能力声明的字段是 `pond_id` / `request_id` / `expected_version`
    // （路径参数同时也是字段，服务端要它们在 body 里）。
    // `expected_version` 比的是**塘口行的 row_version** —— 申请行没有版本列，
    // 而本能力的回读函数返回的是塘口行（见 capability 声明）。
    // 原先这里期待 `expected_pond_version`：那个字段服务端并不接受
    // （实测 400「请求包含不接受的字段」），而且路径参数写成了 `{change_id}`。
    const body = JSON.parse(String(write!.init?.body ?? '{}'))
    expect(body.pond_id).toBe(7)
    expect(body.request_id).toBe(19)
    expect(body.expected_version).toBe(4)
    expect(body.expected_pond_version).toBeUndefined()
  })
})

describe('工作台：能力缺失时明确说明而非渲染空表', () => {
  it('能力清单里没有 work_item 读能力时给出说明', async () => {
    stubFetch({ capabilities: [], resources: [] }, (url) => {
      if (url.startsWith('/api/v1/meta/capabilities')) return ok({ capabilities: [], resources: [] })
      return undefined
    })
    const router = await memoryRouter('')
    const wrapper = mount(WorkbenchPage, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    await flushPromises()

    const notice = wrapper.get('[data-testid="missing-capability"]')
    // 文案在 t10 改过：原先直接把能力名（`work_item.list`）
    // 显示给用户，而那是**内部标识符**。
    // 这条断言现在钉的是"说清了现状 + 不暴露内部名"**两件事**。
    expect(notice.text()).toContain('待办与通知暂不可用')
    expect(notice.text()).not.toContain('work_item.list')
    expect(notice.text()).not.toContain('notification.list')
    // 不能渲染成空表（那会被误读为"确实没有待办"）
    expect(wrapper.find('[data-testid="data-table"]').exists()).toBe(false)
  })

  it('后端补上 work_item.list 后同一页面即可渲染（无需改前端）', async () => {
    const meta = {
      capabilities: [
        {
          name: 'work_item.list',
          title: '待办列表',
          domain: 'workbench',
          resource: 'work_item',
          method: 'GET',
          path: '/api/v1/work-items',
          kind: 'read',
          risk: 'normal',
          confirmation: 'never',
          agent_exposure: 'hidden',
          required_permission: 'work_item.view',
          scope_required: false,
          idempotent: false,
          description: '',
          fields: [],
          path_parameters: [],
          row_actions: ['view'],
        },
      ],
      resources: [
        {
          name: 'work_item',
          title: '待办',
          list_path: '/api/v1/work-items',
          columns: [
            { key: 'title', label: '事项' },
            { key: 'status_label', label: '状态', tone_key: 'status' },
          ],
          status_dict: [{ value: 'pending', label: '待处理', tone: 'warning' }],
        },
      ],
    }
    stubFetch({}, (url) => {
      if (url.startsWith('/api/v1/meta/capabilities')) return ok(meta)
      if (url.startsWith('/api/v1/work-items')) {
        return ok({
          items: [
            { id: 1, title: '核验入库', status: 'pending', status_label: 'x', allowed_actions: ['view'] },
          ],
          page: 1,
          page_size: 20,
          total: 1,
          has_next: false,
        })
      }
      return undefined
    })
    const router = await memoryRouter('')
    const wrapper = mount(WorkbenchPage, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-testid="missing-capability"]').exists()).toBe(false)
    const table = wrapper.get('[data-testid="data-table"]')
    expect(table.text()).toContain('核验入库')
    expect(table.text()).toContain('待处理')
    expect(table.find('[data-tone="tone-warning"]').exists()).toBe(true)
  })
  it('申请提交带 expected_pond_version（§2.5 的并发一致性字段）', async () => {
    const record = { id: 7, code: 'P-007', name: '3 号塘', row_version: 9, allowed_actions: [] }
    const calls = stubFetch({ record }, (url) => {
      if (url.startsWith('/api/v1/meta/capabilities')) return ok(POND_META)
      return undefined
    })
    const router = await memoryRouter('7')
    const wrapper = mount(PondDetailPage, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    await flushPromises()

    await wrapper.get('[data-testid="open-status-change"]').trigger('click')
    await flushPromises()
    const form = wrapper.get('[data-testid="status-change-section"]')
    await form.get('select#field-to_status').setValue('farming')
    await form.get('textarea#field-reason').setValue('批次进场')
    await form.get('form').trigger('submit')
    await flushPromises()

    const write = calls.find((c) => c.url.includes('/status-changes') && c.init?.method === 'POST')
    expect(write, calls.map((c) => c.url).join(', ')).toBeDefined()
    const body = JSON.parse(String(write!.init?.body ?? '{}'))
    expect(body.to_status).toBe('farming')
    expect(body.reason).toBe('批次进场')
    expect(body.expected_pond_version).toBe(9)
  })
})
