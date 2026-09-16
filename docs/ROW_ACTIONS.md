# `row_actions` 与能力的一致性

> `tools/registry_reconcile.py` 的 **[F]** 表的判定口径、以及"人工判定结论写回哪里"。

## 1. 为什么需要这张表

`Workflow` 里的 `RowAction` 是"**该状态允许什么动作**"的声明，`REGISTRY` 是"**系统实现了什么动作**"。
两者不一致时，前端会渲染出一个点不动的按钮（点击 404），而没有任何一处会报错：

* 前端 `RecordActions.vue` 只认服务端给的 `allowed_actions`，不自己推导（这个设计是对的）；
* 服务端算 `allowed_actions` 用的是 `f"{resource}.{action}"` 组出来的权限码
  （见 `domains/master_data/ponds.py::_decorate`）；
* 于是"声明了动作、却没有同名能力"时，权限码永远匹配不上——**按钮渲染不出来**，
  或者更糟：前端按动作词找不到端点时会**回退到该资源第一条 `kind === 'action'` 的能力**，
  把「审批」打到 `/cancel` 这类高危端点上（见 `workflow.py` 里 `RowAction.APPROVE` 的注释）。

[F] 把这类不一致全部报出来。它**只负责报出可疑处，不负责判死**——因为存在"有意不配能力"的情形。

## 2. 判定口径（工具侧）

对每个**已有能力**的资源：

1. 取状态机里所有动作的集合（`RowAction` 的取值）；
2. 取该资源所有能力的**末端 token**（`cost.entry.confirm` → `confirm`）；
3. 动作 → token 的映射见 `tools/registry_reconcile.py::ROW_ACTION_TOKENS`
   （只有两处不同名：`view → list/get/summary/ledger`、`edit → update`，其余同名）；
4. 匹配不上的动作进 [F]；能匹配上的动作一并打印，便于人工判断是不是命名约定不同。

## 3. 结论写回哪里：`Resource.row_action_notes`

**结论必须写在声明处**，不是写在工具的白名单里——理由跟着数据走，就不会漂移。

```python
RESOURCES.register(
    Resource(
        name="feeding",
        ...
        row_action_notes={
            "edit": "点「编辑」进的是新建页（历史上 create 同时承担编辑），没有独立的 update 能力",
        },
    )
)
```

写进去的动作**不再进 [F] 的报警行**，而是单列在"已在资源声明里写明理由的动作"下面，
仍然可见、可复核。**空字典是理想状态**：每加一条都要写下为什么
（与 `check_source_hygiene.py` 的 `EXEMPT`、内核的 `TYPE_ALIASES` 同一条纪律）。

## 3.1 `row_action_notes` 的**唯一**用途，以及两种误用

它只回答一种情形：**该动作确实由某个能力实现，只是命名不同**——
例如"审批"在内核词汇里叫 `approve`，能力名是 `*.approve`，而状态机历史上写的是
`verify`；或者"点编辑进的是新建页"。此时写一句理由，[F] 不再报警，但**动作有用**。

两种**不许**用它的情况：

1. **动作根本没实现**（没有 submit/verify/archive 能力）。
   记成"有意不一致"会让前端继续渲染一个点不动的按钮，用户点了拿到 404。
   **不渲染 > 渲染出来再失败**（这是本项目对 `allowed_actions` 的一贯口径）。
   正确做法是**从状态机里去掉该动作**，或补上对应能力。
   （`domains/sales/service.py:257-262` 记了这条判断，sales-dev 据此选择不用该字段。）
2. **用它换掉一条本该红的断言**。它只能让 [F] 这一张表不再报这个动作，
   不能让别处的判据变绿；如果某个动作既没能力、又用它压住报警，
   那它就从"可见的缺口"变成"不可见的缺口"——正是本项目最警惕的形态。

**判据一句话**：写进 `row_action_notes` 的动作，必须能回答
"**用户点下去，会不会有事发生？**" 答"不能"（没有能力/端点）就不该写在这里。

## 4. 当前状态（截至本次修订）

| 资源 | 结论 | 依据 |
|---|---|---|
| `sales_order` | 已改为声明 `RowAction.APPROVE`（能力名 `sales_order.approve`） | 能力名即 `*.approve`，权限码也是它 |
| `purchase_order` | **待改**：状态机仍把"待审批 → 已审批"标为 `RowAction.VERIFY`，而能力叫 `purchase_order.approve` | [F] 报出 `verify` 无匹配；`RowAction.APPROVE` 已存在 |
| `cost_entry` | 待定：声明 `archive/edit/submit/verify`，实际只有 `confirm`/`view` 能匹配 | 需 cost 域判断是"还没做"还是"有意不配" |
| `area` / `material` / `partner` | 待定：声明 4 个写动作，但只有读能力 | 这些资源当前是只读，状态机里的动作应删掉或确认后续会做 |
| `feeding` / `harvest` | 待定：声明 `edit`，无 `*.update` 能力 | 需 production 域确认"点编辑进新建页"是否成立 |

**这两列都要能回答一个问题**：是"还没做"（进度），还是"有意不配"（写进 `row_action_notes` + 理由）？
回答不了的动作，就是前端上那个点不动的按钮。

## 5. 与 `RowAction.APPROVE` 的关系

`RowAction` 的取值集合必须**覆盖能力命名里真实存在的动作词**，否则状态机只能拿一个近义词顶上，
而近义词会让"动作词 → 能力名"的查找失败。`APPROVE` 就是这样补进来的：
前端 `RecordActions.vue` 早已登记 `approve: '审批'`，能力名是 `*.approve`，
只有状态机还在用 `verify`。

---

## 6. 动作词的**中文标签**：唯一来源是内核

> 本节由 t7 补入。此前本文档只定义了**动作词的集合**（第 1–5 节），
> 而"每个动作叫什么"这件事**没有任何落点** —— 于是实测出现了一个页面上的两套口径。

### 6.1 缺陷现象与根因

塘口列表页的"操作"列渲染的是**裸 token**：按钮写着 `view` / `archive`，
而同一行的状态列是中文（`养殖中` / `已核验`）。

根因不是"少翻译两个词"，而是**同一条纪律在两个位置被区别对待**：

| | 有落点吗 | 结果 |
|---|---|---|
| 状态 | 有 —— `State.label`，经 `status_dict` 下发 | 中文 |
| 动作 | **没有** | `DataTable.vue` 直接渲染 token；`RecordActions.vue` 自己硬编码一张 16 项的表 |

于是同一个系统里出现了**两套动作口径**（一处渲染 token、一处硬编码翻译），
而那些硬编码还写在注释声称"不在页面里重复"的组件里。

### 6.2 落点（**唯一**）

    backend/yuxin/kernel/workflow.py::ACTION_LABELS    ← 中文标签的唯一定义
                                       row_action_label(action)   ← 单值查表（未登记回退原词）
                                       row_action_options()       ← 取值 + 标签

    → backend/yuxin/web/workflow_meta.py::actions_payload()
    → GET /api/v1/meta/capabilities 的 data.actions.{row_actions, row_action_labels}
    → 前端 frontend/src/layers/common/meta/meta.store.ts::rowActionLabel()
    → DataTable.vue / RecordActions.vue 只渲染，不翻译

**前端禁止再出现任何 `{view:'查看', ...}` 形式的动作映射表。**
判据与状态标签完全一致：`State.label` 只有一处，所以状态一直是中文的；
动作标签也必须是同样的形态。

### 6.3 为什么给**全部**动作词都写标签（含本版未使用的）

`RowAction` 刻意保留了 4 个本版未使用的值（`delete` / `correct` / `confirm` / `close`，
见 `kernel/workflow.py` 枚举的 docstring：保留它们是为了让"本版实际用了哪 7 个"有对照物）。

它们的标签**一并给出**：将来某条能力用上它们时，前端不需要任何改动就有中文。
留空等于给下一个用它们的人埋一个裸 token。

守这条的断言在 `tests/test_row_actions.py`：
`test_every_row_action_has_a_chinese_label`（全覆盖 + 无僵尸词）、
`test_row_action_labels_are_not_empty_and_chinese`（防 `{"view": "view"}` 式形式通过）、
`test_actions_metadata_exposes_labels`（落点确实在元数据里）。
