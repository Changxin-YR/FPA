"""最终闭环验证：**模型通过工具真的写进数据库**（注册表来自真实组合根）。

这是整个项目最终要证明的一件事。前面所有自检分别证明了：
    * 内核正确（kernel_smoke）
    * 执行器真实（runner_e2e / agent_e2e）
    * HTTP 层真实（web_e2e）
    * Harness 链路可用（harness_smoke）
    * 真实能力集的行为正确（master_data_e2e）

但它们都是"我构造的调用"。这一份不同：**提示词是由模型自己决定调用哪个工具的**。

## ★ 三份 e2e 的分工（不可互相替代）

| 文件 | 注册表来源 | 证明什么 |
|---|---|---|
| `tools/web_e2e.py` | **手搓夹具**（1 条 `pond.create` + `selftest_ponds` 表） | HTTP 全链路与安全边界，最小、最快、可复现 |
| `tools/master_data_e2e.py` | `yuxin.bootstrap.load_all()` | 真实能力集的行为（权限/范围/不变量/回读），不经过模型 |
| **本文件** | `yuxin.bootstrap.load_all()` | **模型**能否触达真实能力集，并真的写进业务表 `ponds` |

在 t11 之前，本文件用的是 `web_e2e.build_registry()` —— 也就是说"闭环证明"里的注册表是**假的**：
它证明的是"模型能调用一条**夹具**能力、写进**夹具**表 `web_ponds`"，而 master_data 真实声明的
15 条能力在整条闭环里从未被模型触达过。`DEVELOPMENT.md` §1 把它称作"★ 闭环证明"，其中"真实的
MySQL / Flask / Harness / 模型"都对，唯独注册表那一环是夹具——**夹具盖住了生产路径**，
这正是本项目反复要根除的形态（第一次：能力声明无人 import；第二次：回读函数债）。

现在注册表来自真实组合根，本文件因此能证明**四件事同时成立**：
真实注册表 → 真实工具清单 → 模型自主调用 → 真实业务表里有那一行。

## 流程

    0. §0 静态证明：注册表来自 `load_all()`，能力由 `yuxin.domains.*` 声明，夹具模块未被 import
    1. 种探针数据：真实区域 + 一个只有 `pond.create` 权限、范围是那个区域的账号
    2. 起一个真实 Flask 服务（后台线程），装配用**真实注册表**
    3. 登录并为该账号签发上下文令牌（会话级 1800s）
    4. 生成 patch 文件，把网关地址与令牌注入插件
    5. 把插件复制进 Harness 的运行时闭包（伪造"已安装"）
    6. 启动 Harness，发一句自然语言指令
    7. 模型自主调用 `pond_create` 工具 → 插件 → HTTP → 网关 → 执行器 → 真实服务 → `ponds` 表
    8. **直接查库确认那一行真的在**（并确认夹具表里没有）

第 8 步是全部的意义所在：**不是"模型说它建了"，而是"业务表里有那一行"**。

用法::

    python tools/live_agent_e2e.py                 # 完整闭环（需要 DEEPSEEK_API_KEY）
    python tools/live_agent_e2e.py --static-only   # 只跑 §0 的装配来源证明（不需要模型）
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools"))

#: SDK 的 **Python** 包路径（`deepseek_harness` 所在目录）。用 `pip install -e` 装过就不需要设。
#: 与 `tools/harness_smoke.py` 同一个键——两条真模型链路用同一套环境变量，少一个键就少一条链路。
_sdk_env = os.environ.get("DSH_SDK_PYTHONPATH", "").strip()
if _sdk_env:
    sys.path.insert(0, _sdk_env)

import pymysql  # noqa: E402

FAILURES = 0

#: 探针账号（幂等种入）。刻意**不给** `super_admin`：范围必须来自真实的数据范围记录，
#: 否则这条闭环证明的是"超管能写"，而不是"一个受数据范围约束的真实账号能写"。
PROBE_USERNAME = "live_e2e_user"
PROBE_PASSWORD = "LiveE2ePassw0rd!"
PROBE_ROLE = "live-e2e-role"
PROBE_SCOPE = "live-e2e-area"


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


def _machine_env(name: str) -> str:
    """从注册表读机器级环境变量（进程可能看不到后设置的那些）。"""
    if os.name != "nt":
        return ""
    import winreg

    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            r"Environment",
        ):
            try:
                with winreg.OpenKey(root, sub) as handle:
                    value, _ = winreg.QueryValueEx(handle, name)
                if value:
                    return str(value)
            except OSError:
                continue
    return ""


def config():
    from yuxin.kernel.uow import ConnectionConfig

    return ConnectionConfig(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "yuxin"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "yuxin"),
    )


# ---------------------------------------------------------------------------
# §0 装配来源证明（不需要模型，可单独跑）
# ---------------------------------------------------------------------------


def prove_composition_root():
    """证明注册表来自真实组合根，且夹具模块没有参与。

    为什么把这一步**放在最前面**并且单独可跑：它是本文件结论的地基。如果模型那一轮
    失败了，我们仍然要能回答"注册表到底是真的是假的"——否则整条闭环的可信度无法分离。
    """
    print("=== 0. 装配来源证明：注册表来自真实组合根 ===")
    from yuxin.bootstrap import load_all

    registry = load_all()
    spec = registry.find("pond.create")
    check("组合根装载出了 pond.create", spec is not None)
    if spec is None:
        return None, None

    handler_module = str(getattr(spec.handler, "__module__", ""))
    factory_module = str(getattr(spec.service_factory, "__module__", ""))
    check(
        "能力处理器由业务域声明（不是 tools/ 下的夹具）",
        handler_module.startswith("yuxin.domains."),
        f"handler 来自 {handler_module}",
    )
    check(
        "能力服务由业务域声明（service_factory 指向 yuxin.domains.*）",
        factory_module.startswith("yuxin.domains."),
        f"service_factory 来自 {factory_module}",
    )
    check(
        "夹具模块 web_e2e **未被 import**（它一旦参与，能力来源就不再是唯一的）",
        "web_e2e" not in sys.modules,
        "sys.modules 里出现了 web_e2e",
    )

    from yuxin.kernel.workflow import RESOURCES

    pond_resource = RESOURCES.find("pond")
    check("资源声明的表名来自真实域声明（ponds，不是 selftest_ponds）",
          pond_resource is not None and pond_resource.table == "ponds",
          f"table={getattr(pond_resource, 'table', None)}")

    domain_counts: dict[str, int] = {}
    for item in registry.all():
        domain_counts[item.domain] = domain_counts.get(item.domain, 0) + 1
    print(f"        注册表：{len(registry.all())} 条能力，按域 {domain_counts}")
    print(f"        pond.create → {handler_module}.{getattr(spec.handler, '__qualname__', '?')}")
    check(
        "master_data 的能力是**真实那批**（≥ 15 条，含新增的 area/material/partner）",
        domain_counts.get("master_data", 0) >= 15,
        f"master_data 只有 {domain_counts.get('master_data', 0)} 条",
    )
    return registry, spec


# ---------------------------------------------------------------------------
# §1 探针数据
# ---------------------------------------------------------------------------


def prepare_probe_data() -> dict:
    """种入闭环所需的真实数据，返回 `{area_id, area_name, user_id}`。

    全部用**业务表**（`areas` / `users` / `roles` / `permissions` / `data_scopes`），
    幂等：重复运行只更新、不新增。

    为什么不用 `web_e2e.prepare_database()`：它建的是夹具表（`web_ponds`）与夹具账号。
    真实路径需要的是**真实区域 + 一个受数据范围约束的账号**，两件事的形态不同；
    共用会又变成"两处描述同一件事"。
    """
    from yuxin.kernel.password import hash_password

    with config_uow().begin() as tx:
        area = tx.query_one("SELECT id, code, name FROM areas ORDER BY id LIMIT 1")
        if area is None:
            raise SystemExit(
                "areas 表没有数据 —— 先跑 `python tools/migrate.py apply`（003 迁移自包含地种了区域）"
            )
        area_id = int(area["id"])

        tx.execute(
            "INSERT INTO permissions (code, name, domain) VALUES ('pond.create','新建塘口','master_data') "
            "ON DUPLICATE KEY UPDATE name=VALUES(name)"
        )
        tx.execute(
            "INSERT INTO roles (code, name) VALUES (%s,'闭环探针角色') ON DUPLICATE KEY UPDATE name=VALUES(name)",
            (PROBE_ROLE,),
        )
        tx.execute(
            "INSERT IGNORE INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
            "WHERE r.code=%s AND p.code='pond.create'",
            (PROBE_ROLE,),
        )
        tx.execute(
            "INSERT INTO users (username, display_name, password_hash, status) VALUES (%s,%s,%s,'active') "
            "ON DUPLICATE KEY UPDATE password_hash=VALUES(password_hash), status='active'",
            (PROBE_USERNAME, "闭环探针用户", hash_password(PROBE_PASSWORD)),
        )
        user = tx.query_one("SELECT id FROM users WHERE username=%s", (PROBE_USERNAME,))
        user_id = int(user["id"])
        tx.execute(
            "INSERT IGNORE INTO user_roles (user_id, role_id) "
            "SELECT %s, id FROM roles WHERE code=%s",
            (user_id, PROBE_ROLE),
        )
        # 数据范围：**area 型**，指向真实区域。
        # 这条记录是"受约束的真实账号"的载体：RangeResolver 会把它渲染成
        # `area_id IN (...)`，而 `create_pond` 会校验目标区域在这个范围内。
        tx.execute(
            "INSERT INTO data_scopes (code, name, scope_type, area_id, status) "
            "VALUES (%s,'闭环探针范围','area',%s,'active') "
            "ON DUPLICATE KEY UPDATE area_id=VALUES(area_id), status='active'",
            (PROBE_SCOPE, area_id),
        )
        tx.execute(
            "INSERT IGNORE INTO user_data_scopes (user_id, scope_id) "
            "SELECT %s, id FROM data_scopes WHERE code=%s",
            (user_id, PROBE_SCOPE),
        )
    return {"area_id": area_id, "area_name": str(area["name"]), "user_id": user_id}


def config_uow():
    from yuxin.kernel.uow import UnitOfWork

    return UnitOfWork(config())


def _write_publish_manifest(source: Path, target: Path) -> None:
    """把**部署层**运行时 patch 降级成**发布副本**：只保留 `insert` 段。

    为什么必须降级：`agent-runtime/cordis.patch.yml` 是"部署层唯一下发点"，
    它同时带着安全边界（52 条 `disabled: true` 关掉内建工具）与部署人格；
    而装进 `<DSH_HOME>/profiles/<profile>/node_modules/@yuxin/dsh-biz-tools/` 的那一份是
    **发布副本**，只负责"工具源"（`insert`）—— 任何用户装上这个包就能拿到工具。

    原样复制会让同一件事有两处来源，而且**顺序相关**（先跑审计是绿的、先跑本脚本就红）。
    判据：`tools/harness_tools_audit.py` 的第 2 条。

    文本级提取（不 `yaml.safe_load` 再 `dump`）：`config` 里是 `!!js` 自定义标签，
    解析会失败、重序列化会把它丢掉。
    """
    lines = source.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("- insert:"))
    end = start + 1
    while end < len(lines) and lines[end].strip() and not lines[end].startswith("- "):
        end += 1
    block = "\n".join(lines[start:end]).rstrip("\n") + "\n"
    target.write_text(
        "# 发布副本（由 tools/live_agent_e2e.py 生成）：**只含工具源 insert**。\n"
        "# 安全边界（disabled 掉内建工具）与部署人格属于**部署层**的运行时 patch\n"
        "# （agent-runtime/cordis.patch.yml），由 backend/yuxin/harness/session.py 经 patches= 下发。\n"
        + block,
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    global FAILURES

    only_static = "--static-only" in sys.argv

    # dsh_home 在多个阶段都要用（装插件、构造子进程环境），所以在函数顶部定义一次——
    # 放到使用点附近容易漏掉一处（我就漏过一次，报 UnboundLocalError）。
    dsh_home = str(ROOT / ".dsh-home")
    test_code = f"LIVE-{int(time.time()) % 100000}"
    print(f"=== 本次测试用塘口编号：{test_code} ===\n")

    registry, spec = prove_composition_root()
    if registry is None:
        return 1
    if only_static:
        print()
        if FAILURES:
            print(f"结果：{FAILURES} 项失败")
            return 1
        print("结果：装配来源证明通过（未运行模型；去掉 --static-only 可跑完整闭环）")
        return 0

    probe = prepare_probe_data()
    print(f"\n=== 1. 探针数据 ===\n        area_id={probe['area_id']}（{probe['area_name']}），user_id={probe['user_id']}")

    # ------------------------------------------------------------------
    # 2. 组装应用（**真实注册表** + 真实数据范围解析）
    # ------------------------------------------------------------------
    print("\n=== 2. 组装应用 ===")
    from yuxin.agent.gateway import AgentToolGateway
    from yuxin.domains.access.scope_resolver import DataScopeResolver
    from yuxin.domains.access.service import AccessService
    from yuxin.kernel.audit import AuditWriter
    from yuxin.kernel.confirmation import ConfirmationGate, ConfirmationStore
    from yuxin.kernel.idempotency import IdempotencyStore
    from yuxin.kernel.runner import CapabilityRunner
    from yuxin.kernel.uow import UnitOfWork
    from yuxin.settings import Settings
    from yuxin.web.app import create_app
    from yuxin.web.routes_agent import issue_context_token

    def uow_factory() -> UnitOfWork:
        return UnitOfWork(config())

    audit = AuditWriter()
    gate = ConfirmationGate(ConfirmationStore(uow_factory), ttl_seconds=300)
    runner = CapabilityRunner(
        registry=registry,
        uow_factory=uow_factory,
        audit=audit,
        idempotency=IdempotencyStore(uow_factory),
        scope_resolver=DataScopeResolver(uow_factory),
        confirmation_gate=gate,
    )
    access = AccessService(uow_factory)
    gateway = AgentToolGateway(
        registry=registry,
        runner=runner,
        audit=audit,
        confirmation=gate,
        uow_factory=uow_factory,
    )
    settings = Settings.from_env(
        {
            "APP_ENV": "test",
            "SECRET_KEY": "live-e2e-secret",
            "MYSQL_HOST": config().host,
            "MYSQL_PORT": str(config().port),
            "MYSQL_USER": config().user,
            "MYSQL_PASSWORD": config().password,
            "MYSQL_DATABASE": config().database,
        }
    )
    app = create_app(
        registry=registry,
        runner=runner,
        access=access,
        scope_resolver=DataScopeResolver(uow_factory),
        secret_key=settings.secret_key,
    )
    app.config["YUXIN_AGENT_GATEWAY"] = gateway
    check("应用已组装（真实注册表 + 真实 DataScopeResolver）", True)

    # ------------------------------------------------------------------
    # 3. 起真实 HTTP 服务（后台线程）
    # ------------------------------------------------------------------
    print("\n=== 3. 起真实 HTTP 服务 ===")
    from werkzeug.serving import make_server

    port = 5199
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.5)
    check(f"服务已在 127.0.0.1:{port} 监听", thread.is_alive())

    # ------------------------------------------------------------------
    # 4. 登录并签发上下文令牌
    # ------------------------------------------------------------------
    print("\n=== 4. 登录并签发上下文令牌 ===")
    import json
    import urllib.request

    login_body = json.dumps({"identifier": PROBE_USERNAME, "password": PROBE_PASSWORD}).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/auth/login",
        data=login_body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        login_payload = response.read().decode("utf-8")
    check("登录成功", '"code":"OK"' in login_payload.replace(" ", ""), login_payload[:150])

    with uow_factory().begin() as tx:
        row = tx.query_one(
            "SELECT s.token_hash, u.id AS user_id FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE u.username=%s AND s.revoked_at IS NULL ORDER BY s.id DESC LIMIT 1",
            (PROBE_USERNAME,),
        )
    check("查到了刚建立的会话", row is not None)
    if row is None:
        server.shutdown()
        return 1

    from yuxin.domains.access.service import hash_token, new_token

    # 直接造一个有效会话令牌（测试用）：把最新会话的 token_hash 替换成我们知道的令牌。
    # 为什么必须换：令牌原文只在登录响应里（Cookie），而本脚本无法从 Cookie 拿到它
    # ——我们只能从库里看到哈希。换哈希等价于"服务端替这个账号建立了一个我们持钥的会话"。
    session_token = new_token()
    with uow_factory().begin() as tx:
        tx.execute(
            "UPDATE sessions SET token_hash=%s WHERE token_hash=%s",
            (hash_token(session_token), row["token_hash"]),
        )

    context_token = issue_context_token(
        secret=settings.secret_key,
        user_id=int(row["user_id"]),
        session_hash=hash_token(session_token),
        capability="",
        conversation_id="live-e2e",
        ttl_seconds=1800,
    )
    check("已签发上下文令牌", len(context_token) > 40)

    # ------------------------------------------------------------------
    # 5. 安装插件到 **profile 的 node_modules**
    #
    # 位置由解析规则决定，不能猜。`cordis:include` 的报错直接写了它从哪里找：
    #     Cannot find package '@yuxin/dsh-biz-tools' imported from
    #       <DSH_HOME>/profiles/sdk/
    # 第一版复制到了运行时闭包（runtime/node/node_modules/@deepseek-ai/），
    # 于是插件"装好了但找不到"。**读报错比读文档快。**
    # ------------------------------------------------------------------
    print("\n=== 5. 安装插件到 profile 的 node_modules ===")
    profile_dir = Path(dsh_home) / "profiles" / "sdk"
    plugin_src = ROOT / "agent-runtime"
    plugin_dst = profile_dir / "node_modules" / "@yuxin" / "dsh-biz-tools"

    if not profile_dir.is_dir():
        print(f"  FAIL  profile 目录不存在：{profile_dir}")
        server.shutdown()
        return 1

    if not (plugin_src / "lib" / "index.js").is_file():
        print("  FAIL  插件未构建（缺 lib/index.js）。先运行：")
        print(f"        cd {plugin_src} && npm run build")
        server.shutdown()
        return 1

    plugin_dst.parent.mkdir(parents=True, exist_ok=True)
    if plugin_dst.exists():
        shutil.rmtree(plugin_dst)
    # 只带运行时需要的：lib/ + package.json + cordis.patch.yml
    # （src/ tests/ node_modules 是开发期的东西，不带）
    plugin_dst.mkdir(parents=True)
    shutil.copytree(plugin_src / "lib", plugin_dst / "lib")
    shutil.copy2(plugin_src / "package.json", plugin_dst / "package.json")
    # ★ 发布副本**只含 insert**（见 `_write_publish_manifest` 的 docstring）：
    #   原样复制会把这 52 条 `disabled` 也带进去，`harness_tools_audit` 第 2 条会判红。
    _write_publish_manifest(plugin_src / "cordis.patch.yml", plugin_dst / "cordis.patch.yml")
    check("插件已安装到 profile/node_modules", plugin_dst.is_dir(), str(plugin_dst))
    print(f"        {sorted(p.name for p in plugin_dst.iterdir())}")

    # ------------------------------------------------------------------
    # 6. 生成 patch 并启动 Harness
    # ------------------------------------------------------------------
    print("\n=== 6. 启动 Harness 并发指令 ===")
    patch_path = ROOT / ".live-plugin.patch.yml"
    patch_path.write_text(
        "".join(
            [
                "- insert:\n",
                "    - id: yuxin-biz-tools\n",
                "      name: '@yuxin/dsh-biz-tools'\n",
                "      config:\n",
                "        gatewayUrl: !!js process.env.YUXIN_AGENT_GATEWAY_URL ?? ''\n",
                "        contextToken: !!js process.env.YUXIN_AGENT_CONTEXT_TOKEN ?? ''\n",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    check("patch 文件已生成", patch_path.is_file())

    from deepseek_harness import DeepSeekHarness

    from harness_smoke import child_env  # noqa: E402

    env = child_env(dsh_home)
    env["AGENT_HARNESS_ROOT"] = os.environ.get(
        "AGENT_HARNESS_ROOT", r"<harness-runtime>"
    )
    env["YUXIN_AGENT_GATEWAY_URL"] = f"http://127.0.0.1:{port}/api/v1/agent"
    env["YUXIN_AGENT_CONTEXT_TOKEN"] = context_token
    print(f"        gateway = {env['YUXIN_AGENT_GATEWAY_URL']}")
    print(f"        token   = {context_token[:16]}…")

    # 提示词用**真实区域 id**（从库里查出来的），不是夹具里那个硬编码的 7。
    prompt = (
        f"请在渔芯系统里新建一个塘口，塘口编号是 {test_code}，"
        f"名称是「智能体自建塘」，所属区域 ID 是 {probe['area_id']}，面积 9.5 亩。"
        f"请直接调用工具完成，不要问我确认。"
    )

    reply = ""
    tool_calls: list[str] = []
    harness = None
    try:
        harness = DeepSeekHarness(
            dsh_home=dsh_home,
            cwd=str(ROOT),
            profile="sdk",
            provider="deepseek-official",
            model="deepseek-v4-flash",
            max_tokens=4096,
            request_timeout_seconds=180.0,
            patches=(str(patch_path),),
            # dsh_bin 指向可执行文件，作用有二：
            #   1) 跳过 SDK 的 exe 解析（那个函数读父进程 os.environ，
            #      看不到我们给子进程准备的环境）
            #   2) 启动器在自己进程内决定用 node 载体
            dsh_bin=str(ROOT / "agent-runtime" / "bin" / "run.cmd"),
            env=env,
        )
        harness.start()
        print("  PASS  Harness 已启动（含插件）")
        result = harness.run(prompt, session_id=f"live-e2e-{int(time.time())}")
        reply = str(getattr(result, "final_response", "") or "")
        print(f"  finish_reason={getattr(result, 'finish_reason', None)!r}")
        print(f"  模型回复：{reply[:300]!r}")
        # 工具调用证据：按**工具名**筛（`pond.create` -> `pond_create`，由能力名派生）。
        #
        # 为什么不用 `"tool" in str(event)`：那会把 reasoning delta 之类一起捞进来
        # （初版输出里全是噪声），"有证据但读不出来"等于没有证据。这里按工具名定位，
        # 并把命中片段原样打印，结论因此可被复核。
        tool_name = "pond_create"
        for stream in (getattr(result, "events", None), getattr(result, "notifications", None)):
            for item in list(stream or []):
                text = str(item)
                if tool_name in text or '"tool_call"' in text:
                    tool_calls.append(text[:400])
        check("模型返回了回复", bool(reply.strip()), "回复为空")
        check(
            f"模型真的调用了真实能力对应的工具 `{tool_name}`（不是夹具里的同名假能力）",
            bool(tool_calls),
            "事件流里没有任何关于 pond_create 的记录",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  对话失败：{type(exc).__name__}: {exc}")
        FAILURES += 1
    finally:
        if harness is not None:
            try:
                harness.close()
            except Exception:  # noqa: BLE001
                pass

    if tool_calls:
        print(f"  工具调用证据（含 `pond_create` 的事件，共 {len(tool_calls)} 条，截断显示）：")
        for item in tool_calls[:4]:
            print(f"        {item[:300]}")

    # ------------------------------------------------------------------
    # 7. ★ 直接查库 —— 这一步是全部的意义
    # ------------------------------------------------------------------
    print("\n=== 7. ★ 直接查数据库确认（真实业务表 ponds） ===")
    with uow_factory().begin() as tx:
        pond = tx.query_one(
            "SELECT id, code, name, area_id, farm_id, organization_id, capacity_mu, "
            "       pond_status, status, created_by, row_version "
            "FROM ponds WHERE code=%s",
            (test_code,),
        )
        audits = tx.query_all(
            "SELECT capability, result, is_agent, username, object_ref FROM audit_logs "
            "WHERE object_ref=%s ORDER BY id DESC LIMIT 3",
            (test_code,),
        )
        has_fixture_table = tx.query_one(
            "SELECT COUNT(*) AS n FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_name='web_ponds'"
        )
        fixture_row = None
        if has_fixture_table and int(has_fixture_table["n"]) > 0:
            fixture_row = tx.query_one("SELECT id FROM web_ponds WHERE code=%s", (test_code,))

    check(f"★ 业务表 ponds 里存在编号 {test_code} 的塘口", pond is not None, f"模型回复是：{reply[:120]}")
    if pond is not None:
        print(f"        实际写入：{dict(pond)}")
        check("塘口名称正确", str(pond["name"]) == "智能体自建塘", str(pond["name"]))
        check("所属区域正确（= 提示词里的真实区域 id）", int(pond["area_id"]) == probe["area_id"], str(pond["area_id"]))
        check("面积正确（DECIMAL(12,2)）", str(pond["capacity_mu"]) == "9.50", str(pond["capacity_mu"]))
        check("初始记录状态是 draft（服务端决定，不接受客户端指定）", str(pond["status"]) == "draft", str(pond["status"]))
        check("经办人是探针账号（凭令牌解析，不是请求体声称的）",
              int(pond["created_by"]) == probe["user_id"], str(pond["created_by"]))
        check("三个分租键由服务端解析并写入（请求体里没有它们）",
              int(pond["organization_id"]) > 0 and int(pond["farm_id"]) > 0, str(dict(pond)))
    check("审计记录了这次 Agent 写入（is_agent=1）",
          any(int(a["is_agent"]) == 1 for a in audits), str(audits))
    print(f"        审计：{[dict(a) for a in audits]}")
    # 反证：如果能力来自夹具，那一行会落进夹具表 web_ponds —— 这里断言它没有。
    check("夹具表 web_ponds 里**没有**这一行（证明走的不是夹具路径）", fixture_row is None, str(fixture_row))

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------
    print("\n=== 清理 ===")
    server.shutdown()
    with uow_factory().begin() as tx:
        tx.execute("DELETE FROM ponds WHERE code=%s", (test_code,))
        tx.execute("DELETE FROM idempotency_keys WHERE capability='pond.create'")
    patch_path.unlink(missing_ok=True)
    check("已清理探针塘口与幂等键", True)

    print()
    if FAILURES:
        print(f"结果：{FAILURES} 项失败")
        return 1
    print("结果：全部通过 —— 模型通过**真实声明的能力**把数据写进了业务表")
    return 0


if __name__ == "__main__":
    sys.exit(main())
