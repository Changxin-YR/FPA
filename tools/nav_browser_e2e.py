"""真实浏览器逐个点开导航项（t8 验收）。

## 它与 `frontend/tests/e2e/*.spec.ts` 的区别

那些 spec 用 `page.route()` **把 API 拦掉**，所以在它们眼里"页面渲染成功"只说明
前端对着我们以为的契约能跑。t7 的三处缺陷全在这个缝里（spec 全绿、浏览器点不进去），
所以**本脚本一处都不拦**：真 Vite（5273）→ 代理 → 真 Flask（5101）→ 真 MySQL。

## 它回答的问题（两个，缺一不可）

1. **逐个点开每个导航项**，给出"是否渲染成功"的清单，**红项说明原因**。
2. **导航项 == 服务端认为可导航的资源**：脚本自己用 HTTP 登录后端、读
   `/api/v1/meta/capabilities`，按**与前端同一条规则**算出应有条目
   （有读能力 + `list_path` 能推出路径），再与浏览器里实际看到的清单**逐项比对**。
   这一步是"防夹具漂移"的落点：只看浏览器渲染成功，无法发现"少列了一半资源"。

用法::

    python tools/nav_browser_e2e.py

需要 5101 与 5273 都在跑。
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

FRONTEND = ROOT / "frontend"

BACKEND = os.environ.get("FPA_BACKEND_URL", "http://127.0.0.1:5101")
FRONTEND_URL = os.environ.get("FPA_FRONTEND_URL", "http://127.0.0.1:5273")

DEMO_USER = "demo"
DEMO_PASSWORD = "Demo1234!"

PROBLEMS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {label}")
    else:
        PROBLEMS.append(label)
        print(f"  FAIL  {label}  {detail}")


def fetch_meta() -> dict:
    """用 HTTP 真登录后端，取一份**权威**元数据（供比对）。"""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = json.dumps({"identifier": DEMO_USER, "password": DEMO_PASSWORD}).encode()
    login = urllib.request.Request(
        BACKEND + "/api/v1/auth/login",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    opener.open(login).read()
    with opener.open(BACKEND + "/api/v1/meta/capabilities") as response:
        payload = json.loads(response.read().decode())
    # 响应套盰封（INTERFACES.md §1）：真正的内容在 `data` 里。
    # 直接返回埋一层会让下面报 `KeyError: 'capabilities'`
    # —— 那种报错看起来像“服务端没发能力”，而真因只是没剥封。
    return payload["data"]


def expected_nav_paths(meta: dict) -> dict[str, str]:
    """按**与前端同一条规则**算应有条目：有读能力 + list_path 在 /api/v1 下。

    规则抄自 `frontend/src/layers/common/meta/meta.store.ts::navItems()`。
    **刻意抄一遍**而不是 import 前端代码：这条断言的意义就是"两边各自独立算，
    结果必须一致"——共用一份实现会让它退化成同义反复。
    """
    read_resources = {
        cap["resource"]
        for cap in meta["capabilities"]
        if cap.get("kind") == "read" and cap.get("resource")
    }
    result: dict[str, str] = {}
    for res in meta["resources"]:
        if res["name"] not in read_resources:
            continue
        list_path = res["list_path"]
        if not list_path.startswith("/api/v1"):
            continue
        result[res["name"]] = list_path[len("/api/v1") :]
    return result


def _script() -> str:
    return r"""
const { chromium } = require('playwright')

const FRONTEND = process.env.FPA_FRONTEND_URL
const USER = process.env.FPA_DEMO_USER
const PASSWORD = process.env.FPA_DEMO_PASSWORD

;(async () => {
  const out = { nav: [], visited: [], loginError: null, hasNav: false, consoleErrors: [] }
  const browser = await chromium.launch()
  const page = await browser.newPage()
  /*
   * 只收集**真正的** console 错误。
   *
   * 401 要排除，且理由是具体的而不是"噪音"：
   *   * 未登录时访问任何 authOnly 页，`guard.ts` 会先试
   *     `GET /auth/me` 抓当前身份——那一次**必然** 401，
   *     而守卫正是靠它判定"未登录" 的（不是缺陷）。
   *   * 浏览器会把 4xx/5xx 响应也记成 console error，这是浏览器行为。
   *
   * 真正要抓的是"页面自己报了错" —— 那由 `page-error` / `resource-unresolved`
   * 两个标记元素承担（见下面逐项开的判定），不靠 console 文本。
   */
  page.on('console', (m) => {
    if (m.type() !== 'error') return
    const text = m.text()
    if (text.includes('401') || text.includes('UNAUTHORIZED')) return
    out.consoleErrors.push(text)
  })

  await page.goto(FRONTEND + '/auth/login', { waitUntil: 'networkidle' })
  await page.getByTestId('login-identifier').fill(USER)
  await page.getByTestId('login-password').fill(PASSWORD)
  await page.getByTestId('login-form').evaluate((f) => f.requestSubmit())
  await page.waitForURL(/\/workbench|\/ponds/, { timeout: 15000 }).catch(() => {})
  const err = page.getByTestId('login-error')
  if (await err.count()) out.loginError = await err.textContent()

  await page.getByTestId('app-nav').waitFor({ timeout: 10000 }).catch(() => {})
  out.hasNav = (await page.getByTestId('app-nav').count()) > 0

  out.nav = await page.locator('[data-testid^="nav-item-"]').evaluateAll((nodes) =>
    nodes.map((n) => ({
      testid: n.getAttribute('data-testid'),
      text: (n.textContent || '').trim(),
      href: n.getAttribute('href'),
    })),
  )

  for (const item of out.nav) {
    const entry = { testid: item.testid, text: item.text, href: item.href, ok: false, reason: '' }
    try {
      await page.goto(FRONTEND + item.href, { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(800)
      entry.url = page.url()
      const bodyText = (await page.locator('body').innerText()).slice(0, 400)
      entry.body = bodyText.replace(/\n+/g, ' | ')
      const hasTable = (await page.getByTestId('data-table').count()) > 0
      const hasError = (await page.getByTestId('page-error').count()) > 0
      if (hasError) {
        entry.reason = 'page-error: ' + (await page.getByTestId('page-error').innerText())
      } else if (hasTable) {
        entry.ok = true
      } else if (bodyText.includes('暂无数据')) {
        entry.ok = true
        entry.reason = '空态（无数据，渲染正常）'
      } else if (bodyText.includes('页面不存在')) {
        entry.reason = '落到「页面不存在」'
      } else {
        entry.reason = '既无表格也无空态：' + entry.body.slice(0, 100)
      }
    } catch (e) {
      entry.reason = String(e)
    }
    out.visited.push(entry)
  }

  await browser.close()
  console.log(JSON.stringify(out))
})().catch((err) => {
  console.log(JSON.stringify({ fatal: String(err) }))
  process.exit(1)
})
"""


def main() -> int:
    print("#" * 74)
    print("# t8 真实浏览器：逐个点开导航项（Vite 5273 → 代理 → Flask 5101 → MySQL）")
    print("#" * 74)

    print("\n=== 0. 取一份权威元数据（脚本自己 HTTP 登录后端）===")
    try:
        meta = fetch_meta()
    except Exception as error:  # noqa: BLE001
        print(f"FAIL  取元数据失败（5101 在跑吗？）：{error}")
        return 1
    expected = expected_nav_paths(meta)
    print(f"  服务端认为可导航的资源：{len(expected)} 个")
    print(f"  资源总数：{len(meta['resources'])}")

    env = dict(os.environ)
    env.update(
        FPA_FRONTEND_URL=FRONTEND_URL,
        FPA_DEMO_USER=DEMO_USER,
        FPA_DEMO_PASSWORD=DEMO_PASSWORD,
    )
    proc = subprocess.run(
        ["node", "-e", _script()],
        cwd=str(FRONTEND),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        print("node stdout:", proc.stdout[-1500:])
        print("node stderr:", proc.stderr[-1500:])
        check("浏览器脚本跑起来了", False)
        return _report()
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError:
        print("无法解析输出:", proc.stdout[-1200:])
        check("脚本返回 JSON", False)
        return _report()
    if out.get("fatal"):
        check("脚本无致命错误", False, out["fatal"])
        return _report()

    print("\n=== 1. 登录与导航存在 ===")
    check("登录无错误提示", not out.get("loginError"), str(out.get("loginError")))
    check("页面出现导航（t8 之前整个项目没有导航组件）", bool(out.get("hasNav")))

    nav = out.get("nav") or []
    print(f"\n=== 2. 导航条目（共 {len(nav)} 项，全部由服务端元数据生成）===")
    for item in nav:
        print(f"    {item['text']:14s} {item['href']}")

    print("\n=== 3. 导航 vs 服务端权威清单（逐项比对）===")
    actual_paths = {item["href"]: item["text"] for item in nav}
    missing = {name: path for name, path in expected.items() if path not in actual_paths}
    extra = {path: text for path, text in actual_paths.items() if path not in expected.values()}
    check(
        f"导航覆盖全部可导航资源（{len(expected)} 个）",
        not missing,
        f"缺 {missing}",
    )
    check("导航没有多余条目（无读能力的资源不得出现）", not extra, f"多 {extra}")

    print("\n=== 4. 逐个点开的结果 ===")
    visited = out.get("visited") or []
    failed = [e for e in visited if not e["ok"]]
    for entry in visited:
        mark = "OK  " if entry["ok"] else "RED "
        note = f"  ← {entry['reason']}" if entry["reason"] else ""
        print(f"    {mark} {entry['text']:14s} {entry['href']}{note}")
    check(f"{len(visited)} 个入口全部渲染成功", not failed,
          "; ".join(f"{e['text']}: {e['reason']}" for e in failed))
    check(
        "浏览器无非预期的 console error（已排除未登录探针的 401）",
        not out.get("consoleErrors"),
        str(out.get("consoleErrors"))[:300],
    )

    return _report()


def _report() -> int:
    print()
    if PROBLEMS:
        print(f"结果：{len(PROBLEMS)} 项红")
        for item in PROBLEMS:
            print("  -", item)
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
