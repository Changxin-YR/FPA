"""认证路由：登录、登出、当前用户、CSRF 令牌。

这四条是 `web/` 里**唯一手写的端点**（它们不属于业务能力：登录发生在"有身份"之前，
而能力执行器要求先有身份）。其余全部由 `Capability` 声明生成。

登录、登出在 `CAPABILITY_REGISTRY.md` 里标为 `human_only`——这正是
"身份/会话生命周期不能委托给智能体"那条边界的落点：让模型代登录等于把
"当前操作者是谁"交给模型决定，而三层权限校验的前提恰恰是会话属于真人。
"""

from __future__ import annotations

from typing import Any

from flask import Flask, Response, jsonify, request

from fpa.kernel.errors import DomainError, ErrorCode
from fpa.domains.access.service import AccessService
from fpa.web.context import client_ip, current_request_id, user_agent
from fpa.web.security import (
    clear_session_cookies,
    new_csrf_token,
    set_session_cookies,
)


def register_auth_routes(app: Flask) -> None:
    def _access() -> AccessService:
        return app.config["FPA_ACCESS"]

    def _envelope(data: Any, message: str = "") -> dict[str, Any]:
        return {
            "code": "OK",
            "message": message,
            "data": data,
            "request_id": current_request_id(),
        }

    def _user_payload(user: Any) -> dict[str, Any]:
        """把 `AuthenticatedUser` 包成**前端契约形状** `{"user": UserSummary}`。

        ## 为什么这里要包一层，而且 `/auth/login` 与 `/auth/me` 必须共用它

        t7 之前两处都返回**平铺**对象（`_envelope(user.to_me())`），而前端
        `LoginPage.vue:41` 与 `session.store.ts:38` **两处都**读 `data.user`
        —— 于是登录成功后 `data.user` 是 `undefined`，`session.setUser({...undefined})`
        直接把会话写成空对象。

        **共用这一个函数**是刻意的：两处各写一遍 `{"user": ...}` 就又是一处
        "两处描述同一件事"，而这次的教训正是"同一件事两处不一样"最难发现。
        """
        return {"user": user.to_summary()}

    @app.post("/api/v1/auth/login")
    def login() -> tuple[Response, int] | Response:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "请求内容必须是 JSON 对象")
        # 字段名 `identifier` 是**权威契约**（`docs/CAPABILITY_REGISTRY.md` §2.3，
        # 依据早期版本 `product/auth/routes.py:79-82`「字段名 identifier / password」）。
        #
        # 它原先读 `payload.get("username")`，而前端一直发 `identifier` ——
        # 于是拿到空串、抛「请输入账号和密码」。**这是 t7 阻断缺陷的第 2 处**：
        # 浏览器上表现为"明明填了账号密码，却提示请输入账号和密码"。
        #
        # `identifier` 的语义是"手机号**或**登录名"（§2.3 的约束列），而本版
        # `users` 表只有 `username`（001 迁移没有 phone 列，见 `access/admin.py`
        # 的字段差异表）——所以它当前只能按登录名匹配。**不要**为"支持手机号"
        # 在这里加第二个查询分支：那张列不存在，真要做需要一次迁移加列。
        identifier = str(payload.get("identifier") or "").strip()
        password = str(payload.get("password") or "")
        if not identifier or not password:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "请输入账号和密码")

        token, csrf_token, user = _access().login(
            username=identifier,
            password=password,
            ip_address=client_ip(),
            user_agent=user_agent(),
        )
        response = jsonify(_envelope(_user_payload(user), message="登录成功"))
        # Secure 从配置读：本地 http 开发必须为 False，否则浏览器不存 Cookie，
        # 表现为"登录成功但下一个请求 401"——很容易被误诊为后端 bug。
        set_session_cookies(
            response,
            session_token=token,
            csrf_token=csrf_token,
            secure=bool(app.config.get("SESSION_COOKIE_SECURE", False)),
        )
        return response

    @app.post("/api/v1/auth/logout")
    def logout() -> Response:
        from fpa.web.security import SESSION_COOKIE

        _access().logout(request.cookies.get(SESSION_COOKIE, ""))
        response = jsonify(_envelope(None, message="已退出登录"))
        clear_session_cookies(response)
        return response

    @app.get("/api/v1/auth/me")
    def me() -> Response:
        from fpa.web.app import _current_actor  # 局部导入避免循环依赖

        actor = _current_actor()
        # `_current_actor` 返回的是执行器视角的 ActorView（只有权限码），
        # 而这里要给前端展示姓名等信息，所以再查一次。这个"查两次"是有意的：
        # 执行路径不依赖展示字段，展示路径不依赖执行细节。
        detail = _access().resolve_session(request.cookies.get("fpa_session", ""))
        # **形状与 `/auth/login` 完全一致**（同一个 `_user_payload`）——
        # 两处不一致的话 `session.store.ts` 的 `load()` 与首次登录会拿到不同结构，
        # 而它只在"刷新页面"这条路径上才暴露。
        return jsonify(_envelope(_user_payload(detail)))

    @app.get("/api/v1/auth/csrf")
    def csrf_token() -> Response:
        """重新签发一个 CSRF 令牌。

        ## 它**必须**已有会话——而"登录"不在它的服务范围内

        t7 的阻断缺陷正是从这里来的：本端点要求有效会话（下面 `resolve_session` 会抛
        401），而前端 `client.ts` 把 POST 一律当写方法、于是**登录前**先来取令牌
        → 401 → 前端抛「CSRF Token 获取失败」。**登录需要令牌、令牌需要先登录。**

        服务端这一侧**是对的**（未登录时无令牌可发），所以修法在前端：
        `LoginPage` 调登录时传 `skipCsrf: true`——`/api/v1/auth/login` 本来就在
        `web/app.py::_CSRF_EXEMPT` 里（理由写着"登录时尚未持有会话，无从取得令牌"）。

        **别把这条注释当成"以后可以放宽"**：把登录所需的令牌预先发给匿名访客，
        等于让 CSRF 防护在最需要它的那个端点上失效。这条 401 是**正确行为**。

        存在的理由：前端在会话有效期内可能丢失 CSRF Cookie（用户清缓存、
        多标签页竞争）。没有这个端点的话，唯一恢复方式是重新登录——
        而用户会以为"系统坏了"。
        """
        from fpa.web.security import CSRF_COOKIE, SESSION_COOKIE

        session_token = request.cookies.get(SESSION_COOKIE, "")
        detail = _access().resolve_session(session_token)
        if not detail:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "登录状态已失效，请重新登录")
        token = new_csrf_token()
        _access().rotate_csrf_token(session_token=session_token, csrf_token=token)
        response = jsonify(_envelope({"csrf_token": token}))
        response.set_cookie(
            CSRF_COOKIE,
            token,
            max_age=12 * 3600,
            httponly=False,
            secure=bool(app.config.get("SESSION_COOKIE_SECURE", False)),
            samesite="Lax",
            path="/",
        )
        return response

    # ------------------------------------------------------------------------
    # 改密**不在本文件**（这是一处刻意的删除，理由必须写下来）
    #
    # 它曾经是这里的一条固定路由 `POST /api/v1/auth/password`，本次落盘时被删掉，
    # 改成 `domains/access/capabilities.py` 声明的能力 `auth.password.change`
    # （`POST /api/v1/auth/password/change`）。三个问题都不是风格问题：
    #
    #   1. **路径与契约不一致**：registry §1.1 声明的就是 `.../password/change`，
    #      前端按契约拼地址会打到 404。两处描述同一个端点，必删一处。
    #   2. **不受 `human_only` 约束**：手写路由不经过 `runner.invoke`，
    #      于是本模块 docstring 里"身份/会话生命周期不能委托给智能体"那条边界
    #      在它身上**根本没生效**——它靠的是"Agent 不知道这个路径"，不是声明。
    #   3. **无审计**：§1.1 给它标了 `audit=summary`，而手写路由没有审计落点。
    #
    # 为什么改密可以是能力、而登录 / 登出 / `me` 不能：后三者发生在"**有身份**"
    # 之前，而能力执行器的第一步就要求 `actor.user_id`。改密发生在一个已认证的
    # 会话里，因此它是一条普通能力——由 `Capability` 派生路由、权限、审计与
    # Agent 暴露策略，与其余 69 条走同一条链路。

