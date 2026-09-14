import { expect, test, type Page } from '@playwright/test'

/**
 * SPA 内切换资源页时，**内容必须跟着 URL 变** —— 一次真实事故的回归守卫。
 *
 * ## 事故
 * 用户报「切换了页面但还是停在上一页，刷新一次才正常」。
 * 根因是一条容易被忽略的 Vue Router 语义：
 *
 *   **`beforeEnter` 在「同一条路由记录、仅 params 变化」时不会重新触发。**
 *
 * 而本项目全部 19 个资源页**共用同一条记录** `/:resourcePath(.*)`，
 * 资源名由 `beforeEnter` 写进 `route.meta.resource`。于是：
 *   * 首次进入 `/ponds` 正常（记下了 `pond`）；
 *   * 点「物料」→ URL 变成 `/materials`，但 `beforeEnter` **一次都没跑**
 *     （我在里面加过日志验证：整轮只出现 `path:"/ponds"` 一条），
 *     `meta.resource` 仍是 `pond`；
 *   * 页面就停在上一份数据上。**刷新之所以「能好」，是因为整页重载后首次进入
 *     会重新跑一次 —— 这恰好把根因掩盖了。**
 *
 * ## 修法（本测试钉住的就是它）
 * 资源解析移到**全局** `beforeEach`（对每一次导航都执行），
 * 路由记录上不再挂 `beforeEnter` —— 同一件事只留一处。
 */
async function login(page: Page): Promise<void> {
  await page.goto('/auth/login')
  await page.fill('[data-testid="login-identifier"]', 'demo')
  await page.fill('[data-testid="login-password"]', 'Demo1234!')
  await page.click('[data-testid="login-form"] button[type="submit"]')
  await expect(page).toHaveURL(/\/ponds$/)
}

/** 导航项 与 该页正文标题 的对应关系（中文标题来自服务端元数据）。 */
const CASES: [string, string][] = [
  ['物料', '物料'],
  ['成本记录', '成本记录'],
  ['销售单', '销售单'],
  ['操作日志', '操作日志'],
  ['养殖批次', '养殖批次'],
  ['塘口', '塘口'],
]

test('SPA 内逐个切换资源页：URL 与正文标题必须一致，且不得出现降级页', async ({ page }) => {
  await login(page)
  for (const [label, expectedTitle] of CASES) {
    await page.locator('[data-testid="app-nav"] a', { hasText: label }).first().click()
    await expect(page.locator('main h1').first()).toHaveText(expectedTitle)
    await expect(page.getByTestId('resource-unresolved')).toHaveCount(0)
    await expect(page.getByText('页面不存在')).toHaveCount(0)
  }
})

test('来回切换两次仍然正确（防「只有第一次生效」）', async ({ page }) => {
  await login(page)
  for (const [label, expectedTitle] of [
    ['物料', '物料'],
    ['塘口', '塘口'],
    ['物料', '物料'],
    ['成本记录', '成本记录'],
    ['塘口', '塘口'],
  ] as [string, string][]) {
    await page.locator('[data-testid="app-nav"] a', { hasText: label }).first().click()
    await expect(page.locator('main h1').first()).toHaveText(expectedTitle)
  }
})
