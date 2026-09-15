"""`Capability.loader` 契约的**反例测试**。

## 为什么必须有这个文件

`tests/test_architecture.py` 里那三条断言现在全绿。但"全绿"有两种可能：
断言真的在把关，或者断言**永远为真**。本仓已经吃过这个亏两次
（`check_source_hygiene.py` 曾静默扫描 0 个文件却报"通过"；字段集为空导致校验
既不放行也不拒绝）。所以每条断言都要配一个**它必须失败**的输入。

这里的做法是把违规的能力**注入到真实 REGISTRY** 再跑那条断言，最后用 fixture
把它摘掉。为什么注入真实注册表而不是另造一个：断言的实现就是遍历 `REGISTRY.all()`，
如果测试喂给它一个自造的注册表，那测的是"自造注册表"而不是"生产装配"——
即**用夹具盖住生产路径**，正是当前迭代反复出现的那个缺陷形态。
"""

from __future__ import annotations

import pytest

from fpa.kernel.capability import (
    Capability,
    HandlerResult,
    HttpMethod,
    NO_LOADER,
    REGISTRY,
    Risk,
)
from fpa.kernel.fields import f_str

import fpa.bootstrap as bootstrap

#: 直接 import 那三条断言，而不是在本地复制一份。
#:
#: `tests/` 不是包（没有 `__init__.py`，`conftest.py` 只往 `sys.path` 里塞了 backend），
#: 所以 `from tests.test_architecture import ...` 会 ModuleNotFoundError。
#: 按文件路径加载能拿到**同一个函数对象**——这一点很重要：复制一份断言会让
#: "反例测的那个断言"与"生产门禁跑的那个断言"变成两份可以被改散的副本。
import importlib.util as _importlib_util
from pathlib import Path as _Path

_ARCH_PATH = _Path(__file__).resolve().parent / "test_architecture.py"
_SPEC = _importlib_util.spec_from_file_location("_fpa_test_architecture", _ARCH_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_ARCH = _importlib_util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_ARCH)

declares_loader = _ARCH.test_every_write_capability_declares_its_loader
not_ambiguous = _ARCH.test_loader_declaration_is_not_ambiguous
only_where_usable = _ARCH.test_loader_is_only_declared_where_it_can_be_used


# ---------------------------------------------------------------------------
# 一组形状正确的最小处理器（必须定义在模块级：能力字段靠类型标注收集，
# 而本仓所有模块带 `from __future__ import annotations`，
# 在局部作用域定义会让前向引用解析不到，字段收集抛 TypeError）
# ---------------------------------------------------------------------------


def _do_write(tx, ctx, scope, code: f_str("编号", required=True)) -> HandlerResult:
    return HandlerResult(data={"code": code}, resource_id=1)


def _do_action(tx, ctx, scope) -> HandlerResult:
    return HandlerResult(data={}, resource_id=None)


def _do_read(tx, ctx, scope) -> HandlerResult:
    return HandlerResult(data={"items": []})


def _load_row(tx, *, scope, record_id: int):
    return {"id": record_id}


def _load_other(tx, *, scope, record_id: int):
    return {"id": record_id}


def _read_capability(name: str, handler, **extra) -> Capability:
    return Capability(
        name=name,
        title="读探针",
        domain="probe",
        handler=handler,
        method=HttpMethod.GET,
        path=f"/api/v1/{name}",
        kind="read",
        risk=Risk.READ,
        **extra,
    )


def _write_capability(name: str, handler, **extra) -> Capability:
    return Capability(
        name=name,
        title="写探针",
        domain="probe",
        handler=handler,
        service_factory=None,
        method=HttpMethod.POST,
        path=f"/api/v1/{name}",
        kind="create",
        risk=Risk.NORMAL,
        **extra,
    )


@pytest.fixture
def injected():
    """把临时能力注入真实 REGISTRY，测试后摘掉（并复位 handler 上的属性）。

    为什么必须复位 handler 属性：`Capability.__post_init__` 把 loader 挂在
    **函数对象**上，而函数对象是模块级的、跨测试存活。不复位的话，下一个测试
    会在"上一个测试留下的挂载"上做判断。
    """
    added: list[str] = []
    touched: list[tuple[object, str]] = []

    def add(item: Capability) -> None:
        REGISTRY.register(item)
        added.append(item.name)

    def track(handler: object, attribute: str) -> None:
        touched.append((handler, attribute))

    yield add, track

    for name in added:
        REGISTRY._capabilities.pop(name, None)
    for handler, attribute in touched:
        if hasattr(handler, attribute):
            delattr(handler, attribute)


def _failure_message(assertion, *args, **kwargs) -> str:
    with pytest.raises(AssertionError) as info:
        assertion(*args, **kwargs)
    return str(info.value)


# ---------------------------------------------------------------------------
# 反例：缺 loader 的写能力必须被第一条断言抓住
# ---------------------------------------------------------------------------


def test_unwired_write_capability_is_rejected(injected) -> None:
    add, _track = injected
    add(_write_capability("probe.unwired", _do_action))

    message = _failure_message(declares_loader)
    assert "probe.unwired" in message, message
    # 报错必须给出**可操作的修法**，而不是只说"不合法"
    assert "loader=" in message and "NO_LOADER" in message, message


def test_no_loader_marker_is_accepted(injected) -> None:
    add, _track = injected
    add(_write_capability("probe.marked", _do_action, loader=NO_LOADER))

    # 不该抛：这是"不产出可回读单据"的合法声明
    declares_loader()


def test_declared_loader_is_accepted(injected) -> None:
    add, track = injected
    track(_do_action, "__fpa_load_by_id__")
    add(_write_capability("probe.with_loader", _do_action, loader=_load_row))

    declares_loader()


# ---------------------------------------------------------------------------
# 反例：歧义的声明必须被第二条断言抓住
# ---------------------------------------------------------------------------


def test_manual_attach_conflicting_with_loader_is_rejected(injected) -> None:
    add, track = injected
    track(_do_action, "__fpa_load_by_id__")
    # 先注册一条带 loader 的，把 _load_row 挂上去；再手工改成 _load_other 模拟不一致
    add(_write_capability("probe.ambiguous_a", _do_action, loader=_load_row))
    _do_action.__fpa_load_by_id__ = _load_other  # type: ignore[attr-defined]

    message = _failure_message(not_ambiguous)
    assert "probe.ambiguous_a" in message, message
    assert "不一致" in message, message


def test_no_loader_plus_attached_loader_is_rejected(injected) -> None:
    add, track = injected
    track(_do_action, "__fpa_load_by_id__")
    add(_write_capability("probe.ambiguous_b", _do_action, loader=NO_LOADER))
    # 手工再挂一个回读函数 —— 与 NO_LOADER 互相否定
    _do_action.__fpa_load_by_id__ = _load_other  # type: ignore[attr-defined]

    message = _failure_message(not_ambiguous)
    assert "probe.ambiguous_b" in message, message
    assert "NO_LOADER" in message, message


# ---------------------------------------------------------------------------
# 反例：读能力声明 loader 必须被第三条断言抓住
# ---------------------------------------------------------------------------


def test_read_capability_declaring_loader_is_rejected(injected) -> None:
    add, track = injected
    track(_do_read, "__fpa_load_by_id__")
    add(_read_capability("probe.read_bad", _do_read, loader=_load_row))

    message = _failure_message(only_where_usable)
    assert "probe.read_bad" in message, message


# ---------------------------------------------------------------------------
# 正向：真实装配必须通过（回归保护）
# ---------------------------------------------------------------------------


def test_real_registry_passes_all_loader_contracts() -> None:
    """真实组合根下的全部能力都必须满足三条契约。

    这是**唯一**一条针对生产装配的正向断言 —— 上面那些反例都用注入的探针，
    它们证明"断言会失败"，这一条证明"真实数据不触发失败"。
    """
    bootstrap.load_all()
    declares_loader()
    not_ambiguous()
    only_where_usable()

    write_count = sum(1 for item in REGISTRY.all() if not item.is_read)
    assert write_count > 0, "一条写能力都没有 —— 加载路径失效了（空集不是通过）"
