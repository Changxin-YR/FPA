"""真库形状 vs 迁移文件 的逐表比对（`migrate verify` 的盲区补丁）。

## 为什么需要这个工具

`tools/migrate.py verify` 只回答一个问题：**登记值（`schema_migrations`）与文件
的校验和对不对得上**。它**不**回答「真库的形状与文件对不对得上」。

2026-09-13 那次真库漂移正是从这个盲区溜过去的：

  * `004_cost.sql` 在**登记之后**又按 §1.2 裁决被改过（3 个记录生命周期列
    `ENUM` -> `VARCHAR(32)` + `CHECK`），真库没跟着动；
  * `007_purchase.sql` 同样被改过（`cancel_reason` -> `reason`）。

当时 `migrate.py status` 只报"漂移"（登记值≠文件），**没有任何工具能回答
"真库到底长什么样、跟文件差在哪"**。这个工具就是补这一格：它把迁移文件
在一个**临时库**里从零跑一遍，然后把结果与真库逐表 diff。

## 判据（本项目的既定要求）

> 「0 处不一致」必须能**自证**「确实扫到了东西」，否则「0」没有信息量。

所以每次运行都会先打印扫描规模（比了几张表、每张表几行结构定义），
并设三道**自证闸门**；任何一道不过就报"未能证明一致"，而不是打印"一致"：

  1. 临时库必须建成 **>= 20** 张表（迁移没跑起来 / 权限不对 -> 不是"一致"）；
  2. 目标库里至少有一张表被真正比对（禁止空集通过）；
  3. 目标库**不能为空**（空库说明连错了库）。

## 绝不碰目标库

本工具对目标库**只读**（`SHOW TABLES` / `SHOW CREATE TABLE`）。
临时库由 root 建、跑完即删；`--admin-user` 只是用来建/删临时库。
**没有任何 DDL 会落在目标库上。**

## 退出码

    0  一致（且满足三道自证闸门）
    1  发现差异（差异清单已打印）
    2  未能证明一致（临时库没建起来 / 扫不到表 / 连不上）—— **不等于一致**

## 用法

    $env:MYSQL_USER='fpa'; $env:MYSQL_PASSWORD='fpa_dev_password'
    python tools\\schema_parity.py                      # 比对 fpa
    python tools\\schema_parity.py --only cost_entries  # 只比指定的表
    python tools\\schema_parity.py --verbose            # 每张表都打印一行结论
    python tools\\schema_parity.py --temp fpa_parity_keep --keep   # 留现场排查

自检（证明它真的会红，而不是"永远返回 0"）：

    python tools\\schema_parity_selftest.py
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from migrate import MIGRATIONS_DIR, split_statements  # noqa: E402

#: 自证闸门 1：临时库至少要建成这么多张表，否则"比对"本身不成立。
MIN_TABLES = 20

#: 目标库里由 **e2e 工具自建**、迁移里**刻意没有**的表。
#:
#: 列出来是为了让报告说清"为什么它们不在比对范围"，而不是静默忽略 ——
#: 静默忽略会让下一个看到"40/40 一致"的人以为全库都被覆盖了。
TOOL_OWNED_TABLES = frozenset(
    {"agent_ponds", "loader_probe_rows", "selftest_ponds", "web_ponds"}
)

EXIT_OK = 0
EXIT_DIFF = 1
EXIT_UNPROVEN = 2

#: 行首形如 ``CONSTRAINT `name` ...`` / ``KEY `name` ...`` / ``PRIMARY KEY ...``
_NAMED_OBJECT = re.compile(r"^(?:CONSTRAINT|KEY|UNIQUE KEY|FULLTEXT KEY|SPATIAL KEY)\s+`([^`]+)`")
_PRIMARY_KEY = re.compile(r"^PRIMARY KEY\b")


def _category(line: str) -> str:
    """把一行结构定义归类，让差异清单按类聚合（而不是糊成一坨）。"""
    if _PRIMARY_KEY.match(line):
        return "主键"
    if line.startswith("CONSTRAINT") and "FOREIGN KEY" in line:
        return "外键"
    if line.startswith("CONSTRAINT") and "CHECK" in line:
        return "CHECK"
    if "GENERATED ALWAYS AS" in line:
        return "生成列"
    if line.startswith("KEY") or line.startswith("UNIQUE KEY"):
        return "索引/唯一键"
    if line.startswith("CONSTRAINT"):
        return "约束(其它)"
    return "列"


def canonical(ddl: str) -> set[str]:
    """把 `SHOW CREATE TABLE` 的输出规范化成可比对的结构行集合。

    逐行比对（而非整串比对）的理由：MySQL 会**重排**对象的书写顺序
    （外键助手索引、CHECK 的位置都跟 CREATE 时的写法无关），整串比对会把
    "顺序不同"误报成"结构不同"。按行取集合就消除了这个噪声。

    `AUTO_INCREMENT=N` 是**运行时状态**不是结构（真库因为跑过 e2e 而更大），
    必须剔除，否则每次跑都是差异。
    """
    lines: set[str] = set()
    for raw in ddl.splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.startswith("CREATE TABLE") or line.startswith(")"):
            continue
        line = re.sub(r"\s+", " ", line)
        line = re.sub(r"\s*AUTO_INCREMENT=\d+", "", line)
        line = re.sub(r"\s*(ENGINE|DEFAULT CHARSET|COLLATE)=[^\s]*", "", line)
        line = line.strip().rstrip(",").strip()
        if line:
            lines.add(line)
    return lines


@dataclass
class TableDiff:
    table: str
    only_live: list[str] = field(default_factory=list)
    only_file: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.only_live and not self.only_file


@dataclass
class Report:
    compared: int = 0
    clean: int = 0
    identical_tables: list[str] = field(default_factory=list)
    diffs: list[TableDiff] = field(default_factory=list)
    only_target: list[str] = field(default_factory=list)
    only_temp: list[str] = field(default_factory=list)
    structure_lines: int = 0

    @property
    def consistent(self) -> bool:
        return not self.diffs and not self.only_target and not self.only_temp


def admin_connect(args: argparse.Namespace, database: str | None = None):
    return pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.admin_user,
        password=os.environ.get("MYSQL_ROOT_PASSWORD", ""),
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def app_connect(database: str, args: argparse.Namespace):
    return pymysql.connect(
        host=args.host,
        port=args.port,
        user=os.environ.get("MYSQL_USER", "fpa"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def table_names(connection) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute("SHOW TABLES")
        return {list(row.values())[0] for row in cursor.fetchall()}


def show_create(connection, table: str) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SHOW CREATE TABLE `" + table + "`")
        row = cursor.fetchone()
    return row["Create Table"]


def build_temp(args: argparse.Namespace) -> tuple[object, int]:
    """在临时库里从零应用全部迁移。返回 (连接, 建成表数)。"""
    admin = admin_connect(args)
    with admin.cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS `{args.temp}`")
        cursor.execute(
            f"CREATE DATABASE `{args.temp}` "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        # 应用账号被刻意隔离在单库内，需要显式授权才能进临时库。
        # 不能用参数占位符 —— MySQL 的 `GRANT ... TO` 不接受预处理参数，只能拼字符串；
        # 库名与用户名都来自本次运行的参数/环境变量，不是外部输入。
        app_user = os.environ.get("MYSQL_USER", "fpa")
        cursor.execute(f"GRANT ALL PRIVILEGES ON `{args.temp}`.* TO '{app_user}'@'%'")
        cursor.execute("FLUSH PRIVILEGES")
    admin.close()

    connection = app_connect(args.temp, args)
    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migrations:
        raise RuntimeError(f"迁移目录里没有 .sql：{MIGRATIONS_DIR}")
    for path in migrations:
        statements = split_statements(path.read_text(encoding="utf-8"))
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        connection.commit()
    return connection, len(table_names(connection))


def drop_temp(args: argparse.Namespace) -> None:
    admin = admin_connect(args)
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{args.temp}`")
    finally:
        admin.close()


def compare(target, temp, only: set[str] | None) -> Report:
    report = Report()
    live_tables = table_names(target)
    temp_tables = table_names(temp)

    report.only_target = sorted(live_tables - temp_tables - TOOL_OWNED_TABLES)
    report.only_temp = sorted(temp_tables - live_tables)

    shared = sorted(live_tables & temp_tables)
    if only:
        shared = [name for name in shared if name in only]

    for table in shared:
        left = canonical(show_create(target, table))
        right = canonical(show_create(temp, table))
        report.compared += 1
        report.structure_lines += max(len(left), len(right))
        diff = TableDiff(
            table=table,
            only_live=sorted(line for line in left - right),
            only_file=sorted(line for line in right - left),
        )
        if diff.is_clean:
            report.clean += 1
            report.identical_tables.append(table)
        else:
            report.diffs.append(diff)
    return report


def _print_group(title: str, lines: list[str], order: list[str]) -> None:
    if not lines:
        return
    print(f"        {title}")
    buckets: dict[str, list[str]] = {}
    for line in lines:
        buckets.setdefault(_category(line), []).append(line)
    for key in sorted(buckets, key=lambda item: order.index(item) if item in order else 99):
        for line in buckets[key]:
            print(f"          [{key}] {line}")


def print_report(report: Report, args: argparse.Namespace, built_tables: int) -> None:
    order = ["列", "主键", "索引/唯一键", "外键", "CHECK", "生成列", "约束(其它)"]

    print("=== 扫描规模（『0 处不一致』必须能自证『确实扫到了东西』）===")
    print(f"  临时库按迁移文件建成表数 : {built_tables}")
    print(f"  实际比对表数             : {report.compared}")
    print(f"  累计比对结构行（列/键/约束/CHECK）: {report.structure_lines}")
    print(f"  完全一致的表             : {report.clean}")
    if args.verbose:
        for table in report.identical_tables:
            print(f"        ✓ {table}")
    if args.only:
        print(f"  （--only 过滤生效，只比了 {sorted(args.only)}）")

    print()
    print("=== 比对结果 ===")
    if report.only_target:
        print(f"  真库有、迁移文件里没有的表（{len(report.only_target)} 张）：")
        for name in report.only_target:
            marker = "（已知 e2e 工具自建，非迁移职责）" if name in TOOL_OWNED_TABLES else ""
            print(f"        - {name} {marker}")
    if report.only_temp:
        print(f"  迁移文件建出、真库没有的表（{len(report.only_temp)} 张）：")
        for name in report.only_temp:
            print(f"        - {name}")

    if report.diffs:
        print()
        print(f"  结构不一致的表 {len(report.diffs)} 张：")
        for diff in report.diffs:
            print(f"        ✗ {diff.table}")
            _print_group("真库独有（<= 应以迁移文件为准）", diff.only_live, order)
            _print_group("文件独有（=> 迁移文件这样定义）", diff.only_file, order)
    elif report.consistent:
        print(f"  全部 {report.compared} 张公共表结构与迁移文件一致。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="真库形状 vs 迁移文件 逐表比对（migrate verify 的盲区补丁）"
    )
    parser.add_argument("--target", default=os.environ.get("MYSQL_DATABASE", "fpa"),
                        help="要比对的目标库（默认 fpa，**只读**）")
    parser.add_argument("--temp", default="fpa_schema_parity_probe",
                        help="用于从零跑迁移的临时库（用完删除）")
    parser.add_argument("--only", action="append", default=None,
                        help="只比指定的表（可重复；默认全部）")
    parser.add_argument("--keep", action="store_true", help="保留临时库（排查用）")
    parser.add_argument("--verbose", "-v", action="store_true", help="列出每张一致的表")
    parser.add_argument("--host", default=os.environ.get("MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MYSQL_PORT", "3306")))
    parser.add_argument("--admin-user", default=os.environ.get("MYSQL_ROOT_USER", "root"),
                        help="只用于建/删临时库；目标库永不被写入")
    args = parser.parse_args(argv)

    if args.temp == args.target:
        print(f"拒绝执行：--temp 不能等于 --target（都是 {args.temp}）")
        return EXIT_UNPROVEN
    if not os.environ.get("MYSQL_ROOT_PASSWORD"):
        print("拒绝执行：建/删临时库需要 MYSQL_ROOT_PASSWORD（应用账号无权建库）")
        return EXIT_UNPROVEN

    only = set(args.only) if args.only else None

    temp_connection = None
    try:
        temp_connection, built_tables = build_temp(args)
    except Exception as error:  # noqa: BLE001
        print(f"未能证明一致：临时库建不起来 {type(error).__name__}: {error}")
        if not args.keep:
            try:
                drop_temp(args)
            except Exception:  # noqa: BLE001
                pass
        return EXIT_UNPROVEN

    try:
        target = app_connect(args.target, args)
        try:
            report = compare(target, temp_connection, only)
        finally:
            target.close()
    except Exception as error:  # noqa: BLE001
        print(f"未能证明一致：比对过程出错 {type(error).__name__}: {error}")
        return EXIT_UNPROVEN
    finally:
        temp_connection.close()
        if args.keep:
            print(f"（--keep：临时库 {args.temp} 已保留）")
        else:
            try:
                drop_temp(args)
            except Exception as error:  # noqa: BLE001
                print(f"WARN  临时库删除失败：{type(error).__name__}: {error}")

    print_report(report, args, built_tables)

    print()
    if built_tables < MIN_TABLES:
        print(f"未能证明一致：临时库只建成 {built_tables} 张表（阈值 {MIN_TABLES}）"
              "—— 迁移没跑起来时『一致』是假绿。")
        return EXIT_UNPROVEN
    if report.compared == 0:
        print("未能证明一致：没有任何表被真正比对（空集不能算一致）。")
        return EXIT_UNPROVEN
    if report.only_temp:
        print(f"发现差异：迁移文件建出 {len(report.only_temp)} 张真库没有的表。")
        return EXIT_DIFF
    if report.diffs:
        print(f"发现差异：{len(report.diffs)} 张表结构与迁移文件不一致。")
        return EXIT_DIFF
    if report.only_target:
        unknown = [name for name in report.only_target if name not in TOOL_OWNED_TABLES]
        if unknown:
            print(f"发现差异（需人工裁决）：真库有 {len(unknown)} 张表不在迁移文件里：{unknown}")
            return EXIT_DIFF
        print(f"一致：{report.compared} 张公共表结构与迁移文件完全相同"
              f"（另有 {len(report.only_target)} 张 e2e 工具自建的表，非迁移职责）。")
        return EXIT_OK
    print(f"一致：{report.compared} 张表结构与迁移文件完全相同。")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
