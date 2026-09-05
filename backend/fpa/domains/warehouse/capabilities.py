"""仓储域的能力声明（registry §1.6 的 7 条）。

**这是"声明即派生"的落点**：一个 `Capability(...)` 同时派生

    REST 路由（`web/app.py` 按 `method` + `path` 自动注册）
    权限码（`required_permission`）
    DataScope 谓词（`scope`）
    幂等策略（`idempotent`）
    人工确认闸门（`risk` + `confirmation`）
    审计字段（`audit`）
    Agent Tool schema（`agent_exposure` + 从处理器标注收集的字段）
    前端表单与列（`fields` + 资源声明的 `columns`）

所以本文件里**不写**任何路由、校验、权限判断的代码——那些都是派生物。

## 不变量声明（registry §4 在本域的强制点）

| # | 不变量 | 挂在哪条能力 | 声明形态 |
|---|---|---|---|
| 1 | 不得负库存 | `issue.verify` | `NoNegativeStock(group_by=(warehouse_id, material_id, inventory_lot_id))` |
| 3 | 未审批不得收货 | `receipt.create` + `receipt.verify` | `ReferencedStatus(purchase_order_id -> purchase_orders)` |
| 5 | 经办人 ≠ 审批人 | `receipt.verify`、`issue.verify` | `DistinctActors()` |
| 7 | 已关账期间不得入账 | `receipt.verify`、`issue.verify` | `PeriodOpen(date_field="happened_at")` |
| 9 | 累计到货不得超过采购数量 | `receipt.verify` | `CumulativeWithin(target=purchase_orders, children=warehouse_documents)` |
| 18 | 同企业且物料有效 | `receipt.create`、`issue.create` | `SameTenant(...)` + `ReferencedStatus(material_id -> materials)` |
| 19 | 编码在范围内唯一 | `receipt.create`、`issue.create` | `UniqueCode(scope=(organization_id, doc_type))` |

## 仓库主数据（t18 新增的 4 条，见 §7 Q22）

原 §1.6 的 7 条能力里只有 `warehouse.list` 读**仓库这一行**，其余六条都作用于
单据与库存。t18 补上 `warehouse.get` / `create` / `update` / `archive`，
让"仓库"成为与区域/物料同级的**可维护字典资源**。

### 为什么这四条写在 `warehouse` 域而不是 `master_data`

`tests/test_architecture.py::test_capability_domain_matches_its_declaring_directory`
是一条**集合相等**断言：每条能力的 `domain` 必须等于它所在声明目录的域名。
把 `domain="master_data"` 写在仓库能力上是**静默错域**（`Registry.by_domain`、
前端按域菜单、`runner` 的租户分支全部跟着错），而它不会让任何既有检查变红。
判据是**处理器模块路径**——而仓库的写路径属于仓储域（它要管 `is_default`
与 `status='verified'` 的可用性口径）。所以：域归属看代码在哪，不看
"这一轮是谁在做"。

### 两条必须写下来的"为什么"

**① `PeriodOpen` 的 `date_field` 是 `happened_at`，不是默认的 `occurred_on`。**
仓储单据用的是 `happened_at`（registry §2.7，继承旧字段名不改）。如果不覆盖
`date_field`，这个不变量会去 payload 里找一个永远不存在的字段、拿不到日期就
**静默通过**——一条永不放行的规则和一条永不触发的规则一样是缺陷。

**② `UniqueCode.scope` 有两列，不只是 `organization_id`。**
唯一键是 `uq_warehouse_documents_org_type_code (organization_id, doc_type, code)`
（registry §4 #19 点名的键名）：**到货单 RCV-001 与领用单 RCV-001 是两个合法编号**。
只声明 `organization_id` 会把它们误判成重复。`doc_type` 不是分租列，但它是这个
唯一键的组成部分——`scope` 在这里是"唯一键的完整前缀"的意思。
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
from fpa.kernel.invariants import (
    CumulativeWithin,
    DistinctActors,
    NoNegativeStock,
    OptimisticLock,
    PeriodOpen,
    ReferencedStatus,
    SameTenant,
    StateTransition,
    StatusAllowsEdit,
    UniqueCode,
)
from fpa.kernel.scope import ScopePolicy

from .inventory import WarehouseQueryService
from .receipt_write import WarehouseDocumentService
from .warehouse_write import WarehouseWriteService

#: 所有业务表都带 `organization_id` / `farm_id` / `area_id` 三列（DECISIONS Q6），
#: 因此 `resource(area_id)` 直接命中本表列走索引——不需要任何 `via` 跨表解析。
_SCOPE = ScopePolicy.resource("area_id")

#: 写能力默认对智能体开放。
#:
#: 需求明确要求普通业务不能被随意标 `human_only`；管控靠 L1 权限 + DataScope +
#: 不变量 + 确认闸门，而不是靠"不让 Agent 碰"。
_EXPOSED = AgentExposure.EXPOSED

#: 库存账本的分组列——与 §4 #1 / 006 迁移的索引前导列**逐字一致**。
#: 三处（不变量声明、迁移索引、余额查询）必须指同一组列，否则"列表说还有 5"
#: 与"核验说不够"会同时成立。
_STOCK_GROUP_BY = ("warehouse_id", "material_id", "inventory_lot_id")

#: 同企业校验涉及的引用字段及其所在表。
#:
#: 顺序有意义：`SameTenant` 用**第一个取到分租键的字段**当锚点，后续字段与它比较。
#: `warehouse_id` 放最前是因为仓库行三列（org/farm/area）齐全，锚点最稳。
_SAME_TENANT_FIELDS = ("warehouse_id", "material_id", "pond_id", "batch_id")
_SAME_TENANT_TABLES = {
    "warehouse_id": "warehouses",
    "material_id": "materials",
    "pond_id": "ponds",
    "batch_id": "production_batches",
}

#: 物料必须已核验（§4 #18 的后半句）。
_MATERIAL_VERIFIED = ReferencedStatus(
    field="material_id", table="materials", statuses=("verified",), label="物料"
)


def _register_all() -> None:
    """登记仓储域的全部能力。

    用函数包裹而不是模块级裸语句：重复导入这个模块（例如测试里 reload）不会因为
    "能力重复注册"而炸——`REGISTRY.register` 对重名是显式抛错的。
    """
    if REGISTRY.find("warehouse.list") is not None:
        return  # 已注册（重复导入）

    # ---------------------------------------------- 仓库主数据（t18 新增 4 条）
    #
    # registry §1.6 原来只有 7 条，仓库**连详情读都没有**（`Resource` 声明了
    # `detail_path` 却没有对应能力 —— 行内"查看"要么 404、要么拿列表行凑）。
    # t18 把它补成可维护资源：get + create + update + archive。
    #
    # 这四条**不属于** `master_data` 域：处理器模块是 `fpa.domains.warehouse.*`，
    # 而 `tests/test_architecture.py` 有一条集合相等断言（能力声明的 `domain`
    # 必须等于它**所在声明目录**的域名）——把 `domain="master_data"` 写在
    # 仓库能力上是**静默错域**（`Registry.by_domain`、前端按域菜单、
    # `runner` 的 `resource_type` 全部跟着错），所以这里一律 `domain="warehouse"`。

    REGISTRY.register(
        Capability(
            name="warehouse.get",
            title="仓库详情",
            domain="warehouse",
            resource="warehouse",
            handler=WarehouseQueryService.get_warehouse_by_id,
            service_factory=WarehouseQueryService,
            method=HttpMethod.GET,
            path="/api/v1/warehouses/{warehouse_id}",
            kind="read",
            required_permission="warehouse.view",
            scope=_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="查询单个仓库的完整信息，含当前状态与可用的操作",
        )
    )

    REGISTRY.register(
        Capability(
            name="warehouse.create",
            title="新建仓库",
            domain="warehouse",
            resource="warehouse",
            loader=WarehouseQueryService.load_warehouse,
            handler=WarehouseWriteService.create_warehouse,
            service_factory=WarehouseWriteService,
            method=HttpMethod.POST,
            path="/api/v1/warehouses",
            kind="create",
            required_permission="warehouse.create",
            scope=_SCOPE,
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # DB 唯一键 `uq_warehouses_farm_code(farm_id, code)` 是并发下的正确性
                # 底线；本不变量负责给出**可读的** 409 而不是数据库报错。两层都要有。
                UniqueCode(
                    fields=("code",),
                    scope=("farm_id",),
                    table="warehouses",
                    labels=("仓库编号",),
                ),
            ),
            description=(
                "新建一个可用仓库。编号在基地内唯一；数据归属由服务端按数据范围解析"
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="warehouse.update",
            title="编辑仓库",
            domain="warehouse",
            resource="warehouse",
            loader=WarehouseQueryService.load_warehouse,
            handler=WarehouseWriteService.update_warehouse,
            service_factory=WarehouseWriteService,
            method=HttpMethod.PATCH,
            path="/api/v1/warehouses/{warehouse_id}",
            kind="update",
            required_permission="warehouse.update",
            scope=_SCOPE,
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            audit=AuditPolicy.snapshot(),
            description="修改仓库的名称、地址与联系人。仓库编号不可修改；已归档的仓库不能改",
            invariants=(
                OptimisticLock(label="该仓库"),
                StatusAllowsEdit(label="仓库"),
            ),
        )
    )

    # ---------------------------------------------------------- 核验流程两条
    #
    # 2026-09-15：用户报「仓库新建后自动核验完成」。原先刻意没有提交/核验两步，
    # 于是 `warehouse.create` 直接落 `verified`（理由见 `service.py` 的状态表注释）。
    # 与区域/物料同一口径的**正确修法**是把这两步补齐，而不是让字典资源绕过核验。
    #
    # 权限码用的是独立的 `warehouse.submit` / `warehouse.verify`（与 `material.*` 同形）。
    # 它们必须由 `tools/seed_permissions.py` 种进 `permissions` 表、并授给相应角色，
    # 否则按钮对谁都不出现 —— 这与其余 60+ 条权限码是同一条纪律，不是特例。

    REGISTRY.register(
        Capability(
            name="warehouse.submit",
            title="提交仓库核验",
            domain="warehouse",
            resource="warehouse",
            loader=WarehouseQueryService.load_warehouse,
            handler=WarehouseWriteService.submit_warehouse,
            service_factory=WarehouseWriteService,
            method=HttpMethod.POST,
            path="/api/v1/warehouses/{warehouse_id}/submit",
            kind="action",
            required_permission="warehouse.submit",
            scope=_SCOPE,
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.summary(),
            description="把草稿仓库提交核验",
            invariants=(
                OptimisticLock(label="该仓库"),
                StateTransition(machine="warehouse"),
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="warehouse.verify",
            title="核验仓库",
            domain="warehouse",
            resource="warehouse",
            loader=WarehouseQueryService.load_warehouse,
            handler=WarehouseWriteService.verify_warehouse,
            service_factory=WarehouseWriteService,
            method=HttpMethod.POST,
            path="/api/v1/warehouses/{warehouse_id}/verify",
            kind="action",
            required_permission="warehouse.verify",
            scope=_SCOPE,
            # §0.4：核验后仓库才可被收发货引用 ⇒ 与 material.verify 同级，high + always。
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            description=(
                "核验已提交的仓库：通过后它才能被收发货单引用、才能被选为默认出库仓。"
                "执行前必须由当前登录用户确认"
            ),
            invariants=(
                OptimisticLock(label="该仓库"),
                StateTransition(machine="warehouse"),
                DistinctActors(),
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="warehouse.archive",
            title="停用仓库",
            domain="warehouse",
            resource="warehouse",
            loader=WarehouseQueryService.load_warehouse,
            handler=WarehouseWriteService.archive_warehouse,
            service_factory=WarehouseWriteService,
            method=HttpMethod.POST,
            path="/api/v1/warehouses/{warehouse_id}/archive",
            kind="action",
            required_permission="warehouse.archive",
            scope=_SCOPE,
            # §0.4：不可逆（本版没有取消归档的能力）+ 退出所有候选 + 锁掉引用它的
            # 收发货路径 ⇒ `high`。机械约束 `high ∧ action ⇒ always` 因此给出
            # `confirmation=always`。与 `area.archive` / `material.archive` 同一判据。
            risk=Risk.HIGH,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            description=(
                "停用（归档）一个仓库：它退出默认列表与所有下拉候选，"
                "不再能被选为收发货仓或默认出库仓；历史单据与库存账本不受影响"
            ),
            invariants=(
                OptimisticLock(label="该仓库"),
            ),
        )
    )

    # ------------------------------------------------------------ 只读三条

    REGISTRY.register(
        Capability(
            name="warehouse.list",
            title="仓库列表",
            domain="warehouse",
            resource="warehouse",
            handler=WarehouseQueryService.list_warehouses,
            service_factory=WarehouseQueryService,
            method=HttpMethod.GET,
            path="/api/v1/warehouses",
            kind="read",
            required_permission="warehouse.view",
            scope=_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词与状态筛选仓库列表，标出默认出库仓",
        )
    )

    REGISTRY.register(
        Capability(
            name="inventory.list",
            title="库存汇总",
            domain="warehouse",
            resource="inventory_lot",
            handler=WarehouseQueryService.list_inventory,
            service_factory=WarehouseQueryService,
            method=HttpMethod.GET,
            path="/api/v1/inventory",
            kind="read",
            required_permission="inventory.view",
            scope=_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按仓库/物料/批次汇总可用库存，按先到期先出排序",
        )
    )

    REGISTRY.register(
        Capability(
            name="inventory_lot.get",
            title="物料批次详情",
            domain="warehouse",
            resource="inventory_lot",
            handler=WarehouseQueryService.get_inventory_lot_by_id,
            service_factory=WarehouseQueryService,
            method=HttpMethod.GET,
            path="/api/v1/inventory/{lot_id}",
            kind="read",
            required_permission="inventory.view",
            scope=_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="读取物料批次详情，含当前可用数量",
        )
    )

    REGISTRY.register(
        Capability(
            name="warehouse_document.list",
            title="仓储单据列表",
            domain="warehouse",
            resource="warehouse_document",
            handler=WarehouseDocumentService.list_receipts_only,
            service_factory=WarehouseDocumentService,
            method=HttpMethod.GET,
            path="/api/v1/receipts",
            kind="read",
            required_permission="warehouse.view",
            scope=_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词、单据类型与状态筛选仓储单据（到货单 / 领用单）",
        )
    )

    REGISTRY.register(
        Capability(
            name="issue.list",
            title="领用出库列表",
            domain="warehouse",
            resource="issue",
            handler=WarehouseDocumentService.list_issues,
            service_factory=WarehouseDocumentService,
            method=HttpMethod.GET,
            path="/api/v1/issues",
            kind="read",
            required_permission="inventory.view",
            scope=_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词与状态筛选领用出库单",
        )
    )

    REGISTRY.register(
        Capability(
            name="issue.get",
            title="领用单详情",
            domain="warehouse",
            resource="issue",
            handler=WarehouseDocumentService.get_issue_by_id,
            service_factory=WarehouseDocumentService,
            method=HttpMethod.GET,
            path="/api/v1/issues/{issue_id}",
            kind="read",
            required_permission="inventory.view",
            scope=_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="读取领用出库单详情",
        )
    )

    REGISTRY.register(
        Capability(
            name="warehouse_document.get",
            title="仓储单据详情",
            domain="warehouse",
            resource="warehouse_document",
            handler=WarehouseDocumentService.get_document_by_id,
            service_factory=WarehouseDocumentService,
            method=HttpMethod.GET,
            path="/api/v1/receipts/{receipt_id}",
            kind="read",
            required_permission="warehouse.view",
            scope=_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="读取仓储单据详情",
        )
    )

    REGISTRY.register(
        Capability(
            name="inventory.ledger",
            title="库存流水",
            domain="warehouse",
            resource="inventory_ledger",
            handler=WarehouseQueryService.list_inventory_ledger,
            service_factory=WarehouseQueryService,
            method=HttpMethod.GET,
            path="/api/v1/inventory/ledger",
            kind="read",
            required_permission="inventory.view",
            scope=_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="库存增减流水，带逐行结存与来源单据号",
        )
    )

    # ------------------------------------------------------------ 到货两条

    REGISTRY.register(
        Capability(
            name="receipt.create",
            title="登记到货",
            domain="warehouse",
            resource="warehouse_document",
            handler=WarehouseDocumentService.create_receipt,
            service_factory=WarehouseDocumentService,
            method=HttpMethod.POST,
            path="/api/v1/receipts",
            kind="create",
            required_permission="warehouse.receipt.create",
            scope=_SCOPE,
            risk=Risk.NORMAL,
            confirmation=Confirmation.NEVER,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                ReferencedStatus(
                    field="purchase_order_id",
                    table="purchase_orders",
                    statuses=("approved", "partially_received"),
                    label="采购单",
                ),
                SameTenant(
                    fields=_SAME_TENANT_FIELDS,
                    tables=_SAME_TENANT_TABLES,
                    labels=("收货仓", "物料", "塘口", "批次"),
                ),
                _MATERIAL_VERIFIED,
                UniqueCode(fields=("code",), scope=("organization_id", "doc_type")),
            ),
            description="登记一笔到货（草稿态，不入账）；关联采购单时其状态须为已审批或部分到货",
        )
    )

    REGISTRY.register(
        Capability(
            name="receipt.verify",
            title="核验到货（入库存 + 生成应付）",
            domain="warehouse",
            resource="warehouse_document",
            handler=WarehouseDocumentService.verify_receipt,
            service_factory=WarehouseDocumentService,
            method=HttpMethod.POST,
            path="/api/v1/receipts/{receipt_id}/verify",
            kind="action",
            required_permission="warehouse.receipt.verify",
            scope=_SCOPE,
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            # ## 不变量顺序是有意义的：`CumulativeWithin` 必须排在 `ReferencedStatus` 前
            #
            # 执行器的顺序是"**服务先写、不变量后跑**"（`runner.py:250`），所以
            # `receipt.verify` 调 `purchase.apply_receipt` 把采购单推进到
            # `fully_received` **之后**，两个不变量看到的都是**写入后**的采购单状态。
            #
            # 实测（采购单 100：先到 40，再到 100，累计 140）：
            #   顺序为"ReferencedStatus 在前"时，用户拿到的错误是
            #   **「采购单当前状态为 fully_received，不允许本次操作」** ——
            #   而用户的真实问题是"到多了"。同一个拒绝，文案指向错误的原因，
            #   用户会去查采购单状态（那里没有错），而不是去改数量。
            #   把 `CumulativeWithin` 放前面，错误变成「累计到货量不能超过…」。
            #
            # 两者都拦得住（都不写入），差别只在**哪条规则说话**。
            # `ReferencedStatus` 因此仍是有效的兜底：它接住"上一张已把订单顶到
            # `fully_received`、这张又来"的情形。
            invariants=(
                # §4 #9：累计到货不得超过采购数量（本能力是它的强制点）。
                # 子表是**单头**而不是明细——`purchase_order_id` 与 `total_quantity`
                # 都放在单头，正是为了让这条规则能单表 SUM 表达（见 006 迁移的注释
                # 与 ROLLOUT_CONTRACT §2A.4）。`request_field` 指向本次到货量。
                CumulativeWithin(
                    target_table="purchase_orders",
                    target_dims=("purchase_order_id",),
                    target_columns=("id",),
                    children_table="warehouse_documents",
                    target_field="quantity",
                    # `count_field` 用 `total_quantity`（单头上的**本次到货总量**，
                    # 由服务从明细回算后写入）——它才是"这张到货单算多少"。
                    # 不能用单头的 `quantity`：那是**登记时**填的请求量，
                    # 多行单据上它只是首行/初值，不是整单量。
                    count_field="total_quantity",
                    # `request_field` 在 `new_row_counted=True` 时**不参与算术**
                    # （内核只判 `counted`），这里写它纯粹是为了让读声明的人看到
                    # "本次带来多少"这件事。真正的量由服务经 `_invariant_context`
                    # 回传的 `quantity` 提供，仅用于文案。
                    request_field="quantity",
                    statuses=("verified",),
                    # 给累计查询加行锁：并发下两张到货单同时核验时，两边都会读到
                    # "旧累计"而各自以为还没到齐。`apply_receipt` 自己已经锁了采购单行，
                    # 所以这不是当前链路正确性的唯一依赖；打开它是**纵深防御**——
                    # 若将来有路径不经 `apply_receipt` 直接核验到货，累计判定仍有锁。
                    for_update=True,
                    # `True` = **核验类**：本能力的服务把单头置成 `'verified'` 之后
                    # 不变量才跑，所以本次这一行**已经**落在
                    # `statuses=('verified',)` 的 `SUM(total_quantity)` 里。
                    # 内核据此**只判 `counted`**（不再加 `requested`）——
                    # 这正是我要的：`counted` 就是"本次生效之后的累计到货量"。
                    # 内核刻意不给默认值：`False`（登记类）与 `True`（核验类）语义相反，
                    # 猜错会让两类路径共用一种判定（t22 裁决）。
                    new_row_counted=True,
                    label="累计到货量",
                ),
                DistinctActors(),
                PeriodOpen(date_field="happened_at"),
                ReferencedStatus(
                    field="purchase_order_id",
                    table="purchase_orders",
                    statuses=("approved", "partially_received", "fully_received"),
                    label="采购单",
                ),
            ),
            description="核验到货：写入库存账本、建立物料批次，并推进采购单、生成应付",
        )
    )

    # ------------------------------------------------------------ 领用两条

    REGISTRY.register(
        Capability(
            name="issue.create",
            title="登记领用出库",
            domain="warehouse",
            # 领用单有自己的列表页与详情页（`/issues`），所以它是一条**独立资源**；
            # 与到货单共用 `warehouse_documents` 表，但 `doc_type` 区分。
            resource="issue",
            handler=WarehouseDocumentService.create_issue,
            service_factory=WarehouseDocumentService,
            method=HttpMethod.POST,
            path="/api/v1/issues",
            kind="create",
            required_permission="warehouse.issue.create",
            scope=_SCOPE,
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                SameTenant(
                    fields=_SAME_TENANT_FIELDS,
                    tables=_SAME_TENANT_TABLES,
                    labels=("出库仓", "物料", "塘口", "批次"),
                ),
                _MATERIAL_VERIFIED,
                UniqueCode(fields=("code",), scope=("organization_id", "doc_type")),
            ),
            description="登记一笔领用出库（草稿态，不扣账）；物料批次须为可用批次",
        )
    )

    REGISTRY.register(
        Capability(
            name="issue.verify",
            title="核验出库（扣库存）",
            domain="warehouse",
            resource="issue",
            handler=WarehouseDocumentService.verify_issue,
            service_factory=WarehouseDocumentService,
            method=HttpMethod.POST,
            path="/api/v1/issues/{issue_id}/verify",
            kind="action",
            required_permission="warehouse.issue.verify",
            scope=_SCOPE,
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                DistinctActors(),
                PeriodOpen(date_field="happened_at"),
                # §4 #1：**不得负库存**。这是本域最重要的强制点。
                # 注意 `lines` 之类的入参**不存在**——新版形态直接查写入后的余额，
                # "忘传增量行 = 静默通过"这个失败模式从结构上消失了。
                NoNegativeStock(
                    table="inventory_ledger",
                    columns=("quantity_delta",),
                    group_by=_STOCK_GROUP_BY,
                    label="库存",
                ),
            ),
            description="核验领用出库：从选定批次扣减库存，库存不足即拒绝",
        )
    )


_register_all()

__all__ = ["_register_all"]
