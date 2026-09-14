import { execFileSync } from 'node:child_process'
import { expect, test, type Page } from '@playwright/test'

/**
 * 智能体写入之后，**当前列表自动刷新**（用户报："智能体新增数据依旧要刷新后才显示"）。
 *
 * ## 为什么这条必须走真链路
 *
 * 缺陷出在两个组件之间的**缝**里：智能体写入走
 * `Harness 子进程 → /api/v1/agent/tools/<n>/call → CapabilityRunner`（后端内部），
 * 而列表只在挂载/切资源/自己提交后重拉。用 `page.route()` 伪造一个 `executed` 响应
 * 只能证明"前端对着我写的桩能跑"，证明不了"后端真的会在写入轮返回 executed"
 * —— 而那条正是本次修复的另一半。
 *
 * 因此本文件**不拦任何响应**：只旁听请求（`page.on('request')`）来数列表请求次数，
 * 用于"纯查询不该重拉"这条反例。响应一律走真后端。
 *
 * ## 为什么反例同等重要
 *
 * "每轮都刷新"也能让主用例变绿 —— 代价是纯查询也重拉列表，且
 * "哪些轮真的写了"这个事实变得不可观测。所以下面同时断言：查询轮**一次列表请求都不多发**。
 */

// 文件级超时必须显式抬高：Playwright 默认 30s/用例，而真实 Agent 一轮要 1–5 分钟。
// 只给 `waitForFunction` 传 900s 是不够的 —— 用例级 30s 一到会先把整条测试掐断
// （实测踩到过：主用例在 `waitTurnDone` 处报 "Test timeout of 30000ms exceeded"）。
test.setTimeout(960_000)

async function login(page: Page): Promise<void> {
  await page.goto('/auth/login')
  await page.fill('[data-testid="login-identifier"]', 'demo')
  await page.fill('[data-testid="login-password"]', 'Demo1234!')
  await page.click('[data-testid="login-form"] button[type="submit"]')
  await expect(page).toHaveURL(/workbench|ponds/)
}

/** 旁听（**不拦截**）列表请求，返回已发生的次数。 */
function countPartnerListRequests(page: Page): () => number {
  let count = 0
  page.on('request', (request) => {
    const url = request.url()
    if (request.method() === 'GET' && /\/api\/v1\/partners(\?|$)/.test(url)) count += 1
  })
  return () => count
}

async function ask(page: Page, message: string): Promise<void> {
  const launcher = page.getByRole('button', { name: /塘小助|关闭塘小助|打开塘小助/ }).first()
  if ((await page.getByTestId('agent-panel').count()) === 0) await launcher.click()
  await page.getByTestId('agent-panel').waitFor({ timeout: 10_000 })
  await page.getByTestId('agent-input').fill(message)
  await page.getByTestId('agent-composer').evaluate((form) => (form as HTMLFormElement).requestSubmit())
}

/** 等这一轮真正结束（面板 busy 清掉），一轮对话要 1–5 分钟，超时给足。 */
async function waitTurnDone(page: Page): Promise<void> {
  await page.waitForFunction(
    () => {
      const input = document.querySelector('[data-testid="agent-input"]') as HTMLInputElement | null
      return Boolean(input) && !input!.disabled
    },
    { timeout: 900_000 },
  )
}

const PROBE_PREFIX = 'AI-NEW-'
const PROBE_NAME = '智能体刷新探针'
let probeCreated = false

const MYSQL = {
  host: process.env.MYSQL_HOST ?? '127.0.0.1',
  port: process.env.MYSQL_PORT ?? '3306',
  user: process.env.MYSQL_USER ?? 'fpa',
  password: process.env.MYSQL_PASSWORD ?? 'fpa_dev_password',
  database: process.env.MYSQL_DATABASE ?? 'fpa',
}

function cleanupProbeRows(): number {
  const code = [
    'import sys, pymysql',
    'host, port, user, password, database, prefix, name = sys.argv[1:8]',
    'conn = pymysql.connect(host=host, port=int(port), user=user, password=password, database=database)',
    'cur = conn.cursor()',
    'cur.execute("DELETE FROM business_partners WHERE code LIKE %s AND name=%s AND status=\'draft\'", (prefix + "%", name))',
    'print(cur.rowcount)',
    'conn.commit()',
    'conn.close()',
  ].join('\n')
  const out = execFileSync(
    'python',
    [
      '-c',
      code,
      MYSQL.host,
      MYSQL.port,
      MYSQL.user,
      MYSQL.password,
      MYSQL.database,
      PROBE_PREFIX,
      PROBE_NAME,
    ],
    { encoding: 'utf-8' },
  )
  return Number(out.trim())
}

test.beforeAll(() => {
  cleanupProbeRows()
})

test.afterAll(() => {
  const removed = cleanupProbeRows()
  if (probeCreated) expect(removed, '收尾应清掉当前迭代智能体刷新探针').toBeGreaterThan(0)
})

test('智能体写入后，当前列表**不手动刷新**就出现新行', async ({ page }) => {
  await login(page)
  await page.goto('/partners')
  await expect(page.locator('[data-testid="data-table"] tbody tr').first()).toBeVisible({
    timeout: 20_000,
  })
  const listRequests = countPartnerListRequests(page)
  const before = listRequests()

  const code = PROBE_PREFIX + Date.now()
  await ask(page, `新建一个客户：编号 ${code}，名称「智能体刷新探针」。请直接创建，不要反问我。`)
  await waitTurnDone(page)

  // ① 服务端这一轮必须判成"写过"（否则前端没有任何依据去刷新）
  const assistantText = await page
    .locator('[data-testid="agent-messages"] article.agent-message--assistant')
    .last()
    .innerText()
  expect(assistantText.length).toBeGreaterThan(0)

  // ② 列表**没有手动刷新**，却自己重拉了（请求计数增加）
  await expect
    .poll(() => listRequests(), { timeout: 60_000, message: '写入轮之后应当自动重拉一次列表' })
    .toBeGreaterThan(before)

  // ③ 并且界面上**真的出现了那一行**（不只是发了个请求）
  await expect(page.locator('body')).toContainText(code, { timeout: 60_000 })
  probeCreated = true
})

test('反例：纯查询**不得**触发列表重拉', async ({ page }) => {
  await login(page)
  await page.goto('/partners')
  await expect(page.locator('[data-testid="data-table"] tbody tr').first()).toBeVisible({
    timeout: 20_000,
  })
  const listRequests = countPartnerListRequests(page)
  // 让首屏的请求都落定，再开始计数
  await page.waitForTimeout(1_000)
  const before = listRequests()
  const rowsBefore = await page.locator('[data-testid="data-table"] tbody tr').count()

  await ask(page, '现在系统里一共有几个往来单位？只回答数量，不要修改任何数据。')
  await waitTurnDone(page)

  // 纯查询轮没有 `executed`，所以发布端根本不该发信号
  await page.waitForTimeout(3_000)
  expect(listRequests()).toBe(before)
  expect(await page.locator('[data-testid="data-table"] tbody tr').count()).toBe(rowsBefore)
})
