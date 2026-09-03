"""access 与 audit 两条线的能力声明（6 条，registry §1.2 / §1.3）。

    访问控制   access.user.list / access.user.create / access.user.status
               access.user.grants / access.role.permissions
    审计       audit.log.list

**这是"声明即派生"的落点**：一个 `Capability(...)` 同时派生

    REST 路由（`web/app.py` 按 `method` + `path` 自动注册）
    权限码（`required_permission`）
    DataScope 谓词（`scope`）
    幂等策略（`idempotent`）
    人工确认闸门（`risk` + `confirmation`）
    审计字段（`audit`）
    Agent Tool schema（`agent_exposure` + 从处理器标注收集的字段）
    前端表单与列（`fields` + 资源声明的 `columns`）

## `auth.password.change` 为什么**不在**本文件

它在 `domains/identity/capabilities.py`。原因是一条架构约束，不是归类偏好：
`tests/test_architecture.py::test_capability_domain_matches_its_declaring_directory`
把 `Capability.domain` 与**声明目录**钉在一起，而 registry §1.1 把这条能力归在
**identity 域**（小节标题"### 1.1 identity — 4 条"）。声明在本文件里只有两种结局：
`domain="access"` 与文档域归属矛盾（`tools/registry_reconcile.py` 的逐域对账会报
`access: 文档 5 / 运行时 6`，即它点名的"漏登记指纹"），或者 `domain="identity"`
与目录对不上（架构断言红）。所以 identity 有自己的域目录。

## 5 条 access 能力里有 4 条是**新增**，1 条是把固定路由搬过来

落盘前这 5 条在运行时**完全不存在**：`/api/v1/admin/users` 一类地址一律 404
（`tools/registry_reconcile.py` 的 [A] 表把它们逐条列了出来）。
实现落在 `access/admin.py`，它需要 `users` / `roles` / `permissions` /
`role_permissions` / `user_roles` / `data_scopes` / `user_data_scopes` 七张表，
全部由 001 迁移建立。

## 字段名以**真库结构**为准

registry §2.4 的字段表沿用了早期版本的列名（`name` / `phone` / `login_name` /
`assigned_by`），而 001 迁移建的是 `users(username, display_name, ...)`，
并且没有 phone 与 login_name 两列。逐条差异与处置写在 `admin.py` 的模块 docstring 里。
"""

from __future__ import annotations

from fpa.kernel.capability import (
    AgentExposure,
    AuditPolicy,
    Capability,
    Confirmation,
    HttpMethod,
    REGISTRY,
    Risk,
)
from fpa.kernel.scope import ScopePolicy

from .admin import AdminService, confirmation_labels
# `access_user` / `access_role` / `audit_log` 三个资源由 `identity/resources.py` 登记
# （按“表的归属”分域：账号与改密改的是同一张 `users` 表）。
# 这里 import 它只是为了让 `Capability.resource="access_user"` 能解析——
# `tests/test_architecture.py::test_capability_resources_resolve` 要求每一个
# 非空的 `resource` 都在 `RESOURCES` 里找得到。
from fpa.domains.identity.resources import RESOURCES as _RESOURCES  # noqa: F401

# Agent 只获得当前登录用户已有权限；高风险写操作由服务端确认闸门控制。
_ACCESS_EXPOSED = AgentExposure.EXPOSED


def _register_all() -> None:
    """登记 access 域的管理能力。

    用函数包裹而不是模块级裸语句：重复导入这个模块（例如测试里 reload）
    不会因为"能力重复注册"而炸——`REGISTRY.register` 对重名是显式抛错的。
    """
    if REGISTRY.find("access.user.list") is not None:
        return  # 已注册（重复导入）

    # ========================================================================
    # §1.2 access —— 5 条
    # ========================================================================

    REGISTRY.register(
        Capability(
            name="access.user.list",
            title="账号列表",
            domain="access",
            resource="access_user",
            handler=AdminService.list_users,
            service_factory=AdminService,
            method=HttpMethod.GET,
            path="/api/v1/admin/users",
            kind="read",
            required_permission="auth.user.manage",
            # §1.2：`scope=none`。账号与角色是**系统级**对象（§1.2 依据原文：
            # "它们是**系统级**权限，不是业务能力码"），给它们套行级数据范围谓词会让
            # "第一个管理员看不到第二个管理员"。这与"放宽"无关：进入门槛是
            # `auth.user.manage`，执行器与服务各查一次。
            scope=ScopePolicy.none(),
            risk=Risk.READ,
            agent_exposure=_ACCESS_EXPOSED,
            description="按关键词、状态与角色筛选账号列表，附角色与数据范围",
        )
    )

    REGISTRY.register(
        Capability(
            name="access_user.get",
            title="账号详情",
            domain="access",
            resource="access_user",
            handler=AdminService.get_user,
            service_factory=AdminService,
            method=HttpMethod.GET,
            path="/api/v1/admin/users/{user_id}",
            kind="read",
            required_permission="auth.user.manage",
            scope=ScopePolicy.none(),
            risk=Risk.READ,
            agent_exposure=_ACCESS_EXPOSED,
            description="查询账号详情、角色和数据范围",
        )
    )

    REGISTRY.register(
        Capability(
            name="access.user.create",
            title="新建账号",
            domain="access",
            resource="access_user",
            handler=AdminService.create_user,
            service_factory=AdminService,
            method=HttpMethod.POST,
            path="/api/v1/admin/users",
            kind="create",
            required_permission="auth.user.manage",
            scope=ScopePolicy.none(),
            risk=Risk.HIGH,
            # §1.2 给本行标的是 `confirmation=never`。这里**显式**写出来：
            # `risk=HIGH` 的默认生效值是 `ALWAYS`（`Capability.effective_confirmation`），
            # 而 §0.4 收了 5 条"高危但免确认"的能力，本行是其中之一。
            # 不显式声明就会让 Agent 入口多一道闸门——那是与权威清单不一致的偏离。
            #
            # 免确认的理由（§0.4 原文）：`human_only` 的能力**根本不会被 Agent 执行**，
            # 给它标确认闸门没有意义。`human_only` 已经是更强的限制。
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_ACCESS_EXPOSED,
            # §1.2 原文："`access.user.create` 的 `idempotent=true` **强制**：早期版本靠
            # `uq_users_phone` 唯一键兜底并翻译成 `PHONE_EXISTS`"。
            # 本版的承载列是 `uq_users_username`（001 迁移），结论不变。
            idempotent=True,
            confirmation_labels=confirmation_labels,
            audit=AuditPolicy.snapshot(),
            loader=AdminService.load_user,
            description="新建账号并授予角色与数据范围；执行前必须由当前登录用户确认",
        )
    )

    REGISTRY.register(
        Capability(
            name="access.role.list",
            title="角色列表",
            domain="access",
            resource="access_role",
            handler=AdminService.list_roles,
            service_factory=AdminService,
            method=HttpMethod.GET,
            path="/api/v1/admin/roles",
            kind="read",
            required_permission="auth.user.manage",
            scope=ScopePolicy.none(),
            risk=Risk.READ,
            agent_exposure=_ACCESS_EXPOSED,
            description="返回可用于账号授权的启用角色",
        )
    )

    REGISTRY.register(
        Capability(
            name="access_role.get",
            title="角色详情",
            domain="access",
            resource="access_role",
            handler=AdminService.get_role,
            service_factory=AdminService,
            method=HttpMethod.GET,
            path="/api/v1/admin/roles/{role_id}",
            kind="read",
            required_permission="auth.user.manage",
            scope=ScopePolicy.none(),
            risk=Risk.READ,
            agent_exposure=_ACCESS_EXPOSED,
            description="查询角色详情和权限清单",
        )
    )

    REGISTRY.register(
        Capability(
            name="access.scope.list",
            title="数据范围列表",
            domain="access",
            resource="access_scope",
            handler=AdminService.list_scopes,
            service_factory=AdminService,
            method=HttpMethod.GET,
            path="/api/v1/admin/scopes",
            kind="read",
            required_permission="auth.user.manage",
            scope=ScopePolicy.none(),
            risk=Risk.READ,
            agent_exposure=_ACCESS_EXPOSED,
            description="返回可用于账号授权的启用数据范围",
        )
    )

    REGISTRY.register(
        Capability(
            name="access.user.status",
            title="启用/停用账号",
            domain="access",
            resource="access_user",
            handler=AdminService.set_user_status,
            service_factory=AdminService,
            method=HttpMethod.POST,
            path="/api/v1/admin/users/{user_id}/status",
            kind="action",
            required_permission="auth.user.manage",
            scope=ScopePolicy.none(),
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_ACCESS_EXPOSED,
            confirmation_labels=confirmation_labels,
            audit=AuditPolicy.snapshot(),
            loader=AdminService.load_user,
            description="启用或停用一个账号，须填写变更原因；执行前必须由当前登录用户确认",
        )
    )

    REGISTRY.register(
        Capability(
            name="access.user.grants",
            title="调整角色与数据范围",
            domain="access",
            resource="access_user",
            handler=AdminService.replace_user_grants,
            service_factory=AdminService,
            method=HttpMethod.PUT,
            path="/api/v1/admin/users/{user_id}/grants",
            kind="action",
            required_permission="auth.user.manage",
            scope=ScopePolicy.none(),
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_ACCESS_EXPOSED,
            confirmation_labels=confirmation_labels,
            audit=AuditPolicy.snapshot(),
            loader=AdminService.load_user,
            description="按最终集合同步账号的角色与数据范围；执行前必须由当前登录用户确认",
        )
    )

    REGISTRY.register(
        Capability(
            name="access.role.permissions",
            title="调整角色权限",
            domain="access",
            resource="access_role",
            handler=AdminService.replace_role_permissions,
            service_factory=AdminService,
            method=HttpMethod.PUT,
            path="/api/v1/admin/roles/{role_id}/permissions",
            kind="action",
            required_permission="auth.role.manage",
            scope=ScopePolicy.none(),
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_ACCESS_EXPOSED,
            confirmation_labels=confirmation_labels,
            audit=AuditPolicy.snapshot(),
            loader=AdminService.load_role,
            description="按最终集合同步角色的权限；执行前必须由当前登录用户确认",
        )
    )


_register_all()

__all__ = ["_register_all"]
