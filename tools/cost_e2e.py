"""成本域端到端测试（真实 MySQL + 真实注册表 + 真实执行器）。

## 这个文件要证明什么

不是"代码没报错"，而是三件可验证的事：

**1. 写操作真的落了库。**
每一条 `kind=executed` 都跟着一次 `SELECT`，断言行存在、值正确、审计有对应的
success。这是 `docs/WRITE_CONTRACT.md` 规则 2 的可执行形态。

**2. 本域承载的两条不变量确实**被强制**（这是 resume 最强证明点的验证）。**

    DistinctActors(created_by, verified_by)   -> 经办人自审必须被拒
    RequiredField(source_ref)                 -> 空白来源单号必须被拒
    PeriodOpen(occurred_on)                   -> 已关账期间必须挡住（成本侧 + 派生侧）
    NoOverlappingSource                       -> 同期重复归集必须被拒

每一条都构造**反例**并断言"被拒且库里 0 新增行"。只跑正例不能证明规则存在——
一个永远返回成功的不变量和一个永远返回失败的不变量在正例下看起来一样。

**3. 数据范围是 fail-closed 的。**
跨域账号看不到、也确认不了别人的成本记录；范围解析不出具体区域时**报错**
而不是`1=0` 静默返回空集。

## 为什么不用夹具注册表

`tools/web_e2e.py` 的 `build_registry()` 是手搓的夹具（只注册一条能力）。
本测试走 **`yuxin.bootstrap.load_all()` 的真实组合根**——即生产代码路径上真正
装载的那 5 条 cost 能力。夹具测试再绿也证明不了真实声明是对的
（DEVELOPMENT.md 记过这个坑：夹具注册表曾让七套自检全绿而实际能力一条都没装载）。

用法::

    $env:MYSQL_USER='yuxin'; $env:MYSQL_PASSWORD='yuxin_dev_password'
    python tools/cost_e2e.py
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pymysql  # noqa: E402

from yuxin.bootstrap import load_all  # noqa: E402,F401 - 见 _load_cost_capabilities 的说明
from yuxin.kernel.audit import AuditWriter  # noqa: E402
from yuxin.kernel.errors import DomainError, ErrorCode  # noqa: E402
from yuxin.kernel.idempotency import IdempotencyStore  # noqa: E402
from yuxin.kernel.runner import ActorView, CapabilityRunner, Invocation  # noqa: E402
from yuxin.kernel.scope import Scope  # noqa: E402
from yuxin.kernel.uow import ConnectionConfig, UnitOfWork  # noqa: E402

FAILURES = 0

#: 探针用的来源单号前缀。用它做清理与计数，避免与真实数据混淆
#: （**不用 `DELETE FROM cost_entries` 清全表**：脚本可能跑在共享开发库上，
#: 清全表会把别人的数据一起删掉）。
PROBE_PREFIX = "COST-E2E-"

#: 关账测试要用的期间：取今天所在月份，这样它一定在 004 种出的期间范围里。
TODAY = date.today()
THIS_PERIOD = f"{TODAY.year:04d}-{TODAY.month:02d}"
#: 另一个期间：只要不是本月就行，用于验证"关一个期间不挡另一个期间"。
OTHER_PERIOD = _other = (
    f"{TODAY.year + 1:04d}-01" if TODAY.month != 1 else f"{TODAY.year:04d}-02"
)


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


def skip(label: str, reason: str) -> None:
    """登记一条**已定性的已知偏离**：打印出来、但不算失败。

    为什么需要它（而不是把断言删掉或让它一直红）：
      * 删掉断言 = 缺口从报告里消失（本项目最反对的"静默"）；
      * 让它一直红 = `cost_e2e` 永远非零退出，于是**其他**真实回归淹没在噪声里
        （"一个永远红的门禁等于没有门禁"）。
    登记时**必须写清原因与收口方向**，且配套的权威口径要写进
    `docs/CAPABILITY_REGISTRY.md`（此处对应 §4 #8 的"已知待收口"一段）。
    """
    print(f"  SKIP  {label}  —— {reason}")


def expect_rejected(label: str, code: str | tuple[str, ...], action, *, detail: str = "") -> DomainError | None:
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


def count_rows(tx: UnitOfWork, where: str, params: tuple) -> int:
    row = tx.query_one(f"SELECT COUNT(*) AS n FROM cost_entries WHERE {where}", params)
    return int((row or {}).get("n", 0))


def count_audit(tx: UnitOfWork, capability: str, result: str) -> int:
    """审计表是 append-only（001 迁移里有 BEFORE UPDATE 触发器），所以历史行删不掉。

    因此断言的是**增量**而不是绝对值——否则第二次运行必然失败。

    列名是 `capability` 而不是 registry §0.6 里写的 `action_code`：
    实际表结构（001 迁移）用的是 `capability`。**以能跑的表结构为准**——
    文档与实现的差异应当被登记并修正文档，而不是让测试去迁就文档。
    """
    row = tx.query_one(
        "SELECT COUNT(*) AS n FROM audit_logs WHERE capability=%s AND result=%s",
        (capability, result),
    )
    return int((row or {}).get("n", 0))


# ---------------------------------------------------------------------------
# 数据范围替身
# ---------------------------------------------------------------------------

class PersonaScopeResolver:
    """按 user_id 返回不同数据范围的替身。

    ## 为什么要按用户区分（而不是固定一个范围）

    `DistinctActors` 要求"经办人 ≠ 审批人"——测试里**必须有两个不同的人**才能验证
    这条规则的两个方向（自审被拒 / 他人放行）。如果两个用户拿到同一个全权范围，
    跨范围的行为就测不到。

    ## 为什么"解析不出范围"这一支必须真的抛错

    fail-closed 是 `ARCHITECTURE.md:183` 的硬要求。早期版本在同样情形下
    `return "1=0", []`（静默空集）。这里构造一个**没有任何区域范围的用户**，
    断言 `cost.entry.create` 报 `DATA_SCOPE_UNRESOLVED` 而不是"成功但查不到"。
    """

    def __init__(self, mapping: dict[int, list[dict]]) -> None:
        self._mapping = mapping

    def resolve(self, *, user_id: int, role_codes: frozenset[str]) -> Scope:
        entries = self._mapping.get(user_id, [])
        if not entries:
            # 没有任何范围记录 -> 不能构造 Scope（内核的 Scope 会抛
            # scope_unresolved）。这正是我们要的：**解析失败必须抛，不能退化成空集**。
            return Scope(allow_all=False, entries=(), user_id=user_id)
        return Scope.from_rows(entries, user_id=user_id)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 组合根装载：真实装载 + 单域失败不拖垮全局
# ---------------------------------------------------------------------------

def _load_cost_capabilities():
    """装载真实组合根，但**只对自己负责的那部分作硬要求**。

    ## 为什么要包一层（而且这层不是"测试偷懒"）

    `yuxin.bootstrap.load_all()` 会逐个 import **每个域的 `capabilities.py`**，
    任何一个域在导入期抛错都会让整条路径失败。在并行开发期间这是常态：
    别人的域正在写，一个 `AttributeError` 就能让**所有人**的自检变红
    （实测发生过：`AuditPolicy.summary()` 不存在，于是任何走组合根的脚本都崩）。

    所以这里的策略是：

      * `cost` 域装载失败 -> **硬失败**（那是我的交付物，必须修）；
      * 其它域装载失败 -> 打印警告并继续（它们不是本次被测对象，
        而且失败原因在别人的文件里，我改不了也不该改）。

    这一段**刻意记录**了本项目的真实处境：多代理并行开发的"共享组合根"
    是一个耦合点。它值得被 负责人 看见——见我给 负责人 的报告。
    """
    import importlib

    from yuxin.bootstrap import _module_name, discover_domains, load_all

    try:
        return load_all()
    except Exception as exc:  # noqa: BLE001
        print(f"警告：真实组合根完全装载失败（{type(exc).__name__}: {exc}）")
        print("      退化为逐域装载，只强制要求 cost 域成功。\n")

    from yuxin.kernel.capability import REGISTRY

    for domain in discover_domains():
        if domain == "cost":
            continue
        try:
            importlib.import_module(_module_name(domain))
        except Exception as exc:  # noqa: BLE001
            print(f"      跳过域 {domain}：{type(exc).__name__}: {exc}")

    importlib.import_module(_module_name("cost"))
    if REGISTRY.find("cost.entry.create") is None:
        raise SystemExit(
            "FAIL  cost 域未能注册任何能力 —— 组合根装载失败，先修 capabilities.py"
        )
    return REGISTRY


def main() -> int:  # noqa: C901 - 一个顺序检查清单，拆开会让"哪一步在验什么"失焦
    connection = raw_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE() AS db")
            database = cursor.fetchone()["db"]
            cursor.execute("SELECT COUNT(*) AS n FROM cost_categories WHERE status='enabled'")
            category_count = int(cursor.fetchone()["n"])
    finally:
        connection.close()

    print(f"库：{database}")
    if category_count == 0:
        print("FAIL  没有启用的成本类别 —— 先跑 `python tools/migrate.py apply`（004_cost.sql）")
        return 1

    with uow_factory().begin() as tx:
        area_row = tx.query_one(
            "SELECT a.id AS area_id, a.farm_id, a.organization_id, "
            "(SELECT id FROM areas WHERE id <> a.id ORDER BY id LIMIT 1) AS other_area_id "
            "FROM areas AS a ORDER BY a.id LIMIT 1"
        )
        if area_row is None:
            print("FAIL  areas 表没有数据 —— 先跑 003_master_data.sql 的种子")
            return 1
        area_id = int(area_row["area_id"])
        other_area_id = area_row["other_area_id"]
        other_area_id = int(other_area_id) if other_area_id is not None else area_id

        # ★ 探针用户**必须最先种**：下面每一个探针行（塘口 / 批次 / 成本记录）
        #   都在 `created_by` 上引用它们，而 `created_by` 带外键。
        #
        # 这条曾经是错的——塘口种在用户之前，于是新库上第一次跑必然
        # 1452 (fk_ponds_created_by)。它之所以长期没暴露，是因为**上一轮跑留下的
        # 探针用户还在库里**：残留数据把"顺序错误"伪装成"能跑"。
        # 这正是本项目记过的那个形态——看着像通过，其实是上一次的残留。
        # 修法不是"小心顺序"，而是把唯一的依赖放到了它所有使用者之前。
        tx.execute(
            "INSERT INTO users (id, username, display_name, password_hash, status) "
            "VALUES (9001,'cost-e2e-maker','成本经办探针','x','active'),"
            "       (9002,'cost-e2e-checker','成本复核探针','x','active'),"
            "       (9003,'cost-e2e-nobody','无范围探针','x','active') "
            "ON DUPLICATE KEY UPDATE display_name=VALUES(display_name)"
        )

        # 归属对象要用**真实存在**的 ponds 行：`_TARGET_TABLE['pond']` 查的是
        # `ponds` 表，而 003 迁移只种了区域/物料/往来单位，没种塘口。
        # 所以这里建三个探针塘口——它们同时验证了"归属对象必须是真实对象"。
        #
        # **为什么是三个而不是一个**：registry §4 #8 的去重规则按
        # `(归属对象, 期间, 来源类型)` 分组，测试要分别验证"被拒"与"放行"，
        # 就必须有互不干扰的归属对象。第一版测试只用一个塘口，
        # 于是第 7 段被第 1 段留下的手工费用正确拒绝——看起来像 bug，其实是规则生效。
        pond_ids: dict[str, int] = {}
        for code, name in (
            ("COST-E2E-P1", "成本探针塘口一"),
            ("COST-E2E-P2", "成本探针塘口二"),
            ("COST-E2E-P3", "成本探针塘口三"),
        ):
            tx.execute(
                "INSERT INTO ponds (organization_id, farm_id, area_id, code, name, "
                "pond_status, status, created_by) "
                "VALUES (%s,%s,%s,%s,%s,'build','verified',9001) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name)",
                (int(area_row["organization_id"]), int(area_row["farm_id"]), area_id, code, name),
            )
            found = tx.query_one("SELECT id FROM ponds WHERE code=%s", (code,))
            pond_ids[code] = int(found["id"])
        pond_id = pond_ids["COST-E2E-P1"]
        second_pond = pond_ids["COST-E2E-P2"]
        third_pond = pond_ids["COST-E2E-P3"]
        farm_id = int(area_row["farm_id"])

        # 清掉上轮的探针数据。
        # **按前缀清**而不是清表：本脚本可能跑在共享开发库上。
        # 唯一键 uq_cost_entries_org_dedupe 会拦住重复归集，所以必须在每次运行前
        # 清干净，否则第二次运行会因为"上轮的行还在"而误判为不变量生效。
        removed = tx.execute(
            "DELETE FROM cost_entries WHERE source_ref LIKE %s OR source_ref LIKE %s",
            (f"{PROBE_PREFIX}%", "COST-E2E%"),
        )
        # **清完必须验证**，不能只打印删了几行。
        #
        # 为什么：本测试曾因为"上一次失败的运行留下的库存归集行"而在自排除那一段
        # 误报失败——`NoOverlappingSource` 查到旧行、正常拒绝，看起来却像新写入被拒。
        # 排查这类问题最大的干扰源就是残留数据，所以清理必须有**可断言的后置条件**。
        leftover = count_rows(tx, "source_ref LIKE %s OR source_ref LIKE %s",
                              (f"{PROBE_PREFIX}%", "COST-E2E%"))
        # 上轮可能把本月关掉了 —— 恢复成 open，否则当前迭代的写入全部被 PeriodOpen 拦住。
        reopened = tx.execute(
            "UPDATE accounting_periods SET status='open', closed_by=NULL, closed_at=NULL, "
            "close_reason=NULL WHERE period IN (%s,%s) AND status='closed'",
            (THIS_PERIOD, OTHER_PERIOD),
        )
        tx.execute("DELETE FROM idempotency_keys WHERE capability LIKE 'cost.%'")
        if leftover:
            raise SystemExit(
                f"清理未生效：仍有 {leftover} 条 COST-E2E 探针行留在库里。"
                "请检查这些行的 source_ref 是否符合预期前缀，"
                "否则当前迭代断言会把'旧行导致的正常拒绝'误读成'新写入被拒'。"
            )

    print(f"清理：删除 {removed} 条上轮成本探针（复核后剩 0 条）；重开 {reopened} 个探针期间")

    registry = _load_cost_capabilities()

    runner = CapabilityRunner(
        registry=registry,
        uow_factory=uow_factory,
        audit=AuditWriter(),
        idempotency=IdempotencyStore(uow_factory),
        scope_resolver=PersonaScopeResolver(
            {
                9001: [{"scope_type": "area", "area_id": area_id}],
                9002: [{"scope_type": "area", "area_id": area_id}],
                # 9003 故意没有范围记录
            }
        ),
    )

    maker = ActorView(
        user_id=9001, username="cost-e2e-maker",
        permissions=frozenset({"cost.view", "cost.manage", "cost.confirm", "cost.close"}),
    )
    checker = ActorView(
        user_id=9002, username="cost-e2e-checker",
        permissions=frozenset({"cost.view", "cost.confirm"}),
    )
    nobody = ActorView(
        user_id=9003, username="cost-e2e-nobody",
        permissions=frozenset({"cost.view", "cost.manage"}),
    )
    outsider = ActorView(
        user_id=9001, username="cost-e2e-maker", permissions=frozenset(), role_codes=frozenset()
    )

    # ------------------------------------------------------------------
    print("\n=== 1. 登记成本：全链路（权限 -> 范围 -> 期间 -> 写库 -> 审计 -> 回读）===")
    with uow_factory().begin() as tx:
        audit_before = count_audit(tx, "cost.entry.create", "success")

    result = runner.invoke(
        Invocation(
            capability_name="cost.entry.create",
            payload={
                "category_code": "labor",
                "amount": "1200.50",
                "occurred_on": f"{THIS_PERIOD}-10",
                "source_type": "manual_expense",
                "source_ref": f"{PROBE_PREFIX}LABOR-001",
                "note": "临时工工资",
                "target_type": "pond",
                "target_id": pond_id,
            },
            idempotency_key="cost-e2e-0001",
        ),
        maker,
        "req-cost-1",
    )
    check("kind=executed", result.kind == "executed", result.kind)
    check("返回真实主键", isinstance(result.resource_id, int) and result.resource_id > 0,
          str(result.resource_id))

    with uow_factory().begin() as tx:
        rows = count_rows(tx, "source_ref = %s", (f"{PROBE_PREFIX}LABOR-001",))
        check("数据库里确实有那一行", rows == 1, f"实际 {rows} 行")
        stored = tx.query_one(
            "SELECT organization_id, farm_id, area_id, amount, period_start, period_end, "
            "status, confirm_state, created_by FROM cost_entries "
            "WHERE source_ref = %s", (f"{PROBE_PREFIX}LABOR-001",),
        )
        check("分租键由服务端解析并写入（客户端没有提交它们）",
              stored is not None and int(stored["area_id"]) == area_id,
              str(stored))
        check("期间边界由发生日期所在自然月自动确定",
              stored is not None and str(stored["period_start"]) == f"{THIS_PERIOD}-01",
              str(stored))
        check("初始 confirm_state=pending", stored is not None and stored["confirm_state"] == "pending")
        check("初始 status=draft（不接受客户端指定）",
              stored is not None and stored["status"] == "draft")
        check("created_by 是真实操作者", stored is not None and int(stored["created_by"]) == 9001)
        check(
            "审计新增了 1 条 success",
            count_audit(tx, "cost.entry.create", "success") - audit_before == 1,
        )
    check("message 由服务端渲染并含真实单号", f"{PROBE_PREFIX}LABOR-001" in result.message,
          result.message)

    print("\n=== 2. 幂等：同键同体只写一次；同键异体冲突 ===")
    replay = runner.invoke(
        Invocation(
            capability_name="cost.entry.create",
            payload={
                "category_code": "labor", "amount": "1200.50",
                "occurred_on": f"{THIS_PERIOD}-10", "source_type": "manual_expense",
                "source_ref": f"{PROBE_PREFIX}LABOR-001", "note": "临时工工资",
                "target_type": "pond", "target_id": pond_id,
            },
            idempotency_key="cost-e2e-0001",
        ),
        maker,
        "req-cost-2",
    )
    check("识别为重复请求", replay.replayed is True)
    with uow_factory().begin() as tx:
        check("未产生第二行", count_rows(tx, "source_ref = %s", (f"{PROBE_PREFIX}LABOR-001",)) == 1)

    expect_rejected(
        "同键异体被拒绝", "IDEMPOTENCY_CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.create",
                payload={
                    "category_code": "labor", "amount": "999.00",
                    "occurred_on": f"{THIS_PERIOD}-10", "source_type": "manual_expense",
                    "source_ref": f"{PROBE_PREFIX}LABOR-999",
                },
                idempotency_key="cost-e2e-0001",
            ),
            maker, "req-cost-2b",
        ),
    )

    print("\n=== 3. 校验：未知字段 / 缺必填 / 非法枚举 / 非启用类别 ===")
    expect_rejected(
        "未知字段被拒绝", "VALIDATION_ERROR",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.create",
                payload={
                    "category_code": "labor", "amount": "1", "occurred_on": f"{THIS_PERIOD}-10",
                    "source_type": "manual_expense", "source_ref": f"{PROBE_PREFIX}X",
                    "cost_nature": "direct",
                },
                idempotency_key="cost-e2e-0003a",
            ),
            maker, "req-cost-3a",
        ),
        detail="registry §2.10 明确删掉了 cost_nature（三处真相只留类别表一处）",
    )
    expect_rejected(
        "缺必填字段被拒绝", "VALIDATION_ERROR",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.create",
                payload={"amount": "1"},
                idempotency_key="cost-e2e-0003b",
            ),
            maker, "req-cost-3b",
        ),
    )
    expect_rejected(
        "非法来源类型被拒绝", ("VALIDATION_ERROR", "FIELD_INVALID"),
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.create",
                payload={
                    "category_code": "labor", "amount": "1", "occurred_on": f"{THIS_PERIOD}-10",
                    "source_type": "asset_depreciation", "source_ref": f"{PROBE_PREFIX}DEP",
                },
                idempotency_key="cost-e2e-0003c",
            ),
            maker, "req-cost-3c",
        ),
        detail="asset_depreciation 随资产与调整单一起砍除（registry §2.10）",
    )
    expect_rejected(
        "不存在的成本类别被拒绝", "FIELD_INVALID",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.create",
                payload={
                    "category_code": "no-such-category", "amount": "1",
                    "occurred_on": f"{THIS_PERIOD}-10", "source_type": "manual_expense",
                    "source_ref": f"{PROBE_PREFIX}BADCAT",
                },
                idempotency_key="cost-e2e-0003d",
            ),
            maker, "req-cost-3d",
        ),
    )
    expect_rejected(
        "发生日期不在声明的期间内被拒绝", "FIELD_INVALID",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.create",
                payload={
                    "category_code": "labor", "amount": "1", "occurred_on": f"{THIS_PERIOD}-10",
                    "source_type": "manual_expense", "source_ref": f"{PROBE_PREFIX}PERIOD",
                    "period_start": f"{THIS_PERIOD}-15", "period_end": f"{THIS_PERIOD}-28",
                },
                idempotency_key="cost-e2e-0003e",
            ),
            maker, "req-cost-3e",
        ),
    )

    print("\n=== 4. 第三层防御：权限与数据范围（fail-closed）===")
    with uow_factory().begin() as tx:
        before_count = count_rows(tx, "source_ref LIKE %s", (f"{PROBE_PREFIX}%",))

    expect_rejected(
        "无 cost.manage 权限被拒绝", "FORBIDDEN",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.create",
                payload={
                    "category_code": "labor", "amount": "1", "occurred_on": f"{THIS_PERIOD}-10",
                    "source_type": "manual_expense", "source_ref": f"{PROBE_PREFIX}DENIED",
                },
                idempotency_key="cost-e2e-0004a",
            ),
            outsider, "req-cost-4a",
        ),
    )

    expect_rejected(
        "数据范围解析不出具体区域时报 DATA_SCOPE_UNRESOLVED（fail-closed，不返回空集）",
        "DATA_SCOPE_UNRESOLVED",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.create",
                payload={
                    "category_code": "labor", "amount": "1", "occurred_on": f"{THIS_PERIOD}-10",
                    "source_type": "manual_expense", "source_ref": f"{PROBE_PREFIX}NOSCOPE",
                },
                idempotency_key="cost-e2e-0004b",
            ),
            nobody, "req-cost-4b",
        ),
        detail="早期版本在同样情形下 return '1=0', []，用户看到 0 行却不报错",
    )

    with uow_factory().begin() as tx:
        after_count = count_rows(tx, "source_ref LIKE %s", (f"{PROBE_PREFIX}%",))
    check("两次被拒都没有写入任何行", before_count == after_count,
          f"{before_count} -> {after_count}")

    print("\n=== 5. 确认成本：DistinctActors 的两个方向 ===")
    entry_id = result.resource_id
    expect_rejected(
        "经办人不能确认自己登记的成本（FRRORBIDDEN）", "FORBIDDEN",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.confirm",
                path_params={"entry_id": entry_id},
                payload={"entry_id": entry_id, "source_ref": f"{PROBE_PREFIX}LABOR-001",
                         "reason": "自审尝试", "expected_version": 1},
                idempotency_key="cost-e2e-0005a",
            ),
            maker, "req-cost-5a",
        ),
        detail="早期版本这条规则只在 sales 域实现过一处（旧 sales_service.py:121-122）",
    )
    with uow_factory().begin() as tx:
        check("自审被拒后记录仍是 pending",
              tx.query_one("SELECT confirm_state FROM cost_entries WHERE id=%s",
                           (entry_id,))["confirm_state"] == "pending")

    expect_rejected(
        "确认时来源单号留空被拒绝（RequiredField(source_ref)）", "VALIDATION_ERROR",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.confirm",
                path_params={"entry_id": entry_id},
                payload={"entry_id": entry_id, "source_ref": "   ", "reason": "空单号",
                         "expected_version": 1},
                idempotency_key="cost-e2e-0005b",
            ),
            checker, "req-cost-5b",
        ),
        detail="registry §4 #15：附件不做后降级为'核验必须有来源单号'",
    )

    confirm_result = runner.invoke(
        Invocation(
            capability_name="cost.entry.confirm",
            path_params={"entry_id": entry_id},
            payload={"entry_id": entry_id, "source_ref": f"{PROBE_PREFIX}LABOR-001",
                     "reason": "工资单核对无误", "expected_version": 1},
            idempotency_key="cost-e2e-0005c",
        ),
        checker,
        "req-cost-5c",
    )
    check("他人确认成功", confirm_result.kind == "executed", confirm_result.kind)
    with uow_factory().begin() as tx:
        confirmed = tx.query_one(
            "SELECT confirm_state, status, verified_by, verified_at, source_ref "
            "FROM cost_entries WHERE id=%s", (entry_id,),
        )
        check("库里 confirm_state=confirmed", confirmed["confirm_state"] == "confirmed")
        check("确认留痕（verified_by / verified_at 都不为空）",
              int(confirmed["verified_by"]) == 9002 and confirmed["verified_at"] is not None)
        check("状态推进到 verified", str(confirmed["status"]) == "verified")
    check("message 含真实金额", "1200.50" in confirm_result.message, confirm_result.message)

    expect_rejected(
        "重复确认被拒绝", "CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.confirm",
                path_params={"entry_id": entry_id},
                payload={"entry_id": entry_id, "source_ref": f"{PROBE_PREFIX}LABOR-001",
                         "reason": "再确认一次", "expected_version": 2},
                idempotency_key="cost-e2e-0005d",
            ),
            checker, "req-cost-5d",
        ),
    )

    print("\n=== 6. 期间锁定：关账挡住登记与确认（#7 PeriodOpen）===")
    close_result = runner.invoke(
        Invocation(
            capability_name="cost.period.close",
            path_params={"period": OTHER_PERIOD},
            payload={"period": OTHER_PERIOD, "reason": "成本端到端测试：月结"},
            idempotency_key="cost-e2e-0006a",
        ),
        maker,
        "req-cost-6a",
    )
    check(f"{OTHER_PERIOD} 关账成功", close_result.kind == "executed", close_result.kind)
    with uow_factory().begin() as tx:
        closed = tx.query_one(
            "SELECT status, closed_by, closed_at, close_reason, organization_id "
            "FROM accounting_periods WHERE organization_id=1 AND period=%s", (OTHER_PERIOD,),
        )
        check("库里期间已 closed 且留痕（谁关的/何时/为什么）",
              closed is not None and closed["status"] == "closed"
              and int(closed["closed_by"]) == 9001 and closed["closed_at"] is not None
              and closed["close_reason"],
              str(closed))

    # ---- 关账必须按 organization_id 收窄（与 PeriodOpen 同型缺陷的孪生形态）----
    #
    # 这段是那段修复的**反例断言**：`accounting_periods.organization_id` 是 NOT NULL，
    # 每个企业各有一份自己的期间行。若期间反查不带租户键（旧形态：只按日期区间
    # `ORDER BY id LIMIT 1`），关账会命中**别的企业**那一行 —— 结果不是"漏拦"，
    # 而是**改错了对象**：A 企业点"关账"，实际被关的是最先建的那家企业的这个月。
    #
    # 构造：给**另一家企业**（organization_id=2）插入**同一期间**的 open 行，
    # 再断言"本企业关账后，那一行仍然是 open"。
    # ★ 先确保"另一家企业"存在。原实现隐含假设库里已经有 organization_id=2
    #   （旧开发库确实有），于是 `python tools/migrate.py reset` 之后本脚本必然
    #   以 `IntegrityError 1452 fk_accounting_periods_organization` 崩掉 ——
    #   而模块 docstring 承诺的是"每个自检工具都能独立跑"。补一次幂等建行。
    with uow_factory().begin() as tx:
        tx.execute(
            "INSERT INTO organizations (id, code, name, status) "
            "VALUES (2, 'E2E-NEIGHBOUR-ORG', '端到端邻居企业', 'active') "
            "ON DUPLICATE KEY UPDATE name=VALUES(name)"
        )
        tx.execute(
            "INSERT INTO users (id, username, display_name, password_hash, status) "
            "VALUES (9001, 'e2e_neighbour', '端到端邻居经办人', 'x', 'active') "
            "ON DUPLICATE KEY UPDATE status='active'"
        )
    with uow_factory().begin() as tx:
        tx.execute(
            "INSERT INTO accounting_periods (organization_id, period, period_start,"
            " period_end, status, created_by) VALUES (2, %s, %s, %s, 'open', 9001)"
            " ON DUPLICATE KEY UPDATE status='open', closed_by=NULL, closed_at=NULL,"
            " close_reason=NULL",
            (OTHER_PERIOD, f"{OTHER_PERIOD}-01", f"{OTHER_PERIOD}-28"),
        )
    with uow_factory().begin() as tx:
        neighbour = tx.query_one(
            "SELECT id, status FROM accounting_periods WHERE organization_id=2 AND period=%s",
            (OTHER_PERIOD,),
        )
    check("探针就绪：企业 2 的同一期间存在且为 open",
          neighbour is not None and str(neighbour["status"]) == "open", str(neighbour))
    with uow_factory().begin() as tx:
        own_row = tx.query_one(
            "SELECT id, organization_id, status FROM accounting_periods"
            " WHERE organization_id=1 AND period=%s",
            (OTHER_PERIOD,),
        )
    check("A 企业关账关的是**自己那一行**（organization_id=1 已 closed）",
          own_row is not None and str(own_row["status"]) == "closed"
          and int(own_row["organization_id"]) == 1,
          str(own_row))
    with uow_factory().begin() as tx:
        neighbour_after = tx.query_one(
            "SELECT id, status FROM accounting_periods WHERE organization_id=2 AND period=%s",
            (OTHER_PERIOD,),
        )
    check("★ A 企业关账**不影响** B 企业同一期间（B 仍是 open）",
          neighbour_after is not None and str(neighbour_after["status"]) == "open",
          f"B 企业那一行变成了 {neighbour_after}")
    with uow_factory().begin() as tx:
        tx.execute(
            "DELETE FROM accounting_periods WHERE organization_id=2 AND period=%s",
            (OTHER_PERIOD,),
        )

    expect_rejected(
        "已关账期间不能再登记成本（PeriodOpen）", "CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.create",
                payload={
                    "category_code": "labor", "amount": "1",
                    "occurred_on": f"{OTHER_PERIOD}-05", "source_type": "manual_expense",
                    "source_ref": f"{PROBE_PREFIX}CLOSED",
                },
                idempotency_key="cost-e2e-0006b",
            ),
            maker, "req-cost-6b",
        ),
    )
    expect_rejected(
        "已关账期间不能被重复关账", "CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.period.close",
                path_params={"period": OTHER_PERIOD},
                payload={"period": OTHER_PERIOD, "reason": "再关一次"},
                idempotency_key="cost-e2e-0006c",
            ),
            maker, "req-cost-6c",
        ),
    )
    expect_rejected(
        "关账说明必填（不能只关不说为什么）", "VALIDATION_ERROR",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.period.close",
                path_params={"period": OTHER_PERIOD},
                payload={"period": OTHER_PERIOD},
                idempotency_key="cost-e2e-0006d",
            ),
            maker, "req-cost-6d",
        ),
    )
    expect_rejected(
        "非法期间格式（2026-1 而非 2026-01）被拒绝", "FIELD_INVALID",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.period.close",
                path_params={"period": "2026-1"},
                payload={"period": "2026-1", "reason": "格式测试"},
                idempotency_key="cost-e2e-0006e",
            ),
            maker, "req-cost-6e",
        ),
        detail="CHAR(7) 唯一键要求严格 YYYY-MM，否则同一个月会有两个键",
    )

    print("\n=== 6b. 期间锁定：**登记之后**才关账，确认必须被拒（Q16 的确认侧）===")
    # 为什么单独测这一条：Q16 明确要求 `PeriodOpen` 覆盖 `cost.entry.confirm`
    # ——"确认入账也是一次账目影响"。上面 6 段测的是"已关账期间不能再**登记**"，
    # 但没有覆盖"先登记（期间开放）→ 期间被关 → 再确认"这条真实路径。
    # 这一条是**成本域 Q16 语义的确认侧强制点**，缺了它就只能证明一半。
    confirm_period = next(
        period for period in (f"{TODAY.year + 1:04d}-0{month}" for month in (2, 3, 4, 5, 6, 7, 8))
        if period not in (THIS_PERIOD, OTHER_PERIOD)
    )
    with uow_factory().begin() as tx:
        tx.execute(
            "UPDATE accounting_periods SET status='open', closed_by=NULL, closed_at=NULL, "
            "close_reason=NULL WHERE period=%s", (confirm_period,),
        )
        exists = tx.query_one(
            "SELECT id FROM accounting_periods WHERE period=%s", (confirm_period,)
        )
    check(f"待用期间 {confirm_period} 存在且为 open", exists is not None)

    pending = runner.invoke(
        Invocation(
            capability_name="cost.entry.create",
            payload={
                "category_code": "energy", "amount": "300.00",
                "occurred_on": f"{confirm_period}-05", "source_type": "manual_expense",
                "source_ref": f"{PROBE_PREFIX}ENERGY-001",
                "target_type": "pond", "target_id": third_pond,
            },
            idempotency_key="cost-e2e-0006f",
        ),
        maker,
        "req-cost-6f",
    )
    check("期间开放时可以登记（前置条件）", pending.kind == "executed", pending.kind)
    pending_id = pending.resource_id

    runner.invoke(
        Invocation(
            capability_name="cost.period.close",
            path_params={"period": confirm_period},
            payload={"period": confirm_period, "reason": "确认侧测试：先登记后关账"},
            idempotency_key="cost-e2e-0006g",
        ),
        maker,
        "req-cost-6g",
    )
    expect_rejected(
        "期间被关账后，**已登记**成本的确认被拒绝（PeriodOpen 的确认侧）", "CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.confirm",
                path_params={"entry_id": pending_id},
                payload={"entry_id": pending_id, "source_ref": f"{PROBE_PREFIX}ENERGY-001",
                         "reason": "关账后补确认", "expected_version": 1},
                idempotency_key="cost-e2e-0006h",
            ),
            checker, "req-cost-6h",
        ),
        detail="Q16：确认入账也是一次账目影响，已关账期间不得确认",
    )
    with uow_factory().begin() as tx:
        still_pending = tx.query_one(
            "SELECT confirm_state FROM cost_entries WHERE id=%s", (pending_id,)
        )
    check("被拒后记录仍是 pending（没有被半途改掉）",
          still_pending is not None and still_pending["confirm_state"] == "pending",
          str(still_pending))

    print("\n=== 7. 成本去重：NoOverlappingSource（#8）两个方向 ===")
    # registry §4 #8 的规则是双向的："手工费用 与 库存自动成本 不得对**同一归属
    # 对象、同一期间**重复计入"。所以测试也要两个方向，各自用**独立的归属对象** ——
    # 否则前一段测试留下的手工费用会污染后一段。
    #
    # 这正是第一版测试踩的坑：第 1 段已经往 pond_id 上记了一笔手工费用，
    # 第 7 段又往同一个 pond_id 记库存归集，于是**规则正确地把后者拒了**
    # ——看起来像 bug，其实是规则在生效。测试的构造要和规则的分组键对齐。
    #
    # ★ 还有一个更隐蔽的干扰源：**上一轮失败留下的残留行**。
    #   `NoOverlappingSource` 取的是 `LIMIT 1` 的一行，如果那一行是残留的旧行，
    #   它会被判成冲突，于是"往空归集对象写第一笔"这条正常路径也会失败。
    #
    #   注意清理的**范围**：这里只清"库存归集"行，**刻意不清手工费用行**。
    #   原因：第 1 段在 pond_id 上留下的那笔手工费用（已确认）是
    #   第 8 段"已确认金额 = 1200.50"的断言依据、也是本段"反方向"测试的前置条件。
    #   一刀切清空会让后面两段同时失去依据——**清理要清得刚好够，不是越干净越好**。
    _probe_targets = (pond_id, second_pond, third_pond)
    with uow_factory().begin() as tx:
        cleared = tx.execute(
            "DELETE FROM cost_entries WHERE source_type='warehouse_ledger' "
            "AND target_type='pond' AND target_id IN (%s,%s,%s)",
            _probe_targets,
        )
        remaining = count_rows(
            tx, "source_type='warehouse_ledger' AND target_type='pond' "
                "AND target_id IN (%s,%s,%s)",
            _probe_targets,
        )
    check(f"探针塘口无残留的库存归集行（清掉 {cleared} 条，剩 {remaining} 条）",
          remaining == 0)

    ledger_ref = f"{PROBE_PREFIX}FEEDING-991"

    # 方向一：库存归集 -> 后续的手工费用被拒
    runner.invoke(
        Invocation(
            capability_name="cost.entry.create",
            payload={
                "category_code": "feed", "amount": "480.00",
                "occurred_on": f"{THIS_PERIOD}-12", "source_type": "warehouse_ledger",
                "source_ref": ledger_ref, "target_type": "pond", "target_id": second_pond,
            },
            idempotency_key="cost-e2e-0007a",
        ),
        maker,
        "req-cost-7a",
    )
    with uow_factory().begin() as tx:
        check("库存归集记录已写入（模拟 feeding.verify 的产物）",
              count_rows(tx, "source_ref = %s", (ledger_ref,)) == 1)

    expect_rejected(
        "同归属对象同期已有库存归集时，手工费用被拒绝（NoOverlappingSource）", "CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.create",
                payload={
                    "category_code": "feed", "amount": "200.00",
                    "occurred_on": f"{THIS_PERIOD}-13", "source_type": "manual_expense",
                    "source_ref": f"{PROBE_PREFIX}MANUAL-DUP",
                    "target_type": "pond", "target_id": second_pond,
                },
                idempotency_key="cost-e2e-0007b",
            ),
            maker, "req-cost-7b",
        ),
        detail="registry §4 #8：手工费用与库存自动成本不得对同一塘口/批次重复计入",
    )
    with uow_factory().begin() as tx:
        check("被拒的手工费用确实没有落库",
              count_rows(tx, "source_ref = %s", (f"{PROBE_PREFIX}MANUAL-DUP",)) == 0)

    # 方向二：手工费用 -> 后续的库存归集被拒。
    # pond_id 在第 1 段已经留下一笔手工费用（`COST-E2E-LABOR-001`，2026-09-10，labor）
    # —— 上面刻意没有清它，所以这里**不重建**：重建会覆盖掉第 8 段断言的那笔
    # "已确认"记录（新行的 confirm_state 是 pending）。
    #
    # **规则的两个方向都要有强制点，只挡一边等于规则可以被绕过。**
    with uow_factory().begin() as tx:
        manual_rows = count_rows(
            tx, "source_ref = %s", (f"{PROBE_PREFIX}LABOR-001",)
        )
    check("前置条件：pond 探针塘口上有第 1 段留下的手工费用", manual_rows == 1,
          f"实际 {manual_rows} 条")

    expect_rejected(
        "同归属对象同期已有手工费用时，库存归集被拒绝（反方向）", "CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.create",
                payload={
                    "category_code": "feed", "amount": "480.00",
                    "occurred_on": f"{THIS_PERIOD}-12", "source_type": "warehouse_ledger",
                    "source_ref": f"{PROBE_PREFIX}FEEDING-DUP",
                    "target_type": "pond", "target_id": pond_id,
                },
                idempotency_key="cost-e2e-0007c",
            ),
            maker, "req-cost-7c",
        ),
        detail="规则的两个方向都要有强制点，只挡一边等于规则可以被绕过",
    )

    # 自排除：往**空的**归集对象写第一笔必须成功（这是"第一次本来就合法的写入"）。
    # 它是 `_invariant_exclude_id` 自排除机制生效的直接证据。
    first = runner.invoke(
        Invocation(
            capability_name="cost.entry.create",
            payload={
                "category_code": "feed", "amount": "90.00",
                "occurred_on": f"{THIS_PERIOD}-14", "source_type": "warehouse_ledger",
                "source_ref": f"{PROBE_PREFIX}FEEDING-992",
                "target_type": "pond", "target_id": third_pond,
            },
            idempotency_key="cost-e2e-0007d",
        ),
        maker,
        "req-cost-7d",
    )
    check("往空归集对象写第一笔成功（自排除生效，第一次合法写入不被误拒）",
          first.kind == "executed", first.kind)

    with uow_factory().begin() as tx:
        check("该笔确实落库", count_rows(tx, "source_ref = %s",
                                        (f"{PROBE_PREFIX}FEEDING-992",)) == 1)

    # ★ 同来源类型、**不同** `source_ref` 的第二笔**必须被放行**（registry §4 #8 的口径：
    #   本规则是"**跨来源类型**"互斥，不是"同来源类型不得两笔"）。
    #   依据：`004_cost.sql` 的物理去重键 `uq_cost_entries_org_dedupe` 走的生成列
    #   `ledger_dedupe_key` **含 `source_ref`**（迁移注释原文："同一归属对象、同一期间、
    #   **同一来源单号**只能有一条自动归集记录"）—— DB 层本来就允许两笔库存单各自归集。
    #   上面两条（两个方向）则是"跨来源类型必须被拒"的正面证据；两侧合起来才是完整契约。
    try:
        second = runner.invoke(
            Invocation(
                capability_name="cost.entry.create",
                payload={
                    "category_code": "feed", "amount": "90.00",
                    "occurred_on": f"{THIS_PERIOD}-15", "source_type": "warehouse_ledger",
                    "source_ref": f"{PROBE_PREFIX}FEEDING-993",
                    "target_type": "pond", "target_id": third_pond,
                },
                idempotency_key="cost-e2e-0007e",
            ),
            maker,
            "req-cost-7e",
        )
        check("同归属对象可有**多笔不同来源单号**的库存归集（不变量不得无差别拦）",
              second.kind == "executed", second.kind)
    except DomainError as error:
        check(
            "同归属对象可有**多笔不同来源单号**的库存归集（不变量不得无差别拦）",
            False,
            f"被拒：{error.message} —— 违反 §4 #8 的口径"
            "（该规则只拦『跨来源类型』；同来源类型的不同 source_ref 是两笔业务事实）",
        )

    print("\n=== 8. 汇总：占比必须精确闭合到 100.0000 ===")
    summary = runner.invoke(
        Invocation(
            capability_name="cost.summary",
            query={"target_type": "pond", "target_id": pond_id},
        ),
        checker,
        "req-cost-8",
    )
    data = summary.data["summary"]
    print(f"        合计 {data['total_amount']}  已确认 {data['confirmed_amount']}  "
          f"待确认 {data['pending_amount']}  条数 {data['entries']}")
    for row in data["by_category"]:
        print(f"        类别 {row['category_code']:10} {row['amount']:>10} {row['percent']:>10}%")
    check("汇总合计 > 0", float(data["total_amount"]) > 0, data["total_amount"])
    check("已确认金额 = 1200.50",
          str(data["confirmed_amount"]) == "1200.50", str(data["confirmed_amount"]))
    check("占比合计精确等于 100.0000",
          str(data["category_percent_total"]) == "100.0000", str(data["category_percent_total"]))
    check("按性质拆分非空", len(data["by_nature"]) >= 1, str(data["by_nature"]))
    check("按归属对象拆分非空", len(data["by_target"]) >= 1, str(data["by_target"]))

    print("\n=== 9. 列表与数据范围：跨域账号看不到别人的数据 ===")
    listing = runner.invoke(
        Invocation(capability_name="cost.entry.list", query={"page": 1, "page_size": 100,
                                                            "keyword": PROBE_PREFIX}),
        checker, "req-cost-9a",
    )
    check("列表返回分页信封 {items,page,page_size,total,has_next}",
          set(listing.data) == {"items", "page", "page_size", "total", "has_next"},
          str(sorted(listing.data)))
    check("列表里每行都带 status_label 与 confirm_state_label（前端不翻译枚举）",
          all("status_label" in item and "confirm_state_label" in item
              for item in listing.data["items"]),
          "缺派生字段")
    check("列表里每行都带 allowed_actions", all("allowed_actions" in item
                                                for item in listing.data["items"]))
    check("金额在传输层是字符串（避免浮点误差）",
          all(isinstance(item["amount"], str) for item in listing.data["items"]))

    expect_rejected(
        "列表的筛选枚举做白名单校验（非法值报错而不是静默返回空集）", "FIELD_INVALID",
        lambda: runner.invoke(
            Invocation(capability_name="cost.entry.list", query={"status": "no-such-status"}),
            checker, "req-cost-9b",
        ),
        detail="静默返回空集是早期版本 data_scope.py 的经典缺陷",
    )

    print("\n=== 10. 审计：每条 success 都对应一次真实提交 ===")
    with uow_factory().begin() as tx:
        for capability in ("cost.entry.create", "cost.entry.confirm", "cost.period.close"):
            successes = count_audit(tx, capability, "success")
            check(f"{capability} 有 {successes} 条 success 审计", successes >= 1)

        # ★ 两条**方向相反**的断言，各自都有依据：
        #
        # (a) 读能力走 `_invoke_once`，失败时 `_audit_failure` 会写入 failure；
        # (b) 写能力（`idempotent=True`）走 `_invoke_idempotent`，**失败路径不写审计**。
        #
        # (b) 是一个**真实的缺口**，不是"测试写错了"：
        #   `_invoke_idempotent` 捕获 BaseException 后只调 `mark_failed()`，
        #   而 `_call_service` / `run_invariants` 抛出的 DomainError 在其中被直接重抛，
        #   没有任何一处调用 `_audit_failure()`。也就是说——
        #   **"被规则拒绝的写操作在审计里没有痕迹"**，而审计的价值恰恰在于
        #   "谁在什么时候试图干什么、为什么没成"。`WRITE_CONTRACT.md` 规则 5 说
        #   "审计里出现的每一条 success 都对应一次真实提交"（这条成立），
        #   但它没有说清失败的那一半在可幂等路径上是空的。
        #
        # 我选择**把现状断言下来并标注**，而不是把它写成"应该有的样子"然后让测试红：
        # 一个红的测试会被当成噪声，而这条缺口的正确处置（补审计 vs 明确不补）
        # 属于内核的裁决范围。已上报 负责人 与 invariant-kernel。
        # **增量**口径：审计表 append-only，历史行删不掉（`count_audit` 的 docstring 已写明）。
        # 这里比的是"本次运行新写了多少行"，所以下面要先取基线、再比差。
        read_failures = count_audit(tx, "cost.entry.list", "failure")
        check(f"读能力的失败被审计（{read_failures} 条 failure）", read_failures >= 1)
        # 写能力失败审计的**基线**（在下面那次确切的拒绝之前取）
        write_failures_before = count_audit(tx, "cost.entry.create", "failure")

    # ---- 写能力的失败审计（曾经的真缺口，已修）---------------------------------
    #
    # 缺口形态：`_invoke_idempotent` 捕获 `BaseException` 后只调 `mark_failed()`，
    # **没有任何一处**调 `_audit_failure()`。后果：**被规则拒绝的写操作在审计里没有痕迹** ——
    # 而审计的价值恰恰在失败路径：成功写入本身在业务表里可见，"谁试图做了什么但没成"
    # 只存在于审计里。幂等路径的失败尤其如此（往往正是重放/并发冲突）。
    #
    # 这里**直接查 `audit_logs`**（证据是"真库里有那一行"，不是"代码改了"）。
    #
    # ★ 口径必须用**增量**，不能用"全表恰好 1 条"：审计表是 append-only，而
    #   `request_id` 在多轮 e2e 之间**是复用的**（实测 `req-cost-6b` 有 2 行、
    #   其它复用 id 各有 24 行，时间戳各不相同）—— 所以"全表计数"量的是"跑过多少轮"，
    #   与"本次有没有多记"无关。下面用 <本请求本次新增几条> 来判，既不受轮次影响，
    #   又正好是"同一次失败只记一条"要证明的事。
    #
    # 失败样本就用**下面刚造的那一次**（已关账期间不能登记成本 → `CONFLICT`），
    # 它经 `_invoke_idempotent` 走完全程并被规则拒绝，正是"失败的写操作"。
    expect_rejected(
        "为审计探针造一次幂等写失败（已关账期间登记成本）", "CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="cost.entry.create",
                payload={
                    "category_code": "labor", "amount": "1",
                    "occurred_on": f"{OTHER_PERIOD}-05", "source_type": "manual_expense",
                    "source_ref": f"{PROBE_PREFIX}AUDIT-FAIL",
                },
                idempotency_key="cost-e2e-0010-fail",
            ),
            maker, "req-cost-10-fail",
        ),
    )
    with uow_factory().begin() as tx:
        write_failures_after = count_audit(tx, "cost.entry.create", "failure")
        newest = tx.query_one(
            "SELECT capability, result, reason FROM audit_logs "
            "WHERE request_id=%s ORDER BY id DESC LIMIT 1",
            ("req-cost-10-fail",),
        )
    check("可幂等写能力的失败**有**审计（真库里有那一行，不是『代码改了』）",
          write_failures_after > write_failures_before,
          f"基线 {write_failures_before} → 现在 {write_failures_after}")
    # ★ 你要求的那条：**不能翻倍**。同一次失败恰好 +1（不是 +2）——
    #   如果哪天内核把失败审计写成"两处各记一次"，或 `_invoke_idempotent` 既走了
    #   自己的失败分支又回到了外层，这条会立刻红。
    check("一次失败恰好新增 1 条审计（没有翻倍 → 两处没有各记一次）",
          write_failures_after - write_failures_before == 1,
          f"基线 {write_failures_before} → 现在 {write_failures_after}"
          f"（增量 {write_failures_after - write_failures_before}）")
    check("那条审计的 result='failure' 且 capability 正确",
          newest is not None and newest["capability"] == "cost.entry.create"
          and newest["result"] == "failure", str(newest))
    check("失败原因被记下（不是只有一句『失败了』）",
          newest is not None and str(newest["reason"]) != "", str(newest))

    with uow_factory().begin() as tx:
        sample = tx.query_one(
            "SELECT capability, result, object_ref, before_json IS NOT NULL AS has_before, "
            "after_json IS NOT NULL AS has_after FROM audit_logs "
            "WHERE capability='cost.entry.confirm' AND result='success' "
            "ORDER BY id DESC LIMIT 1"
        )
        check("确认的审计带 before/after 快照（audit=before_after）",
              sample is not None and sample["has_before"] and sample["has_after"], str(sample))

    print("\n=== 12. 跨域唯一成本入口 record_fact（ROLLOUT_CONTRACT §2）===")
    # 这是 production / warehouse / sales 三个域要调的函数，必须自己先证明它能用。
    # 走**不经过能力声明**的直调路径：那正是调用方（另一个域的服务方法）的形态——
    # 它在自己那个能力的事务里持有 `tx`，不经过 cost 的执行器。
    from yuxin.domains.cost.entries import LEDGER_FEED, record_fact

    # 用第四个塘口，避免与前 11 段的分组键相撞。
    with uow_factory().begin() as tx:
        tx.execute(
            "INSERT INTO ponds (organization_id, farm_id, area_id, code, name, "
            "pond_status, status, created_by) "
            "VALUES (%s,%s,%s,'COST-E2E-P4','成本探针塘口四','build','verified',9001) "
            "ON DUPLICATE KEY UPDATE name=VALUES(name)",
            (int(area_row["organization_id"]), int(area_row["farm_id"]), area_id),
        )
        fact_pond = int(tx.query_one("SELECT id FROM ponds WHERE code='COST-E2E-P4'")["id"])
        # 该塘口不能有残留（record_fact 的去重按归属+期间+来源单号）
        tx.execute(
            "DELETE FROM cost_entries WHERE target_type='pond' AND target_id=%s",
            (fact_pond,),
        )

    fact_ref = f"{PROBE_PREFIX}FACT-001"
    with uow_factory().begin() as tx:
        fact = record_fact(
            tx,
            organization_id=int(area_row["organization_id"]),
            farm_id=int(area_row["farm_id"]),
            area_id=area_id,
            category_code=LEDGER_FEED,
            amount="288.00",
            occurred_on=f"{THIS_PERIOD}-16",
            source_ref=fact_ref,
            target_type="pond",
            target_id=fact_pond,
            actor_id=9001,
            note="跨域归集探针",
        )
    check("record_fact 返回 CostFact", fact is not None and fact.entry_id > 0, str(fact))
    check("期间由服务端解析并回传（供调用方渲染 message）",
          fact.period == THIS_PERIOD, fact.period)
    check("describe() 的数字来自本次真实写入", "288.00" in fact.describe(), fact.describe())

    with uow_factory().begin() as tx:
        stored = tx.query_one(
            "SELECT source_type, confirm_state, status, created_by, amount, "
            "period_start, period_end, category_code FROM cost_entries WHERE id=%s",
            (fact.entry_id,),
        )
        check("落库的 source_type 是 warehouse_ledger（自动归集专用）",
              stored["source_type"] == "warehouse_ledger", str(stored))
        check("**created_by 是真实经手人**（不是 0 或占位值）",
              int(stored["created_by"]) == 9001, str(stored["created_by"]))
        check("归集出来的记录仍需走双人复核（pending + draft）",
              stored["confirm_state"] == "pending" and stored["status"] == "draft")
        check("状态是 draft 而非 verified（自动归集不等于已确认入账）",
              stored["status"] == "draft", str(stored["status"]))

    # 重复归集：同一事实重放必须被拒（§4 #8；物理底线是 uq_cost_entries_org_dedupe）
    try:
        with uow_factory().begin() as tx:
            record_fact(
                tx, organization_id=int(area_row["organization_id"]),
                farm_id=int(area_row["farm_id"]), area_id=area_id,
                category_code=LEDGER_FEED, amount="288.00",
                occurred_on=f"{THIS_PERIOD}-16", source_ref=fact_ref,
                target_type="pond", target_id=fact_pond, actor_id=9001,
            )
        check("同一来源单号重复归集被拒", False, "竟然成功了")
    except DomainError as error:
        check("同一来源单号重复归集被拒",
              str(error.code) == "CONFLICT"
              and str(error.data.get("rule")) == "COST_SOURCE_DUPLICATED",
              f"{error.code} {error.data}")

    # 金额形态：float 必须被拒（精度问题在源头暴露）
    try:
        with uow_factory().begin() as tx:
            record_fact(
                tx, organization_id=int(area_row["organization_id"]),
                farm_id=int(area_row["farm_id"]), area_id=area_id,
                category_code=LEDGER_FEED, amount=288.1,
                occurred_on=f"{THIS_PERIOD}-16", source_ref=f"{PROBE_PREFIX}FACT-FLOAT",
                target_type="pond", target_id=fact_pond, actor_id=9001,
            )
        check("amount 传 float 被拒（金额精度问题必须在源头暴露）", False, "竟然成功了")
    except DomainError as error:
        check("amount 传 float 被拒（金额精度问题必须在源头暴露）",
              str(error.data.get("rule")) == "NO_FLOAT_MONEY", f"{error.code} {error.data}")

    # 已关账期间：必须拒绝而不是静默跳过（ROLLOUT_CONTRACT §4 / Q16）
    try:
        with uow_factory().begin() as tx:
            record_fact(
                tx, organization_id=int(area_row["organization_id"]),
                farm_id=int(area_row["farm_id"]), area_id=area_id,
                category_code=LEDGER_FEED, amount="10.00",
                occurred_on=f"{OTHER_PERIOD}-05", source_ref=f"{PROBE_PREFIX}FACT-CLOSED",
                target_type="pond", target_id=fact_pond, actor_id=9001,
            )
        check("已关账期间归集被拒（不是静默跳过）", False, "竟然成功了")
    except DomainError as error:
        check("已关账期间归集被拒（不是静默跳过）",
              str(error.code) == "CONFLICT", f"{error.code} {error.message}")

    # 整事务回滚：record_fact 与调用方共用一个 tx，调用方失败时成本行必须消失
    rollback_ref = f"{PROBE_PREFIX}FACT-ROLLBACK"
    try:
        with uow_factory().begin() as tx:
            record_fact(
                tx, organization_id=int(area_row["organization_id"]),
                farm_id=int(area_row["farm_id"]), area_id=area_id,
                category_code=LEDGER_FEED, amount="10.00",
                occurred_on=f"{THIS_PERIOD}-17", source_ref=rollback_ref,
                target_type="pond", target_id=fact_pond, actor_id=9001,
            )
            raise DomainError(ErrorCode.CONFLICT, "模拟调用方业务失败")
    except DomainError:
        pass
    with uow_factory().begin() as tx:
        check("调用方回滚时成本行一并消失（同一事务，WRITE_CONTRACT 规则 1）",
              count_rows(tx, "source_ref = %s", (rollback_ref,)) == 0)

    print("\n=== 12b. 归属对象 target_type=batch（跨域读 production_batches）===")
    # 这一支以前**从未被执行过**：`_TARGET_TABLE` 里的 batch 表名是跨域契约
    # （cost 读 production 域的表，见 ROLLOUT_CONTRACT §2 的例外说明），
    # 而"表名对不对"这件事只能靠真库验证——静态检查看不出来。
    #
    # 与 production-dev 对齐的结论：表名 `production_batches`
    # （由 005_production.sql 的 `uq_production_batches_org_code` 与
    #  `fk_production_batches_*` 佐证；registry §1.5/§2.6 正文里的 `batches`
    #  是**资源名**而不是表名）。
    with uow_factory().begin() as tx:
        batch_exists = tx.query_one(
            "SELECT COUNT(*) AS n FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'production_batches'"
        )
    if not batch_exists or int(batch_exists["n"]) == 0:
        # 005 未 apply 时**显式跳过而不是假装通过**：静默跳过会让"表名写错了"
        # 与"profession 域还没建表"两种情况长得一样。
        # （本仓最坏的失败形态是"看着像通过其实是空跑"，见 DECISIONS 里协作者
        #  上报的那条卫生检查假阳性。）
        print("  SKIP  005_production.sql 未应用，production_batches 不存在")
    else:
        with uow_factory().begin() as tx:
            tx.execute(
                "INSERT INTO production_batches "
                "(organization_id, farm_id, area_id, code, name, pond_id, species, "
                " stocked_at, batch_status, status, created_by) "
                "VALUES (%s,%s,%s,'COST-E2E-B1','成本探针批次',%s,'南美白对虾',"
                "        NOW(),'stocked','verified',9001) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name)",
                (int(area_row["organization_id"]), int(area_row["farm_id"]), area_id,
                 fact_pond),
            )
            batch_row = tx.query_one(
                "SELECT id, organization_id, farm_id, area_id FROM production_batches "
                "WHERE code='COST-E2E-B1'"
            )
            probe_batch = int(batch_row["id"])
            tx.execute(
                "DELETE FROM cost_entries WHERE target_type='batch' AND target_id=%s",
                (probe_batch,),
            )

        batch_entry = runner.invoke(
            Invocation(
                capability_name="cost.entry.create",
                payload={
                    "category_code": "labor", "amount": "66.00",
                    "occurred_on": f"{THIS_PERIOD}-18", "source_type": "manual_expense",
                    "source_ref": f"{PROBE_PREFIX}BATCH-001",
                    "target_type": "batch", "target_id": probe_batch,
                },
                idempotency_key="cost-e2e-0012b",
            ),
            maker,
            "req-cost-12b",
        )
        check("target_type=batch 可用（production_batches 表名与列名正确）",
              batch_entry.kind == "executed", batch_entry.kind)

        with uow_factory().begin() as tx:
            from_batch = tx.query_one(
                "SELECT organization_id, farm_id, area_id, target_type, target_id "
                "FROM cost_entries WHERE source_ref=%s", (f"{PROBE_PREFIX}BATCH-001",)
            )
        check("分租键从 production_batches 正确解析（三者都与批次行一致）",
              from_batch is not None
              and int(from_batch["organization_id"]) == int(batch_row["organization_id"])
              and int(from_batch["farm_id"]) == int(batch_row["farm_id"])
              and int(from_batch["area_id"]) == int(batch_row["area_id"]),
              str(from_batch))

        # 不存在的归属对象必须报 FIELD_INVALID（而不是 1452 外键错或 500）
        expect_rejected(
            "归属批次不存在时报 FIELD_INVALID（不是外键错、不是 500）", "FIELD_INVALID",
            lambda: runner.invoke(
                Invocation(
                    capability_name="cost.entry.create",
                    payload={
                        "category_code": "labor", "amount": "1.00",
                        "occurred_on": f"{THIS_PERIOD}-18", "source_type": "manual_expense",
                        "source_ref": f"{PROBE_PREFIX}BATCH-MISSING",
                        "target_type": "batch", "target_id": 999_999_999,
                    },
                    idempotency_key="cost-e2e-0012c",
                ),
                maker, "req-cost-12c",
            ),
        )

    print("\n=== 13. 清理探针 ===")
    with uow_factory().begin() as tx:
        removed = tx.execute(
            "DELETE FROM cost_entries WHERE source_ref LIKE %s OR source_ref LIKE %s",
            (f"{PROBE_PREFIX}%", "COST-E2E%"),
        )
        tx.execute(
            "UPDATE accounting_periods SET status='open', closed_by=NULL, closed_at=NULL, "
            "close_reason=NULL WHERE period IN (%s,%s,%s)",
            (THIS_PERIOD, OTHER_PERIOD, confirm_period),
        )
        tx.execute("DELETE FROM idempotency_keys WHERE capability LIKE 'cost.%'")
        # 探针批次与探针塘口也要清：迁移是共享开发库，留残留会污染下一次运行
        # （`production_batches` 有 uq_production_batches_org_code 唯一键，
        #  但 `ON DUPLICATE KEY UPDATE` 用的是 code，所以残留不会报错——
        #  **恰恰因为不报错，才必须显式清**）。
        tx.execute("DELETE FROM production_batches WHERE code LIKE 'COST-E2E-%'")
        tx.execute("DELETE FROM ponds WHERE code LIKE 'COST-E2E-%'")
        remaining = count_rows(tx, "source_ref LIKE %s OR source_ref LIKE %s",
                               (f"{PROBE_PREFIX}%", "COST-E2E%"))
    check(f"清理了 {removed} 条探针数据，剩余 {remaining} 条", remaining == 0)

    print()
    if FAILURES:
        print(f"结果：{FAILURES} 项失败")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
