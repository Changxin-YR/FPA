import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AgentPanel from '../src/layers/common/ui/AgentPanel.vue'
import DynamicForm from '../src/layers/common/ui/DynamicForm.vue'
import ResourceListPage from '../src/layers/common/ui/ResourceListPage.vue'
import { api, __resetClientState } from '../src/layers/common/api/client'
import { __setCsrfTokenForTest } from '../src/layers/common/security/csrf'
import {
  __resetMetaForTest,
  loadMeta,
  reloadMeta,
  resourceByName,
  resourceMetas,
  useMeta,
} from '../src/layers/common/meta/meta.store'
import type { CapabilityField, ResourceMeta } from '../src/layers/common/types.gen'

/**
 * 交互路径测试。
 *
 * 前面的用例大多通过 `setValues()` 之类程序化入口驱动组件；这一组走**真实用户交互**
 * （输入、点选、提交表单），覆盖 v-model 与按钮点击这条链路——
 * 早期版本的教训是「组件有测试但用户路径没测」：`DataTablePage` 的筛选控件在
 * `serverSide` 模式下"什么都不发生"，而测试全绿（.local/recon-frontend.md 问题 P-14）。
 */

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

beforeEach(() => {
  __resetClientState()
  __resetMetaForTest()
  __setCsrfTokenForTest('csrf-test')
  vi.unstubAllGlobals()
})

describe('client 的其余方法都带幂等键', () => {
  it.each(['put', 'patch', 'delete'] as const)('%s 发出请求并携带 Idempotency-Key', async (method) => {
    const calls: { url: string; init?: RequestInit }[] = []
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init })
      return Promise.resolve(ok({ id: 1 }))
    })

    await api[method]('/api/v1/ponds/1', { code: 'P-1' })
    const write = calls.find((call) => call.init?.method === method.toUpperCase())
    expect(write).toBeDefined()
    expect(new Headers(write?.init?.headers).get('Idempotency-Key')).toBeTruthy()
    expect(new Headers(write?.init?.headers).get('X-CSRF-Token')).toBe('csrf-test')
  })
})

describe('meta.store 的同步访问器与强制刷新', () => {
  const RESOURCES: ResourceMeta[] = [
    {
      name: 'pond',
      title: '塘口',
      list_path: '/api/v1/ponds',
      columns: [{ key: 'code', label: '塘口编号' }],
      status_dict: [{ value: 'farming', label: '养殖中', tone: 'success' }],
    },
  ]

  function stubMeta() {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(ok({ capabilities: [], resources: RESOURCES }))),
    )
  }

  it('loadMeta 之后 resourceMetas / resourceByName 可同步取值', async () => {
    stubMeta()
    await loadMeta()
    expect(resourceMetas()).toHaveLength(1)
    expect(resourceByName('pond')?.title).toBe('塘口')
    expect(resourceByName('nope')).toBeUndefined()
  })

  it('reloadMeta 强制重新拉取', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(ok({ capabilities: [], resources: RESOURCES })))
    vi.stubGlobal('fetch', fetchMock)

    await loadMeta()
    expect(fetchMock).toHaveBeenCalledTimes(1)

    await reloadMeta()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(resourceMetas()).toHaveLength(1)
  })

  it('加载失败时 error 暴露中文消息，且不影响后续重试', async () => {
    let attempt = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(() => {
        attempt += 1
        if (attempt === 1) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                code: 'INTERNAL_ERROR',
                message: '元数据服务不可用',
                data: null,
                request_id: 'r',
              }),
              {
                status: 500,
                headers: { 'Content-Type': 'application/json' },
              },
            ),
          )
        }
        return Promise.resolve(ok({ capabilities: [], resources: RESOURCES }))
      }),
    )

    await expect(loadMeta()).rejects.toBeTruthy()
    // 5xx 一律只给「服务器暂时无法处理请求（request_id）」——服务端 message 不外泄，
    // 这是 client.ts 的既定契约（errorMessageOf），不是丢信息
    expect(useMeta().error.value).toContain('服务器暂时无法处理请求')

    await loadMeta()
    expect(useMeta().error.value).toBe('')
    expect(resourceMetas()).toHaveLength(1)
  })
})

describe('DynamicForm：真实输入路径', () => {
  const FIELDS: CapabilityField[] = [
    { key: 'code', label: '编号', type: 'string', required: false },
    { key: 'note', label: '备注', type: 'text', required: false },
    { key: 'qty', label: '数量', type: 'number', required: false },
    { key: 'flag', label: '启用', type: 'boolean', required: false },
    {
      key: 'status',
      label: '状态',
      type: 'enum',
      required: false,
      choices: [
        { value: 'a', label: '甲' },
        { value: 'b', label: '乙' },
      ],
    },
  ]

  it('输入、多行、数字、复选框、下拉都会写回表单值', async () => {
    const wrapper = mount(DynamicForm, { props: { fields: FIELDS } })
    await wrapper.get('#field-code').setValue('P-001')
    await wrapper.get('#field-note').setValue('多行备注')
    await wrapper.get('#field-qty').setValue('7.5')
    await wrapper.get('#field-flag').setValue(true)
    await wrapper.get('#field-status').setValue('b')

    const vm = wrapper.vm as unknown as { payload: () => Record<string, unknown> }
    expect(vm.payload()).toEqual({
      code: 'P-001',
      note: '多行备注',
      qty: 7.5,
      flag: true,
      status: 'b',
    })
  })

  it('清空必填字段后提交被拦下', async () => {
    const wrapper = mount(DynamicForm, {
      props: { fields: [{ key: 'code', label: '编号', type: 'string', required: true }] },
    })
    await wrapper.get('#field-code').setValue('临时值')
    await wrapper.get('#field-code').setValue('')
    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('submit')).toBeUndefined()
    expect(wrapper.get('.dynamic-form__error').text()).toContain('请填写编号')
  })

  it('date / datetime 输入也能写回', async () => {
    const wrapper = mount(DynamicForm, {
      props: {
        fields: [
          { key: 'd', label: '日期', type: 'date', required: false },
          { key: 't', label: '时间', type: 'datetime', required: false },
        ],
      },
    })
    await wrapper.get('#field-d').setValue('2026-09-12')
    await wrapper.get('#field-t').setValue('2026-09-12T10:30')

    const vm = wrapper.vm as unknown as { payload: () => Record<string, unknown> }
    expect(vm.payload()).toEqual({ d: '2026-09-12', t: '2026-09-12T10:30' })
  })
})

describe('AgentPanel：真实交互路径', () => {
  function stubStream(result: object) {
    const bodies: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.endsWith('/auth/csrf')) return Promise.resolve(csrfOk())
        bodies.push(String(init?.body ?? ''))
        const encoder = new TextEncoder()
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode(`${JSON.stringify({ type: 'result', result })}\n`))
            controller.close()
          },
        })
        return Promise.resolve(new Response(stream, { status: 200 }))
      }),
    )
    return bodies
  }

  it('通过输入框 + 发送按钮提交', async () => {
    stubStream({ kind: 'assistant', conversation_id: 'c1', message: '收到' })
    const wrapper = mount(AgentPanel, { props: { pageContext: '/ponds' } })
    await wrapper.get('.agent-widget__launcher').trigger('click')
    await wrapper.get('[data-testid="agent-input"]').setValue('查一下 3 号塘')
    await wrapper.get('[data-testid="agent-composer"]').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('查一下 3 号塘')
    expect(wrapper.text()).toContain('收到')
  })

  it('澄清问题点选项后原样作为下一轮消息发出', async () => {
    const bodies: string[] = []
    let round = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.endsWith('/auth/csrf')) return Promise.resolve(csrfOk())
        bodies.push(String(init?.body ?? ''))
        round += 1
        const payload =
          round === 1
            ? {
                type: 'result',
                result: {
                  kind: 'clarification',
                  conversation_id: 'c1',
                  question: '请确认使用哪个物料：',
                  options: ['1号饲料', '2号饲料'],
                  allow_free_text: true,
                },
              }
            : { type: 'result', result: { kind: 'assistant', conversation_id: 'c1', message: '好的' } }
        const encoder = new TextEncoder()
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode(`${JSON.stringify(payload)}\n`))
            controller.close()
          },
        })
        return Promise.resolve(new Response(stream, { status: 200 }))
      }),
    )

    const wrapper = mount(AgentPanel, { props: { pageContext: '/ponds' } })
    await wrapper.get('.agent-widget__launcher').trigger('click')
    await wrapper.get('[data-testid="agent-input"]').setValue('投喂')
    await wrapper.get('[data-testid="agent-composer"]').trigger('submit')
    await flushPromises()

    const option = wrapper.findAll('[data-testid="agent-clarification-option"]')[1]
    await option.trigger('click')
    await flushPromises()

    expect(bodies).toHaveLength(2)
    expect(JSON.parse(bodies[1]).message).toBe('2号饲料')
    expect(wrapper.text()).toContain('好的')
  })

  it('澄清问题用自由文本提交', async () => {
    const bodies: string[] = []
    let round = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.endsWith('/auth/csrf')) return Promise.resolve(csrfOk())
        bodies.push(String(init?.body ?? ''))
        round += 1
        const payload =
          round === 1
            ? {
                type: 'result',
                result: {
                  kind: 'clarification',
                  conversation_id: 'c1',
                  question: '请确认使用哪个物料：',
                  options: [],
                  allow_free_text: true,
                },
              }
            : { type: 'result', result: { kind: 'assistant', conversation_id: 'c1', message: '已记录' } }
        const encoder = new TextEncoder()
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode(`${JSON.stringify(payload)}\n`))
            controller.close()
          },
        })
        return Promise.resolve(new Response(stream, { status: 200 }))
      }),
    )

    const wrapper = mount(AgentPanel, { props: { pageContext: '/ponds' } })
    await wrapper.get('.agent-widget__launcher').trigger('click')
    await wrapper.get('[data-testid="agent-input"]').setValue('投喂')
    await wrapper.get('[data-testid="agent-composer"]').trigger('submit')
    await flushPromises()

    const input = wrapper.find('[data-testid="agent-clarification-input"]')
    expect(input.exists()).toBe(true)
    await input.setValue('用 1 号料')
    await flushPromises()
    const form = wrapper.get('[data-testid="agent-clarification-form"]')
    await form.trigger('submit')
    await flushPromises()

    expect(bodies).toHaveLength(2)
    expect(JSON.parse(bodies[1]).message).toBe('用 1 号料')
  })

  it('空输入不提交', async () => {
    const bodies = stubStream({ kind: 'assistant', conversation_id: 'c1', message: 'x' })
    const wrapper = mount(AgentPanel, { props: { pageContext: '/ponds' } })
    await wrapper.get('.agent-widget__launcher').trigger('click')
    await wrapper.get('[data-testid="agent-input"]').setValue('   ')
    await wrapper.get('[data-testid="agent-composer"]').trigger('submit')
    await flushPromises()

    expect(bodies).toHaveLength(0)
  })
})

describe('PondListPage：真实提交新建表单', () => {
  const META = {
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

  it('填写表单提交后调用能力声明的 path 与 method，并重新拉取列表', async () => {
    const writes: { url: string; method?: string; body?: string }[] = []
    let listCalls = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.endsWith('/auth/csrf')) return Promise.resolve(csrfOk())
        if (url.startsWith('/api/v1/meta/capabilities')) return Promise.resolve(ok(META))
        if (init?.method === 'POST') {
          writes.push({ url, method: init.method, body: String(init.body ?? '') })
          return Promise.resolve(ok({ record: { id: 1 } }))
        }
        listCalls += 1
        return Promise.resolve(ok({ items: [], page: 1, page_size: 20, total: 0, has_next: false }))
      }),
    )

    const wrapper = mount(ResourceListPage, {
      props: { resource: 'pond' },
      global: { stubs: { Teleport: true } },
    })
    await flushPromises()
    await flushPromises()

    await wrapper.get('[data-testid="page-create"]').trigger('click')
    await wrapper.get('[data-testid="page-dialog"] input#field-code').setValue('P-100')
    await wrapper.get('[data-testid="page-dialog"] form').trigger('submit')
    await flushPromises()
    await flushPromises()

    expect(writes).toHaveLength(1)
    expect(writes[0].url).toBe('/api/v1/ponds')
    expect(writes[0].method).toBe('POST')
    expect(JSON.parse(writes[0].body ?? '{}')).toEqual({ code: 'P-100' })
    // 新建成功后表单关闭并刷新列表
    expect(wrapper.find('[data-testid="page-dialog"]').exists()).toBe(false)
    expect(listCalls).toBeGreaterThanOrEqual(2)
  })
})
