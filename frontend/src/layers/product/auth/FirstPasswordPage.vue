<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../../common/api/client'
import { errorText, submitErrorText } from '../../common/api/errors'
import { createSessionStore } from '../../common/session/session.store'

/**
 * 首次登录改密页（`/auth/first-password`）。
 *
 * ## 它补的是哪条缺口（实测）
 *
 * 服务端给新建账号的 `status` 是 `must_change_password`（`access.user.create`
 * 的初始状态），而 `session.store.ts::PATH_BY_STATE` 把它映射到
 * `/auth/first-password` —— 但**这条路由根本不存在**。
 * 于是新账号登录后走到兜底路由，再被守卫判成"元数据已就绪且没有这个地址"，
 * 落到 `/not-found`：**用户第一次登录看到的是「页面不存在」**（实测 `qa-checker`）。
 *
 * ## 契约
 *
 * 调用 `auth.password.change`（`POST /api/v1/auth/password/change`，
 * 字段 `current_password` / `new_password` / `confirm_password`）。
 * 服务端在改密时会**作废当前会话**，所以改完用新密码自动重登一次
 * （账号名在登录页记进 `sessionStorage`）；实在登不上才退回登录页并说明原因。
 */
const router = useRouter()
const session = createSessionStore()

const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const busy = ref(false)
const error = ref('')

async function submit(): Promise<void> {
  if (busy.value) return
  if (!currentPassword.value || !newPassword.value) {
    error.value = '请填写当前密码与新密码'
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    error.value = '两次输入的新密码不一致'
    return
  }
  busy.value = true
  error.value = ''
  try {
    await api.post('/api/v1/auth/password/change', {
      current_password: currentPassword.value,
      new_password: newPassword.value,
      confirm_password: confirmPassword.value,
    })
    let identifier = ''
    try {
      identifier = sessionStorage.getItem('fpa:login-identifier') ?? ''
    } catch {
      identifier = ''
    }
    if (identifier) {
      try {
        const data = await api.post<{ user: never }>(
          '/api/v1/auth/login',
          { identifier, password: newPassword.value },
          { skipCsrf: true },
        )
        session.setUser(data.user)
        await router.replace({ path: session.nextPath.value })
        return
      } catch {
        // 自动重登失败不吞掉"密码已改"这个事实：退回登录页并说明。
      }
    }
    await router.replace({ path: '/auth/login', query: { passwordChanged: '1' } })
  } catch (caught) {
    error.value = submitErrorText(caught, errorText(caught, '修改密码失败，请稍后重试'))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="first-password">
    <form class="first-password__card" data-testid="first-password-form" @submit.prevent="submit">
      <h1>渔芯AI水产养殖一体化系统</h1>
      <p class="first-password__subtitle">首次登录需要修改初始密码</p>

      <label class="first-password__field">
        <span>当前密码</span>
        <input
          v-model="currentPassword"
          type="password"
          autocomplete="current-password"
          data-testid="first-current"
          :disabled="busy"
        />
      </label>
      <label class="first-password__field">
        <span>新密码</span>
        <input
          v-model="newPassword"
          type="password"
          autocomplete="new-password"
          data-testid="first-new"
          :disabled="busy"
        />
      </label>
      <label class="first-password__field">
        <span>确认新密码</span>
        <input
          v-model="confirmPassword"
          type="password"
          autocomplete="new-password"
          data-testid="first-confirm"
          :disabled="busy"
        />
      </label>

      <p v-if="error" class="first-password__error" role="alert" data-testid="first-error">{{ error }}</p>

      <button class="first-password__submit" type="submit" :disabled="busy" data-testid="first-submit">
        {{ busy ? '提交中…' : '修改密码并进入系统' }}
      </button>
    </form>
  </main>
</template>
<style scoped>
.first-password {
  display: grid;
  place-items: center;
  min-height: 100vh;
  padding: 24px;
  background: var(--tone-surface-soft);
}
.first-password__card {
  display: grid;
  gap: 12px;
  width: min(380px, 100%);
  padding: 28px 26px;
  border-radius: var(--tone-radius);
  background: var(--tone-surface);
  box-shadow: var(--tone-shadow-card);
}
.first-password__card h1 {
  margin: 0;
  font-size: 20px;
}
.first-password__subtitle {
  margin: 0 0 6px;
  color: var(--tone-muted);
  font-size: 13px;
}
.first-password__field {
  display: grid;
  gap: 6px;
  font-size: 13px;
  color: var(--tone-ink-soft);
}
.first-password__field input {
  height: var(--tone-control-height);
  padding: 0 10px;
  border: 1px solid var(--tone-line);
  border-radius: var(--tone-radius-sm);
  font-size: 14px;
}
.first-password__error {
  margin: 0;
  color: var(--tone-danger);
  font-size: 13px;
}
.first-password__submit {
  height: var(--tone-control-height);
  margin-top: 4px;
  border: 0;
  border-radius: var(--tone-radius-sm);
  color: var(--tone-on-primary);
  background: var(--tone-primary);
  font-size: 14px;
  cursor: pointer;
}
.first-password__submit:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
