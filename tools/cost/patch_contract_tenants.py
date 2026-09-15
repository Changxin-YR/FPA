"""按 评审结论强化 §2 里 `record_fact` 的第 2 条（调用方必须自带分租键）。

只改这一处文本，不改代码 —— 负责人 明确要求："内核那个 resolve_tenant_keys 还没落地时，
先按裁决把 §2 措辞写好，并在代码里保留现状 + 一行 TODO 指向它；不要为了让契约自洽
而先改成调用一个还不存在的函数。"

（另：本脚本第一版把中文双引号写成了 ASCII 双引号，导致自身语法错误 —— 与
`prove_quote_nesting_is_caught.py` 里记录的是同一个坑，这次用「」规避。）
"""

from __future__ import annotations

import pathlib

CONTRACT = pathlib.Path(__file__).resolve().parents[2] / "docs" / "ROLLOUT_CONTRACT.md"

OLD = (
    "2. **分租键由调用方传，`record_fact` 不回查任何别人的表**（正是本节 §2 的要求）。\n"
    "   调用方的业务表自带这三列（Q6 裁决要求补分租列），所以这条可满足——\n"
    "   **不要只传 `batch_id` 期望 cost 去回查**。\n"
)

NEW = (
    "2. **分租键必须由调用方提供，且调用方必须能从「自己的业务对象」上拿到它们。**\n"
    "   `record_fact` **不回查任何别人的表**（正是本节 §2 的要求）。\n"
    "\n"
    "   这条之所以可满足，是因为 Q6 裁决已要求所有业务表补齐分租列。**已核实自带\n"
    "   `organization_id` / `farm_id` / `area_id` 的表**（调用方直接从手上那一行取值即可，\n"
    "   无需额外查询）：`feedings` / `production_batches`（production 域）、\n"
    "   `inventory_ledger` / `inventory_lots` / `warehouses`（warehouse 域）、\n"
    "   `cost_entries`（cost 域自身）。\n"
    "\n"
    "   **调用方不得只传 `batch_id` / `pond_id` 就期望 cost 去回查** —— 那会迫使 cost\n"
    "   直读别人的表（违反 §2）。若某个调用方手上确实没有这三列，那是**它的表缺分租列**\n"
    "   （Q6 的施工缺陷），应当在它自己那里补，而不是让 cost 替它绕路。\n"
)


def main() -> int:
    text = CONTRACT.read_text(encoding="utf-8")
    count = text.count(OLD)
    print(f"锚点命中 {count} 次（期望 1）")
    if count != 1:
        print("未精确命中，放弃（避免改错位置）")
        return 1
    CONTRACT.write_text(text.replace(OLD, NEW, 1), encoding="utf-8", newline="\n")
    print("已写入 §2 的 record_fact 第 2 条强化版")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
