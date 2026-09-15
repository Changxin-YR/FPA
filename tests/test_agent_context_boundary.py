from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from fpa.harness import session as harness_session
from fpa.harness.session import HarnessSessionManager
from fpa.settings import Settings
from fpa.web.routes_agent import issue_context_token, register_agent_routes
from fpa.kernel.errors import DomainError, ErrorCode


def _settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        {
            "APP_ENV": "test",
            "AGENT_DSH_HOME": str(tmp_path),
            "SECRET_KEY": "test-secret",
        }
    )


def test_harness_session_cache_isolated_by_authenticated_actor(tmp_path: Path) -> None:
    manager = HarnessSessionManager(_settings(tmp_path))
    sessions: list[object] = []
    manager._create_session = lambda **_: sessions.append(object()) or sessions[-1]  # type: ignore[method-assign]

    first = manager._get_or_create(
        (1, "sid-a", "conversation"), gateway_url="http://gateway", context_token="token-a"
    )
    second = manager._get_or_create(
        (2, "sid-b", "conversation"), gateway_url="http://gateway", context_token="token-b"
    )

    assert first is not second
    assert len(sessions) == 2


def test_agent_prompt_does_not_contain_context_token(tmp_path: Path) -> None:
    manager = HarnessSessionManager(_settings(tmp_path))

    rendered = manager._render_prompt("查询库存\n\n[当前页面] 库存")

    assert "secret-token" not in rendered
    assert "X-Agent-Context" not in rendered
    assert "查询库存" in rendered


def test_context_token_uid_must_match_session_user(tmp_path: Path) -> None:
    from flask import Flask

    app = Flask(__name__)
    app.testing = True
    app.config.update(SECRET_KEY="test-secret", FPA_ACCESS=None, FPA_AGENT_GATEWAY=None)

    app.config["FPA_ACCESS"] = SimpleNamespace(
        resolve_by_session_hash=lambda _sid: SimpleNamespace(
            user_id=2,
            username="real-user",
            permissions=frozenset(),
            role_codes=frozenset(),
            session_hash="sid-a",
        )
    )
    app.config["FPA_AGENT_GATEWAY"] = SimpleNamespace(tools_payload=lambda _permissions: {"tools": []})
    register_agent_routes(app)

    token = issue_context_token(
        secret="test-secret",
        user_id=1,
        session_hash="sid-a",
        capability="",
        conversation_id="conversation",
        ttl_seconds=60,
    )

    with pytest.raises(DomainError) as error:
        app.test_client().get("/api/v1/agent/tools", headers={"X-Agent-Context": token})

    assert error.value.code == ErrorCode.AGENT_CONTEXT_INVALID


def test_missing_runtime_patch_is_not_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(harness_session, "_RUNTIME_PATCH_RELPATH", ("missing", "patch.yml"))
    monkeypatch.setattr(harness_session, "_INSTALLED_PATCH_RELPATH", ("missing", "patch.yml"))

    with pytest.raises(DomainError) as error:
        harness_session.resolve_harness_patch(settings)

    assert error.value.code == ErrorCode.AGENT_UNAVAILABLE


def test_runtime_launcher_does_not_fallback_to_legacy_backend_path() -> None:
    launcher = Path(__file__).parents[1] / "agent-runtime" / "bin" / "run.cmd"
    text = launcher.read_text(encoding="utf-8")

    assert "Desktop\\FPA\\deepseek-harness" not in text
    assert "AGENT_HARNESS_ROOT" in text or "deepseek-harness-master" in text


def test_runtime_launcher_supports_a_harness_source_checkout() -> None:
    launcher = Path(__file__).parents[1] / "agent-runtime" / "bin" / "run.cmd"

    assert "apps\\cli\\lib\\bin.js" in launcher.read_text(encoding="utf-8")


def test_runtime_launcher_uses_source_when_configured_node_runtime_is_missing(tmp_path: Path) -> None:
    launcher = Path(__file__).parents[1] / "agent-runtime" / "bin" / "run.cmd"
    source_entry = tmp_path / "apps" / "cli" / "lib" / "bin.js"
    source_entry.parent.mkdir(parents=True)
    source_entry.write_text("console.log('source-runtime-ok')", encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "AGENT_HARNESS_ROOT": str(tmp_path),
            "DSH_NODE_RUNTIME": str(tmp_path / "missing-runtime"),
        }
    )
    result = subprocess.run(
        ["cmd", "/d", "/c", str(launcher)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "source-runtime-ok" in result.stdout
