"""仓储域的读路径：仓库列表 / 库存汇总 / 库存流水。

## 这三个读能力为什么重要

它们不是"便利查询"，而是"库存是怎么变成现在这个数"的唯一可解释入口。
早期版本把余额推导塞在 SQL 里（`早期版本 purchase_payment_store.py:95-96` 同族的做法），
于是"这个物料的库存从哪来"无法回答——`DECISIONS.md` Q12 拒绝裁剪
`payable.list` / `receivable.list` 用的就是这条理由：**删掉余额查询会让余额
变成不可解释的隐藏计算**。

## 余额一律从账本求和，不从任何"余额列"读

`inventory_lots` 上**没有** `quantity` 列（见 006 迁移）。余额是
`SUM(inventory_ledger.quantity_delta)`。这样"批次余额"与"流水"在结构上
不可能不一致——它们本来就是同一个数。
"""

from __future__ import annotations

from typing import Any

from yuxin.kernel.workflow import allowed_actions_for
from yuxin.kernel import capability as cap
from yuxin.kernel.date_bounds import upper_bound
from yuxin.kernel.errors import DomainError, ErrorCode, not_found
from yuxin.kernel.workflow import flag_label
from yuxin.kernel.money import unit_label
from yuxin.kernel.fields import f_int
from yuxin.kernel.scope import Scope, scope_predicate_for_columns
from yuxin.kernel.uow import UnitOfWork
from yuxin.kernel.workflow import RowAction

from yuxin.domains._base import Page

from .service import (
    DOC_STATUS_WORKFLOW,
    DOC_TYPES,
    DOC_TYPE_LABELS,
    INVENTORY_LOT_WORKFLOW,
    SOURCE_TYPE_LABELS,
    WAREHOUSE_SCOPE_COLUMNS,
    WAREHOUSE_WORKFLOW,
)

# 为向后兼容，继续从本模块导出这四个名字；**定义仍在 `service.py`**。
# 这里保留名字是因为 `warehouse_capability_e2e.py` 等脚本按本模块导入它们；
# 但两份定义会在下一次改文案时漂移，所以上面那几行是 import 而不是赋值。


def _page_params(params: dict[str, Any]) -> Page:
    return Page.parse(params.get("page"), params.get("page_size"))


def _label_of(workflow, code: str) -> str:
    for state in workflow.states:
        if state.code == code:
            return state.label
    return code


def _allowed_actions(workflow, status: str, permissions: frozenset[str], resource: str) -> list[str]:
    """行内动作 = 状态机允许的动作 ∩ 当前账号有权做的动作。

    权限过滤放在服务端（而不是只在前端）是因为 `allowed_actions` 是**渲染依据**：
    把无权动作渲染出来再让点击时失败，是把校验成本转嫁给用户。
    """
    return allowed_actions_for(workflow, resource, status, permissions)


class WarehouseQueryService:
    """仓库 / 库存 / 流水的查询。

    与写路径共用 `_scope_row` 的口径：读看到一个样、写看到另一个样是早期版本
    的真实问题（`master_data_store.py` 的读路径与写路径各自拼 scope 条件）。
    """

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _scope_row(tx: UnitOfWork, scope: Scope, record_id: int) -> dict[str, Any]:
        row = tx.query_one("SELECT * FROM warehouses WHERE id = %s", (int(record_id),))
        if row is None:
            raise not_found("仓库")
        if not scope.allows_row(row):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED, "该仓库不在当前账号的数据范围内"
            )
        return row

    @staticmethod
    def _decorate(
        row: dict[str, Any], workflow, permissions: frozenset[str], resource: str
    ) -> dict[str, Any]:
        status = str(row.get("status") or "")
        decorated = dict(row)
        decorated["version"] = int(row.get("row_version") or 1)
        decorated["status_label"] = _label_of(workflow, status)
        decorated["allowed_actions"] = _allowed_actions(workflow, status, permissions, resource)
        # `warehouses.is_default` 是 `TINYINT(1)`，直接渲染会显示 `1` ——
        # 那是给机器看的，不是给人看的。词表的唯一落点在内核
        # `kernel/workflow.py::flag_label`（与 `unit_label` / `category_label` 同一条纪律）。
        # 没有该列的资源不受影响。
        if decorated.get("is_default") is not None:
            decorated["is_default_label"] = flag_label(decorated["is_default"])
        # 仓储单据的「单据类型」列声明的是 `doc_type_label`，而这一项原先谁也算 ——
        # 于是整列显示「—」（派生列声明了、值永远缺失，与 `partner_type_label` 同类）。
        # 词表的唯一落点是 `service.py::DOC_TYPE_LABELS`。
        if decorated.get("doc_type") is not None:
            code = str(decorated["doc_type"])
            decorated["doc_type_label"] = DOC_TYPE_LABELS.get(code, code)
        return decorated

    # -- 回读函数（写路径用） -------------------------------------------------

    @classmethod
    def load_warehouse(
        cls, tx: UnitOfWork, *, scope: Scope, record_id: int
    ) -> dict[str, Any] | None:
        """回读函数：执行器在提交前调它确认"写入真的落了库"。

        `docs/WRITE_CONTRACT.md` 规则 2：`executed` 的含义是**读回来的行确实是
        我们想要的样子**，不是"我们调用了 INSERT"。所以每个写能力都必须在能力声明里
        写 `loader=WarehouseQueryService.load_warehouse`（缺了它执行器在回读步骤抛
        `INTERNAL_ERROR`，即生产路径 500）。

        ## 为什么它不复用 `_scope_row`（那里会抛 NOT_FOUND / DATA_SCOPE_DENIED）

        本函数与 `_scope_row` 的**职责不同**，不是两处实现同一件事：

        | | `_scope_row` | `load_warehouse` |
        |---|---|---|
        | 用途 | **写之前**定位目标行 | **写之后**回读刚写的那一行 |
        | 行不存在时 | 抛 `NOT_FOUND`（用户给了一个不存在的 id，这是业务事实） | 返回 `None`（执行器按 `None` 判失败） |
        | 判定范围 | 是 | **不判**（由执行器的范围/不变量层负责） |

        让回读也抛异常会有一个真实后果：`runner._replay`（幂等回放）也走这个函数，
        而回放路径上**没有** tx 范围校验的语境，抛错会把"回放"变成 500。
        所以这里返回 `None`，与 `load_pond` / `load_partner` / `load_area` 同形。
        """
        row = tx.query_one(
            "SELECT r.* FROM warehouses AS r WHERE r.id = %s", (int(record_id),)
        )
        if row is None:
            return None
        return {**row, "version": int(row.get("row_version") or 1)}

    # -- 仓库详情 -----------------------------------------------------------

    def get_warehouse_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """仓库详情。`warehouse.get`（registry §1.6，t18 补）。

        ## 为什么这一条必须补

        `Resource(name="warehouse", …)` **声明了 `detail_path`**，而 registry §1.6
        原来没有 `warehouse.get` —— 于是"行内查看"要么 404，要么只能拿列表行的数据
        凑详情（那正是 `docs/ROW_ACTIONS.md` 描述的那类"按钮点了没反应"）。
        同族的三条详情读（`pond.get` / `area.get` / `material.get` / `partner.get`）
        已在 t3 补过，本条是同一条判据的补齐。

        **刻意不带任何筛选**：详情接口返回这一行的完整记录（含 `allowed_actions`
        与 `version`），前端据此渲染按钮与编辑表单。
        """
        ctx.require("warehouse.view")
        raw = (path_params or {}).get("warehouse_id")
        if raw is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR, "仓库详情需要 path_params 传 warehouse_id"
            )
        row = self._scope_row(tx, scope, int(raw))
        # ★ **读能力不要带 `resource_id`**。
        #
        # `web/app.py::_ok` 的逻辑是"只要 `result.resource_id` 非空，就把 data 包成
        # `{resource_id, record: data}`"——那是**写**能力的形状（写的结果要带主键）。
        # 读能力本身已经返回 `{"record": …}`，再带上 `resource_id` 就会多包一层，
        # 变成 `data.record.record`。
        #
        # 而前端 `ResourceDetailPage.vue` 读的是 `payload.record`（= `data.record`），
        # 拿到的是内层那个 `{record: …}` 字典 ⇒ 每个字段名都取不到 ⇒ **整页每格显示「—」**，
        # 而且 HTTP 200、没有异常、日志干净。同族四条（`purchase_order.get` /
        # `sales_order.get` / `pond.get` / 本条的四个同伴）都用"不带 resource_id"的形态，
        # 本行此前是唯一漏改的一处（实测 17 个 `*.get` 里只有 `warehouse.get` 是双层）。
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    row, WAREHOUSE_WORKFLOW, ctx.actor.permissions, "warehouse"
                )
            },
        )

    # -- 仓库列表 -----------------------------------------------------------

    def list_warehouses(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        query: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """仓库列表。`warehouse.list`（registry §1.6）。"""
        ctx.require("warehouse.view")
        params = query or {}
        page = _page_params(params)

        where = ["1=1"]
        values: list[Any] = []
        # ## 范围谓词为什么不再用 `scope.where_clause("w")`（t18 换的）
        #
        # `Scope.predicate()` 按账号持有的**所有**范围类型渲染名字相同的列：持有
        # `personal` 型范围的账号会得到 `w.created_by IN (...)`、持有 `pond` 型的
        # 会得到 `w.pond_id IN (...)` —— 而 `warehouses` 这两列**都不存在**，
        # MySQL 直接报 `Unknown column`（HTTP 500）。同族的真实形态：
        # 只持有 farm 型范围的账号查 `areas`（它没有 area_id 列）也会拼出
        # 不存在的列。而区域列表走的是另一条判据（只渲染本表真实存在的列），
        # 于是**同一条数据权限在两个域给出不同结论**。
        #
        # 现在两边都走内核的 `scope_predicate_for_columns`，各域只声明
        # "我这张表有哪些分租列"（`WAREHOUSE_SCOPE_COLUMNS`）。
        fragment, scope_values = scope_predicate_for_columns(
            scope, WAREHOUSE_SCOPE_COLUMNS, "w"
        )
        if fragment and fragment != "1=1":
            where.append(f"({fragment})")
            values.extend(scope_values)

        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            where.append("(w.code LIKE %s OR w.name LIKE %s)")
            values.extend([f"%{keyword}%", f"%{keyword}%"])
        status = str(params.get("status") or "").strip()
        if status:
            where.append("w.status = %s")
            values.append(status)
        else:
            # 默认视图排除已停用的仓库（"停用 = 归档"，见 `service.WAREHOUSE_WORKFLOW`）。
            #
            # 判据与 `master_data.resources_read._DEFAULT_EXCLUDED_STATUS` **逐字相同**，
            # 理由也一样：仓库列表同时是管理页面与 `DynamicForm` 的 `ref` 下拉的数据源，
            # 而后者（`DynamicForm.vue`）请求 `?page=1&page_size=100`，**不带任何筛选**。
            # 下拉里出现已停用的仓库就说明"停用"是假的。
            # 于是：不带 `status` → 排除 archived；带 `status=archived` → 照常能查到。
            where.append("w.status <> %s")
            values.append("archived")

        clause = " AND ".join(where)
        total = int(
            (tx.query_one(f"SELECT COUNT(*) AS n FROM warehouses AS w WHERE {clause}", values) or {}).get("n", 0)
        )
        rows = tx.query_all(
            f"""
            SELECT w.*, a.name AS area_name
            FROM warehouses AS w
            LEFT JOIN areas AS a ON a.id = w.area_id
            WHERE {clause}
            ORDER BY w.is_default DESC, w.id DESC
            LIMIT %s OFFSET %s
            """,
            [*values, page.size, page.offset],
        )
        return cap.HandlerResult(
            data=page.to_result(
                [
                    self._decorate(row, WAREHOUSE_WORKFLOW, ctx.actor.permissions, "warehouse")
                    for row in rows
                ],
                total,
            ),
            message="",
        )

    # -- 库存汇总 -----------------------------------------------------------

    def list_inventory(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        query: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """库存汇总：按 `(仓库, 物料, 批次)` 聚合的可用量。

        分组粒度与不变量 §4 #1 的 `group_by` **逐字一致**
        （`warehouse_id` / `material_id` / `inventory_lot_id`）。
        这不是巧合：余额的"一格"和负库存判定的"一格"必须是同一件事，
        否则"列表显示还有 5"与"核验说不够"会同时成立。
        """
        ctx.require("inventory.view")
        params = query or {}
        page = _page_params(params)

        where = ["1=1"]
        values: list[Any] = []
        fragment, scope_values = scope.where_clause("l")
        if fragment and fragment != "1=1":
            where.append(f"({fragment})")
            values.extend(scope_values)

        warehouse_id = str(params.get("warehouse_id") or "").strip()
        if warehouse_id:
            where.append("l.warehouse_id = %s")
            values.append(int(warehouse_id))
        material_id = str(params.get("material_id") or "").strip()
        if material_id:
            where.append("l.material_id = %s")
            values.append(int(material_id))
        lot_no = str(params.get("lot_no") or "").strip()
        if lot_no:
            where.append("l.lot_no LIKE %s")
            values.append(f"%{lot_no}%")
        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            where.append("(l.lot_no LIKE %s OR m.name LIKE %s)")
            values.extend([f"%{keyword}%", f"%{keyword}%"])
        only_available = str(params.get("only_available") or "").strip() in ("1", "true", "yes")
        if only_available:
            where.append("l.status = 'available'")

        clause = " AND ".join(where)
        base = f"""
            FROM inventory_lots AS l
            LEFT JOIN warehouses AS w ON w.id = l.warehouse_id
            LEFT JOIN materials AS m ON m.id = l.material_id
            LEFT JOIN inventory_ledger AS g ON g.inventory_lot_id = l.id
            WHERE {clause}
            GROUP BY l.id, l.warehouse_id, l.material_id, l.lot_no, l.status,
                     l.expiry_date, l.production_date, w.name, m.name, m.unit
        """
        total = int(
            (
                tx.query_one(
                    f"SELECT COUNT(*) AS n FROM (SELECT l.id {base}) AS grouped", values
                )
                or {}
            ).get("n", 0)
        )
        rows = tx.query_all(
            f"""
            SELECT l.id, l.warehouse_id, l.material_id, l.lot_no, l.status,
                   l.expiry_date, l.production_date,
                   w.name AS warehouse_name, m.name AS material_name, m.unit AS unit,
                   COALESCE(SUM(g.quantity_delta), 0) AS available_quantity
            {base}
            ORDER BY (l.expiry_date IS NULL), l.expiry_date, l.id
            LIMIT %s OFFSET %s
            """,
            [*values, page.size, page.offset],
        )

        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            status = str(item.get("status") or "")
            item["version"] = int(item.get("row_version") or 1)
            item["status_label"] = _label_of(INVENTORY_LOT_WORKFLOW, status)
            # 计量单位的展示标签（码来自 `materials.unit` 的 JOIN，见本文件上方 SQL）。
            if item.get("unit") is not None:
                item["unit_label"] = unit_label(str(item["unit"]))
            # **数量 + 单位**的复合展示列。
            #
            # 为什么必须有这一列，而不是把 `unit_label` 单独放一列：
            # "可用数量 13.00" 与 "计量单位 公斤" 分成两列时，用户在窄表里
            # 看到的是两个互不相关的格子；而实际要读的是"13.00 公斤"这一件事。
            # 这与 `cost` 域的 `target_label`（`塘口#1`）同一手法：
            # **数字与它的单位是一个事实**，在服务端合成一处。
            if item.get("available_quantity") is not None and item.get("unit_label"):
                item["quantity_with_unit"] = (
                    f"{item['available_quantity']} {item['unit_label']}"
                )
            # 数量统一转成字符串再出网：`DECIMAL` 直接序列化成 float 会丢精度，
            # 而"库存 420.00000000001 kg"这种显示是缺陷，不是显示问题。
            item["available_quantity"] = str(item.get("available_quantity"))
            item["expired"] = _is_expired(item.get("expiry_date"))
            item["allowed_actions"] = _allowed_actions(
                INVENTORY_LOT_WORKFLOW, status, ctx.actor.permissions, "inventory_lot"
            )
            items.append(item)

        return cap.HandlerResult(data=page.to_result(items, total), message="")

    def get_inventory_lot_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        ctx.require("inventory.view")
        raw = (path_params or {}).get("lot_id")
        if raw is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "物料批次详情需要 path_params 传 lot_id")
        lot_id = int(raw)
        row = tx.query_one(
            "SELECT l.*, w.name AS warehouse_name, m.name AS material_name, m.unit AS unit, "
            "COALESCE((SELECT SUM(g.quantity_delta) FROM inventory_ledger AS g "
            "WHERE g.inventory_lot_id = l.id), 0) AS available_quantity "
            "FROM inventory_lots AS l "
            "LEFT JOIN warehouses AS w ON w.id = l.warehouse_id "
            "LEFT JOIN materials AS m ON m.id = l.material_id "
            "WHERE l.id = %s",
            (lot_id,),
        )
        if row is None:
            raise not_found("物料批次")
        if not scope.allows_row(row):
            raise DomainError(ErrorCode.DATA_SCOPE_DENIED, "该物料批次不在当前账号的数据范围内")

        record = dict(row)
        status = str(record.get("status") or "")
        record["version"] = int(record.get("row_version") or 1)
        record["status_label"] = _label_of(INVENTORY_LOT_WORKFLOW, status)
        if record.get("unit") is not None:
            record["unit_label"] = unit_label(str(record["unit"]))
        if record.get("available_quantity") is not None and record.get("unit_label"):
            record["quantity_with_unit"] = (
                f"{record['available_quantity']} {record['unit_label']}"
            )
        record["available_quantity"] = str(record.get("available_quantity"))
        record["expired"] = _is_expired(record.get("expiry_date"))
        record["allowed_actions"] = _allowed_actions(
            INVENTORY_LOT_WORKFLOW, status, ctx.actor.permissions, "inventory_lot"
        )
        return cap.HandlerResult(data={"record": record})

    # -- 库存流水 -----------------------------------------------------------

    def list_inventory_ledger(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        query: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """库存流水：只追加账本的分页视图，带**逐行结存**。

        `balance_after` 用窗口函数算（`SUM(...) OVER (PARTITION BY lot ORDER BY id)`），
        不是"读当前余额再倒推"——后者在分页时必然算错（这正是早期版本
        "前端拿当前页算占比/累计"那类缺陷的同族形态）。
        """
        ctx.require("inventory.view")
        params = query or {}
        page = _page_params(params)

        where = ["1=1"]
        values: list[Any] = []
        fragment, scope_values = scope.where_clause("g")
        if fragment and fragment != "1=1":
            where.append(f"({fragment})")
            values.extend(scope_values)

        for key, column in (("warehouse_id", "g.warehouse_id"), ("material_id", "g.material_id")):
            raw = str(params.get(key) or "").strip()
            if raw:
                where.append(f"{column} = %s")
                values.append(int(raw))
        lot_no = str(params.get("lot_no") or "").strip()
        if lot_no:
            where.append("g.lot_no LIKE %s")
            values.append(f"%{lot_no}%")
        source_type = str(params.get("source_type") or "").strip()
        if source_type:
            if source_type not in DOC_TYPES:
                raise DomainError(
                    ErrorCode.VALIDATION_ERROR,
                    f"来源类型只能是 {' / '.join(DOC_TYPES)}",
                    data={"field": "source_type"},
                )
            where.append("g.source_type = %s")
            values.append(source_type)
        source_ref = str(params.get("source_ref") or "").strip()
        if source_ref:
            where.append("g.source_ref LIKE %s")
            values.append(f"%{source_ref}%")
        date_from = str(params.get("date_from") or "").strip()
        if date_from:
            where.append("g.happened_at >= %s")
            values.append(date_from)
        date_to = str(params.get("date_to") or "").strip()
        if date_to:
            # `happened_at` 是 **DATETIME**：上界必须含当天整天，否则"填今天筛不到今天"。
            # 判据与实现只有一处（`kernel/date_bounds.py`）。
            fragment, bound = upper_bound("g.happened_at", date_to)
            where.append(fragment)
            values.append(bound)

        clause = " AND ".join(where)
        total = int(
            (tx.query_one(f"SELECT COUNT(*) AS n FROM inventory_ledger AS g WHERE {clause}", values) or {}).get(
                "n", 0
            )
        )
        rows = tx.query_all(
            f"""
            SELECT g.id, g.warehouse_id, g.material_id, g.inventory_lot_id, g.lot_no,
                   g.source_type, g.source_ref, g.source_line_no, g.quantity_delta,
                   g.unit_cost, g.amount, g.pond_id, g.batch_id, g.happened_at,
                   w.name AS warehouse_name, m.name AS material_name, m.unit AS unit,
                   b.code AS batch_code, p.name AS pond_name,
                   SUM(g.quantity_delta) OVER (
                       PARTITION BY g.inventory_lot_id ORDER BY g.id
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS balance_after
            FROM inventory_ledger AS g
            LEFT JOIN warehouses AS w ON w.id = g.warehouse_id
            LEFT JOIN materials AS m ON m.id = g.material_id
            LEFT JOIN production_batches AS b ON b.id = g.batch_id
            LEFT JOIN ponds AS p ON p.id = g.pond_id
            WHERE {clause}
            ORDER BY g.happened_at DESC, g.id DESC
            LIMIT %s OFFSET %s
            """,
            [*values, page.size, page.offset],
        )

        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["source_type_label"] = SOURCE_TYPE_LABELS.get(str(item.get("source_type")), "")
            if item.get("unit") is not None:
                item["unit_label"] = unit_label(str(item["unit"]))
            # `DECIMAL` 一律以字符串出网，避免 JSON 浮点丢精度
            for key in ("quantity_delta", "balance_after", "unit_cost", "amount"):
                if item.get(key) is not None:
                    item[key] = str(item[key])
            # **数量 + 单位**的复合展示列。
            #
            # 必须在上面那个 str() 循环**之后**拼：否则拿到的是 `Decimal`，
            # f-string 会得到 `-40.0000 公斤`（看似对），但 `quantity_delta` 随后
            # 又被 str() 覆盖一遍 —— 两个字段的值来自两次不同的转换，
            # 迟早会出现"列里是 -40.0000、复合列里是 -40.000000000000001"这种不一致。
            # **一次转换、两处引用**。
            if item.get("quantity_delta") is not None and item.get("unit_label"):
                item["quantity_with_unit"] = f"{item['quantity_delta']} {item['unit_label']}"
            items.append(item)

        return cap.HandlerResult(data=page.to_result(items, total), message="")

    # -- 回读 ---------------------------------------------------------------

    def load_warehouse(
        self, tx: UnitOfWork, *, scope: Scope, record_id: int
    ) -> dict[str, Any] | None:
        row = tx.query_one("SELECT * FROM warehouses WHERE id = %s", (int(record_id),))
        return None if row is None else {**row, "version": int(row.get("row_version") or 1)}


def _is_expired(expiry) -> bool:
    """过期是**查询时计算**的（registry §3.3-A），不是状态。

    所以它不进 `INVENTORY_LOT_WORKFLOW`——一个"每天会自己变"的状态若进状态机，
    就需要一个定时任务去刷它，而那个任务失败时系统会静默地认为库存没过期。
    """
    import datetime

    if expiry is None:
        return False
    if isinstance(expiry, str):
        try:
            expiry = datetime.date.fromisoformat(expiry[:10])
        except ValueError:
            return False
    return expiry < datetime.date.today()


__all__ = [
    "DOC_TYPE_LABELS",
    "DOC_TYPES",
    "SOURCE_TYPE_LABELS",
    "WarehouseQueryService",
    "_allowed_actions",
    "_label_of",
]
