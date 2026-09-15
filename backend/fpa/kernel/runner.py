"""能力执行器：人工入口与 Agent 入口的**唯一**落点。

```
人工:  Vue 页面 → REST 路由 ─┐
                            ├→ CapabilityRunner.invoke() → Business Service
Agent: Harness → Gateway   ─┘
```

这里做的事，是"声明一次、机械派生"里**唯一必须手写**的那部分：把声明变成一次受控调用。
其余（REST 路由、前端表单、权限码、DataScope 谓词、幂等、确认闸门、审计、工具 schema）
全部从 `Capability` 派生。

执行顺序（顺序本身就是安全设计）：

    1. 解析能力          ← 未注册 → CAPABILITY_NOT_FOUND（Agent 的"固定业务路由"就靠它）
    2. 校验请求体        ← schema 校验（与前端表单、Agent Tool schema 同源）
    3. 人工专属检查      ← HUMAN_ONLY → 拒绝 Agent 调用
    4. 权限 + 范围解析   ← 第二层防御，失败时不得签发确认卡
    5. 确认闸门          ← HIGH 风险 + Agent 入口 → 返回待确认，**什么都不写**
    6. 幂等预留          ← 独立事务（见 kernel/idempotency.py 的顺序说明）
    7. 业务事务开始
    8. 业务服务执行      ← 第三层防御在这里（服务自己再校验一次）
    9. 审计（同事务）    ← 业务回滚则审计一起消失
   10. 提交
   11. 回读校验          ← **提交成功 ≠ 结果正确**，必须读回来确认
   12. 幂等收口          ← 提交成功之后才标记 completed
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from fpa.kernel.capability import (
    Capability,
    Confirmation,
    HandlerResult,
    Registry,
    Risk,
    run_invariants,
    validate_payload,
)
from fpa.kernel.errors import DomainError, ErrorCode, forbidden, unauthenticated
from fpa.kernel.idempotency import IdempotencyStore, Reservation
from fpa.kernel.scope import Scope
from fpa.kernel.uow import RequestContext, UnitOfWork
from fpa.kernel.invariants import _EXCLUDE_ID_KEY
from fpa.kernel.workflow import RESOURCES

logger = logging.getLogger(__name__)


class ScopeResolver(Protocol):
    """把用户解析成数据范围。由 access 域实现。

    刻意做成 Protocol：内核不该知道 RBAC 表长什么样。测试可以注入固定范围的替身。
    """

    def resolve(self, *, user_id: int, role_codes: frozenset[str]) -> Scope: ...


@dataclass(slots=True)
class ActorView:
    """执行器视角下的操作者。"""

    user_id: int
    username: str
    permissions: frozenset[str]
    role_codes: frozenset[str] = frozenset()
    session_hash: str = ""
    is_agent: bool = False
    conversation_id: str | None = None
    confirmation_id: int | None = None
    ip_address: str = ""

    def has(self, permission: str | None) -> bool:
        return permission is None or permission in self.permissions

    @property
    def is_super_admin(self) -> bool:
        return "super_admin" in self.role_codes


@dataclass(slots=True)
class Invocation:
    """一次能力调用。"""

    capability_name: str
    payload: dict[str, Any] = field(default_factory=dict)
    #: 路径参数（如 `pond_id`）。与 payload 分开，因为它们的校验语义不同。
    path_params: dict[str, Any] = field(default_factory=dict)
    #: 查询参数（只读能力用）。
    query: dict[str, Any] = field(default_factory=dict)
    #: 提交的原始参数（未含服务端补全值）。
    #:
    #: 确认令牌的 `params_hash` 覆盖**这一份**（裁决 Q11）：因为服务端补全值在签发
    #: 确认卡片时还不存在，无法预知；而且"用户看到什么就确认什么"要求哈希覆盖的
    #: 就是卡片上渲染的那份数据。
    raw_payload: dict[str, Any] | None = None
    idempotency_key: str | None = None


@dataclass(slots=True)
class InvocationResult:
    """调用结果。``kind`` 是判别联合的判别键，与 `docs/WRITE_CONTRACT.md` 一致。"""

    kind: str  # executed | failed | confirmation_required | human_only | read
    data: Any = None
    resource_id: int | None = None
    resource_type: str = ""
    message: str = ""
    replayed: bool = False
    confirmation: dict[str, Any] | None = None


class ConfirmationGate(Protocol):
    """确认闸门的接口；由 agent 域实现。"""

    def issue(
        self,
        *,
        capability: Capability,
        actor: ActorView,
        payload: dict[str, Any],
        raw_payload: dict[str, Any],
        request_id: str,
        summary: str,
        target: str,
        impact: list[str],
    ) -> dict[str, Any]: ...

    def consume(self, *, token: str, actor: ActorView, capability_name: str) -> dict[str, Any]: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...


def _validate_query_values(spec: Capability, query: dict[str, Any]) -> None:
    """只读能力的查询参数：**取值**必须落在声明里（不只校验参数名）。

    为什么必须有这条：`kind="status"` 的候选值来自状态机、是枚举；非法值
    一路落到 SQL 只是匹配不到行 —— 接口回 200 + 空列表，调用方会把
    「查不到」当成「没有」。实测 2026-09-15：模型把 `batch_status` 填成
    `"active"`（一个不存在的状态），据此向用户宣称「养殖中 0 个」——
    真实是 1 个。这类“静默给出错误答案”比直接报错危险得多。

    只校验**声明了候选值的参数**（`read_query_schema()` 里带 `enum` 的），
    其余（page/page_size、ref、date_range、boolean）原样放行；空值放行
    （前端未选中的筛选会发空串）。
    """
    schema = spec.read_query_schema()
    for name, value in query.items():
        field = schema.get(name)
        if not isinstance(field, dict):
            continue
        allowed = field.get("enum")
        if not allowed or value is None or value == "":
            continue
        if value not in allowed:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                f"查询参数 {name} 的取值不在允许范围内",
                data={"parameter": name, "allowed": list(allowed)},
            )


class CapabilityRunner:
    """把 `Capability` 声明变成一次受控的业务调用。"""

    def __init__(
        self,
        *,
        registry: Registry,
        uow_factory: UnitOfWorkFactory,
        audit: Any,
        idempotency: IdempotencyStore,
        scope_resolver: ScopeResolver,
        confirmation_gate: ConfirmationGate | None = None,
    ) -> None:
        self.registry = registry
        self._uow_factory = uow_factory
        self._audit = audit
        self._idempotency = idempotency
        self._scope_resolver = scope_resolver
        self._confirmation_gate = confirmation_gate

    # -- 主流程 ---------------------------------------------------------------

    def invoke(self, invocation: Invocation, actor: ActorView, request_id: str) -> InvocationResult:
        spec = self.registry.get(invocation.capability_name)

        if not actor.user_id:
            raise unauthenticated()

        # 2. 校验请求体（校验模型与前端表单、Agent Tool schema 同源）
        raw = invocation.raw_payload if invocation.raw_payload is not None else invocation.payload
        if spec.is_read:
            cleaned: dict[str, Any] = {}
            _validate_query_values(spec, invocation.query)
        else:
            cleaned = validate_payload(spec, raw, for_update=spec.kind == "update")

        # 3. 人工专属
        if spec.refuses_agent and actor.is_agent:
            raise DomainError(
                ErrorCode.HUMAN_ONLY,
                "该操作只能由本人在系统页面完成，智能体不能代劳",
                data={"capability": spec.name},
            )

        # 4. 权限与数据范围预检：失败时不能先签发确认卡。
        self._require_permission(spec, actor)
        scope = self._scope_resolver.resolve(
            user_id=actor.user_id, role_codes=actor.role_codes
        )

        # 5. 确认闸门
        needs_confirmation = (
            actor.is_agent
            and spec.is_write
            and spec.effective_confirmation is Confirmation.ALWAYS
            and actor.confirmation_id is None
        )
        if needs_confirmation:
            gate = self._confirmation_gate
            if gate is None:
                raise DomainError(
                    ErrorCode.SERVICE_UNAVAILABLE,
                    "人工确认服务暂不可用，请稍后重试",
                )
            card_payload = dict(cleaned, **{
                key: value
                for key, value in invocation.path_params.items()
                if key not in cleaned
            })
            # 卡片可读化：由**声明这条能力的域**提供解析器，把裸 id 换成业务名字
            # （`user_id=7` → 「周海霞（seller）」）。见 `Capability.confirmation_labels`。
            labels = self._confirmation_labels(spec, card_payload)
            issued = gate.issue(
                capability=spec,
                actor=actor,
                # ★ 卡片必须存**完整**那份参数（`payload`），不能存 `cleaned`。
                #
                # `cleaned` 是 `validate_payload` 的输出。修好 F5 之后它的口径是
                # **请求体字段**——路径参数（`{pond_id}` 这类）按约定走
                # `invocation.path_params`，因此**不在** `cleaned` 里。
                #
                # 而确认路径（`gateway.confirm_pending`）只拿得到这张卡：
                # 它用 `_invocation_params(capability, pending["payload"])` 反推
                # 路径参数与 URL。于是路径参数一旦不进 payload，确认时就拿不到：
                #
                #     实测（t2）`pond.update`：签卡正常 → 用户点确认 →
                #     `invocation.path_params == {}` → 处理器收到 `pond_id=None`
                #     → `TypeError: int(None)`（`ponds_write.py:199`）
                #     → 500「服务器暂时无法处理请求」。
                #
                # 这与本方法上面那句注释的口径也一致：`payload` 是"这次调用要用的
                # 完整参数"，`raw_payload` 只是"请求体"那一份。
                payload=card_payload,
                raw_payload=dict(raw),
                request_id=request_id,
                summary=spec.title,
                target=str(labels.get("target") or self._target_label(spec, invocation)),
                impact=self._impact_of(spec),
                row_labels={
                    str(k): str(v) for k, v in (labels.get("rows") or {}).items()
                },
            )
            self._audit_denied_or_pending(spec, actor, request_id, cleaned, "pending")
            return InvocationResult(
                kind="confirmation_required",
                message=f"即将{spec.title}，确认后立即生效。",
                confirmation=issued,
            )

        # 6-12. 幂等 + 业务事务。确认执行会以新请求重新经过上面的权限与范围检查。
        if spec.requires_idempotency_key:
            return self._invoke_idempotent(
                spec=spec,
                invocation=invocation,
                actor=actor,
                request_id=request_id,
                scope=scope,
                cleaned=cleaned,
            )
        return self._invoke_once(
            spec=spec,
            invocation=invocation,
            actor=actor,
            request_id=request_id,
            scope=scope,
            cleaned=cleaned,
        )

    # -- 无幂等路径 -----------------------------------------------------------

    def _invoke_once(
        self,
        *,
        spec: Capability,
        invocation: Invocation,
        actor: ActorView,
        request_id: str,
        scope: Scope,
        cleaned: dict[str, Any],
    ) -> InvocationResult:
        uow = self._uow_factory()
        try:
            with uow.begin() as tx:
                before = self._maybe_load_before(spec, invocation, tx, actor, scope, cleaned)
                result = self._call_service(
                    spec, tx, actor, scope, invocation, cleaned, request_id
                )
                run_invariants(
                    spec,
                    tx=tx,
                    scope=scope,
                    actor_id=actor.user_id,
                    payload={
                        **cleaned,
                        **self._invariant_extra(spec, result, invocation),
                    },
                    before=before,
                )
                after = self._reload_after(spec, tx, actor, scope, result)
                self._write_audit(tx, spec, actor, request_id, cleaned, before, after, "success")
        except DomainError as error:
            self._audit_failure(spec, actor, request_id, cleaned, error)
            raise
        return InvocationResult(
            kind="read" if spec.is_read else "executed",
            data=result.data,
            resource_id=result.resource_id,
            resource_type=spec.domain,
            message=result.message,
        )

    # -- 幂等路径 -------------------------------------------------------------

    def _invoke_idempotent(
        self,
        *,
        spec: Capability,
        invocation: Invocation,
        actor: ActorView,
        request_id: str,
        scope: Scope,
        cleaned: dict[str, Any],
    ) -> InvocationResult:
        key = invocation.idempotency_key
        if not key:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                f"{spec.title}是重复敏感操作，请求必须携带 Idempotency-Key",
                data={"header": "Idempotency-Key"},
            )

        # 预留用「原始参数」的哈希：服务端补全值（farm_id 等）在预留时还不存在。
        reservation: Reservation = self._idempotency.reserve(
            user_id=actor.user_id,
            capability=spec.name,
            key=key,
            payload=self._hash_material(spec, invocation),
        )

        if reservation.is_replay:
            return self._replay(spec, reservation, actor, scope)

        try:
            return self._invoke_after_reservation(
                spec=spec,
                invocation=invocation,
                actor=actor,
                request_id=request_id,
                scope=scope,
                cleaned=cleaned,
                reservation=reservation,
            )
        except BaseException as error:  # noqa: BLE001 - 见下
            # **任何**异常都要收口，不只是 DomainError。
            #
            # 原实现只捕 DomainError，于是非业务异常（例如处理器签名不匹配导致的
            # TypeError）会让预留永久停在 `processing` —— 那个键会被锁到 TTL 到期
            # （默认 30 分钟），**修好 bug 之后仍然不能重试**。
            #
            # 捕获 BaseException 是安全的：异常路径意味着业务事务已回滚，
            # 没有任何写入发生，所以把预留标成 failed 不会掩盖已产生的副作用。
            # KeyboardInterrupt 这类也一并收口——留一个锁死的键没有任何好处。
            commit_unknown = (
                isinstance(error, DomainError)
                and error.code == ErrorCode.IDEMPOTENCY_IN_PROGRESS
                and isinstance(error.data, dict)
                and error.data.get("result_code") == "COMMIT_UNKNOWN"
            )
            if not commit_unknown:
                reason = str(getattr(error, "code", type(error).__name__))
                try:
                    self._idempotency.mark_failed(
                        user_id=actor.user_id,
                        capability=spec.name,
                        key_digest=reservation.key_hash,
                        reason=reason,
                    )
                except Exception:  # noqa: BLE001 - 收口失败不能掩盖原始异常
                    logger.exception(
                        "幂等失败状态收口失败 capability=%s request_id=%s actor_id=%s error_type=%s",
                        spec.name,
                        request_id,
                        actor.user_id,
                        type(error).__name__,
                    )
            # ★ 失败审计：与 `_invoke` 的失败路径**同一处实现**（`_audit_failure`）。
            #
            # 为什么这条必须补（实测缺口，不是预防性设计）：审计的价值**主要在失败路径** ——
            # 成功写入本身在业务表里可见，而"谁试图做了什么但被拒"只存在于审计里。
            # 幂等路径的失败尤其如此：它**往往正是重放/并发冲突**（同一个
            # `Idempotency-Key` 的第二次调用），这类事件恰恰最该被审计。
            #
            # 与 `_write_audit`（成功路径）同一口径：`AuditPolicy.summary()` 的语义是
            # "审计行无条件写、策略只管 diff"，所以失败路径也**不判策略**。
            #
            # 两条路径各调一次同一个 helper、不复制实现 —— 这正是本项目反对
            # "两处描述同一件事"的落点：这里只多一行**调用**。
            self._audit_failure(spec, actor, request_id, cleaned, error)
            raise

    def _invoke_after_reservation(
        self,
        *,
        spec: Capability,
        invocation: Invocation,
        actor: ActorView,
        request_id: str,
        scope: Scope,
        cleaned: dict[str, Any],
        reservation: Reservation,
    ) -> InvocationResult:
        uow = self._uow_factory()
        with uow.begin() as tx:
            before = self._maybe_load_before(spec, invocation, tx, actor, scope, cleaned)
            result = self._call_service(spec, tx, actor, scope, invocation, cleaned, request_id)
            run_invariants(
                spec,
                tx=tx,
                scope=scope,
                actor_id=actor.user_id,
                payload={
                    **cleaned,
                    **self._invariant_extra(spec, result, invocation),
                },
                before=before,
            )
            after = self._reload_after(spec, tx, actor, scope, result)
            self._write_audit(tx, spec, actor, request_id, cleaned, before, after, "success")
            resource_id = result.resource_id

        # ★ 顺序要点：业务事务已提交，才写 completed。
        if resource_id is not None:
            try:
                self._idempotency.mark_completed(
                    user_id=actor.user_id,
                    capability=spec.name,
                    key_digest=reservation.key_hash,
                    resource_type=spec.domain,
                    resource_id=int(resource_id),
                )
            except BaseException as error:  # noqa: BLE001 - 业务已提交，状态可能未知
                try:
                    self._idempotency.mark_commit_unknown(
                        user_id=actor.user_id,
                        capability=spec.name,
                        key_digest=reservation.key_hash,
                    )
                except Exception:  # noqa: BLE001 - 不遮蔽提交状态未知
                    logger.exception(
                        "幂等提交状态未知且无法收口 capability=%s request_id=%s actor_id=%s",
                        spec.name,
                        request_id,
                        actor.user_id,
                    )
                raise DomainError(
                    ErrorCode.IDEMPOTENCY_IN_PROGRESS,
                    "业务可能已经提交但幂等状态未知，请核对业务数据后再决定是否重试",
                    data={"result_code": "COMMIT_UNKNOWN"},
                ) from error
        return InvocationResult(
            kind="executed",
            data=result.data,
            resource_id=resource_id,
            resource_type=spec.domain,
            message=result.message,
        )

    def _replay(
        self, spec: Capability, reservation: Reservation, actor: ActorView, scope: Scope
    ) -> InvocationResult:
        """回放：**从业务表读实际数据**，而不是回放序列化快照。

        这一条是与早期版本的关键差别：早期版本回放 `response_json` 快照，业务表后来被改过
        的话，回放会给出过期数据。
        """
        resource_type, resource_id = reservation.replayed_resource  # type: ignore[misc]
        # 用与 `_reload_after` **同一个**属性名。曾经这里写的是
        # `__fpa_read_by_id__`，而能力声明上挂的是 `__fpa_load_by_id__`，
        # 于是 callable() 为假、静默退化成只返回 {"id": ...} ——
        # 回放数据看起来"有"，但来源不可追溯。一个字母的差别，无报错、无崩溃。
        raw_loader = getattr(spec.handler, "__fpa_load_by_id__", None)
        data: Any = {"id": resource_id}
        if callable(raw_loader):
            uow = self._uow_factory()
            with uow.begin() as tx:
                data = self._resolve(spec, raw_loader)(tx, scope=scope, record_id=resource_id)
        return InvocationResult(
            kind="executed",
            data=data,
            resource_id=resource_id,
            resource_type=resource_type,
            message=f"{spec.title}的重复请求已被识别，未重复执行。",
            replayed=True,
        )

    # -- 服务调用（第三层防御的入口）------------------------------------------

    def _resolve(self, spec: Capability, func: Any) -> Any:
        """把能力声明里的函数对象解析成可调用对象。

        能力声明里给的通常是**类上的函数**（`PondService.create`），它是未绑定的，
        直接调用会报 `missing 1 required positional argument: 'self'`。解析规则：

          * 已绑定（模块级函数、或 `instance.method`）→ 原样返回；
          * 未绑定但有 `service_factory` → 用工厂取实例后 `getattr` 出绑定方法；
          * 未绑定且无工厂 → 抛**可操作的**内部错误，而不是让它表现为莫名的 TypeError。

        为什么不从 `func.__qualname__` 拆类名隐式实例化：对嵌套类、别名、装饰器都不健壮，
        而且把"这个能力属于哪个服务"这个信息藏进了字符串里。显式声明 `service_factory`
        让装配关系写在声明处，一眼可见。
        """
        if getattr(func, "__self__", None) is not None:
            return func

        factory = spec.service_factory
        if factory is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"能力 {spec.name} 使用了实例方法 {func.__name__}，但没有声明 service_factory。"
                "请在能力声明里加上 service_factory=..., 或改为传入已绑定的方法。",
            )
        bound = getattr(factory(), func.__name__, None)
        if not callable(bound):
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"能力 {spec.name}：service_factory 返回的对象上没有 {func.__name__} 方法",
            )
        return bound
    @staticmethod
    def _accepted_params(handler: Any) -> frozenset[str]:
        """处理器签名接受的参数名。

        用它做裁剪，而不是给处理器加 `**kwargs` 兜底——兜底会连"参数名拼错"
        一起吞掉，那是早期版本 `**_: Any` 的毛病。
        """
        import inspect

        try:
            signature = inspect.signature(handler)
        except (TypeError, ValueError):
            return frozenset()
        return frozenset(signature.parameters)

    def _call_service(
        self,
        spec: Capability,
        tx: UnitOfWork,
        actor: ActorView,
        scope: Scope,
        invocation: Invocation,
        cleaned: dict[str, Any],
        request_id: str,
    ) -> HandlerResult:
        context = RequestContext(
            request_id=request_id,
            user_id=actor.user_id,
            username=actor.username,
            session_hash=actor.session_hash,
            conversation_id=actor.conversation_id,
            is_agent=actor.is_agent,
            extra={"confirmation_id": actor.confirmation_id},
        )
        handler = self._resolve(spec, spec.handler)
        kwargs: dict[str, Any] = {
            "tx": tx,
            "ctx": _ServiceCall(spec=spec, actor=actor, scope=scope, request=context),
            "scope": scope,
        }

        # 可选参数：只在处理器签名真的声明了它们时才传。
        #
        # 无条件传会让**每一个**不接受这两个参数的处理器 TypeError ——
        # `(self, tx, ctx, scope, code, name)` 这样的签名很常见。
        # 按签名裁剪，而不是"让处理器容忍多余参数"：后者要靠 `**kwargs` 兜底，
        # 而兜底会连"参数名拼错"一起吞掉。
        accepts = self._accepted_params(handler)
        for optional, value in (("path_params", invocation.path_params), ("query", invocation.query)):
            if value and optional in accepts:
                kwargs[optional] = value

        # 业务字段：**全部**声明过的都交给服务，缺的显式传 None。
        #
        # 为什么不"缺就不传"：「非必填」在 HTTP 层的统一含义应当是**值为 None**。
        # 若改成"缺就不传"，每个处理器的每个可选参数都必须写 `= None` 默认值——
        # 漏写不会报错，只在有人省略该字段时才炸，而调用栈离根因很远。
        # 实测踩过：`capacity_mu` 省略 → TypeError: missing 1 required positional argument。
        #
        # ★ 这一行是必须的：没有它，下面的 setdefault 会把**每个**字段都设成 None，
        #   包括客户端明明传了的。上一版补丁把它吃掉了，症状是 `int(None)`。
        kwargs.update(cleaned)

        # 路径参数属于路由事实，优先于请求体中的同名字段。
        # 更新处理器通常把 `{resource_id}` 声明成业务参数；只传 path_params
        # 字典会让部分字段更新落成 TypeError/500。
        #
        # ★ 收集在这里，但**应用挪到最后**（见下面那段）。
        path_overrides = {
            key: value
            for key, value in invocation.path_params.items()
            if key in accepts
        }

        # 未声明的字段已在 validate_payload 阶段被拒，这里只补齐声明过但没传的。
        for key in spec.create_fields() if not spec.is_read else {}:
            kwargs.setdefault(key, None)

        # ★ 路径参数**最后**应用，且是硬覆盖。
        #
        # 原顺序是"先给路径参数赋值 → 再 setdefault(None)"，而 `setdefault` 对
        # **已存在且为 None** 的键不生效、对**不存在**的键会补一个 None ——
        # 两条路都通向同一个故障：只要 `cleaned` 里没有该路径参数
        # （确认路径就是这样：HTTP body 只有一个令牌），`setdefault` 就把它设成
        # None，**而它刚刚才被路径参数赋过真值**。
        #
        # 实测症状（t2）：`pond.update` 确认时
        #   `TypeError: int() argument must be ... not 'NoneType'`（`ponds_write.py:199`）
        #   → 500「服务器暂时无法处理请求」，而卡片明明签得出来。
        #
        # 挪到最后之后，"路径参数优先于请求体同名字段"这句注释才真正成立：
        # 路由事实不能被"缺省填 None"覆盖。
        kwargs.update(path_overrides)

        # 刻意**不捕获** TypeError。
        #
        # 初版在这里包了一层 `except TypeError` 来把"参数不匹配"翻译成友好错误，
        # 但它无法区分"调用时参数不匹配"和"handler 内部抛了 TypeError"——
        # 实测后果是处理器里一个真实的类型错误被误报成 `INTERNAL_ERROR`，
        # 掩盖了真正的故障点。这与早期版本用 `**_: Any` 吞掉多余参数是同一个毛病。
        #
        # 参数不匹配属于**声明期错误**，由 `tools/check_capabilities.py` 静态检查；
        # 运行期 handler 抛什么就冒泡什么：DomainError → 业务错误，其他 → 500 + 日志。
        result = handler(**kwargs)
        if not isinstance(result, HandlerResult):
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"能力 {spec.name} 的处理器必须返回 HandlerResult",
            )
        return result

    # -- 辅助 -----------------------------------------------------------------

    @staticmethod
    def _hash_material(spec: Capability, invocation: Invocation) -> dict[str, Any]:
        """幂等哈希的覆盖范围。

        用**原始参数** + 路径参数 + 查询参数。刻意不含服务端补全值（见 `Invocation.raw_payload`
        的说明）：同一用户在两个不同数据范围下提交同样参数时，幂等键本就应各自独立，
        而"数据范围"由 user_id 参与唯一键，已经在数据库层隔离了。
        """
        raw = invocation.raw_payload if invocation.raw_payload is not None else invocation.payload
        return {
            "capability": spec.name,
            "path_params": invocation.path_params,
            "query": invocation.query,
            "payload": raw,
        }

    @staticmethod
    def _require_permission(spec: Capability, actor: ActorView) -> None:
        if not actor.has(spec.required_permission):
            raise forbidden(required=spec.required_permission)

    def _invariant_extra(
        self,
        spec: Capability,
        result: HandlerResult,
        invocation: Invocation,
    ) -> dict[str, Any]:
        """处理器可以通过返回值补充不变量所需的上下文。

        `NoNegativeStock` 需要知道"本次要写哪些账本增量"、`PeriodOpen` 需要"期间的
        起止日期"，而那些值都是服务算出来的，不是请求体里的。让服务显式回传比让
        不变量自己再算一遍更可靠 —— 两边各算一遍必然出现不一致。

        除服务回传的键之外，这里还固定注入四个执行器才知道的事实：
        `_resource` / `_resource_table` / `_resource_id` / `_resource_key_column`。
        `OptimisticLock` 与 `StateTransition(machine="*")` 靠它们工作：
        内核的 `Capability` 只声明**资源名**，不声明表名 —— 表名在 `Resource` 上，
        只有执行器同时拿得到两者。注入它们之后，能力声明侧就不需要把"我这张表叫什么"
        再抄一遍（抄一遍就是两处描述同一件事）。
        """
        extra: dict[str, Any] = {}
        if isinstance(result.data, dict):
            provided = result.data.get("_invariant_context")
            if isinstance(provided, dict):
                extra.update(provided)

        if spec.resource:
            extra["_resource"] = spec.resource
            resource = RESOURCES.find(spec.resource)
            if resource is not None:
                extra["_resource_table"] = resource.table or spec.resource
                extra["_resource_key_column"] = resource.key_column

        # `_resource_id` 与 `_invariant_exclude_id` 是**两个不同的问题**，这里刻意
        # 分两次取值（以前是一条 `路径参数 or resource_id` 表达式同时喂给两者）：
        #
        #   * `_resource_id` 回答的是"`OptimisticLock` / `StateTransition(machine=\"*\")`
        #     该去回读哪一行"。对"路径参数是父对象、写入的是子行"的能力（例如
        #     `pond_status_change.verify`：路径是 `/{pond_id}/status-changes/{request_id}/verify`），
        #     要判的是**父对象**（塘口的状态），所以路径参数优先 —— 这与
        #     `_maybe_load_before` 用 `_first_id(path_params)` 取 before 快照**保持同一口径**：
        #     两个键若指向不同行，`before` 与 `_resource_id` 就会自相矛盾。
        #
        #   * `_invariant_exclude_id` 回答的是"`UniqueCode` / `AtMostOnePending` /
        #     `NoOverlappingSource` 该把哪一行从"冲突"里排除掉"，而那**必须是本次写入
        #     产生的那一行** —— 正是 `HandlerResult.resource_id` 的定义
        #     （`WRITE_CONTRACT.md` 规则 2：回读的就是"我们想要的那一行"）。
        #     路径参数是**客户端提交的目标**，它可能根本不是本能力写入的那一行。
        #
        # 顺序颠倒的实测后果（真缺陷，不是理论）：`pond_status_change.request` 的路径参数是
        # `pond_id`，而它 INSERT 的是**申请行**。排除键被指到塘口 id 上，于是刚插入的申请行
        # 不被排除，`AtMostOnePending` 把**自己的新行**判定成"已存在一条待核验申请" ——
        # 首次合法调用就被拒。该能力因此长期**无法**声明 §4 #21，规则退化成建议
        # （`registry_reconcile.py` 的 [B] 表）。
        #
        # 为什么"路径参数优先"当初看起来合理：它假设"路径参数就是被写的行"。这个假设对所有
        # `*.verify` / `*.update`（写入行 = 路径指向的行）都成立，只对"创建子行"的能力不成立
        # ——而后者恰恰是 `AtMostOnePending` 唯一的使用场景。所以两台机器要分别取值。
        target_id = _first_id(invocation.path_params) or result.resource_id
        if target_id is not None:
            extra["_resource_id"] = int(target_id)
        if result.resource_id is not None:
            extra[_EXCLUDE_ID_KEY] = int(result.resource_id)
        elif target_id is not None:
            # 能力没回传 `resource_id`（少数只读/动作能力）——退化到路径参数，
            # 与改动前的行为一致。**没有**这一支会让这些能力失去排除键，
            # 而 `UniqueCode` 等的降级方向是"更严格"（可能误报），所以不能省。
            extra[_EXCLUDE_ID_KEY] = int(target_id)
        return extra

    def _confirmation_labels(self, spec: Capability, payload: dict[str, Any]) -> dict[str, Any]:
        """让能力声明的解析器把卡片上的裸 id 换成业务名字（见 `Capability.confirmation_labels`）。

        三条刻意的边界：

        * **未声明就返回空**：既有能力（没写这个钩子的）卡片行为一个字节都不变。
        * **失败不阻断**：解析器抛错只记日志、退回裸 id。确认闸门是**安全设施**，
          绝不能因为一个显示层的美化失败而让用户签不出卡 —— 那是拿安全换美观。
        * **独立只读事务**：这是签发前的一次旁路查询，不参与业务事务、也不写入。
        """
        resolver = getattr(spec, "confirmation_labels", None)
        if resolver is None:
            return {}
        try:
            with self._uow_factory().begin() as tx:
                resolved = resolver(tx, payload)
            return dict(resolved or {})
        except Exception:  # noqa: BLE001
            logger.exception(
                "确认卡可读化失败，退回裸 id（不阻断签卡）capability=%s", spec.name
            )
            return {}

    @staticmethod
    def _target_label(spec: Capability, invocation: Invocation) -> str:
        parts: list[str] = []
        for key, value in invocation.path_params.items():
            parts.append(f"{key}={value}")
        for key in ("code", "name", "quantity", "amount"):
            if key in invocation.payload:
                parts.append(f"{key}={invocation.payload[key]}")
        return " ".join(parts) or spec.title

    @staticmethod
    def _impact_of(spec: Capability) -> list[str]:
        """确认卡片上"会产生什么影响"的说明。

        故意写成静态文案：动态影响（例如"库存将从 470 扣到 420"）需要在**准备阶段**
        查库，而准备阶段查到的值与执行时的值可能不同（中间有人动了库存）。
        与其展示一个可能过期的数字，不如说明会做什么，并让执行后的回读给出真实数字。
        """
        if spec.domain == "warehouse":
            return ["产生库存流水", "更新库存余额"]
        if spec.domain == "production":
            return ["写入业务记录", "更新塘口/批次状态"]
        if spec.domain in {"purchase", "sales"}:
            return ["写入单据", "影响应付/应收余额"]
        if spec.domain == "cost":
            return ["影响成本归集"]
        if spec.domain in {"access", "identity"}:
            # ★ 这两条原先落到最后的静态兜底 `["写入业务数据"]` —— 对**提权**来说
            #   那句话是错的：它既没说"提权"，也让人以为只是一次普通写入。
            #   实测 `access.user.grants` 的卡片就写着"写入业务数据"。
            return ["改变账号/角色的权限配置（提权类操作）", "立即影响该账号能做什么"]
        return ["写入业务数据"]

    # -- 回读与审计 -----------------------------------------------------------

    def _maybe_load_before(
        self,
        spec: Capability,
        invocation: Invocation,
        tx: UnitOfWork,
        actor: ActorView,
        scope: Scope,
        cleaned: dict[str, Any],
    ) -> dict[str, Any] | None:
        """审计与不变量都需要"改动前的样子"。

        读取方式复用服务在 `__fpa_load_by_id__` 上声明的函数，避免 runner 自己写 SQL
        （架构约束：runner 不碰 SQL）。
        """
        raw_loader = getattr(spec.handler, "__fpa_load_by_id__", None)
        if not callable(raw_loader):
            return None
        record_id = _first_id(invocation.path_params) or cleaned.get("_target_id")
        if record_id is None:
            return None
        return self._resolve(spec, raw_loader)(tx, scope=scope, record_id=int(record_id))

    def _reload_after(
        self,
        spec: Capability,
        tx: UnitOfWork,
        actor: ActorView,
        scope: Scope,
        result: HandlerResult,
    ) -> dict[str, Any] | None:
        """★ 提交前回读，确认写入真的落了库。

        这是 `docs/WRITE_CONTRACT.md` 规则 2 的实现：``executed`` 不是"我们调用了 INSERT"，
        而是"读回来的行确实是我们想要的样子"。

        回读失败 → 抛 INTERNAL_ERROR，**绝不降级成成功**。
        """
        if spec.is_read or result.resource_id is None:
            return None
        raw_loader = getattr(spec.handler, "__fpa_load_by_id__", None)
        if not callable(raw_loader):
            # 能力声明了会返回 resource_id，却没有提供回读函数 → 声明与实现不一致。
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"能力 {spec.name} 声称写入成功但没有提供回读函数，无法确认写入结果",
            )
        row = self._resolve(spec, raw_loader)(tx, scope=scope, record_id=int(result.resource_id))
        if row is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"能力 {spec.name} 报告写入成功但回读不到该记录，写入未生效",
                data={"resource_id": result.resource_id},
            )
        return row

    def _write_audit(
        self,
        tx: UnitOfWork,
        spec: Capability,
        actor: ActorView,
        request_id: str,
        cleaned: dict[str, Any],
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        result: str,
    ) -> None:
        """审计与业务**同事务**：业务回滚则审计一起消失。

        这样审计表里出现的每一条 success 都对应一次真实提交。

        注意 `tx` 是**显式参数**，不是从上下文里取的。之前的版本用 ContextVar 传递当前
        事务，那是多余的一层间接——tx 本来就在调用栈上。少一层隐式状态就少一处"某个
        分支忘了设置它"的可能。
        """
        from fpa.kernel.audit import AuditEvent

        self._audit.write(
            tx,
            AuditEvent(
                capability=spec.name,
                domain=spec.domain,
                object_type=spec.domain,
                object_id=(after or before or {}).get("id"),
                object_ref=str((after or before or {}).get("code") or ""),
                result=result,
                permission=spec.required_permission or "",
                before=before if spec.audit.before_after else None,
                after=after if spec.audit.before_after else None,
            ),
            request_id=request_id,
            user_id=actor.user_id,
            username=actor.username,
            is_agent=actor.is_agent,
            conversation_id=actor.conversation_id,
            confirmation_id=actor.confirmation_id,
        )

    def _audit_failure(
        self,
        spec: Capability,
        actor: ActorView,
        request_id: str,
        cleaned: dict[str, Any],
        error: BaseException,
    ) -> None:
        """失败的审计走**独立事务**。

        为什么这里可以不同事务：失败事件需要被记下来，而业务事务已经回滚了。
        此时"记一条失败"本身就是独立的事实，不存在与业务写入的一致性问题。

        ## 形参为什么是 `BaseException` 而不是 `DomainError`

        **两条失败路径共用这一个实现**，而它们的异常类型不同：
          * `_invoke`（非幂等路径）只捕 `DomainError` → 传进来的一定是它；
          * `_invoke_idempotent`（幂等路径）捕 `BaseException`（理由见那里的注释：
            非业务异常会让预留永久卡在 `processing`），所以传进来的可能是 `TypeError` 之类。

        早先签名写死 `DomainError`，于是幂等路径**根本没法调它** —— 那正是
        "可幂等写能力的失败没有审计行"这条缺口的成因（`cost_e2e.py` 曾把它记成"已知缺口"）。
        签名放宽到 `BaseException` 之后，两条路径**只多一行调用**，判定与落库仍只有一份实现。

        `reason` 的口径与 `mark_failed` 一致（`str(getattr(error, "code", type(error).__name__))`）：
        有业务错误码就用它，否则用异常类名 —— "失败原因"正是失败审计最该回答的问题。
        """
        from fpa.kernel.audit import AuditEvent

        reason = str(getattr(error, "code", type(error).__name__))
        uow = self._uow_factory()
        try:
            with uow.begin() as tx:
                self._audit.write(
                    tx,
                    AuditEvent(
                        capability=spec.name,
                        domain=spec.domain,
                        result="failure",
                        reason=reason,
                        permission=spec.required_permission or "",
                        detail={"payload": cleaned},
                    ),
                    request_id=request_id,
                    user_id=actor.user_id,
                    username=actor.username,
                    is_agent=actor.is_agent,
                    conversation_id=actor.conversation_id,
                    confirmation_id=actor.confirmation_id,
                )
        except Exception:  # noqa: BLE001 - 审计失败不能掩盖原始业务异常
            logger.exception(
                "审计写入失败 capability=%s request_id=%s actor_id=%s event=failure",
                spec.name,
                request_id,
                actor.user_id,
            )

    def _audit_denied_or_pending(
        self,
        spec: Capability,
        actor: ActorView,
        request_id: str,
        cleaned: dict[str, Any],
        result: str,
    ) -> None:
        from fpa.kernel.audit import AuditEvent

        uow = self._uow_factory()
        with uow.begin() as tx:
            self._audit.write(
                tx,
                AuditEvent(
                    capability=spec.name,
                    domain=spec.domain,
                    result=result,
                    permission=spec.required_permission or "",
                    detail={"payload": cleaned},
                ),
                request_id=request_id,
                user_id=actor.user_id,
                username=actor.username,
                is_agent=actor.is_agent,
                conversation_id=actor.conversation_id,
                confirmation_id=actor.confirmation_id,
            )


@dataclass(frozen=True, slots=True)
class _ServiceCall:
    """传给服务方法的上下文对象（第三层防御的入口）。"""

    spec: Capability
    actor: ActorView
    scope: Scope
    request: RequestContext

    def require(self, permission: str | None) -> None:
        """服务自己的权限校验。不信任 runner。"""
        if permission is not None and not self.actor.has(permission):
            raise forbidden(required=permission)

    def assert_row(self, row: dict[str, Any] | None, *, what: str = "目标数据") -> dict[str, Any]:
        """服务自己检查"这一行在不在范围内"。不信任 runner 传入的 scope 是否被执行过。"""
        from fpa.kernel.errors import not_found
        from fpa.kernel.scope import ScopePolicy

        if row is None:
            raise not_found(what)
        if not self.scope.allows_row(row):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED,
                f"{what}不在当前账号的数据范围内",
            )
        return row


def _first_id(path_params: dict[str, Any]) -> int | None:
    for key, value in path_params.items():
        if key.endswith("_id"):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


__all__ = [
    "ActorView",
    "CapabilityRunner",
    "ConfirmationGate",
    "Invocation",
    "InvocationResult",
    "ScopeResolver",
    "UnitOfWorkFactory",
]
