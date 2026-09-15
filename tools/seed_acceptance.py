"""验收环境种子：把"能登录、能点全站"的最小数据集固化成**一条可复跑的命令**。

## 为什么需要它（这是"重置数据库后跑不起来"的总根因）

`tools/migrate.py reset` 只建表 + 结构种子（企业/基地/区域/物料/仓库），
`tools/seed_permissions.py --demo-role` 只种权限码与一个演示角色。
但验收/自检真正依赖的**业务角色（8 个）、数据范围、`demo` / `qa-maker` / `qa-checker`
账号**此前只存在于某次手工操作里：一旦重置数据库，`demo` 直接登录不了，整套门禁全失效。

本工具补上这个入口。它是**验收专用**种子，与生产数据**分开**：角色码、账号口径都写死在
这里，可 review、可 diff；生产库不应执行它（或执行后按 `docs/DEPLOY.md` 摘掉）。

## 用法（顺序不能换）

    $env:MYSQL_PASSWORD='fpa_dev_password'
    python tools/seed_permissions.py --demo-role   # ① 派生权限码 + fpa-demo 角色
    python tools/seed_acceptance.py                # ② 种业务角色 / 数据范围 / 验收账号
    # ③ 业务数据：mysql -u root -p fpa < database/test_data.sql

## 幂等

角色 / 范围 / 账号都用固定 id + `ON DUPLICATE KEY UPDATE`；角色授权与账号绑定先删后建，
因此重复跑结果一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fpa.kernel.password import hash_password  # noqa: E402
from fpa.kernel.uow import UnitOfWork  # noqa: E402
from fpa.kernel.uow_factory import connection_config  # noqa: E402

#: 业务角色：(id, code, name, description, 权限码列表或 `"*"` 表示全部派生权限)。
#:
#: 角色到权限的映射是**组织决策**，注册表里没有它（见 `tools/seed_permissions.py` 的
#: docstring）。这里显式写死，是为了让验收环境可复现、可 diff；生产角色应由管理员经
#: 页面配置，而不是由本工具替他决定。
ROLE_DEFS: list[tuple[int, str, str, str, str | list[str]]] = [
    (3, "super_admin", "超级管理员", "系统内置：全权限", "*"),
    (4, "gm", "总经理", "经营管理全权限", "*"),
    (
        5,
        "purchaser",
        "采购员",
        "采购与付款登记",
        [
            "finance.payable.view",
            "finance.payment.manage",
            "finance.payment.verify",
            "finance.payment.view",
            "purchase.approve",
            "purchase.create",
            "purchase.view",
        ],
    ),
    (
        6,
        "warehouse_keeper",
        "仓管员",
        "仓储与库存",
        [
            "inventory.view",
            "warehouse.archive",
            "warehouse.create",
            "warehouse.issue.create",
            "warehouse.issue.verify",
            "warehouse.receipt.create",
            "warehouse.receipt.verify",
            "warehouse.submit",
            "warehouse.update",
            "warehouse.verify",
            "warehouse.view",
        ],
    ),
    (
        7,
        "aquaculturist",
        "养殖员",
        "塘口、批次、投喂、出塘",
        [
            "batch.create",
            "batch.update",
            "batch.verify",
            "batch.view",
            "feeding.create",
            "feeding.verify",
            "feeding.view",
            "harvest.create",
            "harvest.verify",
            "harvest.view",
            "pond.archive",
            "pond.create",
            "pond.status.request",
            "pond.status.verify",
            "pond.submit",
            "pond.update",
            "pond.verify",
            "pond.view",
        ],
    ),
    (
        8,
        "salesperson",
        "销售员",
        "销售、交付与收款",
        [
            "finance.receipt.manage",
            "finance.receipt.verify",
            "finance.receivable.view",
            "sales.approve",
            "sales.create",
            "sales.deliver",
            "sales.verify",
            "sales.view",
        ],
    ),
    (
        9,
        "cashier",
        "财务",
        "应付、应收、收付款与成本",
        [
            "cost.close",
            "cost.confirm",
            "cost.manage",
            "cost.view",
            "finance.payable.view",
            "finance.payment.manage",
            "finance.payment.verify",
            "finance.payment.view",
            "finance.receipt.manage",
            "finance.receipt.verify",
            "finance.receivable.view",
        ],
    ),
    (
        10,
        "inspector",
        "质检核验员",
        "核验与审批",
        [
            "area.view",
            "batch.verify",
            "batch.view",
            "cost.view",
            "farm.view",
            "feeding.verify",
            "feeding.view",
            "finance.payable.view",
            "finance.payment.verify",
            "finance.payment.view",
            "finance.receipt.verify",
            "finance.receivable.view",
            "harvest.verify",
            "harvest.view",
            "inventory.view",
            "material.submit",
            "material.verify",
            "material.view",
            "partner.view",
            "pond.status.verify",
            "pond.submit",
            "pond.verify",
            "pond.view",
            "purchase.approve",
            "purchase.view",
            "sales.approve",
            "sales.verify",
            "sales.view",
            "warehouse.issue.verify",
            "warehouse.receipt.verify",
            "warehouse.submit",
            "warehouse.verify",
            "warehouse.view",
        ],
    ),
]

#: 数据范围：(id, code, name, scope_type, 基地 code)。`personal` 不需要基地。
SCOPE_DEFS: list[tuple[int, str, str, str, str | None]] = [
    (1, "personal-default", "仅本人数据（默认）", "personal", None),
    (2, "sc-farm", "嵊泗列岛养殖基地（全部塘口）", "farm", "default-farm"),
    (4, "user-demo-farm", "演示账号：全基地", "farm", "default-farm"),
    (5, "user-qa-maker-farm", "测试经办员：全基地", "farm", "default-farm"),
    (6, "user-qa-checker-farm", "测试复核员：全基地", "farm", "default-farm"),
]

#: 验收账号：(id, username, display_name, 明文口令, 角色码, 数据范围码)。
ACCOUNT_DEFS: list[tuple[int, str, str, str, list[str], list[str]]] = [
    (9204, "demo", "演示账号", "Demo1234!", ["fpa-demo-all-permissions", "super_admin"], ["user-demo-farm"]),
    (9205, "qa-maker", "测试经办员", "Tz9$kR4!", ["gm"], ["user-qa-maker-farm"]),
    (
        9206,
        "qa-checker",
        "测试复核员",
        "Tz9$kR4!",
        ["cashier", "inspector", "warehouse_keeper"],
        ["user-qa-checker-farm"],
    ),
]


def main() -> int:
    uow = UnitOfWork(connection_config())
    with uow.begin() as tx:
        permissions = {str(row["code"]): int(row["id"]) for row in tx.query_all("SELECT id, code FROM permissions")}
        farms = {str(row["code"]): row for row in tx.query_all("SELECT id, organization_id, code FROM farms")}
        if not permissions:
            print("FAIL  permissions 表为空 —— 先跑 `python tools/seed_permissions.py --demo-role`")
            return 1
        if "default-farm" not in farms:
            print("FAIL  缺少种子基地 default-farm —— 先跑 `python tools/migrate.py reset|apply`")
            return 1

        granted_total = 0
        for role_id, code, name, description, spec in ROLE_DEFS:
            tx.execute(
                "INSERT INTO roles (id, code, name, description, status) "
                "VALUES (%s,%s,%s,%s,'active') "
                "ON DUPLICATE KEY UPDATE name=VALUES(name), description=VALUES(description), status='active'",
                (role_id, code, name, description),
            )
            codes = sorted(permissions) if spec == "*" else list(spec)
            missing = [item for item in codes if item not in permissions]
            if missing:
                print(f"FAIL  角色 {code} 引用了不存在的权限码：{missing}（先跑 seed_permissions）")
                return 1
            tx.execute("DELETE FROM role_permissions WHERE role_id=%s", (role_id,))
            for permission_code in codes:
                tx.execute(
                    "INSERT INTO role_permissions (role_id, permission_id) VALUES (%s,%s)",
                    (role_id, permissions[permission_code]),
                )
                granted_total += 1

        for scope_id, code, name, scope_type, farm_code in SCOPE_DEFS:
            if scope_type == "personal":
                tx.execute(
                    "INSERT INTO data_scopes (id, code, name, scope_type, status) "
                    "VALUES (%s,%s,%s,'personal','active') "
                    "ON DUPLICATE KEY UPDATE name=VALUES(name), scope_type='personal', "
                    "organization_id=NULL, farm_id=NULL, area_id=NULL, pond_id=NULL, status='active'",
                    (scope_id, code, name),
                )
                continue
            farm = farms.get(str(farm_code))
            if farm is None:
                print(f"FAIL  数据范围 {code} 引用的基地 {farm_code!r} 不存在")
                return 1
            tx.execute(
                "INSERT INTO data_scopes (id, code, name, scope_type, organization_id, farm_id, status) "
                "VALUES (%s,%s,%s,'farm',%s,%s,'active') "
                "ON DUPLICATE KEY UPDATE name=VALUES(name), scope_type='farm', "
                "organization_id=VALUES(organization_id), farm_id=VALUES(farm_id), "
                "area_id=NULL, pond_id=NULL, status='active'",
                (scope_id, code, name, farm["organization_id"], farm["id"]),
            )

        for user_id, username, display_name, password, role_codes, scope_codes in ACCOUNT_DEFS:
            tx.execute(
                "INSERT INTO users (id, username, display_name, password_hash, status, must_change_password) "
                "VALUES (%s,%s,%s,%s,'active',0) "
                "ON DUPLICATE KEY UPDATE username=VALUES(username), display_name=VALUES(display_name), "
                "password_hash=VALUES(password_hash), status='active', must_change_password=0",
                (user_id, username, display_name, hash_password(password)),
            )
            tx.execute("DELETE FROM user_roles WHERE user_id=%s", (user_id,))
            for role_code in role_codes:
                tx.execute(
                    "INSERT INTO user_roles (user_id, role_id) "
                    "SELECT %s, r.id FROM roles r WHERE r.code=%s",
                    (user_id, role_code),
                )
            tx.execute("DELETE FROM user_data_scopes WHERE user_id=%s", (user_id,))
            for scope_code in scope_codes:
                tx.execute(
                    "INSERT INTO user_data_scopes (user_id, scope_id) "
                    "SELECT %s, s.id FROM data_scopes s WHERE s.code=%s",
                    (user_id, scope_code),
                )

    print(
        f"OK  验收种子已就绪：角色 {len(ROLE_DEFS)} 个 / 授权 {granted_total} 条，"
        f"数据范围 {len(SCOPE_DEFS)} 条，账号 {len(ACCOUNT_DEFS)} 个（demo / qa-maker / qa-checker）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
