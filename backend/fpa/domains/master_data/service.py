"""主数据域：塘口、区域、物料、往来单位。

这是**第一个完整的业务域**——表（003 迁移）+ 服务 + 能力声明 + 端到端测试。
它同时是其余域（production / warehouse / purchase / sales / cost）的模板。

## 三个设计要点

**1. `pond_status` 只能经两步审批变更。**
`pond.create` 可给初始值（仅 `build` / `stocked`，继承旧
`master_data_service.py:29` 的 `CREATE_POND_STATUSES`），此外只能走
`pond_status_change.request` → `pond_status_change.verify`。
`pond.update` **拒绝**提交 `pond_status`（它被声明为 readonly）。
理由见 `DECISIONS.md` Q2：塘口状态迁移会解锁/锁死一批业务能力
（`build → stocked` 才能建批次、`farming → rest` 会停掉投喂），是实质性业务迁移。

**2. `status` 与 `pond_status` 是两套状态机。**
前者是记录生命周期（草稿/待核验/已核验/已归档），后者是业务状态。
早期版本也是双字段（`早期版本 master_data_service.py:15` 与 `早期版本 008_master_data.sql:36`）。

**3. 存塘量只读汇总，不在 `ponds` 表冗余。**
registry §2.5 明确删掉了早期版本的 `stock_quantity` 等字段——早期版本自己在
`早期版本 product/production/routes.py:100-101` 的注释里承认那个冗余。
存塘由生产域的事实表汇总，本域不碰。

## 与早期版本的关键差别

* 早期版本靠 `SPECS` 字典把资源名映射到表名与字段集（`早期版本 master_data_store.py`），
  新系统让每个资源成为**显式的服务方法 + 能力声明**。
* 早期版本的字段白名单写在服务类顶部（`MASTER_FIELDS`），新系统从
  `Annotated[T, Field(...)]` 标注**自动收集**——同一份声明同时供给
  校验、前端表单、Agent 工具 schema 与 OpenAPI。
"""

from __future__ import annotations

from typing import Any

from fpa.kernel import capability as cap
from fpa.kernel.audit import AuditEvent
from fpa.kernel.errors import DomainError, ErrorCode, forbidden, not_found, validation
from fpa.kernel.fields import (
    Choice,
    f_int,
    f_num,
    f_ref,
    f_str,
    f_enum,
    text,
)
from fpa.kernel.scope import Scope
from fpa.kernel.uow import UnitOfWork
from fpa.kernel.workflow import (
    RESOURCES,
    FilterKind,
    FilterSpec,
    Resource,
    RowAction,
    State,
    Tone,
    Transition,
    Workflow,
)

# ============================================================================
# 状态机
# ============================================================================

#: 塘口业务状态机。转移表**原样继承**旧 `master_data_service.py:21-24`。
POND_STATUS_WORKFLOW = Workflow(
    resource="pond_status",
    initial="build",
    states=(
        State("build", "待建设", Tone.NEUTRAL, (RowAction.VIEW,)),
        State("stocked", "已放苗", Tone.INFO, (RowAction.VIEW,)),
        State("farming", "养殖中", Tone.SUCCESS, (RowAction.VIEW,)),
        State("rest", "休整", Tone.WARNING, (RowAction.VIEW,)),
        State("clean", "清理中", Tone.INFO, (RowAction.VIEW,)),
        State("rebuild", "改造中", Tone.DANGER, (RowAction.VIEW,)),
    ),
    transitions=(
        Transition("build", "stocked", "pond_status_change.verify"),
        Transition("stocked", "farming", "pond_status_change.verify"),
        Transition("farming", "rest", "pond_status_change.verify"),
        Transition("farming", "clean", "pond_status_change.verify"),
        Transition("rest", "stocked", "pond_status_change.verify"),
        Transition("rest", "rebuild", "pond_status_change.verify"),
        Transition("clean", "rest", "pond_status_change.verify"),
        Transition("clean", "rebuild", "pond_status_change.verify"),
        Transition("rebuild", "build", "pond_status_change.verify"),
    ),
)

#: 记录生命周期状态机（registry §3.1-A）。
RECORD_LIFECYCLE = Workflow(
    resource="record_lifecycle",
    initial="draft",
    states=(
        State("draft", "草稿", Tone.NEUTRAL, (RowAction.VIEW, RowAction.EDIT, RowAction.SUBMIT)),
        State("submitted", "待核验", Tone.WARNING, (RowAction.VIEW, RowAction.VERIFY)),
        State("verified", "已核验", Tone.SUCCESS, (RowAction.VIEW, RowAction.ARCHIVE)),
        State("archived", "已归档", Tone.NEUTRAL, (RowAction.VIEW,), terminal=True),
    ),
    transitions=(
        Transition("draft", "submitted", "*.submit"),
        Transition("submitted", "verified", "*.verify"),
        Transition("submitted", "draft", "*.update"),
        Transition("draft", "archived", "*.archive"),
        Transition("verified", "archived", "*.archive"),
    ),
)

#: **只读资源**的生命周期：状态与文案沿用记录生命周期，但**不声明任何写动作**。
#:
#: 为什么需要它（而不是继续复用 `RECORD_LIFECYCLE`）：`area` / `material` / `partner`
#: 按 registry §1.4 只有 `list` / `get`（外加 `partner.create`，它是页面级动作、
#: 不是行内动作），**没有任何 `update` / `submit` / `verify` / `archive` 能力**。
#: 复用记录生命周期会让这三个资源在每个状态下渲染出 `edit`/`submit`/`verify`/`archive`
#: 四个动作，而前端按动作词解析端点时找不到 —— 回退链最终落到"该资源第一条 `action`
#: 能力"，而这三个资源**一条都没有**。结果就是**点不动的按钮**：这正是
#: `docs/ROW_ACTIONS.md` 开头描述的那类缺陷。
#:
#: 为什么**保留四个状态**而不用"单状态机"：`areas` / `materials` /
#: `business_partners` 的 `status` 列就是 `ENUM('draft','submitted','verified','archived')`
#: （003 迁移），前端的状态字典、列表筛选、`status_label` 派生都依赖这四个码与文案。
#: **状态照旧，动作归零**——两件事本来就不该由同一个声明绑死。
READ_ONLY_LIFECYCLE = Workflow(
    resource="read_only_lifecycle",
    initial="draft",
    states=tuple(
        State(state.code, state.label, state.tone, (RowAction.VIEW,))
        for state in RECORD_LIFECYCLE.states
    ),
    # 没有任何转移：没有能力能推动它。空转移表是**正确**的声明，不是遗漏。
    transitions=(),
)

#: 基地表使用独立状态码（`active` / `disabled` / `archived`），不能复用记录生命周期。
FARM_LIFECYCLE = Workflow(
    resource="farm",
    initial="active",
    states=(
        State("active", "启用", Tone.SUCCESS, (RowAction.VIEW,)),
        State("disabled", "停用", Tone.WARNING, (RowAction.VIEW,)),
        State("archived", "已归档", Tone.NEUTRAL, (RowAction.VIEW,), terminal=True),
    ),
)


#: **可维护的字典资源**生命周期：状态与转移沿用记录生命周期，动作只声明**真的有能力**的那几个。
#:
#: 为什么不能继续用 `READ_ONLY_LIFECYCLE`（t18 起 `area` / `material` 写能力落地）：
#: 它的每个状态只声明 `VIEW`，于是 `_decorate` 算出的 `allowed_actions` 永远是
#: `["view"]` —— **前端不会渲染出编辑/停用按钮**，哪怕能力已经注册、权限已经种进库。
#: 那正好是本项目最怕的形态：后端"有这条能力"、页面上"没有这个按钮"、
#: 而两边都不报错。`docs/ROW_ACTIONS.md` 开头描述的就是这一类。
#:
#: 与 `READ_ONLY_LIFECYCLE` 的分工是**按资源算的，不是按域算的**：
#: `partner` 仍然是只读（t18 只给 `partner.create`，它没有 `update` / `archive`），
#: 所以它继续用 `READ_ONLY_LIFECYCLE`；而 `area` / `material` 有了 update + archive。
#:
#: ## 为什么只给 `draft` 声明 `EDIT`
#:
#: `verified` 的记录不改（与 `StatusAllowsEdit` 的默认可编辑集合逐字一致，
#: §3.1-C）。**核验过的数据只读**是早期版本已有的合规口径（`lifecycle.py:51,76-78`），
#: 本版把它继承下来：能改的只有草稿。
#: 而 `ARCHIVE` 在 `draft` 与 `verified` 两态都给——"停用"是**记录退出业务视图**，
#: 不是"改内容"，核验过的区域同样可能因为业务调整需要停用。
#:
#: ## 为什么没有 `SUBMIT` / `VERIFY`
#:
#: 本版**没有** `area.submit` / `area.verify` 这类能力（registry §1.4）。
#: 声明它们等于在每个状态下渲染出两个点不动的按钮；而"每个状态都必须可达"的
#: 断言也会因此报出"`submitted` 永远进不去"。**状态码保留、动作不声明**——
#: 与 `warehouse_document` 处理 `archived` 的判据同形（见 `domains/warehouse/service.py`）。
EDITABLE_LIFECYCLE = Workflow(
    resource="editable_lifecycle",
    initial="draft",
    states=(
        State(
            "draft", "草稿", Tone.NEUTRAL,
            (RowAction.VIEW, RowAction.EDIT, RowAction.ARCHIVE),
        ),
        # `submitted` 只给 VIEW + ARCHIVE（与 `RECORD_LIFECYCLE` 逐字一致，§3.1-A）。
        # 不给 EDIT：待核验的行正在被别人核验，改它就等于让核验者看的东西变了。
        State("submitted", "待核验", Tone.WARNING, (RowAction.VIEW, RowAction.ARCHIVE)),
        State(
            "verified", "已核验", Tone.SUCCESS,
            (RowAction.VIEW, RowAction.ARCHIVE),
        ),
        State("archived", "已归档", Tone.NEUTRAL, (RowAction.VIEW,), terminal=True),
    ),
    transitions=(
        Transition("draft", "archived", "*.archive"),
        Transition("submitted", "archived", "*.archive"),
        Transition("verified", "archived", "*.archive"),
    ),
)


#: 塘口的完整状态机 = 业务状态（主）+ 记录生命周期。
#:
#: 为什么用一个 Workflow 承载两种状态：`Resource` 只接受一个 `workflow`，
#: 而 `status_dict` 需要同时给出两套状态的文案与配色。
#: 合并的代价是"同一个 Workflow 里有两组互不相关的状态"，
#: 所以 `available_transitions` 只对 `pond_status` 的子集有意义——
#: 这一点由字段级的 `dynamic_choices` 显式限定，不依赖调用方自觉。
POND_WORKFLOW = Workflow(
    resource="pond",
    initial="draft",
    states=RECORD_LIFECYCLE.states + POND_STATUS_WORKFLOW.states,
    transitions=RECORD_LIFECYCLE.transitions + POND_STATUS_WORKFLOW.transitions,
)


def _pond_status_choices(current: str | None) -> tuple[Choice, ...]:
    """`to_status` 的**动态**候选值。

    这是 registry §2.5 那条要求的落点："服务端按 §3.1-B 转移表过滤 choices"。
    静态声明只能给全集，用户就能选到非法目标——而那本该是服务端保证的事。
    """
    return POND_STATUS_WORKFLOW.transition_choices(current)


# ============================================================================
# 资源声明（前端列表页的列与状态字典都从这里来）
# ============================================================================

# ---------------------------------------------------------------------------
# 跨域具名只读入口（`ROLLOUT_CONTRACT.md` §2.0）
# ---------------------------------------------------------------------------
#
# 实现全在 `lookup.py`（SQL 与承诺的列集集中一处）。这里**只是把名字引出来**：
# §2.0 已落地的入口写作 `production.service.lookup_batch`，所以调用方会按同一形态
# 找 `master_data.service.lookup_*`。引名不产生第二份实现。
#
# 为什么这些入口必须存在：实测有**五个域**在读主数据的四张表（production / purchase /
# sales / warehouse / cost），而 §2.0 明令"调用方不得直读"。表所有者改一个列名时，
# 坏处应当落在**本域的测试**上，而不是某天某个调用方的列表页里静默渲染错。
from .lookup import (  # noqa: E402
    lookup_area,
    lookup_areas,
    lookup_material,
    lookup_materials,
    lookup_partner,
    lookup_partners,
    lookup_pond,
    lookup_ponds,
)

__all__ = [
    "EDITABLE_LIFECYCLE",
    "FARM_LIFECYCLE",
    "POND_STATUS_WORKFLOW",
    "POND_WORKFLOW",
    "READ_ONLY_LIFECYCLE",
    "RECORD_LIFECYCLE",
    "lookup_area",
    "lookup_areas",
    "lookup_material",
    "lookup_materials",
    "lookup_partner",
    "lookup_partners",
    "lookup_pond",
    "lookup_ponds",
]


RESOURCES.register(
    Resource(
        name="farm",
        title="基地",
        module="master_data",
        list_path="/api/v1/farms",
        detail_path="/api/v1/farms/{farm_id}",
        table="farms",
        workflow=FARM_LIFECYCLE,
        columns=(
            ("code", "基地编号"),
            ("name", "基地名称"),
            ("status", "状态码"),
            ("status_label", "状态"),
        ),
        search=True,
        filters=(FilterSpec("status", "状态", FilterKind.STATUS),),
    )
)

RESOURCES.register(
    Resource(
        name="pond",
        title="塘口",
        module="master_data",
        list_path="/api/v1/ponds",
        detail_path="/api/v1/ponds/{pond_id}",
        # 塘口详情有**独立页面**（`/ponds/:id`，含两阶段状态变更表单）。列表行「查看」
        # 原先跳通用详情页 `/detail/pond/<id>`，于是那个页面事实上没有入口 —— 只能手打地址。
        ui_detail_path="/ponds/{pond_id}",
        # ★ `table=` 必须显式给：兜底规则是 `<module>_<name>s`，本域会算出
        #   `master_data_ponds` / `master_data_areas` / …，与真实表名完全不同。
        #   它喂给执行器的不变量上下文（`_resource_table`），决定 `OptimisticLock`
        #   与 `StateTransition(machine="*")` 查哪张表 —— 算错会让这些规则报"表不存在"
        #   而不是静默放行，但错误位置离声明处很远（cost 域实测踩过一次）。
        # ★ 服务支持 keyword 模糊搜索（`ponds.py` 的 `keyword` 分支），但资源声明
        #   没开 `search` —— 后果是**人能用、Agent 不能用**：
        #   `GET /api/v1/ponds?keyword=…` 正常返回，Agent 的 `pond_list` 却被
        #   `gateway._invocation_params` 判为「查询参数未在资源声明中开放」。
        #   同一件事两处描述，其中一处漏了。
        search=True,
        table="ponds",
        workflow=POND_WORKFLOW,
        columns=(
            ("code", "塘口编号"),
            ("name", "塘口名称"),
            ("area_name", "所属区域"),
            ("species", "主养品种"),
            ("capacity_mu", "面积（亩）"),
            ("pond_status_label", "塘口状态"),
            ("status_label", "记录状态"),
        ),
    )
)

RESOURCES.register(
    Resource(
        name="area",
        title="区域",
        module="master_data",
        list_path="/api/v1/areas",
        # t18：`area.get` 的路径是 `/api/v1/areas/{area_id}`，行内"查看"要按资源的
        # `detail_path` 定位详情端点。声明它而不是让前端拼地址——拼地址就是
        # "两处描述同一件事"（并且 `area` 的复数恰好规则，拼对了反而掩盖问题）。
        detail_path="/api/v1/areas/{area_id}",
        table="areas",
        # t18：从只读升级为"可维护"。见 `EDITABLE_LIFECYCLE` 的说明。
        workflow=EDITABLE_LIFECYCLE,
        columns=(
            ("code", "区域编号"),
            ("name", "区域名称"),
            # `status` 与 `status_label` **两个都要**：
            #   * `status` 是筛选控件的取值列（`Resource.__post_init__` 强制
            #     `FilterSpec.key ∈ columns`），也是"按状态筛"的落点；
            #   * `status_label` 是给人看的展示列（`tone_key` 靠它挂状态胶囊）。
            # 只给后者会让"停用后还能把它翻出来复核"表达不出来。
            # ★ `status` 是**机器码**（`verified` / `draft`…），必须与 `status_label`
            #   用**不同标签**：同名的两列在界面上无法区分 —— 实测物料详情会渲染出
            #   两行「记录状态」，一行中文（已核验）一行英文（verified），用户读作
            #   "中英文混杂"。命名沿用全系统约定（其余 10 个资源）：`状态码` / `状态`。
            ("status", "状态码"),
            ("status_label", "记录状态"),
        ),
        search=True,
        filters=(FilterSpec("status", "记录状态", kind=str(FilterKind.STATUS)),),
    )
)

RESOURCES.register(
    Resource(
        name="material",
        title="物料",
        module="master_data",
        list_path="/api/v1/materials",
        detail_path="/api/v1/materials/{material_id}",
        table="materials",
        # t18：从只读升级为"可维护"（`docs/DECISIONS.md` Q21 是推翻"物料只读"那次裁决的落点）。
        workflow=RECORD_LIFECYCLE,
        columns=(
            ("code", "物料编号"),
            ("name", "物料名称"),
            # 同 `unit_label`：列点到**展示标签**，而非机器码 `feed`。
            ("category_label", "分类"),
            # 同 sales_order：列点到展示标签，而非机器码。
            ("unit_label", "单位"),
            ("unit_price", "单价"),
            # 与其余 10 个资源同一约定：机器码列叫「状态码」、展示列叫「记录状态」。
            # `status` 是筛选控件的取值列（`Resource.__post_init__` 强制
            # `FilterSpec.key ∈ columns`），`status_label` 才是给人看的中文。
            # 同名会让两列在界面上无法区分 —— 实测物料详情曾渲染出两行
            # 「记录状态」，一行中文（已核验）一行英文（verified）。
            ("status", "状态码"),
            ("status_label", "记录状态"),
        ),
        search=True,
        filters=(FilterSpec("status", "记录状态", kind=str(FilterKind.STATUS)),),
    )
)

RESOURCES.register(
    Resource(
        name="partner",
        title="往来单位",
        module="master_data",
        list_path="/api/v1/partners",
        table="business_partners",
        workflow=READ_ONLY_LIFECYCLE,
        columns=(
            ("code", "单位编号"),
            ("name", "单位名称"),
            ("partner_type_label", "类型"),
            ("contact_name", "联系人"),
            ("phone", "联系电话"),
        ),
    )
)
