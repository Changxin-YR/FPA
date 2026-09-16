# -*- coding: utf-8 -*-
"""把"渔芯业务工具到底有没有挂进 Harness"和"能逃逸的内建工具有没有被关掉"变成
**一条可执行判据**。

## 这个文件为什么存在（t11 的根因）

`session.py` 构造 `DeepSeekHarness(...)` 时传的是 `patches=()`，而 dsh 的合成顺序是：

    bundle 层 → profile 自己的 `<DSH_HOME>/profiles/sdk/cordis.patch.yml`
              → 家目录层 `<DSH_HOME>/cordis.patch.yml`
              → `--patch` 覆盖层（最后生效）

本仓库的 profile patch 是**空数组**、家目录层不存在、`patches=()` 又不给覆盖层，
于是 `agent-runtime/cordis.patch.yml` 里的 1 条 `insert` 与 52 条 `disabled` **一条都没
生效**。后果有两面，而且两面都很安静：

* 模型手里没有任何 渔芯业务工具 —— 它于是**自己拼 HTTP** 打后端网关（真实自述：
  「本会话里我没有挂载 渔芯的业务工具，所以是直接用系统给的 X-Agent-Context 凭据打
  后端网关 `GET /api/v1/agent/tools` …」）。业务数据是真的，但路径绕过了设计；
* `tool-web`（能发任意 HTTP）、`tool-fs`（能读文件）、`tool-pwsh`（能执行命令）**全都开着**
  —— "禁止任意 Shell / 文件系统 / 网络 / 子代理"这条安全边界整条不在。

以前没有任何检查能发现这件事：应用启动正常、`pytest` 全绿、浏览器 e2e 也能跑通
（因为模型自己会把数据搞到手）。所以本工具**不看配置文件，只看真实合成出来的树**。

## 判据（每一项都能"红"）

1. `fpa-biz-tools` 行存在且 **enabled** —— 业务工具的唯一来源；
2. 它引用的包能解析，且该包的 `cordis.patch.yml` **真的含 insert（工具源）**；
3. 一组"必须被关掉"的行 id **一个都不能是 enabled**（web / 文件系统 / 命令 / 子代理 / 技能 / goal …）；
4. 合成树里**不允许再出现**任何 enabled 的工具提供者，除非它在白名单里
   （白名单 = `tools` 注册表服务本身 + `fpa-biz-tools`）。这条是"防止下一版 bundle
   悄悄挂上新的逃逸工具"的兜底 —— 前三条都过、这一条仍可能红；
5. `system-prompt` 行带 `includeHarnessIdentity: false` + 非空 `persona`
   —— 人格归渔芯，而不是 Harness 默认的编程助手。

上面 5 条读的是**配置层**。`--tools` 读的是**运行时终点**：直接从会话日志的
`request/header` 事件里取 `system` 与 `tools` —— 那是 agent-loop 在每次请求前写下的
快照，记录**模型实际收到的**系统提示词与工具清单。配置对了不等于模型拿到了，
中间还隔着 `apply()` 有没有跑、`ctx.tools.register` 有没有被调用、网关清单有没有拉下来。

## 用法

    python tools/harness_tools_audit.py
    python tools/harness_tools_audit.py --json

退出码：`0` 全部通过、`1` 有违规、`2` **没能证明**（合成树没跑出来 / 空树）。
`2` 不等于通过 —— 一个"恒返回 0"的实现也能通过"有问题时返回 1"的测试，
所以这里显式区分"没证明"与"没问题"。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import yaml  # noqa: E402


class _Loader(yaml.SafeLoader):
    """SafeLoader + Harness 的 `!!js` 表达式标签。

    `--dump-config` 会把**未求值**的 `!!js process.env.…` 原样打出来（求值发生在子进程
    启动时），标准 SafeLoader 不认识这个标签会直接抛 `ConstructorError`。这里把它当字符串
    收下即可 —— 本工具只关心"行 id 是否存在、是否 disabled"，不关心表达式的值。
    """


_Loader.add_constructor(
    "tag:yaml.org,2002:js", lambda loader, node: loader.construct_scalar(node)
)

#: 业务工具插件包名（与 patch 的 insert 段一致）。
BIZ_TOOLS_PACKAGE = "@fpa/dsh-biz-tools"

#: 运行时 patch（本仓库内的权威副本）。
RUNTIME_PATCH = ROOT / "agent-runtime" / "cordis.patch.yml"

#: 必须**不能**是 enabled 的行。分成几类只为让失败信息可读，判定逻辑完全相同。
ESCAPE_GROUPS: dict[str, tuple[str, ...]] = {
    "命令执行": ("tool-bash", "tool-pwsh", "tool-jobs", "bash-sandbox", "pwsh-sandbox"),
    "文件系统": ("tool-fs", "tool-fs-search", "tool-str-replace-editor", "fs-sandbox", "fs-local"),
    "网络": ("web", "web-search-deepseek", "web-fetch-http", "tool-web"),
    "子代理 / 工作流": (
        "subagent",
        "tool-subagent",
        "tool-subagent-control",
        "tool-subagent-list-agents",
        "tool-subagent-fork",
        "subagent-spawn-in-process",
        "subagent-fork-in-process",
        "workflow-worker-thread",
        "tool-workflow",
    ),
    "技能 / 计划 / 目标": (
        "skill",
        "tool-skill",
        "skill-filesystem",
        "skill-badge",
        "plan-mode",
        "goal",
        "goal-round-driver",
        "command-goal",
        "tool-goal",
        "tool-ralph",
    ),
    "其它能力面": ("subprocess", "sandbox", "permission", "commands", "tool-commands", "shell-env"),
}

#: 允许在合成树里 enabled 的**行**：`tools` 是工具注册表服务本身（不是工具提供者），
#: `fpa-biz-tools` 是我们的业务工具来源。其余 `tool-*` / `web*` / `subagent*` / `skill*`
#: 只要 enabled 就是一条逃逸面。
PROVIDER_ROW_PATTERNS = (
    re.compile(r"^tool-"),
    re.compile(r"^web$|^web-"),
    re.compile(r"^subagent"),
    re.compile(r"^skill"),
    re.compile(r"^terminal"),
    re.compile(r"^persistent-"),
    re.compile(r"^str-replace"),
    re.compile(r"^fs-local$"),
    re.compile(r"^attachment-local$"),
)
ALLOWED_PROVIDER_ROWS = {"tools", "fpa-biz-tools"}


class AuditOutcome:
    """收集违规；`proven=False` 表示"没证明"，与"通过"严格区分。"""

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.notes: list[str] = []
        self.proven = True
        self.rows: dict[str, dict] = {}

    def fail(self, message: str) -> None:
        self.violations.append(message)

    def unproven(self, message: str) -> None:
        self.proven = False
        self.violations.append(message)


def _parse_first_document(raw: str) -> list[dict] | None:
    """从 dsh 的输出里取出第一个 YAML 文档（前面的告警行会被跳过）。"""
    try:
        documents = [doc for doc in yaml.load_all(raw, Loader=_Loader) if doc]
    except yaml.YAMLError:
        return None
    for document in documents:
        if isinstance(document, list) and all(isinstance(item, dict) for item in document):
            return document
    return None


def compose_tree(patch: Path | None) -> tuple[list[dict] | None, str]:
    """跑一次 `dsh --profile <p> [--patch <patch>] --dump-config`，返回**合成**后的行。

    ★ 为什么必须取**第一个** YAML 文档：`--dump-config` 先打合成结果、再把每一层 patch
    单独列一遍（各自成为一个文档）。第一版这个函数把两个流拼起来再取文档，于是读到的是
    最后一层那 2 行 insert —— 表现是"树只有 1 行"，看不出任何东西，却**看起来**像在检查。
    判据依赖的读数必须来自合成结果，不能来自某一层。

    `patch=None` 时不给覆盖层，用作**反向对照**（见 `main`）：同一棵树、同一段代码，
    少一个 `--patch` 就必须读到"没有业务工具"。
    """
    from fpa.settings import Settings

    settings = Settings.from_env()
    dsh_bin = (settings.agent_dsh_bin or "").strip() or str(
        ROOT / "agent-runtime" / "bin" / "run.cmd"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
                           "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
                           "HOMEDRIVE", "HOMEPATH", "LANG", "TZ", "AGENT_HARNESS_ROOT",
                           "DSH_NODE_RUNTIME"}
    }
    env["DSH_HOME"] = settings.agent_dsh_home or str(ROOT / ".dsh-home")
    env["DSH_RUNTIME_MODE"] = "node"
    # patch 在配置求值阶段读这两个键；给占位值即可（本工具不发起任何调用）。
    env.setdefault("FPA_AGENT_GATEWAY_URL", "http://127.0.0.1:1/api/v1/agent")
    env.setdefault("FPA_AGENT_CONTEXT_TOKEN", "audit-placeholder")
    argv = [dsh_bin, "--profile", settings.agent_profile or "sdk"]
    if patch is not None:
        argv += ["--patch", str(patch)]
    argv.append("--dump-config")

    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(ROOT / "agent-runtime"),
        timeout=180,
    )
    raw = (completed.stdout or "") + "\n" + (completed.stderr or "")
    rows = _parse_first_document(completed.stdout or "")
    # 自证：拿不到行、或者行数少到不像一棵真树时，判为"没证明"。
    if rows is None:
        return None, raw[-2000:]
    if len(rows) < 20:
        return None, f"合成树只有 {len(rows)} 行，不像真实树\n" + raw[-1200:]
    return rows, raw[-2000:]


def _by_id(rows: list[dict]) -> dict[str, dict]:
    """按 id 建立索引。

    合成树里同一 id 可能出现在多层（bundle 一次、用户覆盖层一次）；dsh 的语义是
    **最后写入者生效**，所以取最后一个，而不是第一个。
    """
    by_id: dict[str, dict] = {}
    for row in rows:
        row_id = row.get("id")
        if isinstance(row_id, str):
            by_id[row_id] = row
    return by_id


def audit(rows: list[dict], outcome: AuditOutcome) -> None:
    by_id = _by_id(rows)
    outcome.rows = by_id

    # --- 1. 业务工具行 -------------------------------------------------------
    biz = by_id.get("fpa-biz-tools")
    if biz is None:
        outcome.fail(
            "合成树里没有 `fpa-biz-tools` 行：运行时 patch 没被加载 → 模型不会拿到任何 "
            "渔芯业务工具（它会自己手工拼 HTTP 绕过设计）"
        )
    else:
        if biz.get("disabled") is True:
            outcome.fail("`fpa-biz-tools` 行存在但被 disabled")
        if biz.get("name") != BIZ_TOOLS_PACKAGE:
            outcome.fail(f"`fpa-biz-tools` 行的 name 是 {biz.get('name')!r}，期望 {BIZ_TOOLS_PACKAGE!r}")
        config = biz.get("config") if isinstance(biz.get("config"), dict) else {}
        for key in ("gatewayUrl", "contextToken"):
            if key not in config:
                outcome.fail(f"`fpa-biz-tools` 的 config 缺 `{key}`（插件 apply() 会拒绝启动）")

    # --- 2. 包与它的 insert --------------------------------------------------
    packages = list(ROOT.glob(".dsh-home/profiles/*/node_modules/@fpa/dsh-biz-tools"))
    if not packages:
        outcome.fail(
            "`.dsh-home/profiles/*/node_modules/@fpa/dsh-biz-tools` 不存在："
            "合成树的 insert 解析不到包（插件加载会失败）"
        )
    else:
        for package in packages:
            manifest = package / "cordis.patch.yml"
            if not manifest.is_file():
                outcome.fail(f"{package} 缺 cordis.patch.yml")
                continue
            # 解析结构，不做文本匹配 —— 注释里提到某个键名不算"用了它"。
            entries = None
            try:
                from yaml import load as _yaml_load

                entries = _yaml_load(manifest.read_text(encoding="utf-8"), Loader=_Loader)
            except Exception as exc:  # noqa: BLE001 - 解析失败本身就是违规
                outcome.fail(f"{manifest} 解析失败：{type(exc).__name__}: {exc}")
            if not isinstance(entries, list):
                outcome.fail(f"{manifest} 不是顶层 YAML 数组")
                continue
            inserts = [e for e in entries if isinstance(e, dict) and "insert" in e]
            disables = [e for e in entries if isinstance(e, dict) and "disabled" in e]
            if not inserts:
                outcome.fail(
                    f"{manifest} 里没有 insert 段：这一份被当 bundle 装进来时不会注册任何工具"
                )
            if disables:
                outcome.fail(
                    f"{manifest} 里有 {len(disables)} 条 `disabled` 覆盖"
                    f"（如 {disables[0].get('id')!r}）：发布副本应当只负责「工具源」，"
                    "安全边界属于部署层的运行时 patch（两处描述同一件事会必然漂移）"
                )

    # --- 3. 逃逸行必须都关掉 -------------------------------------------------
    for group, ids in ESCAPE_GROUPS.items():
        for row_id in ids:
            row = by_id.get(row_id)
            if row is not None and row.get("disabled") is not True:
                outcome.fail(f"{group}：行 `{row_id}` 在合成树里是 **enabled**（逃逸面）")

    # --- 4. 兜底：不许有白名单之外的 enabled 工具提供者 -----------------------
    for row_id, row in sorted(by_id.items()):
        if row_id in ALLOWED_PROVIDER_ROWS:
            continue
        if row.get("disabled") is True:
            continue
        if any(pattern.match(row_id) for pattern in PROVIDER_ROW_PATTERNS):
            outcome.fail(
                f"合成树里出现未登记的 enabled 工具提供者 `{row_id}`"
                f"（name={row.get('name')!r}）：逃逸面清单需要复核，不要直接放过"
            )

    # --- 5. 人格 -------------------------------------------------------------
    prompt_row = by_id.get("system-prompt")
    if prompt_row is None:
        outcome.unproven("合成树里没有 `system-prompt` 行：人格判据无法证明")
    else:
        config = prompt_row.get("config") if isinstance(prompt_row.get("config"), dict) else {}
        persona = str(config.get("persona") or "")
        if not persona.strip():
            outcome.fail("`system-prompt` 的 persona 为空：助手会用 Harness 默认身份自我介绍")
        if config.get("includeHarnessIdentity") is not False:
            outcome.fail(
                "`system-prompt` 没有 `includeHarnessIdentity: false`："
                "Harness 那句 'You are an AI agent powered by DeepSeek Harness.' 仍在，"
                "模型会自称 Harness 助手"
            )
        if "渔芯AI水产养殖一体化系统" not in persona:
            outcome.fail("persona 文本里没有出现 渔芯AI水产养殖一体化系统：看起来不是部署方的人格")
        if "塘小助" not in persona:
            outcome.fail("persona 里没有「塘小助」：这不像本项目的部署人格")
        if "{{" in persona:
            outcome.fail(
                "persona 里出现 `{{`：Harness 会按提示词变量严格解析，未注册的变量会让**整轮**报错"
            )


def _control(tree: dict[str, dict]) -> str | None:
    """反向对照：合成树里到底有没有出现"业务工具"这行。

    用法（同一段检查代码、同一棵树、只改一个变量）：

      * 给了 `--patch` 的树 → `"present"`；
      * 没给 `--patch` 的树 → `"absent"`。

    只测"接上 patch 后返回 0"是假测试 —— 一个恒返回 0 的实现同样能过。有了这一对读数，
    "这行确实由那个 patch 带进来"才是被证明的，而不是被假设的。
    """
    if "fpa-biz-tools" not in tree:
        return "absent"
    return "present"


#: 明确属于"通用 / 逃逸面"的工具名。出现在模型清单里就是安全边界漏了。
_ESCAPE_TOOL_NAMES = frozenset(
    {
        "bash", "pwsh", "read", "write", "edit", "str_replace_editor", "glob", "grep",
        "web_search", "web_fetch", "subagent", "subagent_fork", "workflow", "ralph",
        "job_kill", "job_list", "job_output", "skill", "todo_write", "read_image",
        "create_goal", "get_goal", "update_goal", "exit_plan_mode", "list_agents",
        "send_message", "interrupt_agent",
    }
)


def _latest_session_files(limit: int = 1) -> list[Path]:
    """取最近的会话日志。用 `os.walk` 剪枝（本项目纪律：不用 `rglob`）。"""
    import os

    root = ROOT / ".dsh-home" / "sessions"
    found: list[tuple[float, Path]] = []
    for current, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith("session.jsonl.zstd"):
                path = Path(current) / name
                found.append((path.stat().st_mtime, path))
    found.sort(reverse=True)
    return [path for _mtime, path in found[:limit]]


def _read_session_events(path: Path) -> list[dict]:
    import json

    try:
        import zstandard as zstd
    except ImportError:
        return []
    raw = zstd.ZstdDecompressor().stream_reader(open(path, "rb")).read().decode("utf-8", "replace")
    events: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def tool_readout(limit: int = 3) -> tuple[list[dict], str]:
    """读最近几份会话日志，返回"模型实际拿到的工具与系统提示词"。"""
    report: list[dict] = []
    for path in _latest_session_files(limit):
        events = _read_session_events(path)
        header: dict = {}
        for event in events:
            if event.get("type") == "request/header":
                header = (event.get("data") or {}).get("header") or {}
                break
        tools: list[str] = []
        for tool in header.get("tools") or []:
            if isinstance(tool, dict):
                tools.append(str(tool.get("name") or "?"))
            else:
                tools.append(str(tool))
        calls = [
            str((event.get("data") or {}).get("name"))
            for event in events
            if event.get("type") == "tool/call"
        ]
        system = str(header.get("system") or "")
        report.append(
            {
                "session": path.parent.name,
                "system_is_fpa": "塘小助" in system,
                "harness_identity_present": "You are an AI agent powered by DeepSeek Harness" in system,
                "system_head": system[:120],
                "tool_count": len(tools),
                "tools": tools,
                "business_tools": [n for n in tools if not n.startswith(("ask_user",)) and n not in _ESCAPE_TOOL_NAMES],
                "escape_tools": [n for n in tools if n in _ESCAPE_TOOL_NAMES],
                "calls": calls,
            }
        )
    if not report:
        return [], "找不到任何会话日志（还没跑过一轮对话？）"
    return report, ""


def main(argv: list[str]) -> int:
    as_json = "--json" in argv

    if "--tools" in argv:
        report, error = tool_readout(limit=3)
        if error:
            print(f"UNPROVEN {error}")
            return 2
        if as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("=== 模型**实际**收到的工具清单（读会话日志 request/header）===")
            for item in report:
                print()
                print(f"会话 {item['session']}：工具 {item['tool_count']} 个")
                print(f"  系统提示词是 渔芯的（含「塘小助」）：{item['system_is_fpa']}")
                print(f"  仍含 Harness 身份句：{item['harness_identity_present']}")
                print(f"  逃逸类工具（应为空）：{item['escape_tools']}")
                print(f"  当前迭代实际调用：{item['calls'] or '（无）'}")
                print(f"  工具（前 12 个）：{item['business_tools'][:12]}")
        bad = [
            item
            for item in report
            if item["escape_tools"] or not item["system_is_fpa"] or not item["business_tools"]
        ]
        if bad:
            print()
            print(f"结果：{len(bad)} 份会话不合格（有逃逸工具 / 人格不对 / 没有业务工具）")
            return 1
        print()
        print("结果：模型实际拿到了 渔芯业务工具、且清单里没有任何逃逸类工具")
        return 0

    outcome = AuditOutcome()

    # patch 路径取自**生产代码**（`fpa.harness.session.resolve_harness_patch`），不在这里
    # 另写一遍 —— 否则本工具验的是自己那套推导，而不是 session 真正加载的那份。
    from fpa.harness.session import resolve_harness_patch
    from fpa.settings import Settings

    resolved = resolve_harness_patch(Settings.from_env())
    if not resolved:
        print("UNPROVEN resolve_harness_patch() 返回空：生产路径根本不会加载任何 patch")
        return 2
    patch = Path(resolved)
    if patch.resolve() != RUNTIME_PATCH.resolve():
        print(f"UNPROVEN 生产路径加载的不是本仓库的运行时 patch：{patch}")
        print(f"         （仓库内应为 {RUNTIME_PATCH}）")
        return 2

    rows, raw = compose_tree(patch)
    if rows is None:
        print("UNPROVEN 没能合成出真实配置树：")
        print(raw)
        return 2
    with_patch = _control(_by_id(rows))

    bare_rows, bare_raw = compose_tree(None)
    if bare_rows is None:
        print("UNPROVEN 反向对照（不给 patch）没能合成出配置树：")
        print(bare_raw)
        return 2
    without_patch = _control(_by_id(bare_rows))

    # ★ 判据有信号的**必要条件**：加不加这个 patch，那行必须从无到有。
    if not (with_patch == "present" and without_patch == "absent"):
        outcome.unproven(
            "反向对照失败：不给 --patch 时 `fpa-biz-tools` 是 "
            f"{without_patch!r}、给 --patch 时是 {with_patch!r}。"
            "两者应当分别是 'absent' / 'present'，否则说明这行不是这个 patch 带来的"
        )

    audit(rows, outcome)

    summary = {
        "patch": str(patch),
        "rows": len(rows),
        "control": {"with_patch": with_patch, "without_patch": without_patch},
        "fpa_biz_tools": "fpa-biz-tools" in outcome.rows,
        "enabled_escape_rows": [
            row_id
            for group in ESCAPE_GROUPS.values()
            for row_id in group
            if outcome.rows.get(row_id, {}).get("disabled") is not True and row_id in outcome.rows
        ],
        "violations": outcome.violations,
        "proven": outcome.proven,
        "passed": outcome.proven and not outcome.violations,
    }

    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("=== 渔芯 Harness 工具挂载 / 逃逸面审计 ===")
        print(f"运行时 patch：{summary['patch']}")
        print(
            "反向对照：不给 --patch -> 业务工具 "
            f"{without_patch}；给 --patch -> {with_patch}"
            "（必须 absent → present，判据才有信号）"
        )
        print(f"合成树规模：{summary['rows']} 行（自证：远大于 dsh-base 的最小树才算合成成功）")
        print(f"业务工具来源 `fpa-biz-tools`：{'在' if summary['fpa_biz_tools'] else '**不在**'}")
        print(f"合成树里 enabled 的逃逸行：{summary['enabled_escape_rows'] or '无'}")
        print()
        if outcome.violations:
            for violation in outcome.violations:
                print(f"  FAIL  {violation}")
        else:
            print("  PASS  1) 业务工具已挂载   2) 包只含工具源   3) 逃逸行全部关闭")
            print("  PASS  4) 无白名单外的工具提供者   5) 人格归 渔芯AI水产养殖一体化系统 且不使用提示词变量")

    if not outcome.proven:
        print("\n结果：UNPROVEN —— 判据没能执行完，**不等于通过**")
        return 2
    if outcome.violations:
        print(f"\n结果：{len(outcome.violations)} 项违规")
        return 1
    print("\n结果：全部通过 —— 业务工具已挂进真实 Harness 树，逃逸面已关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
