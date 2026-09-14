import { test, expect, type Page } from '@playwright/test'

const user = process.env.FPA_DEMO_USER ?? 'demo'
const password = process.env.FPA_DEMO_PASSWORD ?? 'Demo1234!'

async function blockedIfUnavailable(page: Page): Promise<void> {
  try {
    const response = await page.request.get('/api/v1/auth/me', { timeout: 10_000 })
    if (response.status() !== 401 && response.status() >= 500) {
      test.skip(true, `BLOCKED: 后端不可用 status=${response.status()}`)
    }
  } catch (error) {
    test.skip(true, `BLOCKED: 后端不可用 ${String(error)}`)
  }
}

async function login(page: Page): Promise<void> {
  await blockedIfUnavailable(page)
  await page.goto('/auth/login', { waitUntil: 'networkidle' })
  await page.getByTestId('login-identifier').fill(user)
  await page.getByTestId('login-password').fill(password)
  await page.getByTestId('login-form').evaluate((form) => (form as HTMLFormElement).requestSubmit())
  await page.waitForURL(/\/workbench|\/ponds/, { timeout: 20_000 })
  const error = page.getByTestId('login-error')
  if (await error.count()) {
    const text = (await error.textContent()) ?? ''
    test.skip(true, `BLOCKED: 真实账号登录失败 ${text}`)
  }
}

async function ask(page: Page, message: string): Promise<void> {
  const launcher = page.getByRole('button', { name: /塘小助|关闭塘小助|打开塘小助/ }).first()
  await expect(launcher).toBeVisible({ timeout: 10_000 })
  if (!(await page.getByTestId('agent-panel').count())) await launcher.click()
  const assistantMessages = page.locator('.agent-message--assistant')
  const beforeAssistantCount = await assistantMessages.count()
  await page.getByTestId('agent-input').fill(message)
  await page.getByTestId('agent-composer').evaluate((form) => (form as HTMLFormElement).requestSubmit())
  try {
    await expect
      .poll(() => assistantMessages.count(), { timeout: 240_000 })
      .toBeGreaterThan(beforeAssistantCount)
    await expect(page.getByTestId('agent-input')).toBeEnabled({ timeout: 240_000 })
    await expect(assistantMessages.last()).toContainText(/\S+/, { timeout: 240_000 })
  } catch (error) {
    const panelError =
      (await page
        .getByTestId('agent-error')
        .textContent()
        .catch(() => '')) ?? ''
    if (/不可用|超时|没有返回|协议|未配置/i.test(panelError)) {
      test.skip(true, `BLOCKED: 真实 Agent 不可用 ${panelError}`)
    }
    throw error
  }
}

test.describe('真实 Agent 浏览器链路', () => {
  test('真实查询得到 assistant 回复', async ({ page }) => {
    test.setTimeout(300_000)
    await login(page)
    await ask(page, '请只用一句中文回答：当前系统已连接。不要调用工具。')
    await expect(page.locator('.agent-message--assistant').last()).toContainText(/\S+/)
  })

  test('真实会话连续完成 10 轮聊天', async ({ page }) => {
    test.setTimeout(2_700_000)
    await login(page)
    for (let round = 1; round <= 10; round += 1) {
      await ask(page, `第 ${round} 轮：请只用中文简短回复已收到，不要调用工具。`)
      await expect(page.locator('.agent-message--assistant').last()).toContainText(/\S+/)
    }
    expect(await page.locator('.agent-message--assistant').count()).toBeGreaterThanOrEqual(10)
  })
})
