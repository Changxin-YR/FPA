"""跑任何东西之前的一行检查：**当前树的语法与装配是否健康**。

## 为什么需要这个脚本

同一个"`cost/capabilities.py` 语法错误、装配已断"的报告，在 20 分钟内被**三位成员**
转述给我（并各自附上当时的实测输出）。三次都是真的——它们都产生于那个 19 分钟的
破损窗口（22:09:04 落盘 → 22:28:54 修复）。但三次都被当成**当前状态**读。

消失的东西：**没有人重新跑一次**。于是每个人都在修一个已经不存在的问题，
而真正的阻塞（谁的测试以什么名义红了）反而被这段历史噪音盖住。

这不是谁不认真，而是**"报告里没有时效性"这个结构性缺陷**：
一份带实测输出的报告看起来总是可信，而实测输出**没有时间戳**。

所以修法不是"下次记得复核"，而是把复核变成**一条命令、一秒出结果**：

    python tools/preflight.py

它做三件事，且刻意不做第四件：
  1. **语法**：解析 `backend/yuxin` 下每个 .py —— 语法错误没有公共门禁
     （`check_source_hygiene.py` 不看语法；`test_architecture._imported()` 没有
     `except SyntaxError`），所以它只能在这里被点名；
  2. **装配**：`bootstrap.load_all()` 能否装载，以及每个域注册了多少条能力；
  3. **门禁**：两条公共门禁（卫生 / 迁移 self-test）的退出码。

不做的事：**不跑 pytest**。那由你说"全绿"时负责跑；本脚本只回答
"现在这棵树能不能被 import"。把两件事分开，是因为它们的耗时与信号完全不同——
预检要快到"别人报告某事坏了，我随手一跑就知道"。

退出码：0 = 健康；1 = 有问题（并打印**文件名与行号**）。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PACKAGE = BACKEND / "yuxin"


def python_files(root: Path):
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def check_syntax() -> tuple[bool, list[str]]:
    """逐个文件 `ast.parse`。

    为什么逐个而不是 `import`：`import` 会执行模块顶层代码（可能有副作用），
    而语法错误只需要解析。也正因为用 `ast.parse`，它能抓到"装配成功但某个
    文件坏了"的情况——只要那个文件被 import 到就晚了，而没被 import 的
    文件坏了则完全没人知道。
    """
    broken: list[str] = []
    total = 0
    for path in python_files(PACKAGE):
        total += 1
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            broken.append(f"{path.relative_to(ROOT)}  line {exc.lineno}: {exc.msg}")
    if total == 0:
        # 空输入不是"通过"，是"没检查"（本项目记过的假阳性形态）
        broken.append(f"一个 .py 都没扫到 —— 路径算错了？PACKAGE={PACKAGE}")
    return not broken, broken


def check_bootstrap() -> tuple[bool, list[str]]:
    """装载组合根，返回每个域注册的能力数。"""
    sys.path.insert(0, str(BACKEND))
    try:
        from yuxin.bootstrap import load_all, load_report

        registry = load_all()
        lines = [f"共 {len(registry.all())} 条能力"]
        for domain, count in load_report():
            lines.append(f"    {domain}: {count}")
        return True, lines
    except Exception as exc:  # noqa: BLE001 - 这里就是要报告任何失败
        import traceback

        where = traceback.extract_tb(exc.__traceback__)[-1]
        return False, [
            f"{type(exc).__name__}: {exc}",
            f"    最后一行：{where.filename}:{where.lineno}",
        ]


def check_import_every_module() -> tuple[bool, list[str]]:
    """逐个 import `backend/yuxin` 下的**每个**模块。

    ## 为什么 `ast.parse` 和 `load_all()` 都不够

    一个真实的例子（今天就发生在 `cost/entries.py`）：

        @dataclass                      # 用了 @dataclass，但忘了 import
        class CostFact: ...

    这段代码**语法完全合法**，所以 `ast.parse()`（第 1 项）通过；
    它是个运行期 `NameError`，所以 `compileall` 也通过。
    只有当某条 import 链真的走到它时才会炸 —— 而它当时**还没被
    `cost/capabilities.py` 引用**，于是既没被 `load_all()` 触及，
    也没被任何测试触及：整整一个窗口里，它是"没人看得见的坏文件"。

    `load_all()` 只 import **被组合根触及的那部分**。新写的、还没接线的孤儿模块，
    只有逐个 import 才能拦得住。（本模块的这条结论有可复现验证：
    `tools/cost/prove_orphan_module_gap.py`。）

    代价实测：69 个模块 0.25 秒，且没有任何模块在 import 期连库或写文件。
    对"一轮一次"的预检完全可接受。
    """
    import importlib
    import time

    sys.path.insert(0, str(BACKEND))
    names = [
        ".".join(parts)
        for parts in (
            _module_parts(path) for path in python_files(PACKAGE)
        )
        if parts
    ]
    failed: list[str] = []
    started = time.perf_counter()
    for name in sorted(set(names)):
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            import traceback

            where = traceback.extract_tb(exc.__traceback__)[-1]
            failed.append(
                f"{name}: {type(exc).__name__}: {exc}"
                f"  ({where.filename}:{where.lineno})"
            )
    elapsed = time.perf_counter() - started

    if not names:
        failed.append(f"一个模块都没扫到 —— 路径算错了？PACKAGE={PACKAGE}")
        return False, failed

    failed.append(f"（共 {len(set(names))} 个模块，耗时 {elapsed:.2f}s）")
    return not any(":" in line and "  (" in line for line in failed), failed


def _module_parts(path: Path) -> tuple[str, ...]:
    """`.../backend/yuxin/domains/cost/entries.py` -> `('yuxin','domains','cost','entries')`"""
    try:
        relative = path.relative_to(BACKEND).with_suffix("")
    except ValueError:
        return ()
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return tuple(parts)


def check_gate(name: str, args: list[str]) -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    detail = (result.stdout or "").strip().splitlines()
    tail = detail[-1] if detail else "(无输出)"
    return result.returncode == 0, f"{name}: exit={result.returncode}  {tail}"


def main() -> int:
    failures = 0

    print("=== 1. 语法（backend/yuxin 逐个 ast.parse）===")
    ok, lines = check_syntax()
    if ok:
        count = sum(1 for _ in python_files(PACKAGE))
        print(f"  OK  {count} 个 .py 全部可解析")
    else:
        failures += 1
        print("  FAIL  以下文件语法有误（这就是「某个域坏了却以别人的名义报红」的根因）：")
        for line in lines:
            print(f"        {line}")

    print("\n=== 2. 逐个 import 每个模块（拦运行期名称错误）===")
    ok, lines = check_import_every_module()
    if ok:
        print(f"  OK  {lines[0] if lines else ''}")
    else:
        failures += 1
        print("  FAIL  以下模块 import 失败（`ast.parse`/`compileall` 查不出这类错误）：")
        for line in lines:
            print(f"        {line}")

    print("\n=== 3. 组合根装载 bootstrap.load_all() ===")
    ok, lines = check_bootstrap()
    if ok:
        print("  OK  组合根装载成功")
        for line in lines:
            print(f"    {line}")
    else:
        failures += 1
        print("  FAIL  组合根装载失败：")
        for line in lines:
            print(f"        {line}")

    print("\n=== 4. 公共门禁 ===")
    for name, args in (
        ("卫生检查", ["tools/check_source_hygiene.py"]),
        ("迁移 self-test", ["tools/migrate_selftest.py"]),
    ):
        ok, detail = check_gate(name, args)
        print(f"  {'OK  ' if ok else 'FAIL'} {detail}")
        if not ok:
            failures += 1

    print()
    if failures:
        print(f"预检结果：{failures} 项有问题 —— 先修上面点名的文件，再跑 pytest。")
        return 1
    print("预检结果：健康（语法 / 装配 / 门禁全通）。")
    print("下一步才是 `python -m pytest tests -q`。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
