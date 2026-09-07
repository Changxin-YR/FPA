"""交付单与收款单的写路径。

## 本文件包含两个服务

  * `DeliveryWriteService`  —— `delivery.create` / `delivery.verify`
  * `SalesReceiptWriteService` —— `sales_receipt.create` / `sales_receipt.verify`
  * 以及 `ReceivableWriteService` —— **没有对应能力**，但提供
    `create_from_delivery()`，即 `ROLLOUT_CONTRACT.md` §2 冻结的跨域入口
    `sales.receivables.create_from_delivery(...)`。

放在一起的理由：三者的写路径形状相同（`ctx.require` → `_scope_row` → 状态机校验 →
写库 → 返回 `resource_id`），而它们之间**互相调用**（交付核验生成应收、收款核验推进
应收余额）。分成三个文件会让这条调用链跨三个文件、且 `_scope_row` 出现三次。

## 三条不变量挂在哪里、为什么（registry §4）

| 能力 | 不变量 | 依据 |
|---|---|---|
| `delivery.create` | `HarvestQuantityMatch` | **#6** 交付数量必须与出塘事实一致（Q9 恢复） |
| `delivery.create` | `CumulativeWithin` | **#10** 累计交付不得超过销售数量 |
| `delivery.verify` | `CumulativeWithin` | **#10** 同上（核验才让数量真正入账） |
| `delivery.verify` | `DistinctActors` | **#5** 经办人≠审批人（13 条之一） |
| `delivery.verify` | `PeriodOpen` | **#7** 关账锁整个期间，收入侧一视同仁（Q16） |
| `sales_receipt.create` | `AmountWithin` | **#4** 收款不得超过应收余额（早期版本 create+verify 两处重复，本版只声明一次） |
| `sales_receipt.verify` | `DistinctActors` | **#5** |
| `sales_receipt.verify` | `AmountWithin` | **#4**（旧的第二处，收拢到同一份声明） |
| `sales_order.*` | `DistinctActors` / `UniqueCode` / `OptimisticLock` / `StateTransition` / `StatusAllowsEdit` | #5 / #19 / #13 / #14 / #12 |

## 一处刻意不重复实现的换算

`HarvestQuantityMatch` 内部已经做了"`unit='tail'` 用 `harvest.quantity`，
否则用 `harvest.weight_kg × (2 if unit='jin' else 1)`"的换算（继承旧
`sales_source_control.py:22-24`）。**服务层不再写一遍** —— 两边各写一遍必然出现不一致，
而这条规则的唯一权威来源是那个不变量（registry §2.9 的差异表最后一行也明确写着
本版"不再有 tail 用 quantity、其他用 weight_kg×2 的换算逻辑"，指的就是不把它写在服务里）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fpa.kernel import capability as cap
from fpa.kernel.errors import DomainError, ErrorCode
from fpa.kernel.fields import f_datetime, f_enum, f_int, f_num, f_ref, f_str, text
from fpa.kernel.scope import Scope
from fpa.kernel.uow import UnitOfWork
from fpa.kernel.workflow import RowAction

from .sales_orders import DeliveryService, ReceivableService, SalesReceiptService
from .service import (
    DELIVERABLE_ORDER_STATUSES,
    MAX_AMOUNT,
    RECEIPT_METHOD_CHOICES,
    RECEIPT_METHODS,
    SALES_UNIT_CHOICES,
    TABLE_DELIVERY,
    TABLE_RECEIVABLE,
    TABLE_SALES_ORDER,
    TABLE_SALES_RECEIPT,
    require_date_order,
)


def _non_null(**fields: Any) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


def _as_decimal(value: Any) -> Decimal:
    from decimal import InvalidOperation

    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DomainError(ErrorCode.FIELD_INVALID, "数值格式无效") from exc


class DeliveryWriteService(DeliveryService):
    """交付单的写路径。"""

    @staticmethod
    def _load_order(tx: UnitOfWork, order_id: int) -> dict[str, Any]:
        order = tx.query_one(
            f"SELECT * FROM {TABLE_SALES_ORDER} WHERE id = %s", (int(order_id),)
        )
        if order is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "销售单不存在", data={"field": "sales_order_id"}
            )
        return order

    @staticmethod
    def _assert_order_deliverable(order: dict[str, Any]) -> None:
        """销售单必须处于可交付状态（§2.9 的 `sales_order_id` 约束）。

        可选范围继承旧前端 `SalePage.vue:46` 的下拉范围
        `['approved','partially_delivered']`。旧 `sales_posting.py:19-20` 在核验时才查，
        本版在 **create 与 verify 两处都查**：create 时报错让用户当场改选，
        verify 时再查是因为中间可能有人取消了订单。
        """
        status = str(order.get("status") or "")
        if status not in DELIVERABLE_ORDER_STATUSES:
            raise DomainError(
                ErrorCode.CONFLICT,
                f"销售单当前状态为「{status}」，不可交付",
                data={
                    "field": "sales_order_id",
                    "status": status,
                    "allowed": list(DELIVERABLE_ORDER_STATUSES),
                },
            )

    def create_delivery(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("交付单号", required=True, max_length=64),
        name: f_str("交付事项", required=True, max_length=100),
        sales_order_id: f_ref("销售来源", "sales_order", required=True),
        harvest_document_id: f_ref("出塘单", "harvest", required=True),
        unit: f_enum(
            "计量单位",
            SALES_UNIT_CHOICES,
            help="默认沿用销售单计量单位；填写时必须与销售单一致",
        ),
        quantity: f_num("交付数量", required=True, minimum=0, maximum=MAX_AMOUNT),
        delivered_at: f_datetime("交付时间", required=True),
        transport_info: f_str("运输信息", max_length=200),
        acceptance_note: text("验收说明", max_length=500),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        """登记交付。

        `harvest_document_id` **必填**（§2.9，Q9 恢复）。它承担两件事：
          * §4 #6 交付数量与出塘事实一致（由 `HarvestQuantityMatch` 执行）；
          * §2.9 "出塘单须为 verified、同批次同塘口"（同一条不变量也查这个）。

        `batch_id` / `pond_id` **不接受客户端提交**，从销售单带出 —— 否则调用方可以
        用"塘口 A 的交付"去核销"塘口 B 的销售单"，而 `HarvestQuantityMatch` 的
        同批次同塘口校验就失去意义（它比的是交付行上的列）。
        """
        ctx.require("sales.deliver")

        order = self._load_order(tx, int(sales_order_id))
        self._assert_order_deliverable(order)
        # 第三层防御：销售单本身必须在数据范围内。
        if not scope.allows_row(order):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED, "该销售单不在当前账号的数据范围内"
            )

        delivery_unit = str(unit or order.get("unit") or "kg")
        order_unit = str(order.get("unit") or "kg")
        if delivery_unit != order_unit:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "交付计量单位必须与销售单一致",
                data={"field": "unit", "order_unit": order_unit},
            )

        if _as_decimal(quantity) <= 0:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "交付数量必须大于 0", data={"field": "quantity"}
            )

        tx.execute(
            f"INSERT INTO {TABLE_DELIVERY} "
            "(organization_id, farm_id, area_id, code, name, sales_order_id, "
            " harvest_document_id, batch_id, pond_id, unit, quantity, delivered_at, "
            " transport_info, acceptance_note, note, status, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s)",
            (
                order["organization_id"],
                order["farm_id"],
                order["area_id"],
                code,
                name,
                int(sales_order_id),
                int(harvest_document_id),
                order["batch_id"],
                order["pond_id"],
                delivery_unit,
                quantity,
                delivered_at,
                transport_info,
                acceptance_note,
                note,
                ctx.actor.user_id,
            ),
        )
        delivery_id = tx.last_insert_id()
        row = self.load_delivery(tx, scope=scope, record_id=delivery_id)
        assert row is not None
        return cap.HandlerResult(
            data={
                "record": self._delivery_detail(tx, ctx, scope, delivery_id),
                # ★ 把**服务端解析出来的** batch_id / pond_id 交给内核不变量。
                #
                # 为什么需要它：`HarvestQuantityMatch` 的"同批次同塘口"比对用
                # `_value_from((payload, before), 'batch_id')` 取值，而本能力的 payload
                # **没有**这两个字段 —— registry §0.7 规则 1 / Q7 规定归属由服务端从
                # 塘口解析、**不接受客户端提交**。取不到值时该不变量会 `continue`，
                # 于是"出塘单属于别的塘口"**静默放行**（我用最小假 tx 实测确认）。
                #
                # 这不是把校验搬进服务（那会变成两处描述同一件事），而是把服务
                # **已经算好的事实**提供给不变量 —— 与 `NoOverlappingSource` 的
                # `_period_absolute` 是同一手法。`runner._invariant_extra` 会把这
                # 个字典平铺进 payload，于是 `_value_from` 能按列名取到它们。
                "_invariant_context": {
                    "batch_id": int(order["batch_id"]),
                    "pond_id": int(order["pond_id"]),
                },
            },
            resource_id=delivery_id,
            message=f"已登记交付「{name}」（编号 {code}）",
        )

    def verify_delivery(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        delivery_id: f_int("交付单 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """核验交付：draft -> verified，**并生成应收**。

        ## 三件事按顺序发生，都在同一个事务里

        1. 交付行置 `verified`（这样 `CumulativeWithin` 与销售单状态推导都能看到它）；
        2. 生成应收（`ReceivableWriteService.create_from_delivery`，跨域入口表 §2）；
        3. 推进销售单状态（§3.5 的推导公式，原样继承旧 `sales_posting.py:38`）。

        **顺序是刻意的**：先置状态再算累计，因为 `CumulativeWithin` 在服务之后执行、
        看到的是写入后的状态；先建应收再推状态，是因为应收生成失败时整个事务回滚，
        不会留下"交付已核验但没有应收"的半截账。

        ## 状态推导公式

        旧 `sales_posting.py:38`：
            `fully_delivered` if delivered == ordered
            else `partially_delivered` if delivered
            else `approved`

        这里 `delivered` 取**已核验交付的累计**（与 §4 #10 用同一套状态集），
        所以"列表里显示的已交付量"与"系统据此推进的状态"必然一致。
        """
        ctx.require("sales.verify")
        row = self._scope_row(tx, scope, int(delivery_id))
        from .service import DELIVERY_WORKFLOW

        DELIVERY_WORKFLOW.require_action(str(row["status"]), RowAction.VERIFY)
        if str(row["status"]) != "draft":
            raise DomainError(
                ErrorCode.CONFLICT,
                "该交付单已核验，不能重复核验",
                data={"status": str(row["status"])},
            )

        order = self._load_order(tx, int(row["sales_order_id"]))
        self._assert_order_deliverable(order)

        affected = tx.execute(
            f"UPDATE {TABLE_DELIVERY} SET status='verified', verified_by=%s, "
            "verified_at=NOW(), row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s AND status='draft'",
            (
                ctx.actor.user_id,
                ctx.actor.user_id,
                int(delivery_id),
                int(expected_version),
            ),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该交付单状态已变化或已被他人修改，请刷新后重试",
            )

        # ① 生成应收（同事务）。级联：这条交付之后的累计已核验交付 >
        #    销售数量时，`CumulativeWithin` 会在不变量阶段拒绝，整个事务回滚。
        #
        # ★ `delivery` 传的是**刚写完库的那一行**，不是上面 `_scope_row` 读到的旧快照：
        #   服务在本函数的上一步刚把状态改成 `verified`（`UPDATE ... status='verified'`），
        #   而 `create_from_delivery` 的第一件事就是断言 `delivery["status"] == "verified"`
        #   （它只接受已核验的交付）。传旧快照（`status='draft'`）会让这条断言必然失败，
        #   报 `INTERNAL_ERROR 只有已核验的交付才能生成应收（这是内部调用顺序错误）` ——
        #   实测：任何一次 `delivery.verify` 都拿不到应收，销售域的主链路是断的。
        #
        #   为什么在**这里**补一行而不是让 `create_from_delivery` 放松断言：那条断言是对的
        #   （应收不能由未核验的交付产生），错的是调用方递了一份过期数据。
        #   `row` 是 `_scope_row` 的快照，只用于更新前的判定，不应再当作"当前状态"。
        delivery = {**row, "status": "verified"}
        receivable_id = ReceivableWriteService().create_from_delivery(
            tx,
            delivery=delivery,
            order=order,
            actor_id=ctx.actor.user_id,
        )

        # ② 推进销售单状态
        self._advance_order(tx, order=order, actor_id=ctx.actor.user_id)

        data = {"record": self._delivery_detail(tx, ctx, scope, int(delivery_id))}
        data["receivable_id"] = receivable_id
        # ★ `PeriodOpen`（§4 #7）需要的两个事实，必须显式回传：
        #
        # * `occurred_on` —— 核验是 `action`，请求体里**没有**发生日期；应收行按交付
        #   日期入账（`create_from_delivery` 用 `delivered_at`），期间锁定判的就是那一天。
        # * `organization_id` —— 会计期间**每个企业各有一份**，没有租户键内核就答不出
        #   "本企业这个月关了没有"，会命中邻家的期间行（误拦、或静默放行已关账期间）。
        #
        # 为什么写在这一层：`_delivery_detail` 返回的是纯读结果（不带不变量上下文），
        # 所以这份上下文只能由核验路径自己挂上 —— 漏了它 `PeriodOpen` 会以"缺上下文"
        # `INTERNAL_ERROR` 报错，而不是静默放行（内核刻意如此）。
        data.setdefault("_invariant_context", {}).update(
            {
                "occurred_on": _date_of(row["delivered_at"]),
                "organization_id": row["organization_id"],
            }
        )
        return cap.HandlerResult(
            data=data,
            resource_id=int(delivery_id),
            message=f"交付单「{row['name']}」已核验，已生成应收",
        )

    @staticmethod
    def _advance_order(
        tx: UnitOfWork, *, order: dict[str, Any], actor_id: int
    ) -> None:
        """按累计已核验交付量推进销售单状态（§3.5 推导公式）。"""
        counted = tx.query_one(
            f"SELECT COALESCE(SUM(quantity),0) AS total FROM {TABLE_DELIVERY} "
            "WHERE sales_order_id = %s AND status = 'verified'",
            (int(order["id"]),),
        )
        delivered = _as_decimal((counted or {}).get("total") or 0)
        ordered = _as_decimal(order["quantity"])

        if delivered >= ordered:
            target = "fully_delivered"
        elif delivered > 0:
            target = "partially_delivered"
        else:
            target = "approved"

        # 状态只前进，不回退：`cancelled` / `closed` 不该被这里覆盖。
        current = str(order["status"])
        if current in ("cancelled", "closed"):
            return
        if target == current:
            return

        tx.execute(
            f"UPDATE {TABLE_SALES_ORDER} SET status=%s, row_version=row_version+1, "
            "updated_by=%s WHERE id=%s",
            (target, actor_id, int(order["id"])),
        )


class ReceivableWriteService(ReceivableService):
    """应收的写路径。

    **没有对应能力**：registry §1.8/§2.9 里只有 `receivable.list`，
    没有 `receivable.create`。应收是 `delivery.verify` 的**副作用**，
    并且它的状态由 `sales_receipt.verify` 内部推进。

    所以本类的方法**不是能力**，而是 `ROLLOUT_CONTRACT.md` §2 冻结的跨域入口：
    `sales.receivables.create_from_delivery(...)`。
    """

    def create_from_delivery(
        self,
        tx: UnitOfWork,
        *,
        delivery: dict[str, Any],
        order: dict[str, Any],
        actor_id: int,
    ) -> int:
        """按一条**已核验**的交付生成应收。

        ## 金额怎么算

        `total_amount = delivery.quantity × order.unit_price`。

        用**交付行的数量**而不是销售单的数量：交付是分批的，每批按实际交付量计价。
        单价取自销售单（本版不支持分批不同单价，registry 也没有这个字段）。

        ## 编码怎么生成

        应收的 `code` 从交付单派生：`AR-<交付单 code>`。
        为什么派生而不是让调用方传：`delivery.verify` 没有"应收编号"这个入参
        （registry §1.8 的 `delivery.verify` 只收 `expected_version`），
        而 `receivables.code` 有 `uq_receivables_org_code` 唯一键。
        派生保证"同一条交付只会有一条应收"（配合 `uq_receivables_delivery`），
        且编码可追溯回交付单。

        长度检查：`deliveries.code` 是 VARCHAR(64)，前缀 `AR-` 后是 67 —— 超过
        `receivables.code` 的 VARCHAR(64)。所以这里**截断**并保留可追溯前缀，
        而不是让 DB 报截断错误（那会表现为一次莫名其妙的 500）。
        """
        if str(delivery.get("status")) != "verified":
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "只有已核验的交付才能生成应收（这是内部调用顺序错误）",
                data={"status": str(delivery.get("status"))},
            )

        existing = tx.query_one(
            f"SELECT id FROM {TABLE_RECEIVABLE} WHERE delivery_id = %s",
            (int(delivery["id"]),),
        )
        if existing is not None:
            # 幂等：同一条交付重复核验不该产生第二条应收。
            # `uq_receivables_delivery` 是并发下的底线，这里是**可读的**那一层。
            return int(existing["id"])

        amount = _as_decimal(delivery["quantity"]) * _as_decimal(order["unit_price"])
        code = f"AR-{delivery['code']}"[:64]

        tx.execute(
            f"INSERT INTO {TABLE_RECEIVABLE} "
            "(organization_id, farm_id, area_id, code, name, sales_order_id, "
            " delivery_id, customer_id, total_amount, paid_amount, currency, "
            " due_date, occurred_on, status, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,'CNY',%s,%s,'unpaid',%s)",
            (
                delivery["organization_id"],
                delivery["farm_id"],
                delivery["area_id"],
                code,
                f"交付 {delivery['code']} 应收",
                int(order["id"]),
                int(delivery["id"]),
                int(order["customer_id"]),
                amount,
                order["due_date"],
                # `occurred_on` 是 `PeriodOpen`（§4 #7）的反查日期。
                # 用**交付日期**而不是"今天"：期间锁定要按业务发生日判定，
                # 用今天会让补录历史的交付落进当期，从而绕过已关账期间。
                _date_of(delivery["delivered_at"]),
                actor_id,
            ),
        )
        return int(tx.last_insert_id())


class SalesReceiptWriteService(SalesReceiptService):
    """收款单的写路径。"""

    def create_receipt(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("收款单号", required=True, max_length=64),
        name: f_str("收款事项", required=True, max_length=100),
        receivable_id: f_ref("应收来源", "receivable", required=True),
        amount: f_num("收款金额", required=True, minimum=0, maximum=MAX_AMOUNT),
        received_at: f_datetime("收款日期", required=True),
        receipt_method: f_enum("收款方式", required=True, choices=RECEIPT_METHOD_CHOICES),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        """登记收款。

        应收状态必须是 `unpaid` / `partial`（§2.9）。
        `amount <= 应收余额` 的判定交给 `AmountWithin` 不变量（§4 #4）——
        **服务层不重复实现**：早期版本在 create 与 verify 两处各写一遍，
        而本版同一份声明覆盖两处。
        """
        ctx.require("finance.receipt.manage")

        receivable = tx.query_one(
            f"SELECT * FROM {TABLE_RECEIVABLE} WHERE id = %s", (int(receivable_id),)
        )
        if receivable is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "应收账款不存在", data={"field": "receivable_id"}
            )
        if not scope.allows_row(receivable):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED, "该应收账款不在当前账号的数据范围内"
            )
        if str(receivable["status"]) not in ("unpaid", "partial"):
            raise DomainError(
                ErrorCode.CONFLICT,
                f"该应收当前状态为「{receivable['status']}」，不能再登记收款",
                data={
                    "field": "receivable_id",
                    "status": str(receivable["status"]),
                    "allowed": ["unpaid", "partial"],
                },
            )
        if _as_decimal(amount) <= 0:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "收款金额必须大于 0", data={"field": "amount"}
            )

        tx.execute(
            f"INSERT INTO {TABLE_SALES_RECEIPT} "
            "(organization_id, farm_id, area_id, code, name, receivable_id, amount, "
            " received_at, receipt_method, occurred_on, note, status, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s)",
            (
                receivable["organization_id"],
                receivable["farm_id"],
                receivable["area_id"],
                code,
                name,
                int(receivable_id),
                amount,
                received_at,
                receipt_method,
                _date_of(received_at),
                note,
                ctx.actor.user_id,
            ),
        )
        receipt_id = tx.last_insert_id()
        row = self.load_receipt(tx, scope=scope, record_id=receipt_id)
        assert row is not None
        return cap.HandlerResult(
            data={"record": self._receipt_detail(tx, ctx, scope, receipt_id)},
            resource_id=receipt_id,
            message=f"已登记收款「{name}」（编号 {code}）",
        )

    def verify_receipt(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        receipt_id: f_int("收款单 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """核验收款：draft -> verified，**并推进应收的累计已收款**。

        ## 为什么余额判定不在这里

        `amount <= 应收余额` 由 `AmountWithin` 不变量执行（§4 #4）。本方法只做
        "原子地推进累计值"。两者分工：
          * 不变量负责"**什么条件下不允许写**"，并给出可读文案；
          * 服务负责**写入**与并发控制。

        ## 为什么用 `paid_amount = paid_amount + amount` 而不是先读再写

        读-算-写在并发下会丢失更新（两个收款同时核验，第二个读到旧余额）。
        把它写进一条 UPDATE 让数据库做加法，是这一层唯一可靠的写法；
        `AmountWithin` 的 `FOR UPDATE` 负责串行化"判定"，本语句负责"不回退"。

        ## 状态推进

        `paid_amount` 达到 `total_amount` → `settled`，否则 `partial`。
        `overpaid` 永远不会出现（`AmountWithin` 在源头挡住超收，§4 #4 与
        `receivables` 的 `chk_receivables_paid` 两层都挡）。
        """
        ctx.require("finance.receipt.verify")
        row = self._scope_row(tx, scope, int(receipt_id))
        from .service import SALES_RECEIPT_WORKFLOW

        SALES_RECEIPT_WORKFLOW.require_action(str(row["status"]), RowAction.VERIFY)
        if str(row["status"]) != "draft":
            raise DomainError(
                ErrorCode.CONFLICT,
                "该收款单已核验，不能重复核验",
                data={"status": str(row["status"])},
            )

        # 与 `AmountWithin` 共用同一把行锁，并保存更新前的余额快照。
        # 执行器在服务写入后才运行不变量；没有这份快照，最后一笔合法收款
        # 会被更新后的余额 0 误判为超额。
        receivable = tx.query_one(
            f"SELECT * FROM {TABLE_RECEIVABLE} WHERE id = %s FOR UPDATE",
            (int(row["receivable_id"]),),
        )
        if receivable is None:
            raise DomainError(
                ErrorCode.NOT_FOUND,
                "收款对应的应收账款不存在",
                data={"field": "receivable_id"},
            )
        if not scope.allows_row(receivable):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED,
                "该应收账款不在当前账号的数据范围内",
                data={"field": "receivable_id"},
            )

        affected = tx.execute(
            f"UPDATE {TABLE_SALES_RECEIPT} SET status='verified', verified_by=%s, "
            "verified_at=NOW(), row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s AND status='draft'",
            (
                ctx.actor.user_id,
                ctx.actor.user_id,
                int(receipt_id),
                int(expected_version),
            ),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该收款单状态已变化或已被他人修改，请刷新后重试",
            )

        # ★ 这里**不要**写成一条
        #       SET paid_amount = LEAST(paid_amount + %s, total_amount),
        #           status      = CASE WHEN paid_amount + %s >= total_amount ...
        #   MySQL 单表 UPDATE 的赋值**从左到右**求值：第二条 SET 里的 `paid_amount`
        #   已经是第一条更新后的值 ⇒ **同一笔收款被算两遍** ⇒ 收款额达到应收的
        #   50% 就会被判成 `settled`（实测：总额 100 收 50 → paid=50 而 status='settled'，
        #   余额还剩 50；界面同屏显示「未收款 50 / 状态=已结清」）。
        #   旧写法在 30% 与 100% 时恰好是对的，只在 [50%, 100%) 区间错，极难被偶然发现。
        #
        #   改成"显式算好再写"：不再依赖任何求值顺序。
        #   并发安全由**上面那条 `SELECT ... FOR UPDATE`** 保证——本事务已锁住该行，
        #   "读-算-写"不会丢失更新（原注释担心的正是这一点，锁已经解决了它）。
        receipt_amount = _as_decimal(row["amount"])
        total_amount = _as_decimal(receivable["total_amount"])
        paid_after = min(_as_decimal(receivable["paid_amount"]) + receipt_amount, total_amount)
        status_after = "settled" if paid_after >= total_amount else "partial"
        tx.execute(
            f"UPDATE {TABLE_RECEIVABLE} SET "
            "paid_amount = %s, status = %s, row_version = row_version + 1 "
            "WHERE id = %s",
            (paid_after, status_after, int(row["receivable_id"])),
        )

        data = {"record": self._receipt_detail(tx, ctx, scope, int(receipt_id))}
        data.setdefault("_invariant_context", {})["_amount_within_before"] = {
            "total": str(receivable["total_amount"]),
            "paid": str(receivable["paid_amount"]),
            "status": str(receivable["status"]),
            "currency": str(receivable["currency"]),
        }
        return cap.HandlerResult(
            data=data,
            resource_id=int(receipt_id),
            message=f"收款单「{row['name']}」已核验，应收余额已更新",
        )


def _date_of(value: Any):
    """从 `datetime` / `date` / 字符串里取日期部分（`PeriodOpen` 的反查键）。"""
    from datetime import date, datetime

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text_value = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text_value, fmt).date()
        except ValueError:
            continue
    raise DomainError(
        ErrorCode.FIELD_INVALID, "日期格式无效，应为 YYYY-MM-DD", data={"value": text_value}
    )


__all__ = [
    "DeliveryWriteService",
    "ReceivableWriteService",
    "SalesReceiptWriteService",
]
