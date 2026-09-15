"""「智能体写入 → 列表自动刷新」的**服务端判据**守卫（t14）。

## 这一组防的是什么

用户报「智能体新增数据依旧要刷新后才显示」。根因不在前端事件、也不在缓存：
**智能体写入与页面列表之间没有任何连接** —— 写入走
`Harness 子进程 → /api/v1/agent/tools/<n>/call → CapabilityRunner`（后端内部），
而列表只在挂载/切资源/自己提交后重拉。

要接上这条线，服务端必须先能回答一个问题：**这一轮到底写了没有、写的是哪个资源。**
本文件钉的就是这个答案。

## 判据为什么从事件流里取，而不是从别处

* 回复文本里出现"已创建"**不算** —— 模型说过"我没有创建成功"，文本判据会被骗；
* `tool/result` 的内容是网关按 `docs/WRITE_CONTRACT.md` 返回的判别联合，
  其中 **只有 `executed` 表示已提交事务**（`executed` 只能由一次已提交的事务产生）；
* 工具名要从同一步的 `tool/call` 事件配对取得，因为 `tool/result` 里只有
  `toolCallId`，没有工具名 —— 而这个配对是"写的是哪个资源"的唯一来源。

## 夹具为什么逐字取自真实会话日志

`.dsh-home/sessions/**` 里的真实事件（会话 `54c07ba0918d1152` 的 `pond_create`、
`bc5377306f9d080f` 的 `area_list` + `ask_user`）。自己编的形状只能证明
"我认识自己的假设"，证明不了"真实链路是这么长的"—— 本项目已经栽过一次
（夹具注册表盖住生产路径）。
"""

from __future__ import annotations

import json

import pytest

from fpa.harness.session import executed_tool_calls, pending_confirmations
from fpa.web.agent_turn import _frontend_url_of, _tool_name_index, executed_result


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    """`tool/call` 事件的真实形状（`arguments` 是 JSON **字符串**，不是对象）。"""
    return {
        "type": "tool/call",
        "seq": 1,
        "data": {"turn": 1, "step": 1, "callId": call_id, "name": name, "arguments": json.dumps(arguments)},
    }


def _tool_result(call_id: str, payload: dict) -> dict:
    """`tool/result` 事件的真实形状（三层嵌套：message.content[0].content[0].text）。"""
    return {
        "type": "tool/result",
        "seq": 2,
        "data": {
            "turn": 1,
            "step": 1,
            "message": {
                "source": {"kind": "tool", "callId": call_id},
                "content": [
                    {
                        "type": "tool-result",
                        "toolCallId": call_id,
                        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                        "isError": False,
                    }
                ],
                "role": "user",
                "id": "m-1",
            },
        },
    }


#: 真实会话 `54c07ba0918d1152` 里那次成功的塘口创建（`resource_id` 与库里一致）。
EXECUTED_POND_CREATE = [
    _tool_call("call_x1", "pond_create", {"area_id": 1, "code": "T11-TOOL-001", "name": "t11 工具探针塘"}),
    _tool_result(
        "call_x1",
        {
            "kind": "executed",
            "message": "已创建塘口「t11 工具探针塘」（编号 T11-TOOL-001）",
            "resource_id": 999237,
            "data": {"record": {"id": 999237, "code": "T11-TOOL-001"}},
        },
    ),
]

#: 真实会话里那轮**纯查询**（`area_list` 命中后模型反问，什么都没写）。
READ_ONLY_TURN = [
    _tool_call("call_r1", "area_list", {"page": 1, "page_size": 100}),
    _tool_result("call_r1", {"kind": "read", "message": "", "data": {"items": [], "has_next": False}}),
    _tool_call("call_r2", "ask_user", {"question": "请补充信息"}),
    _tool_result(
        "call_r2",
        {"kind": "clarification", "question": "请补充信息", "options": [], "allow_free_text": True},
    ),
]


@pytest.fixture
def registry():
    """真实组合根注册表（不是夹具注册表——夹具会盖住生产路径）。"""
    from fpa.bootstrap import load_all

    load_all()
    from fpa.kernel.capability import REGISTRY

    return REGISTRY


# ---------------------------------------------------------------------------
# 判据：只有 executed 算写入
# ---------------------------------------------------------------------------


def test_executed_tool_call_is_detected_with_tool_name() -> None:
    calls = executed_tool_calls(EXECUTED_POND_CREATE)

    assert len(calls) == 1
    assert calls[0].tool_name == "pond_create"
    assert calls[0].resource_id == 999237


@pytest.mark.parametrize(
    "kind, payload",
    [
        ("read", {"kind": "read", "message": "", "data": {}}),
        ("clarification", {"kind": "clarification", "question": "?", "options": []}),
        (
            "confirmation_required",
            {"kind": "confirmation_required", "message": "待确认", "confirmation": {"id": 1}},
        ),
        ("failed", {"kind": "failed", "code": "VALIDATION_ERROR", "message": "字段非法"}),
    ],
)
def test_non_executed_kinds_are_not_writes(kind: str, payload: dict) -> None:
    """`read` / `clarification` / `confirmation_required` / `failed` 一个都不算写入。

    `confirmation_required` 尤其重要：那张卡片代表"**还没写**"，
    若把它当成写入，用户会在什么都没发生时看到列表乱跳。
    """
    events = [_tool_call("call_y1", "pond_create", {}), _tool_result("call_y1", payload)]

    assert executed_tool_calls(events) == [], f"{kind} 不该被判定为写入"


def test_read_only_turn_has_no_writes() -> None:
    assert executed_tool_calls(READ_ONLY_TURN) == []


def test_only_confirmation_required_becomes_a_pending_card() -> None:
    """反例方向：其余三种 kind **不得**产生待确认卡片。

    `executed` 与 `confirmation_required` 在协议上互斥（`WRITE_CONTRACT` 规则 1），
    所以两个投影必须给出相反答案：把写成功的轮次也渲染出一张卡，用户会去"确认"
    一件已经做完的事；把纯查询/反问渲染成卡，他会确认一件根本不存在的事。
    """
    assert pending_confirmations(EXECUTED_POND_CREATE) == []
    assert pending_confirmations(READ_ONLY_TURN) == []


def test_multiple_writes_are_all_reported_in_order() -> None:
    """一次对话真的会写多次（实测：先建往来单位、再建批次）—— 一条都不能漏。"""
    events = [
        _tool_call("a", "partner_create", {"code": "0265"}),
        _tool_result("a", {"kind": "executed", "message": "已新建客户", "resource_id": 9273}),
        _tool_call("b", "batch_create", {"code": "B-1"}),
        _tool_result("b", {"kind": "executed", "message": "已新建批次", "resource_id": 500}),
    ]

    calls = executed_tool_calls(events)

    assert [c.tool_name for c in calls] == ["partner_create", "batch_create"]
    assert [c.resource_id for c in calls] == [9273, 500]


def test_result_without_matching_call_is_skipped() -> None:
    """配不到工具名的结果要**跳过**：说不清是哪个能力写的，前端也就无从判断该刷哪个页。"""
    assert executed_tool_calls([_tool_result("orphan", {"kind": "executed", "resource_id": 1})]) == []


def test_malformed_payload_does_not_raise() -> None:
    """畸形文本一律跳过，**绝不**把整轮对话变成错误（历史上有过"解析时静默丢字段"）。"""
    broken = _tool_result("c", {"kind": "executed"})
    broken["data"]["message"]["content"][0]["content"][0]["text"] = "{ 不是 JSON"
    events = [_tool_call("c", "pond_create", {}), broken]

    assert executed_tool_calls(events) == []


# ---------------------------------------------------------------------------
# 翻译成 §3 的 executed 结果
# ---------------------------------------------------------------------------


def test_executed_result_carries_capability_and_resource(registry) -> None:
    """工具名必须反查成**能力名与资源名**（前端不自己反推）。"""
    result = executed_result(executed_tool_calls(EXECUTED_POND_CREATE), registry)

    assert result is not None
    assert result["capability"] == "pond.create"
    assert result["resource"] == "pond"
    assert result["resource_id"] == 999237
    assert result["url"] == "/ponds", "前端列表路径要能从资源读能力的 path 推出来"
    assert result["data"]["executed"] == [
        {"capability": "pond.create", "resource": "pond", "resource_id": 999237}
    ]


def test_executed_result_is_none_for_read_only_turn(registry) -> None:
    assert executed_result(executed_tool_calls(READ_ONLY_TURN), registry) is None


def test_multi_write_keeps_every_resource_but_last_one_on_top(registry) -> None:
    """顶层只带一条（最后一条），但**全部**资源都在 `data.executed` 里。

    刷新需要的是资源集合，不是"最后一个是谁"——契约只能带一条，
    所以多写的情形靠这个字段不失真。
    """
    events = [
        _tool_call("a", "partner_create", {}),
        _tool_result("a", {"kind": "executed", "resource_id": 1}),
        _tool_call("b", "pond_create", {}),
        _tool_result("b", {"kind": "executed", "resource_id": 2}),
    ]

    result = executed_result(executed_tool_calls(events), registry)

    assert result is not None
    assert result["capability"] == "pond.create", "顶层取最后一次写入"
    assert [row["resource"] for row in result["data"]["executed"]] == ["partner", "pond"]


def test_tool_name_to_capability_uses_the_single_mapping(registry) -> None:
    """反查必须走 `Capability.tool_name()`，**不能**把 `_` 换回 `.` 自己反推。

    实测（真实组合根）两处反推都会错：

      * `pond_status_change.request` → 工具名 `pond_status_change_request`
        反推会得到不存在的 `pond.status.change.request`；
      * 更关键的是**资源名**：这条能力的 `resource` 是 **`pond`**，不是
        `pond_status_change`。而 `sales_order.approve` 的资源确是 `sales_order`。
        两者没有可计算的字符串关系 —— 所以 `resource` 必须由服务端查表给出。
    """
    index = _tool_name_index(registry)

    assert "pond_status_change_request" in index
    capability = index["pond_status_change_request"]
    assert capability.name == "pond_status_change.request"
    # 反推的名字根本不在注册表里 —— 这就是"第二处描述同一件事"的代价。
    assert "pond.status.change.request" not in {c.name for c in registry.all()}
    # 而资源名必须来自能力自己的声明（这里是 `pond`，与能力名前缀不同）。
    assert capability.resource == "pond"


def test_resource_without_a_list_page_gets_empty_url(registry) -> None:
    """没有列表页的资源**不编路径**（编出来的链接点了就 404）。"""
    assert _frontend_url_of(registry, "不存在的资源") == ""
