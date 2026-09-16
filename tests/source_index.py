"""源码索引工具：把"遍历 Python 文件 + 解析 import"的公共部分放一处。

## 为什么单独一个文件

`tests/test_architecture.py` 里原本有一个 `_imported()`，它**没有捕获 `SyntaxError`**：

    tree = ast.parse(path.read_text(encoding="utf-8"))

后果不是"少查了一个文件"，而是**报错指向错误的主题**。真实事故：某个域文件里有一处
坏注释（丢掉行首 `#`，于是全角括号落到代码位置），任何调用 `_imported()` 的断言都会
在 `ast.parse` 处抛 `SyntaxError`，pytest 于是把它报成

    test_mysql_driver_is_confined_to_one_module  FAILED
    AssertionError: MySQL 驱动只能出现在 yuxin.kernel.uow；实测出现在：['yuxin.domains.cost...

——**驱动违规是假的**，真正的问题是另一个文件有语法错误。负责人 与 cost-dev 都被这条
误导过，最后靠一个独立扫描器才定位。

**本文件的规矩**：解析失败当场转成 `AssertionError`，消息里带**文件路径 + 语法错误原文**。
失败条数因此与被解析的文件数无关（不会"一个语法错误放大成几十条红"），而且第一条就指向
真正的文件。"扫到 0 个文件"同理会被拦下——空集不得被当成通过（同一形态的守卫在
`tools/check_source_hygiene.py` 的 `walk()` 里也有一份）。

## 为什么放在 `tests/` 而不是 `tools/`

它只服务于测试（`tests/` 下的断言）；放进 `tools/` 会让它看起来像交付工具，而它没有
命令行入口。若将来别的检查也要用，再按"两处描述同一件事就删一处"的原则上提。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Iterator

BACKEND = Path(__file__).resolve().parents[1] / "backend"
PACKAGE = BACKEND / "yuxin"


def python_files(root: Path) -> Iterator[Path]:
    """遍历目录下的 `.py`。

    用 `os.walk` + `dirnames` 剪枝，不用 `Path.rglob`：后者无法剪枝，
    会进入 `node_modules` / `.dsh-home` 再逐个过滤（`DEVELOPMENT.md` §5.2 记过的坑）。
    """
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name != "__pycache__")
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def module_name(path: Path) -> str:
    relative = path.relative_to(BACKEND).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _parse(path: Path) -> ast.Module:
    """解析一个文件；**语法错误要指向那个文件**，不要伪装成别的断言失败。"""
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        raise AssertionError(
            f"{path} 有语法错误，无法进行依赖方向检查："
            f"第 {exc.lineno} 行 {exc.msg}。"
            f"先修这个文件——否则本断言的主题（依赖方向）根本没被检查到。"
        ) from exc


def imported(path: Path) -> set[str]:
    """模块**静态** import 的顶层模块名（相对 import 不计：那是包内引用）。"""
    tree = _parse(path)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module)
    return found


def matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def modules_under(root: Path) -> list[str]:
    """目录下所有模块名（供"扫到 0 个文件即失败"这类守卫使用）。"""
    return sorted(module_name(path) for path in python_files(root))
