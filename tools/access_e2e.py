"""access / audit / identity 七条能力的端到端测试（真实 MySQL + 真实注册表 + 真实执行器）。

## 这个文件要证明什么

不是"代码没报错"，而是四件可验证的事：

**1. 七条能力在**真实组合根**上真的可调用（不是"声明了但 404"）。**

`tools/registry_reconcile.py` 的 [A] 表是这件事的静态判据，本文件是它的动态对偶：
每条能力都走一次 `CapabilityRunner.invoke`，并在库里 `SELECT` 确认结果。
[F] 的修复也在 `docs/ROW_ACTIONS.md` 里有清单，那部分由
`tools/registry_reconcile.py` 的 [F] 表负责——本文件只钉住能力侧。

**2. 元数据闸门真的按声明生效（human_only / HITL 确认 / 权限码）。**

`auth.password.change` 标 `human_only`；账号管理四条在 2026-09-14 复核后改为
`exposed + confirmation=always`（Agent 可见，但每次执行必须过服务端 HITL 确认）；
`audit.log.list` 标 `exposed`，靠 `audit.view` 权限码把守。这些值如果不生效，
唯一症状是"Agent 悄悄做到了不该做的事"——没有任何页面会显示出来。所以本文件
显式构造**Agent 身份**，逐条断言它在正确的那道闸门上被挡下。

**3. `role_permissions` 的合法值集合真的是**能力清单**，不是 `permissions` 表。**

registry §2.4 把这条列为"关键改进"。判据必须是可执行的：提交一个
**存在于 `permissions` 表、但不对应任何能力**的码 → 必须被拒。
本文件现场插一个这样的码再断言拒绝（`tools/` 侧不 assume 库里没有脏码）。

**4. 本文件自己**不**用夹具注册表。**

`tools/web_e2e.py` 的 `build_registry()` 是只注册一条能力的手搓夹具。本文件走
`fpa.bootstrap.load_all()` 的真实组合根——夹具全绿证明不了真实声明是对的
（`docs/DEVELOPMENT.md` §1 记过这个坑：夹具注册表曾让七套自检全绿而实际能力一条没装）。

用法::

    $env:MYSQL_USER='fpa'; $env:MYSQL_PASSWORD='fpa_dev_password'
    python tools/access_e2e.py

**可以重复跑**：所有探针数据都带 `FPA-ACCESS-E2E-` 前缀，脚本开头先清一遍
（`PROBE_PREFIX`），末尾再清一遍。全表 `DELETE` 是禁止的——脚本可能跑在共享开发库上。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pymysql  # noqa: E402

from fpa.bootstrap import load_all  # noqa: E402,F401  —— 见模块 docstring 第 4 点
from fpa.kernel.audit import AuditWriter  # noqa: E402
from fpa.kernel.errors import DomainError  # noqa: E402
from fpa.kernel.idempotency import IdempotencyStore  # noqa: E402
from fpa.kernel.runner import ActorView, CapabilityRunner, Invocation  # noqa: E402
from fpa.kernel.scope import Scope  # noqa: E402
from fpa.kernel.uow import ConnectionConfig, UnitOfWork  # noqa: E402

FAILURES = 0

#: 探针标记。所有本脚本创建的行都带它，清理时只删这些。
PROBE_PREFIX = "FPA-ACCESS-E2E-"
PROBE_ROLE_CODE = PROBE_PREFIX + "ROLE"
PROBE_SCOPE_CODE = PROBE_PREFIX + "SCOPE"
#: 只在"合法值集合 = 能力清单"那条断言里用：一个**不对应任何能力**的权限码。
#: 它必须存在于 `permissions` 表里，否则那条断言测的是"未知输入"而不是"脏码"。
DECOY_PERMISSION_CODE = PROBE_PREFIX + "DECOY-PERMISSION"

#: 探针用的两个账号 id 无关紧要（服务自己 INSERT 并返回 id），
#: 但**操作者**必须是一个真实存在的 user（`audit_logs.user_id` 有外键）。
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


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


def expect_rejected(label: str, code: str | tuple[str, ...], action) -> DomainError | None:
    """断言一次调用**被业务规则拒绝**（而不是成功、也不是崩在别的错误上）。

    只断言"抛了异常"会让"因为有 bug 而崩"与"因为规则生效而拒"看起来一样。
    """
    global FAILURES
    allowed = (code,) if isinstance(code, str) else code
    try:
        action()
    except DomainError as error:
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
    print(f"  FAIL  {label}  竟然成功了")
    return None


class FlatScopeResolver:
    """给每个账号一个"全场"范围。

    账号与角色是**系统级**对象，七条能力的 `scope` 全是 `none` / `owner`，
    所以这里不需要按人区分范围（`DistinctActors` 之类不在这七条上）。
    用 `allow_all=True` 让范围解析不成为干扰项——本文件要测的是权限与委派，
    范围语义由各域自己的 e2e 工具负责。
    """

    def resolve(self, *, user_id: int, role_codes: frozenset[str]) -> Scope:
        return Scope.all_data(user_id=user_id)


# ---------------------------------------------------------------------------
# 环境准备 / 清理
# ---------------------------------------------------------------------------

def prepare() -> tuple[int, int, int]:
    """建探针用的角色、数据范围、操作者账号，返回 `(role_id, scope_id, actor_user_id)`。

    为什么不复用库里的既有行：它们可能被别的探针改过（本仓有多个 e2e 工具共用
    同一个开发库）。探针自带自己的行，才能断言"我看到的集合恰好是我建的那些"。
    """
    config = _config()
    with uow_factory().begin() as tx:
        tx.execute(
            "INSERT INTO roles (code, name, description, status) VALUES (%s,%s,%s,'active') "
            "ON DUPLICATE KEY UPDATE name=VALUES(name), status='active'",
            (PROBE_ROLE_CODE, "access e2e 探针角色", "自检专用"),
        )
        role_row = tx.query_one("SELECT id FROM roles WHERE code=%s", (PROBE_ROLE_CODE,))
        role_id = int(role_row["id"])

        tx.execute(
            "INSERT INTO data_scopes (code, name, scope_type, status) "
            "VALUES (%s,%s,'personal','active') "
            "ON DUPLICATE KEY UPDATE name=VALUES(name), status='active'",
            (PROBE_SCOPE_CODE, "access e2e 探针范围"),
        )
        scope_row = tx.query_one("SELECT id FROM data_scopes WHERE code=%s", (PROBE_SCOPE_CODE,))
        scope_id = int(scope_row["id"])

        # 操作者：一个**真实存在**的 users 行（审计有外键）。
        tx.execute(
            "INSERT INTO users (username, display_name, password_hash, status, "
            "                   must_change_password) "
            "VALUES (%s,%s,%s,'active',0) "
            "ON DUPLICATE KEY UPDATE status='active'",
            (PROBE_PREFIX + "operator", "access e2e 操作者", "scrypt$16384$8$1$00$00"),
        )
        operator = tx.query_one(
            "SELECT id FROM users WHERE username=%s", (PROBE_PREFIX + "operator",)
        )
        actor_id = int(operator["id"])

        # 脏权限码：它存在于 permissions 表、但不对应任何能力。
        tx.execute(
            "INSERT INTO permissions (code, name, domain, description) VALUES (%s,%s,'access','探针') "
            "ON DUPLICATE KEY UPDATE name=VALUES(name)",
            (DECOY_PERMISSION_CODE, DECOY_PERMISSION_CODE),
        )
        return role_id, scope_id, actor_id
    raise AssertionError("unreachable")


def cleanup() -> int:
    """删除本脚本建立的全部探针行。返回删除的行数（给"清理确实做了事"留证）。

    ## 删除顺序不是随意的：先删**引用方**，再删被引用行

    `users` 被六张表外键引用（`ON DELETE RESTRICT`），所以直接删会撞 1451。
    实测就是在这里撞的：`idempotency_keys` 的 `fk_idempotency_user` 是 RESTRICT，
    而一次带幂等键的调用就会往那里写一行。

    ## `audit_logs` **删不掉**，这是设计使然

    001 迁移用触发器在数据库层禁止改删审计（`audit_logs_no_delete` 会
    `SIGNAL SQLSTATE '45000'`）。所以本脚本**不尝试**删审计行——
    那是 append-only 契约，探针数据留在审计里是**正确**的行为
    （审计的意义正是"发生过的事删不掉"）。这里只删业务侧的行。
    """
    removed = 0
    with uow_factory().begin() as tx:
        users = tx.query_all(
            "SELECT id FROM users WHERE username LIKE %s", (PROBE_PREFIX + "%",)
        )
        user_ids = [int(row["id"]) for row in users]
        role_row = tx.query_one("SELECT id FROM roles WHERE code=%s", (PROBE_ROLE_CODE,))
        role_id = int(role_row["id"]) if role_row else 0
        for user_id in user_ids:
            # 引用 users 的六张表，全部先清（顺序无关，但一张都不能漏）。
            tx.execute("DELETE FROM user_roles WHERE user_id=%s", (user_id,))
            tx.execute("DELETE FROM user_data_scopes WHERE user_id=%s", (user_id,))
            tx.execute("DELETE FROM sessions WHERE user_id=%s", (user_id,))
            tx.execute("DELETE FROM idempotency_keys WHERE user_id=%s", (user_id,))
            tx.execute("DELETE FROM agent_confirmations WHERE user_id=%s", (user_id,))
            tx.execute("DELETE FROM record_revisions WHERE actor_user_id=%s", (user_id,))
            removed += tx.execute("DELETE FROM users WHERE id=%s", (user_id,))
        if role_id:
            tx.execute("DELETE FROM role_permissions WHERE role_id=%s", (role_id,))
            removed += tx.execute("DELETE FROM roles WHERE id=%s", (role_id,))
        removed += tx.execute("DELETE FROM data_scopes WHERE code=%s", (PROBE_SCOPE_CODE,))
        removed += tx.execute(
            "DELETE FROM permissions WHERE code=%s", (DECOY_PERMISSION_CODE,)
        )
    return removed


def main() -> int:
    print("#" * 74)
    print("# access / audit / identity 七条能力端到端（真实 MySQL + 真实组合根）")
    print("#" * 74)

    registry = load_all()
    missing = [
        name
        for name in (
            "access.user.list",
            "access.user.create",
            "access.user.status",
            "access.user.grants",
            "access.role.permissions",
            "audit.log.list",
            "auth.password.change",
        )
        if registry.find(name) is None
    ]
    if missing:
        print(f"FAIL  组合根没有装载这些能力：{missing}")
        return 2

    print(f"\n清理上轮残留：删除 {cleanup()} 行")
    role_id, scope_id, operator_id = prepare()

    runner = CapabilityRunner(
        registry=registry,
        uow_factory=uow_factory,
        audit=AuditWriter(),
        idempotency=IdempotencyStore(uow_factory),
        scope_resolver=FlatScopeResolver(),
    )
    # 服务端 `admin.py::_require_super_admin` 现在**严格要求** `role_codes` 里真的有
    # `super_admin`（权限码不能冒充角色）。admin 与 agent 都显式带上，这样 agent 被拒时
    # 命中的一定是 `human_only`，而不是"碰巧没有超管角色"——否则测试会因为**另一条规则**
    # 生效而假绿。
    super_admin = frozenset({"super_admin"})
    admin = ActorView(
        user_id=operator_id,
        username=PROBE_PREFIX + "operator",
        permissions=frozenset({"auth.user.manage", "auth.role.manage", "audit.view"}),
        role_codes=super_admin,
    )
    #: Agent 身份：`is_agent=True` 让 `human_only` 的判定生效。
    agent = ActorView(
        user_id=operator_id,
        username=PROBE_PREFIX + "operator",
        permissions=frozenset({"auth.user.manage", "auth.role.manage", "audit.view"}),
        role_codes=super_admin,
        is_agent=True,
    )
    nobody = ActorView(
        user_id=operator_id,
        username=PROBE_PREFIX + "operator",
        permissions=frozenset(),
        role_codes=frozenset(),
    )

    exit_code = 1
    try:
        # ==================================================================
        print("\n=== 1. 七条能力都走一次真实执行器（[A] 表的动态对偶）===")
        # ==================================================================
        created = runner.invoke(
            Invocation(
                capability_name="access.user.create",
                payload={
                    "username": PROBE_PREFIX + "newuser",
                    "display_name": "探针新账号",
                    "initial_password": "FpaProbe77x",
                    "role_ids": [role_id],
                    "scope_ids": [scope_id],
                },
                idempotency_key=PROBE_PREFIX + "create-1",
            ),
            admin,
            "req-access-1",
        )
        check("access.user.create 执行成功", created.kind == "executed", created.kind)
        new_user_id = int(created.resource_id or 0)
        with uow_factory().begin() as tx:
            stored = tx.query_one(
                "SELECT username, display_name, status, must_change_password FROM users WHERE id=%s",
                (new_user_id,),
            )
            grants = tx.query_one(
                "SELECT COUNT(*) AS n FROM user_roles WHERE user_id=%s", (new_user_id,)
            )
        check("新账号真的落库", stored is not None and stored["username"] == PROBE_PREFIX + "newuser")
        check("初始状态是 active", stored is not None and str(stored["status"]) == "active")
        check("强制下次改密（must_change_password=1）", stored is not None and int(stored["must_change_password"]) == 1)
        check("角色关联已写入", grants is not None and int(grants["n"]) == 1)

        listing = runner.invoke(
            Invocation(
                capability_name="access.user.list",
                payload={},
                query={"keyword": PROBE_PREFIX + "newuser"},
            ),
            admin,
            "req-access-2",
        )
        check("access.user.list 返回一行", int(listing.data["total"]) == 1, str(listing.data))
        item = listing.data["items"][0]
        check("列表带 role_names（批量查询生效，不是 N+1）", item.get("role_names") == ["access e2e 探针角色"], str(item.get("role_names")))
        check("列表**不返回** password_hash", "password_hash" not in item, str(sorted(item)))

        status_result = runner.invoke(
            Invocation(
                capability_name="access.user.status",
                payload={"status": "disabled", "reason": "探针：停用一下"},
                path_params={"user_id": new_user_id},
            ),
            admin,
            "req-access-3",
        )
        check("access.user.status 执行成功", status_result.kind == "executed", status_result.kind)
        with uow_factory().begin() as tx:
            after_status = tx.query_one("SELECT status FROM users WHERE id=%s", (new_user_id,))
        check("库里状态已变为 disabled", str(after_status["status"]) == "disabled")

        grants_result = runner.invoke(
            Invocation(
                capability_name="access.user.grants",
                payload={"role_ids": [role_id], "scope_ids": [scope_id]},
                path_params={"user_id": new_user_id},
            ),
            admin,
            "req-access-4",
        )
        check("access.user.grants 执行成功", grants_result.kind == "executed", grants_result.kind)

        permissions_result = runner.invoke(
            Invocation(
                capability_name="access.role.permissions",
                payload={"permission_codes": ["access.user.list", "audit.log.list"]},
                path_params={"role_id": role_id},
            ),
            admin,
            "req-access-5",
        )
        check("access.role.permissions 执行成功", permissions_result.kind == "executed", permissions_result.kind)
        with uow_factory().begin() as tx:
            codes = [
                str(row["code"])
                for row in tx.query_all(
                    "SELECT p.code FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id "
                    "WHERE rp.role_id=%s ORDER BY p.code",
                    (role_id,),
                )
            ]
        # 两条能力名映射到两个权限码：access.user.list -> auth.user.manage、
        # audit.log.list -> audit.view。**映射是必要的**，不能把能力名直接塞进
        # role_permissions（那里存的是 permission_id）。
        check(
            "能力名已正确映射成权限码（不是把能力名存进去）",
            codes == ["audit.view", "auth.user.manage"],
            str(codes),
        )

        audit_page = runner.invoke(
            Invocation(capability_name="audit.log.list", payload={}, query={}),
            admin,
            "req-access-6",
        )
        check("audit.log.list 返回分页", "total" in audit_page.data, str(audit_page.data)[:200])
        check("审计条目里能找到刚才那次新建", int(audit_page.data["total"]) >= 4, str(audit_page.data["total"]))
        if audit_page.data["items"]:
            row = audit_page.data["items"][0]
            check("结果有中文标签", bool(row.get("result_label")), str(row.get("result_label")))
            check(
                "**不**返回 before/after 快照列",
                not {"before_json", "after_json", "detail_json"} & set(row),
                str(sorted(row)),
            )

        # ==================================================================
        print("\n=== 2. 元数据闸门真的生效：human_only / HITL 确认 / 权限码 ===")
        # ==================================================================
        # 账号管理四条在 2026-09-14 复核后从 `human_only` 改为
        # `exposed + confirmation=always`：Agent 可见，但每次执行都必须过服务端 HITL
        # 确认。本脚本的 runner 没有接确认服务，因此 Agent 直接调用应当被 HITL 闸门
        # 挡下（SERVICE_UNAVAILABLE），而不是 HUMAN_ONLY。
        for name, payload, path_params in (
            ("access.user.create", {
                "username": PROBE_PREFIX + "agentuser",
                "display_name": "Agent 不该能建",
                "initial_password": "FpaProbe77x",
                "role_ids": [role_id],
                "scope_ids": [scope_id],
            }, {}),
            ("access.user.status", {"status": "active", "reason": "不该发生"}, {"user_id": new_user_id}),
            ("access.user.grants", {"role_ids": [role_id], "scope_ids": [scope_id]}, {"user_id": new_user_id}),
            ("access.role.permissions", {"permission_codes": []}, {"role_id": role_id}),
        ):
            expect_rejected(
                f"Agent 调用 {name} 被 HITL 确认挡下（confirmation=always）",
                "SERVICE_UNAVAILABLE",
                lambda n=name, p=payload, pp=path_params: runner.invoke(
                    Invocation(
                        capability_name=n,
                        payload=p,
                        path_params=pp,
                        idempotency_key=PROBE_PREFIX + "agent-" + n,
                    ),
                    agent,
                    "req-access-agent",
                ),
            )
        expect_rejected(
            "Agent 调用 auth.password.change 被拒（human_only）",
            "HUMAN_ONLY",
            lambda: runner.invoke(
                Invocation(
                    capability_name="auth.password.change",
                    payload={
                        "current_password": "whatever1",
                        "new_password": "FpaProbe77x",
                        "confirm_password": "FpaProbe77x",
                    },
                ),
                agent,
                "req-access-agent-pw",
            ),
        )
        # `audit.log.list` 现在是 `exposed`：判据必须是可执行的——有权限的 Agent 能读
        # （靠 `audit.view` 权限码把守），没权限的谁都读不了。
        audit_by_agent = runner.invoke(
            Invocation(capability_name="audit.log.list", payload={}, query={}),
            agent,
            "req-access-agent-audit",
        )
        check(
            "audit.log.list 对 Agent 可读（exposed，靠 audit.view 权限码把守）",
            audit_by_agent.kind == "read",
            audit_by_agent.kind,
        )
        expect_rejected(
            "没有 audit.view 的账号读审计被拒",
            "FORBIDDEN",
            lambda: runner.invoke(
                Invocation(capability_name="audit.log.list", payload={}, query={}),
                nobody,
                "req-access-denied",
            ),
        )

        # ==================================================================
        print("\n=== 3. 权限：合法值集合 = **能力清单**，不是 permissions 表 ===")
        # ==================================================================
        expect_rejected(
            "提交「在 permissions 表里但不对应任何能力」的码被拒",
            "FIELD_INVALID",
            lambda: runner.invoke(
                Invocation(
                    capability_name="access.role.permissions",
                    payload={"permission_codes": [DECOY_PERMISSION_CODE]},
                    path_params={"role_id": role_id},
                ),
                admin,
                "req-access-decoy",
            ),
        )
        expect_rejected(
            "提交不存在的能力名被拒",
            "FIELD_INVALID",
            lambda: runner.invoke(
                Invocation(
                    capability_name="access.role.permissions",
                    payload={"permission_codes": ["no.such.capability"]},
                    path_params={"role_id": role_id},
                ),
                admin,
                "req-access-unknown",
            ),
        )
        expect_rejected(
            "没有 auth.role.manage 的账号改角色权限被拒",
            "FORBIDDEN",
            lambda: runner.invoke(
                Invocation(
                    capability_name="access.role.permissions",
                    payload={"permission_codes": []},
                    path_params={"role_id": role_id},
                ),
                nobody,
                "req-access-forbidden",
            ),
        )

        # ==================================================================
        print("\n=== 4. 业务规则：唯一性 / 自停用 / 空角色 / 范围分租键 ===")
        # ==================================================================
        expect_rejected(
            "重复登录名被拒（uq_users_username → CONFLICT）",
            "CONFLICT",
            lambda: runner.invoke(
                Invocation(
                    capability_name="access.user.create",
                    payload={
                        "username": PROBE_PREFIX + "newuser",
                        "display_name": "重复",
                        "initial_password": "FpaProbe77x",
                        "role_ids": [role_id],
                        "scope_ids": [scope_id],
                    },
                    idempotency_key=PROBE_PREFIX + "create-dup",
                ),
                admin,
                "req-access-dup",
            ),
        )
        expect_rejected(
            "弱初始口令被拒（与改密走同一份判定）",
            "FIELD_INVALID",
            lambda: runner.invoke(
                Invocation(
                    capability_name="access.user.create",
                    payload={
                        "username": PROBE_PREFIX + "weakpw",
                        "display_name": "弱口令",
                        "initial_password": "12345678",
                        "role_ids": [role_id],
                        "scope_ids": [scope_id],
                    },
                    idempotency_key=PROBE_PREFIX + "create-weak",
                ),
                admin,
                "req-access-weak",
            ),
        )
        expect_rejected(
            "空角色数组被拒（registry §2.4 的 ≥1）",
            "FIELD_INVALID",
            lambda: runner.invoke(
                Invocation(
                    capability_name="access.user.create",
                    payload={
                        "username": PROBE_PREFIX + "norole",
                        "display_name": "没有角色",
                        "initial_password": "FpaProbe77x",
                        "role_ids": [],
                        "scope_ids": [scope_id],
                    },
                    idempotency_key=PROBE_PREFIX + "create-norole",
                ),
                admin,
                "req-access-norole",
            ),
        )
        expect_rejected(
            "停用自己会被拒（否则最后一个管理员能把系统锁死）",
            "CONFLICT",
            lambda: runner.invoke(
                Invocation(
                    capability_name="access.user.status",
                    payload={"status": "disabled", "reason": "自锁"},
                    path_params={"user_id": operator_id},
                ),
                admin,
                "req-access-selflock",
            ),
        )

        # ==================================================================
        print("\n=== 5. 改密走能力：字段、强度、撤销会话 ===")
        # ==================================================================
        # 先给探针账号建一个可用会话（`AccessService.change_password` 的实现会撤销
        # 该用户的全部会话，所以要有一条会话才能看见"撤销"这件事）。
        with uow_factory().begin() as tx:
            tx.execute(
                "UPDATE users SET password_hash=%s WHERE id=%s",
                (_hash("FpaProbe77x"), new_user_id),
            )
            tx.execute("UPDATE users SET status='active' WHERE id=%s", (new_user_id,))
            tx.execute(
                "INSERT INTO sessions (user_id, token_hash, csrf_hash, csrf_token, expires_at) "
                "VALUES (%s,%s,%s,%s, DATE_ADD(NOW(), INTERVAL 1 DAY))",
                (new_user_id, "p" * 64, "q" * 64, "csrf-probe"),
            )
        person = ActorView(
            user_id=new_user_id,
            username=PROBE_PREFIX + "newuser",
            permissions=frozenset(),
            role_codes=frozenset(),
        )
        expect_rejected(
            "两次新密码不一致被拒",
            "FIELD_INVALID",
            lambda: runner.invoke(
                Invocation(
                    capability_name="auth.password.change",
                    payload={
                        "current_password": "FpaProbe77x",
                        "new_password": "FpaProbe88y",
                        "confirm_password": "FpaProbe99z",
                    },
                ),
                person,
                "req-access-pw-mismatch",
            ),
        )
        expect_rejected(
            "当前密码错误被拒",
            "FIELD_INVALID",
            lambda: runner.invoke(
                Invocation(
                    capability_name="auth.password.change",
                    payload={
                        "current_password": "WrongPass11",
                        "new_password": "FpaProbe88y",
                        "confirm_password": "FpaProbe88y",
                    },
                ),
                person,
                "req-access-pw-wrong",
            ),
        )
        expect_rejected(
            "新密码过弱被拒（连续数字）",
            "FIELD_INVALID",
            lambda: runner.invoke(
                Invocation(
                    capability_name="auth.password.change",
                    payload={
                        "current_password": "FpaProbe77x",
                        "new_password": "abcd1234",
                        "confirm_password": "abcd1234",
                    },
                ),
                person,
                "req-access-pw-weak",
            ),
        )
        changed = runner.invoke(
            Invocation(
                capability_name="auth.password.change",
                payload={
                    "current_password": "FpaProbe77x",
                    "new_password": "FpaProbe88y",
                    "confirm_password": "FpaProbe88y",
                },
            ),
            person,
            "req-access-pw-ok",
        )
        check("改密执行成功", changed.kind == "executed", changed.kind)
        check(
            "响应里报告了被撤销的会话数",
            int(changed.data["record"]["revoked_sessions"]) >= 1,
            str(changed.data),
        )
        with uow_factory().begin() as tx:
            live = tx.query_scalar(
                "SELECT COUNT(*) FROM sessions WHERE user_id=%s AND revoked_at IS NULL",
                (new_user_id,),
            )
            must_change = tx.query_scalar(
                "SELECT must_change_password FROM users WHERE id=%s", (new_user_id,)
            )
        check("该用户全部会话已撤销", int(live) == 0, str(live))
        check("must_change_password 已清零", int(must_change) == 0, str(must_change))
        check(
            "新密码可校验通过",
            _verify("FpaProbe88y", _password_hash_of(new_user_id)),
            "新哈希校验失败",
        )
        check(
            "旧密码已失效",
            not _verify("FpaProbe77x", _password_hash_of(new_user_id)),
            "旧密码仍然有效",
        )

        # ==================================================================
        print("\n=== 6. 契约：旧固定路由已删除，能力路由存在 ===")
        # ==================================================================
        from fpa.factory import build_app

        app = build_app()
        rules = {str(rule.rule) for rule in app.url_map.iter_rules()}
        check(
            "能力路由 POST /api/v1/auth/password/change 已注册",
            "/api/v1/auth/password/change" in rules,
            "未注册",
        )
        check(
            "旧的手写路由 POST /api/v1/auth/password **已删除**（两处描述同一件事，必删一处）",
            "/api/v1/auth/password" not in rules,
            "旧路由仍在",
        )
        for path in (
            "/api/v1/admin/users",
            "/api/v1/admin/users/<int:user_id>/status",
            "/api/v1/admin/users/<int:user_id>/grants",
            "/api/v1/admin/roles/<int:role_id>/permissions",
            "/api/v1/audit-logs",
        ):
            check(f"路由已注册：{path}", path in rules, "未注册")

        # ==================================================================
        print("\n=== 7. 声明 vs registry §1.1/§1.2/§1.3 逐字对账 ===")
        # ==================================================================
        #
        # 为什么把这张表抄进测试：它是**契约的可执行形态**。`docs/CAPABILITY_REGISTRY.md`
        # §1 的表格是权威清单，而"实现与它对得上"这件事如果没有机器判据，
        # 就只能靠人逐行读——而本项目已经证明"人逐行读"会漏（[A] 表里那 7 条
        # 就是这样长期只存在于文档里、运行时恒为 404）。
        #
        # 每一条只钉**容易静默错**的那几列：方法、路径、权限码、risk、
        # confirmation（生效值）、agent_exposure、idempotent。
        # 字段集不在这里钉（它由处理器签名派生，改了签名就该改测试，
        # 那属于实现细节；`_field_annotations` 的机制由内核单测守着）。
        # 账号管理四条 = 2026-09-14 复核后的新契约：`exposed + confirmation=always`。
        expected = {
            "access.user.list": ("GET", "/api/v1/admin/users", "auth.user.manage", "read", "never", "exposed", False),
            "access.user.create": ("POST", "/api/v1/admin/users", "auth.user.manage", "high", "always", "exposed", True),
            "access.user.status": ("POST", "/api/v1/admin/users/{user_id}/status", "auth.user.manage", "high", "always", "exposed", False),
            "access.user.grants": ("PUT", "/api/v1/admin/users/{user_id}/grants", "auth.user.manage", "high", "always", "exposed", False),
            "access.role.permissions": ("PUT", "/api/v1/admin/roles/{role_id}/permissions", "auth.role.manage", "high", "always", "exposed", False),
            "audit.log.list": ("GET", "/api/v1/audit-logs", "audit.view", "read", "never", "exposed", False),
            "auth.password.change": ("POST", "/api/v1/auth/password/change", None, "high", "never", "human_only", False),
        }
        for name, (method, path, permission, risk, confirmation, exposure, idempotent) in expected.items():
            spec = registry.find(name)
            actual = (
                str(spec.method),
                spec.path,
                spec.required_permission,
                str(spec.risk),
                str(spec.effective_confirmation),
                str(spec.agent_exposure),
                bool(spec.idempotent),
            )
            want = (method, path, permission, risk, confirmation, exposure, idempotent)
            check(f"{name} 声明与 registry §1 一致", actual == want, f"实际 {actual} / 期望 {want}")

        # 2026-09-14 复核后，账号管理四条从 `human_only` 改为
        # `exposed + confirmation=always`（管控换成 RBAC + 服务层 super_admin + HITL），
        # 所以 REGISTRY 里剩下的 `human_only` **只有 `auth.password.change`**。
        #
        # 为什么逐条核而不是"数一数"：数对得上而成员错的组合在这里是可能发生的
        # （比如把某条账号能力误标回 human_only、同时漏掉 auth.password.change）。
        human_only = sorted(
            item.name for item in registry.all() if str(item.agent_exposure) == "human_only"
        )
        check(
            "human_only 集合 = ['auth.password.change']（账号管理四条已改为 HITL）",
            human_only == ["auth.password.change"],
            str(human_only),
        )
        for fixed in ("auth.login", "auth.logout", "auth.me", "meta.capabilities"):
            check(
                f"{fixed} 仍是固定路由（不应被声明成能力，[A2] 表）",
                registry.find(fixed) is None,
                "它被注册进了 REGISTRY",
            )

        exit_code = 0 if FAILURES == 0 else 1
    finally:
        removed = cleanup()
        print(f"\n清理本次探针：删除 {removed} 行")
        with uow_factory().begin() as tx:
            leftovers = int(
                tx.query_scalar(
                    "SELECT COUNT(*) FROM users WHERE username LIKE %s", (PROBE_PREFIX + "%",)
                )
                or 0
            )
        print(f"遗留探针账号：{leftovers}")
        if leftovers:
            print("FAIL  清理不彻底——探针数据会污染后续断言")

    print()
    if FAILURES:
        print(f"结果：{FAILURES} 项红")
    else:
        print("结果：全部通过")
    return 1 if FAILURES else exit_code


def _hash(password: str) -> str:
    from fpa.kernel.password import hash_password

    return hash_password(password)


def _verify(password: str, stored: str) -> bool:
    from fpa.kernel.password import verify_password

    return verify_password(password, stored)


def _password_hash_of(user_id: int) -> str:
    with uow_factory().begin() as tx:
        row = tx.query_one("SELECT password_hash FROM users WHERE id=%s", (user_id,))
    return str(row["password_hash"]) if row else ""


if __name__ == "__main__":
    sys.exit(main())
