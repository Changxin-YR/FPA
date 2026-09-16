"""幂等键：同一键的重复请求必须只产生一次业务写入。

早期版本的实现（`早期版本 common/governance/idempotency.py`）有三个问题：

    1. **三个独立事务**——预留（:61）、执行业务（:96）、回写结果（:102）各开一条连接。
       中间崩溃会留下永久 ``processing`` 记录，源码注释自己承认 "operator recovery can
       mark failed"，也就是**只能靠运维手工收拾**。
    2. **存整个响应体**——``idempotency_keys.response_json`` 可能 200 KB。
    3. **回放快照而非回放事实**——回放的是当时序列化的 JSON，与业务表当前状态可能不一致
       （业务表后来被改过，回放却给旧数据）。

新系统的做法：

    a. 表里只存 ``resource_type`` / ``resource_id``，回放时**从业务表读实际数据**；
    b. **``completed`` 只在业务事务提交成功之后才写**——这个顺序是关键：
       只要留了 completed，就一定有一次真实提交发生过；
    c. 崩溃留下的 ``processing`` **不被静默重试**，而是返回 409 让调用方显式处理，
       避免"以为没做、其实做了"的重复写入。

关于原子性的诚实说明：下面分三步（预留 / 业务提交 / 标记完成），而不是一条事务。
原因是幂等记账必须能表达"预留了但业务还没提交"这个**中间态**——如果两者共用一条事务，
这个中间态在物理上不存在，而恰恰是它让并发请求能正确串行化。

三步顺序被刻意设计成 **fail-safe**：

    * 崩溃在第 2 步之前        → 留下 processing（拒绝重复，安全）
    * 崩溃在第 2 步之后、第 3 步之前 → 业务已提交，但幂等记录仍是 processing
                                  → 调用方收到 409 而不是"成功"，**不会误以为需要重试**（安全）
    * 第 3 步之后              → completed，回放生效

三种崩溃点都**不会产生重复写入**，代价是可能要求人工介入的 409——这比静默重复写业务数据好。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from yuxin.kernel.errors import DomainError, ErrorCode, validation
from yuxin.kernel.uow import UnitOfWork

#: 键的格式：8–128 个 URL 安全字符。前端 `crypto.randomUUID()` 与手填键都能通过。
KEY_PATTERN = re.compile(r"\A[A-Za-z0-9._:-]{8,128}\Z")

#: 记录保留时长。过期后同一键可被重新使用。
DEFAULT_TTL_MINUTES = 30


def canonical_payload(payload: Any) -> str:
    """请求体的规范化形态，用于 ``request_hash``。

    规范化必须**确定**：语义相同的请求体必须得到同一个哈希，否则同一键会被误判为
    "不同请求"而返回 409。早期版本用了相同的规范化方式（`sort_keys=True` +
    `separators=(",", ":")`）——那一处设计是对的，继承。
    """
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()


def validate_key(key: str) -> str:
    if not isinstance(key, str) or not KEY_PATTERN.match(key):
        raise validation("Idempotency-Key 格式无效（需 8–128 个字母、数字或 . _ : -）")
    return key


def key_hash(key: str) -> str:
    return hashlib.sha256(validate_key(key).encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _expiry(now: datetime) -> datetime:
    return now + timedelta(minutes=DEFAULT_TTL_MINUTES)


@dataclass(frozen=True, slots=True)
class Reservation:
    """一次预留结果。

    ``replayed_resource`` 非空表示这是重复请求，调用方应**从业务表读实际数据**返回，
    而不是回放快照。
    """

    key_hash: str
    request_hash: str
    replayed_resource: tuple[str, int] | None = None

    @property
    def is_replay(self) -> bool:
        return self.replayed_resource is not None


class IdempotencyStore:
    """幂等记账。

    **它自己管理连接**——这与 `AuditWriter` 恰好相反，原因值得写清楚：

        AuditWriter  ← 必须与业务**同**事务，否则会留下"业务回滚了但审计说成功"的假记录
        Idempotency  ← 必须与业务**跨**事务，否则无法表达"已预留、业务未提交"的中间态

    两个相反的选择都是对的，取决于各自要保证什么。
    """

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    # -- 预留 -----------------------------------------------------------------

    def reserve(
        self, *, user_id: int, capability: str, key: str, payload: Any
    ) -> Reservation:
        """预留一个键。

        并发安全由唯一键 ``uq_idempotency_scope (user_id, capability, key_hash)`` 保证：
        两个并发请求同时到达时，只有一个能 INSERT 成功，另一个会在 ``FOR UPDATE`` 上
        等到第一个提交，然后读到那条记录。
        """
        normalized = validate_key(key)
        digest = payload_hash(payload)
        key_digest = key_hash(normalized)
        now = _utcnow()

        uow = self._uow_factory()
        with uow.begin() as tx:
            existing = tx.query_one(
                """
                SELECT request_hash, status, resource_type, resource_id, result_code, expires_at
                FROM idempotency_keys
                WHERE user_id=%s AND capability=%s AND key_hash=%s
                FOR UPDATE
                """,
                (user_id, capability, key_digest),
            )
            if existing is None:
                tx.execute(
                    """
                    INSERT INTO idempotency_keys
                      (user_id, capability, key_hash, request_hash, status, expires_at)
                    VALUES (%s,%s,%s,%s,'processing',%s)
                    """,
                    (user_id, capability, key_digest, digest, _expiry(now)),
                )
                return Reservation(key_hash=key_digest, request_hash=digest)

            return self._reuse_existing(
                tx=tx,
                existing=existing,
                user_id=user_id,
                capability=capability,
                key_digest=key_digest,
                incoming_hash=digest,
                now=now,
            )

    def _reuse_existing(
        self,
        *,
        tx: Any,
        existing: dict[str, Any],
        user_id: int,
        capability: str,
        key_digest: str,
        incoming_hash: str,
        now: datetime,
    ) -> Reservation:
        # 同一个键配不同的请求体是**调用方的 bug**，必须显式报错而不是猜。
        if str(existing["request_hash"]) != incoming_hash:
            raise DomainError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "同一个请求标识被用于了不同的请求内容，请勿复用",
            )

        status = str(existing["status"])
        if status == "failed" and str(existing.get("result_code") or "") == "COMMIT_UNKNOWN":
            raise DomainError(
                ErrorCode.IDEMPOTENCY_IN_PROGRESS,
                "业务可能已经提交但幂等状态未知，请核对业务数据后再决定是否重试",
                data={"result_code": "COMMIT_UNKNOWN"},
            )
        expires_at = existing.get("expires_at")
        expired = expires_at is not None and expires_at <= now

        if status == "completed" and not expired:
            resource_type = existing.get("resource_type")
            resource_id = existing.get("resource_id")
            if resource_type is None or resource_id is None:
                # 理论上不可达（completed 必然带 resource）。真出现说明数据被改坏了，
                # 此时**不能**报成功，也不能回放——只能拒绝，让调用方人工核对。
                raise DomainError(
                    ErrorCode.IDEMPOTENCY_IN_PROGRESS,
                    "该请求此前已处理但记录不完整，请核对业务数据后再决定是否重试",
                )
            return Reservation(
                key_hash=key_digest,
                request_hash=incoming_hash,
                replayed_resource=(str(resource_type), int(resource_id)),
            )

        if status == "processing" and not expired:
            # 不静默重试：此刻无法判断前一次是"还没提交"还是"已提交但没标记完成"。
            # 静默重试会在这两种情况下产生不同后果，而其中一种是重复写入。
            raise DomainError(
                ErrorCode.IDEMPOTENCY_IN_PROGRESS,
                "相同请求正在处理中，请勿重复提交",
            )

        # 过期（无论此前是 completed 还是 processing）或此前 failed → 允许重新使用
        tx.execute(
            """
            UPDATE idempotency_keys
            SET status='processing', resource_type=NULL, resource_id=NULL,
                result_code=NULL, expires_at=%s
            WHERE user_id=%s AND capability=%s AND key_hash=%s
            """,
            (_expiry(now), user_id, capability, key_digest),
        )
        return Reservation(key_hash=key_digest, request_hash=incoming_hash)

    # -- 收口 -----------------------------------------------------------------

    def mark_completed(
        self,
        *,
        user_id: int,
        capability: str,
        key_digest: str,
        resource_type: str,
        resource_id: int,
        result_code: str = "OK",
    ) -> None:
        """在业务事务**提交成功之后**调用。

        顺序不可调换：先提交业务、再标记完成。反过来的话，业务提交失败会留下
        "completed"记录，后续重复请求会被回放成一个**从未发生**的操作。
        """
        uow = self._uow_factory()
        with uow.begin() as tx:
            tx.execute(
                """
                UPDATE idempotency_keys
                SET status='completed', resource_type=%s, resource_id=%s,
                    result_code=%s, expires_at=%s
                WHERE user_id=%s AND capability=%s AND key_hash=%s
                """,
                (resource_type, resource_id, result_code, _expiry(_utcnow()), user_id, capability, key_digest),
            )

    def mark_failed(
        self, *, user_id: int, capability: str, key_digest: str, reason: str = ""
    ) -> None:
        uow = self._uow_factory()
        with uow.begin() as tx:
            tx.execute(
                """
                UPDATE idempotency_keys
                SET status='failed', result_code=%s, expires_at=%s
                WHERE user_id=%s AND capability=%s AND key_hash=%s
                """,
                (reason[:64] or "FAILED", _utcnow(), user_id, capability, key_digest),
            )

    def mark_commit_unknown(
        self, *, user_id: int, capability: str, key_digest: str
    ) -> None:
        """记录业务已提交但幂等收口未知，永久禁止自动重试。"""
        uow = self._uow_factory()
        with uow.begin() as tx:
            tx.execute(
                """
                UPDATE idempotency_keys
                SET status='failed', result_code='COMMIT_UNKNOWN'
                WHERE user_id=%s AND capability=%s AND key_hash=%s
                  AND status='processing'
                """,
                (user_id, capability, key_digest),
            )

    # -- 清理 -----------------------------------------------------------------

    def sweep_expired(self) -> int:
        """把过期仍未收口的 ``processing`` 标记为 failed。

        早期版本**没有**任何清理任务（源码注释承认只能靠人工处理），过期记录会永久占着
        唯一键，导致该键再也无法使用。
        """
        uow = self._uow_factory()
        with uow.begin() as tx:
            return tx.execute(
                """
                UPDATE idempotency_keys
                SET status='failed', result_code='EXPIRED'
                WHERE status='processing' AND expires_at <= %s
                """,
                (_utcnow(),),
            )


__all__ = [
    "DEFAULT_TTL_MINUTES",
    "IdempotencyStore",
    "KEY_PATTERN",
    "Reservation",
    "canonical_payload",
    "key_hash",
    "payload_hash",
    "validate_key",
]
