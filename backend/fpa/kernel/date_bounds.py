"""日期区间端点的 SQL 渲染。

## 为什么这里必须是一个共享模块

筛选条上的「日期区间」控件会送出一个**只到日**的上界（`date_to=2026-09-13`）。
如果直接渲染成

    WHERE happened_at <= '2026-09-13'

那么在 **DATETIME** 列上，当天 00:00:00 之后写入的行**全部被排除** ——
用户填"今天"，得到的却是空列表，而界面上没有任何线索说明为什么。
那正是本项目反复抓的那一类缺陷：**看起来在工作、而且不报错**。

（`DATE` 列没有这个问题：`2026-09-13 <= 2026-09-13` 成立。所以只有 DATETIME 列要展开。）
本仓实测的两处 DATETIME 列：`inventory_ledger.happened_at`、`audit_logs.created_at`；
另外四处日期区间（`cost_entries.occurred_on` / `sales_orders.sold_at` / `sales_orders.due_date` /
`purchase_orders.expected_delivery_date`）都是 `DATE`，不走这里。

## 两个实现选择

* 用 `< DATE_ADD(%s, INTERVAL 1 DAY)` 而不是 `DATE(col) <= %s`：后者给列套了函数，
  **必然全表扫**；前者仍能走 `col` 上的范围索引。
* **只在"只给日期"时展开**（长度 ≤ 10，即 `YYYY-MM-DD`）：调用方传带时分秒的值时，
  它要的就是精确比较，替他加一天是**擅自改语义**。
"""

from __future__ import annotations

__all__ = ["upper_bound"]

#: `YYYY-MM-DD` 的长度。比它长就认为带了时间部分（不展开）。
_DATE_ONLY_LENGTH = 10


def upper_bound(column: str, value: str) -> tuple[str, str]:
    """把"日期上界"渲染成 ``(SQL 片段, 绑定值)``。

    调用方把片段 `append` 进 `WHERE`、把绑定值 `append` 进 `values`
    （两者必须同序，所以一起返回而不是分成两个函数）。
    """
    if not value.strip():
        raise ValueError("upper_bound() 不接受空值：空条件应当由调用方直接跳过")
    if len(value.strip()) <= _DATE_ONLY_LENGTH:
        return f"{column} < DATE_ADD(%s, INTERVAL 1 DAY)", value
    return f"{column} <= %s", value
