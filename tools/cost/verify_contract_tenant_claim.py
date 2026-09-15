"""核实我在契约 §2 里写下的那张"自带分租键的表"清单。

契约里写错一张表名比不写更糟：调用方会照着去取值，然后在运行期才发现某一列不存在。
所以逐表核对 `organization_id` / `farm_id` / `area_id` 三列是否真的都在。

只读迁移文件，不连库（迁移是表结构的权威来源）。
"""

from __future__ import annotations

import pathlib
import re

MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / "database" / "migrations"

#: 我在契约里点名的表
CLAIMED = [
    "feedings",
    "production_batches",
    "inventory_ledger",
    "inventory_lots",
    "warehouses",
    "cost_entries",
]
TENANT = ("organization_id", "farm_id", "area_id")


def columns_of(table: str) -> set[str] | None:
    """在所有迁移里找该表的建表语句，返回列名集合。"""
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        i = text.find(f"CREATE TABLE IF NOT EXISTS {table} (")
        if i < 0:
            continue
        j = text.find("ENGINE=InnoDB", i)
        segment = text[i:j if j > 0 else len(text)]
        cols = set(re.findall(r"^\s{2}(\w+)\s+[A-Z]", segment, re.M))
        return cols
    return None


def main() -> int:
    failures = 0
    print(f"{'表':<22} {'organization_id':<17} {'farm_id':<10} {'area_id':<10} 判定")
    print("-" * 78)
    for table in CLAIMED:
        cols = columns_of(table)
        if cols is None:
            print(f"{table:<22} 建表语句未找到（迁移里没有这张表）")
            failures += 1
            continue
        marks = []
        for key in TENANT:
            ok = key in cols
            marks.append("有" if ok else "**缺**")
            if not ok:
                failures += 1
        verdict = "契约写法正确" if all(m == "有" for m in marks) else "契约写错了，必须改"
        print(f"{table:<22} {marks[0]:<17} {marks[1]:<10} {marks[2]:<10} {verdict}")

    print()
    if failures:
        print(f"FAIL  {failures} 处不符 —— 契约 §2 那张表的清单需要改写")
        return 1
    print("OK  契约 §2 点名的 6 张表全部自带三个分租列（写法正确）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
