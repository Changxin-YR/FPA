"""查清残留在库里的成本探针行到底是哪来的、以及清理条件能不能命中它。

不猜：把行、时间戳、以及"用清理语句会不会命中"都打出来。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import pymysql  # noqa: E402

CONFIG = dict(host="127.0.0.1", port=3306, user="fpa",
              password="fpa_dev_password", database="fpa",
              charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
              autocommit=True)


def main() -> int:
    connection = pymysql.connect(**CONFIG)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, source_type, source_ref, target_type, target_id, "
            "occurred_on, period_start, period_end, created_at, created_by, "
            "confirm_state, status FROM cost_entries "
            "WHERE source_ref LIKE 'COST-E2E%' ORDER BY id"
        )
        rows = cursor.fetchall()
        print(f"source_ref LIKE 'COST-E2E%' 命中 {len(rows)} 行：")
        for row in rows:
            print(f"    #{row['id']} {row['source_ref']:28} {row['source_type']:17} "
                  f"{row['target_type']}/{row['target_id']}  {row['occurred_on']}  "
                  f"created_at={row['created_at']}  by={row['created_by']}")

        print("\n清理语句用的模式是 'COST-E2E-%'（带连字符），再查一次：")
        cursor.execute(
            "SELECT COUNT(*) AS n FROM cost_entries WHERE source_ref LIKE 'COST-E2E-%'"
        )
        print(f"    命中 {cursor.fetchone()['n']} 行")

        print("\n所有成本的 source_ref 去重列表（看有没有别的残留）：")
        cursor.execute("SELECT DISTINCT source_ref FROM cost_entries ORDER BY source_ref")
        for row in cursor.fetchall():
            print(f"    {row['source_ref']}")

        print("\n探针塘口：")
        cursor.execute("SELECT id, code, name FROM ponds WHERE code LIKE 'COST-E2E%' ORDER BY id")
        for row in cursor.fetchall():
            print(f"    {row}")

        print("\n幂等键（cost.%）：")
        cursor.execute(
            "SELECT capability, status, COUNT(*) AS n FROM idempotency_keys "
            "WHERE capability LIKE 'cost.%' GROUP BY capability, status"
        )
        for row in cursor.fetchall():
            print(f"    {row}")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
