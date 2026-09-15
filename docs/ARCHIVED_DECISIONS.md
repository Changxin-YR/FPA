# 铺开期裁决记录（ROLLOUT RULINGS）

> 这是 负责人 在五域铺开期间发出的**口径裁决**的唯一事实源。
>
> **为什么需要这个文件**：这些裁决原本只存在于团队邮件里，而邮件会交叉。
> 实测后果："`CumulativeWithin` 用自排除、不用参数"这一条裁决**发了四次都没落地**，
> 而实现侧一直在等一句"授权"。裁决存在于一处（邮件）、实现状态存在于另一处（代码），
> 两处不一致，且**不一致时不报错**——正是本项目的主导失败模式。
>
> **纪律**：实现前先读本文件。若本文件与任何邮件、任何人的转述冲突，**以本文件为准**。
> 若你认为本文件某条无法实现，**写清具体反例**（哪条能力、什么数据、会得出什么错误结果）
> 报 负责人 重新裁决；不要用"另一种写法更好"作为理由默默偏离。

---

## R1. `CumulativeWithin` 的修法

> ### ⚠️ R1 已于第二版改裁（2026-09，负责人）——**机制部分以下面的 Amendment 为准**，本节下方"零参数自排除"的原文仅作留档，**不要照它落笔**。
>
> **改裁理由**：自排除有一个我原先没说出的隐含假设——**它要求"本次贡献正好是一行、且该行 id 就是 `_invariant_exclude_id`"**。这在"一单一行的 children 表"上成立，但**这个耦合没有任何地方强制**；一旦某域改成按明细行计数，`id <> X` 排不掉那批行，症状是**静默的双重计数**（正是本项目的主导失败模式）。
> 必填参数**不依赖任何 id 匹配**，且"本次这一行是否已被计入"是**声明者自己写那段服务时就知道的事实**（create 侧写 draft、verify 侧写 verified）——不是外部实现细节。**必填无默认**把"忘选"变成构造期报错，我原先担心的静默口已被堵住。
> 推翻者是 `复核人`（独立复现 + 论证"必填 + 语义单一，不要自动探测"）；这是本日第二次由成员推翻 负责人 的裁决（上一次是 cost-dev 证伪"一律后验"的普适性）。

### R1 Amendment（**按此落笔**）

**(a) 机制：必填参数 `new_row_counted: bool`，不给默认值。**
- 不传 → **构造期 `ValueError`**（不是运行期跳过）
- `True`（核验类，如 `delivery.verify` / `receipt.verify`）：本次行已被置成算数状态、已在 `counted` 里 → **只判 `counted`**
- `False`（登记类，如 `delivery.create` / `receipt.create`）：本次行还是 `draft`、不在 `counted` 里 → **判 `counted + requested`**
- **不要自动探测**（探测要再查一次库，两边各算一遍同一事实，必然不一致）
- **`for_update` 默认开启**

**(b) 两侧都不做单位换算**（**与机制无关，换机制后它仍然是错的**）
`counted = SUM(children.quantity)` 用原始值，而 `requested` 按子行 `unit` 做 `jin→×2` —— **同一列两种口径**，`unit='jin'` 时量纲不一致：销售单 100 斤、已计 40、本次 40 → `40+80=120>100` → **误拒合法交付**。
**结论：删掉 `CumulativeWithin` 里的 `jin_per_kg` 换算。** 单位换算属于 §4 #6（`HarvestQuantityMatch`：出塘事实 vs 交付数量），**不属于** §4 #10（同一列的两部分相加）。

**(c) 其余**：缺上下文按 R2 显式报错；docstring 删掉"本不变量在写库前执行"，写明判的是**核验生效之后**的累计；两侧测试（create 与 verify 各一条"合法通过 + 越界拒绝"，注释注明对应哪条路径）；修 `INVARIANT_TYPES.md` §2.7/§2.8/§3 三处旧形态；完成后广播 `sales-dev` 与 `purchase-dev`。

---

### ~~原文（已推翻，仅留档）~~

~~终裁，四次重申；不要用参数方案。~~ 结论：~~零参数，用自排除~~

```
counted   = SUM(count_field
                WHERE status IN statuses
                  AND target_dim = ?
                  AND <子行主键> <> _invariant_exclude_id)
projected = counted + requested
```

### 为什么不用 `new_row_counted` 参数
它把"本次这一行算不算在内"变成**每条声明各自的一次判断**。§4 #9/#10 下有 4 条能力
（`receipt.create` / `receipt.verify` / `delivery.create` / `delivery.verify`），
就是四次判错的机会。而"核验类 vs 登记类语义相反"**不是业务意图的差异，是执行顺序的副产物**
——让声明者去表达一个他不该关心的执行细节，就是在契约里泄漏实现。

**自排除连这个区别都不需要**：规则变成"把当前这一行从 SUM 里摘掉、再加上本次请求的量"
——无论服务有没有把本次行写成 `verified`，结果都一样。这才是"两条路径统一"的含义。

- `delivery.create`：新行是 `draft`，本就不在 `counted` 里 → 自排除是空操作 ✓
- `delivery.verify`：服务已置 `verified`、已落在 `counted` 里 → 自排除去掉它、`+ requested` 加回**一次** ✓

### 必须同时做的第二半：**两侧都不做单位换算**
`counted` 现在用原始 `SUM(children.quantity)`，而 `requested` 按子行 `unit` 做 `jin→×2`
——**同一列被两种口径对待**，量纲不一致。实测：销售单 `100 斤`，已交付 `40 斤`
（`counted=40`），再交付 `40 斤`（`requested=80`）→ `40+80=120 > 100` → **误拒合法交付**。

**裁决：删掉 `CumulativeWithin` 里的 `jin_per_kg` 换算**。理由：
**单位换算属于 §4 #6（`HarvestQuantityMatch`：出塘事实 vs 交付数量），不属于 §4 #10
（同一列的两部分相加）。** #10 只需要"同一量纲下的和"；把 #6 的换算照搬过来，
是把一条规则的知识泄漏进另一条规则。

### 其余要求
- `for_update` **默认开启**（并发两笔到货/交付会同时读到旧累计而双双通过）
- `_invariant_exclude_id` 缺失时**显式报错**（按 R2 的口径：基础设施缺失 → 报错）
- docstring 删掉"本不变量在写库前执行"（与模块头自相矛盾），写明 `projected` 是
  "**核验生效之后**的累计"
- 两侧测试：`create` 与 `verify` 各一条"合法通过 + 越界拒绝"，测试注释注明对应哪条路径
- 修 `docs/INVARIANT_TYPES.md` §2.7 / §2.8 / §3 三处自相矛盾的旧形态
- 完成后广播 `sales-dev` 与 `purchase-dev`（各 2 条能力受影响）

---

## R2. "缺上下文"的三分法（业务事实 vs 基础设施）

- **业务事实缺失 → 跳过**（规则不适用）：`payload`/`before` 里没有 `source_ref`、
  没有目标单据 id 等。若这类也报错，每个新建能力都会被不相干的规则拒掉。
- **基础设施缺失 → 显式报错**：缺账本行、解析不出表名、`machine` 未注册、
  解析不出 `_resource_id`/`_resource_table`。

**判据**：**"这条规则适不适用于本次操作"** 属于业务事实；**"内核有没有能力把这条规则跑起来"**
属于基础设施。前者缺 → 不适用；后者缺 → 声明与装配不一致，必须吵。

已确认属"基础设施缺失"必须报错的：
- `NoNegativeStock` 缺账本行 ✅（已落地）
- `UniqueCode` 解析不出表名 ✅（已落地）
- `OptimisticLock` 解析不出 `_resource_table`/`_resource_id` ⏳
- `StateTransition` 交了状态字段但 `before=None` 且无 `_resource_id` ⏳
- `NoOverlappingSource` **缺分租键 → SQL 里的 `organization_id` 条件会消失**，
  查重范围**扩到其他企业**（唯一一条"缺键后方向变宽"的规则）⏳ ← **安全相关，优先**

---

## R3. `UniqueCode` 自排除（**同一病的第三次，逐个类型排查**）

`UniqueCode.check` 用 `before["id"]` 判"是不是自己"，而 create 场景 `before is None`
→ **排除自己的分支永不成立** → 空表里第一次合法插入被判 409。
**修法：改用 `_is_self_match(payload, row)`（即 `_invariant_exclude_id`），
`before["id"]` 保留为更新场景回退。**

**并且逐个类型过一遍**：凡是"查存在性 / 查累计 / 查余额"的规则，都要问
"我查到的这一行会不会就是本次刚写的那一行？如果是，我用什么键排除它？"

已发生的三次同源缺陷：
1. `NoNegativeStock`：`available + delta`（算术把本次算两遍）
2. `CumulativeWithin`：`counted + requested`（同上）
3. `UniqueCode`：`before["id"]`（**存在性**把本次算两遍）

**注意**：此缺陷**只产生假阳性、不减损唯一性**——唯一性由 DB 唯一键在并发下保证。
所以下游"临时停用 `UniqueCode` 声明"是**已界定风险**，但必须在 e2e 里显式记账，
不要在终验里被当成"漏挂 §4 #19"。

---

## R4. registry 措辞（描述服从字段表）

- **`reason` 统一用 `reason`**：§2.11（权威字段表）明确写过"早期版本叫 `cancellation_reason`，
  本版统一为 `reason`"。`cancel_reason` 只出现在 §4 #22 的描述里 → **描述服从字段表**。
  影响：`purchase_order.cancel`、`sales_order.cancel` 的字段声明、`RequiredWhen` 参数、
  迁移里的 CHECK 名（应为 `chk_*_reason`）。
- **§4 #7 的范围改写为"写账目的能力"**，`cost.period.close` 不出现在本行叙述里。
  **不要写成"已知偏离"**——权威文档里不能出现"允许分叉"的措辞，
  它会让后来者以为清单与实现本就允许分叉。
  关账的约束由**服务层 `_require_open_period` + `UPDATE ... WHERE status='open'` 原子占位
  + DB CHECK** 三处承担；在文档里说明"为什么关账不在范围内"（它是**开锁动作**，不是写账目）。

---

## R5. 跨域读写（§2 的两条增补）

- **任何域都不得直接读写另一个域的表。** 例外：内核不变量按表名读取。
- **增补例外（与上一条同类）**：**按"表名 + 主键"解析分租键**由**内核提供唯一实现**
  （形如 `resolve_tenant_keys(table, record_id)` → `(organization_id, farm_id, area_id)`）。
  理由：Q6 已要求所有业务表补齐三列分租键，所以它是**表名参数化**、不含业务语义的通用件；
  且人工入口（人只选"哪个批次"）拿不到 farm/area，所以"由调用方传租户键"对人工入口不成立。
- `record_fact` 要求调用方传 `organization_id`/`farm_id`/`area_id` + **调用方自己的
  `ctx.actor.user_id`**：后者写进 `created_by`，正是 §4 #5 比较的列——
  **传占位值会让双人复核在自动归集路径上直接失效**，所以刻意无默认值。

---

## R6. 三条工程纪律（本日实证得出）

1. **结束任何一轮之前，跑一次 `bootstrap.load_all()`。** 它是唯一的全局门禁：
   `compileall` 只能查语法、`pytest` 要在能导入之后才有意义。
   **`load_all()` 会逐个 import 每个域的 `capabilities.py`，所以任何一个域的半成品
   都会让所有域的 e2e、探针、导出、CI 一起停摆。** 实测发生 3 次。
2. **报读数必须带时间戳或 commit。** 七个 agent 并发编辑同一工作区，
   **没有时间戳的读数不是证据，只是一个曾经真实的快照。** 已发生多次误判
   （"118 passed" 与 "load_all() 失败" 被并列成同一个当下）。
3. **门禁必须能在一部分域不可导入时仍然给出有效结论。**
   一个只能在"全仓都能导入"时才有效的门禁，**恰恰在最需要它的时候失效**。
   做法：关键断言优先走**自造注册表 / 直连库**的探针（`loader_probe.py`、
   `sales_schema_check.py` 在全仓红时依然 ALL PASS），并把"组合根可装载"
   做成**一条**独立门禁，让失败是 1 条而不是几十条级联。

---

## 附：`Resource.table` 必须显式声明

兜底公式 `<module>_<name>s` 会静默算错（`cost_entry` → `cost_cost_entrys`），
且**不报错**。已移除兜底。**凡是能猜错的地方，不许它猜。**
