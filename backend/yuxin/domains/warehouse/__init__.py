"""仓储域。

模块职责（照 `domains/master_data/` 的模板分层）：

    service.py       状态机 + 资源声明（文案与状态的唯一来源）
    inventory.py     读路径：仓库列表 / 库存汇总 / 库存流水
    ledger.py        **跨域唯一入口** `apply_movement`（四个域共用，先于本域能力定稿）
    receipt_write.py 写路径：receipt.create/verify、issue.create/verify
    capabilities.py  7 条能力的声明（派生出路由 / 权限码 / DataScope / 工具 schema）

`ledger.py` 独立于本域能力**单独存在**，因为它是跨域契约的一部分：
`feeding.verify`（production）直接 import 它，而不是走能力入口——
服务到服务是契约（`ROLLOUT_CONTRACT.md` §2），HTTP 不是。
"""

from . import capabilities as _capabilities  # noqa: F401  （导入即注册能力）

__all__ = []
