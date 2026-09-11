"""组合根：把各业务域的能力声明装载进注册表。

## 为什么需要这个文件（以及它在补齐之前造成的后果）

`domains/<域>/capabilities.py` 里的 `Capability(...)` **只有在模块被 import 时**
才会执行 `REGISTRY.register(...)`。在补齐本文件之前，仓库里**没有任何可运行路径
import 它们**：

* `tools/web_e2e.py` 的 `build_registry()` 自己手搓一个注册表，只注册
  `pond.create` 一条；`tools/live_agent_e2e.py` 把它当夹具用
  （`import web_e2e as harness_fixture`）。
* 于是 master_data 真实声明的 9 条能力（`pond.list` … `pond_status_change.verify`）
  在**任何**自检、任何 HTTP 请求、任何 Agent 轮次里都不存在。

这个失败是**静默**的：能力没注册 → `web/app.py` 的 `_register_capability_routes`
不会为它生成路由 → 该 URL 返回 404；而夹具测试走的是自己注册的那条能力，
所以七套自检可以全绿。这正是 `DEVELOPMENT.md` §4 那条纪律的另一种形态——
**"能力的声明处"与"能力的装载处"是两处**，而它们可以不一致。

修法不是"记得同步"，而是让装载处**没有内容可忘记**：本文件按目录自动发现
`fpa/domains/*/capabilities.py` 并逐个 import。新增一个业务域因此不需要修改
任何共享文件——这也是让剩下的五个域能并行开工、而不在同一个 import 列表上
互相冲突的前提。

## `load_all()` 的契约

1. 发现 `fpa/domains/**/capabilities.py`，导入顺序按名字排序（稳定 → 可复现）；
2. 每个**首次**被导入的域必须至少注册一条能力，注册数为 0 视为错误并抛出。
   这条守卫针对的正是"声明文件存在、但底部漏了 `_register_all()`"：
   那种情况下文件语法合法、导入不报错、什么也不发生；
3. 导入失败**不吞**：一个域目录存在却导不进来，必须立刻暴露；
4. 幂等：重复调用不会重复注册，也不会因为重名而抛错。

## 为什么注册进全局 `REGISTRY`，而不是"传一个注册表进来"

域声明模块直接 `from fpa.kernel.capability import REGISTRY` 并在模块底部调用
`_register_all()`，注册目标是**模块级**的。要支持"传入自定义注册表"，就得改所有
声明模块、把一份声明变成两个参数——那等于把刚删掉的"两处"再加回来。

所以本文件的立场是：**全局注册表就是这个项目的装配事实**。测试需要隔离时自己
`Registry()` 造一个（`tools/runner_e2e.py` 等已经是这么做的），不要试图让域声明
模块可重定向。
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from fpa.kernel.capability import REGISTRY, Registry

#: 域声明的固定文件名。**约定即契约**：发现逻辑只认这个名字，于是"哪些域会被
#: 装载"成了一个不需要在任何地方登记的事实。
CAPABILITIES_MODULE = "capabilities.py"

_DOMAINS_PACKAGE = "fpa.domains"
_DOMAINS_DIR = Path(__file__).resolve().parent / "domains"


def discover_domains() -> tuple[str, ...]:
    """列出所有声明了能力的域，返回相对 `fpa.domains` 的点分路径（已排序）。

    刻意用 `os.walk` + `dirnames` 剪枝，而不是 `Path.rglob`：后者**无法剪枝**，
    会先进入 `__pycache__`（将来还会进入任何新增的大目录）再逐个过滤。
    这是 `DEVELOPMENT.md` §5.2 记过的坑，本文件是仓库里第二个需要遍历目录的地方。
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(_DOMAINS_DIR):
        dirnames[:] = sorted(name for name in dirnames if name != "__pycache__")
        if CAPABILITIES_MODULE in filenames:
            relative = os.path.relpath(dirpath, _DOMAINS_DIR)
            found.append(relative.replace(os.sep, "."))
    return tuple(sorted(found))


def _module_name(domain: str) -> str:
    return f"{_DOMAINS_PACKAGE}.{domain}.{CAPABILITIES_MODULE[:-3]}"


def load_all() -> Registry:
    """装载全部域的能力声明，返回全局注册表。

    反复调用是安全的：第二次调用时模块已在 `sys.modules` 里，import 不重新执行，
    `_register_all()` 的 `REGISTRY.find(...)` 提前返回保证不会重复注册；
    而"注册数为 0"的守卫只对**首次**导入生效，所以不会在第二次调用时误报。
    """
    for domain in discover_domains():
        module_name = _module_name(domain)
        first_import = module_name not in sys.modules
        before = len(REGISTRY.all())
        importlib.import_module(module_name)
        if not first_import:
            continue
        added = len(REGISTRY.all()) - before
        if added == 0:
            raise RuntimeError(
                f"域 `{domain}` 有 {CAPABILITIES_MODULE}，但导入后一条能力都没注册。"
                "最常见的原因是模块底部漏了 `_register_all()` 调用——"
                "这种缺陷语法合法、导入不报错、只是什么也不发生，"
                "所以在组合根这里显式失败，而不是留给运行期的 404。"
            )
    return REGISTRY


def load_report() -> tuple[tuple[str, int], ...]:
    """每个域注册了多少条能力——给架构测试与人工排查用。"""
    load_all()
    report: list[tuple[str, int]] = []
    for domain in discover_domains():
        count = sum(1 for item in REGISTRY.all() if item.domain == domain)
        report.append((domain, count))
    return tuple(report)


__all__ = ["CAPABILITIES_MODULE", "discover_domains", "load_all", "load_report"]
