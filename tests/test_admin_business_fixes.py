from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fpa.domains.access.admin import AdminService
from fpa.domains.audit.audit_logs import AuditLogService
from fpa.kernel.runner import ActorView
from fpa.kernel.scope import Scope


def _ctx(*, user_id: int = 1, super_admin: bool = False, permissions: set[str] | None = None):
    actor = ActorView(
        user_id=user_id,
        username="admin",
        permissions=frozenset(permissions or {"auth.user.manage", "auth.role.manage"}),
        role_codes=frozenset({"super_admin"}) if super_admin else frozenset({"demo_admin"}),
    )
    return SimpleNamespace(actor=actor, require=lambda permission: actor.has(permission))


class _AdminListTx:
    def query_scalar(self, sql: str, params: Any = None) -> int:
        return 2

    def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "FROM users AS u" in sql:
            return [
                {"id": 1, "username": "admin", "display_name": "管理员", "status": "active", "row_version": 1},
                {"id": 2, "username": "worker", "display_name": "员工", "status": "disabled", "row_version": 2},
            ]
        if "FROM user_roles" in sql or "FROM user_data_scopes" in sql:
            return []
        if "FROM roles" in sql:
            return [{"id": 3, "code": "operator", "name": "业务员", "description": "", "status": "active"}]
        if "FROM data_scopes" in sql:
            return [
                {
                    "id": 4,
                    "code": "area-east",
                    "name": "东区数据",
                    "scope_type": "area",
                    "status": "active",
                    "area_id": 8,
                    "area_name": "东区",
                },
                {
                    "id": 5,
                    "code": "personal-default",
                    "name": "个人数据",
                    "scope_type": "personal",
                    "status": "active",
                },
            ]
        return []


def test_account_actions_match_the_authenticated_administrator() -> None:
    service = AdminService()
    scope = Scope.all_data(user_id=1)

    regular = service.list_users(_AdminListTx(), _ctx(), scope).data["items"]
    assert regular[0]["allowed_actions"] == ["view"]
    assert regular[1]["allowed_actions"] == ["view", "status"]

    super_rows = service.list_users(_AdminListTx(), _ctx(super_admin=True), scope).data["items"]
    assert super_rows[0]["allowed_actions"] == ["view", "grants"]
    assert super_rows[1]["allowed_actions"] == ["view", "status", "grants"]


def test_role_permissions_action_is_only_visible_to_super_admin() -> None:
    service = AdminService()
    scope = Scope.all_data(user_id=1)

    regular = service.list_roles(_AdminListTx(), _ctx(), scope).data["items"]
    assert regular[0]["allowed_actions"] == ["view"]

    super_rows = service.list_roles(_AdminListTx(), _ctx(super_admin=True), scope).data["items"]
    assert super_rows[0]["allowed_actions"] == ["view", "permissions"]


def test_data_scope_projection_is_fully_chinese() -> None:
    rows = AdminService().list_scopes(
        _AdminListTx(), _ctx(), Scope.all_data(user_id=1)
    ).data["items"]

    assert rows[0]["scope_type_label"] == "区域"
    assert rows[0]["scope_target_label"] == "区域：东区"
    assert rows[0]["status_label"] == "启用"
    assert rows[1]["scope_type_label"] == "个人"
    assert rows[1]["scope_target_label"] == "本人创建的数据"


def test_audit_projection_uses_capability_and_domain_chinese_labels() -> None:
    from fpa.bootstrap import load_all

    load_all()
    row = AuditLogService._decorate(
        {
            "capability": "access.scope.list",
            "domain": "access",
            "result": "success",
            "is_agent": 0,
        }
    )

    assert row["capability_label"] == "数据范围列表"
    assert row["domain_label"] == "账号与权限"
    assert row["result_label"] == "成功"


def test_required_detail_capabilities_exist() -> None:
    from fpa.bootstrap import load_all
    from fpa.kernel.capability import REGISTRY

    load_all()
    required = {
        "access_user.get",
        "access_role.get",
        "farm.get",
        "cost_entry.get",
        "batch.get",
        "purchase_payable.get",
        "sales_receipt.get",
        "warehouse_document.get",
        "inventory_lot.get",
    }
    missing = sorted(name for name in required if REGISTRY.find(name) is None)
    assert missing == []

