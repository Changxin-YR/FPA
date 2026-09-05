"""仓储域的写路径：receipt.create / receipt.verify / issue.create / issue.verify。

## 写能力的统一形状（照 `domains/master_data/ponds_write.py` 的四步）

    1. ctx.require(...)           第三层权限校验（不信任调用方）
    2. _scope_row(...)            第三层范围校验（校验的是**具体那一行**）
    3. 状态机 require_action      非法转移在服务层就挡住
    4. 写库 + HandlerResult(resource_id=...)

第 4 步的 `resource_id` 不是可选的：执行器要按它回读校验（`WRITE_CONTRACT` 规则 2），
少了它 `executed` 就退化成"我们调用了 UPDATE"而不是"数据真的落库了"。

## 两条核验路径的差异（这是本文件最需要注意的地方）

**`receipt.verify` 走 `apply_movement(create_lot=True)`**：入库必须先建批次，
"建批"这个动作本身就是入库的一部分。

**`issue.verify` 走 `apply_movement(create_lot=False)`**：出库**不能凭空造批次**，
否则"库存不足"可以被"先把批次造出来"绕过。

两条路径都**不自己写 `inventory_ledger`**——账本的唯一写入口是
`warehouse.ledger.apply_movement`，四个域共用（`ROLLOUT_CONTRACT.md` §2）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fpa.kernel import capability as cap
from fpa.kernel.errors import DomainError, ErrorCode, not_found
from fpa.kernel.fields import f_date, f_datetime, f_int, f_num, f_ref, f_str, text
from fpa.kernel.scope import Scope
from fpa.kernel.uow import UnitOfWork
from fpa.kernel.workflow import RowAction

from .inventory import WarehouseQueryService
from .ledger import SOURCE_ISSUE, SOURCE_RECEIPT, apply_movement, ledger_source_ref
from .service import DOC_STATUS_WORKFLOW, DOC_TYPES


#: MySQL 的唯一键冲突错误号（1062 ER_DUP_ENTRY / 1586）。
_DUPLICATE_ERRNOS = frozenset({1062, 1586})


def _is_duplicate_key(exc: BaseException) -> bool:
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int) and args[0] in _DUPLICATE_ERRNOS:
        return True
    text = str(exc)
    return "Duplicate entry" in text or "duplicate key" in text.lower()


def _ensure_code_available(
    tx: UnitOfWork, *, organization_id: int, doc_type: str, code: str
) -> None:
    """前置查重：`(organization_id, doc_type, code)` 已被占用时直接 409。

    ## 为什么必须前置查一次（这是一处真实的执行顺序约束）

    唯一键是数据库层的正确性底线，但**它触发在 `INSERT` 那一刻**——而
    `run_invariants()` 跑在 `_call_service()` **之后**（`runner.py:250-263`）。
    也就是说 `UniqueCode` 这条不变量**在当前执行顺序下永远不可能抢先拦住重复编码**：
    重复会先在 INSERT 抛 `IntegrityError`，服务方法不返回、`run_invariants` 根本不执行，
    用户拿到的是 500 而不是"该编号已存在"。

    所以这里补一次前置查重（把可读的 409 交给用户），`UniqueCode` 声明仍然保留并
    作为同一份规则的**声明式台账**；下面 `except` 里的翻译则兜住并发窗口：
    两个请求同时通过前置查重时，后到的那个仍会拿到 409 而不是 500。
    """
    existing = tx.query_one(
        "SELECT id FROM warehouse_documents "
        "WHERE organization_id=%s AND doc_type=%s AND code=%s",
        (int(organization_id), doc_type, code),
    )
    if existing is not None:
        raise DomainError(
            ErrorCode.CONFLICT,
            f"{'到货单号' if doc_type == 'receipt' else '出库单号'}「{code}」已存在，请换一个",
            data={
                "rule": "UNIQUE_CODE",
                "field": "code",
                "existing_id": int(existing["id"]),
            },
        )


def _scope_invariant_context(warehouse: dict[str, Any], doc_type: str) -> dict[str, Any]:
    """给 `UniqueCode`（§4 #19）提供唯一键的完整范围列。

    ## 为什么必须由服务回传

    `create` 请求体里**没有** `organization_id`（§0.7 规则 1 / Q7 裁决：分租键由
    服务端解析，不接受客户端提交）。但唯一键是
    `uq_warehouse_documents_org_type_code (organization_id, doc_type, code)`，
    少了 `organization_id`，`UniqueCode` 会**拒绝**而不是静默放宽
    （kernel/invariants.py 的原文："否则唯一性会被静默放宽到全表"）——
    这正是它该有的行为，于是补全的责任落在服务端，也就是这里。

    `doc_type` 不是分租列，但它也是这个唯一键的一段：**到货单 RCV-001 与领用单
    RCV-001 是两个合法编号**，不带上它会把它们误判成重复。
    """
    return {
        "organization_id": warehouse["organization_id"],
        "doc_type": doc_type,
    }


def _non_null(**fields: Any) -> dict[str, Any]:
    """只保留**提交了**的字段（`None` 视为"没提交"）。

    与 `validate_payload` 把缺失字段填成 `None` 配套：后者统一了"缺字段 = None"，
    这里把"没提交"与"提交了 None"区分开——更新场景下两者含义不同。
    """
    return {key: value for key, value in fields.items() if value is not None}


def _as_decimal(value: Any, *, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise DomainError(
            ErrorCode.FIELD_INVALID, f"{field} 必须是数字", data={"field": field}
        ) from exc


class WarehouseDocumentService(WarehouseQueryService):
    """到货单与领用单的读写。

    继承 `WarehouseQueryService` 以复用 `_scope_row` / `_decorate`——
    读写共用同一套范围与派生态，避免"读看到一个样、写看到另一个样"。
    """

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _scope_document(tx: UnitOfWork, scope: Scope, document_id: int) -> dict[str, Any]:
        row = tx.query_one(
            """
            SELECT d.*, w.name AS warehouse_name, w.status AS warehouse_status,
                   m.name AS material_name, m.status AS material_status,
                   m.unit AS material_unit
            FROM warehouse_documents AS d
            LEFT JOIN warehouses AS w ON w.id = d.warehouse_id
            LEFT JOIN materials AS m ON m.id = d.material_id
            WHERE d.id = %s
            """,
            (int(document_id),),
        )
        if row is None:
            raise not_found("仓储单据")
        if not scope.allows_row(row):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED, "该单据不在当前账号的数据范围内"
            )
        return row

    @staticmethod
    def _require_doc_type(row: dict[str, Any], expected: str) -> None:
        """单据类型必须匹配。

        `warehouse_documents` 两类单据共表（`doc_type` 区分），所以按主键取回来的行
        **可能是另一类单据**。少这一层校验，`receipt.verify` 就能核验一张领用单，
        而它会把 `doc_type='receipt'` 写进账本——账本与单据从此不一致。
        """
        if str(row.get("doc_type")) != expected:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                f"该单据不是{'到货单' if expected == 'receipt' else '领用单'}",
                data={"field": "doc_type", "actual": str(row.get("doc_type"))},
            )

    def _load_lines(self, tx: UnitOfWork, document_id: int) -> list[dict[str, Any]]:
        """单据明细。单头带了 `total_quantity`，但仍以明细求和为准并在核验时回写。"""
        return tx.query_all(
            "SELECT * FROM warehouse_document_lines WHERE document_id = %s ORDER BY line_no",
            (int(document_id),),
        )

    def _doc_source_ref(self, row: dict[str, Any]) -> str:
        return ledger_source_ref("warehouse", str(row["code"]))

    def list_documents(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        query: dict[str, Any] | None = None,
        doc_type_forced: str | None = None,
        resource: str | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """仓储单据列表。`doc_type_forced` / `resource` 供两个列表页复用（内部参数）。

        * `/api/v1/receipts`（仓储单据 = 到货单）→ `doc_type_forced="receipt"`
        * `/api/v1/issues`（领用出库）→ `doc_type_forced="issue"`，且行内动作按 `issue` 解析

        ## 为什么补这一条

        `warehouse_document` 原先**只有** `.get`（`/api/v1/receipts/{receipt_id}`）。
        而前端导航的判据是"该资源有一条可见的读能力"——`.get` 也是读能力，于是
        「仓储单据」出现在菜单里；点进去打的是列表地址 `GET /api/v1/receipts`，
        而那条路由从未注册 ⇒ **405「请求方法不受支持」**（实测）。
        补一条真正的列表能力，菜单里那一项才不是死链。
        """
        ctx.require("warehouse.view")
        from fpa.domains._base import Page

        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))

        where = ["1=1"]
        values: list[Any] = []
        frag, vals = scope.where_clause("d")
        if frag and frag != "1=1":
            where.append(f"({frag})")
            values.extend(vals)
        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            where.append("(d.code LIKE %s OR d.name LIKE %s OR d.lot_no LIKE %s)")
            values.extend([f"%{keyword}%"] * 3)
        doc_type = doc_type_forced or str(params.get("doc_type") or "").strip()
        if doc_type in DOC_TYPES:
            where.append("d.doc_type = %s")
            values.append(doc_type)
        status = str(params.get("status") or "").strip()
        if status:
            where.append("d.status = %s")
            values.append(status)
        clause = " AND ".join(where)

        total = int(
            tx.query_scalar(
                f"SELECT COUNT(*) FROM warehouse_documents AS d WHERE {clause}", values
            )
            or 0
        )
        rows = tx.query_all(
            "SELECT d.*, w.name AS warehouse_name, m.name AS material_name, "
            "       m.unit AS material_unit "
            "FROM warehouse_documents AS d "
            "LEFT JOIN warehouses AS w ON w.id = d.warehouse_id "
            "LEFT JOIN materials AS m ON m.id = d.material_id "
            f"WHERE {clause} ORDER BY d.id DESC LIMIT %s OFFSET %s",
            [*values, page.size, page.offset],
        )
        return cap.HandlerResult(
            data=page.to_result(
                [
                    self._decorate(
                        row,
                        DOC_STATUS_WORKFLOW,
                        ctx.actor.permissions,
                        resource or "warehouse_document",
                    )
                    for row in rows
                ],
                total,
            ),
            message="",
        )

    def list_receipts_only(
        self, tx: UnitOfWork, ctx, scope: Scope, *, query: dict[str, Any] | None = None, **_: Any
    ) -> cap.HandlerResult:
        """`/api/v1/receipts`：只列到货单（仓储单据页）。"""
        return self.list_documents(
            tx, ctx, scope, query=query, doc_type_forced="receipt", resource="warehouse_document"
        )

    def list_issues(
        self, tx: UnitOfWork, ctx, scope: Scope, *, query: dict[str, Any] | None = None, **_: Any
    ) -> cap.HandlerResult:
        """`/api/v1/issues`：只列领用出库单。行内动作按 `issue` 资源解析。"""
        return self.list_documents(
            tx, ctx, scope, query=query, doc_type_forced="issue", resource="issue"
        )

    def get_issue_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """`/api/v1/issues/{issue_id}`：领用单详情（复用仓储单据详情）。"""
        raw = (path_params or {}).get("issue_id")
        if raw is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "领用单详情需要 path_params 传 issue_id")
        return self.get_document_by_id(
            tx, ctx, scope, path_params={"receipt_id": raw}, resource="issue"
        )

    def get_document_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
        resource: str = "warehouse_document",
        **_: Any,
    ) -> cap.HandlerResult:
        ctx.require("warehouse.view")
        raw = (path_params or {}).get("receipt_id")
        if raw is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "仓储单据详情需要 path_params 传 receipt_id")
        row = self._scope_document(tx, scope, int(raw))
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    row, DOC_STATUS_WORKFLOW, ctx.actor.permissions, "warehouse_document"
                )
            },
        )

    # -- 到货登记 -----------------------------------------------------------

    def create_receipt(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("到货单号", required=True, max_length=64),
        name: f_str("到货事项", required=True, max_length=100),
        warehouse_id: f_ref("收货仓", "warehouse", required=True),
        material_id: f_ref("物料", "material", required=True),
        lot_no: f_str("物料批次号", required=True, max_length=64),
        quantity: f_num("到货数量", required=True, minimum=0.0001),
        unit_cost: f_num("单价", required=True, minimum=0),
        happened_at: f_datetime("到货时间", required=True),
        purchase_order_id: f_ref("采购单", "purchase_order"),
        production_date: f_date("生产日期"),
        expiry_date: f_date("有效期"),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        """`receipt.create`（registry §1.6 / §2.7）。

        ## 为什么这里不直接入账

        登记到货与核验到货是**两件事**：登记是"货到了、单据填好了"，
        核验是"账真的记了"。早期版本 `receipts` 也是二态。
        把两者合成一步会让"填错一个数字"直接变成"库存被改了"。

        ## 前置校验的归属

        registry §4 #3（未审批不得收货）与 §4 #18（同企业且物料有效）声明在**能力**上，
        由内核在写库前执行——本方法不重复手写这些判断（那正是本项目要根除的
        "同一件事写在两处"）。这里只做**字段间**的一致性校验。
        """
        ctx.require("warehouse.receipt.create")

        warehouse = tx.query_one(
            "SELECT id, organization_id, farm_id, area_id, name, status FROM warehouses WHERE id = %s",
            (int(warehouse_id),),
        )
        if warehouse is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "收货仓不存在", data={"field": "warehouse_id"}
            )
        if not scope.allows_row(warehouse):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED, "收货仓不在当前账号的数据范围内"
            )

        # 批次号前置校验：出库靠 `lot_no` 定位批次（§2.7 删掉了 inventory_lot_id），
        # 空批次号会让出库时无处可扣，所以不允许登记成空。
        if not str(lot_no).strip():
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "物料批次号不能为空（入库核验必须填写物料批次）",
                data={"field": "lot_no"},
            )
        if production_date and expiry_date and str(expiry_date) < str(production_date):
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "有效期不得早于生产日期",
                data={"field": "expiry_date"},
            )

        amount = _as_decimal(quantity, field="quantity") * _as_decimal(unit_cost, field="unit_cost")

        _ensure_code_available(
            tx,
            organization_id=int(warehouse["organization_id"]),
            doc_type="receipt",
            code=str(code),
        )
        try:
            tx.execute(
                "INSERT INTO warehouse_documents "
                "(organization_id, farm_id, area_id, doc_type, code, name, warehouse_id, "
                " material_id, quantity, unit_cost, total_quantity, total_amount, supplier_id, "
                " purchase_order_id, happened_at, note, status, created_by) "
                "VALUES (%s,%s,%s,'receipt',%s,%s,%s,%s,%s,%s,0,0,NULL,%s,%s,%s,'draft',%s)",
                (
                    warehouse["organization_id"],
                    warehouse["farm_id"],
                    warehouse["area_id"],
                    code,
                    name,
                    int(warehouse_id),
                    int(material_id),
                    quantity,
                    unit_cost,
                    None if purchase_order_id is None else int(purchase_order_id),
                    happened_at,
                    note,
                    ctx.actor.user_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 按类型判定，非重复键错误原样抛出
            if isinstance(exc, DomainError):
                raise
            if _is_duplicate_key(exc):
                raise DomainError(
                    ErrorCode.CONFLICT,
                    f"到货单号「{code}」已存在，请换一个",
                    data={"rule": "UNIQUE_CODE", "field": "code"},
                ) from exc
            raise
        document_id = tx.last_insert_id()
        tx.execute(
            "INSERT INTO warehouse_document_lines "
            "(document_id, line_no, material_id, lot_no, quantity, unit_cost, "
            " production_date, expiry_date) VALUES (%s,1,%s,%s,%s,%s,%s,%s)",
            (
                document_id,
                int(material_id),
                lot_no,
                quantity,
                unit_cost,
                production_date,
                expiry_date,
            ),
        )

        row = self._scope_document(tx, scope, document_id)
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    row, DOC_STATUS_WORKFLOW, ctx.actor.permissions, "warehouse_document"
                ),
                "_invariant_context": _scope_invariant_context(warehouse, "receipt"),
            },
            resource_id=document_id,
            message=f"已登记到货「{name}」（编号 {code}），待核验",
        )

    # -- 到货核验 -----------------------------------------------------------

    def verify_receipt(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        receipt_id: f_int("到货单 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """`receipt.verify`：入库存 + 生成应付（registry §1.6）。

        副作用是**跨域**的，因此走各域的具名入口、在同一个事务内完成
        （`ROLLOUT_CONTRACT.md` §2）：

            ① `warehouse.ledger.apply_movement` 入库存（本域）
            ② `purchase.purchase_orders.apply_receipt` 推进采购单状态（purchase 域）
            ③ `purchase.payables.create_from_receipt` 生成应付（purchase 域）
        """
        ctx.require("warehouse.receipt.verify")
        row = self._scope_document(tx, scope, int(receipt_id))
        self._require_doc_type(row, "receipt")
        DOC_STATUS_WORKFLOW.require_action(str(row["status"]), RowAction.VERIFY)

        if str(row.get("warehouse_status") or "") != "verified":
            raise DomainError(
                ErrorCode.CONFLICT,
                f"仓库「{row.get('warehouse_name')}」尚未核验，不能入库存",
                data={"rule": "WAREHOUSE_NOT_VERIFIED", "field": "warehouse_id"},
            )

        lines = self._load_lines(tx, int(receipt_id))
        if not lines:
            raise DomainError(
                ErrorCode.CONFLICT, "该到货单没有明细行，无法核验"
            )

        # 乐观锁 + 状态前置：`status='draft'` 放进 WHERE 让"重复核验"在数据库层就被挡。
        affected = tx.execute(
            "UPDATE warehouse_documents SET status='verified', verified_by=%s, "
            "verified_at=NOW(), row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s AND status='draft'",
            (ctx.actor.user_id, ctx.actor.user_id, int(receipt_id), int(expected_version)),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该到货单状态已变化或已被他人修改，请刷新后重试",
            )

        total_quantity = Decimal(0)
        total_amount = Decimal(0)
        receipt_movements = []
        source_ref = self._doc_source_ref(row)
        for index, line in enumerate(lines, start=1):
            quantity = _as_decimal(line["quantity"], field="quantity")
            unit_cost = (
                None if line.get("unit_cost") is None
                else _as_decimal(line["unit_cost"], field="unit_cost")
            )
            total_quantity += quantity
            if unit_cost is not None:
                total_amount += quantity * unit_cost

            receipt_movements.append(
                apply_movement(
                    tx,
                    scope=scope,
                    actor_id=ctx.actor.user_id,
                    source_type=SOURCE_RECEIPT,
                    source_ref=source_ref,
                    source_line_no=index,
                    warehouse_id=int(row["warehouse_id"]),
                    material_id=int(line["material_id"]),
                    lot_no=str(line["lot_no"]),
                    quantity_delta=quantity,
                    happened_at=row["happened_at"],
                    create_lot=True,           # 入库：允许按 batch 建立批次
                    production_date=line.get("production_date"),
                    expiry_date=line.get("expiry_date"),
                    unit_cost=unit_cost,
                )
            )

        # 单头的累计列在核验时由服务端从明细回算写入（不接受客户端提交）。
        # `CumulativeWithin`（§4 #9）按单表 SUM 读它，所以它必须与明细一致。
        tx.execute(
            "UPDATE warehouse_documents SET total_quantity=%s, total_amount=%s WHERE id=%s",
            (total_quantity, total_amount, int(receipt_id)),
        )

        # ---- 跨域副作用（purchase 域） -----------------------------------
        # 只在关联了采购单时发生。`purchase_order_id` 为空的到货（如零星采购）
        # 不推进任何采购单、也不生成应付。
        order_id = row.get("purchase_order_id")
        if order_id is not None:
            self._post_receipt_to_purchase(
                tx=tx,
                scope=scope,
                ctx=ctx,
                purchase_order_id=int(order_id),
                document_id=int(receipt_id),
                document_code=str(row["code"]),
                lines=tuple(
                    (int(line["material_id"]), _as_decimal(line["quantity"], field="quantity"))
                    for line in lines
                ),
                amount=total_amount,
                supplier_id=None if row.get("supplier_id") is None else int(row["supplier_id"]),
                occurred_on=row["happened_at"],
            )

        updated = self._scope_document(tx, scope, int(receipt_id))
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    updated, DOC_STATUS_WORKFLOW, ctx.actor.permissions, "warehouse_document"
                ),
                # ---- 不变量的上下文（`_invariant_context`） ------------------
                # 为什么必须由服务回传：核验是 `action`，**请求体里没有数量**，
                # 而本次到货量是服务从明细算出来的（`total_quantity`）。
                # 不变量拿不到它就只能拿单头的 `quantity` —— 那个数在多行单据上
                # 只是"第一行的量"，会让 §4 #9 的累计判定算少。
                # 让服务显式回传比让不变量自己再算一遍更可靠：两边各算一遍
                # 必然出现不一致（kernel/runner.py::_invariant_extra 的注释同此）。
                "_invariant_context": {
                    "quantity": total_quantity,
                    "purchase_order_id": None if row.get("purchase_order_id") is None
                    else int(row["purchase_order_id"]),
                    # `PeriodOpen(date_field="happened_at")` 的反查键，以及它的**租户键**。
                    # 会计期间每个企业各有一份（`accounting_periods.organization_id`
                    # 是 NOT NULL），缺了租户键内核会命中邻家的期间行：邻家已关账
                    # 则误拦本次核验，邻家未关账则静默放行已关账期间的入库。
                    # 内核缺它时**显式报错**，不静默降级，所以必须回传。
                    "happened_at": row["happened_at"],
                    "organization_id": row["organization_id"],
                    # 入库的增量是正的，`NoNegativeStock` 不会因此拒绝；仍然回传是为了
                    # 让"声明了不变量"与"服务回传了上下文"这一对始终成立——
                    # 否则将来谁把这条能力也挂上 NoNegativeStock，就会撞上
                    # `INTERNAL_ERROR 缺少账本行上下文`。
                    "_ledger_lines": [
                        {
                            "warehouse_id": m.warehouse_id,
                            "material_id": m.material_id,
                            "inventory_lot_id": m.lot_id,
                        }
                        for m in receipt_movements
                    ],
                },
            },
            resource_id=int(receipt_id),
            message=(
                f"已核验到货「{updated['name']}」（编号 {updated['code']}），"
                f"入库 {total_quantity} 单位"
            ),
        )

    def _post_receipt_to_purchase(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        ctx,
        purchase_order_id: int,
        document_id: int,
        document_code: str,
        lines: tuple[tuple[int, Decimal], ...],
        amount: Decimal,
        supplier_id: int | None,
        occurred_on: Any,
    ) -> None:
        """跨域：推进采购单状态 + 生成应付（`ROLLOUT_CONTRACT.md` §2）。

        ## 为什么跨域 import 写在函数体内

        `warehouse` 与 `purchase` 之间存在**双向**调用（`receipt.verify` → purchase；
        purchase 侧读仓库单据）。模块级 import 会直接炸成环形导入，所以契约 §2
        明确规定"跨域 import 一律写在函数体内"。

        ## 为什么缺失时显式失败而不是跳过

        静默跳过会让"核验成功但应付没生成"变成一个只在月底对账时才暴露的账目缺口。
        显式失败把缺口服现在集成时——**"缺上下文不得静默跳过"是本项目已确立的口径**
        （`kernel/invariants.py` 的同族纪律）。

        ## 两个入口的分工（purchase 域已定稿的签名）

        * `apply_receipt(tx, ctx, purchase_order_id=, receipt_document_id=, actor_id=, happened_at=)`
          —— **只回报事实**，不做 #9 判定（判定由 `receipt.verify` 上的
          `CumulativeWithin` 统一强制；再手工判一次就是"两处描述同一件事"）；
        * `create_from_receipt(tx, ctx, purchase_order_id=, receipt_document_id=, lines=, amount=, ...)`
          —— 生成应付，幂等（重放返回 `created=False`）。

        ## 参数名提示（这是集成时最容易写混的一处）

        两个入口的"到货单"参数**统一叫 `receipt_document_id`**，指向
        `warehouse_documents.id`（**单头**），不是明细列——明细表
        `warehouse_document_lines` 只有 `document_id` + `line_no`
        （`ROLLOUT_CONTRACT.md` §2A.6）。名字里带 `document` 就是为了消歧：
        单头被到货/领用共用，明细里根本没有 `receipt_id` 这一列。
        """
        try:
            from fpa.domains.purchase.orders_write import apply_receipt
            from fpa.domains.purchase.payables import create_from_receipt
        except ImportError as exc:
            raise DomainError(
                ErrorCode.SERVICE_UNAVAILABLE,
                "采购域的入库过账入口尚未就绪，暂不能核验到货"
                "（不接受「核验成功但漏记应付」这种结果）",
                data={"missing": "purchase.orders_write.apply_receipt"},
            ) from exc

        apply_receipt(
            tx,
            ctx,
            purchase_order_id=int(purchase_order_id),
            # 指向 `warehouse_documents.id`（单头）。purchase 域已把两个入口的参数名
            # **统一为 `receipt_document_id`**（此前一个叫 receipt_id，是集成期最
            # 容易写混的一处）。
            receipt_document_id=int(document_id),
            actor_id=ctx.actor.user_id,
            happened_at=occurred_on,
        )
        create_from_receipt(
            tx,
            ctx,
            purchase_order_id=int(purchase_order_id),
            receipt_document_id=int(document_id),
            lines=lines,
            amount=amount,
            actor_id=ctx.actor.user_id,
            occurred_on=occurred_on,
            supplier_id=supplier_id,
        )

    # -- 领用登记 -----------------------------------------------------------

    def create_issue(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("出库单号", required=True, max_length=64),
        name: f_str("出库事项", required=True, max_length=100),
        warehouse_id: f_ref("出库仓", "warehouse", required=True),
        material_id: f_ref("物料", "material", required=True),
        lot_no: f_str("物料批次号", required=True, max_length=64),
        quantity: f_num("出库数量", required=True, minimum=0.0001),
        happened_at: f_datetime("出库时间", required=True),
        pond_id: f_ref("塘口", "pond"),
        batch_id: f_ref("批次", "batch"),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        """`issue.create`（registry §1.6 / §2.7）。

        ## `lot_no` 必须指向**已存在且可用**的批次

        §2.7 原文："`issue.create` 的 `lot_no` 只能选 `available` 批次"。
        这里在登记时就校验一次——不是为了防止超发（那是 `issue.verify` 的事，
        由账本判定），而是为了**让用户在下单时就看到选错了批次**，
        而不是等核验时才被拒。

        ## 领用去向

        `pond_id` / `batch_id` 是可选的，但**要么都有、要么都没有**（006 迁移里的
        `chk_warehouse_documents_issue_target`）。只填一个是填错了。
        """
        ctx.require("warehouse.issue.create")

        warehouse = tx.query_one(
            "SELECT id, organization_id, farm_id, area_id, name, status FROM warehouses WHERE id = %s",
            (int(warehouse_id),),
        )
        if warehouse is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "出库仓不存在", data={"field": "warehouse_id"}
            )
        if not scope.allows_row(warehouse):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED, "出库仓不在当前账号的数据范围内"
            )

        if (pond_id is None) != (batch_id is None):
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "领用去向的塘口与批次必须同时填写或同时留空",
                data={"field": "batch_id"},
            )

        lot = tx.query_one(
            "SELECT id, lot_no, status FROM inventory_lots "
            "WHERE warehouse_id=%s AND material_id=%s AND lot_no=%s",
            (int(warehouse_id), int(material_id), lot_no),
        )
        if lot is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                f"物料批次「{lot_no}」在该仓库中不存在，请先在到货核验时登记",
                data={"field": "lot_no"},
            )
        if str(lot["status"]) != "available":
            raise DomainError(
                ErrorCode.CONFLICT,
                f"物料批次「{lot_no}」已关闭，不能出库",
                data={"field": "lot_no", "status": str(lot["status"])},
            )

        _ensure_code_available(
            tx,
            organization_id=int(warehouse["organization_id"]),
            doc_type="issue",
            code=str(code),
        )
        try:
            tx.execute(
                "INSERT INTO warehouse_documents "
                "(organization_id, farm_id, area_id, doc_type, code, name, warehouse_id, "
                " material_id, quantity, unit_cost, total_quantity, total_amount, supplier_id, "
                " purchase_order_id, pond_id, batch_id, happened_at, note, status, created_by) "
                "VALUES (%s,%s,%s,'issue',%s,%s,%s,%s,%s,NULL,0,0,NULL,NULL,%s,%s,%s,%s,'draft',%s)",
                (
                    warehouse["organization_id"],
                    warehouse["farm_id"],
                    warehouse["area_id"],
                    code,
                    name,
                    int(warehouse_id),
                    int(material_id),
                    quantity,
                    None if pond_id is None else int(pond_id),
                    None if batch_id is None else int(batch_id),
                    happened_at,
                    note,
                    ctx.actor.user_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, DomainError):
                raise
            if _is_duplicate_key(exc):
                raise DomainError(
                    ErrorCode.CONFLICT,
                    f"出库单号「{code}」已存在，请换一个",
                    data={"rule": "UNIQUE_CODE", "field": "code"},
                ) from exc
            raise
        document_id = tx.last_insert_id()
        tx.execute(
            "INSERT INTO warehouse_document_lines "
            "(document_id, line_no, material_id, lot_no, quantity) VALUES (%s,1,%s,%s,%s)",
            (document_id, int(material_id), lot_no, quantity),
        )

        row = self._scope_document(tx, scope, document_id)
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    row, DOC_STATUS_WORKFLOW, ctx.actor.permissions, "warehouse_document"
                ),
                "_invariant_context": _scope_invariant_context(warehouse, "issue"),
            },
            resource_id=document_id,
            message=f"已登记领用出库「{name}」（编号 {code}），待核验",
        )

    # -- 领用核验 -----------------------------------------------------------

    def verify_issue(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        issue_id: f_int("出库单 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """`issue.verify`：扣库存（registry §1.6）。

        **没有字段表**（§2.7 原文）——只带全局的 `expected_version`。这正是
        "不变量在能力上强制"的形态：**不在入参里声明库存，而在执行时读库判定**。
        调用方无法通过"多传一个数字"来影响扣多少。

        负库存判定发生在 `apply_movement` 内部、持有批次行锁的临界区里
        （见 `ledger.py` 模块头注释：为什么不能只靠声明式不变量）。
        """
        ctx.require("warehouse.issue.verify")
        row = self._scope_document(tx, scope, int(issue_id))
        self._require_doc_type(row, "issue")
        DOC_STATUS_WORKFLOW.require_action(str(row["status"]), RowAction.VERIFY)

        lines = self._load_lines(tx, int(issue_id))
        if not lines:
            raise DomainError(ErrorCode.CONFLICT, "该出库单没有明细行，无法核验")

        affected = tx.execute(
            "UPDATE warehouse_documents SET status='verified', verified_by=%s, "
            "verified_at=NOW(), row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s AND status='draft'",
            (ctx.actor.user_id, ctx.actor.user_id, int(issue_id), int(expected_version)),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该出库单状态已变化或已被他人修改，请刷新后重试",
            )

        total_quantity = Decimal(0)
        source_ref = self._doc_source_ref(row)
        movements = []
        for index, line in enumerate(lines, start=1):
            quantity = _as_decimal(line["quantity"], field="quantity")
            total_quantity += quantity
            movements.append(
                apply_movement(
                    tx,
                    scope=scope,
                    actor_id=ctx.actor.user_id,
                    source_type=SOURCE_ISSUE,
                    source_ref=source_ref,
                    source_line_no=index,
                    warehouse_id=int(row["warehouse_id"]),
                    material_id=int(line["material_id"]),
                    lot_no=str(line["lot_no"]),
                    quantity_delta=-quantity,       # 出库为负
                    happened_at=row["happened_at"],
                    create_lot=False,               # 出库不能凭空造批次
                    pond_id=None if row.get("pond_id") is None else int(row["pond_id"]),
                    batch_id=None if row.get("batch_id") is None else int(row["batch_id"]),
                )
            )

        tx.execute(
            "UPDATE warehouse_documents SET total_quantity=%s, total_amount=0 WHERE id=%s",
            (total_quantity, int(issue_id)),
        )

        updated = self._scope_document(tx, scope, int(issue_id))
        # `message` 用**持锁状态下读出的真实余额**渲染（WRITE_CONTRACT 规则 3）：
        # 用户看到的"还剩多少"必须是数据库里的那个数，不是模型算的。
        remaining = "、".join(
            f"{m.lot_no} 剩余 {m.balance_after}" for m in movements
        )
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    updated, DOC_STATUS_WORKFLOW, ctx.actor.permissions, "warehouse_document"
                ),
                # 不变量的上下文。三项都必须由服务回传：
                #
                # * `happened_at` —— `PeriodOpen(date_field="happened_at")` 要用；issue.verify
                #   是 action，请求体里没有日期。
                # * `organization_id` —— 同一个 `PeriodOpen` 的**租户键**：会计期间
                #   每个企业各有一份，缺它内核会命中邻家的期间行（误拦本次核验，
                #   或静默放行已关账期间的出库）。内核缺键时显式 `INTERNAL_ERROR`。
                # * `_ledger_lines` —— `NoNegativeStock` 用它**定位要校验哪些分组**
                #   （`group_by` 的 `warehouse_id` / `material_id` / `inventory_lot_id`）。
                #   注意新版不变量**不再用它做算术**（余额由它自己 `SUM` 写入后的账本行得出），
                #   但"本次动的是哪个批次"只有服务知道，所以这一项仍然必需；
                #   缺失时它会显式 `INTERNAL_ERROR` 而不是静默放行。
                "_invariant_context": {
                    "happened_at": row["happened_at"],
                    "organization_id": row["organization_id"],
                    "_ledger_lines": [
                        {
                            "warehouse_id": m.warehouse_id,
                            "material_id": m.material_id,
                            "inventory_lot_id": m.lot_id,
                        }
                        for m in movements
                    ],
                },
                "movements": [
                    {
                        "lot_id": m.lot_id,
                        "lot_no": m.lot_no,
                        "quantity_delta": str(m.quantity_delta),
                        "balance_after": str(m.balance_after),
                    }
                    for m in movements
                ],
            },
            resource_id=int(issue_id),
            message=(
                f"已核验出库「{updated['name']}」（编号 {updated['code']}），"
                f"出库 {total_quantity} 单位；{remaining}"
            ),
        )

    # -- 回读函数 -----------------------------------------------------------

    def load_warehouse_document(
        self, tx: UnitOfWork, *, scope: Scope, record_id: int
    ) -> dict[str, Any] | None:
        """回读函数：执行器在提交前调它确认"写入真的落了库"。

        `ROLLOUT_CONTRACT.md` §3：**写能力的 handler 必须挂
        `__fpa_load_by_id__`**，否则 `_reload_after` 抛 `INTERNAL_ERROR`
        ——也就是说"能写但读不回来"会被判为失败。本文件末尾统一绑定。
        """
        row = self._scope_document(tx, scope, int(record_id))
        return {**row, "version": int(row.get("row_version") or 1)}


# ---------------------------------------------------------------------------
# 回读函数绑定（ROLLOUT_CONTRACT §3 的硬要求）
#
# 为什么在类外绑定而不是给每个方法加装饰器：`Capability.handler` 拿到的就是
# 函数对象，绑定在这里让"哪些方法有回读"一眼可见、可被架构测试断言。
# ---------------------------------------------------------------------------
for _method in (
    WarehouseDocumentService.create_receipt,
    WarehouseDocumentService.verify_receipt,
    WarehouseDocumentService.create_issue,
    WarehouseDocumentService.verify_issue,
):
    _method.__fpa_load_by_id__ = WarehouseDocumentService.load_warehouse_document  # type: ignore[attr-defined]


__all__ = ["WarehouseDocumentService"]
