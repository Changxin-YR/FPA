"""主数据域：塘口、区域、物料、往来单位。

这是第一个完整的业务域（表 + 服务 + 能力声明 + 端到端测试），
也是其余域的模板。
"""

from __future__ import annotations

from . import ponds, ponds_write, service

__all__ = ["ponds", "ponds_write", "service"]
