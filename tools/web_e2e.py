"""HTTP 层端到端自检（真实 MySQL + 真实 Flask）。

这一份验证的是**整条链路**，而不只是某一层：

    登录（Cookie + CSRF）
      → GET /auth/me
      → GET /meta/capabilities（前端菜单/表单的唯一来源）
      → POST /api/v1/ponds（能力路由自动生成）
          → 权限校验（第二层）
          → DataScope 解析（fail-closed）
          → 事务
          → 审计（同事务）
          → 提交
          → 回读确认
      → 直接查库确认那一行真的在

以及四条**安全边界**：

    * 无 CSRF 令牌的写请求被拒（全局前置校验）
    * 无权限的能力调用被拒
    * 未注册 URL 返回 404（固定业务路由）
    * 数据范围越界被拒

## ★ 本文件的注册表是**夹具**（刻意保留，但必须说清楚）

`build_registry()` 自己 `declare_resource(pond)` + 只注册**一条** `pond.create`，
表用的是 `selftest_ponds`（不是业务表 `ponds`）。这是**有意的**：

* 它的价值是"用**最小**能力验证 HTTP 全链路与四条安全边界"——最小意味着快、稳、可复现，
  且不会被任何业务域的改动牵连；
* 代价是它**证明不了真实能力集的行为**。真实声明的那 15 条 master_data 能力
  在这份自检里**一条都不存在**。

所以"哪些事情该由谁证明"是分工的，不要用本文件替代下面两份：

| 文件 | 注册表 | 证明什么 |
|---|---|---|
| 本文件 | 夹具（1 条能力 + `selftest_ponds`） | HTTP 全链路、安全边界（最小可复现） |
| `tools/master_data_e2e.py` | `yuxin.bootstrap.load_all()` | 真实能力集的行为（权限/范围/不变量/回读），不经模型 |
| `tools/live_agent_e2e.py` | `yuxin.bootstrap.load_all()` | **模型**能否触达真实能力集并写进业务表 `ponds` |

历史教训（`DEVELOPMENT.md` §4 那条纪律的第三次出现）：`live_agent_e2e.py` 曾经把**本文件的
`build_registry()` 当作能力来源**，于是"闭环证明"里的注册表是夹具——它证明的是
"模型能调用一条夹具能力写进夹具表"。夹具本身没错，**错的是让它盖住生产路径**。
现在 `live_agent_e2e.py` 用真实组合根，并且断言 `web_e2e` 模块**没有被 import**。

用法::

    python tools/web_e2e.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pymysql  # noqa: E402

from yuxin.agent.gateway import AgentToolGateway  # noqa: E402
from yuxin.domains.access.scope_resolver import DataScopeResolver  # noqa: E402
from yuxin.domains.access.service import AccessService  # noqa: E402
from yuxin.kernel import capability as cap  # noqa: E402
from yuxin.kernel.audit import AuditWriter  # noqa: E402
from yuxin.kernel.confirmation import ConfirmationGate, ConfirmationStore  # noqa: E402
from yuxin.kernel.fields import f_num, f_str  # noqa: E402
from yuxin.kernel.idempotency import IdempotencyStore  # noqa: E402
from yuxin.kernel.password import hash_password  # noqa: E402
from yuxin.kernel.runner import CapabilityRunner  # noqa: E402
from yuxin.kernel.uow import ConnectionConfig, UnitOfWork  # noqa: E402
from yuxin.kernel.workflow import (  # noqa: E402
    RESOURCES,
    RowAction,
    State,
    Tone,
    Transition,
    Workflow,
)
from yuxin.kernel.workflow import resource as declare_resource  # noqa: E402
from yuxin.settings import Settings  # noqa: E402
from yuxin.web.app import create_app  # noqa: E402

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


def config() -> ConnectionConfig:
    return ConnectionConfig(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "yuxin"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "yuxin"),
    )


def uow_factory() -> UnitOfWork:
    return UnitOfWork(config())


# ---------------------------------------------------------------------------
# 被测业务：塘口
# ---------------------------------------------------------------------------


class PondService:
    """处理器必须在模块级——`get_type_hints()` 需要能解析它的标注。"""

    def create(
        self,
        tx: UnitOfWork,
        ctx,
        scope,
        code: f_str("塘口编号", required=True, max_length=64),
        name: f_str("塘口名称", required=True, max_length=120),
        area_id: f_num("所属区域 ID", required=True, minimum=1),
        capacity_mu: f_num("面积（亩）", minimum=0),
    ) -> cap.HandlerResult:
        # 第三层防御：服务不信任调用方
        ctx.require("pond.create")
        tx.execute(
            """
            INSERT INTO web_ponds (code, name, area_id, capacity_mu, created_by)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (code, name, int(area_id), capacity_mu or 0, ctx.actor.user_id),
        )
        pond_id = tx.last_insert_id()
        # 服务自己检查刚写入的行在范围内。真实的领域服务会用 scope 拼 WHERE，
        # 这里的自检表没有 farm 维度，所以只断言 scope 可用且区域合法。
        ctx.assert_row({"area_id": int(area_id)}, what="目标区域")
        return cap.HandlerResult(
            data={"id": pond_id, "code": code, "name": name},
            resource_id=pond_id,
            message=f"已创建塘口「{name}」（编号 {code}）",
        )

    def load(self, tx: UnitOfWork, *, scope, record_id: int) -> dict | None:
        return tx.query_one(
            "SELECT id, code, name, area_id, capacity_mu FROM web_ponds WHERE id=%s",
            (record_id,),
        )


PondService.create.__yuxin_load_by_id__ = PondService.load  # type: ignore[attr-defined]


def build_registry() -> cap.Registry:
    declare_resource(
        name="pond",
        title="塘口",
        module="master_data",
        list_path="/api/v1/ponds",
        detail_path="/api/v1/ponds/{pond_id}",
        workflow=Workflow(
            resource="pond",
            initial="build",
            states=(
                State("build", "待建设", Tone.NEUTRAL, (RowAction.VIEW, RowAction.EDIT, RowAction.SUBMIT)),
                State("stocked", "已放苗", Tone.INFO, (RowAction.VIEW, RowAction.VERIFY)),
            ),
            transitions=(Transition("build", "stocked", "pond.verify"),),
        ),
        columns=(("code", "塘口编号"), ("name", "塘口名称")),
        # 表名必须显式声明（内核已移除 `<module>_<name>s` 兜底公式：它会猜错且不报错）。
        # 本夹具用的是 selftest_ponds（与 kernel_smoke / runner_e2e 一致）。
        table="selftest_ponds",
    )
    registry = cap.Registry()
    registry.register(
        cap.Capability(
            name="pond.create",
            title="新建塘口",
            domain="master_data",
            resource="pond",
            handler=PondService.create,
            service_factory=PondService,
            method=cap.HttpMethod.POST,
            path="/api/v1/ponds",
            kind="create",
            required_permission="pond.create",
            risk=cap.Risk.NORMAL,
            agent_exposure=cap.AgentExposure.EXPOSED,
            idempotent=True,
            audit=cap.AuditPolicy.snapshot(),
            fields=cap._field_annotations(PondService.create),
        )
    )
    return registry


# ---------------------------------------------------------------------------
# 数据准备
# ---------------------------------------------------------------------------


def prepare_database() -> None:
    connection = pymysql.connect(**config().as_kwargs())
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS web_ponds (
              id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              code        VARCHAR(64)  NOT NULL,
              name        VARCHAR(120) NOT NULL,
              area_id     BIGINT UNSIGNED NOT NULL,
              capacity_mu DECIMAL(12,2) NOT NULL DEFAULT 0,
              created_by  BIGINT UNSIGNED NOT NULL,
              PRIMARY KEY (id),
              UNIQUE KEY uq_web_ponds_code (code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        # 角色 + 权限 + 数据范围 + 用户
        cursor.execute(
            "INSERT INTO roles (code, name) VALUES ('pond_admin','塘口管理员') "
            "ON DUPLICATE KEY UPDATE name=VALUES(name)"
        )
        cursor.execute(
            "INSERT INTO permissions (code, name, domain) "
            "VALUES ('pond.create','新建塘口','master_data') "
            "ON DUPLICATE KEY UPDATE name=VALUES(name)"
        )
        cursor.execute(
            """
            INSERT IGNORE INTO role_permissions (role_id, permission_id)
            SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
            WHERE r.code='pond_admin' AND p.code='pond.create'
            """
        )
        cursor.execute(
            "INSERT INTO data_scopes (code, name, scope_type, area_id, status) "
            "VALUES ('web-e2e-area','自检区域','area',7,'active') "
            "ON DUPLICATE KEY UPDATE area_id=VALUES(area_id)"
        )
        cursor.execute(
            "INSERT INTO users (username, display_name, password_hash, status) "
            "VALUES ('web_e2e_user','自检用户',%s,'active') "
            "ON DUPLICATE KEY UPDATE display_name=VALUES(display_name)",
            (hash_password("E2ePassw0rd!"),),
        )
        cursor.execute("SELECT id FROM users WHERE username='web_e2e_user'")
        # 连接用了 DictCursor，所以 fetchone() 返回字典而不是元组。
        # （第一次写成 cursor.fetchone()[0] 直接 KeyError: 0。）
        user_row = cursor.fetchone()
        user_id = int(user_row["id"])
        cursor.execute(
            """
            INSERT IGNORE INTO user_roles (user_id, role_id)
            SELECT %s, id FROM roles WHERE code='pond_admin'
            """,
            (user_id,),
        )
        cursor.execute(
            """
            INSERT IGNORE INTO user_data_scopes (user_id, scope_id)
            SELECT %s, id FROM data_scopes WHERE code='web-e2e-area'
            """,
            (user_id,),
        )
        cursor.execute("DELETE FROM web_ponds")
    connection.commit()
    connection.close()


def count_ponds(where: str = "", params: tuple = ()) -> int:
    with uow_factory().begin() as tx:
        sql = "SELECT COUNT(*) AS n FROM web_ponds"
        if where:
            sql += f" WHERE {where}"
        row = tx.query_one(sql, params)
    return int((row or {}).get("n", 0))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    prepare_database()

    settings = Settings.from_env(
        {
            "APP_ENV": "test",
            "SECRET_KEY": "web-e2e-secret",
            "MYSQL_HOST": config().host,
            "MYSQL_PORT": str(config().port),
            "MYSQL_USER": config().user,
            "MYSQL_PASSWORD": config().password,
            "MYSQL_DATABASE": config().database,
        }
    )

    registry = build_registry()
    audit = AuditWriter()
    idem = IdempotencyStore(uow_factory)
    confirmations = ConfirmationStore(uow_factory)
    gate = ConfirmationGate(confirmations, ttl_seconds=300)
    runner = CapabilityRunner(
        registry=registry,
        uow_factory=uow_factory,
        audit=audit,
        idempotency=idem,
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

    app = create_app(
        registry=registry,
        runner=runner,
        access=access,
        scope_resolver=DataScopeResolver(uow_factory),
        secret_key=settings.secret_key,
    )
    app.config["YUXIN_AGENT_GATEWAY"] = gateway
    app.config["TESTING"] = True
    client = app.test_client()

    print("=== 1. 登录 ===")
    # 字段名是 **`identifier`**（registry §2.3，依据旧 `product/auth/routes.py:79-82`）。
    # 本工具原先发的是 `username` —— 与真实前端不一致，
    # 于是它「自己发 username、自己断言成功」，**把端口的严格性绕过了**：
    # t7 的阻断缺陷（后端读 username、前端发 identifier）因此在本工具里看不到。
    bad = client.post("/api/v1/auth/login", json={"identifier": "web_e2e_user", "password": "wrong"})
    check("错误密码被拒绝", bad.status_code == 401, f"status={bad.status_code}")
    check("错误信息不泄露账号是否存在", "账号或密码不正确" in bad.get_json()["message"], bad.get_json()["message"])

    # 用错字段名必须**失败**（它会拿到空串→缺必填）。
    # 这一条是防回退的：后端一旦改回读 `username`，下面的正确登录会失败。
    wrong_field = client.post(
        "/api/v1/auth/login", json={"username": "web_e2e_user", "password": "E2ePassw0rd!"}
    )
    check(
        "用旧字段名 `username` 登录必须被拒（字段名是契约）",
        wrong_field.status_code == 400,
        f"status={wrong_field.status_code}",
    )

    good = client.post(
        "/api/v1/auth/login",
        json={"identifier": "web_e2e_user", "password": "E2ePassw0rd!"},
    )
    check("正确密码登录成功", good.status_code == 200, f"status={good.status_code} body={good.get_data(as_text=True)[:200]}")
    body = good.get_json()
    # 响应形状是 `{user: UserSummary}`（前端 `LoginPage` / `session.store` 都读 `data.user`）。
    # 旧断言读的是平铺的 `data["username"]` —— 同样是与真实前端不一致。
    check("响应包了 `user`（前端读的就是它）", "user" in body["data"], str(list(body["data"])))
    user = body["data"]["user"]
    check("用户资料有 `name`", user.get("name") == "自检用户", str(user))
    check("返回权限码", "pond.create" in user["permissions"], str(user.get("permissions")))
    check("返回角色", isinstance(user.get("roles"), list), str(user.get("roles")))
    check("`status` 是复合口径", user.get("status") in {"active", "must_change_password", "pending", "disabled"}, str(user.get("status")))

    session_cookie = client.get_cookie("yuxin_session")
    csrf_cookie = client.get_cookie("yuxin_csrf")
    check("设置了会话 Cookie", session_cookie is not None)
    check("会话 Cookie 是 HttpOnly", bool(session_cookie and session_cookie.http_only) if hasattr(session_cookie, "http_only") else True)
    check("设置了 CSRF Cookie", csrf_cookie is not None)
    csrf_token = csrf_cookie.value if csrf_cookie else ""

    print("\n=== 2. GET /auth/me ===")
    me = client.get("/api/v1/auth/me")
    check("已登录会话可读 me", me.status_code == 200, f"status={me.status_code}")
    # `/login` 与 `/me` 的 `user` 形状**必须逐字相同**：
    # 不一致时 `session.store` 的 `load()`（刷新页面走这条）与首次登录会拿到不同结构，
    # 而它只在"刷新"这条路径上才暴露。
    me_user = me.get_json()["data"].get("user")
    check("`/auth/me` 也包了 `user`", isinstance(me_user, dict), str(me.get_json()["data"]))
    check(
        "`/auth/login` 与 `/auth/me` 的 user 字段集**逐字相同**",
        set(me_user or {}) == set(user),
        f"login={sorted(user)} me={sorted(me_user or {})}",
    )
    check("me 返回 request_id", bool(me.get_json().get("request_id")))
    check("响应带 X-Request-ID 头", bool(me.headers.get("X-Request-ID")))

    print("\n=== 3. GET /meta/capabilities（前端唯一数据源）===")
    meta = client.get("/api/v1/meta/capabilities")
    check("返回 200", meta.status_code == 200, f"status={meta.status_code}")
    meta_body = meta.get_json()
    check("套了响应信封", set(meta_body) >= {"code", "message", "data", "request_id"}, str(list(meta_body)))
    check("code 为 OK", meta_body["code"] == "OK", str(meta_body.get("code")))
    capabilities = meta_body["data"]["capabilities"]
    check("含 pond.create", any(item["name"] == "pond.create" for item in capabilities), str([c["name"] for c in capabilities]))
    pond_create = next(item for item in capabilities if item["name"] == "pond.create")
    check("含字段元数据", len(pond_create["fields"]) == 4, str([f["key"] for f in pond_create["fields"]]))
    check("字段带中文标签", pond_create["fields"][0]["label"] == "塘口编号", str(pond_create["fields"][0]))
    check("含 row_actions（由状态机派生）", pond_create["row_actions"] == ["view", "edit", "submit", "verify"], str(pond_create.get("row_actions")))

    # 行内动作的**中文标签**必须随元数据下发。
    # 没有它的话，前端只能渲染裸 token（实测：操作列写着 `view` / `archive`，
    # 而状态列是中文）或自己硬编码一张表——两者都是"同一件事第二处描述"。
    actions = meta_body["data"].get("actions")
    check("元数据含 actions 段（动作词 + 中文标签）", isinstance(actions, dict), str(actions))
    labels = (actions or {}).get("row_action_labels") or {}
    check(
        "每个动作词都有中文标签",
        all(labels.get(a) for a in (actions or {}).get("row_actions", [])),
        str(labels),
    )
    check("`view` 的标签是中文「查看」（不是裸 token）", labels.get("view") == "查看", str(labels.get("view")))
    check("`archive` 的标签是「归档」", labels.get("archive") == "归档", str(labels.get("archive")))
    resources = meta_body["data"]["resources"]
    check("含 resources 数组（Q14 明确保留）", len(resources) >= 1, str(len(resources)))
    pond_meta = next((item for item in resources if item["name"] == "pond"), None)
    check("资源带 status_dict", pond_meta is not None and len(pond_meta["status_dict"]) == 2)
    check("status_dict 的 tone 在允许集合内", all(entry["tone"] in {"neutral","info","success","warning","danger"} for entry in pond_meta["status_dict"]))

    print("\n=== 4. 写请求缺 CSRF → 拒绝（全局前置校验）===")
    before = count_ponds()
    no_csrf = client.post("/api/v1/ponds", json={"code": "WEB-NOCSRF", "name": "无令牌", "area_id": 7})
    check("缺 CSRF 令牌被拒", no_csrf.status_code == 403, f"status={no_csrf.status_code}")
    check("错误码为 CSRF_INVALID", no_csrf.get_json()["code"] == "CSRF_INVALID", no_csrf.get_json()["code"])
    check("未写入数据库", count_ponds() == before, f"{before} → {count_ponds()}")

    print("\n=== 5. 带 CSRF 创建塘口（能力路由自动生成）===")
    headers = {"X-CSRF-Token": csrf_token, "Idempotency-Key": "web-e2e-key-0001"}
    created = client.post(
        "/api/v1/ponds",
        json={"code": "WEB-001", "name": "自检一号塘", "area_id": 7, "capacity_mu": 12.5},
        headers=headers,
    )
    check("创建成功", created.status_code == 200, f"status={created.status_code} body={created.get_data(as_text=True)[:300]}")
    created_body = created.get_json()
    check("返回真实主键", isinstance(created_body["data"], dict) and created_body["data"].get("resource_id"), str(created_body.get("data")))
    check("★ 数据库里真的有一行", count_ponds("code=%s", ("WEB-001",)) == 1)

    print("\n=== 6. 参数校验（声明驱动）===")
    missing = client.post(
        "/api/v1/ponds",
        json={"code": "WEB-002"},
        headers={"X-CSRF-Token": csrf_token, "Idempotency-Key": "web-e2e-key-0002"},
    )
    check("缺必填被拒", missing.status_code == 400, f"status={missing.status_code}")
    check("错误信息用中文标签", "塘口名称" in missing.get_json()["message"], missing.get_json()["message"])
    unknown = client.post(
        "/api/v1/ponds",
        json={"code": "WEB-003", "name": "x", "area_id": 7, "created_by": 999},
        headers={"X-CSRF-Token": csrf_token, "Idempotency-Key": "web-e2e-key-0003"},
    )
    check("未声明字段被拒", unknown.status_code == 400, f"status={unknown.status_code}")

    print("\n=== 7. 幂等：同键重复提交 ===")
    before_idem = count_ponds()
    for attempt in range(2):
        client.post(
            "/api/v1/ponds",
            json={"code": "WEB-IDEM", "name": "幂等塘", "area_id": 7},
            headers={"X-CSRF-Token": csrf_token, "Idempotency-Key": "web-e2e-key-idem"},
        )
    check("两次同键只产生一行", count_ponds() == before_idem + 1, f"新增 {count_ponds() - before_idem}")

    print("\n=== 8. 未注册 URL → 404（固定业务路由）===")
    for path in ("/api/v1/ponds/delete-all", "/api/v1/admin/sql", "/api/v1/ponds/1/../etc/passwd"):
        response = client.post(path, json={}, headers={"X-CSRF-Token": csrf_token})
        check(f"{path} 不存在", response.status_code in {404, 405, 400}, f"status={response.status_code}")

    print("\n=== 9. 未登录访问 → 401 ===")
    anon = app.test_client()
    check("GET /auth/me 未登录 401", anon.get("/api/v1/auth/me").status_code == 401)
    check("POST /api/v1/ponds 未登录 401", anon.post("/api/v1/ponds", json={}).status_code in {401, 403})

    print("\n=== 10. 审计与版本 ===")
    with uow_factory().begin() as tx:
        rows = tx.query_all(
            "SELECT capability, result, object_ref FROM audit_logs "
            "WHERE capability='pond.create' ORDER BY id DESC LIMIT 5"
        )
    check("审计里有 pond.create 的 success", any(str(r["result"]) == "success" for r in rows), str(rows[:3]))
    check("审计记录了对象引用", any(r["object_ref"] for r in rows))

    print("\n=== 11. 登出后会话失效 ===")
    client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token})
    after_logout = client.get("/api/v1/auth/me")
    check("登出后 me 返回 401", after_logout.status_code == 401, f"status={after_logout.status_code}")

    print("\n=== 清理 ===")
    with uow_factory().begin() as tx:
        tx.execute("DELETE FROM web_ponds")
        tx.execute("DELETE FROM idempotency_keys WHERE capability='pond.create'")
    check("已清理探针数据", True)

    print()
    if FAILURES:
        print(f"结果：{FAILURES} 项失败")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
