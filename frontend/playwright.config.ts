import { defineConfig, devices } from '@playwright/test'

/**
 * E2E 配置。
 *
 * 早期版本实测缺陷（.local/recon-frontend.md 问题 P-21）：`playwright.w4.config.ts`
 * 用独立端口（5012/5176 + channel:chrome + retries:1）设计了第二条运行通道，
 * 但 CI 跑的是 `playwright test`（默认 config），而默认 config 的 `testDir: './tests/e2e'`
 * **没有 testMatch**，于是把 w4 也跑到默认端口上——两套意图互相覆盖。
 *
 * 新项目只有一份 E2E 配置、一条运行通道，端口不与其他工具冲突：
 * 本项目的 vite dev 用 5273，后端（若有）用 5101。
 */
const PORT = Number(process.env.E2E_PORT ?? 5273)
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? `http://127.0.0.1:${PORT}`

export default defineConfig({
  testDir: './tests/e2e',
  // 整轮结束后清理 UI 探针行 —— 没有它，两条"新建后出现新行"的用例会**永久**往
  // 真实库里留数据（`partner` 没有删除能力），跑多少次就多多少条（P2 实测）。
  globalTeardown: './tests/e2e/global-teardown.ts',
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: 'line',
  use: {
    baseURL,
    trace: 'on-first-retry',
    ...devices['Desktop Chrome'],
  },
  // 已给出外部 baseURL 时不自己拉服务（对着真实环境跑冒烟）
  webServer: process.env.PLAYWRIGHT_BASE_URL
    ? undefined
    : {
        command: `npm run dev -- --host 127.0.0.1 --port ${PORT}`,
        url: `${baseURL}/auth/login`,
        reuseExistingServer: !process.env.CI,
        timeout: 60_000,
      },
})
