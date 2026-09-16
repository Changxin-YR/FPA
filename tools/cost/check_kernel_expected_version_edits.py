"""核对我在共享内核文件里的两处修改是否仍然在位。

背景：`kernel/invariants.py` 与 `kernel/capability.py` 由 invariant-kernel 拥有，
而我在排查成本域 e2e 时改过它们（两处都是影响全部域的缺陷）。文件会被对方继续编辑，
所以需要一个显式核对，避免"我以为修好了、其实已被覆盖"。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))


def main() -> int:
    invariants = (BACKEND / "yuxin" / "kernel" / "invariants.py").read_text(encoding="utf-8")
    capability = (BACKEND / "yuxin" / "kernel" / "capability.py").read_text(encoding="utf-8")

    checks = [
        (
            "invariants: `_EXCLUDE_ID_KEY` 已定义",
            "_EXCLUDE_ID_KEY = " in invariants,
            "自排除机制依赖这个键；缺失则第一次合法写入会被自己的不变量拒绝",
        ),
        (
            "invariants: `_is_self_match()` 已实现",
            "def _is_self_match(" in invariants,
            "同上",
        ),
        (
            "invariants: NoOverlappingSource 调用了自排除",
            "_is_self_match(payload, row)" in invariants[
                invariants.find("class NoOverlappingSource"):
                invariants.find("class HarvestQuantityMatch")
            ],
            "没有它，cost.entry.create 的第一次写入会被自己挡住",
        ),
        (
            "capability: `expected_version` 不在 `_FRAMEWORK_ARGS` 里",
            "expected_version" not in _framework_args(capability),
            "否则所有 action 能力都无法声明乐观锁（TypeError: missing positional argument）",
        ),
        (
            "capability: validate_payload 不再无条件放行 expected_version",
            "set(payload) - set(specs) - " not in capability,
            "否则 expected_version 既不被收进字段表、也不被拒绝、也不传给处理器",
        ),
    ]

    failures = 0
    for label, ok, why in checks:
        if ok:
            print(f"  PASS  {label}")
        else:
            failures += 1
            print(f"  FAIL  {label}\n        影响：{why}")

    # 行为层面的复核：比读代码更硬。
    print("\n=== 行为复核（不读代码，直接跑）===")
    from yuxin.bootstrap import load_all

    registry = load_all()
    confirm = registry.find("cost.entry.confirm")
    if confirm is None:
        failures += 1
        print("  FAIL  cost.entry.confirm 未注册")
    else:
        keys = list(confirm.fields.fields)
        if "expected_version" in keys:
            print(f"  PASS  cost.entry.confirm 的字段含 expected_version：{keys}")
        else:
            failures += 1
            print(f"  FAIL  cost.entry.confirm 的字段缺 expected_version：{keys}")

    from yuxin.kernel import invariants as inv

    if hasattr(inv, "NoOverlappingSource") and hasattr(inv, "_is_self_match"):
        payload = {inv._EXCLUDE_ID_KEY: 7}
        if inv._is_self_match(payload, {"id": 7}) and not inv._is_self_match(payload, {"id": 8}):
            print("  PASS  _is_self_match 的判定正确（自己=True，他人=False）")
        else:
            failures += 1
            print("  FAIL  _is_self_match 的判定错误")
    else:
        failures += 1
        print("  FAIL  内核缺少自排除所需的名字")

    print()
    if failures:
        print(f"结果：{failures} 项不在位（共享内核文件可能已被覆盖，需要重新上报）")
        return 1
    print("结果：两处修改都在位")
    return 0


def _framework_args(source: str) -> str:
    marker = "_FRAMEWORK_ARGS = frozenset("
    start = source.find(marker)
    if start < 0:
        return ""
    return source[start:source.find(")", start)]


if __name__ == "__main__":
    raise SystemExit(main())
