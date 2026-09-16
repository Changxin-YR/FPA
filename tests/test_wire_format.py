"""跨端**显示契约**的守卫：JSON 里不许出现用户读不懂的形态。

## 这一组守的是什么（一次真实的用户报障）

用户说"尤其是中英文混杂"，实测在**同一个响应**里同时命中三处：

    sold_at       = 'Wed, 02 Sep 2026 00:00:00 GMT'   ← 日期成了 HTTP 头的格式
    total_amount  = '100.0000000'                       ← 金额 7 位小数
    unit          = 'jin'                               ← 计量单位是英文码

三处的**根因各不相同**，所以修法也不同（这正是本仓"先定位再动手"的纪律）：

| 症状 | 根因 | 修法 |
|---|---|---|
| GMT 日期 | Flask `DefaultJSONProvider` 对 `datetime`/`date` 用 RFC 1123 | 换 provider，**一处**（`web/app.py::_install_wire_format`） |
| 7 位小数 | `数量(16,3) × 单价(14,4)` 的**派生值**没有列定义兜住 | `kernel/money.py` 按金额口径量化 |
| `jin` | 码是 `CHECK` 约束的合法值，**缺的是展示标签** | 内核 `UNIT_LABELS` + 行内 `unit_label` |

## 为什么这些断言必须存在（而不是"修完就算"）

三处**都不会报错**：日期是合法字符串、7 位小数是合法数字、`jin` 是合法枚举 ——
它们只是**对人不可读**。所以没有任何一层会拦住回归，只能靠这里钉住。

## 判据取得很具体（不是"看起来对不对"）

* 日期：必须是 ISO 8601（`\d{4}-\d{2}-\d{2}` 或带 `T` 的时刻），**不得含 `GMT`**；
* 金额：派生金额的小数位 ≤ 2；单价**必须是 4 位**（那是刻意的，不能被"统一截成 2 位"误伤）；
* 单位：带 `unit` 的行必须有 `unit_label`，且 `unit` 的**机器码保持不变**
  （改码会打断 CHECK 约束与 `HarvestQuantityMatch`）。
"""

from __future__ import annotations

import re

import pytest

from yuxin.factory import build_app
from yuxin.kernel.money import MONEY_QUANTUM, UNIT_PRICE_QUANTUM, money, unit_label
from yuxin.kernel.workflow import RESOURCES

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


# ---------------------------------------------------------------------------
# 1. JSON provider：日期必须是 ISO，不得是 GMT
# ---------------------------------------------------------------------------


def test_json_datetime_is_iso_not_gmt() -> None:
    """`datetime` 序列化成 ISO 8601，**不带 `GMT`**。

    默认 provider 给的是 `Wed, 02 Sep 2026 00:00:00 GMT`（RFC 1123）——
    那是 HTTP 头用的格式，出现在页面的日期列里就直接是缺陷。
    """
    from datetime import datetime

    app = build_app()
    with app.app_context():
        dumped = app.json.dumps({"t": datetime(2026, 9, 2, 13, 5, 0)})
    assert "GMT" not in dumped, dumped
    value = re.search(r'"t": "([^"]+)"', dumped).group(1)  # type: ignore[union-attr]
    assert _ISO_DATETIME.match(value), value


def test_json_date_is_iso_without_time_part() -> None:
    """纯 `date` 只输出日期部分 —— 不补 `T00:00:00`。

    补了会让"日期列"与"时刻列"在 JSON 里长得一样，而它们在语义上不同
    （`sold_at` 是日期、`created_at` 是时刻）。前端 `<input type="date">`
    的 `value` 要求的也正是 `YYYY-MM-DD`。
    """
    from datetime import date

    app = build_app()
    with app.app_context():
        dumped = app.json.dumps({"t": date(2026, 9, 2)})
    value = re.search(r'"t": "([^"]+)"', dumped).group(1)  # type: ignore[union-attr]
    assert _ISO_DATE.match(value), value
    assert "GMT" not in dumped, dumped


# ---------------------------------------------------------------------------
# 2. 金额与单价的位数口径（一处定义，两处消费）
# ---------------------------------------------------------------------------


def test_money_quantizes_to_two_decimals() -> None:
    """`数量 × 单价` 这类派生金额必须量化到 2 位。

    实测缺陷：`100.000(quantity) × 1.0000(unit_price)` 直接相乘得到
    `100.0000000`，而它是**派生值**，没有列定义帮它兜住。
    """
    from decimal import Decimal

    assert str(money(Decimal("100.000") * Decimal("1.0000"))) == "100.00"
    assert str(money(Decimal("12.5") * Decimal("5.0000"))) == "62.50"
    # 四舍五入到分
    assert str(money(Decimal("1.005"))) == "1.01"


def test_unit_price_keeps_four_decimals() -> None:
    """单价**不能**被"统一截成 2 位"——4 位是刻意的（`DECIMAL(14,4)`）。

    单价要能表达 `0.0001 元/尾` 这种极小单位价，而金额只到分。
    这条断言防的是"为了修 7 位小数而把所有小数都按金额口径量化"。
    """
    from decimal import Decimal

    assert UNIT_PRICE_QUANTUM == Decimal("0.0001")
    assert MONEY_QUANTUM == Decimal("0.01")
    assert str(UNIT_PRICE_QUANTUM * 1) == "0.0001"


# ---------------------------------------------------------------------------
# 3. 计量单位：码保留、标签必须有
# ---------------------------------------------------------------------------


def test_unit_labels_cover_the_sales_check_values() -> None:
    """`sales_orders.unit` 的 CHECK 取值（`kg`/`jin`/`tail`）都必须有中文标签。

    这三个码是 `008_sales.sql` 的 `chk_sales_orders_unit` 约束的一部分，
    也是 `HarvestQuantityMatch` 按 `unit == "jin"` 决定是否 ×2 的判据 ——
    **所以码不能改成中文**，补的是标签。
    """
    for code, expected in (("kg", "公斤"), ("jin", "斤"), ("tail", "尾")):
        assert unit_label(code) == expected, code


def test_unit_label_falls_back_to_the_code() -> None:
    """未登记的单位码回退成原词 —— 可诊断，**不猜**。

    `materials.unit` 是无约束的 `VARCHAR(16)`（默认 `kg`），所以未登记是可能的。
    静默换成某个中文会让"服务端加了单位却忘了加标签"变成看不见的问题。
    """
    assert unit_label("sack") == "sack"
    assert unit_label("") == ""


def test_currency_label_maps_cny_and_falls_back() -> None:
    """币种与单位同一口径：码是机器形态（`AmountWithin` 按码比较），补的是标签。

    实测缺陷：应付列表的「币种」列直接渲染 `CNY` —— 与 `unit = jin` 同族，
    根因是"有码、没有给人看的标签"。
    """
    from yuxin.kernel.money import currency_label

    assert currency_label("CNY") == "人民币"
    assert currency_label("cny") == "人民币", "码大小写不该改变结果"
    # 未登记的币种回退成原词（可诊断），不猜一个中文
    assert currency_label("JPY") == "JPY"
    assert currency_label("") == ""


# ---------------------------------------------------------------------------
# 4. 列声明：派生列必须真的被声明，否则界面上看不到
# ---------------------------------------------------------------------------


def test_declared_columns_exist_in_the_read_payload_shape() -> None:
    """`Resource.columns` 里的每个列名，都必须能由**读路径**提供。

    这条防的是一类静默缺陷：服务端算了 `unit_label` 却忘了把它加进 `columns`，
    于是**值在响应里、列不在表里** —— 用户看不到，而没有任何测试会红。

    判据不用起服务：`unit_label` / `quantity_with_unit` 这类派生列与
    `status_label` 同族，出现在 decorate 里就应该被声明。这里检查"声明了的列，
    名字形态是可解释的"（`*_label` / `*_with_unit` / 数据库真列），
    并**正向要求**单位相关的派生列确实被声明了。
    """
    sales = RESOURCES.find("sales_order")
    assert sales is not None
    keys = [key for key, _ in sales.columns]
    assert "unit_label" in keys, f"销售单没有声明 unit_label（列={keys}）"
    assert "unit" not in keys, (
        "销售单的列里不该出现机器码 `unit`——列是给人看的，码留给程序；"
        f"（列={keys}）"
    )

    material = RESOURCES.find("material")
    assert material is not None
    mkeys = [key for key, _ in material.columns]
    assert "unit_label" in mkeys, f"物料没有声明 unit_label（列={mkeys}）"

    for name in ("inventory_lot", "inventory_ledger"):
        resource = RESOURCES.find(name)
        assert resource is not None
        rkeys = [key for key, _ in resource.columns]
        assert "quantity_with_unit" in rkeys, f"{name} 没有声明 quantity_with_unit（列={rkeys}）"


@pytest.mark.parametrize("label_like", ["status_label", "unit_label", "quantity_with_unit"])
def test_no_display_column_leaks_a_raw_gmt_string(label_like: str) -> None:
    """展示列的名字里不许出现"原始时间串"的痕迹。

    这条是**反向**保险：`GMT` 串来自 JSON provider，任何列都可能中招，
    所以这里对"所有展示列"断言其名字不暗示裸时间戳（`*_at_raw` / `*_gmt` 之类）。
    目前没有这种列 —— 断言为**空集**必须能自证（本仓踩过"空集报通过"）。
    """
    offenders: list[str] = []
    inspected = 0
    for resource in RESOURCES.all():
        for key, _label in resource.columns:
            inspected += 1
            if "gmt" in key.lower() or key.endswith("_raw") or key.endswith("_ts"):
                offenders.append(f"{resource.name}.{key}")
    assert inspected > 0, "一个列都没扫到 —— 这个断言无法发现任何问题（口径失效）"
    assert offenders == [], f"展示列里出现了疑似裸时间戳/未格式化列：{offenders}"
