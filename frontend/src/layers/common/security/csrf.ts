import { apiUrl } from '../api/base'

/**
 * CSRF 令牌缓存。
 *
 * 移植自早期版本 `layers/common/security/csrf.ts`（13 行）。
 * 与旧版一样**不写 localStorage**——INTERFACES.md §5 明确要求
 * 「CSRF Token 只保存在内存并通过请求头发送，不写入 URL、localStorage 或业务表」。
 */
let cachedToken: string | null = null

/** 同一时刻只允许一次令牌获取，避免并发写请求各拉一次。 */
let pending: Promise<string> | null = null

export async function getCsrfToken(): Promise<string> {
  if (cachedToken) return cachedToken
  if (pending) return pending

  pending = (async () => {
    const response = await fetch(apiUrl('/api/v1/auth/csrf'), { credentials: 'include' })
    const body = (await response.json()) as { data?: { csrf_token?: string } }
    if (!response.ok || !body.data?.csrf_token) {
      throw new Error('CSRF Token 获取失败')
    }
    cachedToken = body.data.csrf_token
    return cachedToken
  })()

  try {
    return await pending
  } finally {
    pending = null
  }
}

export function clearCsrfToken(): void {
  cachedToken = null
  pending = null
}

/** 仅测试使用：直接写入令牌，省掉一次 fetch。 */
export function __setCsrfTokenForTest(token: string | null): void {
  cachedToken = token
}
