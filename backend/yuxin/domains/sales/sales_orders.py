"""销售单的读路径：列表 / 详情 / 回读。

与 `PondService` / `PurchaseOrderService` / `CostEntryService` **同构**：
`_scope_row` 做第三层防御，`load_order` 是执行器回读用的函数
（`docs/WRITE_CONTRACT.md` 规则 2：``executed`` 的含义是"读回来的行确实是我们要的
样子"，不是"我们调用了 INSERT"）。这个同构是刻意的——四个域的读路径形状一样，
读代码的人不需要为每个域重新建立心智模型。

## §2.9 的 6 个派生列：一个都不进表

registry §2.9 明确列出销售单的派生列：

    customer_name / pond_name / batch_code / delivered_quantity / total_amount / balance

它们全部来自旧前端 `SalePage.vue:175`（**列名必须原样保留**，改名会让旧前端的
语义全部失效）。本版把它们**算出来**而不是存下来：

| 派生列 | 来源（**经表所有者的具名入口**，见下） |
|---|---|
| `customer_name` | `master_data.lookup_partners`（读 `business_partners`） |
| `pond_name` | `master_data.lookup_ponds`（读 `ponds`） |
| `batch_code` | `production.lookup_batches`（读 `production_batches`） |
| `total_amount` | `quantity × unit_price`（本表两列） |
| `delivered_quantity` | 相关子查询 `SUM(deliveries.quantity WHERE status='verified')`（本域） |
| `balance` | `quantity − delivered_quantity` |

早期版本把其中若干冗余存进表，再靠三处回查保持同步——那是"两处描述同一件事"的
教科书形态（`docs/DEVELOPMENT.md` §4），也是本次重构要根除的对象。

## 为什么三个跨域列走**具名入口**而不是 `LEFT JOIN`

`ROLLOUT_CONTRACT.md` §2.0 是硬规则：

> **当一个域需要另一个域的表的数据时，由「表的所有者」提供具名只读函数；
> 调用方需要 N 行时，所有者提供批量形态（`lookup_*(ids)`）。
> 调用方不得直读，也不得要求「登记为 § 2 的例外」。**

它给的理由正好命中本文件：**列表渲染是 schema 耦合最容易静默积累的位置**——
production 改一个列名，本文件的 `LEFT JOIN` 照旧能跑、只是渲染错，
而两侧的单域 e2e 全绿。所以"为了省一次查询"不能成为直读的理由。

批量入口让整页只多发 **2 次**查询（批次 + 塘口），与行数无关 —— 不是 N+1。

## 为什么"已交付量"用相关子查询而不是 JOIN + GROUP BY

一张销售单对应多次交付。`JOIN deliveries` 会让销售单行**被放大**成多行，
于是 `COUNT(*)` 算出的分页总数偏大、`LIMIT` 也会切错位置——而这类错误在
"每单只交付一次"的测试数据上**完全看不出来**。相关子查询不会放大行数。

这与采购域的 `_UNPAID_SUBQUERY` 是同一个理由（`domains/purchase/orders.py:48-58`）。
"""

from __future__ import annotations

from typing import Any

from yuxin.kernel import capability as cap
from yuxin.kernel.errors import DomainError, ErrorCode, not_found, validation
from yuxin.kernel.scope import Scope
from yuxin.kernel.uow import UnitOfWork

from yuxin.domains._base import Page

from .service import (
    DELIVERY_WORKFLOW,
    RECEIVABLE_WORKFLOW,
    SALES_ORDER_WORKFLOW,
    TABLE_DELIVERY,
    TABLE_RECEIVABLE,
    TABLE_SALES_ORDER,
    TABLE_SALES_RECEIPT,
    assert_row_in_scope,
    decorate_delivery,
    decorate_receivable,
    decorate_receipt,
    decorate_sales_order,
)

#: "已交付数量"的聚合子查询。
#:
#: `status='verified'` 与 §4 #10 的 `CumulativeWithin` 用**同一套状态**：
#: 只有已核验的交付才占用销售单的额度。两者若用了不同的状态集，
#: "列表里显示的已交付量"与"不变量判定的累计量"就会不一致——
#: 而用户看到的数字与系统据此拒绝的理由必须是同一个。
#:
#: 为什么显式写 `d.status = 'verified'` 而不是 `d.status <> 'cancelled'`：
#: registry §4 #10 的原文要求"只放**算数**的状态"，用排除集会在新增终止态时
#: 静默把作废单据算进去（`CumulativeWithin` 的 docstring 对这一点有同样的论证）。
_DELIVERED_SUBQUERY = (
    "COALESCE((SELECT SUM(d.quantity) "
    f"FROM {TABLE_DELIVERY} AS d "
    "WHERE d.sales_order_id = o.id AND d.status = 'verified'), 0)"
)

#: "已收款金额"的聚合子查询（详情用）。
#:
#: 一张销售单可能对应多条应收（每次交付生成一条），所以要 `SUM` 而不是取一条。
_RECEIVED_SUBQUERY = (
    "COALESCE((SELECT SUM(r.paid_amount) "
    f"FROM {TABLE_RECEIVABLE} AS r "
    "WHERE r.sales_order_id = o.id), 0)"
)


def _enrich_order(tx: UnitOfWork, row: dict[str, Any]) -> dict[str, Any]:
    """补齐 §2.9 的三个**跨域**派生列：`customer_name` / `pond_name` / `batch_code`。

    ## 为什么这么做，而不是 `LEFT JOIN`

    `ROLLOUT_CONTRACT.md` §2.0 是硬规则：

    > **当一个域需要另一个域的表的数据时，由「表的所有者」提供具名只读函数；
    > 调用方需要 N 行时，所有者提供批量形态（`lookup_*(ids)`）。
    > 调用方不得直读，也不得要求「登记为 § 2 的例外」。**

    而它给的理由正好命中这里的场景：**列表渲染是 schema 耦合最容易静默积累的位置**
    （production 改一个列名，sales 的 `LEFT JOIN` 照旧能跑、只是渲染错，而两侧的
    单域 e2e 全绿）。所以"为了省一次查询"不能成为直读的理由。

    ## 补齐失败时**留空而不是报错**

    所有者明确约定"缺失 id 被略去 / 返回 `None`"（§2.0 取舍 1：*所有者提供事实、
    调用方决定语义*）。这里的语义是：**列表页不能因为一个已删除的塘口而整页 500**。
    所以缺哪一列就留哪一列（前端显示为空白），而不是 raise。这与会话"读路径容忍
    未知值、写路径 fail-closed"的口径一致（见 `service.py::receipt_method_label`）。

    ## 一次批量调用，不是 N 次单行查询

    `lookup_batches` / `lookup_ponds` 收 id 列表返回 `dict[id, row]`，所以整页
    只多发 **2 次**查询（批次 + 塘口），与行数无关。逐行单查会变成 N+1。
    """
    from yuxin.domains.master_data.lookup import lookup_partners, lookup_ponds
    from yuxin.domains.production.service import lookup_batches

    enriched = dict(row)

    # ① 批次（production 域）
    batch_ids = {int(row["batch_id"])} if row.get("batch_id") else set()
    if batch_ids:
        batches = lookup_batches(tx, batch_ids=batch_ids)
        batch = batches.get(int(row["batch_id"]))
        if batch:
            enriched["batch_code"] = batch.get("code")

    # ② 塘口（master_data 域）
    pond_ids = {int(row["pond_id"])} if row.get("pond_id") else set()
    if pond_ids:
        ponds = lookup_ponds(tx, pond_ids=pond_ids)
        pond = ponds.get(int(row["pond_id"]))
        if pond:
            enriched["pond_name"] = pond.get("name")

    # ③ 客户（master_data 域）
    partner_ids = {int(row["customer_id"])} if row.get("customer_id") else set()
    if partner_ids:
        partners = lookup_partners(tx, partner_ids=partner_ids)
        partner = partners.get(int(row["customer_id"]))
        if partner:
            enriched["customer_name"] = partner.get("name")

    return enriched


def _enrich_delivery(tx: UnitOfWork, row: dict[str, Any]) -> dict[str, Any]:
    """补齐交付单的跨域派生列 `harvest_code`（`harvests` 属于 production 域）。

    与 `_enrich_order` 同一手法、同一理由（§2.0）。缺行时**留空**而不是报错：
    列表页不该因为一张已删除的出塘单整页 500。
    """
    from yuxin.domains.production.service import lookup_harvest

    enriched = dict(row)
    harvest_id = row.get("harvest_document_id")
    if harvest_id:
        harvest = lookup_harvest(tx, harvest_id=int(harvest_id))
        if harvest:
            enriched["harvest_code"] = harvest.get("code")
    return enriched


def _enrich_receivable(tx: UnitOfWork, row: dict[str, Any]) -> dict[str, Any]:
    """补齐应收的跨域派生列 `customer_name`（`business_partners` 属于 master_data）。"""
    from yuxin.domains.master_data.lookup import lookup_partners

    enriched = dict(row)
    customer_id = row.get("customer_id")
    if customer_id:
        partners = lookup_partners(tx, partner_ids={int(customer_id)})
        partner = partners.get(int(customer_id))
        if partner:
            enriched["customer_name"] = partner.get("name")
    return enriched


class SalesOrderService:
    """销售单的读路径。

    只读。写路径在 `sales_orders_write.py` 里继承本类，以复用 `_scope_row` /
    `load_order` / 派生列逻辑 —— 读写共用同一套范围校验与派生口径，避免
    "读看到一个样、写看到另一个样"（早期版本 `master_data_store.py` 的真实问题：
    读路径与写路径各自拼 scope 条件）。
    """

    # -- 内部工具 -------------------------------------------------------------

    @staticmethod
    def _scope_row(tx: UnitOfWork, scope: Scope, order_id: int) -> dict[str, Any]:
        """按主键取一行，**并校验它在数据范围内**。

        这是第三层防御里最关键的一步：它校验的不是"令牌对不对"，而是
        **这一行数据属不属于当前用户**。两种校验的失效模式不同——第二层被绕过时
        （例如将来某个新入口忘了走 Gateway），这一层仍然拦得住，因为它看的是数据本身。
        """
        row = tx.query_one(
            f"SELECT o.* FROM {TABLE_SALES_ORDER} AS o WHERE o.id = %s",
            (int(order_id),),
        )
        if row is None:
            raise not_found("销售单")
        assert_row_in_scope(scope, row, "该销售单")
        return row

    @staticmethod
    def _filters(params: dict[str, Any], alias: str = "o") -> tuple[list[str], list[Any]]:
        """把查询参数翻成 WHERE 片段。

        ## 别名是参数，不是"渲染完再改字符串"

        列限定符是查询的一部分。若先渲染片段再 `str.replace` 补前缀，就要靠
        "替换规则"与"片段写法"永远保持一致——而片段写法随时会被下一个人改。

        ## `status` 为什么要做取值白名单

        非法值报 `FIELD_INVALID`，而不是"过滤出 0 行"。**静默返回空集**是早期版本
        `data_scope.py` 的经典缺陷（`return "1=0", []`）：用户看到 0 行却不报错，
        会以为是"确实没有数据"，而不是"参数写错了"。
        """
        prefix = f"{alias}." if alias else ""
        where: list[str] = ["1=1"]
        values: list[Any] = []

        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            where.append(f"({prefix}code LIKE %s OR {prefix}name LIKE %s)")
            values.extend([f"%{keyword}%", f"%{keyword}%"])

        status = str(params.get("status") or "").strip()
        if status:
            allowed = [state.code for state in SALES_ORDER_WORKFLOW.selectable_states()]
            if status not in allowed:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "状态的取值无效",
                    data={"field": "status", "allowed": allowed},
                )
            where.append(f"{prefix}status = %s")
            values.append(status)

        for field, column in (
            ("customer_id", "customer_id"),
            ("pond_id", "pond_id"),
            ("batch_id", "batch_id"),
        ):
            value = params.get(field)
            if value in (None, ""):
                continue
            try:
                values.append(int(value))
            except (TypeError, ValueError) as exc:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "该筛选条件必须是整数",
                    data={"field": field},
                ) from exc
            where.append(f"{prefix}{column} = %s")

        for field, column, op in (
            ("sold_from", "sold_at", ">="),
            ("sold_to", "sold_at", "<="),
            ("due_from", "due_date", ">="),
            ("due_to", "due_date", "<="),
        ):
            value = params.get(field)
            if value in (None, ""):
                continue
            where.append(f"{prefix}{column} {op} %s")
            values.append(value)

        return where, values

    # -- 读 -------------------------------------------------------------------

    def list_orders(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        query: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """销售单列表。

        列出的字段与 `Resource.columns` 一一对应（前端按元数据渲染，不硬编码列）。
        """
        ctx.require("sales.view")
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))

        where, values = self._filters(params, "o")
        scope_fragment, scope_values = scope.where_clause("o")
        if scope_fragment and scope_fragment != "1=1":
            where.append(f"({scope_fragment})")
            values.extend(scope_values)

        clause = " AND ".join(where)
        total_row = tx.query_one(
            f"SELECT COUNT(*) AS n FROM {TABLE_SALES_ORDER} AS o WHERE {clause}",
            values,
        )
        total = int((total_row or {}).get("n", 0))

        # ★ 只查**本域**的表。跨域的名称/编码由表所有者的具名只读函数补齐
        # （`ROLLOUT_CONTRACT.md` §2.0：调用方不得直读，批量形态用于列表渲染）。
        # 原来的 `LEFT JOIN ponds / production_batches / business_partners` 已移除 ——
        # 那些表分别属于 master_data 与 production。
        rows = tx.query_all(
            f"""
            SELECT o.*, {_DELIVERED_SUBQUERY} AS delivered_quantity
            FROM {TABLE_SALES_ORDER} AS o
            WHERE {clause}
            ORDER BY o.id DESC
            LIMIT %s OFFSET %s
            """,
            [*values, page.size, page.offset],
        )
        enriched = [_enrich_order(tx, dict(row)) for row in rows]
        return cap.HandlerResult(
            data=page.to_result(
                [self._present(row, ctx.actor.permissions) for row in enriched], total
            ),
            message="",
        )

    def get_order_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """销售单详情。

        额外带上累计交付与已收款（便于显示余额）以及 `available_transitions`
        （从当前状态出发的合法目标）—— 与 `purchase_order.get` 同形。
        """
        ctx.require("sales.view")
        # 与 `purchase_order.get` 同形：读能力的路径参数以**一个 dict** 进来。
        raw = (path_params or {}).get("order_id")
        if raw is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "销售单详情需要 path_params 传 order_id",
            )
        order_id = int(raw)
        row = self._scope_row(tx, scope, order_id)
        detail = tx.query_one(
            f"""
            SELECT o.*,
                   {_DELIVERED_SUBQUERY} AS delivered_quantity,
                   {_RECEIVED_SUBQUERY}   AS received_amount
            FROM {TABLE_SALES_ORDER} AS o
            WHERE o.id = %s
            """,
            (int(order_id),),
        )
        assert detail is not None  # `_scope_row` 刚刚读到过同一行
        record = self._present(_enrich_order(tx, dict(detail)), ctx.actor.permissions)
        # 与 `purchase_order.get` **同形**：详情要给出"从**当前**状态出发的合法目标"，
        # 前端据此渲染动作。静态声明只能给全集，用户就能选到非法目标 —— 而"服务端按
        # 转移表过滤候选值"正是详情相对列表响应的唯一增量（也是这次登记 `*.get` 的理由）。
        record["available_transitions"] = [
            {"value": state.code, "label": state.label}
            for state in SALES_ORDER_WORKFLOW.available_transitions(str(detail["status"]))
        ]
        # ⚠️ **读能力不带 `resource_id`**：`web/app.py::_ok` 见到它就会把响应再包一层
        # （`data = {resource_id, record: result.data}`），于是 `data.record.record` ——
        # 前端按契约读 `data.record.code` 得到 `undefined`，**每个格子显示「—」且不报错**。
        # 回读校验（`_reload_after`）对读能力本来就直接返回 None，所以这里不需要它。
        return cap.HandlerResult(data={"record": record})

    @staticmethod
    def load_order(
        tx: UnitOfWork, *, scope: Scope, record_id: int
    ) -> dict[str, Any] | None:
        """回读函数。执行器在提交前调它确认"写入真的落了库"。

        刻意**只读本表**、不做 JOIN：回读的用途是"确认那一行是我们要的样子"，
        而 JOIN 引入的派生列会让回读结果依赖别的表的状态——那些状态可能在这一
        瞬间刚好被别人改过，于是回读会因为**无关的**原因失败。
        派生列交给 `get_order_by_id`（那是给用户看的），不交给回读。
        """
        row = tx.query_one(
            f"SELECT o.* FROM {TABLE_SALES_ORDER} AS o WHERE o.id = %s",
            (int(record_id),),
        )
        if row is None:
            return None
        present = decorate_sales_order(row, frozenset())
        # 回读结果必须带 `row_version` 的规则名 `version`（`OptimisticLock`
        # 与审计都要用它），这一点由 `decorate_sales_order` 保证。
        return present

    # -- 展示 ----------------------------------------------------------------

    @staticmethod
    def _present(row: dict[str, Any], permissions: frozenset[str]) -> dict[str, Any]:
        """派生 `total_amount` / `balance` 并统一转成可 JSON 化的形态。

        ## 两处量化（t10 修的真实缺陷）

        原先这里直接 `str(quantity * unit_price)`，于是界面上看到
        **`total_amount = '100.0000000'`** —— 7 位小数。

        根因不是"某列设错了"，而是**乘法把两个不同精度的列相乘**：
        `quantity` 是 `DECIMAL(16,3)`、`unit_price` 是 `DECIMAL(14,4)`，乘出来自然是
        7 位。而它是**派生值**，没有列定义帮它兜住，必须由这里按"金额 2 位"的规则
        量化（口径见 `kernel/money.py` —— 全系统唯一一处）。

        `balance` 是**数量**（`quantity - delivered_quantity`，单位是 kg/jin/tail，
        **不是钱**），所以它按"数量 3 位"量化 —— 用金额规则会把 `100.000` 变成
        `100.00`，让"未交付量"与"已交付量"位数不一致，看着像少算了。
        """
        from decimal import Decimal

        from yuxin.kernel.money import money
        from yuxin.kernel.money import quantity as quantize_quantity

        presented = dict(row)

        quantity = row.get("quantity")
        unit_price = row.get("unit_price")
        if quantity is not None and unit_price is not None:
            presented["total_amount"] = str(
                money(Decimal(str(quantity)) * Decimal(str(unit_price)))
            )

        delivered = row.get("delivered_quantity")
        if quantity is not None and delivered is not None:
            presented["balance"] = str(
                quantize_quantity(Decimal(str(quantity)) - Decimal(str(delivered)))
            )

        return decorate_sales_order(presented, permissions)


# ---------------------------------------------------------------------------
# 交付单 / 应收 / 收款单的读路径
# ---------------------------------------------------------------------------
#
# 三个资源放在同一个文件里：它们的读路径形状完全相同（_scope_row + 列表 + 详情 +
# 回读），而各自的派生列很少。分成三个文件会让"三个几乎一样的 _scope_row"分散在
# 三处，改动时容易只改两处。当某个资源的读逻辑长到有独立关注点时再拆出去。


class DeliveryService:
    """交付单的读路径。"""

    @staticmethod
    def _scope_row(tx: UnitOfWork, scope: Scope, delivery_id: int) -> dict[str, Any]:
        row = tx.query_one(
            f"SELECT d.* FROM {TABLE_DELIVERY} AS d WHERE d.id = %s",
            (int(delivery_id),),
        )
        if row is None:
            raise not_found("交付单")
        assert_row_in_scope(scope, row, "该交付单")
        return row

    def list_deliveries(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        query: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """交付单列表。"""
        ctx.require("sales.view")
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))

        where: list[str] = ["1=1"]
        values: list[Any] = []
        scope_fragment, scope_values = scope.where_clause("d")
        if scope_fragment and scope_fragment != "1=1":
            where.append(f"({scope_fragment})")
            values.extend(scope_values)

        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            where.append("(d.code LIKE %s OR d.name LIKE %s)")
            values.extend([f"%{keyword}%", f"%{keyword}%"])

        status = str(params.get("status") or "").strip()
        if status:
            allowed = [state.code for state in DELIVERY_WORKFLOW.selectable_states()]
            if status not in allowed:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "状态的取值无效",
                    data={"field": "status", "allowed": allowed},
                )
            where.append("d.status = %s")
            values.append(status)

        for field, column in (("sales_order_id", "sales_order_id"), ("pond_id", "pond_id")):
            value = params.get(field)
            if value in (None, ""):
                continue
            try:
                values.append(int(value))
            except (TypeError, ValueError) as exc:
                raise DomainError(
                    ErrorCode.FIELD_INVALID, "该筛选条件必须是整数", data={"field": field}
                ) from exc
            where.append(f"d.{column} = %s")

        clause = " AND ".join(where)
        total_row = tx.query_one(
            f"SELECT COUNT(*) AS n FROM {TABLE_DELIVERY} AS d WHERE {clause}", values
        )
        total = int((total_row or {}).get("n", 0))

        # `harvests` 属于 production 域：按 §2.0 用它的具名只读入口补齐
        # `harvest_code`，不再 `LEFT JOIN` 别人的表。
        rows = tx.query_all(
            f"""
            SELECT d.*, o.code AS order_code
            FROM {TABLE_DELIVERY} AS d
            LEFT JOIN {TABLE_SALES_ORDER} AS o ON o.id = d.sales_order_id
            WHERE {clause}
            ORDER BY d.id DESC
            LIMIT %s OFFSET %s
            """,
            [*values, page.size, page.offset],
        )
        enriched = [_enrich_delivery(tx, dict(row)) for row in rows]
        return cap.HandlerResult(
            data=page.to_result(
                [decorate_delivery(row, ctx.actor.permissions) for row in enriched],
                total,
            ),
            message="",
        )

    def _delivery_detail(
        self, tx: UnitOfWork, ctx, scope: Scope, delivery_id: int
    ) -> dict[str, Any]:
        """拼装一条交付单的详情记录。**刻意不做权限检查。**

        ## 为什么必须与 `get_delivery_by_id` 分开

        本函数被**读写两条路径**共用：
          * 能力入口 `get_delivery_by_id` —— 自己 `ctx.require("sales.view")`；
          * 写路径 `delivery.create` / `delivery.verify` —— 复用本函数拼响应。

        它们原先都走 `self.get_delivery_by_id(...)`，于是写能力**偷偷多要一个读权限**：
        `delivery.verify` 声明的是 `sales.verify`，实际执行却因为内调读路径而要求
        `sales.view` —— 只持有声明权限的账号会拿到 403，而**工具/按钮仍然可见**
        （L1 过滤按 `required_permission` 判定），实测 `qc` 角色就是这样必 403 的。
        写路径已经各自 `ctx.require(...)` 过自己的权限，这里不该再叠一层。
        """
        row = self._scope_row(tx, scope, delivery_id)
        detail = tx.query_one(
            f"""
            SELECT d.*, o.code AS order_code
            FROM {TABLE_DELIVERY} AS d
            LEFT JOIN {TABLE_SALES_ORDER} AS o ON o.id = d.sales_order_id
            WHERE d.id = %s
            """,
            (delivery_id,),
        )
        assert detail is not None
        return decorate_delivery(_enrich_delivery(tx, dict(detail)), ctx.actor.permissions)

    def get_delivery_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        delivery_id: int | None = None,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        ctx.require("sales.view")
        raw = (path_params or {}).get("delivery_id", delivery_id)
        if raw is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "交付详情需要 path_params 传 delivery_id")
        return cap.HandlerResult(
            data={"record": self._delivery_detail(tx, ctx, scope, int(raw))},
        )

    @staticmethod
    def load_delivery(
        tx: UnitOfWork, *, scope: Scope, record_id: int
    ) -> dict[str, Any] | None:
        row = tx.query_one(
            f"SELECT d.* FROM {TABLE_DELIVERY} AS d WHERE d.id = %s", (int(record_id),)
        )
        if row is None:
            return None
        return decorate_delivery(row, frozenset())


class ReceivableService:
    """应收的读路径。

    **只读**：应收由 `delivery.verify` 生成（registry §2.9 只有 `receivable.list`，
    §1.8 里没有 `receivable.create`），它的状态由 `sales_receipt.verify` 内部推进。
    所以本类没有写方法，也没有对应的 `*_write.py`。
    """

    @staticmethod
    def _scope_row(tx: UnitOfWork, scope: Scope, receivable_id: int) -> dict[str, Any]:
        row = tx.query_one(
            f"SELECT r.* FROM {TABLE_RECEIVABLE} AS r WHERE r.id = %s",
            (int(receivable_id),),
        )
        if row is None:
            raise not_found("应收账款")
        assert_row_in_scope(scope, row, "该应收账款")
        return row

    def list_receivables(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        query: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """应收列表。

        权限码是 `finance.receivable.view`（**不是** `sales.view`）——§1.8 明确给
        应收用了独立的 finance 码，因为"看销售数据"与"看应收账款"在真实组织里
        常常是两个人。
        """
        ctx.require("finance.receivable.view")
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))

        where: list[str] = ["1=1"]
        values: list[Any] = []
        scope_fragment, scope_values = scope.where_clause("r")
        if scope_fragment and scope_fragment != "1=1":
            where.append(f"({scope_fragment})")
            values.extend(scope_values)

        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            where.append("(r.code LIKE %s OR r.name LIKE %s)")
            values.extend([f"%{keyword}%", f"%{keyword}%"])

        status = str(params.get("status") or "").strip()
        if status:
            # 应收的 `overpaid` / `bad_debt` 是“不可达但保留在 CHECK 里”的值（见
            # `RECEIVABLE_WORKFLOW` 的注释），按它们筛选只会得到空集。
            allowed = [state.code for state in RECEIVABLE_WORKFLOW.selectable_states()]
            if status not in allowed:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "状态的取值无效",
                    data={"field": "status", "allowed": allowed},
                )
            where.append("r.status = %s")
            values.append(status)

        if params.get("customer_id") not in (None, ""):
            try:
                values.append(int(params["customer_id"]))
            except (TypeError, ValueError) as exc:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "该筛选条件必须是整数",
                    data={"field": "customer_id"},
                ) from exc
            where.append("r.customer_id = %s")

        # 逾期筛选：只有未结清且已过期才算。**服务端算**，不让前端比日期——
        # 前端比日期会受浏览器时钟影响，而"逾期"是财务口径。
        if params.get("overdue") in (1, "1", True, "true"):
            where.append("r.due_date < CURDATE() AND r.status IN ('unpaid','partial')")

        clause = " AND ".join(where)
        total_row = tx.query_one(
            f"SELECT COUNT(*) AS n FROM {TABLE_RECEIVABLE} AS r WHERE {clause}", values
        )
        total = int((total_row or {}).get("n", 0))

        rows = tx.query_all(
            f"""
            SELECT r.*, o.code AS order_code
            FROM {TABLE_RECEIVABLE} AS r
            LEFT JOIN {TABLE_SALES_ORDER} AS o ON o.id = r.sales_order_id
            WHERE {clause}
            ORDER BY r.id DESC
            LIMIT %s OFFSET %s
            """,
            [*values, page.size, page.offset],
        )
        enriched = [_enrich_receivable(tx, dict(row)) for row in rows]
        return cap.HandlerResult(
            data=page.to_result(
                [decorate_receivable(row, ctx.actor.permissions) for row in enriched],
                total,
            ),
            message="",
        )

    def get_receivable_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        receivable_id: int | None = None,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        ctx.require("finance.receivable.view")
        raw = (path_params or {}).get("receivable_id", receivable_id)
        if raw is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "应收详情需要 path_params 传 receivable_id")
        receivable_id = int(raw)
        row = self._scope_row(tx, scope, receivable_id)
        detail = tx.query_one(
            f"""
            SELECT r.*, o.code AS order_code
            FROM {TABLE_RECEIVABLE} AS r
            LEFT JOIN {TABLE_SALES_ORDER} AS o ON o.id = r.sales_order_id
            WHERE r.id = %s
            """,
            (receivable_id,),
        )
        assert detail is not None
        return cap.HandlerResult(
            data={
                "record": decorate_receivable(
                    _enrich_receivable(tx, dict(detail)), ctx.actor.permissions
                )
            },
        )

    @staticmethod
    def load_receivable(
        tx: UnitOfWork, *, scope: Scope, record_id: int
    ) -> dict[str, Any] | None:
        row = tx.query_one(
            f"SELECT r.* FROM {TABLE_RECEIVABLE} AS r WHERE r.id = %s", (int(record_id),)
        )
        if row is None:
            return None
        return decorate_receivable(row, frozenset())


class SalesReceiptService:
    """收款单的读路径。"""

    @staticmethod
    def _scope_row(tx: UnitOfWork, scope: Scope, receipt_id: int) -> dict[str, Any]:
        row = tx.query_one(
            f"SELECT s.* FROM {TABLE_SALES_RECEIPT} AS s WHERE s.id = %s", (int(receipt_id),)
        )
        if row is None:
            raise not_found("收款单")
        assert_row_in_scope(scope, row, "该收款单")
        return row

    def list_receipts(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        query: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """收款单列表。"""
        ctx.require("finance.receipt.manage")
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))

        where: list[str] = ["1=1"]
        values: list[Any] = []
        scope_fragment, scope_values = scope.where_clause("s")
        if scope_fragment and scope_fragment != "1=1":
            where.append(f"({scope_fragment})")
            values.extend(scope_values)

        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            where.append("(s.code LIKE %s OR s.name LIKE %s)")
            values.extend([f"%{keyword}%", f"%{keyword}%"])

        if params.get("receivable_id") not in (None, ""):
            try:
                values.append(int(params["receivable_id"]))
            except (TypeError, ValueError) as exc:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "该筛选条件必须是整数",
                    data={"field": "receivable_id"},
                ) from exc
            where.append("s.receivable_id = %s")

        clause = " AND ".join(where)
        total_row = tx.query_one(
            "SELECT COUNT(*) AS n FROM " + TABLE_SALES_RECEIPT + " AS s WHERE " + clause, values
        )
        total = int((total_row or {}).get("n", 0))

        rows = tx.query_all(
            f"""
            SELECT s.*, r.code AS receivable_code
            FROM {TABLE_SALES_RECEIPT} AS s
            LEFT JOIN {TABLE_RECEIVABLE} AS r ON r.id = s.receivable_id
            WHERE {clause}
            ORDER BY s.id DESC
            LIMIT %s OFFSET %s
            """,
            [*values, page.size, page.offset],
        )
        return cap.HandlerResult(
            data=page.to_result(
                [decorate_receipt(row, ctx.actor.permissions) for row in rows], total
            ),
            message="",
        )

    def _receipt_detail(
        self, tx: UnitOfWork, ctx, scope: Scope, receipt_id: int
    ) -> dict[str, Any]:
        """拼装一条收款单的详情记录。**刻意不做权限检查**（理由同 `_delivery_detail`）。

        `sales_receipt.verify` 声明的是 `finance.receipt.verify`，但它原先内调
        `get_receipt_by_id`（要求 `finance.receipt.manage`）⇒ 只持有核验权限的
        账号必 403。写路径各自已 `ctx.require(...)` 过自己的权限。
        """
        row = self._scope_row(tx, scope, receipt_id)
        detail = tx.query_one(
            f"""
            SELECT s.*, r.code AS receivable_code
            FROM {TABLE_SALES_RECEIPT} AS s
            LEFT JOIN {TABLE_RECEIVABLE} AS r ON r.id = s.receivable_id
            WHERE s.id = %s
            """,
            (int(receipt_id),),
        )
        assert detail is not None
        return decorate_receipt(detail, ctx.actor.permissions)

    def get_receipt_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        receipt_id: int | None = None,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        ctx.require("finance.receipt.manage")
        raw = (path_params or {}).get("receipt_id", receipt_id)
        if raw is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "收款详情需要 path_params 传 receipt_id")
        return cap.HandlerResult(
            data={"record": self._receipt_detail(tx, ctx, scope, int(raw))},
        )

    @staticmethod
    def load_receipt(
        tx: UnitOfWork, *, scope: Scope, record_id: int
    ) -> dict[str, Any] | None:
        row = tx.query_one(
            f"SELECT s.* FROM {TABLE_SALES_RECEIPT} AS s WHERE s.id = %s", (int(record_id),)
        )
        if row is None:
            return None
        return decorate_receipt(row, frozenset())


__all__ = [
    "DeliveryService",
    "ReceivableService",
    "SalesOrderService",
    "SalesReceiptService",
]
