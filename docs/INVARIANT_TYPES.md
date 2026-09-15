# 内核不变量类型：契约与用法

> 面向**声明能力的人**（域工程师）。内核实现见 `backend/fpa/kernel/invariants.py`，
> 单测见 `tests/test_invariants.py`（79 条，每条规则都有"通过 / 拒绝"两条路径）。
>
> 本文件只描述"怎么把规则挂到能力上、声明什么参数、要注意哪些列名"。
> 规则编号（#1–#21）对应 `docs/CAPABILITY_REGISTRY.md` §4。

---

## 0. 一句话

> 参数值**依表而异**的类型（`AmountWithin` / `RequiredWhen` / `DistinctActors`）请先看 **§2.11**：
> 那里用"入参名 / 表列名"两栏写清差异，避免照抄示例时踩"列不存在"或"规则永远放行"。


不变量是**声明出来的规则对象**，由执行器在**同一个事务内、写库之后、回读之前**统一执行：

```python
Capability(
    name="receipt.verify",
    ...
    invariants=(
        NoNegativeStock(table="inventory_ledger", columns=("quantity_delta",),
                        group_by=("warehouse_id", "material_id", "inventory_lot_id")),
        PeriodOpen(table="accounting_periods", date_field="occurred_on", status="open"),
        DistinctActors(),
        OptimisticLock(),
        StateTransition(machine="*"),
    ),
)
```

人工页面与 Agent 走的是**同一份判断**：执行器 `CapabilityRunner.invoke()` 只有一条
路径，不变量挂在那里 —— 不是"两套实现碰巧一致"，而是根本只有一套。

写库前还是写库后：`run_invariants()` 在**服务执行之后、回读之前**调用
（`kernel/runner.py`）。所以 `before` 是"执行前的行快照"、`payload` 是本次请求，
而**任何"累计类"规则都要自己把本次数量算进去**（见 §2.7）。

---

## 1. 通用契约（每个类型都一样）

```python
@dataclass(frozen=True, slots=True)
class SomeInvariant:
    name = "SomeInvariant"

    def check(self, *, tx: UnitOfWork, scope: Scope, actor_id: int,
              payload: dict[str, Any], before: dict[str, Any] | None) -> None:
        ...  # 不满足时抛 DomainError
```

* `check()` 必须是**关键字参数**、签名逐字一致（`tests/test_invariants.py` 断言这件事）。
* 违规时抛 `DomainError`：字段类用 `VALIDATION_ERROR`/`FIELD_INVALID`、
  越权类用 `FORBIDDEN`/`DATA_SCOPE_DENIED`、并发与余额类用 `CONFLICT` 或
  `VERSION_CONFLICT`。错误 `data` 里带 `rule` 键（前端与 Agent 按它复用文案）。
* `before` 可能是 `None`（新建、或该能力没提供回读函数），`payload` 只含**提交了的**
  字段。规则必须"缺事实就跳过"，**不许**把"不适用"报成 `INTERNAL_ERROR`。
* 内核不 import flask / pymysql / domains（`tests/test_architecture.py` 强制）。

## 1.1 三分法：什么时候跳过，什么时候必须吵（t20 的验收口径）

不变量在两种"缺东西"的场景下行为必须不同，判据是**缺的是业务事实还是基础设施**：

| 缺什么 | 判定 | 行为 | 例子 |
|---|---|---|---|
| **业务事实**：本次操作本来就没提交那条规则关心的字段 | 规则**不适用** | **跳过** | `payload` 里没有 `source_ref`、没有目标单据 id、没有状态字段、`expected_version` 未提交（字段声明 `required=True` 兜住） |
| **基础设施**：内核拿不到"把这条规则跑起来"所需的定位/上下文 | **声明与装配不一致** | **`INTERNAL_ERROR`** | 账本行 `_ledger_lines` 没回传、`Resource.table` 解析不出、状态机 `machine` 未注册、`OptimisticLock` 定位不到行、交了状态字段但该能力没有回读函数 |

**为什么不能一律跳过**：跳过会让"声明了规则但从不生效"变成**静默成功** ——
`fields=` 忘传、`_ledger_lines` 忘传、回读函数忘挂，都是同一个失败形态（DEVELOPMENT §4）。
**为什么不能一律报错**：若"业务事实缺失"也报错，那么**每一个新建能力**都会被不相干的
规则拒掉（`test_every_invariant_is_inert_when_nothing_was_submitted` 就是守这条的）。

当前按三分法**改成报错**的具体分支（t20）：
- `OptimisticLock`：提交了 `expected_version` 却解析不出表/主键 → 报错；定位得到行但读不到版本值 → 报错；
- `StateTransition`：`payload` 交了状态字段、**能定位到行**（有 `_resource_id`）却读不到旧值 → 报错（=该能力缺回读函数）；
  若是**新建**（`before=None` 且无 `_resource_id`）→ 仍然跳过；
- 其余 15 种：逐类复核过，静默分支都属于"业务事实缺失"，保持跳过。

## 2. 执行器提供的上下文

除服务回传的 `_invariant_context` 之外，执行器固定注入四个键（`runner._invariant_extra`）：

| 键 | 来源 | 谁在用 |
|---|---|---|
| `_resource` | `Capability.resource` | `StateTransition(machine="*")` |
| `_resource_table` | `Resource.table` | `UniqueCode`、`OptimisticLock`（都支持缺失时用 `_resource` 经 `RESOURCES` 解析） |
| `_invariant_exclude_id` | 路径参数 / `HandlerResult.resource_id` | `UniqueCode`、`AtMostOnePending` —— 不变量在写入**之后**执行，"本次刚写的那一行"会被自己查到，这个键让它们排除自己 |
| `_resource_key_column` | `Resource.key_column`（默认 `id`） | `OptimisticLock` |
| `_resource_id` | 路径参数 / `HandlerResult.resource_id` | `OptimisticLock` |

因此**每个域必须在 `Resource(...)` 上声明 `table=`**（省略时会按 `<module>_<name>s`
兜底，例如 `warehouse` + `material` → `warehouse_materials`；表名不同的域必须显式写）。
服务还可以回传 `_ledger_lines` / `_period_absolute` 等键，见下。

---

## 3. 类型清单

| 类型 | 规则 | 关键参数 | 备注 |
|---|---|---|---|
| `RequiredField` | #15 | `fields`, `labels` | 业务必填（≠ `Field(required=True)`）；值可回退 `before` |
| `RequiredWhen` | #22 | `when_field`, `when_values`, `required_fields` | 取消必须填原因（字段名统一为 `reason`，见 §2.11 权威字段表与 R4） |
| `AtLeastOneOf` | #16 | `fields`, `labels` | 投喂量/重量至少一个；**总是生效**（空提交也拒绝） |
| `DistinctActors` | #5 | `creator_field="created_by"`, `verifier_field="verified_by"`（**按表而定**） | 实际比较的是 `creator_field` 与**当前操作人**；`verifier_field` 目前**不参与**比较（见 §2.11）；`before=None` 时跳过 |
| `SameTenant` | #18 | `fields`, `tables`, `scope_fields` | 引用对象必须同企业/基地/区域；未声明表的字段跳过 |
| `ReferencedStatus` | #3、#18 | `field`, `table`, `statuses` | `statuses` 是**允许集**（不是排除集） |
| `NoNegativeStock` | #1、#2 | `table`, `columns`（元组，多列）, `group_by` | 判**写入后**余额 `SUM(col) < 0`；`_ledger_lines` 只用来定位分组，缺则报错 |
| `ZeroBalance` | #11 | `table`, `group_by`, `columns` | 关账时余额必须为 0；容差 1e-4 |
| `CumulativeWithin` | #9、#10 | `target_table`, `target_dims`, `target_columns`, `children_table`, `count_field`, `target_field`, `request_field`, **`new_row_counted`（必填）**, `key_column`, `for_update=True` | 判"本次生效之后"的累计：`自排除后的 counted + requested`；`new_row_counted` 声明库内那一行是否已算数，见 §2.7 |
| `AmountWithin` | #4 | `amount_field`（**入参名**）, `table`, `target_field`, `target_column`, `balance_column`（**表列名**）, `paid_column`, `statuses`, `currency_column` | 余额 = `balance_column` − `paid_column`；查询带 `FOR UPDATE`。**入参名与表列名不是一回事**，见 §2.11 |
| `NoOverlappingSource` | #8 | `source_types`, `target_fields`, `period_fields`, `override_source_types` | `source_types` 是**禁止并存**的来源类型 |
| `PeriodOpen` | #7 | `table`, `date_field`, `status="open"`, `tenant_keys=("organization_id",)` | 覆盖 7 条能力（成本/生产/仓储/采购/销售）；期间存在且状态不符即拒绝。**谓词必须带租户键**（会计期间每个企业各有一份）：缺租户键 → `INTERNAL_ERROR`（既不退化成跨企业查，也不静默跳过）；`LIMIT 1` 带 `ORDER BY period_start DESC, id DESC` |
| `StateTransition` | #14、#20 | `machine`, `field_name` | `machine` = 资源名 / `"*"` / **`Workflow` 实例**；交了状态字段却定位不到旧值 → `INTERNAL_ERROR`（§1.1） |
| `StatusAllowsEdit` | #12 | `statuses=("draft","submitted")` | 核验后只读 |
| `OptimisticLock` | #13 | `column="row_version"`, `expect="expected_version"` | `before` 里没有版本时回查一次库；**定位不到行或读不到版本值 → `INTERNAL_ERROR`**（§1.1） |
| `UniqueCode` | #19 | `fields`, `scope`, `table` | DB 唯一键兜底 + 应用层给可读 409；**取不到分租键时拒绝** |
| `HarvestQuantityMatch` | #6 | `harvest_field`, `order_field`, `unit_field` | `tail` 用 `quantity`，`jin` 用 `weight_kg×2` |
| `AtMostOnePending` | #21 | `dims`, `table`, `pending_states`, `key_column` | 与 DB 唯一键**都要有**；自排除下推到 SQL（§2.10） |

### §2.6 `StateTransition`

```python
StateTransition(machine="pond")                       # 记录生命周期 status
StateTransition(machine="pond", field_name="pond_status")  # 业务状态机（双状态资源）
StateTransition(machine="*")                          # 用当前能力声明的资源的 state 机
StateTransition(machine=INVENTORY_LOT_WORKFLOW)       # 直接给 Workflow 实例
```

`machine` 的三种形态：**资源名**（`RESOURCES` 里注册的）、`"*"`（当前能力声明的资源）、
或**直接一个 `Workflow` 实例**。第三种是给"没有列表页、不值得注册 `Resource`"的实体用的
（如 `inventory_lot`）：为了让它能被字符串引用而硬注册一个 `Resource`，会逼出一堆没有意义的
`list_path` / `columns` —— 那是为了迁就实现而伪造声明。

只在**本次请求提交了该状态字段**时校验；`from == to` 不算转移（幂等提交放行）。
拒绝时给出"只能变更为：…"的可读文案，由 `Workflow.require_transition` 生产。

### §2.7 `CumulativeWithin`

它比的是**"本次操作生效之后"的累计**。判定式两条路径**同一个**：

```
counted   = SUM(count_field) WHERE 目标维度 = ? AND status IN statuses
                                 AND 子行主键 <> _invariant_exclude_id   # 执行器自动注入
projected = counted + requested        # 两侧同量纲：都不做单位换算（见下）
```

**必填参数 `new_row_counted: bool`（不给默认值，忘选 → 构造期 `ValueError`）**：

* `True` —— **核验类**（`delivery.verify` / `receipt.verify`）：服务已把本行置成
  `statuses` 里的状态、**它已在 `counted` 里** → **只判 `counted`**（此时 SQL **不带**自排除，
  因为 `counted` 要如实含本次那一行）；
* `False` —— **登记类**（`delivery.create` / `receipt.create`）：本行还是 `draft`、
  **不在** `counted` 里 → **判 `counted + requested`**（此时 SQL 额外带 `id <> %s`：
  对登记类它**恒等**，作为"服务把本行写成算数状态却声明 False"这类误用的兜底）。

两个分支算出来的是**同一个业务量**：**本次操作生效之后的累计**。参数只是让声明者如实
说出"我这行在库里算不算数"，两种表达在数值上等价，但**语义各自明确**。
为什么必填、为什么**不自动探测**：探测要再查一次库、把同一件事算两遍，两边必然存在不一致
的时刻；而"我这行是不是已经算数"是声明者写服务时就知道的事实（create 写 `draft`、
verify 写 `verified`）。不给默认值是为了让"忘选"在**声明处**就报错。

为什么需要"子行主键 <> 我"：不变量在 `_call_service()` **之后**执行，两条路径上"本次这一行"
的处境**相反** ——

| 路径 | 本次那一行 | 自排除的作用 | 结果 |
|---|---|---|---|
| **登记**（`delivery.create` / `receipt.create`） | 新插入、还是 `draft`，**不在** `counted` 里 | `False` → 判 `counted + requested` | 本次登记的量也被算进限额 |
| **核验**（`delivery.verify` / `receipt.verify`） | 服务已把它置成 `verified`，**已在** `counted` 里 | `True` → 只判 `counted` | 本次那一行不会被算两遍 |

没有自排除时，核验路径会把本次那一行算两次 → **误拒合法操作**（实测：100 上限、其余已核验
60、本次核验 40，`counted` 已是 100，再 `+40` 判成 140 超限）。把这件事做成"声明时选一条
路径语义"的参数，等于让十条能力各判一次、各错一次；自排除是把它**变成不存在**。

```python
CumulativeWithin(
    target_table="sales_orders", target_dims=("sales_order_id",), target_columns=("id",),
    children_table="deliveries", count_field="quantity",
    target_field="quantity", request_field="quantity",
    statuses=("verified",),
    new_row_counted=True,          # ← 必填：核验类 True / 登记类 False（不给默认值）
    key_column="id", for_update=True,
)
```

- `target_columns`：子行用 `sales_order_id` 指目标，目标表主键列却叫 `id`；
- `key_column`：**子行**主键列名（自排除用），默认 `id`；
- `statuses` 只放**算数**的状态 —— 用排除集很容易漏掉新加的终止态；
- `for_update` **默认开**：并发两笔会同时读到旧累计而双双通过；
- 缺 `_invariant_exclude_id`（执行器定位不到本次那一行）→ `INTERNAL_ERROR`，不继续算。

**两侧都不做单位换算**（负责人 终裁）：`counted` 取子行原始值之和，`requested` 也取原始值。
只给 `requested` 一侧按 `unit` 换算，会让 `unit='jin'` 时**量纲不一致**：`counted=40`（原始值）
+ `requested=40×2=80` = 120 > 100，**误拒合法交付**（sales-dev 实测抓到）。
判据：**本规则（§4 #10）只回答"同一列的两部分加起来超没超限额"**，单位换算属于
**§4 #6 `HarvestQuantityMatch`**（出塘事实 vs 交付数量）—— 把 #6 的换算搬进来是把一条
规则的知识泄漏进另一条。`unit_field` 保留但**不参与算术**。若某条链路两侧量纲确实不同，
**在服务里先统一**。

### §2.8 账本类（`NoNegativeStock` / `ZeroBalance`）

* 账本行由服务回传：`HandlerResult(data={"row": ..., "_invariant_context": {"_ledger_lines": [...]}})`
  ，每行必须能按 `group_by` 取值（列名与库表列名一致：`warehouse_id` / `material_id` /
  `inventory_lot_id` / `batch_id` / `pond_id` …）。
* **`NoNegativeStock` 判的是"写入后"的余额**：不变量在 `_call_service()` 之后执行，
  `SUM()` 已含本次写入，所以判定式是 `SUM(column) < 0`（**不要**自己再加增量 ——
  那是 `available_before + 2×delta`，对负增量会误拒合法操作）。
  `_ledger_lines` **只用来定位分组**，不参与算术；缺它一律 `INTERNAL_ERROR`，
  绝不静默放行（旧形态 `if not lines: return` 会让"忘回传"变成"负库存照写"）。
  多列同时判（存塘要数量与重量都不为负），查询带 `FOR UPDATE`。
* `ZeroBalance` 不带 `FOR UPDATE` —— 聚合查询锁不住"还不存在的行"，行锁由仓储负责。

### §2.9 成本类（`PeriodOpen` / `NoOverlappingSource`）

`PeriodOpen` 需要**发生日期**与**租户键**两项，两项都必须能取到：

```python
PeriodOpen(
    table="accounting_periods",
    date_field="occurred_on",     # warehouse 用 happened_at；由服务经 _invariant_context 回传
    status="open",
    tenant_keys=("organization_id",),   # 默认值；会计期间这张表只有这一列分租键
)
```

* **日期取不到 → 跳过**（"本次操作没有发生日期"是业务事实缺失，字段必填由字段声明负责）。
* **租户键取不到 → `INTERNAL_ERROR`**：`accounting_periods.organization_id` 是 NOT NULL，
  **每个企业各有一份自己的期间行**。少了租户键，命中的是"任一企业的期间"——
  邻家已关账则**误拦**本企业的合法写入，邻家未关账则**静默放行**本企业已关账期间的写入
  （两向都由 `tools/repro_period_open_tenant.py` 复现）。所以服务必须经
  `_invariant_context` 回传 `organization_id`（或让 `before` 快照带上它）。
* `LIMIT 1` **必须带 `ORDER BY`**：不带 `ORDER BY` 时命中哪一行由存储/执行计划顺序决定，
  同一份数据两次查询可能给出不同结论——问题因此"不可复现"。

`NoOverlappingSource` 需要归属对象与期间两对字段，以及分租键（**不代表"能取到就带上"，
取不到就报错**——见下）：

```python
NoOverlappingSource(
    table="cost_entries",
    source_types=("warehouse_ledger",),          # 禁止与本次并存的来源类型
    target_fields=("target_type", "target_id"),
    period_fields=("period_start", "period_end"),
    tenant_keys=("organization_id", "farm_id", "area_id"),
    override_source_types=("manual_feed_direct", "manual_feed_offset"),
)
```

`period` 这类 `YYYY-MM` 输入算不出月末：服务把精确起止放进
`_invariant_context["_period_absolute"] = {"2026-02": ("2026-02-01", "2026-02-28")}`。

---

### §2.10 自排除必须写进 SQL（唯一性/互斥类规则的通用形态）

不变量在**写库之后**执行，所以"有没有冲突行"这类规则必须排除"本次刚写的那一行"。
正确做法是把排除**下推到 WHERE**：

```python
exclude_id = payload.get(_invariant_exclude_id)     # 执行器注入
if exclude_id is not None:
    where_parts.append(f"{self.key_column} <> %s")
    params.append(exclude_id)
```

**不要**写成"`SELECT … LIMIT 1` 取回任意一行，再用 `_is_self_match()` 比对"：
`LIMIT 1` 取到哪一行由存储引擎决定，库里有历史残留时可能先取到**旧行**，
于是比对失败 → 把"我自己那一行"当成"另一笔重复" → **把正常路径判成违规**（假阳性）。
`cost-dev` 的 e2e 就是这么失败的（探针残留导致正常写入被拒）。

采用这个形态的三个类型：`NoOverlappingSource`（#8）、`AtMostOnePending`（#21）、
`UniqueCode`（#19，它的风险方向是"可能漏判重复"）。三者都有 `key_column` 参数
（默认 `id`），并在 `_is_self_match()` 处保留兜底（执行器没注入排除键、或主键列不叫
`id` 时仍按 payload 比对）。

回归测试见 `tests/test_invariants.py`：`test_{no_overlapping_source,at_most_one_pending,
unique_code}_pushes_self_exclusion_into_sql`，以及反证用例
`test_without_exclusion_a_stale_row_would_have_been_a_false_positive`。

### §2.11 列名按表而定（`AmountWithin` / `RequiredWhen` / `DistinctActors`）

这三个类型的**参数值依表而异**，抄别人的示例会踩"列不存在"或"规则永远放行"。权威口径在
`docs/ROLLOUT_CONTRACT.md` §2A（表名列名表）与各域已落地的声明；下面是把"入参名 / 表列名"
分开写的对照，**照着抄之前先核对自己那张表的列**。

**`AmountWithin`（#4）—— 最容易被误导的一个**

| 参数 | 含义 | `purchase_payables` 的实际值 |
|---|---|---|
| `amount_field` | **请求体里的字段名**（本次付款金额） | `"amount"` |
| `balance_column` | **表里的"总额"列名** | `"total_amount"`（**该表没有 `amount` 列**） |
| `paid_column` | 表里的累计已付列 | `"paid_amount"` |
| `currency_column` | 表里的币种列（跨币种必须拒） | `"currency"` |

余额算法：`balance = {balance_column} − {paid_column}`。把 `balance_column` 写成 `"amount"`
会得到 `Unknown column 'amount' in 'field list'` —— 真库当场报错（不是静默失效）。

**`RequiredWhen`（#22）**

```python
RequiredWhen(when_field="status", when_values=("cancelled",),
             required_fields=("reason",), labels=("取消原因",))
```
字段名是 **`reason`**：早期版本叫 `cancellation_reason`，**本版统一为 `reason`**
（见 registry §2.11 权威字段表与 R4「描述服从字段表」；迁移里的 CHECK 名应为 `chk_*_reason`）。
⚠️ **名字写错的后果是"静默放行"**：`required_fields` 里的名字在 payload/`before` 里取不到时，
本规则按"该字段没提交"处理 → 看起来挂上了、实际永远不拦。所以**声明 `required_fields`
时必须与能力的字段表逐字一致**（t19 会把这条列为核对项）。

**`DistinctActors`（#5）**

| 参数 | 含义 | 备注 |
|---|---|---|
| `creator_field` | `before` 行上的"经办人"列 | 采购/销售都是 `created_by` |
| `verifier_field` | `before` 行上的"核验/审批人"列 | **按表而定**：`purchase_order` 是 `approved_by`（三段式：提交→审批→执行），`payment`/`receipt` 等是 `verified_by` |

**如实说明一处实现现状**：`check()` 实际比较的是 **`creator_field` 与当前操作人**
（"经办人不能核验自己提交的单据"），`verifier_field` **目前不参与比较**——它是
"这条规则作用在哪个列上"的声明，用于可读性与对账。所以每个域都应把 `verifier_field`
填成**自己表上的真实列名**：填错不会让规则失效（规则比的是操作人），但会让读声明的人
以为"比的是那一列"。这条已作为内核契约的待收口项记录。

## 4. 分工（不要重复实现）

| 谁 | 负责什么 |
|---|---|
| **内核不变量** | "什么条件下不允许写"；给出 `rule` 与可读文案 |
| **仓储 / 服务** | 行锁（`FOR UPDATE`）、唯一键、`UPDATE ... WHERE row_version=%s` 的实际写入 |
| **DB 约束** | 正确性底线：唯一键、CHECK、外键（并发下唯一可信的判定） |

`UniqueCode` 与 `AtMostOnePending` 都是**两层都要有**：DB 唯一键保证并发下不双写，
应用层预检负责给出可读的 409 —— 少了前者会双写，少了后者用户只看到数据库报错。

---

## 5. 自测

```powershell
python -m pytest tests/test_invariants.py -q     # 79 条：每条规则的通过/拒绝路径
python tools\kernel_smoke.py                     # 内核回归（既有 5 个类型的行为不变）
python -m pytest tests -q                        # 含架构约束
python tools\check_source_hygiene.py             # BOM / CRLF / 编码
```

新增一条规则时请同时补三处：类型（`invariants.py`）、`__all__`、以及在
`tests/test_invariants.py` 的 `_minimal_declarations()` 里一行 —— 最后这一处会让
"空提交必须放行（或按例外显式拒绝）"成为机械可查的事实。
