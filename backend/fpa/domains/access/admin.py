"""账号与权限管理 —— access 域 5 条能力的服务层。

对应 `docs/CAPABILITY_REGISTRY.md`：

    §1.2 access   access.user.list / access.user.create / access.user.status
                  access.user.grants / access.role.permissions
    §1.3 audit    audit.log.list
    §1.1 identity auth.password.change

## 为什么放在 `domains/access/`（而不是新开 identity / audit 域目录）

能力 → 域目录的归属有一条硬约束：`tests/test_architecture.py` 的
`test_capability_domain_matches_its_declaring_directory` 要求
**处理器的模块路径必须与 `Capability.domain` 同域**。三者都在这一个文件里实现，
因此三条能力声明都写 `domain="access"`。

这不是"把三件事塞进一个域"：access / identity / audit 处理的是**同一件事**——
"谁是谁、谁能做什么、做过什么"。`domains/access/service.py` 本来就同时承担
登录、会话与权限加载（见它的模块 docstring）。再看它们的表：
`users` / `roles` / `permissions` / `user_roles` / `data_scopes` / `audit_logs`
**全部由 001 迁移建立**，属于同一批身份表。按"表的归属"分域，它们本来就是一个域。

## 字段名与本文件的第二处事实源：**没有**

registry §2.4 的字段表沿用了**早期版本**的列名（`name` / `phone` / `login_name` /
`assigned_by`），而 001 迁移建的是 `users(username, display_name, ...)`，
并且**没有 phone / login_name 两列**。这里以**真库结构**为准，理由是本项目的一贯口径：
表结构只由迁移定义一处，文档里的字段表是规格**意图**，不是 DDL。

三处具体差异与处置（都在声明侧的注释里再写一遍，避免读代码的人去猜）：

| registry §2.4 | 本实现 | 理由 |
|---|---|---|
| `name` | `display_name` | 001 的实际列名 |
| `phone`（大陆手机号，唯一） | **无**，改用 `username`（全局唯一） | 表里没有 phone 列；`uq_users_username` 是唯一可信的判定 |
| `login_name` | 并入 `username` | 表里只有一个登录名列 |
| `assigned_by` | `created_by` / `updated_by` | 与其余各域的分租与留痕列同名 |

手机号不是被砍掉的能力：它是**一个字段**。要恢复它需要的是迁移加列，不是能力声明。

## 本模块**不管**审计日志

`audit.log.list` 与它的服务在 `domains/audit/`。初稿把它写在本文件里（理由是
"两者的表都由 001 建立、按表分域本来就是一家"），但那条理由站不住：
**分域的判据是能力归属，不是建表时间**。`tests/test_architecture.py`
`::test_capability_domain_matches_its_declaring_directory` 把 `Capability.domain`
与声明目录钉在一起，而 registry §1.3 把它归在 **audit 域**。
实测就是这条断言把初稿拦下来的：
`audit.log.list: domain='audit' 但声明在 fpa.domains.access`。
"""

from __future__ import annotations

from typing import Any

from fpa.kernel import capability as cap
from fpa.kernel.errors import DomainError, ErrorCode, not_found
from fpa.kernel.fields import Choice, RefTarget, f_enum, f_int_list, f_str, f_str_list, text
from fpa.kernel.password import hash_password, password_problem, verify_password
from fpa.kernel.scope import Scope
from fpa.kernel.uow import UnitOfWork

from .service import _utcnow

#: `users.status` 的**可设置**值（registry §2.4 `access.user.status`）。
#:
#: 001 迁移的 ENUM 有四个值（`pending` / `active` / `disabled` / `retired`），
#: 而本版只能设两个：`pending` 随"注册审核"一起砍（registry §6），
#: `retired` 并入 `disabled`（§2.4 原文："`retired` 并入 `disabled` + `note`"）。
#: 与 §2.4 的处置一致，这里**刻意不把 ENUM 全集当可设集**——
#: 让管理员能设一个系统永远不会自己进入、也没有能力读懂的状态，是凭空造一个洞。
SETTABLE_USER_STATUSES = ("active", "disabled")

STATUS_LABELS: dict[str, str] = {
    "pending": "待审核",
    "active": "启用",
    "disabled": "停用",
    "retired": "已注销",
}

#: 下拉选项。中文只写一次（`STATUS_LABELS`），`Choice.label` 从它派生——
#: 与 `master_data/partners_write.py` 同一口径：文案只能有一个来源。
STATUS_CHOICES = tuple(
    Choice(value=code, label=STATUS_LABELS[code]) for code in SETTABLE_USER_STATUSES
)

SCOPE_TYPE_LABELS: dict[str, str] = {
    "farm": "基地",
    "area": "区域",
    "pond": "塘口",
    "personal": "个人",
}

def _group(rows: list[dict[str, Any]], key: str) -> dict[int, list[dict[str, Any]]]:
    """把 `query_all` 的结果按某一列分组成 ``{id: [row, ...]}``。

    用于把"每行各自查一次关联"（N+1）变成**两次批量查询**。列表页是本项目
    明确要求批量形态的场景之一（`docs/ROLLOUT_CONTRACT.md` §2.0）。
    """
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row[key]), []).append(row)
    return grouped


class AdminService:
    """账号与角色。**自己校验权限**（第三层防御）。

    刻意**不**继承 `AccessService`：那个类的每个方法都自己开事务
    （`with self._uow().begin()`），而本类的每个方法都收到执行器传进来的 `tx`。
    两者的事务语义相反，继承会把"谁开事务"这件事变模糊。共用的是
    `hash_password` / `verify_password` 与 `_utcnow`，那些是内核件。
    """

    # ======================================================================
    # 读：账号列表
    # ======================================================================

    def list_users(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> cap.HandlerResult:
        """账号列表。

        `path_params` / `query` 必须**逐字命名**声明：执行器只在处理器签名里真的有
        这个名字时才传（`runner._call_service` 的 `accepts` 裁剪）。写成 `**_` 的
        后果是筛选与分页静默失效——页面永远只看得到第一页且筛不出东西。
        """
        from fpa.domains._base import Page

        ctx.require("auth.user.manage")
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))

        where = ["1=1"]
        values: list[Any] = []
        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            where.append("(u.username LIKE %s OR u.display_name LIKE %s)")
            values.extend([f"%{keyword}%", f"%{keyword}%"])
        status = str(params.get("status") or "").strip()
        if status:
            where.append("u.status = %s")
            values.append(status)
        else:
            # 未指定状态时**排除已注销**（与 master_data 的"客户端没给 status 时排除
            # 若干码"同一口径）。理由：`retired` 不是可设置状态（`SETTABLE_USER_STATUSES`
            # 只有 active/disabled），退役账号留在默认列表里只会把真正在用的账号挤到第二页
            # —— 实测：收尾清理后账号页第一页 20 行里 15 行是"已注销"的探针账号。
            # 需要查它们时，筛选条里的「已注销」可以显式选中。
            where.append("u.status <> %s")
            values.append("retired")
        role_code = str(params.get("role_code") or "").strip()
        if role_code:
            # 用 EXISTS 而不是 JOIN：一个账号可能有多个角色，JOIN 会把同一行复制多份，
            # 于是分页的 `total` 与页内行数都偏大。这类错误只在"某人恰好有两个角色"
            # 时才出现，而那时没人会想到是 JOIN 的锅。
            where.append(
                "EXISTS (SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id "
                "WHERE ur.user_id = u.id AND r.code = %s)"
            )
            values.append(role_code)

        clause = " AND ".join(where)
        total = int(
            tx.query_scalar(f"SELECT COUNT(*) FROM users AS u WHERE {clause}", values) or 0
        )
        rows = tx.query_all(
            "SELECT u.id, u.username, u.display_name, u.status, u.must_change_password, "
            "       u.last_login_at, u.locked_until, u.created_at, u.updated_at, u.row_version "
            f"FROM users AS u WHERE {clause} ORDER BY u.id DESC LIMIT %s OFFSET %s",
            [*values, page.size, page.offset],
        )
        ids = [int(row["id"]) for row in rows]
        roles_map, scopes_map = self._grants_for(tx, ids)
        items = []
        for row in rows:
            item = self._decorate_user(
                row,
                roles_map.get(int(row["id"]), []),
                scopes_map.get(int(row["id"]), []),
            )
            item["allowed_actions"] = self._user_actions(ctx, int(row["id"]))
            items.append(item)
        return cap.HandlerResult(data=page.to_result(items, total), message="")

    def get_user(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        ctx.require("auth.user.manage")
        user_id = self._path_id(path_params, "user_id", "账号")
        row = self.load_user(tx, record_id=user_id)
        if row is None:
            raise not_found("账号")
        row["allowed_actions"] = self._user_actions(ctx, user_id)
        return cap.HandlerResult(data={"record": row})

    def list_roles(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> cap.HandlerResult:
        """返回启用角色，供账号授权下拉使用。"""
        from fpa.domains._base import Page

        ctx.require("auth.user.manage")
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))
        total = int(tx.query_scalar("SELECT COUNT(*) FROM roles WHERE status='active'") or 0)
        rows = tx.query_all(
            "SELECT id, code, name, description, status FROM roles "
            "WHERE status='active' ORDER BY id LIMIT %s OFFSET %s",
            (page.size, page.offset),
        )
        items = []
        for row in rows:
            item = dict(row)
            item["status_label"] = STATUS_LABELS.get(str(row.get("status") or ""), str(row.get("status") or ""))
            item["allowed_actions"] = self._role_actions(ctx)
            items.append(item)
        return cap.HandlerResult(data=page.to_result(items, total), message="")

    def get_role(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        ctx.require("auth.user.manage")
        role_id = self._path_id(path_params, "role_id", "角色")
        row = self.load_role(tx, record_id=role_id)
        if row is None:
            raise not_found("角色")
        row["status_label"] = STATUS_LABELS.get(str(row.get("status") or ""), str(row.get("status") or ""))
        row["allowed_actions"] = self._role_actions(ctx)
        return cap.HandlerResult(data={"record": row})

    def list_scopes(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> cap.HandlerResult:
        """返回启用数据范围，供账号授权下拉使用。"""
        from fpa.domains._base import Page

        ctx.require("auth.user.manage")
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))
        total = int(tx.query_scalar("SELECT COUNT(*) FROM data_scopes WHERE status='active'") or 0)
        rows = tx.query_all(
            "SELECT s.id, s.code, s.name, s.scope_type, s.status, "
            "       s.farm_id, s.area_id, s.pond_id, "
            "       f.name AS farm_name, a.name AS area_name, p.name AS pond_name "
            "FROM data_scopes AS s "
            "LEFT JOIN farms AS f ON f.id = s.farm_id "
            "LEFT JOIN areas AS a ON a.id = s.area_id "
            "LEFT JOIN ponds AS p ON p.id = s.pond_id "
            "WHERE s.status='active' ORDER BY s.id LIMIT %s OFFSET %s",
            (page.size, page.offset),
        )
        return cap.HandlerResult(
            data=page.to_result([self._decorate_scope(row) for row in rows], total),
            message="",
        )

    # ======================================================================
    # 写：新建账号 / 启停 / 授权 / 角色权限
    # ======================================================================

    def create_user(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        username: f_str("登录名", required=True, max_length=64),
        display_name: f_str("姓名", required=True, max_length=120),
        initial_password: f_str("初始密码", required=True, max_length=128),
        role_ids: f_int_list(
            "角色", required=True, max_length=32,
            ref=RefTarget("access_role", "name"),
        ),
        scope_ids: f_int_list(
            "数据范围", required=True, max_length=32,
            ref=RefTarget("access_scope", "name"),
        ),
    ) -> cap.HandlerResult:
        """新建账号。

        ## 唯一性由 `username` 承担，不由手机号

        registry §2.4 依据早期版本的 `uq_users_phone` 唯一键说"`idempotent=true` 是
        强制要求，靠唯一键兜底"。本版的唯一键是 `uq_users_username`（001 迁移），
        结论不变、承载列变了：**并发下唯一可信的判定仍然是数据库唯一键**，
        而 `kernel/uow.py::translate_mysql_error` 已把 errno 1062 统一翻译成 `CONFLICT`。

        ## 初始口令走**同一份**强度判定

        `kernel/password.py::password_problem` 同时服务本能力与 `auth.password.change`。
        两处各写一遍的话，管理员可以设一个用户自己改不成的口令。

        ## `must_change_password`

        置 1：初始口令是管理员"知道"的，首次登录必须换掉。这个字段是 001 迁移里
        就有的列，不是新设计——早期版本的 `must_change_password` 同样服务这件事。
        """
        ctx.require("auth.user.manage")
        role_ids = self._require_ids(role_ids, "role_ids", "角色")
        scope_ids = self._normalize_ids(scope_ids, "scope_ids", "数据范围")
        self._check_roles_active(tx, role_ids)
        self._ensure_can_assign_super_admin(tx, ctx, role_ids)
        self._check_scopes_active(tx, scope_ids)

        reason = password_problem(str(initial_password))
        if reason is not None:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                f"初始密码{reason}",
                data={"field": "initial_password"},
            )

        try:
            tx.execute(
                "INSERT INTO users (username, display_name, password_hash, status, "
                "                   must_change_password, created_at, updated_at) "
                "VALUES (%s,%s,%s,'active',1,%s,%s)",
                (
                    str(username).strip(),
                    str(display_name).strip(),
                    hash_password(str(initial_password)),
                    _utcnow(),
                    _utcnow(),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            # 唯一键 `uq_users_username`。不变量在做"编码在范围内唯一"，
            # 这里兜的是**并发**下两个请求同时通过预检的情形。
            if "uq_users_username" in str(exc):
                raise DomainError(
                    ErrorCode.CONFLICT,
                    f"登录名「{username}」已存在",
                    data={"field": "username"},
                ) from exc
            raise

        user_id = tx.last_insert_id()
        self._replace_grants(tx, user_id=user_id, role_ids=role_ids, scope_ids=scope_ids)
        row = self.load_user(tx, record_id=user_id)
        assert row is not None
        return cap.HandlerResult(
            data={"record": row},
            resource_id=user_id,
            message=f"已新建账号「{display_name}」",
        )

    def set_user_status(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None,
        status: f_enum("账号状态", STATUS_CHOICES, required=True),
        reason: text("变更原因", required=True, max_length=500),
    ) -> cap.HandlerResult:
        """启用 / 停用账号。

        **不允许停用自己**：那不是"多一层保护"，而是把系统锁死的唯一确定方式——
        最后一个管理员把自己停用之后，没有任何能力能把他改回来（本版没有
        `access.user.status` 的越权入口，也不该有）。
        """
        ctx.require("auth.user.manage")
        user_id = self._path_id(path_params, "user_id", "账号")
        if user_id == ctx.actor.user_id:
            raise DomainError(
                ErrorCode.CONFLICT,
                "不能变更自己的账号状态",
                data={"field": "status"},
            )
        row = tx.query_one("SELECT id, status FROM users WHERE id=%s", (user_id,))
        if row is None:
            raise not_found("账号")
        target = str(status)
        if target not in SETTABLE_USER_STATUSES:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                f"账号状态只能是 {'、'.join(SETTABLE_USER_STATUSES)}",
                data={"field": "status", "allowed": list(SETTABLE_USER_STATUSES)},
            )
        if str(row["status"]) == target:
            raise DomainError(
                ErrorCode.CONFLICT,
                f"该账号已经是「{STATUS_LABELS.get(target, target)}」状态",
                data={"field": "status"},
            )

        tx.execute(
            "UPDATE users SET status=%s, row_version=row_version+1, updated_at=%s WHERE id=%s",
            (target, _utcnow(), user_id),
        )
        if target == "disabled":
            # 停用必须**同时**撤销会话。不撤销的话该用户手上的会话在空闲超时内
            # （`AccessService` 的 30 分钟滑动窗口）仍然可用——`resolve_session` 会
            # 因为 `_load()` 查到 status != active 而拒绝，所以这不是安全漏洞，
            # 但它会让"停用了但对方还在线"的现象多存在半小时，而管理员以为自己
            # 已经处理完了。显式撤销让结果与预期一致。
            tx.execute(
                "UPDATE sessions SET revoked_at=%s WHERE user_id=%s AND revoked_at IS NULL",
                (_utcnow(), user_id),
            )
        updated = self.load_user(tx, record_id=user_id)
        assert updated is not None
        return cap.HandlerResult(
            data={"record": updated, "reason": str(reason)},
            resource_id=user_id,
            message=f"账号已{STATUS_LABELS.get(target, target)}",
        )

    def replace_user_grants(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None,
        role_ids: f_int_list(
            "角色", required=True, max_length=32,
            ref=RefTarget("access_role", "name"),
        ),
        scope_ids: f_int_list(
            "数据范围", required=True, max_length=32,
            ref=RefTarget("access_scope", "name"),
        ),
    ) -> cap.HandlerResult:
        """调整账号的角色与数据范围（**整集合替换**）。

        registry §2.4 原文："本版保留整集合替换语义（PATCH 的增量语义与前端的
        表单模型不匹配）"。增量语义下"取消勾选"无法表达——调用方只能传"要加的"，
        永远不知道该删哪个。

        这也是它在状态机之外仍被标 `kind="action"` 而**不是** `update` 的原因：
        它不接收"当前值 + 新值"，只接收最终态。
        """
        ctx.require("auth.user.manage")
        self._require_super_admin(ctx)
        user_id = self._path_id(path_params, "user_id", "账号")
        role_ids = self._require_ids(role_ids, "role_ids", "角色")
        scope_ids = self._normalize_ids(scope_ids, "scope_ids", "数据范围")
        self._check_roles_active(tx, role_ids)
        self._check_scopes_active(tx, scope_ids)
        if tx.query_one("SELECT id FROM users WHERE id=%s", (user_id,)) is None:
            raise not_found("账号")

        self._replace_grants(tx, user_id=user_id, role_ids=role_ids, scope_ids=scope_ids)
        row = self.load_user(tx, record_id=user_id)
        assert row is not None
        return cap.HandlerResult(
            data={"record": row},
            resource_id=user_id,
            message="已更新账号的角色与数据范围",
        )

    def replace_role_permissions(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None,
        permission_codes: f_str_list("权限", required=True, max_length=256),
    ) -> cap.HandlerResult:
        """调整角色权限（整集合替换）。

        ## 合法值集合 = `REGISTRY` 的能力名，**不是** `permissions` 表

        registry §2.4 把这条列为"关键改进"，原文：

            早期版本校验"权限码是否存在于 `permissions` 表"，但 `permissions` 表是迁移里
            手工 INSERT 的；新系统的合法值集合 = Capability Registry 的 `name` 集合，
            由注册表机械派生，**不可能出现"权限码存在但无对应能力"**。

        所以这里查的是注册表。**但 `role_permissions` 存的是 `permission_id`**，
        所以还要把能力名映射到 `permissions.id` —— 后者由
        `tools/seed_permissions.py` 从 `Capability.required_permission` 派生。
        两个集合不等价（一个码可被多条能力共用：`cost.view` ← `cost.entry.list` +
        `cost.summary`），因此判定顺序是：

            能力名 → 该能力的 `required_permission` → `permissions.code` → id

        中间那一跳是**必要**的：授权的主体是权限码而不是能力名
        （`AccessService._load` 加载的是 `permissions.code`）。
        漏掉它就会给 `role_permissions` 塞进一个不存在的码，而症状是
        "配了权限但用户还是没权限"。
        """
        ctx.require("auth.role.manage")
        self._require_super_admin(ctx)
        role_id = self._path_id(path_params, "role_id", "角色")
        row = tx.query_one("SELECT id, code, name FROM roles WHERE id=%s", (role_id,))
        if row is None:
            raise not_found("角色")

        codes = self._require_permission_codes(permission_codes)
        resolved, missing = self._permission_codes_to_ids(tx, codes)
        if missing:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "以下权限码尚未种入库，请先运行 `python tools/seed_permissions.py`",
                data={"field": "permission_codes", "missing": missing},
            )

        tx.execute("DELETE FROM role_permissions WHERE role_id=%s", (role_id,))
        for permission_id in sorted(resolved):
            tx.execute(
                "INSERT INTO role_permissions (role_id, permission_id) VALUES (%s,%s)",
                (role_id, permission_id),
            )
        record = {
            "id": role_id,
            "code": str(row["code"]),
            "name": str(row["name"]),
            "permission_codes": sorted(codes),
        }
        return cap.HandlerResult(
            data={"record": record},
            resource_id=role_id,
            message=f"已更新角色「{row['name']}」的权限（{len(resolved)} 项）",
        )

    # ======================================================================
    # 回读（`loader=` 指向它；见 `docs/WRITE_CONTRACT.md` 规则 2）
    # ======================================================================

    @classmethod
    def load_user(cls, tx: UnitOfWork, *, record_id: int, scope: Scope | None = None) -> dict[str, Any] | None:
        """按主键回读一个账号（含角色与数据范围）。

        这是 `access.user.create` / `.status` / `.grants` 三条写能力的 `loader=`，
        签名与 `runner._reload_after` 的调用逐字一致：``(tx, *, scope, record_id)``。

        ## 为什么**不**在这里做数据范围判定

        `users` / `roles` 是**系统级**对象，它们的 `Capability.scope` 是
        `ScopePolicy.none()`（registry §1.2 的 scope 列全是 `none`）——
        "系统级权限不是业务能力码，不套用同名派生规则"（§1.2 依据）。
        给它们套上 `created_by` 之类的行级谓词会让"第一个管理员看不到第二个管理员"。

        这不是放宽：进入这三条能力的门槛是 `auth.user.manage`，
        由执行器的第二层与服务的第一行 `ctx.require` 各查一次。
        """
        row = tx.query_one(
            "SELECT id, username, display_name, status, must_change_password, "
            "       last_login_at, locked_until, created_at, updated_at, row_version "
            "FROM users WHERE id=%s",
            (record_id,),
        )
        if row is None:
            return None
        roles_map, scopes_map = cls._grants_for(tx, [record_id])
        return cls._decorate_user(
            row, roles_map.get(record_id, []), scopes_map.get(record_id, [])
        )

    @classmethod
    def load_role(cls, tx: UnitOfWork, *, record_id: int, scope: Scope | None = None) -> dict[str, Any] | None:
        """按主键回读一个角色（含它的权限码）。

        这是 `access.role.permissions` 的 `loader=`。**必须与 `load_user` 分开**：
        该能力的 `resource` 是 `access_role`，而 `resource_id` 是**角色 id**。
        共用 `load_user` 会拿角色 id 去查用户表——通常回读为 `None`（撞上"回读不到
        该记录"），**而如果那个 id 恰好撞上某个真实用户，它会回读到一个错误但确实
        存在的行并判定写入成功**。`Capability.loader` 的 docstring 把这一类
        "成功写入配上不属于它的回读结果"列为比报错严重得多的形态，
        所以这里逐条声明、不从名字猜。
        """
        row = tx.query_one("SELECT id, code, name, description, status FROM roles WHERE id=%s", (record_id,))
        if row is None:
            return None
        permissions = tx.query_all(
            "SELECT p.code FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id "
            "WHERE rp.role_id=%s ORDER BY p.code",
            (record_id,),
        )
        codes = [str(item["code"]) for item in permissions]
        # 回读给出的是**权限码**（`role_permissions` 存 id，但对外语义是码）；
        # 同时在库里反查这些码对应哪些能力，让"这个角色实际能调什么"可见。
        # 两列并存不是冗余：一个码可以服务多条能力（`cost.view` ← `cost.entry.list`
        # + `cost.summary`），因此从码推不出能力集合，只能查注册表。
        from fpa.kernel.capability import REGISTRY

        code_set = set(codes)
        capabilities = sorted(
            item.name
            for item in REGISTRY.all()
            if item.required_permission is not None and item.required_permission in code_set
        )
        return {
            "id": int(row["id"]),
            "code": str(row["code"]),
            "name": str(row["name"]),
            "description": str(row.get("description") or ""),
            "status": str(row["status"]),
            "permission_codes": codes,
            "capability_names": capabilities,
        }

    # ======================================================================
    # 内部
    # ======================================================================

    @staticmethod
    def _require_super_admin(ctx) -> None:
        """授权变更必须由服务端解析出的 ``super_admin`` 角色执行。"""
        if not ctx.actor.is_super_admin:
            raise DomainError(
                ErrorCode.FORBIDDEN,
                "只有超级管理员可以执行该授权操作",
                data={"required_role": "super_admin"},
            )

    @staticmethod
    def _user_actions(ctx, user_id: int) -> list[str]:
        actions = ["view"]
        if int(user_id) != int(ctx.actor.user_id):
            actions.append("status")
        if ctx.actor.is_super_admin:
            actions.append("grants")
        return actions

    @staticmethod
    def _role_actions(ctx) -> list[str]:
        actions = ["view"]
        if ctx.actor.is_super_admin and ctx.actor.has("auth.role.manage"):
            actions.append("permissions")
        return actions

    @staticmethod
    def _decorate_scope(row: dict[str, Any]) -> dict[str, Any]:
        decorated = dict(row)
        scope_type = str(row.get("scope_type") or "")
        decorated["scope_type_label"] = SCOPE_TYPE_LABELS.get(scope_type, scope_type)
        decorated["status_label"] = STATUS_LABELS.get(
            str(row.get("status") or ""), str(row.get("status") or "")
        )
        if scope_type == "personal":
            target = "本人创建的数据"
        else:
            target_name = row.get(f"{scope_type}_name")
            target_id = row.get(f"{scope_type}_id")
            label = SCOPE_TYPE_LABELS.get(scope_type, scope_type)
            target = f"{label}：{target_name or ('#' + str(target_id))}"
        decorated["scope_target_label"] = target
        decorated["allowed_actions"] = []
        return decorated

    @classmethod
    def _ensure_can_assign_super_admin(cls, tx: UnitOfWork, ctx, role_ids: list[int]) -> None:
        """普通管理员可以建普通账号，但不能借新建账号完成自我提权。"""
        if ctx.actor.is_super_admin:
            return
        placeholders = ",".join(["%s"] * len(set(role_ids)))
        row = tx.query_one(
            f"SELECT id FROM roles WHERE id IN ({placeholders}) AND code=%s LIMIT 1",
            [*sorted(set(role_ids)), "super_admin"],
        )
        if row is not None:
            cls._require_super_admin(ctx)

    @staticmethod
    def _decorate_user(
        row: dict[str, Any],
        roles: list[dict[str, Any]],
        scopes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """派生前端需要的字段。与各域 `_decorate` 同一口径。"""
        decorated = dict(row)
        decorated["status_label"] = STATUS_LABELS.get(str(row.get("status") or ""), str(row.get("status") or ""))
        decorated["version"] = int(row.get("row_version") or 1)
        decorated["must_change_password"] = bool(row.get("must_change_password"))
        decorated["role_ids"] = [int(item["id"]) for item in roles]
        decorated["role_codes"] = [str(item["code"]) for item in roles]
        decorated["role_names"] = [str(item["name"]) for item in roles]
        decorated["scope_ids"] = [int(item["id"]) for item in scopes]
        decorated["scope_names"] = [str(item["name"]) for item in scopes]
        for key in ("created_at", "updated_at", "last_login_at", "locked_until"):
            if decorated.get(key) is not None:
                decorated[key] = str(decorated[key])
        # `password_hash` 永远不离开服务层。**不是靠调用方记得不选它**——
        # 上面的 SELECT 逐列列出，这一行是第二道闸：将来有人改成 `SELECT *`
        # 也不会把哈希带出去。
        decorated.pop("password_hash", None)
        return decorated

    @staticmethod
    def _grants_for(
        tx: UnitOfWork, user_ids: list[int]
    ) -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[dict[str, Any]]]]:
        """**两次批量查询**取回这批账号的角色与数据范围。

        逐行查会变成 N+1（一页 20 行 = 41 次查询），而列表页是本项目明确要求
        批量形态的场景（`docs/ROLLOUT_CONTRACT.md` §2.0）。
        空入参**不发查询**——那是最容易被忽略的一处，且空 `IN ()` 在 MySQL 上是语法错。
        """
        if not user_ids:
            return {}, {}
        placeholders = ",".join(["%s"] * len(user_ids))
        role_rows = tx.query_all(
            "SELECT ur.user_id, r.id, r.code, r.name "
            "FROM user_roles ur JOIN roles r ON r.id = ur.role_id "
            f"WHERE ur.user_id IN ({placeholders}) ORDER BY r.code",
            user_ids,
        )
        scope_rows = tx.query_all(
            "SELECT uds.user_id, s.id, s.code, s.name, s.scope_type "
            "FROM user_data_scopes uds JOIN data_scopes s ON s.id = uds.scope_id "
            f"WHERE uds.user_id IN ({placeholders}) ORDER BY s.code",
            user_ids,
        )
        return _group(role_rows, "user_id"), _group(scope_rows, "user_id")

    @staticmethod
    def _replace_grants(
        tx: UnitOfWork, *, user_id: int, role_ids: list[int], scope_ids: list[int]
    ) -> None:
        """整集合替换角色与数据范围。

        与 `AccessService.assign_roles_and_scopes` 的语义逐字一致（真集合替换 +
        范围为空时补默认个人范围），但**复用执行器的事务**而不自己开一个。
        两处语义必须一样，否则"管理端改的"与"服务端读的"会用两套规则；
        已在本文件的单测里用"设成空集合 → 落到 personal-default"锁住这一条。
        """
        tx.execute("DELETE FROM user_roles WHERE user_id=%s", (user_id,))
        for role_id in sorted(set(role_ids)):
            tx.execute(
                "INSERT INTO user_roles (user_id, role_id) VALUES (%s,%s)", (user_id, role_id)
            )
        tx.execute("DELETE FROM user_data_scopes WHERE user_id=%s", (user_id,))
        effective = list(dict.fromkeys(scope_ids))
        if not effective:
            # 范围为空 → 补默认个人范围。否则该用户下次访问业务数据会
            # `DATA_SCOPE_UNRESOLVED`，而管理员以为自己只是"暂时没勾选"。
            default = tx.query_one(
                "SELECT id FROM data_scopes WHERE code='personal-default' AND status='active'"
            )
            if default is not None:
                effective = [int(default["id"])]
        for scope_id in effective:
            tx.execute(
                "INSERT INTO user_data_scopes (user_id, scope_id) VALUES (%s,%s)",
                (user_id, scope_id),
            )

    @staticmethod
    def _path_id(path_params: dict[str, Any] | None, key: str, what: str) -> int:
        raw = (path_params or {}).get(key)
        if raw is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"{what}操作需要 path_params 传 {key}（能力声明的路径模板里应有 {{{key}}}）",
            )
        return int(raw)

    @staticmethod
    def _require_ids(raw: Any, field: str, label: str) -> list[int]:
        """把入参规范成"**非空**的整数列表"。

        `role_ids` 在 registry §2.4 里是 **required + ≥1**。这里显式判定而不是
        依赖 schema：`integer[]` 的 schema 只能保证"是个整数数组"，
        "至少一个"是业务规则。空数组是最容易被漏掉的一种非法输入——
        它看起来像"合法的空值"，而后果是账号建出来没有任何角色、
        于是该用户虽然能登录却做不了任何事，且没有任何地方会报错。
        """
        values = AdminService._normalize_ids(raw, field, label)
        if not values:
            raise DomainError(ErrorCode.FIELD_INVALID, f"请至少选择一个{label}", data={"field": field})
        return values

    @staticmethod
    def _normalize_ids(raw: Any, field: str, label: str) -> list[int]:
        """规范成整数列表；**空数组是合法的**。

        为什么 `scope_ids` 用这个而不是 `_require_ids`：仓库里"范围为空时补默认个人
        范围"的语义有**两处**实现（`AccessService.assign_roles_and_scopes` 与
        `AdminService._replace_grants`），两处都只在空集时兜底；若在这里拒绝空集，
        那条兜底就成了死代码，而"取消勾选全部范围"也从"退回个人范围"变成一次 422。
        所以这里只收窄类型，不判空——判空的边界写在一处（`_replace_grants`）。
        """
        if raw is None:
            raise DomainError(ErrorCode.FIELD_INVALID, f"{label}不能为空", data={"field": field})
        values: list[int] = []
        for item in raw:
            try:
                values.append(int(item))
            except (TypeError, ValueError) as exc:
                raise DomainError(
                    ErrorCode.FIELD_INVALID, f"{label}必须是整数列表", data={"field": field}
                ) from exc
        return values

    @staticmethod
    def _require_permission_codes(raw: Any) -> list[str]:
        """校验 `permission_codes` 是**已注册的能力名**集合（registry §2.4 的关键改进）。"""
        if raw is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "请提供权限清单（可为空数组）", data={"field": "permission_codes"}
            )
        from fpa.kernel.capability import REGISTRY

        codes: list[str] = []
        unknown: list[str] = []
        for item in raw:
            code = str(item).strip()
            if not code:
                continue
            if REGISTRY.find(code) is None:
                unknown.append(code)
                continue
            if code not in codes:
                codes.append(code)
        if unknown:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "以下权限码不是已注册的能力："
                + "、".join(sorted(unknown)[:10])
                + "（合法值集合 = 能力清单，不是 permissions 表）",
                data={"field": "permission_codes", "unknown": sorted(unknown)},
            )
        return codes

    @staticmethod
    def _permission_codes_to_ids(
        tx: UnitOfWork, capability_names: list[str]
    ) -> tuple[set[int], list[str]]:
        """能力名 → `permissions.id`（经该能力的 `required_permission`）。

        返回值第二项是"**所需的权限码还没入库**"的那些码。这不是用户输入错误，
        而是**种子没跑**（`tools/seed_permissions.py`）。两者必须分开报：
        一个是"你填错了"，一个是"环境没准备好"，修法完全不同。
        """
        from fpa.kernel.capability import REGISTRY

        wanted: set[str] = set()
        for name in capability_names:
            capability = REGISTRY.find(name)
            if capability is None or capability.required_permission is None:
                # `required_permission=None` 的语义是"无需权限，仅需登录"
                # （registry §1.1：`auth.login` 一类）。它**没有码可授权**，
                # 因此为一角色勾选它不产生任何效果——静默忽略而不是报错：
                # 报错会让管理员无法保存一个"看起来勾了也没事"的表单。
                continue
            wanted.add(capability.required_permission)
        if not wanted:
            return set(), []
        placeholders = ",".join(["%s"] * len(wanted))
        rows = tx.query_all(
            f"SELECT id, code FROM permissions WHERE code IN ({placeholders})",
            sorted(wanted),
        )
        found = {str(row["code"]): int(row["id"]) for row in rows}
        return {found[code] for code in wanted if code in found}, sorted(wanted - set(found))

    @staticmethod
    def _check_roles_active(tx: UnitOfWork, role_ids: list[int]) -> None:
        placeholders = ",".join(["%s"] * len(role_ids))
        rows = tx.query_all(
            f"SELECT id, status FROM roles WHERE id IN ({placeholders})", sorted(set(role_ids))
        )
        by_id = {int(row["id"]): str(row["status"]) for row in rows}
        missing = [item for item in role_ids if item not in by_id]
        if missing:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                f"角色不存在：{missing}",
                data={"field": "role_ids"},
            )
        disabled = [item for item in role_ids if by_id[item] != "active"]
        if disabled:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                f"以下角色已停用，不能授予：{disabled}",
                data={"field": "role_ids"},
            )

    @staticmethod
    def _check_scopes_active(tx: UnitOfWork, scope_ids: list[int]) -> None:
        """数据范围必须存在、启用，且**分租键齐全**。

        最后一条是本项目对早期版本的核心修正（`kernel/scope.py` 的 `ScopeEntry.from_row`）：
        早期版本 `farm` 型范围因为 `data_scopes` 没有 `farm_id` 列而**静默退化成空集**，
        用户看到 0 行数据却不报错。这里在**授权时**就挡住：
        把一条解析不出范围的范围记录授给用户，等于给他配了"看不到任何数据"。
        """
        from fpa.kernel.scope import SCOPE_COLUMN, ScopeType

        placeholders = ",".join(["%s"] * len(scope_ids))
        rows = tx.query_all(
            "SELECT id, code, scope_type, status, farm_id, area_id, pond_id "
            f"FROM data_scopes WHERE id IN ({placeholders})",
            sorted(set(scope_ids)),
        )
        by_id = {int(row["id"]): row for row in rows}
        missing = [item for item in scope_ids if item not in by_id]
        if missing:
            raise DomainError(
                ErrorCode.FIELD_INVALID, f"数据范围不存在：{missing}", data={"field": "scope_ids"}
            )
        problems: list[str] = []
        for scope_id in scope_ids:
            row = by_id[scope_id]
            if str(row["status"]) != "active":
                problems.append(f"#{scope_id} 已停用")
                continue
            scope_type = ScopeType(str(row["scope_type"]))
            if scope_type is ScopeType.PERSONAL:
                continue  # 按 created_by 判定，没有分租键列
            column = SCOPE_COLUMN[scope_type]
            if row.get(column) is None:
                problems.append(f"#{scope_id}（{row['code']}）缺少分租键 {column}")
        if problems:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "以下数据范围不可用：" + "；".join(problems),
                data={"field": "scope_ids"},
            )


# ---------------------------------------------------------------------------
# 确认卡片的可读化（`Capability.confirmation_labels` 的落点）
#
# ## 为什么需要它
#
# 提权类能力的确认卡片原先只有**裸 id**（实测 `access.user.grants`）：
#
#     target : user_id=7
#     rows   : 角色 = [9]     数据范围 = [2]
#     impact : 写入业务数据
#
# 用户名、姓名、角色名、范围名**一个都不在卡片上**；`impact` 还把"提权"说成了
# "写入业务数据"。而**提权恰恰是最需要人看懂"给谁、加了什么"的场景** ——
# HITL 的全部价值就在这一眼，用户看不到名字，确认闸门就退化成点一下的仪式。
#
# ## 为什么由本域提供、而不是内核自己查
#
# 解析要读 `users` / `roles` / `data_scopes` / `permissions` —— **全是本域的表**。
# 内核不认识它们、也不该认识（分层）。所以约定与 `loader=` 完全一致：
# **域提供可调用、内核在签发卡片前调一次**。
#
# ## 失败不阻断
#
# 内核侧已经吞掉异常并退回裸 id（`runner._confirmation_labels`）。这里同样只做
# 尽力而为的解析：查不到的 id 如实写成"已不存在"，绝不让美化把卡片搞没。
# ---------------------------------------------------------------------------

#: 需要"id 列表 -> 名字列表"的字段：字段名 -> (表, 取名字的列)
_CARD_ID_LIST_LOOKUPS: dict[str, tuple[str, str]] = {
    "role_ids": ("roles", "name"),
    "scope_ids": ("data_scopes", "name"),
}


def _current_assignments(
    tx: UnitOfWork, *, user_id: Any, join_table: str, fk_column: str,
    target_table: str, name_column: str,
) -> list[str]:
    """读该用户**当前**的角色名 / 数据范围名（用于卡片上的"变更前"）。"""
    uid = _card_int(user_id)
    if uid is None:
        return []
    rows = tx.query_all(
        f"SELECT t.{name_column} AS label FROM {join_table} AS j "
        f"JOIN {target_table} AS t ON t.id = j.{fk_column} "
        "WHERE j.user_id = %s ORDER BY t.id",
        (uid,),
    )
    return [str(r["label"]) for r in (rows or [])]


def _card_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def confirmation_labels(tx: UnitOfWork, payload: dict[str, Any]) -> dict[str, Any]:
    """把提权卡片的参数解析成业务可读的 {target, rows}。

    契约见 `fpa.kernel.capability.Capability.confirmation_labels`：
    返回 ``{"target": str, "rows": {字段名: 文本}}``，未覆盖的字段由内核按原样渲染。
    """
    rows: dict[str, str] = {}
    target = ""

    # ① 单个对象：`user_id` / `role_id` 出现在 target（它们是路径参数，不在 fields 里）
    user_id = _card_int(payload.get("user_id"))
    if user_id is not None:
        row = tx.query_one(
            "SELECT username, display_name FROM users WHERE id=%s", (user_id,)
        )
        if row is not None:
            target = f"{row['display_name']}（{row['username']}）"
        else:
            target = f"账号 #{user_id}（已不存在）"

    role_id = _card_int(payload.get("role_id"))
    if role_id is not None:
        row = tx.query_one("SELECT code, name FROM roles WHERE id=%s", (role_id,))
        if row is not None:
            target = f"{row['name']}（{row['code']}）"
        else:
            target = f"角色 #{role_id}（已不存在）"

    # ② 新建账号没有路径参数：用即将创建出来的那个人的名字当 target
    if not target and payload.get("display_name"):
        username = str(payload.get("username") or "").strip()
        target = f"{payload['display_name']}（{username}）" if username else str(
            payload["display_name"]
        )

    # ③ id 列表：`role_ids` / `scope_ids`
    #    先读"变更前"，供下面渲染 `变更前 → 变更后`（覆盖语义对用户必须可见）
    current_roles = _current_assignments(
        tx, user_id=payload.get("user_id"), join_table="user_roles",
        fk_column="role_id", target_table="roles", name_column="name",
    )
    current_scopes = _current_assignments(
        tx, user_id=payload.get("user_id"), join_table="user_data_scopes",
        fk_column="scope_id", target_table="data_scopes", name_column="name",
    )
    for key, (table, name_column) in _CARD_ID_LIST_LOOKUPS.items():
        raw_ids = payload.get(key)
        if not isinstance(raw_ids, (list, tuple)) or not raw_ids:
            continue
        ids = [i for i in (_card_int(x) for x in raw_ids) if i is not None]
        if not ids:
            continue
        placeholders = ",".join(["%s"] * len(ids))
        found = tx.query_all(
            f"SELECT {name_column} AS label FROM {table} "
            f"WHERE id IN ({placeholders}) ORDER BY id",
            ids,
        )
        labels = [str(r["label"]) for r in (found or [])]
        text = "、".join(labels)
        missing = len(ids) - len(labels)
        if missing > 0:
            text = (text + "、" if text else "") + f"（另有 {missing} 项已不存在）"
        text = text or f"{len(ids)} 项"

        # ★ 这两行是**覆盖**语义，不是追加：卡片必须同时给出"变更前"。
        #   实测：只显示变更后 ⇒ 模型传 [质检核验员] 时，用户看不出"销售员会被移除"，
        #   点确认后原角色被静默覆盖。渲染成 `变更前 → 变更后` 才与真实语义一致。
        if key == "role_ids":
            current = current_roles
        else:
            current = current_scopes
        if current:
            if sorted(current) == sorted(labels):
                text = "、".join(current) + "（不变）"
            else:
                text = "、".join(current) + "  →  " + text
        rows[key] = text

    # ④ 权限码：解析成权限中文名，并给出总数（`access.role.permissions`）
    codes = payload.get("permission_codes")
    if isinstance(codes, (list, tuple)) and codes:
        wanted = [str(c) for c in codes]
        placeholders = ",".join(["%s"] * len(wanted))
        found = tx.query_all(
            f"SELECT code, name FROM permissions WHERE code IN ({placeholders}) ORDER BY code",
            wanted,
        )
        names = [
            # `permissions.name` 里有一部分**就是** code 本身（如 `pond.view`），
            # 拼成 `pond.view（pond.view）` 只是噪音；二者相同时只显示一次。
            (
                str(r["name"])
                if r.get("name") and str(r["name"]) != str(r["code"])
                else str(r["code"])
            )
            for r in (found or [])
        ]
        rows["permission_codes"] = "、".join(names) if names else "、".join(wanted)

    return {"target": target, "rows": rows}


__all__ = [
    "AdminService",
    "confirmation_labels",
    "SCOPE_TYPE_LABELS",
    "SETTABLE_USER_STATUSES",
    "STATUS_CHOICES",
    "STATUS_LABELS",
]
