"""测试辅助：从函数源码里做 **AST 级**的"它到底调用了什么"判断。

## 为什么需要它（两次真实误判）

本仓的函数注释里会**引用代码原文**来解释缺陷，例如：

    # 不要**再写 `o.supplier_id AS supplier_id`：`p.*` 里已经有 supplier_id
    # 它们原先都走 `self.get_delivery_by_id(...)`……

于是 `inspect.getsource(func)` 里会包含这些字符串 —— 直接
`assert "xxx" not in source` 会被**文档字符串本身**判红，守卫变成"谁把教训写进
注释谁就红"，正好惩罚了正确行为。

判据必须落在**语法结构**上：函数体里有没有真的"调用"它，而不是"提到"它。
"""

from __future__ import annotations

import ast
import inspect
import textwrap


def _function_ast(func) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(func)))


def called_attributes(func) -> set[str]:
    """返回函数体里出现的调用名，形如 `{"ctx.require", "self.get_delivery_by_id"}`。

    只统计**被调用**的属性链（`ast.Call` 的 func），不看注释、字符串与裸引用。
    """
    found: set[str] = set()
    for node in ast.walk(_function_ast(func)):
        if not isinstance(node, ast.Call):
            continue
        func_node = node.func
        if isinstance(func_node, ast.Attribute) and isinstance(func_node.value, ast.Name):
            found.add(f"{func_node.value.id}.{func_node.attr}")
    return found


def string_literals(func) -> list[str]:
    """返回函数体里所有字符串字面量（含 f-string 的字面片段），**不含注释与文档字符串**。

    `ast` 本来就不把注释当节点；文档字符串是一个 `Expr(Constant)`，这里显式跳过它。
    """
    tree = _function_ast(func)
    body = tree.body[0].body if tree.body and isinstance(tree.body[0], ast.FunctionDef) else []
    doc_node = body[0] if body and isinstance(body[0], ast.Expr) else None
    out: list[str] = []
    for node in ast.walk(tree):
        if node is doc_node:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            out.append(
                "".join(
                    part.value for part in node.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
            )
    return out
