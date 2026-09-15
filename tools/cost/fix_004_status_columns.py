"""把 004_cost.sql 里的**记录生命周期状态列**从 ENUM 改为 VARCHAR(32) + CHECK。

## 负责人 的裁决（当前迭代）

区分两类，不一律保留、也不一律改：

* **记录生命周期状态**（`draft/submitted/verified/archived` 这种会随流程演进的）
  → **必须 `VARCHAR(32)`**。§1.2 正是为它们定的（早期版本做过 8 次
  `ALTER TABLE ... MODIFY status ENUM(...)`）。
* **真正封闭的业务字典**（取值由常量固定，如 `open/closed`）→ ENUM 可辩护，保留。

## 为什么同时加 CHECK（而不是只放宽类型）

先核了别的域**已落盘**的写法（`tools/cost/survey_status_columns.py`）：
`006_warehouse` / `007_purchase` / `008_sales` 全部是 `VARCHAR(32)` **加 CHECK**
（`chk_warehouses_status`、`chk_purchase_orders_status`、`chk_sales_orders_status` …）。

这一点很关键：**ENUM 本身是数据库层的取值强约束**。若只把它换成裸 `VARCHAR(32)`，
等于**削弱**了校验（任何字符串都能写进去）。加 CHECK 才既拿到"改状态集不用 MODIFY 列"
的演进自由，又保住原来的强校验，还与全队形态一致。

（`005_production` 是 `VARCHAR(32)` 无 CHECK——那是它与别域不一致，
不是我要照抄的形态；我不去改别人的迁移。）

## 分类结论（004 的全部 7 个 ENUM 列）

| 列 | 取值 | 分类 | 处置 |
|---|---|---|---|
| `cost_entries.status` | draft/submitted/verified/archived | **记录生命周期** | → VARCHAR(32) + CHECK |
| `cost_entries.confirm_state` | pending/confirmed | **会演进**（成本归集状态将来会加状态） | → VARCHAR(32) + CHECK |
| `cost_categories.status` | enabled/disabled | **记录生命周期**（启停是有意为之的软删除） | → VARCHAR(32) + CHECK |
| `accounting_periods.status` | open/closed | 封闭字典（由 `PERIOD_STATUSES` 固定） | **保留 ENUM** + 注释说明 |
| `cost_categories.default_nature` | direct/public | 封闭字典 | **保留 ENUM** + 注释说明 |
| `cost_entries.source_type` | manual_expense/warehouse_ledger | 封闭字典（registry §2.10 固定两值） | **保留 ENUM** + 注释说明 |
| `cost_entries.target_type` | farm/area/pond/batch | 封闭字典（registry §2.10 固定四值） | **保留 ENUM** + 注释说明 |
"""

from __future__ import annotations

import pathlib

TARGET = pathlib.Path(__file__).resolve().parents[2] / "database" / "migrations" / "004_cost.sql"

#: (旧行, 新行) —— 每一条都必须精确命中 1 次，否则整体放弃
REPLACEMENTS: list[tuple[str, str]] = [
    # --- accounting_periods ---
    (
        "  status          ENUM('open','closed') NOT NULL DEFAULT 'open',\n",
        "  -- 【封闭字典，非记录生命周期】取值由 kernel/invariants.PERIOD_STATUSES 固定\n"
        "  -- (open/closed)，不随业务流程演进 —— 按 ROLLOUT_CONTRACT §1.2 的裁决\n"
        "  -- （负责人：真正封闭的字典 ENUM 可辩护），这里**保留 ENUM**。\n"
        "  status          ENUM('open','closed') NOT NULL DEFAULT 'open',\n",
    ),
    # --- cost_categories ---
    (
        "  default_nature  ENUM('direct','public') NOT NULL DEFAULT 'direct',\n",
        "  -- 【封闭字典】直接成本 / 公共成本，两值由会计准则决定，不随流程演进。保留 ENUM。\n"
        "  default_nature  ENUM('direct','public') NOT NULL DEFAULT 'direct',\n",
    ),
    (
        "  status          ENUM('enabled','disabled') NOT NULL DEFAULT 'enabled',\n",
        "  -- 【记录生命周期】类别停用是有意为之的软删除，将来会出现\n"
        "  -- 'deprecated'/'merged' 之类的状态 -> 按 §1.2 用 VARCHAR(32)。\n"
        "  -- CHECK 保住 ENUM 原有强度：改状态集时只需换 CHECK，不必 MODIFY 整列\n"
        "  -- （早期版本为此做过 8 次 ALTER TABLE ... MODIFY status ENUM(...)）。\n"
        "  status          VARCHAR(32)     NOT NULL DEFAULT 'enabled',\n",
    ),
    # --- cost_entries ---
    (
        "  source_type      ENUM('manual_expense','warehouse_ledger') NOT NULL DEFAULT 'manual_expense',\n",
        "  -- 【封闭字典】registry §2.10 明确只留这两个值（asset_depreciation/adjustment\n"
        "  -- 随资产与调整单一起砍除）。保留 ENUM。\n"
        "  source_type      ENUM('manual_expense','warehouse_ledger') NOT NULL DEFAULT 'manual_expense',\n",
    ),
    (
        "  target_type      ENUM('farm','area','pond','batch') NULL,\n",
        "  -- 【封闭字典】registry §2.10 固定这四个归属层级。保留 ENUM。\n"
        "  target_type      ENUM('farm','area','pond','batch') NULL,\n",
    ),
    (
        "  confirm_state    ENUM('pending','confirmed') NOT NULL DEFAULT 'pending',\n",
        "  -- 【记录生命周期】成本归集状态会演进（例如将来加 'reopened'/'reversed'），\n"
        "  -- 按 §1.2 用 VARCHAR(32) —— 这正是 负责人 点名的第一类。\n"
        "  confirm_state    VARCHAR(32)     NOT NULL DEFAULT 'pending',\n",
    ),
    (
        "  status           ENUM('draft','submitted','verified','archived') NOT NULL DEFAULT 'draft',\n",
        "  -- 【记录生命周期】负责人 原话：'cost_entries.status 若也是 ENUM，那它属于第一类\n"
        "  -- ——它一定会演进。' 它确实曾是 ENUM，故按 §1.2 改为 VARCHAR(32)。\n"
        "  status           VARCHAR(32)     NOT NULL DEFAULT 'draft',\n",
    ),
]

#: 新增的 CHECK 约束：插在 cost_entries 现有的最后一个 CHECK 之后
COST_ENTRIES_CHECK_ANCHOR = (
    "  -- 已确认的成本必须有确认人与确认时间（\"确认\"这个动作必须留下痕迹）\n"
    "  CONSTRAINT chk_cost_entries_confirmed CHECK (\n"
    "    confirm_state <> 'confirmed' OR (verified_by IS NOT NULL AND verified_at IS NOT NULL)\n"
    "  )\n"
)

COST_ENTRIES_CHECK_ADDED = (
    "  -- 已确认的成本必须有确认人与确认时间（\"确认\"这个动作必须留下痕迹）\n"
    "  CONSTRAINT chk_cost_entries_confirmed CHECK (\n"
    "    confirm_state <> 'confirmed' OR (verified_by IS NOT NULL AND verified_at IS NOT NULL)\n"
    "  ),\n"
    "  -- 下面两条替代原先 ENUM 提供的取值强约束。\n"
    "  -- 为什么要 CHECK 而不是只放宽成裸 VARCHAR(32)：ENUM **本身**就是数据库层的强约束，\n"
    "  -- 单纯换类型等于削弱校验（任何字符串都能落库）。用 CHECK 既拿到\"改状态集不必\n"
    "  -- MODIFY 整列\"的演进自由，又保住原强度 —— 也与 006/007/008 三个域的既有写法一致\n"
    "  -- （chk_warehouses_status / chk_purchase_orders_status / chk_sales_orders_status）。\n"
    "  CONSTRAINT chk_cost_entries_status CHECK (\n"
    "    status IN ('draft','submitted','verified','archived')\n"
    "  ),\n"
    "  CONSTRAINT chk_cost_entries_confirm_state CHECK (\n"
    "    confirm_state IN ('pending','confirmed')\n"
    "  )\n"
)

CATEGORIES_CHECK_ANCHOR = (
    "  CONSTRAINT fk_cost_categories_created_by FOREIGN KEY (created_by)\n"
    "    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT\n"
    ")"
)

CATEGORIES_CHECK_ADDED = (
    "  CONSTRAINT fk_cost_categories_created_by FOREIGN KEY (created_by)\n"
    "    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,\n"
    "  -- 替代原先 ENUM 的取值强约束（同 chk_cost_entries_status 的理由）\n"
    "  CONSTRAINT chk_cost_categories_status CHECK (\n"
    "    status IN ('enabled','disabled')\n"
    "  )\n"
    ")"
)


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    original = text

    for old, new in REPLACEMENTS:
        count = text.count(old)
        if count != 1:
            print(f"FAIL  命中 {count} 次（期望 1）：{old.strip()[:70]}")
            return 1
        text = text.replace(old, new, 1)

    for anchor, added, label in (
        (COST_ENTRIES_CHECK_ANCHOR, COST_ENTRIES_CHECK_ADDED, "cost_entries"),
        (CATEGORIES_CHECK_ANCHOR, CATEGORIES_CHECK_ADDED, "cost_categories"),
    ):
        count = text.count(anchor)
        if count != 1:
            print(f"FAIL  {label} 的 CHECK 锚点命中 {count} 次（期望 1）")
            return 1
        text = text.replace(anchor, added, 1)

    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"已改写 004_cost.sql（{len(REPLACEMENTS)} 处列定义 + 3 条 CHECK）")
    print(f"  行数 {original.count(chr(10)) + 1} -> {text.count(chr(10)) + 1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
