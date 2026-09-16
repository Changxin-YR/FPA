"""HTTP 适配层。

职责严格限定为"把 HTTP 请求翻译成内核调用，把内核结果翻译成 HTTP 响应"：

    * 由 `Capability` 声明**自动生成**路由（不手写每条端点）
    * 全局 CSRF 前置校验（早期版本是 34 处手写 `require_csrf()` 散在 12 个文件里）
    * 响应信封与错误码映射单点处理
    * **不含任何业务逻辑**，也不直接写 SQL
"""

from __future__ import annotations

__all__: list[str] = []
