"""反例验证：`tools/preflight.py` 的第 2 项**真的能拦住运行期名称错误**吗？

## 为什么需要这个验证

今天的真实事故：`cost/entries.py` 用了 `@dataclass` 但忘了 import。
那段代码**语法完全合法** —— 所以 `ast.parse` 通过、`compileall` 通过；
它是**运行期** `NameError`。当时它还没被 `capabilities.py` 引用，
于是 `load_all()` 也没触及它。**整整一个窗口里它是一个没人看得见的坏文件。**

预检新增的第 2 项（逐个 import 每个模块）就是为了拦这种情况。
一个"从不失败"的检查与没有检查等价，所以这里给它造一个必然失败的输入。

判据：造一个孤儿模块（无人引用、import 时 NameError），
预检必须**点名它**，且清理后恢复健康。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROBE_DIR = ROOT / "backend" / "yuxin" / "domains" / "_preflight_import_probe"
PYTHON = sys.executable

#: 与事故同形：语法合法，import 时 NameError
ORPHAN = (
    '"""孤儿探针：用了 @dataclass 但没 import（与真实事故同形）。"""\n'
    "\n"
    "@dataclass\n"
    "class Probe:\n"
    "    value: int = 0\n"
)


def run_preflight() -> tuple[int, str]:
    result = subprocess.run(
        [PYTHON, "tools/preflight.py"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8",
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def main() -> int:
    print(f"ROOT = {ROOT}")
    if not (ROOT / "tools" / "preflight.py").exists():
        print("  FAIL  路径算错：找不到 tools/preflight.py")
        return 1

    print("\n=== 基线：应当健康 ===")
    code, output = run_preflight()
    print(f"  exit={code}（期望 0）")
    if code != 0:
        print("  基线不健康 —— 先修好当前树再跑本验证")
        for line in output.splitlines():
            if "FAIL" in line:
                print(f"        {line.strip()}")
        return 1

    created = False
    try:
        PROBE_DIR.mkdir(parents=True, exist_ok=True)
        created = True
        (PROBE_DIR / "__init__.py").write_text("", encoding="utf-8", newline="\n")
        (PROBE_DIR / "orphan.py").write_text(ORPHAN, encoding="utf-8", newline="\n")
        print(f"\n=== 造孤儿模块：{ (PROBE_DIR / 'orphan.py').relative_to(ROOT) } ===")
        print("    （语法合法，import 时 NameError，且没有人引用它）")

        code, output = run_preflight()
        caught = code == 1 and "_preflight_import_probe" in output
        print(f"\n  exit={code}（期望 1）  {'OK  预检点名了它' if caught else 'FAIL  没抓住'}")
        for line in output.splitlines():
            if "_preflight_import_probe" in line or "NameError" in line:
                print(f"        {line.strip()}")

        # 对照：单看第 1 项（语法）不该发现问题 —— 这解释了"它当初为什么不可见"
        import ast

        ast.parse(ORPHAN)
        print("\n  对照：同一段代码 `ast.parse` 通过（所以第 1 项与本项目的 compileall 都查不出）")
    finally:
        if created and PROBE_DIR.exists():
            shutil.rmtree(PROBE_DIR, ignore_errors=True)
        print(f"\n已清理探针目录：{not PROBE_DIR.exists()}")

    print("\n=== 回归：清理后应恢复健康 ===")
    code, _ = run_preflight()
    print(f"  exit={code}（期望 0）  {'OK' if code == 0 else 'FAIL'}")

    ok = code == 0
    print(f"\n结论：{'第 2 项确实能拦住这类错误（不是空跑）' if ok else '需检查'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
