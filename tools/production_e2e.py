"""End-to-end proof for the production domain against real MySQL.

This is the executable evidence for the production domain (t5). It runs the REAL
composition root and the REAL `CapabilityRunner` -- no hand-built registry, no
fixture that patches anything onto the service.

## Why "no fixture patching" is an explicit assertion here

The master_data domain shipped for a long time with write capabilities that
returned HTTP 500 on every real path, because the reload function
(`__yuxin_load_by_id__`) was attached only inside the e2e fixtures
(`tools/runner_e2e.py:199-201`, `web_e2e.py:133`, `agent_e2e.py:147-148`). The
fixtures covered the production path, so all seven self-checks stayed green while
the real path was broken.

`ROLLOUT_CONTRACT.md` Sec 3 calls this a template debt. To make sure the new
domains do not copy it, this file asserts:

    `CapabilityRunner._resolve()` can resolve `__yuxin_load_by_id__` for every write
    capability straight from the registered handler, WITHOUT any patching.

## What is proven

    1. Full happy path: create -> submit -> verify (forms initial stock) ->
       feed -> harvest -> close, each via the real runner.
    2. `batch.verify` really appended a POSITIVE stocking row.
    3. `feeding.verify` did NOT change pond stock (zero-delta row only).
    4. `harvest.verify` really appended a NEGATIVE row and reduced the balance.
    5. Invariant #2 blocks a harvest larger than the pond stock (and rolls back).
    6. Invariant #5 blocks the creator from verifying their own document.
    7. Invariant #11 blocks `batch.close` while stock is non-zero, and allows it
       once stock is zero (this is the reachability chain of DECISIONS.md Q10).
    8. The reload path resolves without fixture help (the template-debt guard).

Usage::

    $env:MYSQL_DATABASE='yuxin_production'
    python tools/production_e2e.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from yuxin.kernel import capability as cap  # noqa: E402
from yuxin.kernel.audit import AuditWriter  # noqa: E402
from yuxin.kernel.errors import DomainError  # noqa: E402
from yuxin.kernel.idempotency import IdempotencyStore  # noqa: E402
from yuxin.kernel.runner import ActorView, CapabilityRunner, Invocation  # noqa: E402
from yuxin.kernel.scope import Scope  # noqa: E402
from yuxin.kernel.uow import ConnectionConfig, UnitOfWork  # noqa: E402

FAILURES = 0


def _fixture_name(sql: str) -> str:
    """读 `id=1` 那一行在库里的**实际**名字（用于"派生列解析正确"的断言）。

    为什么不写死字面量：本脚本要能跑在**共享开发库**上（同 `sales_e2e.py` 的模块
    docstring 明说）。`ponds.id=1` / `areas.id=1` 可能已被别的数据占用 —— 本文件的
    夹具用 `ON DUPLICATE KEY UPDATE status=...`，**刻意不覆盖别人的 name**，所以写死的
    期望值会在那种库上变红。断言真正要验的是"派生列由 master_data 的访问器解析出来"，
    不是"这口塘叫什么"，改成与库中实际值比对后判据不变。
    """
    with uow_factory().begin() as tx:
        row = tx.query_one(sql)
    return str(row["name"]) if row else ""


def fixture_pond_name() -> str:
    return _fixture_name("SELECT name FROM ponds WHERE id=1")


def fixture_area_name() -> str:
    return _fixture_name("SELECT name FROM areas WHERE id=1")


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


def _config() -> ConnectionConfig:
    return ConnectionConfig(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "yuxin"),
        password=os.environ.get("MYSQL_PASSWORD", "yuxin_dev_password"),
        database=os.environ.get("MYSQL_DATABASE", "yuxin"),
    )


def uow_factory() -> UnitOfWork:
    return UnitOfWork(_config())


class FixedScopeResolver:
    """Resolve the actor's DataScope to a known area set.

    Real deployments resolve this from `user_data_scopes`; the test pins it so the
    scope predicate is deterministic. It still exercises the REAL `Scope` object
    and the REAL `scope.allows_row()` checks in the services.
    """

    def __init__(self, scopes):
        self._scopes = scopes

    def resolve(self, *, user_id: int, role_codes: frozenset[str]) -> Scope:
        # Signature must match the kernel's `ScopeResolver` protocol exactly.
        # `role_codes` participates in real RBAC resolution; the fixture pins the
        # area set, so it is accepted and deliberately unused.
        return Scope.from_rows(self._scopes, user_id=user_id, allow_all=not self._scopes)


#: Every permission the walkthrough needs.
CREATOR_PERMISSIONS = frozenset(
    {
        "batch.view",
        "batch.create",
        "batch.update",
        "batch.verify",
        "feeding.view",
        "feeding.create",
        "feeding.verify",
        "harvest.view",
        "harvest.create",
        "harvest.verify",
    }
)
VERIFIER_PERMISSIONS = frozenset(
    {
        "batch.view",
        "batch.update",
        "batch.verify",
        "feeding.view",
        "feeding.verify",
        "harvest.view",
        "harvest.verify",
    }
)


def seed(tx: UnitOfWork) -> dict:
    """Seed identity + master data needed by the production domain.

    Rows are created with explicit ids and `ON DUPLICATE KEY UPDATE` so the script
    is re-runnable. Traceability matters here: production tables carry real
    foreign keys to `organizations` / `users` / `areas` / `ponds` / `materials`,
    so this seeding also proves that FK chain is intact.
    """
    tx.execute(
        "INSERT INTO organizations (id, code, name) VALUES (1,'E2E-ORG','端到端测试企业') "
        "ON DUPLICATE KEY UPDATE name=VALUES(name)"
    )
    tx.execute(
        "INSERT INTO farms (id, organization_id, code, name) VALUES "
        "(1,1,'E2E-FARM','端到端测试基地') ON DUPLICATE KEY UPDATE name=VALUES(name)"
    )
    # `areas.created_by` is NOT NULL, so the seeding must name an actor.
    tx.execute(
        "INSERT INTO areas (id, organization_id, farm_id, code, name, status, created_by) "
        "VALUES (1,1,1,'E2E-AREA','端到端测试区域','verified',1) "
        "ON DUPLICATE KEY UPDATE name=VALUES(name)"
    )
    tx.execute(
        "INSERT INTO users (id, username, display_name, password_hash, status) VALUES "
        "(1,'e2e_maker','端到端经办人','x','active'),"
        "(2,'e2e_checker','端到端核验人','x','active') "
        "ON DUPLICATE KEY UPDATE display_name=VALUES(display_name)"
    )
    tx.execute(
        "INSERT INTO ponds (id, organization_id, farm_id, area_id, code, name, species, "
        " pond_status, status, created_by) VALUES "
        "(1,1,1,1,'E2E-POND-1','端到端一号塘','草鱼','farming','verified',1) "
        "ON DUPLICATE KEY UPDATE status=VALUES(status)"
    )
    # A verified feed material. registry Sec 2.6 requires the material to be
    # `verified` before it can be fed, and materials also carry the tenant
    # columns that DataScope needs.
    tx.execute(
        "INSERT INTO materials (id, organization_id, farm_id, area_id, code, name, "
        " category, unit, unit_price, status, created_by) VALUES "
        "(1,1,1,1,'E2E-FEED-1','端到端配合饲料','feed','kg',5.0000,'verified',1) "
        "ON DUPLICATE KEY UPDATE status=VALUES(status)"
    )
    # A default, verified warehouse. `feeding.verify` debits material inventory
    # through warehouse's entry point, and `resolve_issue_lot` picks the warehouse
    # marked `is_default` inside the data scope.
    tx.execute(
        "INSERT INTO warehouses (id, organization_id, farm_id, area_id, code, name, "
        " is_default, status, created_by, verified_by) VALUES "
        "(1,1,1,1,'E2E-WH-1','端到端一号仓',1,'verified',1,2) "
        "ON DUPLICATE KEY UPDATE status=VALUES(status)"
    )
    return {"area_id": 1}


def seed_opening_stock() -> None:
    """Put feed material into the warehouse, through the REAL inventory entry point.

    Opening stock is written with `warehouse.ledger.apply_movement(create_lot=True)`
    rather than a raw INSERT, so the ledger row and the lot are created by the one
    function the whole system uses. That also means the production e2e exercises
    the warehouse contract instead of assuming it.
    """
    from datetime import date, datetime, timedelta

    from yuxin.domains.warehouse.ledger import apply_movement, ledger_source_ref

    scope = Scope.from_rows([{"scope_type": "area", "area_id": 1}], user_id=1)
    with uow_factory().begin() as tx:
        # `resolve_issue_lot` 按 FEFO（`ORDER BY (expiry_date IS NULL), expiry_date, id`）
        # 选批次。共享开发库里 `database/test_data.sql` 的业务批次与本探针**同物料同仓**
        # （LOT-20260901-001，2027-02-25 到期）；若探针批次到期更晚，FEFO 会扣真实业务
        # 库存，本脚本关于扣减批次的断言就会红（实测重置后首跑 4 项失败）。
        # 因此探针到期日取“现有非探针批次的最早到期日 − 1 天”，保证 FEFO 必选探针；
        # 没有可竞争的批次时用一个不影响任何人的固定未来日期。
        existing = tx.query_one(
            "SELECT MIN(expiry_date) AS earliest FROM inventory_lots "
            "WHERE warehouse_id=%s AND material_id=%s AND lot_no NOT LIKE %s",
            (1, 1, "E2E-%"),
        )
        earliest = existing["earliest"] if existing else None
        if isinstance(earliest, datetime):
            expiry = earliest.date() - timedelta(days=1)
        elif isinstance(earliest, date):
            expiry = earliest - timedelta(days=1)
        else:
            expiry = date(2027, 1, 31)
        apply_movement(
            tx,
            scope=scope,
            actor_id=1,
            source_type="receipt",
            source_ref=ledger_source_ref("warehouse", "E2E-OPENING-1"),
            source_line_no=1,
            warehouse_id=1,
            material_id=1,
            quantity_delta=1000,
            happened_at="2026-09-01 07:00:00",
            lot_no="E2E-LOT-1",
            create_lot=True,
            production_date="2026-08-01",
            expiry_date=expiry.isoformat(),
            unit_cost=5,
        )


def clean(tx: UnitOfWork) -> None:
    """Remove THIS suite's probe rows only.

    ROLLOUT_CONTRACT Sec 5 requires the shared default database (`yuxin`): every
    suite runs against the SAME schema, and isolation comes from probe-prefixed rows
    plus cleanup -- NOT from a database per domain ("one DB per domain" was
    overturned by the 负责人; it had also caused a `REVOKE ALL` incident).

    That makes an unscoped `DELETE FROM <table>` a real hazard: it would silently
    destroy another domain's in-flight probe data and make their suite fail for
    reasons that have nothing to do with their code. So every statement here is
    scoped to `E2E-`-prefixed rows owned by this suite.

    `audit_logs` is append-only by design and is deliberately never cleaned.
    """
    # Ledger + documents of this suite's batches.
    tx.execute(
        "DELETE FROM batch_stock_records WHERE batch_id IN "
        "(SELECT id FROM production_batches WHERE code LIKE 'E2E-%')"
    )
    tx.execute(
        "DELETE FROM feedings WHERE batch_id IN "
        "(SELECT id FROM production_batches WHERE code LIKE 'E2E-%') "
        "OR code LIKE 'E2E-%'"
    )
    tx.execute(
        "DELETE FROM harvests WHERE batch_id IN "
        "(SELECT id FROM production_batches WHERE code LIKE 'E2E-%') "
        "OR code LIKE 'E2E-%'"
    )
    tx.execute("DELETE FROM production_batches WHERE code LIKE 'E2E-%'")

    # Inventory: this suite's lot is `E2E-LOT-1`, and its issue row is
    # `feeding:E2E-*`. Both are scoped, so a warehouse suite's own lots survive.
    tx.execute(
        "DELETE FROM inventory_ledger WHERE source_ref LIKE 'feeding:E2E-%' "
        "OR inventory_lot_id IN (SELECT id FROM inventory_lots WHERE lot_no LIKE 'E2E-%')"
    )
    tx.execute("DELETE FROM inventory_lots WHERE lot_no LIKE 'E2E-%'")

    # The cost entry this suite's feeding produced.
    tx.execute("DELETE FROM cost_entries WHERE source_ref LIKE 'feeding:E2E-%'")

    # Only this suite's capability keys.
    tx.execute(
        "DELETE FROM idempotency_keys WHERE capability LIKE 'batch.%' "
        "OR capability LIKE 'feeding.%' OR capability LIKE 'harvest.%'"
    )


def version_of(table: str, record_id: int) -> int:
    """Read the current row_version, which the optimistic-lock field requires.

    `expected_version` is mandatory on every write (declared `required=True` with
    `minimum=1`), so each action must carry the version it expects. Reading it
    between steps also documents the real optimistic-lock flow rather than
    smuggling in a placeholder.
    """
    uow = uow_factory()
    with uow_factory().begin() as tx:
        row = tx.query_one(f"SELECT row_version FROM {table} WHERE id = %s", (record_id,))
    if row is None:
        raise RuntimeError(f"{table}#{record_id} not found while reading version")
    return int(row["row_version"])


def issued_lot_balance() -> tuple:
    """Balance and identity of the lot THIS suite's feeding actually issued.

    Scoped to the feeding's own ledger row rather than summing every row for the
    material. The shared database (ROLLOUT_CONTRACT Sec 5) means a peer domain's
    probe may hold stock for the same material at the same moment -- a
    material-wide sum would silently fold their quantity into my assertion and fail
    for a reason that has nothing to do with my code. That is exactly what happened
    with warehouse's `WHE2E-*` probe, so the assertion is now scoped to the lot this
    suite's own document touched.
    """
    with uow_factory().begin() as tx:
        row = tx.query_one(
            "SELECT inventory_lot_id, lot_no FROM inventory_ledger "
            "WHERE source_ref = %s AND source_type = 'issue'",
            ("feeding:E2E-FEED-1",),
        )
        if row is None:
            return None
        lot_id = int(row["inventory_lot_id"])
        bal = tx.query_one(
            "SELECT COALESCE(SUM(quantity_delta),0) AS b FROM inventory_ledger "
            "WHERE inventory_lot_id = %s",
            (lot_id,),
        )
    return lot_id, str(row["lot_no"]), (bal or {}).get("b")


def stock_balance(tx: UnitOfWork, batch_id: int) -> tuple:
    row = tx.query_one(
        "SELECT COALESCE(SUM(quantity_delta),0) AS q, "
        "COALESCE(SUM(weight_delta_kg),0) AS w "
        "FROM batch_stock_records WHERE batch_id = %s",
        (batch_id,),
    )
    return (row or {}).get("q", 0), (row or {}).get("w", 0)


def main() -> int:
    # The REAL composition root: discovers every yuxin/domains/*/capabilities.py.
    #
    # `bootstrap.load_all()` imports EVERY domain, so a peer domain that is
    # mid-edit and syntactically broken would take this whole suite down. Rather
    # than silently skipping the composition root (which would defeat the point of
    # testing against it), the real loader is used and any domain that fails to
    # import is reported loudly. The production assertions below then run against
    # the genuinely loaded domain set.
    # Syntax pre-check BEFORE importing. Without it, a syntax error in ANY
    # domain's module surfaces as a misleading failure: `ast.parse()` raises inside
    # a shared test helper and pytest attributes it to an unrelated assertion (this
    # happened for real -- a broken `capabilities.py` was reported as a "MySQL
    # driver isolation" violation, and the message listed unrelated modules). A
    # syntax error in one domain therefore appears, under a wrong name, in someone
    # else's output. This check names the actual file and line instead.
    import ast as _ast

    syntax_broken = []
    _backend = Path(__file__).resolve().parents[1] / "backend" / "yuxin"
    for _path in sorted(_backend.rglob("*.py")):
        if "__pycache__" in _path.parts:
            continue
        try:
            _ast.parse(_path.read_text(encoding="utf-8"))
        except SyntaxError as _exc:
            syntax_broken.append(f"{_path.name} line {_exc.lineno}: {_exc.msg}")
    check("no module in the tree has a syntax error", syntax_broken == [],
          "; ".join(syntax_broken[:4]))

    import yuxin.bootstrap as bootstrap

    broken_domains = []
    for domain in bootstrap.discover_domains():
        module_name = bootstrap._module_name(domain)
        try:
            __import__(module_name)
        except Exception as exc:  # noqa: BLE001
            broken_domains.append(f"{domain} ({type(exc).__name__}: {exc})")

    registry = cap.REGISTRY
    if broken_domains:
        print("!! WARNING: these domains failed to import, so the composition root")
        print("!! is incomplete. This is NOT a production-domain failure, but the")
        print("!! run is not a full-composition-root proof:")
        for item in broken_domains:
            print(f"!!   - {item}")
        print()

    # A fresh UnitOfWork per transaction: the kernel forbids reusing one
    # ("同一个 UnitOfWork 不能重复 begin").
    with uow_factory().begin() as tx:
        seed(tx)
        clean(tx)
    seed_opening_stock()

    audit = AuditWriter()
    idem = IdempotencyStore(uow_factory)
    resolver = FixedScopeResolver([{"scope_type": "area", "area_id": 1}])
    runner = CapabilityRunner(
        registry=registry,
        uow_factory=uow_factory,
        audit=audit,
        idempotency=idem,
        scope_resolver=resolver,
    )

    maker = ActorView(user_id=1, username="e2e_maker", permissions=CREATOR_PERMISSIONS)
    checker = ActorView(user_id=2, username="e2e_checker", permissions=VERIFIER_PERMISSIONS)

    print("=== 0. capability ledger ===")
    produced = sorted(c.name for c in registry.all() if c.domain == "production")
    expected = sorted(
        [
            "batch.list", "batch.get", "batch.create", "batch.update", "batch.submit",
            "batch.verify", "batch.close",
            "feeding.list", "feeding.get", "feeding.create", "feeding.verify",
            "harvest.list", "harvest.get", "harvest.create", "harvest.verify",
        ]
    )
    check("exactly the 15 registry capabilities are registered", produced == expected,
          f"{produced}")

    # ---------------------------------------------------------------- template debt
    print("\n=== 1. reload resolves WITHOUT fixture patching (template-debt guard) ===")
    unresolvable = []
    for item in registry.all():
        if item.domain != "production" or not item.is_write:
            continue
        if getattr(item.handler, "__yuxin_load_by_id__", None) is None:
            unresolvable.append(item.name)
    check("every write capability resolves __yuxin_load_by_id__ from the handler itself",
          unresolvable == [], f"missing: {unresolvable}")

    # ---------------------------------------------------------------- happy path
    print("\n=== 2. batch: create -> submit -> verify (forms initial stock) ===")
    created = runner.invoke(
        Invocation(
            capability_name="batch.create",
            payload={
                "code": "E2E-BATCH-1", "name": "端到端批次一", "pond_id": 1,
                "species": "草鱼", "stocked_at": "2026-09-01 08:00:00",
                "initial_quantity": 1000, "initial_weight_kg": 100,
            },
            idempotency_key="e2e-batch-create-1",
        ),
        maker, "req-prod-1",
    )
    check("batch.create executed", created.kind == "executed", created.kind)
    batch_id = created.resource_id
    check("returned a real primary key", isinstance(batch_id, int) and batch_id > 0, str(batch_id))

    submitted = runner.invoke(
        Invocation(capability_name="batch.submit",
                   payload={"batch_id": batch_id,
                            "expected_version": version_of("production_batches", batch_id)},
                   path_params={"batch_id": batch_id}, idempotency_key="e2e-batch-submit-1"),
        maker, "req-prod-2",
    )
    check("batch.submit executed", submitted.kind == "executed", submitted.kind)

    # self-approval must be refused (invariant #5)
    self_verify_failed = False
    try:
        runner.invoke(
            Invocation(capability_name="batch.verify",
                       payload={"batch_id": batch_id,
                                   "expected_version": version_of("production_batches", batch_id)},
                       path_params={"batch_id": batch_id}, idempotency_key="e2e-batch-selfverify"),
            maker, "req-prod-3",
        )
    except DomainError:
        self_verify_failed = True
    check("invariant #5: creator cannot verify their own batch", self_verify_failed)

    verified = runner.invoke(
        Invocation(capability_name="batch.verify",
                   payload={"batch_id": batch_id,
                            "expected_version": version_of("production_batches", batch_id)},
                   path_params={"batch_id": batch_id}, idempotency_key="e2e-batch-submit-1"),
        checker, "req-prod-4",
    )
    check("batch.verify executed by a different actor", verified.kind == "executed", verified.kind)

    with uow_factory().begin() as tx:
        q, w = stock_balance(tx, batch_id)
    check("initial stocking row formed (+1000 / +100)", str(q) == "1000.000" and str(w) == "100.000",
          f"q={q} w={w}")

    print("\n=== 2d. list rendering resolves cross-domain display columns ===")
    # The list path no longer JOINs ponds/areas/materials: it goes through
    # master_data's BATCH accessors (Sec 2.0), so these columns are derived in
    # Python. Assert they still resolve, because a broken derivation would render
    # blanks in the UI while every other assertion stays green.
    listed = runner.invoke(
        Invocation(capability_name="batch.list", query={"page": 1, "page_size": 50}),
        maker, "req-prod-list-1",
    )
    check("batch.list executed", listed.kind == "read", listed.kind)
    items = (listed.data or {}).get("items") or []
    mine = [r for r in items if str(r.get("code")) == "E2E-BATCH-1"]
    check("the batch appears in its own list", len(mine) == 1, f"{len(items)} items")
    if mine:
        check("pond_name is resolved via master_data's batch accessor",
              mine[0].get("pond_name") == fixture_pond_name(), str(mine[0].get("pond_name")))
        check("area_name is resolved via master_data's batch accessor",
              mine[0].get("area_name") == fixture_area_name(), str(mine[0].get("area_name")))

    print("\n=== 2c. cross-domain read entry points (ROLLOUT_CONTRACT Sec 2) ===")
    # Sec 2 forbids one domain reading another domain's tables (only kernel
    # invariants are excepted). cost and sales both read production tables directly
    # today, so production owns these NAMED read-only accessors instead of letting
    # each domain hard-code the table and its columns.
    from yuxin.domains.production.service import lookup_batch, lookup_harvest

    with uow_factory().begin() as tx:
        looked_up = lookup_batch(tx, batch_id=batch_id)
    check("lookup_batch returns the batch for other domains",
          looked_up is not None and int(looked_up["id"]) == int(batch_id),
          str(looked_up))
    check("lookup_batch exposes the tenant keys cost needs to resolve",
          all(looked_up.get(k) is not None
              for k in ("organization_id", "farm_id", "area_id")),
          str(looked_up))
    check("lookup_batch exposes the pond and species sales needs",
          int(looked_up["pond_id"]) == 1 and looked_up.get("species"),
          str(looked_up))

    # A missing row must return None so the caller raises its OWN readable field
    # error -- never a foreign-key message, which is the message-text inspection
    # this project forbids (`production_store.py:142-146`).
    with uow_factory().begin() as tx:
        missing_batch = lookup_batch(tx, batch_id=987654321)
        missing_harvest = lookup_harvest(tx, harvest_id=987654321)
    check("lookup_batch returns None for a missing batch (not an error)",
          missing_batch is None, str(missing_batch))
    check("lookup_harvest returns None for a missing harvest",
          missing_harvest is None, str(missing_harvest))

    # Batch form, for list rendering: one query for N rows (no N+1, and no
    # caller-side JOIN). Missing ids are OMITTED so the caller decides what a
    # dangling reference means.
    from yuxin.domains.production.service import lookup_batches

    with uow_factory().begin() as tx:
        many = lookup_batches(tx, batch_ids=[batch_id, 987654321, batch_id])
    check("lookup_batches returns exactly the ids that exist",
          set(many) == {int(batch_id)}, str(sorted(many)))
    check("lookup_batches carries the same fields as the single form",
          many[int(batch_id)]["code"] == looked_up["code"]
          and int(many[int(batch_id)]["pond_id"]) == int(looked_up["pond_id"]),
          str(many))
    with uow_factory().begin() as tx:
        empty = lookup_batches(tx, batch_ids=[])
    check("lookup_batches with no ids issues no query and returns empty",
          empty == {}, str(empty))


    # NOTE on a hypothesis I tested and had to discard: a stale row sharing the
    # code was expected to be able to cause a false-positive rejection. It cannot
    # on the CREATE path, and the reason is structural: `UniqueCode` is a post-write
    # invariant, but the INSERT itself hits `uq_production_batches_org_code` first,
    # so the DB key decides and the service translates that into a readable 409.
    # The kernel's SQL push-down of self-exclusion therefore matters to
    # `UniqueCode`'s own search (updates, and future callers), not to create.
    # The unique key is the correctness floor, exactly as registry Sec 4 #19 says.
    print("\n=== 2b. invariant #19: duplicate code is refused through the real runner ===")
    # This is the regression guard for the kernel `UniqueCode` defect that was
    # found during this task: the invariant used to compare `before["id"]`, which is
    # None on create, so it never excluded the row it had just written and reported
    # a conflict for EVERY create. It now uses the injected `_invariant_exclude_id`.
    # A fresh Idempotency-Key is required so this is a genuine second request
    # (reusing the key would be short-circuited by the idempotency layer, which
    # would make this assertion pass for the wrong reason).
    duplicate_refused = False
    duplicate_message = ""
    try:
        runner.invoke(
            Invocation(
                capability_name="batch.create",
                payload={
                    "code": "E2E-BATCH-1", "name": "重复编号批次", "pond_id": 1,
                    "species": "草鱼", "stocked_at": "2026-09-01 08:00:00",
                    "initial_quantity": 10,
                },
                idempotency_key="e2e-batch-create-duplicate",
            ),
            maker, "req-prod-1b",
        )
    except DomainError as error:
        duplicate_refused = True
        duplicate_message = error.message
    check("duplicate batch code refused (invariant #19)",
          duplicate_refused, duplicate_message)



    # ---------------------------------------------------------------- feeding
    print("\n=== 3. feeding.verify must NOT change pond stock ===")
    feeding = runner.invoke(
        Invocation(
            capability_name="feeding.create",
            payload={
                "code": "E2E-FEED-1", "name": "端到端投喂一", "pond_id": 1,
                "batch_id": batch_id, "material_id": 1, "quantity": 50,
                "happened_at": "2026-09-02 08:00:00",
            },
            idempotency_key="e2e-feeding-create-1",
        ),
        maker, "req-prod-5",
    )
    check("feeding.create executed", feeding.kind == "executed", feeding.kind)

    # `feeding.verify` has THREE effects (registry Sec 3.3): it debits material
    # inventory (warehouse), appends a cost-only row to the pond-stock ledger, and
    # collects cost (cost). The first and third are CROSS-DOMAIN and therefore
    # travel through the other domains' entry points, inside this one transaction.
    feeding_verified = runner.invoke(
        Invocation(capability_name="feeding.verify",
                   payload={"feeding_id": feeding.resource_id,
                            "expected_version": version_of("feedings", feeding.resource_id)},
                   path_params={"feeding_id": feeding.resource_id},
                   idempotency_key="e2e-feeding-verify-1"),
        checker, "req-prod-6",
    )
    check("feeding.verify executed", feeding_verified.kind == "executed", feeding_verified.kind)
    # The 负责人 requires the DEFAULT to be explainable, not merely visible.
    # Print the real user-facing message so the basis is auditable in output.
    print(f"  INFO  feeding.verify message: {feeding_verified.message}")

    with uow_factory().begin() as tx:
        q2, _ = stock_balance(tx, batch_id)
        feeding_row = tx.query_one("SELECT status FROM feedings WHERE id = %s",
                                   (feeding.resource_id,))
        # Effect (1) is asserted below, scoped to the issued lot (see
        # `issued_lot_balance`).
        # Effect (3): a cost entry was collected by the cost domain.
        cost_row = tx.query_one(
            "SELECT source_type, source_ref, target_type, target_id, amount, category_code "
            "FROM cost_entries WHERE source_ref = %s",
            ("feeding:E2E-FEED-1",),
        )

    # Effect (2) is the one that is easiest to get wrong: feeding must NOT reduce
    # POND stock. If it did, batch.close would become reachable while the pond
    # still holds fish.
    check("effect (2): pond stock UNCHANGED by feeding (still 1000)",
          str(q2) == "1000.000", f"q={q2}")
    check("feeding is verified",
          str((feeding_row or {}).get("status")) == "verified", str(feeding_row))
    # Effect (1): the issued lot was debited 1000 -> 950. Scoped to this suite's
    # own lot so a concurrent peer probe cannot contaminate the number.
    issued = issued_lot_balance()
    check("effect (1): the issued lot was created and debited",
          issued is not None, str(issued))
    if issued is not None:
        check("effect (1): issued lot balance is 950 (opening 1000 - fed 50)",
              str(issued[2]) == "950.0000", str(issued))
    check("effect (3): a cost entry was collected for this feeding",
          cost_row is not None, str(cost_row))
    if cost_row:
        check("cost entry uses source_type=warehouse_ledger and targets the batch",
              str(cost_row["source_type"]) == "warehouse_ledger"
              and str(cost_row["target_type"]) == "batch"
              and int(cost_row["target_id"]) == int(batch_id),
              str(cost_row))
        # 50 consumed x weighted-average receipt cost 5.00 = 250.00
        check("cost amount = consumed quantity x weighted-average receipt unit cost",
              str(cost_row["amount"]) == "250.00", str(cost_row["amount"]))
        # The category comes from the MATERIAL's category through cost's own mapping
        # table, not a hard-coded literal: a feeding may consume a seed/health
        # material, and hard-coding would book those under the wrong category.
        from yuxin.domains.cost.entries import MATERIAL_CATEGORY_TO_COST
        check("cost category is mapped from the material's category",
              str(cost_row["category_code"])
              == MATERIAL_CATEGORY_TO_COST.get("feed", "feed"),
              str(cost_row["category_code"]))

    # Re-verifying the same feeding must be refused: the state machine only allows
    # draft -> verified, and the inventory ledger's unique key would refuse a
    # second posting of the same source line anyway.
    replay_refused = False
    try:
        runner.invoke(
            Invocation(capability_name="feeding.verify",
                       payload={"feeding_id": feeding.resource_id,
                                "expected_version": version_of("feedings", feeding.resource_id)},
                       path_params={"feeding_id": feeding.resource_id},
                       idempotency_key="e2e-feeding-verify-replay"),
            checker, "req-prod-6b",
        )
    except DomainError:
        replay_refused = True
    check("re-verifying an already verified feeding is refused", replay_refused)

    issued_after = issued_lot_balance()
    check("the refused replay did not double-debit the issued lot",
          issued_after is not None and str(issued_after[2]) == "950.0000",
          str(issued_after))

    print("\n=== 3b. explicit warehouse_id / lot_no hints are honoured ===")
    # The 负责人 ruled these two fields in as OPTIONAL, with the default (FEFO)
    # unchanged. This proves the OTHER half: that supplying them actually pins the
    # choice rather than being silently ignored.
    hinted = runner.invoke(
        Invocation(
            capability_name="feeding.create",
            payload={
                "code": "E2E-FEED-2", "name": "端到端投喂二", "pond_id": 1,
                "batch_id": batch_id, "material_id": 1, "quantity": 20,
                "happened_at": "2026-09-02 09:00:00",
                "warehouse_id": 1, "lot_no": "E2E-LOT-1",
            },
            idempotency_key="e2e-feeding-create-2",
        ),
        maker, "req-prod-6c",
    )
    check("feeding.create accepts the optional warehouse_id / lot_no",
          hinted.kind == "executed", f"{hinted.kind}: {hinted.message}")

    with uow_factory().begin() as tx:
        stored = tx.query_one(
            "SELECT warehouse_id, lot_no FROM feedings WHERE id = %s",
            (hinted.resource_id,),
        )
    check("the hints are stored on the row (so create and verify agree)",
          stored is not None and int(stored["warehouse_id"]) == 1
          and str(stored["lot_no"]) == "E2E-LOT-1",
          str(stored))

    with uow_factory().begin() as tx:
        v = int(tx.query_one("SELECT row_version FROM feedings WHERE id = %s",
                             (hinted.resource_id,))["row_version"])
    hinted_verified = runner.invoke(
        Invocation(capability_name="feeding.verify",
                   payload={"feeding_id": hinted.resource_id, "expected_version": v},
                   path_params={"feeding_id": hinted.resource_id},
                   idempotency_key="e2e-feeding-verify-2"),
        checker, "req-prod-6d",
    )
    check("feeding.verify honours the hint", hinted_verified.kind == "executed",
          hinted_verified.kind)
    print(f"  INFO  hinted message: {hinted_verified.message}")
    check("the message states the lot was pinned by the user, not chosen by FEFO",
          "按登记时指定" in hinted_verified.message, hinted_verified.message)

    fed = runner.invoke(
        Invocation(capability_name="feeding.list", query={"page": 1, "page_size": 50}),
        maker, "req-prod-list-2",
    )
    fed_items = [r for r in ((fed.data or {}).get("items") or [])
                 if str(r.get("code")) == "E2E-FEED-1"]
    # `material_name` must be RESOLVED, but not to a hard-coded string: `materials`
    # id=1 is also seeded by a peer suite in the shared database, so its name can
    # legitimately be theirs. Asserting the derivation ran is the honest check;
    # asserting MY name would fail for a reason unrelated to my code.
    check("feeding.list resolves pond_name via the accessor",
          bool(fed_items) and fed_items[0].get("pond_name") == fixture_pond_name(),
          str(fed_items[0]) if fed_items else "not listed")
    check("feeding.list resolves material_name via the accessor (non-empty)",
          bool(fed_items) and bool(fed_items[0].get("material_name")),
          str(fed_items[0].get("material_name")) if fed_items else "not listed")

    print("\n=== 3c. #1: over-feeding must be REFUSED (material stock cannot go negative) ===")
    # This is the compensating control for 负责人's ruling (4): warehouse removed
    # the negative-stock judgement from `apply_movement`, so the ONLY thing stopping
    # a feeding from driving material stock negative is the `NoNegativeStock` on
    # `inventory_ledger` declared on this capability. Without it, an over-issue is
    # accepted silently -- measured directly against `apply_movement` earlier.
    over = runner.invoke(
        Invocation(
            capability_name="feeding.create",
            payload={
                "code": "E2E-FEED-OVER", "name": "超额投喂", "pond_id": 1,
                "batch_id": batch_id, "material_id": 1, "quantity": 100000,
                "happened_at": "2026-09-02 10:00:00",
            },
            idempotency_key="e2e-feeding-create-over",
        ),
        maker, "req-prod-6e",
    )
    with uow_factory().begin() as tx:
        v = int(tx.query_one("SELECT row_version FROM feedings WHERE id = %s",
                             (over.resource_id,))["row_version"])

    over_rejected = False
    over_message = ""
    try:
        runner.invoke(
            Invocation(capability_name="feeding.verify",
                       payload={"feeding_id": over.resource_id, "expected_version": v},
                       path_params={"feeding_id": over.resource_id},
                       idempotency_key="e2e-feeding-verify-over"),
            checker, "req-prod-6f",
        )
    except DomainError as error:
        over_rejected = True
        over_message = error.message
    check("over-feeding is REFUSED (invariant #1 on inventory_ledger)",
          over_rejected, over_message)

    issued_now = issued_lot_balance()
    check("material stock unchanged after the refusal",
          issued_now is not None and str(issued_now[2]) == "930.0000", str(issued_now))
    with uow_factory().begin() as tx:
        over_row = tx.query_one("SELECT status FROM feedings WHERE id = %s",
                                (over.resource_id,))
    check("the refused over-feeding rolled back to draft",
          str((over_row or {}).get("status")) == "draft", str(over_row))

    print("\n=== 4. harvest.verify reduces pond stock ===")
    harvest = runner.invoke(
        Invocation(
            capability_name="harvest.create",
            payload={
                "code": "E2E-HARVEST-1", "name": "端到端出塘一", "pond_id": 1,
                "batch_id": batch_id, "quantity": 400, "weight_kg": 40,
                "happened_at": "2026-09-03 08:00:00",
            },
            idempotency_key="e2e-harvest-create-1",
        ),
        maker, "req-prod-7",
    )
    check("harvest.create executed", harvest.kind == "executed", harvest.kind)

    harvest_verified = runner.invoke(
        Invocation(capability_name="harvest.verify",
                   payload={"harvest_id": harvest.resource_id,
                               "expected_version": version_of("harvests", harvest.resource_id)},
                   path_params={"harvest_id": harvest.resource_id}, idempotency_key="e2e-harvest-verify-1"),
        checker, "req-prod-8",
    )
    check("harvest.verify executed", harvest_verified.kind == "executed", harvest_verified.kind)

    # §4 #5「经办人 ≠ 审批人」在 `harvest.verify` 上的**反例**（Q8′ 已把本能力并入 13 条）。
    # 这一条以前没有强制点：`[]` 里声明过、运行时没挂，而 e2e 只断言了"他人核验成功"
    # （那一半在两种实现下都通过）。所以必须有一条**自审被拒**的反例，否则"挂上了"与
    # "声明了但永不生效"在测试报告里长得一模一样。
    #
    # 构造方式：另建一张出塘单（由 `maker` 登记），再用 `maker` 自己核验。
    self_harvest = runner.invoke(
        Invocation(
            capability_name="harvest.create",
            payload={
                "code": "E2E-HARVEST-SELF", "name": "自审探针", "pond_id": 1,
                "batch_id": batch_id, "quantity": 1, "weight_kg": 1,
                "happened_at": "2026-09-03 08:30:00",
            },
            idempotency_key="e2e-harvest-create-self",
        ),
        maker, "req-prod-7b",
    )
    check("自审探针出塘单已登记（由 maker）", self_harvest.kind == "executed", self_harvest.kind)
    self_verify_rejected = None
    try:
        runner.invoke(
            Invocation(
                capability_name="harvest.verify",
                payload={"harvest_id": self_harvest.resource_id,
                         "expected_version": version_of("harvests", self_harvest.resource_id)},
                path_params={"harvest_id": self_harvest.resource_id},
                idempotency_key="e2e-harvest-verify-self",
            ),
            maker, "req-prod-8b",
        )
    except DomainError as error:
        self_verify_rejected = error
    check("经办人核验自己的出塘记录被拒（§4 #5 DistinctActors）",
          self_verify_rejected is not None
          and str(self_verify_rejected.code) == "FORBIDDEN"
          and (self_verify_rejected.data or {}).get("rule") == "DISTINCT_ACTORS",
          f"{self_verify_rejected}" if self_verify_rejected else "竟然成功了")
    with uow_factory().begin() as tx:
        still_draft = tx.query_one("SELECT status FROM harvests WHERE id=%s",
                                   (self_harvest.resource_id,))
    check("被拒的自审探针回滚为 draft（整体回滚，没有半成品）",
          str((still_draft or {}).get("status")) == "draft", str(still_draft))

    with uow_factory().begin() as tx:
        q3, w3 = stock_balance(tx, batch_id)
    check("stock reduced 1000 -> 600", str(q3) == "600.000", f"q={q3}")
    check("weight reduced 100 -> 60", str(w3) == "60.000", f"w={w3}")

    harv = runner.invoke(
        Invocation(capability_name="harvest.list", query={"page": 1, "page_size": 50}),
        maker, "req-prod-list-3",
    )
    harv_items = [r for r in ((harv.data or {}).get("items") or [])
                  if str(r.get("code")) == "E2E-HARVEST-1"]
    check("harvest.list resolves pond_name via the accessor",
          bool(harv_items) and harv_items[0].get("pond_name") == fixture_pond_name(),
          str(harv_items[0]) if harv_items else "not listed")

    # ---------------------------------------------------------------- harvest

    print("\n=== 5. invariant #2 blocks over-harvest (negative pond stock) ===")
    too_much = runner.invoke(
        Invocation(
            capability_name="harvest.create",
            payload={
                "code": "E2E-HARVEST-2", "name": "端到端超额出塘", "pond_id": 1,
                "batch_id": batch_id, "quantity": 5000,
                "happened_at": "2026-09-04 08:00:00",
            },
            idempotency_key="e2e-harvest-create-2",
        ),
        maker, "req-prod-9",
    )
    over_harvest_rejected = False
    over_harvest_message = ""
    try:
        runner.invoke(
            Invocation(capability_name="harvest.verify",
                       payload={"harvest_id": too_much.resource_id,
                                   "expected_version": version_of("harvests", too_much.resource_id)},
                       path_params={"harvest_id": too_much.resource_id}, idempotency_key="e2e-harvest-verify-over"),
            checker, "req-prod-10",
        )
    except DomainError as error:
        over_harvest_rejected = True
        over_harvest_message = error.message
    check("over-harvest was rejected", over_harvest_rejected, over_harvest_message)

    with uow_factory().begin() as tx:
        q4, _ = stock_balance(tx, batch_id)
        over_row = tx.query_one("SELECT status FROM harvests WHERE id = %s",
                                (too_much.resource_id,))
    check("stock unchanged after the rejected harvest (still 600)", str(q4) == "600.000", f"q={q4}")
    check("the rejected harvest rolled back to draft",
          str((over_row or {}).get("status")) == "draft", str(over_row))

    print("\n=== 6. invariant #11: batch.close needs zero stock ===")
    # Advance the business state machine: stocked -> farming -> pending_settlement.
    # `closed` is refused from batch.update on purpose; only batch.close may close.
    with uow_factory().begin() as tx:
        row = tx.query_one("SELECT row_version, batch_status FROM production_batches WHERE id = %s",
                           (batch_id,))
    version = int(row["row_version"])
    moved = runner.invoke(
        Invocation(capability_name="batch.update",
                   payload={"batch_id": batch_id, "expected_version": version, "batch_status": "farming"},
                   path_params={"batch_id": batch_id}, idempotency_key="e2e-batch-farming"),
        checker, "req-prod-12",
    )
    check("batch_status advanced to farming", moved.kind == "executed", moved.kind)

    # `farming -> pending_settlement` requires that the batch has no unfinished
    # production work (inherited from the old `production_store.py:225-227`,
    # BATCH_OPEN_WORK). Discard every unfinished draft HERE, before the transition:
    # sections 3b/3c legitimately leave drafts behind (a refused verify rolls the
    # document back to draft), and doing it unconditionally also makes the suite
    # re-runnable after an aborted run -- an abort used to leave rows that broke the
    # NEXT run for an unrelated-looking reason (observed: a stale draft made this
    # transition fail).
    with uow_factory().begin() as tx:
        tx.execute("DELETE FROM feedings WHERE batch_id = %s AND status = 'draft'", (batch_id,))

    settled = runner.invoke(
        Invocation(capability_name="batch.update",
                   payload={"batch_id": batch_id,
                            "expected_version": version_of("production_batches", batch_id),
                            "batch_status": "pending_settlement"},
                   path_params={"batch_id": batch_id}, idempotency_key="e2e-batch-settle"),
        checker, "req-prod-13",
    )
    check("batch_status advanced to pending_settlement once no open work remains",
          settled.kind == "executed", settled.kind)

    with uow_factory().begin() as tx:
        version = int(tx.query_one("SELECT row_version FROM production_batches WHERE id = %s",
                                   (batch_id,))["row_version"])
    close_blocked = False
    close_message = ""
    try:
        runner.invoke(
            Invocation(capability_name="batch.close", payload={"batch_id": batch_id, "expected_version": version},
                       path_params={"batch_id": batch_id}, idempotency_key="e2e-batch-close-blocked"),
            checker, "req-prod-14",
        )
    except DomainError as error:
        close_blocked = True
        close_message = error.message
    check("batch.close refused while stock is 600 (invariant #11)", close_blocked, close_message)

    print("\n=== 6b. 'no unfinished production work' guard on pending_settlement ===")
    # `farming -> pending_settlement` must be refused while an UNVERIFIED feeding
    # remains (inherited from the old `production_store.py:225-227`, BATCH_OPEN_WORK).
    # Reach it by rewinding the batch to farming and leaving a draft feeding behind.
    with uow_factory().begin() as tx:
        tx.execute("UPDATE production_batches SET batch_status='farming' WHERE id = %s",
                   (batch_id,))
        tx.execute(
            "INSERT INTO feedings (organization_id, farm_id, area_id, code, name, pond_id, "
            "batch_id, material_id, quantity, happened_at, status, created_by) "
            "VALUES (1,1,1,'E2E-FEED-DRAFT','未核验草稿',1,%s,1,10,NOW(),'draft',1)",
            (batch_id,),
        )

    guard_tripped = False
    guard_message = ""
    try:
        runner.invoke(
            Invocation(capability_name="batch.update",
                       payload={"batch_id": batch_id,
                                "expected_version": version_of("production_batches", batch_id),
                                "batch_status": "pending_settlement"},
                       path_params={"batch_id": batch_id},
                       idempotency_key="e2e-batch-settle-guarded"),
            checker, "req-prod-13c",
        )
    except DomainError as error:
        guard_tripped = True
        guard_message = error.message
    check("pending_settlement refused while a draft feeding exists",
          guard_tripped, guard_message)

    # Discard the unfinished draft ("void and reopen" -- there is deliberately no
    # feeding.update capability), then the transition must succeed.
    # Discard ALL unfinished drafts for this batch, not only the one just inserted:
    # sections 3b/3c legitimately leave drafts behind (a refused verify rolls the
    # document back to draft), and "no open work" is a hard precondition here.
    # Unconditional disposal also makes the suite re-runnable after an aborted run --
    # a real case, since an abort used to leave rows that then broke the NEXT run for
    # an unrelated-looking reason.
    with uow_factory().begin() as tx:
        tx.execute("DELETE FROM feedings WHERE batch_id = %s AND status = 'draft'", (batch_id,))
    settled_after = runner.invoke(
        Invocation(capability_name="batch.update",
                   payload={"batch_id": batch_id,
                            "expected_version": version_of("production_batches", batch_id),
                            "batch_status": "pending_settlement"},
                   path_params={"batch_id": batch_id},
                   idempotency_key="e2e-batch-settle-after"),
        checker, "req-prod-13d",
    )
    check("pending_settlement succeeds once no open work remains",
          settled_after.kind == "executed", settled_after.kind)

    print("\n=== 7. the full closure chain: harvest to zero, then close ===")
    remaining = runner.invoke(
        Invocation(
            capability_name="harvest.create",
            payload={
                "code": "E2E-HARVEST-3", "name": "端到端清塘出塘", "pond_id": 1,
                "batch_id": batch_id, "quantity": 600, "weight_kg": 60,
                "happened_at": "2026-09-05 08:00:00",
            },
            idempotency_key="e2e-harvest-create-3",
        ),
        maker, "req-prod-15",
    )
    runner.invoke(
        Invocation(capability_name="harvest.verify",
                   payload={"harvest_id": remaining.resource_id,
                               "expected_version": version_of("harvests", remaining.resource_id)},
                   path_params={"harvest_id": remaining.resource_id}, idempotency_key="e2e-harvest-verify-final"),
        checker, "req-prod-16",
    )
    with uow_factory().begin() as tx:
        q5, w5 = stock_balance(tx, batch_id)
        version = int(tx.query_one("SELECT row_version FROM production_batches WHERE id = %s",
                                   (batch_id,))["row_version"])
    check("stock is now exactly zero", str(q5) == "0.000" and str(w5) == "0.000", f"q={q5} w={w5}")

    closed = runner.invoke(
        Invocation(capability_name="batch.close", payload={"batch_id": batch_id, "expected_version": version},
                   path_params={"batch_id": batch_id}, idempotency_key="e2e-batch-close-final"),
        checker, "req-prod-17",
    )
    check("batch.close executed once stock reached zero", closed.kind == "executed", closed.kind)

    with uow_factory().begin() as tx:
        final_status = tx.query_one(
            "SELECT batch_status, status FROM production_batches WHERE id = %s", (batch_id,)
        )
    check("batch is closed and verified",
          str(final_status["batch_status"]) == "closed" and str(final_status["status"]) == "verified",
          str(final_status))

    # Clean up AFTER the assertions. ROLLOUT_CONTRACT Sec 5 requires the shared
    # database and says "probe data must be cleaned when done -- an unclean probe is
    # a defect, not something to dodge with a separate database". Cleaning only at
    # the START of a run (which is all this suite did originally) leaves every run's
    # final state behind, so the next suite -- or a human -- sees rows that look like
    # real data. The cleanup is scoped to `E2E-` probes so it cannot touch another
    # domain's in-flight rows.
    with uow_factory().begin() as tx:
        clean(tx)

    print()
    if FAILURES == 0:
        print("结果：全部通过")
        return 0
    print(f"结果：{FAILURES} 项失败")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
