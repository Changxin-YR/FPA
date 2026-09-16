"""audit 域的能力声明（1 条，registry §1.3）。

**这是"声明即派生"的落点**：一个 `Capability(...)` 同时派生

    REST 路由（`web/app.py` 按 `method` + `path` 自动注册）
    权限码（`required_permission`）
    DataScope 谓词（`scope`）
    幂等策略（`idempotent`）
    人工确认闸门（`risk` + `confirmation`）
    审计字段（`audit`）
    Agent Tool schema（`agent_exposure` + 从处理器标注收集的字段）
    前端表单与列（`fields` + 资源声明的 `columns`）

## 为什么本域只有一条能力

registry §1.3 就只声明了一条：`audit.log.list`（`GET /api/v1/audit-logs`）。
审计的**写入**不是能力——它是执行器的一部分（`kernel/audit.py::AuditWriter`，
与业务写入同一事务），所有 70 条能力都会产生审计记录。所以"审计域只有一条读能力"
是契约的形状，不是没做完。

## `agent_exposure=hidden` 是刻意的（registry §1.3 原文）

"审计日志含账号/IP/权限变更细节，Agent 无需读取；但它不是 `human_only`
（不在 `ARCHITECTURE.md:242` 允许的 human_only 方向内）。"

两者的差别是**能不能被人工页面读到**：`hidden` 只是不进 Agent 的工具清单，
人工管理员照旧能查。标成 `human_only` 会连同人工通道一起关掉——那是把
"AI 不该看"误读成"谁都不该看"。
"""

from __future__ import annotations

from yuxin.kernel.capability import (
    AgentExposure,
    Capability,
    HttpMethod,
    REGISTRY,
    Risk,
)
from yuxin.kernel.scope import ScopePolicy

from .audit_logs import AuditLogService
# `audit_log` 资源由 `audit/resources.py` 登记。这里 import 它只是为了让
# `Capability.resource="audit_log"` 能解析——
# `tests/test_architecture.py::test_capability_resources_resolve` 要求每一个
# 非空的 `resource` 都在 `RESOURCES` 里找得到。
from .resources import RESOURCES as _RESOURCES  # noqa: F401


def _register_all() -> None:
    """登记 audit 域的能力。

    与其余域同形：用函数包裹、重复导入时提前返回——`REGISTRY.register`
    对重名是显式抛错的，而测试会反复 reload 这些模块。
    """
    if REGISTRY.find("audit.log.list") is not None:
        return

    REGISTRY.register(
        Capability(
            name="audit.log.list",
            title="操作日志",
            domain="audit",
            resource="audit_log",
            handler=AuditLogService.list_audit_logs,
            service_factory=AuditLogService,
            method=HttpMethod.GET,
            path="/api/v1/audit-logs",
            kind="read",
            required_permission="audit.view",
            # §1.3 的 scope 列是 `none`：审计日志不是按数据范围分租的业务数据，
            # 而是"谁做过什么"的全局流。给它套行级谓词会让"审计一个区域管理员"
            # 变成做不到的事——而审计的全部价值恰恰在于能查别人做过什么。
            scope=ScopePolicy.none(),
            risk=Risk.READ,
            agent_exposure=AgentExposure.EXPOSED,
            description="按账号、域、能力与时间区间查询操作日志（不返回 before/after 快照字段）",
        )
    )


_register_all()

__all__ = ["_register_all"]
