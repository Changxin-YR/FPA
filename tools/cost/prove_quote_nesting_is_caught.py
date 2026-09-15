"""反例验证：`tools/cost/scan_python_syntax.py` 能抓住"中文文案里嵌套同种引号"吗？

起因：我今天两次写出下面这种字符串，两次都让整个文件语法错误、并让
`bootstrap.load_all()` 对全队失败：

    "归集金额必须大于 0（成本记录只表达"支出发生了"，冲销记在下一期间）"

内置的语法扫描用的是 `ast.parse()`，Python 解析器本来就会拒绝它——
关键不是"能不能检测"，而是**检测到之后有没有指向正确的文件**。
本脚本同时演示两件事：
  1. 该形态确实会被拒绝（Python 层面）；
  2. 正则式启发（找相邻引号）**不可用**——合法代码里遍地都是 `= ""`。

结论会是"用语法扫描，不用正则启发"。
"""

from __future__ import annotations

import ast
import re

#: 与事故同形：**嵌在合法函数体里**（否则缩进本身就先报错，测不出引号问题）
BROKEN = (
    "def f():\n"
    '    raise ValueError(\n'
    '        "归集金额必须大于 0（成本记录只表达"支出发生了"，冲销记在下一期间）",\n'
    "    )\n"
)
#: 修好之后（内层改全角引号）
FIXED = (
    "def f():\n"
    '    raise ValueError(\n'
    '        "归集金额必须大于 0（成本记录只表达「支出发生了」，冲销记在下一期间）",\n'
    "    )\n"
)

#: 常见合法代码
LEGIT = [
    'def f(x: str = "") -> None:\n    pass\n',
    'token = request.headers.get("X-Agent-Context", "")\n',
    'conversation_id = str(payload.get("cid") or "") or None\n',
]


def main() -> int:
    print("=== 1. 坏形态是否被 ast.parse 拒绝 ===")
    try:
        ast.parse(BROKEN)
        print("  未被拒绝 —— 意外")
    except SyntaxError as exc:
        print(f"  OK  被拒绝：line {exc.lineno}: {exc.msg}")

    print("\n=== 2. 修好之后应可解析 ===")
    try:
        ast.parse(FIXED)
        print("  OK  可解析")
    except SyntaxError as exc:
        print(f"  FAIL 仍不可解析：{exc.msg}")

    print("\n=== 3. 正则启发（相邻引号）在合法代码上的误报 ===")
    pattern = re.compile(r'""\S')
    false_positives = sum(1 for line in LEGIT if pattern.search(line))
    print(f"  {false_positives}/{len(LEGIT)} 条**合法**代码被命中 -> 这条启发不可用作门禁")

    print("\n=== 4. 结论 ===")
    print("  用 `ast.parse()`（现成的 scan_python_syntax.py / preflight.py），")
    print("  不要用正则找引号 —— 合法代码里 `= \"\"` 遍地都是，加进去只会变成噪声。")
    print("  真正要补的不是检测，而是**让检测指向正确的文件**：")
    print("  `tests/test_architecture.py::_imported()` 目前没有 except SyntaxError，")
    print("  于是它会把这个语法错误报成「MySQL 驱动隔离违规」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
