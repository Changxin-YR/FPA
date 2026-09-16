<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import AgentPanel from './layers/common/ui/AgentPanel.vue'
import AppNav from './layers/common/ui/AppNav.vue'
import { useMeta } from './layers/common/meta/meta.store'
import { createSessionStore } from './layers/common/session/session.store'

/**
 * 应用外壳：**导航 + 内容 + 智能助手**。
 *
 * ## t8 之前它只有一个 `<RouterView />`
 *
 * 后果是：用户登录后落在 `/ponds`，**没有任何入口能去别处**
 * —— 他问"只有一个功能吗？"。后端当时已有 69 条能力 / 23 个资源。
 *
 * ## 为什么导航由 `App.vue` 挂，而不是每个页面自己挂
 *
 * 挂在外壳上，导航就**只有一份**，且新页面天然带导航。
 * 若各页自挂，"12 个页面各自抄一次导航"就是本项目反复要消灭的形态。
 *
 * ## 智能助手同理（t9）
 *
 * `AgentPanel.vue` 与 `agent/service.ts` 早已写好，但**被零个组件引用** ——
 * 用户在界面上看不到、也点不到助手入口。它与"`HarnessSessionManager` 无人实例化"
 * 是**同一个缺陷的前后端两半**：实现存在、装配缺失，而所有测试都是绿的。
 * 所以这里也挂在外壳上：一份入口，新页面天然带上。
 *
 * ## 登录页**不**显示导航与助手
 *
 * 登录时还没有身份，导航里的条目全都是"需要身份才能看"的东西；
 * 助手更直接：`/agent/turns` 要会话 Cookie，未登录必然 401。
 * 判据用路由的 `meta.guestOnly`（与 `guard.ts` 同一个字段），**不另立一份名单**。
 */
const route = useRoute()
const meta = useMeta()
const session = createSessionStore()
const isGuestPage = computed(() => route.meta.guestOnly === true)
const showNav = computed(() => !isGuestPage.value)
/** 当前页面路径，作为 `page_context` 传给后端：模型因此知道用户在哪个页面。 */
const pageContext = computed(() => route.fullPath)
const pageTitle = computed(() => {
  const resource = String(route.meta.resource ?? '')
  if (resource) return meta.resourcesByName.value.get(resource)?.title ?? '业务详情'
  if (route.path === '/workbench') return '工作台'
  if (route.path.startsWith('/detail/')) return '业务详情'
  if (route.path.startsWith('/ponds/')) return '塘口详情'
  return '业务工作区'
})
const mobileNavOpen = ref(false)

function toggleMobileNav(): void {
  mobileNavOpen.value = !mobileNavOpen.value
}

function closeMobileNav(): void {
  mobileNavOpen.value = false
}
</script>

<template>
  <div class="app-shell" :class="{ 'app-shell--bare': !showNav }">
    <template v-if="showNav">
      <AppNav :class="{ 'app-nav--mobile-open': mobileNavOpen }" @navigate="closeMobileNav" />
      <button
        v-if="mobileNavOpen"
        class="app-shell__backdrop"
        type="button"
        aria-label="关闭导航"
        @click="closeMobileNav"
      />

      <section class="app-shell__workspace">
        <header class="app-shell__header">
          <button
            class="mobile-nav-toggle"
            type="button"
            :aria-expanded="mobileNavOpen"
            :aria-label="mobileNavOpen ? '关闭导航' : '打开导航'"
            @click="toggleMobileNav"
          >
            <span aria-hidden="true">{{ mobileNavOpen ? '×' : '☰' }}</span>
          </button>
          <div class="app-shell__breadcrumb">
            <span>渔芯运营中心</span>
            <i aria-hidden="true">/</i>
            <strong>{{ pageTitle }}</strong>
          </div>
          <div v-if="session.user.value" class="app-shell__user">
            <span class="app-shell__avatar" aria-hidden="true">{{
              session.user.value.name.slice(0, 1)
            }}</span>
            <span>
              <strong>{{ session.user.value.name }}</strong>
              <small>{{ session.user.value.roles[0] || '业务成员' }}</small>
            </span>
          </div>
        </header>
        <main class="app-shell__content">
          <RouterView />
        </main>
      </section>
      <AgentPanel :page-context="pageContext" />
    </template>

    <main v-else class="app-shell__content app-shell__content--bare">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.app-shell {
  display: flex;
  min-height: 100vh;
  background: var(--tone-bg);
}
.app-shell--bare {
  display: block;
}
.app-shell__workspace {
  display: flex;
  flex: 1;
  min-width: 0;
  min-height: 100vh;
  margin-left: var(--tone-nav-width);
  flex-direction: column;
}
.app-shell__header {
  position: sticky;
  top: 0;
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  height: var(--tone-header-height);
  padding: 0 28px;
  border-bottom: 1px solid var(--tone-line);
  background: var(--tone-header-surface);
  box-shadow: var(--tone-shadow-header);
}
.app-shell__breadcrumb {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
  margin-right: auto;
  color: var(--tone-muted);
  font-size: 12px;
  font-weight: 600;
}
.app-shell__breadcrumb i {
  color: var(--tone-line-strong);
  font-style: normal;
}
.app-shell__breadcrumb strong {
  overflow: hidden;
  color: var(--tone-ink);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.app-shell__user {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}
.app-shell__user strong,
.app-shell__user small {
  display: block;
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.app-shell__user strong {
  color: var(--tone-ink);
  font-size: 13px;
}
.app-shell__user small {
  margin-top: 2px;
  color: var(--tone-muted);
  font-size: 11px;
}
.app-shell__avatar {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: var(--tone-radius);
  color: var(--tone-on-primary);
  background: var(--tone-primary);
  font-size: 13px;
  font-weight: 700;
}
.mobile-nav-toggle {
  display: none;
  width: 34px;
  height: 34px;
  padding: 0;
  border: 1px solid var(--tone-line);
  border-radius: var(--tone-radius);
  color: var(--tone-ink-soft);
  background: var(--tone-surface);
  font-size: 20px;
  line-height: 1;
}
.app-shell__backdrop {
  display: none;
}
.app-shell__content {
  flex: 1;
  min-width: 0;
  background: var(--tone-bg);
}
.app-shell__content--bare {
  min-height: 100vh;
}

@media (max-width: 900px) {
  .app-shell__workspace {
    margin-left: 0;
  }
  .app-shell__header {
    height: 56px;
    padding: 0 16px;
  }
  .mobile-nav-toggle {
    display: grid;
    place-items: center;
  }
  .app-shell__backdrop {
    position: fixed;
    z-index: 39;
    inset: 0;
    display: block;
    width: 100%;
    height: 100%;
    padding: 0;
    border: 0;
    background: var(--tone-overlay);
  }
}

@media (max-width: 560px) {
  .app-shell__breadcrumb > span,
  .app-shell__breadcrumb > i,
  .app-shell__user small {
    display: none;
  }
  .app-shell__user strong {
    max-width: 88px;
  }
}
</style>
