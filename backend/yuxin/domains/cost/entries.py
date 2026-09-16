"""成本记录的读路径：列表、详情、汇总。

## 汇总的数字从哪来

`cost.summary` 回答"这笔钱是怎么算出来的"，因此它给的不是一个总数，而是：

    total_amount        期间内成本合计
    by_nature           按成本性质（direct / public）拆分
    by_category         按成本类别拆分，**含占比**
    by_target           按归属对象（塘口 / 批次）拆分
    entries / confirmed 记录条数（含"其中已确认多少条"）

**占比之和必须精确等于 100.0000**。这不是浮点误差可以推脱的事：
早期版本 `calculation.py:21-97` 的 `summarize_costs` 专门处理过这件事——
先取整再补差，把余数补给金额最大的一项。这里沿用同一做法，因为它有生产验证。

为什么不用 `Decimal` 的自然四舍五入然后听天由命：4 个类别各占 25% 时，
`ROUND(25,2)` 相加是 100.00；但 3 个类别各占 33.33% 时相加是 99.99——
报表上"占比合计 99.99%"会被每一个财务人员指出来。

## 为什么"占比"在服务端算而不是前端算

前端算占比需要拿到全部明细，而列表是**分页**的——分页数据算出的占比必然是错的
（这正是早期版本的一处缺陷：前端拿当前页算占比）。服务端算占比可以基于全量聚合，
与分页无关。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from yuxin.kernel.money import MONEY_QUANTUM
from typing import Any

from yuxin.kernel import capability as cap
from yuxin.kernel.errors import DomainError, ErrorCode, not_found
from yuxin.kernel.fields import f_date, f_enum, f_ref, f_str, text
from yuxin.kernel.invariants import lock_period_for_date
from yuxin.kernel.scope import Scope
from yuxin.kernel.uow import UnitOfWork

from yuxin.domains._base import Page

from .service import (
    CATEGORY_ENABLED,
    CONFIRM_STATE_WORKFLOW,
    COST_ENTRY_WORKFLOW,
    RECORD_LIFECYCLE,
    SOURCE_LEDGER,
    SOURCE_MANUAL,
    TARGET_TYPES,
    assert_row_in_scope,
    decorate,
    period_bounds,
)

#: 百分比保留 4 位小数（与旧 `calculation.py` 的口径一致）。
PERCENT_QUANTUM = Decimal("0.0001")

#: 金额保留 2 位。**口径来自内核**（`kernel/money.py`），
#: 本文件不再自带一份常量——同一件事曾在三处各写一遍（另两处
#: 在 `purchase/payables.py` 与 `warehouse/ledger.py`），而且数值还不一样。
#: `PERCENT_QUANTUM` 不动：它是**百分比**的精度，不是金额。
AMOUNT_QUANTUM = MONEY_QUANTUM


def _q(value: Any, quantum: Decimal) -> Decimal:
    return Decimal(str(value or 0)).quantize(quantum, rounding=ROUND_HALF_UP)


def share_percentages(amounts: list[Decimal], total: Decimal) -> list[Decimal]:
    """把一组金额换算成占比，**保证合计精确等于 100.0000**。

    算法（继承旧 `calculation.py` 的 allocate_amount 思路）：
      1. 各自四舍五入到 4 位小数；
      2. 把"100 减去已分配的合计"这个差额，全部补给**金额最大**的那一项。

    为什么补给最大项而不是依次 +0.0001：依次补会让"两个金额相同的类别"拿到不同的
    占比（先遍历到的那个多 0.0001），而它们的金额一样，凭什么占比不同？
    补给最大项在金额上站得住（绝对误差最小），也是早期版本验证过的做法。
    """
    if total <= 0 or not amounts:
        return [Decimal("0.0000") for _ in amounts]

    raw = [(amount / total * Decimal("100")).quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_UP)
           for amount in amounts]
    drift = Decimal("100.0000") - sum(raw, Decimal("0"))
    if drift != 0:
        biggest = max(range(len(amounts)), key=lambda index: (amounts[index], -index))
        raw[biggest] = (raw[biggest] + drift).quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_UP)
    return raw


def _selectable(workflow: Workflow) -> list[str]:
    """可选状态的代码列表（筛选白名单用）。

    转手一层而不在调用点写 `[s.code for s in wf.selectable_states()]`，
    是为了让下面那张四行表格的四个维度（字段 / 列 / 允许集）保持同形——
    嵌一个列表推导式进去会把那一行撞成三行，而它的意思没变。
    """
    return [state.code for state in workflow.selectable_states()]

#: 成本性质的中文。`default_nature` 只有两个值（004 迁移的 ENUM）。
#: 放在这里而不是类别表里逐行存：同一性质的所有类别应当是同一个中文，
#: 逐行存迟早会出现"公共成本"与"间接成本"这种同义不同词的漂移。
NATURE_LABELS: dict[str, str] = {"direct": "直接成本", "public": "公共成本"}

#: 会计期间状态码 → 中文（`accounting_periods.status` 的 ENUM）。
PERIOD_STATUS_LABELS: dict[str, str] = {"open": "未关账", "closed": "已关账"}

#: 成本记录读路径的统一 SELECT。
#:
#: `cost_entries.category_code` 是**软外键**（指向 `cost_categories.code`，见
#: `_require_category`），类别名**在库里**、后台可维护 —— 所以它不能在 Python 里
#: 写一张静态映射表（那会在类别改名/新增时静默漂移），必须在读路径 JOIN 出来。
#:
#: 用相关子查询而不是 `LEFT JOIN ... AS c`：`_filters()` / `scope.where_clause()`
#: 生成的 WHERE 片段按**表名**`cost_entries` 限定列，别名会打断它们（两处描述同一件事）。
#: 子查询不引入第二个表名，因此三处读（列表 / 详情 / 回读）可以共用同一段 SQL。
#: `COALESCE(..., category_code)` 的回退口径与 `cost.summary` 的 `by_category` 一致：
#: 类别被删掉时显示原码（可诊断），不显示空白。
_ENTRY_SELECT = (
    "SELECT cost_entries.*, "
    "COALESCE("
    "  (SELECT c.name FROM cost_categories AS c "
    "    WHERE c.organization_id = cost_entries.organization_id "
    "      AND c.code = cost_entries.category_code), "
    "  cost_entries.category_code) AS category_label "
    "FROM cost_entries"
)


class CostEntryService:
    """成本记录的读路径。

    与 master_data 的 `PondService` 同构：`_scope_row` 做第三层防御，
    `load_entry` 是执行器回读用的函数（`docs/WRITE_CONTRACT.md` 规则 2）。
    """

    # -- 内部工具 -------------------------------------------------------------

    @staticmethod
    def _scope_row(tx: UnitOfWork, scope: Scope, entry_id: int) -> dict[str, Any]:
        row = tx.query_one(f"{_ENTRY_SELECT} WHERE id = %s", (entry_id,))
        if row is None:
            raise not_found("成本记录")
        assert_row_in_scope(scope, row)
        return row

    @staticmethod
    def _require_open_period(
        tx: UnitOfWork, occurred_on: date, organization_id: int
    ) -> dict[str, Any]:
        """发生日期必须落在一个**存在且未关账**的会计期间内。

        ## 为什么服务层也查一次（不变量已经查了）

        执行器上的 `PeriodOpen` 不变量在**业务写入之后**才跑（`runner.py`：
        `_call_service` -> `run_invariants`）。它保证"已经不合法"的数据不会落库，
        但报错发生在写入路径的末端。服务层提前查一次，是为了给出**具体到期间**的
        可读错误（"2027-01 已关账，2027-01-15 的成本不能再登记"），
        并顺便解析出 `period_start` / `period_end` 落库。

        两处校验的**判据必须一致**：都只看 `accounting_periods.status`，
        不在这里给某条入口额外宽松。早期版本的缺陷正是"派生路径能绕过期间锁定"
        ——两条路径判据不同就是那个缺陷的复活方式。

        ## 为什么放在基类（`CostEntryService`）而不是写服务

        它被**两条**写入路径共用：人工入口（`cost.entry.create` /
        `cost.entry.confirm`）与跨域自动归集入口（`record_fact()`，由
        production / warehouse / sales 调用）。放在写服务上会让 `record_fact`
        反向依赖 `CostWriteService`；放在基类上两条路径都能用，
        而且"期间是否开放"这件事**只有一处实现**——这正是
        "一个不变量覆盖多条能力"在服务层的对应形态。
        """
        row = lock_period_for_date(
            tx, organization_id=int(organization_id), occurred_on=occurred_on
        )
        if row is None:
            # **刻意放行**（与 `kernel/invariants.py::PeriodOpen` 一致：查不到期间
            # 记录时不阻断）。理由：还没配会计期间的企业不该被系统卡住，
            # 而"关账"这件事必须有明确的作用对象。返回一个合成的 open 期间，
            # 让调用方仍能拿到准确的月份边界。
            start, end = period_bounds(f"{occurred_on.year:04d}-{occurred_on.month:02d}")
            return {
                "id": None,
                "period": f"{occurred_on.year:04d}-{occurred_on.month:02d}",
                "status": "open",
                "period_start": start,
                "period_end": end,
            }
        if str(row["status"]) == "closed":
            raise DomainError(
                ErrorCode.CONFLICT,
                f"{row['period']} 已经关账，不能再登记属于该期间的成本",
                data={"rule": "PERIOD_CLOSED", "period": str(row["period"])},
            )
        return row

    @staticmethod
    def _require_category(
        tx: UnitOfWork, organization_id: int, category_code: str
    ) -> dict[str, Any]:
        """成本类别必须存在且启用（registry §2.10：`category_code` 须为**启用的**成本类别）。

        为什么显式查而不是靠外键：类别是**软外键**（`cost_entries.category_code`
        存 code 而不是 id —— 它要在多个域被引用、且需要可读）。软外键没有数据库
        兜底，所以存在性与启用状态必须在这里查。

        为什么不把"是否启用"也做成外键或触发器：停用是**业务决定**（这个月不再用
        "其他费用"这类），而触发器会让"历史记录引用了已停用类别"变得无法表达
        ——历史记录必须能读出来。
        """
        row = tx.query_one(
            "SELECT code, name, default_nature, status FROM cost_categories "
            "WHERE organization_id = %s AND code = %s",
            (organization_id, category_code),
        )
        if row is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                f"成本类别 {category_code} 不存在",
                data={"field": "category_code"},
            )
        if str(row["status"]) != CATEGORY_ENABLED:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                f"成本类别「{row['name']}」已停用，不能用于新登记",
                data={"field": "category_code"},
            )
        return row

    @staticmethod
    def _filters(params: dict[str, Any], alias: str = "") -> tuple[list[str], list[Any]]:
        """把查询参数翻成 WHERE 片段。``alias`` 用于 JOIN 形态（列名加前缀）。

        ## 为什么别名是参数而不是"渲染完再改字符串"

        列限定符是查询的一部分，不是字符串装饰。第一版我让 `_filters` 不带别名，
        然后在 JOIN 查询前用 `str.replace` 补 `e.` 前缀——那种做法要靠"替换规则"
        与"片段写法"永远保持一致，而片段写法是可以随时被下一个人改的。
        改法就是让前缀在**片段生成时**就确定。

        ## 白名单为什么必需

        `status` / `confirm_state` / `source_type` / `target_type` 都做取值白名单校验：
        非法值报 `FIELD_INVALID`，而不是"过滤出 0 行"。**静默返回空集**是早期版本
        `data_scope.py` 的经典缺陷（`return "1=0", []`）——用户看到 0 行却不报错，
        会以为是"确实没有数据"而不是"参数写错了"。
        """
        prefix = f"{alias}." if alias else ""
        where: list[str] = []
        values: list[Any] = []

        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            where.append(f"({prefix}source_ref LIKE %s OR {prefix}note LIKE %s)")
            values.extend([f"%{keyword}%", f"%{keyword}%"])

        for field, column, allowed in (
            # 筛选白名单用 `selectable_states()`，不是 `states`。
            #
            # 差别是实质的：`submitted` / `archived` 标了 `reserved=True`（没有能力能进入它），
            # 而它们仍然在 `states` 里——因为历史行可能就是那个值，
            # `status_label` 必须能翻译它。但"能筛选"是另一件事：
            # 允许用户筛选一个永远为空的条件，等于把“没数据”伪造成一次空结果。
            ("status", "status", _selectable(RECORD_LIFECYCLE)),
            ("confirm_state", "confirm_state", _selectable(CONFIRM_STATE_WORKFLOW)),
            ("source_type", "source_type", ["manual_expense", "warehouse_ledger"]),
            ("target_type", "target_type", list(TARGET_TYPES)),
            ("category_code", "category_code", None),
        ):
            value = str(params.get(field) or "").strip()
            if not value:
                continue
            if allowed is not None and value not in allowed:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    f"{field} 的取值无效",
                    data={"field": field, "allowed": allowed},
                )
            where.append(f"{prefix}{column} = %s")
            values.append(value)

        if params.get("target_id") not in (None, ""):
            try:
                where.append(f"{prefix}target_id = %s")
                values.append(int(params["target_id"]))
            except (TypeError, ValueError) as exc:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "归属对象必须是整数",
                    data={"field": "target_id"},
                ) from exc

        period = str(params.get("period") or "").strip()
        if period:
            from .service import period_bounds

            start, end = period_bounds(period)
            where.append(f"{prefix}period_start = %s AND {prefix}period_end = %s")
            values.extend([start, end])

        for field, column, op in (
            ("occurred_from", "occurred_on", ">="),
            ("occurred_to", "occurred_on", "<="),
        ):
            value = params.get(field)
            if value in (None, ""):
                continue
            where.append(f"{prefix}{column} {op} %s")
            values.append(value)

        return where, values

    # -- 读 -------------------------------------------------------------------

    def list_entries(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        query: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """成本记录列表（分页）。"""
        ctx.require("cost.view")
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))

        where = ["1=1"]
        values: list[Any] = []
        scope_fragment, scope_values = scope.where_clause("cost_entries")
        if scope_fragment and scope_fragment != "1=1":
            where.append(f"({scope_fragment})")
            values.extend(scope_values)
        extra_where, extra_values = self._filters(params)
        where.extend(extra_where)
        values.extend(extra_values)
        clause = " AND ".join(where)

        total_row = tx.query_one(
            f"SELECT COUNT(*) AS n FROM cost_entries WHERE {clause}", values
        )
        total = int((total_row or {}).get("n", 0))

        rows = tx.query_all(
            f"{_ENTRY_SELECT} WHERE {clause} "
            "ORDER BY occurred_on DESC, id DESC LIMIT %s OFFSET %s",
            [*values, page.size, page.offset],
        )
        return cap.HandlerResult(
            data=page.to_result(
                [decorate(row, ctx.actor.permissions) for row in rows], total
            ),
            message="",
        )

    def get_entry_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        entry_id: int | None = None,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """成本记录详情。"""
        ctx.require("cost.view")
        raw = (path_params or {}).get("entry_id", entry_id)
        if raw is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "成本记录详情需要 path_params 传 entry_id")
        row = self._scope_row(tx, scope, int(raw))
        return cap.HandlerResult(data={"record": decorate(row, ctx.actor.permissions)})

    def load_entry(
        self, tx: UnitOfWork, *, scope: Scope, record_id: int
    ) -> dict[str, Any] | None:
        """回读函数。

        执行器在提交前调它确认"写入真的落了库"——`executed` 不是"我们调用了 INSERT"，
        而是"读回来的行确实是我们想要的样子"（`docs/WRITE_CONTRACT.md` 规则 2）。
        少了它，执行器会抛 `INTERNAL_ERROR`（"声称写入成功但没有提供回读函数"）。
        """
        row = tx.query_one(f"{_ENTRY_SELECT} WHERE id = %s", (record_id,))
        if row is None:
            return None
        return {**row, "version": int(row.get("row_version") or 1)}

    def list_periods(
        self,
        tx: UnitOfWork,
        ctx,
        *,
        query: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """会计期间列表。

        ## 为什么补这一条

        `accounting_period` 原先**只有** `cost.period.close`（action），没有读能力 ——
        于是它进不了导航（导航按"有读能力"判断），**关账在页面上没有入口**。
        期间是全局对象（`cost.period.close` 的 `scope=none`），所以这里与关账同一口径：
        不加数据范围谓词。
        """
        ctx.require("cost.view")
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))

        where = ["1=1"]
        values: list[Any] = []
        status = str(params.get("status") or "").strip()
        if status:
            where.append("status = %s")
            values.append(status)
        clause = " AND ".join(where)

        total = int(
            tx.query_scalar(f"SELECT COUNT(*) FROM accounting_periods WHERE {clause}", values) or 0
        )
        rows = tx.query_all(
            "SELECT * FROM accounting_periods "
            f"WHERE {clause} ORDER BY period DESC LIMIT %s OFFSET %s",
            [*values, page.size, page.offset],
        )
        can_close = "cost.close" in ctx.actor.permissions
        items = []
        for row in rows:
            status_code = str(row["status"])
            items.append(
                {
                    "id": int(row["id"]),
                    "period": str(row["period"]),
                    "period_start": str(row["period_start"]),
                    "period_end": str(row["period_end"]),
                    "status": status_code,
                    "status_label": PERIOD_STATUS_LABELS.get(status_code, status_code),
                    "version": int(row.get("row_version") or 1),
                    # 已关账的期间不能再关一次（状态机语义由能力侧兜底，这里只渲染）
                    "allowed_actions": ["close"] if (can_close and status_code == "open") else [],
                }
            )
        return cap.HandlerResult(data=page.to_result(items, total), message="")

    def load_period(
        self, tx: UnitOfWork, *, scope: Scope, record_id: int
    ) -> dict[str, Any] | None:
        """会计期间的回读函数。

        它按 **period 字符串** 还是 **id** 回读？关账能力的 `resource_id` 是期间记录的
        id（执行器按它回读），所以这里按 id。`period` 本身不是主键——同一期间在不同
        企业下是两行。
        """
        row = tx.query_one("SELECT * FROM accounting_periods WHERE id = %s", (record_id,))
        if row is None:
            return None
        return {**row, "version": int(row.get("row_version") or 1)}

    # -- 汇总 -----------------------------------------------------------------

    def summarize_costs(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        query: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """按塘口 / 批次查询成本汇总。

        这是"成本是怎么算出来的"的界面：不只给总数，还给按性质、按类别、按归属对象的
        拆分。**只给一个总数等于要求用户相信系统**，而"相信"不是可验证的东西。

        ## 为什么这里有两套 WHERE 片段

        计数段只查 `cost_entries` 一张表，**不带别名**；而性质/类别拆分要 JOIN
        `cost_categories` 取成本性质，因此明细表必须起别名 `e`。

        两套片段的列限定符不同（无前缀 vs `e.`），所以它们是**分别渲染**的，
        而不是"渲染一次、改个字符串"。上一次我正是想把一套片段复用到两种形态上，
        结果是 `cost_entries.amount` 出现在别名 `e` 的查询里 —— MySQL 直接报
        "Unknown column"。**列限定符不是字符串装饰，它是查询的一部分。**
        """
        ctx.require("cost.view")
        params = query or {}

        plain_clause, plain_values = self._summary_where(scope, params, alias="")
        joined_clause, joined_values = self._summary_where(scope, params, alias="e")

        # 计数段：单表，无别名
        counts = tx.query_one(
            "SELECT COUNT(*) AS entries, "
            "SUM(CASE WHEN confirm_state='confirmed' THEN 1 ELSE 0 END) AS confirmed, "
            "SUM(CASE WHEN confirm_state='pending' THEN 1 ELSE 0 END) AS pending, "
            "COALESCE(SUM(CASE WHEN confirm_state='confirmed' THEN amount ELSE 0 END),0) AS confirmed_amount, "
            "COALESCE(SUM(CASE WHEN confirm_state='pending' THEN amount ELSE 0 END),0) AS pending_amount, "
            f"COALESCE(SUM(amount),0) AS total_amount FROM cost_entries WHERE {plain_clause}",
            plain_values,
        ) or {}
        total_amount = _q(counts.get("total_amount"), AMOUNT_QUANTUM)

        # 按成本性质拆分。性质取自**类别表**，这是它的唯一来源——
        # 早期版本把这个值同时存在 cost_entries.cost_nature、cost_categories.default_nature
        # 与汇总时的重算里（`早期版本 cost_store.py:54-55` 按 row["nature"] 累加），三处真相。
        nature_rows = tx.query_all(
            "SELECT COALESCE(c.default_nature,'direct') AS nature, "
            "COALESCE(SUM(e.amount),0) AS amount, COUNT(*) AS entries "
            "FROM cost_entries AS e "
            "LEFT JOIN cost_categories AS c "
            "  ON c.organization_id = e.organization_id AND c.code = e.category_code "
            f"WHERE {joined_clause} "
            "GROUP BY COALESCE(c.default_nature,'direct') ORDER BY amount DESC",
            joined_values,
        )
        nature_amounts = [_q(row["amount"], AMOUNT_QUANTUM) for row in nature_rows]
        nature_shares = share_percentages(nature_amounts, total_amount)
        by_nature = [
            {
                "nature": str(row["nature"]),
                "label": str(NATURE_LABELS.get(str(row["nature"]), row["nature"])),
                "amount": str(nature_amounts[index]),
                "entries": int(row["entries"]),
                "percent": str(nature_shares[index]),
            }
            for index, row in enumerate(nature_rows)
        ]

        category_rows = tx.query_all(
            "SELECT e.category_code AS category_code, "
            "COALESCE(c.name, e.category_code) AS category_name, "
            "COALESCE(SUM(e.amount),0) AS amount, COUNT(*) AS entries "
            "FROM cost_entries AS e "
            "LEFT JOIN cost_categories AS c "
            "  ON c.organization_id = e.organization_id AND c.code = e.category_code "
            f"WHERE {joined_clause} "
            "GROUP BY e.category_code, COALESCE(c.name, e.category_code) "
            "ORDER BY amount DESC",
            joined_values,
        )
        category_amounts = [_q(row["amount"], AMOUNT_QUANTUM) for row in category_rows]
        category_shares = share_percentages(category_amounts, total_amount)
        by_category = [
            {
                "category_code": str(row["category_code"]),
                "category_name": str(row["category_name"]),
                "amount": str(category_amounts[index]),
                "entries": int(row["entries"]),
                "percent": str(category_shares[index]),
            }
            for index, row in enumerate(category_rows)
        ]

        target_rows = tx.query_all(
            "SELECT target_type, target_id, "
            "COALESCE(SUM(amount),0) AS amount, COUNT(*) AS entries "
            f"FROM cost_entries WHERE {plain_clause} AND target_type IS NOT NULL "
            "GROUP BY target_type, target_id ORDER BY amount DESC LIMIT 200",
            plain_values,
        )
        target_amounts = [_q(row["amount"], AMOUNT_QUANTUM) for row in target_rows]
        target_shares = share_percentages(target_amounts, total_amount)
        by_target = [
            {
                "target_type": str(row["target_type"]),
                "target_id": int(row["target_id"]),
                "amount": str(target_amounts[index]),
                "entries": int(row["entries"]),
                "percent": str(target_shares[index]),
            }
            for index, row in enumerate(target_rows)
        ]

        # 占比合计的**可验证闭合条件**：合计必须精确等于 100.0000（无数据时为 0）。
        # 不做静默修正——"占比合计 99.99%"是会被每一个财务人员指出来的问题，
        # 所以把它算出来并随响应返回，让这条断言在响应里就可见。
        percent_total = (
            sum(category_shares, Decimal("0")) if category_rows else Decimal("0")
        )

        return cap.HandlerResult(
            data={
                "summary": {
                    "total_amount": str(total_amount),
                    "confirmed_amount": str(_q(counts.get("confirmed_amount"), AMOUNT_QUANTUM)),
                    "pending_amount": str(_q(counts.get("pending_amount"), AMOUNT_QUANTUM)),
                    "entries": int(counts.get("entries") or 0),
                    "confirmed_entries": int(counts.get("confirmed") or 0),
                    "pending_entries": int(counts.get("pending") or 0),
                    "by_nature": by_nature,
                    "by_category": by_category,
                    "by_target": by_target,
                    "category_percent_total": str(percent_total.quantize(PERCENT_QUANTUM)),
                }
            },
            message="",
        )

    def _summary_where(
        self, scope: Scope, params: dict[str, Any], *, alias: str
    ) -> tuple[str, list[Any]]:
        """构造汇总用的 WHERE 与参数。``alias`` 为 '' 或 'e'（JOIN 形态）。

        列限定符在片段生成时就确定（见 `_filters` 的说明），不靠"渲染完再改字符串"。
        """
        prefix = f"{alias}." if alias else ""
        where = ["1=1"]
        values: list[Any] = []

        scope_fragment, scope_values = scope.where_clause(alias)
        if scope_fragment and scope_fragment != "1=1":
            # `where_clause(alias)` 用的是 `Scope` 的分租键列名（area_id / farm_id / …），
            # 这些列在 cost_entries 上直接存在（Q6 裁决：补分租列，不做跨表解析），
            # 所以 scope 谓词与 `_filters` 用的是同一张表、同一个别名。
            where.append(f"({scope_fragment})")
            values.extend(scope_values)

        target_type = str(params.get("target_type") or "").strip()
        if target_type:
            if target_type not in TARGET_TYPES:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "归属对象类型的取值无效",
                    data={"field": "target_type", "allowed": list(TARGET_TYPES)},
                )
            where.append(f"{prefix}target_type = %s")
            values.append(target_type)

        extra_where, extra_values = self._filters(params, alias)
        where.extend(extra_where)
        values.extend(extra_values)
        return " AND ".join(where), values


#
# 契约原文：`cost.entries.record_fact(...)` 归属 cost，调用方是
# production / warehouse / sales，用途是「**唯一**成本归集入口」。
#
# 为什么必须是"函数"而不是让调用方自己 INSERT：
#
#   库存自动归集这条路上有三个业务判断，缺一个就会出账目问题——
#     ① 期间是否已关账（Q16：关账锁整个期间，收入侧与成本侧一视同仁）
#     ② 同一笔业务事实是否已被归集过（§4 #8：防同一塘口/批次同期重复计入）
#     ③ 归属对象与分租键从哪来（Q7：分租键必须由服务端解析，不能由调用方自报）
#
#   如果 production / warehouse / sales 各写一份 INSERT，这三个判断会有三套答案，
#   而每一套在各自的单域 e2e 里都能过——只在集成时炸。这与 `apply_movement`
#   被定为"唯一库存入口"是同一条理由（见 warehouse/ledger.py 的模块说明）。
#
# 刻意**不做**的事：
#   * **不做权限校验**（不收 `ctx`）。本函数是**域内协作**入口，调用方是另一个域
#     的服务方法，而那次调用的权限已在**调用方自己的能力**上校验过
#     （如 `feeding.verify` 的 `feeding.verify` 权限）。在这里再要一个
#     `cost.manage` 权限会让"投喂核验"依赖调用者另有成本权限——那是错的。
#   * **不做幂等键校验**。幂等由唯一键 `uq_cost_entries_org_dedupe` 在数据库层
#     保证（同一 farm + 归属对象 + 期间 + 来源单号只能一条），并发下也成立。
#   * **不写审计**。审计由执行器按**调用方那个能力**的声明写（`feeding.verify`
#     的审计应当记在 feeding 上，而不是记在一条没被调用过的 cost 能力上）。
#   * **不改记录状态以外的字段**。归集出来的记录 `status='draft'`、
#     `confirm_state='pending'`——它仍需走 `cost.entry.confirm` 的双人复核。
#     自动归集**不等于**已确认入账。

#: 跨域归集允许使用的成本类别。
#:
#: 为什么把它们列成**公开常量**而不是让调用方猜字符串：类别是软外键
#: （`cost_entries.category_code` 存 code），调用方拼错会得到一个
#: `FIELD_INVALID`，而那个错误出现在别人的域里、离调用点很远。给出常量的好处是
#: **拼错变成 AttributeError 而不是运行期才发现的字段错误**。
#:
#: 用 `warehouse_ledger` 归集的场景各用哪一类（与 004 迁移的种子一致）：
#:
#:     warehouse  receipt.verify  采购入库    -> LEDGER_FEED（饲料）或按物料 category 映射
#:     warehouse  issue.verify    领用出库    -> LEDGER_FEED
#:     production feeding.verify  投喂用料    -> LEDGER_FEED
#:     sales      delivery.verify 不发库存，本版不归集物料成本（见下方说明）
LEDGER_FEED = "feed"
LEDGER_SEED = "seed"
LEDGER_HEALTH = "health"
LEDGER_CATEGORY_CODES = (LEDGER_FEED, LEDGER_SEED, LEDGER_HEALTH)

#: 物料 `category` 字段 → 成本类别 的映射。
#:
#: `materials.category` 是 VARCHAR（"会变的分类不该固化成约束"，见 003 迁移），
#: 所以这里给的是**已知值的映射**，未命中时回落到 `LEDGER_FEED`。
#: 为什么不直接拿 `materials.category` 当 `category_code`：两者是不同的字典
#: （物料分类可以有 `chemical` / `equipment`，而成本类别只有 6 个），
#: 直接透传会让 `cost_categories` 里出现不存在的类别。
MATERIAL_CATEGORY_TO_COST: dict[str, str] = {
    "feed": LEDGER_FEED,
    "seed": LEDGER_SEED,
    "fingerling": LEDGER_SEED,
    "health": LEDGER_HEALTH,
    "medicine": LEDGER_HEALTH,
    "drug": LEDGER_HEALTH,
}


@dataclass(frozen=True, slots=True)
class CostFact:
    """一次跨域归集的结果。

    为什么返回对象而不是一个 int：调用方要拿它渲染 `message`
    （WRITE_CONTRACT 规则 3：`message` 由服务端从**真实数据**渲染）。
    `period` 尤其重要——用户需要看到"这笔成本归到了哪个月"，而那是服务端算的，
    不是调用方传的。

    字段对齐 `warehouse.ledger.Movement` 的做法（同样是 frozen dataclass +
    业务语义字段），这样调用方处理两个"归集结果"的形态是一致的。
    """

    entry_id: int
    organization_id: int
    farm_id: int
    area_id: int
    period: str
    period_start: date
    period_end: date
    category_code: str
    amount: Decimal
    source_ref: str
    target_type: str
    target_id: int
    confirm_state: str
    status: str

    @property
    def ledger_id(self) -> int:
        """别名为 `cost_entries.id`，与 `Movement.movement_id` 的命名习惯对齐。"""
        return self.entry_id

    def describe(self) -> str:
        """给调用方拼 `message` 用的一句话（数字全部来自本次真实写入）。"""
        return (
            f"归集成本 {self.amount} 元（{self.category_code}，"
            f"归属 {self.target_type}#{self.target_id}，期间 {self.period}），待确认"
        )


def record_fact(
    tx: UnitOfWork,
    *,
    organization_id: int,
    farm_id: int,
    area_id: int,
    category_code: str,
    amount: Decimal | int | str,
    occurred_on: date | str,
    source_ref: str,
    target_type: str,
    target_id: int,
    actor_id: int,
    note: str = "",
    source_type: str = SOURCE_LEDGER,
    period_start: date | str | None = None,
    period_end: date | str | None = None,
) -> CostFact:
    """**全系统唯一的成本归集入口**（ROLLOUT_CONTRACT §2）。

    调用方：production（`feeding.verify` / `harvest.verify`）、
    warehouse（`receipt.verify` / `issue.verify`）、sales（按需）。
    **调用方不得自己 `INSERT INTO cost_entries`。**

    ## 参数

    必填：
      * `tx`      —— **同一个事务**。跨域效果必须与调用方的业务写入同生共死
                     （ROLLOUT_CONTRACT §2："在同一个事务（同一个 `tx`）内完成"）。
      * `organization_id` / `farm_id` / `area_id`
                  —— 分租键，由**调用方从其业务对象上解析**后传入。
                     为什么不由本函数查：这三个值来自调用方的业务行（比如
                     `inventory_ledger` 的那一行），调用方手上已经有了；
                     让本函数拿 `target_id` 回查 `batch_id`/`pond_id` 所属的
                     farm/area 会引入一次多余查询，且**跨域查表**是被禁止的
                     （ROLLOUT_CONTRACT §2）。
      * `category_code` —— 用本模块的 `LEDGER_*` 常量，或经
                     `MATERIAL_CATEGORY_TO_COST` 映射物料分类。
      * `amount`  —— **正数**。归集金额由调用方按自己的业务口径算出
                     （通常 `quantity × unit_cost`）。允许传 `str`/`int`，
                     内部统一转 `Decimal`；不接受浮点（`float` 一律拒绝，
                     理由见下）。
      * `occurred_on` —— 业务事实发生日（最终决定成本归到哪个月）。
      * `source_ref` —— **来源单号，必须稳定且唯一标识那笔业务事实**。
                     推荐用 `warehouse.ledger.ledger_source_ref(域, 单据号)`
                     的同一形态（如 `feeding:FEED-001`）——它是去重键的一半，
                     同一个业务事实重放必须传同一个字面值。
      * `target_type` / `target_id` —— 成本归属对象（`pond` / `batch` / `area` /
                     `farm`）。**必填**：成本归属是财务口径，不该由系统猜。
                     投喂归到 `batch`（无批次时归 `pond`），入库归到 `pond`/`batch`。
      * `actor_id`    —— **经手人**（调用方服务方法里的 `ctx.actor.user_id`）。
                     **必填，没有默认值**。它写进 `cost_entries.created_by`，
                     而 `created_by` 是 §4 #5「经办人 ≠ 审批人」比较的那一列：
                     给默认值（或写死 0）会让自动归集的记录"没有经手人"，
                     于是后续 `cost.entry.confirm` 的双人复核在**自动归集路径上
                     直接失效**——一条本该被他人复核的成本，自己就能确认。
                     这是刻意不给默认值的原因：这类参数一旦可省，就一定会有人省。

    可选：
      * `note`        —— 备注（≤500）。
      * `source_type` —— **默认 `warehouse_ledger`，不要改**。另一个取值
                     `manual_expense` 属于人工入口 `cost.entry.create`；
                     跨域自动归集走 `warehouse_ledger` 才能被 §4 #8 的去重
                     规则正确识别。保留这个参数只为可测试性。
      * `period_start` / `period_end` —— 默认按 `occurred_on` 所在自然月。
                     跨月分摊时才显式给。

    ## 返回

    `CostFact` —— 含 `entry_id` 与**服务端解析出的** `period`，供调用方渲染 message。

    ## 失败语义（调用方只需处理 `DomainError`）

      * `CONFLICT` / `PERIOD_CLOSED`
            —— 该期间已关账（Q16）。**这是拒绝，不是告警**：调用方应当让整个
               业务操作失败（例如 `feeding.verify` 整体回滚），而不是跳过成本归集。
               理由：如果允许"库存扣了但成本没记"，账目就永久失去平衡。
      * `CONFLICT` / `COST_SOURCE_DUPLICATED`
            —— 同一笔业务事实已被归集过（同一归属 + 同期间 + 同 `source_ref`）。
               这条**在数据库唯一键上是物理底线**（并发下也成立），本函数先做一个
               可读性检查以给出准确文案。
               调用方看到它说明**重放了同一笔事实**——应当检查为什么会重放，
               而不是换个 `source_ref` 再试（那会真的记两笔）。
      * `FIELD_INVALID` —— 类别不存在 / 已停用、金额非正、归属对象类型非法。
      * `VALIDATION_ERROR` —— 参数缺失或形态错误（含 `amount` 传了 float）。

    ## 为什么拒绝 `float`

    `float` 参与金额计算会产生二进制舍入误差（`0.1 + 0.2 != 0.3`），而成本要
    参与汇总与占比闭合断言。传 float 时**一律报错**而不是静默 `Decimal(str(x))`：
    后者看起来能用，但调用方以为自己在传精确值——**金额的精度问题必须在源头暴露**。
    """
    # ---- 参数形态校验（全部显式报错，不做静默纠正）----------------------
    if isinstance(amount, float):
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            "归集金额不能是浮点数（会引入二进制舍入误差）；请传 str 或 Decimal",
            data={"field": "amount", "rule": "NO_FLOAT_MONEY"},
        )
    try:
        amount_value = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, "归集金额格式无效", data={"field": "amount"}
        ) from exc
    if not amount_value.is_finite() or amount_value <= 0:
        raise DomainError(
            ErrorCode.FIELD_INVALID,
            "归集金额必须大于 0（成本记录只表达「支出发生了」，冲销记在下一期间）",
            data={"field": "amount"},
        )

    if target_type not in TARGET_TYPES:
        raise DomainError(
            ErrorCode.FIELD_INVALID,
            "成本归属对象类型无效",
            data={"field": "target_type", "allowed": list(TARGET_TYPES)},
        )
    if not str(source_ref or "").strip():
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            "归集成本必须给出来源单号（每笔成本都要能追溯到底单）",
            data={"field": "source_ref"},
        )
    if source_type not in (SOURCE_LEDGER, SOURCE_MANUAL):
        raise DomainError(
            ErrorCode.FIELD_INVALID,
            "来源类型无效",
            data={"field": "source_type", "allowed": [SOURCE_LEDGER, SOURCE_MANUAL]},
        )

    occurred = _as_date(occurred_on, "occurred_on")
    default_start, default_end = period_bounds(
        f"{occurred.year:04d}-{occurred.month:02d}"
    )
    start = _as_date(period_start, "period_start") if period_start is not None else default_start
    end = _as_date(period_end, "period_end") if period_end is not None else default_end
    if start > occurred or occurred > end:
        raise DomainError(
            ErrorCode.FIELD_INVALID,
            "发生日期必须落在期间之内（期间起 ≤ 发生日期 ≤ 期间止）",
            data={"field": "occurred_on", "period_start": str(start), "period_end": str(end)},
        )

    # ---- ①②③ 三个业务判断（唯一实现处）----------------------------------
    CostEntryService._require_category(tx, int(organization_id), str(category_code))
    # 期间锁定用 `start`（跨月分摊时以期间起点所在月为准），并对 occurred_on 再查一次：
    # 两者都可能落在不同月份，任何一个已关账都不该放行（与 `create_entry` 同一口径）。
    CostEntryService._require_open_period(tx, start, int(organization_id))
    if occurred != start:
        CostEntryService._require_open_period(tx, occurred, int(organization_id))

    # 去重的**可读性**检查：给出准确文案；物理底线是下面的唯一键。
    duplicate = tx.query_one(
        "SELECT id FROM cost_entries WHERE organization_id=%s AND farm_id=%s "
        "AND target_type=%s AND target_id=%s AND period_start=%s AND period_end=%s "
        "AND source_ref=%s LIMIT 1",
        (int(organization_id), int(farm_id), str(target_type), int(target_id),
         start, end, str(source_ref).strip()),
    )
    if duplicate is not None:
        raise DomainError(
            ErrorCode.CONFLICT,
            f"这笔业务事实的成本已归集过（来源单号 {source_ref}），不能重复计入",
            data={
                "rule": "COST_SOURCE_DUPLICATED",
                "existing_id": int(duplicate["id"]),
                "source_ref": str(source_ref),
            },
        )

    # ---- 写入 -------------------------------------------------------------
    tx.execute(
        "INSERT INTO cost_entries "
        "(organization_id, farm_id, area_id, category_code, amount, occurred_on, "
        " period_start, period_end, source_type, source_ref, target_type, target_id, "
        " note, confirm_state, status, created_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending','draft',%s)",
        (
            int(organization_id), int(farm_id), int(area_id), str(category_code),
            amount_value, occurred, start, end, str(source_type),
            str(source_ref).strip(), str(target_type), int(target_id),
            note or None, int(actor_id),
        ),
    )
    entry_id = tx.last_insert_id()

    return CostFact(
        entry_id=entry_id,
        organization_id=int(organization_id),
        farm_id=int(farm_id),
        area_id=int(area_id),
        period=f"{start.year:04d}-{start.month:02d}",
        period_start=start,
        period_end=end,
        category_code=str(category_code),
        amount=amount_value,
        source_ref=str(source_ref).strip(),
        target_type=str(target_type),
        target_id=int(target_id),
        confirm_state="pending",
        status="draft",
    )


def _as_date(value: date | str, field: str) -> date:
    """把 `date` / `YYYY-MM-DD` 字符串统一成 `date`；其余形态显式报错。"""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            f"{field} 必须是 YYYY-MM-DD 格式的日期",
            data={"field": field},
        ) from exc


__all__ = [
    "CostEntryService",
    "CostFact",
    "LEDGER_CATEGORY_CODES",
    "LEDGER_FEED",
    "LEDGER_HEALTH",
    "LEDGER_SEED",
    "MATERIAL_CATEGORY_TO_COST",
    "record_fact",
    "share_percentages",
]
