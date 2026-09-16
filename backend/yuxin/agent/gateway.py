"""Agent Gateway：把能力注册表暴露成 DeepSeek Harness 的工具面。

对应需求的"设计 AI Agent Gateway，通过会话绑定、工具注册、参数校验和固定业务路由，
将自然语言指令转换为受控 Business API 调用"。

**三层权限防御的落点**（需求明确要求，逐层可测）：

    第一层 Tool Filtering   → `list_tools(permissions)`：只返回该用户有权调用的工具
    第二层 Gateway Re-check → `call_tool(...)`：即使模型构造了未授权工具，也再次校验
    第三层 Business Service → `CapabilityRunner` 调服务，服务自己再校验一次

三层的**判定函数是同一个**（`capability.authorized`），所以三层不可能给出不同结论。
这不是"三处实现碰巧一致"，而是"只有一处实现、被调用三次"。

**固定业务路由**：`call_tool` 只按 `Registry` 里的 `tool_name` 查能力，
查不到就 `TOOL_NOT_FOUND`。模型无法构造 URL、SQL、Shell 或未注册接口——
因为这里根本没有接收 URL 的入口。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from yuxin.kernel.audit import AuditEvent, AuditWriter
from yuxin.kernel.capability import (
    AgentExposure,
    Capability,
    Confirmation,
    Registry,
    assert_authorized,
    authorized,
)
from yuxin.kernel.confirmation import ConfirmationGate
from yuxin.kernel.errors import DomainError, ErrorCode, forbidden
from yuxin.kernel.runner import ActorView, CapabilityRunner, Invocation, InvocationResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """暴露给 Harness 的一个工具。"""

    name: str
    description: str
    parameters: dict[str, Any]
    capability: str
    #: 该能力是否**要求**调用方携带 `Idempotency-Key`（裁决 Q4）。
    #:
    #: 必须暴露给插件：否则插件要么一律带键（无害但让服务端失去"非幂等能力不该
    #: 收到键"这个契约违规信号），要么一律不带（重复敏感操作会被服务端拒绝）。
    requires_idempotency_key: bool = False

    def to_meta(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "capability": self.capability,
            "requires_idempotency_key": self.requires_idempotency_key,
        }


@dataclass(slots=True)
class TurnOutcome:
    """一次工具调用的结果。

    ``kind`` 与 `docs/WRITE_CONTRACT.md` 完全一致，且是**判别联合**：
    ``executed`` 与 ``confirmation_required`` 在类型上无法同时为真。
    """

    kind: str
    message: str = ""
    data: Any = None
    resource_id: int | None = None
    confirmation: dict[str, Any] | None = None
    capability: str = ""
    resource: str = ""
    resource_url: str = ""
    conversation_id: str = ""

    def to_meta(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind}
        if self.message:
            payload["message"] = self.message
        if self.data is not None:
            payload["data"] = self.data
        if self.resource_id is not None:
            payload["resource_id"] = self.resource_id
        if self.confirmation is not None:
            payload["confirmation"] = self.confirmation
        if self.kind == "executed" and self.capability:
            payload["result"] = {
                "capability": self.capability,
                "resource": self.resource,
                "resource_id": self.resource_id,
                "url": self.resource_url,
                "data": {
                    "executed": [
                        {
                            "capability": self.capability,
                            "resource": self.resource,
                            "resource_id": self.resource_id,
                        }
                    ]
                },
            }
        return payload


class AgentToolGateway:
    """Agent 侧的能力出口。"""

    def __init__(
        self,
        *,
        registry: Registry,
        runner: CapabilityRunner,
        audit: AuditWriter,
        confirmation: ConfirmationGate,
        uow_factory: Any,
    ) -> None:
        self._registry = registry
        self._runner = runner
        self._audit = audit
        self._confirmation = confirmation
        self._uow_factory = uow_factory

    # -- 第一层：工具过滤 -----------------------------------------------------

    def list_tools(self, permissions: frozenset[str]) -> list[ToolSpec]:
        """只返回该用户**有权调用**且**声明为对 Agent 开放**的能力。

        早期版本是把全部操作塞进一个 `biz_query` / `biz_mutation` 的长描述字符串里，
        让模型自己去挑——那等于把权限过滤的责任推给了提示词。
        新实现里，没权限的工具**根本不出现在工具清单里**。
        """
        specs: list[ToolSpec] = []
        for capability in self._registry.agent_tools(permissions):
            specs.append(
                ToolSpec(
                    name=capability.tool_name(),
                    description=capability.tool_description(),
                    parameters=capability.to_tool_schema(),
                    capability=capability.name,
                    requires_idempotency_key=capability.requires_idempotency_key,
                )
            )
        return specs

    def tools_payload(self, permissions: frozenset[str]) -> dict[str, Any]:
        """`GET /api/v1/agent/tools` 的响应体。

        工具 schema 走这个接口**而不是环境变量**——早期版本把它塞进
        `YUXIN_AGENT_TOOL_CATALOG`，源码注释自承是为了绕开 Windows 进程环境变量上限。
        """
        return {"tools": [spec.to_meta() for spec in self.list_tools(permissions)]}

    # -- 第二层：工具调用 -----------------------------------------------------

    def call_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        actor: ActorView,
        request_id: str,
        idempotency_key: str | None = None,
        confirmation_token: str | None = None,
        confirmation_id: int | None = None,
    ) -> TurnOutcome:
        """执行一次工具调用。

        ``tool_name`` 到能力的解析是**唯一**的路由：查不到就拒绝。
        没有任何分支可以接受一个 URL、一段 SQL 或一个未注册的接口名。
        """
        capability = self._resolve_tool(
            tool_name, actor=actor, request_id=request_id
        )

        # Gateway 必须在签发或消费确认令牌前完成第二层授权，避免无权用户拿到
        # 高风险确认卡；具体业务服务仍会在 Runner 内再次校验。
        if not authorized(capability, actor.permissions):
            self._audit_denied(capability, actor, request_id, "FORBIDDEN")
            assert_authorized(capability, actor.permissions)

        # 人工专属：连"准备"都不给，直接拒绝（且留审计）
        if capability.agent_exposure is AgentExposure.HUMAN_ONLY:
            self._audit_denied(capability, actor, request_id, "HUMAN_ONLY")
            raise DomainError(
                ErrorCode.HUMAN_ONLY,
                "该操作只能由本人在系统页面完成，智能体不能代劳",
                data={"capability": capability.name},
            )

        # 确认路径：`.confirm` 必须带令牌；带令牌时先消费并比对参数指纹
        if confirmation_token:
            return self._call_confirmed(
                capability=capability,
                token=confirmation_token,
                arguments=arguments,
                actor=actor,
                request_id=request_id,
                idempotency_key=idempotency_key,
            )

        path_params, query = self._invocation_params(capability, arguments)
        self._require_path_params(capability, arguments, path_params=path_params)
        invocation = Invocation(
            capability_name=capability.name,
            payload=arguments,
            path_params=path_params,
            query=query,
            raw_payload=self._body_payload(capability, arguments),
            idempotency_key=idempotency_key,
        )
        result = self._runner.invoke(invocation, actor, request_id)
        return self._to_outcome(result, capability)

    @staticmethod
    def _require_path_params(
        capability: Capability,
        arguments: dict[str, Any],
        *,
        path_params: dict[str, Any],
    ) -> None:
        """路径参数缺失必须**当场拒绝**，不能一路放行到"签出一张永远执行不了的卡"。

        ## 为什么需要这一层

        `Capability.to_tool_schema()` 会把路径参数放进工具 schema 的 `required`
        （这是对的：模型只有一个 `arguments` 通道，必须被告知要传 id）。但**告诉模型要传**
        不等于**模型一定会传** —— 真实模型偶发漏传时，网关原先什么都不检查：

        1. 缺的参数不在 `arguments` 里 ⇒ `_invocation_params` 挑不出它 ⇒ `path_params` 为空；
        2. 能力仍然照常签发确认卡（`risk=high` ⇒ `confirmation=always`）；
        3. 用户点「确认执行」才在服务层炸 —— 实测 `pond.update` 走到
           `ponds_write.py:199 int(pond_id)` → `TypeError` → **500**，
           或者报一个和"你漏传了参数"毫无关系的业务错误。

        用户拿到的是一张**永远执行不了的卡**：卡在、点了报错、原因看不懂、也没有补输的入口。
        这正是本项目一直在根除的"看起来准备好了、其实是坏的"形态。

        ## 为什么判据放在这一层而不是 schema

        schema 是**给模型看的**（概率性），网关校验是**确定性的**。两者都要有：
        前者提高一次成功率，后者保证"漏传"不会变成一个静默的坏卡。
        """
        missing = [key for key in capability.path_parameters if key not in arguments]
        if not missing:
            return
        labels = [
            (capability.fields[key].label if key in capability.fields else key)
            for key in missing
        ]
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            "缺少必填参数：" + "、".join(labels),
            data={"fields": missing, "capability": capability.name},
        )

    def _call_confirmed(
        self,
        *,
        capability: Capability,
        token: str,
        arguments: dict[str, Any],
        actor: ActorView,
        request_id: str,
        idempotency_key: str | None,
    ) -> TurnOutcome:
        """带确认令牌的执行。

        顺序要点：
          1. 先消费令牌（原子占位，一次性）
          2. **再**比对参数指纹——用户确认的是卡片上那份参数，执行时不能变
          3. 再交给 runner，且把 ``confirmation_id`` 带进 actor，让 runner 知道
             "这是一个已确认的调用，不要再要求确认"
        """
        pending = self._confirmation.consume(
            token=token, actor=actor, capability_name=capability.name
        )
        try:
            self._confirmation.verify_params(pending, dict(arguments))
            confirmed_actor = ActorView(
                user_id=actor.user_id,
                username=actor.username,
                permissions=actor.permissions,
                role_codes=actor.role_codes,
                session_hash=actor.session_hash,
                is_agent=True,
                conversation_id=str(pending.get("conversation_id") or actor.conversation_id or ""),
                confirmation_id=int(pending["confirmation_id"]),
            )
            # 幂等键由 confirmation_id 派生：同一次确认只会执行一次，
            # 因此这个键是稳定的，且**不需要客户端提供**。
            # 这正是把"一次性"的保证收在服务端的做法——调用方无法通过重放制造第二次写入。
            derived_key = f"agent-confirmed:{pending['confirmation_id']}"
            path_params, query = self._invocation_params(
                capability, pending["payload"]
            )
            invocation = Invocation(
                capability_name=capability.name,
                payload=pending["payload"],
                path_params=path_params,
                query=query,
                # 请求体口径取**卡片上那份参数**，不是本次 HTTP body。
                #
                # 这里曾经是 `dict(arguments)`：确认端点只提交一个令牌，于是
                # `{"token": "…"}` 被当成了请求体 —— 而路径参数（`pond_id`）只存在于
                # `path_params` 与 `payload` 里，**不在**这个 body 中。
                #
                # 为什么必须回填 `pending["payload"]`（这是上面那段注释的口径）：
                #   1. `_invocation_params` 还原 URL 用的就是 `pending["payload"]`，
                #      所以"请求体"与"URL 参数来源"必须是同一份，否则两者会漂移
                #      （`pond_id` 在 URL 里、却不在 body 里 → 处理器拿到 None）；
                #   2. 本方法自己的 docstring 写着"**参数由服务端从库里取**，不由调用方
                #      再次提交" —— 把本次 body 当参数就是把刚省掉的那处往返又建回来
                #      （还是以一个只有一个令牌的 body 的形式）；
                #   3. 提交的 arguments 已由上一行 `verify_params` 与卡片指纹比对过，
                #      所以"执行的就是卡片上那份参数"仍然成立。
                # ★ 与 `call_tool` 同口径：路径参数**不属于请求体**。
                # 这里原先写的是 `dict(pending["payload"])`，于是 F5 的修复只覆盖了签名路径、
                # **漏了两条确认路径**。后果是那 3 条提权能力（`access.user.grants` /
                # `access.user.status` / `access.role.permissions`）的路径参数
                # `user_id` / `role_id` **不是处理器声明的字段**，一旦留在 raw_payload 里
                # 就被 `validate_payload` 判成「未知字段」：
                #
                #     实测（卡片 168，用户在界面上点「确认执行」）：
                #       payload_json = {"user_id": 7, "role_ids": [7, 9], "scope_ids": [2]}
                #       → VALIDATION_ERROR 请求包含不接受的字段：user_id
                #       → 卡片落成 failed，前端却只显示「确认令牌无效、已过期或已被使用」
                #
                # 即：**卡签得出来，却永远确认不了** —— 正是 F5 要根除的形态，只是换了条路径。
                raw_payload=self._body_payload(capability, pending["payload"]),
                idempotency_key=idempotency_key or derived_key,
            )
            result = self._runner.invoke(invocation, confirmed_actor, request_id)
        except BaseException as error:  # noqa: BLE001 - claim 后任何失败都必须收口
            # 执行失败要把确认记录收口成 failed，否则它会一直挂在 pending，
            # 用户看到一张永远"等待确认"的卡片。
            try:
                self._confirmation._store.mark_failed(
                    confirmation_id=int(pending["confirmation_id"]),
                    user_id=actor.user_id,
                    reason=str(getattr(error, "code", type(error).__name__)),
                )
            except Exception:  # noqa: BLE001 - 不遮蔽原始业务异常
                logger.exception(
                    "确认失败状态收口失败 confirmation_id=%s actor_id=%s error_type=%s",
                    pending["confirmation_id"],
                    actor.user_id,
                    type(error).__name__,
                )
            raise
        outcome = self._to_outcome(result, capability)
        outcome.conversation_id = str(pending.get("conversation_id") or actor.conversation_id or "")
        return outcome

    def _resolve_tool(
        self,
        tool_name: str,
        *,
        actor: ActorView | None = None,
        request_id: str = "",
    ) -> Capability:
        """工具名 → 能力。**固定业务路由**的唯一入口。

        解析失败时**留审计**。理由：模型（或被注入的提示）反复尝试未注册工具名，
        属于"发生了什么"——如果只抛错不记，探测行为在系统里完全不可见，
        "禁止未注册接口"就退化成一条运行时约束，事后无法回答"有没有人试过"。
        """
        for capability in self._registry.all():
            if capability.tool_name() == tool_name:
                return capability

        if actor is not None:
            self._audit_tool_denied(tool_name, actor, request_id)
        raise DomainError(
            ErrorCode.TOOL_NOT_FOUND,
            "该操作不在允许范围内",
            data={"tool": tool_name},
        )

    def _audit_tool_denied(
        self, tool_name: str, actor: ActorView, request_id: str
    ) -> None:
        """未注册工具调用 → denied 审计。走独立事务（业务事务并不存在）。"""
        uow = self._uow_factory()
        try:
            with uow.begin() as tx:
                self._audit.write(
                    tx,
                    AuditEvent(
                        capability="<unregistered>",
                        domain="agent",
                        object_type="agent_tool",
                        object_ref=tool_name[:255],
                        result="denied",
                        reason="TOOL_NOT_FOUND",
                    ),
                    request_id=request_id,
                    user_id=actor.user_id,
                    username=actor.username,
                    is_agent=actor.is_agent,
                    conversation_id=actor.conversation_id,
                )
        except Exception:  # noqa: BLE001 - 审计失败不能掩盖拒绝本身
            logger.exception(
                "审计写入失败 capability=%s request_id=%s actor_id=%s event=tool_not_found",
                "<unregistered>",
                request_id,
                actor.user_id,
            )

    @staticmethod
    def _body_payload(
        capability: Capability, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """把工具参数里**只作为路径参数存在**的键剔出去，剩下的才是"请求体"。

        ## 为什么必须有这一层（F5，t2）

        `Capability.to_tool_schema()` 会把路径参数（`{user_id}` 这类）加进工具
        schema 的 `properties` **并放进 `required`** —— 这是**对的**：模型手里只有
        一个 `arguments` 对象，没有独立的"路径参数"通道，所以必须告诉它传 id。

        但"模型必须传"不等于"它属于请求体"。执行器在 `runner.py` 用
        ``raw_payload`` 走 `validate_payload`，而后者拿
        `Capability.create_fields()`（**处理器签名里声明过的字段**）算"未知字段"：

            POST /api/v1/agent/tools/access_user_grants/call
              arguments={"user_id": 7, "role_ids": [9], "scope_ids": [2]}
              → 400 VALIDATION_ERROR 请求包含不接受的字段：user_id

        实测后果：`access.user.status` / `access.user.grants` /
        `access.role.permissions` 这 **3 个高危写能力的 Agent 工具完全不可用**，
        HITL 确认卡永远签不出来（**fail-closed**，所以不是越权，是"能力不存在"）。

        ## ★ 只剔"**不是**声明字段"的那部分（第一版在这里踩了坑）

        第一版是无条件剔掉所有路径参数，结果打断了 `tools/agent_e2e.py` 里
        `pond.close` 这类能力 —— 它的路径模板是 `/{pond_id}/close`，而处理器**同时**
        把 `pond_id` 声明成了必填字段（更新类能力普遍如此，见
        `ponds_write.py::update_pond`）。把 `pond_id` 剔掉之后
        `validate_payload` 立刻报「缺少必填字段：塘口 ID」。

        判据因此收紧为：**只有当这个键不是处理器声明的字段时**，它才按"路径参数"
        处理。这样两种形态都对：

            `access.user.grants`  —— `user_id` 不是声明字段 → 剔出 body（修 F5）
            `pond.close`          —— `pond_id` 是声明字段   → 留在 body（保原行为）

        这也正是"请求体"该有的定义：**处理器声明了什么，body 里就有什么**。

        ## 与 `web/app.py` 的口径一致（这是本修法的依据）

        浏览器视图 `web/app.py::_make_view` 的 `view(**path_params)` **从 URL** 取
        路径参数，请求体里只有声明过的字段。Agent 网关这一处原本没遵守这个唯一约定，
        属于"同一件事两处描述"。

        ## 为什么不改 `to_tool_schema`（曾评估并否决）

        不把路径参数放进 schema 的 `required`，模型就没有渠道知道该传哪个 id；
        `arguments` 是它唯一的输入通道。**要改的是网关怎么解释 arguments**。

        ## 刻意不动 `payload`

        ``payload`` 是"调用方原始意图"，仍然保留全部键：它要用于
        `_invocation_params` 还原 URL（确认路径尤其需要，见 `_call_confirmed`），
        也是审计与参数指纹的输入。这里只修正 ``raw_payload`` 这个"请求体"口径。
        """
        declared: set[str] = set()
        for method_name in ("create_fields", "update_fields"):
            # 轻量替身（例如 `tests/test_agent_security.py::_ReadCapability`）可能不实现
            # 这两个方法。取不到就当作"没有声明字段"——对**只读**能力这正是事实
            # （runner 对只读能力跳过 validate_payload），而且这样不必为了一个判据
            # 去给每个替身加两个方法。
            method = getattr(capability, method_name, None)
            if callable(method):
                declared |= set(method())
        path_only = {
            key
            for key in (getattr(capability, "path_parameters", ()) or ())
            if key not in declared
        }
        if not path_only:
            return dict(arguments)
        return {key: value for key, value in arguments.items() if key not in path_only}

    @staticmethod
    def _invocation_params(
        capability: Capability, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """把工具参数还原为 HTTP Invocation 的路径与查询参数。"""
        query_schema = getattr(capability, "read_query_schema", None)
        allowed_query = set(query_schema().keys()) if callable(query_schema) else {
            "page",
            "page_size",
        }
        path_params = {
            key: arguments[key]
            for key in capability.path_parameters
            if key in arguments
        }
        if not capability.is_read:
            return path_params, {}
        unknown = set(arguments) - set(path_params) - allowed_query
        if unknown:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "查询参数未在资源声明中开放",
                data={"parameters": sorted(unknown)},
            )
        query = {key: arguments[key] for key in allowed_query if key in arguments}
        return path_params, query

    def _to_outcome(
        self, result: InvocationResult, capability: Capability | None = None
    ) -> TurnOutcome:
        capability_name = capability.name if capability is not None else ""
        resource = capability.resource if capability is not None else ""
        if result.kind == "confirmation_required":
            return TurnOutcome(
                kind="confirmation_required",
                message=result.message,
                confirmation=result.confirmation,
            )
        if result.kind == "read":
            return TurnOutcome(kind="read", data=result.data, message=result.message)
        return TurnOutcome(
            kind="executed",
            data=result.data,
            resource_id=result.resource_id,
            message=result.message,
            capability=capability_name,
            resource=resource,
            resource_url=self._frontend_url(resource),
        )

    def _frontend_url(self, resource: str) -> str:
        for capability in self._registry.all():
            if capability.resource != resource or capability.kind != "read":
                continue
            path = capability.path or ""
            return path[len("/api/v1") :] if path.startswith("/api/v1") else ""
        return ""

    def _audit_denied(
        self, capability: Capability, actor: ActorView, request_id: str, reason: str
    ) -> None:
        uow = self._uow_factory()
        try:
            with uow.begin() as tx:
                self._audit.write(
                    tx,
                    AuditEvent(
                        capability=capability.name,
                        domain=capability.domain,
                        result="denied",
                        reason=reason,
                        permission=capability.required_permission or "",
                    ),
                    request_id=request_id,
                    user_id=actor.user_id,
                    username=actor.username,
                    is_agent=actor.is_agent,
                    conversation_id=actor.conversation_id,
                )
        except Exception:  # noqa: BLE001 - 审计失败不能掩盖拒绝本身
            logger.exception(
                "审计写入失败 capability=%s request_id=%s actor_id=%s event=denied",
                capability.name,
                request_id,
                actor.user_id,
            )


    # -- 确认端点的服务端入口 ------------------------------------------------

    def confirm_pending(
        self,
        *,
        confirmation_id: int,
        token: str,
        actor: ActorView,
        request_id: str,
    ) -> TurnOutcome:
        """按确认 id + 令牌执行待确认的操作。

        与 `call_tool(confirmation_token=...)` 的区别：**参数由服务端从库里取**，
        不由调用方再次提交。少一次参数往返，就少一处"提交的参数与确认的不一致"的可能。

        契约要点：返回的 `kind` 只可能是 `executed` 或抛错，**不可能**是
        `confirmation_required`——用户已经确认过了，再要求一次确认就是死循环。
        """
        pending = self._confirmation.consume(
            token=token,
            actor=actor,
            capability_name="",  # 见下：这里按 id 定位，能力名从记录里取
            expected_id=confirmation_id,
        )
        capability = self._registry.get(str(pending["capability"]))
        confirmed_actor = ActorView(
            user_id=actor.user_id,
            username=actor.username,
            permissions=actor.permissions,
            role_codes=actor.role_codes,
            session_hash=actor.session_hash,
            is_agent=True,
            conversation_id=str(pending.get("conversation_id") or actor.conversation_id or ""),
            confirmation_id=int(pending["confirmation_id"]),
        )
        path_params, query = self._invocation_params(capability, pending["payload"])
        invocation = Invocation(
            capability_name=capability.name,
            payload=pending["payload"],
            path_params=path_params,
            query=query,
            # ★ 与 `call_tool` 同口径：路径参数**不属于请求体**。
            # 这里原先写的是 `dict(pending["payload"])`，于是 F5 的修复只覆盖了签名路径、
            # **漏了两条确认路径**。后果是那 3 条提权能力（`access.user.grants` /
            # `access.user.status` / `access.role.permissions`）的路径参数
            # `user_id` / `role_id` **不是处理器声明的字段**，一旦留在 raw_payload 里
            # 就被 `validate_payload` 判成「未知字段」：
            #
            #     实测（卡片 168，用户在界面上点「确认执行」）：
            #       payload_json = {"user_id": 7, "role_ids": [7, 9], "scope_ids": [2]}
            #       → VALIDATION_ERROR 请求包含不接受的字段：user_id
            #       → 卡片落成 failed，前端却只显示「确认令牌无效、已过期或已被使用」
            #
            # 即：**卡签得出来，却永远确认不了** —— 正是 F5 要根除的形态，只是换了条路径。
            raw_payload=self._body_payload(capability, pending["payload"]),
            idempotency_key=f"agent-confirmed:{pending['confirmation_id']}",
        )
        try:
            result = self._runner.invoke(invocation, confirmed_actor, request_id)
        except BaseException as error:  # noqa: BLE001 - claim 后任何失败都必须收口
            try:
                self._confirmation._store.mark_failed(
                    confirmation_id=int(pending["confirmation_id"]),
                    user_id=actor.user_id,
                    reason=str(getattr(error, "code", type(error).__name__)),
                )
            except Exception:  # noqa: BLE001 - 不遮蔽原始业务异常
                logger.exception(
                    "确认失败状态收口失败 confirmation_id=%s actor_id=%s error_type=%s",
                    pending["confirmation_id"],
                    actor.user_id,
                    type(error).__name__,
                )
            raise
        outcome = self._to_outcome(result, capability)
        outcome.conversation_id = str(pending.get("conversation_id") or "")
        return outcome

    def cancel_pending(
        self, *, confirmation_id: int, actor: ActorView, request_id: str
    ) -> None:
        """取消一个待确认操作。

        取消只是把状态收口，**没有业务副作用**——这正是"确认前什么都没写"的直接推论：
        一个待确认操作被取消时，没有什么需要回滚。
        """
        changed = self._confirmation._store.consume(
            confirmation_id=confirmation_id,
            user_id=actor.user_id,
            new_status="cancelled",
        )
        if not changed:
            raise DomainError(
                ErrorCode.CONFIRMATION_INVALID,
                "该操作不存在、已处理或已过期",
            )
        self._audit_cancelled(confirmation_id, actor, request_id)

    def _audit_cancelled(
        self, confirmation_id: int, actor: ActorView, request_id: str
    ) -> None:
        uow = self._uow_factory()
        try:
            with uow.begin() as tx:
                self._audit.write(
                    tx,
                    AuditEvent(
                        capability="<confirmation>",
                        domain="agent",
                        object_type="agent_confirmation",
                        object_id=confirmation_id,
                        result="denied",
                        reason="CANCELLED",
                    ),
                    request_id=request_id,
                    user_id=actor.user_id,
                    username=actor.username,
                    is_agent=actor.is_agent,
                    conversation_id=actor.conversation_id,
                )
        except Exception:  # noqa: BLE001 - 审计失败不能掩盖取消结果
            logger.exception(
                "审计写入失败 capability=%s request_id=%s actor_id=%s event=cancelled",
                "<confirmation>",
                request_id,
                actor.user_id,
            )


def summarize_tools_for_prompt(specs: list[ToolSpec]) -> str:
    """把工具清单压缩成一段可放进提示词的摘要。

    仅在**无法**通过原生工具注册通道传 schema 时使用（例如降级运行）。
    主路径是 `ctx.tools.register(defineTool({...}))`，schema 由 Harness 直接持有，
    不需要这层文本压缩——早期版本正是因为只能走文本描述，才不得不发明
    `n=`/`d=`/`m=`/`p=`/`r=`/`q=`/`a=` 那套紧凑键名来塞进环境变量。
    """
    lines: list[str] = []
    for spec in specs:
        required = ", ".join(spec.parameters.get("required", []))
        lines.append(f"- {spec.name}: {spec.description}" + (f"（必填：{required}）" if required else ""))
    return "\n".join(lines)


__all__ = [
    "AgentToolGateway",
    "ToolSpec",
    "TurnOutcome",
    "summarize_tools_for_prompt",
]
