<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { useMeta } from '../meta/meta.store'
import { createSessionStore } from '../session/session.store'

/**
 * 侧边导航。**条目完全由服务端元数据生成，前端不手写任何菜单表。**
 *
 * ## 它为什么必须存在（一次实测缺陷）
 *
 * 后端有 69 条能力 / 23 个资源，而前端只有 14 条资源路由、且 `App.vue` 只有一个
 * `<RouterView />` —— 全项目**没有任何导航组件**。后果是用户登录后落在 `/ponds`，
 * **没有任何入口能去别处**，于是他问"只有一个功能吗？"。
 *
 * ## 判据：服务端新增一个资源，前端改零行代码
 *
 * 条目来自 `meta.navItems()`（见它的 docstring，含"为什么有读能力才出现"
 * 与"权限过滤为什么只过了一次"）。这里**只做渲染**：
 *
 *   * 不从 URL 里猜资源名；
 *   * 不写 `{ pond: '塘口', ... }` 这类映射；
 *   * 不自己算权限码。
 *
 * ## 按 `module` 分组
 *
 * 逐个排 19 个入口会让侧栏变成一长条。分组键用服务端给的 `module`
 * （它本来就是"这个资源属于哪个域"），**顺序按首次出现的顺序**固定下来，
 * 不用字母序——域的顺序是服务端的声明顺序，前端不该另定一套。
 */
const meta = useMeta()
const route = useRoute()
const session = createSessionStore()
const emit = defineEmits<{ navigate: [] }>()

const items = computed(() => meta.navItems())

/**
 * 按业务域分组，保持服务端给的顺序。
 *
 * 分组的**键**用域码（`module`），但**组标题**用服务端给的中文（`moduleTitle`）
 * —— 两者分开是为了不让一个字段承担两种含义：
 *
 *   `key`   用于 `data-testid` 与去重（机器用，稳定）
 *   `title` 用于渲染（人用，可翻译）
 *
 * 实测缺陷：先前只用 `module` 渲染，界面上出现了 `ACCESS` / `AUDIT` /
 * `MASTER_DATA` 这样的机器码（`MASTER_DATA` 还把内部下划线风格暴露给了用户）。
 */
const groups = computed(() => {
  const order: string[] = []
  const byModule = new Map<string, { title: string; items: typeof items.value }>()
  for (const item of items.value) {
    const key = item.module || 'other'
    if (!byModule.has(key)) {
      byModule.set(key, { title: item.moduleTitle || key, items: [] })
      order.push(key)
    }
    byModule.get(key)!.items.push(item)
  }
  return order.map((key) => ({
    key,
    title: byModule.get(key)!.title,
    items: byModule.get(key)!.items,
  }))
})
const closedGroups = ref(new Set<string>())

function toggleGroup(key: string): void {
  const next = new Set(closedGroups.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  closedGroups.value = next
}

/** 当前路径是否命中该条目（用于高亮）。 */
function isActive(path: string): boolean {
  const current = route.path
  return current === path || current.startsWith(`${path}/`)
}

function logout(): void {
  void (async () => {
    try {
      const { api } = await import('../api/client')
      await api.post('/api/v1/auth/logout', undefined, { skipCsrf: false })
    } catch {
      // 登出失败也把本地会话清掉：用户想离开就必须能离开。
    } finally {
      session.clear()
      // ★ 必须带部署前缀：线上应用挂在 `/fpa/` 下，写死 `/auth/login` 会被 nginx
      //   当成站点根路径，直接 404（实测反馈："退出登录出现 404"）。
      //   `import.meta.env.BASE_URL` 由 Vite 的 `base` 提供（部署时 `/fpa/`）。
      const base = String(import.meta.env.BASE_URL || '/').replace(/\/$/, '')
      window.location.assign(`${base}/auth/login`)
    }
  })()
}
</script>

<template>
  <aside class="app-nav" data-testid="app-nav">
    <RouterLink class="app-nav__brand" to="/workbench" aria-label="返回工作台" @click="emit('navigate')">
      <span class="app-nav__brand-mark" aria-hidden="true">渔</span>
      <span>
        <strong class="app-nav__title">渔芯AI水产养殖一体化系统</strong>
        <small>养殖运营平台</small>
      </span>
    </RouterLink>

    <nav class="app-nav__sections">
      <RouterLink
        class="app-nav__link app-nav__workbench"
        :class="{ 'app-nav__link--active': isActive('/workbench') }"
        to="/workbench"
        data-testid="nav-workbench"
        @click="emit('navigate')"
      >
        <span class="app-nav__link-mark" aria-hidden="true">⌂</span>
        工作台
      </RouterLink>
      <section
        v-for="group in groups"
        :key="group.key"
        class="app-nav__group"
        :data-testid="`nav-group-${group.key}`"
        :data-module="group.key"
      >
        <button
          class="app-nav__group-toggle"
          type="button"
          :aria-expanded="!closedGroups.has(group.key)"
          @click="toggleGroup(group.key)"
        >
          <span>{{ group.title }}</span>
          <span class="app-nav__caret" aria-hidden="true">⌄</span>
        </button>
        <ul v-show="!closedGroups.has(group.key)" class="app-nav__list">
          <li v-for="item in group.items" :key="item.name">
            <RouterLink
              class="app-nav__link"
              :class="{ 'app-nav__link--active': isActive(item.path) }"
              :to="item.path"
              :data-testid="`nav-item-${item.name}`"
              @click="emit('navigate')"
            >
              {{ item.title }}
            </RouterLink>
          </li>
        </ul>
      </section>
    </nav>

    <footer class="app-nav__footer">
      <p v-if="session.user.value" class="app-nav__user" data-testid="nav-user">
        <span aria-hidden="true" />{{ session.user.value.name }}
      </p>
      <button class="app-nav__logout" type="button" data-testid="nav-logout" @click="logout">退出登录</button>
    </footer>
  </aside>
</template>

<style scoped>
.app-nav {
  position: fixed;
  z-index: 40;
  inset: 0 auto 0 0;
  display: flex;
  flex-direction: column;
  width: var(--tone-nav-width);
  flex: 0 0 var(--tone-nav-width);
  height: 100vh;
  /* 侧栏自身不滚动：滚动交给 `.app-nav__sections`（只有菜单区需要滚）。
     两层都 `auto` 会同时长出两条原生滚动条，右边缘那条正是用户报的"边框"。 */
  overflow: hidden;
  padding: 18px 12px 14px;
  color: var(--tone-nav-ink);
  background: var(--tone-nav-bg);
  box-shadow: 6px 0 20px var(--tone-nav-shadow);
}
.app-nav__brand {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 48px;
  padding: 0 8px 16px;
  border-bottom: 1px solid var(--tone-nav-line);
  color: inherit;
  text-decoration: none;
}
.app-nav__brand-mark {
  display: grid;
  place-items: center;
  flex: 0 0 34px;
  width: 34px;
  height: 34px;
  border: 1px solid var(--tone-nav-line-strong);
  border-radius: var(--tone-radius);
  color: var(--tone-nav-ink);
  background: var(--tone-primary);
  font-size: 17px;
  font-weight: 700;
}
.app-nav__title {
  display: block;
  font-size: 15px;
  font-weight: 700;
  color: var(--tone-nav-ink);
  white-space: nowrap;
}
.app-nav__brand small {
  display: block;
  margin-top: 3px;
  color: var(--tone-nav-muted);
  font-size: 11px;
}
.app-nav__user {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  font-size: 12px;
  color: var(--tone-nav-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.app-nav__user span {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--tone-success-light);
}
.app-nav__sections {
  flex: 1;
  overflow-y: auto;
  /* 隐藏原生滚动条（用户报"右侧那条边框"）。菜单仍可用滚轮/触控板滚动，
     `scrollbar-width: none` 只去掉滚动条本身、不改变可滚动性。 */
  scrollbar-width: none;
  -ms-overflow-style: none;
  padding: 12px 0;
}
.app-nav__sections::-webkit-scrollbar {
  width: 0;
  height: 0;
}
.app-nav__group + .app-nav__group {
  margin-top: 10px;
}
.app-nav__group-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-height: 30px;
  padding: 0 10px;
  border: 0;
  color: var(--tone-nav-muted);
  background: transparent;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  text-align: left;
}
.app-nav__caret {
  font-size: 15px;
  transition: transform var(--tone-motion);
}
.app-nav__group-toggle[aria-expanded='false'] .app-nav__caret {
  transform: rotate(-90deg);
}
.app-nav__list {
  margin: 0;
  padding: 0;
  list-style: none;
}
/* ★ `white-space: nowrap` 是必须的：先前 200px 宽 + 不能换行克制，
   菜单项文字折成两行是上一版最显眼的缺陷之一。 */
.app-nav__link {
  position: relative;
  display: flex;
  align-items: center;
  height: 38px;
  gap: 8px;
  padding: 0 10px;
  border-radius: var(--tone-radius);
  font-size: 13.5px;
  color: var(--tone-nav-muted);
  text-decoration: none;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition:
    background-color var(--tone-motion),
    color var(--tone-motion);
}
.app-nav__link:hover {
  color: var(--tone-nav-ink);
  background: var(--tone-nav-hover);
}
.app-nav__link--active {
  color: var(--tone-nav-ink);
  background: var(--tone-nav-active);
  font-weight: 600;
}
/* 激活项的左侧 2px 强调条（早期版本是 inset box-shadow，这里用伪元素，
   好处是不占布局宽度、也不会被圆角裁掉）。 */
.app-nav__link--active::before {
  position: absolute;
  top: 7px;
  bottom: 7px;
  left: 0;
  width: 2px;
  border-radius: 0 2px 2px 0;
  background: var(--tone-success-light);
  content: '';
}
.app-nav__workbench {
  margin-bottom: 12px;
}
.app-nav__link-mark {
  width: 16px;
  color: currentColor;
  font-size: 16px;
  text-align: center;
}
.app-nav__footer {
  display: grid;
  gap: 10px;
  padding: 12px 4px 0;
  border-top: 1px solid var(--tone-nav-line);
}
.app-nav__logout {
  height: var(--tone-control-height);
  border: 1px solid var(--tone-nav-line-strong);
  border-radius: var(--tone-radius);
  color: var(--tone-nav-ink);
  background: var(--tone-nav-bg-strong);
  font-size: 13px;
  cursor: pointer;
  transition:
    border-color var(--tone-motion),
    color var(--tone-motion);
}
.app-nav__logout:hover {
  border-color: var(--tone-nav-muted);
}

@media (max-width: 900px) {
  .app-nav {
    transform: translateX(-100%);
    transition: transform 180ms ease;
  }
  .app-nav.app-nav--mobile-open {
    transform: translateX(0);
  }
}
</style>
