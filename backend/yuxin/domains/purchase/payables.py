"""应付账款：跨域入口 `create_from_receipt(...)`。

## 为什么应付**没有** `create` 能力

registry §1.7 只给了 `payable.list`（读）。应付由 **`receipt.verify`** 生成
（warehouse 域的能力），经 `ROLLOUT_CONTRACT.md` §2 登记的这一个进程内函数：

    purchase.payables.create_from_receipt(tx, ctx, *, purchase_order_id,
                                          receipt_id, lines, amount=None, actor_id)

人工建应付会让"应付金额应当等于已核验到货的金额"这条事实出现第二个来源 ——
而"两处描述同一件事"正是本项目要根除的形态。这与销售侧 `receivable`
（由 `delivery.verify` 生成、没有 `receivable.create`）完全同构。

## 幂等是**结构上**的，不是靠"记得查一下"

`purchase_payables` 上有 `UNIQUE KEY uq_purchase_payables_receipt (receipt_id)`。
核验路径是"查有没有 -> 没有则插"，并发下两次核验会双插 —— 唯一键是唯一可信的
判定，而应用层的"先查再插"做不到。所以本函数：

    先 SELECT（为了返回已存在的那一行，让重放可读）
    插入时捕获唯一键冲突 -> 再 SELECT 一次并返回 created=False

两条路径都必须有：只靠 SELECT 会双插，只靠异常会让重放在报错看起来像失败。
**重放返回成功（created=False）而不是报错**，因为 warehouse 的 `receipt.verify`
在重试时不该炸。

## 金额

`amount` 缺省时按 `purchase_orders.unit_price × Σlines 数量` 计算。
**明细单价校验**（旧 `PURCHASE_RECEIPT_PRICE_MISMATCH`，registry §2.7
"`unit_cost` 必须等于采购单价"）是 **warehouse 域 `receipt.verify` 的强制点** ——
本函数不替它判，只按采购单单价兜底；调用方算了金额就显式传 `amount`。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from yuxin.kernel.money import money
from typing import Any

from yuxin.kernel.errors import DomainError, ErrorCode
from yuxin.kernel.scope import Scope
from yuxin.kernel.uow import UnitOfWork

from .service import (
    TABLE_PURCHASE_ORDER,
    TABLE_PURCHASE_PAYABLE,
    assert_row_in_scope,
)

# 金额的小数位口径在 `kernel/money.py`（**全系统唯一一处**）。
#
# 本文件曾经自带 `_AMOUNT_QUANTUM = Decimal("0.01")` 与一个 `_quantize()`。
# 它与另外两处同价实现共存（`cost/entries.py` 的 `AMOUNT_QUANTUM`、
# `warehouse/ledger.py` 的 `quantize(Decimal("0.0001"))`）—— 而且那三处数值还不一样。
# "一个金额保留几位小数"是同一件事，收归到一处；差异也不再能静默存在。
_quantize = money


def create_from_receipt(
    tx: UnitOfWork,
    ctx: Any = None,
    *,
    purchase_order_id: int,
    # `receipt_document_id` 指向 `warehouse_documents.id`（单头），**不是**明细列：
    # warehouse 的明细表 `warehouse_document_lines` 只有 `document_id` + `line_no`
    # （`ROLLOUT_CONTRACT.md` §2A.6）。名字带 document 是**消歧** ——
    # 单头表被到货/领用共用，明细里根本没有 `receipt_id` 这一列。
    receipt_document_id: int,
    lines: tuple[tuple[int, Decimal], ...] = (),
    amount: Decimal | None = None,
    actor_id: int = 0,
    occurred_on: Any = None,
    supplier_id: int | None = None,
    happened_at: Any = None,
) -> dict[str, Any]:
    """为一张**已核验**的到货单生成应付。幂等：重放返回 `created=False`，不报错。

    ## 参数

    * `tx` —— **必须是 `receipt.verify` 那一个事务**（契约 §2：跨域效果必须同事务，
      否则到货核验成功而应付没生成，两者不一致且无法自动修复）；
    * `ctx` —— 调用方（warehouse）的 `ServiceContext`。用它做第三层范围校验：
      采购侧必须回答"这张单属不属于你"，而不是信任调用方；
    * `lines` —— 已核验明细 `((material_id, quantity), ...)`。缺省时按采购单数量计；
    * `amount` —— 缺省按 `purchase_orders.unit_price × Σ数量` 计算；
    * `occurred_on` / `happened_at` —— **入账日**决定这笔应付落在哪个会计期间，
      而 `PeriodOpen` 按它判定"该期间是否已关账"（§4 #7）。推导顺序：
      显式 `occurred_on` → `happened_at` 的日期部分 → `date.today()`。
      **不要用"今天"当默认**：那会让一张上个月核验的到货把应付记进本月，
      既绕开上月的期间锁定，又把成本归到错的月份。

    ## 返回

        {"id", "code", "status", "amount", "paid_amount", "balance",
         "currency", "created"}

    字段名与 `payable.list` 的行保持一致 —— 调用方不必再查一次就能用。
    """
    order = tx.query_one(
        f"SELECT * FROM {TABLE_PURCHASE_ORDER} WHERE id = %s", (int(purchase_order_id),)
    )
    if order is None:
        raise DomainError(
            ErrorCode.NOT_FOUND,
            "应付对应的采购单不存在",
            data={"field": "purchase_order_id"},
        )
    if ctx is not None:
        assert_row_in_scope(ctx.scope, order, "该采购单")

    # 幂等快路径：已生成过就原样返回。
    existing = tx.query_one(
        f"SELECT * FROM {TABLE_PURCHASE_PAYABLE} WHERE receipt_id = %s", (int(receipt_document_id),)
    )
    if existing is not None:
        return _result(existing, created=False)

    if amount is None:
        total_quantity = sum(
            (Decimal(str(quantity)) for _, quantity in lines), Decimal("0")
        )
        if total_quantity <= 0:
            # 没有明细也没传金额 -> 无法确定该记多少。**报错而不是记 0**：
            # 一条金额为 0 的应付会在列表里显示为"已结清"，掩盖真实的账目缺失。
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "无法确定应付金额：请传入已核验明细，或显式给出金额",
                data={"field": "amount"},
            )
        amount = _quantize(Decimal(str(order["unit_price"])) * total_quantity)
    else:
        amount = _quantize(Decimal(str(amount)))

    if amount <= 0:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, "应付金额必须大于 0", data={"field": "amount"}
        )

    # 入账日期：缺省今天。它同时是 `PeriodOpen` 的比较对象，所以必须有值。
    if occurred_on is None:
        # 入账日的来源顺序（**不是随手挑的**）：
        #   1. 调用方显式给的 `occurred_on`；
        #   2. 到货实际发生时间 `happened_at` 的**日期部分** —— 它决定应付落在哪个
        #      会计期间，而 `PeriodOpen` 按它判定期间是否已关账（§4 #7）。
        #      用"今天"会让一张**上个月**核验的到货把应付记进**本月**：
        #      既绕开上月的期间锁定，又让成本归到错的月份。
        #   3. 最后才退化成 `date.today()`（调用方没给 happened_at 时）。
        occurred_on = _as_date(happened_at) or date.today()

    try:
        tx.execute(
            f"INSERT INTO {TABLE_PURCHASE_PAYABLE} "
            "(organization_id, farm_id, area_id, name, purchase_order_id, receipt_id, "
            " supplier_id, total_amount, paid_amount, currency, due_date, occurred_on, "
            " status, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,'CNY',%s,%s,'unpaid',%s)",
            (
                order["organization_id"],
                order["farm_id"],
                order["area_id"],
                f"应付：{order['name']}",
                int(purchase_order_id),
                int(receipt_document_id),
                int(supplier_id if supplier_id is not None else order["supplier_id"]),
                amount,
                order["due_date"],
                occurred_on,
                int(actor_id),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        # 唯一键冲突 = 另一个请求（并发的第二次核验）已经生成过。
        # 判定用**键名**而不是异常消息文本 —— 早期版本 `production_store.py:142-146`
        # 的 `if key not in str(exc)` 就是消息文本判定，随 MySQL 版本变化。
        if "uq_purchase_payables_receipt" in str(exc):
            row = tx.query_one(
                f"SELECT * FROM {TABLE_PURCHASE_PAYABLE} WHERE receipt_id = %s",
                (int(receipt_document_id),),
            )
            if row is not None:
                return _result(row, created=False)
        raise

    row = tx.query_one(
        f"SELECT * FROM {TABLE_PURCHASE_PAYABLE} WHERE id = %s", (tx.last_insert_id(),)
    )
    assert row is not None
    return _result(row, created=True)


def _result(row: dict[str, Any], *, created: bool) -> dict[str, Any]:
    """统一的返回形态（`created` 区分"新建"与"重放"）。"""
    total = Decimal(str(row["total_amount"]))
    paid = Decimal(str(row["paid_amount"]))
    return {
        "id": int(row["id"]),
        "code": None,  # 应付是派生单据，没有人工单号（见迁移注释）
        "status": str(row["status"]),
        "amount": str(total),
        "paid_amount": str(paid),
        "balance": str(total - paid),
        "currency": str(row.get("currency") or "CNY"),
        "created": created,
    }


def _as_date(value: Any) -> Any:
    """把 ``happened_at`` / ``occurred_on`` 归一成 ``date``；取不到返回 None。

    三种受支持形态：``datetime``（取 ``.date()``）、``date``（原样）、
    ISO 字符串（取前 10 字符再 ``fromisoformat``）。

    **不用"看字符串里有没有某个词"那类判定** —— 早期版本
    ``production_store.py:142-146`` 的异常消息文本判定就是那一族，脆且随驱动版本变化。

    返回 ``date`` 或 ``None``（信号量语义）：`None` 表示"调用方没给/给了无效值"，
    由调用方用 `or date.today()` 兜底——**只在一处兜底**，不在这个函数里偷偷取今天
    （否则"入账日来自今天"这件事就藏进了助手内部，读代码的人看不到）。
    """
    from datetime import date as _date
    from datetime import datetime as _datetime

    if value is None:
        return None
    if isinstance(value, _datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return _date.fromisoformat(raw[:10])
    except ValueError:
        return None


__all__ = ["create_from_receipt"]

