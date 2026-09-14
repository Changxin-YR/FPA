import { beforeEach, describe, expect, it, vi } from 'vitest'
import { __idempotencyKeyFor, __resetClientState, api, request } from '../src/layers/common/api/client'
import {
  ApiError,
  errorText,
  isNetworkError,
  messageWithContext,
  submitErrorText,
} from '../src/layers/common/api/errors'

/**
 * 用例 1（任务说明 E.1）：**两个不同模块各调一次写请求，断言复用了同一个
 * `Idempotency-Key`**。
 *
 * 这是早期版本的**真实 bug**，不是假想：
 *  - `createApiClient()` 被 15 处各自实例化（13 个 service + `router.ts:84` +
 *    `useReturns.ts:11` 在函数体内），而 `inflight` / `operationKeys` 是闭包私有
 *    （旧 `client.ts:12-13`）。
 *  - 后果：跨模块的同一次业务提交不共用幂等键，客户端头注释宣称的
 *    「弱网恢复后重提也不重复」只在单模块内成立。
 *  - 旧测试 `tests/api-idempotency.spec.ts:15` 自己 `createApiClient()`，
 *    测的是「同一实例内的行为」，恰好绕开了这个缺陷。
 *
 * 本用例刻意通过**两个不同模块**（`api` 对象与裸 `request` 函数）发起同一请求。
 */

const WRITE_PATH = '/api/v1/ponds'
const WRITE_BODY = { code: 'P-004', name: '4号塘' }

function okResponse(): Response {
  return new Response(JSON.stringify({ code: 'OK', message: '', data: { id: 1 }, request_id: 'r1' }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function csrfResponse(): Response {
  return new Response(
    JSON.stringify({ code: 'OK', message: '', data: { csrf_token: 'csrf-1' }, request_id: 'r0' }),
    {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    },
  )
}

/** 收集 fetch 收到的请求，供断言。 */
function captureFetch(handler?: (url: string, init?: RequestInit) => Response | undefined) {
  const calls: { url: string; init: RequestInit | undefined }[] = []
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    calls.push({ url, init })
    if (url.endsWith('/auth/csrf')) return Promise.resolve(csrfResponse())
    const custom = handler?.(url, init)
    return Promise.resolve(custom ?? okResponse())
  })
  vi.stubGlobal('fetch', mock)
  return calls
}

function idempotencyKeysOf(calls: { url: string; init?: RequestInit }[]): string[] {
  return calls
    .filter((call) => !call.url.endsWith('/auth/csrf'))
    .map((call) => new Headers(call.init?.headers).get('Idempotency-Key') ?? '')
}

beforeEach(() => {
  __resetClientState()
  vi.unstubAllGlobals()
})

describe('client 单例：跨模块共享幂等键与在途去重', () => {
  it('模块 A 与模块 B 对同一写请求复用同一个 Idempotency-Key', async () => {
    const calls = captureFetch()

    // 「模块 A」：走 api.post（等价于生产代码里某个 service 的写法）
    await api.post(WRITE_PATH, WRITE_BODY)
    const keyAfterA = idempotencyKeysOf(calls)[0]

    // 「模块 B」：走裸 request（等价于另一个 service 的写法）
    await request(WRITE_PATH, { method: 'POST', body: WRITE_BODY })
    const keys = idempotencyKeysOf(calls)

    expect(keys).toHaveLength(2)
    expect(keys[0]).toBeTruthy()
    // 这是核心断言：两次调用必须拿到同一个键
    expect(keys[1]).toBe(keys[0])
    expect(keyAfterA).toBe(keys[0])
  })

  it('已在途的同一写请求被去重，只发出一次 fetch', async () => {
    // 让第一次写请求挂起，以便制造「在途」
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/auth/csrf')) return Promise.resolve(csrfResponse())
      return gate.then(() => okResponse())
    })
    vi.stubGlobal('fetch', fetchMock)

    const first = api.post(WRITE_PATH, { code: 'X-1' })
    const second = api.post(WRITE_PATH, { code: 'X-1' })
    release()
    await Promise.all([first, second])

    const writes = fetchMock.mock.calls.filter(([input]) => !String(input).endsWith('/auth/csrf'))
    expect(writes).toHaveLength(1)
  })

  it('不同请求体不共享幂等键（去重键包含 body）', async () => {
    const calls = captureFetch()
    await api.post(WRITE_PATH, { code: 'A' })
    await api.post(WRITE_PATH, { code: 'B' })
    const keys = idempotencyKeysOf(calls)
    expect(keys[0]).not.toBe(keys[1])
  })

  it('请求被明确拒绝（404）后清除登记的键，下次得到新键', async () => {
    captureFetch((url) =>
      url.endsWith(WRITE_PATH)
        ? new Response(
            JSON.stringify({
              code: 'NOT_FOUND',
              message: '目标不存在或已被删除',
              data: null,
              request_id: 'r',
            }),
            { status: 404, headers: { 'Content-Type': 'application/json' } },
          )
        : undefined,
    )

    await expect(api.post(WRITE_PATH, WRITE_BODY)).rejects.toBeInstanceOf(ApiError)
    expect(__idempotencyKeyFor(WRITE_PATH, WRITE_BODY)).toBeUndefined()
  })

  it('5xx 之后保留登记的键，重试复用同一个键（弱网恢复场景）', async () => {
    let attempt = 0
    captureFetch((url) => {
      if (!url.endsWith(WRITE_PATH)) return undefined
      attempt += 1
      return attempt === 1
        ? new Response(
            JSON.stringify({
              code: 'INTERNAL_ERROR',
              message: '服务器暂时无法处理请求',
              data: null,
              request_id: 'r-srv',
            }),
            { status: 500, headers: { 'Content-Type': 'application/json' } },
          )
        : okResponse()
    })

    await expect(api.post(WRITE_PATH, WRITE_BODY)).rejects.toBeInstanceOf(ApiError)
    // 键仍在登记表中 —— 服务端可能已提交，只是响应丢了
    expect(__idempotencyKeyFor(WRITE_PATH, WRITE_BODY)).toBeTruthy()
  })
})

describe('错误归一化', () => {
  it('网络层失败转为中文 NETWORK_ERROR，不透传英文 "Failed to fetch"', async () => {
    vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')))
    const caught = await api.get('/api/v1/ponds').catch((error: unknown) => error)
    expect(caught).toBeInstanceOf(ApiError)
    expect((caught as ApiError).code).toBe('NETWORK_ERROR')
    expect((caught as ApiError).message).toBe('网络连接失败，请检查网络后重试')
    expect(isNetworkError(caught)).toBe(true)
  })

  it('5xx 文案带 request_id', async () => {
    vi.stubGlobal('fetch', () =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            code: 'INTERNAL_ERROR',
            message: 'BOOM',
            data: null,
            request_id: 'req-42',
          }),
          { status: 500, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )
    const caught = (await api.get('/api/v1/ponds').catch((error: unknown) => error)) as ApiError
    expect(caught.message).toBe('服务器暂时无法处理请求')
    expect(caught.requestId).toBe('req-42')
  })

  it('429 解析 Retry-After 头', async () => {
    vi.stubGlobal('fetch', () =>
      Promise.resolve(
        new Response(JSON.stringify({ code: 'RATE_LIMITED', message: '', data: null, request_id: 'r' }), {
          status: 429,
          headers: { 'Content-Type': 'application/json', 'Retry-After': '30' },
        }),
      ),
    )
    const caught = (await api.get('/api/v1/ponds').catch((error: unknown) => error)) as ApiError
    expect(caught.retryAfter).toBe(30)
  })

  it('三个中文文案工厂：弱网提交提示内容已保留', () => {
    const network = new ApiError('NETWORK_ERROR', '网络连接失败，请检查网络后重试', 0)
    expect(submitErrorText(network, '失败')).toBe('提交失败，内容已保留，可重试')
    // 任意底层英文异常都回退为中文文案
    expect(submitErrorText(new Error('x'), '失败')).toBe('失败')
    expect(submitErrorText(new Error(''), '失败')).toBe('失败')
    expect(messageWithContext(new ApiError('CONFLICT', '数据冲突', 409), '保存失败')).toBe(
      '保存失败：数据冲突',
    )
    expect(messageWithContext(new Error('boom'), '保存失败')).toBe('保存失败')
    expect(errorText(new Error('Failed to fetch'), '兜底')).toBe('兜底')
  })
})
