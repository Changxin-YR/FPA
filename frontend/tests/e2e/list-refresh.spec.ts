import { expect, test, type Page } from '@playwright/test'

/** 列表在写入后必须能重新拉到新数据（防浏览器缓存）。 */
async function login(page: Page): Promise<void> {
  await page.goto('/auth/login')
  await page.fill('[data-testid="login-identifier"]', 'demo')
  await page.fill('[data-testid="login-password"]', 'Demo1234!')
  await page.click('[data-testid="login-form"] button[type="submit"]')
  await expect(page).toHaveURL(/ponds$/)
}

async function total(page: Page): Promise<number> {
  const t = (await page.getByTestId('page-total').innerText()).replace(/[^0-9]/g, '')
  return Number(t)
}

test('列表重新拉取时必须看到新写入的行（不被 HTTP 缓存回旧值）', async ({ page }) => {
  await login(page)
  await page.goto('/partners')
  // 等列表**真正加载完**再读总数：初始渲染会先出现「共 0 条」占位，
  // 在那之前读数会得到 0 —— 我第一版就是这么误判成产品缺陷的。
  await expect(page.locator('[data-testid="data-table"] tbody tr').first()).toBeVisible()
  const before = await total(page)

  // 用页面自己的 fetch 创建一条（会携带会话 Cookie 与 CSRF）
  const code = 'E2E-CACHE-' + Date.now()
  const status = await page.evaluate(async (c) => {
    const csrf =
      document.cookie
        .split('; ')
        .find((x) => x.startsWith('yuxin_csrf='))
        ?.split('=')[1] ?? ''
    const res = await fetch('/api/v1/partners', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': c },
      credentials: 'include',
      body: JSON.stringify({ partner_type: 'customer', code: c, name: '缓存探针' }),
    })
    return res.status
  }, code)
  expect(status).toBe(200)

  // 让列表真正重拉一次（切走再切回）
  await page.goto('/materials')
  await page.goto('/partners')
  // ◆ 决定性断言：新写入的行**必须在列表里出现**。
  // 这比「总数变大」更直接，也不依赖读数时机。
  await expect(page.locator('body')).toContainText(code, { timeout: 10_000 })
  const after = await total(page)
  expect(after).toBeGreaterThanOrEqual(before)
})
