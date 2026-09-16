# Business Capability Registry — 权威清单 v1

> **本文件是渔芯AI水产养殖一体化系统业务能力的唯一权威来源。** 路由、请求/响应 schema、OpenAPI、前端字段元数据、权限码、DataScope 谓词、幂等策略、确认闸门、审计字段、Agent Tool、TS 类型——全部由本文件**派生**，不得在别处二次声明。
>
> 服从：`docs/ARCHITECTURE.md`（架构准则，§1 公理、§2 边界、§4 fail-closed、§5 Agent）与 `docs/INTERFACES.md`（接口契约，§2 元数据、§6 scope、§7 版本、§8 分页）。
> 冲突时以 `ARCHITECTURE.md` 为准；本文件与 `INTERFACES.md` 的冲突一律登记在第 7 节待裁决，不自行裁定。
>
> **证据约定**：凡引用早期版本结论，一律写 `早期版本：<相对路径>:<行号>`；凡本文件新增的设计判断，一律写 `【新增设计】`。本文件不含任何代码。
>
> 编制人：backend-recon ｜ 编制依据：`.local/recon-backend.md`（旧后端全量勘查，711 行，含全部行号证据）

---

## 0. 编制规则（先读这一节，它决定了后面所有表格怎么生成）

### 0.1 命名

| 项 | 规则 | 例子 |
|---|---|---|
| `name` | `<资源单数>.<动作>`，全小写，点分层级；跨域财务资源用 `<单据>.<动作>` 不带域前缀 | `pond.create`、`purchase_order.approve`、`payment.verify` |
| `required_permission` | 与 `name` **同名**（一个能力一个权限码，机械派生） | `pond.create` → 权限码 `pond.create` |
| `title` | 简体中文，直接作为前端按钮/菜单/确认卡标题 | `核验塘口` |
| `path` | `/api/v1/<资源复数>`；动作以 `/<id>/<动作>` 结尾 | `/api/v1/ponds/{pond_id}/verify` |

**权限码数量 = 能力数量**，不设 `xxx.manage` 这类宽泛权限。早期版本权限码有 3 套拼法（`production_service.py:44-52` 拼 `production.{resource}.{action}`、`sales_service.py:15-18` 直查码、`admin/routes.py:32-42` 查角色码），新系统统一为同名派生。
> **命名例外（裁决 §7 Q3 / `DECISIONS.md` Q3）：** 系统级权限码 **保留旧命名 `auth.user.manage` / `auth.role.manage`**，不套用上面的同名派生规则。理由：这两个码管的是**系统**（账号与角色），不是某个业务资源，`access.` 前缀会暗示它属于 access 域的资源；且早期版本种子数据与测试已用这两个码，改名等于为统一而统一地增加 churn。

### 0.2 `kind`

`read` | `create` | `update` | `delete` | `action`。
- 本版**不含 `delete`**：所有"删除"改为状态迁移（`cancelled` / `archived`），理由见 §6（早期版本物理删除必须连带 FK 拒绝逻辑，`production_store.py:288-293`、`warehouse_store.py:241-246`、`master_data_store.py:286-291`、`purchase_store.py:198-203`、`sales_store.py:153-156` 共 5 处同构 `except IntegrityError → DELETE_NOT_ALLOWED`，属于纯负债）。
- `action` = 状态迁移或业务动作（提交/核验/审批/取消/关闭/确认）。

### 0.3 `scope` 取值

| 写法 | 语义 | 生成的 SQL |
|---|---|---|
| `none` | 不受数据范围约束 | 无谓词 |
| `resource(area_id)` | 资源表自身的 `area_id` 列受控 | `AND <alias>.area_id IN (…)` |
| `resource(farm_id)` | 同上，列名换 `farm_id` | `AND <alias>.farm_id IN (…)` |
| `resource(pond_id)` | 同上，列名换 `pond_id` | `AND <alias>.pond_id IN (…)` |
| `owner(created_by)` | personal 范围 | `AND <alias>.created_by = :uid` |

**没有"跨表解析"语法。** 本文件初稿曾设计 `resource(area_id) via warehouse`（让本表无区域列的 `inventory_ledger` / `inventory_lots` 经 `warehouses` 关联解析），**已被裁决推翻**（§7 Q6 / `DECISIONS.md` Q6）：

> **裁决**：不支持跨表 JOIN 解析。改为在 `warehouses` / `inventory_lots` / `inventory_ledger` 上**补齐 `farm_id` / `area_id` 分租列**（与早期版本 `areas` / `ponds` / `pond_groups` 的做法一致）。

理由（`DECISIONS.md` Q6 原文三条）：① `via` 生成的相关子查询随数据量增长会显著劣化，而分租列可以走索引；② 补齐的列与既有 `organization_id` 形成一致的四级分租键（org / farm / area / pond），`ScopePolicy.resource(col)` 语法**保持不变**——少一个语法就少一个实现处处；③ 早期版本的 `ponds` / `areas` / `pond_groups` 就是这么做的，有生产验证。

代价：写入时需维护分租列，由 `ScopeResolver` 在 `create` 时一并写入（与 §0.7 规则 1 / Q7 的裁决天然一致）。

**施工影响**：`warehouses` / `inventory_lots` / `inventory_ledger` 三张表在建表时即带 `organization_id` / `farm_id` / `area_id` 三列；§1.6 各能力的 scope 一律写 `resource(area_id)`，**不再有任何 `via` 注解**。

无法解析为可执行谓词 → `DATA_SCOPE_UNRESOLVED`（403），**绝不返回空集**（`ARCHITECTURE.md:183`；早期版本 `common/security/data_scope.py:58` 返回 `"1=0"`，静默空数据）。

### 0.4 `risk` / `confirmation` / `agent_exposure`

- `risk`：`read`｜`normal`｜`high`。取值 = 内核 `Risk` 枚举（`kernel/capability.py`，**唯一登记处**）。
  - `read` — **只读**。内核在注册期与 `method` 绑定校验：`Risk.READ` ⟺ `method == GET`，
    且只读能力**不允许** `confirmation=always`（读操作没有可确认的副作用）。
  - `normal` — 普通写。
  - `high` — 高危写。判定标准只有三条：**① 不可逆**（核验后写账本/生成财务数据）；**② 涉及金额**；**③ 涉及权限或身份**。
  > **本文档的修订（t6）**：原句写的是「`risk`：`normal`｜`high`」，**没有 `read`**，
  > 于是全文 24 条 `kind=read` 的能力 `risk` 列写的是 `normal`，而运行时它们全部标 `Risk.READ`。
  > 这不是两种情况都说得通的小事：内核**已经在注册期强制** `Risk.READ` ⟺ `GET`，
  > 也就是说「只读 = normal」这个写法在运行时**表达不出来**；而文档同一行的 `kind` 列
  > 已经写着 `read`——同一张表里 `kind=read` 配 `risk=normal` 本身就是自相矛盾的。
  > 处置：**文档补 `read` 档，并把 24 条只读能力的 `risk` 列从 `normal` 改为 `read`**（与运行时一致）；
  > 内核 `Risk` 不改——它已经是对的，而且它是这条判据的唯一实现处。
  >
  > 顺带说明**为什么不是反过来改运行时的 `Risk.READ`**：`effective_confirmation`、
  > `refuses_agent`、`to_meta()` 的 `risk` 字段、以及 `authorized()` 的前置判断都按它分支；
  > 合并进 `normal` 会丢掉「读操作不需要确认闸门」这条**已经在强制校验**的事实。
- `confirmation`：**取值只有两个 —— `never` / `always`。**
  > **裁决（§7 Q4 / `DECISIONS.md` Q4.1）**：**删除 `by_key`。** 理由原文："`by_key` 需要一个阈值体系（金额？不可逆性？状态？），而阈值本身需要产品决策。**没有阈值的 `by_key` 就是 `always`**——留一个语义未定义的枚举值比删掉它更危险（实现者会各自猜）。"
  >
  > **初稿遗留的 `by_key` 设计（保留为历史记录，勿实现）**：原设计为"仅当参数命中风险阈值时要求确认令牌"，并留了一个自己未解决的问题——"阈值由谁定义（配置项？capability 声明内的 `by_key={"amount_gte": 10000}`？）"。**这个悬空问题正是该枚举被删除的原因。**
  >
  > **回写结果**：初稿中标 `by_key` 的 5 条能力（`feeding.create` / `issue.create` / `payment.create` / `delivery.create` / `sales_receipt.create`）**全部改为 `always`**——它们本来就是"要确认"的那一批，`by_key` 只是给它们挂了一个无法实现的开关。
  - `never` — Agent 可直接执行
  - `always` — Agent 每次调用都必须拿到一次性确认令牌（`INTERFACES.md:162-175` 的 `confirmation_required` 响应）
  - **语义边界（必须写进实现）**：`confirmation` **只约束 Agent 入口**。页面入口的二次确认是前端 UX 决定，**不受此声明管辖**。
- **`risk` 与 `confirmation` 的一致性约束**【新增设计，内核注册期强制】：
  ```
  risk == "high"  ∧  kind == "action"  ∧  agent_exposure != "human_only"   ⇒   confirmation == "always"
  ```
  即：**除 `human_only` 之外**，`risk=high` 的 `action` 类能力必须为 `always`。
  这条约束来自一次真实的自相矛盾：初稿把 `cost.entry.confirm` 标成 `risk=high` 却标 `confirmation=never`（§7 Q13 / `DECISIONS.md` Q13 的"顺带修正"）。**内核应在注册期校验并拒绝启动**——它可机械检查。
  **为什么排除 `human_only`**：`confirmation` 只管 Agent 入口（见上），而 `human_only` 的能力根本不会被 Agent 执行，给它标确认闸门没有意义。全文恰有 **1** 条 `risk=high ∧ human_only ∧ confirmation=never` 的能力：`auth.password.change`（它落在 §5.2 的 human_only 清单内——这是**正确的**，不是遗漏）。
> **2026-09-14 复核更正**：本条原先写“恰有 5 条”，另外 4 条是 `auth.login`、`access.user.status`、`access.user.grants`、`access.role.permissions`。实测：`auth.login` **不在注册表内**（Flask 固定路由），另 3 条已按 `docs/DECISIONS.md` 第 9 条**改为 `exposed` + `confirmation=always`**（管控从“Agent 完全不可见”换成“RBAC + 服务层 super_admin 校验 + 服务端 HITL”）。
  **回写结果（2026-09-14 按运行时重算）**：全文 `always` **38** 条、`never` **71** 条（合计 109 条，即当前运行时注册表规模），**无一条违反此约束**。上一版写的 23 / 46 是 69 条规模时的读数，已过期。
> 复核口径：`confirmation` 的**生效值**是 `Capability.effective_confirmation`（`kernel/capability.py:360`）——未显式声明时由 `risk` 派生（`risk=HIGH` 的写操作 ⇒ `always`），所以**不要**直接读 `capability.confirmation`（未声明的那些是 `None`）。
- `agent_exposure`：
  - `exposed` — 注册为 Harness Tool
  - `hidden` — 不注册（默认值，`ARCHITECTURE.md:198`）
  - `human_only` — 注册但**拒绝执行**，返回 `HUMAN_ONLY`（409）（`INTERFACES.md:37`）
  
  **`hidden` ≠ `human_only`**：前者是"不可见"，后者是"可见但禁止"。这条区分必须在 Gateway 代码里体现。

### 0.5 `idempotent`

**取值只有 `true` / `false`。`true` 表示强制携带 `Idempotency-Key`。**

> **裁决（§7 Q4 / `DECISIONS.md` Q4.2）**：`idempotent=true` 表示**强制携带** `Idempotency-Key`。
> 理由原文："早期版本前端 `client.ts` 已经对**所有**写方法自动生成 `Idempotency-Key`，所以'强制'不会给前端增加任何工作量。而'允许但不强制'是一个**无法测试的中间态**——服务端无法区分'客户端没传'和'客户端传了但语义不同'。"
>
> **初稿遗留设计（保留为历史记录）**：原写"`true` 的能力**允许**携带；所有 create 与所有写 action 强制要求该头缺失时也接受，但缺失即视为不幂等"——这是一个语义自相矛盾的表述（既"强制要求"又"缺失时也接受"），已被推翻。

**强制范围（由 `Capability` 声明机械派生，不手写）**：
```
requires_idempotency_key  ⟺  (kind == "create")  ∨  (confirmation == "always")  ∨  (creates_record == True)
```
三段含义：
1. 所有 `kind=create` 的能力 —— 每次调用都新建一行，天然需要幂等；
2. 所有 `confirmation=always` 的能力 —— 确认令牌本身是一次性的，重放必须被幂等层挡住（`ARCHITECTURE.md:186-194` 的令牌六元组含 `params_hash`，与幂等键互补）；
3. `creates_record=True` 的 `action` —— 唯一一例是 `pond_status_change.request`（它是 `action`，但实际新建一行申请记录）。

**服务端行为**：缺失 `Idempotency-Key` → `VALIDATION_ERROR`(400)，`data.field="Idempotency-Key"`。
**存储**：`idempotency_keys` 表只存 `key_hash` / `request_hash` / `status` / `resource_type` / `resource_id` / `result_code` / `expires_at`（`ARCHITECTURE.md:165-170`），**预留与业务写入在同一 `tx` 内**——早期版本"三次独立事务"（`早期版本：common/governance/idempotency.py:61/96/102`）的高危形态不再存在。

### 0.6 `audit`

| 值 | 记录内容 | 对应早期版本 |
|---|---|---|
| `before_after` | 动作字段（同 `summary`）**+** `before_json` + `after_json` | 旧 `common/audit/audit_logger.py:112-141` 全 20 字段 |
| `summary` | 动作字段：`capability` / `domain` / `permission` / `object_type` / `object_id` / `object_ref` / `result` / `reason` / `ip_address` / actor，**无 diff** | 旧 `_audit()` 轻量包装 |

**只有这两档，没有「不写审计」。** 审计行由 `runner._write_audit()` **无条件**写入——
成功路径（`kernel/runner.py:266`）与幂等路径（`:368`）各一次，两条路径都不判 `AuditPolicy`。
策略只决定**要不要另外记 before/after diff**。

> **本文档的修订（t6）**：本表原有一行 `| none | 不写审计 | 旧读操作 |`，
> 且 §1 的 26 条只读能力 `audit` 列写的是 `none`。**那个行为在运行时不存在**：
> 实测库里那些能力每一行都留下了审计（`before_json` / `after_json` 均为 NULL），
> 即它们实际执行的是本表的 `summary`。原表误导的后果不止是措辞——
> 读它的人会以为「只读操作不落审计」，进而在别处补一遍审计写入，或把审计缺失当缺陷排查。
> 内核那边同一个错误以 `AuditPolicy.none()` 的形式存在（一个叫 `none` 的构造器
> 实际做的是 `summary`），**两处同时改**：内核删掉 `none()`、正名为 `summary()`；
> 本文档删掉该行、并把 26 条只读能力的 `audit` 列改为 `summary`。
>
> **为什么删而不是留同义别名**：留别名等于把这个会撒谎的名字永久留在公共契约上，
> 而它当时只剩两个调用点。判据与本项目的一贯口径一致——**能表达成一处的事实，不留第二处**。
>
> 另注：**不要把它与 `ScopePolicy.none()` 混为一谈**（§0.3 的 `none` 是数据范围档位，
> 语义是「不受数据范围约束」，与审计无关）。

**全系统审计字段固定**（对齐 `INTERFACES.md` 未展开的旧契约，`004_enterprise_governance_foundation.sql:10-25`）：`user_id, action_code(=capability name), object_type, object_id, object_ref, result, reason, request_id, module_code, action_code, before_json, after_json, changed_fields_json, actor_name_snapshot, actor_role_snapshot, ip_address, created_at`。【新增设计】删除早期版本的 `correlation_id` / `retention_class` / `related_work_item_id` 三个字段——早期版本全仓 **0 处消费**（`retention_class` 从未被读取，`correlation_id` 只在写入侧出现）。

### 0.7 全局请求规则（不在每个字段表里重复）

1. **`create` 不接收也不返回** `organization_id` / `farm_id` / `area_id`——由 `ScopeResolver` 从引用对象解析并写入。
   **早期版本反例**：这三个字段在 payload 里（`master_data_service.py:15-18`、`production_service.py:18`、`warehouse_service.py:14`），且必须靠 `_scope_defaults()`（`production_store.py:100`）/ `_scoped()`（`warehouse_store.py:74-97`、`purchase_store.py:57-69`）/ `master_data_store.py:96-115` 三套实现回查补全。**这是本次最大的一处契约变更。**
   > **裁决（§7 Q7 / `DECISIONS.md` Q7）：确认变更**，理由是"如果客户端能指定 `area_id`，那'用户只能写自己区域的数据'就退化成'用户自报家门'——早期版本正是这样"。
   > **前端形态**：字段元数据里这三个字段标记为 **`readonly: true`**，`DynamicForm` 渲染成**禁用输入框**并在旁边显示服务端解析出的值。用户因此**看得见**自己的数据范围落在哪里——这是 DataScope 的可见证明点。
   > **施工影响**：`meta.capabilities` 的 `fields[]` 中这三个字段带 `readonly: true`；服务端**拒绝**请求体里出现它们（`FIELD_INVALID`，`data.field` 给字段名），不做静默忽略。
2. **`create` / `update` 永不接收** `status` / `row_version` / `version` / `allowed_actions` / `created_by` / `updated_by` / `created_at` / `updated_at`（早期版本：`production_service.py:33` 的 `RESERVED`、`master_data_service.py:10` 的 `RESERVED_FIELDS`）。
3. **所有 `update` 与 `action` 必带** `expected_version: integer, required`；不匹配 → `CONFLICT`(409) + `data.current_version`（`INTERFACES.md:279`）。
4. **所有响应带** `version` 与 `allowed_actions`（服务端算）（`INTERFACES.md:280`；旧 `lifecycle` 的 `allowed_actions` 机制、`frontend/src/layers/common/api/lifecycle.models.ts:1-7` 已固化 15 个动作词）。
5. **分页统一** `?page=&page_size=`（上限 100，超限 400），响应 `{items,page,page_size,total,has_next}`（`INTERFACES.md:284-291`；早期版本 25 处重复分页 SQL、31 处重复 `has_next`，见 `.local/recon-backend.md` §2.5）。
6. **附件字段全部移除**：早期版本 `evidence_attachment_ids` 出现在生产/仓储/采购/销售/成本**每一个** FIELDS 常量中（`production_service.py:20`、`warehouse_service.py:18`、`purchase_service.py:13`、`sales_service.py:7-10`、`cost_enterprise_validation.py:13`）。本版 P0 不做附件（§6），连带影响 1 条不变量降级（§4 #15、Q5）。

---

## 1. 能力清单（权威，共 **113** 条）

> **计数口径**：§1.1–§1.9 的能力表格行 = **69** 行，去重后 **69** 个唯一 `name`。
> 演进：初稿 64 条 → Q9/Q10 恢复 `harvest.list/create/verify`（+3）→ Q2 恢复 `pond_status_change.request/verify`（+2）= **69**（见 §7 各条裁决与 `DECISIONS.md`）。
> §1.11 的裁剪清单与 §1.12 的成本精简**均已被裁决否决**，两节保留为"已评估、不采纳"的历史记录。**本文档的权威清单就是 69 条，零裁剪。**

读法：一行一个能力。`—` 表示 `required_permission` 为"无需权限（仅需登录）"。

> **`audit` 列的取值**：`summary` / `before_after` 是**经执行器的能力**的审计粒度
> （由 `CapabilityRunner` 写 `audit_logs`）。本表另有 **4 条固定路由**（`auth.login`、
> `auth.logout`、`auth.me`、`meta.capabilities`）—— 它们由 `web/app.py` 直接提供、
> **不经 `CapabilityRunner`**，因此**不产生审计行**。它们的 `audit` 列一律写
> **`—（固定路由，不经执行器）`**：这是**唯一**一句说明，§1 表格不再重复写"其实没有审计"。
> 对账工具用已有的 **[A2] 固定路由分类**（判据是真实 `url_map`，不是名字白名单）识别它们，
> 并对这四条**不做** `audit` 逐能力对账 —— 不给"固定路由"另造一套判据。

### 1.1 identity — 4 条

| name | method | path | title | kind | required_permission | scope | risk | confirmation | agent_exposure | idempotent | audit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `auth.login` | POST | `/api/v1/auth/login` | 登录 | action | — | none | high | never | n/a | false | —（固定路由，不经执行器） |
| `auth.logout` | POST | `/api/v1/auth/logout` | 退出登录 | action | — | none | normal | never | n/a | false | —（固定路由，不经执行器） |
| `auth.me` | GET | `/api/v1/auth/me` | 查询当前账号 | read | — | none | `read` | never | n/a | false | —（固定路由，不经执行器） |
| `auth.password.change` | POST | `/api/v1/auth/password/change` | 修改密码 | action | — | owner(user_id) | high | never | human_only | false | summary |

依据：旧 `product/auth/routes.py:74,98,109,120`；会话与限流机制保留（旧 `auth_service.py:21-30` 的"限流→定位→状态→密码→建会话"链路，`.local/recon-backend.md` §4.1）。`auth.csrf` **不计入能力清单**——它是中间件的附属端点，不是业务能力（`INTERFACES.md:258` 已把 CSRF 改为全局前置校验）。

### 1.2 access — 9 条

| name | method | path | title | kind | required_permission | scope | risk | confirmation | agent_exposure | idempotent | audit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `access.user.list` | GET | `/api/v1/admin/users` | 账号列表 | read | `auth.user.manage` | none | `read` | never | exposed | false | `summary` |
| `access.user.create` | POST | `/api/v1/admin/users` | 新建账号 | create | `auth.user.manage` | none | high | **always** | exposed | **true** | before_after |
| `access.user.status` | POST | `/api/v1/admin/users/{user_id}/status` | 启用/停用账号 | action | `auth.user.manage` | none | high | **always** | exposed | false | before_after |
| `access.user.grants` | PUT | `/api/v1/admin/users/{user_id}/grants` | 调整角色与数据范围 | action | `auth.user.manage` | none | high | **always** | exposed | false | before_after |
| `access.role.permissions` | PUT | `/api/v1/admin/roles/{role_id}/permissions` | 调整角色权限 | action | `auth.role.manage` | none | high | **always** | exposed | false | before_after |
| `access_user.get` | GET | `/api/v1/admin/users/{user_id}` | 账号详情 | read | `auth.user.manage` | none | `read` | never | exposed | false | `summary` |
| `access.role.list` | GET | `/api/v1/admin/roles` | 角色列表 | read | `auth.user.manage` | none | `read` | never | exposed | false | `summary` |
| `access_role.get` | GET | `/api/v1/admin/roles/{role_id}` | 角色详情 | read | `auth.user.manage` | none | `read` | never | exposed | false | `summary` |
| `access.scope.list` | GET | `/api/v1/admin/scopes` | 数据范围列表 | read | `auth.user.manage` | none | `read` | never | exposed | false | `summary` |

依据：旧 `product/admin/routes.py:114,136,220,174`（对应 create/status/grants/permissions），`admin/routes.py:124`（list）。权限码沿用旧命名 `auth.user.manage` / `auth.role.manage`（不套用 §0.1 的同名规则——它们是**系统级**权限，不是业务能力码；登记为 Q3 一致性例外）。
`access.user.create` 的 `idempotent=true` 强制：早期版本靠 `uq_users_phone` 唯一键兜底并翻译成 `PHONE_EXISTS`（`mysql_store.py:243-246`、`auth_admin_store.py:48-51`）。

### 1.3 audit — 1 条

| name | method | path | title | kind | required_permission | scope | risk | confirmation | agent_exposure | idempotent | audit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `audit.log.list` | GET | `/api/v1/audit-logs` | 操作日志 | read | `audit.view` | none | `read` | never | exposed | false | `summary` |

依据：旧 `product/admin/routes.py:59-80`。**`agent_exposure=hidden` 是刻意的**：审计日志含账号/IP/权限变更细节，Agent 无需读取；但它不是 `human_only`（不在 `ARCHITECTURE.md:242` 允许的 human_only 方向内）。
早期版本的日志查询支持 9 个过滤维度（`admin/routes.py:64-75`：user_id / module_code / action_code / object_type / result / created_from / created_to / request_id），本版保留其中 5 个：`user_id` / `module_code`(=capability 的域) / `action_code`(=capability name) / `created_from` / `created_to`。

### 1.4 master_data — 28 条

| name | method | path | title | kind | required_permission | scope | risk | confirmation | agent_exposure | idempotent | audit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `meta.capabilities` | GET | `/api/v1/meta/capabilities` | 能力、字段与状态元数据 | read | — | none | `read` | never | n/a | false | —（固定路由，不经执行器） |
| `farm.list` | GET | `/api/v1/farms` | 基地列表 | read | `farm.view` | resource(farm_id) | `read` | never | exposed | false | `summary` |
| `area.list` | GET | `/api/v1/areas` | 区域列表 | read | `area.view` | resource(farm_id) | `read` | never | exposed | false | `summary` |
| `area.get` | GET | `/api/v1/areas/{area_id}` | 区域详情 | read | `area.view` | resource(farm_id) | `read` | never | exposed | false | `summary` |
| `area.create` | POST | `/api/v1/areas` | 新建区域 | create | `area.create` | resource(farm_id) | normal | never | exposed | **true** | before_after |
| `area.update` | PATCH | `/api/v1/areas/{area_id}` | 编辑区域 | update | `area.update` | resource(farm_id) | normal | never | exposed | false | before_after |
| `area.archive` | POST | `/api/v1/areas/{area_id}/archive` | 停用区域 | action | `area.archive` | resource(farm_id) | high | **always** | exposed | **true** | before_after |
| `pond.list` | GET | `/api/v1/ponds` | 塘口列表 | read | `pond.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `pond.get` | GET | `/api/v1/ponds/{pond_id}` | 塘口详情 | read | `pond.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `pond.create` | POST | `/api/v1/ponds` | 新建塘口 | create | `pond.create` | resource(area_id) | normal | never | exposed | **true** | before_after |
| `pond.update` | PATCH | `/api/v1/ponds/{pond_id}` | 编辑塘口 | update | `pond.update` | resource(area_id) | normal | **always** | exposed | false | before_after |
| `pond.submit` | POST | `/api/v1/ponds/{pond_id}/submit` | 提交塘口核验 | action | `pond.update` | resource(area_id) | normal | never | exposed | false | summary |
| `pond.verify` | POST | `/api/v1/ponds/{pond_id}/verify` | 核验塘口 | action | `pond.verify` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `pond.archive` | POST | `/api/v1/ponds/{pond_id}/archive` | 归档塘口 | action | `pond.archive` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `pond_status_change.request` | POST | `/api/v1/ponds/{pond_id}/status-changes` | 申请变更塘口状态 | action | `pond.update` | resource(area_id) | normal | never | exposed | **true** | before_after |
| `pond_status_change.verify` | POST | `/api/v1/ponds/{pond_id}/status-changes/{request_id}/verify` | 核验塘口状态变更 | action | `pond.verify` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `material.list` | GET | `/api/v1/materials` | 物料列表 | read | `material.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `material.get` | GET | `/api/v1/materials/{material_id}` | 物料详情 | read | `material.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `material.create` | POST | `/api/v1/materials` | 新建物料 | create | `material.create` | resource(area_id) | normal | never | exposed | **true** | before_after |
| `material.update` | PATCH | `/api/v1/materials/{material_id}` | 编辑物料 | update | `material.update` | resource(area_id) | normal | never | exposed | false | before_after |
| `material.archive` | POST | `/api/v1/materials/{material_id}/archive` | 停用物料 | action | `material.archive` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `partner.list` | GET | `/api/v1/partners` | 往来单位列表 | read | `partner.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `partner.get` | GET | `/api/v1/partners/{partner_id}` | 往来单位详情 | read | `partner.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `partner.create` | POST | `/api/v1/partners` | 新建往来单位 | create | `partner.create` | resource(area_id) | normal | never | exposed | **true** | before_after |
| `farm.get` | GET | `/api/v1/farms/{farm_id}` | 基地详情 | read | `farm.view` | resource(farm_id) | `read` | never | exposed | false | `summary` |
| `farm.create` | POST | `/api/v1/farms` | 新建基地 | create | `farm.create` | none | normal | never | exposed | **true** | before_after |
| `material.submit` | POST | `/api/v1/materials/{material_id}/submit` | 提交物料核验 | action | `material.submit` | resource(area_id) | normal | never | exposed | **true** | `summary` |
| `material.verify` | POST | `/api/v1/materials/{material_id}/verify` | 核验物料 | action | `material.verify` | resource(area_id) | high | **always** | exposed | **true** | before_after |

依据与裁剪说明：
- **本次新增 `pond.archive` 与 4 条详情读**（t3 补登记）：`pond.archive` 是**能力先有、文档后补**的
  反向漂移之一（评审结论：代码里的 `pond.get` 与 `pond.archive` 都是合理能力，**补进 registry 而不是删代码**）。
  它的字段逐项取自代码声明：`kind=action`、`required_permission=pond.archive`、`scope=resource(area_id)`、
  `risk=high`（按 §0.4 的一致性约束 ⇒ `confirmation=always`，`idempotent=true` 亦由 §0.5 派生）、`audit=before_after`。
- **4 条详情读**（`pond.get` / `area.get` / `material.get` / `partner.get`）：
  列表读已在清单里，但前端点开一行需要完整记录（`available_transitions` 等派生字段只在详情里给），
  早期版本每个资源也都有 `GET /<资源>/{id}`。四条的形状与各自的列表读一致：`kind=read`、
  `required_permission=<资源>.view`、`scope` 与本资源在表上真实存在的分租列一致。
  > 这四条是**代码先有、文档后补**（反向漂移），由 t3 按 §1 的列格式补回来；
  > 在此之前它们只存在于 `domains/master_data/capabilities.py`，对账工具会报成缺口。
- **`meta.*` 两行的登记形态已裁决（对账工具新增 [A2] 分类）**：判据是**实际 Flask 路由表**，
  而不是名字或人工白名单——声明的 `(method, path)` 能在 `app.url_map` 里找到，就归
  「**由固定路由提供（非能力）**」这一类，不再计入漏实现；找不到的仍计入 [A] 缺口。
  * `meta.capabilities` → **由固定路由提供**（`web/app.py::_register_meta_routes` 的
    `GET /api/v1/meta/capabilities`），对账工具现在稳定归入 [A2]；
  * `meta.resources` → **本文件里一条悬空行**：`GET /api/v1/meta/resources` 全仓无定义，
    `DECISIONS.md` Q14 已裁决删除该独立路由（`INTERFACES.md:49` 只定义了一个端点，
    前端要的 `columns`/`status_dict` 在 `/meta/capabilities` 响应的 `data.resources` 段里）。
    该行**已按 Q14 删除**（§1.4 现为 24 条能力行、§1.11 合计 84）——删它是"改被误登记的对象"，
    而不是"让工具学会忽略它"：工具继续按 url_map 判定，不需要为它留任何白名单。
  * 因此 `meta.capabilities` 这一行**保留**在 §1.4（它是接口契约），只是登记形态不同；
    §1.11 的条数口径与 §1.4 的行数已在 t18 后同步；当前迭代再补 `farm.list` 后为 **86/24**。
- `meta.capabilities` 是 `INTERFACES.md:49` 明文要求的端点（**本项目最重要的一条接口**）；
  `meta.resources` **不是**端点（`INTERFACES.md` 从未定义它，Q14 已删该独立路由）——该行已从本表删除。
- 早期版本 master_data 有 **8 个资源**（`master_data_service.py:9`：farms, areas, pond-groups, ponds, materials, suppliers, customers, settings），本版裁到 **可写资源 + 只读资源** 两组：
  > **t18 之后的分组**：可写 = `pond` / `area` / `material` / `partner`；
  > 只读 = 无（`area` 与 `material` 从只读升级为可维护，见本节上方的 Q21 说明）。
  > 原文（t18 之前）写的是「**3 个可写资源 + 2 个只读**」，此处保留原文以便对照。

  原文：本版裁到 **3 个可写资源 + 2 个只读**：
  - **`farms` 保留只读 `farm.list`，不提供写能力**：基地在企业内极少变动；列表用于成本登记的多态归属候选，写入仍由迁移/种子负责。
  - **`pond-groups` 砍掉**：旧字段集只有 `code/name/description`（`master_data_service.py:14`），是 ponds 与 areas 之间的纯中间层；塘口直接挂区域。
  - **`settings` 砍掉**：旧 `business_settings`（`master_data_service.py:19`），`group_code/value_text` 是配置表，与业务闭环无关。
  - **`suppliers` + `customers` 合并为 `partner`**：早期版本本来就是同一张表 `business_partners` + `partner_type` 列（`master_data_store.py:22-23` 的 `SPECS` 映射）。合并是回归旧 schema 的真实形态，不是新增抽象。
  - ~~`materials` 保留**只读**：物料是投喂/出入库的引用对象，由种子或导入建立；`material.verify` 砍掉（见 §6）。~~
    > **该裁决已被推翻（t18 / `DECISIONS.md` Q21）**，理由三条都经核实：
    > ① 本版**没有可用的种子/导入管线**（`database/seeds/` 下没有物料种子，
    > 003 迁移只是 `INSERT ... SELECT` 了 2 条演示饲料；`tools/` 下也没有导入器）；
    > ② 用户报「缺少」（`ROADMAP.md` §4.2 的 P0 第二条）；
    > ③ 原裁决真正要保的意图是「**物料的业务状态不该被随意推动**」，而
    > `material.verify` **至今仍然砍掉**——所以「能新增物料、能改台账字段」
    > 不削弱那条意图。
    > **这是本表里唯一一处被推翻的条目，不是静默加行。**
  - **`area` 也一并改为可维护（t18）**：原口径是「区域由种子/迁移建立」——
    经核实同样不成立（`database/seeds/` 下没有区域种子，003 迁移只种了两条演示
    区域），而「建不了区域」恰好是用户报「缺少」的原始症状。
    三条能力（create/update/archive）与物料的形状一致。
- `pond.verify` 的 `risk=high`：早期版本核验后塘口档案只读（`lifecycle.py:51,76-78`），且核验是放苗前置条件；按 §0.4 的 `risk=high ∧ action ⇒ always` 约束，`confirmation` 由初稿的 `never` 修正为 **`always`**。
#### §1.4 的 t18 增补（6 条）

**`area.create` / `area.update` / `area.archive`** 与
**`material.create` / `material.update` / `material.archive`**。
六条的能力形状取自已跑通的 `partner.create`（写路径）与 `pond.archive`（停用），逐项：

| 项 | 取值与依据 |
|---|---|
| `risk` | create / update = `normal`；archive = **`high`** —— §0.4 的判据只有三条，这两条命中第①条「不可逆」：本版**没有取消归档的能力**（`archived` 是终态，状态机没有任何出边），它同时把「引用它的业务」锁掉（新建塘口选不到那个区域）。命中即 `high`；要放宽到 `normal` 必须给出「它可逆」的理由，而它不可逆 |
| `confirmation` | 由 §0.4 的机械约束派生：`high ∧ action ⇒ always`，故两条 archive 是 `always`，四条 create/update 是 `never` |
| `idempotent` | 由 §0.5 的机械约束派生：`kind=create`（2 条）∪ `confirmation=always`（2 条）⇒ 四条 `true`，两条 `update` `false` |
| `scope` | 区域一律 `resource(farm_id)`（`areas` **没有 `area_id` 列**，与 `area.list` / `area.get` 同一口径）；物料一律 `resource(area_id)` |
| `audit` | 全部 `before_after`（「改了什么」本身是审理要看的） |
| `agent_exposure` | `exposed`（README 的裁决：普通主数据维护不额外阉割） |

**「停用」的语义**：本版全系统 `delete` 能力 = **0**（§0.2），所以停用 = **归档**
（`status -> 'archived'` + `row_version` 前进一格），**不是物理删除**。归档之后：

| | 归档后 | 由谁保证 |
|---|---|---|
| 仍可读（`?status=archived`） | ✅ | `list_rows` 只在「未显式指定 status」时排除 archived |
| 出现在**默认列表**里 | ❌ —— 而 `DynamicForm` 的 `ref` 下拉请求的正是默认列表（不带 `status` 参数），所以它**不会再被选为候选** | `resources_read._DEFAULT_EXCLUDED_STATUS`（**一处实现**） |
| 被 `*.update` 改 | ❌（终态，状态机不给 `edit`） | 服务里的 `require_action` + 内核 `StatusAllowsEdit` |
| 被再次停用 | ❌ | 同上（`archive` 在 `archived` 下不允许） |
| 历史业务数据引用它 | ✅ 不受影响（外键 RESTRICT，行还在） | 003 / 006 的真实外键 |
| 新业务引用它 | ❌（下拉选不到；手工提交 id 会撞服务端的范围校验与 `ReferencedStatus`） | 服务的第三层防御 |

- **塘口状态变更：恢复两步审批**（`pond_status_change.request` + `pond_status_change.verify`）。
  > **裁决（§7 Q2 / `DECISIONS.md` Q2）：保留两步**，恢复唯一键 `uq_pond_status_active_request`（同一塘口只有一个待核验申请）。
  > 理由原文三条：① 需求明确写了"**审核**"属于高风险业务；② 塘口状态变更会解锁/锁死一批业务能力（`build → stocked` 才能建批次、`farming → rest` 会停掉投喂），属实质性的业务状态迁移；③ "同一塘口只能有一个待核验申请"是个**很好的不变量示范**——它证明系统能表达"集合上唯一"而不只是"字段非空"。
  >
  > **初稿设计（保留为历史记录，已被推翻）**：初稿把两步审批合并进 `pond.update` 的 `pond_status` 字段，并把"唯一待审"约束一并删除；理由是"减少 2 个能力与 1 张表"。该简化低估了业务状态迁移的实质影响。
  >
  > **施工影响**：需要 `pond_status_change_requests` 表（`早期版本：019_enterprise_authorization_and_pond_status.sql`），唯一键 `uq_pond_status_active_request`；状态机见 §3.1-B；不变量见 §4 #20/#21。
  > **注意**：此后 `pond.update` **不再接受** `pond_status` 字段（它已成为只读派生列），塘口业务状态只能经两步审批变更。

### 1.5 production — 15 条

| name | method | path | title | kind | required_permission | scope | risk | confirmation | agent_exposure | idempotent | audit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `batch.list` | GET | `/api/v1/batches` | 批次列表 | read | `batch.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `batch.create` | POST | `/api/v1/batches` | 建档放苗 | create | `batch.create` | resource(area_id) | normal | never | exposed | **true** | before_after |
| `batch.update` | PATCH | `/api/v1/batches/{batch_id}` | 编辑批次 | update | `batch.update` | resource(area_id) | normal | **always** | exposed | false | before_after |
| `batch.submit` | POST | `/api/v1/batches/{batch_id}/submit` | 提交批次核验 | action | `batch.update` | resource(area_id) | normal | never | exposed | false | summary |
| `batch.verify` | POST | `/api/v1/batches/{batch_id}/verify` | 核验批次（形成初始存塘） | action | `batch.verify` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `batch.close` | POST | `/api/v1/batches/{batch_id}/close` | 关闭批次 | action | `batch.verify` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `feeding.list` | GET | `/api/v1/feedings` | 投喂记录列表 | read | `feeding.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `feeding.create` | POST | `/api/v1/feedings` | 登记投喂 | create | `feeding.create` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `feeding.verify` | POST | `/api/v1/feedings/{feeding_id}/verify` | 核验投喂（扣库存 + 计成本） | action | `feeding.verify` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `harvest.list` | GET | `/api/v1/harvests` | 出塘记录列表 | read | `harvest.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `harvest.create` | POST | `/api/v1/harvests` | 登记出塘 | create | `harvest.create` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `harvest.verify` | POST | `/api/v1/harvests/{harvest_id}/verify` | 核验出塘（减存塘） | action | `harvest.verify` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `batch.get` | GET | `/api/v1/batches/{batch_id}` | 批次详情 | read | `batch.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `feeding.get` | GET | `/api/v1/feedings/{feeding_id}` | 投喂记录详情 | read | `feeding.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `harvest.get` | GET | `/api/v1/harvests/{harvest_id}` | 出塘记录详情 | read | `harvest.view` | resource(area_id) | `read` | never | exposed | false | `summary` |

依据：旧 `production_service.py:11-14` 的 9 个资源（batches, samplings, transfers, losses, harvests, feed-plans, feed-tasks, feed-logs, daily-operations）。
- 保留 **batches**、**feed-logs（重命名为 feeding）**、**harvests**。`ARCHITECTURE.md:99` 明确新系统 production 域 = "养殖批次、投喂、巡塘"。
- **恢复 `harvests`（出塘事实源）**：
  > **裁决（§7 Q9 + Q10 / `DECISIONS.md` Q9、Q10）：恢复 `harvests`（3 条能力）。**
  >
  > `DECISIONS.md` Q10 记录了恢复的**决定性理由**（这是本清单编制过程中最重要的一次发现）：
  > "砍掉出塘后，**没有任何能力能减少塘内存塘**，`batch.close` 的'存塘必须为 0'不变量永远不为真——一个**永远不可达的能力**比一个缺失的能力更糟，因为它在清单上看起来是有的。"
  >
  > `DECISIONS.md` Q9 补充了业务理由：出塘是养殖业务的**核心事实源**（成本、存塘、交付三者都要它）。
  >
  > **初稿设计（保留为历史记录，已被推翻）**：初稿把 `transfers / losses / harvests` 一并砍掉，理由是"三者都是改变存塘量的非常规事件"；并在 §3.2 的差异表里把 `batch.close` 标为"P0 不可达"。**该简化制造了一个永远不可达的能力**，被 Q10 检出。
  >
  > **施工影响**：
  > - `harvests` 写入 `batch_stock_records`（塘内存塘账本），`harvest.verify` 时校验 `harvest.quantity <= 当前存塘`（复用旧 `production_store.py:273-280` 已验证的 `SELECT ... FOR UPDATE` 写法 → 不变量 §4 #2）。
  > - `batch` 生命周期因此完整可达：`stocked → farming → pending_settlement → closed`（§3.2-B）。
  > - sales 域的"交付数量与出塘事实一致"不变量（§4 #6）**恢复**，比较对象就是 `harvest`。
  > - `issue-requests` **仍不恢复**（Q9 明确："`feeding.verify` 直接判库存（复用'不得负库存'不变量）"），因此不变量 §4 #17 保持降级。
- 砍掉 **transfers / losses**：见 §6（调拨、损耗）。两者与出塘不同——它们不承担"让 `batch.close` 可达"的职责（损耗会减存塘，但出塘是正常业务的唯一减量路径，恢复出塘即已解决可达性）。
- 砍掉 **feed-plans / feed-tasks**：早期版本是"计划→任务→记录"三段（`production_relations.py:24-40` 校验三者跨表一致），实测把 1 次投喂拆成 3 张单据 + 3 层关联校验；本版合并为单一 `feeding`。
- 砍掉 **daily-operations**：早期版本用 `daily_operation_rules.py`(90 行) 做类型化枚举（`production_service.py:16` 的 `HIGH_RISK` 与 `:133-136`）。
- **`feeding.create` 的 `risk=high`**：它是"一次操作同时扣库存 + 计成本"的入口（`ARCHITECTURE.md:262-263` 的示例工具），也是 `ARCHITECTURE.md:308` 对抗性测试的目标（"偷偷创建采购单"同族）。
- **`confirmation` 一致性**：`batch.verify` / `harvest.create` / `harvest.verify` 初稿或为 `never` 或未列，按 §0.4 的 `risk=high ∧ action ⇒ always` 约束统一为 **`always`**；`feeding.create` 由初稿的 `by_key` 改为 **`always`**（§7 Q4）。

### 1.6 warehouse — 18 条

| name | method | path | title | kind | required_permission | scope | risk | confirmation | agent_exposure | idempotent | audit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `warehouse.list` | GET | `/api/v1/warehouses` | 仓库列表 | read | `warehouse.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `warehouse.get` | GET | `/api/v1/warehouses/{warehouse_id}` | 仓库详情 | read | `warehouse.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `warehouse.create` | POST | `/api/v1/warehouses` | 新建仓库 | create | `warehouse.create` | resource(area_id) | normal | never | exposed | **true** | before_after |
| `warehouse.update` | PATCH | `/api/v1/warehouses/{warehouse_id}` | 编辑仓库 | update | `warehouse.update` | resource(area_id) | normal | never | exposed | false | before_after |
| `warehouse.submit` | POST | `/api/v1/warehouses/{warehouse_id}/submit` | 提交仓库核验 | action | `warehouse.submit` | resource(area_id) | normal | never | exposed | **true** | `summary` |
| `warehouse.verify` | POST | `/api/v1/warehouses/{warehouse_id}/verify` | 核验仓库 | action | `warehouse.verify` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `warehouse.archive` | POST | `/api/v1/warehouses/{warehouse_id}/archive` | 停用仓库 | action | `warehouse.archive` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `warehouse_document.list` | GET | `/api/v1/receipts` | 仓储单据列表 | read | `warehouse.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `inventory.list` | GET | `/api/v1/inventory` | 库存汇总 | read | `inventory.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `inventory.ledger` | GET | `/api/v1/inventory/ledger` | 库存流水 | read | `inventory.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `receipt.create` | POST | `/api/v1/receipts` | 登记到货 | create | `warehouse.receipt.create` | resource(area_id) | normal | never | exposed | **true** | before_after |
| `receipt.verify` | POST | `/api/v1/receipts/{receipt_id}/verify` | 核验到货（入库存 + 生成应付） | action | `warehouse.receipt.verify` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `issue.create` | POST | `/api/v1/issues` | 登记领用出库 | create | `warehouse.issue.create` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `issue.verify` | POST | `/api/v1/issues/{issue_id}/verify` | 核验出库（扣库存） | action | `warehouse.issue.verify` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `inventory_lot.get` | GET | `/api/v1/inventory/{lot_id}` | 物料批次详情 | read | `inventory.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `issue.list` | GET | `/api/v1/issues` | 领用出库列表 | read | `inventory.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `issue.get` | GET | `/api/v1/issues/{issue_id}` | 领用单详情 | read | `inventory.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `warehouse_document.get` | GET | `/api/v1/receipts/{receipt_id}` | 仓储单据详情 | read | `warehouse.view` | resource(area_id) | `read` | never | exposed | false | `summary` |

依据：旧 `warehouse_service.py:11` 的 7 种单据（receipts, issue-requests, issues, returns, transfers, stocktakes, scraps）+ `warehouses` + `inventory_ledger` + 预警。
- 保留 **receipts / issues** 两种单据 + 三个只读视图（warehouse / inventory / ledger）。
- 砍掉：`issue-requests`（领用申请是出库的前置审批层，`production_material_control.py:8-34` 用一条 5 表 JOIN 的 SQL 校验"申请额度−已出库−已投喂 ≥ 本次量"，属于可裁的二级管控）、`returns`（§6 退货）、`transfers`（§6 调拨）、`stocktakes`（§6 盘点）、`scraps`（§6 报废）。
- **scope 修正**：`inventory.list` / `inventory.ledger` 的 scope 由初稿的 `resource(area_id) via warehouse` 改为 **`resource(area_id)`**——按 §7 Q6 裁决，`warehouses` / `inventory_lots` / `inventory_ledger` 三张表**补齐 `farm_id` / `area_id` 分租列**，不再需要跨表解析（§0.3）。
- `issue.verify` 是**"不得负库存"不变量的强制点**（§4 #1）。
- `receipt.verify` 的 `risk=high`：它同时写 `inventory_ledger` 和生成 `payable`（旧 `warehouse_ledger_store.py:6` → `purchase_posting.post_purchase_receipt()`）。
- **t18 增补 4 条仓库主数据能力**（`warehouse.get` / `create` / `update` / `archive`）；
  **2026-09-15 再增补 2 条**（`warehouse.submit` / `warehouse.verify`，用户报「仓库新建后
  自动核验完成」）—— 合计 **6 条**。
  ⚠️ 它们**属于 warehouse 域，不是 master_data**：
  `tests/test_architecture.py::test_capability_domain_matches_its_declaring_directory`
  是一条**集合相等**断言（`Capability.domain` 必须等于它**所在声明目录**的域名）。
  把 `domain="master_data"` 写在仓库能力上是**静默错域**——`Registry.by_domain`、
  前端按域菜单、`runner` 的 `resource_type` 全部跟着错，而没有一条既有检查会红。
  **域归属看代码在哪，不看「这一轮是谁在做」。**

  | 项 | 取值与依据 |
  |---|---|
  | `risk` | get = `read`；create / update / submit = `normal`；verify / archive = **`high`**（同 §1.4 的判据①） |
  | `confirmation` | `high ∧ action ⇒ always`（§0.4 的机械约束） |
  | `idempotent` | `kind=create` ∪ `confirmation=always` ⇒ create / verify / archive `true`；get / update / submit `false`（`submit` 幂等 —— 重复提交会因状态机已转移而被拒） |
  | `scope` | 一律 `resource(area_id)`（`warehouses` 三列分租键齐全，可直接命中索引 —— Q6） |
  | `audit` | 写能力 `before_after`（`submit` 用 `summary`）；`warehouse.get` `summary`（读） |
  | `agent_exposure` | `exposed` |

  **`warehouse.create` 的初始状态是 `draft`（2026-09-15 改；此前是 `verified`）。**
  原先定成 `verified` 的理由是"可达性"：`receipt.verify` 与
  `ledger._default_warehouse_id` 都只认 `status='verified'`，而当时**没有**
  `warehouse.verify` 能力 —— 若新建的是 `draft`，它会永远无法被任何业务路径使用
  （`DECISIONS.md` Q10 的「清单上看起来是有的」缺陷）。
  用户报「仓库新建后自动核验完成」后，修法改为**把缺失的那条路径补齐**：
  新增 `warehouse.submit` / `warehouse.verify`，初始状态回到 `draft`，
  与区域 / 物料同一口径；顺带补上**原本缺失的 `archived` 状态**
  （`warehouse.archive` 一直写 `status='archived'`，而状态表里没有这个值 ——
  归档后的行会退化成裸英文 `archived` 且没有任何允许动作）。
  006 迁移种下的 `verified` 历史行不受影响。
  **`warehouse.archive` 的 `confirmation=always` 与 `warehouse.verify` 同判据**
  （`high ∧ action`）。
- **`issue.create` 由初稿的 `by_key` 改为 `always`**（§7 Q4）。

### 1.7 purchase — 13 条

| name | method | path | title | kind | required_permission | scope | risk | confirmation | agent_exposure | idempotent | audit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `purchase_order.list` | GET | `/api/v1/purchase-orders` | 采购单列表 | read | `purchase.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `purchase_order.get` | GET | `/api/v1/purchase-orders/{order_id}` | 采购单详情 | read | `purchase.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `purchase_order.create` | POST | `/api/v1/purchase-orders` | 新建采购单 | create | `purchase.create` | resource(area_id) | normal | never | exposed | **true** | before_after |
| `purchase_order.update` | PATCH | `/api/v1/purchase-orders/{order_id}` | 编辑采购单 | update | `purchase.create` | resource(area_id) | normal | **always** | exposed | false | before_after |
| `purchase_order.submit` | POST | `/api/v1/purchase-orders/{order_id}/submit` | 提交采购单 | action | `purchase.create` | resource(area_id) | normal | never | exposed | false | summary |
| `purchase_order.approve` | POST | `/api/v1/purchase-orders/{order_id}/approve` | 审批采购单 | action | `purchase.approve` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `purchase_order.cancel` | POST | `/api/v1/purchase-orders/{order_id}/cancel` | 取消采购单 | action | `purchase.approve` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `payable.list` | GET | `/api/v1/payables` | 应付列表 | read | `finance.payable.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `payment.list` | GET | `/api/v1/payments` | 付款列表 | read | `finance.payment.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `payment.create` | POST | `/api/v1/payments` | 登记付款 | create | `finance.payment.manage` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `payment.verify` | POST | `/api/v1/payments/{payment_id}/verify` | 核验付款 | action | `finance.payment.verify` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `purchase_payable.get` | GET | `/api/v1/payables/{payable_id}` | 应付详情 | read | `finance.payable.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `payment.get` | GET | `/api/v1/payments/{payment_id}` | 付款详情 | read | `finance.payment.view` | resource(area_id) | `read` | never | exposed | false | `summary` |

依据：旧 `purchase_service.py:11-17`。权限码沿用旧命名（`purchase.view/purchase.create/purchase.approve`、`finance.payable.view`、`finance.payment.manage/verify`，见 `purchase_service.py:26-30` 与 `purchase_payment_store.py:182`）。
- `purchase_order.approve` 的 `risk=high` 且**必须**挂"经办人≠审批人"不变量（§4 #5、Q8）；按 §0.4 的一致性约束，`confirmation` 由初稿的 `never` 修正为 **`always`**。
- `payment.verify` 是**"付款不得超过余额"不变量的强制点**（§4 #4），早期版本在同一条链路上校验了两次（`purchase_payment_store.py:114-115` 创建时 + `:154-157` 核验时），新系统只在校验点 `payment.verify` 强制、`payment.create` 只做提示性校验。
- `receipt.verify` 推进 `purchase_order` 状态到 `partially_received`/`fully_received`（旧 `purchase_posting.py:35-38`），因此不设独立的 `purchase_order.receive` 能力。
- **`payment.create` 由初稿的 `by_key` 改为 `always`**（§7 Q4）。
- **`purchase_order.get` 是 t7 新增的详情读**（`ROADMAP.md` §4.2 的 P1「详情页」）：
  处理器 `PurchaseOrderService.get_order_by_id` 早就实现（含 `available_transitions` 与
  第三层范围校验），此前**刻意未注册**，理由是「69 条是 t9 的验收基线，不该由某个域单独改数」。
  当前迭代的任务本身就是补缺口，因此那条基线被**显式解除**（裁决与理由写在
  `domains/purchase/capabilities.py` 文件末尾）。形状与 `purchase_order.list` 逐格一致：
  `read` / `purchase.view` / `resource(area_id)` / `never` / `exposed` / `false` / `summary`。

### 1.8 sales — 17 条

| name | method | path | title | kind | required_permission | scope | risk | confirmation | agent_exposure | idempotent | audit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `sales_order.list` | GET | `/api/v1/sales-orders` | 销售单列表 | read | `sales.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `sales_order.get` | GET | `/api/v1/sales-orders/{order_id}` | 销售单详情 | read | `sales.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `sales_order.create` | POST | `/api/v1/sales-orders` | 新建销售单 | create | `sales.create` | resource(area_id) | normal | never | exposed | **true** | before_after |
| `sales_order.update` | PATCH | `/api/v1/sales-orders/{order_id}` | 编辑销售单 | update | `sales.create` | resource(area_id) | normal | never | exposed | false | before_after |
| `sales_order.submit` | POST | `/api/v1/sales-orders/{order_id}/submit` | 提交销售单 | action | `sales.create` | resource(area_id) | normal | never | exposed | false | summary |
| `sales_order.approve` | POST | `/api/v1/sales-orders/{order_id}/approve` | 审批销售单 | action | `sales.approve` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `sales_order.cancel` | POST | `/api/v1/sales-orders/{order_id}/cancel` | 取消销售单 | action | `sales.approve` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `delivery.list` | GET | `/api/v1/deliveries` | 交付单列表 | read | `sales.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `delivery.create` | POST | `/api/v1/deliveries` | 登记交付 | create | `sales.deliver` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `delivery.verify` | POST | `/api/v1/deliveries/{delivery_id}/verify` | 核验交付（生成应收） | action | `sales.verify` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `receivable.list` | GET | `/api/v1/receivables` | 应收列表 | read | `finance.receivable.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `sales_receipt.create` | POST | `/api/v1/sales-receipts` | 登记收款 | create | `finance.receipt.manage` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `sales_receipt.verify` | POST | `/api/v1/sales-receipts/{receipt_id}/verify` | 核验收款 | action | `finance.receipt.verify` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `delivery.get` | GET | `/api/v1/deliveries/{delivery_id}` | 交付单详情 | read | `sales.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `receivable.get` | GET | `/api/v1/receivables/{receivable_id}` | 应收账款详情 | read | `finance.receivable.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `sales_receipt.list` | GET | `/api/v1/sales-receipts` | 收款单列表 | read | `finance.receipt.manage` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `sales_receipt.get` | GET | `/api/v1/sales-receipts/{receipt_id}` | 收款详情 | read | `finance.receipt.manage` | resource(area_id) | `read` | never | exposed | false | `summary` |

依据：旧 `sales_service.py:7-11`、`sales_posting.py:42-66`。
- 权限码沿用旧命名（`sales_service.py:16-18,56-86` 使用 `sales.view/sales.manage/sales.verify`；本版拆出 `sales.create` 与 `sales.deliver` 两个码，`sales.manage` 废弃）。
- `delivery.create` 的 `risk=high`：早期版本交付必须绑定出塘单且数量**逐字节相等**（`sales_source_control.py:22-24`）。
  > **裁决（§7 Q9 / `DECISIONS.md` Q9）：不变量保持"累计交付 ≤ 销售数量"，并恢复"交付数量与出塘事实一致"的比对。**
  > `delivery.create` 因此**恢复 `harvest_document_id` 字段**（初稿曾以"出塘事实已砍"为由删除它，见 §2.9 的修订说明）。比较对象是 `harvest`（§1.5 已恢复）。
- `sales_order.approve` 挂"经办人≠审批人"，这是早期版本**唯一**实现了该规则的地方（`sales_service.py:121-122`）；按 §4 #5 该规则已推广到 11 条能力。
- **`delivery.create` / `sales_receipt.create` 由初稿的 `by_key` 改为 `always`**（§7 Q4）；`sales_order.approve` 按 §0.4 一致性约束由 `never` 修正为 **`always`**。
- **`sales_order.get` 是 t7 新增的详情读**（同 §1.7 的 `purchase_order.get`）。两域**同形**：
  形状与 `sales_order.list` 逐格一致（`read` / `sales.view` / `resource(area_id)` / `never` /
  `exposed` / `false` / `summary`）。
  > 落地时由守卫测试抓到一处真缺口：`SalesOrderService.get_order_by_id` **原本没有**返回
  > `available_transitions`（purchase 有、sales 没有），而「详情能给出从当前状态出发的合法目标」
  > 正是登记 `*.get` 的理由 —— 已补，并由 `tests/test_detail_capabilities.py` 钉住。

### 1.9 cost — 7 条

> **本节 5 条全部保留。** 初稿曾在本节挂过一条"被 §1.12 取代为 2 条"的警示，**该精简已被裁决推翻**（§7 Q13 / `DECISIONS.md` Q13）。§1.12 保留为历史记录。

| name | method | path | title | kind | required_permission | scope | risk | confirmation | agent_exposure | idempotent | audit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `cost.entry.list` | GET | `/api/v1/cost/entries` | 成本记录列表 | read | `cost.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `cost.entry.create` | POST | `/api/v1/cost/entries` | 登记成本 | create | `cost.manage` | resource(area_id) | normal | never | exposed | **true** | before_after |
| `cost.entry.confirm` | POST | `/api/v1/cost/entries/{entry_id}/confirm` | 确认成本 | action | `cost.confirm` | resource(area_id) | high | **always** | exposed | **true** | before_after |
| `cost.summary` | GET | `/api/v1/cost/summary` | 成本汇总 | read | `cost.view` | resource(area_id) | `read` | never | exposed | false | `summary` |
| `accounting_period.list` | GET | `/api/v1/cost/periods` | 会计期间列表 | read | `cost.view` | none | `read` | never | exposed | false | `summary` |
| `cost.period.close` | POST | `/api/v1/cost/periods/{period}/close` | 关账 | action | `cost.close` | none | high | **always** | exposed | **true** | before_after |
| `cost_entry.get` | GET | `/api/v1/cost/entries/{entry_id}` | 成本记录详情 | read | `cost.view` | resource(area_id) | `read` | never | exposed | false | `summary` |

依据：旧 `cost_enterprise_validation.py:10-14`（EXPENSE_FIELDS）、`cost_service.py:53-76`（单位成本）、`cost_enterprise_repository.py:86-92`（期间锁定）。
- 按 `ARCHITECTURE.md:338`"成本只做塘口/批次维度的归集"，砍掉旧 cost 域的**资产/折旧/分摊/结算**（旧 6 张表、11 个 store、5 个迁移；见 §6）。
- 换算金额与单位成本逻辑继承旧 `calculation.py:21-97`（`summarize_costs` 保证百分比合计 100.0000、`allocate_amount` 先取整再补差）。
- **`cost.entry.confirm` 的 `confirmation` 由初稿的 `never` 修正为 `always`**（§7 Q13 / `DECISIONS.md` Q13 的"顺带修正"）：它是 `risk=high` 的审批动作，按 §0.4 的一致性约束必须为 `always`，标 `never` 是自相矛盾的。**这一条必须在代码里可机械校验**（注册期拒绝 `risk=high ∧ kind=action ∧ confirmation≠always`）。
- `cost.entry.confirm` 是**最能体现双人复核设计的载体**（`DECISIONS.md` Q13 原文）：它同时挂 `DistinctActors(created_by, verified_by)`（§4 #5）与 `RequiredField(source_ref)`（§4 #15）两条不变量。
- `cost.period.close` 是**"期间锁定"不变量的开启点**；其 `human_only` 倾向见 §5.3。
- **`PeriodOpen` 不变量的覆盖范围已扩展**：不只挂在 `cost.entry.create` / `cost.entry.confirm` 上，而是同时挂到 4 条会写成本派生的能力上——详见 §4 #7（这是比早期版本更强的做法）。

### 1.10 workbench — 1 条

| name | method | path | title | kind | required_permission | scope | risk | confirmation | agent_exposure | idempotent | audit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `work_item.list` | GET | `/api/v1/work-items` | 待办列表 | read | `pond.status.verify` | none | `read` | never | exposed | false | `summary` |

依据：`docs/ROADMAP.md` §4-T-B「P2 工作台待办」。本域**只有一条读能力**：
待办由 `pond_status_change.request` 在**同一事务**内写入（见 §3.1-B），
**没有独立的 `work_item.create`** —— 前端 `frontend/src/layers/product/workbench/WorkbenchPage.vue`
按元数据自己发现 `work_item.list` 并渲染，所以加这一条不需要改页面。

### 1.11 规模统计（113 条）与被否决的裁剪建议

**本节原为"建议裁剪 12 条 → 52 条"。该建议已被裁决否决（§7 Q12 / `DECISIONS.md` Q12：「能力清单零裁剪」）。裁剪分析保留在下方作为历史记录。**

#### 1.11.1 权威统计（可复算）

| 域 | 条数 | 占比 |
|---|---:|---:|
| identity | 4 | 3.7% |
| access | 9 | 8.3% |
| audit | 1 | 0.9% |
| master_data | 28 | 25.9% |
| production | 15 | 13.9% |
| warehouse | 18 | 15.9% |
| purchase | 13 | 12.0% |
| sales | 17 | 15.5% |
| cost | 7 | 6.2% |
| workbench | 1 | 0.9% |
| **合计** | **113** | 100% |

> **t18 的计数口径说明（必读，否则本节会看起来与运行时"对不上"）**：
> 本节的统计对象是 **§1.1–§1.10 的能力行**。当前迭代补齐后它是 **113 行**；
> 而**当前运行时注册表是 109 条**。差的 **4 条全部是契约行**，不是缺口：
> `meta.capabilities` / `auth.login` / `auth.logout` / `auth.me` —— 它们由 Flask 的
> **固定路由**提供、不经能力执行器，对账工具按真实路由表把它们归入 `[A2]`。
> **两个数字都能复核；不一致是已知的口径差，不是漏实现。**
> （本节此前写的"84 行 / 差 3 条全在 identity"是错的：84 与逐域之和、与 §1 实际行数
> 都对不上；t7 按**现算**值改回，现算脚本见 `.tmp-t7/count_doc_stats.py` 的做法。）

**结构比例**（统计口径：§1.1–§1.10 的 113 行）：

| 维度 | 取值分布 |
|---|---|
| `kind` | `read` **52** · `create` **18** · `update` **7** · `action` **36** |
| `confirmation` | `never` **77** · `always` **36**（**无 `by_key`**） |
| `agent_exposure` | `exposed` **108** · `human_only` **1** · `n/a` **4**（固定路由，不入注册表） |
| `idempotent` | `true` **44** · `false` **69** |

> **t18 对这两张表的复核结论**：原表（73 条口径）写的 `action 29` /
> `idempotent true 33` 与**当时的运行时读数**（`action 30` / `true 37`）本身就不一致，
> 差异恰好落在 identity 域那几条"文档有、代码无"的能力上（它们的 `kind` /
> `confirmation` / `idempotent` 写在文档里、不在注册表里）。
> t18 的六条新增能力（2 create + 2 update + 2 action）已并入上表。
> **不要用一条算式复核这些格子**——`confirmation` × `kind` 的交叉表在本文件里
> 从来没有给出过，任何"减法算式"都只是在两次近似之间倒推。可直接复核的只有
> 运行时那一半，命令见下。

**复核方法**（照抄即可验证本表）：
- 能力行 = §1.1–§1.10 中以 `| `xxx.yyy` | GET|POST|PATCH|PUT|DELETE |` 开头的行，计 **113** 行 / **113** 个唯一 `name`。
  （**注意**：§1.11.2 / §1.12 的历史对照表里也有形如能力行的行（`cost.summary` / `cost.entry.list` 重复出现），
  复核时必须限定在 §1.1–§1.10 —— 全文扫会多出 2 行。这个坑已实际踩到过。）
- `idempotent` 由 §0.5 的派生规则算出（`kind=create` ∪ `confirmation=always` ∪
  `creates_record`），**不是逐行手写**。要复核**运行时**的那一半，跑这一条：

  ```powershell
  python -c "import sys;sys.path.insert(0,'backend');from fpa.bootstrap import load_all;from fpa.kernel.capability import REGISTRY;load_all();c=list(REGISTRY.all());print(len(c), sum(1 for x in c if x.requires_idempotency_key))"
  ```

  **文档与工具不一致时以工具为准**，并把文档改成工具的输出。
- §0.4 的一致性约束复核：不存在 `risk=high ∧ kind=action ∧ agent_exposure≠human_only ∧ confirmation≠always` 的行。

`action` 占比 **38%**——这是本项目的核心特征（状态机即业务），也是"一次业务写操作要改哪些东西"必须以能力为单位的根本原因。

#### 1.11.2 被否决的裁剪建议（保留为历史记录）

**原建议：砍掉 12 条 → 52 条**，理由是"均为列表页/编辑入口而非闭环必需"：

| 原建议砍掉 | 原理由 |
|---|---|
| `meta.resources` | **见下方"唯一一条我用阻断路径标准复核后仍主张删除的条目"**。`meta.capabilities` 已能提供字段与 `status_dict`；且 `INTERFACES.md` **从未定义过这个端点** |
| `partner.create` | 往来单位由种子/管理员建立；采购与销售的闭环只需要 `partner.list` 供选择 |
| `purchase_order.update` | 草稿改价可"作废重开"；减少一个 update 能力与一条乐观锁路径 |
| `sales_order.update` | 同上 |
| `payable.list` | 应付余额可作为 `purchase_order.list` 的一列返回（早期版本 `purchase_payment_store.py:95-96` 就是这么算的） |
| `payment.list` | 付款记录可从应付行下钻 |
| `receivable.list` | 同 `payable.list`（旧 `sales_posting.py:55-59` 的余额推导） |
| `delivery.list` | 交付记录可从销售单下钻 |
| `warehouse.list` | 仓库可由 `meta.capabilities` 的 ref 提供选项 |
| `access.user.status` | 停用可并入 `access.user.grants` 的 `status` 字段 |
| `cost.summary` | 单位成本可作为 `cost.entry.list` 的聚合字段 |
| `batch.update` | 批次状态推进可由独立 `batch.status` 动作承担 |

> **裁决（§7 Q12 / `DECISIONS.md` Q12）：不采纳。能力清单零裁剪。**
> 理由原文四条：① "清单里的每一条都是别人能力的产出或输入，不是可选的 UI 便利"——逐条检验后只有 2 条接近"纯便利"（`payable.list` / `receivable.list`），而删掉它们会让"应付余额从哪来"变成不可解释，早期版本正是把余额推导塞在 SQL 里才造成对账困难；② 11 条裁剪约省 25 h，在 300 h 总量里是 8%，但每条都会让对应流程少一个入口；③ 前端 50 h 的估算是**手写页面**的成本，而元数据驱动下加一个资源页的成本是"加一个声明"；④ 更根本的是**砍能力会制造"永远不可达的状态"**（Q10 已证实一次）——清单短了不等于系统简单，可能是系统不完整。
>
> **本次修订中的自我修正**：原建议里有一条 `batch.update` 是**明确错误的**——批次状态推进 `stocked → farming → pending_settlement` 只能由 `batch.update` 触发，砍掉它与 Q10 发现的"`batch.close` 永远不可达"是**同一类错误**。这条应当由我自己用 Q10 的标准检出，但没有。作为对照：本条建议中**唯一经得起复核**的是 `meta.resources`（`INTERFACES.md:49-122` 的 `GET /api/v1/meta/capabilities` 响应体里**同时**含 `capabilities` 与 `resources` 两段，独立端点确属重复）——但它仍未被采纳，理由是保留它可以让 `resources` 元数据单独缓存，且删除它属于"改名/移动"而非"简化"。
> #### 唯一一条我用"阻断路径"标准复核后仍主张删除的条目：`meta.resources`
>
> **标准**（`DECISIONS.md` Q12 第 4 条）：删一条能力，必须能回答"删除后哪条业务路径会断"。
>
> **证据（重新核实 `INTERFACES.md`，非凭记忆）**：
> - `INTERFACES.md:49` 只定义了**一个**端点：`### GET /api/v1/meta/capabilities`；
> - 全文检索 `/api/v1/meta/` 只命中两处（`:49` 定义、`:334` 引用），**没有任何 `GET /api/v1/meta/resources` 的定义**——即 `meta.resources` 是本文件**自造的端点**，`INTERFACES.md` 不认；
> - 该端点的响应体（`:53-122`）**同时**含两段：`"capabilities": [`（`:57`）与 `"resources": [`（`:111`）。前端要的 `columns` / `status_dict` 在 `:111` 的 `data.resources` 段里。
>
> **逐条路径核查**：
>
> | 业务路径 | 依赖什么 | 删除后是否断 |
> |---|---|---|
> | 前端渲染表单字段 | `data.capabilities[].fields` | **不断**（同一响应，`:57`） |
> | 前端渲染列表列与状态字典 | `data.resources[].columns` / `status_dict` | **不断**（同一响应，`:111`） |
> | 前端菜单与按钮（L1 过滤） | `data.capabilities[]` | **不断**（同一响应，`:57`） |
> | CI 断言生成物逐字节一致（`INTERFACES.md:309`） | `types.gen.ts` | **不断**（与端点数无关） |
> | OpenAPI 契约一致性 | `docs/openapi.json` | **不断，且反向改善**——保留它反而要求 OpenAPI 记录一个 `INTERFACES.md` 里不存在的端点，制造契约冲突（`DECISIONS.md` Q1 刚确立"契约里每一个字面值都必须可追溯"） |
> | 任何现存消费者 | — | **不存在**——渔芯当前只有 3 个 `.md`，无任何代码 |
>
> **结论**：删除后**没有任何业务路径会断**，且会消除一处与 `INTERFACES.md` 的契约冲突。
>
> **为什么这不是"看起来可以用别的方式达成"**（负责人 明确排除的那种论证）：`payable.list` 那一类的反对意见是对的——删掉它余额仍在，但**来源变成不可解释的隐藏 SQL 计算**，这是路径**降级**。`meta.resources` 不同：它不是一条业务入口，而是**同一份响应体上的第二条 HTTP 路由**，而 `INTERFACES.md` 从未定义过它。"用另一个端点拿同一份响应里已有的数据"不是替代方案，是**去重复**。
>
> **我的建议**：从 69 条中删除 → **68 条**。**这只是建议，我不擅自改**——正文 §1.4 仍按 69 条保留它，等你裁决。


#### 1.11.3 各域最小可行规模（原判断，保留为分析记录）

- `cost` 域 **2 条**（按 §1.12 方案 B）——**已被否决**，见 §1.12。
- `audit` 域 **1 条够**。
- `identity` 域 **4 条不能再少**（login/logout/me/password.change 缺一不可）。
- `master_data` 域 **7 条**（`meta.capabilities` + `area.list` + pond 的 list/create/update/submit/verify）——**已被否决**；裁决后为 **13 条**（新增两个状态变更审批能力，且 `material.list` / `partner.list` 确认不可砍）。
- `sales` 域 **8 条**——**已被否决**，保留 12 条。
- `production` 域：**初稿判断缺了 3 条出塘能力**（Q10 检出），裁决后为 **12 条**。

### 1.12 成本域精简：按 负责人 指导改为"纯派生 + 查询"（**已被 负责人 推翻 —— 见 `DECISIONS.md` Q13**）

> ## ⛔ 本节结论已被推翻，请勿实施
>
> **裁决（§7 Q13 / `DECISIONS.md` Q13）**：**推翻本节的"纯派生 + 只读"方案，cost 域保留全部 5 条**（`cost.entry.list` / `cost.entry.create` / `cost.entry.confirm` / `cost.summary` / `cost.period.close`）。**下游任何实现都以 §1.9 为准。**
>
> **推翻理由（`DECISIONS.md` Q13 原文三条）**：
> 1. **"成本随业务事实自动生成"不是一个能力**——它是 `feeding.verify` / `receipt.verify` / `issue.verify` 的**副作用**。所以"成本只需 2 条"的真实含义是"成本只需读，写靠别的能力的副作用"，它**完全遗漏了系统外费用**（人工、水电、租金）。**这是覆盖面缺口，不是精简。**
> 2. **`cost.entry.confirm` 是最能体现双人复核设计的载体**。需求明确写"审核、付款、收款"属高风险业务。它上挂着两条不变量：`DistinctActors(created_by, verified_by)`（经办人≠审批人）与 `RequiredField(source_ref)`（每笔成本可溯源）。删掉它等于删掉成本域的双人复核能力——而"经办人≠审批人"在早期版本里**本来就只在 sales 域实现过一处**，正是要推广的合规规则。
> 3. **期间锁定（不变量 #7）不失去载体，而是应该被加强**。本节说"早期版本 `require_unlocked()` 只挂在手工入口上"是**对早期版本的准确描述**，但新系统可以做得更强：把 `PeriodOpen` 挂到**全部 4 个会写成本的能力**上。**一个不变量覆盖 4 条能力，比挂在 1 条手工入口上强得多**——这是架构升级带来的收益，不应该反过来被当成删除的理由。
>
> **本节的处置**：**不删除**，保留完整分析作为历史记录，以便后人理解"为什么没采纳这个看起来更省事的方案"。
>
> **本节唯一被后续采纳的内容**：`PeriodOpen` 覆盖范围的讨论被 Q13 反向采纳为"扩展到 4 条能力"——见 §4 #7。

---

**（以下为原分析，保留为历史记录）**

**指导原文**：「如果你判断某个域只需要 3-4 个能力就能证明闭环（例如 cost 只需要「按塘口/批次查成本」+「成本随业务事实自动生成」），就直接给出这个精简建议并说明砍掉了什么。」

**我的判断：采纳，并把 cost 从 5 条压到 2 条。** 依据是成本在本架构里**本来就是派生的**——§3 已规定三处业务事实在核验时自动写成本：

| 触发能力 | 自动产生的成本 | 本文档中的位置 |
|---|---|---|
| `feeding.verify` | 投喂物料成本（`source_type=warehouse_ledger`，归属 = 批次 / 塘口） | §1.5、§3.3、§2.10 |
| `issue.verify` | 领用出库成本 | §1.6 |
| `receipt.verify` | 采购入库成本（已经由应付，成本按同一金额归集） | §1.6、`早期版本：warehouse_ledger_store.py:6` → `purchase_posting.post_purchase_receipt()` |

因此"成本"真正缺的只有**读**。手工录入成本（`cost.entry.create` + `cost.entry.confirm`）存在的唯一理由是"系统外的费用也要进成本"——这属于完整财务 ERP 的范畴，已被 `ARCHITECTURE.md:338`（"成本只做塘口/批次维度的归集"）与用户"不要重新开发一套大型 ERP"明确排除。

**原拟精简后的 cost 域（2 条，取代 §1.9 的 5 条）——⛔ 未采纳**：

| name | method | path | title | kind | required_permission | scope | risk | confirmation | agent_exposure | idempotent | audit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `cost.summary` | GET | `/api/v1/cost/summary` | 按塘口/批次查成本 | read | `cost.view` | resource(area_id) | normal | never | exposed | false | none |
| `cost.entry.list` | GET | `/api/v1/cost/entries` | 成本流水 | read | `cost.view` | resource(area_id) | normal | never | exposed | false | none |

**原拟砍掉什么（⛔ 未采纳）**：`cost.entry.create`、`cost.entry.confirm`、`cost.period.close` —— 共 **3 条**。

**我当时列出的代价（逐条保留为历史记录）**：

> ⚠️ **负责人 对代价 #1 / #2 的校正（`DECISIONS.md` Q13 第 3 点）**：这两条的**描述是准确的**（早期版本 `require_unlocked()` 确实只挂在手工入口上），但**推论是错的**。正确结论是「**新系统可以把 `PeriodOpen` 挂到全部 4 个会写成本的能力上**」——一个不变量覆盖 4 条能力，比挂在 1 条手工入口上强得多。**架构升级带来的收益不应该反过来被当成删除的理由。** 该校正已回写进 §4 #7。

| # | 代价 | 说明 |
|---|---|---|
| 1 | 不变量 **#7「已关账期间不得再归集成本」失去强制能力** | 它唯一的作用对象是手工录入入口。自动归集发生在业务事实核验时，早期版本的 `require_unlocked()` 也只挂在手工入口上（`早期版本：cost_store.py:71,79` 的两处 `require_entry_open` 调用） |
| 2 | 不变量 **#8「同期不得重复归集库存成本」失去强制能力** | 它防的正是"手工费用与库存自动成本重复计入"——早期实现的白名单 `manual_feed_offset` / `manual_feed_direct`（`早期版本：cost_enterprise_repository.py:96,120-122`）证明这条规则只为手工路径存在。纯派生后**结构上不可能重复**（一次业务事实只写一次账本，唯一键 `uq_cost_settlement_source` 同族约束仍在） |
| 3 | **`accounting_periods` 表消失**（`早期版本：030_accounting_periods.sql`） | 少一张表 + 一个迁移 |
| 4 | `cost_entries.source_type` 枚举从 3 值收到 **1 值**（只剩 `warehouse_ledger`） | §2.10 的字段表相应缩减；`manual_expense` / `asset_depreciation` 随能力一起消失 |
| 5 | §4 的覆盖统计从"17 条落在能力上"变为 **15 条** | #7 / #8 移入"降级"组，与 #6 / #15 / #17 同列——§4 结尾的闭合语句已相应修订 |
| 6 | 失去手工成本录入的兜底 | 系统外费用（如人工、水电）无法进成本。**这是接受代价的核心**：`ARCHITECTURE.md:338` 已把它划出范围 |

**原拟方案 B 的总数（⛔ 未采纳，保留为历史记录）**：

| 方案 | 构成 | cost 条数 | 总数 |
|---|---|---:|---:|
| **A** | §1 的权威清单（完整） | 5 | **64** |
| **B（原拟推荐）** | A − §1.11 的 11 条非成本裁剪 − §1.12 的 3 条成本裁剪 | 2 | **50** |

> **裁决（§7 Q12 + Q13）：A 与 B 均不采纳。** `DECISIONS.md` Q12 结论为"能力清单零裁剪"，Q13 结论为"cost 域保留全部 5 条"。**最终权威规模 = 69 条**（基线 64 + `harvest.*` 3 条 + `pond_status_change.*` 2 条），见 §1 抬头与 §1.11.1。
> 本节末尾原有一段"再往下还能砍：cost 降到 1 条 → 总数 49"的推论，同属未采纳内容，已删除以免误导。

方案 B 的 14 条裁剪明细：`meta.resources`、`partner.create`、`purchase_order.update`、`sales_order.update`、`payable.list`、`payment.list`、`receivable.list`、`delivery.list`、`warehouse.list`、`access.user.status`、`batch.update`（以上 11 条来自 §1.11）+ `cost.entry.create`、`cost.entry.confirm`、`cost.period.close`（以上 3 条来自本节）。
**注意**：§1.11 原先裁剪清单里的 `cost.summary` 在本方案中**被撤回保留**（它是"按塘口/批次查成本"的唯一载体），因此 §1.11 的"12 条"在方案 B 下等价为"11 条"。

**再往下还能砍**：`cost.entry.list` 可并入 `cost.summary` 的明细段，cost 降到 **1 条** → 总数 **49**。我不建议：成本流水是唯一能回答"这笔钱是怎么算出来的"的界面。

---

## 2. 字段清单（可写能力的共同来源）

本节是前端 `DynamicForm` 与后端 Pydantic 模型的**唯一来源**。`type` 取值域固定为 `INTERFACES.md:75` 定义的 9 种：`string|integer|number|boolean|date|datetime|enum|ref|text`。

### 2.1 字段名继承原则（**硬约束**）

> 早期版本在**生产环境验证过**的字段名一律继承。改名会让旧前端 25 个页面的语义全部失效。`.local/recon-backend.md` 统计旧后端共 574 处 SQL、186 个端点，字段名横跨 `master_data_service.py:11-20`、`production_service.py:17-33`、`warehouse_service.py:13-19`、`purchase_service.py:11-17`、`sales_service.py:7-11`、`cost_enterprise_validation.py:10-20` 六处常量。

**继承清单（不得改名）**：
`code`、`name`、`note`、`area_id`、`farm_id`、`pond_id`、`batch_id`、`material_id`、`warehouse_id`、`supplier_id`、`customer_id`、`quantity`、`unit`、`unit_price`、`amount`、`unit_cost`、`species`、`capacity_mu`、`happened_at`、`stocked_at`、`delivered_at`、`sold_at`、`paid_at`、`received_at`、`occurred_on`、`due_date`、`expected_delivery_date`、`payment_method`、`receipt_method`、`category_code`、`source_type`、`source_ref`、`expected_version`、`row_version`。

### 2.1.1 `readonly` 字段（Q7 裁决的施工形态）

**每一个 `create` / `update` 能力的 `fields[]` 里都有三个 `readonly: true` 的字段**（服务端解析，客户端不可提交）：

| key | type | readonly | label | 数据来源 |
|---|---|---|---|---|
| `organization_id` | ref | ✔ | 所属企业 | `ScopeResolver` 从引用对象解析 |
| `farm_id` | ref | ✔ | 所属基地 | 同上 |
| `area_id` | ref | ✔ | 所属区域 | 同上 |

**约束（由测试强制）**：
1. `meta.capabilities` 返回的这三项 `readonly: true`；
2. 前端 `DynamicForm` 渲染成**禁用输入框**并显示服务端解析出的值（`DECISIONS.md` Q7："这样用户**看得见**自己的数据范围落在哪里——这正是需求里 DataScope 的可见证明点，而不是藏在后端"）；
3. **服务端拒绝**请求体里出现这三个字段（`FIELD_INVALID`，`data.field` 给字段名），**不做静默忽略**——静默忽略会让客户端误以为设置生效。

**例外**：`Area` 资源自身的 `area_id` 是它自己的主键（`area.create` 的 `id`），不在此列。

### 2.2 已确认的字段漂移与裁决

早期版本存在**写入侧用 `*_id`、读取侧用 `*_name` 的双份手写清单**，实测 35 处、跨 19 个文件：

| 证据 | 事实 |
|---|---|
| `早期版本：frontend/src/layers/product/purchase/PurchasePage.vue:34-35` | 表单字段写 `supplier_id` / `material_id` |
| `早期版本：frontend/src/layers/product/purchase/PurchasePage.vue:149` | 列表列写 `supplier_name` / `material_name` |
| `早期版本：frontend/src/layers/product/sales/SalePage.vue:36` | 表单字段写 `customer_id` |
| `早期版本：frontend/src/layers/product/sales/SalePage.vue:175` | 列表列写 `customer_name` |
| `早期版本：frontend/src/layers/product/sales/ReceivablePage.vue:113` | 列表列写 `customer_name` |
| `早期版本：frontend/src/layers/product/warehouse/ScrapPage.vue:15`、`StockLedgerPage.vue`、`StockAlertPage.vue` | 列表列写 `material_name` |
| `早期版本：frontend/src/layers/common/api/purchase.models.ts:8,10,27,42` | `supplier_name?: string` / `material_name?: string` |
| `早期版本：frontend/src/layers/common/api/sales.models.ts:4,12,18,23` | `customer_name?: string` |
| `早期版本：frontend/src/layers/common/api/warehouse.models.ts:34` | `material_name: string`（**必填**，与上面三处 `?` 可选**不一致**） |
| `早期版本：frontend/src/layers/common/ui/DataTablePage.vue:121` | 别名表把 `supplier_name` / `material_name` 映射成查询参数 `search`（**第三种名字**） |
| `早期版本：frontend/src/layers/features/agent/agent.glossary.ts:42,146`、`agent.humanize.ts:221` | Agent 侧把 `*_name` 当作"取名字"的 key 列表 |

**裁决（本版）**：

> **`*_id` 是唯一可写字段；`*_name` 是只读派生展示列。两者都存在，但角色互斥、来源唯一。**

| 角色 | 字段 | 出现位置 | 来源 |
|---|---|---|---|
| 可写 | `material_id` / `supplier_id` / `customer_id` / `pond_id` / `batch_id` / `area_id` / `warehouse_id` | 能力的 `fields` | 客户端提交 |
| 只读 | `material_name` / `supplier_name` / `customer_name` / `pond_name` / `batch_code` / `area_name` / `warehouse_name` | `meta.capabilities` 的 `resources[].columns` | 服务端 JOIN 派生，**不出现在 `fields` 中** |

这样处理的三条效果：
1. 旧前端的**展示语义完全保留**（25 个页面里的 `*_name` 列照旧渲染，因为它们走 `columns` 而非 `fields`）。
2. 写侧不再有第二份清单——`fields` 由 capability 派生，前端不得硬编码（`INTERFACES.md:127` 的测试约束）。
3. `DataTablePage.vue:121` 的别名表消失：搜索参数统一为 `search`，由后端按每资源声明的 `searchable_keys` 处理。

**其余三处字段歧义，一并裁决**：

| 歧义 | 旧证据 | 本版裁决 |
|---|---|---|
| `status` **与** `pond_status` 两个状态字段并存 | `早期版本：master_data_service.py:15`（ponds 同时有 `pond_status` 与 RESERVED 中的 `status`）；`早期版本：008_master_data.sql:36`（`pond_status ENUM`）；`早期版本：006_organizations_and_scopes.sql:87`（`status ENUM('draft','submitted','verified','archived')`） | **保留双字段**：`status` = 记录生命周期（draft/submitted/verified/archived），`pond_status` = 塘口业务状态。这是唯一一处"两个状态"的资源，必须在 §3 显式文档化 |
| 数量与重量双计量 | `早期版本：production_service.py:18-22` 的 `COMMON_FIELDS` 同时含 `quantity` 与 `weight_kg`；`早期版本：warehouse_service.py:16` 同 | **保留**：`quantity` 为计量单位下的数量，`weight_kg` 为公斤重；两者都进 `fields`，但**至少填一个**（校验规则见 §4 #16） |
| 各域日期字段名不统一 | `happened_at`（production/warehouse）、`stocked_at`（batch）、`delivered_at`（delivery）、`sold_at`（sales_order）、`paid_at`（payment）、`received_at`（sales_receipt）、`occurred_on`（cost_entry）——分别见 `production_service.py:19`、`:29`、`sales_service.py:8-9`、`purchase_service.py:15`、`cost_enterprise_validation.py:11` | **保留不统一**：前端 25 个页面的表单字段名已固化，统一会破坏继承。代价是每个能力的字段表必须逐个写明日期字段名——本文件已做到 |

### 2.3 identity

**`auth.login`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `identifier` | string | ✔ | 手机号或登录名，1–128 | 手机号 / 登录名 |
| `password` | string | ✔ | 1–128 | 密码 |

依据：旧 `product/auth/routes.py:79-82`（字段名 `identifier` / `password`）。
**不继承**：早期版本 `X-FPA-Client: mobile` 分支把裸 token 塞进响应体（`product/auth/routes.py:91-92`），移动端已砍（`ARCHITECTURE.md:339`）。

**`auth.password.change`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `current_password` | string | ✔ | 1–128 | 当前密码 |
| `new_password` | string | ✔ | 8–128，须含字母与数字，非弱密码 | 新密码 |
| `confirm_password` | string | ✔ | 与 `new_password` 一致 | 确认新密码 |

规则继承旧 `common/validation/auth_validation.py:41-55`（8–128、字母+数字、两次一致）与 `common/security/password.py:30-46`（黑名单 / 连续数字 / 4 连重复 / 键盘序列）。早期版本该模块全仓只有 3 个使用点，属"薄通用层"；新系统把它定位为 `kernel` 内的密码策略，不作为通用校验层（避免重演旧 `common/validation/` 只有 1 个文件 41 行的尴尬）。

### 2.4 access

**`access.user.create`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `name` | string | ✔ | 2–40 | 姓名 |
| `phone` | string | ✔ | 大陆手机号 | 手机号 |
| `login_name` | string | | 3–64，全局唯一 | 登录名 |
| `initial_password` | string | ✔ | 8–128，同上强度规则 | 初始密码 |
| `role_ids` | integer[] | ✔ | ≥1，须为存在的启用角色 | 角色 |
| `scope_ids` | integer[] | ✔ | ≥1，须为存在的启用数据范围 | 数据范围 |

依据：旧 `product/admin/routes.py:114-122` → `review_service.create_user`，字段见 `auth_admin_store.py:32-47`（`phone/login_name/name/role_ids/scope_ids/assigned_by`）。旧字段名 `password_hash` 是服务端派生，**不进入 `fields`**（旧 `product/admin/routes.py:153` 的 `temporary_password` 是重置口令，本版并入 `initial_password`）。

**`access.user.status`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `status` | enum | ✔ | `active` 启用 / `disabled` 停用 | 账号状态 |
| `reason` | text | ✔ | 1–500 | 变更原因 |

依据：旧 `users.status ENUM('pending','rejected','active','disabled','must_change_password','retired')`（`早期版本：004_enterprise_governance_foundation.sql:4`）。本版只保留 `active` / `disabled` 作为**可设置**值（`pending/rejected` 随"注册审核"一起砍，§6；`must_change_password` 由 `initial_password` 流程内部置位；`retired` 并入 `disabled` + `note`）。

**`access.user.grants`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `role_ids` | integer[] | ✔ | ≥1 | 角色 |
| `scope_ids` | integer[] | ✔ | ≥1 | 数据范围 |

依据：旧 `product/admin/routes.py:220-228`（`PUT /users/{id}/grants`），早期实现是"按最终集合同步"（`auth_admin_store.py:106-110` → `review_repository.replace_user_grants`）。本版保留整集合替换语义（PATCH 的增量语义与前端的表单模型不匹配）。

**`access.role.permissions`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `permission_codes` | string[] | ✔ | ≥0，每个须为**已注册的 capability 名** | 权限 |

依据：旧 `product/admin/routes.py:174-182` → `role_repository.replace_permissions`（`role_repository.py:8-25`）。**关键改进**：早期版本校验"权限码是否存在于 `permissions` 表"（`role_repository.py:15-19`），但 `permissions` 表是迁移里手工 INSERT 的（每个域迁移都有一段，见 `.local/recon-backend.md` §7.4）；新系统的合法值集合 = Capability Registry 的 `name` 集合，由注册表机械派生，**不可能出现"权限码存在但无对应能力"**。

### 2.5 master_data

**`pond.create` / `pond.update`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `code` | string | ✔（create） | 1–64，企业内唯一 | 塘口编号 |
| `name` | string | ✔（create） | 1–100 | 塘口名称 |
| `area_id` | ref | ✔（create） | `ref: {resource:"area", label_key:"name"}` | 所属区域 |
| `species` | string | | 1–64 | 主养品种 |
| `capacity_mu` | number | | 0 < v ≤ 100000 | 面积（亩） |
| `pond_status` | enum | | **只读派生**（`readonly: true`）。创建时只允许 `build` / `stocked` 作为**初始值**；此后只能经 `pond_status_change.request` + `pond_status_change.verify` 变更，**`pond.update` 拒绝提交此字段** | 塘口状态 |
| `manager_name` | string | | 1–40 | 负责人 |
| `location_text` | string | | 1–200 | 位置描述 |
| `aerator_count` | integer | | ≥0 | 增氧机数量 |
| `stocking_spec` | string | | 1–64 | 放养规格 |
| `note` | text | | ≤500 | 备注 |

依据：旧 `master_data_service.py:15` 的 `ponds` 字段集。**逐项裁决**：
- 保留 `code/name/species/capacity_mu/manager_name/location_text/aerator_count/stocking_spec/pond_status`。
- **删除 `current_spec` / `stock_quantity` / `stock_quantity_source`**：早期版本在 ponds 表上冗余了存塘汇总（`master_data_service.py:15`、`:28` 的 `STOCK_QUANTITY_SOURCES`），而生产域已经用 `batch_stock_records` 汇总。早期版本自己在 `product/production/routes.py:100-101` 的注释里承认了这个冗余（"塘口存塘量只读汇总……不写入/覆盖 `ponds.stock_quantity`"）。新系统**不冗余**：存塘只从 `batch_stock_records` 汇总。
- **删除 `pond_group_id`**（`master_data_service.py:15`）：`pond-groups` 资源已砍（§1.4）。
- `capacity_mu` 上限 100000 继承旧 `master_data_service.py:26`（`MAX_CAPACITY_MU`）。
- `pond_status` 创建时只允许 `build`/`stocked`，继承旧 `master_data_service.py:29`（`CREATE_POND_STATUSES`）。
- **pond_status 的两条路径（Q2 裁决）**：① pond.create 时可直接给初始值（仅 uild/stocked，继承旧 `CREATE_POND_STATUSES`）；② 之后**只能**经 `pond_status_change.request` → `pond_status_change.verify` 两步审批。`pond.update` 提交 `pond_status` 一律 `FIELD_INVALID`。

**`area.create` / `area.update` / `area.archive`（t18 新增）**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `code` | string | ✔（create） | 1–64，**基地内**唯一（`uq_areas_farm_code`） | 区域编号 |
| `name` | string | ✔（create） | 1–120 | 区域名称 |

**`area.update` 的入参** = `name`（可选，PATCH 语义）+ 全局 `expected_version`（§0.7 规则 3）。
**`area.archive` 没有业务字段**，只带全局 `expected_version`。
**`code` 不在 update 里**——与 `pond.update` 一致：编码是其他系统引用这条记录时的稳定标识，
要换编码就停用旧行、新建一行（那会留下审计线索，而改名不会）。

**`material.create` / `material.update` / `material.archive`（t18 新增）**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `code` | string | ✔（create） | 1–64，**企业内**唯一（`uq_materials_org_code`） | 物料编号 |
| `name` | string | ✔（create） | 1–120 | 物料名称 |
| `category` | string | | 1–32，默认 `feed`。**刻意不是 enum** | 分类 |
| `spec` | string | | 1–64 | 规格 |
| `unit` | string | | 1–16，默认 `kg` | 计量单位 |
| `unit_price` | number | | ≥0 | 单价 |
| `safety_stock` | number | | ≥0 | 安全库存 |
| `shelf_life_days` | integer | | ≥0 | 保质期（天） |
| `note` | text | | ≤500 | 备注 |

> **`category` 为什么不做成 `enum`**（这是一条**被继承的既有决定**，不是我的选择）：
> `materials.category` 是无约束的 `VARCHAR(32)`，003 迁移的原话是——「饲料/药品/物资的
> 划分会随业务演进，而 ENUM 每加一个值都要动 DDL。**会变的分类不该固化成约束。**」
> 把它声明成 `enum` 会把那条决定偷偷推翻：运维分册里的「水质改良剂」会变成非法输入，
> 而用户看到的是「分类不在允许范围内」。中文文案仍由内核
> `kernel/workflow.py::CATEGORY_LABELS` 提供（`category_label` 派生列 + 未登记的码回退原词）。
>
> **`material.update` 的入参** = 上表去掉 `code` 的全部字段（都可选）+ `expected_version`；
> **`material.archive`** 只带 `expected_version`。

**`partner.create`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `partner_type` | enum | ✔ | `supplier` 供应商 / `customer` 客户 | 单位类型 |
| `code` | string | ✔ | 1–64，企业内 + 类型内唯一 | 单位编号 |
| `name` | string | ✔ | 1–100 | 单位名称 |
| `contact_name` | string | | 1–40 | 联系人 |
| `phone` | string | | 1–32 | 联系电话 |
| `address` | string | | 1–200 | 地址 |
| `settlement_days` | integer | | ≥0 | 账期（天） |
| `credit_limit` | number | | ≥0 | 信用额度 |
| `note` | text | | ≤500 | 备注 |

依据：旧 `master_data_service.py:17-18`（suppliers 与 customers 字段集**完全相同**，这正是合并成 `partner` 的依据）。`partner_type` 是新增的显式字段——早期版本靠 `SPECS` 映射把资源名翻成 `partner_type` 并靠 `WHERE partner_type=%s` 过滤（`master_data_store.py:22-23,50-52`），新系统让它成为**可见字段**。

**`pond_status_change.request` / `pond_status_change.verify`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `to_status` | enum | ✔ | 必须命中 §3.1-B 的转移表：从塘口当前 `pond_status` 出发的**合法目标状态**之一（服务端按转移表过滤 `choices`） | 目标状态 |
| `reason` | text | ✔ | 1–500 | 变更原因 |
| `expected_pond_version` | integer | ✔ | 等于塘口当前 `row_version`；不匹配 → `CONFLICT`(409) | （前端自动带） |

依据：旧 `master_data_service.py:286-291`（`to_status` / `reason` 的校验与"1 到 500 字"约束）、`:297`（`expected_pond_version`）。**字段名原样继承**（旧前端已用 `to_status`）。
`pond_status_change.verify` **无额外字段**（只带全局 `expected_version`）——它校验的是申请单与塘口的**并发一致性**（§3.1-D），不是入参。

### 2.6 production

**`batch.create` / `batch.update`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `code` | string | ✔（create） | 1–64，企业内唯一 | 批次编号 |
| `name` | string | ✔（create） | 1–100 | 批次名称 |
| `pond_id` | ref | ✔（create） | `ref: {resource:"pond", label_key:"name"}` | 塘口 |
| `species` | string | ✔（create） | 1–64 | 品种 |
| `initial_quantity` | number | | 0 ≤ v ≤ 999999999999999.999 | 放苗数量 |
| `initial_weight_kg` | number | | 0 ≤ v ≤ 999999999999999.999 | 放苗重量（kg） |
| `stocked_at` | datetime | ✔（create） | 不得晚于当前时间 | 放苗时间 |
| `expected_harvest_date` | date | | 不得早于 `stocked_at` | 预计出塘日期 |
| `batch_status` | enum | | 更新时受转移表约束 | 批次状态 |
| `note` | text | | ≤500 | 备注 |

依据：旧 `production_service.py:27-32`（`batches` 字段集）。
- 数值上限 `999999999999999.999` 继承旧 `production_service.py:35`（`MAX_PRODUCTION_QUANTITY`），与 `DECIMAL(18,3)` 对齐。
- 日期顺序规则继承旧 `production_service.py:91-112`（`_validate_batch_dates`：出塘日期 ≥ 放苗日期、放苗不得晚于当前）。
- **删除 `organization_id/farm_id/area_id`**（§0.7 规则 1）。
- `row_version` 不出现在字段表（§0.7 规则 2），但出现在响应。

**`feeding.create`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `code` | string | ✔ | 1–64，企业内唯一 | 投喂单号 |
| `name` | string | ✔ | 1–100 | 投喂事项 |
| `pond_id` | ref | ✔ | `ref: {resource:"pond"}` | 塘口 |
| `batch_id` | ref | ✔ | 必须属于所选塘口 | 批次 |
| `material_id` | ref | ✔ | 物料须 `verified` 且类别含"饲料" | 饲料 |
| `quantity` | number | ✔ | > 0；与 `weight_kg` 至少填一个 | 投喂量 |
| `weight_kg` | number | | ≥ 0 | 重量（kg） |
| `happened_at` | datetime | ✔ | 不得晚于当前时间 | 投喂时间 |
| `note` | text | | ≤500 | 备注 |

依据：旧 `production_service.py:17-22`（`COMMON_FIELDS`）与 `production_relations.py:8-21`（`feed_plan_relation_valid` 校验"同企业、同塘口的已核验批次 + 类别含 feed 的可用物料"）。
**不继承的字段**：`feed_plan_id` / `feed_task_id` / `material_issue_request_id`（`production_service.py:20-21`）——前两者随 feed-plans/feed-tasks 砍除；`material_issue_request_id` 随 `issue-requests` 砍除（§1.6），由此**"投喂必须关联已核验领料申请且足额出库"这条旧规则消失**（§4 #17、Q9）。本版改为：投喂核验时**按物料类别与数量直接扣库存**，库存不足即拒绝（不变量的强制点从"申请额度"变为"实际库存"）。

**`harvest.create`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `code` | string | ✔ | 1–64，企业内唯一 | 出塘单号 |
| `name` | string | ✔ | 1–100 | 出塘事项 |
| `pond_id` | ref | ✔ | `ref: {resource:"pond"}`，塘口须 `verified` | 塘口 |
| `batch_id` | ref | ✔ | 必须属于所选塘口（继承旧 `production_relations` 的跨表一致性校验） | 批次 |
| `quantity` | number | ✔ | > 0；与 `weight_kg` 至少填一个；核验时须 ≤ 当前存塘 | 出塘数量 |
| `weight_kg` | number | | ≥ 0 | 重量（kg） |
| `happened_at` | datetime | ✔ | 不得晚于当前时间 | 出塘时间 |
| `note` | text | | ≤500 | 备注 |

依据：旧 `production_service.py:17-22` 的 `COMMON_FIELDS`（harvests 与其他生产单据共用同一字段集），字段名与投喂保持一致（`quantity` / `weight_kg` / `happened_at`），**继承不改名**。

**核验副作用（`harvest.verify`）**：向 `batch_stock_records` 追加**负向**行（`quantity_delta = -quantity`、`weight_delta_kg = -weight_kg`），先经不变量 §4 #2 的 `SELECT ... FOR UPDATE` 存量校验。这是**唯一**减少塘内存塘的能力（§3.2 可达性表）。

### 2.7 warehouse


**`receipt.create`**
| key | type | required | 约束 | label |
|---|---|---|---|---|
| `code` | string | ✔ | 1–64，企业内 + 单据类型内唯一 | 到货单号 |
| `name` | string | ✔ | 1–100 | 到货事项 |
| `warehouse_id` | ref | ✔ | 仓库须 `verified`（**原文写 `active` 是笔误，见下**） | 收货仓 |
| `material_id` | ref | ✔ | 物料须 `verified` 且与采购单一致 | 物料 |
| `purchase_order_id` | ref | | 若填，采购单须为 `approved`/`partially_received` | 采购单 |
| `lot_no` | string | ✔ | 1–64 | 物料批次号 |
| `production_date` | date | | | 生产日期 |
| `expiry_date` | date | | 不得早于 `production_date` | 有效期 |
| `quantity` | number | ✔ | > 0 | 到货数量 |
| `unit_cost` | number | ✔ | ≥ 0；若关联采购单须等于采购单价 | 单价 |
| `happened_at` | datetime | ✔ | 不得晚于当前时间 | 到货时间 |
| `note` | text | | ≤500 | 备注 |

依据：旧 `warehouse_service.py:13-19`（`FIELDS`，receipts 与 issues 共用同一字段集）。
- `lot_no` 必填继承旧 `warehouse_ledger_store.py:28-29`（`WAREHOUSE_LOT_REQUIRED`：入库核验必须填写物料批次）。
- `unit_cost` 必须等于采购单价继承旧 `purchase_posting.py:23-24`（`PURCHASE_RECEIPT_PRICE_MISMATCH`）。
- 删 `target_warehouse_id` / `source_document_id` / `task_id` / `scene` / `override_reason` / `correction_reason`（`warehouse_service.py:14-18`）：分别属调拨、单据关联、任务、场景、FEFO 覆盖、更正单，均已砍除（§6）。
- 删 `inventory_lot_id`：早期版本允许直接引用已有批次（`warehouse_ledger_store.py:26-27`），新系统统一按 `lot_no` 走 `INSERT ... ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)` 的幂等建批（继承旧 `warehouse_ledger_store.py:30-34` 的写法），字段从表单里消失。

**`warehouse.get`（t18 新增，只读）**

无入参（路径参数 `{warehouse_id}`），返回该仓库的完整记录（含 `version` /
`status_label` / `allowed_actions` / `is_default_label` 与 `area_name`）。

**`warehouse.create` / `warehouse.update` / `warehouse.archive`（t18 新增）**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `code` | string | ✔（create） | 1–64，**基地内**唯一（`uq_warehouses_farm_code`） | 仓库编号 |
| `name` | string | ✔（create） | 1–100 | 仓库名称 |
| `address` | string | | 1–200 | 地址 |
| `contact_name` | string | | 1–40 | 联系人 |
| `phone` | string | | 1–32 | 联系电话 |

> **`is_default` 刻意不开放给客户端。** 它决定 `feeding.verify` 从哪个仓扣库存
> （006 迁移解释了为什么它是显式列而不是「取 id 最小的仓」），
> 把它放进表单会让「投喂的库存从哪个仓出」变成可以被随手改的参数。
> 它的维护维持现状：由迁移种子决定。
>
> **`warehouse.create` 的初始 `status` 是 `draft`**（与区域/物料一致；
> 2026-09-15 改 —— 原先因为缺 `warehouse.verify` 而定成 `verified`，
> 现在补齐了 `warehouse.submit` / `warehouse.verify`，理由见 §1.6 的增补段）。
>
> **`warehouse.update` 的入参** = `name` / `address` / `contact_name` / `phone`（都可选）
> + `expected_version`；**`warehouse.archive`** 只带 `expected_version`。
>
### ⚠️ `warehouse_id` 的约束是 `verified`，不是 `active`（t18 更正）

本节原先两处（`receipt.create` / `issue.create`）都把 `warehouse_id` 的约束写成
「仓库须 `active`」。**`active` 这个取值在 `warehouses.status` 上不存在**：

* 006 迁移的 `chk_warehouses_status` 是
  `status IN ('draft','submitted','verified','archived')`；
* `receipt.verify` 的真实判定是 `warehouse_status != 'verified'` →
  `WAREHOUSE_NOT_VERIFIED`（`receipt_write.py`）；
* `ledger._default_warehouse_id` 也只在 `status = 'verified'` 的仓里选。

一个不存在的状态值写在权威清单里的后果不是"措辞不准"：读它的人会去把仓库的
状态改成 `active`（改不动，CHECK 会拒），或者以为 `verified` 的仓库不能收货。
**t18 把两处都改成 `verified`**，并保留这段更正——它不是补充说明，是裁决的一部分。

**`issue.create`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `code` | string | ✔ | 1–64 | 出库单号 |
| `name` | string | ✔ | 1–100 | 出库事项 |
| `warehouse_id` | ref | ✔ | 仓库须 `verified`（同 `receipt.create`） | 出库仓 |
| `material_id` | ref | ✔ | 物料须 `verified` | 物料 |
| `lot_no` | string | ✔ | 须为已存在的可用批次 | 物料批次号 |
| `quantity` | number | ✔ | > 0 | 出库数量 |
| `pond_id` | ref | | 领用去向（塘口） | 塘口 |
| `batch_id` | ref | | 领用去向（批次） | 批次 |
| `happened_at` | datetime | ✔ | 不得晚于当前时间 | 出库时间 |
| `note` | text | | ≤500 | 备注 |

**`issue.verify` 没有字段表**（只带全局的 `expected_version`）——这正是"不变量在能力上强制"的形态：不在入参里声明库存，而在执行时读库判定。

### 2.8 purchase

**`purchase_order.create` / `purchase_order.update`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `code` | string | ✔（create） | 1–64 | 采购单号 |
| `name` | string | ✔（create） | 1–100 | 采购事项 |
| `supplier_id` | ref | ✔（create） | `partner_type=supplier` | 供应商 |
| `material_id` | ref | ✔（create） | 物料须 `verified` | 采购物料 |
| `warehouse_id` | ref | ✔（create） | 收货仓 | 收货仓 |
| `quantity` | number | ✔（create） | > 0 | 采购数量 |
| `unit_price` | number | ✔（create） | > 0 | 单价 |
| `expected_delivery_date` | date | ✔（create） | 不得早于当前日期 | 预计到货日期 |
| `due_date` | date | ✔（create） | 不得早于 `expected_delivery_date` | 付款到期日 |
| `note` | text | | ≤500 | 备注 |

依据：旧 `purchase_service.py:11-14`。日期顺序规则继承旧 `purchase_service.py:52-60`（`_validate_dates`）。
**派生列（只读，`resources[].columns`）**：`supplier_name`、`material_name`、`warehouse_name`、`total_amount`（= quantity × unit_price）、`received_quantity`、`unpaid_amount`。这 6 个列名全部来自旧前端（`PurchasePage.vue:149`），必须原样保留。

**`payment.create`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `code` | string | ✔ | 1–64 | 付款单号 |
| `name` | string | ✔ | 1–100 | 付款事项 |
| `payable_id` | ref | ✔ | 应付须为 `unpaid`/`partial` | 应付来源 |
| `amount` | number | ✔ | > 0，且 ≤ 应付余额 | 付款金额 |
| `paid_at` | datetime | ✔ | 不得晚于当前时间 | 付款日期 |
| `payment_method` | enum | ✔ | `bank_transfer` / `cash` / `check` / `digital_wallet` / `other` | 付款方式 |
| `note` | text | | ≤500 | 备注 |

依据：旧 `purchase_service.py:15-17`。枚举值继承旧 `purchase_service.py:17`。

### 2.9 sales

**`sales_order.create` / `sales_order.update`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `code` | string | ✔（create） | 1–64 | 销售单号 |
| `name` | string | ✔（create） | 1–100 | 销售事项 |
| `customer_id` | ref | ✔（create） | `partner_type=customer` | 客户 |
| `pond_id` | ref | ✔（create） | | 塘口 |
| `batch_id` | ref | ✔（create） | 须属于所选塘口 | 批次 |
| `species` | string | ✔（create） | 1–64 | 品种 |
| `quantity` | number | ✔（create） | > 0 | 销售数量 |
| `unit` | enum | ✔（create） | `kg` / `jin` / `tail` | 计量单位 |
| `unit_price` | number | ✔（create） | > 0 | 单价 |
| `sold_at` | date | ✔（create） | | 销售日期 |
| `due_date` | date | ✔（create） | 不得早于 `sold_at` | 收款到期日 |
| `note` | text | | ≤500 | 备注 |

依据：旧 `sales_service.py:7`。`unit` 枚举继承旧 `sales_service.py:96-97`。日期顺序继承旧 `sales_service.py:39-46`。
**派生列（只读）**：`customer_name`、`pond_name`、`batch_code`、`delivered_quantity`、`total_amount`、`balance`——全部来自旧前端 `SalePage.vue:175`。

**`delivery.create`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `code` | string | ✔ | 1–64 | 交付单号 |
| `name` | string | ✔ | 1–100 | 交付事项 |
| `sales_order_id` | ref | ✔ | 销售单须为 `approved`/`partially_delivered` | 销售来源 |
| `unit` | enum | | `kg` / `jin` / `tail`；默认沿用销售单计量单位，填写时必须一致 | 计量单位 |
| `quantity` | number | ✔ | > 0，且累计不超过销售数量 | 交付数量 |
| `delivered_at` | datetime | ✔ | 不得晚于当前时间 | 交付时间 |
| `transport_info` | string | | ≤200 | 运输信息 |
| `acceptance_note` | text | | ≤500 | 验收说明 |
| `harvest_document_id` | ref | ✔ | 出塘单须为 `verified`、同批次同塘口；交付数量须与出塘事实一致（§4 #6） | 出塘单 |
| `note` | text | | ≤500 | 备注 |

依据：旧 `sales_service.py:8`。`sales_order_id` 的可选销售单范围继承旧前端 `SalePage.vue:46`（`['approved','partially_delivered']`）。
**`harvest_document_id`：已恢复为必填**（初稿曾删除它，理由是"出塘事实已砍"；该前提被 §7 Q9/Q10 推翻）。

**`sales_receipt.create`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `code` | string | ✔ | 1–64 | 收款单号 |
| `name` | string | ✔ | 1–100 | 收款事项 |
| `receivable_id` | ref | ✔ | 应收须为 `unpaid`/`partial` | 应收来源 |
| `amount` | number | ✔ | > 0，且 ≤ 应收余额 | 收款金额 |
| `received_at` | datetime | ✔ | 不得晚于当前时间 | 收款日期 |
| `receipt_method` | enum | ✔ | 同 `payment_method` 五值 | 收款方式 |
| `note` | text | | ≤500 | 备注 |

依据：旧 `sales_service.py:9-11`。字段名 `receipt_method` 与采购侧的 `payment_method` **故意不统一**——旧前端两个页面各自命名（`PayablePage.vue` 与 `ReceivablePage.vue` 的表单渲染分支分别用 `payment_method`/`receipt_method`），改名会破坏继承。

### 2.10 cost

**`cost.entry.create`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `category_code` | enum | ✔ | 须为启用的成本类别 | 成本类别 |
| `amount` | number | ✔ | > 0 | 金额 |
| `occurred_on` | date | ✔ | 所在期间须未关账 | 发生日期 |
| `period_start` | date | ✔ | ≤ `occurred_on` | 期间起 |
| `period_end` | date | ✔ | ≥ `occurred_on` | 期间止 |
| `source_type` | enum | ✔ | `manual_expense` / `warehouse_ledger` / `asset_depreciation`→（本版去掉后两项） | 来源类型 |
| `source_ref` | string | ✔ | 1–128 | 来源单号 |
| `target_type` | enum | | `farm` / `area` / `pond` / `batch` | 归属对象类型 |
| `target_id` | integer | | 与 `target_type` 同时出现 | 归属对象 |
| `note` | text | | ≤500 | 备注 |

依据：旧 `cost_enterprise_validation.py:10-14`。
- **删 `cost_nature`**（旧 `cost_enterprise_validation.py:11`）：成本性质（direct/public）在早期版本既存在于此字段、又存在类别表 `cost_categories.default_nature`、又在汇总时被重算（`cost_store.py:54-55` 按 `row["nature"]` 累加）。三处真相 → 保留类别表一处。
- **删 `source_detail`**（旧 `cost_enterprise_validation.py:12`）：未在旧前端出现。
- **删 `evidence_attachment_ids`**（旧 `cost_enterprise_validation.py:13`）：附件不做（§6）。
- `source_type` 只留 `manual_expense`（手工登记）与 `warehouse_ledger`（库存自动归集，由 `receipt.verify`/`issue.verify` 产生）；旧枚举里的 `asset_depreciation`/`adjustment` 随资产与调整单砍除。

**`cost.period.close`**

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `period` | string | ✔ | `YYYY-MM` | 会计期间 |
| `reason` | text | ✔ | 1–500 | 关账说明 |

### 2.11 通用 action 载荷

除上述字段表外，所有 `action` 能力统一接收：

| key | type | required | 约束 | label |
|---|---|---|---|---|
| `expected_version` | integer | ✔ | ≥1，且等于当前 `row_version` | （不出现在表单，前端自动带） |
| `reason` | text | 视能力而定 | 1–500 | 原因 / 说明 |

**`reason` 必填的能力**（继承早期版本的强制口径）：

| 能力 | 旧证据 |
|---|---|
| `purchase_order.cancel` | `早期版本：purchase_service.py:151-153`（`cancellation_reason` 必填） |
| `sales_order.cancel` | `早期版本：sales_service.py:127-128`（`cancellation_reason` 必填） |
| `cost.period.close` | 【新增设计】 |
| `access.user.status` | 【新增设计】 |

早期版本把这些字段命名为 `cancellation_reason` / `reversal_reason` / `correction_reason`（`purchase_service.py:151,228`、`sales_service.py:127`）。本版**统一为 `reason`**——理由是这些字段**不出现在旧前端的表单字段清单里**（`PurchasePage.vue:34-35`、`SalePage.vue:36,46` 均无此 key），属服务端内部命名，统一不破坏继承。

---

## 3. 状态机

### 3.0 三条设计规则

1. **状态机只有一份定义**：每个状态机的完整定义（状态码 / 中文 label / `row_actions` / 转移表）写在 capability 声明侧，由 `kernel/lifecycle.py` 持有。**不再出现 DB ENUM + 代码常量两处**。
   **早期版本反例**：同一状态机在三处独立定义——DB ENUM（`009_production.sql:18-19`、`010_warehouse.sql:71`、`011_purchase_payables.sql:20`、`013_sales_receivables.sql:22`）、通用策略表 `common/governance/lifecycle.py:48-61`、域常量（`production_service.py:15`、`master_data_service.py:21-24`）。
2. **DB 侧只用 `VARCHAR(32)` + 应用层校验**，不用 ENUM：早期版本每加一个状态就要一次 `ALTER TABLE ... MODIFY status ENUM(...)`（`012_purchase_hardening.sql:4` 加 `overpaid`、`032_agent_confirmation_failures.sql:2` 加 `failed`、`015` 加 `closed` 等，共 8 处 MODIFY），且 ENUM 与代码常量必须手工同步。
3. **`row_actions` 词汇表固定为旧前端已固化的 15 个词**（`早期版本：frontend/src/layers/common/api/lifecycle.models.ts:1`：`view | edit | delete | submit | approve | verify | confirm | correct | reverse | depreciate | dispatch | receive | cancel | handle | archive`）。本版**实际使用 7 个**：`view | edit | submit | approve | verify | cancel | archive`（`delete`/`correct`/`reverse`/`depreciate`/`dispatch`/`receive`/`handle` 随 §6 砍除的能力一起消失）。

### 3.1 pond（塘口）

**双状态资源**——`status` 是记录生命周期，`pond_status` 是业务状态。早期版本同样双字段（`早期版本：master_data_service.py:15` 与 `早期版本：008_master_data.sql:36`）。

**A. `status`（记录生命周期）**

| 状态码 | 中文 label | row_actions |
|---|---|---|
| `draft` | 草稿 | view, edit, submit |
| `submitted` | 待核验 | view, verify |
| `verified` | 已核验 | view, archive |
| `archived` | 已归档 | view |

转移与触发：

| from → to | 触发能力 |
|---|---|
| （新建）→ `draft` | `pond.create` |
| `draft` → `draft` | `pond.update`（编辑仍是草稿） |
| `draft` → `submitted` | `pond.submit` |
| `draft` → `archived` | `pond.update`（未提交草稿作废） |
| `submitted` → `verified` | `pond.verify` |
| `submitted` → `draft` | `pond.update`（退回编辑） |
| `verified` → `archived` | `pond.update` |

**与早期版本的差异**：

| 项 | 早期版本 | 本版 | 依据 |
|---|---|---|---|
| `draft` 的 `delete` 动作 | 有（物理删除 + FK 拒绝） | **无**，改为 archive | `早期版本：common/governance/lifecycle.py:49`；删除的早期实现在 `master_data_store.py:286-291`，靠唯一键/外键报错翻译 `DELETE_NOT_ALLOWED` |
| `verified` 的动作集 | `view, correct, reverse`（`lifecycle.py:51`） | **`view, archive`** | 更正/冲销机制砍除（§6） |
| `submitted` 的 `edit` | 有（`lifecycle.py:50` can_edit=True） | **有**，且编辑会写版本快照 | 继承 |
| 乐观锁 | `expected_version` | 同 | `早期版本：lifecycle.py:71-73` |

**B. `pond_status`（塘口业务状态）**

| 状态码 | 中文 label | 说明 |
|---|---|---|
| `build` | 待建设 | 新建塘口的默认状态 |
| `stocked` | 已放苗 | |
| `farming` | 养殖中 | |
| `rest` | 休整 | |
| `clean` | 清理中 | |
| `rebuild` | 改造中 | |

转移表（**原样继承**旧 `master_data_service.py:21-24`）：

| from → to | 触发能力 |
|---|---|
| `build` → `stocked` | `pond_status_change.request` + `pond_status_change.verify` |
| `stocked` → `farming` | `pond_status_change.request` + `pond_status_change.verify` |
| `farming` → `rest` | `pond_status_change.request` + `pond_status_change.verify` |
| `farming` → `clean` | `pond_status_change.request` + `pond_status_change.verify` |
| `rest` → `stocked` | `pond_status_change.request` + `pond_status_change.verify` |
| `rest` → `rebuild` | `pond_status_change.request` + `pond_status_change.verify` |
| `clean` → `rest` | `pond_status_change.request` + `pond_status_change.verify` |
| `clean` → `rebuild` | `pond_status_change.request` + `pond_status_change.verify` |
| `rebuild` → `build` | `pond_status_change.request` + `pond_status_change.verify` |

**每一个转移都必须经两步审批**：`pond_status_change.request` 建申请（写待办），`pond_status_change.verify` 由**另一人**核验后才真正改 `ponds.pond_status`。

**与早期版本的差异（裁决后）**：
- **两步审批：已恢复。**
  > **裁决（§7 Q2 / `DECISIONS.md` Q2）：保留两步**（`pond_status_change.request` + `pond_status_change.verify`），恢复唯一键 `uq_pond_status_active_request`（同一塘口只有一个待核验申请）。
  > 理由原文三条：① 需求明确写了"**审核**"属于高风险业务；② 塘口状态变更会解锁/锁死一批业务能力（`build → stocked` 才能建批次、`farming → rest` 会停掉投喂），属实质性的业务状态迁移；③ "同一塘口只能有一个待核验申请"是个**很好的不变量示范**——它证明系统能表达"集合上唯一"而不只是"字段非空"。
  >
  > **初稿设计（保留为历史记录，已被推翻）**：初稿把两步合并进 `pond.update` 的 `pond_status` 字段，并写明"本版是单步，代价是失去 maker-checker 与'唯一待审'约束"。该简化低估了业务状态迁移的实质影响。
  >
  > **施工影响**：`pond.update` **不再接受** `pond_status`（改为只读派生列）；业务状态只能经两步审批变更。状态机见下方 **D**。

**C. 与 `INTERFACES.md` 的冲突（已裁决，已修正）**

`INTERFACES.md:112-118` 初版给出的 `status_dict` 是：

```
build 待建设 / stocked 已放苗 / farming 养殖中 / rest 休整 / closed 已结束
```

而旧 DB ENUM 是 `ENUM('build','stocked','farming','rest','clean','rebuild')`（`早期版本：008_master_data.sql:36`），转移表也依赖 `clean`/`rebuild`。

**冲突点**：初版 `INTERFACES.md` 既**删掉了** `clean`/`rebuild`，又**新增了** `closed`——而 `closed` 在早期版本任何一个状态表里都不存在。

> **裁决（§7 Q1 / `DECISIONS.md` Q1）：采用旧 DB 的 6 态**，`INTERFACES.md` §2 的 `status_dict` 已修正（`DECISIONS.md` 待办第 1 项，标记"已完成"）。
> `DECISIONS.md` Q1 原文："`INTERFACES.md` 里我写的 `closed` 是我拍脑袋编的——它**在早期版本任何状态表里都不存在**，而 `clean`/`rebuild` 有生产验证的转移表依赖。**权威源应该是被生产验证过的枚举**。"
>
> **`closed` 的唯一归属**：`batch.batch_status` 的终态（§3.2-B），**不属于塘口**。任何在塘口状态里出现 `closed` 的实现都是错的。

**D. `pond_status_change_requests`（塘口状态变更申请）状态机**

| 状态码 | 中文 label | row_actions |
|---|---|---|
| `submitted` | 待核验 | view, verify, cancel |
| `verified` | 已核验 | view |

依据：`早期版本：019_enterprise_authorization_and_pond_status.sql:29`（`status ENUM('submitted','verified') NOT NULL DEFAULT 'submitted'`）。

| from → to | 触发能力 | 副作用 |
|---|---|---|
| （新建）→ `submitted` | `pond_status_change.request` | 写 `work_items` 待办（核验待办） |
| `submitted` → `verified` | `pond_status_change.verify` | ① 更新 `ponds.pond_status` = `to_status`；② 校验申请里的 `from_status` 与塘口当前 `pond_status` 一致；③ 释放唯一键，允许下一次申请 |

**约束**：同一 `pond_id` 在任一时刻最多只能有 **1 行** `status='submitted'`（不变量 §4 #21，DB 唯一键 + 应用层预检双保险）。
**并发保护**：`pond_status_change.verify` 必须校验申请里的 `from_status` 等于塘口当前 `pond_status`；不等则 `CONFLICT`(409)——防止"申请期间塘口状态被别的路径改掉"后错误套用旧申请。这是【新增设计】；早期版本用 `pond_version` 列做同类保护（`早期版本：019` 的 `pond_version`）。

### 3.2 batch（养殖批次）

**A. `status`（记录生命周期）**：与 pond 完全同构（`draft`/`submitted`/`verified`/`archived`），触发能力换成 `batch.*`。

**B. `batch_status`（批次业务状态）**

| 状态码 | 中文 label | row_actions |
|---|---|---|
| `stocked` | 已放苗 | view, edit |
| `farming` | 养殖中 | view, edit |
| `pending_settlement` | 待结算 | view, edit |
| `closed` | 已关闭 | view |

转移表（**原样继承**旧 `production_service.py:15`：`{"stocked": {"farming"}, "farming": {"pending_settlement"}, "pending_settlement": {"closed"}}`）：

| from → to | 触发能力 | 备注 |
|---|---|---|
| （核验）→ `stocked` | `batch.verify` | 核验时写 `stock_records` 初始存塘（旧 `production_store.py:248-249` 的 `stocking` 行） |
| `stocked` → `farming` | `batch.update` | 【新增设计】早期版本走独立接口 `change_batch_status`（`production_store.py:219-241`），本版并入 update 的 `batch_status` 字段 |
| `farming` → `pending_settlement` | `batch.update` | 早期版本此转移要求"批次无未完成生产业务"（`早期版本：production_store.py:225-227`，`BATCH_OPEN_WORK`）；本版保留该检查 |
| `pending_settlement` → `closed` | `batch.close` | 早期版本要求"存塘为 0"（`早期版本：production_store.py:228-235`，`BATCH_STOCK_NOT_ZERO`）。**可达性依赖 `harvests`（§7 Q10 裁决）**：投喂扣的是物料库存、不减塘内存塘，所以**唯一能减少存塘的是 `harvest.verify`**；若砍掉 `harvests`，本转移永远不可达、`batch.close` 成为"清单上看着有、实际不可执行"的能力。 |

**与早期版本的差异**：

| 项 | 早期版本 | 本版 |
|---|---|---|
| 状态枚举 | `stocked/farming/pending_settlement/closed`（`早期版本：009_production.sql:18`） | **不变** |
| 转移定义处数 | 2 处（DB ENUM + `production_service.py:15`） | 1 处 |
| 触发接口 | 独立的 `POST /batches/{id}/status`（`早期版本：product/production/routes.py:82-84`） | `batch.update` + `batch.close` |
| 进入 `pending_settlement` 的前置 | 无未完成生产业务（`production_store.py:225-227`） | 同 |

**生命周期可达性（Q10 裁决的直接结果）**：

| 转移 | 由谁提供存塘/状态变化 | 依赖的能力 |
|---|---|---|
| 核验 → `stocked` | 写 `batch_stock_records` 的 `stocking` 行（+quantity/+weight） | `batch.verify` |
| `stocked` → `farming` | 纯状态位，无账本变化 | `batch.update` |
| `farming` → `pending_settlement` | 纯状态位 + "无未完成生产业务"检查 | `batch.update` |
| `pending_settlement` → `closed` | **必须存塘归零**，而只有出塘会减塘内存塘 | **`harvest.verify`（+ `batch.close` 的前置校验）** |

> **结论**：`batch` 的 4 个状态**全部可达**。这条链的最后一环由 `harvests` 提供——`DECISIONS.md` Q10 记录的"永远不可达的能力比缺失的能力更糟"正是针对这一环。

### 3.3 feeding（投喂记录）

**`status`（两态，刻意不等同于主数据/订单类）**

| 状态码 | 中文 label | row_actions |
|---|---|---|
| `draft` | 草稿 | view, edit, submit→（无 submit，直接 verify）, verify |
| `verified` | 已核验 | view |

精确的 `row_actions`：`draft` → `[view, edit, verify]`；`verified` → `[view]`。

转移：

| from → to | 触发能力 | 副作用 |
|---|---|---|
| （新建）→ `draft` | `feeding.create` | 无 |
| `draft` → `draft` | `feeding.create` 的可编辑窗口（无 update 能力，改单=作废重开） | 无 |
| `draft` → `verified` | `feeding.verify` | ① 扣 `inventory_ledger`；② 写 `batch_stock_records`（投喂不改变存塘量，只记成本）；③ 生成 `cost_entries`（`source_type=warehouse_ledger`） |

**与早期版本的差异（重要）**：

| 项 | 早期版本 | 本版 | 依据 |
|---|---|---|---|
| 状态集 | `draft/submitted/verified/corrected/archived`（`早期版本：009_production.sql:19,66`） | **`draft/verified`** | 单据类资源不做 `submitted` 中间态——减少一次往返。理由：投喂的"提交"与"核验"在早期版本由同一角色完成（`早期版本：production_service.py:237-247` submit 与 `:249+` verify 的权限码同为 `production.verify`），中间态不产生额外管控价值 |
| `corrected` | 有（更正单 + `uq_production_documents_correction` 唯一键） | **无** | 更正单机制砍除（§6）；改单走"作废重开" |
| `archived` | 有 | **无** | 核验后的投喂记录是账本事实，不归档 |
| 领料申请前置 | 必须有已核验领料申请且足额出库（`早期版本：production_material_control.py:8-34`） | **无** | `issue-requests` 砍除；改为核验时直接判库存（§2.6） |

**A. `inventory_lots.status`（物料批次状态）**

| 状态码 | 中文 label |
|---|---|
| `available` | 可用 |
| `quarantined` | 隔离 |
| `expired` | 已过期 |
| `closed` | 已关闭 |

依据：`早期版本：010_warehouse.sql:29`。**本版只保留 `available` / `closed` 两个可设置值**；`quarantined` 与 `expired` 需要质量管控与到期任务，超出闭环（§6）。`issue.create` 的 `lot_no` 只能选 `available` 批次。
**与旧的差异**：早期版本还有 `expiry_date < CURRENT_DATE` 的运行时判定（`早期版本：warehouse_alert_store.py:116-117`）与 `expired` 状态的写回——本版把"过期"降级为**查询时计算**，不在状态机里。

**B. `inventory_ledger`（流水）**

**无状态**——它是只追加账本。早期版本靠唯一键 `uq_inventory_ledger_source_line` 保证"同一来源单据的同一行只追加一次"（`早期版本：010_warehouse.sql`）。本版继承该唯一键。
`source_type` 取值（本版）：`receipt`（入库）/ `issue`（出库）/ `correction`（**不做**）。

### 3.4 purchase_order（采购单）

| 状态码 | 中文 label | row_actions |
|---|---|---|
| `draft` | 草稿 | view, edit, submit, cancel |
| `submitted` | 待审批 | view, approve, cancel |
| `approved` | 已审批 | view, cancel |
| `partially_received` | 部分到货 | view |
| `fully_received` | 全部到货 | view |
| `cancelled` | 已取消 | view |
| `closed` | 已关闭 | view |

依据：`早期版本：011_purchase_payables.sql:20` 的 `ENUM('draft','submitted','approved','partially_received','fully_received','closed','cancelled','disputed')`。

转移与触发：

| from → to | 触发能力 |
|---|---|
| （新建）→ `draft` | `purchase_order.create` |
| `draft` → `submitted` | `purchase_order.submit` |
| `submitted` → `approved` | `purchase_order.approve` |
| `submitted` → `cancelled` | `purchase_order.cancel` |
| `approved` → `partially_received` | `receipt.verify`（累计到货 < 采购量） |
| `approved` → `fully_received` | `receipt.verify`（累计到货 = 采购量） |
| `partially_received` → `fully_received` | `receipt.verify` |
| `approved` / `partially_received` → `cancelled` | `purchase_order.cancel` |
| `fully_received` → `closed` | 【本版无对应能力——见下方差异表】 |

**与早期版本的差异**：

| 项 | 早期版本 | 本版 |
|---|---|---|
| `disputed` | 有（`011:20`） | **删除**：全仓无任何代码写入该状态（`purchase_store.py` 的 `set_order_status` 只处理 approve/cancel/receive 路径） |
| `closed` | 有（`011:20`） | **保留在枚举、但无触发能力**：早期版本的 `closed` 同样没有写入点；本版显式标注为"预留，不可达" |
| `receive` 动作 | `allowed_actions` 含 `receive`（旧 `frontend/.../lifecycle.models.ts:1`） | **删除**：收货由 `receipt.verify` 驱动，订单侧无 receive 动作 |
| 状态推进的写入点 | `purchase_posting.py:35-38`（到货时改订单状态） | 同，但改由 `receipt.verify` 的能力声明持有 |
| 经办人≠审批人 | **未实现**（早期版本 purchase 域无此校验） | **新增强制**（§4 #5，Q8） |

### 3.5 sales_order（销售单）

| 状态码 | 中文 label | row_actions |
|---|---|---|
| `draft` | 草稿 | view, edit, submit, cancel |
| `submitted` | 待审批 | view, approve, cancel |
| `approved` | 已审批 | view, cancel |
| `partially_delivered` | 部分交付 | view |
| `fully_delivered` | 全部交付 | view |
| `cancelled` | 已取消 | view |
| `closed` | 已关闭 | view |

依据：`早期版本：013_sales_receivables.sql:22`。

转移：与 purchase_order **完全同构**，把 `receipt.verify` 换成 `delivery.verify`，`partially_received`/`fully_received` 换成 `partially_delivered`/`fully_delivered`；状态推导公式原样继承旧 `sales_posting.py:38`（`fully_delivered` if delivered == ordered else `partially_delivered` if delivered else `approved`）。

**与早期版本的差异**：

| 项 | 早期版本 | 本版 |
|---|---|---|
| `disputed` | 有（`013:22`） | **删除**（同 purchase，无写入点） |
| 经办人≠审批人 | **已实现**（`早期版本：sales_service.py:121-122`） | 继承并**推广到全部审批类能力**（§4 #5） |
| 交付的 `harvest_document_id` | 强制绑定出塘单且数量逐字节相等（`早期版本：sales_source_control.py:10-33`） | **保留**。**裁决（§7 Q9 / `DECISIONS.md` Q9）：恢复"交付数量与出塘事实一致"的比对**，因此 `delivery.create` 恢复 `harvest_document_id` 字段（§2.9）。初稿曾以"出塘事实已砍"为由删除它——该前提已被 Q9/Q10 推翻 |
| 销售单的 `unit=tail` 分支 | 有（`早期版本：sales_source_control.py:22`） | 保留枚举，但不再有 "tail 用 quantity、其他用 weight_kg×2" 的换算逻辑 |

### 3.6 跨资源状态机一致性检查

| 检查项 | 结果 |
|---|---|
| 是否有资源的状态只出现在 DB 而不在能力声明里 | 否——本文件 §3 的 6 个业务状态机 + `pond_status_change_requests` 一个流程状态机即全部；早期版本 35 个 `status` ENUM 里，本版只用 12 个（`pond.status` / `pond.pond_status` / `pond_status_change_requests.status` / `batch.status` / `batch.batch_status` / `feeding.status` / `harvest.status` / `inventory_lots.status` / `purchase_order.status` / `sales_order.status` / `accounting_periods.status` / 单据二态共用体） |
| 单据类统一两态的资源 | feeding / harvest / receipt / issue / delivery / payment / sales_receipt —— **7 个**，全部 `draft → verified`（`harvest` 由 §7 Q9/Q10 恢复后加入） |
| 主数据与订单类统一四态的资源 | pond / batch / purchase_order / sales_order —— **4 个**，全部 `draft → submitted → verified(approved) → archived(closed/cancelled)` |
| **所有状态是否可达**（Q10 标准） | 是。逐条核验：`pond.pond_status` 6 态均有转移入口；`batch.batch_status` 4 态全部可达（依赖 `harvest.verify`，见 §3.2）；`pond_status_change_requests` 2 态可达；订单类 `closed` 是**唯一一处显式标注"预留、无触发能力"**的状态（§3.4），已在表中声明而非隐藏 |

**单据类与订单类的分类依据**【新增设计】：单据（receipt/issue/feeding/harvest/delivery/payment/sales_receipt）的"经办人录入 → 核验人核验"是**两段**，没有审批层；订单（purchase_order/sales_order）的"经办人提交 → 审批人审批 → 再执行"是**三段**，因为涉及金额承诺与外部供应商/客户。早期版本两类都套用同一套四态模板（`lifecycle.py:48-61`），导致 6 类单据多出一个永不被单独使用的 `submitted` 态。

---

## 4. 不变量清单

格式：**描述 → 强制能力 → 早期实现位置 → 新系统声明形态**。

`【新增设计】` 标记的声明形态是伪代码，用于表达**声明粒度**（group_by / 校验对象 / 比较对象），不是最终 API。

| # | 不变量 | 强制能力 | 早期实现位置 | 新系统声明形态 |
|---|---|---|---|---|
| 1 | **不得负库存** | `issue.verify` | `早期版本：warehouse_ledger_store.py:181-202` `_validate_negative()`：对每条负向 movement 先 `SELECT ... FOR UPDATE` 再比较 `-quantity > available` | `invariant=[NoNegativeStock(table="inventory_ledger", group_by=["warehouse_id","material_id","inventory_lot_id"], column="quantity_delta")]` |
| 2 | **不得负存塘** | `batch.verify` / `feeding.verify` / `harvest.verify` | `早期版本：production_store.py:273-280` `_post_stock()`：逐行 `SELECT COALESCE(SUM(quantity_delta),0) ... FOR UPDATE` 后比较 `-quantity > available` → `BATCH_STOCK_INSUFFICIENT` | `invariant=[NoNegativeStock(table="batch_stock_records", group_by=["batch_id","pond_id"], columns=["quantity_delta","weight_delta_kg"])]` |
| 3 | **未审批不得收货** | `receipt.create` + `receipt.verify` | `早期版本：purchase_posting.py:19-20`：`order["status"] not in {"approved","partially_received","fully_received"} → PURCHASE_ORDER_NOT_RECEIVABLE` | `invariant=[ReferencedStatus(field="purchase_order_id", table="purchase_orders", in_=["approved","partially_received"])]` |
| 4 | **付款不得超过余额** | `payment.verify` | `早期版本：purchase_payment_store.py:114-115`（create 时）**与** `:154-157`（verify 时）两处重复 | `invariant=[AmountWithin(amount_field="amount", balance_of="payable.balance", where="payable.status IN ('unpaid','partial')")]` |
| 5 | **经办人 ≠ 审批人**（**已推广，合规规则统一**） | `purchase_order.approve`、`sales_order.approve`、`receipt.verify`、`issue.verify`、`payment.verify`、`sales_receipt.verify`、`delivery.verify`、`pond.verify`、`material.verify`、`warehouse.verify`、`batch.verify`、`feeding.verify`、`cost.entry.confirm`、`harvest.verify`、`pond_status_change.verify` —— **共 15 个核验/审批能力**（2026-09-15 补 `material.verify` 与 `warehouse.verify`：前者实测可自审、后者为当前迭代新增的仓库核验）（Q8 初裁 11 条 = 上表前 11 条；`harvest.verify` 与 `pond_status_change.verify` 按 Q8 自身原则并入，见 §7 Q8′ 的确认结论） | **仅** `早期版本：sales_service.py:121-122` 一处实现（`SELF_APPROVAL_FORBIDDEN`，比较 `created_by`/`updated_by` 与当前用户）。采购、仓储、生产、成本、主数据**全部缺失** —— 这正是重写要根除的"合规规则跨域不一致" | `invariant=[DistinctActors(creator_field="created_by", verifier_field="verified_by")]`，由内核统一执行——**人工页面与 Agent 走的是同一份判断**。<br>**裁决（§7 Q8 / `DECISIONS.md` Q8）三条**：① 推广到**全部 13 条**核验/审批能力（上表清单；Q8 初裁为 11 条，`harvest.verify` 与 `pond_status_change.verify` 于 Q8′ 并入）；② 比较字段固定为 `created_by` 与 `verified_by`（**比早期版本比 `updated_by` 更严格**）；③ **不留"同一人可自审"的例外**——"留例外会让'规则'退化成'建议'"。<br>✅ **一致性缺口已闭合（§7 Q8′ 已确认）**：`harvest.verify` 与 `pond_status_change.verify` 已并入本行清单，**共 13 条**。
**该清单必须完备**：Q8 的原则是"推广到全部核验/审批能力"且"留例外等于降级成建议"，因此**任何新增的核验/审批类能力都必须同时进本行**；漏掉一条不是"少覆盖一条"，而是把这条合规规则降级成建议。机械核对方式：核验/审批能力集合可由能力命名（`*.verify` / `*.approve` / `*.confirm`）与实际声明交叉验证，`tools/registry_reconcile.py` 的 [B]/[D] 两张表会报出"挂了 `DistinctActors` 但文档没列"或反之的能力。<br>**逐能力（本行真正的类型归属）**：<br>|

| 能力 | 本行真正强制它的类型 |
|---|---|
| `pond_status_change.verify` | **替代强制**（服务层显式判定）——`DistinctActors` 在它身上**结构上不适用**：本能力的 `before` 快照来自 `PondService.load_pond`（**塘口行**，上面没有 `created_by`），而这一条要比的"经办人"是**申请行**的 `requested_by`；`DistinctActors.check()` 读到 `before["created_by"]` 为 `None` 就按"该字段缺失"跳过，挂上去等于一条永不生效的声明。强制点是 `ponds_write.py::verify_pond_status_change` 里的显式判定，它抛**同一个契约**（`FORBIDDEN` + `data.rule == "DISTINCT_ACTORS"`），e2e 有断言：`tools/master_data_e2e.py` §7「申请人不能核验自己的状态变更申请」。 |

| 6 | **交付数量与出塘事实一致**（**已恢复**） | `delivery.create` | `早期版本：sales_source_control.py:22-24`：`harvest.weight_kg × (2 if unit=="jin" else 1)` 或 `harvest.quantity`（tail），必须**等于**交付数量 | `invariant=[HarvestQuantityMatch(harvest_field="harvest_document_id", order_field="unit", tolerance="exact")]`【新增设计】<br>**裁决（§7 Q9 / `DECISIONS.md` Q9）：恢复本不变量**——出塘事实源随 `harvests` 一并恢复（§1.5），比较对象是 `harvest`。<br>**初稿状态（历史记录）**：初稿标为"**降级**：出塘事实砍除，替代形态待裁决 → Q9"，并拟用 `CumulativeWithin` 放弃"逐字节一致"。该降级已撤销。 |
| 7 | **已关账期间不得再归集成本**（**覆盖范围已扩展**） | `cost.entry.create`、`cost.entry.confirm`、`feeding.verify`、`receipt.verify`、`issue.verify`、`delivery.verify` —— **共 6 条** | `早期版本：cost_enterprise_repository.py:86-92` `require_unlocked()`：查 `cost_settlements` 已确认且期间重叠 → `COST_PERIOD_LOCKED`。**早期版本只在手工录入入口调用**（`早期版本：cost_store.py:71,79` 的两处 `require_entry_open`） | `invariant=[PeriodOpen(table="accounting_periods", date_field="occurred_on", status="open")]`<br>**裁决（§7 Q13 / `DECISIONS.md` Q13 第 3 点）：把 `PeriodOpen` 挂到全部 4 个会写成本的能力上**（`feeding.verify` / `receipt.verify` / `issue.verify` / `delivery.verify`），**加上原有的 `cost.entry.create` / `cost.entry.confirm`**。`DECISIONS.md` 原文："一个不变量覆盖 4 条能力，比挂在 1 条手工入口上强得多——这是架构升级带来的收益，不应该反过来被当成删除的理由。"<br>**这是比早期版本更强的做法**：早期版本只挡手工路径，派生路径可以绕过期间锁定；新系统两条路径都被挡。<br>**范围口径（已定稿）**：本行列的是**会写账目的能力**，共 6 条。「已关账期间不得再写账目」这条规则的**反向动作**是「把期间关掉」，它执行完之后该期间的状态**必然**是 `closed`——给它挂上本规则，规则会在它自己的结果上失败（实测：关账永远无法成功）。因此**关账动作不在本行范围内**，它属于「开锁」而不是「记账」。关账自身的期间约束由三处更强的保证承担：服务层 `_require_open_period` 预检 + `UPDATE ... WHERE status='open'` 原子占位 + DB CHECK。**这不是例外，而是范围划分**：见本表之后的「规则方向与适用范围」。 |
| 8 | **同一塘口/批次同期不得重复归集库存成本**（**已恢复强制**） | `cost.entry.create` | `早期版本：cost_enterprise_repository.py:117-150` `require_source_not_duplicated()`：`source_type='manual_expense'` + 类别 ∈ `{feed,seed,health}` + `target_type ∈ {pond,batch}` + 期间重叠 + 已有 `warehouse_ledger` 来源 → `COST_SOURCE_DUPLICATED` | `invariant=[NoOverlappingSource(source_types=["manual_expense","warehouse_ledger"], target_fields=["target_type","target_id"], period_fields=["period_start","period_end"])]`<br>**裁决（§7 Q13）：随 `cost.entry.create` 的恢复，本不变量重新获得强制点。**<br>**初稿状态（历史记录）**：初稿曾标"方案 B 下失去强制能力"，理由是"纯派生后结构上不可能重复"。该理由在**恢复手工录入后即不成立**——手工费用与库存自动成本对同一塘口/批次/期间重复计入的风险原样回来了（早期实现的白名单 `manual_feed_offset` / `manual_feed_direct` 正是为这条规则服务的）。<br>**⭐ 规则口径（2026-09-13 补，本节为权威解释）**：**本规则是"跨来源类型"的互斥，不是"同来源类型不得两笔"。**<br>判据取自早期实现原文：触发条件是"**本次是 `manual_expense` 且已有 `warehouse_ledger` 来源**"——两个**不同**来源类型对同一归属对象、同一期间并存才算重复；反方向同理，所以实现里是**两个方向各一个不变量实例**。<br>**同一来源类型下的不同来源单号是两笔不同的业务事实**：`004_cost.sql` 的物理去重键 `uq_cost_entries_org_dedupe` 走的生成列 `ledger_dedupe_key` **含 `source_ref`**（迁移注释原文："同一归属对象、同一期间、**同一来源单号**只能有一条自动归集记录"）。即"两笔库存单各自归集到同一塘口/期间"是**允许**的；要防的是"**同一笔**业务事实被归集两次"。<br>**实现口径（2026-09-13 收口，与上段一致）**：谓词里带 `source_type <> 本次的来源类型`，因此**只有跨来源类型才判互斥**；同来源类型的不同 `source_ref` 一律放行。两侧都有 e2e 反例（`tools/cost_e2e.py` §7）："已有库存归集时手工费用被拒"与"已有手工费用时库存归集被拒"（跨类型，必须拒）、"同归属对象可有多笔不同来源单号"（同类型，必须放行）。 |
| 9 | **累计到货不得超过采购数量** | `receipt.verify` | `早期版本：purchase_posting.py:25-34`：`SUM(d.quantity − o.quantity)` 计算已到货，`received > ordered → PURCHASE_RECEIPT_EXCEEDS_ORDER` | `invariant=[CumulativeWithin(target="purchase_order.quantity", children="receipts.status='verified'")]` |
| 10 | **累计交付不得超过销售数量** | `delivery.create` + `delivery.verify` | `早期版本：sales_posting.py:35-37`：`delivered < 0 or delivered > ordered → SALES_DELIVERY_EXCEEDS_ORDER` | `invariant=[CumulativeWithin(target="sales_order.quantity", children="deliveries.status='verified'")]` |
| 11 | **批次关闭时存塘必须为 0** | `batch.close` | `早期版本：production_store.py:228-235`：`SUM(quantity_delta) != 0 or SUM(weight_delta_kg) != 0 → BATCH_STOCK_NOT_ZERO` | `invariant=[ZeroBalance(table="batch_stock_records", group_by=["batch_id"], columns=["quantity_delta","weight_delta_kg"])]` |
| 12 | **核验后记录只读** | `pond.update`、`purchase_order.update`、`sales_order.update` —— **共 3 条**（**本条由散文式范围改为明确清单**，理由见右栏） | `早期版本：common/governance/lifecycle.py:76-78` `require_editable()`：`status` 不在可编辑集 → `RECORD_READ_ONLY`；策略表 `:48-61` | `invariant=[StatusAllowsEdit(statuses=["draft","submitted"])]`<br>**为什么把"所有 `*.update`"收窄成 3 条**：这 3 张表有 `draft → submitted → verified → archived` 的记录生命周期，才有"核验后只读"可言；`batch.update` 同样 `kind=update`，但它改的是**批次**（记录生命周期只有 `draft`/`verified`，没有 `submitted` 中间态），挂 `StatusAllowsEdit(statuses=("draft","submitted"))` 会是一句在它身上永不为假的话 —— 声明式规则要挂在它真正管的那件事上。**编号规格**：本行的强制范围原先写成散文式"所有 `*.update`"，对账工具按 `kind` 展开后**包含 `batch.update`**，于是报出一条假缺口；改成明确清单后，`[B2]` 的展开不再越界。 |
| 13 | **乐观锁** | 所有 `*.update` 与写 `action` | `早期版本：lifecycle.py:71-73` `verify_version()` + 76 处 `UPDATE ... SET row_version=row_version+1 WHERE ... AND row_version=%s` + `rowcount != 1` 判定（如 `production_store.py:167-169`） | `invariant=[OptimisticLock(column="row_version", expect="expected_version")]` |
| 14 | **状态转移合法** | 所有状态迁移能力 | 早期版本 **3 处不一致的定义**：DB ENUM（`009:18-19` 等）、`lifecycle.py:48-61` 的 `POLICIES`、域常量（`production_service.py:15`、`master_data_service.py:21-24`） | `invariant=[StateTransition(machine="pond|batch|feeding|purchase_order|sales_order|inventory_lot")]` |
| 15 | ~~核验必须有凭据~~ → **核验必须有来源单号**（**已裁决降级**） | `cost.entry.confirm` | `早期版本：cost_enterprise_repository.py:80-83` `require_evidence()`：无凭据 → 422 `EVIDENCE_REQUIRED`；凭据绑定校验在 `common/files/evidence.py:37-66` | `invariant=[RequiredField(fields=["source_ref"])]`<br>**裁决（§7 Q5 / `DECISIONS.md` Q5）：接受降级。** 理由原文："P0 不做附件（§6 已定），'核验必须有凭据'失去载体。用'必填来源单号'替代，能保住'每笔成本都可追溯到底单'这个核心价值。"<br>**记为已知降级**（须写入最终交付报告的"尚未完成的问题"）：附件能力是可恢复的——后续加 `attachment.*` 能力即可把本不变量升级回"必须有凭据"。 |
| 16 | **投喂量或重量至少填一个** | `feeding.create` | `早期版本：production_validation.py` 的 `require_stock_measurement()`（在 `production_service.py:162,202,228,242` 四处被调用） | `invariant=[AtLeastOneOf(fields=["quantity","weight_kg"])]` |
| 17 | ~~投喂必须有足额已核验领料申请~~ | `feeding.verify`（原） | `早期版本：production_material_control.py:8-34` `require_material_issue()`：一条 5 表 JOIN 的 SQL 校验 `issued − consumed >= quantity` | **降级/替换**：`issue-requests` 砍除 → 改为 `invariant=[NoNegativeStock(...)]`（即 #1 在投喂路径上的复用）。旧规则比新规则更严格（前置申请额度 vs 事后库存检查） |
| 18 | **物料/仓库/批次必须同企业且状态有效** | `receipt.create`、`issue.create`、`feeding.create`、`purchase_order.create`、`sales_order.create` | `早期版本：warehouse_store.py:73-97` `_scoped()`（三次回查 organization/farm/area + 物料 status）+ `:184-193`（核验时再查仓库 active + 物料 verified） | `invariant=[SameTenant(fields=["warehouse_id","material_id","pond_id","batch_id"]), ReferencedStatus(field="material_id", table="materials", in_=["verified"])]` |
| 19 | **编码在范围内唯一** | 所有 `*.create` | DB 唯一键一族：`uq_ponds_farm_code`、`uq_materials_organization_code`、`uq_production_batches_org_code`、`uq_warehouse_documents_org_type_code`、`uq_purchase_orders_org_code`、`uq_sales_orders_org_code`（`早期版本：008/009/010/011/013` 各迁移） | `invariant=[UniqueCode(fields=["code"], scope=["organization_id"])]` —— 由 DB 唯一键兜底 + 应用层翻译为 `CONFLICT`(409)。**禁止**像早期版本那样靠异常消息文本判断（`早期版本：production_store.py:142-146`、`warehouse_store.py:138,155` 的 `if key not in str(exc)`） |
| 20 | **塘口状态转移合法**（**恢复两步审批**） | `pond_status_change.request` + `pond_status_change.verify` | `早期版本：master_data_service.py:21-24`（转移表）+ `:288-289`（`INVALID_POND_STATUS_TRANSITION`）+ `:290-291`（原因必填 1–500 字） | **本行横跨两步，两步各自挂的类型不同**（见右栏末的逐能力表）：申请步负责"理由必填"，核验步负责"状态真的发生转移"。`invariant=[StateTransition(machine="pond_status"), RequiredField(fields=["reason"])]`<br>**裁决（§7 Q2 / `DECISIONS.md` Q2）：保留两步审批**，「pond.update」**不再接受** `pond_status` 字段。<br>**初稿状态（历史记录）**：初稿把两步合并为 `pond.update` 的单字段写入。<br>**逐能力（本行真正的类型归属，见下方小表；`StateTransition` 属核验步、`RequiredField(reason)` 属申请步 —— 核验只是批，理由在申请时已记下，两步互相挂对方的类型会造出两条永不触发的声明）** |

| 能力 | 本行真正强制它的类型 |
|---|---|
| `pond_status_change.request` | `RequiredField` |
| `pond_status_change.verify` | `StateTransition` |

| 21 | **同一塘口只能有一个待核验状态变更**〔新增设计〕 | `pond_status_change.request` | 早期版本靠 DB 唯一键 `uq_pond_status_active_request` 强制（`早期版本：019_enterprise_authorization_and_pond_status.sql`），应用层另有显式预检（`早期版本：pond_status_store.py:35-37` 的 `POND_STATUS_CHANGE_PENDING`） | `invariant=[AtMostOnePending(dims=["pond_id"], table="pond_status_change_requests", status_field="status", pending_states=["submitted"])]` —— **逐能力归属见右栏末的小表**（下面这段说明里出现的 `AtMostOnePending` **不是**本能力挂的）。<br>**「替代强制」**：`pond_status_change.request` 上**没有**不变量强制点 —— 它的 `pond_id` 只在**路径**里、不在字段 schema 里，而该类型的触发条件是"payload 里带齐 `dims`"，挂上去就是一条永不触发的声明；而且它**不可达**：状态机里每个塘口状态的出边只有 1–2 条，同一塘口的第二次申请会先被 `require_transition` 拒。它的强制点是 **DB 唯一键** `uq_pond_status_active_request`（走生成列 `active_pond_id`，`status='submitted'` 时等于 `pond_id`；并发下唯一可信的判定）+ **服务预检**（可读的 409 文案）。<br>**指名能力**：`pond.create`、`pond_status_change.request`（e2e：`tools/master_data_e2e.py` §7「数据库唯一键拦住第二条待核验申请（生成列 active_pond_id）」与「状态变更申请被接受（只建申请，不改状态）」）。对账工具把替代强制那部分归入 **[B-声明]（列出供复核、不报警）**。<br>**来源（§7 Q2 / `DECISIONS.md` Q2 第 3 点）**：`DECISIONS.md` 原文称它是"个**很好的不变量示范**——它证明系统能表达'集合上唯一'而不只是'字段非空'"。**施工要求**：DB 唯一键与应用层预检**都要有**。 |

| 能力 | 本行真正强制它的类型 |
|---|---|
| `pond.create` | `AtMostOnePending`——本次写入的行就是**塘口**，`dims=("pond_id",)` 能取到值、判定真的会执行；正常路径（新塘口）必然放行，不会拦住合法创建 |
| `pond_status_change.request` | **替代强制**（DB 唯一键 + 服务预检；不变量层在这条能力上结构上挂不了、也不可达，理由见上） |
| 22 | **取消必须填原因**〔补收〕 | `purchase_order.cancel`、`sales_order.cancel` | 早期版本两处各实现了一遍：`早期版本：purchase_service.py:151-153`（采购取消缺原因 → 拒绝）、`早期版本：sales_service.py:127-128`（销售取消缺原因 → 拒绝）。同一家公司里两条取消路径各写一遍，正是本系统要根除的"合规规则跨域不一致" | `invariant=[RequiredWhen(when_field="status", when_values=("cancelled",), required_fields=("cancel_reason",))]`<br>**来源**：`docs/INVARIANT_TYPES.md:82` 已把 `RequiredWhen` 的用途登记为"取消必须填原因"，但 §4 原先的 21 条把它漏掉了——内核因此出现一种"实现了、却没有规则援引"的类型。本行是补收，不是新增设计。<br>**施工要求**：`when_field="status"` 按**目标状态**判定，目标状态由服务回传；声明处让规则可见，服务层负责字段级文案，DB 的 `chk_*_cancel_reason` 保证脏数据写不进去（三者不是互相重复的判定，各自守一层）。 |

**不变量与能力的覆盖检查（裁决后）**：共 **22** 条不变量，其中 **20 条落在 §1 的 69 个能力上**、**2 条降级**（#15 成本凭据、#17 领料申请），两条均在 §7 有对应裁决项（Q5 / Q9）。（#22「取消必须填原因」为补收：内核早已实现 `RequiredWhen`，本文档漏收该行，见 §4 #22 的说明。）本条之外，其余表述不变。
### 4.1 规则方向与适用范围（哪类规则不该挂在某些能力上）

21 条规则的绝大多数是**单向**的：它们约束"这次写入之后，业务事实是否自洽"。但有三类规则
会因为方向不同而**天然不适用于某些能力**，把它们一律挂上去不是"更严格"，而是**让能力永远不可达**。
本节把这三类写清楚——**写清分类，而不是逐个记"已知偏离"**：前者让后来者知道"该怎么划范围"，
后者只让他们知道"这里可以破例一次"。

**第一类：反向动作不适用（结构性）**
规则约束的是"某个状态下的行为"，而**改变该状态**的那个能力天然不在范围内。典型是 §4 #7：
「已关账期间不得再写账目」的反向动作是「把期间关掉」，它执行完后期间状态必然是 `closed`，
挂上规则就会在自己的结果上失败。判据：**如果某能力的作用就是让规则的前提变为不成立，它就不在范围内。**
这一类的处置是**范围划分**（在 §4 里写清范围与理由），不是记偏离。

**第二类：前置条件由更强机制承担（可替代）**
有些规则防的是"脏数据被写进去"，而该保证已经由**数据库约束或原子操作**以更强的形式给出时，
应用层不变量只是重复表述。判据：**存在 DB 唯一键 / CHECK / `WHERE 状态=…` 原子占位时，
优先依赖它们**（并发下"先查再写"本来就不可能保证唯一性）。§4 #19 已经写明了这个口径：
"由 DB 唯一键兜底 + 应用层翻译为 409"。这一类的处置是**在规则行里指认出更强的机制**。

**第三类：后验语义的算术后果（需显式声明，不得默认）**
不变量在业务写入**之后**执行（`runner.py`：`_call_service` → `run_invariants`）。因此凡是
"读取某张表的汇总值再判定"的规则，读到的都是**本次写入之后**的值。这对 `NoNegativeStock`
是正确的（它要判的就是后验余额），但对"判增量"的写法会**重复计入本次数值**
（旧形状 `available + delta` 在 available 已含本次增量时，实际算的是 `before + 2×delta`）。
判据：**声明这类规则时必须写明判的是"后验值"还是"增量"**，二选一，不许含糊。
这一类的处置是**在类型 docstring 与能力声明处写明口径**（`INVARIANT_CONTEXT.md` 已记录各类型的实测语义）。

**第四类（系统性盲区）：租户维度从未被真实行使**

DataScope 的判据来自 `kernel/scope.py::SCOPE_COLUMN`，它只覆盖 `farm / area / pond /
personal` 四列，**不包含 `organization_id`** —— 也就是说 DataScope 行使的是"**区域**"，
不是"**租户**"。所以"租户隔离"不能靠 DataScope 兜底，遇到问题必须逐条问
"**这条规则的谓词里有没有租户键**"：

* 所有域的 e2e 都跑在**单企业**库里，`organization_id` 在每张表上都有、却从未被行使过一次；
* `PeriodOpen` 曾经是这条盲区的**实证样本**：它的谓词没有租户键、`LIMIT 1` 也不带 `ORDER BY`，
  于是"命中哪一行"由存储顺序决定 —— **命中别家已关账 → 误拦（吵闹）**、
  **命中别家是 open → 静默放行已关账期间（安静，更危险）**，两方向都实测复现过。
  **该缺陷已修**（t2）：现在谓词带 `organization_id`（声明形态 `tenant_keys`）、缺键按 R2
  显式 `INTERNAL_ERROR`、`LIMIT 1` 带 `ORDER BY period_start DESC, id DESC`。
  回归守卫 `tools/repro_period_open_tenant.py`（8 条断言）与 `tests/test_invariants.py`
  的 8 条 `PeriodOpen` 用例守着它；`[G1]` 因此从 2 个成员变成 3 个。
* `NoOverlappingSource` 缺分租键时同样会把范围放宽到跨企业（该条已按 R2 收紧为 `INTERNAL_ERROR`）。

判据与优先级清单由对账工具的 **[G]** 表产出：从内核源码切出每个类型，看它**自己**有没有
任何租户键（`organization_id` / `tenant` / …）。**以工具输出为唯一事实源，不在本文重述成员
与计数**（两处描述同一件事，必然漂移 —— 本节此前手写的"只有 5 种提到租户、其余 13 种盲"
就是这样过期的）。当前读数：`[G1]` 3 个成员（`PeriodOpen` / `SameTenant` /
`NoOverlappingSource`）、`[G2]` 4 个（委托 Scope，**继承同一盲区**）、`[G3]` 12 个。
**盲不等于一定越界**（有些规则只比同一行的两列），但它是一份**必须先回答"谓词里有没有
租户键"的清单**。这也是"多租户 e2e"任务的立项依据：缺的是**测试维度**，不是测试用例。

**一句话口径**：规则与能力的关系要能写成"**因为 X，所以这条规则覆盖/不覆盖某能力**"，
而不是"这条规则本该覆盖，但这里先放它过去"。后者一次就会被复制成第二次。

**没有任何一条不变量"声明了但没有强制能力"**——这是本清单可验证的闭合条件。对照初稿：初稿 20 条中仅 17 条落在能力上、3 条降级（#6、#15、#17）；本次修订因 Q9 恢复 `harvests` 而**救回 #6**，因 Q13 而**强化 #7**，并新增 **#21**。

---

## 5. Human-Only 清单及理由

### 5.1 判定口径（严格遵循用户原话）

允许 `human_only` 的**方向只有六个**（`ARCHITECTURE.md:242` + 用户指示）：
① 修改权限模型；② 给用户分配系统级角色；③ 修改 Tool Registry；④ 修改安全策略；⑤ 系统级危险配置；⑥ 身份/会话生命周期。

**不在上述方向的能力，本文件一律不标 `human_only`。** 有倾向的单独列在 §5.3 交由 评审结论。

### 5.2 `agent_exposure=human_only` 的能力（运行时实测 **1** 条）

> ## ⚠️ 本节口径已按**运行时实测**重写（2026-09-14 复核）
>
> **实测读数**：注册表 **102** 条能力 = `exposed` **101** + `human_only` **1** + `hidden` **0**
> （复跑：`python -c "…REGISTRY.all()…"` 或见 `tests/test_agent_exposure_doc_consistency.py`）。
>
> 本节标题原先写"共 **7** 条"，**其中 6 条与运行时不符** —— 逐条对照：
>
> | 原先列入本节的能力 | 运行时实测 | 说明 |
> |---|---|---|
> | `auth.login` | **不在注册表内** | 它是 Flask 固定路由（`web/routes_auth.py`），不经能力执行器。Agent 没有寻址入口，保护来自"根本没注册"，而不是"标了 human_only" |
> | `auth.logout` | **不在注册表内** | 同上 |
> | `auth.password.change` | `human_only` ✅ | **本节唯一与实际相符的一条** |
> | `access.user.create` | `exposed` + `risk=high` + `confirmation=always` | 见下"为什么改回 exposed" |
> | `access.user.status` | `exposed` + `risk=high` + `confirmation=always` | 同上 |
> | `access.user.grants` | `exposed` + `risk=high` + `confirmation=always` | 同上 |
> | `access.role.permissions` | `exposed` + `risk=high` + `confirmation=always` | 同上 |
>
> **为什么那 4 条改回 `exposed`**：依据
> `docs/DECISIONS.md` 第 9 条 ——
> 「账号、角色等管理能力**改为 `EXPOSED`**，仍由 Runner、Service 重新校验 RBAC，
> 权限/身份及不可逆能力强制服务端 HITL」。即管控手段从"Agent 完全不可见"换成
> **三层**：L1 权限码（`auth.user.manage` / `auth.role.manage`）
> → 服务层 `_require_super_admin`（`domains/access/admin.py`）
> → 服务端 HITL 确认卡（`confirmation=always`）。
>
> **这条漂移为什么长期无人发现**：`tools/registry_reconcile.py` 逐能力只比对
> `name / kind / method / path / risk / audit`，`agent_exposure` 出现 **0** 次 ⇒
> 对账全绿。已补守卫 `tests/test_agent_exposure_doc_consistency.py`（把本节清单与运行时
> 逐名比对）与"默认值 `HIDDEN` 一次都没生效"的断言。
>
> ---
>
> 下面这张表保留为本节的**原始设计意图**（写于能力规模 64 条时），
> 阅读时请以上面的实测对照为准。

### 5.2.1 原始设计意图（与运行时不符，仅存档）

| name | 方向归类 | 理由 |
|---|---|---|
| `auth.login` | ⑥ 身份/会话生命周期 | 登录会创建新会话、轮换令牌、累计失败计数并可能锁定账号（旧 `auth_service.py:21-30` 的完整链路）。让模型代登录等于把"当前操作者是谁"交给模型决定——L1/L2/L3 三层权限校验的前提（`ARCHITECTURE.md:204-211`）正是"会话属于真人"。早期版本已把改身份/会话的操作标为 `human_only` 并返回 `HUMAN_REQUIRED`（`早期版本：agent_gateway_service.py:74-76`），新系统继承该边界 |
| `auth.logout` | ⑥ 身份/会话生命周期 | 同上；模型注销会话会导致用户后续页面请求全部 401，且该动作不可从对话内恢复（必须重新输密码） |
| `auth.password.change` | ⑥ 身份/会话生命周期 | 修改密码后旧会话与确认令牌的绑定关系全部失效（`ARCHITECTURE.md:190` 的令牌六元组含 `session_id`）。这是凭据操作，不是业务操作 |
| `access.user.create` | ② 分配系统级角色 | 创建账号必须同时指定 `role_ids` 与 `scope_ids`（§2.4）。允许 Agent 创建账号 = 允许模型造出一个有任意角色与数据范围的身份，等价于直接提权。`ARCHITECTURE.md:242` 明确把"提权"列入禁止清单 |
| `access.user.status` | ② 分配系统级角色（停用即撤销全部权限） | 停用账号会立即让该用户所有会话失效（旧 `auth_session_store.py` 的 `revoke_session` 链路）。这是权限撤销动作 |
| `access.user.grants` | ① 修改权限模型 + ② 分配系统级角色 | 直接改写 `user_roles` / `user_data_scopes`——即"谁能看到哪些数据"本身。这是 `ARCHITECTURE.md:312-324` 权限一致性矩阵的**被测量对象**，让 Agent 能改它等于让 Agent 改自己的考题 |
| `access.role.permissions` | ① 修改权限模型 | 改写 `role_permissions`，影响该角色下**所有**用户。早期版本此操作的权限码是 `auth.role.manage` 且必须 `super_admin`（`早期版本：product/admin/routes.py:38-42,174-178`） |

### 5.3 我倾向于 human_only 但**不在**允许方向的候选（交 评审结论，本文件**未**标注）
> **状态（本次修订后）**：本节三条**仍未裁决**——`DECISIONS.md` 的 Q1–Q13 没有覆盖它们。正文按"未标注 human_only"处理（即 `cost.period.close` = `exposed` + `always`；`purchase_order.cancel` / `sales_order.cancel` = `exposed` + `always`；`audit.log.list` = `hidden`）。若 负责人 后续裁决，需**先改 `DECISIONS.md` 再改本文件**。


| name | 我倾向的标记 | 理由 | 为什么我不自行标注 |
|---|---|---|---|
| `cost.period.close` | `human_only` | 关账是全期不可逆操作：一旦某期间被关闭，该期间内所有 `cost.entry.create` / `cost.entry.confirm` 都会被 `PeriodOpen` 不变量拒绝（§4 #7）。这与"永久改变系统可用操作集合"接近 §5.1 的⑤"系统级危险配置" | ⑤ 的边界模糊——"会计期间"是**业务**配置而非**系统**配置，我无法自行判定它算不算。当前标 `exposed` + `confirmation=always` |
| `purchase_order.cancel` / `sales_order.cancel` | `human_only` | 取消已审批的订单会连带使下游的收货/交付不可执行，且早期版本对采购/销售取消都强制填原因（`早期版本：purchase_service.py:151-153`、`sales_service.py:127-128`）。属"不可逆的对外承诺撤销" | 不属于 §5.1 六个方向中的任何一个；它是**业务高风险**，正确的手段是 `confirmation=always`（已标），不是 `human_only` |
| `audit.log.list` | （已标 `hidden`，非 human_only） | 审计日志含账号、IP、权限变更前后值。我判断 Agent 不需要它，但"不需要"≠"禁止执行" | 不属于六个方向。正确手段是 `hidden`（默认值，`ARCHITECTURE.md:198`），已采用 |

### 5.4 明确**不**标 human_only 的普通业务（回应用户原话）

用户原话："不要把新建塘口、投喂、采购、销售这类普通业务随意标记为 Human-Only。"

| name | 标记 | 按什么管控 |
|---|---|---|
| `pond.create` | `exposed` + `confirmation=never` | L1 权限（`pond.create`）+ DataScope |
| `pond.update` / `pond.submit` / `pond.verify` | `exposed` | 权限 + 乐观锁 + 状态机 |
| `batch.create` / `batch.verify` / `batch.close` | `exposed`，`batch.close` 用 `confirmation=always` | 权限 + 不变量（#11 存塘为 0） |
| `feeding.create` / `feeding.verify` | `exposed`，`always` / `always` | 权限 + 不变量（#1 负库存、#16、#5） |
| `purchase_order.create` / `submit` / `approve` | `exposed` | 权限 + 不变量（#5 经办人≠审批人） |
| `sales_order.create` / `submit` / `approve` | `exposed` | 权限 + 不变量（#5） |
| `payment.create` / `payment.verify` | `exposed`，`always` / `always` | 权限 + 不变量（#4 不得超余额、#5） |

**结论（按运行时实测重写）**：**102** 条能力中 `human_only` **1 条**、
`exposed` **101 条**、`hidden` **0 条**。业务的写能力 100% 对 Agent 开放（受 L1 权限码、
DataScope、不变量与服务端 HITL 管控）；身份生命周期里唯一进注册表的
`auth.password.change` 收口为 `human_only`，`auth.login` / `auth.logout` 则是
**根本不在注册表**的固定路由。

> 上一版这段写的是"64 条 = 7 / 53 / 4"，**分母与三个分项全部过期**
> （能力数从 64 涨到 102，百分比依此全部作废）。同一份文档里被计数错误的
> 分子分母，比没有这张表更危险 —— 它会让人据此得出"提权类能力对 Agent 全关"的错误结论。

**`hidden` 的 4 条（按运行时实测：一条都不成立）**：上一版列的是
`access.user.list`、`audit.log.list`、`meta.capabilities`、`meta.resources`，实测：
* `access.user.list` = **`exposed`**（`docs/DECISIONS.md…` 第 9 条把账号管理类一并改为 `exposed`）；
* `audit.log.list` = **`exposed`**（`agent_exposure=exposed`，靠 `audit.view` 权限码把守）；
* `meta.capabilities` / `meta.resources` = **不在注册表内**（与 `auth.login`/`auth.logout` 一样是 Flask 固定路由）。

所以 `hidden` 的**实际条数是 0**。`AgentExposure` 的默认值刻意是 `HIDDEN`
（其 docstring 自述"默认暴露会让新加的能力自动获得 AI 可调用权，方向反了"），
但当前 109 条全部**逐条显式**写了 `agent_exposure`，默认值一次都没生效 ——
这本身不是缺陷（每条都显式声明 + L1 权限码把关），但**"默认安全"的保护目前只存在于纸面**：
新增能力的人照抄邻居那一行 `agent_exposure=_EXPOSED`，就自动获得 AI 可调用权，
而没有任何判据会拦。守卫见 `tests/test_agent_exposure_doc_consistency.py`。

---

## 6. 与早期版本的对照：明确不做的能力

| 早期版本能力/域 | 旧规模证据 | 处置 | 降级理由 |
|---|---|---|---|
| **数据交换（Excel 导入导出）** | `features/data_exchange/` 12 文件 1,746 行；3 张表 `data_import_batches`/`data_import_items`/`data_export_audits`（`早期版本：017_data_exchange.sql`）；11 个端点（`早期版本：product/data_exchange/routes.py`） | **不做** | `ARCHITECTURE.md:340` 已定"降级为可选，不在第一轮闭环内"。补充事实：早期实现里导出过滤**同时在 SQL 与 Python 两侧实现**（`早期版本：data_exchange_store.py:171-212`），撤销导入要做两遍 draft 检查（`早期版本：:145-164`），是可预见的返工源 |
| **调拨 `transfer`（含 `in_transit` 状态）** | `warehouse_transfer_store.py` 74 行；`warehouse_documents.status` 里的 `in_transit`（`早期版本：010_warehouse.sql:71`） | **不做** | 闭环不需要跨仓调拨（采购入库→领用出库已构成完整库存流）。**关键后果**：`in_transit` 这个状态在新系统里**不存在**——这与早期版本 `lifecycle.POLICIES` 的缺口不是同一件事（旧 `POLICIES` 根本没有 `in_transit` 键，见下） |
| **更正单 `correction` / 冲销 `reverse`** | `production_store.py:123-150`、`warehouse_store.py:144-160` 的 `create_correction`；4 份"更正根单回溯"实现（`sales_posting.py:9-22`、`purchase_posting.py:39-52`、`sales_source_control.py:25-32`、`warehouse_ledger_store.py:174-180` 其中最后一份是**递归**）；唯一键 `uq_production_documents_correction` 等 4 个 | **不做** | 更正单是"核验后允许改正式事实"的机制，需要整链补偿（原单金额反冲 + 新单金额追加 + 累计量重算）。改单走"作废重开"。**后果**：`corrected` 状态在新系统不存在 |
| **盘点 `stocktake`** | `warehouse_ledger_store.py:65-73` 的 `_book_quantity`；`warehouse_alert_store.py:131-152` 的盘点差异 SQL（约 20 行） | **不做** | 盘点产生的是"账实差异"，需要差异审批与调整单，属更正单范畴 |
| **报废 `scrap`** | `warehouse_alert_store.py:111-115` 的废品抵扣子查询 | **不做** | 报废与损耗（`losses`）同属非正常库存减少，需要审批链 |
| **损耗 `losses`** | `production_store.py:243-255` 的 `_stock_lines` 对 4 种资源各写一套正负号规则 | **不做** | 损耗是非正常库存/存塘减少，需要独立的审批链与原因分类（旧 `validate_loss_reason`）。**注意：出塘 `harvests` 已从本行移出并恢复**（§7 Q9/Q10），因为它是**正常业务**的唯一存塘减量路径，承担 `batch.close` 的可达性 |
| ~~**出塘 `harvests`**~~ | `production_service.py:11-14` 的 9 资源之一 | **已恢复为 P0（3 条能力）——见 §1.5** | **初稿曾列为"不做"，理由"三者都是改变存塘量的非常规事件"。该判断错误**：出塘是正常业务的核心事实源，且是唯一能减少塘内存塘的能力。`DECISIONS.md` Q10 原文："砍掉出塘后，没有任何能力能减少塘内存塘，`batch.close` 的'存塘必须为 0'不变量永远不为真——一个**永远不可达的能力**比一个缺失的能力更糟。" |
| **领用申请 `issue-requests`** | `production_material_control.py` 30 行（一条 5 表 JOIN 的 SQL）；`warehouse_store.py` 的 `_validate_issue_request` 约 20 行 | **不做** | 二级审批层。旧规则（申请额度 − 已出库 − 已投喂 ≥ 本次量）比"事后库存检查"更严格，砍除是**管控降级**，登记 §4 #17 |
| **退货（采购退货 / 销售退货）** | `features/returns/return_store.py` 122 行 + `029_supplier_customer_returns.sql`（2 张表）；采购与销售两侧都转发到同一个 store（`早期版本：purchase_store.py:17`、`sales_store.py:21`） | **不做** | 退货是反向单据 + 应付款/应收款冲减 + 库存回库三段，等于把闭环再做一遍反向 |
| **请购单 `requisition`** | `purchase_requisition_store.py` 263 行 + `034_purchase_requisitions.sql`；5 个端点 | **不做** | 采购的二级审批层（请购→审批→转采购单→审批→收货）。本版把审批层数从 2 层压到 1 层（`purchase_order.approve`） |
| **成本：资产 / 折旧 / 分摊 / 结算** | `features/cost/` 里 `cost_enterprise_service.py` 217 行 + `calculation.py` 的 `allocate_amount`；`common/db/repositories/` 下 4 个 `cost_*` store；6 张表（`015_cost_assets_settlement.sql`）；分摊算法 `cost_enterprise_repository.py:99-160` | **不做** | `ARCHITECTURE.md:338`"成本只做塘口/批次维度的归集"。分摊算法（`calculation.py:82-97` 的按分取整 + 余数补偿）质量很高但需要资产与结算整套上下文 |
| **库存预警 / 呆滞 / 临期** | `warehouse_alert_store.py` 266 行，一次 `list_alerts` 跑 3 条大 SQL；`_collapse_low_stock_alerts`（`:54-75`）在 Python 侧聚合 | **不做** | 预警是**派生视图**，无状态、无账本；需要它时应由 `inventory.list` 的过滤条件表达 |
| **附件上传 / 恶意软件扫描** | `common/files/` 283 行；`attachments` 表（`早期版本：007_revisions_idempotency_attachments.sql:38-58`） | **不做** | 需要文件存储 + magic 校验 + OOXML 炸弹检测 + 外部扫描子进程 + 凭据绑定校验（`evidence.py:37-66`）。代价：`cost.entry.confirm` 的"必须有凭据"不变量降级（§4 #15、Q5） |
| **注册申请与审核** | `registration_applications` 表；`features/registration/` 92 行；`features/account_review/` 227 行；4 个端点；版本号重提上限 3（`早期版本：mysql_store.py:258-259`） | **不做** | 由管理员建号（`access.user.create`）替代。旧流程有"重提不超过 3 次"的重试语义（`REAPPLY_LIMIT_REACHED`），对本项目价值低、表与状态多 |
| **更正/冲销类能力（`reverse`）** | `sales_receipt_reversal_store.py`、`purchase_payment_reversal_store.py`、`cost_expense_store.py:173` 的 `COST_REVERSAL_NOT_ALLOWED` | **不做** | 同更正单 |
| **多组织（`organizations` 多租户）** | `organizations` 表 + `organization_id` 贯穿约 40 张表 | **保留结构、单条数据** | 表与字段留在 schema 里（DataScope 的 `farm_id` 解析依赖它），但种子只建 1 家企业。理由：`早期版本：work_item_notifications.py:20-28` 的注释承认"当前 schema 无法识别非管理员农场的企业归属"，多租户是早期版本没做透的部分 |
| **移动端 / 小程序 / Electron / 桌面端** | 旧仓库有 `miniprogram/`、`mobile/`、`fpa_phone/`、`frontend` 的 Electron 打包 | **不做** | `ARCHITECTURE.md:339`"早期版本有四端并存，其中两端是废弃的半成品"。连带删除 `auth.login` 的 `X-FPA-Client: mobile` 分支（`早期版本：product/auth/routes.py:91-92`） |
| **`disputed` 状态、`pending/rejected/retired/must_change_password` 用户状态** | `早期版本：011_purchase_payables.sql:20`、`013_sales_receivables.sql:22`、`004_enterprise_governance_foundation.sql:4` | **删除** | 全仓无写入点（`purchase_store.py`/`sales_store.py` 的 `set_order_status` 从不写 `disputed`）；用户状态由 `access.user.status` 收拢为 `active`/`disabled` |
| **`favicon`/软删除/回收站/收藏/操作日志导出** | 无对应实现或仅前端占位 | **不做** | 【新增设计】本版不含 `kind=delete` 能力（§0.2） |

### 6.1 关于"三处状态机不一致"的精确说明

任务指出"早期版本三处定义不一致，且 POLICIES 缺 `in_transit` 与 `corrected`"。**核实结果比原判断更细**：

- `common/governance/lifecycle.py:48-61` 的 `POLICIES` 有 **12 个键**：`draft, submitted, verified, confirmed, cancelled, reversed, archived, approved, partially_received, fully_received, closed, disputed`。
- **缺失的键**（被 DB ENUM 或域常量使用、但 `POLICIES` 里没有）：`in_transit`（`早期版本：010_warehouse.sql:71`）、`corrected`（`早期版本：009_production.sql:19,66`、`010_warehouse.sql:71`）、`unpaid/partial/settled/overpaid/bad_debt`（应付应收状态，`早期版本：012_purchase_hardening.sql:4`、`013:98`）、`invalid/ready/imported/undone`（导入批次，`早期版本：017_data_exchange.sql:13`）、`build/stocked/farming/rest/clean/rebuild`（塘口状态，`早期版本：008_master_data.sql:36`）、`available/quarantined/expired/closed`（物料批次，`早期版本：010:29`）、`unhandled`（预警处理，`早期版本：010:147`）、`pending/claimed/in_progress/escalated`（待办，`早期版本：004:39`）、`open`（会计期间，`早期版本：030:9`）、`pending/confirmed/failed/expired`（Agent 确认，`早期版本：031:10`、`032:2`）。
- 即：`POLICIES` 只覆盖了"单据生命周期"这一类，**用它去查任何一个非单据状态都会抛 `INVALID_STATUS`**（`早期版本：lifecycle.py:64-68`）。这不是"缺两个键"的疏漏，而是"一张表被复用到了它不覆盖的域"。
- **本版的处置**：`POLICIES` 式的全局状态表被**取消**。每个状态机（§3 的 6 个 + 6 个单据二态 + 期间态）自带完整定义；`row_actions` 由状态机的 `from/to` 邻接关系机械派生，不再手写 `can_edit`/`can_delete` 布尔对。

---

## 7. 裁决记录（Q1–Q13 全部已裁决）

> **本节已由"待裁决清单"转为"裁决记录"。** 每条的**结论以 `docs/DECISIONS.md` 为唯一权威**，本节只做回写与索引。
> 下方保留每条**原始的问题陈述与我当时的默认取值**（历史可读性），并在其上方插入一行裁决结论。
> **施工方须知**：正文各节（§0–§6）已按裁决修订完毕；本节用于溯源"为什么是这个设计"。

### 裁决一览（Q1–Q13）

| # | 议题 | 裁决 | 对本文档的影响 |
|---|---|---|---|
| Q1 | 塘口业务状态枚举 | 采用旧 DB 6 态 `build/stocked/farming/rest/clean/rebuild`；`closed` 从塘口删除（只属 batch） | §3.1-B 状态表、§3.1-C 冲突节改判 |
| Q2 | 塘口状态变更是否两步审批 | **保留两步**：`pond_status_change.request` + `pond_status_change.verify`，恢复唯一键 `uq_pond_status_active_request` | §1.4 +2 能力（11→13）、§3.1-B/D、§4 #20/#21 |
| Q3 | 系统级权限码命名 | 保留 `auth.user.manage` / `auth.role.manage`，在 §0.1 记为**显式命名例外** | §1.2 权限码不变 |
| Q4 | `confirmation` / `idempotent` 语义 | **删除 `by_key`**（只留 `never`/`always`）；`confirmation` 只约束 Agent 入口；`idempotent=true` = **强制**携带键 | §0.4、§0.5；5 条 `by_key` → `always` |
| Q5 | 成本凭据降级 | 接受降级：`cost.entry.confirm` 用 `RequiredField(fields=["source_ref"])` | §4 #15 定案 |
| Q6 | DataScope 跨表解析 | **不加 `via` 语法**，改为在 `warehouses`/`inventory_lots`/`inventory_ledger` 补 `farm_id`/`area_id` 分租列 | §0.3 删除 `via` 行；§1.6 两条 scope 改 `resource(area_id)` |
| Q7 | create 是否接收 org/farm/area | **确认变更**：三者由 `ScopeResolver` 解析，字段元数据标 `readonly: true` | §0.7 规则 1 强化；§2 各字段表 |
| Q8 | 经办人≠审批人推广范围 | **推广到 11 条**；比较 `created_by` 与 `verified_by`；**不留自审例外** | §4 #5；11 条能力 |
| Q9 | 出塘事实与领用申请 | **恢复 `harvests`**；"交付数量与出塘事实一致"不变量恢复比对；`issue-requests` **不恢复** | §1.5 +3 能力、§4 #6 恢复、§4 #17 保持降级 |
| Q10 | `batch.close` 可达性 | **恢复 `harvests`**，batch 生命周期完整可达 `stocked→farming→pending_settlement→closed` | §1.5、§3.2 可达性表 |
| Q11 | `params_hash` 覆盖范围 | 覆盖**客户端提交的原始参数**；`canonical_json` 用 `sort_keys=True, separators=(",",":"), ensure_ascii=False` | §7 Q11 详述；影响 `ARCHITECTURE.md` 实现 |
| Q12 | 规模是否裁剪 | **零裁剪**，按 **69 条**做 | §1 抬头、§1.11 转为"已评估、不采纳"、§1.12 同 |
| Q13 | cost 域是否精简 | **推翻**"纯派生+只读"，**保留全部 5 条** | §1.9 保持 5 条、§1.12 整体标"已被推翻"、§4 #7 扩展 `PeriodOpen` 至 6 条能力 |

### 派生待确认（裁决自身引出的新问题，非原 Q1–Q13）

| # | 议题 | 我的取值（已写入正文） | 需 负责人 确认 |
|---|---|---|---|
| Q8′ | Q8 裁决的是"11 条"，但本次新增的 `harvest.verify` 与 `pond_status_change.verify` 也是核验/审批能力 | **已确认扩到 13 条**（回写 §4 #5） | ✅ **负责人 已确认（2026-09，t21）**：按 Q8 自己的原则并入二者 → **13 条**；并写明"**该清单必须完备**"——新增核验/审批能力必须同时进 §4 #5，漏一条等于把合规规则降级成建议 |
| Q13′ | Q13 第 3 点要求 `PeriodOpen` 挂到 4 条"会写成本的能力"，其中 `delivery.verify` 在本设计中写的是**应收（收入侧）**而非成本 | 正文**照裁决照写 4 条**（+ 原 2 条 cost 能力 = 6 条） | 若"写成本"是严格口径，`delivery.verify` 应替换为其他成本派生能力；若口径是"写财务事实"，则现状正确。请确认。 |

---

**（以下为 Q1–Q13 的原始问题陈述与我的默认取值，逐条前置裁决行）**

### Q1（阻塞 master_data 字段与状态机）— 塘口业务状态枚举冲突

> **裁决（`DECISIONS.md` Q1）：采用旧 DB 的 6 态** `build / stocked / farming / rest / clean / rebuild`。**`closed` 从塘口状态中删除**（它只属于 `batch`）；`INTERFACES.md` §2 的 `status_dict` 已修正。→ 已回写 §3.1-B / §3.1-C。

- `INTERFACES.md:112-118` 的 `status_dict` = `build / stocked / farming / rest / **closed**`（5 态），label 为 待建设/已放苗/养殖中/休整/已结束。
- 旧 DB = `ENUM('build','stocked','farming','rest','**clean**','**rebuild**')`（`早期版本：008_master_data.sql:36`，6 态），且转移表依赖 `clean`/`rebuild`（`早期版本：master_data_service.py:21-24`）。
- **`closed` 在早期版本任何状态表里都不存在；`clean`/`rebuild` 在 `INTERFACES.md` 里不存在。**
- 我的默认取值：**采用旧 DB 的 6 态**（转移表可用、有生产验证），`INTERFACES.md` 的 `status_dict` 需修正。请裁决。

### Q2（阻塞 pond 状态变更的实现形态）— 塘口状态变更是否保留两步审批

> **裁决（`DECISIONS.md` Q2）：保留两步审批**（`pond_status_change.request` + `pond_status_change.verify`），恢复唯一键 `uq_pond_status_active_request`。master_data 域 **+2 能力**，需一张 `pond_status_change_requests` 表。→ 已回写 §1.4 / §3.1-B、D / §4 #20、#21。

- 早期版本是两步：`POST /ponds/{id}/status` 建申请 → 另一人核验（`早期版本：master_data_service.py:279-299`、`pond_status_store.py:24-60`），并由唯一键 `uq_pond_status_active_request` 保证"同一塘口只有一个待核验申请"。
- 我的默认取值：**降级为单步**（`pond.update` 直接改 `pond_status`，转移表校验），为此砍掉 2 个能力与 1 张表。
- 如果保留两步，master_data 域 +2 条能力（→ 66 条总数）。

### Q3（阻塞权限码命名一致性）— 系统级权限码是否套用同名派生规则

> **裁决（`DECISIONS.md` Q3）：保留 `auth.user.manage` / `auth.role.manage`**，在 §0.1 记为**显式的命名例外**。理由：这两个码管的是**系统**（账号与角色），不是某个业务资源；早期版本种子数据与测试已用这两个码，改名是为统一而统一地增加 churn。

- §0.1 规定"一个能力一个权限码，与 `name` 同名"。
- 但 `access.*` 的 5 条能力沿用了早期版本的 `auth.user.manage` / `auth.role.manage`（它们管的是**系统**，不是某个业务资源）。
- 我的默认取值：**保留旧码**（`auth.user.manage` / `auth.role.manage`），在 §0.1 记为显式例外。请裁决是否改为 `access.user.manage` 以求命名统一。

### Q4（阻塞幂等与确认的实现）— `confirmation` 与 `idempotent` 的精确语义未定义

> **裁决（`DECISIONS.md` Q4.1 / Q4.2），两项**：① **删除 `by_key`**，`confirmation` 只保留 `never` / `always`，并明确**只约束 Agent 入口**；② `idempotent=true` 表示**强制携带** `Idempotency-Key`，强制范围 = 全部 `create` ∨ 全部 `confirmation=always` ∨ `creates_record`。→ 已回写 §0.4 / §0.5；初稿 5 条 `by_key` 已全部改为 `always`。

- `INTERFACES.md:67` 只给了 `never|always|by_key` 三个字面值，**没有定义语义**。
- 我的默认取值（已写进 §0.4）：
  - `confirmation` **只约束 Agent 入口**，页面入口的二次确认由前端 UX 决定；
  - `always` = Agent 每次调用都必须先拿一次性确认令牌；`by_key` = 仅当参数命中风险阈值（不可逆操作 / 金额超过阈值 / 状态为 `verified` 后的写）时要求令牌；
  - 阈值由谁定义（配置项？capability 声明内的 `by_key={"amount_gte": 10000}`？）**未定**。
- 另需裁决：`idempotent=true` 是"允许携带 Idempotency-Key"还是"**强制**携带"？我的默认取值是 §1 表格里加粗的 **13 条强制**，恰好等于全部 `create` 能力（`access.user.create`、`pond.create`、`partner.create`、`batch.create`、`feeding.create`、`receipt.create`、`issue.create`、`purchase_order.create`、`payment.create`、`sales_order.create`、`delivery.create`、`sales_receipt.create`、`cost.entry.create`）；所有 `action` 与 `update` 允许但不强制。请确认"全部 create 强制"这个口径。

### Q5（阻塞成本域的一条不变量）— 附件不做后"核验必须有凭据"如何降级

> **裁决（`DECISIONS.md` Q5）：接受降级**，`cost.entry.confirm` 的不变量改为 `RequiredField(fields=["source_ref"])`。**记为已知降级**，须写入最终交付报告的"尚未完成的问题"；附件能力是可恢复的（后续加 `attachment.*` 即可升级回去）。

- 早期版本强制：`cost.entry.confirm` 前必须至少绑定一份凭据（`早期版本：cost_enterprise_repository.py:80-83`，422 `EVIDENCE_REQUIRED`）。
- P0 不做附件（§6），该不变量失去载体。
- 我的默认取值：降级为 `invariant=[RequiredField(fields=["source_ref"])]`（必填来源单号代替凭据）。请裁决是否接受，或改为"仅在 `amount ≥ 阈值` 时要求"。

### Q6（阻塞 DataScope 实现）— `ScopePolicy.resource(col)` 是否允许跨表 JOIN 解析

> **裁决（`DECISIONS.md` Q6）：不支持跨表 JOIN 解析**，改为在 `warehouses` / `inventory_lots` / `inventory_ledger` 上**补齐 `farm_id` / `area_id` 分租列**（与早期版本 `areas` / `ponds` / `pond_groups` 一致）。→ 已回写 §0.3（删除 `via` 语法）与 §1.6 两条 scope。

- `INTERFACES.md:272` 只规定 `AND <alias>.<col> IN (…)`——**同表列**。
- 但 `inventory_ledger` / `inventory_lots` 只有 `organization_id` + `warehouse_id`，区域在 `warehouses.area_id`（`早期版本：010_warehouse.sql`）。
- 我的默认取值：新增 `via <表>` 语法（§0.3），生成 `IN (SELECT id FROM warehouses WHERE area_id IN (…))`。请裁决语法与实现位置。

### Q7（阻塞前端契约）— 是否确认"create 不接收 organization_id/farm_id/area_id"

> **裁决（`DECISIONS.md` Q7）：确认变更**。三个字段由 `ScopeResolver` 在服务端解析并写入；字段元数据标记 **`readonly: true`**，`DynamicForm` 渲染成**禁用输入框**并显示解析出的值。理由原文："如果客户端能指定 `area_id`，那'用户只能写自己区域的数据'就退化成'用户自报家门'。"→ 已回写 §0.7 规则 1。

- §0.7 规则 1 是本次**最大的一处契约变更**：早期版本这三个字段出现在绝大多数 payload 里（`早期版本：master_data_service.py:15-18`、`production_service.py:18`、`warehouse_service.py:14`），并需要三套回填实现。
- 新系统由 `ScopeResolver` 解析写入，前端不再传。
- **风险**：旧前端表单里这些字段是可见的（`早期版本：DailyOpsPage.vue:6` 等），继承时会多出三个无法提交的字段。
- 我的默认取值：**确认变更**（这是 DataScope fail-closed 的前提——`ARCHITECTURE.md:182-184` 要求"Scope 注入到仓储调用"）。请确认，并确认前端同步改造的工作量已计入排期。

### Q8（阻塞合规规则的声明形态）— "经办人≠审批人"推广到哪些能力

> **裁决（`DECISIONS.md` Q8）三条**：① **推广到 11 条**核验/审批能力；② 比较字段固定为 `created_by` 与 `verified_by`（比早期版本比 `updated_by` 更严格）；③ **不留"同一人可自审"的例外**。由内核统一执行，**人工页面与 Agent 走同一份判断**。→ 已回写 §4 #5（**13 条**）；另见 §7 开头的 **Q8′**（已确认，清单必须完备）。

- 早期版本**只有** `sales_service.py:121-122` 一处实现。
- 我的默认取值：推广到 **11 个核验/审批能力**（§4 #5 列出的清单），`invariant=[DistinctActors(creator_field="created_by", verifier_field="verified_by")]`。
- 需裁决两点：
  1. 比较哪两个字段？早期版本比的是 `created_by`/`updated_by` 与当前用户（`早期版本：sales_service.py:121`），本版建议比 `created_by` 与 `verified_by`（更严格）。
  2. **是否允许"同一人既是经办又是审批"在数据范围隔离下成立**？（例：区域 A 的经办人在区域 B 无审批人时能否自审？）早期版本无此例外。

### Q9（阻塞 sales/warehouse 的两条不变量）— 砍掉出塘事实与领用申请后的替代形态

> **裁决（`DECISIONS.md` Q9）：恢复 `harvests`（3 条能力）**；`delivery.create` 的不变量保持"累计交付 ≤ 销售数量"，**并恢复**"交付数量与出塘事实一致"的比对。`issue-requests` **不恢复**，`feeding.verify` 直接判库存（复用"不得负库存"不变量）。→ 已回写 §1.5（+3 能力）/ §2.9（恢复 `harvest_document_id`）/ §4 #6（恢复）、#17（保持降级）。

- 砍 `harvests` 使 §4 #6"交付数量与出塘事实一致"失去比较对象；砍 `issue-requests` 使 §4 #17"投喂必须有足额已核验领料申请"失去前置。
- 我的默认取值：`delivery.create` 只保留"累计交付 ≤ 销售数量"（#10），**放弃"逐字节一致"**；`feeding.verify` 改为直接判库存（复用 #1）。
- 这是**管控降级**，需要 负责人 确认接受。备选：恢复 `harvests` 为只读事实源（+0 个能力，只需 1 张表 + `sales_source_control` 的核心校验），我倾向这个折中。

### Q10（阻塞 production 域闭环）— `batch.close` 在 P0 不可达

> **裁决（`DECISIONS.md` Q10）：恢复 `harvests`**，batch 生命周期完整可达 `stocked → farming → pending_settlement → closed`。理由原文（本次最重要的发现）："砍掉出塘后，**没有任何能力能减少塘内存塘**，`batch.close` 的'存塘必须为 0'不变量永远不为真——一个**永远不可达的能力**比一个缺失的能力更糟，因为它在清单上看起来是有的。"→ 已回写 §1.5 / §3.2 可达性表 / §3.6。

- `batch.close` 的不变量是"存塘必须为 0"（§4 #11，`早期版本：production_store.py:228-235`）。
- 但 P0 只做投喂、不做出塘/损耗（§6），而**投喂不减少存塘**（它扣的是库存物料，不是塘内存塘）。
- **因此 P0 里没有任何能力能让 `batch_status` 从 `stocked` 走到 `pending_settlement`/`closed`**（唯一能减少存塘的是出塘 `harvests`，而它被砍了）。
- 我的默认取值：**恢复 `harvests` 为 P0**（+3 条能力：`harvest.list` / `harvest.create` / `harvest.verify` → 总数 67），或接受 `batch.close` 记为"预留能力、P0 不可达"。请裁决——我倾向恢复 `harvests`，因为它是养殖业务的核心事实，且顺带解决 Q9。

### Q11（不阻塞，但影响契约生成）— 确认令牌 `params_hash` 覆盖哪一版参数

> **裁决（`DECISIONS.md` Q11）：覆盖 (a) 客户端提交的原始参数。** 理由三条：① 服务端补全值在签发确认卡片时还不存在，无法预知；② "用户看到什么就确认什么"；③ 补全值是从用户身份推导的，不是用户输入。**实现约束**：`params_hash = sha256(canonical_json(原始参数))`，`canonical_json` 用 `sort_keys=True, separators=(",",":"), ensure_ascii=False`——与早期版本 `idempotency.request_hash()` 的规范化方式一致。

- `ARCHITECTURE.md:194` 要求 `params_hash` 覆盖"执行时的实际参数"，且必须与确认卡片一致。
- 但 §0.7 规则 1 之后，服务端会在执行时**补全** `organization_id`/`farm_id`/`area_id`（由 ScopeResolver 解析）。
- 问题：`params_hash` 覆盖(a) 客户端提交的原始参数，还是(b) 含服务端补全后的参数？
- 我的默认取值：**(a) 客户端原始参数**（服务端补全值不确定，无法在签发卡片时预知；且 (a) 与"用户看到什么就确认什么"语义一致）。请确认。

### Q12（不阻塞，规模裁决）— 64 条是否是 2-3 周的可行规模

> **裁决（`DECISIONS.md` Q12）：不采纳裁剪建议，能力清单零裁剪，按 69 条做。** 诚实结论：**8 周是单人串行的真实数字**，不假装能在 2–3 周完成；按并行结构走 4–5 周节奏、**每阶段交付可验收的中间产物**。→ 已回写 §1 抬头、§1.11（转为"已评估、不采纳"）。

我的实测估算（假设 kernel 已完成）：

| 部分 | 估算 | 依据 |
|---|---|---|
| 22 个 `read` 能力 | ~11 h | 各 0.5 h（repo 方法 + 声明 + 测试） |
| 13 个 `create` + 4 个 `update` | ~43 h | 各 2.5 h（字段 + 不变量 + 测试） |
| 25 个 `action` | ~38 h | 各 1.5 h（统一生命周期机制） |
| 其中 6 个复杂核验（feeding.verify / receipt.verify / issue.verify / payment.verify / delivery.verify / sales_receipt.verify） | 额外 ~18 h | 各自带 1–2 条不变量与账本写入 |
| **能力实现小计** | **~112 h ≈ 2.8 周** | |
| kernel（capability/uow/scope/lifecycle/idempotency/audit/errors/pagination） | ~50 h | `ARCHITECTURE.md:84-104` 的 7 个模块 |
| web 派生层 | ~20 h | 由 Capability 生成，薄 |
| agent（gateway + L1/L2/L3 + 插件包 + 进程池） | ~35 h | `ARCHITECTURE.md:202-264` |
| database（迁移 + 种子） | ~12 h | 约 25 张表 |
| tests（架构 AST 扫描 + 权限矩阵 + 对抗性 + 集成） | ~35 h | `ARCHITECTURE.md:300-324` |
| frontend（约 12 个页面） | ~50 h | 旧 53 页面裁到 1/4 |
| **合计** | **~314 h ≈ 8 周（单人）** | |

**结论**：64 条能力本身约 **2.8 周**，但**整个项目 ≈ 8 周**。若"2-3 周"是硬约束，**不能只砍能力条数**——必须同时压缩前端页面数与 Agent 范围。我的建议：能力收到 **50 条**（推荐方案 B，= §1.11 的 11 条非成本裁剪 + §1.12 的 3 条成本裁剪）+ 前端收到 **8 个页面** + Agent 只做"查询 + 投喂登记"两条链路。请裁决裁剪口径。

### Q13（阻塞 cost 域范围）— 是否采纳"成本纯派生 + 只读"的精简（§1.12 方案 B）

> **裁决（`DECISIONS.md` Q13）：推翻"纯派生 + 只读"，cost 域保留全部 5 条。** 三点纠正：① "成本随业务事实自动生成"不是能力而是副作用，该方案**遗漏了系统外费用**，是覆盖面缺口不是精简；② `cost.entry.confirm` 是最能体现双人复核设计的载体，删掉等于删掉成本域的双人复核；③ 期间锁定不应被删而是应被**加强**（`PeriodOpen` 扩展到 4 条派生能力）。**顺带修正**：`cost.entry.confirm` 的 `confirmation` 由 `never` 改为 `always`。→ 已回写 §1.9、§1.12（整体标"已被推翻"）、§4 #7。

- 负责人 指导原文以 cost 为例："只需要「按塘口/批次查成本」+「成本随业务事实自动生成」"。
- 我已按此把 cost 从 **5 条压到 2 条**（`cost.summary` + `cost.entry.list`），并逐条列出 6 项代价（§1.12 的代价表）。
- **需要你确认的核心一项**：接受"不变量 #7 期间锁定"与"#8 成本去重"**失去强制能力**（它们在纯派生架构下没有作用对象）。若你不接受，最小代价的回退是**只恢复 `cost.entry.create` + `cost.period.close` 两条**（→ 52 条），这样 #7 有载体、#8 仍降级。
- 另需确认：系统外费用（人工、水电、租金）无法进成本，这是被 `ARCHITECTURE.md:338` 划出范围的接受项。
