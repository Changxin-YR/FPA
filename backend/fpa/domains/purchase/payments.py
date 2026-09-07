"""付款单与应付账款的读路径。

## 应付为什么没有 `create` 能力

registry §1.7 只给了 `payable.list` —— 应付**由 `receipt.verify` 生成**
（`purchase.payables.create_from_receipt(...)`，见 `ROLLOUT_CONTRACT.md` §2）。
这与销售侧应收同构（§1.8 也没有 `receivable.create`）：手工建应付会让"应付金额应当
等于已核验到货的金额"这条事实出现第二个来源。

## 余额算一次

`balance = total_amount − paid_amount` 在 `decorate_payable` 里算，而
§4 #4 的 `AmountWithin` 用的也是同一个公式（内核显式声明：
`余额 = balance_column − paid_column`）。**两处算法相同是刻意的**：
列表上显示给用户的余额，与"这一笔还能不能再付"的判断依据，必须是同一个数 ——
否则会出现"页面说还能付 100，提交后说余额只有 80"。
"""

from __future__ import annotations

from typing import Any

from fpa.kernel import capability as cap
from fpa.kernel.errors import DomainError, ErrorCode, not_found
from fpa.kernel.scope import Scope
from fpa.kernel.uow import UnitOfWork

from fpa.domains._base import Page

from .service import (
    PAYMENT_METHODS,
    PURCHASE_PAYABLE_WORKFLOW,
    PURCHASE_PAYMENT_WORKFLOW,
    TABLE_PURCHASE_ORDER,
    TABLE_PURCHASE_PAYABLE,
    TABLE_PURCHASE_PAYMENT,
    assert_row_in_scope,
    decorate_payable,
    decorate_payment,
)

#: 付款列表里显示"这笔付款对应哪张采购单"所需的派生列。
#:
#: 用两级 LEFT JOIN 而不是在 `purchase_payments` 上冗余 `purchase_order_id`：
#: 冗余一列"这张付款属于哪张单"会多出一个需要维护的事实（付款的应付被改动时它
#: 就错了），而本域的表都在同一个库里，JOIN 的成本可以忽略。
#:
#: ★ 第三级 JOIN `business_partners` **不能省**：`Resource(purchase_payment)` 声明了
#:   `("supplier_name", "供应商")` 一列，不 JOIN 就永远取不到值 ⇒ 列表与详情都会渲染成
#:   「—」。`list_payables` 一直是 JOIN 的，只有本常量和 `_payable_scope_row` 漏了，
#:   于是"列表有、详情没有"（实测 `payment.list` / `payment.get` 的 `supplier_name` 为 null）。
_PAYMENT_SELECT = f"""
    SELECT pay.*,
           p.name   AS payable_name,
           p.status AS payable_status,
           o.code   AS order_code,
           o.id     AS purchase_order_id,
           o.supplier_id AS supplier_id,
           s.name   AS supplier_name
    FROM {TABLE_PURCHASE_PAYMENT} AS pay
    LEFT JOIN {TABLE_PURCHASE_PAYABLE} AS p ON p.id = pay.payable_id
    LEFT JOIN {TABLE_PURCHASE_ORDER} AS o ON o.id = p.purchase_order_id
    LEFT JOIN business_partners AS s ON s.id = o.supplier_id
"""


class PurchasePaymentService:
    """付款单与应付账款的读路径。"""

    # -- 内部工具 -------------------------------------------------------------

    @staticmethod
    def _payable_scope_row(tx: UnitOfWork, scope: Scope, payable_id: int) -> dict[str, Any]:
        # 与 `list_payables` 的 SELECT **同形**（同一组派生列）：
        #   * 必须 JOIN `business_partners` 取 `supplier_name` —— 不 JOIN 时
        #     `purchase_payable.get` 的「供应商」列永远是「—」（列表有、详情没有）；
        #   * **不要**再写 `o.supplier_id AS supplier_id`：`p.*` 里已经有 `supplier_id`，
        #     重复列名会被 PyMySQL 的 DictCursor 改写成字面键 `o.supplier_id`
        #     （实测响应里真的多出这个键），而 `supplier_name` 反而缺失。
        row = tx.query_one(
            f"SELECT p.*, o.code AS order_code, s.name AS supplier_name "
            f"FROM {TABLE_PURCHASE_PAYABLE} AS p "
            f"LEFT JOIN {TABLE_PURCHASE_ORDER} AS o ON o.id = p.purchase_order_id "
            f"LEFT JOIN business_partners AS s ON s.id = p.supplier_id "
            "WHERE p.id = %s",
            (payable_id,),
        )
        if row is None:
            raise not_found("应付账款")
        assert_row_in_scope(scope, row, "该应付账款")
        return row

    @staticmethod
    def _payment_scope_row(tx: UnitOfWork, scope: Scope, payment_id: int) -> dict[str, Any]:
        row = tx.query_one(f"{_PAYMENT_SELECT} WHERE pay.id = %s", (payment_id,))
        if row is None:
            raise not_found("付款单")
        assert_row_in_scope(scope, row, "该付款单")
        return row

    @staticmethod
    def _payable_filters(params: dict[str, Any], alias: str = "p") -> tuple[list[str], list[Any]]:
        prefix = f"{alias}." if alias else ""
        where: list[str] = []
        values: list[Any] = []

        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            where.append(f"{prefix}name LIKE %s")
            values.append(f"%{keyword}%")

        status = str(params.get("status") or "").strip()
        if status:
            allowed = [state.code for state in PURCHASE_PAYABLE_WORKFLOW.selectable_states()]
            if status not in allowed:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "状态的取值无效",
                    data={"field": "status", "allowed": allowed},
                )
            where.append(f"{prefix}status = %s")
            values.append(status)

        for field, column in (
            ("purchase_order_id", "purchase_order_id"),
            ("supplier_id", "supplier_id"),
        ):
            value = params.get(field)
            if value in (None, ""):
                continue
            try:
                values.append(int(value))
            except (TypeError, ValueError) as exc:
                raise DomainError(
                    ErrorCode.FIELD_INVALID, "该筛选条件必须是整数", data={"field": field}
                ) from exc
            where.append(f"{prefix}{column} = %s")

        # `only_unpaid` 是 boolean 过滤器而不是让前端自己拼 status ——
        # "未结清"是一个业务口径（unpaid ∪ partial），让每个调用方各拼一次必然漂移。
        if str(params.get("only_unpaid") or "").lower() in ("1", "true", "yes"):
            placeholders = ",".join(["%s"] * len(("unpaid", "partial")))
            where.append(f"{prefix}status IN ({placeholders})")
            values.extend(["unpaid", "partial"])

        return where, values

    @staticmethod
    def _payment_filters(params: dict[str, Any]) -> tuple[list[str], list[Any]]:
        where: list[str] = []
        values: list[Any] = []

        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            where.append("(pay.code LIKE %s OR pay.name LIKE %s)")
            values.extend([f"%{keyword}%", f"%{keyword}%"])

        status = str(params.get("status") or "").strip()
        if status:
            allowed = [state.code for state in PURCHASE_PAYMENT_WORKFLOW.selectable_states()]
            if status not in allowed:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "状态的取值无效",
                    data={"field": "status", "allowed": allowed},
                )
            where.append("pay.status = %s")
            values.append(status)

        method = str(params.get("payment_method") or "").strip()
        if method:
            if method not in PAYMENT_METHODS:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "付款方式的取值无效",
                    data={"field": "payment_method", "allowed": list(PAYMENT_METHODS)},
                )
            where.append("pay.payment_method = %s")
            values.append(method)

        value = params.get("payable_id")
        if value not in (None, ""):
            try:
                values.append(int(value))
            except (TypeError, ValueError) as exc:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "应付来源必须是整数",
                    data={"field": "payable_id"},
                ) from exc
            where.append("pay.payable_id = %s")

        return where, values

    # -- 应付：读 -------------------------------------------------------------

    def list_payables(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        query: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """应付列表（分页）。

        权限码 `finance.payable.view` 与 `purchase.view` **分开**：应付是财务口径，
        能看到采购单的人不一定该看到应付余额（registry §1.7 沿用旧命名）。
        """
        ctx.require("finance.payable.view")
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))

        where, values = self._payable_filters(params, "p")
        scope_fragment, scope_values = scope.where_clause("p")
        if scope_fragment and scope_fragment != "1=1":
            where.append(f"({scope_fragment})")
            values.extend(scope_values)
        clause = " AND ".join(where) if where else "1=1"

        total_row = tx.query_one(
            f"SELECT COUNT(*) AS n FROM {TABLE_PURCHASE_PAYABLE} AS p WHERE {clause}", values
        )
        total = int((total_row or {}).get("n", 0))

        rows = tx.query_all(
            f"""
            SELECT p.*, o.code AS order_code, s.name AS supplier_name
            FROM {TABLE_PURCHASE_PAYABLE} AS p
            LEFT JOIN {TABLE_PURCHASE_ORDER} AS o ON o.id = p.purchase_order_id
            LEFT JOIN business_partners AS s ON s.id = p.supplier_id
            WHERE {clause}
            ORDER BY p.id DESC
            LIMIT %s OFFSET %s
            """,
            [*values, page.size, page.offset],
        )
        return cap.HandlerResult(
            data=page.to_result(
                [decorate_payable(row, ctx.actor.permissions) for row in rows], total
            ),
            message="",
        )

    def get_payable_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        payable_id: int | None = None,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        ctx.require("finance.payable.view")
        raw = (path_params or {}).get("payable_id", payable_id)
        if raw is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "应付详情需要 path_params 传 payable_id")
        row = self._payable_scope_row(tx, scope, int(raw))
        return cap.HandlerResult(
            data={"record": decorate_payable(row, ctx.actor.permissions)},
        )

    def load_payable(
        self, tx: UnitOfWork, *, scope: Scope, record_id: int
    ) -> dict[str, Any] | None:
        """应付的回读函数（`create_from_receipt` 与 `payment.verify` 之后回读用）。

        返回值必须包含 `total_amount` / `paid_amount` / `status` / `currency`：
        `AmountWithin` 的不变量执行依赖这些列。
        """
        row = tx.query_one(
            f"SELECT * FROM {TABLE_PURCHASE_PAYABLE} WHERE id = %s", (record_id,)
        )
        return None if row is None else {**row, "version": int(row.get("row_version") or 1)}

    # -- 付款：读 -------------------------------------------------------------

    def list_payments(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        query: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """付款列表（分页）。"""
        ctx.require("finance.payment.view")
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))

        where, values = self._payment_filters(params)
        scope_fragment, scope_values = scope.where_clause("pay")
        if scope_fragment and scope_fragment != "1=1":
            where.append(f"({scope_fragment})")
            values.extend(scope_values)
        clause = " AND ".join(where) if where else "1=1"

        total_row = tx.query_one(
            f"SELECT COUNT(*) AS n FROM {TABLE_PURCHASE_PAYMENT} AS pay WHERE {clause}", values
        )
        total = int((total_row or {}).get("n", 0))

        rows = tx.query_all(
            f"""
            {_PAYMENT_SELECT}
            WHERE {clause}
            ORDER BY pay.id DESC
            LIMIT %s OFFSET %s
            """,
            [*values, page.size, page.offset],
        )
        return cap.HandlerResult(
            data=page.to_result(
                [
                    decorate_payment(
                        {**row, "supplier_name": row.get("supplier_name")},
                        ctx.actor.permissions,
                    )
                    for row in rows
                ],
                total,
            ),
            message="",
        )

    def get_payment_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        payment_id: int | None = None,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        ctx.require("finance.payment.view")
        raw = (path_params or {}).get("payment_id", payment_id)
        if raw is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "付款详情需要 path_params 传 payment_id")
        payment_id = int(raw)
        row = self._payment_scope_row(tx, scope, payment_id)
        return cap.HandlerResult(
            data={"record": decorate_payment(row, ctx.actor.permissions)},
        )

    def load_payment(
        self, tx: UnitOfWork, *, scope: Scope, record_id: int
    ) -> dict[str, Any] | None:
        """付款的回读函数。

        返回的字段必须包含 `created_by` / `verified_by` ——
        `DistinctActors`（经办人 ≠ 核验人）执行时要从行快照里取这两个列。
        """
        row = tx.query_one(
            f"SELECT * FROM {TABLE_PURCHASE_PAYMENT} WHERE id = %s", (record_id,)
        )
        return None if row is None else {**row, "version": int(row.get("row_version") or 1)}


__all__ = ["PurchasePaymentService"]
