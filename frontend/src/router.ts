import { createRouter, createWebHashHistory, createWebHistory } from 'vue-router'
import { decideNavigation } from './guard'
import { reloadMeta, resolveResourceByPath, useMeta } from './layers/common/meta/meta.store'

/**
 * 路由表 + 守卫装配。
 *
 * 守卫本身在 `guard.ts`（可独立测试）；这里只把路由表与守卫接起来，
 * 避免模块级副作用影响测试。
 *
 * ## 权限元数据的取法（**已不再需要**）
 *
 * 每条业务路由原先各自写一个 `requiredPermission`（用"该页依赖的读能力"的权限码），
 * 注释还警告"前端不发明权限码——写错一个码会让整页跳走"。
 *
 * 那套写法在 t8 被**整段删除**了，理由不是"太麻烦"，而是它与服务端重复：
 *
 *   * 服务端 `/meta/capabilities` 已经按当前账号过滤过（`registry.visible_to`），
 *     所以"这个资源有一条可见的读能力"**就是**"这个账号能看这个列表"；
 *   * 前端再抄一遍权限码，就多了一处会漂移的描述——而漂移的后果正是注释里
 *     担心的那件事（写错一个码 → 整页跳走）。
 *
 * 现在资源级可达性由 `resolveResourceByPath()` 按**同一份已过滤的元数据**判定，
 * 所以"导航里有"与"路由能进去"不可能不一致。
 *
 * ## 资源页：一个动态解析器取代 14 条手写路由
 *
 * 原先 12 个资源各有一条静态路由指向一个 7 行的包装组件（内容只有资源名），
 * 而服务端实际有 **23 个资源** —— 于是其余资源在界面上根本没有入口。
 *
 * 现在只有**一条** `/:resourcePath(.*)`：它把 URL 反查成资源名写进 `route.meta.resource`，
 * 由 `common/ui/ResourcePage.vue` 渲染。
 *
 * **为什么不需要再给每个资源补一条路由**：`resolveResourceByPath` 是查表，
 * 表的键来自服务端 `ResourceMeta.list_path`。服务端新增资源 → 查表自动命中。
 *
 * ## 为什么是"换掉"而不是"再来一条"（同一件事不留两条路径）
 *
 * 每条资源的列表在 t8 之前有两种可能的地址：手写的那条（`/warehouse/materials`）与
 * 由元数据推导的那条（`/materials`）。两条都指向同一个组件、同一份数据，
 * **这就是"同一件事两条路径"**，而它早就在漂移了：
 *
 *     手写路由                      服务端 list_path
 *     /warehouse/materials    ≠    /api/v1/materials
 *     /purchase/orders        ≠    /api/v1/purchase-orders
 *     /sales/orders           ≠    /api/v1/sales-orders
 *     /admin/audit-logs       ≠    /api/v1/audit-logs
 *
 * 那些多出来的前缀是手写时各自拍的。**保留哪一条**：保留由 `list_path`
 * 推导的那条，因为它有服务端依据；手写前缀没有依据，删掉不损失任何信息。
 *
 * ## 保留的静态路由（它们不是"资源的列表页"）
 *
 *   * `/auth/login` —— 登录页，`guestOnly`；
 *   * `/workbench` —— 待办工作台，它**没有对应的 `ResourceMeta`**（`work_item`
 *     读能力尚未登记，见 `WorkbenchPage` 的说明），所以不可能由元数据推导；
 *   * `/ponds/:id` —— 塘口详情。它是**该资源列表页之外的独立页面**（405 行，
 *     含状态变更申请表单），不是列表的重复路径，所以两条并存不构成重复。
 *     它排在解析器之前，因此优先命中。
 */
const auth = { authOnly: true }

/** 全部资源页共用的**唯一**一条路由记录。全局守卫靠它识别哪一条需要解析资源。 */
const RESOURCE_ROUTE_PATH = '/:resourcePath(.*)'

type Loader = () => Promise<unknown>

function listRoute(path: string, component: Loader, permission?: string) {
  return {
    path,
    component,
    meta: permission ? { ...auth, requiredPermission: permission } : { ...auth },
  }
}

// 解析器已移到 `meta.store.ts`（纯元数据逻辑，路由与 ResourcePage 都要用）。
// 这里 re-export 保持既有 import 路径可用。
export { resolveResourceByPath }

export const router = createRouter({
  history:
    typeof window !== 'undefined' && window.location.protocol === 'file:'
      ? createWebHashHistory()
      : createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', redirect: '/workbench' },
    {
      path: '/auth/login',
      component: () => import('./layers/product/auth/LoginPage.vue'),
      meta: { guestOnly: true },
    },
    {
      path: '/auth/first-password',
      component: () => import('./layers/product/auth/FirstPasswordPage.vue'),
      // 已登录但 `status === 'must_change_password'` 的用户落在这里（见 session.store 的
      // `PATH_BY_STATE`）。**必须排在资源解析器之前**，否则会被兜底路由吞掉 →
      // 守卫判成"没有这个地址" → 落到 `/not-found`（实测缺陷）。
      meta: { ...auth },
    },

    // 工作台。注意：`work_item` 读能力尚未登记在能力清单里（见 WorkbenchPage 说明），
    // 因此这一条**不加 requiredPermission**——加一个不存在的权限码会让页面永远打不开。
    listRoute('/workbench', () => import('./layers/product/workbench/WorkbenchPage.vue')),

    // 塘口详情：列表之外的独立页面，必须排在资源解析器之前。
    listRoute('/ponds/:id', () => import('./layers/product/ponds/PondDetailPage.vue')),

    // ★ 通用详情页。**一条动态路由覆盖所有资源**（与列表页同一手法）：
    //   `/detail/:resource/:id`，`resource` 是 `ResourceMeta.name`（如 `purchase_order`）。
    //
    //   为什么给它一个静态前缀：列表那条记录是 `/:resourcePath(.*)` 的**兜底匹配**，
    //   `/purchase-orders/151` 会被它整体吃掉、当成"不存在的资源页"。
    //   与其在解析器里加一套"末尾像不像 id"的猜测，不如给详情一个**明确的地址空间**
    //   ——「猜 URL 形状」与「猜资源名」同类，都是本项目明令禁止的。
    listRoute('/detail/:resource/:id', () => import('./layers/common/ui/ResourceDetailPage.vue')),

    // ★ 全部资源页的唯一入口。资源名由元数据推导（见 resolveResourceByPath）。
    {
      path: RESOURCE_ROUTE_PATH,
      component: () => import('./layers/common/ui/ResourcePage.vue'),
      meta: { ...auth },
    },

    {
      path: '/not-found',
      component: () => import('./layers/common/ui/NotFoundPage.vue'),
      meta: { ...auth },
    },
  ],
})

/**
 * 全局守卫：**鉴权 + 资源解析**。
 *
 * ## 资源解析为什么必须在这里，而不是路由记录的 `beforeEnter`
 *
 * 实测缺陷（用户报「切换了页面但还是停在上一页，要刷新一次才行」）：
 * `beforeEnter` 在**同一条路由记录内、仅 params 变化时不会重新触发** ——
 * 而本项目全部 19 个资源页**共用同一条记录** `/:resourcePath(.*)`。
 * 于是 `/ponds` -> `/materials` 时它**一次都没跑**（我在里面加日志验证过：
 * 整轮只出现 `path:"/ponds"` 一条），`meta.resource` 仍是旧值 `pond`，
 * 页面就停在上一份数据上。刷新之所以「能好」，是因为整页重载后首次进入
 * 会重新跑一次 —— 这正好掩盖了根因。
 *
 * 全局 `beforeEach` 对**每一次**导航都执行，所以解析放在这里才对。
 * 路由记录上不再挂 `beforeEnter` —— 同一件事只留一处。
 */
router.beforeEach(async (to) => {
  const decision = await decideNavigation(to)
  if (decision.redirect) return decision.redirect

  // 只有「资源页」那条记录需要解析；登录页 / 工作台 / 塘口详情 / not-found
  // 各有自己的组件与语义，不参与。
  if (!to.matched.some((record) => record.path === RESOURCE_ROUTE_PATH)) return undefined

  const path = `/${String(to.params.resourcePath ?? '')}`
  let resolved = resolveResourceByPath(path)
  if (!resolved) {
    // 元数据可能「尚未就绪」或「上一次加载失败」（`guard.ts` 刻意不阻断导航，
    // 且失败时会把 resources 清空）→ 强制重取一次再判。
    try {
      await reloadMeta()
    } catch {
      // 失败已记录在 meta.error；下面按「元数据不可用」处理。
    }
    resolved = resolveResourceByPath(path)
  }
  if (resolved) {
    // 把资源名写进 meta，供 ResourcePage 渲染。
    to.meta.resource = resolved
    return undefined
  }
  // ★ 关键区分：**元数据没拿到 ≠ 地址不存在**。
  // 前者是可重试的故障，说成「页面不存在」会把一次网络抖动伪装成 404
  // —— 那正是「大量页面同时显示页面不存在」的成因。
  const meta = useMeta()
  if (meta.resources.value.length === 0) {
    to.meta.resourceUnavailable = true
    return undefined
  }
  // 元数据**已就绪**且确实没有这个地址 ⇒ 这才是真 404。
  return { path: '/not-found' }
})
