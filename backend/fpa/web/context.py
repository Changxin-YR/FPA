"""请求上下文：request_id、客户端信息。

早期版本在 `app.py` 里用 `app.before_request` 生成 request_id 并塞进 `g`，
本模块把它拆出来，因为还有两处需要它（错误响应、审计快照），
集中一处比三处各算一遍可靠。
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from flask import g, request

#: 客户端传来的 request_id 必须匹配这个模式才会被采纳。
#:
#: 采纳客户端的 id 是为了跨服务追踪（网关 → 业务服务），但不加限制地采纳意味着
#: 客户端可以往日志里注入任意内容（换行、超长字符串）。白名单字符集 + 长度上限
#: 是这类"半可信输入"的标准处理。
_REQUEST_ID_PATTERN = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")


def bind_request_context() -> str:
    """为当前请求生成/采纳 request_id。返回最终值。"""
    candidate = (request.headers.get("X-Request-ID") or "").strip()
    request_id = candidate if _REQUEST_ID_PATTERN.match(candidate) else uuid4().hex
    g.fpa_request_id = request_id
    return request_id


def current_request_id() -> str:
    value = getattr(g, "fpa_request_id", None)
    if isinstance(value, str) and value:
        return value
    return uuid4().hex


def client_ip() -> str:
    """取客户端 IP。

    只看 `request.remote_addr`，**不读 `X-Forwarded-For`**——那个头是客户端可伪造的，
    只有在确实位于受控代理之后、且配置了可信跳数时才该采信。
    本项目若部署在 Nginx 后，应使用 Werkzeug 的 `ProxyFix` 中间件（它会**按配置的
    跳数**重写 `remote_addr`），而不是在这里手写解析逻辑。

    早期版本正是这么做的（`app.py` 里的 `ProxyFix(x_for=trusted_proxy_hops)`）——
    这个设计是对的，继承其思路。
    """
    return str(request.remote_addr or "")[:45]


def user_agent() -> str:
    return str(request.headers.get("User-Agent") or "")[:255]


def request_meta() -> dict[str, Any]:
    """审计需要的请求侧信息快照。"""
    return {
        "request_id": current_request_id(),
        "ip": client_ip(),
        "method": request.method,
        "path": request.path,
        "user_agent": user_agent(),
    }


__all__ = ["bind_request_context", "client_ip", "current_request_id", "request_meta", "user_agent"]
