"""唯一迁移 runner。

早期版本的教训（.local/recon-backend.md 高危项 3）：**4 份 runner、3 种校验和算法**——
    * `deploy.sh:141` 与 `deploy-blue-green.sh:88` 做 LF 归一化（双接受，容错）
    * `install-manual-test.sh:73` 用裸字节（不校验）
    * `backend/scripts/readiness/mysql_tools.py:127` 用裸字节（严格）
实测 7/35 个迁移文件是 CRLF（`.gitattributes` 只固定了 `*.sh`），`001_initial_auth.sql`
两种算法得到 `85a13e…` 与 `9ec9a6…`。四套工具先后运行必然互相判"校验和漂移"。

本项目只有一个 runner，因为：
    1. 校验和**先做换行归一化**再哈希，消除跨平台差异；
    2. 只做一件事——把没有记录在案的迁移按序应用并登记；
    3. 漂移直接报错退出，不猜、不糊。

用法::

    python tools/migrate.py status       # 列出已应用/待应用/漂移
    python tools/migrate.py apply        # 应用全部待应用迁移（事务化，逐个提交）
    python tools/migrate.py verify       # 只校验，不写入
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "database" / "migrations"
REGISTRY_FILE = "000_schema_migrations.sql"
FILENAME_PATTERN = re.compile(r"\A(\d{3})_([a-z0-9_]+)\.sql\Z")


def normalized_checksum(path: Path) -> str:
    """换行归一化后的 sha256。

    归一化在**字节层**做（把 CRLF 与孤立 CR 都变成 LF），而不是 shell 的
    ``sed 's/\\r$//'``——后者对行尾 CR 的处理依赖 sed 实现，跨平台不可靠。
    """
    raw = path.read_bytes()
    normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest()


class MigrationFileError(SystemExit):
    """迁移文件本身有问题（不是数据库的问题）。

    这类错误必须在**应用之前**发现：如果等到 ``cursor.execute()`` 才报，
    MySQL 会以 DDL 不可回滚的方式留下"部分建成的库"。
    """


def check_file_hygiene(path: Path) -> None:
    """迁移文件的内容卫生检查。

    两个都是**实测踩过的坑**，不是理论风险：

    1. **BOM**。PowerShell 5.1 的 ``Set-Content -Encoding UTF8`` 会写入 EF BB BF，
       而 MySQL 解析器把它当成 SQL 的一部分，报
       ``syntax error near '\\ufeff-- ===='``。本项目所有文件必须是**无 BOM 的 UTF-8**。
    2. **空文件 / 只有注释**。空迁移通常是误创建，应该报错而不是静默登记为"已应用"。

    注意：CRLF **不是**错误。文件可以有任意换行风格——checksum 会归一化掉它
    （这正是早期版本 4 份 runner / 3 种算法互相判漂移的根因所在，见模块 docstring）。
    """
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise MigrationFileError(
            f"{path.name} 含 UTF-8 BOM，MySQL 会把它当成 SQL 语法错误。\n"
            "  修复（PowerShell）：\n"
            "    $b=[IO.File]::ReadAllBytes($p)\n"
            "    [IO.File]::WriteAllText($p, [Text.Encoding]::UTF8.GetString($b,3,$b.Length-3),"
            " (New-Object Text.UTF8Encoding($false)))\n"
            "  教训：PowerShell 5.1 的 `Set-Content -Encoding UTF8` 会加 BOM；"
            "写 SQL/源码请用 [IO.File]::WriteAllText + UTF8Encoding($false)。"
        )
    if not raw.strip():
        raise MigrationFileError(f"{path.name} 是空文件")
    executable = [
        line
        for line in raw.decode("utf-8", errors="replace").splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    if not executable:
        raise MigrationFileError(f"{path.name} 只有注释、没有可执行语句")


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    path: Path
    checksum: str

    @property
    def label(self) -> str:
        return self.path.name


@dataclass(frozen=True, slots=True)
class Status:
    applied: list[Migration]
    pending: list[Migration]
    drifted: list[tuple[Migration, str]]


def discover() -> list[Migration]:
    if not MIGRATIONS_DIR.is_dir():
        raise SystemExit(f"迁移目录不存在：{MIGRATIONS_DIR}")
    found: list[Migration] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = FILENAME_PATTERN.match(path.name)
        if match is None:
            raise SystemExit(
                f"迁移文件名非法：{path.name}（要求 NNN_lower_snake_case.sql，例如 001_initial.sql）"
            )
        found.append(Migration(version=path.name[: -len(".sql")], path=path, checksum=normalized_checksum(path)))
    versions = [item.version for item in found]
    if len(set(versions)) != len(versions):
        raise SystemExit("迁移版本号重复")
    for migration in found:
        check_file_hygiene(migration.path)
    return found


def connect(database: str | None = None) -> pymysql.Connection:
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "fpa"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=database or os.environ.get("MYSQL_DATABASE", "fpa"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=5,
    )


def ensure_registry(connection: pymysql.Connection) -> None:
    registry = MIGRATIONS_DIR / REGISTRY_FILE
    if not registry.exists():
        raise SystemExit(f"缺少迁移登记表定义：{REGISTRY_FILE}")
    statements = split_statements(registry.read_text(encoding="utf-8"))
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    connection.commit()


def split_statements(sql: str) -> list[str]:
    """按 ``;`` 切分，但尊重 ``DELIMITER $$`` 块（审计表的触发器需要它）。

    早期版本的 runner 是把整个文件喂给 ``mysql`` 客户端，靠客户端处理 DELIMITER。
    我们用 PyMySQL，所以自己切——并且显式处理 DELIMITER 指令，避免把
    ``CREATE TRIGGER ... $$`` 切坏。
    """
    statements: list[str] = []
    delimiter = ";"
    buffer: list[str] = []

    for raw_line in sql.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.upper().startswith("DELIMITER "):
            pending = "\n".join(buffer).strip()
            if pending:
                statements.append(pending)
            buffer.clear()
            delimiter = stripped.split(None, 1)[1].strip()
            continue

        if stripped.startswith("--") or not stripped:
            continue

        buffer.append(line)
        if line.endswith(delimiter):
            statement = "\n".join(buffer).strip()
            if statement.endswith(delimiter):
                statement = statement[: -len(delimiter)]
            statement = statement.strip()
            if statement:
                statements.append(statement)
            buffer.clear()

    tail = "\n".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def read_applied(connection: pymysql.Connection) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT version, checksum FROM schema_migrations")
        rows = cursor.fetchall() or []
    return {str(row["version"]): str(row["checksum"]) for row in rows}


def inspect(connection: pymysql.Connection) -> Status:
    ensure_registry(connection)
    applied_map = read_applied(connection)
    migrations = discover()

    applied: list[Migration] = []
    pending: list[Migration] = []
    drifted: list[tuple[Migration, str]] = []

    for migration in migrations:
        if migration.version not in applied_map:
            pending.append(migration)
            continue
        recorded = applied_map[migration.version]
        if recorded != migration.checksum:
            drifted.append((migration, recorded))
        else:
            applied.append(migration)
    return Status(applied=applied, pending=pending, drifted=drifted)


def apply_migration(connection: pymysql.Connection, migration: Migration) -> None:
    sql = migration.path.read_text(encoding="utf-8")
    statements = split_statements(sql)
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
        cursor.execute(
            "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
            (migration.version, migration.checksum),
        )
    connection.commit()


def cmd_status(connection: pymysql.Connection) -> int:
    status = inspect(connection)
    print(f"已应用 {len(status.applied)} 个，待应用 {len(status.pending)} 个，漂移 {len(status.drifted)} 个")
    for migration in status.pending:
        print(f"  待应用  {migration.label}")
    for migration, recorded in status.drifted:
        print(f"  漂移    {migration.label}")
        print(f"            记录值 {recorded}")
        print(f"            当前值 {migration.checksum}")
    return 1 if status.drifted else 0


def cmd_verify(connection: pymysql.Connection) -> int:
    status = inspect(connection)
    if status.drifted:
        print("校验失败：以下迁移文件在应用后被修改过")
        for migration, recorded in status.drifted:
            print(f"  {migration.label}  记录 {recorded[:12]}…  当前 {migration.checksum[:12]}…")
        return 1
    if status.pending:
        print(f"校验失败：有 {len(status.pending)} 个迁移尚未应用")
        for migration in status.pending:
            print(f"  {migration.label}")
        return 1
    print(f"校验通过：{len(status.applied)} 个迁移全部一致")
    return 0


def cmd_apply(connection: pymysql.Connection) -> int:
    status = inspect(connection)
    if status.drifted:
        print("拒绝应用：以下迁移文件在应用后被修改过")
        for migration, recorded in status.drifted:
            print(f"  {migration.label}  记录 {recorded[:12]}…  当前 {migration.checksum[:12]}…")
        return 1
    if not status.pending:
        print("没有待应用的迁移")
        return 0

    # 应用之前，先把**所有**待应用文件的 SQL 切分与括号平衡检查一遍。
    # 理由：MySQL 的 DDL 不可回滚。如果第 3 个文件的第 20 条语句有语法错，
    # 前 19 条已经落库，会留下"部分建成"的库（本项目第一次试跑就是这样）。
    # 事前的静态检查不能覆盖所有语法错，但能挡住最常见的一类。
    for migration in status.pending:
        statements = split_statements(migration.path.read_text(encoding="utf-8"))
        if not statements:
            print(f"拒绝应用：{migration.label} 没有可执行语句")
            return 1
        for index, statement in enumerate(statements, 1):
            if statement.count("(") != statement.count(")"):
                print(
                    f"拒绝应用：{migration.label} 第 {index} 条语句括号不平衡"
                    f"（左 {statement.count('(')} 右 {statement.count(')')}）"
                )
                return 1

    for migration in status.pending:
        print(f"正在应用 {migration.label} …", end=" ", flush=True)
        try:
            apply_migration(connection, migration)
        except Exception as exc:  # noqa: BLE001
            print("失败")
            print(f"  {type(exc).__name__}: {exc}")
            connection.rollback()
            print()
            print("注意：MySQL 的 DDL 不回滚，本次可能已留下部分建成对象。")
            print("迁移文件必须写成**可重入**的（CREATE TABLE IF NOT EXISTS / DROP TRIGGER IF EXISTS），")
            print("因此修正后可直接重跑 apply。若当前库是脏的，用 `python tools/migrate.py reset` 重建。")
            return 1
        print("完成")
    print(f"全部完成，共应用 {len(status.pending)} 个迁移")
    return 0


def cmd_reset(connection: pymysql.Connection) -> int:
    """**仅用于开发**：删库重建，然后应用全部迁移。

    存在的理由：MySQL 的 DDL 不可回滚，迁移只能写成可重入的形式；而当库已经被
    半成品对象污染时，"重来一遍"比"手工清理残骸"更快也更可靠。这个场景不是假想——
    本项目第一次试跑 `001` 时就在触发器处失败，留下了 15 张已建成的表。

    用 **root** 连接执行：应用账号的授权被刻意隔离在单库内（没有跨库的 CREATE/DROP），
    所以它无法删掉自己所在的库。这与 `bootstrap_db.py` 用 root 建库是同一个理由。

    在 APP_ENV=production 下拒绝执行。
    """
    import os

    if os.environ.get("APP_ENV", "development").strip().lower() == "production":
        print("拒绝执行：reset 不允许在生产环境运行")
        return 1

    database = os.environ.get("MYSQL_DATABASE", "fpa")
    root_password = os.environ.get("MYSQL_ROOT_PASSWORD", "")
    root_user = os.environ.get("MYSQL_ROOT_USER", "root")
    if not root_password:
        print("拒绝执行：reset 需要 MYSQL_ROOT_PASSWORD（应用账号无权删库）")
        return 1

    print(f"将删除并重建数据库 `{database}` 及其全部数据。")
    try:
        admin = pymysql.connect(
            host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
            port=int(os.environ.get("MYSQL_PORT", "3306")),
            user=root_user,
            password=root_password,
            connect_timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"无法以 {root_user} 连接：{type(exc).__name__}: {exc}")
        return 2

    try:
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
            cursor.execute(
                f"CREATE DATABASE `{database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        admin.commit()
    finally:
        admin.close()

    print(f"数据库 `{database}` 已重建")
    connection.select_db(database)
    return cmd_apply(connection)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FPA 迁移 runner（唯一版本）")
    parser.add_argument(
        "command",
        choices=("status", "apply", "verify", "reset"),
        help="status=查看 / apply=应用 / verify=只校验 / reset=删库重建后应用（仅开发）",
    )
    args = parser.parse_args(argv)

    try:
        connection = connect()
    except Exception as exc:  # noqa: BLE001
        print(f"无法连接数据库：{type(exc).__name__}: {exc}")
        print("请先设置 MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE")
        return 2

    try:
        if args.command == "status":
            return cmd_status(connection)
        if args.command == "verify":
            return cmd_verify(connection)
        if args.command == "reset":
            return cmd_reset(connection)
        return cmd_apply(connection)
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
