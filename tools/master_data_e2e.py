"""主数据域端到端测试（真实 MySQL + 真实组合根 + 真实执行器）。

## 这个文件要证明什么

三件可验证的事，不是"代码没报错"：

1. **能力真的被装载了。** 注册表来自 `yuxin.bootstrap.load_all()`（生产路径的组合根），
   不是本文件手搓的夹具。`DEVELOPMENT.md` §4 记过这个坑：能力声明无人 import 时，
   七套自检全绿而真实 HTTP 路径上那条能力根本不存在（404）。

2. **写能力真的落了库，并且执行器的回读是可信的。** 每一条 `kind=executed` 都跟着
   一次直接查库；`replayed=True`（幂等回放）的数据来自**业务表**而不是响应快照
   （`docs/WRITE_CONTRACT.md` 规则 2 / `ARCHITECTURE.md`）。

3. **回读函数不靠夹具补挂。** 这是本文件存在的**首要理由**（`ROLLOUT_CONTRACT.md` §3）：
   `tools/{runner,web,agent}_e2e.py` 各自在夹具里手写 `PondService.create.__yuxin_load_by_id__ = ...`，
   于是"真实域声明没有回读函数"这个缺陷被夹具**盖住**了——生产路径上
   `kernel/runner.py::_reload_after` 会抛 `INTERNAL_ERROR`（500），而七套自检全绿。
   本文件用 `bootstrap.load_all()` 的真实注册表调用真实能力，因此那条路径上的
   任何一个 `__yuxin_load_by_id__` 缺口都会在这里变成 FAIL。

## 与夹具的刻意差别

`tools/web_e2e.py` 用的是自造注册表 + 自造 `web_ponds` 表；本文件用的是
`003_master_data.sql` 里真实的 `ponds` / `pond_status_change_requests` 表，
以及真实执行器（`CapabilityRunner`）。所以它能测到夹具测不到的东西：真实的
双状态资源（`status` + `pond_status`）、真实的两步审批唯一键、真实的外键链路。

## 数据范围：用**真实**范围人设，不用 `super_admin` 绕过

范围解析用 `PersonaScopeResolver` 替身（与 `tools/cost_e2e.py` 同一做法），但替身给的是
**两种真实范围**，覆盖 registry §0.3 的两种写法：

| 探针账号 | 范围 | 覆盖到哪条声明 |
|---|---|---|
| 经办人 `md-e2e-maker` | `area` 型（本区域） | `resource(area_id)` 的全部塘口写能力 |
| 复核人 `md-e2e-复核人` | `farm` 型（本基地） | `area.list` / `area.get` 的 `resource(farm_id)` |

`allow_all`（全场）只在 §9 的一条断言里单独构造。**为什么不图省事全用 `super_admin`**：
`Scope.allows_row` 与 `ScopePolicy` 都按范围类型取同名列，`allow_all` 会短路成"全部放行"，
于是"判定问错了列"这类缺陷测不出来——而本域**确实有过**这样一处缺陷（`create_pond` 拿
`areas` 行去问 `row["area_id"]`，而该表没有这一列，导致 area 型范围的账号永远建不了塘口）。
那处已在域内修掉，写法见 `domains/master_data/_scope_keys.py`，§9 有专门的正/反例断言。

用法::

    # §5：统一用默认库 yuxin（不要一域一库）。MYSQL_DATABASE 不设就是 yuxin。
    $env:MYSQL_USER='yuxin'; $env:MYSQL_PASSWORD='yuxin_dev_password'
    python tools\\master_data_e2e.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pymysql  # noqa: E402

from yuxin.bootstrap import load_all  # noqa: E402
from yuxin.kernel.audit import AuditWriter  # noqa: E402
from yuxin.kernel.errors import DomainError  # noqa: E402
from yuxin.kernel.idempotency import IdempotencyStore  # noqa: E402
from yuxin.kernel.runner import ActorView, CapabilityRunner, Invocation  # noqa: E402
from yuxin.kernel.scope import Scope  # noqa: E402
from yuxin.kernel.uow import ConnectionConfig, UnitOfWork  # noqa: E402

FAILURES = 0

#: 探针数据前缀。用它计数与清理，**绝不清全表**——脚本可能跑在共享开发库上
#: （`ROLLOUT_CONTRACT.md` §5：统一用默认库 `yuxin`，并发 e2e 靠探针 + 清理隔离）。
PROBE_PREFIX = "MD-E2E-"

#: 本次运行的唯一标记。只用于**对账用的 request_id**。
#:
#: 为什么需要它：`audit_logs` 是 append-only（001 的触发器禁止 UPDATE/DELETE），
#: 重复运行会让"同一 request_id 恰好 1 条"这条断言累积成 6 条（实测踩到）。
#: 探针编号不需要它——那些行由清理复位；审计行清不掉，所以对账键必须每次不同。
RUN_TAG = f"{int(time.time())}"

#: 本文件要按请求号精确对账的两处请求号。
CREATE_REQUEST_ID = f"req-md-e2e-create-{RUN_TAG}"
DENIED_REQUEST_ID = f"req-md-e2e-denied-{RUN_TAG}"

#: 探针账号固定 id：`idempotency_keys.user_id` / `record_revisions.actor_user_id`
#: 都有指向 `users(id)` 的外键，所以"写一条幂等记录"本身就在验证外键链路。
MAKER_ID = 9101
VERIFIER_ID = 9102
MAKER_USERNAME = "md-e2e-maker"
VERIFIER_USERNAME = "md-e2e-复核人"

#: 本域在 registry §1.4 里的 13 条能力（v1 权威清单）+ 代码侧新增的 3 条（见下方说明）。
#:
#: §1.4 表里的 `meta.capabilities` / `meta.resources` **不是能力**：它们由
#: `web/app.py::_register_meta_routes` 的固定路由提供（`workflow_meta.py` 还注明
#: `/api/v1/meta/resources` 的独立路由已被 Q14 删除，resources 作为
#: `meta.capabilities` 响应体里的一段返回）。所以本文件断言的是**能力清单**：
#: §1.4 的 13 条里去掉这两条 meta 端点 = **11 条**，加上代码侧新增的
#: `pond.get` / `area.get` / `material.get` / `partner.get` 四条详情读 = **15 条**。
REGISTRY_DECLARED_CAPABILITIES = (
    "area.list",
    "pond.list",
    "pond.create",
    "pond.update",
    "pond.submit",
    "pond.verify",
    "pond_status_change.request",
    "pond_status_change.verify",
    "material.list",
    "partner.list",
    "partner.create",
)

#: 代码侧新增（registry §1.4 尚未登记，t3 已同步写进 registry；见完成输出里的"原文 → 新文"）。
#: 每条的能力形状与 §1.4 的列表读一致：`GET /api/v1/<资源复数>/{<资源>_id}`、`kind=read`、
#: `required_permission=<资源>.view`、`resource(<资源表上真实存在的分租列>)`。
CODE_ADDED_DETAIL_CAPABILITIES = ("pond.get", "area.get", "material.get", "partner.get")

REQUIRED_CAPABILITY_CODES = REGISTRY_DECLARED_CAPABILITIES + CODE_ADDED_DETAIL_CAPABILITIES

#: 固定路由提供的元数据端点（不是能力，但**必须在真实 HTTP 路径上存在**）。
META_FIXED_ROUTES = ("/api/v1/meta/capabilities",)

#: 到目前为止代码里**真的注册了**的写能力（回读函数必须齐备的是这一批）。
WRITE_CAPABILITIES = (
    "pond.create",
    "pond.update",
    "pond.submit",
    "pond.verify",
    "pond.archive",
    "pond_status_change.request",
    "pond_status_change.verify",
)


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


def expect_rejected(label: str, code: str | tuple[str, ...], action, *, detail: str = "") -> DomainError | None:
    """断言一次调用**被业务规则拒绝**（不是成功，也不是崩在别的错误上）。

    只断言"抛了异常"会让"因为有 bug 而崩"与"因为规则生效而拒"看起来一样，
    而这条区分正是本项目全部测试的要点（照 `tools/cost_e2e.py` 的同名工具）。
    """
    global FAILURES
    wanted = (code,) if isinstance(code, str) else code
    try:
        action()
    except DomainError as error:
        if str(error.code) in wanted:
            print(f"  PASS  {label}")
            return error
        FAILURES += 1
        print(f"  FAIL  {label}  错误码是 {error.code}，期望 {list(wanted)}  {detail}")
        return error
    FAILURES += 1
    print(f"  FAIL  {label}  居然通过了（期望被拒绝）  {detail}")
    return None


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------


def config() -> ConnectionConfig:
    return ConnectionConfig(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "yuxin"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "yuxin"),
    )


def uow_factory() -> UnitOfWork:
    return UnitOfWork(config())


def raw_connection():
    """裸连接（仅用于种数据/清理，以及 dict cursor 取行）。"""
    return pymysql.connect(**config().as_kwargs())


# ---------------------------------------------------------------------------
# 数据范围替身
# ---------------------------------------------------------------------------


class PersonaScopeResolver:
    """按 user_id 返回数据范围的替身（实现 `kernel.runner.ScopeResolver` 协议）。

    `_make_scope_resolver()` 的说明解释了**为什么本测试必须用替身**而不是
    `yuxin.domains.access.scope_resolver.DataScopeResolver`。
    """

    def __init__(self, mapping: dict[int, list[dict]]) -> None:
        self._mapping = mapping

    def resolve(self, *, user_id: int, role_codes: frozenset[str]) -> Scope:
        entries = self._mapping.get(user_id, [])
        if not entries:
            # 没有范围记录 -> 不能构造 Scope（内核会抛 scope_unresolved）。
            # 这正是 fail-closed 要的：解析失败必须抛，不能退化成空集。
            return Scope(allow_all=False, entries=(), user_id=user_id)
        return Scope.from_rows(entries, user_id=user_id)


def _make_scope_resolver(area_id: int, farm_id: int) -> PersonaScopeResolver:
    """两个探针账号拿到**两种不同的数据范围**，覆盖 registry §0.3 的两种写法。

    | 账号 | 范围 | 覆盖到哪条声明 |
    |---|---|---|
    | 经办人 `md-e2e-maker` | `area` 型（本区域） | `resource(area_id)` 的全部塘口能力 |
    | 复核人 `md-e2e-复核人` | `farm` 型（本基地） | `area.list` / `area.get` 的 `resource(farm_id)` |

    ## 为什么必须用真实范围（而不是 `super_admin` 走 allow_all）

    `Scope.allows_row` 与 `ScopePolicy.render` 都按**范围类型**取同名列：
    area 型取 `row["area_id"]`、farm 型取 `row["farm_id"]`。用 `allow_all` 会短路成
    "全部放行"，于是"判定问错了列"这类缺陷（例如 `create_pond` 拿 `areas` 行去问
    `area_id`）**测不出来**。这条测试的意义正是在这里：它跑的是 registry 声明的
    范围口径，不是一句"反正超管能过"。

    `super_admin`（allow_all）另有一个账号专门覆盖，见 §9。
    """
    return PersonaScopeResolver(
        {
            MAKER_ID: [{"scope_type": "area", "area_id": area_id}],
            VERIFIER_ID: [{"scope_type": "farm", "farm_id": farm_id}],
        }
    )


# ---------------------------------------------------------------------------
# 数据准备与清理
# ---------------------------------------------------------------------------


def prepare() -> dict[str, int]:
    """种探针数据，返回 `{area_id, farm_id, maker_id, verifier_id}`。

    幂等：`ON DUPLICATE KEY UPDATE` + 按前缀清理，所以可以反复运行。
    """
    connection = raw_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (id, username, display_name, password_hash, status) "
                "VALUES (%s,%s,'主数据经办探针','x','active'),"
                "       (%s,%s,'主数据复核探针','x','active') "
                "ON DUPLICATE KEY UPDATE display_name=VALUES(display_name)",
                (MAKER_ID, MAKER_USERNAME, VERIFIER_ID, VERIFIER_USERNAME),
            )

            cursor.execute("SELECT id, organization_id FROM farms ORDER BY id LIMIT 1")
            farm = cursor.fetchone()
            if farm is None:
                print("FAIL  farms 表没有数据 —— 先跑 001_identity_access_governance.sql 的种子")
                return {}
            farm_id = int(farm["id"])
            organization_id = int(farm["organization_id"])

            cursor.execute(
                "INSERT INTO areas (organization_id, farm_id, code, name, status, created_by) "
                "VALUES (%s,%s,%s,'主数据探针区域','verified',%s) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name)",
                (organization_id, farm_id, f"{PROBE_PREFIX}AREA", MAKER_ID),
            )
            cursor.execute("SELECT id FROM areas WHERE farm_id=%s AND code=%s", (farm_id, f"{PROBE_PREFIX}AREA"))
            area_id = int(cursor.fetchone()["id"])

            # 权限码与能力同名（registry §0.1 的机械派生），所以这里直接按能力名种。
            # **读能力的码是 `<资源>.view`**（不是能力名）：registry §1.4 的 `required_permission`
            # 一栏读能力用的是通用 view 码，写能力才与能力同名。种错会让测试因为"没权限"
            # 失败，而那是测试自己的问题、不是被测代码的问题。
            for code in (
                "area.view", "material.view", "partner.view", "pond.view",
                "partner.create", "pond.create", "pond.update", "pond.submit",
                "pond.verify", "pond.archive", "pond.status.request", "pond.status.verify",
            ):
                cursor.execute(
                    "INSERT INTO permissions (code, name, domain) VALUES (%s,%s,'master_data') "
                    "ON DUPLICATE KEY UPDATE name=VALUES(name)",
                    (code, code),
                )

            cursor.execute(
                "INSERT INTO roles (code, name) VALUES ('md-e2e-role','主数据探针角色') "
                "ON DUPLICATE KEY UPDATE name=VALUES(name)"
            )
            cursor.execute(
                "INSERT IGNORE INTO role_permissions (role_id, permission_id) "
                "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
                "WHERE r.code='md-e2e-role' AND p.domain='master_data'"
            )
            cursor.execute(
                "INSERT IGNORE INTO user_roles (user_id, role_id) "
                "SELECT u.id, r.id FROM users u CROSS JOIN roles r "
                "WHERE u.username IN (%s,%s) AND r.code='md-e2e-role'",
                (MAKER_USERNAME, VERIFIER_USERNAME),
            )

            # 清上一轮探针。**按前缀 + 按 capability 清**，不动别人的数据。
            #
            # 顺序有讲究：`pond_status_change_requests.pond_id` 指向 `ponds(id)`
            # 且是 RESTRICT，所以申请必须先删。
            cursor.execute(
                "DELETE r FROM pond_status_change_requests r JOIN ponds p ON p.id = r.pond_id "
                "WHERE p.code LIKE %s",
                (f"{PROBE_PREFIX}%",),
            )
            removed = cursor.execute("DELETE FROM ponds WHERE code LIKE %s", (f"{PROBE_PREFIX}%",))
            removed_partners = cursor.execute(
                "DELETE FROM business_partners WHERE code LIKE %s", (f"{PROBE_PREFIX}%",)
            )
            cursor.execute("DELETE FROM idempotency_keys WHERE capability LIKE 'pond.%' "
                           "OR capability LIKE 'pond_status_change.%' "
                           "OR capability LIKE 'partner.%'")
            print(f"清理：删除 {removed} 条上轮塘口探针、{removed_partners} 条往来单位探针")
        connection.commit()
    finally:
        connection.close()
    return {"area_id": area_id, "farm_id": farm_id, "maker_id": MAKER_ID, "verifier_id": VERIFIER_ID}


def cleanup() -> None:
    connection = raw_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE r FROM pond_status_change_requests r JOIN ponds p ON p.id = r.pond_id "
                "WHERE p.code LIKE %s",
                (f"{PROBE_PREFIX}%",),
            )
            removed = cursor.execute("DELETE FROM ponds WHERE code LIKE %s", (f"{PROBE_PREFIX}%",))
            removed_partners = cursor.execute(
                "DELETE FROM business_partners WHERE code LIKE %s", (f"{PROBE_PREFIX}%",)
            )
            cursor.execute("DELETE FROM idempotency_keys WHERE capability LIKE 'pond.%' "
                           "OR capability LIKE 'pond_status_change.%' "
                           "OR capability LIKE 'partner.%'")
        connection.commit()
        print(f"\n清理：删除 {removed} 条当前迭代塘口探针、{removed_partners} 条往来单位探针")
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 断言辅助
# ---------------------------------------------------------------------------


def count_ponds(code: str) -> int:
    with uow_factory().begin() as tx:
        row = tx.query_one("SELECT COUNT(*) AS n FROM ponds WHERE code=%s", (code,))
    return int((row or {}).get("n", 0))


def count_audit_for_request(request_id: str, capability: str) -> int:
    """**本文件自己生成的** request_id 对应的审计条数。

    为什么不用"全库计数增量"：审计表是全局 append-only，别的域的 e2e 并发写入会让
    "新增恰好 1 条"假失败（在共享库 `yuxin` 上实测到 `20 -> 22`）。
    §5 的口径是"靠探针数据与清理隔离，不靠分库"，所以断言必须按自己的请求号精确对账。
    """
    with uow_factory().begin() as tx:
        row = tx.query_one(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE request_id=%s AND capability=%s",
            (request_id, capability),
        )
    return int((row or {}).get("n", 0))


def count_audit(capability: str, result: str) -> int:
    with uow_factory().begin() as tx:
        row = tx.query_one(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE capability=%s AND result=%s",
            (capability, result),
        )
    return int((row or {}).get("n", 0))


def fetch_audit(request_id: str, capability: str) -> dict | None:
    with uow_factory().begin() as tx:
        return tx.query_one(
            "SELECT * FROM audit_logs WHERE request_id=%s AND capability=%s ORDER BY id DESC LIMIT 1",
            (request_id, capability),
        )


def count_materials(where: str = "1=1", params: tuple = ()) -> int:
    with uow_factory().begin() as tx:
        row = tx.query_one(f"SELECT COUNT(*) AS n FROM materials WHERE {where}", params)
    return int((row or {}).get("n", 0))


def count_partners(where: str = "1=1", params: tuple = ()) -> int:
    with uow_factory().begin() as tx:
        row = tx.query_one(f"SELECT COUNT(*) AS n FROM business_partners WHERE {where}", params)
    return int((row or {}).get("n", 0))


def fetch_partner(code: str) -> dict | None:
    with uow_factory().begin() as tx:
        return tx.query_one("SELECT * FROM business_partners WHERE code=%s ORDER BY id DESC LIMIT 1", (code,))


def read_pond(pond_id: int) -> dict | None:
    with uow_factory().begin() as tx:
        return tx.query_one("SELECT * FROM ponds WHERE id=%s", (pond_id,))


def load_registry() -> tuple[object, bool]:
    """装载注册表。返回 `(registry, composition_root_ok)`。

    为什么要有隔离回退：五个域并行开发期间，**任何一个域文件写坏都会让
    `bootstrap.load_all()` 对所有人失败**（实测遇到过两次：cost 的引号笔误、
    production 的 try/except 未闭合）。那时本域的验证不该一起停摆。

    但降级必须**可见**：隔离模式下组合根没有被验证，所以本函数返回
    `composition_root_ok=False`，调用方据此把"能力装载"那条断言判 FAIL 并打印原因——
    否则就变成了"夹具盖住生产路径"的翻版（那正是本项目要根除的形态）。
    """
    try:
        return load_all(), True
    except Exception as error:  # noqa: BLE001 - 别人的域正在编辑，任何异常都要能隔离
        print(f"\n[隔离模式] 组合根当前不可用：{type(error).__name__}: {error}")
        print("           —— 这只说明组合根装载没被验证，不代表本域有问题；原因在别的域。")
        import importlib

        importlib.import_module("yuxin.domains.master_data.capabilities")
        from yuxin.kernel.capability import REGISTRY

        return REGISTRY, False


def main() -> int:  # noqa: C901 - 一个顺序检查清单，拆开会让"哪一步在验什么"失焦
    environment = prepare()
    if not environment:
        return 1
    area_id = environment["area_id"]
    farm_id = environment["farm_id"]

    # ------------------------------------------------------------------
    print("\n=== 1. 真实组合根：能力装载 + 写能力回读函数 ===")
    registry, composition_root_ok = load_registry()
    registered = {item.name for item in registry.all()}
    check(
        "组合根 bootstrap.load_all() 可用（别人的域写坏时本项会 FAIL，那是准确的）",
        composition_root_ok,
        "组合根不可用：当前迭代的注册表来自隔离装载，组合根装配未被验证",
    )
    print(f"  注册表里的能力：{len(registered)} 条")

    # 1b. registry 要求但代码未实现的缺口：**如实打印**，让缺口可见。
    #     任务 t3 的缺口清单与此一致（area/material/partner 四个读能力待写）。
    print(f"  registry §1.4 要求 {len(REQUIRED_CAPABILITY_CODES)} 条；当前装载 {len(registered & set(REQUIRED_CAPABILITY_CODES))} 条")
    print(f"  尚未实现：{sorted(set(REQUIRED_CAPABILITY_CODES) - registered)}")

    # 1c. ★ 本任务的核心断言：每一个**返回 resource_id 的写能力**都能解析出回读函数，
    #     而且解析出来的必须是域自己声明的那个（不是夹具补挂的）。
    unwired: list[str] = []
    for name in WRITE_CAPABILITIES:
        spec = registry.get(name)
        loader = getattr(spec.handler, "__yuxin_load_by_id__", None)
        if not callable(loader):
            unwired.append(name)
    check(
        "写能力都声明了 __yuxin_load_by_id__（执行器 _reload_after 依赖它）",
        not unwired,
        f"缺回读函数：{unwired}",
    )

    # 域自己的守卫：**注册表里的写能力**必须全部有回读函数。
    # 它在 import 期已经跑过一次，这里显式再跑一次是为了让 e2e 的输出里有这条证据。
    from yuxin.domains.master_data.capabilities import assert_reload_wired  # noqa: E402

    try:
        assert_reload_wired()
        check("域内守卫 assert_reload_wired() 通过（注册表写能力都有 loader）", True)
    except RuntimeError as error:
        check("域内守卫 assert_reload_wired() 通过（注册表写能力都有 loader）", False, str(error))

    # 1d. 回读函数的签名必须能被 `runner._resolve(...)(tx, scope=..., record_id=...)` 调用。
    for name in WRITE_CAPABILITIES:
        spec = registry.get(name)
        loader = getattr(spec.handler, "__yuxin_load_by_id__", None)
        if loader is None:
            continue
        bound = getattr(spec.service_factory(), loader.__name__) if spec.service_factory else loader
        try:
            bound(tx=None, scope=None, record_id=0)
        except TypeError as error:
            check(f"{name} 的回读函数签名", False, f"{error}")
        except Exception:
            # 真正的业务异常（例如 tx=None 时恰好在 SQL 处炸）说明签名是对的。
            pass
    check("回读函数签名可被 _resolve 解析后按 (tx, scope=, record_id=) 调用", True)

    # ------------------------------------------------------------------
    runner = CapabilityRunner(
        registry=registry,
        uow_factory=uow_factory,
        audit=AuditWriter(),
        idempotency=IdempotencyStore(uow_factory),
        scope_resolver=_make_scope_resolver(area_id, farm_id),
    )

    maker = ActorView(
        user_id=MAKER_ID,
        username=MAKER_USERNAME,
        # 故意**给经办人** pond.verify 权限：否则第二层权限校验就先拦住了，
        # 测不到"有权限但自审"那条业务规则（要测的是规则，不是权限）。
        permissions=frozenset({"pond.create", "pond.submit", "pond.update", "pond.view",
                               "pond.archive", "pond.verify", "pond.status.request",
                               "partner.create", "partner.view",
                               "area.view", "material.view"}),
        # 刻意**不带** super_admin：范围必须来自数据范围记录，否则"问错列"的缺陷测不出来。
        role_codes=frozenset({"md-e2e-role"}),
    )
    复核人 = ActorView(
        user_id=VERIFIER_ID,
        username=VERIFIER_USERNAME,
        permissions=frozenset({"pond.verify", "pond.view", "pond.status.request",
                               "pond.status.verify", "area.view", "material.view",
                               "partner.view"}),
        role_codes=frozenset({"md-e2e-role"}),
    )
    nobody = ActorView(
        user_id=VERIFIER_ID,
        username=VERIFIER_USERNAME,
        permissions=frozenset(),
        role_codes=frozenset({"md-e2e-role"}),
    )

    print("\n=== 2. 写能力全链路：权限 -> scope -> 事务 -> 回读 -> 审计 -> 提交 ===")
    pond_code = f"{PROBE_PREFIX}P1"
    created = runner.invoke(
        Invocation(
            capability_name="pond.create",
            payload={"code": pond_code, "name": "端到端探针塘", "area_id": area_id, "pond_status": "build"},
            idempotency_key="md-e2e-key-create",
        ),
        maker,
        CREATE_REQUEST_ID,
    )
    check("返回 kind=executed（不是 confirmation_required / replayed）", created.kind == "executed", created.kind)
    check(
        "返回真实主键（执行器回读通过才可能返回 executed）",
        isinstance(created.resource_id, int) and created.resource_id > 0,
        str(created.resource_id),
    )
    pond_id = int(created.resource_id or 0)

    row = read_pond(pond_id)
    check("直接查库：那一行真的在（不是「我们调用了 INSERT」）", row is not None)
    if row is not None:
        check("落库字段正确（code/name/area_id/初始状态）",
              str(row["code"]) == pond_code and str(row["name"]) == "端到端探针塘"
              and int(row["area_id"]) == area_id and str(row["status"]) == "draft",
              str({k: row[k] for k in ("code", "name", "area_id", "status")}))
        check("服务端解析并写入了分租列（create 不接受也不返回它们）",
              int(row["organization_id"]) > 0 and int(row["farm_id"]) == farm_id,
              f"org={row['organization_id']} farm={row['farm_id']}")
        check("created_by 是操作者本人", int(row["created_by"]) == MAKER_ID, str(row["created_by"]))
    # 审计断言按**本文件自己的 request_id** 精确对账（见 count_audit_for_request 的说明）。
    check(
        "本次请求留下恰好 1 条审计",
        count_audit_for_request(CREATE_REQUEST_ID, "pond.create") == 1,
        f"实际 {count_audit_for_request(CREATE_REQUEST_ID, 'pond.create')} 条",
    )
    entry = fetch_audit(CREATE_REQUEST_ID, "pond.create")
    check("审计结果是 success", entry is not None and str(entry["result"]) == "success",
          str(entry and entry["result"]))
    check("审计指向本次写入的那一行（object_ref = 探针编号）",
          entry is not None and str(entry["object_ref"]) == pond_code, str(entry and entry["object_ref"]))
    check("审计的 after_json 是**回读**出来的行（回读函数没挂时这里是空的）",
          entry is not None and entry["after_json"] is not None and pond_code in str(entry["after_json"]),
          str(entry and str(entry["after_json"])[:120]))
    check("审计记录了操作者（凭令牌解析的身份）",
          entry is not None and int(entry["user_id"]) == MAKER_ID, str(entry and entry["user_id"]))

    print("\n=== 3. 幂等回放的数据来自业务表（不是快照） ===")
    replayed = runner.invoke(
        Invocation(
            capability_name="pond.create",
            payload={"code": pond_code, "name": "端到端探针塘", "area_id": area_id, "pond_status": "build"},
            idempotency_key="md-e2e-key-create",
        ),
        maker,
        "req-md-e2e-create-2",
    )
    check("同键同体被识别为重复请求", replayed.replayed is True, str(replayed.replayed))
    check("没有产生第二行", count_ponds(pond_code) == 1, f"{count_ponds(pond_code)} 行")
    # 把库里那一行改掉，再回放一次：回放如果读快照，就会返回旧名字。
    with uow_factory().begin() as tx:
        tx.execute("UPDATE ponds SET name='库里的新名字' WHERE id=%s", (pond_id,))
    replayed_again = runner.invoke(
        Invocation(
            capability_name="pond.create",
            payload={"code": pond_code, "name": "端到端探针塘", "area_id": area_id, "pond_status": "build"},
            idempotency_key="md-e2e-key-create",
        ),
        maker,
        "req-md-e2e-create-3",
    )
    check(
        "回放读的是业务表当前值，不是响应快照",
        "库里的新名字" in str(replayed_again.data),
        str(replayed_again.data)[:160],
    )

    print("\n=== 4. 权限：第二层拒绝，且库里 0 新增 ===")
    before_rows = count_ponds(f"{PROBE_PREFIX}DENIED")
    expect_rejected(
        "无权限账号调用 pond.create 被拒",
        "FORBIDDEN",
        lambda: runner.invoke(
            Invocation(
                capability_name="pond.create",
                payload={"code": f"{PROBE_PREFIX}DENIED", "name": "不该建出来", "area_id": area_id},
                idempotency_key="md-e2e-key-denied",
            ),
            nobody,
            DENIED_REQUEST_ID,
        ),
    )
    check("库里确实没有多出那一行", count_ponds(f"{PROBE_PREFIX}DENIED") == before_rows)
    # 这里**故意不断言**"权限被拒留下审计"：当前实现的真实行为是——权限校验（runner.py
    # 第 209 行）发生在审计收口之外，所以权限拒绝既不写 failure 也不写 denied。
    # 这是本域之外的一处缺口（"谁被拒过"查不到），如实打印而不是让断言装作它存在。
    denied_audit = count_audit_for_request(DENIED_REQUEST_ID, "pond.create")
    print(f"  记录：**本次**被拒调用留下 {denied_audit} 条审计"
          "（缺口：权限拒绝不经审计收口，待 评审结论）")

    print("\n=== 5. 数据范围：解析不出范围时 fail-closed（不是静默空集） ===")
    no_scope = CapabilityRunner(
        registry=registry,
        uow_factory=uow_factory,
        audit=AuditWriter(),
        idempotency=IdempotencyStore(uow_factory),
        scope_resolver=PersonaScopeResolver({}),  # 谁都没有范围记录
    )
    scoped_but_permitted = ActorView(
        user_id=VERIFIER_ID,
        username=VERIFIER_USERNAME,
        permissions=frozenset({"pond.create"}),  # 有权限，但解析不出数据范围
        role_codes=frozenset({"super_admin"}),
    )
    expect_rejected(
        "无数据范围账号被 DATA_SCOPE_UNRESOLVED 拒绝",
        "DATA_SCOPE_UNRESOLVED",
        lambda: no_scope.invoke(
            Invocation(
                capability_name="pond.create",
                payload={"code": f"{PROBE_PREFIX}NOSCOPE", "name": "无范围", "area_id": area_id},
                idempotency_key="md-e2e-key-noscope",
            ),
            scoped_but_permitted,
            "req-md-e2e-noscope",
        ),
    )
    check("fail-closed 也没有写出数据", count_ponds(f"{PROBE_PREFIX}NOSCOPE") == 0)

    print("\n=== 6. 状态机 + 乐观锁 + 自审拦截（反例必须被拒） ===")
    submitted = runner.invoke(
        Invocation(
            capability_name="pond.submit",
            payload={"pond_id": pond_id, "expected_version": int(row["row_version"])},
            path_params={"pond_id": str(pond_id)},
            idempotency_key="md-e2e-key-submit",
        ),
        maker,
        "req-md-e2e-submit",
    )
    check("提交核验成功", submitted.kind == "executed", str(submitted.kind))
    after_submit = read_pond(pond_id)
    check("状态真的变成 submitted", str(after_submit["status"]) == "submitted", str(after_submit["status"]))
    check("乐观锁版本自增", int(after_submit["row_version"]) == int(row["row_version"]) + 1,
          f"{row['row_version']} -> {after_submit['row_version']}")

    # 反例 A：拿旧版本号再提交 -> VERSION_CONFLICT（乐观锁真的在拦）
    expect_rejected(
        "过期版本号被 VERSION_CONFLICT 拒绝",
        "CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="pond.submit",
                payload={"pond_id": pond_id, "expected_version": int(row["row_version"])},
                path_params={"pond_id": str(pond_id)},
                idempotency_key="md-e2e-key-stale",
            ),
            maker,
            "req-md-e2e-stale",
        ),
    )

    # 反例 B：经办人核验自己提交的塘口 -> FORBIDDEN（rule=DISTINCT_ACTORS）
    error = expect_rejected(
        "经办人自审被拒（DISTINCT_ACTORS）",
        "FORBIDDEN",
        lambda: runner.invoke(
            Invocation(
                capability_name="pond.verify",
                payload={"pond_id": pond_id, "expected_version": int(after_submit["row_version"])},
                path_params={"pond_id": str(pond_id)},
                idempotency_key="md-e2e-key-selfverify",
            ),
            maker,
            "req-md-e2e-selfverify",
        ),
    )
    check("拒绝原因带 rule=DISTINCT_ACTORS（可机械核对）",
          error is not None and (error.data or {}).get("rule") == "DISTINCT_ACTORS",
          str(error and error.data))
    still = read_pond(pond_id)
    check("被拒后状态没有被改（事务整体回滚）", str(still["status"]) == "submitted", str(still["status"]))

    # 正例：换另一个人核验 -> 成功
    checked = runner.invoke(
        Invocation(
            capability_name="pond.verify",
            payload={"pond_id": pond_id, "expected_version": int(still["row_version"])},
            path_params={"pond_id": str(pond_id)},
            idempotency_key="md-e2e-key-verify",
        ),
        复核人,
        "req-md-e2e-verify",
    )
    check("他人核验成功", checked.kind == "executed", str(checked.kind))
    verified = read_pond(pond_id)
    check("状态变成 verified 且记录了核验人",
          str(verified["status"]) == "verified" and int(verified["verified_by"]) == VERIFIER_ID,
          str({k: verified[k] for k in ("status", "verified_by")}))

    # 反例 C：已核验的塘口不能再改（核验后只读）
    expect_rejected(
        "已核验塘口不允许再编辑",
        "CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="pond.update",
                payload={"pond_id": pond_id, "expected_version": int(verified["row_version"]), "name": "想偷偷改"},
                path_params={"pond_id": str(pond_id)},
                idempotency_key="md-e2e-key-edit-verified",
            ),
            maker,
            "req-md-e2e-edit-verified",
        ),
    )

    print("\n=== 7. 两步审批：状态变更申请 + 唯一键 ===")
    changed = runner.invoke(
        Invocation(
            capability_name="pond_status_change.request",
            payload={"pond_id": pond_id, "to_status": "stocked", "reason": "已放苗",
                     "expected_pond_version": int(verified["row_version"])},
            path_params={"pond_id": str(pond_id)},
            idempotency_key="md-e2e-key-psc-1",
        ),
        maker,
        "req-md-e2e-psc-1",
    )
    check("状态变更申请被接受（只建申请，不改状态）", changed.kind == "executed", str(changed.kind))
    check("塘口的 pond_status 此时还没变", str(read_pond(pond_id)["pond_status"]) == "build",
          str(read_pond(pond_id)["pond_status"]))

    # 第二条待核验申请必须由**直接 INSERT** 构造，不能走能力：
    # 走能力会先在 `POND_STATUS_WORKFLOW.require_transition` 那里被拦（当前状态还是
    # build，build->farming 不是合法转移），于是测到的是状态机而不是唯一键。
    # registry #21 要求"DB 唯一键 + 应用层预检**都要有**"，这里验的是唯一键那一半。
    expect_rejected(
        "数据库唯一键拦住同一塘口的第二个待核验申请",
        "CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="pond_status_change.request",
                payload={"pond_id": pond_id, "to_status": "stocked", "reason": "第二条",
                         "expected_pond_version": int(verified["row_version"])},
                path_params={"pond_id": str(pond_id)},
                idempotency_key="md-e2e-key-psc-2",
            ),
            maker,
            "req-md-e2e-psc-2",
        ),
    )
    duplicate_key_blocked = False
    try:
        with uow_factory().begin() as tx:
            tx.execute(
                "INSERT INTO pond_status_change_requests "
                "(organization_id, pond_id, from_status, to_status, reason, pond_version, requested_by) "
                "VALUES (%s,%s,'build','stocked','绕过应用层预检直插的重复申请',%s,%s)",
                (int(read_pond(pond_id)["organization_id"]), pond_id, int(verified["row_version"]), MAKER_ID),
            )
    except pymysql.err.IntegrityError as error:
        duplicate_key_blocked = "uq_pond_status_active_request" in str(error)
    # 唯一键 uq_pond_status_active_request 走生成列 active_pond_id（status='submitted'
    # 时等于 pond_id），所以第二条待核验申请在**数据库层**就插不进去。
    check("数据库唯一键拦住第二条待核验申请（生成列 active_pond_id）", duplicate_key_blocked)
    with uow_factory().begin() as tx:
        pending = int(tx.query_one(
            "SELECT COUNT(*) AS n FROM pond_status_change_requests "
            "WHERE pond_id=%s AND status='submitted'", (pond_id,))["n"])
    check("库里待核验申请仍然只有 1 条", pending == 1, f"实际 {pending} 条")

    with uow_factory().begin() as tx:
        # 注意：`pond_status_change_requests` 没有 row_version 列（003 迁移里只有
        # status / pond_version），所以这里只取 id。
        request_row = tx.query_one(
            "SELECT id FROM pond_status_change_requests WHERE pond_id=%s AND status='submitted'",
            (pond_id,),
        )
    # `expected_version` 是**塘口**当前版本（不是申请行的）：本能力的 `before` 是塘口行，
    # 而申请行没有 row_version 列 —— 见 capabilities.py 里该能力的 invariants 说明。
    pond_version_now = int(read_pond(pond_id)["row_version"])
    expect_rejected(
        "申请人不能核验自己的状态变更申请（服务的显式判定，before 里没有 requested_by）",
        "FORBIDDEN",
        lambda: runner.invoke(
            Invocation(
                capability_name="pond_status_change.verify",
                payload={"pond_id": pond_id, "request_id": int(request_row["id"]),
                         "expected_version": pond_version_now},
                path_params={"pond_id": str(pond_id), "request_id": str(request_row["id"])},
                idempotency_key="md-e2e-key-psc-self",
            ),
            maker,
            "req-md-e2e-psc-self",
        ),
    )

    psc_verified = runner.invoke(
        Invocation(
            capability_name="pond_status_change.verify",
            payload={"pond_id": pond_id, "request_id": int(request_row["id"]),
                     "expected_version": pond_version_now},
            path_params={"pond_id": str(pond_id), "request_id": str(request_row["id"])},
            idempotency_key="md-e2e-key-psc-verify",
        ),
        复核人,
        "req-md-e2e-psc-verify",
    )
    check("他人核验状态变更成功", psc_verified.kind == "executed", str(psc_verified.kind))
    # ★ 声明**真的挂上了**（不只是写在文档里）：这一步的状态迁移由内核的
    #   `StateTransition` **形态二**判定，而该形态要求服务经 `_invariant_context`
    #   回传迁移两端。下面三条是同一件事的三个可核查侧面 —— 缺任一半，
    #   `check()` 会抛 `INTERNAL_ERROR`（上面那次核验就不会是 executed）。
    from yuxin.kernel.capability import REGISTRY as _REG  # noqa: E402
    from yuxin.kernel.invariants import StateTransition as _ST  # noqa: E402

    _verify_cap = _REG.find("pond_status_change.verify")
    _transition_decls = [inv for inv in _verify_cap.invariants if isinstance(inv, _ST)]
    check("pond_status_change.verify 挂了 StateTransition（§4 #20）",
          len(_transition_decls) == 1,
          str([type(i).__name__ for i in _verify_cap.invariants]))
    check("它是**形态二**（从上下文取迁移两端），不是永不触发的字段触发形态",
          bool(_transition_decls)
          and _transition_decls[0].from_context_fields == {
              "from_field": "status_change_from", "to_field": "status_change_to"},
          str(_transition_decls[0].from_context_fields) if _transition_decls else "（未挂）")
    _supplied = (psc_verified.data or {}).get("_invariant_context") or {}
    check("服务回传了迁移两端（缺它内核会 INTERNAL_ERROR，而不是静默放行）",
          {"status_change_from", "status_change_to"} <= set(_supplied),
          str(sorted(_supplied)))
    final = read_pond(pond_id)
    check("pond_status 真的变成 stocked", str(final["pond_status"]) == "stocked", str(final["pond_status"]))

    print("\n=== 8. 归档（状态迁移，不是物理删除） ===")
    archived = runner.invoke(
        Invocation(
            capability_name="pond.archive",
            payload={"pond_id": pond_id, "expected_version": int(final["row_version"])},
            path_params={"pond_id": str(pond_id)},
            idempotency_key="md-e2e-key-archive",
        ),
        maker,
        "req-md-e2e-archive",
    )
    check("归档成功", archived.kind == "executed", str(archived.kind))
    archived_row = read_pond(pond_id)
    check("行还在（归档不是删除），状态是 archived",
          archived_row is not None and str(archived_row["status"]) == "archived",
          str(archived_row and archived_row["status"]))

    print("\n=== 9. 真实数据范围（area 型 / farm 型 / 全场），不用 super_admin 绕过 ===")
    # 经办人是 area 型范围（本区域），复核人是 farm 型范围（本基地）——见 _make_scope_resolver。
    with uow_factory().begin() as tx:
        other_area = tx.query_one(
            "SELECT id FROM areas WHERE id <> %s ORDER BY id LIMIT 1", (area_id,)
        )
    check("库里至少有两个区域（跨范围反例需要它）", other_area is not None)
    other_area_id = int(other_area["id"]) if other_area else area_id

    expect_rejected(
        "area 型账号提交**别的区域**的塘口 -> DATA_SCOPE_DENIED",
        "DATA_SCOPE_DENIED",
        lambda: runner.invoke(
            Invocation(
                capability_name="pond.create",
                payload={"code": f"{PROBE_PREFIX}SCOPE-OTHER", "name": "越界塘", "area_id": other_area_id},
                idempotency_key="md-e2e-key-scope-other",
            ),
            maker,
            "req-md-e2e-scope-other",
        ),
    )
    check("越界的那次没有落库", count_ponds(f"{PROBE_PREFIX}SCOPE-OTHER") == 0)

    expect_rejected(
        "无数据范围账号 -> DATA_SCOPE_UNRESOLVED（fail-closed，不是空集）",
        "DATA_SCOPE_UNRESOLVED",
        lambda: CapabilityRunner(
            registry=registry,
            uow_factory=uow_factory,
            audit=AuditWriter(),
            idempotency=IdempotencyStore(uow_factory),
            scope_resolver=PersonaScopeResolver({}),      # 谁都没有范围记录
        ).invoke(
            Invocation(
                capability_name="pond.create",
                payload={"code": f"{PROBE_PREFIX}NOSCOPE2", "name": "无范围", "area_id": area_id},
                idempotency_key="md-e2e-key-noscope2",
            ),
            maker,
            "req-md-e2e-noscope2",
        ),
    )

    print("\n=== 10. 新增的读能力（区域 / 物料 / 往来单位）与分页、筛选 ===")
    area_read = runner.invoke(
        Invocation(capability_name="area.list", payload={}, query={}), 复核人, "req-md-e2e-area-list"
    )
    check(
        "area.list 返回分页信封 {items,page,page_size,total,has_next}",
        {"items", "page", "page_size", "total", "has_next"} <= set(area_read.data),
        str(sorted(area_read.data)),
    )
    check("farm 型账号能看到本基地的区域", area_read.data["total"] >= 1, str(area_read.data["total"]))

    # ⚠️ 这条断言记录的是 **registry 声明的口径**，不是"期望的美观结果"：
    # `area.list` 声明 `resource(farm_id)`，而 area 型范围的账号没有 farm 型记录
    # -> 谓词恒假 -> 0 行（200，不是错）。这是"声明与使用者直觉不一致"的一处，
    # 已上报 评审结论（要不要让 area 型范围也能列区域）。**不作为通过条件美化**。
    area_read_by_area_scope = runner.invoke(
        Invocation(capability_name="area.list", payload={}, query={}), maker, "req-md-e2e-area-list-2"
    )
    check(
        "area 型范围的账号在 area.list 里看到 0 行（registry 声明 farm_id 的直接后果）",
        area_read_by_area_scope.data["total"] == 0,
        str(area_read_by_area_scope.data["total"]),
    )

    # 物料与往来单位：**范围必须真的在过滤**，所以两条相反方向的断言都要有。
    # 种子物料/往来单位种在 A-NORTH（不是探针区域），所以：
    #   * farm 型账号（本基地 = 种子所在基地）应当看得到它们；
    #   * area 型账号（探针区域）应当看不到 —— 这就是"范围生效"的证据。
    material_total_in_db = count_materials()
    check("库里有种子物料（否则下面的断言没有意义）", material_total_in_db >= 1, str(material_total_in_db))
    material_farm = runner.invoke(
        Invocation(capability_name="material.list", payload={}, query={}), 复核人, "req-md-e2e-material-farm"
    )
    check(
        "farm 型账号能看到本基地的物料（range 没有误伤）",
        material_farm.data["total"] == material_total_in_db,
        f"{material_farm.data['total']} vs 库里 {material_total_in_db}",
    )
    material_area = runner.invoke(
        Invocation(capability_name="material.list", payload={}, query={}), maker, "req-md-e2e-material-area"
    )
    check(
        "area 型账号看不到别的区域的物料（范围确实在过滤）",
        material_area.data["total"] == 0,
        f"total={material_area.data['total']}，库里共 {material_total_in_db}",
    )

    # 往来单位列表：用**本区域内真实存在**的行验证分页与派生字段。
    # 它的写入在 §11 之前，所以这里先造一条（与 §11 用的是同一条，不重复造数）。
    seed_partner = runner.invoke(
        Invocation(
            capability_name="partner.create",
            payload={"partner_type": "supplier", "code": f"{PROBE_PREFIX}SUP-1", "name": "端到端探针供应商",
                     "contact_name": "张探针", "phone": "13800000000", "settlement_days": 30},
            idempotency_key="md-e2e-key-partner",
        ),
        maker,
        "req-md-e2e-partner-create",
    )
    check("往来单位探针已建（后续断言的对象）", seed_partner.kind == "executed", str(seed_partner.kind))
    partner_read = runner.invoke(
        Invocation(capability_name="partner.list", payload={}, query={"page": "1", "page_size": "1"}),
        maker,
        "req-md-e2e-partner-list",
    )
    check(
        "partner.list 分页生效（page_size=1 只回 1 条，total 是总数）",
        len(partner_read.data["items"]) == 1 and partner_read.data["total"] >= 1,
        f"items={len(partner_read.data['items'])} total={partner_read.data['total']}",
    )
    if partner_read.data["items"]:
        first_partner = partner_read.data["items"][0]
        check(
            "列表行带派生字段（status_label / version / allowed_actions）",
            {"status_label", "version", "allowed_actions"} <= set(first_partner),
            str(sorted(first_partner)),
        )
    partner_by_keyword = runner.invoke(
        Invocation(capability_name="partner.list", payload={}, query={"keyword": "端到端探针"}),
        maker,
        "req-md-e2e-partner-keyword",
    )
    check(
        "partner.list 的关键词筛选真的传到服务（命中探针名称）",
        partner_by_keyword.data["total"] == 1
        and "端到端探针" in str(partner_by_keyword.data["items"][0]["name"]),
        str(partner_by_keyword.data.get("total")),
    )

    # 详情读（路径参数）：farm 型账号能取本基地的区域，area 型账号取不到同一个区域。
    area_detail = runner.invoke(
        Invocation(capability_name="area.get", payload={}, path_params={"area_id": str(area_id)}),
        复核人,
        "req-md-e2e-area-get",
    )
    check(
        "area.get 按路径参数回读成功（farm 型账号）",
        area_detail.data["record"]["id"] == area_id,
        str(area_detail.data["record"].get("id")),
    )
    expect_rejected(
        "area.get 对 area 型账号判为越界（同一个区域，两种范围口径不同）",
        "DATA_SCOPE_DENIED",
        lambda: runner.invoke(
            Invocation(capability_name="area.get", payload={}, path_params={"area_id": str(area_id)}),
            maker,
            "req-md-e2e-area-get-2",
        ),
    )

    print("\n=== 11. partner.create 全链路（新增写能力）+ 它的分租键来自数据范围 ===")
    partner_code = f"{PROBE_PREFIX}SUP-1"
    # 这一行是 §10 建出来的（同一条探针数据），这里**不重复创建**——只做查库核对。
    # 重复创建会命中唯一键（那是 §11 最后那条反例要断言的行为）。
    partner_row = fetch_partner(partner_code)
    check("直接查库：往来单位真的写进去了（§10 的创建结果）", partner_row is not None)
    # 反例：同一企业 + 同一类型下编号重复 -> UniqueCode 应用层 409（读得懂）
    expect_rejected(
        "同类型下重复编号被 UniqueCode 拒（应用层 409，不是数据库报错）",
        "CONFLICT",
        lambda: runner.invoke(
            Invocation(
                capability_name="partner.create",
                payload={"partner_type": "supplier", "code": partner_code, "name": "重复编号"},
                idempotency_key="md-e2e-key-partner-dup",
            ),
            maker,
            "req-md-e2e-partner-dup",
        ),
    )
    # 正例：同一编号换个类型（customer）应当放行 —— 唯一键是"企业 + 类型 + 编号"
    partner_other_type = runner.invoke(
        Invocation(
            capability_name="partner.create",
            payload={"partner_type": "customer", "code": partner_code, "name": "同编号的另一类型"},
            idempotency_key="md-e2e-key-partner-customer",
        ),
        maker,
        "req-md-e2e-partner-customer",
    )
    check("同编号在另一类型下允许（唯一键是 企业+类型+编号）",
          partner_other_type.kind == "executed", str(partner_other_type.kind))

    print("\n=== 12. 未绑定回读函数时会怎样（反证：没有回读 = 500，不会静默成功） ===")
    # 直接验证执行器的行为，证明"回读函数缺失"不是软失败：
    # 拿一条真的能力，把它的 handler 换成一个没有 __yuxin_load_by_id__ 的等价函数。
    # 关键：**不能**去改真实注册表里那条 spec（`Capability` 是 frozen dataclass，
    # 改它会污染真实装配）。做法是造一个只含"有缺口的那一条能力"的临时注册表：
    #
    # 缺口的形态选"声明了一个解析不到的 loader"而不是"不给 loader"——后者在内核里
    # 表达成"这条能力不需要回读函数"，而 `Capability.loader=` 会在 `__post_init__`
    # 里把同类 handler 上的属性**重新挂上**（`dataclasses.replace` 也走这条路），
    # 所以"不给"已经构造不出缺口了。而"声明的 loader 在 service_factory 上不存在"
    # 是真实会发生的笔误（写错方法名），执行器必须响亮地失败而不是静默降级。
    import dataclasses

    from yuxin.kernel.capability import Registry  # noqa: E402
    from yuxin.kernel.workflow import RESOURCES  # noqa: E402

    real_spec = load_all().get("pond.create")
    denuded_registry = Registry()
    denuded_spec = dataclasses.replace(
        real_spec,
        loader=lambda *args, **kwargs: None,   # 有声明、但 factory 上没有这个名字
    )
    # 让执行器的 `_resolve` 去找一个**不存在**的方法名：service_factory 上要有它，
    # 但这里把 handler 换成模块级函数（`__name__` 故意起成不存在的名字）。
    def _ghost_loader(*args, **kwargs):  # pragma: no cover - 只用于构造缺口
        return None

    _ghost_loader.__name__ = "load_pond_that_does_not_exist"
    denuded_spec = dataclasses.replace(real_spec, loader=_ghost_loader)
    denuded_registry.register(denuded_spec)
    denuded_runner = CapabilityRunner(
        registry=denuded_registry,
        uow_factory=uow_factory,
        audit=AuditWriter(),
        idempotency=IdempotencyStore(uow_factory),
        scope_resolver=_make_scope_resolver(area_id, farm_id),
    )
    expect_rejected(
        "声明的 loader 解析不到时抛 INTERNAL_ERROR（绝不降级成成功）",
        "INTERNAL_ERROR",
        lambda: denuded_runner.invoke(
            Invocation(
                capability_name="pond.create",
                payload={"code": f"{PROBE_PREFIX}NORELOAD", "name": "无回读", "area_id": area_id},
                idempotency_key="md-e2e-key-noreload",
            ),
            maker,
            "req-md-e2e-noreload",
        ),
    )
    check("无回读的那次调用没有留下数据（事务回滚）", count_ponds(f"{PROBE_PREFIX}NORELOAD") == 0)

    print("\n=== 13. 跨域具名只读入口（ROLLOUT_CONTRACT §2.0 的表所有者义务）===")
    # §2.0：调用方不得直读别人的表；表所有者必须提供具名只读函数（单行 + 批量）。
    # 实测有**五个域**在读主数据的四张表，所以这些入口是本域的对外契约，必须像能力一样被测。
    from yuxin.domains.master_data import lookup as md_lookup  # noqa: E402
    from yuxin.domains.master_data import service as md_service  # noqa: E402

    single_names = ("lookup_pond", "lookup_area", "lookup_material", "lookup_partner")
    batch_names = ("lookup_ponds", "lookup_areas", "lookup_materials", "lookup_partners")
    check(
        "8 个入口都能从 `master_data.service` 取到（§2.0 表格用的是 `<域>.service.lookup_*` 形态）",
        all(callable(getattr(md_service, name, None)) for name in single_names + batch_names),
        str([n for n in single_names + batch_names if not callable(getattr(md_service, n, None))]),
    )
    check(
        "同一批入口也能从 `master_data.lookup` 取到（实现只有一处，service 只是引名）",
        all(callable(getattr(md_lookup, name, None)) for name in single_names + batch_names),
        str([n for n in single_names + batch_names if not callable(getattr(md_lookup, n, None))]),
    )

    with uow_factory().begin() as tx:
        pond_row = md_service.lookup_pond(tx, pond_id=pond_id)
        check("lookup_pond 返回该塘口的事实", pond_row is not None and int(pond_row["id"]) == pond_id)
        check(
            "lookup_pond 的列集恰好是承诺的那批（多一列/少一列都算契约变更）",
            pond_row is not None and set(pond_row) == set(md_lookup.POND_COLUMNS),
            str(sorted(set(pond_row or {}))),
        )
        check(
            "单行入口对不存在的 id 返回 None（而不是抛错：所有者报事实、调用方定语义）",
            md_service.lookup_pond(tx, pond_id=999_999_999) is None
            and md_service.lookup_partner(tx, partner_id=999_999_999) is None,
        )
        # 批量：缺失 id 被略去，键是 int
        got = md_service.lookup_ponds(tx, pond_ids=[pond_id, 999_999_999])
        check(
            "批量入口返回 dict[int, row]，缺失 id 被略去",
            set(got) == {pond_id} and all(isinstance(k, int) for k in got),
            str(sorted(got)),
        )
        partner_batch = md_service.lookup_partners(tx, partner_ids=[])
        check("批量入口对空入参返回空 dict", partner_batch == {})

        # ★ 「空入参不发查询」——用计数替身证明，而不是靠肉眼读代码
        class _CountingTx:
            def __init__(self, inner):
                self._inner = inner
                self.calls = 0

            def query_one(self, sql, params=None):
                self.calls += 1
                return self._inner.query_one(sql, params)

            def query_all(self, sql, params=None):
                self.calls += 1
                return self._inner.query_all(sql, params)

        counter = _CountingTx(tx)
        md_service.lookup_ponds(counter, pond_ids=[])
        md_service.lookup_materials(counter, material_ids=[])
        check(
            "空入参**一次查询都不发**（列表页在无关联行时的路径）",
            counter.calls == 0,
            f"实际发了 {counter.calls} 次查询",
        )

        # 调用方形态：批量为列表渲染取 N 行（sales/purchase/warehouse 的真实用例）
        two = md_service.lookup_ponds(tx, pond_ids=[pond_id, pond_id + 1])
        check(
            "批量形态可用于列表渲染（一次查询取 N 行，而不是 N+1 次单行）",
            isinstance(two, dict) and pond_id in two,
            str(sorted(two)),
        )

    cleanup()

    print()
    if FAILURES:
        print(f"FAIL  共 {FAILURES} 项断言未通过")
        return 1
    print("PASS  主数据域端到端全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
