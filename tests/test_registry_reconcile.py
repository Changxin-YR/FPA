"""对账工具自身的守卫：**口径失效必须吵，不能静默**。

`tools/registry_reconcile.py` 从 markdown 现场解析台账（不手抄常量表），
因此它有一条独有的失效模式：**文档改格式 → 解析器悄悄解析到 0 条、或把某个域的能力
算进上一个域 → 工具报告「无缺口」**。那比没有工具更糟：它把"没人检查"伪装成"检查通过"。

本文件钉住解析口径的两端：

  1. §1 必须解析出 69 条能力、§4 必须解析出 21 条规则（口径漂移即失败）；
  2. 两种**已实测发生过的**破坏方式必须被当场拒绝：
     * 删掉一条能力行 → 域标题声明的条数与解析行数不一致；
     * 改掉域标题形态（如 `— 13 条` 改成 `（13 条）`）→ 该节无法识别，必须报错，
       而不是把它的能力静默记到上一个域名下（实测过：那时总量守恒也发现不了）。

注意：**不断言「没有缺口」**——缺口是施工进度的函数，会随五个域的推进变化。
这里只钉住"解析口径"，让缺口数字保持可信。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from registry_reconcile import (  # noqa: E402
    ALTERNATIVE_ENFORCEMENT_MARKER,
    DocRule,
    Reconcile,
    compare,
    kernel_type_names,
    parse_doc,
    parse_runtime,
)

DOC = ROOT / "docs" / "CAPABILITY_REGISTRY.md"


def test_document_parses_all_declared_capabilities() -> None:
    doc = parse_doc(DOC, kernel_type_names())
    # 权威清单的条数会随域施工变化（69 -> 73：master_data 补齐了缺口）。
    # 这里钉的是"解析口径"，不是"条数不变"：断言解析结果与**文档自己声明的**总数一致。
    declared_total = sum(doc.doc_counts.values())
    assert len(doc.doc_capabilities) == declared_total, (
        f"§1 解析到 {len(doc.doc_capabilities)} 条能力，文档各域标题声明合计 {declared_total} 条。"
        "文档结构与解析口径不一致时，对账工具会静默报「无缺口」，所以这里必须失败。"
    )
    assert len(set(doc.doc_capabilities)) == len(doc.doc_capabilities), "能力名重复，说明解析到了非能力表格的行"


def test_document_parses_all_invariant_rules() -> None:
    doc = parse_doc(DOC, kernel_type_names())
    # 21 条原有规则 + #22「取消必须填原因」（补收，见 §4 #22）
    assert len(doc.doc_rules) == 22, f"§4 解析到 {len(doc.doc_rules)} 条规则，期望 22 条"
    assert sorted(doc.doc_rules) == list(range(1, 23)), sorted(doc.doc_rules)
    # 例外只有一种：**显式声明了「替代强制方式」**的规则（它的强制点不是不变量，
    # 因此本来就没有类型名可解析 —— 例如 §4 #21 由 DB 唯一键 + 服务预检强制）。
    # 这类规则仍必须通过 `compare()` 落到 `[B-声明]` 里被**打印出来**，见
    # `test_declared_alternative_enforcement_is_listed_not_silent`。
    without_types = [
        number
        for number, rule in doc.doc_rules.items()
        if not rule.types and not rule.declaration
    ]
    assert without_types == [], f"这些规则解析不出类型名：{without_types}"


def test_declared_alternative_enforcement_is_listed_not_silent() -> None:
    """`[B-声明]`：「替代强制」必须**打印出来**，不能变成万能免死金牌。

    为什么这条守卫用**注入的规则**而不是当前文档里的某一条：这条豁免的边界必须与
    "此刻恰好有哪条规则用到它"无关 —— 否则它就成了对某个规则名开后门。
    注入一条合成的 §4 规则（没有任何能力挂它的类型、但声明了「替代强制」并指名了
    一个已注册能力），然后验证四件事：

      1. 它落在 `declared_alternatives` 里（会被渲染进报告，**不是静默跳过**）；
      2. **它不算缺口**（`[B-声明]` 的语义：列出供复核、不报警）；
      3. 抽掉标记词后，同一条规则**退回 `[B]`**（规则退化成建议）—— 豁免由文档证据驱动；
      4. 标记词留着但**不指名任何已注册能力**时，豁免**无效**（回退到 `[B]`）——
         这是"凭一句话就能豁免一切"的堵口。
    """
    from registry_reconcile import (
        ALTERNATIVE_ENFORCEMENT_MARKER,
        DocRule,
        Reconcile,
        compare,
        parse_doc,
    )

    kernel = kernel_type_names()
    runtime, _ = parse_runtime()
    if not runtime:
        pytest.skip("组合根此刻装载不起来（他人进行中改动）—— 本守卫验的是文档侧口径")
    doc = parse_doc(DOC, kernel)

    # 一条"没有任何能力挂它的类型、但声明了替代强制"的合成规则（编号用 999，不与真实规则冲突）。
    #
    # 刻意让 `types=()`：`[B]` 的第一道判据是"规则的类型集里有任意一个被某个能力挂着"，
    # 若借用一个真被挂着的类型（例如 OptimisticLock），探针会走 `continue` 而不进本栏 ——
    # 那样测的就不是这条豁免的边界了。`types=()` 让探针**必然**落到 `[B]` 分支。
    some_capability = sorted(runtime)[0]
    probe = DocRule(
        number=999,
        title="**合成探针**",
        capabilities=(some_capability,),
        types=(),
        declaration=f"`{some_capability}`：{ALTERNATIVE_ENFORCEMENT_MARKER}（合成探针用）",
        scope_text=f"`{some_capability}`",
    )

    def with_rule(rule: DocRule) -> Reconcile:
        return Reconcile(
            doc_capabilities=doc.doc_capabilities,
            doc_kinds=doc.doc_kinds,
            doc_methods=doc.doc_methods,
            doc_paths=doc.doc_paths,
            doc_counts=doc.doc_counts,
            doc_rules={**doc.doc_rules, 999: rule},
        )

    # 基线：没有这条探针时，`OptimisticLock` 是有能力挂的，所以先确认探针形状本身会落 [B]
    gaps_ok = compare(with_rule(probe), runtime, kernel)
    assert 999 in gaps_ok.declared_alternatives, (
        "声明了「替代强制」且指名了已注册能力的规则没有落到 [B-声明] —— "
        "要么解析没读到，要么它被静默忽略了（后者正是这一栏禁止的形态）"
    )
    assert gaps_ok.declared_alternatives[999][1], "指名能力为空，豁免无效"
    assert 999 not in gaps_ok.undefended_rules

    # 护栏 1：抽掉标记词 → 退回 [B]
    weakened_rule = DocRule(
        number=999,
        title=probe.title,
        capabilities=probe.capabilities,
        types=probe.types,
        declaration=probe.declaration.replace(ALTERNATIVE_ENFORCEMENT_MARKER, "不再声明"),
        scope_text=probe.scope_text,
    )
    gaps_weakened = compare(with_rule(weakened_rule), runtime, kernel)
    assert not gaps_weakened.declared_alternatives, (
        "去掉标记词后仍被豁免 —— 说明豁免不是由文档里的证据驱动的"
    )
    assert 999 in gaps_weakened.undefended_rules, (
        "去掉声明后应退回 [B]（规则退化成建议），否则这条豁免没有可核查的边界"
    )

    # 护栏 2：写着标记词但不指名任何已注册能力 → 豁免无效
    anonymous_rule = DocRule(
        number=999,
        title=probe.title,
        capabilities=probe.capabilities,
        types=probe.types,
        declaration=f"本规则采用{ALTERNATIVE_ENFORCEMENT_MARKER}方式（但没说是谁）。",
        scope_text=probe.scope_text,
    )
    gaps_anonymous = compare(with_rule(anonymous_rule), runtime, kernel)
    assert not gaps_anonymous.declared_alternatives, (
        "不指名任何已注册能力也能豁免 —— 这就是万能免死金牌，必须拒绝"
    )
    assert 999 in gaps_anonymous.undefended_rules


def test_per_capability_types_are_not_redistributed_by_the_rule_row(tmp_path: Path) -> None:
    """**一行多能力**时，类型不得按"所有列出的能力"分配。

    守的是本次实测过的那个缺陷（它曾让 `[D]` 报出假缺口）：
    §4 的一条规则横跨两步（申请 + 核验），而两步各自挂的类型不同。文档原先只能把
    类型集写在整行共享的 `invariant=[...]` 里，于是工具把**同一组类型**算给了**两个**
    能力 —— 规则 #20 因此被读成"两步都要挂对方的类型"，报出"实现漏挂"，而真实原因
    是"这两步本来就不是同一组类型"。报错方向指错了地方，比不报更浪费时间。

    注入式判据（不依赖当前文档里恰好有哪条规则）：造一行两能力、并在其后跟一张
    逐能力小表（只给第一个能力一种类型），然后断言：
      * 第一个能力拿到的正是它那一种类型；
      * 第二个能力**不被算上**那种类型（这就是"不再误分配"）。
    """
    text = DOC.read_text(encoding="utf-8")
    lines = text.split("\n")
    # 插在 §4 表格的最后一行之后（保持"能力名必须已存在于 §1"这一前提）
    anchor = next(
        index for index, line in enumerate(lines) if line.startswith("| 22 |")
    )
    synthetic = [
        "",
        "| 999 | **注入探针：两步各自挂不同类型** | `pond.create` + `pond.update` | 合成行 |"
        " `invariant=[RequiredField(fields=[\"code\"])]` |",
        "",
        "| 能力 | 本行真正强制它的类型 |",
        "|---|---|",
        "| `pond.create` | `RequiredField` |",
        "| `pond.update` | （无） |",
    ]
    lines[anchor + 1 : anchor + 1] = synthetic
    target = tmp_path / "registry.md"
    target.write_text("\n".join(lines), encoding="utf-8", newline="\n")

    doc = parse_doc(target, kernel_type_names())
    rule = doc.doc_rules[999]
    assert rule.capabilities == ("pond.create", "pond.update"), rule.capabilities
    assert rule.types_for("pond.create") == ("RequiredField",), rule.types_for("pond.create")
    assert rule.types_for("pond.update") == (), (
        "`pond.update` 被算上了**只属于第一步**的类型 —— 这正是『整行类型按所有列出的"
        f"能力分配』的老毛病：{rule.types_for('pond.update')}"
    )


def test_declared_types_exist_in_the_kernel() -> None:
    kernel = set(kernel_type_names())
    doc = parse_doc(DOC, kernel)
    declared = {name for rule in doc.doc_rules.values() for name in rule.types}
    missing = sorted(declared - kernel)
    assert missing == [], f"文档声明的类型在内核里不存在：{missing}"


def test_parser_refuses_a_heading_it_cannot_read(tmp_path: Path) -> None:
    text = DOC.read_text(encoding="utf-8")
    original = next(
        (line for line in text.split("\n") if line.startswith("### 1.4 ")),
        None,
    )
    assert original is not None, "测试前提失效：文档里找不到 §1.4 的域小节标题"
    mutated = text.replace(original, "### 1.4 master_data（若干条）", 1)
    assert mutated != text, "测试前提失效：替换没有生效"
    target = tmp_path / "mutated.md"
    target.write_text(mutated, encoding="utf-8", newline="\n")
    with pytest.raises(SystemExit) as excinfo:
        parse_doc(target, kernel_type_names())
    assert "无法识别" in str(excinfo.value), str(excinfo.value)


def test_parser_refuses_a_dropped_capability_row(tmp_path: Path) -> None:
    """**删掉一行能力**必须被拒绝（节标题声明条数 vs 实际解析行数不一致）。

    ## 判据为什么改成"按前 4 格定位"而不是"整行字面量逐字存在"

    早先的写法是把整行字面量 `.replace()` 掉、断言 `mutated != text`，于是它把
    **那一行的列格式**也钉死了。§1 表格随后被统一加反引号（`| read |` → `` | `read` | ``），
    那一行被改过，本测试就报"测试前提失效" —— **它守的是"删一行能力要被拒绝"，
    却因为一个与它无关的格式调整变红**。这正是本项目反复在抓的"判据与意图不符"：
    守卫该钉**行为**（解析器会发现条数不符），不是**排版**。

    所以现在只依赖"这是 `partner.list` 那一行"（名字/方法/路径/标题）。**这不是放宽**：
    真删掉那一行时，下面仍然要求解析器抛 `SystemExit` 且消息提到"标题声明"。
    配套的正向守卫见 `test_capability_row_reformatting_is_not_a_false_alarm`。
    """
    text = DOC.read_text(encoding="utf-8")
    lines = text.split("\n")
    marker = "| `partner.list` | GET | `/api/v1/partners` |"
    index = next((i for i, line in enumerate(lines) if line.startswith(marker)), None)
    assert index is not None, f"测试前提失效：文档里找不到这一行（{marker}）"

    del lines[index]
    mutated = "\n".join(lines)
    assert mutated != text, "测试前提失效：删除后内容应当变化"

    target = tmp_path / "dropped.md"
    target.write_text(mutated, encoding="utf-8", newline="\n")
    with pytest.raises(SystemExit) as excinfo:
        parse_doc(target, kernel_type_names())
    assert "标题声明" in str(excinfo.value), str(excinfo.value)


def test_capability_row_reformatting_is_not_a_false_alarm(tmp_path: Path) -> None:
    """上一条的**配套正例**：只改那一行的**排版**，必须正常解析（不误报）。

    为什么要配这一条：把上一条从"逐字匹配"改成"按前 4 格定位"之后，得能区分
    **"误报被修掉"** 与 **"守卫被削弱"**。两条一起才说明：
      * 格式变化 → 不红（本测试）；
      * 行真被删 → 仍红（上一条）。
    """
    text = DOC.read_text(encoding="utf-8")
    lines = text.split("\n")
    marker = "| `partner.list` | GET | `/api/v1/partners` |"
    index = next(i for i, line in enumerate(lines) if line.startswith(marker))

    cells = lines[index].strip().strip("|").split("|")
    cells[-1] = " `before_after` "          # 只动最后一格（audit）的排版
    lines[index] = "|" + "|".join(cells) + "|"

    target = tmp_path / "reformatted.md"
    target.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    doc = parse_doc(target, kernel_type_names())   # 不该抛 SystemExit
    assert "partner.list" in doc.doc_capabilities


def test_note_marker_is_not_parsed_as_enforcement_scope() -> None:
    """`⚠️` 之后是给人读的说明，不得被算进"强制能力"。

    实测过一次假缺口：在 §4 #7 的说明里写 `` `cost.period.close` 不挂 PeriodOpen ``，
    解析器就把这个能力算进了 #7 的强制能力，于是报"文档声称却没人挂"。
    规则：强制能力列的内容到 `⚠️` 为止。
    """
    doc = parse_doc(DOC, kernel_type_names())
    assert "cost.period.close" not in doc.doc_rules[7].capabilities, (
        "§4 #7 的强制能力被解析成了 7 条——说明区里的能力名被误算了。"
        "请确认说明写在 ⚠️ 之后，且解析器按 SCOPE_NOTE_MARKER 截断。"
    )
    assert len(doc.doc_rules[7].capabilities) == 6, doc.doc_rules[7].capabilities


def test_fixed_route_classification_separates_contract_rows_from_gaps(load_all_status) -> None:
    """`[A2]`：有固定路由的契约行不再算缺口，而真正没有实现的仍在 `[A]`。

    守的是两件事：
      1. 分类**真的用了路由表**（`meta.capabilities` 有 `GET /api/v1/meta/capabilities`
         这条固定路由 → 进 [A2]）；
      2. 分类**没有变成"宽恕一切"**：`meta.resources` 声明的
         `GET /api/v1/meta/resources` 全仓没有定义（`DECISIONS.md` Q14 已删该独立路由），
         它必须**仍然**出现在 [A] 里。

    判据必须是"实际路由表"，不是名字或白名单——否则下一步就会有人往里加名字。
    """
    doc = parse_doc(DOC, kernel_type_names())
    runtime, _ = parse_runtime()
    gaps = compare(doc, runtime, kernel_type_names())

    assert gaps.route_map_note == "", (
        f"没取到真实路由表，[A]/[A2] 的划分不可信：{gaps.route_map_note}"
    )
    assert "meta.capabilities" in gaps.fixed_route_capabilities, (
        "`meta.capabilities` 有固定路由，应归入 [A2]；"
        f"实际 [A2]={sorted(gaps.fixed_route_capabilities)}"
    )
    # 分类必须**恰好**覆盖"文档有、注册表无"的那些：既不能全塞进 [A2]，也不能漏进 [A]。
    # 这条断言刻意不点名某一行（早期版本点名 `meta.resources`，而那一行随后被
    # 文档维护者按 Q14 删掉了 —— 点名式守卫会因此变成假红）。
    declared_only = {
        name for name in doc.doc_capabilities if name not in runtime
    }
    assert set(gaps.fixed_route_capabilities) | set(gaps.missing_capabilities) == declared_only, (
        "`[A] ∪ [A2]` 必须恰好等于「文档声明了、运行时没有」的能力集合；"
        f"漏掉的：{sorted(declared_only - set(gaps.fixed_route_capabilities) - set(gaps.missing_capabilities))}"
    )
    assert not (set(gaps.fixed_route_capabilities) & set(gaps.missing_capabilities)), (
        "同一能力不能既算 [A2] 又算 [A]"
    )
    # 有运行时能力的不该出现在 [A2]（分类只针对"文档有、注册表无"的那些）
    assert not (set(gaps.fixed_route_capabilities) & set(runtime)), sorted(
        set(gaps.fixed_route_capabilities) & set(runtime)
    )


def test_tenant_dimension_is_split_into_declared_and_delegated() -> None:
    """`[G1]/[G2]/[G3]` 三栏：判据只看**会执行的代码**。

    为什么必须拆：旧判据的线索词含裸 `organization`，于是
    `ReferencedStatus` 因为**注释**里的一句 organization 被算成"有租户意识"——而它只调
    `scope.allows_row`，`Scope` 又不比 `organization_id`。**假阴性最坏**：优先级清单
    把不安全的说成安全，就没人去看挂它的那 6 条能力了。

    三栏语义：
      * G1 真按租户键比较/查询（代码里出现 `organization_id`）；
      * G2 委托给 `Scope` ⇒ 继承 Scope 的盲区（只比 farm/area/pond/personal）；
      * G3 既无租户键、也不做行级判定 ⇒ 完整盲区清单。
    """
    from registry_reconcile import tenant_dimension

    g1, g2, neither = tenant_dimension()
    assert g1 and neither, "判据没有产出结果 —— 失效了"

    # G1：真的按租户键 —— SameTenant 声明 scope_fields、NoOverlappingSource 有 tenant_keys
    assert "SameTenant" in g1, f"SameTenant 声明了 organization_id，应属 G1；实际 {g1}"
    assert "NoOverlappingSource" in g1, f"NoOverlappingSource 有 tenant_keys，应属 G1；实际 {g1}"

    # G1 新成员：`PeriodOpen`（t2 修复后谓词里带 `organization_id`，缺键即
    # `INTERNAL_ERROR`）。它曾是 G3 的**已实证**样本，所以这条断言有两个方向：
    # 修复被撤回时这里要红，而不是悄悄回到 G3 却仍然"测试通过"。
    assert "PeriodOpen" in g1, (
        f"PeriodOpen 现在按 tenant_keys 查期间（t2），应属 G1；实际 {g1}"
    )

    # G2：只委托 Scope 的类型必须被发现（这是旧判据漏掉的那一类）
    assert "ReferencedStatus" in g2, f"ReferencedStatus 委托 Scope，应属 G2；实际 {g2}"
    assert "ReferencedStatus" not in g1, (
        "ReferencedStatus 不该被算成『真按租户键』——它只在注释里提过 organization"
    )

    # G3：完整盲区清单里**不再**有 PeriodOpen（它已按租户键查，见上）。
    # 这一条守的是"修复不能只落在文档里"：只要实现里的 organization_id 再次消失，
    # PeriodOpen 会回到 G3，这里立刻红。
    assert "PeriodOpen" not in neither, (
        "PeriodOpen 已经按 tenant_keys（organization_id）查期间，不该还在 G3 租户盲清单里"
    )



def test_a_declared_path_absent_from_the_route_map_stays_a_gap(tmp_path: Path) -> None:
    """**注入式**证据：声明了能力、但 (method,path) 不在路由表里 → 必须落在 [A]，不是 [A2]。

    为什么不用"文档里恰好留着某条悬空行"来钉这条：那种做法会让守卫与文档内容互相绑死
    ——文档维护者按 Q14 删掉 `meta.resources` 之后，守卫立刻变红，而它想守的东西
    （分类没有被放宽）其实完好。所以这里**自己造一条**必然是 [A] 的行：

      * `zz_probe.get` 用真实域小节的表格式样插进 §1.1，路径故意取一个不存在的端点；
      * 断言它落在 `missing_capabilities`（[A]）而不是 `fixed_route_capabilities`（[A2]）；
      * 再断言当前文档里**没有** `meta.resources` 这一行（防止有人把它加回来却不给实现）。
    """
    text = DOC.read_text(encoding="utf-8")
    lines = text.split("\n")
    header_at = next(
        index for index, line in enumerate(lines) if line.startswith("| name | method | path |")
    )
    synthetic = (
        "| `zz_probe.get` | GET | `/api/v1/zz-probe/{zz_id}` | 注入探针 | read | — | none | normal "
        "| never | hidden | false | none |"
    )
    lines.insert(header_at + 2, synthetic)
    # 标题声明的条数也要一起 +1：解析器有一条"标题声明 == 实际解析行数"的守卫
    # （正是它挡住了"域标题形态变化时把整段能力算进上一个域"那个 bug）。注入式测试
    # 必须与它共存——顺手也证明了那条守卫仍然活着。
    heading_at = next(
        index for index, line in enumerate(lines) if line.startswith("### 1.1 ")
    )
    lines[heading_at] = lines[heading_at].replace("— 4 条", "— 5 条")
    mutated = tmp_path / "with_probe.md"
    mutated.write_text("\n".join(lines), encoding="utf-8", newline="\n")

    probe_doc = parse_doc(mutated, kernel_type_names())
    runtime, _ = parse_runtime()
    gaps = compare(probe_doc, runtime, kernel_type_names())

    assert "zz_probe.get" in "".join(probe_doc.doc_capabilities), "注入的行没被解析到 —— 测试前提失效"
    assert "zz_probe.get" in gaps.missing_capabilities, (
        "路径不在路由表里，必须落在 [A]；"
        f"实际 [A2]={sorted(gaps.fixed_route_capabilities)}"
    )
    assert "zz_probe.get" not in gaps.fixed_route_capabilities, "不存在的端点不得被算成固定路由"

    # 反方向：文档里不该再留着那条已知无定义的端点行
    assert "meta.resources" not in parse_doc(DOC, kernel_type_names()).doc_capabilities, (
        "`meta.resources` 已按 Q14 删行；若它被加回来，必须同时给出实现（否则它又会变成 [A] 缺口）"
    )


def test_limit_scan_reports_its_own_scale() -> None:
    """`[H]` 必须同时报"扫描规模"：结果为 0 时，先看它有没有真的扫到东西。

    由 purchase-dev 的自省引出：他报"我域 0 处可疑"，紧接着说明"0 处不是干净，
    是我域根本不用 `LIMIT 1`"（采购域只用分页 `LIMIT %s OFFSET %s`）。
    这是本仓那条纪律的又一个形状——**空输入不是通过，是没检查**。
    """
    from registry_reconcile import unordered_limit_one

    found, stats = unordered_limit_one()
    assert stats["files"] > 0, f"没有扫描到任何文件 —— 判据的扫描范围失效了：{stats}"
    assert stats["limit_one"] > 0, (
        "扫描范围内没有出现任何 `LIMIT 1`：那 [H] 的『无』就什么也没证明。"
        f"实际统计：{stats}"
    )
    assert len(stats) == 4 and "paged" in stats, f"规模统计缺项：{stats}"
    # 候选清单里的每一处都必须能定位（文件 + 行号 + 片段）
    for path, line_no, snippet in found:
        assert path.endswith(".py") and line_no > 0 and snippet, (path, line_no, snippet)


def test_partial_load_makes_check_fail_not_warn(monkeypatch) -> None:
    """**降级必须留红**：组合根只装载一部分域时，`--check` 不得返回 0。

    由 purchase-dev 的建议引出：他的 `load_registry_tolerantly()` 只打 WARN、不产出 FAIL，
    于是"降级"可能被"全绿"掩盖（他据此决定改成 master-data-finisher 的形态）。
    本工具的形态是**退出码**：部分装载 → `3`，把"我只查了一半"顶到调用方脸上。

    实现细节值得记一笔：这里**让 `bootstrap.load_all` 真的抛错**，而不是去改模块级
    `load_note`。因为 `main()` 会先调 `parse_runtime()` 把它重置，注入的假状态永远看不到
    （我第一版就是这么写错的，实测 `main` 返回 1 而不是 3）。**走真实路径**才不会自欺。
    """
    import yuxin.bootstrap as bootstrap
    import registry_reconcile as rr

    def _boom() -> None:
        raise RuntimeError("注入：模拟某个域 import 失败")

    monkeypatch.setattr(bootstrap, "load_all", _boom)
    assert rr.main(["--check"]) == 3, "部分装载时 --check 必须非零，否则降级会被读成全绿"


def test_documented_statistics_are_self_consistent() -> None:
    """§1 规模统计小节（原 §1.10.1）的统计必须自洽：四个维度各自「分项之和 == 合计」，且合计 == 解析出的能力数。

    守的是什么：旧版 `confirmation` 写 `never 46 · always 23` = **69**，而合计是 **73** ——
    **数字自相矛盾，却没有一条门禁会红**。它属于"看起来在强制、其实没人检查"的同一家族
    （与租户维度盲区、【G】那类问题同源）：算术自洽是**纯机械**的，没有理由靠人眼看。

    判据不含任何语义：只做加法。解析该表的正则若失效，这里会**报红**而不是静默跳过
    （"解析不到"不是"没有问题"）。
    """
    import re

    text = DOC.read_text(encoding="utf-8")
    # ★ 锚点按**标题文本**定位，不按小节号硬编码：当前迭代给 §1 补了 `workbench` 小节后，
    #   规模统计从 §1.10/§1.10.1 顺移成 §1.11/§1.11.1 —— 硬编码小节号会让本用例
    #   以 `ValueError: substring not found` 变红，而那是"文档改了编号"，不是统计不自洽。
    #   按文本找，编号再变也不会碎；找不到仍然报红（"解析不到"≠"没问题"）。
    head = re.search(r"####\s+1\.\d+\.1\s+权威统计（可复算）", text)
    assert head, "找不到 §1 的『权威统计（可复算）』小节 —— 口径失效（这不是「没问题」）"
    tail = re.search(r"####\s+1\.\d+\.2", text[head.end():])
    assert tail, "找不到紧随其后的下一小节 —— 口径失效"
    section = text[head.start(): head.end() + tail.start()]

    total_match = re.search(r"\|\s*\*\*合计\*\*\s*\|\s*\*\*(\d+)\*\*", section)
    assert total_match, "解析不到规模统计小节的合计行 —— 口径失效（这不是「没问题」）"
    total = int(total_match.group(1))

    # ① 合计 == 解析出的能力数
    doc = parse_doc(DOC, kernel_type_names())
    assert total == len(doc.doc_capabilities), (
        f"规模统计小节写合计 {total}，但 §1.1–§1.9 解析出 {len(doc.doc_capabilities)} 条能力"
    )

    # ② 逐域之和 == 合计（且与解析出的各域计数一致）
    domains = dict(re.findall(r"^\|\s*([a-z_]+)\s*\|\s*(\d+)\s*\|", section, re.M))
    assert sum(int(v) for v in domains.values()) == total, f"逐域分项之和 ≠ 合计：{domains}"
    parsed_by_domain: dict[str, int] = {}
    for domain in doc.doc_capabilities.values():
        parsed_by_domain[domain] = parsed_by_domain.get(domain, 0) + 1
    assert {k: int(v) for k, v in domains.items()} == parsed_by_domain, (
        f"规模统计小节的逐域计数与实际解析不一致：表 {domains} vs 解析 {parsed_by_domain}"
    )

    # ③ 四个维度：各自分项之和 == 合计
    dimensions = {
        "kind": ("read", "create", "update", "action", "delete"),
        "confirmation": ("never", "always", "by_key"),
                # `n/a` = **契约行**（auth.login / auth.logout / auth.me / meta.capabilities）：
        # 它们由 Flask 固定路由提供、**不在注册表里**，不属于任何 exposure 取值。
        # 但它们仍各占 §1 的一行，必须计入加总，否则「分项之和 == 合计」会假红。
        "agent_exposure": ("exposed", "hidden", "human_only", "n/a"),
        "idempotent": ("true", "false"),
    }
    for name, values in dimensions.items():
        parts = dict(re.findall(rf"`({'|'.join(values)})`\s*\*\*(\d+)\*\*", section))
        assert parts, f"解析不到维度 {name} 的分项 —— 口径失效"
        assert sum(int(v) for v in parts.values()) == total, (
            f"维度 {name} 的分项之和 ≠ 合计 {total}：{parts}"
        )
