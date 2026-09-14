import { expect, test, type Page } from '@playwright/test'

/** 用户真实流程：点「新建」-> 填表 -> 提交 -> **不手动刷新**就应该看到新行。
 *
 * 对应用户报的「创建完成后要刷新一下才会出来」。
 */
async function login(page: Page): Promise<void> {
  await page.goto('/auth/login')
  await page.fill('[data-testid="login-identifier"]', 'demo')
  await page.fill('[data-testid="login-password"]', 'Demo1234!')
  await page.click('[data-testid="login-form"] button[type="submit"]')
  await expect(page).toHaveURL(/ponds$/)
}

test('新建后不需手动刷新，列表就应出现新行', async ({ page }) => {
  await login(page)
  await page.goto('/partners')
  await expect(page.locator('[data-testid="data-table"] tbody tr').first()).toBeVisible()

  const code = 'UI-NEW-' + Date.now()
  await page.click('[data-testid="page-create"]')
  await expect(page.getByTestId('dialog-close')).toBeVisible()
  await page.fill('#field-code', code)
  await page.fill('#field-name', '界面新建探针')
  await page.selectOption('#field-partner_type', 'customer')
  await page.locator('form button[type="submit"]').first().click()

  // ◆ 关键：不刷新页面，新行必须自己出现。
  await expect(page.locator('body')).toContainText(code, { timeout: 10_000 })
})
