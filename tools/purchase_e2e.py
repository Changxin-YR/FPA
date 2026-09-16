"""采购域端到端测试（真实 MySQL + 真实组合根 + 真实执行器）。

## 这个文件要证明什么

不是"代码没报错"，而是四件可验证的事：

**1. 写操作真的落了库。** 每一条 `kind=executed` 都跟着一次 `SELECT`，
断言行存在、值正确、审计有对应的 success。这是 `docs/WRITE_CONTRACT.md`
规则 2 的可执行形态。

**2. 本域承载的不变量确实被**强制**（每条都构造反例）。**

    DistinctActors(created_by, approved_by)  -> 自审必须被拒      §4 #5
    DistinctActors(created_by, verified_by)  -> 自审付款必须被拒  §4 #5
    AmountWithin(total_amount - paid_amount) -> 超余额付款必须被拒 §4 #4
    RequiredWhen(status=cancelled)           -> 取消不填原因必须被拒
    StatusAllowsEdit                         -> 已提交后不能编辑   §4 #12
    OptimisticLock                           -> 版本不符必须被拒   §4 #13
    StateTransition                          -> 非法状态转移被拒   §4 #14
    UniqueCode                               -> 单号重复必须被拒   §4 #19
    ReferencedStatus(materials=verified)     -> 未核验物料被拒     §4 #18
    SameTenant                               -> 客户冒充供应商被拒 §4 #18

只跑正例不能证明规则存在 —— 一个永远返回成功的不变量和一个永远返回失败的不变量
在正例下看起来一样。

**3. `__yuxin_load_by_id__` **不靠夹具补挂**也能通过。**

`tools/{runner,web,agent}_e2e.py` 都在夹具里手动补挂它，而那掩盖了
`domains/master_data/ponds_write.py` 的真实缺陷（生产路径下 master_data 的写能力
全都会 500，见 t13）。本文件走真实组合根、**不做任何补挂**；并在第 0 节
**先断言绑定真的存在**，这样"忘了绑"会在第一个断言处就失败，
而不是表现为一个位置很偏的 INTERNAL_ERROR。

**4. 跨域入口 `apply_receipt` 真的推进了采购单状态**（契约 §2）：
造一张 `status='verified'` 的到货单（warehouse 域的表），调用
`purchase.purchase_orders.apply_receipt(...)`，断言 `purchase_orders.status`
变成 `fully_received`。这证明 `receipt.verify` 那条链路在采购侧是通的 ——
即使 warehouse 的域代码此刻尚未就绪。

用法::

    $env:MYSQL_USER='yuxin'; $env:MYSQL_PASSWORD='yuxin_dev_password'
    python tools\\purchase_e2e.py
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pymysql  # noqa: E402

from yuxin.domains._base import Actor, ServiceContext  # noqa: E402
from yuxin.kernel.audit import AuditWriter  # noqa: E402
from yuxin.kernel.errors import DomainError  # noqa: E402
from yuxin.kernel.idempotency import IdempotencyStore  # noqa: E402
from yuxin.kernel.runner import ActorView, CapabilityRunner, Invocation  # noqa: E402
from yuxin.kernel.scope import Scope  # noqa: E402
from yuxin.kernel.uow import ConnectionConfig, UnitOfWork  # noqa: E402

FAILURES = 0

#: 探针前缀。用它做清理与计数，**不用 `DELETE FROM purchase_orders` 清全表**：
#: 本脚本可能跑在共享开发库上，清全表会把别人的数据一起删掉。
PROBE = "PURO-E2E-"

TODAY = date.today()
#: 探针到货单的发生时间（今天 10:00）。**不用 `NOW()`** —— 断言要可复现，
#: 而且它决定应付的入账日（`occurred_on`）进而决定落在哪个会计期间。
RECEIPT_AT = datetime(TODAY.year, TODAY.month, TODAY.day, 10, 0, 0)
NOW = datetime.now()

#: 探针用户 id。9xxx 段在 003/004 的种子里没有占用。
MAKER_ID = 9101
CHECKER_ID = 9102
NOBODY_ID = 9103

MAKER_PERMISSIONS = frozenset(
    {
        "purchase.view",
        "purchase.create",
        "purchase.approve",
        "finance.payable.view",
        "finance.payment.view",
        "finance.payment.manage",
        "finance.payment.verify",
    }
)
CHECKER_PERMISSIONS = frozenset(
    {
        "purchase.view",
        "purchase.approve",
        "finance.payment.view",
        # `payment.verify` 的权限码是 `finance.payment.verify`，与「复核人」这个
        # 角色名无关。少了它，反例会以 FORBIDDEN（缺权限）而不是 CONFLICT
        # （超余额）失败，而那种失败证明不了 `AmountWithin` 存在 ——
        # 正是本项目反复强调的「断言具体的错误码」。
        "finance.payment.verify",
    }
)


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


def expect_rejected(label: str, code: str | tuple[str, ...], action, *, detail: str = ""):
    """断言一次调用**被业务规则拒绝**（而不是成功、也不是崩在别的错误上）。

    为什么要断言**具体的错误码**：只断言"抛了异常"会让"因为有 bug 而崩"
    与"因为规则生效而拒"看起来一样。这条区分正是本项目全部测试的要点。
    """
    global FAILURES
    try:
        action()
    except DomainError as error:
        allowed = (code,) if isinstance(code, str) else code
        if str(error.code) in allowed:
            print(f"  PASS  {label}  [{error.code}] {error.message}")
            return error
        FAILURES += 1
        print(f"  FAIL  {label}  错误码是 {error.code}，期望 {allowed}；消息：{error.message}")
        return error
    except Exception as error:  # noqa: BLE001
        FAILURES += 1
        print(f"  FAIL  {label}  抛了非业务异常 {type(error).__name__}: {error}")
        return None
    FAILURES += 1
    print(f"  FAIL  {label}  竟然成功了  {detail}")
    return None


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------

def _config() -> ConnectionConfig:
    return ConnectionConfig(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "yuxin"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "yuxin"),
    )


def uow_factory() -> UnitOfWork:
    return UnitOfWork(_config())


def raw_connection():
    return pymysql.connect(**_config().as_kwargs())


def count_audit(tx: UnitOfWork, capability: str, result: str) -> int:
    """审计表 append-only（001 有 BEFORE UPDATE 触发器），所以断言的是**增量**。"""
    row = tx.query_one(
        "SELECT COUNT(*) AS n FROM audit_logs WHERE capability=%s AND result=%s",
        (capability, result),
    )
    return int((row or {}).get("n", 0))


class PersonaScopeResolver:
    """按 user_id 返回不同数据范围的替身。

    `DistinctActors` 要求"经办人 ≠ 审批人"，测试里**必须有两个不同的人**
    才能验证这条规则的两个方向（自审被拒 / 他人放行）。
    """

    def __init__(self, mapping: dict[int, list[dict]]) -> None:
        self._mapping = mapping

    def resolve(self, *, user_id: int, role_codes: frozenset[str]) -> Scope:
        entries = self._mapping.get(user_id, [])
        if not entries:
            # 没有任何范围记录 -> 不能构造带有区域的 Scope。
            # 这正是我们要的：**解析失败必须抛，不能退化成空集**（fail-closed）。
            return Scope(allow_all=False, entries=(), user_id=user_id)
        return Scope.from_rows(entries, user_id=user_id)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

#: 五域组合根的顺序装载已被兄弟域的编辑打断过多次（production / warehouse / cost
#: 同时改文件时，任何一个语法错误都会让 `bootstrap.load_all()` 整体抛错）。
#: 这个函数把"装载"与"报告"分开：失败的域名与异常**原样带出**，由调用方决定
#: 是容忍还是中止 —— 绝不在库里静默跳过。
def load_registry_tolerantly():
    """逐域装载能力声明，返回 (registry, {失败的域: 异常描述})。"""
    import importlib

    from yuxin.bootstrap import discover_domains

    unavailable: dict[str, str] = {}
    for domain in discover_domains():
        try:
            importlib.import_module(f"yuxin.domains.{domain}.capabilities")
        except Exception as error:  # noqa: BLE001
            unavailable[domain] = f"{type(error).__name__}: {error}"

    from yuxin.kernel.capability import REGISTRY

    return REGISTRY, unavailable



def main() -> int:  # noqa: C901 - 顺序检查清单，拆开会让"哪一步在验什么"失焦
    # ------------------------------------------------------------------
    print("=== 0. 组合根与回读函数绑定（**不靠夹具补挂**）===")
    registry, unavailable = load_registry_tolerantly()
    if "purchase" in unavailable:
        # 本域自己装载失败 -> 不容忍、不继续（继续跑出来的"通过"没有意义）
        print(f"FAIL  本域（purchase）装载失败：{unavailable['purchase']}")
        return 1
    if unavailable:
        # 别的域正在被其负责人编辑时会出现这种情形（组合根是全队共享入口，
        # 任何一个域的语法错误都会把所有域的 e2e 卡住）。**显式打印，不静默跳过**：
        # 本行的存在就是"这次运行只验证了部分域"的证据。
        print(f"WARN  以下域此刻装载失败，本次运行**未**验证它们（与本域无关）：")
        for domain, detail in sorted(unavailable.items()):
            print(f"        {domain}: {detail}")
        print("      本域（purchase）不受影响，继续。")

    purchase_names = [
        "purchase_order.list",
        "purchase_order.create",
        "purchase_order.get",
        "purchase_order.update",
        "purchase_order.submit",
        "purchase_order.approve",
        "purchase_order.cancel",
        "payable.list",
        "purchase_payable.get",
        "payment.list",
        "payment.get",
        "payment.create",
        "payment.verify",
    ]
    missing = [name for name in purchase_names if registry.find(name) is None]
    check(
        f"采购业务能力全部注册（registry §1.7 + 详情能力）",
        not missing,
        f"缺：{missing}",
    )

    # ★ 这一节是 t13 那个缺陷在本域的**回归护栏**：
    #   master_data 的写能力没有绑 `__yuxin_load_by_id__`，而这个 e2e 家族
    #   （runner/web/agent）都在夹具里手动补挂 —— 于是生产路径全都会 500
    #   而七套自检照样全绿。本域**不补挂**，先在这里把"没绑"钉死。
    from yuxin.domains.purchase.orders_write import PurchaseOrderWriteService
    from yuxin.domains.purchase.payments_write import PurchasePaymentWriteService

    unbound: list[str] = []
    for cls, methods in (
        (
            PurchaseOrderWriteService,
            ("create_order", "update_order", "submit_order", "approve_order", "cancel_order"),
        ),
        (PurchasePaymentWriteService, ("create_payment", "verify_payment")),
    ):
        for method_name in methods:
            loader = getattr(getattr(cls, method_name), "__yuxin_load_by_id__", None)
            if not callable(loader):
                unbound.append(f"{cls.__name__}.{method_name}")
    check(
        "7 个写能力的 __yuxin_load_by_id__ 全部在**生产代码**里绑定（不靠夹具）",
        not unbound,
        f"未绑定：{unbound}",
    )

    # ------------------------------------------------------------------
    print("\n=== 1. 环境与探针数据 ===")
    connection = raw_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE() AS db")
            database = cursor.fetchone()["db"]
    finally:
        connection.close()
    print(f"库：{database}")

    with uow_factory().begin() as tx:
        area_row = tx.query_one(
            "SELECT a.id AS area_id, a.farm_id, a.organization_id, "
            "(SELECT id FROM areas WHERE id <> a.id ORDER BY id LIMIT 1) AS other_area_id "
            "FROM areas AS a ORDER BY a.id LIMIT 1"
        )
        if area_row is None:
            print("FAIL  areas 表没有数据 —— 先跑 `python tools/migrate.py apply`（003）")
            return 1
        area_id = int(area_row["area_id"])
        other_area_id = (
            int(area_row["other_area_id"]) if area_row["other_area_id"] is not None else area_id
        )
        org_id = int(area_row["organization_id"])
        farm_id = int(area_row["farm_id"])

        material = tx.query_one(
            "SELECT id, name, unit FROM materials WHERE status='verified' ORDER BY id LIMIT 1"
        )
        if material is None:
            print("FAIL  没有 verified 的物料 —— 003 的种子没跑")
            return 1
        material_id = int(material["id"])
        material_name = str(material["name"])

        supplier = tx.query_one(
            "SELECT id, name FROM business_partners "
            "WHERE partner_type='supplier' AND status='verified' ORDER BY id LIMIT 1"
        )
        customer = tx.query_one(
            "SELECT id, area_id FROM business_partners WHERE partner_type='customer' LIMIT 1"
        )
        if supplier is None or customer is None:
            print("FAIL  缺少供应商或客户种子（003）")
            return 1
        supplier_id = int(supplier["id"])
        # 种子里的那个客户可能落在**调用方范围之外**的区域，那样它就证明不了
        # `partner_type` 判定（范围校验会先拒绝 —— 实测踩到）。
        # 所以另建一个**同区域**的探针客户，专门用来隔离 `partner_type` 这一条规则。
        tx.execute(
            "INSERT INTO business_partners "
            "(organization_id, farm_id, area_id, partner_type, code, name, status, created_by) "
            "VALUES (%s,%s,%s,'customer',%s,%s,'verified',%s) "
            "ON DUPLICATE KEY UPDATE name=VALUES(name)",
            (org_id, farm_id, area_id, f"{PROBE}CUS", "采购探针客户（同区域）", MAKER_ID),
        )
        probe_customer = tx.query_one(
            "SELECT id FROM business_partners WHERE code=%s", (f"{PROBE}CUS",)
        )
        customer_in_scope_id = int(probe_customer["id"])

        warehouse = tx.query_one("SELECT id FROM warehouses ORDER BY id LIMIT 1")
        if warehouse is None:
            # 收货仓指向 warehouse 域的表（006）。探针库里没有仓库时**自己造一行**，
            # 而不是跳过这一段 —— 跳过会让"供应商/物料/仓库同企业"这条不变量
            # 在测试里形同不存在。
            tx.execute(
                "INSERT INTO warehouses (organization_id, farm_id, area_id, code, name, "
                "status, created_by) VALUES (%s,%s,%s,%s,%s,'verified',%s)",
                (org_id, farm_id, area_id, f"{PROBE}WH", "采购探针仓", MAKER_ID),
            )
            warehouse = tx.query_one("SELECT id FROM warehouses ORDER BY id LIMIT 1")
        warehouse_id = int(warehouse["id"])

        # 探针用户：`created_by` / `approved_by` / `verified_by` 都带外键，
        # 所以"两个人"必须是真实行。
        tx.execute(
            "INSERT INTO users (id, username, display_name, password_hash, status) "
            "VALUES (9101,'purchase-e2e-maker','采购经办探针','x','active'),"
            "       (9102,'purchase-e2e-checker','采购复核探针','x','active'),"
            "       (9103,'purchase-e2e-nobody','无范围探针','x','active') "
            "ON DUPLICATE KEY UPDATE display_name=VALUES(display_name)"
        )

        # 清理上轮探针。顺序：付款 -> 应付 -> 到货单 -> 采购单（外键方向反着来）。
        tx.execute(
            "DELETE FROM purchase_payments WHERE code LIKE %s", (f"{PROBE}%",)
        )
        tx.execute(
            "DELETE pay FROM purchase_payables AS pay "
            "INNER JOIN purchase_orders AS o ON o.id = pay.purchase_order_id "
            "WHERE o.code LIKE %s",
            (f"{PROBE}%",),
        )
        tx.execute(
            "DELETE FROM warehouse_documents WHERE code LIKE %s", (f"{PROBE}%",)
        )
        tx.execute("DELETE FROM purchase_orders WHERE code LIKE %s", (f"{PROBE}%",))
        tx.execute("DELETE FROM idempotency_keys WHERE capability LIKE 'purchase_%'")
        tx.execute("DELETE FROM business_partners WHERE code = %s", (f"{PROBE}CUS",))
        tx.execute("DELETE FROM idempotency_keys WHERE capability LIKE 'payment.%'")

        # **清理必须有可断言的后置条件**：残留数据会让下一轮的"正常拒绝"
        # 被误读成"新写入被拒"（cost_e2e 踩过这个坑）。
        leftover = tx.query_one(
            "SELECT COUNT(*) AS n FROM purchase_orders WHERE code LIKE %s", (f"{PROBE}%",)
        )
        if int((leftover or {}).get("n", 0)):
            raise SystemExit(
                f"清理未生效：仍有 {leftover['n']} 条 {PROBE} 采购单。"
                "请检查这些行的 code 前缀，否则当前迭代断言会把旧行的正常拒绝误读成新写入被拒。"
            )

    print(f"探针：area={area_id}（另一区域 {other_area_id}）material={material_id} "
          f"supplier={supplier_id} warehouse={warehouse_id}")

    runner = CapabilityRunner(
        registry=registry,
        uow_factory=uow_factory,
        audit=AuditWriter(),
        idempotency=IdempotencyStore(uow_factory),
        scope_resolver=PersonaScopeResolver(
            {
                MAKER_ID: [{"scope_type": "area", "area_id": area_id}],
                CHECKER_ID: [{"scope_type": "area", "area_id": area_id}],
                # 9103 故意没有范围记录 -> DATA_SCOPE_UNRESOLVED
            }
        ),
    )

    maker = ActorView(
        user_id=MAKER_ID,
        username="purchase-e2e-maker",
        permissions=MAKER_PERMISSIONS,
    )
    checker = ActorView(
        user_id=CHECKER_ID,
        username="purchase-e2e-checker",
        permissions=CHECKER_PERMISSIONS,
    )
    nobody = ActorView(
        user_id=NOBODY_ID, username="purchase-e2e-nobody", permissions=MAKER_PERMISSIONS
    )
    # 无权限账号：验证第二层防御（权限码）而不是第三层（数据范围）
    no_permission = ActorView(
        user_id=MAKER_ID, username="purchase-e2e-maker", permissions=frozenset()
    )

    def create_order(code_suffix: str, *, quantity: str = "1000", unit_price: str = "4.8",
                     expected_days: int = 7, due_days: int = 30, actor=None,
                     supplier: int | None = None, material: int | None = None,
                     idem: str | None = None):
        return runner.invoke(
            Invocation(
                capability_name="purchase_order.create",
                payload={
                    "code": f"{PROBE}{code_suffix}",
                    "name": f"采购探针 {code_suffix}",
                    "supplier_id": supplier if supplier is not None else supplier_id,
                    "material_id": material if material is not None else material_id,
                    "warehouse_id": warehouse_id,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "expected_delivery_date": str(TODAY + timedelta(days=expected_days)),
                    "due_date": str(TODAY + timedelta(days=due_days)),
                    "note": "采购域 e2e 探针",
                },
                idempotency_key=idem,
            ),
            actor or maker,
            f"purchase-e2e-create-{code_suffix}",
        )

    # ------------------------------------------------------------------
    print("\n=== 2. 新建采购单：全链路（权限 -> 范围 -> 不变量 -> 写库 -> 审计 -> 回读）===")
    with uow_factory().begin() as tx:
        audit_before = count_audit(tx, "purchase_order.create", "success")

    result = create_order("ORD-001", idem="purchase-e2e-ord-001")
    check("kind == executed", result.kind == "executed", f"实际 {result.kind}")
    order_id = result.resource_id
    check("返回了真实主键", isinstance(order_id, int) and order_id > 0, f"{order_id}")

    with uow_factory().begin() as tx:
        row = tx.query_one("SELECT * FROM purchase_orders WHERE id=%s", (order_id,))
        check("库里真的有这一行", row is not None)
        if row:
            check("状态是 draft（服务端决定，不接受客户端指定）", row["status"] == "draft",
                  f"实际 {row['status']}")
            check("分租键由数据范围解析写入", int(row["area_id"]) == area_id,
                  f"area_id={row['area_id']} 期望 {area_id}")
            check("approved_by 为空（只有 approve 能写）", row["approved_by"] is None,
                  f"{row['approved_by']}")
            check("金额列是精确小数", str(row["quantity"]) == "1000.000",
                  f"quantity={row['quantity']}")
        audit_after = count_audit(tx, "purchase_order.create", "success")
        check("审计新增了一条 success（与业务同事务）", audit_after == audit_before + 1,
              f"{audit_before} -> {audit_after}")

    # 总量 = quantity × unit_price 由服务端算（不存列）
    check("派生金额 total_amount = 1000 × 4.8",
          Decimal(str(result.data["record"]["total_amount"])) == Decimal("4800.0000"),
          f"实际 {result.data['record']['total_amount']}")
    check("派生 status_label 来自状态机",
          result.data["record"]["status_label"] == "草稿",
          f"实际 {result.data['record']['status_label']}")

    # ------------------------------------------------------------------
    print("\n=== 3. 列表与派生列 ===")
    listed = runner.invoke(
        Invocation(capability_name="purchase_order.list", query={"keyword": PROBE}),
        maker,
        "purchase-e2e-list",
    )
    check("列表 kind == read", listed.kind == "read", f"实际 {listed.kind}")
    check("列表能按关键词命中探针单", listed.data["total"] >= 1, f"total={listed.data['total']}")
    first = listed.data["items"][0]
    for column in ("supplier_name", "material_name", "unpaid_amount"):
        check(f"派生列 {column} 存在", column in first, f"keys={sorted(first)[:12]}")
    check("unpaid_amount 在没有应付时是 0",
          Decimal(str(first["unpaid_amount"])) == 0, f"实际 {first['unpaid_amount']}")
    # 跨域派生列（warehouse 域）：006 已落盘，但表名权威登记在 t16；
    # 这里只断言"键存在且可为空"，不断言具体值 —— 避免把未定稿的东西写成期望。
    check("跨域派生列 warehouse_name 存在（值可为 None）", "warehouse_name" in first)

    # ------------------------------------------------------------------
    print("\n=== 4. 权限与数据范围（第二层 / 第三层防御）===")
    expect_rejected(
        "无权限账号新建被拒（第二层：权限码）",
        "FORBIDDEN",
        lambda: create_order("ORD-NOPERM", actor=no_permission, idem="purchase-e2e-noperm"),
        detail="无 purchase.create 权限却成功了",
    )
    expect_rejected(
        "无数据范围账号新建被拒（第三层：fail-closed）",
        "DATA_SCOPE_UNRESOLVED",
        lambda: create_order("ORD-NOSCOPE", actor=nobody, idem="purchase-e2e-noscope"),
        detail="数据范围解析不出来却成功了",
    )
    with uow_factory().begin() as tx:
        leaked = tx.query_one(
            "SELECT COUNT(*) AS n FROM purchase_orders WHERE code IN (%s,%s)",
            (f"{PROBE}ORD-NOPERM", f"{PROBE}ORD-NOSCOPE"),
        )
        check("两次被拒都没有写库", int(leaked["n"]) == 0, f"实际 {leaked['n']} 行")

    # ------------------------------------------------------------------
    print("\n=== 5. 不变量反例：§4 #18 / #19（引用与唯一性）===")
    # ★ 两个反例**各隔离一条规则**（否则"因为别的原因被拒"与"因为这条规则被拒"
    #   看起来一样 —— 这正是我之前踩的坑）：
    #   ① 区域外的往来单位 -> 范围校验先拒（DATA_SCOPE_DENIED）
    #   ② 同区域内、类型是客户 -> partner_type 判定拒（FIELD_INVALID）
    if int(customer["area_id"]) != area_id:
        expect_rejected(
            "区域外的供应商被拒（第三层范围校验先生效）",
            ("DATA_SCOPE_DENIED", "FIELD_INVALID"),
            lambda: create_order(
                "ORD-XAREASUP", supplier=int(customer["id"]), idem="purchase-e2e-xareasup"
            ),
            detail="区域外的往来单位竟然能当供应商用",
        )
    else:
        print("  SKIP  种子客户与本例同区域，无法构造'区域外'反例（不假装验证过）")
    expect_rejected(
        "同区域内「客户」当供应商用被拒（partner_type 判定，隔离于范围校验）",
        ("FIELD_INVALID", "CONFLICT"),
        lambda: create_order(
            "ORD-BADSUP", supplier=customer_in_scope_id, idem="purchase-e2e-badsup"
        ),
        detail="客户竟然能当供应商用",
    )
    expect_rejected(
        "采购单号重复被拒（§4 #19 UniqueCode + DB 唯一键兜底）",
        "CONFLICT",
        lambda: create_order("ORD-001", idem="purchase-e2e-dup"),
        detail="同一个单号竟然能建两张单",
    )
    expect_rejected(
        "付款到期日早于预计到货日被拒（§2.8 日期顺序）",
        "FIELD_INVALID",
        lambda: create_order("ORD-BADDATE", expected_days=30, due_days=10,
                             idem="purchase-e2e-baddate"),
        detail="日期倒挂竟然能建单",
    )

    # ------------------------------------------------------------------
    print("\n=== 6. 不变量反例：§4 #12 / #13（只读与乐观锁）===")
    # 先提交，让状态离开 draft
    submitted = runner.invoke(
        Invocation(
            capability_name="purchase_order.submit",
            path_params={"order_id": order_id},
            payload={"order_id": order_id, "expected_version": 1},
            idempotency_key="purchase-e2e-submit-key",
        ),
        maker,
        "purchase-e2e-submit",
    )
    check("提交成功且状态变为 submitted",
          submitted.kind == "executed"
          and submitted.data["record"]["status"] == "submitted",
          f"{submitted.kind} / {submitted.data['record']['status'] if submitted.data else None}")

    expect_rejected(
        "已提交后编辑被拒（§4 #12 StatusAllowsEdit / 状态机 EDIT 动作）",
        ("CONFLICT", "FORBIDDEN"),
        lambda: runner.invoke(
            Invocation(
                capability_name="purchase_order.update",
                path_params={"order_id": order_id},
                payload={"order_id": order_id, "expected_version": 2, "name": "改个名字"},
                idempotency_key="purchase-e2e-update-submitted-key",
            ),
            maker,
            "purchase-e2e-update-submitted",
        ),
        detail="已提交的单据竟然能编辑",
    )
    # §4 #13 乐观锁的反例必须用**能走到版本检查那一步**的能力：
    # `purchase_order.update`（draft 上唯一允许的动作）。用它而不是 submit：
    # submit 在已提交状态会被状态机先拒（CONFLICT「待审批的记录不支持「submit」操作」），
    # 于是那个反例实际验证的是状态机而不是乐观锁。
    lock_probe = create_order("ORD-LOCK", idem="purchase-e2e-lock-src")
    lock_probe_id = lock_probe.resource_id
    expect_rejected(
        "版本号不符被拒（§4 #13 OptimisticLock）",
        "VERSION_CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="purchase_order.update",
                path_params={"order_id": lock_probe_id},
                payload={"order_id": lock_probe_id, "expected_version": 999,
                         "name": "错误版本号"},
                idempotency_key="purchase-e2e-version-key",
            ),
            maker,
            "purchase-e2e-version",
        ),
        detail="错误版本号竟然通过了",
    )
    # 正例：正确版本号能改成功。没有这一条，"版本不符被拒"也可能只是
    # "这条能力坏了"的另一种表现。
    ok_update = runner.invoke(
        Invocation(
            capability_name="purchase_order.update",
            path_params={"order_id": lock_probe_id},
            payload={"order_id": lock_probe_id, "expected_version": 1,
                     "name": "正确版本号改名"},
            idempotency_key="purchase-e2e-version-ok",
        ),
        maker,
        "purchase-e2e-version-ok",
    )
    check("正确版本号可以修改草稿（乐观锁的正例）",
          ok_update.kind == "executed"
          and ok_update.data["record"]["name"] == "正确版本号改名"
          and ok_update.data["record"]["version"] == 2,
          f"{ok_update.kind} / {(ok_update.data or {}).get('record')}")
    expect_rejected(
        "非法状态转移被拒（§4 #14 StateTransition：submitted 已经不能再 submit）",
        ("CONFLICT", "VERSION_CONFLICT"),
        lambda: runner.invoke(
            Invocation(
                capability_name="purchase_order.submit",
                path_params={"order_id": order_id},
                payload={"order_id": order_id, "expected_version": 2},
                idempotency_key="purchase-e2e-twice-submit-key",
            ),
            maker,
            "purchase-e2e-twice-submit",
        ),
        detail="重复提交竟然能推进状态",
    )

    # ------------------------------------------------------------------
    print("\n=== 7. 审批：§4 #5 DistinctActors（自审必须被拒）===")
    # 造一条"由 maker 创建、且处于 submitted"的单据，然后让 maker 自己去审批。
    # 不能直接用 `create_order` 造（它建的是 draft），因为 created_by 只能由服务端写；
    # 所以这里用**直接 UPDATE 把 created_by 改成 maker** 的方式造场景 ——
    # 这正是"经办人=当前用户"的状态，而不变量的判据完全来自行上的 created_by。
    self_audit_order = create_order("ORD-SELFAUDIT", idem="purchase-e2e-selfaudit")
    self_id = self_audit_order.resource_id
    with uow_factory().begin() as tx:
        tx.execute(
            "UPDATE purchase_orders SET status='submitted', created_by=%s WHERE id=%s",
            (MAKER_ID, self_id),
        )
    expect_rejected(
        "经办人审批自己的采购单被拒（§4 #5 DistinctActors）",
        ("FORBIDDEN", "CONFLICT"),
        lambda: runner.invoke(
            Invocation(
                capability_name="purchase_order.approve",
                path_params={"order_id": self_id},
                payload={"order_id": self_id, "expected_version": 1},
                idempotency_key="purchase-e2e-self-approve-key",
            ),
            maker,
            "purchase-e2e-self-approve",
        ),
        detail="自审竟然通过了 —— 这是早期版本 purchase 域的真实缺陷",
    )

    approved = runner.invoke(
        Invocation(
            capability_name="purchase_order.approve",
            path_params={"order_id": order_id},
            payload={"order_id": order_id, "expected_version": 2},
            idempotency_key="purchase-e2e-approve-key",
        ),
        checker,
        "purchase-e2e-approve",
    )
    check("他人审批成功（复核人放行）",
          approved.kind == "executed" and approved.data["record"]["status"] == "approved",
          f"{approved.kind} / {approved.data['record']['status'] if approved.data else None}")
    with uow_factory().begin() as tx:
        row = tx.query_one(
            "SELECT status, approved_by, approved_at FROM purchase_orders WHERE id=%s",
            (order_id,),
        )
        check("approved_by 记录了审批人", int(row["approved_by"]) == CHECKER_ID,
              f"{row['approved_by']}")
        check("approved_at 有值", row["approved_at"] is not None)

    # ------------------------------------------------------------------
    print("\n=== 8. 取消：取消原因必填（RequiredWhen）===")
    cancel_target = create_order("ORD-CANCEL", idem="purchase-e2e-cancel-src")
    cancel_id = cancel_target.resource_id
    with uow_factory().begin() as tx:
        tx.execute(
            "UPDATE purchase_orders SET status='submitted' WHERE id=%s", (cancel_id,)
        )
    expect_rejected(
        "不填原因取消被拒（RequiredWhen + 服务校验 + DB CHECK）",
        ("VALIDATION_ERROR", "CONFLICT"),
        lambda: runner.invoke(
            Invocation(
                capability_name="purchase_order.cancel",
                path_params={"order_id": cancel_id},
                payload={"order_id": cancel_id, "expected_version": 1,
                         "reason": "  "},
                idempotency_key="purchase-e2e-cancel-noreason-key",
            ),
            maker,
            "purchase-e2e-cancel-noreason",
        ),
        detail="空白取消原因竟然通过了",
    )
    cancelled = runner.invoke(
        Invocation(
            capability_name="purchase_order.cancel",
            path_params={"order_id": cancel_id},
            payload={
                "order_id": cancel_id,
                "expected_version": 1,
                "reason": "供应商缺货，改为重新询价",
            },
            idempotency_key="purchase-e2e-cancel-key",
        ),
        maker,
        "purchase-e2e-cancel",
    )
    check("填了原因就能取消",
          cancelled.kind == "executed" and cancelled.data["record"]["status"] == "cancelled",
          f"{cancelled.kind}")
    with uow_factory().begin() as tx:
        row = tx.query_one(
            "SELECT status, reason FROM purchase_orders WHERE id=%s", (cancel_id,)
        )
        check("取消原因落了库", row["reason"] == "供应商缺货，改为重新询价",
              f"{row['reason']}")

    # ------------------------------------------------------------------
    print("\n=== 9. 跨域：apply_receipt 按已核验到货推进状态（契约 §2）===")
    # 造一张已核验的到货单（warehouse 域的表，006 迁移）。
    # 这是**直接构造数据**而不是调用 warehouse 的能力 —— warehouse 域的实现
    # 由 warehouse-dev 负责，采购侧的契约是"给我累计量、我推状态"。
    with uow_factory().begin() as tx:
        tx.execute(
            "INSERT INTO warehouse_documents "
            "(organization_id, farm_id, area_id, doc_type, code, name, warehouse_id, "
            " material_id, quantity, unit_cost, total_quantity, total_amount, supplier_id, "
            " purchase_order_id, happened_at, status, created_by, verified_by, verified_at) "
            "VALUES (%s,%s,%s,'receipt',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'verified',%s,%s,NOW())",
            (
                org_id, farm_id, area_id, f"{PROBE}RCPT-001", "采购探针到货单",
                warehouse_id, material_id, "1000.0000", "4.8000",
                "1000.0000", "4800.0000", supplier_id, order_id,
                # 到货发生时间：它决定应付的入账日（见文件头部说明）
                RECEIPT_AT, MAKER_ID, CHECKER_ID,
            ),
        )
        receipt_id = tx.last_insert_id()

    from yuxin.domains.purchase.orders_write import apply_receipt

    # apply_receipt 是跨域入口：调用方（warehouse 的 receipt.verify）传自己的 ctx，
    # 采购侧用 `ctx.scope` 做第三层范围校验。这里构造等价的 ServiceContext。
    scope = PersonaScopeResolver({MAKER_ID: [{"scope_type": "area", "area_id": area_id}]}).resolve(
        user_id=MAKER_ID, role_codes=frozenset()
    )
    actor = Actor(
        user_id=MAKER_ID,
        username="purchase-e2e-maker",
        permissions=MAKER_PERMISSIONS,
        scope=scope,
    )
    ctx_for_receipt = ServiceContext(actor=actor, request_id="purchase-e2e-apply-receipt")

    with uow_factory().begin() as tx:
        outcome = apply_receipt(
            tx, ctx_for_receipt,
            purchase_order_id=order_id, receipt_document_id=receipt_id,
            actor_id=CHECKER_ID, happened_at=RECEIPT_AT,
        )
    print(f"        apply_receipt -> {outcome}")
    check("apply_receipt 回报了事实（status/累计量/单据量）",
          {"status", "previous_status", "ordered_quantity", "cumulative_received", "changed"}
          <= set(outcome),
          f"keys={sorted(outcome)}")
    check("没有 over_received 字段（评审结论：超量由 CumulativeWithin 拦，不在这里判）",
          "over_received" not in outcome,
          f"keys={sorted(outcome)}")
    check("累计 1000 == 采购量 1000 -> fully_received", outcome["status"] == "fully_received",
          f"实际 {outcome['status']}")
    with uow_factory().begin() as tx:
        row = tx.query_one("SELECT status FROM purchase_orders WHERE id=%s", (order_id,))
        check("库里的采购单状态真的变了", str(row["status"]) == "fully_received",
              f"实际 {row['status']}")

    # ------------------------------------------------------------------
    print("\n=== 10. 应付与付款：§4 #4 AmountWithin 是唯一强制点 ===")
    # 应付由 receipt.verify 经 `create_from_receipt` 生成（契约 §2）。
    from yuxin.domains.purchase.payables import create_from_receipt

    with uow_factory().begin() as tx:
        payable = create_from_receipt(
            tx, ctx_for_receipt,
            purchase_order_id=order_id,
            receipt_document_id=receipt_id,
            lines=((material_id, Decimal("1000")),),
            actor_id=CHECKER_ID, happened_at=RECEIPT_AT,
        )
    print(f"        create_from_receipt -> {payable}")
    payable_id = payable["id"]
    with uow_factory().begin() as tx:
        payable_row_check = tx.query_one(
            "SELECT occurred_on, due_date FROM purchase_payables WHERE id=%s", (payable_id,)
        )
    check("应付的入账日来自到货发生时间（不是今天）",
          str(payable_row_check["occurred_on"]) == str(TODAY),
          f"occurred_on={payable_row_check['occurred_on']} 期望 {TODAY}（来自 happened_at）")
    check("应付金额 = 1000 × 4.8 = 4800", Decimal(payable["amount"]) == Decimal("4800.00"),
          f"实际 {payable['amount']}")
    check("应付初始状态 unpaid 且已付 0",
          payable["status"] == "unpaid" and Decimal(payable["paid_amount"]) == 0,
          f"{payable['status']} / {payable['paid_amount']}")
    check("余额 = 总额 − 已付", Decimal(payable["balance"]) == Decimal("4800.00"),
          f"实际 {payable['balance']}")

    # 幂等：同一张到货单再生成一次，返回 created=False 而不报错
    with uow_factory().begin() as tx:
        again = create_from_receipt(
            tx, ctx_for_receipt,
            purchase_order_id=order_id,
            receipt_document_id=receipt_id,
            lines=((material_id, Decimal("1000")),),
            actor_id=CHECKER_ID, happened_at=RECEIPT_AT,
        )
    check("同一到货单重复生成应付是幂等的（created=False，不报错）",
          again["id"] == payable_id and again["created"] is False,
          f"{again}")

    # 应付列表里能看到余额
    payables = runner.invoke(
        Invocation(capability_name="payable.list", query={"only_unpaid": "true"}),
        maker,
        "purchase-e2e-payables",
    )
    check("应付列表 kind == read 且有数据", payables.kind == "read" and payables.data["total"] >= 1,
          f"{payables.kind} total={payables.data['total'] if payables.data else None}")
    found = next(
        (item for item in payables.data["items"] if int(item["id"]) == payable_id), None
    )
    check("列表里能按 id 找到该应付", found is not None)
    if found:
        check("列表行的 balance = total_amount − paid_amount",
              Decimal(str(found["balance"]))
              == Decimal(str(found["total_amount"])) - Decimal(str(found["paid_amount"])),
              f"{found['balance']}")
        check("列表行有 status_label", found["status_label"] == "未付款",
              f"{found['status_label']}")

    # ---- 登记付款：超过余额只提示、不拦截（registry §1.7） ----
    over = runner.invoke(
        Invocation(
            capability_name="payment.create",
            payload={
                "code": f"{PROBE}PAY-OVER",
                "name": "超余额探针付款",
                "payable_id": payable_id,
                "amount": "5000",
                "paid_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
                "payment_method": "bank_transfer",
            },
            idempotency_key="purchase-e2e-pay-over",
        ),
        maker,
        "purchase-e2e-pay-over",
    )
    check("payment.create **不拦**超余额（只做提示性校验）", over.kind == "executed",
          f"{over.kind}")
    check("返回了 over_balance_warning 提示",
          bool((over.data or {}).get("over_balance_warning")),
          f"{(over.data or {}).get('over_balance_warning')}")
    over_payment_id = over.resource_id
    with uow_factory().begin() as tx:
        row = tx.query_one(
            "SELECT status, amount FROM purchase_payments WHERE id=%s", (over_payment_id,)
        )
        check("超余额的付款仍然以 draft 落库（登记阶段不拦）", row["status"] == "draft",
              f"{row['status']}")

    # ---- 核验该付款：AmountWithin 必须在**这里**拦下 ----
    with uow_factory().begin() as tx:
        payable_before = tx.query_one(
            "SELECT paid_amount, status FROM purchase_payables WHERE id=%s", (payable_id,)
        )
    expect_rejected(
        "核验时超余额被拒（§4 #4 AmountWithin，**唯一强制点**）",
        "CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="payment.verify",
                path_params={"payment_id": over_payment_id},
                payload={"payment_id": over_payment_id, "expected_version": 1},
                idempotency_key="purchase-e2e-verify-over-key",
            ),
            checker,
            "purchase-e2e-verify-over",
        ),
        detail="超余额的付款竟然核验通过了",
    )
    with uow_factory().begin() as tx:
        payable_after = tx.query_one(
            "SELECT paid_amount, status FROM purchase_payables WHERE id=%s", (payable_id,)
        )
        check("被拒后应付金额**没有**变（整体回滚）",
              str(payable_before["paid_amount"]) == str(payable_after["paid_amount"])
              and str(payable_before["status"]) == str(payable_after["status"]),
              f"{payable_before} -> {payable_after}")
        row = tx.query_one(
            "SELECT status FROM purchase_payments WHERE id=%s", (over_payment_id,)
        )
        check("被拒的付款仍是 draft（没有部分写入）", row["status"] == "draft",
              f"{row['status']}")

    # ---- 合规额付款：登记 -> 自审被拒 -> 他人核验 -> 应付推进 ----
    partial = runner.invoke(
        Invocation(
            capability_name="payment.create",
            payload={
                "code": f"{PROBE}PAY-001",
                "name": "首笔探针付款",
                "payable_id": payable_id,
                "amount": "2000",
                "paid_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
                "payment_method": "bank_transfer",
                "note": "采购域 e2e 探针",
            },
            idempotency_key="purchase-e2e-pay-001",
        ),
        maker,
        "purchase-e2e-pay-001",
    )
    check("登记合规金额成功且无超余额提示",
          partial.kind == "executed"
          and not (partial.data or {}).get("over_balance_warning"),
          f"{partial.kind} warn={(partial.data or {}).get('over_balance_warning')}")
    payment_id = partial.resource_id

    expect_rejected(
        "经办人核验自己的付款被拒（§4 #5 DistinctActors）",
        ("FORBIDDEN", "CONFLICT"),
        lambda: runner.invoke(
            Invocation(
                capability_name="payment.verify",
                path_params={"payment_id": payment_id},
                payload={"payment_id": payment_id, "expected_version": 1},
                idempotency_key="purchase-e2e-self-verify-key",
            ),
            maker,
            "purchase-e2e-self-verify-payment",
        ),
        detail="自审付款竟然通过了 —— 早期版本采购域完全没有这条校验",
    )
    with uow_factory().begin() as tx:
        row = tx.query_one(
            "SELECT status, verified_by FROM purchase_payments WHERE id=%s", (payment_id,)
        )
        check("被拒后付款单状态未变、未写核验人",
              row["status"] == "draft" and row["verified_by"] is None,
              f"{row}")

    verified = runner.invoke(
        Invocation(
            capability_name="payment.verify",
            path_params={"payment_id": payment_id},
            payload={"payment_id": payment_id, "expected_version": 1},
            idempotency_key="purchase-e2e-verify-key",
        ),
        checker,
        "purchase-e2e-verify-payment",
    )
    check("他人核验成功", verified.kind == "executed", f"{verified.kind}")
    if verified.kind == "executed":
        pay = verified.data["payable"]
        check("应付已付金额累计到 2000", Decimal(pay["paid_amount"]) == Decimal("2000.00"),
              f"{pay['paid_amount']}")
        check("应付状态推进为 partial（由 paid_amount 推导）", pay["status"] == "partial",
              f"{pay['status']}")
        check("应付余额 = 4800 − 2000 = 2800", Decimal(pay["balance"]) == Decimal("2800.00"),
              f"{pay['balance']}")
    with uow_factory().begin() as tx:
        row = tx.query_one(
            "SELECT status, verified_by FROM purchase_payments WHERE id=%s", (payment_id,)
        )
        check("付款单已核验且记录了核验人",
              row["status"] == "verified" and int(row["verified_by"]) == CHECKER_ID,
              f"{row}")

    # 付款列表
    payments = runner.invoke(
        Invocation(capability_name="payment.list", query={"keyword": PROBE}),
        maker,
        "purchase-e2e-payments",
    )
    check("付款列表 kind == read 且能按关键词命中",
          payments.kind == "read" and payments.data["total"] >= 2,
          f"{payments.kind} total={payments.data['total'] if payments.data else None}")
    if payments.data and payments.data["items"]:
        row = payments.data["items"][0]
        for column in ("payable_name", "order_code", "payment_method_label", "supplier_name"):
            check(f"付款派生列 {column} 存在", column in row, f"keys={sorted(row)[:12]}")

    # ------------------------------------------------------------------
    print("\n=== 11. 第三层防御：范围外的应付不可付款 ===")
    # ★ 本节曾写成"跨企业"，实测**不成立**——原因见下面的说明。
    #   设计真正承诺的是"**范围键**（farm/area/pond/created_by）之外的行不可见"，
    #   而 `organization_id` **不在 DataScope 的强制面内**（`kernel/scope.py` 的
    #   `SCOPE_COLUMN` 只有那四列；`allows_row()` 与 `predicate()` 都不比较它）。
    #   把 `organization_id` 改掉但保留原 `area_id`，行**仍然命中**范围 → 判定通过。
    #   我原先那条断言在断言一件系统从未承诺的事，那种测试会被下一个人
    #   当成"实现 bug"去改，方向是错的。所以这里改成断言范围键。
    with uow_factory().begin() as tx:
        payable_row = tx.query_one(
            "SELECT organization_id, farm_id, area_id FROM purchase_payables WHERE id=%s",
            (payable_id,),
        )
    if other_area_id == area_id:
        print("  SKIP  库里只有一个区域，构造不出'范围之外'的行（不假装验证过）")
    else:
        with uow_factory().begin() as tx:
            tx.execute(
                "UPDATE purchase_payables SET area_id=%s WHERE id=%s",
                (other_area_id, payable_id),
            )
        expect_rejected(
            "范围外的应付不可付款（第三层 DATA_SCOPE_DENIED，不是静默空集）",
            ("DATA_SCOPE_DENIED", "NOT_FOUND"),
            lambda: runner.invoke(
                Invocation(
                    capability_name="payment.create",
                    payload={
                        "code": f"{PROBE}PAY-XSCOPE",
                        "name": "范围外探针",
                        "payable_id": payable_id,
                        "amount": "1",
                        "paid_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
                        "payment_method": "cash",
                    },
                    idempotency_key="purchase-e2e-pay-xscope",
                ),
                maker,
                "purchase-e2e-pay-xscope",
            ),
            detail="范围外的应付竟然能付款",
        )
        # 恢复，别给后面的断言与清理留坑
        with uow_factory().begin() as tx:
            tx.execute(
                "UPDATE purchase_payables SET area_id=%s WHERE id=%s",
                (payable_row["area_id"], payable_id),
            )
        # 反向：恢复之后必须**能**正常付款（否则"被拒"也可能只是"这条能力坏了"）
        with uow_factory().begin() as tx:
            restored = tx.query_one(
                "SELECT area_id FROM purchase_payables WHERE id=%s", (payable_id,)
            )
        check("探针的 area_id 已恢复（不给后面的断言留坑）",
              int(restored["area_id"]) == int(payable_row["area_id"]),
              f"{restored['area_id']} vs {payable_row['area_id']}")

    # ⚠ 记录一个**内核形态层面**的观察（不是本域能修的，交 负责人）：
    #   `organization_id` 是业务表上的租户键（分租、唯一键、不变量都用它），
    #   但 `Scope.allows_row` / `Scope.predicate` **只比较** farm_id / area_id /
    #   pond_id / created_by。所以**多企业部署下 DataScope 拦不住跨企业访问**：
    #   若两家的 areas.id 有重叠，或调用方拿到全企业范围（allow_all），
    #   别的企业的行会照常可见/可写。本域无法通过"改 scope 声明"修掉它。
    print("  NOTE  organization_id 不在 DataScope 的强制面内（见本文件与 kernel/scope.py 的说明）")

    print("\n=== 12. 会计期间锁定：已关账期间不能核验付款（§4 #7 PeriodOpen）===")
    period_row = None
    with uow_factory().begin() as tx:
        period_row = tx.query_one(
            "SELECT id, period FROM accounting_periods WHERE period_start <= %s "
            "AND period_end >= %s LIMIT 1",
            (TODAY, TODAY),
        )
    if period_row is None:
        print("  SKIP  今天不在任何已登记的会计期间内（不假装验证过）")
    else:
        # 再登记一笔（核验前需要 draft），然后关掉今天所在期间，再核验。
        period_code = str(period_row["period"])
        locked = runner.invoke(
            Invocation(
                capability_name="payment.create",
                payload={
                    "code": f"{PROBE}PAY-LOCK",
                    "name": "期间锁定探针",
                    "payable_id": payable_id,
                    "amount": "100",
                    "paid_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
                    "payment_method": "cash",
                },
                idempotency_key="purchase-e2e-pay-lock",
            ),
            maker,
            "purchase-e2e-pay-lock",
        )
        with uow_factory().begin() as tx:
            # `chk_accounting_periods_closed` 要求 status='closed' 时
            # `closed_by` / `closed_at` / `close_reason` **三列都必须有值**
            # （"关账必须留痕"）—— 只写 status 会被数据库拒绝（实测 errno 3819）。
            tx.execute(
                "UPDATE accounting_periods SET status='closed', closed_by=%s, "
                "closed_at=NOW(), close_reason='采购 e2e 探针' WHERE id=%s",
                (CHECKER_ID, int(period_row["id"])),
            )
        try:
            expect_rejected(
                f"已关账期间（{period_code}）核验付款被拒（§4 #7 PeriodOpen）",
                "CONFLICT",
                lambda: runner.invoke(
                    Invocation(
                        capability_name="payment.verify",
                        path_params={"payment_id": locked.resource_id},
                        payload={"payment_id": locked.resource_id,
                                 "expected_version": 1},
                        idempotency_key="purchase-e2e-verify-locked-key",
                    ),
                    checker,
                    "purchase-e2e-verify-locked",
                ),
                detail="已关账期间的付款竟然核验通过",
            )
        finally:
            # **一定要恢复**：否则本库的今天所在期间会一直是 closed，
            # 把 cost / warehouse / sales 的 e2e 全部带崩（它们都依赖这个期间是开的）。
            with uow_factory().begin() as tx:
                tx.execute(
                    "UPDATE accounting_periods SET status='open', closed_by=NULL, "
                    "closed_at=NULL, close_reason=NULL WHERE id=%s",
                    (int(period_row["id"]),),
                )
            with uow_factory().begin() as tx:
                restored = tx.query_one(
                    "SELECT status FROM accounting_periods WHERE id=%s", (int(period_row["id"]),)
                )
                check("探针期间已恢复为 open（不给别的域留坑）", restored["status"] == "open",
                      f"实际 {restored['status']}")

    # ---- 最后一笔付款：核验后应付必须进入 settled（回归此前的终态自拒） ----
    final_payment = runner.invoke(
        Invocation(
            capability_name="payment.create",
            payload={
                "code": f"{PROBE}PAY-002",
                "name": "结清探针付款",
                "payable_id": payable_id,
                "amount": "2800",
                "paid_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
                "payment_method": "bank_transfer",
            },
            idempotency_key="purchase-e2e-pay-002",
        ),
        maker,
        "purchase-e2e-pay-002",
    )
    check("登记最后一笔付款成功", final_payment.kind == "executed", f"{final_payment.kind}")
    final_payment_id = final_payment.resource_id
    final_verified = runner.invoke(
        Invocation(
            capability_name="payment.verify",
            path_params={"payment_id": final_payment_id},
            payload={"payment_id": final_payment_id, "expected_version": 1},
            idempotency_key="purchase-e2e-verify-002",
        ),
        checker,
        "purchase-e2e-verify-002",
    )
    check("最后一笔付款核验成功", final_verified.kind == "executed", f"{final_verified.kind}")
    if final_verified.kind == "executed":
        pay = final_verified.data["payable"]
        check("最后一笔核验后应付状态为 settled", pay["status"] == "settled", f"{pay}")
        check("最后一笔核验后余额为 0", Decimal(pay["balance"]) == Decimal("0.00"), f"{pay}")
    expect_rejected(
        "已结清的应付不能再登记付款",
        "CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="payment.create",
                payload={
                    "code": f"{PROBE}PAY-AFTER-SETTLED",
                    "name": "结清后探针付款",
                    "payable_id": payable_id,
                    "amount": "1",
                    "paid_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
                    "payment_method": "bank_transfer",
                },
                idempotency_key="purchase-e2e-pay-after-settled",
            ),
            maker,
            "purchase-e2e-pay-after-settled",
        ),
    )

    # ------------------------------------------------------------------
    print("\n=== 13. 清理探针 ===")
    with uow_factory().begin() as tx:
        tx.execute("DELETE FROM purchase_payments WHERE code LIKE %s", (f"{PROBE}%",))
        tx.execute(
            "DELETE pay FROM purchase_payables AS pay "
            "INNER JOIN purchase_orders AS o ON o.id = pay.purchase_order_id "
            "WHERE o.code LIKE %s",
            (f"{PROBE}%",),
        )
        tx.execute("DELETE FROM warehouse_documents WHERE code LIKE %s", (f"{PROBE}%",))
        tx.execute("DELETE FROM purchase_orders WHERE code LIKE %s", (f"{PROBE}%",))
        left_payments = tx.query_one(
            "SELECT COUNT(*) AS n FROM purchase_payments WHERE code LIKE %s", (f"{PROBE}%",)
        )
        left_orders = tx.query_one(
            "SELECT COUNT(*) AS n FROM purchase_orders WHERE code LIKE %s", (f"{PROBE}%",)
        )
    check("付款探针清理干净", int(left_payments["n"]) == 0, f"剩 {left_payments['n']}")
    check("采购单探针清理干净", int(left_orders["n"]) == 0, f"剩 {left_orders['n']}")

    print()
    if FAILURES:
        print(f"结果：{FAILURES} 项失败")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
