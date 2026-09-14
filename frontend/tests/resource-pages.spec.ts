import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import ResourceListPage from '../src/layers/common/ui/ResourceListPage.vue'
import { __resetClientState } from '../src/layers/common/api/client'
import { __setCsrfTokenForTest } from '../src/layers/common/security/csrf'
import { __resetMetaForTest, loadMeta, selectableStatuses } from '../src/layers/common/meta/meta.store'
import type { Capability, ResourceMeta } from '../src/layers/common/types.gen'

/**
 * 业务列表页测试。
 *
 * 三个必测面（任务说明要求）：
 *  1. 渲染出正确列（列来自 `ResourceMeta.columns`）
 *  2. 按 `allowed_actions` 渲染动作
 *  3. 未知状态降级 `neutral`
 *
 * 因为所有页面都是 `ResourceListPage` 的薄包装，所以「多资源」的差异主要在
 * **元数据**上：这里为 4 个资源各构造一份 fixture，覆盖不同的
 * status_dict / 列 / 动作组合，从而验证「一份组件驱动 N 个资源」真的成立。
 */

function capability(
  partial: Partial<Capability> & Pick<Capability, 'name' | 'resource' | 'kind'>,
): Capability {
  return {
    title: partial.name,
    domain: partial.resource,
    method: 'GET',
    path: `/api/v1/${partial.resource}s`,
    risk: 'normal',
    confirmation: 'never',
    agent_exposure: 'exposed',
    required_permission: null,
    scope_required: false,
    idempotent: false,
    description: '',
    fields: [],
    path_parameters: [],
    row_actions: ['view', 'edit'],
    ...partial,
  } as Capability
}

function resource(name: string, overrides: Partial<ResourceMeta> = {}): ResourceMeta {
  return {
    name,
    title: name,
    list_path: `/api/v1/${name}s`,
    columns: [
      { key: 'code', label: `${name}编号` },
      { key: 'status_label', label: '状态', tone_key: 'status' },
    ],
    status_dict: [
      { value: 'draft', label: '草稿', tone: 'neutral' },
      { value: 'verified', label: '已核验', tone: 'success' },
    ],
    ...overrides,
  }
}

/** 4 个资源，覆盖不同的状态与动作组合。 */
const META: { capabilities: Capability[]; resources: ResourceMeta[] } = {
  capabilities: [
    capability({ name: 'pond.list', resource: 'pond', kind: 'read', path: '/api/v1/ponds' }),
    capability({ name: 'pond.create', resource: 'pond', kind: 'create', method: 'POST', title: '新建塘口' }),
    capability({ name: 'pond.update', resource: 'pond', kind: 'update', method: 'PATCH' }),
    capability({ name: 'batch.list', resource: 'batch', kind: 'read', path: '/api/v1/batchs' }),
    capability({ name: 'feeding.list', resource: 'feeding', kind: 'read', path: '/api/v1/feedings' }),
    capability({
      name: 'feeding.create',
      resource: 'feeding',
      kind: 'create',
      method: 'POST',
      title: '登记投喂',
      risk: 'high',
      fields: [{ key: 'quantity', label: '数量', type: 'number', required: true }],
    }),
    capability({ name: 'audit.log.list', resource: 'audit_log', kind: 'read', path: '/api/v1/audit-logs' }),
  ],
  resources: [
    resource('pond', {
      title: '塘口',
      columns: [
        { key: 'code', label: '塘口编号' },
        { key: 'status_label', label: '状态', tone_key: 'status' },
      ],
    }),
    resource('batch', {
      title: '养殖批次',
      columns: [
        { key: 'code', label: '批次编号' },
        { key: 'status_label', label: '状态', tone_key: 'status' },
      ],
      status_dict: [{ value: 'farming', label: '养殖中', tone: 'info' }],
    }),
    resource('feeding', {
      title: '投喂记录',
      columns: [
        { key: 'code', label: '投喂单号' },
        { key: 'status_label', label: '状态', tone_key: 'status' },
      ],
      status_dict: [{ value: 'submitted', label: '待核验', tone: 'warning' }],
    }),
    resource('audit_log', {
      title: '操作日志',
      columns: [
        { key: 'action_code', label: '操作' },
        { key: 'result', label: '结果' },
      ],
      status_dict: [],
    }),
  ],
}

function stubFetch(rowsByResource: Record<string, Record<string, unknown>[]>) {
  const writes: { url: string; method?: string; body?: string }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/auth/csrf')) {
        return Promise.resolve(
          new Response(JSON.stringify({ code: 'OK', data: { csrf_token: 'csrf-test' }, request_id: 'r0' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        )
      }
      if (url.startsWith('/api/v1/meta/capabilities')) {
        return Promise.resolve(
          new Response(JSON.stringify({ code: 'OK', message: '', data: META, request_id: 'r1' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        )
      }
      if (init?.method && init.method !== 'GET') {
        writes.push({ url, method: init.method, body: String(init.body ?? '') })
        return Promise.resolve(
          new Response(
            JSON.stringify({ code: 'OK', message: '', data: { record: { id: 1 } }, request_id: 'r2' }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            },
          ),
        )
      }
      const key = Object.keys(rowsByResource).find(
        (k) => url.startsWith(`/api/v1/${k}`) || url.startsWith(`/api/v1/${k}s`),
      )
      const items = key ? rowsByResource[key] : []
      return Promise.resolve(
        new Response(
          JSON.stringify({
            code: 'OK',
            message: '',
            data: { items, page: 1, page_size: 20, total: items.length, has_next: false },
            request_id: 'r3',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
    }),
  )
  return writes
}

function memoryRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', redirect: '/ponds' },
      { path: '/ponds', component: { template: '<div />' } },
      { path: '/:pathMatch(.*)*', component: { template: '<div />' } },
    ],
  })
}

/**
 * 挂载**通用资源页**并指定它渲染哪个资源。
 *
 * t8 之前这里传的是 4 个**包装组件**（每个 7 行、内容只有资源名）。
 * 那些包装件已删除：它们表达的信息（"这个页面是哪个资源"）在服务端元数据里
 * 本来就有，而包装件里的常量已经漂移过 —— `AuditLogPage` 写的是
 * `resource="audit.log"`，而服务端的资源名是 **`audit_log`**（下划线）。
 * 那个错**跑不出症状**（对应的 `ResourceMeta` 查不到 → 页面当空字典处理），
 * 所以测试全绿而页面是坏的。现在资源名由路由按元数据推导，前端不再持有它。
 */
async function mountPage(resourceName: string, rowsByResource: Record<string, Record<string, unknown>[]>) {
  const writes = stubFetch(rowsByResource)
  const wrapper = mount(ResourceListPage, {
    props: { resource: resourceName },
    global: { plugins: [memoryRouter()], stubs: { Teleport: true } },
  })
  await flushPromises()
  await flushPromises()
  return { wrapper, writes }
}

beforeEach(() => {
  __resetClientState()
  __resetMetaForTest()
  __setCsrfTokenForTest('csrf-test')
  vi.unstubAllGlobals()
})

describe('ResourceListPage：列来自 ResourceMeta.columns', () => {
  it.each([
    ['塘口', 'pond', '塘口编号', 'P-001'],
    ['批次', 'batch', '批次编号', 'B-001'],
    ['投喂', 'feeding', '投喂单号', 'F-001'],
    ['审计', 'audit_log', '操作', 'pond.create'],
  ])('%s 页渲染该资源的列与首行数据', async (_label, res, colLabel, cellValue) => {
    const row: Record<string, unknown> = { id: 1, allowed_actions: ['view'] }
    if (res === 'audit_log') {
      row.action_code = cellValue
      row.result = 'success'
    } else {
      row.code = cellValue
      row.status = res === 'pond' ? 'verified' : res === 'batch' ? 'farming' : 'submitted'
      row.status_label = 'x'
    }

    const { wrapper } = await mountPage(res, { [res]: [row] })
    expect(wrapper.get('[data-testid="data-table"]').text()).toContain(colLabel)
    expect(wrapper.get('[data-testid="data-table"]').text()).toContain(cellValue)
  })
})

describe('ResourceListPage：状态中文与配色来自 status_dict', () => {
  it.each([
    ['pond', 'verified', '已核验', 'tone-success'],
    ['batch', 'farming', '养殖中', 'tone-info'],
    ['feeding', 'submitted', '待核验', 'tone-warning'],
  ])('%s：状态码 %s → 中文「%s」/ %s', async (res, status, label, toneClass) => {
    const { wrapper } = await mountPage(res, {
      [res]: [{ id: 1, code: 'X-1', status, status_label: 'ignored', allowed_actions: [] }],
    })
    const table = wrapper.get('[data-testid="data-table"]')
    // 中文来自 status_dict，不是行里的 status_label
    expect(table.text()).toContain(label)
    expect(table.find(`[data-tone="${toneClass}"]`).exists()).toBe(true)
  })

  it('每个资源的未知状态都降级为 tone-neutral（不是空串）', async () => {
    for (const res of ['pond', 'batch', 'feeding'] as const) {
      __resetMetaForTest()
      __resetClientState()
      const { wrapper } = await mountPage(res, {
        [res]: [{ id: 1, code: 'X-1', status: 'never_seen_state', status_label: '???', allowed_actions: [] }],
      })
      const cell = wrapper.get('[data-testid="data-table"] [data-tone]')
      expect(cell.attributes('data-tone')).toBe('tone-neutral')
      expect(cell.attributes('data-tone')).not.toBe('')
    }
  })
})

describe('ResourceListPage：动作只来自 allowed_actions', () => {
  it('只渲染行上给出的动作，不额外补', async () => {
    const { wrapper } = await mountPage('pond', {
      pond: [
        {
          id: 1,
          code: 'P-001',
          status: 'draft',
          status_label: 'x',
          allowed_actions: ['view', 'submit'],
        },
      ],
    })
    const buttons = wrapper.findAll('[data-testid^="row-action-"]').map((n) => n.attributes('data-testid'))
    expect(buttons).toEqual(['row-action-view', 'row-action-submit'])
    expect(buttons).not.toContain('row-action-delete')
  })

  it('空 allowed_actions 时显示占位符，不生成按钮', async () => {
    const { wrapper } = await mountPage('pond', {
      pond: [{ id: 1, code: 'P-001', status: 'verified', status_label: 'x', allowed_actions: [] }],
    })
    expect(wrapper.findAll('[data-testid^="row-action-"]')).toHaveLength(0)
  })
})

describe('ResourceListPage：新建按钮与高风险标记来自能力声明', () => {
  it('有 create 能力时按钮标题用能力的 title', async () => {
    const { wrapper } = await mountPage('pond', { pond: [] })
    expect(wrapper.get('[data-testid="page-create"]').text()).toBe('新建塘口')
  })

  it('没有 create 能力的资源不显示新建按钮', async () => {
    const { wrapper } = await mountPage('audit_log', { audit_log: [] })
    expect(wrapper.find('[data-testid="page-create"]').exists()).toBe(false)
  })

  it('risk=high 的能力在弹窗里给出高风险提示', async () => {
    const { wrapper } = await mountPage('feeding', { feeding: [] })
    await wrapper.get('[data-testid="page-create"]').trigger('click')
    await flushPromises()
    const dialog = wrapper.get('[data-testid="page-dialog"]')
    expect(dialog.find('[data-testid="dialog-risk"]').exists()).toBe(true)
    // 表单字段也来自能力声明
    expect(dialog.text()).toContain('数量')
  })
})

describe('ResourceListPage：元数据缺失时显式失败', () => {
  it('未登记 list_path 时报错而不是渲染空表', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input)
        if (url.endsWith('/auth/csrf')) {
          return Promise.resolve(
            new Response(JSON.stringify({ code: 'OK', data: { csrf_token: 't' }, request_id: 'r0' }), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }),
          )
        }
        // 元数据里没有任何资源
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: 'OK',
              message: '',
              data: { capabilities: [], resources: [] },
              request_id: 'r',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        )
      }),
    )
    const wrapper = mount(ResourceListPage, {
      props: { resource: 'pond' },
      global: { plugins: [memoryRouter()], stubs: { Teleport: true } },
    })
    await flushPromises()
    await flushPromises()
    expect(wrapper.get('[data-testid="page-error"]').text()).toContain('未登记该资源的列表接口')
  })

  it('未登记的动作被拒绝执行并提示（不静默）', async () => {
    const { wrapper } = await mountPage('pond', {
      pond: [{ id: 1, code: 'P-001', status: 'draft', status_label: 'x', allowed_actions: ['teleport'] }],
    })
    await wrapper.get('[data-testid="row-action-teleport"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="action-error"]').text()).toContain('服务端未登记动作')
  })
})

describe('ResourceListPage：编辑 / 删除 / 分页路径', () => {
  it('edit 行内动作打开表单并带上 expected_version（乐观锁）', async () => {
    const writes: { url: string; method?: string; body?: string }[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.endsWith('/auth/csrf')) {
          return Promise.resolve(
            new Response(JSON.stringify({ code: 'OK', data: { csrf_token: 't' }, request_id: 'r0' }), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }),
          )
        }
        if (url.startsWith('/api/v1/meta/capabilities')) {
          return Promise.resolve(
            new Response(JSON.stringify({ code: 'OK', message: '', data: META, request_id: 'r1' }), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }),
          )
        }
        if (init?.method && init.method !== 'GET') {
          writes.push({ url, method: init.method, body: String(init.body ?? '') })
        }
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: 'OK',
              message: '',
              data: {
                items: [
                  {
                    id: 5,
                    code: 'P-005',
                    status: 'draft',
                    status_label: 'x',
                    version: 3,
                    allowed_actions: ['edit'],
                  },
                ],
                page: 1,
                page_size: 20,
                total: 1,
                has_next: false,
              },
              request_id: 'r2',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        )
      }),
    )

    const wrapper = mount(ResourceListPage, {
      props: { resource: 'pond' },
      global: { plugins: [memoryRouter()], stubs: { Teleport: true } },
    })
    await flushPromises()
    await flushPromises()

    await wrapper.get('[data-testid="row-action-edit"]').trigger('click')
    await flushPromises()
    // 编辑表单以现有行数据初始化
    expect(wrapper.find('[data-testid="page-dialog"]').exists()).toBe(true)

    await wrapper.get('[data-testid="page-dialog"] form').trigger('submit')
    await flushPromises()

    const write = writes.find((w) => w.method === 'PATCH')
    expect(write, writes.map((w) => w.method + ' ' + w.url).join(', ')).toBeDefined()
    const body = JSON.parse(write!.body ?? '{}')
    expect(body.expected_version).toBe(3)
  })

  it('delete 行内动作直接按能力声明的 method/path 发出（不需表单）', async () => {
    const meta = JSON.parse(JSON.stringify(META))
    meta.capabilities.push({
      name: 'pond.delete',
      title: '删除塘口',
      domain: 'master_data',
      resource: 'pond',
      method: 'DELETE',
      path: '/api/v1/ponds/{id}',
      kind: 'delete',
      risk: 'high',
      confirmation: 'never',
      agent_exposure: 'hidden',
      required_permission: 'pond.update',
      scope_required: true,
      idempotent: false,
      description: '',
      fields: [],
      path_parameters: ['id'],
      row_actions: ['view'],
    })

    const writes: { url: string; method?: string }[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.endsWith('/auth/csrf')) {
          return Promise.resolve(
            new Response(JSON.stringify({ code: 'OK', data: { csrf_token: 't' }, request_id: 'r0' }), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }),
          )
        }
        if (url.startsWith('/api/v1/meta/capabilities')) {
          return Promise.resolve(
            new Response(JSON.stringify({ code: 'OK', message: '', data: meta, request_id: 'r1' }), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }),
          )
        }
        if (init?.method && init.method !== 'GET') {
          writes.push({ url, method: init.method })
        }
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: 'OK',
              message: '',
              data: {
                items: [
                  { id: 9, code: 'P-009', status: 'draft', status_label: 'x', allowed_actions: ['delete'] },
                ],
                page: 1,
                page_size: 20,
                total: 1,
                has_next: false,
              },
              request_id: 'r2',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        )
      }),
    )

    const wrapper = mount(ResourceListPage, {
      props: { resource: 'pond' },
      global: { plugins: [memoryRouter()], stubs: { Teleport: true } },
    })
    await flushPromises()
    await flushPromises()

    await wrapper.get('[data-testid="row-action-delete"]').trigger('click')
    await flushPromises()

    const write = writes.find((w) => w.method === 'DELETE')
    expect(write, writes.map((w) => w.method + ' ' + w.url).join(', ')).toBeDefined()
    // 路径参数 {id} 被行数据替换
    expect(write!.url).toBe('/api/v1/ponds/9')
  })

  it('分页按钮按 has_next 与页码禁用，点击后重新拉取', async () => {
    let listCalls = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input)
        if (url.endsWith('/auth/csrf')) {
          return Promise.resolve(
            new Response(JSON.stringify({ code: 'OK', data: { csrf_token: 't' }, request_id: 'r0' }), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }),
          )
        }
        if (url.startsWith('/api/v1/meta/capabilities')) {
          return Promise.resolve(
            new Response(JSON.stringify({ code: 'OK', message: '', data: META, request_id: 'r1' }), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }),
          )
        }
        listCalls += 1
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: 'OK',
              message: '',
              data: { items: [], page: 1, page_size: 20, total: 42, has_next: true },
              request_id: 'r2',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        )
      }),
    )

    const wrapper = mount(ResourceListPage, {
      props: { resource: 'pond' },
      global: { plugins: [memoryRouter()], stubs: { Teleport: true } },
    })
    await flushPromises()
    await flushPromises()

    expect(wrapper.get('[data-testid="page-total"]').text()).toBe('共 42 条')
    // 第一页：上一页禁用，下一页可用（has_next=true）
    expect(wrapper.get('[data-testid="page-prev"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="page-next"]').attributes('disabled')).toBeUndefined()

    const before = listCalls
    await wrapper.get('[data-testid="page-next"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="page-current"]').text()).toBe('第 2 页')
    expect(listCalls).toBeGreaterThan(before)
  })
})

/**
 * `selectableStatuses`：**用户可选列表的唯一判据**。
 *
 * 这条规则的由来是一次真实缺陷：`cost_entry` 的 `submitted` / `archived` 标了
 * `reserved=True`（没有能力能进入它们），但它们**必须留在** `status_dict` 里——
 * 否则库里已存在的历史行会退化成裸状态码。而筛选下拉与目标状态下拉如果照
 * `status_dict` 全量渲染，用户就能选到"永远筛不出结果"或"必然被服务端拒"的项。
 *
 * 判据是 `!terminal && !reserved`，由**服务端** `State.selectable` 下发给前端；
 * 前端只筛选、不重算——否则这条规则就有了第二处实现。
 */
describe('selectableStatuses：跳过 terminal / reserved', () => {
  const PROBE = {
    capabilities: [],
    resources: [
      {
        name: 'probe',
        title: '探针',
        module: 'probe',
        list_path: '/api/v1/probes',
        columns: [],
        status_dict: [
          { value: 'draft', label: '草稿', tone: 'neutral', selectable: true },
          { value: 'verified', label: '已核验', tone: 'success', selectable: true },
          { value: 'submitted', label: '待核验', tone: 'warning', selectable: false, reserved: true },
          { value: 'closed', label: '已关闭', tone: 'neutral', selectable: false, terminal: true },
        ],
      },
    ],
  }

  it('只返回 selectable 为真的项，顺序保持服务端给的顺序', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ code: 'OK', message: '', data: PROBE, request_id: 'r' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        ),
      ),
    )
    await loadMeta()
    expect(selectableStatuses('probe').map((entry) => entry.value)).toEqual(['draft', 'verified'])
  })

  it('缺 selectable 字段时视为可选（老服务端不能把下拉清空）', async () => {
    __resetMetaForTest()
    const legacy = {
      capabilities: [],
      resources: [
        {
          name: 'legacy',
          title: '旧服务端',
          module: 'x',
          list_path: '/api/v1/x',
          columns: [],
          // 没有 selectable 字段：老服务端
          status_dict: [{ value: 'draft', label: '草稿', tone: 'neutral' }],
        },
      ],
    }
    vi.unstubAllGlobals()
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ code: 'OK', message: '', data: legacy, request_id: 'r' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        ),
      ),
    )
    await loadMeta()
    // 把"缺字段"读成"不可选"会把一次兼容性问题伪装成"这个资源没有可选状态"
    expect(selectableStatuses('legacy').map((entry) => entry.value)).toEqual(['draft'])
  })

  it('未知资源返回空数组，不抛错', () => {
    expect(selectableStatuses('no-such-resource')).toEqual([])
  })
})
