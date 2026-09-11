"""应用工厂：**真实服务进程的默认装配入口**。

## 为什么需要这个文件

`fpa.bootstrap` 补齐之后，"能力声明"有了装载处；但**真实服务进程**仍然没有装配入口：

* `web/app.py::create_app()` 要求调用方把 `registry` / `runner` / `access` /
  `scope_resolver` 全部显式传进来；
* 全仓库只有 `tools/web_e2e.py` 与 `tools/live_agent_e2e.py` 会这么传，且它们传的是
  **自己手搓的夹具注册表**（`web_e2e.build_registry()` 只注册 `pond.create` 一条）；
* `deploy/` 是空的。

后果与 `bootstrap.py` 自己指认的那类失败**完全同形**：域能力在夹具里"看起来是有的"
（七套自检全绿），而在真实进程里根本没有路由。**"装配处与装载处是两处"** ——
所以本文件把装配也收敛成**一处**：`fpa.bootstrap.load_all()` 拿注册表，
其余依赖按同一份环境变量装配。

## 用法

    # 真实进程（gunicorn / wsgi）
    gunicorn -w 4 "fpa.wsgi:app"

    # 代码里
    from fpa.factory import build_app
    app = build_app()                     # registry 默认走组合根

    # 需要替身/夹具时（现有的 e2e 工具就是这么用的）
    app = build_app(registry=my_registry)

    # 只要组装好的依赖、不要 Flask app
    from fpa.factory import assemble
    deps = assemble()                      # deps.registry / runner / access / gateway …

## 一条刻意的边界

本模块**不 import 任何业务域**。`web/` 与装配层都不该知道"有哪些域"——
域的发现是 `fpa.bootstrap` 的职责（它按目录发现 `domains/*/capabilities.py`）。
这里只调用 `load_all()`，于是"新增一个域"不需要修改本文件。

至于"其余依赖从哪里来"：`fpa.domains.access` 提供的 `AccessService` /
`DataScopeResolver` 是**装配期**依赖（内核只声明 `ScopeResolver` Protocol），
所以 import 它们不违反"内核不依赖业务层"这条架构约束（`web/` 也不被这条约束覆盖）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fpa.agent.gateway import AgentToolGateway
from fpa.domains.access.scope_resolver import DataScopeResolver
from fpa.domains.access.service import AccessService
from fpa.harness.session import HarnessSessionManager
from fpa.kernel.audit import AuditWriter
from fpa.kernel.capability import REGISTRY, Registry
from fpa.kernel.confirmation import ConfirmationGate, ConfirmationStore
from fpa.kernel.idempotency import IdempotencyStore
from fpa.kernel.runner import CapabilityRunner
from fpa.kernel.uow_factory import uow_factory
from fpa.settings import Settings
from fpa.web.agent_turn import (
    build_agent_turn_handler,
    build_agent_turn_stream_lines,
    resolve_gateway_url,
)
from fpa.web.app import create_app


@dataclass(slots=True)
class Assembled:
    """一次装配的全部产物。

    把 `app` 与各依赖放在一起返回，是为了让测试/工具能直接拿到 runner 或 gateway，
    而不是从 `app.config` 里摸（`app.config["FPA_RUNNER"]` 那种写法把"装配结果"变成
    了运行期才知道的字符串键）。
    """

    app: Any
    registry: Registry
    runner: CapabilityRunner
    access: AccessService
    scope_resolver: DataScopeResolver
    audit: AuditWriter
    confirmation: ConfirmationGate
    gateway: AgentToolGateway
    settings: Settings
    #: Harness 会话池。放在装配产物里，一是让 `build_app` 能在进程退出时关掉子进程，
    #: 二是让测试能拿到它做断言（而不是从 `app.config` 里摸）。
    agent_sessions: Any = None
    #: `/api/v1/agent/turns` 的处理器。**由 `assemble()` 构造**，见那里的注释。
    agent_turn_handler: Any = None
    #: `/api/v1/agent/turns/stream` 的**行生成器工厂**。同样由 `assemble()` 构造。
    agent_turn_stream_handler: Any = None


def assemble(
    *,
    registry: Registry | None = None,
    settings: Settings | None = None,
    uow_factory_override: Any | None = None,
) -> Assembled:
    """组装全部依赖；`registry=None` 时**默认走组合根**。

    `registry=None` 这个默认值是本模块存在的理由：真实进程不该"记得"去 load_all()。
    """
    resolved_settings = settings or Settings.from_env()
    factory = uow_factory_override or uow_factory

    if registry is None:
        # 延迟 import：`fpa.bootstrap` 会遍历并 import 全部域，放在函数里可以让
        # "只想拿一份 Settings" 的调用方不必承担装载全部域的开销。
        import fpa.bootstrap as bootstrap

        registry = bootstrap.load_all()

    audit = AuditWriter()
    confirmation = ConfirmationGate(
        ConfirmationStore(factory),
        ttl_seconds=resolved_settings.agent_confirmation_ttl_seconds,
    )
    scope_resolver = DataScopeResolver(factory)
    runner = CapabilityRunner(
        registry=registry,
        uow_factory=factory,
        audit=audit,
        idempotency=IdempotencyStore(factory),
        scope_resolver=scope_resolver,
        confirmation_gate=confirmation,
    )
    access = AccessService(factory)
    gateway = AgentToolGateway(
        registry=registry,
        runner=runner,
        audit=audit,
        confirmation=confirmation,
        uow_factory=factory,
    )

    # ★ 对话轮次的处理器在**装配处**构造（不是 `build_app` 的可选参数）。
    #
    # 这一处修的是本项目第三次犯的同型缺陷：`HarnessSessionManager` 实现齐全、
    # `routes_agent` 也在等 `app.config["FPA_AGENT_TURN"]`，但**全仓没有任何代码实例化它**
    # —— 生产路径（`fpa.wsgi:app`）因此静默地什么都不装：应用启动正常、全部 e2e 全绿，
    # 而用户在界面上"看不到智能助手"、接口返回 `503 AGENT_UNAVAILABLE`。
    # `build_app` 上那个 `agent_turn_handler` 可选参数保留给夹具/测试；
    # **生产路径不再有"记得传一个 handler"这一步**（与上面 gateway 那次同一形态）。
    agent_sessions = HarnessSessionManager(resolved_settings)
    agent_turn_handler = build_agent_turn_handler(
        settings=resolved_settings,
        manager=agent_sessions,
        gateway_url=resolve_gateway_url(resolved_settings),
        #: 判"这一轮写了没有"要用它把工具名反查成能力名/资源名（见 `agent_turn.py`
        #: 的 `_tool_name_index`）。不传就退化成"永远返回 assistant"，
        #: 前端也就永远不知道该刷新 —— 正是 t14 那个"写完要手动刷新"的缺陷。
        registry=registry,
    )
    #: 流式版本：同一个 manager、同一份令牌/提示词/结果整形，只是**边跑边交付**。
    #: 它是 `/agent/turns/stream` 的工厂；接上它之后长任务不会再被代理的空闲超时掐断
    #: （502 的根因，详见 `HarnessSessionManager.run()` 的 `on_notification` 说明）。
    resolved_gateway_url = resolve_gateway_url(resolved_settings)

    def agent_turn_stream_handler(
        *, actor: Any, message: str, conversation_id: str, page_context: str
    ) -> Any:
        return build_agent_turn_stream_lines(
            settings=resolved_settings,
            manager=agent_sessions,
            gateway_url=resolved_gateway_url,
            registry=registry,
            actor=actor,
            message=message,
            conversation_id=conversation_id,
            page_context=page_context,
        )

    return Assembled(
        app=None,  # type: ignore[arg-type]  # 由 build_app 填
        registry=registry,
        runner=runner,
        access=access,
        scope_resolver=scope_resolver,
        audit=audit,
        confirmation=confirmation,
        gateway=gateway,
        settings=resolved_settings,
        agent_sessions=agent_sessions,
        agent_turn_handler=agent_turn_handler,
        agent_turn_stream_handler=agent_turn_stream_handler,
    )


def build_app(
    *,
    registry: Registry | None = None,
    settings: Settings | None = None,
    uow_factory_override: Any | None = None,
    agent_turn_handler: Any | None = None,
) -> Any:
    """构造 Flask app。

    `registry=None`（默认）→ 走组合根 `fpa.bootstrap.load_all()`；
    显式传入 → 用调用方的注册表（现有的 e2e 夹具路径保持不变）。
    """
    deps = assemble(
        registry=registry,
        settings=settings,
        uow_factory_override=uow_factory_override,
    )
    # `agent_turn_handler` 显式传入时优先（夹具/测试用）；否则用 `assemble()` 装好的那个。
    # **生产路径从不传**，所以它拿到的一定是装配处构造的那个 —— 这正是本任务要的形态。
    app = create_app(
        registry=deps.registry,
        runner=deps.runner,
        access=deps.access,
        scope_resolver=deps.scope_resolver,
        secret_key=deps.settings.secret_key,
        agent_turn_handler=agent_turn_handler or deps.agent_turn_handler,
        # 流式 handler 同样由装配处提供：**生产路径不传**，所以它拿到的一定是装好的那个。
        agent_turn_stream_handler=deps.agent_turn_stream_handler,
    )
    # Agent 网关也在这里接线：它原本要调用方自己 `app.config["FPA_AGENT_GATEWAY"] = gateway`，
    # 而那正是"装配处与使用处分离"的另一个落点（忘记赋值 -> Agent 路由 503，
    # 而 app 启动一切正常）。装配本该没有可忘记的步骤。
    app.config["FPA_AGENT_GATEWAY"] = deps.gateway
    deps.app = app
    return app


def registry_from_composition_root() -> Registry:
    """组合根装载的注册表（供自检工具与测试直接使用）。"""
    import fpa.bootstrap as bootstrap

    return bootstrap.load_all()


__all__ = ["Assembled", "assemble", "build_app", "registry_from_composition_root", "REGISTRY"]
