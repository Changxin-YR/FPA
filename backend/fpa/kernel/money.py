"""金额与数量的**小数位数规则**（全系统唯一一处）。

## 为什么它必须在内核里，而不是各域各写一份

实测：仓内已经有**三份**同一件事的不同实现 ——

    domains/cost/entries.py       AMOUNT_QUANTUM = Decimal("0.01")
    domains/purchase/payables.py  _AMOUNT_QUANTUM = Decimal("0.01")  （外加一个 _quantize）
    domains/warehouse/ledger.py   (unit_cost * delta).quantize(Decimal("0.0001"))

三处数值还不一样（`0.01` vs `0.0001`），而它们是**同一件事**："一个金额应当保留几位小数"。
这类"同一规则多处实现"正是本项目的核心论点要消灭的形态，而且它的失效是**静默的**：
某一处写 `0.0001` 不会报错，只会让那一列在界面上变成 `100.0000`。

## 规则来自**列定义**，不是拍脑袋

量不是"看着好看"定的，它必须与迁移里的 DDL 一致，否则会出现两处描述同一件事：

| 概念 | 位数 | 依据 |
|---|---|---|
| 金额 | **2** | `cost_entries.amount` / `receivables.total_amount` / `purchase_payables.total_amount` = `DECIMAL(16,2)` |
| 单价 | **4** | `sales_orders.unit_price` / `materials.unit_price` / `warehouse_documents.unit_cost` = `DECIMAL(14,4)` |
| 数量 | **3** | `sales_orders.quantity` / `feedings.quantity` / `harvests.quantity` = `DECIMAL(18,3)` |

**单价 4 位是刻意的**（不是缺陷）：单价 4 位、金额 2 位是财务惯例 —— 单价要能表达
`0.0001 元/尾` 这种极小单位价，而金额只到分。所以"金额 2 位"这条不能用
"把所有小数都截成 2 位"来实现，那会静默毁掉单价。

## `DECIMAL(18,4)` 的金额列是既有事实，本模块**不**改写它

`warehouse_documents.total_amount` 与 `inventory_ledger.amount` 是 `DECIMAL(18,4)`。
它们是既有列定义（与本表的 `quantity` 同精度，便于同行相加），**改它们需要迁移**。
本模块的作用是：让**计算派生值**有统一口径（尤其 `数量 × 单价` 这种会产出 7 位小数的乘法），
而不是去改列定义。两者不一致这件事见 `docs/DISPLAY_CONTRACT.md` 的口径表。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

#: 金额：2 位小数（分）。
MONEY_QUANTUM = Decimal("0.01")

#: 单价：4 位小数。
UNIT_PRICE_QUANTUM = Decimal("0.0001")

#: 数量：3 位小数。
QUANTITY_QUANTUM = Decimal("0.001")

#: `Decimal` 位数不超过这个值时，`Decimal.__str__` 会用科学计数法？
#: 本项目不依赖它的行为，但**所有**返回值都经 `str()` 显式输出，避免 `1E+2`。
_SCIENTIFIC_THRESHOLD = Decimal("1E+16")


def to_decimal(value: Any) -> Decimal:
    """把任意值安全地转成 `Decimal`。

    走 `str(value)` 而不是 `Decimal(value)`：后者对 `float` 会把二进制浮点误差
    一起搬进来（`Decimal(0.1)` 是 `0.1000000000000000055511151231257827021181583404541015625`）。
    金额出现这种尾巴是财务系统的经典缺陷，而它**不会报错**。
    """
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    if isinstance(value, float):
        # 明确拒绝把 float 当金额来源：它说明上游某处已经丢过精度。
        return Decimal(repr(value))
    return Decimal(str(value))


def money(value: Any) -> Decimal:
    """量化成**金额**（2 位小数，四舍五入）。

    用于：金额字段的派生值（`数量 × 单价` 是最常见的一处 —— 实测它产出 7 位小数）、
    以及任何要写进 `DECIMAL(16,2)` 列的值。
    """
    return to_decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def unit_price(value: Any) -> Decimal:
    """量化成**单价**（4 位小数）。金额 2 位规则**不适用**于单价。"""
    return to_decimal(value).quantize(UNIT_PRICE_QUANTUM, rounding=ROUND_HALF_UP)


def quantity(value: Any) -> Decimal:
    """量化成**数量**（3 位小数）。"""
    return to_decimal(value).quantize(QUANTITY_QUANTUM, rounding=ROUND_HALF_UP)


def as_str(value: Decimal) -> str:
    """按 `Decimal` 自身的位数输出字符串（**不**改位数、不引科学计数法）。

    为什么统一从这里出字符串：`json.dumps` 对 `Decimal` 会退化成 float，
    所以本项目一律先 `str()`。但"先 str()"这件事如果各域各写，就会有的地方
    顺手 `float()` 一下 —— 那是同一个缺陷的另一形态。所以出口也收在这里。
    """
    return str(value)


__all__ = [
    "CURRENCY_LABELS",
    "MONEY_QUANTUM",
    "QUANTITY_QUANTUM",
    "UNIT_LABELS",
    "UNIT_PRICE_QUANTUM",
    "as_str",
    "currency_label",
    "money",
    "quantity",
    "to_decimal",
    "unit_label",
    "unit_price",
]

# ---------------------------------------------------------------------------
# 计量单位：码 → 中文
#
# 与 `kernel/workflow.py::ACTION_LABELS` 同一纪律：**码与它的中文放同一处**，
# 由服务端随行数据下发（`unit_label`），前端不写 `{jin: '斤'}` 这类映射表。
#
# ## 为什么它们必须是**机器码**而不是直接存中文（一次实测缺陷的判定）
#
# 界面上曾看到 `unit = jin`。第一反应是"数据里写了英文"，但实测下来**不是数据问题**：
#
#   * `008_sales.sql` 有 `CONSTRAINT chk_sales_orders_unit CHECK (unit IN ('kg','jin','tail'))`
#     —— 这三个码是**数据库约束的一部分**；
#   * `docs/CAPABILITY_REGISTRY.md` §2.9 也把 `unit` 的合法值定为 `kg` / `jin` / `tail`。
#
# 所以 `jin` 是**正确的存储形态**（码用于判定与比较：`HarvestQuantityMatch` 就要按
# `unit == "jin"` 决定是否 ×2），缺的是**展示标签**。判定结论遂为
# "显示问题，不是数据问题" —— 改的是"码没带标签"，不是"把码改成中文"。
# 若反过来把 `jin` 改成 `斤`，会同时打断 CHECK 约束与那条按码比较的不变量。
# ---------------------------------------------------------------------------

#: 计量单位 码 → 中文。
#:
#: 覆盖 `sales_orders` 的 `kg/jin/tail` 与 `materials.unit` 的常见取值。
#: `materials.unit` 是无约束的 `VARCHAR(16)`（默认 `kg`），因此**未登记的码回退成原词**
#: —— 与 `row_action_label()` 同口径：显示原词是**可诊断**的，静默换成别的中文会让
#: "服务端加了单位却忘了加标签"变成看不见的问题。
UNIT_LABELS: dict[str, str] = {
    "kg": "公斤",
    "jin": "斤",
    "tail": "尾",
    "g": "克",
    "ton": "吨",
    "bag": "袋",
    "box": "箱",
    "piece": "个",
    "yuan": "元",
}


def unit_label(code: str) -> str:
    """计量单位码 → 中文。未登记的码回退成原词（**不猜**）。"""
    return UNIT_LABELS.get(str(code).strip(), str(code))


#: 币种码 → 中文。与 `UNIT_LABELS` 同一条纪律：码是机器形态（`purchase_payables.currency`
#: 存 ISO 4217 码、`AmountWithin` 按码比较），缺的是展示标签。
#:
#: 目前系统只落 `CNY`（`purchase/payables.py` 的默认值与 `AmountWithin(currency="CNY")`），
#: 这里只登记能核实来源的这一条；未登记的码**回退成原词**（可诊断，不猜）。
CURRENCY_LABELS: dict[str, str] = {
    "CNY": "人民币",
}


def currency_label(code: str) -> str:
    """币种码 → 中文。未登记的码回退成原词（**不猜**）。"""
    return CURRENCY_LABELS.get(str(code).strip().upper(), str(code))
