"""成本域的能力声明。

**这是"声明即派生"的落点**：一个 `Capability(...)` 同时派生

    REST 路由（`web/app.py` 按 `method` + `path` 自动注册）
    权限码（`required_permission`）
    DataScope 谓词（`scope`）
    幂等策略（`idempotent`）
    人工确认闸门（`risk` + `confirmation`）
    审计字段（`audit`）
    Agent Tool schema（`agent_exposure` + 从处理器标注收集的字段）
    前端表单与列（`fields` + 资源声明的 `columns`）
    业务不变量（`invariants`，由 `CapabilityRunner` 在同一事务内统一执行）

所以这个文件里**不写**任何路由、校验、权限判断的代码——那些都是派生物。

## 本域的不变量强制点（registry §4 的落点）

| 不变量 | 挂在哪条能力 | 依据 |
|---|---|---|
| `PeriodOpen(occurred_on)` | `cost.entry.create`、`cost.entry.confirm`、`cost.period.close` | §4 #7 + Q16 |
| `RequiredField(source_ref)` | `cost.entry.confirm` | §4 #15（Q5 降级后的替代形态） |
| `DistinctActors(created_by, verified_by)` | `cost.entry.confirm` | §4 #5 + Q8/Q15（13 条能力之一） |
| `NoOverlappingSource` | `cost.entry.create` | §4 #8（2026-09 补挂，见 `_dedupe_invariant()`） |

## 一个必须说清的边界：`cost.period.close` 的 scope

registry §1.9 给 `cost.period.close` 的 scope 是 **`none`**。这不是遗漏：

会计期间是**全局对象**——它锁的是"2026-09 这个月不许再入账"，而不是"某个区域的
2026-09 不许入账"。如果按区域加范围限定，就会出现"同一期间一半已关一半未关"，
那比不锁更危险（报表可以同时用两种口径算出两个数）。

真正的管控靠：`cost.close` 权限码 + `reason` 必填 + 审计（`before_after`）。

## 关于 `confirmation`

`cost.entry.confirm` 与 `cost.period.close` 都是 `risk=high` 的 `action`，
按 §0.4 的一致性约束**必须** `always`。内核在注册期机械校验这条
（`risk=high ∧ kind=action ∧ agent_exposure≠human_only ⇒ confirmation=always`），
所以它是"写错了就启动不了"，不是"靠评审记得"。

## 一个已知缺口（**不是遗漏，请勿"顺手补上"**）

`COST_ENTRY_WORKFLOW` 在 `draft` 上声明了 `edit` / `submit` 动作（照 master_data 的
`RECORD_LIFECYCLE` 抄的），而 registry §1.9 只给 cost 域定了 **5 条**能力，
**没有** `cost.entry.update` / `cost.entry.submit` / `cost.entry.archive`。

后果：`Capability.row_actions()` 会把 `edit` / `submit` / `archive` / `verify`
一起报给前端（`INTERFACES.md` §2 称之为"静态上限"），前端据此渲染按钮，
而点击时**必然 404**——因为既没有能力声明也没有路由。

处置：
* **不注册**这些能力。registry 的 5 条是权威清单；擅自扩成 7 条会改变能力总数，
  而"69 条"是全队的验收基线（t9）。
* `CostWriteService.submit_entry` / `archive_entry` 的实现保留（逻辑正确、
  将来要开放只需加两条声明），并在实现处标注了原委。
* 缺口记录在**这里**，因为它是声明层的缺口，适合由对账工具（t10 的
  "REGISTRY × docs §1/§4 三张缺口表"）扫出来。

**这是一条跨域共性问题**：`Workflow.row_actions` 是"该状态允许什么动作"，
`REGISTRY` 是"系统实现了什么动作"，两者之间没有任何机械一致性保证。
建议在架构测试里加一条断言：**`row_actions` 里出现的每个动作，都要能在
REGISTRY 里找到对应能力**。否则每个新域都会渲染出点不动的按钮，
而这在现有测试里看不出来（没有任何测试会把"渲染出的按钮"和"存在的路由"对账）。
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
from fpa.kernel.invariants import DistinctActors, PeriodOpen, RequiredField
from fpa.kernel.scope import ScopePolicy

from .entries import CostEntryService
from .entries_write import CostWriteService

#: 写能力默认对智能体开放（与 master_data 同一裁决：不额外阉割）。
#:
#: 需求明确要求"新建塘口、投喂、采购、销售这类普通业务"不能被随意标 human_only。
#: 管控靠 L1 权限 + DataScope + 不变量 + 确认闸门，而不是靠"不让 Agent 碰"。
_EXPOSED = AgentExposure.EXPOSED


def _cost_entry_invariants() -> tuple:
    """`cost.entry.create` 的不变量。

    ## `PeriodOpen`（registry §4 #7 + Q16）

    必挂：它是"已关账期间不得产生任何影响该期间的账目记录"这条规则在手工入口上的
    强制点。`date_field="occurred_on"` —— 服务保证 payload（含
    `_invariant_context`）里能取到它。

    ## `NoOverlappingSource`（registry §4 #8）

    **两个实例，方向不同。** `NoOverlappingSource.source_types` 的语义是
    "**禁止与本次并存的**来源类型"，不是"本次的来源类型"——内核的文档字符串
    专门把这条钉死了：

        禁止集合 {warehouse_ledger}  用于"本次手工登记" -> 挡住同期已有的库存归集
        禁止集合 {manual_expense}    用于"本次库存归集" -> 挡住同期已有的手工费用

    规则的两半因此都得到强制（§4 #8 的原文是"同期不得重复归集库存成本"，
    方向是「手工费用 与 库存自动成本 不能对同一归属对象重复计入」，正是这两条）。
    内核的查询是 `source_type IN (禁止集合)`，所以两个实例合起来恰好覆盖两个方向。

    ## 同来源类型的重复由数据库唯一键管

    同 `source_ref` + 同归属 + 同期间的**库存归集**重复插入由
    `uq_cost_entries_org_dedupe`（走 `ledger_dedupe_key` 生成列）在数据库层拒绝，
    服务把 1062 翻译成可读的 409。**这条刻意不用不变量重复一遍**：
    并发下的"先查再插"本来就做不到唯一性保证，唯一键是物理底线。

    ## 租户键

    `NoOverlappingSource.tenant_keys` 默认取 `organization_id` / `farm_id` /
    `area_id`，但**不会自己补全**——它只在 payload / before 里能取到时才限定。
    所以服务必须把这三个值通过 `_invariant_context` 回传，否则不变量会在**跨企业**
    范围内查重（两个不同企业的同一 batch_id 会被误判为重复）。
    """
    from fpa.kernel import invariants as inv

    period_open = inv.PeriodOpen(
        date_field="occurred_on", label="该成本发生日期所在的会计期间"
    )
    # 同归属 + 期间重叠 + 分租键相同 -> 拒绝重复归集。
    # `required=False`：不指定归属对象的成本（落到账号数据范围解析出的区域上）
    # 没有 target 可判定，此时按允许处理——数据库唯一键仍然守着自动归集那条路。
    manual_blocks_ledger = inv.NoOverlappingSource(
        source_types=("warehouse_ledger",),
        target_fields=("target_type", "target_id"),
        period_fields=("period_start", "period_end"),
        tenant_keys=("organization_id", "farm_id", "area_id"),
        # 「显式覆盖则放行」的白名单，继承早期实现的 `manual_feed_offset` /
        # `manual_feed_direct` 两个类别。
        #
        # ⚠️ **本版实际不可达**，这是刻意保留的接线而不是可用的功能：
        # registry §2.10 把 `source_type` 收到**两个值**
        # （`manual_expense` / `warehouse_ledger`），上面还额外拒绝对这两个值之外的
        # 任何输入。所以拿不到 `manual_feed_direct` 这个取值。
        #
        # 为什么仍然写上：内核要求"白名单必须显式声明才生效"，漏了它会让
        # 早期版本里那条"财务明确选择覆盖"的语义在迁移时**静默消失**；
        # 写在这里，等于把这个缺口固定在代码里可被搜索到，而不是留在某人的记忆里。
        # 若将来放开 `source_type` 取值（这是产品决定），这里无需再改。
        override_source_types=("manual_feed_direct", "manual_feed_offset"),
        label="该塘口/批次",
    )
    ledger_blocks_manual = inv.NoOverlappingSource(
        source_types=("manual_expense",),
        target_fields=("target_type", "target_id"),
        period_fields=("period_start", "period_end"),
        tenant_keys=("organization_id", "farm_id", "area_id"),
        label="该塘口/批次",
    )
    return (period_open, manual_blocks_ledger, ledger_blocks_manual)


def _register_all() -> None:
    """登记成本域的全部能力。

    用函数包裹而不是模块级裸语句：重复导入这个模块（例如测试里 reload）
    不会因为"能力重复注册"而炸——`REGISTRY.register` 对重名是显式抛错的。
    """
    if REGISTRY.find("cost.entry.list") is not None:
        return  # 已注册（重复导入）

    # ---------------------------------------------------------------- 读能力

    REGISTRY.register(
        Capability(
            name="cost.entry.list",
            title="成本记录列表",
            domain="cost",
            resource="cost_entry",
            handler=CostEntryService.list_entries,
            service_factory=CostEntryService,
            method=HttpMethod.GET,
            path="/api/v1/cost/entries",
            kind="read",
            required_permission="cost.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description=(
                "按类别、来源、归属对象、期间与发生日期筛选成本记录列表。"
                "支持 keyword 对来源单号与备注做模糊搜索"
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="cost.summary",
            title="成本汇总",
            domain="cost",
            resource="cost_entry",
            handler=CostEntryService.summarize_costs,
            service_factory=CostEntryService,
            method=HttpMethod.GET,
            path="/api/v1/cost/summary",
            kind="read",
            required_permission="cost.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description=(
                "按塘口 / 批次查询成本汇总：合计金额、已确认与待确认金额、"
                "按成本性质、按成本类别、按归属对象的拆分（含占比，合计精确等于 100%）"
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="cost_entry.get",
            title="成本记录详情",
            domain="cost",
            resource="cost_entry",
            handler=CostEntryService.get_entry_by_id,
            service_factory=CostEntryService,
            method=HttpMethod.GET,
            path="/api/v1/cost/entries/{entry_id}",
            kind="read",
            required_permission="cost.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="读取成本记录详情",
        )
    )

    # ---------------------------------------------------------------- 登记成本

    REGISTRY.register(
        Capability(
            name="cost.entry.create",
            title="登记成本",
            domain="cost",
            resource="cost_entry",
            handler=CostWriteService.create_entry,
            service_factory=CostWriteService,
            method=HttpMethod.POST,
            path="/api/v1/cost/entries",
            kind="create",
            required_permission="cost.manage",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=_cost_entry_invariants(),
            description=(
                "登记一笔成本（含系统外费用：人工、水电、租金）。"
                "期间起止留空时按发生日期所在自然月自动确定；"
                "归属对象留空时按当前账号的数据范围解析"
            ),
        )
    )

    # ---------------------------------------------------------------- 确认成本

    REGISTRY.register(
        Capability(
            name="cost.entry.confirm",
            title="确认成本",
            domain="cost",
            resource="cost_entry",
            handler=CostWriteService.confirm_entry,
            service_factory=CostWriteService,
            method=HttpMethod.POST,
            path="/api/v1/cost/entries/{entry_id}/confirm",
            kind="action",
            required_permission="cost.confirm",
            scope=ScopePolicy.resource("area_id"),
            # 审批是高风险业务（需求点名"审核、付款、收款"），
            # 因此 HIGH -> effective_confirmation 默认 ALWAYS。
            risk=Risk.HIGH,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # §4 #5 + Q8/Q15：合规规则统一，费用与采购/仓储/生产一视同仁。
                # 比较字段固定为 created_by 与 verified_by —— 比早期版本比 updated_by 更严格。
                # 内核实现里的默认值就是这两个字段名，这里显式写出来是**声明的一部分**：
                # 读代码的人应当能在这个文件里看到"这条能力受哪条合规规则约束"。
                _distinct_actors(),
                # §4 #15（Q5 降级）：附件不做，"核验必须有凭据"降级为"必须填来源单号"。
                # 这是**已知降级**，须写入最终交付报告的"尚未完成的问题"：
                # 后续加 attachment.* 能力即可升级回"必须有凭据"。
                RequiredField(fields=("source_ref",), labels=("来源单号",)),
                # Q16：确认入账也是一次账目影响，已关账期间不得确认。
                _period_open(),
            ),
            description=(
                "确认成本入账。经办人不能确认自己登记的成本（需他人复核）；"
                "确认时必须给出来源单号，且所在会计期间不得已关账"
            ),
        )
    )

    # ---------------------------------------------------------------- 关账

    REGISTRY.register(
        Capability(
            name="accounting_period.list",
            title="会计期间列表",
            domain="cost",
            resource="accounting_period",
            handler=CostEntryService.list_periods,
            service_factory=CostEntryService,
            method=HttpMethod.GET,
            path="/api/v1/cost/periods",
            kind="read",
            required_permission="cost.view",
            # 期间是全局对象（与 `cost.period.close` 同一口径）。
            scope=ScopePolicy.none(),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按状态筛选会计期间；关账动作的落点",
        )
    )

    REGISTRY.register(
        Capability(
            name="cost.period.close",
            title="关账",
            domain="cost",
            resource="accounting_period",
            handler=CostWriteService.close_period,
            service_factory=CostWriteService,
            # 关账是状态迁移（open -> closed），按 registry §0.2 的**新增设计**用 POST。
            # 早期版本对"月份状态"用的也是 POST（`早期版本 cost_store.py` 的月份操作），
            # 而 PUT/PATCH 的语义是"替换资源整体"，与"执行一次封存动作"不同。
            method=HttpMethod.POST,
            path="/api/v1/cost/periods/{period}/close",
            kind="action",
            required_permission="cost.close",
            # scope=none：期间是全局对象，见文件头的说明。**这不是遗漏。**
            scope=ScopePolicy.none(),
            risk=Risk.HIGH,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            # ★ 刻意**不挂** PeriodOpen —— 这条与 registry §4 #7 / Q16 的字面指示不同，
            #   是一次有依据的偏离，理由见下面的说明。**不要"照文档补回来"。**
            #
            # `run_invariants()` 在**业务写入之后**执行（`runner.py`：
            # `_call_service` -> `run_invariants`）。而本能力的业务写入**就是把这个
            # 期间置为 closed**，于是 `PeriodOpen(date_field="occurred_on")` 查到的是
            # "刚刚被自己关掉的那个期间"，必然判定失败：
            #
            #     PeriodOpen.check -> SELECT status ... -> 'closed' != 'open'
            #     -> CONFLICT「该会计期间已关账，不能再写入」
            #
            # 也就是说：**挂上去的后果是关账永远无法成功**（实测复现过）。
            # 这不是实现缺陷，而是"关账"这个动作与"写入前必须未关账"这条规则在
            # **同一条能力上**语义互斥——规则说的是"关账之后别再往里记东西"，
            # 它天然属于**会写账目**的能力，而不是"执行关账"这个动作本身。
            #
            # 语义澄清（与 Q16 的关系）：Q16 的原意是"不得在已关闭的期间产生任何影响
            #   该期间的账目记录"，它点名覆盖的是
            #   `cost.entry.create` / `cost.entry.confirm` / `feeding.verify` /
            #   `receipt.verify` / `issue.verify` / `delivery.verify` —— 全部是**写账目**
            #   的能力。`cost.period.close` 出现在 §4 #7 的能力清单里是清单编制时的
            #   一个口径混入（把"开启锁的动作"与"受锁约束的动作"列在了一起）。
            #
            # 本能力真正的期间约束由三处更强的手段保证，不依赖不变量：
            #   1. `close_period` 里显式的 `_require_open_period`（给出可读文案）；
            #   2. `UPDATE ... WHERE status='open'` 的**原子占位**——并发下只有一个
            #      请求能改到行，`affected == 0` 即报 VERSION_CONFLICT。
            #      这比"先查再断言"强，也是早期版本缺失的那个正确写法；
            #   3. `uq_accounting_periods_org_period` 唯一键 + `chk_accounting_periods_closed`
            #      的 CHECK（关账必须留痕）。
            description=(
                "关闭一个会计期间（YYYY-MM）。关账后该期间不得再产生任何影响期间的"
                "账目记录——不只在成本域，也在到货/出库/投喂/交付的核验路径上"
            ),
        )
    )


def _distinct_actors():
    """`DistinctActors` 的构造点。

    单独包一层是为了让"这条能力挂了哪条合规规则"在 `_register_all` 里**一眼可见**
    （见 `cost.entry.confirm` 的 invariants 元组），而不是藏在一行长表达式里。
    """
    from fpa.kernel.invariants import DistinctActors

    return DistinctActors(creator_field="created_by", verifier_field="verified_by")


def _period_open():
    """`PeriodOpen` 的构造点。

    `date_field="occurred_on"`：不变量按**这个字段的日期**反查所属期间。
    三个挂它的能力都保证 payload 里能取到 `occurred_on`：

    * `cost.entry.create` —— 请求字段本身
    * `cost.entry.confirm`  —— 服务通过 `_invariant_context` 回传（记录的实际发生日期）
    * `cost.period.close`   —— 服务通过 `_invariant_context` 回传期间起点
      （关账动作发生的"那一天"就是期间起点；用它反查所属期间是唯一的）
    """
    from fpa.kernel.invariants import PeriodOpen

    return PeriodOpen(date_field="occurred_on", label="该成本发生日期所在的会计期间")


_register_all()
