"""Agent 路由：工具清单、工具调用、对话轮次。

三个端点，各自对应一条契约（`docs/INTERFACES.md` §4）：

    GET  /api/v1/agent/tools                  插件启动时拉工具清单（L1 过滤）
    POST /api/v1/agent/tools/{tool_name}/call  插件执行一次工具调用（L2 校验）
    POST /api/v1/agent/turns                   浏览器发起一轮自然语言对话
    POST /api/v1/agent/turns/stream            同上，NDJSON 流式

**上下文令牌**：由后端签发，绑定
``user_id / session_hash / conversation_id / nonce / expiry``。
它**不是**会话 Cookie 的替代品，而是受控 Harness 会话的 bearer 凭据；每次调用仍按
``session_hash`` 回库重验登录状态、RBAC 与 DataScope。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from flask import Flask, Response, current_app, jsonify, request

from fpa.kernel.errors import DomainError, ErrorCode
from fpa.kernel.runner import ActorView

#: 上下文令牌的签名密钥从 app secret 派生。
#:
#: 派生而不是复用 app secret 本身：这样即使令牌签名算法出问题，也不会波及
#: Flask 会话签名；而且轮换 app secret 会**同时**作废全部上下文令牌，
#: 这是合理的（部署方换密钥意味着想清空一切临时授权）。
_CTX_SALT = b"fpa-agent-context-v1"


def _signing_key(secret: str) -> bytes:
    return hashlib.sha256(_CTX_SALT + secret.encode("utf-8")).digest()


def issue_context_token(
    *,
    secret: str,
    user_id: int,
    session_hash: str,
    capability: str,
    conversation_id: str,
    ttl_seconds: int,
) -> str:
    """签发上下文令牌。

    格式 ``base64url(payload).base64url(hmac)``。

    为什么用 HMAC 而不是把状态存库：令牌是会话级、无状态、可自校验的；每次工具
    调用会用其中的 session hash 回库重新鉴权。
    真正需要服务端状态的是**确认令牌**（因为它要防重放），那一个已经在
    `agent_confirmations` 表里了。
    """
    payload = {
        "uid": user_id,
        "sid": session_hash,
        "cap": capability,
        "cid": conversation_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_seconds,
        "nonce": secrets.token_urlsafe(8),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(_signing_key(secret), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(signature)}"


def verify_context_token(secret: str, token: str) -> dict[str, Any]:
    """校验并解出上下文令牌的 payload。任何异常都翻译成 ``AGENT_CONTEXT_INVALID``。"""
    invalid = DomainError(
        ErrorCode.AGENT_CONTEXT_INVALID, "智能助手凭据无效或已过期，请重新发起"
    )
    if not token or "." not in token:
        raise invalid
    raw_part, signature_part = token.split(".", 1)
    try:
        raw = _unb64(raw_part)
        provided = _unb64(signature_part)
    except (ValueError, TypeError) as exc:
        raise invalid from exc

    expected = hmac.new(_signing_key(secret), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(provided, expected):
        raise invalid

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise invalid from exc
    if not isinstance(payload, dict):
        raise invalid
    if int(payload.get("exp") or 0) <= int(time.time()):
        raise invalid
    return payload


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def register_agent_routes(app: Flask) -> None:
    def _gateway() -> Any:
        return current_app.config["FPA_AGENT_GATEWAY"]

    def _actor_from_context_token() -> tuple[Any, dict[str, Any]]:
        """按上下文令牌解析操作者。

        **关键安全点**：这里的用户身份来自令牌签名 + 服务端会话查库，
        **不来自请求体里的任何字段**。模型即使被注入提示词写了
        `{"user_id": 1, "role": "admin"}`，也无法影响解析结果——
        因为根本没有代码去读那些字段。

        这正是需求里"禁止让前端告诉 Agent 用户是什么权限"的落点。
        """
        access = current_app.config["FPA_ACCESS"]
        token = request.headers.get("X-Agent-Context", "")
        payload = verify_context_token(current_app.config["SECRET_KEY"], token)

        claimed_uid = payload.get("uid")
        session_hash = str(payload.get("sid") or "")
        if isinstance(claimed_uid, bool) or not isinstance(claimed_uid, int) or claimed_uid <= 0 or not session_hash:
            raise DomainError(
                ErrorCode.AGENT_CONTEXT_INVALID,
                "智能助手凭据无效或已过期，请重新发起",
            )

        # 用令牌 payload 里的 sid（会话哈希）**直接查库**解析用户。
        #
        # 这里**不能**读 Cookie：Agent 请求走的是 `X-Agent-Context`，
        # 浏览器会话 Cookie 根本不存在（契约 §5 明确这是两套凭据）。
        # 初版复用了 `_current_actor()`（读 Cookie），于是每个 Agent 请求都 401。
        #
        # 每次调用都重新查库，所以权限是**实时**的：
        # 管理员撤销权限、用户登出，下一次调用立刻生效。
        user = access.resolve_by_session_hash(session_hash)
        if int(user.user_id) != claimed_uid:
            raise DomainError(
                ErrorCode.AGENT_CONTEXT_INVALID,
                "智能助手凭据无效或已过期，请重新发起",
            )

        return (
            ActorView(
                user_id=user.user_id,
                username=user.username,
                permissions=user.permissions,
                role_codes=user.role_codes,
                session_hash=user.session_hash,
                is_agent=True,
                conversation_id=str(payload.get("cid") or "") or None,
            ),
            payload,
        )

    # -- 工具清单 -------------------------------------------------------------

    @app.get("/api/v1/agent/tools")
    def agent_tools() -> Response:
        """Harness 插件启动/刷新时拉取的清单。

        L1 过滤的服务端形态：**只返回该用户有权调用的工具**。
        所以"模型看不到的"和"用户本来就不能做的"是同一件事，不靠提示词约定。
        """
        actor, _payload = _actor_from_context_token()
        gateway = _gateway()
        return jsonify(
            {
                "code": "OK",
                "message": "",
                "data": gateway.tools_payload(actor.permissions),
                "request_id": current_app.config.get("FPA_REQUEST_ID", ""),
            }
        )

    # -- 工具调用 -------------------------------------------------------------

    @app.post("/api/v1/agent/tools/<tool_name>/call")
    def agent_tool_call(tool_name: str) -> Response:
        """插件执行一次工具调用。L2 在这里校验。

        三层防御的**第二层**：即使模型构造了一个它不该有的工具名，
        这里也会重新校验权限与范围。第三层在业务服务里。
        """
        from fpa.web.context import current_request_id

        actor, _payload = _actor_from_context_token()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "请求内容必须是 JSON 对象")
        arguments = body.get("arguments")
        if not isinstance(arguments, dict):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "arguments 必须是对象")

        outcome = _gateway().call_tool(
            tool_name=tool_name,
            arguments=arguments,
            actor=actor,
            request_id=current_request_id(),
            idempotency_key=request.headers.get("Idempotency-Key"),
            confirmation_token=body.get("confirmation_token"),
        )
        # ★ 把 `kind` **原样**交给模型。这是 WRITE_CONTRACT 规则 1 的实现点：
        #   插件与模型都不能改写它，因此"待确认"不可能被渲染成"已完成"。
        return jsonify(
            {
                "code": "OK",
                "message": "",
                "data": outcome.to_meta(),
                "request_id": current_request_id(),
            }
        )

    # -- 对话轮次 -------------------------------------------------------------

    @app.post("/api/v1/agent/turns")
    def agent_turn() -> Response:
        """浏览器发起一轮对话。

        本端点**不直接执行业务**：它只把消息交给 Harness，由模型决定调用哪些工具。
        工具调用走 `/agent/tools/<name>/call`，因此权限校验、确认闸门、幂等、
        审计全部复用同一条链路——这是"人工入口与 Agent 入口共用同一套业务服务"的形态。
        """
        from fpa.web.context import current_request_id

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "请求内容必须是 JSON 对象")
        message = str(body.get("message") or "").strip()
        if not message:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "请输入要执行的指令")
        if len(message) > 4000:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "指令长度不能超过 4000 个字符")

        handler = current_app.config.get("FPA_AGENT_TURN")
        if handler is None:
            raise DomainError(
                ErrorCode.AGENT_UNAVAILABLE, "智能助手未启用，请联系管理员"
            )

        from fpa.web.app import _current_actor

        actor = _current_actor()
        result = handler(
            actor=actor,
            message=message,
            conversation_id=str(body.get("conversation_id") or "") or secrets.token_hex(8),
            page_context=str(body.get("page_context") or ""),
            request_id=current_request_id(),
        )
        return jsonify(
            {
                "code": "OK",
                "message": "",
                "data": result,
                "request_id": current_request_id(),
            }
        )

    # -- 流式对话轮次 ---------------------------------------------------------

    @app.post("/api/v1/agent/turns/stream")
    def agent_turn_stream() -> Response:
        """与 `/agent/turns` 同语义，但以 **NDJSON** 逐行交付（INTERFACES.md §3）。

        ## 这条路由为什么必需（502 的根因，不是优化）

        `AgentPanel.vue` **本来就想走流式**：它先试这个端点，只有 404/405 才回退非流式。
        在本路由存在之前那个回退是必然发生的 —— 于是所有长任务都走"一次性响应"：
        一句需要工具的指令要跑 **5 分钟以上**，这段时间响应**一个字节都不发**，
        Vite 的 `http-proxy` 默认 **120 秒**空闲就掐连接。负责人 实测到的
        「第 1 次 `ConnectionResetError` / 第 2 次 502 `AGENT_PROTOCOL_ERROR`」
        就是这条链，**而库里其实已经写成功了**。

        行形状（前端 `service.ts::AgentStreamLine` 逐字依赖）：
        `{"type":"status"}` / `{"type":"delta"}` / `{"type":"result","result":{…}}`。
        **写操作只能由 `result` 行交付** —— `status`/`delta` 只更新显示。

        ## 两条实现约束

        * 生成器与**非流式 handler 共用**同一套令牌签发、提示词拼装与结果整形
          （`fpa.web.agent_turn` 里那一份），这里不做第二份实现；
        * 异常**不**在这里改写成 HTTP 状态码：响应已经开始流式后改状态码是不可靠的。
          模型侧错误会在生成器里冒泡、由 Flask 记录，前端则因缺 `result` 行得到
          `AGENT_PROTOCOL_ERROR`（契约里写明的判定），而不是静默空白。
        """
        from fpa.web.app import _current_actor

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "请求内容必须是 JSON 对象")
        message = str(body.get("message") or "").strip()
        if not message:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "请输入要执行的指令")
        if len(message) > 4000:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "指令长度不能超过 4000 个字符")

        stream_factory = current_app.config.get("FPA_AGENT_TURN_STREAM")
        if stream_factory is None:
            raise DomainError(
                ErrorCode.AGENT_UNAVAILABLE, "智能助手未启用，请联系管理员"
            )

        actor = _current_actor()
        conversation_id = str(body.get("conversation_id") or "") or secrets.token_hex(8)
        lines = stream_factory(
            actor=actor,
            message=message,
            conversation_id=conversation_id,
            page_context=str(body.get("page_context") or ""),
        )
        return Response(lines, mimetype="application/x-ndjson")

    # -- 确认与取消 -----------------------------------------------------------

    @app.post("/api/v1/agent/confirmations/<int:confirmation_id>/confirm")
    def agent_confirm(confirmation_id: int) -> Response:
        """确认并执行一个待确认的高危操作。

        契约要点（见 `WRITE_CONTRACT.md`）：确认**之后**才写。这个端点返回的
        `kind` 一定是 `executed` 或 `failed`，不可能是 `confirmation_required`——
        用户已经确认过了，再返回一次待确认就是死循环。
        """
        from fpa.web.context import current_request_id

        body = request.get_json(silent=True) or {}
        token = str(body.get("token") or "").strip()
        if not token:
            raise DomainError(ErrorCode.CONFIRMATION_INVALID, "缺少确认令牌")

        from fpa.web.app import _current_actor

        actor = _current_actor()
        gateway = _gateway()
        result = gateway.confirm_pending(
            confirmation_id=confirmation_id,
            token=token,
            actor=actor,
            request_id=current_request_id(),
        )
        return jsonify(
            {
                "code": "OK",
                "message": "",
                "data": {
                    **result.to_meta(),
                    **(
                        {"conversation_id": result.conversation_id or actor.conversation_id or ""}
                        if result.kind == "executed"
                        else {}
                    ),
                },
                "request_id": current_request_id(),
            }
        )

    @app.post("/api/v1/agent/confirmations/<int:confirmation_id>/cancel")
    def agent_cancel(confirmation_id: int) -> Response:
        from fpa.web.context import current_request_id
        from fpa.web.app import _current_actor

        actor = _current_actor()
        _gateway().cancel_pending(
            confirmation_id=confirmation_id, actor=actor, request_id=current_request_id()
        )
        return jsonify(
            {
                "code": "OK",
                "message": "已取消该操作",
                "data": {"kind": "cancelled"},
                "request_id": current_request_id(),
            }
        )


__all__ = ["issue_context_token", "register_agent_routes", "verify_context_token"]
