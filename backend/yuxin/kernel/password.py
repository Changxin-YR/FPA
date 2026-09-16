"""密码哈希：scrypt（标准库）。

早期版本用 scrypt（`common/security/password.py`），方向正确。这里保留 scrypt 但把参数
固定成显式常量，并写清每个参数的作用——密码哈希的参数不该是"某个数"，而该是
"能解释为什么是这个数"。

    n=2^14  (16384)   CPU/内存成本。2^14 约 16 MB 内存/次，单次约 50-100ms。
    r=8               block size。与 n 一起决定内存用量：128 * n * r 字节 ≈ 16 MB。
    p=1               并行度。保持 1，靠每连接独立 salt 防彩虹表。
    dklen=32          输出长度。

**为什么不用 bcrypt/argon2**：既定技术栈没有它们，引入新依赖需要理由；而 scrypt
是标准库自带的、且是内存硬的（抵抗 GPU/ASIC 优于 PBKDF2）。这符合"技术栈不偷偷替换"
与"能复用就复用"两条。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: scrypt 参数。改这些值会让既有哈希失效，因此只在有明确理由时改。
_N = 2**14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16

#: 编码前缀。带前缀是为了将来能平滑升级参数而不破坏既有哈希——
#: 校验时按前缀决定用哪套参数。
_PREFIX = "scrypt"


def hash_password(password: str) -> str:
    """生成 ``scrypt$n$r$p$<salt_hex>$<hash_hex>`` 形式的哈希。"""
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN
    )
    return f"{_PREFIX}${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码。

    用 ``hmac.compare_digest`` 做定时安全比较——普通 ``==`` 会在第一个不同的字节处
    提前返回，理论上可通过计时差推断哈希内容。这类细节单独看微不足道，但密码校验是
    攻击者能无限次调用的入口，值得认真。

    任何格式错误都返回 False 而不是抛异常：调用方（登录）不该因为库里有一条脏哈希
    而 500；那条记录就是"永远登不上"，这正是它应该的表现。
    """
    if not password or not stored:
        return False
    try:
        prefix, n_text, r_text, p_text, salt_hex, hash_hex = stored.split("$")
        if prefix != _PREFIX:
            return False
        n = int(n_text)
        r = int(r_text)
        p = int(p_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False

    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (ValueError, MemoryError):
        # 参数越界（例如库里的 n 被改成天文数字）→ 拒绝，不 500
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    """判断一个已有哈希是否需要用当前参数重算。

    登录成功时顺手升级旧参数——这样改参数不需要一次性迁移全部用户。
    """
    try:
        prefix, n_text, r_text, p_text, _, _ = stored.split("$")
    except (ValueError, AttributeError):
        return True
    if prefix != _PREFIX:
        return True
    try:
        return (int(n_text), int(r_text), int(p_text)) != (_N, _R, _P)
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# 密码强度策略
#
# `docs/CAPABILITY_REGISTRY.md` §2.3（`auth.password.change`）把这份规则定在
# **内核**里，原文："早期版本该模块全仓只有 3 个使用点，属'薄通用层'；新系统把它
# 定位为 `kernel` 内的密码策略，不作为通用校验层。"
#
# 它放在本文件（而不是 `domains/access/`）的理由是"**谁判定**"：初始口令
# （`access.user.create`）与改密（`auth.password.change`）必须走**同一份**判定，
# 否则管理员能设一个用户自己改不成的口令。两处各写一遍就是"两处描述同一件事"。
# ---------------------------------------------------------------------------

#: 最短长度。与 registry §2.3 逐字一致（`new_password` 8–128）。
MIN_LENGTH = 8

#: 最长长度。它的作用不是安全，而是防止超长输入把 scrypt 变成 CPU 放大器。
MAX_LENGTH = 128

#: 弱口令黑名单（**统一小写后比较**）。
#:
#: 继承旧 `common/security/password.py:30-46` 的思路，但刻意保持短小：
#: 一张长长的黑名单会让人以为"不在名单里就安全"。它只挡最常见的几个，
#: 其余交给长度 + 字符种类 + 序列判定。
WEAK_PASSWORDS: frozenset[str] = frozenset(
    {
        "password", "passw0rd", "password1", "password123", "12345678", "123456789",
        "1234567890", "qwerty123", "abc12345", "admin123", "admin888", "11111111",
        "88888888", "iloveyou", "a1234567", "letmein1", "welcome1",
    }
)

#: 键盘横向序列片段。用"包含"判定，因此不必枚举全部长度。
_KEYBOARD_RUNS: tuple[str, ...] = (
    "qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890",
)

#: 连续字符 / 连续数字的**最短**长度：`abcd` / `1234` 这种 4 连即拒。
_MAX_RUN = 4


def password_problem(password: str) -> str | None:
    """返回不合规的**具体**理由；合规时返回 ``None``。

    为什么返回理由而不是布尔值：调用方要把它翻译成用户能照着改的错误消息，而
    "密码不符合要求"这种文案会让用户反复试。本项目对错误文案的一贯口径是
    **指向具体的修复动作**。

    ## 返回值**不含**主语（"密码"/"新密码"/"初始密码"）

    调用方自己拼主语。这么写的理由是实测到的一处文案缺陷：初版返回
    `"密码至少 8 位"`，而 `access.user.create` 的调用点是 `f"初始密码{reason}"`
    —— 拼出来是 **"初始密码密码至少 8 位"**。两个调用点
    （`access.user.create` 与 `identity.PasswordService.change_password`）要的主语不同，
    所以主语必须由调用方给，不能写死在返回值里。

    判定顺序即文案优先级：先长度、再字符种类、再可猜测性。
    """
    if not isinstance(password, str) or not password:
        return "不能为空"
    if len(password) < MIN_LENGTH:
        return f"至少 {MIN_LENGTH} 位"
    if len(password) > MAX_LENGTH:
        return f"最多 {MAX_LENGTH} 位"
    if not any(char.isalpha() for char in password) or not any(
        char.isdigit() for char in password
    ):
        # 两个分支合并成一句：分开写会给出两条不同的文案，而用户的修复动作相同
        # （加字母 / 加数字）。同一条缺陷给两种说法是"两处描述同一件事"。
        return "须同时包含字母与数字"
    lowered = password.lower()
    if lowered in WEAK_PASSWORDS:
        return "过于常见，请更换"
    if any(run in lowered for run in _KEYBOARD_RUNS):
        return "不能包含键盘连续序列（如 qwerty）"
    if _has_ascending_run(lowered, _MAX_RUN):
        return "不能包含连续数字或连续字母（如 1234、abcd）"
    if _has_repeated_run(password, _MAX_RUN):
        return "不能包含 4 个及以上重复字符（如 aaaa、1111）"
    return None


def _has_ascending_run(lowered: str, length: int) -> bool:
    """是否存在 ``length`` 个**码位连续**的字符（升序；入参已小写）。"""
    if length <= 1 or len(lowered) < length:
        return False
    run = 1
    for index in range(1, len(lowered)):
        if ord(lowered[index]) == ord(lowered[index - 1]) + 1:
            run += 1
            if run >= length:
                return True
        else:
            run = 1
    return False


def _has_repeated_run(password: str, length: int) -> bool:
    """是否存在 ``length`` 个连续相同的字符。"""
    if length <= 1 or len(password) < length:
        return False
    run = 1
    for index in range(1, len(password)):
        if password[index] == password[index - 1]:
            run += 1
            if run >= length:
                return True
        else:
            run = 1
    return False


__all__ = [
    "MAX_LENGTH",
    "MIN_LENGTH",
    "WEAK_PASSWORDS",
    "hash_password",
    "needs_rehash",
    "password_problem",
    "verify_password",
]
