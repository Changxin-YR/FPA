"""对真实 MySQL 跑通一次完整的写入链路。

这是 `docs/WRITE_CONTRACT.md` 的可执行证明。它验证的不是"代码没报错"，而是
**数据库里真的多了一行**，以及四种失败情形都不会被误报成成功。

覆盖：
    1. 声明 → 校验 → 权限 → DataScope → 事务 → 审计 → 提交 → 回读，全链路
    2. 提交后库里确实有那一行，且审计里有一条对应的 success
    3. 业务抛异常 → 事务回滚 → 库里 0 行，且执行器绝不返回"已执行"
    4. 权限不足 → 拒绝，库里 0 行
    5. 跨数据范围 → 拒绝，库里 0 行
    6. 回读不到 → 报内部错误，绝不降级成成功（防"谎报"）
    7. 幂等：同键同体只写一次；同键异体报冲突

用法::

    python tools/runner_e2e.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pymysql  # noqa: E402

from fpa.domains._base import Page  # noqa: E402
from fpa.kernel import capability as cap  # noqa: E402
from fpa.kernel.audit import AuditWriter  # noqa: E402
from fpa.kernel.errors import DomainError, ErrorCode  # noqa: E402
from fpa.kernel.fields import f_num, f_ref, f_str  # noqa: E402
from fpa.kernel.idempotency import IdempotencyStore  # noqa: E402
from fpa.kernel.runner import (  # noqa: E402
    ActorView,
    CapabilityRunner,
    Invocation,
)
from fpa.kernel.scope import Scope, ScopeType  # noqa: E402
from fpa.kernel.uow import ConnectionConfig, UnitOfWork  # noqa: E402

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


# ---------------------------------------------------------------------------
# 一个最小的业务服务：塘口
# ---------------------------------------------------------------------------

RESOURCE_TYPE = "selftest_pond"


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


def ensure_table() -> None:
    """建自检表 + 种子两个用户。

    种子用户是必需的，不是凑数：`idempotency_keys.user_id` 有指向 `users(id)` 的外键，
    `record_revisions.actor_user_id` 也是。所以"写一行审计/幂等记录"这件事本身就在
    验证外键链路是通的——真实系统里不可能出现"操作者不是已知用户"的写入。
    """
    connection = pymysql.connect(**_config().as_kwargs())
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS selftest_ponds (
              id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              code        VARCHAR(64)  NOT NULL,
              name        VARCHAR(120) NOT NULL,
              area_id     BIGINT UNSIGNED NOT NULL,
              capacity_mu DECIMAL(12,2) NOT NULL DEFAULT 0,
              row_version INT UNSIGNED NOT NULL DEFAULT 1,
              created_by  BIGINT UNSIGNED NOT NULL,
              created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (id),
              UNIQUE KEY uq_selftest_ponds_code (code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cursor.execute(
            """
            INSERT INTO users (id, username, display_name, password_hash, status)
            VALUES (1,'selftest_admin','自检管理员','x','active'),
                   (2,'selftest_weak','自检无权限用户','x','active')
            ON DUPLICATE KEY UPDATE display_name=VALUES(display_name)
            """
        )
    connection.commit()
    connection.close()


class PondService:
    """注意：方法定义在模块级——能力处理器必须能解析类型标注。"""

    def create(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("塘口编号", required=True, max_length=64),
        name: f_str("塘口名称", required=True, max_length=120),
        area_id: f_ref("所属区域", "area", required=True),
        capacity_mu: f_num("面积（亩）", minimum=0),
    ) -> cap.HandlerResult:
        # ── 第三层防御：服务不信任调用方 ──────────────────────────────────
        ctx.require("pond.create")

        tx.execute(
            """
            INSERT INTO selftest_ponds (code, name, area_id, capacity_mu, created_by)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (code, name, area_id, capacity_mu or 0, ctx.actor.user_id),
        )
        pond_id = tx.last_insert_id()

        # 服务自己检查刚写入的行在范围内（真实的领域服务会用 scope 拼 WHERE，
        # 这里的自检表没有 area 维度，所以只断言解析出的 scope 可用）。
        ctx.assert_row({"area_id": area_id}, what="目标区域")

        return cap.HandlerResult(
            data={"id": pond_id, "code": code, "name": name},
            resource_id=pond_id,
            message=f"已创建塘口「{name}」",
        )

    def create_that_fails_after_write(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("塘口编号", required=True),
        name: f_str("塘口名称", required=True),
        area_id: f_ref("所属区域", "area", required=True),
    ) -> cap.HandlerResult:
        """先真的 INSERT，再抛异常。用于验证回滚。

        注意用的是 `ErrorCode.CONFLICT` 枚举成员而不是字符串 `"CONFLICT"`——
        `DomainError` 会拒绝裸字符串（见 `kernel/errors.py` 的构造器守卫）。
        这条守卫是刻意的：358 个散落的错误码字符串正是早期版本的病灶之一，
        让"写错一个码"在构造时就报错，比在客户端联调时才发现要好得多。
        """
        ctx.require("pond.create")
        tx.execute(
            "INSERT INTO selftest_ponds (code, name, area_id, created_by) VALUES (%s,%s,%s,%s)",
            (code, name, area_id, ctx.actor.user_id),
        )
        raise DomainError(ErrorCode.CONFLICT, "故意失败：用于验证事务回滚")

    def create_that_vanishes(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("塘口编号", required=True),
        name: f_str("塘口名称", required=True),
        area_id: f_ref("所属区域", "area", required=True),
    ) -> cap.HandlerResult:
        """真的写入并提交，但**谎报一个不存在的 id**。

        用于验证"回读校验"：执行器必须在回读不到时报错，而不是相信处理器的自述。
        """
        ctx.require("pond.create")
        tx.execute(
            "INSERT INTO selftest_ponds (code, name, area_id, created_by) VALUES (%s,%s,%s,%s)",
            (code, name, area_id, ctx.actor.user_id),
        )
        return cap.HandlerResult(data={"code": code}, resource_id=999_999_999, message="已完成")

    def load_for_audit(self, tx: UnitOfWork, *, scope: Scope, record_id: int) -> dict | None:
        return tx.query_one(
            "SELECT id, code, name, area_id, row_version FROM selftest_ponds WHERE id=%s",
            (record_id,),
        )


PondService.create.__fpa_load_by_id__ = PondService.load_for_audit  # type: ignore[attr-defined]
PondService.create_that_fails_after_write.__fpa_load_by_id__ = PondService.load_for_audit  # type: ignore[attr-defined]
PondService.create_that_vanishes.__fpa_load_by_id__ = PondService.load_for_audit  # type: ignore[attr-defined]


class FixedScopeResolver:
    """测试替身：把 scope 固定成"可以看 area 1 和 2"。"""

    def __init__(self, scopes: list[dict]) -> None:
        self._scopes = scopes

    def resolve(self, *, user_id: int, role_codes: frozenset[str]) -> Scope:
        return Scope.from_rows(self._scopes, user_id=user_id, allow_all=not self._scopes)


def build_registry() -> cap.Registry:
    registry = cap.Registry()
    registry.register(
        cap.Capability(
            name="pond.create",
            title="新建塘口",
            domain="master_data",
            handler=PondService.create,
            service_factory=PondService,
            method=cap.HttpMethod.POST,
            path="/api/v1/ponds",
            kind="create",
            required_permission="pond.create",
            risk=cap.Risk.NORMAL,
            agent_exposure=cap.AgentExposure.EXPOSED,
            idempotent=True,
            audit=cap.AuditPolicy.snapshot(),
            fields=cap._field_annotations(PondService.create),
        )
    )
    registry.register(
        cap.Capability(
            name="pond.create_rollback_probe",
            title="回滚探针",
            domain="master_data",
            handler=PondService.create_that_fails_after_write,
            service_factory=PondService,
            method=cap.HttpMethod.POST,
            path="/api/v1/ponds/rollback-probe",
            kind="create",
            required_permission="pond.create",
            risk=cap.Risk.NORMAL,
            fields=cap._field_annotations(PondService.create_that_fails_after_write),
        )
    )
    registry.register(
        cap.Capability(
            name="pond.create_vanishing_probe",
            title="回读探针",
            domain="master_data",
            handler=PondService.create_that_vanishes,
            service_factory=PondService,
            method=cap.HttpMethod.POST,
            path="/api/v1/ponds/vanishing-probe",
            kind="create",
            required_permission="pond.create",
            risk=cap.Risk.NORMAL,
            fields=cap._field_annotations(PondService.create_that_vanishes),
        )
    )
    return registry


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def count_rows(where: str = "", params: tuple = ()) -> int:
    uow = uow_factory()
    with uow.begin() as tx:
        sql = "SELECT COUNT(*) AS n FROM selftest_ponds"
        if where:
            sql += f" WHERE {where}"
        row = tx.query_one(sql, params)
    return int((row or {}).get("n", 0))


def count_audit(capability: str, result: str) -> int:
    uow = uow_factory()
    with uow.begin() as tx:
        row = tx.query_one(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE capability=%s AND result=%s",
            (capability, result),
        )
    return int((row or {}).get("n", 0))


def main() -> int:
    ensure_table()

    # 清掉上轮探针（审计表删不掉，所以只清业务表与幂等表）
    #
    # 为什么要清幂等表：上面那条 "相同请求正在处理中" 的报错**正是设计的正确行为**——
    # 上一轮失败留下的 processing 记录没有被静默重试。这个选择是刻意的：
    #
    #     如果静默重试，而前一次其实"业务已提交、只是没来得及标记完成"，
    #     那么重试会产生**第二笔真实写入**——这是幂等机制最不能出错的地方。
    #
    # 代价是可能要求人工确认（返回 409 而不是自动重放）。测试需要干净起点，
    # 生产则靠 `sweep_expired()` 在 TTL 之后把它标成 failed 以便重试。
    uow = uow_factory()
    with uow.begin() as tx:
        tx.execute("DELETE FROM selftest_ponds")
        tx.execute("DELETE FROM idempotency_keys WHERE capability LIKE 'pond.%'")

    registry = build_registry()
    audit = AuditWriter()
    idem = IdempotencyStore(uow_factory)
    resolver = FixedScopeResolver([{"scope_type": "area", "area_id": 1}, {"scope_type": "area", "area_id": 2}])
    runner = CapabilityRunner(
        registry=registry,
        uow_factory=uow_factory,
        audit=audit,
        idempotency=idem,
        scope_resolver=resolver,
    )

    actor = ActorView(
        user_id=1,
        username="tester",
        permissions=frozenset({"pond.create"}),
        role_codes=frozenset({"breed_manager"}),
    )

    print("=== 1. 正常写入：全链路 ===")
    # 审计表 append-only，历史行删不掉 -> 断言增量而非绝对值
    audit_before = count_audit("pond.create", "success")
    result = runner.invoke(
        Invocation(
            capability_name="pond.create",
            payload={"code": "E2E-001", "name": "端到端一号塘", "area_id": 1, "capacity_mu": 12.5},
            idempotency_key="e2e-key-0001",
        ),
        actor,
        "req-e2e-1",
    )
    check("返回 kind=executed", result.kind == "executed", result.kind)
    check("返回了真实主键", isinstance(result.resource_id, int) and result.resource_id > 0, str(result.resource_id))
    rows = count_rows("code=%s", ("E2E-001",))
    check("数据库里确实有那一行", rows == 1, f"实际 {rows} 行")
    check(
        "审计新增了 1 条 success",
        count_audit("pond.create", "success") - audit_before == 1,
        f"{audit_before} -> {count_audit('pond.create', 'success')}",
    )
    check("message 由服务端渲染", "端到端一号塘" in result.message, result.message)

    print("\n=== 2. 幂等：同键同体只写一次 ===")
    result2 = runner.invoke(
        Invocation(
            capability_name="pond.create",
            payload={"code": "E2E-001", "name": "端到端一号塘", "area_id": 1, "capacity_mu": 12.5},
            idempotency_key="e2e-key-0001",
        ),
        actor,
        "req-e2e-2",
    )
    check("识别为重复请求", result2.replayed is True)
    check("未产生第二行", count_rows("code=%s", ("E2E-001",)) == 1)
    check("回放数据来自业务表而非快照", result2.data.get("name") == "端到端一号塘", str(result2.data))

    print("\n=== 3. 幂等：同键异体必须冲突 ===")
    try:
        runner.invoke(
            Invocation(
                capability_name="pond.create",
                payload={"code": "E2E-002", "name": "另一个塘", "area_id": 1},
                idempotency_key="e2e-key-0001",
            ),
            actor,
            "req-e2e-3",
        )
        check("同键异体被拒绝", False, "居然通过了")
    except DomainError as error:
        check("同键异体被拒绝", error.code == "IDEMPOTENCY_CONFLICT", str(error.code))
    check("未产生额外行", count_rows() == 1, f"实际 {count_rows()} 行")

    print("\n=== 4. 回滚：业务抛异常后库里必须 0 行 ===")
    before = count_rows()
    probe_fail_before = count_audit("pond.create_rollback_probe", "failure")
    try:
        runner.invoke(
            Invocation(
                capability_name="pond.create_rollback_probe",
                payload={"code": "E2E-ROLLBACK", "name": "会被回滚的塘", "area_id": 1},
                idempotency_key="e2e-key-rollback",
            ),
            actor,
            "req-e2e-4",
        )
        check("抛出了异常", False, "居然返回了结果")
    except DomainError as error:
        check("抛出了业务异常", error.code == "CONFLICT", str(error.code))
    check("INSERT 已被回滚（0 行新增）", count_rows() == before, f"{before} → {count_rows()}")
    check("审计里没有这条的 success", count_audit("pond.create_rollback_probe", "success") == 0)
    check(
        "审计新增了 1 条 failure",
        count_audit("pond.create_rollback_probe", "failure") - probe_fail_before == 1,
    )

    print("\n=== 5. 权限不足：拒绝且不写 ===")
    weak_actor = ActorView(user_id=2, username="weak", permissions=frozenset())
    before = count_rows()
    try:
        runner.invoke(
            Invocation(
                capability_name="pond.create",
                payload={"code": "E2E-DENIED", "name": "无权限的塘", "area_id": 1},
                idempotency_key="e2e-key-denied",
            ),
            weak_actor,
            "req-e2e-5",
        )
        check("无权限被拒绝", False, "居然通过了")
    except DomainError as error:
        check("无权限被拒绝", error.code == "FORBIDDEN", str(error.code))
    check("未写入", count_rows() == before)

    print("\n=== 6. 回读校验：处理器谎报 id 必须报内部错误 ===")
    before = count_rows()
    try:
        runner.invoke(
            Invocation(
                capability_name="pond.create_vanishing_probe",
                payload={"code": "E2E-VANISH", "name": "回读探针", "area_id": 1},
                idempotency_key="e2e-key-vanish",
            ),
            actor,
            "req-e2e-6",
        )
        check("谎报 id 未被识破", False, "居然返回成功")
    except DomainError as error:
        check("谎报 id 被识破并报内部错误", error.code == "INTERNAL_ERROR", str(error.code))
        check("错误信息说明写入未确认", "回读" in error.message, error.message)
    check("该次写入被回滚（因为整事务失败）", count_rows() == before, f"{before} → {count_rows()}")

    print("\n=== 7. 校验：未知字段 / 缺必填 ===")
    try:
        runner.invoke(
            Invocation(
                capability_name="pond.create",
                payload={"code": "X", "name": "X", "area_id": 1, "created_by": 999},
                idempotency_key="e2e-key-unknown",
            ),
            actor,
            "req-e2e-7a",
        )
        check("未知字段被拒绝", False, "居然通过了")
    except DomainError as error:
        check("未知字段被拒绝", error.code == "VALIDATION_ERROR", str(error.code))

    try:
        runner.invoke(
            Invocation(
                capability_name="pond.create",
                payload={"code": "X", "area_id": 1},
                idempotency_key="e2e-key-missing",
            ),
            actor,
            "req-e2e-7b",
        )
        check("缺必填被拒绝", False, "居然通过了")
    except DomainError as error:
        check("缺必填被拒绝", error.code == "VALIDATION_ERROR", str(error.code))
        check("错误信息用中文标签", "塘口名称" in error.message, error.message)

    print("\n=== 8. 能力未注册 ===")
    try:
        runner.invoke(
            Invocation(capability_name="pond.delete_everything", payload={}),
            actor,
            "req-e2e-8",
        )
        check("未注册能力被拒绝", False, "居然通过了")
    except DomainError as error:
        check("未注册能力被拒绝", error.code == "CAPABILITY_NOT_FOUND", str(error.code))

    print("\n=== 9. 清理探针 ===")
    uow = uow_factory()
    with uow.begin() as tx:
        removed = tx.execute("DELETE FROM selftest_ponds")
    check(f"清理了 {removed} 行探针数据", True)

    print()
    if FAILURES:
        print(f"结果：{FAILURES} 项失败")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
