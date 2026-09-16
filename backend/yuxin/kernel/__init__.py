"""渔芯 内核：与 Web 框架无关的业务骨架。

依赖约束（由 tests/test_architecture.py 用 AST 强制，两条）：
    1. kernel 不得 import flask / yuxin.domains（也不得 import web / agent）
    2. MySQL 驱动只允许出现在 `yuxin.kernel.uow` **一处** —— 断言是**集合相等**
       （静态 import pymysql 的模块集合 == {yuxin.kernel.uow}），不是"禁止出现"：
       `UnitOfWork` 就是事务边界，它必须持有连接；把驱动接入与错误码翻译
       收敛到一处，才不会有第二个地方各写一遍 errno 判断

内核只回答四件事：
    1. 出错时用什么码和什么中文文案（errors）
    2. 一条数据属于谁（scope）
    3. 一次业务操作的事务边界在哪（uow）
    4. 一个业务能力长什么样、能派生出什么（capability）
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
