import { beforeEach, describe, expect, it, vi } from 'vitest'
import { __resetClientState } from '../src/layers/common/api/client'
import { __setCsrfTokenForTest } from '../src/layers/common/security/csrf'
import { ApiError } from '../src/layers/common/api/errors'
import { cancelAgent, confirmAgent, sendAgentTurn, streamAgentTurn } from '../src/layers/common/agent/service'

/**
 * 用例 2（任务说明 E.2）：**`/turns/stream` 的回退逻辑——只有 404/405 才回退到非流式**。
 *
 * 为什么这条必须测（早期版本的真实风险）：
 * 早期版本 `AgentPanel.vue:99-105` 的注释写着
 * 「只有端点缺失（部署期）才回退到非流式；其它错误直接上报，**避免写操作被重复执行**」。
 * 也就是说：如果对 500 / 超时 / 网络错误也回退，同一轮业务可能被提交两次。
 * 这是**写操作正确性**问题，不是体验问题。
 *
 * 早期版本对这行的唯一覆盖来自 `tests/agent-panel.spec.ts` 的间接渲染，
 * 且没有针对「哪种状态码才回退」的参数化断言。这里显式参数化。
 */

function ndjsonResponse(lines: string[]): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      const encoder = new TextEncoder()
      for (const line of lines) controller.enqueue(encoder.encode(`${line}\n`))
      controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { 'Content-Type': 'application/x-ndjson' } })
}

function jsonError(status: number, code: string, message: string): Response {
  return new Response(JSON.stringify({ code, message, data: null, request_id: 'r' }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeEach(() => {
  __resetClientState()
  __setCsrfTokenForTest('csrf-test')
  vi.unstubAllGlobals()
})

describe('streamAgentTurn：只有 result 行才算交付', () => {
  it('只有 status/delta 而没有 result 行时抛 AGENT_PROTOCOL_ERROR', async () => {
    vi.stubGlobal('fetch', () =>
      Promise.resolve(
        ndjsonResponse([
          JSON.stringify({ type: 'status', text: '正在查询…' }),
          JSON.stringify({ type: 'delta', text: '部分文字' }),
        ]),
      ),
    )

    const caught = (await streamAgentTurn({ message: 'x' }).catch((error: unknown) => error)) as ApiError
    expect(caught).toBeInstanceOf(ApiError)
    expect(caught.code).toBe('AGENT_PROTOCOL_ERROR')
  })

  it('损坏的 NDJSON 行抛 AGENT_PROTOCOL_ERROR，而不是静默跳过', async () => {
    vi.stubGlobal('fetch', () => Promise.resolve(ndjsonResponse(['{ not json'])))
    const caught = (await streamAgentTurn({ message: 'x' }).catch((error: unknown) => error)) as ApiError
    expect(caught.code).toBe('AGENT_PROTOCOL_ERROR')
  })

  it.each([
    [{ type: 'event', text: '未知事件' }],
    [{ type: 'status', text: 42 }],
    [{ type: 'delta', text: null }],
    [{ type: 'result', result: {} }],
    [{ type: 'result', result: { kind: 'unknown' } }],
  ])('非法流式结构 %j 抛 AGENT_PROTOCOL_ERROR', async (line) => {
    vi.stubGlobal('fetch', () => Promise.resolve(ndjsonResponse([JSON.stringify(line)])))
    const caught = (await streamAgentTurn({ message: 'x' }).catch((error: unknown) => error)) as ApiError
    expect(caught).toBeInstanceOf(ApiError)
    expect(caught.code).toBe('AGENT_PROTOCOL_ERROR')
  })

  it('流式请求携带 CSRF 头（早期版本三处裸 fetch 漏掉的之一）', async () => {
    const calls: RequestInit[] = []
    vi.stubGlobal('fetch', (_input: RequestInfo | URL, init?: RequestInit) => {
      calls.push(init ?? {})
      return Promise.resolve(
        ndjsonResponse([
          JSON.stringify({
            type: 'result',
            result: { kind: 'assistant', conversation_id: 'c', message: 'ok' },
          }),
        ]),
      )
    })

    await streamAgentTurn({ message: 'x' })
    expect(new Headers(calls[0].headers).get('X-CSRF-Token')).toBe('csrf-test')
    expect(new Headers(calls[0].headers).get('Accept')).toBe('application/x-ndjson')
  })
})

// 上面第一个用例的 stub 必须在 describe 之前生效，因此单独放置
describe('streamAgentTurn 正常流', () => {
  it('status 与 delta 分别回调，result 行返回给调用方', async () => {
    const statuses: string[] = []
    const deltas: string[] = []
    vi.stubGlobal('fetch', () =>
      Promise.resolve(
        ndjsonResponse([
          JSON.stringify({ type: 'status', text: '正在查询塘口…' }),
          JSON.stringify({ type: 'delta', text: '3 号塘' }),
          JSON.stringify({ type: 'delta', text: '共有 2 条记录' }),
          JSON.stringify({
            type: 'result',
            result: { kind: 'assistant', conversation_id: 'c1', message: '3 号塘共有 2 条记录' },
          }),
        ]),
      ),
    )

    const result = await streamAgentTurn(
      { message: '查 3 号塘' },
      { onStatus: (t) => statuses.push(t), onDelta: (t) => deltas.push(t) },
    )

    expect(result.kind).toBe('assistant')
    expect(statuses).toEqual(['正在查询塘口…'])
    expect(deltas.join('')).toBe('3 号塘共有 2 条记录')
  })
})

describe('回退判据：只有 404/405 允许回退到非流式', () => {
  /**
   * 这段逻辑在生产代码里位于 `AgentPanel.vue` 的 `submitText`：
   *   `if (streamError instanceof ApiError && (status === 404 || status === 405)) { 非流式 }`
   * 为了让断言不依赖组件挂载，这里复刻同一判据并参数化，
   * 同时用真实 `streamAgentTurn` 产生错误对象以确保 status 来源正确。
   */
  const shouldFallback = (error: unknown): boolean =>
    error instanceof ApiError && (error.status === 404 || error.status === 405)

  it.each([
    [404, 'NOT_FOUND', true],
    [405, 'VALIDATION_ERROR', true],
    [500, 'INTERNAL_ERROR', false],
    [503, 'AGENT_UNAVAILABLE', false],
    [504, 'AGENT_TIMEOUT', false],
    [409, 'CONFIRMATION_INVALID', false],
    [401, 'UNAUTHENTICATED', false],
  ])('HTTP %i (%s) → 回退=%s', async (status, code, expected) => {
    vi.stubGlobal('fetch', () => Promise.resolve(jsonError(status, code, '错误')))
    const caught = await streamAgentTurn({ message: 'x' }).catch((error: unknown) => error)
    expect(shouldFallback(caught)).toBe(expected)
  })

  it('网络层失败（status 0）不回退——写操作风险最高的情况', async () => {
    vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')))
    // fetch 抛错时 streamAgentTurn 原样抛出，类型是 TypeError 而非 ApiError
    const caught = await streamAgentTurn({ message: 'x' }).catch((error: unknown) => error)
    expect(shouldFallback(caught)).toBe(false)
    expect(caught).not.toBeInstanceOf(ApiError)
  })
})

describe('非流式端点与确认/取消端点', () => {
  function jsonOk(data: unknown): Response {
    return new Response(JSON.stringify({ code: 'OK', message: '', data, request_id: 'r1' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }

  it('sendAgentTurn 走 /api/v1/agent/turns 并携带 Idempotency-Key', async () => {
    const calls: { url: string; init?: RequestInit }[] = []
    const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init })
      return jsonOk(null)
    })
    vi.stubGlobal('fetch', mock)

    await sendAgentTurn({ message: 'hi' })
    const write = calls.find((call) => call.url.endsWith('/api/v1/agent/turns'))
    expect(write).toBeDefined()
    expect(new Headers(write?.init?.headers).get('Idempotency-Key')).toBeTruthy()
  })

  it('confirmAgent 走 /confirmations/{id}/confirm 并提交 token', async () => {
    const calls: { url: string; init?: RequestInit }[] = []
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init })
      return jsonOk({
        kind: 'executed',
        conversation_id: 'c',
        message: '已执行',
        result: { capability: 'pond.create', resource_id: 1, url: '/ponds' },
      })
    })

    await confirmAgent(128, 'tok-1')
    const write = calls.find((call) => call.url.endsWith('/api/v1/agent/confirmations/128/confirm'))
    expect(write).toBeDefined()
    expect(write?.init?.body).toBe(JSON.stringify({ token: 'tok-1' }))
  })

  it('cancelAgent 走 /confirmations/{id}/cancel', async () => {
    const calls: string[] = []
    vi.stubGlobal('fetch', (input: RequestInfo | URL, _init?: RequestInit) => {
      calls.push(String(input))
      return jsonOk({ kind: 'cancelled' })
    })

    await cancelAgent(7)
    expect(calls.some((url) => url.endsWith('/api/v1/agent/confirmations/7/cancel'))).toBe(true)
  })
})
