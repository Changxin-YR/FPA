"""Read path for the production domain.

Three resources share one shape, so the query building, scope application and
decoration live in `_ProductionReadService` and each resource subclass supplies
its joins, filters and labels. The old system instead had every resource carry
its own hand-written scope fragment (`production_store.py` vs
`warehouse_store.py` vs `master_data_store.py`), which is exactly how read and
write paths drifted apart.

## Decoration: the label has exactly one source

`status_label` / `batch_status_label` are produced ONLY here, from the Workflow.
The old project translated the same `verified` in 14 separate places, so the
cost page said "pending confirmation" while other pages said "verified". Deriving
the label from the state machine means a state cannot have two names.

`allowed_actions` is computed by the state machine and filtered by permission
server-side. Rendering an action the user cannot perform and letting the click
fail is just moving the validation cost onto the user.

## Version naming

The column is `row_version`, the API field is `version`. The legacy frontend
guessed this field name in four different places, so the mapping is done once,
here.
"""

from __future__ import annotations

from typing import Any

from fpa.domains._base import Page
from fpa.kernel.workflow import allowed_actions_for
from fpa.kernel import capability as cap
from fpa.kernel.errors import DomainError, ErrorCode, not_found
from fpa.kernel.scope import Scope
from fpa.kernel.uow import UnitOfWork
from fpa.kernel.workflow import RowAction, Workflow

from .service import (
    BATCH_STATUS_WORKFLOW,
    BATCH_WORKFLOW,
    FEEDING_WORKFLOW,
    HARVEST_WORKFLOW,
    batch_status_label,
    record_status_label,
)


def _md():
    """master_data's named read entry points (ROLLOUT_CONTRACT Sec 2.0).

    The rule binds this domain too: a caller needing another domain's table data
    uses the OWNER's named read-only function, in its BATCH form for list
    rendering. Imported in the function body per Sec 2 (module-level cross-domain
    imports create circular imports).
    """
    from fpa.domains.master_data.service import lookup_materials, lookup_ponds

    return lookup_ponds, lookup_materials


class _ProductionReadService:
    """Shared read behaviour for the three production resources."""

    #: Subclass supplies these.
    label: str = "记录"
    workflow: Workflow = BATCH_WORKFLOW
    scope_alias: str = "b"

    # -- helpers ------------------------------------------------------------

    def _scope_row(self, tx: UnitOfWork, scope: Scope, record_id: int) -> dict[str, Any]:
        """Load one row by primary key AND verify it is inside the data scope.

        This is the third defence layer. It checks the DATA, not the token: if the
        permission gate is ever bypassed (a new entry point that forgets the
        gateway), this still holds, because it asks "does this row belong to this
        user".
        """
        row = tx.query_one(self._select_one(), (record_id,))
        if row is None:
            raise not_found(self.label)
        if not scope.allows_row(row):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED,
                f"该{self.label}不在当前账号的数据范围内",
            )
        return row

    def _decorate(
        self,
        row: dict[str, Any],
        scope: Scope,
        permissions: frozenset[str],
        *,
        resource: str,
    ) -> dict[str, Any]:
        record_status = str(row.get("status") or "")
        decorated = dict(row)

        actions = allowed_actions_for(self.workflow, resource, record_status, permissions)

        decorated["version"] = int(row.get("row_version") or 1)
        decorated["status_label"] = record_status_label(record_status)
        # 批次是**双状态资源**：`status` 是记录生命周期、`batch_status` 是业务状态。
        # 列表列声明的是 `batch_status_label`（标签「批次状态」），而这一项原先谁也算 ——
        # 于是「批次状态」整列显示「—」（派生列声明了、值永远缺失，与
        # `partner_type_label` 同一类缺陷）。`available_transitions` 也依赖 `batch_status`。
        if row.get("batch_status") is not None:
            decorated["batch_status_label"] = batch_status_label(str(row["batch_status"]))
        decorated["allowed_actions"] = actions

        # DECIMAL columns arrive as Decimal; the wire format is a string so that
        # no float rounding ever touches a quantity.
        for column in ("initial_quantity", "initial_weight_kg", "quantity", "weight_kg"):
            if row.get(column) is not None:
                decorated[column] = str(row[column])
        return decorated

    def _list(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        resource: str,
        permission: str,
        query: dict[str, Any] | None,
        keyword_columns: tuple[str, ...],
        extra_filters: tuple[tuple[str, str], ...] = (),
        order: str = "id DESC",
    ) -> cap.HandlerResult:
        ctx.require(permission)
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))

        where = ["1=1"]
        values: list[Any] = []
        scope_fragment, scope_values = scope.where_clause(self.scope_alias)
        if scope_fragment and scope_fragment != "1=1":
            where.append(f"({scope_fragment})")
            values.extend(scope_values)

        keyword = str(params.get("keyword") or "").strip()
        if keyword and keyword_columns:
            clause = " OR ".join(f"{c} LIKE %s" for c in keyword_columns)
            where.append(f"({clause})")
            values.extend([f"%{keyword}%"] * len(keyword_columns))

        status = str(params.get("status") or "").strip()
        if status:
            where.append(f"{self.scope_alias}.status = %s")
            values.append(status)

        for param_name, column in extra_filters:
            value = str(params.get(param_name) or "").strip()
            if value:
                where.append(f"{column} = %s")
                values.append(value)

        clause = " AND ".join(where)
        total_row = tx.query_one(
            f"SELECT COUNT(*) AS n FROM {self._from_clause()} WHERE {clause}", values
        )
        total = int((total_row or {}).get("n", 0))

        rows = tx.query_all(
            f"{self._select_list()} WHERE {clause} ORDER BY {order} LIMIT %s OFFSET %s",
            [*values, page.size, page.offset],
        )
        rows = self._enrich(tx, rows)
        return cap.HandlerResult(
            data=page.to_result(
                [self._decorate(row, scope, ctx.actor.permissions, resource=resource) for row in rows],
                total,
            ),
            message="",
        )

    def _enrich(self, tx: UnitOfWork, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach cross-domain display columns via master_data's batch accessors.

        This replaces `LEFT JOIN ponds/areas/materials`. Sec 2.0 requires the batch
        form for list rendering: one query per foreign relation for the WHOLE page,
        never a JOIN (which reintroduces a second copy of the SQL and lets a schema
        change break silently) and never a per-row loop (N+1).
        """
        return rows

    def _from_clause(self) -> str:
        raise NotImplementedError

    def _select_list(self) -> str:
        raise NotImplementedError

    def _select_one(self) -> str:
        """Single-row SELECT by primary key, with its WHERE clause.

        Declared per resource rather than derived from the list query: each
        resource uses its own table alias (`b` / `f` / `h`), and building the
        predicate by string concatenation across aliases is how the old project's
        read and write paths drifted apart.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# batches
# ---------------------------------------------------------------------------


class BatchService(_ProductionReadService):
    label = "批次"
    workflow = BATCH_WORKFLOW

    _SELECT = """
        SELECT b.*
        FROM production_batches AS b
    """

    def _from_clause(self) -> str:
        return "production_batches AS b"

    def _select_list(self) -> str:
        return self._SELECT

    def _select_one(self) -> str:
        return self._SELECT + " WHERE b.id = %s"

    def _enrich(self, tx: UnitOfWork, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from fpa.domains.master_data.service import lookup_areas, lookup_ponds

        ponds = lookup_ponds(tx, pond_ids=[r["pond_id"] for r in rows if r.get("pond_id")])
        areas = lookup_areas(tx, area_ids=[r["area_id"] for r in rows if r.get("area_id")])
        for row in rows:
            pond = ponds.get(int(row["pond_id"])) if row.get("pond_id") else None
            area = areas.get(int(row["area_id"])) if row.get("area_id") else None
            row["pond_name"] = (pond or {}).get("name")
            row["area_name"] = (area or {}).get("name")
        return rows

    def list_batches(
        self, tx: UnitOfWork, ctx, scope: Scope, *, query: dict[str, Any] | None = None, **_: Any
    ) -> cap.HandlerResult:
        return self._list(
            tx, ctx, scope,
            resource="batch",
            permission="batch.view",
            query=query,
            keyword_columns=("b.code", "b.name"),
            extra_filters=(("pond_id", "b.pond_id"), ("batch_status", "b.batch_status")),
        )

    def get_batch_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        batch_id: int | None = None,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """Batch detail, including the legal `batch_status` targets from here.

        `available_transitions` is the runtime form of registry Sec 2.5's
        "server filters the choices by the transfer table": the client renders
        the options it is given instead of deriving them.
        """
        ctx.require("batch.view")
        raw = (path_params or {}).get("batch_id", batch_id)
        if raw is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "批次详情需要 path_params 传 batch_id")
        batch_id = int(raw)
        row = self._scope_row(tx, scope, batch_id)
        self._enrich(tx, [row])
        decorated = self._decorate(row, scope, ctx.actor.permissions, resource="batch")
        decorated["available_transitions"] = [
            {"value": state.code, "label": state.label}
            for state in BATCH_STATUS_WORKFLOW.available_transitions(str(row["batch_status"]))
        ]
        return cap.HandlerResult(data={"record": decorated})

    def load_batch(self, tx: UnitOfWork, *, scope: Scope, record_id: int) -> dict[str, Any] | None:
        row = tx.query_one(self._SELECT + " WHERE b.id = %s", (record_id,))
        if row is None:
            return None
        self._enrich(tx, [row])
        return {**row, "version": int(row.get("row_version") or 1)}


# ---------------------------------------------------------------------------
# feedings
# ---------------------------------------------------------------------------


class FeedingService(_ProductionReadService):
    label = "投喂记录"
    workflow = FEEDING_WORKFLOW
    # MUST be set per resource: the base default is the batch alias, and inheriting
    # it silently rendered the scope predicate against a non-existent table alias
    # ("Unknown column 'b.area_id'") for feeding/harvest lists.
    scope_alias = "f"

    _SELECT = """
        SELECT f.*, b.code AS batch_code
        FROM feedings AS f
        LEFT JOIN production_batches AS b ON b.id = f.batch_id
    """

    def _from_clause(self) -> str:
        return "feedings AS f"

    def _select_list(self) -> str:
        return self._SELECT

    def _select_one(self) -> str:
        return self._SELECT + " WHERE f.id = %s"

    def _enrich(self, tx: UnitOfWork, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        lookup_ponds, lookup_materials = _md()

        ponds = lookup_ponds(tx, pond_ids=[r["pond_id"] for r in rows if r.get("pond_id")])
        materials = lookup_materials(
            tx, material_ids=[r["material_id"] for r in rows if r.get("material_id")]
        )
        for row in rows:
            pond = ponds.get(int(row["pond_id"])) if row.get("pond_id") else None
            material = materials.get(int(row["material_id"])) if row.get("material_id") else None
            row["pond_name"] = (pond or {}).get("name")
            row["material_name"] = (material or {}).get("name")
        return rows

    def list_feedings(
        self, tx: UnitOfWork, ctx, scope: Scope, *, query: dict[str, Any] | None = None, **_: Any
    ) -> cap.HandlerResult:
        return self._list(
            tx, ctx, scope,
            resource="feeding",
            permission="feeding.view",
            query=query,
            keyword_columns=("f.code", "f.name"),
            extra_filters=(("pond_id", "f.pond_id"), ("batch_id", "f.batch_id")),
        )

    def get_feeding_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        feeding_id: int | None = None,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        ctx.require("feeding.view")
        raw = (path_params or {}).get("feeding_id", feeding_id)
        if raw is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "投喂详情需要 path_params 传 feeding_id")
        feeding_id = int(raw)
        row = self._scope_row(tx, scope, feeding_id)
        self._enrich(tx, [row])
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    row, scope, ctx.actor.permissions, resource="feeding"
                )
            },
        )

    def load_feeding(self, tx: UnitOfWork, *, scope: Scope, record_id: int) -> dict[str, Any] | None:
        row = tx.query_one(self._SELECT + " WHERE f.id = %s", (record_id,))
        if row is None:
            return None
        self._enrich(tx, [row])
        return {**row, "version": int(row.get("row_version") or 1)}


# ---------------------------------------------------------------------------
# harvests
# ---------------------------------------------------------------------------


class HarvestService(_ProductionReadService):
    label = "出塘记录"
    workflow = HARVEST_WORKFLOW
    scope_alias = "h"

    _SELECT = """
        SELECT h.*, b.code AS batch_code
        FROM harvests AS h
        LEFT JOIN production_batches AS b ON b.id = h.batch_id
    """

    def _from_clause(self) -> str:
        return "harvests AS h"

    def _select_list(self) -> str:
        return self._SELECT

    def _select_one(self) -> str:
        return self._SELECT + " WHERE h.id = %s"

    def _enrich(self, tx: UnitOfWork, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        lookup_ponds, _ = _md()

        ponds = lookup_ponds(tx, pond_ids=[r["pond_id"] for r in rows if r.get("pond_id")])
        for row in rows:
            pond = ponds.get(int(row["pond_id"])) if row.get("pond_id") else None
            row["pond_name"] = (pond or {}).get("name")
        return rows

    def list_harvests(
        self, tx: UnitOfWork, ctx, scope: Scope, *, query: dict[str, Any] | None = None, **_: Any
    ) -> cap.HandlerResult:
        return self._list(
            tx, ctx, scope,
            resource="harvest",
            permission="harvest.view",
            query=query,
            keyword_columns=("h.code", "h.name"),
            extra_filters=(("pond_id", "h.pond_id"), ("batch_id", "h.batch_id")),
        )

    def get_harvest_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        harvest_id: int | None = None,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        ctx.require("harvest.view")
        raw = (path_params or {}).get("harvest_id", harvest_id)
        if raw is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "出塘详情需要 path_params 传 harvest_id")
        harvest_id = int(raw)
        row = self._scope_row(tx, scope, harvest_id)
        self._enrich(tx, [row])
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    row, scope, ctx.actor.permissions, resource="harvest"
                )
            },
        )

    def load_harvest(self, tx: UnitOfWork, *, scope: Scope, record_id: int) -> dict[str, Any] | None:
        row = tx.query_one(self._SELECT + " WHERE h.id = %s", (record_id,))
        if row is None:
            return None
        self._enrich(tx, [row])
        return {**row, "version": int(row.get("row_version") or 1)}


__all__ = ["BatchService", "FeedingService", "HarvestService"]
