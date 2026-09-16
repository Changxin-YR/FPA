"""业务服务层的公共契约。

服务（L3）是**人工入口与 Agent 入口的共同落点**——这是需求里"不要为 Agent 再写一套
业务系统"的落地形态：

    人工:  Vue 页面 → REST 路由 → Service  ┐
                                        ├→ 同一个方法
    Agent: Harness → Gateway    → Service  ┘

三层权限防御的第三层就在这里：服务方法**不信任调用方**，自己再校验一次。

    第一层 Tool Filtering  → 只把有权限的能力注册成 Tool（agent/session.py）
    第二层 Gateway        → 收到 tool call 时再校验（agent/gateway.py）
    第三层 Business Service → 本文件定义的 `ctx` 上的校验（domains/*/service.py）

第三层校验的**独立性**很重要：它检查的是"目标行是否在范围内"，而不是重复第二层的
"权限码是否存在"。两者的失效模式不同——第二层被绕过时（例如未来某个新入口忘了走
Gateway），第三层仍然拦得住，因为它检查的是**具体那一行数据**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from yuxin.kernel.errors import forbidden
from yuxin.kernel.scope import Scope


@dataclass(frozen=True, slots=True)
class Actor:
    """当前操作者。信息全部来自**服务端**解析，不接受客户端声称。

    早期版本的 `X-Agent-Context` 上下文令牌设计是正确的（绑定 session + instruction +
    conversation），这里继承其思路，但把"用户权限必须来自服务器"写成硬约束：

        ✗ 前端传 {"role": "admin"} → Agent 相信它
        ✓ 浏览器 Session → Flask Auth → 查库得真实 User → 加载 RBAC → 加载 DataScope
    """

    user_id: int
    username: str
    permissions: frozenset[str]
    scope: Scope
    role_codes: frozenset[str] = frozenset()
    session_hash: str = ""
    is_agent: bool = False
    conversation_id: str | None = None
    confirmation_id: int | None = None

    def has(self, permission: str | None) -> bool:
        if permission is None:
            return True
        return permission in self.permissions

    def require(self, permission: str | None) -> None:
        if not self.has(permission):
            raise forbidden(required=permission)

    @property
    def is_super_admin(self) -> bool:
        return "super_admin" in self.role_codes


@dataclass(frozen=True, slots=True)
class ServiceContext:
    """一次业务调用携带的可信上下文。"""

    actor: Actor
    request_id: str

    @property
    def user_id(self) -> int:
        return self.actor.user_id

    @property
    def scope(self) -> Scope:
        return self.actor.scope

    @property
    def is_agent(self) -> bool:
        return self.actor.is_agent

    def require(self, permission: str | None) -> None:
        """第三层权限校验的入口。"""
        self.actor.require(permission)


# ---------------------------------------------------------------------------
# 分页：全系统单点提供
#
# 早期版本实测有 25 处重复的分页 SQL、31 处重复的 has_next 计算，且各自的边界处理
# 不一致。这里把边界逻辑固定下来，其余地方只调用 `page_clause()` / `page_result()`。
# ---------------------------------------------------------------------------

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100


@dataclass(frozen=True, slots=True)
class Page:
    number: int = 1
    size: int = PAGE_SIZE_DEFAULT

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("页码必须从 1 开始")
        if not 1 <= self.size <= PAGE_SIZE_MAX:
            raise ValueError(f"每页条数必须在 1~{PAGE_SIZE_MAX} 之间")

    @classmethod
    def parse(cls, number: Any = None, size: Any = None) -> Page:
        from yuxin.kernel.errors import validation

        try:
            parsed_number = 1 if number in (None, "") else int(number)
            parsed_size = PAGE_SIZE_DEFAULT if size in (None, "") else int(size)
        except (TypeError, ValueError) as exc:
            raise validation("分页参数无效") from exc
        if parsed_number < 1:
            raise validation("页码必须从 1 开始", field="page")
        if not 1 <= parsed_size <= PAGE_SIZE_MAX:
            raise validation(f"每页条数必须在 1~{PAGE_SIZE_MAX} 之间", field="page_size")
        return cls(number=parsed_number, size=parsed_size)

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size

    def limit_clause(self) -> str:
        """返回可直接粘进 SQL 的 LIMIT 子句。

        参数用占位符而不是字面量：即使 ``Page`` 已经校验过范围，也不把数字拼进 SQL——
        这样"有没有拼 SQL"这个问题在评审时只需要看有没有 ``%s``。
        """
        return "LIMIT %s OFFSET %s"

    def params(self) -> tuple[int, int]:
        return (self.size, self.offset)

    def to_result(self, items: list[dict[str, Any]], total: int) -> dict[str, Any]:
        return {
            "items": items,
            "page": self.number,
            "page_size": self.size,
            "total": int(total),
            "has_next": self.offset + len(items) < int(total),
        }


__all__ = ["Actor", "Page", "ServiceContext"]
