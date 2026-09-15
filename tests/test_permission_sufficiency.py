"""守卫：核验类写能力的**权限自足性** —— 声明了权限码就必须真的够用。

## 这条守卫为什么存在

能力声明 `required_permission="sales.verify"`，但实现里为了拼响应，内调了**读路径**
`self.get_delivery_by_id(...)`，而那个方法第一行就是 `ctx.require("sales.view")`。
于是：

* L1 过滤按 `required_permission` 判定 ⇒ 工具/按钮**可见**；
* 真去执行却要求一个**没写在声明里**的权限 ⇒ 403 `required_permission: sales.view`。

实测：给 `qc` 角色只发 `sales.verify`（严格按声明配），`delivery.verify` 出现在它的
工具清单里，一调用必 403。同类共三处：

| 能力 | 声明权限 | 实际还要 |
|---|---|---|
| `delivery.create` | `sales.deliver` | `sales.view` |
| `delivery.verify` | `sales.verify` | `sales.view` |
| `sales_receipt.verify` | `finance.receipt.verify` | `finance.receipt.manage` |

## 修法与判据

修法不是"把权限码补进声明"（声明只有一个槽位），而是把"拼装详情记录"抽成**不做权限
检查**的辅助方法（`_delivery_detail` / `_receipt_detail`），读写两条路径共用：
读能力的入口自己 `ctx.require(...)`，写路径各自已 require 过自己的权限。

判据双向，只查一边都会被绕过：
1. **写路径不得再调用带权限检查的读入口**（否则 403 复现）；
2. **读入口必须仍然 require 自己的权限**（否则就成了"为了修 403 把权限删掉"，
   等于把 `delivery.get` 对所有人开放）。
3. **共用的拼装函数里不得出现 `ctx.require`**（否则写路径重新不自足）。

判据用 AST（`called_attributes`）而不是字符串包含：本仓的注释会**引用代码原文**
解释缺陷，用字符串判会"谁把教训写进注释谁就红"。
"""

from __future__ import annotations

from conftest import load_all_status
from func_call_index import called_attributes

#: (类名, 方法名, 不允许调用的读入口, 该读入口必须保留的权限码)
CASES = [
    ("DeliveryWriteService", "create_delivery", "get_delivery_by_id", "sales.view"),
    ("DeliveryWriteService", "verify_delivery", "get_delivery_by_id", "sales.view"),
    ("SalesReceiptWriteService", "create_receipt", "get_receipt_by_id",
     "finance.receipt.manage"),
    ("SalesReceiptWriteService", "verify_receipt", "get_receipt_by_id",
     "finance.receipt.manage"),
]

#: 读入口所在的服务类
READERS = {
    "get_delivery_by_id": "DeliveryService",
    "get_receipt_by_id": "SalesReceiptService",
}


def test_写路径不得内调带权限检查的读入口():
    load_all_status()
    from fpa.domains.sales import deliveries_write

    problems: list[str] = []
    for class_name, method_name, forbidden, _permission in CASES:
        method = getattr(getattr(deliveries_write, class_name), method_name)
        if f"self.{forbidden}" in called_attributes(method):
            problems.append(
                f"{class_name}.{method_name} 调用了 self.{forbidden}(...) "
                f"—— 那会额外要求一个没写在声明里的权限，只持有声明权限的账号必 403"
            )
    assert not problems, "\n  ".join(["权限不自足：", *problems])


def test_读入口必须仍然校验自己的权限():
    """反向的一半：修 403 不能靠"把读权限删掉"。"""
    load_all_status()
    from fpa.domains.sales import sales_orders

    for _class_name, _method_name, entry, permission in CASES:
        service = getattr(sales_orders, READERS[entry])
        source = __import__("inspect").getsource(getattr(service, entry))
        assert f'ctx.require("{permission}")' in source, (
            f"{service.__name__}.{entry} 不再校验 {permission} —— "
            "这会把详情读对所有人开放，不是修 403 的正确方式"
        )


def test_写路径共用的拼装函数不得做权限检查():
    load_all_status()
    from fpa.domains.sales.sales_orders import DeliveryService, SalesReceiptService

    for service, helper in ((DeliveryService, "_delivery_detail"),
                            (SalesReceiptService, "_receipt_detail")):
        assert "ctx.require" not in called_attributes(getattr(service, helper)), (
            f"{service.__name__}.{helper} 里调用了 ctx.require —— "
            "它是读写共用的拼装函数，加权限检查就会让写路径重新不自足"
        )
