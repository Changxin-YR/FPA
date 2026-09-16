"""`Resource.table` 必须显式声明——曾经有个会猜错的兜底。

背景（评审结论 ③）：兜底公式 `<module>_<name>s` 对 `cost` + `cost_entry` 会给出
`cost_cost_entrys`（多前缀 + 复数错），而且**猜错不报错**——只有真正用到表名的规则
（`OptimisticLock` / `UniqueCode` / `StateTransition`）才会在距离声明处很远的地方撞上
"表不存在"。口径：**兜底不是契约，就不该有任何一处依赖它**。

本文件把这条约束钉住：
  1. 省略 `table=` 必须**构造期报错**（而不是算一个名字继续跑）；
  2. 注册表里每一条资源都必须有非空 `table`（当前 16 处 Resource 全部显式声明）。
"""

from __future__ import annotations

import pytest

import yuxin.bootstrap as bootstrap
from yuxin.kernel.workflow import RESOURCES, Resource


def test_resource_without_explicit_table_raises() -> None:
    with pytest.raises(ValueError) as excinfo:
        Resource(name="x", title="X", module="cost", list_path="/api/v1/x")
    message = str(excinfo.value)
    assert "未声明 table" in message
    assert "兜底" in message, "错误信息必须说清兜底已被移除，否则后来者会以为漏了默认值"


def test_explicit_table_is_kept_verbatim() -> None:
    spec = Resource(
        name="x", title="X", module="cost", list_path="/api/v1/x", table="cost_entries"
    )
    assert spec.table == "cost_entries", "显式表名不得被任何公式改写"


def test_every_registered_resource_declares_its_table(load_all_status) -> None:
    """依赖组合根：域装载失败时由 conftest 的 load_all_status 跳过（只剩冒烟那条红），
    而不是让这条断言各报一次"注册表是空的"。"""
    bootstrap.load_all()
    empty = [r.name for r in RESOURCES.all() if not r.table]
    assert empty == [], (
        f"这些资源没有解析出表名：{empty}——"
        "`OptimisticLock` / `UniqueCode` / `StateTransition` 都会用到它"
    )
    # 只查**兜底公式的独有产物**：`…ss`（`cost_entrys` 再加 s）。
    # 刻意不查"表名以 module 前缀开头"——`purchase_order` -> `purchase_orders` 是同名的
    # 合法写法（实测 5 个资源因此被我第一版的启发式误报），兜底早已删除，不必再猜。
    suspicious = [r.name for r in RESOURCES.all() if r.table.endswith("ss")]
    assert suspicious == [], f"这些资源的表名像是兜底公式的产物（…ss）：{suspicious}"
