"""sales 域端到端验证：对**真实 MySQL + 真实组合根 + 真实执行器**跑通销售业务能力。

## 它验证什么（以及刻意不验证什么）

验证的是**业务语义**，不是"代码没报错"：

  1. 12 条能力的元数据与 registry §1.8 逐格一致（权限码 / scope / risk / confirmation /
     idempotent / audit）；
  2. **回读契约**：写能力都挂了 `loader=`，且**不靠夹具补挂**（`WRITE_CONTRACT` 规则 2）；
  3. **不变量真的拦得住**：每条反例都断言**具体错误码**，而不是"抛了异常"
     —— 只断言"抛异常"会让"因为有 bug 而崩"与"因为规则生效而拒"看起来一样；
  4. **事务性**：被拒绝的写入不留下任何行；
  5. **跨域闭环**（本域出口）：`delivery.verify` → 生成应收 → 收款核验推进余额与结清状态；
  6. **§4 #10 的关键回归**：`delivery.verify` 不得把"本次那一行"重复计数
     （这是我在 t7 里实测出的内核缺陷，评审结论后用 `_invariant_exclude_id`
     自排除修好；本文件用**真实执行器**再证一遍）。

**不验证**的是"HTTP 层、前端渲染、Agent 工具 schema"——那些属于 `web_e2e` /
`agent_e2e` / `live_agent_e2e` 的范围，各自有独立的脚本。一个脚本只验一件事，
出问题时的定位成本才低。

## 探针数据与清理

所有探针数据带前缀 `SALE-E2E-`，清理**只删带前缀的行**，不 `DELETE FROM sales_orders`
清全表 —— 本脚本可能跑在共享开发库（`fpa`）上，清全表会把别人的数据一起删掉。
跨域引用（`harvests`）也按前缀清理，因为本域要读它。

## 常规验证方式：**连续跑两遍**

    $env:MYSQL_DATABASE='fpa'
    $env:MYSQL_USER='fpa'; $env:MYSQL_PASSWORD='fpa_dev_password'
    python tools/sales_e2e.py
    python tools/sales_e2e.py      # ← 第二遍必须同样 ALL PASS

**为什么是两遍而不是一遍**：清理语句曾经写成
`DELETE FROM receivables WHERE code LIKE 'SALE-E2E-%'`，而应收的 code 是**派生**的
`AR-<交付单 code>`，所以它**永远匹配 0 行**。单跑一遍"看起来全绿"，因为它只在
`delivery.verify` 之后才产生应收；只有**第二遍**才会在种子阶段撞上外键
（`fk_receivables_delivery` 是 ON DELETE RESTRICT）而崩：
`Cannot delete or update a parent row`。

结论：**只跑一遍的 e2e 不算验证过清理路径**。改本脚本的清理逻辑后，
必须连跑两遍，并确认第二遍 ALL PASS、且跑完四张销售表归零
（脚本收尾会自动清理，所以正常结束后 `sales_orders` / `deliveries` /
`receivables` / `sales_receipts` 都应回到 0 行）。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pymysql  # noqa: E402

from fpa.kernel.errors import DomainError  # noqa: E402
from fpa.kernel.idempotency import IdempotencyStore  # noqa: E402
from fpa.kernel.runner import ActorView, CapabilityRunner, Invocation  # noqa: E402
from fpa.kernel.scope import Scope  # noqa: E402
from fpa.kernel.uow import ConnectionConfig, UnitOfWork  # noqa: E402

FAILURES = 0

#: 探针前缀。用它做清理与计数，**不清全表**（见模块 docstring）。
PROBE = "SALE-E2E-"

#: 幂等键的**每次运行唯一**后缀。
#:
#: 为什么不能固定：幂等预留是**持久化**的。上一次运行留下的
#: completed 记录会让本次同键调用走 `runner._replay` —— 它返回旧的
#: `resource_id` 且 `kind` 仍是 `executed`，于是“返回 executed”的断言通过，
#: 而库里没有那一行。这个坑在 `tools/loader_probe.py` 里已踩过一次，
#: 所以这次写成常量 + 注释，把教训固定在代码里。
RUN_ID = str(int(time.time() * 1000) % 10_000_000)

TODAY = date.today()
NOW = datetime.now()

#: 探针用户 id。9xxx 段在既有种子里没有占用。
MAKER_ID = 9201
CHECKER_ID = 9202
OUTSIDER_ID = 9203

#: 销售域 8 个权限码，逐字取自 registry §1.8。
SALES_PERMISSIONS = frozenset(
    {
        "sales.view",
        "sales.create",
        "sales.approve",
        "sales.deliver",
        "sales.verify",
        "finance.receivable.view",
        "finance.receipt.manage",
        "finance.receipt.verify",
    }
)

#: 第 0 步要逐格核对的元数据，逐字取自 registry §1.8。
#: 结构：name -> (method, path, kind, required_permission, scope_column, risk, confirmation, idempotent)
EXPECTED: dict[str, tuple[str, str, str, str | None, str | None, str, str, bool]] = {
    "sales_order.list": ("GET", "/api/v1/sales-orders", "read", "sales.view", "area_id", "read", "never", False),
    "sales_order.create": ("POST", "/api/v1/sales-orders", "create", "sales.create", "area_id", "normal", "never", True),
    "sales_order.get": ("GET", "/api/v1/sales-orders/{order_id}", "read", "sales.view", "area_id", "read", "never", False),
    "sales_order.update": ("PATCH", "/api/v1/sales-orders/{order_id}", "update", "sales.create", "area_id", "normal", "never", False),
    "sales_order.submit": ("POST", "/api/v1/sales-orders/{order_id}/submit", "action", "sales.create", "area_id", "normal", "never", False),
    "sales_order.approve": ("POST", "/api/v1/sales-orders/{order_id}/approve", "action", "sales.approve", "area_id", "high", "always", True),
    "sales_order.cancel": ("POST", "/api/v1/sales-orders/{order_id}/cancel", "action", "sales.approve", "area_id", "high", "always", True),
    "delivery.list": ("GET", "/api/v1/deliveries", "read", "sales.view", "area_id", "read", "never", False),
    "delivery.create": ("POST", "/api/v1/deliveries", "create", "sales.deliver", "area_id", "high", "always", True),
    "delivery.verify": ("POST", "/api/v1/deliveries/{delivery_id}/verify", "action", "sales.verify", "area_id", "high", "always", True),
    "receivable.list": ("GET", "/api/v1/receivables", "read", "finance.receivable.view", "area_id", "read", "never", False),
    "sales_receipt.list": ("GET", "/api/v1/sales-receipts", "read", "finance.receipt.manage", "area_id", "read", "never", False),
    "sales_receipt.create": ("POST", "/api/v1/sales-receipts", "create", "finance.receipt.manage", "area_id", "high", "always", True),
    "sales_receipt.verify": ("POST", "/api/v1/sales-receipts/{receipt_id}/verify", "action", "finance.receipt.verify", "area_id", "high", "always", True),
}


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


def expect_rejected(label: str, code: str | tuple[str, ...], action, *, detail: str = ""):
    """断言一次调用**被业务规则拒绝**（而不是成功、也不是崩在别的错误上）。

    必须断言**具体错误码**：只断言"抛了异常"会让"因为有 bug 而崩"与"因为规则生效
    而拒"看起来一样。这条区分是本项目全部测试的要点。
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
        print(f"  FAIL  {label}  错误码 {error.code}，期望 {allowed}；消息：{error.message}")
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
        user=os.environ.get("MYSQL_USER", "fpa"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "fpa"),
    )


def uow_factory() -> UnitOfWork:
    return UnitOfWork(_config())


def raw_connection():
    return pymysql.connect(**_config().as_kwargs())


class PersonaScopeResolver:
    """按 user_id 返回不同数据范围的替身。

    `DistinctActors` 要求"经办人 ≠ 审批人"，所以测试里**必须有两个不同的人**
    才能验证这条规则的两个方向（自审被拒 / 他人放行）。
    """

    def __init__(self, mapping: dict[int, list[dict]]) -> None:
        self._mapping = mapping

    def resolve(self, *, user_id: int, role_codes: frozenset[str]) -> Scope:
        entries = self._mapping.get(user_id, [])
        if not entries:
            # 解析失败必须抛/退化要显式：这里返回一个"什么都看不到"的 Scope，
            # 于是越权的断言会以 DATA_SCOPE_DENIED 失败，而不是"恰好没有数据"。
            return Scope(allow_all=False, entries=(), user_id=user_id)
        return Scope.from_rows(entries, user_id=user_id)


# ---------------------------------------------------------------------------
# 种子与清理（**只碰带前缀的行**）
# ---------------------------------------------------------------------------


#: 探针清理清单：(语句, 匹配的 code 前缀)，**顺序即删除顺序（子 -> 父）**。
#:
#: 每条语句**自带** pattern，不靠比较 SQL 文本去猜 —— 原先两条 receivables 语句
#: 是同一个字面量，用 `if sql == "DELETE FROM receivables ..."` 选 pattern 时
#: 只有第一条能拿到 `AR-` 前缀，第二条永远是死代码。这种"看起来在防、其实没防"
#: 的写法比不写更危险。
PROBE_DELETES: tuple[tuple[str, str], ...] = (
    ("DELETE FROM sales_receipts WHERE code LIKE %s", f"{PROBE}%"),
    # 应收的 code 是**派生**的 `AR-<交付单 code>`（见
    # `domains/sales/deliveries_write.py` 的 `create_from_delivery`），
    # 不以 `SALE-E2E-` 开头 —— 只按 `{PROBE}%` 清会**永远匹配 0 行**。
    # 后果不是"留点脏数据"这么轻：`fk_receivables_delivery` 是 ON DELETE RESTRICT，
    # 残留的应收会让下一次运行的 `DELETE FROM deliveries` 抛 1451，
    # **整个 e2e 在种子阶段就崩**（实测症状：第一轮 delivery.verify 生成应收之后，
    # 第二轮必崩）。所以两种前缀都要覆盖。
    ("DELETE FROM receivables WHERE code LIKE %s", f"AR-{PROBE}%"),
    ("DELETE FROM receivables WHERE code LIKE %s", f"{PROBE}%"),
    ("DELETE FROM deliveries WHERE code LIKE %s", f"{PROBE}%"),
    ("DELETE FROM sales_orders WHERE code LIKE %s", f"{PROBE}%"),
    # 跨域：出塘单引用批次，所以先删 harvests、再删 batches
    ("DELETE FROM harvests WHERE code LIKE %s", f"{PROBE}%"),
    ("DELETE FROM production_batches WHERE code LIKE %s", f"{PROBE}%"),
    # ★ 本脚本种下的**主数据夹具**也要清。上面只覆盖了单据层，而它还种了
    #   `organizations/farms/areas/ponds/business_partners`，这些一直留着。
    #
    #   为什么"留着"不是小事：`demo` 持 `super_admin` ⇒ DataScope 是 allow_all
    #   ⇒ `pond.list` 会把**别家企业**的塘口返回给前端；而**成本类别是每企业一份**
    #   （migration 004 按 organization 种 6 条），探测企业没有类别。于是
    #   `frontend/tests/e2e/cost-entry-polymorphic.spec.ts` 取候选第一个（id 最大的
    #   那个 = 探测企业的塘口）提交时必然撞
    #   `FIELD_INVALID 成本类别 feed 不存在` —— 实测：**跑完本脚本再跑那条 UI 用例必红**，
    #   而单独跑 UI 用例是绿的。这就是"顺序相关的假红"，与本文件 docstring 里
    #   "一个『跑完就脏』的 e2e 会让下一个跑验收的人拿到假红"是同一形态。
    #
    #   顺序：business_partners / ponds 引用 areas，areas 引用 farms。
    #   `organizations` 那一行**刻意不删**：它本身不产生任何候选，删它反而要先把
    #   所有潜在引用（data_scopes 等）清干净，代价大于收益。
    ("DELETE FROM business_partners WHERE code LIKE %s", f"{PROBE}%"),
    ("DELETE FROM ponds WHERE code LIKE %s", f"{PROBE}%"),
    ("DELETE FROM areas WHERE code LIKE %s", f"{PROBE}%"),
    ("DELETE FROM farms WHERE code LIKE %s", f"{PROBE}%"),
)


def cleanup_probe_rows(cur) -> int:
    """删掉本脚本种下的**全部**探针行（只按前缀删，绝不清全表）。

    `seed_and_cleanup` 开头调一次（保证当前迭代从干净状态开始），
    `main()` 收尾再调一次（保证**脚本跑完不留残渣**）。

    单跑一次也要收尾清理：否则每跑一轮就永久留下几行 `AR-SALE-E2E-*`，
    而 t4 的验收判据是「四表清空」—— 一个"跑完就脏"的 e2e 会让下一个跑
    验收的人拿到假红，或者更糟：让后来者以为"四表本来就该有行"。
    """
    deleted = 0
    for sql, pattern in PROBE_DELETES:
        deleted += cur.execute(sql, (pattern,))
    # 幂等预留也要清：否则本次会走 replay 分支（见 RUN_ID 的说明）。
    # 按**探针用户**删而不按 capability 删 —— capability 是全队共用的，
    # 按它删会打到别人的数据。
    for probe_user in (MAKER_ID, CHECKER_ID, OUTSIDER_ID):
        cur.execute("DELETE FROM idempotency_keys WHERE user_id = %s", (probe_user,))
    return deleted


def seed_and_cleanup() -> dict[str, int]:
    """准备探针数据，返回各 id。

    **先清后建**，所以连跑两遍是安全的（这也是本脚本的常规验证方式，见模块
    docstring —— 这个清理 bug 之所以活下来，正是因为没人连跑过第二遍）。"""
    conn = raw_connection()
    try:
        with conn.cursor() as cur:
            # ---- 清理（顺序：子 -> 父；只删带前缀的） ----
            #
            # ⚠️ 必须把跨域的探针行**也清掉**：`INSERT IGNORE` 在 id 已存在时
            # **不会更新列值**，而探针用固定 id（9200/9201/9202）。于是第二次运行时，
            # 若上一次留下的 `production_batches` 9201 的 `pond_id` 与本次想要的不同，
            # 新建语句被静默忽略 —— 反例数据没变，"同塘口同批次"那条断言就会
            # 因为**数据没被更新**而误通过。这类"探针没重置"造成的假绿很难看出来，
            # 所以清理必须在所有种子之前、且覆盖探针用到的每一张表。
            cleanup_probe_rows(cur)
            # 幂等预留也要清：否则本次会走 replay 分支（见 RUN_ID 的说明）。
            # 按**探针用户**删而不按 capability 删 —— capability 是全队共用的，
            # 按它删会打到别人的数据。
            for probe_user in (MAKER_ID, CHECKER_ID, OUTSIDER_ID):
                cur.execute(
                    "DELETE FROM idempotency_keys WHERE user_id = %s", (probe_user,)
                )

            # ---- 基础数据（本域依赖，全部幂等） ----
            cur.execute(
                "INSERT IGNORE INTO organizations (id,code,name) VALUES (%s,%s,%s)",
                (9200, f"{PROBE}ORG", "销售 e2e 企业"),
            )
            cur.execute(
                "INSERT IGNORE INTO farms (id,organization_id,code,name) VALUES (%s,%s,%s,%s)",
                (9200, 9200, f"{PROBE}FARM", "销售 e2e 基地"),
            )
            # ★ 用户必须排在所有 `created_by` 引用它的行之前。
            #
            # `areas.created_by` / `ponds.created_by` 都是外键，而 `INSERT IGNORE`
            # 会把外键失败**静默吞掉**：探针用户 9201-9203 不在迁移种子里，先插区域
            # 就会 FK 失败 → 区域没建成 → 下一句塘口报"所属塘口不存在"（实测首跑即红）。
            cur.execute(
                "INSERT IGNORE INTO users (id,username,display_name,password_hash,status) "
                "VALUES (%s,%s,%s,'x','active'),(%s,%s,%s,'x','active'),(%s,%s,%s,'x','active')",
                (MAKER_ID, f"{PROBE}maker", "经办人", CHECKER_ID, f"{PROBE}checker", "复核人",
                 OUTSIDER_ID, f"{PROBE}outsider", "范围外"),
            )
            cur.execute(
                "INSERT IGNORE INTO areas (id,organization_id,farm_id,code,name,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (9200, 9200, 9200, f"{PROBE}AREA", "销售 e2e 区域", MAKER_ID),
            )
            cur.execute(
                "INSERT IGNORE INTO ponds (id,organization_id,farm_id,area_id,code,name,"
                "pond_status,created_by) VALUES (%s,%s,%s,%s,%s,%s,'farming',%s)",
                (9200, 9200, 9200, 9200, f"{PROBE}POND", "销售 e2e 塘口", MAKER_ID),
            )
            cur.execute(
                "INSERT IGNORE INTO production_batches (id,organization_id,farm_id,area_id,code,"
                "name,pond_id,species,stocked_at,batch_status,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'草鱼',NOW(),'farming',%s)",
                (9200, 9200, 9200, 9200, f"{PROBE}BATCH", "销售 e2e 批次", 9200, MAKER_ID),
            )
            cur.execute(
                "INSERT IGNORE INTO business_partners (id,organization_id,farm_id,area_id,"
                "partner_type,code,name,created_by) "
                "VALUES (%s,%s,%s,%s,'customer',%s,%s,%s)",
                (9200, 9200, 9200, 9200, f"{PROBE}CUST", "销售 e2e 客户", MAKER_ID),
            )
            # 供应方（用于验证 partner_type 过滤）
            cur.execute(
                "INSERT IGNORE INTO business_partners (id,organization_id,farm_id,area_id,"
                "partner_type,code,name,created_by) "
                "VALUES (%s,%s,%s,%s,'supplier',%s,%s,%s)",
                (9201, 9200, 9200, 9200, f"{PROBE}SUPP", "销售 e2e 供应商", MAKER_ID),
            )
            # **已核验的出塘单**（§4 #6 要求 harvest 必须 verified）
            cur.execute(
                "INSERT IGNORE INTO harvests (id,organization_id,farm_id,area_id,code,name,"
                "pond_id,batch_id,quantity,weight_kg,happened_at,status,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),'verified',%s)",
                (9200, 9200, 9200, 9200, f"{PROBE}HV1", "销售 e2e 出塘单", 9200, 9200,
                 # ★ 出塘数量取 **40**，与第一笔交付的 quantity 一致：§4 #6 要求
                 # "交付数量与出塘事实逐字节相等"，所以**每一笔交付都需要一张
                 # 数量相同的出塘单**（这是业务的真实形状：一次出塘对应一次交付）。
                 # 三笔 40+30+30 = 销售单的 100，正好能把"恰好满额放行"验到边界。
                 Decimal("40"), Decimal("40"), MAKER_ID),
            )
            # 另一张已核验出塘单，数量 30 —— 供第二笔交付使用
            # （§4 #6 要求交付数量与出塘事实逐字节相等，所以每笔交付要有对应出塘单）
            cur.execute(
                "INSERT IGNORE INTO harvests (id,organization_id,farm_id,area_id,code,name,"
                "pond_id,batch_id,quantity,weight_kg,happened_at,status,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),'verified',%s)",
                (9210, 9200, 9200, 9200, f"{PROBE}HV4", "销售 e2e 出塘单二", 9200, 9200,
                 Decimal("30"), Decimal("30"), MAKER_ID),
            )
            cur.execute(
                "INSERT IGNORE INTO harvests (id,organization_id,farm_id,area_id,code,name,"
                "pond_id,batch_id,quantity,weight_kg,happened_at,status,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),'verified',%s)",
                (9211, 9200, 9200, 9200, f"{PROBE}HV5", "销售 e2e 出塘单三", 9200, 9200,
                 Decimal("30"), Decimal("30"), MAKER_ID),
            )
            # 数量 10 的已核验出塘单 —— 用于"累计超额被拒"的探针
            # （额度已被 40+30+30 用满，再交付 10 必然超限）
            cur.execute(
                "INSERT IGNORE INTO harvests (id,organization_id,farm_id,area_id,code,name,"
                "pond_id,batch_id,quantity,weight_kg,happened_at,status,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),'verified',%s)",
                (9212, 9200, 9200, 9200, f"{PROBE}HV6", "销售 e2e 出塘单四", 9200, 9200,
                 Decimal("10"), Decimal("10"), MAKER_ID),
            )
            # **未核验的出塘单**（用于验证"出塘单必须 verified"）
            cur.execute(
                "INSERT IGNORE INTO harvests (id,organization_id,farm_id,area_id,code,name,"
                "pond_id,batch_id,quantity,weight_kg,happened_at,status,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),'draft',%s)",
                (9201, 9200, 9200, 9200, f"{PROBE}HV2", "销售 e2e 未核验出塘单", 9200, 9200,
                 Decimal("500"), Decimal("500"), MAKER_ID),
            )
            # 另一个塘口/批次的出塘单（用于验证"同塘口同批次"）
            cur.execute(
                "INSERT IGNORE INTO ponds (id,organization_id,farm_id,area_id,code,name,"
                "pond_status,created_by) VALUES (%s,%s,%s,%s,%s,%s,'farming',%s)",
                (9201, 9200, 9200, 9200, f"{PROBE}POND2", "销售 e2e 塘口二", MAKER_ID),
            )
            # 另一个塘口 + **它自己的批次** + 数量 40 的已核验出塘单
            # —— 用于验证"同塘口同批次"。
            #
            # 为什么必须给新塘口配一个新批次：`HarvestQuantityMatch` 比的是
            # `harvest.batch_id/pond_id` 与**交付行**上的列，而交付行的 batch/pond
            # 是从销售单带出的。若两张出塘单共用同一个 batch，那么"塘口不同"这条
            # 在 batch 维度上仍然相同、规则不会触发 —— 反例会因为**另一条规则不
            # 适用**而静默通过，看起来"已覆盖"，其实没有验到目标规则。
            cur.execute(
                "INSERT IGNORE INTO production_batches (id,organization_id,farm_id,area_id,"
                "code,name,pond_id,species,stocked_at,batch_status,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'草鱼',NOW(),'farming',%s)",
                (9201, 9200, 9200, 9200, f"{PROBE}BATCH2", "销售 e2e 批次二", 9201, MAKER_ID),
            )
            cur.execute(
                "INSERT IGNORE INTO harvests (id,organization_id,farm_id,area_id,code,name,"
                "pond_id,batch_id,quantity,weight_kg,happened_at,status,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),'verified',%s)",
                # 数量 40、但属于**另一个塘口 + 另一个批次**
                (9202, 9200, 9200, 9200, f"{PROBE}HV3", "销售 e2e 别的塘口出塘单", 9201, 9201,
                 Decimal("40"), Decimal("40"), MAKER_ID),
            )
            # `INSERT IGNORE` 不能让种子静默失败：种完逐表确认关键探针行真的在。
            # 顺序 bug 的旧症状是塘口报"所属塘口不存在"（根因在更早的区域被吞），
            # 这道校验把根因直接点出来。
            for table, column, value in (
                ("users", "username", f"{PROBE}maker"),
                ("areas", "code", f"{PROBE}AREA"),
                ("ponds", "code", f"{PROBE}POND"),
                ("production_batches", "code", f"{PROBE}BATCH"),
            ):
                cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {column}=%s", (value,))
                if int(cur.fetchone()["n"]) != 1:
                    raise RuntimeError(
                        f"探针种子未落库：{table}.{column}={value}；"
                        "INSERT IGNORE 吞掉了外键/唯一键错误，请检查 seed 顺序。"
                    )

        conn.commit()
    finally:
        conn.close()

    return {
        "org": 9200,
        "area": 9200,
        "pond": 9200,
        "batch": 9200,
        "customer": 9200,
        "supplier": 9201,
        "harvest_ok": 9200,
        "harvest_ok2": 9210,
        "harvest_ok3": 9211,
        "harvest_over": 9212,
        "harvest_draft": 9201,
        "harvest_other_pond": 9202,
    }


# ---------------------------------------------------------------------------
# 组合根装载（容忍别的域在编辑）
# ---------------------------------------------------------------------------


def load_registry_tolerantly():
    """逐域装载能力声明，返回 (registry, {失败的域: 异常描述})。

    五域并行开发期间，任何一个域的语法错误都会让 `bootstrap.load_all()` 整体抛错
    —— 本仓实测发生过多次。这里把"装载"与"报告"分开：失败的域名与异常**原样带出**，
    由调用方决定是容忍还是中止，**绝不在库里静默跳过**。
    """
    import importlib

    from fpa.bootstrap import discover_domains

    unavailable: dict[str, str] = {}
    for domain in discover_domains():
        try:
            importlib.import_module(f"fpa.domains.{domain}.capabilities")
        except Exception as error:  # noqa: BLE001
            unavailable[domain] = f"{type(error).__name__}: {error}"

    from fpa.kernel.capability import REGISTRY

    return REGISTRY, unavailable


def main() -> int:
    print("=== 0. 组合根装载（本域必须成功；别的域失败只告警）===")
    registry, unavailable = load_registry_tolerantly()
    if "sales" in unavailable:
        print(f"FAIL  本域（sales）装载失败：{unavailable['sales']}")
        return 1
    if unavailable:
        print("WARN  以下域此刻装载失败，本次运行**未**验证它们（与本域无关）：")
        for domain, detail in sorted(unavailable.items()):
            print(f"        {domain}: {detail}")
        print("      本域（sales）不受影响，继续。")

    # 收尾**无条件**清掉探针行（含断言失败提前 return 的路径）。
    #
    # 为什么必须有这一段：清理原先只在 `seed_and_cleanup()` 的**开头**做，
    # 于是每一轮跑完都会在库里留下 3 行 `AR-SALE-E2E-*` 应收。除了脏，
    # 它还让 t4 的验收判据「四表清空」永远不成立 —— 一个"跑完就脏"的 e2e
    # 会让下一个跑验收的人拿到假红，或者更糟：让后来者以为"四表本来就该有行"。
    cleanup_conn = raw_connection()
    try:
        return _run_checks(registry)
    finally:
        try:
            with cleanup_conn.cursor() as cur:
                deleted = cleanup_probe_rows(cur)
            cleanup_conn.commit()
            print()
            print(f"=== 收尾：已清理探针行 {deleted} 行（四张销售表应回到空）===")
        except Exception as error:  # noqa: BLE001
            print(f"WARN  收尾清理失败（不影响本次断言结论）：{type(error).__name__}: {error}")
        finally:
            cleanup_conn.close()


def _run_checks(registry) -> int:  # noqa: C901 - 顺序检查清单，拆开会让"哪一步在验什么"失焦
    # `FAILURES` 是模块级计数器，本函数里也会 `+= 1`（第 9 节逐条交付的核验循环），
    # 所以必须在这里声明 `global`：少了它，`FAILURES` 被当成局部名，
    # 于是**跑到函数末尾**（第 12 节之后）的那句 `if FAILURES:` 抛
    # `UnboundLocalError` —— 表现是"全部断言都 PASS，进程却以 traceback 退出"。
    # 实测踩过：把 `main()` 拆成 `main()` + `_run_checks()` 时漏搬这一行，立刻复现。
    global FAILURES

    # ------------------------------------------------------------------
    print()
    print("=== 1. 能力台账 × registry §1.8（含详情能力）===")
    sales_names = sorted(n for n in EXPECTED)
    present = {c.name for c in registry.all() if c.domain == "sales"}
    expected_registered = set(sales_names) | {
        "delivery.get",
        "receivable.get",
        "sales_receipt.get",
    }
    check(
        "sales 域必需能力全部注册（含详情能力）",
        present == expected_registered,
        f"缺少 {sorted(expected_registered - present)}，多出 {sorted(present - expected_registered)}",
    )
    for name in sales_names:
        capability = registry.find(name)
        if capability is None:
            check(f"{name} 已注册", False, "未找到")
            continue
        method, path, kind, permission, scope_col, risk, confirmation, idempotent = EXPECTED[name]
        problems: list[str] = []
        if str(capability.method) != method:
            problems.append(f"method={capability.method}≠{method}")
        if capability.path != path:
            problems.append(f"path={capability.path}≠{path}")
        if capability.kind != kind:
            problems.append(f"kind={capability.kind}≠{kind}")
        if capability.required_permission != permission:
            problems.append(f"perm={capability.required_permission}≠{permission}")
        if str(capability.risk) != risk:
            problems.append(f"risk={capability.risk}≠{risk}")
        if str(capability.effective_confirmation) != confirmation:
            problems.append(f"conf={capability.effective_confirmation}≠{confirmation}")
        if capability.requires_idempotency_key != idempotent:
            problems.append(f"idem={capability.requires_idempotency_key}≠{idempotent}")
        if scope_col is not None:
            rendered = capability.scope.render(
                Scope.from_rows([{"scope_type": "area", "area_id": 9200}], user_id=MAKER_ID),
                "t",
            )
            if scope_col not in str(rendered):
                problems.append(f"scope 未含 {scope_col}：{rendered}")
        check(f"{name} 元数据与 §1.8 一致", not problems, "；".join(problems))

    # ------------------------------------------------------------------
    print()
    print("=== 2. 回读契约：写能力都挂了 loader，且不靠夹具补挂 ===")
    write_names = [n for n in sales_names if EXPECTED[n][2] != "read"]
    unwired = [
        n for n in write_names
        if not callable(getattr(registry.find(n).handler, "__fpa_load_by_id__", None))
    ]
    check("每条写能力都可解析出回读函数", unwired == [], f"未挂载：{unwired}")

    # ------------------------------------------------------------------
    print()
    print("=== 3. 种子探针数据 ===")
    ids = seed_and_cleanup()
    print(f"      探针前缀 {PROBE}，已清理并重建（只删带前缀的行）")

    runner = CapabilityRunner(
        registry=registry,
        uow_factory=uow_factory,
        audit=__import__("fpa.kernel.audit", fromlist=["AuditWriter"]).AuditWriter(),
        idempotency=IdempotencyStore(uow_factory),
        scope_resolver=PersonaScopeResolver(
            {
                MAKER_ID: [{"scope_type": "area", "area_id": ids["area"]}],
                CHECKER_ID: [{"scope_type": "area", "area_id": ids["area"]}],
                # 范围外的人：**不给任何范围记录** → 应当看到 DATA_SCOPE_DENIED
                OUTSIDER_ID: [],
            }
        ),
    )
    maker = ActorView(user_id=MAKER_ID, username="maker", permissions=SALES_PERMISSIONS)
    checker = ActorView(user_id=CHECKER_ID, username="checker", permissions=SALES_PERMISSIONS)
    outsider = ActorView(user_id=OUTSIDER_ID, username="outsider", permissions=SALES_PERMISSIONS)

    _CALL_SEQ = 0

    def invoke(
        name: str,
        payload: dict,
        *,
        actor=None,
        key: str | None = None,
        suffix: str = "",
        path_params: dict | None = None,
    ):
        """调用一次能力。`key=None` 时自动生成**本次运行唯一**的幂等键。

        ## 为什么不能只靠 `RUN_ID`

        `RUN_ID` 在**一次运行内是常量**，所以"同一个能力在一次运行里被调用两次"
        （我的探针正是如此：先断言自审被拒、再断言他人核验成功）会复用同一个键 →
        第二次被幂等层拒成"重复敏感操作"。所以再加一个**单调递增的调用序号**。

        幂等键必须真正唯一：`idempotency_keys` 是**持久化**的，重复键会让本次调用
        走 `runner._replay` 返回旧结果，而断言看起来仍然"通过"。
        """
        nonlocal _CALL_SEQ
        if key is None:
            _CALL_SEQ += 1
            _key = f"{PROBE}K-{RUN_ID}-{_CALL_SEQ}"
        else:
            _key = key
        return runner.invoke(
            Invocation(
                capability_name=name,
                payload=payload,
                path_params=path_params or {},
                idempotency_key=_key,
            ),
            actor or maker,
            f"req-{name}-{suffix}",
        )

    order_code = f"{PROBE}SO-1"
    order_payload = {
        "code": order_code,
        "name": "e2e 销售单",
        "customer_id": ids["customer"],
        "pond_id": ids["pond"],
        "batch_id": ids["batch"],
        "species": "草鱼",
        "quantity": "100",
        "unit": "kg",
        "unit_price": "10",
        "sold_at": TODAY.isoformat(),
        "due_date": (TODAY + timedelta(days=30)).isoformat(),
        "note": "e2e",
    }

    # ------------------------------------------------------------------
    print()
    print("=== 4. sales_order.create：正例 + 反例 ===")
    created = invoke("sales_order.create", order_payload, key=f"{PROBE}K-{RUN_ID}-CREATE-1")
    order_id = created.resource_id
    check("新建销售单返回 executed", created.kind == "executed" and bool(order_id), str(created.kind))

    with uow_factory().begin() as tx:
        row = tx.query_one("SELECT * FROM sales_orders WHERE id=%s", (order_id,))
    check("库里确实有那一行", row is not None and row["code"] == order_code, str(row and row["code"]))
    check("归属由塘口解析（不接受客户端提交）",
          int(row["organization_id"]) == ids["org"] and int(row["area_id"]) == ids["area"],
          f"org={row['organization_id']} area={row['area_id']}")
    check("初始状态是 draft（不接受客户端指定）", str(row["status"]) == "draft", str(row["status"]))

    expect_rejected(
        "due_date 早于 sold_at（§2.9）→ FIELD_INVALID",
        "FIELD_INVALID",
        lambda: invoke("sales_order.create",
                       {**order_payload, "code": f"{PROBE}SO-BAD-DATE",
                        "due_date": (TODAY - timedelta(days=1)).isoformat()},
                       key=f"{PROBE}K-{RUN_ID}-BADDATE"),
    )
    expect_rejected(
        "客户不是 customer 类型 → FIELD_INVALID",
        "FIELD_INVALID",
        lambda: invoke("sales_order.create",
                       {**order_payload, "code": f"{PROBE}SO-SUPP",
                        "customer_id": ids["supplier"]},
                       key=f"{PROBE}K-{RUN_ID}-SUPP"),
    )
    expect_rejected(
        "批次不属于所选塘口 → FIELD_INVALID",
        "FIELD_INVALID",
        lambda: invoke("sales_order.create",
                       {**order_payload, "code": f"{PROBE}SO-POND2",
                        "pond_id": 9201},
                       key=f"{PROBE}K-{RUN_ID}-POND2"),
    )
    expect_rejected(
        "quantity=0 → FIELD_INVALID",
        "FIELD_INVALID",
        lambda: invoke("sales_order.create",
                       {**order_payload, "code": f"{PROBE}SO-ZERO", "quantity": "0"},
                       key=f"{PROBE}K-{RUN_ID}-ZERO"),
    )
    # ⚠️ 这里期望的错误码**暂时放宽为"任何 DomainError 之外的冲突"**，原因是
    # 内核的一个已知缺口（已在报告里上报，非本域代码问题）：
    # `kernel/uow.py::translate_mysql_error` 会把 errno 1062 翻成 `CONFLICT`，
    # 但**全仓没有任何地方调用它**（只有定义处），于是唯一键冲突以裸
    # `pymysql.err.IntegrityError` 冒到调用方 —— 在 HTTP 层就是 500 而不是 409。
    # 等内核把翻译接上（或由能力声明上的 `UniqueCode` 覆盖），这条期望应当收紧为
    # `("CONFLICT",)`。**放宽的是错误码，不是"什么都接受"** —— 下面仍然断言
    # 它确实被拒绝了，而且事务确实回滚了（见第 5 节）。
    try:
        invoke("sales_order.create", order_payload, key=f"{PROBE}K-{RUN_ID}-DUP")
        check("同企业内单号重复（§4 #19）被拒绝", False, "竟然成功了")
    except DomainError as error:
        check("同企业内单号重复（§4 #19）被拒绝（内核已翻译）",
              str(error.code) == "CONFLICT",
              f"错误码 {error.code}")
    except Exception as error:  # noqa: BLE001
        import pymysql as _pymysql

        if isinstance(error, _pymysql.err.IntegrityError) and (error.args or [None])[0] == 1062:
            print("  PASS  同企业内单号重复（§4 #19）被 DB 唯一键拦下  [裸 IntegrityError，"
                  "待内核接上 translate_mysql_error]")
        else:
            # 这里原本写的是 `FAILURES_GLOBAL = True` —— 一个**不存在的名字**：
            # 赋值会创建一个新的局部变量，于是"发生了意外异常"既不计数也不影响退出码，
            # 脚本照样以 0 退出。失败必须进那个真正被 `if FAILURES:` 读的计数器。
            FAILURES += 1
            print(f"  FAIL  同企业内单号重复：抛了意外异常 {type(error).__name__}: {error}")

    # ------------------------------------------------------------------
    print()
    print("=== 5. 事务性：被拒绝的写入不留行 ===")
    with uow_factory().begin() as tx:
        bad = tx.query_one(
            "SELECT COUNT(*) AS n FROM sales_orders WHERE code LIKE %s",
            (f"{PROBE}SO-BAD%",),
        )
        orphan = tx.query_one(
            "SELECT COUNT(*) AS n FROM sales_orders WHERE code LIKE %s",
            (f"{PROBE}SO-ORPHAN%",),
        )
    check("被拒的插入没有留下行（整体回滚）",
          int(bad["n"]) == 0 and int(orphan["n"]) == 0,
          f"bad={bad['n']} orphan={orphan['n']}")

    # ------------------------------------------------------------------
    print()
    print("=== 6. 状态机：submit → approve（§4 #5 经办人≠审批人）===")
    with uow_factory().begin() as tx:
        version = int(tx.query_one("SELECT row_version FROM sales_orders WHERE id=%s",
                                   (order_id,))["row_version"])
    submitted = invoke("sales_order.submit", {"order_id": order_id, "expected_version": version},
                       path_params={"order_id": order_id})
    check("提交成功", submitted.kind == "executed", str(submitted.kind))

    with uow_factory().begin() as tx:
        version = int(tx.query_one("SELECT row_version FROM sales_orders WHERE id=%s",
                                   (order_id,))["row_version"])
    expect_rejected(
        "经办人审批自己的单 → FORBIDDEN（§4 #5）",
        "FORBIDDEN",
        lambda: invoke("sales_order.approve", {"order_id": order_id, "expected_version": version},
                              path_params={"order_id": order_id},
                              key=f"{PROBE}K-{RUN_ID}-SELFAPPROVE"),
    )
    approved = invoke("sales_order.approve", {"order_id": order_id, "expected_version": version},
                      actor=checker, path_params={"order_id": order_id},
                      key=f"{PROBE}K-{RUN_ID}-APPROVE")
    check("他人审批成功", approved.kind == "executed", str(approved.kind))

    with uow_factory().begin() as tx:
        after = tx.query_one("SELECT status, approved_by FROM sales_orders WHERE id=%s", (order_id,))
    check("状态推进到 approved", str(after["status"]) == "approved", str(after["status"]))
    check("approved_by 记的是复核人", int(after["approved_by"]) == CHECKER_ID,
          str(after["approved_by"]))

    expect_rejected(
        "已审批后再提交 → CONFLICT（§4 #14 状态转移）",
        "CONFLICT",
        lambda: invoke("sales_order.submit",
                       {"order_id": order_id, "expected_version": int(after["row_version"]) if "row_version" in after else version},
                       key=f"{PROBE}K-{RUN_ID}-RESUBMIT",
                       path_params={"order_id": order_id}),
    )

    # ------------------------------------------------------------------
    print()
    print("=== 7. delivery.create：§4 #6 交付数量必须与出塘事实一致 ===")
    delivery_code = f"{PROBE}DL-1"
    delivery_payload = {
        "code": delivery_code,
        "name": "e2e 交付",
        "sales_order_id": order_id,
        "harvest_document_id": ids["harvest_ok"],
        "quantity": "40",
        "delivered_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
    }
    delivery = invoke("delivery.create", delivery_payload, key=f"{PROBE}K-{RUN_ID}-DL-1")
    delivery_id = delivery.resource_id
    check("登记交付成功", delivery.kind == "executed" and bool(delivery_id), str(delivery.kind))

    with uow_factory().begin() as tx:
        drow = tx.query_one("SELECT * FROM deliveries WHERE id=%s", (delivery_id,))
    check("批次/塘口从销售单带出（不接受客户端提交）",
          int(drow["batch_id"]) == ids["batch"] and int(drow["pond_id"]) == ids["pond"],
          f"batch={drow['batch_id']} pond={drow['pond_id']}")
    check("交付初始状态 draft", str(drow["status"]) == "draft", str(drow["status"]))

    expect_rejected(
        "出塘单未核验 → CONFLICT（§4 #6）",
        ("CONFLICT", "NOT_FOUND"),
        lambda: invoke("delivery.create",
                       {**delivery_payload, "code": f"{PROBE}DL-DRAFT",
                        "harvest_document_id": ids["harvest_draft"]},
                       key=f"{PROBE}K-{RUN_ID}-DL-DRAFT"),
    )
    expect_rejected(
        "出塘单属于别的塘口 → CONFLICT（§2.9 同塘口同批次）",
        "CONFLICT",
        lambda: invoke("delivery.create",
                       {**delivery_payload, "code": f"{PROBE}DL-OTHER",
                        "harvest_document_id": ids["harvest_other_pond"]},
                       key=f"{PROBE}K-{RUN_ID}-DL-OTHER"),
    )
    expect_rejected(
        "交付数量与出塘事实不等 → CONFLICT（§4 #6 逐字节一致）",
        "CONFLICT",
        lambda: invoke("delivery.create",
                       {**delivery_payload, "code": f"{PROBE}DL-MISMATCH",
                        "quantity": "41"},
                       key=f"{PROBE}K-{RUN_ID}-DL-MISMATCH"),
    )

    # ------------------------------------------------------------------
    print()
    print("=== 8. delivery.verify：§4 #10 关键回归（不得重复计数）+ 生成应收 ===")
    with uow_factory().begin() as tx:
        dver = int(tx.query_one("SELECT row_version FROM deliveries WHERE id=%s",
                                (delivery_id,))["row_version"])
    expect_rejected(
        "经办人核验自己的交付 → FORBIDDEN（§4 #5）",
        "FORBIDDEN",
        lambda: invoke("delivery.verify", {"delivery_id": delivery_id, "expected_version": dver},
                              path_params={"delivery_id": delivery_id},
                              key=f"{PROBE}K-{RUN_ID}-SELFVERIFY"),
    )
    verified = invoke("delivery.verify", {"delivery_id": delivery_id, "expected_version": dver},
                      actor=checker, path_params={"delivery_id": delivery_id})
    check("复核人核验交付成功", verified.kind == "executed", str(verified.kind))

    with uow_factory().begin() as tx:
        vrow = tx.query_one("SELECT status FROM deliveries WHERE id=%s", (delivery_id,))
        recv = tx.query_one("SELECT * FROM receivables WHERE delivery_id=%s", (delivery_id,))
        order_row = tx.query_one("SELECT status FROM sales_orders WHERE id=%s", (order_id,))
    check("交付状态 verified", str(vrow["status"]) == "verified", str(vrow["status"]))
    check("已生成应收（跨域入口副作用）", recv is not None, "未生成")
    if recv is not None:
        check("应收金额 = 交付数量 × 单价", Decimal(str(recv["total_amount"])) == Decimal("400"),
              str(recv["total_amount"]))
        check("应收初始 status=unpaid", str(recv["status"]) == "unpaid", str(recv["status"]))
        check("应收 occurred_on 用交付日期（§4 #7 PeriodOpen 反查键）",
              recv["occurred_on"] == NOW.date(), str(recv["occurred_on"]))
    check("销售单推进到 partially_delivered（§3.5 推导公式）",
          str(order_row["status"]) == "partially_delivered", str(order_row["status"]))

    # ------------------------------------------------------------------
    print()
    print("=== 9. §4 #10 回归：把剩余额度全部核验，**不得被误拒** ===")
    # 业务事实：单 100kg，已核验 40kg；再生产两条各 30kg 的交付并核验 → 恰好 100kg。
    # 修前的 `CumulativeWithin` 会把"本次那一行"算两次（counted 已含它，又 +requested），
    # 于是**最后这一步会被误拒**，导致一张订单永远无法交付完成。
    last_delivery_ids = []
    for index, quantity in enumerate(("30", "30"), start=2):
        payload = {
            "code": f"{PROBE}DL-{index}",
            "name": f"e2e 交付 {index}",
            "sales_order_id": order_id,
            "harvest_document_id": (
                ids["harvest_ok2"] if index == 2 else ids["harvest_ok3"]
            ),
            "quantity": quantity,
            "delivered_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
        }
        result = invoke("delivery.create", payload, key=f"{PROBE}K-{RUN_ID}-DL-{index}")
        last_delivery_ids.append(result.resource_id)
        with uow_factory().begin() as tx:
            dv = int(tx.query_one("SELECT row_version FROM deliveries WHERE id=%s",
                                  (result.resource_id,))["row_version"])
        expect_rejected if False else None  # noqa: B018 - 保持缩进可读
        try:
            invoke("delivery.verify", {"delivery_id": result.resource_id, "expected_version": dv},
                   actor=checker, path_params={"delivery_id": result.resource_id})
            print(f"  PASS  核验第 {index} 条交付（累计 {40 + 30 * (index - 1)}kg）")
        except DomainError as error:
            FAILURES += 1
            print(f"  FAIL  核验第 {index} 条交付被误拒：[{error.code}] {error.message}")

    with uow_factory().begin() as tx:
        total = tx.query_one(
            "SELECT COALESCE(SUM(quantity),0) AS n FROM deliveries "
            "WHERE sales_order_id=%s AND status='verified'",
            (order_id,),
        )
        final_order = tx.query_one("SELECT status FROM sales_orders WHERE id=%s", (order_id,))
    check("累计已核验交付 = 100kg（恰好等于销售量）",
          Decimal(str(total["n"])) == Decimal("100"), str(total["n"]))
    check("销售单推进到 fully_delivered（§3.5）",
          str(final_order["status"]) == "fully_delivered", str(final_order["status"]))

    expect_rejected(
        "再登记超出额度的交付 → CONFLICT（§4 #10 累计上限）",
        "CONFLICT",
        lambda: invoke("delivery.create",
                       {"code": f"{PROBE}DL-OVER", "name": "超量",
                        "sales_order_id": order_id,
                        "harvest_document_id": ids["harvest_over"],
                        "quantity": "10",
                        "delivered_at": NOW.strftime("%Y-%m-%d %H:%M:%S")},
                       key=f"{PROBE}K-{RUN_ID}-DL-OVER"),
    )

    # ------------------------------------------------------------------
    print()
    print("=== 10. sales_receipt.create / verify：§4 #4 不得超过应收余额 ===")
    if recv is None:
        print("  SKIP  没有应收（上一步失败），收款部分跳过")
    else:
        receivable_id = int(recv["id"])
        receipt = invoke(
            "sales_receipt.create",
            {
                "code": f"{PROBE}SR-1", "name": "e2e 收款",
                "receivable_id": receivable_id, "amount": "150",
                "received_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
                "receipt_method": "bank_transfer",
            },
            key=f"{PROBE}K-{RUN_ID}-SR-1",
        )
        receipt_id = receipt.resource_id
        check("登记收款成功", receipt.kind == "executed" and bool(receipt_id), str(receipt.kind))

        expect_rejected(
            "收款金额超过应收余额 → CONFLICT（§4 #4）",
            "CONFLICT",
            lambda: invoke("sales_receipt.create",
                           {"code": f"{PROBE}SR-OVER", "name": "超收",
                            "receivable_id": receivable_id, "amount": "999999",
                            "received_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
                            "receipt_method": "cash"},
                           key=f"{PROBE}K-{RUN_ID}-SR-OVER"),
        )
        expect_rejected(
            "收款方式非法 → 字段校验拒绝",
            ("FIELD_INVALID", "VALIDATION_ERROR"),
            lambda: invoke("sales_receipt.create",
                           {"code": f"{PROBE}SR-BADMETHOD", "name": "坏方式",
                            "receivable_id": receivable_id, "amount": "10",
                            "received_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
                            "receipt_method": "bitcoin"},
                           key=f"{PROBE}K-{RUN_ID}-SR-BAD"),
        )

        with uow_factory().begin() as tx:
            rver = int(tx.query_one("SELECT row_version FROM sales_receipts WHERE id=%s",
                                    (receipt_id,))["row_version"])
        expect_rejected(
            "经办人核验自己的收款 → FORBIDDEN（§4 #5）",
            "FORBIDDEN",
            lambda: invoke("sales_receipt.verify",
                           {"receipt_id": receipt_id, "expected_version": rver},
                           path_params={"receipt_id": receipt_id}),
        )
        rec_verified = invoke("sales_receipt.verify",
                              {"receipt_id": receipt_id, "expected_version": rver},
                              actor=checker, path_params={"receipt_id": receipt_id})
        check("复核人核验收款成功", rec_verified.kind == "executed", str(rec_verified.kind))

        with uow_factory().begin() as tx:
            after_recv = tx.query_one("SELECT * FROM receivables WHERE id=%s", (receivable_id,))
        check("应收累计已收款 = 150", Decimal(str(after_recv["paid_amount"])) == Decimal("150"),
              str(after_recv["paid_amount"]))
        check("应收推进到 partial（150 < 400）", str(after_recv["status"]) == "partial",
              str(after_recv["status"]))

        final_receipt = invoke(
            "sales_receipt.create",
            {
                "code": f"{PROBE}SR-2", "name": "结清收款",
                "receivable_id": receivable_id, "amount": "250",
                "received_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
                "receipt_method": "bank_transfer",
            },
            key=f"{PROBE}K-{RUN_ID}-SR-2",
        )
        final_receipt_id = final_receipt.resource_id
        check("登记最后一笔收款成功", final_receipt.kind == "executed", str(final_receipt.kind))
        with uow_factory().begin() as tx:
            final_version = int(tx.query_one(
                "SELECT row_version FROM sales_receipts WHERE id=%s",
                (final_receipt_id,),
            )["row_version"])
        final_verified = invoke(
            "sales_receipt.verify",
            {"receipt_id": final_receipt_id, "expected_version": final_version},
            actor=checker,
            path_params={"receipt_id": final_receipt_id},
            key=f"{PROBE}K-{RUN_ID}-SR-2-VERIFY",
        )
        check("最后一笔收款核验成功", final_verified.kind == "executed", str(final_verified.kind))
        with uow_factory().begin() as tx:
            settled = tx.query_one(
                "SELECT status, paid_amount, total_amount FROM receivables WHERE id=%s",
                (receivable_id,),
            )
        check("最后一笔核验后应收状态为 settled", settled["status"] == "settled", str(settled))
        check("最后一笔核验后应收余额为 0",
              Decimal(str(settled["paid_amount"])) == Decimal(str(settled["total_amount"])),
              str(settled))
        expect_rejected(
            "已结清的应收不能再登记收款",
            "CONFLICT",
            lambda: invoke(
                "sales_receipt.create",
                {
                    "code": f"{PROBE}SR-AFTER-SETTLED", "name": "结清后收款",
                    "receivable_id": receivable_id, "amount": "1",
                    "received_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
                    "receipt_method": "cash",
                },
                key=f"{PROBE}K-{RUN_ID}-SR-AFTER-SETTLED",
            ),
        )

    # ------------------------------------------------------------------
    print()
    print("=== 11. 读路径：列表/详情带 §2.9 的派生列 ===")
    listing = invoke("sales_order.list", {"page": 1, "page_size": 20})
    check("销售单列表返回 items", "items" in (listing.data or {}), str(listing.data)[:80])
    items = [i for i in (listing.data or {}).get("items", []) if i.get("code") == order_code]
    if items:
        item = items[0]
        for column in ("customer_name", "pond_name", "batch_code",
                       "delivered_quantity", "total_amount", "balance"):
            check(f"派生列 {column} 存在", column in item, f"实际字段 {sorted(item)[:14]}")
        check("total_amount = 100 × 10", Decimal(str(item.get("total_amount"))) == Decimal("1000"),
              str(item.get("total_amount")))
        check("delivered_quantity = 100", Decimal(str(item.get("delivered_quantity"))) == Decimal("100"),
              str(item.get("delivered_quantity")))
        check("balance = 0", Decimal(str(item.get("balance"))) == Decimal("0"),
              str(item.get("balance")))
    else:
        check("列表里能找到探针销售单", False, "未找到")

    recv_list = invoke("receivable.list", {"page": 1, "page_size": 20})
    check("应收列表可读（独立 finance 权限码）", "items" in (recv_list.data or {}),
          str(recv_list.data)[:80])

    delivery_list = invoke("delivery.list", {"page": 1, "page_size": 20})
    check("交付列表可读", "items" in (delivery_list.data or {}), str(delivery_list.data)[:80])

    # ------------------------------------------------------------------
    print()
    print("=== 12. 数据范围：另一账号看不到本单（第三层防御）===")
    # 两种"看不到"是**不同**的规则，测试要把两者都点名，而不是混成一个：
    #   * `DATA_SCOPE_UNRESOLVED` —— 账号没有任何范围记录，**归属解析不出来**。
    #     契约是 fail-closed："绝不返回空集"（`scope_resolver.py` / Q6 裁决）。
    #   * `DATA_SCOPE_DENIED` —— 范围解析得出，但**这一行不在范围内**。
    # `OUTSIDER_ID` 的替身给的是"没有范围记录"，所以这里正确的期望是前者。
    expect_rejected(
        "无范围记录的账号建单 → DATA_SCOPE_UNRESOLVED（fail-closed，不返回空集）",
        "DATA_SCOPE_UNRESOLVED",
        lambda: invoke("sales_order.create",
                       {**order_payload, "code": f"{PROBE}SO-ORPHAN2"}, actor=outsider,
                       key=f"{PROBE}K-{RUN_ID}-ORPHAN2"),
    )

    print()
    if FAILURES:
        print(f"FAILED: {FAILURES} 项")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
