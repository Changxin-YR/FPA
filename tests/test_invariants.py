"""内核不变量的单元测试：每条规则覆盖"通过"与"拒绝"两条路径。

为什么这些测试能脱离 MySQL 跑：不变量被设计成"声明 + 一个 `check()`"，只依赖
`UnitOfWork` 的查询接口（`tools/kernel_smoke.py` 早就证明过这一点）。早期版本把
同样的规则写在紧贴 SQL 的仓储里（`早期版本 production_store.py:273-280`、`早期版本 warehouse_ledger_store.py:181-202`），于是每条规则都只能靠真实 MySQL 验证 ——
这就是"合规规则跨域不一致"能长期存活的土壤。

下面用一个 `FakeUnitOfWork` 替身，按 **SQL 文本特征**路由到预设响应。路由而不是
顺序返回，是为了让"不变量查了哪张表、用没用 FOR UPDATE、参数怎么绑"成为断言对象：
规则写错了表名，测试会失败而不是静默通过。
"""

from __future__ import annotations

import inspect

from decimal import Decimal
from typing import Any

import pytest

from yuxin.kernel.errors import DomainError, ErrorCode
from yuxin.kernel.fields import f_str
from yuxin.kernel.invariants import (
    AmountWithin,
    AtLeastOneOf,
    AtMostOnePending,
    CumulativeWithin,
    HarvestQuantityMatch,
    NoOverlappingSource,
    OptimisticLock,
    ReferencedStatus,
    SameTenant,
    StateTransition,
    StatusAllowsEdit,
    UniqueCode,
    ZeroBalance,
)
from yuxin.kernel.scope import Scope
from yuxin.kernel.workflow import Resource, State, Tone, Transition, Workflow

# ---------------------------------------------------------------------------
# 替身与夹具
# ---------------------------------------------------------------------------


class FakeUnitOfWork:
    """按 SQL 文本特征路由的 UnitOfWork 替身。

    ``matching`` 是 ``(片段, 响应)`` 的有序列表，片段必须**同时**给出表名与列名，
    这样"查错了表"会命中不了任何响应（返回 None）而不是碰巧命中。
    """

    def __init__(self, *matching: tuple[str, Any], default: Any = None) -> None:
        self.routes = list(matching)
        self.default = default
        self.queries: list[tuple[str, list[Any]]] = []

    def query_one(self, sql: str, params: Any = None) -> Any:
        self.queries.append((sql, list(params or ())))
        for fragment, response in self.routes:
            if all(part in sql for part in fragment.split("|")):
                return response
        return self.default

    # 便于断言"用了行锁"
    @property
    def last_sql(self) -> str:
        return self.queries[-1][0] if self.queries else ""


def scope_all() -> Scope:
    return Scope.all_data(user_id=7)


def check(invariant: Any, *, tx: Any = None, payload: dict[str, Any] | None = None,
          before: dict[str, Any] | None = None, scope: Scope | None = None) -> Any:
    return invariant.check(
        tx=tx if tx is not None else FakeUnitOfWork(),
        scope=scope if scope is not None else scope_all(),
        actor_id=7,
        payload=payload or {},
        before=before,
    )


def rejects(invariant: Any, *, code: str | None = None, **kwargs: Any) -> DomainError:
    """断言被拒，并返回异常（供进一步断言 code / data）。"""
    with pytest.raises(DomainError) as caught:
        check(invariant, **kwargs)
    if code is not None:
        assert str(caught.value.code) == code, caught.value
    return caught.value


#: 一个最小的状态机，用来喂 `StateTransition`。表名显式声明 —— `Resource.__post_init__`
#: 的 `<module>_<name>s` 兜底只对真实域有意义。
def _pond_workflow() -> Workflow:
    return Workflow(
        resource="ut_pond",
        states=(
            State("draft", "草稿", Tone.NEUTRAL),
            State("submitted", "待核验", Tone.INFO),
            State("verified", "已核验", Tone.SUCCESS),
        ),
        transitions=(
            Transition("draft", "submitted", "ut_pond.submit"),
            Transition("submitted", "verified", "ut_pond.verify"),
        ),
    )


@pytest.fixture(scope="module", autouse=True)
def _register_test_resource() -> None:
    """注册一个测试专用资源（不碰 master_data 的任何声明）。"""
    from yuxin.kernel.workflow import RESOURCES

    if RESOURCES.find("ut_pond") is None:
        RESOURCES.register(
            Resource(
                name="ut_pond",
                title="测试塘口",
                module="ut",
                list_path="/api/v1/ut-ponds",
                workflow=_pond_workflow(),
                table="ut_ponds",
            )
        )


# ---------------------------------------------------------------------------
# StateTransition（#14 / #20）
# ---------------------------------------------------------------------------


def test_state_transition_legal_transition_passes() -> None:
    check(
        StateTransition(machine="ut_pond"),
        payload={"status": "submitted"},
        before={"status": "draft"},
    )


def test_state_transition_illegal_transition_rejected_with_allowed_list() -> None:
    error = rejects(
        StateTransition(machine="ut_pond", label="塘口"),
        code="CONFLICT",
        payload={"status": "verified"},
        before={"status": "draft"},
    )
    assert error.data is not None
    assert error.data["rule"] == "STATE_TRANSITION"
    assert error.data["allowed"] == ["submitted"]
    assert "草稿" in error.message


def test_state_transition_is_skipped_without_status_in_payload() -> None:
    """没提交状态字段的能力（例如只改名称的 update）不该被这条规则拦住。"""
    check(StateTransition(machine="ut_pond"), payload={"name": "一号塘"}, before={"status": "verified"})


def test_state_transition_is_skipped_for_new_records() -> None:
    """新建时 `before` 为 None：没有"从哪来"，无从校验。"""
    check(StateTransition(machine="ut_pond"), payload={"status": "verified"}, before=None)


def test_state_transition_same_state_is_not_a_transition() -> None:
    check(StateTransition(machine="ut_pond"), payload={"status": "draft"}, before={"status": "draft"})


def test_state_transition_star_uses_current_resource() -> None:
    check(
        StateTransition(machine="*"),
        payload={"status": "submitted", "_resource": "ut_pond"},
        before={"status": "draft"},
    )


def test_state_transition_unknown_machine_is_internal_error() -> None:
    error = rejects(
        StateTransition(machine="ut_missing"),
        code="INTERNAL_ERROR",
        payload={"status": "submitted"},
        before={"status": "draft"},
    )
    assert error.data is not None and "available" in error.data


# ---------------------------------------------------------------------------
# StatusAllowsEdit（#12）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["draft", "submitted"])
def test_status_allows_edit_passes_for_editable_states(status: str) -> None:
    check(StatusAllowsEdit(), payload={}, before={"status": status})


def test_status_allows_edit_rejects_verified_record() -> None:
    error = rejects(
        StatusAllowsEdit(label="塘口"),
        code="CONFLICT",
        payload={},
        before={"status": "verified"},
    )
    assert error.data is not None and error.data["rule"] == "RECORD_READ_ONLY"


def test_status_allows_edit_passes_without_before_snapshot() -> None:
    check(StatusAllowsEdit(), payload={}, before=None)


# ---------------------------------------------------------------------------
# OptimisticLock（#13）
# ---------------------------------------------------------------------------


def test_optimistic_lock_resolves_the_table_from_the_resource_name() -> None:
    """没有 `_resource_table` 时，用 `_resource` 去 `RESOURCES` 查表名。"""
    from yuxin.kernel.invariants import NoNegativeStock as _  # noqa: F401

    tx = FakeUnitOfWork(("FROM ut_ponds", {"row_version": 4}))
    error = rejects(
        OptimisticLock(),
        code="VERSION_CONFLICT",
        tx=tx,
        payload={"_resource": "ut_pond", "_resource_id": 3, "expected_version": 3},
        before=None,
    )
    assert error.data is not None and error.data["current_version"] == 4
    assert "ut_ponds" in tx.last_sql


# ---------------------------------------------------------------------------
# NoNegativeStock（#1 / #2）—— 判定"写入后"的余额
# ---------------------------------------------------------------------------


def _no_negative_stock() -> Any:
    from yuxin.kernel.invariants import NoNegativeStock

    return NoNegativeStock(
        table="inventory_ledger",
        columns=("quantity_delta",),
        group_by=("warehouse_id", "material_id", "inventory_lot_id"),
    )


def test_no_negative_stock_rejects_negative_post_write_balance() -> None:
    tx = FakeUnitOfWork(("FROM inventory_ledger", {"quantity_delta": -20}))
    error = rejects(
        _no_negative_stock(),
        code="CONFLICT",
        tx=tx,
        payload={"_ledger_lines": [{"warehouse_id": 1, "material_id": 2, "inventory_lot_id": 3}]},
    )
    assert error.data is not None and error.data["balance"] == "-20"


def test_no_negative_stock_passes_non_negative_balance() -> None:
    tx = FakeUnitOfWork(("FROM inventory_ledger", {"quantity_delta": 30}))
    check(
        _no_negative_stock(),
        tx=tx,
        payload={"_ledger_lines": [{"warehouse_id": 1, "material_id": 2, "inventory_lot_id": 3}]},
    )
    assert "FOR UPDATE" in tx.last_sql


def test_no_negative_stock_checks_every_declared_column() -> None:
    """存塘要同时保证数量与重量不为负：数量为 0、重量为 −5 也必须被拒。"""
    from yuxin.kernel.invariants import NoNegativeStock

    tx = FakeUnitOfWork(
        ("FROM batch_stock_records", {"quantity_delta": 0, "weight_delta_kg": -5})
    )
    error = rejects(
        NoNegativeStock(
            table="batch_stock_records",
            columns=("quantity_delta", "weight_delta_kg"),
            group_by=("batch_id", "pond_id"),
            label="存塘",
        ),
        code="CONFLICT",
        tx=tx,
        payload={"_ledger_lines": [{"batch_id": 1, "pond_id": 2}]},
    )
    assert error.data is not None and error.data["column"] == "weight_delta_kg"


def test_no_negative_stock_refuses_to_silently_skip_without_context() -> None:
    """**这是本条裁决的核心**：没有账本行上下文时报错，而不是放行。

    旧形态的 `if not lines: return` 意味着"服务忘了回传 → 校验通过 → 负库存照写"，
    与 `fields=` 忘传是同一个失败形态（DEVELOPMENT §4）。
    """
    rejects(_no_negative_stock(), code="INTERNAL_ERROR", payload={})


def test_no_negative_stock_refuses_lines_without_group_key() -> None:
    rejects(
        _no_negative_stock(),
        code="INTERNAL_ERROR",
        payload={"_ledger_lines": [{"material_id": 2}]},
    )


def test_no_negative_stock_accepts_the_legacy_single_column_alias() -> None:
    from yuxin.kernel.invariants import NoNegativeStock

    invariant = NoNegativeStock(table="inventory_ledger", quantity_column="quantity_delta")
    assert invariant.columns == ("quantity_delta",)


# ---------------------------------------------------------------------------
# OptimisticLock（#13）
# ---------------------------------------------------------------------------


def test_optimistic_lock_passes_when_versions_match() -> None:
    check(
        OptimisticLock(),
        tx=FakeUnitOfWork(),
        payload={"expected_version": 3},
        before={"row_version": 3},
    )


def test_optimistic_lock_rejects_stale_version() -> None:
    error = rejects(
        OptimisticLock(label="该塘口"),
        code="VERSION_CONFLICT",
        payload={"expected_version": 2},
        before={"row_version": 3},
    )
    assert error.data is not None and error.data["current_version"] == 3


def test_optimistic_lock_falls_back_to_a_query_when_before_is_missing() -> None:
    tx = FakeUnitOfWork(("FROM ut_ponds|row_version", {"row_version": 5}))
    error = rejects(
        OptimisticLock(),
        code="VERSION_CONFLICT",
        tx=tx,
        payload={"_resource_table": "ut_ponds", "_resource_id": 11, "expected_version": 4},
        before=None,
    )
    assert error.data is not None and error.data["current_version"] == 5
    assert "ut_ponds" in tx.last_sql


def test_optimistic_lock_is_skipped_without_expected_version() -> None:
    check(OptimisticLock(), payload={}, before={"row_version": 9})


# ---------------------------------------------------------------------------
# ReferencedStatus（#3 未审批不得收货 / #18 物料须 verified）
# ---------------------------------------------------------------------------


def test_referenced_status_passes_for_allowed_status() -> None:
    tx = FakeUnitOfWork(
        ("FROM purchase_orders", {"id": 3, "status": "approved", "organization_id": 1})
    )
    check(
        ReferencedStatus(
            field="purchase_order_id",
            table="purchase_orders",
            statuses=("approved", "partially_received"),
            label="采购单",
        ),
        tx=tx,
        payload={"purchase_order_id": 3},
    )


def test_referenced_status_rejects_unapproved_document() -> None:
    tx = FakeUnitOfWork(("FROM purchase_orders", {"id": 3, "status": "draft"}))
    error = rejects(
        ReferencedStatus(
            field="purchase_order_id",
            table="purchase_orders",
            statuses=("approved", "partially_received"),
            label="采购单",
        ),
        code="CONFLICT",
        tx=tx,
        payload={"purchase_order_id": 3},
    )
    assert error.data is not None and error.data["allowed"] == ["approved", "partially_received"]


def test_referenced_status_rejects_missing_document() -> None:
    rejects(
        ReferencedStatus(field="material_id", table="materials", statuses=("verified",), label="物料"),
        code="FIELD_INVALID",
        tx=FakeUnitOfWork(),
        payload={"material_id": 99},
    )


def test_referenced_status_is_skipped_when_reference_absent() -> None:
    check(
        ReferencedStatus(field="purchase_order_id", table="purchase_orders", statuses=("approved",)),
        tx=FakeUnitOfWork(),
        payload={},
    )


def test_referenced_status_required_refuses_absent_reference() -> None:
    rejects(
        ReferencedStatus(
            field="material_id", table="materials", statuses=("verified",),
            label="物料", required=True,
        ),
        code="VALIDATION_ERROR",
        payload={},
    )


def test_referenced_status_rejects_out_of_scope_row() -> None:
    tx = FakeUnitOfWork(("FROM materials", {"id": 5, "status": "verified", "area_id": 999}))
    scope = Scope.from_rows([{"scope_type": "area", "area_id": 2}], user_id=7)
    rejects(
        ReferencedStatus(field="material_id", table="materials", statuses=("verified",), label="物料"),
        code="DATA_SCOPE_DENIED",
        tx=tx,
        payload={"material_id": 5},
        scope=scope,
    )


# ---------------------------------------------------------------------------
# SameTenant（#18）
# ---------------------------------------------------------------------------


def test_same_tenant_passes_when_all_references_share_scope() -> None:
    tx = FakeUnitOfWork(
        ("FROM materials", {"id": 2, "organization_id": 1, "farm_id": 4, "area_id": 9}),
        ("FROM warehouses", {"id": 3, "organization_id": 1, "farm_id": 4, "area_id": 9}),
    )
    check(
        SameTenant(
            fields=("warehouse_id", "material_id"),
            tables={"warehouse_id": "warehouses", "material_id": "materials"},
        ),
        tx=tx,
        payload={"warehouse_id": 3, "material_id": 2},
    )


def test_same_tenant_rejects_cross_farm_reference() -> None:
    tx = FakeUnitOfWork(
        ("FROM warehouses", {"id": 3, "organization_id": 1, "farm_id": 4}),
        ("FROM materials", {"id": 2, "organization_id": 1, "farm_id": 8}),
    )
    error = rejects(
        SameTenant(
            fields=("warehouse_id", "material_id"),
            tables={"warehouse_id": "warehouses", "material_id": "materials"},
            labels=("收货仓", "物料"),
        ),
        code="CONFLICT",
        tx=tx,
        payload={"warehouse_id": 3, "material_id": 2},
    )
    assert error.data is not None and error.data["conflict"] == "farm_id"
    assert "物料" in error.message


def test_same_tenant_skips_fields_without_declared_table() -> None:
    """未声明表的引用字段不归这条规则管（存在性由服务或外键负责）。"""
    check(
        SameTenant(fields=("pond_id",), tables={}),
        tx=FakeUnitOfWork(),
        payload={"pond_id": 1},
    )


# ---------------------------------------------------------------------------
# UniqueCode（#19）
# ---------------------------------------------------------------------------


def test_unique_code_passes_when_no_row_found() -> None:
    tx = FakeUnitOfWork()
    check(
        UniqueCode(fields=("code",), scope=("organization_id",), table="materials", labels=("物料编码",)),
        tx=tx,
        payload={"code": "M-001", "organization_id": 1},
    )
    assert "FROM materials" in tx.last_sql
    assert tx.queries[-1][1] == [1, "M-001"]


def test_unique_code_rejects_duplicate_with_readable_conflict() -> None:
    tx = FakeUnitOfWork(("FROM materials", {"id": 42}))
    error = rejects(
        UniqueCode(fields=("code",), scope=("organization_id",), table="materials", labels=("物料编码",)),
        code="CONFLICT",
        tx=tx,
        payload={"code": "M-001", "organization_id": 1},
    )
    assert error.data is not None and error.data["existing_id"] == 42
    assert "物料编码" in error.message


def test_unique_code_ignores_the_row_being_updated() -> None:
    tx = FakeUnitOfWork(("FROM materials", {"id": 42}))
    check(
        UniqueCode(fields=("code",), scope=("organization_id",), table="materials"),
        tx=tx,
        payload={"code": "M-001", "organization_id": 1},
        before={"id": 42, "code": "M-001", "organization_id": 1},
    )


def test_unique_code_refuses_to_downgrade_to_a_global_check() -> None:
    """新建时分租键由服务端补全；没补全就拒绝，不允许静默放宽唯一性。"""
    rejects(
        UniqueCode(fields=("code",), scope=("organization_id",), table="materials"),
        code="INTERNAL_ERROR",
        payload={"code": "M-001"},
    )


def test_unique_code_without_table_is_internal_error() -> None:
    rejects(
        UniqueCode(fields=("code",), scope=()),
        code="INTERNAL_ERROR",
        payload={"code": "M-001"},
    )


# ---------------------------------------------------------------------------
# CumulativeWithin（#9 / #10）
#
# 判定式（**两条路径同一个式子**）：
#
#     counted   = SUM(count_field) WHERE 目标维度=? AND status IN statuses
#                                      AND 子行主键 <> 本次记录主键（执行器注入的
#                                          `_invariant_exclude_id`，自排除）
#     requested = 本次数量（**不做单位换算**）
#     projected = counted + requested
#
# `new_row_counted` 是**必填**参数（不给默认值，忘选 → 构造期 ValueError），它声明
# "库里那一行是否已被计入 `counted`"：
#   * True  —— 核验类（`delivery.verify` / `receipt.verify`）：服务已把本行置 verified，
#              它在库里 → 自排除真的把它摘掉；
#   * False —— 登记类（`delivery.create` / `receipt.create`）：本行还是 draft、不在
#              `counted` 里 → 自排除是空操作。
# 两种情况 `projected` 的算式相同 —— 这就是"两条路径统一"的落点。
#
# 下面替身按"库里包含本次行的合计"（`counted_with_self`）建模，与真库读数一致。
# ---------------------------------------------------------------------------


class _CumulativeTx:
    """按 SQL 是否带 `id <> %s` 给出不同合计的替身（模拟真库语义）。"""

    def __init__(self, *, counted_with_self: object, self_quantity: object = 0,
                 target_quantity: object = 100) -> None:
        self.counted_with_self = counted_with_self
        self.self_quantity = self_quantity
        self.target_quantity = target_quantity
        self.queries: list[tuple[str, list]] = []

    def query_one(self, sql: str, params=None):
        self.queries.append((sql, list(params or ())))
        if "FROM sales_orders" in sql:
            if self.target_quantity is None:
                return None  # 目标单据不存在
            return {"id": 8, "quantity": self.target_quantity}
        if "FROM deliveries" in sql:
            if "id <> %s" in sql:
                # 排除本次那一行之后，其余已算数的行
                return {"total": _dec(self.counted_with_self) - _dec(self.self_quantity)}
            return {"total": self.counted_with_self}
        return None


def _dec(value: object):
    from decimal import Decimal

    return Decimal(str(value))


def _cumulative(*, path: str = "create", **kwargs: Any) -> CumulativeWithin:
    """`path="create"` → 登记类（`new_row_counted=False`）；`path="verify"` → 核验类（`True`）。"""
    return CumulativeWithin(
        target_table="sales_orders",
        target_dims=("sales_order_id",),
        target_columns=("id",),
        children_table="deliveries",
        count_field="quantity",
        target_field="quantity",
        request_field="quantity",
        statuses=("verified",),
        new_row_counted=path == "verify",
        label="累计交付量",
        **kwargs,
    )


# -- 登记路径（delivery.create / receipt.create）：本次行还是 draft --


def test_register_path_passes_when_under_target() -> None:
    """**登记路径**：其余已核验 60 + 本次登记 40 = 100 ≤ 100 → 通过。"""
    tx = _CumulativeTx(counted_with_self=60)
    check(
        _cumulative(path="create"),
        tx=tx,
        payload={"sales_order_id": 8, "quantity": 40, "_invariant_exclude_id": 77},
    )
    sql, params = tx.queries[-1]
    assert "id <> %s" in sql
    assert params[-1] == 77


def test_register_path_rejects_the_very_first_overrun() -> None:
    """**登记路径**：其余已核验 60 + 本次登记 50 = 110 > 100 → 拒绝（第一次超出就拦）。"""
    error = rejects(
        _cumulative(path="create"),
        code="CONFLICT",
        tx=_CumulativeTx(counted_with_self=60),
        payload={"sales_order_id": 8, "quantity": 50, "_invariant_exclude_id": 77},
    )
    assert error.data is not None
    assert error.data["counted"] == "60" and error.data["projected"] == "110"


def test_register_path_accepts_exactly_the_target() -> None:
    """**登记路径**：60 + 40 == 100 → 通过（取等号不算超）。"""
    check(
        _cumulative(path="create"),
        tx=_CumulativeTx(counted_with_self=60),
        payload={"sales_order_id": 8, "quantity": 40, "_invariant_exclude_id": 77},
    )


# -- 核验路径（delivery.verify / receipt.verify）：服务已把本次行置成 verified --


def test_verify_path_passes_at_the_exact_boundary() -> None:
    """**核验路径**（本次行已在库内合计里）：

    100 上限、库里含本次的合计 `counted=100`、本次 40 → 自排除得其余 60，
    `60 + 40 = 100 ≤ 100` → **通过**。修前这里被误拒（100 + 40 = 140 > 100）——
    这是 负责人 点名的场景、也是 sales-dev 实测的 B 用例。
    """
    tx = _CumulativeTx(counted_with_self=100, self_quantity=40)
    check(
        _cumulative(path="verify"),
        tx=tx,
        payload={"sales_order_id": 8, "quantity": 40, "_invariant_exclude_id": 77},
    )
    # 核验路径按 Amendment"只判 counted"：`counted` 要**如实含本次那一行**（库里已算数的合计），
    # 所以这里**不带**自排除。自排除只出现在登记路径（那里它恒等，作误用兜底）。
    sql, params = tx.queries[-1]
    assert "id <> %s" not in sql, f"核验路径不应排除本次那一行：{sql}"
    assert "FOR UPDATE" in sql


def test_verify_path_rejects_when_the_quota_is_already_taken() -> None:
    """**核验路径**：其余已核验已满 100（库里含本次的合计 140，本次 40）→ 110 > 100 → 拒绝。"""
    error = rejects(
        _cumulative(path="verify"),
        code="CONFLICT",
        tx=_CumulativeTx(counted_with_self=140, self_quantity=40),
        payload={"sales_order_id": 8, "quantity": 40, "_invariant_exclude_id": 77},
    )
    assert error.data is not None
    assert error.data["counted"] == "140" and error.data["projected"] == "140"


def test_verify_path_rejects_when_already_over_even_without_this_row() -> None:
    """**核验路径**：除本次以外已经是 120 > 100 → 即便排除本次也要拒。"""
    rejects(
        _cumulative(path="verify"),
        code="CONFLICT",
        tx=_CumulativeTx(counted_with_self=160, self_quantity=40),
        payload={"sales_order_id": 8, "quantity": 40, "_invariant_exclude_id": 77},
    )


def test_both_paths_agree_on_the_effective_total() -> None:
    """两条路径算出来的是**同一个业务量**（本次操作生效之后的累计），只是表达方式按 R1 写：

    * 核验类：本次行已在 `counted` 里 → 库内合计 100（含本次 40）→ 判 100 ≤ 100 通过；
    * 登记类：本次行不在 `counted` 里 → 库内 60 + 本次 40 = 100 ≤ 100 通过。

    两条都恰好取到边界值，所以它们必须**同时**通过 —— 这就是"两条路径统一"的可执行含义。
    """
    check(
        _cumulative(path="verify"),
        tx=_CumulativeTx(counted_with_self=100, self_quantity=40),
        payload={"sales_order_id": 8, "quantity": 40, "_invariant_exclude_id": 77},
    )
    check(
        _cumulative(path="create"),
        tx=_CumulativeTx(counted_with_self=60),
        payload={"sales_order_id": 8, "quantity": 40, "_invariant_exclude_id": 77},
    )


# -- 单位：两侧都不换算 --


def test_register_path_does_not_convert_units_on_either_side() -> None:
    """**两侧都不换算**（负责人 终裁）：`counted` 与 `requested` 都取原始值。

    只给 `requested` 一侧按 `unit` 换算会让 `unit='jin'` 时量纲不一致 —— 实测：
    其余已交付 40 + 本次 40 斤，若把本次按 80 算，`40 + 80 = 120 > 100` **误拒合法交付**。
    """
    check(
        _cumulative(path="create"),
        tx=_CumulativeTx(counted_with_self=40),
        payload={"sales_order_id": 8, "quantity": 40, "unit": "jin", "_invariant_exclude_id": 77},
    )
    error = rejects(
        _cumulative(path="create"),
        code="CONFLICT",
        tx=_CumulativeTx(counted_with_self=90),
        payload={"sales_order_id": 8, "quantity": 20, "unit": "jin", "_invariant_exclude_id": 77},
    )
    assert error.data is not None
    # 关键：projected 必须是 90 + 20 = 110，而**不是** 90 + 40 = 130
    assert error.data["projected"] == "110", error.data


def test_unit_conversion_would_over_reject_a_legal_delivery() -> None:
    """反证（sales-dev 抓到的缺陷形态）：只给 `requested` 一侧换算会误拒合法交付。

    其余已交付 40（原始值）+ 本次 40 斤：同量纲相加 = 80 ≤ 100 → **应当通过**。
    若把本次按 40×2 = 80 换算，就得到 40 + 80 = 120 > 100 → **误拒**。
    """
    check(
        _cumulative(path="create"),
        tx=_CumulativeTx(counted_with_self=40),
        payload={"sales_order_id": 8, "quantity": 40, "unit": "jin", "_invariant_exclude_id": 77},
    )


# -- 声明与基础设施 --


def test_cumulative_within_requires_new_row_counted() -> None:
    """**忘选 = 构造期报错**（不给默认值）：核验类与登记类语义相反，默认值会让忘选的能力静默用错判定。"""
    with pytest.raises(ValueError):
        CumulativeWithin(
            target_table="sales_orders",
            target_dims=("sales_order_id",),
            children_table="deliveries",
        )


def test_cumulative_within_requires_the_exclude_key() -> None:
    """缺 `_invariant_exclude_id` → `INTERNAL_ERROR`（基础设施缺失，不静默继续算）。"""
    rejects(
        _cumulative(path="create"),
        code="INTERNAL_ERROR",
        tx=_CumulativeTx(counted_with_self=60),
        payload={"sales_order_id": 8, "quantity": 40},
    )


def test_cumulative_within_locks_by_default() -> None:
    """`for_update` **默认开**：并发两笔会同时读到旧累计而双双通过。"""
    tx = _CumulativeTx(counted_with_self=60)
    check(
        _cumulative(path="create"),
        tx=tx,
        payload={"sales_order_id": 8, "quantity": 40, "_invariant_exclude_id": 77},
    )
    assert "FOR UPDATE" in tx.queries[-1][0]


def test_cumulative_within_is_skipped_without_source_document() -> None:
    """没关联目标单据（例如无采购单的到货）：无累计可言，跳过。"""
    check(
        _cumulative(path="create"),
        tx=_CumulativeTx(counted_with_self=0),
        payload={"quantity": 10},
    )


def test_cumulative_within_filters_children_by_allowed_status() -> None:
    """只统计**算数**的状态（允许集），作废单据不算进累计。"""
    tx = _CumulativeTx(counted_with_self=0)
    check(
        _cumulative(path="create"),
        tx=tx,
        payload={"sales_order_id": 8, "quantity": 10, "_invariant_exclude_id": 77},
    )
    sql, params = tx.queries[-1]
    assert "status IN (%s)" in sql
    assert "verified" in params


def test_cumulative_within_rejects_missing_source_document() -> None:
    """目标单据不存在 → `NOT_FOUND`（不是静默放行）。"""
    rejects(
        _cumulative(path="create"),
        code="NOT_FOUND",
        tx=_CumulativeTx(counted_with_self=0, target_quantity=None),
        payload={"sales_order_id": 8, "quantity": 1, "_invariant_exclude_id": 77},
    )


# ---------------------------------------------------------------------------
# ZeroBalance（#11）
# ---------------------------------------------------------------------------


def test_zero_balance_passes_when_both_columns_are_zero() -> None:
    tx = FakeUnitOfWork(("FROM batch_stock_records", {"quantity_delta": 0, "weight_delta_kg": 0}))
    check(
        ZeroBalance(
            table="batch_stock_records",
            group_by=("batch_id",),
            columns=("quantity_delta", "weight_delta_kg"),
            labels=("数量", "重量"),
            label="存塘",
        ),
        tx=tx,
        payload={"batch_id": 5},
    )


def test_zero_balance_rejects_remaining_weight() -> None:
    tx = FakeUnitOfWork(("FROM batch_stock_records", {"quantity_delta": 0, "weight_delta_kg": 12.5}))
    error = rejects(
        ZeroBalance(
            table="batch_stock_records",
            group_by=("batch_id",),
            columns=("quantity_delta", "weight_delta_kg"),
            labels=("数量", "重量"),
            label="存塘",
        ),
        code="CONFLICT",
        tx=tx,
        payload={"batch_id": 5},
    )
    assert error.data is not None and error.data["column"] == "weight_delta_kg"
    assert "重量" in error.message


def test_zero_balance_is_skipped_without_group_key() -> None:
    check(ZeroBalance(table="batch_stock_records", group_by=("batch_id",)), payload={})


def test_zero_balance_declines_mismatched_labels_at_construction() -> None:
    with pytest.raises(ValueError):
        ZeroBalance(table="t", columns=("a", "b"), labels=("只有一个",))


# ---------------------------------------------------------------------------
# AtLeastOneOf（#16）
# ---------------------------------------------------------------------------


def test_at_least_one_of_passes_with_quantity() -> None:
    check(AtLeastOneOf(fields=("quantity", "weight_kg")), payload={"quantity": 10})


def test_at_least_one_of_passes_with_weight_only() -> None:
    check(AtLeastOneOf(fields=("quantity", "weight_kg")), payload={"weight_kg": 0})


def test_at_least_one_of_rejects_blank_values() -> None:
    error = rejects(
        AtLeastOneOf(fields=("quantity", "weight_kg"), labels=("投喂量", "重量（kg）")),
        code="VALIDATION_ERROR",
        payload={"quantity": "   ", "weight_kg": None},
    )
    assert error.data is not None and error.data["fields"] == ["quantity", "weight_kg"]
    assert "投喂量" in error.message


def test_at_least_one_of_falls_back_to_before_row() -> None:
    check(AtLeastOneOf(fields=("quantity", "weight_kg")), payload={}, before={"weight_kg": 4})


# ---------------------------------------------------------------------------
# AmountWithin（#4）
# ---------------------------------------------------------------------------


def _amount_within() -> AmountWithin:
    return AmountWithin(
        amount_field="amount",
        table="purchase_payables",
        target_field="payable_id",
        target_column="id",
        balance_column="amount",
        paid_column="paid_amount",
        statuses=("unpaid", "partial"),
        label="应付账款",
    )


def test_amount_within_passes_when_under_balance() -> None:
    tx = FakeUnitOfWork(("FROM purchase_payables", {"id": 3, "amount": 1000, "paid_amount": 400, "status": "partial"}))
    check(_amount_within(), tx=tx, payload={"payable_id": 3, "amount": 500})
    assert "FOR UPDATE" in tx.last_sql, "余额查询必须带行锁，否则并发下的判定是假的"


def test_amount_within_rejects_over_balance() -> None:
    tx = FakeUnitOfWork(("FROM purchase_payables", {"id": 3, "amount": 1000, "paid_amount": 400, "status": "partial"}))
    error = rejects(
        _amount_within(), code="CONFLICT", tx=tx, payload={"payable_id": 3, "amount": 700}
    )
    assert error.data is not None
    assert error.data["balance"] == "600" and error.data["requested"] == "700"


def test_amount_within_uses_pre_write_snapshot_for_final_payment() -> None:
    tx = FakeUnitOfWork(
        (
            "FROM purchase_payables",
            {"id": 3, "amount": 1000, "paid_amount": 1000, "status": "settled", "currency": "CNY"},
        )
    )
    check(
        AmountWithin(
            amount_field="amount",
            table="purchase_payables",
            target_field="payable_id",
            balance_column="amount",
            paid_column="paid_amount",
            statuses=("unpaid", "partial", "settled"),
            label="应付账款",
        ),
        tx=tx,
        payload={
            "payable_id": 3,
            "amount": 600,
            "_amount_within_before": {
                "total": "1000",
                "paid": "400",
                "status": "partial",
                "currency": "CNY",
            },
        },
    )


def test_amount_within_rejects_closed_payable() -> None:
    tx = FakeUnitOfWork(("FROM purchase_payables", {"id": 3, "amount": 1000, "paid_amount": 1000, "status": "paid"}))
    rejects(
        _amount_within(),
        code="CONFLICT",
        tx=tx,
        payload={"payable_id": 3, "amount": 1},
    )


def test_amount_within_rejects_currency_mismatch() -> None:
    tx = FakeUnitOfWork(
        ("FROM purchase_payables", {"id": 3, "amount": 1000, "paid_amount": 0, "status": "unpaid", "currency": "CNY"})
    )
    rejects(
        _amount_within(),
        code="VALIDATION_ERROR",
        tx=tx,
        payload={"payable_id": 3, "amount": 10, "currency": "USD"},
    )


def test_amount_within_is_skipped_without_amount_or_target() -> None:
    check(_amount_within(), payload={"payable_id": 3})
    check(_amount_within(), payload={"amount": 10})


# ---------------------------------------------------------------------------
# NoOverlappingSource（#8）
# ---------------------------------------------------------------------------


def _no_overlap() -> NoOverlappingSource:
    return NoOverlappingSource(
        table="cost_entries",
        source_types=("warehouse_ledger",),
        target_fields=("target_type", "target_id"),
        period_fields=("period_start", "period_end"),
        label="该塘口/批次",
    )


def test_no_overlapping_source_passes_when_nothing_found() -> None:
    tx = FakeUnitOfWork()
    check(
        _no_overlap(),
        tx=tx,
        payload={
            "source_type": "manual_expense",
            "target_type": "pond",
            "target_id": 4,
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            # 分租键：三个都必须给（缺任何一个都会显式报错，见
            # test_no_overlapping_source_refuses_to_widen_without_tenant_key）
            "organization_id": 1,
            "farm_id": 2,
            "area_id": 3,
        },
    )
    sql, params = tx.queries[-1]
    assert "FROM cost_entries" in sql
    assert "period_end >= %s" in sql and "period_start <= %s" in sql
    # 参数顺序：`source_types`（禁止集合）-> **本次来源类型**（`source_type <> %s`）
    #           -> 目标维度 -> 期间 -> 分租键。
    # 第二段是 2026-09-13 收口加上的：只有**跨来源类型**才互斥，所以本次来源类型
    # 也要进谓词（见 test_no_overlapping_source_allows_a_second_row_of_the_same_source_type）。
    assert params[:4] == ["warehouse_ledger", "manual_expense", "pond", 4], params
    assert "source_type <> %s" in sql


def test_no_overlapping_source_rejects_duplicate() -> None:
    tx = FakeUnitOfWork(("FROM cost_entries", {"id": 77}))
    error = rejects(
        _no_overlap(),
        code="CONFLICT",
        tx=tx,
        payload={
            "source_type": "manual_expense",
            "target_type": "pond",
            "target_id": 4,
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "organization_id": 1,
            "farm_id": 2,
            "area_id": 3,
        },
    )
    assert error.data is not None and error.data["existing_id"] == 77


def test_no_overlapping_source_honours_override_source_type() -> None:
    """财务明确选择"库存已自动归集但仍需登记"时放行（早期实现的白名单）。"""
    invariant = NoOverlappingSource(
        table="cost_entries",
        source_types=("warehouse_ledger",),
        override_source_types=("manual_feed_direct", "manual_feed_offset"),
        target_fields=("target_type", "target_id"),
        period_fields=("period_start", "period_end"),
    )
    tx = FakeUnitOfWork(("FROM cost_entries", {"id": 77}))
    check(
        invariant,
        tx=tx,
        payload={
            "source_type": "manual_feed_direct",
            "target_type": "pond",
            "target_id": 4,
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "organization_id": 1,
            "farm_id": 2,
            "area_id": 3,
        },
    )
    assert tx.queries == [], "覆盖来源类型应当直接放行，不查库"


def test_no_overlapping_source_is_skipped_without_target() -> None:
    check(_no_overlap(), tx=FakeUnitOfWork(), payload={"source_type": "manual_expense"})


def test_no_overlapping_source_refuses_missing_period() -> None:
    rejects(
        _no_overlap(),
        code="VALIDATION_ERROR",
        payload={"source_type": "manual_expense", "target_type": "pond", "target_id": 4},
    )


def test_no_overlapping_source_uses_absolute_period_when_provided() -> None:
    """`YYYY-MM` 这种输入算不出月末：服务把精确起止放进 `_period_absolute`。"""
    tx = FakeUnitOfWork()
    check(
        _no_overlap(),
        tx=tx,
        payload={
            "source_type": "manual_expense",
            "target_type": "batch",
            "target_id": 9,
            "period_start": "2026-02",
            "period_end": "2026-02",
            "organization_id": 1,
            "farm_id": 2,
            "area_id": 3,
            "_period_absolute": {"2026-02": ("2026-02-01", "2026-02-28")},
        },
    )
    sql, params = tx.queries[-1]
    # 参数顺序：`source_types` -> 本次来源类型（`source_type <> %s`）-> 目标维度
    #           -> 期间(起止) -> 分租键
    assert params[4:6] == ["2026-02-01", "2026-02-28"], params
    assert params[6:] == [1, 2, 3], params  # 三个分租键都必须进 SQL


def test_no_overlapping_source_declines_bad_period_fields() -> None:
    with pytest.raises(ValueError):
        NoOverlappingSource(period_fields=("only_one",))


# ---------------------------------------------------------------------------
# HarvestQuantityMatch（#6）
# ---------------------------------------------------------------------------


def _harvest_match(**kwargs: Any) -> HarvestQuantityMatch:
    return HarvestQuantityMatch(harvest_table="harvests", statuses=("verified",), **kwargs)


def test_harvest_quantity_match_passes_for_kg() -> None:
    tx = FakeUnitOfWork(("FROM harvests", {"id": 5, "status": "verified", "weight_kg": 250, "batch_id": 2, "pond_id": 3}))
    check(
        _harvest_match(),
        tx=tx,
        payload={"harvest_document_id": 5, "quantity": 250, "unit": "kg", "batch_id": 2, "pond_id": 3},
    )


def test_harvest_quantity_match_passes_for_jin() -> None:
    """斤 = 2 × kg —— 换算规则继承旧 `sales_source_control.py:22`，只在一处。"""
    tx = FakeUnitOfWork(("FROM harvests", {"id": 5, "status": "verified", "weight_kg": 250}))
    check(
        _harvest_match(),
        tx=tx,
        payload={"harvest_document_id": 5, "quantity": 500, "unit": "jin"},
    )


def test_harvest_quantity_match_passes_for_tail() -> None:
    tx = FakeUnitOfWork(("FROM harvests", {"id": 5, "status": "verified", "quantity": 3000, "weight_kg": 250}))
    check(
        _harvest_match(),
        tx=tx,
        payload={"harvest_document_id": 5, "quantity": 3000, "unit": "tail"},
    )


def test_harvest_quantity_match_rejects_mismatch() -> None:
    tx = FakeUnitOfWork(("FROM harvests", {"id": 5, "status": "verified", "weight_kg": 250}))
    error = rejects(
        _harvest_match(),
        code="CONFLICT",
        tx=tx,
        payload={"harvest_document_id": 5, "quantity": 249, "unit": "kg"},
    )
    assert error.data is not None
    assert error.data["fact"] == "250" and error.data["requested"] == "249"


def test_harvest_quantity_match_rejects_unverified_harvest() -> None:
    tx = FakeUnitOfWork(("FROM harvests", {"id": 5, "status": "draft", "weight_kg": 250}))
    rejects(
        _harvest_match(),
        code="CONFLICT",
        tx=tx,
        payload={"harvest_document_id": 5, "quantity": 250, "unit": "kg"},
    )


def test_harvest_quantity_match_rejects_other_pond() -> None:
    tx = FakeUnitOfWork(("FROM harvests", {"id": 5, "status": "verified", "weight_kg": 250, "pond_id": 3}))
    rejects(
        _harvest_match(),
        code="CONFLICT",
        tx=tx,
        payload={"harvest_document_id": 5, "quantity": 250, "unit": "kg", "pond_id": 8},
    )


def test_harvest_quantity_match_is_skipped_without_harvest() -> None:
    check(_harvest_match(), tx=FakeUnitOfWork(), payload={"quantity": 250, "unit": "kg"})


# ---------------------------------------------------------------------------
# AtMostOnePending（#21）
# ---------------------------------------------------------------------------


def test_at_most_one_pending_passes_when_none_exists() -> None:
    tx = FakeUnitOfWork()
    check(
        AtMostOnePending(
            dims=("pond_id",),
            table="pond_status_change_requests",
            pending_states=("submitted",),
            label="待核验状态变更申请",
        ),
        tx=tx,
        payload={"pond_id": 6},
    )
    params = tx.queries[-1][1]
    assert params == [6, "submitted"]


def test_at_most_one_pending_rejects_second_request() -> None:
    tx = FakeUnitOfWork(("FROM pond_status_change_requests", {"id": 12}))
    error = rejects(
        AtMostOnePending(
            dims=("pond_id",),
            table="pond_status_change_requests",
            label="待核验状态变更申请",
        ),
        code="CONFLICT",
        tx=tx,
        payload={"pond_id": 6},
    )
    assert error.data is not None
    assert error.data["rule"] == "AT_MOST_ONE_PENDING"
    assert error.data["dimensions"] == {"pond_id": 6}


def test_at_most_one_pending_is_skipped_for_non_create_capabilities() -> None:
    """核验/取消类能力不会新增待办行，不该被这条规则拦住。"""
    check(
        AtMostOnePending(dims=("pond_id",), table="pond_status_change_requests"),
        tx=FakeUnitOfWork(("FROM pond_status_change_requests", {"id": 12})),
        payload={"request_id": 12, "expected_version": 1},
    )


def test_at_most_one_pending_declines_empty_dims() -> None:
    with pytest.raises(ValueError):
        AtMostOnePending(dims=(), table="t")


# ---------------------------------------------------------------------------
# 执行器注入的不变量上下文（表名 / 资源名 / 记录主键）
# ---------------------------------------------------------------------------


def test_runner_injects_resource_context_for_optimistic_lock() -> None:
    from yuxin.kernel.capability import Capability, HttpMethod
    from yuxin.kernel.runner import CapabilityRunner

    spec = Capability(
        name="ut_pond.update",
        title="修改测试塘口",
        domain="ut",
        resource="ut_pond",
        handler=lambda: None,
        method=HttpMethod.PATCH,
        path="/api/v1/ut-ponds/{pond_id}",
        kind="update",
    )

    class _Invocation:
        path_params = {"pond_id": 11}

    extra = CapabilityRunner._invariant_extra(
        object.__new__(CapabilityRunner),  # 只用到纯函数，不构造真实依赖
        spec,
        result=type("R", (), {"resource_id": 11, "data": {}})(),
        invocation=_Invocation(),
    )
    assert extra["_resource"] == "ut_pond"
    assert extra["_resource_table"] == "ut_ponds"
    assert extra["_resource_key_column"] == "id"
    assert extra["_resource_id"] == 11


def test_runner_context_can_be_extended_by_the_service() -> None:
    from yuxin.kernel.capability import Capability, HttpMethod
    from yuxin.kernel.runner import CapabilityRunner

    spec = Capability(
        name="ut_pond.verify",
        title="核验测试塘口",
        domain="ut",
        resource="ut_pond",
        handler=lambda: None,
        method=HttpMethod.POST,
        path="/api/v1/ut-ponds/{pond_id}/verify",
        kind="action",
    )

    class _Invocation:
        path_params = {"pond_id": 11}

    result = type(
        "R", (), {"resource_id": 11, "data": {"_invariant_context": {"_ledger_lines": [{"a": 1}]}}}
    )()
    extra = CapabilityRunner._invariant_extra(object.__new__(CapabilityRunner), spec, result, _Invocation())
    assert extra["_ledger_lines"] == [{"a": 1}]
    assert extra["_resource_table"] == "ut_ponds"


def test_decimal_boundaries_for_zero_balance_tolerance() -> None:
    """尾差：float 换算成 Decimal 会留 1e-13 级别的残值，不能因此拒绝关账。"""
    tx = FakeUnitOfWork(("FROM batch_stock_records", {"quantity_delta": Decimal("1E-13")}))
    check(
        ZeroBalance(table="batch_stock_records", group_by=("batch_id",), columns=("quantity_delta",)),
        tx=tx,
        payload={"batch_id": 5},
    )


# ---------------------------------------------------------------------------
# 端到端：执行器真的把已声明的不变量跑起来了
#
# 这一节存在的理由就是 t1 的背景：`invariants=` 在整个 backend/ 与 tools/ 里
# **一次都没被使用过** —— 执行器是通的，但没有任何能力喂给它。所以"不变量单元测试
# 全绿"并不足以证明规则被强制；必须有一条测试证明 `CapabilityRunner.invoke()`
# 真的走到了 `invariant.check()`，且喂给它的上下文是对的。
# ---------------------------------------------------------------------------


class _RunnerTransaction:
    """只记录 SQL 的 UnitOfWork 替身（端到端测试用，不碰真库）。"""

    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.committed = False

    def execute(self, sql: str, params: Any = None) -> int:
        self.statements.append((sql, params))
        return 1

    def query_one(self, sql: str, params: Any = None) -> Any:
        self.statements.append((sql, params))
        return None

    def query_all(self, sql: str, params: Any = None) -> list[Any]:
        self.statements.append((sql, params))
        return []

    def last_insert_id(self) -> int:
        return 41


class _RunnerUow:
    def __init__(self) -> None:
        self.tx = _RunnerTransaction()

    def begin(self) -> Any:
        from contextlib import contextmanager

        @contextmanager
        def _scope() -> Any:
            yield self.tx

        return _scope()


class _FixedScopeResolver:
    def resolve(self, *, user_id: int, role_codes: frozenset[str]) -> Scope:
        return Scope.all_data(user_id=user_id)


class _AuditWriter:
    def __init__(self) -> None:
        self.events: list[str] = []

    def write(self, tx: Any, event: Any, **kwargs: Any) -> None:
        self.events.append(event.capability)


class _SpyInvariant:
    """记录被调用时收到的上下文，并按配置决定是否拒绝。"""

    name = "SpyInvariant"

    def __init__(self, *, refuse: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.refuse = refuse

    def check(self, *, tx: Any, scope: Any, actor_id: int, payload: dict[str, Any],
              before: dict[str, Any] | None) -> None:
        self.calls.append(
            {"tx": tx, "scope": scope, "actor_id": actor_id, "payload": payload, "before": before}
        )
        if self.refuse:
            raise DomainError(ErrorCode.CONFLICT, "间谍不变量按配置拒绝了本次操作")


class _MinimalService:
    def create(
        self,
        tx: Any,
        ctx: Any,
        scope: Any,
        code: f_str("编号", required=True, max_length=64),
    ) -> Any:
        from yuxin.kernel.capability import HandlerResult

        ctx.require("ut.invariant.create")
        tx.execute("INSERT INTO ut_invariants (code) VALUES (%s)", (code,))
        return HandlerResult(data={"row": {"id": 41, "code": code, "status": "draft"}}, resource_id=41)

    @staticmethod
    def load_for_audit(tx: Any, *, scope: Any, record_id: int) -> dict[str, Any]:
        return {"id": record_id, "code": "UT-1", "status": "draft"}


def _run_invariant_capability(spy: _SpyInvariant) -> Any:
    from yuxin.kernel.capability import (
        AgentExposure,
        Capability,
        Confirmation,
        HttpMethod,
        Registry,
        Risk,
    )
    from yuxin.kernel.idempotency import IdempotencyStore
    from yuxin.kernel.runner import ActorView, CapabilityRunner, Invocation

    _MinimalService.create.__yuxin_load_by_id__ = _MinimalService.load_for_audit  # type: ignore[attr-defined]

    registry = Registry()
    registry.register(
        Capability(
            name="ut_invariant.create",
            title="不变量端到端探针",
            domain="ut",
            resource="ut_pond",
            handler=_MinimalService.create,
            service_factory=_MinimalService,
            method=HttpMethod.POST,
            path="/api/v1/ut-invariants",
            kind="create",
            required_permission="ut.invariant.create",
            risk=Risk.NORMAL,
            confirmation=Confirmation.NEVER,
            agent_exposure=AgentExposure.HIDDEN,
            idempotent=False,
            invariants=(spy,),
        )
    )

    uow = _RunnerUow()
    runner = CapabilityRunner(
        registry=registry,
        uow_factory=lambda: uow,
        audit=_AuditWriter(),
        idempotency=IdempotencyStore(lambda: _RunnerUow()),
        scope_resolver=_FixedScopeResolver(),
    )
    actor = ActorView(
        user_id=9,
        username="ut",
        permissions=frozenset({"ut.invariant.create"}),
        role_codes=frozenset(),
    )
    return runner.invoke(
        Invocation(capability_name="ut_invariant.create", payload={"code": "UT-1"}),
        actor,
        "req-ut-1",
    )


def test_runner_really_executes_declared_invariants() -> None:
    """`invariants=(...)` 必须真的被执行 —— 这是 t1 的"零使用"问题的回归测试。"""
    spy = _SpyInvariant()
    result = _run_invariant_capability(spy)

    assert result.kind == "executed"
    assert result.resource_id == 41
    assert len(spy.calls) == 1, "声明了不变量却没有被执行器调用"
    call = spy.calls[0]
    assert call["actor_id"] == 9
    # 执行器注入的上下文：表名/资源名/记录主键（OptimisticLock 与 StateTransition 依赖它们）
    assert call["payload"]["_resource"] == "ut_pond"
    assert call["payload"]["_resource_table"] == "ut_ponds"
    assert call["payload"]["_resource_id"] == 41
    assert call["payload"]["code"] == "UT-1"


def test_runner_turns_invariant_rejection_into_a_failure_and_rolls_back() -> None:
    spy = _SpyInvariant(refuse=True)
    with pytest.raises(DomainError) as caught:
        _run_invariant_capability(spy)
    assert str(caught.value.code) == "CONFLICT"
    assert "间谍不变量" in caught.value.message

# ---------------------------------------------------------------------------
# 类型的**形态**约束（registry §4 的 21 条规则按类型挂在能力上，形态错了就挂不上）
# ---------------------------------------------------------------------------


class _MinimalService:
    """只为端到端探针提供处理器：路径参数名与资源名都要与 `Resource` 对得上。"""

    def create(
        self,
        tx: Any,
        ctx: Any,
        scope: Any,
        code: f_str("编号", required=True, max_length=64),
    ) -> Any:
        from yuxin.kernel.capability import HandlerResult

        ctx.require("ut.invariant.create")
        tx.execute("INSERT INTO ut_invariants (code) VALUES (%s)", (code,))
        return HandlerResult(
            data={"row": {"id": 41, "code": code, "status": "draft"}}, resource_id=41
        )

    @staticmethod
    def load_for_audit(tx: Any, *, scope: Any, record_id: int) -> dict[str, Any]:
        return {"id": record_id, "code": "UT-1", "status": "draft"}


def _invariant_classes() -> list[type]:
    from yuxin.kernel import invariants as module

    return [getattr(module, name) for name in module.__all__]


def test_every_invariant_is_a_frozen_slots_dataclass() -> None:
    """每条规则都必须能"声明一次、多处引用"：不可变 + 无 `__dict__`。"""
    import dataclasses

    for cls in _invariant_classes():
        assert dataclasses.is_dataclass(cls), f"{cls.__name__} 不是 dataclass"
        assert cls.__dataclass_params__.frozen, (  # type: ignore[attr-defined]
            f"{cls.__name__} 不是 frozen —— 规则被就地改动就不再可复现"
        )
        assert getattr(cls, "__slots__", None), f"{cls.__name__} 没有 slots"
        assert isinstance(cls.name, str) and cls.name, f"{cls.__name__} 缺少 name 属性"


def test_every_invariant_implements_the_check_contract() -> None:
    """`check()` 的签名必须与 `Invariant` 协议逐字一致 —— 执行器按关键字调用。"""
    import inspect

    for cls in _invariant_classes():
        signature = inspect.signature(cls.check)
        assert list(signature.parameters) == ["self", "tx", "scope", "actor_id", "payload", "before"], (
            f"{cls.__name__}.check 的签名不符合契约：{list(signature.parameters)}"
        )
        for name in ("tx", "scope", "actor_id", "payload", "before"):
            assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, (
                f"{cls.__name__}.check 的 {name} 必须是关键字参数"
            )


def test_invariant_name_matches_its_class_name() -> None:
    """`name` 是出错载荷与前端文案的锚点；与类名不一致就是两处描述同一件事。"""
    for cls in _invariant_classes():
        assert cls.name == cls.__name__, f"{cls.__name__} 的 name 是 {cls.name!r}"


#: 每条规则的一组"最小可用声明"：参数取默认值、payload 为空时**都必须跳过**。
#: 这张表同时是回归测试 —— 某条规则后来改成"缺参数就报错"时，这里会失败。
_MINIMAL_DECLARATIONS: dict[str, Any] = {}


def _minimal_declarations() -> dict[str, Any]:
    if _MINIMAL_DECLARATIONS:
        return _MINIMAL_DECLARATIONS
    from yuxin.kernel import invariants as module

    _MINIMAL_DECLARATIONS.update(
        {
            "StateTransition": module.StateTransition(machine="ut_pond"),
            "StatusAllowsEdit": module.StatusAllowsEdit(),
            "OptimisticLock": module.OptimisticLock(),
            "ReferencedStatus": module.ReferencedStatus(
                field="material_id", table="materials", statuses=("verified",)
            ),
            "SameTenant": module.SameTenant(fields=("material_id",), tables={"material_id": "materials"}),
            "UniqueCode": module.UniqueCode(
                fields=("code",), scope=("organization_id",), table="materials"
            ),
            "CumulativeWithin": module.CumulativeWithin(
                target_table="sales_orders",
                target_dims=("sales_order_id",),
                children_table="deliveries",
                # 必填（R1 Amendment）：空提交场景下用登记类语义即可
                new_row_counted=False,
            ),
            "ZeroBalance": module.ZeroBalance(table="batch_stock_records", group_by=("batch_id",)),
            "AtLeastOneOf": module.AtLeastOneOf(fields=("quantity", "weight_kg")),
            "AmountWithin": module.AmountWithin(
                amount_field="amount", table="purchase_payables", target_field="payable_id"
            ),
            "NoOverlappingSource": module.NoOverlappingSource(),
            "HarvestQuantityMatch": module.HarvestQuantityMatch(),
            "AtMostOnePending": module.AtMostOnePending(dims=("pond_id",), table="requests"),
            "DistinctActors": module.DistinctActors(),
            "NoNegativeStock": module.NoNegativeStock(table="inventory_ledger"),
            "PeriodOpen": module.PeriodOpen(),
            "RequiredField": module.RequiredField(fields=("source_ref",)),
            "RequiredWhen": module.RequiredWhen(
                when_field="status", when_values=("cancelled",), required_fields=("reason",)
            ),
        }
    )
    return _MINIMAL_DECLARATIONS


def test_every_invariant_is_inert_when_nothing_was_submitted() -> None:
    """空 payload + 无 before（新建场景）时，**除"总是要成立"的两条外**都必须放行。

    这条测试守着一个很容易踩的坑：规则里少写一个"字段没提交就跳过"的判断，
    就会让**每一个**新建能力都被这条不相干的规则拒掉。空提交没有任何业务事实，
    因此绝大多数规则都不该拒绝它。

    `AtLeastOneOf` 与 `RequiredField` 是刻意的例外：它们的语义就是"本次请求必须
    给出某个值"，空提交正是它们要拒绝的情况。`NoNegativeStock` 也是例外 ——
    它缺账本行上下文时**报错而不是跳过**（这是"缺失即放行"那个缺陷的修复）。
    """
    always_on = {"AtLeastOneOf", "RequiredField", "NoNegativeStock"}
    declarations = _minimal_declarations()
    assert set(declarations) == {cls.__name__ for cls in _invariant_classes()}, (
        "有规则没有加进 _minimal_declarations，这条测试就会漏掉它"
    )
    for name, instance in declarations.items():
        if name in always_on:
            rejects(instance, tx=FakeUnitOfWork(), payload={}, before=None)
            continue
        check(instance, tx=FakeUnitOfWork(), payload={}, before=None)


def test_declarations_are_reusable_objects_not_stateful() -> None:
    """同一个声明对象被多个能力共用（`_EXPOSED` 那类共享常量），必须无状态。"""
    declarations = _minimal_declarations()
    instance = declarations["AtMostOnePending"]
    tx = FakeUnitOfWork()
    check(instance, tx=tx, payload={"pond_id": 1}, before=None)
    check(instance, tx=FakeUnitOfWork(), payload={"pond_id": 2}, before=None)

# ---------------------------------------------------------------------------
# 自排除必须是 SQL 语义（"除了我以外还有没有冲突行"），不能靠"取回任意一行再比对"
#
# 缺陷形态：`LIMIT 1` 取到哪一行由存储引擎决定；库里有历史残留时可能先取到**旧行**，
# 于是 `_is_self_match` 返回 False → 把"我自己那一行"当成"另一笔重复" → 正常路径被拒
# （假阳性）。cost-dev 的 e2e 就是这么失败的。修法：把 `id <> %s` 下推到 WHERE。
# ---------------------------------------------------------------------------


class _RecordingTx:
    """记录 SQL 并按"是否已排除本次行"返回结果的替身。

    ``conflict_id``：除"我这一行"以外**还有**的那一行的 id（None = 没有别人）。
    ``legacy_row``：**没有**把排除下推到 SQL 时，`LIMIT 1` 会取到的那一行
    （历史残留）—— 这正是假阳性的来源，用它来证明修法真的改变了行为。
    """

    def __init__(self, *, conflict_id: int | None, legacy_row: dict | None = None) -> None:
        self.conflict_id = conflict_id
        self.legacy_row = legacy_row
        self.queries: list[tuple[str, list]] = []

    def query_one(self, sql: str, params=None):
        self.queries.append((sql, list(params or ())))
        if "id <> %s" in sql:
            return None if self.conflict_id is None else {"id": self.conflict_id}
        return self.legacy_row


def test_no_overlapping_source_pushes_self_exclusion_into_sql() -> None:
    from yuxin.kernel.invariants import NoOverlappingSource

    tx = _RecordingTx(conflict_id=None)  # 库里只有"我这一行"
    check(
        NoOverlappingSource(
            table="cost_entries",
            target_fields=("target_type", "target_id"),
            period_fields=("period_start", "period_end"),
        ),
        tx=tx,
        payload={
            "source_type": "manual_expense",
            "target_type": "pond",
            "target_id": 4,
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "organization_id": 1,
            "farm_id": 2,
            "area_id": 3,
            "_invariant_exclude_id": 77,
        },
    )
    sql, params = tx.queries[-1]
    assert "id <> %s" in sql, f"自排除没有下推到 SQL：{sql}"
    assert 77 in params, f"排除键没进参数：{params}"


def test_no_overlapping_source_still_rejects_another_row() -> None:
    from yuxin.kernel.invariants import NoOverlappingSource

    tx = _RecordingTx(conflict_id=12)  # 除了我以外还有一行 → 真冲突
    rejects(
        NoOverlappingSource(
            table="cost_entries",
            target_fields=("target_type", "target_id"),
            period_fields=("period_start", "period_end"),
        ),
        code="CONFLICT",
        tx=tx,
        payload={
            "source_type": "manual_expense",
            "target_type": "pond",
            "target_id": 4,
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "organization_id": 1,
            "farm_id": 2,
            "area_id": 3,
            "_invariant_exclude_id": 77,
        },
    )


def test_at_most_one_pending_pushes_self_exclusion_into_sql() -> None:
    tx = _RecordingTx(conflict_id=None)
    check(
        AtMostOnePending(dims=("pond_id",), table="pond_status_change_requests"),
        tx=tx,
        payload={"pond_id": 6, "_invariant_exclude_id": 77},
    )
    sql, params = tx.queries[-1]
    assert "id <> %s" in sql, f"自排除没有下推到 SQL：{sql}"
    assert 77 in params


def test_at_most_one_pending_still_rejects_another_pending_row() -> None:
    tx = _RecordingTx(conflict_id=12)
    rejects(
        AtMostOnePending(dims=("pond_id",), table="pond_status_change_requests"),
        code="CONFLICT",
        tx=tx,
        payload={"pond_id": 6, "_invariant_exclude_id": 77},
    )


def test_self_exclusion_is_harmless_without_the_context_key() -> None:
    """执行器没注入排除键时，SQL 与参数都不变，行为与以前一致。"""
    tx = _RecordingTx(conflict_id=None, legacy_row=None)
    check(
        AtMostOnePending(dims=("pond_id",), table="pond_status_change_requests"),
        tx=tx,
        payload={"pond_id": 6},
    )
    sql, params = tx.queries[-1]
    assert "id <> %s" not in sql
    assert params == [6, "submitted"], params


def test_without_exclusion_a_stale_row_would_have_been_a_false_positive() -> None:
    """反证：**没有**下推排除时，`LIMIT 1` 取到历史残留行就会误拒。

    这条不测内核（内核已经修好），它把"缺陷为什么会出现"固化成可执行的事实，
    防止将来有人把排除又改回"取回来再比对"。
    """
    from yuxin.kernel.invariants import AtMostOnePending

    invariant = AtMostOnePending(dims=("pond_id",), table="pond_status_change_requests")
    tx = _RecordingTx(conflict_id=None, legacy_row={"id": 12})  # 12 = 别人的旧行
    # 把排除键**悄悄丢掉**，模拟"没有下推到 SQL"的旧形态：
    original_query = tx.query_one

    def query_without_exclusion(sql: str, params=None):
        return original_query(sql.replace(" AND id <> %s", ""), params)

    tx.query_one = query_without_exclusion  # type: ignore[method-assign]
    rejects(
        invariant,
        code="CONFLICT",
        tx=tx,
        payload={"pond_id": 6, "_invariant_exclude_id": 77},
    )


def test_unique_code_pushes_self_exclusion_into_sql() -> None:
    from yuxin.kernel.invariants import UniqueCode

    tx = _RecordingTx(conflict_id=None)
    check(
        UniqueCode(fields=("code",), scope=("organization_id",), table="materials"),
        tx=tx,
        payload={
            "code": "M-001",
            "organization_id": 1,
            "farm_id": 2,
            "area_id": 3,
            "_invariant_exclude_id": 77,
        },
    )
    sql, params = tx.queries[-1]
    assert "id <> %s" in sql, f"自排除没有下推到 SQL：{sql}"
    assert 77 in params


def test_unique_code_still_rejects_another_row_with_same_code() -> None:
    from yuxin.kernel.invariants import UniqueCode

    tx = _RecordingTx(conflict_id=12)
    rejects(
        UniqueCode(fields=("code",), scope=("organization_id",), table="materials"),
        code="CONFLICT",
        tx=tx,
        payload={"code": "M-001", "organization_id": 1, "_invariant_exclude_id": 77},
    )


def test_every_key_column_default_is_id() -> None:
    """三个类型的 `key_column` 默认都是 `id`（本项目所有表的主键列名）。"""
    from yuxin.kernel.invariants import AtMostOnePending as _A
    from yuxin.kernel.invariants import NoOverlappingSource as _N
    from yuxin.kernel.invariants import UniqueCode as _U

    assert _A(dims=("pond_id",), table="t").key_column == "id"
    assert _N(table="t").key_column == "id"
    assert _U(table="t").key_column == "id"

# ---------------------------------------------------------------------------
# t20：声明了规则、却解析不出"判定所需的基础设施" → 必须吵，不能静默跳过
#
# 三分法（负责人 采纳的验收口径）：
#   * **业务事实缺失** → 跳过（规则不适用）：payload 里没有某个字段、没有目标单据 id……
#   * **基础设施缺失** → `INTERNAL_ERROR`：内核拿不到"把这条规则跑起来"所需的东西。
# 下面每条都对应一次真实事故形态（DEVELOPMENT §4：声明了但不生效，且全程不报错）。
# ---------------------------------------------------------------------------


def test_optimistic_lock_raises_when_it_cannot_locate_the_row() -> None:
    """提交了期望版本、却既没有 `before` 也解析不出表/主键 → 报错。

    事故形态：能力声明了 `OptimisticLock()`，但 `Resource` 没声明 `table`、路径参数也
    没给 —— 静默跳过的后果是**乐观锁在这条能力上根本没生效**，而声明处看起来挂上了。
    """
    error = rejects(
        OptimisticLock(),
        code="INTERNAL_ERROR",
        tx=FakeUnitOfWork(),
        payload={"expected_version": 3},  # 没有 _resource_table / _resource_id
        before=None,
    )
    assert error.data is not None and error.data["rule"] == "OPTIMISTIC_LOCK"


def test_optimistic_lock_raises_when_table_resolves_but_row_id_is_missing() -> None:
    error = rejects(
        OptimisticLock(),
        code="INTERNAL_ERROR",
        tx=FakeUnitOfWork(),
        payload={"_resource_table": "ut_ponds", "expected_version": 3},
        before=None,
    )
    assert error.data is not None and error.data["resource_table"] == "ut_ponds"


def test_optimistic_lock_raises_when_version_column_reads_null() -> None:
    """定位信息齐全、那一行却没有版本值 → 结构/数据不一致，同样要吵。"""
    tx = FakeUnitOfWork(("FROM ut_ponds", {"_version": None}))
    rejects(
        OptimisticLock(),
        code="INTERNAL_ERROR",
        tx=tx,
        payload={"_resource_table": "ut_ponds", "_resource_id": 11, "expected_version": 1},
        before=None,
    )


def test_optimistic_lock_still_skips_when_expected_version_is_absent() -> None:
    """**业务事实缺失**：没提交期望版本 —— 由字段声明 `required=True` 兜住，本规则跳过。"""
    check(OptimisticLock(), payload={}, before={"row_version": 9})


def test_state_transition_accepts_a_workflow_instance() -> None:
    """`machine` 直接给 `Workflow` 实例：给"没有列表页、不值得注册 Resource"的资源用。"""
    workflow = _pond_workflow()
    check(
        StateTransition(machine=workflow, label="inventory_lot"),
        payload={"status": "submitted"},
        before={"status": "draft"},
    )
    rejects(
        StateTransition(machine=workflow),
        code="CONFLICT",
        payload={"status": "verified"},
        before={"status": "draft"},
    )


def test_state_transition_still_rejects_unregistered_machine_name() -> None:
    """传**字符串**但没注册 → 仍然是基础设施缺失 → 报错（两条分支都要守住）。"""
    rejects(
        StateTransition(machine="ut_missing"),
        code="INTERNAL_ERROR",
        payload={"status": "submitted"},
        before={"status": "draft"},
    )


def test_state_transition_raises_when_row_is_locatable_but_unreadable() -> None:
    """交了状态字段、能定位到行、却读不到旧值 → 该能力缺回读函数 → 报错。

    事故形态：`__yuxin_load_by_id__` 忘挂（t13/t15 修过的模板债）。静默跳过的后果是
    状态转移校验在这条能力上不生效，而执行器本该在回读阶段就暴露它。
    """
    error = rejects(
        StateTransition(machine="ut_pond"),
        code="INTERNAL_ERROR",
        payload={"status": "submitted", "_resource_id": 42},
        before=None,
    )
    assert error.data is not None and error.data["rule"] == "STATE_TRANSITION"


def test_state_transition_still_skips_for_a_brand_new_record() -> None:
    """**业务事实缺失**：新建的行没有"从哪个状态来"，跳过是对的。"""
    check(
        StateTransition(machine="ut_pond"),
        payload={"status": "draft"},
        before=None,
    )


def test_state_transition_still_skips_when_no_status_field_is_submitted() -> None:
    check(
        StateTransition(machine="ut_pond"),
        payload={"name": "一号塘", "_resource_id": 42},
        before={"status": "verified"},
    )


# ---------------------------------------------------------------------------
# `StateTransition` 形态二：迁移两端由服务经 `_invariant_context` 提供
#
# 缺陷形态（本次修掉的那种）：`pond_status_change.verify` 真的把塘口状态从
# `from_status` 迁到 `to_status`，但两个值**都不在请求体里、也不在同一份快照里**
# （当前状态在塘口行、目标状态在申请行）。声明挂在字段触发形态上 ⇒
# `field_name in payload` 恒假 ⇒ **永不触发**：声明处看起来挂了、运行期必定跳过。
# 这正是本项目要消灭的"声明了却不生效"。
# ---------------------------------------------------------------------------


def _status_change_transition():
    """形态二的声明：迁移两端从 `_invariant_context` 取。"""
    return StateTransition(
        machine="ut_pond",
        field_name="pond_status",
        from_context_fields={"from_field": "from_status", "to_field": "to_status"},
    )


def _status_change_payload(source: str, destination: str) -> dict[str, Any]:
    # 刻意用**嵌套**的 `_invariant_context`：执行器会把它平铺进 payload，
    # 但直接调用（与本测试）不该依赖平铺 —— 两种来源都要认。
    return {"_invariant_context": {"from_status": source, "to_status": destination}}


def test_state_transition_rejects_an_illegal_transition_from_context() -> None:
    """**反例（这条是核心）**：非法迁移必须真的被拒。

    只测"合法迁移通过"是不够的 —— 一个永不触发的声明同样会让合法迁移通过。
    判据：`ut_pond` 的转移表是 `draft→submitted→verified`，所以 `draft→verified`
    必须被拒，且错误 data 带 `rule=STATE_TRANSITION` 与状态机的拒绝理由。
    """
    error = rejects(
        _status_change_transition(),
        code="CONFLICT",
        payload=_status_change_payload("draft", "verified"),  # 跳过 submitted：非法
        before=None,
    )
    assert error.data is not None
    assert error.data["rule"] == "STATE_TRANSITION"
    assert error.data.get("field") == "pond_status"


def test_state_transition_passes_a_legal_transition_from_context() -> None:
    """正例：转移表里有的迁移放行（与反例配对，证明拒的是**迁移是否合法**而不是参数）。"""
    check(
        _status_change_transition(),
        payload=_status_change_payload("draft", "submitted"),
        before=None,
    )


def test_state_transition_from_context_treats_no_change_as_no_transition() -> None:
    """`from == to` 不构成转移 —— 与形态一同一口径。"""
    check(
        _status_change_transition(),
        payload=_status_change_payload("verified", "verified"),
        before=None,
    )


def test_state_transition_from_context_refuses_a_half_supplied_transition() -> None:
    """**只给一端** → `INTERNAL_ERROR`：半个迁移判定比不判更危险。

    它给出的是**错误的结论**（拿 `None` 去比状态机会命中或漏掉一条真实拒绝），
    而调用方看不出任何异常。按 R2：这是装配缺失（"内核有没有能力跑起来"），必须吵。
    """
    error = rejects(
        _status_change_transition(),
        code="INTERNAL_ERROR",
        payload={"_invariant_context": {"from_status": "draft"}},  # 缺 to_status
        before=None,
    )
    assert error.data is not None
    assert error.data["missing"] == ["to_status"]


def test_state_transition_from_context_refuses_when_nothing_is_supplied() -> None:
    """两端都没给 → 同样显式报错（**不静默放行**）。"""
    error = rejects(
        _status_change_transition(),
        code="INTERNAL_ERROR",
        payload={"pond_id": 7, "request_id": 9},
        before=None,
    )
    assert error.data is not None
    assert error.data["missing"] == ["from_status", "to_status"]


def test_state_transition_declares_a_half_configured_context_as_invalid() -> None:
    """构造期就拒绝半配置的 `from_context_fields`（忘写一个键 = 声明期错误）。"""
    with pytest.raises(ValueError):
        StateTransition(machine="ut_pond", from_context_fields={"from_field": "a"})


def test_state_transition_keeps_the_field_form_untouched() -> None:
    """**既有行为逐字不变**：没声明 `from_context_fields` 时仍走字段触发。

    这条是"纯增量"的守卫：形态二不得改变形态一的分支（含"没提交状态字段就跳过"）。
    """
    # 没提交状态字段 → 跳过（即使上下文里有迁移两端）
    check(
        StateTransition(machine="ut_pond"),
        payload=_status_change_payload("draft", "verified"),
        before={"status": "draft"},
    )
    # 提交了非法迁移 → 仍按 `before` 判定并拒绝
    rejects(
        StateTransition(machine="ut_pond"),
        code="CONFLICT",
        payload={"status": "verified"},
        before={"status": "draft"},
    )

# ---------------------------------------------------------------------------
# "写入之后才查"的真实时序：不变量会查到自己刚写的那一行（两位域工程师实测报过）
#
# 事故形态（`UniqueCode`）：`run_invariants()` 在 `_call_service()` **之后**执行，
# 此时 INSERT 已在同一事务里，`SELECT id FROM <table> WHERE <编码>` **一定查到自己**。
# 而 create 场景 `before is None` → 只从 `before` 取"我是不是自己"的旧写法**永不成立**
# → 干净库第一次 `pond.create` 就 409（生产与 master_data 两处都报过）。
# ---------------------------------------------------------------------------


class _InsertThenReadTx:
    """模拟"INSERT 已落在同一事务里"：表里存了哪些行，`query_one` 就能查到。

    它按 SQL 里是否带 `id <> %s` 与参数值过滤 —— 与真库的 WHERE 语义一致，
    所以能真正验证"排除有没有生效"，而不是靠预设响应。
    """

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.queries: list[tuple[str, list]] = []

    def query_one(self, sql: str, params=None):
        args = list(params or ())
        self.queries.append((sql, args))
        hits = list(self.rows)
        if "id <> %s" in sql:
            excluded = args[-1]
            hits = [row for row in hits if row.get("id") != excluded]
        return hits[0] if hits else None


def test_unique_code_passes_when_the_only_hit_is_the_row_just_written() -> None:
    """**真实时序**：表里只有刚写进去的那一行（id == `_invariant_exclude_id`）→ 放行。"""
    tx = _InsertThenReadTx([{"id": 77, "code": "P-1", "organization_id": 1}])
    check(
        UniqueCode(fields=("code",), scope=("organization_id",), table="ponds"),
        tx=tx,
        payload={
            "code": "P-1",
            "organization_id": 1,
            "organization_id": 1,
            "_resource_table": "ponds",
            "_resource_id": 77,
            "_invariant_exclude_id": 77,
        },
        before=None,  # 新建：没有 before，只能靠执行器注入的排除键
    )


def test_unique_code_rejects_when_another_row_has_the_same_code() -> None:
    """**真实时序**：表里另有一行同编码（id 不同）→ `CONFLICT`（规则仍然有效）。"""
    tx = _InsertThenReadTx(
        [
            {"id": 12, "code": "P-1", "organization_id": 1},
            {"id": 77, "code": "P-1", "organization_id": 1},
        ]
    )
    rejects(
        UniqueCode(fields=("code",), scope=("organization_id",), table="ponds"),
        code="CONFLICT",
        tx=tx,
        payload={
            "code": "P-1",
            "organization_id": 1,
            "_resource_table": "ponds",
            "_resource_id": 77,
            "_invariant_exclude_id": 77,
        },
        before=None,
    )


# ---------------------------------------------------------------------------
# 端到端：执行器注入的 `_invariant_exclude_id` 真的让 `UniqueCode` 排除本次行
# ---------------------------------------------------------------------------


class _InMemoryTx:
    """带"内存表"的事务替身：`execute` 真的把行写进去，`query_one` 真的查回来。"""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def execute(self, sql: str, params=None) -> int:
        if sql.strip().upper().startswith("INSERT"):
            self.rows.append({"id": 41, "code": (params or [""])[0]})
        return 1

    def query_one(self, sql: str, params=None):
        args = list(params or ())
        hits = list(self.rows)
        if "id <> %s" in sql:
            excluded = args[-1]
            hits = [row for row in hits if row.get("id") != excluded]
        return hits[0] if hits else None

    def query_all(self, sql: str, params=None) -> list[Any]:
        return list(self.rows)

    def last_insert_id(self) -> int:
        return 41


class _UniqueCodeService:
    """处理器：INSERT 一条同编码的行（成功后再由不变量查回自己）。"""

    def create(
        self,
        tx: Any,
        ctx: Any,
        scope: Any,
        code: f_str("编号", required=True, max_length=64),
    ) -> Any:
        from yuxin.kernel.capability import HandlerResult

        ctx.require("ut.unique.create")
        tx.execute("INSERT INTO ut_uniques (code) VALUES (%s)", (code,))
        return HandlerResult(
            data={"row": {"id": 41, "code": code}}, resource_id=41
        )

    @staticmethod
    def load_for_audit(tx: Any, *, scope: Any, record_id: int) -> dict[str, Any]:
        return {"id": record_id, "code": "UT-1"}


def _run_unique_code_capability(*, rows: list[dict]) -> Any:
    """跑一次真实的执行器流程（处理器先写、不变量后查），返回 InvocationResult。"""
    from yuxin.kernel.capability import (
        AgentExposure,
        Capability,
        Confirmation,
        HttpMethod,
        Registry,
        Risk,
    )
    from yuxin.kernel.idempotency import IdempotencyStore
    from yuxin.kernel.invariants import UniqueCode as _UniqueCode
    from yuxin.kernel.runner import ActorView, CapabilityRunner, Invocation

    _UniqueCodeService.create.__yuxin_load_by_id__ = _UniqueCodeService.load_for_audit  # type: ignore[attr-defined]

    registry = Registry()
    registry.register(
        Capability(
            name="ut.unique.create",
            title="唯一编码端到端探针",
            domain="ut",
            resource="ut_pond",
            handler=_UniqueCodeService.create,
            service_factory=_UniqueCodeService,
            method=HttpMethod.POST,
            path="/api/v1/ut-uniques",
            kind="create",
            required_permission="ut.unique.create",
            risk=Risk.NORMAL,
            confirmation=Confirmation.NEVER,
            agent_exposure=AgentExposure.HIDDEN,
            invariants=(
                _UniqueCode(fields=("code",), scope=(), table="ut_uniques"),
            ),
        )
    )

    tx = _InMemoryTx(rows)

    class _Uow:
        def begin(self) -> Any:
            from contextlib import contextmanager

            @contextmanager
            def _scope() -> Any:
                yield tx

            return _scope()

    runner = CapabilityRunner(
        registry=registry,
        uow_factory=_Uow,
        audit=_AuditWriter(),
        idempotency=IdempotencyStore(lambda: _Uow()),
        scope_resolver=_FixedScopeResolver(),
    )
    actor = ActorView(
        user_id=9,
        username="ut",
        permissions=frozenset({"ut.unique.create"}),
        role_codes=frozenset(),
    )
    return runner.invoke(
        Invocation(capability_name="ut.unique.create", payload={"code": "UT-41"}),
        actor,
        "req-ut-unique",
    )


def test_executor_injection_lets_unique_code_pass_on_a_fresh_create() -> None:
    """干净表 + 真正的"先写后查"：唯一性规则必须**放行**（否则所有 `*.create` 都 409）。"""
    result = _run_unique_code_capability(rows=[])
    assert result.kind == "executed"
    assert result.resource_id == 41


def test_executor_injection_still_fails_on_a_genuine_duplicate() -> None:
    """表里已有一行**同编码**（id 与本次不同）：仍然必须拒绝。"""
    # 直接断言不变量层：排除掉本次那一行（id=41）之后，仍有 id=12 那一行同编码
    tx = _InMemoryTx([{"id": 12, "code": "UT-41"}])
    rejects(
        UniqueCode(fields=("code",), scope=(), table="ut_uniques"),
        code="CONFLICT",
        tx=tx,
        payload={"_resource_id": 41, "_invariant_exclude_id": 41, "code": "UT-41"},
        before=None,
    )

def test_no_overlapping_source_refuses_to_widen_without_tenant_key() -> None:
    """**缺分租键 → `INTERNAL_ERROR`**，绝不退化成全表查。

    缺陷形态（复核人 实测、负责人 定为优先项）：分租键以前是"能取到就加、
    取不到就跳过"，于是 `organization_id` 缺失时 SQL 的租户条件**静默消失** ——
    查重范围**扩到其他企业**。它是全部规则里**唯一一条"缺键后方向变宽"**的，
    其余规则缺键最坏是"没拦住"，它是"拦错了范围"（租户隔离缺陷）。
    """
    tx = FakeUnitOfWork()
    error = rejects(
        _no_overlap(),
        code="INTERNAL_ERROR",
        tx=tx,
        payload={
            "source_type": "manual_expense",
            "target_type": "pond",
            "target_id": 4,
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            # 故意不给 organization_id
        },
    )
    assert error.data is not None
    assert error.data["missing_tenant_key"] == "organization_id"
    assert tx.queries == [], "缺分租键时不该发出任何查询（宁可不查，也不扩大范围）"


def test_no_overlapping_source_declines_empty_tenant_keys() -> None:
    """`tenant_keys=()` → 构造期拒绝：不留"声明成空就能全表查"的后门。"""
    with pytest.raises(ValueError):
        NoOverlappingSource(table="cost_entries", tenant_keys=())


# ---------------------------------------------------------------------------
# `NoOverlappingSource` 的**跨来源类型**语义（registry §4 #8，2026-09-13 收口）
#
# 契约口径：本规则拦的是"**同一笔**业务事实被两种来源各记一次"
#   （手工费用 `manual_expense` vs 库存自动归集 `warehouse_ledger`），
# **不是**"同来源类型不得两笔"。判据来自两处可核查的来源：
#   1. 早期实现 `require_source_not_duplicated()` 的触发条件明确是**跨类型**
#      （"本次是 manual_expense 且已有 warehouse_ledger 来源"）；
#   2. `004_cost.sql` 的物理去重键 `uq_cost_entries_org_dedupe` 走的生成列
#      `ledger_dedupe_key` **含 `source_ref`** —— DB 层本来就允许"两笔库存单各自归集"。
#
# 下面两条是**双向反例**：同类型/不同 source_ref 必须放行；跨类型同期必须拒。
# 两条都在，才说明谓词里的 `source_type <> 本次来源类型` 既没多拦、也没少拦。
# ---------------------------------------------------------------------------


def _no_overlap_ledger_side() -> NoOverlappingSource:
    """cost #8 的"库存归集"那一侧真实声明形态（两个方向各一个实例）。"""
    return NoOverlappingSource(
        table="cost_entries",
        source_types=("manual_expense",),
        target_fields=("target_type", "target_id"),
        period_fields=("period_start", "period_end"),
        tenant_keys=("organization_id", "farm_id", "area_id"),
        label="该塘口/批次",
    )


def _ledger_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_type": "warehouse_ledger",
        "target_type": "pond",
        "target_id": 7,
        "period_start": "2026-09-01",
        "period_end": "2026-09-30",
        "organization_id": 1,
        "farm_id": 1,
        "area_id": 1,
    }
    payload.update(overrides)
    return payload


def test_no_overlapping_source_allows_a_second_row_of_the_same_source_type() -> None:
    """**(a) 同来源类型、不同 `source_ref` 的第二笔必须被放行。**

    这是本次收口的那条：原先谓词里没有 `source_type <> 本次来源类型`，
    第二次库存归集会命中上一笔（库里确实有那一行）而被误拒。
    判据同时看**谓词**（必须带 `<>`）与**结论**（必须放行）。
    """
    tx = FakeUnitOfWork()
    check(_no_overlap_ledger_side(), tx=tx, payload=_ledger_payload())
    sql, params = tx.queries[-1]
    assert "source_type <> %s" in sql, (
        f"谓词里没有『排除本次来源类型』这一条 —— 同来源类型的第二笔会被误拒。SQL={sql}"
    )
    assert "warehouse_ledger" in params, params


def test_no_overlapping_source_rejects_a_different_source_type_in_the_same_period() -> None:
    """**(b) 跨来源类型、同期必须被拒**（把"收口"钉在不放水的一侧）。

    构造：本次是 `warehouse_ledger`，而库里已有一行 `manual_expense`（禁止集合内的类型）。
    """
    tx = FakeUnitOfWork(("FROM cost_entries", {"id": 42}))
    error = rejects(
        _no_overlap_ledger_side(),
        code="CONFLICT",
        tx=tx,
        payload=_ledger_payload(),
    )
    assert error.data is not None and error.data["rule"] == "NO_OVERLAPPING_SOURCE"


def test_no_overlapping_source_keeps_cross_type_check_when_source_type_is_unknown() -> None:
    """取不到本次来源类型时**不加** `<>` 条件 —— 方向仍是"宁可多拦"。

    为什么不静默放宽：缺一个上下文就关掉跨类型检查，等于让"忘了回传"变成
    "这条规则在本次不生效"，而那种失效从外面看不出来。
    """
    tx = FakeUnitOfWork()
    payload = _ledger_payload()
    payload.pop("source_type")
    check(_no_overlap_ledger_side(), tx=tx, payload=payload)
    sql = tx.queries[-1][0]
    assert "source_type <> %s" not in sql, (
        f"取不到本次来源类型时不该拼 `<>` 条件（否则参数与占位符会错位）。SQL={sql}"
    )




# ---------------------------------------------------------------------------
# `PeriodOpen`：同一病的第四次（谓词里没有租户键 + `LIMIT 1` 没有 `ORDER BY`）
#
# 缺陷形态（**已修**，下面 8 条测试就是它的回归守卫）：`accounting_periods` 的
# `organization_id` 是 NOT NULL —— **每个企业各有一份自己的期间行**。而内核的反查
# 原先只按日期区间取行，于是命中的是"任一企业的期间"：
#   * 命中邻家已关账的期间 → **误拦**本企业的合法写入（吵闹）
#   * 命中邻家未关账的期间 → **静默放行**本企业已关账期间的写入（安静，更危险）
# 这与 `NoOverlappingSource` 的"缺键后方向变宽"是同一族，修法也照抄那一条。
#
# 关于"证据在哪"：本节的断言**直接对实现取行为**（`FakeUnitOfWork` 记录 SQL 与参数），
# 不引用任何外部脚本的输出 —— 修复前的双向复现记录在
# `docs/CAPABILITY_REGISTRY.md` §4.1 第四类里；独立验证者的探针在
# `.tmp-verify-t4/probe_periodopen_tenant.py`（已双向验证：修复前红 / 修复后 8/8 绿）。
# **不要**把 `tools/repro_period_open_tenant.py` 当"缺陷存在"的证据：它在修复后
# 已经变成回归守卫（全绿），引用它去证明"有缺陷"会得到相反结论。
# ---------------------------------------------------------------------------


def _period_open() -> Any:
    from yuxin.kernel.invariants import PeriodOpen

    return PeriodOpen(date_field="occurred_on")


def test_period_open_puts_the_tenant_key_in_the_predicate() -> None:
    """租户键必须出现在 SQL 与参数里 —— 不是注释里，是**会执行的那段**。"""
    tx = FakeUnitOfWork(("FROM accounting_periods", {"status": "open"}))
    check(
        _period_open(),
        tx=tx,
        payload={"occurred_on": "2027-01-15", "organization_id": 9},
    )
    sql, params = tx.queries[-1]
    assert "organization_id=%s" in sql, f"期间反查没有租户键：{sql}"
    assert params[:3] == [9, "2027-01-15", "2027-01-15"], params
    assert "ORDER BY" in sql, (
        f"`LIMIT 1` 不带 `ORDER BY`：命中哪一行由存储顺序决定，结论不可复现。SQL={sql}"
    )


def test_period_open_refuses_to_query_without_a_tenant_key() -> None:
    """缺 `organization_id` → `INTERNAL_ERROR`，且**不发查询**。

    为什么不降级成"不带租户键也查"：那正是跨企业串号的实现方式；
    为什么不降级成"跳过"：那会让这条规则退化成建议（当前有 7 条能力挂它）。
    按 R2 三分法 —— "有没有发生日期"是业务事实（缺则跳过）、
    "能不能把规则跑起来"是基础设施（缺则报错）。
    """
    tx = FakeUnitOfWork()
    error = rejects(
        _period_open(),
        code="INTERNAL_ERROR",
        tx=tx,
        payload={"occurred_on": "2027-01-15"},
    )
    assert error.data is not None
    assert error.data["missing_tenant_key"] == "organization_id"
    assert tx.queries == [], "缺租户键时不该发出任何查询（宁可不查，也不跨企业查）"


def test_period_open_skips_when_no_date_was_submitted() -> None:
    """没有发生日期 = 业务事实缺失 → 跳过，且**不因缺租户键而报错**。

    顺序是刻意的：先看"这条规则适不适用于本次操作"，再看"能不能把它跑起来"。
    反过来的话，每个新建能力都会因为少一个租户键而被一条不相干的规则拒掉
    （`test_every_invariant_is_inert_when_nothing_was_submitted` 守的就是这件事）。
    """
    tx = FakeUnitOfWork()
    check(_period_open(), tx=tx, payload={}, before=None)
    assert tx.queries == []


def test_period_open_reads_the_tenant_key_from_before_as_well() -> None:
    """更新场景下服务不必重复回传：`before` 那一行本来就带 `organization_id`。"""
    tx = FakeUnitOfWork(("FROM accounting_periods", {"status": "open"}))
    check(
        _period_open(),
        tx=tx,
        payload={"occurred_on": "2027-01-15"},
        before={"organization_id": 4, "occurred_on": "2027-01-15"},
    )
    assert tx.queries[-1][1][0] == 4


def test_period_open_rejects_closed_and_passes_open() -> None:
    """两侧路径：`closed` → `CONFLICT`（带 `PERIOD_CLOSED`）；`open` → 放行。"""
    closed = rejects(
        _period_open(),
        code="CONFLICT",
        tx=FakeUnitOfWork(("FROM accounting_periods", {"status": "closed"})),
        payload={"occurred_on": "2027-01-15", "organization_id": 1},
    )
    assert closed.data is not None and closed.data["rule"] == "PERIOD_CLOSED"
    check(
        _period_open(),
        tx=FakeUnitOfWork(("FROM accounting_periods", {"status": "open"})),
        payload={"occurred_on": "2027-01-15", "organization_id": 1},
    )


def test_period_open_passes_when_this_tenant_has_no_period_row() -> None:
    """本企业没有登记该期间 → 放行（期间管理未启用，不拦）。

    "查不到"必须按**本企业**判：修好租户键之后，邻家有没有期间行与本次结论无关。
    """
    check(
        _period_open(),
        tx=FakeUnitOfWork(default=None),
        payload={"occurred_on": "2027-01-15", "organization_id": 1},
    )


def test_period_open_declines_empty_tenant_keys() -> None:
    """`tenant_keys=()` → 构造期拒绝（与 `NoOverlappingSource` 同一道门）。"""
    from yuxin.kernel.invariants import PeriodOpen

    with pytest.raises(ValueError):
        PeriodOpen(tenant_keys=())


def test_period_open_never_ships_an_unordered_limited_query() -> None:
    """结构守卫：`tenant_keys` 与 `ORDER BY` 不能从实现里静默消失（读源码，不依赖替身）。

    与上面的 SQL 断言重复是**刻意的**：上面断言"这一次的 SQL 长这样"，这里断言
    "实现里还有这两件事"。本项目已发生过两次"修正静默消失"，而没有 MySQL 时
    静态源码守卫是唯一抓得住它的手段。
    """
    from yuxin.kernel.invariants import PeriodOpen

    source = inspect.getsource(PeriodOpen.check)
    assert "tenant_keys" in source, "PeriodOpen 不再读 tenant_keys（租户键可能被撤回）"
    assert "ORDER BY" in source, "PeriodOpen 的 LIMIT 1 又回到了无确定行序的形态"

def test_cumulative_within_requires_new_row_counted() -> None:
    """**忘选 = 构造期报错**（R1 Amendment 的核心：不给默认值）。

    缺陷形态：核验类与登记类在"本次行算不算在内"上语义**相反**，一个默认值会让
    忘选的那条能力静默用错判定 —— 与 `fields=` 忘传、`_ledger_lines` 忘传同族。
    """
    with pytest.raises(ValueError):
        CumulativeWithin(
            target_table="sales_orders",
            target_dims=("sales_order_id",),
            children_table="deliveries",
        )

# ---------------------------------------------------------------------------
# 自排除下推的**结构守卫**（读源码，不依赖替身）
#
# 背景：`cost-dev` 报"我下推到 SQL 的 `id <> %s` 没被采纳（两处仍是 `_is_self_match` +
# `LIMIT 1`）"，而内核侧实测是"已下推"。两个成员对同一份代码给出相反读数时，唯一能
# 判定的是**重新读一遍盘** —— 这条测试就是把那次"读盘"固化下来，防止它在后续编辑里
# 静默消失（本项目已发生过两次"其中一份静默消失"）。
# ---------------------------------------------------------------------------


def test_self_exclusion_is_pushed_down_in_source_for_all_three_types() -> None:
    """`UniqueCode` / `NoOverlappingSource` / `AtMostOnePending` 必须都按 `key_column` 拼 `<>`。"""
    for cls in (UniqueCode, NoOverlappingSource, AtMostOnePending):
        source = inspect.getsource(cls.check)
        assert "key_column" in source, f"{cls.__name__} 不再读 key_column（下推可能被撤回）"
        assert "<> %s" in source, f"{cls.__name__} 的 SQL 不再拼 `<> %s`（自排除被撤回）"
        assert "LIMIT 1" in source, (
            f"{cls.__name__} 的 `LIMIT 1` 消失了 —— 若改成取集合，请同步更新本守卫的意图说明"
        )


def test_cumulative_within_excludes_only_on_the_register_path() -> None:
    """`CumulativeWithin` 只在**登记类**（`new_row_counted=False`）拼 `<> %s`。

    R1 Amendment 的字面：核验类"只判 `counted`"，`counted` 要如实含本次那一行，
    因此核验路径**不排除**；登记路径带它（那里恒等，作误用兜底）。
    """
    source = inspect.getsource(CumulativeWithin.check)
    assert "new_row_counted" in source and "<> %s" in source
    assert "if self.new_row_counted else" in source, (
        "排除条件不再与 new_row_counted 绑定 —— 请核对是否偏离 R1 Amendment 的字面"
    )
