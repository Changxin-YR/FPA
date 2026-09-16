"""抽查权限码的中文名是否贴切（验证"优先取同名能力"的取名规则生效）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from yuxin.kernel.uow import UnitOfWork  # noqa: E402
from yuxin.kernel.uow_factory import connection_config  # noqa: E402

SAMPLES = (
    "batch.view", "batch.update", "batch.verify",
    "cost.view", "cost.manage", "cost.confirm", "cost.close",
    "sales.deliver", "sales.verify", "sales.create",
    "warehouse.receipt.verify", "finance.payment.manage",
    "pond.verify", "feeding.create",
)


def main() -> int:
    with UnitOfWork(connection_config()).begin() as tx:
        for code in SAMPLES:
            row = tx.query_one("SELECT name, domain FROM permissions WHERE code=%s", (code,))
            name = row["name"] if row else "（缺）"
            domain = row["domain"] if row else ""
            print(f"  {code:28} {domain:12} -> {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
