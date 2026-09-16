from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from yuxin.kernel.errors import DomainError, ErrorCode
from yuxin.kernel.idempotency import IdempotencyStore, Reservation
from yuxin.kernel.runner import ActorView, CapabilityRunner, Invocation


class _Tx:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: Any = None) -> int:
        self.statements.append((sql, tuple(params or ())))
        return 1


class _Uow:
    def __init__(self) -> None:
        self.tx = _Tx()

    @contextmanager
    def begin(self):
        yield self.tx


def test_commit_unknown_reservation_cannot_be_retried() -> None:
    store = IdempotencyStore(lambda: _Uow())

    with pytest.raises(DomainError) as caught:
        store._reuse_existing(
            tx=_Tx(),
            existing={
                "request_hash": "same-request",
                "status": "failed",
                "result_code": "COMMIT_UNKNOWN",
                "resource_type": None,
                "resource_id": None,
                "expires_at": None,
            },
            user_id=7,
            capability="pond.update",
            key_digest="key-digest",
            incoming_hash="same-request",
            now=datetime.now(timezone.utc).replace(tzinfo=None),
        )

    assert caught.value.code == ErrorCode.IDEMPOTENCY_IN_PROGRESS
    assert caught.value.data == {"result_code": "COMMIT_UNKNOWN"}


class _FailingCompletionIdempotency:
    def __init__(self) -> None:
        self.commit_unknown: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []

    def reserve(self, **_kwargs: Any) -> Reservation:
        return Reservation(key_hash="key", request_hash="hash")

    def mark_completed(self, **_kwargs: Any) -> None:
        raise RuntimeError("completion database unavailable")

    def mark_commit_unknown(self, **kwargs: Any) -> None:
        self.commit_unknown.append(kwargs)

    def mark_failed(self, **kwargs: Any) -> None:
        self.failed.append(kwargs)


def test_runner_does_not_turn_unknown_commit_into_retryable_failure() -> None:
    idempotency = _FailingCompletionIdempotency()
    runner = CapabilityRunner.__new__(CapabilityRunner)
    runner._idempotency = idempotency  # type: ignore[attr-defined]
    runner._uow_factory = lambda: _Uow()  # type: ignore[attr-defined]
    runner._audit_failure = lambda *_args: None  # type: ignore[attr-defined]
    runner._maybe_load_before = lambda *_args: None  # type: ignore[attr-defined]
    runner._call_service = lambda *_args: SimpleNamespace(data={}, resource_id=41)  # type: ignore[attr-defined]
    runner._reload_after = lambda *_args: {"id": 41}  # type: ignore[attr-defined]
    runner._write_audit = lambda *_args: None  # type: ignore[attr-defined]

    spec = SimpleNamespace(
        name="pond.update",
        domain="master_data",
        resource="",
        is_read=False,
        invariants=(),
    )
    actor = ActorView(user_id=7, username="worker", permissions=frozenset())

    with pytest.raises(DomainError) as caught:
        runner._invoke_idempotent(  # type: ignore[attr-defined]
            spec=spec,
            invocation=Invocation(
                capability_name="pond.update",
                payload={},
                raw_payload={},
                idempotency_key="same-key",
            ),
            actor=actor,
            request_id="req-commit-unknown",
            scope=SimpleNamespace(),
            cleaned={},
        )

    assert caught.value.code == ErrorCode.IDEMPOTENCY_IN_PROGRESS
    assert idempotency.commit_unknown
    assert not idempotency.failed
