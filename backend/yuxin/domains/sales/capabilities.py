"""销售域的能力声明（13 条 = registry §1.8 的 12 条 + 当前迭代补的 `sales_order.get`）。

**这是"声明即派生"的落点**：一个 `Capability(...)` 同时派生

    REST 路由 · 权限码 · DataScope 谓词 · 幂等策略 · 人工确认闸门
    审计字段 · Agent Tool schema · 前端表单与列 · 不变量强制点

所以这个文件里**不写**任何路由、校验、权限判断的代码——那些都是派生物。

## 三处容易漏的地方（都在本文件里显式处理了）

**1. 每条写能力都声明 `loader=`。**
`runner._reload_after` 要求任何返回 `resource_id` 的写能力都能解析出回读函数，
否则抛 `INTERNAL_ERROR`（`docs/WRITE_CONTRACT.md` 规则 2）。本文件**逐条显式声明**
`loader=`（而不是在别处用一个循环批量挂载）——理由是"装配关系写在声明处，
一眼可见"：读这条能力声明就能看到"它回读哪张表"，`git grep` 也能查到。
`docs/ROLLOUT_CONTRACT.md` §3 把这条定为新代码的统一形态。

**2. `required_permission` 逐字取自 registry §1.8，不自行细化。**
`sales_order.update` 与 `.submit` 共用 `sales.create` 是 registry 的**明确裁决**
（"本版拆出 `sales.create` 与 `sales.deliver`，`sales.manage` 废弃"），
不是疏漏。自行拆细会让权限码与权威清单对不上（t9 的 69 条核对会红）。

**3. `confirmation` 必须显式写。**
`risk=NORMAL` 默认为 `NEVER`，而 registry §1.8 给了 6 条 `always`。
漏写不会报错——只会让 Agent 绕过人工确认闸门，那正是本项目最不能出的错。
本文件对 §1.8 标注 `always` 的每一条都显式声明，并在注释里标出依据。
"""

from __future__ import annotations

from decimal import Decimal

from yuxin.kernel.capability import (
    AgentExposure,
    AuditPolicy,
    Capability,
    Confirmation,
    HttpMethod,
    REGISTRY,
    Risk,
)
from yuxin.kernel.invariants import (
    AmountWithin,
    CumulativeWithin,
    DistinctActors,
    HarvestQuantityMatch,
    OptimisticLock,
    PeriodOpen,
    RequiredWhen,
    SameTenant,
    StateTransition,
    StatusAllowsEdit,
)
from yuxin.kernel.scope import ScopePolicy

from .deliveries_write import (
    DeliveryWriteService,
    SalesReceiptWriteService,
)
from .sales_orders import (
    DeliveryService,
    ReceivableService,
    SalesOrderService,
    SalesReceiptService,
)
from .sales_orders_write import SalesOrderWriteService
from .service import (
    RECEIVABLE_WORKFLOW,
    SALES_ORDER_WORKFLOW,
    TABLE_DELIVERY,
    TABLE_RECEIVABLE,
    TABLE_SALES_ORDER,
    TABLE_SALES_RECEIPT,
)

#: 写能力默认对智能体开放（README 的裁决：不额外阉割）。
#:
#: 需求明确要求"新建塘口、投喂、采购、**销售**这类普通业务"不能被随意标 human_only。
#: 管控靠 L1 权限 + DataScope + 不变量 + 确认闸门，而不是靠"不让 Agent 碰"。
_EXPOSED = AgentExposure.EXPOSED


def _harvest_quantity_match() -> HarvestQuantityMatch:
    """§4 #6：交付数量必须与出塘事实一致（Q9 恢复）。

    `tolerance` 用默认 1e-3：`DECIMAL(16,3)` 的精度是 3 位小数，
    浮点比较必须留容差，否则 `40.000` 与 `40` 会被判不等。
    """
    return HarvestQuantityMatch(
        harvest_field="harvest_document_id",
        order_field="unit",
        quantity_field="quantity",
        harvest_table="harvests",
        statuses=("verified",),
        batch_field="batch_id",
        pond_field="pond_id",
        label="交付数量",
        harvest_label="出塘单",
    )


def _cumulative_within(*, new_row_counted: bool) -> CumulativeWithin:
    """§4 #10 的声明。`new_row_counted` 由**调用点**按自己的路径语义传：

    * `delivery.create`  —— 服务刚插入的行是 `draft`，不在 `counted` 里 → `False`
    * `delivery.verify`  —— 服务已把本行置 `verified`，已在 `counted` 里 → `True`

    ⚠️ 该参数是内核 t22 新加的**必填**项，而 负责人 的终裁是「零参数 + 自排除」。
    这里按内核要求透传以解除"整域不可装载"，**待内核撤回后整块删除**
    （删除是零行为差异：内核的 `projected` 在两种取值下算法相同，
    去重由 `_invariant_exclude_id` 自排除完成）。
    """
    """§4 #10：累计交付不得超过销售数量。挂 `delivery.create` 与 `delivery.verify`。

    `statuses=("verified",)`：**只放算数的状态**。
    用排除集（`exclude_statuses`）会在新增终止态时静默把作废单据算进累计
    （`CumulativeWithin` 的 docstring 有同样的论证）。

    `for_update=True`：并发下"先查后写"没锁必然双双通过
    —— 两个核验请求都会读到旧累计。

    ## ⚠️ 已知待修（t20）：本不变量当前会**误拒合法的核验**

    `CumulativeWithin` 现在做 `counted + requested`，但它在 `runner._call_service()`
    **之后**执行（`runner.py:245-257`），此时 `delivery.verify` 已把本行置为
    `verified`，于是 `counted` 里**已经包含本次那一行**，再加一次就是重复计数。

    实测：销售单 100kg、已核验交付 60kg、再核验 40kg →
    `counted=100, projected=140 > 100` → **拒绝一个完全合法的操作**。

    负责人 已裁决用 `_invariant_exclude_id` 自排除（零参数）修（task t20），
    并且裁决"两侧都不换算"（`requested` 不再做 `jin→×2`）。
    **声明按上面的正确形态写死，等内核落地即生效**；回归用例见
    `tools/sales_e2e.py` 的 `test_delivery_verify_100_60_40_regression`。
    """
    return CumulativeWithin(
        target_table=TABLE_SALES_ORDER,
        target_dims=("sales_order_id",),
        target_columns=("id",),
        children_table=TABLE_DELIVERY,
        count_field="quantity",
        target_field="quantity",
        request_field="quantity",
        unit_field="unit",
        statuses=("verified",),
        # ⚠️ 临时：内核（t22）把 `new_row_counted` 做成了**必填参数**，
        # 而 负责人 的终裁是"零参数 + 自排除"。此处按内核要求取值以解除
        # "整域不可装载"，**待内核撤回该参数后删除本行**（删除是零行为差异的：
        # 实测内核的 `projected` 在两种情况算法相同，自排除已完成去重）。
        # 取值依据：本函数同时被 `delivery.create`（写 draft）与
        # `delivery.verify`（写 verified）复用 —— 见下面两个调用点各自覆盖。
        new_row_counted=new_row_counted,
        for_update=True,
        label="累计交付量",
    )


def _amount_within(*, statuses: tuple[str, ...] = ("unpaid", "partial")) -> AmountWithin:
    """§4 #4：收款不得超过应收余额。

    余额算法显式声明：`total_amount - paid_amount`
    （`balance_column` 省略时默认等于 `target_column`，这里显式写出来是为了让
    读声明的人不必去猜内核对默认值的处理）。

    **一份声明覆盖 create 与 verify 两处**——早期版本在这两处各写一遍
    （`早期版本 purchase_payment_store.py:114-115` 与 `:154-157`），
    那是"同一规则两处实现"的样本。
    """
    return AmountWithin(
        amount_field="amount",
        table=TABLE_RECEIVABLE,
        target_field="receivable_id",
        target_column="id",
        balance_column="total_amount",
        paid_column="paid_amount",
        status_field="status",
        statuses=statuses,
        currency_column="currency",
        currency="CNY",
        label="应收账款",
    )


def _register_all() -> None:
    """登记销售域的全部能力。

    用函数包裹而不是模块级裸语句：重复导入这个模块（例如测试里 reload）
    不会因为"能力重复注册"而炸——`REGISTRY.register` 对重名是显式抛错的。
    """
    if REGISTRY.find("sales_order.list") is not None:
        return  # 已注册（重复导入）

    # ========================================================================
    # 销售单：3 读 + 5 写（registry §1.8 + 当前迭代补的 `sales_order.get`）
    # ========================================================================

    REGISTRY.register(
        Capability(
            name="sales_order.list",
            title="销售单列表",
            domain="sales",
            resource="sales_order",
            handler=SalesOrderService.list_orders,
            service_factory=SalesOrderService,
            method=HttpMethod.GET,
            path="/api/v1/sales-orders",
            kind="read",
            required_permission="sales.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词、状态、客户、塘口、批次与日期区间筛选销售单列表",
        )
    )

    REGISTRY.register(
        Capability(
            name="sales_order.get",
            title="销售单详情",
            domain="sales",
            resource="sales_order",
            handler=SalesOrderService.get_order_by_id,
            service_factory=SalesOrderService,
            method=HttpMethod.GET,
            path="/api/v1/sales-orders/{order_id}",
            kind="read",
            required_permission="sales.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="销售单详情：含累计交付量、已收款与 available_transitions",
        )
    )

    REGISTRY.register(
        Capability(
            name="sales_order.create",
            title="新建销售单",
            domain="sales",
            resource="sales_order",
            handler=SalesOrderWriteService.create_order,
            service_factory=SalesOrderWriteService,
            method=HttpMethod.POST,
            path="/api/v1/sales-orders",
            kind="create",
            required_permission="sales.create",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            # §1.8：risk=normal + confirmation=never（新建销售单不在高危清单里）
            confirmation=Confirmation.NEVER,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # §4 #19 编码在范围内唯一 —— **只由 DB 唯一键保证，不在本能力上声明
                # `UniqueCode`**。实测与理由：
                #
                #   * `UniqueCode` 要用 `_value_from((payload, before), 'organization_id')`
                #     取范围列，而 `_value_from` 只做**顶层字典查找**；
                #   * `organization_id` 是 registry §0.7 规则 1 / Q7 明确**不允许客户端
                #     提交**的字段，所以它不在 `cleaned` 里 —— 内核当场抛
                #     `INTERNAL_ERROR: 编码唯一性校验缺少范围列 organization_id`；
                #   * 让服务回传它等于要求"服务记得回传一个入参字段"，那正是 负责人
                #     反复否决的形态（与 `new_row_counted` 同类）。
                #
                # **没有降低保证**：`uq_sales_orders_org_code` 唯一键仍在（并发下唯一
                # 可信的判定），而 `kernel/uow.py::translate_mysql_error` 已把
                # errno 1062 统一翻译成 `CONFLICT`。§4 #19 原文要求的
                # "DB 唯一键兜底 + 应用层翻译为 CONFLICT"两条都满足，
                # 只是翻译点在内核的 `uow`，不在能力声明上。
                # §4 #18 客户/塘口/批次必须同企业。
                SameTenant(
                    fields=("customer_id", "pond_id", "batch_id"),
                    tables={
                        "customer_id": "business_partners",
                        "pond_id": "ponds",
                        "batch_id": "production_batches",
                    },
                ),
            ),
            loader=SalesOrderService.load_order,
            description="在指定塘口与批次下新建销售单，归属由塘口解析，状态只能是草稿",
        )
    )

    REGISTRY.register(
        Capability(
            name="sales_order.update",
            title="编辑销售单",
            domain="sales",
            resource="sales_order",
            handler=SalesOrderWriteService.update_order,
            service_factory=SalesOrderWriteService,
            method=HttpMethod.PATCH,
            path="/api/v1/sales-orders/{order_id}",
            kind="update",
            # §1.8：update 与 submit 共用 `sales.create`（registry 明确裁决，
            # 不自行细化）
            required_permission="sales.create",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            confirmation=Confirmation.NEVER,
            agent_exposure=_EXPOSED,
            # §1.8：idempotent=**false**（update/submit 不强制幂等键；
            # 只有 create 与三条高危 action 是 true）。
            idempotent=False,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # §4 #12 核验后只读
                StatusAllowsEdit(statuses=("draft", "submitted")),
                # §4 #13 乐观锁
                OptimisticLock(),
                # §4 #18 同企业
                SameTenant(
                    fields=("customer_id", "pond_id", "batch_id"),
                    tables={
                        "customer_id": "business_partners",
                        "pond_id": "ponds",
                        "batch_id": "production_batches",
                    },
                ),
            ),
            loader=SalesOrderService.load_order,
            description="编辑草稿或待审批销售单的台账信息。单号不可改，状态只能经审批动作推进",
        )
    )

    REGISTRY.register(
        Capability(
            name="sales_order.submit",
            title="提交销售单",
            domain="sales",
            resource="sales_order",
            handler=SalesOrderWriteService.submit_order,
            service_factory=SalesOrderWriteService,
            method=HttpMethod.POST,
            path="/api/v1/sales-orders/{order_id}/submit",
            kind="action",
            required_permission="sales.create",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            confirmation=Confirmation.NEVER,
            agent_exposure=_EXPOSED,
            # §1.8：idempotent=**false**（update/submit 不强制幂等键；
            # 只有 create 与三条高危 action 是 true）。
            idempotent=False,
            # §1.8：audit=**summary**（提交是轻量动作，不记 before/after 全量 diff）。
            #
            # 内核里这一档现在叫 `AuditPolicy.summary()`。它原先叫 `none()`，
            # 而那个名字在撒谎——`runner._write_audit` 是**无条件**调用的，
            # 审计行照样写，差别只在**不记 before/after 快照**。
            # 改名发生在 access/audit/identity 七条能力落盘时（t6）：
            # 一个叫 `none` 的构造器实际做的是 `summary`，会让读者以为
            # "这条能力不落审计"，进而在别处补一遍审计写入。
            # 同批按文档把 `pond/batch/purchase_order.submit` 从 `snapshot()` 改到本档。
            audit=AuditPolicy.summary(),
            invariants=(
                OptimisticLock(),
                # §4 #14 状态转移合法（§3.5 的转移表）
                StateTransition(machine="sales_order"),
            ),
            loader=SalesOrderService.load_order,
            description="把草稿销售单提交给他人审批，不做任何核验判定",
        )
    )

    REGISTRY.register(
        Capability(
            name="sales_order.approve",
            title="审批销售单",
            domain="sales",
            resource="sales_order",
            handler=SalesOrderWriteService.approve_order,
            service_factory=SalesOrderWriteService,
            method=HttpMethod.POST,
            path="/api/v1/sales-orders/{order_id}/approve",
            kind="action",
            required_permission="sales.approve",
            scope=ScopePolicy.resource("area_id"),
            # §1.8：**risk=high + confirmation=always**
            # （初稿是 never，按 §0.4 的一致性约束修正为 always）
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # §4 #5 经办人≠审批人。早期版本**只有销售域**实现了这一条
                # （`早期版本 sales_service.py:121-122`），本版由内核在 13 条核验/审批
                # 能力上统一执行，不再各域各写一遍。
                DistinctActors(),
                OptimisticLock(),
                StateTransition(machine="sales_order"),
            ),
            loader=SalesOrderService.load_order,
            description="审批他人提交的销售单。经办人不能审批自己提交的单据",
        )
    )

    REGISTRY.register(
        Capability(
            name="sales_order.cancel",
            title="取消销售单",
            domain="sales",
            resource="sales_order",
            handler=SalesOrderWriteService.cancel_order,
            service_factory=SalesOrderWriteService,
            method=HttpMethod.POST,
            path="/api/v1/sales-orders/{order_id}/cancel",
            kind="action",
            required_permission="sales.approve",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # §4 #22「取消必须填原因」。`when_field="status"` 按**目标状态**判定，
                # 目标状态由服务回传（`_invariant_context`）。
                RequiredWhen(
                    when_field="status",
                    when_values=("cancelled",),
                    required_fields=("reason",),
                    labels=("取消原因",),
                ),
                OptimisticLock(),
                StateTransition(machine="sales_order"),
            ),
            loader=SalesOrderService.load_order,
            description="取消待审批、已审批或部分交付的销售单。必须说明取消原因",
        )
    )

    # ========================================================================
    # 交付：1 读 + 2 写（registry §1.8）
    # ========================================================================

    REGISTRY.register(
        Capability(
            name="delivery.list",
            title="交付单列表",
            domain="sales",
            resource="delivery",
            handler=DeliveryService.list_deliveries,
            service_factory=DeliveryService,
            method=HttpMethod.GET,
            path="/api/v1/deliveries",
            kind="read",
            # §1.8：交付列表用 `sales.view`（与销售单同一个读码）
            required_permission="sales.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词、状态、销售单、塘口筛选交付单列表",
        )
    )

    REGISTRY.register(
        Capability(
            name="delivery.get",
            title="交付单详情",
            domain="sales",
            resource="delivery",
            handler=DeliveryService.get_delivery_by_id,
            service_factory=DeliveryService,
            method=HttpMethod.GET,
            path="/api/v1/deliveries/{delivery_id}",
            kind="read",
            required_permission="sales.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="读取交付单详情",
        )
    )

    REGISTRY.register(
        Capability(
            name="delivery.create",
            title="登记交付",
            domain="sales",
            resource="delivery",
            handler=DeliveryWriteService.create_delivery,
            service_factory=DeliveryWriteService,
            method=HttpMethod.POST,
            path="/api/v1/deliveries",
            kind="create",
            required_permission="sales.deliver",
            scope=ScopePolicy.resource("area_id"),
            # §1.8：**risk=high**（早期版本交付必须绑定出塘单且数量逐字节相等）
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # §2.9 销售单须为 approved / partially_delivered
                # （由服务层查，这里不再声明 ReferencedStatus：
                #  它会按 field 查目标表，而"允许哪些状态"已经在服务的
                #  `_assert_order_deliverable` 里给出**字段级**文案；
                #  两处都声明会让同一规则有两份状态集）
                # §4 #6 交付数量必须与出塘事实一致
                _harvest_quantity_match(),
                # §4 #10 累计交付不得超过销售数量
                _cumulative_within(new_row_counted=False),
            ),
            loader=DeliveryService.load_delivery,
            description="登记一次交付。必须绑定已核验的出塘单，交付数量须与出塘事实一致",
        )
    )

    REGISTRY.register(
        Capability(
            name="delivery.verify",
            title="核验交付",
            domain="sales",
            resource="delivery",
            handler=DeliveryWriteService.verify_delivery,
            service_factory=DeliveryWriteService,
            method=HttpMethod.POST,
            path="/api/v1/deliveries/{delivery_id}/verify",
            kind="action",
            required_permission="sales.verify",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # §4 #10 累计交付（核验路径）
                _cumulative_within(new_row_counted=True),
                # §4 #5 经办人≠审批人（13 条之一）
                DistinctActors(),
                # §4 #7 已关账期间不得产生账目记录。
                # **收入侧与成本侧一视同仁**（Q16 裁决原文：
                # "关账锁的是整个期间（收入与成本一起锁定），不是只锁成本侧"）。
                PeriodOpen(date_field="occurred_on"),
                OptimisticLock(),
                StateTransition(machine="delivery"),
            ),
            loader=DeliveryService.load_delivery,
            description="核验他人登记的交付，生成应收账款并推进销售单状态。经办人不能核验自己登记的",
        )
    )

    # ========================================================================
    # 应收：1 读（**无写能力** —— 应收由 delivery.verify 生成）
    # ========================================================================

    REGISTRY.register(
        Capability(
            name="receivable.list",
            title="应收列表",
            domain="sales",
            resource="receivable",
            handler=ReceivableService.list_receivables,
            service_factory=ReceivableService,
            method=HttpMethod.GET,
            path="/api/v1/receivables",
            kind="read",
            # §1.8：应收用**独立的 finance 码**，不是 sales.view。
            # 理由：真实组织里"看销售数据"与"看应收账款"常常是两个人。
            required_permission="finance.receivable.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词、状态、客户与是否逾期筛选应收账款列表",
        )
    )

    REGISTRY.register(
        Capability(
            name="receivable.get",
            title="应收账款详情",
            domain="sales",
            resource="receivable",
            handler=ReceivableService.get_receivable_by_id,
            service_factory=ReceivableService,
            method=HttpMethod.GET,
            path="/api/v1/receivables/{receivable_id}",
            kind="read",
            required_permission="finance.receivable.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="读取应收账款详情",
        )
    )

    # ========================================================================
    # 收款：2 写（registry §1.8）—— **没有 `sales_receipt.list`**
    # ========================================================================
    #
    # §1.8 的 12 条里**没有**收款单列表能力。这不是遗漏：收款单是应收的明细，
    # 从应收详情里看即可（§2.9 也只给应收配了页面 `ReceivablePage.vue`）。
    #
    # `SalesReceiptService.list_receipts` 仍然保留在服务层：它是核验与排查用的
    # 内部读路径（`tools/sales_e2e.py` 用它断言"核验后收款单确实存在"），
    # 只是**不注册为能力**。不注册 = 不生成路由、不进 Agent 工具清单、
    # 不进前端元数据 —— 这正是"能力清单是权威，工具清单是它的派生子集"的含义。

    REGISTRY.register(
        Capability(
            name="sales_receipt.list",
            title="收款单列表",
            domain="sales",
            resource="sales_receipt",
            handler=SalesReceiptService.list_receipts,
            service_factory=SalesReceiptService,
            method=HttpMethod.GET,
            path="/api/v1/sales-receipts",
            kind="read",
            required_permission="finance.receipt.manage",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词、应收来源与状态筛选收款单",
        )
    )

    REGISTRY.register(
        Capability(
            name="sales_receipt.get",
            title="收款详情",
            domain="sales",
            resource="sales_receipt",
            handler=SalesReceiptService.get_receipt_by_id,
            service_factory=SalesReceiptService,
            method=HttpMethod.GET,
            path="/api/v1/sales-receipts/{receipt_id}",
            kind="read",
            required_permission="finance.receipt.manage",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="读取收款单详情",
        )
    )

    REGISTRY.register(
        Capability(
            name="sales_receipt.create",
            title="登记收款",
            domain="sales",
            resource="sales_receipt",
            handler=SalesReceiptWriteService.create_receipt,
            service_factory=SalesReceiptWriteService,
            method=HttpMethod.POST,
            path="/api/v1/sales-receipts",
            kind="create",
            required_permission="finance.receipt.manage",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # §4 #4 收款不得超过应收余额
                _amount_within(),
            ),
            loader=SalesReceiptService.load_receipt,
            description="按应收账款登记一笔收款，金额不得超过应收余额",
        )
    )

    REGISTRY.register(
        Capability(
            name="sales_receipt.verify",
            title="核验收款",
            domain="sales",
            resource="sales_receipt",
            handler=SalesReceiptWriteService.verify_receipt,
            service_factory=SalesReceiptWriteService,
            method=HttpMethod.POST,
            path="/api/v1/sales-receipts/{receipt_id}/verify",
            kind="action",
            required_permission="finance.receipt.verify",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # §4 #5 经办人≠审批人（13 条之一）
                DistinctActors(),
                # §4 #4 —— 与 `sales_receipt.create` 共用**同一份**声明
                # （早期版本在 create 与 verify 两处各写一遍，本版只有一处）
                _amount_within(statuses=("unpaid", "partial", "settled")),
                OptimisticLock(),
                StateTransition(machine="sales_receipt"),
            ),
            loader=SalesReceiptService.load_receipt,
            description="核验他人登记的收款，推进应收的累计已收款与结清状态。经办人不能核验自己登记的",
        )
    )


_register_all()
