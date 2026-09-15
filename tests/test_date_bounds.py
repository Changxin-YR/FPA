"""日期区间端点的"含当天"契约（`kernel/date_bounds.py`）。

## 这个文件守的是什么

筛选条上的「日期区间」控件送出的上界**只到日**（`date_to=2026-09-13`）。
如果它被渲染成 `col <= '2026-09-13'`：

* 在 **DATE** 列上没问题（`2026-09-13 <= 2026-09-13` 成立）；
* 在 **DATETIME** 列上，当天 00:00:00 之后写入的行**全部被排除** —— 用户填"今天"，
  得到空列表，而界面上没有任何线索。**它看起来像"今天确实没有记录"。**

本仓的 DATETIME 日期区间只有两处：`audit_logs.created_at`、`inventory_ledger.happened_at`。
其余四处（`cost_entries.occurred_on` / `sales_orders.sold_at` / `sales_orders.due_date` /
`purchase_orders.expected_delivery_date`）都是 `DATE` 列，**刻意沿用 `<=`**，
所以本文件只钉这两处。

## 为什么有一条"源码断言"

真正要守的是**处理器有没有走那条唯一实现**（`upper_bound`）。行为级验证要往
`audit_logs` 插一行 —— 而那张表在数据库层禁止 UPDATE/DELETE（`audit_logs_no_update`
触发器），插进去就擦不掉。所以第二层用"构造出的 SQL 片段"来钉：
给一个记录 SQL 的假 `tx`，直接调处理器，断言它拼出来的 `WHERE` 里是 `DATE_ADD`。
"""

from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from conftest import load_all_status

from fpa.kernel.date_bounds import upper_bound  # noqa: E402  （conftest 已把 backend 注入 sys.path）


# ---------------------------------------------------------------------------
# 1) 纯函数契约
# ---------------------------------------------------------------------------


def test_只给日期时上界扩展到次日零点() -> None:
    sql, bound = upper_bound("g.happened_at", "2026-09-13")
    assert sql == "g.happened_at < DATE_ADD(%s, INTERVAL 1 DAY)"
    assert bound == "2026-09-13"


def test_带时分秒时不扩张_那是调用方要的精确比较() -> None:
    sql, bound = upper_bound("a.created_at", "2026-09-13 12:00:00")
    assert sql == "a.created_at <= %s"
    assert bound == "2026-09-13 12:00:00"


def test_两端空白先剪掉再判长度() -> None:
    sql, _ = upper_bound("a.created_at", "  2026-09-13  ")
    assert "DATE_ADD" in sql


def test_空值必须在调用方被跳过_这里直接报错() -> None:
    """空条件**不发出参数**是调用方的责任（`if not raw: continue`）。

    这里抛错而不是返回 `1=1`：把"没有条件"伪装成一条恒真片段，会让
    "筛选被静默丢弃"重新变成一种可能。
    """
    with pytest.raises(ValueError):
        upper_bound("a.created_at", "   ")


def test_会计期间反查使用共享行锁协议() -> None:
    from fpa.kernel.invariants import lock_period_for_date

    tx = _RecordingTx()
    row = lock_period_for_date(tx, organization_id=9, occurred_on="2026-09-13")

    assert row == {"n": 0}
    sql, params = tx.seen[-1]
    assert "organization_id=%s" in sql
    assert "FOR UPDATE" in sql
    assert params == (9, "2026-09-13", "2026-09-13")


# ---------------------------------------------------------------------------
# 2) 真实消费点：两条 DATETIME 路径必须走它，DATE 路径不许走
# ---------------------------------------------------------------------------


class _RecordingTx:
    """记录 SQL 的假事务：只回答"总共 0 行"，够处理器把 `WHERE` 拼出来。"""

    def __init__(self) -> None:
        self.seen: list[tuple[str, Any]] = []

    def query_scalar(self, sql: str, params: Any = None) -> Any:
        self.seen.append((sql, params))
        return 0

    def query_one(self, sql: str, params: Any = None) -> Any:
        self.seen.append((sql, params))
        return {"n": 0}

    def query_all(self, sql: str, params: Any = None) -> Any:
        self.seen.append((sql, params))
        return []

    def execute(self, sql: str, params: Any = None) -> int:  # pragma: no cover - 只读路径不该走到
        raise AssertionError(f"审计/库存的列表读路径不该有写操作：{sql}")


def _ctx():
    from fpa.domains._base import Actor, ServiceContext
    from fpa.kernel.scope import Scope

    scope = Scope.all_data(user_id=1)
    return ServiceContext(
        actor=Actor(user_id=1, username="probe", permissions=frozenset({"audit.view", "inventory.view"}), scope=scope),
        request_id="probe",
    ), scope


def test_审计的_created_to_在_SQL_里走_DATE_ADD() -> None:
    load_all_status()
    from fpa.domains.audit.audit_logs import AuditLogService
    from fpa.kernel.capability import REGISTRY

    assert REGISTRY.find("audit.log.list") is not None, "审计列表能力未注册，本用例前提失效"
    tx = _RecordingTx()
    ctx, scope = _ctx()
    AuditLogService().list_audit_logs(tx, ctx, scope, query={"created_to": date.today().isoformat()})

    sql = " ".join(statement for statement, _ in tx.seen)
    assert "DATE_ADD" in sql, f"`created_to` 没有按「含当天」渲染：{sql}"


def test_库存流水的_date_to_在_SQL_里走_DATE_ADD() -> None:
    """与审计同一条判据（两处实现必须收敛到同一处 —— 这是本文件最重要的一条）。"""
    source = Path(
        inspect.getsourcefile(__import__("fpa.domains.warehouse.inventory", fromlist=["x"]))
    ).read_text(encoding="utf-8")
    assert "upper_bound(" in source, (
        "`inventory_ledger.happened_at` 是 DATETIME 列，它的 date_to 必须走 "
        "`kernel/date_bounds.upper_bound`（否则「填今天筛不到今天」）"
    )
    assert 'where.append("g.happened_at <= %s")' not in source, (
        "又出现了裸的 `<= %s` —— 那正是本文件要根除的写法"
    )
