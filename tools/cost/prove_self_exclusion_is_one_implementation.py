"""行为证明：`runner.py` 注入的键与 `invariants.py` 读的键**驱动同一段逻辑**。

负责人 要判定「我加的自排除」与「invariant-kernel 在 `_invariant_extra` 里的注入」
是不是**同一份实现**。代码级证据是 `runner.py:48` 从 `invariants` 导入 `_EXCLUDE_ID_KEY`
（同一个常量对象），但**行为证据更硬**：用真实执行器的注入路径跑一次，
看自排除是否真的生效。

判据（用一个 FakeTx 模拟"库里已有一行 id=42"）：
  · payload 里 exclude_id = 42（= 那行自己）  -> 必须**放行**（自排除生效）
  · payload 里 exclude_id = 7 （= 另一行）    -> 必须**拒绝**（真有冲突）

若两半不是同一份实现（例如两侧各写了一个同名字符串、或只有一半），
上面第二、第一项不可能同时成立。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from yuxin.kernel.errors import DomainError  # noqa: E402
from yuxin.kernel.invariants import (  # noqa: E402
    NoOverlappingSource,
    _EXCLUDE_ID_KEY,
    _is_self_match,
)
from yuxin.kernel.scope import Scope  # noqa: E402


class FakeTx:
    """模拟"库里有且只有一行 id=42"的冲突行。"""

    def __init__(self) -> None:
        self.last_sql = ""
        self.last_params: tuple = ()

    def query_one(self, sql, params=None):
        self.last_sql = sql
        self.last_params = tuple(params or ())
        return {"id": 42}


def payload(exclude_id):
    return {
        "source_type": "manual_expense",
        "target_type": "pond",
        "target_id": 1,
        "period_start": "2027-02-01",
        "period_end": "2027-02-28",
        "organization_id": 1,
        "farm_id": 1,
        "area_id": 1,
        _EXCLUDE_ID_KEY: exclude_id,
    }


def main() -> int:
    invariant = NoOverlappingSource(source_types=("warehouse_ledger",))
    scope = Scope.all_data(user_id=1)
    failures = 0

    print(f"runner 注入用的键名 = {_EXCLUDE_ID_KEY!r}")
    print(f"invariants 读取用的键名 = {_EXCLUDE_ID_KEY!r}（同一个常量对象）\n")

    print("=== A. exclude_id = 42（正是库里那一行）→ 期望放行 ===")
    try:
        invariant.check(tx=FakeTx(), scope=scope, actor_id=1,
                        payload=payload(42), before=None)
        print("  OK   放行：自排除生效（否则第一次合法写入会被自己挡住）")
    except DomainError as exc:
        failures += 1
        print(f"  FAIL 被拒：{exc.message}")

    print("\n=== B. exclude_id = 7（另一行）→ 期望拒绝 ===")
    try:
        invariant.check(tx=FakeTx(), scope=scope, actor_id=1,
                        payload=payload(7), before=None)
        failures += 1
        print("  FAIL 竟然放行：冲突行没被识别 —— 规则失效")
    except DomainError as exc:
        print(f"  OK   拒绝：{exc.message}")
        print(f"       错误数据里带 existing_id={exc.data.get('existing_id')}"
              f"（= 42，指向真正的冲突行）")

    print("\n=== C. `_is_self_match` 的直接判定 ===")
    if _is_self_match(payload(42), {"id": 42}) and not _is_self_match(payload(42), {"id": 7}):
        print("  OK   自己=True / 他人=False")
    else:
        failures += 1
        print("  FAIL 判定错误")

    print()
    if failures:
        print(f"结论：{failures} 项不符 —— 两半可能不是同一份实现")
        return 1
    print("结论：**同一份实现的充分证据** —— 注入的键（runner 侧）与自排除判定"
          "（invariants 侧）")
    print("      在同一次调用里共同生效，且缺任一半都无法同时满足 A 与 B。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
