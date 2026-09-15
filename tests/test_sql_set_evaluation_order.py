"""守卫：SQL 里"先给某列赋值、又在后面的 SET 项里读它"的写法。

## 这条守卫为什么存在（一次真实的错账）

`delivery.verify` 生成应收后推进累计已收款，原来的语句是：

    UPDATE receivables SET
      paid_amount = LEAST(paid_amount + %s, total_amount),
      status      = CASE WHEN paid_amount + %s >= total_amount THEN 'settled' ...

MySQL 单表 UPDATE 的赋值**从左到右**求值 —— 第二条 SET 里的 `paid_amount`
已经是第一条更新后的值，于是**同一笔收款被算了两遍**：应收 32240、只收 16120（50%）
就被判成 `settled`，界面同屏显示「未收款 16120.00 / 状态=已结清」。

它极难被偶然发现：30% 与 100% 时结果恰好正确，**只有 [50%, 100%) 区间错**。
而且 `paid_amount` 本身是对的（`LEAST` 封顶），DB 的 `chk_receivables_paid` 只校验
`paid <= total`、`AmountWithin` 只校验单笔不超余额 —— **没有任何一层校验 `status`
与 `paid/total` 的一致性**，所以全套门禁都是绿的。

## 判据

扫描 `backend/fpa` 下所有字符串字面量里的 `UPDATE … SET`：若第 i 项赋值的列名
**又出现在第 j（j>i）项的表达式里**，即判红。这与"哪张表、哪一列"无关，是**类级**
判据：即使换成 `stock = stock - n, flag = IF(stock > 0, …)` 也照样抓得住。

反向对照（本次修复时实测）：把上面那条语句放回去 → 本用例命中 1 处；
换成"显式算好再写" → 0 处。

## 为什么断言里要有"扫描规模"

"0 处命中"与"扫描器什么都没扫到"长得一模一样。项目在
`tools/registry_reconcile.py` 里为同一形态吃过一次亏（"采购域 0 处可疑"的真因是
它根本不用 `LIMIT 1`），所以这里也把**扫了多少条 UPDATE** 打出来并要求它非零。
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

_BACKEND_FPA = Path(__file__).resolve().parents[1] / "backend" / "fpa"

#: 目录级剪枝（禁用 rglob：全盘递归会进 __pycache__ 之类）。
_SKIP_DIRS = {"__pycache__", ".mypy_cache", ".ruff_cache"}

_ASSIGN = re.compile(r"^([a-z_][a-z0-9_]*)\s*=\s*(.+)$", re.S | re.IGNORECASE)
#: 表名可能被 f-string 吃掉（`f"UPDATE {TABLE} SET ..."` 抽出来是 `UPDATE  SET ...`），
#: 所以表名部分必须允许为空；否则带表名占位符的语句**一条都扫不到**。
_UPDATE = re.compile(r"\bUPDATE\b\s+[\w.`{}$]*\s*\bSET\b\s+(.+?)(?:\bWHERE\b|$)", re.S | re.IGNORECASE)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _split_top_level(text: str) -> list[str]:
    """按顶层逗号切分（跳过括号内的逗号，例如 `LEAST(a + b, c)`）。"""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _sql_literals(tree: ast.AST) -> list[str]:
    """取出文件里所有"可能是 SQL"的字符串。

    ★ 必须同时处理两种节点，否则会**静默漏扫**（第一次就踩到了）：
      * `ast.Constant`   —— 纯字符串，或**相邻字面量**被编译器合并后的结果；
      * `ast.JoinedStr`  —— f-string。`f"UPDATE {TABLE} SET " "a = %s, " ...`
        会把 f-string 与后面的相邻字面量合并成一个 `JoinedStr`，**整条 SQL 不是
        Constant**。只认 Constant 时，带表名占位符的那些语句（本仓绝大多数）全都扫不到，
        于是"0 命中"看起来像干净 —— 而真缺陷恰好就藏在里面。
    对 `JoinedStr` 取它的字面量片段拼接（丢掉 `{…}` 插值部分），足以解析 SET 子句。
    """
    texts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            texts.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            texts.append(
                "".join(
                    part.value
                    for part in node.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
            )
    return texts


def _scan() -> tuple[list[str], int, int]:
    """返回 (命中说明列表, 扫到的 UPDATE 条数, 扫到的 .py 个数)。"""
    findings: list[str] = []
    updates = 0
    files = 0
    for directory, dirnames, filenames in os.walk(_BACKEND_FPA):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = Path(directory) / name
            files += 1
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for text in _sql_literals(tree):
                if "UPDATE" not in text.upper():
                    continue
                for match in _UPDATE.finditer(text):
                    assigned: list[tuple[str, str]] = []
                    for part in _split_top_level(match.group(1)):
                        hit = _ASSIGN.match(part)
                        if hit:
                            assigned.append((hit.group(1).lower(), hit.group(2)))
                    if not assigned:
                        continue  # 只是散文里出现了 "UPDATE ... SET"，不算一条语句
                    updates += 1
                    for index, (column, _) in enumerate(assigned):
                        for later_column, later_expr in assigned[index + 1:]:
                            if column in set(_IDENT.findall(later_expr)):
                                findings.append(
                                    f"{path.relative_to(_BACKEND_FPA.parents[1])}: "
                                    f"SET 第 {index + 1} 项给 `{column}` 赋值，"
                                    f"第 {index + 2} 项（`{later_column}`）又读了它"
                                    f" —— MySQL 从左到右求值，读到的已是新值。"
                                )
    return findings, updates, files


def test_同一_UPDATE_里不得先写某列又在后面的_SET_项里读它():
    findings, updates, files = _scan()

    # 空集不得当成通过：先证明扫到了东西（同 registry_reconcile 的"扫描规模"手法）。
    assert files > 50, f"只扫到 {files} 个 .py，扫描器可能坏了"
    assert updates > 10, f"只扫到 {updates} 条 UPDATE ... SET，扫描器可能坏了"

    assert not findings, (
        "发现「先写后读同一列」的 UPDATE，MySQL 从左到右求值会让它读到新值"
        "（`delivery.verify` 的应收状态就是这么错标 settled 的）：\n  "
        + "\n  ".join(findings)
    )
