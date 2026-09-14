/**
 * `apiUrl()`：把相对路径拼成同源或跨源 URL。
 *
 * 移植自早期版本 `frontend/src/layers/common/api/base.ts`（12 行，设计正确，直接继承）。
 * 唯一改动：类型显式化，便于 `tests/api-url.spec.ts` 断言。
 */
const configuredApiBaseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').trim().replace(/\/+$/, '')
const publicBasePath = (import.meta.env.BASE_URL ?? '/').replace(/\/+$/, '')

/**
 * Electron / `file:` 协议下没有同源后端，回落到本机服务。
 * 早期版本同一逻辑见 `base.ts:3-5`。
 */
const defaultApiBaseUrl =
  typeof window !== 'undefined' && window.location.protocol === 'file:' ? 'http://127.0.0.1:5101' : ''

export function apiUrl(path: string, base = configuredApiBaseUrl || defaultApiBaseUrl): string {
  if (/^https?:\/\//i.test(path)) return path
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  if (!base) return `${publicBasePath}${normalizedPath}`
  return `${base.trim().replace(/\/+$/, '')}${normalizedPath}`
}
