"""数据库结构自检：连上真实 MySQL，核对迁移产出的对象**真的存在**。

这不是"命令没报错"，而是"对象确实在库里"。两者的区别在本项目里是实打实的：
第一次试跑 `001` 时迁移报错退出，但前 15 张表已经建成——如果只看返回码，
会以为什么都没发生。

用法::

    python tools/db_selfcheck.py
"""

from __future__ import annotations

import os
import sys

import pymysql

EXPECTED_TABLES = {
    # 身份与组织
    "organizations",
    "farms",
    "users",
    "roles",
    "permissions",
    "role_permissions",
    "user_roles",
    # DataScope
    "data_scopes",
    "user_data_scopes",
    # 会话
    "sessions",
    # 治理
    "idempotency_keys",
    "agent_confirmations",
    "record_revisions",
    "rate_limits",
    # 审计
    "audit_logs",
    # 登记表
    "schema_migrations",
}

EXPECTED_TRIGGERS = {"audit_logs_no_update", "audit_logs_no_delete"}

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


def connect() -> pymysql.Connection:
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "fpa"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "fpa"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def main() -> int:
    try:
        connection = connect()
    except Exception as exc:  # noqa: BLE001
        print(f"无法连接数据库：{type(exc).__name__}: {exc}")
        return 2

    with connection.cursor() as cursor:
        print("=== 1. 表 ===")
        cursor.execute("SHOW TABLES")
        actual = {list(row.values())[0] for row in cursor.fetchall()}
        missing = EXPECTED_TABLES - actual
        unexpected = actual - EXPECTED_TABLES
        check(f"期望的 {len(EXPECTED_TABLES)} 张表全部存在", not missing, f"缺失：{sorted(missing)}")
        if unexpected:
            print(f"  INFO  额外表：{sorted(unexpected)}")

        print("\n=== 2. 审计 append-only 触发器（数据库级强制）===")
        cursor.execute("SHOW TRIGGERS")
        triggers = {row["Trigger"] for row in cursor.fetchall()}
        missing_triggers = EXPECTED_TRIGGERS - triggers
        check(
            "UPDATE / DELETE 触发器都已建立",
            not missing_triggers,
            f"缺失：{sorted(missing_triggers)}",
        )

        print("\n=== 3. DataScope 的分租键约束（本项目要根除的头号缺陷）===")
        cursor.execute(
            "SELECT CONSTRAINT_NAME, CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS "
            "WHERE CONSTRAINT_SCHEMA = DATABASE() AND CONSTRAINT_NAME = 'chk_data_scopes_binding'"
        )
        row = cursor.fetchone()
        check("chk_data_scopes_binding 约束存在", row is not None)
        if row:
            clause = row["CHECK_CLAUSE"]
            for scope_type in ("farm", "area", "pond"):
                check(
                    f"约束覆盖 {scope_type} 型范围的分租键",
                    scope_type in clause and f"{scope_type}_id" in clause,
                )

        print("\n=== 4. 幂等表结构：确认**没有**响应快照列 ===")
        cursor.execute("SHOW COLUMNS FROM idempotency_keys")
        columns = {row["Field"] for row in cursor.fetchall()}
        check(
            "没有 response_json（早期版本在这里存 200 KB 快照）",
            "response_json" not in columns,
            f"实际列：{sorted(columns)}",
        )
        check(
            "改存 resource_type / resource_id（回放时从业务表读）",
            {"resource_type", "resource_id"} <= columns,
        )

        print("\n=== 5. 确认表结构的唯一键：确认令牌必须一次性 ===")
        cursor.execute("SHOW INDEX FROM agent_confirmations WHERE Non_unique = 0")
        unique_columns = {row["Column_name"] for row in cursor.fetchall()}
        check("token_hash 唯一", "token_hash" in unique_columns, f"唯一列：{sorted(unique_columns)}")
        cursor.execute("SHOW COLUMNS FROM agent_confirmations")
        confirm_columns = {row["Field"] for row in cursor.fetchall()}
        check(
            "绑定六元组的字段齐全（user/session/capability/params/target/expiry）",
            {
                "user_id",
                "session_hash",
                "capability",
                "params_hash",
                "target_ref",
                "expires_at",
                "used_at",
            }
            <= confirm_columns,
            f"缺失：{sorted({'user_id','session_hash','capability','params_hash','target_ref','expires_at','used_at'} - confirm_columns)}",
        )

        print("\n=== 6. 行为验证：审计表真的不能改写 ===")
        # 插一行再试改，看触发器是否真的拦得住。
        cursor.execute(
            "INSERT INTO audit_logs (request_id, capability, domain, result) "
            "VALUES ('selfcheck', 'selfcheck.probe', 'audit', 'success')"
        )
        cursor.execute("SELECT LAST_INSERT_ID() AS id")
        audit_id = int(cursor.fetchone()["id"])
        try:
            cursor.execute("UPDATE audit_logs SET result='failure' WHERE id=%s", (audit_id,))
            check("UPDATE 审计表被拒绝", False, "居然改成功了")
        except pymysql.err.OperationalError as exc:
            check(
                "UPDATE 审计表被数据库拒绝",
                "append-only" in str(exc),
                str(exc)[:80],
            )
        try:
            cursor.execute("DELETE FROM audit_logs WHERE id=%s", (audit_id,))
            check("DELETE 审计表被拒绝", False, "居然删成功了")
        except pymysql.err.OperationalError as exc:
            check("DELETE 审计表被拒绝", "append-only" in str(exc), str(exc)[:80])

        print("\n=== 7. 行为验证：DataScope 的 farm 型**必须**带 farm_id ===")
        # 这是早期版本静默返回 1=0 的那个场景。新库必须在数据库层就拒绝。
        #
        # 注意异常类型：MySQL 对 CHECK 约束违规抛 **3819**，PyMySQL 归类为
        # OperationalError（不是 IntegrityError）。这个细节是实测出来的——
        # 凭直觉写 `except IntegrityError` 会漏掉，断言就变成假阳性。
        try:
            cursor.execute(
                "INSERT INTO data_scopes (code, name, scope_type, farm_id) "
                "VALUES ('probe-bad-farm', '缺分租键的农场范围', 'farm', NULL)"
            )
            check("farm 型范围缺 farm_id 被拒绝", False, "居然插进去了")
        except pymysql.err.OperationalError as exc:
            check(
                "farm 型范围缺 farm_id 被数据库拒绝（3819 CHECK 约束）",
                exc.args and int(exc.args[0]) == 3819,
                f"errno={exc.args[0] if exc.args else '?'}",
            )
        except pymysql.err.IntegrityError as exc:
            check("farm 型范围缺 farm_id 被数据库拒绝", True, f"（IntegrityError {exc.args}）")

        try:
            cursor.execute(
                "INSERT INTO data_scopes (code, name, scope_type, farm_id) "
                "VALUES ('probe-good-farm', '合法农场范围', 'farm', 1)"
            )
            check("farm 型范围带 farm_id 可以插入", True)
            cursor.execute("DELETE FROM data_scopes WHERE code='probe-good-farm'")
        except Exception as exc:  # noqa: BLE001
            check("farm 型范围带 farm_id 可以插入", False, str(exc)[:90])

        try:
            cursor.execute(
                "INSERT INTO data_scopes (code, name, scope_type) "
                "VALUES ('probe-personal', '仅本人数据', 'personal')"
            )
            check("personal 型范围不需要分租键", True)
            cursor.execute("DELETE FROM data_scopes WHERE code='probe-personal'")
        except Exception as exc:  # noqa: BLE001
            check("personal 型范围不需要分租键", False, str(exc)[:90])

        print("\n=== 8. 迁移登记表 ===")
        cursor.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
        rows = cursor.fetchall()
        # 断言"磁盘上的每个迁移都在库里"，而不是写死数量——
        # 写死数量的断言每加一个迁移就会红，而它想验证的其实是"没有漏登记的迁移"。
        from pathlib import Path as _Path

        on_disk = sorted(
            item.stem for item in (_Path(__file__).resolve().parents[1] / "database" / "migrations").glob("*.sql")
        )
        registered = sorted(str(row["version"]) for row in rows)
        check(
            f"{len(on_disk)} 个迁移全部已登记",
            registered == on_disk,
            f"库内 {registered} vs 磁盘 {on_disk}",
        )
        for row in rows:
            print(f"        {row['version']}  {row['checksum'][:16]}…")

        # 清理探针行（审计表删不掉，所以只清理 data_scopes 的探针）
        cursor.execute("DELETE FROM data_scopes WHERE code LIKE 'probe-%'")

    connection.close()
    print()
    if FAILURES:
        print(f"结果：{FAILURES} 项失败")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
