"""数据范围解析：把用户解析成可执行的 SQL 谓词。

**这是早期版本头号缺陷的修复点。**

早期版本（`早期版本 common/security/data_scope.py`）的 `scope_predicate()` 在用户只有 `farm`
型范围时走到 `return "1=0", []` —— 因为 `data_scopes` 表只有 `area_id` 列，没有
`farm_id`，于是"全场范围"这个意图无法表达，退化成了"什么都看不到"。

    * 用户看到 0 行数据**却不报错**；
    * 静默返回空集比报错危险得多，因为它伪装成了"确实没数据"；
    * 后果：一个配置正确的账号会以为自己没有数据，而不是去修配置。

新系统的做法分两层：

    第一层（数据库）：`chk_data_scopes_binding` CHECK 约束保证
        `scope_type='farm'` 的行必须有非空 `farm_id`。
        **配置错了根本插不进去**——这是最可靠的一层。

    第二层（应用）：`ScopeEntry.from_row()` 二次校验，缺分租键时抛
        `DATA_SCOPE_UNRESOLVED`（403），**不返回空集**。

另外 `super_admin` 走 `allow_all`：这是显式的角色判断，不是"因为没有范围记录所以放行"。
早期版本的 `unrestricted()` 有一处逻辑是"非强制校验时放行"，那是给测试用的旁路——
新系统不提供任何旁路，测试可以注入替身，但生产代码里没有"因为没配所以全开"这条路径。
"""

from __future__ import annotations

from typing import Any

from yuxin.kernel.errors import DomainError, ErrorCode, scope_unresolved
from yuxin.kernel.scope import Scope, ScopeEntry


class DataScopeResolver:
    """实现 `kernel/runner.py::ScopeResolver` 协议。"""

    def __init__(self, uow_factory: Any) -> None:
        self._uow = uow_factory

    def resolve(self, *, user_id: int, role_codes: frozenset[str]) -> Scope:
        """解析用户的数据范围。

        解析失败抛 `DATA_SCOPE_UNRESOLVED`（403）——这是 fail-closed 的核心。
        """
        # 超级管理员：显式的角色判断，不是"没有记录所以放行"。
        #
        # 为什么让超管绕过范围：它要能管理全部数据，否则"审核新账号"这类操作
        # 会因为范围限制而看不到待审核列表。这是业务需要的例外，写在这里而不是
        # 藏在 SQL 的某个分支里。
        if "super_admin" in role_codes:
            return Scope.all_data(user_id=user_id)

        with self._uow().begin() as tx:
            rows = tx.query_all(
                """
                SELECT ds.code, ds.scope_type, ds.farm_id, ds.area_id, ds.pond_id
                FROM user_data_scopes uds
                JOIN data_scopes ds ON ds.id = uds.scope_id
                WHERE uds.user_id = %s AND ds.status = 'active'
                """,
                (user_id,),
            )

        if not rows:
            raise DomainError(
                ErrorCode.DATA_SCOPE_UNRESOLVED,
                "当前账号没有分配任何数据范围，无法访问业务数据。请联系管理员在「用户管理」中分配。",
                data={"user_id": user_id},
            )

        # `Scope.from_rows` 会逐条调 `ScopeEntry.from_row`，
        # 后者在缺分租键时抛 DATA_SCOPE_UNRESOLVED。**这里是"不返回空集"的实现点。**
        try:
            return Scope.from_rows(rows, user_id=user_id, allow_all=False)
        except DomainError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise scope_unresolved(str(exc)) from exc

    def describe(self, *, user_id: int, role_codes: frozenset[str]) -> dict[str, Any]:
        """给前端展示"我的数据范围是什么"。

        这个接口的价值：用户能**看见**自己的范围。早期版本里范围是不可见的配置，
        出问题时用户只会说"我看不到数据"，运维只能去查库。
        """
        try:
            scope = self.resolve(user_id=user_id, role_codes=role_codes)
        except DomainError as error:
            return {"resolved": False, "reason": error.message, "entries": []}

        if scope.allow_all:
            return {"resolved": True, "scope": "all", "entries": []}

        return {
            "resolved": True,
            "scope": "restricted",
            "entries": [
                {"type": str(entry.scope_type), "bound_id": entry.bound_id}
                for entry in scope.entries
            ],
        }

    def can_read_scope_config(self, *, user_id: int, role_codes: frozenset[str]) -> bool:
        """诊断用：范围是否可解析。**不抛错**。"""
        try:
            self.resolve(user_id=user_id, role_codes=role_codes)
        except DomainError:
            return False
        return True


__all__ = ["DataScopeResolver", "ScopeEntry"]
