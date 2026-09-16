import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AgentPanel from '../src/layers/common/ui/AgentPanel.vue'
import { __resetClientState } from '../src/layers/common/api/client'
import { __setCsrfTokenForTest } from '../src/layers/common/security/csrf'
import type { AgentConfirmation } from '../src/layers/common/agent/service'
import {
  __resetAgentWriteSignal,
  dirtyResources,
  writeVersion,
} from '../src/layers/common/agent/write-signal'

/**
 * 用例 5（任务说明 E.5）：AgentPanel 四种 kind 的渲染 + 确认卡片字段。
 *
 * 四种 kind 来自 INTERFACES.md §3：assistant / clarification /
 * confirmation_required / executed（外加 cancelled）。
 * 确认卡片必须显示 title / target / rows / impact / expires_at。
 */

const CONFIRMATION: AgentConfirmation = {
  id: 128,
  token: 'tok-once',
  capability: 'feeding.create',
  title: '登记投喂',
  target: '3 号塘 / 批次 B-2026-007',
  rows: [
    { label: '物料', value: '1号饲料' },
    { label: '数量', value: '50 kg' },
  ],
  impact: ['扣减 1 号仓 1号饲料 50kg', '计入批次 B-2026-007 投喂成本'],
  // 用一个**远期**时间：本文件另有专门的过期卡用例，普通用例不能被时钟影响。
  expires_at: '2099-01-01T00:00:00Z',
}

interface PanelVm {
  submitText: (text: string) => Promise<void>
  toggle: () => void
  busy: boolean
  open: boolean
  confirmation?: AgentConfirmation
  confirmations: AgentConfirmation[]
  confirm: (card: AgentConfirmation) => Promise<void>
  streamingText: string
}

interface FetchCall {
  url: string
  init?: RequestInit
}

function csrfOk(): Response {
  return new Response(JSON.stringify({ code: 'OK', data: { csrf_token: 'csrf-test' }, request_id: 'r0' }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function envelope(data: unknown): Response {
  return new Response(JSON.stringify({ code: 'OK', message: '', data, request_id: 'r1' }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function errorEnvelope(status: number, code: string, message: string): Response {
  return new Response(JSON.stringify({ code, message, data: null, request_id: 'r-err' }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** 构造一个只发给定 NDJSON 行的流式响应。 */
function ndjson(lines: object[]): Response {
  const encoder = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const line of lines) controller.enqueue(encoder.encode(`${JSON.stringify(line)}\n`))
      controller.close()
    },
  })
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'application/x-ndjson' } })
}

const resultLine = (result: object) => ({ type: 'result', result })

/** 挂载面板并打开它（面板是默认关闭的浮层）。 */
async function mountPanel(handler: (url: string, init?: RequestInit) => Response) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/auth/csrf')) return Promise.resolve(csrfOk())
      return Promise.resolve(handler(url, init))
    }),
  )
  const wrapper = mount(AgentPanel, { props: { pageContext: '/ponds' } })
  const vm = wrapper.vm as unknown as PanelVm
  vm.toggle()
  await flushPromises()
  return { wrapper, vm }
}

/** 常用组合：流式直接返回一条 result 行。 */
async function sendOne(result: object, message = '给 3 号塘投喂 50kg') {
  const mounted = await mountPanel(() => ndjson([resultLine(result)]))
  await mounted.vm.submitText(message)
  await flushPromises()
  return mounted
}

beforeEach(() => {
  __resetClientState()
  __resetAgentWriteSignal()
  __setCsrfTokenForTest('csrf-test')
  vi.unstubAllGlobals()
})

describe('AgentPanel：四种 kind 的渲染', () => {
  it('① assistant → 只渲染正文文本，不渲染确认卡片与澄清卡片', async () => {
    const { wrapper } = await sendOne({
      kind: 'assistant',
      conversation_id: 'c1',
      message: '3 号塘最近 7 天共投喂 210kg，日均 30kg。',
    })

    expect(wrapper.text()).toContain('3 号塘最近 7 天共投喂 210kg')
    expect(wrapper.find('[data-testid="agent-confirmation"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="agent-clarification"]').exists()).toBe(false)
  })

  it('② clarification → 渲染问题与选项，allow_free_text=true 时给出输入框', async () => {
    const { wrapper } = await sendOne({
      kind: 'clarification',
      conversation_id: 'c1',
      question: '请确认使用哪个物料：',
      options: ['1号饲料（鲤鱼配合饲料）', '2号饲料（对虾配合饲料）'],
      allow_free_text: true,
    })

    const card = wrapper.get('[data-testid="agent-clarification"]')
    expect(card.get('[data-testid="agent-clarification-question"]').text()).toBe('请确认使用哪个物料：')

    const options = card.findAll('[data-testid="agent-clarification-option"]').map((node) => node.text())
    expect(options).toEqual(['1号饲料（鲤鱼配合饲料）', '2号饲料（对虾配合饲料）'])
    expect(card.find('[data-testid="agent-clarification-input"]').exists()).toBe(true)
  })

  it('② clarification → allow_free_text=false 时不给输入框，只给选项', async () => {
    const { wrapper } = await sendOne({
      kind: 'clarification',
      conversation_id: 'c1',
      question: '请选择：',
      options: ['A', 'B'],
      allow_free_text: false,
    })

    const card = wrapper.get('[data-testid="agent-clarification"]')
    expect(card.find('[data-testid="agent-clarification-input"]').exists()).toBe(false)
    expect(card.findAll('[data-testid="agent-clarification-option"]')).toHaveLength(2)
  })

  it('② clarification → 可选字段 `message`（模型这一轮的话）不得丢', async () => {
    // 实测 `ask_user` 那轮模型补了「（可选补充：联系人、电话、地址、结算天数、信用额度。）」，
    // 那句话不在 `question` 里 —— 不显示它，用户就只看到一句被截短的问句。
    const { wrapper } = await sendOne({
      kind: 'clarification',
      conversation_id: 'c1',
      question: '请补充新建往来单位所需的信息：1）类型；2）单位名称；3）单位编号。',
      options: ['供应商', '客户'],
      allow_free_text: true,
      message: '新建往来单位需要先补充几项必要信息，麻烦告知：…',
    })

    expect(wrapper.find('[data-testid="agent-clarification"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('新建往来单位需要先补充几项必要信息')
  })

  it('③ confirmation_required → 渲染确认卡片，且不得显示为已完成', async () => {
    const { wrapper } = await sendOne({
      kind: 'confirmation_required',
      conversation_id: 'c1',
      message: '即将为 3 号塘登记投喂 50kg 1号饲料，确认后立即扣减库存并计入成本。',
      confirmation: CONFIRMATION,
    })

    expect(wrapper.find('[data-testid="agent-confirmation"]').exists()).toBe(true)
    // 协议约束：写操作只能由 result 行交付，待确认操作绝不能渲染成已完成
    expect(wrapper.text()).not.toContain('已完成')
    expect(wrapper.text()).not.toContain('已登记投喂')
  })

  it('④ executed → 渲染执行结果正文，不残留确认卡片', async () => {
    const { wrapper } = await sendOne({
      kind: 'executed',
      conversation_id: 'c1',
      message: '已登记投喂：3 号塘 50kg 1号饲料，库存剩 420kg，成本增加 ¥240.00。',
      result: { capability: 'feeding.create', resource_id: 991, url: '/feeding/logs' },
    })

    expect(wrapper.text()).toContain('已登记投喂')
    expect(wrapper.find('[data-testid="agent-confirmation"]').exists()).toBe(false)
  })

  it('④ executed + confirmations → 同时发布写入刷新并保留待确认卡片', async () => {
    const { wrapper } = await sendOne({
      kind: 'executed',
      conversation_id: 'c1',
      message: '已创建塘口，并准备停用一个区域。',
      result: {
        capability: 'pond.create',
        resource: 'pond',
        resource_id: 991,
        url: '/ponds',
      },
      confirmation: CONFIRMATION,
      confirmations: [CONFIRMATION],
    })

    expect(wrapper.text()).toContain('已创建塘口')
    expect(wrapper.findAll('[data-testid="agent-confirmation"]')).toHaveLength(1)
    expect(writeVersion.value).toBe(1)
    expect(dirtyResources.value).toEqual(['pond'])
  })

  it('cancelled → 追加一条系统消息', async () => {
    const { wrapper } = await sendOne({ kind: 'cancelled' })
    expect(wrapper.text()).toContain('已取消该操作')
  })
})

describe('AgentPanel：确认卡片必须显示 title / target / rows / impact / expires_at', () => {
  it('五个字段全部渲染，且 rows 是中文键值对', async () => {
    const { wrapper } = await sendOne({
      kind: 'confirmation_required',
      conversation_id: 'c1',
      message: '即将登记投喂',
      confirmation: CONFIRMATION,
    })

    const card = wrapper.get('[data-testid="agent-confirmation"]')
    expect(card.get('[data-testid="agent-confirmation-title"]').text()).toBe('登记投喂')
    expect(card.get('[data-testid="agent-confirmation-target"]').text()).toContain('3 号塘 / 批次 B-2026-007')

    const rows = card.findAll('[data-testid="agent-confirmation-rows"] dt').map((node) => node.text())
    const values = card.findAll('[data-testid="agent-confirmation-rows"] dd').map((node) => node.text())
    expect(rows).toEqual(['物料', '数量'])
    expect(values).toEqual(['1号饲料', '50 kg'])

    const impact = card.get('[data-testid="agent-confirmation-impact"]').text()
    expect(impact).toContain('扣减 1 号仓 1号饲料 50kg')
    expect(impact).toContain('计入批次 B-2026-007 投喂成本')

    expect(card.get('[data-testid="agent-confirmation-expires"]').text()).toContain('2099-01-01T00:00:00Z')
  })

  it('确认卡固定在 .agent-panel__pending（滚动区之外），不会随消息滚走', async () => {
    const { wrapper } = await sendOne({
      kind: 'confirmation_required',
      conversation_id: 'c1',
      message: '即将执行',
      confirmation: CONFIRMATION,
    })

    const pending = wrapper.get('[data-testid="agent-pending"]')
    expect(pending.find('[data-testid="agent-confirmation"]').exists()).toBe(true)
    // 关键：卡片**不在**滚动容器里，所以滚历史/重开面板都不会把它带走
    expect(wrapper.get('.agent-panel__body').find('[data-testid="agent-confirmation"]').exists()).toBe(false)
  })

  it('过期卡：显示“已过期”并禁用确认执行，避免点了只得到一句看不懂的报错', async () => {
    const { wrapper } = await sendOne({
      kind: 'confirmation_required',
      conversation_id: 'c1',
      message: '即将执行',
      confirmation: { ...CONFIRMATION, expires_at: '2000-01-01T00:00:00Z' },
    })

    expect(wrapper.get('[data-testid="agent-confirmation-expires"]').text()).toContain('已过期')
    const confirmButton = wrapper.get('[data-testid="agent-confirm"]').element as HTMLButtonElement
    expect(confirmButton.disabled).toBe(true)
  })

  it('rows 为空时显示「无需额外填写内容」而不是空列表', async () => {
    const { wrapper } = await sendOne({
      kind: 'confirmation_required',
      conversation_id: 'c1',
      message: '即将执行',
      confirmation: { ...CONFIRMATION, rows: [] },
    })

    expect(wrapper.text()).toContain('无需额外填写内容')
  })

  it('impact 为空时不渲染影响区块', async () => {
    const { wrapper } = await sendOne({
      kind: 'confirmation_required',
      conversation_id: 'c1',
      message: '即将执行',
      confirmation: { ...CONFIRMATION, impact: [] },
    })

    expect(wrapper.find('[data-testid="agent-confirmation-impact"]').exists()).toBe(false)
  })
})

describe('AgentPanel：消息区域保持可滚动并自动定位最新消息', () => {
  it('把滚动位置设置在真正的消息视口，而不是内部内容节点', async () => {
    const { wrapper, vm } = await mountPanel(() =>
      ndjson([
        resultLine({
          kind: 'assistant',
          conversation_id: 'c1',
          message: '收到',
        }),
      ]),
    )
    const body = wrapper.get('.agent-panel__body').element as HTMLElement
    Object.defineProperty(body, 'scrollHeight', { configurable: true, value: 1000 })
    Object.defineProperty(body, 'clientHeight', { configurable: true, value: 500 })

    await vm.submitText('测试消息')
    await flushPromises()

    expect(body.scrollTop).toBe(1000)
  })

  it('关闭再打开面板后自动滚到最下面（确认卡片不会被留在“上个对话”里）', async () => {
    const { wrapper, vm } = await mountPanel(() =>
      ndjson([
        resultLine({
          kind: 'assistant',
          conversation_id: 'c1',
          message: '收到',
        }),
      ]),
    )

    // jsdom 不做布局：不 stub 的话 `scrollHeight` 恒为 0，`scrollTop` 断言会假绿。
    // 面板重开时 `.agent-panel__body` 是**新元素**，所以 stub 必须落在原型上。
    const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight')
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
      configurable: true,
      get: () => 1000,
    })
    try {
      await vm.submitText('测试消息')
      await flushPromises()

      vm.toggle() // 关闭：v-if 卸载 body，重新打开时 scrollTop 会归零
      await flushPromises()
      expect(wrapper.find('.agent-panel__body').exists()).toBe(false)

      vm.toggle() // 重新打开：必须自动滚到底，否则卡片在下方不可见
      await flushPromises()

      const body = wrapper.get('.agent-panel__body').element as HTMLElement
      expect(body.scrollTop).toBe(1000)
    } finally {
      if (original) Object.defineProperty(HTMLElement.prototype, 'scrollHeight', original)
      else Reflect.deleteProperty(HTMLElement.prototype, 'scrollHeight')
    }
  })
})

describe('AgentPanel：确认与取消端点的调用', () => {
  it('点确认调 /confirmations/{id}/confirm 并提交 token', async () => {
    const calls: FetchCall[] = []
    const { wrapper, vm } = await mountPanel((url, init) => {
      calls.push({ url, init })
      if (url.endsWith('/turns/stream')) {
        return ndjson([
          resultLine({
            kind: 'confirmation_required',
            conversation_id: 'c1',
            message: '即将登记投喂',
            confirmation: CONFIRMATION,
          }),
        ])
      }
      return envelope({
        kind: 'executed',
        conversation_id: 'c1',
        message: '已登记投喂',
        result: {
          capability: 'feeding.create',
          resource: 'feeding',
          resource_id: 1,
          url: '/feeding/logs',
          data: {
            executed: [{ capability: 'feeding.create', resource: 'feeding', resource_id: 1 }],
          },
        },
      })
    })

    await vm.submitText('投喂 50kg')
    await flushPromises()
    await wrapper.get('[data-testid="agent-confirm"]').trigger('click')
    await flushPromises()

    const confirmCall = calls.find((call) => call.url.endsWith('/api/v1/agent/confirmations/128/confirm'))
    expect(confirmCall).toBeDefined()
    expect(confirmCall?.init?.body).toBe(JSON.stringify({ token: 'tok-once' }))
    expect(wrapper.text()).toContain('已登记投喂')
    expect(wrapper.find('[data-testid="agent-confirmation"]').exists()).toBe(false)
    expect(writeVersion.value).toBe(1)
    expect(dirtyResources.value).toEqual(['feeding'])
  })

  it('点取消调 /confirmations/{id}/cancel', async () => {
    const urls: string[] = []
    const { wrapper, vm } = await mountPanel((url) => {
      urls.push(url)
      if (url.endsWith('/turns/stream')) {
        return ndjson([
          resultLine({
            kind: 'confirmation_required',
            conversation_id: 'c1',
            message: '即将执行',
            confirmation: CONFIRMATION,
          }),
        ])
      }
      return envelope({ kind: 'cancelled' })
    })

    await vm.submitText('投喂 50kg')
    await flushPromises()
    const buttons = wrapper.get('[data-testid="agent-confirmation"]').findAll('button')
    await buttons[0].trigger('click')
    await flushPromises()

    expect(urls.some((url) => url.endsWith('/api/v1/agent/confirmations/128/cancel'))).toBe(true)
    expect(wrapper.text()).toContain('已取消该操作')
  })
})

describe('AgentPanel：同一轮签出的**多张**确认卡片', () => {
  /**
   * 实测场景（会话 `8a148ce4220e04e8`）：一句话要求归档 3 个草稿区域 → 3 张卡。
   * 每张卡的令牌都是一次性的、只在那一轮响应里出现，所以**一张都不能少渲染**；
   * 而"确认第一张"也不能顺手把其余几张清掉（那等于把它们的令牌永久丢掉）。
   */
  const SECOND: AgentConfirmation = {
    ...CONFIRMATION,
    id: 129,
    token: 'tok-once-2',
    target: '4 号塘 / 批次 B-2026-008',
  }

  const multiCard = () => ({
    kind: 'confirmation_required',
    conversation_id: 'c1',
    message: '草稿状态的区域有 2 个：测试北区、QA 测试北区。',
    confirmation: CONFIRMATION,
    confirmations: [CONFIRMATION, SECOND],
  })

  it('两张卡全部渲染（少渲染一张 = 那张令牌永远拿不回来）', async () => {
    const { wrapper } = await sendOne(multiCard())

    const cards = wrapper.findAll('[data-testid="agent-confirmation"]')
    expect(cards).toHaveLength(2)
    const targets = cards.map((card) => card.get('[data-testid="agent-confirmation-target"]').text())
    expect(targets).toEqual(['影响对象：3 号塘 / 批次 B-2026-007', '影响对象：4 号塘 / 批次 B-2026-008'])
  })

  it('确认其中一张后，**其余卡片原样保留**（含各自的一次性令牌）', async () => {
    const calls: FetchCall[] = []
    const { wrapper, vm } = await mountPanel((url, init) => {
      calls.push({ url, init })
      if (url.endsWith('/turns/stream')) return ndjson([resultLine(multiCard())])
      return envelope({
        kind: 'executed',
        conversation_id: 'c1',
        message: '已登记投喂',
        result: { capability: 'feeding.create', resource_id: 1, url: '/feeding/logs' },
      })
    })

    await vm.submitText('把草稿区域都归档')
    await flushPromises()
    expect(wrapper.findAll('[data-testid="agent-confirmation"]')).toHaveLength(2)

    // 点**第一张**的「确认执行」
    const first = wrapper.findAll('[data-testid="agent-confirmation"]')[0]
    await first.get('[data-testid="agent-confirm"]').trigger('click')
    await flushPromises()

    expect(
      calls.find((call) => call.url.endsWith('/api/v1/agent/confirmations/128/confirm'))?.init?.body,
    ).toBe(JSON.stringify({ token: 'tok-once' }))

    const left = wrapper.findAll('[data-testid="agent-confirmation"]')
    expect(left).toHaveLength(1)
    expect(left[0].get('[data-testid="agent-confirmation-target"]').text()).toBe(
      '影响对象：4 号塘 / 批次 B-2026-008',
    )
    // 第二张的令牌仍留在组件状态里（下一次点确认要用它，没地方补发）
    expect(vm.confirmations.map((card) => card.token)).toEqual(['tok-once-2'])
  })

  it('取消其中一张同样只摘掉那一张', async () => {
    const urls: string[] = []
    const { wrapper, vm } = await mountPanel((url) => {
      urls.push(url)
      if (url.endsWith('/turns/stream')) return ndjson([resultLine(multiCard())])
      return envelope({ kind: 'cancelled' })
    })

    await vm.submitText('归档')
    await flushPromises()
    const second = wrapper.findAll('[data-testid="agent-confirmation"]')[1]
    await second.findAll('button')[0].trigger('click')
    await flushPromises()

    expect(urls.some((url) => url.endsWith('/api/v1/agent/confirmations/129/cancel'))).toBe(true)
    const left = wrapper.findAll('[data-testid="agent-confirmation"]')
    expect(left).toHaveLength(1)
    expect(left[0].get('[data-testid="agent-confirmation-target"]').text()).toBe(
      '影响对象：3 号塘 / 批次 B-2026-007',
    )
    expect(vm.confirmations.map((card) => card.token)).toEqual(['tok-once'])
  })

  it('只有单张卡片时（契约原文形态）行为不变', async () => {
    const { wrapper, vm } = await sendOne({
      kind: 'confirmation_required',
      conversation_id: 'c1',
      message: '即将登记投喂',
      confirmation: CONFIRMATION,
    })

    expect(wrapper.findAll('[data-testid="agent-confirmation"]')).toHaveLength(1)
    expect(vm.confirmation).toEqual(CONFIRMATION)
    expect(vm.confirmations).toEqual([CONFIRMATION])
  })
})

describe('AgentPanel：协议约束与错误处理', () => {
  it('流式缺少 result 行 → 显示协议错误，且已显示的 delta 文本被保留', async () => {
    const { wrapper, vm } = await mountPanel(() => ndjson([{ type: 'delta', text: '已经显示的文字' }]))

    await vm.submitText('查一下')
    await flushPromises()

    expect(wrapper.get('[data-testid="agent-error"]').text()).toContain('没有返回结果')
    // 关键：已经渲染给用户看过的文字不能凭空消失
    // （早期版本 AgentPanel.vue:106-109 的 finally 无条件清空 streamingText，问题 P-6）
    expect(wrapper.text()).toContain('已经显示的文字')
  })

  it('有 result 时**不**重复保留流式正文（同一段话只显示一次）', async () => {
    // "流式缓冲无条件追加成消息"与"无条件丢弃"是同一个错误的两个方向：
    // 有 `result` 时正文由 `result.message` 交付，再把缓冲区追加一遍就是同一段话出现两次。
    const { wrapper, vm } = await mountPanel(() =>
      ndjson([
        { type: 'delta', text: '3 号塘' },
        { type: 'delta', text: '共有 2 条记录' },
        resultLine({ kind: 'assistant', conversation_id: 'c1', message: '3 号塘共有 2 条记录' }),
      ]),
    )

    await vm.submitText('查 3 号塘')
    await flushPromises()

    // 用户 1 条 + 助手 **1 条**
    expect(wrapper.findAll('article')).toHaveLength(2)
    expect(wrapper.text()).toContain('3 号塘共有 2 条记录')
  })

  it('收到 result 之前 busy 保持为真（协议要求：不得提前清理）', async () => {
    let finish: () => void = () => {}
    const { wrapper, vm } = await mountPanel(() => {
      const encoder = new TextEncoder()
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(encoder.encode(`${JSON.stringify({ type: 'status', text: '正在查询…' })}\n`))
          finish = () => {
            controller.enqueue(
              encoder.encode(
                `${JSON.stringify(resultLine({ kind: 'assistant', conversation_id: 'c1', message: '结果' }))}\n`,
              ),
            )
            controller.close()
          }
        },
      })
      return new Response(stream, { status: 200 })
    })

    const pending = vm.submitText('查一下')
    await flushPromises()

    // 还没收到 result 行 —— busy 必须仍为 true
    expect(vm.busy).toBe(true)
    expect(wrapper.text()).toContain('正在查询…')

    finish()
    await pending
    await flushPromises()
    expect(vm.busy).toBe(false)
  })

  it('404 时回退到非流式端点 /turns', async () => {
    const urls: string[] = []
    const { wrapper, vm } = await mountPanel((url) => {
      urls.push(url)
      if (url.endsWith('/turns/stream')) return errorEnvelope(404, 'NOT_FOUND', '端点不存在')
      return envelope({ kind: 'assistant', conversation_id: 'c9', message: '来自非流式端点' })
    })

    await vm.submitText('查一下')
    await flushPromises()

    expect(urls.some((url) => url.endsWith('/api/v1/agent/turns'))).toBe(true)
    expect(wrapper.text()).toContain('来自非流式端点')
  })

  it('405 时同样回退', async () => {
    const urls: string[] = []
    const { vm } = await mountPanel((url) => {
      urls.push(url)
      if (url.endsWith('/turns/stream')) return errorEnvelope(405, 'VALIDATION_ERROR', '方法不允许')
      return envelope({ kind: 'assistant', conversation_id: 'c9', message: '回退成功' })
    })

    await vm.submitText('查一下')
    await flushPromises()
    expect(urls.some((url) => url.endsWith('/api/v1/agent/turns'))).toBe(true)
  })

  it('500 时不回退——避免写操作被重复执行', async () => {
    const urls: string[] = []
    const { wrapper, vm } = await mountPanel((url) => {
      urls.push(url)
      return errorEnvelope(500, 'INTERNAL_ERROR', '内部错误')
    })

    await vm.submitText('投喂 50kg')
    await flushPromises()

    // 绝不能调 /turns（那等于把这个写操作提交第二次）
    expect(urls.some((url) => url.endsWith('/api/v1/agent/turns'))).toBe(false)
    expect(wrapper.find('[data-testid="agent-error"]').exists()).toBe(true)
  })

  it.each([
    [503, 'AGENT_UNAVAILABLE'],
    [504, 'AGENT_TIMEOUT'],
    [409, 'CONFIRMATION_INVALID'],
  ])('%i (%s) 也不回退', async (status, code) => {
    __resetClientState()
    const urls: string[] = []
    const { vm } = await mountPanel((url) => {
      urls.push(url)
      return errorEnvelope(status, code, '不可用')
    })
    await vm.submitText('投喂 50kg')
    await flushPromises()
    expect(urls.some((url) => url.endsWith('/api/v1/agent/turns'))).toBe(false)
  })

  it('每轮都带 page_context，且 history 不超过 8 条', async () => {
    const bodies: Record<string, unknown>[] = []
    const { vm } = await mountPanel((url, init) => {
      if (url.endsWith('/turns/stream')) {
        bodies.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>)
        return ndjson([resultLine({ kind: 'assistant', conversation_id: 'c1', message: 'ok' })])
      }
      return envelope({})
    })

    for (let index = 0; index < 3; index += 1) {
      await vm.submitText(`第 ${index} 轮`)
      await flushPromises()
    }

    expect(bodies).toHaveLength(3)
    expect(bodies[0].page_context).toBe('/ponds')
    expect(bodies[1].message).toBe('第 1 轮')
    expect((bodies[2].history as unknown[]).length).toBeLessThanOrEqual(8)
  })
})

describe('AgentPanel：确认失败后的卡片状态（一次性令牌）', () => {
  /**
   * 令牌是**一次性**的：只要服务端回过话，这张卡的令牌就已经被消费（无论那次执行
   * 成功与否）。而服务端把"过期 / 归属不符 / 参数不匹配 / 已用过"统一成同一句话是
   * **刻意**的（`backend/yuxin/kernel/confirmation.py::consume` 的 docstring：避免把
   * 确认机制变成探测工具）—— 所以"再点一次报令牌无效"不是服务端的 bug，
   * 而是**前端不该让一张已作废的卡继续可点**。
   *
   * 实测来源：卡片 168（用户在界面上点「确认执行」）因 `VALIDATION_ERROR` 失败后
   * 仍留在面板上，用户再点一次，看到的就是那句与真实原因无关的话。
   */
  async function mountWithConfirm(confirmFetch: () => Promise<Response>) {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input)
        if (url.endsWith('/auth/csrf')) return Promise.resolve(csrfOk())
        if (url.includes('/agent/confirmations/')) return confirmFetch()
        return Promise.resolve(
          ndjson([
            resultLine({
              kind: 'confirmation_required',
              conversation_id: 'c1',
              message: '即将调整角色与数据范围，确认后立即生效。',
              confirmation: CONFIRMATION,
              confirmations: [CONFIRMATION],
            }),
          ]),
        )
      }),
    )
    const wrapper = mount(AgentPanel, { props: { pageContext: '/admin/users' } })
    const vm = wrapper.vm as unknown as PanelVm
    vm.toggle()
    await flushPromises()
    await vm.submitText('给周海霞加上质检核验员的角色')
    await flushPromises()
    return { wrapper, vm }
  }

  it('① 服务端拒绝 → 卡片必须摘掉（令牌已作废，再点只会误报「令牌无效」）', async () => {
    const { vm } = await mountWithConfirm(() =>
      Promise.resolve(errorEnvelope(400, 'VALIDATION_ERROR', '请求包含不接受的字段：user_id')),
    )
    expect(vm.confirmations).toHaveLength(1)

    await vm.confirm(vm.confirmations[0])
    await flushPromises()

    expect(vm.confirmations).toHaveLength(0)
  })

  it('② 网络失败 → 卡片必须保留（请求可能没到服务端，令牌也许仍有效）', async () => {
    const { vm } = await mountWithConfirm(() => Promise.reject(new TypeError('Failed to fetch')))
    expect(vm.confirmations).toHaveLength(1)

    await vm.confirm(vm.confirmations[0])
    await flushPromises()

    expect(vm.confirmations).toHaveLength(1)
  })
})
