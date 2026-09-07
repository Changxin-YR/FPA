"""采购域的能力声明（registry §1.7 的 10 条 + 当前迭代补的 `purchase_order.get`，逐条照抄）。

**这是"声明即派生"的落点**：一个 `Capability(...)` 同时派生

    REST 路由（`web/app.py` 按 `method` + `path` 自动注册）
    权限码（`required_permission`）
    DataScope 谓词（`scope`）
    幂等策略（`idempotent`）
    人工确认闸门（`risk` + `confirmation`）
    审计字段（`audit`）
    Agent Tool schema（`agent_exposure` + 从处理器标注收集的字段）
    前端表单与列（`fields` + 资源声明的 `columns`）
    业务不变量（`invariants`，由执行器在同一事务内执行）

所以这个文件里**不写**任何路由、校验、权限判断的代码 —— 那些都是派生物。

## 本文件承载的"不变量强制点"总账（registry §4）

| 能力 | 挂载的不变量 | registry 条目 |
|---|---|---|
| `purchase_order.create` | `ReferencedStatus`(物料须 verified) / `SameTenant` / `UniqueCode` | #18 / #19 |
| `purchase_order.update` | `StatusAllowsEdit` / `OptimisticLock` | #12 / #13 |
| `purchase_order.submit` | `OptimisticLock` / `StateTransition` | #13 / #14 |
| `purchase_order.approve` | `DistinctActors` / `OptimisticLock` / `StateTransition` | **#5** / #13 / #14 |
| `purchase_order.cancel` | `RequiredWhen`(取消必填原因) / `OptimisticLock` / `StateTransition` | 取消必填 / #13 / #14 |
| `payment.create` | `SameTenant` / `UniqueCode` | #18 / #19 |
| `payment.verify` | **`AmountWithin`** / **`DistinctActors`** / `OptimisticLock` / `StateTransition` | **#4** / **#5** / #13 / #14 |

**#3（未审批不得收货）与 #9（累计到货不得超过采购数量）的强制点在 warehouse 域的
`receipt.create` / `receipt.verify` 上** —— 本域只提供被引用的表与状态字面值
（`PURCHASE_ORDER_RECEIVABLE_STATUSES`、`purchase_orders.quantity`）。
不在这里声明它们，是因为"未审批不得收货"这个动作在本域根本没有入口：
采购单侧没有 `receive` 能力（registry §3.4 明确删除）。

**#7（已关账期间不得再归集）** 在本域的强制点是 `payment.verify`
（它写付款账目，而付款是收入/支出的账目事实）。`PeriodOpen` 的
`date_field="occurred_on"` 由服务通过 `_invariant_context` 回传
（表里只有 `paid_at`，期间取它的日期部分）。
"""

from __future__ import annotations

from fpa.kernel.capability import (
    AgentExposure,
    AuditPolicy,
    Capability,
    Confirmation,
    HttpMethod,
    NO_LOADER,
    REGISTRY,
    Risk,
)
from fpa.kernel.fields import f_int
from fpa.kernel.scope import ScopePolicy
from fpa.kernel.workflow import RowAction

from .orders import PurchaseOrderService
from .orders_write import PurchaseOrderWriteService
from .payments import PurchasePaymentService
from .payments_write import PurchasePaymentWriteService
from .service import (
    PURCHASE_ORDER_RECEIVABLE_STATUSES,
    PURCHASE_ORDER_WORKFLOW,
    TABLE_PURCHASE_ORDER,
    TABLE_PURCHASE_PAYABLE,
    TABLE_PURCHASE_PAYMENT,
)

#: 写能力默认对智能体开放（与 master_data / cost 同一裁决：不额外阉割）。
#:
#: 需求明确要求"新建塘口、投喂、采购、销售这类普通业务"不能被随意标 human_only。
#: 管控靠 L1 权限 + DataScope + 不变量 + 确认闸门。
#:
#: registry §5.3 记录了一处**未裁决**的倾向：`purchase_order.cancel` 我（文档编制者）
#: 倾向标 `human_only`，但"取消"不属于 §5.1 允许的六个方向，正确手段是
#: `confirmation=always` —— 本条按 registry 的现状实现（`exposed` + `always`），
#: 不自行扩大 human_only。
_EXPOSED = AgentExposure.EXPOSED


# ---------------------------------------------------------------------------
# 不变量构造点
#
# 每个构造点单独一个函数，理由与 cost 域相同：让"这条能力挂了哪条合规规则"在
# `_register_all()` 里**一眼可见**，而不是藏在一行长表达式里。
#
# 全部在**调用时**构造（而不是模块级常量）：`ReferencedStatus` 等需要表名常量，
# 而表名常量的唯一声明处在 `service.py` —— 引用它而不是抄一遍字面值。
# ---------------------------------------------------------------------------


def _material_verified():
    """§4 #18：采购物料必须是 `verified` 状态的物料。

    "物料须 verified"逐字来自 registry §2.8 的字段约束列。
    与 `SameTenant` 的分工：这条管**状态**，那条管**归属一致性**。
    """
    from fpa.kernel import invariants as inv

    return inv.ReferencedStatus(
        field="material_id",
        table="materials",
        statuses=("verified",),
        label="采购物料",
    )


def _same_tenant():
    """§4 #18：物料 / 收货仓 / 供应商必须同企业。

    `scope_fields` 只取前两列（`organization_id` / `farm_id`），
    **刻意不含 `area_id`**：仓库与物料各自挂在不同区域是正常业务
    （一个区域有饲料仓、另一个区域有药品仓），把 area 也纳入比较会把合法配置判成冲突。
    企业边界与基地边界是真正不能跨的那两条。
    """
    from fpa.kernel import invariants as inv

    return inv.SameTenant(
        fields=("supplier_id", "material_id", "warehouse_id"),
        tables={
            "supplier_id": "business_partners",
            "material_id": "materials",
            # `warehouses` 的表名来自 `database/migrations/006_warehouse.sql`
            #（已落盘并应用到真实库）。**不是猜的**：`SameTenant` 会对这个字段
            # 执行 `SELECT * FROM warehouses WHERE id=%s`，表名错了会抛
            # "table doesn't exist" —— 而这种错误的位置离声明处很远。
            "warehouse_id": "warehouses",
        },
        scope_fields=("organization_id", "farm_id"),
        labels=("供应商", "采购物料", "收货仓"),
        required=True,
    )


def _unique_code():
    """§4 #19：编码在组织范围内唯一。

    DB 唯一键 `uq_purchase_orders_org_code` / `uq_purchase_payments_org_code`
    是**并发下唯一可信**的判定；本条负责在写库前给出可读的 409。
    两者都要有（与 §4 #21 的口径一致：唯一键是正确性底线，预检负责文案）。

    `scope=("organization_id",)`：取不到范围列时内核**显式报错**而不是退化成
    全表查（"静默放宽唯一性比报错危险得多"），所以服务必须通过
    `_invariant_context` 回传 `organization_id`。
    """
    from fpa.kernel import invariants as inv

    return inv.UniqueCode(
        fields=("code",),
        scope=("organization_id",),
        table=TABLE_PURCHASE_ORDER,
        labels=("采购单号",),
    )


def _unique_payment_code():
    from fpa.kernel import invariants as inv

    return inv.UniqueCode(
        fields=("code",),
        scope=("organization_id",),
        table=TABLE_PURCHASE_PAYMENT,
        labels=("付款单号",),
    )


def _status_allows_edit():
    """§4 #12：核验后只读。

    `statuses` 逐字取自 registry §4 #12 的 `statuses=["draft","submitted"]`。
    注意它与状态机动作集合是**两个独立的失效模式**：这一条看状态码集合，
    `RowAction.EDIT` 看"该状态允许哪些动作"。本域的实际可编辑集合更窄（只有
    `draft`，因为 `PURCHASE_ORDER_WORKFLOW` 只给 `draft` 配了 EDIT）——
    声明保持与 registry 一致，收紧的那层由状态机负责。
    """
    from fpa.kernel import invariants as inv

    return inv.StatusAllowsEdit(statuses=("draft", "submitted"), label="采购单")


def _optimistic_lock():
    """§4 #13：行版本必须与请求携带的期望版本一致。

    表名由执行器注入（`_resource_table`，取自 `Resource(table=...)`），
    所以这里**不用**再抄一遍 `TABLE_PURCHASE_ORDER` —— 抄一遍就是两处描述同一件事。
    """
    from fpa.kernel import invariants as inv

    return inv.OptimisticLock(label="该采购单")


def _optimistic_lock_payment():
    from fpa.kernel import invariants as inv

    return inv.OptimisticLock(label="该付款单")


def _state_transition():
    """§4 #14：状态必须沿声明的转移表变化。

    `machine` 取**资源名**（内核在 `RESOURCES` 里解析状态机）。
    目标状态由服务通过 `_invariant_context["status"]` 回传 —— 因为 `status`
    不是 action 能力的请求字段（它由动作决定），不回传就会"声明了却永不触发"。
    """
    from fpa.kernel import invariants as inv

    return inv.StateTransition(machine=PURCHASE_ORDER_WORKFLOW.resource, label="采购单")


def _distinct_actors_order():
    """§4 #5：经办人 ≠ 审批人（`purchase_order.approve`）。

    列名 `approved_by` 而不是 `verified_by` —— 采购单是**三段式**（提交/审批/执行），
    它的审批列叫 `approved_by`。这不是"改了个名字"，而是本表的实际列：
    不变量按列名读 `before` 快照。
    """
    from fpa.kernel import invariants as inv

    return inv.DistinctActors(creator_field="created_by", verifier_field="approved_by")


def _distinct_actors_payment():
    """§4 #5：经办人 ≠ 核验人（`payment.verify`）。列名是 `verified_by`。"""
    from fpa.kernel import invariants as inv

    return inv.DistinctActors(creator_field="created_by", verifier_field="verified_by")


def _reason_required():
    """取消必须填原因（旧 `purchase_service.py:151-153`）。

    `when_field="status"` + `when_values=("cancelled",)`：不变量按**目标状态**判，
    而目标状态由服务回传（同 `_state_transition` 的理由）。
    服务同时无条件要求 `reason`（本能力的语义就是"取消"），
    两者不是重复的判定：这里的声明让规则在**声明处**可见，服务那层负责给出
    字段级文案，DB 的 `chk_purchase_orders_reason` 保证脏数据写不进去。
    """
    from fpa.kernel import invariants as inv

    return inv.RequiredWhen(
        when_field="status",
        when_values=("cancelled",),
        required_fields=("reason",),
        labels=("取消原因",),
    )


def _amount_within_payable():
    """§4 #4：付款不得超过应付余额（**唯一强制点**）。

    参数与表结构一一对应：

        amount_field   = "amount"         请求体里的付款金额
        table          = purchase_payables 被引用的表（**带域前缀**，见迁移注释）
        target_field   = "payable_id"     请求体里指向应付的字段
        target_column  = "id"             应付表的主键列
        balance_column = "total_amount"   应付总额（余额 = 它 − paid_column）
        paid_column    = "paid_amount"    累计已付（只由 payment.verify 推进）
        statuses       = ("unpaid","partial")  允许付款的状态集（写"什么可以"而非
                                               "什么不可以"：后者在枚举新增状态时静默放行）
        currency_column= "currency"       币种不一致必须被拒，而不是按汇率静默换算

    早期版本在 create 与 verify **两处**实现了这条规则
    （`早期版本 purchase_payment_store.py:114-115` 与 `:154-157`）。
    registry §1.7 裁决：只在校验点 `payment.verify` 强制。
    所以本函数**只被 `payment.verify` 使用** —— `payment.create` 里那句余额判断
    是描述性的（返回 warning，不拦截），不是第二个检查点。
    """
    from fpa.kernel import invariants as inv

    return inv.AmountWithin(
        amount_field="amount",
        table=TABLE_PURCHASE_PAYABLE,
        target_field="payable_id",
        target_column="id",
        balance_column="total_amount",
        paid_column="paid_amount",
        # 服务在不变量运行前已把最后一笔付款推进为 settled，允许这次合法的终态写入；
        # 已结清的付款单在登记阶段仍由服务层拒绝。
        statuses=("unpaid", "partial", "settled"),
        currency_column="currency",
        label="应付账款",
    )


def _same_tenant_payment():
    """付款与其应付、供应商必须同企业。

    `payables` 不存 `payable_id` 之外的外域引用 —— 这里只需保证"这张应付真的
    属于本企业"，而应付的分租键由服务从范围解析写入（`tenant_from_scope`？不 ——
    它继承应付行的分租键，见 `create_payment`）。供应商的一致性用
    `purchase_order_id` 反查：
    """
    from fpa.kernel import invariants as inv

    return inv.SameTenant(
        fields=("payable_id",),
        tables={"payable_id": TABLE_PURCHASE_PAYABLE},
        scope_fields=("organization_id", "farm_id"),
        labels=("应付账款",),
        required=True,
    )


def _period_open():
    """§4 #7：已关账期间不得再写入账目记录。

    `date_field="occurred_on"` —— 付款表里没有这一列（只有 `paid_at`），
    服务通过 `_invariant_context` 回传 `paid_at` 的日期部分。
    不回传的话内核**不会静默跳过**，而是报"不变量需要这个字段" ——
    这是内核刻意的设计（"缺上下文不得静默跳过"）。
    """
    from fpa.kernel import invariants as inv

    return inv.PeriodOpen(
        date_field="occurred_on", label="该付款日期所在的会计期间"
    )


def _register_all() -> None:
    """登记采购域的全部能力。

    用函数包裹而不是模块级裸语句：重复导入这个模块（例如测试里 reload）
    不会因为"能力重复注册"而炸 —— `REGISTRY.register` 对重名是显式抛错的。
    """
    if REGISTRY.find("purchase_order.list") is not None:
        return  # 已注册（重复导入）

    # ---------------------------------------------------------------- 读能力

    REGISTRY.register(
        Capability(
            name="purchase_order.list",
            title="采购单列表",
            domain="purchase",
            resource="purchase_order",
            handler=PurchaseOrderService.list_orders,
            service_factory=PurchaseOrderService,
            method=HttpMethod.GET,
            path="/api/v1/purchase-orders",
            kind="read",
            required_permission="purchase.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词、状态、供应商、物料、收货仓、预计到货日期筛选采购单列表",
        )
    )

    REGISTRY.register(
        Capability(
            name="purchase_order.get",
            title="采购单详情",
            domain="purchase",
            resource="purchase_order",
            handler=PurchaseOrderService.get_order_by_id,
            service_factory=PurchaseOrderService,
            method=HttpMethod.GET,
            path="/api/v1/purchase-orders/{order_id}",
            kind="read",
            required_permission="purchase.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="采购单详情：含 available_transitions（从当前状态出发的合法目标）",
        )
    )

    REGISTRY.register(
        Capability(
            name="payable.list",
            title="应付列表",
            domain="purchase",
            resource="purchase_payable",
            handler=PurchasePaymentService.list_payables,
            service_factory=PurchasePaymentService,
            method=HttpMethod.GET,
            path="/api/v1/payables",
            kind="read",
            required_permission="finance.payable.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词、状态、供应商、采购单筛选应付账款列表（余额由总额与已付算出）",
        )
    )

    REGISTRY.register(
        Capability(
            name="purchase_payable.get",
            title="应付详情",
            domain="purchase",
            resource="purchase_payable",
            handler=PurchasePaymentService.get_payable_by_id,
            service_factory=PurchasePaymentService,
            method=HttpMethod.GET,
            path="/api/v1/payables/{payable_id}",
            kind="read",
            required_permission="finance.payable.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="读取应付账款详情",
        )
    )

    REGISTRY.register(
        Capability(
            name="payment.list",
            title="付款列表",
            domain="purchase",
            resource="purchase_payment",
            handler=PurchasePaymentService.list_payments,
            service_factory=PurchasePaymentService,
            method=HttpMethod.GET,
            path="/api/v1/payments",
            kind="read",
            required_permission="finance.payment.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词、状态、付款方式、应付来源筛选付款记录列表",
        )
    )

    REGISTRY.register(
        Capability(
            name="payment.get",
            title="付款详情",
            domain="purchase",
            resource="purchase_payment",
            handler=PurchasePaymentService.get_payment_by_id,
            service_factory=PurchasePaymentService,
            method=HttpMethod.GET,
            path="/api/v1/payments/{payment_id}",
            kind="read",
            required_permission="finance.payment.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="读取付款详情",
        )
    )

    # ---------------------------------------------------------------- 采购单写

    REGISTRY.register(
        Capability(
            name="purchase_order.create",
            title="新建采购单",
            domain="purchase",
            resource="purchase_order",
            handler=PurchaseOrderWriteService.create_order,
            loader=PurchaseOrderService.load_order,
            service_factory=PurchaseOrderWriteService,
            method=HttpMethod.POST,
            path="/api/v1/purchase-orders",
            kind="create",
            required_permission="purchase.create",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            confirmation=Confirmation.NEVER,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                _material_verified(),
                _same_tenant(),
                _unique_code(),
            ),
            description="新建一张采购单（初始状态为草稿）。物料须已核验，供应商必须是供应商类型",
        )
    )

    REGISTRY.register(
        Capability(
            name="purchase_order.update",
            title="编辑采购单",
            domain="purchase",
            resource="purchase_order",
            handler=PurchaseOrderWriteService.update_order,
            loader=PurchaseOrderService.load_order,
            service_factory=PurchaseOrderWriteService,
            method=HttpMethod.PATCH,
            path="/api/v1/purchase-orders/{order_id}",
            kind="update",
            required_permission="purchase.create",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                _status_allows_edit(),
                _optimistic_lock(),
            ),
            description="编辑草稿状态的采购单（单号与状态不可改）。已提交或已审批的采购单不能编辑",
        )
    )

    REGISTRY.register(
        Capability(
            name="purchase_order.submit",
            title="提交采购单",
            domain="purchase",
            resource="purchase_order",
            handler=PurchaseOrderWriteService.submit_order,
            loader=PurchaseOrderService.load_order,
            service_factory=PurchaseOrderWriteService,
            method=HttpMethod.POST,
            path="/api/v1/purchase-orders/{order_id}/submit",
            kind="action",
            required_permission="purchase.create",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            confirmation=Confirmation.NEVER,
            agent_exposure=_EXPOSED,
            idempotent=True,
            # registry 给本行的 `audit` 列是 **summary**（提交是轻量动作，不记 before/after 全量 diff）。
            # 原写 `snapshot()`，与文档不一致：`submit` 只把 status 从 draft 推到 submitted，
            # 那次状态转移本身就写在审计的 `reason` / `object_ref` 里，再记一份整行 diff
            # 只会让审计表膨胀。四领域的同族动作（含 `sales_order.submit`，registry §1.8）统一为 summary——
            # 让三个 `*.submit` 记全量 diff 而第四个不记，是「同一件事两处不一样」的典型。
            audit=AuditPolicy.summary(),
            invariants=(
                _optimistic_lock(),
                _state_transition(),
            ),
            description="把草稿状态的采购单提交给他人审批",
        )
    )

    REGISTRY.register(
        Capability(
            name="purchase_order.approve",
            title="审批采购单",
            domain="purchase",
            resource="purchase_order",
            handler=PurchaseOrderWriteService.approve_order,
            loader=PurchaseOrderService.load_order,
            service_factory=PurchaseOrderWriteService,
            method=HttpMethod.POST,
            path="/api/v1/purchase-orders/{order_id}/approve",
            kind="action",
            required_permission="purchase.approve",
            scope=ScopePolicy.resource("area_id"),
            # 审批是高风险的（涉及金额承诺）：HIGH -> `confirmation` 默认 ALWAYS。
            # 声明里仍显式写出 `confirmation`，让"这一条必须人工确认"在声明处可见 ——
            # registry §0.4 的一致性约束要求 risk=high 的动作能力为 always。
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                _distinct_actors_order(),
                _optimistic_lock(),
                _state_transition(),
            ),
            description="审批他人提交的采购单。经办人不能审批自己提交的采购单",
        )
    )

    REGISTRY.register(
        Capability(
            name="purchase_order.cancel",
            title="取消采购单",
            domain="purchase",
            resource="purchase_order",
            handler=PurchaseOrderWriteService.cancel_order,
            loader=PurchaseOrderService.load_order,
            service_factory=PurchaseOrderWriteService,
            method=HttpMethod.POST,
            path="/api/v1/purchase-orders/{order_id}/cancel",
            kind="action",
            required_permission="purchase.approve",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                _reason_required(),
                _optimistic_lock(),
                _state_transition(),
            ),
            description="取消已提交或已审批的采购单（必须填写取消原因）。取消是不可逆的对外承诺撤销",
        )
    )

    # ---------------------------------------------------------------- 付款写

    REGISTRY.register(
        Capability(
            name="payment.create",
            title="登记付款",
            domain="purchase",
            resource="purchase_payment",
            handler=PurchasePaymentWriteService.create_payment,
            loader=PurchasePaymentService.load_payment,
            service_factory=PurchasePaymentWriteService,
            method=HttpMethod.POST,
            path="/api/v1/payments",
            kind="create",
            required_permission="finance.payment.manage",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                _same_tenant_payment(),
                _unique_payment_code(),
            ),
            # ★ 这条 description 是刻意的：**`payment.create` 不拦"超过余额"**。
            # registry §1.7 明确它"只做提示性校验"，强制点在 `payment.verify`。
            # 不写清楚的话，下一个人会"顺手"把 `AmountWithin` 也挂上来 ——
            # 那就把早期版本"同一规则两处实现"的缺陷抄回来了。
            description=(
                "登记一笔付款（初始状态为草稿）。金额超过应付余额时只给出提示，"
                "核验时才会被拒绝"
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="payment.verify",
            title="核验付款",
            domain="purchase",
            resource="purchase_payment",
            handler=PurchasePaymentWriteService.verify_payment,
            loader=PurchasePaymentService.load_payment,
            service_factory=PurchasePaymentWriteService,
            method=HttpMethod.POST,
            path="/api/v1/payments/{payment_id}/verify",
            kind="action",
            required_permission="finance.payment.verify",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # ★ 本域最重要的一行：`AmountWithin` 是"付款不得超过余额"的
                #   **唯一**强制点；`DistinctActors` 是"经办人≠核验人"在本域的载体。
                _amount_within_payable(),
                _distinct_actors_payment(),
                _period_open(),
                _optimistic_lock_payment(),
                _state_transition(),
            ),
            description=(
                "核验付款并累计到应付账款上。付款金额不得超过应付余额，"
                "且经办人不能核验自己登记的付款。已关账的会计期间不能核验"
            ),
        )
    )


_register_all()

# ---------------------------------------------------------------------------
# 详情读能力：当前迭代补了 `purchase_order.get`（原「已知缺口」一节的处置）
#
# 这一节原本记着一条缺口：`purchase_orders` 是列表型资源，而 registry §1.7 的 10 条
# 里没有「采购单详情」，于是前端只能复用列表响应 —— 代价是详情页拿不到
# `available_transitions`（从当前状态出发的合法目标），而那正是「服务端按转移表
# 过滤候选值」的运行时形态。
#
# **当前迭代的处置（评审结论，对应 ROADMAP §4.2 的 P1「详情页」）**：
# 登记 `purchase_order.get`。原先不补的理由是「69 条是 t9 的验收基线，不该由某个域
# 单独改数」；当前迭代的任务本身就是补缺口，因此那条基线被**显式解除** —— 不是把纪律
# 放宽，是纪律换了目标。
#
# 其余列表型资源（batch / feeding / harvest / cost_entry / delivery / receivable /
# sales_receipt / purchase_payment / warehouse_document / inventory_lot）的详情读
# **当前迭代不补**，按业务重要性分批推进，并已在 `docs/CAPABILITY_REGISTRY.md` 登记为
# 下一轮候选。它们的 `get_*_by_id` 处理器大多已经实现，所以剩下的是「要不要暴露」
# 的取舍，不是工作量问题。
# ---------------------------------------------------------------------------


__all__ = ["_register_all"]
