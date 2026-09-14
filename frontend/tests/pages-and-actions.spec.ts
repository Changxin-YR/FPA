import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import LoginPage from '../src/layers/product/auth/LoginPage.vue'
import ResourceListPage from '../src/layers/common/ui/ResourceListPage.vue'
import RecordActions from '../src/layers/common/ui/RecordActions.vue'
import { __resetClientState } from '../src/layers/common/api/client'
import { __setCsrfTokenForTest } from '../src/layers/common/security/csrf'
import { __resetMetaForTest, loadMeta } from '../src/layers/common/meta/meta.store'
import { createSessionStore } from '../src/layers/common/session/session.store'
import { conflictVersion, isScopeUnresolved } from '../src/layers/common/api/errors'
import { ApiError } from '../src/layers/common/api/errors'
import { hasAllPermissions, hasAnyPermission } from '../src/layers/common/security/access-control'
import type { Capability, ResourceMeta } from '../src/layers/common/types.gen'

const META_PAYLOAD: { capabilities: Capability[]; resources: ResourceMeta[] } = {
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
      description: '新建塘口',
      resource: 'pond',
      fields: [
        { key: 'code', label: '塘口编号', type: 'string', required: true, max_length: 64 },
        {
          key: 'area_id',
          label: '所属区域',
          type: 'ref',
          required: true,
          ref: { resource: 'area', label_key: 'name' },
        },
      ],
      path_parameters: [],
      row_actions: ['view', 'edit', 'delete', 'submit', 'verify'],
    },
  ],
  resources: [
    {
      name: 'pond',
      title: '塘口',
      list_path: '/api/v1/ponds',
      columns: [
        { key: 'code', label: '塘口编号' },
        { key: 'status_label', label: '状态', tone_key: 'status' },
      ],
      status_dict: [{ value: 'farming', label: '养殖中', tone: 'success' }],
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

function fail(status: number, code: string, message: string, data: unknown = null): Response {
  return new Response(JSON.stringify({ code, message, data, request_id: 'r-err' }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function stubFetch(handler: (url: string, init?: RequestInit) => Response) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/auth/csrf')) return Promise.resolve(csrfOk())
      return Promise.resolve(handler(url, init))
    }),
  )
}

/** 两个页面都用了 useRouter()（PondListPage 内嵌 AgentPanel，登录页要跳转），因此挂载时必须提供 router。 */
function memoryRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', redirect: '/ponds' },
      { path: '/ponds', component: { template: '<div />' } },
      { path: '/auth/login', component: { template: '<div />' } },
      { path: '/:pathMatch(.*)*', component: { template: '<div />' } },
    ],
  })
}
beforeEach(() => {
  __resetClientState()
  __resetMetaForTest()
  __setCsrfTokenForTest('csrf-test')
  vi.unstubAllGlobals()
})

describe('access-control 的批量判断', () => {
  const user = { permissions: ['pond.view', 'pond.create'] }

  it('hasAnyPermission 命中其一即为真', () => {
    expect(hasAnyPermission(user, ['nope', 'pond.view'])).toBe(true)
    expect(hasAnyPermission(user, ['nope', 'other'])).toBe(false)
    expect(hasAnyPermission(null, ['pond.view'])).toBe(false)
  })

  it('hasAllPermissions 需要全部命中', () => {
    expect(hasAllPermissions(user, ['pond.view', 'pond.create'])).toBe(true)
    expect(hasAllPermissions(user, ['pond.view', 'pond.delete'])).toBe(false)
    expect(hasAllPermissions(undefined, [])).toBe(true)
  })
})

describe('errors 的领域辅助函数', () => {
  it('isScopeUnresolved 识别 fail-closed 数据范围错误', () => {
    const error = new ApiError('DATA_SCOPE_UNRESOLVED', '当前账号的数据范围无法解析，已拒绝本次访问', 403)
    expect(isScopeUnresolved(error)).toBe(true)
    expect(isScopeUnresolved(new ApiError('DATA_SCOPE_DENIED', 'x', 403))).toBe(false)
    expect(isScopeUnresolved(new Error('x'))).toBe(false)
  })

  it('conflictVersion 从 data.current_version 取版本号', () => {
    const error = new ApiError('VERSION_CONFLICT', '该记录已被他人修改，请刷新后重试', 409, 'r', {
      current_version: 7,
    })
    expect(conflictVersion(error)).toBe(7)
  })

  it('conflictVersion 对非冲突错误与缺字段返回 null', () => {
    expect(conflictVersion(new ApiError('FORBIDDEN', 'x', 403))).toBeNull()
    expect(conflictVersion(new ApiError('CONFLICT', 'x', 409, 'r', { other: 1 }))).toBeNull()
    expect(conflictVersion(new ApiError('CONFLICT', 'x', 409, 'r', null))).toBeNull()
    expect(conflictVersion(new Error('x'))).toBeNull()
  })
})

/**
 * 标签来自**服务端**（`/meta/capabilities` 的 `actions.row_action_labels`），
 * 而不是前端硬编码的表。所以这一组先 stub 元数据。
 *
 * 为什么值得单独说明：这里原先断言的是
 * `RecordActions.vue` 自带的 `ACTION_LABELS`，而同一个仓库里
 * `DataTable.vue` 另一处直接渲染 token —— **两套口径**。
 * 现在只剩一处，测试钉的就是那一处。
 */
function stubActionsMeta(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            code: 'OK',
            message: '',
            request_id: 'r',
            data: {
              capabilities: [],
              resources: [],
              actions: {
                row_actions: ['view', 'edit', 'verify', 'submit'],
                row_action_labels: { view: '查看', edit: '编辑', verify: '核验', submit: '提交' },
              },
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    ),
  )
}

describe('RecordActions：只渲染服务端给的动作', () => {
  it('动作名映射为中文（标签由服务端下发）', async () => {
    stubActionsMeta()
    await loadMeta()
    const wrapper = mount(RecordActions, { props: { actions: ['view', 'edit', 'verify'] } })
    const labels = wrapper.findAll('button').map((node) => node.text())
    expect(labels).toEqual(['查看', '编辑', '核验'])
  })

  it('未登记的动作名原样显示（不隐藏，也不猜）', async () => {
    stubActionsMeta()
    await loadMeta()
    const wrapper = mount(RecordActions, { props: { actions: ['teleport'] } })
    expect(wrapper.get('button').text()).toBe('teleport')
  })

  it('点击后短暂锁定，防止双击重复提交', async () => {
    const wrapper = mount(RecordActions, { props: { actions: ['submit'] } })
    const button = wrapper.get('button')
    await button.trigger('click')
    await button.trigger('click')
    expect(wrapper.emitted('action')).toHaveLength(1)
    expect(button.attributes('disabled')).toBeDefined()
  })

  it('actions 变化时解锁', async () => {
    const wrapper = mount(RecordActions, { props: { actions: ['submit'] } })
    await wrapper.get('button').trigger('click')
    await wrapper.setProps({ actions: ['verify'] })
    await flushPromises()
    expect(wrapper.get('button').attributes('disabled')).toBeUndefined()
  })

  it('危险动作用 danger 变体', () => {
    const wrapper = mount(RecordActions, { props: { actions: ['delete'] } })
    expect(wrapper.get('button').attributes('class')).toContain('action-button--danger')
  })
})

describe('LoginPage', () => {
  it('表单下方标注演示账号的账号与口令', () => {
    const wrapper = mount(LoginPage, { global: { plugins: [memoryRouter()] } })

    expect(wrapper.get('[data-testid="login-demo"]').text()).toContain('演示账号')
    expect(wrapper.get('[data-testid="login-demo-account"]').text()).toBe('demo')
    expect(wrapper.get('[data-testid="login-demo-password"]').text()).toBe('Demo1234!')
  })

  it('演示账号取值跟着 tools/seed_acceptance.py 走（页面不是第二来源）', () => {
    const seed = readFileSync(join(process.cwd(), '..', 'tools', 'seed_acceptance.py'), 'utf-8')
    const demo = /\(\s*9204,\s*"([^"]+)",\s*"[^"]+",\s*"([^"]+)",/.exec(seed)
    if (!demo) throw new Error('tools/seed_acceptance.py 里找不到 9204 这条账号定义')

    const pagePath = join(process.cwd(), 'src', 'layers', 'product', 'auth', 'LoginPage.vue')
    const page = readFileSync(pagePath, 'utf-8')
    expect(page).toContain(`data-testid="login-demo-account">${demo[1]}<`)
    expect(page).toContain(`data-testid="login-demo-password">${demo[2]}<`)
  })

  it('缺少账号或密码时给出中文提示，且不发请求', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(LoginPage, { global: { plugins: [memoryRouter()] } })

    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="login-error"]').text()).toBe('请输入账号和密码')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('登录成功后写入会话', async () => {
    stubFetch(() =>
      ok({ user: { id: 1, name: '管理员', roles: ['super_admin'], permissions: ['pond.view'] } }),
    )

    const wrapper = mount(LoginPage, { global: { plugins: [memoryRouter()] } })
    await wrapper.get('[data-testid="login-identifier"]').setValue('admin')
    await wrapper.get('[data-testid="login-password"]').setValue('secret')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(createSessionStore().user.value?.name).toBe('管理员')
  })

  it('登录失败时显示后端中文消息', async () => {
    stubFetch(() => fail(401, 'UNAUTHENTICATED', '账号或密码错误'))

    const wrapper = mount(LoginPage, { global: { plugins: [memoryRouter()] } })
    await wrapper.get('[data-testid="login-identifier"]').setValue('admin')
    await wrapper.get('[data-testid="login-password"]').setValue('wrong')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="login-error"]').text()).toContain('账号或密码错误')
  })
})

describe('PondListPage：元数据驱动的列表页', () => {
  it('用 list_path 拉列表，并用 status_dict 渲染状态中文', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/v1/meta/capabilities')) return ok(META_PAYLOAD)
      if (url.startsWith('/api/v1/ponds')) {
        return ok({
          items: [
            { id: 1, code: 'P-001', status_label: '养殖中', status: 'farming', allowed_actions: ['view'] },
          ],
          page: 1,
          page_size: 20,
          total: 1,
          has_next: false,
        })
      }
      return ok({})
    })

    const wrapper = mount(ResourceListPage, {
      props: { resource: 'pond' },
      global: { plugins: [memoryRouter()], stubs: { Teleport: true } },
    })
    await flushPromises()
    await flushPromises()

    expect(wrapper.text()).toContain('塘口')
    expect(wrapper.text()).toContain('P-001')
    expect(wrapper.text()).toContain('养殖中')
  })

  it('元数据里没有该资源的 list_path 时显式报错，而不是渲染空表', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/v1/meta/capabilities')) {
        return ok({ capabilities: [], resources: [] })
      }
      return ok({})
    })

    const wrapper = mount(ResourceListPage, {
      props: { resource: 'pond' },
      global: { plugins: [memoryRouter()], stubs: { Teleport: true } },
    })
    await flushPromises()
    await flushPromises()

    expect(wrapper.get('[data-testid="page-error"]').text()).toContain('元数据中未登记该资源的列表接口')
  })

  it('列表加载失败时显示带场景前缀的中文错误', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/v1/meta/capabilities')) return ok(META_PAYLOAD)
      return fail(500, 'INTERNAL_ERROR', 'boom', null)
    })

    const wrapper = mount(ResourceListPage, {
      props: { resource: 'pond' },
      global: { plugins: [memoryRouter()], stubs: { Teleport: true } },
    })
    await flushPromises()
    await flushPromises()

    expect(wrapper.get('[data-testid="page-error"]').text()).toContain('塘口列表加载失败')
  })

  it('新建按钮来自 create 能力的 title，点开后表单字段来自能力声明', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/v1/meta/capabilities')) return ok(META_PAYLOAD)
      return ok({ items: [], page: 1, page_size: 20, total: 0, has_next: false })
    })

    const wrapper = mount(ResourceListPage, {
      props: { resource: 'pond' },
      global: { plugins: [memoryRouter()], stubs: { Teleport: true } },
    })
    await flushPromises()
    await flushPromises()

    const createButton = wrapper.get('[data-testid="page-create"]')
    expect(createButton.text()).toBe('新建塘口')

    await createButton.trigger('click')
    // 字段标签来自服务端，而不是页面里写死的
    expect(wrapper.get('[data-testid="page-dialog"]').text()).toContain('塘口编号')
    expect(wrapper.get('[data-testid="page-dialog"]').text()).toContain('所属区域')
  })

  it('行内动作在没有对应能力声明时被拒绝执行并提示', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/v1/meta/capabilities')) return ok(META_PAYLOAD)
      if (url.startsWith('/api/v1/ponds')) {
        return ok({
          items: [
            {
              id: 1,
              code: 'P-001',
              status_label: '养殖中',
              status: 'farming',
              allowed_actions: ['teleport'],
            },
          ],
          page: 1,
          page_size: 20,
          total: 1,
          has_next: false,
        })
      }
      return ok({})
    })

    const wrapper = mount(ResourceListPage, {
      props: { resource: 'pond' },
      global: { plugins: [memoryRouter()], stubs: { Teleport: true } },
    })
    await flushPromises()
    await flushPromises()

    await wrapper.get('.data-table__row-action').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="action-error"]').text()).toContain('服务端未登记动作')
  })

  it('有对应能力声明的行内动作：按能力 kind 映射到对应能力，并提交到它的 path/method', async () => {
    const writes: { url: string; method?: string }[] = []
    stubFetch((url, init) => {
      if (url.startsWith('/api/v1/meta/capabilities')) return ok(META_PAYLOAD)
      if (init?.method && init.method !== 'GET') {
        writes.push({ url, method: init.method })
        return ok({ record: { id: 1 } })
      }
      return ok({
        items: [
          { id: 1, code: 'P-001', status_label: '养殖中', status: 'farming', allowed_actions: ['create'] },
        ],
        page: 1,
        page_size: 20,
        total: 1,
        has_next: false,
      })
    })

    const wrapper = mount(ResourceListPage, {
      props: { resource: 'pond' },
      global: { plugins: [memoryRouter()], stubs: { Teleport: true } },
    })
    await flushPromises()
    await flushPromises()

    // `create` 是行内动作名，映射到该资源的 create 能力 —— 它需要采集字段，
    // 因此正确行为是**打开表单**，而不是立刻 POST（立刻 POST 会丢字段）。
    await wrapper.get('.data-table__row-action').trigger('click')
    await flushPromises()
    expect(writes).toHaveLength(0)
    expect(wrapper.find('[data-testid="page-dialog"]').exists()).toBe(true)

    // 必填字段没填时不应发出请求（DynamicForm 的校验）
    await wrapper.get('[data-testid="page-dialog"] form').trigger('submit')
    await flushPromises()
    expect(writes).toHaveLength(0)

    // 填齐能力声明的必填字段后提交，才按 path + method 发出请求
    const dialog = wrapper.get('[data-testid="page-dialog"]')
    await dialog.get('input#field-code').setValue('P-100')
    await dialog.get('select#field-area_id').setValue('1')
    await dialog.get('form').trigger('submit')
    await flushPromises()

    expect(writes).toHaveLength(1)
    expect(writes[0].url).toBe('/api/v1/ponds')
    expect(writes[0].method).toBe('POST')
  })
})
