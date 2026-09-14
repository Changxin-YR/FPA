import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import App from '../src/App.vue'
import { mountFpaApp } from '../src/bootstrap'
import DataTable from '../src/layers/common/ui/DataTable.vue'
import DynamicForm from '../src/layers/common/ui/DynamicForm.vue'
import RecordActions from '../src/layers/common/ui/RecordActions.vue'
import { apiUrl } from '../src/layers/common/api/base'
import { toneForCell } from '../src/layers/common/meta/meta.store'
import { __resetMetaForTest, loadMeta } from '../src/layers/common/meta/meta.store'
import { __resetClientState } from '../src/layers/common/api/client'
import { __setCsrfTokenForTest } from '../src/layers/common/security/csrf'
import { createSessionStore } from '../src/layers/common/session/session.store'
import type { ResourceMeta } from '../src/layers/common/types.gen'

beforeEach(() => {
  __resetClientState()
  __resetMetaForTest()
  __setCsrfTokenForTest('csrf-test')
  vi.unstubAllGlobals()
})

describe('App.vue', () => {
  it('渲染当前路由对应的页面组件', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/probe', component: { template: '<p data-testid="probe">探针页面</p>' } }],
    })
    await router.push('/probe')
    await router.isReady()

    const wrapper = mount(App, { global: { plugins: [router] } })
    expect(wrapper.get('[data-testid="probe"]').text()).toBe('探针页面')
  })
})

describe('apiUrl 的三种形态', () => {
  it('同源相对路径', () => {
    expect(apiUrl('/api/v1/ponds')).toBe('/api/v1/ponds')
  })

  it('显式跨源基址拼接，且去掉尾斜杠', () => {
    expect(apiUrl('/api/v1/auth/me', 'https://fpa.example.com/')).toBe(
      'https://fpa.example.com/api/v1/auth/me',
    )
  })

  it('已经是绝对 URL 时原样返回（不再拼基址）', () => {
    expect(apiUrl('https://other.example.com/status', 'https://fpa.example.com')).toBe(
      'https://other.example.com/status',
    )
  })

  it('路径缺少前导斜杠时补齐', () => {
    expect(apiUrl('api/v1/ponds', '')).toBe('/api/v1/ponds')
  })
})

describe('DataTable 暴露的辅助函数', () => {
  const RESOURCE: ResourceMeta = {
    name: 'pond',
    title: '塘口',
    list_path: '/api/v1/ponds',
    columns: [
      { key: 'code', label: '塘口编号' },
      { key: 'status_label', label: '状态', tone_key: 'status' },
    ],
    status_dict: [
      { value: 'farming', label: '养殖中', tone: 'success' },
      { value: 'build', label: '待建设', tone: 'neutral' },
    ],
  }

  function stubMeta() {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              code: 'OK',
              message: '',
              data: { capabilities: [], resources: [RESOURCE] },
              request_id: 'r',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        ),
      ),
    )
  }

  it('toneClass 返回 tone-<name> 形式，未知 tone 兜底为 neutral', async () => {
    stubMeta()
    await loadMeta()
    const wrapper = mount(DataTable, {
      props: { resource: 'pond', columns: RESOURCE.columns, rows: [] },
    })
    const vm = wrapper.vm as unknown as {
      toneClass: (c: { key: string; label: string; tone_key?: string }, r: Record<string, unknown>) => string
    }
    expect(vm.toneClass(RESOURCE.columns[1], { status: 'farming' })).toBe('tone-success')
    expect(vm.toneClass(RESOURCE.columns[1], { status: 'nope' })).toBe('tone-neutral')
  })

  it('cellText 对空值显示破折号，对状态列显示中文标签', async () => {
    stubMeta()
    await loadMeta()
    const wrapper = mount(DataTable, {
      props: { resource: 'pond', columns: RESOURCE.columns, rows: [] },
    })
    const vm = wrapper.vm as unknown as {
      cellText: (c: { key: string; label: string; tone_key?: string }, r: Record<string, unknown>) => string
    }
    expect(vm.cellText(RESOURCE.columns[0], { code: '' })).toBe('—')
    expect(vm.cellText(RESOURCE.columns[0], { code: null })).toBe('—')
    expect(vm.cellText(RESOURCE.columns[1], { status: 'farming', status_label: '养殖中' })).toBe('养殖中')
    expect(vm.cellText(RESOURCE.columns[0], { code: 'P-001' })).toBe('P-001')
  })
})

describe('DynamicForm 暴露的辅助函数', () => {
  const FIELDS = [
    { key: 'code', label: '编号', type: 'string' as const, required: false },
    { key: 'qty', label: '数量', type: 'integer' as const, required: false },
  ]

  it('setValues + payload 按类型转换', () => {
    const wrapper = mount(DynamicForm, { props: { fields: FIELDS } })
    const vm = wrapper.vm as unknown as {
      setValues: (v: Record<string, unknown>) => void
      payload: () => Record<string, unknown>
      validate: () => boolean
    }
    vm.setValues({ code: 'P-1', qty: '12' })
    expect(vm.validate()).toBe(true)
    expect(vm.payload()).toEqual({ code: 'P-1', qty: 12 })
  })

  it('reset 恢复初始值', () => {
    const wrapper = mount(DynamicForm, { props: { fields: FIELDS, initial: { code: 'INIT' } } })
    const vm = wrapper.vm as unknown as {
      setValues: (v: Record<string, unknown>) => void
      reset: () => void
      values: Record<string, unknown>
    }
    vm.setValues({ code: 'CHANGED' })
    expect(vm.values.code).toBe('CHANGED')
    vm.reset()
    expect(vm.values.code).toBe('INIT')
  })
})

describe('RecordActions 的中文映射（来自服务端，不是前端表）', () => {
  /**
   * 这一组在 t7 之后**换了被测对象**，说明值得写下来：
   *
   * 原先它遍历一张 16 项的硬编码表，断言 `RecordActions.vue` 的 `labelOf` 能翻译它们。
   * 那条断言**恰恰是在保护"第二处描述"**——本仓的纪律是标签只有一处
   * （`kernel/workflow.py::ACTION_LABELS`，经 `/meta/capabilities` 下发），
   * 所以那张前端表已被删除，测试改为断言**服务端下发的标签被正确渲染**。
   */
  const ACTIONS = {
    row_actions: ['view', 'edit', 'submit', 'verify', 'archive'],
    row_action_labels: {
      view: '查看',
      edit: '编辑',
      submit: '提交',
      verify: '核验',
      archive: '归档',
    },
  }

  function stubMeta(actions: unknown): void {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              code: 'OK',
              message: '',
              request_id: 'r',
              data: { capabilities: [], resources: [], actions },
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        ),
      ),
    )
  }

  it('渲染服务端下发的标签（含 archive，它原先会渲染成裸 token）', async () => {
    stubMeta(ACTIONS)
    await loadMeta()
    const wrapper = mount(RecordActions, { props: { actions: ['view', 'archive'] } })
    expect(wrapper.findAll('button').map((node) => node.text())).toEqual(['查看', '归档'])
  })

  it('labelOf 用服务端表；未登记动作回退原词（可诊断）', async () => {
    stubMeta(ACTIONS)
    await loadMeta()
    const wrapper = mount(RecordActions, { props: { actions: ['view'] } })
    const vm = wrapper.vm as unknown as { labelOf: (action: string) => string }
    expect(vm.labelOf('verify')).toBe('核验')
    expect(vm.labelOf('unknown_action')).toBe('unknown_action')
  })

  it('服务端没给 actions 段时回退成原词，而不是崩或显示空', async () => {
    stubMeta(undefined)
    await loadMeta()
    const wrapper = mount(RecordActions, { props: { actions: ['view'] } })
    expect(wrapper.get('button').text()).toBe('view')
  })
})

describe('meta.store 的容错', () => {
  it('并发 loadMeta 只发一次请求', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            code: 'OK',
            message: '',
            data: { capabilities: [], resources: [] },
            request_id: 'r',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await Promise.all([loadMeta(), loadMeta(), loadMeta()])
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('加载成功后二次调用不再请求', async () => {
    __resetMetaForTest()
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            code: 'OK',
            message: '',
            data: { capabilities: [], resources: [] },
            request_id: 'r',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await loadMeta()
    await loadMeta()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('toneForCell 在未加载元数据时返回 neutral，不抛异常', () => {
    expect(toneForCell('pond', 'status', { status: 'farming' })).toBe('neutral')
  })
})

describe('session.store 的 load()', () => {
  it('成功时写入用户', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              code: 'OK',
              message: '',
              data: { user: { id: 1, name: '管理员', roles: [], permissions: ['pond.view'] } },
              request_id: 'r',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        ),
      ),
    )
    const session = createSessionStore()
    const user = await session.load()
    expect(user?.name).toBe('管理员')
    expect(session.user.value?.id).toBe(1)
  })

  it('UNAUTHENTICATED 时清空会话', async () => {
    const session = createSessionStore()
    session.setUser({ id: 9, name: '旧用户', roles: [], permissions: [] })

    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              code: 'UNAUTHENTICATED',
              message: '登录状态已失效',
              data: null,
              request_id: 'r',
            }),
            {
              status: 401,
              headers: { 'Content-Type': 'application/json' },
            },
          ),
        ),
      ),
    )

    await session.load()
    expect(session.user.value).toBeNull()
  })

  it('500 时保留会话（避免后端抖动把用户踢下线）', async () => {
    const session = createSessionStore()
    session.setUser({ id: 9, name: '在岗用户', roles: [], permissions: [] })

    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({ code: 'INTERNAL_ERROR', message: 'boom', data: null, request_id: 'r' }),
            {
              status: 500,
              headers: { 'Content-Type': 'application/json' },
            },
          ),
        ),
      ),
    )

    const user = await session.load()
    expect(user).toBeNull()
    // 关键：会话没有被清掉
    expect(session.user.value?.name).toBe('在岗用户')
  })
})

describe('csrf', () => {
  it('同一时刻只拉一次令牌，并缓存复用', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ code: 'OK', data: { csrf_token: 'tok' }, request_id: 'r' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const { clearCsrfToken, getCsrfToken } = await import('../src/layers/common/security/csrf')
    clearCsrfToken()
    const [a, b] = await Promise.all([getCsrfToken(), getCsrfToken()])
    expect(a).toBe('tok')
    expect(b).toBe('tok')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('令牌缺失时抛中文错误', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ code: 'OK', data: {}, request_id: 'r' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        ),
      ),
    )
    const { clearCsrfToken, getCsrfToken } = await import('../src/layers/common/security/csrf')
    clearCsrfToken()
    await expect(getCsrfToken()).rejects.toThrow('CSRF Token 获取失败')
  })
})

describe('bootstrap 装配', () => {
  it('mountFpaApp 把应用挂到给定选择器上（含真实 router）', async () => {
    // router 的初始导航需要可解析的响应，避免 jsdom 里的重定向告警
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              code: 'UNAUTHENTICATED',
              message: '登录状态已失效',
              data: null,
              request_id: 'r',
            }),
            { status: 401, headers: { 'Content-Type': 'application/json' } },
          ),
        ),
      ),
    )
    document.body.innerHTML = '<div id="app"></div><div id="probe-root"></div>'
    const app = await mountFpaApp('#probe-root')
    await flushPromises()
    expect(document.querySelector('#probe-root')?.innerHTML).not.toBe('')
    app.unmount()
  })
})
