# 裁决记录

> 本文件记录对 `CAPABILITY_REGISTRY.md` §7（Q1–Q12）的裁决。
> 每条给出**决定 + 理由 + 影响**。决定一旦记录，代码与文档都必须服从；
> 需要推翻时先改本文件，再改代码。

---

## Q1 — 塘口状态枚举 → 采用旧 DB 的 6 态，修正 INTERFACES.md

**决定**：采用 `build / stocked / farming / rest / clean / rebuild`（6 态）。

**理由**：`INTERFACES.md` 里我写的 `closed` 是我拍脑袋编的——它**在早期版本任何状态表里都不存在**，而 `clean`/`rebuild` 有生产验证的转移表依赖（`早期版本 master_data_service.py:21-24`）。协作者指出这一点是对的：**权威源应该是被生产验证过的枚举，而不是我的示意**。

**影响**：`INTERFACES.md` §2 的 `status_dict` 示例已修正。`closed` 从塘口状态里删除（它只属于 batch）。

**教训记录**：我在契约文档里写示例数据时用了自造枚举，差点让它变成事实标准。契约里的**每一个字面值都必须是可追溯的**。

---

## Q2 — 塘口状态变更保留两步审批

**决定**：**保留两步**（`pond_status.request` + `pond_status.verify`），恢复唯一键 `uq_pond_status_active_request`（同一塘口只有一个待核验申请）。

**理由**：
1. 需求明确写了"**审核**"属于高风险业务；
2. 塘口状态变更会解锁/锁死一批业务能力（`build → stocked` 才能建批次、`farming → rest` 会停掉投喂），属实质性的业务状态迁移；
3. "同一塘口只能有一个待核验申请"是个**很好的不变量示范**——它证明系统能表达"集合上唯一"而不只是"字段非空"。

**影响**：master_data 域 +2 能力（→ +2 总数）。需要一张 `pond_status_change_requests` 表。

---

## Q3 — 系统级权限码保留旧命名

**决定**：保留 `auth.user.manage` / `auth.role.manage`，在 `§0.1` 记为**显式的命名例外**。

**理由**：这两个权限码管的是**系统**（账号与角色），不是某个业务资源，`access.` 前缀会暗示它属于 access 域的资源。而且早期版本的种子数据与测试已用这两个码，改名等于为统一而统一地增加churn——不符合"能复用就复用"。

---

## Q4 — 确认与幂等的精确语义

**决定（两项）**：

### 4.1 `confirmation` 只保留 `never` / `always` 两个值，**删除 `by_key`**

理由：`by_key` 需要一个阈值体系（金额？不可逆性？状态？），而阈值本身需要产品决策。**没有阈值的 `by_key` 就是 `always`**——留一个语义未定义的枚举值比删掉它更危险（实现者会各自猜）。

**并明确语义边界**：`confirmation` **只约束 Agent 入口**。页面入口的二次确认是 UX 决定，不受此声明管辖。

### 4.2 `idempotent=true` 表示**强制携带** `Idempotency-Key`

理由：早期版本前端 `client.ts` 已经对**所有**写方法自动生成 `Idempotency-Key`，所以"强制"不会给前端增加任何工作量。而"允许但不强制"是一个无法测试的中间态——服务端无法区分"客户端没传"和"客户端传了但语义不同"。

**强制范围**：全部 13 条 `create` 能力 + 全部 `confirmation=always` 的能力（后者由 `requires_idempotency_key` 属性自动蕴含）。

---

## Q5 — 成本凭据降级为必填来源单号

**决定**：接受降级。`cost.entry.confirm` 的不变量改为 `RequiredField(fields=["source_ref"])`。

**理由**：P0 不做附件（§6 已定），"核验必须有凭据"失去载体。用"必填来源单号"替代，能保住"每笔成本都可追溯到底单"这个核心价值。

**记为已知降级**：写进最终交付报告的"尚未完成的问题"。附件能力是可恢复的（后续加 `attachment.*` 能力即可把不变量升级回去）。

---

## Q6 — DataScope 跨表解析：**不加 `via` 语法，改为补分租列**

**决定**：不支持跨表 JOIN 解析。改为在 `warehouses` / `inventory_lots` / `inventory_ledger` 上**补齐 `farm_id` / `area_id` 分租列**（与早期版本 `areas` / `ponds` / `pond_groups` 的做法一致）。

**理由**（这是我对协作者默认取的推翻）：
1. `via` 生成 `IN (SELECT id FROM warehouses WHERE ...)` 是**相关子查询**，随数据量增长会显著劣化，而分租列可以走索引；
2. 已加的列与既有 `organization_id` 形成一致的四级分租键（org / farm / area / pond），`ScopePolicy.resource(col)` 语法保持不变——**少一个语法就少一个实现处**；
3. 早期版本的 `ponds` / `areas` / `pond_groups` 就是这么做的，有生产验证。

**代价**：写入时需维护分租列（由 `ScopeResolver` 在 create 时一并写入，与 Q7 的裁决天然一致）。

---

## Q7 — create 不接收 `organization_id` / `farm_id` / `area_id`

**决定**：**确认变更**。这三个字段由 `ScopeResolver` 在服务端解析并写入。

**理由**：这是 DataScope fail-closed 的前提。如果客户端能指定 `area_id`，那"用户只能写自己区域的数据"就退化成"用户自报家门"——早期版本正是这样（`早期版本 master_data_service.py:15-18` 把这三个字段放在可写清单里）。

**前端影响**：字段元数据里这三个字段标记为**只读**（`readonly: true`），`DynamicForm` 渲染成禁用输入框并在旁边显示解析出的值。这样用户**看得见**自己的数据范围落在哪里——这正是需求里 DataScope 的可见证明点，而不是藏在后端。

**工作量**：`DynamicForm` 增加一个 `readonly` 分支即可，不算独立工作量。

---

## Q8 — "经办人≠审批人"推广到全部核验/审批能力

**决定**：
1. **推广到 11 条**核验/审批能力（协作者清单）；
2. 比较字段固定为 `created_by` 与 `verified_by`（比早期版本比 `updated_by` 更严格）；
3. **不留"同一人可自审"的例外**。

**理由**：早期版本只在 `sales_service.py:121-122` 一处实现——这是合规规则跨域不一致，正是重写要根除的东西。留例外会让"规则"退化成"建议"。

**声明形态**：`invariant=[DistinctActors(creator_field="created_by", verifier_field="verified_by")]`，由内核统一执行——因此**人工页面与 Agent 走的是同一份判断**。

---

## Q9 — 恢复 `harvests` 作为出塘事实源

**决定**：恢复 `harvests`（3 条能力）。`delivery.create` 的不变量保持"累计交付 ≤ 销售数量"，**并**恢复"交付数量与出塘事实一致"的比对。

**理由**：出塘是养殖业务的核心事实源（成本、存塘、交付三者都要它）。砍掉它换来的"省事"小于它带来的闭环缺失。同时 `issue-requests` 不恢复，`feeding.verify` 直接判库存（复用"不得负库存"不变量）。

---

## Q10 — `batch.close` 必须可达 → 恢复 `harvests`

**决定**：恢复 `harvests`，`batch` 生命周期完整可达：`stocked → farming → pending_settlement → closed`。

**理由**：这是协作者发现的最重要一条。砍掉出塘后，**没有任何能力能减少塘内存塘**，`batch.close` 的"存塘必须为 0"不变量永远不为真——一个**永远不可达的能力**比一个缺失的能力更糟，因为它在清单上看起来是有的。

**顺带确认**：`harvests` 写入 `batch_stock_records`（塘内存塘账本），`harvest.verify` 时校验 `harvest.quantity <= 当前存塘`（复用旧 `production_store.py` 已验证的 `FOR UPDATE` 写法）。

---

## Q11 — `params_hash` 覆盖客户端原始参数

**决定**：覆盖 **(a) 客户端提交的原始参数**。

**理由**：
1. 服务端补全值（`organization_id` 等）在**签发确认卡片时还不存在**，无法预知；
2. "用户看到什么就确认什么"——卡片上的 `rows` 就是从原始参数渲染的，哈希覆盖同一份数据才自洽；
3. 补全值是**从用户身份推导的**，不是用户输入，改它也改变不了授权的本质。

**实现约束**：`params_hash = sha256(canonical_json(原始参数))`，`canonical_json` 用 `sort_keys=True, separators=(",",":"), ensure_ascii=False`——与早期版本 `idempotency.request_hash()` 的规范化方式一致（那一处设计是对的）。

---

## Q12 — 规模：**一条都不裁，按 69 条做**

**决定**：不采用协作者的"砍 12 条 → 52 条"或"方案 B → 50 条"建议。**能力清单零裁剪**。

**理由**：

1. **清单里的每一条都是别人能力的产出或输入**，不是可选的 UI 便利。逐条检验后只有 2 条接近"纯便利"（`payable.list` / `receivable.list`），而删掉它们会让"应付余额从哪来"变成不可解释——早期版本把余额推导塞在 `purchase_payment_store.py:95-96` 的 SQL 里，正是这种"看不见的账"造成了对账困难。
2. **省下的时间不够**。11 条裁剪约省 25 h，在 300 h 总量里是 8%，但每条都会让对应流程少一个入口。
3. 协作者按"每页 4 小时"估前端 50 h，那是**手写页面**的成本。元数据驱动下加一个资源页的成本是"加一个声明"，不是"写一个页面"——这正是本架构的主要收益，不能按旧成本算。
4. 更根本的是：**砍能力会制造"永远不可达的状态"**（Q10 已证实一次）。清单短了不等于系统简单，可能是系统不完整。

**诚实结论**：**8 周是单人串行的真实数字**。我不假装它能在 2–3 周完成。按当前并行结构（内核 → 域 → web/agent 走关键路径，前端与 Harness 插件全程并行），我按 4–5 周节奏推进，**每阶段交付可验收的中间产物**，而不是最后一次性交付。

---

## Q13 — 成本域：**推翻协作者的"纯派生 + 只读"，保留 5 条**

**决定**：cost 域保留全部 5 条（`cost.entry.list` / `cost.entry.create` / `cost.entry.confirm` / `cost.summary` / `cost.period.close`）。

**背景**：我在分工时举过一个例子说"cost 只需要「按塘口/批次查成本」+「成本随业务事实自动生成」"。协作者照此把 cost 从 5 条压到 2 条，并逐条列出了 6 项代价。

**推翻理由**（三处需要纠正）：

1. **"成本随业务事实自动生成"不是一个能力**——它是 `feeding.verify` / `receipt.verify` / `issue.verify` 的**副作用**。所以"成本只需 2 条"这个说法的真实含义是"成本只需读，写靠别的能力的副作用"，它**完全遗漏了系统外费用**（人工、水电、租金）。这是覆盖面缺口，不是精简。
2. **`cost.entry.confirm` 是最能体现双人复核设计的载体**。需求明确写"审核、付款、收款"属高风险业务。`cost.entry.confirm` 上挂着两条不变量：`DistinctActors(created_by, verified_by)`（经办人≠审批人）与 `RequiredField(source_ref)`（每笔成本可溯源）。删掉它等于删掉成本域的双人复核能力——而"经办人≠审批人"在早期版本里**本来就只在 sales 域实现过一处**，正是我们要推广的合规规则。
3. **期间锁定（不变量 #7）不失去载体，而是应该被加强**。协作者说"早期版本 `require_unlocked()` 只挂在手工入口上（`早期版本 cost_store.py:71,79`）"——这是**对早期版本的准确描述**，但新系统可以做得更强：把 `PeriodOpen` 不变量挂到**全部 4 个会写成本的能力**上（`feeding.verify` / `receipt.verify` / `issue.verify` / `delivery.verify`）。这样"已关账期间不得再归集成本"覆盖派生路径，而不只是手工路径。**一个不变量覆盖 4 条能力，比挂在 1 条手工入口上强得多**——这是架构升级带来的收益，不应该反过来被当成删除的理由。

**顺带修正**：`cost.entry.confirm` 在 §1.9 里标的是 `confirmation=never`，而它是 `risk=high` 的审批动作——按 Q4 的裁决（`risk=HIGH` 默认 `always`），这里应当是 `always`。已记录为需要修订的条目。

---

## 关于我的指令质量的一条备注

Q13 这次往返是我的责任：我在分工时用"例如 cost 只需要…"这种**举例口吻**给出了一个数值目标，协作者把它当成了**指令**并严格执行，还逐条列出了执行代价——它的执行是规范的，问题在于我的例子本身是错的。

**教训**：给协作者的规模类指导必须区分"这是硬目标"与"这是我随手举的例子"。后续分工里我会明确标注 `【硬约束】` / `【举例，可推翻】`。

同时也说明一件事：**协作者的产出要被复核，不能直接进方案**——当前迭代它给出的 H3（迁移校验和）是误报，Q13 是过度执行，而它自己发现的 Q10（`batch.close` 不可达）与 Q1（我自造的状态枚举）是真问题。

---

## 汇总影响

| 项 | 决定 |
|---|---|
| 能力总数 | **69** |
| 恢复 | `harvest.list/create/verify`（3）、`pond_status.request/verify`（2） |
| 删除 | `by_key` 枚举值 |
| 保留（推翻裁剪建议） | cost 域全部 5 条 |
| 新增表 | `pond_status_change_requests`、`batch_stock_records`、`inventory_lots`、`inventory_ledger`、`accounting_periods` |
| 契约修订 | `INTERFACES.md`：pond status 6 态、`confirmation` 去 `by_key`、字段加 `readonly` |
| 不变量 | 20 条，全部由内核统一执行；`PeriodOpen` 由 1 条能力扩展到 4 条 |
| 规模 | 单人串行 ~8 周；按并行结构 4–5 周节奏，分阶段交付 |

---

## 待办（本次裁决派生）

1. `INTERFACES.md` §2 `status_dict` 改 6 态 ← **已完成**
2. `INTERFACES.md` §2 字段加 `readonly` 标记 ← **已完成**
3. `INTERFACES.md` 确认策略去 `by_key` ← **已完成**
4. 内核 `Field.readonly` + `Capability.scope_fields()` + 拒绝提交 readonly ← **已完成并有自检**
5. 内核不变量 `DistinctActors` / `RequiredField` / `NoNegativeStock` / `PeriodOpen` / `RequiredWhen` ← **已完成并有自检（可脱离 MySQL 测试）**
6. `CAPABILITY_REGISTRY.md` 按 Q1–Q13 裁决修订 ← **待办（派给 backend-recon）**
7. 业务表迁移 002（按裁决后的表清单）← **待办**
8. `cost.entry.confirm` 的 `confirmation` 由 `never` 改 `always` ← **待办（随 6）**

---

## 附：内核自检的当前状态

```
tools/kernel_smoke.py        11 组断言，全部通过
tools/migrate_selftest.py     4 组断言，全部通过
tools/db_selfcheck.py         8 组断言，全部通过（真实 MySQL 9.7）
tools/runner_e2e.py          22 组断言，全部通过（真实 MySQL 9.7）
```

这些自检的作用不是"证明代码能跑"，而是**证明早期版本的高危缺陷在新系统里被结构性消除**。
每一条 `PASS` 都对应一个有证据的旧缺陷，可以在最终交付报告里作为 Resume Verification 的材料。

---

## Q14 — 删除 `meta.resources` 这个**独立端点**（采纳协作者论证）

> ## ⚠️ 术语澄清（必读，防误读）
>
> 本裁决删的是**一条多余 HTTP 路由**，**不是**响应体里的 `resources` 数组。
>
> | 对象 | 处置 | 说明 |
> |---|---|---|
> | `GET /api/v1/meta/resources` 这条**独立路由** | **删除** | 它从未在 `INTERFACES.md` 里定义过，是 registry 自造的 |
> | `GET /api/v1/meta/capabilities` **响应体里的 `resources: [...]` 数组** | **保留，一个字都不许改** | `DataTable` 的 `columns` 与 `status_dict` **全靠它**；删了会真的断掉列表页 |
>
> 前端成员的提醒是必要的：我原来的措辞「删除 `meta.resources`」把"路由"和"数组"混成了一个词。
> 缩写造成的歧义在跨端协作里就是缺陷——**这条澄清是裁决的一部分，不是补充说明**。

**决定**：删除 `GET /api/v1/meta/resources` 这条独立路由，能力总数 **69 → 68**。

**理由**：协作者按我要求的标准（"指出具体断掉的路径"）重新论证，并**去核实了 `INTERFACES.md` 原文**而非凭记忆，结论比我上次的判断更强：

1. `INTERFACES.md:49` 只定义了**一个**端点 `GET /api/v1/meta/capabilities`；全文 `/api/v1/meta/` 只命中两处（定义与引用），**没有任何 `/api/v1/meta/resources` 的定义**——`meta.resources` 是 registry 自造的端点，契约文档从未认过它。
2. 该响应体（`:53-122`）**同时**含 `capabilities` 段与 `resources` 段。所以列表列与 `status_dict` 的消费路径**不会断**（见上方澄清表）。
3. 保留它反而制造矛盾：OpenAPI 需要记录一个契约文档里不存在的端点——这与 Q1 刚确立的"契约里每个字面值都必须可追溯"直接冲突。

**为什么这次我采纳，而上次不采纳 cost 的裁剪**：两者的性质不同。

| | 删 `payable.list`（不采纳） | 删 `meta.resources` 路由（采纳） |
|---|---|---|
| 被删的是什么 | 一条**业务读路径** | 同一份响应体上的**第二条 HTTP 路由** |
| 删后余额/数据从哪来 | 变成不可解释的隐藏 SQL 计算（**降级**） | 同响应里本来就有（**去重**） |
| 契约文档是否定义过 | 是 | **从未定义** |

判据是"**删掉之后，信息是否还可得、以及是否仍可解释**"。REST 资源与业务能力不是一个概念——前者是传输形状，后者是业务语义。

---

## Q15 — `DistinctActors` 扩展到 **13 条**（采纳协作者 Q8′）

**决定**：扩展到 13 条，含新增的 `harvest.verify` 与 `pond_status_change.verify`。

**理由**：Q8 自己确立的原则是"推广到全部核验/审批能力""留例外等于把规则降级成建议"。新增的两条能力确实是核验/审批，**按同一原则就该纳入**——否则规则从建立的第一天就带着例外，以后每条新核验能力都要重新争论一次。

协作者**没有擅自扩大**而是把冲突报上来，这个做法是对的。

`pond_status_change.verify` 尤其重要：塘口状态变更会解锁/锁死一批业务能力（`build → stocked` 才能建批次），**要求由另一个人核验**是这条规则最有价值的应用场景。

---

## Q16 — `PeriodOpen` 的语义修正：不是"写成本"，是"记账期间已关闭"

**决定**：修正口径。`PeriodOpen` 覆盖 6 条能力，其语义是"**不得在已关闭的会计期间产生任何影响期间的账目记录**"，而不仅是"成本"。

| 能力 | 挂 `PeriodOpen` 的理由 |
|---|---|
| `cost.entry.create` | 直接记成本 |
| `cost.entry.confirm` | 确认成本入账 |
| `cost.period.close` | 关账这一动作本身 |
| `feeding.verify` | 核验时自动归集投喂成本 |
| `receipt.verify` | 核验时自动归集采购成本 |
| `issue.verify` | 核验时自动归集领用成本 |
| `delivery.verify` | **经裁决纳入**：它写应收（收入侧）。关账锁的是整个期间（收入与成本一起锁定），不是只锁成本侧 |

**为什么 `delivery.verify` 该进去**：协作者指出它写的是应收而非成本——**这个观察是准确的**，而正确的推论是修正不变量语义，不是把它排除。关账如果只挡成本侧、放行收入侧，会出现"期间已关闭但收入仍可入账"的账目失衡，这比漏挡成本侧更严重。

**因此把 `PeriodOpen` 的文档表述从"不得再归集成本"改为"不得再产生影响该期间的账目记录"**，并要求实现时对收入侧与成本侧一视同仁。

---

## Q17 — 上下文令牌改为**会话级**（修正我自己的契约矛盾）

**决定**：令牌有效期从"单轮 90 秒"改为"会话级 1800 秒"，并明确"授权实时性来自
每次调用重新校验会话，而不是令牌短命"。

**问题来源**（协作者 backend-recon 上报）：`INTERFACES.md` §4.1 同时写了三件互相
矛盾的事——

    (a) "有效期：单轮（默认 90s）"
    (b) "每轮对话重新签发"
    (c) "由插件从受控配置读取"

(c) 是插件的实际能力：`apply()` 只在进程启动时读一次配置。(a)+(b) 要求每轮换令牌，
**第二轮起每次调用都会 401**。协作者按契约实现了插件，然后发现契约自己不自洽。

**为什么我写错了**（值得记下来）：我下意识认为"令牌短命 = 更安全"，于是把 TTL 设成
一轮。但**这个系统的授权实际来自每次调用对实时会话的重新校验**：

`
routes_agent._actor_from_context_token()
  -> _current_actor() -> AccessService.resolve_session()
`

所以：用户登出，会话撤销，令牌立刻失效；管理员改权限，下一次调用就按新权限判定；
令牌泄露，攻击者还需要该会话仍然有效。

**令牌短命带来的额外安全性几乎为零**，代价却是"必须每轮重建子进程"（冷启动 1.8 秒）。

**这是从早期版本照抄来的妥协**：早期版本坚持"令牌绑定当前迭代提示词与请求、且 SDK 在启动时
快照扩展配置"，因此不得不每轮 `_cache.drop()` 重建。我抄了 TTL 数值，却没有意识到
它服务于旧架构的一个限制——**"这个参数为什么是这个值"比"这个值是多少"重要**。

---

## Q18 — 确认结果**不回传**给模型（刻意的）

**决定**：确认由用户在前端点按钮触发，服务端执行，结果由**前端直接显示**；模型不参与。

**理由**：`WRITE_CONTRACT.md` 规则 3 要求 `message` 由服务端从真实数据渲染。
若让模型复述确认结果，就多了一条"模型转述走样"的路径——而用户在确认卡片上看到的
本来就是服务端给的准确数字。**让模型复述只会引入不确定性，没有收益。**

这也保证了 `executed` 与 `confirmation_required` 在协议层互斥：模型永远不处在
"既要发起确认、又要报告结果"的位置上。

---

## Q19 — 工具元数据增加 `requires_idempotency_key`

**决定**：`ToolSpec.to_meta()` 输出 `requires_idempotency_key`，插件据此决定是否生成键。

**理由**（协作者上报）：原实现没有这个字段，插件只能一律带键或一律不带。前者无害但让
服务端失去"非幂等能力不该收到键"这个契约违规信号；后者会让重复敏感操作被服务端拒绝。

这是"声明即契约"的一个直接推论：**凡是服务端要据此判定的东西，都必须出现在元数据里**，
否则调用方只能猜。

---

## Q20 — `ask_user` 作为**工具**是预期设计

**决定**：确认。

**理由**：模型需要**在轮次进行中**主动发问（例如"有多个同名塘口，你要哪个"），
而不是等轮次结束。Harness 的工具调用机制就是模型在一轮内表达"我需要用户输入"的
唯一通道。

它的返回值走 `kind="clarification"`——这是本项目在 `WRITE_CONTRACT.md` 的判别联合里
新增的第四种 `kind`（与 `executed` / `confirmation_required` / `failed` 并列）。

---

## Q21 — 推翻「`materials` 保留只读」：物料改为**可维护**（t18）

> ## ⚠️ 这是一次**显式的推翻**，不是补充
>
> `docs/CAPABILITY_REGISTRY.md` §1.4 原文（本次修订前）：
>
> > 「`materials` 保留**只读**：物料是投喂/出入库的引用对象，由种子或导入建立；
> > `material.verify` 砍掉（见 §6）。」
>
> 那条口径**被本条裁决推翻**。registry §1.4 那一行已改为删除线 + 引用本条，
> 并把新增的三条能力（`material.create` / `material.update` / `material.archive`）
> 写进 §1 的能力表。**本条的用途就是让"推翻"这件事有一个可追溯的落点**——
> 而不是让下一个人从"表里多出三行"去猜是谁加的、为什么加。

**决定**：物料改为**可维护**，新增 `material.create` / `material.update` /
`material.archive` 三条能力（停用 = 归档，见下）。区域同理（见 Q22）。

**理由三条，每一条都经核实，不是转述**：

1. **本版没有可用的种子/导入管线——这是核实过的，不是推测。**
   核实方式（三条命令，任何人可复跑；**实测输出一并记在下面**）：

   ```powershell
   # ① 有没有 database/seeds/ 这个目录？
   Get-ChildItem database -Recurse -File | Select-Object FullName
   # ② 迁移里是怎么建立物料的？（只看 INSERT 那一句）
   Select-String -Path database\migrations\*.sql -Pattern 'INSERT INTO materials' -Context 0,12
   # ③ tools/ 下有没有导入器？
   Get-ChildItem tools -Filter *import* -Recurse | Select-Object FullName
   ```

   **实测输出**：

   * ① `database/` 下**只有 `migrations/` 一个子目录**（`000`–`009` 共 10 个文件），
     **`database/seeds/` 这个目录根本不存在**——所以"由种子建立"连一个约定的落点都没有；
   * ② 全仓唯一建立物料的路径是 `003_master_data.sql:305` 的
     `INSERT INTO materials (...) SELECT ... FROM areas CROSS JOIN (...) AS seed`，
     而那个内联 `seed` 只有 **2 行**（`MAT-001` 1号虾配合饲料 / `MAT-002` 2号虾配合饲料）。
     要加第三条物料就得**改迁移文件本身**；
   * ③ `tools/` 下**没有任何导入器**——唯一命中 `*import*` 的是
     `tools/cost/verify_preflight_import_probe.py`，它是"预检能不能 import 模块"的
     探针，与数据导入无关。

   于是"由种子或导入建立"在本版**没有任何可执行路径**——原裁决描述的那条路不存在。
   这与 Q10 检出的 `batch.close` 是同一类问题的镜像：
   **一个在文档里"有来源"、在系统里"没有入口"的东西**。

2. **用户报「缺少」。** `ROADMAP.md` §4.2 的 P0 第二条逐字为
   「**主数据可维护**：区域 / 物料 / 仓库 的 `create` + `update` + 停用」，
   而该节 §0 记录了用户的原始表述是「总感觉有地方缺少」。
   §4.1 的能力矩阵也把「区域 / 物料 / 仓库」的"增"与"改"两格标为 ❌。

3. **"能新增物料"不削弱原裁决的只读意图。**
   原裁决真正要保的是"**物料的业务状态不该被随意推动**"，判据在它砍掉的那条能力上：
   `material.verify`（把物料从 `draft` 推到 `verified`，从而让它成为投喂/出入库的
   **合法引用对象**）。

   **`material.verify` 至今仍然砍掉。** 本版新增的三条里没有它，而且停用（归档）是
   **单向**的（`archived` 是终态，没有取消归档的能力）。也就是说：

   | 原裁决要保的 | 本版的实际状态 |
   |---|---|
   | 物料不能被"核验"成合法引用对象 | ✅ **仍然成立**——没有 `material.verify` |
   | 物料的来源应当被审计 | ✅ **更强了**——三条能力都记 `before_after` 全量 diff，而"改迁移文件"完全不落审计 |
   | 物料不该被随手删掉 | ✅ 全部改为归档；全系统 `delete` 能力仍为 **0**（§0.2） |
   | （原裁决没有主张的）台账字段不可维护 | ❌ 这一格改变了——而它**不是**原裁决主张的东西 |

   换一种说法：原裁决把"只读"当成了达成"状态不可推动"的手段，而**这两件事本来
   可以分开**。分开之后，原来那条意图不仅没被削弱，反而因为"建立物料的是一个
   带权限与审计的能力，而不是一次改 SQL 的迁移"而变得**更可追溯**。

**同时明确**：`materials` 的 `category` **仍然不做 enum 校验**（003 迁移的原话：
"会变的分类不该固化成约束"）。新增 `create` 时最省事的做法就是把它写成
`f_enum(...)`，而那会把那条既有决定偷偷推翻——运维分册里的「水质改良剂」
会变成非法输入。**本条一并把它钉住。**

**代价（必须写下来）**：目录里的"演示数据"与"真实数据"从此不再由同一条路径产生。
003 迁移的种子仍然是 `verified`，而新建的物料是 `draft`——两者的 `status` 不同。
这不是缺陷，但会让"为什么这条能投喂、那条不能"看起来不一致。
处置见 Q22 的同名段落（本版裁掉了 `*.submit` / `*.verify`，所以 `submitted` 状态码
保留在 `status_dict` 里但没有能力能进入它）。

---

## Q22 — 区域与仓库同样改为可维护；`warehouse.create` 的初始状态是 `verified`

**决定**：`area` 新增 `create` / `update` / `archive`（与物料同形）；
`warehouse` 新增 `get` / `create` / `update` / `archive`（4 条）。

**区域与物料同理由**：registry §1.4 对区域的原始口径是"区域由种子/迁移建立"，
Q21 的第 1 条理由逐字适用——`database/` 下没有 `seeds/` 目录，
而 003 迁移的 `INSERT INTO areas ... CROSS JOIN (...)` 只种了
`A-NORTH` / `A-SOUTH` **两条**演示区域（实测见 Q21 第 1 条的复跑命令与输出）。而"建不了区域"正是用户报「缺少」时
最先撞到的东西——塘口的 `area_id` 是必填的，而它是唯一的下拉来源。

**仓库的 4 条里有一条需要单独裁决：`warehouse.create` 的初始状态**。

另两个资源的 `create` 产出 `draft`，仓库**刻意不同**，产出 `verified`。判据不是
一致性，是**可达性**：

* `receipt.verify` 只在 `warehouses.status = 'verified'` 时才入账
  （`receipt_write.py` 的 `WAREHOUSE_NOT_VERIFIED`）；
* `ledger._default_warehouse_id` 只在 `verified` 的仓里选（`feeding.verify` 走它）；
* 而本版**没有** `warehouse.verify` 能力（不在 t18 的十条里，也不在 §1.6 里）。

于是"新建的仓库是 `draft`"的后果是：**它永远无法被任何业务路径使用**。
用户建了仓库、列表里有这一行、所有用到仓库的功能都拒它——这正是 Q10 判定的
"一个**永远不可达的能力**比一个缺失的能力更糟，因为它在清单上看起来是有的"
的**记录级**形态。

**代价（明确写下来，不许省略）**：`verified` 从此不再表示"有人核验过"，
而表示"这条记录处于**可被业务引用**的状态"。这与 006 迁移的种子一致
（它就是把仓库直接写成 `verified`），也与"本版裁掉 `warehouse.submit`/`verify`"
这一事实一致。**将来若恢复两步审批，这里应当改成 `draft`**——那时"建出来即可用"
就不再需要由初始状态承担。

**并入的一处更正**：registry §2.7 原先两处把 `warehouse_id` 的约束写成
「仓库须 `active`」，而 `warehouses.status` 的 CHECK 约束取值是
`draft / submitted / verified / archived`——**`active` 这个值不存在**。
已更正为 `verified`（真实判定见 `receipt.verify`）。一个不存在的状态值写在权威
清单里会让读者去改一个改不动的状态，或以为 `verified` 的仓库不能收货。

**域归属**：这 4 条**属于 `warehouse` 域，不是 `master_data`**。
`tests/test_architecture.py::test_capability_domain_matches_its_declaring_directory`
是一条集合相等断言（`Capability.domain` 必须等于声明目录的域名）——
把 `domain="master_data"` 写在仓库能力上是**静默错域**：`Registry.by_domain`、
前端按域菜单、`runner` 的 `resource_type` 全部跟着错，而没有一条既有检查会红。
**判据是代码在哪，不是"这一轮是谁在做"。**

**顺便记一条口径**：`docs/CAPABILITY_REGISTRY.md` §1.4 提到的「`material.verify` 砍掉」
在 t18 之后**仍然成立**——本版没有新增任何 `*.submit` / `*.verify` 能力。
所以 `area` / `material` / `warehouse` 的 `submitted` 状态码**保留在 `status_dict` 里**
（历史行要有中文标签），但**没有任何能力能进入它**。处置与
`warehouse_document` 的 `archived` 一致：`reserved=True` 是机器可读的声明，
而不是一句注释。

---

## Q23 — 两种"卡片"必须**交付到浏览器**；一轮只能有一个 `kind`，优先级写死

**决定**：`agent_turn` 把事件流里的 `confirmation_required`（网关签发的确认卡片）与
`clarification`（`ask_user` 的提问）提取出来，作为 §3 的 `result` 交付给浏览器。
同一轮命中多种 `kind` 时按 **`executed` > `confirmation_required` > `clarification`
> `assistant`** 取第一条。

**理由**（用户报障 + 契约缺口）：

1. **令牌只出现一次。** 确认令牌是服务端一次性签发的，`IssuedConfirmation.token`
   只在那一次响应里存在 —— **没有补发的入口**。所以"模型在工具结果里看到了卡片"
   与"用户能确认这张卡"是两件事，中间必须有这一段交付。缺它的后果是一条完整的
   静默失败链（实测会话 `8a148ce4220e04e8`）：模型说"已生成 3 张待确认卡片，
   请在界面上确认" → 界面上 0 张卡 → 用户回"执行" → 模型答"我这边还没有收到
   确认结果"。**三方都没报错，坏的是中间的交付。**
2. **`clarification` 是 Q20 已经确认的第四种 `kind`**，而前端那张卡（问题 + 可点选项
   + 自由作答框）此前在生产路径上一次都没被触发过。用户只会看到模型复述的一行文字，
   实测会话 `bf3c8c19c0c2e101` 里 `options:["供应商","客户"]` 就是这样丢的。
3. **一轮只能交付一个 `kind`**（§3 是单值字段），所以必须给出确定的顺序。
   `executed` 排第一是因为"列表要不要重拉"最硬：卡片可以由用户重新发起，
   而一个没刷新的列表会让用户以为写没成功。判定收在 `agent_turn.turn_result()` 一处，
   **流式与非流式共用同一个函数**（两条路径各自判断必然漂移）。
4. **卡片是"一批"。** 一次对话里模型可以对多个对象各签一张卡（实测 3 张），
   所以 §3 ③ 增加附加字段 `confirmations`（`confirmation` 仍是第一张，非破坏性），
   前端逐张渲染、逐张确认；确认其中一张时**其余卡片原样保留**。
   少渲染一张 = 那张的令牌永久丢失。

**顺带记一条同源的交付缺陷（本次一并修）**：`assistant/chunk` 里的
`reasoning-delta`（模型的**思考**）此前被当成正文 `delta` 发给前端。
模型用英文推理、用中文作答，于是界面上出现「我来您I must执行。Actually.我这边」
这类中英夹杂的碎片 —— 用户报障的"智能体总是输出英文"就是它（实测 34 个会话命中）。
判据改成**白名单**（只认 `text-delta`）：上游以后新增的 chunk 类型默认不外发。

**影响**：`INTERFACES.md` §3 增加上述两个附加字段与优先级说明；
`frontend/src/layers/common/agent/service.ts::AgentTurnResult` 同步；
两种交付各有守卫（`tests/test_agent_runtime_wiring.py` 的 ④⑤⑥ 三节、
`frontend/tests/agent-panel.spec.ts`、`agent-contract-shape.spec.ts`）。

---

## 附：协作者上报的两处工程遗留（已修）

| 问题 | 处置 |
|---|---|
| `tools/check_source_hygiene.py` 的 `walk()` 传目录时**静默扫描 0 个文件** | 已修：目录会递归展开；**路径不存在直接报错**，不再当成空集 |
| `agent-runtime/bin/dsh.cmd` 含 CRLF 导致卫生检查失败 | 已修：卫生工具区分"要求 LF"与"要求 CRLF"（`.cmd`/`.bat` 用 LF 会让标签跳转解析异常） |

**第一条值得单独记**：协作者反馈"我第一次跑得到'0 个文本文件'，看着像通过其实是空跑"。
这是最坏的一类假阳性——**它比报错危险，因为它让人以为已经验证过了**。
修法不是"修 bug"，而是把语义改对：**空输入不是"通过"，是"没检查"**。

同类问题在早期版本也有（`tools/audit_source.py` 的 `--paths` 语义），所以这不是笔误，
而是一个模式：**凡是"筛选项为空"的地方，都要问一句"这该报错还是该通过"。**

---

## 关于协作者产出的复核结论（累计）

协作者在 t1–t5 中共交付 5 份文档。复核结论：

| 产出 | 结论 |
|---|---|
| H3 迁移校验和"必然互判漂移" | **误报**（`deploy.sh:146` 双接受；`install-manual-test.sh` 的库是新建后跑完即删） |
| Q1 塘口状态枚举冲突 | **真问题**，且指出了我自己文档里的错误 |
| Q10 `batch.close` 不可达 | **真问题**，最重要的一条 |
| `batch.update` 可裁剪 | **自己的错误**（与 Q10 同类），已自行修正 |
| `meta.resources` 应删除 | **采纳**（当前迭代论证质量显著提升） |
| Q8′ / Q13′ 两条派生冲突 | **采纳**，且其"不擅自扩大、报上来等裁决"的做法值得保留 |

**规律**：它的价值在"**机械核查发现的矛盾**"（枚举对不上、状态不可达、契约未定义），风险在"**从代码片段直接跳到后果**"（H3）。我在分工里已要求"结论必须落到可执行的判定路径上"，当前迭代它的自我修正说明这个要求起作用了。
