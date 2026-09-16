"""验证一个**残余缺口**：没被任何 import 链触及的模块，`load_all()` 查不出来。

队长提出的纪律是"结束任何一轮之前跑一次 `bootstrap.load_all()`"，并指出
`compileall` 查不出 `NameError`（只有真去 import 才会炸）。两条都成立。

但还有一层：`load_all()` 只 import **被触及的那部分**。
一个孤儿模块（例如新写的、还没被 `capabilities.py` 引用的 `.py`，
或 `tools/` 之外的一次性脚本）即使有运行期 NameError，
`load_all()` 也**不会**碰到它 —— 而它一旦被引入就会炸。

本脚本用一个探针证明这个缺口，并给出修法方向。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile

BACKEND = pathlib.Path(__file__).resolve().parents[2] / "backend"

#: 语法合法、但 import 时 NameError 的模块
ORPHAN = "@dataclass\nclass X:\n    pass\n"

PROBE = BACKEND / "yuxin" / "domains" / "cost" / "_orphan_probe.py"


def main() -> int:
    print("=== 造一个**孤儿模块**（语法合法、import 时 NameError、没有任何人引用它）===")
    print(f"    {PROBE.relative_to(BACKEND.parent)}")
    try:
        PROBE.write_text(ORPHAN, encoding="utf-8", newline="\n")

        for label, command in (
            ("compileall", [sys.executable, "-m", "compileall", "-q", str(BACKEND)]),
            ("load_all", [sys.executable, "-c",
                          "import sys;sys.path.insert(0,'backend');"
                          "import yuxin.bootstrap as b;b.load_all();print('OK')"]),
        ):
            result = subprocess.run(command, cwd=BACKEND.parent,
                                    capture_output=True, text=True, encoding="utf-8")
            verdict = "通过（查不出来）" if result.returncode == 0 else "失败（查出来了）"
            print(f"  {label:12} exit={result.returncode}  {verdict}")

        # 真的 import 它才会炸
        import importlib

        sys.path.insert(0, str(BACKEND))
        try:
            importlib.import_module("yuxin.domains.cost._orphan_probe")
            print("  import 该模块 -> 通过（意外）")
        except NameError as exc:
            print(f"  import 该模块 -> NameError: {exc}")
    finally:
        if PROBE.exists():
            PROBE.unlink()
        # 顺手清掉可能的 pyc
        cache = PROBE.parent / "__pycache__"
        if cache.is_dir():
            for stale in cache.glob("*_orphan_probe*"):
                stale.unlink(missing_ok=True)

    print("\n=== 结论 ===")
    print("  `compileall` 与 `load_all()` **都**查不出孤儿模块的运行期名称错误。")
    print("  修法：预检除了 `load_all()`，还应当**逐个 import** `backend/yuxin` 下的模块，")
    print("        把 import 失败（不只是语法失败）也点名报出来。")
    print("  对改动者本人的纪律仍然是队长那条：结束一轮前跑 `bootstrap.load_all()`；")
    print("  但对**新写的、还没接线的文件**，只有逐个 import 才拦得住。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
