"""audit 域的资源声明（前端列表页的列的唯一来源）。

## 为什么需要它：`Capability.resource` 必须能解析

`Capability.resource` 是前端"按资源聚合能力"的键，而
`tests/test_architecture.py::test_capability_resources_resolve` 要求它必须在
`RESOURCES` 里存在。`audit.log.list` 声明的是 `resource="audit_log"`，
所以这里把它登记上。

## 这个资源**没有状态机**，这是刻意的

`Resource.workflow` 表示"这台状态机属于谁"，并从它派生 `status_dict` 与
`row_actions`。而**审计日志是 append-only 的**——001 迁移用两个触发器
（`audit_logs_no_update` / `audit_logs_no_delete`）在**数据库层**禁止改写与删除，
`SIGNAL SQLSTATE '45000'` 会直接拒绝任何 UPDATE/DELETE。

给一个不可变的流声明状态机，等于声明一台永远不会转移的机器；而 `row_actions`
会接着渲染出一排按钮，每一个都会撞上那个触发器。`docs/ROW_ACTIONS.md` §3.1 把
这类"声明了却推不动"的东西归为应当删掉的动作。

没有状态机的资源在元数据里就没有 `status_dict` / `transitions`，
前端据此不渲染状态列与行内按钮。**不渲染 > 渲染出来再失败**。

## 列名全部来自 `audit_logs` 的真实结构（001 迁移）

`result` 一列走的是 `result_label`（中文），映射在
`audit_logs.py::AUDIT_RESULT_LABELS`。**`before_json` / `after_json` /
`detail_json` 三列不在列表里**，也不在查询返回里——理由见
`audit_logs.py` 的模块 docstring。
"""

from __future__ import annotations

from yuxin.kernel.fields import RefTarget
from yuxin.kernel.workflow import (
    RESOURCES,
    DateRangeParam,
    FilterKind,
    FilterSpec,
    Resource,
)

RESOURCES.register(
    Resource(
        name="audit_log",
        title="操作日志",
        module="audit",
        list_path="/api/v1/audit-logs",
        # 表名显式声明：兜底公式（`<module>_<name>s`）会算出 `audit_audit_logs`。
        # 这与 cost 域的 `cost_entry` → `cost_cost_entrys` 是同一个坑：
        # 猜错了**不报错**，只有真正用到表名的规则才会在很远的地方撞上"表不存在"。
        table="audit_logs",
        columns=(
            ("created_at", "时间"),
            ("username", "账号"),
            ("capability_label", "操作"),
            ("domain_label", "业务模块"),
            ("object_ref", "对象"),
            ("result_label", "结果"),
            ("reason", "说明"),
        ),
        # 对应 `audit_logs.py` 的 `AUDIT_FILTERS` / `AUDIT_DATE_FILTERS` 两张表 ——
        # 过滤器清单本来就是声明式的，这里只是把它**下发**给前端，而不是另写一份。
        # **没有 keyword**：审计列表不支持模糊搜索。
        #
        # 为什么 `module_code` / `action_code` 两个审计维度**照实不声明**：
        # 它们的取值是机器码（`domain` / `capability`），而 `Resource.columns` 里
        # 对应的是 `domain` / `capability` 两列 —— 参数名与列名不同，
        # 于是两种声明形态都会出错：
        #   * 声明成 `key="module_code"` + `string`：控件渲染成**自由文本框**，
        #     用户打错一个字母就得到 0 行并且不报错（后端是等值过滤）；
        #   * 声明成 `key="domain"` + `enum`：候选值能取到，但拼出来的参数名是
        #     `domain`，而**后端只认 `module_code`** —— 一个永远筛不动的控件，
        #     正是本任务点名要避免的静默失败。
        # 收口方向（不是本次的活）：给审计维度补一条"参数名 ≠ 取值列"的声明形态
        # （例如 `FilterSpec(key=..., column=...)`），或者让前端把这两个字段渲染成
        # 按列取值的 ENUM，再由**服务端**把 `domain=` 翻成 `module_code=`。
        # 在那之前，"能按账号、能按时间"是真的能用，比多两个假控件有价值。
        filters=(
            # 按**账号**筛（链接原话就是"谁"）。判据是 `user_id` 在列里取不到值，
            # 而引用资源能给出候选与中文 —— 因此它是 ref 而不是 string：
            # 声明成 string 会让用户填 id，填错只会得到 0 行。
            FilterSpec(
                "user_id",
                "账号",
                FilterKind.REF,
                ref=RefTarget("access_user", "username"),
            ),
            # 按**时间**筛。区间做成一个控件，参数名由 `params` 给出。
            FilterSpec(
                "created_at",
                "时间",
                FilterKind.DATE_RANGE,
                params=DateRangeParam("created_from", "created_to"),
            ),
        ),
    )
)

__all__ = ["RESOURCES"]
