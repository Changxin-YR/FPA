"""浏览器对话轮次的处理器：把 `/api/v1/agent/turns` 接到 Harness 会话管理器上。

## 这个文件为什么存在（"可忘记的步骤"的第三次）

`yuxin.factory.build_app` 的注释里自己写着：

> Agent 网关也在这里接线：它原本要调用方自己 `app.config["YUXIN_AGENT_GATEWAY"] = gateway`，
> 而那正是"装配处与使用处分离"的另一个落点（忘记赋值 -> Agent 路由 503，而 app 启动
> 一切正常）。**装配本该没有可忘记的步骤。**

同一条缺陷在这里**第三次**出现，而且这次是完整的：`HarnessSessionManager` 实现齐全
（`run()` / `available` / `drop` / `close`），`routes_agent.py` 也在等
`app.config["YUXIN_AGENT_TURN"]`，但**全仓没有任何代码实例化那个类**。
`build_app(agent_turn_handler=None)` 的默认值让生产路径（`yuxin.wsgi:app`）
静默地什么都不装，于是用户在界面上"看不到智能助手"、接口返回
`503 AGENT_UNAVAILABLE`，**而应用启动、全部测试、全部 e2e 都是绿的**。

所以本模块的形态是刻意的：**装配处 `assemble()` 负责构造它**，调用方没有任何
"记得传一个 handler"的机会（`build_app` 上那个可选参数保留给夹具，不再是生产路径）。

## 一轮对话的数据流

    POST /api/v1/agent/turns  (浏览器 + 会话 Cookie + CSRF)
      -> routes_agent.agent_turn()  取 actor（身份只来自 Cookie），把消息交给 handler
      -> 本模块：签发**上下文令牌**（绑定 uid/sid/cid，TTL 见下）
      -> HarnessSessionManager.run()  启动/复用一个 Harness 子进程
      -> 子进程里的 `@yuxin/dsh-biz-tools` 插件带 `X-Agent-Context` 调
         `POST /api/v1/agent/tools/<name>/call`（**回调本进程**）
      -> L2 按令牌里的 sid 查库实时解析权限 -> 执行器 -> 业务服务 -> 真实数据库
      -> 本模块：把这一轮的产物翻译成 §3 的判别联合（`turn_result()`）交回浏览器
         —— 确认卡片（含**一次性令牌**）与反问卡片**只**经这一步回到浏览器；
            模型看到它们不等于用户能确认它们（见 `confirmation_result`）

## 令牌为什么必须"每个会话一份"而不是"每轮一份"

子进程的插件配置里 `contextToken` 在**进程启动时读一次**（`agent-runtime/src/index.ts`
的 `apply()`），而 `HarnessSessionManager` 按会话复用子进程（那正是它的设计目的：
把冷启动从"每轮一次"降到"每会话一次"）。**每轮重新签发令牌与"进程复用"是互斥的** ——
若每轮换令牌，复用的子进程会一直用第一次那个，若干轮之后必然过期。

因此这里的口径是：**令牌按 `(user_id, session_hash, conversation_id)` 签发一次，
TTL 取 `settings.agent_context_ttl_seconds`（默认 1800 秒 / 30 分钟）**。

安全性不来自"令牌短命"，而来自**每次工具调用都重新查库**：`_actor_from_context_token`
用令牌里的 `sid` 走 `access.resolve_by_session_hash()`，所以用户登出、权限被撤销，
**下一次工具调用立刻生效**（`docs/DECISIONS.md` Q17 已把这条裁决写死）。
`routes_agent.py` 里"令牌只能用于它被签发的那一轮"是**过时表述**（它自己的 TTL 参数
现在由 `AGENT_CONTEXT_TTL_SECONDS` 给到 1800），本模块以能跑的机制为准。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Iterator

from yuxin.harness.session import (
    RETRY_EVENT,
    RETRY_STARTED_EVENT,
    ExecutedToolCall,
    HarnessSessionManager,
    PendingConfirmation,
)
from yuxin.settings import Settings

logger = logging.getLogger(__name__)

#: 网关 URL 的环境变量名。与 `agent-runtime/cordis.patch.yml` 里那行
#: `gatewayUrl: !!js process.env.YUXIN_AGENT_GATEWAY_URL ?? ''` **是同一个键** ——
#: 插件就是从子进程环境里读它的，所以这里导出的名字必须与它一致。
GATEWAY_URL_ENV = "YUXIN_AGENT_GATEWAY_URL"

#: 上下文令牌的环境变量名（同样与 patch 文件里的 `contextToken:` 那行一致）。
CONTEXT_TOKEN_ENV = "YUXIN_AGENT_CONTEXT_TOKEN"


#: 前端路径前缀——`ResourceMeta.list_path` 是 API 地址，前端页面路径去掉这一段。
_API_PREFIX = "/api/v1"


def actor_context(actor: Any) -> str:
    """逐轮注入的**服务端权威身份**，与 `[当前页面]` 同一条范式。

    为什么必须有：`auth.me` 是**固定路由、不是 agent 工具**，模型手里没有"我是谁"的工具。
    实测（2026-09-16 线上巡检）：登录 `demo` 问「我是谁」，模型自己猜了一个 user_id 去调
    `access_user.get`，结果回复「我是张操作（经办人），用户名 test-operator，角色和数据范围为空」
    —— 它把库里另一个账号当成了自己。身份属于"服务端已知、模型不该推测"的信息，所以直接注入。
    """
    roles = ",".join(sorted(getattr(actor, "role_codes", ()) or ())) or "（无）"
    permissions = getattr(actor, "permissions", ()) or ()
    return (
        f"[当前登录用户] {getattr(actor, 'username', '')}；角色：{roles}；"
        f"权限条数：{len(permissions)}。"
        "回答「我是谁 / 我有什么权限 / 我的数据范围」时**以此为准**，"
        "不要用工具去猜或套用列表里的其他账号。"
    )


def _tool_name_index(registry: Any) -> dict[str, Any]:
    """`工具名 → Capability` 的反查表。

    **为什么必须用 `Capability.tool_name()` 而不是自己把 `_` 换回 `.`**：
    `tool_name()` 是"能力名 → 工具名"这个映射的**唯一实现处**（它还额外做了
    `-`→`_`）。在前端或这里再写一遍反推，就是第二处描述同一件事 ——
    而 `pond_status_change.request` 这种能力名一旦反推就会得到不存在的
    `pond.status.change.request`。
    """
    index: dict[str, Any] = {}
    for capability in registry.all():
        index[capability.tool_name()] = capability
    return index


def _frontend_url_of(registry: Any, resource: str) -> str:
    """资源的前端列表路径（由该资源读能力的 `path` 推导）。

    与 `meta.store.ts::resourcePathOf()` 是同一条推导规则：服务端给 API 地址，
    前端页面地址就是去掉 `/api/v1` 前缀。推导不出来时返回空串——
    **不编一条路径**（编出来的路径会让"完成的写入"变成一个点了就 404 的链接）。
    """
    for capability in registry.all():
        if capability.resource == resource and capability.kind == "read":
            path = capability.path or ""
            if path.startswith(_API_PREFIX):
                return path[len(_API_PREFIX):]
            return ""
    return ""


def executed_result(
    executed: list[ExecutedToolCall], registry: Any
) -> dict[str, Any] | None:
    """把"当前迭代写过什么"翻译成 `INTERFACES.md` §3 的 `executed` 结果；没写就返回 None。

    ## 多条写入时取哪一条

    一次对话里模型可能写多次（实测：先建往来单位、再建批次）。契约的 `executed`
    只有**一个** `result`，所以：

      * 顶层 `result` 取**最后一条** —— 它通常是用户要的那件事的收尾；
      * 但 `data.executed` 列出**全部**写入（能力 / 资源 / 资源 id 各一条）。

    这样"该刷新哪些资源"不会因为契约只能带一条而丢信息 —— 前端刷新需要的是
    **资源集合**，不需要"最后一个是谁"。
    """
    if not executed:
        return None
    index = _tool_name_index(registry)
    rows: list[dict[str, Any]] = []
    for item in executed:
        capability = index.get(item.tool_name)
        if capability is None:
            # 工具名不在注册表里：不可能发生（清单本来就是从注册表下发的）。
            # 真要发生，宁可**不报写入**也不要编一个能力名 —— 报错比谎报好，
            # 但这里只影响"刷不刷新"，所以记一条日志后跳过。
            logger.warning("工具 %s 不在能力注册表里，当前迭代不据此判定写入", item.tool_name)
            continue
        rows.append(
            {
                "capability": capability.name,
                "resource": capability.resource,
                "resource_id": item.resource_id,
            }
        )
    if not rows:
        return None
    last = rows[-1]
    return {
        "capability": last["capability"],
        "resource": last["resource"],
        "resource_id": last["resource_id"],
        "url": _frontend_url_of(registry, last["resource"]),
        # 契约扩展（附加字段，老客户端忽略）：当前迭代牵动过的**全部**资源。
        "data": {"executed": rows},
    }


def confirmation_result(
    pending: list[PendingConfirmation], *, conversation_id: str, reply: str
) -> dict[str, Any] | None:
    """把"当前迭代签发了哪些待确认卡片"翻译成 §3 ③（`confirmation_required`）；没有就返回 None。

    ## 为什么令牌必须回到浏览器（这是"卡片从来没弹出来"的根因）

    确认令牌是一次性的，而且**只在签发它的那一次响应里出现**
    （`kernel/confirmation.py::IssuedConfirmation` 的 `token` 字段注释）。
    因此"模型看到了卡片原文"完全不等于"用户能确认"：**没有别的地方可以补发令牌**，
    浏览器拿不到它，这张卡就等同于不存在。

    实测报障的形状：模型按插件的 `note` 在界面上说「已生成 3 张待确认卡片，请在界面上
    确认」，界面上 0 张卡；用户回一句"执行"，模型答「我这边还没有收到确认结果」——
    模型、网关、前端三方都对，缺的就是本函数这一段提取。

    ## `message` 为什么优先用模型这一轮的话

    卡片自己只有 `title` / `target` / `rows` / `impact`，说不出"为什么是这几个对象"。
    实测：一句话要求归档 3 个草稿区域，模型的回答是「草稿状态的区域有 3 个：测试北区、
    QA 测试北区、带路径参数」——那句是用户真正的上下文。网关渲染的
    「即将X，确认后立即生效。」是**兜底**，不是正文。两处都不编造，只做选择。
    """
    if not pending:
        return None
    cards = [dict(item.confirmation) for item in pending]
    server_message = next((item.message for item in pending if item.message), "")
    payload: dict[str, Any] = {
        "kind": "confirmation_required",
        "conversation_id": conversation_id,
        "message": reply.strip() or server_message or "该操作需要您确认后才会执行。",
        "confirmation": cards[0],
    }
    if len(cards) > 1:
        # 契约扩展（附加字段，老客户端忽略）：同一轮签发的**全部**卡片，按发生顺序。
        # `confirmation` 仍然是其中第一张，所以按 §3 ③ 原文写的客户端行为不变。
        #
        # 为什么不能只留一张：一次对话里模型可以对多个对象各签一张卡（实测 3 张）。
        # 丢掉的那几张**无法补发**（令牌只出现一次），用户就永远确认不了它们了。
        payload["confirmations"] = cards
    return payload


def clarification_result(
    question: dict[str, Any] | None, *, conversation_id: str, reply: str
) -> dict[str, Any] | None:
    """把 `ask_user` 的那次提问翻译成 §3 ②（`clarification`）；没有就返回 None。

    与确认卡片是**同一个交付缺口**：插件把问题交给模型之后当前迭代就结束了，浏览器那一侧
    什么都没收到。前端 `AgentPanel` 的澄清卡片（问题 + 可点选项 + 自由作答框）
    因此在生产路径上一次都没被触发过 —— 用户只会看到模型复述的一行文字，
    而**选项与输入框全都丢了**。

    注：`question` 本身就是给用户看的那句话；只有它为空时才退回模型正文
    （空卡片等于界面上什么都不显示，那是最难查的一种失败）。

    模型这一轮的话单独放在 `message` 里（**可选字段**）：实测模型在提问前后会补上
    有用的上下文（「（可选补充：联系人、电话、地址、结算天数、信用额度。）」），
    那部分不在 `question` 里，丢掉就永远看不到了。卡片本身仍然只由 question/options 驱动。
    """
    if not question:
        return None
    options = question.get("options")
    payload: dict[str, Any] = {
        "kind": "clarification",
        "conversation_id": conversation_id,
        "question": str(question.get("question") or "").strip() or reply.strip(),
        "options": [str(item) for item in options] if isinstance(options, list) else [],
        "allow_free_text": question.get("allow_free_text") is not False,
    }
    if reply.strip():
        payload["message"] = reply
    return payload


def turn_result(turn: Any, registry: Any) -> dict[str, Any]:
    """把一轮对话的产物翻译成 `INTERFACES.md` §3 的**判别联合**。

    ## 流式与非流式必须共用这一个函数

    两条路径各自判断"这一轮写没写、在等什么"，就是第二处描述同一件事，
    而它们的答案会漂移（本项目记过这个形态的事故：同一个轮次在一条路径上是 `assistant`、
    在另一条上是 `executed`，前端因此永远收不到该有的那个 kind）。
    所有关于"这一轮的产物是什么"的判断都收在这里，两个入口只负责把结果发出去。

    ## 分支顺序（有依据，不是随手排的）

    1. **`executed` 优先**：写已经真的发生了，前端必须据此重拉列表（`write-signal`）。
       若同轮还有待确认卡，沿用确认分支的字段附带交付，不能丢一次性令牌。
    2. **`confirmation_required`**：什么都没写、等用户点卡片（令牌只出现一次，
       见 `confirmation_result`）。
    3. **`clarification`**：等用户补一句话。
    4. **`assistant`**：纯回答。

    混合情形仍用单值 `kind="executed"`，同时附带 `confirmation(s)`。这样既不丢
    已发生写入的刷新信号，也不丢只返回一次的确认令牌。
    """
    conversation_id = str(getattr(turn, "session_id", "") or "")
    executed = executed_result(turn.executed, registry) if registry is not None else None
    pending = confirmation_result(
        turn.pending, conversation_id=conversation_id, reply=turn.reply
    )
    if executed is not None:
        payload = {
            "kind": "executed",
            "conversation_id": conversation_id,
            "message": turn.reply,
            "result": executed,
        }
        if pending is not None:
            payload["confirmation"] = pending["confirmation"]
            payload["confirmations"] = pending.get(
                "confirmations", [pending["confirmation"]]
            )
        return _guard_fake_confirmation(payload)

    if pending is not None:
        return _guard_fake_confirmation(pending)

    asked = clarification_result(
        getattr(turn, "clarification", None),
        conversation_id=conversation_id,
        reply=turn.reply,
    )
    if asked is not None:
        return _guard_fake_confirmation(asked)

    # ★ 末道闸：防模型**只说不做**。
    #
    # 实测（2026-09-16）：模型建塘口 → 提交核验 → 然后回一句
    # 「请在界面上确认执行核验操作」，**却从没调用 `pond.verify`**。
    # 会话日志里只有 area_list / pond_create / pond_submit，
    # `agent_confirmations` 也没有新行 —— 系统里根本没有待确认卡片，
    # 而用户看到的是“要求我确认，但按钮不出现”。
    #
    # 这与本仓一贯的口径一致：**“待确认”这个状态只能由服务端签发**，
    # 不能让模型用一句话凭空宣布。所以这里不改模型正文，只在后面**补一句
    # 服务端自己的说明**，把“系统里到底有没有卡”如实告诉用户。
    return _guard_fake_confirmation(
        {"kind": "assistant", "conversation_id": conversation_id, "message": turn.reply}
    )


def resolve_gateway_url(settings: Settings, *, port: int | None = None) -> str:
    """决定 Harness 子进程应该回调哪个地址。

    优先级（**从显式到推导，都有依据，不硬编码任何一台机器的地址**）：

    1. `settings.agent_gateway_url`（env `AGENT_GATEWAY_URL`）—— 部署方显式指定，
       反向代理/自定义域名的场景只有它能表达；
    2. 推导：`http://127.0.0.1:<port>/api/v1/agent`，端口取 `settings.port`
       （env `PORT`，与 `yuxin.wsgi` 听的是同一个值；未配置时用 Flask 默认 5000）。

    为什么推导值是 `127.0.0.1` 而不是 `0.0.0.0` 或某个局域网地址：
    Harness 子进程与后端**在同一台机器上**（`agent-runtime` 是本地子进程，
    `DSH_HOME`/`AGENT_HARNESS_ROOT` 都是本机路径），所以回环地址是唯一
    "不需要知道本机对外 IP、也不受多网卡影响"的选择。**不要**用 `request.host_url`：
    那会让地址随"用户是从哪个域名访问的"而变（同一进程多个调用方会拿到不同地址），
    而子进程的 config 在启动时只读一次。
    """
    configured = (settings.agent_gateway_url or "").strip()
    if configured:
        return configured
    resolved_port = port if port is not None else settings.port
    return f"http://127.0.0.1:{resolved_port}/api/v1/agent"


def build_agent_turn_stream_lines(
    *,
    settings: Settings,
    manager: HarnessSessionManager,
    gateway_url: str,
    registry: Any = None,
    actor: Any,
    message: str,
    conversation_id: str,
    page_context: str,
) -> Any:
    """**生成器**：跑一轮并把子进程事件翻译成 INTERFACES §3 的 NDJSON 行。

    行形状（前端 `service.ts::AgentStreamLine` 逐字依赖这三种）::

        {"type":"status","text":"…"}          # 只更新显示，不产生状态迁移
        {"type":"delta","text":"…"}           # 同上（正文增量）
        {"type":"result","result":{…}}        # **唯一的**结果交付点

    为什么必须是生成器：见 `HarnessSessionManager.run()` 的 `on_notification` 说明 ——
    需要工具的轮次要跑几分钟，一次性返回会被代理按"空闲超时"掐断（实测 502）。

    ## `status` 行的作用是**心跳**，不是装饰

    模型每次调用工具之间可能有几十秒没有正文输出。若只在有正文时写响应，
    连接照样会空闲到被掐。所以只要有事件就写一行 `status`（工具调用、步骤开始等），
    保证连接上**始终有字节流动**。
    """
    from yuxin.web.routes_agent import issue_context_token

    token = issue_context_token(
        secret=settings.secret_key,
        user_id=int(actor.user_id),
        session_hash=str(actor.session_hash),
        capability="",
        conversation_id=conversation_id,
        ttl_seconds=settings.agent_context_ttl_seconds,
    )

    prompt = message
    if page_context:
        prompt = f"{prompt}\n\n[当前页面] {page_context}"
    prompt = f"{prompt}\n\n{actor_context(actor)}"

    # 子进程事件 → NDJSON 行的缓冲。回调在 SDK 的接收线程里执行，生成器在请求线程里消费，
    # 所以用一个 list + 逐行弹出（同一个进程内、GIL 保证 append/pop 原子）。
    pending: list[str] = []

    def _on_notification(notification: Any) -> None:
        text = _line_from_notification(notification, conversation_id)
        if text:
            pending.append(text)

    # 先发一行：让浏览器/代理立刻看到"已受理"，而不是等第一个模型事件。
    yield json.dumps({"type": "status", "text": "已收到指令，正在处理…"}, ensure_ascii=False) + "\n"

    turn_holder: dict[str, Any] = {}

    def _worker() -> None:
        try:
            turn_holder["turn"] = manager.run(
                prompt=prompt,
                session_id=conversation_id,
                user_id=int(actor.user_id),
                session_hash=str(actor.session_hash),
                gateway_url=gateway_url,
                context_token=token,
                on_notification=_on_notification,
            )
        except BaseException as error:  # noqa: BLE001 - 由生成器翻译成 result/failed 行
            turn_holder["error"] = error

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    while worker.is_alive() or pending:
        if pending:
            yield pending.pop(0)
            continue
        worker.join(timeout=0.5)
        # 空闲心跳：即使模型这一分钟没有产出，也让连接上有字节。
        yield json.dumps({"type": "status", "text": "助手正在处理…"}, ensure_ascii=False) + "\n"

    worker.join()
    while pending:
        yield pending.pop(0)

    error = turn_holder.get("error")
    if error is not None:
        # 错误也必须以 `result` 行交付（前端只在收到 `result` 时才清理 busy）。
        raise error
    turn = turn_holder["turn"]
    # 与非流式 handler **同一份实现**（`turn_result()`）：流式与非流式不能对"这一轮
    # 写没写、在等什么"给出不同答案 —— 两条路径各自判断就是第二处描述同一件事。
    yield (
        json.dumps({"type": "result", "result": turn_result(turn, registry)}, ensure_ascii=False)
        + "\n"
    )


def _line_from_notification(notification: Any, conversation_id: str) -> str | None:
    """把一条子进程通知翻译成 `status` / `delta` 行；与当前迭代无关的返回 None。

    判据尽量窄（只认 `session.event` 且 `sessionId` 匹配），因为通知流里还混着
    别的会话/系统事件 —— 放进来会让界面显示不属于当前迭代的内容。

    覆盖的四种会话事件：正文增量（`assistant/chunk`）、工具调用开始与结束
    （`tool/call*`）、以及**自动重试**（`llm/retry` / `llm/retry-started`，见下面注释）。

    ## `assistant/chunk` 里只有「模型对用户说的话」能变成 delta

    同一条 `assistant/chunk` 按 `chunk.type` 分成好几种：正文（`text-delta`）、
    **思考**（`reasoning-delta`）、工具参数（`tool-call-delta`）、块边界与用量统计。
    初版按"**有没有 `text` 键**"判定，于是把模型的思考当成正文发给了前端 ——
    实测漏出去的两条就是 `{"type":"reasoning-delta","index":0,"text":"I must"}` 与
    `{"type":"reasoning-delta","index":0,"text":"Actually."}`。

    症状：助手正文中英夹杂、碎成「我来您I must执行。Actually.我这边」，
    而**思考的语言与正文的语言不一样**（模型用英文推理、用中文作答），
    于是用户报障"智能体总是输出英文"。

    判据因此改成**白名单 `chunk.type == "text-delta"`**，方向与"黑名单掉 reasoning"
    相反：Harness 以后新增的 chunk 类型默认**不外发**，而不是默认外发 ——
    思考泄漏是不可逆的，它会混进用户已经看过的那段文字里。
    """
    method = getattr(notification, "method", "")
    payload = getattr(notification, "payload", None)
    if method != "session.event" or not isinstance(payload, dict):
        return None
    if str(payload.get("sessionId") or "") != conversation_id:
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    kind = str(event.get("type") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}

    if kind == "assistant/chunk":
        chunk = data.get("chunk") if isinstance(data.get("chunk"), dict) else {}
        # 只认正文增量。思考（`reasoning-delta`）与工具参数（`tool-call-delta`）都不是
        # 给用户看的东西 —— 见上面"只有模型对用户说的话能变成 delta"。
        if str(chunk.get("type") or "") != "text-delta":
            return None
        for key in ("text", "delta", "content"):
            value = chunk.get(key)
            if isinstance(value, str) and value:
                return json.dumps({"type": "delta", "text": value}, ensure_ascii=False) + "\n"
        return None

    if kind in {"tool/call", "tool/call/start", "assistant/tool-call"}:
        name = str(data.get("name") or data.get("toolName") or "").strip()
        label = f"正在调用工具 {name}…" if name else "正在调用工具…"
        return json.dumps({"type": "status", "text": label}, ensure_ascii=False) + "\n"

    if kind == "tool/result" or kind == "tool/call/end":
        return json.dumps({"type": "status", "text": "工具已返回，正在整理回答…"}, ensure_ascii=False) + "\n"

    # ★ 自动重试必须**在界面上可见**，不能只在日志里。
    #
    # `dsh-llm-retry` 遇到瞬时失败（实测 `TRANSPORT`）会退避重试（0.5s→10s 递增，共 5 次），
    # 这段空档里没有正文、也没有工具调用 —— 若不翻译成 status 行，用户看到的就是长时间
    # "助手正在处理…"，一次**真实发生的失败**被读成了正常等待，而重试次数在 UI 上为 0。
    # 前端 `AgentStreamLine` 只认 status/delta/result 三种行，所以这里用 status 承载。
    if kind == RETRY_EVENT:
        failure = data.get("failure") if isinstance(data.get("failure"), dict) else {}
        attempt = data.get("retry")
        limit = data.get("maxRetries")
        code = str(failure.get("code") or "").strip()
        parts = ["模型服务响应异常"]
        if code:
            parts.append(f"（{code}）")
        if isinstance(attempt, int) and isinstance(limit, int):
            parts.append(f"，正在自动重试（第 {attempt}/{limit} 次）…")
        else:
            parts.append("，正在自动重试…")
        return json.dumps({"type": "status", "text": "".join(parts)}, ensure_ascii=False) + "\n"

    if kind == RETRY_STARTED_EVENT:
        return (
            json.dumps({"type": "status", "text": "重试请求已发出，等待模型响应…"}, ensure_ascii=False)
            + "\n"
        )

    return None


def build_agent_turn_handler(
    *,
    settings: Settings,
    manager: HarnessSessionManager,
    gateway_url: str,
    registry: Any = None,
) -> Callable[..., dict[str, Any]]:
    """构造 `routes_agent.agent_turn` 期望的 handler。

    契约（`routes_agent.py` 按关键字调用，返回体直接进响应的 `data`）::

        handler(*, actor, message, conversation_id, page_context, request_id) -> dict

    返回体是 §3 的**判别联合**（`turn_result()` 的产物），**不是**自由字典：
    `AgentPanel.vue::applyResult()` 按 `result.kind` 分派，形状不对（例如早期那版
    只回一个 `{reply: …}`）就"任何分支都不匹配" —— 用户发了消息、界面什么都不显示，
    而 HTTP 是 200、后端日志里没有错误。
    """
    from yuxin.web.routes_agent import issue_context_token

    def handle(
        *,
        actor: Any,
        message: str,
        conversation_id: str,
        page_context: str,
        request_id: str,
    ) -> dict[str, Any]:
        """跑一轮对话。

        `page_context`（当前路由路径）会拼进提示词：模型因此知道"用户现在在哪个页面"，
        而不需要在工具调用里猜。它**不是**授权信息——权限只由令牌 + 服务端查库决定。
        """
        # 令牌绑定 (uid, sid, cid)：见模块文档"令牌为什么必须每个会话一份"。
        # `capability` 传空串：令牌的授权面是**该用户的实时权限集**，
        # 由 `/agent/tools` 与 `/agent/tools/<name>/call` 每次查库重新过滤；
        # 令牌里的 `cap` 字段只是一条给审计看的备注，不是判定依据。
        context_token = issue_context_token(
            secret=settings.secret_key,
            user_id=int(actor.user_id),
            session_hash=str(actor.session_hash),
            capability="",
            conversation_id=conversation_id,
            ttl_seconds=settings.agent_context_ttl_seconds,
        )

        prompt = message
        if page_context:
            prompt = f"{prompt}\n\n[当前页面] {page_context}"
        prompt = f"{prompt}\n\n{actor_context(actor)}"

        turn = manager.run(
            prompt=prompt,
            session_id=conversation_id,
            user_id=int(actor.user_id),
            session_hash=str(actor.session_hash),
            gateway_url=gateway_url,
            context_token=context_token,
        )
        # ★ 返回体必须是 `docs/INTERFACES.md` §3 的**判别联合**，不是自由字典。
        #
        # 为什么这一条不修就等于没接：`AgentPanel.vue::applyResult()` 按 `result.kind`
        # 分派（`assistant` / `clarification` / `confirmation_required` / `executed`）。
        # 若这里返回 `{reply: …}`，前端**任何分支都不匹配** → 用户发了消息、界面什么都不显示
        # —— 而 HTTP 是 200、后端日志没有错误。那是最难查的一类"接线接了一半"。
        # 形状的权威是 `frontend/src/layers/common/agent/service.ts::AgentTurnResult`，
        # 它由 `frontend/tests/agent-contract-shape.spec.ts` 逐字对着 §3 钉住。
        #
        # ★ 四种 kind 的判定全部收在 `turn_result()` 一处，流式路由调用的是同一个函数。
        #
        # 这里曾经**只**接了 `assistant` 与 `executed`：`confirmation_required` 与
        # `clarification` 只发给模型、从不回浏览器（当时的注释把这件事写成"另一个增量"）。
        # 后果是两张已经在界面上做好、也各自测过的卡片**在生产路径上永远不出现** ——
        # 模型说"请在卡片上确认"，而用户界面上没有卡片；两边都不报错。
        return turn_result(turn, registry)

    return handle


#: 模型"只说不做"的判据：正文里出现这些词、但系统**没有签发任何待确认卡片**。
_FAKE_CONFIRMATION_HINTS = (
    "请确认执行",
    "点击确认",
    "请点击确认",
    "确认卡片",
    "等待您确认",
    "等你确认",
    "请在界面上确认",
)


def _guard_fake_confirmation(payload: dict[str, Any]) -> dict[str, Any]:
    """正文宣称“有待确认操作”、但这一轮**没签发任何卡片**时，补一句服务端说明。

    为什么必须在服务端拦：实测（2026-09-16）模型建塘口 → 提交核验 →
    回一句「请在界面上确认执行核验操作」，**却从没调用 `pond.verify`**：
    会话日志里只有 area_list / pond_create / pond_submit，`agent_confirmations`
    也没有新行。用户看到的是“要求我确认，但按钮不出现”。

    与本仓一贯口径一致：**“待确认”这个状态只能由服务端签发**。
    所以不改模型正文，只在后面补一句服务端自己的说明。
    """
    if payload.get("confirmation") or payload.get("confirmations"):
        return payload
    if payload.get("kind") not in ("assistant", "executed"):
        return payload
    if not _claims_pending_confirmation(str(payload.get("message") or "")):
        return payload
    payload["message"] = (
        f"{payload.get('message') or ''}\n\n"
        "（系统提示：本轮**没有**生成待确认卡片，上面提到的「确认执行」并未发生。"
        "请让我用工具重新发起该操作。）"
    )
    return payload


def _claims_pending_confirmation(reply: str) -> bool:
    """正文是否在宣称“有一个待确认操作在等你”。

    只在**没有任何待确认卡片**时才调用（调用点在 `pending` 为 None 之后）。
    """
    text = str(reply or "")
    return any(hint in text for hint in _FAKE_CONFIRMATION_HINTS)


__all__ = [
    "CONTEXT_TOKEN_ENV",
    "GATEWAY_URL_ENV",
    "build_agent_turn_handler",
    "build_agent_turn_stream_lines",
    "clarification_result",
    "confirmation_result",
    "executed_result",
    "resolve_gateway_url",
    "turn_result",
]
