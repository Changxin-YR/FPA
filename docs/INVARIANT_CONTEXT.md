# 不变量所需的调用方上下文（invariant context）

> 谁需要回传、回传什么键、缺失时会发生什么。
> 这份清单是 t10 的产出（评审结论 ③），供 t19 与 t9 核对使用。

## 0. 为什么需要这份清单

不变量在 `runner._invoke_once()` 里的顺序是 **`_call_service()` 先写库 → `run_invariants()` 后校验**。
因此有些事实只有**调用方**知道（本次写的是哪个分组的账本行、期间边界怎么解析、刚写入的主键是哪一行），
内核无法自己算出来。这些事实经 `HandlerResult.data["_invariant_context"]`（服务回传）
与 `runner._invariant_extra()`（执行器注入）合并进不变量的 `payload`。

风险不在于"要不要回传"，而在于**缺失时的降级方向**。本项目的立场是：
**需要上下文才能判定的规则，缺上下文必须报错，不得静默放行**——因为"静默放行"会让
一条"看起来在强制"的规则变成建议（这正是早期版本那些"声明了却从不生效"的规则的产生方式）。

下面每行的"缺失时"一列都是**实测结果**，不是推测；证据脚本见 §4。

## 1. 执行器固定注入的键（调用方无需处理）

由 `kernel/runner.py::_invariant_extra()` 注入，来源是能力声明与路径参数：

| 键 | 来源 | 谁在用 |
|---|---|---|
| `_resource` | `Capability.resource` | `StateTransition(machine="*")`；`_resolve_table()` 的第三级回退 |
| `_resource_table` | `Resource.table`（经 `RESOURCES` 解析） | `UniqueCode`、`OptimisticLock` 的表名解析 |
| `_resource_key_column` | `Resource.key_column`（默认 `id`） | `OptimisticLock` 的主键列名 |
| `_resource_id` | 路径参数或 `HandlerResult.resource_id` | `OptimisticLock` 回查版本 |
| `_invariant_exclude_id` | 同上 | `UniqueCode` / `AtMostOnePending` / `NoOverlappingSource`：不变量在写入之后执行，"本次刚写的那一行"会被自己查到。三者都把它**拼进 SQL**（`<key_column> <> %s`），不再依赖「取回一行再比对」——见 §7 |

**这些键不需要任何域手工传。** 值取不到时（例如能力没有路径参数、也没有 `resource_id`），
键本身不会出现在 payload 里，由各不变量自行决定降级方向（见下表）。

## 2. 服务必须回传的键（`_invariant_context`）

| 键 | 谁需要 | 含义 | 缺失时的实测行为 |
|---|---|---|---|
| `_ledger_lines` | `NoNegativeStock`（§4 #1/#2） | 本次写入的账本行，**只用于定位分组**（哪些 `warehouse_id/material_id/lot`、`batch_id/pond_id` 组合），不参与算术 | **`INTERNAL_ERROR`**（硬报错，不静默放行）。账本行里缺 `group_by` 列同样 `INTERNAL_ERROR` |
| `_period_absolute` | `NoOverlappingSource`（§4 #8） | 期间解析结果，如 `{"2026-01": ("2026-01-01", "2026-01-31")}`；`YYYY-MM` 这种输入在声明里算不出精确月末 | 不报错：退回用 `period_start/period_end` 原值比较。**后果是期间重叠判定变粗**（如 `"2026-01"` 与 `"2026-01-15"` 的字符串比较），可能漏判重复归集 |

## 3. 来自能力载荷/快照的业务字段（缺则跳过或按 `required` 报错）

| 不变量 | 读什么 | 缺失时的实测行为 |
|---|---|---|
| `ZeroBalance`（#11） | `group_by` 的取值（`batch_id` 等） | **跳过（ALLOW）**——⚠️ 调用方漏传分组值时规则不生效，且不报错 |
| `CumulativeWithin`（#9/#10） | `target_dims`（如 `purchase_order_id`）、`request_field`（数量）、`unit_field` | **跳过（ALLOW）**——"未关联目标单据"是合法场景（如无采购单的到货），所以这里是刻意跳过；但目标单据**存在而查不到**时是 `NOT_FOUND`（不静默） |
| `HarvestQuantityMatch`（#6） | `harvest_document_id` | 跳过（ALLOW） |
| `AmountWithin`（#4） | `amount_field`、`target_field` | 跳过（ALLOW） |
| `ReferencedStatus`（#3/#18） | `field`（被引用对象 id） | 默认跳过；声明 `required=True` 时拒绝 |
| `SameTenant`（#18） | `fields` 里的引用 id + `scope_fields`（`organization_id/farm_id/area_id`） | 取不到的字段**自动忽略**（表里没有该列就跳过）；分租键取不到则不限定范围 |
| `DistinctActors`（#5） | `before.created_by`（经办人） | `before` 为 None → `INTERNAL_ERROR`（无快照无法判定经办人，不放行） |
| `StatusAllowsEdit`（#12） | `before.status` | `before` 为 None → 跳过（新建记录没有"核验后只读"可言） |
| `StateTransition`（#14/#20） | 目标状态；`machine="*"` 时还要 `_resource` | 目标状态取不到 → 跳过；`machine="*"` 且缺 `_resource` → `INTERNAL_ERROR`；**`before=None` 且无 `_resource_id` → 跳过（新建）**，但 `before=None` 又有 `_resource_id` 却读不到旧值 → `INTERNAL_ERROR`（=该能力缺回读函数，t20 收紧）。`machine=` 也接受 `Workflow` 实例（不必先注册 `Resource`） |
| `OptimisticLock`（#13） | `expected_version`；`before.row_version`，或 `_resource_table` + `_resource_id` 回查 | `expected_version` 缺 → 跳过（由能力字段的 `required` 负责拒绝）；`expected_version`**已提交**但解析不出表/主键 → **`INTERNAL_ERROR`**（t20 收紧，不再静默跳过） |
| `UniqueCode`（#19） | `fields`（编码）、`scope`（分租键） | 编码缺 → 跳过（更新时值没变）；**分租键缺 → `INTERNAL_ERROR`**（明确拒绝退化成全表查） |
| `AtMostOnePending`（#21） | `dims` 取值（如 `pond_id`）；执行器注入的 `_invariant_exclude_id` | `dims` 缺 → 跳过（本次不是新增一行）；**`_invariant_exclude_id` 缺且查到行 → `CONFLICT`**（方向是更严格：宁可误报，不放行真重复） |
| `RequiredField`（#15）/`AtLeastOneOf`（#16）/`RequiredWhen`（#22） | 被声明的字段 | **总是生效**：空提交也拒绝（`VALIDATION_ERROR`） |
| `PeriodOpen`（#7） | `date_field`（日期）、`tenant_keys`（分租键，服务经 `_invariant_context` 回传 `organization_id`，或由 `before` 快照带上） | **日期缺 → 跳过**（"本次没有发生日期"是业务事实缺失）；**租户键缺 → `INTERNAL_ERROR`**（会计期间每个企业各有一份，缺键会跨企业串号：邻家已关账→误拦、邻家未关账→静默放行已关账期间）。本企业查不到期间记录 → 放行（无期间即无锁定）；`status='closed'` → `CONFLICT`。SQL 带 `ORDER BY period_start DESC, id DESC` |
| `NoOverlappingSource`（#8） | `tenant_keys`（分租键）、期间字段、归属字段 | 分租键**任何一个取不到 → `INTERNAL_ERROR`**（t20 收紧：以前是「取不到就从 WHERE 里去掉」，那会让查重范围**扩到其他企业** —— 唯一一条「缺键后方向变宽」的规则）；`tenant_keys=()` 在构造期即被拒绝 |

## 4. 证据与复跑方式

三个探测脚本（都在 `%TEMP%` 下，不污染仓库）：

* `probe_kernel_contract.py` —— 33 条契约断言（写入后余额、多列、`FOR UPDATE` 有无、`jin` 换算、容差、`machine="*"`、排除键语义等），当前 **33 PASS / 0 FAIL**。
* `probe_context_degradation.py` —— 上表"缺失时"一列的来源。
* `probe_before_snapshot.py` —— 逐能力列是否声明了 `__fpa_load_by_id__`（`before` 快照的来源）。

## 5. 需要持续盯住的静默失效模式（来自上表实测）

1. **`ZeroBalance` / `CumulativeWithin` / `HarvestQuantityMatch` 缺字段时跳过**（判定为「业务事实缺失」）：
   与 `NoNegativeStock`（缺则硬报错）方向相反。跳过对"该字段本来就可选"的场景是对的，
   但对"本该必填却漏传"的场景是静默失效。核对方法：确认这些能力的字段表里对应字段是 `required`。
2. **`OptimisticLock` 提交了 `expected_version` 却解析不出表/主键 → 现在是 `INTERNAL_ERROR`**（t20 已收紧）；
   未提交 `expected_version` 仍跳过（由字段声明的 `required=True` 负责拒绝）。
3. **`StateTransition` 的三分法（t20）**：交了状态字段但定位不到资源 → `INTERNAL_ERROR`；
   能定位到行却读不到旧值 → `INTERNAL_ERROR`（等于该能力缺回读函数）；**新建**（`before=None` 且无 `_resource_id`）
   或**没提交状态字段** → 跳过。`machine=` 现在也接受 `Workflow` 实例（不必先注册 `Resource`），未注册的字符串仍报错。
4. **`NoOverlappingSource` 缺分租键时查重范围跨企业** —— **已在 t20 收紧为硬报错**：
   任何 `tenant_keys` 取不到 → `INTERNAL_ERROR`（以前是「取不到就从 WHERE 里去掉」，会让
   查重范围扩到其他企业；它是唯一一条「缺键后**方向变宽**」的规则，其余是变严或跳过）。
   服务必须回传 `organization_id` 等分租键；`tenant_keys=()` 已不允许（构造期 `ValueError`）。

## 5.1 租户维度：三种"看起来在管租户"的状态（对账工具 `[G1]/[G2]/[G3]`）

`kernel/scope.py::SCOPE_COLUMN` 只覆盖 `farm / area / pond / personal`，**不含
`organization_id`** ⇒ DataScope 行使的是"区域"而不是"租户"。但"规则里没出现
`organization_id`"**不足以**说明它不管租户，而且这个判据很容易写错——我自己就写错过一次：

* 旧判据的线索词里有裸 `organization`，于是 `ReferencedStatus` 因为**注释**里的一句
  "回查 organization/farm/area" 被算成"有租户意识"。它实际只调 `scope.allows_row`，
  而 `Scope` 不比 `organization_id`。**假阴性最坏**：优先级清单把不安全的说成安全，
  就没人去看挂它的那 6 条能力（含 warehouse 的 `receipt.*`）。
* 现在判据只看**会执行的代码**（先剥 `#` 注释与三引号文档串），并拆成三栏：

| 栏 | 语义 | 当前成员 |
|---|---|---|
| **G1 真按租户键** | 代码里出现 `organization_id`，真的按租户比较/查询 | `SameTenant`、`NoOverlappingSource`、**`PeriodOpen`（t2 已修：谓词带 `organization_id`，缺键即 `INTERNAL_ERROR`）** |
| **G2 委托 Scope** | 调 `scope.allows_row` / `scope.predicate` ⇒ **继承 Scope 的盲区** | `ReferencedStatus`、`SameTenant`、`AmountWithin`、`HarvestQuantityMatch` |
| **G3 完整盲区** | 既无租户键、也不做任何行级判定 | 其余 12 种（`PeriodOpen` 已于 t2 移出本栏） |

**这个区分指向不同的修法**，所以值得留着：G2 是一类"看着在管、实际不管"的规则
（做了行级判定，但那一维里没有租户）。**若将来在 `Scope` 层补上 `organization_id`，
G2 会被一次性治好**——那是"修一处、治一片"的位置；G1 与 G3 不会。

## 5.2 无确定行序的 `LIMIT 1`（对账工具 `[H]`）

`PeriodOpen` 的 `SELECT ... WHERE ... LIMIT 1` 曾经**不带 `ORDER BY`** ⇒ 命中哪一行由存储
顺序决定（`tools/repro_period_open_tenant.py` 双向复现）。**内核这一处已在 t2 修掉**
（现在是 `... WHERE <租户键> AND <日期区间> ORDER BY period_start DESC, id DESC LIMIT 1`）。

同一形态**不止内核有**：服务层的期间查询仍是这样，所以 `[H]` 继续做全仓扫描，
并明确它只是**候选清单**：

* 由唯一键保证至多一行的查询（例如查唯一键列）**是安全的**（`UniqueCode` 属这类）；
* 静态扫描分不清这两者 ⇒ `[H]` 只报位置 + SQL 片段，由负责人标注"这条靠唯一约束兜住"。

**t2 复核后仍在内核之外的候选**（读的是当前盘，不是记忆）：

* `cost/entries_write.py::close_period` ——
  `SELECT ... FROM accounting_periods WHERE period_start <= %s AND period_end >= %s ORDER BY id LIMIT 1`
  刻意不带 `organization_id`（关账能力声明 `scope=none`，期间是全局对象），
  所以它按"系统里任何一家企业的期间行"匹配。**它是同一族缺陷的剩余一处**，
  已上报 负责人 定夺（见 t2 报告"剩余风险"）。

判据一句话：**`LIMIT 1` 只有在"结果集至多一行"或"有 `ORDER BY`"时才确定。**
这条不只在核心里成立——**别只扫内核**。

## 6. 与 `INVARIANT_TYPES.md` 的分工

* `INVARIANT_TYPES.md`：**有哪些类型、参数怎么填**（内核视角）。
* 本文：**谁必须在什么时候回传什么、缺失会怎样**（调用方视角）。
* 执行器固定注入的键**不需要任何域代码参与**——域只管业务字段，以及服务经
  `_invariant_context` 回传的那两个键（`_ledger_lines` / `_period_absolute`）。

## 7. 自排除为什么必须写在 SQL 里（已实测的语义）

三个"查重/唯一"类规则都接受 `key_column`（默认 `id`），并把排除条件**拼进 SQL**：

```
UniqueCode          : SELECT id FROM ponds WHERE organization_id=%s AND code=%s AND id <> %s LIMIT 1
AtMostOnePending    : SELECT id FROM pond_status_change_requests WHERE pond_id=%s AND status IN (%s) AND id <> %s LIMIT 1
NoOverlappingSource : SELECT id FROM cost_entries WHERE ... AND <分租键=%s> AND id <> %s LIMIT 1
```

**为什么不靠"取回一行再比对 id"**：`LIMIT 1` 取到哪一行不由调用方决定。库里若先有别的
历史行，"取回来的那一行不是你刚写的"就会被判成冲突——**第一次合法写入被拒**，而错误消息
指向一个并不存在的问题。把条件写进 `WHERE` 之后，数据库只会返回"除本次以外"的行，
**查询动作与判定语义一致**。

**对调用方的要求**：不需要做任何事（执行器注入 `_invariant_exclude_id`）。唯一需要知道的是：
这些规则要求能力**能定位到自己的主键**（路径参数或 `HandlerResult.resource_id`）；两者都取不到的
写能力会在运行期撞上 `INTERNAL_ERROR`，而不是静默跳过——这是 t20 定下的方向：
**基础设施缺失 → 报错；业务事实缺失 → 跳过。**

## 8. 证据脚本与核对快照

`%TEMP%\fpa_verify\` 下的探针（可复跑、不依赖 MySQL）：

* `probe_kernel_contract.py` —— 33 条内核契约断言（写入后余额、多列、`FOR UPDATE` 有无、`jin` 换算、容差、`machine="*"`…）
* `probe_context_degradation.py` —— §3 表里"缺失时"一列的来源
* `probe_kernel_updates.py` —— 自排除下推 SQL、t20 的两条硬报错、`Workflow` 实例分支
* `probe_kernel_final.py` —— 终裁后语义：两侧都不换算、分租键硬报错（含"缺键时不发 SQL"）、`tenant_keys=()` 构造期报错
* `probe_row_actions.py` —— `row_actions` × `REGISTRY`（配合 `docs/ROW_ACTIONS.md`）

### 核对快照（**必须带时间戳**）

本文件描述的行为，最后核对于 **2026-09-12 23:28**，内核状态：

| 文件 | sha256 前 12 位 |
|---|---|
| `backend/fpa/kernel/invariants.py` | `0EE513A30370` |

当时读数：`pytest tests -q` → **184 passed / 1 xfailed**；`tools/kernel_smoke.py` → 全部通过；
`tools/gen_contract_docs.py --check` → 3 个生成区与内核一致。

**为什么每条读数都要带时间戳**：七个 agent 并发编辑同一个工作区，
没有时间戳的读数只是"某个曾真实的快照"，而不是"现在的事实"。
本文里若出现与快照不符的行为，以**重新跑一次探针**的结果为准，不以上文的描述为准。
