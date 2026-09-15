from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from fpa.agent.gateway import AgentToolGateway
from fpa.domains.access.admin import AdminService
from fpa.kernel.capability import (
    AgentExposure,
    Capability,
    Confirmation,
    HttpMethod,
    Registry,
    Risk,
)
from fpa.kernel.confirmation import ConfirmationStore
from fpa.kernel.errors import DomainError, ErrorCode
from fpa.kernel.runner import ActorView, CapabilityRunner, Invocation, InvocationResult


class _NoTouchTx:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"非超级管理员请求不应触碰事务: {name}")


def _admin_context(*, super_admin: bool = False, permissions: frozenset[str] | None = None):
    actor = ActorView(
        user_id=7,
        username="admin",
        role_codes=frozenset({"super_admin"}) if super_admin else frozenset(),
        permissions=permissions or frozenset(),
    )
    return SimpleNamespace(actor=actor, require=lambda _permission: None)


def test_user_grants_require_the_real_super_admin_role_before_db_access() -> None:
    with pytest.raises(DomainError) as error:
        AdminService().replace_user_grants(
            _NoTouchTx(),
            _admin_context(permissions=frozenset({"auth.user.manage"})),
            SimpleNamespace(),
            {"user_id": 8},
            [3],
            [4],
        )

    assert error.value.code == ErrorCode.FORBIDDEN
    assert error.value.data == {"required_role": "super_admin"}


def test_role_permissions_require_the_real_super_admin_role_before_db_access() -> None:
    with pytest.raises(DomainError) as error:
        AdminService().replace_role_permissions(
            _NoTouchTx(),
            _admin_context(permissions=frozenset({"auth.role.manage"})),
            SimpleNamespace(),
            {"role_id": 3},
            ["pond.view"],
        )

    assert error.value.code == ErrorCode.FORBIDDEN
    assert error.value.data == {"required_role": "super_admin"}


class _CreateUserTx:
    def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "FROM roles" in sql:
            return [{"id": 3, "code": "super_admin", "status": "active"}]
        if "FROM data_scopes" in sql:
            return [{"id": 4, "status": "active"}]
        return []

    def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        if "FROM roles" in sql:
            return {"id": 3, "code": "super_admin"}
        return None

    def execute(self, sql: str, params: Any = None) -> int:
        raise AssertionError("分配 super_admin 的请求应在创建用户前被拒绝")


def test_non_super_admin_cannot_assign_super_admin_role_to_new_user() -> None:
    with pytest.raises(DomainError) as error:
        AdminService().create_user(
            _CreateUserTx(),
            _admin_context(permissions=frozenset({"auth.user.manage"})),
            SimpleNamespace(),
            "new-user",
            "新用户",
            "Valid-Password-123!",
            [3],
            [4],
        )

    assert error.value.code == ErrorCode.FORBIDDEN
    assert error.value.data == {"required_role": "super_admin"}


class _ConfirmationStateTx:
    def __init__(self) -> None:
        self.row = {"id": 9, "status": "confirmed", "failure_reason": None}

    def execute(self, sql: str, params: Any = None) -> int:
        if "status IN ('pending', 'confirmed')" not in sql:
            return 0
        self.row["status"] = "failed"
        self.row["failure_reason"] = params[1]
        return 1


class _ConfirmationUow:
    def __init__(self) -> None:
        self.tx = _ConfirmationStateTx()

    def begin(self):
        @contextmanager
        def scope():
            yield self.tx

        return scope()


def test_claimed_confirmation_can_close_as_failed() -> None:
    uow = _ConfirmationUow()
    ConfirmationStore(lambda: uow).mark_failed(
        confirmation_id=9,
        user_id=7,
        reason="INTERNAL_ERROR",
    )

    assert uow.tx.row == {
        "id": 9,
        "status": "failed",
        "failure_reason": "INTERNAL_ERROR",
    }


class _ExpiringConfirmationTx:
    def __init__(self) -> None:
        self.sql = ""

    def execute(self, sql: str, params: Any = None) -> int:
        self.sql = sql
        return 0 if "expires_at > UTC_TIMESTAMP()" in sql else 1


class _ExpiringConfirmationUow:
    def __init__(self) -> None:
        self.tx = _ExpiringConfirmationTx()

    def begin(self):
        @contextmanager
        def scope():
            yield self.tx

        return scope()


def test_confirmation_claim_rechecks_expiry_in_the_atomic_update() -> None:
    """令牌可在 find 与 claim 之间过期；claim 自身必须拒绝这条竞态。"""
    uow = _ExpiringConfirmationUow()

    claimed = ConfirmationStore(lambda: uow).consume(
        confirmation_id=9,
        user_id=7,
    )

    assert claimed is False
    assert "used_at=UTC_TIMESTAMP()" in uow.tx.sql
    assert "expires_at > UTC_TIMESTAMP()" in uow.tx.sql


class _Capability:
    name = "pond.update"
    required_permission = "pond.update"
    agent_exposure = AgentExposure.EXPOSED

    def tool_name(self) -> str:
        return "pond_update"


class _Registry:
    def all(self):
        return (_Capability(),)


class _NeverRunner:
    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("无权限工具不应进入 runner")


class _FailingAuditUow:
    def begin(self):
        raise RuntimeError("audit database unavailable")


def test_gateway_denies_before_confirmation_or_runner(caplog: pytest.LogCaptureFixture) -> None:
    gateway = AgentToolGateway(
        registry=_Registry(),
        runner=_NeverRunner(),
        audit=SimpleNamespace(),
        confirmation=SimpleNamespace(),
        uow_factory=lambda: _FailingAuditUow(),
    )

    with pytest.raises(DomainError) as error:
        gateway.call_tool(
            tool_name="pond_update",
            arguments={"pond_id": 1, "name": "blocked"},
            actor=ActorView(user_id=7, username="worker", permissions=frozenset()),
            request_id="security-test",
        )

    assert error.value.code == ErrorCode.FORBIDDEN
    assert "审计写入失败" in caplog.text


class _ReadCapability:
    name = "pond.get"
    resource = "pond"
    required_permission = "master_data.view"
    agent_exposure = AgentExposure.EXPOSED
    path_parameters = ("pond_id",)
    is_read = True

    def tool_name(self) -> str:
        return "pond_get"


class _ReadRegistry:
    def all(self):
        return (_ReadCapability(),)


class _CapturingRunner:
    def __init__(self) -> None:
        self.invocation: Invocation | None = None

    def invoke(self, invocation: Invocation, *_args: Any) -> InvocationResult:
        self.invocation = invocation
        return InvocationResult(kind="read", data={"ok": True})


def test_gateway_maps_agent_tool_path_and_query_arguments_to_invocation() -> None:
    runner = _CapturingRunner()
    gateway = AgentToolGateway(
        registry=_ReadRegistry(),
        runner=runner,
        audit=SimpleNamespace(),
        confirmation=SimpleNamespace(),
        uow_factory=lambda: _FailingAuditUow(),
    )
    arguments = {"pond_id": 7, "page": 2, "page_size": 20}

    gateway.call_tool(
        tool_name="pond_get",
        arguments=arguments,
        actor=ActorView(
            user_id=7,
            username="worker",
            permissions=frozenset({"master_data.view"}),
        ),
        request_id="path-params-test",
    )

    assert runner.invocation is not None
    assert runner.invocation.path_params == {"pond_id": 7}
    assert runner.invocation.query == {"page": 2, "page_size": 20}


def test_agent_tools_cover_the_logged_in_users_business_permissions() -> None:
    from fpa.bootstrap import load_all
    from fpa.kernel.capability import REGISTRY

    load_all()
    permissions = frozenset(
        {
            "auth.user.manage",
            "auth.role.manage",
            "audit.view",
            "pond.status.verify",
        }
    )
    visible = {item.name for item in REGISTRY.agent_tools(permissions)}

    assert {
        "access.user.list",
        "access.user.create",
        "access.role.list",
        "access.scope.list",
        "access.user.status",
        "access.user.grants",
        "access.role.permissions",
        "audit.log.list",
        "work_item.list",
    } <= visible

    # 口令会进入模型上下文和 Harness 会话文件，不属于可安全委托的业务权限。
    assert "auth.password.change" not in visible


def test_agent_access_writes_use_server_side_hitl() -> None:
    from fpa.bootstrap import load_all
    from fpa.kernel.capability import Confirmation, REGISTRY

    load_all()
    for name in (
        "access.user.create",
        "access.user.status",
        "access.user.grants",
        "access.role.permissions",
    ):
        capability = REGISTRY.get(name)
        assert capability.exposed_to_agent
        assert capability.effective_confirmation is Confirmation.ALWAYS


def test_runner_resolves_data_scope_before_issuing_confirmation() -> None:
    class _Service:
        def write(self, tx, ctx, scope):
            raise AssertionError("范围预检失败后不应执行服务")

    class _ScopeResolver:
        def resolve(self, *, user_id: int, role_codes: frozenset[str]):
            raise DomainError(ErrorCode.DATA_SCOPE_UNRESOLVED, "账号没有可用数据范围")

    class _Gate:
        def issue(self, **kwargs):
            raise AssertionError("范围预检失败后不应签发确认卡")

    registry = Registry()
    registry.register(
        Capability(
            name="security.write",
            title="安全写入",
            domain="security",
            handler=_Service.write,
            service_factory=_Service,
            method=HttpMethod.POST,
            path="/api/v1/security/write",
            kind="action",
            required_permission="security.write",
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=AgentExposure.EXPOSED,
        )
    )
    runner = CapabilityRunner(
        registry=registry,
        uow_factory=lambda: SimpleNamespace(),
        audit=SimpleNamespace(),
        idempotency=SimpleNamespace(),
        scope_resolver=_ScopeResolver(),
        confirmation_gate=_Gate(),
    )

    with pytest.raises(DomainError) as error:
        runner.invoke(
            Invocation(capability_name="security.write"),
            ActorView(
                user_id=7,
                username="worker",
                permissions=frozenset({"security.write"}),
                is_agent=True,
            ),
            "security-scope-preflight",
        )

    assert error.value.code == ErrorCode.DATA_SCOPE_UNRESOLVED
