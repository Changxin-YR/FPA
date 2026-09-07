"""采购单的写路径：create / update / submit / approve / cancel，以及
warehouse 域的 `receipt.verify` 所需的 `apply_receipt(...)` 跨域入口。

## 写能力的统一形状（照 master_data / cost 抄）

每个写能力都做四件事，顺序固定：

    1. ctx.require(...)             —— 第三层权限校验（不信任执行器）
    2. _scope_row(...)              —— 第三层范围校验（校验的是**具体那一行**）
    3. 状态机校验                    —— require_action / require_transition
    4. 写库 + HandlerResult(resource_id=...)

第 4 步的 `resource_id` **不是可选的**：执行器要按它回读校验（`_reload_after`）。

## `__fpa_load_by_id__` 必须显式绑定（本域第一条纪律）

`kernel/runner.py::_reload_after` 要求"任何返回 `resource_id` 的写能力，其 handler
必须能解析出 `__fpa_load_by_id__(tx, *, scope, record_id)`"，否则抛
`INTERNAL_ERROR`。实测缺口：`domains/master_data/ponds_write.py` 的写能力**没有**
设置它，只有 `tools/*_e2e.py` 在夹具里手动补挂 —— 也就是**生产路径下 master_data
的写能力全都会 500**（已立项 t13）。本文件在模块底部显式绑定，
且 `tools/purchase_e2e.py` 断言"不靠夹具补挂也能通过"。

## 三处刻意的"不做"

**1. `purchase_order.update` 不接受 `status`。**
状态只能经 `submit` / `approve` / `cancel` 三个动作推进 —— 让 update 能改状态
等于让调用方跳过流程（例如把一个 submitted 的单子直接改成 approved）。
这不靠"客户端自觉"：字段声明里根本没有它。

**2. `approve` / `cancel` 不"先查再断言"式地重复实现合规规则。**
`DistinctActors` 由能力声明挂载（内核在同一事务内执行），服务里**不再写第二遍**
`if int(row["created_by"]) == ctx.actor.user_id`。理由：两个检查点就是"两处描述
同一件事"，而本项目的纪律是删掉一处。服务的状态机校验（`require_action`）
与不变量**不是重复** —— 前者是"这一步在流程上是否可行"（给出带可用动作的中文
文案），后者是"变更本身是否合法"（`StateTransition` 看转移表）。两者语义不同。
（对照：`master_data/ponds_write.py::verify_pond` 把 `created_by <> %s` 又写进了
UPDATE 的 WHERE —— 那是它当时的取舍；本域按 负责人 的裁决只保留一个检查点。）

**3. `create` 不做"未审批不得收货"那类跨域判断。**
§4 #3 的强制点是 warehouse 的 `receipt.create` / `receipt.verify`，
本域只提供被引用的表与状态字面值（`PURCHASE_ORDER_RECEIVABLE_STATUSES`）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fpa.kernel import capability as cap
from fpa.kernel.errors import DomainError, ErrorCode, not_found
from fpa.kernel.fields import f_date, f_int, f_num, f_ref, f_str, text
from fpa.kernel.scope import Scope
from fpa.kernel.uow import UnitOfWork
from fpa.kernel.workflow import RowAction

from .orders import PurchaseOrderService
from .service import (
    MAX_AMOUNT,
    PURCHASE_ORDER_WORKFLOW,
    TABLE_PURCHASE_ORDER,
    assert_row_in_scope,
    decorate_order,
    require_date_order,
    tenant_from_scope,
)

#: 乐观锁版本参数的**统一声明**。
#:
#: ## 为什么必须声明成字段（这是踩过的真坑，不是预防性设计）
#:
#: registry §0.7 规则 3 把 `expected_version` 定为**所有 update 与 action 能力的
#: 必带参数**（不出现在表单，前端自动带）。它要真的到达处理器，就必须出现在
#: `Capability.fields` 里 —— 执行器调用服务前会先过 `validate_payload()`，
#: 而那个函数**只保留声明过的字段**。
#:
#: 后果（cost 域实测）：处理器签名里有 `expected_version`、字段表里没有它 ->
#: `validate_payload` 丢掉 -> `handler(**kwargs)` 缺参数 ->
#: `TypeError: missing 1 required positional argument`。**校验层与处理器对
#: "可接受的参数集合"有两份不同的答案**，而报错是 TypeError 而不是业务错误，
#: 排查方向会被引向"服务签名写错了"。
_EXPECTED_VERSION = f_int(
    "乐观锁版本",
    required=True,
    minimum=1,
    help="当前记录的版本号：与库里不一致会被拒绝（防止两人同时改同一条）",
)


def _non_null(**fields: Any) -> dict[str, Any]:
    """只保留**提交了**的字段（None 视为"没提交"）。

    更新场景下"没提交"与"提交了 None"含义不同（前者不动、后者清空），
    而 `validate_payload` 会把缺失字段统一填成 None，所以在这里区分。
    """
    return {key: value for key, value in fields.items() if value is not None}


class PurchaseOrderWriteService(PurchaseOrderService):
    """采购单的写路径。

    继承 `PurchaseOrderService` 复用 `_scope_row` / `_present` / `load_order` ——
    读写共用同一套范围校验与派生逻辑，避免"读看到一个样、写看到另一个样"。
    """

    # -- 创建 ----------------------------------------------------------------

    def create_order(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("采购单号", required=True, max_length=64),
        name: f_str("采购事项", required=True, max_length=100),
        supplier_id: f_ref("供应商", "partner", required=True),
        material_id: f_ref("采购物料", "material", required=True),
        warehouse_id: f_ref("收货仓", "warehouse", required=True),
        quantity: f_num("采购数量", required=True, minimum=0.001, maximum=float(MAX_AMOUNT)),
        unit_price: f_num("单价", required=True, minimum=0.0001, maximum=float(MAX_AMOUNT)),
        expected_delivery_date: f_date("预计到货日期", required=True),
        due_date: f_date("付款到期日", required=True, help="不得早于预计到货日期"),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        """新建采购单（初始状态 draft，**不接受客户端指定状态**）。

        ## 服务端决定的三件事

        * `status = 'draft'` —— 让客户端指定等于让调用方跳过流程
          （例如直接建成 approved）。
        * 分租键 `organization_id` / `farm_id` / `area_id` —— 由当前账号的
          DataScope 解析（Q7 裁决：这三个字段**不接受客户端提交**，
          否则"用户只能写自己区域的数据"退化成"用户自报家门"）。
        * `approved_by` / `approved_at` —— 保持 NULL，只有 approve 能写。

        ## 引用的存在性在本方法内校验（不只在声明里）

        `SameTenant` 不变量也会查物料，但它查的是"同企业"；这里先给出**字段级**
        的可读错误（`data.field = material_id`），并且顺带按 `scope.allows_row`
        挡住跨范围的引用。父类 `test_purchase_orders_scope_area` 覆盖这一点。
        """
        ctx.require("purchase.create")

        # ① 日期顺序（旧 `purchase_service.py:52-60` 的 `_validate_dates`）
        require_date_order(expected_delivery_date, due_date)

        # ③ 供应商：必须是当前企业下的 supplier（`partner_type` 过滤）。
        #    只查 id/name 不够 —— 客户表与供应商表是同一张 `business_partners`，
        #    不判类型就能把客户当供应商用。
        supplier = tx.query_one(
            "SELECT id, name, partner_type, organization_id, farm_id, area_id "
            "FROM business_partners WHERE id = %s",
            (int(supplier_id),),
        )
        if supplier is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "供应商不存在", data={"field": "supplier_id"}
            )
        assert_row_in_scope(scope, supplier, "该供应商")
        if str(supplier.get("partner_type")) != "supplier":
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "所选往来单位不是供应商",
                data={"field": "supplier_id", "partner_type": str(supplier.get("partner_type"))},
            )

        # ④ 物料与收货仓：存在性 + 数据范围。
        #    状态有效性（物料须 verified）由 §4 #18 的 `ReferencedStatus` 不变量负责 ——
        #    不在这里再判一次，那就是第二个检查点。
        #
        #    `warehouse_id` 指向 warehouse 域的表（006 迁移）。这里**不查它的表名** ——
        #    ROLLOUT_CONTRACT §2 的权威表名表（t16）未落，猜表名会抛表不存在，
        #    把尚未定稿伪装成实现缺陷。存在性与同企业由 `SameTenant` 不变量负责，
        #    它按内核的表名参数读取，与 t16 的结论共用同一个字面值。
        material = tx.query_one(
            "SELECT id, name, unit, organization_id, farm_id, area_id "
            "FROM materials WHERE id = %s",
            (int(material_id),),
        )
        if material is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "采购物料不存在", data={"field": "material_id"}
            )
        assert_row_in_scope(scope, material, "该物料")

        # ⑤ 分租键：从数据范围解析，**失败即报错**（fail-closed，绝不填默认值）。
        #    放在引用行之后：全场范围（`allow_all`）的账号范围里没有具体区域，
        #    此时以**被引用的物料**所在区域定归属（见 `tenant_from_scope` 的 fallback 段）。
        tenant = tenant_from_scope(tx, scope, fallback=material)

        try:
            tx.execute(
                f"INSERT INTO {TABLE_PURCHASE_ORDER} "
                "(organization_id, farm_id, area_id, code, name, supplier_id, material_id, "
                " warehouse_id, quantity, unit_price, expected_delivery_date, due_date, "
                " note, status, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s)",
                (
                    tenant["organization_id"],
                    tenant["farm_id"],
                    tenant["area_id"],
                    code,
                    name,
                    int(supplier_id),
                    int(material_id),
                    int(warehouse_id),
                    quantity,
                    unit_price,
                    expected_delivery_date,
                    due_date,
                    note,
                    ctx.actor.user_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            # §4 #19 的兜底路径：数据库唯一键**先于**应用层 `UniqueCode` 生效
            # （后者在写库之后执行），所以重复单号会以 1062 到这里。
            #
            # 判定用**键名**（`uq_purchase_orders_org_code`）而不是异常消息文本 ——
            # registry §4 #19 点名禁止早期版本的 `if key not in str(exc)`
            # （`早期版本 production_store.py:142-146`），那种判定随 MySQL 版本变化。
            # 这里与 cost/master_data 的既有写法一致。
            if "uq_purchase_orders_org_code" in str(exc):
                raise DomainError(
                    ErrorCode.CONFLICT,
                    f"采购单号「{code}」已存在，请换一个",
                    data={"rule": "UNIQUE_CODE", "field": "code"},
                ) from exc
            raise
        order_id = tx.last_insert_id()
        row = self.load_order(tx, scope=scope, record_id=order_id)
        assert row is not None
        total = Decimal(str(quantity)) * Decimal(str(unit_price))
        return cap.HandlerResult(
            data={
                "record": self._present(row, ctx.actor.permissions),
                # 执行器把这里的键合并进不变量的 payload（`runner._invariant_extra`）。
                #
                # 为什么必须回传 `organization_id`：`UniqueCode(scope=("organization_id",))`
                # 在 payload 里取不到范围列时**显式报错**（"不允许静默放宽成全表唯一"），
                # 而分租键是服务算出来的，请求体里没有。
                "_invariant_context": {
                    "organization_id": tenant["organization_id"],
                    "farm_id": tenant["farm_id"],
                    "area_id": tenant["area_id"],
                },
            },
            resource_id=order_id,
            message=(
                f"已创建采购单「{name}」（{code}）：{material['name']} "
                f"{quantity}{material['unit']}，金额 {total}"
            ),
        )

    # -- 修改 ----------------------------------------------------------------

    def update_order(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        order_id: f_int("采购单 ID", required=True),
        expected_version: _EXPECTED_VERSION,
        name: f_str("采购事项", max_length=100),
        supplier_id: f_ref("供应商", "partner"),
        material_id: f_ref("采购物料", "material"),
        warehouse_id: f_ref("收货仓", "warehouse"),
        quantity: f_num("采购数量", minimum=0.001, maximum=float(MAX_AMOUNT)),
        unit_price: f_num("单价", minimum=0.0001, maximum=float(MAX_AMOUNT)),
        expected_delivery_date: f_date("预计到货日期"),
        due_date: f_date("付款到期日"),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        """编辑采购单。

        **只在 draft 状态可改**：`PURCHASE_ORDER_WORKFLOW.require_action(EDIT)`
        在 `draft` 之外都会拒绝（其他状态的 `actions` 里没有 `edit`）。
        这与 §4 #12「核验后只读」是同一件事的两种表述 —— 不变量
        `StatusAllowsEdit` 也在能力声明里（它按 `before` 的快照判定，
        与状态机动作集合是**两个独立的失效模式**：一个看状态码集合，
        一个看该状态的可用动作）。

        `code` **不可改**：它是 §4 #19 的唯一性载体，改它等于换一张单。
        """
        ctx.require("purchase.create")
        row = self._scope_row(tx, scope, int(order_id))
        PURCHASE_ORDER_WORKFLOW.require_action(str(row["status"]), RowAction.EDIT)

        patch = _non_null(
            name=name,
            supplier_id=None if supplier_id is None else int(supplier_id),
            material_id=None if material_id is None else int(material_id),
            warehouse_id=None if warehouse_id is None else int(warehouse_id),
            quantity=quantity,
            unit_price=unit_price,
            expected_delivery_date=expected_delivery_date,
            due_date=due_date,
            note=note,
        )
        if not patch:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "没有需要更新的内容")

        # 日期顺序要按**改动后**的组合判，而不是只看本次提交的字段：
        # 只改 due_date 时，必须拿库里的 expected_delivery_date 一起比。
        require_date_order(
            patch.get("expected_delivery_date") or row["expected_delivery_date"],
            patch.get("due_date") or row["due_date"],
        )

        if "supplier_id" in patch:
            supplier = tx.query_one(
                "SELECT id, partner_type FROM business_partners WHERE id = %s",
                (patch["supplier_id"],),
            )
            if supplier is None:
                raise DomainError(
                    ErrorCode.FIELD_INVALID, "供应商不存在", data={"field": "supplier_id"}
                )
            if str(supplier.get("partner_type")) != "supplier":
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "所选往来单位不是供应商",
                    data={"field": "supplier_id"},
                )
        if "material_id" in patch:
            material = tx.query_one(
                "SELECT id, organization_id, farm_id, area_id FROM materials WHERE id = %s",
                (patch["material_id"],),
            )
            if material is None:
                raise DomainError(
                    ErrorCode.FIELD_INVALID, "采购物料不存在", data={"field": "material_id"}
                )
            assert_row_in_scope(scope, material, "该物料")

        assignments = ", ".join(f"{key}=%s" for key in patch)
        affected = tx.execute(
            f"UPDATE {TABLE_PURCHASE_ORDER} SET {assignments}, "
            "row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s AND status='draft'",
            [*patch.values(), ctx.actor.user_id, int(order_id), int(expected_version)],
        )
        if affected == 0:
            current = self._scope_row(tx, scope, int(order_id))
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该采购单已被他人修改或状态已变化，请刷新后重试",
                data={"current_version": int(current["row_version"])},
            )

        updated = self.load_order(tx, scope=scope, record_id=int(order_id))
        assert updated is not None
        return cap.HandlerResult(
            data={
                "record": self._present(updated, ctx.actor.permissions),
                # `StatusAllowsEdit` 与 `StateTransition` 都按 `before`/payload 判定，
                # 而 `status` 不在本能力的字段集里（update 不允许改状态），
                # 所以 `StateTransition` 会跳过 —— 这是它设计好的"无变更即放行"。
                "_invariant_context": {"status": str(updated["status"])},
            },
            resource_id=int(order_id),
            message="采购单已更新",
        )

    # -- 生命周期动作 ----------------------------------------------------------

    def submit_order(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        order_id: f_int("采购单 ID", required=True),
        expected_version: _EXPECTED_VERSION,
    ) -> cap.HandlerResult:
        """提交审批：draft -> submitted。权限码是 `purchase.create`（非同名派生）。"""
        return self._lifecycle(
            tx, ctx, scope,
            order_id=int(order_id),
            expected_version=int(expected_version),
            from_states=("draft",),
            to_state="submitted",
            action=RowAction.SUBMIT,
            permission="purchase.create",
            message="采购单已提交审批",
        )

    def approve_order(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        order_id: f_int("采购单 ID", required=True),
        expected_version: _EXPECTED_VERSION,
    ) -> cap.HandlerResult:
        """审批：submitted -> approved。

        同时写 `approved_by` / `approved_at` —— `DistinctActors`
        （经办人 ≠ 审批人）由能力声明挂载，本方法**不再写第二遍判断**。
        数据库层另有 `chk_purchase_payments_distinct_actors` 那类约束保护单据类，
        采购单的对应保护就是不变量本身 + 字段的只读性。
        """
        return self._lifecycle(
            tx, ctx, scope,
            order_id=int(order_id),
            expected_version=int(expected_version),
            from_states=("submitted",),
            to_state="approved",
            # `APPROVE` 而不是 `VERIFY`：本域审批的动作词是 approve
            # （kernel `RowAction.APPROVE` 已存在，registry §3.4 的 submitted 行
            #  写的也是 view, approve, cancel）。用 verify 会被状态机拒绝 ——
            #  实测报「待审批的记录不支持「verify」操作」。
            action=RowAction.APPROVE,
            permission="purchase.approve",
            message="采购单已审批，可以收货",
            approved_by=ctx.actor.user_id,
        )

    def cancel_order(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        order_id: f_int("采购单 ID", required=True),
        expected_version: _EXPECTED_VERSION,
        reason: text(
            "取消原因", max_length=500,
            help="取消是不可逆的对外承诺撤销，必须留下原因",
        ),
    ) -> cap.HandlerResult:
        """取消：submitted / approved / partially_received -> cancelled。

        `reason` 的必填由 §4 的 `RequiredWhen(when_field="status",
        when_values=("cancelled",), required_fields=("reason",))` 声明
        （旧 `purchase_service.py:151-153`）。这里同时**无条件**要求它 ——
        因为本能力的语义就是"取消"，没有别的目标状态；`RequiredWhen` 的存在是为了
        让这条规则在**声明处**可见（而不是靠"参数没有默认值"暗示）。

        数据库层另有 `chk_purchase_orders_reason` 保证 cancelled 行**不可能**
        没有原因（导入/修数脚本也绕不过）。
        """
        reason = (reason or "").strip()
        if not reason:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR, "取消原因不能为空", data={"field": "reason"}
            )
        return self._lifecycle(
            tx, ctx, scope,
            order_id=int(order_id),
            expected_version=int(expected_version),
            from_states=("submitted", "approved", "partially_received"),
            to_state="cancelled",
            action=RowAction.CANCEL,
            permission="purchase.approve",
            message=f"采购单已取消：{reason}",
            reason=reason,
        )

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
        approved_by: int | None = None,
        reason: str | None = None,
    ) -> cap.HandlerResult:
        """生命周期动作的统一实现。

        三个动作形状相同，差别只在"从哪些状态来、到哪个状态去、需要什么权限、
        顺带写哪些列"。抽成一个方法而不是复制三遍 —— 早期版本每个动作各写一遍
        UPDATE + WHERE + 冲突翻译，那是 bug 的温床（改了一处忘了另一处）。
        """
        ctx.require(permission)
        row = self._scope_row(tx, scope, order_id)
        PURCHASE_ORDER_WORKFLOW.require_action(str(row["status"]), action)
        if str(row["status"]) not in from_states:
            raise DomainError(
                ErrorCode.CONFLICT,
                "当前状态不允许该操作",
                data={"status": str(row["status"]), "allowed_from": list(from_states)},
            )

        sets = ["status=%s", "row_version=row_version+1", "updated_by=%s"]
        params: list[Any] = [to_state, ctx.actor.user_id]
        if approved_by is not None:
            sets.extend(["approved_by=%s", "approved_at=NOW()"])
            params.append(approved_by)
        if reason is not None:
            sets.append("reason=%s")
            params.append(reason)

        placeholders = ",".join(["%s"] * len(from_states))
        # 乐观锁 + 状态前置条件**都放在 WHERE 里**（原子占位）：
        # 并发下两个请求会都通过应用层检查，只有 WHERE 才能保证只有一个改到行 ——
        # 这正是早期版本缺失的那个正确写法。
        params.extend([order_id, expected_version, *from_states])
        affected = tx.execute(
            f"UPDATE {TABLE_PURCHASE_ORDER} SET {', '.join(sets)} "
            f"WHERE id=%s AND row_version=%s AND status IN ({placeholders})",
            params,
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该采购单状态已变化或已被他人修改，请刷新后重试",
            )

        updated = self.load_order(tx, scope=scope, record_id=order_id)
        assert updated is not None
        return cap.HandlerResult(
            data={
                "record": self._present(updated, ctx.actor.permissions),
                # ★ 必须回传目标状态，否则 `StateTransition` 会**静默跳过**。
                #
                # `kernel/invariants.py::StateTransition` 的判定是："本次请求确实提交了
                # 状态字段（`field_name`）"才校验；`status` 不在本能力的字段集里
                # （由 action 决定，不由客户端提交），而 `before` 里读到的是**写库后**的
                # 同一个值 → `target == current` → 按"无变更即放行"直接返回。
                # 结果就是"声明了 #14 却永远不会触发"。
                #
                # 回传 `status` = **本次要到达的目标状态**，让不变量真的去查转移表：
                #   submitted -> approved 在表里  -> 放行
                #   draft     -> approved 不在表里 -> 拒绝
                # `from` 由 `before` 提供（执行器在写库前读的那份快照，仍是旧状态）。
                "_invariant_context": {"status": to_state},
            },
            resource_id=order_id,
            message=message,
        )


# ---------------------------------------------------------------------------
# 回读函数绑定（`__fpa_load_by_id__`）
#
# `CapabilityRunner._reload_after` 在**提交前**调它回读，确认"写入真的落了库"
# （`docs/WRITE_CONTRACT.md` 规则 2）。**忘了绑定的后果是显式的**：
#
#     INTERNAL_ERROR: 能力 X 声称写入成功但没有提供回读函数，无法确认写入结果
#
# 这条"忘了就报错"的设计是刻意的：早期版本此刻会返回 {"id": ...} 之类的占位数据，
# 看起来"有数据"但来源不可追溯。
#
# 为什么用模块级赋值而不是装饰器：注册表里挂的是**类上的未绑定函数**
# （`PurchaseOrderWriteService.create_order`），`__fpa_load_by_id__` 必须挂在
# **同一个函数对象**上；装饰器会让"能力声明里的 handler"与"挂载回读函数的对象"
# 变成两个不同的东西，而 `runner._resolve()` 只认前者。
#
# 这五行就是 t13 那个缺陷（master_data 的写能力全都会 500）在本域**不存在**的
# 全部证据 —— `tools/purchase_e2e.py` 会断言"不靠夹具补挂也能通过"。
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# 跨域入口：warehouse 的 `receipt.verify` 调用它推进采购单状态
# （`ROLLOUT_CONTRACT.md` §2）
# ---------------------------------------------------------------------------

def apply_receipt(
    tx: UnitOfWork,
    ctx: Any = None,
    *,
    purchase_order_id: int,
    receipt_document_id: int | None = None,
    actor_id: int = 0,
    happened_at: Any = None,
) -> dict[str, Any]:
    """按**已核验到货**的累计量推进采购单状态。只做推进与回报，**不做判定、不报错**。

    ## 为什么不做判定（评审结论）

    「累计到货不得超过采购数量」（registry §4 #9）的强制点是 warehouse 域
    `receipt.verify` 上的 `CumulativeWithin` —— 执行器会在**同一个事务内**拦住并
    整体回滚。如果本函数再返回一个"超量"标志让调用方抛错，就变成
    "不变量拦一次 + 手工再拦一次"，正是本项目要根除的"两处描述同一件事"。
    所以本函数**只回报事实**：推进到哪个状态、累计到货多少、单据数量多少。

    早期实现（`早期版本 purchase_posting.py:35-38`）是"到货时改订单状态"，
    状态推导原样继承：

        累计 = 0                    -> 保持 approved（本次只核验了草稿行？不改）
        0 < 累计 < quantity         -> partially_received
        累计 >= quantity            -> fully_received

    ## 为什么它自己加行锁

    并发下两张到货单同时核验，两边都会读到"旧累计"而各自认为还没到齐 ——
    于是订单永远停在 `partially_received`。`SELECT ... FOR UPDATE` 让第二个请求
    等第一个提交后再读，这正是早期版本 `FOR UPDATE` 写法的正确之处（要继承）。

    ## 为什么**不能**在 `receipt.create`（草稿）阶段调用

    §4 #3「未审批不得收货」要求 `receipt.create` / `receipt.verify` 都校验采购单
    处于 `approved` / `partially_received`。草稿阶段只**校验**，不推进状态。

    ## 累计量的查询形态（已定稿，`ROLLOUT_CONTRACT.md` §2A）

    表名与过滤条件由 warehouse 域的 t16 定稿（§2A.2/§2A.4）：

        SELECT COALESCE(SUM(total_quantity),0) FROM warehouse_documents
         WHERE purchase_order_id = %s AND doc_type = 'receipt' AND status = 'verified'

    三点都不是装饰：
      * `purchase_order_id` 在**单头**（刻意去规范化）—— 所以 #9 保持**单表可表达**，
        不需要 JOIN 明细；代价是约定"**一张到货单只对一张采购单**"。
      * `doc_type='receipt'` —— 单头表被到货与领用**共用**，漏掉它会把领用单也算成到货。
      * `status='verified'` —— 只算已核验的到货，草稿到货不得推进订单状态。

    与 `receipt.verify` 上的 `CumulativeWithin(children_table="warehouse_documents")`
    **共用同一个事实源**（不是两份各自维护的 SQL）。

    `happened_at` 只用于**审计与调用形状一致**（本函数不写账目，所以不需要它）；
    `create_from_receipt` 用它决定应付的入账日。

    """
    order = tx.query_one(
        f"SELECT * FROM {TABLE_PURCHASE_ORDER} WHERE id = %s FOR UPDATE",
        (int(purchase_order_id),),
    )
    if order is None:
        raise not_found("采购单")
    if ctx is not None:
        assert_row_in_scope(ctx.scope, order, "该采购单")

    # 累计到货量：形态的**权威来源是 `docs/ROLLOUT_CONTRACT.md` §2A.4**（t16 交付的
    # 权威表名/列名表，负责人 认定的唯一口径）：
    #
    #   SELECT COALESCE(SUM(total_quantity),0) FROM warehouse_documents
    #    WHERE organization_id=%s AND purchase_order_id=%s
    #      AND doc_type='receipt' AND status='verified'
    #
    # ★ 它与 `receipt.verify` 上 `CumulativeWithin` 的声明**逐字相同**——这是刻意的：
    #   §2A 存在的全部理由就是消灭"同一段 SQL 的两份副本"，而两份只要有一个条件
    #   不同就不再是同一个事实源，且**这类差异不会让任何单域 e2e 变红**。
    #
    # `organization_id` 这一条我要如实说明：`purchase_order_id` 是代理主键、全局唯一，
    # 所以**在合法数据上它不改变结果**（企业的单不可能挂别家的到货单）。加它的理由
    # 是"两条查询字面一致"这条契约要求，以及把"跨租户读取"的意图写进 SQL——
    # 而**不是**它防住了什么越权（租户边界目前不由 DataScope 行使，我已单独上报）。
    #
    # 四个 WHERE 条件都不是装饰：
    #   * `doc_type='receipt'` —— 单头表被到货与领用**共用**（006 用 doc_type
    #     一个枚举列换掉了一整份重复）。漏掉它会把领用单也计进到货量。
    #     （领用单的 `purchase_order_id` 由 `chk_warehouse_documents_supplier`
    #     约束为 NULL，所以严格说它天然不计入；但**不能靠这个**——那是另一个约束的
    #     副作用，不是本查询的保证。写出来才不依赖别人表上的 CHECK。）
    #   * `status='verified'` —— 只算**已核验**的到货。草稿到货不该推进订单状态，
    #     否则一张没核验的单就能把采购单标成全部到货。
    #   * `purchase_order_id` —— 006 明确约定"一到货单只对一张采购单"，
    #     `purchase_order_id` 放在**单头**，因此"某采购单累计到货量"是**单表聚合**。
    #
    # `FOR UPDATE`：与前面锁 `purchase_orders` 那行配套 —— 两张到货单并发核验时，
    # 第二个请求要等第一个提交后再读，否则两边都读到旧累计、都认为"还没到齐"，
    # 采购单会永远停在 `partially_received`。
    #
    # `warehouse_documents` 是**跨域读**：契约 §2 明确豁免"内核按表名读取"这类
    # 表名参数化的读取（`CumulativeWithin` 就是这么读的）。这里与
    # `receipt.verify` 上声明的 `CumulativeWithin(children_table="warehouse_documents",
    # count_field="total_quantity")` **字面一致** —— 两边共用同一个事实源，
    # 不是两份各自维护的 SQL。
    counted = tx.query_one(
        "SELECT COALESCE(SUM(total_quantity), 0) AS received "
        "FROM warehouse_documents "
        "WHERE organization_id = %s AND purchase_order_id = %s "
        "AND doc_type = 'receipt' AND status = 'verified' "
        "FOR UPDATE",
        (order["organization_id"], int(purchase_order_id)),
    )
    cumulative = Decimal(str((counted or {}).get("received") or 0))
    ordered = Decimal(str(order["quantity"]))
    previous = str(order["status"])

    # 状态推导（旧 `purchase_posting.py:35-38`，逐字继承）：
    #   累计 = 0                     -> 保持 approved
    #   0 < 累计 < quantity          -> partially_received
    #   累计 >= quantity             -> fully_received
    if cumulative <= 0:
        target_status = previous
    elif cumulative < ordered:
        target_status = "partially_received"
    else:
        target_status = "fully_received"

    changed = target_status != previous
    if changed:
        # 目标状态必须过状态机 —— 状态机只有一份，本函数不自己造转移表。
        # `require_transition` 会在非法时列出合法目标（"已取消只能变更为：…"），
        # 那比 "if status not in (...)" 的裸判断可读得多。
        PURCHASE_ORDER_WORKFLOW.require_transition(
            previous, target_status, action="receipt.verify"
        )
        tx.execute(
            f"UPDATE {TABLE_PURCHASE_ORDER} "
            "SET status=%s, row_version=row_version+1, updated_by=%s WHERE id=%s",
            (target_status, int(actor_id), int(purchase_order_id)),
        )

    return {
        "id": int(order["id"]),
        "code": str(order["code"]),
        "status": target_status,
        "previous_status": previous,
        "changed": changed,
        "ordered_quantity": str(ordered),
        "cumulative_received": str(cumulative),
    }


__all__ = ["PurchaseOrderWriteService", "apply_receipt"]
