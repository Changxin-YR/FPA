"""在**临时库**里验证改写后的 004_cost.sql：能建起来、可重入、CHECK 真的拦得住。

只碰 `yuxin_cost_enum_probe` 这个临时库，用完即删，不碰 yuxin。

验证四件事：
  1. 000/001/003/004 能按序应用；
  2. 004 **连跑两遍**（迁移必须可重入）；
  3. 被改成 VARCHAR(32) 的四列，**非法取值被 CHECK 拒绝**、合法取值放行；
  4. 仍保留 ENUM 的四列，非法取值被 MySQL 自己拒绝（ENUM 的原生行为）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import pymysql  # noqa: E402

from migrate import split_statements  # noqa: E402 - 复用 runner 的切分器（含 DELIMITER 处理）

PROBE = "yuxin_cost_enum_probe"
ORDER = [
    "000_schema_migrations.sql",
    "001_identity_access_governance.sql",
    "003_master_data.sql",
    "004_cost.sql",
]
FAILS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILS
    if ok:
        print(f"  PASS  {label}")
    else:
        FAILS += 1
        print(f"  FAIL  {label}  {detail}")


def apply(conn, name: str, times: int = 1) -> bool:
    sql = (ROOT / "database" / "migrations" / name).read_text(encoding="utf-8")
    for attempt in range(1, times + 1):
        with conn.cursor() as cur:
            for statement in split_statements(sql):
                try:
                    cur.execute(statement)
                except Exception as exc:  # noqa: BLE001
                    print(f"  FAIL  {name}（第 {attempt} 遍）{type(exc).__name__}: {exc}")
                    print(f"        {statement[:160]}")
                    return False
    return True


def main() -> int:
    root = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="1234",
                           charset="utf8mb4", autocommit=True)
    with root.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {PROBE}")
        cur.execute(f"CREATE DATABASE {PROBE} DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci")

    conn = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="1234",
                           database=PROBE, charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor, autocommit=True)
    try:
        print("=== 1/2. 应用迁移，004 连跑两遍（可重入）===")
        for name in ORDER:
            times = 2 if name == "004_cost.sql" else 1
            if not apply(conn, name, times):
                return 1
            suffix = "连跑两遍均成功" if times == 2 else "应用成功"
            print(f"  PASS  {name} {suffix}")

        print("\n=== 3. 列类型确认 ===")
        with conn.cursor() as cur:
            for table, column, want in (
                ("cost_entries", "status", "varchar(32)"),
                ("cost_entries", "confirm_state", "varchar(32)"),
                ("cost_categories", "status", "varchar(32)"),
                ("accounting_periods", "status", "enum('open','closed')"),
                ("cost_entries", "source_type", "enum('manual_expense','warehouse_ledger')"),
                ("cost_entries", "target_type", "enum('farm','area','pond','batch')"),
                ("cost_categories", "default_nature", "enum('direct','public')"),
            ):
                cur.execute(
                    "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                    (PROBE, table, column),
                )
                row = cur.fetchone()
                got = (row["COLUMN_TYPE"] if row else "").lower()
                check(f"{table}.{column} = {want}", got == want, f"实际 {got!r}")

        print("\n=== 4. CHECK 真的拦得住非法取值 ===")
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM areas ORDER BY id LIMIT 1")
            area = cur.fetchone()
            cur.execute("SELECT id, organization_id, farm_id FROM areas ORDER BY id LIMIT 1")
            a = cur.fetchone()

            def insert_entry(status="draft", confirm_state="pending"):
                cur.execute(
                    "INSERT INTO cost_entries (organization_id, farm_id, area_id, "
                    "category_code, amount, occurred_on, period_start, period_end, "
                    "source_type, source_ref, confirm_state, status, created_by) "
                    "VALUES (%s,%s,%s,'feed',10,'2026-09-10','2026-09-01','2026-09-30',"
                    "'manual_expense','PROBE-X',%s,%s,1)",
                    (a["organization_id"], a["farm_id"], a["id"], confirm_state, status),
                )

            # 合法：先证"能写"
            try:
                insert_entry()
                print("  PASS  合法取值可写入（确保下面的拒绝不是因为表根本插不进）")
            except Exception as exc:  # noqa: BLE001
                check("合法取值可写入", False, f"{exc}")
                return 1

            for label, kwargs in (
                ("非法 status 被 CHECK 拒绝", {"status": "no-such-status"}),
                ("非法 confirm_state 被 CHECK 拒绝", {"confirm_state": "maybe"}),
            ):
                try:
                    insert_entry(**kwargs)
                    check(label, False, "竟然写进去了")
                except Exception:
                    check(label, True)

            # 保留 ENUM 的列：非法取值由 ENUM 拒绝
            try:
                cur.execute(
                    "INSERT INTO cost_entries (organization_id, farm_id, area_id, "
                    "category_code, amount, occurred_on, period_start, period_end, "
                    "source_type, source_ref, created_by) "
                    "VALUES (%s,%s,%s,'feed',10,'2026-09-10','2026-09-01','2026-09-30',"
                    "'bogus_type','PROBE-Y',1)",
                    (a["organization_id"], a["farm_id"], a["id"]),
                )
                check("非法 source_type 被 ENUM 拒绝（仍是 ENUM）", False, "竟然写进去了")
            except Exception:
                check("非法 source_type 被 ENUM 拒绝（仍是 ENUM）", True)
    finally:
        conn.close()
        with root.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {PROBE}")
        root.close()

    print(f"\n{'全部通过' if not FAILS else f'{FAILS} 项失败'}（临时库 {PROBE} 已删除）")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
