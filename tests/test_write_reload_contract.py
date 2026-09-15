"""写能力的回读函数契约（`docs/WRITE_CONTRACT.md` 规则 2 / `ROLLOUT_CONTRACT.md` §3）。

## 这条测试补的是哪一类缺陷

`kernel/runner.py::_reload_after` 要求：**任何返回 `resource_id` 的写能力，其 handler
必须能解析出 `__fpa_load_by_id__(tx, *, scope, record_id)`**，否则抛 `INTERNAL_ERROR`。

这个属性只有两处会出现：

* `kernel/runner.py`（读取方，三处 `getattr`）；
* `tools/{runner,web,agent}_e2e.py`（**夹具里手动补挂** `PondService.create.__fpa_load_by_id__ = ...`）。

于是出现过这样的形态：`backend/fpa/domains/` 下一处都没有，**生产路径上写能力全部
500**，而七套自检全绿——因为那七套走的是自己补挂过的夹具。这是 `DEVELOPMENT.md` §4
那条纪律的又一形态（"两处描述同一件事"，其中一处是夹具）。

修法不是"记得挂"，而是让**漏挂在这里变成测试失败**：本文件遍历组合根装载出来的
真实注册表（不是夹具），逐条解析回读函数，并断言它的签名真的能被
`runner._resolve(...)(tx, scope=..., record_id=...)` 调用。

## 为什么断言签名而不只断言"属性存在"

属性存在但签名不对（例如 `def load(record_id)`）时，执行器会在**运行期**抛
`TypeError`——那是个更难查的错误，而且它只在写路径上暴露。签名是这条契约的一部分，
所以在这里一起钉住。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

import fpa.bootstrap as bootstrap

#: master_data 是其余五域的模板，所以对它额外做一次**逐条点名**的断言：
#: 只要有人新增/改名一个写方法，这条测试就会失败，而不是让新方法静默地没有回读函数。
MASTER_DATA_WRITE_CAPABILITIES = (
    "pond.create",
    "pond.update",
    "pond.submit",
    "pond.verify",
    "pond.archive",
    "pond_status_change.request",
    "pond_status_change.verify",
    # t3 新增：往来单位是 §1.4 里第二条可写资源（suppliers/customers 合并后的 partner）
    "partner.create",
    # t18 新增：区域与物料从"只读"升级为"可维护"（registry §1.4 + `DECISIONS.md` Q21）。
    # 这两批各三条（create / update / archive），形状与 `partner.create` 一致。
    "area.create",
    "area.update",
    "area.archive",
    "material.create",
    "material.update",
    "material.archive",
    "material.submit",
    "material.verify",
    # t19 新增：基地创建能力，归属由服务端从当前账号数据范围解析。
    "farm.create",
)


def _load_registry():
    """装载注册表，返回 `(registry, composition_root_ok)`。

    五个域并行开发期间，**任何一个域文件写坏都会让 `bootstrap.load_all()` 对所有人失败**
    （已实测三次：cost 的引号笔误、production 的缩进/语法、production 的 workflow 状态未声明）。
    那时本文件不该整段红掉——**但也不能假装通过了**：降级由返回值显式表达，
    跨域那条测试会据此给出准确的失败原因。
    """
    try:
        return bootstrap.load_all(), True
    except Exception as error:  # noqa: BLE001 - 别人的域正在编辑
        import importlib

        importlib.import_module("fpa.domains.master_data.capabilities")
        from fpa.kernel.capability import REGISTRY

        print(f"\n[隔离模式] 组合根不可用：{type(error).__name__}: {error}")
        return REGISTRY, False


def _master_data_registry():
    """**只**装载 master_data 的声明模块（不碰组合根）。

    本域事实的断言不该依赖别的域的可用性：五个域并行开发期间，任何一个域的文件写坏
    都会让 `bootstrap.load_all()` 失败（实测三次），那时"本域是否漏挂"这个问题的答案
    仍然应当是确定的。`fpa.domains.master_data.capabilities` 的导入链只依赖内核与
    本域自己的模块（含 `service.py` / `resources_read.py` / `ponds_write.py`）。
    """
    import importlib

    importlib.import_module("fpa.domains.master_data.capabilities")
    from fpa.kernel.capability import REGISTRY

    return REGISTRY


def _loaders() -> tuple[list[tuple[str, Any]], bool]:
    """组合根装载出来的全部写能力 → 它的 handler 上解析出的回读函数。"""
    registry, ok = _load_registry()
    return (
        [
            (spec.name, getattr(spec.handler, "__fpa_load_by_id__", None))
            for spec in registry.all()
            if spec.is_write
        ],
        ok,
    )


def test_every_write_capability_declares_a_reload_function() -> None:
    """遍历真实注册表：每条写能力都必须能解析出回读函数。"""
    loaders, composition_root_ok = _loaders()
    assert composition_root_ok, (
        "组合根 `bootstrap.load_all()` 当前不可用（别的域的文件写坏时就会这样），"
        "所以这条**跨域**断言无法执行。这不是本域的问题，但也不能算通过——"
        "请先修好组合根再跑："
        "python -c \"import sys;sys.path.insert(0,'backend');import fpa.bootstrap as b;b.load_all()\""
    )
    missing = sorted(name for name, loader in loaders if not callable(loader))
    assert missing == [], (
        "这些写能力没有回读函数："
        f"{missing}。执行器 `_reload_after` 会抛 INTERNAL_ERROR（HTTP 500），"
        "因为 `executed` 必须是'读回来的行确实是我们想要的样子'，而不是"
        "'我们调用了 INSERT'。请在域内给写能力的处理器挂上 `__fpa_load_by_id__`"
        "（形状见 `domains/master_data/ponds_write.py` 末尾的声明块）。"
    )


def test_master_data_write_capabilities_all_covered() -> None:
    """master_data 是模板：写能力必须与点名清单一一对应。

    这条同时在两个方向上防守：少一条（改名的写方法没补回读函数）与多一条
    （新增写能力却没登记）都会失败。
    """
    registry = _master_data_registry()
    declared = {
        spec.name
        for spec in registry.all()
        if spec.is_write and spec.domain == "master_data"
    }
    assert declared == set(MASTER_DATA_WRITE_CAPABILITIES), (
        f"master_data 的写能力集合变了：{sorted(declared)}\n"
        f"点名清单是 {sorted(MASTER_DATA_WRITE_CAPABILITIES)}。"
        "新增写能力时，请同时补上回读函数与这条清单。"
    )


def test_domain_guard_passes_for_master_data() -> None:
    """域自己的守卫（`capabilities.py::assert_reload_wired`）必须通过。

    它断言的是**注册表里的写能力**，与本文件的遍历是两种取数方式：这里保证"任何一个域
    漏挂都会被拦住"，域内守卫保证"本域 import 期就炸"。两条都要有——只留一条时，
    另一条的失效模式（漏改清单 / 只在某个 import 路径下才跑）就无人看着了。
    """
    _master_data_registry()
    from fpa.domains.master_data.capabilities import assert_reload_wired

    assert_reload_wired()


@pytest.mark.parametrize("name", MASTER_DATA_WRITE_CAPABILITIES)
def test_master_data_reload_signature_is_callable_by_runner(name: str) -> None:
    """回读函数的签名必须能被 `_resolve` 解析后按执行器的方式调用。

    执行器的调用形态（`runner.py` 三处都一样）::

        self._resolve(spec, raw_loader)(tx, scope=scope, record_id=int(...))

    所以签名里必须有 `tx`（位置参数）与 `scope` / `record_id`（关键字参数）。
    """
    spec = _master_data_registry().get(name)
    loader = getattr(spec.handler, "__fpa_load_by_id__", None)
    assert callable(loader), f"{name} 没有回读函数"

    factory = spec.service_factory
    if factory is not None and not hasattr(loader, "__self__"):
        bound = getattr(factory(), loader.__name__, None)
        assert bound is not None, f"{name} 的 service_factory 上没有 {loader.__name__}"
        loader = bound

    parameters = inspect.signature(loader).parameters
    assert "tx" in parameters, f"{name} 的回读函数第一个参数必须叫 tx：{parameters}"
    assert parameters["tx"].kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ), f"{name} 的 tx 必须是位置参数"
    for keyword in ("scope", "record_id"):
        assert keyword in parameters, f"{name} 的回读函数缺少关键字参数 {keyword}：{parameters}"
        assert parameters[keyword].kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ), f"{name} 的 {keyword} 必须可按关键字传"
