import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import AppNav from '../src/layers/common/ui/AppNav.vue'
import ResourceListPage from '../src/layers/common/ui/ResourceListPage.vue'
import { __resetClientState } from '../src/layers/common/api/client'
import { __resetMetaForTest, loadMeta, navItems, resourcePathOf } from '../src/layers/common/meta/meta.store'
import type { ActionsMeta, Capability, ResourceMeta } from '../src/layers/common/types.gen'

/**
 * 导航与动态资源路由。
 *
 * ## 这一组守的是什么（一次实测缺陷）
 *
 * 后端有 69 条能力 / 23 个资源，而前端只有 14 条资源路由、且 `App.vue` 只有一个
 * `<RouterView />` —— **全项目没有任何导航组件**。用户登录后落在 `/ponds`，
 * 没有任何入口能去别处，于是他问"只有一个功能吗？"。
 *
 * 所以这里钉的判据是**两件机械事实**，而不是"菜单好不好看"：
 *
 *   1. 导航条目**完全由服务端元数据推导**（`navItems()`）；
 *   2. **服务端新增一个资源时，前端改零行代码**它就出现（见最后一条用例）。
 *
 * 第 2 条是这一组的核心：它把"前端不再持有资源清单"变成可执行的断言。
 */

function capability(
  partial: Partial<Capability> & Pick<Capability, 'name' | 'resource' | 'kind'>,
): Capability {
  return {
    title: partial.name,
    domain: partial.resource,
    method: 'GET',
    path: `/api/v1/${partial.resource}`,
    risk: 'normal',
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

function resource(name: string, listPath: string, title = name): ResourceMeta {
  return { name, title, list_path: listPath, columns: [], status_dict: [], module: 'probe' }
}

const ACTIONS: ActionsMeta = {
  tones: [],
  row_actions: ['view'],
  row_action_labels: { view: '查看' },
}

const META = {
  capabilities: [
    capability({ name: 'pond.list', resource: 'pond', kind: 'read', path: '/api/v1/ponds' }),
    capability({ name: 'batch.list', resource: 'batch', kind: 'read', path: '/api/v1/batches' }),
    // audit_log **有**读能力 → 应出现在导航里
    capability({ name: 'audit.log.list', resource: 'audit_log', kind: 'read', path: '/api/v1/audit-logs' }),
  ],
  resources: [
    resource('pond', '/api/v1/ponds', '塘口'),
    resource('batch', '/api/v1/batches', '批次'),
    resource('audit_log', '/api/v1/audit-logs', '操作日志'),
    // sales_receipt **没有**读能力 → 不得出现在导航里（点进去只有报错）
    resource('sales_receipt', '/api/v1/sales-receipts', '收款单'),
    // accounting_period 也没有读能力
    resource('accounting_period', '/api/v1/cost/periods', '会计期间'),
  ],
  actions: ACTIONS,
}

function stubMeta(meta = META) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ code: 'OK', message: '', request_id: 'r', data: meta }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    ),
  )
}

beforeEach(() => {
  __resetClientState()
  __resetMetaForTest()
  vi.unstubAllGlobals()
})

describe('资源路径由 list_path 推导（唯一规则）', () => {
  it('去掉 /api/v1 前缀', () => {
    expect(resourcePathOf('/api/v1/ponds')).toBe('/ponds')
    expect(resourcePathOf('/api/v1/purchase-orders')).toBe('/purchase-orders')
    expect(resourcePathOf('/api/v1/inventory/ledger')).toBe('/inventory/ledger')
  })

  it('不在 /api/v1 下的路径不可导航（返回空串，不瞎猜）', () => {
    expect(resourcePathOf('/other/thing')).toBe('')
  })
})

describe('navItems：完全由服务端元数据推导', () => {
  it('只收录「有读能力」的资源，跳过没有的', async () => {
    stubMeta()
    await loadMeta()
    const names = navItems().map((item) => item.name)
    expect(names).toContain('pond')
    expect(names).toContain('batch')
    expect(names).toContain('audit_log')
    // 没有读能力 → 不在导航里（否则点进去只有报错）
    expect(names).not.toContain('sales_receipt')
    expect(names).not.toContain('accounting_period')
  })

  it('条目的标题与路径都取自服务端', async () => {
    stubMeta()
    await loadMeta()
    const pond = navItems().find((item) => item.name === 'pond')
    expect(pond).toBeDefined()
    expect(pond!.title).toBe('塘口')
    expect(pond!.path).toBe('/ponds')
  })

  it('★ 新增一个服务端资源 → 导航自动出现，前端零改动', async () => {
    stubMeta()
    await loadMeta()
    expect(navItems().map((i) => i.name)).not.toContain('brand_new_thing')

    // 模拟"服务端加了一个资源 + 一条读能力"：前端**没有任何代码改动**
    stubMeta({
      capabilities: [
        ...META.capabilities,
        capability({ name: 'brand_new_thing.list', resource: 'brand_new_thing', kind: 'read' }),
      ],
      resources: [...META.resources, resource('brand_new_thing', '/api/v1/brand-new-things', '新资源')],
      actions: ACTIONS,
    })
    __resetMetaForTest()
    await loadMeta()

    const item = navItems().find((i) => i.name === 'brand_new_thing')
    expect(item).toBeDefined()
    expect(item!.title).toBe('新资源')
    expect(item!.path).toBe('/brand-new-things')
  })
})

describe('AppNav：渲染来自元数据，不手写菜单', () => {
  function mountNav() {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/:pathMatch(.*)*', component: { template: '<div />' } },
      ],
    })
    return mount(AppNav, { global: { plugins: [router], stubs: { RouterLink: false } } })
  }

  it('每个可导航资源渲染一个入口，标题用服务端的中文', async () => {
    stubMeta()
    await loadMeta()
    const wrapper = mountNav()
    await flushPromises()
    const links = wrapper.findAll('[data-testid^="nav-item-"]')
    const texts = links.map((node) => node.text())
    expect(texts).toContain('塘口')
    expect(texts).toContain('批次')
    expect(texts).toContain('操作日志')
    // 没有读能力的资源不渲染入口
    expect(wrapper.find('[data-testid="nav-item-sales_receipt"]').exists()).toBe(false)
  })

  it('链接地址就是推导出的资源路径', async () => {
    stubMeta()
    await loadMeta()
    const wrapper = mountNav()
    await flushPromises()
    const pond = wrapper.get('[data-testid="nav-item-pond"]')
    expect(pond.attributes('href')).toBe('/ponds')
    expect(wrapper.get('[data-testid="nav-item-audit_log"]').attributes('href')).toBe('/audit-logs')
  })
})

describe('ResourceListPage 能被任意资源复用（一个组件覆盖全部资源）', () => {
  it('同一组件换 resource 就换资源，不需要 per-resource 页面文件', async () => {
    stubMeta()
    await loadMeta()
    const wrapper = mount(ResourceListPage, {
      props: { resource: 'audit_log' },
      global: { stubs: { Teleport: true } },
    })
    await flushPromises()
    // 标题来自该资源的元数据
    expect(wrapper.text()).toContain('操作日志')
  })
})
