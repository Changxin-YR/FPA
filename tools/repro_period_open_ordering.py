"""`PeriodOpen` 在"关账"这类能力上必然自相矛盾的**可独立复现脚本**。

给 复核人 用于 t21（修订 registry §4 #7 与 Q16 的表述）。这个脚本**不依赖 cost 域**，
也不依赖任何服务代码——它只演示执行器的执行顺序与 `PeriodOpen` 的判定方式之间
为什么会冲突。任何人可以在一分钟内跑出同样的结论。

用法：
    python tools/repro_period_open_ordering.py

它做三件事（全部在**临时库**里，用后即删，不碰 yuxin）：
  1. 建一张最小的 `accounting_periods`，期间为 open；
  2. 模拟"关账动作"：先在**同一事务内** `UPDATE ... SET status='closed'`，
     再在**同一个事务内**调 `PeriodOpen.check()`——这正是执行器的顺序
     （`runner._invoke_once`: `_call_service()` -> `run_invariants()`）；
  3. 打印判定结果：必然为"已关账，拒绝"，即**关账永远做不成**。

对照实验：把顺序反过来（先 check 再 update）就通过——说明问题只在顺序上，
不在期间数据上。这条对照很关键：它排除了"是数据/配置不对"的误判。

## 结论已定稿（读这一段，不必再问"那怎么办"）

这条冲突**不会被"修好"**，因为它不是缺陷而是**范围划分**：`cost.period.close`
（"开锁"动作）不在 §4 #7 的能力清单里（清单只列**写账目**的能力）。关账自身的
期间约束由三处更强的保证承担——服务层 `_require_open_period` 预检、
`UPDATE ... WHERE status='open'` 的原子占位、DB CHECK。裁决原文见
`docs/CAPABILITY_REGISTRY.md` §4 #7 的"规则方向与适用范围"与 `ARCHIVED_DECISIONS.md` R4。
运行期事实：`cost/capabilities.py` 的 `cost.period.close` **刻意不挂** `PeriodOpen`，
连同"为什么不挂"的理由都写在声明处。本脚本因此是一条**反证**（"挂上去会坏"），
不是一张待办。

## `PeriodOpen.check()` 现在必须给租户键（t2 之后）

两处 `check()` 都要带 `organization_id`：会计期间每个企业各有一份期间行，
内核缺租户键时**显式报错**（既不跨企业查，也不静默跳过）。本脚本是单企业数据，
`organization_id=1` 即可。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pymysql  # noqa: E402

from yuxin.kernel.errors import DomainError  # noqa: E402
from yuxin.kernel.invariants import PeriodOpen  # noqa: E402
from yuxin.kernel.scope import Scope  # noqa: E402
from yuxin.kernel.uow import ConnectionConfig, UnitOfWork  # noqa: E402

PROBE = "yuxin_periodopen_repro"


def config(database: str) -> ConnectionConfig:
    return ConnectionConfig(
        host="127.0.0.1", port=3306, user="root", password="1234", database=database
    )


def main() -> int:
    root = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="1234",
                           charset="utf8mb4", autocommit=True)
    with root.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {PROBE}")
        cur.execute(f"CREATE DATABASE {PROBE} DEFAULT CHARSET utf8mb4")

    conn = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="1234",
                           database=PROBE, charset="utf8mb4", autocommit=True)
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE accounting_periods ("
            " id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,"
            " organization_id BIGINT UNSIGNED NOT NULL,"
            " period CHAR(7) NOT NULL,"
            " period_start DATE NOT NULL,"
            " period_end DATE NOT NULL,"
            " status VARCHAR(32) NOT NULL DEFAULT 'open')"
        )
        cur.execute(
            "INSERT INTO accounting_periods (organization_id, period, period_start,"
            " period_end, status) VALUES (1,'2027-01','2027-01-01','2027-01-31','open')"
        )
    conn.close()

    invariant = PeriodOpen(date_field="occurred_on")
    scope = Scope.all_data(user_id=1)
    target_day = date(2027, 1, 1)   # 关账动作的"发生日" = 该期间的起点

    print("=" * 72)
    print("对照实验：先 check 再 update（= 前置条件式调用）")
    print("=" * 72)
    uow = UnitOfWork(config(PROBE))
    with uow.begin() as tx:
        try:
            invariant.check(tx=tx, scope=scope, actor_id=1,
                            payload={"occurred_on": target_day, "organization_id": 1},
                            before=None)
            print("  PeriodOpen.check -> 通过（期间是 open，符合预期）")
        except DomainError as exc:
            print(f"  PeriodOpen.check -> 拒绝：{exc.message}")
        tx.execute("UPDATE accounting_periods SET status='closed' WHERE period='2027-01'")
        print("  然后执行关账 UPDATE -> 成功")
    print("  结论：这个顺序下**一切正常**。\n")

    print("=" * 72)
    print("真实执行器的顺序：先 update（_call_service）再 check（run_invariants）")
    print("=" * 72)
    uow = UnitOfWork(config(PROBE))
    with uow.begin() as tx:
        # 步骤 1：恢复为 open，模拟"本次要关的这个期间现在是开的"
        tx.execute("UPDATE accounting_periods SET status='open' WHERE period='2027-01'")
        print("  步骤 1：期间恢复为 open（本次要关的对象现在是开的）")

        # 步骤 2：业务写入 = 关账本身（这正是 runner._call_service 的位置）
        tx.execute("UPDATE accounting_periods SET status='closed' WHERE period='2027-01'")
        print("  步骤 2：业务写入（关账）-> 期间变为 closed")

        # 步骤 3：不变量（这正是 runner.run_invariants 的位置）
        try:
            invariant.check(tx=tx, scope=scope, actor_id=1,
                            payload={"occurred_on": target_day, "organization_id": 1},
                            before=None)
            print("  步骤 3：PeriodOpen.check -> 通过")
            print("\n  ★ 通过 = 本能力可以挂 PeriodOpen（但见模块头的「范围划分」）。")
        except DomainError as exc:
            print(f"  步骤 3：PeriodOpen.check -> 拒绝：{exc.message}")
            print(f"           错误码={exc.code}  data={exc.data}")
            print()
            print("  ★ 冲突复现：关账动作一旦挂上 PeriodOpen，")
            print("    不变量查到的是**它自己刚关掉的那个期间**，必然判定失败 ——")
            print("    也就是说**关账永远无法成功**。这就是 §4 #7 把"
                  "『开锁动作』排除在清单之外的原因。")
            print()
            print("  这不是数据或配置问题：上一次对照实验证明了同一条规则")
            print("  在'先 check 后 update'的顺序下完全正常。**问题只在执行顺序。**")
    uow = UnitOfWork(config(PROBE))
    with uow.begin() as tx:
        tx.execute("UPDATE accounting_periods SET status='open' WHERE period='2027-01'")

    with root.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {PROBE}")
    root.close()
    print(f"\n（临时库 {PROBE} 已删除；未触碰 yuxin）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
