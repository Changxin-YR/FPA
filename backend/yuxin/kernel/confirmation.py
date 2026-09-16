"""人工确认闸门：一次性令牌 + 六元组绑定。

对应需求的"先准备、后确认、再执行"。早期版本有这套机制（`agent_confirmations` 表、
`token_hash`、`session_hash` 绑定），设计方向是对的，继承并收紧。

**为什么确认是"操作安全流程"而不是"禁止 Agent 做这个操作"**：
确认之后仍由 Agent 完成业务操作。`confirmation_required` 与 `executed` 是协议层
互斥的两个分支——要么写了，要么什么都没写（见 `docs/WRITE_CONTRACT.md`）。

绑定六元组（缺一不可）：

    user_id        — 换个人不能用
    session_hash   — 换个会话不能用（会话轮换后旧令牌失效）
    capability     — 换个能力不能用
    params_hash    — **参数被改过不能用**（这是最关键的一环）
    target_ref     — 目标对象（用于展示与审计，不参与校验）
    expires_at     — 过期不能用
    used_at IS NULL — 第二次使用必须失败（一次性）

`params_hash` 覆盖**客户端提交的原始参数**（裁决 Q11）：服务端补全值（`farm_id` 等）
在签发卡片时还不存在，无法预知；而且"用户看到什么就确认什么"要求哈希覆盖的正是
卡片上渲染的那一份数据。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from yuxin.kernel.capability import Capability
from yuxin.kernel.errors import DomainError, ErrorCode
from yuxin.kernel.idempotency import canonical_payload
from yuxin.kernel.scope import Scope
from yuxin.kernel.uow import RequestContext, UnitOfWork

#: 确认令牌的默认有效期（秒）。够用户读完卡片并点确认，又不至于长期挂着。
DEFAULT_TTL_SECONDS = 300


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def params_hash(payload: dict[str, Any]) -> str:
    """参数指纹。

    用与幂等同一套规范化（`canonical_payload`）：排序键、紧凑分隔符、不转义中文。
    两处用同一个函数是刻意的——如果这里自己写一套，就会出现"幂等认为相同、确认
    认为不同"的诡异不一致。
    """
    return _sha256(canonical_payload(payload))


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    """一次待确认操作。"""

    id: int
    capability: str
    params_hash: str
    payload: dict[str, Any]
    target_ref: str
    expires_at: datetime
    conversation_id: str = ""


@dataclass(frozen=True, slots=True)
class IssuedConfirmation:
    """签发给前端的确认卡片。``token`` 只在这一次响应里出现。"""

    id: int
    token: str
    capability: str
    title: str
    target: str
    rows: list[dict[str, str]]
    impact: list[str]
    expires_at: str

    def to_meta(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "token": self.token,
            "capability": self.capability,
            "title": self.title,
            "target": self.target,
            "rows": self.rows,
            "impact": self.impact,
            "expires_at": self.expires_at,
        }


class ConfirmationStore:
    """`agent_confirmations` 表的读写。**自己管理连接**。

    它与 `AuditWriter`（必须同事务）相反：确认是**跨请求**的长期状态，
    天然不能绑在某一次业务事务上。这个选择与 `IdempotencyStore` 同理。
    """

    def __init__(self, uow_factory: Any) -> None:
        self._uow_factory = uow_factory

    def create(
        self,
        *,
        token_digest: str,
        user_id: int,
        session_hash: str,
        conversation_id: str,
        capability: str,
        params_hash_value: str,
        payload: dict[str, Any],
        target_ref: str,
        summary: str,
        ttl_seconds: int,
        idempotency_key: str | None,
    ) -> int:
        import json

        uow = self._uow_factory()
        with uow.begin() as tx:
            tx.execute(
                """
                INSERT INTO agent_confirmations
                  (token_hash, user_id, session_hash, conversation_id, capability,
                   params_hash, payload_json, target_ref, summary, status,
                   idempotency_key, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s)
                """,
                (
                    token_digest,
                    user_id,
                    session_hash,
                    conversation_id,
                    capability,
                    params_hash_value,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                    target_ref,
                    summary[:255],
                    idempotency_key,
                    _utcnow() + timedelta(seconds=ttl_seconds),
                ),
            )
            return tx.last_insert_id()

    def find_pending(
        self, *, token_digest: str, user_id: int, session_hash: str
    ) -> PendingConfirmation | None:
        import json

        uow = self._uow_factory()
        with uow.begin() as tx:
            row = tx.query_one(
                """
                SELECT id, capability, params_hash, payload_json, target_ref, expires_at,
                       conversation_id, status, used_at
                FROM agent_confirmations
                WHERE token_hash=%s AND user_id=%s AND session_hash=%s
                """,
                (token_digest, user_id, session_hash),
            )
        if row is None:
            return None
        if str(row["status"]) != "pending" or row["used_at"] is not None:
            return None
        if row["expires_at"] <= _utcnow():
            return None
        payload = json.loads(row["payload_json"])
        return PendingConfirmation(
            id=int(row["id"]),
            capability=str(row["capability"]),
            params_hash=str(row["params_hash"]),
            payload=payload if isinstance(payload, dict) else {},
            target_ref=str(row["target_ref"]),
            expires_at=row["expires_at"],
            conversation_id=str(row.get("conversation_id") or ""),
        )

    def consume(
        self, *, confirmation_id: int, user_id: int, new_status: str = "confirmed"
    ) -> bool:
        """把 pending 原子地改成终态。

        ``WHERE status='pending'`` 是**一次性**的保证：两个并发确认请求中，
        只有第一个能改到这行，第二个影响 0 行 → 返回 False → 上层报 409。

        这正是早期版本缺的那一步：它先 `find` 再 `claim`，两个查询之间没有原子性，
        但它的 `claim` 里带了 `status='pending'` 条件，所以实际上是对的——
        这个写法继承。
        """
        uow = self._uow_factory()
        with uow.begin() as tx:
            affected = tx.execute(
                """
                UPDATE agent_confirmations
                SET status=%s, used_at=UTC_TIMESTAMP(), failure_reason=NULL
                WHERE id=%s AND user_id=%s AND status='pending' AND used_at IS NULL
                  AND expires_at > UTC_TIMESTAMP()
                """,
                (new_status, confirmation_id, user_id),
            )
        return affected > 0

    def mark_failed(self, *, confirmation_id: int, user_id: int, reason: str) -> None:
        uow = self._uow_factory()
        with uow.begin() as tx:
            tx.execute(
                """
                UPDATE agent_confirmations
                SET status='failed', used_at=%s, failure_reason=%s
                WHERE id=%s AND user_id=%s
                  AND status IN ('pending', 'confirmed') AND used_at IS NOT NULL
                """,
                (_utcnow(), reason[:255], confirmation_id, user_id),
            )

    def mark_expired(self, *, user_id: int | None = None) -> list[int]:
        """把过期的 pending 收口为 expired。

        早期版本把它放在"对话入口"做惰性清理（因为它没有调度器）。新系统也保留惰性清理，
        但**同时提供后台任务入口**（`sweep`），这样即使没人发起对话，过期记录也会被收口。
        """
        uow = self._uow_factory()
        with uow.begin() as tx:
            params: list[Any] = [_utcnow()]
            sql = (
                "SELECT id FROM agent_confirmations "
                "WHERE status='pending' AND expires_at <= %s"
            )
            if user_id is not None:
                sql += " AND user_id=%s"
                params.append(user_id)
            rows = tx.query_all(sql, params)
            ids = [int(row["id"]) for row in rows]
            if ids:
                tx.execute(
                    "UPDATE agent_confirmations SET status='expired' "
                    "WHERE status='pending' AND expires_at <= %s"
                    + (" AND user_id=%s" if user_id is not None else ""),
                    params,
                )
        return ids


class ConfirmationGate:
    """签发与消费确认令牌。实现 `kernel/runner.py` 的 `ConfirmationGate` 协议。"""

    def __init__(self, store: ConfirmationStore, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._store = store
        self._ttl = ttl_seconds

    # -- 签发 -----------------------------------------------------------------

    def issue(
        self,
        *,
        capability: Capability,
        actor: Any,
        payload: dict[str, Any],
        raw_payload: dict[str, Any],
        request_id: str,
        summary: str,
        target: str,
        impact: list[str],
        row_labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """签发确认卡片。

        注意入参里的 ``raw_payload`` 参与哈希、``payload`` 只落库供执行时使用：
        两者在"客户端原始参数"这个口径下通常是同一份，但显式分开让语义清楚——
        未来若加了服务端补全，也不会不小心把补全值算进哈希。
        """
        token = secrets.token_urlsafe(32)
        digest = params_hash(raw_payload)
        confirmation_id = self._store.create(
            token_digest=_sha256(token),
            user_id=actor.user_id,
            session_hash=actor.session_hash or "",
            conversation_id=actor.conversation_id or "",
            capability=capability.name,
            params_hash_value=digest,
            payload=payload,
            target_ref=target,
            summary=summary,
            ttl_seconds=self._ttl,
            idempotency_key=None,
        )
        issued = IssuedConfirmation(
            id=confirmation_id,
            token=token,
            capability=capability.name,
            title=capability.title,
            target=target,
            rows=self._rows(capability, raw_payload, row_labels),
            impact=impact,
            expires_at=(_utcnow() + timedelta(seconds=self._ttl)).isoformat() + "Z",
        )
        return issued.to_meta()

    @staticmethod
    def _render_value(value: Any) -> str:
        """把一个字段值渲染成人能读的文本。

        为什么要单独一个函数：`str([9])` 得到的是 Python 的列表字面量 `'[9]'` ——
        提权卡上"角色 = [9]"就是这么来的，用户读到的是**一种编程语言的语法**而不是
        "质检核验员"。同理 `{'a': 1}` / `None` 都不该原样出现在业务界面上。
        """
        if isinstance(value, (list, tuple, set)):
            return "、".join(str(item) for item in value) or "—"
        if isinstance(value, dict):
            return "、".join(f"{k}: {v}" for k, v in value.items()) or "—"
        return str(value)

    @staticmethod
    def _rows(
        capability: Capability,
        payload: dict[str, Any],
        row_labels: dict[str, str] | None = None,
    ) -> list[dict[str, str]]:
        """把参数渲染成"填写内容"行，标签用声明里的中文 label。

        这一行的价值：用户看到的是**业务语义**（「数量：50 kg」）而不是 JSON
        （`{"quantity": 50}`）。早期版本做过同样的事（`agent_humanize.change_rows`）。

        ``row_labels`` 是能力**自己**提供的"可读化覆盖"（见
        `Capability.confirmation_labels`）：把裸 id 换成业务名字（`9` → `质检核验员`）。
        没有覆盖的字段退回 `_render_value` —— 所以未声明该钩子的能力行为不变。
        """
        overrides = row_labels or {}
        rows: list[dict[str, str]] = []
        for key, spec in capability.fields.items():
            if key not in payload:
                continue
            value = payload[key]
            if value is None or value == "":
                continue
            label = overrides.get(key)
            rows.append(
                {
                    "label": spec.label,
                    "value": label if label else ConfirmationGate._render_value(value),
                }
            )
        return rows

    # -- 消费 -----------------------------------------------------------------

    def consume(
        self,
        *,
        token: str,
        actor: Any,
        capability_name: str = "",
        expected_id: int | None = None,
    ) -> dict[str, Any]:
        """校验并消费令牌，返回待执行的参数。

        两种定位方式（`capability_name` 或 `expected_id`，至少给一个）：

            * 插件侧：`capability_name` —— 调用方手上本来就有参数，只需确认"这个令牌
              确实是发给这个能力的"
            * 浏览器侧：`expected_id` —— 服务端从库里取参数，少一次往返

        **两种方式都必须用到令牌**。只按 id 是不行的：id 是自增的，可枚举——
        那样攻击者只要猜中一个 id 就能执行别人准备好的高危操作。

        所有失败原因都归一到同一个错误消息，不泄露是过期、归属不符还是参数不匹配，
        避免把确认机制变成探测工具。
        """
        invalid = DomainError(
            ErrorCode.CONFIRMATION_INVALID, "确认令牌无效、已过期或已被使用"
        )
        if not token or not token.strip():
            raise invalid

        pending = self._store.find_pending(
            token_digest=_sha256(token.strip()),
            user_id=actor.user_id,
            session_hash=actor.session_hash or "",
        )
        if pending is None:
            raise invalid
        if expected_id is not None and pending.id != int(expected_id):
            raise invalid
        if capability_name and pending.capability != capability_name:
            raise invalid

        # 原子占位：失败说明有并发确认或已被用过
        if not self._store.consume(confirmation_id=pending.id, user_id=actor.user_id):
            # 原子占位失败 = 有并发确认，或已被用过。这条路径恰好是"一次性"的证明点：
            # `UPDATE ... WHERE status='pending'` 影响 0 行。
            raise invalid

        return {
            "confirmation_id": pending.id,
            "capability": pending.capability,
            "payload": pending.payload,
            "params_hash": pending.params_hash,
            "target_ref": pending.target_ref,
            "conversation_id": pending.conversation_id,
        }

    def verify_params(self, pending: dict[str, Any], raw_payload: dict[str, Any]) -> None:
        """确认执行时**再次**比对参数指纹。

        这一条是"用户看到什么就确认什么"的技术保证：如果执行时提交的参数与签发卡片时
        的参数不同，即使令牌本身有效也必须拒绝。少了这一步，确认卡片就成了摆设——
        用户在卡片上确认的是 50kg，执行时可以变成 5000kg。
        """
        if params_hash(raw_payload) != pending.get("params_hash"):
            raise DomainError(
                ErrorCode.CONFIRMATION_INVALID,
                "提交的参数与确认时不一致，请重新发起",
            )


__all__ = [
    "ConfirmationGate",
    "ConfirmationStore",
    "DEFAULT_TTL_SECONDS",
    "IssuedConfirmation",
    "PendingConfirmation",
    "params_hash",
]
