"""列出各迁移里状态列的写法，确认 004 该照哪种惯例改。

负责人 的裁决是"记录生命周期状态必须 VARCHAR(32)"。改之前先看清别的域
**已经落盘**的写法（production / warehouse / purchase / sales），
避免我这边改出一个与全队不一致的形态。
"""

from __future__ import annotations

import pathlib
import re

MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / "database" / "migrations"


def main() -> int:
    for name in ("003_master_data.sql", "004_cost.sql", "005_production.sql",
                 "006_warehouse.sql", "007_purchase.sql", "008_sales.sql"):
        path = MIGRATIONS / name
        if not path.exists():
            print(f"{name}: （不存在）")
            continue
        text = path.read_text(encoding="utf-8")
        enums = re.findall(r"^\s{2}(\w+)\s+ENUM\(", text, re.M)
        varchar32 = re.findall(r"^\s{2}(\w+)\s+VARCHAR\(32\)", text, re.M)
        checks_status = bool(re.search(r"CHECK\s*\([^)]*\bstatus\b[^)]*IN\s*\(", text, re.I))
        print(f"=== {name} ===")
        print(f"  仍是 ENUM 的列    : {enums if enums else '（无）'}")
        print(f"  VARCHAR(32) 的列  : {varchar32 if varchar32 else '（无）'}")
        print(f"  用 CHECK 限定状态取值: {checks_status}")
        print()

    print("=== 判据 ===")
    print("  若别的域用 VARCHAR(32) 且**不加** CHECK 限定取值 -> 我照做，保持一致；")
    print("  若它们加了 CHECK -> 我也加，避免少一层保护。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
