import { loadMeta } from './layers/common/meta/meta.store'
import { hasPermission } from './layers/common/security/access-control'
import { createSessionStore } from './layers/common/session/session.store'

/**
 * 导航守卫（独立于 router 实例）。
 *
 * ## 为什么单独一个文件
 *
 * 早期版本实测缺陷（.local/recon-frontend.md 问题 P-18）：`router.ts:87-96` 的守卫链
 * 在单测中**完全未被执行**——`tests/access-control.spec.ts:23-33` 只断言 `meta` 的
 * 声明值，其余组件测试一律自建假路由表，因此四类跳转都没有回归保护。
 *
 * 新项目把守卫做成**可独立调用的纯函数**。放在单独模块里还有一个实际原因：
 * 如果测试从 `router.ts` import 它，会连带创建真实的 router 实例并触发初始导航，
 * 在 jsdom 里表现为「Detected a possibly infinite redirection」告警噪声
 * （`router.ts` 的 `/` → `/ponds` 重定向 + 未登录拦截）。
 * 拆开之后测试只加载这个模块，不产生任何副作用。
 */

const session = createSessionStore()

export interface GuardTarget {
  /** 目标路径，用于避免「重定向到自己」造成的死循环。 */
  path?: string
  meta: Record<string, unknown>
}

export interface GuardDecision {
  /** `null` 表示放行。 */
  redirect: { path: string } | null
}

/**
 * 若重定向目标就是当前目标路径，则不重定向。
 *
 * 实测教训（真实死循环，会导致整页白屏）：未登录访问 `/ponds` → 守卫跳 `/auth/login`；
 * 再次进入守卫时 `session.load()` 仍返回 null，于是又算出一个「回到 `/auth/login`」的
 * 重定向——vue-router 连续 30 次跳到同一 location 后抛
 * 「Infinite redirect in navigation guard」。守卫返回「重定向到自己」即等价于死循环。
 */
function redirectTo(target: GuardTarget, path: string): GuardDecision {
  if (target.path === path) return { redirect: null }
  return { redirect: { path } }
}

export async function decideNavigation(to: GuardTarget): Promise<GuardDecision> {
  const requiresUser = Boolean(to.meta.authOnly || to.meta.guestOnly)
  if (!requiresUser) return { redirect: null }

  const user = session.user.value ?? (await session.load())

  if (to.meta.guestOnly && user) return redirectTo(to, session.nextPath.value)
  if (to.meta.authOnly && !user) return redirectTo(to, '/auth/login')

  // 初始密码未改：**只允许停在改密页**。
  //
  // 这是体验层的第一道；**安全边界在服务端**（`web/app.py::_current_actor` 会拒绝
  // 一切非 `/auth/*` 的请求）。两道都要有：少了服务端那道，改密可被整段绕过（P1 实测）；
  // 少了这道，用户会先看到满屏 403 才知道要改密。
  if (user && user.status === 'must_change_password' && to.path !== '/auth/first-password') {
    return redirectTo(to, '/auth/first-password')
  }

  const requiredPermission =
    typeof to.meta.requiredPermission === 'string' ? to.meta.requiredPermission : null
  if (requiredPermission && !hasPermission(user, requiredPermission)) {
    return redirectTo(to, session.nextPath.value)
  }

  // 元数据是「渲染而非声明」的前提，进入业务页前尽力就绪。
  //
  // **但它不能阻断导航。** 实测教训：第一版直接 `await loadMeta()`，当元数据接口
  // 不可用（后端未起、网络失败、代理返回 HTML 错误页）时异常从守卫抛出，
  // Vue Router 报「Unexpected error when starting the router」，整页白屏
  // （#app 为空）。守卫的职责是鉴权，元数据只是渲染输入——拿不到就让页面自己
  // 显示「元数据加载失败 + 重新加载」（见 PondListPage 的 pageError），
  // 而不是把用户挡在一个空壳外面。
  //
  // 安全性不受影响：真正的权限判定在后端，前端守卫只是体验层。
  try {
    await loadMeta()
  } catch {
    // 错误已记录在 meta.store 的 error 里，供页面展示。
  }
  return { redirect: null }
}
