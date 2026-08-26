"""进程级 UnitOfWork 工厂：把"连接怎么来"收敛成**一处**。

## 为什么需要在核心里有这个东西

`UnitOfWork` 的构造需要一个 `ConnectionConfig`，而本项目有**五处**各自从环境变量
拼同一份配置：`tools/runner_e2e.py`、`tools/agent_e2e.py`、`tools/web_e2e.py`、
`tools/master_data_e2e.py`、`tools/cost_e2e.py`（外加 `fpa/settings.py::from_env()`
的 `mysql` 段）。五处描述同一件事，必然有一处先改。

更要紧的是：**业务域不允许自己连库**（`tests/test_architecture.py` 强制"pymysql 只能
出现在 `fpa.kernel.uow`"），但 `Field.dynamic_choices` 这类东西确实需要"读几行数据"
才能给出下拉候选。域不能 import pymysql，于是要么在域里造第二个连接实现（违规），
要么在核心里提供一个**可复用的 uow 工厂** —— 这就是本模块存在的唯一理由。

## 用法

```python
from fpa.kernel.uow_factory import uow_factory

uow = uow_factory()                # 每次调用返回一个新的 UnitOfWork，不共享连接
with uow.begin() as tx:            # 事务边界仍然由调用方显式持有
    rows = tx.query_all("SELECT code, name FROM cost_categories WHERE status='enabled'")
```

刻意**不**做成"进程级单例连接"：`UnitOfWork` 的生命周期是**请求/操作**级的
（见 `kernel/uow.py` 的规则），共享连接会让两个请求串到同一个事务里——那正是早期版本
`get_connection()` 无条件提交的同一类问题。

## 与 `fpa/settings.py` 的关系（尚未收敛，已知）

`fpa/settings.py::from_env()` 也拼了一份 `mysql` 字典（默认值与本模块一致：host
`127.0.0.1`、port `3306`、user `fpa`、database `fpa`）。两者应当合并为
**一处**，但 `settings` 是顶层模块、`kernel` 不能反向依赖它（依赖方向由
`tests/test_architecture.py` 约束）。合并方向应当是 `settings` 读本模块的
`connection_config()`，而不是反过来。**这条尚未落地**，已记入报告供裁决。
"""

from __future__ import annotations

import os
from typing import Any

from fpa.kernel.uow import ConnectionConfig, UnitOfWork

#: 环境变量 → 连接配置的默认值。**默认值只在这里出现一次。**
_DEFAULTS: dict[str, str] = {
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PORT": "3306",
    "MYSQL_USER": "fpa",
    "MYSQL_PASSWORD": "",
    "MYSQL_DATABASE": "fpa",
}


def _env(name: str, values: dict[str, str] | None = None) -> str:
    source = values if values is not None else os.environ
    value = source.get(name)
    if value is None or not str(value).strip():
        return _DEFAULTS[name]
    return str(value).strip()


def connection_config(values: dict[str, str] | None = None) -> ConnectionConfig:
    """从环境变量（或传入的映射）构造 `ConnectionConfig`。

    `values` 参数是为测试准备的：让"环境变量 → 配置"成为可单测的纯函数，而不是
    只能靠 `monkeypatch.setenv` 的隐式全局状态。
    """
    raw_port = _env("MYSQL_PORT", values)
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError(f"MYSQL_PORT 必须是整数，收到 {raw_port!r}") from exc
    return ConnectionConfig(
        host=_env("MYSQL_HOST", values),
        port=port,
        user=_env("MYSQL_USER", values),
        password=_env("MYSQL_PASSWORD", values),
        database=_env("MYSQL_DATABASE", values),
    )


def uow_factory(values: dict[str, str] | None = None) -> UnitOfWork:
    """返回一个新的 `UnitOfWork`；事务边界由调用方用 `with uow.begin() as tx` 持有。"""
    return UnitOfWork(connection_config(values))


def query_rows(sql: str, params: Any = None, *, values: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """开一个短事务、查一批行、立刻提交并关闭。

    给"只读元数据"这类场景用（例如 `Field.dynamic_choices` 的候选值）：调用方不需要
    自己管事务，也不会持有连接。**写操作不要用它** —— 写必须由执行器持有事务边界
    （`docs/WRITE_CONTRACT.md` 规则 1）。
    """
    uow = uow_factory(values)
    with uow.begin() as tx:
        return tx.query_all(sql, params)


__all__ = ["connection_config", "query_rows", "uow_factory"]
