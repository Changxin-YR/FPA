"""审计写入器：与业务写入在**同一个事务**内。

早期版本的审计是独立事务（`mysql_store.audit_event()` 自己开一个 `with self.transaction()`），
后果是业务回滚后审计仍留下"成功"记录——而审计的全部价值恰恰在于"它记的就是发生过的"。

新系统规则：
    * 审计写入复用调用方的 ``tx``，**不自己开连接**；
    * 业务回滚 → 审计一起消失；
    * 因此审计表里出现的每一条 ``success`` 都对应一次真实提交。
    这条规则是 `docs/WRITE_CONTRACT.md` 规则 5 的实现。

脱敏：``_SENSITIVE_KEYS`` 里的字段递归替换成 ``[REDACTED]``。早期版本有同样的机制
（`早期版本 agent_gateway_policy.py:_redact`），继承并集中到一处。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fpa.kernel.uow import UnitOfWork

#: 命中这些子串的**键名**会被脱敏（大小写不敏感）。
SENSITIVE_KEY_MARKERS = frozenset(
    {
        "password",
        "passwd",
        "token",
        "cookie",
        "authorization",
        "secret",
        "credential",
        "csrf",
        "api_key",
        "apikey",
        "session",
        "attachment",
    }
)

#: 单个 JSON 字段的上限，防止有人把整个响应体塞进审计。
MAX_JSON_BYTES = 16_384


def redact(value: Any) -> Any:
    """递归脱敏。"""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        return json.dumps(
            {"_truncated": True, "_bytes": len(text.encode("utf-8"))},
            ensure_ascii=False,
        )
    return text


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """一次可审计的操作。字段与 `audit_logs` 表一一对应。"""

    capability: str
    domain: str
    object_type: str = ""
    object_id: int | None = None
    object_ref: str = ""
    result: str = "success"  # success | failure | denied | pending
    reason: str = ""
    permission: str = ""
    before: Any = None
    after: Any = None
    detail: Any = None
    ip_address: str = ""


class AuditWriter:
    """把事件写进 `audit_logs`。"""

    def write(
        self,
        tx: UnitOfWork,
        event: AuditEvent,
        *,
        request_id: str,
        user_id: int | None,
        username: str,
        is_agent: bool = False,
        conversation_id: str | None = None,
        confirmation_id: int | None = None,
    ) -> int:
        tx.execute(
            """
            INSERT INTO audit_logs
              (request_id, user_id, username, is_agent, conversation_id, confirmation_id,
               capability, domain, permission, object_type, object_id, object_ref,
               result, reason, before_json, after_json, detail_json, ip_address)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                request_id,
                user_id,
                username,
                int(is_agent),
                conversation_id,
                confirmation_id,
                event.capability,
                event.domain,
                event.permission,
                event.object_type,
                event.object_id,
                event.object_ref,
                event.result,
                event.reason,
                _dump(redact(event.before)),
                _dump(redact(event.after)),
                _dump(redact(event.detail)),
                event.ip_address,
            ),
        )
        return tx.last_insert_id()


__all__ = ["AuditEvent", "AuditWriter", "MAX_JSON_BYTES", "SENSITIVE_KEY_MARKERS", "redact"]
