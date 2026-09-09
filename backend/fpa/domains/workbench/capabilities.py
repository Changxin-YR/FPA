"""工作台能力声明。"""

from fpa.kernel.capability import (
    AgentExposure,
    Capability,
    Confirmation,
    HttpMethod,
    REGISTRY,
    Risk,
)
from fpa.kernel.scope import ScopePolicy

from .service import WorkbenchService


if REGISTRY.find("work_item.list") is None:
    REGISTRY.register(
        Capability(
            name="work_item.list",
            title="待办列表",
            domain="workbench",
            resource="work_item",
            handler=WorkbenchService.list_work_items,
            service_factory=WorkbenchService,
            method=HttpMethod.GET,
            path="/api/v1/work-items",
            kind="read",
            required_permission="pond.status.verify",
            scope=ScopePolicy.none(),
            risk=Risk.READ,
            agent_exposure=AgentExposure.EXPOSED,
            description="查询当前账号数据范围内待核验的塘口状态变更申请",
        )
    )


__all__ = ["WorkbenchService"]
