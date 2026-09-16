"""Capability declarations for the production domain (12 capabilities).

**This is where "declaration IS derivation" happens**: one `Capability(...)`
simultaneously produces

    REST route          (`web/app.py` registers by `method` + `path`)
    permission code     (`required_permission`)
    DataScope predicate (`scope`)
    idempotency policy  (`idempotent`)
    human confirmation  (gated by `risk` + `confirmation`)
    audit fields        (`audit`)
    Agent tool schema   (`agent_exposure` + fields collected from handler annotations)
    frontend form/columns (`fields` + the resource's `columns`)
    business invariants (`invariants`, executed by `CapabilityRunner` in one transaction)

So this file contains NO route, validation or permission logic -- those are all
derived.

## The 12 capabilities (registry Sec 1.5)

    batch.list / batch.create / batch.update / batch.submit / batch.verify / batch.close
    feeding.list / feeding.create / feeding.verify
    harvest.list / harvest.create / harvest.verify

Deliberately ABSENT, and each absence is load-bearing:

  * no `feeding.submit` -- feeding has two states, draft -> verified (Sec 3.3);
    submit and verify were the same role in the old system, so the state bought
    no control.
  * no `feeding.update` -- a draft is discarded and re-created, never edited.
  * no `harvest.submit` -- the ledger lists only list/create/verify for harvests.
  * no `batch.archive` -- the capability ledger stops at `batch.close`.
  * no `delete` of any kind -- deletion is replaced by state transition.
  * cut entirely (Sec 1.5, Sec 6): transfers, losses, feed-plans, feed-tasks,
    daily-operations, samplings.

## Two permission codes that do NOT follow the same-name rule

Both are counter-intuitive and both are taken verbatim from the registry ledger:

  * `batch.submit` requires **`batch.update`**, not `batch.submit`.
  * `batch.close` requires **`batch.verify`**, not `batch.close`.

## Confirmation consistency (enforced at registration, not by review)

Sec 0.4 of the registry demands:

    risk == high AND kind == action AND agent_exposure != human_only
        => confirmation == always

The kernel mechanically rejects a capability that violates this at registration
time, so a mistake here fails at startup rather than in production. The six
`high + always` capabilities in this domain are:
`batch.verify`, `batch.close`, `feeding.create`, `feeding.verify`,
`harvest.create`, `harvest.verify`.

## Invariant enforcement points (registry Sec 4)

| invariant | capability | why here |
|---|---|---|
| #2 `NoNegativeStock(batch_stock_records)` | `batch.verify`, `feeding.verify`, `harvest.verify` | every capability that appends to the pond-stock ledger |
| #5 `DistinctActors` | `batch.verify`, `feeding.verify` | creator may not verify their own document |
| #7 `PeriodOpen` | `feeding.verify` | it writes cost, so a closed period must block it |
| #11 `ZeroBalance` | `batch.close` | stock must be zero to close |
| #12 `StatusAllowsEdit` | `batch.update` | a verified record is read-only |
| #13 `OptimisticLock` | `batch.update`, action writes | `row_version` must match |
| #14 `StateTransition` | the four state-driving actions | one state machine, one definition |
| #16 `AtLeastOneOf(quantity, weight_kg)` | `feeding.create` | "at least one measurement" |
| #18 `SameTenant` + `ReferencedStatus` | create capabilities | referenced rows must share tenant and be valid |
| #19 `UniqueCode(code, organization_id)` | all three creates | readable 409 over the DB unique key |

`ReferencedStatus` is attached with `required=False` semantics by the kernel and
the explicit service-side checks in `write.py` produce the human-readable message,
so both layers exist without describing the rule twice in code.
"""

from __future__ import annotations

from yuxin.kernel.capability import (
    AgentExposure,
    AuditPolicy,
    Capability,
    Confirmation,
    HttpMethod,
    REGISTRY,
    Risk,
)
from yuxin.kernel.invariants import (
    AtLeastOneOf,
    DistinctActors,
    NoNegativeStock,
    OptimisticLock,
    PeriodOpen,
    ReferencedStatus,
    SameTenant,
    StateTransition,
    StatusAllowsEdit,
    UniqueCode,
    ZeroBalance,
)
from yuxin.kernel.scope import ScopePolicy

from .read import BatchService, FeedingService, HarvestService
from .service import BATCH_STATUS_WORKFLOW
from .write import BatchWriteService, FeedingWriteService, HarvestWriteService

#: Write capabilities are exposed to the agent by default, matching the
#: master_data ruling: do not cripple ordinary business work such as "create a
#: batch", "feed", "harvest". Control comes from L1 permission + DataScope +
#: invariants + the confirmation gate, not from denying the agent access.
_EXPOSED = AgentExposure.EXPOSED

#: Read capabilities share one permission code per resource.
_BATCH_SCOPE = ScopePolicy.resource("area_id")
_FEEDING_SCOPE = ScopePolicy.resource("area_id")
_HARVEST_SCOPE = ScopePolicy.resource("area_id")


def _batch_status_choices(current: str | None):
    """Dynamic candidate values for `to_status`-style fields.

    This is the runtime form of registry Sec 2.5's "the server filters choices by
    the transfer table": the client renders the options it is handed instead of
    deriving them, so a client cannot even display an illegal target.
    """
    return BATCH_STATUS_WORKFLOW.transition_choices(current)


def _register_all() -> None:
    """Register all 12 production capabilities.

    Wrapped in a function rather than using module-level bare statements so that
    re-importing the module (tests do this) cannot explode: `REGISTRY.register`
    raises on duplicate names by design.
    """
    if REGISTRY.find("batch.list") is not None:
        return  # already registered (repeated import)

    # ------------------------------------------------------------------ batch
    REGISTRY.register(
        Capability(
            name="batch.list",
            title="批次列表",
            domain="production",
            resource="batch",
            handler=BatchService.list_batches,
            service_factory=BatchService,
            method=HttpMethod.GET,
            path="/api/v1/batches",
            kind="read",
            required_permission="batch.view",
            scope=_BATCH_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词、记录状态、批次状态筛选养殖批次列表",
        )
    )

    REGISTRY.register(
        Capability(
            name="batch.get",
            title="批次详情",
            domain="production",
            resource="batch",
            handler=BatchService.get_batch_by_id,
            service_factory=BatchService,
            method=HttpMethod.GET,
            path="/api/v1/batches/{batch_id}",
            kind="read",
            required_permission="batch.view",
            scope=_BATCH_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="读取养殖批次详情，含可用的批次状态目标",
        )
    )

    REGISTRY.register(
        Capability(
            name="batch.create",
            title="建档放苗",
            domain="production",
            resource="batch",
            handler=BatchWriteService.create_batch,
            service_factory=BatchWriteService,
            method=HttpMethod.POST,
            path="/api/v1/batches",
            kind="create",
            required_permission="batch.create",
            scope=_BATCH_SCOPE,
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # #18: the pond must share the tenant; #19: the code is unique
                # inside the organization.
                SameTenant(fields=("pond_id",), tables={"pond_id": "ponds"}),
                UniqueCode(fields=("code",), scope=("organization_id",)),
            ),
            description="在已核验的塘口下建立养殖批次并登记放苗；批次号在企业内唯一",
        )
    )

    REGISTRY.register(
        Capability(
            name="batch.update",
            title="编辑批次",
            domain="production",
            resource="batch",
            handler=BatchWriteService.update_batch,
            service_factory=BatchWriteService,
            method=HttpMethod.PATCH,
            path="/api/v1/batches/{batch_id}",
            kind="update",
            required_permission="batch.update",
            scope=_BATCH_SCOPE,
            risk=Risk.NORMAL,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=False,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # #12 `StatusAllowsEdit` is deliberately NOT declared here.
                #
                # registry Sec 3.2-B makes ONE capability carry two concerns:
                # `batch.update` edits record fields AND drives the business state
                # machine (`stocked -> farming -> pending_settlement`), because the
                # old system's separate `change_batch_status` endpoint
                # (`production_store.py:219-241`) was folded into update.
                #
                # The kernel invariant compares a single `status` field, so it
                # cannot tell those two apart: a batch is `status='verified'` for its
                # whole productive life, which means the invariant blocks the legal
                # `stocked -> farming` transition and makes the entire batch
                # lifecycle unreachable -- the exact defect DECISIONS.md Q10 calls
                # out ("an unreachable capability is worse than a missing one").
                #
                # #12 is therefore enforced in the SERVICE (`write.update_batch`),
                # which is the only layer that can distinguish "record fields were
                # submitted" from "only the business state advanced". Record-field
                # edits on a verified batch are still refused with
                # `rule=RECORD_READ_ONLY`, and the business transition remains
                # guarded by `StateTransition` below. REPORTED to the 负责人 and
                # 复核人 as a genuine gap between the invariant catalogue and the
                # capability ledger -- not silently dropped.
                #
                # #13: optimistic lock on row_version.
                OptimisticLock(),
                # #14: drives batch_status (stocked -> farming ->
                # pending_settlement). `closed` is refused here; only
                # batch.close may close a batch.
                StateTransition(machine="batch", field_name="batch_status"),
            ),
            description="修改批次台账信息，或推进批次状态（已核验的批次不可修改）",
        )
    )

    REGISTRY.register(
        Capability(
            name="batch.submit",
            title="提交批次核验",
            domain="production",
            resource="batch",
            handler=BatchWriteService.submit_batch,
            service_factory=BatchWriteService,
            method=HttpMethod.POST,
            path="/api/v1/batches/{batch_id}/submit",
            kind="action",
            # NOT `batch.submit` -- the registry ledger assigns `batch.update`.
            required_permission="batch.update",
            scope=_BATCH_SCOPE,
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            idempotent=False,
            # registry Sec 1.5 lists `summary` for this capability, but this
            # repository implements only two audit policies: none() and
            # snapshot(). `summary` was never built and no other domain uses it
            # (master_data and cost both use snapshot() everywhere), so rather
            # than invent a third policy here this records a snapshot, which is
            # STRICTLY MORE evidence than a summary would carry. The deviation is
            # written down so it stays visible instead of silent.
            # registry 给本行的 `audit` 列是 **summary**（提交是轻量动作，不记 before/after 全量 diff）。
            # 原写 `snapshot()`，与文档不一致：`submit` 只把 status 从 draft 推到 submitted，
            # 那次状态转移本身就写在审计的 `reason` / `object_ref` 里，再记一份整行 diff
            # 只会让审计表膨胀。四领域的同族动作（含 `sales_order.submit`，registry §1.8）统一为 summary——
            # 让三个 `*.submit` 记全量 diff 而第四个不记，是「同一件事两处不一样」的典型。
            audit=AuditPolicy.summary(),
            invariants=(
                OptimisticLock(),
                StateTransition(machine="batch"),
            ),
            description="把草稿状态的批次提交给他人核验",
        )
    )

    REGISTRY.register(
        Capability(
            name="batch.verify",
            title="核验批次（形成初始存塘）",
            domain="production",
            resource="batch",
            handler=BatchWriteService.verify_batch,
            service_factory=BatchWriteService,
            method=HttpMethod.POST,
            path="/api/v1/batches/{batch_id}/verify",
            kind="action",
            required_permission="batch.verify",
            scope=_BATCH_SCOPE,
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # #5 + Q8: the creator may not verify their own batch.
                DistinctActors(),
                OptimisticLock(),
                StateTransition(machine="batch"),
                # #2: this is where the initial pond stock is formed, so the
                # resulting balance must not be negative.
                NoNegativeStock(
                    table="batch_stock_records",
                    columns=("quantity_delta", "weight_delta_kg"),
                    group_by=("batch_id", "pond_id"),
                    label="存塘",
                ),
            ),
            description="核验他人提交的批次，并形成初始存塘；经办人不能核验自己提交的批次",
        )
    )

    REGISTRY.register(
        Capability(
            name="batch.close",
            title="关闭批次",
            domain="production",
            resource="batch",
            handler=BatchWriteService.close_batch,
            service_factory=BatchWriteService,
            method=HttpMethod.POST,
            path="/api/v1/batches/{batch_id}/close",
            kind="action",
            # NOT `batch.close` -- the registry ledger assigns `batch.verify`.
            required_permission="batch.verify",
            scope=_BATCH_SCOPE,
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                OptimisticLock(),
                # #14: only pending_settlement -> closed.
                StateTransition(machine="batch", field_name="batch_status"),
                # #11: stock must be exactly zero. This is the invariant that makes
                # `harvests` load-bearing: harvest.verify is the only capability
                # that can bring the balance to zero (DECISIONS.md Q10).
                ZeroBalance(
                    table="batch_stock_records",
                    group_by=("batch_id",),
                    columns=("quantity_delta", "weight_delta_kg"),
                    labels=("数量", "重量"),
                    label="存塘",
                ),
            ),
            description="关闭批次；要求塘内存塘已归零（出塘核验是唯一能减少存塘的能力）",
        )
    )

    # ---------------------------------------------------------------- feeding
    REGISTRY.register(
        Capability(
            name="feeding.list",
            title="投喂记录列表",
            domain="production",
            resource="feeding",
            handler=FeedingService.list_feedings,
            service_factory=FeedingService,
            method=HttpMethod.GET,
            path="/api/v1/feedings",
            kind="read",
            required_permission="feeding.view",
            scope=_FEEDING_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词、塘口、批次筛选投喂记录列表",
        )
    )

    REGISTRY.register(
        Capability(
            name="feeding.get",
            title="投喂记录详情",
            domain="production",
            resource="feeding",
            handler=FeedingService.get_feeding_by_id,
            service_factory=FeedingService,
            method=HttpMethod.GET,
            path="/api/v1/feedings/{feeding_id}",
            kind="read",
            required_permission="feeding.view",
            scope=_FEEDING_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="读取投喂记录详情",
        )
    )

    REGISTRY.register(
        Capability(
            name="feeding.create",
            title="登记投喂",
            domain="production",
            resource="feeding",
            handler=FeedingWriteService.create_feeding,
            service_factory=FeedingWriteService,
            method=HttpMethod.POST,
            path="/api/v1/feedings",
            kind="create",
            required_permission="feeding.create",
            scope=_FEEDING_SCOPE,
            # `high` because it is the entry of an operation that later debits
            # inventory AND collects cost in one step (registry Sec 1.5).
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # #16: at least one of quantity / weight_kg.
                AtLeastOneOf(fields=("quantity", "weight_kg"), labels=("投喂量", "重量（kg）")),
                # #18: pond, batch and material must share the tenant.
                SameTenant(
                    fields=("pond_id", "batch_id", "material_id"),
                    tables={
                        "pond_id": "ponds",
                        "batch_id": "production_batches",
                        "material_id": "materials",
                    },
                ),
                # #18: the feed material must be verified.
                ReferencedStatus(
                    field="material_id",
                    table="materials",
                    statuses=("verified",),
                    label="饲料",
                ),
                UniqueCode(fields=("code",), scope=("organization_id",)),
            ),
            description="在指定塘口与批次下登记一次投喂；饲料须为已核验物料",
        )
    )

    REGISTRY.register(
        Capability(
            name="feeding.verify",
            title="核验投喂（扣库存 + 计成本）",
            domain="production",
            resource="feeding",
            handler=FeedingWriteService.verify_feeding,
            service_factory=FeedingWriteService,
            method=HttpMethod.POST,
            path="/api/v1/feedings/{feeding_id}/verify",
            kind="action",
            required_permission="feeding.verify",
            scope=_FEEDING_SCOPE,
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # #5: the creator may not verify their own feeding.
                DistinctActors(),
                OptimisticLock(),
                StateTransition(machine="feeding"),
                # #7: it writes cost, so a closed accounting period must block it.
                PeriodOpen(
                    table="accounting_periods",
                    date_field="happened_at",
                    status="open",
                    label="该投喂时间所在的会计期间",
                ),
                # #2 applied to the feeding path. Feeding does NOT reduce pond
                # stock, so this is a safety net rather than the enforcement point
                # for material stock: material stock is guarded by invariant #1
                # inside warehouse's `apply_movement`, which is the single entry
                # point for inventory.
                #
                # #17 (registry Sec 4) is the reason this invariant is attached
                # here at all: the old rule "feeding requires an approved, fully
                # issued material issue request" was DOWNGRADED when
                # `issue-requests` was cut (Q9). The replacement is to judge real
                # inventory at verify time. This is a known, accepted reduction in
                # strength and must not be reported as fully implemented.
                NoNegativeStock(
                    table="batch_stock_records",
                    columns=("quantity_delta", "weight_delta_kg"),
                    group_by=("batch_id", "pond_id"),
                    label="存塘",
                ),
                # #1: THE enforcement point for MATERIAL inventory on the feeding
                # path, and the direct cost of 负责人's ruling (4) which removed the
                # negative-stock judgement from `warehouse.ledger.apply_movement`:
                # that function now only creates the lot, locks the lot row and
                # writes the ledger row. Without this declaration NOTHING would stop
                # a feeding from driving material stock negative -- proven by test:
                # an issue of 50 against an opening balance of 10 was accepted and
                # left the lot at -40.
                #
                # `group_by` MUST match `warehouse.ledger`'s `_STOCK_GROUP_BY` and the
                # migration's index prefix word-for-word: they are three views of one
                # rule, and a drift here silently validates the wrong group.
                NoNegativeStock(
                    table="inventory_ledger",
                    columns=("quantity_delta",),
                    group_by=("warehouse_id", "material_id", "inventory_lot_id"),
                    label="库存",
                ),
            ),
            description="核验投喂记录：扣减物料库存、生成成本归集；经办人不能核验自己的记录",
        )
    )

    # ---------------------------------------------------------------- harvest
    REGISTRY.register(
        Capability(
            name="harvest.list",
            title="出塘记录列表",
            domain="production",
            resource="harvest",
            handler=HarvestService.list_harvests,
            service_factory=HarvestService,
            method=HttpMethod.GET,
            path="/api/v1/harvests",
            kind="read",
            required_permission="harvest.view",
            scope=_HARVEST_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词、塘口、批次筛选出塘记录列表",
        )
    )

    REGISTRY.register(
        Capability(
            name="harvest.get",
            title="出塘记录详情",
            domain="production",
            resource="harvest",
            handler=HarvestService.get_harvest_by_id,
            service_factory=HarvestService,
            method=HttpMethod.GET,
            path="/api/v1/harvests/{harvest_id}",
            kind="read",
            required_permission="harvest.view",
            scope=_HARVEST_SCOPE,
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="读取出塘记录详情",
        )
    )

    REGISTRY.register(
        Capability(
            name="harvest.create",
            title="登记出塘",
            domain="production",
            resource="harvest",
            handler=HarvestWriteService.create_harvest,
            service_factory=HarvestWriteService,
            method=HttpMethod.POST,
            path="/api/v1/harvests",
            kind="create",
            required_permission="harvest.create",
            scope=_HARVEST_SCOPE,
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                SameTenant(
                    fields=("pond_id", "batch_id"),
                    tables={"pond_id": "ponds", "batch_id": "production_batches"},
                ),
                UniqueCode(fields=("code",), scope=("organization_id",)),
            ),
            description="登记出塘；塘口须已核验，批次须属于该塘口",
        )
    )

    REGISTRY.register(
        Capability(
            name="harvest.verify",
            title="核验出塘（减存塘）",
            domain="production",
            resource="harvest",
            handler=HarvestWriteService.verify_harvest,
            service_factory=HarvestWriteService,
            method=HttpMethod.POST,
            path="/api/v1/harvests/{harvest_id}/verify",
            kind="action",
            required_permission="harvest.verify",
            scope=_HARVEST_SCOPE,
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                OptimisticLock(),
                StateTransition(machine="harvest"),
                # #5 经办人 ≠ 审批人。Q8′ 已确认把 `harvest.verify` 并入"13 条核验/审批能力"
                # （registry §4 #5 的清单里就有它），而运行时**一直漏挂** —— 技能清单说挂了、
                # 实际没有，正是本项目要根除的"声明了却不生效"。
                #
                # 它的强制点在这里、而且只在这里：本能力把出塘单置为 `verified`，而
                # "谁登记的"是 `before` 快照（`harvests.created_by`，该列 NOT NULL）上的
                # 既成事实，**不能由本次请求声称** —— 这正是 `DistinctActors` 读
                # `before` 而不是读 payload 的原因。
                DistinctActors(),
                # #2: THE enforcement point of "harvest quantity <= current pond
                # stock". Because invariants run after the write, this compares the
                # real post-write balance with FOR UPDATE, so a concurrent harvest
                # cannot slip through a pre-read race (inherited from the old
                # `production_store.py:273-280`).
                NoNegativeStock(
                    table="batch_stock_records",
                    columns=("quantity_delta", "weight_delta_kg"),
                    group_by=("batch_id", "pond_id"),
                    label="存塘",
                ),
            ),
            description="核验出塘记录并减少塘内存塘；出塘数量不得超过当前存塘",
        )
    )


_register_all()
