import { expect, test, type Page } from '@playwright/test'

/**
 * 用户真实流程：列表里点「查看」→ **进的是详情页**（只读），不是编辑表单。
 *
 * 对应这一轮修的缺口：`ResourceListPage.runAction('view')` 过去既没有落点
 * （只把弹窗关掉），更早的版本则是打开编辑表单 —— 用户想「看」，系统让他「改」。
 *
 * 真链路（真 Flask 5101 + 真 MySQL），**没有** `page.route` 拦截。
 */
async function login(page: Page): Promise<void> {
  await page.goto('/auth/login')
  await page.fill('[data-testid="login-identifier"]', 'demo')
  await page.fill('[data-testid="login-password"]', 'Demo1234!')
  await page.click('[data-testid="login-form"] button[type="submit"]')
  await expect(page).toHaveURL(/ponds$/)
}

test('行内「查看」进详情页：只读、显示服务端字段、直链可刷新', async ({ page }) => {
  await login(page)
  await page.goto('/purchase-orders')

  const firstRow = page.locator('[data-testid="data-table"] tbody tr').first()
  await expect(firstRow).toBeVisible()

  // 先把列表里那一行的「采购单号」记下来 —— 详情页必须显示**同一个值**。
  // （这一条是补的：只断言"列标签在"曾经骗过我们一次 ——
  //  服务端返回 `data.record.record` 时页面每个格子都是「—」，标签却照样在。）
  const codeCell = (await firstRow.locator('td').first().innerText()).trim()

  await firstRow.getByTestId('row-action-view').click()

  // ① 地址进了详情路由（`/detail/<资源名>/<id>`）
  await expect(page).toHaveURL(/\/detail\/purchase_order\/\d+$/)
  const detail = page.getByTestId('resource-detail')
  await expect(detail).toBeVisible()
  await expect(detail).toContainText('采购单详情')

  // ② 它是**详情**不是编辑表单：页内不该有可提交的表单控件
  await expect(detail.locator('form')).toHaveCount(0)

  // ③ 显示的列标签来自服务端元数据（不是前端写死的）
  await expect(detail).toContainText('采购单号')
  // ③b **值也要真的显示出来**（不是每个格子都「—」）—— 防"响应被多包一层"这类缺陷。
  //     只断言"列标签在"曾经骗过我们一次：服务端返回 `data.record.record` 时
  //     页面每个格子都是「—」，而标签照样在。
  expect(codeCell.length).toBeGreaterThan(0)
  await expect(detail).toContainText(codeCell)

  // ④ 直链可访问：刷新一次仍然打得开（不是只有从列表点才行）
  const detailUrl = page.url()
  await page.reload()
  await expect(page).toHaveURL(detailUrl)
  await expect(page.getByTestId('resource-detail')).toBeVisible()
})

test('没有详情接口的资源：点「查看」给出显式说明，而不是静默无反应', async ({ page }) => {
  await login(page)
  // `batch` 只有 list / create / update / submit / verify / close，**没有** `batch.get`
  await page.goto('/batches')

  const firstRow = page.locator('[data-testid="data-table"] tbody tr').first()
  await expect(firstRow).toBeVisible()
  await firstRow.getByTestId('row-action-view').click()

  // 旧行为是"点了什么都不发生"；现在必须**出声**（或服务端后来补了 get ⇒ 进详情页）。
  const explained = page.getByTestId('action-error')
  const navigated = page.getByTestId('resource-detail')
  await expect(explained.or(navigated).first()).toBeVisible({ timeout: 8000 })
  if (await explained.isVisible().catch(() => false)) {
    await expect(explained).toContainText('详情接口')
  }
})
