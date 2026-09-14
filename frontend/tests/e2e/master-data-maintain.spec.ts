import { expect, test, type Page } from '@playwright/test'

/**
 * P0「主数据可维护」的真浏览器验收：**区域的新建 → 编辑 → 停用**。
 *
 * 为什么必须有这一条：这条 P0 的整个论点是「前端是元数据驱动的 —— 加能力，
 * 按钮/表单/列会自己长出来」。**没有一条真链路断言的话，"能力注册了"与
 * "用户点得到"是两件事**（本项目反复抓到过"后端有能力、页面上没按钮"）。
 *
 * 真链路（真 Flask 5101 + 真 MySQL），**没有** `page.route`。
 * 探针数据用时间戳前缀，跑完顺手停用（归档）掉，不给后面的断言留脏数据。
 */
async function login(page: Page): Promise<void> {
  await page.goto('/auth/login')
  await page.fill('[data-testid="login-identifier"]', 'demo')
  await page.fill('[data-testid="login-password"]', 'Demo1234!')
  await page.click('[data-testid="login-form"] button[type="submit"]')
  await expect(page).toHaveURL(/ponds$/)
}

test('区域：新建 → 出现在列表 → 编辑 → 停用后从默认列表消失', async ({ page }) => {
  await login(page)
  await page.goto('/areas')

  const table = page.locator('[data-testid="data-table"]')
  await expect(table.locator('tbody tr').first()).toBeVisible()
  const before = await table.locator('tbody tr').count()

  // ---- 新建（按钮必须是元数据长出来的，不是页面上本来就有的）
  const createButton = page.getByTestId('page-create')
  await expect(createButton).toBeVisible()
  const code = `E2E-AREA-${Date.now()}`
  await createButton.click()
  await expect(page.getByTestId('dialog-close')).toBeVisible()
  await page.fill('#field-code', code)
  await page.fill('#field-name', '界面验收区域')
  await page.locator('form button[type="submit"]').first().click()

  // 不刷新页面，新行必须自己出现（写入后刷新那条链路也一起验了）
  await expect(table.locator('tbody')).toContainText(code, { timeout: 15_000 })
  await expect(table.locator('tbody tr')).toHaveCount(before + 1)

  // ---- 编辑
  const row = table.locator('tbody tr').filter({ hasText: code }).first()
  await row.getByTestId('row-action-edit').click()
  await expect(page.getByTestId('dialog-close')).toBeVisible()
  await page.fill('#field-name', '界面验收区域（已改名）')
  await page.locator('form button[type="submit"]').first().click()
  await expect(table.locator('tbody')).toContainText('界面验收区域（已改名）', { timeout: 15_000 })

  // ---- 停用（归档）：默认列表里应当看不到它了
  const renamed = table.locator('tbody tr').filter({ hasText: code }).first()
  await renamed.getByTestId('row-action-archive').click()
  // 归档是高 risk 动作 ⇒ `confirmation=always`，弹窗里要填原因/确认
  const dialog = page.locator('[data-testid="dialog-close"]').first()
  if (await dialog.isVisible().catch(() => false)) {
    const reason = page.locator('#field-reason')
    if (await reason.isVisible().catch(() => false)) await reason.fill('界面验收：用后停用')
    await page.locator('form button[type="submit"]').first().click()
  }
  await expect(table.locator('tbody')).not.toContainText(code, { timeout: 15_000 })
})
