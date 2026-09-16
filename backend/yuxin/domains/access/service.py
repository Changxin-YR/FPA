"""身份与会话：登录、会话解析、权限加载。

**边界**：本模块只回答"你是谁"，不回答"你能看哪些数据"——
后者在 `scope_resolver.py`，且**故意分开**，因为两者的失败语义不同：

    身份解析失败 → 401，必须拒绝（不知道是谁就不能做任何事）
    范围解析失败 → 保留身份成功，但在**业务操作处** fail-closed（403）

为什么不干脆让范围解析失败也导致 401：那样用户会看到"登录失败"，而真实原因是
"你的数据范围配置坏了"。用户会去重试密码、找管理员重置密码，而真正要修的是
`data_scopes` 表里少了一列。**错误信息指向错误的修复动作，比不报错还糟。**
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from yuxin.kernel.errors import DomainError, ErrorCode, unauthenticated
from yuxin.kernel.password import hash_password, needs_rehash, verify_password
from yuxin.kernel.uow import UnitOfWork

#: 会话令牌长度（URL 安全字符）。32 字节 ≈ 256 bit 熵。
TOKEN_BYTES = 32

#: 会话空闲超时（分钟）。
DEFAULT_IDLE_MINUTES = 30

#: 登录失败锁定阈值与时长。
LOCK_THRESHOLD = 5
LOCK_MINUTES = 15


def utcnow() -> datetime:
    """**无时区的 UTC 当前时间**。本域全部时间列的唯一来源。

    ## 为什么必须公开（而不是域内私有）

    `users.updated_at` / `sessions.revoked_at` / `sessions.expires_at` 在 001 迁移里
    都是**无时区** `DATETIME`。identity 域的改密要写同一批列，它需要一个**同口径**的
    "当前时间"——初版在 `domains/identity/capabilities.py` 里又写了一份
    `datetime.now(timezone.utc).replace(tzinfo=None)`。

    两个"当前时间"的后果不是"差几微秒"，而是**比较方向可能翻转**：
    "会话刚刚被撤销"与"撤销时间早于现在"会同时成立，而这类不一致只在
    跨越零点或时区设置变化时才显形——正是本项目最反对的"随机复现"形态。

    所以它从私有升格为公开：**跨子域复用的东西不能带下划线**，
    下划线暗示"这只是本模块的实现细节"，而复用者照着它再写一份就成了第二个真相。

    `_utcnow` 仍保留为别名：`access/admin.py` 与既有调用点都按那个名字引用，
    改名的收益（语义正确）不足以抵掉"再动一遍 8 处调用"的风险。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


#: 兼容别名。新代码请用 :func:`utcnow`。
_utcnow = utcnow


def hash_token(token: str) -> str:
    """会话令牌只存哈希。

    原始令牌绝不落库——这样即使数据库被读走，攻击者也无法直接用里面的值冒用会话。
    早期版本用的是同样的做法（`hash_session_token`），继承。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """一个已通过身份校验的用户。**权限与范围都来自服务端查询**。"""

    user_id: int
    username: str
    display_name: str
    status: str
    permissions: frozenset[str]
    role_codes: frozenset[str]
    session_hash: str
    must_change_password: bool = False

    def to_summary(self) -> dict[str, Any]:
        """**前端契约 `UserSummary` 的服务端唯一实现**（`/auth/login` 与 `/auth/me` 共用）。

        ## 为什么是"服务端适配前端那个类型"，而不是反过来

        这个形状原先叫 `to_me()`、返回 `{id, username, display_name, status,
        permissions, roles, must_change_password}` —— 它**从来没有匹配过任何一方**：
        前端 `common/api/models.ts` 的 `UserSummary` 要的是
        `{id, name, status, roles, permissions}`。

        裁决（负责人 / t7）：以 `UserSummary` 为准。理由：它是**唯一带类型的定义**，
        且已被守卫、会话 store、登录页、e2e 夹具四处共同消费 —— 它**事实上就是契约**。
        "展示字段由客户端决定"这句在这里尤其成立：`status` 是**复合口径**（见下），
        该给什么值由服务端判定比让前端二次拼装更可靠。

        ## `name` 来自 `display_name`，**不是别名**

        001 迁移里那一列叫 `display_name`，而契约字段叫 `name`。只给 `name`：
        同时给两个名字就是"两处描述同一件事"，而它们必然漂移。

        ## `status` 是**复合口径**，不是 `users.status` 的原值

        前端 `session.store.ts` 的 `PATH_BY_STATE` 按它跳转：

            must_change_password → '/auth/first-password'
            pending              → '/auth/pending'
            disabled             → '/auth/login'
            （其余）              → '/ponds'

        所以"需改密"必须从布尔列**折叠进** `status`。这是契约里最容易漏的一处：
        漏了它 `PATH_BY_STATE` 永远命中不到 `must_change_password`，
        首登用户会被放进业务页，而**系统以为自己跳过了改密**——静默，且只在首登出现。

        `retired` 折叠成 `disabled`，与 `admin.py::SETTABLE_USER_STATUSES`
        （本版只把 `active`/`disabled` 当可设置值）同口径。
        """
        if self.must_change_password:
            status = "must_change_password"
        elif self.status == "retired":
            status = "disabled"
        else:
            status = self.status
        return {
            "id": self.user_id,
            "name": self.display_name,
            "status": status,
            "roles": sorted(self.role_codes),
            "permissions": sorted(self.permissions),
        }


class AccessService:
    """身份与会话的读写。**自己管理事务**（因为它在业务事务之外）。"""

    def __init__(self, uow_factory: Any, *, idle_minutes: int = DEFAULT_IDLE_MINUTES) -> None:
        self._uow = uow_factory
        self._idle = timedelta(minutes=idle_minutes)

    # -- 登录 -----------------------------------------------------------------

    def login(
        self, *, username: str, password: str, ip_address: str = "", user_agent: str = ""
    ) -> tuple[str, AuthenticatedUser]:
        """校验凭据并建立会话。

        返回 ``(原始令牌, 用户)``。原始令牌只在这一次响应里出现。

        **为什么不区分"用户不存在"与"密码错误"**：两种情形返回同一个错误消息，
        避免把接口变成用户名枚举工具。代价是用户输错用户名时提示不够具体——
        这个代价值得付。
        """
        normalized = username.strip()
        if not normalized or not password:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "请输入账号和密码")

        with self._uow().begin() as tx:
            row = tx.query_one(
                """
                SELECT id, username, display_name, password_hash, status, must_change_password,
                       failed_login_count, locked_until
                FROM users WHERE username=%s
                """,
                (normalized,),
            )
            if row is None:
                raise self._bad_credentials()

            if row.get("locked_until") and row["locked_until"] > _utcnow():
                raise DomainError(
                    ErrorCode.RATE_LIMITED,
                    f"账号已因多次登录失败被临时锁定，请 {LOCK_MINUTES} 分钟后再试",
                )

            if row["status"] != "active":
                # 状态是业务事实，可以告知——它不是凭据信息
                reason = {
                    "pending": "账号正在审核中，请等待管理员通过",
                    "disabled": "账号已被停用，请联系管理员",
                    "retired": "账号已注销",
                }.get(str(row["status"]), "账号当前不可用")
                raise DomainError(ErrorCode.FORBIDDEN, reason)

            if not verify_password(password, str(row["password_hash"])):
                self._record_failure(tx, int(row["id"]), int(row.get("failed_login_count") or 0))
                raise self._bad_credentials()

            # 登录成功：清零失败计数；必要时升级哈希参数
            updates = ["failed_login_count=0", "locked_until=NULL", "last_login_at=%s"]
            params: list[Any] = [_utcnow()]
            if needs_rehash(str(row["password_hash"])):
                updates.append("password_hash=%s")
                params.append(hash_password(password))
            params.append(int(row["id"]))
            tx.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=%s", params)

            token = new_token()
            csrf_token = secrets.token_urlsafe(32)
            tx.execute(
                """
                INSERT INTO sessions
                  (user_id, token_hash, csrf_hash, csrf_token, ip_address, user_agent, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    int(row["id"]),
                    hash_token(token),
                    hash_token(csrf_token),
                    csrf_token,  # 原值：双提交需要它能被比对（见 web/security.py）
                    ip_address[:45],
                    user_agent[:255],
                    _utcnow() + self._idle,
                ),
            )
            user = self._load(tx, int(row["id"]), hash_token(token))

        return token, csrf_token, user

    @staticmethod
    def _bad_credentials() -> DomainError:
        return DomainError(ErrorCode.UNAUTHENTICATED, "账号或密码不正确")

    @staticmethod
    def _record_failure(tx: UnitOfWork, user_id: int, current: int) -> None:
        new_count = current + 1
        locked = new_count >= LOCK_THRESHOLD
        tx.execute(
            "UPDATE users SET failed_login_count=%s, locked_until=%s WHERE id=%s",
            (
                new_count,
                _utcnow() + timedelta(minutes=LOCK_MINUTES) if locked else None,
                user_id,
            ),
        )

    # -- 会话解析 -------------------------------------------------------------

    def resolve_session(self, token: str) -> AuthenticatedUser:
        """按会话令牌取回当前用户。**权限来自数据库，不来自请求体。**"""
        if not token:
            raise unauthenticated()
        digest = hash_token(token)
        with self._uow().begin() as tx:
            row = tx.query_one(
                """
                SELECT id, user_id, expires_at, revoked_at, last_seen_at
                FROM sessions WHERE token_hash=%s
                """,
                (digest,),
            )
            if row is None or row["revoked_at"] is not None:
                raise unauthenticated()
            if row["expires_at"] <= _utcnow():
                raise unauthenticated("登录状态已过期，请重新登录")

            # 滑动续期：每次成功访问把过期时间往后推。
            # 只更新"确实在活跃使用"的会话，因此空闲超时仍然有效。
            tx.execute(
                "UPDATE sessions SET last_seen_at=%s, expires_at=%s WHERE id=%s",
                (_utcnow(), _utcnow() + self._idle, int(row["id"])),
            )
            return self._load(tx, int(row["user_id"]), digest)

    def _load(self, tx: UnitOfWork, user_id: int, session_hash: str) -> AuthenticatedUser:
        user = tx.query_one(
            "SELECT id, username, display_name, status, must_change_password "
            "FROM users WHERE id=%s",
            (user_id,),
        )
        if user is None:
            raise unauthenticated()
        if user["status"] != "active":
            raise DomainError(ErrorCode.FORBIDDEN, "账号当前不可用，请重新登录")

        roles = tx.query_all(
            "SELECT r.code FROM user_roles ur JOIN roles r ON r.id=ur.role_id "
            "WHERE ur.user_id=%s AND r.status='active'",
            (user_id,),
        )
        role_codes = frozenset(str(row["code"]) for row in roles)

        # 注意 `AND r.status='active'` 在 join 条件里而不是 where 里——
        # 被停用的角色必须**同时**不贡献权限码。初版漏了这个条件，
        # 后果是停用一个角色后，持有该角色的用户仍然带着它的权限，直到重新登录。
        # 这类"停用了但没生效"的问题很难被发现，因为用户的界面看起来正常。
        permissions = tx.query_all(
            """
            SELECT DISTINCT p.code
            FROM user_roles ur
            JOIN roles r ON r.id = ur.role_id AND r.status = 'active'
            JOIN role_permissions rp ON rp.role_id = r.id
            JOIN permissions p ON p.id = rp.permission_id
            WHERE ur.user_id = %s
            """,
            (user_id,),
        )
        return AuthenticatedUser(
            user_id=int(user["id"]),
            username=str(user["username"]),
            display_name=str(user["display_name"]),
            status=str(user["status"]),
            permissions=frozenset(str(row["code"]) for row in permissions),
            role_codes=role_codes,
            session_hash=session_hash,
            must_change_password=bool(user["must_change_password"]),
        )

    def resolve_by_session_hash(self, session_hash: str) -> AuthenticatedUser:
        """按**会话哈希**解析用户。

        与 `resolve_session(token)` 的区别：后者接收原始令牌并自己算哈希；
        本方法接收已经算好的哈希。用的地方只有一个——Agent 上下文令牌：

            `X-Agent-Context` 的 payload 里带 `sid`（会话哈希），
            插件请求没有 Cookie，所以无法走 `resolve_session`。

        安全性不降低：令牌本身有 HMAC 签名，且这里仍然**重新查库**校验会话
        未过期、未撤销、账号仍可用。**"从令牌里读一个声称的身份"和
        "用令牌的签名换一次数据库查询"是两回事**，后者才是这里做的。
        """
        if not session_hash:
            raise unauthenticated()
        with self._uow().begin() as tx:
            row = tx.query_one(
                """
                SELECT id, user_id, expires_at, revoked_at
                FROM sessions WHERE token_hash=%s
                """,
                (session_hash,),
            )
            if row is None or row["revoked_at"] is not None:
                raise unauthenticated()
            if row["expires_at"] <= _utcnow():
                raise unauthenticated("登录状态已过期，请重新登录")
            tx.execute(
                "UPDATE sessions SET last_seen_at=%s WHERE id=%s",
                (_utcnow(), int(row["id"])),
            )
            return self._load(tx, int(row["user_id"]), session_hash)


    # -- 登出与改密 -----------------------------------------------------------

    def logout(self, token: str) -> None:
        if not token:
            return
        with self._uow().begin() as tx:
            tx.execute(
                "UPDATE sessions SET revoked_at=%s WHERE token_hash=%s AND revoked_at IS NULL",
                (_utcnow(), hash_token(token)),
            )

    def change_password(self, *, user_id: int, current: str, new_password: str) -> None:
        """改密并**撤销该用户的其他全部会话**。

        撤销是必须的：密码泄露场景下，攻击者可能已经建立了会话；改密如果不撤销旧会话，
        泄露方仍然在线。早期版本也做了这件事（`revoke_session` 链路），继承。
        """
        if len(new_password) < 8:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "新密码至少 8 位", data={"field": "new_password"})
        with self._uow().begin() as tx:
            row = tx.query_one("SELECT password_hash FROM users WHERE id=%s", (user_id,))
            if row is None:
                raise unauthenticated()
            if not verify_password(current, str(row["password_hash"])):
                raise DomainError(ErrorCode.FIELD_INVALID, "当前密码不正确", data={"field": "current_password"})
            tx.execute(
                "UPDATE users SET password_hash=%s, must_change_password=0 WHERE id=%s",
                (hash_password(new_password), user_id),
            )
            tx.execute(
                "UPDATE sessions SET revoked_at=%s WHERE user_id=%s AND revoked_at IS NULL",
                (_utcnow(), user_id),
            )

    # -- CSRF 与数据范围（供 web 层与诊断使用）-------------------------------

    def csrf_matches(self, session_token: str, submitted: str) -> bool:
        """三方比对的服务端一侧。

        比对的是**原值**（`sessions.csrf_token`），因为双提交模式要求服务端能把它与
        Cookie、请求头对齐。CSRF 令牌不是凭据，单独拿到它还需要一个有效会话配合，
        所以存原值是合理的取舍——这与会话令牌存哈希的理由不冲突，两者安全等级不同。
        """
        if not session_token or not submitted:
            return False
        import hmac

        with self._uow().begin() as tx:
            row = tx.query_one(
                "SELECT csrf_token, revoked_at, expires_at FROM sessions WHERE token_hash=%s",
                (hash_token(session_token),),
            )
        if row is None or row["revoked_at"] is not None:
            return False
        if row["expires_at"] <= _utcnow():
            return False
        stored = str(row.get("csrf_token") or "")
        if not stored:
            return False
        return hmac.compare_digest(stored, submitted)

    def scope_ids(self, user_id: int) -> tuple[list[int], list[int]]:
        """返回 ``(role_ids, scope_ids)``，供管理端表单回显。"""
        with self._uow().begin() as tx:
            roles = tx.query_all(
                "SELECT role_id FROM user_roles WHERE user_id=%s", (user_id,)
            )
            scopes = tx.query_all(
                "SELECT scope_id FROM user_data_scopes WHERE user_id=%s", (user_id,)
            )
        return (
            [int(row["role_id"]) for row in roles],
            [int(row["scope_id"]) for row in scopes],
        )

    def assign_roles_and_scopes(
        self, *, user_id: int, role_ids: list[int], scope_ids: list[int]
    ) -> None:
        """整体替换用户的角色与数据范围。

        **整体替换而不是增量合并**：增量语义下"取消勾选"无法表达——调用方只能传
        "要加的"，永远不知道该删哪个。整体替换让"最终状态"由一次请求唯一确定，
        这与 PATCH 语义不同，所以能力声明里它是 action 而不是 update。

        范围为空时自动补上默认的个人范围：否则该用户下次访问业务数据会
        `DATA_SCOPE_UNRESOLVED`，而管理员以为自己只是"暂时没勾选"。
        """
        with self._uow().begin() as tx:
            tx.execute("DELETE FROM user_roles WHERE user_id=%s", (user_id,))
            for role_id in sorted(set(role_ids)):
                tx.execute(
                    "INSERT INTO user_roles (user_id, role_id) VALUES (%s,%s)", (user_id, role_id)
                )
            tx.execute("DELETE FROM user_data_scopes WHERE user_id=%s", (user_id,))
            effective = list(dict.fromkeys(scope_ids))
            if not effective:
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


    def rotate_csrf_token(self, *, session_token: str, csrf_token: str) -> None:
        """重新签发 CSRF 令牌。

        前端可能丢失 CSRF Cookie（清缓存、多标签页竞争）。没有这个入口的话，
        唯一恢复方式是重新登录——而用户会以为"系统坏了"。
        """
        with self._uow().begin() as tx:
            tx.execute(
                "UPDATE sessions SET csrf_hash=%s, csrf_token=%s "
                "WHERE token_hash=%s AND revoked_at IS NULL",
                (hash_token(csrf_token), csrf_token, hash_token(session_token)),
            )


    # -- 维护 -----------------------------------------------------------------

    def sweep_expired_sessions(self) -> int:
        with self._uow().begin() as tx:
            return tx.execute(
                "DELETE FROM sessions WHERE expires_at <= %s OR revoked_at IS NOT NULL",
                (_utcnow(),),
            )


__all__ = [
    "AccessService",
    "AuthenticatedUser",
    "DEFAULT_IDLE_MINUTES",
    "LOCK_MINUTES",
    "LOCK_THRESHOLD",
    "hash_token",
    "new_token",
]
