"""warehouse 域（t12 部分）验收：006 迁移 + `apply_movement` 跨域唯一入口。

用法::

    $env:MYSQL_ROOT_PASSWORD='1234'
    $env:MYSQL_PASSWORD='yuxin_dev_password'
    $env:MYSQL_DATABASE='yuxin_wh'
    python tools/migrate.py apply     # 先建库建表
    python tools/warehouse_e2e.py

本文件只测**账本层**（`UnitOfWork` + `ledger.py`），因此**不 import 任何业务域**，
永远可跑：它断言的是账本原语的语义——幂等建批 / 唯一键 / FEFO / DataScope /
入参纪律，以及**行锁的串行化**。

并发那一段是本次最重要的产出：它在开发过程中**抓到了一个真实的负库存缺陷**
（快照读 vs 锁定读，见 `backend/yuxin/domains/warehouse/ledger.py::_lot_balance`
的注释与实测时间线）。单线程测试永远看不到它，所以它必须常驻。

**能力层（声明式不变量真的在拦）不在这里**，那需要一个真实注册表，而
`bootstrap.load_all()` 会 import 全部域——任何 peer 域半编辑（实测过一次 sales
在导入期抛 `NameError`）都会把本文件染红，那种红不是本域的缺陷。所以能力层验收
独立成 `tools/warehouse_capability_e2e.py`（只装载本域的真实依赖）。

### 判定归属（评审结论 ④）

账本原语**不判定负库存**，只做"幂等建批 + 加锁 + 写账本行"；判定由
`NoNegativeStock` 在能力层强制。所以这里的并发用例断言的是**串行化**
（第二个事务必须读到第一个提交后的余额），而"会不会变成负"由能力层用例断言。
"""

from __future__ import annotations

import datetime
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from yuxin.domains.warehouse.ledger import (  # noqa: E402
    apply_movement,
    ledger_source_ref,
    resolve_issue_lot,
)
from yuxin.kernel.errors import DomainError  # noqa: E402
from yuxin.kernel.scope import Scope, ScopeEntry, ScopeType  # noqa: E402
from yuxin.kernel.uow import ConnectionConfig, UnitOfWork  # noqa: E402

D = lambda v: Decimal(str(v))  # noqa: E731

#: 探针行的前缀。契约（ROLLOUT_CONTRACT §5）要求：并发靠**探针数据 + 清理隔离**，
#: 不靠分库；每个 e2e 用自己前缀的行，结束时删干净。
PREFIX = "WHE2E-"

#: 每轮运行的唯一后缀。
#:
#: 为什么必须有：幂等键（`Idempotency-Key`）会**持久化**在 `idempotency_keys` 表里，
#: 而清理探针行时并不会清它。若两轮用同一个键，第二轮会走**回放路径**
#: （`_replay`），去读第一轮已被删掉的那一行，于是报"仓储单据不存在"——
#: 一个纯粹由测试自身造成的假失败。
#: 单据号后缀同时保证 `UniqueCode`（§4 #19）不会被上一轮的同号单据撞到。
RUN = datetime.datetime.now().strftime("%H%M%S%f")[:10]

#: 探针批次号在 `main()` 里按前缀赋值（见 `_bootstrap` 上方的说明）
_LOT_LATE = ""
_LOT_SOON = ""
_LOT_RACE = ""

#: 这些 id 由 `_bootstrap()` 在运行时**按 code 解析**，不写死。
#: 写死自增主键在共享库（`yuxin`，已有各域的种子与探针数据）里必然失效——
#: 别的域插一行就能让 `warehouse_id=1` 指向别的东西。
WAREHOUSE = 0
MATERIAL = 0
ACTOR = 0
WAREHOUSE_CODE = "WH-001"
MATERIAL_CODE = "MAT-001"

_PASS: list[str] = []
_FAIL: list[str] = []


def check(label: str, condition: bool, extra: object = "") -> bool:
    (_PASS if condition else _FAIL).append(label)
    mark = "  PASS  " if condition else "  FAIL  "
    print(mark + label + ((f"  | {extra}") if extra != "" else ""))
    return condition


def config() -> ConnectionConfig:
    import os

    return ConnectionConfig(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "yuxin"),
        password=os.environ.get("MYSQL_PASSWORD", "yuxin_dev_password"),
        database=os.environ.get("MYSQL_DATABASE", "yuxin_wh"),
        read_timeout=60,
        write_timeout=60,
    )


def new_uow() -> UnitOfWork:
    """每个事务一个新实例——`UnitOfWork` 复用会显式抛错（设计如此）。"""
    return UnitOfWork(config())


def scope_for(area_id: int) -> Scope:
    return Scope(
        allow_all=False,
        entries=(ScopeEntry(scope_type=ScopeType.AREA, bound_id=area_id),),
        user_id=ACTOR,
    )


def _clean(tx, lot_no: str) -> None:
    tx.execute("DELETE FROM inventory_ledger WHERE lot_no = %s", (lot_no,))
    tx.execute("DELETE FROM inventory_lots WHERE lot_no = %s", (lot_no,))


def _bootstrap() -> None:
    """按 code 解析出本 e2e 要用的仓库 / 物料 / 操作者 id。

    **不写死 id**：本仓已统一到共享库 `yuxin`（ROLLOUT_CONTRACT §5），
    五个域的种子与探针数据都在里面，任何"id=1"的假设都会随别人的一次插入失效。
    这一段同时把"本域迁移的种子是否真的落库"变成一条断言。
    """
    global WAREHOUSE, MATERIAL, ACTOR
    with new_uow().begin() as tx:
        row = tx.query_one(
            "SELECT id, name, status, is_default FROM warehouses WHERE code = %s",
            (WAREHOUSE_CODE,),
        )
        if row is None:
            raise SystemExit(
                f"仓库 {WAREHOUSE_CODE} 不存在——006_warehouse.sql 的种子没有落库，"
                "请先跑 python tools/migrate.py apply"
            )
        WAREHOUSE = int(row["id"])
        print(f"  仓库  {WAREHOUSE_CODE} -> id={WAREHOUSE} ({row['name']}, "
              f"status={row['status']}, is_default={row['is_default']})")

        row = tx.query_one(
            "SELECT id, name, status FROM materials WHERE code = %s", (MATERIAL_CODE,)
        )
        if row is None:
            raise SystemExit(f"物料 {MATERIAL_CODE} 不存在——003_master_data.sql 的种子缺失")
        MATERIAL = int(row["id"])
        print(f"  物料  {MATERIAL_CODE} -> id={MATERIAL} ({row['name']}, status={row['status']})")

        actor = tx.query_scalar("SELECT MIN(id) FROM users")
        ACTOR = int(actor)
        print(f"  操作者 id={ACTOR}")


# ===========================================================================
# 一、账本语义
# ===========================================================================


def verify_ledger() -> None:
    scope = scope_for(1)

    print("=" * 72)
    print("A. 入库：幂等建批 + 写账本")
    print("=" * 72)
    with new_uow().begin() as tx:
        _clean(tx, _LOT_LATE)
        _clean(tx, _LOT_SOON)

    with new_uow().begin() as tx:
        m1 = apply_movement(
            tx, scope=scope, actor_id=ACTOR,
            source_type="receipt", source_ref=ledger_source_ref("warehouse", "RCV-E2E-001"),
            source_line_no=1, warehouse_id=WAREHOUSE, material_id=MATERIAL,
            lot_no=_LOT_LATE, quantity_delta=100, happened_at="2026-01-05 09:00:00",
            create_lot=True, expiry_date=datetime.date(2026, 12, 31), unit_cost="4.80",
        )
    check("入库后余额 = 100", m1.balance_after == D(100), m1.balance_after)
    check("balance_before = 0（持锁下读出的前置余额）", m1.balance_before == D(0), m1.balance_before)
    check("账本行已落库且带主键", m1.ledger_id > 0)

    with new_uow().begin() as tx:
        m2 = apply_movement(
            tx, scope=scope, actor_id=ACTOR,
            source_type="receipt", source_ref=ledger_source_ref("warehouse", "RCV-E2E-002"),
            source_line_no=1, warehouse_id=WAREHOUSE, material_id=MATERIAL,
            lot_no=_LOT_LATE, quantity_delta=50, happened_at="2026-01-06 09:00:00",
            create_lot=True, expiry_date=datetime.date(2026, 12, 31), unit_cost="4.80",
        )
    check("同一批次号二次入库复用同一 lot（幂等建批）", m2.lot_id == m1.lot_id,
          f"{m1.lot_id} vs {m2.lot_id}")
    check("二次入库余额累计 = 150", m2.balance_after == D(150), m2.balance_after)

    with new_uow().begin() as tx:
        n = tx.query_scalar("SELECT COUNT(*) AS n FROM inventory_lots WHERE lot_no = %s",
                            (_LOT_LATE,))
    check("批次表该批次只有 1 行（幂等建批没有产生重复行）", int(n) == 1, n)

    print()
    print("=" * 72)
    print("B. 唯一键：同一来源行的重复追加必须被拦住")
    print("=" * 72)
    dup = None
    try:
        with new_uow().begin() as tx:
            apply_movement(
                tx, scope=scope, actor_id=ACTOR,
                source_type="receipt", source_ref=ledger_source_ref("warehouse", "RCV-E2E-002"),
                source_line_no=1, warehouse_id=WAREHOUSE, material_id=MATERIAL,
                lot_no=_LOT_LATE, quantity_delta=50, happened_at="2026-01-06 09:00:00",
                create_lot=True, expiry_date=datetime.date(2026, 12, 31),
            )
    except DomainError as error:
        dup = error
    check("重复来源行被拒绝（uq_inventory_ledger_source_line 生效）", dup is not None,
          f"{dup.code} / {dup.message}" if dup else "没有抛错")
    check("重复被翻译成 CONFLICT(409) 而不是裸 IntegrityError(500)",
          dup is not None and dup.code == "CONFLICT",
          str(dup.code) if dup else "")

    with new_uow().begin() as tx:
        total = tx.query_scalar(
            "SELECT COALESCE(SUM(quantity_delta),0) FROM inventory_ledger WHERE lot_no=%s",
            (_LOT_LATE,),
        )
    check("重复尝试没有改变账本合计（仍为 150）", D(total) == D(150), total)

    print()
    print("  反例：不同来源域的同号单据必须**不**互撞——这正是不拿自增主键做 source_ref 的理由")
    print("        （warehouse 的单据 5 与 production 的投喂单 5 是两个 id 空间）")
    with new_uow().begin() as tx:
        m3 = apply_movement(
            tx, scope=scope, actor_id=ACTOR,
            source_type="issue", source_ref=ledger_source_ref("feeding", "RCV-E2E-002"),
            source_line_no=1, warehouse_id=WAREHOUSE, material_id=MATERIAL,
            lot_no=_LOT_LATE, quantity_delta=-10, happened_at="2026-01-07 09:00:00",
        )
    check("feeding:RCV-E2E-002 与 warehouse:RCV-E2E-002 不冲突", m3.ledger_id > 0, m3.ledger_id)
    check("出库 10 后余额 = 140", m3.balance_after == D(140), m3.balance_after)

    print()
    print("=" * 72)
    print("C. 不得负库存（registry §4 #1）")
    print("=" * 72)
    # ★ 判定归属（评审结论 ④）：`apply_movement` **不判定**负库存，它只做
    #   "幂等建批 + 加锁 + 写账本行"。判定由声明式不变量 `NoNegativeStock` 在能力层
    #   强制（H 段有真实反例）。所以这里断言的是**原语如实回报**，而不是它拒绝。
    with new_uow().begin() as tx:
        over = apply_movement(
            tx, scope=scope, actor_id=ACTOR,
            source_type="issue", source_ref=ledger_source_ref("feeding", "FEED-OVER"),
            source_line_no=1, warehouse_id=WAREHOUSE, material_id=MATERIAL,
            lot_no=_LOT_LATE, quantity_delta=-1000, happened_at="2026-01-08 09:00:00",
        )
    check("账本原语如实写负向移动并回报结存（判定不在这一层）",
          over.balance_after == D(-860), over.balance_after)
    check("回报的 balance_before 是持锁读出的真实前置余额（140）",
          over.balance_before == D(140), over.balance_before)

    # 把这笔"本不该被写下"的移动撤掉，恢复后续用例的前置状态
    with new_uow().begin() as tx:
        tx.execute(
            "DELETE FROM inventory_ledger WHERE source_ref=%s",
            (ledger_source_ref("feeding", "FEED-OVER"),),
        )
    with new_uow().begin() as tx:
        after = tx.query_scalar(
            "SELECT COALESCE(SUM(quantity_delta),0) FROM inventory_ledger WHERE lot_no=%s",
            (_LOT_LATE,),
        )
    check("撤掉后余额恢复 140（负余额不会留在库里）", D(after) == D(140), after)

    # 判定归属已按 评审结论 ④ 调整：账本原语不判定，`NoNegativeStock` 在能力层
    # 强制。**能力层的真实反例**在 `tools/warehouse_capability_e2e.py`——这里不重复断言，
    # 因为本文件的 A–G 段刻意不依赖任何域的能力声明（见文件头说明）。
    print()
    print("=" * 72)
    print("D. FEFO：物料 -> (warehouse_id, lot_no) 的解析入口（feeding.verify 用）")
    print("=" * 72)
    with new_uow().begin() as tx:
        apply_movement(
            tx, scope=scope, actor_id=ACTOR,
            source_type="receipt", source_ref=ledger_source_ref("warehouse", "RCV-E2E-003"),
            source_line_no=1, warehouse_id=WAREHOUSE, material_id=MATERIAL,
            lot_no=_LOT_SOON, quantity_delta=30, happened_at="2026-02-05 09:00:00",
            create_lot=True, expiry_date=datetime.date(2026, 6, 30),
        )

    with new_uow().begin() as tx:
        picked = resolve_issue_lot(tx, scope=scope, material_id=MATERIAL,
                                   warehouse_id=WAREHOUSE)
    check("FEFO 选中更早到期的批次 LOT-FEFO-SOON", picked.lot_no == _LOT_SOON, picked.lot_no)
    check("解析出的可用量 = 30", picked.available == D(30), picked.available)
    check("解析出的仓库 = 中心物料仓", picked.warehouse_name == "中心物料仓", picked.warehouse_name)

    with new_uow().begin() as tx:
        by_lot = resolve_issue_lot(tx, scope=scope, material_id=MATERIAL,
                                  warehouse_id=WAREHOUSE, lot_no=_LOT_LATE)
    check("显式指定 lot_no 时精确命中（不被 FEFO 覆盖）",
          by_lot.lot_no == _LOT_LATE and by_lot.available == D(140),
          f"{by_lot.lot_no} / {by_lot.available}")

    with new_uow().begin() as tx:
        defaulted = resolve_issue_lot(tx, scope=scope, material_id=MATERIAL)
    check("不传 warehouse_id 时落到默认仓（warehouses.is_default=1）",
          defaulted.warehouse_id == WAREHOUSE and defaulted.lot_no == _LOT_SOON,
          f"wh={defaulted.warehouse_id} lot={defaulted.lot_no}")

    print()
    print("=" * 72)
    print("E. 入参纪律：出库不能凭空造批次，来源类型与标识必须合法")
    print("=" * 72)
    cases = [
        ("出库引用不存在的批次被拒绝",
         dict(source_type="issue", source_ref=ledger_source_ref("feeding", "FEED-GHOST"),
              lot_no="LOT-DOES-NOT-EXIST", quantity_delta=-1, create_lot=False),
         "FIELD_INVALID"),
        ("零增量被拒绝（否则唯一键会被 0 行占位）",
         dict(source_type="issue", source_ref=ledger_source_ref("feeding", "FEED-ZERO"),
              lot_no=_LOT_LATE, quantity_delta=0),
         "VALIDATION_ERROR"),
        ("source_type=correction 被拒绝（§3.3 只允许 receipt/issue）",
         dict(source_type="correction", source_ref="warehouse:X",
              lot_no=_LOT_LATE, quantity_delta=-1),
         "VALIDATION_ERROR"),
        ("裸 source_ref（无域前缀）被拒绝",
         dict(source_type="issue", source_ref="no-colon-ref",
              lot_no=_LOT_LATE, quantity_delta=-1),
         "VALIDATION_ERROR"),
    ]
    for label, override, expected in cases:
        params = dict(
            scope=scope, actor_id=ACTOR, source_line_no=1, warehouse_id=WAREHOUSE,
            material_id=MATERIAL, happened_at="2026-01-09 09:00:00",
        )
        params.update(override)
        error = None
        try:
            with new_uow().begin() as tx:
                apply_movement(tx, **params)
        except DomainError as exc:
            error = exc
        check(label, error is not None and str(error.code) == expected,
              f"{error.code} / {error.message}" if error else "没有抛错")

    print()
    print("=" * 72)
    print("F. DataScope：越权仓库必须 403（fail-closed，不返回空集）")
    print("=" * 72)
    denied = None
    try:
        with new_uow().begin() as tx:
            apply_movement(
                tx, scope=scope_for(99), actor_id=ACTOR,
                source_type="receipt", source_ref=ledger_source_ref("warehouse", "RCV-DENIED"),
                source_line_no=1, warehouse_id=WAREHOUSE, material_id=MATERIAL,
                lot_no="LOT-DENIED", quantity_delta=1, happened_at="2026-01-10 09:00:00",
                create_lot=True,
            )
    except DomainError as error:
        denied = error
    check("仓库不在数据范围内 -> DATA_SCOPE_DENIED(403)",
          denied is not None and denied.code == "DATA_SCOPE_DENIED",
          f"{denied.code} / {denied.message}" if denied else "没有抛错")


# ===========================================================================
# 二、并发语义（本次抓出真实缺陷的那一条）
# ===========================================================================


def verify_concurrency() -> None:
    print()
    print("=" * 72)
    print("G. 并发：两个真连接抢同一个批次，锁必须让它们串行化")
    print("=" * 72)
    scope = scope_for(1)
    lot = _LOT_RACE

    with new_uow().begin() as tx:
        _clean(tx, lot)
    with new_uow().begin() as tx:
        apply_movement(
            tx, scope=scope, actor_id=ACTOR,
            source_type="receipt", source_ref=ledger_source_ref("warehouse", "RACE-SEED"),
            source_line_no=1, warehouse_id=WAREHOUSE, material_id=MATERIAL,
            lot_no=lot, quantity_delta=100, happened_at="2026-03-01 08:00:00", create_lot=True,
        )
    print("  批次余额 = 100；两个请求各要出库 60（合计 120 > 100）")

    held = threading.Event()
    results: dict[str, tuple] = {}
    #: 后拿到锁的那个事务读到的前置余额——用来证明它没有读到陈旧快照。
    second_balance_before = None

    def issue(tag: str, *, signal: bool = False, wait: bool = False) -> None:
        """出库 60。`signal`/`wait` 组成握手：先持锁的一方通知对方，
        对方的阻塞就发生在这一刻。

        **不能用 Barrier**：先拿到锁的线程若在临界区里等对方，而对方正卡在
        入口等锁，双方互等——第一版就是这么写成死锁的。
        """
        try:
            with UnitOfWork(config()).begin() as tx:
                movement = apply_movement(
                    tx, scope=scope, actor_id=ACTOR, source_type="issue",
                    source_ref=ledger_source_ref("feeding", f"RACE-{tag}"), source_line_no=1,
                    warehouse_id=WAREHOUSE, material_id=MATERIAL, lot_no=lot,
                    quantity_delta=-60, happened_at="2026-03-01 09:00:00",
                )
                if signal:
                    held.set()
                    time.sleep(1.0)
                else:
                    nonlocal second_balance_before
                    second_balance_before = movement.balance_before
                results[tag] = ("OK", str(movement.balance_before), str(movement.balance_after))
        except DomainError as error:
            results[tag] = ("REJECTED", str(error.code), error.message)
        except Exception as error:  # noqa: BLE001
            results[tag] = ("ERROR", type(error).__name__, str(error)[:160])

    thread_a = threading.Thread(target=issue, args=("A",), kwargs={"signal": True})
    thread_b = threading.Thread(target=issue, args=("B",), kwargs={"wait": True})
    thread_a.start()
    time.sleep(0.2)
    thread_b.start()
    thread_a.join(60)
    thread_b.join(60)

    for tag in ("A", "B"):
        print(f"    线程 {tag}: {results.get(tag)}")

    with new_uow().begin() as tx:
        final = tx.query_scalar(
            "SELECT COALESCE(SUM(quantity_delta),0) FROM inventory_ledger WHERE lot_no=%s", (lot,)
        )
        rows = tx.query_scalar("SELECT COUNT(*) FROM inventory_ledger WHERE lot_no=%s", (lot,))

    # 这一层的语义是**串行化**：批次行锁让两个事务严格排队，谁都不会重叠写。
    # 至于"合计会不会变成负"——那是 `NoNegativeStock` 在能力层的职责（H 段有真实反例）。
    check("两笔移动都按顺序落到账本（行锁串行化，无丢失更新）",
          int(rows) == 3, f"rows={rows}")
    check("第二个事务读到的是**第一个提交后**的余额（40，不是陈旧的 100）",
          second_balance_before is not None and D(second_balance_before) == D(40),
          second_balance_before)
    check("最终余额 = 100 − 60 − 60 = −20（本层不判定，由不变量在能力层拦）",
          D(final) == D(-20), final)


def _report() -> int:
    """统一结论输出。**两条路径都必须有结论**——只跑 `--ledger-only` 时若没有
    这一行，输出看起来是"没有失败"而不是"跑完了且没有失败"（本项目已确立的口径：
    空输入不是通过，是没检查）。"""
    print()
    print("=" * 72)
    print(f"结果：{len(_PASS)} 项通过，{len(_FAIL)} 项失败")
    for label in _FAIL:
        print("  FAILED: " + label)
    print("=" * 72)
    return 1 if _FAIL else 0


def _cleanup() -> None:
    """删除本 e2e 的全部探针行。

    契约 §5 原文："探针不干净就是缺陷，不是'换个库躲开'"。
    所以清理是**验收的一部分**，而不是随手一删。
    """
    removed = {}
    with new_uow().begin() as tx:
        removed["ledger"] = tx.execute(
            "DELETE FROM inventory_ledger WHERE source_ref LIKE %s", (PREFIX + "%",)
        )
        removed["ledger_by_lot"] = tx.execute(
            "DELETE FROM inventory_ledger WHERE lot_no LIKE %s", (PREFIX + "%",)
        )
        removed["lots"] = tx.execute(
            "DELETE FROM inventory_lots WHERE lot_no LIKE %s", (PREFIX + "%",)
        )
        # H 段产生的单据与关账探针（探针不干净就是缺陷，契约 §5）
        removed["document_lines"] = tx.execute(
            "DELETE FROM warehouse_document_lines WHERE document_id IN "
            "(SELECT id FROM warehouse_documents WHERE code LIKE %s)", (PREFIX + "%",)
        )
        removed["document_headers"] = tx.execute(
            "DELETE FROM warehouse_documents WHERE code LIKE %s", (PREFIX + "%",)
        )
        removed["periods"] = tx.execute(
            "DELETE FROM accounting_periods WHERE period = '2026-04' AND organization_id = 1"
        )
        # 跨域探针：应付 -> 采购单 -> 往来单位（外键 ON DELETE RESTRICT，顺序不能反）
        removed["payables"] = tx.execute(
            "DELETE FROM purchase_payables WHERE purchase_order_id IN "
            "(SELECT id FROM purchase_orders WHERE code LIKE %s)", (PREFIX + "%",)
        )
        removed["purchase_orders"] = tx.execute(
            "DELETE FROM purchase_orders WHERE code LIKE %s", (PREFIX + "%",)
        )
        removed["partners"] = tx.execute(
            "DELETE FROM business_partners WHERE code LIKE %s", (PREFIX + "%",)
        )
    print()
    print("探针清理：" + "、".join(f"{k}={v}" for k, v in removed.items()))

    with new_uow().begin() as tx:
        left_lots = tx.query_scalar(
            "SELECT COUNT(*) FROM inventory_lots WHERE lot_no LIKE %s", (PREFIX + "%",)
        )
        left_ledger = tx.query_scalar(
            "SELECT COUNT(*) FROM inventory_ledger WHERE source_ref LIKE %s OR lot_no LIKE %s",
            (PREFIX + "%", PREFIX + "%"),
        )
    with new_uow().begin() as tx:
        left_docs = tx.query_scalar(
            "SELECT COUNT(*) FROM warehouse_documents WHERE code LIKE %s", (PREFIX + "%",)
        )
        left_periods = tx.query_scalar(
            "SELECT COUNT(*) FROM accounting_periods WHERE period='2026-04' AND organization_id=1"
        )
    check("探针已清干净（批次 0 行）", int(left_lots) == 0, left_lots)
    check("探针已清干净（账本 0 行）", int(left_ledger) == 0, left_ledger)
    check("探针已清干净（单据 0 行）", int(left_docs) == 0, left_docs)
    check("探针已清干净（关账期间 0 行）", int(left_periods) == 0, left_periods)

    with new_uow().begin() as tx:
        left_po = tx.query_scalar(
            "SELECT COUNT(*) FROM purchase_orders WHERE code LIKE %s", (PREFIX + "%",)
        )
        left_pay = tx.query_scalar(
            "SELECT COUNT(*) FROM purchase_payables WHERE purchase_order_id IN "
            "(SELECT id FROM purchase_orders WHERE code LIKE %s)", (PREFIX + "%",)
        )
        left_sup = tx.query_scalar(
            "SELECT COUNT(*) FROM business_partners WHERE code LIKE %s", (PREFIX + "%",)
        )
    check("跨域探针已清干净（采购单 0 行）", int(left_po) == 0, left_po)
    check("跨域探针已清干净（应付 0 行）", int(left_pay) == 0, left_pay)
    check("跨域探针已清干净（往来单位 0 行）", int(left_sup) == 0, left_sup)


def main() -> int:
    print("=" * 72)
    print("0. 探针前置：按 code 解析种子 id（不写死自增主键）")
    print("=" * 72)
    _bootstrap()
    print()

    # 本 e2e 的批次号统一带前缀，避免与队友/上次运行的数据互相干扰。
    #
    # 开头**按前缀**清一次残留：上次若被 Ctrl-C 打断，会留下"批次已删但账本行还在"
    # 的状态，而那些孤儿账本行会在下次运行被 `SUM` 计进来（实测让 FEFO 段读到 170
    # 而不是 30）。清理放在这里（而不是每个用例里逐个删批次）就是为了不依赖
    # "上次跑完了" 这个假设——**同一份探针必须可重复运行**。
    with new_uow().begin() as tx:
        tx.execute("DELETE FROM inventory_ledger WHERE lot_no LIKE %s", (PREFIX + "%",))
        tx.execute("DELETE FROM inventory_ledger WHERE source_ref LIKE %s", (PREFIX + "%",))
        tx.execute("DELETE FROM inventory_lots WHERE lot_no LIKE %s", (PREFIX + "%",))
    global _LOT_LATE, _LOT_SOON, _LOT_RACE
    _LOT_LATE = PREFIX + "LOT-FEFO-LATE"
    _LOT_SOON = PREFIX + "LOT-FEFO-SOON"
    _LOT_RACE = PREFIX + "LOT-RACE"

    verify_ledger()
    verify_concurrency()
    _cleanup()
    return _report()


if __name__ == "__main__":
    sys.exit(main())
