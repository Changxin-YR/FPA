"""数据范围（DataScope）：一条数据属于谁。

早期版本的教训（.local/recon-backend.md，高危项）：
    * 6+ 套并行实现、口径不一致，横跨生产/仓储/主数据/数据交换/采购/销售/退货七个域；
    * `common/security/data_scope.py` 里 `farm` 型范围因为 `data_scopes` 表没有 `farm_id`
      字段，`scope_predicate()` 走到 `return "1=0", []` —— **用户看到 0 行数据却不报错**；
    * 校验是"谁记得调 `require_scope` 谁安全"，仓储方法签名里没有 scope，无法强制。

新系统规则：
    1. `Scope` 只能由 `AccessService` 从数据库解析产生，不能凭空构造；
    2. 解析不出可执行谓词时**抛 {@link scope_unresolved}**，绝不退化成"返回空集"；
    3. 仓储的 list/get 方法签名强制收 `Scope`，忘记传是类型错误。

谓词渲染只碰 `fragment` 与 `params`，表别名走白名单字符集校验——这是本项目里
唯一允许把标识符拼进 SQL 的地方，因此单独收紧。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from fpa.kernel.errors import scope_denied, scope_unresolved


class ScopeType(StrEnum):
    """一条数据范围记录限定到哪一级。"""

    FARM = "farm"
    AREA = "area"
    POND = "pond"
    PERSONAL = "personal"


#: 每种范围类型对应的"分租键"列名。
#:
#: 早期版本正是因为 `farm` 型没有对应的分租键列（`data_scopes.farm_id` 不存在）而静默
#: 退化成 `1=0`。这里把它写成显式映射，缺失即报错。
SCOPE_COLUMN: dict[ScopeType, str] = {
    ScopeType.FARM: "farm_id",
    ScopeType.AREA: "area_id",
    ScopeType.POND: "pond_id",
    ScopeType.PERSONAL: "created_by",
}


@dataclass(frozen=True, slots=True)
class ScopeEntry:
    """一条已解析的数据范围记录。"""

    scope_type: ScopeType
    bound_id: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        """从 `data_scopes` 表的一行构造。

        缺失分租键时立刻抛错——这是与早期版本的关键差别。早期版本此时会静默返回空集。
        """
        raw_type = row.get("scope_type")
        try:
            scope_type = ScopeType(str(raw_type))
        except ValueError as exc:
            raise scope_unresolved(f"未知的数据范围类型 {raw_type!r}") from exc

        column = SCOPE_COLUMN[scope_type]
        bound_id = row.get(column)
        if bound_id is None:
            raise scope_unresolved(
                f"数据范围 {row.get('code') or scope_type} 缺少分租键 {column}；"
                "请先补齐 data_scopes 表的该列，否则该账号会看到空数据"
            )
        try:
            return cls(scope_type=scope_type, bound_id=int(bound_id))
        except (TypeError, ValueError) as exc:
            raise scope_unresolved(f"分租键 {column} 不是整数：{bound_id!r}") from exc


@dataclass(frozen=True, slots=True)
class ScopePredicate:
    """渲染后的 SQL 片段与其参数。

    刻意把 `fragment` 和 `params` 绑在一起返回：调用方想漏掉参数就写不出代码。
    """

    fragment: str
    params: Sequence[Any]


class Scope:
    """一个用户的已解析数据范围。

    ``allow_all`` 为 True 时代表全场/超级管理员，不加任何限制。
    否则 ``entries`` 必须非空——空 entries + 非 allow_all 是"解析失败"，抛错。
    """

    __slots__ = ("_allow_all", "_entries", "_user_id")

    def __init__(
        self,
        *,
        allow_all: bool,
        entries: Iterable[ScopeEntry] = (),
        user_id: int,
    ) -> None:
        self._allow_all = allow_all
        self._entries = tuple(entries)
        self._user_id = int(user_id)
        if not allow_all and not self._entries:
            # 不变量：非无限范围必须至少有一条可执行记录。
            raise scope_unresolved("账号没有可用数据范围记录")
        for entry in self._entries:
            if entry.scope_type is ScopeType.PERSONAL and entry.bound_id != self._user_id:
                raise scope_unresolved("个人数据范围的归属与当前账号不一致")

    # -- 构造 -----------------------------------------------------------------

    @classmethod
    def all_data(cls, *, user_id: int) -> Self:
        return cls(allow_all=True, entries=(), user_id=user_id)

    @classmethod
    def from_rows(cls, rows: Sequence[dict[str, Any]], *, user_id: int, allow_all: bool = False) -> Self:
        return cls(
            allow_all=allow_all,
            entries=[ScopeEntry.from_row(row) for row in rows],
            user_id=user_id,
        )

    # -- 只读属性 -------------------------------------------------------------

    @property
    def allow_all(self) -> bool:
        return self._allow_all

    @property
    def entries(self) -> tuple[ScopeEntry, ...]:
        return self._entries

    @property
    def user_id(self) -> int:
        return self._user_id

    # -- 谓词渲染 -------------------------------------------------------------

    def predicate(self, alias: str = "") -> ScopePredicate:
        """渲染成 ``<alias>.<col> IN (%s, %s) OR ...``。

        ``allow_all`` 返回恒真片段 ``1=1``（调用方可直接 AND 进 WHERE，无需特判）。
        """
        prefix = _safe_alias(alias)
        if self._allow_all:
            return ScopePredicate(fragment="1=1", params=())

        groups: dict[str, list[int]] = {}
        for entry in self._entries:
            column = SCOPE_COLUMN[entry.scope_type]
            groups.setdefault(column, []).append(entry.bound_id)

        fragments: list[str] = []
        params: list[Any] = []
        for column, ids in groups.items():
            unique_ids = sorted(set(ids))
            placeholders = ",".join(["%s"] * len(unique_ids))
            fragments.append(f"{prefix}{column} IN ({placeholders})")
            params.extend(unique_ids)
        return ScopePredicate(fragment=" OR ".join(fragments), params=params)

    def where_clause(self, alias: str = "") -> tuple[str, list[Any]]:
        """便捷形态：直接返回 ``(fragment, params)`` 二元组。"""
        rendered = self.predicate(alias)
        return rendered.fragment, list(rendered.params)

    # -- 单行判定 -------------------------------------------------------------

    def allows_row(self, row: dict[str, Any]) -> bool:
        """判断一行数据是否在范围内。

        用于"先按主键取回来再判定"的场景（例如编辑单条记录）。缺失分租键的行走
        fail-closed：判定为不可见，而不是抛错——因为这里面对的是业务数据行，
        而不是配置行。
        """
        if self._allow_all:
            return True
        for entry in self._entries:
            column = SCOPE_COLUMN[entry.scope_type]
            value = row.get(column)
            if value is None:
                continue
            try:
                if int(value) == entry.bound_id:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def assert_allows_row(self, row: dict[str, Any], *, what: str = "目标数据") -> None:
        if not self.allows_row(row):
            raise scope_denied(what)


#: 表别名只允许这些字符。别名全部来自代码字面量，不接受用户输入；这个校验是
#: "唯一把标识符拼进 SQL 的地方"的护栏，防止未来有人把用户输入接进来。
_ALIAS_PATTERN = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,31}\z")


def scope_predicate_for_columns(
    scope: Scope,
    columns: tuple[str, ...],
    alias: str = "",
    *,
    farm_column: str = "",
) -> tuple[str, list[Any]]:
    """按**每条范围记录自己的分租列**渲染谓词；`columns` 里没有的列不参与。

    返回 `("1=1", [])`（全场）、`("col IN (%s) OR col2 IN (%s)", [...])`，
    或 `("1=0", [])`（这几列上一条范围记录都没有）。

    ## 它与 `Scope.predicate()` / `ScopePolicy.render()` 的分工（三个都不是多余的）

    | 接口 | 判据 | 什么时候用 |
    |---|---|---|
    | `Scope.predicate(alias)` | 按账号持有的**所有**范围类型渲染 | 表上有全部四类分租列时（`ponds` / `batches` 这类） |
    | `ScopePolicy.render(scope, alias)` | 按**能力声明的策略列**渲染 | 能力声明了 `resource(col)`、且该列一定存在时 |
    | 本函数 | 按**本表真实存在的列**渲染 | 表上缺某一类分租列时 |

    ## 为什么不能一把梭用前两个

    * `Scope.predicate()` 对 `areas` 这张表会拼出 `r.area_id IN (...)` ——
      而 `areas` **没有 `area_id` 列**（它自己就是区域层级），MySQL 报
      `Unknown column 'r.area_id'`。同族：只持有 farm 型范围的账号查 `warehouses`
      会拼出 `w.pond_id` → `Unknown column`。
    * `ScopePolicy.render()` 对"账号持有 farm 型范围、策略列是 area_id"这个**正常组合**
      会抛 `DATA_SCOPE_UNRESOLVED`（把正常配置当成配置错误）。

    所以判据只能是"本表的这一列上，这个账号有没有对应的范围记录"。
    `"1=0"` 是**正常结果**而不是错误：只持有区域型范围的账号在区域列表上确实看不到
    东西（这与 `area.get` 对它 403 一致），它是有意的 fail-closed 谓词，
    而不是早期版本那种静默返回空集（`common/security/data_scope.py:58`）。

    ## 为什么它必须是一个**函数**而不是各域各写一份

    原先只有 `master_data.resources_read.DictionaryService._scope_predicate` 一份实现，
    而 `warehouse.inventory.list_warehouses` 走的是 `Scope.predicate()`（见上表第二行）。
    两个域的表结构不同、判据却是同一条，两处实现必然分叉：
    只持有 `personal` / `pond` 型范围的账号查仓库会拼出 `w.created_by` / `w.pond_id`
    这样的不存在列 → MySQL 报错（HTTP 500），而查区域列表不会 ——
    **同一条数据权限，两个域给出不同结论**。这与 `DEVELOPMENT.md` §6.1
    "三层权限防御的判定函数是同一个"是同一条纪律。
    """
    if scope.allow_all:
        return "1=1", []
    prefix = f"{alias}." if alias else ""
    fragments: list[str] = []
    params: list[Any] = []
    for entry in scope.entries:
        column = farm_column if entry.scope_type is ScopeType.FARM and farm_column else SCOPE_COLUMN[entry.scope_type]
        if column not in columns:
            continue
        fragment = f"{prefix}{column}"
        # 同列只渲染一次：同一列的两条范围记录各写一次 `IN (%s)` 会让
        # "参数个数"与"列数"脱钩（绑定值相同，语义不变），排查时极易读错。
        if fragment in fragments:
            continue
        fragments.append(fragment)
        params.append(entry.bound_id)
    if not fragments:
        return "1=0", []
    # 每条片段形如 `alias.col`，统一补 `IN (%s)`；不同列之间用 OR 合并。
    return " OR ".join(f"{frag} IN (%s)" for frag in fragments), params


def _safe_alias(alias: str) -> str:
    if not alias:
        return ""
    if not _ALIAS_PATTERN.match(alias):
        raise ValueError(f"非法表别名：{alias!r}")
    return f"{alias}."


# ---------------------------------------------------------------------------
# 声明形态：能力声明里写 ScopePolicy.xxx()，而不是在服务里手写 require_scope()
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScopePolicy:
    """一个能力如何受数据范围约束。

    ``column`` 为 None 表示该能力不受数据范围约束（例如"列出我有权访问的塘口"——
    它本身就按 scope 过滤，不再额外加一层）。
    """

    column: str | None
    owner_column: str | None = None

    @classmethod
    def none(cls) -> Self:
        return cls(column=None)

    @classmethod
    def resource(cls, column: str) -> Self:
        """按资源表的某一列受控，例如 ``resource("area_id")``。"""
        return cls(column=column)

    @classmethod
    def owner(cls, column: str = "created_by") -> Self:
        """按"仅本人数据"受控。"""
        return cls(column=None, owner_column=column)

    @property
    def constrained(self) -> bool:
        return self.column is not None or self.owner_column is not None

    def render(self, scope: Scope, alias: str = "") -> ScopePredicate:
        """把策略 + 具体 Scope 渲染成 SQL 片段。"""
        if alias:
            _safe_alias(alias)  # 提前校验

        if not self.constrained:
            return ScopePredicate(fragment="1=1", params=())

        prefix = _safe_alias(alias)

        if scope.allow_all:
            return ScopePredicate(fragment="1=1", params=())

        predicates: list[ScopePredicate] = []
        if self.column is not None:
            predicates.append(_render_column(scope, self.column, prefix))
        if self.owner_column is not None:
            predicates.append(
                ScopePredicate(fragment=f"{prefix}{self.owner_column}=%s", params=(scope.user_id,))
            )

        active = [item for item in predicates if item.fragment and item.fragment != "1=0"]
        if not active:
            # 非无限范围的账号走到了这里，说明解析阶段就应当报错。
            raise scope_unresolved("能力声明的数据范围策略无法为本账号渲染出谓词")
        if len(active) == 1:
            return active[0]
        fragment = "(" + ") OR (".join(item.fragment for item in active) + ")"
        params: list[Any] = [value for item in active for value in item.params]
        return ScopePredicate(fragment=fragment, params=params)


def _render_column(scope: Scope, column: str, prefix: str) -> ScopePredicate:
    """按 ``scope`` 中与 ``column`` 对应的范围记录渲染 ``IN`` 片段。

    关键行为：如果该账号**没有**任何映射到这一列的范围记录，返回 ``1=0``。
    但这种情况已经不可能静默发生，因为 `ScopeEntry.from_row` 在解析期就保证了
    每条记录都有分租键；剩下的 `1=0` 只在"策略要求的列与账号范围类型不匹配"
    时出现，而那种情况由调用方通过 {@link ScopePolicy} 声明保证不会发生。
    为防御起见，这里仍返回 `1=0`（deny），并且 `render()` 会在 active 为空时报错。
    """
    mapping = {column: scope_type for scope_type, column in SCOPE_COLUMN.items()}
    scope_type = mapping.get(column)
    if scope_type is None:
        raise scope_unresolved(f"未知的分租键列：{column}")

    ids = sorted({entry.bound_id for entry in scope.entries if entry.scope_type is scope_type})
    if not ids:
        return ScopePredicate(fragment="1=0", params=())

    placeholders = ",".join(["%s"] * len(ids))
    return ScopePredicate(fragment=f"{prefix}{column} IN ({placeholders})", params=tuple(ids))


#: 便于测试与文档引用的类型别名。
ScopeRowFilter = Callable[[dict[str, Any]], bool]
