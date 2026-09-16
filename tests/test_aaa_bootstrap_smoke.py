"""独立的"装载冒烟"测试：**让域导入失败表现为一条清晰失败，而不是几十条连锁**。

## 为什么需要它（负责人 采纳 复核人 的建议）
组合根是"按目录自动发现每个域的 `capabilities.py` 并 import"。所以**任何一个域语法错 /
import 失败**，都会让 `yuxin.bootstrap.load_all()` 整体装不起来，于是所有依赖组合根的
断言（架构约束、注册表对账、读回契约、行内动作……）一次性全红。实测过一次：
**1 条真故障膨胀成 22 failed / 94 passed** —— 真问题被噪声淹没。

## 做法
* 本文件按字母序**第一个**收集执行（`test_aaa_` 前缀），先调用一次 `load_all()`，
  把结果（成功或异常）缓存进 `sys.modules` 上的一个共享对象；
* 导入失败时，**本文件的用例失败**（一条，带完整堆栈与域名）；
* 其余依赖组合根的用例在 fixture 里检查这个共享状态，失败则 **skip 并注明原因**
  （"域装载失败：<异常>"），于是不再产生连锁失败。

约定：需要真实注册表的测试请依赖 `load_all_status` fixture（见 `conftest.py`）。
"""

from __future__ import annotations

import sys
from typing import Any

_BOOTSTRAP_STATE = "yuxin_test_bootstrap_state"


class BootstrapState:
    """组合根装载结果（成功 / 失败的异常对象）。"""

    def __init__(self) -> None:
        self.attempted = False
        self.error: BaseException | None = None
        self.capability_count = 0

    @property
    def ok(self) -> bool:
        return self.attempted and self.error is None

    def summary(self) -> str:
        if not self.attempted:
            return "未尝试装载"
        if self.error is None:
            return f"装载成功（{self.capability_count} 条能力）"
        return f"装载失败：{type(self.error).__name__}: {self.error}"


def bootstrap_state() -> BootstrapState:
    """取得（必要时创建）进程内的装载状态对象。"""
    existing = getattr(sys, _BOOTSTRAP_STATE, None)
    if existing is None:
        existing = BootstrapState()
        setattr(sys, _BOOTSTRAP_STATE, existing)
    return existing


def attempt_load() -> BootstrapState:
    """尝试装载全部域；异常**不抛出**，而是记进状态里供冒烟测试断言。"""
    state = bootstrap_state()
    if state.attempted:
        return state
    import yuxin.bootstrap as bootstrap
    from yuxin.kernel.capability import REGISTRY

    state.attempted = True
    try:
        bootstrap.load_all()
        state.capability_count = len(REGISTRY.all())
    except BaseException as error:  # noqa: BLE001 —— 故意接住一切，见文件头说明
        state.error = error
    return state


def reset_load_state() -> None:
    """清掉状态（供需要重新装载的用例使用）。"""
    if hasattr(sys, _BOOTSTRAP_STATE):
        delattr(sys, _BOOTSTRAP_STATE)


def test_all_domains_import() -> None:
    """组合根必须能把每个域的 `capabilities.py` 装载起来。

    失败时这条测试给出**唯一的真失败**（含原始异常与域名），其余依赖注册表的用例会
    skip —— 门禁的信号因此保住。
    """
    state = attempt_load()
    if state.error is not None:
        raise AssertionError(
            "组合根装载失败：某个域导不进来（这会让所有依赖注册表的断言连锁失败，"
            "所以单独在这一条里报）。原始异常：\n"
            f"    {type(state.error).__name__}: {state.error}"
        ) from state.error
    assert state.capability_count > 0, "装载成功但一条能力都没注册（域声明底部漏了 _register_all()？）"


def test_bootstrap_load_is_idempotent() -> None:
    """重复装载必须安全（组合根被多个入口调用：web / agent / 自检工具）。"""
    import yuxin.bootstrap as bootstrap
    from yuxin.kernel.capability import REGISTRY

    state = attempt_load()
    if state.error is not None:
        # 装载本身就失败了 —— 这条跟着红只会重复报同一个原因，skip 掉更清楚。
        import pytest

        pytest.skip("组合根装载失败，跳过幂等性检查（见 test_all_domains_import）")

    before = len(REGISTRY.all())
    bootstrap.load_all()
    assert len(REGISTRY.all()) == before, "重复装载把能力注册了第二遍"
