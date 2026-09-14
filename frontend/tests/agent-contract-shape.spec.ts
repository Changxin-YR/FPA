import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AgentPanel from '../src/layers/common/ui/AgentPanel.vue'
import { __resetClientState } from '../src/layers/common/api/client'
import { __setCsrfTokenForTest } from '../src/layers/common/security/csrf'
import {
  __resetAgentWriteSignal,
  dirtyResources,
  isResourceDirty,
  writeVersion,
} from '../src/layers/common/agent/write-signal'

/**
 * `AgentPanel` 的**契约形状**用例（负责人 硬性要求）。
 *
 * ## 这条测试防的是什么
 *
 * e2e 用 Playwright `route` 拦截返回的是**我们自己写的桩**——后端若改了
 * `confirmation` 对象的字段名（例如把 `rows` 改成 `changes`、`target` 改成
 * `object`），桩不知道，测试照样绿。这正是早期版本「Python 桩与真实契约漂移但
 * CI 全绿」的盲区。
 *
 * 因此这里**逐字使用 `docs/INTERFACES.md` §3 的四种 `kind` 样例原文**
 * （含 `confirmation_required` 的完整 `confirmation` 对象）。
 * 契约一改，这条测试的样例就与文档不同步——复核时能直接看出来。
 *
 * ## 样例来源（逐个标注）
 *
 * | kind | INTERFACES.md 行 |
 * |---|---|
 * | `assistant` | §3 ① |
 * | `clarification` | §3 ② |
 * | `confirmation_required` | §3 ③ |
 * | `executed` | §3 ④ |
 *
 * 流式行形状：`{ "type": "result", "result": { /* 上面四种之一 *\/ } }`（§3 stream）。
 */

/** §3 ① 纯回答 */
const ASSISTANT = {
  kind: 'assistant',
  conversation_id: 'c1',
  message: '3 号塘最近 7 天共投喂 210kg，日均 30kg。',
}

/** §3 ② 信息不足，反问 */
const CLARIFICATION = {
  kind: 'clarification',
  conversation_id: 'c1',
  question: '请确认使用哪个物料：',
  options: ['1号饲料（鲤鱼配合饲料）', '2号饲料（对虾配合饲料）'],
  allow_free_text: true,
}

/**
 * §3 ③ 高风险写操作，待确认 —— **完整 confirmation 对象，一字不改**。
 * 字段：id / token / capability / title / target / rows / impact / expires_at
 */
const CONFIRMATION_REQUIRED = {
  kind: 'confirmation_required',
  conversation_id: 'c1',
  message: '即将为 3 号塘登记投喂 50kg 1号饲料，确认后立即扣减库存并计入成本。',
  confirmation: {
    id: 128,
    token: '一次性令牌，仅返回一次',
    capability: 'feeding.create',
    title: '登记投喂',
    target: '3 号塘 / 批次 B-2026-007',
    rows: [
      { label: '物料', value: '1号饲料' },
      { label: '数量', value: '50 kg' },
      { label: '发生时间', value: '2026-09-12' },
    ],
    impact: ['扣减 1 号仓 1号饲料 50kg', '计入批次 B-2026-007 投喂成本'],
    // 远期时间：本文件只核形状，过期态由 `agent-panel.spec.ts` 专门覆盖。
    expires_at: '2099-01-01T00:00:00Z',
  },
}

/**
 * §3 ③ 的**附加字段**（非破坏性）：`confirmations` 是同一轮签出的全部卡片。
 *
 * 实测场景（会话 `8a148ce4220e04e8`）：一句话要求归档 3 个草稿区域 → 3 张卡。
 * 每张卡的令牌都是一次性的、只在那一轮响应里出现，所以少了哪一张，
 * 那一张就永远无法确认。`confirmation`（单数）仍是其中第一张 ——
 * 按 §3 ③ 原文写的客户端行为不变。
 */
const CONFIRMATION_BATCH = {
  ...CONFIRMATION_REQUIRED,
  confirmations: [
    CONFIRMATION_REQUIRED.confirmation,
    {
      ...CONFIRMATION_REQUIRED.confirmation,
      id: 129,
      token: '一次性令牌（第二张）',
      target: '4 号塘 / 批次 B-2026-008',
    },
  ],
}

/**
 * §3 ④ 已执行。
 *
 * `result.resource` 与 `result.data.executed` 是本任务（t14）向 §3 追加的字段：
 * 前者让「写入后当前列表自动刷新」知道**该刷哪个资源**，后者承载一次对话里的
 * 多条写入。`capability` / `resource_id` / `url` 三个原有字段一字未动。
 */
const EXECUTED = {
  kind: 'executed',
  conversation_id: 'c1',
  message: '已登记投喂：3 号塘 50kg 1号饲料，库存剩 420kg，成本增加 ¥240.00。',
  result: {
    capability: 'feeding.create',
    resource: 'feeding',
    resource_id: 991,
    url: '/feeding/logs',
    data: { executed: [{ capability: 'feeding.create', resource: 'feeding', resource_id: 991 }] },
  },
}

function csrfOk(): Response {
  return new Response(JSON.stringify({ code: 'OK', data: { csrf_token: 'csrf-test' }, request_id: 'r0' }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** 按 INTERFACES.md §3 的流式行形状构造 NDJSON 响应。 */
function ndjson(result: object): Response {
  const encoder = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(`${JSON.stringify({ type: 'result', result })}\n`))
      controller.close()
    },
  })
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'application/x-ndjson' } })
}

async function ask(result: object) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/auth/csrf')) return Promise.resolve(csrfOk())
      return Promise.resolve(ndjson(result))
    }),
  )
  const wrapper = mount(AgentPanel, { props: { pageContext: '/feedings' } })
  const vm = wrapper.vm as unknown as { submitText: (t: string) => Promise<void>; toggle: () => void }
  vm.toggle()
  await flushPromises()
  await vm.submitText('给 3 号塘投喂 50kg 1号饲料')
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  __resetClientState()
  __resetAgentWriteSignal()
  __setCsrfTokenForTest('csrf-test')
  vi.unstubAllGlobals()
})

describe('AgentPanel 契约形状：INTERFACES.md §3 四种 kind 原文样例', () => {
  it('① assistant：正文按原文渲染，不出现确认卡片', async () => {
    const wrapper = await ask(ASSISTANT)
    expect(wrapper.text()).toContain(ASSISTANT.message)
    expect(wrapper.find('[data-testid="agent-confirmation"]').exists()).toBe(false)
  })

  it('② clarification：question / options / allow_free_text 三字段齐全', async () => {
    const wrapper = await ask(CLARIFICATION)
    const card = wrapper.get('[data-testid="agent-clarification"]')
    expect(card.get('[data-testid="agent-clarification-question"]').text()).toBe(CLARIFICATION.question)
    const options = card.findAll('[data-testid="agent-clarification-option"]').map((n) => n.text())
    expect(options).toEqual(CLARIFICATION.options)
    // allow_free_text=true → 有输入框
    expect(card.find('[data-testid="agent-clarification-input"]').exists()).toBe(true)
  })

  it('③ confirmation_required：confirmation 的 8 个字段逐一渲染', async () => {
    const wrapper = await ask(CONFIRMATION_REQUIRED)
    const card = wrapper.get('[data-testid="agent-confirmation"]')

    // title
    expect(card.get('[data-testid="agent-confirmation-title"]').text()).toBe('登记投喂')
    // target
    expect(card.get('[data-testid="agent-confirmation-target"]').text()).toContain('3 号塘 / 批次 B-2026-007')
    // rows（3 行中文键值）
    expect(card.findAll('[data-testid="agent-confirmation-rows"] dt').map((n) => n.text())).toEqual([
      '物料',
      '数量',
      '发生时间',
    ])
    expect(card.findAll('[data-testid="agent-confirmation-rows"] dd').map((n) => n.text())).toEqual([
      '1号饲料',
      '50 kg',
      '2026-09-12',
    ])
    // impact
    const impact = card.get('[data-testid="agent-confirmation-impact"]').text()
    for (const item of CONFIRMATION_REQUIRED.confirmation.impact) {
      expect(impact).toContain(item)
    }
    // expires_at
    expect(card.get('[data-testid="agent-confirmation-expires"]').text()).toContain('2099-01-01T00:00:00Z')
    // message 也要出现（§3 ③ 的正文）
    expect(wrapper.text()).toContain('确认后立即扣减库存并计入成本')
  })

  it('③ 待确认状态**不得**被渲染成已完成（写操作只能由 result 行交付）', async () => {
    const wrapper = await ask(CONFIRMATION_REQUIRED)
    expect(wrapper.text()).not.toContain('已完成')
    expect(wrapper.text()).not.toContain('已登记投喂')
    // 两个按钮必须在
    expect(wrapper.find('[data-testid="agent-confirm"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="agent-confirmation"]').exists()).toBe(true)
  })

  it('③ 带 `confirmations` 时逐张渲染（一张都不能少）', async () => {
    const wrapper = await ask(CONFIRMATION_BATCH)

    const cards = wrapper.findAll('[data-testid="agent-confirmation"]')
    expect(cards).toHaveLength(2)
    expect(cards.map((card) => card.get('[data-testid="agent-confirmation-target"]').text())).toEqual([
      '影响对象：3 号塘 / 批次 B-2026-007',
      '影响对象：4 号塘 / 批次 B-2026-008',
    ])
    // 每张卡各自有「取消 / 确认执行」——确认哪一张由用户决定
    expect(wrapper.findAll('[data-testid="agent-confirm"]')).toHaveLength(2)
  })

  it('④ executed：message 与 result.capability 的语义（正文来自服务端）', async () => {
    const wrapper = await ask(EXECUTED)
    expect(wrapper.text()).toContain(EXECUTED.message)
    expect(wrapper.find('[data-testid="agent-confirmation"]').exists()).toBe(false)
  })

  it('④ executed：必须发布写入信号，且只认服务端给的资源名', async () => {
    // 这是「写入后列表自动刷新」的**发布端**：不发布，订阅端再正确也不会刷。
    __resetAgentWriteSignal()
    await ask(EXECUTED)
    expect(writeVersion.value).toBe(1)
    expect(isResourceDirty('feeding')).toBe(true)
    // 没有被写入的资源不得被标记（否则会白刷别的页面）
    expect(isResourceDirty('pond')).toBe(false)
  })

  it('① assistant：**不得**发布写入信号（纯查询不该触发刷新）', async () => {
    __resetAgentWriteSignal()
    await ask(ASSISTANT)
    expect(writeVersion.value).toBe(0)
    expect(dirtyResources.value).toEqual([])
  })

  it('确认端点按 §3 的路径与载荷调用', async () => {
    const calls: { url: string; body?: string }[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.endsWith('/auth/csrf')) return Promise.resolve(csrfOk())
        if (url.endsWith('/turns/stream')) return Promise.resolve(ndjson(CONFIRMATION_REQUIRED))
        calls.push({ url, body: String(init?.body ?? '') })
        return Promise.resolve(
          new Response(JSON.stringify({ code: 'OK', message: '', data: EXECUTED, request_id: 'r1' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        )
      }),
    )

    const wrapper = mount(AgentPanel, { props: { pageContext: '/feedings' } })
    const vm = wrapper.vm as unknown as { submitText: (t: string) => Promise<void>; toggle: () => void }
    vm.toggle()
    await flushPromises()
    await vm.submitText('投喂')
    await flushPromises()
    await wrapper.get('[data-testid="agent-confirm"]').trigger('click')
    await flushPromises()

    // §3：POST /api/v1/agent/confirmations/{id}/confirm  载荷 { token }
    const confirm = calls.find((c) => c.url.endsWith('/api/v1/agent/confirmations/128/confirm'))
    expect(
      confirm,
      `期望调用 confirmations/128/confirm，实际调用：${calls.map((c) => c.url).join(', ')}`,
    ).toBeDefined()
    expect(JSON.parse(confirm!.body ?? '{}')).toEqual({ token: '一次性令牌，仅返回一次' })
  })

  it('四种 kind 的 kind 值本身与契约一致（防拼写漂移）', () => {
    // 这是「契约形状」的最基础一层：kind 的取值域
    const kinds = [ASSISTANT.kind, CLARIFICATION.kind, CONFIRMATION_REQUIRED.kind, EXECUTED.kind]
    expect(kinds).toEqual(['assistant', 'clarification', 'confirmation_required', 'executed'])
  })
})
