import { computed, ref } from 'vue'
import { api } from '../api/client'
import { ApiError } from '../api/errors'
import type { UserSummary } from '../api/models'

/**
 * 会话与当前用户。
 *
 * 移植自早期版本 `layers/common/session/session.store.ts`（25 行，逻辑简单正确）。
 * 保留它的两处好设计：
 *   - `nextPath` 是 computed，由用户状态派生跳转目标，避免在守卫里写 if-else 链；
 *   - `load()` 只在明确的鉴权类错误码上清空 user，其它错误（如 500）保留会话，
 *     避免一次后端抖动就把用户踢下线。
 */
const user = ref<UserSummary | null>(null)
const loading = ref(false)

/**
 * 状态 → 落地页。`unauthenticated` 表示只能回登录页。
 *
 * ## ⚠️ 三个目标页在 `router.ts` 里**都还不存在**（t7 实测发现的既有缺口）
 *
 * 这张表是**契约意图**（它来自早期版本 `session.store.ts:6-8`），但 `router.ts` 只定义了
 * `/auth/login`；`/auth/first-password` 与 `/auth/pending` 没有对应路由，
 * 于是它们会被 `{ path: '/:pathMatch(.*)*', redirect: '/workbench' }` 兜住 ——
 * **`must_change_password` 的用户会被放进业务页，而不是改密页**。
 *
 * 这不是本次 t7 要修的东西（那三个页面属于本阶段明确不做的"前端业务页面"），
 * 但必须写在这里：**它会静默**。把三个页面做出来之前，这段注释就是唯一的提示；
 * 做了之后请把这段注释删掉（"能表达成一处的事实不留第二处"）。
 *
 * 客户端的 `PATH_BY_STATE` 由**服务端**下发的 `status` 驱动（服务端把
 * `must_change_password` 布尔列折叠进 `status`，见 `access/service.py::to_summary`），
 * 所以这一侧不需要改。
 */
const PATH_BY_STATE: Record<string, string> = {
  must_change_password: '/auth/first-password',
  pending: '/auth/pending',
  disabled: '/auth/login',
}

export function createSessionStore() {
  const nextPath = computed(() => {
    if (!user.value) return '/auth/login'
    return PATH_BY_STATE[user.value.status ?? ''] ?? '/ponds'
  })

  function setUser(value: UserSummary | null): void {
    user.value = value
  }

  async function load(): Promise<UserSummary | null> {
    loading.value = true
    try {
      const data = await api.get<{ user: UserSummary }>('/api/v1/auth/me')
      user.value = data.user
      return data.user
    } catch (error) {
      // 只有明确的鉴权类错误才清会话；500/网络错误保留，避免抖动踢人。
      if (error instanceof ApiError && ['UNAUTHENTICATED', 'CSRF_INVALID'].includes(error.code)) {
        user.value = null
      }
      return null
    } finally {
      loading.value = false
    }
  }

  function clear(): void {
    user.value = null
  }

  return { user, loading, nextPath, setUser, load, clear }
}
