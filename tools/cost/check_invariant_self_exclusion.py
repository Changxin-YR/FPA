"""回答 负责人 的归口问题：`_EXCLUDE_ID_KEY` / 自排除**是一份实现还是两份**。

要证明三件事：
  A. `invariants.py` 里的 `_is_self_match` 与 `_EXCLUDE_ID_KEY` 定义存在；
  B. `runner.py::_invariant_extra` 是否注入该键（invariant-kernel 说它做了这件事）；
  C. 两处是否**指向同一个键名常量**（不是各自定义了一个同名字符串）。

C 是关键：如果 runner 注入的键名与 invariants 读的键名来自**同一个常量对象**，
那就是一份实现；如果是两处各自写死的字符串字面量，那才是"两份"。
"""

from __future__ import annotations

import pathlib
import re

BACKEND = pathlib.Path(__file__).resolve().parents[2] / "backend"
INV = BACKEND / "fpa" / "kernel" / "invariants.py"
RUN = BACKEND / "fpa" / "kernel" / "runner.py"


def main() -> int:
    inv = INV.read_text(encoding="utf-8")
    run = RUN.read_text(encoding="utf-8")

    print("=== A. invariants.py ===")
    m = re.search(r"^(_EXCLUDE_ID_KEY\s*=\s*.+)$", inv, re.M)
    print(f"  定义：{m.group(1) if m else '（未找到）'}")
    print(f"  _is_self_match 定义：{'def _is_self_match(' in inv}")
    for name in ("NoOverlappingSource", "AtMostOnePending"):
        i = inv.find("class " + name)
        j = inv.find("\nclass ", i + 10)
        seg = inv[i:j if j > 0 else len(inv)]
        print(f"  {name}:")
        print(f"      调用自排除      : {'_is_self_match(payload, row)' in seg}")
        print(f"      SQL 下推 id<>   : {'id <> %s' in seg}")

    print("\n=== B. runner.py ===")
    print(f"  是否出现 _EXCLUDE_ID_KEY：{'_EXCLUDE_ID_KEY' in run}")
    for line in run.split("\n"):
        if "_EXCLUDE_ID_KEY" in line:
            print(f"      {line.strip()}")

    print("\n=== C. 键名是否同一个来源（这是'一份 vs 两份'的判据）===")
    imports_exclude = [
        line.strip()
        for line in run.split("\n")
        if "import" in line and "_EXCLUDE_ID_KEY" in line
    ]
    print(f"  runner 是否从 invariants 导入该常量：{imports_exclude or '（未见导入）'}")
    if imports_exclude:
        print("  -> 若是从 invariants 导入，则两边用的是**同一个常量对象**，是一份实现。")
    else:
        print("  -> 未导入：需确认 runner 是否自己写了字符串字面量（那才是两份）。")

    print("\n=== D. 运行时验证：常量对象是否同一 ===")
    import sys

    sys.path.insert(0, str(BACKEND))
    from fpa.kernel import invariants as inv_mod

    print(f"  invariants._EXCLUDE_ID_KEY = {inv_mod._EXCLUDE_ID_KEY!r}")
    runner_src_has_key = "_EXCLUDE_ID_KEY" in run
    print(f"  runner.py 文本引用该名字：{runner_src_has_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
