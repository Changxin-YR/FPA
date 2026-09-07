"""销售域：销售单、交付、应收、收款。

**四个资源、四个状态机、两种形态**（`docs/CAPABILITY_REGISTRY.md` §3.5 / §3.6）：

    sales_order   订单类，7 态：draft -> submitted -> approved
                              -> partially_delivered -> fully_delivered
                              -> cancelled / closed
    delivery      单据类，2 态：draft -> verified
    receivable    财务类，派生：unpaid -> partial -> settled（**没有独立新建能力**）
    sales_receipt 单据类，2 态：draft -> verified

## 三处刻意的设计决定

**1. `closed` 保留在枚举里但不可达，且**显式标注**。**
§3.5 与 purchase_order 逐字同构：`fully_delivered -> closed` 这一条转移
**没有触发能力**。早期版本同样没有写入点（`早期版本 013_sales_receivables.sql:22` 有该值，
但全仓无人写它）。本版把它保留在状态机里、标注为不可达，而不是偷偷删掉——
`docs/CAPABILITY_REGISTRY.md` §1.11.1 的判据是"显式声明'预留、不可达'比隐藏它好"。

**2. `delivery` 与 `sales_receipt` 都注册 `Resource`，尽管前端没有独立页面。**
`Resource` 的作用不只是渲染列表页：`row_actions` 从它的状态机推导、
`allowed_actions` 由它计算、`OptimisticLock` / `UniqueCode` 靠它的 `table` 找表
（`docs/INVARIANT_TYPES.md` §2）。**为了让它们"有页面"而合并资源会让这三件事丢掉依据。**
`list_path` 仍填 registry §1.8 的真实路径。

**3. 状态的 label 只在本文件出现一次。**
早期版本把同一状态在 14 处各自翻译（`verified` 在成本页叫「待确认」、在其他页叫
「已核验」、在 agent 词典里叫「已提交」）。本仓所有文案一律引用 `State.label`——
`service.py` 里写一次，前端、Agent、错误消息都从这里取。

## 与早期版本的关键差别

* 早期版本 `sales_service.py` 用 `sales.view` / `sales.manage` / `sales.verify` 三个宽泛码；
  本版按 registry §1.8 拆成 `sales.view` / `sales.create` / `sales.approve` /
  `sales.deliver` / `sales.verify` + 三个 finance 码，**`sales.manage` 废弃**。
* 早期版本**只有销售域**实现了"经办人≠审批人"（`早期版本 sales_service.py:121-122`）；
  本版它由内核 `DistinctActors` 在 13 条核验/审批能力上统一执行（Q8/Q15）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fpa.domains.master_data.lookup import lookup_pond

from fpa.kernel.workflow import allowed_actions_for
from fpa.kernel.errors import DomainError, ErrorCode
from fpa.kernel.money import unit_label
from fpa.kernel.fields import Choice, RefTarget
from fpa.kernel.scope import Scope, ScopeType
from fpa.kernel.uow import UnitOfWork
from fpa.kernel.workflow import (
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
# 表名常量
# ============================================================================
#
# 为什么表名要有常量，而不是在每条 SQL 里手写字符串：
#
#   1. `Resource.table=` 与仓储里的 SQL 必须指同一张表。手写两遍就会有一天不一致，
#      而不一致的后果是"查询命中不存在的表"——错误位置离声明处很远。
#   2. SQL 里靠 `""` 拼接时，拼错一个表名不会被任何检查发现；引用常量至少
#      让"哪几张表属于本域"成为可枚举的事实（`git grep TABLE_SALES_` 一目了然）。
#
# 这三个名字已由 负责人 确认（与早期版本库一致、符合本仓 `uq_<表名>_<列>` 规律），
# 并与 `Resource.table=` 逐字相同。

TABLE_SALES_ORDER = "sales_orders"
TABLE_DELIVERY = "deliveries"
TABLE_RECEIVABLE = "receivables"
TABLE_SALES_RECEIPT = "sales_receipts"

#: 收款方式五值。与采购侧 `payment_method` **故意同名同值但不同字段名**（§2.9）。
#: 字段名不统一是继承旧前端的命名（`ReceivablePage.vue` 用 `receipt_method`，
#: `PayablePage.vue` 用 `payment_method`），改名会破坏字段名继承原则（§2.1）。
RECEIPT_METHODS = ("bank_transfer", "cash", "check", "digital_wallet", "other")

#: 收款方式的中文文案。**只在这里出现一次**——早期版本在两个页面各自翻译一遍。
RECEIPT_METHOD_LABELS: dict[str, str] = {
    "bank_transfer": "银行转账",
    "cash": "现金",
    "check": "支票",
    "digital_wallet": "电子钱包",
    "other": "其他",
}

#: 计量单位的中文文案。**只在这里出现一次** —— `Choice.label` 是前端唯一来源，
#: 手写第二遍就是早期版本那 14 处独立翻译的起点。
#: 计量单位三值（§2.9，继承旧 sales_service.py:96-97）。
#:
#: 注意 §3.5 的差异表最后一行：本版保留 tail 枚举，但不再有
#: 「tail 用 quantity、其他用 weight_kg 乘 2」的换算逻辑 —— 那段换算的唯一去处是
#: HarvestQuantityMatch 不变量（§4 #6），不在服务里再写一遍。
SALES_UNITS = ("kg", "jin", "tail")

#: 销售订单的计量单位。**取自内核词表**（`kernel/money.py::UNIT_LABELS`），
#: 不再在本文件里第二次定义—— 否则同一个 `jin` 会有两个中文来源。
#: 下拉选项（`SALES_UNIT_CHOICES`）与行内标签（`unit_label`）因此同源。
SALES_UNIT_LABELS: dict[str, str] = {code: unit_label(code) for code in SALES_UNITS}


def _choices(codes: tuple[str, ...], labels: dict[str, str]) -> tuple[Choice, ...]:
    """把（码元组，文案表）变成 f_enum 需要的 Choice 元组。

    为什么要有这一步：f_enum(label, choices) 的第二个参数是
    ``tuple[Choice, ...]``，直接传字符串元组会让内核在 ``_coerce`` 里撞
    ``'str' object has no attribute 'value'`` —— 而那是**运行期**才炸。
    集中在这里构造，让“码”与“文案”各自只有一处定义。
    """
    return tuple(Choice(value=code, label=labels.get(code, code)) for code in codes)


#: f_enum 需要的 Choice 元组。
SALES_UNIT_CHOICES = _choices(SALES_UNITS, SALES_UNIT_LABELS)

#: f_enum 需要的 Choice 元组（收款方式）。
#:
#: 与采购侧的 payment_method **故意同名同值但不同字段名**（§2.9）：字段名不统一
#: 是继承旧前端的命名（ReceivablePage.vue 用 receipt_method，
#: PayablePage.vue 用 payment_method），改名会破坏字段名继承原则（§2.1）。
RECEIPT_METHOD_CHOICES = _choices(RECEIPT_METHODS, RECEIPT_METHOD_LABELS)


#: `delivery.create` 允许的销售单状态（§2.9）。
#:
#: 这个元组的权威来源是旧前端 `SalePage.vue:46` 的下拉可选范围，
#: registry §2.9 原文引用为 `['approved','partially_delivered']`。
DELIVERABLE_ORDER_STATUSES = ("approved", "partially_delivered")

#: 允许继续收款的应收状态（§2.9，继承旧 `sales_receipt` 的可选范围）。
PAYABLE_RECEIVABLE_STATUSES = ("unpaid", "partial")

#: 金额上限。与采购域取同一量级：`DECIMAL(16,2)` 的可用区间留出安全余量，
#: 目的是让"输入了一个离谱的大数"在字段校验层就被挡住，而不是等到 DB 报错。
MAX_AMOUNT = 99_999_999_999_999.99


# ============================================================================
# 状态机
# ============================================================================

#: 销售单状态机。7 态，转移表与 `purchase_order` **逐字同构**（§3.5）。
#:
#: 同构是刻意的：两个域的业务形状确实一样（下单 → 审批 → 分批收货/交付 → 全部完成），
#: 强行让它们"看起来不同"只会让读代码的人去找并不存在的差异。
#:
#: ## 关于 `_approve()`：为什么是函数而不是 `RowAction.APPROVE`
#:
#: registry §3.0 规则 3 与 §3.5 都要求 `submitted` 的 `row_actions` 是
#: `view, approve, cancel`，前端 `RecordActions.vue:34` 也已备好 `approve: '审批'` 的
#: 文案。但内核 `RowAction` 枚举里**一度没有 `approve` 值**。
#:
#: **绝不改成 `RowAction.VERIFY`**（采购域当前的做法）：前端
#: `ResourceListPage.vue:179-182` 按动作词拼能力名（`sales_order.verify`），找不到就
#: **回退到"该资源第一条 action 能力"**——`sales_order` 的 action 能力是
#: submit / approve / cancel，于是「审批」按钮可能打到 `/cancel`
#: （`confirmation=always` 的高危操作）的端点上。一个词的差别，后果是点错按钮。
#:
#: 因此这里在**构造状态机时**才去取那个枚举值：`RowAction("approve")` 在枚举缺少该值
#: 时抛 `ValueError`（且报错文本会列出全部合法值），而不是静默降级。
#: 把它延迟到函数体内，是为了让**不涉及审批的代码路径现在就能用**——`approve` 只是
#: 状态机里的一个动作词，读路径与 `create/update/submit` 都不需要它。
#: 枚举补上之后，这个函数可以整体替换为 `RowAction.APPROVE`，行为完全一致。
_APPROVE_ACTION_HINT = (
    "内核 RowAction 缺少 'approve'。registry §3.0 规则 3 与 §3.5 都要求 "
    "submitted 的 row_actions 含 approve，前端 RecordActions.vue:34 也已登记 "
    "'审批' 文案。请在 kernel/workflow.py 的 RowAction 里补上 APPROVE = 'approve'；"
    "**不要**改用 RowAction.VERIFY —— 那会让前端的「审批」按钮回退到该资源的"
    "第一条 action 能力（可能是 /cancel）。"
)


def _approve() -> RowAction:
    """取 `approve` 动作词。内核枚举尚未补上时抛 `ValueError`（见上方说明）。"""
    try:
        return RowAction("approve")
    except ValueError as error:  # pragma: no cover - 依赖内核缺口何时修好
        raise ValueError(_APPROVE_ACTION_HINT) from error


SALES_ORDER_WORKFLOW = Workflow(
    resource="sales_order",
    initial="draft",
    states=(
        State(
            "draft", "草稿", Tone.NEUTRAL,
            (RowAction.VIEW, RowAction.EDIT, RowAction.SUBMIT, RowAction.CANCEL),
        ),
        State(
            "submitted", "待审批", Tone.WARNING,
            (RowAction.VIEW, _approve(), RowAction.CANCEL),
        ),
        State(
            "approved", "已审批", Tone.SUCCESS,
            (RowAction.VIEW, RowAction.CANCEL),
        ),
        State("partially_delivered", "部分交付", Tone.INFO, (RowAction.VIEW,)),
        State("fully_delivered", "全部交付", Tone.SUCCESS, (RowAction.VIEW,)),
        State("cancelled", "已取消", Tone.DANGER, (RowAction.VIEW,), terminal=True),
        # `closed` 是**唯一一处显式标注"预留、无触发能力"**的状态（§3.5 差异表原文：
        # "保留在枚举、但无触发能力……本版显式标注为'预留，不可达'"；§3.6 也确认它
        # 是唯一一处）。
        #
        # `reserved=True` 是**机器可读的声明**，不是注释：它让
        # `tests/test_row_actions.py::test_every_reachable_state_has_an_entry`
        # 能把它与"忘了接转移"（Q10 检出的 `batch.close` 那种缺陷）区分开。
        # 例外必须是声明的、不能是推断的——否则下一个人改个名字就静默失效。
        State("closed", "已关闭", Tone.NEUTRAL, reserved=True),
    ),
    transitions=(
        Transition("draft", "submitted", "sales_order.submit"),
        Transition("submitted", "approved", "sales_order.approve"),
        Transition("submitted", "cancelled", "sales_order.cancel"),
        # 交付核验推进状态：累计交付 < 销售量走前者，= 走后者。
        # 公式原样继承旧 `sales_posting.py:38`。
        Transition("approved", "partially_delivered", "delivery.verify"),
        Transition("approved", "fully_delivered", "delivery.verify"),
        Transition("partially_delivered", "fully_delivered", "delivery.verify"),
        Transition("approved", "cancelled", "sales_order.cancel"),
        Transition("partially_delivered", "cancelled", "sales_order.cancel"),
        # ⚠️ `fully_delivered -> closed` **刻意缺席**：没有触发能力。
        # 见本模块 docstring 第 1 条。
    ),
)

#: 单据类的统一两态（§3.6：7 个单据类资源全部 `draft -> verified`）。
#:
#: `harvest` 的四态不同（它由 §7 Q9/Q10 恢复后带 `archived`），所以这里**不能**
#: 做成"全局共享一个 Workflow 常量"——那会让某个域想加状态时被迫改到别人的域。
#: 每个域各自持有同形的一份，是刻意的重复：状态机的**所有权**比它的形状更重要。
#:
#: ## 模板只声明"每个单据都有的动作"，**不声明 `edit`**
#:
#: 初版在 `draft` 上带了 `RowAction.EDIT`，结果 `tools/registry_reconcile.py` 的 [F] 表报了两条：
#:
#:     delivery：声明了 ['edit']，没有能力的末端 token 与之匹配
#:     sales_receipt：声明了 ['edit']，没有能力的末端 token 与之匹配
#:
#: **根因是模板在替使用者承诺**：registry §1.8 给交付与收款**都没有配
#: update 能力**（交付只有 create/verify, 收款只有 create/verify），于是 `edit`
#: 渲染出来点不动：前端按 `{resource}.edit` 拼不出能力，回退链也没有落点。
#:
#: 两条同步收紧：
#:   1. 模板去掉 `EDIT`；
#:   2. `verified -> draft`（触发器 `*.update`）也去掉——它同样指向不存在的能力。
#:
#: **复用本模板的资源，必须自备 update 能力**（并在自己的
#: `Workflow` 里补上 `EDIT` 与 `verified -> draft`）。模板只说它能保证的事。
#:
#: 为什么不用 `Resource.row_action_notes` 记下"我故意声明了但没能力"：
#: 那个字段是给**另一类**不一致准备的——动作确实由某个能力实现、
#: 只是**命名不同**（例如"审批"在能力里叫 `approve`）。而这里是"这个动作
#: **根本没实现**"，记成"有意不一致"会让前端继续渲染一个点不动的按钮，
#: 用户点了拿到 404。**不渲染 > 渲染出来再失败**，这是本项目对
#: `allowed_actions` 的一贯口径。
DOCUMENT_TWO_STATE = Workflow(
    resource="document",
    initial="draft",
    states=(
        State(
            "draft", "草稿", Tone.NEUTRAL,
            (RowAction.VIEW, RowAction.VERIFY),
        ),
        State("verified", "已核验", Tone.SUCCESS, (RowAction.VIEW,), terminal=True),
    ),
    transitions=(
        Transition("draft", "verified", "*.verify"),
    ),
)

#: 交付单状态机（两态）。
#:
#: 它可以用模板，因为交付**有读能力**（`delivery.list`），
#: 于是模板里的 `RowAction.VIEW` 有落点。
DELIVERY_WORKFLOW = Workflow(
    resource="delivery",
    initial="draft",
    states=DOCUMENT_TWO_STATE.states,
    transitions=(
        Transition("draft", "verified", "delivery.verify"),
    ),
)

#: 收款单状态机（两态）。**刻意不复用 `DOCUMENT_TWO_STATE`：收款单没有读能力。**
#:
#: `DOCUMENT_TWO_STATE` 的两个状态都带 `RowAction.VIEW`，而 `view` 在
#: `tools/registry_reconcile.py::ROW_ACTION_TOKENS` 里映射到
#: `list` / `get` / `summary` / `ledger` 四个动作词。收款单自己的两条能力是
#: `sales_receipt.create` 与 `sales_receipt.verify`——**没有一条是读**，
#: 于是 `view` 渲染出来点不动（`registry_reconcile.py` 的 [F] 表把
#: `sales_receipt` 报了出来：`声明了 ['view']，能匹配上的 ['verify']`）。
#:
#: 判据是"**这个资源有没有读能力**"，所以 `delivery` 与 `sales_receipt` 这两个
#: 本该同形的东西在这里必须分开：
#:
#:     delivery       有 `delivery.list`  → 两个状态都保留 `VIEW`
#:     sales_receipt  **没有读能力**       → 去掉 `VIEW`
#:
#: 这不是"给收款单开例外"，而是模板只承诺它保证得了的事——
#: `DOCUMENT_TWO_STATE` 的注释已经写过同一条口径（它当初去掉 `EDIT`，
#: 正是因为 registry §1.8 给交付与收款都没配 update 能力）。
#:
#: **不许用 `Resource.row_action_notes` 记下"我故意声明了却没能力"**：
#: `docs/ROW_ACTIONS.md` §3.1 把那种写法列为不合规（情形 1）——
#: 记成"有意不一致"会让前端继续渲染一个点不动的按钮，用户点了拿到 404。
SALES_RECEIPT_WORKFLOW = Workflow(
    resource="sales_receipt",
    initial="draft",
    states=(
        State("draft", "草稿", Tone.NEUTRAL, (RowAction.VERIFY,)),
        State("verified", "已核验", Tone.SUCCESS, (), terminal=True),
    ),
    transitions=(
        Transition("draft", "verified", "sales_receipt.verify"),
    ),
)

#: 应收状态机。**不是两态单据**：它由 `delivery.verify` 派生，随累计收款推移。
#:
#: 只写 `unpaid` / `partial` / `settled` 三个。`overpaid` 与 `bad_debt` 保留在
#: `receivables` 表的 CHECK 里（与早期版本库语义对齐）但**不可达**：
#:   * `overpaid` —— `AmountWithin` 在源头挡住"收款金额 > 余额"，产生不了超收；
#:   * `bad_debt` —— 核销需要独立能力，registry §6 明确不做。
#: 这是"显式声明预留值"的同一手法：写下它、标注不可达，比让它悄悄消失好。
RECEIVABLE_WORKFLOW = Workflow(
    resource="receivable",
    initial="unpaid",
    states=(
        State("unpaid", "未收款", Tone.WARNING, (RowAction.VIEW,)),
        State("partial", "部分收款", Tone.INFO, (RowAction.VIEW,)),
        State("settled", "已结清", Tone.SUCCESS, (RowAction.VIEW,), terminal=True),
        State("overpaid", "超额收款", Tone.DANGER, (RowAction.VIEW,), terminal=True),
        State("bad_debt", "坏账", Tone.DANGER, (RowAction.VIEW,), terminal=True),
    ),
    # 应收的状态**没有对应的 action 能力**——它由 `sales_receipt.verify` 内部推进。
    # 因此这里不声明 Transition：声明了却没有能力触发，会让"状态是否可达"的检查
    # 与 `REGISTRY` 对不上（t10 那套对账工具正是查这个）。
    transitions=(),
)

#: 四个资源各自的完整状态机（`Resource.workflow` 只接受一个）。
#:
#: 为什么单态资源也要自己的别名而不是直接复用常量：`Workflow.resource` 是
#: "这台状态机属于谁"的标识，被 `StateTransition(machine=...)` 按名查。
#: 让 `delivery` 与 `sales_receipt` 共用同一个 `resource` 名，两边的转移表就会
#: 互相覆盖。


# ============================================================================
# 资源声明（前端列表页的列与状态字典都从这里来）
# ============================================================================

RESOURCES.register(
    Resource(
        name="sales_order",
        title="销售单",
        module="sales",
        list_path="/api/v1/sales-orders",
        detail_path="/api/v1/sales-orders/{order_id}",
        workflow=SALES_ORDER_WORKFLOW,
        # 表名显式声明：`OptimisticLock` / `UniqueCode` 靠 `Resource.table` 找表
        # （`INVARIANT_TYPES.md` §2）。不声明会走 `<module>_<name>s` 兜底，
        # 而兜底"不是契约"——表名一旦靠猜，改表名就会静默失效。
        table="sales_orders",
        # 派生列（§2.9 的"派生列（只读）"清单）：`customer_name` / `pond_name` /
        # `batch_code` / `delivered_quantity` / `total_amount` / `balance`。
        # 全部由 service 计算，**不进表**——早期版本把它们冗余存下再靠三处回查同步。
        columns=(
            ("code", "销售单号"),
            ("name", "销售事项"),
            ("customer_name", "客户"),
            ("pond_name", "塘口"),
            ("batch_code", "批次"),
            ("quantity", "销售数量"),
            # 列点到 `unit_label`（而不是 `unit`）：列名是**展示决定**，
            # 而 `unit` 是机器码（CHECK 约束与不变量的判定依据）。
            # 两者同时存在是刻意的：码供程序，标签供人。
            ("unit_label", "计量单位"),
            ("total_amount", "金额"),
            ("delivered_quantity", "已交付"),
            ("balance", "未交付"),
            ("sold_at", "销售日期"),
            ("due_date", "收款到期日"),
            ("status", "状态码"),
            ("status_label", "状态"),
        ),
        # 逐条对应 `SalesOrderService._filters` 的 `params.get(...)`：
        # keyword / status + 三个外键 + 两组日期区间（销售日期、收款到期日）。
        search=True,
        filters=(
            FilterSpec("status", "状态", FilterKind.STATUS),
            FilterSpec(
                "customer_id",
                "客户",
                FilterKind.REF,
                ref=RefTarget("partner", "name"),
            ),
            FilterSpec(
                "pond_id", "塘口", FilterKind.REF, ref=RefTarget("pond", "name")
            ),
            FilterSpec(
                "batch_id", "批次", FilterKind.REF, ref=RefTarget("batch", "code")
            ),
            # 两组区间各做**一个**控件，参数名由 `params` 给出。
            FilterSpec(
                "sold_at",
                "销售日期",
                FilterKind.DATE_RANGE,
                params=DateRangeParam("sold_from", "sold_to"),
            ),
            FilterSpec(
                "due_date",
                "收款到期日",
                FilterKind.DATE_RANGE,
                params=DateRangeParam("due_from", "due_to"),
            ),
        ),
    )
)

RESOURCES.register(
    Resource(
        name="delivery",
        title="交付单",
        module="sales",
        # 没有独立前端页面，但 `list_path` 必须是 registry §1.8 的真实路径 ——
        # 否则前端按元数据渲染时会指向一个不存在的地址。
        list_path="/api/v1/deliveries",
        detail_path="/api/v1/deliveries/{delivery_id}",
        workflow=DELIVERY_WORKFLOW,
        table="deliveries",
        columns=(
            ("code", "交付单号"),
            ("name", "交付事项"),
            ("order_code", "销售单号"),
            ("quantity", "交付数量"),
            ("unit_label", "计量单位"),
            ("delivered_at", "交付时间"),
            ("harvest_code", "出塘单号"),
            ("transport_info", "运输信息"),
            ("status", "状态码"),
            ("status_label", "状态"),
        ),
        # 对应 `DeliveryService.list_deliveries` 的 `params.get(...)`。
        # 关键词列是 `d.code` / `d.name`。
        search=True,
        filters=(
            FilterSpec("status", "状态", FilterKind.STATUS),
            FilterSpec(
                "sales_order_id",
                "销售单",
                FilterKind.REF,
                ref=RefTarget("sales_order", "code"),
            ),
            FilterSpec(
                "pond_id", "塘口", FilterKind.REF, ref=RefTarget("pond", "name")
            ),
        ),
    )
)

RESOURCES.register(
    Resource(
        name="receivable",
        title="应收",
        module="sales",
        list_path="/api/v1/receivables",
        workflow=RECEIVABLE_WORKFLOW,
        table="receivables",
        columns=(
            ("code", "应收单号"),
            ("name", "应收事项"),
            ("customer_name", "客户"),
            ("order_code", "销售单号"),
            ("total_amount", "应收金额"),
            ("paid_amount", "已收款"),
            ("balance", "未收款"),
            ("due_date", "收款到期日"),
            ("status", "状态码"),
            ("status_label", "状态"),
        ),
        # 对应 `ReceivableService.list_receivables` 的 `params.get(...)`。
        search=True,
        filters=(
            FilterSpec("status", "状态", FilterKind.STATUS),
            FilterSpec(
                "customer_id",
                "客户",
                FilterKind.REF,
                ref=RefTarget("partner", "name"),
            ),
            # "逾期"是**服务端算的财务口径**（`due_date < CURDATE()` 且未结清），
            # 不让前端比日期：客户端时钟会让"逾期"逐台机器不同。
            FilterSpec("overdue", "仅看逾期", FilterKind.BOOLEAN),
        ),
    )
)

RESOURCES.register(
    Resource(
        name="sales_receipt",
        title="收款单",
        module="sales",
        list_path="/api/v1/sales-receipts",
        detail_path="/api/v1/sales-receipts/{receipt_id}",
        workflow=SALES_RECEIPT_WORKFLOW,
        table="sales_receipts",
        columns=(
            ("code", "收款单号"),
            ("name", "收款事项"),
            ("receivable_code", "应收单号"),
            ("amount", "收款金额"),
            ("received_at", "收款日期"),
            ("receipt_method_label", "收款方式"),
            ("status_label", "状态"),
        ),
    )
)


# ============================================================================
# 共享助手
# ============================================================================
#
# 这些函数与 `domains/purchase/service.py` 的同名函数**形状一致**（同样是
# `label_of` / `assert_row_in_scope` / `decorate_*` / `require_date_order`）。
# 刻意不抽到 `_base/` 共用：它们的**参数与列名不同**（采购是 `approved_by`、
# 销售是 `verified_by`；采购的派生列是 `supplier_name`、销售的是 `customer_name`），
# 强行统一会引入一层"参数映射"，而映射表正是本项目要消灭的东西。
# 真正跨域共用的是**判定**（`Scope.allows_row`、`Workflow.allowed_actions`），
# 它们在核心里，不在这里。


def label_of(workflow: Workflow, code: str) -> str:
    """状态码 → 中文。**文案的唯一来源是 `State.label`**。

    早期版本把同一个 `verified` 在成本页叫「待确认」、在其他页叫「已核验」、
    在 agent 词典里叫「已提交」——14 处独立翻译。这里只查状态机。
    """
    for state in workflow.states:
        if state.code == code:
            return state.label
    return code


def receipt_method_label(code: str) -> str:
    """收款方式码 → 中文。未登记时回退原码（不抛错）。

    为什么回退而不是抛错：这个值来自**数据库**，而数据库里的历史值可能比代码新
    （运维直接改过）。回退至少让页面显示得出东西，抛错会让整张列表渲染失败。
    真正的取值约束在字段声明的 `choices` 与迁移的 CHECK 上——**写入**路径是
    fail-closed 的，**读取**路径容忍未知值。
    """
    return RECEIPT_METHOD_LABELS.get(code, code)


def assert_row_in_scope(scope: Scope, row: dict[str, Any], what: str = "该记录") -> None:
    """第三层范围校验：校验的是**具体那一行**，不是"权限码是否存在"。

    两种校验的失效模式不同——第二层（Gateway）被绕过时，这一层仍然拦得住，
    因为它看的是数据本身。
    """
    if not scope.allows_row(row):
        raise DomainError(ErrorCode.DATA_SCOPE_DENIED, f"{what}不在当前账号的数据范围内")


def tenant_from_pond(tx: UnitOfWork, *, scope: Scope, pond_id: int) -> dict[str, int]:
    """从塘口解析出可落库的分租键，并校验该塘口在数据范围内。**fail-closed**。

    ## 为什么归属从 `ponds` 取，而不是让客户端传 `area_id`

    registry §0.7 规则 1 / DECISIONS.md Q7：`organization_id` / `farm_id` / `area_id`
    **不接受客户端提交**。如果客户端能指定 `area_id`，"用户只能写自己区域的数据"
    就退化成"用户自报家门"——早期版本正是这样（三个字段都在 payload 里，
    再靠 `_scoped()` 等三套实现回查补全）。

    ## 为什么"全场范围"在这里是允许的，而采购域不允许

    采购域的 `tenant_from_scope` 在 `scope.allow_all` 时抛 `DATA_SCOPE_UNRESOLVED`，
    因为采购单没有"归属对象"可查——它必须由**范围本身**决定归属，
    而"全场"意味着归属不唯一。

    销售单不同：它**必然挂在一个塘口上**（`pond_id` 是必填字段，§2.9），
    而塘口自己带 `organization_id` / `farm_id` / `area_id`。所以归属是**查得出来的**，
    "全场范围"不构成歧义。super_admin 因此能正常建销售单，而不需要额外的例外逻辑。

    这与"不返回空集"的纪律不冲突：这里从不猜，查不到就报错。
    """
    # \ponds\ 属于 master_data 域：按 ROLLOUT_CONTRACT §2.0 走它的具名只读入口，
    # 不直读别人的表。返回 None 而不是抛错 -> 由**这里**给出字段级可读错误。
    pond = lookup_pond(tx, pond_id=int(pond_id))
    if pond is None:
        raise DomainError(
            ErrorCode.FIELD_INVALID, "所属塘口不存在", data={"field": "pond_id"}
        )

    # 第三层防御：塘口本身必须在范围内。**这一条对全场范围也执行**
    # （`Scope.allows_row` 在 allow_all 时返回 True，语义正确）。
    if not scope.allows_row(pond):
        raise DomainError(
            ErrorCode.DATA_SCOPE_DENIED, "所属塘口不在当前账号的数据范围内"
        )

    # 除全场范围之外，还要求"范围确实含具体区域"——
    # 否则 `resource(area_id)` 的谓词在列表查询里会退化成不可解释的空集。
    if not scope.allow_all:
        area_ids = {
            entry.bound_id for entry in scope.entries if entry.scope_type is ScopeType.AREA
        }
        if area_ids and int(pond["area_id"]) not in area_ids:
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED, "所属塘口不在当前账号的数据范围内"
            )

    return {
        "organization_id": int(pond["organization_id"]),
        "farm_id": int(pond["farm_id"]),
        "area_id": int(pond["area_id"]),
    }


def require_date_order(earlier: date | None, later: date | None, *, message: str) -> None:
    """日期先后校验。继承旧 `sales_service.py:39-46` 的口径。

    参数可以是 `None`（更新场景下字段可能未提交），此时跳过——
    "缺事实就跳过"是全部字段级校验的统一口径：把"不适用"报成错误会让
    部分更新永远无法通过。
    """
    if earlier is None or later is None:
        return
    if later < earlier:
        raise DomainError(ErrorCode.FIELD_INVALID, message)


def _decimal_text(value: Any) -> Any:
    """`Decimal` → 字符串。

    为什么不在 JSON 里给数字：`DECIMAL(16,2)` 的精度在 JavaScript 的 `number`
    里表达不了（`0.1 + 0.2 !== 0.3`）。金额与数量一律以字符串出库，
    由前端按其精度要求格式化——这是"服务端算，前端渲染"的同一原则。
    """
    if isinstance(value, Decimal):
        return str(value)
    return value


def decorate_sales_order(row: dict[str, Any], permissions: frozenset[str]) -> dict[str, Any]:
    """给销售单加派生字段：状态文案、可执行动作、金额与累计交付。

    ## 派生字段一个都不进表

    §2.9 列的 6 个派生列（`customer_name` / `pond_name` / `batch_code` /
    `delivered_quantity` / `total_amount` / `balance`）全部在这里算或由 SQL 带出。
    早期版本把它们冗余存进表，再用三处回查保持同步——那是"两处描述同一件事"的
    教科书形态，也是 `DEVELOPMENT.md` §4 要根除的对象。

    ## `allowed_actions` 要按权限过滤

    过滤放在服务端而不是前端：`allowed_actions` 是**渲染依据**，把无权动作渲染出来
    再让点击时失败，等于把校验成本转嫁给用户。判定用的是与本能力**同一个**
    `permissions` 集合（三层防御用的是同一份事实）。
    """
    status = str(row.get("status") or "")
    decorated = dict(row)

    decorated["version"] = int(row.get("row_version") or 1)
    decorated["status_label"] = label_of(SALES_ORDER_WORKFLOW, status)
    decorated["allowed_actions"] = _allowed_actions(
        SALES_ORDER_WORKFLOW, "sales_order", status, permissions
    )

    # 计量单位的**展示标签**。码本身（`kg`/`jin`/`tail`）是 CHECK 约束与
    # `HarvestQuantityMatch` 的判定依据，不能改；缺的只是给人看的那个词。
    # 文案来源是内核 `UNIT_LABELS`（与 `ACTION_LABELS` 同一纪律：前端不写映射表）。
    if decorated.get("unit") is not None:
        decorated["unit_label"] = unit_label(str(decorated["unit"]))

    for key in ("quantity", "unit_price", "total_amount", "delivered_quantity", "balance"):
        if key in decorated:
            decorated[key] = _decimal_text(decorated[key])
    return decorated


def _allowed_actions(
    workflow: Workflow,
    resource: str,
    status: str,
    permissions: frozenset[str],
) -> list[str]:
    """状态机给的动作 × 当前账号的权限 = 真正可渲染的动作。

    判定规则（继承 `PondService._decorate`）：动作 `x` 只要有任一权限码
    `<resource>.x` 或 `<resource>.<status>.x` 就放行。`view` 永远放行
    （看不到就不该看到这一行，那是范围的事，不是动作权限的事）。

    `approve` 这类动作的权限码与动作词**不同名**（权限是 `sales.approve`，
    动作是 `approve`；但 `sales_order.submit` 的权限码却是 `sales.create`），
    所以这里不能用"拼权限码"的办法——必须查 `REGISTRY` 拿该能力的
    `required_permission`。这也顺带保证了"渲染出的按钮"与"存在的路由"一致。
    """
    # 过滤口径只有一处（`kernel/workflow.py::allowed_actions_for`）。
    # 这里原先自己实现了一遍，且按 `<资源>.<动作>` 找能力 —— 于是 `edit` 永远找不到
    # 对应能力（它的能力叫 `update`），「编辑」按钮因此从不渲染。
    return allowed_actions_for(workflow, resource, status, permissions)


def decorate_delivery(row: dict[str, Any], permissions: frozenset[str]) -> dict[str, Any]:
    """给交付单加派生字段。"""
    status = str(row.get("status") or "")
    decorated = dict(row)
    decorated["version"] = int(row.get("row_version") or 1)
    decorated["status_label"] = label_of(DELIVERY_WORKFLOW, status)
    decorated["allowed_actions"] = _allowed_actions(
        DELIVERY_WORKFLOW, "delivery", status, permissions
    )
    if decorated.get("unit") is not None:
        decorated["unit_label"] = unit_label(str(decorated["unit"]))
    for key in ("quantity",):
        if key in decorated:
            decorated[key] = _decimal_text(decorated[key])
    return decorated


def decorate_receivable(row: dict[str, Any], permissions: frozenset[str]) -> dict[str, Any]:
    """给应收加派生字段。

    `balance`（未收款）是 `total_amount − paid_amount`——**算出来的，不存列**。
    应收状态机没有 action 能力（它由 `sales_receipt.verify` 内部推进），
    所以 `allowed_actions` 只有 `view`。
    """
    status = str(row.get("status") or "")
    decorated = dict(row)
    decorated["version"] = int(row.get("row_version") or 1)
    decorated["status_label"] = label_of(RECEIVABLE_WORKFLOW, status)
    decorated["allowed_actions"] = _allowed_actions(
        RECEIVABLE_WORKFLOW, "receivable", status, permissions
    )

    total = row.get("total_amount")
    paid = row.get("paid_amount")
    if total is not None and paid is not None:
        decorated["balance"] = str(Decimal(str(total)) - Decimal(str(paid)))
    for key in ("total_amount", "paid_amount"):
        if key in decorated:
            decorated[key] = _decimal_text(decorated[key])
    return decorated


def decorate_receipt(row: dict[str, Any], permissions: frozenset[str]) -> dict[str, Any]:
    """给收款单加派生字段。"""
    status = str(row.get("status") or "")
    decorated = dict(row)
    decorated["version"] = int(row.get("row_version") or 1)
    decorated["status_label"] = label_of(SALES_RECEIPT_WORKFLOW, status)
    decorated["allowed_actions"] = _allowed_actions(
        SALES_RECEIPT_WORKFLOW, "sales_receipt", status, permissions
    )
    if row.get("receipt_method") is not None:
        decorated["receipt_method_label"] = receipt_method_label(str(row["receipt_method"]))
    for key in ("amount",):
        if key in decorated:
            decorated[key] = _decimal_text(decorated[key])
    return decorated
