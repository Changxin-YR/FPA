"""Write path for the production domain: batches / feeding / harvests.

## The uniform shape of a write capability

Every write method does four things in a fixed order:

    1. ctx.require(...)                 third-layer permission check (never trust the caller)
    2. _scope_row(...)                  third-layer scope check on the CONCRETE ROW
    3. state-machine validation         require_action / require_transition
    4. write + return HandlerResult(resource_id=...)

Step 4's `resource_id` is NOT optional: the runner reloads by it to verify the write
really landed. Without it, "executed" would degrade into "we called INSERT".

## Cross-domain side effects are NOT implemented here

`feeding.verify` has cross-domain effects. Per ROLLOUT_CONTRACT Sec 2 a domain may
NEVER read or write another domain's tables; the effect travels through the owning
domain's named in-process function, inside the SAME transaction. So this module
contains no second copy of the inventory ledger and no cost ledger: it calls
`warehouse.ledger.apply_movement` and `cost.entries.record_fact`.

## The one thing that is easy to get wrong about the stock ledger

`harvest.verify` is the ONLY capability in the system that reduces pond stock. It
appends a NEGATIVE row to `batch_stock_records`, after invariant #2 has confirmed
the post-write balance is not negative.

`feeding.verify` does NOT reduce pond stock -- feeding consumes MATERIAL inventory,
not pond stock -- so the row it appends carries ZERO quantity/weight deltas. If it
carried a negative delta, `batch.close` would become reachable while the pond still
holds fish.

## Duplicate codes: why the service translates, not the invariant

registry Sec 4 #19 wants the DB unique key as the correctness floor plus an
application-layer translation into a readable 409 -- and explicitly not the old
system's message-text matching. `UniqueCode` cannot supply that message: it is a
POST-write invariant, and the INSERT fails first, so it never executes for a
duplicate. See `_translate_conflict`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fpa.kernel import capability as cap
from fpa.kernel.errors import DomainError, ErrorCode
from fpa.kernel.fields import f_enum, f_int, f_num, f_ref, f_str, text
from fpa.kernel.scope import Scope
from fpa.kernel.uow import UnitOfWork
from fpa.kernel.workflow import RowAction

from .read import BatchService, FeedingService, HarvestService
from .service import (
    BATCH_STATUS_WORKFLOW,
    BATCH_WORKFLOW,
    FEEDING_WORKFLOW,
    HARVEST_WORKFLOW,
)

#: Upper bound inherited from the old system (`production_service.py:35`),
#: aligned with DECIMAL(18,3).
MAX_PRODUCTION_QUANTITY = 999999999999999.999

#: 可设置的批次状态。**不是** `states`：`closed` 标了 `terminal=True`（进入后不能再转移），
#: 而本列表的消费点是“用户提交的目标状态是否合法”。它仍然是 `batch.close`
#: （其他能力）的合法目标，所以下面仍保留对 `closed` 的专项拒绝分支。
_BATCH_STATUS_CODES = tuple(state.code for state in BATCH_STATUS_WORKFLOW.selectable_states())


def _non_null(**fields: Any) -> dict[str, Any]:
    """Keep only submitted fields (None means "not submitted")."""
    return {key: value for key, value in fields.items() if value is not None}


def _md():
    """Return master_data's named read entry points (ROLLOUT_CONTRACT Sec 2.0).

    Imported inside the function body per Sec 2: cross-domain imports at module
    level create circular imports. These accessors replace direct reads of
    master_data's tables -- the rule is "the OWNER provides a named read-only
    function", and it binds me as much as it binds anyone asking me for batches.
    """
    try:
        from fpa.domains.master_data.service import (
            lookup_area,
            lookup_areas,
            lookup_material,
            lookup_materials,
            lookup_pond,
            lookup_ponds,
        )
    except Exception as exc:  # noqa: BLE001
        raise DomainError(
            ErrorCode.INTERNAL_ERROR,
            "主数据跨域只读入口不可用（ROLLOUT_CONTRACT 第 2.0 节）；"
            "本域不得直接读 ponds / areas / materials",
            data={"missing": "master_data.service.lookup_*"},
        ) from exc
    return lookup_pond, lookup_ponds, lookup_area, lookup_areas, lookup_material, lookup_materials


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _translate_conflict(exc: Exception, *, code: str, what: str) -> DomainError:
    """Turn a raw duplicate-key IntegrityError into a readable CONFLICT (409).

    registry Sec 4 #19 assigns this translation to the application layer: the DB
    unique key is the floor, the application layer phrases it -- and it must NOT do
    what the old system did and inspect the exception message text
    (`production_store.py:142-146`). The kernel ships the canonical errno mapping,
    so this delegates to `UnitOfWork.translate_mysql_error`.

    `UniqueCode` cannot produce this message: it is a POST-write invariant, but the
    INSERT fails first, so it never executes for a duplicate (verified by tracing
    `UniqueCode.check` across a duplicate create: zero calls). Uniqueness is still
    guaranteed -- by the DB unique key, the layer that also holds under concurrency.
    """
    from fpa.kernel.uow import translate_mysql_error

    translated = translate_mysql_error(exc)
    if translated is not None and translated.code is ErrorCode.CONFLICT:
        return DomainError(
            ErrorCode.CONFLICT,
            f"{what}「{code}」已存在，请换一个编号",
            data={"rule": "UNIQUE_CODE", "field": "code", "code": code},
        )
    raise exc


def _as_date(value: Any) -> Any:
    """Normalise a datetime/date/ISO-string to a `date`.

    `record_fact` accepts `date | str`, but passing the documented primary form
    (`date`) is more honest than relying on the callee's parsing, and it keeps the
    period the cost is booked into unambiguous.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    text = str(value)
    return text[:10]

def _now_is_before(value: Any, field: str, what: str) -> None:
    """`value` must not be in the future (inherited `production_service.py:91-112`)."""
    if value is None:
        return
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR, f"{what}格式无效", data={"field": field}
            ) from exc
    else:
        parsed = value
    if parsed > datetime.now():
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, f"{what}不得晚于当前时间", data={"field": field}
        )


# ---------------------------------------------------------------------------
# Cross-domain entry points (ROLLOUT_CONTRACT Sec 2)
#
# The import lives inside the function body: cross-domain imports at module level
# create circular imports.
# ---------------------------------------------------------------------------


def _warehouse_entry_points():
    """Return warehouse's inventory entry points, or fail with an actionable error.

    `warehouse.ledger.apply_movement(...)` is the ONE function in the system that
    may change inventory (ROLLOUT_CONTRACT Sec 2). This domain must never write
    `inventory_ledger` itself.
    """
    try:
        from fpa.domains.warehouse.ledger import (
            apply_movement,
            ledger_source_ref,
            resolve_issue_lot,
        )
    except Exception as exc:  # noqa: BLE001
        raise DomainError(
            ErrorCode.INTERNAL_ERROR,
            "投喂核验需要扣减物料库存，但 warehouse 域的跨域入口不可用"
            "（ROLLOUT_CONTRACT 第 2 节：warehouse.ledger.apply_movement）。"
            "该效果不得由 production 域自行实现。",
            data={"missing": "warehouse.ledger.apply_movement"},
        ) from exc
    return apply_movement, resolve_issue_lot, ledger_source_ref


def _require_cost_record_fact():
    """Return `cost.entries.record_fact`, or fail with an actionable error."""
    try:
        from fpa.domains.cost.entries import (
            LEDGER_FEED,
            MATERIAL_CATEGORY_TO_COST,
            record_fact,
        )
    except Exception as exc:  # noqa: BLE001
        raise DomainError(
            ErrorCode.INTERNAL_ERROR,
            "核验需要归集成本，但 cost 域的跨域入口 `cost.entries.record_fact` "
            "尚未交付（ROLLOUT_CONTRACT 第 2 节）。该效果不得由 production 域自行实现。",
            data={"missing": "cost.entries.record_fact"},
        ) from exc
    return record_fact


def _cost_category(material_category: str) -> str:
    """Map a material's category to cost's category code.

    Delegates to cost's own table so the mapping has ONE definition (a second copy
    here would drift the moment cost adds a category). Falls back to `feed`, the
    conservative default for a feeding cost, for a category cost does not know.
    """
    try:
        from fpa.domains.cost.entries import LEDGER_FEED, MATERIAL_CATEGORY_TO_COST
    except Exception as exc:  # noqa: BLE001
        raise DomainError(
            ErrorCode.INTERNAL_ERROR,
            "成本类别映射不可用：cost 域未暴露 MATERIAL_CATEGORY_TO_COST",
            data={"missing": "cost.entries.MATERIAL_CATEGORY_TO_COST"},
        ) from exc
    return MATERIAL_CATEGORY_TO_COST.get(material_category, LEDGER_FEED)


def _ledger_source_ref(domain: str, code: str) -> str:
    """Build the ledger's `<domain>:<code>` source reference.

    Delegates to warehouse so the format has ONE definition. Passing a raw code
    would break the ledger unique key, because codes of different document tables
    are not globally unique.
    """
    return _warehouse_entry_points()[2](domain, code)

class BatchWriteService(BatchService):
    """Batch write path.

    Inherits `BatchService` so read and write share ONE `_scope_row` and ONE
    `_decorate`. The old system built read and write scope fragments separately,
    and they disagreed.
    """

    def create_batch(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("批次编号", required=True, max_length=64),
        name: f_str("批次名称", required=True, max_length=100),
        pond_id: f_ref("塘口", "pond", required=True),
        species: f_str("品种", required=True, max_length=64),
        stocked_at: f_str("放苗时间", required=True),
        initial_quantity: f_num("放苗数量", minimum=0, maximum=MAX_PRODUCTION_QUANTITY),
        initial_weight_kg: f_num("放苗重量（kg）", minimum=0, maximum=MAX_PRODUCTION_QUANTITY),
        expected_harvest_date: f_str("预计出塘日期"),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        ctx.require("batch.create")

        lookup_pond = _md()[0]
        pond = lookup_pond(tx, pond_id=int(pond_id))
        if pond is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "所属塘口不存在", data={"field": "pond_id"}
            )
        if not scope.allows_row(pond):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED, "所属塘口不在当前账号的数据范围内"
            )
        if str(pond["status"]) != "verified":
            raise DomainError(
                ErrorCode.CONFLICT,
                "塘口尚未核验，不能建档放苗",
                data={"field": "pond_id", "pond_status": str(pond["status"])},
            )

        _now_is_before(stocked_at, "stocked_at", "放苗时间")
        if expected_harvest_date and stocked_at:
            if str(expected_harvest_date) < str(stocked_at)[:10]:
                raise DomainError(
                    ErrorCode.VALIDATION_ERROR,
                    "预计出塘日期不得早于放苗时间",
                    data={"field": "expected_harvest_date"},
                )

        try:
            tx.execute(
                "INSERT INTO production_batches "
                "(organization_id, farm_id, area_id, code, name, pond_id, species, "
                " initial_quantity, initial_weight_kg, stocked_at, expected_harvest_date, "
                " note, batch_status, status, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'stocked','draft',%s)",
                (
                    pond["organization_id"],
                    pond["farm_id"],
                    pond["area_id"],
                    code,
                    name,
                    int(pond_id),
                    species,
                    None if initial_quantity is None else _decimal(initial_quantity),
                    None if initial_weight_kg is None else _decimal(initial_weight_kg),
                    stocked_at,
                    expected_harvest_date,
                    note,
                    ctx.actor.user_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - translated below
            raise _translate_conflict(exc, code=code, what="批次编号") from exc

        batch_id = tx.last_insert_id()
        row = self.load_batch(tx, scope=scope, record_id=batch_id)
        assert row is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    row, scope, ctx.actor.permissions, resource="batch"
                ),
                "_invariant_context": {
                    "organization_id": pond["organization_id"],
                    "farm_id": pond["farm_id"],
                    "area_id": pond["area_id"],
                },
            },
            resource_id=batch_id,
            message=f"已创建批次「{name}」（编号 {code}）",
        )
    def update_batch(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        batch_id: f_int("批次 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
        name: f_str("批次名称", max_length=100),
        species: f_str("品种", max_length=64),
        initial_quantity: f_num("放苗数量", minimum=0, maximum=MAX_PRODUCTION_QUANTITY),
        initial_weight_kg: f_num("放苗重量（kg）", minimum=0, maximum=MAX_PRODUCTION_QUANTITY),
        expected_harvest_date: f_str("预计出塘日期"),
        batch_status: f_enum("批次状态", ()),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        """Edit a batch, and drive the business-state machine.

        `batch_status` is accepted here (registry Sec 3.2-B): the old system drove
        `stocked -> farming -> pending_settlement` through a separate
        `change_batch_status` endpoint (`production_store.py:219-241`); this version
        folds it into update. Entering `pending_settlement` requires that the batch
        has no unfinished production work, and `closed` is NOT reachable from here --
        only `batch.close` may close a batch.
        """
        ctx.require("batch.update")
        row = self._scope_row(tx, scope, int(batch_id))

        # Invariant #12 (verified records are read-only) guards RECORD FIELDS, not the
        # business state machine. These are two separate concerns that `batch.update`
        # happens to carry at once (registry Sec 3.2-B folded the old
        # `change_batch_status` endpoint into update), so they must be checked
        # separately -- otherwise a verified batch could never advance
        # stocked -> farming and the whole batch lifecycle would be unreachable,
        # exactly the failure DECISIONS.md Q10 warns about.
        #
        # The 负责人 approved this as a NAMED CATEGORY of exception: this capability
        # deliberately does NOT declare `StatusAllowsEdit`. #12 is enforced here at
        # the service layer instead, because the kernel invariant sees only a single
        # `status` field and cannot tell "ledger fields were edited" from "only the
        # business state advanced". `batch_status` is a closed enum, so the branch
        # that skips the edit check is fail-closed: only the four declared states can
        # reach it, and the transfer itself is still validated by the state machine
        # below.
        record_fields_submitted = any(
            value is not None
            for value in (
                name, species, initial_quantity, initial_weight_kg,
                expected_harvest_date, note,
            )
        )
        verified_record = str(row["status"]) not in ("draft", "submitted")

        if record_fields_submitted or not verified_record:
            BATCH_WORKFLOW.require_action(str(row["status"]), RowAction.EDIT)
            if verified_record:
                raise DomainError(
                    ErrorCode.CONFLICT,
                    "该批次已核验，台账字段不能修改；如需调整请作废重开",
                    data={"status": str(row["status"]), "rule": "RECORD_READ_ONLY"},
                )

        patch = _non_null(
            name=name,
            species=species,
            initial_quantity=None if initial_quantity is None else _decimal(initial_quantity),
            initial_weight_kg=None if initial_weight_kg is None else _decimal(initial_weight_kg),
            expected_harvest_date=expected_harvest_date,
            note=note,
        )

        if batch_status is not None:
            target = str(batch_status)
            current = str(row["batch_status"])
            if target not in _BATCH_STATUS_CODES:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    "批次状态无效",
                    data={"field": "batch_status", "allowed": list(_BATCH_STATUS_CODES)},
                )
            if target != current:
                if target == "closed":
                    raise DomainError(
                        ErrorCode.CONFLICT,
                        "关闭批次请使用 batch.close",
                        data={"rule": "STATE_TRANSITION"},
                    )
                BATCH_STATUS_WORKFLOW.require_transition(current, target, action="batch.update")
                # `farming -> pending_settlement` requires no unfinished production
                # work (inherited from `production_store.py:225-227`, BATCH_OPEN_WORK).
                if target == "pending_settlement":
                    open_work = tx.query_one(
                        "SELECT COUNT(*) AS n FROM feedings "
                        "WHERE batch_id = %s AND status = 'draft'",
                        (int(batch_id),),
                    )
                    if int((open_work or {}).get("n", 0)) > 0:
                        raise DomainError(
                            ErrorCode.CONFLICT,
                            "该批次还有未核验的投喂记录，不能进入待结算",
                            data={"rule": "BATCH_OPEN_WORK"},
                        )
                patch["batch_status"] = target

        if not patch:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "没有需要更新的内容")

        assignments = ", ".join(f"{key}=%s" for key in patch)
        affected = tx.execute(
            f"UPDATE production_batches SET {assignments}, "
            "row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s",
            [*patch.values(), ctx.actor.user_id, int(batch_id), int(expected_version)],
        )
        if affected == 0:
            current_row = self._scope_row(tx, scope, int(batch_id))
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该批次已被他人修改，请刷新后重试",
                data={"current_version": int(current_row["row_version"])},
            )

        updated = self.load_batch(tx, scope=scope, record_id=int(batch_id))
        assert updated is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    updated, scope, ctx.actor.permissions, resource="batch"
                ),
                "_invariant_context": {
                    "organization_id": row["organization_id"],
                    "status": str(updated["status"]),
                },
            },
            resource_id=int(batch_id),
            message="批次已更新",
        )

    def submit_batch(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        batch_id: f_int("批次 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """draft -> submitted.

        The permission is `batch.update`, NOT `batch.submit` (registry Sec 1.5):
        submitting only advances the review queue, so it needs no separate grant.
        """
        return self._lifecycle(
            tx, ctx, scope,
            batch_id=int(batch_id),
            expected_version=int(expected_version),
            from_states=("draft",),
            to_state="submitted",
            action=RowAction.SUBMIT,
            permission="batch.update",
            message="批次已提交核验",
        )

    def verify_batch(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        batch_id: f_int("批次 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """submitted -> verified, and form the INITIAL pond stock.

        Two effects beyond the status change:

        1. The initial `stocking` row is appended to `batch_stock_records`
           (`+initial_quantity`, `+initial_weight_kg`), inherited from
           `production_store.py:248-249`. This is what `harvest.verify` later
           reduces, and what invariant #11 requires to reach zero before closing.
        2. `batch_status` becomes `stocked`, making `stocked -> farming` available.

        Self-approval is forbidden (invariant #5). The check is ALSO encoded in the
        UPDATE's WHERE clause, because the strength of a compliance rule is set by
        the weakest path, not the strongest: an invariant guards the runner, but a
        background job calling this service directly is not the runner.
        """
        ctx.require("batch.verify")
        row = self._scope_row(tx, scope, int(batch_id))
        BATCH_WORKFLOW.require_action(str(row["status"]), RowAction.VERIFY)

        if int(row["created_by"]) == ctx.actor.user_id:
            raise DomainError(
                ErrorCode.FORBIDDEN,
                "经办人不能核验自己提交的批次，请由他人复核",
                data={"rule": "DISTINCT_ACTORS"},
            )

        affected = tx.execute(
            "UPDATE production_batches SET status='verified', batch_status='stocked', "
            "verified_by=%s, verified_at=NOW(), row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s AND status='submitted' AND created_by <> %s",
            (
                ctx.actor.user_id,
                ctx.actor.user_id,
                int(batch_id),
                int(expected_version),
                ctx.actor.user_id,
            ),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该批次状态已变化或已被他人修改，请刷新后重试",
            )

        quantity = row.get("initial_quantity")
        weight = row.get("initial_weight_kg")
        if quantity is not None or weight is not None:
            tx.execute(
                "INSERT INTO batch_stock_records "
                "(organization_id, farm_id, area_id, batch_id, pond_id, movement_type, "
                " quantity_delta, weight_delta_kg, happened_at, source_type, source_id, "
                " note, created_by) "
                "VALUES (%s,%s,%s,%s,%s,'stocking',%s,%s,COALESCE(%s,NOW()),"
                " 'production_batch',%s,%s,%s)",
                (
                    row["organization_id"],
                    row["farm_id"],
                    row["area_id"],
                    int(batch_id),
                    int(row["pond_id"]),
                    _decimal(quantity or 0),
                    _decimal(weight or 0),
                    row.get("stocked_at"),
                    int(batch_id),
                    f"批次核验形成初始存塘（{row['code']}）",
                    ctx.actor.user_id,
                ),
            )

        verified = self.load_batch(tx, scope=scope, record_id=int(batch_id))
        assert verified is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    verified, scope, ctx.actor.permissions, resource="batch"
                ),
                "_invariant_context": {
                    "organization_id": row["organization_id"],
                    "_ledger_lines": [
                        {"batch_id": int(batch_id), "pond_id": int(row["pond_id"])}
                    ],
                },
            },
            resource_id=int(batch_id),
            message=f"批次「{verified['name']}」已核验，已形成初始存塘",
        )

    def close_batch(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        batch_id: f_int("批次 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """pending_settlement -> closed. Stock must be zero.

        The permission is `batch.verify` (registry Sec 1.5), not `batch.close`.

        Reachability, recorded because it was the key finding of the capability
        review (DECISIONS.md Q10): the transfer into `closed` requires pond stock to
        be zero, and `harvest.verify` is the ONLY capability that reduces pond stock.
        The zero-stock rule itself is invariant #11 (`ZeroBalance`); this method only
        performs the transition and lets the kernel reject it when stock remains.
        """
        ctx.require("batch.verify")
        row = self._scope_row(tx, scope, int(batch_id))
        BATCH_STATUS_WORKFLOW.require_transition(
            str(row["batch_status"]), "closed", action="batch.close"
        )

        affected = tx.execute(
            "UPDATE production_batches SET batch_status='closed', "
            "row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s AND batch_status='pending_settlement'",
            (ctx.actor.user_id, int(batch_id), int(expected_version)),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该批次状态已变化或已被他人修改，请刷新后重试",
            )

        closed = self.load_batch(tx, scope=scope, record_id=int(batch_id))
        assert closed is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    closed, scope, ctx.actor.permissions, resource="batch"
                ),
                "_invariant_context": {
                    "organization_id": row["organization_id"],
                    "_ledger_lines": [{"batch_id": int(batch_id)}],
                },
            },
            resource_id=int(batch_id),
            message=f"批次「{closed['name']}」已关闭",
        )

    def _lifecycle(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        batch_id: int,
        expected_version: int,
        from_states: tuple[str, ...],
        to_state: str,
        action: RowAction,
        permission: str,
        message: str,
    ) -> cap.HandlerResult:
        """Shared record-lifecycle transition."""
        ctx.require(permission)
        row = self._scope_row(tx, scope, batch_id)
        BATCH_WORKFLOW.require_action(str(row["status"]), action)
        if str(row["status"]) not in from_states:
            raise DomainError(
                ErrorCode.CONFLICT,
                "当前状态不允许该操作",
                data={"status": str(row["status"]), "allowed_from": list(from_states)},
            )

        placeholders = ",".join(["%s"] * len(from_states))
        affected = tx.execute(
            f"UPDATE production_batches SET status=%s, row_version=row_version+1, "
            f"updated_by=%s WHERE id=%s AND row_version=%s AND status IN ({placeholders})",
            (to_state, ctx.actor.user_id, batch_id, expected_version, *from_states),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT, "该批次已被他人修改，请刷新后重试"
            )

        updated = self.load_batch(tx, scope=scope, record_id=batch_id)
        assert updated is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    updated, scope, ctx.actor.permissions, resource="batch"
                ),
                "_invariant_context": {
                    "organization_id": row["organization_id"],
                    "status": str(updated["status"]),
                },
            },
            resource_id=batch_id,
            message=message,
        )


class FeedingWriteService(FeedingService):
    """Feeding write path: create + verify."""

    def create_feeding(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("投喂单号", required=True, max_length=64),
        name: f_str("投喂事项", required=True, max_length=100),
        pond_id: f_ref("塘口", "pond", required=True),
        batch_id: f_ref("批次", "batch", required=True),
        material_id: f_ref("饲料", "material", required=True),
        happened_at: f_str("投喂时间", required=True),
        quantity: f_num("投喂量", minimum=0, maximum=MAX_PRODUCTION_QUANTITY),
        weight_kg: f_num("重量（kg）", minimum=0, maximum=MAX_PRODUCTION_QUANTITY),
        warehouse_id: f_ref("出库仓", "warehouse"),
        lot_no: f_str("物料批次号", max_length=64),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        """Register a feeding as `draft`.

        Invariant #16 (`AtLeastOneOf(quantity, weight_kg)`) is enforced by the
        kernel, not repeated here: two places describing one rule is the defect this
        project exists to remove.

        ## `warehouse_id` / `lot_no` are OPTIONAL on purpose

        They were added by 负责人 ruling because the choice of "which warehouse,
        which material lot" was being made SERVER-SIDE (default warehouse + FEFO)
        and the user could not see it in advance or override it. The registry's field
        table is a DERIVED fact (it inherits the old frontend's fields, Sec 2.1),
        not a normative decision that users must not choose a lot -- treating an
        inherited status quo as a design decision is what let a consequential choice
        hide behind a default.

        Leaving both empty keeps the previous behaviour exactly (default warehouse +
        FEFO). Supplying them pins the choice. Either way the user is TOLD which lot
        was used and WHY, in the verify message.

        They are stored on the row rather than merely passed through, because
        create and verify are separate transactions: re-deriving with FEFO at verify
        time would make the lot shown to the user at create time a lie whenever FEFO
        picked something else in between.
        """
        ctx.require("feeding.create")

        pond = _md()[0](tx, pond_id=int(pond_id))
        if pond is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "所属塘口不存在", data={"field": "pond_id"}
            )
        if not scope.allows_row(pond):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED, "所属塘口不在当前账号的数据范围内"
            )

        # registry Sec 2.6: the batch MUST belong to the chosen pond. NOTE the tenant
        # columns are included because `scope.allows_row()` reads the scope column
        # (area_id) off this row; omitting them fails the scope check for a reason
        # that looks nothing like its cause.
        batch = tx.query_one(
            "SELECT id, pond_id, batch_status, organization_id, farm_id, area_id "
            "FROM production_batches WHERE id = %s",
            (int(batch_id),),
        )
        if batch is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "批次不存在", data={"field": "batch_id"}
            )
        if int(batch["pond_id"]) != int(pond_id):
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "所选批次不属于该塘口",
                data={"field": "batch_id"},
            )
        if not scope.allows_row(batch):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED, "所选批次不在当前账号的数据范围内"
            )

        material = _md()[4](tx, material_id=int(material_id))
        if material is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "饲料不存在", data={"field": "material_id"}
            )
        if str(material["status"]) != "verified":
            raise DomainError(
                ErrorCode.CONFLICT,
                "所选物料尚未核验，不能用于投喂",
                data={"field": "material_id", "status": str(material["status"])},
            )

        _now_is_before(happened_at, "happened_at", "投喂时间")

        try:
            tx.execute(
                "INSERT INTO feedings "
                "(organization_id, farm_id, area_id, code, name, pond_id, batch_id, "
                " material_id, quantity, weight_kg, happened_at, note, status, created_by, "
                " warehouse_id, lot_no) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s,%s,%s)",
                (
                    pond["organization_id"],
                    pond["farm_id"],
                    pond["area_id"],
                    code,
                    name,
                    int(pond_id),
                    int(batch_id),
                    int(material_id),
                    None if quantity is None else _decimal(quantity),
                    None if weight_kg is None else _decimal(weight_kg),
                    happened_at,
                    note,
                    ctx.actor.user_id,
                    # Cross-domain reference: BIGINT only, no FK (Sec 1.1).
                    None if warehouse_id is None else int(warehouse_id),
                    None if lot_no is None else str(lot_no),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - translated below
            raise _translate_conflict(exc, code=code, what="投喂单号") from exc

        feeding_id = tx.last_insert_id()
        row = self.load_feeding(tx, scope=scope, record_id=feeding_id)
        assert row is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(row, scope, ctx.actor.permissions, resource="feeding"),
                "_invariant_context": {
                    "organization_id": pond["organization_id"],
                    "farm_id": pond["farm_id"],
                    "area_id": pond["area_id"],
                },
            },
            resource_id=feeding_id,
            message=f"已登记投喂「{name}」（编号 {code}），待核验",
        )

    def verify_feeding(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        feeding_id: f_int("投喂记录 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """draft -> verified, with three side effects (registry Sec 3.3).

            1. debit `inventory_ledger`     (material stock leaves the warehouse)
            2. write `batch_stock_records`  (cost record only; pond stock UNCHANGED)
            3. create `cost_entries`        (source_type = warehouse_ledger)

        Effect 2 is the one that is easy to get wrong: feeding does NOT reduce pond
        stock, so its ledger row carries zero quantity/weight deltas. If it carried a
        negative delta, `batch.close` would become reachable while the pond still
        holds fish.

        Effects 1 and 3 are CROSS-DOMAIN and therefore travel through the owning
        domains' entry points inside this same transaction (ROLLOUT_CONTRACT Sec 2).
        """
        ctx.require("feeding.verify")
        row = self._scope_row(tx, scope, int(feeding_id))
        FEEDING_WORKFLOW.require_action(str(row["status"]), RowAction.VERIFY)

        affected = tx.execute(
            "UPDATE feedings SET status='verified', verified_by=%s, verified_at=NOW(), "
            "row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s AND status='draft'",
            (ctx.actor.user_id, ctx.actor.user_id, int(feeding_id), int(expected_version)),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该投喂记录状态已变化或已被他人修改，请刷新后重试",
            )

        # (1) DEBIT MATERIAL INVENTORY -- cross-domain, owned by warehouse.
        #
        # The lot is resolved FIRST, separately from the deduction: the warehouse
        # contract is explicit that keeping resolution and judgement apart lets the
        # caller say "this lot does not have enough" before any write happens.
        # `resolve_issue_lot` applies FEFO (earliest expiry first).
        apply_movement, resolve_issue_lot, _ = _warehouse_entry_points()

        # Honour the OPTIONAL hints captured at create time. Leaving them empty
        # keeps the original behaviour (warehouse's default warehouse + FEFO).
        #
        # The resolution RULE itself lives entirely in warehouse's
        # `resolve_issue_lot` (explicit lot -> explicit warehouse + FEFO -> default
        # warehouse + FEFO). This call deliberately does not re-implement any of
        # that precedence: splitting one rule across two domains is what produces
        # "each domain got half of it right".
        hinted_warehouse = row.get("warehouse_id")
        hinted_lot = row.get("lot_no")
        issue_lot = resolve_issue_lot(
            tx,
            scope=scope,
            material_id=int(row["material_id"]),
            warehouse_id=None if hinted_warehouse is None else int(hinted_warehouse),
            lot_no="" if not hinted_lot else str(hinted_lot),
        )

        # 负责人's constraint on adding these fields: the DEFAULT must be
        # explainable, not merely visible. Rendering "lot E2E-LOT-1" tells the user
        # what happened but not why, which is a default with no stated reason. So the
        # choice is always accompanied by its basis.
        if hinted_lot:
            lot_basis = "按登记时指定的物料批次扣减"
        elif hinted_warehouse:
            lot_basis = (
                f"未指定批次，在该仓内按先到期先出（FEFO）选出 {issue_lot.lot_no}"
            )
        else:
            lot_basis = (
                f"未指定仓库与批次，按默认仓「{issue_lot.warehouse_name}」"
                f"先到期先出（FEFO）选出 {issue_lot.lot_no}"
            )
        movement = apply_movement(
            tx,
            scope=scope,
            actor_id=ctx.actor.user_id,
            source_type="issue",
            source_ref=_ledger_source_ref("feeding", str(row["code"])),
            source_line_no=1,
            warehouse_id=int(issue_lot.warehouse_id),
            material_id=int(row["material_id"]),
            quantity_delta=-_decimal(row.get("quantity") or 0),
            happened_at=row["happened_at"],
            lot_no=str(issue_lot.lot_no),
            create_lot=False,
            pond_id=int(row["pond_id"]),
            batch_id=int(row["batch_id"]),
        )

        # (2) POND-STOCK LEDGER ROW -- cost record only, ZERO deltas.
        tx.execute(
            "INSERT INTO batch_stock_records "
            "(organization_id, farm_id, area_id, batch_id, pond_id, movement_type, "
            " quantity_delta, weight_delta_kg, happened_at, source_type, source_id, "
            " note, created_by) "
            "VALUES (%s,%s,%s,%s,%s,'feeding',0,0,%s,'feeding',%s,%s,%s)",
            (
                row["organization_id"],
                row["farm_id"],
                row["area_id"],
                int(row["batch_id"]),
                int(row["pond_id"]),
                row["happened_at"],
                int(feeding_id),
                f"投喂核验登记成本（{row['code']}）；投喂不改变塘内存塘",
                ctx.actor.user_id,
            ),
        )

        # (3) COST COLLECTION -- cross-domain, owned by cost.
        #
        # The amount is this domain's business figure: the value of the feed actually
        # consumed. Nothing in the warehouse schema stores a per-lot cost basis
        # (`inventory_lots` has no cost column, and an ISSUE ledger row carries
        # unit_cost = NULL unless the caller passes one), so the valuation uses the
        # lot's WEIGHTED AVERAGE receipt cost, computed from the ledger itself:
        #
        #     unit_cost = SUM(receipt qty x receipt unit_cost) / SUM(receipt qty)
        #
        # Reasons: it uses only facts already in the ledger; it is deterministic; it
        # is moving-average inventory valuation; it stays correct when the same lot is
        # received again at a different price. FEFO picks exactly ONE lot per movement
        # (`_pick_lot_fefo` returns the single best lot), so there is no multi-lot
        # split needing layer-by-layer costing.
        # The material's category decides the cost category; read it here because
        # `verify` starts from the feeding row, which carries only `material_id`.
        material = _md()[4](tx, material_id=int(row["material_id"])) or {}

        valuation = tx.query_one(
            "SELECT SUM(quantity_delta * unit_cost) AS cost_pool, "
            "       SUM(quantity_delta) AS received_qty "
            "FROM inventory_ledger "
            "WHERE inventory_lot_id = %s AND source_type = 'receipt' "
            "  AND unit_cost IS NOT NULL AND quantity_delta > 0",
            (int(movement.lot_id),),
        )
        cost_pool = (valuation or {}).get("cost_pool")
        received_qty = (valuation or {}).get("received_qty")
        if not cost_pool or not received_qty or _decimal(received_qty) <= 0:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                f"投喂核验无法确定归集金额：物料批次 {movement.lot_no} 没有带单价的入库记录，"
                "无法为出库估值为成本",
                data={"rule": "COST_AMOUNT_UNRESOLVED", "lot_no": movement.lot_no},
            )
        consumed = abs(_decimal(movement.quantity_delta))
        amount = (_decimal(cost_pool) / _decimal(received_qty) * consumed).quantize(
            Decimal("0.01")
        )
        if amount <= 0:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                f"投喂核验归集金额为 {amount}，必须大于 0",
                data={"rule": "COST_AMOUNT_UNRESOLVED"},
            )

        record_fact = _require_cost_record_fact()
        # Map the material's OWN category through cost's table rather than
        # hard-coding "feed". A feeding document may legitimately consume a seed or
        # health material (the registry only constrains the category to a feed-type
        # for the FEEDING case, and the material master carries six category codes),
        # so hard-coding would book the cost under the wrong category whenever it is
        # not actually feed. Falls back to `feed` only for a category cost does not
        # know, which is the conservative default for a feeding cost.
        material_category = str(material["category"]) if material.get("category") else ""
        category_code = _cost_category(material_category)
        cost_fact = record_fact(
            tx,
            organization_id=int(row["organization_id"]),
            farm_id=int(row["farm_id"]),
            area_id=int(row["area_id"]),
            category_code=category_code,
            amount=amount,
            # Pass a real `date` when the driver hands back a datetime (the documented
            # primary form); fall back to the ISO prefix for any driver that returns a
            # string, so this stays correct either way.
            occurred_on=_as_date(row["happened_at"]),
            source_ref=_ledger_source_ref("feeding", str(row["code"])),
            target_type="batch",
            target_id=int(row["batch_id"]),
            actor_id=ctx.actor.user_id,
            note=f"投喂核验自动归集（{row['code']}）",
        )

        verified = self.load_feeding(tx, scope=scope, record_id=int(feeding_id))
        assert verified is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    verified, scope, ctx.actor.permissions, resource="feeding"
                ),
                "_invariant_context": {
                    "organization_id": row["organization_id"],
                    # ONE entry carrying the union of the group keys used by BOTH
                    # ledger invariants declared on this capability:
                    #   inventory_ledger     -> (warehouse_id, material_id, inventory_lot_id)
                    #   batch_stock_records  -> (batch_id, pond_id)
                    # Each invariant reads only the columns it declares, so a single
                    # row locates both groups. The union is REQUIRED, not stylistic:
                    # a missing group column is an explicit INTERNAL_ERROR, not a skip.
                    "_ledger_lines": [
                        {
                            "warehouse_id": int(movement.warehouse_id),
                            "material_id": int(movement.material_id),
                            "inventory_lot_id": int(movement.lot_id),
                            "batch_id": int(row["batch_id"]),
                            "pond_id": int(row["pond_id"]),
                        }
                    ],
                    # `PeriodOpen(date_field="happened_at")` reads this: an `action`
                    # request body carries no date, so the service must supply it.
                    # `organization_id`（上面那行）同时是 `PeriodOpen` 的租户键 ——
                    # 会计期间每个企业各有一份，缺它内核会命中邻家的期间行。
                    "happened_at": row["happened_at"],
                },
            },
            resource_id=int(feeding_id),
            # The cost sentence comes from cost's own `CostFact.describe()` rather
            # than being hand-rendered here: the amount/period/category are COST's
            # facts, and a second formatter would drift the moment cost changes how
            # it phrases them (its docstring is explicit that the numbers come from
            # the row it actually wrote).
            message=(
                f"投喂「{verified['name']}」已核验：{lot_basis}"
                f"（结存 {movement.balance_after}）；{cost_fact.describe()}"
            ),
        )


class HarvestWriteService(HarvestService):
    """Harvest write path: create + verify.

    `harvest.verify` is the ONLY capability that reduces pond stock. The
    immutability contract that the sales domain depends on is documented in
    `service.py`.
    """

    def create_harvest(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("出塘单号", required=True, max_length=64),
        name: f_str("出塘事项", required=True, max_length=100),
        pond_id: f_ref("塘口", "pond", required=True),
        batch_id: f_ref("批次", "batch", required=True),
        quantity: f_num("出塘数量", minimum=0, maximum=MAX_PRODUCTION_QUANTITY),
        happened_at: f_str("出塘时间", required=True),
        weight_kg: f_num("重量（kg）", minimum=0, maximum=MAX_PRODUCTION_QUANTITY),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        """Register a harvest as `draft`.

        registry Sec 2.6 requires the pond to be `verified` and the batch to belong
        to the chosen pond. The `quantity <= current pond stock` rule is NOT checked
        here: it is invariant #2, enforced against the post-write ledger balance at
        `harvest.verify` time, so the check and the write cannot drift apart.
        """
        ctx.require("harvest.create")

        pond = _md()[0](tx, pond_id=int(pond_id))
        if pond is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "所属塘口不存在", data={"field": "pond_id"}
            )
        if not scope.allows_row(pond):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED, "所属塘口不在当前账号的数据范围内"
            )
        if str(pond["status"]) != "verified":
            raise DomainError(
                ErrorCode.CONFLICT,
                "塘口尚未核验，不能登记出塘",
                data={"field": "pond_id", "status": str(pond["status"])},
            )

        batch = tx.query_one(
            "SELECT id, pond_id, batch_status, organization_id, farm_id, area_id "
            "FROM production_batches WHERE id = %s",
            (int(batch_id),),
        )
        if batch is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "批次不存在", data={"field": "batch_id"}
            )
        if int(batch["pond_id"]) != int(pond_id):
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "所选批次不属于该塘口",
                data={"field": "batch_id"},
            )
        if not scope.allows_row(batch):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED, "所选批次不在当前账号的数据范围内"
            )

        _now_is_before(happened_at, "happened_at", "出塘时间")

        try:
            tx.execute(
                "INSERT INTO harvests "
                "(organization_id, farm_id, area_id, code, name, pond_id, batch_id, "
                " quantity, weight_kg, happened_at, note, status, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s)",
                (
                    pond["organization_id"],
                    pond["farm_id"],
                    pond["area_id"],
                    code,
                    name,
                    int(pond_id),
                    int(batch_id),
                    _decimal(quantity or 0),
                    None if weight_kg is None else _decimal(weight_kg),
                    happened_at,
                    note,
                    ctx.actor.user_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - translated below
            raise _translate_conflict(exc, code=code, what="出塘单号") from exc

        harvest_id = tx.last_insert_id()
        row = self.load_harvest(tx, scope=scope, record_id=harvest_id)
        assert row is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(row, scope, ctx.actor.permissions, resource="harvest"),
                "_invariant_context": {
                    "organization_id": pond["organization_id"],
                    "farm_id": pond["farm_id"],
                    "area_id": pond["area_id"],
                },
            },
            resource_id=harvest_id,
            message=f"已登记出塘「{name}」（编号 {code}），待核验",
        )

    def verify_harvest(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        harvest_id: f_int("出塘记录 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """draft -> verified, appending a NEGATIVE pond-stock ledger row.

        Ordering is deliberate:

          1. status transition to `verified`
          2. append the negative ledger row
          3. invariants run afterwards (post-write view) -- `NoNegativeStock`
             confirms the resulting balance is not negative, and `ZeroBalance` is
             what later lets `batch.close` pass

        The `quantity <= current stock` rule is therefore enforced by the kernel
        against the real post-write balance, with `FOR UPDATE` (inherited from the
        old `production_store.py:273-280`), rather than by a pre-read here that could
        race a concurrent harvest.

        `quantity` and `weight_kg` are never modified after this point: the row
        becomes an immutable fact for the sales domain (see `service.py`).
        """
        ctx.require("harvest.verify")
        row = self._scope_row(tx, scope, int(harvest_id))
        HARVEST_WORKFLOW.require_action(str(row["status"]), RowAction.VERIFY)

        affected = tx.execute(
            "UPDATE harvests SET status='verified', verified_by=%s, verified_at=NOW(), "
            "row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s AND status='draft'",
            (ctx.actor.user_id, ctx.actor.user_id, int(harvest_id), int(expected_version)),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该出塘记录状态已变化或已被他人修改，请刷新后重试",
            )

        # NEGATIVE row: the system's only pond-stock reduction.
        tx.execute(
            "INSERT INTO batch_stock_records "
            "(organization_id, farm_id, area_id, batch_id, pond_id, movement_type, "
            " quantity_delta, weight_delta_kg, happened_at, source_type, source_id, "
            " note, created_by) "
            "VALUES (%s,%s,%s,%s,%s,'harvest',%s,%s,%s,'harvest',%s,%s,%s)",
            (
                row["organization_id"],
                row["farm_id"],
                row["area_id"],
                int(row["batch_id"]),
                int(row["pond_id"]),
                -_decimal(row["quantity"]),
                -_decimal(row.get("weight_kg") or 0),
                row["happened_at"],
                int(harvest_id),
                f"出塘核验减少存塘（{row['code']}）",
                ctx.actor.user_id,
            ),
        )

        verified = self.load_harvest(tx, scope=scope, record_id=int(harvest_id))
        assert verified is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    verified, scope, ctx.actor.permissions, resource="harvest"
                ),
                "_invariant_context": {
                    "organization_id": row["organization_id"],
                    "_ledger_lines": [
                        {"batch_id": int(row["batch_id"]), "pond_id": int(row["pond_id"])}
                    ],
                    "quantity": str(row["quantity"]),
                    "weight_kg": (
                        None if row.get("weight_kg") is None else str(row["weight_kg"])
                    ),
                    "status": "verified",
                },
            },
            resource_id=int(harvest_id),
            message=f"出塘「{verified['name']}」已核验，已减少塘内存塘",
        )


# ---------------------------------------------------------------------------
# Reload bindings (`__fpa_load_by_id__`)
#
# `CapabilityRunner._reload_after` requires every write capability that returns a
# `resource_id` to expose a reload function, otherwise it raises INTERNAL_ERROR.
# That failure mode is deliberate: the alternative (returning a placeholder id)
# looks like success while the data's provenance is unverifiable.
#
# Module-level assignment, not a decorator: the registry holds the UNBOUND function
# (`BatchWriteService.create_batch`), so `__fpa_load_by_id__` must be attached to
# that same function object. A decorator would produce a different object than the
# one the capability declares, and `runner._resolve()` only reads the declared one.
#
# master_data shipped WITHOUT these bindings and its write capabilities returned 500
# on every real HTTP path while fixture-based self-checks stayed green. The e2e for
# this domain asserts the reload resolves without any fixture patching, precisely so
# that defect cannot be copied into a new domain.
# ---------------------------------------------------------------------------

BatchWriteService.create_batch.__fpa_load_by_id__ = BatchService.load_batch  # type: ignore[attr-defined]
BatchWriteService.update_batch.__fpa_load_by_id__ = BatchService.load_batch  # type: ignore[attr-defined]
BatchWriteService.submit_batch.__fpa_load_by_id__ = BatchService.load_batch  # type: ignore[attr-defined]
BatchWriteService.verify_batch.__fpa_load_by_id__ = BatchService.load_batch  # type: ignore[attr-defined]
BatchWriteService.close_batch.__fpa_load_by_id__ = BatchService.load_batch  # type: ignore[attr-defined]

FeedingWriteService.create_feeding.__fpa_load_by_id__ = FeedingService.load_feeding  # type: ignore[attr-defined]
FeedingWriteService.verify_feeding.__fpa_load_by_id__ = FeedingService.load_feeding  # type: ignore[attr-defined]

HarvestWriteService.create_harvest.__fpa_load_by_id__ = HarvestService.load_harvest  # type: ignore[attr-defined]
HarvestWriteService.verify_harvest.__fpa_load_by_id__ = HarvestService.load_harvest  # type: ignore[attr-defined]


__all__ = [
    "MAX_PRODUCTION_QUANTITY",
    "BatchWriteService",
    "FeedingWriteService",
    "HarvestWriteService",
]
