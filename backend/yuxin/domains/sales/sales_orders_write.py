"""销售单的写路径：create / update / submit / approve / cancel。

继承 `SalesOrderService` 以复用 `_scope_row` / `load_order` / 派生列逻辑——
读写共用同一套范围校验与派生口径，避免"读看到一个样、写看到另一个样"。

## 每个写能力都做这几件事，顺序固定

    1. ctx.require(...)          —— 第三层权限校验（不信任执行器）
    2. _scope_row / tenant_from_pond  —— 第三层范围校验（校验的是**具体那一行**）
    3. 状态机校验                 —— require_action / require_transition
    4. 写库 + 返回 HandlerResult(resource_id=...)

第 4 步的 `resource_id` **不是可选的**：执行器要按它回读校验
（`docs/WRITE_CONTRACT.md` 规则 2）。

## 状态只能经三个动作推进

`status` 在 `create` / `update` 里**永不接受客户端提交**（registry §0.7 规则 2）。
让客户端指定状态等于让调用方跳过流程（例如直接把单子建成 `approved`）。
推进只能走 `submit` / `approve` / `cancel` 三个能力。

## 合规规则不在服务里重复实现

`approve` **不**自己写"经办人≠审批人"的判断。那条规则由内核 `DistinctActors`
在能力声明上统一执行（registry §4 #5 推广到全部核验/审批能力）。
在这之前，早期版本**只有销售域**实现了它（`早期版本 sales_service.py:121-122`），
采购/仓储/生产/成本全都没有——那正是"合规规则跨域不一致"的样本。
`DistinctActors` 比的是 `created_by` 与业务行上的核验列（`approved_by`，比早期版本
比 `updated_by` 更严格）。
"""

from __future__ import annotations

from typing import Any

from yuxin.domains.master_data.lookup import lookup_partner
from yuxin.domains.production.service import lookup_batch

from yuxin.kernel import capability as cap
from yuxin.kernel.errors import DomainError, ErrorCode
from yuxin.kernel.fields import f_int, f_num, f_enum, f_ref, f_str, text
from yuxin.kernel.scope import Scope
from yuxin.kernel.uow import UnitOfWork
from yuxin.kernel.workflow import RowAction

from .sales_orders import SalesOrderService
from .service import (
    MAX_AMOUNT,
    SALES_ORDER_WORKFLOW,
    SALES_UNIT_CHOICES,
    SALES_UNITS,
    TABLE_SALES_ORDER,
    require_date_order,
    tenant_from_pond,
)


def _non_null(**fields: Any) -> dict[str, Any]:
    """只保留**提交了**的字段（None 视为"没提交"）。

    与 `validate_payload` 把缺失字段填成 None 配套：后者统一了"缺字段 = None"，
    这里把"没提交"与"提交了 None"区分开——更新场景下两者含义不同
    （前者不动、后者清空）。
    """
    return {key: value for key, value in fields.items() if value is not None}


class SalesOrderWriteService(SalesOrderService):
    """销售单的写路径。"""

    # -- 内部工具 -------------------------------------------------------------

    @staticmethod
    def _assert_partner_is_customer(
        tx: UnitOfWork, *, customer_id: int, organization_id: int
    ) -> None:
        """客户必须是**本企业**的、且 `partner_type='customer'` 的往来单位。

        两条都要查（§2.9 只写了 `partner_type=customer`，但**同企业**是隐含前提）：
        跨企业引用会让"客户名称"这个派生列显示别家的数据，而数据范围是按企业划分的。
        这条属于内核 `SameTenant` 不变量的职责范围，但服务层也查一次——
        第三层防御的意义就是"不信任调用方"，而不变量只在执行器路径上执行。
        """
        # `business_partners` 属于 master_data 域：按 §2.0 走它的具名只读入口，
        # 不直读别人的表。返回 `None` 而不是抛错 —— 由**本方法**决定语义
        # （下面给出字段级可读错误）。
        partner = lookup_partner(tx, partner_id=int(customer_id))
        if partner is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "客户不存在", data={"field": "customer_id"}
            )
        if str(partner["partner_type"]) != "customer":
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "所选往来单位不是客户",
                data={"field": "customer_id"},
            )
        if int(partner["organization_id"]) != int(organization_id):
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "客户不属于当前企业",
                data={"field": "customer_id"},
            )

    @staticmethod
    def _assert_batch_matches_pond(
        tx: UnitOfWork, *, batch_id: int, pond_id: int
    ) -> None:
        """批次必须属于所选塘口（§2.9 的 `batch_id` 约束："须属于所选塘口"）。

        为什么这条必须在服务里查而不能交给不变量：不变量看到的是**列的值**，
        而"这个 batch 的 pond_id 是不是那个 pond"需要再查一次库。
        内核 `SameTenant` 校验的是"同企业"，不是"batch 属于那个 pond"——
        两者都必要，但不是同一条规则。
        """
        # `production_batches` 属于 production 域：按 §2.0 走它的具名只读入口。
        batch = lookup_batch(tx, batch_id=int(batch_id))
        if batch is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "批次不存在", data={"field": "batch_id"}
            )
        if int(batch["pond_id"]) != int(pond_id):
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "所选批次不属于所选塘口",
                data={"field": "batch_id"},
            )

    # -- 写 -------------------------------------------------------------------

    def create_order(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("销售单号", required=True, max_length=64),
        name: f_str("销售事项", required=True, max_length=100),
        customer_id: f_ref("客户", "partner", required=True),
        pond_id: f_ref("塘口", "pond", required=True),
        batch_id: f_ref("批次", "batch", required=True),
        species: f_str("品种", required=True, max_length=64),
        quantity: f_num("销售数量", required=True, minimum=0, maximum=MAX_AMOUNT),
        unit: f_enum("计量单位", required=True, choices=SALES_UNIT_CHOICES),
        unit_price: f_num("单价", required=True, minimum=0, maximum=MAX_AMOUNT),
        sold_at: f_str("销售日期", required=True),
        due_date: f_str("收款到期日", required=True),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        ctx.require("sales.create")

        # 归属从**塘口**解析，不接受客户端提交 organization_id/farm_id/area_id
        # （registry §0.7 规则 1 / DECISIONS.md Q7）。
        tenant = tenant_from_pond(tx, scope=scope, pond_id=int(pond_id))
        self._assert_partner_is_customer(
            tx, customer_id=int(customer_id), organization_id=tenant["organization_id"]
        )
        self._assert_batch_matches_pond(tx, batch_id=int(batch_id), pond_id=int(pond_id))

        # §2.9："due_date 不得早于 sold_at"。
        # 迁移里也有 `chk_sales_orders_dates` 兜底 —— 两层各守一件事：
        # 服务层给**字段级**错误（用户知道改哪个框），DB 保证它不可能被绕过
        # （导入/修数脚本也要守规矩）。
        require_date_order(
            _as_date(sold_at),
            _as_date(due_date),
            message="收款到期日不得早于销售日期",
        )

        # 数量与单价都要求 > 0（§2.9 两条都是 ✔ 约束）。
        # 字段声明的 `minimum=0` 允许 0，因为 0 是合法输入值但非法业务值——
        # 这是两组不同的概念，错误码也不同（前者 FIELD_INVALID，后者语义错误）。
        if _as_decimal(quantity) <= 0:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "销售数量必须大于 0", data={"field": "quantity"}
            )
        if _as_decimal(unit_price) <= 0:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "单价必须大于 0", data={"field": "unit_price"}
            )

        tx.execute(
            f"INSERT INTO {TABLE_SALES_ORDER} "
            "(organization_id, farm_id, area_id, code, name, customer_id, pond_id, "
            " batch_id, species, quantity, unit, unit_price, sold_at, due_date, note, "
            " status, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s)",
            (
                tenant["organization_id"],
                tenant["farm_id"],
                tenant["area_id"],
                code,
                name,
                int(customer_id),
                int(pond_id),
                int(batch_id),
                species,
                quantity,
                unit,
                unit_price,
                sold_at,
                due_date,
                note,
                ctx.actor.user_id,
            ),
        )
        order_id = tx.last_insert_id()
        row = self.load_order(tx, scope=scope, record_id=order_id)
        assert row is not None
        return cap.HandlerResult(
            data={
                "record": self.get_order_by_id(
                    tx, ctx, scope, path_params={"order_id": int(order_id)}
                ).data["record"]
            },
            resource_id=order_id,
            message=f"已创建销售单「{name}」（编号 {code}）",
        )

    def update_order(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        order_id: f_int("销售单 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
        name: f_str("销售事项", max_length=100),
        customer_id: f_ref("客户", "partner"),
        pond_id: f_ref("塘口", "pond"),
        batch_id: f_ref("批次", "batch"),
        species: f_str("品种", max_length=64),
        quantity: f_num("销售数量", minimum=0, maximum=MAX_AMOUNT),
        unit: f_enum("计量单位", choices=SALES_UNIT_CHOICES),
        unit_price: f_num("单价", minimum=0, maximum=MAX_AMOUNT),
        sold_at: f_str("销售日期"),
        due_date: f_str("收款到期日"),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        """编辑销售单。

        `code` **不可改**：它是业务单号，且 `uq_sales_orders_org_code` 保证同企业内
        唯一。改单号等于把审计线索重新指向另一张单据。这不是"没实现"，
        是刻意不提供（registry §2.9 的字段表也只在 `code` 上标了 `✔（create）`）。
        """
        ctx.require("sales.create")
        row = self._scope_row(tx, scope, int(order_id))

        # §4 #12：核验后只读。状态机层面用 `require_action(EDIT)`，
        # 不变量层面由 `StatusAllowsEdit` 在执行器路径上再判一次。
        SALES_ORDER_WORKFLOW.require_action(str(row["status"]), RowAction.EDIT)

        patch = _non_null(
            name=name,
            customer_id=None if customer_id is None else int(customer_id),
            pond_id=None if pond_id is None else int(pond_id),
            batch_id=None if batch_id is None else int(batch_id),
            species=species,
            quantity=quantity,
            unit=unit,
            unit_price=unit_price,
            sold_at=sold_at,
            due_date=due_date,
            note=note,
        )
        if not patch:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "没有需要更新的内容")

        if "customer_id" in patch:
            self._assert_partner_is_customer(
                tx,
                customer_id=patch["customer_id"],
                organization_id=int(row["organization_id"]),
            )

        # 塘口改了要重解归属（把单据挪到别的区域），批次也要跟着校验归属关系。
        if "pond_id" in patch:
            tenant = tenant_from_pond(tx, scope=scope, pond_id=patch["pond_id"])
            patch["organization_id"] = tenant["organization_id"]
            patch["farm_id"] = tenant["farm_id"]
            patch["area_id"] = tenant["area_id"]
            self._assert_batch_matches_pond(
                tx,
                batch_id=int(patch.get("batch_id", row["batch_id"])),
                pond_id=patch["pond_id"],
            )
        elif "batch_id" in patch:
            self._assert_batch_matches_pond(
                tx, batch_id=patch["batch_id"], pond_id=int(row["pond_id"])
            )

        effective_sold = _as_date(patch.get("sold_at") or row["sold_at"])
        effective_due = _as_date(patch.get("due_date") or row["due_date"])
        require_date_order(
            effective_sold, effective_due, message="收款到期日不得早于销售日期"
        )

        if "quantity" in patch and _as_decimal(patch["quantity"]) <= 0:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "销售数量必须大于 0", data={"field": "quantity"}
            )
        if "unit_price" in patch and _as_decimal(patch["unit_price"]) <= 0:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "单价必须大于 0", data={"field": "unit_price"}
            )

        assignments = ", ".join(f"{key}=%s" for key in patch)
        affected = tx.execute(
            f"UPDATE {TABLE_SALES_ORDER} SET {assignments}, "
            "row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s",
            [*patch.values(), ctx.actor.user_id, int(order_id), int(expected_version)],
        )
        if affected == 0:
            current = self._scope_row(tx, scope, int(order_id))
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该销售单已被他人修改，请刷新后重试",
                data={"current_version": int(current["row_version"])},
            )

        return cap.HandlerResult(
            data={
                "record": self.get_order_by_id(
                    tx, ctx, scope, path_params={"order_id": int(order_id)}
                ).data["record"]
            },
            resource_id=int(order_id),
            message="销售单已更新",
        )

    def submit_order(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        order_id: f_int("销售单 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """提交审批：draft -> submitted。"""
        return self._lifecycle(
            tx, ctx, scope,
            order_id=int(order_id),
            expected_version=int(expected_version),
            from_states=("draft",),
            to_state="submitted",
            action=RowAction.SUBMIT,
            permission="sales.create",
            message="销售单已提交审批",
        )

    def approve_order(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        order_id: f_int("销售单 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """审批：submitted -> approved。

        **不在这里写"经办人≠审批人"的判断**——那是内核 `DistinctActors` 的职责
        （registry §4 #5，推广到 13 条核验/审批能力）。服务层只做状态与版本校验。
        """
        return self._lifecycle(
            tx, ctx, scope,
            order_id=int(order_id),
            expected_version=int(expected_version),
            from_states=("submitted",),
            to_state="approved",
            action=RowAction.APPROVE,
            permission="sales.approve",
            message="销售单已审批",
            stamp_verified=True,
        )

    def cancel_order(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        order_id: f_int("销售单 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
        reason: text("取消原因", required=True, max_length=500),
    ) -> cap.HandlerResult:
        """取消：submitted / approved / partially_delivered -> cancelled。

        `reason` 必填（§2.11 与 §4 #22 都列 `sales_order.cancel` 为必填能力，
        继承旧 `sales_service.py:127-128` 的强制口径）。

        **已交付的单子能取消吗**：能。§3.5 的转移表把
        `partially_delivered -> cancelled` 列进去了。已经在途的交付不会被撤销
        （那需要退货，registry §6 明确不做），所以取消只表示"不再继续交付"。
        这个语义写在 `_lifecycle` 的 message 里，而不是靠调用方猜。
        """
        return self._lifecycle(
            tx, ctx, scope,
            order_id=int(order_id),
            expected_version=int(expected_version),
            from_states=("submitted", "approved", "partially_delivered"),
            to_state="cancelled",
            action=RowAction.CANCEL,
            permission="sales.approve",
            message="销售单已取消",
            reason=reason,
        )

    # -- 生命周期动作的统一实现 -----------------------------------------------

    def _lifecycle(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        order_id: int,
        expected_version: int,
        from_states: tuple[str, ...],
        to_state: str,
        action: RowAction,
        permission: str,
        message: str,
        reason: str | None = None,
        stamp_verified: bool = False,
    ) -> cap.HandlerResult:
        """四个动作形状相同，差别只在"从哪些状态来、到哪个状态去、需要什么权限"。

        抽成一个方法而不是复制四遍——早期版本 `sales_store.py` 里每个动作各写一遍
        UPDATE + WHERE + 冲突翻译，那是 bug 的温床（改了一处忘了另一处）。

        `reason` 与 `stamp_verified` 是可选的：`cancel` 需要原因，
        `approve` 需要盖核验章（`approved_by` / `approved_at`），
        `submit` 两者都不需要。
        """
        ctx.require(permission)
        row = self._scope_row(tx, scope, order_id)
        SALES_ORDER_WORKFLOW.require_action(str(row["status"]), action)
        if str(row["status"]) not in from_states:
            raise DomainError(
                ErrorCode.CONFLICT,
                "当前状态不允许该操作",
                data={"status": str(row["status"]), "allowed_from": list(from_states)},
            )

        extra_columns = ""
        extra_values: list[Any] = []
        if reason is not None:
            # 列名与字段名**同名**（§2.11 把早期版本的 cancellation_reason 统一为
            # reason）。所以这里直接写 reason=，不引入「字段名 -> 列名」的映射——
            # 那正是本项目要删掉的那类东西。
            extra_columns += ", reason=%s"
            extra_values.append(reason)
        if stamp_verified:
            extra_columns += ", approved_by=%s, approved_at=NOW()"
            extra_values.append(ctx.actor.user_id)
        extra_columns += ", updated_by=%s"
        extra_values.append(ctx.actor.user_id)

        placeholders = ",".join(["%s"] * len(from_states))
        affected = tx.execute(
            f"UPDATE {TABLE_SALES_ORDER} SET status=%s{extra_columns}, "
            f"row_version=row_version+1 "
            f"WHERE id=%s AND row_version=%s AND status IN ({placeholders})",
            [
                to_state,
                *extra_values,
                order_id,
                expected_version,
                *from_states,
            ],
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT, "该销售单已被他人修改，请刷新后重试"
            )

        updated = self.get_order_by_id(tx, ctx, scope, path_params={"order_id": int(order_id)})
        return cap.HandlerResult(
            data=updated.data,
            resource_id=order_id,
            message=message,
        )


# ---------------------------------------------------------------------------
# 类型转换小工具
# ---------------------------------------------------------------------------
#
# 为什么自己转而不是让字段声明去管：
# `f_date` / `f_datetime` 会由内核按 `FieldType` 解析成 `date` / `datetime`，
# 但本文件的字段声明用了 `f_str`（原因见 capabilities.py 的说明：`pymysql` 对
# `date` 对象的适配与 `str` 的精度不同，而迁移列是 `DATE`，字符串直达最省一层转换）。
# 因此这里做一次显式转换，把"可接受的日期格式"收在一处。


def _as_date(value: Any):
    """把 `str` / `date` 转成 `date`。无效值报字段级错误。"""
    from datetime import date, datetime

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text_value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text_value, fmt).date()
        except ValueError:
            continue
    raise DomainError(
        ErrorCode.FIELD_INVALID, "日期格式无效，应为 YYYY-MM-DD", data={"value": text_value}
    )


def _as_decimal(value: Any):
    from decimal import Decimal, InvalidOperation

    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DomainError(ErrorCode.FIELD_INVALID, "数值格式无效") from exc


__all__ = ["SalesOrderWriteService"]
