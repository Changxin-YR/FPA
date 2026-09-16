"""守卫：提权类确认卡**必须让人看懂自己在批准什么**。

## 这条守卫为什么存在

实测（t2 + 当前迭代复核）`access.user.grants` 的确认卡原先长这样：

    target : user_id=7
    rows   : 角色 = [9]      数据范围 = [2]
    impact : 写入业务数据

用户名、姓名、角色名、范围名**一个都不在卡片上**；`rows` 里的 `[9]` 是 Python
列表的 `str()`（用户读到了一种编程语言的语法）；`impact` 还把"提权"说成了普通写入。

**危害**：确认闸门是提权的**唯一人工关口**，而它的全部价值就在"用户能看懂这一眼"。
看不到名字，HITL 就退化成点一下的仪式 —— 卡还在、点得动、但没人真正批准了什么。

## 判据（三条，各钉一层）

1. **声明层**：4 条提权能力都必须声明 `confirmation_labels`（可读化解析器）；
2. **渲染层**：`_rows` 不得把 list/dict/None 渲染成 Python 字面量（`[9]` / `{'a': 1}`）；
3. **文案层**：`access` / `identity` 域的 `impact` 必须写明这是**权限/账号变更**，
   而不是落进"写入业务数据"那句静态兜底。

另有一条**行为层**用例：用假 tx 直接调解析器，断言 id 真的被换成了业务名字。
"""

from __future__ import annotations

from typing import Any

from conftest import load_all_status

#: 4 条提权类能力（§5.2 讨论过的那 4 条里，进了注册表的那些）
ACCESS_CAPABILITIES = (
    "access.user.create",
    "access.user.status",
    "access.user.grants",
    "access.role.permissions",
)


def test_提权能力都声明了可读化解析器():
    load_all_status()
    from yuxin.kernel.capability import REGISTRY

    missing = [
        name for name in ACCESS_CAPABILITIES
        if not getattr(REGISTRY.get(name), "confirmation_labels", None)
    ]
    assert not missing, (
        "这些提权能力没有声明 `confirmation_labels` —— 它们的卡片会退回裸 id，"
        f"用户看不懂自己在批准什么：{missing}"
    )


def test_列表与字典值不得渲染成_python_字面量():
    """`str([9])` → `'[9]'`：卡片上出现的是**编程语言的语法**，不是业务值。"""
    load_all_status()
    from yuxin.kernel.capability import (
        Capability,
        Confirmation,
        Field,
        FieldSet,
        HttpMethod,
        Risk,
    )
    from yuxin.kernel.confirmation import ConfirmationGate

    capability = Capability(
        name="probe.card.render",
        title="探针卡片",
        domain="access",
        resource="probe",
        handler=lambda **_: None,  # type: ignore[arg-type]
        method=HttpMethod.POST,
        path="/api/v1/probe-card",
        kind="create",
        required_permission="probe.manage",
        risk=Risk.HIGH,
        confirmation=Confirmation.ALWAYS,
        fields=FieldSet(
            fields={
                "role_ids": Field(type="array", label="角色"),
                "scope_ids": Field(type="array", label="数据范围"),
            }
        ),
    )
    rows = ConfirmationGate._rows(
        capability, {"role_ids": [9, 10], "scope_ids": [2]}, None
    )
    values = {row["label"]: row["value"] for row in rows}
    assert values["角色"] == "9、10", f"列表应渲染成顿号分隔，实际 {values['角色']!r}"
    assert values["数据范围"] == "2"
    for row in rows:
        assert not row["value"].startswith("["), (
            f"卡片上出现了 Python 列表字面量：{row!r}"
        )


def test_提权能力的_impact_必须写明是权限变更():
    load_all_status()
    from yuxin.kernel.capability import REGISTRY
    from yuxin.kernel.runner import CapabilityRunner

    for name in ACCESS_CAPABILITIES:
        text = "".join(CapabilityRunner._impact_of(REGISTRY.get(name)))
        assert "权限" in text or "账号" in text, (
            f"{name} 的 impact 没有说明这是权限/账号变更，实际：{text!r}"
        )
        assert text != "写入业务数据", (
            f"{name} 的 impact 落进了静态兜底 —— 它会让用户以为这只是一次普通写入"
        )


class _FakeTx:
    """只实现解析器用到的那两个只读方法。"""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def query_one(self, sql: str, params: Any = ()) -> dict[str, Any] | None:
        self.queries.append(sql)
        if "FROM users" in sql:
            return {"username": "seller", "display_name": "周海霞"}
        if "FROM roles" in sql:
            return {"code": "inspector", "name": "质检核验员"}
        return None

    def query_all(self, sql: str, params: Any = ()) -> list[dict[str, Any]]:
        self.queries.append(sql)
        # 「读当前分配」的语句形如 `FROM user_roles AS j JOIN roles AS t …`，
        # 所以必须先判 `FROM user_roles`，否则会被下面的 `FROM roles` 抢先命中。
        if "FROM user_roles" in sql:
            return [{"label": "销售员"}]
        if "FROM user_data_scopes" in sql:
            return [{"label": "嵊泗列岛养殖基地（全部塘口）"}]
        if "FROM roles" in sql:
            return [{"label": "质检核验员"}]
        if "FROM data_scopes" in sql:
            return [{"label": "嵊泗列岛养殖基地（全部塘口）"}]
        if "FROM permissions" in sql:
            return [{"code": "pond.view", "name": "pond.view"}]
        return []


def test_解析器把裸_id_换成业务名字():
    """行为层：id（7 / [9] / [2]）必须变成名字，而不是原样留在卡上。"""
    load_all_status()
    from yuxin.domains.access.admin import confirmation_labels

    tx = _FakeTx()
    labels = confirmation_labels(
        tx, {"user_id": 7, "role_ids": [9], "scope_ids": [2]}
    )

    assert labels["target"] == "周海霞（seller）", labels
    # ★ 这两行是**覆盖**语义，卡片必须同时给出"变更前"：只写变更后时，用户看不出
    #   原角色会被移除 —— 实测发生过（模型只传 [质检核验员]，点确认后销售员被静默覆盖）。
    assert labels["rows"]["role_ids"] == "销售员  →  质检核验员", labels
    # 与当前值相同 ⇒ 明确写"不变"，而不是让人以为它也会被重写
    assert labels["rows"]["scope_ids"] == "嵊泗列岛养殖基地（全部塘口）（不变）", labels

    # 权限码：名字与 code 相同时只显示一次（`permissions.name` 有一部分就是 code）
    perm_labels = confirmation_labels(tx, {"role_id": 8, "permission_codes": ["pond.view"]})
    assert perm_labels["target"] == "质检核验员（inspector）", perm_labels
    assert perm_labels["rows"]["permission_codes"] == "pond.view", perm_labels
