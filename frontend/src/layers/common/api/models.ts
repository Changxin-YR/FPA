import type { ErrorCode } from '../types.gen'

/** 响应信封——INTERFACES.md §1，沿用早期版本已验证形态。 */
export interface ApiResponse<T> {
  code: ErrorCode | 'OK'
  message: string
  data: T
  request_id: string
}

/**
 * 分页信封——INTERFACES.md §8：全系统统一，禁止各写各的。
 *
 * 早期版本实测有 25 处重复的分页 SQL 与 31 处重复的 `has_next` 计算
 * （.local/recon-backend.md）。前端侧对应的是 3 处手写的全量翻页循环
 * （`master-data.service.ts:12-18`、`:20-27`、`useReturns.ts:15-24`）。
 */
export interface Page<T> {
  items: T[]
  page: number
  page_size: number
  total: number
  has_next: boolean
}

/** 当前登录用户——形状由 `GET /api/v1/auth/me` 决定。 */
export interface UserSummary {
  id: number
  name: string
  /**
   * 账号状态。`must_change_password` 时需要跳转改密页，
   * 因此它参与 `nextPath` 的派生（见 session.store.ts）。
   */
  status?: 'active' | 'must_change_password' | 'pending' | 'disabled'
  /** 角色码，用于展示；权限判断一律走 `permissions`。 */
  roles: string[]
  /** 能力码清单。`hasPermission()` 只读它。 */
  permissions: string[]
}
