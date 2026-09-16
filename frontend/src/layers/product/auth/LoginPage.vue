<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ActionButton from '../../common/ui/ActionButton.vue'
import { api } from '../../common/api/client'
import { errorText, submitErrorText } from '../../common/api/errors'
import { createSessionStore } from '../../common/session/session.store'
import type { UserSummary } from '../../common/api/models'

/**
 * 登录页。
 *
 * 与早期版本 `layers/product/auth/LoginPage.vue`（61 行）的差别：
 *  - 登录成功后**不再自己拼跳转路径**，交给 `session.nextPath` 派生
 *    （早期版本把 `pathByStatus` 写在 `session.store.ts:6-8`，但页面另有硬编码跳转，
 *    属于 §P-28 的「路由字面量散落」）；
 *  - 用 `useSubmitGuard` 的等价物：`busy` 一旦为真就阻止重复提交
 *    （早期版本有 15 处手写 `submitting = ref(false)`，见问题 P-21）。
 *
 * ## t7：本页曾有三处契约断裂，都在这里说明，免得再犯
 *
 * 1. **CSRF 死锁** —— 调登录时原先**没传 `skipCsrf`**，于是 `client.ts` 把 POST
 *    当写方法、先去 `GET /api/v1/auth/csrf` 取令牌，而那个端点要求已有会话 → 401
 *    → 用户**还没点登录**就看到「CSRF Token 获取失败」。修法见下面的调用点。
 * 2. **字段名** —— 发的是 `identifier`（正确，registry §2.3），但后端当时读
 *    `username`，于是拿到空串。**前端是对的**，改的是后端。
 * 3. **响应形状** —— 原先读 `data.user`，而后端返回平铺对象。
 *    现在服务端返回的正是 `{user: UserSummary}`（`routes_auth.py::_user_payload`），
 *    且与本页的类型标注**逐字一致**。
 */

const router = useRouter()
const route = useRoute()
const session = createSessionStore()

/** 改密页无法自动重登时会回到这里（`?passwordChanged=1`），给一句明确的话。 */
const notice = ref(route.query.passwordChanged === '1' ? '密码已修改，请用新密码登录' : '')

const identifier = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')

async function submit(): Promise<void> {
  if (busy.value) return
  if (!identifier.value.trim() || !password.value) {
    error.value = '请输入账号和密码'
    return
  }

  busy.value = true
  error.value = ''
  try {
    /*
     * `skipCsrf: true` 是必须的，理由不是"省一次请求"。
     *
     * `/api/v1/auth/login` 已经在服务端 `web/app.py::_CSRF_EXEMPT` 豁免清单里
     * （理由："登录时尚未持有会话，无从取得令牌"）。而 `client.ts` 是按**方法**
     * 判定要不要预取令牌的：POST ⇒ 是写方法 ⇒ 先取令牌。
     * **前端不该为一个已豁免的端点预取令牌** —— 那正是死锁的来源。
     *
     * 类型用 `UserSummary`（`common/api/models.ts`）而不是就地写一个字面量类型：
     * 就地写就是"同一个形状的第二处描述"，而这次的缺陷正是两处描述不一致
     * （本页只列了 id/name/roles/permissions，而 store 另外还认 `status`）。
     *
     * 注意 `skipCsrf` 同时会跳过自动幂等键（`client.ts` 的 `isWrite && !skipCsrf`）——
     * 对登录是**正确**的：登录没有幂等语义，registry 里 `auth.login` 也是
     * `idempotent=false`。不要为了给它带幂等键而在这里改 `skipCsrf`。
     */
    const data = await api.post<{ user: UserSummary }>(
      '/api/v1/auth/login',
      { identifier: identifier.value.trim(), password: password.value },
      { skipCsrf: true },
    )
    session.setUser(data.user)
    // 记住登录名：改密页在改密后要**自动用新密码重新登录**（服务端会作废旧会话），
    // 没有它就只能把用户退回登录页再输一遍账号。
    try {
      sessionStorage.setItem('fpa:login-identifier', identifier.value.trim())
    } catch {
      // 隐私模式禁用 storage：不是致命问题，只是改密后要手输一次账号。
    }
    await router.replace({ path: session.nextPath.value })
  } catch (caught) {
    error.value = submitErrorText(caught, errorText(caught, '登录失败，请稍后重试'))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="login">
    <form class="login__card" data-testid="login-form" @submit.prevent="submit">
      <h1>渔芯AI水产养殖一体化系统</h1>
      <p class="login__subtitle">请使用您的账号登录</p>

      <label class="login__field">
        <span>账号</span>
        <input
          v-model="identifier"
          type="text"
          autocomplete="username"
          data-testid="login-identifier"
          :disabled="busy"
        />
      </label>

      <label class="login__field">
        <span>密码</span>
        <input
          v-model="password"
          type="password"
          autocomplete="current-password"
          data-testid="login-password"
          :disabled="busy"
        />
      </label>

      <p v-if="error" class="login__error" role="alert" data-testid="login-error">{{ error }}</p>
      <p v-else-if="notice" class="login__notice" role="status" data-testid="login-notice">{{ notice }}</p>

      <ActionButton type="submit" variant="primary" :loading="busy" label="登录">
        {{ busy ? '登录中…' : '登录' }}
      </ActionButton>

      <!--
        演示账号提示。放在按钮**下方**而不是输入框之间：登录是本页唯一的主任务，
        提示不该插在"填完 → 提交"这条路径中间抢注意力；靠层级（淡底 + 细边）而不是
        颜色区分就够（`styles/tokens.css` 的"商务冷静"方向）。
        值与 `tools/seed_acceptance.py::ACCOUNT_DEFS` 里 demo 那条一致，有守卫
        （`frontend/tests/pages-and-actions.spec.ts`）。
      -->
      <aside class="login__demo" data-testid="login-demo">
        <p class="login__demo-title">演示账号</p>
        <dl class="login__demo-list">
          <div class="login__demo-row">
            <dt>账号</dt>
            <dd data-testid="login-demo-account">demo</dd>
          </div>
          <div class="login__demo-row">
            <dt>密码</dt>
            <dd data-testid="login-demo-password">Demo1234!</dd>
          </div>
        </dl>
      </aside>
    </form>
  </main>
</template>

<style scoped>
.login {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 24px;
}
.login__card {
  display: grid;
  gap: 14px;
  width: min(420px, 100%);
  padding: 28px 32px;
  border: 1px solid var(--tone-line);
  border-radius: 14px;
  background: var(--tone-surface);
}
.login__card h1 {
  margin: 0;
  font-size: 20px;
}
.login__subtitle {
  margin: 0;
  color: var(--tone-muted);
  font-size: 14px;
}
.login__field {
  display: grid;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--tone-ink-soft);
}
.login__field input {
  min-height: 40px;
  padding: 8px 12px;
  border: 1px solid var(--tone-line);
  border-radius: var(--tone-radius);
  font-weight: 400;
}
.login__notice {
  margin: 0;
  color: var(--tone-success);
  font-size: 13px;
}
.login__error {
  margin: 0;
  color: var(--tone-danger);
  font-size: 13px;
}

/* 演示账号提示：刻意做"轻"——淡底 + 细边 + 令牌灰字，不与主按钮抢焦点。
   口令用 `user-select: all`，点一下就能整串选中，省掉手抄。 */
.login__demo {
  padding: 10px 12px;
  border: 1px solid var(--tone-line);
  border-radius: var(--tone-radius);
  background: var(--tone-surface-soft);
}
.login__demo-title {
  margin: 0 0 6px;
  color: var(--tone-muted);
  font-size: 12px;
  font-weight: 600;
}
.login__demo-list {
  display: grid;
  gap: 4px;
  margin: 0;
  font-size: 13px;
}
.login__demo-row {
  display: flex;
  gap: 10px;
}
.login__demo-row dt {
  flex: 0 0 32px;
  color: var(--tone-muted);
}
.login__demo-row dd {
  margin: 0;
  color: var(--tone-ink);
  font-weight: 500;
  user-select: all;
}
</style>
