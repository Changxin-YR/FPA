import { ApiError } from './errors'
import type { ApiResponse } from './models'
import { apiUrl } from './base'
import { clearCsrfToken, getCsrfToken } from '../security/csrf'
import type { ErrorCode } from '../types.gen'

/**
 * 统一请求客户端——**单例**，跨模块共享在途去重与幂等键。
 *
 * ## 为什么必须是单例（本文件最重要的设计决定）
 *
 * 早期版本实测缺陷（.local/recon-frontend.md 问题 P-3，ARCHITECTURE.md §6.2 列为必修）：
 * `createApiClient()` 被 **15 处**各自实例化
 * （13 个 service + `router.ts:84` + `useReturns.ts:11` 甚至在函数体内新建），
 * 而 `inflight` / `operationKeys` 是**闭包私有**状态（旧 `client.ts:12-13`）。
 * 后果：跨模块的同一次业务提交不共用幂等键，客户端头部注释宣称的
 * 「弱网恢复后重提也不重复」只在单模块内成立，且旧测试
 * （`tests/api-idempotency.spec.ts:15` 自己 `createApiClient()`）恰好绕开了这个缺陷。
 *
 * 新实现把两个 Map 放到**模块级**：模块只加载一次，所有 import 方共享同一份状态。
 * `tests/client-singleton.spec.ts` 用例 1 专门断言跨模块共用同一个 `Idempotency-Key`。
 *
 * ## 继承自早期版本的四项能力（ARCHITECTURE.md §6.1）
 * - CSRF 注入（写方法）
 * - `Idempotency-Key` 自动生成与失败保留
 * - 在途去重
 * - 中文错误归一化 + `request_id` 回显 + `Retry-After` 解析
 */

// ---------------------------------------------------------------------------
// 模块级状态：所有调用方共享
// ---------------------------------------------------------------------------

/**
 * 在途写请求：同一 `METHOD path body` 复用同一个 Promise。
 *
 * 值里带上 `idempotencyKey`，这样成功分支不必再去 `operationKeys` 里查
 * ——见下方「幂等键的生命周期」。
 */
const inflight = new Map<string, { promise: Promise<unknown>; idempotencyKey: string }>()

/**
 * 已登记的幂等键：同一 `METHOD path body` 在**后续重试**时复用原键，
 * 这样后端能识别为同一次请求而不是新请求。
 *
 * ## 幂等键的生命周期（本项目实测踩出来的坑）
 *
 * 第一版实现在**成功**后删除了这个键，结果 `tests/client-singleton.spec.ts`
 * 的「模块 A 与模块 B 复用同一个键」直接失败：两次相同请求拿到了两个不同的 UUID。
 * 这是错的——幂等键的语义是「同一次业务意图在弱网/重试下用同一个键」，
 * 因此它必须在**成功之后仍然保留**，否则"用户点了发送、超时、再点一次"就会
 * 变成两次真实写入。
 *
 * 保留策略：只清除两种情况——
 *   ① 请求被**明确拒绝**（4xx 且非 409）：服务端没有产生任何副作用；
 *   ② 记录数超上限时按插入顺序淘汰最旧的（防止长驻 SPA 内存无限增长）。
 */
const operationKeys = new Map<string, string>()

/** 保留的幂等键上限。按插入顺序淘汰，避免长驻会话内存无限增长。 */
const MAX_TRACKED_OPERATIONS = 200

function rememberOperationKey(operationKey: string, idempotencyKey: string): void {
  // Map 保证插入顺序；重新插入同一键会更新顺序，这里不需要
  operationKeys.set(operationKey, idempotencyKey)
  while (operationKeys.size > MAX_TRACKED_OPERATIONS) {
    const oldest = operationKeys.keys().next()
    if (oldest.done) break
    operationKeys.delete(oldest.value)
  }
}

/** 仅测试使用：重置模块级状态，避免用例间串扰。 */
export function __resetClientState(): void {
  inflight.clear()
  operationKeys.clear()
  clearCsrfToken()
}

const stateChangingMethods = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

/** 取一个幂等键：优先复用已登记的，其次用 crypto.randomUUID。 */
function nextIdempotencyKey(operationKey: string): { key: string; generated: boolean } {
  const existing = operationKeys.get(operationKey)
  if (existing) return { key: existing, generated: false }
  const key = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`
  return { key, generated: true }
}

/**
 * 失败后是否**清除**已登记的幂等键。
 *
 * 保留（返回 false）的情况，语义都是「这次请求可能已经在服务端生效或正在生效」：
 * - `status === 0`：请求未到达服务端，无法判断——弱网重试必须复用键；
 * - `409`：可能是 `IDEMPOTENCY_IN_PROGRESS`（同键处理中）或版本冲突，都要复用；
 * - `>= 500`：服务端可能已提交只是响应丢失。
 *
 * 清除（返回 true）是请求被**明确拒绝**、确定没有副作用的情况：
 * `400/401/403/404` 等，此时下一次提交是一次新的业务意图，应该用新键。
 */
function shouldDropKeyAfterFailure(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  return error.status !== 0 && error.status !== 409 && error.status < 500
}

// ---------------------------------------------------------------------------
// 请求实现
// ---------------------------------------------------------------------------

export interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown
  /** 显式指定幂等键（Harness 插件回放场景用）。 */
  idempotencyKey?: string
  /** 跳过 CSRF 注入（仅 CSRF 端点自身需要）。 */
  skipCsrf?: boolean
}

/**
 * 面向用户的错误文案：5xx 带 `request_id`；其余情况后端中文消息优先，
 * 缺省时按状态码给中文兜底。移植自早期版本 `client.ts:74-85`。
 */
function errorMessageOf(
  payload: { code?: string; message?: string; request_id?: string },
  status: number,
): string {
  if (status >= 500) {
    return '服务器暂时无法处理请求'
  }
  if (payload.message && payload.message.trim()) return payload.message
  const fallbacks: Record<number, string> = {
    400: '请求参数有误，请检查后重试',
    401: '登录状态已失效，请重新登录',
    403: '当前账号没有权限执行该操作',
    404: '请求的数据不存在',
    409: '数据冲突，请刷新后重试',
    429: '操作过于频繁，请稍后重试',
  }
  return fallbacks[status] ?? '请求失败，请稍后重试'
}

function parseRetryAfter(response: Response): number | undefined {
  const raw = response.headers.get('Retry-After')
  if (!raw || !/^\d+$/.test(raw)) return undefined
  return Number(raw)
}

async function doRequest<T>(
  path: string,
  options: RequestOptions,
  method: string,
  headers: Headers,
): Promise<T> {
  let response: Response
  try {
    response = await fetch(apiUrl(path), {
      ...options,
      method,
      headers,
      credentials: 'include',
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    })
  } catch {
    // 网络层失败（断网/弱网/DNS 失败）：统一为中文网络错误，
    // 绝不展示浏览器英文 "Failed to fetch"。
    throw new ApiError('NETWORK_ERROR', '网络连接失败，请检查网络后重试', 0)
  }

  let payload: ApiResponse<T>
  try {
    payload = (await response.json()) as ApiResponse<T>
  } catch {
    throw new ApiError('NETWORK_ERROR', '网络响应无法解析', response.status)
  }

  if (!response.ok || payload.code !== 'OK') {
    if (payload.code === 'CSRF_INVALID') clearCsrfToken()
    throw new ApiError(
      (payload.code ?? 'INTERNAL_ERROR') as ErrorCode,
      errorMessageOf(payload, response.status),
      response.status,
      payload.request_id,
      payload.data,
      parseRetryAfter(response),
    )
  }
  return payload.data
}

/**
 * 发起请求。写方法自动带 CSRF 与幂等键，并按需做在途去重。
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method ?? 'GET').toUpperCase()
  const headers = new Headers(options.headers)
  headers.set('Accept', 'application/json')
  if (options.body !== undefined) headers.set('Content-Type', 'application/json')

  const isWrite = stateChangingMethods.has(method)
  if (isWrite && !options.skipCsrf) {
    headers.set('X-CSRF-Token', await getCsrfToken())
  }

  // 在途去重的键：方法 + 路径 + 序列化后的请求体。
  const dedupeKey = isWrite ? `${method} ${path} ${JSON.stringify(options.body ?? null)}` : ''

  let generatedKey = false
  let usedKey = ''
  if (isWrite && !options.skipCsrf) {
    if (options.idempotencyKey) {
      usedKey = options.idempotencyKey
      headers.set('Idempotency-Key', usedKey)
    } else if (!headers.has('Idempotency-Key')) {
      const { key, generated } = nextIdempotencyKey(dedupeKey)
      headers.set('Idempotency-Key', key)
      generatedKey = generated
      usedKey = key
      if (generated) rememberOperationKey(dedupeKey, key)
    }
  }

  if (dedupeKey && inflight.has(dedupeKey)) {
    return inflight.get(dedupeKey)!.promise as Promise<T>
  }

  const promise = doRequest<T>(path, options, method, headers)

  if (dedupeKey) {
    const tracked = promise.then(
      (value) => {
        inflight.delete(dedupeKey)
        // 成功时**保留**已登记的幂等键：同一次业务意图在之后的弱网重试中
        // 必须复用同一个键，否则重试会变成第二次真实写入。
        return value
      },
      (reason: unknown) => {
        inflight.delete(dedupeKey)
        if (generatedKey && shouldDropKeyAfterFailure(reason)) {
          operationKeys.delete(dedupeKey)
        }
        throw reason
      },
    )
    inflight.set(dedupeKey, { promise: tracked, idempotencyKey: usedKey })
    return tracked
  }

  return promise
}

export const api = {
  request,
  get: <T>(path: string, options: RequestOptions = {}) => request<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, body?: unknown, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: 'PATCH', body }),
  delete: <T>(path: string, body?: unknown, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: 'DELETE', body }),
}

/** 仅测试可读：暴露当前登记的幂等键，用于断言跨模块复用。 */
export function __idempotencyKeyFor(path: string, body: unknown, method = 'POST'): string | undefined {
  return operationKeys.get(`${method} ${path} ${JSON.stringify(body ?? null)}`)
}
