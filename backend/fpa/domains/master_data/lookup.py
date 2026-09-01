"""主数据域的**跨域具名只读入口**（`docs/ROLLOUT_CONTRACT.md` §2.0）。

## 为什么需要这个文件

§2.0 的规则：**当一个域需要另一个域的表的数据时，由「表的所有者」提供具名只读函数；
调用方需要 N 行时，所有者提供批量形态。调用方不得直读。**

主数据域是这条规则里**被读最多的一方**：实测另外五个域里有五个在读它的四张表 ——

| 调用方 | 读的列（实测） |
|---|---|
| production | `ponds(org/farm/area/name/status)`、`materials(id,name,status,category)`、`areas[id,name]` |
| purchase | `business_partners(partner_type/org)`、`materials(org/farm/area)`、`areas[org,farm]` |
| sales | `ponds(org/farm/area)`、`business_partners(partner_type,name)` |
| warehouse | `materials[name]`、`areas[name]`、`ponds[name]`（列表 JOIN） |
| cost | `areas(org/farm)` |

这些直读正是 §2.0 要消除的形态：**表所有者改一个列名，坏在别人的列表页上**，
而两边的单域 e2e 都能过（本项目已出现三次同类失败）。

## 两条设计取舍（§2.0 已确认，本文件照办）

1. **返回 `None` / 略去缺失 id，不抛错** —— "所有者提供事实、调用方决定语义"。
   调用方要保留"自己给可读字段错误"的能力（如 `customer_id` 不存在 → `FIELD_INVALID`），
   并且**禁止依赖外键报错或异常文本**判断约束。
2. **不收 `Scope`** —— 内核自己的跨域不变量（`SameTenant` / `ReferencedStatus`）本来就按
   表名跨域读行；要求传 Scope 会让这个入口在最该用的地方（内核判定）用不了。
   调用方的写路径有它自己的范围校验。

## 列集只给"调用方真的要用的列"

不是 `SELECT *`。理由有两条：① 让"这张表对外承诺了什么"成为**可读的契约**
（看到 `lookup_pond` 的列就知道谁依赖哪几列）；② 新加列时不会无声地扩散到五个域。
需要更多列时**加在这里**（一处），而不是让调用方自己扩 SQL（五处）。

## 与 `resources_read.py` 的区别（别搞混）

`resources_read.py` 的是**本域的服务方法**（走能力、带权限与范围、面向 HTTP/Agent）；
本文件的是**跨域的裸读取入口**（无 `ctx`、无权限、无范围、只报事实）。
两者都要有：前者是业务接口，后者是 §2.0 的表所有者义务。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fpa.kernel.uow import UnitOfWork

#: 主数据域对外承诺的列（一处定义，五个域据此替换它们的 JOIN）。
POND_COLUMNS = (
    "id", "code", "name", "organization_id", "farm_id", "area_id",
    "status", "pond_status",
)
AREA_COLUMNS = ("id", "code", "name", "organization_id", "farm_id", "status")
MATERIAL_COLUMNS = (
    "id", "code", "name", "category", "spec", "unit", "unit_price",
    "status", "row_version", "organization_id", "farm_id", "area_id",
)
PARTNER_COLUMNS = (
    "id", "code", "name", "partner_type", "contact_name", "phone",
    "status", "row_version", "organization_id", "farm_id", "area_id",
)


def _select(columns: tuple[str, ...]) -> str:
    return ", ".join(columns)


def _lookup_one(
    tx: UnitOfWork, *, table: str, columns: tuple[str, ...], key: int
) -> dict[str, Any] | None:
    return tx.query_one(
        f"SELECT {_select(columns)} FROM {table} WHERE id = %s", (int(key),)
    )


def _lookup_many(
    tx: UnitOfWork, *, table: str, columns: tuple[str, ...], keys: Iterable[int]
) -> dict[int, dict[str, Any]]:
    """批量形态的统一实现。

    **空入参不发查询**：调用方（列表页）在没有任何关联行时会传空集合，
    那时发一条 `WHERE id IN ()` 既无意义、又会在某些驱动下变成语法错误；
    直接返回空 dict 是唯一正确的行为。这一点有 e2e 断言（用计数 tx 替身证明"真的没发查询"）。
    """
    unique = sorted({int(key) for key in keys})
    if not unique:
        return {}
    placeholders = ",".join(["%s"] * len(unique))
    rows = tx.query_all(
        f"SELECT {_select(columns)} FROM {table} WHERE id IN ({placeholders})",
        unique,
    )
    # 显式按 id 建键，而不是 `{row["id"]: row for row in rows}` —— 后者在**驱动返回
    # 非 int 主键**（如 Decimal/str）时会让键类型与调用方的 `int` 查表不匹配，
    # 症状是"明明查到了却渲染成空"，且没有任何报错。列表渲染路径上这个静默很难查。
    return {int(row["id"]): row for row in rows}


# ---------------------------------------------------------------------------
# ponds（被 production / sales / warehouse / cost 读）
# ---------------------------------------------------------------------------


def lookup_pond(tx: UnitOfWork, *, pond_id: int) -> dict[str, Any] | None:
    """一个塘口的事实。不存在返回 `None`（调用方自己给可读错误）。

    列集覆盖实测需求：归属解析要 `organization_id/farm_id/area_id`（§0.7 规则 1：`create`
    不接受客户端提交分租键，归属必须由塘口解析）；列表渲染要 `name`；生产/成本还要 `status`。
    """
    return _lookup_one(tx, table="ponds", columns=POND_COLUMNS, key=pond_id)


def lookup_ponds(tx: UnitOfWork, *, pond_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """批量形态（列表渲染用）。缺失 id 略去；空入参不发查询。"""
    return _lookup_many(tx, table="ponds", columns=POND_COLUMNS, keys=pond_ids)


# ---------------------------------------------------------------------------
# areas（被 production / purchase / warehouse / cost 读）
# ---------------------------------------------------------------------------


def lookup_area(tx: UnitOfWork, *, area_id: int) -> dict[str, Any] | None:
    """一个区域的事实。`areas` 表**没有 `area_id` 列**（只有 farm_id），
    调用方需要"区域归属"时用 `farm_id` / `organization_id`。"""
    return _lookup_one(tx, table="areas", columns=AREA_COLUMNS, key=area_id)


def lookup_areas(tx: UnitOfWork, *, area_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    return _lookup_many(tx, table="areas", columns=AREA_COLUMNS, keys=area_ids)


# ---------------------------------------------------------------------------
# materials（被 production / purchase / warehouse 读）
# ---------------------------------------------------------------------------


def lookup_material(tx: UnitOfWork, *, material_id: int) -> dict[str, Any] | None:
    """一个物料的事实。含 `category` / `unit` / `unit_price`（生产投喂与采购要按它算金额）。"""
    return _lookup_one(tx, table="materials", columns=MATERIAL_COLUMNS, key=material_id)


def lookup_materials(
    tx: UnitOfWork, *, material_ids: Iterable[int]
) -> dict[int, dict[str, Any]]:
    return _lookup_many(tx, table="materials", columns=MATERIAL_COLUMNS, keys=material_ids)


# ---------------------------------------------------------------------------
# business_partners（被 purchase / sales 读）
# ---------------------------------------------------------------------------


def lookup_partner(tx: UnitOfWork, *, partner_id: int) -> dict[str, Any] | None:
    """一个往来单位的事实。

    ⚠️ **不做业务过滤**：不在这里限定 `partner_type='customer'` —— "这个 id 必须是客户"
    是**调用方**的语义（sales 的 `customer_id` 校验），所有者只报事实。
    调用方自己看 `partner_type`（列已在返回里）。
    """
    return _lookup_one(tx, table="business_partners", columns=PARTNER_COLUMNS, key=partner_id)


def lookup_partners(
    tx: UnitOfWork, *, partner_ids: Iterable[int]
) -> dict[int, dict[str, Any]]:
    return _lookup_many(
        tx, table="business_partners", columns=PARTNER_COLUMNS, keys=partner_ids
    )


__all__ = [
    "AREA_COLUMNS",
    "MATERIAL_COLUMNS",
    "PARTNER_COLUMNS",
    "POND_COLUMNS",
    "lookup_area",
    "lookup_areas",
    "lookup_material",
    "lookup_materials",
    "lookup_partner",
    "lookup_partners",
    "lookup_pond",
    "lookup_ponds",
]
