"""付款的写路径：登记付款 / 核验付款。

## `payment.verify` 是本域的核心证明点

它同时挂两条不变量（registry §1.7 与 §4）：

    AmountWithin      #4  付款不得超过应付余额
    DistinctActors    #5  经办人 ≠ 核验人（不留自审例外，Q8 裁决）

**早期版本的两处缺陷，本域都不抄：**

1. #4 在早期版本里被实现了**两遍** —— `早期版本 purchase_payment_store.py:114-115`（创建时）
   与 `:154-157`（核验时）。registry §1.7 明确："新系统只在校验点 `payment.verify`
   强制、`payment.create` 只做**提示性**校验"。所以
   `AmountWithin` 只出现在 `payment.verify` 的声明里；
   `payment.create` 里的余额判断是**描述性**的（不拦截），它只负责让用户提前看见
   "这笔登记超过了余额"，真正的强制在同一事务的写库前执行。

2. #5 在早期版本里**完全没有** —— 采购域允许自审。新系统按 Q8 推广且不留例外。

## 付款核验的三件事必须原子

    1. 付款单仍是 draft        —— `UPDATE ... WHERE status='draft'`（原子占位）
    2. 经办人 ≠ 核验人          —— 不变量 + 服务的显式判断 + DB CHECK
    3. 累计已付不得超过应付总额 —— 不变量（写库前）+ `chk_purchase_payables_paid`（写库后）

第 1 条必须是 WHERE 条件而不是"先查再断言"：并发下两个请求会都通过检查再都执行，
而 `WHERE status='draft'` 保证只有一个能改到行。**约束要放在能真正保证它的地方。**
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from yuxin.kernel import capability as cap
from yuxin.kernel.errors import DomainError, ErrorCode
from yuxin.kernel.fields import Choice, f_datetime, f_enum, f_int, f_num, f_ref, f_str, text
from yuxin.kernel.scope import Scope
from yuxin.kernel.uow import UnitOfWork
from yuxin.kernel.workflow import RowAction

from .payments import PurchasePaymentService
from .service import (
    MAX_AMOUNT,
    PAYABLE_PAYABLE_STATUSES,
    PAYMENT_METHODS,
    PAYMENT_METHOD_LABELS,
    TABLE_PURCHASE_PAYABLE,
    TABLE_PURCHASE_PAYMENT,
    decorate_payment,
    tenant_from_scope,
)

_EXPECTED_VERSION = f_int(
    "乐观锁版本",
    required=True,
    minimum=1,
    help="当前记录的版本号：与库里不一致会被拒绝（防止两人同时改同一条）",
)


def _method_choices() -> tuple[Choice, ...]:
    """付款方式的候选值。

    label **取自 `PAYMENT_METHOD_LABELS`**，不在这里再写一份中文 ——
    早期版本同一个枚举在三个页面里三种翻译。
    """
    return tuple(
        Choice(value=code, label=PAYMENT_METHOD_LABELS.get(code, code))
        for code in PAYMENT_METHODS
    )


def _payable_status_after(paid_amount: Decimal, total_amount: Decimal) -> str:
    """按累计已付推导应付状态（**唯一的一处推导**）。

    为什么写成函数而不是内联 `if`：这个推导在第一版实现里会出现在"核验付款"
    一处就够，但下一轮"红字冲销"或"付款作废"也会需要它 —— 那时若各写一遍
    `partial` / `settled` 的判定，就又多出一处描述同一件事。放在这里，
    调用方只有一种写法。

    不使用 `Decimal.compare` 之外的容差：金额列是 `DECIMAL(16,2)`，比较是精确的。
    刻意**不加 epsilon** —— 财务口径上"少 1 分"与"少 1 元"都叫未结清。
    """
    if paid_amount >= total_amount:
        return "settled"
    if paid_amount > Decimal("0"):
        return "partial"
    return "unpaid"


class PurchasePaymentWriteService(PurchasePaymentService):
    """付款与应付的写路径。"""

    def create_payment(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("付款单号", required=True, max_length=64),
        name: f_str("付款事项", required=True, max_length=100),
        payable_id: f_ref("应付来源", "purchase_payable", required=True),
        amount: f_num("付款金额", required=True, minimum=0.01, maximum=float(MAX_AMOUNT)),
        paid_at: f_datetime("付款日期", required=True, help="不得晚于当前时间"),
        payment_method: f_enum("付款方式", _method_choices(), required=True),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        """登记一笔付款（初始状态 draft，**不做金额强校验**）。

        ## 为什么这里不拦"超过应付余额"

        registry §1.7：`payment.create` **只做提示性校验**，强制点是
        `payment.verify`。理由是从业务上讲得通：登记阶段可以先记下来
        （供应商开的票可能大于本次应付、或应付款项尚未核验），
        **核验**才是"这笔钱真的付出去了"的那一刻。

        但"提示性"不等于"沉默"：超出余额时这里**不报错**，
        而是在返回的 `data` 里带 `over_balance_warning`，
        让前端能显示一句提示。这样既有提示、又没有第二个强制点。

        ## 状态与应付的其他约束

        * `status` 固定 `draft`（不接受客户端指定）
        * 应付状态必须是 `unpaid` / `partial`（`PAYABLE_PAYABLE_STATUSES`）——
          这是**引用对象的当前可用性**，与"金额是否超余额"是两件事：
          已结清的应付根本不该再挂付款单。它由 `payment.verify` 的
          `AmountWithin(statuses=...)` 负责强制；这里判一次是为了给出
          **更早、更具体**的错误（否则用户会先登记成功、到核验时才被拒）。
        """
        ctx.require("finance.payment.manage")

        payable = self._payable_scope_row(tx, scope, int(payable_id))
        if str(payable["status"]) not in PAYABLE_PAYABLE_STATUSES:
            raise DomainError(
                ErrorCode.CONFLICT,
                "该应付账款当前状态不允许再登记付款",
                data={
                    "field": "payable_id",
                    "status": str(payable["status"]),
                    "allowed": list(PAYABLE_PAYABLE_STATUSES),
                },
            )

        # registry §2.8：`paid_at` 不得晚于当前时间。
        # 判据用**服务器时间**，不用客户端传时区 —— 否则"未来时间"可以由时区差伪造。
        now = datetime.now()
        if paid_at > now:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "付款日期不得晚于当前时间",
                data={"field": "paid_at", "now": str(now)},
            )

        # 余额只有一处算法（`_lock_payable_balance`）：登记的**提示性**文案与
        # 核验的**硬拒绝**读的是同一个数 —— 否则会出现"页面说还能付 100，
        # 提交后说余额只有 80"。
        balance = Decimal(str(payable["total_amount"])) - Decimal(str(payable["paid_amount"]))
        over_balance = Decimal(str(amount)) > balance

        try:
            tx.execute(
                f"INSERT INTO {TABLE_PURCHASE_PAYMENT} "
                "(organization_id, farm_id, area_id, code, name, payable_id, amount, "
                " paid_at, payment_method, note, status, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s)",
                (
                    payable["organization_id"],
                    payable["farm_id"],
                    payable["area_id"],
                    code,
                    name,
                    int(payable_id),
                    amount,
                    paid_at,
                    str(payment_method),
                    note,
                    ctx.actor.user_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            # 同 create_order：§4 #19 的兜底路径，判定用键名不用消息文本。
            if "uq_purchase_payments_org_code" in str(exc):
                raise DomainError(
                    ErrorCode.CONFLICT,
                    f"付款单号「{code}」已存在，请换一个",
                    data={"rule": "UNIQUE_CODE", "field": "code"},
                ) from exc
            raise
        payment_id = tx.last_insert_id()
        row = self.load_payment(tx, scope=scope, record_id=payment_id)
        assert row is not None

        message = f"已登记付款「{name}」（{code}）{amount}"
        if over_balance:
            message += f"；注意：本次金额超过该应付的未付余额 {balance}，核验时会被拒绝"

        return cap.HandlerResult(
            data={
                "record": decorate_payment(row, ctx.actor.permissions),
                # ★ 分租键必须回传（`runner._invariant_extra` 会把它合并进不变量的
                #   payload）。`UniqueCode(scope=("organization_id",))` 取不到范围列时
                #   **显式报错**（"不允许静默放宽成全表唯一"），所以少了它，
                #   登记付款永远无法成功 —— 实测报的是
                #   「编码唯一性校验缺少范围列 organization_id」。
                #   值来自应付行（继承它的企业/基地/区域），客户端无法影响。
                "_invariant_context": {
                    "organization_id": payable["organization_id"],
                    "farm_id": payable["farm_id"],
                    "area_id": payable["area_id"],
                },
                # 提示性信息：**不是错误**，也不是第二个强制点。
                "over_balance_warning": (
                    {
                        "balance": str(balance),
                        "requested": str(amount),
                        "message": f"本次付款金额超过未付余额 {balance}，核验时会被拒绝",
                    }
                    if over_balance
                    else None
                ),
            },
            resource_id=payment_id,
            message=message,
        )

    @staticmethod
    def _lock_payable_balance(tx: UnitOfWork, payable_id: int) -> Decimal:
        """锁定应付行并返回**当前余额**（`total_amount − paid_amount`）。

        与内核 `AmountWithin` 的算法逐字一致（`余额 = balance_column − paid_column`）：
        两处算同一个减法，所以不可能给出不同结论。

        `FOR UPDATE` 不是装饰：并发两笔付款各自读到旧余额就会双双通过，
        而 `paid_amount` 的自增在数据库层是原子的 —— 最终和会超过总额，
        唯一挡住它的是 CHECK 约束（一条 500 而不是可读的 409）。
        """
        row = tx.query_one(
            f"SELECT total_amount, paid_amount FROM {TABLE_PURCHASE_PAYABLE} "
            "WHERE id = %s FOR UPDATE",
            (payable_id,),
        )
        if row is None:
            raise DomainError(
                ErrorCode.NOT_FOUND,
                "付款对应的应付账款不存在",
                data={"field": "payable_id"},
            )
        return Decimal(str(row["total_amount"])) - Decimal(str(row["paid_amount"]))

    def verify_payment(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        payment_id: f_int("付款单 ID", required=True),
        expected_version: _EXPECTED_VERSION,
    ) -> cap.HandlerResult:
        """核验付款：draft -> verified，并推进应付的 `paid_amount` / `status`。

        ## 顺序（每一步都有理由）

        1. `ctx.require("finance.payment.verify")` —— 第三层权限
        2. `_payment_scope_row` —— 第三层范围（校验**具体那一行**）
        3. 状态机 `require_action(VERIFY)` —— "这一步在流程上是否可行"
        4. 原子占位 `UPDATE ... WHERE id AND row_version AND status='draft'
           AND created_by <> actor` —— 并发与自审的唯一可信保证
        5. 推进应付的 `paid_amount`（`UPDATE ... SET paid_amount = paid_amount + %s`）
           **用 SQL 自增而不是"读出来再写回"**：后者的写法在并发下会丢更新
           （两个请求都读到旧值，各自加完写回，只有一次生效）。
        6. `HandlerResult(resource_id=payment_id, ...)` —— 执行器按它回读付款单

        ## 为什么 `created_by <> %s` 也要写进 WHERE

        `DistinctActors` 不变量会拦自审，DB 也有 `chk_purchase_payments_distinct_actors`
        CHECK。WHERE 里再写一次**不是第三个检查点**，而是把"唯一可信的判定"
        （行锁范围内的原子条件）落在同一个语句里：不变量在写库**之后**执行、
        CHECK 在写库**之时**执行，两者失败都靠事务回滚 —— 而 WHERE 让"根本不写"
        成为可能，从而不产生一次无谓的写入与回滚。
        （这与 `over_received` 那种"两处各自判定同一件事"不同：这里是同一个判定
        在"尽早不写"层面的一次实现，判定结果不可能是另一个答案。）
        """
        ctx.require("finance.payment.verify")
        row = self._payment_scope_row(tx, scope, int(payment_id))
        from .service import PURCHASE_PAYMENT_WORKFLOW

        PURCHASE_PAYMENT_WORKFLOW.require_action(str(row["status"]), RowAction.VERIFY)
        if str(row["status"]) != "draft":
            raise DomainError(
                ErrorCode.CONFLICT,
                "该付款单已核验，不能重复核验",
                data={"status": str(row["status"])},
            )

        # ★ 余额校验必须在**任何写入之前**。
        #
        # 为什么不能只靠能力上的 `AmountWithin`：不变量在 `_call_service()` **之后**
        # 执行，而服务写 `paid_amount = paid_amount + amount` 时会**先**撞上数据库的
        # `chk_purchase_payables_paid (paid_amount <= total_amount)` ——
        # 原始 `OperationalError(3819)` 冒泡成 500，"余额不够"的可读文案根本没机会跑。
        # 实测过：那正是本测试第一版拿到的结果。
        #
        # 这**不是**第二个检查点：`AmountWithin` 与这里算的是同一个减法
        # （`total_amount − paid_amount`，与内核 `AmountWithin` 的
        # `余额 = balance_column − paid_column` 逐字一致），所以不可能给出不同结论。
        # 它只是把同一条判定放在"用户能看到可读错误"的那一侧；
        # `AmountWithin` 继续守着"绕过服务直接改库"的路径。
        #
        # 行锁（`FOR UPDATE`）：与 `AmountWithin` 一致 —— 并发两笔付款各自读到旧余额
        # 就会双双通过，而 `paid_amount` 的自增在数据库层是原子的，
        # 两个请求的真实和会超过总额。
        balance = self._lock_payable_balance(tx, int(row["payable_id"]))
        requested = Decimal(str(row["amount"]))
        if requested > balance:
            raise DomainError(
                ErrorCode.CONFLICT,
                f"本次金额 {requested} 超过应付账款余额 {balance}",
                data={
                    "rule": "AMOUNT_WITHIN",
                    "balance": str(balance),
                    "requested": str(requested),
                },
            )

        affected = tx.execute(
            f"UPDATE {TABLE_PURCHASE_PAYMENT} SET status='verified', verified_by=%s, "
            "verified_at=NOW(), row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s AND status='draft' AND created_by <> %s",
            (
                ctx.actor.user_id,
                ctx.actor.user_id,
                int(payment_id),
                int(expected_version),
                ctx.actor.user_id,
            ),
        )
        if affected == 0:
            current = self._payment_scope_row(tx, scope, int(payment_id))
            if int(current["created_by"]) == ctx.actor.user_id:
                raise DomainError(
                    ErrorCode.FORBIDDEN,
                    "经办人不能核验自己登记的付款，请由他人复核",
                    data={"rule": "DISTINCT_ACTORS"},
                )
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该付款单已被他人修改或已核验，请刷新后重试",
                data={"current_version": int(current["row_version"])},
            )

        payable_id = int(row["payable_id"])
        payable = tx.query_one(
            f"SELECT * FROM {TABLE_PURCHASE_PAYABLE} WHERE id = %s FOR UPDATE",
            (payable_id,),
        )
        if payable is None:
            raise DomainError(
                ErrorCode.NOT_FOUND,
                "付款对应的应付账款不存在",
                data={"field": "payable_id"},
            )

        # 用 SQL 自增，不读出来再写回（并发下后者会丢更新）。
        tx.execute(
            f"UPDATE {TABLE_PURCHASE_PAYABLE} SET paid_amount = paid_amount + %s, "
            "row_version = row_version + 1 WHERE id = %s",
            (row["amount"], payable_id),
        )
        # 状态按**更新后**的累计值推导。再查一次而不复用内存里的值：内存里的
        # `payable["paid_amount"]` 是加之前的，而本函数的推导依赖加之后的值 ——
        # 用错的那个会让"结清"晚一步才显示。
        updated_payable = tx.query_one(
            f"SELECT paid_amount, total_amount FROM {TABLE_PURCHASE_PAYABLE} WHERE id = %s",
            (payable_id,),
        )
        assert updated_payable is not None
        next_status = _payable_status_after(
            Decimal(str(updated_payable["paid_amount"])),
            Decimal(str(updated_payable["total_amount"])),
        )
        if next_status != str(payable["status"]):
            tx.execute(
                f"UPDATE {TABLE_PURCHASE_PAYABLE} SET status=%s, row_version=row_version+1 "
                "WHERE id=%s",
                (next_status, payable_id),
            )

        verified = self.load_payment(tx, scope=scope, record_id=int(payment_id))
        assert verified is not None
        return cap.HandlerResult(
            data={
                "record": decorate_payment(verified, ctx.actor.permissions),
                "payable": {
                    "id": payable_id,
                    "status": next_status,
                    "paid_amount": str(updated_payable["paid_amount"]),
                    "total_amount": str(updated_payable["total_amount"]),
                    "balance": str(
                        Decimal(str(updated_payable["total_amount"]))
                        - Decimal(str(updated_payable["paid_amount"]))
                    ),
                },
                # ★ 期间锁定需要的两个事实（`PeriodOpen(date_field="occurred_on")`）。
                #
                # `purchase_payments` 表里**没有** `occurred_on` 列：付款只有
                # `paid_at`（datetime），期间由它的**日期部分**决定。不额外存一列
                # 是因为同一事实有两个载体必然漂移。这里显式回传，
                # 让不变量能拿到它（"缺上下文不得静默跳过"）。
                #
                # `organization_id` 与它一样必需：会计期间**每个企业各有一份**
                # （`accounting_periods.organization_id` 是 NOT NULL），少了租户键，
                # 内核的期间反查会命中邻家的期间行——邻家已关账则误拦本企业的合法
                # 付款，邻家未关账则静默放行本企业已关账期间的付款。内核对此
                # **显式报错**（`INTERNAL_ERROR`）而不静默降级，所以这里必须回传。
                "_invariant_context": {
                    "occurred_on": str(row["paid_at"])[:10],
                    "organization_id": row["organization_id"],
                    "_amount_within_before": {
                        "total": str(payable["total_amount"]),
                        "paid": str(payable["paid_amount"]),
                        "status": str(payable["status"]),
                        "currency": str(payable["currency"]),
                    },
                },
            },
            resource_id=int(payment_id),
            message=(
                f"付款已核验：{verified['name']}（{verified['code']}）{verified['amount']}"
            ),
        )


# ---------------------------------------------------------------------------
# 回读函数绑定（`__yuxin_load_by_id__`）
#
# 见 `orders_write.py` 的同名说明 —— 不绑定会让执行器抛
# `INTERNAL_ERROR: 能力声称写入成功但没有提供回读函数`（master_data 的写能力
# 在 t13 之前的实际状态）。本域的 e2e 断言"不靠夹具补挂也能通过"。
# ---------------------------------------------------------------------------



__all__ = ["PurchasePaymentWriteService"]
