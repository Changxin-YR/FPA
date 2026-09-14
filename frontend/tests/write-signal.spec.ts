import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import ResourceListPage from '../src/layers/common/ui/ResourceListPage.vue'
import { __resetClientState } from '../src/layers/common/api/client'
import { __setCsrfTokenForTest } from '../src/layers/common/security/csrf'
import { __resetMetaForTest } from '../src/layers/common/meta/meta.store'
import {
  __resetAgentWriteSignal,
  executedResourcesOf,
  isResourceDirty,
  publishAgentWrite,
} from '../src/layers/common/agent/write-signal'
import type { Capability, ResourceMeta } from '../src/layers/common/types.gen'

/**
 * 智能体写入后**当前列表自动刷新**（用户报："智能体新增数据依旧要刷新后才显示"）。
 *
 * ## 这条链路的三个环节，逐环节钉住
 *
 * 1. **判据来自服务端**（`executedResourcesOf`）：只有 `kind === 'executed'` 才算写入。
 *    回复文本里出现"已创建"**不算** —— 那会被"我**没有**创建成功"骗到。
 * 2. **发布只有一处**（`AgentPanel` 拿到 result 时）；**订阅只有一处**
 *    （`ResourceListPage` 监听版本号）。两个组件各写一套判定 = 两处会漂移的规则。
 * 3. **只刷"被改动的那个资源"**：用户在塘口页让助手登记成本，刷塘口列表毫无用处。
 *
 * ## 反例与正例同等重要
 *
 * 纯查询（"现在有几个塘口"）**不得**触发刷新 —— 否则"哪些轮真的写了"这个事实
 * 就不可观测了，而且每轮都白拉一次列表。下面同时断言正例与反例。
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
    row_actions: ['view'],
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
    status_dict: [{ value: 'draft', label: '草稿', tone: 'neutral' }],
    ...overrides,
  }
}

const META = {
  capabilities: [
    capability({ name: 'pond.list', resource: 'pond', kind: 'read', path: '/api/v1/ponds' }),
    capability({
      name: 'cost_entry.list',
      resource: 'cost_entry',
      kind: 'read',
      path: '/api/v1/cost_entrys',
    }),
  ],
  resources: [resource('pond', { title: '塘口' }), resource('cost_entry', { title: '成本' })],
}

interface ListCall {
  url: string
}

/**
 * 打桩：**服务端真的变了**才会给出更多行。
 *
 * 第一版我按"第几次请求"给数据，于是"发了请求"和"库里多了行"被混成一件事 ——
 * 断言就变成了在数请求次数。这里改成显式的服务端状态：测试自己控制
 * `rowsOnServer`（模拟智能体真的写进去了），于是"重新拉取"与"界面更新"是两件
 * 各自可断言的事。
 */
function stubFetch(listCalls: ListCall[] = []): { lists: ListCall[]; setRows: (n: number) => void } {
  let rowsOnServer = 1
  const lists = listCalls
  const build = (count: number) =>
    Array.from({ length: count }, (_value, index) => ({
      id: index + 1,
      code: `P-00${index + 1}`,
      status: 'draft',
      status_label: '草稿',
      allowed_actions: [],
    }))

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
        return Promise.resolve(
          new Response(
            JSON.stringify({ code: 'OK', message: '', data: { record: { id: 1 } }, request_id: 'r2' }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        )
      }
      lists.push({ url })
      const items = build(rowsOnServer)
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
  return {
    lists,
    setRows: (n: number) => {
      rowsOnServer = n
    },
  }
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
 * 每个用例挂载过的组件。**必须逐个卸载**：
 * `ResourceListPage` 的 `watch(writeVersion, …)` 挂在模块级单例上，上一用例的
 * 组件只要还活着，下一次 `publishAgentWrite()` 就会同时唤醒它 ——
 * 列表请求计数于是被别的用例污染（实测报错"期望 2、实际 6"）。
 */
const mounted: { unmount: () => void }[] = []

async function mountPage(resourceName: string) {
  const server = stubFetch()
  const wrapper = mount(ResourceListPage, {
    props: { resource: resourceName },
    global: { plugins: [memoryRouter()], stubs: { Teleport: true } },
  })
  mounted.push(wrapper)
  await flushPromises()
  await flushPromises()
  return { wrapper, lists: server.lists, setRows: server.setRows }
}

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount()
})

/** 表格里渲染出的数据行数（用它读"界面真的更新了没有"）。 */
function renderedRows(wrapper: { get: (s: string) => { findAll: (s: string) => unknown[] } }): number {
  return wrapper.get('[data-testid="data-table"]').findAll('tbody tr').length
}

beforeEach(() => {
  __resetClientState()
  __resetMetaForTest()
  __resetAgentWriteSignal()
  __setCsrfTokenForTest('csrf-test')
  vi.unstubAllGlobals()
})

describe('executedResourcesOf：判据只认服务端的 executed', () => {
  it('executed + data.executed 多条 → 取回全部资源（一次对话可能写多次）', () => {
    const resources = executedResourcesOf({
      kind: 'executed',
      result: {
        resource: 'batch',
        data: {
          executed: [{ resource: 'partner' }, { resource: 'batch' }],
        },
      },
    })
    expect(resources).toEqual(['partner', 'batch'])
  })

  it('executed 且没有 data.executed → 退回顶层单条资源', () => {
    expect(executedResourcesOf({ kind: 'executed', result: { resource: 'pond' } })).toEqual(['pond'])
  })

  it('executed 但资源解析不出来 → 返回空数组（不编造资源名）', () => {
    expect(executedResourcesOf({ kind: 'executed', result: {} })).toEqual([])
  })

  it.each([
    ['assistant', '我只是回答了一下'],
    ['clarification', '信息不足，反问'],
    ['confirmation_required', '待确认（还没写）'],
    ['cancelled', '用户取消'],
  ])('%s **不是**写入：不得触发任何刷新', (kind) => {
    expect(executedResourcesOf({ kind, result: { resource: 'pond' } })).toEqual([])
  })
})

describe('ResourceListPage 自动刷新：智能体写入后不手动刷新也能看到新行', () => {
  it('写入的正是当前资源 → 自动重拉，界面出现新行', async () => {
    const { wrapper, lists, setRows } = await mountPage('pond')
    expect(renderedRows(wrapper)).toBe(1)
    const before = lists.length

    // 智能体真的写进了库（服务端多一行），然后 `AgentPanel` 收到
    // `kind === 'executed'`（resource=pond）并发布写入信号。
    setRows(2)
    publishAgentWrite(['pond'])
    await flushPromises()
    await flushPromises()

    // ◆ 两件事各自可断言：真的重新拉了一次，且界面**渲染出**了新行。
    expect(lists.length).toBe(before + 1)
    expect(renderedRows(wrapper)).toBe(2)
    expect(wrapper.text()).toContain('P-002')
  })

  it('**纯查询**（没有写入）→ 一次列表请求都不多发', async () => {
    const { wrapper, lists } = await mountPage('pond')
    const before = lists.length

    // 纯查询轮次：不存在 executed，所以根本没有 publish 调用
    await flushPromises()
    await flushPromises()

    expect(lists.length).toBe(before)
    expect(renderedRows(wrapper)).toBe(1)
  })

  it('写的是**别的**资源 → 不刷新（刷了也没用，数据在另一个页面）', async () => {
    const { wrapper, lists } = await mountPage('pond')
    const before = lists.length

    publishAgentWrite(['cost_entry'])
    await flushPromises()
    await flushPromises()

    expect(lists.length).toBe(before)
    expect(renderedRows(wrapper)).toBe(1)
    // 但那个资源自己是被标记过的 —— 它挂载时会拉到最新数据
    expect(isResourceDirty('cost_entry')).toBe(true)
  })

  it('同一资源被写入**两次** → 两次都刷新（版本号单调，不是"只生效一次"）', async () => {
    const { wrapper, lists, setRows } = await mountPage('pond')
    const before = lists.length

    setRows(2)
    publishAgentWrite(['pond'])
    await flushPromises()
    await flushPromises()
    const afterFirst = lists.length

    setRows(3)
    publishAgentWrite(['pond'])
    await flushPromises()
    await flushPromises()

    expect(afterFirst).toBe(before + 1)
    expect(lists.length).toBe(before + 2)
    // 第二次写入后界面是 3 行 —— 证明第二次也真的重拉了，
    // 而不是"只在第一轮生效"（那正是用布尔标记会掉的坑）。
    expect(renderedRows(wrapper)).toBe(3)
  })

  it('资源名大小写/空白不敏感，但不做前缀匹配（pond ≠ pond_status_change）', async () => {
    const { lists } = await mountPage('pond')
    const before = lists.length

    // 前缀匹配会把 pond_status_change 误判成 pond —— 真实并存的两个资源名
    publishAgentWrite(['pond_status_change'])
    await flushPromises()
    await flushPromises()
    expect(lists.length).toBe(before)

    // 大小写与空白不该影响判定
    publishAgentWrite([' POND '])
    await flushPromises()
    await flushPromises()
    expect(lists.length).toBe(before + 1)
  })
})
