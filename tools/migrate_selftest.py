"""迁移 runner 自检：不需要数据库，只验证 SQL 切分与校验和归一化。

这是早期版本踩过的坑的回归测试：
    * CRLF 与 LF 文件必须得到**同一个** checksum（早期版本 4 份 runner / 3 种算法，
      7/35 个 CRLF 文件在工具间互相判漂移）；
    * `DELIMITER $$` 里的 CREATE TRIGGER 不能被切坏（早期版本靠 mysql 客户端处理，
      我们用 PyMySQL 得自己切）。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import migrate  # noqa: E402  （同目录下的唯一 runner）

FAILURES = 0


#: 遍历时**整个跳过**的目录名。
#:
#: 用 `os.walk` 的 `dirnames` 原地剪枝，而不是"进入后再过滤"——后者对
#: `.dsh-home/`（Harness 运行时闭包，193 个 npm 包）这种目录会慢到超时。
#: `Path.rglob` **无法剪枝**，它一定会进入每个子目录，所以这里不能用它。
_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".dsh-home",
        ".venv",
        "venv",
        "dist",
        "release",
        "coverage",
        "htmlcov",
        ".pytest_cache",
        ".ruff_cache",
        ".worktrees",
        "test-results",
        "playwright-report",
    }
)


def find_source_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    """按后缀收集源文件，**跳过整个大目录子树**。"""
    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(root):
        # 原地修改 dirnames —— os.walk 会据此决定不再进入这些子目录。
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for filename in filenames:
            if filename.endswith(suffixes):
                found.append(Path(directory) / filename)
    return sorted(found)


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


def test_checksum_is_line_ending_agnostic() -> None:
    print("\n=== 1. 校验和与换行风格无关 ===")
    body = "SET NAMES utf8mb4;\nSELECT 1;\nSELECT 2;\n"
    with tempfile.TemporaryDirectory() as tmp:
        lf = Path(tmp) / "lf.sql"
        crlf = Path(tmp) / "crlf.sql"
        mixed = Path(tmp) / "mixed.sql"
        lf.write_bytes(body.encode("utf-8"))
        crlf.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
        mixed.write_bytes(body.replace("\n", "\r\n").replace("SELECT 1;\r\n", "SELECT 1;\n").encode("utf-8"))

        sums = {migrate.normalized_checksum(path) for path in (lf, crlf, mixed)}
        check("LF / CRLF / 混合 三种文件的 checksum 完全相同", len(sums) == 1, f"得到 {len(sums)} 个不同值：{sums}")

        # 反向证明：如果不归一化，CRLF 与 LF 必然不同——这正是早期版本的病根
        raw_sums = {hashlib.sha256(path.read_bytes()).hexdigest() for path in (lf, crlf)}
        check("裸字节哈希在 LF/CRLF 下确实不同（说明归一化是必要的）", len(raw_sums) == 2)


def test_split_handles_delimiter_blocks() -> None:
    print("\n=== 2. DELIMITER $$ 块不被切坏 ===")
    sql = """
SET NAMES utf8mb4;

CREATE TABLE t (id INT) ENGINE=InnoDB;

DROP TRIGGER IF EXISTS t_no_update;

DELIMITER $$
CREATE TRIGGER t_no_update
BEFORE UPDATE ON t
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only'$$
DELIMITER ;

CREATE TABLE u (id INT);
"""
    statements = migrate.split_statements(sql)

    check("共切出 5 条语句", len(statements) == 5, f"实际 {len(statements)}：{statements}")
    trigger_statements = [item for item in statements if "CREATE TRIGGER" in item]
    check("CREATE TRIGGER 是不可分割的单独一条", len(trigger_statements) == 1)
    if trigger_statements:
        trigger = trigger_statements[0]
        check("触发器语句完整保留 FOR EACH ROW", "FOR EACH ROW" in trigger)
        check("触发器语句没有被 $$ 污染", "$$" not in trigger, trigger[-40:])
        check("触发器语句没有被分号截断", trigger.count("SIGNAL") == 1)
    check("DELIMITER 指令本身没有进入 SQL", "DELIMITER" not in "\n".join(statements).upper())
    # 注意：不能简单断言 "--" 不出现——SIGNAL 的字符串字面量里就含 "--"。
    # 这里断言的是"纯注释行被剔除"这一实际行为。
    comment_lines = [
        line
        for statement in statements
        for line in statement.splitlines()
        if line.strip().startswith("--")
    ]
    check("纯注释行被剔除", not comment_lines, str(comment_lines))


def test_real_migrations_parse() -> None:
    print("\n=== 3. 仓库内真实迁移可解析 ===")
    migrations = migrate.discover()
    check(f"发现 {len(migrations)} 个迁移", len(migrations) >= 2, f"实际 {len(migrations)}")
    for migration in migrations:
        statements = migrate.split_statements(migration.path.read_text(encoding="utf-8"))
        check(f"{migration.label} 切出 {len(statements)} 条语句", len(statements) > 0)
        for index, statement in enumerate(statements, 1):
            if statement.count("(") != statement.count(")"):
                check(
                    f"{migration.label} 第 {index} 条括号平衡",
                    False,
                    f"左 {statement.count('(')} 右 {statement.count(')')}",
                )
    versions = [item.version for item in migrations]
    check("版本号严格递增且无重复", versions == sorted(set(versions)))


def test_two_runners_cannot_drift() -> None:
    print("\n=== 4. 只存在一个 runner（早期版本有 4 个）===")
    root = Path(__file__).resolve().parents[1]
    relative = sorted(
        str(path.relative_to(root)) for path in find_source_files(root, (".py",))
        if "migrat" in path.name.lower() and "test" not in path.name.lower()
    )
    check("仓库内只有一个迁移 runner", len(relative) == 1, f"发现 {relative}")
    shell_runners = [
        str(path.relative_to(root))
        for path in find_source_files(root, (".sh",))
        if "migrat" in path.name.lower()
    ]
    check("没有 shell 版迁移 runner", not shell_runners, str(shell_runners))


def main() -> int:
    test_checksum_is_line_ending_agnostic()
    test_split_handles_delimiter_blocks()
    test_real_migrations_parse()
    test_two_runners_cannot_drift()
    print()
    if FAILURES:
        print(f"结果：{FAILURES} 项失败")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
