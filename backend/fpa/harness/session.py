"""DeepSeek Harness 会话管理。

早期版本的两个工程问题，本模块针对性解决：

**问题 1：一把全局锁罩住整个 `harness.run`。**
旧 `harness_sidecar.py` 用 `with self._lock:` 包住整轮对话，配合
`gunicorn --workers 2 --threads 2` 和 90 秒超时，**每个 worker 同时只能跑 1 个智能体
回合**。两个业务员同时用助手，第二个必然排队。
这里改成**按会话亲和 + 有界子进程池**：锁只保护池的记账（几微秒），不覆盖对话时长。

**问题 2：每轮丢弃子进程重建。**
早期实现因为"`context_token` 绑定了当前迭代提示词与请求、且 SDK 在启动时快照扩展配置"
而每轮 `_cache.drop(namespace)` —— 一轮对话的代价包含一次完整的运行时冷启动。
这里把**令牌从进程配置里搬出来，变成每次工具调用的参数**：

    每轮都变的（提示词、context_token）→ 工具调用参数
    长期不变的（gatewayUrl）            → 进程配置

于是子进程可以跨轮复用，冷启动从"每轮一次"降到"每会话一次"。

**环境隔离**：凭据只通过 SDK 的 `env` 参数传给子进程，**不写 `os.environ`**。
早期版本里有 `with _HARNESS_ENV_LOCK: previous = dict(os.environ); os.environ.clear()`
这样在进程全局状态下做快照/清空的代码——即使有锁，它也会让任何并发请求看到空的
`os.environ`。新实现不碰 `os.environ`。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from fpa.kernel.errors import DomainError, ErrorCode
from fpa.settings import Settings

logger = logging.getLogger(__name__)

#: 传给 Harness 子进程的环境变量白名单。
#:
#: **只放运行必需的**。凭据（`DEEPSEEK_API_KEY`）走 SDK 的 `api_key` 参数或
#: 子进程自己的凭据文件，不由本进程转发——本项目进程里根本没有那个值。
_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
        "TZ",
        "NODE_PATH",
        "DSH_RUNTIME_MODE",
        "HOMEDRIVE",
        "HOMEPATH",
        "PROGRAMDATA",
    }
)


#: 业务工具插件包名。运行时 patch 里的 `insert` 段引用它，本模块只用来做**自证**：
#: 合成出来的配置树里到底有没有这个工具来源。
BIZ_TOOLS_PACKAGE = "@fpa/dsh-biz-tools"

#: 仓库内运行时 patch 的相对路径（`agent-runtime/cordis.patch.yml`）。
_RUNTIME_PATCH_RELPATH = ("agent-runtime", "cordis.patch.yml")

#: 已安装副本的相对路径（`<DSH_HOME>/profiles/<profile>/node_modules/@fpa/dsh-biz-tools/`）。
_INSTALLED_PATCH_RELPATH = ("profiles", "sdk", "node_modules", "@fpa", "dsh-biz-tools", "cordis.patch.yml")
_MIN_DISABLED_PATCH_ROWS = 50


def _validate_runtime_patch(path: Path) -> None:
    """拒绝会让 Harness 退回默认工具集的 patch。"""
    try:
        import yaml
    except ImportError as exc:
        raise DomainError(
            ErrorCode.AGENT_UNAVAILABLE,
            "智能助手运行时缺少 YAML 校验依赖，请联系管理员",
        ) from exc

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_constructor(
        "tag:yaml.org,2002:js", lambda loader, node: loader.construct_scalar(node)
    )
    try:
        documents = [doc for doc in yaml.load_all(path.read_text(encoding="utf-8"), Loader=_Loader) if doc]
    except (OSError, yaml.YAMLError) as exc:
        raise DomainError(
            ErrorCode.AGENT_UNAVAILABLE,
            "智能助手运行时策略文件不可读或格式无效，请联系管理员",
        ) from exc
    entries = next(
        (doc for doc in documents if isinstance(doc, list) and all(isinstance(item, dict) for item in doc)),
        None,
    )
    if entries is None:
        raise DomainError(ErrorCode.AGENT_UNAVAILABLE, "智能助手运行时策略文件无效，请联系管理员")
    inserts = [
        item
        for item in entries
        if isinstance(item.get("insert"), list)
        and any(
            isinstance(insert, dict) and insert.get("name") == BIZ_TOOLS_PACKAGE
            for insert in item["insert"]
        )
    ]
    disabled = [item for item in entries if item.get("disabled") is True]
    has_persona = any(item.get("id") == "system-prompt" for item in entries)
    if not inserts or len(disabled) < _MIN_DISABLED_PATCH_ROWS or not has_persona:
        raise DomainError(
            ErrorCode.AGENT_UNAVAILABLE,
            "智能助手运行时安全策略不完整，拒绝启动",
        )


def _machine_env(name: str) -> str:
    """从 Windows 注册表读**机器级/用户级**环境变量（进程可能看不到后设置的那些）。

    为什么需要它（与 `tools/harness_smoke.py::_machine_env` 同一实现、同一理由）：
    `DEEPSEEK_API_KEY` 是机器级变量，而**服务进程可能在它被设置之前就启动了** ——
    于是 `os.environ` 里没有它。只信 `os.environ` 会得到
    「llm-deepseek: no API key for provider route」这种"配置明明有、子进程就是读不到"的失败。
    非 Windows 直接返回空串：L1 凭据走别的路径（`~/.dsh/.credentials.yaml`）。
    """
    if os.name != "nt":
        return ""
    import winreg

    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            r"Environment",
        ):
            try:
                with winreg.OpenKey(root, sub) as handle:
                    value, _ = winreg.QueryValueEx(handle, name)
                if value:
                    return str(value)
            except OSError:
                continue
    return ""


def _resolve_api_key() -> str:
    """凭据：优先进程环境，其次注册表。**只回传值，不打印、不落日志、不写文件。**"""
    return os.environ.get("DEEPSEEK_API_KEY", "") or _machine_env("DEEPSEEK_API_KEY")


def resolve_harness_patch(settings: Settings) -> str:
    """解析要传给 Harness 子进程的运行时 patch 文件。

    顺序（**从显式到推导，都能给出依据**）：

    1. `settings.agent_harness_patch`（env `AGENT_HARNESS_PATCH`）—— 部署方显式指定，
       打包/镜像布局只有它能表达；
    2. 仓库内的 `agent-runtime/cordis.patch.yml` —— 开发形态（本仓库就是"源码检出 + Node
       载体"，与 `dsh_bin` 的推导同源：见 `_create_session` 里 `bin/run.cmd` 那一段）；
    3. `<DSH_HOME>/profiles/<profile>/node_modules/@fpa/dsh-biz-tools/cordis.patch.yml`
       —— 部署形态：插件包已随 profile 安装。这一份**只含 insert 工具源**，安全边界（禁用
       内建工具）与部署人格仍在第 2 步那份里；走到这一步会打一条 warning，因为它意味着
       仓库检出没找到。

    找不到任何一份时返回空串并**吵一声**：那会让子进程退回 Harness 出厂形态 ——
    没有业务工具、却留着能读文件/能发任意 HTTP 的工具（模型于是会自己去手工拼 HTTP），
    而应用启动、单元测试、其它 e2e 全都是绿的。这正是本项目的主导航失败模式。
    """
    configured = (settings.agent_harness_patch or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            resolved = candidate.resolve()
            _validate_runtime_patch(resolved)
            return str(resolved)
        raise DomainError(
            ErrorCode.AGENT_UNAVAILABLE,
            "AGENT_HARNESS_PATCH 指向的文件不存在，请联系管理员",
        )

    root = Path(__file__).resolve().parents[3]
    repo_copy = root.joinpath(*_RUNTIME_PATCH_RELPATH)
    if repo_copy.is_file():
        resolved = repo_copy.resolve()
        _validate_runtime_patch(resolved)
        return str(resolved)

    home = (settings.agent_dsh_home or "").strip()
    if home:
        installed = Path(home).expanduser().joinpath(*_INSTALLED_PATCH_RELPATH)
        if installed.is_file():
            _validate_runtime_patch(installed.resolve())
            return str(installed.resolve())

    raise DomainError(
        ErrorCode.AGENT_UNAVAILABLE,
        "找不到完整的 Harness 运行时安全策略，拒绝启动智能助手",
    )


@dataclass
class HarnessTurn:
    """一轮对话的结果。"""

    reply: str
    session_id: str
    events: list[Any] = field(default_factory=list)
    #: 当前迭代的可观测健康度（见 `harness_health`）：重试次数、每次重试的原始码与退避、
    #: 以及重试耗尽后的终端失败。**一次成功的轮次也会有这个字段**，只是 retry_count=0。
    health: dict[str, Any] = field(default_factory=dict)
    #: 当前迭代**真正写入**库的工具调用，按发生顺序（见 `executed_tool_calls`）。
    #:
    #: 它是"列表该不该自动刷新"的服务端判据：前端不猜、也不"每轮都刷"，
    #: 只看这个列表非不非空（以及写的是不是当前打开的那个资源）。
    executed: list["ExecutedToolCall"] = field(default_factory=list)
    #: 当前迭代**只签发了卡片、什么都没写**的调用，按发生顺序（见 `pending_confirmations`）。
    #:
    #: 浏览器用它渲染确认卡片。它与 `executed` 互斥（同一张 `tool/result` 只有一个
    #: kind），所以"这一轮是写了还是等确认"在类型上就不可能含糊。
    pending: list["PendingConfirmation"] = field(default_factory=list)
    #: 当前迭代 `ask_user` 提出的那个问题（见 `clarification_question`），没提问就是 None。
    #:
    #: 它与上面两项**互斥**：插件在提问结果里带了 `CLARIFICATION_NOTE`，要求模型
    #: 立即停止当前迭代输出。
    clarification: dict[str, Any] | None = None


#: Harness 在"一次请求失败、准备重试"时写入的会话事件（`dsh-llm-retry`）。
RETRY_EVENT = "llm/retry"
#: 重试真正开始前写入的事件（退避等待结束）。
RETRY_STARTED_EVENT = "llm/retry-started"
#: 一轮对话的结束事件；`data.reason.kind == "error"` 时带**终端失败码**。
TURN_END_EVENT = "turn/end"
#: 模型发起一次工具调用。
TOOL_CALL_EVENT = "tool/call"
#: 工具调用的返回。内容里是**网关原样透传**的判别联合（`docs/WRITE_CONTRACT.md`）。
TOOL_RESULT_EVENT = "tool/result"


def _retry_delay_seconds(value: Any) -> float | None:
    """把 `delayMs` 转成秒并保留一位小数；取不到就返回 None（不编造 0）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value) / 1000.0, 1)


def harness_health(events: list[Any]) -> dict[str, Any]:
    """把一轮对话的事件流投影成**可观测**的健康度读数。

    为什么必须做这件事（而不是"让 Harness 自己重试就行"）：

    * 上游 `api.deepseek.com` 的**间歇性** `TRANSPORT` 失败已经被 `dsh-llm-retry` 按
      提供商策略自动重试（实测日志：同一轮里 retry 1→5、退避 0.5s→10s 递增）。但**重试
      本身只落进会话日志**：`session.py` 只读 `final_response`，于是"这一轮是第几次才成的"
      完全不可见 —— 把不稳定链路读成稳定链路，正是"用重试掩盖间歇性故障"。
    * 重试**耗尽**时 `session.run()` **不抛异常**，只是没有 assistant 消息；若只报一句
      通用文案，`TRANSPORT` 这个原始码就永远不会出现在任何地方。

    因此这里产出三样东西，全部来自子进程的真实事件、不做任何加工：

        retry_count         当前迭代的自动重试次数
        retries[]           每次重试的**第几次 / 上限 / 原始 code 与 message / 退避秒数**
        terminal_failure    重试耗尽后的终端失败（`{"code","message"}`）或 None

    `retries` 为空 + `terminal_failure` 为 None 才是"一次成功"。
    """
    retries: list[dict[str, Any]] = []
    terminal: dict[str, Any] | None = None
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if kind == RETRY_EVENT:
            failure = data.get("failure") if isinstance(data.get("failure"), dict) else {}
            record: dict[str, Any] = {
                "attempt": data.get("retry"),
                "max_retries": data.get("maxRetries"),
                "provider": data.get("provider"),
                "mode": data.get("mode"),
                "code": failure.get("code"),
                "message": failure.get("message"),
                "backoff_seconds": _retry_delay_seconds(data.get("delayMs")),
            }
            retries.append(record)
        elif kind == TURN_END_EVENT:
            reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
            if reason.get("kind") == "error":
                error = reason.get("error") if isinstance(reason.get("error"), dict) else {}
                terminal = {
                    "code": error.get("code"),
                    "message": error.get("message"),
                }
    return {
        "retry_count": len(retries),
        "retries": retries,
        "terminal_failure": terminal,
    }


@dataclass(frozen=True, slots=True)
class ExecutedToolCall:
    """当前迭代里**确实写进了库**的一次工具调用。

    判据是服务端自己的：网关按 `docs/WRITE_CONTRACT.md` 返回的判别联合里
    `kind == "executed"` —— 那是"已提交事务"的唯一标记（`executed` 只能由一次
    已提交的事务产生）。`read` / `clarification` / `confirmation_required` /
    `failed` 都不是写入，**一个都不算**。

    `tool_name` 是 Agent 侧的扁平工具名（`pond_create`）。之所以记工具名而不是
    直接记能力名：工具名是**事件里真实出现的东西**，能力名要靠 `Registry` 反查
    （`Capability.tool_name()` 是那个映射的唯一实现处，见 `agent_turn.py`）。
    """

    tool_name: str
    resource_id: int | None


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    """当前迭代里**只签发了卡片、什么都没写**的一次工具调用。

    判据与 `ExecutedToolCall` 同源：服务端返回的判别联合里
    `kind == "confirmation_required"`（`docs/WRITE_CONTRACT.md` 规则 1）。
    它与 `executed` 在类型上互斥 —— 同一张 `tool/result` 不可能既"写了"又"等确认"。

    `confirmation` 是网关签发的卡片原文（**含一次性令牌**）：`title` / `target` /
    `rows` / `impact` / `expires_at` 全部由服务端渲染，浏览器只负责显示。
    """

    tool_name: str
    message: str
    confirmation: dict[str, Any]


def executed_tool_calls(events: list[Any]) -> list[ExecutedToolCall]:
    """从一轮对话的事件流里挑出**真正写入**的工具调用，按发生顺序。

    判据是服务端自己的：网关按 `docs/WRITE_CONTRACT.md` 返回的判别联合里
    `kind == "executed"` —— 那是"已提交事务"的唯一标记（`executed` 只能由一次
    已提交的事务产生）。`read` / `clarification` / `confirmation_required` /
    `failed` 都不是写入，**一个都不算**。

    `tool_name` 是 Agent 侧的扁平工具名（`pond_create`）。之所以记工具名而不是
    直接记能力名：工具名是**事件里真实出现的东西**，能力名要靠 `Registry` 反查
    （`Capability.tool_name()` 是那个映射的唯一实现处，见 `agent_turn.py`）。

    配对与容错解析收在 `_tool_results()` 一处（`pending_confirmations()` 用的是
    同一份）：解析不出来的一律**跳过**而不是抛异常 —— 判据宁可漏报（前端少刷一次），
    也不能因为一段畸形文本把**整个对话轮**变成错误。
    """
    executed: list[ExecutedToolCall] = []
    for tool_name, payload in _tool_results(events):
        if payload.get("kind") != "executed":
            continue
        resource_id = payload.get("resource_id")
        executed.append(
            ExecutedToolCall(
                tool_name=tool_name,
                resource_id=resource_id
                if isinstance(resource_id, int) and not isinstance(resource_id, bool)
                else None,
            )
        )

    # 去掉拿不到工具名的那几条：说不清是哪个能力写的，前端也无从判断该刷哪个页。
    return [item for item in executed if item.tool_name]


def _tool_results(events: list[Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """把工具调用与结果**配对**后依次吐出 `(工具名, 网关原样返回的 payload)`。

    `tool/result` 事件的内容是模型看到的那段 JSON 文本，**里面没有工具名** ——
    它只有一个 `toolCallId`。工具名在同一步的 `tool/call` 事件里。两者必须配对
    才能回答"是哪个能力写的/等的"，而"该刷哪个页""卡片是哪张"都需要后者。
    """
    tool_names_by_call: dict[str, str] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            continue

        if kind == TOOL_CALL_EVENT:
            call_id = data.get("callId")
            name = data.get("name")
            if isinstance(call_id, str) and isinstance(name, str) and name:
                tool_names_by_call[call_id] = name
            continue

        if kind != TOOL_RESULT_EVENT:
            continue

        payload = _tool_result_payload(data)
        if payload is None:
            continue
        call_id = payload.get("_call_id")
        yield tool_names_by_call.get(call_id if isinstance(call_id, str) else "", ""), payload


def pending_confirmations(events: list[Any]) -> list[PendingConfirmation]:
    """从一轮对话的事件流里挑出**等用户确认**的工具调用，按发生顺序。

    ## 这条投影为什么必须存在（实测报障）

    网关返回的 `confirmation_required` 原本**只**回到模型（插件把它转交给 LLM），
    浏览器那一侧什么都没收到。后果是一条完整的静默失败链：

    * 模型按插件给的 `note` 在界面上说"已在界面上生成待确认卡片，请点击确认"；
    * 而**卡片从来没有出现过**（前端 `AgentPanel` 的确认卡片分支一次都没被触发）；
    * 用户于是回一句"执行"，模型答"我这边还没有收到确认结果，请在卡片上确认" ——
      两边都对，中间的交付环节不存在。

    契约 §3 ③ 早就定义了 `confirmation_required` 结果，缺的就是这一段提取；
    可复现的证据在 `.dsh-home/sessions/**/8a148ce4220e04e8`（一句话归档 3 个草稿区域
    → 3 张卡，界面上 0 张）。

    同一轮里模型可能对多个对象各签一张卡，所以返回**列表**；重复的卡片按 id 去重
    （同一张卡出现两次只说明事件流里重复了一次，不该让用户看到两张一样的卡）。
    """
    pending: list[PendingConfirmation] = []
    seen: set[int] = set()
    for tool_name, payload in _tool_results(events):
        if payload.get("kind") != "confirmation_required":
            continue
        confirmation = payload.get("confirmation")
        if not isinstance(confirmation, dict):
            # 没有卡片就没有可确认的东西。凭空造一张只会让用户点到一个不存在的令牌。
            continue
        card_id = confirmation.get("id")
        if isinstance(card_id, int) and not isinstance(card_id, bool):
            if card_id in seen:
                continue
            seen.add(card_id)
        pending.append(
            PendingConfirmation(
                tool_name=tool_name,
                message=str(payload.get("message") or ""),
                confirmation=dict(confirmation),
            )
        )
    return pending


def clarification_question(events: list[Any]) -> dict[str, Any] | None:
    """从事件流里挑出 `ask_user` 的那次提问（`kind == "clarification"`）；没提问就是 None。

    与 `pending_confirmations()` 是**同一个交付缺口**：插件把问题交给模型之后就结束当前迭代，
    浏览器那一侧什么都没收到 —— 前端那张澄清卡片（问题 + 可点选项 + 自由作答框）
    因此在生产路径上一次都没被触发过。这里把问题**原文**取出来交给浏览器。

    取**第一条**：插件的 `CLARIFICATION_NOTE` 要求模型提问后立即停止当前迭代输出，
    所以一轮里出现多条提问本身说明模型没守约；此时按用户会先看到的那一条渲染更稳。

    返回的是**重排过的干净对象**（只留 `question` / `options` / `allow_free_text`）：
    插件放在同一个 payload 里的 `note` 是给模型的控制指令，不是给用户看的文案，
    原样透传会让界面出现"请立即停止当前迭代输出"这种系统话术。
    """
    for _tool_name, payload in _tool_results(events):
        if payload.get("kind") != "clarification":
            continue
        options = payload.get("options")
        return {
            "question": str(payload.get("question") or ""),
            "options": [str(item) for item in options] if isinstance(options, list) else [],
            "allow_free_text": payload.get("allow_free_text") is not False,
        }
    return None


def _tool_result_payload(data: dict[str, Any]) -> dict[str, Any] | None:
    """从 `tool/result` 的嵌套结构里取出插件回给模型的那个 JSON 对象。

    结构（实测形状，见 `.dsh-home/sessions/**`）：::

        data.message.content[0]
            .content[0].text = '{"kind":"executed","message":"…","resource_id":999237,…}'

    取到之后**补一个 `_call_id`**，让调用方能把工具名配回来。
    """
    message = data.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        inner = block.get("content")
        if not isinstance(inner, list):
            continue
        for part in inner:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            parsed = _load_json_object(part.get("text"))
            if parsed is None:
                continue
            call_id = block.get("toolCallId")
            if isinstance(call_id, str):
                parsed["_call_id"] = call_id
            return parsed
    return None


def _load_json_object(raw: Any) -> dict[str, Any] | None:
    """把一段可能是 JSON 字符串的文本解析成 dict；不是就返回 None（不抛）。"""
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


_SESSION_LOG_BROKEN_MARKERS = (
    "must have tool source",
    "session event at seq",
)


def _is_session_log_broken(detail: str) -> bool:
    """事件日志是否已损坏到**无法回放**。

    实测 2026-09-15：模型一轮里并发发了 6 个工具调用，只有第 1 个拿到 callId，
    其余是空串；harness 校验 `source.callId !== ""` 后抛
    「session event at seq 54 message must have tool source」。
    这份坏日志会被**每一轮**回放，于是该会话之后每一次提问都失败 ——
    用户看到的表现就是“助手卡了很久、再也不回话”。
    """
    text = str(detail or "")
    return any(marker in text for marker in _SESSION_LOG_BROKEN_MARKERS)


class HarnessSessionManager:
    """按会话持有 Harness 实例。

    与早期实现的关键差别：**不做"每轮重建"**。会话 id 跨轮稳定，令牌按调用传参。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._sessions: dict[tuple[int, str, str], Any] = {}
        self._context_tokens: dict[tuple[int, str, str], tuple[str, float]] = {}
        #: 记录每个会话最近一次使用，供淘汰策略用。
        self._touched: dict[tuple[int, str, str], float] = {}

    # -- 生命周期 -------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Harness 是否可用。

        判据是"配置齐备"，不是"能连上"——真正的连接错误在 `run()` 里翻译成
        `AGENT_UNAVAILABLE`。这里只做快速判断，避免每个请求都去探测。
        """
        return bool(self.settings.agent_dsh_home)

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._context_tokens.clear()
            self._touched.clear()
        for session in sessions:
            closer = getattr(session, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001 - 关闭失败不该阻塞进程退出
                    logger.warning("关闭 Harness 会话失败", exc_info=True)

    def drop(self, session_key: tuple[int, str, str]) -> None:
        """丢弃一个会话（用于错误恢复：卡住或协议异常的子进程不该复用）。"""
        with self._lock:
            session = self._sessions.pop(session_key, None)
            self._context_tokens.pop(session_key, None)
            self._touched.pop(session_key, None)
        closer = getattr(session, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass

    def _quarantine_session_log(self, session_id: str) -> None:
        """把这个会话的**磁盘事件日志**挪走，让下一轮从干净会话开始。

        为什么必须动磁盘：harness 的事件日志是**追加在文件里**的
        （``<DSH_HOME>/sessions/<cwd>/<session_id>/session.jsonl.zstd``），
        子进程重建后会**重新加载它**。所以只 `drop()` 子进程没用 ——
        同一个 conversation_id 下一轮照样读到那份坏日志。
        """
        home = str(getattr(self.settings, "agent_dsh_home", "") or "")
        if not home or not session_id:
            return
        import glob
        import os
        import shutil
        import time

        for path in glob.glob(os.path.join(home, "sessions", "*", session_id)):
            if not os.path.isdir(path):
                continue
            target = "%s.broken-%d" % (path, int(time.time()))
            try:
                shutil.move(path, target)
                logger.warning("会话日志已隔离（损坏，无法回放）：%s -> %s", path, target)
            except OSError:  # pragma: no cover
                logger.warning("隔离会话日志失败：%s", path, exc_info=True)

    # -- 运行 -----------------------------------------------------------------

    def run(
        self,
        prompt: str,
        *,
        session_id: str,
        user_id: int,
        session_hash: str,
        gateway_url: str,
        context_token: str,
        on_notification: Any = None,
    ) -> HarnessTurn:
        """跑一轮对话。

        `context_token` 只用于当前迭代——它作为**工具调用参数**传给插件，不进进程配置。
        这是"子进程可跨轮复用"的前提。

        ## `on_notification` 是必需的，不是可选的锦上添花（502 的根因）

        子进程事件流默认被 SDK 收进 `RunResult.notifications`，**整轮结束才返回**。
        实测后果：一句需要工具的指令（"现在有几个塘口？"）要跑 **5 分钟以上**，
        这段时间 HTTP 响应**一个字节都不发** —— Vite 的 `http-proxy` 默认 **120 秒**空闲
        就掐掉连接，浏览器于是收到 `ConnectionResetError` / 502，
        **而库里其实已经写成功了**（审计里有那几行）。这正是 负责人 实测到的
        "第 1 次连接被重置、第 2 次协议错"。

        接上回调后，NDJSON 路由可以边收事件边写响应，连接不再空闲 ——
        这是长任务该有的形态，而且 `AgentPanel` **本来就是按流式写的**
        （它先试 `/agent/turns/stream`，只有 404/405 才回退非流式）。
        """
        if not self.available:
            raise DomainError(
                ErrorCode.AGENT_UNAVAILABLE,
                "智能助手尚未配置（缺少 AGENT_DSH_HOME），请联系管理员",
            )

        session_key = (int(user_id), str(session_hash), session_id)
        session = self._get_or_create(
            session_key, gateway_url=gateway_url, context_token=context_token
        )
        try:
            result = session.run(
                self._render_prompt(prompt),
                session_id=session_id,
                **({"on_notification": on_notification} if on_notification else {}),
            )
        except TimeoutError as exc:
            raise DomainError(
                ErrorCode.AGENT_TIMEOUT, "智能助手响应超时，请把问题拆小一点后重试"
            ) from exc
        except DomainError:
            raise
        except Exception as exc:  # noqa: BLE001
            # 坏掉的子进程不复用——下一轮会新建。这个判断是有意的：
            # 一个卡住的运行时如果被复用，会让**后续每一轮**都失败，
            # 表现为"助手彻底坏了"，而实际只坏了一个子进程。
            self.drop(session_key)
            logger.warning("Harness 轮次失败，已丢弃会话 %s", session_key, exc_info=True)
            raise self._translate(exc) from exc

        # ★ 当前迭代健康度：从子进程事件流里投影出来（重试次数 / 原始失败码 / 退避 / 终端失败）。
        #
        # 为什么在这里而不是等 Harness 报错：`dsh-llm-retry` 的自动重试**只落进会话日志**，
        # 重试到第 5 次才成的轮次与一次就成的轮次在这里长得一模一样。不投影出来，
        # "链路不稳定"就会被读成"链路稳定" —— 那正是加不可观测重试的害处。
        events = list(getattr(result, "events", []) or [])
        health = harness_health(events)

        reply = str(getattr(result, "final_response", "") or "")
        # ★ 空回复 + `finish_reason=error` **必须吵**，不能当成"模型没话说"。
        #
        # 实测过：`DEEPSEEK_API_KEY` 没被放进子进程环境时，`session.run()` **不抛异常**，
        # 而是返回 `final_response=''` + `finish_reason='error'`，事件里才有
        # 「llm-deepseek: no API key for provider route」。若这里直接返回空串，
        # 用户看到的是"打开面板、发了消息、什么都没有" —— 而全部测试仍然绿。
        # 这正是本项目的主导航失败模式（错误被降级成静默的空结果）。
        finish_reason = str(getattr(result, "finish_reason", "") or "")
        if not reply.strip():
            detail = ""
            for event in reversed(events):
                text = str(event)
                if "error" in text.lower() or "no api key" in text.lower():
                    detail = text[:500]
                    break
            # ★ 终端失败码必须**原样带出来**（`TRANSPORT` / `AUTH` / `RATE_LIMIT` / …）。
            #
            # 实测：上游间歇性 `TRANSPORT` 会先被自动重试 5 次、退避到 8 秒，耗尽后
            # `session.run()` **不抛异常**，只是 `final_response=""` + `finish_reason=error`。
            # 若这里只报一句"没有返回内容"，`TRANSPORT` 与那 5 次重试就全丢了 ——
            # 运维看到的是"智能助手偶尔不回话"，而不是"到 api.deepseek.com 的链路在抖"。
            terminal = (health.get("terminal_failure") or {}) if isinstance(health, dict) else {}
            logger.warning(
                "Harness 返回空回复（finish_reason=%s，terminal_code=%s，retry_count=%s）：%s",
                finish_reason,
                terminal.get("code") or "-",
                (health or {}).get("retry_count", 0),
                detail or "（事件里没有错误信息）",
            )
            # ★ 终端失败也要丢弃会话；若坏的是**磁盘日志**，必须连日志一起隔离。
            #
            # 只 `drop()` 不够：日志追加在文件里，新子进程会重新加载它，
            # 于是同一个 conversation_id 的**每一轮**都会在回放时失败。
            self.drop(session_key)
            if _is_session_log_broken(detail):
                self._quarantine_session_log(session_id)

            raise DomainError(
                ErrorCode.AGENT_UNAVAILABLE,
                "智能助手没有返回内容，请联系管理员检查助手配置"
                + (f"（finish_reason={finish_reason}）" if finish_reason else ""),
                data={
                    "finish_reason": finish_reason,
                    "detail": detail,
                    # 可观测重试的**唯一交付面**：第几次、原始码与 message、退避秒数、
                    # 以及重试耗尽后的终端失败。前端只读 `kind` 与 `message`，
                    # 这些键是给日志/运维/演示看的，不参与渲染。
                    "health": health,
                },
            )

        # 稳定链路 = retry_count 0。有重试就留下一条**带次数**的 warning：
        # "这一轮是第几次才成的"必须能从日志里读出来。
        if health.get("retry_count"):
            logger.warning(
                "Harness 回合用了自动重试：retry_count=%s，codes=%s，total_backoff=%.1fs",
                health["retry_count"],
                sorted({str(item.get("code")) for item in health.get("retries", [])}),
                sum(float(item.get("backoff_seconds") or 0) for item in health.get("retries", [])),
            )
        return HarnessTurn(
            reply=reply,
            session_id=str(getattr(result, "session_id", session_id)),
            events=events,
            health=health,
            executed=executed_tool_calls(events),
            pending=pending_confirmations(events),
            clarification=clarification_question(events),
        )

    # -- 内部 -----------------------------------------------------------------

    def _get_or_create(
        self,
        session_key: tuple[int, str, str],
        *,
        gateway_url: str,
        context_token: str,
    ) -> Any:
        import time

        with self._lock:
            existing = self._sessions.get(session_key)
            token_info = self._context_tokens.get(session_key)
            if existing is not None and token_info is not None and time.time() < token_info[1] - 5:
                self._touched[session_key] = time.monotonic()
                return existing
            if existing is not None:
                self._sessions.pop(session_key, None)
                self._context_tokens.pop(session_key, None)
                self._touched.pop(session_key, None)
                closer = getattr(existing, "close", None)
                if callable(closer):
                    closer()

        # 在锁外创建：冷启动可能耗时数秒，持锁创建会让**所有**会话排队——
        # 那正是早期版本全局锁的问题在更小尺度上的重演。
        created = self._create_session(gateway_url=gateway_url, context_token=context_token)

        with self._lock:
            # 双检：并发首轮请求可能同时创建，保留先放进来的那个。
            existing = self._sessions.get(session_key)
            if existing is not None:
                closer = getattr(created, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001
                        pass
                self._touched[session_key] = time.monotonic()
                return existing
            self._sessions[session_key] = created
            self._context_tokens[session_key] = (
                context_token,
                time.time() + self.settings.agent_context_ttl_seconds,
            )
            self._touched[session_key] = time.monotonic()
            return created

    def _create_session(self, *, gateway_url: str, context_token: str) -> Any:
        from deepseek_harness import DeepSeekHarness

        settings = self.settings
        env = {key: value for key, value in _current_env().items() if key in _ENV_ALLOWLIST}
        settings_home = settings.agent_dsh_home
        env["DSH_HOME"] = settings_home

        # ★ 插件配置：把网关地址与上下文令牌注入**子进程环境**，让插件自带的
        #   `cordis.patch.yml` 里那两行
        #       gatewayUrl:   !!js process.env.FPA_AGENT_GATEWAY_URL ?? ''
        #       contextToken: !!js process.env.FPA_AGENT_CONTEXT_TOKEN ?? ''
        #   取到值（patch 在配置求值阶段读 env）。
        #
        # 为什么两个键走 env 而不是写进 patch 文件本身：`patches` 收的是"额外 patch **文件**
        # 的路径"，能改的是"加载哪份策略"；而**每次会话都可能不同的赋值**（网关地址按端口
        # 推导、令牌按 (uid,sid,cid) 签发）只能走值注入。env 是唯一"不改策略文件、也不新增
        # 部署工件"的值注入点 —— 策略文件里那两行 `!!js process.env.…` 正是为此留的接口。
        #
        # 这两个键刻意**不在** `_ENV_ALLOWLIST` 里：它们由本函数逐次显式放入，
        # 而不是从父进程环境整体继承 —— 父进程环境里可能残留旧值，而插件自己的
        # `scrubCredentialsFromEnv` 正是为"外部已把令牌放进 env"这个泄漏面写的。
        #
        # `gateway_url` 以前是"收了参数却不用"，于是插件 `apply()` 在
        # `resolveGatewayBase('')` 处抛配置错误 —— 现象是"助手起不来"。（那次的另一个
        # 症状是 `patches=()` 让插件压根没被加载，见本方法末尾 `patches=patches` 的说明。）
        if gateway_url:
            env["FPA_AGENT_GATEWAY_URL"] = gateway_url
        if context_token:
            env["FPA_AGENT_CONTEXT_TOKEN"] = context_token

        # ★ 开发载体与凭据：**显式写进去，不依赖从父进程环境继承**。
        #
        # 两个都实测踩过，症状都是"配置明明有、子进程就是读不到"：
        #   1. `DSH_RUNTIME_MODE=node` —— 不放行就去要那个不存在的生产 exe，
        #      报 `FileNotFoundError: deepseek-harness-runtime-bin is missing the runtime executable`；
        #   2. `DEEPSEEK_API_KEY` —— 不放行会让模型侧报
        #      「llm-deepseek: no API key for provider route "deepseek-official"」，
        #      而 `run()` 返回的是**空回复 + finish_reason=error**（不是异常！），
        #      于是上层会把它当成一次成功的空回答。
        # 这台机器的 key 是**机器级**变量，而服务进程可能在它设置之前就启动，
        # 所以从注册表兜一次（见 `_machine_env`）。
        #
        # 教训（与 `tools/harness_smoke.py` 的注释同源）：**白名单式的环境构造里，
        # "自然继承"是一个不存在的概念** —— 凡是子进程必需的，就在这里显式写上。
        env["DSH_RUNTIME_MODE"] = env.get("DSH_RUNTIME_MODE") or settings.runtime_mode or "node"
        if settings.agent_harness_root:
            env["AGENT_HARNESS_ROOT"] = str(Path(settings.agent_harness_root).expanduser().resolve())
        runtime_root = Path(settings.agent_harness_root).expanduser() / "python" / "sdk-runtime" / "src" / "deepseek_harness_runtime" / "runtime" / "node"
        if runtime_root.is_dir():
            env["DSH_NODE_RUNTIME"] = str(runtime_root)
        api_key = _resolve_api_key()
        if api_key:
            env["DEEPSEEK_API_KEY"] = api_key

        # ★ 载体选择：**用 `dsh_bin` 显式指定，不靠 `DSH_RUNTIME_MODE`**（实测踩到）
        #
        # 为什么 env 里那个变量不够：SDK 的 `HarnessClient._default_launch_args()` 调的是
        # `resolve_bundled_launch_args()`（**不带参数**），而它读的是 **`os.environ`** ——
        # 也就是**后端进程自己的**环境，**不是**我们传给 `DeepSeekHarness(env=…)` 的那份。
        # 所以只往子进程 env 里放 `DSH_RUNTIME_MODE=node` **完全无效**，仍抛
        # `FileNotFoundError: …runtime executable`（我在 t9 上实测过这一条，
        # 而且它的表象是 502 / `AGENT_PROTOCOL_ERROR`，不是这句 FileNotFoundError）。
        #
        # 正解是 `HarnessConfig.dsh_bin`：给了它，SDK 直接拿它当启动命令、
        # **根本不碰** `resolve_bundled_launch_args()`。这正是
        # `tools/harness_smoke.py` 的做法（它的注释还写明：必须指向**可执行文件/包装器**，
        # 不能是 `.js`；本机那个 `dsh.exe` 会访问违例，所以用 `bin/run.cmd`）。
        #
        # `settings.agent_dsh_bin`（env `AGENT_DSH_BIN`）为空时**回退到仓库里的包装器**：
        # 开发环境不该因为这个变量没配就整条链路起不来。
        dsh_bin = (settings.agent_dsh_bin or "").strip()
        if not dsh_bin:
            candidate = Path(__file__).resolve().parents[3] / "agent-runtime" / "bin" / "run.cmd"
            dsh_bin = str(candidate) if candidate.is_file() else ""

        # ★ 运行时 patch：**必须显式传给 SDK**，不能指望 runtime 自动发现。
        #
        # 事实（t11 用 `dsh --profile sdk --dump-config` 逐字核对过合成顺序）：
        #   bundle 层 → profile 自己的 `<DSH_HOME>/profiles/sdk/cordis.patch.yml`
        #   → 家目录层 `<DSH_HOME>/cordis.patch.yml` → `--patch` 覆盖层（最后生效）
        # 本仓库的 `<DSH_HOME>/profiles/sdk/cordis.patch.yml` 是**空数组**，家目录层根本
        # 不存在，而 `patches=()` 又不给覆盖层 —— 于是插件的 `insert` 与全部 `disabled`
        # 一条都没生效：模型手里没有任何 FPA 业务工具，却留着 `tool-web`（能发任意 HTTP）、
        # `tool-fs`（能读文件）、`tool-pwsh`（能执行命令）。实测后果就是模型自己的交代：
        #   「本会话里我没有挂载 FPA 的业务工具，所以是直接用系统给的 X-Agent-Context 凭据
        #     打后端网关 GET /api/v1/agent/tools … 拿到的数据」
        # —— 业务数据是**真的**，但拿数据的路径绕过了设计（类型化工具 + schema 约束），
        # 同时那条"禁止任意 Shell / 文件系统 / 网络"的安全边界整条不在。
        #
        # 这里用 `patches=` 而不是往 `<DSH_HOME>` 里写一份 patch，理由是让**接线在代码里
        # 可见**：一份"必须存在的文件、写错就是静默降级"的隐式约定，正是本项目一路在
        # 消除的失败模式（"装配本该没有可忘记的步骤"）。
        patch_file = resolve_harness_patch(settings)
        patches: tuple[str, ...] = (patch_file,)

        return DeepSeekHarness(
            dsh_home=settings_home,
            cwd=settings.agent_harness_root or settings_home,
            provider=settings.agent_model_provider,
            model=settings.agent_model,
            max_tokens=settings.agent_max_tokens,
            request_timeout_seconds=float(settings.agent_request_timeout_seconds),
            # ★ `dsh_bin` 必须显式给出（**不能省略**）。
            #
            # 省略后 SDK 会走 `client.py::_default_launch_args()` →
            # `resolve_bundled_launch_args()`（**不带 `'node'`**）→ 去找那个
            # **不存在的生产 exe**，报：
            #   FileNotFoundError: deepseek-harness-runtime-bin is missing the runtime executable at
            #     ...\deepseek-harness-sdk-runtime-win-x64.exe
            #
            # ★ 为什么是 `dsh_bin=` 而不是 `_launch_args=`（我第一版就在这里踩过）：
            #   `_launch_args` 会整块替掉 SDK 自己拼的参数，
            #   而 SDK 在那里还会补 `--profile <profile>`（`client.py:487`）。
            #   用 `_launch_args` 的话这个参数会丢，现象是 dsh 报：
            #     error: --profile <name> is required
            #   而传 `dsh_bin=` 则是"用我的可执行文件、其余参数你继续拼"。
            #
            # 这正是 `docs/DEVELOPMENT.md` §5.3 记载的对策——“**给 `dsh_bin` 跳过它**”：
            # `AGENT_DSH_BIN` 配置项一直在，但**从来没被用上**。
            # 注意 `or None`：空串不是 `None`，传 `""` 会被当成一个真实路径去 resolve。
            dsh_bin=dsh_bin or None,
            env=env,
            patches=patches,
        )

    def _render_prompt(self, prompt: str) -> str:
        """只加入业务提示，不把任何可重放凭据交给模型。"""
        return prompt

    @staticmethod
    def _translate(error: Exception) -> DomainError:
        text = f"{type(error).__name__} {error}".lower()
        if "timeout" in text:
            return DomainError(
                ErrorCode.AGENT_TIMEOUT, "智能助手响应超时，请把问题拆小一点后重试"
            )
        if any(marker in text for marker in ("protocol", "jsonrpc", "transport")):
            return DomainError(
                ErrorCode.AGENT_PROTOCOL_ERROR, "智能助手通信异常，请稍后重试"
            )
        return DomainError(
            ErrorCode.AGENT_UNAVAILABLE, "智能助手服务暂时不可用，请稍后重试"
        )


def _current_env() -> dict[str, str]:
    import os

    return dict(os.environ)


__all__ = [
    "ExecutedToolCall",
    "HarnessSessionManager",
    "HarnessTurn",
    "executed_tool_calls",
    "harness_health",
    "resolve_harness_patch",
]
