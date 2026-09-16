from __future__ import annotations

from types import SimpleNamespace


def test_warehouse_verified_rows_expose_edit_and_archive_actions():
    from yuxin.domains.warehouse.service import WAREHOUSE_WORKFLOW
    from yuxin.kernel.workflow import RowAction

    actions = WAREHOUSE_WORKFLOW.allowed_actions("verified")
    assert RowAction.VIEW in actions
    assert RowAction.EDIT in actions
    assert RowAction.ARCHIVE in actions


def test_final_receipt_states_are_accepted_by_post_write_invariants():
    from yuxin.domains.sales.capabilities import _amount_within
    from yuxin.domains.warehouse.capabilities import _register_all as register_warehouse
    from yuxin.kernel.capability import REGISTRY
    from yuxin.kernel.invariants import ReferencedStatus

    register_warehouse()
    receipt = REGISTRY.get("receipt.verify")
    referenced = next(item for item in receipt.invariants if isinstance(item, ReferencedStatus))
    assert "fully_received" in referenced.statuses
    assert "settled" not in _amount_within().statuses
    assert "settled" in next(
        item for item in REGISTRY.get("sales_receipt.verify").invariants
        if item.__class__.__name__ == "AmountWithin"
    ).statuses


def test_path_parameter_is_passed_to_handler_and_wins_over_body():
    from yuxin.kernel.capability import Capability, HandlerResult, Registry, Risk
    from yuxin.kernel.runner import ActorView, CapabilityRunner, Invocation
    from yuxin.kernel.scope import Scope
    from yuxin.kernel.uow import UnitOfWork

    seen = {}

    class Handler:
        def handle(self, tx, ctx, scope, area_id):
            seen["area_id"] = area_id
            return HandlerResult(data={"area_id": area_id})

    registry = Registry()
    registry.register(
        Capability(
            name="test.path",
            title="路径参数测试",
            domain="test",
            method="POST",
            path="/test/{area_id}",
            kind="action",
            handler=Handler.handle,
            service_factory=Handler,
            risk=Risk.NORMAL,
        )
    )
    runner = CapabilityRunner(
        registry=registry,
        uow_factory=lambda: UnitOfWork,
        audit=None,
        idempotency=None,
        scope_resolver=None,
    )
    result = runner._call_service(
        registry.get("test.path"),
        tx=object(),
        actor=ActorView(user_id=1, username="u", permissions=frozenset()),
        scope=Scope.all_data(user_id=1),
        invocation=Invocation("test.path", payload={"area_id": 99}, path_params={"area_id": 7}),
        cleaned={"area_id": 99},
        request_id="req",
    )
    assert seen["area_id"] == 7
    assert result.data["area_id"] == 7


def test_access_grants_use_reference_fields_and_workbench_is_registered():
    import yuxin.bootstrap as bootstrap

    registry = bootstrap.load_all()
    create_user = registry.get("access.user.create")
    assert create_user.fields["role_ids"].ref is not None
    assert create_user.fields["scope_ids"].ref is not None
    assert registry.find("access.role.list") is not None
    assert registry.find("access.scope.list") is not None
    assert registry.find("work_item.list") is not None


def test_new_materials_start_as_draft_until_verified(monkeypatch):
    from yuxin.domains.master_data import masterdata_write
    from yuxin.domains.master_data.masterdata_write import MaterialWriteService
    from yuxin.kernel.scope import Scope

    class Tx:
        def __init__(self):
            self.sql = ""

        def execute(self, sql, *_args, **_kwargs):
            self.sql = sql
            return 1

        def last_insert_id(self):
            return 42

    tx = Tx()
    ctx = SimpleNamespace(
        actor=SimpleNamespace(user_id=7, permissions=frozenset()),
        require=lambda _permission: None,
    )
    monkeypatch.setattr(
        masterdata_write,
        "tenant_keys_for_create",
        lambda _tx, _scope: {"organization_id": 1, "farm_id": 2, "area_id": 3},
    )
    service = MaterialWriteService()
    monkeypatch.setattr(
        service,
        "load_material",
        lambda _tx, *, scope, record_id: {
            "id": record_id,
            "organization_id": 1,
            "farm_id": 2,
            "area_id": 3,
                "status": "draft",
            "row_version": 1,
            "name": "测试物料",
        },
    )
    service.create_material(
        tx,
        ctx,
        Scope.all_data(user_id=7),
        "MAT-REG",
        "测试物料",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    assert "'draft'" in tx.sql

def test_warehouse_create_returns_decorated_record(monkeypatch):
    from yuxin.domains.warehouse import warehouse_write
    from yuxin.domains.warehouse.warehouse_write import WarehouseWriteService
    from yuxin.kernel.scope import Scope

    class Tx:
        def execute(self, *_args, **_kwargs):
            return 1

        def last_insert_id(self):
            return 42

    ctx = SimpleNamespace(
        actor=SimpleNamespace(user_id=7, permissions=frozenset()),
        require=lambda _permission: None,
    )
    monkeypatch.setattr(
        warehouse_write,
        "tenant_keys_for_create",
        lambda _tx, _scope: {"organization_id": 1, "farm_id": 2, "area_id": 3},
    )
    service = WarehouseWriteService()
    monkeypatch.setattr(
        service,
        "load_warehouse",
        lambda _tx, *, scope, record_id: {
            "id": record_id,
            "farm_id": 2,
            "status": "verified",
            "row_version": 1,
            "name": "测试仓",
        },
    )

    result = service.create_warehouse(
        Tx(),
        ctx,
        Scope.all_data(user_id=7),
        "QA-REG",
        "测试仓",
        None,
        None,
        None,
    )

    assert result.resource_id == 42
    assert result.data["record"]["id"] == 42


def test_flask_path_keeps_non_numeric_parameters_as_strings():
    from yuxin.web.app import _flask_path

    assert _flask_path("/api/v1/cost/periods/{period}/close") == "/api/v1/cost/periods/<period>/close"
    assert _flask_path("/api/v1/areas/{area_id}") == "/api/v1/areas/<int:area_id>"
