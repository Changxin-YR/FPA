import { expect, test, type Page } from '@playwright/test'

/**
 * 元数据接口故障时的**降级行为** —— 一次真实事故的回归守卫。
 *
 * ## 事故
 * 用户报"大量页面同时显示『页面不存在』"。根因不是路由写错，而是**三件事叠加**：
 *   1. `guard.ts` 的 `loadMeta()` 出于"不要白屏"的考虑被 try/catch 包住、
 *      **不阻断导航**，失败时还会把 `resources` 清空；
 *   2. `router.ts` 的 `beforeEnter` 当时是**同步**的，读到空表就立刻
 *      `return { path: '/not-found' }`；
 *   3. 智能体一轮对话会长时间占住后端线程（实测 1–5 分钟），元数据请求排队超时。
 * 于是**一次网络/后端抖动被断言成"这个地址不存在"**，全站资源页一起 404，
 * 而且不会自愈（那条导航已经跳到 not-found 了）。
 *
 * ## 修法（本测试钉住的就是它）
 * **元数据拿不到 ≠ 地址不存在**：
 *   * `beforeEnter` 改为 async，解析失败时**强制重取一次**再判；
 *   * 元数据仍为空 → **不再判 404**，交给 `ResourcePage` 显示可重试的
 *     "暂时无法打开这个页面 + 重新加载"；
 *   * 只有元数据**已就绪**且确实没有该地址 —— 那才是真 404（/not-found）。
 *   * `serve_dev.py` 的 waitress 线程 8 → 32，消除后端被对话占满的级联。
 *
 * ## 为什么这里允许用 page.route
 * 本项目**反对**用 route 拦截去替换真实契约（那会掩盖前后端漂移，t7 的教训）。
 * 但这里是**故障注入**：真实链路仍然存在，只是把其中一个接口临时打坏，
 * 用来验证"坏了之后界面怎么表现"。这与"用桩替换契约"是两件事。
 */
async function login(page: Page): Promise<void> {
  await page.goto('/auth/login')
  await page.fill('[data-testid="login-identifier"]', 'demo')
  await page.fill('[data-testid="login-password"]', 'Demo1234!')
  await page.click('[data-testid="login-form"] button[type="submit"]')
  await expect(page).toHaveURL(/\/ponds$/)
}

test('元数据不可用：不得显示"页面不存在"，而要给可重试的降级页', async ({ page }) => {
  await login(page)
  // 故障注入：把元数据接口打坏
  await page.route('**/api/v1/meta/capabilities', (route) => route.abort())
  await page.goto('/materials', { waitUntil: 'domcontentloaded' })

  await expect(page.getByTestId('resource-unresolved')).toBeVisible()
  await expect(page.getByRole('button', { name: '重新加载' })).toBeVisible()
  // ★ 核心断言：这一次故障**不得**被断言成"地址不存在"。
  await expect(page.getByText('页面不存在')).toHaveCount(0)
})

test('点"重新加载"后应就地恢复出表格，无需刷新整页', async ({ page }) => {
  await login(page)
  let blockMeta = true
  await page.route('**/api/v1/meta/capabilities', (route) => (blockMeta ? route.abort() : route.continue()))
  await page.goto('/materials', { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('resource-unresolved')).toBeVisible()

  blockMeta = false
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.getByTestId('data-table')).toBeVisible()
})

test('元数据就绪时，真正不存在的地址仍然是 404', async ({ page }) => {
  await login(page)
  await page.goto('/definitely-not-a-registered-resource', { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('not-found')).toBeVisible()
})
