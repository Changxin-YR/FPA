"""`source_index` 的守卫：语法错误必须指向**那个文件**，不能伪装成别的断言失败。

## 这条测试守的是一次真实事故

`tests/test_architecture.py` 原先自己实现 `_imported()`，里面 `ast.parse` **没有捕获
`SyntaxError`**。于是某个域文件里的一处坏注释（丢掉行首 `#`）让

    test_mysql_driver_is_confined_to_one_module  FAILED
    AssertionError: MySQL 驱动只能出现在 fpa.kernel.uow；实测出现在：['fpa.domains.cost...

——**驱动违规是假的**，真问题是另一个文件语法坏了。负责人 与 cost-dev 都被误导过。

本文件把"报错指向真文件"这件事钉住：
  1. 语法错误 → `AssertionError`，消息里带**文件路径**与 `SyntaxError` 原文；
  2. 一个语法错误**只**让这一条失败，不会因遍历顺序放大成多条；
  3. 正常文件仍能解析出 import（守卫不能因为吞错误而变成空转）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import source_index


def test_syntax_error_points_at_the_offending_file(tmp_path: Path) -> None:
    broken = tmp_path / "broken_module.py"
    broken.write_text("def oops(:\n", encoding="utf-8", newline="\n")

    with pytest.raises(AssertionError) as excinfo:
        source_index.imported(broken)

    message = str(excinfo.value)
    assert str(broken) in message, f"报错必须点名真正的文件，实际是：{message}"
    assert "语法错误" in message
    assert "第 1 行" in message, f"要带行号，实际是：{message}"


def test_one_syntax_error_fails_once_not_per_call(tmp_path: Path) -> None:
    """同一个坏文件反复解析，抛的是同一种错误（不会因为调用次数变成多种失败）。"""
    broken = tmp_path / "broken_again.py"
    broken.write_text("class X(:\n", encoding="utf-8", newline="\n")

    messages = set()
    for _ in range(3):
        with pytest.raises(AssertionError) as excinfo:
            source_index.imported(broken)
        messages.add(str(excinfo.value).split("：", 1)[0])
    assert len(messages) == 1, f"同一次错误报出了不同主题：{messages}"


def test_healthy_file_still_yields_its_imports() -> None:
    kernel = source_index.PACKAGE / "kernel" / "uow.py"
    assert kernel.is_file(), "测试前提失效：kernel/uow.py 不存在"
    found = source_index.imported(kernel)
    assert "pymysql" in found, f"守卫不能吞错误也不能空转；实际解析到：{sorted(found)[:8]}"


def test_module_name_maps_files_to_dotted_paths() -> None:
    assert source_index.module_name(source_index.PACKAGE / "kernel" / "uow.py") == "fpa.kernel.uow"
    assert source_index.module_name(source_index.PACKAGE / "web" / "__init__.py") == "fpa.web"
