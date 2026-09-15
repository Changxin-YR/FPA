"""列出去重后的全部权限码及其来源能力 —— 这是种子的"事实来源"。

t17 的核心是"从 REGISTRY 派生"，所以先把派生结果看全：
  · 一共几个码；
  · 每个码被哪些能力使用（一对多的码是 registry §0.1 之外的现实形态）；
  · 哪些码与能力名不同名（这类必须靠派生，手写必然漏）。
"""

from __future__ import annotations

import pathlib
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    import importlib

    from fpa.bootstrap import _module_name, discover_domains
    from fpa.kernel.capability import REGISTRY

    for domain in discover_domains():
        try:
            importlib.import_module(_module_name(domain))
        except Exception as exc:  # noqa: BLE001
            print(f"跳过域 {domain}: {type(exc).__name__}: {exc}")

    by_code: dict[str, list[str]] = defaultdict(list)
    no_permission: list[str] = []
    for capability in REGISTRY.all():
        if capability.required_permission is None:
            no_permission.append(capability.name)
        else:
            by_code[capability.required_permission].append(capability.name)

    print(f"能力 {len(REGISTRY.all())} 条；权限码 {len(by_code)} 个；"
          f"无语权限码 {len(no_permission)} 条\n")

    print("=== 一个码被多条能力共用（这些必须去重）===")
    for code, owners in sorted(by_code.items()):
        if len(owners) > 1:
            print(f"  {code:28} <- {owners}")

    print("\n=== 码与能力名不同名的（手写清单必然漏这些）===")
    for code, owners in sorted(by_code.items()):
        if code not in owners:
            print(f"  {code:28} <- {owners}")

    print("\n=== 全部权限码（按字母序）===")
    for index, code in enumerate(sorted(by_code), 1):
        print(f"  {index:3}. {code}")

    print("\n=== required_permission=None 的能力 ===")
    print(f"  {no_permission or '（无）'}")

    print("\n=== 码的前缀分布（用于推断 domain 列）===")
    prefixes: dict[str, int] = defaultdict(int)
    for code in by_code:
        prefixes[code.split(".")[0]] += 1
    for prefix, count in sorted(prefixes.items()):
        print(f"  {prefix:14} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
