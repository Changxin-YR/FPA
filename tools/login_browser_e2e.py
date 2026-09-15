"""真实浏览器验收：登录链路（t7 阻断缺陷的端到端证据）。

## 它与 `frontend/tests/e2e/*.spec.ts` 的区别，以及为什么需要它

那些 spec 用 Playwright 的 `page.route()` **把 API 拦掉**、在测试文件里造响应。
好处是快、且不依赖后端；**代价是它验证的是"前端对着我们以为的契约能不能跑"，
不是"对着真实后端能不能跑"** —— 而 t7 的三处缺陷（CSRF 死锁、`identifier` 字段名、
`{user}` 包裹）**全都在这个缝里**：三个 spec 全绿，浏览器却点不进登录。

所以本脚本**一处都不拦**：真 Vite（5273）→ 真代理 → 真 Flask（5101）→ 真 MySQL，
并且**先跑一个反例**（见下）来自证"它确实有信号"。

## 反例：怎么证明这个脚本能抓到缺陷

脚本会**故意**用旧字段名 `username` 打一次真实接口。真实后端必须回 400。
把这条反例去掉、或后端被改回读 `username` 时它会红 —— 这是"守卫非空洞"的证据。

## 用法

    # 需要 5101（后端）与 5273（前端 dev server）都在跑
    python tools/login_browser_e2e.py

    环境：MYSQL_USER / MYSQL_PASSWORD（只有反例会用到，走 HTTP 不需要库权限）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

BACKEND = os.environ.get("FPA_BACKEND_URL", "http://127.0.0.1:5101")
FRONTEND_URL = os.environ.get("FPA_FRONTEND_URL", "http://127.0.0.1:5273")

#: 演示账号（开发库里的真实行，见 `users.username='demo'`）。
DEMO_USER = "demo"
DEMO_PASSWORD = "Demo1234!"

PROBLEMS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {label}")
    else:
        PROBLEMS.append(label)
        print(f"  FAIL  {label}  {detail}")


def http(path: str, body: dict | None = None) -> tuple[int, dict]:
    """打真实后端（无 cookie 容器；只用于那条反例与健康检查）。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BACKEND + path,
        data=data,
        method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode())
        except Exception:  # noqa: BLE001
            return error.code, {}
    except Exception as error:  # noqa: BLE001
        return 0, {"_error": str(error)}


def _playwright_script() -> str:
    """浏览器侧的动作脚本。

    写成独立字符串而不是 import：`playwright` 装在 `frontend/node_modules` 里，
    而本脚本由**仓库根目录的 Python** 运行。两者不是同一套依赖树，
    所以用 `node` 跑一段脚本、由它 require 那个包，是最短且不需要改依赖的路径。
    """
    return """
const { chromium } = require('playwright')

const FRONTEND = process.env.FPA_FRONTEND_URL
const USER = process.env.FPA_DEMO_USER
const PASSWORD = process.env.FPA_DEMO_PASSWORD

;(async () => {
  const browser = await chromium.launch()
  const page = await browser.newPage()
  const out = { requests: [], console: [], pageErrors: [] }

  // 记录所有 API 请求：用来证明登录过程**没有**打 /auth/csrf
  page.on('request', (req) => {
    const url = req.url()
    if (url.includes('/api/')) out.requests.push({ url, method: req.method() })
  })
  page.on('console', (msg) => {
    if (msg.type() === 'error') out.console.push(msg.text())
  })
  page.on('pageerror', (err) => out.pageErrors.push(String(err)))

  await page.goto(FRONTEND + '/auth/login', { waitUntil: 'networkidle' })

  // 未登录时**页面上不该有**那条把用户吓到的 CSRF 报错
  const errorLocator = page.getByTestId('login-error')
  out.errorBeforeSubmit = (await errorLocator.count()) > 0
    ? await errorLocator.textContent()
    : null

  await page.getByTestId('login-identifier').fill(USER)
  await page.getByTestId('login-password').fill(PASSWORD)
  await page.getByTestId('login-form').evaluate((f) => f.requestSubmit())

  await page.waitForURL(/\\/ponds$/, { timeout: 15000 }).catch(() => {})

  out.finalUrl = page.url()
  out.errorAfterSubmit = (await errorLocator.count()) > 0
    ? await errorLocator.textContent()
    : null
  // 业务页真的渲染出来了（不是"地址变了但白屏"）
  out.bodyText = (await page.locator('body').innerText()).slice(0, 400)
  out.hasDataTable = (await page.getByTestId('data-table').count()) > 0

  await browser.close()
  console.log(JSON.stringify(out))
})().catch((err) => {
  console.log(JSON.stringify({ fatal: String(err) }))
  process.exit(1)
})
"""


def main() -> int:
    print("#" * 74)
    print("# t7 真实浏览器验收：登录链路（Vite 5273 -> 代理 -> Flask 5101 -> MySQL）")
    print("#" * 74)

    print("\n=== 0. 两个服务是否在跑 ===")
    st, _ = http("/api/v1/auth/csrf")
    check(f"后端 {BACKEND} 可达（/auth/csrf 返回 {st}，未带会话所以 401 是对的）", st == 401, str(st))

    print("\n=== 1. 反例：旧字段名必须被拒（这是本脚本能自证有信号的地方）===")
    st, body = http("/api/v1/auth/login", {"username": DEMO_USER, "password": DEMO_PASSWORD})
    check(
        "旧字段名 `username` 登录被拒（400）—— 若后端被改回读 username，这条会红",
        st == 400,
        f"status={st} body={body.get('message')}",
    )

    print("\n=== 2. 真实浏览器走完整登录（无任何 route 拦截）===")
    env = dict(os.environ)
    env["FPA_FRONTEND_URL"] = FRONTEND_URL
    env["FPA_DEMO_USER"] = DEMO_USER
    env["FPA_DEMO_PASSWORD"] = DEMO_PASSWORD
    script = _playwright_script()
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=str(FRONTEND),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        print("  node 输出:", proc.stdout[-2000:])
        print("  node 错误:", proc.stderr[-2000:])
        check("浏览器脚本跑起来了", False, "见上面的输出")
        return _report()
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError:
        print("  无法解析 node 输出:", proc.stdout[-1500:])
        check("浏览器脚本返回了 JSON", False)
        return _report()

    if out.get("fatal"):
        check("浏览器脚本无致命错误", False, out["fatal"])
        return _report()

    check("提交前登录页**没有**报 CSRF 错（那条红字就是缺陷 1 的症状）",
          not out.get("errorBeforeSubmit"), str(out.get("errorBeforeSubmit")))
    check("登录后落在 /ponds", str(out.get("finalUrl", "")).endswith("/ponds"), str(out.get("finalUrl")))
    check("登录后没有错误提示", not out.get("errorAfterSubmit"), str(out.get("errorAfterSubmit")))
    check("业务页真的渲染出列表（不是白屏）", bool(out.get("hasDataTable")), str(out.get("bodyText"))[:120])
    check("浏览器无未捕获异常", out.get("pageErrors") == [], str(out.get("pageErrors")))

    api_urls = [item["url"] for item in out.get("requests", [])]
    print("\n  API 请求序列：")
    for item in out.get("requests", []):
        print(f"    {item['method']:5s} {item['url'].replace(FRONTEND_URL, '')}")
    check(
        "★ 登录全过程**一次都没打** /api/v1/auth/csrf（死锁的判据）",
        not any("/auth/csrf" in u for u in api_urls),
        str([u for u in api_urls if "/auth/csrf" in u]),
    )
    check(
        "登录请求打到了 /api/v1/auth/login",
        any("/auth/login" in u for u in api_urls),
        str(api_urls),
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
