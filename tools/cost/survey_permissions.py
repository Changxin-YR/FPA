"""t17 勘查：权限是怎么被解析出来的，以及 `permissions` 空表会在哪一步炸。

只读。要回答：
  1. 一个用户的权限集合从哪些表算出来（链路）；
  2. 空 `permissions` 表会让哪一步返回空集；
  3. 是否有 `required_permission=None` 的能力（不依赖权限表就能跑）；
  4. 现有能力里 `required_permission` 的取值分布（用于设计种子）。
"""

from __future__ import annotations

import pathlib
import re
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def show_queries(path: pathlib.Path, needles: tuple[str, ...]) -> None:
    text = path.read_text(encoding="utf-8")
    print(f"=== {path.name} 里含 {needles} 的 SELECT ===")
    for match in re.finditer(r"SELECT\b[^\"]{0,400}", text):
        stmt = " ".join(match.group(0).split())
        if any(needle in stmt for needle in needles):
            print(f"  {stmt[:260]}")
    print()


def main() -> int:
    access = ROOT / "backend" / "yuxin" / "domains" / "access" / "service.py"
    show_queries(access, ("permissions", "role_permissions", "user_roles"))

    text = access.read_text(encoding="utf-8")
    for name in ("def resolve_session", "def permissions_for", "def _permissions"):
        i = text.find(name)
        if i > 0:
            print(f"=== {name} ===")
            print(text[i:i + 1400])
            print()

    print("=== 注册表里 required_permission 的分布 ===")
    import importlib

    from yuxin.bootstrap import _module_name, discover_domains
    from yuxin.kernel.capability import REGISTRY

    for domain in discover_domains():
        try:
            importlib.import_module(_module_name(domain))
        except Exception as exc:  # noqa: BLE001
            print(f"  跳过域 {domain}: {type(exc).__name__}: {exc}")

    caps = list(REGISTRY.all())
    none_caps = [c for c in caps if c.required_permission is None]
    with_perm = [c for c in caps if c.required_permission]
    print(f"  总能力 {len(caps)}；required_permission=None 的 {len(none_caps)} 条")
    for c in none_caps:
        print(f"     · {c.name}（{c.kind}）")
    print(f"  有权限码的 {len(with_perm)} 条；去重后 {len(set(c.required_permission for c in with_perm))} 个码")
    print("\n  按域统计：")
    for domain, count in sorted(Counter(c.domain for c in caps).items()):
        print(f"    {domain}: {count}")
    print("\n  权限码与能力名是否同名（registry §0.1 的规则）：")
    mismatched = [c for c in with_perm if c.required_permission != c.name]
    print(f"    同名 {len(with_perm) - len(mismatched)} / 不同名 {len(mismatched)}")
    for c in mismatched:
        print(f"     · {c.name} -> {c.required_permission}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
