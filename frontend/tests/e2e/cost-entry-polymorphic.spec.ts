import { execFileSync } from 'node:child_process'
import { expect, test, type Page } from '@playwright/test'

/**
 * `cost.entry.create` 的「归属对象」必须是一个**选得出真实对象**的下拉框。
 *
 * ## 这条用例修的是用户报的原话
 *
 * 「登记成本失败、登记对象不可输入」。根因不是前端忘了画下拉，而是**声明层把类型
 * 丢了**：`target_id` 曾经声明成 `integer`，元数据于是告诉前端"这是个数字输入框"，
 * 前端**正确地**照做 —— 用户看到一个要求自己填 id 的数字框，既不知道填哪个、
 * 也无从知道合法值有哪些。修法是让声明能表达"引用哪类对象由 `target_type` 的取值
 * 决定"（`ref.resource_field`），前端据此在**类型切换时清空并重拉**候选。
 *
 * ## 为什么必须走真链路（不拦任何响应）
 *
 * 前端单测只能证明"对着我写的桩能跑"。这条链路上真正容易断的地方全在缝里：
 *   * 服务端到底有没有下发 `ref.resource_field`（下发了前端才会去取类型值）；
 *   * `target_type` 的枚举取值是否**真的是** `ResourceRegistry` 里的资源名
 *     —— 只要有一个不是，那个下拉就永远拉不到候选（这是后端契约缺陷，桩测不出；
 *     `farm` 这一支当前就没有读能力，见 t6）；
 *   * 提交时 `target_type + target_id` 是否被服务端接受（半给形态会被拒）。
 *
 * 因此本文件**不调用 `page.route()`**：只旁听（`waitForResponse`）真实请求，
 * 把"服务端真的接受了这笔登记"取出来当证据。响应一律来自真 MySQL + 真 Flask。
 */

async function login(page: Page): Promise<void> {
  await page.goto('/auth/login')
  await page.fill('[data-testid="login-identifier"]', 'demo')
  await page.fill('[data-testid="login-password"]', 'Demo1234!')
  await page.click('[data-testid="login-form"] button[type="submit"]')
  await expect(page).toHaveURL(/workbench|ponds/)
}

/** 本机今天（本地时区）——表单 `occurred_on` 的默认值取的就是它。 */
function dayOffset(offsetDays = 0): string {
  const d = new Date()
  d.setDate(d.getDate() + offsetDays)
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

/** 当前迭代探针的来源单号前缀（测试体里造的那个 `sourceRef`）。 */
const PROBE_PREFIX = 'FE-T13-'
let probeCreated = false

/** 子进程用的库凭据。
 *
 * 为什么走**命令行参数**而不是环境变量：测试运行器包装下 `process.env` 可能已被
 * stub，子进程因此拿不到 MySQL 变量（实测踩过）。默认值是本机开发库的公开凭据
 * （`docs/ROADMAP.md` §1），所以本机直接能跑。
 */
const MYSQL = {
  host: process.env.MYSQL_HOST ?? '127.0.0.1',
  port: process.env.MYSQL_PORT ?? '3306',
  user: process.env.MYSQL_USER ?? 'fpa',
  password: process.env.MYSQL_PASSWORD ?? 'fpa_dev_password',
  database: process.env.MYSQL_DATABASE ?? 'fpa',
}

/** 按前缀删掉当前迭代产生的成本行（外键无引用，直接删）。返回删掉的条数。 */
function cleanupProbeRows(prefix: string): number {
  const code = [
    'import sys, pymysql',
    'host, port, user, password, database, prefix = sys.argv[1:7]',
    'conn = pymysql.connect(host=host, port=int(port), user=user, password=password, database=database)',
    'cur = conn.cursor()',
    'cur.execute("DELETE FROM cost_entries WHERE source_ref LIKE %s", (prefix + "%",))',
    'print(cur.rowcount)',
    'conn.commit()',
    'conn.close()',
  ].join('\n')
  const out = execFileSync(
    'python',
    ['-c', code, MYSQL.host, MYSQL.port, MYSQL.user, MYSQL.password, MYSQL.database, prefix],
    { encoding: 'utf-8' },
  )
  return Number(out.trim())
}

test.afterAll(() => {
  // 探针必须收尾：共享开发库不该被 e2e 的残留慢慢填满（其他探针脚本都自带清理）。
  const removed = cleanupProbeRows(PROBE_PREFIX)
  if (probeCreated) expect(removed, `收尾应清掉当前迭代探针行（前缀 ${PROBE_PREFIX}）`).toBeGreaterThan(0)
})

/** 控件的 id 由 `DynamicForm` 统一生成为 `field-<key>`。 */
const TARGET_TYPE = '#field-target_type'
const TARGET_ID = '#field-target_id'

function optionTexts(page: Page, selector: string): Promise<string[]> {
  return page.locator(`${selector} option`).allTextContents()
}

/** 去掉空选项（placeholder）之后的候选值。 */
function optionValues(page: Page, selector: string): Promise<string[]> {
  return page
    .locator(`${selector} option`)
    .evaluateAll((opts) => opts.map((o) => (o as HTMLOptionElement).value).filter(Boolean))
}

test('回归：既有静态 ref 字段（新建塘口-所属区域）不受元数据变更影响', async ({ page }) => {
  // t13 顺带给**所有** ref 字段的元数据多下发了 `resource_field`（静态形态是空串）。
  // 静态形态必须一如既往往常：`ref.resource` 直接解析出列表地址，不显示"请先选类型"，
  // 也不应为空。这条覆盖的是本次改动的**外溢面**，与上面的多态用例互补。
  await page.goto('/auth/login')
  await page.fill('[data-testid="login-identifier"]', 'demo')
  await page.fill('[data-testid="login-password"]', 'Demo1234!')
  await page.click('[data-testid="login-form"] button[type="submit"]')
  await expect(page).toHaveURL(/workbench|ponds/)

  await page.goto('/ponds')
  await expect(page.getByTestId('page-create')).toBeVisible()
  await page.click('[data-testid="page-create"]')
  const dialog = page.locator('[data-testid="page-dialog"]')
  await expect(dialog).toBeVisible()

  const area = dialog.locator('select[name="area_id"]')
  expect(await area.evaluate((el) => el.tagName)).toBe('SELECT')
  // 加载态是 disabled（见 DynamicForm 的 ref 分支）：必须等候选拉完再断言"可用"，
  // 否则测到的是"候选还在拉"而不是"静态 ref 正常"。
  await expect(area).toBeEnabled({ timeout: 20_000 })
  await expect(page.getByTestId('ref-needs-target')).toHaveCount(0)
  // 候选项真的来自 /api/v1/areas（不是空下拉）。
  // **不写死某个区域名**：那会让本用例依赖 `tools/production_e2e.py` 的副产物
  // （它往库里塞过一条「端到端测试区域」，而那是另一个工具的数据），换一套数据就红
  // —— 实测踩到：清理重建数据集后本用例因"库里没有那个区域名"而失败。
  // 判据改为"候选非空 **且** 与 /api/v1/areas 的返回取交集非空"，意图不变、不再依赖环境。
  const shown: string[] = []
  await expect
    .poll(
      async () => {
        shown.length = 0
        shown.push(
          ...(await area.locator('option').allTextContents()).map((text) => text.trim()).filter(Boolean),
        )
        return shown.length
      },
      { timeout: 20_000 },
    )
    .toBeGreaterThan(0)
  const areasResponse = await page.request.get('/api/v1/areas?page=1&page_size=100')
  const apiAreaNames: string[] = (
    ((await areasResponse.json())?.data?.items ?? []) as { name: string }[]
  ).map((item) => item.name)
  expect(apiAreaNames.length).toBeGreaterThan(0)
  expect(shown.some((name) => apiAreaNames.includes(name))).toBe(true)
  await expect(page.getByTestId('ref-empty')).toHaveCount(0)
})

test('登记成本：四种归属对象类型都能加载真实候选', async ({ page }) => {
  await login(page)
  await page.goto('/cost/entries')
  await expect(page.getByTestId('page-title')).toHaveText('成本记录', { timeout: 20_000 })
  await page.getByTestId('page-create').click()
  await expect(page.getByTestId('page-dialog')).toBeVisible()

  const typeSelect = page.locator(TARGET_TYPE)
  const idSelect = page.locator(TARGET_ID)
  const endpoints: Record<string, RegExp> = {
    farm: /\/api\/v1\/farms(\?|$)/,
    area: /\/api\/v1\/areas(\?|$)/,
    pond: /\/api\/v1\/ponds(\?|$)/,
    batch: /\/api\/v1\/batches(\?|$)/,
  }

  for (const target of ['farm', 'area', 'pond', 'batch']) {
    const responseWait = page.waitForResponse(
      (resp) => resp.request().method() === 'GET' && endpoints[target].test(resp.url()),
      { timeout: 20_000 },
    )
    await typeSelect.selectOption(target)
    const response = await responseWait
    expect(response.status()).toBe(200)
    const items = (await response.json()).data?.items ?? []
    expect(items.length, `${target} 列表应有候选`).toBeGreaterThan(0)
    await expect(idSelect).toBeEnabled()
    await expect.poll(() => optionValues(page, TARGET_ID)).toHaveLength(items.length)
    for (const item of items) {
      expect(await page.locator(`${TARGET_ID} option`, { hasText: item.name }).count()).toBeGreaterThan(0)
    }
  }
})

test('登记成本：「归属对象」是下拉、候选随类型切换并清空旧值、选真实对象后提交成功', async ({ page }) => {
  await login(page)

  // ---- ① 打开成本记录列表（走元数据推导出的真实路由） ----
  await page.goto('/cost/entries')
  await expect(page.getByTestId('page-title')).toBeVisible({ timeout: 20_000 })
  await expect(page.getByTestId('page-title')).toHaveText('成本记录')

  // ---- ② 打开「登记成本」弹窗 ----
  await page.getByTestId('page-create').click()
  await expect(page.getByTestId('page-dialog')).toBeVisible()

  // ★ 核心断言之一：这里必须是 <select>，**不是**用户抱怨的那个数字框
  const typeSelect = page.locator(TARGET_TYPE)
  const idSelect = page.locator(TARGET_ID)
  await expect(typeSelect).toBeVisible()
  await expect(idSelect).toBeVisible()
  expect(await idSelect.evaluate((el) => el.tagName)).toBe('SELECT')

  // ---- ③ 类型未选时：禁用 + 明确说明原因，而不是"看起来没数据"的空下拉 ----
  await expect(idSelect).toBeDisabled()
  await expect(idSelect.locator('option').first()).toHaveText('请先选择归属对象类型')
  await expect(page.getByTestId('ref-needs-target')).toContainText('归属对象类型')
  // 此刻候选必须真的是空的（没人去猜地址发请求）
  expect(await optionValues(page, TARGET_ID)).toEqual([])

  // ---- ④ 选「区域」→ 候选来自真实 /api/v1/areas ----
  const areasWait = page.waitForResponse(
    (resp) => resp.request().method() === 'GET' && /\/api\/v1\/areas(\?|$)/.test(resp.url()),
    { timeout: 20_000 },
  )
  await typeSelect.selectOption('area')
  const areasResp = await areasWait
  expect(areasResp.status()).toBe(200)
  const areaNames: string[] = ((await areasResp.json()).data?.items ?? []).map(
    (item: { name: string }) => item.name,
  )
  expect(areaNames.length).toBeGreaterThan(0)

  await expect.poll(() => optionValues(page, TARGET_ID), { timeout: 15_000 }).toHaveLength(areaNames.length)
  // 候选就是后端返回的那些区域（说明地址取自 ResourceMeta.list_path，没猜复数）
  for (const name of areaNames) {
    await expect(page.getByRole('option', { name, exact: true })).toHaveCount(1)
  }
  await expect(page.getByTestId('ref-needs-target')).toHaveCount(0)

  // 选一个真实区域，用来验证"切换类型会把旧值清干净"
  const areaValues = await optionValues(page, TARGET_ID)
  await idSelect.selectOption(areaValues[0])
  expect(await idSelect.inputValue()).toBe(areaValues[0])

  // ---- ⑤ 切到「塘口」：旧的已选值必须被清空，候选换成塘口 ----
  const pondsWait = page.waitForResponse(
    (resp) => resp.request().method() === 'GET' && /\/api\/v1\/ponds(\?|$)/.test(resp.url()),
    { timeout: 20_000 },
  )
  await typeSelect.selectOption('pond')
  expect((await pondsWait).status()).toBe(200)

  // ★ 清空：残留一个属于"区域"的 id 会提交出 target_type=pond&target_id=<旧区域id>
  await expect(idSelect).toHaveValue('')
  const pondTexts = await optionTexts(page, TARGET_ID)
  for (const name of areaNames) expect(pondTexts).not.toContain(name)

  const pondValues = await optionValues(page, TARGET_ID)
  expect(pondValues.length).toBeGreaterThan(0)
  await idSelect.selectOption(pondValues[0])

  // ---- ⑥ 填必填字段并提交 ----
  await page.locator('#field-category_code').selectOption({ index: 1 })
  await page.locator('#field-amount').fill('12.34')
  const marker = 'FE-T13-' + Date.now()
  await page.locator('#field-source_ref').fill(marker)
  await page.locator('#field-source_type').selectOption({ index: 1 })

  // 期间必须是**未关账**的：先试今天，被拒（该月已关账）就退到更早的日期。
  // 不硬编码某一年的某个月 —— 那会把"今天能不能登记"变成对测试数据状态的依赖。
  let createdId: number | undefined
  let lastError = ''
  for (const offset of [0, -1, -2, -3, -5, -10, -20, -40]) {
    await page.locator('#field-occurred_on').fill(dayOffset(offset))
    const submitted = page.waitForResponse(
      (resp) => resp.request().method() === 'POST' && /\/api\/v1\/cost\/entries(\?|$)/.test(resp.url()),
      { timeout: 30_000 },
    )
    await page
      .locator('[data-testid="page-dialog"] form.dynamic-form')
      .evaluate((form) => (form as HTMLFormElement).requestSubmit())
    const resp = await submitted
    if (resp.ok()) {
      probeCreated = true
      const payload = await resp.json()
      // `HandlerResult.data` 同时给出 `resource_id`（本次写入的主键）；这不是
      // 稳定契约的一部分，取不到也不影响本用例的判据（下面用唯一标记断言那一行）。
      createdId = payload.data?.resource_id ?? payload.data?.id ?? payload.data?.entry_id
      await expect(page.getByTestId('page-dialog')).toHaveCount(0, { timeout: 10_000 })
      break
    }
    lastError = (await resp.text()).slice(0, 300)
    // 只有"该月已关账"才值得换个日期重试；其它错误直接失败并打印服务端原话
    if (!/PERIOD_CLOSED|关账|期间/.test(lastError)) {
      throw new Error(`登记成本被服务端拒绝（HTTP ${resp.status()}）：${lastError}`)
    }
  }
  // 提交必须成功：要么拿到了主键，要么至少弹窗已关（= 服务端接受了这笔登记）
  expect(lastError, `所有候选期间都被拒绝，最后一次错误：${lastError}`).toBe('')
  await expect(page.getByTestId('action-error')).toHaveCount(0)

  // ---- ⑦ 列表里真的出现这一行（不是只发了个请求） ----
  // 用**唯一标记**断言：它由本次测试生成，不可能来自别处（主键回读不作判据，
  // 因为那会把用例绑在 `resource_id` 这个非稳定键名上）。
  await expect(page.locator('[data-testid="data-table"] tbody')).toContainText(marker, {
    timeout: 20_000,
  })
  if (createdId !== undefined) expect(createdId).toBeGreaterThan(0)
})
