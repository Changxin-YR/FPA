"""Flask 应用工厂与路由自动生成。

**核心*：路由不手写，由 `Capability` 声明生成。新增一个业务能力只需要写声明，
不需要碰 HTTP 层。这消除早期版本"加一条能力要改路由、校验表、Agent 策略、OpenAPI
四处"的问题。

分工：
    * `product` 层（早期版本）→ 消失，被本文件的 `_register_capability_routes` 取代
    * 业务逻辑 → `domains/*/service.py`
    * 受控执行 → `kernel/runner.py`

本文件只做三件事：翻译 HTTP ↔ 内核调用、注入可信身份、统一错误响应。
"""

from __future__ import annotations

import re
from typing import Any

from flask import Flask, Response, current_app, jsonify, request
from werkzeug.exceptions import HTTPException

from yuxin.kernel.capability import HttpMethod, Registry
from yuxin.kernel.errors import DomainError, ErrorCode
from yuxin.kernel.runner import ActorView, CapabilityRunner, Invocation
from yuxin.kernel.uow import translate_mysql_error
from yuxin.domains.access.scope_resolver import DataScopeResolver
from yuxin.domains.access.service import AccessService
from yuxin.web.context import bind_request_context, current_request_id, request_meta
from yuxin.web.security import CSRF_HEADER, SESSION_COOKIE, require_csrf

#: `{pond_id}` → `<int:pond_id>`。
#:
#: 路径参数**一律当整数**：本项目所有主键都是 BIGINT UNSIGNED，用 Flask 的 int
#: 转换器能让 `/ponds/abc` 直接 404 而不是带着字符串走到业务层。类型在边界处收窄，
#: 内部就不需要再判。
_PATH_PARAM = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def create_app(
    *,
    registry: Registry,
    runner: CapabilityRunner,
    access: AccessService,
    scope_resolver: DataScopeResolver,
    secret_key: str,
    agent_turn_handler: Any | None = None,
    agent_turn_stream_handler: Any | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key,
        JSON_SORT_KEYS=False,
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    )
    _install_wire_format(app)
    # 通过 config 注入依赖，而不是用模块级单例：
    # 测试可以传入替身，且同一个进程里可以并存多个配置不同的 app。
    app.config["YUXIN_REGISTRY"] = registry
    app.config["YUXIN_RUNNER"] = runner
    app.config["YUXIN_ACCESS"] = access
    app.config["YUXIN_SCOPE_RESOLVER"] = scope_resolver

    _register_middleware(app)

    from yuxin.web.routes_auth import register_auth_routes

    register_auth_routes(app)
    _register_capability_routes(app, registry)
    _register_meta_routes(app)

    # Agent 路由**总是**注册。
    #
    # 初版写成"传了 agent_turn_handler 才注册"——那是把两件事混成了一件：
    # `/tools` 与 `/tools/{name}/call`（插件走的高频路径）**不需要** turn handler，
    # 只有 `/turns` 需要。结果是插件启动时拉工具清单直接 404，
    # 而插件报出来的是 `GatewayUnavailableError: 请求的资源不存在`——
    # 看着像"网关不可用"，实际是路由从未注册。
    #
    # `/turns` 在缺 handler 时由路由内部返回 AGENT_UNAVAILABLE：
    # 运行期的问题报在运行期，而不是让整组路由消失（让启动期的问题伪装成别的现象）。
    app.config["YUXIN_AGENT_TURN"] = agent_turn_handler
    # 流式版本同理：缺它时 `/turns/stream` 报 AGENT_UNAVAILABLE（而不是让路由消失）。
    # 两者分开配：`AgentPanel` 先试流式、只有 404/405 才回退非流式 ——
    # 若把流式做成"缺了就 404"，面板会**每次都**并发一条注定 404 的请求（噪音）。
    app.config["YUXIN_AGENT_TURN_STREAM"] = agent_turn_stream_handler
    from yuxin.web.routes_agent import register_agent_routes

    register_agent_routes(app)

    _register_error_handlers(app)
    return app


# ---------------------------------------------------------------------------
# 线上格式（JSON 的日期与小数口径）
# ---------------------------------------------------------------------------


def _install_wire_format(app: Flask) -> None:
    """把 JSON 序列化的**两处默认行为**改成用户能读的形态。

    ## 为什么必须改，以及为什么改在**这一处**

    Flask 默认的 `DefaultJSONProvider` 对两个类型有"自己的一套决定"：

    | 类型 | Flask 默认输出 | 用户看到的问题 |
    |---|---|---|
    | `datetime` / `date` | `Wed, 02 Sep 2026 00:00:00 GMT`（RFC 1123） | 日期列直接暴露 HTTP 头格式的英文串 |
    | `Decimal` | `str(value)`，如 `100.0000000` | 金额 8 位小数（而且位数随域变化：有的 7 位是计算得来） |

    实测：销售单接口一次响应里同时命中两处 —— `sold_at='Wed, 02 Sep 2026 00:00:00 GMT'`、
    `total_amount='100.0000000'`。**这不是某一列写错了**：`datetime` 与 `Decimal`
    在所有域都是同一个类型，所以症状遍布 25 个日期列与 10 个金额列。

    ## 为什么放在应用装配处，而不是每个 `decorate()` 里各转一遍

    本项目已有 22 处 `decorated["xxx_label"]`、以及若干处把日期转成字符串的代码 ——
    再多一处"每个域各自 str()"就是**同一件事在 N 处描述**：任何新增的日期列都会
    默认漏掉（因为它不需要人写代码就会出现在响应里）。放在 JSON provider 上，
    新增列**自动**是对的，这是"能表达成一处的事实不留第二处"的直接应用。

    口径选择：
      * **日期时间 → ISO 8601**（`2026-09-02T00:00:00`）。它是 `date.fromisoformat` /
        `datetime.fromisoformat` 的直接反函数，前端 `<input type="date">` 的
        `value` 也要求 ISO（`YYYY-MM-DD`）。**不在这里做中文格式化**
        （`2026年9月2日`）：那是**展示**决定，属于前端；后端给的是**可解析**的值。
      * **`date` 只输出日期部分**（`2026-09-02`），不补 `T00:00:00`。
        补了会让纯日期列与日期时间列在 JSON 里长得一样，而它们在语义上不同
        （`sold_at` 是日期、`created_at` 是时刻）。
      * **金额 → 保留原值但去掉无意义的尾随零**？**不**。`Decimal` 的位数是**列定义**
        决定的业务事实（`DECIMAL(16,2)` 就是 2 位小数），序列化层删零会让
        "100.00" 与 "100" 在不同列表现不一致。所以这里**原样输出**
        （`str(value)`），位数问题归**列定义/服务层**管 —— 见
        `docs/DISPLAY_CONTRACT.md` 的口径表。
    """

    class _WireJSONProvider(app.json_provider_class):  # type: ignore[misc, name-defined]
        """ISO 日期 + 原样小数的 JSON provider。"""

        def default(self, obj: Any) -> Any:
            from datetime import date, datetime

            # `datetime` 必须在 `date` **之前**判：`datetime` 是 `date` 的子类，
            # 反过来写会让所有时刻退化成日期（静默丢时间）。
            if isinstance(obj, datetime):
                return obj.isoformat()
            if isinstance(obj, date):
                return obj.isoformat()
            return super().default(obj)

    app.json_provider_class = _WireJSONProvider
    app.json = _WireJSONProvider(app)


# ---------------------------------------------------------------------------
# 中间件
# ---------------------------------------------------------------------------


def _register_middleware(app: Flask) -> None:
    @app.before_request
    def _bind() -> Any:
        bind_request_context()
        # 全局 CSRF 前置校验。
        #
        # 早期版本是 34 处手写 `require_csrf()` 散落在 12 个文件里、没有统一拦截器——
        # 后果是"新增写端点可以合法地忘记加"。这里改成默认拒绝：
        # 非安全方法一律要求令牌，白名单显式豁免。
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if not _csrf_exempt():
                require_csrf()
        return None

    @app.after_request
    def _stamp(response: Response) -> Response:
        response.headers["X-Request-ID"] = current_request_id()
        # ★ API 响应一律**不可缓存**。
        #
        # 实测缺陷（用户报「创建完成后要刷新一下才会出来」）：
        # 本钩子原先只发 `X-Request-ID`，**没有 `Cache-Control`**，
        # 于是 `GET /api/v1/<resource>?...` 在浏览器看来是**可缓存**的。
        # 后果：写入成功后重新拉列表，拿回的是**缓存里的旧值** ——
        # 数据库里有新行、接口也返回它，只有界面不动；
        # 用户手动刷新之所以「能好」，是因为**整页重载绕过了缓存** ——
        # 这正好把根因掩盖了。
        # 已验证：同一条创建请求后，**绕开浏览器**调列表接口能看到新行（total 2 -> 3），
        # 而浏览器页面仍是 2。
        #
        # 为什么放在服务端而不是前端 `fetch(..., { cache: 'no-store' })`：
        # 「这个响应能不能被缓存」是**资源自己的属性**，不是调用方的选项；
        # 写在这里一处就覆盖全部 `/api/` 端点，新增端点也不会漏。
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


#: CSRF 豁免路径。每加一条都要有理由。
_CSRF_EXEMPT = frozenset(
    {
        "/api/v1/auth/login",  # 登录时尚未持有会话，无从取得令牌
        "/api/v1/agent/tools",  # 由 Harness 插件用上下文令牌调用，不走浏览器会话
    }
)

#: 「初始密码未改」时**唯一放行**的路径前缀。
#:
#: 只放身份自身的端点，且**必须**放行 —— 否则改密请求本身会被闸门拦死（死锁）：
#:   * `/auth/password/change` 要会话；
#:   * `/auth/me` 是前端判断"该不该去改密页"的依据；
#:   * `/auth/csrf`、`/auth/logout` 是任何已登录状态都要能用的。
_PASSWORD_CHANGE_EXEMPT_PREFIXES = ("/api/v1/auth/",)


def _csrf_exempt() -> bool:
    """CSRF 豁免判定。

    两条豁免各有理由：
      * 登录：此刻尚无会话，无从取得令牌；
      * `X-Agent-Context`：Harness 插件与浏览器是**两套凭据**，
        上下文令牌由后端签发并绑定 user/session/capability/nonce/expiry，
        它自身就是一次性的授权证明，不需要再叠一层 CSRF。
    """
    if request.path in _CSRF_EXEMPT:
        return True
    return bool(request.headers.get("X-Agent-Context"))


# ---------------------------------------------------------------------------
# 路由生成
# ---------------------------------------------------------------------------


def _flask_path(template: str) -> str:
    def convert(match: re.Match[str]) -> str:
        name = match.group(1)
        # 当前唯一的非数字路径参数是会计期间（YYYY-MM）；其余能力路径参数
        # 都是资源主键。类型在这里集中声明，避免把期间误注册成整数导致 404。
        return f"<{name}>" if name == "period" else f"<int:{name}>"

    return _PATH_PARAM.sub(convert, template)


def _register_capability_routes(app: Flask, registry: Registry) -> None:
    """为每个能力注册一条 Flask 规则。

    端点名用能力名（`.` 换成 `_`），这样 `url_for('pond_create')` 可用，
    且冲突会在启动时暴露而不是运行时。
    """
    used_endpoints: set[str] = set()
    for capability in registry.all():
        endpoint = "cap_" + capability.name.replace(".", "_").replace("-", "_")
        if endpoint in used_endpoints:
            raise RuntimeError(
                f"端点名冲突：{capability.name} → {endpoint}。"
                "能力名必须唯一到可安全转成 Python 标识符——这条约束能防止"
                "两条能力映射到同一个 URL 规则而不自知。"
            )
        used_endpoints.add(endpoint)
        app.add_url_rule(
            _flask_path(capability.path),
            endpoint=endpoint,
            view_func=_make_view(capability.name),
            methods=[str(capability.method)],
            provide_automatic_options=False,
        )


def _make_view(capability_name: str) -> Any:
    """构造一个视图函数。

    刻意做成闭包而不是 `functools.partial`：Flask 的 `view_func.__name__` 参与路由
    注册，闭包能让每个端点在调试器里显示成对应的能力名。
    """

    def view(**path_params: Any) -> Response:
        # 用 Flask 的 current_app 而不是把 app 塞进 g —— 后者是我从前一个项目的
        # 写法带过来的臆造属性，会在首次请求时 AttributeError，而因为它在视图
        # 函数体内，启动时不会暴露。
        runner: CapabilityRunner = current_app.config["YUXIN_RUNNER"]
        actor = _current_actor()
        payload, query = _read_input()
        result = runner.invoke(
            Invocation(
                capability_name=capability_name,
                payload=payload,
                raw_payload=dict(payload),
                path_params=path_params,
                query=query,
                idempotency_key=request.headers.get("Idempotency-Key"),
            ),
            actor,
            current_request_id(),
        )
        return _ok(result)

    view.__name__ = capability_name.replace(".", "_")
    view.__qualname__ = view.__name__
    return view


def _read_input() -> tuple[dict[str, Any], dict[str, Any]]:
    """读请求体与查询串。

    请求体**必须是对象**——早期版本在 `json_object()` 里做了同样的事。数组或标量会让
    后续的字段校验失去意义（`payload.get` 直接 AttributeError）。
    """
    payload: dict[str, Any] = {}
    if request.method != "GET" and request.get_data(cache=True).strip():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "请求内容必须是 JSON 对象")
        payload = data
    query = {key: value for key, value in request.args.items()}
    return payload, query


def _current_actor() -> ActorView:
    """把 HTTP 请求翻译成可信的 ActorView。

    **身份只来自 Cookie 里的会话令牌**，不接受任何请求体/请求头声称的 user_id 或 role。
    这是需求里"禁止让前端告诉 Agent 用户是什么权限"那条的落点。

    ## 强制改密的**服务端闸门**（P1 实测缺陷）

    服务端把 `users.must_change_password` 折算进 `/auth/me` 的 `status`，前端据此把用户
    送到改密页 —— 但**执行路径上没有任何一层读它**，于是"初始口令未改"的账号登录后
    直接访问 `/api/v1/purchase-orders` 依然 **200**：改密可以被整段绕过，初始口令（管理员
    知道的）继续有效。

    判据放在这里，因为它是**唯一的执行身份入口**：所有能力路由、`/meta/capabilities`、
    Agent 的每一个端点都从这里取 actor，一处拦住就全拦住；而 `/auth/*` 走的是
    `routes_auth.py` 里的会话直查，不在本函数内（改密本身必须放行，否则死锁）。
    """
    access: AccessService = current_app.config["YUXIN_ACCESS"]
    token = request.cookies.get(SESSION_COOKIE, "")
    user = access.resolve_session(token)
    if user.must_change_password and not request.path.startswith(
        _PASSWORD_CHANGE_EXEMPT_PREFIXES
    ):
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "初始密码尚未修改：请先修改密码，再使用业务功能",
            data={"reason": "must_change_password"},
        )
    return ActorView(
        user_id=user.user_id,
        username=user.username,
        permissions=user.permissions,
        role_codes=user.role_codes,
        session_hash=user.session_hash,
        is_agent=bool(request.headers.get("X-Agent-Context")),
    )


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------


def _register_meta_routes(app: Flask) -> None:
    from yuxin.web.workflow_meta import actions_payload, resources_payload

    @app.get("/api/v1/meta/capabilities")
    def meta_capabilities() -> Response:
        """前端菜单、表单、表格的**唯一**数据来源。

        L1 过滤的服务端形态：只返回当前用户有权调用的能力。因此前端"看不到的按钮"
        和 Agent"拿不到的工具"来自同一份过滤结果——两者不可能不一致。
        """
        registry_: Registry = current_app.config["YUXIN_REGISTRY"]
        actor = _current_actor()
        visible = registry_.visible_to(actor.permissions)
        # 必须套响应信封（INTERFACES.md §1）。裸对象会让前端 client.ts 读到
        # undefined 的 payload.code 而判定失败——这类缺陷只在联调时暴露。
        return jsonify(
            {
                "code": "OK",
                "message": "",
                "data": {
                    "capabilities": [item.to_meta() for item in visible],
                    "resources": resources_payload(),
                    # 动作词与其**中文标签**。原先只下发 `capabilities` + `resources`，
                    # 于是前端拿不到动作标签，`DataTable.vue` 只能把裸 token 渲染出来
                    # （实测：操作列写着 `view` / `archive`，而状态列是中文）。
                    # 标签的唯一来源是 `kernel/workflow.py::ACTION_LABELS`。
                    "actions": actions_payload(),
                },
                "request_id": current_request_id(),
            }
        )


# ---------------------------------------------------------------------------
# 响应
# ---------------------------------------------------------------------------


def _ok(result: Any) -> Response:
    body: dict[str, Any] = {
        "code": "OK",
        "message": result.message or "",
        "request_id": current_request_id(),
    }
    if result.kind == "confirmation_required":
        body["data"] = {
            "kind": "confirmation_required",
            "confirmation": result.confirmation,
        }
    else:
        body["data"] = result.data
        if result.resource_id is not None:
            body["data"] = {"resource_id": result.resource_id, "record": result.data}
    return jsonify(body)


#: HTTP 状态码 → (业务错误码, 中文消息)。
#:
#: 集中一处，而不是在每个 handler 里各写一遍分支——早期版本正是把这段映射写在
#: `app.py` 的一个 dict 里，那个做法是对的，继承。
_HTTP_ERROR_MESSAGES: dict[int, tuple[str, str]] = {
    400: ("BAD_REQUEST", "请求格式无效"),
    401: ("UNAUTHENTICATED", "请先登录"),
    403: ("FORBIDDEN", "当前账号无权执行该操作"),
    404: ("NOT_FOUND", "请求的资源不存在"),
    405: ("METHOD_NOT_ALLOWED", "请求方法不受支持"),
    409: ("CONFLICT", "请求与当前数据状态冲突"),
    413: ("PAYLOAD_TOO_LARGE", "上传内容过大"),
    415: ("UNSUPPORTED_MEDIA_TYPE", "请求内容类型不受支持"),
    422: ("VALIDATION_ERROR", "请求内容未通过校验"),
    429: ("RATE_LIMITED", "请求过于频繁，请稍后重试"),
}

def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(DomainError)
    def _domain_error(error: DomainError) -> tuple[Response, int]:
        return (
            jsonify(
                {
                    "code": str(error.code),
                    "message": error.message,
                    "data": error.data,
                    "request_id": current_request_id(),
                }
            ),
            error.status,
        )

    @app.errorhandler(HTTPException)
    def _http_error(error: HTTPException) -> tuple[Response, int]:
        """把 Flask 自己的 HTTP 异常翻译成业务信封。

        **为什么必须显式注册**：`@app.errorhandler(Exception)` **会捕获
        `HTTPException`**——Flask 的路由 404/405 也走异常路径。不显式处理的话，
        访问不存在的 URL 会返回 500 而不是 404，把客户端的错误当成服务端故障。
        实测踩过：`POST /api/v1/ponds/delete-all` 返回 500。

        这类"错误分类错误"比不报错更糟：它会让排查方向跑偏——开发者会去查服务端
        日志，而真实原因只是路径写错了。
        """
        status = error.code or 500
        code, message = _HTTP_ERROR_MESSAGES.get(
            status, (str(ErrorCode.INTERNAL_ERROR), "请求暂时无法处理")
        )
        return (
            jsonify(
                {
                    "code": code,
                    "message": message,
                    "data": None,
                    "request_id": current_request_id(),
                }
            ),
            status,
        )

    @app.errorhandler(Exception)
    def _unexpected(error: Exception) -> tuple[Response, int]:
        # MySQL 错误翻译单点处理。早期版本在每个 store 里各写一遍 errno 判断，
        # 而且用**异常消息文本**匹配约束类型（`if key not in str(exc)`）——
        # 那会随 MySQL 版本变化而失效。
        translated = translate_mysql_error(error)
        if translated is not None:
            return _domain_error(translated)
        app.logger.exception("未处理异常 request_id=%s", current_request_id(), exc_info=error)
        return (
            jsonify(
                {
                    "code": str(ErrorCode.INTERNAL_ERROR),
                    "message": "服务器暂时无法处理请求",
                    "data": None,
                    "request_id": current_request_id(),
                }
            ),
            500,
        )


__all__ = ["CSRF_HEADER", "SESSION_COOKIE", "create_app"]
