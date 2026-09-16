"""启动前真实 Harness runtime 配置探针。

退出码：0=配置完整，2=BLOCKED（缺少真实运行前置），1=探针自身错误。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _value(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'BLOCKED'} {label}{f': {detail}' if detail else ''}")
    return ok


def main() -> int:
    blocked = False
    dsh_home = Path(_value("AGENT_DSH_HOME")) if _value("AGENT_DSH_HOME") else None
    harness_root = Path(_value("AGENT_HARNESS_ROOT")) if _value("AGENT_HARNESS_ROOT") else None
    dsh_bin = Path(_value("AGENT_DSH_BIN")) if _value("AGENT_DSH_BIN") else None
    patch = Path(_value("AGENT_HARNESS_PATCH")) if _value("AGENT_HARNESS_PATCH") else ROOT / "agent-runtime" / "cordis.patch.yml"

    for label, path in (
        ("AGENT_DSH_HOME", dsh_home),
        ("AGENT_HARNESS_ROOT", harness_root),
        ("AGENT_DSH_BIN", dsh_bin),
    ):
        ok = path is not None and path.exists()
        blocked |= not _check(label, ok, "未设置或路径不存在" if not ok else str(path))

    patch_ok = patch.is_file()
    blocked |= not _check("Harness patch", patch_ok, str(patch))

    package = ROOT / "agent-runtime" / "lib" / "index.js"
    installed = dsh_home / "profiles" / "sdk" / "node_modules" / "@yuxin" / "dsh-biz-tools" / "lib" / "index.js" if dsh_home else None
    blocked |= not _check("本地 Agent 插件构建物", package.is_file(), str(package))
    blocked |= not _check("DSH_HOME 插件构建物", installed is not None and installed.is_file(), str(installed))

    if dsh_bin and dsh_bin.name.lower() == "run.cmd":
        launcher = dsh_bin.read_text(encoding="utf-8", errors="replace")
        runtime = _value("DSH_NODE_RUNTIME")
        fallback_is_old_project = "\\Desktop\\FPA\\deepseek-harness" in launcher
        runtime_entry = Path(runtime) / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js" if runtime else None
        source_entry = harness_root / "apps" / "cli" / "lib" / "bin.js" if harness_root else None
        if fallback_is_old_project and not runtime:
            blocked |= not _check("DSH_NODE_RUNTIME", False, "run.cmd 仍依赖早期版本 fallback，必须显式设置")
        elif source_entry is not None and source_entry.is_file() and shutil.which("node"):
            blocked |= not _check("Harness source entry", True, str(source_entry))
        else:
            blocked |= not _check("DSH Node runtime entry", runtime_entry is not None and runtime_entry.is_file(), str(runtime_entry))

    api_key_ok = bool(_value("DEEPSEEK_API_KEY"))
    blocked |= not _check("DEEPSEEK_API_KEY", api_key_ok, "未设置" if not api_key_ok else "已设置（不输出密钥）")
    if blocked:
        print("BLOCKED: 真实 Agent E2E 不具备完整 runtime 前置")
        return 2
    print("PASS: 真实 Agent runtime 配置完整")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
