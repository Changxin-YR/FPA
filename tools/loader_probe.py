"""t15 的端到端证明：`Capability.loader=` 真的修好了写路径的回读步骤。

## 它证明什么

修复前（等价于 `domains/` 的现状）：写能力没有回读函数，执行器在**回读**步骤抛
`INTERNAL_ERROR` —— 写入其实已经落库，但 `executed` 永远返回不了。这正是
"生产路径下所有写能力都会 500，而七套自检全绿"的那个缺口。

修复后：`loader=` 一条声明就把回读函数挂到 handler 上，`runner._reload_after` 拿到它，
回读成功，`executed` 正常返回。

## 为什么不用 `python -c` 跑

`Capability._field_annotations` 用 `typing.get_type_hints(handler, include_extras=True)`
解析 `Annotated[...]`；本仓所有模块都带 `from __future__ import annotations`，
在 `python -c` 的 `<string>` 命名空间里前向引用解析不到，字段收集会抛 TypeError。
所以处理器必须定义在**真实模块**里。这不是本脚本的取巧，而是内核已经声明过的约束：

    "能力处理器必须定义在模块级，且其标注中的自定义类型必须可导入"

用法（独立库，避免与其它域的 e2e 抢数据）::

    $env:MYSQL_DATABASE='fpa_sales'
    $env:MYSQL_USER='fpa'; $env:MYSQL_PASSWORD='fpa_dev_password'
    python tools/loader_probe.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fpa.kernel.audit import AuditWriter  # noqa: E402
from fpa.kernel.capability import (  # noqa: E402
    Capability,
    HandlerResult,
    HttpMethod,
    NO_LOADER,
    Registry,
    Risk,
)
from fpa.kernel.fields import f_str  # noqa: E402
from fpa.kernel.idempotency import IdempotencyStore  # noqa: E402
from fpa.kernel.runner import ActorView, CapabilityRunner, Invocation  # noqa: E402
from fpa.kernel.scope import Scope  # noqa: E402
from fpa.kernel.uow import ConnectionConfig, UnitOfWork  # noqa: E402

FAILURES = 0


def key(suffix: str) -> str:
    """每次运行都不同的幂等键。

    为什么不能用固定键：幂等预留是**持久化**的。失败路径按设计会把键标记为
    ``failed``（见 `runner._invoke_idempotent` 的收口注释），但如果探针在标记之前
    就崩了，键会停在 ``processing``，下一次运行会得到"相同请求正在处理中，请勿
    重复提交"——于是一条真实的断言被一条**环境噪声**顶掉，看起来像代码坏了。
    """
    import time

    return f"probe-{suffix}-{int(time.time() * 1000) % 10_000_000}"


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILURES
    if ok:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


class ProbeService:
    """一个最小的写服务，形状与 `domains/*/xxx_write.py` 一致。"""

    @staticmethod
    def create_row(
        tx: UnitOfWork,
        ctx: Any,
        scope: Scope,
        # 注意：`f_str(...)` 本身**就是** `Annotated[str, Field(...)]`。
        # 再套一层 `Annotated[str, f_str(...)]` 会得到
        # `Annotated[str, Annotated[str, Field(...)]]` —— `collect_fields` 的
        # `next(item for item in args[1:] if isinstance(item, Field))` 找不到 Field，
        # 于是**字段被静默丢空**，提交 `code` 反而被当成未知字段拒绝。
        # 这正是本仓记过的"字段集为空 → 校验放行一切/拒绝一切，且全程不报错"那一类。
        code: f_str("编号", required=True, max_length=64),
    ) -> HandlerResult:
        tx.execute(
            "INSERT INTO loader_probe_rows (code, created_by) VALUES (%s, %s)",
            (code, ctx.actor.user_id),
        )
        return HandlerResult(
            data={"code": code},
            resource_id=tx.last_insert_id(),
            message="已创建",
        )

    @staticmethod
    def load_row(tx: UnitOfWork, *, scope: Scope, record_id: int) -> dict[str, Any] | None:
        return tx.query_one(
            "SELECT id, code, row_version FROM loader_probe_rows WHERE id=%s",
            (record_id,),
        )

    @staticmethod
    def close_period(tx: UnitOfWork, ctx: Any, scope: Scope) -> HandlerResult:
        """一个**不产出可回读单据**的能力，用来验证 `NO_LOADER` 这一路。"""
        return HandlerResult(data={"closed": True}, resource_id=None, message="已关账")


class LegacyService:
    """修复前 `domains/` 的形态：写能力**没有**回读函数。

    刻意用**独立的函数对象**，不复用 `ProbeService.create_row`：
    `Capability.__post_init__` 把回读函数挂在 `handler` 这个**函数对象**上
    （属性属于函数，不属于 Capability）。两个能力共用同一个 handler 时，
    先注册的那个会给后者也"顺手挂上"，于是"未接线"这一路根本测不到。

    这是真实存在的陷阱，不只是本探针的取巧：**同一个服务方法被两条能力复用**
    （例如 `create` 与 `update` 指向同一个实现）时，loader 是共享的。当前设计下
    这是可接受的——共享 handler 意味着回读的本来就是同一张表，共享 loader 是对的。
    """

    @staticmethod
    def create_row(
        tx: UnitOfWork,
        ctx: Any,
        scope: Scope,
        code: f_str("编号", required=True, max_length=64),
    ) -> HandlerResult:
        tx.execute(
            "INSERT INTO loader_probe_rows (code, created_by) VALUES (%s, %s)",
            (code, ctx.actor.user_id),
        )
        return HandlerResult(
            data={"code": code},
            resource_id=tx.last_insert_id(),
            message="已创建",
        )


def config() -> ConnectionConfig:
    return ConnectionConfig(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "fpa"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "fpa_sales"),
    )


def uow_factory() -> UnitOfWork:
    return UnitOfWork(config())


class AllDataScope:
    def resolve(self, *, user_id: int, role_codes: frozenset[str]) -> Scope:
        return Scope.all_data(user_id=user_id)


def build_registry() -> Registry:
    registry = Registry()
    registry.register(
        Capability(
            name="probe.create_with_loader",
            title="带 loader 的写能力",
            domain="probe",
            handler=ProbeService.create_row,
            service_factory=ProbeService,
            method=HttpMethod.POST,
            path="/api/v1/probe-rows",
            kind="create",
            risk=Risk.NORMAL,
            idempotent=True,
            loader=ProbeService.load_row,
        )
    )
    registry.register(
        Capability(
            name="probe.create_without_loader",
            title="未接线的写能力（修复前的形态）",
            domain="probe",
            handler=LegacyService.create_row,
            service_factory=LegacyService,
            method=HttpMethod.POST,
            path="/api/v1/probe-rows-legacy",
            kind="create",
            risk=Risk.NORMAL,
            idempotent=True,
        )
    )
    registry.register(
        Capability(
            name="probe.close_period",
            title="不产出单据的动作",
            domain="probe",
            handler=ProbeService.close_period,
            service_factory=ProbeService,
            method=HttpMethod.POST,
            path="/api/v1/probe-periods/close",
            kind="action",
            risk=Risk.NORMAL,
            idempotent=True,
            loader=NO_LOADER,
        )
    )
    return registry


def main() -> int:
    uow = uow_factory()
    with uow.begin() as tx:
        tx.execute(
            "CREATE TABLE IF NOT EXISTS loader_probe_rows ("
            "  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,"
            "  code VARCHAR(64) NOT NULL,"
            "  row_version INT UNSIGNED NOT NULL DEFAULT 1,"
            "  created_by BIGINT UNSIGNED NOT NULL,"
            "  PRIMARY KEY (id)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        tx.execute("DELETE FROM loader_probe_rows")

    print("=== 1. loader= 声明：回读函数被挂到 handler 上 ===")
    # ★ 顺序要点：挂载发生在 `Capability.__post_init__`，所以**必须先构造注册表**，
    #   否则断言的是"还没接线的 handler"——一个因为顺序而永远失败的假失败。
    registry = build_registry()
    check(
        "loader=load_row -> handler.__fpa_load_by_id__ 已挂载",
        getattr(ProbeService.create_row, "__fpa_load_by_id__", None) is ProbeService.load_row,
    )
    check(
        "未声明 loader 的写能力 -> 没有 __fpa_load_by_id__（未接线状态）",
        getattr(LegacyService.create_row, "__fpa_load_by_id__", None) is None,
    )
    check(
        "loader=NO_LOADER -> 标记 __fpa_no_loader__，且不挂回读函数",
        getattr(ProbeService.close_period, "__fpa_no_loader__", False) is True
        and getattr(ProbeService.close_period, "__fpa_load_by_id__", None) is None,
    )
    check(
        "默认（不传 loader）与 NO_LOADER 可区分：前者不标记",
        not hasattr(LegacyService.create_row, "__fpa_no_loader__"),
    )

    runner = CapabilityRunner(
        registry=registry,
        uow_factory=uow_factory,
        audit=AuditWriter(),
        idempotency=IdempotencyStore(uow_factory),
        scope_resolver=AllDataScope(),
    )
    actor = ActorView(user_id=1, username="probe", permissions=frozenset())

    print()
    print("=== 2. 修复后的形态：executed 正常返回 ===")
    try:
        result = runner.invoke(
            Invocation(
                capability_name="probe.create_with_loader",
                payload={"code": "PROBE-OK"},
                idempotency_key=key("ok"),
            ),
            actor,
            "req-probe-ok",
        )
        check("kind=executed", result.kind == "executed", result.kind)
        check("返回真实主键", isinstance(result.resource_id, int) and result.resource_id > 0,
              str(result.resource_id))
        row = uow_factory()
        with row.begin() as tx:
            found = tx.query_one("SELECT code FROM loader_probe_rows WHERE id=%s", (result.resource_id,))
        check("库里确实有那一行", found is not None and found["code"] == "PROBE-OK", str(found))
    except Exception as error:  # noqa: BLE001
        check("修复后的形态应当成功", False, f"{type(error).__name__}: {error}")

    print()
    print("=== 3. 未接线的形态：必须抛 INTERNAL_ERROR，不得静默降级 ===")
    before = None
    probe = uow_factory()
    with probe.begin() as tx:
        before = tx.query_one("SELECT COUNT(*) AS n FROM loader_probe_rows")
    try:
        result = runner.invoke(
            Invocation(
                capability_name="probe.create_without_loader",
                payload={"code": "PROBE-LEGACY"},
                idempotency_key=key("legacy"),
            ),
            actor,
            "req-probe-legacy",
        )
        check("未接线时不得返回 executed", False, f"实际返回 {result.kind}")
    except Exception as error:  # noqa: BLE001
        code = getattr(error, "code", None)
        check("抛 INTERNAL_ERROR", str(code) == "INTERNAL_ERROR", f"实际 {code}")

    after = uow_factory()
    with after.begin() as tx:
        post = tx.query_one("SELECT COUNT(*) AS n FROM loader_probe_rows")
    check(
        "失败的整体回滚（未接线的写入没有留下来）",
        int(post["n"]) == int(before["n"]),
        f"{before['n']} -> {post['n']}",
    )

    print()
    print("=== 4. NO_LOADER：不产出单据的动作正常执行 ===")
    try:
        result = runner.invoke(
            Invocation(
                capability_name="probe.close_period",
                payload={},
                idempotency_key=key("period"),
            ),
            actor,
            "req-probe-period",
        )
        check("kind=executed", result.kind == "executed", result.kind)
    except Exception as error:  # noqa: BLE001
        check("NO_LOADER 的能力应当成功", False, f"{type(error).__name__}: {error}")

    print()
    if FAILURES:
        print(f"FAILED: {FAILURES} 项")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
