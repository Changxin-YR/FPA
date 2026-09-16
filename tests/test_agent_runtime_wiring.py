"""Harness 接线的守卫：**运行时 patch 真的接上了**、**自动重试真的可观测**、
**模型的思考不外泄到界面**、**网关签发的卡片真的送达浏览器**。

## 这一组守的是几个"全绿但是坏的"缺陷

**① 插件没被挂载，但一切测试都绿。** `session.py` 构造 `DeepSeekHarness(...)` 时传的是
`patches=()`，而 dsh 的合成顺序是：

    bundle 层 → profile 自己的 `<DSH_HOME>/profiles/sdk/cordis.patch.yml`
              → 家目录层 `<DSH_HOME>/cordis.patch.yml`
              → `--patch` 覆盖层（最后生效）

本仓库 profile patch 是空数组、家目录层不存在、`patches=()` 又不给覆盖层 ——
于是 `insert` 与 52 条 `disabled` 一条都没生效。后果是模型**没有业务工具却留着
`tool-web`**，于是它自己拼 HTTP 打后端网关（实测自述：「本会话里我没有挂载 渔芯的业务
工具…」）。业务数据是真的，但路径绕过了设计、安全边界整条不在，
而**应用启动正常、pytest 全绿、浏览器 e2e 也能过**。

**② 自动重试把间歇性故障掩盖成"链路正常"。** 上游 `api.deepseek.com` 间歇性
`TRANSPORT`，`dsh-llm-retry` 会按提供商策略重试（实测同一轮里 retry 1→5、退避
0.5s→8s 递增）。这些事件只落进会话日志，而 `session.py` 只读 `final_response` ——
"这一轮是第几次才成的"完全不可见；重试耗尽时 `session.run()` **不抛异常**，
用户只看到一句通用文案，`TRANSPORT` 这个原始码不会出现在任何地方。

**③ 模型的思考被当成正文流给了用户（报障："智能体总是输出英文"）。**
`assistant/chunk` 按 `chunk.type` 分成正文（`text-delta`）与思考（`reasoning-delta`）
等几种，而 `_line_from_notification` 初版按"**有没有 `text` 键**"判定 —— 于是两者
一起外发。模型**用英文推理、用中文作答**，界面上于是出现
「我来您I must执行。Actually.我这边」这种中英夹杂的碎片；有的轮次模型没写正文，
界面上**只剩**这些英文碎片。实测 `.dsh-home/sessions` 里 34 个会话命中这个泄漏。

**④ 网关签发的卡片从来没送到浏览器（报障："说弹确认卡片也没弹出来"）。**
确认令牌是一次性的、**只在签发它的那一次响应里出现**，所以"模型看到卡片"≠"用户
能确认"：没有别的地方可以补发令牌。实测会话 `8a148ce4220e04e8`：用户说"帮我把我的
所有草稿全部归档" → 网关签了 3 张卡 → 模型答"已生成 3 张待确认卡片，请在界面上
确认" → **界面上 0 张卡** → 用户回"执行" → 模型答"我这边还没有收到确认结果"。
同一形状的缺口还有 §3 ② 的 `clarification`：实测会话 `bf3c8c19c0c2e101` 里
`ask_user` 返回了 `options:["供应商","客户"]`，而浏览器只收到一句正文 ——
**两个可点选项与自由作答框全丢了**。

## 判据为什么这样取

* 事件夹具**逐字取自真实会话日志**（`.dsh-home/sessions/**/*.jsonl.zstd`，
  会话 `9076cbea330758a2` 那两轮），不是我自己编的形状 —— 编形状只能证明我认识自己的假设；
* 断言里钉了**次数、原始 code、退避秒数**三样：只断言"有重试"无法区分
  "第 1 次就成"与"第 5 次才成"，那正是"把不稳定链路读成稳定"的形态；
* 正例与反例成对：**一次就成**的轮次必须给出 `retry_count == 0`，
  而且 `retries` 与 `terminal_failure` 都为空 —— 恒返回"有重试"的实现过不了这一条。
"""

from __future__ import annotations

import json
import os
import sys
import types

import pytest

from yuxin.harness.session import (
    BIZ_TOOLS_PACKAGE,
    ExecutedToolCall,
    HarnessTurn,
    PendingConfirmation,
    clarification_question,
    harness_health,
    pending_confirmations,
    resolve_harness_patch,
)
from yuxin.web.agent_turn import _line_from_notification, turn_result

# ---------------------------------------------------------------------------
# 真实事件夹具：逐字抄自 `.dsh-home/sessions/.../9076cbea330758a2/session.jsonl.zstd`
# ---------------------------------------------------------------------------

#: 一次 `TRANSPORT` 失败后被安排的第 3 次重试（真实日志的原样形状）。
RETRY_TRANSPORT_3 = {
    "type": "llm/retry",
    "seq": 2954,
    "time": 17892651611745,
    "data": {
        "retryId": "377371de-8c36-4464-8b8d-79b2372844ff",
        "turn": 1,
        "step": 8,
        "provider": "deepseek-official",
        "mode": "normal",
        "policyKey": (
            '["normal",5,["EMPTY_RESPONSE","RATE_LIMIT","SERVER","TIMEOUT","TRANSPORT"],'
            "500,10000,0.1]"
        ),
        "retry": 3,
        "maxRetries": 5,
        "delayMs": 2016.0950976602367,
        "failure": {
            "message": "DeepSeek API request to https://api.deepseek.com failed",
            "code": "TRANSPORT",
        },
    },
}

#: 重试真正开始（退避结束）——它只带 retryId 与第几次，没有 failure。
RETRY_STARTED_3 = {
    "type": "llm/retry-started",
    "seq": 2955,
    "time": 1789265163768,
    "data": {"retryId": "377371de-8c36-4464-8b8d-79b2372844ff", "turn": 1, "step": 8, "retry": 3},
}

#: 重试**耗尽**后的终局：`turn/end` 的 reason 带终端失败码（真实日志原样）。
TURN_END_ERROR_TRANSPORT = {
    "type": "turn/end",
    "seq": 4651,
    "time": 17892651944417,
    "data": {
        "turn": 1,
        "reason": {
            "kind": "error",
            "error": {
                "message": "DeepSeek API request to https://api.deepseek.com failed",
                "code": "TRANSPORT",
            },
        },
    },
}

#: 一次**正常**结束的轮次（其它会话里的真实形状）。
TURN_END_COMPLETED = {
    "type": "turn/end",
    "seq": 44,
    "time": 1789262595262,
    "data": {"turn": 1, "reason": {"kind": "completed"}},
}


class _Notification:
    """最小通知替身：`_line_from_notification` 只读 `method` 与 `payload`。"""

    def __init__(self, method: str, payload: dict) -> None:
        self.method = method
        self.payload = payload


def _session_event(event: dict, session_id: str = "conv-1") -> _Notification:
    return _Notification("session.event", {"sessionId": session_id, "event": event})


# ---------------------------------------------------------------------------
# ① 重试投影
# ---------------------------------------------------------------------------


def test_no_retry_turn_is_clean() -> None:
    """反例：一次就成的轮次必须读数干净，否则"有重试"这个信号没有分辨力。"""
    health = harness_health([TURN_END_COMPLETED])

    assert health["retry_count"] == 0
    assert health["retries"] == []
    assert health["terminal_failure"] is None


def test_retry_events_project_original_code_and_backoff() -> None:
    """重试事件必须投影出**原始 code / message / 第几次 / 上限 / 退避秒数**。"""
    health = harness_health([RETRY_TRANSPORT_3, RETRY_STARTED_3])

    assert health["retry_count"] == 1, "只统计 `llm/retry`，`llm/retry-started` 不是一次新重试"
    record = health["retries"][0]
    assert record["attempt"] == 3
    assert record["max_retries"] == 5
    assert record["code"] == "TRANSPORT"
    assert record["message"] == "DeepSeek API request to https://api.deepseek.com failed"
    # 2016.0950976602367 ms → 2.0 s：退避必须能读出来，"重试了几次"不够，
    # "每次等多久"才是判断链路抖动幅度的依据。
    assert record["backoff_seconds"] == 2.0
    assert record["provider"] == "deepseek-official"


def test_exhausted_retries_expose_terminal_code() -> None:
    """重试耗尽后终端失败码必须出现 —— 这正是"TRANSPORT 看不到"的那一格。"""
    retries = []
    for attempt, delay in ((1, 478.2591902786006), (2, 983.9916631792911), (3, 2016.0950976602367),
                           (4, 4339.098948799838), (5, 8183.663112861706)):
        retries.append(
            {
                "type": "llm/retry",
                "data": {
                    "retry": attempt,
                    "maxRetries": 5,
                    "provider": "deepseek-official",
                    "mode": "normal",
                    "delayMs": delay,
                    "failure": {
                        "message": "DeepSeek API request to https://api.deepseek.com failed",
                        "code": "TRANSPORT",
                    },
                },
            }
        )

    health = harness_health([*retries, TURN_END_ERROR_TRANSPORT])

    assert health["retry_count"] == 5, "第 5 次才失败 = 5 次重试，不能被压成 1"
    assert health["terminal_failure"] == {
        "code": "TRANSPORT",
        "message": "DeepSeek API request to https://api.deepseek.com failed",
    }
    assert [r["backoff_seconds"] for r in health["retries"]] == [0.5, 1.0, 2.0, 4.3, 8.2]
    assert all(r["code"] == "TRANSPORT" for r in health["retries"])


def test_health_tolerates_malformed_events() -> None:
    """事件来自子进程，形状不可全信：坏数据不能让健康度投影抛异常。"""
    health = harness_health(
        [
            "not a dict",
            {"type": "llm/retry"},                                  # 没 data
            {"type": "llm/retry", "data": {"retry": 1}},             # data 里什么都没有
            {"type": "turn/end", "data": {"reason": {"kind": "error"}}},  # error 不是对象
            {"type": "turn/end", "data": None},
        ]
    )

    assert health["retry_count"] == 1
    assert health["retries"][0]["code"] is None
    assert health["retries"][0]["backoff_seconds"] is None
    assert health["terminal_failure"] == {"code": None, "message": None}


# ---------------------------------------------------------------------------
# ② 界面可见性：重试不再是"几十秒没有解释的等待"
# ---------------------------------------------------------------------------


def test_retry_becomes_a_visible_status_line() -> None:
    """一次真实发生的失败尝试必须能在界面上看见，而不是被读成"助手正在处理…"。"""
    line = _line_from_notification(_session_event(RETRY_TRANSPORT_3), "conv-1")

    assert line is not None
    payload = json.loads(line)
    assert payload["type"] == "status", "前端只认 status/delta/result 三种行"
    assert "TRANSPORT" in payload["text"], "原始码必须显示出来，否则只是一句「稍等」"
    assert "第 3/5 次" in payload["text"], "必须能看出是第几次，否则无法区分抖动与持续故障"


def test_retry_started_is_a_visible_status_line() -> None:
    line = _line_from_notification(_session_event(RETRY_STARTED_3), "conv-1")

    assert line is not None
    assert json.loads(line)["type"] == "status"


def test_other_sessions_retry_is_not_shown() -> None:
    """别的会话的重试不能混进当前迭代界面（判据必须窄）。"""
    assert _line_from_notification(_session_event(RETRY_TRANSPORT_3, "conv-2"), "conv-1") is None


# ---------------------------------------------------------------------------
# ③ 生产接线：patch 路径必须来自仓库、且真的存在
# ---------------------------------------------------------------------------


def test_resolve_harness_patch_finds_the_repo_file() -> None:
    """`resolve_harness_patch()` 必须解析到仓库内那份**运行时** patch。

    它同时证明"接线在代码里"：只要这一步返回空串，`_create_session` 就会退回
    `patches=()`，也就是上面那个"全绿但是坏的"缺陷。
    """
    from yuxin.settings import Settings

    resolved = resolve_harness_patch(Settings.from_env())

    assert resolved, "解析不到运行时 patch：子进程会退回 Harness 出厂形态（无业务工具、有逃逸面）"
    from pathlib import Path

    patch = Path(resolved)
    assert patch.is_file()
    assert patch.parent.name == "agent-runtime", resolved
    assert patch.name == "cordis.patch.yml", resolved


def test_explicit_setting_wins_and_missing_file_is_loud() -> None:
    """显式配置优先；指向不存在的文件时直接拒绝启动，不静默降级。"""
    from dataclasses import replace

    from yuxin.settings import Settings

    settings = replace(
        Settings.from_env(), agent_harness_patch=r"C:\definitely\not\here\cordis.patch.yml"
    )

    from yuxin.kernel.errors import DomainError, ErrorCode

    with pytest.raises(DomainError) as error:
        resolve_harness_patch(settings)
    assert error.value.code == ErrorCode.AGENT_UNAVAILABLE


def test_patch_file_states_the_business_tool_package() -> None:
    """patch 文件必须**解析后**满足：一个 insert + 禁用列表 + 渔芯人格。

    ⚠️ 这里读的是结构，不是文本。第一版用 `"{{" not in text` 这种字符串搜索，
    结果命中了注释里"本文件不许出现 `{<!-- -->{`"那句话 —— 判据读散文就会读错。
    """
    from pathlib import Path

    import yaml

    from yuxin.settings import Settings

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_constructor(
        "tag:yaml.org,2002:js", lambda loader, node: loader.construct_scalar(node)
    )

    patch = Path(resolve_harness_patch(Settings.from_env()))
    entries = yaml.load(patch.read_text(encoding="utf-8"), Loader=_Loader)

    assert isinstance(entries, list) and entries, "运行时 patch 必须是顶层 YAML 数组"
    inserts = [e for e in entries if isinstance(e, dict) and "insert" in e]
    disables = [e for e in entries if isinstance(e, dict) and e.get("disabled") is True]
    assert len(inserts) == 1, "工具源只有一个 insert 段"
    assert len(disables) >= 50, f"禁用列表被削短了：只剩 {len(disables)} 条"
    assert any(e.get("name") == BIZ_TOOLS_PACKAGE for e in inserts[0]["insert"]), (
        "insert 必须指向本项目的插件包名"
    )

    prompt_row = [e for e in entries if isinstance(e, dict) and e.get("id") == "system-prompt"]
    assert len(prompt_row) == 1, "部署人格必须覆盖 `system-prompt` 行"
    config = prompt_row[0].get("config") or {}
    assert config.get("includeHarnessIdentity") is False, "必须关掉 Harness 写死的身份句"
    persona = str(config.get("persona") or "")
    assert "渔芯AI水产养殖一体化系统" in persona and "塘小助" in persona, "人格必须是渔芯AI水产养殖一体化系统的，不是 Harness 默认编程助手"
    # `{{` 会被 Harness 当提示词变量严格解析，未注册的变量让整轮渲染报错。
    assert "{{" not in persona, "persona 里不许出现提示词变量引用"


@pytest.mark.parametrize("field", ["retry_count", "retries", "terminal_failure"])
def test_health_shape_is_stable(field: str) -> None:
    """三个键是日志与错误 data 的契约面，缺一个就等于那部分又不可观测了。"""
    assert field in harness_health([])


def test_dev_server_passes_listen_port_to_agent_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 端口必须同时成为 Agent Gateway 的回调端口。"""
    from tools import serve_dev

    waitress = types.ModuleType("waitress")
    waitress.serve = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    wsgi = types.ModuleType("yuxin.wsgi")
    wsgi.app = object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "waitress", waitress)
    monkeypatch.setitem(sys.modules, "yuxin.wsgi", wsgi)
    monkeypatch.delenv("PORT", raising=False)

    assert serve_dev.main(["serve_dev.py", "5199"]) == 0
    assert os.environ["PORT"] == "5199"


# ---------------------------------------------------------------------------
# ④ 思考不得外泄：`assistant/chunk` 里只有 `text-delta` 能变成 `delta` 行
# ---------------------------------------------------------------------------

REASONING_DELTA = {
    "type": "assistant/chunk",
    "seq": 1327,
    "time": 1789310275686,
    "data": {
        "turn": 3,
        "step": 2,
        "chunk": {
            "type": "reasoning-delta",
            "index": 0,
            "text": "I"
        }
    }
}
REASONING_DELTA_ACTUALLY = {
    "type": "assistant/chunk",
    "seq": 1842,
    "time": 1789310290672,
    "data": {
        "turn": 4,
        "step": 1,
        "chunk": {
            "type": "reasoning-delta",
            "index": 0,
            "text": "Actually"
        }
    }
}
TEXT_DELTA = {
    "type": "assistant/chunk",
    "seq": 36,
    "time": 1789310206736,
    "data": {
        "turn": 1,
        "step": 1,
        "chunk": {
            "type": "text-delta",
            "index": 1,
            "text": "我来"
        }
    }
}


def test_reasoning_delta_never_becomes_a_delta_line() -> None:
    """思考一旦外发就**不可逆**：它会混进用户已经看过的那段正文里。

    这两条就是报障里那段"没有理由出现在正文里的英文"（`I` / `Actually`）。
    """
    assert _line_from_notification(_session_event(REASONING_DELTA), "conv-1") is None
    assert _line_from_notification(_session_event(REASONING_DELTA_ACTUALLY), "conv-1") is None


def test_text_delta_is_still_delivered() -> None:
    """正例对照：修掉泄漏不能顺手把正文增量也修没了 —— 那会让流式显示整个变空。"""
    line = _line_from_notification(_session_event(TEXT_DELTA), "conv-1")

    assert line is not None
    assert json.loads(line) == {"type": "delta", "text": "我来"}


def test_chunk_types_are_whitelisted_not_blacklisted() -> None:
    """判据方向必须是**默认不外发**。

    初版的形状是"有 `text` 键就外发"，那等于默认放行 —— Harness 以后加一种带
    `text` 的 chunk 类型就再漏一次。这里用一个不存在的类型钉住白名单方向。
    """
    future_chunk = {
        "type": "assistant/chunk",
        "data": {"turn": 1, "step": 1, "chunk": {"type": "future-delta", "text": "秘密"}},
    }

    assert _line_from_notification(_session_event(future_chunk), "conv-1") is None


# ---------------------------------------------------------------------------
# ⑤ 确认卡片必须真的到达浏览器（令牌只出现一次，丢了就永远确认不了）
# ---------------------------------------------------------------------------

TOOL_CALL_AREA_ARCHIVE = {
    "type": "tool/call",
    "seq": 1318,
    "time": 1789310274805,
    "data": {
        "turn": 3,
        "step": 1,
        "callId": "call_00_NbFGDiICkYS1cDfUJZHL3842",
        "name": "area_archive",
        "arguments": "{\"area_id\": 9225, \"expected_version\": 1}"
    }
}
TOOL_RESULT_AREA_ARCHIVE = {
    "type": "tool/result",
    "seq": 1319,
    "time": 1789310274858,
    "data": {
        "turn": 3,
        "step": 1,
        "message": {
            "source": {
                "kind": "tool",
                "callId": "call_00_NbFGDiICkYS1cDfUJZHL3842"
            },
            "content": [
                {
                    "type": "tool-result",
                    "toolCallId": "call_00_NbFGDiICkYS1cDfUJZHL3842",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "{\"kind\":\"confirmation_required\",\"message\":\"即将停用区域，确认后立即生效。\",\"confirmation\":{\"capability\":\"area.a"
                                "rchive\",\"expires_at\":\"2026-09-13T14:42:54.842974Z\",\"id\":24,\"impact\":[\"写入业务数据\"],\"rows\":[{\"label\":"
                                "\"区域 ID\",\"value\":\"9225\"},{\"label\":\"乐观锁版本\",\"value\":\"1\"}],\"target\":\"停用区域\",\"title\":\"停用区域\",\"token\":\"Q"
                                "oJwaKUq55apnjOnss7zOWNRlxEYGtOwNhLF7L2RQlY\"},\"note\":\"本条调用尚未写入任何数据，只生成了一张待确认卡片。请立即停止当前迭代输出，等待用户在界面上"
                                "确认或取消；在收到确认结果之前，不要向用户报告任何执行结果。\"}"
                            )
                        }
                    ],
                    "isError": False
                }
            ],
            "role": "user",
            "id": "d44fe6b9-526f-4dab-9c89-fd671e0980f9"
        }
    },
    "sourceEventSeqs": [
        1318
    ],
    "surfaceOp": "append"
}
CARD_AREA_ARCHIVE_2 = {
    "capability": "area.archive",
    "expires_at": "2026-09-13T14:42:54.905847Z",
    "id": 25,
    "impact": [
        "写入业务数据"
    ],
    "rows": [
        {
            "label": "区域 ID",
            "value": "9227"
        },
        {
            "label": "乐观锁版本",
            "value": "1"
        }
    ],
    "target": "停用区域",
    "title": "停用区域",
    "token": "DSWlTNHVFPuaed7s2tWFToy4_a6sGrTrm_JQJ9iKbPk"
}
CARD_AREA_ARCHIVE_3 = {
    "capability": "area.archive",
    "expires_at": "2026-09-13T14:42:54.966751Z",
    "id": 26,
    "impact": [
        "写入业务数据"
    ],
    "rows": [
        {
            "label": "区域 ID",
            "value": "9228"
        },
        {
            "label": "乐观锁版本",
            "value": "8"
        }
    ],
    "target": "停用区域",
    "title": "停用区域",
    "token": "YEMnWySkWUnUCjbtmeG2YyCNnuZgD_3Zh-v9FsEAaXc"
}

#: 这三张卡是同一次对话里对三个草稿区域各签的一张（会话 `8a148ce4220e04e8`）。
AREA_ARCHIVE_TOKENS = [
    "QoJwaKUq55apnjOnss7zOWNRlxEYGtOwNhLF7L2RQlY",
    "DSWlTNHVFPuaed7s2tWFToy4_a6sGrTrm_JQJ9iKbPk",
    "YEMnWySkWUnUCjbtmeG2YyCNnuZgD_3Zh-v9FsEAaXc",
]


def _pending(card: dict) -> PendingConfirmation:
    """把一张卡片原文包成投影结果（形状与 `pending_confirmations()` 的产物一致）。"""
    return PendingConfirmation(
        tool_name="area_archive",
        message="即将停用区域，确认后立即生效。",
        confirmation=card,
    )


def test_pending_confirmation_is_extracted_with_its_one_time_token() -> None:
    """提取的判据是 `kind == "confirmation_required"`，不是"回复里提到了确认"。"""
    pending = pending_confirmations([TOOL_CALL_AREA_ARCHIVE, TOOL_RESULT_AREA_ARCHIVE])

    assert len(pending) == 1
    assert pending[0].tool_name == "area_archive"
    assert pending[0].message == "即将停用区域，确认后立即生效。"
    assert pending[0].confirmation["id"] == 24
    # 令牌是这张卡唯一能被确认的凭据，且只签发这一次 —— 必须原样带出来。
    assert pending[0].confirmation["token"] == AREA_ARCHIVE_TOKENS[0]


def test_turn_result_hands_the_confirmation_card_to_the_browser() -> None:
    """这条就是"卡片弹不出来"的守卫：`kind` 与**令牌**都必须到浏览器。"""
    events = [TOOL_CALL_AREA_ARCHIVE, TOOL_RESULT_AREA_ARCHIVE]
    turn = HarnessTurn(
        reply="草稿状态的区域有 3 个：测试北区、QA 测试北区、带路径参数。",
        session_id="conv-1",
        events=events,
        pending=pending_confirmations(events),
    )

    result = turn_result(turn, None)

    assert result["kind"] == "confirmation_required"
    assert result["conversation_id"] == "conv-1"
    assert result["confirmation"]["id"] == 24
    assert result["confirmation"]["token"] == AREA_ARCHIVE_TOKENS[0]
    # 正文优先用模型这一轮的话：卡片自己说不出"为什么是这几个对象"。
    assert result["message"] == "草稿状态的区域有 3 个：测试北区、QA 测试北区、带路径参数。"


def test_every_card_of_a_multi_card_turn_reaches_the_browser() -> None:
    """一张卡都不能丢：丢掉的那张**无法补发**（令牌只出现一次）。

    `confirmation`（单数）仍是第一张 —— 按 §3 ③ 原文写的客户端行为不变；
    全部卡片在附加字段 `confirmations` 里。
    """
    first = pending_confirmations([TOOL_CALL_AREA_ARCHIVE, TOOL_RESULT_AREA_ARCHIVE])[0]
    turn = HarnessTurn(
        reply="",
        session_id="conv-1",
        pending=[first, _pending(CARD_AREA_ARCHIVE_2), _pending(CARD_AREA_ARCHIVE_3)],
    )

    result = turn_result(turn, None)

    assert result["kind"] == "confirmation_required"
    assert result["confirmation"]["id"] == 24
    assert [card["token"] for card in result["confirmations"]] == AREA_ARCHIVE_TOKENS
    # 模型没说话时退回网关渲染的那句（两处都不编造，只做选择）。
    assert result["message"] == "即将停用区域，确认后立即生效。"


def test_executed_turn_also_delivers_its_pending_cards() -> None:
    """混合轮次既要触发写入刷新，也不能丢掉一次性确认令牌。"""
    from yuxin.bootstrap import load_all
    from yuxin.kernel.capability import REGISTRY

    load_all()
    events = [TOOL_CALL_AREA_ARCHIVE, TOOL_RESULT_AREA_ARCHIVE]
    turn = HarnessTurn(
        reply="已创建塘口，并准备停用一个区域。",
        session_id="conv-1",
        executed=[ExecutedToolCall(tool_name="pond_create", resource_id=999237)],
        pending=pending_confirmations(events),
    )

    result = turn_result(turn, REGISTRY)

    assert result["kind"] == "executed"
    assert result["confirmation"]["id"] == 24
    assert result["confirmation"]["token"] == AREA_ARCHIVE_TOKENS[0]
    assert [card["token"] for card in result["confirmations"]] == [AREA_ARCHIVE_TOKENS[0]]


# ---------------------------------------------------------------------------
# ⑥ 反问卡片（`ask_user`）同理：问题、选项、自由作答三样都要到浏览器
# ---------------------------------------------------------------------------

TOOL_CALL_ASK_USER = {
    "type": "tool/call",
    "seq": 100,
    "time": 1789286106342,
    "data": {
        "turn": 1,
        "step": 1,
        "callId": "call_00_rkpNowl9IG0KwYcokj5F9843",
        "name": "ask_user",
        "arguments": "{\"question\": \"请补充新建往来单位所需的信息：1）类型是「供应商」还是「客户」；2）单位名称；3）单位编号。\", \"options\": [\"供应商\", \"客户\"]}"
    }
}
TOOL_RESULT_ASK_USER = {
    "type": "tool/result",
    "seq": 101,
    "time": 1789286106348,
    "data": {
        "turn": 1,
        "step": 1,
        "message": {
            "source": {
                "kind": "tool",
                "callId": "call_00_rkpNowl9IG0KwYcokj5F9843"
            },
            "content": [
                {
                    "type": "tool-result",
                    "toolCallId": "call_00_rkpNowl9IG0KwYcokj5F9843",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "{\"kind\":\"clarification\",\"question\":\"请补充新建往来单位所需的信息：1）类型是「供应商」还是「客户」；2）单位名称；3）单位编号。\",\"options\":[\""
                                "供应商\",\"客户\"],\"allow_free_text\":true,\"note\":\"已向用户提问，请立即停止当前迭代输出。\"}"
                            )
                        }
                    ],
                    "isError": False
                }
            ],
            "role": "user",
            "id": "2679193f-392d-4c04-8a20-88cf9b2696b4"
        }
    },
    "sourceEventSeqs": [
        100
    ],
    "surfaceOp": "append"
}

ASK_USER_REPLY = "新建往来单位需要先补充几项必要信息，麻烦告知：…"


def test_clarification_question_is_extracted_without_the_model_note() -> None:
    question = clarification_question([TOOL_CALL_ASK_USER, TOOL_RESULT_ASK_USER])

    assert question is not None
    assert question["question"].startswith("请补充新建往来单位所需的信息")
    assert question["options"] == ["供应商", "客户"]
    assert question["allow_free_text"] is True
    # `note` 是插件给**模型**的控制指令（"请立即停止当前迭代输出"），不是给用户看的文案：
    # 原样透传会让界面上出现系统话术。
    assert "note" not in question


def test_turn_result_hands_the_clarification_card_to_the_browser() -> None:
    events = [TOOL_CALL_ASK_USER, TOOL_RESULT_ASK_USER]
    turn = HarnessTurn(
        reply=ASK_USER_REPLY,
        session_id="conv-1",
        events=events,
        clarification=clarification_question(events),
    )

    result = turn_result(turn, None)

    assert result["kind"] == "clarification"
    assert result["options"] == ["供应商", "客户"]
    assert result["allow_free_text"] is True
    # 模型这一轮的上下文不在 `question` 里（实测那句话带着"（可选补充：联系人…）"），
    # 所以单独用 `message` 带着走，不能丢。
    assert result["message"] == ASK_USER_REPLY


def test_a_plain_answer_still_comes_back_as_assistant() -> None:
    """反例：没有任何"产物"的一轮必须仍是 `assistant`，不能被新分支吃掉。"""
    turn = HarnessTurn(reply="3 号塘最近 7 天共投喂 210kg，日均 30kg。", session_id="conv-1")

    assert turn_result(turn, None) == {
        "kind": "assistant",
        "conversation_id": "conv-1",
        "message": "3 号塘最近 7 天共投喂 210kg，日均 30kg。",
    }
