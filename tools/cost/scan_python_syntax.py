"""找出让 `test_mysql_driver_is_confined_to_one_module` 失败的**真正原因**。

该测试用 `ast.parse()` 逐个解析 `backend/yuxin` 下的 .py；`_imported()` 没有捕获
SyntaxError，所以**任何一个文件语法有误，这条"驱动隔离"断言就会失败**——
报错信息会指向驱动，而真实原因在别人的文件里。这正是"报告的表象与根因不一致"。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
PACKAGE = BACKEND / "yuxin"


def python_files(root: Path):
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def main() -> int:
    broken: list[tuple[Path, str]] = []
    total = 0
    for path in python_files(PACKAGE):
        total += 1
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            broken.append((path, f"line {exc.lineno}: {exc.msg}"))

    print(f"扫描 {total} 个 .py；语法有误的 {len(broken)} 个：")
    for path, detail in broken:
        print(f"   {path.relative_to(BACKEND)}  ->  {detail}")

    # ★ 空输入不是"通过"，是"没检查"。
    # 本脚本从 .local/ 搬到 tools/cost/ 时路径深度没改，于是它**扫到 0 个文件
    # 并打印"没有语法错误"** —— 看起来像通过，实际什么都没看。
    # 修法不是"下次记得改路径"，而是让 0 个文件成为**显式失败**
    # （与 DEVELOPMENT 记的 check_source_hygiene 那个假阳性同族）。
    if total == 0:
        print(f"\nFAIL  一个文件都没扫到 —— 路径算错了？BACKEND={BACKEND}")
        print("      空输入不是通过，是没检查。")
        return 1

    if broken:
        print("\n结论：`test_mysql_driver_is_confined_to_one_module` 的失败**不是**驱动违规，")
        print("      而是上面这些文件的语法错误让 `ast.parse()` 在测试内部抛错。")
        print("      修法是修语法，不是改 import。")
        return 1
    print("\n结论：没有语法错误——若该测试仍失败，才是真的驱动违规。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
