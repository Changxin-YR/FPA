"""守卫：`Resource` 声明的**派生显示列**，读路径必须真的取得到。

## 这条守卫为什么存在

`Resource(...).columns` 是前端列表/详情渲染列的直接来源。声明了某一列、而读路径
的 SQL 没有 SELECT 它，结果是该列**永远渲染成「—」** —— HTTP 200、无异常、日志干净。

实测到的三处（同一个成因：只 JOIN 到采购单，没 JOIN 往来单位）：

| 能力 | 症状 |
|---|---|
| `purchase_payable.get` | `supplier_name` 缺失，**且**多出一个垃圾键 `o.supplier_id` |
| `payment.list` | `supplier_name` 键在但恒为 `null` |
| `payment.get` | `supplier_name` 键根本不存在 |

`list_payables` 一直是 JOIN 的，所以当时是"列表有、详情没有"——两处描述同一件事却不一致。
垃圾键的来历：`_payable_scope_row` 的 SQL 里 `p.*` 已带 `supplier_id`，又显式
`SELECT o.supplier_id AS supplier_id`，**重复列名被 PyMySQL 的 DictCursor 改写成字面键
`o.supplier_id`**，而真正需要的 `supplier_name` 反而没有。

## 判据

只看函数体里的**字符串字面量**（`string_literals`），不看注释/文档字符串 ——
否则解释这条缺陷的那段注释本身会把守卫判红（第一版就踩了）。
"""

from __future__ import annotations

from conftest import load_all_status
from func_call_index import string_literals


def _resource_columns(name: str) -> dict[str, str]:
    from fpa.kernel.workflow import RESOURCES

    resource = RESOURCES.find(name)
    assert resource is not None, f"资源 {name} 未注册"
    return {key: label for key, label in resource.columns}


def test_应付与付款的读路径都取得到_supplier_name():
    load_all_status()
    from fpa.domains.purchase import payments

    # 锚点：列声明在，本条守卫才有意义（删掉列声明就绕不过来了）
    assert "supplier_name" in _resource_columns("purchase_payable")
    assert "supplier_name" in _resource_columns("purchase_payment")

    sources = {
        "payment.list / payment.get 共用的 _PAYMENT_SELECT": payments._PAYMENT_SELECT,
        "purchase_payable.get 的 _payable_scope_row": " ".join(
            string_literals(payments.PurchasePaymentService._payable_scope_row)
        ),
    }
    for label, sql in sources.items():
        assert "supplier_name" in sql, f"{label} 没有取 supplier_name（该列会永远显示「—」）"
        assert "business_partners" in sql, (
            f"{label} 没有 JOIN business_partners，取不到供应商名"
        )


def test_应付详情不得再出现重复列名导致的垃圾键():
    """`p.*` 已带 `supplier_id` 时，SQL 里不该再显式 SELECT 一个 `supplier_id`。"""
    load_all_status()
    from fpa.domains.purchase import payments

    sql = " ".join(string_literals(payments.PurchasePaymentService._payable_scope_row))
    assert "p.*" in sql, "判据锚点丢了：这段 SQL 不再用 p.*"
    assert "supplier_id AS supplier_id" not in sql, (
        "`_payable_scope_row` 的 SQL 里同时有 `p.*` 与 `... AS supplier_id`，"
        "重复列名会被 PyMySQL 改写成 `o.supplier_id` 这样的垃圾键：\n" + sql
    )


def test_成本类别与币种的展示列由读路径提供():
    """`category_label` / `currency_label` 必须是读路径算出来的派生列。

    这两列是同一类缺陷的收尾：**给人看的列直接渲染了机器码**（成本记录的
    `成本类别` 显示 `other`、应付的 `币种` 显示 `CNY`）。修法不是把码改成中文
    （`category_code` 是软外键、`currency` 被 `AmountWithin` 按码比较），
    而是补派生标签 —— 所以断言必须落在"读路径真的产出了这个键"，
    否则列声明在、值永远缺失，界面上只会是一片「—」。
    """
    load_all_status()
    from fpa.domains.cost import entries as cost_entries
    from fpa.domains.purchase import service as purchase_service

    cost_columns = _resource_columns("cost_entry")
    assert "category_label" in cost_columns, f"成本记录未声明 category_label（列={cost_columns}）"
    assert "category_code" not in cost_columns, (
        "成本记录的展示列里不该出现机器码 `category_code` —— "
        f"那是给 `_require_category` / `by_category` 用的（列={cost_columns}）"
    )
    # 类别名在库里（后台可维护），必须 JOIN 出来，不能在 Python 里写静态映射表
    sql = cost_entries._ENTRY_SELECT
    assert "category_label" in sql, f"成本读路径没有产出 category_label：{sql}"
    assert "cost_categories" in sql, f"成本读路径没有 JOIN 类别表：{sql}"

    payable_columns = _resource_columns("purchase_payable")
    assert "currency_label" in payable_columns, (
        f"应付未声明 currency_label（列={payable_columns}）"
    )
    assert "currency" not in payable_columns, (
        f"应付的展示列里不该出现机器码 `currency`（列={payable_columns}）"
    )
    literals = string_literals(purchase_service.decorate_payable)
    assert "currency_label" in literals, (
        "`decorate_payable` 没有产出 currency_label —— 该列会永远显示「—」"
    )
