"""仓储域：仓库、到货单、领用单、库存台账。

本域是四个域的**公共事实源**：warehouse 自己的 `receipt.verify` / `issue.verify`、
production 的 `feeding.verify`、以及 sales 的成本/交付侧都要读这本账。
所以它的表与入口先于其它域定稿（`docs/ROLLOUT_CONTRACT.md` §2 / §2A）。

## 三个设计要点

**1. 单据类资源只有 `draft` / `verified` 两态，没有 `submitted`。**

依据 registry §3.3 行 1114：**"单据类资源不做 `submitted` 中间态——减少一次往返"**。
理由在原文里：投喂的"提交"与"核验"在早期版本由同一角色完成（旧
`production_service.py:237-247` 的 submit 与 `:249+` 的 verify 权限码同为
`production.verify`），中间态不产生额外管控价值。仓储的到货/领用同构，继承同一决定。

**2. `reserved` 这个状态不存在——这是刻意的。**

早期版本 `warehouse_documents.status` 里有 `in_transit` 等中间态（调拨用），
本版砍掉调拨（registry §6），于是 `in_transit` **在新系统里不存在**。
少一个状态不等于少一条规则，而是少一条**永远不会被验证**的规则。

**3. 状态机的 label 只在这里出现一次。**

`status_dict`、`row_actions`、`allowed_actions`、前端按钮文案全部从 `Workflow` 派生。
早期版本把同一个状态在 14 处各自翻译（`verified` 在成本页叫「待确认」、在其他页叫
「已核验」），本文件是这些文案的**唯一来源**。
"""

from __future__ import annotations

from fpa.kernel.fields import Choice, RefTarget
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
# 码 → 中文
# ============================================================================
#
# 这三张表**只在这里出现一次**（早期版本把同一个状态在 14 处各自翻译）。
# 资源声明里的筛选候选值（`Choice`）与列表里派生的 `…_label` 都从它们取，
# 因此它们必须在资源声明**之前**定义 —— 与 cost 域的 `SOURCE_TYPE_LABELS` 同一处置。

#: 单据类型的中文文案。
DOC_TYPE_LABELS: dict[str, str] = {"receipt": "到货单", "issue": "领用单"}

#: 账本来源类型的中文文案。
SOURCE_TYPE_LABELS: dict[str, str] = {"receipt": "入库", "issue": "出库"}

#: 可用的单据类型筛选值（服务端校验，不接受任意字符串）。
DOC_TYPES: tuple[str, ...] = ("receipt", "issue")

# ============================================================================
# 状态机
# ============================================================================

#: 单据类资源的状态机（到货单与领用单共用，对外资源名 `warehouse_document`）。
#:
#: `draft → verified` 由 `receipt.verify` / `issue.verify` 触发。
#:
#: ## 为什么这里**没有** `edit` / `view` / `archive`
#:
#: 三个动作都**没有能力实现**，而 `row_actions` 是"该状态允许什么动作"的声明
#: （前端只渲染服务端给的 `allowed_actions`）——声明它们会渲染出点不动的按钮。
#: `tools/registry_reconcile.py` 的 [F] 表正是这样把 `warehouse_document` 报出来的：
#: `声明了 ['edit', 'view']，能匹配上的 ['verify']`。逐条核对 registry §1.6：
#:
#:     edit     需要 `warehouse_document.update`。§1.6 的 7 条里没有它，
#:              而旧注释写的是"已核验的单据改一个字就会让已入的账与单据不符"
#:              ——也就是说这**不是疏漏，是刻意的**。既然没有能力，动作就不该声明。
#:     view     在 `ROW_ACTION_TOKENS` 里映射到 `list` / `get` / `summary` / `ledger`。
#:              单据上的读能力**不存在**：§1.6 与单据有关的只有
#:              `receipt.create` / `receipt.verify` / `issue.create` / `issue.verify`，
#:              而 `warehouse.list` / `inventory.list` / `inventory.ledger` 属于
#:              `warehouse` / `inventory_lot` / `inventory_ledger` 三个**别的**资源。
#:              所以单据列表当前没有端点，`view` 同样渲染不出来。
#:     archive  `draft → archived` 的触发能力 `*.archive` 不存在。
#:              "填错的草稿作废而不必物理删除"这件事本身是对的（registry §0.2），
#:              但它需要一条能力，而那条能力不在 §1.6 的清单里——所以这条转移一并删掉。
#:
#: 删掉之后 `archived` 状态码**仍然保留**：`warehouse_documents.status` 是
#: `VARCHAR(32)`，而 `status_dict` 要给前端中文标签。状态码与动作回答的是两个不同
#: 的问题——这一列能取什么值（数据侧）vs 用户在这一行上能做什么（能力侧）。
#: `reserved=True` 把"尚无能力能进入它"写成**机器可读的声明**（不是注释），
#: 于是可达性断言能把它与"忘了接转移"区分开。
#:
#: 判据与先例见 `docs/ROW_ACTIONS.md` §3.1，以及 `domains/sales/service.py` 的
#: `DOCUMENT_TWO_STATE`（它当初去掉 `EDIT` 正是因为同一件事）。
DOC_STATUS_WORKFLOW = Workflow(
    resource="warehouse_document",
    initial="draft",
    states=(
        State("draft", "草稿", Tone.NEUTRAL, (RowAction.VERIFY,)),
        State("verified", "已核验", Tone.SUCCESS, (), terminal=True),
        State("archived", "已归档", Tone.NEUTRAL, (), terminal=True, reserved=True),
    ),
    transitions=(
        Transition("draft", "verified", "*.verify"),
    ),
)

#: 物料批次状态机。registry §3.3-A：**只保留 `available` / `closed`**。
#: `quarantined` / `expired` 需要质量管控与到期任务，超出闭环（§6 砍除）；
#: "过期"降级为**查询时计算**（按 `expiry_date` 排序），不进状态机。
INVENTORY_LOT_WORKFLOW = Workflow(
    resource="inventory_lot",
    initial="available",
    states=(
        State("available", "可用", Tone.SUCCESS, (RowAction.VIEW,)),
        # 当前能力清单没有关闭物料批次的业务入口；保留状态值供历史数据展示，
        # 但明确标记为预留，避免状态机伪造一个永远走不通的关闭操作。
        State("closed", "已关闭", Tone.NEUTRAL, (), terminal=True, reserved=True),
    ),
    transitions=(),
)

#: `warehouses` 表上**真实存在**的分租列。
#:
#: 声明的唯一用途是喂给内核的 `scope_predicate_for_columns`（它只渲染这里面的列）。
#: 放在 `service.py` 而不是 `inventory.py`：`ledger._default_warehouse_id`
#: 也要用同一条判据，而"这张表有哪些分租列"是**一个事实**、不该有第二处答案；
#: 同时 `ledger.py` 与 `inventory.py` 之间存在真实的反向依赖
#: （`inventory` 的库存汇总要调 `ledger` 的取数），把常量放在 `service.py`
#: 让它同时被两者读到而不成环。
WAREHOUSE_SCOPE_COLUMNS: tuple[str, ...] = ("area_id", "farm_id", "organization_id")


#: 仓库自身的状态。
#:
#: ## 它为什么是一条完整的生命周期转移表
#:
#: **2026-09-15 修**：用户报「仓库新建后自动核验完成」。原实现把初始状态定成
#: `verified`，理由是"没有 `warehouse.submit` / `verify` 能力，`draft` 会永远卡住"。
#: 那个理由本身没错，但结论错了：正确做法是**把那条路径补齐**，而不是让字典资源
#: 绕过整个核验流程。于是：
#:
#:   * 新增 `warehouse.submit` / `warehouse.verify` 两条能力（声明与转移同时落地）；
#:   * 初始状态改回 `draft`，与区域 / 物料同一口径；
#:   * 补上**原本缺失的 `archived` 状态** —— `warehouse.archive` 一直在写
#:     `status='archived'`，而状态表里没有这个值，归档后的行会退化成裸英文
#:     `archived` 且没有任何允许动作（同一个"声明与能力不一致"的缺陷）。
#:
#: 「声明与能力必须同时落地」仍然是本表的纪律：`tests/test_row_actions.py` 会检查
#: 每条转移引用的 `<资源>.<动作>` 能力真的存在。
WAREHOUSE_WORKFLOW = Workflow(
    resource="warehouse",
    initial="draft",
    states=(
        State(
            "draft",
            "草稿",
            Tone.NEUTRAL,
            (RowAction.VIEW, RowAction.EDIT, RowAction.SUBMIT),
        ),
        State("submitted", "待核验", Tone.WARNING, (RowAction.VIEW, RowAction.VERIFY)),
        State(
            "verified",
            "已核验",
            Tone.SUCCESS,
            (RowAction.VIEW, RowAction.EDIT, RowAction.ARCHIVE),
        ),
        State("archived", "已归档", Tone.NEUTRAL, (), terminal=True),
    ),
    transitions=(
        Transition("draft", "submitted", "*.submit"),
        Transition("submitted", "verified", "*.verify"),
        Transition("submitted", "draft", "*.update"),
        Transition("draft", "archived", "*.archive"),
        Transition("verified", "archived", "*.archive"),
    ),
)

#: 到货单/领用单的状态机（对外名：`warehouse_document`）。
RECEIPT_WORKFLOW = DOC_STATUS_WORKFLOW
ISSUE_WORKFLOW = DOC_STATUS_WORKFLOW


# ============================================================================
# 资源声明（前端列表页的列与状态字典都从这里来）
# ============================================================================

RESOURCES.register(
    Resource(
        name="warehouse",
        title="仓库",
        module="warehouse",
        list_path="/api/v1/warehouses",
        detail_path="/api/v1/warehouses/{warehouse_id}",
        workflow=WAREHOUSE_WORKFLOW,
        table="warehouses",
        columns=(
            ("code", "仓库编号"),
            ("name", "仓库名称"),
            ("area_name", "所属区域"),
            ("contact_name", "联系人"),
            # 同 `unit_label`：列点到展示标签，而非 `1`/`0`。
            ("is_default_label", "默认出库仓"),
            ("status", "状态码"),
            ("status_label", "状态"),
        ),
        # 对应 `WarehouseQueryService.list_warehouses` 的 `params.get(...)`：
        # **只有** keyword 与 status。区域/联系人等参数后端不接受，
        # 因此不声明（声明了就是永远筛不动的控件）。
        search=True,
        filters=(FilterSpec("status", "状态", FilterKind.STATUS),),
    )
)

RESOURCES.register(
    Resource(
        name="warehouse_document",
        title="仓储单据",
        module="warehouse",
        list_path="/api/v1/receipts",
        detail_path="/api/v1/receipts/{receipt_id}",
        workflow=DOC_STATUS_WORKFLOW,
        table="warehouse_documents",
        columns=(
            ("code", "单据编号"),
            ("name", "事项"),
            ("doc_type_label", "单据类型"),
            ("warehouse_name", "仓库"),
            ("material_name", "物料"),
            ("quantity", "数量"),
            ("lot_no", "物料批次"),
            ("happened_at", "发生时间"),
            ("status_label", "状态"),
        ),
    )
)

RESOURCES.register(
    Resource(
        name="issue",
        title="领用出库",
        module="warehouse",
        list_path="/api/v1/issues",
        detail_path="/api/v1/issues/{issue_id}",
        workflow=ISSUE_WORKFLOW,
        table="warehouse_documents",
        columns=(
            ("code", "出库单号"),
            ("name", "出库事项"),
            ("warehouse_name", "出库仓"),
            ("material_name", "物料"),
            ("quantity", "出库数量"),
            ("lot_no", "物料批次"),
            ("happened_at", "出库时间"),
            ("status_label", "状态"),
        ),
    )
)

RESOURCES.register(
    Resource(
        name="inventory_lot",
        title="物料批次",
        module="warehouse",
        list_path="/api/v1/inventory",
        detail_path="/api/v1/inventory/{lot_id}",
        workflow=INVENTORY_LOT_WORKFLOW,
        table="inventory_lots",
        columns=(
            ("lot_no", "物料批次号"),
            ("material_name", "物料"),
            ("warehouse_name", "仓库"),
            # 点到**复合列**：数字与它的单位是一个事实（与 `cost` 域的 `target_label` 同手法）。
            ("quantity_with_unit", "可用数量"),
            ("expiry_date", "有效期"),
            ("status", "状态码"),
            ("status_label", "状态"),
        ),
        # 对应 `WarehouseQueryService.list_inventory` 的 `params.get(...)`。
        # 关键词列是 `l.lot_no` / `m.name`（批次号或物料名）。
        search=True,
        filters=(
            FilterSpec(
                "warehouse_id",
                "仓库",
                FilterKind.REF,
                ref=RefTarget("warehouse", "name"),
            ),
            FilterSpec(
                "material_id",
                "物料",
                FilterKind.REF,
                ref=RefTarget("material", "name"),
            ),
            FilterSpec("lot_no", "物料批次号", FilterKind.STRING),
            # `only_available` 在服务端翻成 `l.status = 'available'`：
            # 让前端拼 status 会把"可用"这个口径复制到第二处。
            FilterSpec("only_available", "仅看可用", FilterKind.BOOLEAN),
        ),
    )
)

RESOURCES.register(
    Resource(
        name="inventory_ledger",
        title="库存流水",
        module="warehouse",
        list_path="/api/v1/inventory/ledger",
        table="inventory_ledger",
        columns=(
            ("happened_at", "发生时间"),
            ("warehouse_name", "仓库"),
            ("material_name", "物料"),
            ("lot_no", "物料批次"),
            ("source_type", "来源类型码"),
            ("source_type_label", "来源类型"),
            ("source_ref", "来源单据"),
            ("quantity_with_unit", "增减（含单位）"),
            ("balance_after", "结存"),
        ),
        # 对应 `WarehouseQueryService.list_inventory_ledger` 的 `params.get(...)`。
        # **没有 keyword**：流水表按仓库/物料/批次/来源查，处理器也不接受 keyword。
        filters=(
            FilterSpec(
                "warehouse_id",
                "仓库",
                FilterKind.REF,
                ref=RefTarget("warehouse", "name"),
            ),
            FilterSpec(
                "material_id",
                "物料",
                FilterKind.REF,
                ref=RefTarget("material", "name"),
            ),
            FilterSpec("lot_no", "物料批次", FilterKind.STRING),
            FilterSpec(
                "source_type",
                "来源类型",
                FilterKind.ENUM,
                # 候选值与中文取自 `SOURCE_TYPE_LABELS`（唯一中文来源）。
                choices=tuple(
                    Choice(code, label) for code, label in SOURCE_TYPE_LABELS.items()
                ),
            ),
            FilterSpec("source_ref", "来源单据", FilterKind.STRING),
            FilterSpec(
                "happened_at",
                "发生时间",
                FilterKind.DATE_RANGE,
                params=DateRangeParam("date_from", "date_to"),
            ),
        ),
    )
)

__all__ = [
    "DOC_STATUS_WORKFLOW",
    "INVENTORY_LOT_WORKFLOW",
    "ISSUE_WORKFLOW",
    "RECEIPT_WORKFLOW",
    "WAREHOUSE_WORKFLOW",
]
