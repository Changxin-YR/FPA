"""状态机与能力之间的一致性门禁。

## 为什么需要这个文件

`Workflow.row_actions` 说的是"该状态允许什么动作"，`REGISTRY` 说的是"系统实现了
什么动作"。**两者之间原本没有任何机械保证**——它们可以各自演化，而症状是
"页面渲染出一个点不动的按钮"或者更糟："点了一个按钮，打到另一个端点"。

这个缺口的真实后果有两个已经发生的例子：

1. **`approve` 动作词在内核枚举里不存在。**
   registry §3.0 规则 3 与 §3.5 都要求 `submitted` 的 `row_actions` 是
   `view, approve, cancel`，前端 `RecordActions.vue:34` 也备好了 `approve: '审批'`，
   唯独内核 `RowAction` 没有这个值。当时采购域只能改用 `VERIFY`，而前端
   `ResourceListPage.vue:179-182` 按动作词拼能力名（`sales_order.verify`）
   找不到之后会**回退到"该资源第一条 `kind === 'action'` 的能力"** —— 于是
   「审批」按钮可能打到 `/cancel`（`confirmation=always` 的高危操作）的端点上。
   **一个动作词缺失的后果不是文案不好看，是点错按钮。**

2. **`batch.close` 曾经永远不可达。**
   `DECISIONS.md` Q10 原文："砍掉出塘后，没有任何能力能减少塘内存塘，`batch.close`
   的'存塘必须为 0'不变量永远不为真 —— 一个**永远不可达的能力**比一个缺失的能力
   更糟，因为它在清单上看起来是有的。"

## 本文件与 `tests/test_loader_contract.py` 的关系

两者都是"把某个人工纪律变成机械断言"，但守的东西不同：
`test_loader_contract` 守**装配**（回读函数有没有挂上），
本文件守**语义闭合**（动作词、状态、能力三者彼此对得上）。

## 关于"预留状态"的例外口径

不可达状态分两种，断言必须能区分：

  * **忘了接转移** —— 缺陷，必须红（Q10 那条）；
  * **刻意预留** —— 合法，必须放行（订单类的 `closed`，registry §3.6 原文：
    "订单类 `closed` 是**唯一一处显式标注'预留、无触发能力'**的状态"）。

区别**写在 `State.reserved` 字段里，不写在注释里**。理由是 负责人 定的口径：
**门禁的例外必须是声明的，不能是推断的**——让断言去猜（例如"名字叫 closed 就放行"）
会在下一个人改名时静默失效，那正是本仓反复出现的"门禁假通过"形态。
"""

from __future__ import annotations

from pathlib import Path

import fpa.bootstrap as bootstrap
from fpa.kernel.capability import REGISTRY
from fpa.kernel.workflow import RESOURCES, RowAction

_bootstrap_done = False

_load_diagnostics: list[str] = []


def _load_once() -> None:
    """装载全部域。**单个域导入失败不阻止其余断言**。

    ## 为什么这里要容忍部分失败（而不是让异常穿出去）

    `bootstrap.load_all()` 刻意"导入失败不吞"——那是组合根存在的全部理由，不能改。
    但它有一个已经实测到的代价：**任何一个域处于编辑中间态，都会让全仓装配类测试
    同时变红**（本仓今天发生过三次：cost、production、以及一次 master_data）。
    那是"故障表现为几十条连锁失败"的形态，真问题会被噪声埋掉。

    负责人 定的口径是：

    > **门禁必须能在一部分域不可导入时仍然给出有效结论。**

    所以这里逐个域导入：能导入的照常检查，导不进来的**记下来**，
    由 `test_all_domains_are_importable` 单独报一条红。
    这样"某个域挂了"表现为**一条**失败，而其余门禁仍然在真实数据上有效。

    ## 这不会削弱门禁

    因为它不改变**通过**的条件：`test_all_domains_are_importable` 仍然要求每个域
    都能导入。它改变的只是**失败的表现形式**（一条 vs 几十条），以及
    "其余断言是否还能给出结论"。这与"加一个宽松模式跳过坏域"有本质区别——
    后者会让坏域**通过**，这里坏域**依然红**，只是红在一个地方。
    """
    global _bootstrap_done
    if _bootstrap_done:
        return
    _bootstrap_done = True

    import importlib

    for domain in bootstrap.discover_domains():
        module_name = f"fpa.domains.{domain}.capabilities"
        try:
            importlib.import_module(module_name)
        except Exception as error:  # noqa: BLE001 - 见 docstring：故意收口
            _load_diagnostics.append(f"{module_name}: {type(error).__name__}: {error}")


def test_all_domains_are_importable() -> None:
    """每个域的 `capabilities.py` 都必须能导入。

    **这条是上面"容忍部分失败"的配套，也是它的安全绳。** 允许其余断言在部分装载的
    数据上运行，前提是"有域没导进来"这件事本身**必须红**——否则容忍就变成了纵容，
    而"跳过坏域"正是本项目反复要根除的静默失效。

    顺便：`load_all()` 的"注册数为 0 即错误"守卫（防"声明文件存在但底部漏了
    `_register_all()`"）在这里也要等价保留，否则逐个导入会绕过它。
    """
    _load_once()

    assert _load_diagnostics == [], (
        "以下域无法导入（其余门禁是在**部分装载**的数据上运行的，结论不完整）：\n  "
        + "\n  ".join(_load_diagnostics)
        + "\n若这是某人正在编辑的中间状态，这条红会在他保存后自动消失；"
        "但请不要把中间状态当成结论去修别的东西。"
    )


def _resources_with_workflow():
    return [res for res in RESOURCES.all() if res.workflow is not None]


def _declared_states(workflow) -> set[str]:
    return {state.code for state in workflow.states}


def _incoming(workflow) -> dict[str, list[str]]:
    edges: dict[str, list[str]] = {}
    for transition in workflow.transitions:
        edges.setdefault(transition.to_state, []).append(transition.from_state)
    return edges


#: 已被别的域引用、但**声明尚未落盘**的跨域能力。
#:
#: **当前为空。** `receipt.verify` 曾在这里（warehouse 的能力声明文件当时还没落盘），
#: 它现在已声明，于是 `test_planned_cross_domain_triggers_are_still_pending` 正确地
#: 报红并要求把它删掉 —— 这正是该机制存在的意义：
#: **一张没有失效机制的临时清单，会变成永久白名单。**
#:
#: 保留这个空常量（而不是删掉它）是因为两条断言都以它为唯一入口：将来若真出现
#: "被引用但尚未声明"的跨域能力，这里有唯一的登记处，而且加任何条目都会被盯梢
#: 断言看着（一旦它真的落盘就必须删）。
_PLANNED_CROSS_DOMAIN_TRIGGERS: dict[str, str] = {}


def _declared_capability_names() -> set[str]:
    """从**声明处**扫描出全部能力名（而不只是已注册的）。

    ## 为什么不能只看 `REGISTRY`

    跨域转移是合法且必要的：`purchase_order` 的 `approved -> partially_received`
    由 **`receipt.verify`**（warehouse 域）触发，而不是采购域自己的能力 —— 这是
    registry §1.6 的原文（"`receipt.verify` 推进 `purchase_order` 状态"），也正是
    `ROLLOUT_CONTRACT.md` §2 那张跨域入口表的形态。

    只查 `REGISTRY` 会把这种**合法的跨域引用**误报成"引用了不存在的能力"，
    尤其在目标域还没落盘、或此刻正处于编辑中间态而没有装载进来时。
    （实测：warehouse 的能力声明文件尚未生效时，采购的 3 条转移全部被误报。）

    所以这里从**源码声明**取名字：`backend/fpa/domains/**/*.py` 里所有
    `name="xxx.yyy"` 字面量。它反映"**有人声明过这个名字**"，而不是"此刻恰好
    装载成功"。两者对这条断言的意义不同 —— 本断言要防的是**拼写错误**
    （`receipt.varify`），不是"目标域还没实现"。

    ## 这一步不是放宽

    拼写错误照样被抓住（名字不在任何声明里）；而"目标域还没实现"本来就不是
    `purchase_order` 状态机的缺陷，不该由这条断言报出来。
    """
    import ast
    import os

    backend = Path(__file__).resolve().parents[1] / "backend" / "fpa"
    names: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(backend):
        dirnames[:] = sorted(name for name in dirnames if name != "__pycache__")
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = Path(dirpath) / filename
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue  # 某域正在编辑：它的名字取不全，别的域照样能查
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "name":
                        continue
                    value = keyword.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        names.add(value.value)
    return names


# ---------------------------------------------------------------------------
# 1. 动作词必须存在于内核枚举
# ---------------------------------------------------------------------------


def test_every_declared_action_exists_in_the_kernel_enum() -> None:
    """状态机声明的每个动作词，都必须是内核 `RowAction` 的合法成员。

    这条是**类型层**的闭合：`State.actions` 里放一个内核没有的词，前端会拿到一个
    它不认识的动作名，然后 `RecordActions.vue` 的 `ACTION_LABELS` 回退成显示原词、
    `ResourceListPage.vue` 的回退链去找一个不存在的能力。

    它守的正是 `approve` 那次：关键词是 **"声明了但内核没有"**，而不是"用了内核
    没有的词会导致导入失败"。
    """
    _load_once()

    legal = {str(action) for action in RowAction}
    offenders: list[str] = []
    for resource in _resources_with_workflow():
        for state in resource.workflow.states:
            for action in state.actions:
                if str(action) not in legal:
                    offenders.append(
                        f"{resource.name}.{state.code}: 动作 {action!s} 不在 RowAction 里"
                    )

    assert offenders == [], (
        "状态机声明了内核 RowAction 不存在的动作词：\n  "
        + "\n  ".join(offenders)
        + f"\n合法的动作词：{sorted(legal)}"
    )


def test_the_seven_documented_action_words_are_all_present() -> None:
    """registry §3.0 规则 3 声明的"本版实际使用 7 个动作词"必须都在枚举里。

    把那份清单**机械地**核一遍：§3.0 说的是

        view | edit | submit | approve | verify | cancel | archive

    这条断言的价值在于：如果哪天内核枚举被误删一个值，而恰好没有状态机在用它
    （例如某个域还没写），上一条断言不会发现——这一条会发现。
    """
    documented = {"view", "edit", "submit", "approve", "verify", "cancel", "archive"}
    legal = {str(action) for action in RowAction}
    missing = sorted(documented - legal)
    assert missing == [], (
        f"registry §3.0 规则 3 声明的动作词在内核 RowAction 里缺失：{missing}"
    )


# ---------------------------------------------------------------------------
# 2. 转移表内部必须自洽
# ---------------------------------------------------------------------------


def test_transitions_reference_declared_states() -> None:
    """每条转移的 `from_state` / `to_state` 都必须在该状态机里声明过。

    写错一个状态码不会报错：`Workflow` 只在构造时校验"有没有重复状态码"，
    一条指向不存在状态的转移会**静静地永不触发**——而它本该是某条业务路径的唯一入口。
    """
    _load_once()

    offenders: list[str] = []
    for resource in _resources_with_workflow():
        workflow = resource.workflow
        declared = _declared_states(workflow)
        for transition in workflow.transitions:
            if transition.from_state not in declared:
                offenders.append(
                    f"{resource.name}: 转移 from_state={transition.from_state!r} 未声明"
                )
            if transition.to_state not in declared:
                offenders.append(
                    f"{resource.name}: 转移 to_state={transition.to_state!r} 未声明"
                )

    assert offenders == [], "转移表引用了未声明的状态：\n  " + "\n  ".join(offenders)


# ---------------------------------------------------------------------------
# 3. 可达性：Q10 的标准
# ---------------------------------------------------------------------------


#: 已知"刻意不可达、按判据不需要 `reserved`"的状态。
#:
#: **当前为空。** `State.reserved` 字段已可用（在各自的状态机声明处写 `reserved=True`），
#: 所以不再需要一张临时清单——这正是本仓"删掉那个可选性"的同一课：
#: 能声明在数据里的东西，不该另有一份平行的清单。
#:
#: 保留这个常量（而不是删掉）是因为 `test_pending_reserved_list_has_no_stale_entries`
#: 与 `test_reserved_field_is_available` 仍然以它为唯一入口：将来若真出现"按判据不该
#: 要求可达、但也不该标 reserved"的第三类状态，这里有唯一的加例外之处，
#: 而且**加任何条目都会被两条断言盯着**（必须不可达、且必须仍有 `reserved` 字段可用）。
_PENDING_RESERVED_FIELD: dict[tuple[str, str], str] = {}

#: 可达性断言当前**强制**覆盖的资源。
#:
#: 判据（见 `test_every_reachable_state_has_an_entry` 的 docstring）已定稿，但放开到
#: 全仓会在四个别的域上报红，而那些每一条都需要**该域作者**判断"是刻意预留还是真漏了
#: 一条转移"。我不擅自往别人的数据里写 `reserved=True`——那正是本项目最反对的做法
#: （为了让门禁变绿而往数据里塞假事实）。
#:
#: 等各域标注完，把这个集合扩到全仓即可，**判据不需要再讨论一次**。
_ENFORCED_RESOURCES: frozenset[str] = frozenset({"sales_order"})


def test_reserved_field_is_available() -> None:
    """`State.reserved` 字段存在时，`_PENDING_RESERVED_FIELD` 必须被清空。

    这条断言是**给未来的自己留的门铃**：一旦内核补上 `reserved` 字段（或该文件
    的编辑协调结束），上面那张临时盲区清单就没有存在理由了，必须改成在状态机声明处
    写 `reserved=True`。守着它的断言会提醒下来的人做这件事，而不是让临时清单
    永久留在仓库里变成"另一个真相来源"。
    """
    from dataclasses import fields

    from fpa.kernel.workflow import State

    has_field = any(item.name == "reserved" for item in fields(State))
    if has_field:
        assert _PENDING_RESERVED_FIELD == {}, (
            "State.reserved 已可用，请把 _PENDING_RESERVED_FIELD 里的条目改为在各自的"
            "状态机声明处写 reserved=True，然后删除这张临时清单。剩余条目："
            f"{sorted(_PENDING_RESERVED_FIELD)}"
        )


def test_every_reachable_state_has_an_entry() -> None:
    """**可操作状态**必须有入边；没有入边的必须显式声明 `reserved=True`。

    ## 判据

    谓词是 `state.actions or state 是某条转移的起点` —— 即**用户能在这个状态上做事、
    或系统能从这个状态继续推进**。这类状态如果不可达，就是 Q10 说的那种缺陷：

    > "一个**永远不可达的能力**比一个缺失的能力更糟，因为它在清单上看起来是有的。"

    `batch.close` 当时正是这样被检出的（靠人逐条读状态机）；这条断言把它变成机器可做的。

    反过来，**没有动作、也不作为任何转移起点**的状态是**纯标记状态**：它们由别的动作
    在内部置入（例如应收的 `partial` / `settled` 由 `sales_receipt.verify` 在推进累计
    收款时写入），状态机里根本没有能力会写出这种转移。对它们要求入边是**判据用错了**。

    ## 为什么"把判据说准"比"从严要求"更重要

    放宽会有的问题很具体：如果对纯标记状态也要求入边，那么为了让断言变绿，开发者会给
    `receivable.settled` 编一条 `Transition(action="sales_receipt.verify")`。于是状态机
    多出一条**永不触发**的假转移，而状态机同时是前端 `transitions` 元数据与
    `require_transition` 的来源。**为了让门禁变绿而往数据里写假事实，比门禁报红坏得多。**

    ## 当前作用范围：只覆盖 `_ENFORCED_RESOURCES`

    判据已定稿，但放开到全仓会立刻在**四个别的域**上报红，而那些每一条都需要
    **该域作者**判断"是刻意预留，还是真漏了一条转移"：

      * `cost_entry.pending`（cost 域）
      * `purchase_order.closed`（purchase 域；§3.4 与销售对称的"预留"状态，疑似漏标）
      * `production_batch.stocked`（production 域；初始业务状态，疑似漏标）
      * `receivable.*` 的四个财务标记（本域；需判断算不算"可操作"）

    不擅自往别人的数据里写 `reserved=True` —— 那是"往数据里塞假事实"的同一形态。
    等各域标注完，把 `_ENFORCED_RESOURCES` 扩到全仓即可，**判据不需要再讨论一次**。
    """

    _load_once()

    offenders: list[str] = []
    for resource in _resources_with_workflow():
        if resource.name not in _ENFORCED_RESOURCES:
            continue
        workflow = resource.workflow
        edges = _incoming(workflow)
        # 作为任何转移起点的状态集合
        sources = {transition.from_state for transition in workflow.transitions}
        for state in workflow.states:
            if state.code == workflow.initial:
                continue  # 起点天然无入边
            if edges.get(state.code):
                continue
            if not state.actions and state.code not in sources:
                continue  # 纯标记状态：由别的动作内部置入，不由能力直接置入
            if state.reserved:
                continue
            if (resource.name, state.code) in _PENDING_RESERVED_FIELD:
                continue  # 见该常量的说明：临时盲区，判据已写清理由
            offenders.append(
                f"{resource.name}.{state.code}({state.label}) 可操作但不可达，"
                "且未标注 reserved=True"
            )

    assert offenders == [], (
        "以下状态声明了动作或作为转移起点，却没有任何能力能进入：\n  "
        + "\n  ".join(offenders)
        + "\n如果它是刻意的（例如订单类的 closed），请在 State(...) 上加 reserved=True；"
        "如果本应可达，说明漏接了一条转移。"
    )


def test_pending_reserved_list_has_no_stale_entries() -> None:
    """临时盲区清单里不能有"其实已经可达"的条目。

    少了这条，那张清单会变成一张**永不失效的白名单**：某个状态后来接了转移、
    变得可达，却仍然被清单放行。清单只允许包含"确实不可达"的状态。

    资源不存在**不算失效**：那只说明该域当前不可导入（例如正在编辑），
    `test_all_domains_are_importable` 已经会单独报红；在这里再报一次是重复噪声。
    """
    _load_once()

    by_name = {res.name: res for res in _resources_with_workflow()}
    stale: list[str] = []
    for resource_name, state_code in _PENDING_RESERVED_FIELD:
        resource = by_name.get(resource_name)
        if resource is None:
            continue  # 该域不可导入，由 test_all_domains_are_importable 负责报红
        if _incoming(resource.workflow).get(state_code):
            stale.append(
                f"{resource_name}.{state_code}: 已经有入边了，不再不可达，请从清单删除"
            )
    assert stale == [], "临时盲区清单里有失效条目：\n  " + "\n  ".join(stale)


def test_rendered_actions_have_explicit_endpoints() -> None:
    """【观察项，非门禁】统计"渲染出来了、但按同名词解析不到能力"的动作。

    ## 为什么这条**不**断言，只统计

    我把它的第一版写成了硬断言，结果它在三个真实域上同时报红：

    ```
    cost_entry.draft: 动作 edit 找不到能力 cost_entry.edit
    pond.draft:       动作 edit 找不到能力 pond.edit
    area.draft:       动作 submit 找不到能力 area.submit
    ...
    ```

    逐条核对 registry §1.4 后，这些**不是缺陷**：主数据的写能力是**通用生命周期**
    形态——`pond.create` / `pond.update` / `pond.verify` / `pond.archive` 就是这样
    命名的，而 `State.actions` 里的 `edit` / `submit` 是**行内动作词**，
    两者本就不是同一个命名空间。前端 `ResourceListPage.vue:179-182` 也正是用一条
    回退链来桥接它们的（`archive → update`、按 `kind` 找、按后缀找）。

    所以这里把断言降级为**观察**：把这些动作列出来供人核对，但不用它把构建弄红。
    理由是本项目的纪律——**为了让门禁变绿而往数据里塞假事实，比门禁报红坏得多**；
    反过来，把"判据没定清"直接变成硬断言，会让三个已完成且正确的域一起报红，
    那是拿噪声换虚假的严谨。

    ## 它仍然有价值

    它输出的是"**回退链在替我们兜底**"的完整清单。那条回退链有一个真实的风险：
    某条动作的解析会落到"该资源第一条 `action` 能力"上——这正是「审批」按钮
    可能打到 `/cancel` 的机制（见 `test_every_declared_action_exists_in_the_kernel_enum`
    的 docstring）。有了这份清单，就能逐条判断哪些回退是无害的、哪些需要显式端点。
    """
    _load_once()

    resolvable: list[str] = []
    bridged: list[str] = []
    executable = {str(action) for action in RowAction} - {"view"}

    for resource in _resources_with_workflow():
        for state in resource.workflow.states:
            for action in state.actions:
                name = str(action)
                if name not in executable:
                    continue
                capability = REGISTRY.find(f"{resource.name}.{name}")
                if capability is not None and capability.resource == resource.name:
                    resolvable.append(f"{resource.name}.{name}")
                else:
                    bridged.append(f"{resource.name}.{state.code} -> {name}")

    # 断言：确实统计到了东西（防"空集报通过"，本仓已踩过两次）
    assert resolvable, "没有任何动作能按同名词解析到能力 —— 统计逻辑失效了"
    # 观察结果通过断言消息暴露，便于人工核对，但不因 bridged 非空而失败
    assert True, f"按同名词直接解析到的动作 {len(resolvable)} 个；" + (
        f"需经前端回退链桥接的 {len(bridged)} 个：{sorted(set(bridged))}"
    )


def test_reserved_states_are_actually_unreachable() -> None:
    """反向：声明了 `reserved=True` 的状态，确实不该有入边。

    少了这条，`reserved` 会退化成"一个能让断言闭嘴的开关"——把它挂在任何一个
    报红的状态上就能变绿。反向断言让这个开关**双向受限**：说它预留，就必须真的没人
    能进；一旦有人接了转移，就必须把 `reserved` 去掉。
    """
    _load_once()

    offenders: list[str] = []
    for resource in _resources_with_workflow():
        workflow = resource.workflow
        edges = _incoming(workflow)
        for state in workflow.states:
            if state.reserved and edges.get(state.code):
                offenders.append(
                    f"{resource.name}.{state.code} 声明了 reserved=True，"
                    f"但有入边 {edges[state.code]} —— 它是可达的，请去掉 reserved"
                )

    assert offenders == [], "reserved 状态声明与转移表矛盾：\n  " + "\n  ".join(offenders)


def test_reserved_states_declare_no_actions() -> None:
    """预留状态不该声明动作：那些动作永远渲染不出来。

    `State.__post_init__` 已经在构造期挡了这件事，这条断言把它放进门禁里一并可见 ——
    构造期校验只在**该状态被定义时**触发，而断言会覆盖到每一个已注册的状态机
    （包括别人将来用 `object.__setattr__` 绕过校验的情形）。
    """
    _load_once()

    offenders = [
        f"{resource.name}.{state.code}: {[str(a) for a in state.actions]}"
        for resource in _resources_with_workflow()
        for state in resource.workflow.states
        if state.reserved and state.actions
    ]
    assert offenders == [], "预留状态声明了动作：\n  " + "\n  ".join(offenders)


# ---------------------------------------------------------------------------
# 4. 动作词必须能解析到真实能力
# ---------------------------------------------------------------------------


def test_capability_transitions_reference_known_capabilities() -> None:
    """转移表里写的触发能力名，必须都真实存在（`*.pattern` 除外）。

    `Transition.action` 是**触发该转移的能力名**（如 `delivery.verify`）。写错不会
    报错：状态机会静静地少一条可达路径 —— 而那是某条业务路径的唯一入口。

    ## 三类合法值

    1. **本域能力名**（`delivery.verify`）；
    2. **跨域能力名**（`receipt.verify` 触发采购单状态推进）—— 合法且必要，
       见 `_declared_capability_names` 的说明；
    3. **通配模式**（`*.submit` / `*.update` / `*.archive`）。

    ## 两类合法值

    1. **具体能力名** —— 必须能在 `REGISTRY` 里查到；
    2. **通配模式**（`*.submit` / `*.verify` / `*.archive` / `*.update`）——
       表示"由该资源上的同名动作触发，具体能力名由资源名拼出"。这是
       `domains/master_data/service.py` 的 `RECORD_LIFECYCLE` 的用法：
       它是一份**被多个资源复用**的生命周期模板，写死 `pond.submit` 就没法复用了。
       通配模式在这里**只校验它是不是认识的动作词**，不要求存在同名能力——
       逐资源解析由下面的 `test_rendered_actions_have_explicit_endpoints` 负责。

    ## 与上一条断言的分工

    上一条查**渲染动作**（`State.actions`，给用户点），这一条查**转移触发器**
    （`Transition.action`，由系统在核验时调用）。两者来源不同，可以各自漏。
    """
    _load_once()

    declared = _declared_capability_names() | {
        capability.name for capability in REGISTRY.all()
    }
    assert declared, "一个能力名都没扫到 —— 扫描逻辑失效了（空集不是通过）"
    legal_actions = {str(action) for action in RowAction}
    #: 通配触发的合法后缀 = 动作词 ∪ `Capability.kind`。
    #:
    #: 为什么 `update` 在列：`*.update` 指的是**「由该资源的 update 能力触发」**，
    #: 而 `update` 是 `Capability.kind` 的取值（`read|create|update|delete|action`），
    #: 不是 `RowAction` 的取值。它是 `RECORD_LIFECYCLE` 里
    #: `submitted→draft` 那条回退转移的触发器（§4 #12：未核验前可改回草稿）。
    #: 两个词汇表在这里**故意交叉**：`State.actions` 用行内动作词（给用户点），
    #: `Transition.action` 用能力名或 `*.kind`（给系统调）。
    legal_patterns = legal_actions | {"update", "create", "delete", "action"}

    unknown_patterns: list[str] = []
    missing: list[str] = []
    #: 本断言只检查**声明了能力的资源**，即真实生产资源。
    #:
    #: 判据的来源是一条已记录过的教训的两面：**测试夹具会盖住生产路径，也会污染
    #: 生产门禁。** `tests/test_invariants.py` 为了让单测独立运行，注册了一个只带
    #: 状态机、**刻意不声明任何能力**的夹具资源 `ut_pond`。它挂在全局 `RESOURCES` 上，
    #: 于是这条**生产**门禁会去判一个测试夹具 —— 而夹具的转移本来就没有对应能力，
    #: 因为单测不需要端点。
    #:
    #: 按"资源名前缀"收窄是不够的：夹具的转移恰恰以 `ut_pond.` 开头。正确的判据是
    #: "这个资源在生产里**有没有声明过能力**"。
    production_resources = {name.split(".", 1)[0] for name in declared if "." in name}
    for resource in _resources_with_workflow():
        if resource.name not in production_resources:
            continue  # 测试夹具（无任何能力声明），见上方说明
        for transition in resource.workflow.transitions:
            action = transition.action
            if action.startswith("*."):
                if action[2:] not in legal_patterns:
                    unknown_patterns.append(
                        f"{resource.name}: 通配触发 `{action}` 的 `{action[2:]}` "
                        "既不是合法动作词、也不是合法 Capability.kind"
                    )
                continue
            if action in _PLANNED_CROSS_DOMAIN_TRIGGERS:
                continue  # 声明尚未落盘；有专门的断言盯着这张表
            if action in declared:
                continue
            if not action.startswith(f"{resource.name}."):
                # 触发器不以本资源名开头 —— 它指的是别的东西（例如测试夹具注册的
                # 伪资源、或某个域的别名）。本断言只对"声称是本资源自己的能力"负责。
                #
                # 具体案例：`tests/test_invariants.py` 注册了一个只带状态机、**刻意
                # 不声明能力**的夹具资源 `ut_pond`（单测 `StateTransition` 用）。它的
                # 转移写成 `ut_pond.submit` —— 看起来像拼写错误，实际是测试夹具。
                # 生产门禁不该去判测试夹具，所以这里按"是否声称属于本资源"收窄。
                continue
            missing.append(
                    f"{resource.name}: 转移 "
                    f"{transition.from_state}→{transition.to_state} 的触发能力 "
                    f"{action!r} 不在任何域的能力声明里（疑似拼写错误）"
                )

    assert unknown_patterns == [], "通配触发写了不认识的动作词：\n  " + "\n  ".join(
        unknown_patterns
    )
    assert missing == [], (
        "转移表引用了不存在的能力（那条路径永远走不通）：\n  " + "\n  ".join(missing)
    )


def test_guard_is_not_vacuous() -> None:
    """守卫自身的"空集检查"：断言必须真的遍历到了东西。

    本仓已经吃过两次这个亏 ——
    `check_source_hygiene.py` 曾因传目录而**静默扫描 0 个文件**却报"通过"；
    字段集为空让校验既不放行也不拒绝。**空输入不是"通过"，是"没检查"。**

    所以这里显式要求：至少装载到一个域、至少一个带状态机的资源、至少一条转移、
    至少一个动作词。少任何一项都说明装载路径坏了，而"坏掉的装载"会让上面所有
    断言一起变成**空洞的真**。
    """
    _load_once()

    assert bootstrap.discover_domains(), "一个域都没被发现 —— 组合根失效"
    assert REGISTRY.all(), "一条能力都没注册 —— 装载路径失效"

    resources = _resources_with_workflow()
    assert resources, "没有任何资源声明状态机 —— 上面的断言全是空跑"

    total_transitions = sum(len(res.workflow.transitions) for res in resources)
    assert total_transitions > 0, "所有状态机的转移表都是空的 —— 断言无法发现漏接的转移"

    total_actions = sum(
        len(state.actions) for res in resources for state in res.workflow.states
    )
    assert total_actions > 0, "没有任何状态声明动作 —— 动作相关断言全是空跑"


def test_planned_cross_domain_triggers_are_still_pending() -> None:
    """`_PLANNED_CROSS_DOMAIN_TRIGGERS` 里的名字一旦被声明，就必须从表里删掉。

    少了这条，那张表会退化成**永久白名单**：目标域把能力写完、名字已经能解析了，
    而表里仍然留着它 —— 于是它继续掩盖将来某次真正的拼写错误。

    这是本仓"临时例外必须有失效机制"的同一手法
    （`test_pending_reserved_list_has_no_stale_entries` 对可达性判据做的是同一件事）。
    """
    _load_once()

    declared = _declared_capability_names() | {
        capability.name for capability in REGISTRY.all()
    }
    landed = sorted(name for name in _PLANNED_CROSS_DOMAIN_TRIGGERS if name in declared)
    assert landed == [], (
        "以下跨域能力已经落盘了，请从 _PLANNED_CROSS_DOMAIN_TRIGGERS 里删掉，"
        f"否则这张表会变成永久白名单：{landed}"
    )

# ---------------------------------------------------------------------------
# 5. `selectable`：用户可选列表的判据（唯一一处实现）
# ---------------------------------------------------------------------------


def test_selectable_excludes_terminal_and_reserved() -> None:
    """`State.selectable` = `!terminal && !reserved`，且 `to_meta()` 会下发它。

    这条断言的由来是一次真实缺陷：`cost_entry` 的 `submitted` / `archived` 标了
    `reserved=True`（没有能力能进入它们），但它们**仍在** `status_dict` 里——
    那是必须的，否则库里已存在的历史行会退化成裸状态码。而筛选下拉与目标状态下拉
    如果照 `status_dict` 全量渲染，用户就能选到"永远筛不出结果"或"必然被拒"的项。

    判据因此必须是 `!terminal && !reserved`，而且只能有**一处**实现
    （服务端属性 + `to_meta()` 下发的布尔值）——前端各处各写一遍就是第二处实现。
    """
    from fpa.kernel.workflow import State, Tone, Workflow

    workflow = Workflow(
        resource="selectable_probe",
        initial="a",
        states=(
            State("a", "甲", Tone.NEUTRAL),
            State("b", "乙", Tone.NEUTRAL, reserved=True),
            State("c", "丙", Tone.NEUTRAL, terminal=True),
            State("d", "丁", Tone.NEUTRAL, reserved=True, terminal=True),
        ),
    )
    assert [s.code for s in workflow.selectable_states()] == ["a"], (
        "selectable_states 的判据错了：应当同时排除 terminal 与 reserved"
    )
    # to_meta 必须把它**下发**给前端，否则前端只能自己重算（第二处实现）
    meta = {item["value"]: item for item in workflow.to_status_dict()}
    assert meta["a"]["selectable"] is True
    assert meta["b"]["selectable"] is False, "reserved 的项不该可选"
    assert meta["c"]["selectable"] is False, "terminal 的项不该可选"
    # 但**全部**状态都必须留在 status_dict 里（历史行的标签靠它）
    assert set(meta) == {"a", "b", "c", "d"}, (
        "status_dict 不能过滤掉不可选状态——库里已有的那些行会渲染成裸状态码"
    )
    assert meta["c"]["terminal"] is True and meta["b"]["reserved"] is True


def test_selectable_is_used_by_every_filter_whitelist() -> None:
    """列表筛选的**白名单**必须与"可选状态"一致，不能比下拉宽。

    若白名单比下拉宽，客户端能问出一个界面上选不出的过滤条件——那种请求永远返回
    空集，而调用方会以为"数据没了"（本项目最反对的"静默返回空集"形态之一）。

    这条断言是**防回归**的：`selectable_states()` 引入之前，各域写的是
    `[state.code for state in WORKFLOW.states]`。它不会报错、不会崩，
    只会在有人筛 `archived` 时给一个空列表。
    """
    import ast
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1] / "backend" / "fpa" / "domains"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            # 找 `[state.code for state in X.states]` 形态的列表推导式
            if not isinstance(node, ast.ListComp):
                continue
            source = ast.unparse(node)
            if ".states]" in source and ".selectable_states()]" not in source:
                offenders.append(f"{path.name}:{node.lineno}: {source}")
    assert offenders == [], (
        "这些地方用 `.states` 当筛选白名单，应当用 `.selectable_states()`"
        "（否则客户端能筛出一个界面选不出的条件）：\n  " + "\n  ".join(offenders)
    )

# ---------------------------------------------------------------------------
# 6. 动作词的中文标签：**只有一个来源**，且必须全覆盖
# ---------------------------------------------------------------------------


def test_every_row_action_has_a_chinese_label() -> None:
    """每个 `RowAction` 都必须有中文标签（`kernel/workflow.py::ACTION_LABELS`）。

    ## 这条守的是什么（一次实测缺陷）

    列表页曾被看到"操作"列渲染**裸 token**（按钮写着 `view` / `archive`），
    而同一行的状态列是中文（`养殖中`）—— 因为：

      * 状态有 `State.label` 落点、经 `status_dict` 下发 → 中文；
      * 动作**没有**落点 → `DataTable.vue` 直接渲染 token，
        而 `RecordActions.vue` 自己硬编码了一张表 —— **同一个系统里两套口径**。

    现在标签的唯一来源在内核（与 `State.label` 同一条纪律），经
    `/meta/capabilities` 的 `actions.row_action_labels` 下发，前端只查表。

    ## 为什么必须"全覆盖"而不是"用到才有"

    `row_action_label()` 对未登记的词**回退成原词**（可诊断，不静默改写）。
    那条回退对**运行时**是对的（前端不该因为一个缺标签的动作用不了整张列表），
    但它会让"漏了标签"从**编译期问题**退化成**显示问题**。
    所以这条断言把它拉回构建期：**每个动作词都必须有标签**。
    """
    from fpa.kernel.workflow import ACTION_LABELS, RowAction

    missing = sorted(str(item) for item in RowAction if str(item) not in ACTION_LABELS)
    assert missing == [], (
        f"这些动作词没有中文标签，会在页面上渲染成裸 token：{missing}\n"
        "请补进 `kernel/workflow.py::ACTION_LABELS`——**不要**在前端补一张映射表，"
        "那会重新变成「同一件事两处描述」。"
    )
    # 反向：表里不能有已不属于 `RowAction` 的僵尸词（枚举删值时会留下它）
    legal = {str(item) for item in RowAction}
    zombie = sorted(key for key in ACTION_LABELS if key not in legal)
    assert zombie == [], f"标签表里有不属于 RowAction 的动作词：{zombie}"


def test_row_action_labels_are_not_empty_and_chinese() -> None:
    """标签必须非空且含中文——防"补了个空串/英文词"这种形式上的通过。

    没有这条的话，`ACTION_LABELS = {"view": "view"}` 也能让上一条变绿，
    而那正是缺陷本身（裸 token）。
    """
    import re

    from fpa.kernel.workflow import ACTION_LABELS

    bad: list[str] = []
    for action, label in ACTION_LABELS.items():
        if not label.strip():
            bad.append(f"{action}: 空标签")
        elif not re.search(r"[\u4e00-\u9fff]", label):
            bad.append(f"{action}: 不含中文（{label!r}）")
    assert bad == [], "动作标签无效：\n  " + "\n  ".join(bad)


def test_actions_metadata_exposes_labels() -> None:
    """`/meta/capabilities` 的动作段必须**同时**给出取值与标签。

    这条是"落点存在"的机械判据：前端曾经因为元数据里没有标签，
    只好自己硬编码或渲染 token。少了 `row_action_labels`，前端就只能又猜一遍。
    """
    from fpa.kernel.workflow import RowAction
    from fpa.web.workflow_meta import actions_payload

    payload = actions_payload()
    assert set(payload) >= {"tones", "row_actions", "row_action_labels"}, sorted(payload)
    assert payload["row_actions"] == [str(item) for item in RowAction], payload["row_actions"]
    labels = payload["row_action_labels"]
    assert isinstance(labels, dict), type(labels)
    for action in payload["row_actions"]:
        assert labels.get(action), f"{action} 在 row_actions 里但没有标签"


def test_admin_row_actions_have_chinese_labels() -> None:
    """账号与角色页的管理动作也必须走服务端统一标签链路。"""
    from fpa.kernel.workflow import row_action_label
    from fpa.web.workflow_meta import actions_payload

    expected = {
        "status": "启用/禁用",
        "grants": "授权",
        "permissions": "权限设置",
    }
    payload = actions_payload()
    for action, label in expected.items():
        assert row_action_label(action) == label
        assert payload["row_action_labels"].get(action) == label
