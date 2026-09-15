"""能力台账对账：把 `docs/CAPABILITY_REGISTRY.md` 与**运行时注册表**逐条对上。

## 为什么需要这个工具

本项目的权威清单是 `docs/CAPABILITY_REGISTRY.md`（69 条能力 / 21 条业务不变量），
而运行时事实是 `fpa.kernel.capability.REGISTRY`（由各域 `capabilities.py` 声明出来）。
两者是**同一件事的两处描述**——按 `DEVELOPMENT.md` §4 的纪律，这种地方必然漂移：

* 域名写错（`domain='master_data'` 写在 warehouse 的能力上）不会报错，只会静默错域；
* 能力声明了但漏挂不变量（`invariants=()`），运行时"看起来有这条能力"，
  但 21 条规则一条都没被强制——**早期版本的正是这个形态**；
* 文档 §4 声明了某条不变量，却没有一条能力真的强制它（"声明了但无强制能力"）；
* 内核里实现了不变量类型，但没有任何能力引用它（写了没用，等于没写）。

这四种缺口都能被机械检出，却都会**静默通过**所有既有自检。本工具把它们变成
可执行的断言，供五个域在开发期自查（`--check` 模式非零退出），而不是留到最后
由人工读 150 KB 文档对账。

## 文档解析口径（**从 markdown 解析，不手抄常量表**）

手抄一份"文档里有什么"的常量表，就是把"两处描述同一件事"又加回来一次——
文档改一次那份常量就失效，而且失效时没有任何信号。所以本工具的**全部**
文档侧输入都从 markdown 现场解析：

1. **能力清单** = §1.1–§1.9 各域小节里的第一张表格的**第一列**（反引号里的能力名），
   域名字取小节标题（`### 1.4 master_data — 13 条`）。小节标题里声明的条数会与
   实际解析到的行数**互相校验**，不一致直接报错（解析口径失效时必须吵，不能静默）。
2. **不变量规则** = §4 的表格行：`| # | 不变量 | 强制能力 | 早期实现位置 | 新系统声明形态 |`。
   * 规则号 = 第 1 列的 `#` 之前的数字；
   * 强制能力 = 该行中所有**在能力清单里出现过**的反引号 token（按能力清单匹配，
     因此"`早期版本：xxx.py`"这类引用不会被误当成能力）；
   * 声明形态类型名 = 该行 `invariant=[...]` 之后的 CamelCase 标识符
     （如 `NoNegativeStock(` / `CumulativeWithin(`）。
3. **域标题声明的条数** = 小节标题尾部的 `N 条`；与解析行数交叉校验。

## 三张缺口表（本工具的输出）

| 表 | 含义 | 对应的静默失败 |
|---|---|---|
| A | 文档声明了、运行时没有的能力 | 该能力的 URL 恒为 404，但"清单上看起来是有的" |
| B | 声明了不变量规则、却没有任何能力挂它 | 规则退化成建议（"声明了但无强制能力"） |
| C | 挂了不变量、但类型在文档 §4 里没有声明 | 规则来源不可追溯（可能是凭空发明或写错类型名） |

外加一张**双向覆盖表**：逐能力列出「文档 §4 声称的规则号」vs「运行时实际挂的类型」，
把两侧不一致单独列出（这条能捉住"挂错了不变量"这类最难发现的缺陷）。

## 用法

    python tools/registry_reconcile.py            # 人类可读报告（永远 0 退出）
    python tools/registry_reconcile.py --check    # 有缺口即退出 1（供各域开发期当自检入口）
    python tools/registry_reconcile.py --json     # 机器可读（供 agent / CI 消费）

退出码：`0` = 无缺口；`1` = 有缺口（仅 `--check`）；`2` = 文档解析失败（口径失效）；`3` = **组合根只装载了一部分域**（`--check` 下也算失败——降级必须留红，不能把「我只查了一半」读成「全绿」）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import fpa.bootstrap as bootstrap  # noqa: E402
from fpa.kernel.capability import REGISTRY  # noqa: E402
from fpa.kernel.workflow import RESOURCES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_DOC = ROOT / "docs" / "CAPABILITY_REGISTRY.md"

#: 域小节标题：`### 1.4 master_data — 13 条`
DOMAIN_SECTION = re.compile(r"^###\s+1\.\d+\s+([a-z_]+)\s+—\s+(\d+)\s*条\s*$")
#: 一级小节：`## 1. 能力清单…` / `## 4. 不变量清单`
TOP_SECTION = re.compile(r"^##\s+(\d+)\.\s")
#: 内核不变量模块（类型名的唯一事实来源）
KERNEL_INVARIANTS = Path(__file__).resolve().parents[1] / "backend" / "fpa" / "kernel" / "invariants.py"
#: 每个不变量类型都声明 `name = "TypeName"`；用它把 SQL 函数名从类型名里排除
NAME_ATTRIBUTE = re.compile('name = "([A-Za-z]+)"')

#: `RowAction` -> 前端/权限侧使用的**动作关键词**。
#:
#: 多数与动作同名；两处不是：
#:   * `view` 的能力叫 `*.list` / `*.get`（读能力按 list/get 命名）；
#:   * `edit` 的能力叫 `*.update`（`pond.update`），权限码也是 `pond.update`。
#: 其余（submit/verify/archive/cancel/confirm/close/delete）与能力名末端 token 同名 ——
#: 域代码里算 `allowed_actions` 时用的就是 `f"{resource}.{action}"` 组的权限码
#: （见 `domains/master_data/ponds.py::_decorate`），所以这条匹配口径与实现同源。
ROW_ACTION_TOKENS: dict[str, tuple[str, ...]] = {
    "view": ("list", "get", "summary", "ledger"),
    "edit": ("update",),
    "delete": ("delete",),
    "submit": ("submit",),
    "verify": ("verify",),
    "approve": ("approve",),
    "correct": ("correct",),
    "archive": ("archive",),
    "cancel": ("cancel",),
    "confirm": ("confirm",),
    "close": ("close",),
}

#: §4 行里"给人读的说明"与"参与解析的内容"的分隔标记。
#: 说明区写能力名时用反引号（`` `cost.period.close` ``），若整行扫描会被算成
#: 该行的强制能力——实测过一次假缺口。所以只解析标记之前的内容。
SCOPE_NOTE_MARKER = "⚠️"

#: `audit` 列的第四种合法取值：**固定路由**（不经 `CapabilityRunner` ⇒ 不产生审计行）。
#:
#: 为什么需要它：§1 表必须能表达"这条没有审计行"。原先那四条固定路由
#: （`auth.login` / `auth.logout` / `auth.me` / `meta.capabilities`）写的是 `summary`，
#: 于是文档让读者以为它们会被审计 —— 而实测**无审计行**（它们由 `web/app.py` 直接提供，
#: 不经执行器）。这是**文档对固定路由能力的描述错误**，不是"固定路由 vs 能力"的边界问题。
#:
#: 判据来源：识别它们**复用 [A2] 的固定路由分类**（那条分类的判据是真实 `url_map`，
#: 不是名字白名单）。这里**不给固定路由另造一套判据**。
AUDIT_NOT_EXECUTED = "—（固定路由，不经执行器）"

#: 表格分隔行：`|---|---|`
TABLE_SEPARATOR = re.compile(r"^\|\s*:?-{2,}.*\|\s*$")
#: `invariant=[...]` 之后的 CamelCase 类型名（排除掉 `invariant` 本身）
#: 严格贴紧左括号（`Foo(` / `Foo(x=1)`），因此 SQL 里的 `SUM (` / `COALESCE (`
#: 不会被误当成类型名——宽松匹配会把 §4 行内的早期实现 SQL 混进类型清单。
#: 取「新系统声明形态」列里的 `TypeName(`。注意早期实现 SQL 里的 `SUM(` / `COALESCE(`
#: 也会命中——所以解析结果必须再与内核**实际定义的类名**求交集，否则这些 SQL 函数
#: 会被当成不变量类型名（实测噪声，见 _types_in_declaration）。
#: 从 `invariant=[...]` 的方括号内容里提取类型名：以大写字母开头的标识符后紧跟 `(`。
TYPE_NAME = re.compile('([A-Z][A-Za-z0-9]{2,})' + chr(92) + 's*' + chr(92) + '(')
BACKTICK = re.compile(r"`([^`]+)`")
RULE_NUMBER = re.compile(r"^\s*(\d+)")
#: 反引号本身（用于剥掉单元格两端的代码围栏）
BACKTICK_CHAR = chr(96)

#: §1 表格 `kind` 列的已知取值（`docs/CAPABILITY_REGISTRY.md` §0.2）。
KNOWN_KINDS = {"read", "create", "update", "delete", "action"}

#: 散文式范围 -> 机械展开规则。"所有 `*.update`" 这类写法在文档里是给人读的，
#: 但 `kind` 列让它可以被**机械展开**：这正是"把人工核对降级成机械核对"的落点。
#: 每加一条都要能说清判据，否则它又变成一处人造清单。
KIND_PROSE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("所有 `*.update`（", ("update",)),
    ("所有 `*.update` 与写 `action`", ("update", "action")),
    ("所有 `*.create`", ("create",)),
)


def _derive_rule_kinds(scope_text: str) -> tuple[str, ...]:
    """把散文式强制范围展开成 `kind` 集合；展开不了就返回空元组（降级为人工核对）。"""
    text = scope_text.strip()
    for prefix, kinds in KIND_PROSE_PATTERNS:
        if text.startswith(prefix):
            return kinds
    return ()


#: 文档里的声明形态可能用简写（例如 `NoNegativeStock` 被写成 `NoNegativeStock` 之外
#: 的同义写法）。**空集是理想状态**：每加一条都要写下为什么，并且当内核补齐类型后，
#: 应优先改文档而不是加别名——别名本身就是"两处描述同一件事"。
TYPE_ALIASES: dict[str, str] = {}


@dataclass
class DocRule:
    number: int
    title: str
    capabilities: tuple[str, ...]
    types: tuple[str, ...]
    declaration: str = ""
    #: 该规则的"强制范围"在文档里写成什么（列出的能力名，或散文式范围）
    scope_text: str = ""
    #: 散文式范围被机械展开后的能力种类（`kind` 列取值），如 ("update",)
    derived_kinds: tuple[str, ...] = ()
    #: **逐能力**的类型集（规则横跨多个能力、且各自挂不同类型时才出现）
    #:
    #: 为什么需要它：`[D]` 原先的唯一判据是"这一行的类型集 ⊇ 该能力运行时的类型集"，
    #: 也就是把**一整行的类型按「所有列出的能力」分配**。规则 #20 就是这样被读错的：
    #: 它一行横跨两步（申请 + 核验），而 `StateTransition` 只属于**核验**那一步、
    #: `RequiredField(reason)` 只属于**申请**那一步。机械分配会让 `[D]` 报"实现漏挂"，
    #: 而真实原因往往是"这两步本来就不是同一组类型" —— 报错方向指错了地方。
    #: 逐能力声明一旦给出就以它为准；未列到的能力视为**不被本行覆盖**。
    types_by_capability: dict[str, tuple[str, ...]] = field(default_factory=dict)

    #: 逐能力声明了"替代强制"的**能力强名集合**（同一条规则可以部分能力走不变量、
    #: 部分能力走 DB 唯一键/服务预检）。判据与 `_declared_alternatives` 同源，但粒度是能力。
    alternative_by_capability: tuple[str, ...] = ()

    @property
    def is_kind_derived(self) -> bool:
        return bool(self.derived_kinds) and not self.capabilities

    def types_for(self, capability: str) -> tuple[str, ...]:
        """该能力在这条规则下**真正**被声称的类型集。"""
        if self.types_by_capability:
            return self.types_by_capability.get(capability, ())
        return self.types


@dataclass
class Reconcile:
    doc_capabilities: dict[str, str] = field(default_factory=dict)      # name -> domain
    doc_kinds: dict[str, str] = field(default_factory=dict)             # name -> kind（§1 表第 5 列）
    doc_methods: dict[str, str] = field(default_factory=dict)           # name -> GET/POST/...
    doc_paths: dict[str, str] = field(default_factory=dict)             # name -> /api/v1/...
    doc_counts: dict[str, int] = field(default_factory=dict)            # domain -> declared count
    #: §1 表第 8 列（`risk`）与第 12 列（`audit`）。**逐能力**的契约，与运行时一一对应。
    #:
    #: 为什么单列出来：这两列是"文档描述了运行时属性"的**直接**形态，
    #: 因而可以机械对账；而它们又各自出过一次真实的词汇分叉——
    #: `risk` 文档原先只有 `normal|high`（没有 `read`）、`audit` 原有一档
    #: 叫 `none`（❝不写审计❞，而运行时不判 `AuditPolicy`、审计行无条件写）。
    #: 两处分叉都不是"少写一行字"：它们让读者以为运行时的行为与真实不同。
    doc_risks: dict[str, str] = field(default_factory=dict)             # name -> read/normal/high
    doc_audits: dict[str, str] = field(default_factory=dict)            # name -> summary/before_after

    doc_rules: dict[int, DocRule] = field(default_factory=dict)
    runtime_capabilities: dict[str, Any] = field(default_factory=dict)  # name -> Capability
    kernel_types: set[str] = field(default_factory=set)


@dataclass
class Gaps:
    missing_capabilities: dict[str, str] = field(default_factory=dict)   # name -> domain
    #: [B] 文档声称有强制能力、但**没有任何能力挂它的类型**——规则退化成建议
    undefended_rules: dict[int, DocRule] = field(default_factory=dict)
    #: [B-声明] 文档显式声明了"替代强制方式"的规则（列出来供复核，不报警）
    #:
    #: 与 `[F]` 的 `row_action_notes` 同一形态：**免责不是静默的**，必须打印出来，
    #: 否则豁免就成了隐藏缺口。值 = (规则, 它指名了的已注册能力)。
    declared_alternatives: dict[int, tuple[DocRule, tuple[str, ...]]] = field(default_factory=dict)
    #: 散文式范围**无法**机械展开的规则——单列，不计入 any（保持门禁有意义）。
    manual_audit: dict[int, DocRule] = field(default_factory=dict)
    #: 散文式范围按 `kind` 展开后，**已实现**却没挂对应类型的能力：真缺口，计入 any。
    kind_gaps: dict[str, tuple[int, tuple[str, ...]]] = field(default_factory=dict)
    #: 同一范围的**尚未实现**能力：进度，不计入 any（否则五域开工期间门禁恒红）。
    kind_unimplemented: dict[int, tuple[str, ...]] = field(default_factory=dict)
    #: 由散文式范围（按 kind 展开）指定了使用者的类型——与"内核多写的类型"区分开
    kind_declared_types: tuple[str, ...] = ()
    #: 文档在 §4 声称要挂某类型、但该能力**尚未实现**（[A] 已覆盖；[D] 里单列说明）
    mismatches_unimplemented: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 文档声明了、运行时没有能力，但**实际有固定路由**提供（契约行，非漏实现）
    fixed_route_capabilities: dict[str, str] = field(default_factory=dict)
    #: 路由表取不到时的说明（不能静默当成"没有固定路由"）
    route_map_note: str = ""
    #: [1-R] 运行时已注册、文档 §1 未列（[A] 的对称面）
    undocumented_capabilities: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    #: 逐域：文档条数 vs 运行时条数（不等即漂移；漏登记在文档里表现为 registered > documented）
    domain_count_drift: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: [G] 租户维度盲区：租户盲的类型 -> 挂着它的能力（空 = 尚无能力挂它）
    tenant_blind: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: [G1] 真按租户键比较/查询的类型
    tenant_declared: tuple[str, ...] = ()
    #: [G2] 委托给 Scope（因而继承其盲区）的类型
    tenant_delegated: tuple[str, ...] = ()
    #: [H] 「LIMIT 1 且无 ORDER BY」的候选位置（不是缺陷判决）
    unordered_limit_one: tuple[tuple[str, int, str], ...] = ()
    #: [H] 的扫描规模（files / limit_any / limit_one / paged）——空结果必须能自证扫过东西
    limit_scan_stats: dict[str, int] = field(default_factory=dict)
    #: 资源声明的 row_action 在运行时没有对应能力（前端会渲染出点不动的按钮）；
    #: 值 = (匹配不到的动作, 能匹配上的动作)——后者一并给出，便于人工判断是不是命名约定不同
    row_action_gaps: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = field(default_factory=dict)
    #: 已在 `Resource.row_action_notes` 里写明理由的动作（不必再报警，但要在报告里可见）
    row_action_declared: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: [I] 逐能力：§1 的 `risk` / `audit` 列与运行时不符（值 = (文档值, 运行时值)）。
    #:
    #: **只在两边都"认识"该值时判缺陷**：`risk` 必须是内核 `Risk` 的合法值、
    #: `audit` 必须是 `summary`/`before_after`——否则是文档写了内核没有的词汇，
    #: 那属于下面 `unknown_*` 两类（更快暴露根因，不要混在一起报）。
    risk_mismatches: dict[str, tuple[str, str]] = field(default_factory=dict)
    audit_mismatches: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: 文档里出现了内核**不存在**的 `risk` / `audit` 取值（词汇分叉，见 Reconcile 的说明）。
    unknown_risk_values: dict[str, tuple[str, ...]] = field(default_factory=dict)
    unknown_audit_values: dict[str, tuple[str, ...]] = field(default_factory=dict)
    unattributed_types: dict[str, list[str]] = field(default_factory=dict)  # cap -> [type]
    mismatches: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = field(default_factory=dict)
    unreferenced_kernel_types: tuple[str, ...] = ()

    @property
    def any(self) -> bool:
        """是否存在**必须修**的缺口（`--check` 的判据）。

        `[I]`（`risk` / `audit` 两列逐能力错配）与 `[D]` 同类：都是「文档描述了运行时的
        属性，而两者不符」。既然 `[D]` 计入，`[I]` 也应当计入——否则这两列可以无声
        漂移，而它们各自出过一次真实的词汇分叉（见 `Reconcile.doc_risks` 的说明）。

        `[G]`（租户盲区）与 `[H]`（`LIMIT 1` 无 `ORDER BY`）**不在这里**：
        它们是候选清单、提供线索，不是缺陷判决（工具自己的 docstring 已如此定调）。
        """
        return bool(
            self.missing_capabilities
            or self.undefended_rules
            or self.unattributed_types
            or self.mismatches
            or self.unreferenced_kernel_types
            or self.kind_gaps
            or self.risk_mismatches
            or self.audit_mismatches
        )


def _split_row(line: str) -> list[str]:
    """把 markdown 表格行拆成单元格（去掉首尾竖线与两侧空白）。"""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


#: 「逐能力类型表」的表头标记。
#:
#: 规则横跨多个能力、而**各自挂不同类型**时，整行共享的 `invariant=[...]` 表达不了，
#: 机械按"所有列出的能力"分配就会产生**假缺口**（`[D]` 报"实现漏挂"）。文档只需在规则行
#: 下面紧跟一张小表，本工具就按能力读类型：
#:
#:     | 能力 | 本行真正强制它的类型 |
#:     |---|---|
#:     | `pond_status_change.request` | `RequiredField` |
#:     | `pond_status_change.verify`  | `StateTransition` |
#:
#: 为什么不在 `invariant=[...]` 里发明新语法：那张表同时承载"早期实现 SQL"与"新系统声明形态"，
#: 再塞一层嵌套语法会让它更难读；而 markdown 的相邻小表本来就是本项目已有的表达手段。
PER_CAPABILITY_TYPES_HEADER = "本行真正强制它的类型"

#: 逐能力表的一行：`| `cap.name` | T1、T2 |`
_PER_CAPABILITY_ROW = re.compile(r"^\|\s*`([a-z_][a-z0-9_.]*)`\s*\|(.*)$")


#: 「声明了替代强制方式」的标记词。文档的规则行里必须**逐字**写出它，工具才认。
#: 用中文短语而不是英文键名：它出现在给人读的裁决格里，写 `alternative_enforcement=True`
#: 会让那一格从"读起来是一句话"退化成"读起来是一个配置项"。
ALTERNATIVE_ENFORCEMENT_MARKER = "替代强制"


def _declared_alternatives(rule: DocRule, runtime_names: set[str]) -> tuple[str, ...]:
    """规则是否**显式声明**了"用别的方式强制"？返回它指名了的已注册能力。

    护栏（缺一不可，否则就是万能免死金牌）：
      1. 声明格里必须出现 `替代强制` 这个词；
      2. 且必须指名**至少一个已注册的能力**（凭空写一句"已用 DB 唯一键强制"不算）。
    """
    text = rule.declaration or ""
    if ALTERNATIVE_ENFORCEMENT_MARKER not in text:
        return ()
    named = tuple(
        sorted({token for token in BACKTICK.findall(text) if token in runtime_names})
    )
    return named


def _per_capability_types(
    lines: list[str],
    rule_line: str,
    known: set[str],
    kernel_names: tuple[str, ...],
) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    """读取紧跟规则行之后的「逐能力类型表」。

    返回 ``(能力 -> 类型集, 声明了「替代强制」的能力名)``；没有该表时返回两个空值。

    判据刻意严格（**不能**变成"任何相邻表格都算"）：只有表头里出现
    ``PER_CAPABILITY_TYPES_HEADER`` 才认，且只认紧跟其后的连续表格行。
    松一点会让 §4 里别的表格被误读成类型声明 —— 那是"猜"而不是"读"。
    """
    try:
        start = lines.index(rule_line)
    except ValueError:
        return {}, ()

    # 表头未必紧跟在规则行后面：规则行的"裁决/说明"格可以**换行续写**（markdown 允许
    # 表格单元格内容不闭合），于是中间会夹着若干纯文本续行。所以扫描要跳过空行与续行，
    # 但**遇到下一条规则行就必须停** —— 否则会把下一条规则的表读成本条的。
    # （实测踩过：规则 #5 的表头在规则行后第 3 行，而第 2 行是「该清单必须完备…」续行；
    #   只跳过空行的版本直接返回空表，逐能力归属**静默失真**。）
    index = start + 1
    header_at: int | None = None
    limit = min(len(lines), start + 60)
    while index < limit:
        line = lines[index]
        if line.startswith("|"):
            cells_here = _split_row(line)
            if cells_here and RULE_NUMBER.match(cells_here[0]):
                break  # 下一条规则行 —— 本条的表格区到这里结束
        if PER_CAPABILITY_TYPES_HEADER in line:
            header_at = index
            break
        index += 1
    if header_at is None:
        return {}, ()
    index = header_at + 1
    if index < len(lines) and TABLE_SEPARATOR.match(lines[index]):
        index += 1

    found: dict[str, tuple[str, ...]] = {}
    alternatives: list[str] = []
    while index < len(lines) and lines[index].startswith("|"):
        match = _PER_CAPABILITY_ROW.match(lines[index])
        if match:
            capability = match.group(1)
            if capability in known:
                # 这一格只放类型名，没有早期实现 SQL 的噪声（`SUM(` / `COALESCE(`），
                # 所以**不能**复用 `_types_in_declaration`：它的 `TYPE_NAME` 要求
                # `Name(` 形态（正是为了过滤 SQL 函数），而这里的类型名是**纯名字**。
                # 复用会让整张逐能力表解析出全空 —— 表现为"文档没写"，那是静默失真。
                found[capability] = tuple(
                    name
                    for name in type_cell_names(match.group(2))
                    if not kernel_names or name in kernel_names
                )
                if ALTERNATIVE_ENFORCEMENT_MARKER in match.group(2):
                    alternatives.append(capability)
        index += 1
    return found, tuple(alternatives)


#: 逐能力表里的类型名：反引号包裹（`` `RequiredField` ``）或裸名字。
_PER_CAPABILITY_TYPE_NAME = re.compile(r"`([A-Z][A-Za-z0-9]{2,})`|\b([A-Z][A-Za-z0-9]{2,})\b")


def type_cell_names(cell: str) -> tuple[str, ...]:
    """从逐能力表的类型格里取类型名（去重、保序）。"""
    found: list[str] = []
    for match in _PER_CAPABILITY_TYPE_NAME.finditer(cell):
        name = match.group(1) or match.group(2)
        if name not in found:
            found.append(name)
    return tuple(found)


def _declaration_cell(cells: list[str]) -> str:
    """取出「新系统声明形态」那一格的内容，容忍它被 `|` 拆成多个单元格。

    起始格用 `invariant=[` 定位——不能写死列号：那一格里还写着 `machine="pond|batch|..."`，
    单元格数量随数据变化。
    """
    start = next((i for i, cell in enumerate(cells) if "invariant=[" in cell), None)
    if start is None:
        return cells[-1] if cells else ""
    text = "|".join(cells[start:])
    fence = text.find("`", text.find("invariant=["))
    return text[:fence] if fence != -1 else text


def _types_in_declaration(text: str, kernel_names: tuple[str, ...] = ()) -> tuple[str, ...]:
    """从「新系统声明形态」单元格里取出 `invariant=[...]` 中的类型名。

    只取方括号**内部**的内容：同一格里还有早期实现 SQL（`SUM(` / `COALESCE(`），
    整格扫描会把它们当成不变量类型名（实测噪声）。

    注意正则**不要求闭合的 `]`**：表格行里有未闭合的方括号（声明文本里写了 `[` 而没写
    对应的 `]`，例如 `invariant=[Foo(x=["a","b"])`），要求闭合会让这些行解析出**空集**，
    进而被误判成"该规则没有声明形态"。宁可多解析一些，也不要静默返回空集。
    """
    found: list[str] = []
    for segment in re.findall(r"invariant=\[([^\]]*)", text):
        for match in TYPE_NAME.finditer(segment):
            name = TYPE_ALIASES.get(match.group(1), match.group(1))
            if kernel_names and name not in kernel_names:
                continue  # 早期实现 SQL 里的 SUM( / COALESCE( 之类
            if name not in found:
                found.append(name)
    if not found and not kernel_names:
        # 拿不到内核类名时退化为整格扫描——宁可多报，也不能静默返回空集
        for match in TYPE_NAME.finditer(text):
            name = TYPE_ALIASES.get(match.group(1), match.group(1))
            if name not in ("invariant", "Invariant") and name not in found:
                found.append(name)
    return tuple(found)


def kernel_type_names() -> tuple[str, ...]:
    """内核**实际定义**的不变量类型名（从源码取，不在本工具里手抄一份）。

    为什么要这一步：`invariant=[...]` 这一格里还夹着早期实现 SQL（`COALESCE(` / `SUM(`），
    只靠"大写字母开头的标识符后跟 `(`"会把 SQL 函数当成类型名（实测噪声）。把解析结果
    与内核类名求交集，既消掉噪声，又让"文档写了、内核没有"的类型名显式落空。
    """
    if not KERNEL_INVARIANTS.is_file():
        raise SystemExit(f"内核不变量模块不存在：{KERNEL_INVARIANTS}")
    source = KERNEL_INVARIANTS.read_text(encoding="utf-8")
    return tuple(sorted(set(NAME_ATTRIBUTE.findall(source))))


def _looks_like_domain_heading(line: str) -> bool:
    """这一行是不是"本该是域小节标题、但形态不合口径"的行。

    判据：`##`/`###` 开头，且第二个空白分隔的 token 形如 `1.<一位数字>`（即 §1.1–§1.9；
    §1.10 及以后是统计/历史小节，不算）。命中即说明文档改了标题形态——此时必须报错。
    """
    if not (line.startswith("### ") or line.startswith("## ")):
        return False
    parts = line.split()
    if len(parts) < 2:
        return False
    token = parts[1]
    return token.startswith("1.") and len(token) == 3 and token[2].isdigit() and token[2] != "0"


def parse_doc(path: Path, kernel_names: tuple[str, ...] = ()) -> Reconcile:
    """解析权威清单。

    能力清单用**两遍**扫：先按域小节标题切出行区间，再在区间内取第一张表的行。
    初版是单遍状态机（遇到标题就切换当前域、遇到空行就停止收集），有两个静默失败：
    标题形态变化时整段域被算进**上一个域**（总量守恒发现不了），以及表头与首行之间
    的空行被当成"表格结束"。切成区间后这两类错误都有明确的落点，读代码时也能一眼
    看出"这一节的范围是什么"。
    """
    if not path.is_file():
        raise SystemExit(f"文档不存在：{path}")
    lines = path.read_text(encoding="utf-8").split("\n")

    # ---- 1. 能力清单（§1.1–§1.9）：先切域区间，再取每节第一张表 --------------
    sections: list[tuple[str, int, int]] = []   # (domain, 起始行, 结束行)
    for index, line in enumerate(lines):
        heading = DOMAIN_SECTION.match(line)
        if heading:
            sections.append((heading.group(1), index, len(lines)))
        elif _looks_like_domain_heading(line):
            # 能力清单小节里出现了非预期形态的标题（例如 `### 1.4 master_data（13 条）`）。
            # 静默跳过它的代价是：那一节的能力被算进上一个域，且总量守恒也发现不了。
            if sections:
                domain, begin, _ = sections[-1]
                sections[-1] = (domain, begin, index)
            if re.match(r"^###\s+1\.", line):
                raise SystemExit(
                    f"遇到无法识别的能力清单小节标题：{line.strip()!r}——"
                    "解析口径已失效（预期形如 `### 1.4 master_data — 13 条`），"
                    "请先修本工具再相信它的结论。"
                )
            continue

    doc_capabilities: dict[str, str] = {}
    doc_kinds: dict[str, str] = {}
    doc_methods: dict[str, str] = {}
    doc_paths: dict[str, str] = {}
    doc_risks: dict[str, str] = {}
    doc_audits: dict[str, str] = {}
    doc_counts: dict[str, int] = {}
    for domain, begin, end in sections:
        declared = DOMAIN_SECTION.match(lines[begin])
        doc_counts[domain] = int(declared.group(2)) if declared else 0
        rows: list[tuple[str, str, str, str]] = []
        in_table = False
        for line in lines[begin + 1:end]:
            if line.startswith("|"):
                if TABLE_SEPARATOR.match(line):
                    continue
                cells = _split_row(line)
                if cells and cells[0] == "name":
                    if rows:
                        break  # 第一张表已收完，后面的表格（统计表等）不是能力清单
                    in_table = True
                    continue
                if not in_table:
                    continue
                name = cells[0].strip("`").strip() if cells else ""
                if not name or " " in name:
                    continue
                # 第 5 列是 `kind`。它让"所有 *.update"这类散文式范围变成可机械展开的集合，
                # 所以解析它；缺失时置空，由 _derive_rule_kinds 决定是否降级为人工。
                kind = cells[4].strip("`* ").strip() if len(cells) > 4 else ""
                # method / path 是 [A2] 分类的判据：拿它们去实际路由表里找。
                method = cells[1].strip("`* ").strip().upper() if len(cells) > 1 else ""
                path = cells[2].strip("`* ").strip() if len(cells) > 2 else ""
                # 第 8 列 `risk` / 第 12 列 `audit`：**逐能力的运行时属性**，
                # 因此可以机械对账（见 Reconcile 里那两个字段的说明）。
                # 用同一套 strip 口径（`*` 与空格也去掉），因为表格里有的格子会加粗。
                risk = cells[7].strip("`* ").strip() if len(cells) > 7 else ""
                audit = cells[11].strip("`* ").strip() if len(cells) > 11 else ""
                rows.append((name, kind, method, path, risk, audit))
            elif in_table and rows and line.strip() == "":
                break  # 表后第一个空行即表格结束
        for name, kind, method, path, risk, audit in rows:
            if name in doc_capabilities and doc_capabilities[name] != domain:
                raise SystemExit(f"能力 {name} 同时出现在域 {doc_capabilities[name]} 与 {domain}")
            doc_capabilities[name] = domain
            doc_kinds[name] = kind
            doc_methods[name] = method
            doc_paths[name] = path
            doc_risks[name] = risk
            doc_audits[name] = audit

    parsed_counts: dict[str, int] = {}
    for domain in doc_capabilities.values():
        parsed_counts[domain] = parsed_counts.get(domain, 0) + 1
    for domain, declared_count in doc_counts.items():
        if parsed_counts.get(domain, 0) != declared_count:
            raise SystemExit(
                f"域 `{domain}` 标题声明 {declared_count} 条，实际解析到 {parsed_counts.get(domain, 0)} 条"
                "——文档解析口径已失效，请先修本工具再相信它的结论"
            )
    declared_total = sum(doc_counts.values())
    if len(doc_capabilities) != declared_total:
        raise SystemExit(
            f"解析到 {len(doc_capabilities)} 条能力，但各域标题声明的条数合计 {declared_total} 条"
            "——文档解析口径已失效，请先修本工具再相信它的结论"
        )

# ---- 2. 不变量规则（§4）：规则号 / 强制能力 / 声明形态类型名 --------------
    known = set(doc_capabilities)
    doc_rules: dict[int, DocRule] = {}
    in_rules = False
    for line in lines:
        top = TOP_SECTION.match(line)
        if top:
            in_rules = top.group(1) == "4"
            continue
        if not in_rules or not line.startswith("|") or TABLE_SEPARATOR.match(line):
            continue
        cells = _split_row(line)
        if len(cells) < 3:
            continue
        number = RULE_NUMBER.match(cells[0])
        if not number or cells[1] in {"不变量", ""}:
            continue
        # 强制范围**只从「强制范围」那一格**（cells[2]）取。
        #
        # 曾经取的是"整行里 `SCOPE_NOTE_MARKER` 之前的全部反引号 token"，于是
        # **说明文字里出现的**能力名会被算成"文档声称的强制能力" —— 实例：规则 #12 的
        # 说明里写了"`batch.update` 同样 kind=update，但挂 StatusAllowsEdit 不适用"，
        # 于是 `[D]` 报"batch.update 漏挂 StatusAllowsEdit"，而那是一句**否命题**。
        # 与 `SCOPE_NOTE_MARKER`（`⚠️`）当初要解决的问题同型：**声明与说明必须分离**，
        # 而不是靠作者记得"不要在说明里提别的能力名"。
        scope_cell = cells[2] if len(cells) > 2 else ""
        caps_text = scope_cell.split(SCOPE_NOTE_MARKER, 1)[0]
        caps: list[str] = []
        for token in BACKTICK.findall(caps_text):
            if token in known and token not in caps:
                caps.append(token)
        # 声明形态可能**跨列**：文本里写了 `machine="pond|batch|..."`，
        # 其中的 `|` 会被当成 markdown 单元格分隔符。只取 cells[-1] 会拿到半截文本
        # （实测：规则 #14 因此解析出**空类型集**，被误判成"该规则没有声明形态"）。
        declaration = _declaration_cell(cells)
        row_types = _types_in_declaration(declaration, kernel_names)
        scope_text = cells[2] if len(cells) > 2 else ""
        # 逐能力表紧跟规则行之后（见 PER_CAPABILITY_TYPES_HEADER）
        per_capability, alternative_caps = _per_capability_types(
            lines, line, known, kernel_names
        )
        doc_rules[int(number.group(1))] = DocRule(
            number=int(number.group(1)),
            title=cells[1],
            capabilities=tuple(caps),
            types=tuple(row_types),
            declaration=declaration,
            scope_text=scope_text,
            derived_kinds=_derive_rule_kinds(scope_text),
            types_by_capability=per_capability,
            alternative_by_capability=alternative_caps,
        )
    if not doc_rules:
        raise SystemExit("§4 没有解析到任何不变量规则——解析口径失效")
    if not doc_capabilities:
        raise SystemExit("§1 没有解析到任何能力——解析口径失效")
    for name, kind in doc_kinds.items():
        if kind not in KNOWN_KINDS:
            raise SystemExit(
                f"能力 {name} 的 kind 列解析为 {kind!r}，不在已知取值 {sorted(KNOWN_KINDS)} 内"
                "——§1 表格结构变了，解析口径已失效"
            )
    return Reconcile(
        doc_capabilities=doc_capabilities,
        doc_kinds=doc_kinds,
        doc_methods=doc_methods,
        doc_paths=doc_paths,
        doc_risks=doc_risks,
        doc_audits=doc_audits,
        doc_counts=doc_counts,
        doc_rules=doc_rules,
    )


def _flask_path(template: str) -> str:
    """把能力声明的路径模板转成 Flask 规则文本：`{pond_id}` -> `<int:pond_id>`。

    与 dp.web.app::_flask_path **同一条规则**（那里用的正则同样是「花括号+小写标识符」）。
    这里重写一份是刻意的取舍：import 私有函数会把"路径模板怎么写"这件事绑到 web 层的
    实现细节上，而本工具需要它在没有 Flask app 时也能工作（例如只解析文档的自检）。
    """
    return re.sub(r"\{([a-z_][a-z0-9_]*)\}", r"<int:\1>", template)


def _available_routes() -> set[tuple[str, str]] | None:
    """真实 Flask app 的路由表（方法, 路径模板）集合。

    取不到就返回 `None`——**不能返回空集**：空集会让所有"文档声明了但没注册"的能力
    都被当成 [A] 缺口，看起来像"缺了很多"，实际只是路由表没读到（空输入不得当成结论，
    这条纪律在 `check_source_hygiene.py::walk()` 与 `source_index.py` 里都写过一遍）。
    """
    try:
        from fpa.factory import build_app
        from fpa.settings import Settings
    except Exception:  # noqa: BLE001
        return None
    try:
        # 显式传一个空注册表：本函数只关心**固定路由**（meta / auth / agent 等），
        # 能力路由不在判据内，所以不需要装载全部域（也就不依赖组合根能不能装起来）。
        from fpa.kernel.capability import Registry as _Registry

        app = build_app(
            registry=_Registry(),
            settings=Settings.from_env({"APP_ENV": "test", "SECRET_KEY": "reconcile"}),
        )
    except Exception:  # noqa: BLE001
        return None
    found: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        for method in rule.methods or ():
            if method in {"HEAD", "OPTIONS"}:
                continue
            found.add((method, rule.rule))
    return found


#: 租户维度的键名线索。刻意宽松：宁可把类型算成"有租户意识"，也不要制造假缺陷。
TENANT_HINTS = ("organization_id", "organization", "org_id", "tenant", "enterprise", "company_id")


def tenant_blind_types() -> dict[str, bool]:
    """类型名 -> 该类型的实现里是否**完全没有**租户键（True = 盲）。

    判据与证据脚本 `%TEMP%\\fpa_verify\\probe_tenant_blind.py` 一致：按 `^class ` 切块、
    块内搜线索词。**不做语义判断**——语义判断留给人工与 e2e（那正是 [G] 存在的原因：
    把"该查而没查"的候选集缩到可人工过一遍的规模）。
    """
    if not KERNEL_INVARIANTS.is_file():
        return {}
    lines = KERNEL_INVARIANTS.read_text(encoding="utf-8").split("\n")
    blocks: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^class (\w+)", line)
        if match:
            blocks.append((match.group(1), index))
    blocks.append(("__end__", len(lines)))
    result: dict[str, bool] = {}
    for (name, begin), (_, end) in zip(blocks, blocks[1:]):
        if name == "__end__":
            continue
        block = "\n".join(lines[begin:end])
        result[name] = not any(hint in block for hint in TENANT_HINTS)
    return result


def _strip_comments_and_docstrings(text: str) -> str:
    """只看**会执行的代码**：去掉 `#` 注释与三引号文档串。

    这一步是 [G] 判据的关键。旧版线索词里含裸 `organization`，于是
    `ReferencedStatus` 因为一句注释被算成"有租户意识"——而它只调 `scope.allows_row`，
    `Scope` 又不比 `organization_id`。**假阴性最坏**：优先级清单把不安全的说成安全，
    就没人去看挂它的那 6 条能力了。
    """
    without_docstrings = re.sub(r'"""(?:.|\n)*?"""', "", text)
    return "\n".join(line.split("#", 1)[0] for line in without_docstrings.split("\n"))


def tenant_dimension() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """返回 (G1 真按租户键, G2 委托 Scope, 两者皆无)。

    判据只看代码：G1 出现 `organization_id`；G2 出现 `scope.allows_row` / `scope.predicate`。
    """
    if not KERNEL_INVARIANTS.is_file():
        return (), (), ()
    lines = KERNEL_INVARIANTS.read_text(encoding="utf-8").split("\n")
    blocks: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^class (\w+)", line)
        if match:
            blocks.append((match.group(1), index))
    blocks.append(("__end__", len(lines)))
    g1: list[str] = []
    g2: list[str] = []
    neither: list[str] = []
    for (name, begin), (_, end) in zip(blocks, blocks[1:]):
        if name == "__end__":
            continue
        code = _strip_comments_and_docstrings("\n".join(lines[begin:end]))
        has_key = "organization_id" in code
        delegates = "scope.allows_row" in code or "scope.predicate" in code
        if has_key:
            g1.append(name)
        if delegates:
            g2.append(name)
        if not has_key and not delegates:
            neither.append(name)
    return tuple(g1), tuple(g2), tuple(neither)


#: `LIMIT 1` 的确定性扫描范围（源码，不含 tools/ 的 e2e 夹具）。
ORDER_SCAN_ROOTS = ("backend/fpa/kernel", "backend/fpa/domains")


def unordered_limit_one() -> tuple[tuple[tuple[str, int, str], ...], dict[str, int]]:
    """扫出「`LIMIT 1` 且同一语句窗口内没有 `ORDER BY`」的位置（[H]），并返回扫描规模。

    **这是候选清单，不是缺陷清单**：由唯一键保证至多一行的查询是安全的
    （例如 `UniqueCode` 查的是唯一键列）。静态扫描分不清这两者，所以 [H] 只报位置 +
    该行 SQL 片段，由人工/域负责人标注"哪几条由唯一约束兜住"。

    返回的第二个值是**扫描规模**：`files` / `limit_any`（出现 LIMIT 的行数）/
    `limit_one`（出现 LIMIT 1 的行数）/ `paged`（`LIMIT %s OFFSET %s` 的行数）。
    **为什么必须报它**：一条判据报"0 处"时，得先确认它**有没有真的扫到东西**——
    purchase-dev 报"我域 0 处可疑"，紧接着说明"0 处不是干净，是我域根本不用 `LIMIT 1`"
    （采购域只用分页 `LIMIT %s OFFSET %s`）。空输入不是通过，是没检查。
    """
    root = Path(__file__).resolve().parents[1]
    found: list[tuple[str, int, str]] = []
    stats = {"files": 0, "limit_any": 0, "limit_one": 0, "paged": 0}
    for relative in ORDER_SCAN_ROOTS:
        base = root / relative
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [name for name in dirnames if name != "__pycache__"]
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                path = Path(dirpath) / filename
                stats["files"] += 1
                source = _strip_comments_and_docstrings(path.read_text(encoding="utf-8"))
                lines = source.split("\n")
                for index, line in enumerate(lines):
                    upper = line.upper()
                    if "LIMIT" in upper:
                        stats["limit_any"] += 1
                        if re.search(r"LIMIT\s+%S\s+OFFSET", upper):
                            stats["paged"] += 1
                        if re.search(r"LIMIT\s+1\b", upper):
                            stats["limit_one"] += 1
                    if "SELECT" not in upper:
                        continue
                    window = "\n".join(lines[index:index + 5]).upper()
                    if re.search(r"LIMIT\s+1\b", window) and "ORDER BY" not in window:
                        found.append((str(path.relative_to(root)), index + 1, line.strip()[:100]))
    return tuple(found), stats


#: 部分装载时的说明（不能静默把它当成"全部域都在这儿"）。
load_note = ""


def parse_runtime() -> tuple[dict[str, Any], set[str]]:
    """取运行时能力与内核类型。

    **某个域 import 失败时不再整体抛错**：`bootstrap.load_all()` 是"一个域坏了就整锅端"，
    而本工具的价值恰恰是在那种时刻告诉人"现在缺了什么"。所以这里降级为**部分装载**，
    但把事实写进 `load_note` 并打印出来——"只覆盖已装载的域"必须是读得到的结论，
    不能让人以为眼前这张表是完整的。
    """
    global load_note
    try:
        bootstrap.load_all()
        load_note = ""
    except Exception as exc:  # noqa: BLE001
        load_note = (
            f"组合根只装载了一部分域：{type(exc).__name__}: {exc}"
            "（本表只覆盖装载成功的域；请以 bootstrap.load_all() 全绿后重跑为准）"
        )
    runtime = {item.name: item for item in REGISTRY.all()}
    from fpa.kernel import invariants as invariant_module

    kernel_types = {
        name
        for name in getattr(invariant_module, "__all__", ())
        if isinstance(getattr(invariant_module, name, None), type)
    }
    for name in dir(invariant_module):
        obj = getattr(invariant_module, name)
        if isinstance(obj, type) and hasattr(obj, "name") and getattr(obj, "name") is not None:
            kernel_types.add(getattr(obj, "name"))
    return runtime, kernel_types

def compare(doc: Reconcile, runtime: dict[str, Any], kernel_names: tuple[str, ...]) -> Gaps:
    """把文档侧与运行时侧逐条对上，产出四类缺口。

    只做机械比对，不做判断：
      * 文档有、运行时无 —— 能力没实现或名字写错；
      * 文档声称的每条规则，是否有**任意**一条运行时能力挂了它的类型；
      * 运行时挂的类型是否都能在文档里找到出处；
      * 逐能力：文档声称的类型集合是否被运行时实际类型集合包含。
    """
    gaps = Gaps()
    kernel_set = set(kernel_names)

    # 1-R. 运行时已注册、文档 §1 未列 —— [A] 的对称面。
    # 没有这张表时，"漏登记的能力"只能靠人偶然发现（实例：`pond.archive` 曾在代码里
    # 注册很久而文档一直没列，八张表没有一张会报它）。
    for name, cap in sorted(runtime.items()):
        if name not in doc.doc_capabilities:
            gaps.undocumented_capabilities[name] = (
                str(cap.domain),
                f"{cap.method} {cap.path}",
                str(cap.kind),
            )

    # 逐域条数对账：文档条数 vs 运行时条数。
    # **不判定为缺陷**（五域还在施工，文档条数大于运行时是正常进度）；它报的是"哪一对
    # 数字不等"，供人工判"该补文档还是该补实现"——`pond.archive` 那次就是这一栏能看出来。
    for domain in set(doc.doc_counts) | {cap.domain for cap in runtime.values()}:
        documented = sum(1 for value in doc.doc_capabilities.values() if value == domain)
        registered = sum(1 for cap in runtime.values() if str(cap.domain) == domain)
        if documented != registered:
            gaps.domain_count_drift[domain] = (documented, registered)

    # A. 文档声明了、运行时没有的能力。
    # 先按**路由表**过滤掉"由固定路由提供"的契约行（判据是实际 url_map，不是名字/白名单）。
    declared_only = {
        name: domain for name, domain in doc.doc_capabilities.items() if name not in runtime
    }
    available = _available_routes()
    if available is None:
        gaps.route_map_note = "（未取到真实路由表，无法区分「契约行」与「漏实现」，本表的 [A] 可能偏大）"
    else:
        for name in list(declared_only):
            method, path = doc.doc_methods.get(name, ""), doc.doc_paths.get(name, "")
            if method and path and (method.upper(), _flask_path(path)) in available:
                gaps.fixed_route_capabilities[name] = f"{method.upper()} {path}"
                declared_only.pop(name)
    for name, domain in declared_only.items():
        gaps.missing_capabilities[name] = domain

    # 运行时实际挂载的类型 -> 能力清单
    attached_by_type: dict[str, list[str]] = {}
    for cap_name, cap in runtime.items():
        for invariant in cap.invariants:
            type_name = getattr(invariant, "name", type(invariant).__name__)
            attached_by_type.setdefault(type_name, []).append(cap_name)

    # B. 文档 §4 的规则：没有任何能力挂它
    #
    # **先看"是否声明了替代强制"**：有些规则的强制点是**结构上不是不变量**的东西
    # （DB 唯一键 / 服务侧预检 / 状态机的原子 UPDATE）。把这类一律算成"退化成建议"是
    # 把「没挂」与「用别的方式强制了」混为一谈 —— 工具分辨不出这两者，只有文档能说清。
    # 判据（与 `[F]` 的 `row_action_notes` 同一形态）：
    #   * 规则行的声明格里必须**显式写出** `替代强制` 这一段；
    #   * 且必须指名至少一个**已注册能力**（否则就是"任何规则都能声明豁免"的万能后门）；
    # 满足则**列出来供复核、不报警** —— 打印在 [B-声明] 里，绝不静默跳过。
    for number, rule in doc.doc_rules.items():
        if rule.types and any(attached_by_type.get(type_name) for type_name in rule.types):
            continue
        alternatives = _declared_alternatives(rule, set(runtime))
        if alternatives:
            gaps.declared_alternatives[number] = (rule, alternatives)
            continue
        if rule.capabilities:
            gaps.undefended_rules[number] = rule
            continue
        if rule.is_kind_derived:
            # 散文式范围（「所有 `*.update`」）已按 §1 的 `kind` 列展开成能力集合。
            # **只核对已实现的能力**：尚未实现的能力属于进度，不是缺陷——
            # 把两者混在一起会让这把尺子在五域开工期间永久变红、失去信号。
            wanted = [
                name
                for name, kind in doc.doc_kinds.items()
                if kind in rule.derived_kinds
            ]
            implemented = [name for name in wanted if name in runtime]
            todo = tuple(name for name in wanted if name not in runtime)
            if todo:
                gaps.kind_unimplemented[number] = todo
            for name in implemented:
                live = {
                    getattr(inv, "name", type(inv).__name__)
                    for inv in runtime[name].invariants
                }
                if not (set(rule.types) & live):
                    gaps.kind_gaps[name] = (number, rule.types)
            continue
        # 既没有能力清单、也不是可展开的散文式范围——只能人工核对。
        gaps.manual_audit[number] = rule

    # C. 挂了不变量、但类型在文档里没有出处（或该类型内核根本没实现）
    documented_types = {name for rule in doc.doc_rules.values() for name in rule.types}
    for cap_name, cap in runtime.items():
        for invariant in cap.invariants:
            type_name = getattr(invariant, "name", type(invariant).__name__)
            if type_name not in documented_types or type_name not in kernel_set:
                bucket = gaps.unattributed_types.setdefault(cap_name, [])
                if type_name not in bucket:
                    bucket.append(type_name)

    # D. 逐能力：文档 §4 声称的类型集合必须是运行时实际类型集合的**子集**
    claimed: dict[str, set[int]] = {}
    for number, rule in doc.doc_rules.items():
        for cap_name in rule.capabilities:
            claimed.setdefault(cap_name, set()).add(number)
    for cap_name in sorted(set(runtime) | set(claimed)):
        # 逐能力表优先（规则横跨多能力、各自挂不同类型时，整行类型集不能按"所有列出的能力"分）
        doc_types = {
            t
            for number in claimed.get(cap_name, set())
            for t in doc.doc_rules[number].types_for(cap_name)
        }
        # 逐能力声明了「替代强制」的能力不进 [D]：那一条的类型**故意**不由不变量强制。
        # 与 [B-声明] 同一护栏：必须逐字写「替代强制」且能力名已注册（见解析处）。
        if any(
            cap_name in doc.doc_rules[number].alternative_by_capability
            for number in claimed.get(cap_name, set())
        ):
            continue
        cap = runtime.get(cap_name)
        live_types = {
            getattr(inv, "name", type(inv).__name__)
            for inv in (cap.invariants if cap is not None else ())
        }
        if not doc_types.issubset(live_types):
            gaps.mismatches[cap_name] = (tuple(sorted(doc_types)), tuple(sorted(live_types)))

    # D-补：文档声称要挂、但能力根本还没实现的那部分。
    # 它本来由 [A] 覆盖；但在 [D] 里单列，是为了让"漏挂已被穷举"这句话成立——
    # 否则读者会以为 §4 声称的 13 条能力漏挂只在 [D] 里出现的那几条。
    for name, numbers in sorted(claimed.items()):
        if name in runtime:
            continue
        types = tuple(
            sorted({t for number in numbers for t in doc.doc_rules[number].types_for(name)})
        )
        if types:
            gaps.mismatches_unimplemented[name] = types

    # F. row_actions × REGISTRY：资源声明的动作必须有对应能力。
    # 判据只有一条：某资源在状态机里声明了动作 A，运行时就该有一条同资源的写能力，
    # 其 `kind` 等于 A 对应的 kind（view -> read、edit -> update、其余 -> action）。
    # 只报"已实现的资源"（有能力的资源），未实现的资源属于 [A] 的进度。
    actions_by_resource: dict[str, set[str]] = {}
    for resource in RESOURCES.all():
        if resource.workflow is None:
            continue
        for state in resource.workflow.states:
            for action in state.actions:
                actions_by_resource.setdefault(resource.name, set()).add(str(action))
    for resource, actions in sorted(actions_by_resource.items()):
        caps = [cap for cap in runtime.values() if cap.resource == resource]
        if not caps:
            continue  # 该资源还没有任何能力——进度问题，[A] 会报
        # 该资源所有能力的"末端 token"（`cost.entry.confirm` -> `confirm`）。
        tokens = {name.split(".")[-1] for name in (cap.name for cap in caps)}
        notes = getattr(RESOURCES.find(resource), "row_action_notes", {}) or {}
        missing: list[str] = []
        matched: list[str] = []
        declared: list[str] = []
        for action in sorted(actions):
            keywords = ROW_ACTION_TOKENS.get(action)
            if not keywords:
                continue
            if tokens & set(keywords):
                matched.append(action)
            elif action in notes:
                # 资源声明里写明了"这个动作有意不配能力"及其理由 -> 不再重复报警。
                # 结论写回**声明处**（不是工具里的白名单）：理由跟着数据走，不会漂移。
                declared.append(f"{action}（{notes[action]}）")
            else:
                missing.append(action)
        if missing or declared:
            gaps.row_action_declared[resource] = tuple(declared)
            if missing:
                gaps.row_action_gaps[resource] = (tuple(missing), tuple(matched))

    # G. 租户维度盲区：类型实现里没有任何租户键，却已被能力挂上（或尚未被挂）。
    # 见 `tenant_blind_types()` 的 docstring：这是**优先级清单**，不是缺陷判决。
    g1, g2, neither = tenant_dimension()
    gaps.tenant_declared = g1
    gaps.tenant_delegated = g2
    gaps.unordered_limit_one, gaps.limit_scan_stats = unordered_limit_one()
    blind = {name: True for name in neither}
    if blind:
        attached_caps: dict[str, list[str]] = {}
        for cap_name, cap in runtime.items():
            for invariant in cap.invariants:
                type_name = getattr(invariant, "name", type(invariant).__name__)
                if blind.get(type_name):
                    attached_caps.setdefault(type_name, []).append(cap_name)
        for type_name, is_blind in sorted(blind.items()):
            if not is_blind:
                continue
            caps = tuple(sorted(set(attached_caps.get(type_name, ()))))
            if caps or type_name not in {t for r in doc.doc_rules.values() for t in r.types}:
                # 只列两类：已被挂（可能正在跨租户判定）／内核有但一条规则都没援引（连判据都还没有）
                gaps.tenant_blind[type_name] = caps

    # E. 内核实现了、却没有任何能力引用的类型。
    # 注意：这里**不**把"文档声称但尚未挂"算作引用——那是 B/D 两表的事。
    # 但由散文式范围（按 kind 展开）声明的类型，只要该范围内已有能力实现，
    # 就属于"已被文档指定了使用者"，单列说明，避免与"内核多写的类型"混为一谈。
    # I. 逐能力：§1 的 `risk` / `audit` 列 vs 运行时。
    #
    # 先分两类，顺序有意义：
    #   1. 文档写了内核**不存在**的取值 → 词汇分叉（根因在文档），单独报；
    #   2. 两边都在内核词汇表里、但取值不同 → 逐能力错配。
    # 混在一起报的后果是"文档写了一个内核没有的词"会被读成"某一处标错了"，
    # 而两者的修法完全不同（一个改词汇表，一个改那一行）。
    from fpa.kernel.capability import AuditPolicy, Risk  # noqa: F401  （词汇表来源）

    legal_risks = {str(item) for item in Risk}
    legal_audits = {"summary", "before_after", AUDIT_NOT_EXECUTED}

    unknown_risk: dict[str, list[str]] = {}
    unknown_audit: dict[str, list[str]] = {}
    for name, doc_value in doc.doc_risks.items():
        if doc_value and doc_value not in legal_risks:
            unknown_risk.setdefault(doc_value, []).append(name)
    for name, doc_value in doc.doc_audits.items():
        if doc_value and doc_value not in legal_audits:
            unknown_audit.setdefault(doc_value, []).append(name)
    gaps.unknown_risk_values = {k: tuple(sorted(v)) for k, v in unknown_risk.items()}
    gaps.unknown_audit_values = {k: tuple(sorted(v)) for k, v in unknown_audit.items()}

    for name in sorted(set(doc.doc_risks) & set(runtime)):
        doc_value = doc.doc_risks[name]
        if not doc_value or doc_value not in legal_risks:
            continue  # 已在 unknown_* 里报过
        live = str(runtime[name].risk)
        if doc_value != live:
            gaps.risk_mismatches[name] = (doc_value, live)

    for name in sorted(set(doc.doc_audits) & set(runtime)):
        doc_value = doc.doc_audits[name]
        if not doc_value or doc_value not in legal_audits:
            continue
        live = "before_after" if runtime[name].audit.before_after else "summary"
        if doc_value != live:
            gaps.audit_mismatches[name] = (doc_value, live)

    gaps.unreferenced_kernel_types = tuple(sorted(kernel_set - set(attached_by_type)))
    gaps.kind_declared_types = tuple(
        sorted(
            {
                type_name
                for rule in doc.doc_rules.values()
                if rule.is_kind_derived
                for type_name in rule.types
            }
        )
    )
    return gaps


def render(doc: Reconcile, runtime: dict[str, Any], kernel_names: tuple[str, ...], gaps: Gaps) -> str:
    out: list[str] = []
    add = out.append
    attached = sum(1 for cap in runtime.values() if cap.invariants)
    add("== 能力台账对账（docs/CAPABILITY_REGISTRY.md × 运行时 REGISTRY）==")
    if load_note:
        add(f"!! {load_note}")
    add(f"文档 §1 声明能力 {len(doc.doc_capabilities)} 条 / 运行时已注册 {len(runtime)} 条")
    add(f"运行时挂了不变量的能力：{attached} / {len(runtime)} 条")
    add(f"文档 §4 声明规则 {len(doc.doc_rules)} 条 / 内核实现类型 {len(kernel_names)} 种")
    add(f"文档已声明的类型：{'、'.join(sorted({t for r in doc.doc_rules.values() for t in r.types})) or '（无）'}")
    add("")

    add("[A] 文档声明了、运行时没有的能力（该能力现在恒为 404）")
    if gaps.missing_capabilities:
        by_domain: dict[str, list[str]] = {}
        for name, domain in gaps.missing_capabilities.items():
            by_domain.setdefault(domain, []).append(name)
        for domain in sorted(by_domain):
            add(f"  - {domain}（{len(by_domain[domain])} 条）：{'、'.join(sorted(by_domain[domain]))}")
    else:
        add("  无")
    add("")

    add("[A2] 文档声明了、运行时没有能力，但**有固定路由**提供（接口契约行，不是漏实现）")
    if gaps.fixed_route_capabilities:
        for name in sorted(gaps.fixed_route_capabilities):
            add(f"  - {name}：{gaps.fixed_route_capabilities[name]}（由 web/app.py 的固定路由提供）")
    else:
        add("  无")
    if gaps.route_map_note:
        add(f"  {gaps.route_map_note}")
    add("")

    add("[1-R] 运行时已注册、文档 §1 未列（**[A] 的对称面**；评审结论：补文档，不删代码）")
    if gaps.undocumented_capabilities:
        by_domain: dict[str, list[str]] = {}
        for name, (domain, route, kind) in gaps.undocumented_capabilities.items():
            by_domain.setdefault(domain, []).append(f"{name}（{kind} {route}）")
        for domain in sorted(by_domain):
            add(f"  - {domain}：{'；'.join(sorted(by_domain[domain]))}")
    else:
        add("  无")
    add("")

    add("  · 逐域条数对账（文档 vs 运行时；**不等不判缺陷**，五域施工中属正常进度）：")
    if gaps.domain_count_drift:
        for domain in sorted(gaps.domain_count_drift):
            documented, registered = gaps.domain_count_drift[domain]
            hint = "文档多（待实现）" if documented > registered else "**运行时多（疑似漏登记！）**"
            add(f"      {domain}: 文档 {documented} / 运行时 {registered} —— {hint}")
        add("      注：`运行时多` 那一类是 `pond.archive` 式漂移的指纹——补文档，不删代码。")
    else:
        add("      各域条数一致")
    add("")

    add("[B] 文档声称有强制能力、但运行时没有任何能力挂它（规则退化成建议）")
    if gaps.undefended_rules:
        for number in sorted(gaps.undefended_rules):
            rule = gaps.undefended_rules[number]
            add(f"  - #{number} {rule.title}")
            add(f"      声明类型：{'、'.join(rule.types) or '（未解析到类型名）'}")
            add(f"      文档声称的强制能力：{'、'.join(rule.capabilities)}")
    else:
        add("  无")
    add("")

    add("[B-声明] 文档**显式声明**了替代强制方式的规则（列出来供复核；不算缺陷）")
    if gaps.declared_alternatives:
        for number in sorted(gaps.declared_alternatives):
            rule, named = gaps.declared_alternatives[number]
            add(f"  - #{number} {rule.title}")
            add(f"      声明类型：{'、'.join(rule.types)}（运行时**没有**能力挂它 —— 这是声明出来的，不是漏挂）")
            add(f"      指名的强制点所在能力：{'、'.join(named)}")
            add("      判据：规则行里逐字写了「替代强制」且指名了已注册能力；"
                "该分支**必须打印**，不得静默豁免。")
    else:
        add("  无")
    add("")

    add("[B2] 散文式范围已按 `kind` 列机械展开——**已实现**却没挂该类不变量（真缺口）")
    if gaps.kind_gaps:
        for name in sorted(gaps.kind_gaps):
            number, types = gaps.kind_gaps[name]
            add(f"  - {name}（kind={doc.doc_kinds.get(name, '?')}）应按 §4 #{number} 挂 {'、'.join(types)}")
    else:
        add("  无")
    kind_covered = [n for n, r in doc.doc_rules.items() if r.is_kind_derived]
    for number in sorted(kind_covered):
        rule = doc.doc_rules[number]
        todo = gaps.kind_unimplemented.get(number, ())
        add(
            f"  · #{number} 展开范围 kind={list(rule.derived_kinds)}："
            f"已实现 {len([n for n, k in doc.doc_kinds.items() if k in rule.derived_kinds and n in runtime])} 条，"
            f"尚未实现 {len(todo)} 条（**进度，不是缺陷**）"
        )
    add("")

    add("[B3] 既无能力清单、也无法按 kind 展开——只能人工核对")
    if gaps.manual_audit:
        for number in sorted(gaps.manual_audit):
            rule = gaps.manual_audit[number]
            add(f"  - #{number} {rule.title}：声明类型 {'、'.join(rule.types) or '（未解析到）'}")
            add(f"      文档原文范围：{rule.scope_text or '（空）'}")
    else:
        add("  无")
    add("")

    add("[C] 挂了不变量、但类型在文档里没有出处（来源不可追溯 / 类型名写错 / 内核未实现）")
    if gaps.unattributed_types:
        for name in sorted(gaps.unattributed_types):
            add(f"  - {name}：{'、'.join(sorted(set(gaps.unattributed_types[name])))}")
    else:
        add("  无")
    add("")

    add("[D] 逐能力：文档 §4 声称的类型未被运行时覆盖（挂错/漏挂）")
    if gaps.mismatches:
        for name in sorted(gaps.mismatches):
            doc_types, live_types = gaps.mismatches[name]
            add(f"  - {name}")
            add(f"      文档声称：{'、'.join(doc_types) or '（无）'}")
            add(f"      运行实际：{'、'.join(live_types) or '（未挂任何不变量）'}")
    else:
        add("  无")
    add("")

    add("[D-补] 文档声称要挂、但该能力尚未实现（因此无法核对；进度问题，见 [A]）")
    if gaps.mismatches_unimplemented:
        for name in sorted(gaps.mismatches_unimplemented):
            add(f"  - {name}：文档声称 {'、'.join(gaps.mismatches_unimplemented[name])}")
    else:
        add("  无")
    add("")

    add("[F] row_actions × REGISTRY：资源声明了动作、运行时没有对应能力（前端会渲染出点不动的按钮）")
    if gaps.row_action_gaps:
        for resource in sorted(gaps.row_action_gaps):
            missing, matched = gaps.row_action_gaps[resource]
            add(f"  - {resource}：声明了 {list(missing)}，没有能力的末端 token 与之匹配"
                f"（能匹配上的：{list(matched) or '无'}）")
    else:
        add("  无")
    if gaps.row_action_declared:
        add("  · 已在资源声明 `row_action_notes` 里写明理由的动作（不报警，但列出来供复核）：")
        for resource in sorted(gaps.row_action_declared):
            for item in gaps.row_action_declared[resource]:
                add(f"      {resource}.{item}")
    add("")

    add("[I] 逐能力：§1 的 `risk` / `audit` 列 vs 运行时")
    if gaps.unknown_risk_values or gaps.unknown_audit_values:
        for value, names in sorted(gaps.unknown_risk_values.items()):
            add(f"  - `risk` 文档写了内核不存在的取值 `{value}`（{len(names)} 条）：{'、'.join(names[:6])}")
        for value, names in sorted(gaps.unknown_audit_values.items()):
            add(f"  - `audit` 文档写了内核不存在的取值 `{value}`（{len(names)} 条）：{'、'.join(names[:6])}")
        add("      → 词汇分叉：先决定是补内核档位还是改文档，**不要**逐条改能力行")
    if gaps.risk_mismatches:
        add(f"  - `risk` 不符 {len(gaps.risk_mismatches)} 条（文档 / 运行时）：")
        for name in sorted(gaps.risk_mismatches):
            doc_value, live = gaps.risk_mismatches[name]
            add(f"      {name}: {doc_value} / {live}")
    if gaps.audit_mismatches:
        add(f"  - `audit` 不符 {len(gaps.audit_mismatches)} 条（文档 / 运行时）：")
        for name in sorted(gaps.audit_mismatches):
            doc_value, live = gaps.audit_mismatches[name]
            add(f"      {name}: {doc_value} / {live}")
    if not (
        gaps.risk_mismatches
        or gaps.audit_mismatches
        or gaps.unknown_risk_values
        or gaps.unknown_audit_values
    ):
        add("  无")
    add("")

    add("[G] 租户维度：谁真的按租户键判定、谁只是「看着在管」（只看**会执行的代码**）")
    add(f"  [G1] 真按租户键比较/查询（{len(gaps.tenant_declared)}）：{'、'.join(gaps.tenant_declared) or '无'}")
    add(f"  [G2] 委托给 Scope ⇒ **继承 Scope 的盲区**（{len(gaps.tenant_delegated)}）：{'、'.join(gaps.tenant_delegated) or '无'}")
    add("       （`Scope` 只比 farm_id/area_id/pond_id/created_by；G2 这些类型做了行级判定，但那一维里没有租户）")
    add("")
    add("[G3] 租户盲区：类型实现里既无租户键、也不做任何行级判定（**优先级清单**，不是缺陷判决）")
    if gaps.tenant_blind:
        for type_name in sorted(gaps.tenant_blind):
            caps = gaps.tenant_blind[type_name]
            where = "、".join(caps) if caps else "（当前没有任何能力挂它）"
            add(f"  - {type_name}：{where}")
        add("  · 背景：SCOPE_COLUMN 只覆盖 farm/area/pond/personal，**不含 organization_id** ⇒")
        add("    DataScope 行使的是「区域」不是「租户」；上表这些规则一旦查了带 organization_id 的表，")
        add("    就必须先回答「谓词里有没有租户键」。实证：tools/repro_period_open_tenant.py"
            "（PeriodOpen 曾两向可复现；t2 修好后该脚本转为回归守卫）。")
    else:
        add("  无")
    add("")

    add("[H] 「LIMIT 1 且无 ORDER BY」的候选位置（**候选，不是缺陷判决**：唯一键等值查询是安全的）")
    if gaps.unordered_limit_one:
        for path, line_no, snippet in gaps.unordered_limit_one:
            add(f"  - {path}:{line_no}  {snippet}")
        add("      判据：`LIMIT 1` 命中的行由存储顺序决定 ⇒ 结果集可能多行时结论就不确定。")
        add("      已实证：`PeriodOpen`（内核 invariants.py，**t2 已补 `ORDER BY`**，本行已不再列出）。")
        # ★ 当前迭代逐条复核：本表剩余 5 处**都不是缺陷**，别再让人按行号去"修"：
        #   * kernel/invariants.py 三处 = 唯一键等值查询；
        #   * access/admin.py 那处 = 主键 IN + 唯一 `code` 过滤；
        #   * cost/entries.py 那处 = **重复归集的存在性检查**（只判"有没有"，
        #     取哪一行不影响结论，`id` 仅用于报错诊断）。
        # 旧文本"`_require_open_period`（服务层仍无 ORDER BY）"是过时描述：
        # 该函数已委托给 `kernel/invariants.py::lock_period_for_date`（带 ORDER BY）。
        add("      复核结论：上列均为**等值 / 存在性**查询，取哪一行不影响结论。")
    else:
        add("  无")
    stats = gaps.limit_scan_stats
    if stats:
        add(
            f"  · 扫描规模：{stats.get('files', 0)} 个文件；含 LIMIT 的行 {stats.get('limit_any', 0)}"
            f"（其中 LIMIT 1 共 {stats.get('limit_one', 0)} 行、分页 `LIMIT %s OFFSET %s` {stats.get('paged', 0)} 行）"
        )
        add("    **这一行是为了让「无」可被检验**：结果为 0 时，先看扫描规模是不是也为 0。")
        add("    实例（purchase-dev）：采购域「0 处可疑」的真因是**它根本不用 LIMIT 1**，只用分页——")
        add("    『0 处』不等于『干净』，必须能自证『扫到了东西』。")
    add("")

    add("[E] 内核实现了、但当前没有任何能力引用的类型")
    add(f"  {'、'.join(gaps.unreferenced_kernel_types) or '无'}")
    add(
        "  · 其中已被文档指定使用者（由散文式范围按 kind 展开）、只是能力还没实现的："
        f"{'、'.join(gaps.kind_declared_types) or '无'}"
    )
    add("  · 说明：五域开工期间「无人引用」是正常进度；只有出现「文档声称有强制能力、却没人挂」（B 表）才是缺陷")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="能力台账对账（文档 × 运行时注册表）")
    parser.add_argument("--check", action="store_true", help="有缺口即退出 1（开发期自检入口）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--doc", default=str(REGISTRY_DOC), help="权威清单路径")
    args = parser.parse_args(argv)

    kernel_names = kernel_type_names()
    doc = parse_doc(Path(args.doc), kernel_names)
    runtime, _ = parse_runtime()
    # 用**本次运行**拿到的状态，而不是模块级变量：测试会注入 `load_note` 模拟降级，
    # 但 `parse_runtime()` 会把它重置，于是那条测试永远看不到自己注入的状态
    # （实测：注入后 main 返回 1 而不是 3）。判据必须来自本次落地的数据。
    partial_load = bool(load_note)
    gaps = compare(doc, runtime, kernel_names)

    if args.json:
        payload = {
            "load_note": load_note,
            "doc_capabilities": len(doc.doc_capabilities),
            "runtime_capabilities": len(runtime),
            "doc_rules": len(doc.doc_rules),
            "kernel_types": list(kernel_names),
            "missing_capabilities": gaps.missing_capabilities,
            "undocumented_capabilities": {
                name: {"domain": domain, "route": route, "kind": kind}
                for name, (domain, route, kind) in gaps.undocumented_capabilities.items()
            },
            "domain_count_drift": {
                domain: {"documented": documented, "registered": registered}
                for domain, (documented, registered) in gaps.domain_count_drift.items()
            },
            "fixed_route_capabilities": gaps.fixed_route_capabilities,
            "route_map_note": gaps.route_map_note,
            "undefended_rules": {
                str(number): {
                    "title": rule.title,
                    "types": list(rule.types),
                    "capabilities": list(rule.capabilities),
                }
                for number, rule in gaps.undefended_rules.items()
            },
            "manual_audit_rules": {
                str(number): {"title": rule.title, "types": list(rule.types)}
                for number, rule in gaps.manual_audit.items()
            },
            # [I] `risk` / `audit` 逐能力错配。必须出现在 `--json` 里，
            # 否则独立复核者（复核人）读不到这项——而"读不到"会被读成"全绿"。
            "risk_mismatches": {
                name: {"doc": doc_value, "runtime": live}
                for name, (doc_value, live) in gaps.risk_mismatches.items()
            },
            "audit_mismatches": {
                name: {"doc": doc_value, "runtime": live}
                for name, (doc_value, live) in gaps.audit_mismatches.items()
            },
            "unknown_risk_values": {
                value: list(names) for value, names in gaps.unknown_risk_values.items()
            },
            "unknown_audit_values": {
                value: list(names) for value, names in gaps.unknown_audit_values.items()
            },
            "row_action_declared": {
                resource: list(items) for resource, items in gaps.row_action_declared.items()
            },
            "row_action_gaps": {
                resource: {
                    "missing_actions": list(missing),
                    "matched_actions": list(matched),
                    "required_tokens": [ROW_ACTION_TOKENS[a] for a in missing],
                }
                for resource, (missing, matched) in gaps.row_action_gaps.items()
            },
            "mismatches_unimplemented": {
                name: list(types) for name, types in gaps.mismatches_unimplemented.items()
            },
            "kind_derived_gaps": {
                name: {"rule": number, "types": list(types)}
                for name, (number, types) in gaps.kind_gaps.items()
            },
            "kind_derived_unimplemented": {
                str(number): list(names) for number, names in gaps.kind_unimplemented.items()
            },
            "tenant_declared": list(gaps.tenant_declared),
            "tenant_delegated": list(gaps.tenant_delegated),
            "limit_scan_stats": gaps.limit_scan_stats,
            "unordered_limit_one": [
                {"file": path, "line": line_no, "sql": snippet}
                for path, line_no, snippet in gaps.unordered_limit_one
            ],
            "tenant_blind": {
                name: list(caps) for name, caps in gaps.tenant_blind.items()
            },
            "unattributed_types": gaps.unattributed_types,
            "mismatches": {
                name: {"doc": list(doc_types), "runtime": list(live_types)}
                for name, (doc_types, live_types) in gaps.mismatches.items()
            },
            "unreferenced_kernel_types": list(gaps.unreferenced_kernel_types),
            "has_gaps": gaps.any,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(doc, runtime, kernel_names, gaps))

    if not args.check:
        return 0
    # 降级必须留红（purchase-dev 的建议：降级可见 = 有一条红，而不是一条警告）。
    # 部分装载意味着这张表只覆盖了装载成功的域；把 exit 0 交给调用方，
    # 就会把「我只查了一半」读成「全绿」。
    if partial_load:
        return 3
    return 1 if gaps.any else 0


if __name__ == "__main__":
    sys.exit(main())
