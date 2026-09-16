# 渔芯AI水产养殖一体化系统 — 架构决策记录

> 本文是渔芯AI水产养殖一体化系统的**唯一架构准则**。所有代码必须服从本文；与本文冲突的早期版本 代码一律不继承。
> 目标：证明需求中的每一条，而不是"做一个能跑的 CRUD"。

---

## 0. 设计输入的来源

本仓库是一套独立实现。设计输入来自两类材料：一类是已经跑通的业务规则——负库存与负存塘的
行锁写法、状态机、幂等语义、审计字段；一类是对上一版问题的复盘——同一次业务写操作在四处
被重复描述、安全策略从 URL 单词推导、泛型资源路由让新增路由可以绕开任何策略。

有价值的只是**结论**，不是那些代码。本仓库的代码全部重新写过：
1. 已验证过的**领域不变量**（负库存/负存塘的行锁写法、状态机、幂等语义、审计字段）；
2. 已验证过的**前端契约层**（响应信封、中文文案工厂、`allowed_actions` 服务端驱动）；
3. 已验证过的**Harness 集成面**（工具注册机制、`disabled` 内建工具清单）。

其余（泛型资源路由、URL 正则安全策略、进程内 HTTP 自环、6 套 DataScope、358 个错误码）**全部重写**。

---

## 1. 核心论点：Capability 是唯一事实来源

### 1.1 早期版本的病根

早期版本里"一次业务写操作要改哪些东西"被描述了 **4 遍**，且没有一处权威：

```
① product/*/routes.py   的 handler
② features/*_service.py 顶部的 FIELDS / RESERVED 常量
③ features/agent/agent_tool_policy.py 的 URL 正则启发式
④ tools/build_openapi.py 的契约生成
```

后果（已在早期版本库实测）：前端手写了第二份字段清单且已漂移（`material_name` vs `material_id`）；Agent 的确认策略靠 URL 里有没有 `approve/status/verify` 这种词决定；新增路由可以不惊动任何安全策略。

### 1.2 新系统的公理

> **一个业务能力（Capability）只声明一次，其余全部机械派生。**

```python
@capability(
    name="pond.create",
    method="POST", path="/api/v1/ponds",
    service=PondService.create,
    permission="pond.create",
    scope=ScopePolicy.pond_area(),          # 声明分租键，不是调用 require_scope()
    risk=Risk.NORMAL,
    confirmation=Confirm.NEVER,
    invariant=[ScopeResolvable()],
    audit=AuditPolicy(before_after=True),
    idempotent=True,
)
```

从这一处**派生**：

| 派生物 | 说明 |
|---|---|
| REST 路由 + 请求/响应 schema | 由 `path` / `method` / `pydantic` 模型生成 |
| OpenAPI 文档 | 生成，**不再手工维护** |
| 前端字段元数据 | 生成，前端不再手写 `fields`/`columns` |
| RBAC 权限码 | 注册进 Permission Registry |
| DataScope 谓词 | 由 `scope` 声明生成 SQL 谓词 |
| 幂等策略 | 由 `idempotent` 决定是否要求 `Idempotency-Key` |
| 人工确认闸门 | 由 `confirmation` 决定，**不再看 URL** |
| 审计字段 | 由 `audit` 决定 |
| Agent Tool | 由 `agent_exposure` 决定是否注册、以及注册成什么 schema |
| 类型化 TS 类型 | 前端 `types.gen.ts` |

### 1.3 为什么这条论点成立

因为"改 URL 名字会不会改变安全策略"这个问题的答案变成了 **不会**——策略不从 URL 来，策略来自声明。

---

## 2. 分层与边界

```
yuxin/
├─ backend/
│  ├─ kernel/            # 与框架无关，HTTP 零依赖，可脱离 Flask 测试
│  │   ├─ errors.py      # 唯一异常类型 + 错误码枚举（唯一注册表）
│  │   ├─ scope.py       # Scope：分租键 + SQL 谓词构造
│  │   ├─ uow.py         # UnitOfWork：显式事务边界（禁止嵌套隐式提交）
│  │   ├─ capability.py  # Capability 声明 + 注册表 + 派生器
│  │   ├─ lifecycle.py   # 状态机 + 乐观锁 + before/after 快照
│  │   ├─ idempotency.py # 幂等（单事务内完成）
│  │   └─ audit.py       # 审计记录器
│  ├─ domains/           # 业务层：声明 + 不变量 + 仓储
│  │   ├─ _base/         # BaseService/BaseRepo，所有域共用
│  │   ├─ identity/      # 用户、会话、登录
│  │   ├─ access/        # RBAC + DataScope
│  │   ├─ master_data/   # 塘口/区域/基地/物料/供应商/客户
│  │   ├─ production/    # 养殖批次、投喂、巡塘
│  │   ├─ warehouse/     # 库存、流水、批次
│  │   ├─ purchase/      # 采购单、收货、应付
│  │   ├─ sales/         # 销售单、出库、应收
│  │   ├─ cost/          # 成本归集
│  │   └─ audit/         # 审计查询
│  ├─ web/               # HTTP 适配层：薄，无业务；由 Capability 生成
│  ├─ agent/             # Agent Gateway：消费 Capability，无策略猜测
│  └─ harness/           # DeepSeek Harness 会话生命周期（进程池 + 隔离）
├─ frontend/             # Vue 3 + TS + Vite
├─ agent-runtime/        # Harness 插件包（TypeScript），注册类型化 Tool
├─ database/             # 迁移 + 种子
├─ tests/                # pytest / vitest / playwright
└─ docs/
```

### 2.1 依赖方向（硬约束，由测试强制）

```
web ──→ kernel ◄── agent
 │                  │
 └──→ domains ──────┘
         │
         └──→ kernel
```

- `kernel/` **禁止** import `flask`、`pymysql`、`domains/`、`web/`、`agent/`
- `domains/` **禁止** import `flask`、`web/`、`agent/`
- `web/` 与 `agent/` **禁止**直接写 SQL，禁止 `get_connection()`
- 唯一允许 `pymysql` 的地方：`kernel/uow.py` 的驱动适配

**这条约束由 `tests/test_architecture.py` 用 AST 扫描强制**，不靠自觉。

---

## 3. 事务：显式边界，禁止隐式提交

### 3.1 早期版本的错误

```python
# 旧 common/db/connection.py:67-91
@contextmanager
def get_connection(settings):
    ...
    yield connection
    connection.commit()      # ← 每个 with 块退出都提交
```
SAVEPOINT 全仓 0 处。后果：`核验单据 → 建待办 → 发通知 → 写审计` 这种跨模块操作，每跨一个模块多一个提交点；中间失败则前半截已落库，无法回滚。旧 `idempotency.py` 就是三次独立事务（预留/执行/回写）。

### 3.2 新系统的规则

```python
# 唯一正确写法
with uow.begin() as tx:
    pond = repo.get(tx, pond_id)
    repo.update(tx, pond, patch)
    audit.record(tx, ...)        # 同一 tx
    # 退出时一次提交；异常则整体回滚
```

规则：
1. **仓储方法签名第一个参数永远是 `tx`**，仓储自己不开连接。
2. 一个 HTTP 请求 = 一个 `UnitOfWork` = **最多一次 commit**。
3. 需要嵌套语义时用 **SAVEPOINT**（`tx.savepoint()`），不用嵌套连接。
4. 幂等的预留、业务写入、版本快照、审计、幂等回写**在同一 tx 内**。

### 3.3 幂等键的存储修正

早期版本 `idempotency_keys.response_json` 存整个响应体（可能 200 KB）。新系统：
- 表只存 `key_hash`、`request_hash`、`status`、`resource_type`、`resource_id`、`result_code`、`expires_at`；
- 回放时**从业务表读实际资源**返回，而不是回放快照；
- 天然解决"崩溃留下永久 `processing`"——因为它和业务写入同事务，崩溃即整体回滚。

---

## 4. 三处必须 fail-closed 的地方

### 4.1 DataScope

早期版本的致命缺陷：`farm` 型数据范围因为 `data_scopes` 表没有 `farm_id` 字段，导致 `scope_predicate()` 走到 `return "1=0", []`——**用户看到 0 行，但不报错**。静默返回空数据比报错危险。

新系统规则：
1. `data_scopes` 表带 `scope_type ∈ {farm, area, pond, personal}` 与对应的 `farm_id` / `area_id` / `pond_id`。
2. `Scope` 由 `AccessService.resolve(user_id)` 解析，**注入到仓储调用**。
3. `ScopeResolver` 无法把某个声明解析为可执行谓词时，抛 `DATA_SCOPE_UNRESOLVED`（403），**不返回空集**。
4. 仓储的 `list/get` 方法签名强制收 `scope: Scope`，无法"忘记传"。

### 4.2 确认令牌

绑定的值是 6 元组，全部校验：
```
(user_id, session_id, capability_name, params_hash, target_ref, expires_at)
```
外加 `used_at IS NULL` 的单次性。任何一项不符 → `CONFIRMATION_INVALID`。

关键：**`params_hash` 覆盖执行时的实际参数**。用户在确认卡片上的"填写内容"必须与 Execute 时提交的参数逐字节一致。

### 4.3 Agent 暴露

`AgentExposure` 的默认值是 `HIDDEN`——新写的能力默认不进 Agent，必须显式声明 `EXPOSED` 才会注册成 Tool。这是刻意设计的，理由：**能力清单是权威的，Tool 清单是派生的子集**；默认暴露会让"新加的能力自动获得 AI 可调用权"，方向反了。

---

## 5. Agent 架构

### 5.1 三层权限防御（需求明确要求，逐层可测）

| 层 | 位置 | 做什么 | 测试方式 |
|---|---|---|---|
| **L1 Tool Filtering** | `agent/session.py` 组装 Harness 会话时 | 只把 `user.permissions` 覆盖到的 Capability 注册成 Tool | 断言 Tool 清单集合等于用户权限子集 |
| **L2 Gateway Re-check** | `agent/gateway.py` 收到 tool call 时 | 即使模型构造了未授权 Tool，也再次 `permission_check()` + `scope_check()` | 伪造 tool call 必须 403 |
| **L3 Service Re-check** | `domains/*/service.py` 每个能力入口 | 再次校验 permission + scope（不信任调用方） | 绕过 Gateway 直调 Service 必须 403 |

### 5.2 调用链（禁止绕过）

```
DeepSeek Harness (子进程)
  ↓ tools/call  → 插件打包成 HTTP
Agent Gateway (/api/v1/agent/tools/{name}/call)
  ↓ 1. 校验 X-Agent-Context 上下文令牌（绑定 user + session + conversation + nonce）
  ↓ 2. Schema 校验（Pydantic，同一份模型）
  ↓ 3. 权限校验（L2）
  ↓ 4. DataScope 校验（L2）
  ↓ 5. 确认策略判定
  ↓ 6. 幂等
  → Business Service（L3 再次校验）
  ↓
  UnitOfWork → PyMySQL → MySQL
  ↓
  Audit
```

### 5.3 禁止清单（需求要求，逐条有测试）

| 禁止项 | 强制手段 |
|---|---|
| 任意 SQL | Agent 进程**不持有** DB 凭据；Harness 子进程环境无 `MYSQL_*` |
| 任意 Shell / PowerShell | Harness patch 逐条 `disabled: true`（沿用旧清单，已覆盖 bash/pwsh/fs/web/subprocess/skill/subagent…） |
| 任意 URL | 插件只 `fetch` 网关一个 endpoint，URL 来自配置且校验协议 |
| 未注册接口 | Gateway 只按 `Capability.registry` 路由，未注册 → `TOOL_NOT_FOUND` |
| 绕过 Gateway | 业务 Service 的 L3 校验 + Tool 层无其他出口 |
| Agent 自读 DB | 子进程环境变量白名单（沿用旧 `_ENV_ALLOWLIST` 并收窄） |
| eval / exec | 代码里不出现；由 `tests/test_agent_no_escape.py` 扫描 |
| 改 Registry / 提权 | `RBAC`/`Registry` 类能力标记 `HUMAN_ONLY` |

### 5.4 Harness 集成的工程修正

早期版本三个致命工程问题，新系统必须解决：

| 旧问题 | 证据 | 新方案 |
|---|---|---|
| 单把 `RLock` 罩住整个 `harness.run`，配合 `gunicorn --workers 2 --threads 2` → 每 worker 同时只能跑 1 个智能体回合 | `harness_sidecar.py` 的 `with self._lock:` + 90s 超时 | **子进程池**，按 session 亲和分配；并发度可配，锁只保护池的记账 |
| 每轮丢弃子进程重建（`context_token` 存在即 `_cache.drop(namespace)`） | 同上 | 会话亲和 + 令牌轮换不重建进程；令牌绑定改由 Gateway 校验 |
| 工具目录塞进进程环境变量（`YUXIN_AGENT_TOOL_CATALOG`），注释自承"为绕开 Windows 环境变量上限" | `build_agent_tool_catalog()` | **正规插件包**：工具 schema 由插件启动时从 Gateway 拉取（`GET /api/v1/agent/tools`），不经过环境变量 |

### 5.5 工具粒度

早期版本是 `biz_query` / `biz_mutation` 两个元工具，把业务操作名塞进 `description` 字符串里让模型猜。

新系统：**每个 Capability 一个真实 Tool，带真实 JSON Schema**：
```
pond.get      { pond_id: integer! }
pond.create   { code: string!, name: string!, area_id: integer!, capacity_mu: number }
feeding.create{ pond_id: integer!, batch_id: integer!, material_id: integer!, quantity: number!, happened_at: string }
```
理由：模型不需要"从长描述里挑字符串"，参数由 schema 约束，校验在模型这一侧就开始生效。

---

## 6. 前端架构

### 6.1 必须继承的（早期版本已验证良好）

| 资产 | 为什么保留 |
|---|---|
| 响应信封 `{code, message, data, request_id}` | 前端 15 处代码依赖它 |
| `errors.ts` 的 4 个中文文案工厂 | 实测文案质量良好 |
| `api/client.ts` 的 CSRF 注入 / Idempotency-Key 生成 / 中文错误归一化 | 设计正确 |
| `lifecycle.models.ts` 的 `allowed_actions` 服务端驱动 | 权限 UI 的正确做法 |
| `AppShell` / `navigation` / `ActionButton`（含 a11y + reduced-motion） | 质量高于平均 |

### 6.2 必须修掉的（早期版本实测缺陷）

| 缺陷 | 证据 | 新方案 |
|---|---|---|
| `createApiClient()` 被 15 处各自实例化，导致 `inflight` / `operationKeys` 去重失效 | `client.ts:12-13` 状态是实例私有 | **单例 + 模块级状态**，并有测试断言跨模块共享 |
| 字段清单前端手写第二遍且已漂移（`material_name` vs `material_id`） | 25 个薄页面 | 后端 `GET /api/v1/meta/resources` 返回字段元数据，前端渲染；**抄 `template_catalog` 已上线形态** |
| 同一状态三种中文（`verified` → 待确认/已核验/已提交） | 14 处独立字面量 | 后端单一状态字典，前端只取 `{label, tone}` |
| 状态色表键风格两套（中文键 vs 状态码键），跨文件复用后静默全灰 | `returnModel.ts:43` | tone 用状态码联合类型，编译期约束 |
| 三处裸 `fetch` 绕过统一客户端 | `data-exchange.service.ts:44/60/79` | 全部走单例客户端 |
| `dist` 引用 `/assets/` 而部署路径是 `/yuxin/` | 直接部署白屏；CI 只跑默认 base | 构建脚本强制 `VITE_PUBLIC_BASE_PATH`，CI 增加带 base 的构建断言 |
| 15/53 页面无任何测试；`router.ts` 守卫链在单测中完全未执行 | 单测用假路由表 | 守卫链必须有针对性测试 |
| 前端无覆盖率工具也无门槛（后端有 `--cov-fail-under=85`） | | 引入覆盖率与门槛 |
| 无 ESLint / Prettier | | 引入 |

### 6.3 死代码

早期版本 `dataset.ts` 38.9 KB（占 src 字节 12%）零引用。新项目**不产生**这类文件；用 `tests/test_no_dead_export.py` + 构建体积预算约束。

---

## 7. 测试策略

| 层 | 工具 | 门槛 |
|---|---|---|
| 后端单元（kernel，无 DB） | pytest | 覆盖率 ≥ 85% |
| 后端集成（真实 MySQL） | pytest + 临时库 | 必须真跑，**不允许静默 skip**（早期版本 24 个集成测试在未设环境变量时静默跳过） |
| 架构约束（依赖方向、禁 import） | pytest + AST | 0 违规 |
| 权限一致性 | pytest 参数化 | Page/API/Agent 三者对同一 `(user, capability)` 三元组结论必须相同 |
| 对抗性（提示注入越权） | pytest | Operator 明确要求"偷偷创建采购单"必须失败 |
| 前端单元 | Vitest | 覆盖率门槛 |
| E2E | Playwright | 覆盖用户要求的 4 条流程 |

### 7.1 权限一致性矩阵（本项目的核心验收物）

```
              页面      REST      Agent
Admin          ✓         ✓         ✓
Manager        ✓         ✓         ✓
Operator       ✗        403        工具不暴露
Viewer         只读      只读      只读
DataScope-A    塘1,2     塘1,2     塘1,2
DataScope-A(越权) ✗      403        拒绝
```

这张矩阵由**同一份 Capability 声明**驱动生成，三个入口对同一行的结论必须一致——这才是需求里"防止智能体绕过人工页面权限"的**可证明**形态。

---

## 8. 技术栈（既定选型，不要随意替换）

Vue 3 · TypeScript · Vite · Flask · Python · MySQL · PyMySQL · DeepSeek Harness · Agent Gateway · RESTful API · Nginx · Gunicorn · Pytest · Vitest · Playwright

**明确不引入**：SQLAlchemy/Alembic（既定选型是 PyMySQL；迁移继续用手写编号 SQL + 单一 runner）、FastAPI、Docker、Redis、任何 ORM。

---

## 9. 明确不做

- 完整财务 ERP（成本只做塘口/批次维度的归集）
- 微信小程序 / Expo 移动端 / Electron 桌面端（早期版本有四端并存，其中两端是废弃的半成品）
- 数据交换（Excel 导入导出）——**降级为可选**，不在第一轮闭环内
- 多租户 SaaS 化

---

## 10. 验收

只有用户明确说"测试通过，可以替换早期版本"之后，才进入迁移/部署阶段。

在此之前：
- 早期版本 目录：0 改动
- 线上环境：0 改动
- GitHub：0 推送
