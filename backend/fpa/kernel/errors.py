"""错误码的唯一注册表。

早期版本的教训（.local/recon-backend.md）：358 个错误码散在 882 处抛出，没有注册表，
客户端契约只能靠人工维护 270 KB 的 openapi.json。前端因此出现"同一个 5xx 在导出路径
和列表路径给用户不同文案"。

新系统规则：
    * 所有业务异常都是 DomainError；
    * code 必须来自 ErrorCode 枚举，未注册的 code 在构造时即拒绝；
    * message 必须是可直接展示给用户的简体中文；
    * 技术细节只进日志，不进 message。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # --- 请求侧 ---
    VALIDATION_ERROR = "VALIDATION_ERROR"
    FIELD_INVALID = "FIELD_INVALID"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"

    # --- 鉴权 / 授权 ---
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    DATA_SCOPE_UNRESOLVED = "DATA_SCOPE_UNRESOLVED"
    DATA_SCOPE_DENIED = "DATA_SCOPE_DENIED"
    CSRF_INVALID = "CSRF_INVALID"
    RATE_LIMITED = "RATE_LIMITED"

    # --- 治理 ---
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    CONFIRMATION_INVALID = "CONFIRMATION_INVALID"
    HUMAN_ONLY = "HUMAN_ONLY"
    VERSION_CONFLICT = "VERSION_CONFLICT"

    # --- Agent ---
    CAPABILITY_NOT_FOUND = "CAPABILITY_NOT_FOUND"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    AGENT_CONTEXT_INVALID = "AGENT_CONTEXT_INVALID"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_PROTOCOL_ERROR = "AGENT_PROTOCOL_ERROR"

    # --- 兜底 ---
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


#: 每个错误码对应的 HTTP 状态码。集中在一处，避免早期版本那样在每个 error handler
#: 里各写一遍映射。
_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.FIELD_INVALID: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.DATA_SCOPE_UNRESOLVED: 403,
    ErrorCode.DATA_SCOPE_DENIED: 403,
    ErrorCode.CSRF_INVALID: 403,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.IDEMPOTENCY_IN_PROGRESS: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.CONFIRMATION_INVALID: 409,
    ErrorCode.HUMAN_ONLY: 409,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.CAPABILITY_NOT_FOUND: 404,
    ErrorCode.TOOL_NOT_FOUND: 404,
    ErrorCode.AGENT_CONTEXT_INVALID: 401,
    ErrorCode.AGENT_UNAVAILABLE: 503,
    ErrorCode.AGENT_TIMEOUT: 504,
    ErrorCode.AGENT_PROTOCOL_ERROR: 502,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
}


class DomainError(Exception):
    """唯一业务异常类型。

    ``message`` 面向最终用户，必须中文、可直接展示。
    ``data`` 只放结构化补充信息（如 ``{"field": "capacity_mu"}``），不放技术栈信息。
    """

    __slots__ = ("code", "message", "status", "data")

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(code, ErrorCode):
            raise TypeError(
                f"错误码必须是 ErrorCode 枚举成员，收到 {code!r}。"
                "新增错误码请先加进 ErrorCode 与本文件的 _HTTP_STATUS。"
            )
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status if status is not None else _HTTP_STATUS[code]
        self.data = data

    def to_payload(self, request_id: str) -> dict[str, Any]:
        return {
            "code": str(self.code),
            "message": self.message,
            "data": self.data,
            "request_id": request_id,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"DomainError({self.code!s}, {self.message!r}, status={self.status})"


# ---------------------------------------------------------------------------
# 常用构造器：把"文案 + 码 + 状态码"的组合固定下来，避免同一场景在不同模块
# 写出不同措辞（早期版本 14 处独立 draft:'草稿' 就是这么来的）。
# ---------------------------------------------------------------------------


def unauthenticated(message: str = "登录状态已失效，请重新登录") -> DomainError:
    return DomainError(ErrorCode.UNAUTHENTICATED, message)


def forbidden(*, required: str | None = None) -> DomainError:
    data = {"required_permission": required} if required else None
    return DomainError(ErrorCode.FORBIDDEN, "当前账号没有权限执行该操作", data=data)


def not_found(what: str = "目标") -> DomainError:
    return DomainError(ErrorCode.NOT_FOUND, f"{what}不存在或已被删除")


def scope_unresolved(detail: str = "") -> DomainError:
    """数据范围无法解析时**必须**抛这个，绝不允许退化成"返回空集"。

    早期版本 `common/security/data_scope.py` 的 `scope_predicate()` 在用户只有
    `farm` 型范围时返回 `"1=0", []`——用户看到 0 行数据却不报错。静默返回空集比
    报错危险得多，因为它伪装成了"确实没数据"。
    """
    message = "当前账号的数据范围无法解析，已拒绝本次访问"
    if detail:
        message = f"{message}（{detail}）"
    return DomainError(ErrorCode.DATA_SCOPE_UNRESOLVED, message)


def scope_denied(what: str = "目标数据") -> DomainError:
    return DomainError(ErrorCode.DATA_SCOPE_DENIED, f"{what}不在当前账号的数据范围内")


def validation(message: str, *, field: str | None = None) -> DomainError:
    code = ErrorCode.FIELD_INVALID if field else ErrorCode.VALIDATION_ERROR
    return DomainError(code, message, data={"field": field} if field else None)


def version_conflict(*, current: int) -> DomainError:
    return DomainError(
        ErrorCode.VERSION_CONFLICT,
        "该记录已被他人修改，请刷新后重试",
        data={"current_version": current},
    )
