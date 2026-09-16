"""采购单的读路径：列表、详情、回读。

## 派生列全部**算出来**，一个都不存

registry §2.8 明确要求列表里有这 6 个派生列（它们来自旧前端 `PurchasePage.vue:149`，
必须原样保留）：

    supplier_name / material_name / warehouse_name / total_amount /
    received_quantity / unpaid_amount

早期版本把它们**冗余存进表**，然后靠三处回查保持同步 —— 那是本项目要删掉的
"两处描述同一件事"（`DEVELOPMENT.md` §4）。新系统一律在查询里 JOIN / 聚合得到：

    total_amount      = quantity × unit_price           （本表两列，服务端算）
    supplier_name     = JOIN business_partners          （003 的往来单位）
    material_name     = JOIN materials                  （003 的物料）
    warehouse_name    = JOIN warehouse 域的表 —— **等 t16 权威表名表**
    received_quantity = SUM(到货明细)                   —— 等 t16
    unpaid_amount     = SUM(total_amount − paid_amount) FROM purchase_payables（本域）

**其中两列依赖跨域表名**，而 `ROLLOUT_CONTRACT.md` §2 的权威表名表（t16）还没落。
按 负责人 的裁决"不猜默认值"：这里**显式降级**为 `NULL` 而不是编一个表名去查 ——
查一张猜错名的表会抛 "table doesn't exist"，把"还没定"伪装成"实现 bug"；
返回 NULL 则让前端渲染空白单元格，且本文件里注释与 todo 都写明原因。
`unpaid_amount` **不依赖任何跨域表**，所以它现在就给出真实值。
"""

from __future__ import annotations

from typing import Any

from yuxin.kernel import capability as cap
from yuxin.kernel.errors import DomainError, ErrorCode, not_found
from yuxin.kernel.fields import f_ref
from yuxin.kernel.scope import Scope
from yuxin.kernel.uow import UnitOfWork

from yuxin.domains._base import Page

from .service import (
    PURCHASE_ORDER_WORKFLOW,
    TABLE_PURCHASE_ORDER,
    TABLE_PURCHASE_PAYABLE,
    assert_row_in_scope,
    decorate_order,
)

#: 列表查询里"未付金额"的聚合子查询。
#:
#: 为什么不冗余一列 `unpaid_amount`：它是**可算的**（应付总额 − 已付），
#: 而冗余列必须靠回查保持同步。这里用相关子查询而不是 JOIN + GROUP BY：
#: 一张采购单对应多条应付，JOIN 会让采购单行被放大，分页总数会算错 ——
#: 这类错误在"每单只有一条应付"的测试数据上看不出来。
_UNPAID_SUBQUERY = (
    "COALESCE((SELECT SUM(p.total_amount - p.paid_amount) "
    f"FROM {TABLE_PURCHASE_PAYABLE} AS p "
    "WHERE p.purchase_order_id = o.id AND p.status <> 'settled'), 0)"
)


class PurchaseOrderService:
    """采购单的读路径。

    与 `PondService` / `CostEntryService` 同构：`_scope_row` 做第三层防御，
    `load_order` 是执行器回读用的函数（`docs/WRITE_CONTRACT.md` 规则 2）。
    """

    # -- 内部工具 -------------------------------------------------------------

    @staticmethod
    def _scope_row(tx: UnitOfWork, scope: Scope, order_id: int) -> dict[str, Any]:
        """按主键取一行，**并校验它在数据范围内**。

        这是第三层防御里最关键的一步：它校验的不是"令牌对不对"，而是
        **这一行数据属不属于当前用户**。
        """
        row = tx.query_one(
            f"SELECT o.* FROM {TABLE_PURCHASE_ORDER} AS o WHERE o.id = %s",
            (order_id,),
        )
        if row is None:
            raise not_found("采购单")
        assert_row_in_scope(scope, row, "该采购单")
        return row

    @staticmethod
    def _filters(params: dict[str, Any], alias: str = "o") -> tuple[list[str], list[Any]]:
        """把查询参数翻成 WHERE 片段。

        ## 别名是参数，不是"渲染完再改字符串"

        列限定符是查询的一部分，不是字符串装饰。若先渲染片段再 `str.replace` 补前缀，
        就要靠"替换规则"与"片段写法"永远保持一致 —— 而片段写法随时会被下一个人改。

        ## 白名单为什么必需

        `status` 做取值白名单校验：非法值报 `FIELD_INVALID`，而不是"过滤出 0 行"。
        **静默返回空集**是早期版本 `data_scope.py` 的经典缺陷（`return "1=0", []`）——
        用户看到 0 行却不报错，会以为是"确实没有数据"而不是"参数写错了"。
        """
        prefix = f"{alias}." if alias else ""
        where: list[str] = []
        values: list[Any] = []

        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            where.append(f"({prefix}code LIKE %s OR {prefix}name LIKE %s)")
            values.extend([f"%{keyword}%", f"%{keyword}%"])

        status = str(params.get("status") or "").strip()
        if status:
            # 只允许可选状态（见 `State.selectable`）：`closed` 是刻意预留的不可达状态，
            # 按它筛选永远得到空集。
            allowed = [state.code for state in PURCHASE_ORDER_WORKFLOW.selectable_states()]
            if status not in allowed:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "状态的取值无效",
                    data={"field": "status", "allowed": allowed},
                )
            where.append(f"{prefix}status = %s")
            values.append(status)

        for field, column in (
            ("supplier_id", "supplier_id"),
            ("material_id", "material_id"),
            ("warehouse_id", "warehouse_id"),
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
            ("expected_from", "expected_delivery_date", ">="),
            ("expected_to", "expected_delivery_date", "<="),
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
        """采购单列表（分页）。

        权限码是 `purchase.view` 而**不是** `purchase_order.view`：registry §1.7
        的权限码沿用早期版本命名（`早期版本 purchase_service.py:26`），
        "权限码 = 能力名去点前缀"这条派生规则在本域不成立。
        """
        ctx.require("purchase.view")
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))

        where, values = self._filters(params, "o")
        scope_fragment, scope_values = scope.where_clause("o")
        if scope_fragment and scope_fragment != "1=1":
            where.append(f"({scope_fragment})")
            values.extend(scope_values)
        clause = " AND ".join(where) if where else "1=1"

        total_row = tx.query_one(
            f"SELECT COUNT(*) AS n FROM {TABLE_PURCHASE_ORDER} AS o WHERE {clause}", values
        )
        total = int((total_row or {}).get("n", 0))

        rows = tx.query_all(
            f"""
            SELECT o.*,
                   s.name AS supplier_name,
                   m.name AS material_name,
                   {_UNPAID_SUBQUERY} AS unpaid_amount
            FROM {TABLE_PURCHASE_ORDER} AS o
            LEFT JOIN business_partners AS s ON s.id = o.supplier_id
            LEFT JOIN materials AS m ON m.id = o.material_id
            WHERE {clause}
            ORDER BY o.id DESC
            LIMIT %s OFFSET %s
            """,
            [*values, page.size, page.offset],
        )
        return cap.HandlerResult(
            data=page.to_result(
                [self._present(row, ctx.actor.permissions) for row in rows], total
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
        """采购单详情。

        详情里带上 `available_transitions` —— 前端渲染动作时要知道**从当前状态出发**
        能去哪些状态。这是"服务端按转移表过滤"的运行时形态：静态声明只能给全集，
        用户就能选到非法目标。
        """
        ctx.require("purchase.view")
        # 路径参数由执行器作为**一个 dict** 传进来（读能力时请求体不参与），
        # 所以这里自己取 —— 判据与 `DictionaryService.get_row_by_id` 同一口径。
        raw = (path_params or {}).get("order_id")
        if raw is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "采购单详情需要 path_params 传 order_id",
            )
        order_id = int(raw)
        row = self._scope_row(tx, scope, order_id)
        decorated = self._present(row, ctx.actor.permissions)
        decorated["available_transitions"] = [
            {"value": state.code, "label": state.label}
            for state in PURCHASE_ORDER_WORKFLOW.available_transitions(str(row["status"]))
        ]
        # ⚠️ **读能力不带 `resource_id`**：`web/app.py::_ok` 见到它就会把响应再包一层
        # （`data = {resource_id, record: result.data}`），于是 `data.record.record` ——
        # 前端按契约读 `data.record.code` 得到 `undefined`，**每个格子显示「—」且不报错**。
        # 回读校验（`_reload_after`）对读能力本来就直接返回 None，所以这里不需要它。
        return cap.HandlerResult(data={"record": decorated})

    def load_order(
        self, tx: UnitOfWork, *, scope: Scope, record_id: int
    ) -> dict[str, Any] | None:
        """回读函数（执行器在提交前调它确认"写入真的落了库"）。

        `docs/WRITE_CONTRACT.md` 规则 2：`executed` 不是"我们调用了 INSERT"，
        而是"读回来的行确实是我们想要的样子"。

        返回的字段必须包含 `created_by` / `approved_by` —— `DistinctActors`
        的不变量执行需要"改动前/后的行快照"里的这两个列（规则是
        **经办人 ≠ 审批人**），而 `before` 就是本函数的返回值。
        """
        row = tx.query_one(
            f"SELECT o.* FROM {TABLE_PURCHASE_ORDER} AS o WHERE o.id = %s", (record_id,)
        )
        return None if row is None else {**row, "version": int(row.get("row_version") or 1)}

    # -- 派生 -----------------------------------------------------------------

    def _present(self, row: dict[str, Any], permissions: frozenset[str]) -> dict[str, Any]:
        """列表/详情共用的派生。

        两处（列表与详情）用同一个函数，是因为早期版本正是让它们各拼一遍 SELECT ——
        于是"列表里看到的金额"与"详情里看到的金额"在某一轮改动后开始不一致。
        """
        decorated = decorate_order(row, permissions)
        # 跨域派生列（仓库名 / 已到货量）：**等 t16 的权威表名**，显式给 None。
        # 不用 `if key not in row` 分支：键必须存在（前端列声明里有它），
        # 值可以为空 —— "这一列现在拿不到"与"这一列不存在"是两件事。
        decorated.setdefault("warehouse_name", None)
        decorated.setdefault("received_quantity", None)
        return decorated


__all__ = ["PurchaseOrderService"]
