/**
 * ⚠️ 本文件是**占位实现**，将由 `tools/gen_frontend_types.py` 覆盖。
 *
 * 权威来源：`yuxin/docs/INTERFACES.md`（冻结版 v1）
 *   - §2   `GET /api/v1/meta/capabilities` 的形状（Capability / CapabilityField / ResourceMeta / StatusEntry）
 *   - §1.1 错误码注册表
 *   - §9   本文件的目标形状
 *
 * 后端真实内核（`yuxin/backend/yuxin/kernel/`）已存在，本文件按它的真实输出对齐，
 * 而不是按文档的示意代码对齐。两处实测差异已在报告里提出（见 §F 的疑问清单）：
 *
 *   1. `Capability.to_meta()`（`kernel/capability.py`）实际输出的字段是
 *      name/title/domain/method/path/kind/risk/confirmation/agent_exposure/
 *      required_permission/scope_required/idempotent/description/fields/path_parameters。
 *      它**不含** INTERFACES.md §2 示例里的 `resource` 与 `row_actions`。
 *      本文件按内核真实输出定义（`idempotent: boolean`），并把 `resource` /
 *      `row_actions` 声明为可选，以便后端补齐时无需改前端。
 *   2. `FieldType`（`kernel/fields.py`）的 **10** 个取值与 INTERFACES.md §2 一致：
 *      string|text|integer|number|boolean|date|datetime|enum|ref|array。
 *      第 10 个（`array`）是 access 域七条能力落地时补进内核的：registry §2.4 的
 *      `role_ids` / `scope_ids` / `permission_codes` 是 `integer[]` / `string[]`，
 *      而当时没有类型可以声明它们。它在 `Field.items` 上给出元素类型。
 *   3. `ErrorCode`（`kernel/errors.py`）比 INTERFACES.md §1.1 的表多 6 个码
 *      （CSRF_INVALID / VERSION_CONFLICT / TOOL_NOT_FOUND / AGENT_CONTEXT_INVALID /
 *      AGENT_PROTOCOL_ERROR / SERVICE_UNAVAILABLE），且文档表里的 `CONFLICT`
 *      在错误码映射中由 `VERSION_CONFLICT` 承担版本冲突语义。
 *      本文件以**内核枚举**为准（它是唯一注册表）。
 *
 * 为什么手写占位而不是等生成器：`DynamicForm` / `DataTable` / `AgentPanel` 都必须
 * 依赖这些类型才能编译，且类型形状必须严格对齐，否则生成器上线后会出现大面积改错。
 */

/** 错误码——必须与 `backend/yuxin/kernel/errors.py::ErrorCode` 逐字一致。 */
export type ErrorCode =
  // 请求侧
  | 'VALIDATION_ERROR'
  | 'FIELD_INVALID'
  | 'NOT_FOUND'
  | 'CONFLICT'
  // 鉴权 / 授权
  | 'UNAUTHENTICATED'
  | 'FORBIDDEN'
  | 'DATA_SCOPE_UNRESOLVED'
  | 'DATA_SCOPE_DENIED'
  | 'CSRF_INVALID'
  | 'RATE_LIMITED'
  // 治理
  | 'IDEMPOTENCY_IN_PROGRESS'
  | 'IDEMPOTENCY_CONFLICT'
  | 'CONFIRMATION_INVALID'
  | 'HUMAN_ONLY'
  | 'VERSION_CONFLICT'
  // Agent
  | 'CAPABILITY_NOT_FOUND'
  | 'TOOL_NOT_FOUND'
  | 'AGENT_CONTEXT_INVALID'
  | 'AGENT_UNAVAILABLE'
  | 'AGENT_TIMEOUT'
  | 'AGENT_PROTOCOL_ERROR'
  // 兜底
  | 'INTERNAL_ERROR'
  | 'SERVICE_UNAVAILABLE'

/**
 * 状态配色标签。**固定这 5 个值**。
 *
 * 早期版本实测缺陷（.local/recon-frontend.md 问题 P-2）：20 处 `tones` 声明中有 15 处
 * 用**中文标签**当键（`returnModel.ts:43` 的 `{ 草稿: 'slate', … }`），而取色逻辑
 * `DataTablePage.vue:91` 按 `row[column.key]` 查表。跨文件复用后必然查不到，
 * 结果全部静默退化为默认灰色，且因为"颜色错也不报错"，测试抓不到。
 */
export type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'danger'

/** 字段类型——与 `kernel/fields.py::FieldType` 的 10 个取值一致。 */
export type FieldType =
  | 'string'
  | 'text'
  | 'integer'
  | 'number'
  | 'boolean'
  | 'date'
  | 'datetime'
  | 'enum'
  | 'ref'
  /** 数组（`integer[]` / `string[]`）：元素类型在 `CapabilityField.items` 上。 */
  | 'array'

/** 枚举可选值——对应 `kernel/fields.py::Choice`。 */
export interface ChoiceEntry {
  value: string
  label: string
}

/**
 * 筛选控件的类型——对应 `kernel/workflow.py::FilterKind`。
 *
 * 六个值中前五个来自 `INTERFACES.md` §2；`date_range` 是内核**消歧**加的一个
 * （`date` 与 `date_range` 都是"填日期"，但前者一个查询参数、后者**两个**）。
 * 本仓当前**没有** `date` 的使用者，所以这里不声明它——声明一个没有生产者、
 * 也没有消费者的取值，只会让"到底该渲染什么"多一种猜法。
 *
 * 每种类型对应一个控件（前端按**声明**选控件，不按资源名分支）：
 *   * `status`     —— 下拉，候选值取自该资源的 `status_dict`（**不下发 choices**，
 *                     状态的唯一来源是状态机，不许在这里复制一份中文）；
 *   * `enum`       —— 下拉，候选值随声明下发（`choices`）；
 *   * `ref`        —— 下拉，候选值来自 `ref.list_path` 指向的列表接口；
 *   * `boolean`    —— 勾选框，"勾上就多一个 WHERE 条件"（如"仅看逾期"）；
 *   * `string`     —— 文本输入；
 *   * `date_range` —— **一个**控件、**两个**日期输入，查询参数名在 `params` 里。
 */
export type FilterKind = 'status' | 'enum' | 'ref' | 'date_range' | 'boolean' | 'string'

/** `date_range` 的两个查询参数名（对应 `kernel/workflow.py::DateRangeParam`）。 */
export interface FilterParams {
  /** 起始参数名。缺省表示该筛选没有起点（单边区间）。 */
  from?: string
  /** 结束参数名。缺省表示该筛选没有终点（单边区间）。 */
  to?: string
}

/**
 * 列表页筛选条上的**一个**控件声明——对应 `kernel/workflow.py::FilterSpec.to_meta()`。
 *
 * ## `key` 是查询参数名，不是描述
 *
 * 对**单参数**筛选，`key` **同时**是 list 接口真实的查询参数名（`?<key>=…`）
 * 与 `Resource.columns` 里的列名——两处同名是**刻意的**（内核在构造期强制
 * `key ∈ columns`）：若"参数名"与"取值的列"是两套名字，同一条筛选就有两处真相。
 *
 * `date_range` 是唯一例外：它有两个参数，名字在 `params` 里（`key` 只是控件标识）。
 * 前端因此**不许**自己推导参数名——推导必漂移，而错了不报错，症状是
 * "填了日期却筛不掉任何行"。
 */
export interface FilterMeta {
  key: string
  /** 控件的中文标签（前端不写映射表）。 */
  label: string
  type: FilterKind
  /** 仅 `type === 'enum'` 时下发。 */
  choices?: ChoiceEntry[]
  /**
   * 仅 `type === 'ref'` 时下发。`list_path` 已由**内核**按引用资源的声明解析好
   * （`RefTarget.resolved_list_path`），前端直接用它拉候选，不拼接、不猜复数。
   *
   * 筛选声明里**不存在多态形态**：内核在构造期就拒绝多态 ref
   * （筛选条的兄弟字段不一定同时存在，既拿不到资源名也解析不出地址）。
   * 所以这里不声明 `resource_field`。
   */
  ref?: { resource: string; label_key: string; list_path: string }
  /** 仅 `type === 'date_range'` 时下发。 */
  params?: FilterParams
}

/** `ref` 类型的取值来源——对应 `kernel/fields.py::RefTarget`。 */
export interface RefTarget {
  /**
   * 静态形态：本字段永远引用的资源名。
   *
   * **多态形态下是空串**，不要拿它去查 `ResourceMeta`——那样永远查不到，
   * 症状是"下拉一直是空的"。多态请走下面的 `resource_field`。
   */
  resource: string
  label_key: string
  /**
   * 候选值的列表地址。
   *
   * INTERFACES.md §9 的字段类型表写的是「下拉（选项来自 ref.list_path）」；
   * 若后端未提供，前端回退到 ResourceMeta.list_path（见 DynamicForm）。
   */
  list_path?: string
  /**
   * **多态**形态：目标资源由本能力**另一个字段**的取值决定（取值就是资源名）。
   *
   * 实测来源：`cost.entry.create` 的 `归属对象`（`target_id`）——
   * `resource_field: 'target_type'`，即"选区域时拉区域列表、选塘口时拉塘口列表"。
   *
   * 前端**不写 `{area: …, pond: …}` 映射表**（那会是同一个事实的第二处来源，
   * 且随后端加值漂移）：只按这个名字去 `values` 里取值，再用取到的名字查
   * `ResourceMeta.list_path`。取值到资源的对应关系因此**只有后端一处**。
   */
  resource_field?: string
}

/**
 * 一个可写字段的元数据。
 * 对应 `kernel/fields.py::Field.to_meta(key)` 的**真实输出**。
 */
export interface CapabilityField {
  key: string
  label: string
  type: FieldType
  /** `Field.to_meta()` 无条件输出该键（即使为 false）。 */
  required: boolean
  max_length?: number
  /**
   * 该字段出现在响应与表单里，但**不接受客户端提交**。
   *
   * INTERFACES.md §2：「典型用途是数据范围字段（farm_id/area_id）——由服务端从当前
   * 账号的 DataScope 解析后写入。前端渲染成禁用输入框并显示解析结果，让用户看得见
   * 自己的数据范围落在哪里。」例：pond_status 是只读派生。
   */
  readonly?: boolean
  precision?: number
  placeholder?: string
  help?: string
  /** 仅当后端字段声明了非 null 默认值时才出现。 */
  default?: unknown
  choices?: ChoiceEntry[]
  /**
   * 该字段的候选值**取决于别的字段或运行期数据**，因此元数据这一次给出的可能不是
   * 精确集合。
   *
   * 实测来源有两处（`kernel/fields.py::Field.to_meta`）：
   *   * `dynamic_choices` 声明了，但没有具体行可依（`current_status is None`）——
   *     此时 `choices` 是全集。例：`cost.entry.create` 的 `category_code`；
   *   * `ref.resource_field` 多态引用——目标资源由兄弟字段的取值决定，所以
   *     `ref.resource` 是空串。例：`cost.entry.create` 的 `target_id`。
   *
   * 它**不是**渲染开关：多态与否的唯一依据是 `ref.resource_field`。
   * 这里把它声明出来，是因为后端真的会下发这个键——不声明就等于把一处
   * 契约差异藏起来（`vue-tsc` 会对着真实数据报 TS2353）。
   */
  dynamic?: boolean
  ref?: RefTarget
  /**
   * `type === 'array'` 时的**元素类型**（`'integer'` / `'string'`）。
   *
   * 由后端 `Field.items` 给出，而不是由控件去猜：
   * 与 `ref.list_path` 同一手法——让声明决定渲染。
   */
  items?: string
}

/** 能力类型——对应 `Capability.kind`（自由字符串，实测取值见下）。 */
export type CapabilityKind = 'read' | 'create' | 'update' | 'delete' | 'action'

/** 风险等级——对应 `kernel/capability.py::Risk`。 */
export type Risk = 'read' | 'normal' | 'high'

/** 确认闸门生效值——对应 `Capability.effective_confirmation`。 */
export type Confirmation = 'never' | 'always'

/** 智能体暴露度——对应 `kernel/capability.py::AgentExposure`。 */
export type AgentExposure = 'exposed' | 'hidden' | 'human_only'

/**
 * 一个业务能力的服务端声明。
 * 对应 `Capability.to_meta()` 的真实输出（注意：**无 `resource`**，见文件头说明 1）。
 */
export interface Capability {
  name: string
  title: string
  domain: string
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  path: string
  kind: CapabilityKind
  risk: Risk
  confirmation: Confirmation
  agent_exposure: AgentExposure
  required_permission: string | null
  scope_required: boolean
  /** `Capability.requires_idempotency_key`：`idempotent` 或需确认时为 true。 */
  idempotent: boolean
  description: string
  fields: CapabilityField[]
  path_parameters: string[]
  /**
   * 该能力归属的资源（单数，与 `ResourceMeta.name`、`RefTarget.resource` 同一命名空间）。
   *
   * INTERFACES.md §2 定义了它，**后端必须输出** —— 前端靠它把能力绑到资源，
   * 从而取到 `resources[].columns` 与 `status_dict`。
   */
  resource: string
  /**
   * 该资源状态机所有动作的**并集**，即静态上限。
   *
   * INTERFACES.md §2：「某一行实际能做什么仍由服务端按该行状态算出的
   * `allowed_actions` 决定」。因此前端渲染按钮**只读行上的 allowed_actions**，
   * 本字段仅用于「哪些动作理论上可能出现」的提示/校验。
   */
  row_actions: string[]
}

/**
 * `status_dict` 的一项：**全系统唯一**的状态中文与配色来源。
 * 前端禁止再出现任何状态中文字面量。
 */
export interface StatusEntry {
  value: string
  label: string
  tone: Tone
  /** 终态：进入后不能再转移（服务端 `require_transition` 会拒）。 */
  terminal?: boolean
  /**
   * 刻意预留的状态：保留在枚举里，但**没有任何能力能进入它**。
   *
   * 例：`cost_entry.archived`（registry §1.9 只给成本域 5 条能力）、
   * `sales_order.closed`（§3.5 原文标注“预留、无触发能力”）。
   */
  reserved?: boolean
  /**
   * 是否应当出现在**用户可选的列表**里（筛选下拉、目标状态下拉）。
   *
   * 判据是 `!terminal && !reserved`，由服务端 `State.selectable` 给出——
   * 前端**不自己重算**：一旦前端各处写 `!e.reserved && !e.terminal`，
   * 这条规则就有了第二处实现。
   *
   * **注意两者用途不同**：渲染行的状态标签时必须用**全部**项
   * （否则历史数据里的 `archived` 行会退化成裸状态码）；
   * 只有渲染**下拉**时才跳过 `selectable === false`。
   */
  selectable?: boolean
}

/**
 * 行内动作的动作词与中文标签（`/meta/capabilities` 的 `actions` 段）。
 *
 * 标签的**唯一来源是服务端**
 * （`backend/yuxin/kernel/workflow.py::ACTION_LABELS`，由 `workflow_meta.actions_payload()` 下发）。
 * 前端禁止写 `{view:'查看', ...}` 这种映射表——那就是"同一件事两处描述"。
 */
export interface ActionsMeta {
  /** 状态配色语义（只有 5 个值）。 */
  tones: string[]
  /** 全部合法动作词（闭集）。 */
  row_actions: string[]
  /** 动作词 → 中文标签。查不到时前端回退显示原词。 */
  row_action_labels: Record<string, string>
}

/** 列表列声明——对应 `FieldSet.columns()` 的输出，另加 `tone_key`。 */
export interface ColumnMeta {
  key: string
  label: string
  /**
   * 指定该列取配色的依据列。
   *
   * INTERFACES.md §2 示例中 `status_label` 列带 `"tone_key": "status"`——
   * 即"显示中文标签、按状态码取色"。这正是早期版本 `RETURN_TONES` 想做却做错的事
   * （它直接把中文标签当键）。新形状把两者显式分开：
   * 用 `row[tone_key]`（状态码）去查 `status_dict`，而不是用 `row[key]`（中文）。
   *
   * 简写形式（`tone_key` 缺省）表示该列自身就是状态码。
   */
  tone_key?: string
  /** 列宽提示（可选）。 */
  width?: string
}

/** 资源元数据——对应 `GET /api/v1/meta/capabilities` 的 `resources[]`。 */
export interface ResourceMeta {
  name: string
  title: string
  /**
   * 该资源属于哪个业务域（`master_data` / `production` / `warehouse` /
   * `purchase` / `sales` / `cost` / `access` / `audit`）。
   *
   * 服务端一直在发（`Resource.to_meta()` 的 `module`），而本文件漏了这一行
   * —— 于是前端只能拿不到它。导航按域分组需要它，所以补上。
   */
  module?: string
  /**
   * 详情页的**前端路由模板**（服务端 `Resource.to_meta()` 的 `ui_detail_path`）。
   *
   * 缺省时前端跳通用详情页 `/detail/<资源>/<id>`；有值时按它跳（如塘口的
   * `/ponds/{pond_id}` —— 那个页面含两阶段状态变更表单，是列表之外独立的一页）。
   * 路由形状由服务端声明，前端不按资源名写 if。
   */
  ui_detail_path?: string
  /**
   * 域的**展示标签**（服务端 `Resource.to_meta()` 的 `module_title`）。
   *
   * 导航按 `module` 分组后，**组标题用它**，不用 `module`——
   * 否则界面上出现 `MASTER_DATA` 这种机器码（实测过）。
   * 与 `unit_label` / `row_action_labels` 同一条纪律：标签来自服务端。
   */
  module_title?: string
  list_path: string
  columns: ColumnMeta[]
  status_dict: StatusEntry[]
  /**
   * 该资源的列表接口是否支持关键字搜索（`?keyword=…`）。
   *
   * **不是 `?q=`**：参数名由内核的 list 处理器定为 `keyword`
   * （`tools/list_filter_e2e.py` 按这个名字断言"每一条都命中"），
   * 前端照声明发，不自己起名。
   */
  search?: boolean
  /** 该资源声明过的筛选控件。为空表示"这个列表没有筛选条"。 */
  filters?: FilterMeta[]
}
