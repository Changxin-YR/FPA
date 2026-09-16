"""强制改密闸门（P1 安全缺陷的回归守卫）。

## 这条守卫防的是什么（一次实测缺陷）

服务端把 `users.must_change_password` 折叠进 `/auth/me` 的 `status`，前端据此把用户
送到改密页 —— 但**执行路径上没有任何一层读它**。实测：`must_change_password` 的账号
登录后直接访问 `/api/v1/purchase-orders` 依然 **200**，"初始口令未改"形同虚设
（管理员知道的初始口令可以一直用下去）。

修法在 `web/app.py::_current_actor()` —— 它是**唯一的执行身份入口**，所有能力路由、
`/meta/capabilities`、Agent 各端点都从这里取 actor；`/auth/*` 走 `routes_auth.py` 的
会话直查，因此改密本身不会被拦死。

## 判据（四个方向，缺一不可）

1. 未改密时**业务接口必须 403**，且 `data.reason == "must_change_password"`；
2. 未改密时 **`/auth/me` 必须 200**（前端要靠它判断该去改密页）；
3. 未改密时**改密请求必须放行**（否则死锁）；
4. 改密之后业务接口必须恢复 200。
"""

from __future__ import annotations

import time

import pytest

from yuxin.factory import build_app


CODE = "qa-pwgate-test"
OLD_PASSWORD = "Tz9$kR4!"
NEW_PASSWORD = "Rn8#wQ3!y"


def _admin(app):  # noqa: ANN001, ANN201
    c = app.test_client()
    response = c.post(
        "/api/v1/auth/login", json={"identifier": "demo", "password": "Demo1234!"}
    )
    if response.status_code != 200:
        pytest.skip(f"演示账号登录失败（status={response.status_code}），本条需要真实库")
    return c


def _probe_grants() -> tuple[int, int]:
    """取探针要用的角色/范围 id，按**码**查而不是写死数字。

    写死 id 在库重建后会指向别的行、或根本不存在（实测：重置后 `role_id=9` 变成了
    cashier，而 `scope_id=2` 不存在）——那时测试撞的是"角色/范围不存在"，不是"强制改密"。
    `inspector` 持有 `purchase.view` / `pond.view`，能满足"改密后业务接口恢复 200"的断言。
    """
    from yuxin.kernel.uow_factory import connection_config

    import pymysql

    connection = pymysql.connect(**connection_config().as_kwargs())
    try:
        with connection.cursor() as cursor:
            role_id = 0
            for role_code in ("inspector", "gm", "super_admin"):
                cursor.execute(
                    "SELECT id FROM roles WHERE code=%s AND status='active'", (role_code,)
                )
                row = cursor.fetchone()
                if row:
                    role_id = int(row["id"] if isinstance(row, dict) else row[0])
                    break
            if not role_id:
                raise RuntimeError("找不到可用业务角色（inspector/gm/super_admin）")
            cursor.execute(
                "SELECT id FROM data_scopes WHERE status='active' AND scope_type='farm' "
                "AND code=%s LIMIT 1",
                ("sc-farm",),
            )
            row = cursor.fetchone()
            if not row:
                raise RuntimeError("找不到基地数据范围 sc-farm")
            scope_id = int(row["id"] if isinstance(row, dict) else row[0])
    finally:
        connection.close()
    return role_id, scope_id


def _cleanup(app) -> None:  # noqa: ANN001, ANN201
    """把探针账号**彻底删除**，保证这条测试可重复跑、且不污染交付数据集。

    只置 `retired` 是不够的：用户行会留下来，交付文档里"10 启用 / 16 已注销"
    马上对不上（实测踩到过：跑完一轮变成 11 / 17）。探针只读业务接口、没有任何
    业务外键引用它，因此先清绑定、再删行是安全的。
    """
    from yuxin.kernel.uow_factory import connection_config

    import pymysql

    connection = pymysql.connect(**connection_config().as_kwargs())
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE username=%s", (CODE,))
            row = cursor.fetchone()
            if row:
                uid = int(row["id"] if isinstance(row, dict) else row[0])
                for table in ("sessions", "user_roles", "user_data_scopes", "idempotency_keys"):
                    cursor.execute(f"DELETE FROM {table} WHERE user_id=%s", (uid,))  # noqa: S608
                cursor.execute("DELETE FROM users WHERE id=%s", (uid,))
        connection.commit()
    finally:
        connection.close()


def test_未改初始密码的账号不得使用任何业务能力():
    app = build_app()
    app.config["TESTING"] = True
    admin = _admin(app)
    role_id, scope_id = _probe_grants()

    # 造一个"初始密码未改"的账号（`access.user.create` 的初始状态就是它）
    csrf = admin.get("/api/v1/auth/csrf").get_json()["data"]["csrf_token"]
    created = admin.post(
        "/api/v1/admin/users",
        json={
            "username": CODE,
            "display_name": "强制改密闸门探针",
            "initial_password": OLD_PASSWORD,
            "role_ids": [role_id],   # 按码解析的业务角色（持有 purchase.view）
            "scope_ids": [scope_id],  # 按码解析的基地范围
        },
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": f"pwgate-{time.time_ns()}"},
    )
    if created.status_code == 409:  # 上一轮跑剩的，先复位
        _cleanup(app)
        created = admin.post(
            "/api/v1/admin/users",
            json={
                "username": CODE,
                "display_name": "强制改密闸门探针",
                "initial_password": OLD_PASSWORD,
                "role_ids": [role_id],
                "scope_ids": [scope_id],
            },
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": f"pwgate-{time.time_ns()}"},
        )
    assert created.status_code == 200, created.get_json()

    try:
        weak = app.test_client()
        assert weak.post(
            "/api/v1/auth/login", json={"identifier": CODE, "password": OLD_PASSWORD}
        ).status_code == 200
        me = weak.get("/api/v1/auth/me").get_json()["data"]["user"]
        assert me["status"] == "must_change_password", me

        # ① 业务接口与元数据都必须被拦
        for path in ("/api/v1/purchase-orders?page=1&page_size=5", "/api/v1/ponds?page=1&page_size=5"):
            response = weak.get(path)
            assert response.status_code == 403, (path, response.status_code)
            assert (response.get_json().get("data") or {}).get("reason") == "must_change_password"
        assert weak.get("/api/v1/meta/capabilities").status_code == 403

        # ② /auth/me 必须仍然可用
        assert weak.get("/api/v1/auth/me").status_code == 200

        # ③ 改密本身必须放行
        token = weak.get("/api/v1/auth/csrf").get_json()["data"]["csrf_token"]
        changed = weak.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": OLD_PASSWORD,
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
            headers={"X-CSRF-Token": token, "Idempotency-Key": f"pwchange-{time.time_ns()}"},
        )
        assert changed.status_code == 200, changed.get_json()

        # ④ 改完之后业务接口恢复
        fresh = app.test_client()
        assert fresh.post(
            "/api/v1/auth/login", json={"identifier": CODE, "password": NEW_PASSWORD}
        ).status_code == 200
        assert fresh.get("/api/v1/auth/me").get_json()["data"]["user"]["status"] == "active"
        assert fresh.get("/api/v1/purchase-orders?page=1&page_size=5").status_code == 200
    finally:
        _cleanup(app)
