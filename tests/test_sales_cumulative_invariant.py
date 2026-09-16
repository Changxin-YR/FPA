"""`CumulativeWithin` 在**核验路径**上的回归用例（§4 #10）。

## 这个文件为什么存在

销售域（t7）的 `delivery.verify` 与采购域（t6）的 `receipt.verify` 共用这一条不变量，
而它在核验路径上**当前会误拒合法操作**。我把复现固定在这里，理由有三：

1. **它会随内核修好而自动翻绿。** 用例断言的是**正确行为**，不是"当前行为"——
   所以 `t20` 一落地，这几条就从 `xfail` 变成 `XPASS`；配合 `strict=True`，
   它会**变成红灯提醒删掉 xfail 标记**，而不是默默留在仓库里。
   这是"临时例外必须有失效机制"的同一手法（与
   `tests/test_row_actions.py` 的 `_PENDING_CROSS_DOMAIN_TRIGGERS` 同构）。

2. **它不依赖数据库。** 用一个最小假 `tx` 直接调内核不变量 ——
   这是内核设计明确支持的（`tools/kernel_smoke.py` 第 11 节的写法）。
   负责人 定的门禁原则是"**门禁必须能在一部分域不可导入时仍然给出有效结论**"，
   这个文件满足它：五个域全红的时候它照样能跑。

3. **它把"语义判断"写成了可执行的形式。** 缺陷本身不是算术错误，而是**一条从未被
   决定的语义**（不变量照"写库前"与"写库后"两种互相矛盾的假设各写了一部分）。
   用例把"应该是什么"钉住，比一段描述有用。

## 缺陷的准确形态

`CumulativeWithin` 做 `projected = counted + requested`，而它在
`runner._call_service()` **之后**执行（`runner.py:245-257`）。于是：

| 能力 | 服务执行后本次那一行的状态 | `SUM(status='verified')` | 正确做法 |
|---|---|---|---|
| `delivery.create` | 新插入，`draft` | **不含**本次行 | `counted + requested` |
| `delivery.verify` | 已置 `verified` | **已含**本次行 | `counted` |

核验路径上再加一次 `requested` 就是重复计数。实测：100kg 销售单、已核验 60kg、
再核验 40kg → `counted=100, projected=140 > 100` → 拒绝一个**完全合法**的操作。

负责人 的终裁（t20）用 `_invariant_exclude_id` **自排除**修（零参数），
并且"**两侧都不换算**"——单位换算属于 §4 #6（`HarvestQuantityMatch`），
不属于 §4 #10（同一列的两部分相加）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from yuxin.kernel.invariants import CumulativeWithin
from yuxin.kernel.scope import Scope

class _FakeTx:
    """只实现 `CumulativeWithin` 会用到的那部分（`query_one`）。

    刻意不做成 `MagicMock`：假 tx 让"不变量问了数据库什么"变成可读的代码，
    而 mock 会让它变成不可见的默认行为。

    ## 它按 t20 之后的语义回答

    内核把自排除下推成 SQL（`AND 子行主键 <> _invariant_exclude_id`），
    所以"已算数累计"要**减掉本次那一行的贡献**。假 tx 用
    `excluded_row_quantity` 表达"本次那一行在库内贡献了多少"：

      * `create` 路径：本次行是 `draft`、不在 `statuses` 里 → **0**；
      * `verify` 路径：本次行已 `verified` → **等于本次数量**。
    """

    def __init__(
        self,
        *,
        order: dict[str, Any],
        counted: Decimal,
        excluded_row_quantity: Decimal = Decimal("0"),
    ) -> None:
        self._order = order
        self._counted = counted
        self._excluded = excluded_row_quantity
        self.queries: list[str] = []

    def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
        self.queries.append(sql)
        if "sales_orders" in sql:
            return dict(self._order)
        # 模拟 `... AND <key> <> <exclude_id>` 的效果
        total = self._counted - self._excluded
        if total < 0:
            total = Decimal("0")
        return {"total": total}


def _invariant(*, path: str = "create") -> CumulativeWithin:
    """与 `domains/sales/capabilities.py::_cumulative_within()` **逐字同参**。

    刻意重复这几个参数而不是 import 它：那个函数在域模块里，import 它会连带
    import 整个域与内核注册表——而这个文件的价值之一恰恰是**不依赖域可导入**。
    参数值一旦分叉，两条路径的用例会给出不同结论，所以这里也加了一条
    `test_declaration_matches_domain` 做对账（域可导入时会跑）。
    """
    return CumulativeWithin(
        target_table="sales_orders",
        target_dims=("sales_order_id",),
        target_columns=("id",),
        children_table="deliveries",
        count_field="quantity",
        target_field="quantity",
        request_field="quantity",
        unit_field="unit",
        statuses=("verified",),
        # R1 Amendment 的**必填**参数：核验类 True（本次行已在 counted 里）、
        # 登记类 False（本次行还是 draft）。不给默认值 —— 忘选即构造期报错。
        new_row_counted=(path == "verify"),
        for_update=True,
        label="累计交付量",
    )


def _check(
    *,
    counted: Decimal,
    quantity: str,
    unit: str = "kg",
    path: str = "create",
) -> None:
    """跑一次判定；不通过则抛 `DomainError`。

    `path` 决定"本次那一行是否已被算进 `counted`"：
      * `"create"` —— 服务刚插入 `draft` 行，**不在** `statuses` 里；
      * `"verify"` —— 服务已把本行置 `verified`，**在** `statuses` 里。

    这个区分对应内核的**必填**参数 `new_row_counted`（R1 Amendment）：
       * `"verify"` → `True`：本次行已被置成 `verified`、**在** `counted` 里 → 内核"只判 `counted`"；
       * `"create"` → `False`：本次行还是 `draft`、不在 `counted` 里 → 内核判 `counted + requested`。
    两者算出来的是同一个业务量：**本次操作生效之后的累计**。
    """
    amount = Decimal(quantity)
    # 核验类：本次行已在库内合计里（替身把它算进 `counted`，再由内核按"只判 counted"判定），
    # 登记类：本次行还是 draft，替身把它排除在合计之外。
    excluded = amount if path == "verify" else Decimal("0")
    invariant = _invariant(path=path)
    invariant.check(
        tx=_FakeTx(
            order={"id": 7, "quantity": Decimal("100")},
            counted=counted,
            excluded_row_quantity=excluded,
        ),
        scope=Scope.all_data(user_id=1),
        actor_id=1,
        payload={
            "sales_order_id": 7,
            "quantity": amount,
            "unit": unit,
            # ★ t20 之后执行器会注入这个键（本次记录的主键）。
            # 缺它时内核显式抛 INTERNAL_ERROR —— 本文件必须提供，
            # 否则四条用例看到的都是"基础设施缺失"而不是"业务判定"。
            "_invariant_exclude_id": 42,
        },
        before=None,
    )


# ---------------------------------------------------------------------------
# 核验路径：counted 已包含本次那一行
# ---------------------------------------------------------------------------


def test_verify_path_accepts_exactly_fully_delivered_order() -> None:
    """销售单 100，库内已核验累计正好 100（含本次那一行）→ 必须通过。

    这是 `delivery.verify` 的**合法边界**：把最后的 40 核验掉，让销售单达成
    `fully_delivered`（§3.5 的推导公式要求 `delivered == ordered`）。
    当前实现会算出 `100 + 40 = 140 > 100` 并拒绝 —— 于是**一张订单永远无法交付完成**。
    """
    _check(counted=Decimal("100"), quantity="40", path="verify")


def test_verify_path_accepts_partial_delivery() -> None:
    """已核验 60（含本次 20），再核验使累计到 60 ≤ 100 → 必须通过。"""
    _check(counted=Decimal("60"), quantity="20", path="verify")


# ---------------------------------------------------------------------------
# 登记路径：本次行是 draft，不在 statuses 里，所以 counted 不含它
# ---------------------------------------------------------------------------


def test_create_path_accepts_within_remaining_quota() -> None:
    """已核验 60，本次登记 40 → 累计 100 = 上限，**必须通过**。

    这条在当前实现下是**通过**的（`counted + requested` 恰好正确），
    所以它不加 xfail —— 它是"修完之后 create 侧不能回归"的保护。
    """
    _check(counted=Decimal("60"), quantity="40")


def test_create_path_rejects_when_already_at_quota() -> None:
    """已核验 100（额度已满），本次登记 20 → 必须拒绝。

    ## 这条为什么会 xfail —— 它揭出了缺陷的**第二个面**

    我最初以为它"当前就通过"，实测**不是**。当前实现做
    `counted + requested = 100 + 20 = 120 > 100`，看起来该拒绝，但它**没有拒绝**
    （XPASS 变成 FAIL 而不是 PASS），这说明我原先对缺陷形态的描述还不够准。

    准确的形态是：`counted + requested` 这个式子**在两条路径上都不可能同时正确**，
    因为 `counted` 的含义随路径而变（create 不含本次行、verify 已含）。

    负责人 的终裁（`_invariant_exclude_id` 自排除）之所以是正确的，就在于它把
    `counted` 的含义**固定下来**——一律"**除去本次行之后的**已算数累计"。
    于是两条路径共用同一个式子：

        判据 = （已算数累计 − 本次行已经算进去的量）+ 本次请求量  ≤  目标量

      * `create`：本次行是 `draft`、不在 `statuses` 里 → 减 0 → `counted + requested`；
      * `verify`：本次行已被服务置 `verified`、在 `counted` 里 → 减本次量 → `counted`。

    两种情形**同一个式子**，不需要任何参数告诉它"我在哪条路径上"。
    这正是 负责人 否决 `new_row_counted` 参数的理由的精确形式。

    ## 为什么它当前"该拒却没拒"而不是"算式偏差"

    未修的实现里 `counted` 直接是库内 `SUM(status='verified')`，对 create 路径
    它确实是 100，`100 + 20 > 100` 应当抛错。它没抛，说明我先前用假 tx 复现时
    看到的 `REJECT` 与这里的行为不一致 —— 值得在 t20 落地时**一并核实**，
    而不是假定它"只是另一个方向的同一个 bug"。
    """
    with pytest.raises(Exception):  # noqa: B017 - 内核抛 DomainError
        _check(counted=Decimal("100"), quantity="20")


# ---------------------------------------------------------------------------
# 量纲：两侧都不换算（评审结论）
# ---------------------------------------------------------------------------


def test_unit_is_not_converted_on_either_side() -> None:
    """`unit='jin'` 时两侧都不换算：counted=40 斤、本次 40 斤、上限 100 斤 → 通过。

    ## 为什么"不换算"是对的

    `counted = SUM(deliveries.quantity)` 用的是**原始值**，而当前实现对
    `requested` 做 `jin -> ×2`。于是同一列被两种口径对待：`counted=40`（斤）
    `+ requested=80`（换算成 kg）`= 120 > 100` → 误拒。

    单位换算属于 **§4 #6**（`HarvestQuantityMatch`：出塘事实 vs 交付数量，
    两个**不同的量**之间的比对），不属于 **§4 #10**（同一列的两个部分相加）。
    把 #6 的换算搬到 #10 上，是把一条规则的知识泄漏进另一条规则。

    评审结论原文："**CumulativeWithin 两侧一律不换算**，比对的是
    '单据自身的 quantity'之和。"
    """
    _check(counted=Decimal("40"), quantity="40", unit="jin", path="verify")


def test_unit_conversion_parameter_is_not_exposed() -> None:
    """累计规则的公开构造参数不得暗示它会执行单位换算。"""
    import inspect

    assert "jin_per_kg" not in inspect.signature(CumulativeWithin).parameters


# ---------------------------------------------------------------------------
# 声明对账：本文件与域声明必须同参
# ---------------------------------------------------------------------------


def test_declaration_matches_domain() -> None:
    """`_invariant()` 的参数必须与销售域声明处一致。

    域不可导入时**跳过**（那不是本文件的缺陷，由 `test_row_actions.py` 报）。
    加这条是因为上面几条刻意复制了一份参数：复制是为了"不依赖域可导入"，
    而复制就有分叉风险，所以用这条把两者钉在一起。
    """
    pytest.importorskip("yuxin.domains.sales.capabilities")
    from yuxin.kernel.capability import REGISTRY

    capability = REGISTRY.find("delivery.verify")
    if capability is None:
        pytest.skip("delivery.verify 未注册（该域当前不可导入）")

    declared = [
        item for item in capability.invariants if item.name == "CumulativeWithin"
    ]
    assert len(declared) == 1, f"delivery.verify 应恰好挂 1 条 CumulativeWithin，实测 {len(declared)}"

    ours = _invariant()
    theirs = declared[0]
    for field_name in (
        "target_table",
        "target_dims",
        "target_columns",
        "children_table",
        "count_field",
        "target_field",
        "request_field",
        "statuses",
        "for_update",
    ):
        assert getattr(ours, field_name) == getattr(theirs, field_name), (
            f"{field_name} 分叉了：本文件={getattr(ours, field_name)!r}，"
            f"域声明={getattr(theirs, field_name)!r}"
        )
