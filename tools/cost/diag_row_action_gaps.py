"""把 `test_capability_transitions_reference_known_capabilities` 的失败内容打全。

pytest 的断言摘要在终端里被截断，看不到"哪条转移引用了哪个不存在的能力"。
本脚本复现该测试的判据并逐条列出，同时指出**是不是 cost 域的问题**。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import importlib  # noqa: E402

from yuxin.bootstrap import _module_name, discover_domains  # noqa: E402
from yuxin.kernel.capability import REGISTRY  # noqa: E402
from yuxin.kernel.workflow import RESOURCES, RowAction  # noqa: E402


def main() -> int:
    for domain in discover_domains():
        try:
            importlib.import_module(_module_name(domain))
        except Exception as exc:  # noqa: BLE001
            print(f"跳过域 {domain}: {type(exc).__name__}: {exc}")

    declared = {c.name for c in REGISTRY.all()}
    legal_actions = {str(a) for a in RowAction}
    legal_suffixes = legal_actions | {"update", "create", "read"}

    print(f"注册表能力 {len(declared)} 条\n")
    problems: list[str] = []
    for resource in RESOURCES.all():
        workflow = resource.workflow
        if workflow is None:
            continue
        for transition in workflow.transitions:
            action = str(transition.action)
            if action.startswith("*."):
                suffix = action[2:]
                if suffix not in legal_suffixes:
                    problems.append(
                        f"{resource.name}: 通配 {action} 的后缀不是合法动作词"
                    )
                continue
            if action not in declared:
                problems.append(
                    f"{resource.name}: 转移 {transition.from_state}->{transition.to_state} "
                    f"引用能力 {action}（REGISTRY 里没有）"
                )

    if not problems:
        print("没有发现引用不存在能力的转移。")
        return 0

    print(f"发现 {len(problems)} 处：")
    for line in problems:
        owner = line.split(":")[0]
        mark = "  <-- cost" if owner == "cost_entry" else ""
        print(f"   {line}{mark}")

    cost_problems = [p for p in problems if p.startswith("cost_entry")]
    print(f"\n其中属于 cost 域的：{len(cost_problems)} 处")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
