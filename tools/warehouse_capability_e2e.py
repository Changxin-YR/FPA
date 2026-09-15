"""warehouse 能力层验收：真实 CapabilityRunner × 声明式不变量（真实 MySQL）。

## 为什么这个文件独立于 `warehouse_e2e.py`

`warehouse_e2e.py` 的账本层用例（A–G 段）只需要 `UnitOfWork` + `ledger.py`，
不依赖任何域的能力声明，因此永远是可跑的。但它的 H 段（能力流）必须拿到
**真实注册表**，而 `fpa.bootstrap.load_all()` 会 import **全部**域——
任何一个 peer 域处于半编辑状态（实测过一次：sales 的 `service.py` 在导入期抛
`NameError`）都会把整个 `load_all()` 拖下水，于是本域的能力层验收**被他人的
进行中改动弄成红**。那种红不是我的缺陷，却和我的缺陷长得一样。

所以这里只装载本域及其真实依赖（master_data / cost / production / purchase / warehouse），
并把缺失的域**显式报告**出来（空集不是通过）。代价要说清楚：**sales 不在装载范围内**，
所以本文件不声称覆盖 sales。

## 证明什么

    1. registry §1.6 的 18 条能力真的经组合根注册
    2. 写能力的 `__fpa_load_by_id__` 无需夹具补挂（ROLLOUT_CONTRACT §3 的模板债）
    3. §4 该挂的 7 类不变量都挂在正确的能力上，且 `PeriodOpen.date_field` 覆盖正确
    4. **反例（不变量真的在拦）**：#1 负库存、#5 自审、#7 关账期间、#19 重复编码
    5. **跨域**：`receipt.verify` 真的推进采购单并生成应付（直接查采购域的表）

用法::

    $env:MYSQL_DATABASE='fpa'; $env:MYSQL_PASSWORD='fpa_dev_password'
    python tools/warehouse_capability_e2e.py
"""

from __future__ import annotations

import datetime
import importlib
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import fpa.kernel.capability as cap  # noqa: E402
from fpa.kernel.audit import AuditWriter  # noqa: E402
from fpa.kernel.errors import DomainError  # noqa: E402
from fpa.kernel.idempotency import IdempotencyStore  # noqa: E402
from fpa.kernel.runner import ActorView, CapabilityRunner, Invocation  # noqa: E402
from fpa.kernel.scope import Scope  # noqa: E402
from fpa.kernel.uow import ConnectionConfig, UnitOfWork  # noqa: E402

D = lambda v: Decimal(str(v))  # noqa: E731

#: 本域真实依赖的域。**逐条列出**而不是 `load_all()`：见模块头说明。
DOMAINS = ("master_data", "cost", "production", "purchase", "warehouse")

#: 每轮唯一后缀（幂等键与单据号跨轮复用会走回放路径，读到已被清理的行）。
RUN = datetime.datetime.now().strftime("%H%M%S%f")[:8]
PREFIX = "WHCAP-"

_PASS: list[str] = []
_FAIL: list[str] = []


def check(label: str, condition: bool, extra: object = "") -> None:
    (_PASS if condition else _FAIL).append(label)
    mark = "  PASS  " if condition else "  FAIL  "
    print(mark + label + ((f"  | {extra}") if extra != "" else ""))


def config() -> ConnectionConfig:
    return ConnectionConfig(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "fpa"),
        password=os.environ.get("MYSQL_PASSWORD", "fpa_dev_password"),
        database=os.environ.get("MYSQL_DATABASE", "fpa"),
    )


def new_uow() -> UnitOfWork:
    return UnitOfWork(config())


class _FixedScopeResolver:
    """把 DataScope 钉到区域 1（真实部署从 `user_data_scopes` 解析）。"""

    def resolve(self, *, user_id: int, role_codes: frozenset[str]) -> Scope:
        return Scope.from_rows([{"scope_type": "area", "area_id": 1}], user_id=user_id)


MAKER_PERMISSIONS = frozenset({
    "warehouse.view", "inventory.view", "warehouse.receipt.create",
    "warehouse.receipt.verify", "warehouse.issue.create", "warehouse.issue.verify",
})
CHECKER_PERMISSIONS = frozenset({
    "warehouse.view", "inventory.view",
    "warehouse.receipt.verify", "warehouse.issue.verify",
})


def load_domains() -> None:
    """装载本域及其真实依赖；缺失/损坏的域**显式报告**（不静默跳过）。"""
    broken = []
    for domain in DOMAINS:
        module = f"fpa.domains.{domain}.capabilities"
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{domain} ({type(exc).__name__}: {exc})")
    if broken:
        print("!! 下列域导入失败，本次运行**不是**完整证明：")
        for item in broken:
            print("!!   - " + item)
        print()


def resolve_seeds() -> tuple[int, int, int]:
    """按 code 解析仓库 / 物料 id —— **不写死自增主键**。"""
    with new_uow().begin() as tx:
        wh = tx.query_one("SELECT id FROM warehouses WHERE code = %s", ("WH-001",))
        mat = tx.query_one("SELECT id FROM materials WHERE code = %s", ("MAT-001",))
        actor = tx.query_scalar("SELECT MIN(id) FROM users")
    if wh is None or mat is None:
        raise SystemExit("种子缺失：006 的 WH-001 或 003 的 MAT-001 不存在，先跑 migrate apply")
    return int(wh["id"]), int(mat["id"]), int(actor)


def main() -> int:
    print("=" * 74)
    print("warehouse 能力层验收（真实 CapabilityRunner）")
    print("=" * 74)
    load_domains()

    warehouse_id, material_id, actor_id = resolve_seeds()
    print(f"  种子：warehouse_id={warehouse_id} material_id={material_id} actor_id={actor_id}")

    registry = cap.REGISTRY
    produced = sorted(c.name for c in registry.all() if c.domain == "warehouse")
    # registry §1.6 已在 2026-09-14 之后扩到 18 条：除了最初的 7 条，还包含
    # warehouse 详情/创建/编辑/提交/核验/停用、仓储单据详情、库存批次详情、
    # 领用出库列表与详情。逐条对账而不是"至少包含 7 条"，是为了拦住**多出未登记能力**
    # 这种更隐蔽的漂移。
    expected = sorted([
        "inventory.ledger",
        "inventory.list",
        "inventory_lot.get",
        "issue.create",
        "issue.get",
        "issue.list",
        "issue.verify",
        "receipt.create",
        "receipt.verify",
        "warehouse.archive",
        "warehouse.create",
        "warehouse.get",
        "warehouse.list",
        "warehouse.submit",
        "warehouse.update",
        "warehouse.verify",
        "warehouse_document.get",
        "warehouse_document.list",
    ])
    check("注册的正是 registry §1.6 的 18 条能力", produced == expected, produced)

    # ---- 模板债守卫（ROLLOUT_CONTRACT §3） ----
    missing_loader = [
        c.name for c in registry.all()
        if c.domain == "warehouse" and c.is_write
        and getattr(c.handler, "__fpa_load_by_id__", None) is None
    ]
    check("写能力无需夹具补挂即可回读（__fpa_load_by_id__ 来自 handler 本身）",
          missing_loader == [], f"missing: {missing_loader}")

    # ---- 不变量覆盖 ----
    declared: dict[str, set[str]] = {
        c.name: {type(i).__name__ for i in c.invariants}
        for c in registry.all() if c.domain == "warehouse"
    }
    for capability, invariant in (
        ("issue.verify", "NoNegativeStock"),
        ("receipt.verify", "CumulativeWithin"),
        ("receipt.create", "ReferencedStatus"),
    ):
        check(f"{capability} 声明了 {invariant}", invariant in declared.get(capability, set()),
              declared.get(capability))
    check("receipt.verify / issue.verify 都声明了 DistinctActors 与 PeriodOpen",
          all({"DistinctActors", "PeriodOpen"} <= declared.get(n, set())
              for n in ("receipt.verify", "issue.verify")))
    check("receipt.create / issue.create 都声明了 SameTenant 与 UniqueCode",
          all({"SameTenant", "UniqueCode"} <= declared.get(n, set())
              for n in ("receipt.create", "issue.create")))

    from fpa.kernel.invariants import PeriodOpen
    date_fields = {
        i.date_field for c in registry.all() if c.domain == "warehouse"
        for i in c.invariants if isinstance(i, PeriodOpen)
    }
    check('PeriodOpen 覆盖为 date_field="happened_at"（不是默认 occurred_on）',
          date_fields == {"happened_at"}, date_fields)

    runner = CapabilityRunner(
        registry=registry, uow_factory=new_uow, audit=AuditWriter(),
        idempotency=IdempotencyStore(new_uow), scope_resolver=_FixedScopeResolver(),
    )
    maker = ActorView(user_id=actor_id, username="wh_cap_maker", permissions=MAKER_PERMISSIONS)
    checker = ActorView(user_id=actor_id + 1, username="wh_cap_checker",
                        permissions=CHECKER_PERMISSIONS)

    def invoke(name, *, actor=maker, payload=None, path=None, key=None):
        payload = dict(payload or {})
        payload.update(path or {})  # 路径参数同时进 payload：校验器只看 payload
        return runner.invoke(
            Invocation(capability_name=name, payload=payload, path_params=path or {},
                       query={}, idempotency_key=key),
            actor=actor, request_id="wh-cap",
        )

    def cleanup(lots: list[str], codes: list[str]) -> None:
        with new_uow().begin() as tx:
            for lot in lots:
                tx.execute("DELETE FROM inventory_ledger WHERE lot_no = %s", (lot,))
                tx.execute("DELETE FROM inventory_lots WHERE lot_no = %s", (lot,))
            for code in codes:
                tx.execute(
                    "DELETE FROM warehouse_document_lines WHERE document_id IN "
                    "(SELECT id FROM warehouse_documents WHERE code = %s)", (code,)
                )
                tx.execute("DELETE FROM warehouse_documents WHERE code = %s", (code,))
            tx.execute("DELETE FROM purchase_payables WHERE purchase_order_id IN "
                       "(SELECT id FROM purchase_orders WHERE code LIKE %s)", (PREFIX + "%",))
            tx.execute("DELETE FROM purchase_orders WHERE code LIKE %s", (PREFIX + "%",))
            tx.execute("DELETE FROM business_partners WHERE code LIKE %s", (PREFIX + "%",))
            tx.execute("DELETE FROM accounting_periods WHERE period = '2026-09' "
                       "AND organization_id = 1")

    lot = f"{PREFIX}LOT-{RUN}"
    lot_fe = f"{PREFIX}FELOT-{RUN}"
    rcv_code = f"{PREFIX}RCV-{RUN}"
    iss_code = f"{PREFIX}ISS-{RUN}"
    fe_cost = f"{PREFIX}FEC-{RUN}"
    fe_rcv = f"{PREFIX}FERC-{RUN}"
    ovr_code = f"{PREFIX}OVR-{RUN}"
    ovr_lot = f"{PREFIX}OVRLOT-{RUN}"
    cleanup([lot, lot_fe, f"{PREFIX}NNS-{RUN}", ovr_lot],
            [rcv_code, iss_code, fe_cost, fe_rcv, ovr_code, f"{PREFIX}LK-{RUN}"])

    try:
        # ---- 只读能力 ----
        check("warehouse.list 可调用", invoke("warehouse.list", actor=checker).data["total"] >= 1)
        check("inventory.list 可调用", "items" in invoke("inventory.list", actor=checker).data)
        check("inventory.ledger 可调用", "items" in invoke("inventory.ledger", actor=checker).data)

        # ---- #1 不得负库存（真实反例） ----
        print()
        print("  -- #1 不得负库存（issue.verify）：反例 --")
        rcv = invoke("receipt.create", payload={
            "code": rcv_code, "name": "验收到货", "warehouse_id": warehouse_id,
            "material_id": material_id, "lot_no": lot, "quantity": 10,
            "unit_cost": 4.8, "happened_at": "2026-08-01 08:00:00",
        }, key=f"{PREFIX}K1-{RUN}")
        invoke("receipt.verify", payload={"expected_version": 1},
               path={"receipt_id": rcv.resource_id}, actor=checker, key=f"{PREFIX}K2-{RUN}")
        issue = invoke("issue.create", payload={
            "code": iss_code, "name": "超量领用", "warehouse_id": warehouse_id,
            "material_id": material_id, "lot_no": lot, "quantity": 999,
            "happened_at": "2026-08-02 08:00:00",
        }, key=f"{PREFIX}K3-{RUN}")
        rejected = None
        try:
            invoke("issue.verify", payload={"expected_version": 1},
                   path={"issue_id": issue.resource_id}, actor=checker, key=f"{PREFIX}K4-{RUN}")
        except DomainError as error:
            rejected = error
        check("超量出库被 NoNegativeStock 拒绝",
              rejected is not None and rejected.code == "CONFLICT",
              f"{rejected.code} / {rejected.message}" if rejected else "没有抛错")
        check("拒绝信息来自**写入后余额**（新版形态不接受调用方做算术）",
              rejected is not None and "写入后余额" in rejected.message,
              rejected.message if rejected else "")
        check("错误 data 带规则名与分组，供前端定位",
              rejected is not None and rejected.data.get("rule") == "NO_NEGATIVE_STOCK",
              rejected.data if rejected else "")
        with new_uow().begin() as tx:
            bal = tx.query_scalar(
                "SELECT COALESCE(SUM(quantity_delta),0) FROM inventory_ledger WHERE lot_no=%s",
                (lot,))
            doc = tx.query_one("SELECT status FROM warehouse_documents WHERE id=%s",
                               (issue.resource_id,))
        check("不变量失败后账本零写入（余额仍 10）", D(bal) == D(10), bal)
        check("出库单仍为 draft（整体回滚，没有半成品）", str(doc["status"]) == "draft",
              doc["status"])

        # ---- #5 经办人 ≠ 审批人 ----
        print()
        print("  -- #5 经办人 ≠ 审批人：反例 --")
        with new_uow().begin() as tx:
            doc_id = tx.query_scalar("SELECT id FROM warehouse_documents WHERE code=%s",
                                     (f"{PREFIX}LK-{RUN}",))
        if doc_id is None:
            lk = invoke("receipt.create", payload={
                "code": f"{PREFIX}LK-{RUN}", "name": "自审探针", "warehouse_id": warehouse_id,
                "material_id": material_id, "lot_no": lot, "quantity": 1,
                "unit_cost": 1, "happened_at": "2026-08-03 08:00:00",
            }, key=f"{PREFIX}K5-{RUN}")
            doc_id = lk.resource_id
        self_approve = None
        try:
            invoke("receipt.verify", payload={"expected_version": 1},
                   path={"receipt_id": int(doc_id)}, actor=maker, key=f"{PREFIX}K6-{RUN}")
        except DomainError as error:
            self_approve = error
        check("经办人核验自己登记的单据被拒（DistinctActors）",
              self_approve is not None and self_approve.code == "FORBIDDEN",
              f"{self_approve.code} / {self_approve.message}" if self_approve else "没有抛错")

        # ---- #7 已关账期间 ----
        print()
        print("  -- #7 已关账期间不得入账：反例 --")
        with new_uow().begin() as tx:
            tx.execute(
                "INSERT INTO accounting_periods (organization_id, period, period_start,"
                " period_end, status, closed_by, closed_at, close_reason, created_by)"
                " VALUES (1,'2026-09','2026-09-01','2026-09-30','closed',1,NOW(),'cap e2e 关账',1)"
                " ON DUPLICATE KEY UPDATE status='closed', closed_by=1, closed_at=NOW(),"
                " close_reason='cap e2e 关账'")
        fec = invoke("receipt.create", payload={
            "code": fe_cost, "name": "关账期间到货", "warehouse_id": warehouse_id,
            "material_id": material_id, "lot_no": lot_fe, "quantity": 1,
            "unit_cost": 1, "happened_at": "2026-09-15 08:00:00",
        }, key=f"{PREFIX}K7-{RUN}")
        closed_period = None
        try:
            invoke("receipt.verify", payload={"expected_version": 1},
                   path={"receipt_id": fec.resource_id}, actor=checker, key=f"{PREFIX}K8-{RUN}")
        except DomainError as error:
            closed_period = error
        check("已关账期间核验被拒（PeriodOpen）",
              closed_period is not None and closed_period.code == "CONFLICT"
              and closed_period.data and closed_period.data.get("rule") == "PERIOD_CLOSED",
              f"{closed_period.code} / {closed_period.message}" if closed_period else "没有抛错")

        with new_uow().begin() as tx:
            tx.execute("UPDATE accounting_periods SET status='open', closed_by=NULL,"
                       " closed_at=NULL, close_reason=NULL WHERE period='2026-09'")
        opened = invoke("receipt.verify", payload={"expected_version": 1},
                        path={"receipt_id": fec.resource_id}, actor=checker,
                        key=f"{PREFIX}K9-{RUN}")
        check("期间打开后同一张单据核验通过（证明拦截来自期间而非其它原因）",
              opened.kind == "executed", opened.kind)

        # ---- #19 编码唯一 ----
        print()
        print("  -- #19 编码唯一：反例 --")
        dup = None
        try:
            invoke("receipt.create", payload={
                "code": rcv_code, "name": "重复编号", "warehouse_id": warehouse_id,
                "material_id": material_id, "lot_no": lot, "quantity": 1,
                "unit_cost": 1, "happened_at": "2026-08-04 08:00:00",
            }, key=f"{PREFIX}KA-{RUN}")
        except DomainError as error:
            dup = error
        check("同一编号重复登记被拒为 CONFLICT（不是裸 IntegrityError/500）",
              dup is not None and dup.code == "CONFLICT",
              f"{dup.code} / {dup.message}" if dup else "没有抛错")

        # ---- 跨域：真的推进采购单并生成应付 ----
        print()
        print("  -- 跨域：receipt.verify -> 采购单状态 + 应付 --")
        po_code = f"{PREFIX}PO-{RUN}"
        sup_code = f"{PREFIX}SUP-{RUN}"
        with new_uow().begin() as tx:
            tx.execute("INSERT INTO business_partners (organization_id,farm_id,area_id,"
                       "partner_type,code,name,status,created_by)"
                       " VALUES (1,1,1,'supplier',%s,'cap 供应商','verified',1)", (sup_code,))
            supplier_id = tx.last_insert_id()
            tx.execute("INSERT INTO purchase_orders (organization_id,farm_id,area_id,code,name,"
                       "supplier_id,material_id,warehouse_id,quantity,unit_price,"
                       "expected_delivery_date,due_date,status,created_by)"
                       " VALUES (1,1,1,%s,'cap 采购单',%s,%s,%s,100,4.8,'2026-08-20','2026-08-31',"
                       "'approved',1)", (po_code, supplier_id, material_id, warehouse_id))
            po_id = tx.last_insert_id()
        linked = invoke("receipt.create", payload={
            "code": fe_rcv, "name": "关联采购单到货", "warehouse_id": warehouse_id,
            "material_id": material_id, "lot_no": f"{PREFIX}NNS-{RUN}", "quantity": 40,
            "unit_cost": 4.8, "happened_at": "2026-08-05 08:00:00", "purchase_order_id": po_id,
        }, key=f"{PREFIX}KB-{RUN}")
        invoke("receipt.verify", payload={"expected_version": 1},
               path={"receipt_id": linked.resource_id}, actor=checker, key=f"{PREFIX}KC-{RUN}")
        with new_uow().begin() as tx:
            po_status = tx.query_scalar("SELECT status FROM purchase_orders WHERE id=%s", (po_id,))
            payables = tx.query_all("SELECT total_amount FROM purchase_payables "
                                    "WHERE purchase_order_id=%s", (po_id,))
            header = tx.query_one("SELECT total_quantity FROM warehouse_documents WHERE id=%s",
                                  (linked.resource_id,))
        check("采购单被推进到 partially_received（40/100）",
              str(po_status) == "partially_received", po_status)
        check("生成了应付且金额 = 40 × 4.8 = 192",
              len(payables) == 1 and D(payables[0]["total_amount"]) == D("192.00"), payables)
        check("到货单单头累计列由服务端从明细回算（40）",
              D(header["total_quantity"]) == D(40), header)

        # ---- §4 #9 累计到货不得超过采购数量（真实反例，且要**第二条**到货才暴露） ----
        #
        # 为什么必须用第二条到货：采购单 100。第一条到 40 时它是合法的，
        # `ReferencedStatus` 也过（订单还是 partially_received）。
        # 第二条再到 100 → 累计 140 > 100，此刻才该由 #9 说话。
        #
        # 这条用例抓过一个真实的可用性缺陷：`apply_receipt` 会把采购单推进到
        # `fully_received`，而执行器是"**服务先写、不变量后跑**"，于是
        # `ReferencedStatus` 看到的是**写入后**的状态、抢先报
        # 「采购单当前状态为 fully_received，不允许本次操作」——同一个拒绝，
        # 文案却指向"状态不对"（用户会去查状态，而状态没错），掩盖了真实原因
        # "到多了"。修法是把 `CumulativeWithin` 排到 `ReferencedStatus` 之前。
        print()
        print("  -- #9 累计到货不得超过采购数量：反例（第二条到货） --")
        ovr = invoke("receipt.create", payload={
            "code": ovr_code, "name": "超量到货", "warehouse_id": warehouse_id,
            "material_id": material_id, "lot_no": ovr_lot, "quantity": 100, "unit_cost": 4.8,
            "happened_at": "2026-08-06 08:00:00", "purchase_order_id": po_id,
        }, key=f"{PREFIX}KD-{RUN}")
        over = None
        try:
            invoke("receipt.verify", payload={"expected_version": 1},
                   path={"receipt_id": ovr.resource_id}, actor=checker, key=f"{PREFIX}KE-{RUN}")
        except DomainError as error:
            over = error
        check("第二条到货使累计超量，被拒为 CONFLICT",
              over is not None and over.code == "CONFLICT",
              f"{over.code} / {over.message}" if over else "没有抛错")
        check("拒绝由 **#9 累计规则**说话（不是被状态规则抢先掩盖真实原因）",
              over is not None and over.data and over.data.get("rule") == "CUMULATIVE_WITHIN",
              over.data if over else "")
        check("拒绝文案给出真实数字（已到 40、本次 100、合计 140、上限 100）",
              over is not None and all(x in over.message for x in ("40", "100", "140")),
              over.message if over else "")
        with new_uow().begin() as tx:
            po_after = tx.query_scalar("SELECT status FROM purchase_orders WHERE id=%s",
                                       (po_id,))
            ovr_doc = tx.query_one("SELECT status FROM warehouse_documents WHERE id=%s",
                                   (ovr.resource_id,))
            ovr_led = tx.query_scalar("SELECT COUNT(*) FROM inventory_ledger WHERE lot_no=%s",
                                      (ovr_lot,))
        check("超量被拒后采购单未被越界请求改动（仍 partially_received）",
              str(po_after) == "partially_received", po_after)
        check("超量到货单仍为 draft（整体回滚）", str(ovr_doc["status"]) == "draft",
              ovr_doc["status"])
        check("超量请求零账本写入", int(ovr_led) == 0, ovr_led)
    finally:
        cleanup([lot, lot_fe, f"{PREFIX}NNS-{RUN}", ovr_lot],
                [rcv_code, iss_code, fe_cost, fe_rcv, ovr_code, f"{PREFIX}LK-{RUN}"])
        print()
        print("  探针已清理（含 lot / 单据 / 采购单 / 应付 / 期间）")

    print()
    print("=" * 74)
    print(f"结果：{len(_PASS)} 项通过，{len(_FAIL)} 项失败")
    for label in _FAIL:
        print("  FAILED: " + label)
    print("=" * 74)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
