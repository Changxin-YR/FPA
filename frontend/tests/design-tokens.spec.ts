import { readFileSync, readdirSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 设计系统的守卫。
 *
 * ## 为什么需要"禁止裸 hex"这条
 *
 * 令牌文件 `src/styles/tokens.css` 是配色的**唯一定义处**。一旦某个组件开始写
 * `#2f7e74` 这样的裸值，它就是一处**不会报错**的第二来源：改令牌时它不会跟着变，
 * 而界面上只会"有一点不对"——没人会为此开单。
 * 早期版本正是这样烂掉的：`tokens.css` 的 `--auth-*`（15 个）与 `workbench.css` 的
 * `--wb-*`（26 个）同名同值并存，`auth.css` 里整套暗色主题被同文件后面的浅色规则
 * 整体覆盖、成为死代码（见 `.local/recon-frontend.md` 问题 P-9 / P-11）。
 *
 * ## 为什么需要"product 目录白名单"这条
 *
 * 前端是**元数据驱动**的：19 个资源列表页共用同一个 `ResourcePage` + `ResourceListPage`
 * + `DataTable`。曾经有 **13 个 7 行的薄包装**（`PondListPage.vue` …），每个都手写一次
 * 资源名，结果 `resource="cost.entry"` 与服务端的 `cost_entry` 漂移，**6 个页面白屏
 * 而测试全绿**（详见 t8）。
 * 所以 `layers/product/` 下只允许存在**非资源页**。新增一个"某资源的列表页"文件 = 退化开始。
 */

const ROOT = process.cwd()
const SRC = join(ROOT, 'src')
const TOKENS = join(SRC, 'styles', 'tokens.css')

/** `layers/product/` 下允许存在的文件：都不是"某个资源的列表页"。 */
const ALLOWED_PRODUCT_FILES = [
  'auth/LoginPage.vue',
  // 首登改密页（`/auth/first-password`）：`session.store` 把 `must_change_password`
  // 映射到它，所以它是一张**非资源页**（与登录页同类）。
  'auth/FirstPasswordPage.vue',
  'ponds/PondDetailPage.vue',
  'workbench/WorkbenchPage.vue',
]

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) walk(full, out)
    else out.push(full)
  }
  return out
}

/** 取 `<style>` 之后的整段（`.css` 文件返回全文）。 */
function styleSection(file: string): string {
  const text = readFileSync(file, 'utf-8')
  if (file.endsWith('.vue')) {
    const i = text.indexOf('<style')
    return i >= 0 ? text.slice(i) : ''
  }
  return text
}

const HEX = /#[0-9a-fA-F]{3,8}\b/g

describe('设计令牌是配色的唯一定义处', () => {
  it('除 tokens.css 外，任何样式块都不出现裸 hex', () => {
    const offenders: string[] = []
    const files = [...walk(join(SRC, 'layers')), join(SRC, 'App.vue')].filter(
      (f) => f.endsWith('.vue') || f.endsWith('.css'),
    )

    for (const file of files) {
      if (file === TOKENS) continue
      const hits = styleSection(file).match(HEX)
      if (hits) offenders.push(`${relative(SRC, file)}: ${[...new Set(hits)].join(', ')}`)
    }

    // 断言同时打印"扫了多少文件"——否则"0 处"无法与"没扫到"区分。
    expect(files.length).toBeGreaterThan(10)
    expect(offenders).toEqual([])
  })

  it('layers/product 下只允许存在非资源页（元数据驱动不许退化）', () => {
    const found = walk(join(SRC, 'layers', 'product'))
      .map((f) => relative(join(SRC, 'layers', 'product'), f).replace(/\\/g, '/'))
      .sort()
    expect(found).toEqual([...ALLOWED_PRODUCT_FILES].sort())
  })
})

describe('渔芯 工作台视觉契约', () => {
  it('壳层具备工作台头部与移动导航结构', () => {
    const app = readFileSync(join(SRC, 'App.vue'), 'utf-8')
    const nav = readFileSync(join(SRC, 'layers', 'common', 'ui', 'AppNav.vue'), 'utf-8')
    const tokens = readFileSync(TOKENS, 'utf-8')

    expect(tokens).toContain('--tone-nav-bg')
    expect(tokens).toContain('--tone-header-surface')
    expect(app).toContain('app-shell__workspace')
    expect(app).toContain('app-shell__header')
    expect(app).toContain('mobile-nav-toggle')
    expect(nav).toContain('app-nav__brand-mark')
    expect(nav).toContain('app-nav__group-toggle')
  })

  it('通用数据表与筛选表单具备工作台视觉钩子', () => {
    const table = readFileSync(join(SRC, 'layers', 'common', 'ui', 'DataTable.vue'), 'utf-8')
    const filters = readFileSync(join(SRC, 'layers', 'common', 'ui', 'ListFilterBar.vue'), 'utf-8')
    const form = readFileSync(join(SRC, 'layers', 'common', 'ui', 'DynamicForm.vue'), 'utf-8')
    const resource = readFileSync(join(SRC, 'layers', 'common', 'ui', 'ResourceListPage.vue'), 'utf-8')
    const agent = readFileSync(join(SRC, 'layers', 'common', 'ui', 'AgentPanel.vue'), 'utf-8')

    expect(table).toContain('data-table__scroll')
    expect(filters).toContain('filter-bar__panel')
    expect(form).toContain('dynamic-form__grid')
    expect(resource).toContain('resource-page__panel-head')
    expect(agent).toContain('agent-panel__close')
  })
})
