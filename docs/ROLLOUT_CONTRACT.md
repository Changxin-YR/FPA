# 五域铺开契约（ROLLOUT CONTRACT）

> 本文件是 `production / warehouse / purchase / sales / cost` 五个域并行开发期间的**唯一事实源**。
> 与 `docs/CAPABILITY_REGISTRY.md`（业务规格）和 `docs/DEVELOPMENT.md`（固定七步）配套使用。
>
> **纪律**：本文件里写过的东西，不要在别处再写一遍，也不要凭记忆自己定一套。
> 如果发现本文件与代码不一致，**报给 负责人**，不要各自解释——那正是本项目要根除的
> "两处描述同一件事"。

---

## 1. 迁移编号（已冻结，禁止自行取号）

| 编号 | 文件 | 归属 |
|---|---|---|
| 000–003 | 已应用 | 内核 / 身份 / 会话 / master_data |
| 004 | `004_cost.sql` | cost（**含 `accounting_periods`**，已落盘） |
| **005** | `005_production.sql` | production |
| **006** | `006_warehouse.sql` | warehouse |
| **007** | `007_purchase.sql` | purchase |
| **008** | `008_sales.sql` | sales |

**取号前先跑 `python tools/migrate.py status` 复核**（防漂移）。若你的域还需要第二个迁移文件，
用 `00N_<域>_<用途>.sql`，并向 负责人 报备，**不要占用上表之外的号**。

### 1.1 禁止跨域外键（硬规则）
**任何迁移都不得建立指向另一个域的表的外键。** 引用别的域时只存 `BIGINT`，
约束由**不变量**负责（`ReferencedStatus` / `CumulativeWithin` 等），不由数据库负责。

理由有两条，都踩过：
1. 跨域 FK 让迁移变成**全序**依赖，五个域无法并行；一旦顺序错了，apply 直接失败。
2. 早期版本用数据库约束表达业务规则，结果"改业务规则要改表结构"（早期版本为状态枚举做了 8 次
   `ALTER TABLE ... MODIFY status ENUM(...)`）。

**指向同一批基础表的 FK 是允许的**：`organizations` / `users`（001 已建）、本域自己的表。
即：跨域禁止，跨层（业务表 → 身份表）允许。

### 1.2 其它 DDL 规矩
- 建表一律 `CREATE TABLE IF NOT EXISTS`（迁移必须**可重入**）
- 状态列一律 `VARCHAR(32)`，**不用 ENUM**（registry §3.2）
- 能表达成唯一键 / CHECK 的不变量就放数据库层（唯一键是正确性底线）
- 迁移必须**自包含**：不得假设前置数据存在

---

## 2. 跨域调用的唯一入口（已冻结）

**规则：任何域都不得直接读写另一个域的表。** 跨域效果只能经对方**具名的进程内函数**，
在**同一个事务**（同一个 `tx`）内完成。

**例外（明确允许）**：内核不变量按表名读取（`NoNegativeStock` / `ReferencedStatus` /
`CumulativeWithin` 等）——它们本来就是表名参数化的内核件，不在此限。

### 2.0 一般化规则：跨域取数只能经**所有者**的具名只读函数（含批量形态）

> **当一个域需要另一个域的表的数据时，由「表的所有者」提供具名只读函数；
> 调用方需要 N 行时，所有者提供批量形态（`lookup_*(ids)`）。
> 调用方不得直读，也不得要求「登记为 §2 的例外」。**

这条规则把"谁来写这段 SQL"钉在**拥有该表并已为它写测试**的域里，因此：

- **schema 变更只在一处爆**：表所有者的列名/状态变了，坏在他自己的测试上，
  而不是在某天某个调用方的列表页里**静默少一列或渲染错**，而两侧单域 e2e 全绿。
- **列表渲染是最需要它的场景**，不是例外。列表页是 schema 耦合**最容易静默积累**的位置
  （production 改一个列名，sales 的 `LEFT JOIN` 照旧能跑、只是渲染错），所以"为了省一次查询"
  绝不能成为登记例外的理由。
- **例外必须稀有**：本节的例外只有一条（内核不变量按表名读取），它能成立是因为那些是
  **表名参数化的通用件**、不含业务语义。领域特定的 `LEFT JOIN <别人的表>` 不属于这一类——
  **一个按需求生长的例外类，最终会变成没有规则。**

**已落地的具名只读入口（production 域提供，实测于 `tools/production_e2e.py` 第 2c 段）**：

| 入口 | 形态 | 说明 |
|---|---|---|
| `production.service.lookup_batch(tx, *, batch_id)` | 单行 | 不存在返回 `None` |
| `production.service.lookup_batches(tx, *, batch_ids)` | **批量**（列表渲染用） | 返回 `dict[id, row]`，**不存在的 id 被略去**；空入参不发查询 |
| `production.service.lookup_harvest(tx, *, harvest_id)` | 单行 | 不存在返回 `None`（sales 的交付校验用） |

**两条设计取舍（负责人 已确认，理由是通用的，后续入口照此办）**：

1. **返回 `None` / 略去缺失 id，而不是抛错**——**"所有者提供事实、调用方决定语义"**。
   调用方要保留"自己给可读字段错误"的能力（cost 对 `batch` 目标就是 `FIELD_INVALID`），
   并且必须**避免依赖外键或异常消息判断约束**（本项目明令禁止后者）。
2. **不要求调用方传 `Scope`**——内核自己的跨域不变量（`SameTenant` / `ReferencedStatus`）
   本来就按表名跨域读行；若要求传 `Scope`，这个入口会**在最该用的地方（内核判定）用不了**，
   而它又提供不了调用方尚未具备的保护。**参数只应表达"调用方需要什么"，
   不应顺手要求它交出一个它没有的东西。**

| 入口 | 归属域 | 调用方 | 用途 |
|---|---|---|---|
| `warehouse.ledger.apply_movement(...)` | warehouse | warehouse 自身、production、sales | **全系统唯一**能改库存的函数 |
| `purchase.purchase_orders.apply_receipt(...)` | purchase | warehouse（`receipt.verify`） | 推进采购单到 partially/fully_received |
| `purchase.payables.create_from_receipt(...)` | purchase | warehouse（`receipt.verify`） | 生成应付 |
| `cost.entries.record_fact(...)` | cost | production、warehouse、sales | **唯一**成本归集入口 |
| `sales.receivables.create_from_delivery(...)` | sales | sales 自身 | 生成应收 |

**`record_fact` 定稿签名**（cost 域；实测 `from yuxin.domains.cost.entries import record_fact` 可导入）：

```python
record_fact(
    tx, *,
    organization_id: int, farm_id: int, area_id: int,   # 分租键，由**调用方**从其业务对象解析
    category_code: str,                                  # 用 LEDGER_FEED / LEDGER_SEED / LEDGER_HEALTH
    amount: Decimal | int | str,                         # **拒绝 float**（金额精度在源头暴露）
    occurred_on: date | str,
    source_ref: str,                                     # 必须稳定标识那笔业务事实（重放传同一字面值）
    target_type: str,                                    # pond | batch | area | farm
    target_id: int,
    actor_id: int,                                       # = ctx.actor.user_id，**无默认值**
    note: str = "",
    source_type: str = "warehouse_ledger",               # 勿改
    period_start: date | str | None = None,              # 默认按 occurred_on 所在自然月
    period_end: date | str | None = None,
) -> CostFact
```

返回 `CostFact`（frozen dataclass）：`entry_id` / `organization_id` / `farm_id` / `area_id` /
`period` / `period_start` / `period_end` / `category_code` / `amount` / `source_ref` /
`target_type` / `target_id` / `confirm_state` / `status`，另有 `describe()` 供调用方渲染 `message`。
配套常量：`LEDGER_FEED="feed"`、`LEDGER_SEED="seed"`、`LEDGER_HEALTH="health"`、
`MATERIAL_CATEGORY_TO_COST`、`LEDGER_CATEGORY_CODES`（均在 `__all__`）。

调用方必须知道的四条（前两条是"唯一入口"能成立的前提）：

1. **`actor_id` 必须传调用方自己的 `ctx.actor.user_id`。** 它写进 `created_by`，而那是
   §4 #5「经办人 ≠ 审批人」比较的列——传占位值会让双人复核在自动归集路径上**直接失效**，
   所以刻意没有默认值（有默认值就等于允许忘记）。
2. **分租键必须由调用方提供，且调用方必须能从「自己的业务对象」上拿到它们。**
   `record_fact` **不回查任何别人的表**（正是本节 §2 的要求）。

   这条之所以可满足，是因为 Q6 裁决已要求所有业务表补齐分租列。**已核实自带
   `organization_id` / `farm_id` / `area_id` 的表**（调用方直接从手上那一行取值即可，
   无需额外查询）：`feedings` / `production_batches`（production 域）、
   `inventory_ledger` / `inventory_lots` / `warehouses`（warehouse 域）、
   `cost_entries`（cost 域自身）。

   **调用方不得只传 `batch_id` / `pond_id` 就期望 cost 去回查** —— 那会迫使 cost
   直读别人的表（违反 §2）。若某个调用方手上确实没有这三列，那是**它的表缺分租列**
   （Q6 的施工缺陷），应当在它自己那里补，而不是让 cost 替它绕路。
3. 结果 `confirm_state='pending'` / `status='draft'`：**自动归集 ≠ 已确认入账**，
   仍需走 `cost.entry.confirm` 的双人复核。
4. `PERIOD_CLOSED` / `COST_SOURCE_DUPLICATED` 是**拒绝，不是告警**：调用方应让整个业务
   操作回滚，而不是跳过成本归集——否则会出现"库存扣了但成本没记"，账目永久失衡。

**★ `source_type=warehouse_ledger` 的 `amount` 金额口径（评审结论 2026-09-12）**

口径：**`amount` = 消耗量 × 该批次（`inventory_lot_id`）的加权平均入库成本**：

```sql
-- 加权平均入库成本，只用账本里已有的事实
SELECT SUM(quantity_delta * unit_cost) / SUM(quantity_delta)
  FROM inventory_ledger
 WHERE inventory_lot_id = %s
   AND source_type = 'receipt' AND unit_cost IS NOT NULL AND quantity_delta > 0
```

**为什么需要这条口径**：**出库账本行不带成本** —— `inventory_lots` 没有单价列，
而 `apply_movement` 只在调用方传入 `unit_cost` 时写该列（文档把单价描述为**入库**关注点）。
所以"这次领用的物料值多少钱"在账本里**没有直接答案**，必须由调用方按某个口径算出来再传给 `record_fact`。

**为什么选批次加权平均**：只用账本已有事实、确定性、与移动平均法一致；且 FEFO 每次
只选**单一**批次（`resolve_issue_lot` 只返回一个 `IssueLot`），不存在需要分层计价的拆批。
上面那条查询的过滤条件**恒排除出库行**（`unit_cost IS NULL` 或 `quantity_delta < 0`），
因此结果**与"出库行是否已写入"无关**，不存在"写前写后读到两个值"的时序问题（已实测核对）。

**归属权与变更规则**：本口径是**成本核算规则**，归属方是 **cost 域**（`record_fact` 的所有者）。
生产侧（`feeding.verify`）只是实现它。若 cost 认为应按 **FEFO 所在批次的入库单价**而非批次均价，
**那是 cost 的裁决权**：由 cost 明确后，生产侧一处改动即可（两种口径都实现得出）。
**t9 按本口径核对**；在 cost 未改判前，本口径即为验收口径，不得记为"未定义行为"。

> 记账理由（负责人 原话意）：**这条规则不该只活在调用方的代码注释里**。让它住在
> 不拥有它的域里，就是"两处描述同一件事"的温床 —— 下一个调用方会自己实现一套。

**初版实现时的形态值得记一笔**：生产侧最初是读回**刚写入的出库行**取单价，得到 NULL
并**响亮失败**（而不是静默记 0 元成本）——正是这次失败暴露了"出库行不带成本"这个缺口。
"响亮失败"是这条口径能被讨论的前提。

实现与实测：`backend/yuxin/domains/cost/entries.py`（`record_fact` / `CostFact`）与
`tools/cost_e2e.py` §12（**直调不经执行器** = 调用方形态，11 项断言）、§12b（`target_type=batch`）。

**实现方式**：跨域 import 一律写在**函数体内**（不要模块级 import），避免环形导入；
`warehouse ↔ purchase` 之间存在双向调用，模块级 import 会直接炸。

**最终签名由各域在完成输出里贴出并由 负责人 记入本表**——签名未定稿前，调用方按本表的名字预留调用点，
**不要自己实现一份对方的逻辑**。

---

## 2A. 权威表名 / 列名表（t16 交付，跨域读写的唯一口径）

> **为什么需要这一节**：`docs/CAPABILITY_REGISTRY.md` **从不命名**跨域读取的明细表——
> 权威 `uq_*` 里只有 `uq_warehouse_documents_org_type_code`，而"明细表叫什么、
> 关联列叫什么、数量列叫什么"全都没有出现过。五个域各自猜一个的结果是
> **同一段 SQL 的两份副本**：两边的单域 e2e 都能过，只在集成时炸。
> 本项目已经出现过三次这种失败形态，所以这张表是**强制口径**，不是参考资料。

### 2A.1 表名命名规律（**先读这条，它决定了所有表名**）

1. 本仓唯一键一律 `uq_<表名>_<列...>`。003 实测：`uq_ponds_farm_code` /
   `uq_materials_org_code` / `uq_business_partners_org_type_code`。
2. **凡是 registry 正文写短名、而 §4 的 `uq_*` 透露真实表名的，以 `uq_*` 为准。**
   已据此裁定：`batches` → **`production_batches`**（`uq_production_batches_org_code`）。
   同理 `payables` → `purchase_payables`，`payments` → `purchase_payments`。
3. 单头 + 明细：单头 `warehouse_documents`（`doc_type` 区分 receipts / issues），
   明细 `warehouse_document_lines`。
4. 状态列一律 `VARCHAR(32) + CHECK`（registry §3.2 规则 2），**不用 ENUM**。

### 2A.1b 每个资源的**三处表名必须字面相同**（purchase-dev 提议，已采纳）

对每一个资源，"这张表叫什么"有三个落点，**必须字面一致**：

| 落点 | 在哪 | 谁读它 |
|---|---|---|
| 迁移建的表名 | `database/migrations/*.sql` | 数据库 |
| `Resource(table="...")` | 各域 `service.py` | 执行器注入 `_resource_table`，`OptimisticLock` / `StateTransition` / `UniqueCode` 靠它 |
| 不变量里的 `table=` / `children_table=` | 各域 `capabilities.py` | 内核查表 |

**为什么要单列一节：`Resource(table=)` 省略时有个兜底公式 `<module>_<name>s`，而它在本仓实测 16/16 全部算错。**

| 资源 | 显式 `table=` | 兜底会算成 |
|---|---|---|
| `warehouse` | `warehouses` | `warehouse_warehouses` |
| `purchase_payable` | `purchase_payables` | `purchase_purchase_payables` |
| `batch` | `production_batches` | `production_batchs` |
| `cost_entry` | `cost_entries` | `cost_cost_entrys` |
| `partner` | `business_partners` | `master_data_partners` |
| …（其余 11 个同样不一致） | | |

所以本仓的纪律是：**`table=` 一律显式声明，不依赖兜底**（内核的 `Resource.__post_init__` 在 `table=""` 时构造期直接抛错，兜底并不存在——这里记下来是为了让下一个人知道"为什么每个资源都写了这一行"）。

**由此得出一条反直觉的结论**：资源名与兜底值**不一致是好事**。若为了让兜底"算对"而改名（例如把资源名改成能凑出 `purchase_payables` 的写法），就**抹掉了漏写 `table=` 的信号**。资源名应当为**语义**服务（`purchase_payable` 表示"采购域的应付"），表名靠显式声明收敛。

### 2A.2 跨域可读的表与列

「归属域」= 谁拥有写入权；「被谁读」= 谁会在自己的 SQL / 不变量里读它。
**读可以，写不行**——写入只能经 §2 那张表的具名函数。

| 表 | 归属域 | 被谁读 | 跨域要用到的列 |
|---|---|---|---|
| `warehouses` | warehouse | warehouse | `id` / `organization_id` / `farm_id` / `area_id` / `code` / `name` / `is_default` / `status` |
| `warehouse_documents` | warehouse | purchase（对账） | `id` / `doc_type` / `code` / `warehouse_id` / `material_id` / `quantity` / `unit_cost` / **`total_quantity`** / `total_amount` / `supplier_id` / **`purchase_order_id`** / `pond_id` / `batch_id` / `happened_at` / `status` |
| `warehouse_document_lines` | warehouse | warehouse | `id` / `document_id` / `line_no` / `material_id` / `lot_no` / `quantity` / `unit_cost` / `expiry_date` |
| `inventory_lots` | warehouse | warehouse / production | `id` / `warehouse_id` / `material_id` / `lot_no` / `expiry_date` / `status` |
| `inventory_ledger` | warehouse | **cost**（成本归集） | `id` / `warehouse_id` / `material_id` / `inventory_lot_id` / `lot_no` / `source_type` / `source_ref` / `source_line_no` / `quantity_delta` / `unit_cost` / `amount` / `pond_id` / `batch_id` / `happened_at` |
| `production_batches` | production | warehouse? 否 / sales / cost | `id` / `pond_id` / `code` / `status` |
| `batch_stock_records` | production | cost | `id` / `batch_id` / `pond_id` / `quantity_delta` / `weight_delta_kg` / `source_type` / `source_id` |
| `feedings` | production | cost | `id` / `code` / `pond_id` / `batch_id` / `material_id` / `quantity` / `status` |
| `harvests` | production | **sales**（交付必须与出塘一致，#6） | `id` / `code` / `pond_id` / `batch_id` / `quantity` / `status` |
| `purchase_orders` | purchase | warehouse（#3 / #9） | `id` / `code` / `material_id` / `warehouse_id` / `quantity` / `status` |
| `purchase_payables` | purchase | purchase | `id` / `purchase_order_id` / **`receipt_id`** / `total_amount` / `status` |
| `purchase_payments` | purchase | purchase | `id` / `payable_id` / `amount` / `status` |
| `sales_orders` | sales | sales | `id` / `code` / `customer_id` / `pond_id` / `batch_id` / `quantity` / `status` |
| `deliveries` | sales | sales | `id` / `sales_order_id` / **`harvest_document_id`** / `batch_id` / `pond_id` / `quantity` / `status` |
| `receivables` | sales | sales | `id` / `sales_order_id` / `delivery_id` / `customer_id` / `total_amount` / `status` |
| `sales_receipts` | sales | sales | `id` / `receivable_id` / `amount` / `status` |
| `cost_entries` | cost | cost / warehouse | `id` / `target_type` / `target_id` / `amount` / `source_type` / `source_ref` / `occurred_on` / `status` |
| `accounting_periods` | cost | **全部 6 条挂 `PeriodOpen` 的能力**（#7） | `id` / `period_start` / `period_end` / `status`（`open` / `closed`） |

**分租列是统一的**：所有业务表都带 `organization_id` / `farm_id` / `area_id` 三列
（Q6 裁决），所以每条能力的 `scope=resource(area_id)` 都能**直接命中本表列走索引**。
这条也是"不许跨表 JOIN 解析数据范围"的落地形态。

### 2A.2b purchase 侧三张表的权威值（负责人 已裁决，2026 定稿）

"这张表叫什么"的三个落点**字面相同**（依据见 §2A.1b）：

| 资源 | 迁移建的表名 | `Resource(table=)` | 不变量里 `table=` |
|---|---|---|---|
| 采购单 | `purchase_orders` | `purchase_orders` | `purchase_orders` |
| 应付 | **`purchase_payables`** | **`purchase_payables`** | **`purchase_payables`** |
| 付款 | **`purchase_payments`** | **`purchase_payments`** | **`purchase_payments`** |

**裁决要点**：registry 正文里的裸名 `payables` / `payments` 是**唯一一族没有域前缀的裸名**，
与全仓规律（`purchase_orders` / `production_batches` / `warehouse_documents`）分叉，故以带前缀
的全名为准。**但 `table=` 仍必须显式声明、不依赖兜底**——兜底公式在本仓 16/16 全错
（§2A.1b 的实测表），它在 `table=""` 时直接构造期报错。

### 2A.2c 跨域共用的**字面值**（两个域读同一个值，必须单点）

下面这些不是表名，而是**两个域都写在 SQL / 声明里的字面量**。它们属于"同一个字面值被两个域
使用"，同样必须单点——否则一边写 `'receipt'` 一边写 `'receipts'` 会**静默查不到行**。

| 字面值 | 列 | 谁写 | 谁读 | 权威值 |
|---|---|---|---|---|
| 到货单据类型 | `warehouse_documents.doc_type` | warehouse（`receipt.create`） | purchase（`apply_receipt` 的累计查询）、内核 `CumulativeWithin` | **`'receipt'`** |
| 出库单据类型 | `warehouse_documents.doc_type` | warehouse（`issue.create`） | warehouse | **`'issue'`** |
| 已核验（算数状态） | `warehouse_documents.status` | warehouse（`receipt.verify` / `issue.verify` 置为） | purchase（累计查询）、内核 `CumulativeWithin(statuses=…)` | **`'verified'`** |
| 可收货的采购单状态 | `purchase_orders.status` | purchase | warehouse（`ReferencedStatus`，声明在 `receipt.create` / `receipt.verify` 上） | **`'approved'` / `'partially_received'`** |

**为什么 `doc_type='receipt'` 绝不能漏**：`warehouse_documents` **同时**承载到货单与领用单
（用 `doc_type` 区分）。漏掉这个过滤条件，领用单会被算成到货——**累计量凭空变大**，
而且因为两张表结构相同，**类型检查与静态审查都发现不了**。

### 2A.3 到货明细的三个具体问题（purchase-dev 提，t16 裁决）

**① 明细表名 = `warehouse_document_lines`。**

**② 关联列 = `document_id` + `line_no`，不是 `receipt_id`。**
没有 `receipt_id` 这个列，**也不该有**：单头 `warehouse_documents` 用 `doc_type`
（`receipt` / `issue`）区分两类单据，明细只指回单头主键。若明细上再加一个
`receipt_id`，就等于"同一件事有两个列名"，而 receipts 与 issues 的明细结构完全相同
——两列会立刻漂移。
从明细取"这张到货单"的写法：

```sql
SELECT l.* FROM warehouse_document_lines AS l
 JOIN warehouse_documents AS d ON d.id = l.document_id
WHERE d.id = %s AND d.doc_type = 'receipt'
```

**③ 数量列 = `quantity`**（明细的本次到货量）；单头上另有一个 `total_quantity`，
两者含义不同，见下。

### 2A.4 ★ 指向采购单的列放在**单头**，累计量因此能单表 SUM（本节的结论）

`CumulativeWithin`（#9 累计到货不得超过采购数量）**只接受一个 `children_table`**，
不能 JOIN。若 `purchase_order_id` 放在明细上，"某采购单累计到货量"就必须 JOIN
单头——而这个不变量表达不了。所以：

> **`warehouse_documents.purchase_order_id`（单头） + `warehouse_documents.total_quantity`
> 是刻意的去规范化，为的就是让 #9 保持单表可表达：**

```sql
SELECT COALESCE(SUM(total_quantity), 0)
  FROM warehouse_documents
 WHERE organization_id = %s AND purchase_order_id = %s
   AND doc_type = 'receipt' AND status = 'verified'
```

**⚠️ 累计量取 `total_quantity`，不要取 `quantity`**（这一条与内核 `CumulativeWithin` 的
示例不同，落到本仓必须换）：

| 列 | 由谁写、何时 | 含义 | 能否做累计 |
|---|---|---|---|
| `warehouse_documents.quantity` | 客户端在 `receipt.create` 填 | **登记时的请求量**；多行单据上只代表首行/初值 | **不能**（多行会少算） |
| `warehouse_documents.total_quantity` | 服务端在 `receipt.verify` 从明细回算 | **已核验的整单量** | **能**（唯一正解） |

单行单据（P0 形态）两者相等，所以用错**测不出来**——只有多行单据才会暴露。
内核 `CumulativeWithin` 的类文档示例写的是 `count_field="quantity"`，那是**通用示例**；
本仓的到货单是多行结构，声明处必须写 `count_field="total_quantity"` 并配
`new_row_counted=True`（核验类：服务已把单头置为 `verified`，本次行已在 `counted` 里）。
实测：采购单 100、先核验 40、再核验 100 → `counted=140` → 正确拒绝。

**取舍与代价（写下来，否则将来没人知道为什么这么放）**：
- 单头放 `purchase_order_id` 等于约定"**一张到货单只对一张采购单**"。P0 的真实
  形态就是如此（旧 `purchase_posting.py` 即按单条明细计算）。
- 若将来需要"一张到货单同时对多张采购单"，这个去规范化表达不了：要么把
  `purchase_order_id` 下沉到明细并改用 JOIN 聚合（那时 #9 必须换实现形态），
  要么维持"一到货单只对一张采购单"的约定。**改之前先知会 负责人**。
- `total_quantity` / `total_amount` 在核验时由服务端从明细回算写入，
  **不接受客户端提交**；两张单据的语义差异（到货必须有供应商、领用没有）由
  `chk_warehouse_documents_supplier` 在数据库层保证。

同理，`pond_id` / `batch_id`（领用去向）也在单头：一次领用只有一个去向。

### 2A.5 两套 `source_*` 命名**不一样**，别照抄

容易混，所以单列一节——它们属于**不同域**、**不同模型**：

| 归属 | 列 | 语义 |
|---|---|---|
| `inventory_ledger`（warehouse） | `source_type` + **`source_ref`** + `source_line_no` | `source_ref` 是**单据号**，形如 `feeding:FEED-001`；与行号组成唯一键 `uq_inventory_ledger_source_line` |
| `batch_stock_records`（production） | `source_type` + **`source_id`** | `source_id` 是**主键** |

**库存账本为什么必须用单据号而不是主键**：不同单据表的主键各自从 1 开始，
`issue#5` 与 `feeding#5` 会拼成同一个 ref，唯一键就会把两个正常出库里的一个
判成"重复入账"。加来源域前缀后两个 id 空间不可能相遇。
拼 ref 请一律调用 `warehouse.ledger.ledger_source_ref("<域>", "<单据号>")`，
**不要自己 f-string**（它带冒号校验，防 `Warehouse:` / `warehouse:` 这种
"看着一样、唯一键认为不同"的静默重复入账）。

### 2A.6 其它已定稿的跨域列名

- 应付指回单头主键：`purchase_payables.receipt_id` → `warehouse_documents.id`
  （**仅 `doc_type='receipt'` 的行**）。
- 交付比对出塘：`deliveries.harvest_document_id` → `harvests.id`（#6）。
- 关账期间：`accounting_periods` 的 `period_start` / `period_end` / `status`，
  按**日期区间**反查（#7 / `PeriodOpen`），不是按月份字符串。
- 领用去向：`warehouse_documents.pond_id` / `batch_id`（单头）；
  `inventory_ledger.pond_id` / `batch_id` 由 `apply_movement` 从单据带过来。

---

## 3. 写能力必须声明回读函数（模板债，正在修）

`kernel/runner.py::_reload_after` 要求：**任何返回 `resource_id` 的写能力，其 handler 必须可解析出
`__yuxin_load_by_id__(tx, *, scope, record_id)`**，否则抛 `INTERNAL_ERROR`。

实测缺口（**已由 t13 修完**）：`domains/master_data/ponds_write.py` 的能力**没有**设置它，只有
`tools/{runner,web,agent}_e2e.py` 在夹具里手动补挂 —— 也就是**生产路径下 master_data 的写能力全都会 500**。
这是与"组合根缺失"同一形态的缺陷（夹具盖住了生产路径）。

**现在的挂载入口是内核的 `Capability.loader=`**（`capability.__post_init__` 会
`setattr(handler, "__yuxin_load_by_id__", loader)`）。域**不要**自己写那个属性名字符串：
让五个域各写一遍，等于把这个"内核与执行器之间的内部约定"复制五份（内核那一份已经因为
一次笔误 `__yuxin_read_by_id__` 让幂等回放静默退化过）。

* 写能力：`loader=<回读函数>`（且回读函数签名必须是 `(tx, *, scope, record_id)`）；
* 读能力：**不要**声明 loader（`tests/test_architecture.py::test_loader_is_only_declared_where_it_can_be_used` 强制）。

**对你的域的要求**：
- 每个写能力的服务方法上显式挂 `__yuxin_load_by_id__`；
- 你的 `tools/<域>_e2e.py` 必须**断言"不靠夹具补挂也能通过"**——否则你会把同一个缺陷复制一份。

---

## 4. `NoNegativeStock` 的契约变更（评审结论，覆盖旧形态）

**旧形态（作废）**：`quantity_column`（单列）+ 从 `payload["_ledger_lines"]` 取增量行
+ `if not lines: return`（**没传增量行 = 校验通过**）。

作废理由有两条：
1. **静默失效**：旧形态把 `_ledger_lines` 当**算术输入**，只能由服务经
   `HandlerResult.data["_invariant_context"]` 回传，忘传即静默通过。这与 DEVELOPMENT §4 记录的
   `fields=` 事故是同一个失败形态——而 DEVELOPMENT 的教训原文就是"修法不是'记得传'，
   而是**删掉那个可选性**"。新形态的做法是：**把它从"算术输入"降级为"定位输入"，
   并把缺失从"放行"改成"报错"**（见下）。
2. **算错**：不变量在 `_call_service()` **之后**执行（`runner.py:245-257` 与 `:351-356`），
   此时 `SUM()` 已包含本次写入，再 `+ delta` 等于 `available_before + 2×delta`，对负增量会**误拒合法操作**。

**新形态（照此声明）**：直接查**写入后**的账本余额，`SUM(column) < 0` 即违规。
- 参数：`table` / `columns`（**元组，支持多列**，如 `("quantity_delta","weight_delta_kg")`）/ `group_by`
  / `label`（可选）
- **不参与算术**：不再从任何调用方提供的增量行做加减——"忘传增量行 = 校验通过"
  这个失败模式因此从结构上消失。
- **但仍必须回传账本行，它只承担"定位"职责**：
  ```python
  return cap.HandlerResult(
      data={"row": ..., "_invariant_context": {
          "_ledger_lines": [{"warehouse_id": 1, "material_id": 2, "inventory_lot_id": 3}],
      }},
      resource_id=...,
  )
  ```
  **为什么不是"什么都不用传"**：不变量在**写入之后**执行，它拿不到"本次改了哪些
  `(warehouse_id, material_id, inventory_lot_id)` 分组"这个事实，而 `payload` 里也没有
  （账本的列名不是请求字段）。没有分组就无法只查被改动的那几个组合 —— 要么全表扫，
  要么少查。所以账本行**只用来回答"查哪几组"**，行的数值不参与任何比较。
- 缺 `_ledger_lines` → **`INTERNAL_ERROR`（不是放行）**：它意味着"能力声明了规则、
  但处理器没回传上下文"，属于**基础设施缺失**（判据见 t20 的三分法：
  业务事实缺失→跳过；基础设施缺失→报错）。

**执行顺序的事实**（文档已按此更正）：不变量在**同一事务内、提交之前**执行，
但看到的是**写入之后**的状态；失败仍整体回滚，不产生部分写入。

### 4.1 其它需注意的内核件
- `PeriodOpen(table=..., date_field=..., status="open")` —— `date_field` 可覆盖（warehouse 用 `happened_at`）
- `UniqueCode(fields=(...), scope=(...))` —— `scope` 支持多列，
  `warehouse_documents` 用 `scope=("organization_id","doc_type")`
- `StateTransition(machine=...)` —— 非列表资源（如 `inventory_lot`）可直接传 `Workflow` 实例，
  不要求它有 `Resource` 注册

---

## 5. 本机环境约定

```powershell
# 统一使用默认库 yuxin（**不要**给每个域新建库）
$env:MYSQL_USER='yuxin'; $env:MYSQL_PASSWORD='yuxin_dev_password'
$env:MYSQL_ROOT_PASSWORD='1234'      # 只有 bootstrap/migrate reset 需要 root
python tools\bootstrap_db.py
python tools\migrate.py apply
```

- **不要一域一库**（本行原先写作「每个域一个以旧前缀命名的库」，已被负责人推翻）：
  一域一库的代价高于并行隔离的收益 —— 它诱发过 `bootstrap_db.py` 的 `REVOKE ALL` 事故
  （新库建账号时把 `yuxin` 的授权一并收回），而且让集成验收（t9 的"69 条能力 ×
  21 条不变量"）无法在同一个库里核对。
- **并发 e2e 靠探针数据 + 清理隔离，不靠分库**：每个 e2e 用自己前缀的探针行，结束时删干净；
  探针不干净就是缺陷，不是"换个库躲开"。
- **`migrate reset` 只由 负责人 执行**（它会清库，且只允许在开发环境运行）。

- **写文件用 Python 脚本 + `Path.write_text(content, encoding='utf-8', newline='\n')`**。
  不要用 PowerShell 的 `Set-Content` / here-string：会写 BOM（MySQL 报
  `syntax error near '\ufeff'`）或静默失败。见 DEVELOPMENT §5.1。
- 遍历目录用 `os.walk` + `dirnames` 剪枝，**不要 `Path.rglob`**（会进 node_modules）。
- 提交前跑 §5.1 的三道门禁（**组合根 → hygiene → pytest**，按优先级）。
- **域代码不得 import pymysql**（由 `tests/test_architecture.py` 强制，已抓到一次真实违规）。
  一切数据库访问经 `UnitOfWork` 的 `query_one/query_all/execute`。
- `tests/` 是新增的架构与内核测试：`python -m pytest tests -q` 必须全绿。

### 5.1 提交前清单（按优先级；以下是三个人今天踩出来的）

**① 组合根必须可装载**（最高优先级，先跑这一条）：

```powershell
python -c "import sys;sys.path.insert(0,'backend');import yuxin.bootstrap as b;b.load_all();print('OK')"
```

**为什么它排在 hygiene 之前**：`hygiene` 只判编码与换行（BOM / CRLF / 末行），
而组合根一坏，**所有域的 e2e、探针、pytest 同时失去意义**——那不是"测试失败"，
而是"测试的结论不可信"。杀伤面差一个量级，所以优先级也应当不同。

**今天已实测 3 次**（每次都是**一个域的半成品**让全队验证失效）：

| # | 位置 | 症状 |
|---|---|---|
| 1 | `cost` 域 | 字符串内层用了与外层同种引号 → `SyntaxError` |
| 2 | `production/write.py` | `try` / 缩进未闭合 → 连续三处 `IndentationError`、`SyntaxError` |
| 3 | `production/service.py` | `Workflow` 转移引用了未声明的状态 → `ValueError`（**内核是对的**：构造期就吵） |

注意第 3 次的形态：**不是内核太严，而是没有门禁**。内核在构造期报错是正确的设计
（"声明错了就当场吵，不要留到运行期"），缺的是"别让它进主干"的那一步。

**配套约定（写给所有 `tools/*_e2e.py` 的作者）**：组合根不可用时，e2e **必须**
① 退到隔离模式、继续跑本域能跑的断言；② 把"组合根可用"这一条**判 FAIL** 并打印原因。
**降级必须可见**——这样"根坏了"与"我的域坏了"才是两件能分开的事。
参考实现：`tools/live_agent_e2e.py` 的 §0、`tools/master_data_e2e.py` 的第 1 节。

> 测试侧的等价冒烟断言（`tests/` 里那条 `load_all()`，含 `_imported()` 的 `SyntaxError`
> 处理）**已授权给 cost-dev 实现**。本节只定义**流程要求**，不在测试里重复实现同一件事。

**② `python tools\check_source_hygiene.py`**：BOM / CRLF / 合法 UTF-8 / 末行换行。

**③ `python -m pytest tests -q` 必须全绿**（含架构约束与内核测试）。


---

## 6. 未决事项（不阻塞你开工，但别自己拍板）

- registry §4 #5「经办人≠审批人」清单是 11 条，文档自己标注 `harvest.verify` 与
  `pond_status_change.verify` 应纳入 → 13 条。以 评审结论为准。
- 附件能力缺失，导致 #15 从"必须有凭据"降级为"必须有来源单号"（Q5）。
  **这是已知降级，不得在交付物里写成已实现。**
