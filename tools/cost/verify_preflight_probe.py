"""反例验证：`tools/preflight.py` **真的会失败**吗？

## 为什么必须做这个验证

本项目反复吃过"看着像通过其实是空跑"的亏——`check_source_hygiene.py` 曾经因为
`walk()` 路径算错而**扫到 0 个文件却报通过**。一个从不失败的检查与没有检查是一样的，
而且它更糟：**它让人以为已经验证过了**。

所以这里给预检**造一个必然失败的输入**，确认它点名了真正的文件。

## 上一版为什么是坏的（记录在此，避免重犯）

上一版把 `ROOT` 写成 `parents[1]`——那是 `tools/`，不是仓库根。后果：

  1. 在 `tools/backend/yuxin/domains/` 下**误建**探针目录，留下 `tools/backend/` 垃圾树；
  2. 把"预检没抓住"判成失败，**而真相是它查的是另一个路径**；
  3. 报出的 exit=2 是解释器自身的用法错误，我却先归因到被调脚本上。

**结论：脚本路径深度错一格时，症状会伪装成"被测对象有问题"。**
这正是我今天在别人代码上见过多次的形态，这次是我自己犯的。所以本版把 ROOT
用 `parents[2]` 明确写出，并**在跑之前先自检路径**。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

#: `tools/cost/<this file>` -> parents[0]=cost, [1]=tools, [2]=仓库根
ROOT = Path(__file__).resolve().parents[2]
PROBE_DIR = ROOT / "backend" / "yuxin" / "domains" / "zz_preflight_probe"
PYTHON = sys.executable

#: 与真实事故同形：多行 import 语句中间插入裸代码 -> IndentationError
BROKEN = (
    '"""故意坏掉的探针域。"""\n'
    "\n"
    "from __future__ import annotations\n"
    "\n"
    "from yuxin.kernel.capability import (\n"
    '"""\n'
    "    AgentExposure,\n"
    "    Risk,\n"
    ")\n"
)


def run_preflight() -> tuple[int, str]:
    result = subprocess.run(
        [PYTHON, "tools/preflight.py"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8",
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def main() -> int:
    print(f"ROOT = {ROOT}")
    if not (ROOT / "backend" / "yuxin" / "bootstrap.py").exists() or \
       not (ROOT / "tools" / "preflight.py").exists():
        print("  FAIL  路径算错了：ROOT 下找不到 backend/yuxin/bootstrap.py 或 tools/preflight.py")
        print("        （上一版就是这里错了一格，症状伪装成「预检没抓住」）")
        return 1
    print("  OK  路径自检通过\n")

    print("=== 基线：当前树应当是健康的 ===")
    code, output = run_preflight()
    baseline_ok = code == 0
    print(f"  exit={code}（期望 0）  {'OK' if baseline_ok else 'FAIL'}")
    if not baseline_ok:
        print("  基线不健康 —— 先修好当前树再跑本验证（否则分不清是探针生效还是树本来就坏）")
        for line in output.splitlines():
            if "FAIL" in line or "Error" in line:
                print(f"        {line.strip()}")
        return 1

    created = False
    try:
        PROBE_DIR.mkdir(parents=True, exist_ok=True)
        created = True
        (PROBE_DIR / "__init__.py").write_text("", encoding="utf-8", newline="\n")
        (PROBE_DIR / "capabilities.py").write_text(BROKEN, encoding="utf-8", newline="\n")
        print(f"\n=== 造语法坏掉的探针域：{PROBE_DIR.relative_to(ROOT)} ===")

        code, output = run_preflight()
        caught = code == 1 and "zz_preflight_probe" in output
        print(f"  exit={code}（期望 1）  {'OK  预检点名了坏文件' if caught else 'FAIL  没抓住'}")
        for line in output.splitlines():
            if "zz_preflight_probe" in line:
                print(f"        {line.strip()}")
        if not caught:
            print("        --- 完整输出 ---")
            for line in output.splitlines()[:20]:
                print(f"        {line}")
    finally:
        if created and PROBE_DIR.exists():
            shutil.rmtree(PROBE_DIR, ignore_errors=True)
        print(f"\n已清理探针目录：{not PROBE_DIR.exists()}")

    print("\n=== 回归：清理后应恢复健康 ===")
    code, _ = run_preflight()
    print(f"  exit={code}（期望 0）  {'OK' if code == 0 else 'FAIL'}")

    ok = code == 0
    print(f"\n结论：{'预检既能通过健康树、也能点名坏文件（不是空跑）' if ok else '需检查'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
