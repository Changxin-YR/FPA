"""会话 Cookie 与 CSRF：全局前置校验。

**这是早期版本一处结构性缺陷的修复点。**

旧做法是 34 处 `require_csrf()` 手写在 12 个文件里，**没有统一拦截器**。
后果不是"某个端点漏了"——那个能测出来——而是**新增写端点时可以合法地忘记加**，
而且代码评审时没人看得出漏了。

新做法：`before_request` 对非安全方法一律校验，豁免必须显式列出（每条附理由）。
"忘记加"从此不可能，只能"显式豁免"，而豁免会出现在 diff 里被人看见。

## 双提交（double-submit）：为什么是三方比对

    浏览器 Cookie `yuxin_csrf`        <- 服务端写入，JS 可读（非 HttpOnly）
    请求头 `X-CSRF-Token`           <- 前端从 Cookie 读出后填
    服务端 `sessions.csrf_token`    <- 数据库里存**原值**

要求**三者一致**：

    * 只比 Cookie 与请求头：攻击者若能设 Cookie（子域接管、XSS）就能同时伪造两者
    * 加上服务端比对：攻击者必须能读到会话记录才能通过，门槛高一个量级

**为什么 CSRF 存原值而会话令牌存哈希**——这是刻意的区别，不是疏漏：

    会话令牌（yuxin_session，HttpOnly）-> 服务端只存 sha256，**不可还原**
        它是真正的凭据；库泄露时攻击者拿不到可直接冒用的值
    CSRF 令牌（yuxin_csrf，非 HttpOnly）-> 服务端存原值
        双提交要求它能与 Cookie 比对；而它本身不是凭据——
        单独拿到它还需要一个有效会话来配合
"""

from __future__ import annotations

import hmac
import secrets

from flask import Response, request

from yuxin.kernel.errors import DomainError, ErrorCode

#: 会话 Cookie 名。HttpOnly——真正的凭据，JS 读不到。
SESSION_COOKIE = "yuxin_session"

#: CSRF Cookie 名。非 HttpOnly——前端必须能读它来填请求头。
CSRF_COOKIE = "yuxin_csrf"

#: CSRF 请求头名。
CSRF_HEADER = "X-CSRF-Token"

#: Cookie 有效期。与会话空闲超时解耦：会话靠服务端过期，Cookie 只是载体。
COOKIE_MAX_AGE = 12 * 3600


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_session_cookies(
    response: Response, *, session_token: str, csrf_token: str, secure: bool
) -> None:
    """登录成功后写两个 Cookie。

    `SameSite=Lax`：允许顶级导航携带（用户点书签/链接进来），阻止跨站 POST。
    这已挡住绝大多数 CSRF，双提交是第二层。

    `Secure` 由配置决定：本地 http 开发必须为 False，否则浏览器根本不存这个 Cookie，
    表现为"登录成功但下一个请求又是 401"——一个极易被误诊为后端 bug 的现象。
    """
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="Lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=COOKIE_MAX_AGE,
        httponly=False,
        secure=secure,
        samesite="Lax",
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def require_csrf() -> None:
    """全局 CSRF 校验。失败抛 403。"""
    submitted = (request.headers.get(CSRF_HEADER) or "").strip()
    from_cookie = (request.cookies.get(CSRF_COOKIE) or "").strip()
    session_token = (request.cookies.get(SESSION_COOKIE) or "").strip()

    if not submitted or not from_cookie or not session_token:
        raise _invalid("缺少安全令牌，请刷新页面后重试")

    # 用 compare_digest 做定长比较：CSRF 令牌不是长期凭据，但不该通过响应时间泄露，
    # 而这个成本是零。
    if not hmac.compare_digest(submitted, from_cookie):
        raise _invalid("安全令牌与页面不一致，请刷新页面后重试")

    from flask import current_app

    access = current_app.config.get("YUXIN_ACCESS")
    if access is None or not access.csrf_matches(session_token, submitted):
        raise _invalid("安全令牌无效，请重新登录")


def _invalid(message: str) -> DomainError:
    return DomainError(ErrorCode.CSRF_INVALID, message)


__all__ = [
    "COOKIE_MAX_AGE",
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "clear_session_cookies",
    "new_csrf_token",
    "require_csrf",
    "set_session_cookies",
]
