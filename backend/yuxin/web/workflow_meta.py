"""资源元数据的序列化。

对应 `INTERFACES.md` §2 的 `resources: [...]` 段——前端 `DataTable` 的列与
`status_dict` 全靠它。

**注意**：`DECISIONS.md` Q14 删的是 `GET /api/v1/meta/resources` 这条**独立路由**
（契约从未定义过它），**不是**这个数组。数组必须保留——删了列表页会真的断掉。
`DECISIONS.md` 里为此加了一张术语澄清表。
"""

from __future__ import annotations

from typing import Any

from yuxin.kernel.workflow import (
    RESOURCES,
    RowAction,
    Tone,
    all_row_action_labels,
    row_action_options,
)


def resources_payload() -> list[dict[str, Any]]:
    """全部已注册资源的元数据。

    顺序稳定（按注册顺序），这样前端可以做 diff、CI 可以做快照比对。
    """
    return [item.to_meta() for item in RESOURCES.all()]


def actions_payload() -> dict[str, Any]:
    """动作与配色的枚举，供前端做编译期约束。

    `Tone` 只有 5 个值，`RowAction` 是闭集——前端 tone 色表的键必须落在这个集合里。
    早期版本用中文标签当色表键，跨文件复用后静默全灰且测试抓不到
    （`早期版本 returnModel.ts:43`）。
    """
    row_actions = row_action_options()
    return {
        "tones": [str(item) for item in Tone],
        # `row_actions` 保留为**字符串数组**（它回答"有哪些动作词"这类机械问题，
        # 例如 `kernel_smoke` 的 `== ['view','edit',...]` 断言）；
        # 而 `row_action_labels` 回答"每个词叫什么"。两者是不同的问题，
        # 合并成一份对象数组会让"只要值"的调用方多一次映射。
        "row_actions": [item["value"] for item in row_actions],
        # ★ 行内动作的中文标签。**服务端是唯一来源**
        #   （`kernel/workflow.py::ACTION_LABELS`）——前端只查表、不翻译。
        #
        # 加这一项的直接原因是实测缺陷：塘口列表页的"操作"列渲染的是**裸 token**
        # （按钮写着 `view` / `archive`），而同一行的状态列是中文（`养殖中`）。
        # 根因是动作词**没有标签落点**：状态有 `State.label` 经 `status_dict` 下发，
        # 动作却没有对应的东西 —— 于是 `DataTable.vue` 只能渲染 token、
        # `RecordActions.vue` 只能自己硬编码一张表，**同一个系统里两套口径**。
        # 本项就是那个缺失的落点。
        "row_action_labels": all_row_action_labels(),
    }


__all__ = ["actions_payload", "resources_payload"]
