"""Agent 请求的身份解析不应依赖 Cookie。

**根因**：`routes_agent._actor_from_context_token()` 里我调了 `_current_actor()`，
而后者从 **Cookie** 读会话令牌。但 Agent（Harness 插件）请求带的是
`X-Agent-Context` 头，**根本没有 Cookie** → `resolve_session("")` → 401。

契约 §5 其实写清楚了这是两套凭据：

| 场景 | 机制 |
|---|---|
| 浏览器 | HttpOnly Cookie 会话 + `X-CSRF-Token` |
| Harness 插件 → Gateway | `X-Agent-Context` 上下文令牌 |

我在实现时把两者混成了同一条路径。正解：让 access 域支持**按会话哈希直接解析**
（上下文令牌的 payload 里本来就有 `sid` = 会话哈希），不经过 Cookie。

这样也有一个额外好处：**权限取的是实时值**——每次调用都用 `sid` 重新查库，
所以管理员撤销权限、用户登出，下一次调用立刻生效（这是 Q17 那条"授权实时性"
的具体实现点）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCESS = ROOT / "backend" / "yuxin" / "domains" / "access" / "service.py"
ROUTES = ROOT / "backend" / "yuxin" / "web" / "routes_agent.py"

# --- 1) AccessService 增加按会话哈希解析 -----------------------------------

METHOD = '''    def resolve_by_session_hash(self, session_hash: str) -> AuthenticatedUser:
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


'''

# --- 2) routes_agent 改用它 ------------------------------------------------

OLD_ACTOR = '''        from yuxin.web.app import _current_actor

        token = request.headers.get("X-Agent-Context", "")
        payload = verify_context_token(current_app.config["SECRET_KEY"], token)

        # 令牌里的 sid 是会话哈希；用它反查当前会话，确保：
        #   * 会话仍然有效（未被登出/撤销）
        #   * 权限取的是**当前**的，而不是签发令牌时的快照
        # 第二条很重要：管理员在令牌签发后撤销了某人的权限，该令牌必须立即失效。
        actor = _current_actor()
        if actor.session_hash != str(payload.get("sid")):
            raise DomainError(
                ErrorCode.AGENT_CONTEXT_INVALID, "智能助手凭据与会话不匹配，请重新发起"
            )
        return actor, payload'''

NEW_ACTOR = '''        access = current_app.config["YUXIN_ACCESS"]
        token = request.headers.get("X-Agent-Context", "")
        payload = verify_context_token(current_app.config["SECRET_KEY"], token)

        # 用令牌 payload 里的 sid（会话哈希）**直接查库**解析用户。
        #
        # 这里**不能**读 Cookie：Agent 请求走的是 `X-Agent-Context`，
        # 浏览器会话 Cookie 根本不存在（契约 §5 明确这是两套凭据）。
        # 初版复用了 `_current_actor()`（读 Cookie），于是每个 Agent 请求都 401。
        #
        # 每次调用都重新查库，所以权限是**实时**的：
        # 管理员撤销权限、用户登出，下一次调用立刻生效。
        user = access.resolve_by_session_hash(str(payload.get("sid") or ""))

        return (
            ActorView(
                user_id=user.user_id,
                username=user.username,
                permissions=user.permissions,
                role_codes=user.role_codes,
                session_hash=user.session_hash,
                is_agent=True,
                conversation_id=str(payload.get("cid") or "") or None,
            ),
            payload,
        )'''

# 需要 ActorView 导入
IMPORT_OLD = "from yuxin.kernel.errors import DomainError, ErrorCode"
IMPORT_NEW = "from yuxin.kernel.errors import DomainError, ErrorCode\nfrom yuxin.kernel.runner import ActorView"


def patch(path: Path, pairs: list[tuple[str, str]], label: str) -> bool:
    source = path.read_text(encoding="utf-8")
    changed = 0
    for old, new in pairs:
        if old not in source:
            print(f"  FAIL {label}：找不到片段 → {old.splitlines()[0][:60]}")
            return False
        source = source.replace(old, new, 1)
        changed += 1
    if changed:
        ast.parse(source)
        path.write_text(source, encoding="utf-8", newline="\n")
    print(f"  {label}：{changed} 处")
    return True


def main() -> int:
    source = ACCESS.read_text(encoding="utf-8")
    if "def resolve_by_session_hash(" not in source:
        anchor = "    # -- 登出与改密 -----------------------------------------------------------"
        if anchor not in source:
            print("FAIL：AccessService 锚点未找到")
            return 1
        source = source.replace(anchor, METHOD + anchor, 1)
        ast.parse(source)
        ACCESS.write_text(source, encoding="utf-8", newline="\n")
        print("  AccessService：新增 resolve_by_session_hash")
    else:
        print("  AccessService：已存在")

    ok = patch(ROUTES, [(OLD_ACTOR, NEW_ACTOR), (IMPORT_OLD, IMPORT_NEW)], "routes_agent")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
