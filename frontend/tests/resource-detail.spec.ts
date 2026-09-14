import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import ResourceDetailPage from '../src/layers/common/ui/ResourceDetailPage.vue'
import { __resetClientState } from '../src/layers/common/api/client'
import { __setCsrfTokenForTest } from '../src/layers/common/security/csrf'
import { __resetMetaForTest } from '../src/layers/common/meta/meta.store'
import type { Capability, ResourceMeta } from '../src/layers/common/types.gen'

/**
 * 通用详情页（`/detail/:resource/:id`）。
 *
 * ## 它补的是哪条缺口
 *
 * 列表里每行的「查看」按钮**一直没有落点**：`ResourceListPage.runAction('view')`
 * 只把弹窗关掉、什么也不发生（更早的版本是打开**编辑表单**，那是另一种错位）。
 *
 * ## 这个文件钉住三件事
 *
 * 1. 列、状态中文、状态配色**全部来自元数据**（前端不写字段名、不写中文）；
 * 2. `available_transitions`（服务端给的合法目标）要渲染出来 —— 它是详情相对
 *    列表响应的**唯一增量**，也是登记 `*.get` 的理由；
 * 3. 资源**没有** `*.get` 时给**显式说明**，而不是白屏或伪装成 404。
 */

function capability(
  partial: Partial<Capability> & Pick<Capability, 'name' | 'resource' | 'kind'>,
): Capability {
  return {
    title: partial.name,
    domain: partial.resource,
    method: 'GET',
    path: `/api/v1/${partial.resource}s`,
    risk: 'read',
    confirmation: 'never',
    agent_exposure: 'exposed',
    required_permission: null,
    scope_required: false,
    idempotent: false,
    description: '',
    fields: [],
    path_parameters: [],
    row_actions: ['view'],
    ...partial,
  } as Capability
}

function resource(name: string, overrides: Partial<ResourceMeta> = {}): ResourceMeta {
  return {
    name,
    title: name,
    list_path: `/api/v1/${name}s`,
    columns: [{ key: 'code', label: '编号' }],
    status_dict: [],
    ...overrides,
  } as ResourceMeta
}

const ORDER_ROW = {
  id: 151,
  code: 'PO-000151',
  supplier_name: '某某饲料厂',
  status: 'draft',
  status_label: '草稿',
  available_transitions: [
    { value: 'submitted', label: '待审批' },
    { value: 'cancelled', label: '已取消' },
  ],
}

const META_WITH_GET = {
  capabilities: [
    capability({
      name: 'purchase_order.get',
      resource: 'purchase_order',
      kind: 'read',
      path: '/api/v1/purchase-orders/{order_id}',
    }),
  ],
  resources: [
    resource('purchase_order', {
      title: '采购单',
      list_path: '/api/v1/purchase-orders',
      columns: [
        { key: 'code', label: '采购单号' },
        { key: 'supplier_name', label: '供应商' },
        { key: 'status_label', label: '状态', tone_key: 'status' },
      ],
      status_dict: [
        { value: 'draft', label: '草稿', tone: 'neutral' },
        { value: 'submitted', label: '待审批', tone: 'warning' },
      ],
    }),
  ],
}

const META_WITHOUT_GET = {
  capabilities: [capability({ name: 'sales_order.list', resource: 'sales_order', kind: 'read' })],
  resources: [
    resource('sales_order', {
      title: '销售单',
      list_path: '/api/v1/sales-orders',
      columns: [{ key: 'code', label: '销售单号' }],
    }),
  ],
}

function stubFetch(meta: unknown, detailBody: unknown = { record: ORDER_ROW }) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/auth/csrf')) {
        return Promise.resolve(
          new Response(JSON.stringify({ code: 'OK', data: { csrf_token: 'csrf-test' } }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        )
      }
      if (url.startsWith('/api/v1/meta/capabilities')) {
        return Promise.resolve(
          new Response(JSON.stringify({ code: 'OK', message: '', data: meta }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        )
      }
      return Promise.resolve(
        new Response(JSON.stringify({ code: 'OK', message: '', data: detailBody }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    }),
  )
}

function mountDetail(resourceName: string, id: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/detail/:resource/:id', component: ResourceDetailPage },
      { path: '/:pathMatch(.*)*', component: { template: '<div />' } },
    ],
  })
  void router.push(`/detail/${resourceName}/${id}`)
  return mount(ResourceDetailPage, {
    props: { resource: resourceName, id },
    global: { plugins: [router] },
  })
}

describe('ResourceDetailPage：列与状态都来自元数据', () => {
  beforeEach(() => {
    __resetMetaForTest()
    __resetClientState()
    __setCsrfTokenForTest('csrf-test')
  })

  it('渲染服务端声明的列、状态中文，以及 available_transitions', async () => {
    stubFetch(META_WITH_GET)
    const wrapper = mountDetail('purchase_order', '151')
    await flushPromises()

    const text = wrapper.text()
    // 列标签来自 `ResourceMeta.columns`
    expect(text).toContain('采购单号')
    expect(text).toContain('供应商')
    // 值来自详情接口
    expect(text).toContain('PO-000151')
    expect(text).toContain('某某饲料厂')
    // 状态渲染成中文（来自 status_dict），不是 `draft`
    expect(text).toContain('草稿')
    expect(text).not.toContain('draft')
    // 详情相对列表的**唯一增量**
    expect(text).toContain('待审批')
    expect(text).toContain('已取消')
    expect(wrapper.find('[data-testid="resource-detail"]').exists()).toBe(true)
  })

  it('详情接口的地址来自能力声明的 path，而不是前端拼串', async () => {
    stubFetch(META_WITH_GET)
    mountDetail('purchase_order', '151')
    await flushPromises()
    const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
    const urls = calls.map((call) => String(call[0]))
    expect(urls.some((url) => url === '/api/v1/purchase-orders/151')).toBe(true)
    // 不许出现拼接痕迹（`/api/v1/purchase_orderss` 这类）
    expect(urls.some((url) => url.includes('purchase_orderss'))).toBe(false)
  })

  it('资源没有 *.get 时显式说明缺什么，而不是白屏', async () => {
    stubFetch(META_WITHOUT_GET)
    const wrapper = mountDetail('sales_order', '7')
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('服务端还没有登记')
    expect(text).toContain('销售单')
    expect(wrapper.find('[data-testid="resource-detail"]').exists()).toBe(true)
  })

  it('声明了机器码列 + 中文派生列时，详情只渲染中文列，不渲染裸机器码', async () => {
    // 实测缺陷：物料详情同时渲染 `status`（`verified`）与 `status_label`（`已核验`），
    // 两行同名「记录状态」，用户读作"系统中英文混杂"。
    // 判据与 DataTable 的 `displayColumns` 同一条：有 `<key>_label` 就隐藏机器码列。
    stubFetch(
      {
        capabilities: [
          capability({
            name: 'material.get',
            resource: 'material',
            kind: 'read',
            path: '/api/v1/materials/{material_id}',
          }),
        ],
        resources: [
          resource('material', {
            title: '物料',
            list_path: '/api/v1/materials',
            columns: [
              { key: 'code', label: '物料编号' },
              { key: 'status', label: '状态码' },
              { key: 'status_label', label: '记录状态', tone_key: 'status' },
            ],
            status_dict: [{ value: 'verified', label: '已核验', tone: 'success' }],
          }),
        ],
      },
      {
        record: { id: 12, code: 'MT-FEED-01', status: 'verified', status_label: '已核验' },
      },
    )
    const wrapper = mountDetail('material', '12')
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('记录状态')
    expect(text).toContain('已核验')
    // 机器码列必须被隐藏：`状态码` 与 `verified` 都不该出现在页面上
    expect(text).not.toContain('状态码')
    expect(text).not.toContain('verified')
  })
})
