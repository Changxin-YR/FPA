"""守卫：**读能力不得设置 `HandlerResult.resource_id`**。

## 这条守卫为什么存在（一次"整页都是 —"的静默缺陷）

`web/app.py::_ok` 的封装规则是：

    body["data"] = result.data
    if result.resource_id is not None:
        body["data"] = {"resource_id": result.resource_id, "record": result.data}

那是**写**能力的形状（写的结果要带主键）。而详情读能力本身已经返回
`data={"record": …}`，若它也带上 `resource_id`，就会多包一层变成
`data.record.record`：

* HTTP 200、没有异常、日志干净；
* 前端 `ResourceDetailPage.vue` 读 `payload.record`（= `data.record`），拿到的是
  内层那个 `{record: …}` 字典 ⇒ 每个字段都取不到 ⇒ **整页每格渲染「—」**。

实测：17 个 `*.get` 里只有 `warehouse.get` 是这样（`warehouse/inventory.py` 里
多写了一行 `resource_id=int(raw)`），其余 16 个都是单层 —— 也就是说这是"漏改一处"
而不是"约定不清"。判据写成**通用规则**，将来新增详情读不会再漏。

## 判据

遍历注册表，对 `kind == "read"` 的能力，取它的 handler 源码做 AST 分析：
只要出现带 `resource_id=` 关键字实参的 `HandlerResult(...)` 调用就判红。
`load_*` 之类的回读函数不返回 `HandlerResult`，因此不受影响。
"""

from __future__ import annotations

import ast
import inspect

from conftest import load_all_status


def _handler_sets_resource_id(handler) -> bool:
    source = inspect.getsource(handler)
    tree = ast.parse(inspect.cleandoc(source) if source.startswith(" ") else source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "HandlerResult":
            continue
        if any(kw.arg == "resource_id" for kw in node.keywords):
            return True
    return False


def test_读能力不得设置_resource_id():
    load_all_status()
    from yuxin.kernel.capability import REGISTRY

    reads = [c for c in REGISTRY.all() if c.kind == "read"]
    # 空集不得当成通过：先证明真的扫到了读能力。
    assert len(reads) > 20, f"只扫到 {len(reads)} 条读能力，判据可能失效了"

    offenders = [c.name for c in reads if _handler_sets_resource_id(c.handler)]
    assert not offenders, (
        "这些读能力设置了 `resource_id`，`_ok` 会因此多包一层 "
        "(`data.record.record`)，详情页会整页显示「—」且不报错：\n  "
        + "\n  ".join(sorted(offenders))
    )
