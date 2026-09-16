"""状态机与资源声明的单一来源。

为什么需要这个模块：`INTERFACES.md` 承诺了 `status_dict` 与 `row_actions`，但内核里
**没有对应的声明形态**——前端只能等后端硬编码，而后端要写在哪儿也没定。早期版本正是在
这里分叉的：状态在 DB ENUM、`lifecycle.POLICIES`、各域常量**三处定义**，且 POLICIES
缺 `in_transit` 与 `corrected` 两个状态。

新系统的做法：一个资源的状态机声明一次，派生出
    ① 前端状态字典（状态码 → 中文 + tone）  ② 每个状态允许的 row_actions
    ③ 合法转移（并据此校验）                ④ Agent 的可用动作提示

关于 `Tone`：只用 5 个值。早期版本用**中文标签当色表键**，跨文件复用后静默全灰且测试
抓不到（`早期版本 returnModel.ts:43`）。这里把它做成枚举，前端 tone 表的键只能是这 5 个之一，
未知状态必须显式降级成 `neutral` 而**不允许是空串**。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from enum import StrEnum
from typing import Any

from yuxin.kernel.errors import DomainError, ErrorCode, validation


class Tone(StrEnum):
    """状态标签的配色语义。**只有这 5 个值。**

    刻意不叫 red/green——那样会把视觉决定塞进业务语义里。前端把 5 个语义值映射到色板，
    换主题时不需要改业务代码。
    """

    NEUTRAL = "neutral"
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"


class RowAction(StrEnum):
    """一行数据上可以执行的动作。

    与 `Capability.kind` 的关系：`view`/`edit`/`delete` 对应同名 kind，
    `submit`/`verify`/`archive`/`cancel`/`correct` 等对应 `kind="action"`。
    前端只渲染服务端给的 `allowed_actions`（早期版本的这个设计是对的，继承）。

    **词汇表的权威来源是 `docs/CAPABILITY_REGISTRY.md` §3.0 规则 3**：它把 15 个动作词
    固定下来（继承旧前端 `frontend/src/layers/common/api/lifecycle.models.ts:1` 已固化的
    那一份），并声明本版**实际使用 7 个**：
    `view | edit | submit | approve | verify | cancel | archive`。

    这里保留 `delete` / `correct` / `confirm` / `close` 四个**本版未使用**的值：它们是
    同一份旧词汇表里的词，删掉会让"本版实际用了哪 7 个"失去对照物。这与 registry
    §1.11.1 对 `closed` 状态的处置同理——**显式保留一个已知不用的值，比让它悄悄消失
    更有信息量**。
    """

    VIEW = "view"
    EDIT = "edit"
    DELETE = "delete"
    SUBMIT = "submit"
    #: 审批。`submitted → approved` 的动作词。
    #:
    #: registry §3.0 规则 3 的「本版实际使用 7 个」清单里**有它**；§3.4 与 §3.5 的
    #: `submitted` 行也逐行写了 `view, approve, cancel`；前端 `RecordActions.vue:34`
    #: 同样登记了 `approve: '审批'`。它是那 7 个之一，不是新增设计。
    #:
    #: **为什么必须存在，而不是让域改用 `VERIFY`**：前端按动作词拼能力名找端点
    #: （`frontend/src/layers/common/ui/ResourceListPage.vue:179-182`）——
    #: `capabilityByName("sales_order.verify")` 找不到之后会**回退到
    #: "该资源第一条 `kind === 'action'` 的能力"**。`sales_order` 的 action 能力是
    #: submit / approve / cancel，于是「审批」按钮可能打到 `/cancel`
    #: （`confirmation=always` 的高危操作）的端点上。
    #: 一个动作词缺失的后果不是文案不好看，是**点错按钮**。
    APPROVE = "approve"
    VERIFY = "verify"
    CORRECT = "correct"
    ARCHIVE = "archive"
    CANCEL = "cancel"
    CONFIRM = "confirm"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class State:
    """一个状态：码、中文标签、配色、允许的动作。"""

    code: str
    label: str
    tone: Tone = Tone.NEUTRAL
    actions: tuple[RowAction, ...] = ()
    #: 是否终态（进入后不能再转移出去）。
    terminal: bool = False
    #: 是否是**预留状态**：刻意保留在枚举里，但**没有任何能力能进入它**。
    #:
    #: registry §3.6 的可达性检查结论原文："订单类 `closed` 是**唯一一处显式标注
    #: '预留、无触发能力'**的状态（§3.4），已在表中声明而非隐藏。"
    #:
    #: ## 为什么必须是字段，而不能只是一句注释
    #:
    #: 有一条架构断言在守"每个状态都必须可达"（`docs/CAPABILITY_REGISTRY.md` §3.6；
    #: `DECISIONS.md` Q10 原文："一个**永远不可达的能力**比一个缺失的能力更糟，
    #: 因为它在清单上看起来是有的"）。那条断言必须能区分两种情况：
    #:
    #:   * **忘了接转移** —— 缺陷，必须红（Q10 检出的 `batch.close` 就是这种）；
    #:   * **刻意预留** —— 合法，必须放行（订单类的 `closed` 是这种）。
    #:
    #: 如果这个区别只写在注释里，断言就只能去**猜**（"状态名叫 closed 就放行"），
    #: 而猜出来的例外会在下一个人改名时静默失效——那正是本仓反复出现的
    #: "门禁假通过"形态。**例外必须是声明的，不能是推断的。**
    reserved: bool = False

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("状态码不能为空")
        if not self.label:
            # 中文标签是**前端唯一的状态文案来源**。缺标签会让页面上出现裸状态码。
            raise ValueError(f"状态 {self.code} 缺少中文标签")
        if self.reserved and self.actions:
            # "没有任何能力能进入、却挂着动作"是自相矛盾的：那些动作永远渲染不出来
            # （行永远不会处于该状态）。在构造期挡住，比等到前端渲染出一个
            # 永远不出现的按钮再排查便宜。
            raise ValueError(
                f"状态 {self.code} 标记为 reserved，因此不该声明任何动作"
                f"（当前声明了 {[str(item) for item in self.actions]}）。"
                "如果它确实可达，请去掉 reserved 并补上转移。"
            )

    @property
    def selectable(self) -> bool:
        """该状态是否应当出现在**用户可选的列表**里（筛选下拉、目标状态下拉）。

        ## 为什么不直接用 `reserved`

        `terminal` 与 `reserved` 回答的是两个不同的问题，而"能不能选"是它们的**并集**：

            terminal  进入后不能转移出去（`require_transition` 会拒），
                      所以"选它当目标状态"永远得不到后续操作；
            reserved  刻意保留在枚举里、但**没有任何能力能进入它**（见该字段的说明），
                      所以"选它当筛选条件"只会得到空结果。

        两者都让"这条选项选出来没用"，而这条判据只该有**一处**实现。

        ## 为什么是服务的属性而不是前端各自 `if`

        `to_meta()` 会把它下发给前端，于是前端只需读一个布尔值。
        让前端自己写 `!entry.reserved && !entry.terminal` 等于把判据复制到
        每一个渲染下拉的地方——本项目已经因为"同一规则多处实现"吃过足够多的亏。
        """
        return not self.terminal and not self.reserved

    def to_meta(self) -> dict[str, Any]:
        return {
            "value": self.code,
            "label": self.label,
            "tone": str(self.tone),
            "actions": [str(action) for action in self.actions],
            "terminal": self.terminal,
            "reserved": self.reserved,
            # ★ 用户可选列表的**唯一判据**。前端渲染下拉时跳过 `selectable=False`
            #   的项；渲染行状态标签时**必须保持用全部项**（否则历史数据里的
            #   `archived` 行会退化成裸状态码）。
            "selectable": self.selectable,
        }


@dataclass(frozen=True, slots=True)
class Transition:
    """一次合法转移。``action`` 是触发它的能力名（如 `pond.verify`）。"""

    from_state: str
    to_state: str
    action: str

    def to_meta(self) -> dict[str, str]:
        return {"from": self.from_state, "to": self.to_state, "action": self.action}


@dataclass(frozen=True, slots=True)
class Workflow:
    """一个资源的状态机。"""

    resource: str
    states: tuple[State, ...]
    transitions: tuple[Transition, ...] = ()
    initial: str = ""

    def __post_init__(self) -> None:
        codes = [state.code for state in self.states]
        if len(set(codes)) != len(codes):
            raise ValueError(f"{self.resource} 的状态码重复")
        if not codes:
            raise ValueError(f"{self.resource} 没有声明任何状态")
        if not self.initial:
            object.__setattr__(self, "initial", codes[0])
        if self.initial not in codes:
            raise ValueError(f"{self.resource} 的初始状态 {self.initial} 不在状态列表里")
        for transition in self.transitions:
            for code in (transition.from_state, transition.to_state):
                if code not in codes:
                    raise ValueError(
                        f"{self.resource} 的转移引用了未声明的状态 {code}"
                    )

    # -- 查询 ----------------------------------------------------------------

    def state(self, code: str) -> State:
        for item in self.states:
            if item.code == code:
                return item
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            f"{self.resource} 不存在状态 {code}",
            data={"field": "status", "allowed": [s.code for s in self.states]},
        )

    def allowed_actions(self, code: str) -> tuple[RowAction, ...]:
        if not code:
            return ()
        try:
            return self.state(code).actions
        except DomainError:
            # 未知状态：返回空动作而不是抛错。列表页渲染一行的动作不该因为
            # 一个脏状态码而整页失败——但**必须记日志**，否则就是静默降级。
            return ()

    def can(self, from_state: str, to_state: str) -> bool:
        return any(
            item.from_state == from_state and item.to_state == to_state
            for item in self.transitions
        )

    def require_transition(self, from_state: str, to_state: str, *, action: str) -> None:
        """校验一次状态转移合法。非法时抛错并**列出合法目标**。"""
        if from_state == to_state:
            return
        if self.can(from_state, to_state):
            return
        allowed = [
            item.to_state for item in self.transitions if item.from_state == from_state
        ]
        current = self.state(from_state)
        if current.terminal:
            raise DomainError(
                ErrorCode.CONFLICT,
                f"{current.label}的记录不能再变更状态",
            )
        if not allowed:
            raise DomainError(
                ErrorCode.CONFLICT,
                f"{current.label}不能直接变更状态",
                data={"from": from_state, "action": action},
            )
        labels = [self.state(code).label for code in allowed]
        raise DomainError(
            ErrorCode.CONFLICT,
            f"{current.label}只能变更为：{'、'.join(labels)}",
            data={"from": from_state, "allowed": allowed, "action": action},
        )

    def require_action(self, code: str, action: RowAction) -> None:
        """校验某状态下允许某动作。"""
        state = self.state(code)
        if action not in state.actions:
            raise DomainError(
                ErrorCode.CONFLICT,
                f"{state.label}的记录不支持「{action}」操作",
                data={"status": code, "action": str(action)},
            )

    def available_transitions(self, from_state: str | None) -> tuple[State, ...]:
        """从 ``from_state`` 出发的**合法目标状态**。

        ``from_state`` 为空时返回除初始态外的全部状态——用于"新建"场景
        （那时没有当前状态，需要给一个可选的初始集合）。

        这个方法存在的理由：合法目标取决于**该行当前的状态**，而能力声明是静态的。
        少了它，静态声明只能给全集，用户就能选到非法目标。
        """
        if from_state is None:
            return tuple(state for state in self.states if not state.terminal)
        try:
            current = self.state(from_state)
        except DomainError:
            # 未知状态（脏数据）：返回全集而不是空集。
            # 返回空集会让前端渲染一个没有选项的下拉，用户以为"不能改"——
            # 而真实原因是数据脏。让它给全集，由服务端的转移校验兜底。
            return tuple(state for state in self.states if not state.terminal)
        if current.terminal:
            # 终态没有可去之处，这个返回值是**准确的**
            return ()
        allowed = {
            item.to_state for item in self.transitions if item.from_state == from_state
        }
        return tuple(state for state in self.states if state.code in allowed)

    def transition_choices(self, from_state: str | None) -> tuple[Choice, ...]:
        """`available_transitions` 的 Choice 形态，供字段声明直接使用。"""
        # Tone 定义在本模块里（不在 fields.py）——初版误从 fields 导入，
        # 于是这个函数一调就 ImportError。**只有真正调用过的路径才会暴露这种错。**
        from .fields import Choice

        return tuple(
            Choice(value=state.code, label=state.label) for state in self.available_transitions(from_state)
        )

    # -- 派生：前端元数据 -----------------------------------------------------

    def selectable_states(self) -> tuple[State, ...]:
        """用户可选的状态（`terminal` / `reserved` 之外）。

        用途是**筛选白名单**：列表接口按 `status` 过滤时，允许的取值应当与下拉里
        能选到的完全一致。若白名单比下拉宽，客户端可以问出一个"界面上选不出"
        的过滤条件（那种请求永远返回空集，而调用方会以为数据没了）；
        若比下拉窄，用户选了一个合法项却被 422 拒。两者都是"两处描述同一件事"。

        `to_status_dict()` 与之的分工：那个给**全部**状态（渲染历史行标签要用），
        这个给**可选**集合。判据都落在 `State.selectable` 一处。
        """
        return tuple(state for state in self.states if state.selectable)

    def to_status_dict(self) -> list[dict[str, Any]]:
        return [state.to_meta() for state in self.states]

    def to_meta(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "initial": self.initial,
            "status_dict": self.to_status_dict(),
            "transitions": [item.to_meta() for item in self.transitions],
        }


# ---------------------------------------------------------------------------
# 注册表：资源的展示元数据
#
# 与 `Capability` 的关系：能力管"能做什么"，资源管"长什么样"。
# 前端的列表列、状态字典、下拉选项都从这里来——`INTERFACES.md` §2 的
# `resources` 段就是这个对象序列化后的形态。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 列表筛选条件的声明
#
# 与 `columns` 是同一个问题的两半：`columns` 回答"这个列表展示什么"，
# `filters` 回答"这个列表能按什么找"。两者都**属于资源**，因此落点都在本类。
#
# 为什么它必须由服务端下发：前端不许硬编码"哪个资源有搜索框"。早期版本 14 个列表页
# 各自手写查询条件，与后端 `params.get(...)` 早已漂移——那是同一条"两处描述同一件事"
# 的旧账。声明在这里，前端只**渲染**，它连参数名都没有机会写错。
#
# 为什么声明必须与处理器**真正接受**的查询参数一一对应（本文件最要紧的一条）：
# 声明一个后端不认的参数 ⇒ 前端渲染出一个永远不生效的控件。用户在筛选条上选了
# "区域"，请求发出去了、后端 200 回来了，而结果是**全表**——
# 这正是本项目明令禁止的静默失败，比"少一个控件"危险得多。
# 所以每个资源的筛选声明都必须逐个从它的 list 处理器的 `params.get("…")` 反查。
# ---------------------------------------------------------------------------


class FilterKind(StrEnum):
    """筛选控件的类型。

    `INTERFACES.md` §2 冻结了 `status | enum | ref | date | boolean | string`
    六个值，本类**多一个 `date_range`**。这不是扩充语义，而是消歧：

      * `date` 与 `date_range` 都要求"填一个日期"，但前者是一个查询参数、
        后者是**两个**（`sold_from` / `sold_to`）。两者塞进同一个值，前端就
        没法从声明里知道该拼几个参数，只能去猜参数名——而猜错**不报错**，
        症状是"填了日期却筛不掉任何行"。
      * 本仓有三处真实的两端区间（`sales_order` 的 `sold_*` / `due_*`、
        `purchase_order` 的 `expected_*`、`cost_entry` 的 `occurred_*`），
        因此它不是为假想的用例扩的口子。

    加这个值时 `date` 在本仓**当前没有使用者**：那两类控件对前端都是"日期输入框 +
    是否区间由 `params` 决定"，再拆一个同形的值是给同一件事加第二个名字。
    真出现"单个日期比较"的参数时再加回来，而不是先留着。
    """

    #: 状态。**候选值不下发**：前端拿该资源的 `status_dict` 渲染，
    #: 状态的唯一来源是状态机（`State.label`），不许在这里复制一份中文。
    STATUS = "status"
    #: 枚举：候选值随声明下发（`choices`）。
    ENUM = "enum"
    #: 外键引用：下拉选项来自另一个资源的列表接口（`ref`）。
    REF = "ref"
    #: 日期**区间**：一个控件、两个查询参数（`params` 给出两个参数名）。
    DATE_RANGE = "date_range"
    #: 布尔：勾上就多一个 WHERE 条件（如"仅看逾期"）。
    BOOLEAN = "boolean"
    #: 字符串（如物料批次号这类"本身就是串"的模糊/等值条件）。
    STRING = "string"


#: 合法的筛选控件类型。构造期用它挡错——写错一个字这里不会静默降级。
FILTER_KINDS: tuple[str, ...] = tuple(str(item) for item in FilterKind)

#: 取值**必须**来自 `Resource.columns` 的那几类（判据见 `FilterSpec`）。
#: 判据是"这个控件是不是要从行里挑一个值出来"：
#:   * `status` / `enum` —— 是（候选集就是某一列的取值集合），列里没有它 ⇒ 空控件；
#:   * `ref` / `date_range` / `boolean` / `string` —— 不是（取值分别来自引用资源的
#:     列表接口、用户输入的日期、勾选框、输入框本身）。
_COLUMN_BACKED_FILTER_KINDS: frozenset[str] = frozenset(
    {
        str(FilterKind.STATUS),
        str(FilterKind.ENUM),
    }
)


@dataclass(frozen=True, slots=True)
class DateRangeParam:
    """`kind="date_range"` 的**两个**查询参数名。

    ## 为什么筛选条上必须把它做成**一个**控件

    `sales_order` 的 `sold_from` / `sold_to`、`purchase_order` 的
    `expected_from` / `expected_to`、`cost_entry` 的 `occurred_from` / `occurred_to`
    在请求上都是两个参数、语义上却是**一个**条件（"这段时间内的"）。

    只声明两个独立的 `date` 控件会有一个不会报错的后果：前端各自生成"起" / "止"，
    而 `x_to` 的取值必须由**声明**给出——写错成 `x_end` 的后端不认，
    于是"填了止日期却筛不掉任何行"。这类错误只在用户抱怨"筛选没用"时暴露。
    把它做成一条声明、同时下发两个参数名，前端就没有第二处可写错。

    ## 为什么必须至少给一个参数名

    两个都不给就不能构成 HTTP 请求（没有参数名可拼）。只给起点或只给终点是合法的
    （单边区间），因此判据是"至少一个"，不是"两个都要"。
    """

    #: 起始参数名（等值或 `>=`）。
    param_from: str = ""
    #: 结束参数名（`<=`）。
    param_to: str = ""

    def __post_init__(self) -> None:
        if not self.param_from and not self.param_to:
            raise ValueError(
                "date_range 必须至少给出 param_from 或 param_to 之一："
                "两个都空就拼不出任何查询参数，控件填了也不会生效。"
            )
        if self.param_from and self.param_from == self.param_to:
            raise ValueError(
                f"date_range 的起止参数名同为 {self.param_from!r}："
                "同一个参数不可能既当 `>=` 又当 `<=`。"
            )

    def names(self) -> tuple[str, ...]:
        return tuple(name for name in (self.param_from, self.param_to) if name)

    def to_meta(self) -> dict[str, str]:
        meta: dict[str, str] = {}
        if self.param_from:
            meta["from"] = self.param_from
        if self.param_to:
            meta["to"] = self.param_to
        return meta


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """列表页筛选条上的**一个**控件声明。

    `key` 是 list 接口真实的**查询参数名**（`?<key>=…`）。这不是描述、是契约：
    它同时回答"控件往哪个参数里填值"，以及（`ref` 之外）"值取自哪一列"。

    ## 为什么 `key` 必须也出现在 `Resource.columns` 里（ref 除外）
    前端渲染一个筛选控件要用**两件事**：参数名（key）与候选值（列）。
    若"参数名"与"取值的列"是两套名字，同一条筛选就有两处真相——早期版本
    `search_columns` / `filter_columns` 并存正是这么漂移的。因此本类强制
    `key ∈ Resource.columns`（构造期检查，不靠约定）。`ref` 是唯一例外：
    `area_id` / `material_id` 这类外键在列里展示的是 `area_name` / `material_name`，
    它的候选值来自**引用资源的列表接口**，所以走 `ref` 而不是列。

    ## 为什么 `choices` / `ref` 是"与类型绑定"而不是两个自由字段
    与 `RefTarget.resource` / `resource_field` 同一口径：
      * `type="enum"` 不给 `choices` ⇒ 前端渲染一个永远选不出东西的空下拉；
      * `type="enum"` 却给 `ref`（或反过来）⇒ "到底信哪个"变成运行时问题。
    两者都在**构造期**报错，而不是等某个用户点开筛选条才发现。
    """

    #: 控件的稳定标识。也是 `Resource.columns` 里的列名（`date_range` 之外）。
    #:
    #: 对**单参数**筛选，它同时就是查询参数名（`?<key>=…`）；
    #: `date_range` 的查询参数名在 `params` 里（因为它有两个）。分开的原因是
    #: `date_range` 没有对应的列 —— 而"取哪一列的值"正是这里在用 `key` 表达的。
    key: str
    #: 控件的中文标签（前端不写映射表）。
    label: str
    kind: str = str(FilterKind.STRING)
    #: `kind="enum"` 的候选值**与中文**（与 `kernel/fields.Choice` 同形）。
    choices: tuple[Any, ...] = ()
    #: `kind="ref"` 的取值来源（`kernel/fields.RefTarget`）。
    ref: Any = None
    #: `kind="date_range"` 的两个参数名（`DateRangeParam`）。
    #:
    #: **只有 date_range 用它**：其余类型的查询参数名就是 `key`。两个都写会让
    #: "参数到底叫什么"有两处答案 —— 与 `RefTarget.resource` / `resource_field`
    #: 同一条纪律。
    params: Any = None

    def __post_init__(self) -> None:
        # 局部 import：`kernel/fields.py` 在**模块级** import 了本模块的 `RESOURCES`
        # （见 `RefTarget.resolved_list_path`），模块级反向 import 会成环。
        from .fields import RefTarget

        if not self.key:
            raise ValueError(f"筛选条件 {self.label!r} 未声明查询参数名（key）")
        if self.kind not in FILTER_KINDS:
            raise ValueError(
                f"筛选条件 {self.key!r} 的类型 {self.kind!r} 不在允许的取值里"
                f"（{' / '.join(FILTER_KINDS)}）—— 前端按这个值选控件，"
                "写错就意味着渲染不出任何控件。"
            )
        if not self.label:
            # 没有标签的控件在筛选条上是一块空白。与 `State.label` 同一条纪律：
            # 给人看的字必须由声明给出，不许前端翻译。
            raise ValueError(f"筛选条件 {self.key!r} 缺少中文标签（label）")
        if self.kind == str(FilterKind.ENUM) and not self.choices:
            raise ValueError(
                f"筛选条件 {self.key!r} 声明为 enum，却没有给 choices："
                "前端会渲染一个永远选不出东西的空下拉。"
            )
        if self.kind != str(FilterKind.ENUM) and self.choices:
            raise ValueError(
                f"筛选条件 {self.key!r} 的类型是 {self.kind!r}，却给了 choices："
                "候选值不会被渲染，等于声明了一处不生效的东西。"
            )
        if self.kind == str(FilterKind.DATE_RANGE):
            if not isinstance(self.params, DateRangeParam):
                raise ValueError(
                    f"筛选条件 {self.key!r} 声明为 date_range，"
                    "必须给出 params=DateRangeParam('…_from', '…_to')："
                    "否则前端不知道两个查询参数各叫什么。"
                )
        elif self.params is not None:
            raise ValueError(
                f"筛选条件 {self.key!r} 的类型是 {self.kind!r}，却给了 params："
                "单参数筛选的查询参数名就是 key，写成两处会让参数名有第二个来源。"
            )
        if self.kind == str(FilterKind.REF):
            if not isinstance(self.ref, RefTarget):
                raise ValueError(
                    f"筛选条件 {self.key!r} 声明为 ref，必须给出 ref=RefTarget(...)"
                    "：否则前端不知道去哪个列表接口拉下拉选项。"
                )
            if self.ref.polymorphic:
                raise ValueError(
                    f"筛选条件 {self.key!r} 的 ref 声明成了多态"
                    f"（resource_field={self.ref.resource_field!r}）："
                    "筛选条上它的兄弟字段不一定同时存在，"
                    "此时既拿不到资源名、也解析不出列表地址。"
                    "多态引用属于**表单字段**的形态（`f_ref_by`），不属于筛选声明。"
                )
        elif self.ref is not None:
            raise ValueError(
                f"筛选条件 {self.key!r} 的类型是 {self.kind!r}，却给了 ref："
                "只有 ref 类型会用到它，声明在这里不会生效。"
            )

    @property
    def is_ref(self) -> bool:
        return self.kind == str(FilterKind.REF)

    def to_meta(self) -> dict[str, Any]:
        """序列化。

        `type` 这个**键名**与 `CapabilityField.to_meta()` 一致（那里也把
        `Field.type` 发成 `type`）：前端两种控件按同一套判别方式阅读。
        """
        meta: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "type": str(self.kind),
        }
        if self.choices:
            meta["choices"] = [
                {"value": item.value, "label": item.label} for item in self.choices
            ]
        if self.ref is not None:
            # `list_path` 由**引用资源的声明**解析（`RefTarget.resolved_list_path`
            # 查 `RESOURCES`），内核不另造一条"猜复数"的推导规则；
            # 解析不出地址时直接抛错，而不是发一个前端会 404 的路径。
            try:
                list_path = self.ref.resolved_list_path()
            except ValueError as error:
                # 把"哪个资源、哪条筛选"补进消息：`resolved_list_path` 只知道资源名，
                # 而调用方需要能直接定位到声明处。
                raise ValueError(
                    f"资源 {self.key!r} 的筛选条件引用了不可解析的资源：{error}"
                ) from error
            meta["ref"] = {
                "resource": self.ref.resource,
                "label_key": self.ref.label_key,
                "list_path": list_path,
            }
        if self.params is not None:
            # 键名 `{"from": …, "to": …}`：前端把它拼成 `?<from>=…&<to>=…`，
            # **不自己推导**参数名（推导必漂移，而错了不报错）。
            meta["params"] = self.params.to_meta()
        return meta

    @property
    def param_names(self) -> tuple[str, ...]:
        """该筛选在请求里真正使用的全部查询参数名。

        探针（`tools/list_filter_e2e.py`）与文档都读这一个属性，
        而不是各自去判断"是 date_range 吗"。判据只有一处实现。
        """
        if self.params is not None:
            return self.params.names()
        return (self.key,)


#: 行内动作词 → 实现它的能力 `kind`。
#:
#: 两个词汇表**不是同一套**：`RowAction` 是给用户看的动作词（`INTERFACES.md` §2），
#: `Capability.kind` 是给系统看的写类型。`edit` 的能力是 `update`，**不是** `edit`。
_ACTION_TO_KIND: dict[str, str] = {
    str(RowAction.VIEW): "read",
    str(RowAction.EDIT): "update",
    str(RowAction.DELETE): "delete",
}


def capability_for_action(resource: str, action: str) -> Any:
    """该资源上**实现某个行内动作的能力**；没有实现就返回 `None`。

    ## 为什么不能只按 `<资源>.<动作>` 找

    能力命名在本仓**不统一**：`area.archive` / `pond.verify` / `purchase_order.submit`
    是"资源.动作"，而 `cost.entry.confirm` 是"域.资源.动作"。
    更关键的是动作词与能力名**不是同一套词汇**：`edit` 的能力叫 `update`。
    按 `<资源>.<动作>` 查 `edit` 会得到 `None`，于是「编辑」按钮永远不渲染
    （实测：master data 的浏览器验收点不到它，超时才暴露）。

    解析顺序：① 同名能力 → ② 同一资源的"以 `.<动作>` 结尾"的能力 →
    ③ 按 `kind`（`edit` → `update`）。
    """
    from yuxin.kernel.capability import REGISTRY  # 局部 import：capability 与本模块互相引用

    named = REGISTRY.find(f"{resource}.{action}")
    if named is not None:
        return named
    suffix = f".{action}"
    for candidate in REGISTRY.all():
        if candidate.resource == resource and candidate.name.endswith(suffix):
            return candidate
    kind = _ACTION_TO_KIND.get(str(action))
    if kind is None:
        return None
    for candidate in REGISTRY.all():
        if candidate.resource == resource and candidate.kind == kind:
            return candidate
    return None


def allowed_actions_for(
    workflow: Any, resource: str, status: str, permissions: frozenset[str]
) -> list[str]:
    """状态机声明的动作 × 当前账号的权限 = **真的能执行**的行内动作。

    ## 为什么必须全仓一处

    这个过滤原先在 7 个域里各写一遍 `f"{资源}.{action}" in permissions`，
    于是**同一个错误在 7 个文件里各错一次**：`edit` 的权限码是 `area.update`，
    拼出来的 `area.edit` 谁都不持有 ⇒ 「编辑」按钮从不渲染，而后端一切正常
    （点不到的按钮不会 403）。判据只能是能力自己的 `required_permission`。

    两条继承下来的语义（都保留）：
      * `view` 永远放行 —— 能看到这一行就说明范围与读权限都过了；
      * 动作若**没有任何能力实现**，跳过它（渲染出来就是点不动的按钮），
        事实由 `tools/registry_reconcile.py` 的 [F] 表暴露；
      * 另接受 `<资源>.<状态>.<动作>` 这种状态细粒度权限码。
    """
    allowed: list[str] = []
    for raw in workflow.allowed_actions(status):
        action = str(raw)
        if action == str(RowAction.VIEW):
            allowed.append(action)
            continue
        capability = capability_for_action(resource, action)
        if capability is None:
            continue
        required = capability.required_permission
        if (
            required is None
            or required in permissions
            or f"{resource}.{status}.{action}" in permissions
        ):
            allowed.append(action)
    return allowed


@dataclass(frozen=True, slots=True)
class Resource:
    """一个业务资源的展示元数据 + 状态机。"""

    name: str
    title: str
    module: str
    list_path: str
    detail_path: str = ""

    #: 详情页的**前端路由模板**（可选）。缺省时前端用通用详情页 `/detail/<资源>/<id>`。
    #:
    #: 为什么需要它：塘口有一个**独立**的详情页 `/ponds/:id`（含两阶段状态变更表单），
    #: 而列表行的「查看」原先硬编码跳通用详情页 —— 于是那个独立页**没有任何入口**
    #: （只能手打地址）。路由形状是"这个资源长什么样"的一部分，属于资源元数据，
    #: 该由服务端声明，而不是让前端按资源名写 if。
    ui_detail_path: str = ""
    workflow: Workflow | None = None
    #: 列表页展示的列；`key` 必须能在列表响应里找到。
    columns: tuple[tuple[str, str], ...] = ()

    #: 该资源的列表接口是否支持 `?keyword=` 模糊搜索（匹配编码 / 名称）。
    #:
    #: 默认 `False`：**不声明就不渲染搜索框**。声明了而处理器不认，前端就会
    #: 渲染一个把用户输入吞掉的搜索框——那是静默失败，不是"功能少一点"。
    #: 逐资源核对方法见 `FilterSpec` 的模块级说明。
    search: bool = False

    #: 列表页筛选条上的控件声明。**空元组 = 不渲染任何控件。**
    #:
    #: 与 `columns` 并列而不是放进 `Capability`：筛选是"这个列表长什么样"的另一半，
    #: 而 `columns` 已经是那个问题的权威落点。放进能力里会让同一条筛选在
    #: "read 能力"与"资源"之间产生第二处真相（列表接口只有一个，能力可能有多条）。
    filters: tuple[FilterSpec, ...] = ()

    #: 表名必须**显式声明**（当前 16 处 Resource 全部显式）。
    #:
    #: 曾经有个兜底：省略时按 `<module>_<name>s` 猜一个（如 `cost` + `cost_entry` -> `cost_cost_entrys`）。
    #: 它的问题不是"猜错名字"，而是**猜错了不报错**——只有真正用到表名的规则（`OptimisticLock` /
    #: `UniqueCode` / `StateTransition`）才会在距离声明处很远的地方撞上"表不存在"。
    #:
    #: 按本项目已确立的口径：**兜底不是契约，就不该有任何一处依赖它**。所以现在缺表名 →
    #: 构造期直接抛错。这与 `Capability.loader=` 的三态契约同形：能猜错的地方，不许它猜。
    table: str = ""
    #: 主键列名。本项目统一用 `id`；显式声明是为了让非自增主键的表也能用。
    key_column: str = "id"

    #: 状态机里声明了某个动作、但**有意不配对应能力**时的说明（动作名 -> 理由）。
    #:
    #: 为什么需要它：`row_actions` 是"该状态允许什么动作"的声明，而"系统实现了什么动作"
    #: 在 `REGISTRY` 里。两者不一致时前端会渲染出点不动的按钮（点击 404），所以
    #: `tools/registry_reconcile.py` 的 [F] 表会把它们全部报出来。
    #: 但有一类不一致是**对的**（例如"点编辑进新建页"、"审批动作在能力里叫 approve"）。
    #: 这类必须能把结论**写回声明处**——否则下次跑工具还会报同一条，久而久之没人看。
    #: 空字典是理想状态：每加一条都要写下为什么。
    row_action_notes: dict[str, str] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.table:
            raise ValueError(
                f"资源 {self.name!r} 未声明 table：表名必须显式声明。"
                "兜底公式（<module>_<name>s）会猜错且不报错，已被移除；"
                "请写 table=<真实表名>（见该资源的迁移文件）。"
            )

        for spec in self.filters:
            # 同一条筛选声明两次 ⇒ 请求里同一个参数被写两次，前端的取值、
            # 后端的生效项都变成"看谁后写"。
            if sum(1 for item in self.filters if item.key == spec.key) > 1:
                raise ValueError(
                    f"资源 {self.name!r} 重复声明了筛选条件 {spec.key!r}"
                )
            if spec.kind not in _COLUMN_BACKED_FILTER_KINDS:
                # 见 `FilterSpec` 的说明：这几类没有"取哪一列的值"这回事。
                #   ref        候选值来自**引用资源的列表接口**（列里展示 `…_name`）；
                #   date_range 取值由用户输入日期（列是 `date` 型，不是候选集）；
                #   boolean    取值由勾选框给（`overdue` 是**服务端的业务口径**：
                #               `due_date < CURDATE() AND status IN ('unpaid','partial')`，
                #               没有"哪一列等于它"这回事）；
                #   string     输入框自己拿值（`lot_no` / `source_ref` 这类"本身就是串"
                #               的条件，数据里本来也不保证有样本）。
                # 换句话说：**只有"取值必须从行里挑一个"的那三类才受此约束** ——
                # 那正是这条检查能真正防住的那类失效（控件渲染出来但选不出东西）。
                continue
            # 其余类型：`key` 同时承载查询参数名与取值列名。
            # 列里没有它 ⇒ 候选值无从取得，前端只能渲染一个空控件。
            if spec.key not in {key for key, _ in self.columns}:
                raise ValueError(
                    f"资源 {self.name!r} 的筛选条件 {spec.key!r} 不在 columns 里："
                    "筛选控件的候选值取自列表列（`Resource.columns` 是它的唯一来源）。"
                    "外键请声明成 FilterKind.REF（候选值来自引用资源的列表接口）。"
                )

    def _column_meta(self, key: str, label: str) -> dict[str, Any]:
        """列元数据，并在**状态列**上补 `tone_key`。

        ## 为什么需要它（一处真实的"声明了但不生效"）
        前端 `DataTable` 一直支持 `tone_key`：拿它去 `row[tone_key]` 取状态码、
        再查 `status_dict` 得 `tone`，据此渲染**带底色的状态胶囊**。
        但后端**从来不输出 `tone_key`**（这里原先只发 `{"key","label"}`），
        于是那套胶囊在真数据上是**死代码** —— 它只在手写的冒烟测试夹具里"工作"过，
        所以测试全绿，而页面上状态是**没有底色、没有圆点的纯文字**。

        ## 判据必须是结构性的，不能按值猜
        约定（本仓已有、且是唯一的）：
          * 状态码列名以 `status` 结尾 —— `status` / `pond_status` / `batch_status` …；
          * 它的派生展示列叫 `<状态码列>_label`，如 `status_label` / `pond_status_label`。
        这两个条件都来自**声明本身**，不依赖行数据。
        **`unit_label` 之类不会被误伤**：`unit` 不以 `status` 结尾。

        ## 万一将来这条约定被误用，退化方向是安全的
        前端 `toneForCell` 对"查不到的状态码"返回 `neutral`（已有测试钉住），
        所以最坏情况是"没有颜色"，而不是"颜色错"。
        """
        column: dict[str, Any] = {"key": key, "label": label}
        if self.workflow is not None and key.endswith("_label"):
            base = key[: -len("_label")]
            if base.endswith("status"):
                column["tone_key"] = base
        return column

    def _filters_meta(self) -> list[dict[str, Any]]:
        """筛选条声明。**无条件下发**（空列表 = 该资源没有筛选条）。"""
        return [spec.to_meta() for spec in self.filters]

    def to_meta(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "name": self.name,
            "title": self.title,
            "module": self.module,
            # 域的**展示标签**。前端按 `module` 分组时用它做组标题，
            # 而不是直接渲染 `module`（否则界面上会出现 `MASTER_DATA`）。
            "module_title": module_label(self.module),
            "list_path": self.list_path,
            "columns": [self._column_meta(key, label) for key, label in self.columns],
            # 这两个键**无条件出现**（不声明时是 `false` / `[]`），理由与
            # `Capability` 无条件发 `fields` 相同：前端"缺这个键"与
            # "后端说不需要控件"必须是两件可分辨的事。省略键会让前端去猜，
            # 于是"后端漏发"退化成"页面少一个搜索框"——正是本文件反对的静默。
            "search": self.search,
            "filters": self._filters_meta(),
        }
        if self.detail_path:
            meta["detail_path"] = self.detail_path
        if self.ui_detail_path:
            meta["ui_detail_path"] = self.ui_detail_path
        if self.workflow is not None:
            meta["status_dict"] = self.workflow.to_status_dict()
            meta["transitions"] = [item.to_meta() for item in self.workflow.transitions]
            meta["initial_status"] = self.workflow.initial
        return meta


class ResourceRegistry:
    """资源注册表。与 `Registry`（能力）成对使用。"""

    def __init__(self) -> None:
        self._resources: dict[str, Resource] = {}

    def register(self, resource: Resource) -> Resource:
        if resource.name in self._resources:
            raise ValueError(f"资源 {resource.name} 重复注册")
        self._resources[resource.name] = resource
        return resource

    def get(self, name: str) -> Resource:
        try:
            return self._resources[name]
        except KeyError as exc:
            raise DomainError(
                ErrorCode.NOT_FOUND, f"资源类型 {name} 不存在"
            ) from exc

    def find(self, name: str) -> Resource | None:
        return self._resources.get(name)

    def all(self) -> tuple[Resource, ...]:
        return tuple(self._resources.values())

    def list_path_of(self, name: str) -> str:
        return self.get(name).list_path

    def __contains__(self, name: object) -> bool:
        return name in self._resources

    def __len__(self) -> int:
        return len(self._resources)


#: 进程级资源注册表。
RESOURCES = ResourceRegistry()


def resource(**kwargs: Any) -> Any:
    """把一个 `Resource` 声明登记进 `RESOURCES`。

    用法::

        resource(
            name="pond", title="塘口", module="master_data",
            list_path="/api/v1/ponds", detail_path="/api/v1/ponds/{pond_id}",
            workflow=POND_WORKFLOW,
            columns=(("code","塘口编号"), ("name","塘口名称")),
        )
    """
    spec = Resource(**kwargs)
    return RESOURCES.register(spec)


#: 业务域（`Resource.module`）→ 中文。**全系统唯一一处**。
#:
#: ## 为什么需要它（一次实测缺陷）
#:
#: 导航按 `module` 分组后，组标题直接渲染了机器码，界面上出现了
#: **`ACCESS` / `AUDIT` / `COST` / `MASTER_DATA`** —— 与“操作列显示裸 token”
#: 是**同一个缺陷**：有码、没有给人看的标签。
#: 而且 `MASTER_DATA` 还把内部命名风格（下划线大写）暴露给了用户。
#:
#: 与 `ACTION_LABELS` / `UNIT_LABELS` 同一条纪律：码与它的中文放同一处，
#: 随元数据下发，**前端不写映射表**。
#: 物料分类码 -> 中文。
#:
#: `materials.category` 是**无约束的 `VARCHAR(32)`**，不是 ENUM ——
#: `003_master_data.sql:201` 的原话：「category 用 VARCHAR 而非 ENUM：
#: 饲料/药品/物资的划分会随业务演进」。
#: 因此**未登记的码回退成原词**，与 `unit_label()` / `row_action_label()` 同口径：
#: 显示原码是**可诊断**的，静默换成别的中文会让「服务端加了分类却忘了加标签」
#: 变成看不见的问题。
#:
#: **只登记能核实来源的码**。每条都要写清"从哪核实的"，因为猜错一个码不会报错，
#: 只会在某天显示成原码，而那时没人知道该查哪里。
#:
#: 目前三个码的来源都可复核：
#:
#: | 码 | 中文 | 核实方式 |
#: |---|---|---|
#: | `feed`  | 饲料 | `003_master_data.sql:313-317` 的种子数据（`MAT-001` / `MAT-002`） |
#: | `seed`  | 种苗 | 业务数据里真实存在（虾苗 / 鱼苗），见下 |
#: | `health`| 药品保健 | 同上（维生素预混料 / 光合细菌改良剂） |
#:
#: 复核命令（**新码先跑这个，再决定加不加**）：
#:
#:     SELECT category, COUNT(*) FROM materials GROUP BY category;
#:
#: ## 未登记的码为什么**显示原码**而不是兜底成"其他"
#:
#: 这是刻意的：服务端新增了分类却忘了加标签时，"显示原码"会让缺口**立刻可见**；
#: 而兜底成一个笼统的中文，会把这类漏登记变成看不见的问题。所以本表的作用是
#: **字典**，不是"美化层" —— 数据里出现新分类码时，这里必须同步补一行。
CATEGORY_LABELS: dict[str, str] = {
    "feed": "饲料",
    "seed": "种苗",
    "health": "药品保健",
}


def category_label(code: str) -> str:
    """物料分类码 -> 中文。未登记的码回退成原词（**不猜**）。"""
    return CATEGORY_LABELS.get(str(code).strip(), str(code))


def flag_label(value: object) -> str:
    """布尔/0-1 标志 -> 是/否。

    `warehouses.is_default` 存的是 `TINYINT(1)`，直接渲染会显示 `1`。
    """
    if value is None or value == "":
        return "—"
    return "是" if int(value) else "否"


MODULE_LABELS: dict[str, str] = {
    "access": "账号与权限",
    "audit": "审计",
    "cost": "成本",
    "master_data": "基础数据",
    "production": "养殖生产",
    "purchase": "采购",
    "sales": "销售",
    "warehouse": "仓储",
    "workbench": "工作台",
    "identity": "身份",
    "probe": "探针",
}


def module_label(code: str) -> str:
    """域码 → 中文。未登记的域回退成原词（**不猜**）。

    与 `row_action_label()` / `unit_label()` 同口径：回退原词是**可诊断**的
    （一眼看出哪个域缺标签），而不是静默换成一个中文。
    """
    return MODULE_LABELS.get(str(code), str(code))


#: 动作词 → 中文标签。**全系统唯一一处**（前端不再持有任何动作文案表）。
#:
#: ## 为什么它必须在这里，而不是在 `RecordActions.vue` 里
#:
#: 状态的中文只有一处（`State.label`，经 `status_dict` 下发），所以列表里的状态列一直是中文的；
#: 而动作词原先**没有对应的标签落点** —— 前端只好在 `RecordActions.vue` 里自己写一张
#: `{view:'查看', archive:'归档', ...}` 的表，而 `DataTable.vue` 干脆把 token 直接渲染出来。
#:
#: 于是同一个页面上出现两种口径：状态列是中文（`养殖中`），操作列却是**裸 token**（`view` / `archive`）。
#: 这不是"少翻译两个词"，而是**同一条纪律（标签只有一处）在两个位置被区别对待**。
#:
#: 所以标签落到本模块、与 `RowAction` 同处：动作词与它的中文是同一个事实的两面，分开存放必然漂移。
#:
#: ## 为什么给**全部 15 个**动作都写了标签
#:
#: `RowAction` 刻意保留了 4 个本版未使用的值（见枚举的 docstring）。它们的标签一并给出：
#: 将来某条能力用上它们时，前端不需要任何改动就有中文；
#: **留空等于给下一个用它们的人埋一个裸 token**。
#:
#: 判据与 `State.label` 完全一致：**文案只在这里出现一次，前端只渲染不翻译**。
ACTION_LABELS: dict[str, str] = {
    "view": "查看",
    "edit": "编辑",
    "delete": "删除",
    "submit": "提交",
    "approve": "审批",
    "verify": "核验",
    "correct": "更正",
    "archive": "归档",
    "cancel": "取消",
    "confirm": "确认",
    "close": "关闭",
}

#: 管理页的行级动作不属于状态机词汇，但同样由服务端统一下发标签。
ADMIN_ACTION_LABELS: dict[str, str] = {
    "status": "启用/禁用",
    "grants": "授权",
    "permissions": "权限设置",
}


def all_row_action_labels() -> dict[str, str]:
    """返回生命周期动作与管理动作的统一展示标签。"""
    return {**ACTION_LABELS, **ADMIN_ACTION_LABELS}


def row_action_label(action: str) -> str:
    """动作词 → 中文标签。未登记的动作**回退成原词**（不静默改写成别的文案）。

    回退成原词而不是抛错：动作词来自 `allowed_actions`，而那是服务端算出来的。
    真出现未登记的词，说明服务端加了动作却忘了加标签 —— 那时前端显示原词是
    **可诊断**的（一眼看出是哪个词缺标签），抛错反而会让整张列表打不开。
    但"每个 `RowAction` 都必须有标签"这条必须有断言守着（`tests/test_row_actions.py`）。
    """
    return all_row_action_labels().get(str(action), str(action))


def row_action_options() -> list[dict[str, str]]:
    """全部动作词 + 中文标签，供前端建查表（`/meta/capabilities` 下发）。

    与 `Workflow.to_status_dict()` 同族的形态：**服务端下发标签，前端只渲染**。
    """
    return [
        {"value": str(item), "label": row_action_label(str(item))} for item in RowAction
    ]


__all__ = [
    "ADMIN_ACTION_LABELS",
    "all_row_action_labels",
    "ACTION_LABELS",
    "FILTER_KINDS",
    "DateRangeParam",
    "FilterKind",
    "FilterSpec",

    "MODULE_LABELS",
    "CATEGORY_LABELS",
    "category_label",
    "flag_label",
    "RESOURCES",
    "Resource",
    "ResourceRegistry",
    "RowAction",
    "State",
    "Tone",
    "Transition",
    "Workflow",
    "resource",
    "module_label",
    "row_action_label",
    "row_action_options",
]
