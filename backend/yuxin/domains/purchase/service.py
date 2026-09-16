"""采购域：采购单、应付账款、付款。

这是照 master_data / cost 抄的第四个域（`DEVELOPMENT.md` §3 的固定七步），
按 registry §1.7 的 10 条能力实现：

    purchase_order.list     采购单列表      read   purchase.view
    purchase_order.create   新建采购单      create purchase.create
    purchase_order.update   编辑采购单      update purchase.create       ← 非同名派生
    purchase_order.submit   提交采购单      action purchase.create       ← 非同名派生
    purchase_order.approve  审批采购单      action purchase.approve   risk=high
    purchase_order.cancel   取消采购单      action purchase.approve   risk=high
    payable.list            应付列表        read   finance.payable.view
    payment.list            付款列表        read   finance.payment.view
    payment.create          登记付款        create finance.payment.manage risk=high
    payment.verify          核验付款        action finance.payment.verify risk=high

## 四处刻意的不对称（都不是笔误）

**1. 权限码不按能力名派生。**
`submit` 用的是 `purchase.create`（不是 `purchase.submit`）、`approve`/`cancel`
共用 `purchase.approve`。依据是 registry §1.7 的权限码沿用早期版本命名
（`早期版本 purchase_service.py:26-30`）。"权限码 = 能力名"这条派生规则在这里
**不成立**，所以声明必须逐个显式写出来。

**2. 采购单有三段流程，付款单只有两段。**
订单类是"经办人提交 → 审批人审批 → 再执行"（涉及金额承诺与外部供应商），
单据类是"经办人录入 → 核验人核验"（registry §3.6 的分类依据）。
早期版本两类都套同一套四态模板，于是单据类多出一个永不被单独使用的 `submitted`。

**3. `payment.verify` 是"付款不得超过余额"的**唯一**强制点。**
早期版本在 `create`（`早期版本 purchase_payment_store.py:114-115`）与 `verify`
（`:154-157`）**两处**重复实现了同一条规则。registry §1.7 明确：
新系统只在校验点 `payment.verify` 强制，`payment.create` 只做**提示性**校验。
把旧的重复抄回来就是"两处描述同一件事"。

**4. 应付/付款表名带域前缀（`purchase_payables` / `purchase_payments`）。**
registry 正文只写资源名 `payables` / `payments`，而本仓表名一律带域前缀。
两处不一致必须单点定死：迁移建同名表 + 本文件的 `Resource(table=...)` 显式声明 +
不变量 `table=` 写同一字面值 —— **任何一处依赖 `Resource` 的
`<module>_<name>s` 兜底，都会在"谁省略一次 `table=`"时得到一个离声明处很远的错误**。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from yuxin.kernel.workflow import allowed_actions_for
from yuxin.kernel.errors import DomainError, ErrorCode
from yuxin.kernel.fields import Choice, RefTarget
from yuxin.kernel.money import currency_label, money
from yuxin.kernel.scope import Scope, ScopeType
from yuxin.kernel.workflow import (
    RESOURCES,
    DateRangeParam,
    FilterKind,
    FilterSpec,
    Resource,
    RowAction,
    State,
    Tone,
    Transition,
    Workflow,
)

# ============================================================================
# 表名常量：**唯一声明处**
#
# 为什么把它们写成常量而不是在每条 SQL 里直接写字面量：这三张表名会被三处引用
# （迁移 DDL / 本文件的能力与不变量声明 / SQL 查询），字面量散落时任何一处改动
# 都会漂移。`Resource(table=...)` 与不变量声明都从这里取，**物理上不可能不一致**。
# ============================================================================

TABLE_PURCHASE_ORDER = "purchase_orders"
TABLE_PURCHASE_PAYABLE = "purchase_payables"
TABLE_PURCHASE_PAYMENT = "purchase_payments"


# ============================================================================
# 状态机
# ============================================================================

#: 采购单状态机（registry §3.4，**逐字照抄**）。
#:
#: 三处刻意：
#:   * `closed` 在枚举里但**没有转移入口** —— registry §3.4 显式标注
#:     "保留在枚举、但无触发能力"（早期版本的 `closed` 同样没有写入点）。
#:     把它标成 `terminal=True`，前端就不会渲染任何指向它的动作。
#:   * `disputed` **不存在** —— 早期版本库枚举有它，但全仓无任何代码写入该状态
#:     （`早期版本 purchase_store.py` 的 `set_order_status` 只处理 approve/cancel/receive）。
#:     枚举里放一个永远达不到的值，会让"状态是否可达"这条检查失去意义。
#:   * `partially_received` / `fully_received` 的**触发能力是 `receipt.verify`**
#:     （warehouse 域），不是采购域的任何能力。它们仍必须写在这里 ——
#:     状态机是这条转移的唯一声明处，`apply_receipt` 依赖它。
PURCHASE_ORDER_WORKFLOW = Workflow(
    resource="purchase_order",
    initial="draft",
    states=(
        State("draft", "草稿", Tone.NEUTRAL, (RowAction.VIEW, RowAction.EDIT, RowAction.SUBMIT, RowAction.CANCEL)),
        # `approve` 而不是 `verify`：本域的审批能力名是 `purchase_order.approve`，
        # 前端按 `${resource}.${action}` 拼能力名 —— 写成 `verify` 会找不到端点，
        # 然后回退到该资源第一条 action 能力（那是 `/cancel`，高危）。
        State("submitted", "待审批", Tone.WARNING, (RowAction.VIEW, RowAction.APPROVE, RowAction.CANCEL)),
        State("approved", "已审批", Tone.SUCCESS, (RowAction.VIEW, RowAction.CANCEL)),
        State("partially_received", "部分到货", Tone.INFO, (RowAction.VIEW,)),
        State("fully_received", "全部到货", Tone.SUCCESS, (RowAction.VIEW,)),
        State("cancelled", "已取消", Tone.DANGER, (RowAction.VIEW,), terminal=True),
        # `reserved=True`：registry §3.4 原文——`closed` 保留在枚举里但**无触发能力**
        # （早期版本同样没有写入点）。内核要求这个区别写成**字段**而不是注释：
        # 断言必须能区分「忘了接转移」（缺陷）与「刻意预留」（合法），
        # 而"名字叫 closed 就放行"这种推断会在改名时静默失效。
        State("closed", "已关闭", Tone.NEUTRAL, (), terminal=True, reserved=True),
    ),
    transitions=(
        Transition("draft", "submitted", "purchase_order.submit"),
        Transition("submitted", "approved", "purchase_order.approve"),
        Transition("submitted", "cancelled", "purchase_order.cancel"),
        Transition("approved", "cancelled", "purchase_order.cancel"),
        Transition("partially_received", "cancelled", "purchase_order.cancel"),
        # 下面三条由 warehouse 的 `receipt.verify` 经 `apply_receipt()` 触发。
        # 写在这里而不是"留给 apply_receipt 自己判"：状态机只有一份，
        # 两处各写一份转移表就会漂移。
        Transition("approved", "partially_received", "receipt.verify"),
        Transition("approved", "fully_received", "receipt.verify"),
        Transition("partially_received", "fully_received", "receipt.verify"),
    ),
)

#: 应付账款状态机。**只有三个可达状态**。
#:
#: 早期版本库枚举还有 `overpaid` / `bad_debt`：前者被 `AmountWithin` 挡在源头
#: （付款金额 ≤ 余额 ⇒ 产生不了超付），后者需要独立的核销能力而本版没有
#: （registry §6 明确不做）。把不可达的值写进状态机，会让"每个状态是否可达"
#: 这条自检失去意义。
PURCHASE_PAYABLE_WORKFLOW = Workflow(
    resource="purchase_payable",
    initial="unpaid",
    states=(
        State("unpaid", "未付款", Tone.WARNING, (RowAction.VIEW,)),
        State("partial", "部分付款", Tone.INFO, (RowAction.VIEW,)),
        State("settled", "已结清", Tone.SUCCESS, (RowAction.VIEW,), terminal=True),
    ),
    transitions=(
        # 触发能力都是 `payment.verify` —— 已付金额只由它推进。
        Transition("unpaid", "partial", "payment.verify"),
        Transition("unpaid", "settled", "payment.verify"),
        Transition("partial", "settled", "payment.verify"),
    ),
)

#: 付款单状态机。单据类统一两态（registry §3.6）：`draft -> verified`。
PURCHASE_PAYMENT_WORKFLOW = Workflow(
    resource="purchase_payment",
    initial="draft",
    states=(
        State("draft", "草稿", Tone.NEUTRAL, (RowAction.VIEW, RowAction.VERIFY)),
        State("verified", "已核验", Tone.SUCCESS, (RowAction.VIEW,)),
    ),
    transitions=(
        Transition("draft", "verified", "payment.verify"),
    ),
)


# ============================================================================
# 业务常量（全部可追溯到 registry）
# ============================================================================

#: 付款方式五种取值（registry §2.8，继承旧 `purchase_service.py:17`）。
PAYMENT_METHODS = ("bank_transfer", "cash", "check", "digital_wallet", "other")

#: 付款方式的中文文案。**只此一处** —— 前端读 `meta` 里的 choices，
#: 不在页面里各译一遍（早期版本同一个枚举在三个页面里三种翻译）。
PAYMENT_METHOD_LABELS: dict[str, str] = {
    "bank_transfer": "银行转账",
    "cash": "现金",
    "check": "支票",
    "digital_wallet": "数字钱包",
    "other": "其他",
}

#: 允许**收货**的采购单状态（registry §4 #3）。
#:
#: 它是 `ReferencedStatus(field="purchase_order_id", table="purchase_orders",
#: statuses=...)` 的参数来源 —— warehouse 域的 `receipt.create` / `receipt.verify`
#: 用同一个元组。写"什么状态可以"而不是"什么状态不可以"：后者在状态枚举新增
#: 一个值时静默放行。
PURCHASE_ORDER_RECEIVABLE_STATUSES = ("approved", "partially_received")

#: 允许**付款**的应付状态（registry §4 #4 的 `AmountWithin(statuses=...)`）。
PAYABLE_PAYABLE_STATUSES = ("unpaid", "partial")

#: 金额上限：DECIMAL(16,2) 的物理上限。声明它让**校验层**先给出可读文案，
#: 而不是让 MySQL 用 1264 报一句"数值超出允许范围"。
MAX_AMOUNT = 99_999_999_999_999.99


# ============================================================================
# 资源声明（前端列表页的列与状态字典都从这里来）
# ============================================================================

RESOURCES.register(
    Resource(
        name="purchase_order",
        title="采购单",
        module="purchase",
        list_path="/api/v1/purchase-orders",
        detail_path="/api/v1/purchase-orders/{order_id}",
        workflow=PURCHASE_ORDER_WORKFLOW,
        # ★ `table=` **必须显式给**（`ROLLOUT_CONTRACT.md` §3 与 评审结论）：
        #   兜底规则是 `<module>_<name>s`，对 `purchase_order` 恰好算出
        #   `purchase_orders` —— 但"恰好对"不是契约。内核文档自己写了
        #   "兜底只是便利、不是契约"，那就更不该让便利值与契约值有可能不同。
        #   执行器把表名注入不变量上下文（`_resource_table`），`OptimisticLock`
        #   拿它去查库；兜底错了不会静默放行，只会报一个离声明处很远的错误。
        table=TABLE_PURCHASE_ORDER,
        columns=(
            ("code", "采购单号"),
            ("name", "采购事项"),
            ("supplier_name", "供应商"),
            ("material_name", "采购物料"),
            ("warehouse_name", "收货仓"),
            ("quantity", "采购数量"),
            ("unit_price", "单价"),
            ("total_amount", "金额"),
            ("received_quantity", "已到货"),
            ("unpaid_amount", "未付金额"),
            ("expected_delivery_date", "预计到货"),
            ("due_date", "付款到期"),
            ("status", "状态码"),
            ("status_label", "状态"),
        ),
        # 逐条对应 `PurchaseOrderService._filters` 的 `params.get(...)`。
        # 关键词列是 `o.code` / `o.name`。
        search=True,
        filters=(
            FilterSpec("status", "状态", FilterKind.STATUS),
            FilterSpec(
                "supplier_id",
                "供应商",
                FilterKind.REF,
                ref=RefTarget("partner", "name"),
            ),
            FilterSpec(
                "material_id",
                "采购物料",
                FilterKind.REF,
                ref=RefTarget("material", "name"),
            ),
            FilterSpec(
                "warehouse_id",
                "收货仓",
                FilterKind.REF,
                ref=RefTarget("warehouse", "name"),
            ),
            # 两端区间做成**一个**控件：参数名由 `params` 给出，
            # 前端不自己拼 `expected_from` / `expected_to`（拼错不报错）。
            FilterSpec(
                "expected_delivery_date",
                "预计到货",
                FilterKind.DATE_RANGE,
                params=DateRangeParam("expected_from", "expected_to"),
            ),
        ),
    )
)

RESOURCES.register(
    Resource(
        name="purchase_payable",
        title="应付账款",
        module="purchase",
        list_path="/api/v1/payables",
        detail_path="/api/v1/payables/{payable_id}",
        workflow=PURCHASE_PAYABLE_WORKFLOW,
        # 表名与兜底（`purchase_purchase_payables`）**不同** —— 兜底会多一层前缀，
        # 这正是"表名与兜底结果不同的域必须显式声明"的实例。
        table=TABLE_PURCHASE_PAYABLE,
        columns=(
            ("name", "应付事项"),
            ("order_code", "采购单号"),
            ("supplier_name", "供应商"),
            ("total_amount", "应付金额"),
            ("paid_amount", "已付金额"),
            ("balance", "未付余额"),
            # 币种同 `unit_label`：`currency` 是 ISO 码（CNY），给人看的是派生标签。
            # 码不能改（`AmountWithin` 按码比较），所以补的是 `currency_label`。
            ("currency_label", "币种"),
            ("due_date", "付款到期"),
            ("occurred_on", "入账日期"),
            ("status", "状态码"),
            ("status_label", "状态"),
        ),
        # 对应 `PurchaseQueryService._payable_filters`。关键词**只匹配 `p.name`**
        # （没有 `code` 列，因此也不声明 `code` 为可筛项）。
        search=True,
        filters=(
            FilterSpec("status", "状态", FilterKind.STATUS),
            FilterSpec(
                "purchase_order_id",
                "采购单",
                FilterKind.REF,
                ref=RefTarget("purchase_order", "code"),
            ),
            FilterSpec(
                "supplier_id",
                "供应商",
                FilterKind.REF,
                ref=RefTarget("partner", "name"),
            ),
            # `only_unpaid` 是**业务口径**（未结清 = unpaid ∪ partial），
            # 服务端一条 WHERE 给出；让前端自己拼 status 会随口径演进漂移。
            FilterSpec("only_unpaid", "只看未结清", FilterKind.BOOLEAN),
        ),
    )
)

RESOURCES.register(
    Resource(
        name="purchase_payment",
        title="付款单",
        module="purchase",
        list_path="/api/v1/payments",
        workflow=PURCHASE_PAYMENT_WORKFLOW,
        table=TABLE_PURCHASE_PAYMENT,
        columns=(
            ("code", "付款单号"),
            ("name", "付款事项"),
            ("payable_name", "应付来源"),
            ("supplier_name", "供应商"),
            ("amount", "付款金额"),
            ("paid_at", "付款日期"),
            ("payment_method", "付款方式码"),
            ("payment_method_label", "付款方式"),
            ("status", "状态码"),
            ("status_label", "状态"),
        ),
        # 对应 `PurchaseQueryService._payment_filters`。关键词列是
        # `pay.code` / `pay.name`。
        search=True,
        filters=(
            FilterSpec("status", "状态", FilterKind.STATUS),
            # 候选值与中文取自 `PAYMENT_METHOD_LABELS`——**不在声明里再写一份**。
            FilterSpec(
                "payment_method",
                "付款方式",
                FilterKind.ENUM,
                choices=tuple(
                    Choice(code, PAYMENT_METHOD_LABELS[code]) for code in PAYMENT_METHODS
                ),
            ),
            FilterSpec(
                "payable_id",
                "应付来源",
                FilterKind.REF,
                ref=RefTarget("purchase_payable", "name"),
            ),
        ),
    )
)


# ============================================================================
# 共享校验与派生
# ============================================================================

def label_of(workflow: Workflow, code: str) -> str:
    """状态码 -> 中文标签。

    **状态的文案只能有一个来源**（`State.label`）。这里刻意不写第二份映射表 ——
    早期版本把同一个状态在 14 处各自翻译，同一个 `verified` 在成本页叫「待确认」、
    在其他页叫「已核验」、在 agent 词典里叫「已提交」。
    """
    for state in workflow.states:
        if state.code == code:
            return state.label
    return code


def _decimal_text(value: Any) -> Any:
    """Decimal -> str。

    Decimal 直接进 JSON 会被序列化成浮点数，而金额在传输层出现浮点误差是财务系统
    的经典缺陷。所有金额字段统一在这里转字符串 —— 一处转换，不是每个能力各转一遍。
    """
    if value is None:
        return value
    return str(value)


def decorate_order(row: dict[str, Any], permissions: frozenset[str]) -> dict[str, Any]:
    """给一行采购单加上前端需要的派生字段。

    派生项与它们的理由：

    * `status_label` —— 中文文案只在这里产生；
    * `total_amount` —— `quantity × unit_price`，**不存列**（冗余列要靠回查保持同步，
      那正是早期版本三处不一致的来源）；金额在服务端算，前端不重复算；
    * `allowed_actions` —— 由状态机算，前端只渲染不推导。权限过滤也在服务端做：
      把无权动作渲染出来再让点击时失败，是把校验成本转嫁给用户；
    * `version` —— 统一叫 `version` 而不是 `row_version`（早期版本前端在 4 处猜过这
      个字段名）。
    """
    status = str(row.get("status") or "")
    actions = allowed_actions_for(PURCHASE_ORDER_WORKFLOW, "purchase_order", status, permissions)

    decorated = dict(row)
    decorated["version"] = int(row.get("row_version") or 1)
    decorated["status_label"] = label_of(PURCHASE_ORDER_WORKFLOW, status)
    decorated["allowed_actions"] = actions

    quantity = row.get("quantity")
    unit_price = row.get("unit_price")
    if quantity is not None and unit_price is not None:
        # 数量 × 单价。**必须按金额口径量化**（`kernel/money.py`）：
        # 两个不同精度的列相乘会产出 7 位小数（实测
        # `total_amount = '62.5000000'`），而它是**派生值**、
        # 没有列定义帮它兜住 —— 与 `sales_order` 同一个缺陷。
        decorated["total_amount"] = _decimal_text(money(quantity * unit_price))
    for key in ("quantity", "unit_price", "received_quantity", "unpaid_amount", "balance"):
        if key in decorated:
            decorated[key] = _decimal_text(decorated[key])
    for key in ("expected_delivery_date", "due_date"):
        if row.get(key) is not None:
            decorated[key] = str(row[key])
    return decorated


def decorate_payable(row: dict[str, Any], permissions: frozenset[str]) -> dict[str, Any]:
    """给一行应付加上派生字段。"""
    status = str(row.get("status") or "")
    decorated = dict(row)
    decorated["version"] = int(row.get("row_version") or 1)
    decorated["status_label"] = label_of(PURCHASE_PAYABLE_WORKFLOW, status)
    # 币种标签（`CNY` → `人民币`）。与 `unit_label` 同一条纪律：码留在行里做机器判定，
    # 派生列给界面渲染；未登记的码回退原词，可诊断。
    decorated["currency_label"] = currency_label(
        str(row.get("currency") or "CNY")
    )
    decorated["allowed_actions"] = [
        str(action)
        for action in PURCHASE_PAYABLE_WORKFLOW.allowed_actions(status)
        if action == RowAction.VIEW or f"finance.payable.{action}" in permissions
    ]

    total = row.get("total_amount")
    paid = row.get("paid_amount")
    if total is not None and paid is not None:
        # 余额是**算出来的**，不存列 —— §4 #4 的 `AmountWithin` 也用同一个算法
        # （`余额 = balance_column − paid_column`）。两处算法相同是刻意的：
        # 列表上显示的余额与"能不能再付"的判断依据必须是同一个数。
        decorated["balance"] = _decimal_text(total - paid)
    for key in ("total_amount", "paid_amount"):
        if key in decorated:
            decorated[key] = _decimal_text(decorated[key])
    for key in ("due_date", "occurred_on"):
        if row.get(key) is not None:
            decorated[key] = str(row[key])
    return decorated


def decorate_payment(row: dict[str, Any], permissions: frozenset[str]) -> dict[str, Any]:
    """给一行付款加上派生字段。"""
    status = str(row.get("status") or "")
    decorated = dict(row)
    decorated["version"] = int(row.get("row_version") or 1)
    decorated["status_label"] = label_of(PURCHASE_PAYMENT_WORKFLOW, status)
    decorated["payment_method_label"] = PAYMENT_METHOD_LABELS.get(
        str(row.get("payment_method") or ""), str(row.get("payment_method") or "")
    )
    decorated["allowed_actions"] = [
        str(action)
        for action in PURCHASE_PAYMENT_WORKFLOW.allowed_actions(status)
        if action == RowAction.VIEW or f"finance.payment.{action}" in permissions
    ]
    if row.get("amount") is not None:
        decorated["amount"] = _decimal_text(row["amount"])
    if row.get("paid_at") is not None:
        decorated["paid_at"] = str(row["paid_at"])
    return decorated


def assert_row_in_scope(scope: Scope, row: dict[str, Any], what: str = "该记录") -> None:
    """第三层范围校验：校验的是**具体那一行**，不是"权限码是否存在"。

    两种校验的失效模式不同 —— 第二层（Gateway）被绕过时，这一层仍然拦得住，
    因为它看的是数据本身。
    """
    if not scope.allows_row(row):
        raise DomainError(ErrorCode.DATA_SCOPE_DENIED, f"{what}不在当前账号的数据范围内")


def tenant_from_scope(
    tx, scope: Scope, *, fallback: dict[str, Any] | None = None
) -> dict[str, int]:
    """把数据范围解析成可落库的分租键。**fail-closed**。

    采购单的 `organization_id` / `farm_id` / `area_id` 是 NOT NULL：
    它们让 `resource(area_id)` 的谓词能直接命中本表列并走索引（Q6 裁决：不做跨表
    JOIN 解析）。所以必须能解析出**具体**的归属。

    解析不出具体区域时抛 `DATA_SCOPE_UNRESOLVED`，**绝不退化成默认值**：
    早期版本 `common/security/data_scope.py:58` 在同样情形下 `return "1=0", []`，
    用户看到 0 行数据却不报错，是本项目要根除的形态之一。

    为什么落到"范围内的最小区域"而不是让调用方传 `area_id`：
    registry §0.7 规则 1 / Q7 —— `farm_id` / `area_id` **不接受客户端提交**。
    如果客户端能指定 `area_id`，"用户只能写自己区域的数据"就退化成"用户自报家门"。

    ## `fallback`：全场范围（`allow_all`）下用**被引用的行**定归属

    实测缺陷（用户报"新建采购单失败：无法确定采购单的归属——当前账号的数据范围是全场"）：
    超级管理员走 `allow_all`，范围里**没有**任何具体区域，于是这里直接抛错 ——
    一个权限最大的账号反而建不了单。

    `allow_all` 的语义是"全场都允许"，所以"落到哪一行被引用的物料所在区域"**不是**
    猜默认值（那才是被禁止的）：它是**范围之内的确定归属**，而且与 `SameTenant`
    不变量（采购单与物料必须同企业）取的是同一个方向。调用方在加载完被引用的
    物料之后再调用本函数，把那一行作为 `fallback` 传进来。

    没有 `fallback` 可依据时仍然 fail-closed —— 不挑第一个企业/区域。
    """
    if scope.allow_all:
        if (
            fallback is not None
            and fallback.get("organization_id") is not None
            and fallback.get("farm_id") is not None
            and fallback.get("area_id") is not None
        ):
            return {
                "organization_id": int(fallback["organization_id"]),
                "farm_id": int(fallback["farm_id"]),
                "area_id": int(fallback["area_id"]),
            }
        raise DomainError(
            ErrorCode.DATA_SCOPE_UNRESOLVED,
            "无法确定采购单的归属：当前账号的数据范围是全场，且无法从被引用的数据推断归属",
        )

    area_ids = sorted(
        entry.bound_id for entry in scope.entries if entry.scope_type is ScopeType.AREA
    )
    if not area_ids:
        # 基地型范围（`sc-farm` = "本基地全部塘口"）没有**具体区域**，但基地下有区域，
        # 归属是确定的 —— 取该基地 id 最小的区域。**实测缺陷**：原先这里直接抛
        # "数据范围不含具体区域"，于是「总经理 / 仓管员」这类基地范围的账号
        # 建不了采购单（权限最大的账号反而被挡）。与 `master_data._scope_keys`
        # 的 `_resolve_org_farm` 同一口径（那里也是 farm → 再向下取区域）。
        farm_ids = sorted(
            entry.bound_id for entry in scope.entries if entry.scope_type is ScopeType.FARM
        )
        for farm_id in farm_ids:
            area = tx.query_one(
                "SELECT id, organization_id, farm_id FROM areas "
                "WHERE farm_id = %s ORDER BY id LIMIT 1",
                (farm_id,),
            )
            if area is not None:
                return {
                    "organization_id": int(area["organization_id"]),
                    "farm_id": int(area["farm_id"]),
                    "area_id": int(area["id"]),
                }
        if farm_ids:
            raise DomainError(
                ErrorCode.DATA_SCOPE_UNRESOLVED,
                f"无法确定采购单的归属：数据范围里的基地 #{farm_ids[0]} 下还没有任何区域",
            )
        raise DomainError(
            ErrorCode.DATA_SCOPE_UNRESOLVED,
            "无法确定采购单的归属：当前账号的数据范围不含具体区域或基地",
        )

    area = tx.query_one(
        "SELECT id, organization_id, farm_id FROM areas WHERE id = %s", (area_ids[0],)
    )
    if area is None:
        # 范围里引用了一个不存在的区域 -> 数据范围本身不可解析，必须报错。
        # 静默换一个区域会让采购单落到错误的区域上，而列表页看不出任何异常。
        raise DomainError(
            ErrorCode.DATA_SCOPE_UNRESOLVED,
            f"无法确定采购单的归属：数据范围引用的区域 {area_ids[0]} 不存在",
        )
    return {
        "organization_id": int(area["organization_id"]),
        "farm_id": int(area["farm_id"]),
        "area_id": int(area["id"]),
    }


def require_date_order(expected_delivery_date: date, due_date: date) -> None:
    """registry §2.8：`due_date` 不得早于 `expected_delivery_date`。

    继承旧 `purchase_service.py:52-60` 的 `_validate_dates`。数据库层也有 CHECK
    兜底（`chk_purchase_orders_dates`），但两者各管一件事：DB 那层保证"不可能写入
    倒挂的日期"（导入/修数脚本也绕不过），这层负责给出**字段级**的可读文案。
    """
    if due_date < expected_delivery_date:
        raise DomainError(
            ErrorCode.FIELD_INVALID,
            "付款到期日不得早于预计到货日期",
            data={
                "field": "due_date",
                "expected_delivery_date": str(expected_delivery_date),
                "due_date": str(due_date),
            },
        )


__all__ = [
    "MAX_AMOUNT",
    "PAYABLE_PAYABLE_STATUSES",
    "PAYMENT_METHODS",
    "PAYMENT_METHOD_LABELS",
    "PURCHASE_ORDER_RECEIVABLE_STATUSES",
    "PURCHASE_ORDER_WORKFLOW",
    "PURCHASE_PAYABLE_WORKFLOW",
    "PURCHASE_PAYMENT_WORKFLOW",
    "TABLE_PURCHASE_ORDER",
    "TABLE_PURCHASE_PAYABLE",
    "TABLE_PURCHASE_PAYMENT",
    "assert_row_in_scope",
    "decorate_order",
    "decorate_payable",
    "decorate_payment",
    "label_of",
    "require_date_order",
    "tenant_from_scope",
]
