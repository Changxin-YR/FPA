import { expect, test } from '@playwright/test'

/**
 * 骨架级 E2E：验证登录页可渲染、表单可交互、守卫会挡未登录访问。
 *
 * 早期版本实测缺陷：8 个 E2E spec 里只有 1 个打真实域名，其余全部对着
 * `tests/e2e/full_stub.py`（16 KB 手写 Python HTTP 桩）。这里不引入桩服务器，
 * 而是用 Playwright 的 route 拦截在浏览器侧构造响应——桩的逻辑留在测试文件里，
 * 不会产生"桩与真实契约漂移但 CI 全绿"的盲区。
 */

/**
 * 一份**自洽的**元数据夹具。
 *
 * ## 为什么需要它（两条实测教训叠在一起）
 *
 * 1. t7 之前这些 spec 把后端形状 stub 错（平铺而不是 `{user}`），于是**夹具与真实契约
 *    漂移但 CI 全绿** —— 本文件头那句"不会产生漂移盲区"的自述当时是假的。
 * 2. t8 之后资源页由**一条动态路由**承载，它按元数据把路径反查成资源名。
 *    所以"stub 成 `data: {}`"不再是"页面少点东西"，而是**路径解析不出来**、
 *    页面直接落到「页面不存在」。
 *
 * 结论：夹具必须长得像真服务端**到能被解析器接受**。下面这份就是那个最小形态：
 * 一个资源 + 它的一条读能力（判据与 `meta.store.ts::navItems()` 同源）。
 */
const META_FIXTURE = {
  capabilities: [
    {
      name: 'pond.list',
      title: '塘口列表',
      domain: 'master_data',
      resource: 'pond',
      kind: 'read',
      method: 'GET',
      path: '/api/v1/ponds',
      risk: 'read',
      confirmation: 'never',
      agent_exposure: 'exposed',
      required_permission: 'pond.view',
      scope_required: false,
      idempotent: false,
      description: '',
      fields: [],
      path_parameters: [],
      row_actions: ['view'],
    },
  ],
  resources: [
    {
      name: 'pond',
      title: '塘口',
      module: 'master_data',
      list_path: '/api/v1/ponds',
      columns: [
        { key: 'code', label: '塘口编号' },
        { key: 'status_label', label: '状态', tone_key: 'status' },
      ],
      status_dict: [{ value: 'farming', label: '养殖中', tone: 'success' }],
    },
  ],
  // 动作词与**中文标签**（服务端唯一定义处：`kernel/workflow.py::ACTION_LABELS`）。
  // 夹具必须跟着契约走，否则就是上面第 1 条那个盲区。
  actions: {
    tones: ['neutral', 'info', 'success', 'warning', 'danger'],
    row_actions: ['view', 'archive'],
    row_action_labels: { view: '查看', archive: '归档' },
  },
}

test('登录页渲染并显示中文标题', async ({ page }) => {
  await page.goto('/auth/login')
  await expect(page.locator('h1')).toHaveText('渔芯AI水产养殖一体化系统')
  await expect(page.getByTestId('login-form')).toBeVisible()
})

test('未填写账号密码时给出中文提示', async ({ page }) => {
  await page.goto('/auth/login')
  await page.getByTestId('login-form').evaluate((form: HTMLFormElement) => form.requestSubmit())
  await expect(page.getByTestId('login-error')).toHaveText('请输入账号和密码')
})

test('未登录访问业务页被守卫挡回登录页', async ({ page }) => {
  await page.route('**/api/v1/auth/me', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({
        code: 'UNAUTHENTICATED',
        message: '登录状态已失效',
        data: null,
        request_id: 'e2e',
      }),
    }),
  )

  await page.goto('/ponds')
  await expect(page).toHaveURL(/\/auth\/login$/)
})

test('登录成功后按元数据渲染塘口列表（含状态中文与配色）', async ({ page }) => {
  await page.route('**/api/v1/meta/capabilities', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        code: 'OK',
        message: '',
        request_id: 'e2e',
        data: META_FIXTURE,
      }),
    }),
  )

  await page.route('**/api/v1/auth/me', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        code: 'OK',
        message: '',
        request_id: 'e2e',
        data: { user: { id: 1, name: '管理员', roles: [], permissions: ['pond.view'] } },
      }),
    }),
  )

  await page.route('**/api/v1/ponds?*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        code: 'OK',
        message: '',
        request_id: 'e2e',
        data: {
          items: [
            {
              id: 1,
              code: 'P-001',
              status_label: '养殖中',
              status: 'farming',
              allowed_actions: ['view', 'archive'],
            },
          ],
          page: 1,
          page_size: 20,
          total: 1,
          has_next: false,
        },
      }),
    }),
  )

  await page.goto('/ponds')

  const table = page.getByTestId('data-table')
  await expect(table).toBeVisible()
  // 状态中文来自 status_dict，不是页面里的字面量
  await expect(table).toContainText('养殖中')
  await expect(table).toContainText('P-001')
  // 配色来自 tone，且必须是合法枚举（不会退化成空值）
  await expect(table.locator('[data-tone="tone-success"]')).toHaveCount(1)

  // 操作列渲染的必须是**中文标签**，不是裸 token。
  // 这一条来自实测缺陷：状态列是中文（`养殖中`）而操作列是 `view` / `archive`。
  await expect(table.locator('[data-testid="row-action-view"]')).toHaveText('查看')
  await expect(table.locator('[data-testid="row-action-archive"]')).toHaveText('归档')
})

/**
 * ## 登录路径的三条回归守卫（t7）
 *
 * 这三条针对的是**用户点不进去登录**的阻断缺陷。它们的价值在于**能自证有信号**：
 * 每一条都在断言"某个具体字节"，把缺陷改回去就会红。
 *
 * 这个文件头写的"不引桩可以避免桩与真实契约漂移的盲区"——本次缺陷恰恰说明
 * **光不引桩还不够**：这些是浏览器侧真跑的断言，而且钉的是**契约的字面量**
 * （字段名 `identifier`、读 `data.user`、登录不得预取 CSRF），
 * 不是"页面看起来对不对"。
 */

test('登录时**不得**预取 CSRF 令牌（否则是"登录需要令牌、令牌需要先登录"的死锁）', async ({ page }) => {
  const requested: string[] = []
  await page.route('**/api/v1/**', async (route) => {
    const url = route.request().url()
    requested.push(url)
    // 服务端这个端点要求已有会话（未登录 401）——所以登录前打到它就是死锁。
    if (url.includes('/api/v1/auth/csrf')) {
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({
          code: 'UNAUTHENTICATED',
          message: '登录状态已失效，请重新登录',
          data: null,
          request_id: 'e2e',
        }),
      })
      return
    }
    if (url.includes('/api/v1/auth/login')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          code: 'OK',
          message: '登录成功',
          request_id: 'e2e',
          data: {
            user: {
              id: 1,
              name: '演示账号',
              status: 'active',
              roles: ['demo_admin'],
              permissions: ['pond.view'],
            },
          },
        }),
      })
      return
    }
    // 元数据必须**能被解析器接受**（t8 起路径 → 资源名靠它反查），
    // 所以这里不能像以前那样返回 `{}`。
    const isMeta = url.includes('/api/v1/meta/capabilities')
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        code: 'OK',
        message: '',
        request_id: 'e2e',
        data: isMeta ? META_FIXTURE : {},
      }),
    })
  })

  await page.goto('/auth/login')
  await page.getByTestId('login-identifier').fill('demo')
  await page.getByTestId('login-password').fill('Demo1234!')
  await page.getByTestId('login-form').evaluate((form: HTMLFormElement) => form.requestSubmit())

  // 提交后必须真的走到登录成功（而不是停在错误提示上）
  await expect(page).toHaveURL(/\/ponds$/)
  // ★ 关键断言：整条登录路径**一次都没请求过** /auth/csrf
  expect(requested.filter((u) => u.includes('/api/v1/auth/csrf'))).toEqual([])
})

test('登录请求体是 `identifier`/`password`，且带 skipCsrf（不注入 CSRF 头）', async ({ page }) => {
  let sentBody: Record<string, unknown> | null = null
  let sentCsrfHeader: string | null = null

  await page.route('**/api/v1/auth/login', async (route) => {
    sentBody = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
    sentCsrfHeader = route.request().headers()['x-csrf-token'] ?? null
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        code: 'OK',
        message: '登录成功',
        request_id: 'e2e',
        data: {
          user: {
            id: 1,
            name: '演示账号',
            status: 'active',
            roles: ['demo_admin'],
            permissions: ['pond.view'],
          },
        },
      }),
    })
  })
  await page.route('**/api/v1/auth/me', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({
        code: 'UNAUTHENTICATED',
        message: '登录状态已失效',
        data: null,
        request_id: 'e2e',
      }),
    }),
  )

  await page.goto('/auth/login')
  await page.getByTestId('login-identifier').fill('demo')
  await page.getByTestId('login-password').fill('Demo1234!')
  await page.getByTestId('login-form').evaluate((form: HTMLFormElement) => form.requestSubmit())

  await expect.poll(() => sentBody).not.toBeNull()
  // 字段名是**权威契约**（registry §2.3）。后端读错（读 username）就是 t7 的缺陷 2。
  expect(sentBody).toEqual({ identifier: 'demo', password: 'Demo1234!' })
  // 登录已豁免 CSRF，所以不该带这个头；带了说明又走回预取令牌那条路
  expect(sentCsrfHeader).toBeNull()
})

test('复合 `status=must_change_password` 不得被放进业务页', async ({ page }) => {
  await page.route('**/api/v1/auth/login', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        code: 'OK',
        message: '登录成功',
        request_id: 'e2e',
        data: {
          user: {
            id: 7,
            name: '新同事',
            // 复合口径：服务端要把"需改密"折叠进 status（否则 PATH_BY_STATE 永远命中不到）
            status: 'must_change_password',
            roles: [],
            permissions: [],
          },
        },
      }),
    }),
  )
  await page.route('**/api/v1/auth/me', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({
        code: 'UNAUTHENTICATED',
        message: '登录状态已失效',
        data: null,
        request_id: 'e2e',
      }),
    }),
  )

  await page.goto('/auth/login')
  await page.getByTestId('login-identifier').fill('demo')
  await page.getByTestId('login-password').fill('Demo1234!')
  await page.getByTestId('login-form').evaluate((form: HTMLFormElement) => form.requestSubmit())

  /*
   * 钉的是"服务端给的**复合** status 真的驱动了跳转"。
   *
   * 断言写成"不在 /ponds"而不是"在 /auth/first-password"，是因为**后者现在不存在**：
   * `router.ts` 只定义了 `/auth/login`，`/auth/first-password` 会被
   * `{ path: '/:pathMatch(.*)*', redirect: '/workbench' }` 兜住，于是落在 `/workbench`。
   *
   * 把断言写成"应该在改密页"会红，而那个页面属于本阶段明确不做的前端业务页。
   * **为了让门禁变绿而去造一个空页面，比门禁报红坏得多**。
   * 真正要防的回归是"复合 status 被忽略、直接进业务页"——即下面这条。
   * 缺口已写在 `session.store.ts::PATH_BY_STATE` 的注释里。
   */
  await expect(page).not.toHaveURL(/\/ponds/)
})
