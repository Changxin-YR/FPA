"""守卫：`docs/CAPABILITY_REGISTRY.md` 里对 `agent_exposure` 的断言必须与运行时一致。

## 这条守卫为什么存在

该文档自称"权威清单"，而 `tools/registry_reconcile.py --check` 一直全绿 ——
因为它逐能力只比对 `name / kind / method / path / risk / audit`，
`agent_exposure` 出现 **0** 次。于是下面这条漂移长期隐形：

    §5.2 声称 7 条 human_only；运行时实测只有 1 条（auth.password.change），
    其中 auth.login / auth.logout 根本不在注册表内，
    另 4 条（access.user.create / status / grants / role.permissions）
    实测是 exposed + risk=high + confirmation=always。

危害不是"今天有洞"（那 4 条有 HITL + 服务层 `_require_super_admin` 兜底），
而是**文档是自述的权威清单**：下一个人按 §5.2 做安全评估，会得出
"提权类能力对 Agent 全关"的错误结论。

## 判据

解析 §5.2 里那张"逐条对照"表（`| \`name\` | 实测值 | … |`），对每一行：
  * 实测值含"不在注册表"  -> 该 name **不得**出现在注册表；
  * 实测值含 `human_only` -> 运行时的 exposure 必须是 `human_only`；
  * 实测值含 `exposed`    -> 运行时的 exposure 必须是 `exposed`。
这样文档里**任何**一条 exposure 断言都会被校验；新增能力时若只改代码不改文档，
或反之，本条就红。
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import load_all_status

_DOC = Path(__file__).resolve().parents[1] / "docs" / "CAPABILITY_REGISTRY.md"

#: 只扫 §5.2 这一节（到下一个三级标题为止）。
_SECTION = re.compile(r"### 5\.2 .*?(?=\n### )", re.S)

#: 表格行：`> | \`cap.name\` | 实测值 | 理由 |`（blockquote 前缀可有可无）
_ROW = re.compile(r"^\s*>?\s*\|\s*`([a-z_][a-z0-9_.]*)`\s*\|\s*([^|]+?)\s*\|", re.M)


def _doc_exposure_claims() -> list[tuple[str, str]]:
    text = _DOC.read_text(encoding="utf-8")
    section = _SECTION.search(text)
    assert section is not None, "CAPABILITY_REGISTRY.md 里找不到 §5.2 小节"
    claims: list[tuple[str, str]] = []
    for name, verdict in _ROW.findall(section.group(0)):
        claims.append((name, verdict.strip()))
    return claims


def test_文档里的_exposure_断言与运行时一致():
    load_all_status()
    from fpa.kernel.capability import REGISTRY

    runtime = {
        c.name: (c.agent_exposure.value if hasattr(c.agent_exposure, "value")
                 else str(c.agent_exposure))
        for c in REGISTRY.all()
    }

    claims = _doc_exposure_claims()
    # 空集不得当成通过：解析器若失效，"无断言"会伪装成"全对"。
    assert len(claims) >= 6, f"只从 §5.2 解析出 {len(claims)} 条断言，解析器可能坏了"

    problems: list[str] = []
    for name, verdict in claims:
        if "不在注册表" in verdict:
            if name in runtime:
                problems.append(f"文档称 `{name}` 不在注册表，实际运行时是 {runtime[name]}")
        elif "human_only" in verdict:
            if runtime.get(name) != "human_only":
                problems.append(f"文档称 `{name}` 是 human_only，实际是 {runtime.get(name)!r}")
        elif "exposed" in verdict:
            if runtime.get(name) != "exposed":
                problems.append(f"文档称 `{name}` 是 exposed，实际是 {runtime.get(name)!r}")

    assert not problems, (
        "docs/CAPABILITY_REGISTRY.md 的 agent_exposure 断言与运行时不符"
        "（`registry_reconcile.py` 不校验这一项，所以只有本条能拦）：\n  "
        + "\n  ".join(problems)
    )


def test_默认_hidden_当前一次都没生效这件事被文档记录():
    """`AgentExposure` 默认值是 HIDDEN，但 102 条全部显式声明 —— 这个事实必须在文档里。

    它不影响今天的暴露面（每条都显式 + 权限码把关），但意味着"默认安全"的保护
    **只在纸面上**：新增能力的人照抄邻居的 `agent_exposure=_EXPOSED` 就自动获得
    AI 可调用权，且没有任何判据会拦。钉住它，避免这层薄保护被当成"已生效"。
    """
    load_all_status()
    from fpa.kernel.capability import REGISTRY

    values = [
        c.agent_exposure.value if hasattr(c.agent_exposure, "value") else str(c.agent_exposure)
        for c in REGISTRY.all()
    ]
    assert len(values) > 50, f"只读到 {len(values)} 条能力，判据可能失效"
    assert "hidden" not in values, (
        "居然有 hidden 能力了 —— 请同时更新 §5.4 的读数与本节断言"
    )
    doc = _DOC.read_text(encoding="utf-8")
    assert "默认值一次都没生效" in doc, (
        "文档里没有记录『HIDDEN 默认值一次都没生效』这个事实 —— "
        "它会被误读成『默认安全已经生效』"
    )


# ---------------------------------------------------------------------------
# §1 能力表的两列：`confirmation`（第 9 列）与 `agent_exposure`（第 10 列）
# ---------------------------------------------------------------------------

#: 只扫 §1.1–§1.9 的能力表行（`| \`name\` | method | … |`）。
_TABLE_ROW = re.compile(r"^\|\s*`([a-z_][a-z0-9_.]*)`\s*\|", re.M)


def _doc_table_cells(name: str) -> list[str] | None:
    text = _DOC.read_text(encoding="utf-8")
    for line in text.split("\n"):
        if not line.startswith("| `"):
            continue
        cells = line.split("|")
        if len(cells) < 13:
            continue
        if cells[1].strip().strip("`") == name:
            return [c.strip() for c in cells]
    return None


def test_能力表的_confirmation_与_exposure_两列与运行时一致():
    """这两列是**安全相关**的：它们决定"模型点得动吗"与"要不要人工确认"。

    实测曾同时存在两类错配（`tools/registry_reconcile.py` 的 `[I]` 只比 `risk`/`audit`，
    这两列它**不看**）：
      * `confirmation`：`pond.update` / `batch.update` / `purchase_order.update` /
        `access.user.*` / `access.role.permissions` 文档写 `never`，实际生效值是 `always`
        —— 读者会以为这些高危写操作**不需要人工确认**；
      * `agent_exposure`：`access.user.list` / `audit.log.list` 文档写 `hidden`，
        4 条 `access.*` 写 `human_only`，实测全部是 `exposed`。

    **必须用 `effective_confirmation`**：`confirmation` 字段未显式声明时是 `None`，
    生效值由 `risk` 派生（`kernel/capability.py:360`）。直接读 `capability.confirmation`
    会把 `None` 误判成"不匹配"。
    """
    load_all_status()
    from fpa.kernel.capability import REGISTRY

    runtime = {c.name: c for c in REGISTRY.all()}
    checked = 0
    problems: list[str] = []
    for name, cap in runtime.items():
        cells = _doc_table_cells(name)
        if cells is None:
            continue  # §1 未登记 —— 由 registry_reconcile 的 [1-R] 负责，不在本条范围
        checked += 1
        conf = cap.effective_confirmation
        conf_value = conf.value if hasattr(conf, "value") else str(conf)
        exposure = cap.agent_exposure
        exposure_value = (exposure.value if hasattr(exposure, "value") else str(exposure))

        doc_conf = cells[9].replace("*", "").strip()
        doc_exp = cells[10].strip()
        if doc_conf != conf_value:
            problems.append(f"{name}: 文档 confirmation={doc_conf!r} 运行时={conf_value!r}")
        if doc_exp != exposure_value:
            problems.append(f"{name}: 文档 agent_exposure={doc_exp!r} 运行时={exposure_value!r}")

    assert checked > 50, f"只核对到 {checked} 行 §1 表，解析器可能坏了"
    assert not problems, (
        "§1 能力表的 `confirmation` / `agent_exposure` 列与运行时不符"
        "（`registry_reconcile` 不看这两列，只有本条能拦）：\n  " + "\n  ".join(problems)
    )
