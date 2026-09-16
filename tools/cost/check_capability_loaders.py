"""核对 cost 域满足 ROLLOUT_CONTRACT §3 的新 `loader=` 契约。

t15 把回读函数从"模块级手工挂 `__yuxin_load_by_id__`"改成了 `Capability.loader=` 声明。
本脚本检查 cost 域是否两种形态都合规：
  * 写能力：必须能解析出 loader（`loader=` 字段 或 `__yuxin_load_by_id__`）；
  * 读能力：**不得**声明 loader（读能力不写库，声明它是自相矛盾的）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from yuxin.bootstrap import load_all  # noqa: E402


def main() -> int:
    registry = load_all()
    failures = 0

    print("cost 域能力的 loader 声明：\n")
    for capability in registry.by_domain("cost"):
        declared = getattr(capability, "loader", None)
        attached = getattr(capability.handler, "__yuxin_load_by_id__", None)
        no_loader = bool(getattr(capability.handler, "__yuxin_no_loader__", False))
        kind = "读" if capability.is_read else "写"

        if capability.is_read:
            ok = declared is None and not no_loader
            why = "读能力不应声明 loader / NO_LOADER"
        else:
            ok = callable(declared) or callable(attached)
            why = "写能力必须能解析出回读函数"

        if not ok:
            failures += 1
        print(f"  {'OK  ' if ok else 'FAIL'} [{kind}] {capability.name:22} "
              f"loader={getattr(declared, '__qualname__', declared)!r} "
              f"attached={getattr(attached, '__qualname__', attached)!r} "
              f"no_loader={no_loader}")
        if not ok:
            print(f"        -> {why}")

    print()
    print(f"结论：{'通过' if not failures else f'{failures} 项不合规'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
