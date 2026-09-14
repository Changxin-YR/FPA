import { beforeEach, describe, expect, it, vi } from 'vitest'
import { decideNavigation } from '../src/guard'
import { __resetClientState } from '../src/layers/common/api/client'
import { createSessionStore } from '../src/layers/common/session/session.store'
import { __resetMetaForTest } from '../src/layers/common/meta/meta.store'
import { hasPermission } from '../src/layers/common/security/access-control'

/**
 * 守卫链测试。
 *
 * 早期版本实测缺陷（.local/recon-frontend.md 问题 P-18）：`router.ts:87-96` 的
 * `beforeEach` 守卫链在单测中**完全未被执行**：
 *  - `tests/access-control.spec.ts:23-33` 只断言 `router.getRoutes()` 上
 *    `meta.requiredPermission` 的**声明值**；
 *  - 其余组件测试一律自建假路由表（`tests/enterprise-workbench.spec.ts:20-31`、
 *    `tests/shell-contract.spec.ts:11-18`、`tests/demo-scope.spec.ts:12-19`），
 *    因此 `authOnly` / `guestOnly` / `requiredPermission` 三类跳转
 *    都没有任何回归保护。
 *
 * 新项目把守卫抽成可独立调用的 `decideNavigation()`，直接驱动即可，无需挂载。
 */

const session = createSessionStore()

function stubMeta() {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            code: 'OK',
            message: '',
            data: { capabilities: [], resources: [] },
            request_id: 'r',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    ),
  )
}

beforeEach(() => {
  __resetClientState()
  __resetMetaForTest()
  session.clear()
  vi.unstubAllGlobals()
})

describe('hasPermission', () => {
  it('按能力码判断，不看角色名', () => {
    const user = { permissions: ['pond.view', 'pond.create'] }
    expect(hasPermission(user, 'pond.view')).toBe(true)
    expect(hasPermission(user, 'pond.delete')).toBe(false)
    expect(hasPermission(null, 'pond.view')).toBe(false)
  })
})

describe('decideNavigation：guestOnly', () => {
  it('已登录用户访问登录页 → 跳到 nextPath', async () => {
    stubMeta()
    session.setUser({ id: 1, name: '管理员', roles: [], permissions: ['pond.view'] })

    const decision = await decideNavigation({ meta: { guestOnly: true } })
    expect(decision.redirect).toEqual({ path: '/ponds' })
  })

  it('未登录用户访问登录页 → 放行', async () => {
    stubMeta()
    const decision = await decideNavigation({ meta: { guestOnly: true } })
    expect(decision.redirect).toBeNull()
  })
})

describe('decideNavigation：authOnly', () => {
  it('未登录访问业务页 → 跳到登录页', async () => {
    stubMeta()
    const decision = await decideNavigation({ meta: { authOnly: true, requiredPermission: 'pond.view' } })
    expect(decision.redirect).toEqual({ path: '/auth/login' })
  })

  it('已登录且有权限 → 放行', async () => {
    stubMeta()
    session.setUser({ id: 1, name: '管理员', roles: [], permissions: ['pond.view'] })

    const decision = await decideNavigation({ meta: { authOnly: true, requiredPermission: 'pond.view' } })
    expect(decision.redirect).toBeNull()
  })

  it('已登录但缺少能力码 → 跳到 nextPath', async () => {
    stubMeta()
    session.setUser({ id: 2, name: '作业员', roles: [], permissions: ['other.view'] })

    const decision = await decideNavigation({ meta: { authOnly: true, requiredPermission: 'pond.view' } })
    expect(decision.redirect).toEqual({ path: '/ponds' })
  })

  it('没有任何能力码的用户 → 仍被挡在业务页之外', async () => {
    stubMeta()
    session.setUser({ id: 3, name: '空权限账号', roles: [], permissions: [] })

    const decision = await decideNavigation({ meta: { authOnly: true, requiredPermission: 'pond.view' } })
    expect(decision.redirect).not.toBeNull()
  })
})

describe('decideNavigation：不需要用户的页面', () => {
  it('既非 authOnly 也非 guestOnly → 直接放行且不拉元数据', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const decision = await decideNavigation({ meta: {} })
    expect(decision.redirect).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('decideNavigation：must_change_password', () => {
  it('需要改密的用户访问业务页时被挡住（缺少能力码时走 nextPath）', async () => {
    stubMeta()
    session.setUser({
      id: 4,
      name: '新账号',
      status: 'must_change_password',
      roles: [],
      permissions: [],
    })

    const decision = await decideNavigation({ meta: { authOnly: true, requiredPermission: 'pond.view' } })
    expect(decision.redirect).toEqual({ path: '/auth/first-password' })
  })
})

describe('session.nextPath 派生', () => {
  it.each([
    ['active', '/ponds'],
    ['must_change_password', '/auth/first-password'],
    ['pending', '/auth/pending'],
    ['disabled', '/auth/login'],
  ])('status=%s → %s', (status, expected) => {
    session.setUser({ id: 1, name: 'x', status: status as 'active', roles: [], permissions: [] })
    expect(session.nextPath.value).toBe(expected)
  })

  it('未登录 → /auth/login', () => {
    session.clear()
    expect(session.nextPath.value).toBe('/auth/login')
  })
})

describe('元数据不可用时守卫不得阻断导航（白屏回归）', () => {
  it('loadMeta 抛错时仍放行，把错误留给页面展示', async () => {
    session.setUser({ id: 1, name: '管理员', roles: [], permissions: ['pond.view'] })
    // 元数据接口返回 HTML（代理错误页）→ JSON 解析失败
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response('<html>502 Bad Gateway</html>', {
            status: 500,
            headers: { 'Content-Type': 'text/html' },
          }),
        ),
      ),
    )

    const decision = await decideNavigation({ meta: { authOnly: true, requiredPermission: 'pond.view' } })
    // 关键：不得抛异常，也不得跳转 —— 否则 Vue Router 报
    // 「Unexpected error when starting the router」，整页白屏（实测）
    expect(decision.redirect).toBeNull()
  })

  it('元数据返回网络错误时同样放行', async () => {
    session.setUser({ id: 1, name: '管理员', roles: [], permissions: ['pond.view'] })
    vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')))

    const decision = await decideNavigation({ meta: { authOnly: true, requiredPermission: 'pond.view' } })
    expect(decision.redirect).toBeNull()
  })

  it('元数据不可用不影响鉴权判定本身', async () => {
    // 未登录 + 元数据不可用 → 仍应被挡回登录页
    session.clear()
    vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')))

    const decision = await decideNavigation({ meta: { authOnly: true, requiredPermission: 'pond.view' } })
    expect(decision.redirect).toEqual({ path: '/auth/login' })
  })
})

describe('重定向到自己时的死循环回归', () => {
  it('未登录访问登录页时不产生重定向（否则 vue-router 会连续跳 30 次）', async () => {
    session.clear()
    // 未登录 + guestOnly：既不该跳走，也不该跳回自己
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({ code: 'UNAUTHENTICATED', message: 'x', data: null, request_id: 'r' }),
            { status: 401, headers: { 'Content-Type': 'application/json' } },
          ),
        ),
      ),
    )

    const decision = await decideNavigation({ path: '/auth/login', meta: { guestOnly: true } })
    expect(decision.redirect).toBeNull()
  })

  it('缺少权限且 nextPath 恰好是当前页时也不重定向', async () => {
    session.setUser({ id: 1, name: '作业员', roles: [], permissions: [] })
    stubMeta()
    const decision = await decideNavigation({
      path: '/ponds',
      meta: { authOnly: true, requiredPermission: 'pond.view' },
    })
    // nextPath 对 active 用户是 /ponds，即当前页 —— 必须放行而不是跳回自己
    expect(decision.redirect).toBeNull()
  })

  it('重定向到不同路径时仍然生效（不能因为防死循环而放过鉴权）', async () => {
    session.clear()
    stubMeta()
    const decision = await decideNavigation({ path: '/ponds', meta: { authOnly: true } })
    expect(decision.redirect).toEqual({ path: '/auth/login' })
  })
})
