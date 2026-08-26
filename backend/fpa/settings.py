"""配置：全部来自环境变量，构造时校验。

**与早期版本的差别**：旧 `settings.py` 有若干危险默认值（`MYSQL_PASSWORD` 默认空串、
附件根目录默认仓库内相对路径、开发密钥在生产回落）。新系统把"危险默认"改成
"启动即失败"：

    生产环境（APP_ENV=production）缺少任何必需项 → 抛 ConfigError，进程起不来。

这是刻意的取舍：**配置错误应该在部署时暴露，而不是在第一次请求时**。早期版本那种
"能起来但一用就错"的模式，让配置问题伪装成了运行时 bug。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


class ConfigError(ValueError):
    """配置不满足当前环境。构造 `Settings` 时抛出，用于让进程立即失败。"""


def _as_bool(name: str, value: str | None, *, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} 必须是 true/false 之一，收到 {value!r}")


def _as_int(name: str, value: str | None, *, default: int, minimum: int = 0) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，收到 {value!r}") from exc
    if parsed < minimum:
        raise ConfigError(f"{name} 不能小于 {minimum}，收到 {parsed}")
    return parsed


def _required_in_production(name: str, value: str, *, app_env: str) -> str:
    if app_env == "production" and not value:
        raise ConfigError(f"生产环境必须配置 {name}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str
    secret_key: str
    session_cookie_secure: bool
    csrf_cookie_secure: bool

    mysql: dict[str, Any] = field(default_factory=dict)

    # --- Agent ---
    agent_dsh_home: str = ""
    agent_dsh_bin: str = ""
    agent_harness_root: str = ""
    #: Harness 运行时覆盖层（patch 文件，env `AGENT_HARNESS_PATCH`）。
    #:
    #: 为什么需要这个配置项：Harness 子进程只有拿到这份 patch 才会**挂载 FPA 业务工具**
    #: 并禁用内建工具。不给它，子进程就退回 Harness 出厂形态 —— 模型手里没有业务工具，
    #: 却带着能读文件、能发任意 HTTP 的工具，于是它自己去"手工拼 HTTP 打后端网关"
    #: （实测就是模型那句自述：「本会话里我没有挂载 FPA 的业务工具」）。
    #: 空串时按仓库内的 `agent-runtime/cordis.patch.yml` 推导（见 `fpa.harness.session`）。
    agent_harness_patch: str = ""
    #: dsh profile 名（env `AGENT_PROFILE`），默认 `sdk` —— 与 SDK 自己的默认值一致，
    #: 插件包按 `docs/DEVELOPMENT.md` §5.3 装在 `<DSH_HOME>/profiles/sdk/node_modules/@fpa/` 下。
    agent_profile: str = "sdk"
    agent_gateway_url: str = ""
    agent_model_provider: str = "deepseek-official"
    agent_model: str = "deepseek-v4-flash"
    agent_max_tokens: int = 32768
    agent_request_timeout_seconds: int = 90
    agent_context_ttl_seconds: int = 1800
    agent_confirmation_ttl_seconds: int = 300
    agent_pool_size: int = 4

    # --- 其它 ---
    cors_origins: tuple[str, ...] = ()
    trusted_proxy_hops: int = 0
    #: 本进程监听端口（env `PORT`）。
    #:
    #: 为什么放在 Settings 里而不是去问 Flask：Harness 子进程的回调地址需要**一个确定的
    #: 端口**，而 `fpa.wsgi` 听的就是 `PORT`。放在这里，"子进程该往哪回调"与"服务听在哪"
    #: 就是同一个值，不会各自漂移。未配置时用 Flask 默认的 5000。
    port: int = 5000

    #: Harness 运行时载体（env `DSH_RUNTIME_MODE`）：`"node"` = 开发用的 Node 载体。
    #:
    #: 为什么必须显式配而不是"让子进程自然继承"：SDK 默认去找**打包好的运行时可执行文件**
    #: （`…runtime\deepseek-harness-sdk-runtime-win-x64.exe`），而本仓库里的 `deepseek-harness`
    #: 是**源码检出**、没有那个 exe —— 于是启动 Harness 时抛
    #: `FileNotFoundError: deepseek-harness-runtime-bin is missing the runtime executable`，
    #: 上层看到的是 502/`AGENT_PROTOCOL_ERROR`。实测踩过：110 秒内一个字节都不返回。
    #: 默认给 `"node"`：本项目的部署形态就是源码检出 + Node 载体（见 `agent-runtime/bin/run.cmd`）；
    #: 真要换成打包运行时，把它设成对应取值即可。
    runtime_mode: str = "node"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        values = dict(os.environ if env is None else env)
        app_env = (values.get("APP_ENV") or "development").strip().lower()
        if app_env not in {"development", "test", "production"}:
            raise ConfigError(
                f"APP_ENV 只能是 development/test/production，收到 {app_env!r}"
            )

        secret_key = (values.get("SECRET_KEY") or "").strip()
        _required_in_production("SECRET_KEY", secret_key, app_env=app_env)
        if not secret_key:
            # 开发环境给一个固定值而不是每次随机：随机会让重启后所有会话失效，
            # 表现为"改完代码就得重新登录"，容易被误认为会话有 bug。
            secret_key = "dev-only-insecure-secret-change-me"

        # 生产环境必须安全 Cookie；开发环境默认不安全，否则本地 http 下浏览器不存
        # Cookie，表现为"登录成功但下个请求 401"。
        secure_default = app_env == "production"
        session_secure = _as_bool(
            "SESSION_COOKIE_SECURE", values.get("SESSION_COOKIE_SECURE"), default=secure_default
        )
        if app_env == "production" and not session_secure:
            raise ConfigError("生产环境 SESSION_COOKIE_SECURE 必须为 true")

        mysql_password = values.get("MYSQL_PASSWORD") or ""
        _required_in_production("MYSQL_PASSWORD", mysql_password, app_env=app_env)

        mysql = {
            "host": (values.get("MYSQL_HOST") or "127.0.0.1").strip(),
            "port": _as_int("MYSQL_PORT", values.get("MYSQL_PORT"), default=3306, minimum=1),
            "user": (values.get("MYSQL_USER") or "fpa").strip(),
            "password": mysql_password,
            "database": (values.get("MYSQL_DATABASE") or "fpa").strip(),
        }
        if app_env == "production" and not mysql["database"]:
            raise ConfigError("生产环境必须配置 MYSQL_DATABASE")

        dsh_home = (values.get("AGENT_DSH_HOME") or "").strip()
        if app_env == "production" and not dsh_home:
            raise ConfigError(
                "生产环境必须配置 AGENT_DSH_HOME（SDK 刻意不发现 ~/.dsh，必须显式给出）"
            )

        origins = tuple(
            item.strip()
            for item in (values.get("FPA_CORS_ORIGINS") or "").split(",")
            if item.strip()
        )

        return cls(
            app_env=app_env,
            secret_key=secret_key,
            session_cookie_secure=session_secure,
            csrf_cookie_secure=session_secure,
            mysql=mysql,
            agent_dsh_home=dsh_home,
            agent_dsh_bin=(values.get("AGENT_DSH_BIN") or "").strip(),
            agent_harness_root=(values.get("AGENT_HARNESS_ROOT") or "").strip(),
            agent_harness_patch=(values.get("AGENT_HARNESS_PATCH") or "").strip(),
            agent_profile=(values.get("AGENT_PROFILE") or "sdk").strip() or "sdk",
            agent_gateway_url=(values.get("AGENT_GATEWAY_URL") or "").strip(),
            agent_model_provider=(values.get("AGENT_MODEL_PROVIDER") or "deepseek-official").strip(),
            agent_model=(values.get("AGENT_MODEL") or "deepseek-v4-flash").strip(),
            agent_max_tokens=_as_int(
                "AGENT_MAX_TOKENS", values.get("AGENT_MAX_TOKENS"), default=32768, minimum=1
            ),
            agent_request_timeout_seconds=_as_int(
                "AGENT_REQUEST_TIMEOUT_SECONDS",
                values.get("AGENT_REQUEST_TIMEOUT_SECONDS"),
                default=90,
                minimum=1,
            ),
            agent_context_ttl_seconds=_as_int(
                "AGENT_CONTEXT_TTL_SECONDS",
                values.get("AGENT_CONTEXT_TTL_SECONDS"),
                default=1800,
                minimum=1,
            ),
            agent_confirmation_ttl_seconds=_as_int(
                "AGENT_CONFIRMATION_TTL_SECONDS",
                values.get("AGENT_CONFIRMATION_TTL_SECONDS"),
                default=300,
                minimum=1,
            ),
            agent_pool_size=_as_int(
                "AGENT_POOL_SIZE", values.get("AGENT_POOL_SIZE"), default=4, minimum=1
            ),
            cors_origins=origins,
            trusted_proxy_hops=_as_int(
                "TRUSTED_PROXY_HOPS", values.get("TRUSTED_PROXY_HOPS"), default=0
            ),
            port=_as_int("PORT", values.get("PORT"), default=5000, minimum=1),
            runtime_mode=(values.get("DSH_RUNTIME_MODE") or "node").strip() or "node",
        )


__all__ = ["ConfigError", "Settings"]
