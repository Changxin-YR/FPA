"""采购域：采购单 / 应付 / 付款。

照 master_data 与 cost 的同一结构：表（007 迁移）+ 服务 + 能力声明 + 端到端测试。
"""

from __future__ import annotations

from . import orders, orders_write, payments, payments_write, service

__all__ = ["orders", "orders_write", "payments", "payments_write", "service"]
