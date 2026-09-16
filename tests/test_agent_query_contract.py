from __future__ import annotations

from typing import Any

import pytest

from yuxin.agent.gateway import AgentToolGateway
from yuxin.kernel.audit import AuditWriter
from yuxin.kernel.capability import (
    AgentExposure,
    Capability,
    HandlerResult,
    HttpMethod,
    Registry,
    Risk,
)
from yuxin.kernel.errors import DomainError, ErrorCode
from yuxin.kernel.runner import ActorView, Invocation, InvocationResult
from yuxin.kernel.workflow import FilterKind, FilterSpec, RESOURCES, Resource


def _list_handler(tx: Any, ctx: Any, scope: Any, query: dict[str, Any] | None = None) -> HandlerResult:
    return HandlerResult(data=query or {})


class _Runner:
    def __init__(self) -> None:
        self.invocation: Invocation | None = None

    def invoke(self, invocation: Invocation, *_args: Any) -> InvocationResult:
        self.invocation = invocation
        return InvocationResult(kind="read", data={})


class _Uow:
    def begin(self):
        raise AssertionError("查询参数契约测试不应触碰审计事务")


def _gateway() -> tuple[AgentToolGateway, _Runner]:
    resource_name = "agent_query_contract"
    if RESOURCES.find(resource_name) is None:
        RESOURCES.register(
            Resource(
                name=resource_name,
                title="查询契约探针",
                module="ut",
                list_path="/api/v1/agent-query-contract",
                table="agent_query_contract",
                columns=(("status", "状态"),),
                search=True,
                filters=(
                    FilterSpec("status", "状态", FilterKind.STRING),
                    FilterSpec("active", "仅看启用", FilterKind.BOOLEAN),
                ),
            )
        )
    capability = Capability(
        name="agent_query_contract.list",
        title="查询契约探针",
        domain="ut",
        handler=_list_handler,
        method=HttpMethod.GET,
        path="/api/v1/agent-query-contract",
        kind="read",
        resource=resource_name,
        required_permission="ut.view",
        risk=Risk.READ,
        agent_exposure=AgentExposure.EXPOSED,
    )
    registry = Registry()
    registry.register(capability)
    runner = _Runner()
    gateway = AgentToolGateway(
        registry=registry,
        runner=runner,
        audit=AuditWriter(),
        confirmation=object(),
        uow_factory=lambda: _Uow(),
    )
    return gateway, runner


def test_read_tool_schema_and_gateway_preserve_declared_query_filters() -> None:
    gateway, runner = _gateway()
    tool = gateway.list_tools(frozenset({"ut.view"}))[0]

    assert {"keyword", "status", "active", "page", "page_size"} <= set(
        tool.parameters["properties"]
    )

    gateway.call_tool(
        tool_name="agent_query_contract_list",
        arguments={
            "page": 2,
            "page_size": 20,
            "keyword": "北区",
            "status": "draft",
            "active": True,
        },
        actor=ActorView(
            user_id=7,
            username="worker",
            permissions=frozenset({"ut.view"}),
        ),
        request_id="query-contract",
    )

    assert runner.invocation is not None
    assert runner.invocation.query == {
        "page": 2,
        "page_size": 20,
        "keyword": "北区",
        "status": "draft",
        "active": True,
    }


def test_read_tool_rejects_undeclared_query_parameter() -> None:
    gateway, _runner = _gateway()

    with pytest.raises(DomainError) as caught:
        gateway.call_tool(
            tool_name="agent_query_contract_list",
            arguments={"secret_filter": "should-not-be-ignored"},
            actor=ActorView(
                user_id=7,
                username="worker",
                permissions=frozenset({"ut.view"}),
            ),
            request_id="query-contract-reject",
        )

    assert caught.value.code == ErrorCode.VALIDATION_ERROR
