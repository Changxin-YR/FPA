"""Agent 工具链路上「路径参数」与「请求体字段」的**唯一解释约定**。

## 这个文件为什么存在（F5，t2）

`Capability.to_tool_schema()` 会把 **路径参数** 加进工具 schema 的 `properties`
并放进 `required` —— 这是**对的**：模型只有一个 `arguments` 对象，没有独立的
"路径参数"通道，所以必须告诉它传 `user_id`。

错的是**网关怎么解释这些 arguments**：`AgentToolGateway.call_tool` 把**整个**
`arguments` 同时塞进 `path_params`（经 `_invocation_params`）与 `raw_payload`
（原样 `dict(arguments)`）。而执行器在 `runner.py:174` 用 `raw_payload` 走
`validate_payload`，后者拿 `Capability.create_fields()`（**处理器签名里声明过的
字段**）算"未知字段"——路径参数不在其中，于是被拒：

    POST /api/v1/agent/tools/access_user_grants/call
      arguments={"user_id": 7, "role_ids": [9], "scope_ids": [2]}
      → 400 VALIDATION_ERROR 请求包含不接受的字段：user_id

后果是**3 个高危写能力的 Agent 工具完全不可用**（`access.user.status` /
`access.user.grants` / `access.role.permissions`），HITL 卡永远签不出来。
浏览器侧不受影响：`web/app.py::_make_view` 的 `view(**path_params)` 从 **URL**
取路径参数，body 里只有声明过的字段 —— 所以这是"同一能力两套调用约定"，
而只有 Agent 那一套坏了。

## 修法与判据的方向（不要反过来）

修法是让 `gateway` **在构造 `raw_payload` 时剔除属于 `path_parameters` 的键**
（它们已经进了 `path_params`）。**不要**改 `to_tool_schema` 让它别再要求这些参数：
那会让模型没有渠道知道该传什么 id。

所以本文件的判据必须同时钉住两件相反的事：

1. **路径参数确实进了 `path_params`** —— 只把参数从 body 里丢掉是不行的，
   那会从"未知字段"变成"缺 user_id"（`admin.py::_path_id` 会抛 INTERNAL_ERROR），
   **仍然不可用**；
2. **普通字段照旧进 body**，而且**真正的未知字段仍然被拒**（fail closed 不能放松）。

另外每个用例都断言 `kind == "confirmation_required"` —— 这 3 条都是
`risk=HIGH` + `confirmation=ALWAYS`，"能走到签卡"才是"工具可用"的判据，
"没报 VALIDATION_ERROR" 不是。
"""

from __future__ import annotations

import pytest

from fpa.agent.gateway import AgentToolGateway
from fpa.kernel.capability import (
    AgentExposure,
    Capability,
    Confirmation,
    Field,
    FieldSet,
    HttpMethod,
    Registry,
    Risk,
)
from fpa.kernel.errors import DomainError, ErrorCode
from fpa.kernel.runner import ActorView, CapabilityRunner, Invocation, InvocationResult

# ---------------------------------------------------------------------------
# 夹具：与那 3 条真实能力**同形**的合成能力
#
# 刻意用合成能力而不是真注册表：真注册表依赖域模块（负责人 正在并行改
# sales/cost/warehouse/purchase），而本文件要证明的是**网关的解释约定**，
# 与哪个域无关。真能力的验证由 `tools/agent_e2e.py` 与手工 HTTP 取证负责。
# ---------------------------------------------------------------------------


class _Recorded:
    """处理器把"我实际收到了什么参数"记在这里。

    为什么不用 mock：本文件要断言的是**值**（`path_params["user_id"] == 7`、
    `role_ids == [9]`），而不是"某个方法被调用过"。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []


def _int_list(field: str, label: str) -> Field:
    return Field(
        type="array",
        label=label,
        required=True,
        max_length=32,
        items="integer",
    )


def _build_registry(recorded: _Recorded) -> Registry:
    """三条"路径参数 + 请求体字段"混合的高危能力。

    处理器签名**不含**路径参数（与真实实现一致：真实实现用
    `path_params: dict | None` 再 `_path_id(...)` 取），这正是 F5 的触发条件。
    """

    def replace_grants(
        tx,
        ctx,
        scope,
        path_params,
        role_ids,
        scope_ids,
    ) -> InvocationResult:
        recorded.calls.append(
            {
                "capability": "access.user.grants",
                "path_params": dict(path_params or {}),
                "role_ids": role_ids,
                "scope_ids": scope_ids,
            }
        )
        raise AssertionError("本用例只验到签卡为止，不应真的执行业务")

    def change_status(
        tx,
        ctx,
        scope,
        path_params,
        status,
        reason,
    ) -> InvocationResult:
        recorded.calls.append(
            {
                "capability": "access.user.status",
                "path_params": dict(path_params or {}),
                "status": status,
                "reason": reason,
            }
        )
        raise AssertionError("本用例只验到签卡为止，不应真的执行业务")

    def replace_role_permissions(
        tx,
        ctx,
        scope,
        path_params,
        permission_codes,
    ) -> InvocationResult:
        recorded.calls.append(
            {
                "capability": "access.role.permissions",
                "path_params": dict(path_params or {}),
                "permission_codes": permission_codes,
            }
        )
        raise AssertionError("本用例只验到签卡为止，不应真的执行业务")

    registry = Registry()
    registry.register(
        Capability(
            name="access.user.grants",
            title="调整角色与数据范围",
            domain="access",
            handler=replace_grants,
            method=HttpMethod.PUT,
            path="/api/v1/admin/users/{user_id}/grants",
            kind="action",
            required_permission="auth.user.manage",
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=AgentExposure.EXPOSED,
            fields=FieldSet(
                fields={
                    "role_ids": _int_list("role_ids", "角色"),
                    "scope_ids": _int_list("scope_ids", "数据范围"),
                }
            ),
        )
    )
    registry.register(
        Capability(
            name="access.user.status",
            title="启用或停用账号",
            domain="access",
            handler=change_status,
            method=HttpMethod.POST,
            path="/api/v1/admin/users/{user_id}/status",
            kind="action",
            required_permission="auth.user.manage",
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=AgentExposure.EXPOSED,
            fields=FieldSet(
                fields={
                    "status": Field(type="string", label="状态", required=True),
                    "reason": Field(type="string", label="变更原因", required=True),
                }
            ),
        )
    )
    registry.register(
        Capability(
            name="access.role.permissions",
            title="调整角色权限",
            domain="access",
            handler=replace_role_permissions,
            method=HttpMethod.PUT,
            path="/api/v1/admin/roles/{role_id}/permissions",
            kind="action",
            required_permission="auth.role.manage",
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=AgentExposure.EXPOSED,
            fields=FieldSet(
                fields={
                    "permission_codes": Field(
                        type="array",
                        label="权限",
                        required=True,
                        max_length=256,
                        items="string",
                    ),
                }
            ),
        )
    )
    return registry


class _ScopeResolver:
    """范围解析：这三条能力的 `scope=ScopePolicy.none()`，随便给个哨兵即可。"""

    def resolve(self, *, user_id: int, role_codes: frozenset[str]):
        return "SCOPE"


class _Gate:
    """确认闸门替身：记下"签发时的 payload / raw_payload"，并返回一张卡。

    同时实现 `consume` / `verify_params` / `_store.mark_failed`，让**确认路径**
    也能在不碰数据库的前提下走完（t2 的第二个缺陷就在这条路径上）。
    """

    def __init__(self) -> None:
        self.issued: list[dict] = []
        self.consumed: list[dict] = []
        self.failed: list[dict] = []
        self._seq = 0
        self._store = self

    def mark_failed(self, *, confirmation_id, user_id, reason):
        self.failed.append(
            {"confirmation_id": confirmation_id, "user_id": user_id, "reason": reason}
        )

    def issue(self, **kwargs):
        self._seq += 1
        self.issued.append(kwargs)
        return {
            "id": self._seq,
            "token": "tok-" + str(self._seq),
            "capability": kwargs["capability"].name,
            "title": kwargs["capability"].title,
            "target": kwargs["target"],
            "rows": [],
            "impact": kwargs["impact"],
            "expires_at": "2030-01-01T00:00:00Z",
        }

    def consume(self, *, token, actor, capability_name="", expected_id=None):
        self.consumed.append({"token": token, "expected_id": expected_id})
        # 注意：`issued` 里存的是 `issue(**kwargs)` 的**入参**，没有 `id`
        # （id 是 `issue` 的返回值）。所以这里用自增序号，与上面返回的 id 同源。
        issued = self.issued[-1]
        return {
            "confirmation_id": self._seq,
            "capability": issued["capability"].name,
            # ★ 与生产一致：卡片上存的是**完整**参数（含路径参数）。
            "payload": dict(issued["payload"]),
            "params_hash": "stub",
            "target_ref": issued["target"],
            "conversation_id": "conv-stub",
        }

    def verify_params(self, pending, raw_payload):
        return None


class _PassthroughUow:
    def begin(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield object()

        return scope()


def _gateway(recorded: _Recorded, gate: _Gate) -> tuple[AgentToolGateway, Registry]:
    registry = _build_registry(recorded)
    runner = CapabilityRunner(
        registry=registry,
        uow_factory=lambda: _PassthroughUow(),
        audit=type("_Audit", (), {"write": staticmethod(lambda *a, **k: None)})(),
        idempotency=type("_Idem", (), {"check": staticmethod(lambda *a, **k: None)})(),
        scope_resolver=_ScopeResolver(),
        confirmation_gate=gate,
    )
    return (
        AgentToolGateway(
            registry=registry,
            runner=runner,
            audit=type("_Audit2", (), {"write": staticmethod(lambda *a, **k: None)})(),
            confirmation=gate,
            uow_factory=lambda: _PassthroughUow(),
        ),
        registry,
    )


def _super_admin() -> ActorView:
    return ActorView(
        user_id=2,
        username="demo",
        role_codes=frozenset({"super_admin"}),
        permissions=frozenset({"auth.user.manage", "auth.role.manage"}),
        session_hash="sid-1",
        is_agent=True,
    )


# ---------------------------------------------------------------------------
# ★ 三条能力各一条：路径参数必须进 path_params，且必须走到"签卡"
# ---------------------------------------------------------------------------

#: 这 3 条正是 F5 里"完全不可用"的能力。**3 条都要覆盖**——
#: 只验一条正是上一轮 `farm.list` 溜过去的老坑。
_BROKEN_THREE = (
    (
        "access_user_grants",
        "access.user.grants",
        {"user_id": 7, "role_ids": [9], "scope_ids": [2]},
        "user_id",
        {"role_ids", "scope_ids"},
    ),
    (
        "access_user_status",
        "access.user.status",
        {"user_id": 7, "status": "disabled", "reason": "离职"},
        "user_id",
        {"status", "reason"},
    ),
    (
        "access_role_permissions",
        "access.role.permissions",
        {"role_id": 8, "permission_codes": ["pond.view"]},
        "role_id",
        {"permission_codes"},
    ),
)


@pytest.mark.parametrize(
    "tool_name,capability_name,arguments,path_key,body_keys",
    _BROKEN_THREE,
    ids=[item[0] for item in _BROKEN_THREE],
)
def test_path_argument_reaches_card_signing(
    tool_name: str,
    capability_name: str,
    arguments: dict,
    path_key: str,
    body_keys: set[str],
) -> None:
    """F5 主判据：这 3 条高危写能力必须能走到 `confirmation_required`。

    ★ 反向对照（把修复撤掉必须变红）：撤掉剔除逻辑后，`raw_payload` 里会带上
      `path_key`，`validate_payload` 抛 `VALIDATION_ERROR 请求包含不接受的字段`，
      本用例在 `outcome.kind` 那行就红 —— 我实测过，见 t2 报告。
    """
    recorded = _Recorded()
    gate = _Gate()
    gateway, _registry = _gateway(recorded, gate)

    outcome = gateway.call_tool(
        tool_name=tool_name,
        arguments=dict(arguments),
        actor=_super_admin(),
        request_id="t2-path-params",
    )

    # ① 不是 VALIDATION_ERROR，而是**能签卡**（这 3 条都是 always）
    assert outcome.kind == "confirmation_required", (
        f"{capability_name} 应走到签卡，实际 kind={outcome.kind!r}；"
        f"签出的卡={outcome.confirmation}"
    )
    assert outcome.confirmation is not None
    assert outcome.confirmation.get("token"), "确认卡必须带一次性令牌"

    # ② 路径参数**确实进了 path_params**（只把参数丢掉会变成"缺 id"，仍然不可用）
    assert gate.issued, "闸门应收到一次签发请求"
    issued = gate.issued[-1]
    assert issued["raw_payload"].get(path_key) is None, (
        f"路径参数 {path_key} 不该留在 raw_payload 里（它是 path，不是 body 字段）"
    )

    # ③ 普通字段照旧进 body
    assert body_keys <= set(issued["payload"]), (
        f"普通字段 {body_keys} 应全部进 payload，实际 {sorted(issued['payload'])}"
    )


def test_path_params_are_passed_to_the_invocation_not_dropped() -> None:
    """机制层：`Invocation.path_params` 必须含路径参数，`raw_payload` 必须不含。

    与上面那条互补：上面验"能签卡"，这条验"**为什么**能签卡"——
    参数是被**搬到 path_params**，不是被丢掉。
    """
    recorded = _Recorded()
    gate = _Gate()
    registry = _build_registry(recorded)
    seen: list[Invocation] = []

    class _CapturingRunner:
        def invoke(self, invocation: Invocation, actor: ActorView, request_id: str):
            seen.append(invocation)
            return InvocationResult(kind="confirmation_required", confirmation={"id": 1})

    gateway = AgentToolGateway(
        registry=registry,
        runner=_CapturingRunner(),
        audit=type("_Audit", (), {"write": staticmethod(lambda *a, **k: None)})(),
        confirmation=gate,
        uow_factory=lambda: _PassthroughUow(),
    )

    gateway.call_tool(
        tool_name="access_user_grants",
        arguments={"user_id": 7, "role_ids": [9], "scope_ids": [2]},
        actor=_super_admin(),
        request_id="t2-mechanism",
    )

    assert len(seen) == 1
    invocation = seen[0]
    assert invocation.path_params == {"user_id": 7}
    assert "user_id" not in (invocation.raw_payload or {}), (
        "路径参数必须在 raw_payload 里被剔除（否则 validate_payload 会拒它）"
    )
    assert invocation.raw_payload == {"role_ids": [9], "scope_ids": [2]}
    # payload 是"调用方原始意图"，路径参数仍在其中（用于还原 URL 与审计）
    assert invocation.payload == {"user_id": 7, "role_ids": [9], "scope_ids": [2]}


def test_path_param_as_string_is_still_accepted() -> None:
    """边界：模型可能把整数路径参数写成字符串（`"7"`）。

    只要它被**搬进 path_params**，就不再经过 `validate_payload` 的类型校验，
    服务层的 `int(raw)`（`admin.py::_path_id`）会收口。这条断言"字符串形式不会被
    当成未知字段拒掉"，避免修好了整数、漏了字符串。
    """
    recorded = _Recorded()
    gate = _Gate()
    gateway, _registry = _gateway(recorded, gate)

    outcome = gateway.call_tool(
        tool_name="access_user_grants",
        arguments={"user_id": "7", "role_ids": [9], "scope_ids": [2]},
        actor=_super_admin(),
        request_id="t2-string-path",
    )

    assert outcome.kind == "confirmation_required"


def test_unknown_fields_are_still_rejected() -> None:
    """fail closed 不能因为这次修复而放松。

    `user_id` 被放行是因为它**在能力声明的 `path_parameters` 里**；
    任何声明里没有的字段必须继续被 `validate_payload` 拒绝。
    """
    recorded = _Recorded()
    gate = _Gate()
    gateway, _registry = _gateway(recorded, gate)

    with pytest.raises(DomainError) as error:
        gateway.call_tool(
            tool_name="access_user_grants",
            arguments={
                "user_id": 7,
                "role_ids": [9],
                "scope_ids": [2],
                "is_super_admin": True,
            },
            actor=_super_admin(),
            request_id="t2-unknown-field",
        )

    assert error.value.code == ErrorCode.VALIDATION_ERROR
    assert "is_super_admin" in str(error.value)


def test_missing_path_argument_is_rejected_before_any_card_is_issued() -> None:
    """模型**没传**路径参数时，网关必须**当场拒绝** —— 不能签出一张永远执行不了的卡。

    ## 这条为什么从"现状断言"翻成了"期望断言"

    t2 当时如实钉住的是**当时的现状**：网关不拒绝，`_invocation_params` 只收集
    "存在的"路径参数 ⇒ `path_params={}` 一路放行 ⇒ 实际链路里**先签出一张卡**，
    用户点「确认执行」时才在服务层炸（`admin.py::_path_id` 抛 `INTERNAL_ERROR`，
    或 `ponds_write.py:199 int(None)` → 500）。代价是用户拿到一张**永远执行不了**的卡片，
    卡在、点了报错、原因看不懂、也没有补输的入口。

    原 docstring 里写明了"将来真的加了拦截，这条会红，从而被**有意识地**更新" ——
    当前迭代修这个缺口，就是那次更新。判据现在是**期望**：

    1. 抛 `VALIDATION_ERROR`，`data.fields` 指明缺哪个参数；
    2. ★ **一张卡都不签** —— 判据必须是"没有卡"，而不是"报了个错"。
       只报错但先把卡签出来，用户还是会看到一张点不动的卡。
    """
    recorded = _Recorded()
    gate = _Gate()
    gateway, _registry = _gateway(recorded, gate)

    with pytest.raises(DomainError) as excinfo:
        gateway.call_tool(
            tool_name="access_user_grants",
            arguments={"role_ids": [9], "scope_ids": [2]},
            actor=_super_admin(),
            request_id="t2-missing-path",
        )

    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert "user_id" in (excinfo.value.data or {}).get("fields", []), (
        f"错误里要指明缺哪个参数，实际 data={excinfo.value.data!r}"
    )
    # ★ 关键判据：缺参数时不得签发确认卡
    assert not gate.issued, (
        "缺路径参数时**不能**签发确认卡 —— 那会是一张永远执行不了的卡；"
        f"实际签了 {len(gate.issued)} 张：{gate.issued!r}"
    )


# ---------------------------------------------------------------------------
# ★ 确认路径：卡片必须**留着**路径参数，否则确认时拿不到它
# ---------------------------------------------------------------------------


def test_issued_card_keeps_the_path_argument_for_the_confirm_path() -> None:
    """F5 的第二个缺陷：签卡时把 `cleaned` 存进卡片 ⇒ 确认时路径参数丢了。

    ## 症状（实测，t2）

    `gateway.confirm_pending` 是**浏览器点「确认执行」**的入口，它只拿得到那张卡：
    `_invocation_params(capability, pending["payload"])` 从卡片里反推路径参数与 URL。

    签卡时存的是 `validate_payload` 的输出 `cleaned`，而（F5 修好之后）它的口径是
    **请求体字段** —— 路径参数按约定走 `invocation.path_params`，因此不在里面。
    于是确认时 `path_params == {}`：

        `pond.update` → 处理器收到 `pond_id=None`
        → `TypeError: int() argument must be ... not 'NoneType'`（`ponds_write.py:199`）
        → 500「服务器暂时无法处理请求」

    用户的体验是**卡片签得出来、点了确认却报 500** —— 比"工具不可用"更难受，
    因为它看起来"已经准备好了"。

    ## 判据

    卡片里必须能读出路径参数。用合成能力验证机制（真能力的端到端由
    `tests/test_agent_http_contract.py` 在真 HTTP 上覆盖）。
    """
    recorded = _Recorded()
    gate = _Gate()
    registry = _build_registry(recorded)
    seen_invocations: list[Invocation] = []

    # 用**真 runner**（它才会走到签卡、才会应用 validate_payload 与 path_params
    # 覆盖），外面包一层只做"记录 Invocation"的壳。
    inner = CapabilityRunner(
        registry=registry,
        uow_factory=lambda: _PassthroughUow(),
        audit=type("_Audit", (), {"write": staticmethod(lambda *a, **k: None)})(),
        idempotency=type("_Idem", (), {"check": staticmethod(lambda *a, **k: None)})(),
        scope_resolver=_ScopeResolver(),
        confirmation_gate=gate,
    )

    class _ObservingRunner:
        def invoke(self, invocation: Invocation, actor: ActorView, request_id: str):
            seen_invocations.append(invocation)
            # 签卡轮：真 runner 会返回 confirmation_required（因为它会把卡签出来）
            if actor.confirmation_id is None:
                return inner.invoke(invocation, actor, request_id)
            # 确认轮：语义是"已经确认过了"，直接当成功，不去碰数据库
            return InvocationResult(kind="executed", resource_id=7)

    gateway = AgentToolGateway(
        registry=registry,
        runner=_ObservingRunner(),
        audit=type("_Audit2", (), {"write": staticmethod(lambda *a, **k: None)})(),
        confirmation=gate,
        uow_factory=lambda: _PassthroughUow(),
    )

    gateway.call_tool(
        tool_name="access_user_grants",
        arguments={"user_id": 7, "role_ids": [9], "scope_ids": [2]},
        actor=_super_admin(),
        request_id="t2-card-keeps-path",
    )

    issued_payload = gate.issued[-1]["payload"]
    assert issued_payload.get("user_id") == 7, (
        "卡片必须留着路径参数，否则确认时 `_invocation_params` 反推不出 URL："
        f"实际 payload={issued_payload}"
    )
    # 请求体字段当然也要在（卡片是"这次调用要用的完整参数"）
    assert issued_payload.get("role_ids") == [9]
    assert issued_payload.get("scope_ids") == [2]

    # ★ 端到端确认路径：这条路径在缺陷版本上抛 TypeError / VALIDATION_ERROR
    outcome = gateway.confirm_pending(
        confirmation_id=1,
        token="tok-1",
        actor=_super_admin(),
        request_id="t2-confirm-path",
    )
    assert outcome.kind == "executed", f"确认路径应成功执行，实际 {outcome.kind!r}"
    assert seen_invocations, "确认路径必须把 Invocation 交给 runner"
    confirmed = seen_invocations[-1]
    assert confirmed.path_params == {"user_id": 7}, (
        f"确认时路径参数必须还原出来，实际 {confirmed.path_params}"
    )

    # ★★ 这条断言原先写的是"raw_payload 必须包含 user_id" —— 它把**缺陷本身**
    #    钉成了期望值。`user_id` 对 `access.user.grants` 是**路径参数**、
    #    **不是**处理器声明的字段，所以它一旦留在 raw_payload 里，
    #    真 runner 的 `validate_payload` 就会判「未知字段」：
    #
    #        实测（卡片 168，用户在界面上点「确认执行」）：
    #          VALIDATION_ERROR 请求包含不接受的字段：user_id
    #          → 卡片落成 failed，前端只显示「确认令牌无效、已过期或已被使用」
    #
    #    而本用例的"确认轮"用的是合成 runner（刻意不碰数据库），它**绕过了
    #    validate_payload**，于是这个错误在单测里完全看不见 —— 典型的
    #    "夹具比被测代码宽松，缺陷被夹具吃掉"。判据因此必须落在
    #    **raw_payload 的形状**上（与签名路径同口径），这是不依赖数据库就能钉死的那一半。
    assert confirmed.raw_payload == {"role_ids": [9], "scope_ids": [2]}, (
        "确认路径的 raw_payload 必须与 `call_tool` 同口径：**路径参数不进请求体**。"
        f"实际 {confirmed.raw_payload}"
    )
    assert "user_id" not in confirmed.raw_payload, (
        "`user_id` 是路径参数、不是处理器声明字段，留在 raw_payload 里会让"
        "真实 `validate_payload` 判成未知字段 ⇒ 卡片永远确认不了"
    )
