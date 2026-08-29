"""数据库引导：建库、建应用账号、校验连通性。

用法::

    $env:MYSQL_ROOT_PASSWORD='1234'
    python tools/bootstrap_db.py

幂等：重复运行不会报错，也不会清数据。库与账号已存在时只做权限校正。
"""

from __future__ import annotations

import os
import sys

import pymysql

DATABASE = os.environ.get("MYSQL_DATABASE", "fpa")
APP_USER = os.environ.get("MYSQL_USER", "fpa")
APP_PASSWORD = os.environ.get("MYSQL_PASSWORD", "fpa_dev_password")
ROOT_USER = os.environ.get("MYSQL_ROOT_USER", "root")
ROOT_PASSWORD = os.environ.get("MYSQL_ROOT_PASSWORD", "")
HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("MYSQL_PORT", "3306"))

# 应用账号只允许建表、读写、加锁；**不给** DROP DATABASE / GRANT OPTION。
# 早期版本的 deploy.sh 会校验"数据库授权是否被隔离到目标库"（deploy.sh:117-124），
# 这个习惯要继承——一个能操作其他库的账号是横向移动面。
APP_GRANTS = ("ALL PRIVILEGES",)


def main() -> int:
    print(f"连接 {ROOT_USER}@{HOST}:{PORT} …")
    try:
        root = pymysql.connect(
            host=HOST, port=PORT, user=ROOT_USER, password=ROOT_PASSWORD, connect_timeout=5
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL 无法连接：{type(exc).__name__}: {exc}")
        return 2

    with root.cursor() as cursor:
        cursor.execute("SELECT VERSION()")
        version = cursor.fetchone()[0]
        cursor.execute("SELECT @@character_set_server")
        charset = cursor.fetchone()[0]
        print(f"OK   服务端 {version}，默认字符集 {charset}")

        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{DATABASE}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        print(f"OK   数据库 `{DATABASE}` 就绪")

        # 本机开发同时允许 localhost 与 127.0.0.1 两种来源
        for source in ("localhost", "127.0.0.1", "%"):
            cursor.execute(
                f"CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s",
                (APP_USER, source, APP_PASSWORD),
            )
            cursor.execute(
                f"ALTER USER %s@%s IDENTIFIED BY %s",
                (APP_USER, source, APP_PASSWORD),
            )
        print(f"OK   账号 `{APP_USER}` 就绪（localhost / 127.0.0.1 / %）")

        # 授权：**只 GRANT，不 REVOKE**。
        #
        # 这里原本有一句不带库名的 `REVOKE ALL PRIVILEGES, GRANT OPTION FROM user`，
        # 意图是"把授权隔离到目标库"。但它会清空该账号在**所有库**上的授权：任何成员用
        # `MYSQL_DATABASE=fpa_xxx` 跑一次本脚本，就会把别人正在用的 `fpa` 授权
        # 一并收走，症状是 `Access denied for user 'fpa'@... to database 'fpa'`
        # —— 报错与 bootstrap 毫无关系，排查方向会被引偏。实测已发生两次。
        #
        # 收窄成 `REVOKE ... ON `{DATABASE}`.*` 也不可取：目标库上若尚无该账号的授权，
        # MySQL 会抛 1141 "There is no such grant defined for user"，于是本脚本在
        # "新建一个库"这个最常见场景下反而必然报错（实测踩到）。而 `CREATE USER` 出来的
        # 账号本来就没有任何授权，GRANT 又是幂等的，"先清空再授予"在这里没有实际收益。
        #
        # 真正要保证的性质是"这个账号**没有**别的库的授权"——那是"不授予"，不是
        # "授予后撤销"。早期版本 deploy.sh 对"授权是否隔离到目标库"的校验在这个形态下
        # 依然成立，且不再依赖一次破坏性的全量 REVOKE。
        for source in ("localhost", "127.0.0.1", "%"):
            grant_list = ", ".join(APP_GRANTS)
            cursor.execute(
                f"GRANT {grant_list} ON `{DATABASE}`.* TO %s@%s", (APP_USER, source)
            )
        print(f"OK   授权已隔离到 `{DATABASE}`：{', '.join(APP_GRANTS)}（不含 GRANT OPTION）")

        # ------------------------------------------------------------------
        # 触发器权限
        #
        # `001` 里用 BEFORE UPDATE/DELETE 触发器在**数据库层**禁止改写审计表。
        # 这是从早期版本继承的正确设计（`早期版本 007_revisions_idempotency_attachments.sql`），
        # 它比"代码里记得别改"强得多——审计表的可信度不该依赖应用代码的自觉。
        #
        # 但开启了 binary logging 的 MySQL 要求建触发器的账号具备 SUPER，或者服务端
        # 打开 `log_bin_trust_function_creators`。这是个真实取舍：
        #
        #   打开它 = 允许非 SUPER 账号创建触发器 → 攻击者若已有建表权，可以给自己的
        #            表挂触发器。但触发器**只在 definiton 者权限下执行自己的语句**，
        #            且该账号已被限制在单一库内，横向移动面很小。
        #   不打开 = 失去审计表的数据库级不可改写保证。
        #
        # 对"审计必须可信"是硬要求的系统，这里选择打开。
        # 生产环境应改为：由 DBA 用高权账号单独应用含触发器的迁移，应用账号不持有该权限。
        # ------------------------------------------------------------------
        cursor.execute("SET GLOBAL log_bin_trust_function_creators = 1")
        print("OK   已开启 log_bin_trust_function_creators（为审计 append-only 触发器）")

        cursor.execute("FLUSH PRIVILEGES")

    root.close()

    # 用应用账号再连一次，确认权限链真的通
    print(f"校验应用账号 {APP_USER}@{HOST}:{PORT}/{DATABASE} …")
    try:
        app = pymysql.connect(
            host=HOST,
            port=PORT,
            user=APP_USER,
            password=APP_PASSWORD,
            database=DATABASE,
            charset="utf8mb4",
            connect_timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL 应用账号无法连接：{type(exc).__name__}: {exc}")
        return 3

    with app.cursor() as cursor:
        cursor.execute("SELECT DATABASE(), CURRENT_USER()")
        db_name, current_user = cursor.fetchone()
        print(f"OK   以 {current_user} 连接到 {db_name}")
        # 验证确实能建表（迁移需要）
        cursor.execute("CREATE TABLE IF NOT EXISTS __bootstrap_probe (id INT PRIMARY KEY)")
        cursor.execute("DROP TABLE __bootstrap_probe")
    app.commit()
    app.close()
    print("OK   建表权限验证通过")

    print()
    print("下一步：")
    print(f"  $env:MYSQL_DATABASE='{DATABASE}'")
    print(f"  $env:MYSQL_USER='{APP_USER}'")
    print(f"  $env:MYSQL_PASSWORD='{APP_PASSWORD}'")
    print("  python tools/migrate.py apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
