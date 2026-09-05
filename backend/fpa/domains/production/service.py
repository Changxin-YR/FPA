"""production domain: batches / feeding / harvests.

This is the authoritative state-machine and resource declaration module for the
production domain. It mirrors the master_data template (`domains/master_data/
service.py`): the workflow is declared ONCE here and everything else is derived
from it (status_dict, row_actions, legal transitions, and the dynamic candidate
list for `to_status` fields).

## 1. Why this module is the one place the three state machines live

registry Sec 3.0 rule 1: a state machine has exactly ONE definition. The old
system defined the same machine in three independent places -- a DB ENUM
(`009_production.sql:18-19`), a generic policy table
(`common/governance/lifecycle.py:48-61`), and a domain constant
(`production_service.py:15`) -- and the three could disagree. Everything the
runtime needs is derived from the `Workflow` objects below.

## 2. Two state machines per batch, exactly like ponds has two

A batch carries BOTH:

    status         record lifecycle   draft / submitted / verified / archived
    batch_status   business state     stocked / farming / pending_settlement / closed

`Workflow.allowed_actions()` is only meaningful for the record-lifecycle subset,
because `available_transitions` operates on `batch_status`. That is why the
`batch_status` field is declared with an explicit `dynamic_choices` callback in
`capabilities.py` rather than relying on the caller to pick the right subset.

## 3. The batch closure chain, and why harvests must exist

    verify -> stocked -> farming -> pending_settlement -> closed

The last link requires stock to be zero (invariant #11 ZeroBalance). Feeding does
NOT reduce pond stock -- it consumes material inventory -- so `harvest.verify` is
the ONLY capability in the whole system that reduces pond stock. This is the
decisive argument recorded in DECISIONS.md Q10: with harvests cut, `batch.close`
became an unreachable capability, and an unreachable capability is worse than a
missing one because it still looks present on the checklist.

## 4. Two states, not four, for feeding and harvests

registry Sec 3.3 gives feeding `draft / verified` only, and offers no
`feeding.submit`, so the record lifecycle's `submitted` state would be
unreachable. Harvests are the same shape: the capability ledger (Sec 1.5) lists
exactly `harvest.list / create / verify` and NO `harvest.submit`, so a
`submitted` state there would also be unreachable.

The master_data template has the same characteristic -- `RECORD_LIFECYCLE`
declares `archived` while batch-like documents never archive -- but for the new
domains we prefer the minimal lifecycle whose every state is reachable. This is
the project's own principle applied to itself: do not ship a state the system
cannot actually enter.

## 5. IMMUTABILITY CONTRACT for harvests (consumed cross-domain by sales)

Once a harvest row is `verified`, its `quantity` and `weight_kg` are NEVER
modified. `harvest.verify` only ever writes `status` and `verified_by`.

The sales domain binds deliveries to a harvest document and enforces invariant
#6 `HarvestQuantityMatch` with `tolerance="exact"` (registry Sec 4 #6), comparing
the delivery quantity against `harvests.quantity` / `harvests.weight_kg`
(`weight_kg * 2` when the sales unit is `jin`, `quantity` when it is `tail`).
It must be able to treat a verified harvest row as an IMMUTABLE FACT.

Corrections are therefore made by voiding and re-creating a harvest, never by
editing a verified one. This is a cross-domain promise, so it is recorded here
in code rather than only in conversation.
"""

from __future__ import annotations

from fpa.kernel.fields import RefTarget
from fpa.kernel.workflow import (
    RESOURCES,
    FilterKind,
    FilterSpec,
    Resource,
    RowAction,
    State,
    Tone,
    Transition,
    Workflow,
)

# ============================================================================
# Record lifecycle (shared shape with master_data's RECORD_LIFECYCLE)
# ============================================================================

RECORD_LIFECYCLE = Workflow(
    resource="record_lifecycle",
    initial="draft",
    states=(
        State("draft", "草稿", Tone.NEUTRAL, (RowAction.VIEW, RowAction.EDIT, RowAction.SUBMIT)),
        State("submitted", "待核验", Tone.WARNING, (RowAction.VIEW, RowAction.VERIFY)),
        State("verified", "已核验", Tone.SUCCESS, (RowAction.VIEW,)),
    ),
    transitions=(
        Transition("draft", "submitted", "*.submit"),
        Transition("submitted", "verified", "*.verify"),
        Transition("submitted", "draft", "*.update"),
    ),
)

#: batch_status (registry Sec 3.2-B). Transfer table inherited VERBATIM from the
#: old system (`production_service.py:15`):
#:     {"stocked": {"farming"}, "farming": {"pending_settlement"},
#:      "pending_settlement": {"closed"}}
BATCH_STATUS_WORKFLOW = Workflow(
    resource="batch_status",
    initial="stocked",
    states=(
        State("stocked", "已放苗", Tone.INFO, (RowAction.VIEW, RowAction.EDIT)),
        State("farming", "养殖中", Tone.SUCCESS, (RowAction.VIEW, RowAction.EDIT)),
        State("pending_settlement", "待结算", Tone.WARNING, (RowAction.VIEW, RowAction.EDIT)),
        State("closed", "已关闭", Tone.NEUTRAL, (RowAction.VIEW,), terminal=True),
    ),
    transitions=(
        Transition("stocked", "farming", "batch.update"),
        Transition("farming", "pending_settlement", "batch.update"),
        Transition("pending_settlement", "closed", "batch.close"),
    ),
)

#: The batch's complete workflow = record lifecycle + business status.
#:
#: One `Workflow` carries both because `Resource` accepts only one, and
#: `status_dict` must supply the wording and tone for both state sets. The cost
#: of merging is that the two state sets are mutually unrelated inside one
#: Workflow, so `available_transitions` is only meaningful for the
#: `batch_status` subset -- pinned down by the field-level `dynamic_choices`
#: callback in `capabilities.py`, not left to caller discipline.
BATCH_WORKFLOW = Workflow(
    resource="batch",
    initial="draft",
    states=RECORD_LIFECYCLE.states + BATCH_STATUS_WORKFLOW.states,
    transitions=(
        # `draft -> stocked` is the ENTRY edge of the business state machine, declared
        # explicitly rather than left implicit. `batch.verify` performs it: it moves
        # the record lifecycle to `verified` AND sets `batch_status = 'stocked'` while
        # writing the initial stock ledger row.
        #
        # It lives on the MERGED workflow because `draft` belongs to the record
        # lifecycle while `stocked` belongs to the business state machine -- the two
        # halves are separate Workflows by construction.
        #
        # Why declare an edge no `require_transition` call validates: without it,
        # `stocked` carries row actions (`view`, `edit`) and starts a transition, yet
        # has no incoming edge, so it reads as unreachable in the `transitions`
        # metadata the frontend uses to render available moves. That is the same shape
        # as the Q10 defect -- "a capability that looks present on the checklist but
        # cannot be executed" -- so it is made explicit instead of masked.
        # `batch.verify` never passes `batch_status` in its request, so the kernel's
        # `StateTransition` does not validate this edge; it exists to describe the
        # machine truthfully.
        Transition("draft", "stocked", "batch.verify"),
    )
    + RECORD_LIFECYCLE.transitions
    + BATCH_STATUS_WORKFLOW.transitions,
)

#: feeding: TWO states only (registry Sec 3.3).
#:
#: Deliberately not the four-state master-data shape:
#:   * no `submitted` -- submit and verify were the SAME role in the old system
#:     (`production_service.py:237-247` submit and `:249+` verify both used
#:     `production.verify`), so the intermediate state bought no control and cost
#:     a round trip.
#:   * no `archived` -- a verified feeding is a ledger fact, it is not archived.
#:   * no `corrected` -- the correction-document mechanism was cut (Sec 6);
#:     corrections are done by void-and-reopen.
FEEDING_WORKFLOW = Workflow(
    resource="feeding",
    initial="draft",
    states=(
        # NO `edit` action: the capability ledger (registry Sec 1.5) defines
        # feeding.list / create / verify and deliberately no `feeding.update`
        # ("a draft is discarded and re-created, never edited"), so an `edit` action
        # here would render a button no capability can serve -- the frontend looks up
        # `${resource}.${action}`, misses, and falls back to the resource's first
        # action capability. `registry_reconcile` reports exactly this as "the
        # frontend will render an unclickable button".
        State("draft", "草稿", Tone.NEUTRAL, (RowAction.VIEW, RowAction.VERIFY)),
        State("verified", "已核验", Tone.SUCCESS, (RowAction.VIEW,), terminal=True),
    ),
    transitions=(
        # draft -> verified. There is no feeding.update capability on purpose:
        # a draft is discarded and re-created rather than edited.
        Transition("draft", "verified", "feeding.verify"),
    ),
)

#: harvests: TWO states only.
#:
#: Sec 1.5 lists exactly `harvest.list / create / verify` -- there is no
#: `harvest.submit` -- so a `submitted` state would be unreachable. Sales binds
#: `delivery.harvest_document_id` to a harvest that is `verified` (Sec 2.9),
#: which this lifecycle delivers directly.
HARVEST_WORKFLOW = Workflow(
    resource="harvest",
    initial="draft",
    states=(
        # NO `edit` action: the ledger (registry Sec 1.5) defines
        # harvest.list / create / verify with no `harvest.update`, so an `edit`
        # action would render a button with no capability behind it. Corrupt or
        # wrong harvests are voided and re-created.
        State("draft", "草稿", Tone.NEUTRAL, (RowAction.VIEW, RowAction.VERIFY)),
        State("verified", "已核验", Tone.SUCCESS, (RowAction.VIEW,), terminal=True),
    ),
    transitions=(
        Transition("draft", "verified", "harvest.verify"),
    ),
)


def _label_of(workflow: Workflow, code: str) -> str:
    """Status code -> Chinese label, with exactly one source (the Workflow)."""
    if not code:
        return ""
    for state in workflow.states:
        if state.code == code:
            return state.label
    return code


def batch_status_label(code: str) -> str:
    return _label_of(BATCH_STATUS_WORKFLOW, code)


def record_status_label(code: str) -> str:
    return _label_of(RECORD_LIFECYCLE, code)


# ============================================================================
# Resource declarations
#
# These feed the frontend list pages and, more importantly, tell the kernel
# which physical TABLE each resource lives on. `Resource.table` is what
# `OptimisticLock` and `UniqueCode` resolve their queries against
# (docs/INVARIANT_TYPES.md Sec 2), so a wrong value here would make those
# invariants query a table that does not exist.
#
# The fallback when `table` is omitted is `<module>_<name>s`, which would give
# `batchs` / `feedings` -- wrong for the batch resource (its table is
# `production_batches`).
# The real names come from registry Sec 4 #19 (`uq_production_batches_org_code`).
# ============================================================================

RESOURCES.register(
    Resource(
        # The resource name is `batch`, NOT `production_batch`. Three names must agree
        # for the frontend to resolve a rendered action:
        #   resource name  ==  capability prefix  ==  route segment  ==  `batch`
        # The registry derives `batch.list/create/...` and `/api/v1/batches`, and a
        # capability must declare `resource == resource.name` or the frontend's lookup
        # `\`${resource}.${action}\`` misses and silently falls back to "the resource's
        # first action capability" (the bug that made an "approve" button POST to
        # `/cancel`). The physical TABLE is a separate fact and is declared below.
        name="batch",
        title="养殖批次",
        module="production",
        list_path="/api/v1/batches",
        detail_path="/api/v1/batches/{batch_id}",
        workflow=BATCH_WORKFLOW,
        table="production_batches",
        columns=(
            ("code", "批次编号"),
            ("name", "批次名称"),
            ("pond_name", "塘口"),
            ("species", "品种"),
            ("initial_quantity", "放苗数量"),
            ("batch_status", "批次状态码"),
            ("batch_status_label", "批次状态"),
            ("status", "记录状态码"),
            ("status_label", "记录状态"),
        ),
        # 筛选声明逐条对应 `ProductionQueryService.list_batches`
        # （`read.py::_list` 的 `params.get(...)`）：keyword / status 是通用参数，
        # 另加 `_scope_row` 之外的 `pond_id`、`batch_status` 两个额外条件。
        # 关键词列是 `b.code` / `b.name`（见该处的 `keyword_columns`）。
        search=True,
        filters=(
            # 判据：`selectable_states()` 才是**可选**集合（`closed` 之类不可达状态
            # 按它筛选只会得到空集），前端拿 `status_dict` 渲染时会跳过它们。
            FilterSpec("status", "记录状态", FilterKind.STATUS),
            # `batch_status` 是 `batch_status_label` 的原始码列；
            # `tone_key` 已指向它，因此前端渲染同一个状态胶囊。
            FilterSpec("batch_status", "批次状态", FilterKind.STATUS),
            FilterSpec(
                "pond_id",
                "塘口",
                FilterKind.REF,
                ref=RefTarget("pond", "name"),
            ),
        ),
    )
)

RESOURCES.register(
    Resource(
        name="feeding",
        title="投喂记录",
        module="production",
        list_path="/api/v1/feedings",
        detail_path="/api/v1/feedings/{feeding_id}",
        workflow=FEEDING_WORKFLOW,
        table="feedings",
        columns=(
            ("code", "投喂单号"),
            ("name", "投喂事项"),
            ("pond_name", "塘口"),
            ("batch_code", "批次"),
            ("material_name", "饲料"),
            ("quantity", "投喂量"),
            ("weight_kg", "重量（kg）"),
            ("happened_at", "投喂时间"),
            ("status", "状态码"),
            ("status_label", "状态"),
        ),
        # 对应 `list_feedings` 的 `params.get(...)`。
        # **没有 `happened_at` 区间**：处理器不接受日期参数，声明它等于给前端
        # 一个永远筛不出结果的控件。
        search=True,
        filters=(
            FilterSpec("status", "状态", FilterKind.STATUS),
            FilterSpec(
                "pond_id", "塘口", FilterKind.REF, ref=RefTarget("pond", "name")
            ),
            FilterSpec(
                "batch_id", "批次", FilterKind.REF, ref=RefTarget("batch", "code")
            ),
        ),
    )
)

RESOURCES.register(
    Resource(
        name="harvest",
        title="出塘记录",
        module="production",
        list_path="/api/v1/harvests",
        detail_path="/api/v1/harvests/{harvest_id}",
        workflow=HARVEST_WORKFLOW,
        table="harvests",
        columns=(
            ("code", "出塘单号"),
            ("name", "出塘事项"),
            ("pond_name", "塘口"),
            ("batch_code", "批次"),
            ("quantity", "出塘数量"),
            ("weight_kg", "重量（kg）"),
            ("happened_at", "出塘时间"),
            ("status", "状态码"),
            ("status_label", "状态"),
        ),
        # 与 `list_feedings` 同形（共用 `read.py::_list`）。
        search=True,
        filters=(
            FilterSpec("status", "状态", FilterKind.STATUS),
            FilterSpec(
                "pond_id", "塘口", FilterKind.REF, ref=RefTarget("pond", "name")
            ),
            FilterSpec(
                "batch_id", "批次", FilterKind.REF, ref=RefTarget("batch", "code")
            ),
        ),
    )
)

# ---------------------------------------------------------------------------
# Cross-domain read entry point (ROLLOUT_CONTRACT Sec 2)
#
# Sec 2 is explicit: "any domain may NOT read or write another domain's tables.
# Cross-domain effects may only go through the owning domain's NAMED in-process
# function." The only stated exception is kernel invariants, which are
# table-name-parameterised by design.
#
# Three real violations existed against the production tables:
#   cost/entries_write.py  _TARGET_TABLE["batch"] -> production_batches
#   sales/sales_orders.py  LEFT JOIN production_batches (2 queries)
#   sales/sales_orders_write.py  validation "the batch belongs to this pond"
# plus sales' LEFT JOIN harvests (HarvestQuantityMatch is the permitted kernel
# exception; those joins are not).
#
# Rather than have each domain hard-code the table name and its columns -- which
# also lets them drift when the schema changes -- production owns this one
# read-only accessor. It is deliberately NOT a scope-checked service method:
# the kernel's own invariants (`SameTenant`, `ReferencedStatus`) already do
# cross-domain row reads without a scope predicate, and the caller's write path
# applies its own scope rules. Requiring a Scope here would make the function
# unusable from the places that need it while adding no protection the caller
# does not already have.
# ---------------------------------------------------------------------------


def lookup_batch(
    tx: UnitOfWork, *, batch_id: int
) -> dict[str, Any] | None:
    """Read one batch's identity, tenant keys and pond, for OTHER domains.

    Returns the fields the two real consumers need:

        id / code / pond_id / species / status / batch_status,
        organization_id / farm_id / area_id   (the tenant keys)

    Returns ``None`` when the batch does not exist, so a caller can produce its
    own readable field error rather than depending on a foreign-key message --
    which is exactly what this project forbids (the old system decided constraint
    types by inspecting exception text, `production_store.py:142-146`).

    This is a plain function, not a service method, because it takes no `ctx`:
    it reports facts, it does not perform a business action.
    """
    return tx.query_one(
        "SELECT id, code, pond_id, species, status, batch_status, "
        "       organization_id, farm_id, area_id "
        "FROM production_batches WHERE id = %s",
        (int(batch_id),),
    )


def lookup_batches(
    tx: UnitOfWork, *, batch_ids: Iterable[int]
) -> dict[int, dict[str, Any]]:
    """Batch form of `lookup_batch`, for LIST RENDERING by other domains.

    ## Why a batch form exists, and why callers must use it

    A caller that needs batch facts for N rows must NOT loop `lookup_batch` (N+1
    queries) and must NOT `LEFT JOIN production_batches` either. The 负责人 ruled
    explicitly on this, and the reasoning generalises into a contract rule:

        When a domain needs data from another domain's tables, the OWNER provides a
        named read-only function; when the caller needs N rows, the owner provides a
        batch form (`lookup_*(ids)`). Callers must neither read the table directly
        nor ask for a registered exception.

    The reason list rendering is the important case: it is where schema coupling
    accumulates most quietly. If production renames a column or adds a state, a
    caller's `LEFT JOIN` keeps working while silently rendering a missing or wrong
    column, and BOTH domains' isolated e2e stay green -- the same failure shape this
    project spent a day chasing. Keeping the SQL in the owner's module means a schema
    change breaks in exactly one place, where the owner is already testing it.

    Returns a dict keyed by batch id, and **omits ids that do not exist** so the
    caller can decide what a missing reference means (a readable field error, or a
    blank cell) rather than being handed an exception it must translate.
    """
    wanted = sorted({int(value) for value in batch_ids})
    if not wanted:
        return {}
    placeholders = ",".join(["%s"] * len(wanted))
    rows = tx.query_all(
        "SELECT id, code, pond_id, species, status, batch_status, "
        "       organization_id, farm_id, area_id "
        f"FROM production_batches WHERE id IN ({placeholders})",
        wanted,
    )
    return {int(row["id"]): row for row in rows}


def lookup_harvest(
    tx: UnitOfWork, *, harvest_id: int
) -> dict[str, Any] | None:
    """Read one harvest's immutable facts, for OTHER domains (notably sales).

    The sales domain binds deliveries to a harvest document and enforces invariant
    #6 (`HarvestQuantityMatch`, tolerance exact). It must treat a verified harvest
    as an IMMUTABLE FACT: `harvest.verify` never modifies `quantity`/`weight_kg`.
    See this module's docstring (`service.py`) for that cross-domain promise.
    """
    return tx.query_one(
        "SELECT id, code, pond_id, batch_id, quantity, weight_kg, status, "
        "       organization_id, farm_id, area_id "
        "FROM harvests WHERE id = %s",
        (int(harvest_id),),
    )
