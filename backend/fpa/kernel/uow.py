"""事务边界：一个请求 = 一个 UnitOfWork = 最多一次 commit。

早期版本的结构性错误（.local/recon-backend.md 高危项 1）：
    `common/db/connection.py:67-91` 的 `get_connection()` 在每个 ``with`` 块退出时
    无条件 ``connection.commit()``，全仓 SAVEPOINT 0 处。后果是
    "核验单据 → 建待办 → 发通知 → 写审计" 这种跨模块操作，每跨一个模块多一个提交点，
    中间失败则前半截已落库且无法回滚。旧 `idempotency.py` 就是三次独立事务
    （预留 / 执行 / 回写），并且会留下永久 ``processing`` 记录。

新系统规则：
    1. 仓储方法第一个参数永远是 ``tx``，仓储自己不开连接；
    2. 一个请求最多一次 commit，异常整体回滚；
    3. 需要嵌套语义时用 SAVEPOINT（``tx.savepoint()``），不用嵌套连接；
    4. 幂等预留、业务写入、版本快照、审计、幂等回写在**同一个 tx** 内。

本模块是本项目唯一允许 import pymysql 的地方（架构约束由 tests 强制）。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, Self, runtime_checkable

import pymysql
from pymysql.connections import Connection

from fpa.kernel.errors import DomainError, ErrorCode


@runtime_checkable
class Cursor(Protocol):
    """仓储只依赖这个最小接口，便于测试替身。"""

    def execute(self, query: str, args: Sequence[Any] | Mapping[str, Any] | None = ...) -> int: ...
    def fetchone(self) -> Any: ...
    def fetchall(self) -> Any: ...
    def executemany(self, query: str, args: Sequence[Any]) -> int: ...


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"
    connect_timeout: int = 5
    read_timeout: int = 15
    write_timeout: int = 15

    def as_kwargs(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "charset": self.charset,
            "cursorclass": pymysql.cursors.DictCursor,
            "autocommit": False,
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "write_timeout": self.write_timeout,
        }


class UnitOfWork:
    """一次业务操作的显式事务边界。

    典型用法::

        with uow.begin() as tx:
            pond = ponds.get(tx, pond_id)
            ponds.update(tx, pond, patch)
            audit.record(tx, action="pond.update", before=before, after=after)
            # 退出时提交；异常时整体回滚

    嵌套用法（需要"局部失败不影响整体"时）::

        with tx.savepoint() as sp:
            ...  # 这里失败只回滚到保存点
    """

    __slots__ = ("_config", "_connection", "_depth", "_savepoint_seq", "_committed", "_begun")

    def __init__(self, config: ConnectionConfig) -> None:
        self._config = config
        self._connection: Connection | None = None
        self._depth = 0
        self._savepoint_seq = 0
        self._committed = False
        self._begun = False

    # -- 生命周期 -------------------------------------------------------------

    @contextmanager
    def begin(self) -> Iterator[Self]:
        """开启事务作用域。最外层退出时提交，异常时回滚并重抛。"""
        is_outermost = self._depth == 0
        if is_outermost:
            if self._begun:
                raise RuntimeError("同一个 UnitOfWork 不能重复 begin；请为每个请求新建实例")
            self._connection = pymysql.connect(**self._config.as_kwargs())
            self._begun = True
        self._depth += 1
        try:
            yield self
        except BaseException:
            if is_outermost:
                self._rollback_quietly()
            raise
        else:
            if is_outermost:
                self._commit()
        finally:
            self._depth -= 1
            if is_outermost:
                self._close_quietly()

    def _commit(self) -> None:
        connection = self._require_connection()
        connection.commit()
        self._committed = True

    def _rollback_quietly(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.rollback()
        except Exception:  # noqa: BLE001 - 回滚失败不能掩盖原始异常
            pass

    def _close_quietly(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._connection = None

    @property
    def committed(self) -> bool:
        return self._committed

    # -- 保存点 ---------------------------------------------------------------

    @contextmanager
    def savepoint(self) -> Iterator[Self]:
        """SAVEPOINT 作用域：内部失败只回滚到保存点，不影响外层事务。

        早期版本此能力为 0 处实现，导致"局部失败"只能靠"整个模块单独开事务"绕开。
        """
        connection = self._require_connection()
        self._savepoint_seq += 1
        name = f"sp_{self._savepoint_seq}"
        with connection.cursor() as cursor:
            cursor.execute(f"SAVEPOINT {name}")
        try:
            yield self
        except BaseException:
            with connection.cursor() as cursor:
                cursor.execute(f"ROLLBACK TO SAVEPOINT {name}")
                cursor.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            with connection.cursor() as cursor:
                cursor.execute(f"RELEASE SAVEPOINT {name}")

    # -- 查询 ----------------------------------------------------------------

    def _require_connection(self) -> Connection:
        if self._connection is None:
            raise RuntimeError("UnitOfWork 尚未 begin()；请用 'with uow.begin() as tx:' 包裹业务操作")
        return self._connection

    @contextmanager
    def cursor(self) -> Iterator[Cursor]:
        connection = self._require_connection()
        with connection.cursor() as cursor:
            yield cursor

    def query_one(self, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        with self.cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
        return dict(row) if row is not None else None

    def query_all(self, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        with self.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall() or []
        return [dict(row) for row in rows]

    def query_scalar(self, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None) -> Any:
        with self.cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return next(iter(row.values()), None)
        return row[0] if isinstance(row, (list, tuple)) else row

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None) -> int:
        with self.cursor() as cursor:
            return int(cursor.execute(sql, params))

    def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        if not rows:
            return 0
        with self.cursor() as cursor:
            return int(cursor.executemany(sql, rows))

    def last_insert_id(self) -> int:
        return int(self._require_connection().insert_id() or 0)

    def affected_rows(self) -> int:
        return int(self._require_connection().affected_rows())


# ---------------------------------------------------------------------------
# MySQL 错误翻译
# ---------------------------------------------------------------------------

#: MySQL 错误码 → 业务错误码。早期版本用**异常消息文本**判定约束类型
#: （`production_store.py:142-146` 的 `if key not in str(exc)`、`app.py:37-42` 正则
#: 抠字段名），脆弱且随 MySQL 版本变化。
_BUSINESS_ERROR_BY_ERRNO: dict[int, tuple[ErrorCode, str]] = {
    1062: (ErrorCode.CONFLICT, "该编码已存在，请换一个"),
    1451: (ErrorCode.CONFLICT, "该记录被其他业务引用，无法删除"),
    1452: (ErrorCode.FIELD_INVALID, "关联对象不存在或已删除"),
    1406: (ErrorCode.FIELD_INVALID, "填写内容超出允许长度"),
    1264: (ErrorCode.FIELD_INVALID, "填写数值超出允许范围"),
    1265: (ErrorCode.FIELD_INVALID, "填写内容格式无效"),
    1292: (ErrorCode.FIELD_INVALID, "日期或数值格式无效"),
}


def translate_mysql_error(error: Exception) -> DomainError | None:
    """把 PyMySQL 异常翻译成 DomainError；无法识别时返回 None（由上层兜底 500）。

    集中在一处，避免早期版本那样在每个 store 里各写一遍 `if errno == ...`。
    """
    args = getattr(error, "args", ())
    errno = args[0] if args else None
    if not isinstance(errno, int):
        return None

    if isinstance(error, pymysql.err.IntegrityError):
        mapped = _BUSINESS_ERROR_BY_ERRNO.get(errno)
        if mapped is not None:
            code, message = mapped
            return DomainError(code, message)
        return DomainError(ErrorCode.CONFLICT, "数据冲突，请刷新后重试")

    if isinstance(error, pymysql.err.DataError):
        mapped = _BUSINESS_ERROR_BY_ERRNO.get(errno)
        if mapped is not None:
            code, message = mapped
            return DomainError(code, message)
        return DomainError(ErrorCode.FIELD_INVALID, "字段值无效或超出允许范围")

    if isinstance(error, pymysql.err.OperationalError):
        if errno in {1205, 1213}:  # 锁等待超时 / 死锁
            return DomainError(ErrorCode.CONFLICT, "数据正在被其他操作处理，请稍后重试")
        return DomainError(ErrorCode.SERVICE_UNAVAILABLE, "数据库服务暂时不可用，请稍后重试")

    return None


@dataclass(slots=True)
class RequestContext:
    """一次请求的可信身份与追踪信息。

    由 Web 中间件构造，**不允许**从请求体里读（早期版本 `X-Agent-Context` 的设计
    是对的，这里继承）。Agent 侧会额外放上下文令牌的 payload。
    """

    request_id: str
    user_id: int | None = None
    username: str = ""
    session_hash: str = ""
    conversation_id: str | None = None
    is_agent: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
