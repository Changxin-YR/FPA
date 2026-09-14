import { expect, test, type Page } from '@playwright/test'

/**
 * review P2 回归（真实浏览器）：弹窗键盘交互。
 *
 * 缺陷实测：`ResourceListPage` 的弹窗打开后焦点仍停在背景「新建」按钮上，
 * 于是 Esc 不关闭、Tab 会一路走进背景筛选栏；`AgentPanel` 声明的 `@keydown.esc`
 * 也因为焦点没进面板而收不到事件。jsdom 里能验事件契约，但"真实浏览器会不会
 * 把 Tab 交给背景"只有真浏览器能证 —— 这条专门补那一刀。
 *
 * 本用例**不提交表单**，因此不产生任何业务数据，无需收尾。
 */

async function login(page: Page): Promise<void> {
  await page.goto('/auth/login')
  await page.fill('[data-testid="login-identifier"]', 'demo')
  await page.fill('[data-testid="login-password"]', 'Demo1234!')
  await page.click('[data-testid="login-form"] button[type="submit"]')
  await expect(page).toHaveURL(/workbench|ponds/)
}

function focusInside(page: Page, selector: string): Promise<boolean> {
  return page.evaluate((value) => Boolean(document.activeElement?.closest(value)), selector)
}

test('弹窗：打开入焦、Tab 圈定在弹窗内、Esc 关闭并把焦点还给触发按钮', async ({ page }) => {
  await login(page)
  await page.goto('/partners')

  const create = page.getByTestId('page-create')
  await expect(create).toBeVisible({ timeout: 20_000 })
  await create.focus()
  await create.click()

  const dialog = page.getByTestId('page-dialog')
  await expect(dialog).toBeVisible()
  // 根因回归点①：焦点必须已经进入弹窗，而不是留在背景的「新建」上
  expect(await focusInside(page, '[data-testid="page-dialog"]')).toBe(true)

  // 根因回归点②：连续 Tab 必须在弹窗内循环，不能走到背景筛选栏
  for (let index = 0; index < 8; index += 1) {
    await page.keyboard.press('Tab')
    expect(await focusInside(page, '[data-testid="page-dialog"]')).toBe(true)
  }
  await page.keyboard.press('Shift+Tab')
  expect(await focusInside(page, '[data-testid="page-dialog"]')).toBe(true)

  // 根因回归点③：Esc 关闭，并且焦点还原到触发它的按钮
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(create).toBeFocused()
})
