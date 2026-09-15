"""`tools/schema_parity.py` 的自证测试：**故意造不一致，确认它会红**。

## 为什么必须这么测

一个"从不报红"的比对器与"比对通过"是**观测等价**的 —— 它给不出任何信息。
所以本项目的判据是：「0 处不一致」必须能自证「确实扫到了东西」，
而比对器本身必须能自证「**真的会红**」。

只测"一致时返回 0"是**假测试**：恒返回 0 的实现也能通过（本仓已被这类
假绿坑过多次，见 `docs/ARCHIVED_DECISIONS.md` R3、`DEVELOPMENT.md` 的"夹具注册表"）。

## 测什么

在一个**探针库**（不是 `fpa`！）上做三件事，每件都必须被检出：

  1. **列类型被改**（`varchar(32)` -> `varchar(64)`）—— 对应当前迭代真实漂移的形态；
  2. **CHECK 被删** —— 对应 004 那次"ENUM -> VARCHAR+CHECK"漂移；
  3. **列被改名** —— 对应 007 那次 `cancel_reason` -> `reason` 漂移。

外加两个反向断言（防止它"永远报红"）：

  4. 探针库**刚建好时**必须返回 0（否则它恒报红，同样没信息量）；
  5. 恢复后必须**再次**返回 0（证明红→绿是数据驱动的，不是一次性粘住）。

只碰探针库 `fpa_schema_parity_selftest`；跑完删除。**`fpa` 全程只读。**
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from migrate import split_statements  # noqa: E402

PROBE = "fpa_schema_parity_selftest"
TOOL = ROOT / "tools" / "schema_parity.py"
FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


def run_tool(extra: list[str]) -> tuple[int, str]:
    """在探针库上跑比对器（临时库用带前缀的名字，避免和手工运行撞车）。"""
    env = dict(os.environ)
    completed = subprocess.run(
        [sys.executable, str(TOOL), "--target", PROBE, "--temp", PROBE + "_scratch", *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(ROOT),
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def admin():
    return pymysql.connect(
        host="127.0.0.1", port=3306, user="root",
        password=os.environ.get("MYSQL_ROOT_PASSWORD", "1234"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=True,
    )


def build_probe() -> None:
    """把全部迁移应用进探针库 —— 与真库起点的形状相同。"""
    root = admin()
    with root.cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS `{PROBE}`")
        cursor.execute(
            f"CREATE DATABASE `{PROBE}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        cursor.execute(f"GRANT ALL PRIVILEGES ON `{PROBE}`.* TO 'fpa'@'%'")
        cursor.execute("FLUSH PRIVILEGES")
    root.close()

    connection = pymysql.connect(
        host="127.0.0.1", port=3306, user="fpa", password="fpa_dev_password",
        database=PROBE, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )
    from migrate import MIGRATIONS_DIR

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        with connection.cursor() as cursor:
            for statement in split_statements(path.read_text(encoding="utf-8")):
                cursor.execute(statement)
        connection.commit()
    connection.close()


def probe_execute(statements: tuple[str, ...]) -> None:
    connection = pymysql.connect(
        host="127.0.0.1", port=3306, user="fpa", password="fpa_dev_password",
        database=PROBE, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        connection.commit()
    finally:
        connection.close()


def drop_probe() -> None:
    root = admin()
    with root.cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS `{PROBE}`")
        cursor.execute(f"DROP DATABASE IF EXISTS `{PROBE}_scratch`")
    root.close()


def main() -> int:
    print("=== 0. 建探针库（形状 = 迁移文件的形状）===")
    build_probe()
    print(f"        已建 {PROBE}")

    print()
    print("=== 1. 反向断言：起点必须是一致（否则它恒报红，同样没信息量）===")
    code, output = run_tool([])
    check("起点返回 0", code == 0, f"exit={code}\n{output[-600:]}")
    check("起点报告了扫描规模（不是空跑）",
          "累计比对结构行" in output and "临时库按迁移文件建成表数 : 40" in output,
          output[-400:])

    print()
    print("=== 2. 列类型被改：必须红（对应当前迭代 varchar(32) 漂移的形态）===")
    probe_execute((
        "ALTER TABLE cost_entries MODIFY COLUMN status VARCHAR(64) NOT NULL DEFAULT 'draft'",
    ))
    code, output = run_tool(["--only", "cost_entries"])
    check("列类型差异 -> exit 1", code == 1, f"exit={code}")
    check("差异清单点名了 cost_entries 与那一列",
          "cost_entries" in output and "status" in output, output[-500:])

    print()
    print("=== 3. CHECK 被删：必须红（对应 004 那次 ENUM -> VARCHAR+CHECK 漂移）===")
    probe_execute((
        "ALTER TABLE cost_entries MODIFY COLUMN status VARCHAR(32) NOT NULL DEFAULT 'draft'",
        "ALTER TABLE cost_entries DROP CHECK chk_cost_entries_status",
    ))
    code, output = run_tool(["--only", "cost_entries", "--verbose"])
    check("CHECK 差异 -> exit 1", code == 1, f"exit={code}")
    check("差异清单点名了 chk_cost_entries_status",
          "chk_cost_entries_status" in output, output[-500:])

    print()
    print("=== 4. 列被改名：必须红（对应 007 那次 cancel_reason -> reason 漂移）===")
    probe_execute((
        "ALTER TABLE cost_entries ADD CONSTRAINT chk_cost_entries_status "
        "CHECK (status IN ('draft','submitted','verified','archived'))",
        "ALTER TABLE purchase_orders DROP CHECK chk_purchase_orders_reason",
        "ALTER TABLE purchase_orders RENAME COLUMN reason TO cancel_reason",
        "ALTER TABLE purchase_orders ADD CONSTRAINT chk_purchase_orders_cancel_reason "
        "CHECK (status <> 'cancelled' OR (cancel_reason IS NOT NULL AND cancel_reason <> ''))",
    ))
    code, output = run_tool(["--only", "purchase_orders"])
    check("列改名差异 -> exit 1", code == 1, f"exit={code}")
    check("差异清单同时点名 reason 与 cancel_reason",
          "reason" in output and "cancel_reason" in output, output[-500:])

    print()
    print("=== 5. 反向断言：恢复后必须再次返回 0（红->绿由数据驱动）===")
    probe_execute((
        "ALTER TABLE purchase_orders DROP CHECK chk_purchase_orders_cancel_reason",
        "ALTER TABLE purchase_orders RENAME COLUMN cancel_reason TO reason",
        "ALTER TABLE purchase_orders ADD CONSTRAINT chk_purchase_orders_reason "
        "CHECK (status <> 'cancelled' OR (reason IS NOT NULL AND reason <> ''))",
    ))
    code, output = run_tool([])
    check("恢复后返回 0", code == 0, f"exit={code}\n{output[-600:]}")

    print()
    print("=== 6. 自证闸门：临时库建不起来时必须报『未能证明一致』，而不是『一致』===")
    # 用错口令让临时库建不起来。若此时还打印"一致"，就是最危险的那种假绿：
    # 迁移根本没跑，"比对"是在拿真库跟自己比。
    env = dict(os.environ)
    env["MYSQL_ROOT_PASSWORD"] = "definitely-wrong-password"
    completed = subprocess.run(
        [sys.executable, str(TOOL), "--target", PROBE, "--temp", PROBE + "_nope"],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT),
    )
    check("临时库建不起来 -> exit 2（不是 0）", completed.returncode == 2,
          f"exit={completed.returncode}")
    check("明确说『未能证明一致』，不说『一致』",
          "未能证明一致" in (completed.stdout or ""),
          (completed.stdout or "")[-300:])
    check("退出码语义：2 != 一致，也不是 1（差异）",
          completed.returncode not in (0, 1), f"exit={completed.returncode}")

    drop_probe()
    print()
    print("全部通过" if not FAILURES else f"{FAILURES} 项失败")
    print(f"（探针库 {PROBE} 已删除；fpa 全程只读）")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
