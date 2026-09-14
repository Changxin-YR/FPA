import type { UserSummary } from '../api/models'

/**
 * 权限判断——只读 `permissions` 能力码，**不猜角色名**。
 *
 * 移植自早期版本 `layers/common/security/access-control.ts`（8 行）。
 * 早期版本有一处反面教材值得记住：`dataset.ts` 里出现过 `super_admin` /
 * `breed_manager` 这类**角色码字面量**，而角色码可以被管理员改名，能力码不会。
 */
export function hasPermission(
  user: Pick<UserSummary, 'permissions'> | null | undefined,
  code: string,
): boolean {
  return Boolean(user?.permissions.includes(code))
}

export function hasAnyPermission(
  user: Pick<UserSummary, 'permissions'> | null | undefined,
  codes: readonly string[],
): boolean {
  return codes.some((code) => hasPermission(user, code))
}

/** 是否拥有全部指定能力码。 */
export function hasAllPermissions(
  user: Pick<UserSummary, 'permissions'> | null | undefined,
  codes: readonly string[],
): boolean {
  return codes.every((code) => hasPermission(user, code))
}
