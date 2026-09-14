<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import ActionButton from './ActionButton.vue'
import { reloadMeta, resolveResourceByPath, useMeta } from '../meta/meta.store'
import ResourceListPage from './ResourceListPage.vue'
import { errorText } from '../api/errors'

/**
 * 通用资源页：**一个组件覆盖全部资源**。
 *
 * ## 它为什么取代了 12 个「每个资源一个文件」
 *
 * 原先每个资源都有一份 7 行的包装组件，唯一差异就是那个资源名，而
 * **只有 14 个资源有路由** —— 服务端实际有 **23 个资源**。于是用户在界面上
 * 没有任何入口能到其余资源（这正是"只有一个功能吗？"的来源）。更糟的是那些
 * 包装件**已经漂移**：`AuditLogPage.vue` 写 `resource="audit.log"`，而服务端
 * 资源名是 `audit_log` —— 一个查不到、也不报错的错。
 *
 * 那 12 个文件表达的信息（"这个路由对应哪个资源"）**服务端元数据里本来就有**
 * （`ResourceMeta.list_path`）。所以它不是页面数据，而是**路由数据**，
 * 现在只住在 `meta.store.ts::resolveResourceByPath()` 一处。
 *
 * ## 资源名从哪来
 *
 * 优先取 `route.meta.resource`（由 `router.ts` 解析路径时按元数据写入），
 * **不是**从 URL 里猜；重试成功后回退到就地解析值（见 `retry()`）。
 *
 * ## 新增一个资源要改什么
 *
 * 什么都不用改：服务端加一条 `Resource(...)` + 一条读能力 →
 * 导航自动出现 → 点进去就是这个组件。判据见 `frontend/tests/nav.spec.ts`。
 */
const route = useRoute()
const meta = useMeta()

/** 重试成功后写在这里（路由的 `meta.resource` 不会因重试而改变）。 */
const retried = ref('')

/** 资源名优先取路由写入的 `meta.resource`，其次用重试时就地解析出的值。 */
const resource = computed(() => String(route.meta.resource ?? '') || retried.value)

const unresolved = computed(() => resource.value === '')

const retrying = ref(false)
const retryFailed = ref('')

/**
 * 重新拉元数据并**就地重解析**。
 *
 * ## 为什么必须有这个按钮（一次真实事故）
 *
 * 用户报"大量页面同时显示『页面不存在』"。根因是 `router.ts` 的 `beforeEnter`
 * 在元数据**尚未就绪或刚加载失败**时读到空表，就把一次网络故障断言成
 * "地址不存在"。正确的区分是：**元数据拿不到 ≠ 地址不存在**——
 * 前者可重试，后者才是 404。
 *
 * 路由层已改为：元数据为空时**不再判 404**，把页面交到这里。所以这一屏的语义
 * 是"**暂时打不开**"，不是"不存在"——文案与按钮都必须说清楚，否则用户会以为
 * 资源没登记，去查一个并不存在的问题。
 */
async function retry(): Promise<void> {
  retrying.value = true
  retryFailed.value = ''
  try {
    await reloadMeta()
    retried.value = resolveResourceByPath(route.path)
    if (!retried.value) {
      retryFailed.value = '服务端仍未登记与当前地址对应的资源。'
    }
  } catch (caught) {
    retryFailed.value = errorText(caught, '元数据加载失败')
  } finally {
    retrying.value = false
  }
}
</script>

<template>
  <ResourceListPage v-if="!unresolved" :resource="resource" />
  <main v-else class="resource-page__error" role="alert" data-testid="resource-unresolved">
    <h1>暂时无法打开这个页面</h1>
    <p>
      没能取到服务端的资源清单，因此无法把当前地址对应到某个资源。
      <strong>这不代表该地址不存在</strong>——通常是网络或后端一时的故障。
    </p>
    <p v-if="meta.error.value" class="resource-page__error-detail">原因：{{ meta.error.value }}</p>
    <p v-if="retryFailed" class="resource-page__error-detail">{{ retryFailed }}</p>
    <ActionButton variant="primary" :loading="retrying" @click="retry">重新加载</ActionButton>
  </main>
</template>

<style scoped>
.resource-page__error {
  padding: 24px;
}
.resource-page__error h1 {
  margin-bottom: 8px;
  color: var(--tone-ink);
  font-size: 20px;
  font-weight: 600;
}
.resource-page__error p {
  margin: 0 0 10px;
  max-width: 640px;
  color: var(--tone-ink-soft);
  font-size: 13px;
  line-height: 1.7;
}
.resource-page__error-detail {
  color: var(--tone-muted);
  font-size: 12px;
}
</style>
