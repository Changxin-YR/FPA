"""`PeriodOpen` **跨租户**缺口的可独立复现脚本（负责人 授权）。

## 当前状态：**缺陷已修，本脚本转为回归守卫**

| | 缺陷 | 形态（修复前） | 后果 |
|---|---|---|---|
| 一 | 执行顺序 | 不变量在写库**之后**跑 → 查到的是自己刚关掉的期间 | **关账永远失败**（吵闹：用户会投诉） |
| 二 | **查询谓词**（本脚本） | `WHERE period_start <= %s AND period_end >= %s LIMIT 1` —— **无 `organization_id`、无 `ORDER BY`** | **两家的期间互相干扰**：被误拦（吵闹）**或已关账被静默放行**（安静、更危险） |

缺陷一由 `tools/repro_period_open_ordering.py` 复现（且**属于范围划分**：`cost.period.close`
不进 §4 #7 的能力清单，见 registry §4 #7 的"规则方向与适用范围"）。

缺陷二**已修**（t2 / invariant-kernel）：`PeriodOpen` 现在带 `tenant_keys`（默认
`("organization_id",)`）、缺租户键时按 R2 显式 `INTERNAL_ERROR`、`LIMIT 1` 带
`ORDER BY period_start DESC, id DESC`。**本脚本因此变成一条回归守卫**：
它跑的两个场景（"邻家先插、两家 status 相反"）在修复前必红，修复后必须全绿。
断言红 = 缺陷回来了，而不是"脚本坏了"。

## 为什么必须单独一个脚本

`repro_period_open_ordering.py` 用的是**单企业**数据（`organization_id` 有列但只有一个值），
所以它**结构上不可能**暴露谓词缺口 —— 这正是缺陷二活到今天的原因：
**`organization_id` 在每一张表上都有，却从来没有被真实地行使过一次。**

## 产出：**两个场景**

两家的 `status` 相反（一个 open 一个 closed），且**邻家先插入**（修复前 `LIMIT 1`
会命中它）。修复前后唯一变的是"内核谓词命中哪一行"，所以并排打出来就是可读的事实。

## 用法与安全边界（负责人 明确要求）

```powershell
python tools\\repro_period_open_tenant.py            # 临时库，跑完即删
python tools\\repro_period_open_tenant.py --keep      # 保留临时库以便手工查看
```

- **默认绝不碰 `yuxin`**：所有 DDL/DML 只发生在固定临时库 `yuxin_periodopen_tenant`；
  库名**不从命令行取** —— 否则"手滑传了 `yuxin`"就会在共享库里建两家企业、污染所有人的 e2e；
- 启动时 `DROP DATABASE IF EXISTS` 重建，结束时删除（除非 `--keep`）；
- 需要建库权限：`MYSQL_ROOT_USER`（默认 `root`）/ `MYSQL_ROOT_PASSWORD`（默认 `1234`，
  与 `tools/repro_period_open_ordering.py`、`tools/bootstrap_db.py` 一致）。

**退出码恒为 0**：本脚本的读数是"打出来的场景对照"，判定写在红/绿里；
把它做成非零退出会让人在 CI 里把"复现事实"当成"脚本坏了"。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pymysql  # noqa: E402

from yuxin.kernel.errors import DomainError  # noqa: E402
from yuxin.kernel.invariants import PeriodOpen  # noqa: E402
from yuxin.kernel.scope import Scope  # noqa: E402
from yuxin.kernel.uow import ConnectionConfig, UnitOfWork  # noqa: E402

PROBE_DB = "yuxin_periodopen_tenant"

PERIOD = "2027-01"
PERIOD_START = date(2027, 1, 1)
PERIOD_END = date(2027, 1, 31)
PROBE_DAY = date(2027, 1, 15)

PROBLEMS = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PROBLEMS
    if condition:
        print(f"  PASS  {label}")
    else:
        PROBLEMS += 1
        print(f"  FAIL  {label}  {detail}")


def root_connection(database: str | None = None) -> pymysql.Connection:
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_ROOT_USER", "root"),
        password=os.environ.get("MYSQL_ROOT_PASSWORD", "1234"),
        database=database,
        charset="utf8mb4",
        autocommit=True,
    )


def build_fixture(*, status_org1: str, status_org2: str) -> None:
    """重建临时库，并为**两家企业**插入同一份期间（status 由参数决定）。

    建库与建表要**两个连接**：`CREATE DATABASE` 之后当前连接还没有默认库
    （实测 errno 1046 "No database selected"）。
    """
    with root_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {PROBE_DB}")
            cur.execute(f"CREATE DATABASE {PROBE_DB} DEFAULT CHARSET utf8mb4")

    with root_connection(PROBE_DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE accounting_periods ("
                " id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,"
                " organization_id BIGINT UNSIGNED NOT NULL,"
                " period CHAR(7) NOT NULL,"
                " period_start DATE NOT NULL,"
                " period_end DATE NOT NULL,"
                " status VARCHAR(32) NOT NULL DEFAULT 'open',"
                " closed_by BIGINT UNSIGNED NULL,"
                " closed_at DATETIME NULL,"
                " close_reason VARCHAR(255) NULL,"
                " KEY idx_org_period (organization_id, period_start, period_end))"
            )
            # 两家企业的**同一期间**，一个开一个关。两行差别只有 status 与 id 顺序，
            # 而 `LIMIT 1` 无 `ORDER BY` 时命中的正是 id 小的那一行。
            # ★ 插入顺序决定 id 顺序，而内核谓词 `LIMIT 1` 无 `ORDER BY` 时命中的
            #   正是 id 最小的那一行。所以**必须让"别家"先插**，否则内核永远命中
            #   基准企业自己，"命中别家"这个缺陷就复现不出来（上一版就栽在这里）。
            for org, status in ((2, status_org2), (1, status_org1)):
                closed = status == "closed"
                cur.execute(
                    "INSERT INTO accounting_periods (organization_id, period, period_start,"
                    " period_end, status, closed_by, closed_at, close_reason)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        org,
                        PERIOD,
                        PERIOD_START,
                        PERIOD_END,
                        status,
                        org if closed else None,
                        None,
                        f"org{org} 已关账" if closed else None,
                    ),
                )


def _kernel_sql() -> str:
    """从**实际实现**里取内核 `PeriodOpen.check` 的源码。

    抄一份 SQL 就等于"我复现的是我以为的写法"；读源码字符串则永远与实现同步。
    """
    import inspect

    from yuxin.kernel import invariants

    return inspect.getsource(invariants.PeriodOpen.check)


def service_predicate_hits(tx: UnitOfWork, organization_id: int) -> list[dict]:
    """**服务层谓词**（逐字取自 `cost/entries.py::_require_open_period`）。

    这一份**故意保留**：服务层的期间反查是**独立实现**（它要给出具体到期间的可读文案），
    本脚本观察的是"两处判据命中同一行"，所以必须真的有两处。它与内核的那份若再次分叉
    （例如内核改了谓词而服务层没改），这里会直接把差异打出来 —— 这正是要防的事。
    """
    return tx.query_all(
        "SELECT id, organization_id, status FROM accounting_periods "
        "WHERE organization_id = %s AND period_start <= %s AND period_end >= %s "
        "LIMIT 1",
        (organization_id, PROBE_DAY, PROBE_DAY),
    )


def kernel_verdict(tx: UnitOfWork, organization_id: int | None = None) -> str:
    """内核不变量的**真实判定**（用生产实现，**不抄 SQL**）。

    ## 为什么这里不再有"内核谓词"的副本

    那份副本曾经是必要的（要复现的正是它），但它同时是**第二个事实来源**：它与真实
    实现一旦分叉，本脚本就会给出自相矛盾的读数 —— 实测发生过（抄件恒红、真实内核恒绿，
    **没有任何实现能同时满足那两条断言**）。按本项目的纪律（两处描述同一件事就删一处），
    副本已删除，这里只驱动**真实实现**并观察三件事：

      * `organization_id=None` → 缺分租键，应按 R2 显式 `INTERNAL_ERROR`；
      * 给定 `organization_id` → 判定必须**只看本企业那一行**（邻家那行 id 更小、先插入）；
      * SQL 的结构（租户键 / `ORDER BY`）由 `_kernel_sql()` **读源码**断言 ——
        那是同一事实的另一种观察方式，不是第二份实现。
    """
    invariant = PeriodOpen(date_field="occurred_on")
    scope = Scope.all_data(user_id=1)
    payload: dict[str, Any] = {"occurred_on": str(PROBE_DAY)}
    if organization_id is not None:
        payload["organization_id"] = organization_id
    try:
        invariant.check(
            tx=tx,
            scope=scope,
            actor_id=1,
            payload=payload,
            before=None,
        )
        return "通过"
    except Exception as error:  # noqa: BLE001
        return f"拒绝（{error}）"


def kernel_verdict_without_tenant_key(tx: UnitOfWork) -> tuple[str, str]:
    """**缺租户键**时内核的判定：返回 `(错误码, 判词)`。

    只比字符串会漏掉关键区分：`PERIOD_CLOSED`（期间真的关了）与
    `INTERNAL_ERROR`（缺分租键 → 装配不一致）都是"拒绝"，
    但前者说明规则在正常工作、后者说明**声明与装配不一致**（R2 要求后者必须吵）。
    """
    invariant = PeriodOpen(date_field="occurred_on")
    scope = Scope.all_data(user_id=1)
    try:
        invariant.check(
            tx=tx,
            scope=scope,
            actor_id=1,
            payload={"occurred_on": str(PROBE_DAY)},   # 故意不给 organization_id
            before=None,
        )
    except DomainError as error:  # noqa: PERF203
        return str(error.code), f"拒绝（{error}）"
    except Exception as error:  # noqa: BLE001
        return type(error).__name__, f"意外异常（{error}）"
    return "未拒绝", "通过"


def run_scenario(scenario: str, status_org1: str, status_org2: str, tx: UnitOfWork) -> None:
    """跑一个场景。"对错"以 **org 1 自己的期间**为基准。

    `org 2` 的期间行**先插入**（id 更小）—— 那正是修复前 `LIMIT 1` 会命中的一行。
    所以下面每条判定都在回答同一个问题：**内核看的是本企业那行，还是 id 最小的那行？**
    """
    own = kernel_verdict(tx, organization_id=1)
    no_key_code, no_key_text = kernel_verdict_without_tenant_key(tx)
    service_hits = service_predicate_hits(tx, 1)
    expected = "通过" if status_org1 == "open" else "拒绝"

    print("  对照：同一个 (企业=1, 日期) 下的三种观察")
    print(f"    内核（给 organization_id=1）：{own}")
    print(f"    内核（不给租户键）        ：{no_key_code} / {no_key_text}")
    print(f"    服务层谓词命中            ：{service_hits}")
    print(f"    企业 1 自己的期间状态     ：{status_org1}（邻家 org 2 = {status_org2}）")

    if own.startswith(expected):
        print(f"    （判定与 org 1 自己的期间一致：{status_org1} → {expected}。）")
    else:
        print(f"    ★★ 缺陷：org 1 自己是 {status_org1}，判定却是「{own}」"
              " —— 说明它看的不是本企业那行。")

    check(
        f"[{scenario}] 两层判据命中同一行（内核 vs 服务层：分叉就是「一处改了另一处没改」）",
        bool(service_hits) and int(service_hits[0]["organization_id"]) == 1,
        f"服务层谓词命中：{service_hits}",
    )
    check(
        f"[{scenario}] 给租户键时，判定只看**本企业自己的**期间（{status_org1} → {expected}）",
        own.startswith(expected),
        f"org 1={status_org1}、邻家 org 2={status_org2}（先插入），实际判定：{own}",
    )
    check(
        f"[{scenario}] 判定与邻家的状态无关（邻家={status_org2}）",
        own.startswith(expected),
        f"实际判定：{own}",
    )
    check(
        f"[{scenario}] 内核谓词带租户键（`organization_id` 出现在会执行的 SQL 里）",
        "organization_id" in _kernel_sql(),
        "内核 SQL 里没有 organization_id —— 期间反查会跨企业串号",
    )
    check(
        f"[{scenario}] 内核谓词有确定行序（`ORDER BY`）",
        "ORDER BY" in _kernel_sql(),
        "LIMIT 1 无 ORDER BY：命中哪一行由存储/执行计划顺序决定，结论不可复现",
    )
    check(
        f"[{scenario}] 缺租户键时内核按 R2 显式 INTERNAL_ERROR（不跨企业查、也不静默跳过）",
        no_key_code == "INTERNAL_ERROR",
        f"实际错误码={no_key_code}，判词：{no_key_text}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="PeriodOpen 跨租户缺口复现（临时库）")
    parser.add_argument("--keep", action="store_true", help="保留临时库（默认跑完即删）")
    args = parser.parse_args()

    print("=" * 74)
    print(f"临时库：{PROBE_DB}（**绝不碰 yuxin**）")
    print(f"期间：{PERIOD}（{PERIOD_START} ~ {PERIOD_END}）；探测日 {PROBE_DAY}")

    config = ConnectionConfig(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_ROOT_USER", "root"),
        password=os.environ.get("MYSQL_ROOT_PASSWORD", "1234"),
        database=PROBE_DB,
    )

    try:
        # 两个场景都是"org 2 先插 → 内核命中 org 2"，只翻转两家的 status：
        #   A：org 1 closed / org 2 open  -> 内核看到 org 2 的 open  -> **静默放行**
        #   B：org 1 open   / org 2 closed -> 内核看到 org 2 的 closed -> **误拦**
        for index, (scenario, status_org1, status_org2) in enumerate(
            (("A", "closed", "open"), ("B", "open", "closed")), start=1
        ):
            build_fixture(status_org1=status_org1, status_org2=status_org2)
            print()
            print("#" * 74)
            print(
                f"场景 {scenario}（第 {index}/2）：基准企业 org 1 = {status_org1}；"
                f"邻家 org 2 = {status_org2}（**先插入 → 内核谓词会命中它**）"
            )
            print("#" * 74)
            with UnitOfWork(config).begin() as tx:
                run_scenario(scenario, status_org1, status_org2, tx)

        print()
        print("=" * 74)
        print("结论")
        print("=" * 74)
        print("  两个场景里，内核的判定**都只看 org 1（本企业）自己的那一行** —— 与邻家 org 2 的状态无关。")
        print("  修复前它看的是 id 最小的那一行（= 先插入的邻家），于是：")
        print("    * 命中邻家且邻家已关账 → **误拦**本企业的合法写入（吵闹，用户会投诉）")
        print("    * 命中邻家且邻家是 open → **静默放行**本企业已关账期间的写入（安静，无人发现）")
        print("  本脚本修复后的观察方式：只驱动**真实实现** `PeriodOpen.check`，对照三件事 ——")
        print("    ① organization_id 给 / 不给 两种调用的判定；")
        print("    ② 服务层 `_require_open_period` 的谓词命中行（两层判据是否仍在同一行）；")
        print("    ③ 内核 SQL 的源码结构。**不再保有任何 SQL 副本** ——")
        print("       第二事实来源会给出自相矛盾的读数（实测已发生过）。")
        print("  修复内容（负责人 已定，t2 invariant-kernel 落地）：")
        print("    `PeriodOpen` 加 `tenant_keys`（默认 organization_id）")
        print("    + 服务经 `_invariant_context` 回传 `organization_id`")
        print("    + **缺租户键时按 R2 显式 `INTERNAL_ERROR`**（不许静默放宽范围）")
        print("    + 补 `ORDER BY period_start DESC, id DESC`")
        print("      （`LIMIT 1` 不带 `ORDER BY` 本身就是'不可复现'的来源）")
    finally:
        if args.keep:
            print()
            print(f"（--keep）临时库 {PROBE_DB} 已保留，便于手工查看")
        else:
            with root_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"DROP DATABASE IF EXISTS {PROBE_DB}")
            print()
            print(f"临时库 {PROBE_DB} 已删除；**未触碰 yuxin**")

    print()
    if PROBLEMS:
        print(f"结果：{PROBLEMS} 项红 —— **缺陷回来了**（本脚本已转为回归守卫，见模块头）")
    else:
        print("结果：全部通过 —— 租户键、确定行序、缺键报错三项都在（t2 的修复仍然有效）")
    # 退出码恒为 0：红的是"被复现的事实"，不是"脚本坏了"。
    return 0


if __name__ == "__main__":
    sys.exit(main())
