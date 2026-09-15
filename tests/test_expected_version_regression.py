"""回归测试：**声明了 `expected_version` 的处理器必须真的收到它**。

## 这条测试守的是哪个缺陷

`kernel/capability.py` 里曾经有两个各自看起来合理、合起来致命的设置：

1. `_FRAMEWORK_ARGS` 把 `expected_version` 列为"框架注入参数"，于是
   `_field_annotations()` 从处理器签名收集字段时把它**剔除**；
2. `validate_payload()` 在算"未知字段"时无条件放行它
   （`set(payload) - set(specs) - {"expected_version"}`）。

净效果：它**既不进字段表、也不被拒绝、也不传给处理器**。于是任何按文档
（registry §0.7 规则 3 / §2.11）在签名里声明 `expected_version` 的处理器，
执行器调它时都会少传一个参数：

    TypeError: xxx() missing 1 required positional argument: 'expected_version'

实测命中：`cost.entry.confirm`（直接报错）、`pond.submit` / `pond.verify`
（**从未真正收到过它** —— master_data 的乐观锁一直没接上，且没有任何测试发现）。

## 为什么必须是这三种断言的组合

* 只断言"`Capability.fields` 里有它"**不够**；
* 只断言"`validate_payload` 保留了它"**不够**：值可能被保留却仍没传给处理器；
* 所以第三组走**真实执行器**，断言处理器**实际收到**的值。

"断言声明正确"与"断言行为正确"是两件事，而静默失效的缺陷总是只被前者覆盖——
这是本项目反复出现的形态。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from fpa.kernel import capability as cap
from fpa.kernel.errors import DomainError
from fpa.kernel.fields import FieldSet, f_int

#: 处理器**实际收到**的参数。模块级可变容器——让断言读真实调用结果，
#: 而不是读我们以为会发生的事情。
RECEIVED: dict[str, Any] = {}


class ProbeService:
    """探针服务。

    两个约束必须同时满足，否则执行器拿不到 handler：

      * `service_factory` 返回的对象上**必须存在与 handler 同名的属性**
        （`runner._resolve()` 用 `getattr(factory(), func.__name__)`），
        所以方法名必须与能力声明里传的函数名一致；
      * 类必须定义在**模块级**：`_field_annotations()` 用 `get_type_hints()`
        解析标注，嵌套类的标注会退化成字符串而无法收集（内核第一版踩过）。
    """

    def with_version(
        self,
        tx,
        ctx,
        scope,
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        RECEIVED.clear()
        RECEIVED["expected_version"] = expected_version
        # **不返回 resource_id**：这是本探针能走完执行路径的关键。
        # 返回 resource_id 会让 `runner._reload_after()` 去回读（ROLLOUT_CONTRACT §3），
        # 而本探针没有（也不该有）回读函数——那会以"缺少回读函数"的
        # INTERNAL_ERROR 掩盖我们要验的东西（`expected_version` 到底有没有传下去）。
        # 探针只该暴露一个变量：被测的那一个。
        return cap.HandlerResult(data={"ok": True}, resource_id=None)

    def without_version(self, tx, ctx, scope) -> cap.HandlerResult:
        RECEIVED.clear()
        RECEIVED["called"] = True
        return cap.HandlerResult(data={"ok": True}, resource_id=None)


# ---------------------------------------------------------------------------
# 一、声明期：能从处理器标注里收集到
# ---------------------------------------------------------------------------

def test_expected_version_is_collected_from_handler_annotation() -> None:
    """处理器把它声明成 Annotated 字段时，必须进字段表。

    缺陷形态：`_FRAMEWORK_ARGS` 把它当框架参数剔除 → 字段表里没有它 →
    `validate_payload` 随后把它丢掉。
    """
    fields = cap._field_annotations(ProbeService.with_version)
    assert "expected_version" in fields.fields, (
        "`expected_version` 没有从处理器标注里收集到 —— 它又回到 "
        "`_FRAMEWORK_ARGS` 里了？那会让所有在签名里声明它的 action 能力直接 TypeError。"
    )
    assert fields["expected_version"].required is True
    assert fields["expected_version"].minimum == 1


# ---------------------------------------------------------------------------
# 二、校验期：声明了就保留；没声明就拒绝（fail closed）
# ---------------------------------------------------------------------------

def _probe_capability(*, handler, name: str, fields: FieldSet | None = None) -> cap.Capability:
    kwargs: dict[str, Any] = {}
    if fields is not None:
        kwargs["fields"] = fields
    return cap.Capability(
        name=name,
        title="乐观锁回归探针",
        domain="probe",
        handler=handler,
        service_factory=ProbeService,
        method=cap.HttpMethod.POST,
        path=f"/api/v1/probe/{name}",
        kind="action",
        required_permission="probe.action",
        risk=cap.Risk.NORMAL,
        **kwargs,
    )


def test_validate_payload_keeps_declared_expected_version() -> None:
    spec = _probe_capability(
        handler=ProbeService.with_version,
        name="probe.declared",
        fields=cap._field_annotations(ProbeService.with_version),
    )
    cleaned = cap.validate_payload(spec, {"expected_version": 7})
    assert cleaned.get("expected_version") == 7, (
        f"`validate_payload` 丢弃了已声明的 expected_version：{cleaned}"
    )


def test_validate_payload_rejects_undeclared_expected_version() -> None:
    """未声明时必须报"不接受的字段"，而不是静默放行。

    缺陷的另一半就是那个无条件放行：它让"忘了声明"看起来没问题，
    直到执行器少传一个参数才炸，而那时栈已经离声明处很远了。
    """
    spec = _probe_capability(handler=ProbeService.without_version, name="probe.undeclared")
    assert "expected_version" not in spec.fields.fields
    with pytest.raises(DomainError) as caught:
        cap.validate_payload(spec, {"expected_version": 7})
    assert str(caught.value.code) == "VALIDATION_ERROR"
    assert "expected_version" in str(caught.value.message), (
        "拒绝信息应点名 expected_version，并给出可操作的提示"
    )


# ---------------------------------------------------------------------------
# 三、运行期：走真实执行器，断言处理器**实际收到**了它
# ---------------------------------------------------------------------------

class _NullTx:
    """最小事务替身：本探针不碰数据库。"""

    def query_one(self, sql, params=None):
        return None

    def query_all(self, sql, params=None):
        return []

    def query_scalar(self, sql, params=None):
        return None

    def execute(self, sql, params=None):
        return 1

    def last_insert_id(self) -> int:
        return 1


class _NullUow:
    def begin(self):
        @contextmanager
        def _scope():
            yield _NullTx()

        return _scope()


def test_runner_actually_passes_expected_version_to_the_handler() -> None:
    """**核心断言**：处理器必须收到调用方提交的期望版本号。

    这是唯一能抓住"字段表看起来对、值却没传下去"那种静默失效的形态。
    """
    from fpa.kernel.audit import AuditWriter
    from fpa.kernel.idempotency import IdempotencyStore
    from fpa.kernel.runner import ActorView, CapabilityRunner, Invocation
    from fpa.kernel.scope import Scope

    registry = cap.Registry()
    registry.register(
        _probe_capability(
            handler=ProbeService.with_version,
            name="probe.reaches_handler",
            fields=cap._field_annotations(ProbeService.with_version),
        )
    )

    class _Resolver:
        def resolve(self, *, user_id: int, role_codes) -> Scope:
            return Scope.all_data(user_id=user_id)

    RECEIVED.clear()
    runner = CapabilityRunner(
        registry=registry,
        uow_factory=_NullUow,
        audit=AuditWriter(),
        idempotency=IdempotencyStore(_NullUow),
        scope_resolver=_Resolver(),
    )
    actor = ActorView(user_id=1, username="probe",
                      permissions=frozenset({"probe.action"}))

    runner.invoke(
        Invocation(capability_name="probe.reaches_handler",
                   payload={"expected_version": 42}),
        actor,
        "req-expected-version",
    )

    assert RECEIVED.get("expected_version") == 42, (
        "处理器没有收到 expected_version —— 乐观锁在真实执行路径上是失效的。"
        f"实际收到：{RECEIVED}"
    )


# ---------------------------------------------------------------------------
# 四、真实注册表上的正向覆盖（防止断言只在探针上成立）
# ---------------------------------------------------------------------------

def test_real_action_capabilities_declare_expected_version() -> None:
    """真实注册表里，凡签名里有 `expected_version` 的能力都必须能在字段表里查到它。

    参考 `tests/test_loader_contract.py` 的做法：反例证明断言会失败，
    真实数据证明它不该失败。这里把"检查了几条"打印出来，
    避免这条断言在真实数据上退化成**空跑**——那正是本项目记过的
    "看着像通过其实是空跑"的形态。

    ## 为什么逐域装载、而不是 `bootstrap.load_all()`

    并行开发期间，**任何一个**域的导入期错误（语法错误、无效的状态机声明、
    不存在的 API）都会让 `load_all()` 整体失败。本测试要守的是
    "`expected_version` 这一条契约"，不该被别人的进行中编辑判红——
    那样它给出的信号会从"契约被破坏"变成"某处又坏了"，而后者已经有别人在管。

    因此这里逐域 import，只跳过导不进来的域，并把跳过的域名打印出来
    （**不静默跳过**：静默跳过会让"没检查到"与"检查通过"长得一样）。
    """
    import importlib

    from fpa.bootstrap import _module_name, discover_domains
    from fpa.kernel.capability import REGISTRY

    skipped: list[str] = []
    for domain in discover_domains():
        try:
            importlib.import_module(_module_name(domain))
        except Exception as exc:  # noqa: BLE001 - 别人的域正在编辑，不是本测试的对象
            skipped.append(f"{domain}({type(exc).__name__})")

    offenders: list[str] = []
    checked: list[str] = []
    for item in REGISTRY.all():
        if item.is_read:
            continue
        code = getattr(item.handler, "__code__", None)
        if code is None or "expected_version" not in code.co_varnames:
            continue
        checked.append(item.name)
        if "expected_version" not in item.fields.fields:
            offenders.append(item.name)

    print(f"\n[expected_version 契约] 检查了 {len(checked)} 条声明的能力")
    for name in checked:
        print(f"    · {name}")
    if skipped:
        print(f"[expected_version 契约] 跳过装载失败的域：{skipped}")

    assert offenders == [], (
        "以下能力的处理器签名里有 expected_version，但字段表里没有它 —— "
        f"执行器会少传这个参数：{offenders}"
    )
    # 反向断言：确认真的检查到了东西。为 0 说明"没检查"，不是"检查通过"。
    assert checked, (
        "没有任何能力被检查到 —— 要么全部域装载失败，要么各域都还没有声明 "
        "expected_version 的 action 能力。**这不算通过**。"
    )
