"""identity 域：`auth.password.change`（registry §1.1 的第 4 条）。

## 为什么它必须是**独立域目录**，而不是挂在 `access/` 下

`tests/test_architecture.py::test_capability_domain_matches_its_declaring_directory`
把 `Capability.domain` 与**声明目录**钉在一起。于是"把 `auth.password.change` 声明
在 `access/capabilities.py` 里"只有两种可能：

  * `domain="access"` —— 与 registry §1.1 的域归属矛盾，且
    `tools/registry_reconcile.py` 的逐域条数对账会显示 `access: 文档 5 / 运行时 6`
    （**运行时多**），而那正是该工具点名的"漏登记指纹"；
  * `domain="identity"` —— 目录名对不上，架构断言直接红。

第三种可能是"把目录也建成 identity"——这正是本文件。判据不是"auth 看起来像
identity"，而是 registry §1.1 的小节标题：**identity 域 4 条**（`auth.login` /
`auth.logout` / `auth.me` / `auth.password.change`）。

## 为什么这个域里只有一条能力

`auth.login` / `auth.logout` / `auth.me` 三条**仍然是 `web/routes_auth.py` 的固定路由**
（`tools/registry_reconcile.py` 的 [A2] 表把它们单列，并判定"接口契约行，不是漏实现"）。
理由写在 `routes_auth.py` 的模块 docstring 里：它们发生在"**有身份**"之前，
而能力执行器的第一步就要求 `actor.user_id`。

改密不同——它发生在一个已认证的会话里，所以它是能力。这个域因此只有一条能力，
**这是契约的形状，不是没做完**：`[A2]` 表会一直显示那三条，直到有人决定
"登录也可以是一条能力"（那需要执行器先支持匿名能力，属于内核改造）。

## 实现只有一处

处理逻辑在 `domains/access/admin.py::AdminService.change_password`——
改密要"改哈希 + 撤销全部会话"，而会话表与权限加载都在 access 域。
本文件只做**声明**与**转发**，不复制实现。跨域 import 在这里是允许的：
`docs/ROLLOUT_CONTRACT.md` §2 禁止的是"直接读写另一个域的表"，
而这里是调用对方**具名的进程内函数**。
"""

from __future__ import annotations

from typing import Any

from yuxin.kernel import capability as cap
from yuxin.kernel.capability import (
    AgentExposure,
    AuditPolicy,
    Capability,
    Confirmation,
    HttpMethod,
    REGISTRY,
    Risk,
)
from yuxin.kernel.fields import f_str
from yuxin.kernel.scope import ScopePolicy
from yuxin.kernel.uow import UnitOfWork

# “当前时间”只能有一处：`sessions.revoked_at` 与 `users.updated_at` 都是
# **无时区** DATETIME，两个“当前时间”会让“会话刚刚被撤销”
# 与“撤销时间早于现在”同时成立。初版在本文件里又写了一份（
# 本仓 `_utcnow` 已有四处），已删除并改为复用。
from yuxin.domains.access.service import utcnow as _utcnow  # noqa: F401


class PasswordService:
    """改密。**唯一的实现**（`access/admin.py` 的那一份已删除）。

    ## 为什么它是类而不是模块级函数

    执行器的 `runner._resolve()` 用 `getattr(func, "__self__", None)` 判断
    "这个函数对象是不是已绑定的方法"：

      * 未绑定 → 必须用 `spec.service_factory` 取实例再 `getattr` 出绑定方法；
      * **模块级函数没有 `__self__`**，于是它会被当成未绑定方法，
        在缺工厂时抛 `INTERNAL_ERROR: 使用了实例方法 <name>，但没有声明 service_factory`。

    也就是说"把处理器写成模块级函数、并期待内核直接调用它"这条路的**唯一**出口是
    `service_factory=<一个返回带同名方法的对象>` ——那不如直接把方法写在类上。
    本文件初版就踩了这个坑：错误只在**运行期**出现，而且看起来像服务装配坏了。

    ## 三个字段逐个写在签名里（不是 `**fields`）

    字段集由 `Capability.__post_init__` 从**处理器签名**的 `Annotated[T, Field(...)]`
    自动收集（`kernel/capability.py::_field_annotations`）。写成 `**fields` 会收集到
    **空集**，后果是 registry §2.3 的三个字段一个都不校验、前端渲染不出表单、
    Agent 的 tool schema 里没有参数——**且全程不报错**。这正是
    `Capability.fields` 那次事故（"9 条能力全忘传 fields="）的同形复发点。

    ## 事务

    改密与撤销会话在**执行器传进来的 `tx`** 里完成，不新开事务。
    `AccessService.change_password` 自己开事务，因此不能复用它——嵌套事务会让
    "改密成功但审计失败"各自回滚一半。
    """

    def change_password(
        self,
        tx: UnitOfWork,
        ctx,
        scope,
        current_password: f_str("当前密码", required=True, max_length=128),
        new_password: f_str("新密码", required=True, max_length=128),
        confirm_password: f_str("确认新密码", required=True, max_length=128),
    ) -> cap.HandlerResult:
        from yuxin.kernel.errors import DomainError, ErrorCode, not_found
        from yuxin.kernel.password import (
            hash_password,
            password_problem,
            verify_password,
        )

        user_id = ctx.actor.user_id
        row = tx.query_one("SELECT password_hash FROM users WHERE id=%s", (user_id,))
        if row is None:
            raise not_found("账号")
        if not verify_password(str(current_password), str(row["password_hash"])):
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "当前密码不正确",
                data={"field": "current_password"},
            )
        if str(new_password) != str(confirm_password):
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "两次输入的新密码不一致",
                data={"field": "confirm_password"},
            )
        reason = password_problem(str(new_password))
        if reason is not None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, f"新密码{reason}", data={"field": "new_password"}
            )
        if verify_password(str(new_password), str(row["password_hash"])):
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "新密码不能与当前密码相同",
                data={"field": "new_password"},
            )

        tx.execute(
            "UPDATE users SET password_hash=%s, must_change_password=0, "
            "                 row_version=row_version+1, updated_at=%s WHERE id=%s",
            (hash_password(str(new_password)), _utcnow(), user_id),
        )
        # 撤销**本人全部**会话（含当前这一个）。理由见
        # `AccessService.change_password` 的 docstring：密码泄露场景下攻击者可能
        # 已经建立了会话；改密不撤销旧会话，泄露方仍然在线。
        revoked = tx.execute(
            "UPDATE sessions SET revoked_at=%s WHERE user_id=%s AND revoked_at IS NULL",
            (_utcnow(), user_id),
        )
        return cap.HandlerResult(
            data={"record": {"id": user_id, "revoked_sessions": int(revoked or 0)}},
            resource_id=user_id,
            message="密码已修改，请用新密码重新登录",
        )

    @classmethod
    def load_user(
        cls, tx: UnitOfWork, *, record_id: int, scope: Any = None
    ) -> dict[str, Any] | None:
        """回读账号。**与 `access.user.*` 复用同一个 `AdminService.load_user`**。

        为什么不做成"identity 域自己的回读"：回读的是同一张 `users` 表的同一行、
        同一份投影。第二处实现会在某天漏掉一个派生列（`role_names` 之类），
        而症状是"改密之后返回的用户信息少了几个字段"——没人会为此报 bug。
        """
        from yuxin.domains.access.admin import AdminService

        return AdminService.load_user(tx, record_id=record_id, scope=scope)


def _register_all() -> None:
    """登记 identity 域的能力。

    与其余域同形：用函数包裹、重复导入时提前返回——`REGISTRY.register`
    对重名是显式抛错的，而测试会反复 reload 这些模块。
    """
    if REGISTRY.find("auth.password.change") is not None:
        return

    REGISTRY.register(
        Capability(
            name="auth.password.change",
            title="修改密码",
            domain="identity",
            resource="access_user",
            handler=PasswordService.change_password,
            # `service_factory` **必须**给：执行器的 `_resolve` 用
            # `getattr(func, "__self__", None)` 判"是不是已绑定的方法"，
            # 而**模块级函数没有 `__self__`** ——于是它会被当成未绑定方法，
            # 在缺工厂时抛 `INTERNAL_ERROR: 使用了实例方法 ... 但没有声明 service_factory`。
            #
            # 这一条踩过（本文件初版把处理器写成模块级函数，忘了工厂）：
            # 错误在**运行期**才出现，而且看起来像"服务装配坏了"，
            # 真实原因只是少了一行声明。为此把 `delivery` 那一组也一并核对了一遍。
            service_factory=PasswordService,
            method=HttpMethod.POST,
            path="/api/v1/auth/password/change",
            kind="action",
            # registry §1.1 的 `required_permission` 是 `—`，表注写的是
            # "无需权限（仅需登录）"。内核里 `None` 的语义逐字就是它：
            # `capability.authorized()` 对 `None` 直接返回 True。
            required_permission=None,
            # §1.1：`scope=owner(user_id)`。只能改**自己**的密码，
            # 而"自己"由服务取 `ctx.actor.user_id` 保证（声明与实现是同一条事实）。
            scope=ScopePolicy.owner("user_id"),
            risk=Risk.HIGH,
            # §1.1 标的是 `confirmation=never`。显式写出来：`risk=HIGH` 的默认生效值
            # 是 `ALWAYS`（`Capability.effective_confirmation`），而本行属于 §0.4
            # 那 5 条"高危但免确认"——理由是 `human_only` 已经是更强的限制。
            confirmation=Confirmation.NEVER,
            # §5.2 第 ⑥ 类"身份/会话生命周期"。
            agent_exposure=AgentExposure.HUMAN_ONLY,
            # registry §1.1 给本行的 `audit` 列是 **summary**（改密记动作，不记 before/after diff）。
            #
            # 这一条尤其不能记 before_after：`users` 行的 before/after 里含 `password_hash`。
            # `kernel/audit.py::redact` 会按键名把含 password 的键替换成 [REDACTED]，所以不会泄哈希；
            # 但**少记这一档就少一个风险面**——脱敏是按名字匹配的，而名字匹配的漏网方式不需要举例。
            audit=AuditPolicy.summary(),
            loader=PasswordService.load_user,
            description="修改本人的登录密码，成功后撤销全部会话（人工专属：凭据操作不能由智能体代劳）",
        )
    )


_register_all()

__all__ = ["PasswordService", "_register_all"]
