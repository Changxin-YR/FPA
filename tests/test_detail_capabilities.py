"""`*.get` 详情读能力（t7）：核心业务详情资源。

## 这个文件补的是哪一类缺口

`purchase_orders` / `sales_orders` 是列表型资源，但 registry §1.7 / §1.8 里**没有**
详情读能力，于是前端"点开一行看详情"只能复用列表响应 —— 代价是拿不到
`available_transitions`（从当前状态出发的合法目标），而那正是"服务端按转移表过滤
候选值"的运行时形态。

两个域的 `get_order_by_id` 处理器**早就实现了**（含 `available_transitions` 与第三层
范围校验），只是刻意没注册：理由是「69 条是 t9 的验收基线，不该由某个域单独改数」。
当前迭代（`ROADMAP.md` §4.2 的 P1「详情页」）把缺口本身当成任务，因此那条基线被**显式
解除**，两条能力登记进 registry 与本文件。

## 为什么断言这四件事

* **形状**：`path` / `kind` / `required_permission` / `scope` 是派生「路由 / 权限 /
  DataScope 谓词」的唯一来源，写错一格就是一个"看起来能用、其实越权或 404"的端点。
* **处理器签名**：执行器按处理器签名裁剪入参（`runner._call_service` 的
  `optional in accepts`），签名里没有 `order_id` 会表现为"路径参数明明给了、服务却说
  没给"——而那是个会被误读成"路由没接上"的错误。
* **`available_transitions`**：这是**详情存在的理由**。没有它，详情页还不如复用列表
  响应 —— 那正是原缺口描述的状态。
* **范围外必须拒绝**：详情是最容易泄数据的地方。列表有 WHERE 兜着，详情只有主键。
  一个"详情能跨范围读到"的端点，等于把列表的范围防御整条绕过去。

连不上真库时**显式 skip**（本仓既有口径），不静默通过。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import load_all_status
from fpa.kernel.scope import Scope

DETAILS: dict[str, dict[str, str]] = {
    "pond.get": {
        "resource": "pond",
        "path": "/api/v1/ponds/{pond_id}",
        "permission": "pond.view",
        "table": "ponds",
        "handler_path": "fpa.domains.master_data.ponds:PondService.get_pond_by_id",
        "path_param": "pond_id",
        "available_transitions": True,
    },
    "purchase_order.get": {
        "resource": "purchase_order",
        "path": "/api/v1/purchase-orders/{order_id}",
        "permission": "purchase.view",
        "table": "purchase_orders",
        "handler_path": "fpa.domains.purchase.orders:PurchaseOrderService.get_order_by_id",
        "available_transitions": True,
    },
    "sales_order.get": {
        "resource": "sales_order",
        "path": "/api/v1/sales-orders/{order_id}",
        "permission": "sales.view",
        "table": "sales_orders",
        "handler_path": "fpa.domains.sales.sales_orders:SalesOrderService.get_order_by_id",
        "available_transitions": True,
    },
    "feeding.get": {
        "resource": "feeding",
        "path": "/api/v1/feedings/{feeding_id}",
        "permission": "feeding.view",
        "table": "feedings",
        "handler_path": "fpa.domains.production.read:FeedingService.get_feeding_by_id",
        "path_param": "feeding_id",
    },
    "harvest.get": {
        "resource": "harvest",
        "path": "/api/v1/harvests/{harvest_id}",
        "permission": "harvest.view",
        "table": "harvests",
        "handler_path": "fpa.domains.production.read:HarvestService.get_harvest_by_id",
        "path_param": "harvest_id",
    },
    "delivery.get": {
        "resource": "delivery",
        "path": "/api/v1/deliveries/{delivery_id}",
        "permission": "sales.view",
        "table": "deliveries",
        "handler_path": "fpa.domains.sales.sales_orders:DeliveryService.get_delivery_by_id",
        "path_param": "delivery_id",
    },
    "receivable.get": {
        "resource": "receivable",
        "path": "/api/v1/receivables/{receivable_id}",
        "permission": "finance.receivable.view",
        "table": "receivables",
        "handler_path": "fpa.domains.sales.sales_orders:ReceivableService.get_receivable_by_id",
        "path_param": "receivable_id",
    },
    "payment.get": {
        "resource": "purchase_payment",
        "path": "/api/v1/payments/{payment_id}",
        "permission": "finance.payment.view",
        "table": "purchase_payments",
        "handler_path": "fpa.domains.purchase.payments:PurchasePaymentService.get_payment_by_id",
        "path_param": "payment_id",
    },
}


def test_dictionary_detail_does_not_trigger_double_record_envelope() -> None:
    from fpa.domains.master_data.resources_read import MaterialService

    class Service(MaterialService):
        @classmethod
        def _scope_row(cls, tx, scope, record_id):
            return {"id": record_id, "code": "M-1", "name": "饲料", "status": "verified", "row_version": 1}

        @classmethod
        def _decorate(cls, row, scope, permissions):
            return dict(row)

    ctx = SimpleNamespace(
        actor=SimpleNamespace(permissions=frozenset()),
        require=lambda _permission: None,
    )
    result = Service().get_row_by_id(
        SimpleNamespace(), ctx, Scope.all_data(user_id=1), {"material_id": 7}
    )

    assert result.data == {"record": {"id": 7, "code": "M-1", "name": "饲料", "status": "verified", "row_version": 1}}
    assert result.resource_id is None


# ---------------------------------------------------------------------------
# 1) 声明层（不需要数据库）
# ---------------------------------------------------------------------------


def _capability(name: str):
    load_all_status()
    from fpa.kernel.capability import REGISTRY

    found = REGISTRY.find(name)
    assert found is not None, f"{name} 未注册 —— 详情页拿不到数据（这就是原缺口）"
    return found


@pytest.mark.parametrize("name", sorted(DETAILS))
def test_详情能力的形状与登记表一致(name: str) -> None:
    expected = DETAILS[name]
    capability = _capability(name)

    assert str(capability.method) == "GET", capability.method
    assert capability.path == expected["path"]
    # `path_parameters` 从 path 模板派生；没有它 Web 层不会把它当路径参数。
    assert capability.path_parameters == (expected.get("path_param", "order_id"),), capability.path_parameters
    assert capability.kind == "read", capability.kind
    assert capability.resource == expected["resource"], capability.resource
    assert capability.required_permission == expected["permission"], capability.required_permission
    # 详情不是"全场可见"：它必须带着范围策略，否则第三层就只剩权限码。
    # `constrained` 是 property（不是方法）—— `ScopePolicy.constrained` 的定义处如此。
    assert capability.scope.constrained, "详情能力没有声明 scope —— 范围校验没有依据"


@pytest.mark.parametrize("name", sorted(DETAILS))
def test_详情能力挂在真实处理器上且签名收得到路径参数(name: str) -> None:
    """处理器必须是真方法、且签名里有 `path_params`。

    签名断言不是形式主义：执行器按签名裁剪入参，签名错了会表现成
    "路径参数丢了"，而不是"方法写错了"。
    """
    module_name, function_name = DETAILS[name]["handler_path"].split(":")
    owner_name, method_name = function_name.split(".")
    module = __import__(module_name, fromlist=[owner_name])
    owner = getattr(module, owner_name)
    declared = getattr(owner, method_name)

    capability = _capability(name)
    assert capability.handler is declared, (
        f"{name} 的处理器不是 {DETAILS[name]['handler_path']}：{capability.handler!r}"
    )

    parameters = inspect.signature(declared).parameters
    # ★ 读能力的路径参数契约：执行器在 `spec.is_read` 时把 `cleaned` 置空
    #   （`runner.py::invoke`），路径参数**只有**声明了 `path_params` 才会传进来；
    #   单独的资源 ID 形参只能作为兼容旧 Service 调用的可选参数。
    assert "path_params" in parameters, (
        f"{name} 的处理器必须声明 `path_params`（读能力的路径参数以 dict 传入）：{list(parameters)}"
    )
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()), (
        f"{name} 的处理器没有 `**_`：执行器会传它不认识的 query 参数"
    )


# ---------------------------------------------------------------------------
# 2) 真库（连不上就 skip）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_db_ready() -> bool:
    from fpa.kernel.uow_factory import uow_factory

    try:
        with uow_factory().begin() as tx:
            tx.query_one("SELECT 1 AS ok")
    except Exception as error:  # noqa: BLE001 - 连不上就是 skip，不是通过
        pytest.skip(f"连不上真实 MySQL，本组断言无法执行（不是通过）：{error}")
    return True


def _open_tx():
    from fpa.kernel.uow_factory import uow_factory

    return uow_factory().begin()


def _context(permissions: frozenset[str], scope: Any):
    from fpa.domains._base import Actor, ServiceContext

    return ServiceContext(
        actor=Actor(
            user_id=scope.user_id,
            username="t7_detail_probe",
            permissions=permissions,
            scope=scope,
        ),
        request_id="t7-detail-probe",
    )


def _row_or_skip(tx: Any, table: str) -> dict[str, Any]:
    row = tx.query_one(
        f"SELECT id, organization_id, farm_id, area_id FROM {table} ORDER BY id DESC LIMIT 1"
    )
    if row is None:
        pytest.skip(f"{table} 里一行数据都没有，详情契约无法在真库上验证（不是通过）")
    return dict(row)


def _farm_scope(*, user_id: int, farm_id: int):
    from fpa.kernel.scope import Scope

    return Scope.from_rows(
        [{"scope_type": "farm", "farm_id": farm_id, "code": "t7-probe"}], user_id=user_id
    )


@pytest.mark.parametrize("name", sorted(DETAILS))
def test_不存在的记录返回_NOT_FOUND_而不是空对象(name: str, real_db_ready: bool) -> None:
    from fpa.kernel.errors import DomainError, ErrorCode

    spec = DETAILS[name]
    with _open_tx() as tx:
        service = _service_for(name)
        with pytest.raises(DomainError) as excinfo:
            _invoke_detail(
                tx,
                _context(frozenset({spec["permission"]}), _farm_scope(user_id=1, farm_id=1)),
                _farm_scope(user_id=1, farm_id=1),
                name,
                path_params={spec.get("path_param", "order_id"): 2**53},
            )
    assert excinfo.value.code is ErrorCode.NOT_FOUND, excinfo.value.code


@pytest.mark.parametrize("name", sorted(DETAILS))
def test_范围外的记录必须被拒绝(name: str, real_db_ready: bool) -> None:
    """**这条是本文件里最重要的一条。**

    列表接口有 `WHERE` 兜着范围；详情只有主键 —— 所以详情是最容易泄数据的地方。
    构造一个"农场级范围指向别家农场"的账号，去读本组织的一行：必须拒绝。
    """
    from fpa.kernel.errors import DomainError, ErrorCode

    spec = DETAILS[name]
    with _open_tx() as tx:
        row = _row_or_skip(tx, spec["table"])
        scope = _farm_scope(user_id=row["organization_id"], farm_id=int(row["farm_id"]) + 999_999)
        service = _service_for(name)
        with pytest.raises(DomainError) as excinfo:
            _invoke_detail(
                tx,
                _context(frozenset({spec["permission"]}), scope),
                scope,
                name,
                path_params={spec.get("path_param", "order_id"): int(row["id"])},
            )
    assert excinfo.value.code is ErrorCode.DATA_SCOPE_DENIED, excinfo.value.code


@pytest.mark.parametrize(
    "name", sorted(name for name, spec in DETAILS.items() if spec.get("available_transitions"))
)
def test_详情返回_available_transitions(name: str, real_db_ready: bool) -> None:
    """详情必须给出"从当前状态出发的合法目标"——这正是详情存在的理由。"""
    spec = DETAILS[name]
    with _open_tx() as tx:
        row = _row_or_skip(tx, spec["table"])
        scope = _farm_scope(user_id=row["organization_id"], farm_id=int(row["farm_id"]))
        result = _invoke_detail(
            tx,
            _context(frozenset({spec["permission"]}), scope),
            scope,
            name,
            path_params={spec.get("path_param", "order_id"): int(row["id"])},
        )

    record = result.data["record"]
    assert "available_transitions" in record, sorted(record)
    assert isinstance(record["available_transitions"], list)
    # 空列表是合法的（终态：没有下一步），但**每个元素**必须是 value/label 形状。
    for item in record["available_transitions"]:
        assert set(item) == {"value", "label"}, item


def _service_for(name: str):
    if name == "pond.get":
        from fpa.domains.master_data.ponds import PondService

        return PondService()
    if name == "purchase_order.get":
        from fpa.domains.purchase.orders import PurchaseOrderService

        return PurchaseOrderService()
    if name == "payment.get":
        from fpa.domains.purchase.payments import PurchasePaymentService

        return PurchasePaymentService()
    if name in {"feeding.get", "harvest.get"}:
        from fpa.domains.production.read import FeedingService, HarvestService

        return FeedingService() if name == "feeding.get" else HarvestService()
    from fpa.domains.sales.sales_orders import (
        DeliveryService,
        ReceivableService,
        SalesOrderService,
    )

    if name == "delivery.get":
        return DeliveryService()
    if name == "receivable.get":
        return ReceivableService()
    return SalesOrderService()


def _invoke_detail(tx: Any, ctx: Any, scope: Any, name: str, *, path_params: dict[str, Any]):
    service = _service_for(name)
    method_name = {
        "pond.get": "get_pond_by_id",
        "purchase_order.get": "get_order_by_id",
        "sales_order.get": "get_order_by_id",
        "feeding.get": "get_feeding_by_id",
        "harvest.get": "get_harvest_by_id",
        "delivery.get": "get_delivery_by_id",
        "receivable.get": "get_receivable_by_id",
        "payment.get": "get_payment_by_id",
    }[name]
    return getattr(service, method_name)(tx, ctx, scope, path_params=path_params)
