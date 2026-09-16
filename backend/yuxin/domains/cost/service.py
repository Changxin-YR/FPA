"""成本域：成本归集、汇总、关账。

这是照 master_data 抄的第三个域（DEVELOPMENT.md §3 的固定七步），按 registry §1.9
的 5 条能力实现：

    cost.entry.list     成本记录列表        read   cost.view
    cost.entry.create   登记成本            create cost.manage
    cost.entry.confirm  确认成本            action cost.confirm   risk=high
    cost.summary        成本汇总            read   cost.view
    cost.period.close   关账                action cost.close     risk=high

## 三个设计要点

**1. 成本记录是双状态资源**（与 `ponds` 同构）：

    status        记录生命周期 draft / submitted / verified / archived
    confirm_state 成本归集状态 pending / confirmed

为什么两个都要：生命周期回答"这条记录走完流程了吗"（草稿能不能改、能不能归档），
归集状态回答"这笔钱算进成本了吗"。早期版本把它们压成 `cost_settlements.confirmed`
一个布尔，于是"已归档但未确认"这种合法组合表达不出来，只能靠状态组合去猜。

**2. 关账锁的是整个期间，不只是成本侧。**

`PeriodOpen` 不变量的语义按 `DECISIONS.md` Q16 修正为"**不得在已关闭的会计期间
产生任何影响该期间的账目记录**"——它不只挂在成本域，也挂在 `receipt.verify` /
`issue.verify` / `feeding.verify` / `delivery.verify` 上（后者写的是应收，
即收入侧）。如果关账只挡成本、放行收入，会出现"期间已关闭但收入仍可入账"的
账目失衡，那比漏挡成本侧更严重。

本域是这条不变量的**开启点**（`cost.period.close` 把它变成已关闭）与**数据源**
（`accounting_periods` 表）。

**3. 金额只允许为正，冲销不在这里表达。**

`chk_cost_entries_amount CHECK (amount > 0)`：如果允许负数，"金额"这一个字段就
同时承担"成本"与"冲销"两个语义，汇总时无法区分"这个月少花了钱"与"冲掉了一笔
错账"。要冲销，在**下一期间**记一笔正数的调整记录——这也与关账不可逆
（关掉的期间数字不许变）自洽。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from yuxin.kernel.workflow import allowed_actions_for
from yuxin.kernel.errors import DomainError, ErrorCode
from yuxin.kernel.fields import Choice
from yuxin.kernel.scope import Scope
from yuxin.kernel.workflow import (
    RESOURCES,
    DateRangeParam,
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

#: 记录生命周期。与 master_data 的 `RECORD_LIFECYCLE` **字面同构**：
#: 同一个业务动作（提交/核验/归档）在所有域里应当有同一套状态码与中文标签，
#: 否则前端就要为每个域写一份状态翻译。
#:
#: ## 为什么四个状态里有两个标 `reserved=True`，且 `actions` 只剩一个词
#:
#: registry §1.9 给成本域只定了 **5 条**能力：
#:
#:     cost.entry.list / cost.entry.create / cost.entry.confirm / cost.summary
#:     cost.period.close
#:
#: 也就是说**没有 `cost.entry.update`，也没有 `cost.entry.submit` / `.verify` /
#: `.archive`**。而 `row_actions` 是"该状态允许什么动作"的声明，前端只渲染服务端给的
#: `allowed_actions`（`frontend/.../RecordActions.vue`），于是照 master_data 抄来的
#: `edit / submit / verify / archive` 会渲染成**四个点不动的按钮**：
#: 前端按 `cost_entry.edit` 拼不出能力，回退链最终只找到 `cost.entry.confirm`。
#:
#: 修法有三种，`docs/ROW_ACTIONS.md` §3.1 把界线写死了：
#:
#:   1. **补能力**（动作由某条能力实现，只是命名不同）→ 合规；
#:   2. **从状态机里删掉动作**（动作确实没实现）→ 合规；
#:   3. 写 `Resource.row_action_notes` 把报警压住（而动作根本没实现）→ **不合规**：
#:      用户点下去拿到 404，缺口从"可见"变成"不可见"。
#:
#: 这里只能选 2：补能力意味着给成本域加三条 registry 里没有的能力——那是改权威清单，
#: 不是收口。既有先例也选 2：`domains/sales/service.py` 的 `DOCUMENT_TWO_STATE`
#: 当初正是因为同样的 [F] 报警去掉了 `EDIT` 与 `*.update`。
#:
#: **四个状态码全部保留**，因为 `cost_entries.status` 是 `VARCHAR(32)`（004 迁移），
#: 而 `entries.py` 的列表筛选下拉正是按 `RECORD_LIFECYCLE.states` 生成的。
#: 删掉状态码会让库里已存在的那些行**渲染不出中文标签**（`label_of` 回退成裸状态码）。
#: 于是状态码与动作各自回答一个不同的问题：
#:
#:     状态码  ——  这一列能取什么值（数据侧的事实，含历史数据）
#:     actions ——  用户在这一行上能做什么（能力侧的事实，只有 5 条能力）
#:
#: `reserved=True` 是**机器可读的声明**（不是注释）：它让
#: `tests/test_row_actions.py::test_every_reachable_state_has_an_entry` 能把
#: "刻意保留、尚无能力实现的中间状态"与"忘了接转移"（Q10 检出的 `batch.close`
#: 那种缺陷）区分开。按 负责人 的口径：**门禁的例外必须是声明的，不能是推断的**。
RECORD_LIFECYCLE = Workflow(
    resource="record_lifecycle",
    initial="draft",
    states=(
        State("draft", "草稿", Tone.NEUTRAL, (RowAction.VIEW,)),
        State("submitted", "待核验", Tone.WARNING, (), reserved=True),
        State("verified", "已核验", Tone.SUCCESS, (RowAction.VIEW,)),
        State("archived", "已归档", Tone.NEUTRAL, (), terminal=True, reserved=True),
    ),
    transitions=(
        # `draft -> verified` 由 `cost.entry.confirm` 触发，写的是**具体能力名**
        # 而不是通配 `*.verify`：成本域没有 `cost.entry.verify`，
        # 而 `confirm` 才是它实际的动作词。写通配会让这条转移指向一条不存在的能力
        # ——`tests/test_row_actions.py::test_capability_transitions_reference_known_capabilities`
        # 刻意放行通配（因为它要支持 `RECORD_LIFECYCLE` 这种被多资源复用的模板），
        # 所以这个错**不会**被断言抓住，只能靠这里写对。
        Transition("draft", "verified", "cost.entry.confirm"),
    ),
)

#: 成本归集状态。**独立于记录生命周期**。
#:
#: `pending` 不是"草稿"：一条 `verified` 的成本记录仍可能是 `pending`
#: （已核验但还没确认入账）。这正是不把它做成布尔的原因——
#: 布尔会强迫"核验"与"确认"变成同一个动作。
CONFIRM_STATE_WORKFLOW = Workflow(
    resource="cost_confirm_state",
    initial="pending",
    states=(
        State("pending", "待确认", Tone.WARNING, (RowAction.CONFIRM,)),
        State("confirmed", "已确认", Tone.SUCCESS, ()),
    ),
    transitions=(
        Transition("pending", "confirmed", "cost.entry.confirm"),
    ),
)

#: 成本记录的完整状态机 = 记录生命周期（主）+ 归集状态。
#:
#: 与 ponds 用 `POND_WORKFLOW` 承载两套状态是同一个手法：`Resource` 只接受一个
#: `workflow`，而 `status_dict` 必须同时给出两套状态的文案与配色——
#: 前端只读 `status_dict` 就能渲染两个列，不需要知道"这个资源有两套状态机"。
COST_ENTRY_WORKFLOW = Workflow(
    resource="cost_entry",
    initial="draft",
    states=RECORD_LIFECYCLE.states + CONFIRM_STATE_WORKFLOW.states,
    transitions=RECORD_LIFECYCLE.transitions + CONFIRM_STATE_WORKFLOW.transitions,
)


# ============================================================================
# 业务常量（全部可追溯到 registry）
# ============================================================================

#: 成本类别为**启用**状态才能使用（registry §2.10：`category_code` "须为启用的成本类别"）。
CATEGORY_ENABLED = "enabled"

#: 来源类型。registry §2.10 明确只有两个值：
#:   manual_expense   系统外费用手工登记（人工/水电/租金）—— Q13 推翻"纯派生"的理由
#:   warehouse_ledger 投喂/入库/领用核验时自动归集
#: 旧枚举里的 asset_depreciation / adjustment 随资产与调整单一起砍除。
SOURCE_MANUAL = "manual_expense"
SOURCE_LEDGER = "warehouse_ledger"

#: 来源类型的中文。**只此一处** —— 筛选条的候选值（`Choice`）与列表里派生的
#: `source_type_label` 都从这里取，页面与声明里都不再出现第二份中文。
SOURCE_TYPE_LABELS: dict[str, str] = {
    SOURCE_MANUAL: "手工登记",
    SOURCE_LEDGER: "库存归集",
}

#: 归属对象类型（registry §2.10：farm / area / pond / batch）。
TARGET_TYPES = ("farm", "area", "pond", "batch")

#: 金额上限：DECIMAL(16,2) 的物理上限。声明它让**校验层**先给出可读文案，
#: 而不是让 MySQL 用 1264 报一句"数值超出允许范围"。
MAX_AMOUNT = 99_999_999_999_999.99


# ============================================================================
# 资源声明（前端列表页的列与状态字典都从这里来）
# ============================================================================

RESOURCES.register(
    Resource(
        name="cost_entry",
        title="成本记录",
        module="cost",
        list_path="/api/v1/cost/entries",
        detail_path="/api/v1/cost/entries/{entry_id}",
        workflow=COST_ENTRY_WORKFLOW,
        # ★ `table=` 必须显式给：兜底规则是 `<module>_<name>s`，对资源名 `cost_entry`
        #   会算出 `cost_cost_entrys`（多一层前缀、且复数拼错）。表名与兜底结果不同
        #   的域**必须**显式声明（见 `kernel/workflow.py::Resource.table` 的说明）。
        #
        #   为什么这个错误值得单独标出来：它**不报错**。执行器把表名注入不变量上下文
        #   （`_resource_table`），只有真正用到表名的规则（如 `OptimisticLock`）才会
        #   拿它去查库——那时才会撞上"表不存在"，而错误位置离声明处很远。
        #   实测就是在这里被发现的：`_resource_table='cost_cost_entrys'`。
        table="cost_entries",
        columns=(
            ("occurred_on", "发生日期"),
            # `category_code` 是软外键（`other` / `feed` …），给人看的是读路径
            # JOIN 出来的类别名（`other` → `其他`）。码不能改：`_require_category`
            # 按码校验、`cost.summary` 的 `by_category` 也按码聚合。
            ("category_label", "成本类别"),
            ("amount", "金额"),
            ("source_type", "来源类型码"),
            ("source_type_label", "来源类型"),
            ("source_ref", "来源单号"),
            ("target_label", "归属对象"),
            ("confirm_state_label", "归集状态"),
            # 原始状态码：`status_label` 供人看，`status` 是**筛选控件的取值列**
            # （筛选条的候选项是一个集合，必须能在一行数据里取到值）。
            ("status", "状态码"),
            ("status_label", "记录状态"),
        ),
        # 对应的处理器是 `CostEntryService.list_entries`，它按
        # `entries.py::_filters` 的 `params.get(...)` 组装 WHERE。
        #
        # 为什么 `_filters` 里出现的 `confirm_state` / `target_id` / `period`
        # **没有**出现在声明里 —— 这三条都是"声明了就会变成不生效控件"的地方：
        #
        #   * `confirm_state`：它是另一台状态机（`CONFIRM_STATE_WORKFLOW`）。
        #     `type="status"` 语义是"前端拿**本资源**的 `status_dict` 渲染候选值"，
        #     而 `status_dict` 是 `COST_ENTRY_WORKFLOW`。用它渲染 `confirm_state`
        #     会给出**另一套状态码**，于是筛选条上选什么后端都不认。
        #     收口方向是给 `FilterKind.STATUS` 增加"状态机名"这一维（例如
        #     `workflow="confirm_state"`，并由 `to_meta` 下发该状态机的 `status_dict`），
        #     而不是在声明里写一份 `pending` / `confirmed` 的中文副本。
        #   * `target_id`：要按归属对象筛，得先选 `target_type`，那是**多态引用**
        #     （`f_ref_by` 的筛选条形态）。当前冻结的筛选契约里没有它。
        #     声明成普通整数输入框会让用户填 id —— 那正是 `target_id` 当初被改成
        #     多态 ref 要消灭的体验。收口方向同看板 t13。
        #   * `period`：取值是 `YYYY-MM`，是**月份**控件而不是日期。当前 6 种类型
        #     里没有 `month`，用 `date` 会拼出 `period=2026-01-01`，服务端解析不了。
        #     `occurred_from` / `occurred_to` 已经覆盖"按时间找"的绝大多数用法。
        search=True,
        filters=(
            FilterSpec("status", "记录状态", FilterKind.STATUS),
            FilterSpec(
                "source_type",
                "来源类型",
                FilterKind.ENUM,
                # 候选值与中文取自 `SOURCE_TYPE_LABELS`（唯一中文来源）。
                choices=tuple(
                    Choice(code, label) for code, label in SOURCE_TYPE_LABELS.items()
                ),
            ),
            FilterSpec(
                "occurred_on",
                "发生日期",
                FilterKind.DATE_RANGE,
                params=DateRangeParam("occurred_from", "occurred_to"),
            ),
        ),
    )
)

RESOURCES.register(
    Resource(
        name="accounting_period",
        title="会计期间",
        module="cost",
        list_path="/api/v1/cost/periods",
        workflow=None,
        # 同样显式声明：兜底会给出 `cost_accounting_periods`，而真实表名是
        # `accounting_periods`（它不属于任何单一业务域——期间是全局对象）。
        table="accounting_periods",
        columns=(
            ("period", "期间"),
            ("period_start", "开始日期"),
            ("period_end", "结束日期"),
            ("status_label", "状态"),
        ),
    )
)


# ============================================================================
# 共享校验与派生
# ============================================================================

def period_bounds(period: str) -> tuple[date, date]:
    """把 ``YYYY-MM`` 解析成 (期间起, 期间止)。

    为什么不用 `strptime('%Y-%m')`：它接受 `2026-1`，而 registry §2.10 要求的是
    严格的 `YYYY-MM`。**格式校验要卡在契约写的那个格式上**，不要接受"看起来也行"
    的变体——变体会让 `accounting_periods.period` 这个 CHAR(7) 唯一键的语义变松
    （`2026-01` 与 `2026-1` 是同一个月的两个键）。
    """
    parts = str(period).split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise DomainError(
            ErrorCode.FIELD_INVALID,
            "会计期间格式必须是 YYYY-MM（例如 2026-09）",
            data={"field": "period"},
        )
    year_text, month_text = parts
    if not (year_text.isdigit() and month_text.isdigit()):
        raise DomainError(
            ErrorCode.FIELD_INVALID,
            "会计期间格式必须是 YYYY-MM（例如 2026-09）",
            data={"field": "period"},
        )
    year, month = int(year_text), int(month_text)
    if not (1 <= month <= 12):
        raise DomainError(
            ErrorCode.FIELD_INVALID, "会计期间的月份必须在 01 到 12 之间", data={"field": "period"}
        )
    start = date(year, month, 1)
    end = date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)
    return start, date.fromordinal(end.toordinal() - 1)


def label_of(workflow: Workflow, code: str) -> str:
    """状态码 -> 中文标签。

    **状态的文案只能有一个来源**（`State.label`）。这里刻意不写第二份映射表——
    早期版本把同一个状态在 14 处各自翻译，同一个 `verified` 在成本页叫「待确认」、
    在其他页叫「已核验」、在 agent 词典里叫「已提交」。
    """
    for state in workflow.states:
        if state.code == code:
            return state.label
    return code


TARGET_TYPE_LABELS: dict[str, str] = {
    "farm": "基地",
    "area": "区域",
    "pond": "塘口",
    "batch": "批次",
}


def decorate(row: dict[str, Any], permissions: frozenset[str]) -> dict[str, Any]:
    """给一行成本记录加上前端需要的派生字段。

    派生项与它们的理由：

    * `status_label` / `confirm_state_label` —— 中文文案只在这里产生；
    * `source_type_label` / `target_label` —— 枚举值同样不该让前端翻译；
    * `amount` 转成字符串 —— Decimal 直接进 JSON 会变成浮点数，
      金额在传输层出现浮点误差是财务系统的经典缺陷；
    * `allowed_actions` —— 由状态机算，前端只渲染不推导；
    * `version` —— 统一叫 `version` 而不是 `row_version`
      （早期版本前端在 4 处地方猜过这个字段名）。
    """
    actions = list(
        allowed_actions_for(
            COST_ENTRY_WORKFLOW, "cost_entry", str(row.get("status") or ""), permissions
        )
    )
    # 成本是**双状态资源**：`status` 是记录生命周期、`confirm_state` 是归集状态。
    # 「确认」这个动作属于 `confirm_state` 机器（`pending -> confirmed`），只按
    # `status` 算 `allowed_actions` 会让**确认按钮永远不出现**（实测：草稿行的动作
    # 只有「查看」，用户只能走 Agent/API 确认）。两个机器的动作取并集。
    for action in allowed_actions_for(
        COST_ENTRY_WORKFLOW, "cost_entry", str(row.get("confirm_state") or ""), permissions
    ):
        if action not in actions:
            actions.append(action)

    decorated = dict(row)
    decorated["version"] = int(row.get("row_version") or 1)
    decorated["status_label"] = label_of(RECORD_LIFECYCLE, str(row.get("status") or ""))
    decorated["confirm_state_label"] = label_of(
        CONFIRM_STATE_WORKFLOW, str(row.get("confirm_state") or "")
    )
    decorated["source_type_label"] = SOURCE_TYPE_LABELS.get(
        str(row.get("source_type") or ""), str(row.get("source_type") or "")
    )
    target_type = row.get("target_type")
    decorated["target_label"] = (
        f"{TARGET_TYPE_LABELS.get(str(target_type), target_type)}#{row.get('target_id')}"
        if target_type
        else ""
    )
    if row.get("amount") is not None:
        decorated["amount"] = str(row["amount"])
    for key in ("occurred_on", "period_start", "period_end"):
        if row.get(key) is not None:
            decorated[key] = str(row[key])
    decorated["allowed_actions"] = actions
    return decorated


def assert_row_in_scope(scope: Scope, row: dict[str, Any], what: str = "该成本记录") -> None:
    """第三层范围校验：校验的是**具体那一行**，不是"权限码是否存在"。

    两种校验的失效模式不同——第二层（Gateway）被绕过时，这一层仍然拦得住，
    因为它看的是数据本身。
    """
    if not scope.allows_row(row):
        raise DomainError(ErrorCode.DATA_SCOPE_DENIED, f"{what}不在当前账号的数据范围内")


__all__ = [
    "CATEGORY_ENABLED",
    "CONFIRM_STATE_WORKFLOW",
    "COST_ENTRY_WORKFLOW",
    "MAX_AMOUNT",
    "RECORD_LIFECYCLE",
    "SOURCE_LEDGER",
    "SOURCE_MANUAL",
    "TARGET_TYPES",
    "assert_row_in_scope",
    "decorate",
    "label_of",
    "period_bounds",
]
