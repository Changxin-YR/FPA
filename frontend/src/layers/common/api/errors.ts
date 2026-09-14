import type { ErrorCode } from '../types.gen'

/**
 * 唯一业务异常类型。
 *
 * 移植自早期版本 `frontend/src/layers/common/api/errors.ts`（35 行，实测文案质量良好，
 * ARCHITECTURE.md §6.1 明确列为「必须继承」）。
 *
 * 继承理由（早期版本实测）：三个文案工厂覆盖了「网络层 / 表单提交 / 页面级提示」三类
 * 场景，并且落实了「绝不把英文底层错误透传给用户」这条硬规则。
 */
export class ApiError extends Error {
  readonly code: ErrorCode | 'NETWORK_ERROR'
  /** HTTP 状态码；0 表示请求未到达服务器（断网/DNS 失败）。 */
  readonly status: number
  readonly requestId?: string
  readonly data?: unknown
  /** 来自 `Retry-After` 响应头，仅当它是纯数字时解析。 */
  readonly retryAfter?: number

  constructor(
    code: ErrorCode | 'NETWORK_ERROR',
    message: string,
    status: number,
    requestId?: string,
    data?: unknown,
    retryAfter?: number,
  ) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.requestId = requestId
    this.data = data
    this.retryAfter = retryAfter
  }
}

/** 网络层失败（请求未到达服务器或连接中断）——用于弱网提示判断。 */
export function isNetworkError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 0 || error.code === 'NETWORK_ERROR')
}

/** 把任意异常转为可直接展示的中文文案（绝不透传英文底层错误）。 */
export function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return chineseMessage(error.message, fallback)
  if (error instanceof Error && error.message) return chineseMessage(error.message, fallback)
  return fallback
}

function chineseMessage(message: string, fallback: string): string {
  return /[A-Za-z]/.test(message) ? fallback : message
}

/** 表单提交失败文案：弱网时提示内容已保留；其余情况直接展示中文业务消息。 */
export function submitErrorText(error: unknown, fallback: string): string {
  if (isNetworkError(error)) return '提交失败，内容已保留，可重试'
  return errorText(error, fallback)
}

/** 页面级提示：在固定业务场景前缀后拼接 ApiError 消息；非业务异常回退到场景文案。 */
export function messageWithContext(error: unknown, fallback: string): string {
  return error instanceof ApiError ? `${fallback}：${chineseMessage(error.message, '请稍后重试')}` : fallback
}

/**
 * 数据范围无法解析是**服务端 fail-closed** 的信号（INTERFACES.md §1.1：
 * `DATA_SCOPE_UNRESOLVED` 403「数据范围无法解析——fail closed，不返回空集」）。
 *
 * 前端必须把它与「确实没有数据」区分开——早期版本的对应缺陷是后端在无法生成谓词时
 * 退回 `"1=0", []`，用户看到 0 行却不报错（`kernel/errors.py::scope_unresolved`
 * docstring 记录了这一点）。
 */
export function isScopeUnresolved(error: unknown): boolean {
  return error instanceof ApiError && error.code === 'DATA_SCOPE_UNRESOLVED'
}

/** 版本冲突（乐观锁）。`data.current_version` 给出服务端当前版本。 */
export function conflictVersion(error: unknown): number | null {
  if (!(error instanceof ApiError)) return null
  if (error.code !== 'VERSION_CONFLICT' && error.code !== 'CONFLICT') return null
  const data = error.data
  if (typeof data === 'object' && data !== null && 'current_version' in data) {
    const value = (data as { current_version: unknown }).current_version
    return typeof value === 'number' ? value : null
  }
  return null
}
