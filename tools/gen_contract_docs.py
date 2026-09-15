"""从内核生成契约文档里的"事实表"，并同步前端类型。

**为什么需要这个工具**：`INTERFACES.md` 是手工维护的，而它有若干段内容是内核事实的
投影——错误码表、字段类型枚举、tone 枚举。手工同步必然漂移，事实上已经漂移了：
frontend-recon 指出文档列了 17 个错误码，而内核有 23 个；文档把版本冲突写成
`CONFLICT`，内核实际抛 `VERSION_CONFLICT`。

这与本项目要根除的早期版本病根是**同一类问题**：同一件事有多个来源。区别只是这次
是"文档 vs 代码"而不是"前端 vs 后端"。

所以：**把手写段落换成生成段落**，并用 CI 断言生成物与仓库中的文件逐字节一致。
生成区用标记包裹，标记之外的内容（叙述、理由、示例）仍是手工的——那些是**人的判断**，
不该被生成覆盖。

用法::

    python tools/gen_contract_docs.py            # 写入
    python tools/gen_contract_docs.py --check    # 只校验（CI 用）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fpa.kernel.errors import ErrorCode, _HTTP_STATUS  # noqa: E402
from fpa.kernel.fields import FieldType  # noqa: E402
from fpa.kernel.workflow import RowAction, Tone  # noqa: E402

INTERFACES = ROOT / "docs" / "INTERFACES.md"

BEGIN = "<!-- BEGIN GENERATED: {name} -->"
END = "<!-- END GENERATED: {name} -->"

#: 给每个错误码一句中文说明。**这是人的判断，不是可从代码推导的**，
#: 所以留在本工具里显式维护，而不是试图从错误码名字生成。
ERROR_NOTES: dict[str, str] = {
    "VALIDATION_ERROR": "请求内容未通过 schema 校验",
    "FIELD_INVALID": "具体字段值非法；`data.field` 给出字段名",
    "NOT_FOUND": "资源不存在",
    "CONFLICT": "业务冲突（重复编码、被引用无法删除等）",
    "UNAUTHENTICATED": "未登录或会话过期",
    "FORBIDDEN": "权限不足；`data.required_permission` 给出所需权限码",
    "DATA_SCOPE_UNRESOLVED": "**数据范围无法解析——fail closed，不返回空集**",
    "DATA_SCOPE_DENIED": "目标资源不在当前账号的数据范围内",
    "CSRF_INVALID": "CSRF 令牌缺失或无效",
    "RATE_LIMITED": "触发限流；响应带 `Retry-After`",
    "IDEMPOTENCY_IN_PROGRESS": "同键请求正在处理中（含前次崩溃未收口的情况）",
    "IDEMPOTENCY_CONFLICT": "同一 Idempotency-Key 被用于不同请求内容",
    "CONFIRMATION_INVALID": "确认令牌无效、过期、已使用，或参数与确认时不一致",
    "HUMAN_ONLY": "该能力禁止智能体执行，只能由本人在页面完成",
    "VERSION_CONFLICT": "乐观锁冲突；`data.current_version` 给出当前版本",
    "CAPABILITY_NOT_FOUND": "能力未注册（Agent 的固定业务路由就靠它拒绝）",
    "TOOL_NOT_FOUND": "请求的工具不在当前账号可用的工具清单内",
    "AGENT_CONTEXT_INVALID": "Agent 上下文令牌无效或已过期",
    "AGENT_UNAVAILABLE": "Harness 运行时不可用",
    "AGENT_TIMEOUT": "单轮超时",
    "AGENT_PROTOCOL_ERROR": "Harness 返回的协议消息不符合约定",
    "INTERNAL_ERROR": "未预期错误；必须带 `request_id` 供排查",
    "SERVICE_UNAVAILABLE": "依赖服务暂时不可用",
}


def render_error_codes() -> str:
    rows = ["| code | HTTP | 含义 |", "|---|---:|---|"]
    for member in ErrorCode:
        status = _HTTP_STATUS[member]
        note = ERROR_NOTES.get(member.value, "（待补说明）")
        rows.append(f"| `{member.value}` | {status} | {note} |")
    rows.append("")
    rows.append(
        f"共 **{len(list(ErrorCode))}** 个错误码。这张表由 "
        "`tools/gen_contract_docs.py` 从 `backend/fpa/kernel/errors.py` 生成，"
        "**不要手工编辑**——CI 会校验它与内核逐字节一致。"
    )
    return "\n".join(rows)


def render_field_types() -> str:
    lines = [
        "| 类型 | 前端控件 | JSON Schema 类型 |",
        "|---|---|---|",
    ]
    controls = {
        "string": "单行文本",
        "text": "多行文本",
        "integer": "整数输入",
        "number": "小数输入（金额/数量）",
        "boolean": "开关",
        "date": "日期选择",
        "datetime": "日期时间选择",
        "enum": "下拉（选项来自 `choices`）",
        "ref": "下拉（选项来自 `ref.list_path`）",
        # 多选控件：元素类型由 `Field.items` 给出（`integer` / `string`），
        # 而**不是**由控件猜。这与 `ref` 用 `ref.list_path` 是同一个手法：
        # 让声明决定渲染，而不是让渲染去猜声明。
        "array": "多选（元素类型来自 `items`）",
    }
    json_types = {
        "string": "string",
        "text": "string",
        "integer": "integer",
        "number": "number",
        "boolean": "boolean",
        "date": "string",
        "datetime": "string",
        "enum": "string",
        "ref": "integer",
        "array": "array",
    }
    for member in FieldType:
        key = member.value
        lines.append(f"| `{key}` | {controls.get(key, '?')} | `{json_types.get(key, '?')}` |")
    lines.append("")
    lines.append(
        f"共 **{len(list(FieldType))}** 种字段类型。同样由生成器产出。"
    )
    return "\n".join(lines)


def render_tone_and_actions() -> str:
    tones = "、".join(f"`{t.value}`" for t in Tone)
    actions = "、".join(f"`{a.value}`" for a in RowAction)
    return (
        f"**`Tone`（状态配色语义，只有 {len(list(Tone))} 个值）**：{tones}\n\n"
        f"**`RowAction`（行内动作）**：{actions}\n\n"
        "前端 tone 色表的键**只能**是上述 5 个值之一。早期版本用中文标签当色表键，"
        "跨文件复用后静默全灰且测试抓不到（`早期版本 returnModel.ts:43`）——"
        "现在未知状态必须显式降级为 `neutral`，**不允许是空串**。\n\n"
        "`row_actions` 由资源状态机派生（各状态动作的并集），是**静态上限**；"
        "某一行实际能做什么仍由服务端按该行状态算出的 `allowed_actions` 决定。"
    )


SECTIONS = {
    "error-codes": render_error_codes,
    "field-types": render_field_types,
    "tone-and-actions": render_tone_and_actions,
}


def replace_section(text: str, name: str, body: str) -> str:
    begin = BEGIN.format(name=name)
    end = END.format(name=name)
    start = text.find(begin)
    if start == -1:
        raise SystemExit(f"文档里缺少生成区标记：{begin}")
    stop = text.find(end, start)
    if stop == -1:
        raise SystemExit(f"文档里缺少生成区结束标记：{end}")
    return text[: start + len(begin)] + "\n" + body + "\n" + text[stop:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成契约文档的事实表")
    parser.add_argument("--check", action="store_true", help="只校验，不写入")
    args = parser.parse_args(argv)

    original = INTERFACES.read_text(encoding="utf-8")
    updated = original
    for name, renderer in SECTIONS.items():
        updated = replace_section(updated, name, renderer())

    if args.check:
        if updated != original:
            print("契约文档的生成区已过期。请运行：python tools/gen_contract_docs.py")
            # 指出是哪一段变了
            for name in SECTIONS:
                probe = replace_section(original, name, SECTIONS[name]())
                if probe != original:
                    print(f"  过期段落：{name}")
                    break
            return 1
        print(f"校验通过：{len(SECTIONS)} 个生成区与内核一致")
        return 0

    if updated == original:
        print("无需更新")
        return 0
    INTERFACES.write_text(updated, encoding="utf-8", newline="\n")
    print(f"已更新 {INTERFACES.relative_to(ROOT)} 的 {len(SECTIONS)} 个生成区")
    return 0


if __name__ == "__main__":
    sys.exit(main())
