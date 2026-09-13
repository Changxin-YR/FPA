"""t9 真实浏览器验收：智能助手入口 → 打开面板 → 发一句自然语言 → 拿到真实回复。

## 为什么必须走真实链路（不拦任何请求）

`frontend/tests/*.spec.ts` 用 Playwright 的 `page.route()` 把 API 拦掉、在测试里造响应。
那条路**证明了"前端对着我们以为的契约能跑"**，但 t9 的缺陷恰恰在缝里：

  * `HarnessSessionManager` 无人实例化 → `/agent/turns` 503；
  * `AgentPanel.vue` 无人引用 → 界面上根本没有入口；
  * `/agent/turns` 返回 `{reply}` 而不是 §3 的判别联合 → 前端**任何分支都不匹配**，
    界面一片空白而 HTTP 是 200。

这三条**全都不会**被 `page.route()` 拦截式测试发现（拦截时后端根本不参与）。
所以本脚本一处都不拦：真 Vite（5273）→ 真代理 → 真 Flask（5101）→ 真 Harness 子进程
→ 真模型 → 真 MySQL。

## 用法

    # 前置：5101（后端，带 AGENT_DSH_HOME 等 env）与 5273（Vite dev）都在跑
    python tools/agent_browser_e2e.py

它做四件事，每件都断言：
    1. 登录后**看得到助手入口**（t9 之前为零个引用）；
    2. 打开面板、输入一句自然语言、发送；
    3. **等待真实回复出现**（不是"未启用"、不是超时、不是空串）；
    4. 把模型回复**原文**打出来（供 负责人 复核）。

退出码：0 = 全过；1 = 有失败。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

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


def _script() -> str:
    return r"""
const { chromium } = require('playwright')

const FRONTEND = process.env.FPA_FRONTEND_URL
const USER = process.env.FPA_DEMO_USER
const PASSWORD = process.env.FPA_DEMO_PASSWORD
const PROMPT = process.env.FPA_AGENT_PROMPT

;(async () => {
  const out = { loginError: null, hasLauncher: false, reply: '', panelError: '', messages: [], consoleErrors: [] }
  const browser = await chromium.launch()
  const page = await browser.newPage()
  page.on('console', (m) => {
    if (m.type() !== 'error') return
    const text = m.text()
    // 401 是未登录时 guard 的探针，不是缺陷（与 nav_browser_e2e 同一判据）。
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

  // 登录页**不该**有助手（判据 `meta.guestOnly`，不另立名单）。
  //
  // ★ 必须用**全新的匿名 context**：已登录的浏览器访问 `/auth/login` 会被守卫
  //   重定向回业务页（`guestOnly` 的语义），于是"在那里数按钮"量到的是业务页。
  //   我第一版就是这么错的 —— 判据要量的是"**未登录**时登录页有没有助手"。
  const anon = await browser.newContext()
  const anonPage = await anon.newPage()
  await anonPage.goto(FRONTEND + '/auth/login', { waitUntil: 'networkidle' })
  out.guestCheckUrl = anonPage.url()
  out.launcherOnGuestPage =
    (await anonPage.getByRole('button', { name: /塘小助/ }).count()) > 0
  await anon.close()

  // 助手入口：AgentPanel 的 launcher 是一个 button，文案是 assistantName。
  const launcher = page.getByRole('button', { name: /塘小助|关闭塘小助|打开塘小助/ }).first()
  out.hasLauncher = (await launcher.count()) > 0
  if (!out.hasLauncher) {
    out.bodyHead = (await page.locator('body').innerText()).slice(0, 400)
    console.log(JSON.stringify(out))
    await browser.close()
    return
  }

  await launcher.click()
  const panel = page.getByTestId('agent-panel')
  await panel.waitFor({ timeout: 5000 })

  await page.getByTestId('agent-input').fill(PROMPT)
  await page.getByTestId('agent-composer').evaluate((f) => f.requestSubmit())

  // 等真实回复：assistant 消息出现即算拿到（面板把 result.message 追加为 assistant 行）。
  await page.waitForFunction(
    () => {
      const nodes = document.querySelectorAll('.agent-message--assistant')
      return Array.from(nodes).some((n) => (n.textContent || '').trim().length > 0)
    },
    undefined,
    { timeout: 900000 },
  ).catch(() => {})

  out.messages = await page.locator('[data-testid="agent-messages"] article').evaluateAll((nodes) =>
    nodes.map((n) => ({ cls: n.className, text: (n.textContent || '').trim() })),
  )
  // ★ 诊断用：面板**收到流式行**的证据。
  //   statusHint / streamingText 只由 status/delta 行驱动 —— 若它们有内容，
  //   说明浏览器确实走的是流式端点（否则面板回退到一次性响应时这两处永远是空的）。
  const statusBox = page.getByTestId('agent-status')
  out.statusText = (await statusBox.count()) ? ((await statusBox.textContent()) || '') : ''
  const streamBox = page.getByTestId('agent-streaming')
  out.streamTextLen = (await streamBox.count()) ? ((await streamBox.textContent()) || '').length : 0

  const errBox = page.getByTestId('agent-error')
  if (await errBox.count()) out.panelError = (await errBox.textContent()) || ''
  out.confirmationShown = (await page.getByTestId('agent-confirmation').count()) > 0
  out.inputDisabled = await page.getByTestId('agent-input').isDisabled()

  console.log(JSON.stringify(out))
  await browser.close()
})().catch((e) => {
  console.log(JSON.stringify({ fatal: String(e) }))
})
"""


def main() -> int:
    print("#" * 74)
    print("# t9 真实浏览器：智能助手（Vite 5273 → Flask 5101 → Harness → 真模型 → MySQL）")
    print("#" * 74)

    # 服务不可用是阻塞态，不是业务断言失败，更不能被报告成 PASS。
    try:
        import urllib.request

        with urllib.request.urlopen(FRONTEND_URL + "/auth/login", timeout=10) as response:
            if response.status >= 500:
                print(f"BLOCKED: 前端不可用 status={response.status}")
                return 2
    except Exception as error:  # noqa: BLE001 - 前置探针只负责分类，不吞到 PASS
        print(f"BLOCKED: 前端不可用: {error}")
        return 2

    prompt = os.environ.get(
        "FPA_AGENT_PROMPT", "你好，请用一句话回复：你是谁？不要调用任何工具。"
    )
    env = dict(os.environ)
    env.update(
        FPA_FRONTEND_URL=FRONTEND_URL,
        FPA_DEMO_USER=DEMO_USER,
        FPA_DEMO_PASSWORD=DEMO_PASSWORD,
        FPA_AGENT_PROMPT=prompt,
    )
    proc = subprocess.run(
        ["node", "-e", _script()],
        cwd=str(FRONTEND),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
    )
    stdout = (proc.stdout or "").strip()
    if not stdout:
        print("node stdout:", stdout[-800:])
        print("node stderr:", (proc.stderr or "")[-1500:])
        check("浏览器脚本跑起来了", False)
        return _report()
    try:
        out = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError:
        print("无法解析输出:", stdout[-1200:])
        check("脚本返回 JSON", False)
        return _report()
    if out.get("fatal"):
        check("脚本无致命错误", False, out["fatal"])
        return _report()

    print("\n=== 1. 登录与入口 ===")
    check("登录无错误提示", not out.get("loginError"), str(out.get("loginError")))
    check(
        "业务页**看得到**智能助手入口（t9 之前 AgentPanel 被零个组件引用）",
        bool(out.get("hasLauncher")),
        str(out.get("bodyHead", ""))[:200],
    )
    check(
        "登录页**不**显示助手（判据 meta.guestOnly，不另立名单）",
        not out.get("launcherOnGuestPage"),
        f"URL={out.get('guestCheckUrl')} 出现了助手入口",
    )

    print("\n=== 2. 真实回复 ===")
    messages = out.get("messages") or []
    assistant = [m["text"] for m in messages if "assistant" in m.get("cls", "")]
    print(f"  面板消息条数：{len(messages)}（assistant {len(assistant)} 条）")
    check("面板里出现了 assistant 回复", bool(assistant), f"消息：{messages}")
    reply = assistant[-1] if assistant else ""
    check("回复不是空串", bool(reply.strip()), "空回复")
    check(
        "回复不是『未启用 / 不可用』类错误文案",
        not any(word in reply for word in ("未启用", "不可用", "未配置")),
        reply[:200],
    )
    check("面板没有显示错误框", not out.get("panelError"), str(out.get("panelError")))
    check("回复完成后输入框仍可用", not out.get("inputDisabled"), "输入框仍处于 busy")
    check(
        "没有未预期的 console 错误（流式端点 404 是**预期**的：面板按契约回退到非流式）",
        not [e for e in (out.get("consoleErrors") or []) if "404" not in e],
        str(out.get("consoleErrors"))[:300],
    )

    if reply:
        print("\n=== 3. 模型回复原文（负责人 复核用）===")
        print(f"  {reply}")

    print(f"\n  （当前迭代是否出现确认卡片：{bool(out.get('confirmationShown'))}）")
    return _report()


def _report() -> int:
    print()
    if PROBLEMS:
        print(f"结果：{len(PROBLEMS)} 项失败")
        return 1
    print("结果：全部通过 —— 用户能看到助手、且拿到了真实回复")
    return 0


if __name__ == "__main__":
    sys.exit(main())
