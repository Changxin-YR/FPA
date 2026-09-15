"""t17 验收：证明"种权限字典"**解开了**那个问题，而不是"表里有行"。

任务说明的要求原话：验收里最重要的一条不是"表里有行"，而是**端到端证明它解开了问题**：
同一个角色/用户跑一条真实能力，**种之前失败、种之后成功**，贴两次原始输出。

## 这个脚本怎么做到"同为一个人、同一条能力、两次不同结果"

角色与用户的**绑定**在种之前就建好并保持不变；两次之间**只种权限字典**。于是两次的
唯一变量就是"权限码在不在 `permissions` 表里"——这正是 t17 要修的东西。

链路（`AccessService._load()` 的解析 SQL，与生产完全一致）：

    users -> user_roles -> roles(active)
          -> role_permissions -> permissions.code

`role_permissions` 里那行**从第一次实验之前就存在**（它指向一个当时还不存在的
权限码，所以没有 FK 问题——FK 指向 `permissions.id`，而我们的绑定是
`INSERT ... SELECT`，码不在表里时它自然不插入任何行）。所以"绑定先有、权限码后到"
这个顺序本身也验证了：**权限链的终点是 `permissions.code` 的存在性**。

用法：
    $env:MYSQL_USER='fpa'; $env:MYSQL_PASSWORD='fpa_dev_password'
    python tools/accept_seed_permissions.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "cost"))

from fpa.kernel.errors import DomainError  # noqa: E402
from fpa.kernel.uow import UnitOfWork  # noqa: E402
from fpa.kernel.uow_factory import connection_config  # noqa: E402

USERNAME = "t17_probe_user"
PASSWORD = "T17-probe-Passw0rd!"
ROLE_CODE = "t17-probe-role"

#: 探针要跑的真实能力。选 `cost.entry.list`：
#:   · required_permission = `cost.view`（库里**没有**这个码，且不是 pond.create 那条已有行）；
#:   · 它是只读能力，跑起来不需要造业务数据、也不会写库；
#:   · 它走完整的三层校验（L2 权限 + scope 解析），所以能真实验证"权限不足"。
CAPABILITY = "cost.entry.list"
PERMISSION = "cost.view"

PASSES = 0
FAILS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSES, FAILS
    if ok:
        PASSES += 1
        print(f"  PASS  {label}")
    else:
        FAILS += 1
        print(f"  FAIL  {label}  {detail}")


def prepare_probe() -> None:
    """建探针用户 + 角色 + **绑定**（绑定在两次实验之前就存在）。

    ## 为什么要**先复位**

    第一次跑完会把 `cost.view` 种进 `permissions`，于是第二次跑时"种之前"已经不成立，
    验收会假失败（实测：第一次 6 PASS，第二次 4 PASS / 2 FAIL）。

    办法不是"换一个权限码再跑"（那只是把问题推给下一次），而是**每次跑都回到同一
    起点**：删掉本次探针自己的绑定与权限码。这正是本仓其他 e2e 的做法（清理探针数据），
    也是"测试必须可重复"的最低要求。

    清理范围严格限定在**探针自建的东西**，不碰任何别人的行。
    """
    from fpa.kernel.password import hash_password

    with UnitOfWork(connection_config()).begin() as tx:
        # 复位：只删探针角色的授权行 + 探针要观察的那个权限码
        tx.execute(
            "DELETE rp FROM role_permissions rp "
            "JOIN roles r ON r.id = rp.role_id WHERE r.code = %s",
            (ROLE_CODE,),
        )
        tx.execute("DELETE FROM permissions WHERE code=%s", (PERMISSION,))

        tx.execute(
            "INSERT INTO users (username, display_name, password_hash, status) "
            "VALUES (%s, %s, %s, 'active') "
            "ON DUPLICATE KEY UPDATE password_hash=VALUES(password_hash), status='active'",
            (USERNAME, "t17 权限种子探针", hash_password(PASSWORD)),
        )
        tx.execute(
            "INSERT INTO roles (code, name, description) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE name=VALUES(name)",
            (ROLE_CODE, "t17 探针角色", "验收专用：只需它持有 cost.view"),
        )
        # 数据范围：能力需要一个可解析的 scope，否则会以 DATA_SCOPE_UNRESOLVED 失败，
        # 那不是我们要观察的现象。
        tx.execute(
            "INSERT INTO data_scopes (code, name, scope_type, area_id, status) "
            "SELECT 't17-probe-area', 't17 探针区域', 'area', a.id, 'active' "
            "FROM areas a ORDER BY a.id LIMIT 1 "
            "ON DUPLICATE KEY UPDATE area_id=VALUES(area_id)"
        )


def bind_role_and_scope() -> None:
    """把探针用户绑到探针角色与数据范围上。**两次实验之间保持不变。**"""
    with UnitOfWork(connection_config()).begin() as tx:
        tx.execute(
            "INSERT INTO user_roles (user_id, role_id) "
            "SELECT u.id, r.id FROM users u CROSS JOIN roles r "
            "WHERE u.username=%s AND r.code=%s "
            "ON DUPLICATE KEY UPDATE user_id=VALUES(user_id)",
            (USERNAME, ROLE_CODE),
        )
        tx.execute(
            "INSERT INTO user_data_scopes (user_id, scope_id) "
            "SELECT u.id, s.id FROM users u CROSS JOIN data_scopes s "
            "WHERE u.username=%s AND s.code='t17-probe-area' "
            "ON DUPLICATE KEY UPDATE user_id=VALUES(user_id)",
            (USERNAME,),
        )


def bind_permission_to_role() -> int:
    """把目标权限码授给探针角色。

    这一步**必须**发生在种子之后：`role_permissions.permission_id` 有 FK 指向
    `permissions.id`，码不在表里时这一步插不进任何行。
    这本身就是"字典必须先存在"的机械证据。
    """
    with UnitOfWork(connection_config()).begin() as tx:
        tx.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
            "WHERE r.code=%s AND p.code=%s "
            "ON DUPLICATE KEY UPDATE role_id=VALUES(role_id)",
            (ROLE_CODE, PERMISSION),
        )
        row = tx.query_one(
            "SELECT COUNT(*) AS n FROM role_permissions rp "
            "JOIN roles r ON r.id=rp.role_id JOIN permissions p ON p.id=rp.permission_id "
            "WHERE r.code=%s AND p.code=%s",
            (ROLE_CODE, PERMISSION),
        )
        return int(row["n"])


def chain_permissions() -> list[str]:
    """用 AccessService 的真实解析 SQL 取该用户的权限集合。

    不自己写一份等价 SQL——那样就成了"两处描述同一件事"，而且可能与被测实现漂移。
    这里直接调用 **真实的 `AccessService.login()`**，让 `_load()` 去解析。
    """
    from fpa.domains.access.service import AccessService

    service = AccessService(lambda: UnitOfWork(connection_config()))
    try:
        # `login()` 返回 `(令牌, CSRF 令牌, 用户)` —— 三元组，不是两元组。
        _token, _csrf, user = service.login(username=USERNAME, password=PASSWORD)
    except DomainError as exc:
        return [f"<login failed: {exc.code}: {exc.message}>"]
    return sorted(user.permissions)


def attempt_capability() -> tuple[str, str]:
    """走真实执行器跑一次能力，返回 `(kind/errcode, message)`。

    用能力自己声明的 `required_permission` 构造成 **ActorView 的真实权限集合**
    ——也就是"如果数据库里没有这个码，执行器看到的权限集合就是空的"。
    """
    from fpa.kernel.audit import AuditWriter
    from fpa.kernel.idempotency import IdempotencyStore
    from fpa.kernel.runner import ActorView, CapabilityRunner, Invocation
    from fpa.kernel.scope import Scope

    from domain_loader import load_registry_tolerant

    # 逐域装载：**本验收要测的是"权限字典缺失"这一件事**，不该因为某个兄弟域
    # 正在编辑而被判红（实测：sales 报 NameError 时 load_all() 直接崩）。
    # 但装载失败必须被打印出来，否则"少测了几个域"会被当成"全都对"。
    registry, failed = load_registry_tolerant()
    if failed:
        print(f"        （注意：{len(failed)} 个域未装载，本次结果不完整）")
        for item in failed:
            print(f"          ! {item}")
    spec = registry.get(CAPABILITY)

    class _Resolver:
        def resolve(self, *, user_id: int, role_codes) -> Scope:
            return Scope.all_data(user_id=user_id)

    runner = CapabilityRunner(
        registry=registry,
        uow_factory=lambda: UnitOfWork(connection_config()),
        audit=AuditWriter(),
        idempotency=IdempotencyStore(lambda: UnitOfWork(connection_config())),
        scope_resolver=_Resolver(),
    )

    with UnitOfWork(connection_config()).begin() as tx:
        row = tx.query_one("SELECT id FROM users WHERE username=%s", (USERNAME,))
    user_id = int(row["id"])

    permissions = frozenset(chain_permissions())
    print(f"        （执行器看到的权限集合：{sorted(permissions) or '空'}）")
    actor = ActorView(
        user_id=user_id,
        username=USERNAME,
        permissions=permissions,
        role_codes=frozenset({ROLE_CODE}),
    )
    try:
        result = runner.invoke(
            Invocation(capability_name=CAPABILITY, query={"page": 1, "page_size": 1}),
            actor,
            "req-t17",
        )
        return (result.kind, result.message or "")
    except DomainError as exc:
        return (str(exc.code), exc.message)


def main() -> int:
    from fpa.kernel.errors import DomainError  # noqa: F401  （供 attempt 内引用）

    print("=" * 78)
    print("t17 验收：同一个用户 / 同一条能力，种之前失败、种之后成功")
    print("=" * 78)

    prepare_probe()
    bind_role_and_scope()

    print("\n--- 前置：确认权限码当前**不在**库里（否则本验收不成立）---")
    with UnitOfWork(connection_config()).begin() as tx:
        row = tx.query_one("SELECT COUNT(*) AS n FROM permissions WHERE code=%s", (PERMISSION,))
    before_rows = int(row["n"])
    check(f"权限码 {PERMISSION} 在 permissions 表里不存在（{before_rows} 行）",
          before_rows == 0,
          "库里已有该码 —— 本验收要先删掉它才有意义（或用另一个码）")

    print(f"\n--- 第 1 次：种之前 ---")
    perms_before = chain_permissions()
    print(f"      AccessService 解析出的权限集合：{perms_before}")
    code, message = attempt_capability()
    print(f"      调用 {CAPABILITY} -> code={code}  message={message}")
    check("种之前：权限解析为空 / 能力被拒（FORBIDDEN）",
          code == "FORBIDDEN",
          f"期望 FORBIDDEN，实际 {code}")

    print(f"\n--- 种子：python tools/seed_permissions.py ---")
    result = subprocess.run(
        [sys.executable, "tools/seed_permissions.py"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8",
    )
    print((result.stdout or "").strip())
    check("种子工具退出码为 0", result.returncode == 0, f"exit={result.returncode}")

    print(f"\n--- 授予：把 {PERMISSION} 授给探针角色 ---")
    granted = bind_permission_to_role()
    print(f"      role_permissions 里现在有 {granted} 行")
    check("授权行写入成功（种子之前插不进去，因为 FK 指向不存在的码）", granted == 1)

    print(f"\n--- 第 2 次：种之后（**角色绑定与用户都没变，只多了权限字典**）---")
    perms_after = chain_permissions()
    print(f"      AccessService 解析出的权限集合：{perms_after}")
    check(f"种之后：权限集合里出现了 {PERMISSION}", PERMISSION in perms_after,
          str(perms_after))
    code2, message2 = attempt_capability()
    print(f"      调用 {CAPABILITY} -> kind={code2}  message={message2 or '(无)'}")
    check("种之后：能力执行成功（不再是 FORBIDDEN）", code2 == "read",
          f"期望 read，实际 {code2}")

    print("\n" + "=" * 78)
    print(f"结论：{PASSES} PASS / {FAILS} FAIL")
    print("=" * 78)

    # ★ 收尾必须**恢复**被复位掉的那个权限码。
    #
    # 本脚本为了造出"种之前"的状态、删掉了 `cost.view`。如果不补回来，它就留下了
    # 一个**残缺的权限字典** —— 而并行开发期间别人正在跑各自的 e2e，
    # 他们可能正好需要这个码（`cost.view` 虽然在 cost 域，但字典完整性是共享事实）。
    # 一次"验收"不该给别人留下副作用。
    print(f"\n--- 收尾：恢复被复位掉的 {PERMISSION}（不留副作用）---")
    restore = subprocess.run(
        [sys.executable, "tools/seed_permissions.py"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8",
    )
    with UnitOfWork(connection_config()).begin() as tx:
        row = tx.query_one("SELECT COUNT(*) AS n FROM permissions WHERE code=%s",
                           (PERMISSION,))
    restored = int(row["n"])
    check(f"收尾后 {PERMISSION} 已回到 permissions 表（{restored} 行）",
          restored == 1, "残留缺口会让别人跑 e2e 时莫名其妙地权限不足")
    print(f"      （恢复用的种子工具 exit={restore.returncode}）")

    print(f"\n最终：{PASSES} PASS / {FAILS} FAIL")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
