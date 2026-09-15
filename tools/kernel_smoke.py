"""内核第一轮自检：证明早期版本的高危缺陷在新内核里被结构性消除。

直接运行：``python tools/kernel_smoke.py``
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fpa.kernel import capability as cap  # noqa: E402
from fpa.kernel.errors import DomainError  # noqa: E402
from fpa.kernel.fields import (  # noqa: E402
    Choice,
    collect_fields,
    f_enum,
    f_num,
    f_ref,
    f_str,
)

PASS = "  PASS"
FAIL = "  FAIL"


class FakeUnitOfWork:
    """极简 UnitOfWork 替身：只实现不变量会用到的那部分。

    这本身就是一条设计验证——**不变量能脱离 MySQL 测试**，而早期版本把它们写在
    紧贴 SQL 的仓储里（`早期版本 production_store.py:273-280`），只能靠真实库验证。
    """

    def __init__(self, query_one_result: dict | None = None) -> None:
        self.query_one_result = query_one_result
        self.queries: list[tuple[str, tuple]] = []

    def query_one(self, sql: str, params=None):
        self.queries.append((sql, tuple(params or ())))
        return self.query_one_result


#: 定义在模块级——这既是真实用法，也是 `get_type_hints()` 能解析标注的前提。
#: 内嵌在函数里的类会让标注退化成字符串而无法收集（第一版内核就踩过这个坑）。
class DemoService:
    def create(
        self,
        tx,
        scope,
        name: f_str("塘口名称", required=True, max_length=120),
        capacity_mu: f_num("面积（亩）", minimum=0, required=True, list_column=True),
        area_id: f_ref("所属区域", "area", required=True),
        status: f_enum("状态", (Choice("build", "待建设"), Choice("stocked", "已放苗"))),
    ) -> cap.HandlerResult:
        return cap.HandlerResult(data={}, resource_id=1)

    def create_scoped(
        self,
        tx,
        scope,
        code: f_str("塘口编号", required=True),
        name: f_str("塘口名称", required=True),
        farm_id: f_ref("所属基地", "farm", required=True, readonly=True),
        area_id: f_ref("所属区域", "area", required=True, readonly=True),
    ) -> cap.HandlerResult:
        return cap.HandlerResult(data={}, resource_id=2)


def section(number: int, title: str) -> None:
    print(f"\n=== {number}. {title} ===")


def main() -> int:
    failures = 0

    section(1, "Scope：farm 范围缺少分租键必须报错")
    print("  早期版本 common/security/data_scope.py 在同样输入下静默 return '1=0', []")
    from fpa.kernel.scope import ScopeEntry

    try:
        ScopeEntry.from_row({"code": "farm-all", "scope_type": "farm", "farm_id": None})
        print(f"{FAIL}: 没有报错，缺陷仍在")
        failures += 1
    except DomainError as error:
        print(f"{PASS}: {error.code} -> {error.message}")

    section(2, "Scope：area 范围正确渲染 SQL 谓词")
    from fpa.kernel.scope import Scope, ScopePolicy

    scope = Scope.from_rows(
        [{"scope_type": "area", "area_id": 5}, {"scope_type": "area", "area_id": 2}],
        user_id=7,
    )
    rendered = ScopePolicy.resource("area_id").render(scope, alias="p")
    expected = "p.area_id IN (%s,%s)"
    if rendered.fragment == expected and list(rendered.params) == [2, 5]:
        print(f"{PASS}: {rendered.fragment}  params={list(rendered.params)}（id 已去重排序）")
    else:
        print(f"{FAIL}: 得到 {rendered.fragment} / {list(rendered.params)}")
        failures += 1

    section(3, "Scope：空范围无法构造（不允许静默空集）")
    try:
        Scope(allow_all=False, entries=(), user_id=7)
        print(f"{FAIL}: 没有报错")
        failures += 1
    except DomainError as error:
        print(f"{PASS}: {error.code}")

    section(4, "Scope：personal 范围的归属必须与当前账号一致")
    from fpa.kernel.scope import ScopeType

    try:
        Scope(allow_all=False, entries=[ScopeEntry(ScopeType.PERSONAL, 999)], user_id=7)
        print(f"{FAIL}: 没有报错")
        failures += 1
    except DomainError as error:
        print(f"{PASS}: {error.code}")

    section(5, "Capability：字段从 Annotated 标注自动收集（无需第二份字段表）")

    # 注意：不能用 `DemoService.create.__annotations__`——本模块带
    # `from __future__ import annotations`，那里的值是字符串。
    fields = cap._field_annotations(DemoService.create)
    spec = cap.Capability(
        name="pond.create",
        title="新建塘口",
        domain="master_data",
        handler=DemoService.create,
        method=cap.HttpMethod.POST,
        path="/api/v1/ponds",
        kind="create",
        required_permission="pond.create",
        risk=cap.Risk.NORMAL,
        agent_exposure=cap.AgentExposure.EXPOSED,
        idempotent=True,
        fields=fields,
    )
    expected_fields = ["name", "capacity_mu", "area_id", "status"]
    expected_required = {"name", "capacity_mu", "area_id"}
    actual_required = set(spec.required_create_fields())
    if list(spec.fields.fields) == expected_fields and actual_required == expected_required:
        print(f"{PASS}: fields={expected_fields}")
        print(f"        required={sorted(actual_required)}")
    else:
        print(f"{FAIL}: fields={list(spec.fields.fields)} required={sorted(actual_required)}")
        failures += 1
    if "tx" not in spec.fields.fields and "scope" not in spec.fields.fields:
        print(f"{PASS}: 框架参数 tx/scope 未被误当成业务字段")
    else:
        print(f"{FAIL}: 框架参数混进了字段表")
        failures += 1

    schema = spec.to_tool_schema()
    if schema["required"] == ["name", "capacity_mu", "area_id"] and schema["additionalProperties"] is False:
        print(f"{PASS}: Agent Tool schema 自动派生 -> required={schema['required']}")
    else:
        print(f"{FAIL}: tool schema 不对：{schema}")
        failures += 1
    if schema["properties"]["area_id"]["type"] == "integer":
        print(f"{PASS}: ref 字段映射为 integer")
    else:
        print(f"{FAIL}: ref 字段类型错误")
        failures += 1

    section(6, "确认闸门：HIGH 风险默认 ALWAYS，不再看 URL")
    confirm_spec = cap.Capability(
        name="purchase.confirm",
        title="确认采购单",
        domain="purchase",
        handler=DemoService.create,
        method=cap.HttpMethod.POST,
        path="/api/v1/purchases/{purchase_id}/confirm",
        kind="action",
        required_permission="purchase.confirm",
        risk=cap.Risk.HIGH,
    )
    if confirm_spec.effective_confirmation is cap.Confirmation.ALWAYS:
        print(f"{PASS}: risk=HIGH -> confirmation=ALWAYS（声明生效）")
    else:
        print(f"{FAIL}: {confirm_spec.effective_confirmation}")
        failures += 1
    # 高危能力**不**自动要求幂等键——这一条曾经的实现是错的，已推翻：
    # 高危能力走确认路径，确认令牌本身一次性（`WHERE status='pending'` 原子占位），
    # 再要求幂等键是重复约束，且会把负担推给客户端（准备阶段签发的键没有持久化）。
    # 确认路径的幂等性由 confirmation_id 派生的键保证（见 agent/gateway.py）。
    if not confirm_spec.requires_idempotency_key:
        print(f"{PASS}: 高危能力不额外要求幂等键（一次性由确认令牌保证）")
    else:
        print(f"{FAIL}: 高危能力不应自动要求幂等键")
        failures += 1
    explicit_idem = cap.Capability(
        name="pond.create",
        title="新建塘口",
        domain="master_data",
        handler=DemoService.create,
        method=cap.HttpMethod.POST,
        path="/api/v1/ponds",
        kind="create",
        risk=cap.Risk.NORMAL,
        idempotent=True,
    )
    if explicit_idem.requires_idempotency_key:
        print(f"{PASS}: 显式声明 idempotent=True 的能力要求幂等键")
    else:
        print(f"{FAIL}: 显式声明的幂等键要求丢失")
        failures += 1
    other = cap.Capability(
        name="purchase.update",
        title="修改采购单",
        domain="purchase",
        handler=DemoService.create,
        method=cap.HttpMethod.PATCH,
        path="/api/v1/purchases/{purchase_id}",
        kind="update",
        risk=cap.Risk.HIGH,
        confirmation=cap.Confirmation.NEVER,
    )
    if other.effective_confirmation is cap.Confirmation.NEVER:
        print(f"{PASS}: 显式 NEVER 可覆盖默认（这是一个有意识的决定，不是碰巧躲过正则）")
    else:
        print(f"{FAIL}: 显式覆盖失效")
        failures += 1

    section(7, "Agent 暴露默认关闭")
    default_spec = cap.Capability(
        name="pond.list",
        title="塘口列表",
        domain="master_data",
        handler=DemoService.create,
        method=cap.HttpMethod.GET,
        path="/api/v1/ponds",
        kind="read",
        risk=cap.Risk.READ,
    )
    if default_spec.agent_exposure is cap.AgentExposure.HIDDEN:
        print(f"{PASS}: 默认 HIDDEN，新能力不会自动获得 AI 可调用权")
    else:
        print(f"{FAIL}: {default_spec.agent_exposure}")
        failures += 1

    section(8, "声明自检：READ 必须是 GET；HUMAN_ONLY 必须说明理由")
    checks = 0
    try:
        cap.Capability(
            name="bad.read", title="x", domain="d", handler=DemoService.create,
            method=cap.HttpMethod.POST, path="/x", kind="read", risk=cap.Risk.READ,
        )
        print(f"{FAIL}: READ+POST 未被拒绝")
        failures += 1
    except ValueError:
        checks += 1
    try:
        cap.Capability(
            name="bad.human", title="x", domain="d", handler=DemoService.create,
            method=cap.HttpMethod.POST, path="/x", kind="action",
            agent_exposure=cap.AgentExposure.HUMAN_ONLY,
        )
        print(f"{FAIL}: HUMAN_ONLY 无理由未被拒绝")
        failures += 1
    except ValueError:
        checks += 1
    if checks == 2:
        print(f"{PASS}: 两条声明约束都生效")

    section(9, "注册表：L1 权限过滤")
    registry = cap.Registry()
    # 让三个能力各带不同权限，才能验证过滤（无权限声明的能力按设计对所有人可见）。
    no_permission = cap.Capability(
        name="meta.bootstrap",
        title="启动元数据",
        domain="meta",
        handler=DemoService.create,
        method=cap.HttpMethod.GET,
        path="/api/v1/meta/capabilities",
        kind="read",
        risk=cap.Risk.READ,
        agent_exposure=cap.AgentExposure.HIDDEN,
    )
    for item in (
        spec,
        confirm_spec,
        dataclasses.replace(default_spec, required_permission="pond.read"),
        no_permission,
    ):
        registry.register(item)

    visible = registry.visible_to(frozenset({"pond.create"}))
    names = sorted(item.name for item in visible)
    if names == ["meta.bootstrap", "pond.create"]:
        print(f"{PASS}: 仅有 pond.create 权限时可见 = {names}")
        print(f"        （无权限声明的 meta.bootstrap 对所有人可见，符合设计）")
    else:
        print(f"{FAIL}: {names}")
        failures += 1

    tools = registry.agent_tools(frozenset({"pond.create"}))
    if [item.name for item in tools] == ["pond.create"]:
        print(f"{PASS}: Agent 工具清单是可见能力的子集（按 exposed 声明过滤掉 bootstrap 与 human_only）")
    else:
        print(f"{FAIL}: {[item.name for item in tools]}")
        failures += 1

    admin_only = cap.Capability(
        name="access.grant_role",
        title="分配系统角色",
        domain="access",
        handler=DemoService.create,
        method=cap.HttpMethod.POST,
        path="/api/v1/users/{user_id}/roles",
        kind="action",
        required_permission="auth.role.manage",
        risk=cap.Risk.HIGH,
        agent_exposure=cap.AgentExposure.HUMAN_ONLY,
        description="修改权限模型属于人工专属操作，智能体不得代劳",
    )
    registry.register(admin_only)
    if admin_only.tool_name() and not admin_only.exposed_to_agent and admin_only.refuses_agent:
        print(f"{PASS}: HUMAN_ONLY 能力不生成工具（即便权限满足）")
    else:
        print(f"{FAIL}: HUMAN_ONLY 未生效")
        failures += 1

    section(10, "readonly 字段：看得见、不可提交、不进 Agent schema")
    print("  对应裁决 Q7：create 不接收 organization_id/farm_id/area_id，由 ScopeResolver 解析")
    scoped = cap.Capability(
        name="pond.create",
        title="新建塘口",
        domain="master_data",
        handler=DemoService.create_scoped,
        method=cap.HttpMethod.POST,
        path="/api/v1/ponds",
        kind="create",
        required_permission="pond.create",
        risk=cap.Risk.NORMAL,
        agent_exposure=cap.AgentExposure.EXPOSED,
        idempotent=True,
        fields=cap._field_annotations(DemoService.create_scoped),
    )
    if set(scoped.create_fields()) == {"code", "name"}:
        print(f"{PASS}: 可提交字段只有 {sorted(scoped.create_fields())}（farm_id/area_id 被排除）")
    else:
        print(f"{FAIL}: 可提交字段 = {sorted(scoped.create_fields())}")
        failures += 1
    if set(scoped.scope_fields()) == {"farm_id", "area_id"}:
        print(f"{PASS}: 分租字段被识别为服务端解析：{sorted(scoped.scope_fields())}")
    else:
        print(f"{FAIL}: {sorted(scoped.scope_fields())}")
        failures += 1
    meta_keys = [item["key"] for item in scoped.to_meta()["fields"]]
    if "farm_id" in meta_keys and "area_id" in meta_keys:
        print(f"{PASS}: 但前端元数据里仍然展示它们（用户看得见自己的数据范围）")
    else:
        print(f"{FAIL}: readonly 字段没进元数据：{meta_keys}")
        failures += 1
    tool_props = scoped.to_tool_schema()["properties"]
    if "farm_id" not in tool_props and "area_id" not in tool_props:
        print(f"{PASS}: Agent Tool schema 里没有它们（模型不该编造 farm_id）")
    else:
        print(f"{FAIL}: tool schema 含 readonly 字段：{sorted(tool_props)}")
        failures += 1
    try:
        cap.validate_payload(scoped, {"code": "P-1", "name": "一号塘", "farm_id": 3})
        print(f"{FAIL}: 提交 readonly 字段未被拒绝")
        failures += 1
    except DomainError as error:
        if error.code == "FORBIDDEN" and error.data and "readonly_fields" in error.data:
            print(f"{PASS}: 提交 readonly 字段被拒 → {error.message}")
        else:
            print(f"{FAIL}: 错误码/结构不对：{error.code} {error.data}")
            failures += 1

    section(11, "不变量：可脱离 MySQL 测试")
    from fpa.kernel.invariants import (
        DistinctActors,
        NoNegativeStock,
        PeriodOpen,
        RequiredField,
        RequiredWhen,
        StateTransition,
    )

    scope_for_check = Scope.all_data(user_id=1)
    distinct = DistinctActors()
    try:
        distinct.check(
            tx=FakeUnitOfWork(),
            scope=scope_for_check,
            actor_id=1,
            payload={},
            before={"created_by": 1, "verified_by": None},
        )
        print(f"{FAIL}: 经办人自审未被拒绝")
        failures += 1
    except DomainError as error:
        print(f"{PASS}: 经办人不能核验自己 → {error.message}")
    try:
        distinct.check(
            tx=FakeUnitOfWork(),
            scope=scope_for_check,
            actor_id=2,
            payload={},
            before={"created_by": 1},
        )
        print(f"{PASS}: 他人核验放行")
    except DomainError as error:
        print(f"{FAIL}: 他人核验被误拒：{error.message}")
        failures += 1

    required = RequiredField(fields=("source_ref",), labels=("来源单号",))
    try:
        required.check(tx=FakeUnitOfWork(), scope=scope_for_check, actor_id=1, payload={"source_ref": "  "}, before=None)
        print(f"{FAIL}: 空白来源单号未被拒绝")
        failures += 1
    except DomainError as error:
        print(f"{PASS}: 空白来源单号被拒 → {error.message}")

    stock = NoNegativeStock(
        table="inventory_ledger",
        columns=("quantity_delta",),
        group_by=("warehouse_id", "material_id", "inventory_lot_id"),
        label="物料库存",
    )
    # 后验语义：假返回值代表**写入之后**的账本余额（库存 30 扣 50 → −20），
    # 不再拿增量行做算术（旧形态 `available + delta` 会算成 available_before + 2×delta）。
    fake = FakeUnitOfWork(query_one_result={"quantity_delta": -20})
    try:
        stock.check(
            tx=fake,
            scope=scope_for_check,
            actor_id=1,
            payload={
                "_ledger_lines": [
                    {"warehouse_id": 1, "material_id": 2, "inventory_lot_id": 3, "quantity_delta": -50}
                ]
            },
            before=None,
        )
        print(f"{FAIL}: 负库存未被拒绝")
        failures += 1
    except DomainError as error:
        print(f"{PASS}: 库存 30 扣 50 后余额 −20 被拒 → {error.message}")
    if fake.queries and "FOR UPDATE" in fake.queries[0][0]:
        print(f"{PASS}: 查询用了 FOR UPDATE 行锁（继承早期版本已验证的写法）")
    else:
        print(f"{FAIL}: 缺少 FOR UPDATE：{fake.queries}")
        failures += 1

    # 缺账本行必须**显式报错**，不得静默放行 —— 旧形态 `if not lines: return` 会让
    # "忘了回传增量行"变成"校验通过"，负库存一路写进库。
    fake_missing = FakeUnitOfWork(query_one_result={"quantity_delta": 0})
    try:
        stock.check(
            tx=fake_missing,
            scope=scope_for_check,
            actor_id=1,
            payload={},
            before=None,
        )
        print(f"{FAIL}: 缺少账本行时静默放行（负库存会被写进库）")
        failures += 1
    except DomainError as error:
        print(f"{PASS}: 缺少账本行显式报错而非放行 → {error.message}")

    # 多列形态：存塘账本要同时保证数量与重量都不为负（registry §4 #2）
    stock_multi = NoNegativeStock(
        table="batch_stock_records",
        columns=("quantity_delta", "weight_delta_kg"),
        group_by=("batch_id", "pond_id"),
        label="塘内存塘",
    )
    fake_weight = FakeUnitOfWork(query_one_result={"quantity_delta": 0, "weight_delta_kg": -5})
    try:
        stock_multi.check(
            tx=fake_weight,
            scope=scope_for_check,
            actor_id=1,
            payload={"_ledger_lines": [{"batch_id": 1, "pond_id": 2}]},
            before=None,
        )
        print(f"{FAIL}: 重量为负未被拒绝（单列签名表达不了的场景）")
        failures += 1
    except DomainError as error:
        print(f"{PASS}: 数量为 0 但重量 −5 被拒 → {error.message}")

    fake_ok = FakeUnitOfWork(query_one_result={"quantity_delta": 30})
    try:
        stock.check(
            tx=fake_ok,
            scope=scope_for_check,
            actor_id=1,
            payload={
                "_ledger_lines": [
                    {"warehouse_id": 1, "material_id": 2, "inventory_lot_id": 3, "quantity_delta": -50}
                ]
            },
            before=None,
        )
        print(f"{PASS}: 库存 80 扣 50 后余额 30 放行")
    except DomainError as error:
        print(f"{FAIL}: 合法扣减被拒：{error.message}")
        failures += 1
    if list(fake_ok.queries[0][1]) == [1, 2, 3]:
        print(f"{PASS}: 分组键按声明顺序绑定 → {list(fake_ok.queries[0][1])}")
    else:
        print(f"{FAIL}: 参数绑定错误 {fake_ok.queries[0][1]}")
        failures += 1

    period = PeriodOpen(date_field="occurred_on")
    fake_period = FakeUnitOfWork(query_one_result={"status": "closed"})
    try:
        period.check(
            tx=fake_period,
            scope=scope_for_check,
            actor_id=1,
            payload={"occurred_on": "2026-08-15", "organization_id": 1},
            before=None,
        )
        print(f"{FAIL}: 已关账期间未被拒绝")
        failures += 1
    except DomainError as error:
        print(f"{PASS}: 已关账期间被拒 → {error.message}")
    # 期间反查必须带租户键与确定行序：`accounting_periods` 每个企业各有一份自己的
    # 期间行，而没有 `ORDER BY` 的 `LIMIT 1` 命中哪一行由存储顺序决定
    # （`tools/repro_period_open_tenant.py` 两个方向都能复现）。
    period_sql, period_params = fake_period.queries[-1]
    if "organization_id=%s" in period_sql and "ORDER BY" in period_sql and period_params[0] == 1:
        print(f"{PASS}: 期间反查带租户键与 ORDER BY → {list(period_params)}")
    else:
        print(f"{FAIL}: 期间反查缺租户键或行序：{period_sql} / {list(period_params)}")
        failures += 1
    try:
        period.check(
            tx=FakeUnitOfWork(query_one_result={"status": "open"}),
            scope=scope_for_check,
            actor_id=1,
            payload={"occurred_on": "2026-08-15"},  # 故意不给 organization_id
            before=None,
        )
        print(f"{FAIL}: 缺租户键时不该照常查期间（那是跨企业串号的入口）")
        failures += 1
    except DomainError as error:
        print(f"{PASS}: 缺租户键时显式报错 → {error.message}")

    # StateTransition 形态二：迁移两端由服务经 `_invariant_context` 提供。
    # 为什么值得单独一条：这个形态的缺口形态是"声明了却永不触发" ——
    # 只测"合法迁移通过"**测不出来**（一个永不触发的声明同样会让合法迁移通过）。
    # 所以这里必须有**反例**：非法迁移要真的被拒。
    #
    # 状态机在这里**就地建一个独立实例**（不导入 master_data 的 POND_STATUS_WORKFLOW：
    # 那会连它的 `resource()` 一起执行，与本脚本第 12 段对 `pond` 的注册冲突，实测
    # `资源 pond 重复注册`；也不能用第 12 段那个 `pond_flow` —— 它在本行之后才定义，
    # 会得到 UnboundLocalError。就地建一个既无耦合、又只依赖状态机的构造契约）。
    from fpa.kernel.workflow import (
        State,
        Tone,
        Transition,
        Workflow,
    )

    selftest_flow = Workflow(
        resource="ut_status_change",
        initial="build",
        states=(
            State("build", "待建设", Tone.NEUTRAL),
            State("stocked", "已放苗", Tone.INFO),
            State("farming", "养殖中", Tone.SUCCESS),
        ),
        transitions=(Transition("build", "stocked", "ut_status_change.verify"),),
    )
    from_context = StateTransition(
        machine=selftest_flow,
        field_name="pond_status",
        from_context_fields={"from_field": "from_status", "to_field": "to_status"},
    )
    try:
        from_context.check(
            tx=FakeUnitOfWork(),
            scope=scope_for_check,
            actor_id=1,
            payload={"_invariant_context": {"from_status": "build", "to_status": "stocked"}},
            before=None,
        )
        print(f"{PASS}: 形态二 合法迁移（build→stocked）通过")
    except DomainError as error:
        print(f"{FAIL}: 形态二 合法迁移被误拒：{error.message}")
        failures += 1
    try:
        from_context.check(
            tx=FakeUnitOfWork(),
            scope=scope_for_check,
            actor_id=1,
            payload={"_invariant_context": {"from_status": "build", "to_status": "farming"}},
            before=None,
        )
        print(f"{FAIL}: 形态二 非法迁移（build→farming，跳过 stocked）未被拒绝")
        failures += 1
    except DomainError as error:
        print(f"{PASS}: 形态二 非法迁移被拒 → {error.message}")
    try:
        from_context.check(
            tx=FakeUnitOfWork(),
            scope=scope_for_check,
            actor_id=1,
            payload={"_invariant_context": {"from_status": "build"}},  # 缺 to_status
            before=None,
        )
        print(f"{FAIL}: 形态二 缺一端未报错（半个迁移判定比不判更危险）")
        failures += 1
    except DomainError as error:
        print(f"{PASS}: 形态二 缺一端显式报错 → {error.message}")

    when = RequiredWhen(when_field="status", when_values=("cancelled",), required_fields=("reason",), labels=("取消原因",))
    try:
        when.check(
            tx=FakeUnitOfWork(),
            scope=scope_for_check,
            actor_id=1,
            payload={"status": "cancelled"},
            before=None,
        )
        print(f"{FAIL}: 取消未填原因未被拒绝")
        failures += 1
    except DomainError as error:
        print(f"{PASS}: 取消订单必须填原因 → {error.message}")
    try:
        when.check(tx=FakeUnitOfWork(), scope=scope_for_check, actor_id=1, payload={"status": "draft"}, before=None)
        print(f"{PASS}: 非取消状态不受该规则约束")
    except DomainError as error:
        print(f"{FAIL}: 误触发：{error.message}")
        failures += 1

    section(12, "状态机与资源声明：前端 status_dict 与 row_actions 由此派生")
    print("  对应 frontend-recon 提的契约缺口：to_meta 不输出 resource/row_actions，且 ref 缺 list_path")
    from fpa.kernel.fields import RefTarget as RefTargetSpec
    from fpa.kernel.workflow import (
        RowAction,
        State,
        Tone,
        Transition,
        Workflow,
    )
    from fpa.kernel.workflow import resource as declare_resource

    pond_flow = Workflow(
        resource="pond",
        initial="build",
        states=(
            State("build", "待建设", Tone.NEUTRAL, (RowAction.VIEW, RowAction.EDIT, RowAction.SUBMIT)),
            State("stocked", "已放苗", Tone.INFO, (RowAction.VIEW, RowAction.VERIFY)),
            State("scrapped", "已废弃", Tone.DANGER, (RowAction.VIEW,), terminal=True),
        ),
        transitions=(
            Transition("build", "stocked", "pond.verify"),
            Transition("stocked", "scrapped", "pond.archive"),
        ),
    )
    declare_resource(
        name="pond",
        title="塘口",
        module="master_data",
        list_path="/api/v1/ponds",
        detail_path="/api/v1/ponds/{pond_id}",
        workflow=pond_flow,
        # 表名必须显式声明：内核已移除 `<module>_<name>s` 兜底公式（它会猜错且不报错），
        # 所以本自检的资源声明也要跟上 —— 否则 kernel_smoke 会在构造期直接 500。
        table="selftest_ponds",
        columns=(("code", "塘口编号"),),
    )

    codes = [s.code for s in pond_flow.states]
    if codes == ["build", "stocked", "scrapped"] and all(s.label for s in pond_flow.states):
        print(f"{PASS}: 状态字典 {len(codes)} 态且中文标签齐备")
    else:
        print(f"{FAIL}: {codes}")
        failures += 1

    tones = {str(s.tone) for s in pond_flow.states}
    if tones <= {"neutral", "info", "success", "warning", "danger"}:
        print(f"{PASS}: tone 只用了允许的 5 个值 -> {sorted(tones)}")
    else:
        print(f"{FAIL}: 越界 tone {tones}")
        failures += 1

    try:
        pond_flow.require_transition("build", "scrapped", action="pond.archive")
        print(f"{FAIL}: 非法转移未被拒绝")
        failures += 1
    except DomainError as error:
        if "已放苗" in error.message:
            print(f"{PASS}: 非法转移给出可操作提示 -> {error.message}")
        else:
            print(f"{FAIL}: 提示不具体：{error.message}")
            failures += 1

    try:
        pond_flow.require_transition("scrapped", "build", action="pond.verify")
        print(f"{FAIL}: 终态未拦住转移")
        failures += 1
    except DomainError as error:
        print(f"{PASS}: 终态不可转移 -> {error.message}")

    late_spec = cap.Capability(
        name="pond.create",
        title="新建塘口",
        domain="master_data",
        resource="pond",
        handler=DemoService.create,
        method=cap.HttpMethod.POST,
        path="/api/v1/ponds",
        kind="create",
        risk=cap.Risk.NORMAL,
    )
    meta = late_spec.to_meta()
    if meta["resource"] == "pond":
        print(f"{PASS}: to_meta 输出 resource={meta['resource']}")
    else:
        print(f"{FAIL}: resource 缺失")
        failures += 1
    expected_actions = ["view", "edit", "submit", "verify"]
    if meta["row_actions"] == expected_actions:
        print(f"{PASS}: row_actions 由状态机派生 -> {meta['row_actions']}")
    else:
        print(f"{FAIL}: row_actions = {meta['row_actions']}")
        failures += 1

    if RefTargetSpec(resource="pond").resolved_list_path() == "/api/v1/ponds":
        print(f"{PASS}: ref 的 list_path 从资源注册表解析（不再猜复数）")
    else:
        print(f"{FAIL}: list_path 解析失败")
        failures += 1
    try:
        RefTargetSpec(resource="no_such_resource").resolved_list_path()
        print(f"{FAIL}: 未注册资源的 ref 没有报错")
        failures += 1
    except ValueError:
        print(f"{PASS}: 未注册资源显式报错而非猜地址")

    print()
    if failures:
        print(f"结果：{failures} 项失败")
        return 1
    print("结果：全部通过")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
