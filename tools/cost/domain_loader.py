"""逐域装载注册表，**跳过装载失败的域但明确报告它们**。

## 为什么需要这一层（而不是直接用 `bootstrap.load_all()`）

`load_all()` 的契约是"一个域导不进来就立刻暴露"，这对**生产入口**是正确的：
装配不完整就不该启动。但在**工具与自检**里它是错的选择——

    并行开发期间任何一个域的进行中编辑（语法错误、无效的状态机声明、
    引用了不存在的 API），都会让所有工具的派生结果**不完整**，
    而它们看起来仍然"成功"。

实测发生过：`sales` 域报 `NameError: _choices` 时，权限码派生从 47 个降到 39 个。
如果工具只是 `load_all()` 崩掉，或者静默跳过，都会给出错误结论；
正确做法是**继续派生、但把"少了哪些域"当成一等输出**，并让 `--check` 失败。

本模块把那套逻辑收成一处，避免每个工具各写一遍（那正是本项目反对的
"两处描述同一件事"）。

## 用法

    from domain_loader import load_registry_tolerant

    registry, failed = load_registry_tolerant()
    if failed:
        print("以下域未装载，本次结果不完整：", failed)
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def load_registry_tolerant() -> tuple[object, list[str]]:
    """逐域 import；返回 `(REGISTRY, 失败域描述列表)`。

    `failed` 为空表示派生结果完整。**调用方必须对它做点什么**——
    打印、或让检查失败。忽略它就等于静默接受不完整的结论。
    """
    from fpa.bootstrap import _module_name, discover_domains
    from fpa.kernel.capability import REGISTRY

    failed: list[str] = []
    for domain in discover_domains():
        try:
            importlib.import_module(_module_name(domain))
        except Exception as exc:  # noqa: BLE001 - 记下来，不静默跳过
            failed.append(f"{domain}: {type(exc).__name__}: {exc}")
    return REGISTRY, failed
