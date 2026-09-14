import 'vue-router'

/**
 * 路由 `meta` 的类型声明。
 *
 * 为什么需要它：动态资源路由在 `beforeEnter` 里把**解析出的资源名**
 * 写进 `to.meta.resource`，由 `ResourcePage.vue` 读取。
 * `RouteMeta` 默认是空接口，不声明就是 `any`—— 而一个 `any`
 * 会让拼错字段名（`resource` 写成 `resouce`）在**运行期**才暴露。
 *
 * 同样声明 `requiredPermission` —— 它虽然在 t8 后只剩下工作台用到，
 * 但 `guard.ts` 仍按这个字段判定，而且它原本就是隐式 `any`。
 */
declare module 'vue-router' {
  interface RouteMeta {
    /** 仅已登录可访（未登录重定向登录页）。 */
    authOnly?: boolean
    /** 仅未登录可访（已登录则转向落地页）。 */
    guestOnly?: boolean
    /** 本页依赖的权限码（取自能力清单，前端不自行发明）。 */
    requiredPermission?: string
    /** 动态资源路由解析出的资源名（与 `ResourceMeta.name` 同命名空间）。 */
    resource?: string
  }
}

export {}
