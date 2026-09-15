"""真实 Agent HTTP 契约验收。

本文件不 mock Flask、Harness、业务工具或数据库。缺少真实前置条件时明确标记
BLOCKED（pytest skipped），不能把服务不可用当成 PASS。
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import pytest

from fpa.web.routes_agent import issue_context_token


BASE_URL = os.environ.get("FPA_BACKEND_URL", "http://127.0.0.1:5101").rstrip("/")
DEMO_USER = os.environ.get("FPA_DEMO_USER", "demo")
DEMO_PASSWORD = os.environ.get("FPA_DEMO_PASSWORD", "Demo1234!")
LOW_PRIV_USER = os.environ.get("FPA_LOW_PRIV_USER", "")
LOW_PRIV_PASSWORD = os.environ.get("FPA_LOW_PRIV_PASSWORD", "")


class Blocked(RuntimeError):
    pass


@dataclass
class LiveClient:
    opener: urllib.request.OpenerDirector
    user: dict[str, Any]
    session_token: str

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any] | list[dict[str, Any]] | str]:
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(
            BASE_URL + path,
            data=payload,
            method=method,
            headers={
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if payload is not None else {}),
                **(headers or {}),
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                text = response.read().decode("utf-8")
                return response.status, _decode(text)
        except urllib.error.HTTPError as error:
            text = error.read().decode("utf-8", errors="replace")
            return error.code, _decode(text)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise Blocked(f"BLOCKED: 后端不可用 {BASE_URL}: {error}") from error


def _decode(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _data(payload: Any) -> Any:
    return payload.get("data") if isinstance(payload, dict) else None


def _code(payload: Any) -> str:
    return str(payload.get("code") or "") if isinstance(payload, dict) else ""


def _blocked(reason: str) -> None:
    pytest.skip(f"BLOCKED: {reason}")


def _login(username: str, password: str) -> LiveClient:
    jar = http.cookiejar.CookieJar()
    client = LiveClient(urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), {}, "")
    try:
        status, payload = client.request(
            "/api/v1/auth/login",
            method="POST",
            body={"identifier": username, "password": password},
        )
    except Blocked as error:
        _blocked(str(error))
    if status != 200:
        _blocked(f"真实账号 {username!r} 登录失败 status={status} code={_code(payload)}")
    user_payload = _data(payload)
    if not isinstance(user_payload, dict) or not isinstance(user_payload.get("user"), dict):
        pytest.fail(f"登录响应缺少 data.user: {payload!r}")
    user = user_payload["user"]
    token = next((cookie.value for cookie in jar if cookie.name == "fpa_session"), "")
    if not token:
        pytest.fail("登录成功但没有 fpa_session Cookie")
    return LiveClient(client.opener, user, token)


@pytest.fixture(scope="module")
def live() -> LiveClient:
    return _login(DEMO_USER, DEMO_PASSWORD)


def _csrf(live: LiveClient) -> str:
    status, payload = live.request("/api/v1/auth/csrf")
    if status != 200:
        pytest.fail(f"真实 CSRF 端点失败 status={status}: {payload!r}")
    token = _data(payload)
    if not isinstance(token, dict) or not token.get("csrf_token"):
        pytest.fail(f"CSRF 响应不符合契约: {payload!r}")
    return str(token["csrf_token"])


def _stream(live: LiveClient, message: str) -> list[dict[str, Any]]:
    token = _csrf(live)
    request = urllib.request.Request(
        BASE_URL + "/api/v1/agent/turns/stream",
        data=json.dumps({"message": message}, ensure_ascii=False).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/x-ndjson",
            "X-CSRF-Token": token,
        },
    )
    try:
        with live.opener.open(request, timeout=240) as response:
            if response.status in {502, 503, 504}:
                _blocked(f"Agent 服务不可用 status={response.status}")
            if response.status != 200:
                pytest.fail(f"stream status={response.status}: {response.read()!r}")
            rows = [json.loads(line) for line in response.read().decode().splitlines() if line.strip()]
    except urllib.error.HTTPError as error:
        if error.code in {502, 503, 504}:
            _blocked(f"Agent 服务不可用 status={error.code}")
        pytest.fail(f"stream HTTP {error.code}: {error.read()!r}")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        _blocked(f"Agent stream 不可用: {error}")
    if not rows:
        pytest.fail("stream 没有返回任何 NDJSON 行")
    return rows


def _agent_token(live: LiveClient, conversation_id: str) -> str:
    secret = os.environ.get("SECRET_KEY", "dev-only-insecure-secret-change-me")
    return issue_context_token(
        secret=secret,
        user_id=int(live.user["id"]),
        session_hash=hashlib.sha256(live.session_token.encode()).hexdigest(),
        capability="",
        conversation_id=conversation_id,
        ttl_seconds=300,
    )


def _tool_call(
    live: LiveClient,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    conversation_id: str,
) -> tuple[int, Any]:
    return live.request(
        f"/api/v1/agent/tools/{tool_name}/call",
        method="POST",
        body={"arguments": arguments},
        headers={"X-Agent-Context": _agent_token(live, conversation_id)},
    )


def _pond_id_and_area(live: LiveClient) -> tuple[int, int]:
    for record_status in ("draft", "submitted"):
        status, payload = live.request(
            f"/api/v1/ponds?status={record_status}&page=1&page_size=1"
        )
        if status != 200:
            pytest.fail(f"无法读取真实塘口 status={status}: {payload!r}")
        items = (_data(payload) or {}).get("items", [])
        if items:
            pond = items[0]
            return int(pond["id"]), int(pond["area_id"])
    pytest.fail("没有可编辑的真实塘口，无法执行确认/取消回读验收")


def _db_row(pond_id: int) -> dict[str, Any] | None:
    import pymysql

    from fpa.kernel.uow_factory import connection_config

    with pymysql.connect(**connection_config().as_kwargs()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, name, row_version FROM ponds WHERE id=%s", (pond_id,))
            return cursor.fetchone()


def _restore_pond_name(live: LiveClient, pond_id: int, name: str) -> None:
    current = _db_row(pond_id)
    if current is None or current["name"] == name:
        return
    status, payload = live.request(
        f"/api/v1/ponds/{pond_id}",
        method="PATCH",
        body={"name": name, "expected_version": current["row_version"]},
        headers={
            "X-CSRF-Token": _csrf(live),
            "Idempotency-Key": f"qa-restore-{time.time_ns()}",
        },
    )
    assert status == 200, f"验收塘口恢复失败: {payload!r}"


def test_agent_stream_delivers_status_then_result(live: LiveClient) -> None:
    rows = _stream(live, "请查询当前系统的塘口数量，只用一句中文回答，不要写入任何数据。")
    assert rows[0]["type"] == "status"
    assert rows[-1]["type"] == "result"
    result = rows[-1]["result"]
    assert result["kind"] in {"assistant", "executed"}
    assert result["message"].strip()


def test_agent_rejects_empty_message(live: LiveClient) -> None:
    token = _csrf(live)
    status, payload = live.request(
        "/api/v1/agent/turns",
        method="POST",
        body={"message": ""},
        headers={"X-CSRF-Token": token},
    )
    assert status == 400
    assert _code(payload) == "VALIDATION_ERROR"


def test_agent_confirmation_cancel_does_not_change_database(live: LiveClient) -> None:
    pond_id, _area_id = _pond_id_and_area(live)
    before = _db_row(pond_id)
    assert before is not None
    status, payload = _tool_call(
        live,
        "pond_update",
        {"pond_id": pond_id, "name": f"QA-cancel-{time.time_ns()}", "expected_version": before["row_version"]},
        conversation_id=f"qa-cancel-{time.time_ns()}",
    )
    if status in {502, 503, 504}:
        _blocked(f"Agent 工具网关不可用 status={status}")
    assert status == 200, payload
    pending = _data(payload)
    assert pending["kind"] == "confirmation_required"
    card = pending["confirmation"]
    cancel_status, cancel_payload = live.request(
        f"/api/v1/agent/confirmations/{card['id']}/cancel",
        method="POST",
        body={},
        headers={"X-CSRF-Token": _csrf(live)},
    )
    assert cancel_status == 200, cancel_payload
    assert _data(cancel_payload)["kind"] == "cancelled"
    assert _db_row(pond_id) == before


def test_agent_confirmation_executes_and_database_reads_back(live: LiveClient) -> None:
    pond_id, _area_id = _pond_id_and_area(live)
    before = _db_row(pond_id)
    assert before is not None
    new_name = f"QA-confirm-{time.time_ns()}"
    try:
        status, payload = _tool_call(
            live,
            "pond_update",
            {"pond_id": pond_id, "name": new_name, "expected_version": before["row_version"]},
            conversation_id=f"qa-confirm-{time.time_ns()}",
        )
        if status in {502, 503, 504}:
            _blocked(f"Agent 工具网关不可用 status={status}")
        assert status == 200, payload
        card = _data(payload)["confirmation"]
        confirm_status, confirm_payload = live.request(
            f"/api/v1/agent/confirmations/{card['id']}/confirm",
            method="POST",
            body={"token": card["token"]},
            headers={"X-CSRF-Token": _csrf(live)},
        )
        assert confirm_status == 200, confirm_payload
        confirmed = _data(confirm_payload)
        assert confirmed["kind"] == "executed"
        assert confirmed["conversation_id"]
        assert confirmed["result"]["capability"] == "pond.update"
        assert confirmed["result"]["resource"] == "pond"
        assert confirmed["result"]["resource_id"] == pond_id
        assert confirmed["result"]["data"]["executed"] == [
            {"capability": "pond.update", "resource": "pond", "resource_id": pond_id}
        ]
        after = _db_row(pond_id)
        assert after is not None
        assert after["name"] == new_name
        assert int(after["row_version"]) > int(before["row_version"])
    finally:
        _restore_pond_name(live, pond_id, str(before["name"]))

    restored = _db_row(pond_id)
    assert restored is not None
    assert restored["name"] == before["name"]


def test_agent_tool_filtering_rejects_a_real_low_privilege_user() -> None:
    if not LOW_PRIV_USER or not LOW_PRIV_PASSWORD:
        _blocked("未配置 FPA_LOW_PRIV_USER/FPA_LOW_PRIV_PASSWORD")
    weak = _login(LOW_PRIV_USER, LOW_PRIV_PASSWORD)
    conversation_id = f"qa-denied-{time.time_ns()}"
    status, payload = weak.request(
        "/api/v1/agent/tools",
        headers={"X-Agent-Context": _agent_token(weak, conversation_id)},
    )
    if status in {502, 503, 504}:
        _blocked(f"Agent 工具网关不可用 status={status}")
    assert status == 200, payload
    tools = (_data(payload) or {}).get("tools", [])
    names = {str(item.get("name")) for item in tools}
    assert "pond_create" not in names
    status, payload = _tool_call(
        weak,
        "pond_create",
        {"code": f"QA-denied-{time.time_ns()}", "name": "不应创建", "area_id": 1},
        conversation_id=conversation_id,
    )
    assert status == 403, payload
    assert _code(payload) in {"FORBIDDEN", "PERMISSION_DENIED"}
