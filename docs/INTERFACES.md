# FPA 接口契约（冻结版 v1）

> 本文件是前后端与 Harness 插件**并行开发的法律依据**。任何一方需要改动，先改本文件、同步对方，再动代码。
> 所有路径以 `/api/v1` 为前缀。字段名一律 `snake_case`（与早期版本一致，便于前端资产继承）。

---

## 1. 响应信封（沿用早期版本已验证形态）

```jsonc
// 成功
{ "code": "OK", "message": "", "data": { ... }, "request_id": "3f2a…" }
// 失败
{ "code": "FORBIDDEN", "message": "当前账号没有权限执行该操作", "data": null, "request_id": "3f2a…" }
```

- `code` 为 `OK` 时 `data` 有效；非 `OK` 时 HTTP 状态码 ≥ 400 且 `data` 可为 `null`。
- `message` **永远是可直接展示给用户的简体中文**。技术细节只进日志与 `request_id`。
- 每个响应带 `X-Request-ID` 头，值等于 body 的 `request_id`。

### 1.1 错误码（唯一注册表，见 `backend/kernel/errors.py`）

<!-- BEGIN GENERATED: error-codes -->
| code | HTTP | 含义 |
|---|---:|---|
| `VALIDATION_ERROR` | 400 | 请求内容未通过 schema 校验 |
| `FIELD_INVALID` | 400 | 具体字段值非法；`data.field` 给出字段名 |
| `NOT_FOUND` | 404 | 资源不存在 |
| `CONFLICT` | 409 | 业务冲突（重复编码、被引用无法删除等） |
| `UNAUTHENTICATED` | 401 | 未登录或会话过期 |
| `FORBIDDEN` | 403 | 权限不足；`data.required_permission` 给出所需权限码 |
| `DATA_SCOPE_UNRESOLVED` | 403 | **数据范围无法解析——fail closed，不返回空集** |
| `DATA_SCOPE_DENIED` | 403 | 目标资源不在当前账号的数据范围内 |
| `CSRF_INVALID` | 403 | CSRF 令牌缺失或无效 |
| `RATE_LIMITED` | 429 | 触发限流；响应带 `Retry-After` |
| `IDEMPOTENCY_IN_PROGRESS` | 409 | 同键请求正在处理中（含前次崩溃未收口的情况） |
| `IDEMPOTENCY_CONFLICT` | 409 | 同一 Idempotency-Key 被用于不同请求内容 |
| `CONFIRMATION_INVALID` | 409 | 确认令牌无效、过期、已使用，或参数与确认时不一致 |
| `HUMAN_ONLY` | 409 | 该能力禁止智能体执行，只能由本人在页面完成 |
| `VERSION_CONFLICT` | 409 | 乐观锁冲突；`data.current_version` 给出当前版本 |
| `CAPABILITY_NOT_FOUND` | 404 | 能力未注册（Agent 的固定业务路由就靠它拒绝） |
| `TOOL_NOT_FOUND` | 404 | 请求的工具不在当前账号可用的工具清单内 |
| `AGENT_CONTEXT_INVALID` | 401 | Agent 上下文令牌无效或已过期 |
| `AGENT_UNAVAILABLE` | 503 | Harness 运行时不可用 |
| `AGENT_TIMEOUT` | 504 | 单轮超时 |
| `AGENT_PROTOCOL_ERROR` | 502 | Harness 返回的协议消息不符合约定 |
| `INTERNAL_ERROR` | 500 | 未预期错误；必须带 `request_id` 供排查 |
| `SERVICE_UNAVAILABLE` | 503 | 依赖服务暂时不可用 |

共 **23** 个错误码。这张表由 `tools/gen_contract_docs.py` 从 `backend/fpa/kernel/errors.py` 生成，**不要手工编辑**——CI 会校验它与内核逐字节一致。
<!-- END GENERATED: error-codes -->

<!-- BEGIN GENERATED: tone-and-actions -->
**`Tone`（状态配色语义，只有 5 个值）**：`neutral`、`info`、`success`、`warning`、`danger`

**`RowAction`（行内动作）**：`view`、`edit`、`delete`、`submit`、`approve`、`verify`、`correct`、`archive`、`cancel`、`confirm`、`close`

前端 tone 色表的键**只能**是上述 5 个值之一。早期版本用中文标签当色表键，跨文件复用后静默全灰且测试抓不到（`早期版本 returnModel.ts:43`）——现在未知状态必须显式降级为 `neutral`，**不允许是空串**。

`row_actions` 由资源状态机派生（各状态动作的并集），是**静态上限**；某一行实际能做什么仍由服务端按该行状态算出的 `allowed_actions` 决定。
<!-- END GENERATED: tone-and-actions -->

---

## 2. 元数据接口（前端字段契约的权威源）

> 这是本项目**最重要的一条接口**。早期版本把同一份字段清单在前端手写了第二遍并已漂移，新系统必须让前端**渲染**而不是**声明**。

### `GET /api/v1/meta/capabilities`

返回当前登录用户**有权调用**的全部能力声明（L1 过滤的服务端形态，前端菜单/按钮/表单均由此驱动）。

```jsonc
{
  "code": "OK",
  "data": {
    "capabilities": [
      {
        "name": "pond.create",
        "title": "新建塘口",
        "domain": "master_data",
        "resource": "pond",
        "method": "POST",
        "path": "/api/v1/ponds",
        "kind": "create",                  // read|create|update|delete|action
        "risk": "normal",                   // normal|high
        "confirmation": "never",            // never|always  （只约束 Agent 入口）
        "agent_exposure": "exposed",        // exposed|hidden|human_only
        "required_permission": "pond.create",
        "scope_required": true,
        "fields": [
          {
            "key": "code",
            "label": "塘口编号",
            "type": "string",               // string|text|integer|number|boolean|date|datetime|enum|ref
            "required": true,
            "max_length": 64,
            "placeholder": "如 P-001",
            "help": "全场唯一"
          },
          {
            "key": "area_id",
            "label": "所属区域",
            "type": "ref",
            "required": true,
            "ref": { "resource": "area", "label_key": "name" }
          },
          {
            "key": "farm_id",
            "label": "所属基地",
            "type": "ref",
            "required": true,
            "readonly": true,
            "help": "由当前账号的数据范围自动确定，不可修改",
            "ref": { "resource": "farm", "label_key": "name" }
          },
          {
            "key": "status",
            "label": "状态",
            "type": "enum",
            "readonly": true,
            "choices": [
              { "value": "build",   "label": "待建设" },
              { "value": "stocked", "label": "已放苗" }
            ]
          }
        ],
        "row_actions": ["view", "edit", "delete", "submit", "verify"]
      }
    ],
    "resources": [
      {
        "name": "pond",
        "title": "塘口",
        "list_path": "/api/v1/ponds",
        "columns": [
          { "key": "code", "label": "塘口编号" },
          { "key": "name", "label": "塘口名称" },
          { "key": "area_name", "label": "所属区域" },
          { "key": "status_label", "label": "状态", "tone_key": "status" }
        ],
        "search": false,
        "filters": [],
        "status_dict": [
          { "value": "build",   "label": "待建设", "tone": "neutral" },
          { "value": "stocked", "label": "已放苗", "tone": "info" },
          { "value": "farming", "label": "养殖中", "tone": "success" },
          { "value": "rest",    "label": "休整",   "tone": "warning" },
          { "value": "clean",   "label": "清塘中", "tone": "info" },
          { "value": "rebuild", "label": "待重建", "tone": "danger" }
        ]
      }
    ]
  }
}
```

> **关于 `status_dict` 的取值**：塘口 6 态（`build/stocked/farming/rest/clean/rebuild`）
> 取自早期版本已生产验证的 DB ENUM（`早期版本 008_master_data.sql:36`）与其转移表
> （`早期版本 master_data_service.py:21-24`）。**不要自造状态值**——契约里的每一个字面值
> 都必须可追溯到被验证过的来源。裁决记录见 `docs/DECISIONS.md` Q1。

**`search` / `filters`：列表页的"找得到"由服务端声明，前端只渲染。**

这两个键**无条件下发**（不声明时是 `false` / `[]`）——与 `Capability.fields` 同一口径：
前端"缺这个键"与"服务端说不需要控件"必须是**两件可分辨的事**，否则"后端漏发"会退化成
"页面上少一个搜索框"而不报错。声明的唯一目的是让前端不必硬编码"哪个资源有搜索框"。

```jsonc
// 以 sales_order 的真实声明为例（`Resource.to_meta()` 的输出）
"search": true,                       // true ⇒ 渲染 `?keyword=` 搜索框（模糊匹配编码/名称）
"filters": [
  { "key": "status", "label": "状态", "type": "status" },
  { "key": "customer_id", "label": "客户", "type": "ref",
    "ref": { "resource": "partner", "label_key": "name", "list_path": "/api/v1/partners" } },
  { "key": "sold_at", "label": "销售日期", "type": "date_range",
    "params": { "from": "sold_from", "to": "sold_to" } }
]
```

`type` 的取值集合由内核 `kernel/workflow.py::FilterKind` 固定为六个：

| `type` | 前端控件 | 查询参数名怎么来 | 候选值怎么来 |
|---|---|---|---|
| `status` | 下拉 | `key` | 该资源的 `status_dict`（**状态中文的唯一来源仍是状态机**，不许在这里复制一份） |
| `enum` | 下拉 | `key` | 声明里的 `choices` |
| `ref` | 下拉 | `key` | `ref.list_path` 指向的列表接口（**不许拼接**） |
| `date_range` | **一个**控件、两个日期输入 | `params.from` / `params.to`（**不是 `key`**） | 无 |
| `boolean` | 勾选框 | `key`（`true` 才发参数） | 无 |
| `string` | 文本框 | `key` | 无 |

三条硬约束（都有机械守卫，不是约定）：

1. **`type != "enum"` 不许给 `choices`，`type != "ref"` 不许给 `ref`，
   `type != "date_range"` 不许给 `params`** —— 构造期报错。声明了却不生效的字段
   会让前端渲染出一个永远不起作用的控件。
2. **声明的参数名必须是 list 处理器真接受的**：`declared but ignored` 会 200 且返回全表，
   是**最危险的那类静默失败**（用户以为筛过了）。
3. **`date_range` 的上界含当天**：`kernel/date_bounds.py::upper_bound` 是唯一实现
   （DATETIME 列上必须 `< DATE_ADD(%s, INTERVAL 1 DAY)`，否则"填今天筛不到今天"）。

**`readonly` 的含义**：该字段出现在响应与表单里，但**不接受客户端提交**。
典型用途是数据范围字段（`farm_id`/`area_id`）——由服务端从当前账号的 DataScope
解析后写入（见 Q7）。前端渲染成禁用输入框并显示解析结果，让用户**看得见**自己的
数据范围落在哪里。

**约束（由测试强制）**：

1. `fields` 中出现的每个可写 key，必须与后端 `Capability` 声明的字段**一一对应**；不一致则测试失败。前端不得出现任何硬编码字段名。
2. `status_dict` 是**全系统唯一**的状态中文来源。前端 `getStatusLabel()` 类函数只能读它，禁止字面量。
3. `tone` 枚举固定：`neutral | info | success | warning | danger`，前端 `tones` 表的键用这 5 个值（早期版本用中文标签当键导致静默全灰）。
4. `confirmation` 只有 `never` / `always` 两个值（`by_key` 已删除，见 Q4）。
5. `readonly: true` 的字段**不得**出现在 Agent Tool 的 `input_schema` 里——模型不该去填它无法影响的值。

---

## 3. Agent 对话协议

### `POST /api/v1/agent/turns`

请求：
```jsonc
{
  "message": "给 3 号塘今天投喂 50kg 1号饲料",
  "conversation_id": "可选，缺省时服务端生成",
  "page_context": "/ponds/3",          // 可选，当前页面路径
  "history": [                          // 可选，最近 N 轮，客户端携带
    { "role": "user", "text": "…" },
    { "role": "assistant", "text": "…" }
  ]
}
```

响应（`kind` 判别联合）：
```jsonc
// ① 纯回答 / 查询结果
{ "kind": "assistant", "conversation_id": "c1", "message": "3 号塘最近 7 天共投喂 210kg，日均 30kg。" }

// ② 信息不足，反问
{ "kind": "clarification", "conversation_id": "c1",
  "question": "请确认使用哪个物料：",
  "options": ["1号饲料（鲤鱼配合饲料）", "2号饲料（对虾配合饲料）"],
  "allow_free_text": true }

// ③ 高风险写操作，待确认
{ "kind": "confirmation_required", "conversation_id": "c1",
  "message": "即将为 3 号塘登记投喂 50kg 1号饲料，确认后立即扣减库存并计入成本。",
  "confirmation": {
    "id": 128,
    "token": "一次性令牌，仅返回一次",
    "capability": "feeding.create",
    "title": "登记投喂",
    "target": "3 号塘 / 批次 B-2026-007",
    "rows": [ { "label": "物料", "value": "1号饲料" },
              { "label": "数量", "value": "50 kg" },
              { "label": "发生时间", "value": "2026-09-12" } ],
    "impact": ["扣减 1 号仓 1号饲料 50kg", "计入批次 B-2026-007 投喂成本"],
    "expires_at": "2026-09-12T12:34:56Z"
  } }

// ④ 已执行
{ "kind": "executed", "conversation_id": "c1",
  "message": "已登记投喂：3 号塘 50kg 1号饲料，库存剩 420kg，成本增加 ¥240.00。",
  "result": {
    "capability": "feeding.create",
    "resource": "feeding",
    "resource_id": 991,
    "url": "/feeding/logs",
    "data": { "executed": [ { "capability": "feeding.create", "resource": "feeding", "resource_id": 991 } ] }
  } }
```

> **`executed` 的两个附加字段（t14 追加，非破坏性）**
>
> `result.resource` —— 这次写入牵动的**资源名**（与 `ResourceMeta.name` /
> `Capability.resource` 同一命名空间）。前端不从这个字段以外的地方推断资源：
> 能力名到资源名**不是**字符串切分能得出的关系（`pond_status_change.request` 的
> 资源是 `pond_status_change`，而 `sales_order.approve` 的资源是 `sales_order`）。
>
> `result.data.executed` —— 当前迭代牵动过的**全部**写入。顶层 `result` 只能带一条
> （取最后一次），而一次对话里模型可能写多次（实测：先建往来单位、再建批次）；
> 刷新列表需要的是**资源集合**，不是"最后一个是谁"。
>
> **谁用它**：`AgentPanel` 在 `kind === 'executed'` 时发布"写入信号"
> （`agent/write-signal.ts`），`ResourceListPage` 订阅它、且**只在自己这个资源
> 被写入时**重拉列表。判据必须是服务端这个 `kind`，**不是**回复文本里有没有
> "已创建" —— 文本判据会被"我**没有**创建成功"这类句子骗到。

> **`confirmation_required` 的两个附加字段（非破坏性）**
>
> `confirmations` —— 同一轮签发的**全部**待确认卡片，按发生顺序；`confirmation`
> 仍是其中第一张，所以按上面 ③ 原文实现的客户端行为不变。
> 一次对话里模型可以对多个对象各签一张卡（实测：一句话要求归档 3 个草稿区域 → 3 张卡），
> 而每张卡的 `token` 都是**一次性、只在这一轮响应里出现**的 —— 界面上少给一张，
> 那一张就永远无法确认（没有补发的入口）。
>
> `message` —— 优先是**模型这一轮说的话**，网关渲染的「即将X，确认后立即生效。」
> 只在模型没说话时兜底。理由：卡片自己只有 `title`/`target`/`rows`/`impact`，
> 说不出"为什么是这几个对象"，而那句话正是用户的上下文。
>
> **`clarification` 的附加字段**：`message`（可选，同上口径）—— `question` 只说
> "要问什么"，模型在提问前后补的上下文（实测：「（可选补充：联系人、电话、地址、
> 结算天数、信用额度。）」）不在里面，丢了它用户就只看到一句被截短的问句。

> **一轮里同时命中多种 `kind` 时的优先级**（单值字段，必须选一个）：
> `executed` > `confirmation_required` > `clarification` > `assistant`。
> 混合轮次仍返回 `kind: "executed"` 以触发列表刷新，但必须同时附带
> `confirmation` / `confirmations`；确认令牌只返回一次，不能要求用户重新发起。
> 判定收在 `agent_turn.turn_result()` 一处，流式与非流式共用同一个函数。

### `POST /api/v1/agent/turns/stream`

`Content-Type: application/x-ndjson`，每行一个 JSON：
```jsonc
{ "type": "status", "text": "正在查询塘口…" }
{ "type": "delta",  "text": "3 号塘" }
{ "type": "result", "result": { /* 上面四种之一 */ } }
```
**约束**：写操作**只能**通过 `result` 行交付，绝不允许把待确认操作"流式渲染成已完成"。前端在收到 `result` 前不得清理 `busy` 状态。

> **`delta` 里只装「模型对用户说的话」**：同一条 `assistant/chunk` 事件按 `chunk.type`
> 分成正文（`text-delta`）与**思考**（`reasoning-delta`）等几种，只有前者能外发。
> 模型是**用英文推理、用中文作答**的，思考一旦混进正文，用户就会看到
> 「我来您I must执行。Actually.我这边」这种中英夹杂的碎片（实测 34 个会话命中）。
> 判据写成**白名单**（只认 `text-delta`）：上游以后新增的 chunk 类型默认不外发。

> **字段名确认（裁决）**：是 `result`，**不是** `data`。早期版本用的是 `data`（`早期版本 agent_stream.py`），新契约统一为 `result`——与 `POST /agent/turns` 的响应体字段名保持一致，避免同一个东西在两条路径上叫两个名字。后端 stream 适配器必须同步使用 `result`。

### `POST /api/v1/agent/confirmations/{id}/confirm`

请求：`{ "token": "…" }`
响应：`executed` 形态（同 ④）。重复提交 → `CONFIRMATION_INVALID`。

### `POST /api/v1/agent/confirmations/{id}/cancel`

响应：`{ "kind": "cancelled" }`

---

## 4. Harness ↔ Gateway 协议（插件与后端之间的契约）

### `GET /api/v1/agent/tools`

由 **Harness 插件**在会话启动时调用，携带上下文令牌。返回该用户当前可用的工具清单（L1 过滤）。

```jsonc
{
  "code": "OK",
  "data": {
    "tools": [
      {
        "name": "pond_create",
        "description": "新建塘口。需要 pond.create 权限。",
        "parameters": { "type": "object", "properties": { … 标准 JSON Schema … }, "required": ["code","area_id"] },
        "capability": "pond.create"
      }
    ]
  }
}
```

> **为什么走这个接口而不是环境变量**：早期版本把工具目录 JSON 塞进 `FPA_AGENT_TOOL_CATALOG` 环境变量，源码注释自承"为绕开 Windows 进程环境变量上限"。新系统工具 schema 从 HTTP 拉取。

### `POST /api/v1/agent/tools/{tool_name}/call`

请求（插件构造）：
```jsonc
{ "arguments": { "code": "P-004", "name": "4号塘", "area_id": 2 } }
```
请求头：
- `X-Agent-Context: <上下文令牌>`（必需，绑定 user/session/conversation/nonce/expiry）
- `Idempotency-Key: <可选>`

响应：`data` 为判别联合，与第 3 节四种 `kind` 一致（`assistant` 除外）。
**插件必须把 `kind` 原样交给模型**，让模型知道"这是待确认"而不是"已完成"。

### 4.1 上下文令牌

- 由后端签发，格式：`base64url(payload).hmac_sha256`
- payload：`{ uid, sid (session hash), cap, cid (conversation), iat, exp, nonce }`
- **有效期：会话级（默认 1800 秒，`AGENT_CONTEXT_TTL_SECONDS`）**

> ## ⚠️ 关于有效期的一处修正（必读）
>
> 本文早先同时写了三条互相矛盾的话："有效期：单轮（默认 90s）"+
> "每轮对话重新签发"+"由插件从受控配置读取"。
> **插件在 `apply()` 时只读一次配置**，若令牌每轮轮换，**第二轮起每次调用都会 401**。
>
> 修正后自洽的一套：**令牌按会话签发，插件在进程启动时读一次。**
>
> 真正的授权实时性**不来自令牌短命**，而来自每次调用时对实时会话的重新校验：
>
> `
> routes_agent._actor_from_context_token()
>   -> _current_actor() -> AccessService.resolve_session()
> `
>
> 因此：
>
> | 事件 | 效果 |
> |---|---|
> | 用户登出 | 会话被撤销，令牌**立刻**失效（不必等 TTL） |
> | 管理员改权限 | 下一次调用就按**新**权限判定 |
> | 令牌被泄露 | 攻击者还需要该会话**仍然有效** |
>
> 令牌在会话有效且 TTL 未到期时可以重放，因此必须只存在于受控 Harness 子进程，
> 不落日志、不返回浏览器。缩短 TTL 的代价是到期时重建子进程；生产部署应按会话时长
> 与风险接受度调整 `AGENT_CONTEXT_TTL_SECONDS`。
>
> 把令牌改成单轮短命的代价是"必须每轮重建子进程"——
> 冷启动 1.8 秒。早期版本正是坚持短命令牌，才不得不每轮丢弃子进程重建。
> 详见 `docs/DECISIONS.md` Q17。

- 令牌不落日志、不返回浏览器；后端只把它注入对应的受控 Harness 子进程环境，
  插件读取后会清理可继承的凭据环境变量。

---

## 5. 鉴权

| 场景 | 机制 |
|---|---|
| 浏览器 | HttpOnly Cookie 会话 + `X-CSRF-Token`（写操作必需） |
| Harness 插件 → Gateway | `X-Agent-Context` 上下文令牌 |
| 插件 → 后端不需要 | 用户密码、DB 凭据、Flask secret |

**CSRF 改为全局前置校验**（早期版本是 34 处手写 `require_csrf()` 散落 12 文件、无拦截器）：`web/middleware.py` 对非安全方法统一校验，白名单显式声明。

---

## 6. 数据范围声明形态

```python
# 能力声明里写这个
scope=ScopePolicy.resource("area_id")        # 资源表的 area_id 列受控
scope=ScopePolicy.resource("farm_id")
scope=ScopePolicy.owner("created_by")         # personal 范围
scope=ScopePolicy.none()                      # 不受范围约束（如"列出我有权访问的塘口"）
```

`ScopePolicy.resource(col)` 在 SQL 层生成 `AND <alias>.<col> IN (…)`。**无法生成谓词时抛 `DATA_SCOPE_UNRESOLVED`**。

---

## 7. 版本与乐观锁

- 所有可编辑资源带 `row_version INT UNSIGNED NOT NULL DEFAULT 1`
- 更新请求必须带 `expected_version`；不匹配 → `CONFLICT` 409 且 `data.current_version`
- 响应中每个资源带 `version` 与 `allowed_actions`（服务端算，前端只渲染）

---

## 8. 分页（全系统统一，禁止各写各的）

请求：`?page=1&page_size=20`（`page_size` 上限 100，超限 400）

响应：
```jsonc
{ "items": [ … ], "page": 1, "page_size": 20, "total": 137, "has_next": true }
```

早期版本实测有 **25 处重复的分页 SQL、31 处重复的 `has_next` 计算**。新系统由 `kernel/pagination.py` 单点提供。

---

## 9. 前端类型生成

`tools/gen_frontend_types.py` 从 Capability 注册表生成 `frontend/src/layers/common/types.gen.ts`：

```ts
export type ErrorCode = 'VALIDATION_ERROR' | 'FORBIDDEN' | ...
export type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'danger'
export interface CapabilityField { key: string; label: string; ... }
export interface Capability { name: string; ... }
export interface ResourceMeta { name: string; status_dict: StatusEntry[]; ... }
```

CI 断言：重新生成的结果与仓库中的文件**逐字节一致**（防止手改生成物）。

> 注意：**不依赖 OpenAPI codegen**。早期版本实测业务请求体是通用 `$ref MutationRequest`，字段级信息不在 OpenAPI schema 内（187 个 op 中 160 个只含信封引用）。字段契约走 `/api/v1/meta/capabilities`。

---

## 附录 A. 字段类型枚举（由生成器产出）

<!-- BEGIN GENERATED: field-types -->
| 类型 | 前端控件 | JSON Schema 类型 |
|---|---|---|
| `string` | 单行文本 | `string` |
| `text` | 多行文本 | `string` |
| `integer` | 整数输入 | `integer` |
| `number` | 小数输入（金额/数量） | `number` |
| `boolean` | 开关 | `boolean` |
| `date` | 日期选择 | `string` |
| `datetime` | 日期时间选择 | `string` |
| `enum` | 下拉（选项来自 `choices`） | `string` |
| `ref` | 下拉（选项来自 `ref.list_path`） | `integer` |
| `array` | 多选（元素类型来自 `items`） | `array` |

共 **10** 种字段类型。同样由生成器产出。
<!-- END GENERATED: field-types -->
