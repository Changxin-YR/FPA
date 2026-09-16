"""线上站点智能体验收（真浏览器 + 真后端 + 真模型）。

用法::

    python tools/live_site_agent_check.py
    python tools/live_site_agent_check.py --base https://23331.cloud/yuxin --identifier demo --password Demo1234!
    python tools/live_site_agent_check.py --question "现在一共有几个塘口？"

它做的事：登录 → 打开「塘小助」面板 → 提问 → 取回复 → 断言非空且不含错误标记。
失败退出码非 0，便于挂进发布流程。

依赖：`playwright`（`pip install playwright && playwright install chromium`）。
"""

from __future__ import annotations

import argparse
import sys

DEFAULT_BASE = "https://23331.cloud/yuxin"
DEFAULT_IDENTIFIER = "demo"
DEFAULT_PASSWORD = "Demo1234!"
DEFAULT_QUESTION = "现在一共有几个塘口？用一句话回答。"
#: 这些字样出现在回复里即判失败（后端/网关的错误兜底文案）。
ERROR_MARKERS = ("暂时不可用", "智能助手通信异常", "请稍后重试", "执行失败")


def main() -> int:
    parser = argparse.ArgumentParser(description="线上站点智能体验收")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--identifier", default=DEFAULT_IDENTIFIER)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--timeout", type=int, default=180, help="等待回复的秒数（首次要起 Harness）")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright：pip install playwright && playwright install chromium")
        return 2

    base = args.base.rstrip("/")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        page.goto(f"{base}/auth/login", wait_until="networkidle", timeout=40000)
        print(f"登录页标题: {page.title()}")
        page.fill('input[type="text"]', args.identifier)
        page.fill('input[type="password"]', args.password)
        page.click('button[type="submit"]')
        page.wait_for_timeout(2500)
        print(f"登录后 URL: {page.url}")

        page.locator('button[aria-label*="塘小助"]').first.click()
        page.wait_for_timeout(1000)
        page.fill('[data-testid="agent-input"]', args.question)
        page.click('[data-testid="agent-composer"] button[type="submit"]')
        print(f"已提问: {args.question}")

        reply = ""
        for _ in range(max(1, args.timeout // 3)):
            page.wait_for_timeout(3000)
            rows = page.locator('[data-testid="agent-messages"] article')
            texts = [rows.nth(i).inner_text().strip() for i in range(rows.count())]
            answers = [t for t in texts if t and args.question[:8] not in t]
            if answers:
                reply = answers[-1]
                break
            if page.locator('[data-testid="agent-error"]').count():
                reply = page.locator('[data-testid="agent-error"]').inner_text().strip()
                break
        browser.close()

    print("---- 助手回复 ----")
    print(reply or "(超时未收到回复)")
    if not reply or any(marker in reply for marker in ERROR_MARKERS):
        print("结果：失败")
        return 1
    print("结果：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
